# TensorScalarPtrMulti (Arith/Bitvec) — the multi-scalar 4D fold that closes the family

> **Scope.** `TensorScalarPtrMulti` is the **pointer-multi** member of the `TensorScalar*` family: a 4-D
> elementwise op that applies **one** `AluOp` to a `[W, Z, Y, X]` tensor, **swapping a new scalar for each
> outer W-slice** — `for i in range(W): dst[i,:,:,:] = src[i,:,:,:] op scalar[i]`, with `1 <= W <= 8`. The
> `W` scalars are **not** inline immediates: they are **preloaded into DVE flops** by a preceding
> [`TensorScalarImmLd`](ts-immld.md) instruction, so the operand struct carries **no** scalar at all.
>
> It is **two opcodes** binding **one 64-byte struct**:
>
> - `0x4F` `TensorScalarPtrMultiArith` — preloaded by [`TensorScalarImmLdArith`](ts-immld.md) (`0x70`).
> - `0x5F` `TensorScalarPtrMultiBitvec` — preloaded by [`TensorScalarImmLdBitvec`](ts-immld.md) (`0x71`).
>
> Both are **`// n, ucode/kaenadve exists, not maintained/used`** — deprecated/unwired, like the
> [`ImmLd`](ts-immld.md) loader. Decoding them **closes** the `TensorScalar*` family
> ([`tensor-scalar.md`](tensor-scalar.md) §10): every `TensorScalar*` opcode is now accounted for.
>
> This page decodes the struct (compile-verified `sizeof == 64` on all four gens); pins the **"PtrMulti"
> multi-scalar semantics** (the verbatim header loop, the producer/consumer pairing with `ImmLd`); proves
> the **Arith-vs-Bitvec split** is an **op-class + dtype** split (not a two-op split); tabulates the dtype
> matrix; documents the **stub-wired** DVE dispatch (registered self-name thunk, no compute body); and
> presents the **family-close synthesis**. The **SUNDA-only Dual** predecessor (`0x87`/`0x88`) is
> cross-linked to [`sunda-dual-tensorscalarptr.md`](sunda-dual-tensorscalarptr.md).
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | `TensorScalarPtrMultiArith` = **`0x4F`** (79), `TensorScalarPtrMultiBitvec` = **`0x5F`** (95). Both `// n, ucode/kaenadve exists, not maintained/used`. Byte-identical value/flag on **all four** gens. | `common.h` (mariana `:190`/`:191`, sunda `:182`/`:183`, cayman/maverick same) | HIGH/OBSERVED |
| 2 | Both bind **one** 64-byte struct `NEURON_ISA_TPB_S4D4_TSM_STRUCT` — a **4D-tensor, SINGLE-AluOp, NO-immediate** struct, structurally **distinct** from `S3D3_TS`. | `s4d4_tsm.h:110` + `ISA_STATIC_ASSERT == 64` + my own gcc `offsetof` (all 4 gens) | HIGH/OBSERVED |
| 3 | **"PtrMulti" semantics**: one `op`, a **new scalar per outer W-slice** — `for i in range(W): dst[i,:,:,:] = src[i,:,:,:] op scalar[i]`, `1 <= W <= 8` (`W = src.num_elem[3]`). | `s4d4_tsm.h:28–46` verbatim + `has_valid_src_slices_tsm_singular` (`:197`) | HIGH/OBSERVED |
| 4 | The `W` scalars are **preloaded** by the preceding [`TensorScalarImmLd`](ts-immld.md): `ImmLdArith → 0x4F`, `ImmLdBitvec → 0x5F`. The struct has **zero** immediate fields — it *must* read the ImmLd-loaded flops. | `s4d4_tsm.h:22–26, 36` + the imm-less struct | HIGH/OBSERVED |
| 5 | **Arith-vs-Bitvec is an op-class + dtype split** (because `S4D4_TSM` *has* an `op` field — unlike [`ImmLd`](ts-immld.md)). Arith = `is_general_arith_op`; Bitvec = `is_bitvec_op` (the **full 10**, *including* `Crc32`). Both reject `Divide`/`Pow`/`Mod`. | `tensor_scalar_ptr_multi_valid_ops` (`:172`) | HIGH/OBSERVED |
| 6 | **dtype split**: Arith admits any valid dtype (12-way, FP32R barred on input), "all converted to fp32 in the DVE"; Bitvec forces `in_dtype == out_dtype ∈ {UINT8, UINT16, UINT32, INT32}` raw-bits. | `tensor_scalar_ptr_multi_valid_types` (`:183`) + header `:54–59` | HIGH/OBSERVED |
| 7 | `op_dim` is **FORCED `XYZ`**; `reverse_operands` is **FORCED `{None, Both}`** (not the 4-valued set) — the 8 scalars **bank 4+4 across two physical DVE ALU stages**, so a reverse must flip both. | `is_valid_tsm_subdim` (`:193`) + `tensor_scalar_ptr_multi_reverse_chk` (`:202`) + the verbatim hardware note `:99–107` | HIGH/OBSERVED |
| 8 | **Stub-wired** on every DVE gen: the self-name `"S: TensorScalarPtrMulti{Arith,Bitvec}"` strings + a registered **LOG-only** thunk exist (CAYMAN/MARIANA/MAVERICK), but **no compute body** — the `// n` signature. | binary strings (sha256 `b7c67e89…`) + the SX-FW-58 carve | HIGH/OBSERVED; runtime-result inference HIGH/INFERRED |
| 9 | **Per-gen**: opcodes + struct byte-identical on all 4 gens. The **SUNDA-only Dual** pair `0x87`/`0x88` (W≤4, dual-op) was **retired** on NC-v3+ (`0x87` reused as a semaphore constant); the `struct2opcode` JSON still **stale-lists** Dual everywhere. MAVERICK adds tile-aware channel ranging. | `common.h` + `instruction_mapping.json` + `s4d4_tsm.h` diff (all 4 gens) | HIGH/OBSERVED |

---

## 2. Provenance / carve anchors

