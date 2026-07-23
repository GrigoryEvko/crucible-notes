# CopyPredicatedReduce — `SelectReduce` (opcode `0xea`): the predicated-copy-with-reduce twin of CastPredicated

**CopyPredicatedReduce** is the **device-firmware name** for the ISA opcode **`SelectReduce` = `0xea`** — a
**predicated, dtype-PRESERVING copy fused with a masked REDUCE-MAX**. It is the reduce-bearing member of the
GPSIMD *predicated-op family*: the same per-lane `src0` integer predicate, the same `bitkillt` write-enable
merge, and the same DVE (Vector) datapath as [CastPredicated](castpredicated.md) (`0x99`) and CopyPredicated
(`0x72`), but it **adds** a second ALU op (`op1 == Max`) and a PSUM **accumulator** (`accumulator_cmd`) that
fold the predicate-TRUE lanes into a running maximum. The public arch-isa C header names the opcode
`SelectReduce`; the carved DVE firmware self-names the kernel `S: CopyPredicatedReduce` — they are the **same
instruction**, proven by the validator field `has_valid_s2s2d2_cpr_accum_cmd` (**CPR** = **C**opy-**P**redicated-**R**educe).

Everything below is derived from static analysis of the shipped GPSIMD customop package
(`aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`): the in-package arch-isa C interface headers under
`custom_op/c10/include/neuron_<arch>_arch_isa/tpb/` (`aws_neuron_isa_tpb_s2s2d2_stt.h`,
`aws_neuron_isa_tpb_common.h`, `instruction_mapping.json`), the host ucode library
`custom_op/c10/lib/libnrtucode_internal.so` (the DVE/POOL ucode blob store, read with `nm -S`/`readelf`/`rg
-ba`), the `libfiss-base.so` ISS value-oracle from the shipped `gpsimd_tools` (`ncore2gp`), and a struct
compile-verify against the headers. Reads use the shipped Cadence `xtensa-elf-objdump`/`readelf`
(`XTENSA_CORE=ncore2gp`) for device images and stock binutils/`gcc`/`ctypes` for the x86-64 host
libraries. Every claim carries a confidence tag `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

---

## 1. Executive summary — the verdict, and the cross-page resolution up front

> **CROSS-PAGE RESOLUTION — opcode `0xea` = `SelectReduce` = CopyPredicatedReduce, and it binds
> `S2S2D2_STT`.** The committed [tensor-tensor-scan.md (#712)](tensor-tensor-scan.md) and
> [scalar-tensor-tensor.md (#713)](scalar-tensor-tensor.md) both flagged that the **4th `struct2opcode`
> member** of `NEURON_ISA_TPB_S2S2D2_STT_STRUCT` is opcode `0xea` (which #713 provisionally called
> *"SelectReduce"*). **This page confirms it byte-exact.** `instruction_mapping.json` (all
> four gens, identical) lists `S2S2D2_STT_STRUCT → [ ScalarTensorTensorArith, ScalarTensorTensorBitvec,
> TensorTensorScanArith, SelectReduce ]` — and the `aws_neuron_isa_tpb_common.h` opcode enum assigns those
> four members exactly **`0x9d`, `0x9e`, `0xe5`, `0xea`**. So the `S2S2D2_STT` binding squares to a closed
> 4-tuple, and `0xea`/SelectReduce/CopyPredicatedReduce is its 4th leg. **The reconcile flag is closed.**
> `[HIGH/OBSERVED]`

The four-member binding, opcode-resolved (`aws_neuron_isa_tpb_common.h` CAYMAN; identical SUNDA/MARIANA/
MAVERICK, §10):

| `struct2opcode` slot | opcode enum | hex | name on this struct | authored page |
|---|---|---|---|---|
| 1 | `SCALAR_TENSOR_TENSOR_ARITH` | **`0x9d`** | ScalarTensorTensorArith | [scalar-tensor-tensor.md](scalar-tensor-tensor.md) (#713) |
| 2 | `SCALAR_TENSOR_TENSOR_BITVEC` | **`0x9e`** | ScalarTensorTensorBitvec | [scalar-tensor-tensor.md](scalar-tensor-tensor.md) (#713) |
| 3 | `TENSOR_TENSOR_SCAN_ARITH` | **`0xe5`** | TensorTensorScan | [tensor-tensor-scan.md](tensor-tensor-scan.md) (#712) |
| 4 | **`SELECT_REDUCE`** | **`0xea`** | **SelectReduce / CopyPredicatedReduce** | **THIS page** |

CopyPredicatedReduce is, mechanically:

* **The PREDICATE** is `src0` (`dtype_lo`), an **integer mask** — the *same* 6-int predicate set as
  CastPredicated / CopyPredicated: `{INT8, INT16, INT32, UINT8, UINT16, UINT32}` via
  `is_valid_int_dtype_datapath`.
* **The COPY** is **dtype-PRESERVING**: `op0 == AluOp::Bypass` (`0x00`) — no arithmetic, **no cast** on the
  data value. This is the exact contrast with CastPredicated, which converts dtype. The data `src1`
  (`dtype_hi`) may be any datapath dtype **except a 32-bit int** (`!is_valid_32b_int_dtype`).
* **The REDUCE** is the new content: `op1 == AluOp::Max` (`0x08`) — a **masked reduce-MAX**, seeded by an
  `accumulator_cmd ∈ {Idle(0), ZeroAccumulate(3), Accumulate(2)}`. Only predicate-TRUE lanes contribute to
  the maximum (the *masked* reduction), realized on the DVE datapath by the predicate-gated NaN-suppressing
  reduce-max ivp primitive `ivp_rbmaxnumn_2xf32t` (the `_t` / `_64` vbool-gated variant).
* It runs **HARDWARE-NATIVE on the DVE (Vector) engine** — *not* a Q7-POOL software kernel — co-resident in
  the same 4 DVE DEBUG blobs as CastPredicated, at byte-exact offsets `0x18e16a` / `0x42844a` / `0x6f016a`
  / `0x8b0552` in `libnrtucode_internal.so`.
* The operand struct is **`NEURON_ISA_TPB_S2S2D2_STT_STRUCT`** (64 B, compile/header-verified) — **not** the
  `S3S3D3_TT` of CastPredicated/CopyPredicated. The reduce *required* the `op1` and `accumulator_cmd` fields
  that `S3S3D3_TT` does not have (§3).

> **CORRECTION vs the naïve reading of the predicated-op family.** There is **NO**
> `NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_REDUCE` opcode in *any* of the four `common.h` files. A full scan
> of the four opcode enums for `COPY_PREDICATED*` / `*PREDICATED_REDUCE` returns **only**
> `{COPY_PREDICATED 0x72, CAST_PREDICATED 0x99, COPY_PREDICATED_SCALAR 0xe8}` — **none ends in `REDUCE`**.
> The device kernel name "CopyPredicatedReduce" therefore maps to the ISA opcode `SelectReduce 0xea`, and
> the kernel lives on a **different struct** (`S2S2D2_STT`) from the rest of the family (`S3S3D3_TT`). Any
> page expecting a `0xea` member of the `S3S3D3_TT` predicated-copy family is wrong: `S3S3D3_TT_STRUCT →
> {COPY_PREDICATED 0x72, CAST_PREDICATED 0x99}` only (two members, both no-reduce).

> **CORRECTION vs the `REDUCE_OP` model of [Tensor-Reduce](tensor-reduce.md).** CopyPredicatedReduce does
> **not** use the closed 6-entry `NEURON_ISA_TPB_REDUCE_OP` enum (`{ADD0, AVERAGE1, MAX2, BITWISE_OR3,
> BITWISE_AND4, BITWISE_XOR5}`) that the cross-partition Tensor-Reduce (`0x7c`/`0x7d`, `clr_reduce_local`)
> switches on. Its reduce operator is **hard-fixed to MAX** by the validator (`op1 == AluOp::Max`, an
> `AluOp` from the *general-arith* space, not a `REDUCE_OP` code). So CopyPredicatedReduce is the **masked
> reduce-MAX specialization** — one operator, predicate-gated — *not* a configurable reduce. Two different
> reduce mechanisms: `REDUCE_OP` (6-way, `S4D4_CR`, POOL) vs `op1==Max` (fixed, `S2S2D2_STT`, DVE).

---

## 2. Opcode & enum — the name-mapping discovery (byte-exact, all 4 gens)

The opcode is defined byte-identically in all four arch-isa generations:

```
NEURON_ISA_TPB_OPCODE_SELECT_REDUCE = 0xea,   // Y
  aws_neuron_isa_tpb_common.h:
    neuron_sunda_arch_isa    :296   = 0xea  // Y   (NC-v2)
    neuron_cayman_arch_isa   :300   = 0xea  // Y   (NC-v3)
    neuron_mariana_arch_isa  :310   = 0xea  // Y   (NC-v4)
    neuron_maverick_arch_isa :316   = 0xea  // Y   (NC-v5)
