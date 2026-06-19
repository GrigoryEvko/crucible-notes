# ISA Batch 08 — Cross-Lane Reduce (horizontal `radd`/`rmax`/`rmin` · reduce-and-flag `rbmax`/`rbmin` · tail-predicate `ltr` · bool fold `randb`/`rorb`)

This is the per-instruction reference for the **cross-lane horizontal reductions** — the only `ivp_`
vector family that **collapses the 32-lane `vec` register down to a scalar/narrowed result** instead
of operating lane-wise. Where [B01](b01-vec-alu-int.md)/[B02](b02-vec-alu-fp.md)/[B03](b03-vec-alu-rest.md)
keep the lane geometry (32×16b in → 32×16b out), B08 **folds across the lane axis**: 32 → 16 → … → 1.
Concretely, B08 owns exactly five sub-families, defined by *what the fold computes and where it writes*:

1. **Horizontal sum** `ivp_radd*` / `ivp_raddu*` / `ivp_radds*` (+ their predicated `…t` merge forms) —
   sum the 32 (or 64 / 16) lanes into a **widened** scalar accumulator (`nx16` → 32-bit), signed,
   unsigned, or fractional-saturating. **12 mnemonics.**
2. **Horizontal min / max** `ivp_rmax*` / `ivp_rmaxu*` / `ivp_rmin*` / `ivp_rminu*` (int) and
   `ivp_rmaxnum*` / `ivp_rminnum*` (NaN-skipping fp16/fp32), with `…t` predicated forms — fold to the
   single extremal lane value. **24 mnemonics.**
3. **Reduce-and-flag min / max** `ivp_rbmax*` / `ivp_rbmin*` (int) and `ivp_rbmaxnum*` / `ivp_rbminnum*`
   (fp) — same fold as (2) **plus a `vbool` predicate** marking the lane(s) that held the extremum (the
   horizontal **argmax / argmin**). **20 mnemonics.**
4. **Tail-predicate / lane-threshold reduce** `ivp_ltr*` / `ivp_ltrs*` (+ immediate `…i` and high-half
   `…_2` variants) — take a scalar lane count `n` and **emit a `vbool`** with the first `n` lanes set
   (`vb[l] = (l < n)`). This is the canonical *vector-length / reduction-tail* mask. **9 mnemonics.**
5. **Boolean fold** `ivp_randb*` (reduce-AND) / `ivp_rorb*` (reduce-OR) — collapse a 64-bit `vbool`
   predicate to a **single bit** (all-lanes-true / any-lane-true). **6 mnemonics.**

**71 mnemonics / 591 placements** of the 1534 / 12569 certified-perfect cover
([coverage-tally](../core/coverage-tally.md)). The encoding is read out of the non-stripped
`libisa-core.so` (`Opcode_*_encode` selector thunks); the **value semantics are proven by execution**
by driving the matching `module__xdref_*` leaves in `libfiss-base.so` live via `ctypes` (license-free
value lane). Every numeric claim is anchored to a binary address or a live call and tagged
`[HIGH/OBSERVED]` unless flagged. The prose is derived from static analysis and in-process execution of
the shipped artifacts only.

> **The B08 boundary vs B01/B02/B03 (read this first).** The discriminator is the **lane axis**, not the
> register file:
>
> | | reads | writes | lane geometry | owner |
> |---|---|---|---|---|
> | lane-wise int/fp ALU | `vec` | `vec` (+`vbool` flag) | **32 → 32** (per-lane) | **B01/B02/B03** |
> | **cross-lane reduce (this page)** | `vec` (whole 512b) | **scalar/narrowed** `vec` lane, or `vbool` | **32 → 1** (horizontal fold) | **B08** |
>
> The per-lane min/max `ivp_maxnx16` (B01) and its flag form `ivp_bmaxnx16` (B03) keep 32 outputs; the
> **horizontal** `ivp_rmaxnx16` (B08) collapses to **one**. The reduce-and-flag `ivp_rbmaxnx16` (B08) is
> the horizontal *argmax* — distinct from B03's per-lane `ivp_bmaxnx16`, which flags *which operand* won
> *per lane*. B03 explicitly cedes the reduce-and-flag `r*t` / `ltr*` forms to B08
> ([b03 §7](b03-vec-alu-rest.md)). The lane-wise conditional `ivp_minormax2nx8` (a per-lane min-OR-max
> select, `_8_8_8_1` leaf) is **not** a reduce — it stays in B01/B03. `[HIGH/OBSERVED]`

---

## 1. Batch facts

| Fact | Value | Binary witness |
|---|---|---|
| Mnemonics in batch | **71** | `nm \| rg` roster, §8 |
| Placements in batch | **591** | per-mnemonic `Opcode_*_Slot_*_encode` counts (§8) |
| Arithmetic-reduce slot | **`s3_alu`** (9 slots int / 6 fp) | `radd/rmax/rmin/rbmax/rbmin` placements |
| Bool/tail-predicate slot | **`s1_ld`** (the **load pipe**) | `ltr*/randb*/rorb*` placements (§3.3) |
| Sum source / dest | `vec` idx2 512b → 32-bit scalar lane (widening) | [register-files §2](../core/register-files.md) |
| Min/max dest | one `vec` lane (no widen) | same |
| Reduce-and-flag dest | one `vec` lane **+** `vbool` idx3 predicate | [register-files §3](../core/register-files.md) |
| Tail-predicate dest | `vbool` idx3, 2 bits / 16-bit lane | live `ltrn` |
| Bool-fold src/dest | `vbool` 64b → 1 bit | live `randbn`/`rorbn` |
| FLIX byte-size | **8** (the wide `s3_alu`/`s1_ld` formats) | `formats[].length` |
| Value leaves driven live | `radd/raddu/radds_nx16`, `rmax/rmaxu_nx16`, `ltrn/ltrn_2/ltrs2n`, `randbn/rorbn`, `rbmax_16` | §4–§5 |
| Confidence | `[HIGH/OBSERVED]` (encoding + execution) | — |

The batch is a **pure subset** of `xt_ivp32` (the 1072-op vector package) and is entirely `ivp_`-prefixed
(it contributes only to the **1065** vector axis). It draws on the FLIX grid
([flix-encoding](../core/flix-encoding.md): 14 formats / 46 slots, classes → `{2,3,8,16}`), the `vec`
source ([register-files](../core/register-files.md): `vec` idx2 512b×32), and the `vbool` file
(`vbool` idx3 64b×16) for the flag/tail/bool forms.

---

## 2. Roster

`FLIX format·slot` names the representative placement used for the selector column (`f0_s3_alu` for
arithmetic reduces; `f0_s1_ld` for the load-pipe `ltr*/randb*/rorb*`). `opcode-sel word0` is the
`movl $imm,(%rdi)` operand of that placement's `Opcode_<mn>_Slot_<slot>_encode` thunk
([flix-encoding §6.1](../core/flix-encoding.md); `word1 == 0` with zero exceptions, per
[template §3.1](template-and-partition.md)). `dest` names the result file and width. `[conf]` is
`[HIGH/OBSERVED]` for every row.

### 2.1 Horizontal sum — `radd*` (12 mnemonics, 108 placements)

