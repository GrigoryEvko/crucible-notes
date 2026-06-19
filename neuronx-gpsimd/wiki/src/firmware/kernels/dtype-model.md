# The Unified Datatype Model

This is the **anchor page for datatypes** across the GPSIMD (Vision-Q7 *Cairo* `ncore2gp`)
kernel cluster. There is exactly **one** on-instruction datatype code space —
`NEURON_ISA_TPB_DTYPE`, a one-byte packed enum — and every kernel page that says
"`in_dtype`", "the dtype field", "FP8", "MX block scale", or "the FP32 hub" resolves its
meaning here. The page delivers five things: (1) the **complete ordinal table** of the enum,
every code `0x00..0x1F` with width / signedness / format / role, flagged per generation;
(2) the **global dtype-dispatch map** — how a code byte selects a concrete compute primitive,
including the **FP32 convert hub**, the **MX block-scale** path, the **matmul/PE** path, and
the **quantize/dequantize** paths; (3) the **CPTC** sub-byte transport encoding
(`CPTC1..7 = 0x19..0x1F`, `bit-count = code & 0x7`) and the **MX / E8M0** scale codes;
(4) the **host ↔ device ↔ NKI reconciliation** (which dtype names map across the three
layers, and where un-mappable codes die); and (5) the two **master tables** —
per-generation × per-dtype availability, and per-dtype × per-op-class support.

