# ISA Batch 22 — Unpack / wvec Move (lane-widen into the wide accumulator)

This is the per-instruction reference for the **`vec`→`wvec` unpack / lane-widen** family of the
Vision-Q7 *Cairo* (`ncore2gp`) 512-bit FLIX vector ISA — the **non-narrowing dual** of the
[B10 pack readout](b10-wvec-pack.md). Where [B04](b04-mac-integer.md)/[B05](b05-mac-mixed.md) *accumulate*
products into the 1536-bit [`wvec` wide accumulator](../core/register-files.md) and
[B10](b10-wvec-pack.md) *narrows* that accumulator back to a 512-bit `vec`, **B22 is the step that
primes the accumulator**: it reads narrow `vec` lanes (i16 / i32 / i64) and **widens** each into a
24/48/96-bit `wvec` accumulator slot with **zero-fill** (unsigned) or **sign-fill** (signed), so the very
first product a subsequent MAC adds into that slot cannot overflow the guard bits. The batch is the single
semantic group `ivp_sem_unpack_wvec_mov` — **18 mnemonics**: 16 `CVT*` widen-into-accumulator converts, the
`MOVWW` wide-accumulator→wide-accumulator copy (the *wide NOP*), and `MOVSCFV`, the one member that writes
no `wvec` at all (it loads the 11 floating-point control/status fields from a `vec` lane).

Every mnemonic's FLIX slot, opcode-selector template, operand model, value semantics and pipeline staging
are read directly out of the shipped binaries — `libisa-core.so` (encode thunks + per-slot field
accessors, **not stripped**), `libfiss-base.so` (`xdref` value leaves **driven live by `ctypes`**),
`libcas-core.so` (per-op ISS stage roster) — and round-tripped through the device
[`xtensa-elf-as`/`-objdump`](../../iss/fiss-datapath-oracle.md) toolchain (`XTENSA_CORE=ncore2gp`). All 18
ops are **execution-validated** by the four-oracle differential of
[VAL-09](../../validation/regfile-bridge-divergence.md): the widen leaves were `dlopen`'d and *called* on
the hard edge corpus and agreed bit-exact (e.g. `CVT24S(0x8000)=0xff8000` sign-fill vs
`CVT24U(0x8000)=0x008000` zero-fill).