```

The `// Y` annotation marks the opcode **actively maintained** in this ISA revision. The family
neighborhood (CAYMAN `common.h`):

```
:206  COPY_PREDICATED          = 0x72,   // Y   (predicated copy, no reduce — the family sibling)
:232  CAST_PREDICATED          = 0x99,   // Y   (predicated cast, no reduce — the twin, castpredicated.md)
:236  SCALAR_TENSOR_TENSOR_ARITH  = 0x9d, // Y  (S2S2D2_STT member #1)
:237  SCALAR_TENSOR_TENSOR_BITVEC = 0x9e, // Y  (S2S2D2_STT member #2)
:295  TENSOR_TENSOR_SCAN_ARITH = 0xe5,   // Y   (S2S2D2_STT member #3, tensor-tensor-scan.md)
:298  COPY_PREDICATED_SCALAR   = 0xe8,   // Y   (scalar-src predicated copy, copypredicatedscalar.md)
:300  SELECT_REDUCE            = 0xea,   // Y   <-- THIS (device name CopyPredicatedReduce, S2S2D2_STT member #4)
```

### 2.1 The device-name ↔ ISA-opcode binding (the defining evidence)

```
device self-name  "S: CopyPredicatedReduce\n"  (x4, in the DVE DEBUG blobs)
        ==        ISA opcode  SELECT_REDUCE 0xea
```

The proof is structural, not a string match: `is_valid_select_reduce()`
(`aws_neuron_isa_tpb_s2s2d2_stt.h`, the validator) uniquely calls **`has_valid_s2s2d2_cpr_accum_cmd(i)`**.
The token **`cpr`** = **C**opy-**P**redicated-**R**educe. No other instruction's validator references any
`*_cpr_*` field — the accumulator-command gate is the device semantic baked into the validator name. The
firmware names the kernel by its *semantic* (a predicated copy + a reduce); the public ISA names the opcode
by its *mechanism* (a select [predicate] + reduce). Same instruction.

> **GOTCHA — `"SelectReduce"` is NOT a device self-name.** `rg -ba 'SelectReduce'
> libnrtucode_internal.so` returns **count = 0**. The firmware *only* names the kernel
> `CopyPredicatedReduce` (count = 4). If you search the carved ucode for the ISA opcode name you will find
> nothing; search for the *device* name. The reverse holds for the headers: `rg 'CopyPredicatedReduce'`
> over the `arch_isa/` headers returns nothing — they say `SelectReduce`.

### 2.2 Operand-struct binding (`instruction_mapping.json`, all 4 gens)

```json
"struct2opcode": {
  "NEURON_ISA_TPB_S2S2D2_STT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_ARITH",     // 0x9d
    "NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_BITVEC",    // 0x9e
    "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH",       // 0xe5
    "NEURON_ISA_TPB_OPCODE_SELECT_REDUCE"                   // 0xea  <-- THIS
  ],
  "NEURON_ISA_TPB_S3S3D3_TT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_COPY_PREDICATED",                // 0x72
    "NEURON_ISA_TPB_OPCODE_CAST_PREDICATED"                 // 0x99
  ],
  "NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_SCALAR"          // 0xe8
  ],
  ...
}
```