Everything below is re-grounded **this pass** against the shipped binaries. The enum bodies,
the validity-gate predicates, the CPTC comment, the MX descriptor, and the opcode codes are
read **byte-for-byte** from the four shipped arch-isa headers
`neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_common.h` plus
`arch-isa/tpb/aws_tonga_isa_tpb_common.h` (these enums **are** binary-derived: they ship in
the customop-lib package's `c10/include` tree, mirrored into the DWARF of
`libneuroncustomop.a`); the firmware reconciliation from the assert-string table of
`libnrtucode_internal.so`; the device dispatch shape from the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). The headers live under `extracted/`
(gitignored — reach with `fd --no-ignore` or an absolute path). Confidence tags per
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-execution, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]`
= re-used at a sibling report's confidence without re-reading the artifact this pass.

> **NOTE — the four byte-grounded generations are SUNDA / CAYMAN / MARIANA / MAVERICK
> (= NC-v2 / v3 / v4 / v5), plus the older 8-code TONGA (V1) subset.** All five enum bodies
> were read verbatim this pass. The `mariana`→`maverick` step is the MX wave; whether a
> distinct *MARIANA_PLUS* trim exists is **not** witnessed by a separate header here — the
> superset chain is `tonga ⊂ {sunda = cayman} ⊂ mariana ⊂ maverick`. Treat the v4/v4+ split
> as MARIANA/MAVERICK. `[HIGH/OBSERVED for the five headers; MED/INFERRED that no separate
> MARIANA_PLUS header ships in this package.]`

---

## 0. TL;DR — the model in six facts

1. **One code space.** `NEURON_ISA_TPB_DTYPE` is a 1-byte `NEURON_ISA_PACKED` enum and a
   **strict additive superset chain** by generation: a 16-code base (`0x0..0xF`, SUNDA ==
   CAYMAN byte-identical) → MARIANA adds `FP4_EXP2(0x10)` + `CPTC1..7(0x19..0x1F)` → MAVERICK
   adds `FP8_EXP2(0x11)` + `INT4(0x12)` + `SFP8_E8..E5(0x13..0x16)`. **Codes never change
   meaning; generations only *add*.** `[HIGH/OBSERVED — §1, all five enums read this pass.]`
2. **The same byte flows everywhere unchanged.** The custom-op `ARG_TENSOR` descriptor
   (`dtype@0x02`), the ucode instruction structs (`in_dtype`/`out_dtype`, the `DTYPE_PAIR`
   nibble field), the SDMA descriptor (`SDMA_DTYPE`), and the collective2 descriptor
   (`dtype@13`) all carry the *same* enum. The only translation that ever happens is
   ISA-code → `c10::ScalarType` at the `at::Tensor` boundary. `[HIGH/OBSERVED — §2.1, §5.]`
3. **Compute is dtype-WIDTH-keyed.** 8/16-bit → 16-bit-class native scalar / IVP vector ops;
   32-bit → 32-bit ops; 64-bit (admitted only for `INT64`/`UINT64`) → synthesised from
   32-bit halves; floats → soft-float bodies. **There is no hardware FP and no `fp64`
   anywhere on the value path.** `[HIGH/OBSERVED — §2.2 + ISS-13/CARRIED.]`
4. **FP32 is the universal convert hub.** The *only* native float-width converts are
   `fp16 ↔ fp32`. `bf16` **and** all three `fp8` formats have **no native convert op** — they
   are realised *through* the FP32 intermediate. `Cast` = `in → FP32 → out`.
   `[HIGH/OBSERVED — §2.3; exhaustive negative control CARRIED from ISS-07.]`
5. **Microscaled (MX) dtypes carry a separate block scale**, two mechanisms: a dequant-side
   **in-band block-of-8** scale applied as a vector multiply (POOL `TensorDequantize`), and
   the **out-of-band E8M0** scale tensor (`MXTensorV2.scale_dtype = SFP8_E8`). The
   `SFP8_E8..E5` codes are **scale-only** — never a compute element. `[HIGH/OBSERVED — §2.4.]`
6. **Roles partition cleanly.** *Compute* dtypes (the int/fp set the ALU/PE work on),
   *transport-only* (`FP4`/`INT4` and the `CPTC1..7` trellis widths ride sub-byte / packed
   fields), *scale-only* (`SFP8_E8..E5`), and the *output-only* `FP32R` (a rounded "FP22"
   partial fp32). `[HIGH/OBSERVED — §2.7 + the header gate predicates §1c.]`

---

## 1. The complete `NEURON_ISA_TPB_DTYPE` enum

Storage: **1 byte**, `NEURON_ISA_PACKED`. The DWARF in `libneuroncustomop.a` confirms
`byte_size = 0x01`. The codes are **not** contiguous and **not** in declaration order — each
is an explicit `= 0xN`. Codes `≤ 0xF` are **nibble-packable** (they fit a `DTYPE_PAIR`'s
4-bit field); codes `0x10..0x1F` (`FP4`/`FP8_EXP2`/`INT4`/`SFP8`/`CPTC`) are **not**
nibble-packable and live only in full-byte dtype fields. `[HIGH/OBSERVED — DWARF byte_size +
the `DTYPE_PAIR` bitfield read §1b.]`

The MAVERICK body (the 30-code superset) read byte-for-byte this pass (header lines
850–879, comments preserved):

| Code | Name | SU | CA | MA | MV | Width | Sign | Format / semantics | Role |
|---|---|:-:|:-:|:-:|:-:|---|:-:|---|---|
| `0x00` | `INVALID`  | ● | ● | ● | ● | — | — | sentinel ("used in RTL for bitvec") | n/a |
| `0x01` | `UINT64`   | ● | ● | ● | ● | 8 B | u | unsigned 64-bit integer | compute † |
| `0x02` | `INT8`     | ● | ● | ● | ● | 1 B | s | signed 8-bit integer | compute |
| `0x03` | `UINT8`    | ● | ● | ● | ● | 1 B | u | unsigned 8-bit integer | compute |
| `0x04` | `INT16`    | ● | ● | ● | ● | 2 B | s | signed 16-bit integer | compute |
| `0x05` | `UINT16`   | ● | ● | ● | ● | 2 B | u | unsigned 16-bit integer | compute |
| `0x06` | `BFLOAT16` | ● | ● | ● | ● | 2 B | s | bf16 `1-8-7`; via FP32 hub | compute |
| `0x07` | `FP16`     | ● | ● | ● | ● | 2 B | s | IEEE binary16 `1-5-10`; **native** cvt | compute |
| `0x08` | `INT32`    | ● | ● | ● | ● | 4 B | s | signed 32-bit integer | compute |
| `0x09` | `UINT32`   | ● | ● | ● | ● | 4 B | u | unsigned 32-bit integer | compute / transport |
| `0x0A` | `FP32`     | ● | ● | ● | ● | 4 B | s | IEEE binary32 `1-8-23`; **native** cvt | compute |
| `0x0B` | `FP32R`    | ● | ● | ● | ● | 4 B | s | "FP22" rounded / partial fp32 | **output-only** |
| `0x0C` | `INT64`    | ● | ● | ● | ● | 8 B | s | signed 64-bit integer | compute † |
| `0x0D` | `FP8_EXP3` | ● | ● | ● | ● | 1 B | s | FP8 **E3M4** `1-3-4`; via FP32 hub | compute |
| `0x0E` | `FP8_EXP4` | ● | ● | ● | ● | 1 B | s | FP8 **E4M3** `1-4-3`; via FP32 hub | compute |
| `0x0F` | `FP8_EXP5` | ● | ● | ● | ● | 1 B | s | FP8 **E5M2** `1-5-2`; via FP32 hub | compute |
| `0x10` | `FP4_EXP2` | – | – | ● | ● | 4-bit | s | OCP **FP4_E2M1** `1-2-1` | MX element / transport |
| `0x11` | `FP8_EXP2` | – | – | – | ● | 1 B | s | FP8 **E2M5** `1-2-5` | MX element / compute |
| `0x12` | `INT4`     | – | – | – | ● | 4-bit | s | signed 4-bit integer | MX element / transport |
| `0x13` | `SFP8_E8`  | – | – | – | ● | 1 B | u (S0) | **FP8_S0E8M0** (E8M0, no sign) | **scale-only** (MX block scale) |
| `0x14` | `SFP8_E7`  | – | – | – | ● | 1 B | u (S0) | FP8_S0E7M1 | scale-only |
| `0x15` | `SFP8_E6`  | – | – | – | ● | 1 B | u (S0) | FP8_S0E6M2 | scale-only |
| `0x16` | `SFP8_E5`  | – | – | – | ● | 1 B | u (S0) | FP8_S0E5M3 | scale-only |
| `0x19` | `CPTC1`    | – | – | ● | ● | 1-bit | — | Computed Permutation Trellis Coding | transport |
| `0x1A` | `CPTC2`    | – | – | ● | ● | 2-bit | — | (`code & 0x7` = bit count) | transport |
| `0x1B` | `CPTC3`    | – | – | ● | ● | 3-bit | — |  | transport |
| `0x1C` | `CPTC4`    | – | – | ● | ● | 4-bit | — | `is_cptc`: `(code & 0xF8)==0x18` | transport |
| `0x1D` | `CPTC5`    | – | – | ● | ● | 5-bit | — | `&& (code & 0x7)!=0` | transport |
| `0x1E` | `CPTC6`    | – | – | ● | ● | 6-bit | — |  | transport |
| `0x1F` | `CPTC7`    | – | – | ● | ● | 7-bit | — | (`0x1F` is the reserved sentinel anchor) | transport |

`[HIGH/OBSERVED — every row read byte-for-byte from the maverick header this pass; the SU/CA/MA
columns are the per-gen presence rg-counted §1a. `●` = present, `–` = absent in that gen's enum.]`

**Per-gen enumerator counts (rg-counted this pass): SUNDA 16, CAYMAN 16, MARIANA 24,
MAVERICK 30.** Exact command:
`rg 'NEURON_ISA_TPB_DTYPE_[A-Z0-9_]+\s*=\s*0x' FILE | rg -v 'BASIC|_PAIR|ALLOW|SDMA|MXTENSOR'
| wc -l` → 16 / 16 / 24 / 30. `[HIGH/OBSERVED.]`

> **GOTCHA — the `FP8_EXPn` suffix is the EXPONENT width, and it is *not* monotone with the
> code.** `FP8_EXP3 = E3M4`, `FP8_EXP4 = E4M3`, `FP8_EXP5 = E5M2`, and `FP8_EXP2 = E2M5`. The
> familiar deep-learning FP8 formats **e4m3** and **e5m2** are `FP8_EXP4(0xE)` and
> `FP8_EXP5(0xF)` — **not** `FP8_EXP3`. Read the suffix as "exponent-bit count", then the
> mantissa is `7 − exp`. `[HIGH/OBSERVED — the header comments + the `DEQUANT_FMT` comments
> name `FP8_E5M2`/`FP8_E4M3` against these codes §2.6.]`

> **NOTE — `FP32R(0xB)` is a *rounded* output form of fp32, not a distinct width.** The header
> comment on `FP32(0xA)` states verbatim: *"RTL will used 0xB for FP22 partial fp32 type"*.
> `FP32R` is 4-byte storage, maps to `Float` on the host, and is **output-only** — every gate
> admits it only on `out_dtype` under an explicit `ALLOW_FP32R` permission (§1c, §5).
> `[HIGH/OBSERVED.]`

† **`INT64`/`UINT64` compute is *gated*.** The `ncore2gp` has no native 64-bit ALU; 64-bit is
synthesised from 32-bit halves and is admitted **only** where the op passes the
`is_valid_dtype_64` gate with the matching `AllowU64`/`AllowI64` permission `True` (§1c). The
plain 32-bit ALU gate (`is_valid_dtype`) **excludes** both. `[HIGH/OBSERVED — gate bodies §1c.]`

### 1a. The TONGA (V1) subset

The oldest generation ships a **distinct name family** `TONGA_ISA_TPB_DTYPE_*` — an **8-code
strict subset** read verbatim this pass from `aws_tonga_isa_tpb_common.h`:

| Code | TONGA name | Header role comment |
|---|---|---|
| `0x0` | `INVALID` | — |
| `0x3` | `UINT8`   | "FMAPs and Weights for UINT8 networks" |
| `0x5` | `UINT16`  | "FMAPs and Weights for UINT16 networks" |
| `0x6` | `BFLOAT16`| "FMAP and Weights for BFLOAT16 networks" |
| `0x7` | `FP16`    | "FMAP and Weights for FP16 networks" |
| `0x8` | `INT32`   | "MatMul results (aka 'partial sums') for UINT8 networks" |
| `0xA` | `FP32`    | "MatMul results … for FP16/BFLOAT16 networks" |
| `0xC` | `INT64`   | "MatMul results … for UINT16 networks" |

TONGA has **no** FP8, **no** `INT8`/`INT16`, **no** `UINT32`/`UINT64`, **no** `FP32R`, and
none of the MX/FP4/CPTC codes. The codes it lacks (`0x1,0x2,0x4,0x9,0xB,0xD,0xE,0xF`) are
exactly the CAYMAN-base additions. The codes it **does** carry use the *same* ordinals as the
modern enum — the superset chain is ordinal-preserving back to V1. `[HIGH/OBSERVED — the TONGA
enum body read this pass.]`

### 1b. Companion / alias / packing types

* **`NEURON_ISA_TPB_DTYPE_BASIC`** — a parallel alias family of the **16 base codes**
  (`BASIC_INVALID..BASIC_INT64`), present in **MARIANA and MAVERICK only** (absent from
  SUNDA/CAYMAN). It *types* the 4-bit nibble fields of `DTYPE_PAIR` (the `≤0xF` subset that
  fits a nibble), keeping the full `NEURON_ISA_TPB_DTYPE` byte free to carry `0x10..0x1F`.
  `is_valid_dtype_for_sdma` requires `is_valid_enum(EnumList::DtypeBasic, dtype)` — i.e. only
  the BASIC subset is SDMA-marshallable. `[HIGH/OBSERVED — `is_valid_dtype_for_sdma` body +
  the per-gen presence read this pass.]`

  > **CORRECTION — the BASIC alias has 16 enumerators, not 20.** An earlier synthesis recorded
  > "20 enumerators". Re-grounded this pass with
  > `rg 'NEURON_ISA_TPB_DTYPE_BASIC_[A-Z0-9_]+' FILE -o | sort -u | wc -l` → **16** for both
  > MARIANA and MAVERICK (one BASIC alias per base code `0x0..0xF`). `[HIGH/OBSERVED.]`

* **`DTYPE_PAIR`** (`{ dtype_lo : 4; dtype_hi : 4; }`, 1 byte) — two 4-bit dtype nibbles, used
  where an op carries a src0/src1 dtype pair (Tensor-Tensor). It can address **only** the
  `≤0xF` (BASIC) codes — the extended dtypes cannot ride a pair. `[HIGH/OBSERVED — bitfield
  read this pass.]`

* **`DTYPE_ALLOW_FP32R` / `_ALLOW_U64` / `_ALLOW_I64`** (`{ FALSE = 0, TRUE = 1 }`) — per-op
  dtype-permission gates; `FP32R`, `UINT64`, `INT64` are "conditional" dtypes admitted only
  when the op sets the matching flag. (MAVERICK additionally ships `DtypeAllowFP4` and
  `DtypeAllowScale` — see §1c.) `[HIGH/OBSERVED — all enum bodies read this pass.]`

* **`NEURON_ISA_TPB_MXTENSOR_V2`** (MARIANA+/MAVERICK) — the Microscaled-Tensor descriptor,
  read verbatim this pass:
  `{ NEURON_ISA_TPB_ADDR4 data_addr; NEURON_ISA_TPB_ADDR4 scale_addr; uint8_t num_elem[2];
  int16_t step_elem_data_1; int16_t step_elem_scale_1; uint8_t p_f_dim; NEURON_ISA_TPB_DTYPE
  scale_dtype; }`. `scale_dtype` is a `NEURON_ISA_TPB_DTYPE` (`0` = no scales); `p_f_dim`
  packs the F (upper nibble) and P (lower nibble) block dims as 2's exponents. `[HIGH/OBSERVED.]`

### 1c. The validity-gate predicates — the dtype *legality* algebra

The arch-isa header ships the gate predicates as **Rust pseudocode comments** (the
specification the host ucode decoder and the device firmware both implement). These **are**
binary-derived (they ship in the package) and are the authoritative legality source. Read
verbatim this pass:

```rust
// the universal scalar gate (Cast/Copy, ALU src/dst, …) — REJECTS 64-bit, 4-bit, scale, fp32r-unless-allowed
fn is_valid_dtype(dtype: Dtype, allow_fp32r: DtypeAllowFP32R) -> bool {
       dtype_invalid_check(dtype)                                   // dtype != INVALID
    && dtype_fp32r_illegal_check(dtype, allow_fp32r)                // FP32R only if allow_fp32r
    && dtype_uint64_illegal_check(dtype, DtypeAllowU64::False)      // UINT64 rejected
    && dtype_int64_illegal_check(dtype, DtypeAllowI64::False)       // INT64  rejected
    && dtype_4bit_illegal_check(dtype, DtypeAllowFP4::False)        // FP4_EXP2, INT4 rejected
    && dtype_scale_illegal_check(dtype, DtypeAllowScale::False)     // SFP8_E5..E8 rejected
    && is_valid_enum(EnumList::Dtype, dtype)
}

// the 64-bit-capable gate (TensorReduce) — parameterised on every permission
fn is_valid_dtype_64(dtype, allow_fp32r, allow_u64, allow_i64) -> bool {
       dtype_invalid_check(dtype)
    && dtype_fp32r_illegal_check(dtype, allow_fp32r)
    && dtype_uint64_illegal_check(dtype, allow_u64)   // UINT64 admitted iff allow_u64 == True
    && dtype_int64_illegal_check(dtype, allow_i64)    // INT64  admitted iff allow_i64 == True
    && is_valid_enum(EnumList::Dtype, dtype)
}

// the datapath category predicates (the op-class buckets)
fn is_valid_int_dtype(dtype)          { INT8|INT16|INT32|INT64 | UINT8|UINT16|UINT32|UINT64 }   // includes 64-bit
fn is_valid_int_dtype_datapath(dtype) { INT8|INT16|INT32       | UINT8|UINT16|UINT32 }          // 32-bit-max
fn is_valid_fp_dtype_datapath(dtype, allow_fp32r) {
       FP8_EXP3|FP8_EXP4|FP8_EXP5 | FP16 | BFLOAT16 | FP32 | (allow_fp32r && FP32R) }
fn is_valid_mx_dtype(dtype)           { FP8_EXP2|FP8_EXP3|FP8_EXP4|FP8_EXP5 | FP4_EXP2 | INT4 }  // the MX element set
fn is_signed_int(dtype)               { INT8|INT16|INT32|INT64 }
fn is_unsigned_int(dtype)             { UINT8|UINT16|UINT32|UINT64 }
fn is_valid_32b_int_dtype(dtype)      { INT32|UINT32 }
fn is_valid_64b_int_dtype(dtype)      { INT64|UINT64 }
fn is_valid_dtype_for_sdma(dtype, allow_fp32r) { is_valid_dtype(dtype, allow_fp32r) && is_valid_enum(DtypeBasic, dtype) }
```

`[HIGH/OBSERVED — all predicate bodies read verbatim this pass; 48 such `fn is_valid_*` /
`fn dtype_*_check` comment-functions live in the maverick header.]`

> **GOTCHA — the universal `is_valid_dtype` gate rejects **more** than just INVALID/64-bit.**
> On MAVERICK it *also* rejects the 4-bit (`FP4_EXP2`, `INT4`) and scale (`SFP8_E5..E8`)
> dtypes via `dtype_4bit_illegal_check(…False)` and `dtype_scale_illegal_check(…False)`. So a
> Cast/Copy/ALU op can never name a 4-bit or scale dtype directly — those reach the datapath
> only through the dedicated MX/dequant descriptors (§2.4, §2.6). An earlier synthesis listed
> only the invalid/fp32r/u64/i64 checks; the 4-bit and scale checks are the MAVERICK
> additions. `[HIGH/OBSERVED — the `is_valid_dtype` body read this pass.]`

---

## 2. The global dtype-dispatch map

The unified pattern: a kernel's operand struct carries one or two dtype bytes; the decoder
**gates** them (the `is_valid_*` family §1c) *before* compute, then dispatches on
`(op, dtype-WIDTH, signedness)` to a concrete native primitive. The dtype byte is carried
**verbatim** into any DMA / collective descriptor — no re-encode. `[HIGH/OBSERVED structure;
MED on the exact per-cell on-device arm — see the FLIX wall §7.]`

### 2.1 Where the dtype byte lives, per kernel

| Kernel / struct | dtype field(s) | provenance |
|---|---|---|
| Cast / Copy (`S4D4_TR`) | `in_dtype@32`, `out_dtype@33` (two full bytes) | CARRIED (FW-72 DWARF) |
| TensorReduce (`S4D4_TR`) | `in_dtype@32`, `out_dtype@33` (same struct) | CARRIED |
| Tensor-Tensor (`S3S3D3`) | `in0_in1_dtype@12` (`DTYPE_PAIR` 4+4), `out@13` | CARRIED (FW-49) |
| Tensor-Scalar (`S3D3_TS`) | `in_dtype@32`, `out_dtype@33` + `reverse_operands` | CARRIED (FW-50) |
| TensorDequantize (`S3D3`) | `in_dtype`/`out_dtype` == `UINT32` (transport); logical fmt in `dequant_fmt` | CARRIED (FW-63/75) |
| Matmul (`S3D3_MM`) | `in_dtype`, `out_dtype` (+ `fp32_mode` for tf32) | CARRIED (FW-66) |
| LdWeights (`S3_LW`) | `in_dtype` (weight dtype) | CARRIED (FW-66) |
| QuantizeMX | `MXTensorV2.scale_dtype` (`SFP8_E8..E5`) | OBSERVED (MX desc §1b) |
| `ARG_TENSOR` (custom-op) | `dtype@0x02` (1 byte) | CARRIED (ABI-06 DWARF) |
| Collective2 descriptor | `dtype@13` (1 byte) | CARRIED (CCL-02) |
| SDMA descriptor | `SDMA_DTYPE` (= `NEURON_ISA_TPB_DTYPE` codes) | CARRIED (ABI-06) |

One dtype byte (or a 4-bit nibble inside a `DTYPE_PAIR`) per operand slot — the **same** enum
everywhere. The struct **field offsets** are `[…/CARRIED]` from the DWARF reads of the sibling
ABI/FW reports; the dtype *type* of each field is the enum read here. `[HIGH/OBSERVED for the
enum; CARRIED for the offsets.]`

### 2.2 The dispatch mechanism — `(op, width, sign)` → native primitive

* **Scalar path** (the shared ALU evaluator + per-kernel decoders). The dtype select is a
  **chain of `beqi`/`bnei` immediate compares against the enum codes** — *not* an indexed jump
  table (no dtype-keyed `jx`/`l32r`+`callx` was found on device this pass). The compares match
  the enum ordinals exactly; observed in the carved firmware `.text` (VMA base `0x1000000`):
  `bnei a7, 7` (FP16=0x7) at `0x1000c94`, `beqi a3, 10` (FP32=0xA) at `0x10008b6`, an
  `INT8(2)/UINT8(3)/INT64(12)` cluster at `0x1002f0c`, `beqi a0, 8` (INT32) at `0x10013cc`,
  `beqi a14, 4` (INT16) at `0x100036d`, `beqi a3, 6` (BFLOAT16) at `0x100554d`. Each arm
  resolves to one native Xtensa instruction at the dtype **width**
  (`sll`/`srl`/`sra`/`add.n`/`sub`/`mull`/`quos`/`quou`/`and`/`or`/`xor`/`salt`/`saltu`).
  **Signedness is a runtime branch** on the dtype-signedness flag (`bbci → salt`/`blt` signed
  vs `saltu`/`bltu` unsigned), so one leaf serves both signednesses. A **separate** 64-bit
  switch (`bnei a*,1` UINT64 / `bnei a*,12` INT64 → else "not supported dtype") gates the
  synthesised 64-bit handlers (the `tensor_tensor_64bit_dispatch<VectorInt64/VectorUint64>` and
  `setup_64bit_rw` kernels, demangled from the firmware `.xt.prop` name table). `[HIGH/OBSERVED
  — the compare-chain addresses disassembled on device this pass with the native
  `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the per-arm leaf identity MED under FLIX
  desync §7.]`

* **Vector path** (DVE / POOL). The same `(ALU_OP, dtype)` selects an IVP intrinsic whose
  **lane geometry encodes the width** (`2nx8` = 64×i8, `nx16` = 32×i16, `n_2x32` = 16×i32,
  `nxf16`/`n_2xf32` float) and whose **signed/unsigned variant is a distinct opcode**
  (`ivp_bmaxnx16` signed vs `ivp_bmaxunx16` unsigned). Saturation is *opcode-encoded*
  (`ivp_addsnx16` saturating vs `ivp_addn` wrap), not a runtime mode. `[HIGH/OBSERVED —
  CARRIED from FW-79.]`

* **The category buckets** (§1c, header-authoritative). The op-class gate partitions by
  category before dispatch: `is_valid_int_dtype` (INT/UINT 8/16/32/64),
  `is_valid_int_dtype_datapath` (the 32-bit-max int datapath), `is_valid_fp_dtype_datapath`
  (`FP8_E3/E4/E5`, `FP16`, `BF16`, `FP32`, `[FP32R]`). `[HIGH/OBSERVED — bodies read §1c.]`

### 2.3 The FP32 convert hub

The single most consequential dtype-flow fact:

* The **only native float-width converts are `fp16 ↔ fp32`** (a widen `fp16 → fp32` and a
  narrow `fp32 → fp16`).
* **`bf16` and all three `fp8` formats (e3m4 / e4m3 / e5m2) have NO native convert op.** The
  device soft-float library is **binary16 + binary32 only**; a negative-control sweep this pass
  finds **zero** bf16/fp8/e4m3/e5m2 convert primitives. Two independent witnesses, both grounded
  this pass: (a) the `ncore2gp` ISA header `xt_ivp32.h` —
  `rg -ic 'bf16|bfloat|fp8|e4m3|e5m2|float8|vecNxf8' xt_ivp32.h` → **0** (the only native vector
  float types are `f16` and `f32`; the native converts are `IVP_CVTF16F32`/`IVP_CVTF32F16`/
  `IVP_CVTF16N_2XF32`/`IVP_CVTF32NXF16`); (b) the device symbol tables (via
  `XTENSA_CORE=ncore2gp xtensa-elf-nm`) —
  `rg -ic 'cvtbf16|bf16cvt|cvtfp8|fp8cvt|cvte4m3|cvte5m2' syms` → **0** (the only `BFloat16`
  hits are C++ container template instantiations, not converts; the only fp16↔fp32 *software*
  symbol is `c10::detail::fp16_ieee_to_fp32_value`). `[HIGH/OBSERVED — both witnesses grounded
  on device this pass; CARRIED corroboration from ISS-07's earlier exhaustive sweep.]`
* **Therefore every conversion that touches `bf16` or `fp8` routes *through* FP32.** `Cast`
  is the canonical consumer:

```c
// Cast(in_dtype, out_dtype): a general any -> any convert, FP32 as the universal hub.
// There is NO per-pair (in,out) converter table; the pivot is always fp32.
fp32_t pivot = widen_to_fp32(src, in_dtype);    // fp16: native widen; bf16/fp8: unpack+scale into fp32 lanes
dst          = narrow_from_fp32(pivot, out_dtype);// fp16: native narrow; bf16/fp8: pack/round out of fp32
// functional model == numpy astype: RNE for float targets, round-toward-zero for int targets.
```

The int ↔ fp legs use the IVP `float`/`ufloat` (int → fp) and `trunc`/`utrunc` (fp → int)
intrinsics; float narrowing **packs-saturate**. Rounding is FSR-driven, **RNE by default**;
`fp → int` truncation **ignores** the rounding mode (always round-toward-zero); the
`fp16 → fp32` widen is lossless. `[HIGH/OBSERVED — the FP32-hub fact; MED/CARRIED for the
intrinsic roster from ISS-07/FW-72.]`

> **QUIRK — there is no `bf16 ↔ fp16` short-cut and no per-pair convert matrix.** Because the
> only native float converts are `fp16 ↔ fp32`, even `bf16 → fp16` is two hops
> (`bf16 → fp32 → fp16`). A reimplementer should build **one** widen-to-fp32 and **one**
> narrow-from-fp32 per format and compose, not an N×N table. `[HIGH/INFERRED — forced by the
> zero-native-bf16/fp8-convert observation.]`

### 2.4 The MX block-scale path

Microscaled dtypes carry a separate block scale, by **two distinct mechanisms**:

**(A) Dequant-side, in-band (NC-v3+; POOL `TensorDequantize`).** The block is **8 elements**
(`group_size == 8`); the per-block scale rides **in-band** within each group of 8 and is
applied as a **vector multiply**. Inputs are 4-bit nibble-packed; the `in_dtype`/`out_dtype`
struct fields are `UINT32` **transport** — the *logical* micro-format is named by `dequant_fmt`
(§2.6), not the dtype field. Output is FP8 (`E5M2`/`E4M3`). `[HIGH/OBSERVED — CARRIED from
FW-75; `DEQUANT_FMT` enum re-read §2.6.]`

**(B) Out-of-band E8M0 (NC-v5; `QuantizeMX`, `MatmulMX`/`LdweightsMX`).** The scale is an
**out-of-band tensor** (`MXTensorV2.scale_addr`, separate from `data_addr`), with
`scale_dtype = SFP8_E8 (0x13) = FP8_S0E8M0` — the OCP-MX power-of-two block scale (no sign
bit). `p_f_dim` carries the power-of-two block dims. The **MX element** set (`is_valid_mx_dtype`,
read verbatim §1c) is exactly `{ FP8_EXP2(0x11), FP8_EXP3(0xD), FP8_EXP4(0xE), FP8_EXP5(0xF),
FP4_EXP2(0x10), INT4(0x12) }`. `[HIGH/OBSERVED — the MX descriptor §1b + `is_valid_mx_dtype`
§1c + `QUANTIZE_MX = 0xe3`, `MATMUL_MX = 0x0A`, `LDWEIGHTS_MX = 0x09` opcodes read this pass.]`

> **NOTE — `SFP8_E8..E5` are scale-only.** They appear **only** as `scale_dtype`, never as a
> compute element, and are rejected by `is_valid_dtype` via `dtype_scale_illegal_check`.
> `SFP8_E8` (E8M0) is the OCP-MX shared block scale; the lower variants (`E7M1`/`E6M2`/`E5M3`)
> trade scale exponent range for mantissa. `[HIGH/OBSERVED — gate + enum comments.]`

The two MX surfaces are **chronologically distinct** — the POOL in-band dequant (NC-v3+,
predating the MX *enum codes*) and the NC-v5 `MXTensorV2` out-of-band wave. Do not conflate
them. See [the MX dequant page](mx-dequant.md) and [the tensor-dequantize page](tensor-dequantize.md).

### 2.5 The matmul / PE path

`nc_matmul` inputs = `{ fp8_e4m3, fp8_e5m2, bf16, fp16, tf32, fp32 }`; internal accumulation
is **FP32** (the PSUM banks); output is FP32 (gen2/gen3) or FP32/BF16 (gen4+). The FP8
"double-row" perf mode is the PAIR primitive (`a1*b1 + a2*b2`, two multiplies/cycle).
`[HIGH/OBSERVED structure — CARRIED from FW-66; see the matmul page below.]`

> **GOTCHA — `tf32` is NOT a dtype ordinal.** It is the reduced-mantissa fp32 **input mode** —
> the `S3D3_MM.fp32_mode` bit (TF32-vs-FP32 select). Confirmed this pass:
> `rg -c 'TPB_DTYPE_TF32|TPB_DTYPE_TFLOAT' maverick.h` → **0 hits**. Rule: if either input is
> tf32/fp32, both must be tf32/fp32. `[HIGH/OBSERVED.]`

The PSUM accumulate mode (read verbatim this pass) is
`MATMUL_PSUM_ACCUMULATE_MODE { MULTI_MID = 0, MULTI_START = 1, MULTI_END = 2, SINGLE = 3 }`
(`static const uint32_t`). The MX matmul (`MatmulMX 0x0A`/`LdweightsMX 0x09`) carries the
FP4/MX block-scale via `MXTensorV2` — MARIANA+. `[HIGH/OBSERVED.]`
See [the PE matmul page](pe-matmul.md).

### 2.6 The quantize / dequantize paths

The `DEQUANT_FMT` enum (read verbatim this pass) is the sub-byte micro-format selector — it,
**not** `NEURON_ISA_TPB_DTYPE`, is what `TensorDequantize` switches on:

| `DEQUANT_FMT` | value | meaning (header comment) |
|---|:-:|---|
| `INVALID`       | 0 | — |
| `E2M1TO_E5M2`   | 1 | FP4_E2M1 → FP8_E5M2 |
| `INT4TO_E5M2`   | 2 | INT4 → FP8_E5M2 |
| `NF4TO_E4M3`    | 3 | NF4 → FP8_E4M3 (16-entry codebook) |
| `E2M3TO_E4M3`   | 4 | FP6_E2M3 → FP8_E4M3 |

* **`TensorDequantize`** (POOL, NC-v3+): `UINT32`-transport + `dequant_fmt` selects the
  sub-byte input micro-format → FP8 output. The kernel does **not** switch on the dtype enum.
  Three paths: grp-8 MX-scaled, grp-0 non-MX 2:1, grp-0 6-bit non-MX (FP6/E2M3 — **FP6 can
  never be MX**). `[HIGH/OBSERVED — `DEQUANT_FMT` body read this pass; the path triage CARRIED
  from FW-63/75.]`
* **`QuantizeMX`** (`0xe3`, DVE, NC-v5): the **forward** data → MX direction, out-of-band E8M0
  scale (§2.4 B). `[HIGH/OBSERVED — opcode read this pass.]`

> **NOTE — `NF4` and `FP6_E2M3` are `dequant_fmt` micro-formats, *not* `NEURON_ISA_TPB_DTYPE`
> ordinals.** There is no `0xNN` enum code for them — they exist only as `DEQUANT_FMT` values
> consumed by the dequant expansion. `NF4` is a 16-entry codebook; `FP6_E2M3` is the
> `E2M3TO_E4M3` format. `[HIGH/OBSERVED.]`

### 2.7 The role partition

| Role | dtypes | gate / evidence |
|---|---|---|
| **Compute** | INT/UINT 8/16/32 (native); INT64/UINT64 (gated, 32×2 synth); FP16/FP32 (native + soft-float); BF16/FP8_E3/E4/E5 (via FP32 hub); FP8_E2M5 (MX) | `is_valid_int_dtype_datapath`, `is_valid_fp_dtype_datapath`, `is_valid_dtype_64` |
| **Output-only** | `FP32R(0xB)` — never a source; `ALLOW_FP32R` only on `out_dtype` | `dtype_fp32r_illegal_check` |
| **Transport** | `UINT32` (dequant transport); `FP4_EXP2`/`INT4` (sub-byte packed); `CPTC1..7` (trellis, `code & 0x7` bits) | `dtype_4bit_illegal_check`, the CPTC identity §3 |
| **Scale-only** | `SFP8_E8`(E8M0)/`E7`/`E6`/`E5` — only as `scale_dtype` | `dtype_scale_illegal_check` |

`[HIGH/OBSERVED — every role keyed to a header gate read this pass.]`

---

## 3. The CPTC encoding (`0x19..0x1F`)

`CPTC1..7` are **Computed Permutation Trellis Coding** transport widths — sub-byte
compressed-tensor codes that ride `32-bit-aligned` storage. The header comment (read verbatim
this pass, MARIANA and MAVERICK identical) is the normative spec:

> *"32-bit aligned Computed Permutation Trellis Coding format space. Dtype & 0x7 gives the bit
> count of the dtype. Reserved 0x1F so that `(dtype & 0xF8) == 0x18 && (dtype & 0x7) != 0`
> check always works to determine if a dtype is a CPTC dtype."*

The two structural facts, **verified byte-exact this pass** (python over the literal codes):

```text
bit-count:  code & 0x7  ==  N   for CPTCn
  CPTC1=0x19 -> 0x19 & 7 = 1     CPTC5=0x1D -> 0x1D & 7 = 5
  CPTC2=0x1A -> 0x1A & 7 = 2     CPTC6=0x1E -> 0x1E & 7 = 6
  CPTC3=0x1B -> 0x1B & 7 = 3     CPTC7=0x1F -> 0x1F & 7 = 7
  CPTC4=0x1C -> 0x1C & 7 = 4

is_cptc(code) := (code & 0xF8) == 0x18  &&  (code & 0x7) != 0
  -> True  for exactly 0x19,0x1A,0x1B,0x1C,0x1D,0x1E,0x1F
  -> False for 0x18 (the reserved "CPTC0" sentinel: code & 0x7 == 0)
  -> False for every non-CPTC code (0x10,0x11,0x12,0x13,0x16,0x0A,0x20, …)
```

`[HIGH/OBSERVED — the `code & 0x7` relation and the `is_cptc` identity hold for all seven
codes, the `0x18` sentinel exclusion, and a negative-control sweep; matches the header comment
exactly.]`

> **GOTCHA — `0x18` is the reserved "CPTC0" anchor and is *deliberately not* a CPTC dtype.**
> The designers reserved `0x1F` (CPTC7, the max bit-count) and aligned the whole family to the
> `0x18` base **precisely so** the single mask test `(code & 0xF8) == 0x18 && (code & 0x7) != 0`
> classifies a byte as CPTC with no table lookup. A decoder can branch CPTC-vs-not in two
> instructions. `[HIGH/OBSERVED.]`

> **NOTE — only the CPTC *classification* + bit-width is recovered; the trellis *decode
> algorithm* is a wall.** How a CPTC code stream is unpacked (the "computed permutation
> trellis"), and which compute consumer reads it, are **not** recovered from this corpus —
> CPTC is transport/storage-only here. `[LOW — open wall; see the CPTC codec page below.]`

For the broader CPTC codec family (the `cptc_decode_impl<1..6>` decoders), see
[the CPTC codec page](cptc-codec.md).

---

## 4. The MX / E8M0 scale codes

The microscaling scale representation is the `SFP8_*` family — **scale-only**, no sign bit:

| Code | Name | Format | OCP role |
|---|---|---|---|
| `0x13` | `SFP8_E8` | `FP8_S0E8M0` | the OCP-MX **E8M0** power-of-two block scale (the shared scale) |
| `0x14` | `SFP8_E7` | `FP8_S0E7M1` | scale (1 mantissa bit) |
| `0x15` | `SFP8_E6` | `FP8_S0E6M2` | scale (2 mantissa bits) |
| `0x16` | `SFP8_E5` | `FP8_S0E5M3` | scale (3 mantissa bits) |

The `S0` prefix denotes **zero sign bits** — these are unsigned magnitude-only scales. The
canonical MX configuration uses `SFP8_E8` (E8M0): the scale is purely a power-of-two exponent,
applied to a block of MX elements. The scale is carried out-of-band via
`MXTensorV2.scale_dtype` (`0` = no scales), and the elements it scales are the
`is_valid_mx_dtype` set (`FP8_EXP2/3/4/5`, `FP4_EXP2`, `INT4`). `[HIGH/OBSERVED — the `SFP8_*`
enum comments + the MX descriptor + `is_valid_mx_dtype` read this pass.]`

> **NOTE — the dequant-side in-band block-of-8 scale's exact bit-format is not pinned.**
> Whether the in-band POOL-dequant scale (§2.4 A) is itself E8M0 is **not** byte-proven — the
> dequant struct names no `scale_dtype` (the scale is in-band). The E8M0 fact is OBSERVED only
> for the *separate* NC-v5 `MXTensorV2.scale_dtype`. `[MED/INFERRED.]`

---

## 5. Host ↔ device ↔ NKI reconciliation

Three name spaces meet at the custom-op boundary:

* **`c10::ScalarType`** (host C++, `enum class : int8_t`, ordinals `0..19`, frozen order).
  **This package is a PRE-FP8 PyTorch** — there is **no** `Float8_*` / `Float4` /
  `UInt16/32/64` ScalarType (`grep Float8 ScalarType.h` → 0 hits). The enum ends at
  `QUInt2x4 = 17`, `Undefined = 18`, `NumOptions = 19`. `[HIGH/OBSERVED — ABI-06 double-sourced
  header + DWARF / CARRIED.]`
* **`NEURON_ISA_TPB_DTYPE`** (device, §1) — the on-instruction / ucode / SDMA / collective code
  space.
* **NKI / MLIR dtype names** (compiler front end) — the spelled-out `float8_e4m3` /
  `float8_e5m2` / `bfloat16` / `float16` / `tfloat32` / `float32` (the matmul validator) and
  the `tensor_copy` MLIR op.

The bridge is **`isa_to_torch_dtype(NEURON_ISA_TPB_DTYPE) → c10::ScalarType`** — a file-local
`static`, inlined at ≥4 sites; **one-directional** (no `torch_to_isa_dtype` symbol exists; the
reverse is implicit — the output descriptor already carries its ISA dtype, and the gate
*validates* ISA → torch). `[HIGH/OBSERVED signature; the arm map INFERRED — Xtensa not locally
disassemblable / CARRIED from ABI-06.]`

The 3-way Rosetta (the supported `at::Tensor` surface = **9 dtypes**):

| `c10::ScalarType` (ord) | `NEURON_ISA_TPB_DTYPE` | bytes | NKI / MLIR name | confidence |
|---|---|:-:|---|---|
| `Byte` (0)     | `UINT8` (0x3) | 1 | `uint8` | HIGH/INFERRED map |
| `Char` (1)     | `INT8` (0x2)  | 1 | `int8`  | HIGH/INFERRED |
| `Short` (2)    | `INT16` (0x4) | 2 | `int16` | HIGH/INFERRED |
| `Int` (3)      | `INT32` (0x8) | 4 | `int32` | HIGH/INFERRED |
| `Long` (4)     | `INT64` (0xC) | 8 | `int64` | HIGH/INFERRED (I64-gated) |
| `Half` (5)     | `FP16` (0x7)  | 2 | `float16` | HIGH/INFERRED |
| `Float` (6)    | `FP32` (0xA) **&** `FP32R` (0xB) | 4 | `float32` / `tf32` ‡ | HIGH/INFERRED |
| `BFloat16` (15)| `BFLOAT16` (0x6) | 2 | `bfloat16` | HIGH/INFERRED |
| — (no ScalarType) | `UINT16` (0x5) | 2 | — | unmapped / gated |
| — (no ScalarType) | `UINT32` (0x9) | 4 | — | unmapped / gated |
| — (no ScalarType) | `UINT64` (0x1) | 8 | — | unmapped / gated (U64) |
| — (no ScalarType) | `FP8_EXP3` (0xD) | 1 | (`float8_e3m4`) | no Float8 in pre-FP8 c10 |
| — (no ScalarType) | `FP8_EXP4` (0xE) | 1 | `float8_e4m3` † | no Float8 ScalarType |
| — (no ScalarType) | `FP8_EXP5` (0xF) | 1 | `float8_e5m2` † | no Float8 ScalarType |
| — | `FP4_EXP2`/`FP8_EXP2`/`INT4`/`SFP8_E8..E5`/`CPTC1..7` | sub-byte/1 | — | ISA/ucode/MX-only; no `at::Tensor` form |

‡ `tf32` is an NKI matmul name mapping to the device `fp32_mode` bit on `FP32`, **not** a
distinct ScalarType nor a dtype ordinal (§2.5).
† `float8_e4m3`/`e5m2` are NKI matmul-input names usable in the NKI/ISA matmul/MX paths but
**not** marshallable across the `at::Tensor` boundary in this pre-FP8 build.

**The legality gate** (where un-mapped dtypes die): the `at::Tensor` boundary asserts
`aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)`. So a dtype is **supported iff**
`isa_to_torch_dtype` yields a real ScalarType with a defined `elementSize()`. The 9 mapped
dtypes (Byte..Long, Half, Float, BFloat16) pass; FP8 / FP4 / MX / INT4 / CPTC / unsigned-wide
are **reachable through the raw ucode / SDMA / collective / MX paths** but **effectively absent
from the `at::Tensor` custom-op API** in v0.21.2.0. A newer FP8-capable PyTorch would extend
the supported set **without changing the ISA codes** (they are already reserved).
`[HIGH/OBSERVED for the gate + the pre-FP8 vintage / CARRIED from ABI-06; the unsigned-wide /
FP8 arm behavior is MED/INFERRED — no same-meaning pre-FP8 target.]`

The full host-side reconciliation (the `c10::ScalarType` ordinals, element sizes, and the
`isa_to_torch_dtype` arm analysis) is owned by
[the ScalarType ↔ dtype Rosetta page](../../abi/scalartype-dtype-rosetta.md).

> **NOTE — forward link.** `abi/scalartype-dtype-rosetta.md` is a **Part-7 page not yet
> authored** (the `abi/` directory is presently empty). The 9-dtype surface and the
> one-directional bridge are summarised here; the byte-level DWARF/host detail is deferred to
> that page. `[link planned.]`

---

## 6. Master tables

### Table A — per-generation × per-dtype availability

`●` = present in that gen's `NEURON_ISA_TPB_DTYPE` enum; `–` = absent. `[HIGH/OBSERVED — the
per-gen enum bodies read + counted this pass.]`

| DTYPE (code) | TONGA | SUNDA | CAYMAN | MARIANA | MAVERICK | role |
|---|:-:|:-:|:-:|:-:|:-:|---|
| `INVALID` (0x0)   | ● | ● | ● | ● | ● | sentinel |
| `UINT64` (0x1)    | – | ● | ● | ● | ● | compute (gated) |
| `INT8` (0x2)      | – | ● | ● | ● | ● | compute |
| `UINT8` (0x3)     | ● | ● | ● | ● | ● | compute |
| `INT16` (0x4)     | – | ● | ● | ● | ● | compute |
| `UINT16` (0x5)    | ● | ● | ● | ● | ● | compute |
| `BFLOAT16` (0x6)  | ● | ● | ● | ● | ● | compute (FP32 hub) |
| `FP16` (0x7)      | ● | ● | ● | ● | ● | compute (native cvt) |
| `INT32` (0x8)     | ● | ● | ● | ● | ● | compute |
| `UINT32` (0x9)    | – | ● | ● | ● | ● | compute / transport |
| `FP32` (0xA)      | ● | ● | ● | ● | ● | compute (native cvt) |
| `FP32R` (0xB)     | – | ● | ● | ● | ● | output-only |
| `INT64` (0xC)     | ● | ● | ● | ● | ● | compute (gated) |
| `FP8_EXP3` (0xD)  | – | ● | ● | ● | ● | compute (FP32 hub) |
| `FP8_EXP4` (0xE)  | – | ● | ● | ● | ● | compute (FP32 hub) |
| `FP8_EXP5` (0xF)  | – | ● | ● | ● | ● | compute (FP32 hub) |
| `FP4_EXP2` (0x10) | – | – | – | ● | ● | MX element / transport |
| `FP8_EXP2` (0x11) | – | – | – | – | ● | MX element / compute |
| `INT4` (0x12)     | – | – | – | – | ● | MX element / transport |
| `SFP8_E8` (0x13)  | – | – | – | – | ● | scale-only (E8M0) |
| `SFP8_E7` (0x14)  | – | – | – | – | ● | scale-only |
| `SFP8_E6` (0x15)  | – | – | – | – | ● | scale-only |
| `SFP8_E5` (0x16)  | – | – | – | – | ● | scale-only |
| `CPTC1..7` (0x19–1F) | – | – | – | ● | ● | transport (trellis) |
| **COUNT** | **8** | **16** | **16** | **24** | **30** | |

Capability-arrival notes: FP8 (E3/E4/E5) enters at **SUNDA** in the enum (computable via the
FP32 hub all gens; TONGA had no FP8). `FP4_EXP2` + `CPTC` enter at **MARIANA**. `FP8_EXP2` +
`INT4` + `SFP8_E8..E5` (the full MX micro-format + E8M0 scale) + the `MXTensorV2` v2 addressing
enter at **MAVERICK**. The POOL `TensorDequantize` MX surface predates the MARIANA
`MXTensorV2`/DVE-`QuantizeMX`/PE-`MatmulMX` wave — two distinct MX surfaces. `[HIGH/OBSERVED.]`

### Table B — per-dtype × per-op-class support

Cell key: `Y` = legal-and-handled; `out` = output-only; `s\|u` = either signedness; `T` =
transport (rides `UINT32`, not the dtype field); `SC` = scale-only; `(mx)` = via the MX
element/matmul path; `–` = gate-rejected. `[HIGH on the op-class × dtype-gate legality (the
header predicates §1c); MED per-cell on the on-device handler existence — the FLIX wall §7.]`

| DTYPE ↓ \ OP CLASS → | ALU-arith | ALU-bitvec | Cast/Copy | TensorReduce | Matmul-in | Dequant-out | MX-scale |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| INT8 / UINT8     | Y (s\|u) | Y | Y | Y | – | – | – |
| INT16 / UINT16   | Y (s\|u) | Y | Y | Y | – | – | – |
| INT32 / UINT32   | Y (s\|u) | Y | Y | Y | – | T (uint32) | – |
| INT64 / UINT64   | Y (64 band) | Y | – | Y (`_64` gated) | – | – | – |
| FP16             | Y | – | Y | Y | Y | Y (out) | – |
| BFLOAT16         | Y | – | Y | Y | Y | – | – |
| FP32             | Y | – | Y | Y | Y (/tf32) | – | – |
| FP32R            | out | – | out | out | – | – | – |
| FP8_EXP3 (E3M4)  | Y | – | Y | Y | – | – | (mx elem) |
| FP8_EXP4 (E4M3)  | Y | – | Y | Y | Y | Y (out) | (mx elem) |
| FP8_EXP5 (E5M2)  | Y | – | Y | Y | Y | Y (out) | (mx elem) |
| FP8_EXP2 (E2M5)  | Y (mav) | – | Y (mav) | — | – | – | (mx elem) |
| FP4_EXP2         | – | – | – | – | (mx) | T (fmt1) | (mx elem) |
| INT4             | – | – | – | – | (mx) | T (fmt2) | (mx elem) |
| SFP8_E8..E5      | – | – | – | – | – | – | SC (E8M0…) |
| CPTC1..7         | – | – | – | – | – | – | (transport) |

**Gate rules (header-verbatim §1c):**

* **ALU-arith** — `out_dtype` is `is_valid_dtype(AllowFP32R = True)`; src is
  `is_valid_dtype(AllowFP32R = False)`. Int-band ops require src/dst signedness to **match**
  the op's signedness (`is_signed_int`/`is_unsigned_int`). 64-bit only via the int band +
  `is_valid_dtype_64` (`INT64`/`UINT64` only).
* **ALU-bitvec** — out **must** be `is_valid_int_dtype` (integer-only); fp dtypes rejected.
  TS-bitvec further requires `in == out ∈ {INT/UINT 8/16/32}` (`is_valid_int_dtype_datapath`).
* **Cast/Copy** — `in` `is_valid_dtype(False)`, `out` `is_valid_dtype(True)`; **no** 64-bit
  (plain `is_valid_dtype`, not `_64`); **no** sub-byte FP4/INT4/scale (rejected by
  `dtype_4bit_illegal_check`/`dtype_scale_illegal_check`).
* **TensorReduce** — uses `is_valid_dtype_64(AllowU64 = True, AllowI64 = True)` — so it **can**
  take `INT64`/`UINT64` (contrast Cast/Copy).
* **Matmul-in** — `{ fp8_e4m3, fp8_e5m2, bf16, fp16, tf32, fp32 }`; accumulate FP32; out
  FP32/(BF16 v4+). `tf32` = `fp32_mode`, not a dtype.
* **Dequant-out** — FP8 (E5M2 for fmt1/2, E4M3 for fmt3/4); in via `UINT32` transport.
* **MX-scale** — `SFP8_E8..E5` only (`scale_dtype`); MX elements are the `is_valid_mx_dtype` set.

For the op-class × ALU-op detail (the per-op support grid) see
[the ALU op matrix](alu-op-matrix.md), and for the full opcode roster see
[the opcode catalog ledger](opcode-catalog-ledger.md). The full quantize/dequantize element
flow lives in [tensor-dequantize](tensor-dequantize.md) and [mx-dequant](mx-dequant.md).

---

## 7. Walls — what is *not* recovered

| Wall | Status | What would close it |
|---|---|---|
| **CPTC trellis-decode algorithm** | LOW — only the classification + bit-width recovered | a decoded `cptc_decode_impl<n>` body (see [cptc-codec](cptc-codec.md)) |
| **The dequant in-band block-8 scale bit-format** | MED/INFERRED (assumed E8M0) | a byte-pinned in-band scale field |
| **`NF4` 16-entry codebook values** | MED — observed as a const16 lookup, not byte-pinned | a clean read past the FLIX desync |
| **`isa_to_torch_dtype` unsigned-wide / FP8 arms** | MED/INFERRED — pre-FP8 c10 has no same-meaning target | a local Xtensa backend over the inlined arms |
| **Per-cell on-device handler existence (Table B)** | MED — gate-legality HIGH, byte-confirmation MED | resyncing the hand-scheduled FLIX VLIW bodies |
| **`move`/`get_sequence_bounds` per-dtype legs** | PENDING — gated to `{UINT32,INT32,FP32}` ("TODO other dtypes") | see [move-dtype](move-dtype.md) |

> **NOTE — the FLIX desync wall, quantified.** The carved device firmware is *stripped* (no
> symtab) and its `.xt.prop` bundle-boundary tables have a broken `sh_link`, so the native
> `xtensa-elf-objdump` in ELF mode falls back to a raw data dump (0 mnemonics). The only working
> disassembly is **raw-binary mode**, which decodes mnemonics and groups some FLIX bundles but
> loses the bundle boundaries → **28.4 % of instruction lines emit as `.byte`** (3349 / 11782,
> consistent across the three carved EXEC images this pass). A *non-stripped* device object
> (`libneuroncustomop.a`) by contrast decodes at **0 %** desync — confirming the desync is the
> stripped-firmware / raw-mode limitation, not a tool defect. The function **entries**,
> string-loaders, the dtype-test `bnei`/`beqi` compare chains (§2.2), and the byte-clean scalar
> leaves are OBSERVED; the *mid-bundle arm selection* is structural. The header gate predicates
> (§1c) are byte-exact, so Table B's **legality** is HIGH even where a cell's switch-arm is
> only MED-confirmed on device. `[MED — FLIX wall; the 28.4 % figure OBSERVED this pass.]`

The `move` full-register path is gated, byte-exact, to `{ UINT32, INT32, FP32 }` — the
firmware rodata carries the verbatim assert (read this pass from `libnrtucode_internal.so`):

```text
/opt/workspace/NeuronUcode/src/decode/move.cpp:41
  ((ins.dtype == NEURON_ISA_TPB_DTYPE_UINT32) ||
   (ins.dtype == NEURON_ISA_TPB_DTYPE_INT32)  ||
   (ins.dtype == NEURON_ISA_TPB_DTYPE_FP32))
  && "highest priority is full-register moves. TODO other dtypes"
```

This is the byte-exact witness that the firmware's `ins.dtype` field is the **same**
`NEURON_ISA_TPB_DTYPE` enum documented here, and that `move`'s non-32-bit legs are explicitly
incomplete. See [move-dtype](move-dtype.md). `[HIGH/OBSERVED — string read this pass.]`

---

## Cross-references

* [ScalarType ↔ dtype Rosetta](../../abi/scalartype-dtype-rosetta.md) — *forward link, Part 7
  (not yet authored; `abi/` is empty this pass)*: the host-side `c10::ScalarType` ordinals,
  element sizes, and the `isa_to_torch_dtype` arm analysis.
* [ALU op matrix](alu-op-matrix.md) — *stub*: the per-op × per-dtype ALU support grid that
  Table B summarises.
* [tensor-dequantize](tensor-dequantize.md) — *stub*: the POOL `TensorDequantize` /
  `dequant_fmt` micro-format expansion.
* [mx-dequant](mx-dequant.md) — *stub*: the MX block-scale dequant (both mechanisms §2.4).
* [opcode-catalog-ledger](opcode-catalog-ledger.md) — *concurrently authored*: the full opcode
  roster (`QUANTIZE_MX = 0xe3`, `MATMUL_MX = 0x0A`, `LDWEIGHTS_MX = 0x09`, …).
* [pe-matmul](pe-matmul.md) — *stub*: the matmul/PE FP32-accum dtype matrix (§2.5).
* [cptc-codec](cptc-codec.md) — *stub*: the CPTC codec decoder family (§3).
* [move-dtype](move-dtype.md) — the `move` full-register dtype gate (§7).
* [The Confidence & Walls model](../../reference/confidence-model.md) — the `[HIGH/OBSERVED]`
  tag and wall taxonomy used throughout this page.
