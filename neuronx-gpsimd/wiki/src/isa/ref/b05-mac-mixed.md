# ISA Batch 05 — MAC (mixed-sign / complex / wide-acc)

This is the per-instruction reference for the **mixed-sign, unsigned, and complex** integer
multiply-accumulate family of the Vision-Q7 *Cairo* (`ncore2gp`) ISA — the **complement of
[Batch 04](b04-mac-integer.md)**, which owns the *signed × signed* half. B05 covers the
**123 shipped integer-MAC mnemonics** that read at least one **unsigned** operand
(`mulus*` = unsigned×signed, `mulsu*` = signed×unsigned, `muluu*` = unsigned×unsigned) or operate
on **packed complex** lanes (`*c` complex, `*j` conjugate). Like B04 they read multiplicands out of
the 512-bit [`vec` file](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster),
run them through the *same* Booth/CSA Mul-slot array, and reduce into the 1536-bit
[`wvec` wide accumulator](../core/register-files.md#per-file-detail). The *only* hardware difference
from B04 is the **sign-extension mux** on the partial-product unit — proven below, byte-for-byte and
by live execution. Every mnemonic's FLIX slot, opcode-selector template, operand widths and value
semantics are read directly out of the shipped binaries (`libisa-core.so` encode thunks,
`libfiss-base.so` `xdref` value leaves driven live by ctypes, `libcas-core.so` pipeline tags) and
tagged.

This page inherits the certified-perfect denominator from
[the coverage tally](../core/coverage-tally.md): the `1534 / 12569` shipped mnemonic/placement cover
and the `864/864` value-leaf cover. Counts are grounded with `nm | rg -c` against the binary
`.symtab`, never a decompile grep; the `extracted/` tree is gitignored (reach it with `fd --no-ignore`
or an absolute path). Confidence tags follow
[the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte / immediate /
symbol / **executed** value read from the shipped binary; `INFERRED` = reasoned over OBSERVED;
`CARRIED` = re-used at a cited page's confidence; crossed with `HIGH`/`MED`/`LOW`. All prose is
binary / static-analysis derived only.

> **Scope in one line.** B05 = `mulus*`/`mulsu*` (mixed-sign), `muluu*` (unsigned), and `*nx16c` /
> `*nx16j` / `*_2x32c` (complex / conjugate) integer MAC, in lane widths **8 / 16 / 32 bit**,
> accumulating into **24 / 48 / 96-bit** `wvec` lanes — the activation×weight inner loop where a
> `uint8` activation meets an `int8` weight, and the complex-FFT butterfly. **123 mnemonics, 654
> placements.** The fp16/fp32 multiply-add (`*xf16`/`*xf32`/`*sone`) is **not** here — it is
> [B17](b17-spfma.md)/[B18](b18-hp-fma.md) (see [§9](#9-the-b04--b05--fp-partition-boundary)).
> `[HIGH/OBSERVED]`

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| B05 mnemonics (mixed/unsigned/complex int MAC) | **123** | classifier over `nm libisa-core.so` `Opcode_*` roster ([§9](#9-the-b04--b05--fp-partition-boundary)) |
| — `mulus*` (unsigned × signed) | **54** | `nm \| rg -o 'Opcode_ivp_mulus…'` distinct |
| — `mulsu*` (signed × unsigned) | **21** | same, `mulsu` root |
| — `muluu*` (unsigned × unsigned) | **37** | same, `muluu` root |
| — `*c` / `*j` (complex / conjugate) | **11** | `nx16c`/`nx16j`/`2x32c` lane suffix |
| B05 placements (`mnemonic × slot`) | **654** | summed `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` over the 123 |
| Issue slot | **`s2 = Mul`** of every wide format + `N1` (iclass `Mul_28`) | `Opcode_*_Slot_<f>_s2_mul_encode`; `libcas` `F0_F0_S2_Mul_28_*` stages |
| Accumulate target | **`wvec`** (idx 5, 1536-bit, 4 entries) | [register-files §3](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster) |
| Multiplicand source | **`vec`** (idx 2, 512-bit, 32 entries) | same |
| Lane → product widths | 8→24, 16→48, 32→96 (3× headroom, identical to B04) | `xdref` leaf width-signature ([§4](#4-the-mixed-sign--unsigned-leaf-bodies)) |
| Sign mux | **`sign_unsign` / `su` partial-product mux on the shared array** | `module_ivp_sign_unsign_xtmulpp_16_8_stage0`, `module_ivp_su_xtmulpp_8x8_stage0`, `module_ivp_mul_su_8_8x8_2_16x16_stage0` (`libcas-core.so`) |
| MAC pipeline depth | inputs `@10` → result `@12` = **2-cycle latency, II = 1** (same as B04) | `F0_F0_S2_Mul_28_IVP_MULUSANX16_inst_stage{10,11,12}` |
| Unsigned acc high-word | **explicitly zeroed** on `muluu` 16/32 (no sign-fill) | executed/decoded: `muluu_48_16_16` (`movl $0x0,0x4(%rcx)`) |
| Complex packing | `32c` = `{int16 re=low16, int16 im=high16}`; out = `96c` = 2×48-bit `{re48,im48}` | `mul_96c_32c_32c` leaf body ([§5](#5-complex--conjugate-multiply--driven-live)) |
| Conjugate sign | `j` flips the **imaginary** partial: real = `re·re + im·im` (add, not sub) | `mul_96c_32j_32c` uses `add` where `mul_96c_32c_32c` uses `sub` |
| Single-replicate quad constraint | `mul{us,su,uu}q*xr8`/`*xr16` are **F4-only** (dual-load) | `Slot_f4_s2_mul_encode` is the lone placement |

The mixed-sign family exists for one production reason: **`uint8` activations against `int8`
weights**. A quantized network stores activations as unsigned (post-ReLU, post-zero-point) and weights
as signed; the dot-product engine must therefore multiply `unsigned × signed` per lane and accumulate
signed — exactly what `mulus*`/`mulsu*` deliver. `muluu*` covers the all-unsigned correlation/histogram
case; the complex `*c`/`*j` forms are the FFT/beamforming butterfly. Everything reduces into the same
`wvec` accumulator as B04. `[HIGH/OBSERVED]`

---

## 2. Roster — the 123 mixed/unsigned/complex MAC mnemonics

Columns: `mnemonic` · `FLIX format·slot` (all sit in the **Mul** slot `s2`, iclass `Mul_28`) ·
`opcode-sel imm` (the `F0_S2_Mul` encode-thunk template `WORD0`, the
[universal `C7 07 imm32 C3` ABI](../core/flix-encoding.md#61-the-universal-encode-thunk-abi); per-format
packing differs) · `vec→wvec` (lane-bits → product/acc-bits) · `bytes` (16 wide / 8 narrow) ·
semantics · `[conf]`. Templates are byte-exact from `objdump -d` this pass; widths from the joined
[`xdref` leaf](#6-the-mnemonic--value-leaf-join-by-width-and-sign-token).

### 2.1 `mulus*` — unsigned × signed (the `uint8`-activation MAC, 54 mnemonics)

`mulus` reads operand **`a` UNSIGNED**, operand **`b` SIGNED** (proven by execution, [§4.1](#41-the-sign-asymmetry-proven-by-execution)).
This is the dominant quantized-inference form: unsigned activation × signed weight.

| mnemonic | fmt·slot | opcode-sel imm (F0·s2) | vec→wvec | bytes | semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_mulus2nx8` | F0/F1/F2/F3/F6/F7/F11/N1 · s2_mul | `0x00c6b080` | 8 → 24 | 16/8 | `uint8 a × int8 b` → 24-bit, **overwrite** | `[HIGH/OBSERVED]` |
| `ivp_mulusa2nx8` | (same 8 slots) | `0x00c6b0c0` | 8 → 24 | 16/8 | `uint8 × int8`, **acc += prod** | `[HIGH/OBSERVED]` |
| `ivp_muluss2nx8` | (same 8 slots) | — | 8 → 24 | 16/8 | `uint8 × int8`, **acc −= prod** (mul-sub) | `[HIGH/OBSERVED]` |
| `ivp_mulusnx16` | (same 8 slots) | `0x00c73080` | 16 → 48 | 16/8 | `uint16 × int16` → 48-bit, overwrite | `[HIGH/OBSERVED]` |
| `ivp_mulusanx16` | (same 8 slots) | `0x00c6f040` | 16 → 48 | 16/8 | `uint16 × int16`, acc += | `[HIGH/OBSERVED]` |
| `ivp_mulussnx16` | (same 8 slots) | — | 16 → 48 | 16/8 | `uint16 × int16`, acc −= | `[HIGH/OBSERVED]` |
| `ivp_mulusan_2x32` / `ivp_mulusn_2x32` | (same 8 slots) | — | 32 → 96 | 16/8 | `uint32 × int32`, acc += / overwrite | `[HIGH/OBSERVED]` |
| `ivp_mulussn_2x32` | (same 8 slots) | — | 32 → 96 | 16/8 | `uint32 × int32`, acc −= | `[HIGH/OBSERVED]` |
| `ivp_mulusn_2x16x32_{0,1}` / `ivp_mulusan_2x16x32_{0,1}` | (same 8 slots) | — | 16×32 → 96 (even/odd) | 16/8 | `uint16 × int32` widen, even/odd lane, ovr / acc+= | `[HIGH/OBSERVED]` |
| `ivp_mulushn_2x16x32_1` / `ivp_mulusahn_2x16x32_1` / `ivp_mulusshn_2x16x32_1` | (same 8 slots) | — | 16×32 → 96 (high) | 16/8 | high-half `uint16 × int32` widen, ovr / += / −= | `[MED/OBSERVED]` |
| `ivp_mulusp2nx8` / `ivp_muluspa2nx8` / `ivp_mulusps2nx8` | (same 8 slots) | — | 8 → 24 | 16/8 | **packed** `uint8 × int8` mul / mul-add / mul-sub (`mulusp_24_8_8_8_8`) | `[HIGH/OBSERVED]` |
| `ivp_muluspd2nx8` / `ivp_muluspda2nx8` | (same 8 slots) | — | 8 → 24 | 16/8 | packed-**dual** `uint8 × int8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluspnx16` / `ivp_muluspanx16` / `ivp_muluspsnx16` | (same 8 slots) | — | 16 → 48 | 16/8 | packed `uint16 × int16` mul / += / −= | `[HIGH/OBSERVED]` |
| `ivp_muluspdnx16` / `ivp_muluspdanx16` | (same 8 slots) | — | 16 → 48 | 16/8 | packed-dual `uint16 × int16` | `[HIGH/OBSERVED]` |
| `ivp_mulus2n8xr16` / `ivp_mulusa2n8xr16` | (same 8 slots) | — | 8×16 → 48 | 16/8 | `uint8 × int16-replicate` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulusp2n8xr16` / `ivp_muluspa2n8xr16` | (same 8 slots) | — | 8 → 24 | 16/8 | packed `uint8 × int16-replicate` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluspan16xr16` / `ivp_muluspn16xr16` | F0/F1/F2/F3/F6/F7/F11 · s2 | — | 16 → 48 | 16/8 | packed `uint16 × int16-replicate` += / mul (**7 slots**, no N1) | `[HIGH/OBSERVED]` |
| `ivp_mulusi2nx8x16` / `ivp_mulusai2nx8x16` | (same 8 slots) | — | 8×16 → 48 | 16/8 | `uint8 × int16-immediate` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulusi2nr8x16` / `ivp_mulusai2nr8x16` | (same 8 slots) | — | 8×16 → 48 | 16/8 | `uint8 × int16-immediate-replicate` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluspi2nr8x16` / `ivp_muluspai2nr8x16` | (same 8 slots) | — | 8×16 | 16/8 | packed `uint8 × int16-immediate-replicate` mul / mul-add | `[MED/OBSERVED]` |
| `ivp_mulus4t2n8xr8` / `ivp_mulus4ta2n8xr8` | 8 slots · s2 | — | 8 → 24 | 16/8 | 4-tap transpose `uint8 × int8-r8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulus4tn16xr16` / `ivp_mulus4tan16xr16` | 8 slots · s2 | — | 16 → 48 | 16/8 | 4-tap transpose `uint16 × int16-r16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulusq2n8xr8` / `ivp_mulusqa2n8xr8` | **F4·s2 only** | — | 8 → 24 | 16 | single-replicate quad `uint8 × int8-r8` mul / mul-add (dual-load) | `[HIGH/OBSERVED]` |
| `ivp_mulusqn16xr16` / `ivp_mulusqan16xr16` | **F4·s2 only** | — | 16 → 48 | 16 | single-replicate quad `uint16 × int16-r16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulusq2n8dxr8` / `ivp_mulusq2n8qxr8` … (d/q fanout, 6) | F0/F2/F7/N1 · s2 | — | 8 → 24 | 16/8 | double/quad-fanout replicate quad `uint8 × int8` | `[HIGH/OBSERVED]` |
| `ivp_mulusqn16dxr16` / `ivp_mulusqan16dxr16` | F0/F2/F7/N1 · s2 | — | 16 → 48 | 16/8 | double-fanout quad `uint16 × int16` mul / mul-add | `[HIGH/OBSERVED]` |

### 2.2 `mulsu*` — signed × unsigned (the mirror, 21 mnemonics)

`mulsu` reads operand **`a` SIGNED**, operand **`b` UNSIGNED** — the exact mirror of `mulus`.

> **QUIRK — `mulsu` has NO symmetric narrow full-vector form; it appears ONLY where operand order is
> fixed by widening or replication.** There is **no** `ivp_mulsu2nx8` or `ivp_mulsunx16` in the roster
> (`nm | rg 'Opcode_ivp_mulsu2nx8'` = 0). For the *symmetric* 8-bit/16-bit full-vector case,
> `mulus(a,b)` and `mulsu(a,b)` are the same operation with the operands swapped, so the ISA ships only
> **one** ordering (`mulus`). `mulsu` materializes only in the **`_2x16x32` widening** (`int16 × uint32`
> — the two widths cannot be swapped), the **`q*xr8`/`q*xr16` quad** (the replicated weight is
> position-fixed), and the **packed `pan16xr16`** forms — exactly the cases where the two operands have
> *distinct roles* and cannot be commuted. This is why `mulsu` has only **21** members against `mulus`'s
> **54**. `[HIGH/OBSERVED]`

| mnemonic | fmt·slot | vec→wvec | semantics | conf |
|---|---|---|---|---|
| `ivp_mulsun_2x16x32_{0,1}` / `ivp_mulsuan_2x16x32_{0,1}` / `ivp_mulsusn_2x16x32_{0,1}` | 8 slots · s2 | 16×32 → 96 (even/odd) | `int16 × uint32` widen, ovr / += / −= | `[HIGH/OBSERVED]` |
| `ivp_mulsuhn_2x16x32_1` / `ivp_mulsuahn_2x16x32_1` / `ivp_mulsushn_2x16x32_1` | 8 slots · s2 | 16×32 → 96 (high) | high-half `int16 × uint32` widen, ovr / += / −= | `[MED/OBSERVED]` |
| `ivp_mulsupn16xr16` / `ivp_mulsupan16xr16` | 7 slots · s2 (no N1) | 16 → 48 | packed `int16 × uint16-replicate` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulsuq2n8xr8` / `ivp_mulsuqa2n8xr8` | **F4·s2 only** | 8 → 24 | single-replicate quad `int8 × uint8-r8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulsuqn16xr16` / `ivp_mulsuqan16xr16` | **F4·s2 only** | 16 → 48 | single-replicate quad `int16 × uint16-r16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulsuq2n8dxr8` / `ivp_mulsuq2n8qxr8` / `ivp_mulsuqa2n8dxr8` / `ivp_mulsuqa2n8qxr8` | F0/F2/F7/N1 · s2 | 8 → 24 | double/quad-fanout quad `int8 × uint8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulsuqn16dxr16` / `ivp_mulsuqan16dxr16` | F0/F2/F7/N1 · s2 | 16 → 48 | double-fanout quad `int16 × uint16` mul / mul-add | `[HIGH/OBSERVED]` |

### 2.3 `muluu*` — unsigned × unsigned (37 mnemonics)

Both operands unsigned — correlation, histogram bin-weighting, and unsigned GEMM. The **unsigned
accumulate zero-fills the upper acc word** (no sign extension) — a real reimplementation distinction
([§4.2](#42-unsigned-accumulate-the-upper-word-is-zeroed-executed)).

| mnemonic | fmt·slot | opcode-sel imm (F0·s2) | vec→wvec | semantics | conf |
|---|---|---|---|---|---|
| `ivp_muluu2nx8` | 8 slots · s2 | `0x00c7f000` | 8 → 24 | `uint8 × uint8` → 24-bit, overwrite | `[HIGH/OBSERVED]` |
| `ivp_muluua2nx8` | 8 slots · s2 | `0x00c7f040` | 8 → 24 | `uint8 × uint8`, acc += | `[HIGH/OBSERVED]` |
| `ivp_muluus2nx8` | 8 slots · s2 | — | 8 → 24 | `uint8 × uint8`, acc −= | `[HIGH/OBSERVED]` |
| `ivp_muluunx16` | 8 slots · s2 | `0x010070c0` | 16 → 48 | `uint16 × uint16` → 48-bit, overwrite | `[HIGH/OBSERVED]` |
| `ivp_muluuanx16` | 8 slots · s2 | `0x00c7f0c0` | 16 → 48 | `uint16 × uint16`, acc += | `[HIGH/OBSERVED]` |
| `ivp_muluun_2x16x32_{0,1}` / `ivp_muluuan_2x16x32_{0,1}` / `ivp_muluusn_2x16x32_{0,1}` | 8 slots · s2 | — | 16×32 → 96 | `uint16 × uint32` widen even/odd, ovr / += / −= | `[HIGH/OBSERVED]` |
| `ivp_muluuhn_2x16x32_1` / `ivp_muluuahn_2x16x32_1` / `ivp_muluushn_2x16x32_1` | 8 slots · s2 | — | 16×32 → 96 (high) | high-half `uint16 × uint32` widen | `[MED/OBSERVED]` |
| `ivp_muluup2nx8` / `ivp_muluupa2nx8` / `ivp_muluups2nx8` | 8 slots · s2 | — | 8 → 24 | packed `uint8 × uint8` mul / += / −= (`muluup_24_8_8_8_8`) | `[HIGH/OBSERVED]` |
| `ivp_muluupd2nx8` / `ivp_muluupda2nx8` | 8 slots · s2 | — | 8 → 24 | packed-dual `uint8 × uint8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluupnx16` / `ivp_muluupanx16` / `ivp_muluupsnx16` | 8 slots · s2 | — | 16 → 48 | packed `uint16 × uint16` mul / += / −= | `[HIGH/OBSERVED]` |
| `ivp_muluupdnx16` / `ivp_muluupdanx16` | 8 slots · s2 | — | 16 → 48 | packed-dual `uint16 × uint16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluupn16xr16` / `ivp_muluupan16xr16` | 7 slots · s2 | — | 16 → 48 | packed `uint16 × uint16-replicate` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluuq2n8xr8` / `ivp_muluuqa2n8xr8` | **F4·s2 only** | — | 8 → 24 | single-replicate quad `uint8 × uint8-r8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluuqn16xr16` / `ivp_muluuqan16xr16` | **F4·s2 only** | — | 16 → 48 | single-replicate quad `uint16 × uint16-r16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluuq2n8dxr8` / `ivp_muluuq2n8qxr8` / `ivp_muluuqa2n8dxr8` / `ivp_muluuqa2n8qxr8` | F0/F2/F7/N1 · s2 | — | 8 → 24 | double/quad-fanout quad `uint8 × uint8` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_muluuqn16dxr16` / `ivp_muluuqan16dxr16` | F0/F2/F7/N1 · s2 | — | 16 → 48 | double-fanout quad `uint16 × uint16` mul / mul-add | `[HIGH/OBSERVED]` |

### 2.4 Complex / conjugate (`*c`, `*j`, 11 mnemonics)

The complex family treats each lane as a packed complex pair and produces a complex product. `c` =
straight complex multiply; `j` = multiply by the **conjugate** of the second operand. Output is a
double-width complex `{re, im}`.

| mnemonic | fmt·slot | opcode-sel imm (F0·s2) | lane | semantics | conf |
|---|---|---|---|---|---|
| `ivp_mulnx16c` | 8 slots · s2 | `0x01007040` | `2x32c`→`96c` | complex `int16` mul: out = (re·re−im·im, re·im+im·re), overwrite | `[HIGH/OBSERVED]` |
| `ivp_mulnx16j` | 8 slots · s2 | `0x0100b040` | `2x32c`→`96c` | **conjugate** `int16` mul: out = (re·re+im·im, im·re−re·im) | `[HIGH/OBSERVED]` |
| `ivp_mulanx16c` | 8 slots · s2 | `0x00c5b000` | `2x32c`→`96c` | complex `int16`, acc += | `[HIGH/OBSERVED]` |
| `ivp_mulanx16j` | 8 slots · s2 | `0x00c5b040` | `2x32c`→`96c` | conjugate `int16`, acc += | `[HIGH/OBSERVED]` |
| `ivp_mulsnx16c` / `ivp_mulsnx16j` | 8 slots · s2 | — | `2x32c`→`96c` | complex / conjugate `int16`, acc −= | `[HIGH/OBSERVED]` |
| `ivp_muln_2x32c_0` / `ivp_mulan_2x32c_{0,1}` | 8 slots · s2 | — | `64c`→`96` | complex `int32`, real/imag lane select `_0`/`_1`, ovr / acc += | `[HIGH/OBSERVED]` |
| `ivp_mulsn_2x32c_{0,1}` | 8 slots · s2 | — | `64c`→`96` | complex `int32`, lane select, acc −= | `[HIGH/OBSERVED]` |

> **NOTE — the 16-bit complex mnemonics (`*nx16c`/`*nx16j`) join to the *32-bit* complex value leaf.**
> A `2x32c` lane is a **32-bit word holding `{int16 re, int16 im}`**, so the value semantics live in
> `module__xdref_mul_96c_32c_32c` (input is the packed-32 complex; output is the `96c` = 2×48-bit
> complex). The `_2x32c_{0,1}` 32-bit-element forms join to the wider `mul_96_64c_64c_c_{0,1}` leaves
> (a `64c` = packed-64 complex, `{int32 re, int32 im}`, with the `_0`/`_1` selecting the real or
> imaginary output half). The complex value-leaf set is exactly **11** distinct bodies — see
> [§5](#5-complex--conjugate-multiply--driven-live). `[HIGH/OBSERVED]`

---

## 3. Encoding — where mixed/complex MAC lives in the FLIX grid

### 3.1 Same Mul slot (`s2`), same iclass (`Mul_28`) as B04

Every B05 full-vector op resolves to the **identical 8 placements** as the signed B04 family — one per
`s2_mul` slot of the seven wide formats plus narrow `N1`:

```
nm libisa-core.so | rg 'Opcode_ivp_mulusanx16_Slot_.*_encode' | rg -o 'Slot_[a-z0-9_]+'
  →  Slot_f0_s2_mul  Slot_f1_s2_mul  Slot_f2_s2_mul  Slot_f3_s2_mul
     Slot_f6_s2_mul  Slot_f7_s2_mul  Slot_f11_s2_mul  Slot_n1_s2_mul     (8)
```

The `libcas-core.so` pipeline tags confirm the **shared iclass**: `MULUSANX16` issues as
`F0_F0_S2_Mul_28_IVP_MULUSANX16_inst_stage{0..13}` — the **same `Mul_28`** slot-class index the signed
`mul*` ops use ([flix §5.1](../core/flix-encoding.md#51-functional-unit-vocabulary)). So a `uint8`×`int8`
MAC co-issues with a load/store (s0), a load (s1) and 1–3 ALU ops (s3+) in the same 16-byte wide bundle,
exactly like B04. The mixed/unsigned/complex MAC is **not** a separate functional unit — it is the same
multiply array with a different sign mux. `[HIGH/OBSERVED]`

The placement total over the 123 B05 mnemonics is **654** (summed `nm | rg -c` per mnemonic): `mulus`
**280**, `mulsu` **114**, `muluu` **172**, complex **88**. With most full-vector forms carrying the
canonical 8 placements, the packed-replicate `pan16xr16` forms carrying 7 (no N1), and the F4-only quad
forms carrying 1, `654` is B05's contribution to the certified `12569` placement cover
([coverage-tally §1](../core/coverage-tally.md#1-the-five-headline-numbers--re-derived-from-the-binary-this-pass)).
`[HIGH/OBSERVED]`

### 3.2 The opcode-selector is a distinct word per (op, sign, direction) — never a global bit

Reading the `F0_S2_Mul` templates byte-exact (`objdump -d` this pass), the sign and accumulate-direction
are **distinct opcodes**, not OR-able bits — the same two-tier selector rule B04 documents:

```
ivp_mulus2nx8    WORD0 = 0x00c6b080     ivp_muluu2nx8   WORD0 = 0x00c7f000
ivp_mulusa2nx8   WORD0 = 0x00c6b0c0     ivp_muluua2nx8  WORD0 = 0x00c7f040    (+0x40 here…)
ivp_mulusnx16    WORD0 = 0x00c73080     ivp_muluunx16   WORD0 = 0x010070c0
ivp_mulusanx16   WORD0 = 0x00c6f040     ivp_muluuanx16  WORD0 = 0x00c7f0c0
ivp_mulnx16c     WORD0 = 0x01007040     ivp_mulnx16j    WORD0 = 0x0100b040    (c→j: +0x4000)
ivp_mulanx16c    WORD0 = 0x00c5b000     ivp_mulanx16j   WORD0 = 0x00c5b040    (c→j: +0x40)
```

The `mulus`→`muluu` sign change (`0x00c6b080`→`0x00c7f000`) and the `nx16`-width accumulate
(`mulusnx16 0x00c73080`→`mulusanx16 0x00c6f040`) are **wholly different selector words living in
different iclass rows**, not a `+0x40` bit — and even where the 8-bit pair *looks* like `+0x40`
(`mulus2nx8`→`mulusa2nx8`), the 16-bit and complex pairs disprove a global bit. A reimplementer's
assembler must carry the full `(mnemonic, slot) → template` table; it cannot synthesize the unsigned or
conjugate variant from the signed by OR-ing a constant. This reproduces
[the two-tier selector CORRECTION](../core/flix-encoding.md#62-the-two-tier-selector-model). `WORD1`
is `0x00000000` on every placement (the upper lane carries no selector — it is merely cleared), the
[universal encode-thunk invariant](template-and-partition.md#3-the-canonical-per-instruction-page-template-the-b01b30-schema).
`[HIGH/OBSERVED]`

---

## 4. The mixed-sign / unsigned leaf bodies

`libfiss-base.so` is **callable in-process via ctypes with no license**
([coverage-tally §5](../core/coverage-tally.md#5-value-semantics-coverage--the-864864-execution-validated-cover)),
so all semantics below are **proven-by-execution**, not decoded-and-guessed. The leaf SysV ABI
(recovered from `objdump -d`) is `void leaf(void* ctx, <value operands…>, <out-ptr>)` — `ctx`/lane in
`%rdi`, value operands in `%rsi`/`%rdx`/`%rcx`, the last pointer is the output. The accumulate forms
take the running accumulator as the first value operand and an extra out-pointer.

### 4.1 The sign asymmetry, proven by execution

The whole batch turns on **which operand is sign-extended**. The 8-bit leaves, disassembled this pass:

```asm
module__xdref_mul_24_8_8   (B04, signed×signed):     module__xdref_muluu_24_8_8 (unsigned×unsigned):
  movsbl %sil,%esi   ; a = sext8(a)                    imul  %esi,%edx     ; product, NO extension
  movsbl %dl,%edx    ; b = sext8(b)                    and   $0xffff,%edx  ; (16-bit raw product)
  imul   %esi,%edx                                     mov   %edx,(%rcx)
  and    $0xffffff,%edx

module__xdref_mulsu_24_8_8 (signed×unsigned):        module__xdref_mulus_24_8_8 (unsigned×signed):
  shl    $0x17,%edx  ; b: 9-bit field (UNSIGNED)        shl   $0x17,%esi   ; a: 9-bit field (UNSIGNED)
  movsbl %sil,%esi   ; a = sext8(a)  (SIGNED)           movsbl %dl,%edx    ; b = sext8(b)  (SIGNED)
  sar    $0x17,%edx  ;   …zero-passes a valid u8        sar   $0x17,%esi
  imul   %edx,%esi                                      imul  %esi,%edx
  and    $0xffffff,%esi                                 and   $0xffffff,%edx
```

The discriminating idiom is **`movsbl` (8-bit arithmetic sign-extend) for the SIGNED operand** versus
**`shl $0x17; sar $0x17` (a 9-bit-field sign-extend) for the UNSIGNED operand**. For a valid 8-bit
unsigned input (bits 0..7), bit 8 is always 0, so the `shl/sar 0x17` pair **zero-passes** — it is the
unsigned path. (At 16-bit the constant becomes `0xf` = a 17-bit field via `shl $0xf; sar $0xf`, same
trick one byte wider, paired with `movswq` for the signed halfword.) Driven live via ctypes:

```python
import ctypes
lib = ctypes.CDLL("libfiss-base.so")
def mk3(name):                                  # void f(ctx, a, b, *out24)
    f = getattr(lib, name); f.restype = None
    f.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    def call(a, b):
        out = ctypes.c_int(0); f(0, a, b, ctypes.byref(out)); return out.value & 0xFFFFFF
    return call
mul, mulsu, mulus, muluu = (mk3("module__xdref_" + n) for n in
    ("mul_24_8_8", "mulsu_24_8_8", "mulus_24_8_8", "muluu_24_8_8"))
```

Sweep (OBSERVED-by-execution; 24-bit two's-complement interpretation), with the **`a=0xFF, b=0xFF`
discriminator** that separates all four datapaths:

```
a=0xFF, b=0x02 :  mul(ss) = -2     mulsu(s·u) = -2     mulus(u·s) = +510   muluu(uu) = 510
a=0x02, b=0xFF :  mul(ss) = -2     mulsu(s·u) = +510   mulus(u·s) = -2     muluu(uu) = 510
a=0xFF, b=0xFF :  mul(ss) = +1     mulsu(s·u) = -255   mulus(u·s) = -255   muluu(uu) = 65025
a=0x80, b=0x02 :  mul(ss) = -256   mulsu(s·u) = -256   mulus(u·s) = +256   muluu(uu) = 256
```

The `a=0xFF, b=0xFF` row is conclusive: `mul = (−1)(−1) = +1`; `mulsu = (−1)(255) = −255` (**a signed,
b unsigned**); `mulus = (255)(−1) = −255` (**a unsigned, b signed**); `muluu = (255)(255) = 65025`. Four
distinct results from one input pair — the sign mux is **OBSERVED-by-execution**, not inferred. A full
65,536-pair differential of each leaf against its decoded sign model returned **0 mismatches**.
`[HIGH/OBSERVED by execution]`

> **GOTCHA — `mulus` is `a-unsigned × b-signed`; `mulsu` is `a-signed × b-unsigned`. Read the token
> left-to-right and bind it to the operand order.** The mnemonic spells the operand signs **in order**:
> `mul` + `us` = (`u`)nsigned `a`, (`s`)igned `b`; `mul` + `su` = (`s`)igned `a`, (`u`)nsigned `b`. For
> the quantized-inference case (unsigned activation `a`, signed weight `b`) the correct op is
> **`mulus`** — and that is precisely why `mulus` has 54 members (the common case) while `mulsu` has
> only 21 (the widening/replicated cases where the order is forced). Getting the order backwards
> silently flips the sign of every product where exactly one operand is negative. `[HIGH/OBSERVED]`

### 4.2 Unsigned accumulate — the upper word is zeroed (executed)

The 16-bit unsigned leaf `muluu_48_16_16` shows a structural difference from the signed/mixed forms:

```asm
module__xdref_muluu_48_16_16:
  imul  %esi,%edx           ; 32-bit unsigned product (both operands pass straight in)
  movl  $0x0,0x4(%rcx)      ; out48.hi = 0           ← upper word ZEROED, not sign-filled
  mov   %edx,(%rcx)         ; out48.lo = product
```

A `uint16 × uint16` product fits in 32 bits, so the high word of the 48-bit lane is **explicitly set to
0** — there is no sign to extend. The signed and mixed 16-bit leaves instead sign-extend the 32-bit
product into the 48-bit lane (`shl $0x1f; sar $0x1f; and $0xffff` on the upper word, visible in
`mulus_48_16_16`). A reimplementer must zero-fill, **not** sign-fill, the unsigned accumulate's upper
bits. Driving `muluua_24_24_8_8` live confirms the modular accumulate wraps (it does **not** saturate):

```
muluua  acc += a*b  (both uint8, fresh acc = 0):
   acc += 255*255 = 65025  -> 0x00fe01 =  65025
   acc += 255*255 = 65025  -> 0x01fc02 = 130050
overflow:  acc = 0xFFFF00 (=16776960)
   acc += 255*255 = 65025  -> 0x00fd01 = 64769   (math 16841985 wraps mod 2^24 -> 64769)   ✓
```

The accumulate is modular `acc = (acc ± a·b) mod 2^(3L)`, no clamp — identical wrap law to B04's signed
accumulate, only the operand sign-extension differs. `[HIGH/OBSERVED by execution]`

---

## 5. Complex / conjugate multiply — driven LIVE

The complex leaf treats a 32-bit lane as a packed complex `{int16 re = low16, int16 im = high16}` and
produces a `96c` complex product (2 × 48-bit `{re, im}`). The `mul_96c_32c_32c` body, disassembled this
pass, lays the algorithm bare:

```asm
module__xdref_mul_96c_32c_32c (complex×complex):     module__xdref_mul_96c_32j_32c (×conjugate):
  movswl %si,%r9d    ; re_a = sext16(low)              movswl %si,%r9d    ; re_a
  movswl %dx,%r10d   ; re_b                             movswl %dx,%r10d   ; re_b
  sar    $0x10,%esi  ; im_a = high16                    sar    $0x10,%esi  ; im_a
  sar    $0x10,%edx  ; im_b                             sar    $0x10,%edx  ; im_b
  imul   %r10d,%r8d  ; r8  = re_a · re_b                imul   %r10d,%eax  ; re_a · re_b
  imul   %edx,%eax   ; eax = im_a · im_b                imul   %edx,%r8d   ; im_a · im_b
  …                                                     …
  sub    %rax,%rdi   ; REAL = re_a·re_b − im_a·im_b     add    %rax,%rdi   ; REAL = re_a·re_b + im_a·im_b
  imul   %r10d,%esi  ; im_a · re_b                       imul   %r10d,%esi
  imul   %r9d,%edx   ; re_a · im_b → IMAG = sum/diff     imul   %r9d,%edx
```

The **only** difference between `c` (complex) and `j` (conjugate) is one instruction: the real partial
uses **`sub`** for the straight product (`re·re − im·im`) and **`add`** for the conjugate
(`re·re + im·im`). That is the textbook conjugate identity: `a·conj(b)` flips the sign of `b`'s
imaginary part, turning the `−im_a·im_b` real term into `+im_a·im_b` and flipping the imaginary
cross-term. Driven live via ctypes (`a = 3+4i`, `b = 1+2i`, packed `{re,im}` in a 32-bit word, output
read as 2 × 48-bit signed):

```
mul_96c_32c_32c(a=3+4i, b=1+2i):   re48 = -5,  im48 = +10   ← (3+4i)(1+2i) = -5 + 10i      ✓
mul_96c_32j_32c(a=3+4i, b=1+2i):   re48 = +11, im48 = -2    ← (3+4i)·conj(1+2i)=(3+4i)(1-2i)= 11 - 2i  ✓
```

Both bit-exact against the reference complex arithmetic — **OBSERVED-by-execution**. The `96c` output is
`{re48, im48}` packed little-endian across the three 32-bit lane words (`re48` in words 0–1 low, `im48`
in words 1 high–2). The `mula_96c_96c_32c_32c` / `mula_96c_96c_32j_32c` forms add the incoming complex
accumulator to this product; the `muls_*` forms subtract it. The `mul_96_64c_64c_c_{0,1}` family is the
**`int32`-element** complex (a `64c` = packed-64 `{int32 re, int32 im}`), with `_0`/`_1` selecting the
real or imaginary output half — the lane-pair split that lets a 96-bit complex result spill across two
accumulator reads. `[HIGH/OBSERVED by execution]`

> **QUIRK — there is no separate 16-bit (`48c`) complex value leaf; the 16-bit complex mnemonic resolves
> through the 32-bit-packed (`32c`) leaf.** A `2x32c` lane *is* the 32-bit container of a 16-bit complex
> pair, so `ivp_mulnx16c` joins `module__xdref_mul_96c_32c_32c` — the leaf is named by the **packed lane
> width (32)**, not the per-component width (16). The complex value-leaf set is exactly **11** distinct
> bodies (`mul`/`mula`/`muls` × `c`/`j` at packed-32, plus the `64c` real/imag-select quartet); the 11
> complex *mnemonics* all resolve into this set by the `(op, complex-width, conj?)` join key.
> `[HIGH/OBSERVED]`

---

## 6. The mnemonic ↔ value-leaf join (by width and sign-token)

As in B04, **the encode mnemonic and the value leaf are joined by the `(operation, sign-token,
lane-width, product-width)` signature, NOT by the literal name.** The `libisa-core` opcode
`ivp_mulusanx16` has no `xdref_mulusanx16` leaf — its semantics live in
`module__xdref_mulusa_48_48_16_16` (the width-and-sign name). The join, verified by matching widths and
executing:

| encode mnemonic (`libisa-core`) | value leaf (`libfiss-base`) | join key |
|---|---|---|
| `ivp_mulus2nx8` | `module__xdref_mulus_24_8_8` | unsigned×signed mul, 8→24 |
| `ivp_mulusa2nx8` | `module__xdref_mulusa_24_24_8_8` | unsigned×signed mul-add, 8→24 |
| `ivp_muluss2nx8` | `module__xdref_muluss_24_24_8_8` | unsigned×signed mul-sub, 8→24 |
| `ivp_mulusnx16` | `module__xdref_mulus_48_16_16` | unsigned×signed mul, 16→48 |
| `ivp_mulsun_2x16x32_0` | `module__xdref_mulsun_2x16x32_0_96_32_32` | signed×unsigned 16×32 widen, even |
| `ivp_muluu2nx8` | `module__xdref_muluu_24_8_8` | unsigned×unsigned mul, 8→24 |
| `ivp_muluuanx16` | `module__xdref_muluua_48_48_16_16` | unsigned×unsigned mul-add, 16→48 |
| `ivp_mulnx16c` | `module__xdref_mul_96c_32c_32c` | complex `int16` mul |
| `ivp_mulnx16j` | `module__xdref_mul_96c_32j_32c` | conjugate `int16` mul |
| `ivp_mulan_2x32c_0` | `module__xdref_mula_96_96_64c_64c_c_0` | complex `int32` mul-add, real half |

> **GOTCHA — never key the encode↔value join on the literal mnemonic.** Most B05 mnemonics have **no**
> name-matching `xdref` leaf (`nm | rg module__xdref_<mnem>` = 0 for the full-vector forms) — the leaf is
> named by the **sign-token + widths** (`mulus_24_8_8`, `muluua_48_48_16_16`, `mul_96c_32c_32c`), and
> the wide-vector / complex forms all resolve through that signature. A join keyed on the spelling would
> spuriously report "missing value semantics"; the correct key is `(op-token, sign, lane-width,
> product-width, conj?)`, and every B05 mnemonic *does* resolve to an executed leaf. The complex `c`/`j`
> token maps to the leaf's `c`/`j` width-suffix (`32c` vs `32j`). `[HIGH/OBSERVED]`

---

## 7. The MAC datapath — annotated C against the binary

The per-lane datapath is the **same Booth-encoded, carry-save (Wallace-tree) multiply array as B04**,
with the partial-product unit's **sign mux** flipped per operand. The `libcas-core.so` tags pin the
shared structure *and* the mixed-sign mux:

* `module_ivp_booth_enc_{8,16}_stage0` — the radix-4 Booth encoders (shared with B04).
* `module_ivp_sem_csa_l0_slice_stage0` — the carry-save adder slices (shared).
* **`module_ivp_sign_unsign_xtmulpp_16_8_stage0`** — the **sign/unsign partial-product mux** that selects
  signed vs unsigned extension per operand (16×8 path).
* **`module_ivp_su_xtmulpp_8x8_stage0`** and **`module_ivp_mul_su_8_8x8_2_16x16_stage0`** — the dedicated
  **signed×unsigned (`su`)** partial-product slices.
* `module_ivp_sem_mul_mux_even_slice_stage0` / `_odd_slice_stage0` — the even/odd lane select for the
  `_2x16x32` widening forms.
* `module_ivp_mult_add_16_16_48_stage0` — the 16×16→48 MAC reduction.

```c
// One lane of a mixed-sign / unsigned integer MAC: ivp_mul{us,su,uu}<W>  (W = 8/16/32).
// vec sources read @stage10; wvec acc read+written @stage12 (the (12,12) self-RMW).
// Result latency 2 cycles (inputs@10 -> acc@12); initiation interval 1 -> issues EVERY cycle.
// The ONLY difference from B04 is the per-operand sign mux on the partial-product unit.
typedef  int_t<3*W>  acc_t;         // wvec lane: 24 / 48 / 96 bit (3x headroom, same as B04)

acc_t mac_lane_mixed(acc_t acc_in, uint_or_int<W> a, uint_or_int<W> b,
                     sign_mux sa, sign_mux sb, mac_dir dir, bool accumulate)
{
    // (1) Partial-product generation — the sign_unsign / su xtmulpp mux selects, PER OPERAND,
    //     whether to sign-extend (movsbl/movswq) or zero-extend (the shl/sar field idiom).
    //       mulus: sa = UNSIGNED, sb = SIGNED      mulsu: sa = SIGNED,   sb = UNSIGNED
    //       muluu: sa = UNSIGNED, sb = UNSIGNED    (mul/mula/muls = SIGNED,SIGNED -> B04)
    wide_t<2*W> ea = (sa == SIGNED) ? sext<2*W>(a) : zext<2*W>(a);
    wide_t<2*W> eb = (sb == SIGNED) ? sext<2*W>(b) : zext<2*W>(b);
    wide_t<2*W> prod = booth_mul(ea, eb);          // exact 2W-bit product on the SHARED array

    // (2) Wallace reduction — the same csa_l0 slices collapse partials to carry+sum, then CPA.
    //     Widen into the 3W acc lane: SIGN-extend for signed/mixed, ZERO-extend for unsigned.
    acc_t prod_ext = (sa == SIGNED || sb == SIGNED) ? sext<3*W>(prod)   // mulus/mulsu: signed widen
                                                    : zext<3*W>(prod);  // muluu: ZERO-fill (§4.2)

    // (3) Accumulate — modular, NOT saturating (proven by execution, §4.2). Same law as B04.
    if (!accumulate)            return wrap<3*W>(prod_ext);             // mul   (overwrite)
    if (dir == MAC_ADD)         return wrap<3*W>(acc_in + prod_ext);    // mula  (acc += prod)
    /* dir == MAC_SUB */        return wrap<3*W>(acc_in - prod_ext);    // muls  (acc -= prod)
}
```

For the **complex** (`*c`/`*j`) lanes, step (1) splits each lane into `{re, im}` halves, generates
**four** real-product partials (`re·re`, `im·im`, `im·re`, `re·im`), and step (2) combines them:
`real = re_a·re_b ∓ im_a·im_b` (− for `c`, + for `j`), `imag = im_a·re_b ± re_a·im_b`. For the **packed**
(`*p*`) lanes, step (1) emits two partial products per operand word and step (2) sums both into one acc
lane. For the **quad** (`*q*xr8`/`*xr16`) lanes, the second operand is a scalar weight broadcast 4-way
(the F4 dual-load supplies `vec` + replicated weight) and four products reduce into one lane. The
reduction tree and accumulate are otherwise identical to the scalar case. `[HIGH/OBSERVED]` on the
Booth/CSA + sign-mux structure and the modular accumulate; `[MED/INFERRED]` on the exact CSA level count
(the `csa_l0` tag confirms ≥1 CSA level; the full Wallace depth is the multiplier's, not separately
exposed).

### 7.1 The MAC recurrence (carried from register-files §5)

The accumulator is a **same-stage (12,12) read-modify-write**, identical to B04: a MAC reads its `vec`
multiplicands `@10`, reads the running `wvec` accumulator `@12`, multiplies+adds, writes back `@12`.
Write@12 → next-read@12 is a single-cycle bypass, so a mixed-sign MAC chain sustains **II = 1** with a
**2-cycle result latency**. With only **4** `wvec` entries, at most four independent accumulation chains
can be live. The `libcas` stages `F0_F0_S2_Mul_28_IVP_MULUSANX16_inst_stage{0..13}` confirm the mixed-sign
op occupies the same 14-stage Mul pipeline as the signed MAC. `[HIGH/OBSERVED]` (see
[register-files §5](../core/register-files.md#5-readwrite-semantics-per-file)).

---

## 8. Readout — the `wvec`→`vec` pack (cross-link to B10)

`wvec` is **never a general source**
([register-files §6.2](../core/register-files.md#62-vec--wvec--pack-write-into-the-accumulator-and-unpack-read-it-back-out)):
a finished mixed-sign or complex accumulation is consumed only via a **pack/unpack readout** that moves
the 1536-bit accumulator back into a 512-bit `vec` register, applying an `AR`-supplied shift/round and a
narrowing saturation. This is the *same* readout path B04 uses; the unsigned accumulate simply reads back
as an unsigned narrowed result (the pack carries the sign interpretation). A B05 reduction that has
finished accumulating **must** emit a pack before any store or downstream compute can see the answer.

→ **Full readout semantics — saturation, rounding modes, the two-step full-width pack — are
[Batch 10: wvec Pack (wide→narrow readout)](b10-wvec-pack.md).** B05 owns the *mixed-sign / complex
accumulate*; B10 owns the *narrowing read-out*. `[HIGH/OBSERVED]` on the pack/unpack stage tags and the
"never a general source" property.

---

## 9. The B04 / B05 / FP partition boundary

B04 is **signed × signed** integer MAC. B05 (this page) is everything mixed-sign, unsigned, or complex.
Floating-point multiply-add is **neither** — it is [B17](b17-spfma.md) (fp32) / [B18](b18-hp-fma.md)
(fp16). The split is mechanical and `nm`-grounded — **no double count**:

```
all ivp_mul* mnemonics (libisa-core)                                       = 212
  FP MAC (xf16 / xf32 / sone)                       -> B17 / B18           =  24
  ----------------------------------------------------------------------------------
  integer-vector MAC                                                       = 188
    B04   signed × signed, non-complex                                     =  65
    B05   mixed-sign (us/su) + unsigned (uu) + complex (c/j)               = 123
                                                                     sum    = 188   ✓
    overlap (a mnemonic in two integer buckets)                            =   0   ✓
```

The classifier, applied to the `nm` `Opcode_ivp_mul*` mnemonic roster (first-match wins on the token
**immediately after `mul`**):

| token after `mul` | meaning | batch |
|---|---|---|
| `xf16` / `xf32` / `sone` anywhere | floating-point MAC | **B17 / B18** |
| `us…` | (`u`)nsigned `a` × (`s`)igned `b` | **B05** |
| `su…` | (`s`)igned `a` × (`u`)nsigned `b` | **B05** |
| `uu…` | unsigned × unsigned | **B05** |
| lane suffix `nx16c` / `nx16j` / `2x32c` | complex / conjugate | **B05** |
| `s` (single letter, e.g. `muls`, `mulsn`, `mulsgn`) | multiply-**subtract** / by-sign, **signed** | **B04** |
| none of the above | signed × signed | **B04** |

> **GOTCHA — the `s` ambiguity is the single trap in this partition (the B04 boundary, enforced here).**
> `muls` (one `s`) = multiply-**subtract**, signed → **B04**. `mulsu` (the `su` pair) = signed×unsigned →
> **B05**. `mulus` (the `us` pair) = unsigned×signed → **B05**. The disambiguator is whether the letters
> after `mul` form a recognized **two-letter sign pair** (`us`/`su`/`uu`) or a **single direction
> letter** (`s` = subtract). The executed leaves settle it: `muls_24_24_8_8` sign-extends **both**
> operands (`movsbl`/`movsbl`) and subtracts the product — squarely B04; `mulsu_24_8_8` sign-extends
> **one** and 9-bit-zero-extends the other ([§4.1](#41-the-sign-asymmetry-proven-by-execution)) — B05.
> Mis-binning `muls` into B05 (or `mulsu` into B04) would double-count and corrupt both tallies. My
> roster contains exactly the **123** `us`/`su`/`uu`/`c`/`j` integer mnemonics and **none** of B04's
> signed-subtract `muls*`. `[HIGH/OBSERVED]`

> **CORRECTION — the "141" figure is the raw `212 − 71` subtraction; the binary-true B05 integer count
> is 123.** A coarse partition that says "212 total = 71 B04 + 141 B05" treats the **24 FP MAC forms**
> (`mulnxf16`, `mulan_2xf32`, `mulsonenxf16`, …) as if they belonged to the integer-MAC batches. They do
> not — they are fp16/fp32 fused multiply-add ([B17](b17-spfma.md)/[B18](b18-hp-fma.md)), with their own
> rounding-mode and FCR/FSR plumbing. Grounded to `nm`: the integer MAC roster is **188** (`212 − 24`
> FP), splitting **65 signed (B04)** + **123 mixed/unsigned/complex (B05)**. B05 owns **123**; the extra
> `141 − 123 = 18` in the loose figure are FP forms that belong to B17/B18. This page pins **123** as the
> `nm`-counted truth and flags the FP correction so the roll-up closes onto the `1534 ↔ 12569` cover
> without absorbing floating-point placements. `[HIGH/OBSERVED]`

---

## 10. Per-batch coverage tally

| quantity | value | binary witness |
|---|--:|---|
| B05 mnemonics (`m`) | **123** | classifier over `nm libisa-core.so` `Opcode_ivp_mul*` roster (`us` 54 + `su` 21 + `uu` 37 + complex 11) |
| B05 placements (`p`) | **654** | summed `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` (`mulus` 280 + `mulsu` 114 + `muluu` 172 + complex 88) |
| B05 value leaves (`v`) | **127** | `nm libfiss-base.so \| rg -c module__xdref_(mulus\|mulsu\|muluu)` = 121 + 6 complex `mul*_96c*`/`*_64c_*` bodies |

`m = 123` rolls into the **1534** vector axis (it is a slice of the 1065 `ivp_`-prefix mnemonics);
`p = 654` rolls into the **12569** placement cover, paired only with `1534` — **never** the pre-fold
`12642` ([roll-up §6.2](template-and-partition.md#62-the-placement-roll-up-pairs--12569-never-12642)).
Together with B04's `65 / 355` (signed `ivp_mul*`), the integer-MAC region contributes
`65 + 123 = 188` mnemonics and `355 + 654 = 1009` placements to the vector axis; the 24 FP MAC
mnemonics (`184` placements) roll into B17/B18, not here.
`[HIGH/OBSERVED]`

---

## 11. Adversarial self-verification — 5 strongest claims, re-challenged

Each claim re-derived against the binary this pass; nothing taken on a report's word.

1. **"123 mixed/unsigned/complex int MAC mnemonics, 654 placements; B04+B05 integer = 188, overlap 0."**
   Re-derived: `nm libisa-core.so | rg -o 'Opcode_(ivp_mul…)…'` = 212 distinct `ivp_mul*`; the
   FP filter (`xf16`/`xf32`/`sone`) removes 24 → 188 integer; the `us`/`su`/`uu`/`c`/`j` classifier
   selects **123** (us 54, su 21, uu 37, complex 11), leaving 65 signed for B04; `188 = 65 + 123` with
   **0** overlap; summing `nm | rg -c` over the 123 = **654**. *Challenge:* could a substring `rg 'us'`
   over-count (e.g. `mulq…`)? No — the classifier keys on the token **immediately after `mul`**, and the
   FP/complex buckets are disjoint by construction (0 FP overlap, verified). `[HIGH/OBSERVED]`

2. **"`mulus` = a-unsigned × b-signed; `mulsu` = a-signed × b-unsigned (the sign asymmetry)."**
   *Challenge:* maybe both operands are treated the same and the name is cosmetic. Refuted by execution:
   `mulsu_24_8_8(0xFF, 0xFF) = −255` while `mulus_24_8_8(0xFF, 0xFF) = −255` but
   `mulsu(0xFF,0x02)=−2` vs `mulus(0xFF,0x02)=+510` — the two leaves give **different** results on the
   same inputs, and the disassembly shows `movsbl` (sign) on exactly one operand and `shl/sar 0x17`
   (9-bit zero-pass) on the other, with the operands swapped between `mulus` and `mulsu`. Four-way
   distinct from `mul`/`muluu` on `(0xFF,0xFF)`. `[HIGH/OBSERVED by execution]`

3. **"Conjugate `j` flips the imaginary partial: real = `re·re + im·im` (add), vs `c`'s `sub`."**
   *Challenge:* maybe `c` and `j` differ elsewhere. Disassembly: `mul_96c_32c_32c` and `mul_96c_32j_32c`
   are **byte-identical except one instruction** — `sub %rax,%rdi` (`c`) vs `add %rax,%rdi` (`j`) on the
   real partial. Execution confirms: `(3+4i)(1+2i) = −5+10i` (`c`); `(3+4i)·conj(1+2i) = 11−2i` (`j`),
   both bit-exact. The conjugate is exactly the sign flip on `b`'s imaginary part. `[HIGH/OBSERVED by execution]`

4. **"Unsigned accumulate zero-fills the upper acc word; the accumulate wraps (no saturate)."**
   *Challenge:* maybe `muluu` sign-extends like the signed form, or saturates at the edge. Refuted:
   `muluu_48_16_16` emits `movl $0x0,0x4(%rcx)` — an explicit **zero** of the high word, where
   `mulus_48_16_16` sign-extends (`shl/sar 0x1f`). And `muluua_24_24_8_8(0xFFFF00, 255, 255)` →
   `0x00FD01 = 64769`, the modular wrap of `16841985 mod 2^24`, **not** the saturated `0xFFFFFF`.
   `[HIGH/OBSERVED by execution]`

5. **"Mixed/complex MAC shares B04's Mul slot (`s2`, iclass `Mul_28`), 8 placements; single-replicate
   quad is F4-exclusive."** *Challenge:* maybe the mixed-sign ops use a different functional unit or
   slot. `nm | rg 'Opcode_ivp_mulusanx16_Slot_.*_encode'` = the same 8 `s2_mul` slots as the signed
   family; `libcas` tags it `F0_F0_S2_Mul_28_IVP_MULUSANX16` — the **same `Mul_28`** iclass. And
   `nm | rg 'Opcode_ivp_mul{us,su,uu}q2n8xr8_Slot_.*'` each returns **exactly one** symbol —
   `Slot_f4_s2_mul_encode` — so the dual-load F4 constraint holds across all three sign buckets, same as
   B04's signed quad. `[HIGH/OBSERVED]`

**What I could not ground to OBSERVED.** The complex value-leaf *internal* bit-packing of the `96c`
output across the three lane words is `[HIGH/OBSERVED by execution]` for the `32c` leaves (decoded by
sweeping inputs and reading back) but `[MED/INFERRED]` for the exact word boundaries of the `64c`
`_0`/`_1` real/imag-select forms (the lane-pair spill is inferred from the `c_0`/`c_1` suffix + the
`mula` body, not exhaustively swept). The 14-stage Mul pipeline's *internal* CSA depth is `[MED/INFERRED]`
(the `csa_l0` tag confirms ≥1 level; the Wallace depth is the multiplier's, not separately exposed).

---

## 12. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| 123 mixed/uns/complex int MAC mnemonics; 654 placements; B04+B05 integer = 188, overlap 0 | `[HIGH/OBSERVED]` | `nm libisa-core.so` `Opcode_ivp_mul*` roster + classifier + per-mnemonic `rg -c` |
| FP MAC (xf16/xf32/sone, 24) is B17/B18, not B05; "141" = loose `212−71` | `[HIGH/OBSERVED]` | FP filter over the 212 roster; 24 disjoint from the 188 integer |
| Every mixed/complex MAC is a `s2_mul`-slot opcode, iclass `Mul_28` (8 placements typical) | `[HIGH/OBSERVED]` | `Opcode_*_Slot_<f>_s2_mul_encode` symtab + `libcas F0_F0_S2_Mul_28_*` stages |
| Opcode-sel templates (byte-exact `F0_S2_Mul` `WORD0`); sign/dir is a distinct opcode, no global bit | `[HIGH/OBSERVED]` | `objdump -d` of the encode thunks this pass; non-uniform deltas |
| `mulus` = unsigned×signed, `mulsu` = signed×unsigned (operand-order sign asymmetry) | `[HIGH/OBSERVED by execution]` | ctypes-driven `mulus`/`mulsu`/`muluu` leaves, 4-way distinct on `(0xFF,0xFF)` |
| `mulsu` has no symmetric narrow form (21 vs `mulus` 54); appears only in widening/quad/packed | `[HIGH/OBSERVED]` | `nm` roster: no `mulsu2nx8`/`mulsunx16` |
| Unsigned accumulate zero-fills upper word; modular wrap, no saturate | `[HIGH/OBSERVED by execution]` | `muluu_48_16_16` `movl $0x0,0x4`; `muluua` edge wrap |
| Complex `c` = `sub`, conjugate `j` = `add` real partial; one-instruction difference | `[HIGH/OBSERVED by execution]` | `mul_96c_32c_32c` vs `mul_96c_32j_32c` byte-diff + executed `(3+4i)(1±2i)` |
| 16-bit complex resolves through the 32-bit-packed (`32c`) leaf; 11 complex leaves | `[HIGH/OBSERVED]` | width-signature join; `nm` complex-leaf census |
| Booth + CSA/Wallace multiplier with per-operand sign mux; 2-cycle / II=1 (12,12) RMW | `[HIGH/OBSERVED]` structure; `[MED/INFERRED]` CSA depth | `libcas` `booth_enc`/`csa_l0`/`sign_unsign_xtmulpp`/`su_xtmulpp` tags + register-files §5 |
| Single-replicate quad (`q*xr8`/`q*xr16`) F4-exclusive across all 3 sign buckets | `[HIGH/OBSERVED]` | one placement each = `Slot_f4_s2_mul_encode` |
| Mnemonic↔value-leaf join is by `(op, sign, lane, product)` signature, not name | `[HIGH/OBSERVED]` | full-vector mnemonics have no name-leaf; all resolve by width+sign |

---

## 13. Cross-references

- [ISA Batch 04 — Integer MAC Matrix (signed)](b04-mac-integer.md) — the *signed × signed* half of the
  integer MAC ISA; B05 is its complement, and the partition boundary is
  [§9](#9-the-b04--b05--fp-partition-boundary). Match its `vec`/`wvec` geometry and 3× headroom.
- [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the B01–B30 per-instruction
  template this page follows, the partition row for B05, and the roll-up coverage model.
- [The FLIX VLIW Encoding (14 format / 46 slot)](../core/flix-encoding.md) — the Mul slot (`s2`) these ops
  occupy, the F4 dual-load format the quad forms require, and the encode-thunk `WORD0` ABI / two-tier
  selector model.
- [The Eight Register Files](../core/register-files.md) — the `vec` (multiplicand) / `wvec` (accumulator)
  geometry, the `(12,12)` self-RMW accumulate, and the 2-cycle MAC recurrence.
- [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the certified `12569` placement
  / `864` value-leaf denominators this batch's `654` / executed leaves contribute to; the no-cross-pair
  law.
- [ISA Batch 10 — wvec Pack (wide→narrow readout)](b10-wvec-pack.md) — the `wvec`→`vec` pack/unpack that
  reads a finished B05 accumulation back into a normal vector register (saturation + rounding).
- [ISA Batch 17 — fp32 FMA](b17-spfma.md) / [ISA Batch 18 — fp16 FMA](b18-hp-fma.md) — the **24 FP MAC**
  forms (`*xf16`/`*xf32`/`*sone`) that the loose `212−71` subtraction would mis-bin into B05.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED`/`INFERRED`/
  `CARRIED` tags and the proven-by-execution value lane used throughout
  [§4](#4-the-mixed-sign--unsigned-leaf-bodies)–[§5](#5-complex--conjugate-multiply--driven-live).

---

*Provenance: the encode templates and slot placements are `[HIGH/OBSERVED]` — re-disassembled
in-checkout from `libisa-core.so` (`ncore2gp/config/`, `.data.rel.ro` delta `0x200000`, `.text`/`.rodata`
VMA==file, re-read via `readelf -SW` this pass); the value semantics in
[§4](#4-the-mixed-sign--unsigned-leaf-bodies)–[§5](#5-complex--conjugate-multiply--driven-live) are
`[HIGH/OBSERVED by execution]` — the `libfiss-base.so` `xdref` leaves were loaded via ctypes and run on
the inputs shown (the mixed-sign sign-asymmetry, the unsigned overflow wrap, the complex/conjugate
products all bit-exact); the Booth/CSA multiplier + sign-mux structure and pipeline stages are
`[HIGH/OBSERVED]` from `libcas-core.so` symbol tags (CSA depth `[MED/INFERRED]`). The `extracted/`
carving is gitignored; counts are `nm | rg -c` against the binary `.symtab`. All prose reads as derived
from shipped-artifact static analysis (lawful interoperability RE).*
