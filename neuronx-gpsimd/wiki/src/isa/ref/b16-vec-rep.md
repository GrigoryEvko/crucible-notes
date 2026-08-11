# ISA Batch 16 — Vector Replicate / Extract (vec_rep)

This batch is the **lane-broadcast / lane-extract layer** of the Vision-Q7 *Cairo* (`ncore2gp`)
vector ISA: the family that moves data **between the lane axis and a single scalar/predicate value**.
It owns three regular patterns — **replicate** (`ivp_rep*`: select one lane, splat it across all
lanes of a `vec`), **inject** (`ivp_injbi*`: write one boolean into one lane of a `vec`), and
**extract** (`ivp_extr*`/`ivp_extrpr*`/`ivp_extract*`: pull one lane out to an `AR` scalar, a
`b32_pr` predicate, or a `vbool` boolean). These are the *index-by-a-single-scalar* shape-changers
that sit between the plain reg↔reg moves of [B09](b09-vec-mov.md) and the full per-lane permutation
networks (`sel`/`shfl`/`dsel`) of [B21](b21-select-shuffle.md). It owns **21 mnemonics / 285 of the
12 569 shipped placements** ([the coverage tally's certified denominator](../core/coverage-tally.md)),
all in package `xt_ivp32`, all on the vector (`ivp_`) axis; the
[partition classifier](template-and-partition.md) routes these here by the `rep|splat|bcast|inj`
verb match plus the regular-extract scope this page is chartered with (§0).

Everything below is grounded in the shipped binaries: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks read byte-for-byte, the `opcodes[]` table
walked directly for `opc#`/iclass/package), the **value semantics** by *executing* the matching
`module__xdref_*` leaves in `libfiss-base.so` live in-process (license-free), the **slot/issue model**
from the `opcode__/regload__/writeback__` per-op bodies in `libfiss-base.so`, and a byte-exact
**encode/decode oracle** from the device-native `xtensa-elf-as`/`xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`). The page default is `[HIGH/OBSERVED]` (read-from-byte /
proven-by-execution); claims that depart from that default carry an explicit tag per
[the Confidence & Walls model](../../reference/confidence-model.md) — `[MED/INFERRED]` = reasoned
over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic.** `libisa-core.so` (sha256
> `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`, 9 690 712 B, ET_DYN x86-64, not
> stripped). Per `readelf -SW`: `.text` (`0x312c10`) and `.rodata` (`0x3b6e40`) are
> **VMA == file-offset**; `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) carries the per-binary
> delta **`0x200000`** — **not** libtpu's `0x400000`. The `opcodes[]` table is at VMA `0x6ce6c0` (file
> `0x4ce6c0`), stride **72**. Encode thunks live in `.text` (VMA == file). Both libraries are in
> `extracted/` (gitignored; reach with `fd --no-ignore` or an absolute path).

---

## 0. Scope boundary — what this batch owns, and the four walls

The `rep`/`inj`/`extr` family is a magnet for mis-assignment because its mnemonics *spell* like moves
(`mov`-adjacent), shuffles (`extr`-adjacent), and reductions (`sqz`-adjacent). Four boundaries are
enforced so the 30 batches never double-count. **The unifying property this batch owns is the
*regular, single-scalar-indexed* lane↔scalar transform**: exactly one lane index (immediate, AR, or a
single vector lane) selects one source/destination lane; there is **no per-output-lane index vector**.

* **Plain reg↔reg move / regfile bridge (`ivp_movvv`, `ivp_movva*`, `ivp_movvint*`) → [B09](b09-vec-mov.md), NOT here.**
  B09's `movva*` writes an `AR` scalar into **one** `vec` lane (an *inject of a scalar*, not a
  broadcast-from-a-lane); B09's `movvint*` broadcasts an *immediate constant* to all lanes. Our `rep*`
  broadcasts a **runtime lane already in a `vec` register** to all lanes (a lane→lane fan-out), and our
  `injbi*` injects a **`vbool` boolean** (not an AR scalar) into one lane. The classifier gives
  `rep|inj` priority by verb; the leaf widths prove the data shape (§4). The `movpa16`/`movqa16`
  pack-half selects ship spelled `mov*` and stay in B09 (cited there as adjacency).
* **Arbitrary per-lane permutation / selection (`ivp_sel*`, `ivp_shfl*`, `ivp_dsel*`) → [B21](b21-select-shuffle.md), NOT here.**
  A `shfl`/`sel` reads a **per-lane index vector** (a different source lane for *every* output lane) —
  a full crossbar. Our `rep` (one source lane → all outputs) and `extr` (one source lane → one scalar)
  are the **degenerate, regular** cases of that crossbar and are owned here. The
  [partition classifier](template-and-partition.md) §4.2 lists `extr` under the B21 glob
  (`sel|shfl|dsel|extr|compr|zip`); **this page reclassifies the `extr*` regular single-lane extracts
  to B16** because they are the structural inverse of `rep` (same single-scalar index, opposite
  direction) and share the `vec_rep` semantic neighbourhood (the
  [register-files §6.3/§6.4 bridge taxonomy](../core/register-files.md) groups `rep`/`extr` as the two
  `AR↔vec` lane bridges). B21 owns only the *permutation* `extr` variants if any exist with a per-lane
  index; the twelve `extr*` here are all single-scalar-indexed (verified §2, §4). The boundary is
  stated in both directions so the rolls-up close (§6).
* **Cross-lane reduce-to-scalar (`ivp_sqzn`, `ivp_radd*`, `rmax*`) → [B08](b08-reduce.md) / [B24](b24-composite.md), NOT here.**
  A *squeeze* (`IVP_SQZN`, [register-files §6.4](../core/register-files.md)) compacts the **active**
  lanes selected by a `vbool` predicate into a packed prefix and emits an `AR` *count* — it reads all
  32 lanes and a mask. Our `extr` reads **one** lane at a fixed index. `sqz`/`qli`/`hist` are
  [B24](b24-composite.md)'s; the reductions are [B08](b08-reduce.md)'s. Both are excluded.
* **`wvec` wide-accumulator readout (`ivp_pack*`, `ivp_unpack*`) → [B10](b10-wvec-pack.md) / [B22](b22-unpack-wvec-mov.md), NOT here.**
  Narrowing a 1536-bit `wvec` accumulator down to a `vec` is a `pack` (B10); reading it without
  narrowing is an `unpack` (B22). Our `extr`/`rep` never touch `wvec`. Excluded.

> **GOTCHA — `extr` is in this batch despite the template classifier's B21 glob; `splat`/`bcast`/`dup`
> are *not* real mnemonics.** Two facts a reimplementer must internalise before pairing a mnemonic.
> (a) The `nm` roster contains **no** `splat*`, `bcast*`, or `dup*` opcode — the broadcast verb the
> ISA actually ships is **`rep`** (`ivp_repnx16` and siblings); "splat" is conceptual prose, not a
> symbol (`nm libisa-core.so | rg 'splat|bcast|dup'` over the opcode glob = ∅). (b) The
> `extr*` family is OWNED HERE, overriding the §4.2 first-match glob, on the explicit scope charter
> above; B21's stub and roll-up are written to *not* re-count the twelve `extr*` mnemonics. Sum
> arithmetic (§6) is the proof there is no double-count. `[HIGH/OBSERVED]`

---

## 1. Batch key facts

| Fact | Value | Binary source |
|---|---|---|
| Axis / package | vector (`ivp_`) / **`xt_ivp32`** for all 21 | `opcodes[].package` @ `+0x08`, parsed for all 21 rows |
| Mnemonics this batch | **21** | §2; `nm libisa-core.so` distinct `Opcode_ivp_(rep\|inj\|extr\|dextr\|extract)*` |
| Placements this batch | **285** | per-mnemonic `nm \| rg -c` sum (§6): `102+54+45+8+60+16` |
| Value leaves this batch | **20** distinct `module__xdref_*` (rep/inj/extr) | `nm libfiss-base.so \| rg module__xdref_'(rep\|replo\|inj\|extr\|dextr)'` (§4) |
| Source / dest files | `vec` (idx2), `AR` (idx0), `b32_pr` (idx6), `vbool` (idx3) | §3 bridge matrix; [register-files](../core/register-files.md) |
| Dominant slot class | **S3_ALU** (every mnemonic), + S0_LdSt / S2_Mul reach for `rep`/`inj`/`extrpr`; **S1_Ld** for `extract*` | `opcodedefs[]` slot tokens (§2.5) |
| Encode-thunk ABI | `C7 07 imm32 [C7 47 04 0] C3` — `imm32` = the `(opcode×slot)` selector, `word1==0` (S3_ALU) | [flix-encoding §6.1](../core/flix-encoding.md) |
| Lane-index source | **immediate** (`rep`/`inj`/`extr`/`extrpr`), **AR register** (`extrvr`), **vec lane** (`extrprvr`) | device oracle (§5) |
| `t`-form predication | **shared base value leaf** + a `vbool`-masked merge `writeback__*t` (RMW) | `writeback__ivp_repnx16t` @`0x4bfa30` |
| Oracle | `xtensa-elf-as`/`objdump`, `XTENSA_CORE=ncore2gp` | **13 of 21 round-trip byte-exact** (§5) |

> **The batch is one lane-mux with three index sources and four ports.** Every op in this batch is a
> *route through a single lane-selection network*: a lane index (immediate / AR / vec) drives a mux
> that either fans **one input lane out to all output lanes** (`rep`), **picks one input lane into one
> scalar/predicate** (`extr`), or **drops one boolean into one input lane** (`inj`). A reimplementation
> builds one parameterised select-and-route unit and decodes `(direction, index-source, element-width,
> dest-file, mask-enable)` from the opcode. The *value* is a pure permutation of bits — there is no
> arithmetic — so the value oracle is a byte-exact identity on the selected lane (§4). `[HIGH/OBSERVED]`

---

## 2. Batch roster — 21 replicate / extract / inject opcodes

Columns: `mnemonic` · `lanes×width` (the dtype shape — `nx16`=32×16b over the 512-bit `vec`,
`2nx8`=64×8b, `n_2x32`=16×32b, `n_4x64`=8×64b) · representative **`FLIX slot · opcode-sel imm`** (the
`Opcode_<mnem>_Slot_<slot>_encode` thunk's `movl $imm`, disassembled) · `opc#` (the
`opcodes[]` row index, read by walking the table at file `0x4ce6c0`, stride 72) · `src→dst` lane/scalar
bridge · device `bytes` of the bundle · one-line semantics. Rows of §2.1–§2.3 are `[HIGH/OBSERVED]`,
the non-`t` forms additionally **proven by execution**; §2.4 keeps its per-row tag because its
evidence varies. The selector imm is for the
**named representative slot only** — the selector is per-`(opcode×slot)`
([flix-encoding §6.2 two-tier rule](../core/flix-encoding.md); the B01 GOTCHA holds here). `word1==0`
for every S3_ALU thunk (the upper lane carries no selector bits); `extract*` is an S1_Ld single-word
template (no `word1`). All 21 are `package == xt_ivp32`, `ivp_`-prefixed.

