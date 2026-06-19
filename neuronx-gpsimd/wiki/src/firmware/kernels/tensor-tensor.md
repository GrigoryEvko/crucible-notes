# Tensor-Tensor Elementwise Arith + the ALU-OP Table (`TensorTensorArithOp` `0x41` / `TensorTensorBitvecOp` `0x51`, struct `S3S3D3_TT`)

This page decodes the **binary elementwise** kernel family of the GPSIMD NeuronCore — the
handler the firmware self-names `decode_tensor_tensor_arith` (with its extended sibling
`decode_extended_inst_tensor_tensor_arith`) — and pins, *definitively*, the foundational
`NEURON_ISA_TPB_ALU_OP` table that this family, **Tensor-Scalar**
([`tensor-scalar.md`](tensor-scalar.md)), **Scalar-Tensor-Tensor**
([`scalar-tensor-tensor.md`](scalar-tensor-tensor.md)), `TensorScalarImmLd`, and the
host-side `alu_op.cpp` all share. The two tensor-tensor opcodes — `TensorTensorArithOp`
(`0x41`) and `TensorTensorBitvecOp` (`0x51`) — decode **one** 64-byte wire struct
(`NEURON_ISA_TPB_S3S3D3_TT_STRUCT`), read a 1-byte `op` selector, and run a compiled C++
switch that lowers each ALU op to one or more Cadence Tensilica **IVP** (vector) intrinsics.
The whole family is the two-source counterpart of the single-source Tensor-Scalar form;
they differ only in whether `src1` is a tensor stream or a broadcast scalar — and they share
the *exact same* `op` enum, which is why this page is the table reference the sibling kernels
cross-link.

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD compute core's** own
POOL-engine firmware — hand-scheduled, windowed-ABI Xtensa FLIX/VLIW code in the `ncore2gp`
configuration (Xtensa24, RI-2022.9, NX1.1.4, 512-bit datapath). Every device fact below is
byte-pinned to the carved `CAYMAN` POOL **PERF** EXTISA image (`extisa_CAYMAN_POOL_PERF_EXTISA_0`,
embedded in `libnrtucode_internal.so`; `.text` VMA `0x01000000`), re-disassembled with the
native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); every host-ISA fact (the operand struct,
the ALU-op enum, the validity predicates) is read field-exact from the `aws_neuron_isa_tpb_*.h`
arch-isa headers and `instruction_mapping.json` shipped in the same customop-lib
(`0.21.2.0`) package. The `extracted/` and `ida/` trees are gitignored — reach them with
`fd --no-ignore` or absolute paths. Confidence/evidence tags follow the project
[Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / read-from-header this pass, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a cited sibling page's confidence.

> **Scope of the device disassembly.** The CAYMAN POOL PERF image is **FLIX VLIW with
> interleaved literal pools** that desync under *stock* `objdump` (the documented carve
> limitation): function-start records, `entry` prologues, and the surviving IVP mnemonics
> recover cleanly, but the per-op switch-arm *bodies* sit inside the desyncing bundles. The
> ALU-op **table** itself is therefore recovered from the **authoritative arch-isa enum**
> (`common.h`, compile-asserted, `[HIGH/OBSERVED]`); the per-op→single-IVP **binding** is the
> best correspondence the recovered mnemonics + the header op-class predicates support, marked
> `[MED]`/`[LOW]` per row and *never* asserted as a byte-exact jump-arm mapping. There is **no
> discrete `.rodata` jump table** — dispatch is a compiled switch, confirmed by the
> string-anchored error default (§3.4). v2–v4 (`SUNDA`/`CAYMAN`/`MARIANA`/`MARIANA_PLUS`) are
> byte-grounded; v5/`MAVERICK` is header-OBSERVED only.

---

## 1. The family at a glance — two opcodes, one struct, one ALU table

The tensor-tensor elementwise family is **two POOL opcodes that share one 64-byte operand
struct and one ALU-op enum**, plus an *extended-instruction* third entry point:

| opcode | value | mnemonic | struct | kernel entry (CAYMAN VMA) | role |
|---|---|---|---|---|---|
| `0x41` | 65 | `TensorTensorArithOp` | `S3S3D3_TT` | `0x01000f1c` `pool_tensor_tensor_arith_op` | arithmetic / compare / int-engine ops |
| `0x51` | 81 | `TensorTensorBitvecOp` | `S3S3D3_TT` | `0x0100105c` → `decode_tensor_tensor_arith@0x01000f60` | bitwise / shift ops (integer-only) |
| `0xf0`/spec 2 | — | `ExtendedInstTensorTensorArith` | `EXTENDED_TTA` | `0x01003484` → `decode_extended_inst_tensor_tensor_arith@0x010034b0` | narrowed add/multiply, 2-D patterns |

`[HIGH/OBSERVED — opcode constants common.h:166–167; entry VMAs from .xt.prop records, SX-FW-18/SX-FW-15 trampolines]`

The opcode numbers are read from the arch-isa header verbatim:

```c
// aws_neuron_isa_tpb_common.h:166–167  (enum NEURON_ISA_TPB_OPCODE)
NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_ARITH_OP  = 0x41,    // arith
NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_BITVEC_OP = 0x51,    // bitvec
```

`instruction_mapping.json` `struct2opcode` binds `S3S3D3_TT` to **five** opcodes — the two
tensor-tensor forms plus three siblings that reuse the identical wire layout:

```json
"NEURON_ISA_TPB_S3S3D3_TT_STRUCT": [
  "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_ARITH_OP",     // 0x41
  "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_BITVEC_OP",    // 0x51
  "NEURON_ISA_TPB_OPCODE_COPY_PREDICATED",
  "NEURON_ISA_TPB_OPCODE_CAST_PREDICATED",
  "NEURON_ISA_TPB_OPCODE_BATCH_NORM_BACK_PROP"        // 0x65 — see batchnorm-backprop.md
]
```

`[HIGH/OBSERVED — instruction_mapping.json struct2opcode]` This is exactly why the
[BatchNorm BackProp page](batchnorm-backprop.md) documents the *same* `S3S3D3_TT` struct: the
five opcodes are decoded by separate handlers but all read one wire format. The two
**predicated** forms (`CopyPredicated`/`CastPredicated`) and BackProp pin `op == AluOp::Bypass`
(via `s3s3d3_tt_is_zero_op`); only the two *tensor-tensor* opcodes exercise the full ALU table.

### 1.1 Handler call chain (CAYMAN_0 `.xt.prop` function starts)

Function-start VMAs are read from the first 4 bytes (LE VMA) of each `.xt.prop.<mangled>`
section; a `.xt.prop` section *is* a function-start record, so these are EXACT. Names via
`c++filt`.

| VMA | demangled signature | role |
|---|---|---|
| `0x01000f60` | `decode_tensor_tensor_arith(unsigned int)` | BASE decode entry (`0x51`) |
| `0x01001108` | `setup_64bit_rw(unsigned int, NEURON_ISA_TPB_ALU_OP)` | 64-bit strided r/w setup |
| `0x01001280` | `tensor_tensor_arith_impl(unsigned int, unsigned int, unsigned int)` | **32-bit core elementwise worker** |
| `0x01001f7c` | `tensor_tensor_64bit_dispatch<VectorInt64>(uint, uint, ALU_OP)` | i64 arith dispatch |
| `0x01002720` | `tensor_tensor_64bit_dispatch<VectorUint64>(uint, uint, ALU_OP)` | u64 arith dispatch |
| `0x01002c2c` | `tensor_tensor_64bit_bitvec_dispatch<VectorInt64>(uint, ALU_OP)` | i64 bitvec dispatch |
| `0x01002fc4` | `tensor_tensor_64bit_bitvec_dispatch<VectorUint64>(uint, ALU_OP)` | u64 bitvec dispatch |
| `0x010034b0` | `decode_extended_inst_tensor_tensor_arith(bool, unsigned int)` | EXTENDED decode entry (`0xf0`/spec 2) |

`[HIGH/OBSERVED — .xt.prop function-start records, CAYMAN POOL PERF EXTISA]`

Verified `entry` prologues (native `xtensa-elf-objdump --adjust-vma`, binary mode):

```
0x01000f60:  36 41 00   entry a1,32     ; decode_tensor_tensor_arith
0x01001108:  36 61 00   entry a1,48     ; setup_64bit_rw  (larger frame, 64-bit temps)
0x01001280:  36 81 02   entry a1,0x140 ; movi a10,-64 ; and a8,a1,a10 ; movsp a1,a8
             ;            tensor_tensor_arith_impl — aligns SP to 64 B for vector spills (big worker)
0x010034b0:  36 41 00   entry a1,32     ; decode_extended_inst_tensor_tensor_arith
0x0100105c:  36 41 00   entry a1,32     ; op-0x51 trampoline (body FLIX-desyncs on `4f 00 ..`)
```

`[HIGH/OBSERVED — re-anchored prologue disasm]` The pervasive `unsigned int` first argument is
the per-instruction descriptor index / SBUF-resident instruction-word pointer the POOL
front-end hands the kernel; the two extra `unsigned int` args of
`tensor_tensor_arith_impl(j,j,j)` are the decoded loop bounds (`num_tensor_elements`,
`num_active_channels`) materialised by the decode stage `[MED/INFERRED — from the 0x140 frame + the decode-stage DEBUG strings of §7]`.

> **NOTE — the `ncore2gp` FLIX desync.** The function *bodies* (`decode_tensor_tensor_arith`,
> `tensor_tensor_arith_impl`, the four 64-bit dispatchers, `decode_extended_inst_*`) desync
> under stock `objdump` on the recurring `4f 00 6f ..` / `5f ..` literal-pool lead bytes — the
> native `ncore2gp`-configured `xtensa-elf-objdump` recovers the prologues and individual FLIX
> bundles (exposing the IVP mnemonics), but the exact switch-arm scheduling sits inside the
> mis-decoded spans and is **not** reported here as real instructions.

---

## 2. The operand struct — `NEURON_ISA_TPB_S3S3D3_TT_STRUCT` (64 B)

Read field-exact from `aws_neuron_isa_tpb_s3s3d3_tt.h:27`, compile-asserted `sizeof == 64`.
This is the single wire format both tensor-tensor opcodes decode. The name `S3S3D3` literally
encodes the shape: **S**rc-3D, **S**rc-3D, **D**st-3D — two 3-D-strided source tensors and one
3-D-strided destination.

| off | size | field | type | notes |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode:1, inst_word_len:1, debug_cmd:1, debug_hint:1}`; `opcode`=`0x41`/`0x51` |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | `{wait_mode:1, wait_idx:1, update_mode:1, update_idx:1, semaphore_value:4}` (sync) |
| 12 | 1 | `in0_in1_dtype` | `NEURON_ISA_TPB_DTYPE_PAIR` | bitfield `{dtype_lo:4 (src0), dtype_hi:4 (src1)}` |
| 13 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | destination element dtype |
| **14** | **1** | **`op`** | **`NEURON_ISA_TPB_ALU_OP`** | **THE ALU-OP SELECTOR — the §3 table** |
| 15 | 1 | `num_active_channels` | `uint8_t` | partition / channel count (1..128) |
| 16–31 | 16 | `src0_mem_pattern` | `NEURON_ISA_TPB_TENSOR3D` | INPUT TENSOR 0 (3-D strided) |
| 32–47 | 16 | `src1_mem_pattern` | `NEURON_ISA_TPB_TENSOR3D` | INPUT TENSOR 1 (3-D strided) |
| 48–63 | 16 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR3D` | OUTPUT TENSOR (3-D strided) |

