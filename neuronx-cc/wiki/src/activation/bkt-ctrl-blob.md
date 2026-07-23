# bkt.bin / ctrl.bin — On-Device PWP Blob Format

> *All offsets, sizes, addresses, and hexdumps on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The blobs live in `neuronxcc/pwp/pwp_bin_trainium/` and `neuronxcc/pwp/pwp_bin_with_ln/`, shipped in the wheel; they are **byte-identical across cp310/11/12** (md5-verified — e.g. `exp_and_others_bkt.bin` = `14b0d1b5d0ae7b57bed70bdebeca46dc`, `_ctrl.bin` = `0fddfa2e29d5bc3d31d033b45deb7356` in all three wheels). The reference parser is `neuronxcc/starfish/lib/libpwp_sim.so` (`PWPSim::Simulator`, not stripped); the codegen producer is `neuronxcc::backend::LowerPWPImpl` in `libwalrus.so` (stripped — symbol names only). For `.text`/`.rodata`, VA equals file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

An activation function-set ships as a pair of flat binary blobs plus a JSON manifest: `<set>_bkt.bin` (the polynomial **bucket** table — the inner LUT) and `<set>_ctrl.bin` (the per-exponent-octave **control** table — the outer index), bound by `<set>.json`. The Activation engine DMAs both into on-chip table memory when it executes a `LoadActFuncSet` (BIR IT6) op, then every `Activation` instruction indexes the resident tables to evaluate a piecewise-cubic approximation per element. This page is the byte-format reference for the two blobs: a reader who finishes it can both **parse** a shipped blob into its fields and **pack** a fresh one from a profile.

The two records are small and rigid. A `bkt` entry is exactly **32 bytes** — five `float32` fields `[d0, d1, d2, d3, x]` (a degree-3 segment polynomial plus its breakpoint) followed by 12 bytes of zero pad. A `ctrl` entry is a single packed **`uint32`** carrying three bitfields — the absolute bucket base index, an `extract_lsb`, and an `extract_size` — that together say *where* an octave's buckets live and *how* to derive the in-octave bucket id from the input mantissa. The `ctrl` record is padded to a fixed stride that is **target-dependent** (32 B on Trainium, 16 B on the `with_ln` image), but the 4-byte control word is identical between targets.

For reimplementation, the contract is:

- The **32-byte `bkt` record layout** — field order, offsets, endianness, and the always-zero pad — and the fact that it stores raw IEEE-754 bits (= the profile JSON `.int` member), not a custom mini-float.
- The **`ctrl` `uint32` bitfield split** `(extract_size << 16) | ((23 − extract_size) << 11) | bkt_base`, and the `extract_lsb == 23 − extract_size` invariant that holds on every populated octave.
- The **exponent-stepped addressing**: an octave is selected by the input's biased exponent, the in-octave bucket by the top `extract_size` mantissa bits (`mant >> extract_lsb`).
- The **`bkt_base` accumulator**: `func_to_bkt_start_idx[f]` plus the running sum of *actual materialized* sections of preceding octaves (not the declared `num_sections` capacity).
- The four **saturation slots** appended after each func's LUT body, and the `func_id` arch-native silicon selector that picks the func within a loaded set.

| | |
|---|---|
| **Blobs** | `neuronxcc/pwp/pwp_bin_{trainium,with_ln}/<set>_{bkt,ctrl}.bin` |
| **Manifest** | `neuronxcc/pwp/pwp_bin_*/<set>.json` (binds blobs to per-func profiles) |
| **Per-func source** | `neuronxcc/pwp/pwp_jsons/<func>_<Np>.json` (the coefficient source of truth) |
| **bkt record** | 32 B = `[d0, d1, d2, d3, x]` fp32 @ 0/4/8/12/16 + 12 B zero pad |
| **ctrl record** | 1× `uint32` word + zero pad; stride 32 B (trainium) / 16 B (with_ln) |
| **ctrl word** | `bits[0:11]=bkt_base`, `bits[11:16]=extract_lsb`, `bits[16:24]=extract_size`, `bits[24:32]=0` |
| **Reference parser** | `PWPSim::Simulator::evaluate_generic` / `find_pwp_nonsat_section` (`libpwp_sim.so`) |
| **Producer** | `LowerPWPImpl::generateInstLoadActFuncSet` (`libwalrus.so`, stripped) |
| **Catalog scope** | 35 sets, 409 funcs, 32,759 `bkt` + 3,071 `ctrl` records — zero mismatch vs JSON |

---

## 1. File Inventory and the Manifest

Each function-set is three files that travel together. The JSON manifest is the binding glue: it names the two blobs, gives their entry counts, and maps each co-resident func to its starting index in each table.

### Layout