All device-firmware facts derive from static analysis of the shipped device-firmware blob (carved from
`libnrtucode_internal.so`, disassembled with the Cadence Xtensa toolchain that ships *inside* the
gpsimd-tools package, `XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW) plus the shipped public ISA C headers,
**compile-verified this session with gcc**. No source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — re-verified in-task (`sha256sum`) |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM / DRAM | VA `0x408fc0` / `0x425520` (`.rodata` VA == file offset) |
| CAYMAN `NX_DVE` DEBUG DRAM | self-name pool `0x27c0`/`0x27de` |
| MAVERICK `NX_DVE` DEBUG DRAM | self-name pool `0x28d0`/`0x28ee` |

The authoritative struct/enum/validator source is the shipped public ISA headers (same package):

- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s4d4_tsm.h` — the `S4D4_TSM` struct + the
  `is_valid_tensor_scalar_ptr_multi` validator chain + the verbatim op semantics comment.
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — the opcode enum; `ALU_OP`;
  `TENSOR4D`; `TENSOR_SUBDIM`; `TENS_SCALAR_REV_OPS`; `DTYPE`; `POOLING_NUM_CHANNELS == 128`.
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_assert.h` — the `is_general_arith_op` / `is_bitvec_op`
  predicate bodies.
- `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` — the `struct2opcode` binding.

The `S4D4_TSM` struct is **byte-identical** (`sizeof 64`, same offsets) on sunda/cayman/mariana/maverick —
**compile-verified** below (§4). `[HIGH/OBSERVED]`

> **NOTE — `.rodata`/`.text` VMA == file offset, but the ncore2gp config DLLs' `.data`/`.data.rel.ro`
> carry a `+0x200000` VMA−fileoffset delta** (confirm per-section with `readelf -SW`). The DVE DEBUG IRAM/DRAM
> blobs of this page live in `.rodata`, so VA == offset; do not over-generalise the delta to them. `[HIGH/OBSERVED]`

---

## 3. The opcodes — `TensorScalarPtrMulti{Arith,Bitvec}` (+ the SUNDA Dual twins)

Read verbatim from `aws_neuron_isa_tpb_common.h` (mariana lines 190–191; identical values on every gen):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_ARITH_OP          = 0x43,   // Y                                       base, maintained
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_BITVEC_OP         = 0x53,   // Y                                       base, maintained
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP      = 0x44,   // n, use TensorScalarArithOp instead      deprecated, WORKING
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP     = 0x54,   // n, use TensorScalarBitvecOp instead      deprecated, WORKING
…
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_ARITH   = 0x4F,   // n, ucode/kaenadve exists, not maintained/used   <== THIS PAGE
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_BITVEC  = 0x5F,   // n, ucode/kaenadve exists, not maintained/used   <== THIS PAGE
…
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_ARITH      = 0x70,   // n, ucode/kaenadve exists, not maintained/used   the LOADER (ts-immld.md)
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_BITVEC     = 0x71,   // n, ucode/kaenadve exists, not maintained/used
```

And the **SUNDA-only** Dual twins (`sunda/common.h:221–222`, absent on NC-v3+):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH  = 0x87,   // n, ucode/kaenadve exists, not maintained/used   SUNDA ONLY
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC  = 0x88,   // n, ucode/kaenadve exists, not maintained/used   SUNDA ONLY
```

The arith/bitvec opcode-pair convention is the family's standard low-nibble split: `0x4F`→arith,
`0x5F`→bitvec (high nibble `4`=arith, `5`=bitvec, low nibble `F` for the pointer-multi form). `0x4F`/`0x5F`
are firmware **kernel-lane** opcodes (the ~140-entry `NEURON_ISA_TPB_OPCODE_*` SEQ axis), **not** Xtensa ISA
mnemonics — the same two-axis caution [`tensor-scalar.md`](tensor-scalar.md) §3 raises. `[HIGH/OBSERVED]`

> **GOTCHA — `0x87` is reused as a non-opcode constant on NC-v3+.** On cayman/mariana/maverick the value
> `0x87` is `NEURON_ISA_TPB_UPDATE_MODE_SEM_SUB_REG_READ = 0x87` (`common.h:366`), a **semaphore-update-mode**
> constant in a *different* enum — confirming the Dual *opcode* was **retired** post-NC-v2, not merely
> renamed. A reimplementer must not treat `0x87`/`0x88` as PtrMulti opcodes outside SUNDA. `[HIGH/OBSERVED]`

### 3a. The struct→opcode binding (`instruction_mapping.json`)

The `struct2opcode` table binds `NEURON_ISA_TPB_S4D4_TSM_STRUCT` to **four** opcodes — on **every** gen,
including the Dual pair — `jq`-read verbatim (mariana):

```json
"NEURON_ISA_TPB_S4D4_TSM_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_ARITH",        // 0x4F  this page
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_BITVEC",       // 0x5F  this page
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH",   // 0x87  SUNDA-only — STALE on NC-v3+
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC"   // 0x88  SUNDA-only — STALE on NC-v3+
]
```

`S4D4_TSM` is bound to the PtrMulti family and **nothing else** — there are no shared-struct co-residents
(unlike [`ImmLd`](ts-immld.md)'s `S2_BN`, which serves three load ops). `[HIGH/OBSERVED]`

> **CORRECTION — the JSON is a STALE superset on NC-v3+.** On cayman/mariana/maverick the `struct2opcode`
> table still lists `PTR_MULTI_DUAL_ARITH`/`DUAL_BITVEC` under `S4D4_TSM`, but those opcode **names are no
> longer defined** in the NC-v3+ `common.h` enum (only sunda defines them). The mapping table was never
> pruned when the Dual pair was retired. The **live** PtrMulti pair is `0x4F`/`0x5F` everywhere; do not
> emit the Dual binding on NC-v3+. `[HIGH/OBSERVED — `common.h` ⊕ `instruction_mapping.json` diff, all 4 gens]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S4D4_TSM_STRUCT` (64 B)

`s4d4_tsm.h:110`, `ISA_STATIC_ASSERT(sizeof == 64)`. **Compile-verified this session** — a real C program
`#include`-ing each gen's `aws_neuron_isa_tpb_s4d4_tsm.h` and printing `offsetof`/`sizeof` prints
**byte-identical** output on all four gens:

```text
sunda/cayman/mariana/maverick (identical):
  sizeof = 64
  header=0  events=4  src_mem_pattern=12  in_dtype=32  out_dtype=33  num_active_channels=34
  reserved0=35  op=36  op_dim=37  reverse_operands=38  reserved1=39  dst_mem_pattern=44
```