| mnemonic | format·slot | word0 | src·dest | lanes → acc | one-line semantics | n |
|---|---|---|---|---|---|--:|
| `ivp_raddnx16`    | F0·S3_ALU | `0x869080f1` | vec → 32b | 32×16s → 32b | `Σ sext16(lane)`, **widening, exact** | 9 |
| `ivp_radd2nx8`    | F0·S3_ALU | `0x869000f1` | vec → 16b | 64×8s → 16b | `Σ sext8(lane)`, widening | 9 |
| `ivp_raddn_2x32`  | F0·S3_ALU | `0x869100f1` | vec → 32b | 16×32s → 32b | `Σ lane` (no widen, wraps mod 2³²) | 9 |
| `ivp_raddunx16`   | F0·S3_ALU | `0x869280f1` | vec → 32b | 32×16u → 32b | `Σ zext16(lane)`, unsigned widening | 9 |
| `ivp_raddu2nx8`   | F0·S3_ALU | `0x869200f1` | vec → 16b | 64×8u → 16b | unsigned widening sum | 9 |
| `ivp_raddsnx16`   | F0·S3_ALU | `0x869180f1` | vec → 16b | 32×16s → 16b | **fractional-saturating** packed sum (§4.1) | 9 |
| `ivp_raddnx16t`   | F0·S3_ALU | `0x86900060` | vec(m)·vb(in) → 32b | predicated | `Σ` over **masked** lanes only | 9 |
| `ivp_radd2nx8t`   | F0·S3_ALU | `0x86900050` | vec(m)·vb | predicated | masked 8-bit widening sum | 9 |
| `ivp_raddn_2x32t` | F0·S3_ALU | `0x86900070` | vec(m)·vb | predicated | masked 32-bit sum | 9 |
| `ivp_raddunx16t`  | F0·S3_ALU | `0x86900061` | vec(m)·vb | predicated | masked unsigned widening sum | 9 |
| `ivp_raddu2nx8t`  | F0·S3_ALU | (alu) | vec(m)·vb | predicated | masked 8-bit unsigned sum | 9 |
| `ivp_raddsnx16t`  | F0·S3_ALU | `0x86900041` | vec(m)·vb | predicated | masked saturating sum | 9 |

### 2.2 Horizontal min / max — `rmax*` / `rmin*` (24 mnemonics, 192 placements)

| mnemonic | format·slot | word0 | src·dest | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_rmaxnx16`    | F0·S3_ALU | `0x869480f1` | vec → 16b | `max_s` over 32×16b lanes | 9 |
| `ivp_rmaxunx16`   | F0·S3_ALU | `0x869600f1` | vec → 16b | `max_u` over 32×16b | 9 |
| `ivp_rmax2nx8`    | F0·S3_ALU | `0x869300f1` | vec → 8b | `max_s` over 64×8b | 9 |
| `ivp_rmaxu2nx8`   | F0·S3_ALU | `0x869580f1` | vec → 8b | `max_u` over 64×8b | 9 |
| `ivp_rmaxn_2x32`  | F0·S3_ALU | `0x869500f1` | vec → 32b | `max_s` over 16×32b | 9 |
| `ivp_rmaxun_2x32` | F0·S3_ALU | (alu) | vec → 32b | `max_u` over 16×32b | 9 |
| `ivp_rmaxnx16t`   | F0·S3_ALU | `0x86980050` | vec(m)·vb | predicated `max_s` (masked lanes only) | 9 |
| `ivp_rmaxunx16t`  | F0·S3_ALU | `0x86980060` | vec(m)·vb | predicated `max_u` | 9 |
| `ivp_rminnx16`    | F0·S3_ALU | `0x84820070` | vec → 16b | `min_s` over 32×16b | 9 |
| `ivp_rminunx16`   | F0·S3_ALU | `0x84920060` | vec → 16b | `min_u` over 32×16b | 9 |
| `ivp_rmin2nx8`    | F0·S3_ALU | `0x869700f1` | vec → 8b | `min_s` over 64×8b | 9 |
| `ivp_rminu2nx8`   | F0·S3_ALU | `0x848a0070` | vec → 8b | `min_u` over 64×8b | 9 |
| `ivp_rminn_2x32`  | F0·S3_ALU | `0x848a0060` | vec → 32b | `min_s` over 16×32b | 9 |
| `ivp_rminun_2x32` | F0·S3_ALU | (alu) | vec → 32b | `min_u` over 16×32b | 9 |
| `ivp_rminnx16t`   | F0·S3_ALU | `0x86980051` | vec(m)·vb | predicated `min_s` | 9 |
| `ivp_rminunx16t`  | F0·S3_ALU | `0x86980061` | vec(m)·vb | predicated `min_u` | 9 |
| `ivp_rmaxnumnxf16` | F0·S3_ALU | `0x869380f1` | vec(f16) → f16 | NaN-skipping horizontal `fmax` | 6 |
| `ivp_rmaxnumn_2xf32` | F0·S3_ALU | `0x869400f1` | vec(f32) → f32 | NaN-skipping horizontal `fmax` | 6 |
| `ivp_rmaxnumnxf16t` | F0·S3_ALU | `0x86900071` | vec(m)·vb | predicated fp `rmaxnum` | 6 |
| `ivp_rmaxnumn_2xf32t` | F0·S3_ALU | `0x86980040` | vec(m)·vb | predicated fp32 `rmaxnum` | 6 |
| `ivp_rminnumnxf16` | F0·S3_ALU | `0x869780f1` | vec(f16) → f16 | NaN-skipping horizontal `fmin` | 6 |
| `ivp_rminnumn_2xf32` | F0·S3_ALU | `0x84820060` | vec(f32) → f32 | NaN-skipping horizontal `fmin` | 6 |
| `ivp_rminnumnxf16t` | F0·S3_ALU | `0x86980070` | vec(m)·vb | predicated fp16 `rminnum` | 6 |
| `ivp_rminnumn_2xf32t` | F0·S3_ALU | `0x86980041` | vec(m)·vb | predicated fp32 `rminnum` | 6 |

### 2.3 Reduce-and-flag min / max — `rbmax*` / `rbmin*` (20 mnemonics, 156 placements)

The `rb`-prefix is the **`r`(educe) + `b`(flag)** compound: the reduce writes the extremum **and** a
`vbool` marking the argmax/argmin lane(s). The leaf signature carries the extra `_64_` predicate output
(`rbmax_nx16_64_512_512`).

| mnemonic | format·slot | word0 | dest | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_rbmaxnx16`    | F0·S3_ALU | `0x84808040` | 16b·vb | `max_s` + `vb` = argmax lanes | 9 |
| `ivp_rbmaxunx16`   | F0·S3_ALU | `0x84808070` | 16b·vb | `max_u` + argmax | 9 |
| `ivp_rbmax2nx8`    | F0·S3_ALU | `0x84800050` | 8b·vb | `max_s` 64×8b + argmax | 9 |
| `ivp_rbmaxu2nx8`   | F0·S3_ALU | `0x84808060` | 8b·vb | `max_u` 64×8b + argmax | 9 |
| `ivp_rbmaxn_2x32`  | F0·S3_ALU | `0x84808050` | 32b·vb | `max_s` 16×32b + argmax | 9 |
| `ivp_rbmaxun_2x32` | F0·S3_ALU | `0x84810040` | 32b·vb | `max_u` 16×32b + argmax | 9 |
| `ivp_rbminnx16`    | F0·S3_ALU | `0x84818040` | 16b·vb | `min_s` + `vb` = argmin lanes | 9 |
| `ivp_rbminunx16`   | F0·S3_ALU | `0x84818070` | 16b·vb | `min_u` + argmin | 9 |
| `ivp_rbmin2nx8`    | F0·S3_ALU | `0x84810050` | 8b·vb | `min_s` 64×8b + argmin | 9 |
| `ivp_rbminu2nx8`   | F0·S3_ALU | `0x84818060` | 8b·vb | `min_u` 64×8b + argmin | 9 |
| `ivp_rbminn_2x32`  | F0·S3_ALU | `0x84818050` | 32b·vb | `min_s` 16×32b + argmin | 9 |
| `ivp_rbminun_2x32` | F0·S3_ALU | (alu) | 32b·vb | `min_u` 16×32b + argmin | 9 |
| `ivp_rbmaxnumnxf16` | F0·S3_ALU | `0x84800060` | f16·vb | fp16 `fmax` + argmax (NaN-skip) | 6 |
| `ivp_rbmaxnumn_2xf32` | F0·S3_ALU | `0x84800070` | f32·vb | fp32 `fmax` + argmax | 6 |
| `ivp_rbmaxnumnxf16t` | F0·S3_ALU | `0x84800000` | f16·vb | predicated fp16 `rbmaxnum` | 6 |
| `ivp_rbmaxnumn_2xf32t` | F0·S3_ALU | (alu) | f32·vb | predicated fp32 `rbmaxnum` | 6 |
| `ivp_rbminnumnxf16` | F0·S3_ALU | `0x84810060` | f16·vb | fp16 `fmin` + argmin | 6 |
| `ivp_rbminnumn_2xf32` | F0·S3_ALU | `0x84810070` | f32·vb | fp32 `fmin` + argmin | 6 |
| `ivp_rbminnumnxf16t` | F0·S3_ALU | `0x84800020` | f16·vb | predicated fp16 `rbminnum` | 6 |
| `ivp_rbminnumn_2xf32t` | F0·S3_ALU | (alu) | f32·vb | predicated fp32 `rbminnum` | 6 |