### 2.1 Replicate — lane-broadcast (`rep*`, splat one lane to all lanes)

The `rep<shape>` op reads lane `k` (an immediate index) of source `vec` `vs` and writes that value into
**every** lane of dest `vec` `vd`. The `t` suffix adds a `vbool` mask operand: only lanes whose mask
bit is set are written (the rest keep their old value — a read-modify-write merge, §4.3).

| mnemonic | lanes×w | rep. slot · sel imm | opc# | src→dst | bytes | semantics |
|---|---|---|---|---|---|---|
| `ivp_repnx16`   | 32×16 | `f0_s3_alu` `0x86900020` | 364 | `vec[k]`→`vec`(all) | 8/2 | `vd[*] = vs[k]`, 16-bit lanes, k=imm |
| `ivp_rep2nx8`   | 64×8  | `f0_s3_alu` `0x86900000` | 366 | `vec[k]`→`vec`(all) | 8/2 | `vd[*] = vs[k]`, 8-bit lanes |
| `ivp_repn_2x32` | 16×32 | `f0_s3_alu` `0x81000010` | 368 | `vec[k]`→`vec`(all) | 8/2 | `vd[*] = vs[k]`, 32-bit lanes |
| `ivp_repnx16t`  | 32×16 | `f0_s3_alu` `0x80300010` | 980 | `vec[k]`→`vec`(masked) | 8/2 | masked broadcast, `vbool` write-enable |
| `ivp_rep2nx8t`  | 64×8  | `f0_s3_alu` `0x80300000` | 979 | `vec[k]`→`vec`(masked) | 8/2 | masked broadcast, 8-bit |
| `ivp_repn_2x32t`| 16×32 | `f0_s3_alu` `0x81000000` | 981 | `vec[k]`→`vec`(masked) | 8/2 | masked broadcast, 32-bit |

### 2.2 Inject — boolean-to-lane (`injbi*`, drop one `vbool` bit into one `vec` lane)

`injbi<shape>` takes a destination `vec` and a `vbool` boolean source, and writes the boolean's value
into lane `k` (immediate) of the dest while leaving all other lanes unchanged: a 1-lane masked merge
keyed by an immediate position. The leaf builds `mask = 1<<k`, sign-broadcasts the boolean to all-ones
or all-zero, and does `out = (vec & ~mask) | (bit & mask)` (§4.4).

| mnemonic | lanes×w | rep. slot · sel imm | opc# | src→dst | bytes | semantics |
|---|---|---|---|---|---|---|
| `ivp_injbinx16`  | 32×16 | `f0_s3_alu` `0x869000e1` | 1068 | `vbool`→`vec[k]` | 8/2 | inject boolean into 16-bit lane k=imm |
| `ivp_injbi2nx8`  | 64×8  | `f0_s3_alu` `0x869040e1` | 1066 | `vbool`→`vec[k]` | 8/2 | inject boolean into 8-bit lane |
| `ivp_injbin_2x32`| 16×32 | `f0_s3_alu` `0x86900040` | 1070 | `vbool`→`vec[k]` | 8/2 | inject boolean into 32-bit lane |

### 2.3 Extract → AR scalar (`extr*`, pull lane[k] out to a 32-bit AR register)

`extr<shape>` reads lane `k` (immediate) of source `vec` and writes it to an `AR` scalar; the `nx16`
form **sign-extends** the 16-bit lane to 32 bits. `extrvrn_2x32` is the variable-index sibling: the
lane index comes from an **`AR` register** (`a2`) rather than an immediate, and is interpreted
byte-granularly (§4.5 quirk).

| mnemonic | lanes×w | rep. slot · sel imm | opc# | src→dst | bytes | semantics |
|---|---|---|---|---|---|---|
| `ivp_extrnx16`    | 32×16→32 | `f0_s3_alu` `0x80e68200` | 524 | `vec[k]`→`AR` | 8/2 | AR = **sign-ext**(vs[k] as int16), k=imm |
| `ivp_extr2nx8`    | 64×8→32  | `f0_s3_alu` `0x86a60000` | 1218 | `vec[k]`→`AR` | 8/2 | AR = vs[k] (8-bit lane) |
| `ivp_extrn_2x32`  | 16×32→32 | `f0_s3_alu` `0x80878304` | 1212 | `vec[k]`→`AR` | 8/2 | AR = vs[k] (32-bit lane), k=imm |
| `ivp_extrvrn_2x32`| 16×32→32 | `f0_s3_alu` `0x80ee8300` | 1219 | `vec[byte k]`→`AR` | 8/2 | AR = vs[k], **k from AR reg** (byte-granular, bound 0x40) |