```text
neuronxcc/pwp/pwp_bin_trainium/      (21 sets; ctrl stride 32 B)
  <set>_bkt.bin    flat array of 32-byte polynomial records
  <set>_ctrl.bin   flat array of (32-byte) control records
  <set>.json       manifest: counts + func→index maps + profile_meta_data[]
  act_info.json    the 21-set roster (name, bkt_bin, ctrl_bin, profile_json, act-dict)
neuronxcc/pwp/pwp_bin_with_ln/       (14 sets; ctrl stride 16 B — same blob schema)
neuronxcc/pwp/pwp_jsons/             per-FUNCTION source profiles <func>_<Np>.json
```

### Manifest fields (`<set>.json`)

`exp_and_others.json` has nine keys:

```text
bkt_bin                "exp_and_others_bkt.bin"
bkt_entry_cnt          941          # number of 32-byte bkt records
ctl_bin                "exp_and_others_ctrl.bin"   # NB: key is ctl_bin, value ends _ctrl.bin
ctl_entry_cnt          89           # number of ctrl records
func_to_bkt_start_idx  { exp:0, identity:781, copy:785, …, tanh:813, …, square:937 }
func_to_ctl_start_idx  { exp:0, identity:52,  …,         tanh:64,  …, square:88  }
func_exp_to_bkt_start_idx / func_exp_to_ctl_start_idx   # per-exponent variant index maps
profile_meta_data[]    # one 30-field scalar control object per co-resident func (§6)
```

The blobs are **prebuilt**, not regenerated at compile time. `bkt_entry_cnt × 32 = bkt file size` and `ctl_entry_cnt × stride = ctrl file size` are exact with zero remainder:

```text
exp_and_others_bkt.bin   30112 B / 941 = 32.0     small_bkt.bin    1664 B /  52 = 32.0
exp_and_others_ctrl.bin   2848 B /  89 = 32.0  (trainium)
exp_and_others_ctrl.bin   1248 B /  78 = 16.0  (with_ln)
```

A set is **self-contained**: one `bkt` blob + one `ctrl` blob + the per-func scalar meta in the manifest. Every index inside a `ctrl` word and every `pwl_control` pointer in `profile_meta_data` is **absolute** into *this* set's `bkt`/`ctrl` table.

> **NOTE —** the per-func *coefficient* source of truth is `pwp_jsons/<func>_<Np>.json` (e.g. `exp_400p.json`). The `<set>.json` manifest denormalizes the scalar control fields into `profile_meta_data[]` and carries the index maps, but the blob coefficient bytes are a verbatim copy of the per-func profile's `.int` members (§3). The `_<Np>` suffix (the `act`-dict value in `act_info.json`) is an *approximation-budget* tag, **not** the section count — `exp:400` yields a 777-section LUT body.

---

## 2. The `bkt` Record — 32-Byte Cubic Segment

A `bkt` record is one piecewise-cubic segment. The Activation engine, given an input `v` that lands in this segment, computes `t = v − x` and evaluates `f(v) ≈ d0 + d1·t + d2·t² + d3·t³`.

### Byte layout

| Offset | Width | Field | Encoding | Meaning |
|---|---|---|---|---|
| `+0`  | 4 B | `d0` | `float32` LE (raw IEEE-754 bits) | degree-0 coeff (constant term `f(x)`) |
| `+4`  | 4 B | `d1` | `float32` LE | degree-1 coeff |
| `+8`  | 4 B | `d2` | `float32` LE | degree-2 coeff |
| `+12` | 4 B | `d3` | `float32` LE | degree-3 coeff |
| `+16` | 4 B | `x`  | `float32` LE | segment breakpoint / Taylor expansion centre |
| `+20` | 12 B | `pad` | `0x00 × 12` | reserved — **always zero** across the entire catalog |