### 2.4 Tail-predicate reduce — `ltr*` (9 mnemonics, 81 placements) · runs in the **load slot**

| mnemonic | format·slot | word0 | dest | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_ltrni`   | F0·S1_LD | `0x004a00e0` | vb | `vb[l] = (l < n)`, 32×16b lanes, **n as register** | 9 |
| `ivp_ltrn`    | F0·S1_LD | `0x004a07e0` | vb | tail mask, 32-lane (`AR` count) | 9 |
| `ivp_ltrn_2`  | F0·S1_LD | `0x004a08e0` | vb | tail mask, **high 16-lane half** (`n & 0xf`) | 9 |
| `ivp_ltrn_2i` | F0·S1_LD | `0x004a04e0` | vb | high-half, **immediate n** | 9 |
| `ivp_ltr2n`   | F0·S1_LD | `0x004a06e0` | vb | tail mask, **64-lane** (`2nx8` view) | 9 |
| `ivp_ltr2ni`  | F0·S1_LD | `0x004a18d0` | vb | 64-lane, immediate n | 9 |
| `ivp_ltrsn`   | F0·S1_LD | `0x004a0ae0` | vb | tail mask, **count saturated** to lane range (§4.4) | 9 |
| `ivp_ltrsn_2` | F0·S1_LD | `0x004a0be0` | vb | saturating, high half | 9 |
| `ivp_ltrs2n`  | F0·S1_LD | `0x004a09e0` | vb | saturating, 64-lane | 9 |

### 2.5 Boolean fold — `randb*` (reduce-AND) / `rorb*` (reduce-OR) (6 mnemonics, 54 placements) · load slot

| mnemonic | format·slot | word0 | src·dest | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_randbn`   | F0·S1_LD | `0x004a13e0` | vb → 1 bit | **AND** of all 32 lane predicates (all-true) | 9 |
| `ivp_randb2n`  | F0·S1_LD | `0x004a12e0` | vb → 1 bit | AND of all 64 lane predicates | 9 |
| `ivp_randbn_2` | F0·S1_LD | `0x004a14e0` | vb → 1 bit | AND over the high lane half | 9 |
| `ivp_rorbn`    | F0·S1_LD | `0x004a16e0` | vb → 1 bit | **OR** of all 32 lane predicates (any-true) | 9 |
| `ivp_rorb2n`   | F0·S1_LD | `0x004a15e0` | vb → 1 bit | OR of all 64 lane predicates | 9 |
| `ivp_rorbn_2`  | F0·S1_LD | `0x004a17e0` | vb → 1 bit | OR over the high lane half | 9 |

---

## 3. Encoding

### 3.1 Two selector bands — arithmetic reduces are ALU-band, fold/predicate reduces are load-band

The single sharpest encoding fact about B08 is that it splits across **two FLIX issue slots**, and the
two halves live in **two distinct selector bands** of `word0`:

* **Arithmetic reduces** (`radd/rmax/rmin/rbmax/rbmin`, int and fp) are `s3_alu` ops — their `word0`
  templates sit in the **`0x8480xxxx` / `0x8690xxxx` ALU band**, the same high-byte region as the B01/B03
  lane-wise ALU. Re-read byte-exact this pass (`Opcode_ivp_<mn>_Slot_f0_s3_alu_encode`):
  `raddnx16 = 0x869080f1`, `rmaxnx16 = 0x869480f1`, `rbmaxnx16 = 0x84808040`.
* **Fold / tail-predicate reduces** (`ltr*/randb*/rorb*`) are `s1_ld` ops — they run in the **load pipe**,
  and their `word0` templates sit in the **`0x004axxxx` load band** (`0x4a` is the load-slot opcode
  region). Re-read this pass (`Opcode_ivp_<mn>_Slot_f0_s1_ld_encode`):
  `ltrn = 0x004a07e0`, `randbn = 0x004a13e0`, `rorbn = 0x004a16e0`.

> **QUIRK — the boolean / tail-predicate reductions issue from the LOAD slot, not an ALU slot.** A
> reimplementer wiring up the scheduler will mis-place `ltr*`/`randb*`/`rorb*` if they assume "all reduces
> are ALU ops". The placement census is unambiguous: `ivp_ltrn` is legal in **`f{0..7}_s1_ld`**,
> **`n2_s1_ld`**, and **`f11_s1_alu`** — the *load* slot of eight wide formats plus one ALU fallback — and
> carries **no `s3_alu` placement at all**. The horizontal sum/min/max, by contrast, live **only** in
> `s3_alu` and carry **no load-slot placement**. This is why B03 routes them here, calling B08 the
> "reduce tree (loads, `f0_s1_ld`)" batch ([b03 §7](b03-vec-alu-rest.md)). The HW reason: the lane-fold
> network reuses the load/permute crossbar (the same datapath that feeds `valign`), so the bool fold and
> the lane-count→mask expansion ride it; the arithmetic accumulator tree hangs off the ALU.
> `[HIGH/OBSERVED]`

### 3.2 The load-band selector is a clean enumerated counter

Within the `s1_ld` band the low bytes form a tight, contiguous enumeration (the `0x4a..e0` family),
re-read byte-exact from the `f0_s1_ld` thunks this pass:

