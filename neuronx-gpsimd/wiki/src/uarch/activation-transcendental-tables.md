# Activation + Transcendental Table Engine

This page documents the **two physically and architecturally separate table-lookup machines** the
Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD core ships for transcendental and activation work. They are
constantly confused for one machine; they are not. A reimplementer building a Vision-Q7-compatible
GPSIMD engine must implement **both, separately**:

* **Engine A — the Q7 in-core transcendental SEED LUT** (the *RECIP0 path*). A pair of 128×8-bit ROM
  tables (`RECIP_Data8` / `RSQRT_Data8`) baked into the vector lookup unit, indexed by the top
  mantissa bits of an fp value, emitting a ~7-bit Newton/QLI seed for `1/x`, `1/√x`, `√x`, `1/d`,
  `2^x`. Its content is **fixed in silicon and fully recovered** byte-exact. This is the
  per-lane arithmetic *primitive* a kernel composes (softmax `1/sum`, layernorm `1/√var`).
* **Engine B — the ACT-engine piecewise-CUBIC PWP activation table** (the *activation path*). A
  four-table SoC-block machine (CAM / PROFILE / CONTROL / BUCKET) where each BUCKET segment is a
  degree-≤3 polynomial `{float d0,d1,d2,d3,x0}`. Its *format* is fully recovered; its per-function
  **coefficient content is host-loaded per model and is OUT-OF-CORPUS** — the host-supplied-content
  wall. This is the named-activation *function* (relu/gelu/sigmoid/tanh/exp/…) the compiler emits as
  a fused 2D pass.

They share **no table**, are keyed differently, live in different SoC regions, and have different
content provenance. They meet only at the algebra layer (a transcendental realised at two layers,
§7) and ride the same VFPU FMA/FCR/FSR substrate ([VFPU / IEEE model](vfpu-ieee.md)).

> **Epistemic guard — the OBSERVED-table / CARRIED-interior split is crisp on this page.** Engine A's
> seed *tables* are **OBSERVED** — read byte-for-byte from `libfiss-base.so` `.rodata` at the
> confirmed offsets, and the leaf value semantics are **proven by execution** (driven live via
> `ctypes`, the binary arbitrating its own arithmetic). Engine B's table *format* (the four struct
> layouts, the index-extract spec, the cubic shape) is **OBSERVED** — read verbatim from the shipped
> `tpb_activation_entries.h` and compile-verified (`gcc offsetof`, all 5 gens). What is **CARRIED** is
> (i) the QLI/Newton *refine polynomial interior* — the per-segment `{A,gx}` coefficient algebra
> between the seed read and the full-precision result (the [FW-42 wall](../reference/confidence-model.md)),
> and (ii) Engine B's per-function *coefficient CONTENT* — never resident in the shipped image. Every
> interior-coefficient claim is flagged CARRIED; nothing here states the refine polynomial as
> freshly-derived fact. `[HIGH/OBSERVED]` on the tables + the format + the leaf outputs; `[—/CARRIED]`
> on the refine interior and the host content.

---

## 0. Headline findings

| # | Finding | Tag |
|---|---|---|
| 1 | **Two separate table engines.** Engine A (Q7 seed LUT, baked) and Engine B (ACT PWP cubic, host-loaded) share no table, no key, no SoC region. | `[HIGH/OBSERVED]` |
| 2 | **The 8 Q7 seed/QLI table symbols sit at the cited addresses**, all in `.rodata` (VMA==file-offset). `RECIP_Data8` @`0x958fc0`, `RSQRT_Data8` @`0x958dc0`, + `recip_tab`/`rsqrt_tab` byte-identical aliases + 4 QLI LUTs. | `[HIGH/OBSERVED]` |
| 3 | **The seed value == the table byte** — `recip0`/`rsqrt0` reproduce the `.rodata` table over all 128 buckets, 0 mismatches, fp16 *and* fp32, driven live. | `[HIGH/OBSERVED·exec]` |
| 4 | **The QLI refine is a single quadratic-interpolation pass** off a second pair of LUTs, with a 6-bit segment index `seg=(x>>25)&0x3f` — the seg extract is disassembled verbatim; the refine *polynomial interior* is CARRIED. | `[HIGH/OBSERVED] seg-extract; [—/CARRIED] interior` |
| 5 | **The ACT PWP BUCKET is a cubic** `{float d0,d1,d2,d3,x0}` (32B), not a linear PWL; CAM/PROFILE/CONTROL/BUCKET = 32/128/32/32 B, compile-verified all 5 gens. | `[HIGH/OBSERVED]` |
| 6 | **The host-content wall is one-sided and precise.** Engine A content is resident & recovered; Engine B `{d0..d3,x0}` cubics + PROFILE config words are host-loaded via `0x23 ACTIVATION_TABLE_LOAD` and never in the image. | `[HIGH/OBSERVED]` |
| 7 | **Device round-trip bit-exact** — `ivp_recip0nxf16`/`ivp_rsqrt0nxf16`/`recip0.s`/`recipqli.s` assemble+disassemble byte-for-byte under `XTENSA_CORE=ncore2gp`. | `[HIGH/OBSERVED]` |

All addresses are verified with `readelf -SW`, `nm`, `xxd`, `objdump`; all value facts are
driven live in-process against `libfiss-base.so`; all struct facts are compile-verified against
the shipped headers. Provenance: lawful interoperability RE of shipped artifacts; every fact reads as
derived from the binary, the `.rodata` bytes, the disassembly, the shipped C header, and the
device-native assembler.

---

## 1. Engine A — the Q7 transcendental SEED-lookup engine (the RECIP0 path)

The in-core seed LUT is the arithmetic-primitive engine that boots the divide / reciprocal / sqrt /
exp kernels. It is the hardware behind the [B14 fp16 `hp_lookup`](../isa/ref/b14-hp-lookup.md) and
[B15 fp32 `sp_lookup`](../isa/ref/b15-sp-lookup.md) ISA batches; this page is the **table-HW** view of
the same unit those two pages cover from the per-instruction ISA side. Keep the numbers identical to
those pages — this section re-grounds them at the table level, it does not re-litigate them.

### 1.1 Section layout — the seed tables are `.rodata`, VMA==file-offset `[HIGH/OBSERVED]`

The tables live in `libfiss-base.so`
(`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so`, `not stripped`). The
relevant sections, from `readelf -SW`:

| section | Address (VMA) | Off (file) | Δ | flags |
|---|---|---|---|---|
| `.text` | `0x190430` | `0x190430` | 0 | `AX` |
| `.rodata` | `0x88ff00` | `0x88ff00` | 0 | `A` |
| `.data.rel.ro` | `0xc17e80` | `0xa17e80` | **`0x200000`** | `WA` |
| `.data` | `0xc8eb68` | `0xa8eb68` | **`0x200000`** | `WA` |