| off | size | field | type | role for PtrMulti |
|----:|-----:|-------|------|------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `opcode = 0x4F`(Arith) / `0x5F`(Bitvec); (NC-v5) `inst_flags` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 20 | `src_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | **the INPUT — 4D `[W,Z,Y,X]`; `W = num_elem[3]` = the scalar count (1..8)** |
| 32 | 1 | `in_dtype` | `DTYPE` | input dtype (**`FP32R` NOT allowed**) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype (**`FP32R` allowed**) |
| 34 | 1 | `num_active_channels` | `uint8` | partition count `1..128` (`POOLING_NUM_CHANNELS`) |
| 35 | 1 | `reserved0[1]` | `uint8` | **must be 0** (`s4d4_tsm_reserved_zero`) |
| 36 | 1 | `op` | `NEURON_ISA_TPB_ALU_OP` | **the SINGLE AluOp** — `src op scalar[i]` (second op is bypass) |
| 37 | 1 | `op_dim` | `NEURON_ISA_TPB_TENSOR_SUBDIM` | **FORCED `XYZ` (0x04)** — the inner sub-volume per W-slice |
| 38 | 1 | `reverse_operands` | `NEURON_ISA_TPB_TENS_SCALAR_REV_OPS` | **FORCED `{None=0, Both=3}`** (§4b) |
| 39 | 5 | `reserved1[5]` | `uint8` | **must be 0** (`s4d4_tsm_reserved_zero`) |
| 44 | 20 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | **the OUTPUT — same element count as `src`** |

`[HIGH/OBSERVED — my own gcc `offsetof` output, all 4 gens; header text verbatim]`

`NEURON_ISA_TPB_TENSOR4D` (`common.h:693`, **20 B**) = `{ ADDR4 start_addr(4); int16 step_elem[4](8);
uint16 num_elem[4](8) }` — a 4-D strided access pattern in C-order `[W,Z,Y,X]`. `num_elem[3]` is **W**, the
outer scalar-index dimension; `num_elem[2..0]` are the `Z,Y,X` sub-volume the op streams for each W. Both
`src` and `dst` are `AllowedInPSUM::True && AllowedInSBUF::True`, so PtrMulti can read the matmul **PSUM**
directly (`tensor4d_valid(…, AllowedInPSUM::True, …)`, `s4d4_tsm.h:154–155`). `[HIGH/OBSERVED]`

> **CORRECTION — the header's inline `reserved1` byte-count comment is a typo.** The declaration is
> `uint8_t reserved1[5]` with a trailing comment `// 6 (39 - 43)`. The byte range `39..43` is **five** bytes
> (39, 40, 41, 42, 43); the array is `[5]`, and `s4d4_tsm_reserved_zero` (`:209`) checks exactly
> `reserved1[0..4]` (five indices). The "6" in the comment is wrong — the struct is `5` reserved bytes
> there, which is what makes `sizeof == 64` hold (`44 + 20 = 64`). `[HIGH/OBSERVED — header text + my compile output]`

### 4a. The four "Multi" deltas vs `S3D3_TS` (the structural fingerprint)

`S4D4_TSM` is **not** `S3D3_TS` ([`tensor-scalar.md`](tensor-scalar.md) §4) with a relabelled field — it is a
structurally different layout, and **every** difference flows from "Multi":

| field | `S3D3_TS` (base, `0x43`/`0x53`) | `S4D4_TSM` (PtrMulti, `0x4F`/`0x5F`) | why |
|-------|--------------------------------|--------------------------------------|-----|
| src/dst pattern | `TENSOR3D` 16 B @16/@48 | `TENSOR4D` **20 B** @12/@44 | the **W** scalar-index axis (`num_elem[3]`) |
| ALU op(s) | `op0`@36 **+** `op1`@37 (TWO) | `op`@36 (**ONE**; "2nd op bypass") | "merge 8 *single*-op TensorScalarPtr folds" |
| `op_dim` | (none) | `op_dim`@37 = **XYZ** | the inner sub-volume; scalar swaps at W |
| scalar source | `imm0`@40 / `imm1`@44 + `imm0_src`@35 / `imm1_src`@39 | **(none)** — scalars in ImmLd-preloaded DVE flops | "Ptr": no inline immediate |
| `reverse_operands` | `{None, First, Second, Both}` (4) | `{None, Both}` only (2) | the 4+4 two-stage banking (§4b) |
| `accumulator_cmd` | `accum_cmd`@12 (forced Idle) | (none) | PtrMulti is pure elementwise |

The decisive structural proof of "Ptr": `S4D4_TSM` has **zero** immediate fields. The op **cannot** carry an
inline scalar; it **must** consume the [`ImmLd`](ts-immld.md)-preloaded flops. `[HIGH/OBSERVED]`

### 4b. `op_dim` forced XYZ, `reverse_operands` forced `{None, Both}` — and the hardware reason

```c
// fn is_valid_tsm_subdim(i: Inst) -> bool { (i.s4d4_tsm.op_dim == TensorSubdim::XYZ) }      // s4d4_tsm.h:193
// fn tensor_scalar_ptr_multi_reverse_chk(i: Inst) -> bool {                                  // s4d4_tsm.h:202
//       is_valid_enum(EnumList::TensScalarRevOps, i.s4d4_tsm.reverse_operands)
//    && has_tensor_scalar_ptr_multi_opcode(i)
//    && (   (i.s4d4_tsm.reverse_operands == TensScalarRevOps::None)
//        || (i.s4d4_tsm.reverse_operands == TensScalarRevOps::Both))
// }
```

`TENSOR_SUBDIM` (`common.h:1195`): `UNUSED=0x00, X=0x02, XY=0x03, XYZ=0x04, XYZW=0x05`. `op_dim` is **forced
`XYZ`** — the op iterates the inner `XYZ` sub-volume and swaps the scalar at the `W` boundary (the header
note `:84`: "will use a different Ptr value for every XYZ dimension"). `TENS_SCALAR_REV_OPS`
(`common.h:1390`): `None=0, First=1, Second=2, Both=3`.

The header explains **why** `reverse` collapses to `{None, Both}` — a verbatim hardware note (`s4d4_tsm.h:99–107`):

```text
NONE (0):  result = tensor op scalar          (src on the left)
BOTH (3):  result = scalar op tensor          (scalar on the left — the reverse)
"Why Both?  reverse_operands controls the 'reverse' signal to the ALUs in the first two pipeline
 stages in DVE.  Although we only have a SINGLE op in TensorScalarPtrMulti, we actually use the ALU
 in stage 0 for the FIRST FOUR scalars and the one in stage 1 for the SECOND FOUR scalars.
 Therefore, when we reverse that single op, we need to reverse 'Both'."
```

The 8-scalar vector is **banked 4+4 across two physical DVE ALU pipeline stages** (stage 0 = scalars 0..3,
stage 1 = scalars 4..7). A "reverse" must flip the reverse signal on **both** stages, so `First`/`Second`
(which would reverse only one stage) are **illegal**. This is independent hardware corroboration of the
"up to 8 / two banks of 4" scalar geometry. `[HIGH/OBSERVED — reverse_chk + verbatim header note, all 4 gens]`

---

## 5. The "PtrMulti" semantics — the per-W-slice scalar swap + the ImmLd producer/consumer pair

The whole op is documented **verbatim** in the header (`s4d4_tsm.h:28–46`, read here from mariana; identical
on all four gens):