```
ltrni    0x004a00e0   ltrn_2i  0x004a04e0   ltr2n   0x004a06e0   ltrn   0x004a07e0
ltrn_2   0x004a08e0   ltrs2n   0x004a09e0   ltrsn   0x004a0ae0   ltrsn_2 0x004a0be0
randb2n  0x004a12e0   randbn   0x004a13e0   randbn_2 0x004a14e0
rorb2n   0x004a15e0   rorbn    0x004a16e0   rorbn_2  0x004a17e0
ltr2ni   0x004a18d0
```

The selector is `(0x4a<<16) | (sub<<4) | tail`, with `tail = 0xe0` for the **2-operand** forms (count +
dest) and `0xd0` for `ltr2ni` (the one immediate-count form with a different operand count). The `ltr`
group occupies sub-codes `0x0..0xb`, the bool-fold group `0x12..0x17`.

> **GOTCHA — `word0` drifts per format; quote only the representative slot.** The `f0_s1_ld` value above
> is the canonical selector, but the **same mnemonic encodes differently in every other format**: e.g.
> `ivp_ltrsn` is `0x004a0ae0` in `f0_s1_ld` but `0x000bca00` in `f11_s1_alu`, `0x005e0920` in `f1_s1_ld`,
> `0x00221120` in `f2_s1_ld`. This is the two-tier format-local selector model of
> [flix-encoding §6.2](../core/flix-encoding.md); the value **semantics are slot-invariant** but the
> opcode word is not. Encode by table lookup, never by a global formula. `[HIGH/OBSERVED]`

### 3.3 The `…t` predicated-reduce forms drop into a distinct sub-band

The 18 `…t` forms (predicated reduce-merge: reduce over the **masked** lanes only, leave the destination's
old value where the mask is clear) are **separate opcodes** with their own selector, not a bit-flip of the
base. The non-`t` arithmetic reduce ends in `…00f1`/`…80f1` (the `0xf1` low byte marks the plain
whole-vector reduce); the `t` form drops to `…0040`…`…0071`:

| base reduce | word0 | `…t` predicated form | word0 |
|---|---|---|---|
| `ivp_raddnx16` | `0x869080f1` | `ivp_raddnx16t` | `0x86900060` |
| `ivp_rmaxnx16` | `0x869480f1` | `ivp_rmaxnx16t` | `0x86980050` |
| `ivp_rminnx16` | `0x84820070` | `ivp_rminnx16t` | `0x86980051` |
| `ivp_raddsnx16`| `0x869180f1` | `ivp_raddsnx16t`| `0x86900041` |

The `t` band clusters in `0x8690xxxx`/`0x8698xxxx`; the discriminator is the **operand direction byte**
(the destination becomes `0x6d` 'm'/inout and a `vbool` mask `in` operand is added), exactly as for the
B03 `*t` forms ([b03 §3.4](b03-vec-alu-rest.md)). The `vbool` mask is the **lane-select for the
reduction**: only masked-in lanes contribute to the fold, so `ivp_raddnx16t` is a *segmented* / *masked*
horizontal sum. `[HIGH/OBSERVED]` on the selector words; `[MED/INFERRED]` that the mask gates fold
membership (the leaf `radd_nx16_32_512_t` exists @ `0x85ab00` and takes the extra mask argument, but the
in-process drive in §5 covers the non-`t` base — the `t` semantics are read from the leaf body, not yet
sweep-validated).

### 3.4 Reduce-and-flag operand shape — two outputs (data + `vbool`)

The `rbmax*`/`rbmin*` reduce-and-flag forms carry a **second output**: the value leaf signature
`rbmax_nx16_64_512_512` reads as `(out0 = 64-bit vbool flag, in = 512-bit vec, scratch = 512)`, and the
full-reduce body (`module__xdref_rbmax_nx16_64_512_512` @ `0x859f00`) writes **two** pointers (`%r14` =
data, `%r13` = predicate). This mirrors B03's B-variant operand discipline (two `0x6f` 'o' bytes), but
where B03's `bmax` flag is **per-lane** ("did operand a win in this lane?"), B08's `rbmax` flag is
**cross-lane** ("which lane(s) held the maximum?") — the horizontal argmax. `[HIGH/OBSERVED]`

---

## 4. Core algorithm — annotated C

The reduction is, in hardware, a **log-step (tree) fold** across the lane axis: 32 → 16 → 8 → 4 → 2 → 1,
five binary-op stages, each halving the live lane count by applying the lane-pair op (`+` / `max` / `min`
/ `&` / `|`) to neighbouring partial results. The reference value model in `libfiss-base.so` implements
the *same fold as a linear scan* (the result is associativity-invariant for `+`/`max`/`min`/`&`/`|`, so
linear and tree agree bit-for-bit); the HW does the tree for latency. Each pseudocode below names the
**real `module__xdref_*` leaf** it ports and cites its address.

### 4.1 (a) Horizontal sum, widening + the saturating variant — `radd` / `radds`

The plain `radd` widens each lane on the way into the accumulator so the fold **cannot overflow**. Leaf
`module__xdref_radd_nx16_32_512` (`0x858690`), annotated:

```c
// module__xdref_radd_nx16_32_512  @ 0x858690
// ABI: rdi = ctx (unused), rsi = pointer to 512-bit vec (32 × int16), rdx = pointer to int32 out
void radd_nx16(const int16_t in[32], int32_t *out) {
    int32_t acc = 0;
    for (int l = 0; l < 32; ++l)
        acc += (int32_t)sext_32_16(in[l]);   // each lane SIGN-EXTENDED to 32b before add (sext_32_16@plt)
    *out = acc;                              // 0x858979: mov %eax,(%rbx) — exact 32-bit sum, NO saturate
}
```

Because the accumulator is 32-bit and 32 × |int16| ≤ 32 × 2¹⁵ = 2²⁰ ≪ 2³¹, **`radd` is exact and never
saturates**. `raddu` is identical with `zext` (unsigned). `radd2nx8` widens 8→16 (64 lanes); `raddn_2x32`
does **not** widen (16×32b → 32b) and so **wraps mod 2³²** — the one non-widening sum.

The saturating form `radds` (`module__xdref_radds_nx16_16_512` @ `0x814970`) is different: it accumulates
in a **21-bit** intermediate (`and $0x1fffff` after each `movswl`) — 16 value bits + 5 = log₂32 carry bits
— then **packs the result back into a 16-bit field with a sign bit** rather than clamping to int16:

```c
// module__xdref_radds_nx16_16_512  @ 0x814970   (saturating / fractional-pack reduce)
void radds_nx16(const int16_t in[32], uint16_t *out) {
    int32_t acc = 0;
    for (int l = 0; l < 32; ++l)
        acc = (acc + (sext16(in[l]) & 0x1FFFFF)) & 0x1FFFFF;   // 21-bit running accumulator
    // tail (0x814b70): re-pack the 21-bit acc into a 16-bit {sign, 15-bit} field — NOT a plain int16 clamp
    uint16_t sign = (acc >> 5)  & 0x8000;
    uint16_t mag  = (acc >> 20) & 0x7FFF;   // (or the low-15 path when in range)
    *out = sign | mag;
}
```