> **GOTCHA — the `0x200000` delta is `.data`/`.data.rel.ro` only; the seed tables are NOT affected.**
> All 8 seed/QLI tables are in `.rodata` (`0x88ff00`–`0x959200`), where **VMA == file-offset**, so
> `xxd -s <VMA>` reads them directly with no delta. The `0x200000` (not libtpu's `0x400000`) delta
> applies only to the writable sections; a reimplementer reading these read-only ROM tables subtracts
> nothing. Confirm per-section with `readelf -SW` before trusting any address.

### 1.2 The two seed tables — symbols, addresses, format

`nm libfiss-base.so` resolves all 8 table symbols at the cited addresses, every
one an `r` (read-only) symbol inside `.rodata`:

| symbol | VMA | entries | stride | role |
|---|---|---|---|---|
| `table__rsqrt_tab` | `0x9551c0` | 128 | 4 B (u32) | fp16-leaf alias of `RSQRT_Data8` |
| `table__recip_tab` | `0x9553c0` | 128 | 4 B (u32) | fp16-leaf alias of `RECIP_Data8` |
| `table__fp_recip_qli_lut2_gx` | `0x9587c0` | **64** | 4 B | QLI segment base (lo-seg), 28-bit |
| `table__fp_recip_qli_lut1_gx` | `0x9588c0` | **128** | 4 B | QLI segment base (hi-seg), 28-bit |
| `table__fp_recip_qli_lut2_A` | `0x958ac0` | **64** | 4 B (i32) | QLI slope/quad (lo-seg) |
| `table__fp_recip_qli_lut1_A` | `0x958bc0` | **128** | 4 B (i32) | QLI slope/quad (hi-seg) |
| `table__RSQRT_Data8` | `0x958dc0` | 128 | 4 B (u32) | `1/√x` seed ROM |
| `table__RECIP_Data8` | `0x958fc0` | 128 | 4 B (u32) | `1/x` seed ROM |

**Physical form.** Each seed table is **128 entries, 4-byte stride, 8-bit seed in the low byte, upper
3 bytes zero** (the `Data8` name = 8-bit data widened to u32). Read straight from `.rodata`:

```
RECIP_Data8 (0x958fc0):  ff 00 00 00  fd 00 00 00  fb 00 00 00  f9 00 00 00  …  (low-byte = ff fd fb f9…)
                         …  → 88 88 87 87 86 85 85 84 84 83 83 82 82 81 81 81   (entries 112..127)
RSQRT_Data8 (0x958dc0):  b4 00 00 00  b3 00 00 00  b2 00 00 00  b0 00 00 00  …  (low-byte = b4 b3 b2 b0…)
                         … entry 62=81, 63=80 | 64=ff, 65=fd …  → … b8 b7 b6 b5  (entries 124..127)
```

> **QUIRK — two symbols name the same bytes (`recip_tab` ≡ `RECIP_Data8`).** A 512-byte `cmp`
> returns **IDENTICAL** for both `recip_tab`(`0x9553c0`) vs `RECIP_Data8`(`0x958fc0`) and
> `rsqrt_tab`(`0x9551c0`) vs `RSQRT_Data8`(`0x958dc0`). The fp16 leaves `lea` the `*_tab` name, the
> fp32 leaves the `*_Data8` name — **one ROM each**, two aliases. Cite `*_Data8` as the canonical seed
> source.

**`RECIP_Data8` construction** (re-derived; the value matches the bytes 127/128 exact, 128/128 ±1):

```c
// RECIP_Data8[i] == round( 256 / (1 + (i + 0.5)/128) ),  i = 0..127   (bucket midpoint, x in [1,2))
//   monotone-decreasing 0xff → 0x81; the 8-bit reciprocal of the bucket centre, the seed mantissa byte.
//   RECIP[0]=0xff (≈1/1.0), RECIP[64]=0xaa (≈1/1.5), RECIP[127]=0x81 (≈1/2.0).
```

**`RSQRT_Data8` construction** — a **two-range table split by EXPONENT PARITY, odd-half first**:

```c
// lo64  RSQRT_Data8[i],   i in [0,64):  ODD-exponent binade, x in [2,4)
//   == round( 256 / sqrt( 2*(1 + (i+0.5)/64) ) )    // 0xb4 … 0x80   (63/64 exact, 64/64 ±1)
// hi64  RSQRT_Data8[64+j], j in [0,64): EVEN-exponent binade, x in [1,2)
//   == round( 256 / sqrt(    1 + (j+0.5)/64   ) )    // 0xff … 0xb5   (64/64 exact)
// The 0x80→0xff jump at index 64 is the binade boundary, NOT a table error.
```

### 1.3 The index bits — the lookup key `[HIGH/OBSERVED·exec]`

The element function is **piecewise-constant within each mantissa bucket** (no interpolation in the
seed; the interpolation is the refine, §1.5). The index extract, **proven by live execution** (0
mismatches in every sweep below):

| seed | index formula | width | exponent frame |
|---|---|---|---|
| `RECIP0` / `DIV0` | `idx = (mant >> shift) & 0x7f` — **top 7 mantissa bits** | 7 b | recip reflects exp; div keeps parity only |
| `RSQRT0` / `SQRT0` | `idx = ((exp & 1) << 6) \| (mant >> shift)` — **top 6 mantissa bits ++ exponent parity** | 6 b + 1 | half-exponent (`>>1`) rebias |

where `shift` = 3 for fp16 (10-bit mantissa) / 16 for fp32 (23-bit mantissa) on RECIP, and 4/17 on
RSQRT. The **seed-mantissa placement** (table byte → output mantissa):

```c
fp16: seed_mant = (tab & 0x7f) << 3   // 8-bit byte at the mantissa top of a 10-bit field
fp32: seed_mant = (tab & 0x7f) << 16  // … of a 23-bit field
```

Verified live over all 128 buckets, both widths:

```
fp16 recip0:  idx=(m>>3)&0x7f         → seed hi-byte == RECIP_Data8[idx]&0x7f   0 mismatches / 1024 mantissas
fp16 rsqrt0:  idx=((e&1)<<6)|(m>>4)   → seed hi-byte == RSQRT_Data8[idx]&0x7f   0 mismatches / [1,2)
fp32 recip0:  idx=(mant>>16)          → seed hi-byte == RECIP_Data8[idx]&0x7f   0 mismatches / 128 buckets
fp32 rsqrt0:  idx=((e&1)<<6)|(m>>17)  → seed hi-byte == RSQRT_Data8[idx]&0x7f   0 mismatches / both binades
```

**Segment selection.** The seed engine is a **single-segment-per-half LUT** — the only "segment"
choice is the rsqrt exponent-parity half (2 ranges); reciprocal is one 128-entry monotone run. The
multi-segment piecewise structure lives in the QLI refine (§1.5) and the ACT PWP (§2), **not** the
seed table.

> **GOTCHA — these are SEEDs, not transcendentals; even an exact reciprocal carries the seed error.**
> Driven live: `recip0(2.0) = 0x3eff0000 = 0.498047`, **not** `0.5`, even though `1/2` is exactly
> representable. It reads bit-exact as `RECIP_Data8[0]&0x7f = 0x7f` placed in the mantissa with the
> reflected exponent — a perturbed `0.5`, not a rounding bug. Full-range fp32 max relative error,
> 50,000-sample live sweep: **`recip0` = 0.00781 = 2⁻⁷·⁰⁰ (7.0 bits)**, **`rsqrt0` =
> 0.00545 = 2⁻⁷·⁵² (7.5 bits)** — exactly the resolution a 128×8-bit table delivers, matching
> [B15 §6](../isa/ref/b15-sp-lookup.md). A reimplementer treating a seed op as the function is wrong
> by ~1 part in 128 on **every** input, including powers of two. `[HIGH/OBSERVED·exec]`

### 1.4 The seed ops + the lookup-unit HW

The seed/lookup ops are the **`ivpep_sem_hp_lookup`** (fp16, 30 ops) + **`ivpep_sem_sp_lookup`**
(fp32, 29 ops) groups — **one multiplexed `S3_ALU`-class lookup datapath** across six slots
`{F0,F1,F2,F3,F7,N0}`, the opcode-selector immediate indexing the mux. `libcas-core.so` (the cycle
model, `not stripped`, 179,079 symbols, **no DWARF**) names the family directly: 38
`ivpep_sem_hp_lookup_*` symbols, 38 `ivpep_sem_sp_lookup_*`, and a single shared
`bbn_sem_vec_sprecip_rsqrt_opcode` pipeline of **16 stages** (`stage0..stage15`) —
recip and rsqrt are **one 16-deep datapath** multiplexed by the opcode selector. The seed ROM is
mirrored in the simulator as `CONST_TBL_RECIP_Data8_0` @`0x17bd580` and `CONST_TBL_RSQRT_Data8_0`
@`0x17bd340`.

The seed roster (per-op output frame; structure HIGH/OBSERVED, the kernel-on-top is out-of-carve):

| op | seed | exponent frame |
|---|---|---|
| `RECIP0` | `1/x` seed, table byte at mantissa top | reflected (`0xfd − exp` fp32) |
| `RSQRT0` | `1/√x` seed, parity-selected half | half-exponent rebias |
| `SQRT0` | reuses RSQRT bytes (`√x = x·rsqrt0(x)`) | fixed sqrt frame |
| `DIV0` | reciprocal-of-divisor seed (shares `RECIP_Data8`) | parity-only (rest deferred to `divn`) |
| `NEXP0` / `NEXP01` | `2^x` range-reduce + integer-octave split | table-free (fp32 ships only `nexp01`) |

> **NOTE — `div0`/`sqrt0` ship no table of their own.** `div0` `lea`s `table__RECIP_Data8` (the same
> ROM as `recip0`); `sqrt0` `lea`s `table__RSQRT_Data8` (the same as `rsqrt0`). The four
> reciprocal-class seeds cost **two** 512-byte ROMs, not four. Driven live: `div0(1.0)==div0(4.0)`
> (same mantissa & parity), `sqrt0(1.0)==sqrt0(4.0)` — the parity-domain collision the shared ROM
> produces. `nexp01` references **no** table (pure bit manipulation).

**Timing** (the lookup-unit pipe, [B14 §6](../isa/ref/b14-hp-lookup.md)): the lookup unit is one cycle
deeper than the integer vec ALU — `vr USE@10 → vt DEF@12` = **2-cycle result latency**; IEEE flags
trail @13/14, the imprecise-err surface @15, `CPENABLE` (cp1 gate) sampled @3. The seed ops read FCR
Invalid/DivZero Enable and write the sticky FSR flags (recip/rsqrt `x==0 → +Inf + DivZeroFlag`;
`x<0 → Invalid`). Special inputs, driven live: `recip0(+0)=+inf`, `recip0(-0)=-inf`,
`recip0(+inf)=+0`, `recip0(NaN)=quiet-NaN`, `rsqrt0(-1.0)=NaN`, `rsqrt0(+0)=+inf`; denormals are
**not** flushed (a `bsr`-normalize path indexes them).

### 1.5 The QLI refine — the seed → full-precision step `[HIGH/OBSERVED seg-extract; —/CARRIED interior]`

The reciprocal seed (~7 bits) is taken to full fp32 (~24 bits) by a **single QLI
(quadratic-interpolation) step — NOT a Newton loop**. The refine leaf
`module__xdref_recipqli_1_1_1_1_1_32f_32f` @`0x87df20`, disassembled verbatim:

```c
// recipqli — the single-pass quadratic-interpolation refine of the reciprocal seed.
// Disassembled @0x87df20 (libfiss-base.so):
seg = (x >> 0x19) & 0x3f;          // 87df26: shr $0x19,%eax ; 87df2f: and $0x3f,%eax  — 6-bit segment index
                                   // 87df3b: cmp $0x3f,%eax — seg clamped 0..63
m23 = (x >> 0x17) & 0x7fffff;      // 87df44: shr $0x17,%esi ; 87df54: and $0x7fffff — fp32 mantissa crack
//   {A, gx} = fp_recip_qli_lut{1,2}_{A,gx}[seg]      // slope/quad + 28-bit base per segment
//   y_full  = quadratic_interp(seed, A, gx)          // one quadratic pass: error is CUBED, 8b → ~24b
```

The refine references **all four** QLI LUTs by `lea`: `lut2_A` @`0x958ac0`,
`lut1_A` @`0x958bc0`, `lut2_gx` @`0x9587c0`, `lut1_gx` @`0x9588c0`. The two LUTs are the two segments
of the piecewise refine: **lut1 (128× {i32 A, u32 gx 28-bit}) + lut2 (64× {i32 A, u32 gx 28-bit})**.
The header/ISA exposes exactly two refine sub-stages — `IVP_RECIPQLIN_2XF32_0` / `_1` (the segment
split, confirmed in `libcas-core.so`), **not** an iterated Newton loop. `recipqli.s`
round-trips bit-exact on the device (§5).

> **WALL (FW-42) — the QLI refine POLYNOMIAL INTERIOR is CARRIED.** The 6-bit segment extract, the
> four LUT addresses and sizes, and the substage split are **OBSERVED**.
> What is **CARRIED** is the exact `{A, gx}` coefficient algebra — the precise quadratic the
> hardware evaluates between the segment read and the writeback — and the device-interior reduction.
> The `libfiss-base` leaf is the functional reference model, but the refine math is not stated here as
> freshly-derived fact; it is the [B17](../isa/ref/b15-sp-lookup.md)/B24 refine concern, tagged
> `[HIGH/INFERRED]` there. **The seed TABLE is validated OBSERVED truth (the bytes were read); the
> refine polynomial interior is CARRIED.** `[—/CARRIED]` on the interior.

> **NEGATIVE — there is NO rsqrt QLI table / no `RSQRTQLI` intrinsic.** Only the reciprocal path has
> QLI LUTs. `rsqrt`/`sqrt` refine via the generic fp-FMA **Newton** step
> `y_{n+1} = y_n·(1.5 − 0.5·x·y_n²)`, 1–2 iterations from the 8-bit seed (the
> `bbn_sem_vec_sprecip_rsqrt` block).

> **NOTE — why QLI for recip but Newton for rsqrt (the engine-design fact).** A quadratic interpolant
> **cubes** the error per pass (8 bits → ~24 in one step), so the reciprocal needs one refine op + the
> seed; a Newton reciprocal step only **doubles** the bits (8→16→24, two steps). The silicon spends a
> second pair of LUTs (the QLI `{A,gx}`) to buy the single-pass reciprocal; rsqrt/sqrt, which would
> need a costlier QLI table, reuse the free FMA Newton iteration instead. `[HIGH/INFERRED]` from the
> LUT structure + the 2-substage split.

---

## 2. Engine B — the ACT-engine piecewise-CUBIC PWP activation table

The activation engine is a **separate four-table SoC-block machine**. The format is a
**piecewise-CUBIC** (PWP = Piece-Wise Polynomial) function approximator, **not** a linear
breakpoint/slope/intercept PWL. Source: `arch-headers/<gen>/tpb_activation_entries.h`, read verbatim
and compile-verified across all 5 gens.

### 2.1 The four tables — sizes + roles `[HIGH/OBSERVED]`

`sizeof` (compile-verified, `gcc offsetof`, all 5 gens identical): **CAM=32B, PROFILE=128B,
CONTROL=32B, BUCKET=32B**.

**CAM** — `aws_hal_stpb_act_cam_entry_t` (9 B used / 32 B stride), read verbatim:

```c
uint8_t  opcode;       // 7:0
uint8_t  func_id;      // 15:8
uint16_t rsvd0;        // 31:16
uint8_t  opcode_mask;  // 39:32        (SUNDA swaps func_id_mask/opcode_mask byte-order — §6)
uint8_t  func_id_mask; // 47:40
uint16_t rsvd1;        // 63:48
uint32_t valid : 1;    // 64
uint32_t unused1 : 31; // 95:65
uint32_t unused2[5];   // 255:96       (the trailer that pads 9 used → 32 B stride)
```

Matches `(opcode, func_id)` to SELECT the PROFILE/CONTROL/BUCKET slot. Region 0x1000 (4 KiB) / 32 B =
**128 CAM entries**. The `func_id` key + the 32 B stride are the structural separator from the
per-engine PROF_CAM profiler (16 B, no `func_id`) — see §4.

**PROFILE** — `aws_hal_stpb_act_profile_entry_t` (128 B, dense to bit ~754). The per-function PWL
config (field groups read verbatim):

* **dtype**: `bias_dtype_sel:4`, `scale_dtype_sel:4`.
* **PWL approx / region-split**: `pwl_bypass:1` (@32), `symmetry_opt_en:2`,
  `symmetry_opt_use_neg_region:2`, `func_sym_inv_sign:2`, `sub_src_sel:1`, `symmetry_point:32`,
  `exponent_offset:8` (@79:72), the four `{small,large}×{pos,neg}_signal_threshold` (8 b each),
  `pwl_control_base_{pos,neg}:8`, `ctrl_table_select_logic:32`, and the four
  `{large,small}×{pos,neg}_signal_pwl_control:20` + `mantissa_threshold_{msbs:12,lsbs:11}` +
  `mantissa_compare_enable:1` words (the region-split: small/large × pos/neg signal, per-region 20-bit
  control + mantissa thresholds + the **symmetry** optimisation — evaluate one half + reflect for
  odd/even functions like tanh/sigmoid, halving the table).
* **bias/scale fuse**: `bias_imm_val_sel:3`, `bias_fuse_en:1`, `bias_src_sel:2`, `bias_const:32`;
  `scale_imm_val_sel:3`, `scale_fuse_en:1`, `scale_src_sel:2`, `scale_const:32` (the affine
  `scale·x+bias` / de-quant, table-resident or instruction-resident).
* **FMA control**: `fma_bypass:3`, `bias_scale_fma_bypass:1` (@435), `fma_indirection_src_sel:6`,
  `fma_src_sel0..5` (4 b each), `fma_const0:32`, `fma_const1:32`, `mp_high_threshold:32`.
* **special inputs**: `func_rslt_for_{zero,nan,pos_inf,neg_inf}:32` each (bits 592–719) — the exact
  fp32 results the HW **substitutes** for 0/NaN/+Inf/−Inf so the PWL never has to approximate the
  saturating tails (exp/sigmoid/tanh get their asymptotes free).
* **tail**: `mp_low_threshold:32`, `mp_low_threshold_config:2`, `batchnorm_accumulator_rd:1` (@754).

Region 0x2000 (8 KiB) / 128 B = **64 func slots**. `[HIGH/OBSERVED]` bit layout; the per-bit *silicon
behaviour* is not claimed — `[LOW/NOT-CLAIMED]`.

**CONTROL** — `aws_hal_stpb_act_control_entry_t` (32 B stride; 4 B used as a union), read verbatim —
**the fp value → BUCKET INDEX extract spec**:

```c
union {
  struct {
    uint16_t act_tbl_base : 11;  // 10:0    base offset of this function's bucket run
    uint8_t  extract_lsb  : 5;   // 15:11   LSB bit position to start extracting
    uint16_t extract_size : 4;   // 19:16   number of bits to extract
    uint16_t unused1      : 12;  // 31:20
  };
  uint32_t data;
};
uint32_t unused2[7];             // 255:32   (the trailer: 4 B used + 28 B → 32 B stride)
```

The HW extracts `extract_size` bits starting at `extract_lsb` from the input's bit pattern, uses that
as the bucket index, offset by `act_tbl_base`. Because the input is fp, the extracted slice = an
**exponent + mantissa-MSBs window** → a **non-uniform (LOG-SPACED) segmentation**; the remaining low
mantissa bits are the interpolation fraction the cubic evaluates against (the `t=(x−x0)` local
coordinate). `PROFILE.exponent_offset` biases the exponent before the extract.

> **QUIRK — the CONTROL header's `// 16 bytes` comment is stale; the struct stride is 32 B.** The
> comment block reads "Control Table Entry / 16 bytes", but the `unused2[7]` tail makes the real
> stride 32 B (4 B used + 28 B unused). Compile-verified `sizeof = 32`. A reimplementer must walk
> CONTROL at a **32-byte** stride, not 16.

**BUCKET** — `aws_hal_stpb_act_bucket_entry_t` (32 B), read verbatim — **the per-segment CUBIC
polynomial**:

```c
float    d0;        //  0   constant term
float    d1;        //  4   linear coeff
float    d2;        //  8   quadratic coeff   <-- present => NOT a linear PWL
float    d3;        // 12   cubic coeff       <-- present => NOT a linear PWL
float    x0;        // 16   segment BREAKPOINT
uint32_t unused[3]; // 20..31
```

Offsets `d0@0, d1@4, d2@8, d3@12, x0@16` — compile-verified all 5 gens. The BUCKET region is a
**shared pool** indexed per-function via `CONTROL.act_tbl_base`; cayman `ACT_BUCKET_TABLE` = 0x10000
(64 KiB) / 32 B = **2048 buckets** (+ a 512 KiB `LOCAL_STORAGE` shadow = 16384).

### 2.2 The PWP evaluation — the per-segment math `[HIGH format / INFERRED-HIGH eval]`

With the local coordinate `t = (x − x0)`:

```c
func(x) ≈ d0 + d1·t + d2·t² + d3·t³     // degree-≤3 polynomial PER segment (cubic Hermite/Taylor)
```

A linear PWL would carry `{d0=intercept, d1=slope, x0=breakpoint}` only; the `d2`/`d3` quadratic/cubic
terms are **real and compile-verified present** at offsets 8/12. The exact HW Horner/Estrin schedule
is silicon. `[INFERRED-HIGH]` cubic eval from the 4-coeff+breakpoint shape.

### 2.3 The end-to-end lookup pipeline (Engine B)

```text
in_value(fp)
  ── CAM(opcode, func_id) ───────────────────────────────────────► func slot
  ── CONTROL(extract_lsb/size, + act_tbl_base, + PROFILE.exponent_offset bias)
                            ─────────────────────────────────────► bucket index (LOG-SPACED)
  ── BUCKET[index] ──────────────────────────────────────────────► {d0, d1, d2, d3, x0}
  ── HW eval d0 + d1·t + d2·t² + d3·t³,  t = (x − x0) ────────────► raw func value
  ── PROFILE(region split / symmetry reflect / special-input substitute / bias·scale affine / FMA)
                            ─────────────────────────────────────► final func output
```

The instruction's scale/bias (or the PROFILE fused const) is the affine applied PRE the function.
`[HIGH/OBSERVED]` for the table roles; `[MED/INFERRED]` for the exact HW micro-sequence.

### 2.4 The ACT opcode quad + the ACTIVATE2 (0x25) apply/fusion

The ACT family opcodes, read directly from `aws_neuron_isa_tpb_common.h`:

| opcode | enum | struct | role |
|---|---|---|---|
| `0x21` | `ACTIVATE` | `S3D3_AC` | scale+bias+act_func, 3D, accumulator_cmd |
| `0x22` | `ACTIVATE_QUANTIZE` | `S3D3_AQ` | act + inline requantize → UINT8 |
| `0x23` | `ACTIVATION_TABLE_LOAD` | `CTRL_NO` | **the PWP-table DMA install (LoadActFuncSet)** |
| `0x24` | `ACTIVATION_READ_ACCUMULATOR` | `D1_RD` | drain the per-lane fp32 ACT accumulator |
| `0x25` | `ACTIVATE2` | `S2D2_AC` | fused act + dual-ALU + reduce, 2D `[v4+]` |
| `0x26` | `ACTIVATE_MULTIPASS` | `S1S2D2_AM` | act + prev-pass 1D accumulator, 2D `[v5]` |

The `S2D2_AC` (0x25) fields, read verbatim (`activation_func @35`, `reduce_cmd @26`):

| off | field | type | note |
|---|---|---|---|
| 24 | `relu_param_src` | `IMM_SRC` | "byte 24-27 → RTL imm12" |
| 26 | `reduce_cmd` | `REDUCE_CMD` | `{IDLE,RESET,REDUCE,RESET_REDUCE}` |
| 29 | `op0` | `ALU_OP` | fused tensor-scalar op 0 |
| 30 | `op1` | `ALU_OP` | fused tensor-scalar op 1 |
| 31 | `reduce_op` | `ALU_OP` | the reduction op |
| 34 | `num_active_channels` | `u8` | validated vs `DVE_NUM_CHANNELS` |
| **35** | **`activation_func`** | **`u8`** | **INDEX into the loaded PWP table** |
| 36/40/44 | `imm0`/`imm1`/`relu_param` | `IMM_VAL_INST_FIELD` | relu_param = parametric-ReLU slope |

`op0`/`op1`/`reduce_op` draw from the `ALU_OP` table: `BYPASS=0x00, ADD=0x04,
SUBTRACT=0x05, MULT=0x06, DIVIDE=0x07, MAX=0x08, MIN=0x09, RE_LU=0x22, SQUARE=0x23` — so
`op0=MULT, op1=ADD` ⇒ the classic affine `scale·x + bias`. The fused 2D pass:

* **STAGE A (dual-ALU affine)**: `src ⊗op0 imm0 ⊗op1 imm1` (MULT+ADD = `scale·x+bias`; or MAX = clamp).
* **STAGE B (PWL func)**: `activation_func` indexes the PWP quad; `relu_param` is the parametric-ReLU
  negative slope (RTL imm12).
* **STAGE C (reduce)**: `reduce_cmd`/`reduce_op` folds the func outputs into the ACT per-lane
  accumulator (drained by `0x24 ACTIVATION_READ_ACCUMULATOR`).

Order affine→PWL→reduce is `[MED/INFERRED]` (FLIX-desync, not byte-pinned). The PWP table is
INSTALLED by `0x23 ACTIVATION_TABLE_LOAD`, the DMA that stages the host coefficients.

### 2.5 The maverick ACT→DVE HW-region fold `[HIGH/OBSERVED]`

On cayman/mariana (v3/v4) the four tables are an ACT-engine block (`TPB_n_ACT_{PROFILE_CAM,
PROFILE_TABLE, BUCKET_TABLE, CONTROL_TABLE}` + `LOCAL_STORAGE` shadows). On **maverick (v5) there is
NO ACT engine block**; the PWP SRAM physically MOVED into the DVE block. From
`maverick/vpc-mirror/arch-regs/src/address_map/TPB_DVE.json`:

| block | AddressOffset | size | note |
|---|---|---|---|
| `DVE_PROFILE_CAM` | `0x8D000` | `0x1000` | the DVE HW-decode profiler |
| `DVE_PROFILE_TABLE` | `0x8E000` | `0x2000` | (every engine has this) |
| `ACT_CONTROL_TABLE` | `0xA0000` | `0x2000` | |
| `PWP_CONTROL_TABLE` | `0xB0000` | `0x10000` (64 KiB) | the relocated CONTROL |
| `PWP_BUCKETS_TABLE` | `0xC0000` | `0x80000` (512 KiB) | the relocated BUCKET cubic pool |

So the v5 fold is a **real hardware-region migration**: the DVE engine HOSTS the activation PWL
datapath, not just its schedule. The PWP table FORMAT is byte-identical across the fold (sha
`8f6f5f49…` cayman..maverick).

---

## 3. The host-supplied-content WALL (the precise boundary)

The wall is between Engine B's table CONTENT and everything else. It is **one-sided and precise**:

**IN-CORPUS (recovered, OBSERVED):**

* Engine A's seed LUT content: `RECIP_Data8` / `RSQRT_Data8` (128×8 each) + the QLI LUTs
  (`fp_recip_qli_lut1/lut2 {A,gx}`) — baked in silicon/`.rodata`; read byte-exact.
* Engine B's table FORMAT: CAM/PROFILE/CONTROL/BUCKET struct layouts, the index-extract spec, the
  cubic-eval shape, the affine/symmetry/special-input config bit layout — compile-verified byte-exact.
* The activation OPS (`0x21/0x22/0x25/0x26`) + the table-install op (`0x23`) + the accumulator-drain
  op (`0x24`) — struct + validator decoded.

**OUT-OF-CORPUS (the WALL; host-loaded, never in the image):**

* Engine B's per-function COEFFICIENT CONTENT: the `{d0,d1,d2,d3,x0}` cubic coefficients for
  relu/gelu/sigmoid/tanh/exp/silu/swish/softplus/elu/… AND the per-function PROFILE config words
  (region thresholds, `symmetry_point`, `exponent_offset`, `func_rslt_for_{zero,nan,pos_inf,neg_inf}`,
  bias/scale consts).

> **NOTE — `activation_func @35` is a RAW uint8 index; there is NO ISA-named activation enum.** A grep
> of all arch-isa headers finds **0** hits for `gelu`/`sigmoid`/`silu`/`swish`/`softplus` as
> an activation enum (the only stray hit is `tonga/intc_axim.go` — an unrelated AXI-interconnect Go
> file, not an activation). The only RELU is the `ALU_OP RE_LU=0x22` + the `relu_param` immediate. The
> host KRT loads the functions as PWP TABLE DATA via `0x23` and selects by index.

**Why the wall is where it is.** Engine A's seed LUT is a FIXED arithmetic primitive (`1/x`, `1/√x` —
the same for every model, hence baked); Engine B's PWP is a PROGRAMMABLE function approximator (any
activation a model wants, hence host-loaded). The shipped artifacts contain the MACHINE (both engines'
format + HW) but only the FIXED engine's content. No static analysis on this corpus can reach the host
PWP coefficients — they arrive at runtime per model. `[HIGH/OBSERVED + INFERRED rationale]`. The
forward firmware page [Activate + the PWL application mechanism](../firmware/kernels/activate-pwl.md)
(Part 5) covers the `0x23` install path; the forward validation page
[VAL — fp Transcendental Seed/Refine Family](../validation/transcendental-seed.md) (Part 15, VAL-08)
carries the bit-exact differential certificate for Engine A.

