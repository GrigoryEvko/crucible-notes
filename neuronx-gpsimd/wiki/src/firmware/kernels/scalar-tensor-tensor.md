# Scalar-Tensor-Tensor (STT) — the 3-input fused elementwise op

`decode_scalar_tensor_tensor` / `NEURON_ISA_TPB_S2S2D2_STT_STRUCT` — the third and
widest member of the GPSIMD elementwise-arithmetic family. Where
[Tensor-Tensor](./tensor-tensor.md) fuses two tensors under one ALU op and
[Tensor-Scalar](./tensor-scalar.md) fuses one tensor with two scalars under two ALU
ops, **Scalar-Tensor-Tensor takes one broadcast scalar plus two tensors and folds
them through two ALU ops** — `(src0 op0 scalar) op1 src1`. It is the generic
two-stage fusion that, as one special case (`op0=MULT`, `op1=ADD`), realises a
fused multiply-add `dst = scalar·A + B`, but it is *not* a dedicated MAC: any pair
of general arithmetic (or general bit-vector) ops is legal.

STT is a Vector-Engine (DVE) op, dispatched as SEQ opcode `0x9d`/`0x9e`. It does
**not** appear in the Q7 POOL `kernel_info_table`; instead it runs on the DVE's
shared `alu_op.cpp` per-lane datapath as the `"S: Scalar-Tensor-Tensor"` worker.
This page pins the opcodes, the compile-verified 64-byte operand struct (shared
byte-for-byte with [TensorTensorScan](./tensor-tensor-scan.md) and SelectReduce),
the exact fused datapath, the AluOp op-set for each opcode, the dtype matrix, and
per-generation presence.

> **Provenance.** Every fact below derives from static analysis of the shipped
> GPSIMD device/host binaries (`libnrtucode_internal.so`, the carved DVE EXTISA
> images) and the shipped arch-ISA C headers + `instruction_mapping.json` that ship
> in the customop-lib package, read with stock binutils (`readelf`/`nm`/`strings`),
> `gcc` (compile-verify), `jq`, and the Cadence `xtensa-elf-objdump`
> (`XTENSA_CORE=ncore2gp`). DEBUG-build ASCII log strings are used purely as
> name/order anchors. FLIX/literal-pool desync spans are flagged honestly, never
> invented.

---

## 1. The opcodes — `ScalarTensorTensorArith` / `ScalarTensorTensorBitvec`

STT is **two opcodes sharing one operand struct**. From
`aws_neuron_isa_tpb_common.h` (cayman lines 236-237; identical value on
mariana/maverick/sunda):

```c
NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_ARITH  = 0x9d,    // Y  (157)
NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_BITVEC = 0x9e,    // Y  (158)
```

Both carry the `// Y` ("maintained") status flag on every generation. The `_ARITH`
form runs the float/general-arithmetic datapath (FP32-hub soft-float, §6); the
`_BITVEC` form runs the integer bit-vector datapath (int-only, identity dtype, §6).

The `S2S2D2_STT` struct backs **four** opcodes, the other two being siblings out of
this page's core scope (§3d):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH = 0xe5,    // Y  (229) — prefix scan
NEURON_ISA_TPB_OPCODE_SELECT_REDUCE            = 0xea,    // Y  (234) — select+max reduction
```

`instruction_mapping.json` (mariana lines 232-237; identical on all gens, read
verbatim) pins this 1-struct→4-opcode binding:

```json
"NEURON_ISA_TPB_S2S2D2_STT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_ARITH",
    "NEURON_ISA_TPB_OPCODE_SCALAR_TENSOR_TENSOR_BITVEC",
    "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_SCAN_ARITH",
    "NEURON_ISA_TPB_OPCODE_SELECT_REDUCE"
],
```

> `[HIGH/OBSERVED]` — opcodes `0x9d`/`0x9e`/`0xe5`/`0xea` read verbatim from
> `common.h` on cayman/mariana/maverick/sunda; the struct→opcode binding read
> verbatim from `instruction_mapping.json`.

### The elementwise-arith family at a glance

| family member | arith / bitvec | operand struct | inputs | op selectors |
|---|---|---|---|---|
| [Tensor-Tensor](./tensor-tensor.md) (TT) | `0x41` / `0x51` | `S3S3D3_TT` (64 B) | 2 tensor | **one** (`op` @14) |
| [Tensor-Scalar](./tensor-scalar.md) (TS) | `0x43` / `0x53` | `S3D3_TS` (64 B) | 1 tensor + 2 scalar | **two** (`op0`@36,`op1`@37) |
| **Scalar-Tensor-Tensor (STT)** | `0x9d` / `0x9e` | `S2S2D2_STT` (64 B) | **1 scalar + 2 tensor** | **two** (`op0`@36,`op1`@37) |

STT is the 3-input completion of the family: it keeps TS's two-AluOp +
`reverse_operands` control machinery, but swaps TS's *second scalar* for a *second
tensor*. `[HIGH/OBSERVED]`

---

## 2. The operand struct — `NEURON_ISA_TPB_S2S2D2_STT_STRUCT` (64 B, compile-verified)

From `aws_neuron_isa_tpb_s2s2d2_stt.h:24`. The header carries
`ISA_STATIC_ASSERT(sizeof == 64)`. **Self-compiled this task** (`gcc -I<gen>/tpb`,
`offsetof`/`sizeof` printed) — offsets are **byte-identical** on
cayman/mariana/maverick/sunda:

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode:1, inst_word_len:1, debug_cmd:1, debug_hint:1}`; `opcode`=`0x9d`/`0x9e` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 12 | `src0_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | **INPUT TENSOR A** — 2-D strided |
| 24 | 12 | `src1_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | **INPUT TENSOR B** — 2-D strided |
| 36 | 1 | `op0` | `NEURON_ISA_TPB_ALU_OP` | **first AluOp selector** |
| 37 | 1 | `op1` | `NEURON_ISA_TPB_ALU_OP` | **second AluOp selector** |
| 38 | 1 | `reverse_operands` | `NEURON_ISA_TPB_TENS_SCALAR_REV_OPS` | `{None0,First1,Second2,Both3}` |
| 39 | 1 | `imm0_src` | `NEURON_ISA_TPB_IMM_SRC` | **scalar source** `{Inst0,Ptr1,RegPtr2}` |
| 40 | 1 | `in0_in1_dtype` | `NEURON_ISA_TPB_DTYPE_PAIR` | `dtype_lo`=src0, `dtype_hi`=src1 (4+4 bit) |
| 41 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | destination dtype (FP32R allowed) |
| 42 | 1 | `num_active_channels` | `uint8_t` | partition/lane count 1..128 |
| 43 | 1 | `imm0_dtype` | `NEURON_ISA_TPB_DTYPE` | **scalar dtype** (FP32 for arith; §6) |
| 44 | 1 | `accumulator_cmd` | `NEURON_ISA_TPB_ACCUM_CMD` | `{Idle0,Zero1,Accumulate2,ZeroAccum3,LoadAccum4}` |
| 45 | 3 | `reserved[3]` | `uint8_t` | must be 0 (`has_s2s2d2_stt_reserved_zero`) |
| 48 | 12 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | **OUTPUT TENSOR** — 2-D strided |
| 60 | 4 | `imm0` | `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` | **the SCALAR value** (4-B union) |