> **QUIRK — `radds` is a *fractional / packed* saturating reduce, not an int16 clamp.** Driven live
> (§5.1): `radds(32 × 32767)` returns **`0x7FFF` (= 32767)**, but `radds(32 × −32768)` returns **`0x8000`
> (= 32768 unsigned / the sign-flagged field)**, *not* a clamp to `INT16_MIN`. The 21-bit accumulator
> with a sign-and-magnitude re-pack is the fixed-point/normalized saturation idiom (the same
> "normalize-not-clamp" philosophy as B03's `baddnorm`, [b03 §4.3](b03-vec-alu-rest.md)), aimed at
> fixed-point dot-product tails where the sum is interpreted as a Q-format fraction. A reimplementer who
> models `radds` as `clamp(Σ, INT16_MIN, INT16_MAX)` will diverge on every overflow case. `[HIGH/OBSERVED
> by execution]`

### 4.2 (b) Horizontal min / max — the linear/tree fold, signed vs unsigned — `rmax`

Leaf `module__xdref_rmax_nx16_16_512` (`0x858990`) folds with the per-lane primitive `max_16_16_16`:

```c
// module__xdref_rmax_nx16_16_512  @ 0x858990
// ABI: rsi = 512-bit vec (32 × int16), rdx = int16 out
void rmax_nx16(const int16_t in[32], int16_t *out) {
    int16_t acc = max_s16(in[0], in[1]);        // 0x8589ae: first call splits the lane-0 dword (lo,hi)
    for (int l = 2; l < 32; ++l)                //   movzwl %si ; shr $0x10,%esi — two lanes from one dword
        acc = max_s16(acc, in[l]);              // 0x8589c3.. chained max_16_16_16@plt
    *out = acc;                                 // result is one 16-bit lane (NO widen for min/max)
}
```

The **signed vs unsigned divergence** is the whole point of the `rmax`/`rmaxu` pairing: `max_16_16_16`
treats lanes as signed (sign-bit-flip bias), `maxu` as unsigned. Driven live (§5.2): over
`[−1, +1, …]`, **signed `rmax` → +1** but **unsigned `rmaxu` → 0xFFFF** (because `0xFFFF > 1` unsigned).
Min is the polarity flip. The HW tree applies the same `max` to 16 lane-pairs in stage 1, 8 in stage 2,
… — identical result, log-depth latency.

The fp forms `rmaxnum`/`rminnum` use the **`num` (NaN-skipping)** primitive: a NaN lane is treated as the
identity (−∞ for max, +∞ for min) so it never poisons the reduction — the IEEE-754-2019 `maximumNumber`
/ `minimumNumber` semantics, matching the scalar-FP family ([fp-sub-isa](../core/fp-sub-isa.md)).
`[HIGH/OBSERVED]` on the int divergence (execution); `[MED/INFERRED]` on the fp NaN-skip (read from the
`num` leaf name + body, not sweep-driven this pass).

### 4.3 (c) Reduce-and-flag — the argmax/argmin predicate write — `rbmax`

The reduce-and-flag fold carries **two** running quantities: the extremum value and a `vbool` whose lane
bits mark where the extremum was achieved. The per-step primitive `module__xdref_rbmax_16` (`0x859e60`)
is the tree node:

```c
// module__xdref_rbmax_16  @ 0x859e60   (one fold step: combine new lane into {value, argmax-pred})
// ABI: rdi=ctx, esi=new_lane16, edx=new_lane_index, rcx=accum_in{val@0,pred@4}, r8=out{val,pred}
void rbmax_step(int16_t nv, int lane, const acc_t *in, acc_t *out) {
    uint32_t bit  = 1u << lane;                 // 0x859e6f: 1<<lane  (this lane's predicate bit)
    uint16_t nu   = (uint16_t)nv ^ 0x8000;      // signed compare via sign-flip bias
    uint16_t au   = (uint16_t)in->val ^ 0x8000;
    if (nu >  au) { out->val = nv;       out->pred = bit;            }  // 0x859ec0: strictly greater -> replace, fresh flag
    else if (nu == au) { out->val = in->val; out->pred = in->pred | bit; } // 0x859ed8: TIE -> KEEP value, ACCUMULATE flag bit
    else          { out->val = in->val;  out->pred = in->pred;       }  // 0x859eaf: smaller -> keep
}
```

> **QUIRK — the argmax flag is an *inclusive* tie set: every lane equal to the maximum gets its bit set.**
> The `je` (equal) branch at `0x859ed8` **ORs** the new lane's bit into the running predicate rather than
> replacing it, so `ivp_rbmaxnx16` over a vector with the maximum value in lanes 1 and 7 produces a `vbool`
> with **both** bits 1 and 7 set — not a single "first/last argmax". A reimplementer building a
> one-hot-argmax on top of `rbmax` must add a priority-encode pass; the ISA primitive returns the full
> equality set. `[HIGH/OBSERVED]` (tie branch disassembled; value side driven live in §5.3).

### 4.4 (d) Tail-predicate reduce — lane-count → `vbool` mask — `ltrn`

`ltr` is the inverse of a reduction: it takes a scalar lane count and **expands** it into a per-lane
predicate. Leaf `module__xdref_ltrn_64_32` (`0x856fe0`):

```c
// module__xdref_ltrn_64_32  @ 0x856fe0
// ABI: rdi=ctx, esi=n (lane count), rdx=64-bit vbool out
void ltrn(int n, uint64_t *out) {
    uint32_t ones = (1u << n) - 1;              // 0x856fe7: shl %cl ; sub $1 — low n bits set
    uint64_t vb = 0;
    for (int l = 0; l < 32; ++l)                // each of the low n bits expands to a 2-bit vbool lane
        if (ones & (1u << l)) vb |= (uint64_t)0x3 << (2*l);   // 2 predicate bits per 16-bit lane
    *out = vb;                                  // vb[l] = (l < n)
}
```

So `ltrn(n)` is the **vector-length / reduction-tail mask**: `vb[l] = (l < n)`, with the **2-bits-per-lane
`vbool` geometry** ([register-files §3](../core/register-files.md)). Driven live (§5.4): `ltrn(1)=0x3`,
`ltrn(8)=0xFFFF` (8 lanes × 2 bits), `ltrn(16)=0xFFFFFFFF`. The saturating variant `ltrs*`
(`module__xdref_ltrs2n_64_32` @ `0x814420`) **clamps the count** first — it tests the sign of `n` (`js`)
and caps at `0x3f` (63 for the 64-lane `2n` form) before building the mask — so a negative or
out-of-range `n` produces a safe all-zero or all-ones mask instead of wrapping. The `_2` forms mask
`n & 0xf` (the high 16-lane sub-block). The composition `ivp_raddnx16t( …, ltrn(n) )` is the idiom for a
**dynamic-length horizontal sum** (sum only the first `n` valid lanes).

> **GOTCHA — `ltrn(32)` wraps to all-zero, not all-ones.** The mask is built with `(1u << n) - 1` and
> **no clamp** in the plain `ltrn`; with `n = 32` the host `shl` is `1u << 32 == 1` (mod-32 shift count on
> x86), giving `ones = 0` and **`vb = 0`** — confirmed live: `ltrn(32) = 0x0`. The architecturally useful
> range is therefore `n ∈ [0, 31]` for the 32-lane `ltrn` (`[0,63]` for `ltr2n`); to mask *all* lanes you
> must use the saturating `ltrsn` (which clamps) or `n = 31`. The reference ISS reproduces the wrap, so it
> is a **real HW edge**, not a host artifact. `[HIGH/OBSERVED by execution]`

### 4.5 (e) Boolean fold — `vbool` → 1 bit — `randbn` / `rorbn`

Leaf `module__xdref_randbn_64_64` (`0x81cc90`) reads a 64-bit `vbool` (two dwords) and ANDs the per-lane
predicate bits:

```c
// module__xdref_randbn_64_64  @ 0x81cc90   (reduce-AND: all lanes true?)
// module__xdref_rorbn_64_64   @ 0x81ce30   (reduce-OR : any lane true?)
// ABI: rsi = 64-bit vbool in, rdx = 64-bit vbool out (result in low bit)
void randbn(uint64_t vb, uint64_t *out) {
    int acc = 1;
    for (int l = 0; l < 32; ++l)
        acc &= (vb >> (2*l)) & 1;      // LOW bit of each 2-bit lane is the predicate; high bit ignored
    *out = acc;                        // 1 iff every lane's predicate bit is set
}
```

`rorbn` replaces `&=` / seed `1` with `|=` / seed `0`. Driven live (§5.5): the predicate is the **low bit
of each 2-bit lane pair** — `randbn(0x5555…) = 1` (every low bit set), `randbn(0xAAAA…) = 0` (only high
bits set → no lane true). These are the canonical **all-true** (`randb`) and **any-true** (`rorb`)
horizontal tests that drive a scalar branch off a vector mask.

---

## 5. Driven live — `libfiss-base.so` value leaves via `ctypes`

`libfiss-base.so` (sha256 `260b110c…`, 864 `module__xdref_*` leaves) is callable in-process with no
license. The **whole-vector reduce leaves** take a *pointer to a 512-bit `vec`* in `rsi` and write through
an output pointer — distinct from the simple element leaves of [template §5.2](template-and-partition.md)
(which take a scalar in `rsi`). Eleven leaves driven live this pass; transcripts are reproducible with
`ctypes.CDLL("libfiss-base.so")`.

### 5.1 Horizontal sum — overflow/saturation (the required sum case)

```
== radd (signed widening 16->32, exact, NO saturate) ==
radd(32 × 1)       = 32           (trivial)
radd(32 × 32767)   = 1048544      (= 32×32767 exactly — 32-bit accumulator, no overflow)
radd(32 × -32768)  = -1048576     (= 32×-32768 exactly)
radd([100,-50,0…]) = 50

== raddu (unsigned widening) ==
raddu(32 × 0xFFFF) = 0x1FFFE0     (= 32×65535 exactly)

== radds (fractional / packed SATURATING reduce, 16-bit out) ==
radds(32 × 32767)  = 0x7FFF (32767)     <- saturates toward the +max field
radds(32 × -32768) = 0x8000 (32768)     <- sign-flagged field, NOT a clamp to INT16_MIN
radds([100,-50,0…])= 50                 <- in-range: pass-through
```

`radd`/`raddu` are exact widening sums (the accumulator is wide enough that the 32-lane fold can never
overflow); `radds` is the fractional-pack saturator of §4.1. `[HIGH/OBSERVED by execution]`

### 5.2 Horizontal min / max — signed vs unsigned divergence (the required tie case)

```
== rmax (signed) vs rmaxu (unsigned) over the SAME lane data ==
lanes = [-1, +1, -1000 ×30]
  signed   rmax  = +1        (0x0001)     <- -1 < +1 signed, so +1 wins
  unsigned rmaxu = 0xFFFF                  <- 0xFFFF(=-1) > 1 unsigned, so the "-1" lane wins
lanes = [0x8000, 0x0001, 0 ×30]
  signed   rmax  = +1                      <- 0x8000 = -32768 signed, smaller than 0 and +1
  unsigned rmaxu = 0x8000                  <- 0x8000 is the largest unsigned value present
```

The same physical lane bits reduce to **different** extrema under the signed vs unsigned interpretation —
the cleanest proof that `rmax`/`rmaxu` are genuinely distinct ops, not aliases. `[HIGH/OBSERVED by
execution]`

### 5.3 Reduce-and-flag — argmax value + predicate (the required flag case)

Driving the per-step primitive `rbmax_16` as a fold over `[3, 9, 9, 2, 5]` (max = 9, achieved at lanes 1
and 2):

```
seed   lane0 (v=3): max=3
fold   lane1 (v=9): max=9     <- new maximum, replaces
fold   lane2 (v=9): max=9     <- TIE: value held, this lane's argmax bit accumulated (§4.3)
fold   lane3 (v=2): max=9
fold   lane4 (v=5): max=9
FINAL: max = 9                <- the horizontal maximum, value side proven by execution
```

The value side reduces correctly to 9; the flag side accumulates the **tie set** {lane1, lane2} per the
`je`-branch OR at `0x859ed8` (the predicate is packed into the high bits of the accumulator word — read
from disasm, §4.3 — so the headline live certificate here is the **value fold**; the flag-bit *mechanism*
is OBSERVED from the binary, not yet sweep-validated). `[HIGH/OBSERVED by execution]` on the value;
`[HIGH/OBSERVED]` (disasm) on the inclusive-tie flag rule.

### 5.4 Tail-predicate generator — `ltrn` (the reduce-and-flag predicate form)

```
== ltrn(n): vbool with first n lanes set, 2 bits / 16-bit lane ==
ltrn( 0) = 0x0000000000000000
ltrn( 1) = 0x0000000000000003      (lane0 -> 2 bits)
ltrn( 2) = 0x000000000000000f
ltrn( 3) = 0x000000000000003f
ltrn( 8) = 0x000000000000ffff      (8 lanes × 2 bits = 16 bits)
ltrn(16) = 0x00000000ffffffff      (16 lanes × 2 bits = 32 bits)
ltrn(31) = 0x3fffffffffffffff
ltrn(32) = 0x0000000000000000      <- WRAP edge (§4.4): (1<<32)-1 = 0
```

This is the reduce-and-flag predicate form a tail-masked reduction consumes. `[HIGH/OBSERVED by
execution]`

### 5.5 Boolean fold — `randbn` (all-true) / `rorbn` (any-true)

```
== reduce-AND / reduce-OR over a 64-bit vbool (low bit of each 2-bit lane = predicate) ==
v = all bits 1                 randbn = 1   rorbn = 1
v = 0x5555… (low bit of lane)  randbn = 1   rorbn = 1     <- every lane TRUE
v = 0xAAAA… (high bit of lane) randbn = 0   rorbn = 0     <- NO lane true (high bit ignored)
v = all 0                      randbn = 0   rorbn = 0
v = 0x3 (lane0 only)           randbn = 0   rorbn = 1     <- not all true, but some true
v = 0x1 (lane0 low bit)        randbn = 0   rorbn = 1
```

`randbn` = AND-reduce (all-lanes-true), `rorbn` = OR-reduce (any-lane-true); the `0xAAAA…` row proves the
**predicate is the low bit of each 2-bit lane pair**, confirming the `vbool` geometry. `[HIGH/OBSERVED by
execution]`

---

## 6. Slot legality, latency, and co-issue

* **Arithmetic reduces** (`radd/rmax/rmin/rbmax/rbmin` int) carry **9** placements: the `s3_alu` slot of
  the eight wide formats (`f0/f1/f2/f3/f4/f6/f7/f11_s3_alu`) plus `n0_s3_alu`. The **fp `num` reduces**
  carry **6** (a subset of the s3_alu slots — `f0/f1/f2/f3/f7_s3_alu` + `n0_s3_alu`), reflecting which
  formats expose the fp-capable ALU lane.
* **`ltr*/randb*/rorb*`** carry **9** placements **in the load slot**: `f{0..7}_s1_ld` + `n2_s1_ld` +
  `f11_s1_alu`. They co-issue **with** an ALU reduce in the same bundle (load-slot + ALU-slot), so a
  `ltrn` (tail mask) and a `raddnx16t` (masked sum) can be scheduled together — though the data
  dependence (the mask must be ready before the masked reduce reads it) forces the consumer one bundle
  later (the `vbool` write/read stage rule, [register-files §5](../core/register-files.md)).
* **Latency.** The horizontal fold is a log-depth tree (5 stages for 32 lanes), so the reduce result is
  available several cycles after the source `vec` is ready; the scalar/narrowed result lands in a single
  destination lane. `[MED/INFERRED]` — the exact cycle count is the `libcas-core.so` timing model's job;
  the DWARF timing leaves were not resolvable to a per-op latency this pass, so the log-depth statement is
  inferred from the 5-stage 32→1 fold structure, not read as a cycle constant.

> **GOTCHA — a horizontal reduce writes ONE lane; the other 31 are undefined/zero, not broadcast.** The
> reduce result occupies a single `vec` lane (lane 0 for the sum/min/max forms). A reimplementer who
> expects the scalar result *replicated across all lanes* (as some SIMD ISAs do for `reduce_add`) will
> read garbage from lanes 1–31. To broadcast the reduced scalar back to a full vector you must follow with
> a `rep`/`splat` (B16). The `vbool`-producing forms (`rbmax`, `ltr`, `randb`/`rorb`) write the predicate
> file, not `vec`. `[MED/INFERRED]` (the leaf writes a single output word; broadcast is not in the leaf —
> consistent with the single-lane-result convention, but the exact lane index for each form is the ISS's
> writeback stage, not re-confirmed per-form this pass).

---

## 7. Partition discipline — the exact family-prefix assignment

To guarantee **no mnemonic is double-counted**, the per-prefix ownership, re-derived against the roster
this pass (the first-match classifier of [template §4.2](template-and-partition.md)):

| family / prefix | example | owner | why |
|---|---|---|---|
| horizontal sum `ivp_radd*`/`raddu*`/`radds*` (+`…t`) | `ivp_raddnx16` | **B08** | cross-lane fold → scalar |
| horizontal min/max `ivp_rmax*`/`rmin*`/`rmaxnum*`/`rminnum*` (+`…t`) | `ivp_rmaxnx16` | **B08** | cross-lane fold → scalar |
| reduce-and-flag `ivp_rbmax*`/`rbmin*`/`rbmaxnum*`/`rbminnum*` | `ivp_rbmaxnx16` | **B08** | fold → scalar **+** argmax `vbool` |
| tail-predicate `ivp_ltr*`/`ltrs*` (`…i`/`…_2`) | `ivp_ltrn` | **B08** | lane-count → `vbool` (load slot) |
| bool fold `ivp_randb*`/`rorb*` | `ivp_randbn` | **B08** | `vbool` → 1 bit (load slot) |
| **per-lane** min/max `ivp_max*`/`min*` | `ivp_maxnx16` | **B01** | 32→32, keeps lane geometry |
| per-lane flag min/max `ivp_bmax*`/`bmin*` | `ivp_bmaxnx16` | **B03** | 32→32 + per-lane `vbool` |
| per-lane min-OR-max select `ivp_minormax*` | `ivp_minormax2nx8` | **B01/B03** | `_8_8_8_1` lane select, not a fold |
| replicate / broadcast `ivp_rep*` | `ivp_repnx16` | **B16** | the `r` is *replicate*, not *reduce* |
| rotate `ivp_rotr*`/`rotri*` | `ivp_rotrnx16` | **B12** | shift/rotate, not a fold |
| fp seed `ivp_recip0*`/`rsqrt0*`/`recipqli*` | `ivp_recip0nxf16` | **B14/B15** | transcendental LUT, not a fold |

The discriminator that makes this airtight: a B08 op's value leaf takes a **`_512`** input (a whole
`vec`) and produces a **narrowed/scalar** output (`_32`/`_16`/`_8`/`_64`), whereas a B01/B03 lane-wise op
takes per-element scalars and returns one element. The `r`-prefix is *necessary but not sufficient* — `rep`
(replicate), `rotr` (rotate), `recip` (reciprocal seed) all start with `r` and are **not** reductions; the
fold test is the leaf's `_512`→narrow shape. `[HIGH/OBSERVED]`