---

## 4. The table-HW model (the two engines, side by side)

| axis | ENGINE A — Q7 SEED LUT | ENGINE B — ACT PWP |
|---|---|---|
| purpose | transcendental SEED / first-iterate (`1/x`,`1/√x`,`√x`,`div`,`exp` seeds) | named ACTIVATION (relu/gelu/sigmoid/tanh/exp/…) + asymptotes |
| physical home | the `S3_ALU` lookup-unit ROM (in-core, per-lane) | ACT-engine SoC block (v2–v4) / DVE block (v5 `PWP_*` SRAM) |
| table(s) | `RECIP_Data8`/`RSQRT_Data8` 128×8 + `fp_recip_qli_lut1/2 {A,gx}` | CAM / PROFILE / CONTROL / BUCKET (BUCKET = cubic `{d0..d3,x0}`) |
| content | **BAKED** in silicon (`.rodata`) — fixed; FULLY RECOVERED | **HOST-LOADED** per model (`0x23` DMA); FORMAT recovered, CONTENT out-of-corpus |
| index key | top 7 mantissa bits (recip) / top 6 + exp parity (rsqrt) | `CONTROL.extract_lsb/size` of the fp bit pattern (log-spaced) |
| segmentation | 1 run (recip) / 2 parity halves (rsqrt); QLI refine = 2 segs | up to 2048 shared buckets, region-split (small/large × ±) |
| precision | ~2⁻⁷ seed → ~2⁻²⁴ after 1 QLI (recip) or 1–2 FMA Newton (rsqrt) | cubic per segment (host-set coeff precision) |
| eval | 8-bit table → mantissa placement → QLI quadratic / Newton refine | `d0+d1·t+d2·t²+d3·t³`, `t=x−x0` + PROFILE region/symmetry/special/affine |
| op / datapath | `ivpep_sem_hp/sp_lookup`, `S3_ALU`, 2-cyc, FCR/FSR | `0x21/0x25/0x26 ACTIVATE*`, 2D, PSUM/SBUF accumulator (`0x24` drain) |
| exceptions | FCR/FSR Invalid/DivZero/Inexact + imprecise-err | PROFILE special-input substitute (no PWL approx of asymptotes) |