```text
// TensorScalarPtrMulti merges up to eight regular TensorScalarPtr instructions
// (single ALU op per instruction, i.e. second op is bypass).
//   src has shape [W, Z, Y, X] (numpy C-order); W = number of preloaded scalars (1 <= W <= 8)
//   scalars preloaded by the previous TensorScalarImmLd instruction
//
// This instruction effectively does:
//   for i in range(W):
//       dst[i, :, :, :] = (src[i, :, :, :] op scalar[i])      # broadcasting
//
// ... performs ONE TensorScalar ALU op on the input src tensor and SWAPS A NEW SCALAR every time
//     one round of XYZ dimensions are completed. Note, only a single op type is applied for all
//     input elements.
```

### 5a. The "Multi" — one op, a new scalar per W-slice

"Multi" is the **outer W dimension** (1..8) over which a **different** preloaded scalar is folded:
`scalar[0]` against the sub-volume `src[0,:,:,:]`, `scalar[1]` against `src[1,:,:,:]`, … `scalar[W-1]`
against `src[W-1,:,:,:]`. There is exactly **one** `AluOp` (`op`@36) for **all** elements; the only thing
that changes per W-slice is *which* preloaded scalar is folded. `has_valid_src_slices_tsm_singular`
(`s4d4_tsm.h:197`) enforces the W cap:

```c
// fn has_valid_src_slices_tsm_singular(i: Inst) -> bool {
//       (i.s4d4_tsm.src_mem_pattern.num_elem[3] >= 1)         // W >= 1
//    && (i.s4d4_tsm.src_mem_pattern.num_elem[3] <= 8)         // W <= 8
// }
```

This is literally "merge `W` single-op `TensorScalarPtr` instructions into one 4D kernel." `[HIGH/OBSERVED]`

### 5b. The "Ptr" — the scalars come from the ImmLd preload (the producer/consumer macro-pair)

"Ptr" = the scalars are **pointer-fetched from memory** (via [`ImmLd`](ts-immld.md)), not inline immediates.
The header (`s4d4_tsm.h:22–26`) pins the producer/consumer pairing exactly:

```text
// - TensorScalarPtrMultiArith   -> Preload immediate using TensorScalarImmLdArith   (0x70)
// - TensorScalarPtrMultiBitvec  -> Preload immediate using TensorScalarImmLdBitvec   (0x71)
// - Preloaded scalars from the previous TensorScalarImmLd instruction
```

So PtrMulti is the deprecated single-pointer `TensorScalarPtr` ([`tensor-scalar.md`](tensor-scalar.md) §8,
`0x44`/`0x54`, "scalar always from a pointer") **generalised** from **one** pointer-scalar to a **vector of
up-to-8** pointer-loaded scalars, indexed by the 4th (W) tensor dimension. The two instructions form a
**macro-pair**:

```c
// The ImmLd -> PtrMulti producer/consumer macro-pair (the firmware-confirmed fusion the name encodes).
//
// 1) PRODUCER — TensorScalarImmLd{Arith,Bitvec} (0x70/0x71, struct S2_BN; see ts-immld.md):
//      LOAD up to 8 per-channel scalars from src memory (an SBUF/PSUM read) into DVE storage flops.
//      Arith path: convert each loaded value TO fp32.   Bitvec path: take the raw integer bits.
//
// 2) CONSUMER — TensorScalarPtrMulti{Arith,Bitvec} (0x4F/0x5F, struct S4D4_TSM; THIS page):
//      W = src_mem_pattern.num_elem[3];                 // 1..8, MUST equal the ImmLd scalar count
//      for (i = 0; i < W; ++i)                          // outer W loop = scalar index
//        for (each (z,y,x) in the XYZ sub-volume)       // op_dim == XYZ
//          dst[i][z][y][x] = (reverse == REV_BOTH)
//                          ? alu(op, /*lhs=*/scalar[i], /*rhs=*/src[i][z][y][x])   // scalar op tensor
//                          : alu(op, /*lhs=*/src[i][z][y][x], /*rhs=*/scalar[i]);  // tensor op scalar
//      // scalar[i] = the i-th ImmLd-preloaded flop; the 8 flops bank 4+4 across two DVE ALU stages (§4b).
```

The ImmLd dtype-convert behaviour and the PtrMulti compute dtype **must agree**: the Arith path is fp32, the
Bitvec path is raw integer bits. The loader and the consumer carry the **identical** arith/bitvec dtype
contract (§6). `[HIGH/OBSERVED — header verbatim + the imm-less struct]`

> **NOTE — `W` is a runtime contract, not enforced by the struct.** `has_valid_src_slices_tsm_singular`
> bounds `W ∈ [1,8]`, but **nothing in the `PtrMulti` instruction proves the preceding `ImmLd` actually
> loaded `W` scalars** — the header says only "`W` *must be equal to* the number of scalars preloaded." The
> producer/consumer count agreement is a programmer obligation, checked by neither validator; a mismatched
> `W` reads stale/uninitialised flops. `[HIGH/OBSERVED — header `:34–35`; the no-cross-check is INFERRED from the absence of any ImmLd reference in the validator]`

### 5c. Same element count + the validator chain

The full validity chain (`is_valid_tensor_scalar_ptr_multi`, `s4d4_tsm.h:134`), read verbatim:

```c
// fn is_valid_tensor_scalar_ptr_multi(i: Inst) -> bool {
//       has_valid_neuron_header(i) && has_valid_neuron_events(i)
//    && has_tensor_scalar_ptr_multi_opcode(i)             // opcode ∈ {0x4F, 0x5F}
//    && has_valid_src_slices_tsm_singular(i)              // 1 <= W <= 8         (§5a)
//    && is_valid_tensor_scalar_ptr_multi_common(i)        // the body below:
// }
// fn is_valid_tensor_scalar_ptr_multi_common(i: Inst) -> bool {
//       tensor_scalar_ptr_multi_valid_ops(i)              // §6 — op-class split
//    && tensor_scalar_ptr_multi_valid_types(i)            // §6 — dtype split
//    && tensor_scalar_ptr_multi_reverse_chk(i)            // §4b — reverse ∈ {None, Both}
//    && s4d4_tsm_reserved_zero(i)                         // reserved0[0] + reserved1[0..4] == 0
//    && is_valid_dtype(i.s4d4_tsm.in_dtype,  DtypeAllowFP32R::False)   // input bars FP32R
//    && is_valid_dtype(i.s4d4_tsm.out_dtype, DtypeAllowFP32R::True)    // output admits FP32R
//    && is_valid_aluop(i.s4d4_tsm.op)
//    && is_valid_tsm_subdim(i)                            // op_dim == XYZ      (§4b)
//    && has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS)  // 128
//    && start_addr_active_channels(src.start_addr, num_active_channels)
//    && start_addr_active_channels(dst.start_addr, num_active_channels)
//    && tensor4d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)
//    && tensor4d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
//    && same_element_count_t4d(src, dst)                  // W·Z·Y·X out == W·Z·Y·X in
// }
```