`[HIGH/OBSERVED — aws_neuron_isa_tpb_s3s3d3_tt.h:27; HEADER/EVENTS/DTYPE_PAIR/TENSOR3D from common.h]`

```c
// aws_neuron_isa_tpb_s3s3d3_tt.h:27
typedef struct NEURON_ISA_TPB_S3S3D3_TT_STRUCT {
    NEURON_ISA_TPB_HEADER     header;                 // 4    ( 0 -  3)
    NEURON_ISA_TPB_EVENTS     events;                 // 8    ( 4 - 11)
    NEURON_ISA_TPB_DTYPE_PAIR in0_in1_dtype;          // 1    (12     )  // src0 low, src1 high
    NEURON_ISA_TPB_DTYPE      out_dtype;              // 1    (13     )
    NEURON_ISA_TPB_ALU_OP     op;                      // 1    (14     )
    uint8_t                   num_active_channels;     // 1    (15     )
    NEURON_ISA_TPB_TENSOR3D   src0_mem_pattern;        // 16   (16 - 31)
    NEURON_ISA_TPB_TENSOR3D   src1_mem_pattern;        // 16   (32 - 47)
    NEURON_ISA_TPB_TENSOR3D   dst_mem_pattern;         // 16   (48 - 63)
} NEURON_ISA_TPB_S3S3D3_TT_STRUCT;
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3S3D3_TT_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S3S3D3_TT_STRUCT is NOT 64B.");
```

The supporting structs (all `common.h`):

```c
typedef struct NEURON_ISA_TPB_TENSOR3D {     // 16 B
    NEURON_ISA_TPB_ADDR4 start_addr;   // 4  — partition-offset / shape-reg / addr-reg union (marker byte)
    int16_t              step_elem[3]; // 6  — per-dim signed stride (elements); 0 => broadcast axis
    uint16_t             num_elem[3];  // 6  — per-dim count
} NEURON_ISA_TPB_TENSOR3D;

typedef struct NEURON_ISA_TPB_HEADER {       // 4 B
    NEURON_ISA_TPB_OPCODE opcode;  uint8_t inst_word_len, debug_cmd, debug_hint;
} NEURON_ISA_TPB_HEADER;

typedef struct NEURON_ISA_TPB_EVENTS {       // 8 B
    NEURON_ISA_TPB_WAIT_MODE wait_mode;  uint8_t wait_idx;
    NEURON_ISA_TPB_UPDATE_MODE update_mode; uint8_t update_idx; uint32_t semaphore_value;
} NEURON_ISA_TPB_EVENTS;

typedef struct NEURON_ISA_TPB_DTYPE_PAIR {   // 1 B
    NEURON_ISA_TPB_DTYPE dtype_lo : 4;  NEURON_ISA_TPB_DTYPE dtype_hi : 4;
} NEURON_ISA_PACKED NEURON_ISA_TPB_DTYPE_PAIR;
```