### 2.4 Extract → predicate / boolean (`extrpr*`, `dextrpr`, `extract*`)

The predicate-destination extracts pull lane `k` out to a 64-bit `b32_pr` (high word zeroed) or, for
`extract*`, a `vbool` boolean. `dextrprn_2x32` is the "dual" form (a pair of lanes → a predicate pair).
`extrprvrn_2x32` indexes from a vec lane. `extract{bl,bh}` extract a boolean lane low/high into a
2-bit `vbool` field and ride the **S1_Ld** slot (single-word thunk), unlike the rest of the family.

| mnemonic | lanes×w | rep. slot · sel imm | opc# | src→dst | bytes | semantics | conf |
|---|---|---|---|---|---|---|---|
| `ivp_extrprnx16`    | 32×16→64 | `f0_s3_alu` `0x80ee8000` | 1373 | `vec[k]`→`b32_pr` | 8/2 | pr = vs[k] (16-bit), hi word=0, k=imm | `[HIGH/OBSERVED]` |
| `ivp_extrpr2nx8`    | 64×8→64  | `f0_s3_alu` `0x86bf8000` | 1372 | `vec[k]`→`b32_pr` | 8/2 | pr = vs[k] (8-bit), hi=0 | `[HIGH/OBSERVED]` |
| `ivp_extrprn_2x32`  | 16×32→64 | `f0_s3_alu` `0x80878306` | 1374 | `vec[k]`→`b32_pr` | 8/2 | pr = vs[k] (32-bit), hi=0 | `[HIGH/OBSERVED by exec]` |
| `ivp_extrpr64n_4x64`| 8×64→64  | `f0_s3_alu` `0x868e0100` | 1413 | `vec[k]`→`b32_pr` | 8/2 | pr = vs[k] (64-bit lane), k=imm | `[HIGH/OBSERVED]` |
| `ivp_extrprvrn_2x32`| 16×32→64 | `f0_s3_alu` `0x80ee8200` | 1378 | `vec[vk]`→`b32_pr` | 8/2 | pr = vs[k], **k from vec lane** | `[HIGH/OBSERVED]` |
| `ivp_dextrprn_2x32` | 16×32→64 | `f0_s3_alu` `0x67000000` | 1379 | `vec[k0],vec[k1]`→`b32_pr` | 8/2 | dual-lane extract → predicate pair | `[HIGH/OBSERVED]` opc; `[MED]` pairing |
| `ivp_extractbl`     | bool→2b  | `f0_s1_ld` `0x004a0ee0`  | 514  | `vbool`→`vbool`(2b) | 8/2 | extract boolean **low** → 2-bit field | `[HIGH/OBSERVED by exec]` |
| `ivp_extractbh`     | bool→2b  | `f0_s1_ld` `0x004a0de0`  | 515  | `vbool`→`vbool`(2b) | 8/2 | extract boolean **high** → 2-bit field | `[HIGH/OBSERVED by exec]` |

### 2.5 The FLIX slot spread — four placement families

Read from the `opcodedefs[]` slot tokens (`nm | rg -o 'Opcode_ivp_<mn>_Slot_<slot>_encode'`), the 21
mnemonics fall into exactly four placement families, distinguished by **which FLIX slots accept them**:

| family | mnemonics | #slots | slot set | why |
|---|---|--:|---|---|
| `rep*` (6) | `repnx16(t)`, `rep2nx8(t)`, `repn_2x32(t)` | 17 | S3_ALU (all 9 formats) + S0_LdSt (f0/f3/f6/f7) + S2_Mul (f1/f2/f7) | broadest reach: a lane fan-out is legal on ALU, LdSt, and Mul datapaths |
| `injbi*` (3) | `injbinx16`, `injbi2nx8`, `injbin_2x32` | 18 | the `rep*` 17 **+ `n1_s2_mul`** | one extra narrow Mul slot |
| `extr*` ALU (5) | `extrnx16`, `extr2nx8`, `extrn_2x32`, `extrprvrn_2x32`, `dextrprn_2x32` | 9 | **S3_ALU only**, all 9 formats (f0/f1/f2/f3/f4/f6/f7/f11/n0) | pure ALU lane-pick; `extrvrn_2x32` is the lone **8**-slot exception (no `n0`) |
| `extrpr*` (4) | `extrprnx16`, `extrpr2nx8`, `extrprn_2x32`, `extrpr64n_4x64` | 15 | S3_ALU + S0_LdSt (f0/f1/f2/f3/f6/f7) | predicate-write has LdSt reach but **no** Mul slot (vs `rep`) |
| `extract*` (2) | `extractbl`, `extractbh` | 8 | **S1_Ld only**, f0–f7 + `n2_s1_ld` | boolean extract rides the **Ld** pipe (single-word thunk, no `word1`) |