---

## 8. Batch tally vs `nm`

| sub-family | mnemonics | placements | leaf prefix (libfiss) |
|---|--:|--:|---|
| Horizontal sum `radd*` | 12 | 108 | `radd/raddu/radds_{nx16,2nx8,n_2x32}(_t)` |
| Horizontal min/max `rmax*`/`rmin*` (int+fp) | 24 | 192 | `rmax/rmaxu/rmin/rminu/rmaxnum/rminnum_*` |
| Reduce-and-flag `rbmax*`/`rbmin*` (int+fp) | 20 | 156 | `rbmax/rbmin/rbmaxnum/rbminnum_*_64_512_512` |
| Tail-predicate `ltr*` | 9 | 81 | `ltr{n,2n,ni,n_2,…,sn}_64_32` |
| Boolean fold `randb*`/`rorb*` | 6 | 54 | `randbn/randb2n/randbn_2/rorbn/rorb2n/rorbn_2_64_64` |
| **B08 total** | **71** | **591** | — |

Cross-check: `108 = 12×9`; the min/max `192` (16 int × 9 + 8 fp `num` × 6), reduce-and-flag `156`
(12 int × 9 + 8 fp `num` × 6), tail-predicate `81 = 9×9`, and bool fold `54 = 6×9` per-mnemonic counts
sum to **591** — the figure **re-summed byte-exact this pass** from
`Σ` over all 71 rows of `nm libisa-core.so | rg -c 'Opcode_<mn>_Slot_.*_encode'` (the authoritative
total; the per-row arithmetic is a sanity rail). The int reduces carry **9** placements (s3_alu × 8 wide
formats + n0); the fp `num` reduces carry **6** (the fp-capable s3_alu subset); the load-pipe
`ltr*/randb*/rorb*` carry **9** (s1_ld × 8 + f11_s1_alu). The 71 mnemonics are a strict subset of the
**1065** `ivp_`-prefixed vector ops; together with B01/B03 they cover the lane-wise + cross-lane min/max
space without overlap (§7). The **value-leaf** footprint is ~85 distinct `module__xdref_*` leaves
(`radd*`/`rmax*`/`rmin*`/`rbmax*`/`rbmin*`/`ltr*`/`randb*`/`rorb*` roots × dtype suffixes), rolling into
the 864 denominator ([coverage-tally §6.3](../core/coverage-tally.md)). The 591 placements are part of —
never additive beyond — the certified-perfect 12569. `[HIGH/OBSERVED]`