The lookup/refine pipeline (A) vs the lookup/eval pipeline (B):

```text
A:  fp ─►[extract top mantissa bits]─► SEED LUT (8-bit) ─►[place mantissa, frame exp]─► SEED y0
       ─►[QLI quad refine | FMA Newton]─► y_full                     (2-LEVEL: seed then refine)
B:  fp ─►[CONTROL bitfield extract, +act_tbl_base, +exponent_offset]─► BUCKET index ─► CUBIC {d0..d3,x0}
       ─►[eval d0+d1·t+d2·t²+d3·t³]─►[PROFILE region/symmetry/special/affine]─► func(x)   (1-LEVEL: extract then cubic)
```

---

## 5. Device round-trip — the seed + refine ops `[HIGH/OBSERVED]`

Assembled+disassembled bit-exact with `XTENSA_CORE=ncore2gp`
(`tools/XtensaTools/bin/xtensa-elf-{as,objdump}`):

```text
ivp_recip0nxf16 v5,v1   →  32514d080248452f   { nop; nop; nop; ivp_recip0nxf16 v5, v1 }
ivp_rsqrt0nxf16 v6,v2   →  32514d002289452f   { nop; nop; nop; ivp_rsqrt0nxf16 v6, v2 }
recip0.s        v4,v1   →  32514d08021d452f   { nop; nop; nop; recip0.s        v4, v1 }
recipqli.s      v17,v18 →  32505818f052452f   { nop; nop; nop; recipqli.s      v17, v18 }
```