Self-compile output (cayman; identical mariana/maverick/sunda):

```
sizeof=64
  header 0   events 4   src0 12   src1 24   op0 36   op1 37
  reverse_operands 38   imm0_src 39   in0_in1_dtype 40   out_dtype 41
  num_active_channels 42   imm0_dtype 43   accumulator_cmd 44   reserved 45
  dst_mem_pattern 48   imm0 60
TENSOR2D=12  HEADER=4  EVENTS=8  IMMVAL=4  DTYPEPAIR=1
```

`NEURON_ISA_TPB_TENSOR2D` (12 B, `common.h:643`):

```c
typedef struct {
    NEURON_ISA_TPB_ADDR4 start_addr;     // 4   partition-offset base
    int16_t              step_elem[2];   // 4   per-dim stride (signed; 0 => broadcast)
    uint16_t             num_elem[2];    // 4   per-dim element count
} NEURON_ISA_TPB_TENSOR2D;
```

> **NOTE — the two-dimensional pattern.** STT uses `TENSOR2D` (2-D) for all three
> patterns, *not* the `TENSOR3D` (3-D) that [Tensor-Tensor](./tensor-tensor.md) and
> [Tensor-Scalar](./tensor-scalar.md) use. The struct gives up one address-generator
> dimension per operand to make room for the second tensor pattern inside the same
> 64-byte envelope. `[HIGH/OBSERVED compile-verify + common.h:643]`

> **GOTCHA — the `imm0 @60` placement.** `dst_mem_pattern` is 12 bytes at offset
> 48, so it occupies bytes `48..59`; `imm0` then occupies `60..63`, total exactly
> 64. The header comments `// (48 - 60)` / `// (60 - 63)` use inclusive-of-next-start
> notation — there is **no overlap**, as the compile-verify confirms (`dst`@48,
> `imm0`@60, `sizeof==64`). Reading `imm0` as if it overlapped `dst` is a decode
> bug. `[HIGH/OBSERVED]`

### The three operands, byte-pinned