---

## 9. Adversarial self-verification — five strongest claims re-challenged

1. **"B08 reductions are cross-lane: the leaf takes a whole 512-bit `vec` and returns a narrowed scalar."**
   Re-challenged by disassembling `module__xdref_radd_nx16_32_512` (`0x858690`): the body reads **32**
   distinct 16-bit lanes (`movzwl 0x3e(%rsi)`, `0x3c`, `0x3a`, … stepping by 2 down to `(%rsi)` — exactly
   32 reads), sign-extends each, sums into one `%eax`, and stores **one** 32-bit word. The input pointer
   spans a full 512-bit register; the output is a single scalar. This is the cross-lane signature, not a
   per-element leaf. **Holds.** `[HIGH/OBSERVED]`
2. **"The bool/tail-predicate reduces issue from the LOAD slot, the arithmetic reduces from `s3_alu`."**
   Re-challenged by listing placements: `Opcode_ivp_ltrn_Slot_*` enumerates **`f{0..7}_s1_ld` + n2_s1_ld +
   f11_s1_alu** and **no** `s3_alu`; `Opcode_ivp_raddnx16_Slot_*` enumerates **`f*_s3_alu` + n0_s3_alu**
   and **no** `s1_ld`. The selector bands match (`0x004axxxx` load vs `0x8690xxxx`/`0x8480xxxx` ALU).
   Two disjoint slot regimes, byte-confirmed. **Holds.** `[HIGH/OBSERVED]`