Verified `jq`-exact, all four gens, byte-identical:
`jq -c '.struct2opcode["NEURON_ISA_TPB_S2S2D2_STT_STRUCT"]' instruction_mapping.json` →
`["...SCALAR_TENSOR_TENSOR_ARITH","...SCALAR_TENSOR_TENSOR_BITVEC","...TENSOR_TENSOR_SCAN_ARITH","...SELECT_REDUCE"]`.
SELECT_REDUCE is **1 of 4** opcodes on the scalar-tensor-tensor struct, and the **only** struct that
references it (`jq -r '.struct2opcode | to_entries[] | select(.value|index("...SELECT_REDUCE")) | .key'`
returns exactly `NEURON_ISA_TPB_S2S2D2_STT_STRUCT`, nothing else). The three predicated-copy structs are
disjoint: no opcode appears on two structs.

The header self-documents the same four-tuple at the top of `aws_neuron_isa_tpb_s2s2d2_stt.h`:

```c
// s2s2d2_stt struct is used for the following instruction opcodes:
// - `ScalarTensorTensorArith`
// - `ScalarTensorTensorBitvec`
// - `TensorTensorScan`
// - `SelectReduce`
```

This header comment is the human-readable mirror of the `struct2opcode` JSON — both independently confirm
the 4th member.

---

## 3. Why `S2S2D2_STT` (not `S3S3D3_TT`) — the struct-family rationale

CastPredicated / CopyPredicated ride `NEURON_ISA_TPB_S3S3D3_TT_STRUCT`: a pure two-tensor predicated copy/
cast with a **single** ALU op field. Reading `aws_neuron_isa_tpb_s3s3d3_tt.h` byte-exact:

```c
// S3S3D3_TT (the CastPredicated/CopyPredicated struct):
    NEURON_ISA_TPB_ALU_OP   op;   // 1  (offset 14)   <-- ONE op field, forced Bypass
//  ... NO op1, NO accumulator_cmd, NO reduce field anywhere
```

`rg -ic 'accumulator|accum_cmd|op1|reduce' aws_neuron_isa_tpb_s3s3d3_tt.h` → **0**. The single `op@14` is
all `S3S3D3_TT` carries — there is no second op and no accumulator. CopyPredicatedReduce **needs both**: a
second op (`op1`) to name the reduce operator, and an `accumulator_cmd` to seed/continue the running
maximum. So the kernel was placed on the richer **`S2S2D2_STT`** struct (shared with ScalarTensorTensor and
TensorTensorScan), which supplies `op0`, `op1`, `accumulator_cmd`, `reverse_operands`, and an immediate
(`imm0`) inherited from its scalar-tensor-tensor cousins. **This is precisely why the device kernel name
does not appear among the `S3S3D3_TT` opcodes** — it lives on a different struct under the ISA name
`SelectReduce`. `[HIGH/OBSERVED — both struct headers + the mapping JSON]`

---

## 4. Dispatch surface — DVE, not POOL

CopyPredicatedReduce is a **hardware-native DVE (Vector) instruction**, not a Q7-POOL software ucode kernel.
Two independent proofs, both following the established blob-multiplicity rule
(16 copies ⇒ iTPB/SEQ, 4 copies ⇒ DVE, 3 copies ⇒ ACT/PE/POOL):

**(i) POOL-absence.** A whole-binary scan of `libnrtucode_internal.so` for `select_reduce` /
`cpr_accum` / `_cpr_` returns **count = 0** for all (`rg -ba`); there is no POOL `.xt.prop` worker, no
`pool_*reduce*` accessor, no kernel_info_table entry. The CrossLaneReduce POOL family (`pool_cross_lane_
reduce_arith/bitvec`, `clr_reduce_local`) is the *only* reduce in the POOL ucode — CopyPredicatedReduce is
not there.

**(ii) DVE-presence — the self-name tag x4.** `"S: CopyPredicatedReduce\n"` appears **exactly 4 times** in
`libnrtucode_internal.so`, each inside a per-gen `NX_DVE_DEBUG_DRAM` blob (containment confirmed via `nm -S`
symbol ranges):

| device offset | DVE DEBUG blob (`nm -S` symbol) | symbol range | gen |
|---|---|---|---|
| `0x18e16a` | `CAYMAN_NX_DVE_DEBUG_DRAM_get.data` | `0x18b320 .. 0x192080` | CAYMAN (v3) |
| `0x42844a` | `MARIANA_NX_DVE_DEBUG_DRAM_get.data` | `0x425520 .. 0x42c520` | MARIANA (v4) |
| `0x6f016a` | `MARIANA_PLUS_NX_DVE_DEBUG_DRAM_get.data` | `0x6ed240 .. 0x6f43a0` | MARIANA_PLUS |
| `0x8b0552` | `MAVERICK_NX_DVE_DEBUG_DRAM_get.data` | `0x8ad5c0 .. 0x8b3540` | MAVERICK (v5) |

All four offsets fall strictly inside their `_DVE_DEBUG_DRAM_get.data` blob — containment verified, not
inferred. The multiplicity (4) is the DVE-family signature, byte-identical to CastPredicated's 4 self-name
copies.

The co-resident DVE roster (the predicate-select / index / accumulator cluster) sits in the same blobs,
each also ×4 (`rg -o 'S: <name>' | sort | uniq -c`):

```
S: MatchReplace        x4    S: TensorScalarSelect   x4    S: CopyPredicatedScalar x4
S: CopyPredicated      x4    S: CastPredicated       x4    S: TensorTensorScan     x4
S: CopyPredicatedReduce x4   S: RangeSelect          x4    S: DveReadAccumulator   x4
S: DveReadIndices      x4    S: FindIndex8           x4    S: EngineNop            x13
```

CopyPredicatedReduce sits in the same table as CastPredicated / CopyPredicated / RangeSelect /
TensorScalarSelect (the predicate-select cluster) **and** `DveReadAccumulator` — the accumulator-readout
sibling that reads back this kernel's reduce result (§7).

> **NOTE — counting `CopyPredicated`.** A naïve `rg -c 'CopyPredicated'` returns **12**, not 4, because
> `CopyPredicated` is a substring of `CopyPredicatedReduce` and `CopyPredicatedScalar`. The true bare
> `CopyPredicated` count is 4 (12 = 4 + 4 + 4). Always anchor counts to the full self-name string
> (`S: CopyPredicatedReduce\n`), never a substring.

**Verdict:** CopyPredicatedReduce dispatches on the **DVE engine, hardware-native**.

---

## 5. The operand struct — `NEURON_ISA_TPB_S2S2D2_STT_STRUCT` (64 B)