In one sentence: **apply the single `AluOp` to every element of the 4-D `src` (PSUM or SBUF), swapping the
preloaded scalar at each W boundary, and write the same-shaped 4-D `dst`** — `op_dim` XYZ, `reverse ∈
{None,Both}`, dtypes valid, the op in the arith/bitvec accept set. `same_element_count_t4d` makes the op an
elementwise transform (one out-element per in-element). `[HIGH/OBSERVED]`

---

## 6. The Arith-vs-Bitvec split — op-class **+** dtype (NOT a two-op split)

This is the central conceptual correction the page makes. **`0x4F` vs `0x5F` is a dtype/op-class split, not
a two-op split.** PtrMulti has only **one** `op` field; the two opcodes select **different ALU-op accept
sets and different dtype rules** for that single op — exactly like the maintained base ops `0x43`/`0x53`
([`tensor-scalar.md`](tensor-scalar.md) §6), and **unlike** [`ImmLd`](ts-immld.md) (whose split is *purely*
a load-dtype split because its `S2_BN` struct has no `op` field). Read verbatim (`s4d4_tsm.h:172`):

```c
// fn tensor_scalar_ptr_multi_valid_ops(i: Inst) -> bool {
//       is_valid_enum(EnumList::AluOp, i.s4d4_tsm.op)
//    && (i.s4d4_tsm.op != AluOp::Divide)                  // 0x07   common reject
//    && (i.s4d4_tsm.op != AluOp::Pow)                     // 0x1A   common reject
//    && (i.s4d4_tsm.op != AluOp::Mod)                     // 0x1B   common reject
//    && ( !tensor_scalar_ptr_multi_bitvec(i) || is_bitvec_op(i.s4d4_tsm.op) )         // 0x5F arm
//    && ( !tensor_scalar_ptr_multi_arith(i)  || is_general_arith_op(i.s4d4_tsm.op) )  // 0x4F arm
// }
```

### 6a. The op-set split (the FW-79 op-class predicates)

| family | `op` accept set | Tag |
|--------|-----------------|-----|
| **Arith** (`0x4F`) | `is_general_arith_op(op)` | HIGH/OBSERVED |
| **Bitvec** (`0x5F`) | `is_bitvec_op(op)` — the **full 10**, *including* `Crc32` | HIGH/OBSERVED |
| **both** | reject `Divide(0x07)`, `Pow(0x1A)`, `Mod(0x1B)` | HIGH/OBSERVED |

The predicate bodies are read verbatim from `aws_neuron_isa_tpb_assert.h`:

- **`is_bitvec_op`** (`assert.h:1788`) = `{Bypass, BitwiseNot, ArithShiftLeft, ArithShiftRight,
  LogicalShiftLeft, LogicalShiftRight, BitwiseAnd, BitwiseOr, BitwiseXor, Crc32}` — **10 ops**.
- **`is_general_arith_op`** (`assert.h`) = `is_arith_op(op)` minus `{Divide, Pow, Mod}`, minus
  `is_valid_int_aluop(op)`, minus `Rsqrt` — i.e. **17 ops**: `{Bypass, Add, Subtract, Mult, Max, Min,
  LogicalAnd, LogicalOr, LogicalXor, IsEQ, IsGT, IsGE, IsLE, IsLT, IsNE, AbsoluteDiff, AbsoluteValue}`
  (plus the per-gen extension band `AbsMax/AbsMin/ReLU/Square` `0x20..0x23` where the target gen's enum
  defines it; see [`tensor-scalar.md`](tensor-scalar.md) §6c).

`[HIGH/OBSERVED — predicate bodies verbatim]`

> **NOTE — Bitvec uses `is_bitvec_op` (Crc32 IN), NOT `is_general_bitvec_op` (Crc32 OUT).** Keep the
> accounting straight: `is_bitvec_op` = **10 ops** (with `Crc32`); `is_general_bitvec_op` = **9 ops**
> (`Crc32` removed; `assert.h:1827`) — the latter is what [`scalar-tensor-tensor.md`](scalar-tensor-tensor.md)
> uses. PtrMultiBitvec admits `Crc32` (it gates on the full `is_bitvec_op`); STT-bitvec does **not**. Do not
> conflate the two predicates. `[HIGH/OBSERVED]`

> **NOTE — no `op0`/`op1` composition rule.** Because PtrMulti carries a **single** `op`, there is **no**
> `(op0 != Bypass) || (op1 == Bypass)` gate and **no** `Rsqrt`-then-`Bypass` special case (both of which the
> two-op base `S3D3_TS` carries; [`tensor-scalar.md`](tensor-scalar.md) §6b). The op is whatever single
> member of the accept set you put in `op`@36; `Bypass` is the degenerate copy. `[HIGH/OBSERVED]`

### 6b. The dtype split (`tensor_scalar_ptr_multi_valid_types`)

```c
// fn tensor_scalar_ptr_multi_valid_types(i: Inst) -> bool {
//       tensor_scalar_ptr_multi_arith(i)                  // ARITH: no extra dtype constraint
//    || (   tensor_scalar_ptr_multi_bitvec(i)             // BITVEC: in==out ∈ {u8,u16,u32,i32}
//        && (i.s4d4_tsm.in_dtype == i.s4d4_tsm.out_dtype)
//        && (   (i.s4d4_tsm.in_dtype == Dtype::INT32)
//            || (i.s4d4_tsm.in_dtype == Dtype::UINT32)
//            || (i.s4d4_tsm.in_dtype == Dtype::UINT16)
//            || (i.s4d4_tsm.in_dtype == Dtype::UINT8)))
// }
```

| family | `in_dtype` | `out_dtype` | rule | Tag |
|--------|-----------|-------------|------|-----|
| **Arith** (`0x4F`) | any valid dtype, **`FP32R` barred** (`AllowFP32R::False`) | same set **+ `FP32R`** (`AllowFP32R::True`) | unconstrained beyond the outer `is_valid_dtype`; "all converted to fp32 in the DVE" | HIGH/OBSERVED |
| **Bitvec** (`0x5F`) | `in == out` **and** ∈ `{UINT8(0x3), UINT16(0x5), UINT32(0x9), INT32(0x8)}` | identical to `in_dtype` | raw-bits identity; "u8/u16 zero-extended to 32 bits" | HIGH/OBSERVED |

This **mirrors the whole family**: arith = fp-hub (convert-to-fp32) general-arith; bitvec =
integer-container raw-bits identity — the same shape as the base `0x43`/`0x53`
([`tensor-scalar.md`](tensor-scalar.md) §6d). And it mirrors the **loader**: PtrMultiBitvec's 4-dtype set
`{u8, u16, u32, i32}` is **identical** to [`ImmLdBitvec`](ts-immld.md)'s load-dtype set — `INT8`/`INT16` are
excluded on both (zero-extend is meaningless for signed-narrow), all fp/`FP32R`/64-bit excluded. `[HIGH/OBSERVED]`