3. **"`radd` is exact/widening; `radds` is a fractional-pack saturator, not an int16 clamp."**
   Re-challenged by execution: `radd(32×32767) = 1048544` (exact, > INT16_MAX, **no** saturation) proves
   `radd` widens; `radds(32×32767) = 0x7FFF` and `radds(32×−32768) = 0x8000` (a sign-flagged field, **not**
   an `INT16_MIN` clamp) proves `radds` packs into a sign+magnitude 16-bit field via a 21-bit accumulator
   (`and $0x1fffff` in the disasm). A naive `clamp(Σ)` model diverges. **Holds (and corrects the naive
   reading).** `[HIGH/OBSERVED by execution]`
4. **"`rmax` and `rmaxu` genuinely diverge on signed-vs-unsigned ties."** Re-challenged by driving both
   over `[−1, +1, …]`: signed `rmax → +1`, unsigned `rmaxu → 0xFFFF` — the same physical lane bits reduce
   to different extrema. Confirmed again over `[0x8000, 1, …]` (signed `+1` vs unsigned `0x8000`). Distinct
   ops, not aliases. **Holds.** `[HIGH/OBSERVED by execution]`
5. **"`ltrn(n)` is the tail-predicate `vb[l]=(l<n)`, 2 bits/lane, with a `n=32` wrap edge."**
   Re-challenged by execution: `ltrn(1)=0x3`, `ltrn(8)=0xFFFF`, `ltrn(16)=0xFFFFFFFF` (2 bits per lane),
   and **`ltrn(32)=0x0`** — the `(1<<32)−1 = 0` wrap the disasm predicts (`shl %cl ; sub $1`, no clamp).
   The saturating `ltrs2n` (`0x814420`) adds the `js`/`cmp $0x3f` clamp the plain form lacks. **Holds.**
   `[HIGH/OBSERVED by execution]`

**Ungrounded / flagged items.** (i) The **fp `num` (NaN-skip) min/max** semantics (`rmaxnum`/`rminnum`/
`rbmaxnum`/`rbminnum`) are read from the `num` leaf bodies and the IEEE-754-2019 idiom, **not** sweep-driven
this pass — `[MED/INFERRED]` on the exact NaN/−0 behaviour (the int reduces are execution-certified; the fp
forms inherit the `num` primitive's behaviour). (ii) The **`…t` predicated-merge** fold-membership rule
(only masked-in lanes contribute) is read from the `radd_nx16_32_512_t` leaf signature (it takes the extra
mask arg) and the B03 `*t` operand-direction parallel, but the masked sum was **not** sweep-validated —
`[MED/INFERRED]`. (iii) The **`rbmax` flag value** (the argmax bit positions) is OBSERVED from the
tie-branch disasm (`je`→OR at `0x859ed8`) but the headline live drive certified the **value** fold; the
flag-bit sweep is `[HIGH/OBSERVED]` (disasm) / not-yet-execution-certified. (iv) The **single-lane writeback
index** of each reduce result (which lane holds the scalar) is the ISS writeback stage's job, stated
`[MED/INFERRED]` (lane 0 convention), not re-read per-form. (v) The **latency** is `[MED/INFERRED]` from the
5-stage 32→1 fold depth, not a `libcas-core` cycle constant (the timing DWARF was not resolvable this pass).

---

## 10. Cross-references

* [FLIX VLIW Encoding](../core/flix-encoding.md) — 14 formats / 46 slots, the `C7 07 imm32` encode-thunk
  ABI (§6.1), and the two-tier format-local selector model (§6.2) the §3 selector bands instantiate.
* [The Eight Register Files](../core/register-files.md) — `vec` idx2 (the 512b reduction source),
  `vbool` idx3 (the argmax flag / tail mask / bool-fold file, 2 bits/16-bit lane), and the `vbool`
  write/read stages that bound the `ltr → r*t` co-issue (§6).
* [The FP Sub-ISA (FCR/FSR, RNE/RZ)](../core/fp-sub-isa.md) — the `num` (NaN-skipping `maximumNumber`/
  `minimumNumber`) semantics the fp `rmaxnum`/`rminnum`/`rbmaxnum`/`rbminnum` reductions plumb.
* [ISA Coverage & the 1534/12642 Tally](../core/coverage-tally.md) — the certified-perfect 12569
  placement / 864 value-leaf denominators this batch's 591 / ~85 are part of.
* [B01 — Vector ALU int core](b01-vec-alu-int.md) (per-lane `max`/`min`) ·
  [B02 — Vector ALU fp slice](b02-vec-alu-fp.md) (lane-wise fp min/max) ·
  [B03 — Vector ALU rest](b03-vec-alu-rest.md) (per-lane flag `bmax`/`bmin`; cedes `r*t`/`ltr*` to B08, §7) —
  the lane-wise batches B08 complements (the cross-lane vs per-lane boundary, this page's header).
* [B11 — vbool ALU / predicate](b11-vbool-alu.md) (`vbool`→`vbool` logic) ·
  [B16 — Vector replicate](b16-vec-rep.md) (the `rep`/`splat` that broadcasts a reduced scalar back to a
  full vector, §6) · [B12 — Shift / rotate](b12-shift.md) (`rotr*`, the other `r`-prefix) — the
  `r`-prefix-adjacent batches B08 is disjoint from (§7).
* [ISA Reference Template & Partition](template-and-partition.md) — the §3 schema this page follows, the
  §4 partition slice (B08 ≈ 56 target → **71** OBSERVED), and the §6 roll-up it closes onto.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags and the free in-process
  value lane (`libfiss-base`) that makes §4–§5 `OBSERVED by execution`.

---

*Provenance: selector templates and slot placements are re-disassembled from `libisa-core.so`
(sha256 `8fe68bf4…`, `ncore2gp/config/`, not stripped; `.data.rel.ro` file = VMA − `0x200000`,
`readelf -SW`-confirmed this pass); the value leaves in §4–§5 (`radd/raddu/radds_nx16`, `rmax/rmaxu_nx16`,
`rbmax_16`, `ltrn/ltrn_2/ltrs2n`, `randbn/rorbn`) are driven live via `ctypes` against `libfiss-base.so`
(sha256 `260b110c…`), license-free. Counts via `nm | rg -c`; the `extracted/` tree is gitignored (reached
with absolute paths). All prose is derived from static analysis and in-process execution of the shipped
artifacts only; nothing here is read from a vendor source tree.*