All four match the cited bytes byte-for-byte. The opcode-selector immediates at `F1_S3_ALU` (read from
the `Opcode_ivp_<mnem>_Slot_f1_s3_alu_encode` thunks in `libisa-core.so`): `recip0n_2xf32 =
0x26348306`, `rsqrt0n_2xf32 = 0x26350106`, `div0n_2xf32 = 0x26338306`, `nexp01n_2xf32 = 0x26348106` —
identical to [B15 §2](../isa/ref/b15-sp-lookup.md).

---

## 6. Per-gen evolution `[HIGH/OBSERVED]`

The PWP table format (`tpb_activation_entries.h`), sha256 + compile-verify all 5 gens:

| gen | sha256 (16) | CAM/PROFILE/CONTROL/BUCKET | delta vs cayman |
|---|---|---|---|
| `sunda` (v2) | `dbdca26b045ec658` | 32/128/32/32 | CAM `{func_id_mask@39:32, opcode_mask@47:40}` swapped; `rsvd33:5`@439:435 (no `bias_scale_fma_bypass`); `unused1:14`@767:754 (no `batchnorm_accumulator_rd`) |
| `cayman` (v3) | `8f6f5f494d816bb6` | 32/128/32/32 | baseline (adds `bias_scale_fma_bypass:1`@435 + `batchnorm_accumulator_rd:1`@754) |
| `mariana` (v4) | `8f6f5f494d816bb6` | 32/128/32/32 | byte-identical |
| `mariana_plus` | `8f6f5f494d816bb6` | 32/128/32/32 | byte-identical |
| `maverick` (v5) | `8f6f5f494d816bb6` | 32/128/32/32 | format byte-identical; HW-region fold (§2.5) |