- **SCALAR** = `imm0 @60`, a 4-byte union `NEURON_ISA_TPB_IMM_VAL_INST_FIELD`
  (`common.h:866`):

  ```c
  typedef union {
      NEURON_ISA_TPB_PARTITION_OFFSET imm_ptr;            // for PointerImmediate
      NEURON_ISA_TPB_IMM_REG          imm_reg;            // for RegPtrImmediate
      float                           imm_arith_fp32;     // arith inline scalar (FP32, §6)
      int32_t                         imm_bitvec_int32;   // bitvec inline scalar
      uint32_t                        imm_bitvec_uint32;
      uint16_t                        imm_bitvec_uint16[2];
      uint8_t                         imm_bitvec_uint8[4];
  } NEURON_ISA_TPB_IMM_VAL_INST_FIELD;
  ```

  The scalar's *source* is `imm0_src @39` (`{InstructionImmediate=0, PointerImmediate=1,
  RegPtrImmediate=2}`) and its *dtype* is `imm0_dtype @43`. STT has **exactly one**
  immediate field — *not* two as Tensor-Scalar has. This single-immediate property
  is the structural difference from TS. `[HIGH/OBSERVED]`

- **TENSOR A** = `src0_mem_pattern @12` (`TENSOR2D`), dtype = `in0_in1_dtype.dtype_lo`.
- **TENSOR B** = `src1_mem_pattern @24` (`TENSOR2D`), dtype = `in0_in1_dtype.dtype_hi`.
- **DST** = `dst_mem_pattern @48` (`TENSOR2D`), dtype = `out_dtype`.

### TWO op-selector fields — the "one or two?" answer is **two**

`op0 @36` and `op1 @37`, each a full `NEURON_ISA_TPB_ALU_OP` (1 byte). This is
*unlike* [Tensor-Tensor](./tensor-tensor.md) (one `op` field) and *like*
[Tensor-Scalar](./tensor-scalar.md) (`op0`+`op1`). The two ops are the two fusion
stages (§3). `[HIGH/OBSERVED — two distinct ALU_OP fields, compile-verified offsets
36 & 37]`

---

## 3. The exact fused op — the two-AluOp composition of `{scalar, A, B}`

### 3a. The composition

STT is a **two-AluOp fusion of three operands**: one broadcast scalar (`imm0`) and
two tensors (`src0`, `src1`). The `reverse_operands` field selects the per-stage
operand order. The canonical composition is the `tensor_scalar reverse operands
options` block read verbatim from `common.h:1239`, applied to the STT roster where
the `scalar[0]` slot is `imm0` and the trailing operand is the **second tensor**
`src1`:

```
reverse_operands == NONE   (0):  dst = (src0   op0 scalar)  op1 src1
reverse_operands == FIRST  (1):  dst = (scalar op0 src0  )  op1 src1   <- op0 operands swapped
reverse_operands == SECOND (2):  dst = src1 op1 (src0   op0 scalar)    <- op1 operands swapped
reverse_operands == BOTH   (3):  dst = src1 op1 (scalar op0 src0  )    <- both swapped
```

So **`op0` fuses the broadcast scalar with tensor `src0`**; **`op1` then fuses that
intermediate with the second tensor `src1`**. `reverse_operands` flips operand order
per stage — the identical scalar-src-vs-src-scalar control that
[Tensor-Scalar](./tensor-scalar.md) documents.

> `op0`/`op1` existence + the `reverse_operands` enum are `[HIGH/OBSERVED]` (struct
> + `common.h:1246`). The binding "scalar pairs with `src0` in `op0`, `src1` enters
> at `op1`" follows from the canonical scalar-tensor-tensor semantics applied to the
> struct + the verbatim `reverse_operands` template — `[HIGH]` for the two-stage
> *structure* (the device runs two sequential window-program + compute passes, §4),
> `[MED]` for *which src indexes which stage* through the FLIX desync (§11).

`NEURON_ISA_TPB_TENS_SCALAR_REV_OPS` (`common.h:1246`):

```c
NEURON_ISA_TPB_TENS_SCALAR_REV_OPS_NONE   = 0x00,
NEURON_ISA_TPB_TENS_SCALAR_REV_OPS_FIRST  = 0x01,
NEURON_ISA_TPB_TENS_SCALAR_REV_OPS_SECOND = 0x02,
NEURON_ISA_TPB_TENS_SCALAR_REV_OPS_BOTH   = 0x03,
```

### 3b. It is generic — *not* a hard-wired multiply-add

`op0` and `op1` are each an *arbitrary* AluOp. `has_valid_scalar_tensor_tensor_op`
(`s2s2d2_stt.h:200`) constrains them by opcode class:

```c
// has_valid_scalar_tensor_tensor_op(i):
//   ARITH (0x9d):  is_general_arith_op(op0)  && is_general_arith_op(op1)
//   BITVEC(0x9e):  is_general_bitvec_op(op0) && is_general_bitvec_op(op1)
```

The two predicates resolve from the float `NEURON_ISA_TPB_ALU_OP` enum
(`common.h:939`):

- **`is_general_arith_op`** = `is_arith_op` minus `{Divide, Pow, Mod, Rsqrt}` and minus
  *all* `is_valid_int_aluop` members (the `*_INT`/`*_UINT` integer-engine ops
  `0xC4..0xE1`). The resulting general-arith set is **per-gen: 17 ops on SUNDA/CAYMAN, 21 on
  MARIANA+/MAVERICK** (MARIANA+ adds `AbsMax 0x20`, `AbsMin 0x21`, `ReLU 0x22`, `Square 0x23`
  — see the [ALU-op matrix §3.1](./alu-op-matrix.md), the canonical home of this count). The
  **17-op SUNDA/CAYMAN core**:

  | op | code | | op | code | | op | code |
  |---|---|---|---|---|---|---|---|
  | `Bypass` | 0x00 | | `Min` | 0x09 | | `IsGE` | 0x14 |
  | `Add` | 0x04 | | `LogicalAnd` | 0x0D | | `IsLE` | 0x15 |
  | `Subtract` | 0x05 | | `LogicalOr` | 0x0E | | `IsLT` | 0x16 |
  | `Mult` | 0x06 | | `LogicalXor` | 0x0F | | `AbsoluteDiff` | 0x17 |
  | `Max` | 0x08 | | `IsEQ` | 0x12 | | `IsNE` | 0x18 |
  |  |  | | `IsGT` | 0x13 | | `AbsoluteValue` | 0x19 |

  On **MARIANA/MARIANA_PLUS/MAVERICK** the four extra float AluOps `AbsMax 0x20`, `AbsMin 0x21`,
  `ReLU 0x22`, `Square 0x23` are real enumerators in the ALU_OP enum, are in `is_arith_op`, are
  not special-excluded, and are not in `is_valid_int_aluop` — so they join the set, giving **21**.

- **`is_general_bitvec_op`** = `is_bitvec_op` minus `{Crc32 0x1C}`, i.e. **9 ops**:
  `Bypass 0x00`, `BitwiseNot 0x01`, `ArithShiftLeft 0x02`, `ArithShiftRight 0x03`,
  `LogicalShiftLeft 0x10`, `LogicalShiftRight 0x11`, `BitwiseAnd 0x0A`,
  `BitwiseOr 0x0B`, `BitwiseXor 0x0C`.

> **CORRECTION (per-gen) vs SX-FW-53.** The backing report's general-arith list appends
> "(+AbsMax/AbsMin/ReLU/Square on the gens that define them)" — and that *gen-qualified* form is
> correct. An earlier draft of this page over-corrected it to "no `ReLU`/`Square`,
> no `AbsMax`/`AbsMin` on **all four** gens", which is wrong: only **SUNDA/CAYMAN** lack those
> float AluOps (there they exist solely as `*_INT` variants `ABS_MAX_INT 0xCB` / `ABS_MIN_INT 0xCC`,
> which `is_valid_int_aluop` excludes — giving the 17-op set above). **MARIANA/MARIANA_PLUS/MAVERICK**
> add the float enumerators `ABS_MAX = 0x20`, `ABS_MIN = 0x21`, `RE_LU = 0x22`, `SQUARE = 0x23` to
> the ALU_OP enum and to `is_arith_op`; none are special-excluded, so general-arith there is **21**.
> So the STT general-arith set is **17 (SUNDA/CAYMAN) / 21 (MARIANA+)**, harmonized with the
> canonical [ALU-op matrix §3.1](./alu-op-matrix.md). `[HIGH/OBSERVED — common.h ALU_OP enum +
> `is_arith_op`/`is_general_arith_op`/`is_valid_int_aluop` bodies, per-gen]`

The fused-multiply-add `dst = scalar·src0 + src1` is therefore the *special case*
`op0=MULT(0x06)`, `op1=ADD(0x04)`, `reverse_operands=NONE`. STT is the **generic
superset** of that: any `(op0,op1)` pair from the general-arith (arith) /
general-bitvec (bitvec) class. It does **not** map to a single
`ivp_macn`/`ivp_2xfmac` MAC intrinsic; it maps to **two sequential per-lane AluOp
passes** through the shared `alu_op.cpp` datapath (§4), the same datapath
[Tensor-Tensor](./tensor-tensor.md) and [Tensor-Scalar](./tensor-scalar.md) use. The
anchor is the [ALU-op matrix](./alu-op-matrix.md), not a dedicated MAC opcode.
`[HIGH/OBSERVED]`

> **NOTE — no `op0!=Bypass` guard.** The `s3d3_ts.h` Tensor-Scalar header has a
> `tensor_scalar_valid_ops` ordering constraint (`(op0!=Bypass)||(op1==Bypass)`).
> `s2s2d2_stt.h` carries **no** STT-specific equivalent — both ops must merely be
> *independently* general-arith/bitvec. Since `Bypass` is itself a general op,
> `op0=Bypass, op1=Add` is structurally permitted (a single-tensor-stage degenerate:
> `dst = src0 + src1`, scalar ignored at stage 0). `[HIGH/OBSERVED — header has no
> STT op0!=Bypass guard]`

### 3c. The per-element trace evidence

The STT worker funnels into the same `alu_op.cpp` per-element dispatcher that
[Tensor-Tensor](./tensor-tensor.md) / [Tensor-Scalar](./tensor-scalar.md) use. That
dispatcher carries two distinct DEBUG trace forms, **both present** in the STT-reaching
DVE image (confirmed in the shipped `libnrtucode_internal.so` via `strings`):

```
"S: OP=%x R[%d] = OP(R[%d], imm)"      <- the SCALAR-immediate AluOp  (op0 with imm0)
"S: OP=%x R[%d] = OP(R[%d], R[%d])"    <- the TENSOR-tensor   AluOp  (op1 with the 2nd src)
"S: Scalar-Tensor-Tensor"              <- the worker self-name (DVE DRAM)
```

STT uses **both** forms — unlike Tensor-Scalar, which uses only the
`R[d]=OP(R[d],imm)` form. One stage folds the scalar (the `imm` trace), the other
folds the second tensor (the `R,R` trace). This is the decisive evidence that STT
mixes a scalar-imm op **and** a tensor-tensor op inside one instruction.
`[HIGH/OBSERVED — all three strings present in the shipped binary]`

### 3d. The siblings on the same struct (out of core scope)

- **`TensorTensorScanArith` (`0xe5`)** — same struct, but a **prefix scan**:
  `has_zero_accum_cmd` is *forced* (`accumulator_cmd == 0`); `op0`/`op1` both
  general-arith (`has_valid_tensor_tensor_scan_op`); the running accumulator carries
  `dst[i] = acc op src` across the tensor (the `imm0` is the scan seed). See
  [TensorTensorScan](./tensor-tensor-scan.md) — that page shares this struct
  byte-for-byte. `[HIGH/OBSERVED — is_valid_tensor_tensor_scan]`
- **`SelectReduce` (`0xea`)** — same struct, **constrained ops**:
  `has_valid_select_reduce_op` *forces* `op0==Bypass && op1==Max`, and
  `has_valid_select_reduce_rev_ops` restricts `reverse_operands` to `{None, First}` —
  a select-then-max reduction. Its dtypes go through `has_valid_select_reduce_dtypes`
  (`dtype_lo` int-datapath **and** `dtype_hi` **not** a 32-bit int) and the
  `has_valid_s2s2d2_cpr_accum_cmd` accum contract (`{Idle, ZeroAccumulate,
  Accumulate}`). `[HIGH/OBSERVED — is_valid_select_reduce]`

---

## 4. The dispatch chain — opcode → DVE kernel_info → funcVA → worker body

STT runs on the **DVE engine** (Vector Engine), indicated by the `"S:"` worker
prefix and the NKI engine column. There is **no STT row in the Q7 POOL
`kernel_info_table`**, and `nm libnrtucode_internal.so | rg -i scalar_tensor_tensor`
returns **zero hits** — STT is a static DVE worker embedded inside the carved EXTISA
image, not an exported POOL compute kernel (§9). `[HIGH/OBSERVED]`

The dispatch mirrors [Tensor-Scalar](./tensor-scalar.md) exactly: **two workers** (an
arith twin + a bitvec twin), each registered by a
`const16-funcVA / s32i-[a1+12] / call8-<DVE-register>` stub. Byte-decoded MARIANA
NX_DVE DEBUG IRAM:

```
; THE TWO REGISTRATION STUBS (MARIANA, byte-exact)
0x220c: entry a1,48 ; const16 a2,0 ; const16 a2,0xb0f8 ; s32i a2,[a1+12]
        l32r a11,0xfffc8368 ; l32r a2,0xfffc38e8 ; call8 0x9920 ; retw   ; -> STT worker #1 @0xb0f8