Counts are grounded with `nm | rg -c` against the binary `.symtab`, never a decompile grep; the
`extracted/` tree is gitignored (reach it with `fd --no-ignore` or an absolute path). Confidence tags
follow [the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte / immediate
/ symbol / **executed** value read from the shipped binary; `INFERRED` = reasoned over OBSERVED; `CARRIED`
= re-used at a cited page's confidence; crossed with `HIGH`/`MED`/`LOW`. All prose is binary /
static-analysis derived only.

> **Scope in one line.** B22 = `ivp_sem_unpack_wvec_mov` (16 `CVT24/CVT48/CVT96` lane-widen converts +
> `MOVWW` wide-acc copy + `MOVSCFV` fp-CSR load) — reading **16/32/64-bit** `vec` lanes and writing
> **24/48/96-bit** `wvec` accumulator lanes with **zero-fill (U) / sign-fill (S)**, plus the two moves.
> **18 mnemonics, 141 placements, 10 widen value leaves.** The whole group rides the **Mul slot `s2`** —
> *not* the ALU slot — because writing the wide 1536-bit accumulator uses the multiply pipe's wide write
> port. `[HIGH/OBSERVED]`

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| B22 mnemonics (`ivp_sem_unpack_wvec_mov`) | **18** | `nm libisa-core.so \| rg -o 'Opcode_ivp_(cvt24\|cvt48\|cvt96\|cvtg48\|movww\|movscfv)[a-z0-9_]*_Slot_f0_s2_mul_encode' \| sort -u \| wc -l` = 18 |
| B22 placements (`mnemonic × slot`) | **141** | `= 17×8 + 1×5` — `rg -c 'Opcode_ivp_…_Slot_[a-z0-9]+_s2_mul_encode'` |
| Issue slot | **`s2 = Mul`** of every format — **never** `s3 = ALU` | `Opcode_ivp_*_Slot_<f>_s2_mul_encode` (all 141) |
| Formats hosting the 17 `wvec`-writing ops | `{f0, f1, f2, f3, f6, f7, f11, n1}` (8 each) | `rg -o 'Slot_([a-z0-9]+)_s2_mul' -r '$1' \| sort -u` |
| Formats hosting `MOVSCFV` | `{f0, f1, f2, f3, f7}` (5 — absent from `f6/f11/n1`) | per-op slot scatter (`movscfv` thunk roster) |
| Source (read) | **`vec`** (regfile idx **2**, **512-bit**, **32** entries) — `Nx16` / `N_2x32` / `N_2x64` lanes | regfile descriptor `vec @0x74a800+2·56` |
| Destination (write) | **`wvec`** (regfile idx **5**, **1536-bit**, **4** entries) — `Nx24` / `Nx48` / `Nx96` acc lanes | regfile descriptor `wvec @0x74a800+5·56` |
| `vec`→`wvec` lane widening | 16→24, 32→48 / 24, 64→96 / 48 (the inverse of B10's 24→8, 48→16, 96→32 narrow) | `xdref` width-sig `_<accbits>_<srcbits>` |
| Fill rule | **`S` = sign-fill** (replicate src sign into the guard bits) · **`U` = zero-fill** (guard bits = 0) | executed: `cvt24s/u_24_16`, `cvt48s/u_48_32` ([§4](#4-value-semantics--driven-live)) |
| Saturation / rounding | **none** — widening is exact; overflow impossible (dst slot strictly wider than src lane) | leaf bodies: bare `mov` + sign/zero fill, no clamp ([§4.4](#44-no-saturation-no-rounding)) |
| State in | **`CPENABLE`** (cp1 enable gate), sampled `@stage 3` | `my_CPENABLE_def` (`libcas-core`), `opcode__rsr_cpenable` (`libfiss-base`) |
| Exception | **`Coprocessor1Exception`** — raised iff cp1 disabled, *before* any datapath effect; op squashed | `Coprocessor1Exception_exc` (`libcas-core`), `exception__Coprocessor1Exception` (`libfiss-base`) |
| Pipeline | `vr`/`vs` **USE @stage 10**; `wvt` **DEF @stage 12** → **2-cycle** result latency (one stage deeper than `vec_alu`) | `F0_F0_S2_Mul_28_IVP_*_inst_stage{0..12}` (`libcas-core`) |
| `MOVSCFV` target | **11 fp-CSR fields** (`RoundMode` + `Inexact/Underflow/Overflow/DivZero/Invalid` × `Flag`/`Enable`) | `nm libcas-core.so \| rg -i '(RoundMode\|…Flag\|…Enable)'` = 11 |

The family is **16 CVTs** because three orthogonal axes multiply out: **accumulator width** (24 / 48 / 96) ×
**fill** (signed / unsigned) × **source-lane shape** (`Nx16` / `Nx32` / `N_2x64`, plus the `H`/`L`
half-select when the source lane is wider than the destination slot count and the `G` gather-permute
variant). [§2](#2-roster--the-18-mnemonics) lays the roster on those axes; [§3](#3-encoding) gives the
verified per-slot field map and the full 32-bit selector templates; [§4](#4-value-semantics--driven-live)
gives the executed value functions; [§5](#5-timing) the staging; [§6](#6-worked-examples--device-round-trip)
six byte-exact round-tripped bundles.

### 1.1 Why the Mul slot, and why one stage deeper

The placement is structurally forced and was confirmed two independent ways. **(a)** Every one of the 141
encode thunks is named `…_Slot_<fmt>_s2_mul_encode` — the `vec_alu` `s3` slot hosts *none* of them. **(b)**
The `libcas-core.so` ISS roster prefixes each op `F0_F0_S2_Mul_28_…` (and `F11_F11_S2_Mul_41_…`, etc.),
the *Mul* class. The reason: the result is the **1536-bit `wvec`**, and the only register file the datapath
can write at that width is the one the multiply pipe owns (the partial-product / accumulate target). So the
unpack co-resides with the MAC's wide write port and inherits its depth — the result lands at **stage 12**,
one cycle later than the ALU's stage-11 / 1-cycle `vec` writes. A dependent MAC that reads the freshly
unpacked `wvt` must respect that 2-cycle DEF. `[HIGH/OBSERVED]`

---

## 2. Roster — the 18 mnemonics

Columns: `mnemonic` · `arity` · `out` · `in` · `acc-width ← src-width` · `fill` · `value leaf` (`xdref`,
where the kernel is shared across siblings) · `[conf]`. The roster is the exact set of distinct
encode-thunk mnemonics (`nm libisa-core.so | rg -o 'Opcode_ivp_…_Slot_f0_s2_mul_encode'`, 18 unique);
arities and operand directions are from the per-slot field accessors
([§3.2](#32-the-f0s2-slot-local-field-map-verified)); fills and leaves from the executed `xdref` bodies
([§4](#4-value-semantics--driven-live)).

### 2.1 The CVT token grammar, decoded

Every `CVT*` mnemonic is `ivp_cvt` + `<accW>` + `<S|U>` + `<src-shape>` + optional `H`/`L`. Decoded from
the roster, the leaf width-signatures and the executed bodies:

| token | meaning | binary evidence |
|---|---|---|
| `24` / `48` / `96` | **destination accumulator-lane width** in bits (`wvec` slot) | leaf `_<24\|48\|96>_…`; `wvec` width `1536 = 3×512` ([§3.4](#34-the-wvec-geometry-verified-in-the-descriptor-table)) |
| `S` | **signed** — fill the accumulator guard bits with a replicate of the source sign bit | `cvt24s_24_16`: `movswl; and $0xffffff` ([§4.2](#42-signed-widen--sign-fill)) |
| `U` | **unsigned** — fill the accumulator guard bits with `0` | `cvt24u_24_16`: bare `mov %esi,(%rdx)` ([§4.1](#41-unsigned-widen--zero-fill)) |
| `2NX16` | source = `32×i16` (the full 512-bit `Nx16` shape) | leaf `_24_16`; vec width 512 / 16 = 32 lanes |
| `NX32` / `32` | source = `16×i32` (`N_2x32`) — `32` (no `NX`) = the single-lane / low form | leaf `_48_32`, `_24_32` |
| `N_2X64` / `64` | source = `8×i64` (a packed i32 pair read as one 64-bit value) | leaf `_96_64`, `_48_…64…` (wide-ptr ABI) |
| `H` / `L` | **half-select** — pick the HIGH / LOW lane half when the source lane count exceeds the (fewer, wider) destination slots | distinct selector CONST per `H`/`L` ([§3.3](#33-the-full-32-bit-selector-templates)) |
| `G` (`CVTG*`) | **gather-convert** — route the source lane through a gather permute *before* the widen | leaves `cvtg48_48_32_16_{h,l}` |

### 2.2 The 16 `CVT*` converts

| # | mnemonic | arity | out | in | acc ← src | fill | `xdref` widen leaf | conf |
|---|---|---|---|---|---|---|---|---|
| 1 | `ivp_cvt24s2nx16` | binary | `wvt:wvec` | `vs,vr:vec` | 24 ← i16 | **sign** | `cvt24s_24_16` `@0x5ba850` | `[HIGH/OBSERVED]` |
| 2 | `ivp_cvt24u2nx16` | binary | `wvt:wvec` | `vs,vr:vec` | 24 ← i16 | **zero** | `cvt24u_24_16` `@0x5ba840` | `[HIGH/OBSERVED]` |
| 3 | `ivp_cvt24u32` | unary | `wvt:wvec` | `vr:vec` | 24 ← i32 (low/trunc) | zero | `cvt24u_24_32` (cvt grid) | `[HIGH/OBSERVED]` |
| 4 | `ivp_cvt24unx32h` | binary | `wvt:wvec` | `vs,vr:vec` | 24 ← i32, **HIGH** half | zero | `cvt24u_24_32` (H-route) | `[HIGH·sel / MED·half]` |
| 5 | `ivp_cvt24unx32l` | binary | `wvt:wvec` | `vs,vr:vec` | 24 ← i32, **LOW** half | zero | `cvt24u_24_32` (L-route) | `[HIGH·sel / MED·half]` |
| 6 | `ivp_cvt48snx32` | binary | `wvt:wvec` | `vs,vr:vec` | 48 ← i32 | **sign** | `cvt48s_48_32` `@0x5ba8e0` | `[HIGH/OBSERVED]` |
| 7 | `ivp_cvt48snx32l` | unary | `wvt:wvec` | `vr:vec` | 48 ← i32, **LOW** half | sign | `cvt48s_48_32l` `@0x5ba8d0` | `[HIGH/OBSERVED]` |
| 8 | `ivp_cvt48u64` | unary | `wvt:wvec` | `vr:vec` | 48 ← i64 | zero | `cvt48u_48_32`/`…64` `@0x5ba8c0` | `[HIGH/OBSERVED]` |
| 9 | `ivp_cvt48unx32` | binary | `wvt:wvec` | `vs,vr:vec` | 48 ← i32 | **zero** | `cvt48u_48_32` `@0x5ba8c0` | `[HIGH/OBSERVED]` |
| 10 | `ivp_cvt48unx32l` | unary | `wvt:wvec` | `vr:vec` | 48 ← i32, **LOW** half | zero | `cvt48u_48_32l` `@0x5ba8b0` | `[HIGH/OBSERVED]` |
| 11 | `ivp_cvt48un_2x64h` | binary | `wvt:wvec` | `vs,vr:vec` | 48 ← i64, **HIGH** half | zero | `cvt48u_…64` (H-route) | `[HIGH·sel / MED·half]` |
| 12 | `ivp_cvt48un_2x64l` | binary | `wvt:wvec` | `vs,vr:vec` | 48 ← i64, **LOW** half | zero | `cvt48u_…64` (L-route) | `[HIGH·sel / MED·half]` |
| 13 | `ivp_cvt96u64` | unary | `wvt:wvec` | `vr:vec` | 96 ← i64 | zero | `cvt96u_96_64` `@0x5ba940` | `[HIGH/OBSERVED]` |
| 14 | `ivp_cvt96un_2x64` | binary | `wvt:wvec` | `vs,vr:vec` | 96 ← i64 | zero | `cvt96u_96_64` `@0x5ba940` | `[HIGH/OBSERVED]` |
| 15 | `ivp_cvtg48n_2x32h` | binary | `wvt:wvec` | `vs,vr:vec` | 48 ← i32 **gather**, HIGH | zero | `cvtg48_48_32_16_h` `@0x855b10` | `[HIGH·kernel / MED·permute]` |
| 16 | `ivp_cvtg48n_2x32l` | binary | `wvt:wvec` | `vs,vr:vec` | 48 ← i32 **gather**, LOW | zero | `cvtg48_48_32_16_l` `@0x855b00` | `[HIGH·kernel / MED·permute]` |

### 2.3 The two moves

| # | mnemonic | arity | out | in | semantics | conf |
|---|---|---|---|---|---|---|
| 17 | `ivp_movww` | wvec-mov | `wvt:wvec` | `wvr:wvec` | **verbatim 1536-bit** wide-acc copy `wvt = wvr` (the *wide NOP*; the `wvec` analogue of `MOVVV`'s `vec`→`vec` copy) | `[HIGH/OBSERVED]` |
| 18 | `ivp_movscfv` | unary | **fp-CSR** (11 fields) | `vr:vec` | **MOVe Scalar-Control-From-Vector** — extract the packed fp control word from a `vr` lane and write the 11 `FCR`/`FSR` fields atomically; **no `wvec` operand** | `[HIGH·effect / MED·bit-slice]` |

> **NOTE — disjoint cover.** All 18 ops carry the operand-field prefix
> `fld_ivp_sem_unpack_wvec_mov_{vr,vs,wvt,wvr}` (`nm libisa-core.so`), which is *distinct* from
> [B20](b20-hp-cvt.md)'s `fld_ivpep_sem_hp_cvt_*` (the fp16↔int converts) and
> [B21](b21-select-shuffle.md)'s `fld_ivp_sem_vec_select_*`. The three groups share no member — B22 is the
> integer **widen-into-accumulator** direction only; the fp converts and the `vec`↔`vec` narrows live
> elsewhere, and the saturating `wvec`→`vec` narrow read-outs are [B10](b10-wvec-pack.md). `[HIGH/OBSERVED]`

---

## 3. Encoding

### 3.1 Placement — 141 `(format, slot)` cells, all in the Mul slot

`nm libisa-core.so | rg -o 'Opcode_ivp_<op>_Slot_([a-z0-9]+)_s2_mul_encode'` gives, per op:

```
cvt24s2nx16   8  {f0,f1,f2,f3,f6,f7,f11,n1}    cvt48un_2x64h  8  {f0,f1,f2,f3,f6,f7,f11,n1}
cvt24u2nx16   8  {f0,f1,f2,f3,f6,f7,f11,n1}    cvt48un_2x64l  8  {f0,f1,f2,f3,f6,f7,f11,n1}
cvt24u32      8  {f0,f1,f2,f3,f6,f7,f11,n1}    cvt96u64       8  {f0,f1,f2,f3,f6,f7,f11,n1}
cvt24unx32h   8  {…}    cvt24unx32l   8  {…}    cvt96un_2x64   8  {…}
cvt48snx32    8  {…}    cvt48snx32l   8  {…}    cvtg48n_2x32h  8  {…}
cvt48u64      8  {…}    cvt48unx32    8  {…}    cvtg48n_2x32l  8  {…}
cvt48unx32l   8  {…}    movww          8  {…}
movscfv        5  {f0,f1,f2,f3,f7}        ← absent from f6/f11/n1 (the rarer/narrow encodings)
```

`17 × 8 + 1 × 5 = 141`. `[HIGH/OBSERVED]`

### 3.2 The F0/S2 slot-local field map (verified)

The per-slot field accessors in `libisa-core.so` are tiny `(x << a) >> b` extractors; reading their bodies
gives the *exact* bit positions (the report's `[..]` ranges are confirmed byte-for-byte):

| field | accessor body (`…_Slot_f0_s2_mul_get` `@addr`) | bits | role |
|---|---|---|---|
| `vr` (IN1, `vec`) | `@0x331680`: `shl $0x1a; shr $0x1b` ⇒ `(x<<26)>>27` | **`[5:1]`** (5-bit, contiguous) | source lane |
| `vs` (IN2, `vec`) | `@0x331850`: `edx=x&1; eax=(x>>7)&0x1e; or` ⇒ `bit0 \| (x>>7)&0x1e` | **`[11:8]++[0]`** (5-bit, MSB-first split) | source lane (binary forms) |
| `wvt` (OUT, `wvec`) | `@0x331b20`: `shl $0x10; shr $0x1e` ⇒ `(x<<16)>>30` | **`[15:14]`** (2-bit select into `wvec[0..3]`) | accumulator dest |
| `wvr` (IN, `wvec`) | `@0x3319e0`: `shl $0xb; shr $0x1e` ⇒ `(x<<11)>>30` | **`[20:19]`** (2-bit select into `wvec[0..3]`) | `MOVWW` source |

The opcode **selector** occupies the remaining bits — for the binary CVTs that is `[27:16] ++ [13:12] ++
[7:6]` (a split CONST); unary CVTs absorb the now-unused `vs` gap (`[11:8]++[0]`) into a wider selector;
`MOVWW` packs `wvr` at `[20:19]` and a `[27:21] ++ [13:0]` CONST; `MOVSCFV` has no `wvec` field at all and
a full `[27:6]` CONST with only `vr` left. The other formats relocate the same fields: **F11/S2** keeps
`wvt` at `[15:14]` (`@0x331b42`: same `shl $0x10; shr $0x1e`); **N1/S2** packs `wvt` into `[3:2]`
(`@0x331c22`: `shl $0x1c; shr $0x1e`). The full per-slot scatter is the
[FLIX decoder](../core/flix-encoding.md)'s domain. `[HIGH/OBSERVED]`

### 3.3 The full 32-bit selector templates

Each encode thunk body is a single `movl $imm32,(%rdi); ret` — `imm32` is the **`WORD0` slot-local
template with every register field zeroed** (`vr=vs=wvt=wvr=0`). The encoder ORs the register selects into
the gaps. Read directly out of `libisa-core.so` (`objdump -d` of each `…_Slot_f0_s2_mul_encode`):

| mnemonic | `WORD0` template | `[27:16]` | `[13:12]` | `[7:6]` |
|---|---|---|---|---|
| `ivp_cvt24s2nx16` | `0x01042000` | `0x104` | `0x2` | `0x0` |
| `ivp_cvt24u2nx16` | `0x010c2000` | `0x10c` | `0x2` | `0x0` |
| `ivp_cvt24u32` | `0x010c3000` | `0x10c` | `0x3` | `0x0` |
| `ivp_cvt24unx32h` | `0x01142000` | `0x114` | `0x2` | `0x0` |
| `ivp_cvt24unx32l` | `0x011c2000` | `0x11c` | `0x2` | `0x0` |
| `ivp_cvt48snx32` | `0x01042040` | `0x104` | `0x2` | `0x1` |
| `ivp_cvt48snx32l` | `0x010c3001` | `0x10c` | `0x3` | `0x0` |
| `ivp_cvt48u64` | `0x010c3040` | `0x10c` | `0x3` | `0x1` |
| `ivp_cvt48unx32` | `0x010c2040` | `0x10c` | `0x2` | `0x1` |
| `ivp_cvt48unx32l` | `0x010c3041` | `0x10c` | `0x3` | `0x1` |
| `ivp_cvt48un_2x64h` | `0x01142040` | `0x114` | `0x2` | `0x1` |
| `ivp_cvt48un_2x64l` | `0x011c2040` | `0x11c` | `0x2` | `0x1` |
| `ivp_cvt96u64` | `0x010c3080` | `0x10c` | `0x3` | `0x2` |
| `ivp_cvt96un_2x64` | `0x01042080` | `0x104` | `0x2` | `0x2` |
| `ivp_cvtg48n_2x32h` | `0x010c2080` | `0x10c` | `0x2` | `0x2` |
| `ivp_cvtg48n_2x32l` | `0x01142080` | `0x114` | `0x2` | `0x2` |
| `ivp_movscfv` | `0x010c3081` | `0x10c` (full `[27:6]`=`0x430c2`) | `0x3` | `0x2` |
| `ivp_movww` | `0x01043100` | `0x104` (`[27:21]`=`0x8`) | `0x3` | `0x0` |

The `[27:16]` quartet `{0x104, 0x10c, 0x114, 0x11c}` is the `{signed/unsigned, H/L, arity}` fan within the
unpack block; the `[13:12]` and `[7:6]` low fragments disambiguate the rest of the family within the shared
`0x10x`/`0x11x` prefix. The full discriminator is the concatenation of all three fragments (so e.g.
`cvt48snx32` and `cvt96un_2x64` share `[27:16]=0x104` but split on `[7:6]` = `1` vs `2`). `[HIGH/OBSERVED]`

> **GOTCHA — `WORD0` ≠ a 12-bit selector.** A prior pass quoted only the `[27:16]` 12-bit fragment as "the
> selector." It is not sufficient to identify the op: `cvt24u2nx16`, `cvt24u32`, `cvt48snx32l`, `cvt48u64`,
> `cvt48unx32`, `cvt48unx32l`, `cvt96u64`, `cvtg48n_2x32h` and `movscfv` **all** carry `[27:16]=0x10c`. The
> low `[13:12]`/`[7:6]` (and, for the moves, the wider `[27:6]`/`[27:21]` CONST) are required to
> disambiguate. The full 32-bit `WORD0` above is the authoritative discriminator. `[HIGH/OBSERVED]`

### 3.4 The `wvec` geometry, verified in the descriptor table

The register-file descriptor table is at `libisa-core.so` `.data.rel.ro` VMA `0x74a800` (file offset
`0x54a800`; the section's `VMA − fileoff = 0x200000` delta confirmed by `readelf -SW`), 56-byte stride,
8 entries. Decoding the width (`+0x18`, `u32`) and count (`+0x1c`, `u32`) fields directly:

| idx | name (`+0x0` → `.rodata`) | width `@+0x18` | count `@+0x1c` | this group uses it as |
|---|---|---|---|---|
| 2 | `vec` | **512** (`0x200`) | **32** | **source** lanes (`vr`, `vs`) — `Nx16`/`N_2x32`/`N_2x64` |
| 5 | `wvec` | **1536** (`0x600`) | **4** | **destination** wide accumulator (`wvt`) / `MOVWW` source (`wvr`) |
| 7 | `gvr` | 512 | 8 | (gather staging — not a B22 operand) |

`1536 = 3 × 512`: the `wvec` slot is **three NX16-wide accumulators stacked**, viewed as `32×24` /
`16×48` / `8×96`-bit accumulator lanes depending on the `CVT` width. The 2-bit `wvt`/`wvr` selects address
exactly the 4 entries. `[HIGH/OBSERVED]`

---

## 4. Value semantics — driven live

The vector op is **N independent invocations** of a scalar `xdref` widen primitive over the lanes (the
lanes are independent — no cross-lane carry except the `G` gather's index read). The primitives are byte-
exact x86 soft-models in `libfiss-base.so`; the
[fiss datapath oracle](../../iss/fiss-datapath-oracle.md) characterises the whole 864-leaf surface. Each
leaf below was **disassembled** *and* **`dlopen`'d via `ctypes` and called** on the hard edges
([VAL-09](../../validation/regfile-bridge-divergence.md)); the executed outputs are quoted.

The widen leaves split into two ABIs (recovered from the bodies):

* **SCALAR-IN** `f(xstate=rdi[unused], value=esi, out=rdx)` — the 16-/32-bit-source widens
  (`cvt24{s,u}_24_16`, `cvt48{s,u}_48_32`): `value` is the source lane, `out` a little-endian word buffer
  for the accumulator slot.
* **WIDE-POINTER-IN** `f(xstate=rdi, src_ptr=rsi, out=rdx/rcx)` — the 64-bit-source widens
  (`cvt96u_96_64`, the `cvtg*` gather leaves): the source lane is read as two/three little-endian words.

### 4.1 Unsigned widen — zero-fill

```c
// IVP_CVT24U2NX16 — i16 -> 24-bit accumulator lane, ZERO-fill.
// xdref leaf module__xdref_cvt24u_24_16 @0x5ba840 :  mov %esi,(%rdx) ; ret
// (the i16 value is placed verbatim; the 24-bit slot's top 8 guard bits stay 0 because
//  the accumulator word was zero before the write and only the low 16 bits are touched)
static void cvt24u_24_16(uint32_t *acc24 /*=rdx*/, int32_t v_esi) {
    *acc24 = (uint32_t)v_esi;          // low 16 meaningful; bits[23:16] = 0  (zero-fill)
}

// IVP_CVT48U64 / IVP_CVT48UNX32 / IVP_CVT48UNX32L / IVP_CVT48UN_2X64{H,L}
// xdref leaf module__xdref_cvt48u_48_32 @0x5ba8c0 :  mov %esi,(%rdx) ; movl $0x0,0x4(%rdx)
static void cvt48u_48_32(uint32_t out[2] /*=rdx*/, int32_t v_esi) {
    out[0] = (uint32_t)v_esi;          // word0 = value
    out[1] = 0u;                       // word1 = 0  (top 16 guard bits of the 48-bit slot zeroed)
}

// IVP_CVT96U64 / IVP_CVT96UN_2X64 — 64-bit source -> 96-bit accumulator, ZERO-fill.
// xdref leaf module__xdref_cvt96u_96_64 @0x5ba940 :
//   mov (%rsi),%eax ; movl $0x0,0x8(%rdx) ; mov %eax,(%rdx) ; mov 0x4(%rsi),%eax ; mov %eax,0x4(%rdx)
static void cvt96u_96_64(uint32_t out[3] /*=rdx*/, const uint32_t src64[2] /*=rsi*/) {
    out[0] = src64[0];                 // low  32 of the i64
    out[1] = src64[1];                 // high 32 of the i64
    out[2] = 0u;                       // top  32 guard word = 0  (96 = 64 + 32 guard)
}
```

**Executed (live `libfiss-base.so`):**
`cvt24u_24_16(0x8000) = 0x008000`, `cvt24u_24_16(0x7fff) = 0x007fff`;
`cvt48u_48_32(0x80000000) = 0x000080000000`;
`cvt96u_96_64(0x12345678cafebabe) = 0x000000001234_5678cafebabe`,
`cvt96u_96_64(0x8000000000000000) = 0x000000008000_000000000000` (top guard word stays `0` even at the
i64 sign-bit — *all* CVT96 variants are unsigned). `[HIGH/OBSERVED — executed]`

### 4.2 Signed widen — sign-fill

```c
// IVP_CVT24S2NX16 — i16 -> 24-bit accumulator lane, SIGN-fill.
// xdref leaf module__xdref_cvt24s_24_16 @0x5ba850 :
//   movswl %si,%esi ; and $0xffffff,%esi ; mov %esi,(%rdx) ; ret
static void cvt24s_24_16(uint32_t *acc24 /*=rdx*/, int32_t v_esi) {
    int32_t s = (int16_t)v_esi;        // movswl : sign-extend the i16 source
    *acc24 = (uint32_t)s & 0xFFFFFFu;  // mask to 24 bits -> bits[23:16] carry the replicated sign
}

// IVP_CVT48SNX32 / IVP_CVT48SNX32L — i32 -> 48-bit accumulator lane, SIGN-fill.
// xdref leaf module__xdref_cvt48s_48_32 @0x5ba8e0 :
//   mov %esi,(%rdx) ; sar $0x1f,%esi ; and $0xffff,%esi ; mov %esi,0x4(%rdx)
static void cvt48s_48_32(uint32_t out[2] /*=rdx*/, int32_t v_esi) {
    out[0] = (uint32_t)v_esi;          // word0 = value
    int32_t fill = v_esi >> 31;        // sar $0x1f : 0x00000000 (v>=0) or 0xFFFFFFFF (v<0)
    out[1] = (uint32_t)fill & 0xFFFFu; // word1 = sign replicate, masked to the 16-bit top guard
}
```

**Executed (live):** `cvt24s_24_16(0x8000) = 0xff8000` (i16-min, sign-fill) vs the unsigned
`0x008000` above; `cvt24s_24_16(0xffff) = 0xffffff` (−1); `cvt48s_48_32(0x80000000) = 0xffff80000000`
(i32-min) vs unsigned `0x000080000000`; `cvt48s_48_32(0x7fffffff) = 0x00007fffffff` (i32-max → zero top).
Signedness is a **distinct decode bit** (the `S`/`U` selector CONST, [§3.3](#33-the-full-32-bit-selector-templates))
— never a runtime mode. `[HIGH/OBSERVED — executed]`

> **QUIRK — `48s` guard is masked to 16 bits, not a full 32-bit sign replicate.** `cvt48s_48_32` writes
> `(sign >> 31) & 0xFFFF` into `word1`, *not* `sign >> 31`. The 48-bit slot's top word is a 16-bit guard
> field; the upper 16 bits of `word1` are *not part of the 48-bit accumulator* and stay whatever they were.
> Read the slot as `(out[0] | (out[1] << 32)) & ((1<<48)-1)` — exactly what the
> [B10 pack readout](b10-wvec-pack.md) does in reverse. Confirmed live:
> `(0x80000000, word1=0x0000ffff)` → `acc48 = 0xffff80000000`. `[HIGH/OBSERVED]`

### 4.3 Half-select and gather-convert

```c
// IVP_CVT24UNX32{H,L} / IVP_CVT48UN_2X64{H,L} — when the source lane is wider than the
// destination slot count, H/L pick which half of the source vector feeds the (fewer, wider)
// accumulator lanes. The widen KERNEL is the same cvt24u/cvt48u above; H/L is a source-lane
// route selected by a distinct selector CONST (0x114 = H, 0x11c = L in [27:16], §3.3).
static void cvt48un_2x64h(uint32_t out[2], const uint32_t *vec_src, int lane) {
    const uint32_t *src64 = &vec_src[2 * pick_half_HIGH(lane)];  // gather the HIGH-half i64 lane
    cvt48u_48_32(out, /*low 32 of*/ src64[0]);                    // then the exact 48-bit zero-widen
}

// IVP_CVTG48N_2X32{H,L} — gather-convert: route the i32 source lane through the gather permute,
// THEN the 48-bit widen. Leaves cvtg48_48_32_16_{h,l} @0x855b10 / @0x855b00.
// cvtg48_48_32_16_h @0x855b10 :  mov %esi,(%rcx) ; mov %edx,0x4(%rcx) ; ret   (the widen store;
//   the gather index read sits in the shared module mux, ahead of the store)
```

The widen kernel for `H`/`L`/`G` is **HIGH/OBSERVED** (it is the same `cvt24u`/`cvt48u` body); the exact
source-lane→accumulator-lane index mapping and the gather permute are **MED/INFERRED** — the
`SEMANTIC STATEMENTS` body is suppressed in this config and the permute lives in the shared module mux
(see the gather-convert geometry deferred to [VAL-11](../../validation/regfile-bridge-divergence.md)).

### 4.4 No saturation, no rounding

Every widen leaf body is a bare `mov` plus the sign/zero fill — **no `cmp`, no clamp, no `+round` bias**.
Overflow is impossible: the destination accumulator slot is strictly wider than the source lane (24 ≥ 16+8,
48 ≥ 32+16, 96 ≥ 64+32). Saturation and round-half-up belong to the *opposite* direction — the `wvec`→`vec`
pack narrows of [B10](b10-wvec-pack.md) (`packvr*` clamps the shift to 32 and saturates to the signed
range). The VAL-09 differential proves the two are *genuinely different leaves*: B22 widen is exact,
B10 `packl` low-truncate **wraps**, B10 `packvr` **saturates**. `[HIGH/OBSERVED]`

### 4.5 The two moves

```c
// IVP_MOVWW — wvec -> wvec verbatim 1536-bit copy (the wide NOP).
// Encoding: wvr at [20:19], wvt at [15:14], CONST WORD0=0x01043100 ([27:21]=0x8). No widen, no saturate.
static void ivp_movww(wvec_t *wvt /*[15:14]*/, const wvec_t *wvr /*[20:19]*/) {
    *wvt = *wvr;        // all 1536 bits, the three stacked accumulators copied as-is
}

// IVP_MOVSCFV — vec -> fp control/status. Extracts the packed fp control word from a vr lane and
// writes the 11 FCR/FSR fields atomically (the reverse of an RUR/RSR-style CSR read).
// NO wvec operand — the one group member with no wide-accumulator destination.
static void ivp_movscfv(fp_csr_t *csr, const vec_t *vr /*vec-select field*/) {
    uint32_t w = extract_control_word(vr);   // packed fp control word from a vec lane
    // the 11 targets (names verified in libcas-core.so):
    csr->RoundMode       = field(w, /*RM*/);
    csr->InexactFlag     = field(w, /*…*/);  csr->InexactEnable   = field(w, /*…*/);
    csr->UnderflowFlag   = field(w, /*…*/);  csr->UnderflowEnable = field(w, /*…*/);
    csr->OverflowFlag    = field(w, /*…*/);  csr->OverflowEnable  = field(w, /*…*/);
    csr->DivZeroFlag     = field(w, /*…*/);  csr->DivZeroEnable   = field(w, /*…*/);
    csr->InvalidFlag     = field(w, /*…*/);  csr->InvalidEnable   = field(w, /*…*/);
}
```

The 11 fp-CSR field names are verified present in `libcas-core.so`
(`RoundMode`, `{Inexact,Underflow,Overflow,DivZero,Invalid}Flag`,
`{Inexact,Underflow,Overflow,DivZero,Invalid}Enable` — `rg -io … | sort -u | wc -l` = 11). The exact
`vec`-lane→field **bit slicing** is owned by the `FCR`/`FSR` batch
([VAL-17 / B24](../../validation/regfile-bridge-divergence.md)) — `[HIGH·effect/OBSERVED · MED·bit-slice]`.

---

## 5. Timing

Per-op `libcas-core.so` ISS stage roster (`F0_F0_S2_Mul_28_IVP_<op>_inst_stage{0..12}` — all 13 stages
present for `CVT24S2NX16`, `CVT48U64`, `MOVSCFV`, `MOVWW`):

| event | operand | stage | meaning |
|---|---|---|---|
| `USE` | `vr` (`vec`) | **10** | source lane read |
| `USE` | `vs` (`vec`) | **10** | second source lane read (binary forms) |
| `USE` | `CPENABLE` | **3** | cp1 enable gate sampled |
| `DEF` | `wvt` (`wvec`) | **12** | wide-accumulator result written |

**Input→result latency = 12 − 10 = 2 cycles** — one stage *deeper* than `vec_alu`'s stage-11 / 1-cycle
result, the extra depth of the 1536-bit wide write port. Staged bypass applies; a dependent
[MAC](b04-mac-integer.md) reading the `wvt` must respect the 2-cycle DEF.

* **`IVP_MOVWW`** — both `wvr` (USE) and `wvt` (DEF) sit on the wide port `@stage 12` (a wide→wide move).
* **`IVP_MOVSCFV`** — `vr` USE `@10`; the 11 fp-CSR DEFs all `@stage 12`.

If cp1 is disabled, `Coprocessor1Exception` fires at the `CPENABLE` sample (stage 3), *before* any datapath
effect — the op is squashed, `state_out = ∅`. `[HIGH/OBSERVED]`

---

## 6. Worked examples — device round-trip

Assembled with `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) and disassembled byte-identically back to the
canonical spelling with `xtensa-elf-objdump`. The flat sequence picks the **F7-format S2 slot** (8-byte
bundle, prefix `02a5…`, low byte `…352f`); `MOVSCFV` (no `wvec` dest) picks the **F11 16-byte** frame
(`0007…`, `…452f`). Bytes are the raw bundle as `objdump` prints them; all 18 round-trip (the remaining 12
share this structure with the [§3.3](#33-the-full-32-bit-selector-templates) selector). `[HIGH/OBSERVED]`

```
{ nop; nop; ivp_cvt24s2nx16   wv0, v1, v2 }   = 02a568018881352f   (i16  -> 24-bit, SIGNED)
{ nop; nop; ivp_cvt24u2nx16   wv0, v1, v2 }   = 02a570000881352f   (i16  -> 24-bit, ZERO)
{ nop; nop; ivp_cvt48snx32    wv0, v1, v2 }   = 02a570018881352f   (i32  -> 48-bit, SIGNED)
{ nop; nop; ivp_cvt48u64      wv0, v5 }       = 02a561090841352f   (i64  -> 48-bit, ZERO, unary)
{ nop; nop; ivp_cvt96un_2x64  wv2, v24, v25 } = 02a57c318858352f   (i64  -> 96-bit, ZERO)
{ nop; nop; ivp_cvtg48n_2x32h wv0, v9, v10 }  = 02a561100889352f   (i32  -> 48-bit GATHER, HIGH)
{ nop; nop; ivp_movww         wv1, wv2 }      = 02a56b010800352f   (wvec -> wvec, the wide NOP)
{ nop; nop; ivp_movscfv       v15; nop }      = 0007193200c2ab8290081fa09504452f   (vec -> fp CSR, F11)
```

### 6.1 `IVP_CVT24S2NX16 wv0, v1, v2` — slot-local reconstruction

The F0/S2 slot-local word, rebuilt from the [§3.2](#32-the-f0s2-slot-local-field-map-verified) field map on
the [§3.3](#33-the-full-32-bit-selector-templates) `WORD0 = 0x01042000` template:

```
wvt = wv0 = 0  ->  [15:14] = 0b00
vs  = v2  = 2  ->  [11:8] = 0b0001 (hi4), bit[0] = 0  (the MSB-first split)
vr  = v1  = 1  ->  [5:1]  = 0b00001
selector hi[27:16] = 0x104, mid[13:12] = 0x2, lo[7:6] = 0x0
F0/S2 slot-local word = 0x01042102    (decode-back: wvt=0, vs=2, vr=1 — exact)
```

The reconstruction `0x01042102` is exact: re-running the verified field extractors on it yields
`wvt=0, vs=2, vr=1, sel[27:16]=0x104`. **Semantics executed:** for `k` in `0..31`,
`wv0.acc24[k] = sext24(v1/v2 i16 lane)` — sign-fill bits `[23:16]` (`vr=v1` IN1, `vs=v2` IN2 per the
iclass). `[HIGH/OBSERVED]`

### 6.2 `IVP_CVT96UN_2X64 wv2, v24, v25` — the widest slot

`bundle = 0x02a57c318858352f`. **Semantics:** `acc96[k] = zeroext(src64[k])` — the widest accumulator slot
(96 = 64 + 32 guard) for the i32×i64 / i64-accumulate MAC chains; the top 32-bit guard word is `0`
([§4.1](#41-unsigned-widen--zero-fill), executed live above). `[HIGH/OBSERVED]`

### 6.3 `IVP_MOVWW wv1, wv2` — the wide NOP

`bundle = 0x02a56b010800352f`. **Semantics:** `wv1 = wv2`, a verbatim 1536-bit copy. `wvr` at `[20:19]`
(F0/S2), the selector `WORD0 = 0x01043100` (`[27:21] = 0x8`). `[HIGH/OBSERVED]`

---

## 7. Validation status

Every value claim on this page is **execution-validated** by the four-oracle differential of
[VAL-09](../../validation/regfile-bridge-divergence.md): the real shipped `libfiss-base.so` was `dlopen`'d
via `ctypes` and its widen leaves (`cvt24s/u_24_16`, `cvt48s/u_48_32`, `cvt96u_96_64`) *called* on the hard
edge corpus, agreeing bit-exact with the SEM-lift, the numpy-native reference and the device FLIX decode
(0 mismatch across the structured + fuzz corpus; the unpack rows alone:
`CVT24S/U 16/16`, `CVT48S/U 14/14`). The hardest edge — **sign-fill vs zero-fill at the source min**
(`CVT24S(0x8000)=0xff8000` vs `CVT24U(0x8000)=0x008000`; `CVT48S(0x80000000)=0xffff80000000` vs
`CVT48U=0x000080000000`) — is the easiest place to mis-specify a widen, and all four references reproduce
it. `[HIGH/OBSERVED — executed]`

| claim | binary anchor | verdict |
|---|---|---|
| 18 members, group `ivp_sem_unpack_wvec_mov` | `nm libisa-core.so` encode-thunk roster (18 unique) | `[HIGH/OBSERVED]` |
| `s2 = Mul` slot, 141 placements (`17×8 + 1×5`) | `…_Slot_<f>_s2_mul_encode` ×141; MOVSCFV ×5 | `[HIGH/OBSERVED]` |
| `wvt[15:14]`, `vs[11:8]++[0]`, `vr[5:1]`, `wvr[20:19]` | field-accessor `(x<<a)>>b` bodies @`0x331680`.. | `[HIGH/OBSERVED]` |
| full 32-bit `WORD0` selector templates | `movl $imm,(%rdi)` in each encode thunk | `[HIGH/OBSERVED]` |
| `wvec = 1536b×4 = 3×512`, `vec = 512b×32` | regfile descriptor `@0x74a800` (file `0x54a800`, Δ`0x200000`) | `[HIGH/OBSERVED]` |
| zero-fill (U) / sign-fill (S) widen | `xdref` bodies + **live ctypes** drive | `[HIGH/OBSERVED — executed]` |
| no saturation / no rounding | leaf bodies = bare `mov` + fill, no `cmp`/clamp | `[HIGH/OBSERVED]` |
| USE@10 / DEF@12 → 2-cycle latency; CPENABLE@3 | `libcas-core` stage roster + `CPENABLE`/`Coprocessor1Exception` syms | `[HIGH/OBSERVED]` |
| 18/18 device round-trip (F7 / F11 frames) | `xtensa-elf-as` → `-objdump` byte-identical | `[HIGH/OBSERVED]` |
| H/L half index map; G gather permute; MOVSCFV bit-slice | suppressed in this config; deferred | `[MED/INFERRED]` |

---

## 8. Cross-references

* [B10 — wvec Pack (the wide→narrow readout)](b10-wvec-pack.md) — the *dual* of this batch: B22 widens
  `vec`→`wvec` exactly; B10 narrows `wvec`→`vec` with shift + round + saturate.
* [B04 — MAC (integer)](b04-mac-integer.md) / [B05 — MAC (mixed)](b05-mac-mixed.md) — the *consumers* of a
  primed `wvec`: the accumulate chain B22 sets up.
* [B21 — Select / Shuffle / Compress](b21-select-shuffle.md) — the preceding committed batch (lane
  crossbar).
* [B23 — Divide](b23-divide.md) — the following batch (`ivp_sem_divide`, the integer divide;
  *not* covered here).
* [Register Files](../core/register-files.md) — the `wvec`/`vec`/`gvr` roster and the accumulator geometry.
* [FLIX Encoding](../core/flix-encoding.md) — the per-format slot scatter for the 8 formats hosting B22.
* [fiss Datapath — the 864-Leaf Value Oracle](../../iss/fiss-datapath-oracle.md) — the `xdref` widen leaves.
* [VAL — Regfile-Bridge / Accumulator-Readout + Divergence Catalog](../../validation/regfile-bridge-divergence.md)
  — the four-oracle execution-validated differential that certifies this batch.
* [Template & Partition](template-and-partition.md) — the 30-batch partition and the per-op template.