So the sunda→cayman PWP step is a **minor config-bit growth** (FMA-bypass + batchnorm-accum-read) + a
**CAM mask byte-order swap**; the core PWL machine (CONTROL extract + BUCKET cubic + the
region/symmetry config) is unchanged v2→v5. The activation datapath home: **ACT-engine v2–v4 → DVE
block v5**.

> **NOTE — `PROF_CAM`/`PROFILE_TABLE` is the per-engine PROFILER, NOT the activation PWL.** Every NX
> engine (PE/DVE/POOL/ACT) ships a `PROFILE_CAM`/`PROFILE_TABLE` pair — the HW-decode instruction
> profiler — with **no** bucket/control companion, a different key (`opcode:32`, no `func_id`),
> different stride (16 B vs 32 B), different density (sparse vs the 128 B dense PROFILE). The BUCKET +
> CONTROL tables exist ONLY where the activation datapath lives (ACT v2–v4, DVE v5). The
> `0xe4 ConvLutLoad` LUT is a **third, unrelated** table (a PE-array 4-bit input data-converter).
> Three distinct table machines; do not conflate.

---

## 7. The relationship — Q7 RECIP0 path vs ACT activation path

**They are NOT the same machine.** The Q7 RECIP0 seed LUT and the ACT PWP are physically separate
(different SRAM/ROM, different SoC region), keyed differently (top-mantissa-bits vs CONTROL bitfield
extract), with different content provenance (baked vs host-loaded), at different precision granularity
(8-bit seed vs 128-bit-coefficient cubic). They share **no** table. `[HIGH/OBSERVED]`

