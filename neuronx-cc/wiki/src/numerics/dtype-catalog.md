# Neuron Dtype Catalog and x4-Packing

> *All addresses, offsets, and symbols on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel). The dtype enum body and JSON serializers live in `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`); the codegen wire-tag / stride LUTs live in `neuronxcc/starfish/lib/libwalrus.so` (GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`); the simulator's mirror LUTs live in `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`); the host-side dtype predicates and promotion lattice live in `neuronxcc/starfish/penguin/dtypes.cpython-310-x86_64-linux-gnu.so` (build-id sha1 `01c37fd6d3190601e1104533925fcb08b5d4a066`); the x4 / cast façade in `neuronxcc/starfish/support/dtype.cpython-310-x86_64-linux-gnu.so` (build-id sha1 `6391eb2ee151588a95478f841f35f787a714c3a9`). For `.text`/`.rodata`, VA == file offset; cp311/cp312 rebuild every library at shifted addresses — re-confirm raw addresses against the target wheel. The enum ordinals, LUT contents, and field offsets are stable across the three Python ABIs.*

## Abstract

This is the **reference page** for the Neuron compiler's numeric type system: every dtype the toolchain can name, its byte size, its on-wire tag, and how the sub-byte FP4/FP8 "x4-packed" lane containers work. Every other Part 9 page — and every ISA encoding page that stamps a dtype field into a bundle — keys off the roster fixed here. The numbers on this page are not re-derived elsewhere; they are cited.

The Neuron dtype alphabet is **wider than IEEE 754**. Alongside the familiar `bfloat16` / `float16` / `float32` and the integer family, it carries five OCP-microscaling formats — `float8_e4m3` / `float8_e5m2` (FP8), `float8_e8m0fnu` (the E8M0 block-scale exponent), and `float4_e2m1fn` (FP4) — plus `float32r`, a TF32-style reduced-precision fp32, and three **x4-packed lane containers** (`float4_e2m1fn_x4`, `float8_e4m3fn_x4`, `float8_e5m2_x4`) that hold four sub-byte elements per addressable word. The packing is the structural novelty: a single 2-byte or 4-byte container stores four logical FP4/FP8 lanes, and the MX matmul path multiplies the per-partition element count by 4 to recover the logical contraction extent. A reimplementer who treats an `_x4` type as a single scalar will compute the wrong K-extent on every MX matmul.

The page is built in three layers, matching the three binaries that own them. The **front-end enum** (`bir::Dtype`, `libBIR.so`) is the 20-member alphabet that a JSON loader parses and a `MemoryLocation` carries; the [BIR Dtype Tables](../bir/dtype-tables.md) page (7.6) owns its definitive switch body and the three backend LUTs, which this page reproduces as the master roster. The **on-wire tag** (`NEURON_ISA_TPB_DTYPE`, `libwalrus.so`) is the byte the silicon encoder actually stamps — a permutation of the enum, ordered by container-size class, reproduced here and cross-checked against the [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) (2.23). The **host predicates and cast surface** (`is_x4_dtype` / `launder_x4_dtype` / `static_cast_*`, the `penguin`/`support` Cython modules) live above both; the [nki/dtype Façade](../nki/dtype-facade.md) (6.3.3) is the Python-facing veneer over them.

For reimplementation, the contract is:

- **The complete dtype roster** — every member, its ordinal, byte size, wire-tag, and alignment, with the x4 lane-container variants called out.
- **The x4 packing model** — four sub-byte lanes per word, the container/logical distinction, and the `×4` K-extent expand the MX path applies.
- **The classifier predicates** — `is_x4_dtype` / `get_x1_from_x4` / `launder_x4_dtype` and the `sizeinbytes` / wire-tag LUTs that consume them.

| | |
|---|---|
| **Front-end enum** | `bir::Dtype` — 20 members, ordinals `0..19`, `bir::Dtype2string` @0x2641e0 (libBIR) |
| **Wire-tag enum** | `NEURON_ISA_TPB_DTYPE` — byte stamped into the 64-byte bundle |
| **Wire-tag LUT** | `byte_1DFBAD0` @0x1DFBAD0 (libwalrus `.rodata`, 20 × `u8`) |
| **Stride LUT** | `qword_1DFC040` @0x1DFC040 (libwalrus `.rodata`, 20 × `u64`) — container bytes |
| **Sim stride mirror** | `qword_5F74E0` @0x5F74E0 (libBIRSimulator) — byte-identical 20 × `u64` |
| **x4 dtype set** | `{2 = float4_e2m1fn_x4, 8 = float8_e4m3fn_x4, 9 = float8_e5m2_x4}` |
| **x4 expand factor** | 4 — `if (dt==2 \|\| (unsigned)(dt-8)<=1) K *= 4` |
| **x4 / cast façade** | `support/dtype.so` — `from neuron_dtypes import …` (45 re-exports) |
| **Promotion lattice** | `promote_type` @ `penguin/dtypes.so` — 4-way closure (`_promote_floats`/`_ints`/`_float_int`/`_is_floating`) |

---

## The master roster

Every numeric type the Neuron compiler can name is a member of the `bir::Dtype` enum: exactly **20** members, ordinals `0..19`, with no member `≥ 20` (the dense `Dtype2string` switch traps any higher value, and every consumer guards with `cmp …,0x13; ja default`). The table below is the complete catalog. `Int` is the `bir::Dtype` ordinal; `Name` is the verbatim `Dtype2string` literal; `Bytes` is the container byte-size (`qword_1DFC040[Int]`, the stride used for address arithmetic); `WireTag` is `byte_1DFBAD0[Int]` (the byte stamped into a CoreV4 bundle's dtype field); `Class` is the format family.

| Int | Name | Bytes | WireTag | Lanes/word | Class | Confidence |
|----:|------|------:|--------:|:----------:|-------|:----------:|
| 0 | `uint8` | 1 | `0x03` (3) | 1 | unsigned int 8 | CONFIRMED |
| 1 | `int8` | 1 | `0x02` (2) | 1 | signed int 8 | CONFIRMED |
| 2 | `float4_e2m1fn_x4` | 2 | `0x10` (16) | **4** | FP4 E2M1 ×4 | CONFIRMED |
| 3 | `float8e3` | 1 | `0x0d` (13) | 1 | FP8 (legacy e3) | CONFIRMED |
| 4 | `float8e4` | 1 | `0x0e` (14) | 1 | FP8 (legacy e4 ≡ e4m3) | CONFIRMED |
| 5 | `float8_e4m3fn` | 1 | `0x0e` (14) | 1 | FP8 E4M3 (finite) | CONFIRMED |
| 6 | `float8_e8m0fnu` | 1 | `0x03` (3) | 1 | **E8M0 block-scale** | CONFIRMED |
| 7 | `float8e5` | 1 | `0x0f` (15) | 1 | FP8 (legacy e5 ≡ e5m2) | CONFIRMED |
| 8 | `float8_e4m3fn_x4` | 4 | `0x0e` (14) | **4** | FP8 E4M3 ×4 | CONFIRMED |
| 9 | `float8_e5m2_x4` | 4 | `0x0f` (15) | **4** | FP8 E5M2 ×4 | CONFIRMED |
| 10 | `uint16` | 2 | `0x05` (5) | 1 | unsigned int 16 | CONFIRMED |
| 11 | `int16` | 2 | `0x04` (4) | 1 | signed int 16 | CONFIRMED |
| 12 | `bfloat16` | 2 | `0x06` (6) | 1 | BF16 (1-8-7) | CONFIRMED |
| 13 | `float16` | 2 | `0x07` (7) | 1 | FP16 (1-5-10) | CONFIRMED |
| 14 | `uint32` | 4 | `0x09` (9) | 1 | unsigned int 32 | CONFIRMED |
| 15 | `int32` | 4 | `0x08` (8) | 1 | signed int 32 | CONFIRMED |
| 16 | `float32` | 4 | `0x0a` (10) | 1 | FP32 (1-8-23) | CONFIRMED |
| 17 | `float32r` | 4 | `0x0b` (11) | 1 | **FP32 reduced (TF32-like)** | CONFIRMED |
| 18 | `uint64` | 8 | `0x01` (1) | 1 | unsigned int 64 | CONFIRMED |
| 19 | `int64` | 8 | `0x0c` (12) | 1 | signed int 64 | CONFIRMED |

The roster is byte-verified from two `.rodata` arrays, both dumped at their VA (== file offset):

```text
$ xxd -s 0x1DFBAD0 -l 24 libwalrus.so          ; the wire-tag LUT
01dfbad0: 03 02 10 0d 0e 0e 03 0f  0e 0f 05 04 06 07 09 08   byte_1DFBAD0[0..15]
01dfbae0: 0a 0b 01 0c  | 11 15 17 13                          [16..19] | adjacent LUT

$ xxd -s 0x1DFC040 -l 160 libwalrus.so         ; the stride LUT (20 × u64, LE)
  1 1 2 1 1 1 1 1 4 4 2 2 2 2 4 4 4 4 8 8       qword_1DFC040[0..19]
```

The first 20 bytes of `byte_1DFBAD0` are the table; the four trailing bytes (`11 15 17 13`) are a *different* adjacent LUT, kept out of range by every consumer's `cmp …,0x13` bound. The stride array runs exactly 20 qwords before the `.rodata` strings begin — there is no 21st entry. [CONFIRMED — both arrays dumped this pass; byte-identical to the [BIR Dtype Tables](../bir/dtype-tables.md) (7.6) master table and to the [ISA enum-ordinal](../isa/isa-enum-ordinals.md) (2.23) `_DTYPE` enum.]

> **QUIRK —** the wire-tag column is a *permutation*, not the enum ordinal and not `ordinal + k`. `int8`→2 but `uint8`→3; `bfloat16`→6 but `float16`→7; and the 32-bit class swaps sign-vs-unsigned: `uint32`→9, `int32`→8. The reason is that `NEURON_ISA_TPB_DTYPE` orders types by **container-size class** (8b → 16b → 32b → 64b → FP8 → FP4), an ordering unrelated to `bir::Dtype`. Stamp `wire_tag = byte_1DFBAD0[dtype]` and never assume identity. The map is also **many-to-one**: `float8_e4m3fn` (Int 5), `float8e4` (Int 4), and `float8_e4m3fn_x4` (Int 8) all collapse to wire-tag 14 — you cannot recover the Dtype from the tag.

> **NOTE — the pre-CoreV4 LUT differs at one index.** The older CoreV2/V3 wire-tag LUT `byte_1DF5760` (`03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c`) is byte-identical to `byte_1DFBAD0` **except at index 2**: pre-v4 maps `float4_e2m1fn_x4` to wire-tag `0x05` (colliding with the `uint16` tag), while CoreV4 gives FP4-x4 its own tag `0x10`. FP4 is a gen-4 MX-era type; CoreV3 matmul actually *rejects* the x4 dtypes upstream rather than consuming them. The 17 non-FP4 tags are shared across all arches. [CONFIRMED — both LUTs `xxd`'d; the single-index divergence corroborated by 2.23 and 7.6.]

---

## The x4-packed lane containers

Three Neuron dtypes are not scalars — they are **lane containers** that pack four logical sub-byte FP4/FP8 elements into one addressable word. These are the OCP-microscaling (MXFP) data formats the gen-4 MX matmul consumes:

| x4 container | Lane format | Lanes | Container bytes | Bits/lane | Element dtype (`get_x1_from_x4`) |
|---|---|:--:|:--:|:--:|---|
| `float4_e2m1fn_x4` (2) | FP4 E2M1 | 4 | 2 | 4 | `float4_e2m1fn` |
| `float8_e4m3fn_x4` (8) | FP8 E4M3 | 4 | 4 | 8 | `float8_e4m3fn` |
| `float8_e5m2_x4` (9) | FP8 E5M2 | 4 | 4 | 8 | `float8_e5m2` |

### The packing model

Each container word holds four lanes laid out densely from the low bits up. For FP4-x4, four 4-bit E2M1 values fill a 16-bit word (`4 × 4 = 16` bits → 2-byte container, stride 2). For the two FP8-x4 types, four 8-bit FP8 values fill a 32-bit word (`4 × 8 = 32` bits → 4-byte container, stride 4 — addressed as a `u32`).

```text
float4_e2m1fn_x4   (Int 2, stride 2):     [ lane0 | lane1 | lane2 | lane3 ]
  16-bit container                          4b      4b      4b      4b

float8_e4m3fn_x4   (Int 8, stride 4):     [ lane0 | lane1 | lane2 | lane3 ]
float8_e5m2_x4     (Int 9, stride 4):       8b      8b      8b      8b
  32-bit (u32) container
```

The **container** byte-size is what drives address arithmetic: `qword_1DFC040[2] = 2` and `[8] = [9] = 4` — *not* the logical lane width (which would be 0.5 / 1 / 1 byte). The matmul/ldweight encoder folds this stride into the start address as `start = base + (numContainers − 1) · stride[dtype]`, walking the data region by packed containers. [CONFIRMED — stride LUT bytes; the encoder fold is `imul rbx, qword[0x1dfc040+rax*8]` in `generateLdweightMx` @0x143e350.]

> **GOTCHA —** the intra-word lane *order* — which of the four logical lanes occupies which sub-field of the container after the hardware de-interleave — is **not** encoded in any compiler LUT examined. The compiler tables fix only the container size and the `×4` expand factor; the lane permutation is a hardware micro-op detail. A reimplementer must not assume a particular sub-field assignment from these tables. [SPECULATIVE — not in the binary.]

### The `×4` K-extent expand

The container/logical distinction surfaces a second time, in the contraction extent. The MX matmul reads `numElementsPerPartition` (the count of *containers* per partition) and multiplies it by 4 to recover the logical element count `K`. The gate is a compact two-comparison test on the dtype ordinal, applied identically across the encoder, the verifier, and the simulator:

```c
// the x4 expand gate — byte-identical in three binaries
K = getNumElementsPerPartition(ap);            // count of packed containers
if (ap.dtype == 2 ||                            // float4_e2m1fn_x4
    (unsigned)(ap.dtype - 8) <= 1)              // {8,9}: float8_e4m3fn_x4 / _e5m2_x4
    K *= 4;                                      // → logical lane count
```

The `(unsigned)(dt - 8) <= 1` idiom tests `dt ∈ {8, 9}` in a single unsigned compare; together with the explicit `dt == 2` it covers exactly the x4 set `{2, 8, 9}`. In the CoreV4 MX descriptor encoder (`assignAccessForMX<MXMEM_PATTERN1D>` @0x150e2f0) this is the disasm sequence `cmp edx, 2` (0x150e603) / `cmp edx, 1` (0x150e60b, on `dt − 8`) / `shl eax, 2` (0x150e610, the `× 4`), with the result stored as the K-extent `WORD` at descriptor offset `+0x8`. The same rule appears in the BIR simulator's `visitInstMatmultMx` (`Ki *= 4 iff dtype∈{2,8,9}`) and in the MX verifier — one rule, three consumers, byte-exact. [CONFIRMED — disasm of `assignAccessForMX` @0x150e2f0; cross-confirmed against the simulator `dequantizeMx` path and the `qword_5F74E0`/`qword_1DFC040` stride-LUT identity.]

> **QUIRK —** the **packed** K (containers) drives *addressing* — `start + (K_packed − 1) · stride[dtype]` walks the data region — while the **×4 logical** K drives the descriptor's K-extent field and every element-count contract the verifier checks. The two K values differ by exactly the factor of 4. Mixing them is the classic MX bug: address by logical K and you overrun the buffer by 4×; size the contraction by packed K and you under-count by 4×.

---

## The classifier predicates

The host compiler reasons about dtypes through a small set of predicates. The `_x4` machinery is re-exported into `support/dtype.so` from the external `neuron_dtypes` C extension (the façade module performs a single `from neuron_dtypes import …` and re-exports 45 names; it defines **no** function bodies of its own). The names below are CONFIRMED present in the façade's string pool; their bodies live in `neuron_dtypes`, which is not shipped in this wheel, so the semantics are reconstructed from name + call shape and cross-checked against the in-binary encoder/verifier behaviour.

| Predicate | Signature | Returns | Confidence |
|---|---|---|:--:|
| `is_x4_dtype(dt)` | `dtype → bool` | `dt ∈ {float4_e2m1fn_x4, float8_e4m3fn_x4, float8_e5m2_x4}` | STRONG |
| `get_x1_from_x4(dt)` / `x4_to_x1(dt)` | `dtype → dtype` | the scalar element dtype of the container (e.g. `…_e4m3fn_x4 → float8_e4m3fn`) | STRONG |
| `launder_x4_dtype(dt)` | `dtype → dtype` | strips the `_x4` tag → the underlying x1 element dtype | STRONG |
| `sizeinbytes(dt)` | `dtype → int` | container byte-size (matches `qword_1DFC040[Int]`) | STRONG |
| `is_float_type(dt)` / `is_int_type(dt)` | `dtype → bool` | format-family membership | STRONG |
| `dtype2str(dt)` / `str2dtype(s)` | name ↔ dtype round-trip | (matches `bir::Dtype2string` / `string2Dtype`) | STRONG |

### `is_x4_dtype` and `launder_x4_dtype`

```c
// support/dtype.so → neuron_dtypes (re-exported; body external)
// names CONFIRMED in __pyx_k_ pool: is_x4_dtype, launder_x4_dtype,
//   get_x1_from_x4, x4_to_x1  — semantics STRONG (set-membership + element-map)
bool is_x4_dtype(dtype dt) {
    return dt == float4_e2m1fn_x4         // Int 2
        || dt == float8_e4m3fn_x4         // Int 8
        || dt == float8_e5m2_x4;          // Int 9
}

dtype get_x1_from_x4(dtype dt) {          // == x4_to_x1: container → element
    switch (dt) {
      case float4_e2m1fn_x4: return float4_e2m1fn;   // E2M1
      case float8_e4m3fn_x4: return float8_e4m3fn;   // E4M3
      case float8_e5m2_x4:   return float8_e5m2;     // E5M2
    }
}

// "launder" = pass an x4 dtype through the x1 type-system and back.
// Strip the container tag so promotion / comparison logic reasons about the
// ELEMENT type, then the caller re-applies the x4 tag to the container result.
dtype launder_x4_dtype(dtype dt) {
    return is_x4_dtype(dt) ? get_x1_from_x4(dt) : dt;
}
```

The naming `launder` is literal: an `_x4` container dtype is "laundered" through the scalar type-system so that promotion and merge logic — which only understands x1 element types — can reason about it, after which the `_x4` tag is restored on the result container. The `promote_type` and `merge_type` callers launder x4 inputs to their element dtype *before* promotion, then re-tag the container. [STRONG — the four x4 names are CONFIRMED in `support/dtype.so` (`strings` shows `is_x4_dtype`, `launder_x4_dtype`, `get_x1_from_x4`, `x4_to_x1` plus the three `_x4` singletons); the bodies are in the external `neuron_dtypes`, so the set-membership and element-map semantics are inferred from name + the encoder's `{2,8,9}` gate.]

> **NOTE —** the binary carries the four x4 element-dtype names as a slightly inconsistent pool. The forward FP4 cast is spelled `static_cast_fp32_to_float4_e2m1fn_x4`, but the reverse is `static_cast_float4_e4m2fn_x4_to_fp32` — `e4m2` instead of `e2m1`. Both spellings are CONFIRMED present; the reverse name is an upstream typo. Both refer to the same 4-bit E2M1 format. A name-based loader must accept the typo'd spelling.

---

## The reference scalar casts

The `support/dtype.so` façade re-exports a family of `static_cast_<from>_to_<to>` reference casts from `neuron_dtypes`. These are the **host-side** byte re-encoders that `dt.static_cast(value)` invokes at compile time when a constant is folded to a target dtype; their **device-side** twins are the BIR primitive `bir::CastToNewDType` and the simulator's `cast_to`. The cast *bodies* are not in this wheel (they live in `neuron_dtypes`), so the bit-twiddling below is reconstructed from the IEEE/OCP format specs and cross-checked against the simulator's confirmed dequant math.

| Cast pair (façade name) | Format | Rounding | Confidence |
|---|---|---|:--:|
| `static_cast_fp32_to_bfloat16` / `…_bfloat16_to_fp32` | BF16 = top 16 bits of FP32 | RNE (ties-to-even) | STRONG |
| `static_cast_fp32_to_float8_e4m3` / `…_e4m3_to_fp32` | FP8 E4M3 (1-4-3) | RNE, saturate-to-max | STRONG |
| `static_cast_fp32_to_float8_e5m2_x4` / `…_e5m2_to_fp32` | FP8 E5M2 (1-5-2, has Inf/NaN) | RNE | STRONG |
| `static_cast_fp32_to_float4_e2m1fn_x4` / `…_e4m2fn_x4_to_fp32`(*) | FP4 E2M1 (1-2-1) | RNE | STRONG |
| `static_cast_fp32_to_float8_e8m0fnu` / `…_e8m0fnu_to_fp32` | E8M0 (8-bit exponent, no mantissa) | — | STRONG |
| `static_cast_fp32_to_fp32r` / `…_fp32r_to_fp32` | FP32r reduced (TF32-like) | mantissa re-round | STRONG |

(*) reverse spelling carries the `e4m2` typo (see note above).

### BF16 — round-to-nearest-even

```c
// static_cast_fp32_to_bfloat16  (body external; math from BF16 spec,
//   cross-checked vs device CastToNewDType bf16-round const 0x3f000000)
uint16_t fp32_to_bf16(float f) {
    uint32_t u   = bitcast_u32(f);
    uint32_t lsb = (u >> 16) & 1;               // retained low bit
    uint32_t bias = 0x7FFF + lsb;               // 0.5 ULP, ties-to-even
    u += bias;
    return (uint16_t)(u >> 16);                 // NaN/Inf top bits preserved
}
// inverse: exact widen, no rounding
float bf16_to_fp32(uint16_t b) { return bitcast_f32((uint32_t)b << 16); }
```

### E8M0 — the block-scale exponent, not a numeric value

`float8_e8m0fnu` (Int 6) is an **8-bit exponent-only** value: no sign, no mantissa, bias 127, `0xFF` reserved for NaN. It is *not* a compute datum — it is the per-block shared exponent the OCP-MXFP matmul applies as a power-of-two scale. The host reference decode is the OCP `2^(e − 127)`:

```c
// host reference (OCP-MXFP) — INFERRED from format spec
uint8_t fp32_to_e8m0(float scale) {                 // positive scale
    int e = round_to_nearest(log2f(scale)) + 127;
    return (uint8_t)clamp(e, 0, 254);               // 0xFF reserved = NaN
}
float e8m0_to_fp32(uint8_t e) {                     // e==0xFF → NaN
    return ldexpf(1.0f, (int)e - 127);              // 2^(e-127)
}
```

> **CORRECTION (D-X05 / D-X06) — the device E8M0 "cast" is a raw byte, not a power-of-two.** The OCP `2^(e − 127)` decode above is the *host* reference. The **device** twin `bir::CastToNewDType` decodes E8M0 (source tag 6) as a raw `uint8 → float` (`movzx`, **no** scaling) — it materialises the stored exponent byte as an integer, not the `2^(e−127)` value. The two are therefore **not** bit-identical for E8M0. The actual power-of-two application happens later, in the MX dequant: the simulator's `dequantizeMx` @0x27b040 computes `out = ldexpf(data, scale[block] − 127)`, applying the shared block exponent per 32-element (8-partition × 4-column) OCP block. Treat an E8M0 "cast" as exponent **bookkeeping**, not a numeric value conversion. [DIVERGENCE CONFIRMED — device `CastToNewDType` raw-byte path vs the simulator's `ldexpf(…, e−127)` dequant.]

This is also why `promote_type._promote_floats` (below) **special-cases E8M0 out** of its normal widen ladder: it is a scale exponent, not a numeric float, so it can never be the numeric result of a float-float promotion.

### FP4 / FP8 — saturating narrow encoders

```c
// FP8 E4M3 (finite variant): saturate-to-max, RNE on 3 mantissa bits
//   max encoded 0x7E = 448.0 ; 0x7F = NaN ; overflow-without-saturate → NaN
// FP8 E5M2 (IEEE-like): has Inf/NaN, RNE on 2 mantissa bits
//   max encoded 0x7B = 57344.0 ; overflow-without-saturate → Inf
// FP4 E2M1: 4-bit, finite. Representable set:
//   {±0, ±0.5, ±1, ±1.5, ±2, ±3, ±4, ±6}. Encode = nearest (RNE),
//   then pack 4 lanes per 2-byte x4 container.
```

All narrowing FP encoders round **RNE only** (round-to-nearest-even; explicit guard+sticky, `fesetround(FE_TONEAREST)`); float→int paths use `roundss imm=0xC` (nearest, suppress-exception) + `cvttss2si` + a saturating `cmov`. There is **no** stochastic rounding anywhere in the cast surface. [STRONG — the saturate constants (E4M3 `0x7E`, E5M2 `0x7B`) and the RNE-only policy are confirmed via the device `CastToNewDType` consts; the FP4 value set is from the E2M1 spec.]

### FP32r — reduced-precision FP32

`float32r` (Int 17) is a first-class 4-byte dtype, **not** a flag on `float32`. It carries its own wire-tag `0x0b` (11) and is sized/aligned identically to `float32` (stride 4, align 4), but it holds a TF32-style truncated-mantissa fp32. The cast pair re-rounds the mantissa to the fp32r grid and widens back. It ranks **above** plain `float32` in the promotion precedence ladder (next section). The simulator's MAC special-cases it: `matmultImpl` calls `sub_2A4750` to round/truncate both operands when `ap.getType() == 17`. [STRONG — enum ordinal + stride/align identity + the sim's fp32r round-truncate call; the host cast body is external.]

---

## The promotion lattice

When a binary op has operands of two dtypes, the result type is chosen by `promote_type(t0, t1)` in `penguin/dtypes.so`. This is the implicit auto-cast policy the IR builder, the type-legalization passes, and the NKI kernel builder all import. It is a **closure factory**: its body defines four nested functions and dispatches on a `np.issubdtype(_, integer)` 2×2 truth table.

```c
// promote_type(t0, t1)  @ penguin/dtypes.so  (closure structure CONFIRMED)
dtype promote_type(dtype t0, dtype t1) {
    bool t0_int = np.issubdtype(t0, np.integer);
    bool t1_int = np.issubdtype(t1, np.integer);
    if ( t0_int &&  t1_int) return _promote_ints(t0, t1);
    if (!t0_int && !t1_int) return _promote_floats(t0, t1);
    return _promote_float_int(t0, t1);            // mixed: the FLOAT side wins
    // no path → raise: "No available implicit dtype promotion path for input
    //   dtypes {t0} and {t1}. Use .astype(dtype) explicitly."
}
```

The four sub-rules, in order of how much they reveal about the type system:

- **`_is_floating(dt)`** = `dt in custom_dtypes || np.issubdtype(dt, np.floating)`. The explicit `custom_dtypes` membership test exists because NumPy's `issubdtype(_, floating)` does **not** recognise the Neuron custom floats (bf16 / FP8 / FP4 / E8M0) — they are caught by the set first.

- **`_promote_floats(t0, t1)`**: (1) E8M0 is guarded out at the top (it is a scale format, never a widen target). (2) Wider `itemsize` wins. (3) On equal itemsize, walk a fixed precedence ladder and return the first match:
  ```text
  float64 > float32r > float32 > bfloat16 > float16
          > float8_e4m3 > float8_e4m3fn > float8_e5m2
  ```
  Note `float32r` ranks **above** plain `float32` at equal width.

- **`_promote_ints(t0, t1)`**: both-unsigned → wider uint; both-signed → wider int; mixed signed/unsigned → a widening ladder, with `uint64 × signed` escaping to `float32` (it fits no integer type).

- **`_promote_float_int(t0, t1)`** = `t0 if _is_floating(t0) else t1` — the **float side always wins**, with no widening of the float to fit the integer's range.

[CONFIRMED — the four nested-closure qualnames (`promote_type._is_floating` / `._promote_floats` / `._promote_ints` / `._promote_float_int`), the `__pyx_scope_struct__promote_type` cell struct, and the error strings `"No available implicit dtype promotion path…"` and `"Cannot merge type!"` are all present in `penguin/dtypes.so`; the dispatch and the float-precedence ladder are disasm-grounded per D-X05. The mixed-sign integer result widths are STRONG (ladder targets inferred). The full promotion lattice is the subject of Numerics §9.2 (PLANNED).]

A stricter sibling, `merge_type` / `can_merge_type`, is used by type-inference and fusion passes (PSUM accumulate, copy elimination); it widens by itemsize and signedness with `float32r` / `bfloat16` special handling and raises `ValueError("Cannot merge type!")` on an incompatible pair. The data flow is: a binary op calls `promote_type(a, b)` → result dtype `R` → operands auto-cast to `R` via `dt.static_cast` → at the BIR level this lowers to `bir::CastToNewDType`. **x4 inputs are laundered** (`launder_x4_dtype`) to their x1 element dtype before promotion, then the `_x4` tag is restored on the container result.

---

## Reimplementation checklist

To handle a Neuron dtype end-to-end:

1. **Validate the ordinal.** `0 ≤ dtype ≤ 19`; trap otherwise (every consumer enforces `cmp …,0x13; ja default`). There is no member `≥ 20`.
2. **Size it by container.** `bytes = qword_1DFC040[dtype]`. For the x4 types this is the container size (2 for FP4-x4, 4 for FP8-x4), **not** the logical lane width.
3. **Stamp the wire-tag.** `wire_tag = byte_1DFBAD0[dtype]` — a permutation, many-to-one for the FP8/FP8-x4 pairs. On pre-CoreV4 arches use `byte_1DF5760` (differs only at index 2: FP4-x4 → 5 not 16).
4. **Expand the x4 K-extent.** If `dtype == 2 || (unsigned)(dtype − 8) <= 1`, multiply `numElementsPerPartition` by 4 to get the logical contraction extent. Address by the *packed* container count; size the contraction by the *logical* (×4) count.
5. **Launder before promotion.** Strip the `_x4` tag with `launder_x4_dtype` so `promote_type` / `merge_type` reason about the x1 element type, then re-apply the tag to the result container.
6. **Treat E8M0 as a scale, not a value.** Do not run a numeric `2^(e−127)` cast in the cast primitive; the device path materialises the raw byte and applies the power-of-two later, in the MX dequant per 32-element block.

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::Dtype` enum (`libBIR.so`) | the 20-member front-end alphabet this page rosters |
| `byte_1DFBAD0` / `qword_1DFC040` (`libwalrus.so`) | the wire-tag and container-stride LUTs |
| `qword_5F74E0` (`libBIRSimulator.so`) | the simulator's byte-identical stride mirror |
| `promote_type` / `merge_type` (`penguin/dtypes.so`) | the implicit auto-cast policy keyed on this roster |
| `is_x4_dtype` / `launder_x4_dtype` (`support/dtype.so` → `neuron_dtypes`) | the x4 container predicates |
| `bir::CastToNewDType` (`libBIR.so`) | the device-side twin of the `static_cast_*` reference casts |
| `dequantizeMx` (`libBIRSimulator.so`) | applies the E8M0 block scale (`ldexpf(data, e−127)`) the cast path does not |

> **CORRECTION (cross-page numbering) —** the [BIR Dtype Tables](../bir/dtype-tables.md) (7.6) page links this catalog as "Numerics §9.3" in its abstract. The catalog is task **9.1** in Part 9, the first numerics page. The section number on the 7.6 link is stale; the slug (`numerics/dtype-catalog.md`) is correct. Noted here rather than silently editing the sibling page.

## Cross-References

- [BIR Dtype Tables](../bir/dtype-tables.md) — Part 7.6, the definitive `Dtype2string` switch body, the three backend LUTs, and the alignment predicate
- [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) — Part 2.23, the `NEURON_ISA_TPB_DTYPE` wire ordinals and the pre-v4 `byte_1DF5760` LUT
- [nki/dtype Façade](../nki/dtype-facade.md) — Part 6.3.3, the Python-facing veneer over `support.dtype` and `neuron_dtypes`
- [PE Matmul Encoding](../isa/pe-matmul-encoding.md) — Part 2.10, the dense matmul dtype field and the CoreV3 x4-rejection gate
- Numerics §9.2 (PLANNED) — the full `promote_type` / `merge_type` promotion lattice
- Numerics §9.8 (PLANNED) — the FP8 / FP4 / E8M0 cast and MX dequant numeric models