`start_addr` is the `NEURON_ISA_TPB_ADDR4` union: an immediate `PARTITION_OFFSET`
(SBUF/PSUM partition-offset encoding) or an `ADDR_REG4` (`{regnum, reserved[2], marker}`); the
high `marker` byte selects immediate / addr-reg / shape-reg / indirect addressing
(`ADDR4_MARKER_*`, `0x00`/`0x80`/`0x40`/`0xC0`/`0x20`/`0xA0`). When `start_addr` resolves to a
*shape register*, several validity checks (element-count, partitions) are relaxed because the
shape is dynamic — see §6. `[HIGH/OBSERVED — common.h ADDR4/TENSOR3D/ADDR4_MARKER_*]`

> **NOTE — header compile-verify.** `S3S3D3_TT` and the extended `EXTENDED_TTA`/`EXTENDED_STRUCT`
> all `ISA_STATIC_ASSERT(sizeof == 64)`; the layouts here are read field-exact from the shipped
> headers, so `sizeof == 64` is established by the headers, not inferred. The struct is shared
> verbatim by BackProp / Copy-/Cast-Predicated (§1) — do not attribute the `op@14`/`out_dtype@13`
> *semantics* differences to layout differences; the bytes are identical.

### 2.1 DTYPE encoding (`NEURON_ISA_TPB_DTYPE`, 4-bit, `common.h:722`)

The two nibbles of `in0_in1_dtype` and the `out_dtype` byte each hold one of these 4-bit codes.
The numbering is **not** dense and **not** ordered by width — pin it from the header:

| code | dtype | | code | dtype |
|---|---|---|---|---|
| `0x0` | `INVALID` (RTL bitvec sentinel) | | `0x8` | `INT32` |
| `0x1` | `UINT64` | | `0x9` | `UINT32` |
| `0x2` | `INT8` | | `0xA` | `FP32` |
| `0x3` | `UINT8` | | `0xB` | `FP32R` (RTL FP22 partial fp32) |
| `0x4` | `INT16` | | `0xC` | `INT64` |
| `0x5` | `UINT16` | | `0xD` | `FP8_EXP3` |
| `0x6` | `BFLOAT16` | | `0xE` | `FP8_EXP4` |
| `0x7` | `FP16` | | `0xF` | `FP8_EXP5` |

`[HIGH/OBSERVED — common.h:722]`

> **QUIRK — `UINT64`=`0x1` sits between `INVALID` and `INT8`.** The 64-bit codes are *not*
> contiguous with the 32-bit ones: `UINT64`=`0x1`, `INT64`=`0xC`. The kernel's 64-bit detector
> is the predicate `is_valid_64b_int_dtype(d) == (d==INT64 || d==UINT64)` — it tests the two
> specific codes, **not** a width field. Reimplementations must table-map dtype→width; there is
> no arithmetic relation between code and byte-width.

### 2.2 The EXTENDED variant — `NEURON_ISA_TPB_EXTENDED_TTA_STRUCT` (64 B)

`decode_extended_inst_tensor_tensor_arith` (`0xf0`/spec 2) reads a *different* 64-byte struct
(`aws_neuron_isa_tpb_extended_utils.h:88`) that uses **2-D** mem patterns (`TENSOR2D`, 12 B)
and carries the extended-instruction sub-opcode + completion fields:

| off | size | field | type | notes |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `opcode`=`0xf0` (ExtendedInst) |
| 4–11 | 8 | `events` | `EVENTS` | |
| 12 | 1 | `extended_opcode` | `EXAMPLE_EXTENDED_OPCODES1` | `= 2` (`EXTENDED_TENSOR_TENSOR_ARITH`) |
| 13 | 1 | `completion_info` | `EXT_COMPLETION_INFO` | `{has_read:1, _:1, has_write:1, num_active_ports:3, _:2}` |
| 14 | 1 | `in0_in1_dtype` | `DTYPE_PAIR` | `dtype_lo`=src0, `dtype_hi`=src1 |
| 15 | 1 | `out_dtype` | `DTYPE` | |
| **16** | **1** | **`op`** | **`NEURON_ISA_TPB_ALU_OP`** | header comment: **"op restricted to add/multiply"** |
| 17–19 | 3 | `reserved0[3]` | — | |
| 20–31 | 12 | `src0_mem_pattern` | `TENSOR2D` | `{ADDR4 start_addr(4); int16 step[2]; uint16 num[2]}` |
| 32–43 | 12 | `src1_mem_pattern` | `TENSOR2D` | |
| 44–55 | 12 | `dst_mem_pattern` | `TENSOR2D` | |
| 56–63 | 8 | `reserved1[8]` | — | |

`[HIGH/OBSERVED — aws_neuron_isa_tpb_extended_utils.h:88]`

Note `op` is at offset **16** here (not 14) — the extended struct inserts `extended_opcode` +
`completion_info` ahead of the dtype pair. `completion_info.num_active_ports` encoding:
`0 => 8 ports (128 partitions)`, `1..7 => that many ports × 16 partitions` — each "port" maps to
a **single Q7** (this is the wire-level confirmation that a NeuronCore's POOL work fans across 8
Vision-Q7 lanes). `[HIGH/OBSERVED — EXT_COMPLETION_INFO bitfield + num_active_ports comment, aws_neuron_isa_tpb_extended.h]`

The extended-inst sub-opcode enum (authoritative; corrects an earlier order guess):

```c
// aws_neuron_isa_tpb_extended_utils.h:22  (EXAMPLE_EXTENDED_OPCODES1)
EXTENDED_ENGINE_NOP          = 0,
EXTENDED_COPY                = 1,
EXTENDED_TENSOR_TENSOR_ARITH = 2,   // <== this handler
EXTENDED_RAND_SET_STATE      = 3,
EXTENDED_RAND_GET_STATE      = 4,
// (5 is reserved/skipped)
EXTENDED_SBUF_TO_SBUF        = 6,
EXTENDED_CPTC_DECODE         = 7,
EXTENDED_RDMA_DESC_GEN       = 8,
EXTENDED_RDMA_DESC_START     = 9,
```

> **CORRECTION — spec-3 is `RandSetState`, spec-4 is `RandGetState`.** The header's explicit enum
> is the opposite of an earlier enum-order *guess* (which probed `RandGetState=3 / RandSetState=4`).
> Pin it to the header: `RAND_SET_STATE = 3`, `RAND_GET_STATE = 4`. `[HIGH/OBSERVED — header enum]`

> **GOTCHA — extended-inst headers are flagged "not official architecture."** The
> `extended_utils.h` preamble states these structs "may change or be deleted without notice"
> and are "examples of the use of the ExtendedInst opcode." Treat the extended-TTA path as a
> real-but-narrow capability: `op` is restricted to **add/multiply** only, 2-D patterns, 128
> active channels by default. The full ALU table (§3) is exercised by the *base* `0x41`/`0x51`
> path, not the extended one. `[HIGH/OBSERVED — header comments]`

---

## 3. The FULL ALU-OP table — `NEURON_ISA_TPB_ALU_OP`

This is the foundational sub-table the task pins definitively. Source:
`aws_neuron_isa_tpb_common.h:939`, enum `NEURON_ISA_TPB_ALU_OP`, `NEURON_ISA_PACKED` (1 byte).
It is the **complete** set of values the `s3s3d3_tt.op` (and extended `op`) byte can hold and
the kernel switches on. **Header-grounded count: 60 ops** — a contiguous base band
`0x00..0x1D` (30 ops) plus an integer-engine band `0xC4..0xE1` (30 ops). The integer band sets
`bit[7:6] == 0x3` to select the integer compute engine (`0xC4 = 0b11000100`).