**Where they meet (the algebra, not the silicon).** A model's transcendental can be realised at two
layers:

* **(i) the ACT-engine layer (the common path):** exp/sigmoid/tanh/gelu are loaded as ACT PWP cubic
  tables (host coefficients) and evaluated by Engine B in one 2D `ACTIVATE` pass with asymptote/symmetry
  handling for free. This is the tensor-wide, throughput path.
* **(ii) the Q7 arithmetic layer (the kernel path):** a bespoke NX kernel that needs a reciprocal /
  rsqrt / divide / exp INSIDE its inner loop uses the Q7 seed ops (`RECIP0`/`RSQRT0`/`NEXP0`) + the
  QLI/Newton refine (Engine A), per-lane, in the vector datapath — NOT the ACT engine. e.g. a softmax
  denominator's `1/sum`, a layernorm's `1/√var`, a divide's `a·(1/b)`.

So Engine A is the PRIMITIVE a kernel composes; Engine B is the FUNCTION the compiler emits as a fused
activation. The **DVE Exponential `0x30`** (struct `S3D3_TS`, a DVE-bank element-wise exp) is a THIRD
path, separate from both — confirming the struct2opcode mapping that puts `EXPONENTIAL` in the
TensorScalar family, not the ACT `activation_func`. `[INFERRED-HIGH]` from the op homes + use-sites.

**The shared substrate.** Both ride the same VFPU FP datapath ([VFPU / IEEE model](vfpu-ieee.md)):
Engine A's seed ops are `S3_ALU` lookup ops whose refine (QLI/Newton) uses the FMA tree; Engine B's
PROFILE affine (`scale·x+bias`) + the cubic Horner multiply-adds use the same FMA/round HW + the FCR
RoundMode + the FSR sticky flags. The 'N' (no-imprecise) FMA forms (MADDN/MULAN/DIVN) are the
un-flagged fast path the seed REFINE loop uses — so the Q7 RECIP0→QLI/Newton chain runs on the no-flag
FMA variant, the ACT affine runs flagged.

The seed-table and the bucket-table are the two ENDS of one design spectrum: a fixed
8-bit-resolution single-segment seed (cheap, universal, refined by iteration) at one end; a
programmable 128-bit-coefficient multi-segment cubic (expensive, per-function, single-shot) at the
other. The QLI refine LUT sits in between — a fixed multi-segment (`{A,gx}`) quadratic that bridges the
seed up to full precision. GPSIMD ships **all three** table classes. `[INFERRED-HIGH]`

---

## 8. Adversarial self-verification — the five strongest claims, re-challenged

Each headline claim is re-tested against the binary + the live fiss oracle; a claim survives
only if a second independent witness agrees.

1. **The seed-table FORMAT/offsets are as stated (`RECIP_Data8` @`0x958fc0`, 128×u32, 8-bit low
   byte).** *Challenge:* could the addresses be stale or the stride wrong? *Re-test:* `readelf -SW`
   places `.rodata` at VMA==file-off `0x88ff00`; `nm` resolves `table__RECIP_Data8` @`0x958fc0` (`r`
   flag, inside `.rodata`); `xxd -s 0x958fc0` reads `ff 00 00 00 fd 00 00 00 …` — 4-byte stride, seed
   in low byte, top 3 bytes zero, monotone `0xff→0x81`. Cross-witness: `recip_tab` @`0x9553c0` is
   byte-identical over 512 B (`cmp`). **Survives.**

2. **The index-bit extraction (recip = top 7 mantissa bits; rsqrt = top 6 + exp parity).**
   *Challenge:* maybe rsqrt also uses 7 bits, or the parity half is an artifact? *Re-test:* driven
   live, `recip0` seed byte == `RECIP_Data8[(m>>16)]&0x7f` over all 128 fp32 buckets and
   `[(m>>3)]&0x7f` over all 1024 fp16 mantissas, **0 mismatches**; `rsqrt0` seed byte ==
   `RSQRT_Data8[((e&1)<<6)|(m>>17)]&0x7f` over **both** binade halves, 0 mismatches — `rsqrt0(2.0)`
   (even) and `rsqrt0(4.0)` (odd) read *different* table halves (`0xb4` vs `0xff`). **Survives.**

3. **The QLI refine is a single quadratic pass with a 6-bit segment index, off a second pair of
   LUTs.** *Challenge:* could it be an iterated Newton loop, or could the seg index be wrong?
   *Re-test:* `objdump` of `recipqli` @`0x87df20` shows `shr $0x19; and $0x3f` (the 6-bit
   `seg=(x>>25)&0x3f`) and `cmp $0x3f` (clamp), with `lea` to all four `fp_recip_qli_lut{1,2}_{A,gx}`;
   `libcas-core.so` carries exactly two substages `IVP_RECIPQLIN_2XF32_0`/`_1` (a segment split, not
   `stage0..N` iteration). The refine *interior* coefficient algebra is flagged CARRIED (FW-42), not
   asserted. **Survives** (seg-extract OBSERVED; interior CARRIED).