Source: `aws_neuron_isa_tpb_s2s2d2_stt.h` (CAYMAN, "ISA header for NC-v3"). Read byte-exact, lines 24–43,
with the in-comment offset annotations:

| offset | size | field | type | meaning for CopyPredicatedReduce |
|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `header.opcode == 0xea` lives here |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | sync/event bits |
| 12 | 12 | `src0_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | **PREDICATE** mask tensor (read; `dtype_lo`) |
| 24 | 12 | `src1_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | **DATA** tensor (read; `dtype_hi` — the copied value) |
| 36 | 1 | `op0` | `NEURON_ISA_TPB_ALU_OP` | **COPY** op == `AluOp::Bypass` (`0x00`); no arith/cast |
| 37 | 1 | `op1` | `NEURON_ISA_TPB_ALU_OP` | **REDUCE** op == `AluOp::Max` (`0x08`); reduce-MAX |
| 38 | 1 | `reverse_operands` | `NEURON_ISA_TPB_TENS_SCALAR_REV_OPS` | `None(0)` \| `First(1)` only |
| 39 | 1 | `imm0_src` | `NEURON_ISA_TPB_IMM_SRC` | immediate source (constrained; §6) |
| 40 | 1 | `in0_in1_dtype` | `NEURON_ISA_TPB_DTYPE_PAIR` | `dtype_lo` = `src0` PREDICATE, `dtype_hi` = `src1` DATA |
| 41 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | OUTPUT / accumulator dtype (FP32R `0xB` allowed) |
| 42 | 1 | `num_active_channels` | `uint8_t` | active partitions |
| 43 | 1 | `imm0_dtype` | `NEURON_ISA_TPB_DTYPE` | FP32 for the non-bitvec path |
| 44 | 1 | `accumulator_cmd` | `NEURON_ISA_TPB_ACCUM_CMD` | **THE REDUCE SEED**: `Idle(0)` \| `Accumulate(2)` \| `ZeroAccumulate(3)` |
| 45 | 3 | `reserved[3]` | `uint8_t` | must be 0 |
| 48 | 12 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | OUTPUT (write; the reduced/copied result) |
| 60 | 4 | `imm0` | `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` | immediate value field |
| **64** | | | | `ISA_STATIC_ASSERT(sizeof == 64)` (line 43) |

```c
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S2S2D2_STT_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S2S2D2_STT_STRUCT is NOT 64B.");
```

A struct compile-verify (`gcc -I<cayman tpb hdr dir>`, `offsetof`/`sizeof` dump) reproduces the header
annotations exactly: `sizeof = 64`; `header=0 events=4 src0=12 src1=24 op0=36 op1=37 reverse_operands=38
imm0_src=39 in0_in1_dtype=40 out_dtype=41 num_active_channels=42 imm0_dtype=43 accumulator_cmd=44
reserved=45 dst=48 imm0=60`. Every offset matches.

> **NOTE — the dual src-tensor naming.** The struct names its inputs `src0`/`src1` and packs both dtypes in
> one `in0_in1_dtype` byte (`dtype_lo` = `src0`, `dtype_hi` = `src1`). For CopyPredicatedReduce the
> **predicate** rides `src0` (`dtype_lo`, integer-only) and the **data** rides `src1` (`dtype_hi`). This is
> the same predicate-on-the-low-source convention CastPredicated uses on `S3S3D3_TT`.

---

## 6. The validator — `is_valid_select_reduce` (byte-exact)

The full validator is encoded as Rust-style pseudocode comments in `aws_neuron_isa_tpb_s2s2d2_stt.h`
(lines 112–140), reproduced verbatim:

```rust
// fn is_valid_select_reduce(i: Inst) -> bool {
//       has_valid_neuron_header(i)
//    && has_valid_neuron_events(i)
//    && has_select_reduce_opcode(i)                                       // header.opcode == SelectReduce (0xea)
//    && is_valid_dtype(dtype_lo, DtypeAllowFP32R::False)                  // src0 predicate dtype
//    && is_valid_dtype(dtype_hi, DtypeAllowFP32R::False)                  // src1 data dtype
//    && is_valid_dtype(i.s2s2d2_stt.imm0_dtype, DtypeAllowFP32R::False)
//    && is_valid_dtype(i.s2s2d2_stt.out_dtype, DtypeAllowFP32R::True)     // OUT may be FP32R (wide accum)
//    && has_valid_select_reduce_dtypes(i)                                 // §6.1
//    && s2s2d2_stt_imm0_dtype(i)                                          // imm0_dtype == FP32 (non-bitvec)
//    && is_valid_enum(EnumList::AluOp, i.s2s2d2_stt.op0)
//    && is_valid_enum(EnumList::AluOp, i.s2s2d2_stt.op1)
//    && has_valid_select_reduce_op(i)                                     // §6.2  op0==Bypass, op1==Max
//    && has_valid_s2s2d2_stt_immediate(i)
//    && start_addr_active_channels(src0.start_addr, num_active_channels)
//    && start_addr_active_channels(src1.start_addr, num_active_channels)
//    && start_addr_active_channels(dst.start_addr,  num_active_channels)
//    && tensor2d_valid(src0, dtype_lo, WriteTensor::False, PSUM::True, SBUF::True)
//    && tensor2d_valid(src1, dtype_hi, WriteTensor::False, PSUM::True, SBUF::True)
//    && tensor2d_valid(dst,  out_dtype, WriteTensor::True, PSUM::True, SBUF::True)
//    && check_active_channels(i.s2s2d2_stt.num_active_channels)
//    && s2s2d2_stt_src_element_cnt_check(i)                               // src0 elem-count == src1 elem-count
//    && s2s2d2_stt_dst_element_cnt_check(i)                               // src0 elem-count == dst  elem-count
//    && tt_valid_partitions(src0.start_addr, src1.start_addr)
//    && has_valid_s2s2d2_cpr_accum_cmd(i)                                 // §6.3  THE CPR ACCUM GATE
//    && is_valid_enum(EnumList::TensScalarRevOps, i.s2s2d2_stt.reverse_operands)
//    && has_s2s2d2_stt_reserved_zero(i)
//    && has_valid_select_reduce_rev_ops(i)                               // §6.4  None | First only
// }
```