The "recovered IVP" column is the device-side lowering (the vector intrinsic(s) the family
emits for that op). It is `[MED]`/`[LOW]` per the §4 caveat — the IVP *vocabulary* is recovered
with confidence, the per-op binding is the best correspondence the surviving mnemonics + the
header op-class semantics support, never asserted byte-exact.

### 3.1 Base band `0x00..0x1D` (30 ops)

| code | name (`NEURON_ISA_TPB_ALU_OP_*`) | class | recovered IVP / note | conf(IVP) |
|---|---|---|---|---|
| `0x00` | `BYPASS` | bitvec + arith | pass-through (copy src0); legal in **both** classes; the zero-op for BackProp/Predicated | HIGH(enum) |
| `0x01` | `BITWISE_NOT` | bitvec | bitwise complement (`ivp_notb` / xor-with-ones) | MED |
| `0x02` | `ARITH_SHIFT_LEFT` | bitvec | arithmetic shift-left (`ivp_sll*`) | MED |
| `0x03` | `ARITH_SHIFT_RIGHT` | bitvec | arithmetic shift-right (`ivp_sra*` / `ivp_lsr2nx8_i`) | MED |
| `0x04` | `ADD` | arith | `ivp_addn_2x32t` / `ivp_addnx16` / `ivp_addexpnxf16t` | MED |
| `0x05` | `SUBTRACT` | arith | `ivp_subsnx16` / `ivp_subn_2x32` | MED |
| `0x06` | `MULT` | arith | `ivp_mulan_2x32` / `ivp_mulnx16c` / `ivp_mulpan16xr16` / MAC family | MED |
| `0x07` | `DIVIDE` | arith\* | reciprocal + Newton (fp); **not** a `general_arith_op` | MED |
| `0x08` | `MAX` | arith | `ivp_maxnxf16t` / `ivp_maxnx16` / `ivp_maxn_2x32` | MED |
| `0x09` | `MIN` | arith | `ivp_minnxf16` / `ivp_minnx16` / `ivp_minn_2x32` | MED |
| `0x0A` | `BITWISE_AND` | bitvec | `ivp_and2nx8` / `ivp_andb` (vector AND) | MED |
| `0x0B` | `BITWISE_OR` | bitvec | vector OR (`ivp_or2nx8` / `ivp_orb`) | MED |
| `0x0C` | `BITWISE_XOR` | bitvec | vector XOR (`ivp_xor2nx8` / `ivp_xorb`) | MED |
| `0x0D` | `LOGICAL_AND` | arith | logical (0/1-producing) AND | LOW |
| `0x0E` | `LOGICAL_OR` | arith | logical OR | LOW |
| `0x0F` | `LOGICAL_XOR` | arith | logical XOR | LOW |
| `0x10` | `LOGICAL_SHIFT_LEFT` | bitvec | logical shl | MED |
| `0x11` | `LOGICAL_SHIFT_RIGHT` | bitvec | logical shr | MED |
| `0x12` | `IS_EQ` | arith | `ivp_eq2nx8` / `ivp_eqnx16` / `ivp_oeqn_2xf32` | MED |
| `0x13` | `IS_GT` | arith | compare-gt (`ivp_lt*` with operands swapped) | LOW |
| `0x14` | `IS_GE` | arith | compare-ge (`ivp_le*` swapped) | LOW |
| `0x15` | `IS_LE` | arith | `ivp_le2nx8` / `ivp_len_2x32` | LOW |
| `0x16` | `IS_LT` | arith | `ivp_lt2nx8` / `ivp_ltn_2x32` / `ivp_oltn_2xf32` | MED |
| `0x17` | `ABSOLUTE_DIFF` | arith | `ivp_babssub*` / `ivp_babssubu*` — `abs(src0 − src1)` | MED |
| `0x18` | `IS_NE` | arith | `ivp_neqnx16` / `ivp_neq2nx8` | MED |
| `0x19` | `ABSOLUTE_VALUE` | arith | `abs(src0)` (`ivp_abs2nx8` / `ivp_absn_2x32`) | LOW |
| `0x1A` | `POW` | arith\* | **Pool only**; polynomial via `.rodata` fp coeffs | MED |
| `0x1B` | `MOD` | arith\* | modulo; **also** a valid int-aluop (see §3.3) | LOW |
| `0x1C` | `CRC32` | bitvec\* | **Pool only**; **not** a `general_bitvec_op` | LOW |
| `0x1D` | `RSQRT` | arith\* | **Pool only**; reciprocal-sqrt via `.rodata` fp coeffs | MED |

### 3.2 Integer-engine band `0xC4..0xE1` (30 ops, `bit[7:6]==0x3`)

| code | name | sign | recovered IVP / note | conf(IVP) |
|---|---|---|---|---|
| `0xC4` | `ADD_INT` | shared s/u | shared signed/unsigned int add | MED |
| `0xC5` | `MULT_INT` | signed | signed int multiply | MED |
| `0xC6` | `SUBTRACT_INT` | shared s/u | shared signed/unsigned int subtract | MED |
| `0xC7` | `DIVIDE_INT` | signed | signed int divide | LOW |
| `0xC8` | `MOD_INT` | signed | signed C-style modulo in 32b (np.fmod) | LOW |
| `0xC9` | `IS_EQUAL_INT` | shared s/u | shared int equality | MED |
| `0xCA` | `IS_NE_INT` | shared s/u | shared int not-equal | MED |
| `0xCB` | `ABS_MAX_INT` | signed | signed-only abs-max | LOW |
| `0xCC` | `ABS_MIN_INT` | signed | signed-only abs-min | LOW |
| `0xCD` | `ABS_DIFF_INT` | signed | signed abs-diff (`ivp_babssub*` int form) | MED |
| `0xCE` | `ABS_VALUE_INT` | signed | signed-only abs | LOW |
| `0xCF` | `MAX_INT` | signed | `ivp_bmaxnx16` / `ivp_bmaxun_2x32` | MED |
| `0xD0` | `MIN_INT` | signed | `ivp_bminn_2x32` / `ivp_bmin2nx8` | MED |
| `0xD1` | `IS_GT_INT` | signed | signed int gt | LOW |
| `0xD2` | `IS_GE_INT` | signed | signed int ge | LOW |
| `0xD3` | `IS_LE_INT` | signed | signed int le | LOW |
| `0xD4` | `IS_LT_INT` | signed | signed int lt | LOW |
| `0xD5` | `MAX_UINT` | unsigned | `ivp_bmaxu2nx8` / `ivp_bmaxunx16` | MED |
| `0xD6` | `MIN_UINT` | unsigned | `ivp_bminu2nx8` / `ivp_bminunx16` | MED |
| `0xD7` | `IS_GT_UINT` | unsigned | unsigned gt | LOW |
| `0xD8` | `IS_GE_UINT` | unsigned | unsigned ge | LOW |
| `0xD9` | `IS_LE_UINT` | unsigned | unsigned le | LOW |
| `0xDA` | `IS_LT_UINT` | unsigned | unsigned lt | LOW |
| `0xDB` | `MULT_UINT` | unsigned | `ivp_muluu*` / `ivp_dmuluuq2n8xr8` | MED |
| `0xDC` | `DIVIDE_UINT` | unsigned | unsigned divide | LOW |
| `0xDD` | `MOD_UINT` | unsigned | unsigned modulo | LOW |
| `0xDE` | `AMAX_INT` | signed | **argmax**, `out_dtype` = u32 | LOW |
| `0xDF` | `AMIN_INT` | signed | **argmin**, `out_dtype` = u32 | LOW |
| `0xE0` | `AMAX_UINT` | unsigned | unsigned argmax, `out_dtype` = u32 | LOW |
| `0xE1` | `AMIN_UINT` | unsigned | unsigned argmin, `out_dtype` = u32 | LOW |