> **GOTCHA — `extrn_2x32` (immediate index) and `extrvrn_2x32` (AR index) are *different opcodes*, not
> one op with a flag, and the AR-indexed form is byte-granular.** `extrn_2x32` (opc# 1212) takes the
> lane as an **immediate** (`ivp_extrn_2x32 a3, v1, 3`); `extrvrn_2x32` (opc# 1219) takes it from an
> **`AR` register** (`ivp_extrvrn_2x32 a3, v1, a2`), proven by the device oracle (§5). The AR-indexed
> leaf interprets the index **byte-granularly into the 512-bit register** with a `cmp $0x40`
> out-of-range guard that returns 0 (§4.5), whereas the immediate form masks the index to the lane
> count (`& 0xf` for 16 lanes). A reimplementation that models one "extract with variable index" is
> wrong: the two have distinct iclasses and distinct index semantics. The same split exists for the
> predicate destination (`extrprn_2x32` imm vs `extrprvrn_2x32` vec-indexed).

---

## 3. The lane↔scalar bridge matrix — how the four files interconnect

This is the table a reimplementer needs: for each mnemonic, the **source → destination** file, the
**index source**, and the **lane/bit mapping**. The four files this batch touches
([register-files](../core/register-files.md)): `AR` (idx0, 32b×64), `vec` (idx2, 512b×32 = the lane
axis), `vbool` (idx3, 64b×16 boolean masks), `b32_pr` (idx6, 64b×16 packed predicate). The width
reshape is **proven by the value-leaf name** (`<verb>_<outw>_<inw>_<ctx>`): e.g.
`extr_nx16_32_512_32` is `out32 ← in512[lane32]` (32-bit out, 512-bit in, 32-bit lane-context arg).

| mnemonic | source | dest | index source | width map (out ← in) | value leaf |
|---|---|---|---|---|---|
| `repnx16` | `vec` 512 | `vec` 512 | immediate | broadcast in[k16] → all 32 lanes | `rep_nx16_512_512_32` |
| `rep2nx8` | `vec` 512 | `vec` 512 | immediate | broadcast in[k8] → all 64 lanes | `rep_2nx8_512_512_32` |
| `repn_2x32` | `vec` 512 | `vec` 512 | immediate | broadcast in[k32] → all 16 lanes | `rep_n_2x32_512_512_32` |
| `repnx16t`/`rep2nx8t`/`repn_2x32t` | `vec` 512 (+`vbool`) | `vec` 512 | immediate | masked broadcast (RMW merge) | *(same base leaf)* + `writeback__*t` |
| `injbinx16` | `vbool` + `vec` 512 | `vec` 512 | immediate | inject bit → in[k16] | `injbi_16_16_2_32` |
| `injbi2nx8` | `vbool` + `vec` 512 | `vec` 512 | immediate | inject bit → in[k8] | `injbi_8_8_1_32` |
| `injbin_2x32` | `vbool` + `vec` 512 | `vec` 512 | immediate | inject bit → in[k32] | `injbi_32_32_4_32` |
| `extrnx16` | `vec` 512 | `AR` 32 | immediate | **sign-ext**(in[k16]) → AR | `extr_nx16_32_512_32` |
| `extr2nx8` | `vec` 512 | `AR` 32 | immediate | in[k8] → AR | `extr_2nx8_32_512_32` |
| `extrn_2x32` | `vec` 512 | `AR` 32 | immediate | in[k32] → AR | `extr_n_2x32_32_512_32` |
| `extrvrn_2x32` | `vec` 512 | `AR` 32 | **AR reg** | in[byte k] → AR (bound 0x40) | `extrvr_n_2x32_32_512_32` |
| `extrprnx16` | `vec` 512 | `b32_pr` 64 | immediate | in[k16] → pr (hi=0) | `extrpr_nx16_64_512_32` |
| `extrpr2nx8` | `vec` 512 | `b32_pr` 64 | immediate | in[k8] → pr (hi=0) | `extrpr_2nx8_64_512_32` |
| `extrprn_2x32` | `vec` 512 | `b32_pr` 64 | immediate | in[k32] → pr (hi=0) | `extrpr_n_2x32_64_512_32` |
| `extrpr64n_4x64` | `vec` 512 | `b32_pr` 64 | immediate | in[k64] → pr | `extrpr64_n_4x64_64_512_32` |
| `extrprvrn_2x32` | `vec` 512 | `b32_pr` 64 | **vec lane** | in[k] → pr | `extrprvr_n_2x32_64_512_32` |
| `dextrprn_2x32` | `vec` 512 | `b32_pr` 64 | imm (dual) | {in[k0],in[k1]} → pr pair | `dextrpr_n_2x32_64_512_512_32_32` |
| `extractbl` | `vbool` | `vbool` 2b | (none) | bool low → 2-bit field | `extractbl_2_1` |
| `extractbh` | `vbool` | `vbool` 2b | (none) | bool high → 2-bit field | `extractbh_2_1` |

> **The matrix is a star with `vec` at the centre and three rims (AR / b32_pr / vbool).** Of the 21
> ops, **19 read or write `vec`** (the lane axis); the only non-`vec` ops are `extractbl`/`extractbh`
> (`vbool`→`vbool`). The three extract rims (`extr*`→`AR`, `extrpr*`→`b32_pr`, plus the boolean
> `extract*`) are the *exits* from the lane axis to a scalar/predicate; `rep*` (lane→all-lanes) is the
> only intra-`vec` op and `inj*` (boolean→lane) is the only *entry*. There is **no** direct
> `vec[k]`→`vbool` *value* extract and **no** `AR`-scalar→all-lanes broadcast in this batch (the
> AR→lane *inject* is B09's `movva*`, and the AR→all-lanes broadcast of an **immediate** is B09's
> `movvint*`); a reimplementation routes those through B09. `[HIGH/OBSERVED]` on the enumerated edges;
> `[MED/INFERRED]` on the *absence* claim (an enumeration over the 21, not a proof no other op bridges).

### 3.1 The replicate / extract C model (the two inverse single-lane bridges)

The `rep`/`extr` pair is the scalar↔lane boundary, exactly inverse: `rep` fans **one** lane out to all,
`extr` picks **one** lane in. The value-leaf bodies (disassembled and executed live, §4) give the bit
map:

```c
// ivp_repnx16 : broadcast vec lane k (16-bit) to all 32 lanes   (leaf rep_nx16_512_512_32)
//   k is masked to the lane count: k &= 0x1f (32 lanes); the host model does
//   movd 2*k(%rsi),%xmm0 ; pshuflw/pshufd to broadcast ; 4x movups (64 bytes).
void repnx16(uint16_t vd[32], const uint16_t vs[32], unsigned k) {
    uint16_t v = vs[k & 0x1f];                 // select one lane
    for (int l = 0; l < 32; l++) vd[l] = v;    // fan out to all
}

// ivp_extrnx16 : pull vec lane k (16-bit) into an AR scalar, SIGN-extend  (leaf extr_nx16_32_512_32)
int32_t extrnx16(const int16_t vs[32], unsigned k) {
    return (int32_t)vs[k & 0x1f];              // sext16->32
}

// ivp_extrn_2x32 : pull vec lane k (32-bit) into an AR scalar  (leaf extr_n_2x32_32_512_32)
uint32_t extrn_2x32(const uint32_t vs[16], unsigned k) {
    return vs[k & 0xf];                        // 32-bit lane, no extension
}
```

The decisive asymmetry inside the extract family: **`extrnx16` sign-extends, the 8- and 32-bit forms
do not** (the 8-bit `extr2nx8` zero-extends the byte; the 32-bit `extrn_2x32` is width-exact). A
reimplementation that always zero-extends a 16-bit lane to AR is wrong for `extrnx16`
(`0x8000 → 0xFFFF8000`, not `0x00008000` — proven live in §4.2).

### 3.2 The predicate-destination extract — `extrpr*` (lane → b32_pr, high word zeroed)

`b32_pr` is a 64-bit packed-predicate file ([register-files §3](../core/register-files.md)). The
`extrpr<shape>` ops are *exactly* the `extr<shape>` lane-pick but with the result deposited into a
64-bit `b32_pr` register, with the **upper 32 bits explicitly cleared** (`movl $0x0,0x4(%rcx)` in the
leaf body):

```c
// ivp_extrprn_2x32 : pull vec lane k (32-bit) into a 64-bit b32_pr, high word = 0
//   (leaf extrpr_n_2x32_64_512_32, executed live)
void extrprn_2x32(uint64_t *pr, const uint32_t vs[16], unsigned k) {
    *pr = (uint64_t)vs[k & 0xf];               // lo = lane[k], hi = 0
}
```

Executed live (§4): `extrpr_n_2x32` lane k returns `{lo = vs[k], hi = 0}` for k ∈ {0,1,8,15} — the high
word is unconditionally zero, so a `b32_pr` produced this way is a *zero-extended* lane, never
sign-extended (contrast the AR-destination `extrnx16` which sign-extends).

---

## 4. Lane value semantics — proven by execution

The `module__xdref_*` value leaves in `libfiss-base.so` are the per-element transfer functions,
**callable in-process via ctypes with no license** ([coverage-tally §5](../core/coverage-tally.md)).
The ABI is `void leaf(int ctx, <ins…>, T *out)`; for the lane ops the lane index is a leading `int`
argument and the `vec` operands are pointers to 64-byte register images. **Ten leaves were disassembled
*and executed live***, all bit-exact certificates of the lane semantics.

### 4.1 Lane broadcast — `rep_nx16` / `rep_n_2x32` / `rep_2nx8` (executed live)

The `rep_n_2x32_512_512_32` leaf @ `0x856b90` is a 16-way index dispatch (`%edx & 0xf`) feeding a
single broadcast tail:

```asm
856b90: 83 e2 0f        and  $0xf,%edx          ; lane &= 15  (16 lanes of 32b)
856b93: 0f 84 …         je   856c20              ; dispatch by lane
   …    (cmp $1..$0xe, je to per-lane movd of 0x{lane*4}(%rsi))
856c20: 66 0f 6e 06     movd  (%rsi),%xmm0       ; load selected lane (here lane 0)
856c24: 66 0f 70 c0 00  pshufd $0x0,%xmm0,%xmm0  ; broadcast lane0 across all 4 dwords
856c29: 0f 11 01        movups %xmm0,(%rcx)      ; write 4×128b = 512b to all lanes
856c2c: …               movups %xmm0,0x10/0x20/0x30(%rcx)
856c38: c3              ret
```

ABI: `void rep_n_2x32(int ctx, const void *vs@rsi, int lane@edx, void *vd@rcx)`. Executed live (lane k
selects one source lane; all output lanes must equal it):

```
rep_nx16  lane=0 : out[0]=0x1000 out[31]=0x1000  all-32-same=True  (in[0]=0x1000)   OK
rep_nx16  lane=7 : out[0]=0x1007 out[31]=0x1007  all-32-same=True                   OK
rep_nx16  lane=31: out[0]=0x101f out[31]=0x101f  all-32-same=True                   OK
rep_n_2x32 lane=0 : out[0]=out[15]=0x0000aa00     all-16-same=True                   OK
rep_n_2x32 lane=15: out[0]=out[15]=0x0000aa0f     all-16-same=True                   OK
rep_2nx8  lane=0 : out[0]=out[63]=0x40            all-64-same=True (in[0]=0x40)      OK
rep_2nx8  lane=63: out[0]=out[63]=0x7f            all-64-same=True                   OK
```

All three lane widths (8/16/32) confirmed: the selected lane appears identically in **every** output
lane (32 / 64 / 16), the rest of the register is overwritten. The lane index masks to the lane count
(`&0x1f`, `&0x3f`, `&0xf`). This is the **lane-broadcast certificate** — `out[l] = in[k]` for all l,
k=immediate.

### 4.2 Lane extract to AR — width & sign-extension (executed live)

`extr_n_2x32_32_512_32` @ `0x814820` is the same 16-way dispatch but the tail is `mov 0x{k*4}(%rsi),%eax
; mov %eax,(%rcx)` — pick one 32-bit lane, write it to the AR-image. `extr_nx16` @ `0x870ec0`
sign-extends the 16-bit lane. Executed live:

```
extr_n_2x32 lane=0 = 0x0000aa00   (in[0]=0xaa00)            OK   extr_n_2x32 lane=15 = 0x0000aa0f  OK
extr_nx16   lane=0 = 0x00007fff   (in=0x7fff, positive)
extr_nx16   lane=1 = 0xffff8000   (in=0x8000  -> SIGN-extended)
extr_nx16   lane=2 = 0xffffffff   (in=0xffff  -> SIGN-extended)
```

The `extr_n_2x32` returns the lane verbatim (AR == lane[k], the **lane-extract certificate**). The
`extr_nx16` **sign-extends** the 16-bit lane (0x8000 → 0xFFFF8000) — the single most reimplementation-
critical fact in this batch: the AR-destination 16-bit extract is *signed*, while the
predicate-destination `extrpr*` and the 8-bit `extr2nx8` are zero-extending. `[HIGH/OBSERVED by execution]`

### 4.3 The `t`-form — predicated-writeback merge (NOT a distinct value)

The `t` mnemonics (`repnx16t`, `rep2nx8t`, `repn_2x32t`) resolve to the **same base value leaf** as
their unmasked form — there is **no** `rep_nx16t` `module__xdref_*` leaf (`nm | rg 'rep.*t'` over the
xdref glob = ∅). The `t` predication lives in the **writeback**: `writeback__ivp_repnx16t` @ `0x4bfa30`
does, per 32-bit destination word `i`:

```asm
4bfa56: mov   (%rsi),%r9d        ; computed value (the broadcast)
4bfa59: and   %r8d,%r9d          ; &= per-lane mask    (mask = vbool lane word)
4bfa5c: not   %r8d
4bfa5f: and   0x2c(%rdi),%r8d    ; old_dest &= ~mask
4bfa63: or    %r9d,%r8d          ; merge
4bfa66: mov   %r8d,(%rsi)        ; write back  ->  new = (computed & mask) | (old & ~mask)
```

i.e. a lane whose mask bit is **0 keeps its prior destination value** (read-modify-write merge), a lane
whose mask bit is **1 takes the broadcast**. The unmasked `repnx16` has a separate, unconditional
`writeback__ivp_repnx16` @ `0x38dd80`. A reimplementation must model `repnx16t` as a **merge**
(`vd[l] = mask[l] ? vs[k] : vd[l]`, reading the old `vd`), not a conditional that zeros the false lanes
— identical to B09's `mov2nx8t` and the [B03](b03-vec-alu-rest.md) `t`-throttle convention. The device
oracle confirms the extra operand: `IVP_REPNX16T v3, v1, 5, vb2` (a `vbool` `vb2` write-enable, §5).

### 4.4 Inject — boolean-into-lane (executed live)

`injbi_8_8_1_32` @ `0x5e92b0` builds a 1-lane mask from the immediate position and merges the boolean:

```asm
5e92b0: and  $0x7,%ecx           ; lane &= 7   (8 lanes of the byte field)
5e92bb: shl  %cl,%eax            ; mask = 1 << lane          (eax started at 1)
5e92b8: shl  $0x1f,%edx ; sar $0x1f,%edx   ; bit = -(bit & 1)  -> all-ones if odd else 0
5e92cb: and  %edx,%eax           ; (bit & mask)
5e92cd: and  ~mask, in           ; (pred & ~mask)
5e92cf: or  ; mov %eax,(%r8)      ; out = (pred & ~mask) | (bit & mask)
```

ABI: `void injbi_8(int ctx, int pred@esi, int bitval@edx, int lane@ecx, int *out@r8)`. Executed live:

```
injbi_8  pred=0x00 bit=1 lane=0 -> 0x01    injbi_8  pred=0x00 bit=1 lane=3 -> 0x08    OK
injbi_8  pred=0xff bit=0 lane=5 -> 0xdf    injbi_8  pred=0xff bit=1 lane=5 -> 0xff    OK
injbi_8  pred=0xaa bit=1 lane=0 -> 0xab    injbi_8  pred=0xaa bit=0 lane=1 -> 0xa8    OK
injbi_16 pred=0x0000 bit=1 lane=15 -> 0x8000   injbi_16 pred=0xffff bit=0 lane=8 -> 0xfeff  OK
```

`out = (pred & ~(1<<lane)) | (bit ? (1<<lane) : 0)`, bit-exact for all sweeps. The boolean is taken as
its low bit (`bit&1`), broadcast to the full mask, and the dest lane is the only one changed — a
**single-lane masked merge**, the structural inverse of the boolean `extract*`. The device oracle
spells the `vbool` source: `IVP_INJBINX16 v3, vb0, 5` (§5).

### 4.5 The variable-index extract — `extrvr_n_2x32` (byte-granular, bounded, executed live)

`extrvr_n_2x32_32_512_32` @ `0x8154c0` differs from the immediate `extr_n_2x32` in two ways a
reimplementer must replicate exactly:

```asm
8154c0: xor  %eax,%eax           ; default result = 0
8154c2: cmp  $0x40,%edx          ; index > 64 ?
8154c5: ja   8154cd              ;   -> return 0   (OUT-OF-RANGE GUARD)
8154c7: test %edx,%edx ; jne …   ; idx==0 fast path: mov (%rsi),%eax
   …    (large dispatch on idx, byte-granular offsets)
```

Executed live (the index is **byte-granular**, not lane-granular, and bounded at 0x40):

```
extrvr_n_2x32 idx=0x00 -> 0x0000bb00   idx=0x01 -> 0x010000bb   idx=0x08 -> 0x0000bb02
extrvr_n_2x32 idx=0x0f -> 0x00bb0400   idx=0x41 -> 0x00000000   idx=0x80 -> 0x00000000
```

`idx=1` shifts the 4-byte window by one byte (a *byte* offset into the 512-bit image, not a 32-bit lane
offset); `idx > 0x40` returns 0. This is **structurally different** from the immediate `extr_n_2x32`
(which masks the index to the lane count and reads aligned 32-bit lanes). The AR-sourced index is a raw
byte position with an out-of-range floor — a reimplementation must implement the byte-granular window +
bound, not a clean lane select. The device oracle confirms the AR operand:
`IVP_EXTRVRN_2X32 a3, v1, a2` (§5).

### 4.6 Predicate extract & boolean extract (executed live)

`extrpr_n_2x32_64_512_32` @ `0x82d8b0` is the `extr` lane-pick with a 64-bit deposit (`movl $0x0,
0x4(%rcx)` clears the high word). `extractbl_2_1` / `extractbh_2_1` @ `0x870e00` / `0x870e10` extract a
boolean lane to a 2-bit field by replicating bit0 (`shl $0x1f; sar $0x1f; and $0x3` = `-(in&1) & 3`):

```
extrpr_n_2x32 lane=0  -> 0x000000000000cc00  (hi word = 0)   lane=15 -> 0x...cc0f  hi=0   OK
extractbl in=0x0 -> 0x00   in=0x1 -> 0x03   in=0x2 -> 0x00   in=0x3 -> 0x03   in=0xff -> 0x03
extractbh in=0x1 -> 0x03   in=0x2 -> 0x00   (identical body to extractbl)
```

`extrpr*` zero-extends the lane into the 64-bit predicate; `extract{bl,bh}` map a boolean's low bit to a
2-bit `xtbool2` field (`0/1 → 0x0/0x3`, replicating the bit across both positions).

### 4.7 The scalar-narrow replicate helpers — `rep_16_8` / `replo8_16_16` (executed live)

Two scalar (non-vector) replicate leaves accompany the family: `rep_16_8` @ `0x82d040` broadcasts an
8-bit value into both byte positions of a 16-bit word, and `replo8_16_16` @ `0x82d020` replicates the
**low** byte across both byte positions:

```
rep_16_8(0x00)=0x0000  rep_16_8(0x7f)=0x7f7f  rep_16_8(0xab)=0xabab  rep_16_8(0xff)=0xffff   (= (x<<8)|x)
replo8_16_16(0x1234)=0x3434  replo8_16_16(0xabcd)=0xcdcd  replo8_16_16(0xab00)=0x0000        (low byte dup)
```

`rep_16_8 = (x<<8)|x`; `replo8 = (lo<<8)|lo` where `lo = x & 0xff`. These are the per-element building
blocks the wider `rep_2nx8` lane-broadcast composes (a byte splat realised lane-wise); they appear as
discrete leaves because the dtype-typed leaf set enumerates the narrow-element cases.

---

## 5. Device-assembler oracle — byte-exact round-trip

The end-to-end check: feed the **device-native** `xtensa-elf-as`
(`XTENSA_SYSTEM=…/ncore2gp/config`, `XTENSA_CORE=ncore2gp`) each op with its operands, then
disassemble back. **13 of the 21** assemble `rc=0` and round-trip to the same lowercase mnemonic with
the correct register-file operands and the correct index source. Verbatim bytes (LE, the assembler's
packed-bundle layout):

| assembled | bundle bytes | disasm bundle | proves |
|---|---|---|---|
| `IVP_REPNX16 v3, v1, 5`        | `32501208c0c1452f` | `{ …; ivp_repnx16 v3, v1, 5 }`        | vec→vec, **immediate** lane index |
| `IVP_REP2NX8 v3, v1, 5`        | `3250120880c1452f` | `ivp_rep2nx8 v3, v1, 5`               | 8-bit broadcast, imm index |
| `IVP_REPN_2X32 v3, v1, 5`      | `3250122880c1452f` | `ivp_repn_2x32 v3, v1, 5`             | 32-bit broadcast, imm index |
| `IVP_REPNX16T v3, v1, 5, vb2`  | `3250122820c1452f` | `ivp_repnx16t v3, v1, 5, vb2`         | **4th operand `vb2` = `vbool` mask** (t-form) |
| `IVP_EXTRNX16 a3, v1, 7`       | `325153087681452f` | `ivp_extrnx16 a3, v1, 7`              | vec→**AR**, imm index |
| `IVP_EXTR2NX8 a3, v1, 7`       | `325113086001452f` | `ivp_extr2nx8 a3, v1, 7`              | vec→AR, 8-bit |
| `IVP_EXTRN_2X32 a3, v1, 3`     | `3250d9087201452f` | `ivp_extrn_2x32 a3, v1, 3`            | vec→AR, 32-bit, imm |
| `IVP_EXTRVRN_2X32 a3, v1, a2`  | `0009192388cc8300` | `ivp_extrvrn_2x32 a3, v1, a2`         | vec→AR, **index from AR reg `a2`** |
| `IVP_EXTRPRNX16 pr3, v1, 7`    | `3251530876c1452f` | `ivp_extrprnx16 pr3, v1, 7`           | vec→**b32_pr**, imm |
| `IVP_EXTRPRN_2X32 pr3, v1, 3`  | `3250d9087241452f` | `ivp_extrprn_2x32 pr3, v1, 3`         | vec→b32_pr, 32-bit |
| `IVP_EXTRPR2NX8 pr3, v1, 7`    | `325113086041452f` | `ivp_extrpr2nx8 pr3, v1, 7`           | vec→b32_pr, 8-bit |
| `IVP_EXTRPR64N_4X64 pr3, v1, 1`| `32514c006441452f` | `ivp_extrpr64n_4x64 pr3, v1, 1`       | vec→b32_pr, 64-bit lane |
| `IVP_INJBINX16 v3, vb0, 5`     | `022524088843352f` | `ivp_injbinx16 v3, vb0, 5`            | **`vbool` src `vb0`** → vec lane 5 |
| `IVP_EXTRACTBL vb3, vb1`       | `02a462283c81c52f` | `ivp_extractbl vb3, vb1`              | **`vbool`→`vbool`**, no index (2-operand) |

Three structural facts the oracle pins:

* **The index source is part of the opcode, not a register-class choice.** `rep*`/`extr*`/`extrpr*`
  take an **immediate** third operand (`v3, v1, 5`); `extrvrn_2x32` takes an **AR register** (`a3, v1,
  a2`); the device assembler *rejects* the wrong arity (`IVP_EXTRNX16 a3, v1, a2` → "invalid symbolic
  operand", confirming the immediate form will not accept a register, and vice-versa). This is the
  encoding proof of the §2.5 GOTCHA.
* **The destination file round-trips exactly as the bridge matrix predicts.** `extr* a3, v1` (AR
  dest), `extrpr* pr3, v1` (`b32_pr` dest), `rep* v3, v1` (vec dest), `inj* v3, vb0` (vec dest, `vbool`
  src), `extractbl vb3, vb1` (`vbool` both). The register short-names (`a`/`v`/`pr`/`vb`) round-trip as
  [register-files](../core/register-files.md) specifies.
* **The `t`-form's fourth operand is a `vbool`.** `IVP_REPNX16T v3, v1, 5, vb2` round-trips with the
  mask register spelled — the encoding-level witness for the §4.3 predicated-writeback merge.

The eight not shown (`repnx16t`-family 8-bit/32-bit, `injbi2nx8`/`injbin_2x32`, `extrprvrn_2x32`,
`dextrprn_2x32`, `extrnx16`-variants, and the boolean `extractbh`) either share the spelling of a shown
sibling or use an operand class the bare oracle probe did not spell (`extrvr`/`dextr` need specific
file+arity combinations the probe sweep did not land); they are documented from the encode thunk + the
executed value leaf instead (§2, §4). The device byte order is the assembler's packed-bundle layout and
is **not** byte-identical to the §2 slot-normalised selector imm (a different representation); they
agree **structurally** — the placement exists, the mnemonic, register files, and index source
round-trip — which is the property the oracle certifies. `[HIGH/OBSERVED]`

---

## 6. Batch coverage tally — 21 mnemonics / 285 placements

Counted with `nm libisa-core.so | rg -c 'Opcode_ivp_<mn>_Slot_…_encode'` per mnemonic
(never the decompile — [coverage-tally §0 GOTCHA](../core/coverage-tally.md)). Every one of the 21
grounds to ≥ 8 placements; **none ungrounded**.

| sub-family | mnemonics | placements | notes |
|---|--:|--:|---|
| replicate (`repnx16`/`rep2nx8`/`repn_2x32` + 3 `t`-forms) | 6 | 17×6 = 102 | 17 slots each (S3_ALU + S0_LdSt + S2_Mul) |
| inject (`injbinx16`/`injbi2nx8`/`injbin_2x32`) | 3 | 18×3 = 54 | 18 slots (the rep 17 + `n1_s2_mul`) |
| extract→AR, imm-index (`extrnx16`/`extr2nx8`/`extrn_2x32`) | 3 | 9×3 = 27 | 9 S3_ALU slots each |
| extract, dual / vec-indexed (`extrprvrn_2x32`/`dextrprn_2x32`) | 2 | 9×2 = 18 | 9 S3_ALU slots each |
| extract→AR, AR-index (`extrvrn_2x32`) | 1 | 8 | the lone **8**-slot op (no `n0_s3_alu`) |
| extract→b32_pr, imm (`extrprnx16`/`extrpr2nx8`/`extrprn_2x32`/`extrpr64n_4x64`) | 4 | 15×4 = 60 | 15 slots (S3_ALU + S0_LdSt, no Mul) |
| boolean extract (`extractbl`/`extractbh`) | 2 | 8×2 = 16 | 8 **S1_Ld** slots each |
| **TOTAL** | **21** | **285** | |

Arithmetic: `102 + 54 + 27 + 18 + 8 + 60 + 16 = 285`. ✓ (Equivalently `102+54+45+8+60+16`, grouping
the 5 nine-slot extracts as `45`.) These 285 are a strict subset of the **12 569 certified-perfect
placements**; the broadcast/inject/extract ops adjacent in name to B09's `movva*`/`movvint*` (B09's
246), the squeeze/QLI in [B24](b24-composite.md), and the `sel`/`shfl` permutations in
[B21](b21-select-shuffle.md) are counted in **those** batches — no double-count. The 21 mnemonics roll
into the **1065-op vector axis**; the batch adds **0** to the scalar axis (every row is `package ==
xt_ivp32`, `ivp_`-prefixed, confirmed by the §2 `opcodes[]` walk). The value-leaf tally is **20**
distinct `module__xdref_*` leaves (the 18 lane leaves + the 2 scalar-narrow helpers `rep_16_8` /
`replo8_16_16`), rolling into the 864 value-leaf denominator. `[HIGH/OBSERVED]`

> **NOTE — the `t`-forms add placements and mnemonics but *no* new value leaf.** `repnx16t`/`rep2nx8t`/
> `repn_2x32t` are 3 of the 21 mnemonics and contribute 51 of the 285 placements, but they resolve to
> the **same** base value leaf as the unmasked form (§4.3); the predication is in `writeback__*t`, not a
> distinct `xdref`. So the value-leaf count (20) is *below* the lane-op count, exactly as the roll-up
> model expects (one leaf serves a dtype/predication family).

---

## 7. Adversarial self-verification — the five strongest claims

1. **"21 mnemonics / 285 placements; `extr*` is B16's by the scope charter, overriding the §4.2 B21
   glob."** `nm libisa-core.so | rg -o 'Opcode_ivp_(rep|inj|extr|dextr|extract)[a-z0-9_]*_Slot'
   | sort -u` = **21** distinct; the per-mnemonic placement sum = **285** (`102+54+27+18+8+60+16`).
   There are **no** `splat`/`bcast`/`dup` opcodes (the broadcast verb is `rep`).
   The §6 table sums 285; the 21 roll into the 1065 vector axis. **Confirmed.**
2. **"`rep` is a lane broadcast: `out[l] = in[k]` for *all* l, k=immediate."** Executing
   `rep_nx16`/`rep_n_2x32`/`rep_2nx8` live: every output lane (32 / 16 / 64) equalled the
   selected source lane for k ∈ {0,1,7,15,31,32,63}, with the index masked to the lane count
   (`&0x1f`/`&0xf`/`&0x3f`). A non-broadcast (e.g. copy) implementation would leave non-`k` lanes
   distinct. **Confirmed by execution.**
3. **"`extrnx16` SIGN-extends the 16-bit lane to AR; the predicate-destination `extrpr*` and 8-bit
   `extr2nx8` ZERO-extend."** Executed `extr_nx16(0x8000) = 0xFFFF8000`, `extr_nx16(0xffff) =
   0xFFFFFFFF` (sign) vs `extrpr_n_2x32` returning `{lo=lane, hi=0}` (zero, high word explicitly
   cleared). Two distinct extension behaviours by destination file — a reimplementation that uses one
   rule is wrong at the 16-bit signed boundary. **Confirmed by execution.**
4. **"The `t`-form is a `vbool`-masked RMW merge (mask-0 lanes keep the old destination), sharing the
   base value leaf."** Three witnesses: (a) `nm | rg 'rep.*t'` over the `module__xdref_` glob = ∅ (no
   distinct value leaf); (b) `writeback__ivp_repnx16t` @ `0x4bfa30` is `new = (computed & mask) | (old &
   ~mask)` per lane (disassembled); (c) the device oracle spells the mask operand:
   `IVP_REPNX16T v3, v1, 5, vb2`. A zero-fill model would be wrong on mask-0 lanes. **Confirmed.**
5. **"`extrn_2x32` (immediate index) and `extrvrn_2x32` (AR-register index) are different opcodes with
   different index semantics."** Distinct opc# (1212 vs 1219), distinct iclass
   (`IVP_EXTRN_2X32` vs `IVP_EXTRVRN_2X32`), distinct device syntax (`extrn_2x32 a3,v1,3` immediate vs
   `extrvrn_2x32 a3,v1,a2` register — the assembler rejects the cross), and distinct executed
   semantics: `extr_n_2x32` masks the index to lane count and reads aligned lanes; `extrvr_n_2x32` is
   **byte-granular** with a `cmp $0x40` out-of-range floor (`idx=1 → byte-shifted window`,
   `idx>0x40 → 0`). **Confirmed by execution.**

**Ungrounded / flagged items** (honest residue): (a) The `dextrprn_2x32` **dual-lane pairing** — that
it extracts *two* lanes into a `b32_pr` *pair* — is `[HIGH/OBSERVED]` from the leaf name suffix
(`_64_512_512_32_32`, two 512-bit ins) and the `d`-prefix, but the **exact two source-lane fields** in
the encoding are `[MED/INFERRED]` (the device oracle did not round-trip its specific operand class;
the encode thunk and opc# are OBSERVED). (b) The `extractbl` vs `extractbh` **low/high
distinction** — both leaf bodies disassemble to the *identical* `-(in&1)&3`, so the
low-vs-high difference is in the **source-lane selector field** of the encoding (different selector
imms: `0x4a0ee0` vs `0x4a0de0`), not the value body; the semantic label (`bl`=low half, `bh`=high half
of the boolean pair) is `[MED/INFERRED]` from the mnemonic + the `+1`-stepped selector. (c) The
`extrprvrn_2x32` **vec-lane index source** is `[HIGH/OBSERVED]` from the `vr` infix matching `extrvr`'s
register-index pattern, but its index was not driven live (the leaf takes a vec-image index
arg). None is a missing decode or a missing transfer semantics — every row has a resolved encode thunk,
an `opc#`/iclass/package from a direct `opcodes[]` walk, a value leaf, and (for 13 of 21) a device
round-trip.

---

## 8. Function & symbol map

`libisa-core.so` unless noted. `.text`/`.rodata`: VMA == file. `.data.rel.ro`: file = VMA − `0x200000`
(`readelf -SW` — **not** libtpu's `0x400000`).

| Symbol / table | Addr | Role |
|---|---|---|
| `opcodes` | `0x6ce6c0` (file `0x4ce6c0`) | 1534 × stride 72; all 21 rows resolved (`opc#`/iclass/pkg=`xt_ivp32`) |
| `Opcode_ivp_repnx16_Slot_f0_s3_alu_encode` | `.text` | `movl $0x86900020` (lane-broadcast selector), `word1=0` |
| `Opcode_ivp_extrnx16_Slot_f0_s3_alu_encode` | `.text` | `movl $0x80e68200` (lane→AR selector) |
| `Opcode_ivp_extractbl_Slot_f0_s1_ld_encode` | `.text` | `movl $0x004a0ee0` (S1_Ld single-word, no `word1`) |
| `module__xdref_rep_nx16_512_512_32` | `0x855b20` (`libfiss-base.so`) | 16-bit lane broadcast (executed live) |
| `module__xdref_rep_n_2x32_512_512_32` | `0x856b90` (`libfiss-base.so`) | 32-bit lane broadcast — `movd`+`pshufd`+4×`movups` (executed live) |
| `module__xdref_rep_2nx8_512_512_32` | `0x8560c0` (`libfiss-base.so`) | 8-bit lane broadcast, 64 lanes (executed live) |
| `module__xdref_extr_n_2x32_32_512_32` | `0x814820` (`libfiss-base.so`) | lane→AR 32-bit (executed live) |
| `module__xdref_extr_nx16_32_512_32` | `0x870ec0` (`libfiss-base.so`) | lane→AR 16-bit **sign-ext** (executed live) |
| `module__xdref_extrvr_n_2x32_32_512_32` | `0x8154c0` (`libfiss-base.so`) | AR-indexed byte-granular extract, bound 0x40 (executed live) |
| `module__xdref_extrpr_n_2x32_64_512_32` | `0x82d8b0` (`libfiss-base.so`) | lane→b32_pr 64-bit, hi=0 (executed live) |
| `module__xdref_injbi_8_8_1_32` | `0x5e92b0` (`libfiss-base.so`) | boolean→lane masked merge (executed live) |
| `module__xdref_extractbl_2_1` / `extractbh_2_1` | `0x870e00` / `0x870e10` (`libfiss-base.so`) | boolean→2-bit field (executed live) |
| `module__xdref_rep_16_8` / `replo8_16_16` | `0x82d040` / `0x82d020` (`libfiss-base.so`) | scalar-narrow byte replicate helpers (executed live) |
| `writeback__ivp_repnx16t` | `0x4bfa30` (`libfiss-base.so`) | the `t`-form `vbool`-masked RMW merge body |
| `writeback__ivp_repnx16` | `0x38dd80` (`libfiss-base.so`) | the unmasked-form unconditional writeback (contrast) |
| `xtensa-elf-as` / `xtensa-elf-objdump` | `tools/XtensaTools/bin/` | device round-trip oracle (`XTENSA_CORE=ncore2gp`, 13 of 21) |

---

## 9. Cross-references

* [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the schema this page
  follows, the `rep|splat|bcast|inj` classifier verb that routes the broadcast/inject ops here, and the
  1534/12569 roll-up this batch's 21/285 closes into (with the `extr*` scope override stated in §0).
* [The Eight Register Files](../core/register-files.md) — the `vec`/`AR`/`vbool`/`b32_pr` files this
  batch bridges; **§6.3** names the `ivp_sem_vec_rep_*` replicate family and `IVP_REPNX16`, **§6.4** the
  `vec→AR` extract/squeeze boundary, **§6.5** the `vbool↔vec` predicate bridge our `inj`/`extract` use.
* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid, the
  per-`(opcode×slot)` selector model, and the S3_ALU / S0_LdSt / S2_Mul / S1_Ld slot classes the §2.5
  spread rides.
* [ISA Coverage & the 1534/12569 Tally](../core/coverage-tally.md) — the certified denominator and the
  `nm`-not-decompile counting rule the §6 tally obeys.
* [B09 — Vector move / regfile bridge](b09-vec-mov.md) — the plain reg↔reg moves and the AR→lane
  `movva*` / immediate-broadcast `movvint*` injects we exclude (our `rep` broadcasts a *runtime lane*,
  our `inj` injects a *`vbool` boolean*); the `t`-throttle convention `repnx16t` shares with `mov2nx8t`.
* [B21 — Select / shuffle / compress](b21-select-shuffle.md) — the *arbitrary per-lane permutation*
  network (`sel`/`shfl`/`dsel`, a per-output-lane index vector) we are the **regular** (single-scalar-
  index) degenerate case of; B21 must not re-count our twelve `extr*` single-lane extracts.
* [B22 — Unpack / `wvec` move](b22-unpack-wvec-mov.md) / [B10 — `wvec` pack](b10-wvec-pack.md) — the
  `wvec` accumulator readouts (`pack`/`unpack`) our `extr`/`rep` never touch.
* [B08 — Cross-lane reduce](b08-reduce.md) / [B24 — Histogram / squeeze / QLI](b24-composite.md) — the
  reduce-to-scalar (`radd*`) and squeeze (`sqz`) ops that read *all* lanes + a mask (vs our single
  fixed lane).
* [B03 — Vector ALU residual](b03-vec-alu-rest.md) — the `t`-throttle (predicated-writeback) convention
  the `rep*t` forms share.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags and the proven-by-
  execution value lane.

---

*Provenance: encoding (`Opcode_*` thunks, `opc#`/iclass/package from a direct `opcodes[]` walk at file
`0x4ce6c0`, stride 72), placement counts, and the slot spread are `[HIGH/OBSERVED]` — disassembled /
`nm`-counted / table-parsed in-checkout from `libisa-core.so` (`ncore2gp/config`, not stripped,
`.data.rel.ro` delta `0x200000`). Lane value semantics are `[HIGH/OBSERVED by execution]` for ten leaves
(`rep_nx16`, `rep_n_2x32`, `rep_2nx8`, `extr_n_2x32`, `extr_nx16`, `extrvr_n_2x32`, `extrpr_n_2x32`,
`injbi_8_8_1_32`/`injbi_16_16_2_32`, `extractbl`/`extractbh`, `rep_16_8`/`replo8_16_16` — called live
via ctypes from the license-free `libfiss-base.so`) and `[HIGH/OBSERVED]`-by-disasm for the `t`-form
writeback merge. The byte round-trip is `[HIGH/OBSERVED]` from the device-native `xtensa-elf-as`/
`objdump` (`XTENSA_CORE=ncore2gp`, 13 of 21 round-tripped). All facts read as derived from
shipped-artifact static analysis and in-process execution of license-free leaves (lawful
interoperability RE).*