The src/dst element-count checks (`s2s2d2_stt_src_element_cnt_check` /
`s2s2d2_stt_dst_element_cnt_check`) require `src0.num_elem[0]*[1] == src1.num_elem[0]*[1]` and
`src0.num_elem[0]*[1] == dst.num_elem[0]*[1]` — i.e. **one predicate element per data element, one dst slot
per predicate element** (unless the start_addr is register-shaped, which bypasses the static check).

### 6.1 The dtype gate `has_valid_select_reduce_dtypes` (lines 195–198)

```rust
// fn has_valid_select_reduce_dtypes(i: Inst) -> bool {
//       is_valid_int_dtype_datapath(dtype_lo)        // src0 PREDICATE: integer only
//    && !is_valid_32b_int_dtype(dtype_hi)            // src1 DATA: any dtype EXCEPT 32-bit int
// }
```

with the helper sets (`aws_neuron_isa_tpb_common.h`, read byte-exact):

```rust
// is_valid_int_dtype_datapath = { INT8 0x2, INT16 0x4, INT32 0x8, UINT8 0x3, UINT16 0x5, UINT32 0x9 }
// is_valid_32b_int_dtype      = { INT32 0x8, UINT32 0x9 }
```

So the predicate is **exactly** the same 6-int set as CastPredicated / CopyPredicated, and the data dtype
is *any* datapath dtype except a 32-bit int (§9).

### 6.2 The op gate `has_valid_select_reduce_op` (lines 214–219) — the COPY+REDUCE pair

```rust
// fn has_valid_select_reduce_op(i: Inst) -> bool {
//       is_general_arith_op(i.s2s2d2_stt.op0)
//    && is_general_arith_op(i.s2s2d2_stt.op1)
//    && (i.s2s2d2_stt.op0 == AluOp::Bypass)        // the COPY: no arithmetic on the data value
//    && (i.s2s2d2_stt.op1 == AluOp::Max)           // the REDUCE: reduce-MAX
// }
```

`AluOp::Bypass = 0x00`, `AluOp::Max = 0x08` (`common.h:940` / `:948`). `is_general_arith_op` admits both
`Bypass` and `Max` (it excludes only Divide/Pow/Mod/int-aluops/Rsqrt). The op pair is therefore
**hard-constrained**: copy = Bypass, reduce = Max. CopyPredicatedReduce is the masked-reduce-MAX
specialization, *not* a general reduce.

### 6.3 The accumulator gate `has_valid_s2s2d2_cpr_accum_cmd` (lines 243–247) — the **CPR** signature

```rust
// fn has_valid_s2s2d2_cpr_accum_cmd(i: Inst) -> bool {
//       (i.s2s2d2_stt.accumulator_cmd == AccumCmd::Idle)             // 0
//    || (i.s2s2d2_stt.accumulator_cmd == AccumCmd::ZeroAccumulate)   // 3
//    || (i.s2s2d2_stt.accumulator_cmd == AccumCmd::Accumulate)       // 2
// }
```

This is the unique `*_cpr_*` validator — the structural proof that the device "CopyPredicatedReduce" name
maps to opcode `SelectReduce` (§2.1). The full `NEURON_ISA_TPB_ACCUM_CMD` enum (`common.h:778-782`):
`IDLE = 0, ZERO = 1, ACCUMULATE = 2, ZERO_ACCUMULATE = 3, LOAD_ACCUMULATE = 4`. CPR permits exactly **3 of
the 5**: `Idle(0)`, `Accumulate(2)`, `ZeroAccumulate(3)`. Bare `ZERO(1)` and `LOAD_ACCUMULATE(4)` are
**forbidden** for CPR.

> **QUIRK — CPR vs the general STT accum gate.** The sibling `has_valid_s2s2d2_stt_accum_cmd` (used by the
> ScalarTensorTensor opcodes) is *bitvec-aware*: it forces `Idle` for the bitvec opcode and allows
> `{Idle, ZeroAccumulate, Accumulate}` only for arith. CopyPredicatedReduce has its **own** gate
> (`*_cpr_*`) that unconditionally allows `{Idle, ZeroAccumulate, Accumulate}` — there is no bitvec
> CopyPredicatedReduce, so no bitvec branch. Two distinct accum validators on the same struct.

### 6.4 The reverse-ops gate `has_valid_select_reduce_rev_ops` (lines 259–262)

```rust
// fn has_valid_select_reduce_rev_ops(i: Inst) -> bool {
//       (i.s2s2d2_stt.reverse_operands == TensScalarRevOps::None)    // 0
//    || (i.s2s2d2_stt.reverse_operands == TensScalarRevOps::First)   // 1
// }
```

`reverse_operands ∈ {None(0), First(1)}` only — `Second`/`Both` are forbidden. This selects which operand
the predicate/data binding uses; the predicate is always `src0` but `First` swaps the pairing for the
reduce-vs-copy operand roles.

---

## 7. The reduce — `op1 == Max`, predicate-gated, accumulator-seeded (the new content)

### 7.1 The reduce operator — `AluOp::Max` (`0x08`), fixed

Unlike [Tensor-Reduce / CrossLaneReduce](tensor-reduce.md) (a full 6-entry `REDUCE_OP` enum) or
TensorScalarCacheReduce, CopyPredicatedReduce's reduce operator is **hard-fixed to MAX** by the validator
(`op1 == AluOp::Max`, §6.2). It is a **masked reduce-MAX**, not a configurable reduce.

### 7.2 How the predicate gates the reduce — the masked fold `[HIGH/OBSERVED for the primitive]`

Only predicate-**TRUE** lanes contribute to the maximum. On the DVE datapath this is realized by the
predicate-bounded reduce-max ivp primitive. The masked fp32 reduce-max body is OBSERVED in the
`libfiss-base.so` ISS oracle (x86-64, `.text` VMA == file-offset, clean disasm):