The enum **closes at `0xE1`**. Total = 30 base + 30 int = **60 ALU ops**. `[HIGH/OBSERVED — full enum read verbatim from common.h:939–1000]`

```c
// aws_neuron_isa_tpb_common.h:939 — verbatim header, NEURON_ISA_PACKED (1 byte)
typedef enum NEURON_ISA_TPB_ALU_OP {
    NEURON_ISA_TPB_ALU_OP_BYPASS              = 0x00,  // bitvec
    NEURON_ISA_TPB_ALU_OP_BITWISE_NOT         = 0x01,  // bitvec
    /* ... 0x02..0x1C ... */
    NEURON_ISA_TPB_ALU_OP_RSQRT               = 0x1D,  // Pool only
    NEURON_ISA_TPB_ALU_OP_ADD_INT             = 0xC4,  // bit 7:6 == 0x3 -> integer engine; shared s/u
    /* ... 0xC5..0xE0 ... */
    NEURON_ISA_TPB_ALU_OP_AMIN_UINT           = 0xE1,  // unsigned argmin, out_dtype u32
} NEURON_ISA_PACKED NEURON_ISA_TPB_ALU_OP;
```

> **QUIRK — the integer band is a *disjoint* high-code range, not a flag on the base ops.** The
> integer-engine variants (`ADD_INT` etc.) are **distinct enum values** at `0xC4+`, *not* the
> base ops (`ADD`=`0x04`) decoded with an "int" modifier. The base `ADD`/`MULT`/… run the generic
> (fp-capable, type-converting) datapath; the `*_INT` codes route to the dedicated integer
> engine with strict signedness checks (§3.3). A reimplementation **must** keep both as separate
> opcodes — `is_valid_int_aluop(op)` is a membership test over the `0xC4..0xE1` set (minus the
> four argmax/argmin) **plus** `MOD`(`0x1B`).

### 3.3 ALU-op class partition (which op is legal in which opcode)

The header carries the op-class predicates verbatim (Rust-pseudocode comments,
`common.h:1419/1624/1653/1666/1671/2456/2477/2497`). These are the *exact* legality gates the
kernel applies. `[HIGH/OBSERVED — predicate bodies read field-exact]`

```c
// is_bitvec_op(op): the bitwise/shift set (10 ops)
is_bitvec_op = { BYPASS, BITWISE_NOT, ARITH_SHIFT_LEFT, ARITH_SHIFT_RIGHT,
                 LOGICAL_SHIFT_LEFT, LOGICAL_SHIFT_RIGHT, BITWISE_AND, BITWISE_OR,
                 BITWISE_XOR, CRC32 }
is_general_bitvec_op(op) = is_bitvec_op(op) && op != CRC32
//  => TensorTensorBitvecOp(0x51) accepts the bitvec set MINUS CRC32 (9 ops).

// is_arith_op(op): the arithmetic/compare/logical set (26 ops)
is_arith_op = { BYPASS, ADD, SUBTRACT, MULT, DIVIDE, MAX, MIN,
                LOGICAL_AND, LOGICAL_OR, LOGICAL_XOR,
                IS_EQ, IS_GT, IS_GE, IS_LE, IS_LT, IS_NE,
                ABSOLUTE_DIFF, ABSOLUTE_VALUE, POW, MOD,
                ADD_INT, MULT_INT, SUBTRACT_INT, DIVIDE_INT, MOD_INT, RSQRT }
is_general_arith_op(op) = is_arith_op(op)
                       && op != DIVIDE && op != POW && op != MOD && op != RSQRT
                       && !is_valid_int_aluop(op)
//  => TensorTensorArithOp(0x41) accepts:
//       is_general_arith_op(op) || op == POW || is_valid_int_aluop(op)
//     (the general arith set, PLUS POW, PLUS the int band — but NOT raw DIVIDE/RSQRT;
//      MOD re-enters via is_valid_int_aluop, DIVIDE/RSQRT do not.)

// is_valid_int_aluop(op): the int band MINUS the 4 argmax/argmin, PLUS MOD(0x1B)  (26 ops)
is_valid_int_aluop = { AddInt, MultInt, SubtractInt, Mod, DivideInt, ModInt,
                       DivideUint, ModUint, MultUint, IsEqualInt, IsNeInt, AbsDiffInt,
                       AbsMaxInt, AbsMinInt, MaxInt, MinInt, IsGtInt, IsGeInt, IsLeInt, IsLtInt,
                       MaxUint, MinUint, IsGtUint, IsGeUint, IsLeUint, IsLtUint }
//  NOTE: AMAX_INT/AMIN_INT/AMAX_UINT/AMIN_UINT (0xDE..0xE1) are NOT in is_valid_int_aluop.

is_shift_op(op) = { ArithShiftLeft, ArithShiftRight, LogicalShiftLeft, LogicalShiftRight }
```

> **QUIRK — `MOD` is the cross-over op.** `MOD`(`0x1B`) lives in the **base** band but is a member
> of `is_valid_int_aluop` *and* both `is_signed_alu_op` and `is_unsigned_alu_op` — it is the one
> base-band op that participates in the integer-engine signedness machinery. `DIVIDE`(`0x07`),
> `POW`(`0x1A`), `RSQRT`(`0x1D`), `CRC32`(`0x1C`) are the Pool-only / special ops excluded from
> the *general* sets and admitted (if at all) only by an explicit `|| op == POW` style clause.

The signedness partition gates the int band against the dtypes (`common.h:2477/2497`):

```c
is_signed_alu_op(op)   = { AddInt, MultInt, SubtractInt, DivideInt, ModInt, Mod,
                           IsEqualInt, IsNeInt, AbsDiffInt, AbsMaxInt, AbsMinInt,
                           MaxInt, MinInt, IsGtInt, IsGeInt, IsLeInt, IsLtInt }
is_unsigned_alu_op(op) = { AddInt, MultInt, SubtractInt, DivideInt, ModInt, Mod,
                           DivideUint, ModUint, MultUint, IsEqualInt, IsNeInt,
                           MaxUint, MinUint, IsGtUint, IsGeUint, IsLeUint, IsLtUint }
```

Note the *shared* int ops (`AddInt`, `MultInt`, `SubtractInt`, `DivideInt`, `ModInt`, `Mod`,
`IsEqualInt`, `IsNeInt`) appear in **both** signed and unsigned lists — they are legal with
either signedness as long as **all three** operands (src0/src1/out) agree (§5.2). The
sign-specific ops (`MaxInt`/`MaxUint`, `AbsMaxInt`, the `_INT`/`_UINT` compares) appear in only
one list. `[HIGH/OBSERVED — both predicate bodies]`

### 3.4 The switch default (string-anchored)