Five fields occupy 20 bytes; the record is padded to a 32-byte (`8 × fp32`) granule — a natural SBUF/DMA tile width — leaving three reserved fp32 lanes (slots 5, 6, 7) that are zero in every shipped record. The float encoding is **plain IEEE-754**: each field is the verbatim 32-bit reinterpretation of the coefficient (the profile JSON's `.int` member). There is no bf16, no packed coeff format.

### Annotated parse

```c
// Parse one 32-byte bkt record at byte offset `off` in <set>_bkt.bin.
// All five fields are little-endian raw IEEE-754; the trailing 12 bytes are zero.
typedef struct {
    float d0, d1, d2, d3;   // cubic coefficients, degree 0..3
    float x;                // breakpoint (expansion centre)
} pwp_bkt_t;                // logical = 20 B; on disk = 32 B (12 B zero pad)

static void bkt_parse(const uint8_t *blob, size_t i, pwp_bkt_t *out) {
    const uint8_t *r = blob + i * 32;              // stride is 32 B in BOTH targets
    memcpy(&out->d0, r +  0, 4);                   // d0 @ +0
    memcpy(&out->d1, r +  4, 4);                   // d1 @ +4
    memcpy(&out->d2, r +  8, 4);                   // d2 @ +8
    memcpy(&out->d3, r + 12, 4);                   // d3 @ +12
    memcpy(&out->x,  r + 16, 4);                   // x  @ +16  (LAST on disk)
    // r[20..31] are reserved and must be zero (validated: 0 nonzero over 32,759 records)
}
```

### Worked parse — `exp_and_others_bkt.bin[0]`

This is the first LUT section of `exp` (the near-zero negative octave). Raw 32 bytes from `xxd -s 0 -l 32`:

```text
00000000: d0ff 7f3f d0ff 7f3f d0ff ff3e 8baa 2a3e   <- d0 d1 d2 d3
00000010: ffff 3fb6 0000 0000 0000 0000 0000 0000   <- x, then 12 B zero pad
```

Reading little-endian words:

| Field | Bytes (LE word) | hex value | float |
|---|---|---|---|
| `d0` | `d0 ff 7f 3f` | `0x3f7fffd0` | `0.99999714` |
| `d1` | `d0 ff 7f 3f` | `0x3f7fffd0` | `0.99999714` |
| `d2` | `d0 ff ff 3e` | `0x3effffd0` | `0.49999857` |
| `d3` | `8b aa 2a 3e` | `0x3e2aaa8b` | `0.16666619` |
| `x`  | `ff ff 3f b6` | `0xb63fffff` | `−2.861e-06` |
| pad  | `00 × 12` | `0` | — |

The matching JSON section, `exp_400p.json :: neg_exponents[0].exponent_sections[0]`, has `.int` members `x=b63fffff, d0=3f7fffd0, d1=3f7fffd0, d2=3effffd0, d3=3e2aaa8b` — **byte-identical**. This is the `e^t` Taylor expansion about `x ≈ −2.86e-06`: `f(t) ≈ 1 + t + t²/2 + t³/6`.

> **QUIRK — disk order is `x`-LAST, but the simulator's in-memory struct is `x`-FIRST.** The on-disk record is `[d0, d1, d2, d3, x]` (`x` at `+16`). The `PWPSim` in-memory `exponent_sect` (20 bytes, no pad) is reordered to `[x, d0, d1, d2, d3]` (`x` at `+0`). The loader reorders the disk d0-first record into the sim/HW x-first struct — the field *set* is identical, the *order* differs. A reader of the `.bin` must use `x @ +16`; a reader of the sim struct must use `x @ +0`. Do not assume disk == memory.

> **GOTCHA — the disk field is `x`, not `−x`.** Descriptions of the hardware evaluation as `dx = in + slot4` with `slot4 = −x` frame the breakpoint as a *negated* constant baked for a fused-add datapath. On disk the field is **un-negated**: `bkt[0].x = 0xb63fffff` matches `exp_400p.json`'s `.x.int = 0xb63fffff` bit-for-bit, same sign. The hardware may internally fold `t = v − x` into an add-with-negated-constant, but a parser reads `x` directly and computes `t = v − x` itself.

### The four saturation slots

After a func's LUT body, four `bkt` records are appended — the saturation-clamp polynomials, in the fixed disk order `[pos_low, neg_low, pos_high, neg_high]`. They are pointed to by `profile_meta_data`'s four `*_signal_pwl_control` fields (which are `bkt` indices, **not** `ctrl` indices). For `exp` these are `bkt[777..780]`:

| Slot | `bkt` idx | Bytes | Decoded | Meaning |
|---|---|---|---|---|
| `pos_low`  | 777 | `0000803f 0000803f 0000003f abaa2a3e \| x=0` | `d0=1.0, d1=1.0, d2=0.5, d3=0x3e2aaaab(0.16667), x=0` | unit `e^t` Taylor (small-signal clamp) |
| `neg_low`  | 778 | identical to 777 | same unit Taylor | small-signal clamp |
| `pos_high` | 779 | `0000807f 00000000 00000000 00000000 \| x=0` | `d0=0x7f800000(+inf), d1=d2=d3=x=0` | overflow → `+inf` |
| `neg_high` | 780 | all zero | `d0=0, d1=d2=d3=x=0` | underflow → `0` |

These match `exp_400p.json`'s `saturation_points` (`sat_point_pos_high.d0 = 0x7f800000`, `neg_high.d0 = 0`, `*_low.d0 = 0x3f800000`) byte-for-byte. The clamp poly is returned through the *same* Horner path as a normal bucket; with `d1=d2=d3=0` the result collapses to the constant `d0`.

> **GOTCHA — the small-signal `d3` differs from `bkt[0]`'s.** `bkt[777].d3 = 0x3e2aaaab` (the *exactly-rounded* `1/6` Taylor coeff about `x=0`), whereas `bkt[0].d3 = 0x3e2aaa8b` (the coeff fitted about `x = −2.86e-06`). They look the same in decimal (`0.16667`) but differ in the low mantissa bits. Always compare the raw `.int`, never the lossy decimal `float` string in the JSON.

Even pos-only funcs carry all four slots: `sqrt` (pos-only) stores `nan` (`0x7fc00000`) in its two `neg_*` slots — `√(x<0) = nan`. Resident budget-1 funcs (`identity`, `copy`, `relu`, …; `lut_size = 0`) have **no LUT body** — their `bkt` block is *exactly* the 4 saturation slots. `identity`'s four slots are all `{d0=0, d1=1.0, d2=0, d3=0, x=0}` ⇒ the linear pass-through `f(t) = 0 + 1·t = v`.

---

## 3. Catalog Validation — `bkt`

The `bkt` body of a func is laid out as its **neg-exponent** octave sections flattened first, then its **pos-exponent** octave sections, then the four saturation slots:

```text
bkt block(func f) = [ f.neg-octave sections | f.pos-octave sections | 4 sat slots ]
func_to_bkt_start_idx[f] = absolute index where f's block begins
bkt_entry_cnt(set)       = Σ_f (lut_size(f) + 4)     # the 4 sat slots per func
```

For `exp`: `lut_size = 777`, with 26 neg + 26 pos octaves whose actual section counts sum to `406 + 371 = 777`. The neg block fills `bkt[0..405]`, the pos block `bkt[406..776]`, then sat slots `bkt[777..780]`.

A firsthand programmatic sweep this session decoded all 777 `exp` LUT records with `struct.unpack("<5I")` at disk order `[d0,d1,d2,d3,x]` and diffed each against the neg-then-pos flattened JSON `.int` fields:

```text
exp LUT body : 777 sections — mismatches = 0, nonzero-pad = 0
FULL CATALOG : 35 sets, 409 funcs, 32,759 bkt records — 0 mismatch, 0 bad pad
```

Independent heroes corroborate: `sqrt_65536p` (1113 pos-only LINEAR sections — `d2=d3=0`, a tangent line per power-of-two octave — at `bkt_start=52` behind the 13 universal residents); `derivative_gelu_apprx_sigmoid_4096p` (the densest single octave: `extract_size=10` ⇒ 1024 cubic segments in one octave). `cp310/11/12` are md5-identical.

---

## 4. The `ctrl` Record — Packed `uint32` Octave Descriptor

The `ctrl` table is the **outer** index of the two-level LUT: one record per IEEE-754 exponent octave (per func, neg octaves then pos octaves). Each record's first 4 bytes is a packed little-endian `uint32`; the rest of the record is reserved zero, padded to the stride.

### Bitfield layout

```text
   bit  31 ......... 24  23 ............... 16  15 ......... 11  10 ............... 0
        [  reserved=0 ]  [   extract_size    ]  [ extract_lsb ]  [    bkt_base       ]
            8 bits            8 bits                5 bits            11 bits
```

| Bits | Mask | Field | Meaning |
|---|---|---|---|
| `[0:11]`  | `0x7FF` | `bkt_base` | absolute `bkt` entry index where this octave's section block begins (≤ 2047) |
| `[11:16]` | `0x1F`  | `extract_lsb` | mantissa right-shift for the in-octave section id (`= 23 − extract_size`) |
| `[16:24]` | `0xFF`  | `extract_size` | number of top mantissa bits used as the in-octave bucket index (octave addresses `2^extract_size` sections) |
| `[24:32]` | `0xFF`  | reserved | **zero** across the entire catalog (scan: 0 entries with any bit ≥ 24 set) |

The 11-bit `bkt_base` width is not arbitrary: the codegen DMAs `bkt` sections in **2048-entry tiles** (`2^11`), so a section base is addressed within a 2048-tile — the `libwalrus` `num_2048_tiles_cur_section` asserts name the tiling. The shipped sets never overflow it: max observed `bkt_base = 1443` (trainium) / `1223` (with_ln), both < 2048. Max `extract_size = 10`.

### Annotated unpack and the invariant

```c
typedef struct { uint16_t bkt_base; uint8_t extract_lsb, extract_size; } pwp_ctrl_t;

static void ctrl_unpack(uint32_t word, pwp_ctrl_t *out) {
    out->bkt_base     =  word        & 0x7FF;   // bits[0:11]  : absolute bkt index
    out->extract_lsb  = (word >> 11) & 0x1F;    // bits[11:16] : mantissa shift
    out->extract_size = (word >> 16) & 0xFF;    // bits[16:24] : # top mantissa bits
    // bits[24:32] are reserved and must be zero.
    // INVARIANT (holds on every populated octave, 0 violations over the catalog):
    //   extract_lsb == 23 - extract_size
    // Rationale: the section id takes the TOP `extract_size` of the 23 mantissa bits,
    // i.e. a right-shift of (23 - extract_size). The blob stores BOTH the precomputed
    // shift (extract_lsb, what HW uses) and the size (what the simulator uses, recomputing
    // 23 - size at runtime) — redundant but self-checking.
}

// The packer (LowerPWPImpl, libwalrus, stripped — reconstructed from the bytes):
static uint32_t ctrl_pack(uint16_t bkt_base, uint8_t extract_size) {
    return ((uint32_t)extract_size << 16)
         | ((uint32_t)(23 - extract_size) << 11)   // extract_lsb is DERIVED from size
         | (bkt_base & 0x7FF);
}
```

The reconstructed `ctrl_pack` reproduces all 52 `exp` `ctrl` words byte-for-byte. Note `extract_lsb` is **derived** from `extract_size`; a packer never stores an independent value.

### Stride — target-dependent

```text
ctrl stride: pwp_bin_trainium = 32 B/entry   (e.g. exp 2848 B / 89 = 32.0)
             pwp_bin_with_ln  = 16 B/entry   (e.g. exp 1248 B / 78 = 16.0)
bkt stride : 32 B in BOTH targets.
```

The 4-byte `ctrl` *word* is identical between targets for a co-resident func with the same `bkt` placement; only the trailing zero pad (28 B vs 12 B) differs. The `with_ln` entry-0 dump `00 b8 00 00 | 00 …` shows the word `0x0000b800` followed by 12 zero bytes.

> **GOTCHA — reading `with_ln` `ctrl` with a 32-byte stride reads every *other* entry and overflows.** The stride is target-dependent. Resolve it from the manifest: `stride = ctrl_file_size / ctl_entry_cnt`. Do **not** assume 32 B from the Trainium case.

### Empty-octave sentinels

One `ctrl` word is emitted per **declared** octave, including empty ones (`len(exponent_sections) = 0`). An empty octave stores `bkt_base = (running index, unchanged)`, `extract_lsb` and `extract_size = 0` — a sentinel the selector lands on but whose 0-section block is never indexed (the saturation clamp catches that exponent range first). Hence `ctl_entry_cnt` counts *declared* octaves, not just populated ones:

```text
ctl_entry_cnt(set) = Σ_f (len(neg_exponents) + len(pos_exponents))
```

---

## 5. Exponent-Stepped Addressing

The two blobs form a two-level lookup keyed by the IEEE-754 decomposition of the (sign-folded, pre-scaled) input `v`: the **biased exponent** selects the octave (`ctrl` record), and the **top mantissa bits** select the bucket within it (`bkt` record). This is the `find_pwp_nonsat_section` math at `libpwp_sim.so` offset `0x9340`, in symboled disassembly.

### Selection algorithm

```c
// Two-level non-saturated section lookup.  Mirrors find_pwp_nonsat_section @0x9340.
// Preconditions: special values (0, ±inf, nan) and the 4 saturation regions have already
// been handled (§6); `v` is the in-range, sign-folded input.
static const pwp_bkt_t *select_bucket(const uint32_t *vbits,
                                       const pwp_ctrl_t *ctrl_octaves,  // outer index
                                       const pwp_bkt_t  *bkt) {         // inner table
    uint32_t I    = *vbits;
    uint32_t expo = (I >> 23) & 0xFF;          // 8-bit biased exponent  -> OUTER index
    uint32_t mant =  I        & 0x7FFFFF;      // 23-bit mantissa        -> INNER index

    // OUTER: one ctrl word per octave; the engine's region array is indexed by `expo`.
    // In the sim: region = region_base + 40 * (uint8_t)expo;  (40 = sizeof pwp_nonsat_region)
    const pwp_ctrl_t *w = &ctrl_octaves[octave_index_for(expo)];

    // INNER: top `extract_size` mantissa bits become the in-octave section id.
    uint32_t sectid = mant >> w->extract_lsb;  // == mant >> (23 - extract_size)
                                               // assert sectid < octave.num_sections
    return &bkt[w->bkt_base + sectid];         // absolute bkt index = base + sectid
}
```

Because the mantissa is a fixed 23 bits and `extract_size` grows by one per exponent step, the float's segmentation gets exponentially finer toward large `|x|` — the NeuronCore "log-uniform" PWL grid. For `exp` the per-octave size walk is `1, 1, …, 1` (exp `−19..−2`), then `2` (exp `−1`), `4` (exp `0`), `8` (exp `+1`), … up to `256` (exp `+6`).

### `bkt_base` accumulates *actual* sections, not declared capacity

`extract_size = s` declares an octave *capacity* of `2^s` buckets, but an octave may **materialize fewer** sections than its capacity (the topmost octave of every multi-section func). The `bkt_base` of the *next* octave advances by the **actual** materialized count:

```text
bkt_base(func f, octave i) = func_to_bkt_start_idx[f]
                           + Σ_{j<i} len( octave[j].exponent_sections )   # ACTUAL, not 2^size
```

This is why the pos block of `exp` begins at `bkt_base = 406` (= the 406 actual neg sections) rather than at `Σ num_sections` (which would overcount). The slack — the high buckets of a saturating octave — is caught by the high-saturation clamp before it is ever bucket-indexed.

The accumulator walks `len(exponent_sections)` — the *actual* stored sections — and not `num_sections`, the `2^extract_size` capacity. The difference is large: summing capacities gives `exp` 1056 against its true `lut_size` of 777. Worked through, `exp` octave `+6` (biased exponent 133) declares `num_sections = 256` (`extract_size = 8`) but materializes only **99** sections on the positive side and **134** on the negative. `large_pos_signal_mantissa_threshold = 3240472`, and `3240472 >> (23 − 8) = 3240472 >> 15 = 98`, so buckets `0..98` exist (99 of them) and anything at bucket ≥ 99 hits the `+inf` clamp. The mantissa threshold *is* the LUT truncation point.

> **GOTCHA —** `bkt_base` accumulates *materialized* sections, not declared `num_sections`. Summing the declared capacities overcounts every func with a truncated top octave.

### Worked `ctrl` decodes

From `exp_and_others_ctrl.bin`, trainium, 32-byte stride (`xxd` + bitfield split):

| `ctrl` idx | offset | word | `bkt_base` | `extract_lsb` | `extract_size` | octave |
|---|---|---|---|---|---|---|
| 0  | `0x000` | `0x0000b800` | 0   | 23 | 0 | neg exp `−19`, 1 section (whole mantissa → 1 bucket) |
| 1  | `0x020` | `0x0000b801` | 1   | 23 | 0 | neg exp `−18`, 1 section |
| 18 | `0x240` | `0x0001b012` | 18  | 22 | 1 | neg exp `−1`, 2 sections (`sectid = mant >> 22 ∈ {0,1}`) |
| 25 | `0x320` | `0x00087910` | 272 | 15 | 8 | neg exp `+6`, 256-capacity (134 materialized) |
| 26 | `0x340` | `0x0000b996` | 406 | 23 | 0 | **first pos octave** — `base=406` = the neg-section count |
| 51 | `0x660` | `0x00087aa6` | 678 | 15 | 8 | pos exp `+6`, 256-capacity (99 materialized) |
| 64 | `0x800` | `0x0000bb2d` | 813 | 23 | 0 | `tanh` octave — `base=813` = `func_to_bkt_start_idx[tanh]` (ABSOLUTE) |

`ctrl[26].bkt_base = 406` is an independent cross-check on the neg-then-pos ordering: the pos block begins exactly where the 406 neg sections end. `ctrl[64].bkt_base = 813` confirms the base is absolute into the set's `bkt` table even for a func that does not start at index 0. Every `extract_lsb` obeys `23 − extract_size` (23−0=23, 23−1=22, 23−8=15).

### Catalog validation — `ctrl`

A firsthand sweep this session decoded all 52 `exp` `ctrl` words and rebuilt each expected `(bkt_base, extract_lsb, extract_size)` from the neg-then-pos JSON octaves:

```text
exp ctrl    : 52 octaves — mismatches = 0, (extract_lsb != 23 - extract_size) violations = 0
FULL CATALOG: 35 sets, 3,071 ctrl words — 0 mismatch on (base, lsb, size);
              extract_lsb == 23 - extract_size on every populated octave.
              bits[24:32] == 0 always.
```

> **NOTE — cross-target `ctrl` word identity holds only for a set's *first* func.** `exp`'s 52 `ctrl` words are byte-identical between Trainium and `with_ln` *because* `exp` is the first func in both `exp_and_others` sets (`func_to_bkt_start_idx[exp] = 0`), so its absolute `bkt_base` coincides. In general only the per-octave shape `(extract_lsb, extract_size)` is target-independent; `bkt_base` shifts by the `func_to_bkt_start_idx` delta. `sigmoid` sits at `bkt_start` 136 (trainium) vs 132 (`with_ln`), so every `sigmoid` octave's base differs by exactly +4.

---

## 6. `func_id` and the Per-Function Control Config

The `ctrl.bin` table is per-*octave*; the per-*function* config — which selects the func variant, its symmetry fold, its clamps, and its special-value results — lives in the manifest's `profile_meta_data[]`, one 30-field scalar object per co-resident func, in func-array order — a uniform 30-key schema across all 409 funcs.

### `func_id` — the silicon LUT selector

`func_id` is the **chip-arch-native** activation opcode id the engine uses to pick this func within the loaded set. It is **target-dependent**: across the catalog, 309/309 trainium ids equal the profile's `sunda_id` and 100/100 with_ln ids equal `tonga_id`, with no exceptions.

```text
func_id  ==  sunda_id   on pwp_bin_trainium
func_id  ==  tonga_id   on pwp_bin_with_ln
```

For `exp`, `func_id = 7 = sunda_id = tonga_id` (coincidentally equal on both arches). For funcs where the arches diverge, `reciprocal` is `sunda 129 / tonga 15`, `abs` is `sunda 33 / tonga 22`. This is **not** the BIR `bir::ActivationFunctionType` (a third id-space); it is the silicon selector the loaded set is indexed by. A `LoadActFuncSet` installs the set; a later `Activation` names its func by this silicon LUT code and the engine resolves that func's `bkt`/`ctrl` block within the resident set.

### The 30 scalar fields (grouped)

`profile_meta_data[exp]` — every field is a denormalized copy of the func's `pwp_jsons` profile:

```text
selector :   func_name="exp_400p", func_id=7
special  :   fzero_result=0x3f800000(1.0=e^0)  fpinf_result=0x7f800000(+inf)
             fninf_result=0(e^-inf)             fnan_result=0x7fc00000        (raw fp32 bits)
symmetry :   symmetry_opt_en=0  sym_invert_sign_point  symmetry_opt_use_neg_region
             symmetry_point (raw fp32)          [exp: all 0 — no fold]
bias     :   imm_bias=0  exp_offset=-19         fma_const_0  fma_const_1  fma_indirection_src_sel
ctrl ptrs:   pwl_control_base_pos=26  pwl_control_base_neg=0   (CTL-table indices)
sat ptrs :   pos_small_signal_pwl_control=777  neg_small=778
             pos_large=779  neg_large=780        (BKT-table indices of the 4 clamp slots)
sat thr  :   small_pos_signal_exp_threshold=108  large_pos_signal_exp_threshold=133
             large_pos_signal_mantissa_threshold=3240472   (LARGE regions only carry mantissa)
range    :   lower_bound  upper_bound (raw fp32)  use_multipass=false
```

> **GOTCHA — two distinct index spaces in `profile_meta_data`.** `pwl_control_base_pos`/`_neg` are **`ctrl`-table** indices (where the func's pos/neg octave sub-block starts; `exp`: 26 / 0). The four `*_signal_pwl_control` are **`bkt`-table** indices (the four clamp slots; `exp`: 777–780). The latter exceed `ctl_entry_cnt = 89` precisely because they live in the 941-entry `bkt` table. Confusing them mis-addresses the clamps.

A few catalog-wide facts worth a reimplementer's attention. `imm_bias = 1` for exactly `{reciprocal_400p, copy_1p, memset_zero_1p, is_finite_1p}`; nonzero `fma_const_*` / `fma_indirection_src_sel` for *only* `parametric_relu_1p` (the prelu slope blend, `fma_const_0 = 0x3f800000`, `src_sel = 2`); `use_multipass = false` for every shipped meta (the two-pass `ln_4p_0mp/1mp` profiles exist in `pwp_jsons` but are not bound into any shipped set). The four `*_low` (small-signal) regions carry **no** mantissa threshold — `sat_point_*_low.mantissa_point == 0` for all 409 funcs.

---

## 7. End-to-End: a Worked Evaluation

Putting §2, §4, §5, §6 together — the full chain from the loaded set to a scalar result, validated on `exp(0.5)`:

```text
v = 0.5  ->  bits = 0x3f000000,  biased_exp = 126 (unbiased -1),  mant = 0
 1. special/saturation:  126 < large_pos.exp_thr(133) and > small_pos.exp_thr(108) -> in range
 2. symmetry fold:       exp.symmetry_opt_en = 0  -> sel = v (no fold)
 3. decompose:           expo = 126,  mant = 0
 4. outer (octave -1):   ctrl word for exp=-1 -> extract_size = 1  (2 buckets)
 5. inner:               sectid = mant >> (23 - 1) = 0 >> 22 = 0
                         pos block base = 406; cumulative pos sections before octave -1 -> 424
                         bkt[424] = JSON section byte-for-byte (x=0x3f1fffff, d0=0x3fef22ae)
 6. cubic Horner:        x = 0.625;  t = v - x = -0.125
                         f = d0 + d1*t + d2*t^2 + d3*t^3 = 1.64870270
                         (true e^0.5 = 1.64872127 — within the 400-budget tolerance)
```

The reference numeric model (`PWPSim::Simulator::evaluate_generic`) accumulates the higher-degree terms in **double** and computes `t³` via libm `pow` — a bit-exact reimplementation must replicate that. The selector branch order is: special values → symmetry fold → 4-way saturation clamp → in-range bucket lookup → cubic → symmetry reconstruct. The saturation clamp runs *before* the bucket lookup, which is what keeps the truncated high buckets (§5) from ever asserting `sectid < num_sections`.

The producer chain is `LoadActFuncSet` (BIR IT6) carrying a single `act_func_set_id` (the `act_info.json` array order) — it has **no** LUT source address; the `(bkt, ctrl)` image is engine-table-resident and the id selects which set is active. (The `LoadActFuncSet` codegen and IT6 wire layout are detailed in [10.6 (LoadActFuncSet)](loadactfuncset.md); the `profile_json` schema is [10.5 (activation profile-json)](activation-profile-json.md).)

---

## 8. Reimplementation Recipe

To **parse** a shipped set:

1. Read `<set>.json`: `bkt_entry_cnt`, `ctl_entry_cnt`, `func_to_{bkt,ctl}_start_idx`, `profile_meta_data[]`.
2. `bkt` stride = 32 B; `ctrl` stride = `ctrl_file_size / ctl_entry_cnt` (32 trainium / 16 with_ln — never assume).
3. For each func `f`: its `bkt` block = `bkt[func_to_bkt_start_idx[f] .. +lut_size+4)` (LUT body then 4 sat slots); its `ctrl` block = `ctrl[func_to_ctl_start_idx[f] .. +neg_oct+pos_oct)`.
4. Parse each `bkt` record as `[d0,d1,d2,d3,x]` fp32 @ 0/4/8/12/16; assert bytes 20..31 == 0.
5. Unpack each `ctrl` word: `base = w & 0x7FF`, `lsb = (w>>11) & 0x1F`, `size = (w>>16) & 0xFF`; assert `bits[24:32] == 0` and `lsb == 23 − size` (for populated octaves).

To **pack** a fresh set from per-func profiles:

1. Lay out `bkt`: per func, neg-octave sections, pos-octave sections, then the 4 sat slots; emit each as `[d0,d1,d2,d3,x]` from the profile `.int` + 12 zero bytes.
2. Lay out `ctrl`: per func, one word per declared octave (neg then pos); `bkt_base` = `func_to_bkt_start_idx[f]` + running sum of *actual* preceding sections; `word = (size<<16) | ((23−size)<<11) | base`; pad to stride.
3. Emit `func_id = sunda_id` (trainium) / `tonga_id` (with_ln) and the rest of `profile_meta_data` from the profile.

> **NOTE — verification cross-checks.** `bkt_entry_cnt == Σ_f (lut_size+4)`; `ctl_entry_cnt == Σ_f (neg_oct+pos_oct)`; `bkt_file_size == bkt_entry_cnt × 32`; `ctrl_file_size == ctl_entry_cnt × stride`; `ctrl[first pos octave].bkt_base == Σ neg sections`; `large_*_mantissa_threshold >> extract_lsb == #materialized sections in the top octave`. All hold across the full 35-set catalog with zero mismatch.

---

## 9. Confidence and Cross-References

Byte decode plus JSON match across the full 35-set / 409-func / 32,759-bkt + 3,071-ctrl catalog, zero mismatch, covers: the 32-byte `bkt` record `[d0,d1,d2,d3,x]+12B-zero-pad` (disk d0-first, raw IEEE-754 = profile `.int`); neg-octaves-then-pos disk ordering; the 4 saturation slots `[pos_low,neg_low,pos_high,neg_high]`; the `ctrl` `uint32` bitfields `base[0:11] / lsb[11:16] / size[16:24]`, reserved `[24:32]=0`; `extract_lsb == 23 − extract_size`; `ctrl` stride 32 B trainium / 16 B with_ln (word identical); `bkt_base` = `func_to_bkt_start_idx` + Σ actual sections; `func_id = sunda_id`/`tonga_id`; cp310/11/12 byte-identical.

Symbol- and disasm-grounded: the consumer selection math (`find_pwp_nonsat_section @0x9340`, `libpwp_sim.so`, symboled disasm — `sectid = mant >> (23 − extract_size)`, `region = base + 40·expo`); the 2048-tile DMA rationale for the 11-bit `bkt_base` (`libwalrus` `num_2048_tiles_cur_section` rodata asserts). **[INFERRED]** — the blobs are precomputed offline (KaenaPWP) and merely selected/referenced by `LowerPWPImpl`, not recomputed at compile time; this is a rule derived over the catalog, not read off a code path. **GAP** (→ §10.6): the `generateInstLoadActFuncSet` IT6 wire layout and the exact `(size<<16)|((23−size)<<11)|base` packing instruction (the producing lib is stripped, so the bitfield split is proven from the *bytes* and the *consumer*, not from the packer code itself).

Cross-references: [10.1 (`pwp-model.md`)](pwp-model.md) for the evaluation model and the `find_pwp_nonsat_section` algorithm this format feeds; [10.5 (activation profile-json)](activation-profile-json.md) for the per-set `profile_json` manifest that indexes these blobs (the per-*function* coefficient source of truth is `pwp_jsons`, §10.1); [10.6 (LoadActFuncSet)](loadactfuncset.md) and [10.7 (set-cover)](set-cover.md) for the set-selection and `LoadActFuncSet` codegen.