```
module__xdref_rbmaxnumn_2xf32t_1_64_32f_512f_64   @0x531310   (fp32, NaN-suppressing, vbool-gated)
  531310:  push r15..rbx                    ; prologue
  531314:  mov  %rdx,%r14                   ; r14 = arg3 = the PREDICATE word pointer
  531329:  mov  (%r14),%r9d                 ; <== LOADS the per-lane predicate word
  ...
  531337:  mov  %r11d,%r12d
  53133a:  shr  $0x17,%eax    ; cmp $0xff,%al ; sete  %dl     ; exp == 0xff  ?
  531342:  test $0x7fffff,%r11d ;            setne %al        ; mantissa != 0 ?
  531352:  and  %eax,%edi                                     ; (exp==0xff && mant!=0) => NaN
  ...        per lane: predicate bit (in r9) gates whether the lane updates the running max;
             lanes whose predicate bit is 0 are SKIPPED. The exp/mantissa tests are the fp32 NaN
             classification — maxnum suppresses NaN (the *num* in rbmaxnum).
```

The naming decodes the gating exactly: the trailing **`t`** (`_2xf32t`) and the **`_64`** word size are the
**vbool-gated** variant. The non-gated sibling `module__xdref_rbmaxnum_n_2x32f_1_64_32f_512f` @0x5274c0
(NO trailing `t`, NO `_64`) takes **no predicate** — the contrast that pins the `t`+`_64` as the predicate
load. The masked-reduce-max family, all OBSERVED via `nm`:

| symbol | VA | dtype / role |
|---|---|---|
| `module__xdref_rbmaxnumn_2xf32t_1_64_32f_512f_64` | `0x531310` | fp32 masked NaN-suppress reduce-max (the body above) |
| `module__xdref_rbmaxnumnxf16t_1_64_16f_512f_64` | `0x5b72a0` | fp16 masked NaN-suppress reduce-max |
| `module__xdref_rbmax_nx16_64_512_512` | `0x859f00` | int 16-lane segment reduce-max |
| `module__xdref_rbmaxnum_n_2x32f_1_64_32f_512f` | `0x5274c0` | **non-masked** fp32 reduce-max (the contrast — no predicate) |
| `opcode__ivp_rbmaxnumn_2xf32t__stage_5` | `0x7e2470` | the cas decode-stage entry (the IVP op exists in the encode/decode tables) |

The instruction is a genuine FLIX ALU-slot3 op: `nm` shows
`slotfill__F{0,1,2,3,7}__..._S3_ALU_slot3__IVP_RBMAXNUMN_2XF32T` and the matching `_RBMAXNUMNXF16T` —
i.e. the masked reduce-max occupies ALU slot 3 across the F0/F1/F2/F3/F7/N0 FLIX bundle formats.

### 7.3 The accumulator seed `accumulator_cmd` (struct off 44) `[HIGH validator OBSERVED / MED device init]`

`accumulator_cmd` seeds and chains the running maximum across the masked fold (the allowed set is §6.3):

* **`ZeroAccumulate(3)`** — clear the PSUM accumulator to the **MAX identity** (`-inf` for fp / `INT_MIN`
  for int), then fold this instruction's masked max into it. This is the "start a fresh reduce" seed.
* **`Accumulate(2)`** — **continue** an existing PSUM accumulator, chaining the masked max across multiple
  CopyPredicatedReduce instructions (a multi-instruction reduction tree).
* **`Idle(0)`** — no accumulator chaining (single-shot).

The accumulator is read back through the co-resident **`DveReadAccumulator`** DVE kernel (the ×4 sibling in
the same DVE blobs, §4). The MAX identity-init value on a Zero/ZeroAccumulate seed (`-inf` / `INT_MIN`)
follows the Tensor-Reduce rmax "no-widen, init `-inf`/`INT_MIN`" model. The *exact* device identity-init
value is `[MED/INFERRED]` (the per-gen DVE-image body funcVA was not carved — §11).
`[HIGH accum set / MED init value]`

### 7.4 The output geometry `[MED/INFERRED]`

The masked reduce-max collapses the masked lanes to **fewer outputs than inputs** (a reduce collapses; a
copy preserves count). The dst element-count check (§6) compares `src0` vs `dst` products for the *copy*
footprint, while the reduce writes the accumulated max to the PSUM accumulator. The exact reduce axis
(intra-vector lane fold vs per-partition) is the DVE `rbmax` 512-bit / 64-lane register fold — on GpSimd
the partition axis IS the SIMD-lane axis, parallel to Tensor-Reduce's `reduce_axis`. The fold is OBSERVED
at the primitive level (a 512-bit register reduce); that CopyPredicatedReduce maps a given
`num_active_channels` onto that fold is `[MED/INFERRED]` (the DVE per-gen body funcVA was not carved). `[MED]`

`out_dtype` allows **FP32R (`0xB`)** (`is_valid_dtype(out_dtype, DtypeAllowFP32R::True)`, §6) — the **wide
accumulator dtype** for the running max, distinct from the data `dtype_hi`.

---

## 8. The copy — dtype-PRESERVING, no cast (the contrast with CastPredicated)

`op0 == AluOp::Bypass (0x00)` — the data value `src1` (`dtype_hi`) is copied with **no arithmetic and no
dtype conversion**. CopyPredicatedReduce **never casts the data** (unlike CastPredicated, which short-
circuits the dtype-equality check to permit `src1_dtype != out_dtype` and converts through an FP32 hub).
For CopyPredicatedReduce the data is transported dtype-preserving; `out_dtype` is the (possibly wider,
FP32R) accumulator/result dtype for the MAX, **not** a cast target for the copied value. The only data-dtype
freedom is "any datapath dtype except a 32-bit int" (§6.1).

The predicate-FALSE lanes **MERGE** (retain the prior `dst` value) — they are not filled with a constant.
On the DVE datapath this is the `bitkillt` write-enable guard: predicate-false lanes clear their byte-write
mask, so the destination keeps its existing bytes (the all-ones merge of the `_t` predicated copy, the same
mechanism CastPredicated/CopyPredicated use). The predicate consume into a `vbool` and the bitkillt merge
are `[MED/INFERRED]` from the DVE cluster + the predicate ISS page; the per-lane predicate-bit *test* in
the reduce-max body is `[HIGH/OBSERVED]` (§7.2). `[HIGH op-gate / MED merge mechanism]`

---

## 9. The dtype matrix

DTYPE codes from `aws_neuron_isa_tpb_common.h:724-737`; predicate set `:2415`; 32b-int set `:2446`.