An `op` outside the legal class for its opcode hits the C++ switch default, which logs a baked
DEBUG string from `dbg_dram.bin`:

```
"P%i: Error, Unimplemented tensor-tensor ALU op(0x%x)"   // 32-bit path; prints op hex
"P%i: Error, Unimplemented tensor-tensor ALU op."        // 64-bit dispatch path
```

`[HIGH/OBSERVED — strings on dbg_dram.bin]` These strings *prove* the kernel is a
**switch-over-`alu_op` with an error default**, not a `.rodata` table-indexed jump. There is no
discrete op→address table in `.rodata` (which holds only the POW/RSQRT polynomial fp coeffs and
dispatcher size/mask fields, `0x02000000..0x020001ec`, incl. the `0x3f800000`=`1.0f` constants).

---

## 4. Per-op IVP lowering — recovered vector vocabulary

The kernel issues Cadence `ncore2gp` GPSIMD vector (IVP) intrinsics per op. The bodies are FLIX
VLIW with interleaved literal pools that desync under stock `objdump`, but individual FLIX
bundles decode and expose the IVP mnemonics. Harvested across the tensor-tensor band
(`0x01001108..0x010035a0`, CAYMAN POOL PERF EXTISA) — these are the vector ops the family
actually emits, and they corroborate the §3 lowering column (the same mnemonics appear in
the ISA batches [b01](../../isa/ref/b01-vec-alu-int.md) / [b02](../../isa/ref/b02-vec-alu-fp.md) /
[b03](../../isa/ref/b03-vec-alu-rest.md)):

| op group | IVP intrinsics (recovered) |
|---|---|
| multiply | `ivp_mulan_2x32`, `ivp_mulnx16c`, `ivp_muli2nx8x16`, `ivp_mulpan16xr16`, `ivp_mulpn16xr16`, `ivp_mulus*`/`ivp_mulsup*`/`ivp_mulsun*`/`ivp_mulup*`/`ivp_mul4t*`, `ivp_dmulq2n8*xr8`, `ivp_dmuluuq2n8xr8`, `ivp_dmulusq2n8xr8` |
| add / sub | `ivp_addn_2x32t`, `ivp_addnx16`, `ivp_addexpnxf16t`, `ivp_subsnx16`, `ivp_srsnx16` |
| max / min (fp/16b) | `ivp_maxnxf16t`, `ivp_maxnx16`, `ivp_minnxf16`, `ivp_minnx16` |
| max / min (int) | `ivp_bmaxnx16`, `ivp_bmaxunx16`, `ivp_bmaxu2nx8`, `ivp_bmaxun_2x32`; `ivp_bmin2nx8`, `ivp_bminn_2x32`, `ivp_bminu2nx8` |
| abs-diff | `ivp_babssub2nx8`, `ivp_babssubnx16`, `ivp_babssubunx16`, `ivp_babssubu2nx8` |
| compare | `ivp_eq2nx8`, `ivp_eqnx16`, `ivp_oeqn_2xf32`, `ivp_neqnx16`, `ivp_lt2nx8`, `ivp_oltn_2xf32` |
| select / blend | `ivp_dselnx16t`, `ivp_dseln_2x32t`, `ivp_sel2nx8i`, `ivp_sel2nx8i_s4` (lane select for predication) |
| load / store / align | `ivp_la*`/`ivp_lv*`/`ivp_sv*`/`ivp_lalign_i`/`ivp_labvdcmprs2nx8_xp` (strided loads for the TENSOR3D patterns) |
| convert | `ivp_float16nx16t`, `ivp_ufloatn_2x32t`, `ivp_movvint16`, `ivp_movpint16`, `ivp_dextrprn_2x32` (dtype conversion across the in/out pair) |
| misc / avg | `ivp_avgu2nx8` (rounding helper), `ivp_counteqmz4nx8`, `ivp_gatheranx8ut` |

`[MED — IVP vocabulary recovered with confidence; per-op selection arithmetic sits in the desynced switch and is reported structurally]`

> **GOTCHA — the op→single-IVP binding cannot be pinned 1:1 for every op.** Because the
> surrounding FLIX bundles desync, the exact `case ADD: ivp_addn_2x32t(...)` arm cannot be read
> byte-exact for all 60 ops. The §3 bindings are the strongest correspondence the recovered
> mnemonics + the header op-class semantics support (`[MED]`/`[LOW]` flagged). The IVP
> *vocabulary itself* (the set the family emits) is recovered with confidence; the exact per-op
> selection is **not fabricated** where it could not be read.

---

## 5. Dtype handling and the 64-bit split

### 5.1 The 64-bit path (INT64 / UINT64)

The `ncore2gp` vector engine has **no native 64-bit ALU**; 64-bit operands are synthesised from
32-bit halves and the handler branches them to dedicated template dispatchers:

```c
// is_valid_64b_int_dtype(d) == (d == INT64 || d == UINT64)   [common.h:2451]
if (is_valid_64b_int_dtype(out_dtype)) {
    if (tensor_tensor_arith(opcode)) {          // 0x41
        if (out_dtype == INT64)  tensor_tensor_64bit_dispatch<VectorInt64 >(idx, n, op);  // 0x01001f7c
        else /* UINT64 */        tensor_tensor_64bit_dispatch<VectorUint64>(idx, n, op);  // 0x01002720
    } else {                                    // 0x51 bitvec
        if (out_dtype == INT64)  tensor_tensor_64bit_bitvec_dispatch<VectorInt64 >(idx, op); // 0x01002c2c
        else /* UINT64 */        tensor_tensor_64bit_bitvec_dispatch<VectorUint64>(idx, op); // 0x01002fc4
    }
    // setup_64bit_rw(idx, op) @0x01001108 prepares the 64-bit strided read/write
    //   (entry a1,48 — a larger frame for the 64-bit temporaries)
} else {
    tensor_tensor_arith_impl(idx, num_tensor_elements, num_active_channels);  // 0x01001280 — the 32-bit core
}
```

`[HIGH/OBSERVED — .xt.prop dispatch signatures; is_valid_64b_int_dtype body]`

> **NOTE — the bitvec 64-bit dispatcher takes no second dtype.** `tensor_tensor_64bit_bitvec_dispatch`
> is `(uint, ALU_OP)` — only one dtype argument — consistent with the bitvec rule that
> src0/src1/out must all be the identical INT dtype (§5.2). The arith dispatcher is `(uint, uint,
> ALU_OP)` (two index/count args). Everything ≤32-bit (fp8/fp16/bf16/fp32/fp32r, int8/16/32,
> uint8/16/32) runs the 32-bit core worker `tensor_tensor_arith_impl@0x01001280`. The recovered
> i64 IVP set on the arith dispatcher: `ivp_bmaxun_2x32`/`ivp_bmaxunx16` (max),
> `ivp_bminn_2x32`/`ivp_bminu2nx8` (min), `ivp_babssub*` (abs-diff), `ivp_dextrprn_2x32` (64-bit
> recombine), `ivp_dmulusq2n8xr8`/`ivp_mul*` (multiply), `ivp_subsnx16` (sub). `[MED]`

### 5.2 The dtype matrix (from the s3s3d3_tt validity predicates)

The arch-isa header spells the dtype legality out as the predicates the validator runs
(`aws_neuron_isa_tpb_s3s3d3_tt.h:50–227`, `common.h`). `[HIGH/OBSERVED — predicate bodies]`

