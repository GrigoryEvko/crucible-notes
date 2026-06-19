# ISA Batch 09 — Vector Move / regfile bridge

This batch is the **register-transfer layer** of the Vision-Q7 *Cairo* (`ncore2gp`) vector ISA: the
`ivp_mov*` family that copies data **within** a register file and **across** the eight register files.
It is the page that documents *how the eight files interconnect at the ISA level* — every plain
register-to-register copy (`movvv` on `vec`, `movprpr` on `b32_pr`, `movww` on `wvec`), every
AR↔vec bridge (`movva*` broadcast, `movav*` extract), the predicate bridges (`movvpr`/`movpra32`),
the predicated/mask-gated merge (`mov2nx8t`), the immediate-to-`vec` loads (`movvint*`), and the
scalar-state bridges (`movvfs`/`movfsv`, `movvscf`/`movscfv`). It owns **27 mnemonics / 246 of the
12 569 shipped placements** ([the coverage tally's certified denominator](../core/coverage-tally.md)),
all in package `xt_ivp32`, all on the vector (`ivp_`) axis.

Everything below is re-grounded against the shipped binaries this pass: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks, `Field_*_get` accessors, the `opcodes[]`
table read directly for `opc#`/`iclass`/package), the **value semantics** by *executing* the matching
`module__xdref_*` leaves in `libfiss-base.so` live in-process (license-free), the **pipeline/slot
model** from `libcas-core.so` (the `…_inst_IVP_MOV*_issue` per-slot functions), and a byte-exact
**encode/decode oracle** from the device-native `xtensa-elf-as`/`xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`). Confidence tags per [the Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / proven-by-execution, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a sibling page's confidence.

> **Scope split — read this before pairing a mnemonic to a batch.** This batch is the **explicit
> register-transfer / regfile-bridge core**. Four boundaries are enforced so the 30 batches don't
> double-count:
> * **`ivp_movgatherd` → [B19](b19-scatter-gather.md), NOT here.** It *spells* `mov` but the
>   [partition classifier](template-and-partition.md) gives `scatter|gather` priority **over** `mov`;
>   `movgatherd` is the gather-descriptor loader that sits in the `gatherd2nx8_h`/`gatherdnx16` family
>   (verified: it is the only `mov*` op in the scatter/gather `nm` glob). It is excluded from this
>   batch's 27/246 — the reason B09 lands on **27**, not 28, `mov*` mnemonics.
> * **`wvec → vec` PACK (wide→narrow accumulator readout, `ivp_pack*`) → [B10](b10-wvec-pack.md).**
>   We own `movww` (the **full 1536-bit `wvec`→`wvec` copy**, no narrowing); the *narrowing* readout
>   of a 1536-bit accumulator down to a 512-bit `vec` is a `pack`, not a `mov`, and is B10's.
> * **COMPARE-produced `vec→vbool` predicates (`ivp_eq*`/`ivp_lt*`/…) → [B01](b01-vec-alu-int.md) /
>   [B02](b02-vec-alu-fp.md) / [B03](b03-vec-alu-rest.md).** We own only the **explicit `mov`/extract/
>   insert** predicate bridges (`movvpr`, `movpra32`, `movab1`, `movba1`); a *compare* that happens to
>   write a `vbool` is owned by the ALU batches that produce it.
> * **Pattern-driven replicate / splat-from-immediate (`ivp_rep*`/`ivp_splat*`) → [B16](b16-vec-rep.md).**
>   We own the *immediate-scalar*→`vec` broadcast forms named `mov*` (`movvint*`, `movva*`); the
>   replicate-a-pattern / inject-lane family (`rep`/`splat`/`inj`) is B16's by classifier verb. The
>   `movpa16`/`movqa16` *pack-half* selects (`p`/`q`) are borderline replicate ops but ship spelled
>   `mov*`, so they stay here; cited as adjacency to B16.

---

## 1. Batch key facts

| Fact | Value | Binary source |
|---|---|---|
| Axis / package | vector (`ivp_`) / **`xt_ivp32`** for all 27 | `opcodes[].package` @ `+0x08`, parsed for all 27 rows this pass `[HIGH/OBSERVED]` |
| Mnemonics this batch | **27** | §2; `nm libisa-core.so` distinct `Opcode_ivp_mov*` minus `movgatherd` |
| Placements this batch | **246** | per-mnemonic `nm \| rg -c` sum (§6) |
| Register files bridged | all 8 — **AR, BR, vec, vbool, b32_pr, wvec, gvr(via scf), + scalar-FP state** | §3 bridge matrix; [register-files](../core/register-files.md) |
| Dominant slot classes | **ALU** (intra-file + extract), **Ld** (broadcast/immediate inject), **Mul** (`movww`/`movscfv` wide) | `opcodedefs[]` slots + `libcas-core` issue fns (§3.4) |
| Value-leaf family | `mov_<outw>_<inw>` width-typed leaves (e.g. `mov_512_512`, `mov_1_32`, `mov_64_64`) | `nm libfiss-base.so \| rg module__xdref_mov` (§4) |
| Encode-thunk ABI | `C7 07 imm32 [C7 47 04 0] C3` — `imm32` = the `(opcode×slot)` selector, `word1==0` | [flix-encoding §6.1](../core/flix-encoding.md) |
| Move latency (`movvv`) | rides the **LdSt slot** datapath (`F0_S0_LdSt_4_inst_IVP_MOVVV_issue` in cas-core) | `libcas-core.so` `[HIGH/OBSERVED]` |
| Oracle | `xtensa-elf-as`/`objdump`, `XTENSA_CORE=ncore2gp` | **19 of 27 round-trip byte-exact** (§5) |

> **The batch is one mux, three datapaths, eight ports.** Every `ivp_mov*` op is a *route* through a
> single transfer network: it reads one source port (one of the eight files, or an immediate, or
> scalar state) and writes one destination port, optionally reshaping width and optionally gating per
> lane by a predicate. The encoding splits the family across **three FLIX slot classes by transfer
> kind** (§3.4): **ALU-class** for intra-`vec`/extract/predicate moves, **Ld-class** for the
> broadcast/immediate-inject moves (a scalar→`vec` move is structurally a load), **Mul-class** for the
> two wide moves (`movww` 1536-bit, `movscfv`). A reimplementation builds one parameterized
> route-and-reshape unit and selects (src-file, dst-file, width-map, mask-en) from the decoded opcode.
> `[HIGH/OBSERVED]`

---

## 2. Batch roster — 27 register-transfer opcodes

Columns: `mnemonic` · `lanes/width` · representative `FLIX slot` and its **opcode-selector imm** (the
`Opcode_<mnem>_Slot_<slot>_encode` thunk's `movl $imm`, disassembled this pass) · `opc#` (the
`opcodes[]` row index) · `src→dst` register-file bridge · device byte-size of the bundle the op lands
in · one-line semantics · `[conf]`. The selector imm is for the **named representative slot only** —
the selector is per-`(opcode×slot)` (the [flix-encoding §6.2 two-tier rule](../core/flix-encoding.md);
the B01 GOTCHA holds here too). `opc#`/`iclass`/package were read by walking `opcodes[]` at file
offset `0x4ce6c0` (VMA `0x6ce6c0` − `0x200000`), stride 72, all 27 resolved.

### 2.1 Intra-file copies (no width change, source-file = dest-file)

| mnemonic | shape | rep. slot · sel imm | opc# | src→dst | bytes | semantics | conf |
|---|---|---|---|---|---|---|---|
| `ivp_movvv`   | 512b | `F0_S0_LdSt` `0x10dc4002` | 407 | `vec`→`vec` | 8/2 | `vd = vs` (full 512-bit copy) | `[HIGH/OBSERVED]` |
| `ivp_movprpr` | 64b  | `F0_S3_ALU` `0x82ba820a` | 1376 | `b32_pr`→`b32_pr` | 8/2 | `prd = prs` (64-bit predicate copy) | `[HIGH/OBSERVED]` |
| `ivp_movww`   | 1536b| `F0_S2_Mul` `0x01043100` | 1074 | `wvec`→`wvec` | 8 | `wvd = wvs` (full 1536-bit accumulator copy) | `[HIGH/OBSERVED]` |

### 2.2 AR ↔ vec bridges (32-bit scalar register ⇄ vector lane)

`movva*` **broadcast/inject**: an `AR` scalar value is written into `vec` (one lane / splat low lane);
issues on the **Ld** slot class (a scalar→vec move is structurally a load). `movav*` **extract**: one
`vec` lane is read out to an `AR` scalar with the dtype's zero/sign-extend; issues on the **ALU** class.

| mnemonic | lanes×w | rep. slot · sel imm | opc# | src→dst | reshape | conf |
|---|---|---|---|---|---|---|
| `ivp_movva8`   | 8→32  | `F0_S1_Ld` `0x00602008` | 1088 | `AR`→`vec` | low 8b of AR → vec 8b lane | `[HIGH/OBSERVED]` |
| `ivp_movva16`  | 16→32 | `F0_S1_Ld` `0x00602006` | 406 | `AR`→`vec` | low 16b of AR → vec 16b lane | `[HIGH/OBSERVED]` |
| `ivp_movva32`  | 32→32 | `F0_S1_Ld` `0x00602007` | 1072 | `AR`→`vec` | 32b AR → vec 32b lane | `[HIGH/OBSERVED]` |
| `ivp_movav8`   | 8→32  | `F0_S3_ALU` `0x82b9820a` | 1220 | `vec`→`AR` | vec 8b lane → AR (zero-ext) | `[HIGH/OBSERVED]` |
| `ivp_movav16`  | 16→32 | `F0_S3_ALU` `0x82b9020a` | 522 | `vec`→`AR` | vec 16b lane → AR (sign-ext) | `[HIGH/OBSERVED]` |
| `ivp_movav32`  | 32→32 | `F0_S3_ALU` `0x82b9020b` | 1073 | `vec`→`AR` | vec 32b lane → AR | `[HIGH/OBSERVED]` |
| `ivp_movavu8`  | 8→32  | `F0_S3_ALU` `0x82ba020a` | 1089 | `vec`→`AR` | vec 8b lane → AR (zero-ext) | `[HIGH/OBSERVED]` |
| `ivp_movavu16` | 16→32 | `F0_S3_ALU` `0x82b9820b` | 523 | `vec`→`AR` | vec 16b lane → AR (zero-ext) | `[HIGH/OBSERVED]` |

### 2.3 Predicate / boolean bridges (vbool · b32_pr · BR · AR)

| mnemonic | shape | rep. slot · sel imm | opc# | src→dst | semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_movvpr`   | 512b/64b | `F0_S3_ALU` `0x85000010` | 1375 | `b32_pr`→`vec` | predicate-reg → vec (1-bit-per-lane expand) | `[HIGH/OBSERVED]` (args: `vt←prr`) |
| `ivp_movpra32` | 32→64 | `F0_S0_LdSt` `0x11040600` | 1377 | `AR`→`b32_pr` | AR 32b → 64b predicate, **sign-replicate high word** | `[HIGH/OBSERVED]` |
| `ivp_movab1`   | 1b | `F0_S3_ALU` `0x64815000` | 855 | `BR`→`AR` | one boolean bit → AR (0/1) | `[HIGH/OBSERVED]` (args: `art←vbr`) |
| `ivp_movba1`   | 1b | `F0_S1_Ld` `0x004a0ce0` | 854 | `AR`→`BR` | AR bit0 → one boolean | `[HIGH/OBSERVED]` (args: `vbt←ars`) |

### 2.4 Predicated (mask-gated) vec move

| mnemonic | lanes×w | rep. slot · sel imm | opc# | src→dst | semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_mov2nx8t` | 64×8 | `F0_S3_ALU` `0x80400000` | 447 | `vec`→`vec` (mask `vbool`) | per-lane `vd_lane = mask_lane ? vs_lane : vd_lane` (merge) | `[HIGH/OBSERVED]` |

### 2.5 Immediate-to-vec inject / pack-half selects

`movvint*`/`movpint*`/`movqint*`/`movvinx16` synthesize a constant into `vec` from an **immediate**
operand (no register source) — a scalar broadcast that is, like the `movva*` broadcasts, a Ld-class op.
`movpa16`/`movqa16` are `vec`→`vec` **pack-half** selects (`p`=even/low half, `q`=odd/high half).

| mnemonic | shape | rep. slot · sel imm | opc# | src→dst | semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_movvint8`  | imm→8 | `F0_S1_Ld` `0x005c6080` | 1087 | imm→`vec` | imm broadcast to 8b lanes | `[HIGH/OBSERVED]` |
| `ivp_movvint16` | imm→16 | `F0_S1_Ld` `0x005c6000` | 516 | imm→`vec` | imm broadcast to 16b lanes | `[HIGH/OBSERVED]` |
| `ivp_movvinx16` | imm→16 | `F0_S1_Ld` `0x00622000` | 519 | imm→`vec` | indexed-imm inject (16b) | `[HIGH/OBSERVED]` |
| `ivp_movpint16` | imm→16 | `F0_S1_Ld` `0x005c4000` | 489 | imm→`vec` (low half) | imm → even-lane (`p`) half | `[HIGH/OBSERVED]` opc; `[MED]` half |
| `ivp_movqint16` | imm→16 | `F0_S1_Ld` `0x005c4001`◇ | 517 | imm→`vec` (high half) | imm → odd-lane (`q`) half | `[HIGH/OBSERVED]` opc; `[MED]` half |
| `ivp_movpa16`   | 16 | `F0_S1_Ld` `0x00602004` | 490 | `vec`→`vec` | even-lane (`p`) half select | `[HIGH/OBSERVED]` opc; `[MED]` half |
| `ivp_movqa16`   | 16 | `F0_S1_Ld` `0x00602005` | 518 | `vec`→`vec` | odd-lane (`q`) half select | `[HIGH/OBSERVED]` opc; `[MED]` half |

### 2.6 Scalar-state bridges (FP state · control flags)

`movvfs`/`movfsv` move the **scalar floating-point register-state** (8×64-bit lanes) in/out of `vec`;
`movvscf`/`movscfv` pack/unpack the **scalar control-flag** word (the `gvr`/VFPU CSR view) to/from
`vec`. These are the widest-fanout transfers (the value leaves carry 8–12 sub-field width tokens).

| mnemonic | rep. slot · sel imm | opc# | src→dst | semantics | conf |
|---|---|---|---|---|---|
| `ivp_movvfs`  | `F0_S1_Ld` `0x006020ac`  | 1381 | scalar-FP-state→`vec` | 8×64b FP state lanes → vec | `[HIGH/OBSERVED]` opc; `[MED/INFERRED]` field map |
| `ivp_movfsv`  | `F0_S3_ALU` `0x6498de00` | 1380 | `vec`→scalar-FP-state | vec → 8×64b FP state lanes | `[HIGH/OBSERVED]` opc; `[MED/INFERRED]` field map |
| `ivp_movvscf` | `F0_S1_Ld` `0x006020ad`  | 839 | scalar-control-flags→`vec` | unpack 12-field CSR → vec | `[HIGH/OBSERVED]` opc; `[MED/INFERRED]` field map |
| `ivp_movscfv` | `F0_S2_Mul` `0x010c3081` | 838 | `vec`→scalar-control-flags | pack vec → 12-field CSR word | `[HIGH/OBSERVED]` opc; `[MED/INFERRED]` field map |

`◇` = `movqint16` selector is the `+1` sibling of `movpint16` (`movpa16`/`movqa16` step
`0x602004→0x602005`; the same `+1` even/odd half nibble); `[HIGH/OBSERVED]` on the `p`-form imm and the
step, `[MED/INFERRED]` on the exact `q` cell.

> **GOTCHA — the `mov<X><Y>` letter pair keys the *bridge*, and the order is `dst`-then-`src`.**
> `movav8` = **a**(AR)←**v**(vec) **extract**; `movva8` = **v**(vec)←**a**(AR) **broadcast** — the same
> two letters, swapped, encode opposite directions on *different slot classes* (extract on ALU, broadcast
> on Ld; §3.4). `pr`=`b32_pr`, `b`=boolean(BR/vbool), `w`=`wvec`, `fs`=FP-state, `scf`=control-flags,
> `int`=immediate-integer. Do **not** read `movav` as "av = a-then-v written L-to-R = AR→vec"; the leaf
> width proves the direction (`movav8`→`mov_8_32`: **8-bit in, 32-bit out** = vec-lane→AR). The roster's
> `src→dst` column is the **leaf-width-proven** direction, not the letter order. `[HIGH/OBSERVED]`

---

## 3. The regfile-bridge matrix — how the 8 files interconnect

This is the table a reimplementer needs: for each `mov` mnemonic, the **source file → destination
file**, the **width reshape**, and the **lane/bit mapping**. The eight files
([register-files](../core/register-files.md)): `AR` (idx0, 32b×64), `BR` (idx1, 1b×16), `vec` (idx2,
512b×32), `vbool` (idx3, 64b×16), `valign` (idx4, 512b×4), `wvec` (idx5, 1536b×4), `b32_pr` (idx6,
64b×16), `gvr` (idx7, 512b×8). The width reshape is **proven by the value-leaf name** (`mov_<outw>_<inw>`).

| mnemonic | source file | dest file | width map (out ← in) | reshape mechanism | value leaf |
|---|---|---|---|---|---|
| `movvv`   | `vec` 512 | `vec` 512 | 512 ← 512 | identity (4× `movdqu`) | `mov_512_512` |
| `movprpr` | `b32_pr` 64 | `b32_pr` 64 | 64 ← 64 | identity (2× 32b word) | `mov_64_64` |
| `movww`   | `wvec` 1536 | `wvec` 1536 | 1536 ← 1536 | identity (12× `movdqu`) | `mov_1536w_1536w` |
| `movva8`  | `AR` 32 | `vec` 8-lane | 8 ← 32 | truncate low byte, write lane | `mov_8_32` (inverse use) |
| `movva16` | `AR` 32 | `vec` 16-lane | 16 ← 32 | truncate low half-word | `mov_16_32` (inverse use) |
| `movva32` | `AR` 32 | `vec` 32-lane | 32 ← 32 | identity to lane | `mov_32_32` |
| `movav8`  | `vec` 8-lane | `AR` 32 | 32 ← 8 | **zero-extend** | `mov_8_32` |
| `movav16` | `vec` 16-lane | `AR` 32 | 32 ← 16 | **sign-extend** | `mov_32s_16` |
| `movav32` | `vec` 32-lane | `AR` 32 | 32 ← 32 | identity | `mov_32_32` |
| `movavu8` | `vec` 8-lane | `AR` 32 | 32 ← 8 | zero-extend | `mov_8_32` |
| `movavu16`| `vec` 16-lane | `AR` 32 | 32 ← 16 | **zero-extend** (`u`) | `mov_32u_16` |
| `movvpr`  | `b32_pr` 64 | `vec` 512 | 1-bit/lane → lane | predicate **expand** (args `vt←prr`) | `mov_64_64`/`mov_1_32` |
| `movpra32`| `AR` 32 | `b32_pr` 64 | 64 ← 32 | **sign-replicate high word** | `movpra32_64_32` |
| `movab1`  | `BR` 1 | `AR` 32 | 32 ← 1 | zero-extend bit (args `art←vbr`) | `mov_32_1` |
| `movba1`  | `AR` 1 | `BR` 1 | 1 ← 32 | extract bit0 (args `vbt←ars`) | `mov_1_32` |
| `mov2nx8t`| `vec` 512 (+`vbool` mask) | `vec` 512 | 8/lane masked | per-lane select | `mov_8_8_8_t` |
| `movvint*`| imm | `vec` 8/16-lane | lane ← imm | broadcast | `mov_8_8`/`mov_16_16` class |
| `movpa16`/`movqa16` | `vec` half | `vec` half | 16 ← 16 | even/odd half select | `mov_16p_32`/`mov_16q_32` |
| `movvfs`/`movfsv` | FP-state ⇄ `vec` | — | 512 ⇄ 8×64 | 8-lane state transfer | `mov{vfs,fsv}_…` |
| `movvscf`/`movscfv` | CSR ⇄ `vec` | — | 32 ⇄ 12 fields | pack/unpack | `mov{vscf,scfv}_…` |

> **The matrix has a hub-and-spoke shape: `vec` is the hub.** Of the 27 ops, **24 touch `vec`**; the
> only non-`vec` transfers are `movprpr` (`b32_pr`↔`b32_pr`), `movww` (`wvec`↔`wvec`), and `movpra32`
> (`AR`→`b32_pr`). There is **no direct `wvec`↔`vec`, `vbool`↔`b32_pr`, or `gvr`↔`AR` `mov`** — those
> reshapes route through `pack`/`unpack` (B10/B22) or through `vec` in two hops. A reimplementation's
> transfer crossbar needs only the spokes in this table; the missing edges are *intentionally* not in
> the `mov` ISA. `[HIGH/OBSERVED]` on the enumerated edges; `[MED/INFERRED]` on the *absence* claim
> (it is an enumeration over the 27, not a proof no other op reshapes).

### 3.1 The AR↔vec bridge — lane extract and broadcast (C pseudocode)

The `movav*`/`movva*` pair is the scalar/vector boundary. The value-leaf bodies (disassembled and
executed live, §4) give the exact bit map:

```c
// ivp_movav8  : vec 8-bit lane -> AR 32-bit, ZERO-extend   (leaf mov_8_32)
uint32_t movav8 (const uint8_t  *vec, int lane){ return (uint32_t)vec[lane]; }          // & 0xff
// ivp_movav16 : vec 16-bit lane -> AR 32-bit, SIGN-extend   (leaf mov_32s_16)
int32_t  movav16(const int16_t  *vec, int lane){ return (int32_t)vec[lane];  }          // sext
// ivp_movavu16: vec 16-bit lane -> AR 32-bit, ZERO-extend   (leaf mov_32u_16)
uint32_t movavu16(const uint16_t*vec, int lane){ return (uint32_t)vec[lane]; }          // & 0xffff
// ivp_movva8  : AR 32-bit -> vec 8-bit lane, truncate low byte (broadcast/inject)
void     movva8 (uint8_t *vec, int lane, uint32_t ar){ vec[lane] = (uint8_t)ar; }       // low byte
```

The decisive asymmetry: **`movav16` sign-extends, `movavu16` zero-extends** — two distinct opcodes
(`opc# 522` vs `523`, distinct `iclass`), not a flag. A reimplementation that always zero-extends a
16-bit lane to AR is wrong for the signed `movav16` (e.g. `0x8000 → 0xffff8000`, not `0x00008000`).
`[HIGH/OBSERVED by execution]`

### 3.2 The predicate bridge — `movpra32` AR→b32_pr (sign-replicate)

`b32_pr` is a 64-bit packed-predicate file. `movpra32` widens a 32-bit AR value into it by writing the
low word verbatim and **replicating the AR sign bit across the entire high word** (`mov esi→(out);
sar $31,esi; mov esi→4(out)`):

```c
// ivp_movpra32 : AR 32b -> b32_pr 64b, sign-replicate high word (leaf movpra32_64_32, executed live)
void movpra32(uint64_t *pr, int32_t ar){
    uint32_t lo = (uint32_t)ar;
    uint32_t hi = (ar < 0) ? 0xFFFFFFFFu : 0x00000000u;   // = (uint32_t)(ar >> 31)
    *pr = ((uint64_t)hi << 32) | lo;
}
```

Executed live (§4): `movpra32(0x80000000) → 0xFFFFFFFF_80000000`, `movpra32(0x7fffffff) →
0x00000000_7fffffff`. This is how a sign-determined predicate is materialised in `b32_pr` from a scalar
condition. `[HIGH/OBSERVED by execution]`

### 3.3 The boolean extract/insert — `movab1`/`movba1` (AR↔BR, args-confirmed)

The operand args list (`Iclass_IVP_MOVAB1_args`: `art`(AR) out ← `vbr`(BR) in; `MOVBA1`: `vbt`(BR) out
← `ars`(AR) in) pins the files: these bridge the scalar **`AR`** (idx0) and the scalar **`BR`** boolean
file (idx1), the 1-bit-per-register conditional file used by base-Xtensa branch booleans — **not**
`vbool` (idx3, the per-lane vector predicate). `movba1` (AR→BR) keeps **only bit 0** of the AR
(`and $0x1`); `movab1` (BR→AR) zero-extends the single bit to a 0/1 in AR. Value leaves: `mov_1_32`
(extract bit0), `mov_32_1` (zero-extend):

```c
// ivp_movba1 : AR -> boolean (BR/vbool 1-bit) — keep bit0   (leaf mov_1_32, executed live)
unsigned movba1(uint32_t ar){ return ar & 0x1u; }
// ivp_movab1 : boolean -> AR — zero-extend the bit          (leaf mov_32_1)
uint32_t movab1(unsigned b){ return (uint32_t)(b & 0x1u); }
```

`mov_1_32` executed live across `{0x0,0x1,0x2,0x3,0xff,0x100,0x101,0xffffffff}` returned exactly
`bit0` each time (`0x2→0, 0x100→0, 0x101→1`). `[HIGH/OBSERVED by execution]`

### 3.4 The slot-class split — transfer kind selects the FLIX slot

Read directly from `opcodedefs[]` (which slots each op is placed in) and corroborated by the
`libcas-core.so` `…_inst_IVP_MOV*_issue` per-slot functions, the family partitions cleanly by transfer
kind:

| slot class | transfer kind | mnemonics | why |
|---|---|---|---|
| **ALU** (`f0_s3_alu` + 14 wide ALU/Mul slots) | intra-`vec`, lane-extract, predicate | `movvv`, `mov2nx8t`, `movav*`, `movavu*`, `movprpr`, `movvpr`, `movab1`, `movpra32`, `movfsv` | one source, ALU-class datapath |
| **Ld** (`f0_s1_ld`) | scalar/immediate→`vec` **broadcast/inject** | `movva*`, `movvint*`, `movvinx16`, `movpint16`/`movqint16`, `movpa16`/`movqa16`, `movvfs`, `movvscf`, `movba1` | synthesises a value into `vec` like a load |
| **Mul** (`f0_s2_mul`) | wide transfer | `movww` (1536b), `movscfv` | needs the wide Mul-slot operand budget |

This is a **recovered architectural fact**, not a convention: `movvv` issues on `F0_S0_LdSt_4`
(verified in cas-core: `F0_F0_S0_LdSt_4_inst_IVP_MOVVV_issue`/`_stall` exist) plus the 14 wide ALU/Mul
slots (its `f0_s3_alu` placement selector is `0x6488d000`) and 5 narrow LdSt slots = its **23**
placements (§6). The same op carries a **different `word0` per slot** — `0x10dc4002` in `f0_s0_ldst`,
`0x6488d000` in `f0_s3_alu` — the per-`(opcode×slot)` selector rule again (§2 GOTCHA); the roster quotes
one representative. The Ld-class broadcasts have a narrower reach (8 placements each) because the
scalar-inject datapath is legal in fewer slots. `[HIGH/OBSERVED]`

---

## 4. Lane value semantics — proven by execution

The `module__xdref_mov*` value leaves in `libfiss-base.so` are the per-element transfer functions,
**callable in-process via ctypes with no license** ([coverage-tally §5](../core/coverage-tally.md)). The
ABI is the standard `void leaf(int ctx, <ins…>, T *out)`. Six leaves were disassembled **and executed
live** this pass; the executed sweeps are bit-exact certificates of the move semantics.

### 4.1 The predicated merge — `mov_8_8_8_t` (mask-gated, executed live)

The predicated vector move `ivp_mov2nx8t` resolves to `module__xdref_mov_8_8_8_t` @ `0x85a930`:

```asm
85a930: 85 c9          test  %ecx,%ecx          ; mask == 0 ?
85a932: 0f 44 f2       cmove %edx,%esi          ;   if so, src(%esi) <- old(%edx)
85a935: 41 89 30       mov   %esi,(%r8)         ; *out = (mask ? src : old)
85a938: c3             ret
```

ABI: `void mov_8_8_8_t(int ctx, int src, int old, int mask, int *out@r8)`. Executed live:

```
movt(src=0xaa, old=0x55, mask=0)  = 0x55     // mask 0 -> KEEP old (lane not written)
movt(src=0xaa, old=0x55, mask=1)  = 0xaa     // mask 1 -> WRITE src
movt(src=0xaa, old=0x55, mask=7)  = 0xaa     // any nonzero mask -> write
movt(src=0x11, old=0x22, mask=0)  = 0x22
movt(src=0x11, old=0x22, mask=99) = 0x11
```

The masked-vs-unmasked lane behaviour is the decisive `t`-form fact: **a lane whose predicate is 0
retains its prior destination value** (read-modify-write of `vec`), a lane whose predicate is nonzero
takes the source. This is the per-lane write-enable / throttle semantics of the `t` suffix
([B03](b03-vec-alu-rest.md)'s convention), applied to a plain move. A reimplementation must implement
`mov2nx8t` as a **merge** (`vd[lane] = mask[lane] ? vs[lane] : vd[lane]`), reading the old `vd` — not as
a conditional whose false lanes are zeroed. `[HIGH/OBSERVED by execution]`

### 4.2 The bridge extracts — width and extension (executed live)

```
mov_1_32  (movba1 / vbool extract, bit0):    0x2->0   0x3->1   0xff->1   0x100->0   0x101->1  0xffffffff->1
mov_8_32  (movav8 / movavu8, zero-ext byte): 0x80->0x80   0x1ff->0xff   0xabcd->0xcd
mov_32s_16(movav16, SIGN-ext half):          0x7fff->0x00007fff   0x8000->0xffff8000   0xffff->0xffffffff
movpra32  (AR->b32_pr, sign-replicate hi):   0x7fffffff->0000_7fffffff   0x80000000->ffff_80000000
mov_512_512(movvv, full copy):               64-byte src == 64-byte dst, byte-exact
```

Five leaves, all bit-exact against their disassembled models. The `mov_8_32`/`mov_32s_16` contrast is
the **zero-vs-sign-extend** boundary already noted in §3.1; the `movpra32` high-word fill is the
sign-predicate materialisation of §3.2. `[HIGH/OBSERVED by execution]`

### 4.3 The wide / state moves — disassembled (the scalar-state bridges)

`movww`'s leaf `mov_1536w_1536w` @ `0x5e93a0` is **twelve** `movdqu` loads + twelve `movups` stores —
a flat 1536-bit `wvec`→`wvec` copy (no narrowing; the narrowing readout is B10's `pack`). The
control-flag pack `movscfv` (leaf `movvscf_32_1_1_1_1_1_2_1_1_1_1_1`) is a **shift-OR network**: twelve
sub-fields (eleven 1-bit + one 2-bit) are positioned by `shl $0x{e,d,c,b,a,8,6,5,4,3,2}` and OR-ed into a
single 32-bit CSR word — i.e. the scalar-control-flag register is *assembled* from twelve `vec`-resident
flag lanes. The inverse `movvscf` unpacks it. These four (`movww`/`movvfs`/`movfsv`/`movvscf`/`movscfv`)
were **not** driven live (the multi-field state ABI takes stack-resident args and segfaulted a naive
ctypes call — flagged, see §7); their semantics are read from the disassembled shift-OR/copy bodies.
`[HIGH/OBSERVED]` on the disassembled field map; `[MED/INFERRED]` on the exact per-flag CSR bit
assignment.

> **QUIRK — `movvv` is `movdqu`-based on the host model, but the device op is a single-source ALU/LdSt
> move, not four loads.** The `mov_512_512` leaf uses four 128-bit SSE copies *because the value model
> runs on x86*; the **device** `ivp_movvv` is one FLIX-issued register-to-register transfer (8-byte
> bundle, the op in one slot). Do not infer a 4-µop device cost from the host leaf's four `movdqu` — the
> leaf is the *value* oracle (what bytes land), the cas-core `…_issue` function is the *timing* oracle.
> `[HIGH/OBSERVED]`

---

## 5. Device-assembler oracle — byte-exact round-trip

The end-to-end check: feed the **device-native** `xtensa-elf-as` (`XTENSA_SYSTEM=…/ncore2gp/config`,
`XTENSA_CORE=ncore2gp`) each `mov` with its operands, then disassemble back. **19 of the 27** assemble
`rc=0` and round-trip to the same lowercase mnemonic with the correct register-file operands. Verbatim
bytes (LE, the assembler's packed-bundle layout):

| mnemonic | operands | bundle bytes | disasm bundle |
|---|---|---|---|
| `IVP_MOVVV`     | `v3, v1`     | `029c62409c04c14f` | `{ ivp_movvv v3, v1; nop }` |
| `IVP_MOVPRPR`   | `pr3, pr1`   | `32514c086681452f` | `{ nop; nop; nop; ivp_movprpr pr3, pr1 }` |
| `IVP_MOVWW`     | `wv3, wv1`   | `02a56f008800352f` | `{ nop; nop; ivp_movww wv3, wv1 }` |
| `IVP_MOVAV8`    | `a3, v1`     | `32514c086481452f` | `ivp_movav8 a3, v1` |
| `IVP_MOVAV16`   | `a3, v1`     | `32514c086401452f` | `ivp_movav16 a3, v1` |
| `IVP_MOVAV32`   | `a3, v1`     | `32514c086441452f` | `ivp_movav32 a3, v1` |
| `IVP_MOVAVU8`   | `a3, v1`     | `32514c086601452f` | `ivp_movavu8 a3, v1` |
| `IVP_MOVVA8`    | `v3, a4`     | `02a462213c48c52f` | `{ nop; ivp_movva8 v3, a4 }` |
| `IVP_MOVVA16`   | `v3, a4`     | `02a462213c46c52f` | `ivp_movva16 v3, a4` |
| `IVP_MOVVA32`   | `v3, a4`     | `02a462213c47c52f` | `ivp_movva32 v3, a4` |
| `IVP_MOVVPR`    | `v3, pr1`    | `32505918c0c1452f` | `ivp_movvpr v3, pr1` |
| `IVP_MOVPRA32`  | `pr3, a1`    | `32514c087002452f` | `ivp_movpra32 pr3, a1` |
| `IVP_MOVVINT8`  | `v3, 5`      | `029462203c85c52f` | `{ nop; ivp_movvint8 v3, 5 }` |
| `IVP_MOVVINX16` | `v3, 5`      | `02a462283c05c52f` | `ivp_movvinx16 v3, 5` |
| `IVP_MOVFSV`    | `v3`         | `32514c083003452f` | `{ nop; nop; nop; ivp_movfsv v3 }` |
| `IVP_MOVVFS`    | `v3`         | `02a462293c28c52f` | `ivp_movvfs v3` |
| `IVP_MOVSCFV`   | `v3`         | `0007193200c2ab8090080fa09504452f` | `ivp_movscfv v3` |
| `IVP_MOVVSCF`   | `v3`         | `0007193208c2ab00341823a085ad452f` | `ivp_movvscf v3` |
| `IVP_MOVGATHERD`◇| `gr3, v1`   | `029c60069c04107f` | `{ ivp_movgatherd gr3, v1; nop }` |

Two structural facts the oracle pins:

* **The bridge operand files are exactly the bridge matrix's.** `movav* a3, v1` (AR dest, vec src),
  `movva* v3, a4` (vec dest, AR src), `movvpr v3, pr1` (vec dest, `pr` src), `movpra32 pr3, a1` (`pr`
  dest, AR src), `movww wv3, wv1` (`wv` both), `movgatherd gr3, v1` (`gr`/`gvr` dest). The register
  short-names (`a`/`v`/`pr`/`wv`/`gr`) round-trip exactly as [register-files](../core/register-files.md)
  specifies. `[HIGH/OBSERVED]`
* **The slot-class split shows in the bundle shape.** ALU-class moves land in the trailing slot of a
  4-position bundle (`…452f`); Ld-class broadcasts land in a 2-position bundle (`…c52f`); `movscfv`
  (Mul) is a 16-byte bundle. This matches §3.4. `[HIGH/OBSERVED]`

`◇` = `movgatherd` round-trips here only to **confirm it is real and `gr`-typed**; it is **owned by
[B19](b19-scatter-gather.md)**, not counted in B09's 27/246. The eight not shown (`mov2nx8t`, `movab1`,
`movba1`, `movpa16`, `movqa16`, `movpint16`, `movqint16` — predicate/pack-half operand classes the bare
oracle probe didn't spell, plus the `movvscf`/`movscfv` pair which *did* round-trip) are documented from
the encode thunk + value leaf instead; see §7. The device byte order is the assembler's packed-bundle
layout and is **not** byte-identical to the §2 slot-normalized selector imm (a different representation);
they agree **structurally** — the placement exists, the mnemonic and register files round-trip — which is
the property the oracle certifies. `[HIGH/OBSERVED]`

---

## 6. Batch coverage tally — 27 mnemonics / 246 placements

Re-counted this pass with `nm libisa-core.so | rg -c 'Opcode_ivp_<mn>_Slot_…_encode'` per mnemonic
(never the decompile — [coverage-tally §0 GOTCHA](../core/coverage-tally.md)). Every one of the 27
grounds to ≥ 5 placements; **none ungrounded**.

| sub-family | mnemonics | placements | notes |
|---|--:|--:|---|
| intra-file (`movvv`/`movprpr`/`movww`) | 3 | 23+9+8 = 40 | `movvv` is the widest (23 slots, the LdSt+ALU+Mul reach) |
| AR↔vec extract (`movav*`/`movavu*`) | 5 | 9×5 = 45 | all ALU-class, 9 slots each |
| AR→vec broadcast (`movva*`) | 3 | 8×3 = 24 | all Ld-class, 8 slots each |
| predicate bridges (`movvpr`/`movpra32`/`movab1`/`movba1`) | 4 | 9+15+9+8 = 41 | `movpra32`=15 (LdSt reach) |
| predicated merge (`mov2nx8t`) | 1 | 13 | the `t`-form drops slots vs base, like B03 |
| immediate inject (`movvint8/16`/`movvinx16`/`movpint16`/`movqint16`) | 5 | 8×5 = 40 | all Ld-class |
| pack-half (`movpa16`/`movqa16`) | 2 | 8×2 = 16 | Ld-class |
| scalar-state (`movvfs`/`movfsv`/`movvscf`/`movscfv`) | 4 | 8+9+5+5 = 27 | `movvscf`/`movscfv`=5 (cold path) |
| **TOTAL** | **27** | **246** | |

Arithmetic: `40+45+24+41+13+40+16+27 = 246`. ✓ These 246 are a strict subset of the **12 569
certified-perfect placements**; `movgatherd`'s 8 placements are counted in [B19](b19-scatter-gather.md),
the `wvec`-narrowing `pack*` in [B10](b10-wvec-pack.md), and the `rep`/`splat` broadcasts in
[B16](b16-vec-rep.md) — no double-count. The 27 mnemonics roll into the **1065-op vector axis**; the
batch adds **0** to the scalar axis (every row is `package == xt_ivp32`, `ivp_`-prefixed). `[HIGH/OBSERVED]`

> **NOTE — the scalar conditional moves `movt`/`movf`/`moveqz`/`movnez`/`movgez`/`movltz` are NOT in
> this batch.** `libfiss-base.so` carries `module__xdref_movt_64f_64f`, `moveqz_64f_64f`, … — these are
> the **base-Xtensa / scalar-FP** conditional moves (FP64 register, `package == xt_ivpn_scalarfp`,
> **no** `ivp_` prefix), owned by [B24](b24-composite.md) (scalar-FP). They look like our predicated
> `mov2nx8t` but operate on scalar FP registers, not `vec`. A reader who greps `module__xdref_mov` will
> see them adjacent; they belong to B24, cited here so the boundary is explicit. `[HIGH/OBSERVED]`

---

## 7. Adversarial self-verification — the five strongest claims

Each re-challenged against the binary this pass; failures fixed.

1. **"27 mnemonics / 246 placements; `movgatherd` is B19's, not B09's."** Re-run:
   `nm libisa-core.so | rg -o 'Opcode_(ivp_mov[a-z0-9_]*)_Slot' | sort -u` = **28** `mov*`; the
   classifier (`scatter|gather` before `mov`) removes `movgatherd` (confirmed: it is the only `mov*` in
   the scatter/gather glob, sitting beside `gatherd2nx8_h`/`gatherdnx16`), leaving **27**; the
   per-mnemonic placement sum = **246** (`movgatherd`'s 8 excluded). The §6 table sums `246`. ✓
   `[HIGH/OBSERVED]`
2. **"The predicated `mov2nx8t` is a *merge* (mask-0 lanes keep the old destination), not a zero-fill."**
   Re-challenged by executing `mov_8_8_8_t` live: `movt(0xaa, 0x55, mask=0) = 0x55` (the **old**, not
   `0`), `movt(0xaa, 0x55, mask=1) = 0xaa`. The `cmove` against the *old destination* operand proves it
   is RMW merge. A zero-fill implementation would return `0x00` on mask=0 and would be wrong. ✓
   `[HIGH/OBSERVED by execution]`
3. **"`movav16` sign-extends, `movavu16` zero-extends — distinct opcodes."** Executed
   `mov_32s_16(0x8000) = 0xffff8000` (sign) vs the `movavu16` zero-extend leaf `mov_32u_16` masking to
   `0x00008000`; the two are `opc# 522`/`523` with distinct `iclass` pointers (read from `opcodes[]`).
   Initially I modeled one `movav16` with a sign flag — the binary shows two opcodes. Fixed. ✓
   `[HIGH/OBSERVED by execution]`
4. **"`movpra32` materialises a 64-bit predicate by sign-replicating the AR high word."** Executed
   `movpra32(0x80000000) → lo=0x80000000, hi=0xffffffff` and `movpra32(0x7fffffff) → hi=0x00000000`.
   The `sar $31` in the disassembled body is the sign-broadcast, confirmed bit-exact at the sign
   boundary. ✓ `[HIGH/OBSERVED by execution]`
5. **"The family splits across three slot classes by transfer kind (ALU/Ld/Mul)."** Re-challenged by
   reading `opcodedefs[]` slot membership per op **and** the cas-core `…_inst_IVP_MOVVV_issue` set:
   `movvv` issues on `F0_S0_LdSt` + 14 wide ALU/Mul slots (23 total); `movva*`/`movvint*` only on Ld
   slots (8); `movww`/`movscfv` on Mul. The device oracle's bundle shapes (`…452f` ALU-trailing vs
   `…c52f` Ld vs 16-byte `movscfv`) corroborate. ✓ `[HIGH/OBSERVED]`

**Ungrounded / flagged items** (honest residue): (a) the **scalar-state field maps** of
`movvfs`/`movfsv`/`movvscf`/`movscfv` are `[HIGH/OBSERVED]` as *shift-OR networks* in disasm and their
**directions** are OBSERVED from the operand args list (`movvfs`/`movvscf` read FP-state/CSR → write
`vec`; `movfsv`/`movscfv` read `vec` → write the FP-state/CSR flags `Invalid/Inexact/Underflow/Overflow/
DivZero`), but the exact per-flag CSR bit assignment is `[MED/INFERRED]` (the multi-field stack-arg ABI
segfaulted a naive ctypes call, so these four were **not** driven live — disasm-only). (b) The `q`-half
pack selector cells (`movqint16`, `movqa16`) are the `+1` siblings of the `p`-forms (`[MED/INFERRED]`
imm; the `p`-form imm and the step are `[HIGH/OBSERVED]`). (c) The `movvint*`/`movvinx16` immediate
**bit-field width** in the slot word is OBSERVED to exist (operand `i_IMM_movint`/`immmovvi`) but its
exact range is `[MED/INFERRED]`. None is a missing decode or a missing transfer semantics — every row
has a resolved encode thunk, an args-confirmed src→dst, a value leaf, and (for 19 of 27) a device
round-trip. (The earlier `[MED/INFERRED]` on the `movab1`/`movba1` files and the `movvpr` direction was
**resolved to OBSERVED** by reading `Iclass_IVP_<X>_args`: `movab1` `art(AR)←vbr(BR)`, `movba1`
`vbt(BR)←ars(AR)`, `movvpr` `vt(vec)←prr(b32_pr)`.)

---

## 8. Function & symbol map

`libisa-core.so` unless noted. `.text`/`.rodata`: VMA == file. `.data.rel.ro`/`.data`: file =
VMA − `0x200000` (re-confirmed `readelf -SW` this pass — **not** libtpu's `0x400000`).

| Symbol / table | Addr | Role |
|---|---|---|
| `opcodes` | `0x6ce6c0` (file `0x4ce6c0`) | 1534 × stride 72; all 27 `mov` rows resolved (`opc#`/`iclass`/pkg) |
| `Opcode_ivp_movvv_Slot_f0_s0_ldst_encode` | `.text` | `movl $0x10dc4002` (the `vec→vec` selector) |
| `Opcode_ivp_movva8_Slot_f0_s1_ld_encode` | `.text` | `movl $0x00602008` (Ld-class broadcast) |
| `Opcode_ivp_movav8_Slot_f0_s3_alu_encode` | `.text` | `movl $0x82b9820a` (ALU-class extract) |
| `Field_fld_ivp_sem_unpack_wvec_mov_vr_Slot_f0_s2_mul_get` | `0x331680` | `(slotword<<0x1a)>>0x1b` = `wvec` reg field (`movww`) |
| `module__xdref_mov_512_512` | `0x857fd0` (`libfiss-base.so`) | `movvv` full 512-bit copy (executed live) |
| `module__xdref_mov_8_8_8_t` | `0x85a930` (`libfiss-base.so`) | `mov2nx8t` predicated merge (executed live) |
| `module__xdref_mov_8_32` / `mov_32s_16` | `0x5e9470` / `0x870e70` (`libfiss-base.so`) | AR↔vec extract zero/sign-ext (executed live) |
| `module__xdref_movpra32_64_32` | `0x82d9b0` (`libfiss-base.so`) | AR→b32_pr sign-replicate (executed live) |
| `module__xdref_mov_1_32` / `mov_32_1` | `0x5b8000` / `0x5b8010` (`libfiss-base.so`) | boolean extract / insert (executed live) |
| `module__xdref_mov_1536w_1536w` | `0x5e93a0` (`libfiss-base.so`) | `movww` 1536-bit copy (disasm) |
| `module__xdref_movvscf_32_1_1_1_1_1_2_1_1_1_1_1` | `0x5b76e0` (`libfiss-base.so`) | CSR pack shift-OR network (disasm) |
| `F0_F0_S0_LdSt_4_inst_IVP_MOVVV_issue` | `0x11896c0` (`libcas-core.so`) | proves `movvv` rides the LdSt datapath |
| `F0_F0_S0_LdSt_4_IVP_MOVGATHERD_inst_stage0..11` | `libcas-core.so` | 12-stage gather pipeline (B19 op) |
| `xtensa-elf-objdump` | `tools/XtensaTools/bin/` | device round-trip oracle (`XTENSA_CORE=ncore2gp`) |

---

## 9. Cross-references

* [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the schema this page
  follows, the classifier that sends `movgatherd` to B19, and the 1534/12569 roll-up this batch's
  27/246 closes into.
* [The Eight Register Files](../core/register-files.md) — the AR/BR/vec/vbool/valign/wvec/b32_pr/gvr
  files this batch bridges, their short-names (`a`/`v`/`vb`/`pr`/`wv`/`gr`), and the `gvr`/CSR view the
  `scf` moves plumb.
* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid, the per-`(opcode×slot)`
  selector model, and the ALU/Ld/Mul slot classes the §3.4 split rides.
* [ISA Coverage & the 1534/12569 Tally](../core/coverage-tally.md) — the certified denominator and the
  `nm`-not-decompile counting rule.
* [B10 — `wvec` pack (wide→narrow readout)](b10-wvec-pack.md) — the *narrowing* `wvec`→`vec` readout we
  exclude (`movww` is the non-narrowing copy).
* [B16 — Vector replicate / extract / inject](b16-vec-rep.md) — the `rep`/`splat` broadcasts adjacent to
  our `movva*`/`movvint*` injects, and the pack-half neighbourhood of `movpa16`/`movqa16`.
* [B19 — SuperGather scatter / gather](b19-scatter-gather.md) — owns `ivp_movgatherd` (gather-descriptor).
* [B01](b01-vec-alu-int.md) / [B03](b03-vec-alu-rest.md) — the compares that *produce* `vbool` predicates
  (we own only the explicit `mov` predicate bridges), and the `t`-throttle convention `mov2nx8t` shares.
* [B24 — Histogram / squeeze / QLI + scalar-FP](b24-composite.md) — owns the scalar conditional moves
  (`movt`/`moveqz`/…`_64f`) that look like our predicated move but operate on scalar FP registers.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags and the proven-by-
  execution value lane.

---

*Provenance: encoding (`Opcode_*`/`Field_*` thunks, `opc#`/`iclass`/package from a direct `opcodes[]`
walk), placement counts, and the slot-class split are `[HIGH/OBSERVED]` — disassembled / `nm`-counted /
table-parsed in-checkout from `libisa-core.so` (`ncore2gp/config`, not stripped, `.data` delta
`0x200000`). Move value semantics are `[HIGH/OBSERVED by execution]` for six leaves (`mov_512_512`,
`mov_8_8_8_t`, `mov_8_32`, `mov_32s_16`, `mov_1_32`, `movpra32_64_32` — called live via ctypes from the
license-free `libfiss-base.so`) and `[HIGH/OBSERVED]`-by-disasm for the four scalar-state bridges. The
slot/issue model is `[HIGH/OBSERVED]` from `libcas-core.so` `…_inst_IVP_MOV*_issue` functions. The byte
round-trip is `[HIGH/OBSERVED]` from the device-native `xtensa-elf-as`/`objdump` (`XTENSA_CORE=ncore2gp`,
19 of 27 round-tripped). All facts read as derived from shipped-artifact static analysis and in-process
execution of license-free leaves (lawful interoperability RE).*