| role | field | constraint | allowed dtypes |
|---|---|---|---|
| **PREDICATE** (`src0`) | `dtype_lo` | `is_valid_int_dtype_datapath` (int-datapath ONLY) | `INT8 0x2, INT16 0x4, INT32 0x8, UINT8 0x3, UINT16 0x5, UINT32 0x9` |
| **DATA** (`src1`) | `dtype_hi` | `is_valid_dtype(FP32R::False)` **AND** `!is_valid_32b_int_dtype` | `FP16 0x7, BFLOAT16 0x6, FP8_EXP3 0xD / EXP4 0xE / EXP5 0xF, FP32 0xA, INT8 0x2, INT16 0x4, UINT8 0x3, UINT16 0x5` (+ gen-specific FP4_EXP2 `0x10` / CPTC at MARIANA+) |
| **OUT / ACCUM** | `out_dtype` | `is_valid_dtype(FP32R::True)` | data domain **plus** `FP32R 0xB` (the wide reduce accumulator) |
| immediate | `imm0_dtype` | `s2s2d2_stt_imm0_dtype` | `FP32` for the arith (non-bitvec) path |

> **GOTCHA — the 32-bit-int DATA exclusion.** CopyPredicatedReduce **forbids** a 32-bit int data dtype
> (`!is_valid_32b_int_dtype(dtype_hi)` ⇒ `INT32`/`UINT32` rejected), whereas CastPredicated has no such
> restriction. The reason is the masked reduce-max path: the available `rbmax*` bodies cover 8/16-lane int
> and fp16/fp32, but there is **no 32-bit-int masked reduce-max** primitive (§7.2). The predicate may be
> any int width (including 32-bit), but the *data* being max-reduced may not. The 6-int predicate set and
> the 32-bit-int data exclusion are **invariant across all four gens**; only the fp/sub-byte data domain
> widens per generation.

---

## 10. Algorithm (the decoded behavior) `[HIGH contract / MED body]`

Annotated C pseudocode naming the recovered symbols (the validator
`is_valid_select_reduce`, the device worker self-named `CopyPredicatedReduce`, the DVE reduce primitive
`ivp_rbmaxnumn_2xf32t`):

```c
/* CopyPredicatedReduce  (ISA: SelectReduce, opcode 0xea, struct NEURON_ISA_TPB_S2S2D2_STT_STRUCT)
 *   op0 == AluOp::Bypass (copy, no cast) ; op1 == AluOp::Max (masked reduce-max)
 *   predicate = src0 (dtype_lo, integer) ; data = src1 (dtype_hi, !32b-int)
 *   accum     = accumulator_cmd @ off44  ; out/accum dtype = out_dtype (FP32R allowed)
 */
void copy_predicated_reduce(const NEURON_ISA_TPB_S2S2D2_STT_STRUCT *i)
{
    /* --- seed the running maximum (has_valid_s2s2d2_cpr_accum_cmd gate) --- */
    accum_t acc;
    switch (i->accumulator_cmd) {
        case ACCUM_CMD_ZERO_ACCUMULATE:  /* 3 */ acc = MAX_IDENTITY(i->out_dtype);   /* -inf / INT_MIN */
                                                 break;
        case ACCUM_CMD_ACCUMULATE:       /* 2 */ acc = psum_read_accumulator();      /* chain prior reduce */
                                                 break;
        case ACCUM_CMD_IDLE:             /* 0 */ acc = MAX_IDENTITY(i->out_dtype);    /* single-shot, no chain */
                                                 break;
        /* ZERO(1) and LOAD_ACCUMULATE(4) are rejected by the CPR validator */
    }

    /* element counts are equal (s2s2d2_stt_src/dst_element_cnt_check): one predicate, one data, one dst slot */
    const size_t N = elem_count(&i->src0_mem_pattern);  /* == src1 == dst */

    /* --- predicated, dtype-PRESERVING copy + masked reduce-MAX --- */
    for (size_t e = 0; e < N; ++e) {
        bool p = (read_int(&i->src0_mem_pattern, e, dtype_lo(i)) != 0);   /* integer predicate -> vbool */
        if (p) {                                  /* predicate TRUE lane */
            data_t v = read(&i->src1_mem_pattern, e, dtype_hi(i));        /* op0 == Bypass: NO cast */
            write(&i->dst_mem_pattern, e, v, i->out_dtype);              /* the predicated copy writes v */
            acc = max_num(acc, v);                /* op1 == Max: ONLY true lanes update the running max */
        } else {                                  /* predicate FALSE lane */
            /* bitkillt: write-enable cleared -> dst retains its prior bytes (MERGE), and NO max update */
        }
    }

    /* the masked reduce-max writes acc to the PSUM accumulator footprint (fewer outputs than inputs) */
    psum_write_accumulator(acc);
}
```

**Datapath realization on DVE** (per the predicate ISS semantics + the OBSERVED `rbmaxnum...t` body):
load the per-lane integer predicate into a `vbool`; the predicated copy writes via the `bitkillt`-guarded
select (predicate-false lanes retain `dst` = MERGE); the masked reduce is the vbool-gated NaN-suppressing
reduce-max `ivp_rbmaxnumn_2xf32t` (fp) / `rbmax_nx16` (int), which loads the `vbool` predicate word and
tests each lane's bit, folding **only** predicate-true lanes into the running max (OBSERVED @0x531310:
`mov (%r14),%r9d` ; per-lane bit test ; conditional max). The predicate-load + per-lane gating are
`[HIGH/OBSERVED]`; the exact DVE per-gen body funcVA is `[MED/INFERRED]` — the device DVE IRAM was not
disassembled (§11). `[HIGH/OBSERVED contract; MED body]`

It is **NOT**: a fill/constant on false lanes (it MERGEs); **NOT** an unmasked reduce (only true lanes
contribute); **NOT** a dtype-converting copy (`op0 == Bypass`, no cast — the CastPredicated contrast);
**NOT** a general reduce (`op1` is hard-fixed to Max).

---

## 11. Per-gen presence (SUNDA → MAVERICK)