```c
// is_valid_tensor_tensor(i): the per-operand dtype gates
//   out_dtype : is_valid_dtype_64(out, FP32R=True,  U64=True, I64=True)
//   src0      : is_valid_dtype_64(lo,  FP32R=False, U64=True, I64=True)
//   src1      : is_valid_dtype_64(hi,  FP32R=False, U64=True, I64=True)
//   && is_valid_enum(AluOp, op) && s3s3d3_tt_valid_op(i)
//   && s3s3d3_tt_dtype(i) && s3s3d3_tt_src_dst_dtype(i)
```

* **ARITH op (`0x41`):** `out_dtype` may be any of `{fp8_e3/e4/e5, fp16, bf16, fp32, fp32r,
  int8/16/32/64, uint8/16/32/64}` (FP32R **allowed** on out). `src0`/`src1` are the same set
  **minus FP32R** (sources cannot be the round-mode partial fp32 type). `[HIGH/OBSERVED]`
* **Int-signedness (`s3s3d3_tt_valid_int_signness`):** *only* applies when `op` is an int-aluop.
  A signed int op requires `src0`/`src1`/`out` **all signed-int**; an unsigned int op requires
  all three **unsigned-int**. (Shared ops accept either, consistently across all three.)
  `[HIGH/OBSERVED]`
* **BITVEC op (`0x51`):** `out_dtype` **must** be a valid INT dtype (`s3s3d3_tt_dtype` =>
  `is_valid_int_dtype(out)` — bitvec ops are integer-only). And `s3s3d3_tt_src_dst_dtype`
  requires `src0 == src1 == out` (dtype identity) — **except** for the shift sub-case below.
  `[HIGH/OBSERVED]`

```c
// s3s3d3_tt_src_dst_dtype(i)  — bitvec opcode only
//   !tensor_tensor_bitvec(i)                                    // arith opcode: vacuously true
//   || (dtype_lo == out_dtype && dtype_hi == out_dtype)         // identity rule
//   || (   is_shift_op(op)                                      // SHIFT special-case:
//       && is_valid_64b_int_dtype(dtype_lo)                     //   value operand is 64-bit int
//       && dtype_hi == UINT32)                                  //   shift-count operand is u32
```

> **QUIRK — shift ops break the bitvec dtype-identity rule.** For a *shift* op (`0x02`/`0x03`/
> `0x10`/`0x11`) on a 64-bit value, `src1` (the **shift count**) is allowed to be `UINT32` while
> `src0` is `INT64`/`UINT64` — the two sources are deliberately *different* dtypes. A
> reimplementation that hard-codes `src0==src1==out` for all bitvec ops will reject legal 64-bit
> shifts. `[HIGH/OBSERVED — s3s3d3_tt_src_dst_dtype body]`

---

## 6. Broadcast handling

There is **no dedicated broadcast-flag bit** in `S3S3D3_TT`. Broadcasting is expressed entirely
**through the per-operand `TENSOR3D` mem pattern** — the standard TPB addressing convention.
`[HIGH/OBSERVED — struct + the element-count predicates; MED that the device loop is exactly this]`

* Each src is a 3-D strided pattern `{start_addr, step_elem[3], num_elem[3]}`. A src is
  **broadcast along a dimension** by setting its `step_elem` (stride) for that dim to **0** while
  `num_elem` matches the other operand — the same element is re-read across that axis (a
  degenerate / size-1-effective stride). The handler does **not** special-case broadcast; the
  address generator (the strided IVP loads `ivp_la*`/`ivp_lv*`) re-reads the stride-0 element
  implicitly.
* The validator enforces **element-count compatibility**, not shape identity — which is what
  admits broadcast shapes:

```c
// s3s3d3_tt_src_element_cnt_check(i)   [s3s3d3_tt.h:116]
//   shape_from_register(src0.start_addr) || shape_from_register(src1.start_addr)   // dynamic shape => skip
//   || ( src0.num_elem[0]*[1]*[2] == src1.num_elem[0]*[1]*[2] )                    // total element product equal

// s3s3d3_tt_dst_element_cnt_check(i)   [s3s3d3_tt.h:123]
//   shape_from_register(src0.start_addr) || shape_from_register(dst.start_addr)
//   || ( src0.num_elem[0]*[1]*[2] == dst.num_elem[0]*[1]*[2] )
```

  The check is on the **product** of the three per-dim counts, so a `{N,1,1}` src and a
  `{1,N,1}` src (both product `N`) pass against a `{1,1,N}` dst — the kernel walks the dst
  iteration space and indexes each src by **its own** pattern, so a stride-0/size-1 src is
  naturally broadcast. When `start_addr` is a **shape register** (`shape_from_register`), the
  static element-count check is skipped because the shape is resolved dynamically.
  `[HIGH/OBSERVED — predicate bodies]`

* **Scalar broadcast is a separate opcode, not a flag.** The dedicated scalar-operand forms are
  *different* structs/opcodes that reuse the **same `NEURON_ISA_TPB_ALU_OP` table** (§3):
  - **Tensor-Scalar** — struct `S3D3_TS`, opcodes `TensorScalarArithOp` (`0x43`) /
    `TensorScalarBitvecOp` (`0x53`) — `src1` is a per-partition scalar.
    See [tensor-scalar.md](tensor-scalar.md).
  - **Scalar-Tensor-Tensor** — struct `S2S2D2_STT`, opcodes `ScalarTensorTensorArith` (`0x9d`) /
    `ScalarTensorTensorBitvec` (`0x9e`) — fused scalar-then-tensor-tensor.
    See [scalar-tensor-tensor.md](scalar-tensor-tensor.md).

  `instruction_mapping.json` confirms both `S3D3_TS` and `S2S2D2_STT` carry an
  `op:NEURON_ISA_TPB_ALU_OP` field — they are the cross-links to this page's table.
  `[HIGH/OBSERVED — struct2opcode; opcode constants common.h:170/171/236/237]`

> **CORRECTION — Scalar-Tensor-Tensor is `0x9d`/`0x9e`, not `0x43`/`0x53`.** `0x43`/`0x53` are the
> **Tensor-Scalar** opcodes; `ScalarTensorTensor*` are `0x9d`/`0x9e`. Both reuse the §3 ALU table;
> pin each opcode to its struct per `instruction_mapping.json`. `[HIGH/OBSERVED — header opcode enum + struct2opcode]`

---

## 7. Decode / execute flow (end-to-end)