4. **The ACT PWP BUCKET is a cubic, not a linear PWL; the quad is 32/128/32/32 B.** *Challenge:* maybe
   `d2`/`d3` are reserved padding, or the sizes drift across gens? *Re-test:* the header reads
   `float d0,d1,d2,d3,x0` at offsets 0/4/8/12/16 with a 3-word tail; `gcc offsetof` confirms
   `d2@8, d3@12` are real float coeffs; `sizeof` = 32/128/32/32 compile-verified on cayman, maverick,
   AND sunda. A linear PWL would carry `{d0,d1,x0}` only. **Survives.**

5. **The host-content boundary — `activation_func @35` is a raw index; the cubic CONTENT is never in
   the image.** *Challenge:* could a named activation enum or resident coefficients exist somewhere?
   *Re-test:* `activation_func` is a `uint8` field "INDEX into the loaded PWP table" (header comment);
   a grep for `gelu/sigmoid/silu/swish/softplus` across all arch-isa headers returns 0 activation-enum
   hits (only an unrelated `tonga/intc_axim.go`); the install path is `0x23 ACTIVATION_TABLE_LOAD` (a
   DMA). The seed LUT content IS resident (read byte-exact above); the PWP content is not. The wall is
   between Engine B's content (out) and everything else (in). **Survives.**

No claim here rests on a raw dump, an unnamed symbol, or a single uncorroborated witness; every seed
value carries a differential-execution certificate against the shipped leaf and the re-derived
`.rodata` table, the encode/round-trip facts are double-witnessed by `libisa-core.so`/`libcas-core.so`
and the device assembler, and the PWP format is compile-verified across all 5 gens.

---

## 9. Confidence ledger

**HIGH / OBSERVED (read / disassembled / round-tripped / driven live):**

* The 8 Q7 seed/QLI table symbols at the cited addresses (all `.rodata`, VMA==file-off), the
  `recip_tab`≡`RECIP_Data8` / `rsqrt_tab`≡`RSQRT_Data8` 512-byte identity, the RECIP monotone `0xff→0x81`
  / RSQRT two-binade-with-`0x80→0xff`-boundary byte content.
* The seed value == table byte certificate, fp16 (1024 mantissas) and fp32 (128 buckets), recip *and*
  rsqrt, 0 mismatches; the index-bit extraction (recip top 7, rsqrt top 6 + parity); the special-input
  algebra; the full-range accuracy (recip 7.0 bits, rsqrt 7.5 bits, 50k-sample live sweep); the
  `div0`/`sqrt0` shared-ROM parity collision; the `nexp01` table-free integer staircase.
* The `recipqli` 6-bit segment extract (`shr $0x19; and $0x3f`) + the fp32 mantissa crack
  (`shr $0x17; and $0x7fffff`) + the four QLI-LUT `lea` targets + the two `RECIPQLIN_2XF32_0/_1`
  substages.
* The ACT PWP quad: CAM 32B / PROFILE 128B / CONTROL 32B `{act_tbl_base:11, extract_lsb:5,
  extract_size:4}` / BUCKET 32B `{float d0,d1,d2,d3,x0}`, compile-verified all 5 gens; the cross-gen
  sha (`8f6f5f49` cayman..maverick, `dbdca26b` sunda) + the sunda CAM-mask-swap + two cayman+-added
  config bits.
* The ACT opcodes `0x21–0x26` + `RECIPROCAL=0x48` + `EXPONENTIAL=0x30` from `common.h`; the `S2D2_AC`
  field order (`activation_func@35`); the `ALU_OP` table; the absence of a named activation enum.
* The maverick DVE fold (`ACT_CONTROL_TABLE`/`PWP_CONTROL_TABLE`/`PWP_BUCKETS_TABLE` inside `TPB_DVE`
  at `0xA0000`/`0xB0000`/`0xC0000`).
* The device round-trip (`ivp_recip0nxf16`/`ivp_rsqrt0nxf16`/`recip0.s`/`recipqli.s`, bytes match) +
  the F1 opcode selectors; the `bbn_sem_vec_sprecip_rsqrt` 16-stage pipeline; the `CONST_TBL_*` mirrors.

**MED / INFERRED:**

* The PWP HW Horner/Estrin schedule + the exact CONTROL extract HW-vs-firmware split + the ACTIVATE2
  affine→PWL→reduce ordering (FLIX-desync).
* The two-layer transcendental relationship (§7) + the QLI-vs-Newton design rationale.

**CARRIED (FW-42 wall):**

* The QLI/Newton refine **polynomial interior** (the `{A,gx}` coefficient algebra between the seed read
  and the full-precision result) — the seg-extract + LUTs are OBSERVED; the polynomial is CARRIED.
* The device-interior seed-coefficient bytes / gate-level reduction beyond the table value.

**LOW / NOT CLAIMED:**

* The host-supplied ACT PWP table CONTENT (per-function cubic coefficients + PROFILE config words for
  relu/gelu/sigmoid/tanh/exp) — out-of-corpus, the WALL.
* The per-bit silicon behaviour of every PROFILE field (the bit LAYOUT is OBSERVED; the gate behaviour
  is not); the per-function bucket count (shared pool, host-set); the exact `2^x` kernel on top of the
  `NEXP0`/`NEXP01` seed (out-of-carve).

All facts read as derived from shipped-artifact static analysis and license-free in-process execution
of the binary's own value leaves and the device-native assembler (lawful interoperability RE).

---

## Cross-references

* [ISA Batch 14 — fp16 Transcendental Seeds (`hp_lookup`)](../isa/ref/b14-hp-lookup.md) — the
  per-instruction ISA view of Engine A's fp16 half: the 10-mnemonic roster, the encode thunks, and the
  by-execution seed certificates this page re-grounds at the table level.
* [ISA Batch 15 — fp32 Transcendental Seeds (`sp_lookup`)](../isa/ref/b15-sp-lookup.md) — Engine A's
  fp32 half: the `recip0`/`rsqrt0`/`sqrt0`/`div0`/`nexp01` `n_2xf32` forms, the 128-bucket fp32
  reproduction certificate, the 7.0/7.5-bit accuracy this page matches.
* [The Floating-Point Sub-ISA (FCR/FSR view)](../isa/core/fp-sub-isa.md) — the NaN/round/exception
  model both engines ride, and the scalar `.h` siblings of the vector seeds.
* [The VFPU / IEEE-754 Exception Model](vfpu-ieee.md) — the FMA tree + FCR/FSR substrate the seed
  refine (QLI/Newton) and the ACT affine/cubic share.
* [Activate + the PWL Application Mechanism](../firmware/kernels/activate-pwl.md) *(Part 5 — forward)*
  — the firmware `0x23 ACTIVATION_TABLE_LOAD` install path that stages the host PWP coefficients.
* [VAL — fp Transcendental Seed/Refine Family](../validation/transcendental-seed.md)
  *(Part 15, VAL-08 — forward)* — the bit-exact differential validation of Engine A's seed/refine.
* [The Confidence & Walls Model](../reference/confidence-model.md) — the tags, the FW-42
  seed-coefficient / refine-interior wall, and the proven-by-execution value lane (`libfiss-base.so`
  via `ctypes`).