---

## 7. The dtype matrix

`DTYPE` ordinals (`common.h:805`): `INVALID 0x0, UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5,
BF16 0x6, FP16 0x7, INT32 0x8, UINT32 0x9, FP32 0xA, FP32R 0xB, INT64 0xC, FP8_E3 0xD, FP8_E4 0xE,
FP8_E5 0xF`.

| | `PtrMultiArith` (`0x4F`) | `PtrMultiBitvec` (`0x5F`) |
|---|---|---|
| **`in_dtype`** | `{FP8_E3, FP8_E4, FP8_E5, BF16, FP16, FP32, INT8, UINT8, INT16, UINT16, INT32, UINT32}` (12; `AllowFP32R::False`) → **all converted to fp32** in the DVE | `{UINT8, UINT16, UINT32, INT32}` (4), raw bits; u8/u16 zero-extended to 32 |
| **`out_dtype`** | same 12 **+ `FP32R`** (`AllowFP32R::True`; header `:74` lists `FP16/BF16/UINT8/INT8/FP8*/FP32/FP32R/INT32/UINT32`) | `== in_dtype` (identity) |
| **excluded `in`** | `FP32R`, `INT64`, `UINT64` | `INT8`, `INT16`, all fp, `FP32R`, all 64-bit |
| **channels** | `1..POOLING_NUM_CHANNELS(128)` | same |

> **NOTE — MAVERICK relaxes the channel-range gate (the NC-v5 tile-aware path).** On MAVERICK the
> validator uses `has_valid_active_channel_range_with_tile(num_active_channels, POOLING_NUM_CHANNELS,
> i.s4d4_tsm.header.inst_flags)` (the `inst_flags`-driven tile relaxation), whereas
> sunda/cayman/mariana use the plain `has_valid_active_channel_range(…, POOLING_NUM_CHANNELS)`. This is the
> **only** validator-text divergence; the struct and dtype rules are byte-identical.
> `[HIGH/OBSERVED — `mariana`↔`maverick` `s4d4_tsm.h` diff; v5 interior INFERRED]`

The arith fp-hub vs bitvec integer-container split is the family-wide dtype rule ([`tensor-scalar.md`](tensor-scalar.md)
§6d): arith = fp-hub through fp32; bitvec = integer-container raw-bits identity. `[HIGH/OBSERVED]`

---

## 8. The DVE dispatch — the "S:" self-name LOG thunk (registered, but body-less)

The family dispatches through the DVE SEQ-ASCII `"S:"` surface (not the `Q7_POOL` `kernel_info_table`). The
PtrMulti self-name strings are **present in the shipped binary** — `strings` on
`libnrtucode_internal.so` (sha256 `b7c67e89…`) shows them sitting **immediately adjacent to** the
[`ImmLd`](ts-immld.md) self-names (the loader/consumer pair physically adjacent in the string pool):

```text
S: TensorScalarImmLdArith
S: TensorScalarImmLdBitvec
S: TensorScalarPtrMultiArith        <- 0x4F
S: TensorScalarPtrMultiBitvec       <- 0x5F
```

Per-gen DVE-DRAM self-name VAs (xtensa-elf-strings on the carved DVE DEBUG DRAM):

| gen | `PtrMultiArith` | `PtrMultiBitvec` | context |
|-----|-----------------|------------------|---------|
| CAYMAN | `0x27c0` | `0x27de` | after `ImmLd`, before `TensorScalarSelect` |
| MARIANA | `0x28a0` | `0x28be` | `ImmLd`@`0x283f`/`0x285a`; `Select`@`0x2921` |
| MAVERICK | `0x28d0` | `0x28ee` | same block layout |

`[HIGH/OBSERVED — binary strings + the SX-FW-58 carve]`

### 8a. The wiring verdict — "stub-wired" (the `// n` middle state, decoded)

The opcodes occupy the **middle state** the maintenance flag names: the kaenadve toolchain **emits** a
self-naming worker and **registers** it (string + thunk + registration stub on CAYMAN/MARIANA/MAVERICK), but
the worker **body is a LOG-only announce-and-return thunk** with **no compute datapath**. Each MARIANA thunk
body is only:

```asm
caac:  entry  a1, 32                      ; (shared frame: the six "// n" thunks share ONE 0x1F4-byte frame)
cab8:  const16 a10, 8                     ; LOG level
       const16 a10, 0x28a0               ; -> "S: TensorScalarPtrMultiArith"
       call8   0x188a4                    ; the family LOG routine
       …                                  ; FLIX-desync tail -> retw.n  (NO config-write, NO alu_op compute chain)
```

The MARIANA thunk funcVAs (`0xcaac` Arith / `0xcad4` Bitvec) are registered into the DVE `kernel_info` slot
via the standard stub template (`const16 a2,funcVA ; s32i a2,[a1+12] ; … ; call8 0x9920`) at stub VAs
`0x1f7f`→`0xcaac` and `0x1f9b`→`0xcad4`. The **same** 53-entry stub table also registers the **maintained**
workers (`0xa040` TensorScalar, `0xa298` TensorScalar-PTR, `0xa7b0` CacheReduce, `0xacbc` CacheCumulative) —
cross-validating the carve. `[HIGH/OBSERVED — LOG loaders + const16 VAs + stub edges; the opcode→descriptor
`l32r` literal is out-of-carve, LOW]`

So PtrMulti is **byte-distinct** from a deprecated-but-**working** op
(`TensorScalarPtr` `0x44`/`0x54`, which has a full worker `0xa298`/`0xa310`, ~hundreds of bytes) **and** from
an unwired-absent op. The runtime can *dispatch* the opcode (the slot is occupied) but the op performs **no
real work** — the literal meaning of "exists, not maintained/used." `[thunk-vs-full-worker body-size contrast
HIGH/OBSERVED; "produces no result at runtime" HIGH/INFERRED from the body absence — the exact dispatch-time
behaviour of an unmaintained op (no-op vs fault) is not statically observable, MED]`

> **GOTCHA — the FLIX literal-pool desyncs the thunk region.** The thunk table (`0xca5c..0xcc50`) desyncs
> under stock `xtensa-elf-objdump` on the recurring `.byte 0x2f` literal-pool lead byte. The `entry`, the
> `const16` self-name loaders, the `call8 0x188a4`/`0x9920` edges, and the stub funcVA `const16`s are
> byte-**clean**; the desynced `.byte`/spurious bundles between LOG calls are **not** real instructions. The
> stub verdict rests only on the byte-clean facts. `[HIGH/OBSERVED]`

---

## 9. Per-generation presence