| gen | `common.h` `0xea // Y` | `is_valid_select_reduce` validator | DVE self-name `S: CopyPredicatedReduce` |
|---|---|---|---|
| SUNDA (v2) | YES (line 296) | YES (op0==Bypass, op1==Max, cpr_accum) | absent — only the DVE RELEASE blob ships (RELEASE strips self-name strings) |
| CAYMAN (v3) | YES (line 300) | YES (`s2s2d2_stt.h:112-140, 214-219`) | PRESENT `@0x18e16a` (DVE DEBUG_DRAM) |
| MARIANA (v4) | YES (line 310) | YES | PRESENT `@0x42844a` (DVE DEBUG_DRAM) |
| MAVERICK (v5) | YES (line 316) | YES | PRESENT `@0x8b0552` (DVE DEBUG_DRAM) |

(MARIANA_PLUS is a MARIANA respin: its DVE blobs include CopyPredicatedReduce `@0x6f016a`; there is no
separate `arch_isa` header — only `neuron_{sunda,cayman,mariana,maverick}_arch_isa` dirs exist.)

CopyPredicatedReduce (`SELECT_REDUCE 0xea`) is therefore present in **all four** ISA generations — the
opcode and the `op0==Bypass / op1==Max / cpr_accum` validator are OBSERVED in every header. Like
CastPredicated, it **exists already at SUNDA (v2)**. `[HIGH/OBSERVED opcode + validator all 4 gens;
MED SUNDA device-string absence (RELEASE-build stripping)]`

> **NOTE — v5/MAVERICK depth.** MAVERICK is header-OBSERVED (opcode + validator byte-exact) and the DVE
> self-name string is byte-present in the MAVERICK DVE DEBUG blob, but the MAVERICK DVE *interior* (the
> per-gen reduce-max body funcVA) is `[INFERRED]` — the v5 device images are not disassembled here.
> `[HIGH header / INFERRED interior]`

---

## 12. The predicated-op family — closed

CopyPredicatedReduce closes the GPSIMD predicated-op family:

| kernel | opcode | operand struct | op0 / op1 | cast? | reduce? | predicate | accumulator |
|---|---|---|---|---|---|---|---|
| CopyPredicated | `0x72` | `S3S3D3_TT` | Bypass / — | no | no | src0 int | — |
| [CastPredicated](castpredicated.md) | `0x99` | `S3S3D3_TT` | Bypass / — | **YES** | no | src0 int | — |
| [CopyPredicatedScalar](copypredicatedscalar.md) | `0xe8` | `S3D3_CP_PRED_SCALAR` | Bypass / — | no | no | src0 int (scalar-imm form) | — |
| **CopyPredicatedReduce** | **`0xea`** | **`S2S2D2_STT`** | **Bypass / Max** | no | **YES — MAX** | src0 int | **Idle/Acc/ZeroAcc** |

All four share: the **DVE engine** (hardware-native, ×4 DVE DEBUG self-name); the **`src0` integer
predicate** (`is_valid_int_dtype_datapath`, the same 6-int set); the **`bitkillt` `_t` MERGE** consume
(predicate-false lanes retain `dst`). They differ:

* **CopyPredicated** (`0x72`) — dtype-preserving copy (`src1_dtype == out_dtype`), no reduce.
* **CastPredicated** (`0x99`) — dtype-CONVERTING copy (`src1_dtype` may `!= out_dtype`, FP32 hub), no reduce.
* **CopyPredicatedScalar** (`0xe8`) — scalar-immediate-src predicated copy.
* **CopyPredicatedReduce** (`0xea`) — dtype-preserving copy (`op0=Bypass`) **plus** a masked REDUCE-MAX
  (`op1=Max`) with a PSUM accumulator — and a different struct (`S2S2D2_STT`, which carries `op1` +
  `accumulator_cmd`). Data dtype additionally excludes 32-bit ints.

The family is now closed: predicated-copy (`0x72`), predicated-cast (`0x99`), predicated-copy-scalar
(`0xe8`), predicated-copy-reduce (`0xea`).

---

## 13. FLIX / desync note

No host-side FLIX desync affects the **primary facts**: the opcode enum, the `S2S2D2_STT` struct layout
(header static-assert + compile-verify), the validator pseudocode (`op0=Bypass` / `op1=Max` /
`cpr_accum_cmd` / predicate dtype), the `instruction_mapping.json` binding, and the
`libnrtucode_internal.so` self-name offsets are all sourced from the in-package C headers and the firmware
string/symbol tables — none depend on Vision-Q7 FLIX bundle decode. The masked reduce-max **value** body
(`rbmaxnumn_2xf32t` @0x531310) is x86-64 `libfiss-base` (`.text` VMA == file-offset, clean disasm) — also
no FLIX risk; the entry + per-lane predicate-bit gating are OBSERVED. The one item **not** carved here — the
per-gen **DVE-image** CopyPredicatedReduce funcVA and the Xtensa instruction body — would be a device
Xtensa disasm (`ncore2gp xtensa-elf-objdump`) and is the standard FLIX-desync surface; it is left `[MED]`
and not asserted byte-exact (the reduce axis/segment mapping and the device identity-init value). Flagged
for a follow-up DVE-IRAM trace if a body-level decode is required.

---

## 14. Cross-references

* **[CastPredicated](castpredicated.md)** (`0x99`) — the twin (predicated cast, no reduce); shares the
  DVE/predicate/`bitkillt` machinery and the `src0` integer predicate. Also the documented home of
  CopyPredicated (`0x72`).
* **[CopyPredicatedScalar](copypredicatedscalar.md)** (`0xe8`) — the scalar-immediate-src member of the
  predicated-op family.
* **[Tensor-Reduce (cross-partition)](tensor-reduce.md)** — the `REDUCE_OP` reference
  (`{ADD0, AVERAGE1, MAX2, BITWISE_OR3, BITWISE_AND4, BITWISE_XOR5}`, `clr_reduce_local`, `0x7c`/`0x7d`);
  the *contrast* reduce mechanism (configurable 6-way vs CopyPredicatedReduce's fixed `op1==Max`).
* **[TensorTensorScan](tensor-tensor-scan.md)** (`0xe5`) and
  **[Scalar-Tensor-Tensor](scalar-tensor-tensor.md)** (`0x9d`/`0x9e`) — the other three `S2S2D2_STT`
  opcodes that share this page's operand struct; #712/#713 raised the `0xea` flag this page closes.
* **cas/fiss Predicate / Boolean (vbool) semantics** (`../../iss/cas-predicate-boolean.md`, planned) — the
  `vbool`/`bitkillt` predicate model behind the masked fold and the predicated-copy merge.