```c
// POOL front-end dispatcher (entry 0x01005610) scans kernel_info_table, matches (opcode, spec):
//   0x51        -> callx8 0x0100105c -> decode_tensor_tensor_arith(idx)@0x01000f60   [HIGH route]
//   0x41        -> callx8 0x01000f1c  pool_tensor_tensor_arith_op (state 0x0200045c) [HIGH route]
//   0xf0/spec2  -> 0x01003484 -> callx8 0x010034b0
//                                 decode_extended_inst_tensor_tensor_arith(b,idx)     [HIGH route]

void decode_tensor_tensor_arith(unsigned int idx) {            // 0x01000f60
    const S3S3D3_TT *s = sbuf_instr(idx);                      // SBUF-resident instruction word

    // 1. pull the decode fields (FLIX-desync on the exact field-read encoding — struct is HIGH)
    Dtype  d0  = dtype_lo(s->in0_in1_dtype);
    Dtype  d1  = dtype_hi(s->in0_in1_dtype);
    Dtype  od  = s->out_dtype;
    AluOp  op  = s->op;
    uint8_t nc = s->num_active_channels;
    uint32_t n = num_tensor_elements(&s->src0_mem_pattern);    // product of num_elem[0..2]

    // DEBUG anchors (dbg_dram.bin):
    //   "P%i: TensorTensorArith num_chans = %0d"  /  "P%i: TensorTensorBitvec : num_chans = %0d"
    //   "P%i: ExtendedInstTensorTensorArith : num_tensor_elements = %d : alu_op = 0x%x"

    // 2. dtype branch -> 64-bit dispatch or 32-bit core (§5.1)
    if (is_valid_64b_int_dtype(od)) { /* tensor_tensor_64bit*_dispatch<...>(...) */ return; }

    // 3. the ALU-op switch (§3) — each arm emits the per-op IVP vector loop over the
    //    strided TENSOR3D patterns; each pool core partitions nc by get_cpu_id().
    tensor_tensor_arith_impl(idx, n, nc);                      // 0x01001280
    //   switch (op) {
    //     case ADD:  ... ivp_addn_2x32t ...   case MULT: ... ivp_mul* ...
    //     case MAX:  ... ivp_maxn_2x32 ...     ...  (all §3 arms)
    //     default:   "Error, Unimplemented tensor-tensor ALU op(0x%x)";   // §3.4
    //   }
}
```

`[HIGH for the route + struct + branch; MED for the exact switch-arm bodies — FLIX desync]`
Each pool core partitions `num_active_channels` across the 8 Q7 lanes by `get_cpu_id()`; the
512-bit (`2nx8`/`nx16`/`n_2x32`) IVP forms process a partition's element vector per FLIX bundle.

---

## 8. Cross-image stability

`extisa_MARIANA_PLUS_POOL_PERF_EXTISA_0` carries the **identical** tensor-tensor `.xt.prop`
chain — same names, VMAs offset by a small build delta:

| function | CAYMAN | MARIANA_PLUS | Δ |
|---|---|---|---|
| `decode_tensor_tensor_arith` | `0x01000f60` | `0x01000f60` | byte-identical |
| `setup_64bit_rw` | `0x01001108` | `0x01001114` | +0x0c |
| `tensor_tensor_arith_impl` | `0x01001280` | `0x010012a0` | +0x20 |
| `tensor_tensor_64bit_dispatch<Int64>` | `0x01001f7c` | `0x01001f9c` | +0x20 |
| `tensor_tensor_64bit_dispatch<Uint64>` | `0x01002720` | `0x01002740` | +0x20 |
| `tensor_tensor_64bit_bitvec_dispatch<Int64>` | `0x01002c2c` | `0x01002c4c` | +0x20 |
| `tensor_tensor_64bit_bitvec_dispatch<Uint64>` | `0x01002fc4` | `0x01002fe4` | +0x20 |
| `decode_extended_inst_tensor_tensor_arith` | `0x010034b0` | `0x010034d0` | +0x20 |

`[HIGH/OBSERVED — .xt.prop records, both images]` The handler family, the `S3S3D3_TT` struct,
and the `ALU_OP` enum are **stable** across the SUNDA/CAYMAN/MARIANA/MARIANA_PLUS (v2–v4)
generation that ships the `0xa260` EXTISA_0 image. SUNDA corroboration: the SUNDA POOL JSON maps
`pool_tensor_tensor_arith_op` → opcode `65` (`0x41`). v5/`MAVERICK` ships the same arch-isa
header layout (header-OBSERVED) — interior firmware bytes for v5 are **not** disassembled here;
flag any v5 device-body claim INFERRED.

---

## 9. Reimplementation checklist & honest limitations

A Vision-Q7-compatible tensor-tensor engine must:

1. Decode the **64-byte `S3S3D3_TT`** wire format field-exact: `op@14`, `in0_in1_dtype@12`
   (packed `dtype_lo:4`/`dtype_hi:4`), `out_dtype@13`, `num_active_channels@15`, three
   `TENSOR3D@16/32/48` (`{ADDR4, int16 step[3], uint16 num[3]}`). `[HIGH]`
2. Implement the **full 60-entry `ALU_OP` enum** (30 base `0x00..0x1D` + 30 int `0xC4..0xE1`) as
   a packed 1-byte selector, with the **two disjoint code ranges** (the int band is `0xC4+`, not
   a flag). `[HIGH]`
3. Gate legality with the exact predicates: bitvec opcode => `is_general_bitvec_op` (no CRC32);
   arith opcode => `is_general_arith_op || POW || is_valid_int_aluop`; int ops => signedness must
   match all three operands; **shift ops** allow `INT64`/`UINT64` value + `UINT32` count. `[HIGH]`
4. Split `INT64`/`UINT64` to the 64-bit template dispatchers (no native 64-bit ALU; synthesise
   from 32-bit halves); everything ≤32-bit through the 32-bit core. `[HIGH]`
5. Express broadcast through **stride-0 `TENSOR3D` dimensions** + element-count *product*
   compatibility — there is **no broadcast flag**. `[HIGH]`
6. Lower each op to the IVP vocabulary of §4 — but treat the precise per-op binding as `MED`/`LOW`
   (FLIX-desynced switch arms). `[MED]`

**Honest limitations.** No discrete `.rodata` op→address jump table exists — dispatch is a
compiled C++ switch with a string-anchored error default (§3.4). The switch-arm *bodies*
(`decode_tensor_tensor_arith`, `tensor_tensor_arith_impl`, the four 64-bit dispatchers,
`decode_extended_inst_*`) desync under stock `objdump` on the `4f 00 6f ..` / `5f ..`
literal-pool lead bytes; recovered & reported are the `entry` prologues (HIGH), the `.xt.prop`
function starts (HIGH/EXACT), the IVP vocabulary (MED), and the operand struct + ALU enum +
op-class predicates from the authoritative arch-isa headers (HIGH). The per-op IVP binding in §3
is the strongest correspondence the recovered mnemonics + header semantics support, never a
byte-exact jump-arm mapping. The extended-inst structs are header-flagged "not official
architecture" and `op`-restricted to add/multiply.

---

## Related pages

* [ALU-Op matrix](alu-op-matrix.md) — the full per-op × per-dtype support matrix (this page pins
  the *enum*; that page pins the *support cells*).
* [Tensor-Scalar (`S3D3_TS`, `0x43`/`0x53`)](tensor-scalar.md) — single-tensor + scalar form, same `op` table.
* [Scalar-Tensor-Tensor (`S2S2D2_STT`, `0x9d`/`0x9e`)](scalar-tensor-tensor.md) — fused scalar-then-TT, same `op` table.
* [BatchNorm BackProp (`0x65`, `S3S3D3_TT`)](batchnorm-backprop.md) — shares the *struct*; pins `op = Bypass`.
* ISA Vector-ALU reference: [b01 int/compare/logic](../../isa/ref/b01-vec-alu-int.md) ·
  [b02 fp16/fp32](../../isa/ref/b02-vec-alu-fp.md) ·
  [b03 int-compare / B-variant / predicated](../../isa/ref/b03-vec-alu-rest.md) — the IVP op vocabulary the ALU table lowers to.