| gen | NC | `0x4F`/`0x5F` | `S4D4_TSM` | W cap | Dual `0x87`/`0x88` | DVE "S:…PtrMulti" thunk | wired? |
|-----|----|--------------|-----------|-------|--------------------|-------------------------|--------|
| **SUNDA** | v2 | defined `// n` | 64 B (id) | 1..8 (+Dual 1..4) | **DEFINED `// n`** | (no DVE DEBUG img carved) | stub-defined |
| **CAYMAN** | v3 | defined `// n` | 64 B (id) | 1..8 | **RETIRED** | DRAM `0x27c0`/`0x27de` | stub-wired |
| **MARIANA** | v4 | defined `// n` | 64 B (id) | 1..8 | **RETIRED** | DRAM `0x28a0`/`0x28be` | stub-wired |
| **MAVERICK** | v5 | defined `// n` | 64 B (id) | 1..8 | **RETIRED** | DRAM `0x28d0`/`0x28ee` | stub-wired |

- Opcode values, the maintenance flag, and the `S4D4_TSM` struct (`sizeof 64` + every offset) are
  **byte-identical** on all four gens (my compile output, §4). `[HIGH/OBSERVED]`
- The stub-wired DVE thunks + registration stubs are present on every DVE-equipped gen (CAYMAN/MARIANA/
  MAVERICK). `[HIGH/OBSERVED]`
- **SUNDA-only Dual** (NC-v2): the twin pair `0x87`/`0x88`
  (`TensorScalarPtrMultiDual{Arith,Bitvec}`) is defined in `sunda/common.h` and documented in
  `sunda/s4d4_tsm.h` ("merges up to **FOUR** regular TensorScalarPtr instructions (**dual** ALU op per
  instruction)", `has_valid_src_slices_tsm_dual` → `num_elem[3] ∈ [1,4]`). On NC-v3+ the Dual opcode names,
  the Dual struct doc, and `has_valid_src_slices_tsm_dual` are **all gone** — only the singular `0x4F`/`0x5F`
  (W≤8) survive (`0x87` reused as `UPDATE_MODE_SEM_SUB_REG_READ`). The `struct2opcode` JSON still
  **stale-lists** Dual on every gen (§3a). Details: [`sunda-dual-tensorscalarptr.md`](sunda-dual-tensorscalarptr.md).
  `[HIGH/OBSERVED — `common.h` + `s4d4_tsm.h` + JSON diff, all 4 gens]`
- **MAVERICK** (NC-v5): the `has_valid_active_channel_range_with_tile` channel relaxation (§7), else
  byte-identical. v5 interiors are header-OBSERVED only. `[HIGH/OBSERVED header; v5 interior INFERRED]`

---

## 10. Family-close synthesis — the `TensorScalar*` family, side by side

With PtrMulti decoded, the `TensorScalar*` family is **fully decoded**. Every opcode is an elementwise
"tensor (op) scalar" kernel sharing the DVE `"S:"` dispatch surface and the splat+AluOp datapath
([`tensor-scalar.md`](tensor-scalar.md) §5), but fanning across **four operand structs** and **five scalar-
source / accumulator / multi-scalar** variations:

| opcode(s) | name | maint | struct | scalar source | distinguishing semantic |
|-----------|------|-------|--------|---------------|-------------------------|
| `0x43`/`0x53` | `TensorScalar{Arith,Bitvec}` | `// Y` | `S3D3_TS` (64B) | `imm0`/`imm1`, per-imm `{Inst,Ptr,RegPtr}` | **the base**: `out = op1(op0(src, s0), s1)`; two AluOps; 4-way reverse; accum Idle |
| `0x44`/`0x54` | `TensorScalarPtr{Ar,Bv}` | `// n` | `S3D3_TS` (64B) | `imm0`/`imm1` **forced Ptr** | deprecated but **WORKING**: scalar always a `PartitionOffset` pointer (full worker `0xa298`) |
| **`0x4F`/`0x5F`** | **`TensorScalarPtrMulti{Ar,Bv}`** | **`// n`** | **`S4D4_TSM` (64B)** | **W (1..8) ImmLd-preloaded flops** | **MULTI: 4D `[W,Z,Y,X]`; SINGLE op; a new scalar per W-slice (`dst[i]=src[i] op scalar[i]`); banked 4+4 across two ALU stages; reverse ∈ {None,Both}; STUB-WIRED** |
| (SUNDA `0x87`/`0x88` Dual, retired) | `…Dual{Ar,Bv}` | `// n` | `S4D4_TSM` | W (1..4) **PAIRS** | NC-v2 only: **dual** ALU op, W≤4 scalar pairs ([`sunda-dual-tensorscalarptr.md`](sunda-dual-tensorscalarptr.md)) |
| `0x70`/`0x71` | `TensorScalarImmLd{Ar,Bv}` | `// n` | `S2_BN` (64B) | (it **IS** the loader) | **LOAD op: src-only, no dst/op**; stages 1..8 per-channel scalars into DVE flops **for PtrMulti**; Arith=convert-to-fp32 / Bitvec=raw-bits zero-ext ([`ts-immld.md`](ts-immld.md)) |
| `0x98` | `TensorScalarSelect` | `// Y` | `S3D3_TS_SELECT` | predicate + 2 scalars | SELECT/blend: `out = pred ? a : b` (predicate-driven scalar choice) |
| `0x9a` | `TensorScalarCacheReduce` | `// Y` | `S3D3_TS` | `imm0` scalar / `imm1` seed | REDUCE: `acc = acc op1 (src op0 s0)`; writes **final** acc (collapse) |
| `0xe6` | `TensorScalarCacheCumulative` | `// Y` | `S3D3_TS` | `imm0` scalar / `imm1` seed | SCAN: writes **running** acc at each step (inclusive prefix scan); twin of `0x9a` |

**The five axes that distinguish the family (the closing synthesis):**

1. **Scalar source** — inline-immediate (`0x43`/`0x53`), forced-pointer (`0x44`/`0x54`),
   **multi-pointer-preloaded vector** (`0x4F`/`0x5F` via `ImmLd`), select-predicate (`0x98`), or
   N/A-it-IS-the-loader (`0x70`/`0x71`).
2. **Scalar count** — one-or-two (`imm0`/`imm1` of `S3D3_TS`) vs a **vector of 1..8** (`S4D4_TSM` W-dim +
   `ImmLd` preload) — the "Multi" axis is **unique** to PtrMulti + `ImmLd`.
3. **Op composition** — two AluOps `op0`+`op1` (`0x43`/`0x53`/`0x9a`/`0xe6`) vs **single op "2nd-bypass"**
   (`0x4F`/`0x5F`) vs select/blend (`0x98`) vs no-op-it's-a-load (`0x70`/`0x71`).
