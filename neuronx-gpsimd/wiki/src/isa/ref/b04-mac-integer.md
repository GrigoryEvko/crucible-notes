# ISA Batch 04 — Integer MAC Matrix (signed)

This is the per-instruction reference for the **signed integer multiply-accumulate** family of the
Vision-Q7 *Cairo* (`ncore2gp`) ISA: the 71 shipped mnemonics that read two **signed** integer
operands out of the 512-bit [`vec` file](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster),
generate a per-lane product on the Booth/CSA multiply array, and reduce it into the 1536-bit
[`wvec` wide accumulator](../core/register-files.md#per-file-detail) — the deep-learning inner-loop
critical path. It is reimplementation-grade: every mnemonic's FLIX slot, opcode-selector template,
operand widths and value semantics are read **directly out of the shipped binaries**
(`libisa-core.so` encode thunks, `libfiss-base.so` `xdref` value leaves driven live by ctypes,
`libcas-core.so` pipeline tags) and tagged. The signed family here is *one half* of the integer MAC
ISA; the mixed-sign (`us`/`su`), unsigned (`uu`) and complex (`c`/`j`) variants are
[**Batch 05**](b05-mac-mixed.md) — the boundary is stated and arithmetic-checked in
[§9](#9-the-b04--b05-partition-boundary).

This page inherits the certified-perfect denominator from
[the coverage tally](../core/coverage-tally.md): the `1534 / 12569` shipped mnemonic/placement
cover and the `864/864` value-leaf cover. Counts are grounded with `nm | rg -c` against the
binary `.symtab`, never a decompile grep; the `extracted/` tree is gitignored (reach it with
`fd --no-ignore` or an absolute path). Confidence tags follow
[the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte / immediate
/ symbol / **executed** value read from the shipped binary; `INFERRED` = reasoned over OBSERVED;
`CARRIED` = re-used at a cited page's confidence; crossed with `HIGH`/`MED`/`LOW`. All prose is
binary / static-analysis derived only.

> **Scope in one line.** B04 = `mul`/`mula`/`muls` (signed multiply, signed mul-**add**, signed
> mul-**sub**) plus the packed (`mulp*`), sign-multiply (`mulsgn*`), quad-replicate (`mulq*`,
> `mul4t*`) and immediate (`mul*i*`) signed variants, in lane widths **8 / 16 / 32 bit**,
> accumulating into **24 / 48 / 96-bit** `wvec` lanes. **71 mnemonics, 373 placements.**
> `[HIGH/OBSERVED]`

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| B04 mnemonics (signed-int MAC) | **71** | classifier over `nm libisa-core.so` `Opcode_*` roster ([§9](#9-the-b04--b05-partition-boundary)) |
| B04 placements (`mnemonic × slot`) | **373** | summed `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` over the 71 |
| Issue slot | **`s2 = Mul`** of every wide format + `N1` | `Opcode_*_Slot_<f>_s2_mul_encode` (8 placements typical) |
| Accumulate target | **`wvec`** (idx 5, 1536-bit, 4 entries) | [register-files §3](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster) |
| Multiplicand source | **`vec`** (idx 2, 512-bit, 32 entries) | same |
| Lane → product widths | 8→24, 16→48, 32→96 (3× headroom) | `xdref` leaf width-signature ([§4](#4-the-accumulator-width--lane-grouping-matrix)) |
| Multiplier datapath | **Booth-encoded + carry-save (CSA) Wallace** | `module_ivp_booth_enc_{8,16}_stage0`, `module_ivp_sem_csa_l0_slice_stage0` (`libcas-core.so`) |
| MAC pipeline depth | inputs `@10` → result `@12` = **2-cycle latency, II = 1** | [register-files §5](../core/register-files.md#5-readwrite-semantics-per-file); `mul32_opcode_stage0..6` |
| Accumulate overflow | **wraps mod 2^(lanewidth)** (no saturate on the accumulate) | executed: `mula_24_24_8_8`, `mula_48_48_16_16` ([§5](#5-value-semantics--driven-live)) |
| Quad (single-replicate) constraint | `mulq*xr8`/`mulq*xr16` forms are **F4-only** (dual-load) | `mulq2n8xr8`/`mulqn16xr16` → `Slot_f4_s2_mul_encode` only |
| Value-leaf join key | **(op, lane-width, product-width)**, *not* the mnemonic spelling | `ivp_mulnx16` → `module__xdref_mul_48_16_16` ([§6](#6-the-mnemonic--value-leaf-join)) |

The signed family is the densest single sub-batch of the vector ISA because three orthogonal
axes multiply out: **accumulate direction** (`mul` overwrite / `mula` add / `muls` subtract) ×
**lane width** (8/16/32) × **operand-delivery form** (full-vector / packed / replicated-scalar /
immediate). The roster below is organized on those three axes.

---

## 2. Roster — the 71 signed integer MAC mnemonics

Columns: `mnemonic` · `FLIX format·slot` (the slots hosting it; all sit in the **Mul** slot `s2`) ·
`opcode-sel imm` (the `F0_S2_Mul` encode-thunk template `WORD0`, the
[universal `C7 07 imm32 C3` ABI](../core/flix-encoding.md#61-the-universal-encode-thunk-abi); per-format
packing differs) · `vec→wvec widths` (lane-bits → product/acc-bits) · `bytes` (16 wide / 8 narrow) ·
semantics · `[conf]`. Templates are byte-exact from `objdump -d` this pass; widths from the joined
[`xdref` leaf](#6-the-mnemonic--value-leaf-join).

### 2.1 Core full-vector signed MAC (the dot-product spine)

| mnemonic | fmt·slot | opcode-sel imm (F0·s2) | vec→wvec | bytes | semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_mul2nx8` | F0/F1/F2/F3/F6/F7/F11/N1 · s2_mul | `0x00c57000` | 8 → 24 | 16/8 | signed `int8` × `int8` → 24-bit, **overwrite** lane | `[HIGH/OBSERVED]` |
| `ivp_mula2nx8` | (same 8 slots) | `0x00c57040` | 8 → 24 | 16/8 | signed `int8`×`int8`, **acc += prod** | `[HIGH/OBSERVED]` |
| `ivp_muls2nx8` | (same 8 slots) | `0x0101f040`† | 8 → 24 | 16/8 | signed `int8`×`int8`, **acc −= prod** | `[HIGH/OBSERVED]` |
| `ivp_mulnx16` | (same 8 slots) | `0x01003040`† | 16 → 48 | 16/8 | signed `int16`×`int16` → 48-bit, overwrite | `[HIGH/OBSERVED]` |
| `ivp_mulanx16` | (same 8 slots) | `0x00c570c0` | 16 → 48 | 16/8 | signed `int16`×`int16`, acc += prod | `[HIGH/OBSERVED]` |
| `ivp_mulsnx16` | (same 8 slots) | `0x01027040`† | 16 → 48 | 16/8 | signed `int16`×`int16`, acc −= prod | `[HIGH/OBSERVED]` |
| `ivp_muln_2x32` | (same 8 slots) | `0x010…`† | 32 → 96 | 16/8 | signed `int32`×`int32` → 96-bit, overwrite | `[HIGH/OBSERVED]` |
| `ivp_mulan_2x32` | (same 8 slots) | `0x010…`† | 32 → 96 | 16/8 | signed `int32`×`int32`, acc += prod | `[HIGH/OBSERVED]` |
| `ivp_mulsn_2x32` | (same 8 slots) | — | 32 → 96 | 16/8 | signed `int32`×`int32`, acc −= prod | `[HIGH/OBSERVED]` |
| `ivp_muln_2x16x32_0` | (same 8 slots) | `0x0100f040`† | 16×32 → 96 (even) | 16/8 | signed `int16`×`int32` widening, even lanes, overwrite | `[HIGH/OBSERVED]` |
| `ivp_muln_2x16x32_1` | (same 8 slots) | — | 16×32 → 96 (odd) | 16/8 | … odd lanes | `[HIGH/OBSERVED]` |
| `ivp_mulan_2x16x32_0` | (same 8 slots) | `0x00c5b080` | 16×32 → 96 (even) | 16/8 | signed `int16`×`int32`, even lanes, acc += | `[HIGH/OBSERVED]` |
| `ivp_mulan_2x16x32_1` | (same 8 slots) | — | 16×32 → 96 (odd) | 16/8 | … odd lanes, acc += | `[HIGH/OBSERVED]` |
| `ivp_mulhn_2x16x32_1` | (same 8 slots) | — | 16×32 → 96 (high) | 16/8 | high-half signed widening, overwrite | `[MED/OBSERVED]` |
| `ivp_mulahn_2x16x32_1` | (same 8 slots) | — | 16×32 → 96 (high) | 16/8 | high-half signed widening, acc += | `[MED/OBSERVED]` |
| `ivp_mulshn_2x16x32_1` | (same 8 slots) | — | 16×32 → 96 (high) | 16/8 | high-half signed widening, acc −= | `[MED/OBSERVED]` |

† `mulnx16`/`muls*`/`muln_2x16x32` carry a *distinct iclass* whose `F0_S2_Mul` template is **not** a
`+0x40` step off the overwrite form — accumulate is a separate opcode, not a single global bit
([§3.2](#32-accumulate-is-a-distinct-opcode-not-a-bit-flip)).

### 2.2 Packed signed MAC (`mulp*` — dual-product per slot)

The `mulp` family produces **two** partial products per lane-pair (the leaf signature `_24_8_8_8_8`
= two 8-bit × two 8-bit feeding one 24-bit lane) — a packed/parallel MAC used for `int8` GEMM
inner loops where two weights ride one operand word.

| mnemonic | vec→wvec | semantics | conf |
|---|---|---|---|
| `ivp_mulp2nx8` / `ivp_mulpa2nx8` | 8 → 24 | packed signed `int8` mul / mul-add (`mulp_24_8_8_8_8`) | `[HIGH/OBSERVED]` |
| `ivp_mulps2nx8` | 8 → 24 | packed signed mul-**sub** | `[HIGH/OBSERVED]` |
| `ivp_mulpd2nx8` / `ivp_mulpda2nx8` | 8 → 24 | packed-**dual** signed mul / mul-add (`mulpd_24_8_8_8_8`) | `[HIGH/OBSERVED]` |
| `ivp_mulpnx16` / `ivp_mulpanx16` | 16 → 48 | packed signed `int16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulpsnx16` | 16 → 48 | packed signed `int16` mul-sub | `[HIGH/OBSERVED]` |
| `ivp_mulpdnx16` / `ivp_mulpdanx16` | 16 → 48 | packed-dual signed `int16` mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulp2n8xr16` / `ivp_mulpa2n8xr16` | 8 → 24 | packed signed `int8` × replicated-16 mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulpan16xr16` / `ivp_mulpn16xr16` | 16 → 48 | packed signed `int16` × replicated-16 mul-add / mul | `[HIGH/OBSERVED]` |
| `ivp_mulpai2nr8x16` / `ivp_mulpi2nr8x16` | 8×16 | packed signed immediate-replicated mul-add / mul | `[MED/OBSERVED]` |

### 2.3 Quad-replicate signed MAC (`mulq*`, `dmulq*`, `mul4t*`)

The quad family multiplies one `vec` operand against a **replicated scalar weight** broadcast four
ways (`q` = quad, `d`-prefix = double-quad, `4t` = 4-tap transpose). It is the systolic-style
weight-stationary form. **Slotting splits on the replicate fan-out:**

| mnemonic | fmt·slot | opcode-sel imm | vec→wvec | semantics | conf |
|---|---|---|---|---|---|
| `ivp_mulq2n8xr8` | **F4·s2_mul only** | `0x88000000` | 8 → 24 | signed `int8` × replicate-8 quad mul (needs dual-load F4) | `[HIGH/OBSERVED]` |
| `ivp_mulqa2n8xr8` | **F4·s2_mul only** | `0x8c000000` | 8 → 24 | … quad **mul-add** | `[HIGH/OBSERVED]` |
| `ivp_mulqn16xr16` | **F4·s2_mul only** | `0x98000000` | 16 → 48 | signed `int16` × replicate-16 quad mul | `[HIGH/OBSERVED]` |
| `ivp_mulqan16xr16` | **F4·s2_mul only** | `0x90000000` | 16 → 48 | … quad mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulqn16xr8` / `ivp_mulqan16xr8` | F4·s2_mul | — | 16×8 → 48 | signed `int16` × replicate-8 quad mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulq2n8dxr8` / `ivp_mulq2n8qxr8` | F0/F2/F7/N1 · s2 | (per-format) | 8 → 24 | double / quad-fanout replicate quad mul | `[HIGH/OBSERVED]` |
| `ivp_mulqa2n8dxr8` / `ivp_mulqa2n8qxr8` | F0/F2/F7/N1 · s2 | — | 8 → 24 | … quad mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulqn16dxr16` / `ivp_mulqan16dxr16` | F0/F2/F7/N1 · s2 | — | 16 → 48 | double-fanout signed quad mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_dmulq2n8xr8` … `ivp_dmulqa2n8qxr8` (6) | F0/F2/F7/N1 · s2 | — | 8 → 24 | **double**-quad signed mul / mul-add, ×r8 fanouts | `[HIGH/OBSERVED]` |
| `ivp_mul4t2n8xr8` / `ivp_mul4ta2n8xr8` | 8 slots · s2 | — | 8 → 24 | 4-tap transpose signed mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mul4tn16xr16` / `ivp_mul4tan16xr16` | 8 slots · s2 | `0x00c00080` / `0x00c00040` (F0) | 16 → 48 | 4-tap transpose `int16`×r16 mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mul4tn16xr8` / `ivp_mul4tan16xr8` | 8 slots · s2 | — | 16×8 → 48 | 4-tap transpose `int16`×r8 mul / mul-add | `[HIGH/OBSERVED]` |

> **GOTCHA — the `mulq*` single-replicate quad is *F4-exclusive*; the `dxr8`/`qxr8` multi-fanout and
> the `mul4t*` transpose forms are *not*.** `mulq2n8xr8`, `mulqn16xr16`, `mulqan16xr16`,
> `mulqn16xr8`, `mulqan16xr8` each resolve to **one** placement — `Slot_f4_s2_mul_encode` — because
> the single-replicate quad consumes *two load slots* to deliver `vec` operand + replicated weight,
> and only **F4** (the dual-`Ld` format,
> [flix §2](../core/flix-encoding.md#2-the-14-formats--selector-length-slot-composition)) carries two
> `Ld` slots. The `dxr8`/`qxr8` fan-out forms read the weight from a single load and place in
> F0/F2/F7/N1; the `mul4t*` transpose forms place in all 8 wide+N1 slots (verified `nm`). A
> reimplementer's bundler **must** schedule the `mulq*xr8`/`mulq*xr16` quad MAC into F4 or the bundle
> is unencodable. `[HIGH/OBSERVED]`

### 2.4 Immediate-scalar, sign-multiply, and pack-low

| mnemonic | vec→wvec | semantics | conf |
|---|---|---|---|
| `ivp_muli2nx8x16` / `ivp_mulai2nx8x16` | 8×16 → 48 | signed `int8` × **immediate** `int16` mul / mul-add (`mul[a]i2nx8x16_48_…_16`) | `[HIGH/OBSERVED]` |
| `ivp_muli2nr8x16` / `ivp_mulai2nr8x16` | 8×16 → 48 | signed immediate-replicated mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mul2n8xr16` / `ivp_mula2n8xr16` | 8×16 → 48 | signed `int8` × replicate-16 mul / mul-add | `[HIGH/OBSERVED]` |
| `ivp_mulsgnnx16` | 16 → 16 | **multiply-by-sign**: `b · sign(a)` per lane (returns ±b) | `[HIGH/OBSERVED]` |
| `ivp_mulsgnsnx16` | 16 → 16 | signed sign-multiply, saturating variant | `[HIGH/OBSERVED]` |
| `ivp_mulsgnn_2x32` | 32 → 32 | 32-bit multiply-by-sign | `[HIGH/OBSERVED]` |
| `ivp_mulnx16packl` / `ivp_mulanx16packl` | 16 → **16** | signed `int16`×`int16`, keep **low 16** packed into `vec` (no wide acc) — `mul_16_16_16_packl` | `[HIGH/OBSERVED]` |
| `ivp_mulsnx16packl` / `ivp_mulanx16packlt` | 16 → 16 | low-half pack, sub / throttled (`_t`) variants | `[HIGH/OBSERVED]` |

> **NOTE — `packl` writes `vec`, not `wvec`.** The `*packl` forms are the only B04 ops that do **not**
> target the wide accumulator: they compute the full signed product and write back only the **low
> lane-width bits** into a normal `vec` register (leaf `mul_16_16_16_packl`, output is one 16-bit
> lane). They are the "I want the truncated product directly, no readout" path — useful for modular
> arithmetic. Everything else in B04 lands in `wvec` and needs a
> [pack/unpack readout](#7-readout-the-wvecvec-pack-cross-link-to-b10). `[HIGH/OBSERVED]`

---

## 3. Encoding — where signed MAC lives in the FLIX grid

### 3.1 Every signed MAC is a Mul-slot (`s2`) opcode

Querying the encode-thunk symtab, the full-vector signed MAC ops resolve to **8 placements** each —
one per `s2_mul` slot of the seven wide formats that carry a Mul slot **plus** narrow `N1`:

```
nm libisa-core.so | rg 'Opcode_ivp_mulanx16_Slot_.*_encode' | rg -o 'Slot_[a-z0-9_]+'
  →  Slot_f0_s2_mul  Slot_f1_s2_mul  Slot_f2_s2_mul  Slot_f3_s2_mul
     Slot_f6_s2_mul  Slot_f7_s2_mul  Slot_f11_s2_mul  Slot_n1_s2_mul     (8)
```

This is exactly the Mul-slot roster from
[flix §5.1](../core/flix-encoding.md#51-functional-unit-vocabulary): `mul` = s2 of every wide format,
`N1` s2, **absent in N0/N2**. So a signed MAC can co-issue with a load/store (s0), a load (s1) and
1–3 ALU ops (s3+) in the same 16-byte wide bundle — the structural basis of the weight-stationary
inner loop. The `packl` forms drop the `F0/F3/F11` placements (they need the ALU-style writeback
path), landing in F1/F2/F7/N1 only — confirmed `nm` this pass. `[HIGH/OBSERVED]`

The placement total over the 71 B04 mnemonics is **373** (summed `nm | rg -c` per mnemonic). With
45 of the 71 carrying the canonical 8 Mul-slot placements and the F4-only quad / restricted-`packl`
forms carrying fewer, `373` is the contribution of B04 to the certified `12569` placement cover
([coverage-tally §1](../core/coverage-tally.md#1-the-five-headline-numbers--re-derived-from-the-binary-this-pass)).
`[HIGH/OBSERVED]`

### 3.2 Accumulate is a *distinct opcode*, not a bit flip

The `mul` (overwrite) vs `mula` (add) vs `muls` (subtract) distinction is **not** a single
selector bit. Reading the `F0_S2_Mul` templates byte-exact:

```
ivp_mul2nx8         WORD0 = 0x00c57000
ivp_mula2nx8        WORD0 = 0x00c57040     (+0x40   — looks like a bit)
ivp_mulnx16         WORD0 = 0x01003040
ivp_mulanx16        WORD0 = 0x00c570c0     (DIFFERENT iclass — not +0x40)
ivp_muln_2x16x32_0  WORD0 = 0x0100f040
ivp_mulan_2x16x32_0 WORD0 = 0x00c5b080     (DIFFERENT iclass)
```

For the 8-bit `2nx8` pair the delta *happens* to be `+0x40`, but for `nx16` and `_2x16x32` the
accumulate form is a wholly different selector word (it lives in a different iclass and is packed
differently per format). This reproduces the flix-encoding page's
[two-tier selector CORRECTION](../core/flix-encoding.md#62-the-two-tier-selector-model): the
add/sub/overwrite variant is a **distinct opcode** realized by per-format packing, *not* a
roster-wide "accumulate bit". A reimplementer's assembler must carry the full `(mnemonic, slot) →
template` table; it cannot synthesize `mula` from `mul` by OR-ing a constant. `[HIGH/OBSERVED]`

The quad F4 forms show the same pattern: `mulq2n8xr8 = 0x88000000` → `mulqa2n8xr8 = 0x8c000000`
(`+0x04000000`), but `mulqn16xr16 = 0x98000000` → `mulqan16xr16 = 0x90000000` (`−0x08000000`). The
accumulate delta is sign- and direction-dependent on the iclass, never a uniform bit. `[HIGH/OBSERVED]`

---

## 4. The accumulator-width / lane-grouping matrix

The signed family spans three lane widths, each accumulating into a `wvec` lane that is **3×** the
lane width — the "3× NX16 headroom" of the
[`wvec` file](../core/register-files.md#per-file-detail). Read directly from the `xdref` leaf
width-signatures (`module__xdref_<op>_<accbits>_<accbits>_<in>_<in>`):

| input lane | product width | `wvec` acc lane | lanes / 512b vec | acc lanes / 1536b wvec | `xdref` width-sig | canonical mnemonic |
|---:|---:|---:|---:|---:|---|---|
| **8-bit** (`int8`) | 16-bit raw | **24-bit** (3×8) | 64 | 64 | `_24_8_8` / `_24_24_8_8` | `ivp_mul2nx8` / `ivp_mula2nx8` |
| **16-bit** (`int16`) | 32-bit raw | **48-bit** (3×16) | 32 | 32 | `_48_16_16` / `_48_48_16_16` | `ivp_mulnx16` / `ivp_mulanx16` |
| **32-bit** (`int32`) | 64-bit raw | **96-bit** (3×32) | 16 | 16 | `_96_32_32` / `_96_96_32_32` | `ivp_muln_2x32` / `ivp_mulan_2x32` |
| **16×32 mixed** | 48-bit raw | **96-bit** | 16 (even/odd select) | 16 | `_2x16x32_{0,1}_96_96_32_32` | `ivp_mulan_2x16x32_{0,1}` |

**How the 512-bit `vec` lanes map into the 1536-bit `wvec`:** the multiplier widens each lane by 3×,
so a 512-bit `vec` of *L*-bit lanes (512/*L* lanes) accumulates into a 1536-bit `wvec` of *3L*-bit
lanes (1536/3*L* = 512/*L* lanes — the **same lane count**, each 3× taller). The lane index is
preserved 1:1; only the per-lane bit budget triples. This is why `wvec` has exactly **3× headroom**:
one full-width product (2*L*) plus *L* bits of carry-growth room across a long reduction, before the
3*L*-bit lane wraps ([§5](#5-value-semantics--driven-live) proves the wrap point).

The **mixed 16×32** (`_2x16x32`) widening MAC reads a 16-bit multiplicand and a 32-bit one (both
sign-extended — `movswq`/`movslq` in the leaf), so its 16 even-or-odd lanes pull from a *pair* of
`vec` registers; `_0` selects even 32-bit lanes, `_1` selects odd. This is the form that bridges an
`int16` activation against an `int32` partial sum. `[HIGH/OBSERVED]`

---

## 5. Value semantics — driven LIVE

`libfiss-base.so` is **callable in-process via ctypes with no license**
([coverage-tally §5](../core/coverage-tally.md#5-value-semantics-coverage--the-864864-execution-validated-cover)),
so the value semantics below are **proven-by-execution**, not decoded-and-guessed. The leaf SysV ABI,
recovered from `objdump -d` of the leaf bodies, is: `rdi` = unused context; then the value operands;
the last pointer arg is the output. Both signed inputs are sign-extended (`movsbl`/`movswl`/`movslq`)
before the `imul` — confirming the **signed** datapath byte-for-byte.

```c
// Recovered leaf prototypes (libfiss-base.so), grouped by lane width:
void module__xdref_mul_24_8_8     (void*, int8_t a,  int8_t b,            uint32_t *out24);
void module__xdref_mula_24_24_8_8 (void*, uint32_t acc, int8_t a, int8_t b, uint32_t *out24);
void module__xdref_mul_48_16_16   (void*, int16_t a, int16_t b,           acc48_t  *out48);
void module__xdref_mula_48_48_16_16(void*, acc48_t *acc, int16_t a, int16_t b, acc48_t *out48);
void module__xdref_mul_96_32_32   (void*, int32_t a, int32_t b,           acc96_t  *out96);
void module__xdref_mula_96_96_32_32(void*, acc96_t *acc, int32_t a, int32_t b, acc96_t *out96);
// acc48 = {uint32 lo; uint32 hi /*low16 used*/}; acc96 = {uint32 w0; uint32 w1; uint32 w2}.
```

### 5.1 Signed 8×8 → 24-bit and the multi-step accumulate (executed)

Driving `mul_24_8_8` and `mula_24_24_8_8` live (ctypes, raw int8 bytes, 24-bit two's-complement
interpretation):

```
mul_24_8_8:    127 *  127 = 0x003f01 = +16129   (ref +16129)  OK
              -128 * -128 = 0x004000 = +16384   (ref +16384)  OK
              -128 *  127 = 0xffc080 = -16256   (ref -16256)  OK   ← sign-correct
               100 *  -50 = 0xffec78 =  -5000   (ref  -5000)  OK

mula_24_24_8_8  (acc += a*b, four steps, fresh acc=0):
   acc += 120*100 = +12000 → +12000   OK
   acc += -90* 80 =  -7200 →  +4800   OK
   acc += 127*-128= -16256 → -11456   OK
   acc +=  64* 64 =  +4096 →  -7360   OK
   final accumulator = -7360 ; reference dot-product = -7360 → EXACT
```

The accumulate is bit-exact across the chain — the proof that a multi-step `acc += a*b` reduction
retains precision within the 24-bit lane. `[HIGH/OBSERVED by execution]`

### 5.2 Signed-overflow accumulate — the wrap point (executed)

The accumulate **wraps mod 2^(lanewidth)**; it does **not** saturate. Pushing the 24-bit lane past
its positive edge:

```
mula_24_24_8_8:  acc = +8388607 (=0x7FFFFF, max +24bit)
                 acc += 127*100 = +12700
                 result = 0x80319b = -8375909
                 math (8401307) wraps mod 2^24 → -8375909   ✓ matches leaf
```

and the same at 48-bit (`mula_48_48_16_16`, acc = `0x7FFF_FFFF_FFFF`):

```
                 acc = +140737488355327 (max +48bit)
                 acc += 32767*32767 = +1073676289
                 result = -140736414679040  = math wrapped mod 2^48   ✓ matches leaf
```

A reimplementer models the accumulate as a **modular** `acc = (acc ± a·b) mod 2^(3L)`, with **no**
clamp on the accumulate itself. (Saturation, where it exists, is applied later at the
[pack readout](#7-readout-the-wvecvec-pack-cross-link-to-b10), not on the accumulate.) `[HIGH/OBSERVED by execution]`

### 5.3 The wide accumulator earns its width — long-reduction precision (executed)

The whole point of the 3× `wvec` headroom is a long reduction without intermediate overflow. Driving
`mula_48_48_16_16` 64 times with the maximal product `32767·32767 ≈ 2³⁰`:

```
after 64×  acc += 32767*32767:
   wide 48-bit acc = 68715282496       (needs 36 bits)
   reference       = 68715282496       → EXACT
   a 32-bit acc would give 4290773056  → WRONG (overflowed after ~2 steps)
```

The 48-bit lane holds the full 36-bit running sum; a 32-bit accumulator would have wrapped after the
second step. This is the architectural justification for accumulating into `wvec` rather than back
into `vec`. `[HIGH/OBSERVED by execution]`

### 5.4 Multiply-subtract and multiply-by-sign (executed/decoded)

- **`muls*` = multiply-SUBTRACT, not "signed".** The `s` immediately after `mul` is the
  *subtract-accumulate* direction (`muls_24_24_8_8` does `imul` then a borrow-handling `acc − prod`
  with the `test/sub $1/lea` big-int sequence). It is signed×signed with a subtracting accumulate —
  squarely B04. Do **not** confuse it with the mixed-sign `mulsu`/`mulus` ([B05](b05-mac-mixed.md));
  the `su`/`us` token is a *two-letter* sign pair, distinct from the single `s`. `[HIGH/OBSERVED]`
- **`mulsgn` = multiply-by-sign.** `mulsgn_16_16_16` tests operand `a`'s sign bit (`and $0x8000`)
  and negates `b` accordingly (`neg %edx`), returning `±b` per lane — a sign-transfer op, not a true
  product. Used to apply a stored sign to a magnitude. `[HIGH/OBSERVED]`

---

## 6. The mnemonic ↔ value-leaf join

A reimplementer's single most important non-obvious fact: **the encode mnemonic and the value leaf
are joined by the `(operation, lane-width, product-width)` signature, NOT by the literal name.** The
`libisa-core` opcode `ivp_mulnx16` has **no** `xdref_mulnx16` leaf — its semantics live in
`module__xdref_mul_48_16_16` (the width-signed name). The join, verified by matching widths and
executing:

| encode mnemonic (`libisa-core`) | value leaf (`libfiss-base`) | join key |
|---|---|---|
| `ivp_mul2nx8` | `module__xdref_mul_24_8_8` | signed mul, 8-bit → 24-bit |
| `ivp_mula2nx8` | `module__xdref_mula_24_24_8_8` | signed mul-add, 8 → 24 |
| `ivp_muls2nx8` | `module__xdref_muls_24_24_8_8` | signed mul-sub, 8 → 24 |
| `ivp_mulnx16` | `module__xdref_mul_48_16_16` | signed mul, 16 → 48 |
| `ivp_mulanx16` | `module__xdref_mula_48_48_16_16` | signed mul-add, 16 → 48 |
| `ivp_mulsnx16` | `module__xdref_muls_48_48_16_16` | signed mul-sub, 16 → 48 |
| `ivp_muln_2x32` | `module__xdref_mul_96_32_32` | signed mul, 32 → 96 |
| `ivp_mulan_2x32` | `module__xdref_mula_96_96_32_32` | signed mul-add, 32 → 96 |
| `ivp_mulan_2x16x32_0` | `module__xdref_mulan_2x16x32_0_96_96_32_32` | 16×32 widen, even lane |
| `ivp_mulnx16packl` | `module__xdref_mul_16_16_16_packl` | signed mul, low-16 pack |

> **GOTCHA — never key the encode↔value join on the name.** 26 of the 71 B04 mnemonics have **no**
> name-matching `xdref` leaf (`nm | rg module__xdref_<mnem>` = 0) — the leaf is named by widths, and
> the wide-vector forms (`mul2nx8`, `mulnx16`, `mulan_2x32`, the `packl`/`mulsgn` forms) all resolve
> through the width signature. A join keyed on the literal mnemonic would spuriously report "26
> missing value semantics"; the correct key is `(op-token, lane-width, product-width)`, and every
> B04 mnemonic *does* resolve to an executed leaf. `[HIGH/OBSERVED]`

---

## 7. The MAC datapath — annotated C against the binary

The per-lane datapath, reconstructed from the `libcas-core.so` pipeline tags (`module_ivp_booth_enc_
{8,16}_stage0` Booth encoders, `module_ivp_sem_csa_l0_slice_stage0` carry-save adder slices,
`module_ivp_sign_unsign_xtmulpp_16_8_stage0` signed partial-product unit, `mul32_opcode_stage0..6`)
and the executed `xdref` leaves. The structure is a **Booth-encoded, carry-save (Wallace-tree)
multiply array feeding a same-stage RMW accumulator** — the textbook signed MAC.

```c
// One lane of a signed integer MAC: ivp_mula<W>  (W = lane width 8/16/32).
// vec sources read @stage10; wvec acc read+written @stage12 (the (12,12) self-RMW).
// Result latency 2 cycles (inputs@10 -> acc@12); initiation interval 1 -> a MAC chain
// issues EVERY cycle.  See register-files.md §5.
typedef  int_t<3*W>  acc_t;         // wvec lane: 24 / 48 / 96 bit (3x headroom)

acc_t mac_lane(acc_t acc_in, int_t<W> a, int_t<W> b, mac_dir dir, bool accumulate)
{
    // (1) Partial-product generation — Booth-encoded, sign-aware.
    //     Both operands are SIGNED: sign-extend to the 2W product domain before the
    //     radix-4 Booth recoding.  The xtmulpp unit's "sign_unsign" mux is wired to
    //     signed/signed here (B04); the us/su/uu muxings are B05.
    int_t<2*W> prod = booth_mul_signed(a, b);     // exact 2W-bit signed product

    // (2) Wallace reduction — the partial products collapse through CSA levels
    //     (csa_l0_slice ...) to one carry + one sum, then a final CPA. No rounding,
    //     no truncation: the full 2W-bit product survives into the 3W-bit acc lane.
    acc_t prod_ext = sext<3*W>(prod);             // widen product into the 3W lane

    // (3) Accumulate — modular, NOT saturating (proven by execution, §5.2).
    if (!accumulate)            return wrap<3*W>(prod_ext);         // mul   (overwrite)
    if (dir == MAC_ADD)         return wrap<3*W>(acc_in + prod_ext);// mula  (acc += prod)
    /* dir == MAC_SUB */        return wrap<3*W>(acc_in - prod_ext);// muls  (acc -= prod)
    // wrap<N>(x) = sign_interpret(x mod 2^N): the 3W-bit lane wraps in two's complement.
}
```

For the **packed** (`mulp*`) lanes, step (1) emits *two* partial products per operand word
(`mulpp_8x8`/`mulpp_16_8` slices) and step (2) sums both into one acc lane — a 2-MAC-per-issue
form. For the **quad** (`mulq*`/`mul4t*`) lanes, the `b` operand is a scalar weight broadcast 4-way
(the F4 dual-load supplies `vec` + replicated weight), and four products reduce into one lane. The
reduction tree and accumulate are otherwise identical. `[HIGH/OBSERVED]` on the Booth/CSA structure
and the modular accumulate; `[MED/INFERRED]` on the exact CSA level count (the `csa_l0` tag confirms
≥1 CSA level; the full Wallace depth is the multiplier's, not separately exposed).

### 7.1 The 2-cycle MAC recurrence (carried from register-files §5)

The accumulator is a **same-stage (12,12) read-modify-write**: a MAC reads its `vec` multiplicands
`@10`, reads the running `wvec` accumulator `@12`, multiplies+adds, and writes the accumulator back
`@12`. Write@12 → next-read@12 is a single-cycle bypass, so a MAC chain sustains **II = 1** (one MAC
per cycle) with a **2-cycle result latency**. With only **4** `wvec` entries, at most four
independent accumulation chains can be live; a fifth aliases an in-use accumulator — a
software-scheduling constraint, not a port limit. `[HIGH/OBSERVED]` (see
[register-files §5](../core/register-files.md#5-readwrite-semantics-per-file)).

---

## 8. Readout — the `wvec`→`vec` pack (cross-link to B10)

`wvec` is **never a general source** ([register-files §6.2](../core/register-files.md#62-vec--wvec--pack-write-into-the-accumulator-and-unpack-read-it-back-out)):
the only way to consume an accumulated signed result is a **pack/unpack readout** that moves the
1536-bit accumulator back into a 512-bit `vec` register, applying an `AR`-supplied shift/round and a
narrowing saturation. The `libcas-core` tags confirm the two-sided path: `ivp_sem_wvec_pack_semantic`
rides the Ld slot `@stage3` (the write-into-acc side) and `ivp_sem_unpack_wvec_mov_semantic` rides
the Mul slot `@stage12` (the read-back side). The user opcode is `IVP_PACKVRNX48` (and the two-half
`_0`/`_1` variants). A B04 reduction that has finished accumulating **must** emit a pack before any
store or downstream compute can see the answer.

→ **Full readout semantics — saturation, rounding modes, the two-step full-width pack — are
[Batch 10: wvec Pack (wide→narrow readout)](b10-wvec-pack.md).** B04 owns the *accumulate*; B10 owns
the *narrowing read-out*. `[HIGH/OBSERVED]` on the pack/unpack stage tags and the "never a general
source" property.

---

## 9. The B04 / B05 partition boundary

B04 (this page) is **signed × signed** integer MAC, non-complex. Everything mixed-sign, unsigned, or
complex is [B05](b05-mac-mixed.md). The split is mechanical and arithmetic-checked — **no double
count**:

```
integer-vector MAC mnemonics (libisa-core, drop FP xf16/xf32 + scalar base) = 212
  B04 (this page)  signed×signed, non-complex                                =  71
  B05 (next page)  mixed-sign (us/su) + unsigned (uu) + complex (c/j)        = 141
                                                                       sum   = 212   ✓
  overlap (us/su/uu that are also complex)                                   =   0   ✓
```

The classifier, applied to the `nm` `Opcode_*` mnemonic roster:

| token in mnemonic | meaning | batch |
|---|---|---|
| no `us`/`su`/`uu`, no `c`/`j` lane-type | signed × signed | **B04** |
| `s` immediately after `mul` (e.g. `muls`, `mulsn`) | multiply-**subtract** (still signed) | **B04** |
| `sgn` (`mulsgn*`) | multiply-by-sign (signed) | **B04** |
| `us` (`mulus*`) | unsigned × signed | B05 |
| `su` (`mulsu*`) | signed × unsigned | B05 |
| `uu` (`muluu*`) | unsigned × unsigned | B05 |
| `c` / `j` lane suffix (`*nx16c`, `*nx16j`, `*_2x32c`) | complex / conjugate | B05 |
| `xf16` / `xf32` / `_s` / `_h` scalar-FP | floating-point MAC | B17 / B18 |

> **GOTCHA — the `s` ambiguity is the single trap in this partition.** `muls` (one `s`) =
> multiply-**subtract**, signed, **B04**. `mulsu` (the `su` pair) = signed×unsigned, **B05**. `mulus`
> (the `us` pair) = unsigned×signed, **B05**. The disambiguator is whether the letters after `mul`
> form a recognized **two-letter sign pair** (`us`/`su`/`uu`) or a **single direction letter**
> (`s`=subtract). Executed leaves confirm it: `muls_24_24_8_8` sign-extends *both* operands
> (`movsbl`/`movsbl`) and subtracts the product, whereas `mulsu_24_8_8` sign-extends one and
> zero-extends the other. Mis-binning `muls` into B05 would double-count and corrupt both batches'
> tallies. `[HIGH/OBSERVED]`

---

## 10. Adversarial self-verification — 5 strongest claims, re-challenged

Each claim re-derived against the binary this pass; nothing taken on a report's word.

1. **"71 signed-int MAC mnemonics, 373 placements."** Re-derived: integer-vector MAC roster (drop FP
   + scalar) = 212; the `us`/`su`/`uu`/`c`/`j` classifier removes 141, leaving **71**; summing
   `nm | rg -c 'Opcode_<m>_Slot_*_encode'` over the 71 = **373**. The 71+141=212 partition closes
   with **0** overlap. *Challenge:* could `muls` (subtract) have been wrongly counted as a sign? No —
   the executed leaf `muls_24_24_8_8` sign-extends both operands; it is signed×signed.
   `[HIGH/OBSERVED]`

2. **"Accumulate wraps mod 2^(3L), no saturation."** *Challenge:* maybe the leaf saturates and the
   test just didn't hit the edge. Re-run at the exact edge: `mula_24_24_8_8(0x7FFFFF, 127, 100)` →
   `0x80319b` = the modular wrap of `+8401307`, **not** `0x7FFFFF` (which a saturator would return).
   Same at 48-bit. Saturation is **refuted** for the accumulate. `[HIGH/OBSERVED by execution]`

3. **"Single-replicate quad (`mulq*xr8`/`mulq*xr16`) is F4-exclusive."** *Challenge:* maybe it places
   elsewhere too. `nm | rg 'Opcode_ivp_mulq2n8xr8_Slot_.*_encode'` returns **exactly one** symbol:
   `Slot_f4_s2_mul_encode`. Same for `mulqn16xr16`, `mulqan16xr16`, `mulqn16xr8`, `mulqan16xr8`. The
   dual-load F4 requirement holds. *Counter-scoping (verified this pass):* the `mul4t*` transpose
   forms and the *multi-fanout* `dxr8`/`qxr8` forms correctly place in all 8 wide+N1 slots (they read
   one load), so the F4-exclusivity is scoped to the `mulq*` single-replicate quad only. `[HIGH/OBSERVED]`

4. **"Accumulate is a distinct opcode, not a global bit."** *Challenge:* the 8-bit `mul2nx8`→
   `mula2nx8` delta is `+0x40`, which looks like a bit. Counter-evidence from the same pass:
   `mulnx16`(`0x01003040`)→`mulanx16`(`0x00c570c0`) and `mulq2n8xr8`(`0x88000000`)→`mulqa2n8xr8`
   (`0x8c000000`) vs `mulqn16xr16`(`0x98000000`)→`mulqan16xr16`(`0x90000000`, a *negative* delta).
   No single bit produces all three; accumulate is a distinct iclass per the
   [two-tier model](../core/flix-encoding.md#62-the-two-tier-selector-model). `[HIGH/OBSERVED]`

5. **"`wvec` lanes = `vec` lane count, each 3× taller."** *Challenge:* maybe the lane count changes.
   From the file geometry ([register-files §3](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster)):
   `vec` = 512 bits, `wvec` = 1536 bits = 3×512. The executed leaf widths confirm the per-lane 3×:
   `8→24`, `16→48`, `32→96` — exactly triple. 512/8 = 64 vec lanes ↔ 1536/24 = 64 acc lanes;
   512/16 = 32 ↔ 1536/48 = 32; 512/32 = 16 ↔ 1536/96 = 16. Lane count preserved, width tripled.
   `[HIGH/OBSERVED]`

---

## 11. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| 71 signed-int MAC mnemonics; 373 placements; B04+B05=212, overlap 0 | `[HIGH/OBSERVED]` | `nm libisa-core.so` `Opcode_*` roster + classifier + per-mnemonic `rg -c` |
| Every signed MAC is a `s2_mul`-slot opcode (8 placements typical) | `[HIGH/OBSERVED]` | `Opcode_*_Slot_<f>_s2_mul_encode` symtab |
| Opcode-sel templates (byte-exact `F0_S2_Mul`/`F4` `WORD0`) | `[HIGH/OBSERVED]` | `objdump -d` of the encode thunks this pass |
| Accumulate is a distinct opcode/iclass (no global bit) | `[HIGH/OBSERVED]` | template deltas non-uniform across lane widths |
| Lane→acc widths 8→24 / 16→48 / 32→96 (3× headroom) | `[HIGH/OBSERVED]` | `xdref` leaf width-signatures + `wvec` 1536/`vec` 512 geometry |
| Signed multiply (both operands sign-extended), modular accumulate, no saturate | `[HIGH/OBSERVED by execution]` | ctypes-driven `mul/mula/muls_*` leaves, edge + multi-step |
| Wide-acc retains precision over long reduction (64× max-product, exact) | `[HIGH/OBSERVED by execution]` | `mula_48_48_16_16` ×64 vs reference |
| Single-replicate quad (`xr8`/`xr16`) F4-exclusive | `[HIGH/OBSERVED]` | one placement each = `Slot_f4_s2_mul_encode` |
| Booth + CSA/Wallace multiplier; 2-cycle / II=1 (12,12) RMW accumulate | `[HIGH/OBSERVED]` structure; `[MED/INFERRED]` CSA depth | `libcas-core` `booth_enc`/`csa_l0`/`mul32_stage*` tags + register-files §5 |
| `packl` writes `vec` (low-half), not `wvec` | `[HIGH/OBSERVED]` | leaf `mul_16_16_16_packl` (16-bit output) |
| Mnemonic↔value-leaf join is by `(op,lane,product)` signature, not name | `[HIGH/OBSERVED]` | 26 mnemonics have no name-leaf; all resolve by width |
| `muls` = subtract (B04); `mulsu`/`mulus` = mixed sign (B05) | `[HIGH/OBSERVED]` | executed leaf sign-extension pattern |

---

## 12. Cross-references

- [The FLIX VLIW Encoding (14 format / 46 slot)](../core/flix-encoding.md) — the Mul slot (`s2`) these
  ops occupy, the F4 dual-load format the quad forms require, and the encode-thunk `WORD0` ABI.
- [The Eight Register Files](../core/register-files.md) — the `vec` (multiplicand) / `wvec`
  (accumulator) geometry, the `(12,12)` self-RMW accumulate, and the 2-cycle MAC recurrence.
- [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the certified `12569`
  placement / `864` value-leaf denominators this batch's `373` / executed leaves contribute to.
- [ISA Batch 05 — MAC (mixed-sign/complex/wide-acc)](b05-mac-mixed.md) — the `us`/`su`/`uu`/complex
  half of the integer MAC ISA; the partition boundary is [§9](#9-the-b04--b05-partition-boundary).
- [ISA Batch 10 — wvec Pack (wide→narrow readout)](b10-wvec-pack.md) — the `wvec`→`vec` pack/unpack
  that reads a finished B04 accumulation back into a normal vector register (saturation + rounding).
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED`/`INFERRED`/
  `CARRIED` tags and the proven-by-execution value lane used throughout [§5](#5-value-semantics--driven-live).

---

*Provenance: the encode templates and slot placements are `[HIGH/OBSERVED]` — re-disassembled
in-checkout from `libisa-core.so` (`ncore2gp/config/`); the value semantics in
[§5](#5-value-semantics--driven-live) are `[HIGH/OBSERVED by execution]` — the `libfiss-base.so`
`xdref` leaves were loaded via ctypes and run on the inputs shown; the Booth/CSA multiplier structure
and pipeline stages are `[HIGH/OBSERVED]` from `libcas-core.so` symbol tags (CSA depth `[MED/INFERRED]`).
The `extracted/` carving is gitignored; counts are `nm | rg -c` against the binary `.symtab`. All
prose reads as derived from shipped-artifact static analysis (lawful interoperability RE).*