0x2228: entry a1,48 ; const16 a2,0 ; const16 a2,0xb23c ; s32i a2,[a1+12]
        l32r a11,0xfffc83a0 ; l32r a2,0xfffc3920 ; call8 0x9920 ; retw   ; -> STT worker #2 @0xb23c
```

`call8 0x9920` is the MARIANA DVE kernel-register routine. The negative `l32r`
literals (`0xfffc8368`, `0xfffc38e8`, …) resolve **outside** the carved IRAM into
the runtime-bound opcode-`0x9d`/`0x9e`→descriptor table — so the opcode→funcVA
binding is runtime-bound, **not** statically pinned in this carve (the same FW-50/FW-42
out-of-carve limitation).

```
; THE WORKER MAP (MARIANA; entry prologues xxd-verified `00 36 a1 00` = entry a1,80)
funcVA   entry        self-name (DRAM 0x20c0)        role
0xb0f8   entry a1,80  "S: Scalar-Tensor-Tensor"      STT worker #1 (arith config)
0xb23c   entry a1,80  "S: Scalar-Tensor-Tensor"      STT worker #2 (bitvec config)
0xb218   entry a1,48  (imm0 RegPtr reg-fetch sub-handler, called from worker#1 @0xb1d5)
```

The worker body (MARIANA `0xb0f7`, byte-decoded as far as the FLIX desync allows):

```
b0f7: entry a1,80
b0fe: const16 a10,8 ; const16 a10,0x20c0 ; call8 0x188a4    ; LOG "S: Scalar-Tensor-Tensor"
b112: call0 0x1c3a4                                          ; DVE setup helper
b11c: s8i a2,[a1+40] ; s8i a3,[a1+44]                        ; two single-bit config flags
b12b: const16 a2,0x2250 ; l32i ... -> s32i [a1+28/24/20/16]  ; copy 16-B descriptor block
b154: s16i a11,[a4+176]   ; b159: call0 0xfffbc24c           ; STAGE 1: window-program + compute (op0)
b1a0: s16i a11,[a4+0x130] ; b1a5: call0 0xfffbe298           ; STAGE 2: window-program + compute (op1)
b1cf: bnei a2,2,0xb1dd -> b1d5: call8 0xb218                 ; IMM_SRC: imm0_src==RegPtr? reg-fetch
b200..b213: accumulator_cmd handling (bnei a2,2 -> movi a10,-1); the Accum/ZeroAccum path (§5)
b20e: retw                                                   ; worker RETURNS
```

So the macro-shape is: **log → setup → copy descriptor → TWO sequential
(window-program + `alu_op.cpp` compute) passes** (the two AluOp stages `op0`, `op1`)
**→ resolve the single scalar source → accumulator handling → return.** The two
`s16i` window-programs at `[a4+176]` and `[a4+0x130]` are the two distinct FLIX
vector windows; the single `bnei a2,2` is the lone `RegPtrImmediate` test (one
immediate, not two).

> Stub edges, the entry/log edges, the two-stage `s16i` window-program, and the
> single `bnei` imm-src test are `[HIGH/OBSERVED]`. The inner FLIX compute bundles
> are `[MED]` through the SX-FW-00 desync; the two `call0 0xfffb…` compute targets
> resolve out-of-carve into the shared `alu_op.cpp` datapath, so the exact compute
> body is `[LOW]`. The opcode→descriptor literal is runtime-bound, `[LOW/out-of-carve]`.

Assembled canonical chains:

```
ScalarTensorTensorArith  : SEQ 0x9d -> DVE kernel_info slot (registered by stub 0x220c)
                         -> funcVA 0xb0f8 -> "S: Scalar-Tensor-Tensor" worker
                         -> stage1 op0(scalar,src0) + stage2 op1(.,src1) via alu_op.cpp
                         -> imm0 source dispatch (0xb218) -> accum handling.
ScalarTensorTensorBitvec : SEQ 0x9e -> DVE kernel_info slot (registered by stub 0x2228)
                         -> funcVA 0xb23c -> "S: Scalar-Tensor-Tensor" worker (bitvec config).
```

> The worker#1(arith)/#2(bitvec) assignment is `[MED/INFERRED]` from the twin-body
> config divergence (two stubs register two funcVAs; the exact opcode each registers
> lives in the out-of-carve descriptor).

---

## 5. The 3-operand fetch — the scalar source + the two tensor patterns

### 5a. The scalar (`imm0`) — `imm0_src @39`

`NEURON_ISA_TPB_IMM_SRC` (`common.h:1207`):

```c
NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,   // fp32 value inline in the instruction
NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,   // PartitionOffset pointer to immediates
NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,   // PartitionOffset pointer held in a register
```

`has_valid_s2s2d2_stt_immediate` (`s2s2d2_stt.h:142`):

```c
// has_valid_s2s2d2_stt_immediate(i):
//      (imm0_src == InstructionImmediate)                                          // inline: unconditional
//   || (imm0_src == PointerImmediate
//        && tpb_addr_active_channels(imm0.imm_ptr, num_active_channels)
//        && addr_aligned_dtype(imm0.imm_ptr, imm0_dtype))                          // per-partition scalar ptr
//   || (imm0_src == RegPtrImmediate
//        && is_valid_imm_reg(imm0.imm_reg))                                         // register-held ptr
```

On the device this is realised by the single `bnei a2,2` test (MARIANA `0xb1cf`,
`66 22 0a` — byte-identical to the FW-39/40/50 `RegPtrImmediate` reg-fetch test):
`imm0_src==RegPtr` branches to the reg-fetch sub-handler `@0xb218`. **Exactly one**
`bnei` test → exactly one immediate. This is the structural difference from
[Tensor-Scalar](./tensor-scalar.md), which has two immediates / two `bnei` tests.
`[HIGH/OBSERVED]`

### 5b. The two tensors — `src0 @12` + `src1 @24` (`TENSOR2D`)

Each is validated by `tensor2d_valid(pattern, dtype, WriteTensor::False,
AllowedInPSUM::True, AllowedInSBUF::True)` — so **either source may read matmul PSUM
directly** (no SBUF copy needed). The element counts are cross-checked:

```c
// s2s2d2_stt_src_element_cnt_check(i):  (unless either start_addr is shape_from_register)
//   src0.num_elem[0]*num_elem[1] == src1.num_elem[0]*num_elem[1]
// s2s2d2_stt_dst_element_cnt_check(i):  (unless src0 or dst start_addr is shape_from_register)
//   src0.num_elem[0]*num_elem[1] == dst.num_elem[0]*num_elem[1]
```

There is **no broadcast-flag bit**: a source broadcasts (if at all) via a `step_elem`
(stride) of `0` in its `TENSOR2D` pattern — the same convention
[Tensor-Tensor](./tensor-tensor.md) uses. `tt_valid_partitions(src0.start_addr,
src1.start_addr)` further constrains the two sources' partition placement. `[HIGH/OBSERVED]`

### 5c. The accumulator contract — `accumulator_cmd @44`

`NEURON_ISA_TPB_ACCUM_CMD` (`common.h:777`):
`{Idle 0, Zero 1, Accumulate 2, ZeroAccumulate 3, LoadAccumulate 4}`.
`has_valid_s2s2d2_stt_accum_cmd` (`s2s2d2_stt.h:234`):

```c
// BITVEC (0x9e): accumulator_cmd MUST == Idle(0)
// ARITH  (0x9d): accumulator_cmd in {Idle(0), Accumulate(2), ZeroAccumulate(3)}
```

So **STT-arith may engage the DVE running accumulator** (`Accumulate` /
`ZeroAccumulate`) — unlike plain [Tensor-Scalar](./tensor-scalar.md), which forces
`Idle`. The device's `@0xb200` `bnei a2,2` + `movi a10,-1` is this accum branch.
`[HIGH/OBSERVED header; the device accum branch MED]`

---

## 6. The dtype matrix — the FP32-hub soft-float path

From `is_valid_scalar_tensor_tensor` + the dtype predicates (`s2s2d2_stt.h:54-82,
186-232`).

### ARITH (`0x9d`)

```c
// src0 dtype = in0_in1_dtype.dtype_lo : is_valid_dtype(.., AllowFP32R::False)
// src1 dtype = in0_in1_dtype.dtype_hi : is_valid_dtype(.., AllowFP32R::False)
// imm0_dtype                          : is_valid_dtype(.., AllowFP32R::False)
// out_dtype                           : is_valid_dtype(.., AllowFP32R::True)   <- FP32R allowed on OUT only
// has_valid_scalar_tensor_tensor_dtypes : arith is UNCONSTRAINED (any valid mix)
// s2s2d2_stt_src_dst_dtype              : arith is UNCONSTRAINED (no in==out identity)
```

The FP32-hub rule lives in `s2s2d2_stt_imm0_dtype` (`s2s2d2_stt.h:228`):

```c
// s2s2d2_stt_imm0_dtype(i):
//      has_s2s2d2_stt_bitvec_op(i)                       // bitvec: handled below
//   || (is_const_ptr_dve(imm0_src)                       // ptr/reg-ptr ⇒ dtype taken from memory
//        || (imm0_dtype == Dtype::FP32))                 // inline ⇒ MUST be FP32
```

⇒ an **inline** arith scalar immediate is **always FP32** — the same FP32-hub
soft-float convention [Tensor-Tensor](./tensor-tensor.md) /
[Tensor-Scalar](./tensor-scalar.md) follow (the arith datapath converts to/through
FP32). A pointer- or reg-ptr-sourced scalar takes its dtype from memory
(`is_const_ptr_dve` covers both `PointerImmediate` and `RegPtrImmediate`) and so is
exempt. `[HIGH/OBSERVED — s2s2d2_stt.h:228]`

### BITVEC (`0x9e`)

```c
// has_valid_scalar_tensor_tensor_dtypes : src0, src1, imm0, out ALL is_valid_int_dtype_datapath (int only)
// s2s2d2_stt_src_dst_dtype              : src0==out && src1==out && imm0_dtype==out  (IDENTITY — all four equal)
// accumulator_cmd                       : MUST be Idle (no accumulation for bitvec)
```

`[HIGH/OBSERVED — s2s2d2_stt.h:221, 186]`

### Dtype ordinals — `NEURON_ISA_TPB_DTYPE` (4-bit, `common.h:722`)

| code | dtype | code | dtype | code | dtype | code | dtype |
|---|---|---|---|---|---|---|---|
| 0x0 | INVALID | 0x4 | INT16 | 0x8 | INT32 | 0xC | INT64 |
| 0x1 | UINT64 | 0x5 | UINT16 | 0x9 | UINT32 | 0xD | FP8_EXP3 |
| 0x2 | INT8 | 0x6 | BF16 | 0xA | FP32 | 0xE | FP8_EXP4 |
| 0x3 | UINT8 | 0x7 | FP16 | 0xB | FP32R | 0xF | FP8_EXP5 |

`in0_in1_dtype` packs `dtype_lo`(src0) and `dtype_hi`(src1) as two 4-bit fields
(`NEURON_ISA_TPB_DTYPE_PAIR`, 1 byte). `num_active_channels` is validated by
`check_active_channels` (1..128, the DVE 128-lane width). `[HIGH/OBSERVED]`

> **NOTE.** The per-element trace `R[d]=OP(R[d],imm)` / `R[d]=OP(R[d],R[d])` shows the
> compute operates on register-resident lanes (`R[]`) — the FP32-hub staging
> (convert-in / convert-out around the FP32 compute) happens inside the shared
> `alu_op.cpp` datapath, the same path the rest of the family uses. `[HIGH]`

---

## 7. The algorithm — annotated C pseudocode

```c
/* decode_scalar_tensor_tensor — the DVE "S: Scalar-Tensor-Tensor" worker.
 * Backs ScalarTensorTensorArith (0x9d) and ScalarTensorTensorBitvec (0x9e).
 * Real symbols: NEURON_ISA_TPB_S2S2D2_STT_STRUCT, NEURON_ISA_TPB_ALU_OP,
 *               has_valid_scalar_tensor_tensor_op, s2s2d2_stt_imm0_dtype,
 *               has_valid_s2s2d2_stt_accum_cmd, alu_op.cpp per-lane dispatch.
 * Structure HIGH/OBSERVED (entry/log/two-stage/imm-src/accum/return byte-decoded);
 * inner FLIX compute MED through the SX-FW-00 desync.
 */
void decode_scalar_tensor_tensor(const NEURON_ISA_TPB_S2S2D2_STT_STRUCT *i)
{
    /* 1. Decode operands from the 64-B struct (offsets compile-verified, §2). */
    const bool is_bitvec = (i->header.opcode == 0x9e);

    /* 2. Resolve the SINGLE broadcast scalar per imm0_src (the lone bnei a2,2 @0xb1cf). */
    scalar_t s;
    switch (i->imm0_src) {                                  /* NEURON_ISA_TPB_IMM_SRC */
        case INSTRUCTION_IMMEDIATE:                         /* inline imm0 union @60 */
            /* arith: imm0.imm_arith_fp32 (s2s2d2_stt_imm0_dtype forces FP32);
               bitvec: imm0.imm_bitvec_* (dtype == out_dtype, identity). */
            s = read_inline_imm(&i->imm0, i->imm0_dtype, is_bitvec);
            break;
        case POINTER_IMMEDIATE:                             /* PartitionOffset ptr; dtype from mem */
            s = deref_partition_ptr(i->imm0.imm_ptr, i->imm0_dtype, i->num_active_channels);
            break;
        case REG_PTR_IMMEDIATE:                             /* device: call8 0xb218 reg-fetch */
            s = deref_partition_ptr(reg_ptr(i->imm0.imm_reg), i->imm0_dtype,
                                    i->num_active_channels);
            break;
    }
    /* BROADCAST the scalar across the DVE lanes via the IVP replicate family
     * (ivp_rep2nx8t / ivp_repnx16t / ivp_repn_2x32t — vocabulary byte-OBSERVED in the
     * shared DVE PERF datapath, bundled in the same FLIX word as the AluOp). */
    vec_t splat = ivp_replicate(s, i->imm0_dtype);

    /* 3. Walk the dst iteration space tile-by-tile across num_active_channels (128 lanes).
     *    Each src/dst indexed by its own 2-D TENSOR2D pattern (step_elem/num_elem). */
    for_each_tile (i->dst_mem_pattern, i->num_active_channels) {
        vec_t a = load_tile(i->src0_mem_pattern, dtype_lo(i->in0_in1_dtype));  /* tensor A */
        vec_t b = load_tile(i->src1_mem_pattern, dtype_hi(i->in0_in1_dtype));  /* tensor B */

        /* 4a. STAGE 1 (op0): program FLIX window [a4+176]; fuse the splatted SCALAR
         *     with tensor A through the per-lane alu_op.cpp compute.
         *     ("S: OP=%x R[%d] = OP(R[%d], imm)" trace.) */
        vec_t mid;
        switch (i->reverse_operands) {                      /* TENS_SCALAR_REV_OPS */
            case NONE: case SECOND: mid = alu_op(i->op0, a,     splat); break; /* src0 op0 scalar */
            case FIRST: case BOTH:  mid = alu_op(i->op0, splat, a    ); break; /* scalar op0 src0 */
        }

        /* 4b. STAGE 2 (op1): program FLIX window [a4+0x130]; fuse the intermediate
         *     with tensor B. ("S: OP=%x R[%d] = OP(R[%d], R[%d])" trace.) */
        vec_t r;
        switch (i->reverse_operands) {
            case NONE: case FIRST:  r = alu_op(i->op1, mid, b  ); break;       /* mid op1 src1 */
            case SECOND: case BOTH: r = alu_op(i->op1, b,   mid); break;       /* src1 op1 mid */
        }

        /* 5. ACCUMULATE (arith only): fold into the DVE running accumulator if requested
         *    (device branch @0xb200); else write to dst. Bitvec forces Idle. */
        if (!is_bitvec &&
            (i->accumulator_cmd == ACCUMULATE || i->accumulator_cmd == ZERO_ACCUMULATE))
            dve_accumulate(i->accumulator_cmd, r);
        else
            store_tile(i->dst_mem_pattern, r, i->out_dtype);  /* FP32-hub convert-out */
    }
}
```

> Structure `[HIGH/OBSERVED]` — two sequential window-program + compute passes, the
> single imm-src test, the accum branch, all byte-decoded in MARIANA
> `0xb0f7..0xb238`. The exact src↔stage binding under `reverse_operands` is `[MED]`
> through the FLIX desync. The IVP splat vocabulary is `[HIGH/OBSERVED]`; its binding
> to the `op0` stage specifically is `[MED]`.

---

## 8. Per-generation presence

| gen | opcodes `0x9d`/`0x9e` | `S2S2D2_STT` struct | `"S: Scalar-Tensor-Tensor"` DVE str | workers | wired? |
|---|---|---|---|---|---|
| **SUNDA** | defined (Y/Y) | 64 B, compile-id | *(no DVE DEBUG image carved here)* | n/a | defined |
| **CAYMAN** | defined (Y/Y) | 64 B, compile-id | DRAM `0x1fdf` | `0xac08`, `0xad48` (stubs `0x236a`/`0x2386`, reg `call8 0x951c`) | WIRED |
| **MARIANA** | defined (Y/Y) | 64 B, compile-id | DRAM `0x20c0` | `0xb0f8`, `0xb23c` (stubs `0x220c`/`0x2228`, reg `call8 0x9920`) | WIRED |
| **MARIANA_PLUS** | defined (Y/Y) | 64 B, compile-id | DRAM (corroborated, +build delta) | (stable) | WIRED |
| **MAVERICK** | defined (Y/Y) | 64 B, compile-id | DRAM `0x20ef` | `0xad84`, `0xaf00` (stubs `0x2076`/`0x208e`) | WIRED |

Two STT workers per gen (arith + bitvec twins), each registered by a
`const16-funcVA / s32i-[a1+12] / call8-<DVE-register>` stub. The struct is
byte-identical (self-compile-verified offsets) on all four header sets; the opcodes
`0x9d`/`0x9e` are `// Y` maintained on all four. Each gen's per-element trace strings
(CAYMAN `0x2ab4`/`0x2ad5`, MAVERICK `0x2bc4`/`0x2be5`) carry **both** the `imm` and
the `R,R` forms — the STT scalar+tensor mix is present on every generation.

> **v5 / MAVERICK flag.** The MAVERICK (NC-v5) struct and opcode definitions are
> **header-OBSERVED** (the shipped `neuron_maverick_arch_isa` headers; the struct body
> `diff`s byte-identical to cayman). The MAVERICK DVE worker *interiors* are
> `[INFERRED]` from the same DEBUG-string + stub pattern as the byte-grounded
> v2–v4 carves; v5 interiors are not independently byte-walked. `[struct/opcode
> HIGH/OBSERVED; v5 worker interior INFERRED]`

STT is a **core DVE op present on every DVE-equipped generation**: SUNDA defines the
opcode + struct; the DVE worker is wired CAYMAN→MAVERICK. `[HIGH/OBSERVED]`

---

## 9. The POOL-vs-DVE path split

The elementwise-arith family has a POOL (Q7 `kernel_info_table`) surface and a DVE
(`"S:"` worker) surface. The three members split as:

| member | POOL `kernel_info_table` | DVE `"S:"` worker |
|---|---|---|
| [Tensor-Tensor](./tensor-tensor.md) (`0x41`/`0x51`) | yes (Q7 POOL compute kernel) | — |
| [Tensor-Scalar](./tensor-scalar.md) (`0x43`/`0x53`) | yes (`pool_tensor_scalar_arith_op`) | yes (`"S: Tensor-Scalar"`) |
| **Scalar-Tensor-Tensor (`0x9d`/`0x9e`)** | **no** | **yes (`"S: Scalar-Tensor-Tensor"`)** |

STT is **DVE-only** in the carved images: the `"S: Scalar-Tensor-Tensor"` worker
exists on the DVE; there is **no** STT row in the Q7_POOL `kernel_info_table`
matrix, and `nm`/`strings` find **no** `pool_scalar_tensor_tensor` symbol
(confirmed: `nm -C libnrtucode_internal.so | rg -ci scalar_tensor_tensor` ⇒ 0). So
STT is dispatched as a SEQ opcode `0x9d`/`0x9e` to the DVE engine, **not** through
the Q7 POOL `kernel_info_table`. This is consistent with the NKI engine table
listing `scalar_tensor_tensor_{arith,bitvec}` under the Vector Engine. `[HIGH/OBSERVED]`

There is also **no discrete DVE per-op jump table** for STT (unlike the
Tensor-Tensor DVE path's discrete DRAM jump table): STT's `op0`/`op1` dispatch is the
*shared compiled-C++ `alu_op.cpp` switch* (the "not supported op/dtype" asserts),
reached by the two sequential compute `call0`s (`0xfffbc24c` / `0xfffbe298`), not a
per-op address table. `[HIGH structure]`

---

## 10. The elementwise-arith family side by side

| property | Tensor-Tensor | Tensor-Scalar | **Scalar-Tensor-Tensor** |
|---|---|---|---|
| opcode arith/bitvec | `0x41` / `0x51` | `0x43` / `0x53` (+PTR `44`/`54`) | `0x9d` / `0x9e` |
| operand struct | `S3S3D3_TT` (64 B) | `S3D3_TS` (64 B) | `S2S2D2_STT` (64 B) |
| inputs | 2 tensor | 1 tensor + 2 scalar | **1 scalar + 2 tensor** (3-input fused) |
| tensor pattern dim | `TENSOR3D` (3-D) | `TENSOR3D` (3-D) | `TENSOR2D` (2-D) |
| op-selector fields | one (`op`@14) | two (`op0`@36,`op1`@37) | two (`op0`@36,`op1`@37) |
| `reverse_operands` | none | None/First/Second/Both | None/First/Second/Both |
| fused formula (NONE) | `src0 op src1` | `(tensor op0 s0) op1 s1` | `(src0 op0 scalar) op1 src1` |
| scalar broadcast | n/a (stride-0) | IVP `rep*` splat | IVP `rep*` splat (scalar stage) |
| ALU-op enum | `NEURON_ISA_TPB_ALU_OP` | same | same |
| engine | POOL (Q7 KIT) | POOL + DVE | **DVE only** |
| per-elem trace | (TT decode strings) | `R[d]=OP(R[d],imm)` | `R[d]=OP(R[d],imm)` **AND** `OP(R[d],R[d])` |
| shared compute | `alu_op.cpp` | `alu_op.cpp` | `alu_op.cpp` |
| accumulator | no | no (Idle forced) | arith: Idle/Accum/ZeroAccum |
| gens | CAY→MAV | all (CAY→MAV wired) | all (CAY→MAV wired) |

⇒ **STT is the 3-input completion**: it takes Tensor-Scalar's two-AluOp +
`reverse_operands` + scalar-splat machinery and replaces the second scalar with a
second tensor, yielding a generic `(src0 op0 scalar) op1 src1` fusion. The
fused-multiply-add `dst = scalar·A + B` is the `op0=MULT`, `op1=ADD` special case —
**not** a dedicated MAC opcode. See the [ALU-op matrix](./alu-op-matrix.md) for the
shared op-set/dtype datapath.

---

## 11. Honesty ledger

**HIGH / OBSERVED**
- Opcodes `SCALAR_TENSOR_TENSOR_ARITH=0x9d` / `_BITVEC=0x9e`, `// Y` maintained,
  identical value on cayman/mariana/maverick/sunda (`common.h`, read verbatim).
- `S2S2D2_STT` struct→opcode binding `{STT-Arith, STT-Bitvec, TensorTensorScanArith,
  SelectReduce}` read verbatim from `instruction_mapping.json`. The 64-B layout
  **self-compile-verified this task** (`gcc` `offsetof`: src0@12, src1@24, op0@36,
  op1@37, reverse@38, imm0_src@39, in0_in1_dtype@40, out_dtype@41, channels@42,
  imm0_dtype@43, accum@44, reserved@45, dst@48, imm0@60; `sizeof==64`), byte-identical
  on all four gens.
- TWO op-selector fields (`op0`@36, `op1`@37) — the answer to "one or two?" is **two**.
- THREE operands: scalar=`imm0`@60 (single 4-B union, single `imm0_src`@39, single
  device `bnei a2,2`), tensorA=`src0`@12 (`TENSOR2D`), tensorB=`src1`@24 (`TENSOR2D`).
- `has_valid_scalar_tensor_tensor_op` requires both `op0`,`op1` ∈ general-arith
  (arith) / general-bitvec (bitvec) — generic, not a fixed MAC. The
  `reverse_operands` enum + composition formula read verbatim (`common.h:1239,1246`).
- All three DVE strings present in the shipped `libnrtucode_internal.so`:
  `"S: Scalar-Tensor-Tensor"`, `"S: OP=%x R[%d] = OP(R[%d], imm)"`,
  `"S: OP=%x R[%d] = OP(R[%d], R[%d])"` — the decisive scalar-op + tensor-tensor-op
  mix evidence.
- POOL-vs-DVE split: STT is DVE-only — present as the `"S:"` worker, **absent** from
  the Q7 POOL `kernel_info_table`, no `pool_scalar_tensor_tensor` symbol
  (`nm … | rg -ci scalar_tensor_tensor` ⇒ 0).
- dtype contract: arith FP32-hub (inline `imm0_dtype==FP32` unless ptr-sourced);
  bitvec int-only identity (src0==src1==imm0==out). accum: bitvec Idle, arith
  Idle/Accum/ZeroAccum.

**MED / INFERRED**
- The exact operand→stage wiring (scalar pairs with `src0` in `op0`; `src1` enters at
  `op1`) is the canonical semantics applied to the struct + the verbatim
  `reverse_operands` template; the device runs two sequential window-program+compute
  passes (HIGH structure), but *which src indexes which stage* is not byte-pinnable
  through the FLIX desync.
- The worker#1(arith)/#2(bitvec) assignment — inferred from the twin-body config
  divergence; the exact opcode each registers lives in the out-of-carve descriptor.
- The IVP splat / `op0`-stage binding — the vocabulary is OBSERVED, the per-STT-stage
  selection is in the desynced compute.
- v5/MAVERICK DVE worker *interiors* — INFERRED from the same DEBUG-string + stub
  pattern; struct/opcode are header-OBSERVED.

**LOW / UNRECOVERED**
- The exact opcode-`0x9d`/`0x9e`→funcVA descriptor bytes (runtime-bound; the stub
  `l32r` literals `0xfffc8368`/`0xfffc38e8`… resolve outside the carved IRAM).
- The `alu_op.cpp` compute body itself (the `call0` negative-literal targets
  `0xfffbc24c`/`0xfffbe298` resolve out-of-carve; the per-element two-AluOp loop is
  reached but not byte-walked end-to-end).

**FLIX-DESYNC FLAG.** The DEBUG worker bodies desync under stock `objdump` on the
recurring `.byte 0x2f/0x8f/0x4f/0x5f/0xcf` literal-pool lead bytes (the SX-FW-00
limitation). Mis-decoded `.byte`/spurious bundles are not reported as real
instructions. The cleaner PERF image supplies the IVP splat vocabulary. The
macro-structure (entry/log/descriptor-copy/two-stage/imm-src/accum/return) is
recovered from the surviving well-formed instructions + the xxd-verified
entry/log/stub edges.

---

## 12. Artifacts / reproduction

```sh
export XTENSA_SYSTEM=.../gpsimd_tools/tools/XtensaTools/config XTENSA_CORE=ncore2gp
OBJD=.../XtensaTools/bin/xtensa-elf-objdump
H=extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/.../custom_op/c10/include
SO=extracted/.../custom_op/c10/lib/libnrtucode_internal.so

# opcodes (0x9d/0x9e // Y, all gens):
rg 'SCALAR_TENSOR_TENSOR_(ARITH|BITVEC)' \
   $H/neuron_{cayman,mariana,maverick,sunda}_arch_isa/tpb/aws_neuron_isa_tpb_common.h

# struct + predicates:
#   $H/.../aws_neuron_isa_tpb_s2s2d2_stt.h
#     struct @24 ; is_valid_scalar_tensor_tensor @54 ; has_valid_scalar_tensor_tensor_op @200
#     s2s2d2_stt_imm0_dtype @228 ; s2s2d2_stt_src_dst_dtype @221 ; has_valid_s2s2d2_stt_accum_cmd @234
#     has_valid_s2s2d2_stt_immediate @142

# compile-verify (this task): sizeof==64, offsets 12/24/36/37/38/39/40/41/42/43/44/45/48/60
gcc -I$H/neuron_cayman_arch_isa/tpb /tmp/stt_verify.c -o /tmp/v && /tmp/v

# enums: common.h  ALU_OP @939 ; IMM_SRC @1207 ; TENS_SCALAR_REV_OPS @1246 (formula @1239)
#        ACCUM_CMD @777 ; DTYPE @722 ; TENSOR2D @643 ; IMM_VAL_INST_FIELD @866

# struct->opcode binding:
rg -A5 'S2S2D2_STT_STRUCT' $H/.../instruction_mapping.json

# the three DVE strings (decisive scalar+tensor mix):
strings -a $SO | rg 'Scalar-Tensor-Tensor|OP=%x R\['
#   "S: Scalar-Tensor-Tensor" / "...OP(R[%d], imm)" / "...OP(R[%d], R[%d])"

# negative claim — no pool symbol:
nm -C $SO | rg -ci scalar_tensor_tensor   # => 0

# carves (DVE workers): MAR self-name 0x20c0 -> workers 0xb0f8/0xb23c (entry a1,80) ;
#   stubs 0x220c/0x2228 (s32i a2,[a1+12]; call8 0x9920) ; imm-src bnei a2,2 @0xb1cf -> call8 0xb218
#   CAY 0x1fdf -> 0xac08/0xad48 (stubs 0x236a/0x2386) ; MAV 0x20ef -> 0xad84/0xaf00 (stubs 0x2076/0x208e)
```

---

## See also

- [Tensor-Tensor Elementwise Arith](./tensor-tensor.md) — the 2-tensor / one-op base
  of the family (`0x41`/`0x51`, `S3S3D3_TT`).
- [Tensor-Scalar + Tensor-Scalar-PTR](./tensor-scalar.md) — the 1-tensor + 2-scalar /
  two-op member STT extends (`0x43`/`0x53`, `S3D3_TS`).
- [TensorTensorScan](./tensor-tensor-scan.md) — the prefix-scan sibling sharing this
  exact `S2S2D2_STT` struct byte-for-byte (`0xe5`).
- [The ALU-Op Datapath + Dtype Matrix](./alu-op-matrix.md) — the shared `alu_op.cpp`
  op-set / FP32-hub datapath all three members funnel into.