4. **Accumulator** — none/pure-elementwise (`0x43`/`0x53`/`0x4F`/`0x5F`/`0x98`) vs the `ACCUM_CMD` cache
   (`0x9a` collapse / `0xe6` scan) — the "Cache" axis is **unique** to CacheReduce/CacheCumulative.
5. **Struct dim** — 3D `TENSOR3D` (`S3D3_TS` family) vs **4D `TENSOR4D`** (`S4D4_TSM`, the W scalar-index
   axis) vs src-only (`S2_BN`) — the 4D axis is **unique** to PtrMulti.

**Arith-vs-Bitvec** is the orthogonal sub-split present on every compute member (op-set + dtype:
arith = general-arith / fp-hub-fp32; bitvec = bitvec-ops / int-only-raw-bits), realised as an **op-class
gate** on the op-bearing structs (`S3D3_TS`, `S4D4_TSM`) and as a **load-dtype convert-vs-rawbits** choice on
the op-less `S2_BN` [`ImmLd`](ts-immld.md). **Maintenance**: the base `0x43`/`0x53`, Select `0x98`,
CacheReduce `0x9a`, CacheCumulative `0xe6` are `// Y`; the working Ptr `0x44`/`0x54` and the **stub-wired**
PtrMulti `0x4F`/`0x5F` + `ImmLd` `0x70`/`0x71` are `// n`. **PtrMulti + `ImmLd` are the deprecated multi-scalar
macro-pair that CLOSES the family.** `[HIGH/OBSERVED — joined from the family reports' verbatim opcodes +
struct2opcode + the compile-verified structs + the DVE self-name/worker evidence]`

---

## 11. Honesty ledger

**HIGH / OBSERVED** (direct disasm / byte / header / compile-output):

- Opcodes `0x4F`(Arith) / `0x5F`(Bitvec), `// n, ucode/kaenadve exists, not maintained/used`,
  byte-identical on all 4 gens (`common.h` verbatim); the SUNDA-only Dual `0x87`/`0x88` + the `0x87`-reuse
  (`UPDATE_MODE_SEM_SUB_REG_READ`) on NC-v3+.
- `S4D4_TSM` binds `0x4F`/`0x5F` (+ stale Dual) in `struct2opcode`; the 64-B struct (src `TENSOR4D`@12 /
  dst @44, in/out dtype @32/@33, num_chan @34, **single** `op`@36, `op_dim`@37, `reverse`@38; **no** imm
  fields) — **my own gcc `offsetof`/`sizeof` output, byte-identical all 4 gens**.
- The "PtrMulti" semantics: `for i in range(W): dst[i]=src[i] op scalar[i]`, `W=num_elem[3]∈[1,8]`, single
  op, scalar-swap-per-XYZ, ImmLd-preloaded scalars (Arith←ImmLdArith / Bitvec←ImmLdBitvec), `op_dim==XYZ`,
  `same_element_count`, `reverse∈{None,Both}` + the verbatim "two ALU stages, 4+4 scalars" hardware note —
  all verbatim `s4d4_tsm.h`, all 4 gens.
- The Arith-vs-Bitvec split: `tensor_scalar_ptr_multi_valid_ops` (arith = `is_general_arith_op` / bitvec =
  `is_bitvec_op` incl. `Crc32`, both minus `Div`/`Pow`/`Mod`) + `_valid_types` (arith = any-valid-fp-hub /
  bitvec = `in==out ∈ {u8,u16,u32,i32}` raw-bits) — verbatim, all 4 gens.
- The DVE self-name strings present in the shipped binary (sha256 `b7c67e89…`), adjacent to the `ImmLd`
  names; per-gen DVE-DRAM VAs (CAY `0x27c0`/`0x27de`, MAR `0x28a0`/`0x28be`, MAV `0x28d0`/`0x28ee`); the
  registered LOG-only thunks (MAR funcVA `0xcaac`/`0xcad4`, stubs `0x1f7f`/`0x1f9b` via `call8 0x9920`) —
  the `// n` stub-wired state; MAVERICK tile-aware channel-range relaxation.

**HIGH / INFERRED**:

- "Stub-wired produces no result at runtime" — HIGH from the body absence (LOG-only thunk, no compute
  chain), but the **exact** dispatch-time behaviour of an unmaintained op (silent no-op vs fault) is not
  statically observable.

**MED / INFERRED**:

- The on-device per-element compute of PtrMulti **specifically**: the op ships as a LOG-only stub, so there
  is no compute body to byte-walk. The fold algorithm (§5b) is read from the header (HIGH); its bind to a
  concrete on-device datapath is **family-level** (the shared `alu_op.cpp` single-op `(R,imm)` arm + the
  splat datapath of [`tensor-scalar.md`](tensor-scalar.md) §5) — MED for **this** opcode.
- The SUNDA Dual's on-struct realisation (how a 2nd op is expressed on the single-`op` `S4D4_TSM` struct) is
  not pinned; the Dual is SUNDA-only and retired, reported at header level (W≤4 scalar-PAIR cap + "dual ALU
  op" doc). See [`sunda-dual-tensorscalarptr.md`](sunda-dual-tensorscalarptr.md).

**LOW / UNRECOVERED**:

- The `0x4F`/`0x5F` → funcVA descriptor bytes (the stub `l32r` literals resolve out-of-carve;
  runtime-bound).

**FLIX-DESYNC FLAG**: the thunk region (`0xca5c..0xcc50`) desyncs under stock objdump on the `.byte 0x2f`
literal-pool lead byte. The `entry`, the `const16` string loaders, the `call8` LOG/register edges, and the
stub funcVA `const16`s are byte-clean; the desynced bundles are **not** reported as real instructions.

---

## 12. See also

- [`tensor-scalar.md`](tensor-scalar.md) — the family base (`0x43`/`0x53`/`0x44`/`0x54`): the
  splat+two-AluOp datapath, the `S3D3_TS` struct, the AluOp accept set (the canonical reference), and the
  full family-close synthesis (§10).
- [`ts-immld.md`](ts-immld.md) — `TensorScalarImmLd` (`0x70`/`0x71`): **the loader** that preloads
  PtrMulti's 1..8 scalars into the DVE flops (the producer in the producer/consumer macro-pair).
- [`sunda-dual-tensorscalarptr.md`](sunda-dual-tensorscalarptr.md) — the **SUNDA-only Dual** predecessor
  (`0x87`/`0x88`, W≤4 scalar pairs, dual ALU op) retired on NC-v3+.
- [`scalar-tensor-tensor.md`](scalar-tensor-tensor.md) — the `is_general_arith_op` (17) /
  `is_general_bitvec_op` (9) accounting reference (note PtrMultiBitvec uses the **10-op** `is_bitvec_op`, not
  the 9-op `is_general_bitvec_op`).
- [`tensor-tensor.md`](tensor-tensor.md) — the two-tensor twin sharing the `alu_op.cpp` per-lane datapath.
