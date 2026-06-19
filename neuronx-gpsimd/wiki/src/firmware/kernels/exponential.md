# Exponential (the EXP transform)

> **Scope.** Exponential is **opcode `0x30`** (`NEURON_ISA_TPB_OPCODE_EXPONENTIAL`) — the
> element-wise **exponential TRANSFORM** op of the NeuronCore GPSIMD ISA. It applies an
> intrinsic exp-family function to every element of an input tensor and writes the
> transformed output: `dst[i] = exp_family(src[i])`. This page decodes its operand struct
> (`NEURON_ISA_TPB_S3D3_TS_STRUCT`, the 64-byte **Tensor-Scalar** struct, header-grounded),
> proves from the validator bytes that the two ALU ops are **forced to `Bypass`** and
> `reverse_operands` to `None` (so the transform is intrinsic, not composed from the ALU
> table), decodes the DVE-engine thin handler instruction-exact (entry `0xf698`, self-name
> `"S: Exponential"`), maps the transcendental to the DVE's **hardware piecewise-cubic PWL
> table** (the op is armed in the DVE `PROF_CAM`), tabulates the dtype matrix and the
> per-generation presence, and **reconciles the misleading name**: this is *not* a random
> exponential-distribution sampler.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

> **CORRECTION — Exponential is the EXP *transform*, not a distribution *sampler*.** The
> name invites the reading "draw `X ~ Exp(λ)` via the inverse-CDF `X = −ln(U)/λ`". That
> reading is **refuted by the bytes**. Exponential `0x30` binds to the **Tensor-Scalar**
> struct `S3D3_TS` (the same struct as `TensorScalarArithOp`), reads an input tensor
> `src_mem_pattern`, and writes a transformed output `dst_mem_pattern`, element-wise. There
> is **no** `rand_algorithm` field, **no** seed, **no** `post_process`, **no** uniform draw
> `U`, **no** `λ` rate parameter, **no** `−ln`/`ln(2)` magic constant, and **no** RNG
> staging anywhere in the handler body — all proven absent by an exhaustive `const16`-xref
> sweep of the handler. The exp() itself is evaluated by the DVE's **hardware PWL
> piecewise-cubic transcendental table** (the op is armed in the DVE `PROF_CAM`), exactly
> the [Activate](activate-pwl.md) mechanism. An exponential-distribution *sampler*, if a
> model wants one, is composed **host-side** from [`Rand2`](rand2.md) uniform draws plus
> (separately) this exp() transform — but the on-device `0x30` op is the transform only.
> The op shares only (a) the DVE engine and (b) an adjacent registration-stub slot with the
> RNG family; that adjacency is what makes the name mislead. `[HIGH/OBSERVED]`

---

## 1. TL;DR — the eight pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | Exponential is **opcode `0x30`** (`NEURON_ISA_TPB_OPCODE_EXPONENTIAL`). It sits in a different opcode band from the RNG family (`Rand=0x76`, `Rand2=0xe2`), adjacent to the Tensor-Scalar ops. | `aws_neuron_isa_tpb_common.h:242` (`= 0x30`) | HIGH/OBSERVED |
| 2 | It is an **element-wise transform**, **not** a random sampler. No `rand_algorithm`, no seed, no `post_process`, no `λ`, no RNG staging, no `−ln`/`ln(2)` constant. | struct layout + exhaustive handler `const16`-xref | HIGH/OBSERVED |
| 3 | It **reuses the Tensor-Scalar struct** `NEURON_ISA_TPB_S3D3_TS_STRUCT` (64 B) — the SAME struct as `TensorScalarArithOp`. | `instruction_mapping.json:286–294` (8th member) + `ISA_STATIC_ASSERT==64` | HIGH/OBSERVED |
| 4 | The two ALU ops are **forced**: `op0 == Bypass`, `op1 == Bypass`, `reverse_operands == None` — the exp is intrinsic to the opcode, not composed from the `ALU_OP` table (which has no exp/log entry). | `has_valid_exponential_ops` (s3d3_ts.h) | HIGH/OBSERVED |
| 5 | It **runs on the DVE engine**. Handler entry `0xf698`, self-name `"S: Exponential"`; the firmware's own `"exponential.cpp:76"` assert pins the source file. | `MARIANA_NX_DVE_DEBUG` IRAM `0xf698` + DRAM `0x3072` | HIGH/OBSERVED |
| 6 | The transcendental is **HW-offloaded to the DVE's piecewise-cubic PWL table** — `0x30` is **armed** in the DVE `PROF_CAM` (entry `@0x2d0`, `enable=1`). No software `−ln`/poly/exp loop in the handler. | `MARIANA_NX_DVE_PROF_CAM` byte-read | PROF_CAM HIGH/OBSERVED; eval HIGH-by-FW-76 |
| 7 | dtype: **input** any valid dtype **except `FP32R`**; **output** any valid dtype **including `FP32R`**. Integer inputs are converted to fp for the transcendental. | `is_valid_exponential` (`DtypeAllowFP32R::False`/`::True`) | HIGH/OBSERVED |
| 8 | Per-gen: opcode defined on **all** gens; validation + struct binding + handler + `PROF_CAM` arming only on **MARIANA / MARIANA_PLUS / MAVERICK**; **CAYMAN / SUNDA** define-but-unwired. | header dir + 3× string-image membership + `PROF_CAM` | HIGH/OBSERVED; MAVERICK interior INFERRED |

---

## 2. Provenance / carve anchors

All facts derive from static analysis of the shipped device-firmware blob (disassembled
with the Cadence Xtensa toolchain, `XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW) and the
shipped host customop-lib **ISA C headers**. No source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — the FW-26/27/28/41/48/76 anchor, re-verified in-task |
| Disassembler | `gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM | host VA `0x408fc0`, size `0x1c560` (`.rodata` VA == device file offset) |
| MARIANA `NX_DVE` DEBUG DRAM | host VA `0x425520`, size `0x7000` |
| MARIANA `NX_DVE` PROF_CAM / PROF_TABLE | host VA `0x59e880` / `0x59ec80`, size `0x400` / `0x2000` |
| MARIANA `NX_DVE` PERF IRAM/DRAM | host VA `0x31f5c0` / `0x332b00` (cleaner FLIX; strips `"S:"` logs — the corroborating IVP-op source) |

The authoritative struct/enum source is the shipped public ISA headers (same package):

- `…/neuron_mariana_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts.h` — the **S3D3_TS** struct + `is_valid_exponential` (NC-v4)
- `…/neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts.h` — same (NC-v5; sole `is_valid_exponential` diff = the tile-aware channel-range gate, §8)
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — `OPCODE_EXPONENTIAL = 0x30`; the `ALU_OP` / `IMM_SRC` / `ACCUM_CMD` / `TENS_SCALAR_REV_OPS` / `DTYPE` / `MEM_PATTERN3D` / `TENSOR3D` enums; `POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128`
- `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` — the struct→opcode binding

> **QUIRK — the header tree alone tells you Exponential is a MARIANA-and-later op.** `cayman/`
> and `sunda/` arch-isa dirs *define* `OPCODE_EXPONENTIAL = 0x30` in `common.h` and list it in
> the generic `is_valid_instruction` enum guard, but ship **no** `is_valid_exponential` function
> in `s3d3_ts.h` and **no** `OPCODE_EXPONENTIAL` member in `instruction_mapping.json`'s
> `S3D3_TS_STRUCT` binding (verified by directory listing + `rg`). The opcode is reserved
> everywhere but only *wired* from NC-v4 on. This matches the [`Rand2`](rand2.md) per-gen
> matrix exactly. `[HIGH/OBSERVED]`

---

## 3. The opcode — Exponential `0x30` vs the RNG family

Read verbatim from `aws_neuron_isa_tpb_common.h` (identical value on sunda/cayman/mariana/maverick):

```c
NEURON_ISA_TPB_OPCODE_EXPONENTIAL     = 0x30,  // (no status comment)
…
NEURON_ISA_TPB_OPCODE_RAND            = 0x76,  // n, ucode exists, not maintained/used
NEURON_ISA_TPB_OPCODE_RAND_GET_STATE  = 0x77,  // Y
NEURON_ISA_TPB_OPCODE_RAND_SET_STATE  = 0x78,  // Y
NEURON_ISA_TPB_OPCODE_RAND2           = 0xe2,  // Y
```

`0x30` sits in a **different opcode band** entirely from the RNG ops (`0x76`–`0x78`, `0xe2`).
It is opcode-adjacent to the Tensor-Scalar family (`TensorScalarArithOp`, `…BitvecOp`, etc.),
not to `Rand`. The **only** things `0x30` shares with the RNG family are (a) the DVE engine and
(b) an adjacent registration-stub slot (§5). Functionally it is an **activation-class transform**.
`[HIGH/OBSERVED]`

> **NOTE — `0x30` is a NeuronCore firmware-lane opcode, not a Vision-Q7 ISA mnemonic.** Keep the
> two axes distinct (see [`b14-hp-lookup.md`](../../isa/ref/b14-hp-lookup.md) §7's same caution):
> the ~140-entry `NEURON_ISA_TPB_OPCODE_*` enum is the *firmware kernel-lane* axis; the 1534-entry
> `ivp_*` roster is the *Xtensa ISA* axis. There is **no** dedicated `ivp_exp`/`ivp_log` ISA
> mnemonic (the [`sp_lookup`](../../isa/ref/b15-sp-lookup.md) roster has only `nexp01` range-split
> + `recip0`/`rsqrt0` seeds). Exponential `0x30` is a firmware opcode whose exp() function is the
> silicon PWL unit, not an ISA op. `[HIGH/OBSERVED]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S3D3_TS_STRUCT` (64 B)

Exponential **reuses the Tensor-Scalar struct**. The `instruction_mapping.json` binding (read
verbatim, mariana — `OPCODE_EXPONENTIAL` is the 8th member of the `S3D3_TS_STRUCT` list):

```json
"NEURON_ISA_TPB_S3D3_TS_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_ARITH_OP",
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_BITVEC_OP",
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP",
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP",
    "NEURON_ISA_TPB_OPCODE_TRANSPOSE_TENSOR_SCALAR_ARITH_OP",
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_REDUCE",
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_CACHE_CUMULATIVE",
    "NEURON_ISA_TPB_OPCODE_EXPONENTIAL"          // <-- Exponential rides the Tensor-Scalar struct
]
```

The `s3d3_ts.h` struct-usage comment also lists `` - `Exponential` `` among the opcodes that share
this struct. `[HIGH/OBSERVED]`

### 4a. The layout (`s3d3_ts.h`, `ISA_STATIC_ASSERT == 64`)

| off | size | field | type | Exponential role |
|----:|-----:|-------|------|------------------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `opcode = 0x30`, `inst_word_len`, dbg, (NC-v5) `inst_flags` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 1 | `accumulator_cmd` | `ACCUM_CMD` | `Idle/Zero/Accumulate/ZeroAccumulate/LoadAccumulate` (§4c) |
| 13 | 3 | `reserved0[3]` | — | must be 0 |
| 16 | 16 | `src_mem_pattern` | `MEM_PATTERN3D` (`TENSOR3D`) | **INPUT tensor `x`** |
| 32 | 1 | `in_dtype` | `DTYPE` | input dtype (**`FP32R` NOT allowed**) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype (**`FP32R` allowed**) |
| 34 | 1 | `num_active_channels` | `uint8` | partition count `1..128` |
| 35 | 1 | `imm0_src` | `IMM_SRC` | source of `imm0` (`inst`/`ptr`/`regptr`) |
| 36 | 1 | `op0` | `ALU_OP` | **FORCED `Bypass` (`0x00`)** |
| 37 | 1 | `op1` | `ALU_OP` | **FORCED `Bypass` (`0x00`)** |
| 38 | 1 | `reverse_operands` | `TENS_SCALAR_REV_OPS` | **FORCED `None` (`0x00`)** |
| 39 | 1 | `imm1_src` | `IMM_SRC` | source of `imm1` |
| 40 | 4 | `imm0` | `IMM_VAL_INST_FIELD` (union) | the scalar immediate (§4e) |
| 44 | 4 | `imm1` | `IMM_VAL_INST_FIELD` (union) | FORCED 0 unless `LoadAccumulate` (§4c) |
| 48 | 16 | `dst_mem_pattern` | `MEM_PATTERN3D` (`TENSOR3D`) | **OUTPUT tensor** |

`[HIGH/OBSERVED — header text + `ISA_STATIC_ASSERT == 64`]`

> **GOTCHA — the struct has NO randomness fields, because Exponential is not random.** The 64-byte
> `S3D3_TS` carries `src_mem_pattern` (an input tensor), an `in_dtype`/`out_dtype` pair, two ALU-op
> slots, and two immediates. It carries **no** `rand_algorithm`, **no** `seed`/state pointer, **no**
> `post_process` selector, **no** `min`/`max` range — every field [`Rand2`](rand2.md)'s `d3_rand`
> struct has and this one lacks. A reimplementer wiring `0x30` against the `d3_rand` struct (the
> sampler reading) builds a struct of the wrong shape; `0x30` is `S3D3_TS`, full stop. `[HIGH/OBSERVED]`

### 4b. The Exponential validation contract (`is_valid_exponential`, verbatim sense)

The `s3d3_ts.h` validator pins the whole op. Read verbatim (mariana):

```c
// fn is_valid_exponential(i: Inst) -> bool {
//       has_valid_neuron_header(i)
//    && has_valid_neuron_events(i)
//    && has_exponential_opcode(i)
//    && is_valid_dtype(i.s3d3_ts.in_dtype,  DtypeAllowFP32R::False)   // input  FP32R disallowed
//    && is_valid_dtype(i.s3d3_ts.out_dtype, DtypeAllowFP32R::True)    // output FP32R allowed
//    && tensor_scalar_immediates_check(i)
//    && has_valid_exponential_accum_cmd(i)              // §4c
//    && has_valid_exponential_num_elements(i)           // §4d (parity / indirect gate)
//    && has_valid_exponential_ops(i)                    // §4 — op0/op1 Bypass, reverse None
//    && s3d3_ts_exponential_unused_fields(i)
//    && tensor_scalar_tensor_chk(i)
//    && has_valid_active_channel_range(i.s3d3_ts.num_active_channels, POOLING_NUM_CHANNELS)  // 128
//    && check_m3d_active_channels(i.s3d3_ts.src_mem_pattern, i.s3d3_ts.num_active_channels)
//    && check_m3d_active_channels(i.s3d3_ts.dst_mem_pattern, i.s3d3_ts.num_active_channels)
//    && mem3d_valid(i.s3d3_ts.src_mem_pattern, i.s3d3_ts.in_dtype,
//                   WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)   // src may be in PSUM
//    && mem3d_valid(i.s3d3_ts.dst_mem_pattern, i.s3d3_ts.out_dtype,
//                   WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)
//    && indirect_quadrant_check_src3d(i.s3d3_ts.src_mem_pattern)
//    && indirect_quadrant_check_src3d_dst3d(i.s3d3_ts.src_mem_pattern, i.s3d3_ts.dst_mem_pattern)
// }
```

In one sentence: **apply the intrinsic exponential transform to every element of the `src`
`TENSOR3D` (which may be in PSUM or SBUF) and write the `dst` `TENSOR3D`**, with `op0`/`op1`
unused, an optional per-element scalar immediate (`imm0`), and the engine's running accumulator
optionally engaged. There is no rate/lambda, no seed, no algorithm field. `[HIGH/OBSERVED]`

### 4c. The accumulator contract (`has_valid_exponential_accum_cmd`, verbatim)

```c
// fn has_valid_exponential_accum_cmd(i: Inst) -> bool {
//       (   (   (i.s3d3_ts.accumulator_cmd == AccumCmd::ZeroAccumulate)
//            || (i.s3d3_ts.accumulator_cmd == AccumCmd::Idle)
//            || (i.s3d3_ts.accumulator_cmd == AccumCmd::Accumulate))
//        && (i.s3d3_ts.imm1_src == ImmSrc::InstructionImmediate)
//        && (i.s3d3_ts.imm1.imm_bitvec_uint32 == 0))
//    || (i.s3d3_ts.accumulator_cmd == AccumCmd::LoadAccumulate)
// }
```

`ACCUM_CMD` (common.h): `IDLE=0, ZERO=1, ACCUMULATE=2, ZERO_ACCUMULATE=3, LOAD_ACCUMULATE=4`. So
Exponential can run with the DVE per-lane running accumulator engaged — `exp()` *then* accumulate,
a softmax-style fused reduction — and the accumulator is drained the same way Activate's is (the
[`0x24 ACTIVATION_READ_ACCUMULATOR`](activate-pwl.md) / `D1_RD` path). When *not* `LoadAccumulate`,
`imm1` must be `InstructionImmediate`-sourced and exactly `0`. `[HIGH/OBSERVED]`

### 4d. The num_elements / parity contract (`has_valid_exponential_num_elements`, verbatim)

```c
// fn has_valid_exponential_num_elements(i: Inst) -> bool {
//       (i.s3d3_ts.accumulator_cmd == AccumCmd::Idle)
//    || (indirect_pattern(i.s3d3_ts.src_mem_pattern.t.start_addr))
//    || (indirect_pattern(i.s3d3_ts.dst_mem_pattern.t.start_addr))
//    || (exp_x_even_or_unpacked(i) && exp_upper_src_dim_even(i))
// }
```

`exp_x_even_or_unpacked` is true when `in_dtype ∈ {FP32, UINT32, INT32}` (the **unpacked** dtypes),
OR the inner-dim step is non-unit, OR `num_elem[0]` is even; `exp_upper_src_dim_even` constrains the
upper dims to even counts. The **packed fp16/bf16 path processes lanes in 2-wide pairs** (the `ivp`
`_2x` pairing), so when the accumulator is engaged the inner-dim element count must be **even** (or the
access non-unit-stride / indirect). For fp32/int32 (unpacked) the parity is waived. This is the FP16
even-pair processing constraint — **not** a distribution gate. `[HIGH/OBSERVED]`

### 4e. The immediate union (`imm0` / `imm1`)

`imm0`/`imm1` overlay `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` (a union of `imm_arith_fp32` /
`imm_bitvec_uint32` / `imm_ptr` (`PartitionOffset`) / `imm_reg`); `imm0_src`/`imm1_src` are
`IMM_SRC {INSTRUCTION_IMMEDIATE=0, POINTER_IMMEDIATE=1, REG_PTR_IMMEDIATE=2}`. For Exponential
`imm1` is constrained to 0 (unless `LoadAccumulate`); `imm0` is the op's scalar immediate — most
plausibly an input scale/offset folded into the exp argument (the analogue of Activate's
`scale·x + bias` affine, [activation page](../../uarch/activation-transcendental-tables.md) §2.4).
It is **not** a rate `λ`. `[HIGH/OBSERVED struct + `IMM_SRC` enum; the `imm0` *semantic* INFERRED]`

### 4f. `MEM_PATTERN3D`

`MEM_PATTERN3D = union { TENSOR3D t ; INDIRECT16B i }` (common.h). `TENSOR3D = ADDR4 start_addr +
int16 step_elem[3] + uint16 num_elem[3]`; the `INDIRECT16B` arm is the gather/indirect form the
`num_elements` gate references. `src` is `AllowedInPSUM` **and** `AllowedInSBUF` — Exponential can read
the matmul PSUM directly (e.g. an attention-logit `exp()` straight off the PE-array output). `[HIGH/OBSERVED]`

---

## 5. The dispatch chain — SEQ opcode `0x30` → DVE kernel → funcVA → body

### 5a. The DVE registration-stub trio

Three **consecutive** stubs register the modern DVE random/transform family (the FW-70/FW-48
registration template). MARIANA DVE IRAM, byte-decoded:

```asm
0x2308: entry a1,48 ; const16 a2,0xebe0 ; s32i a2,[a1+12] ; … ; call8 0x9920  -> RandSetState (0xebe0)
0x2324: entry a1,48 ; const16 a2,0xf2cc ; s32i a2,[a1+12] ; … ; call8 0x9920  -> Rand2        (0xf2cc)
0x2340: entry a1,48 ; const16 a2,0xf698 ; s32i a2,[a1+12] ;             ; call8 0x9920  -> Exponential  (0xf698)
0x235c: entry a1,48 ; const16 a2,0xb35c ; …                                  (the +4th DVE family member)
```

`s32i a2,[a1+12]` writes the `funcVA` into the DVE `kernel_info` slot; `call8 0x9920` is the DVE
kernel-register routine. Exponential is the **third** stub in the contiguous trio — *adjacent to*
the RNG family but functionally unrelated to it. `[HIGH/OBSERVED]`

> **GOTCHA — the opcode→funcVA descriptor literal is out-of-carve.** The Exponential stub's two
> `l32r` literals resolve to `0xfffc84b8` / `0xfffc3a38` — **negative** PC-relative addresses that
> land *outside* the carved IRAM, in the runtime-bound kernel-descriptor / decode-table region. So
> the `opcode 0x30 → descriptor` binding is a **runtime-bound** table entry, not statically pinned in
> this carve (the identical limitation [`Rand2`](rand2.md) §5a flags). The `funcVA → body` edge
> (`0xf698` → handler) is byte-pinned; the `opcode → funcVA` edge is the FW-70 template + the stub.
> `[stub edges HIGH/OBSERVED ; opcode→funcVA descriptor LOW/out-of-carve]`

### 5b. The handler→name pinning (the identity proof)

Handler `0xf698` logs `"S: Exponential"`:

```asm
0xf6a1: const16 a10, 8            ; DRAM VA high half
0xf6a4: const16 a10, 0x3072       ; "S: Exponential"  (DRAM @0x3072)
0xf6a7: call8   0x188a4           ; the DVE "S:" log helper
```

`DRAM @0x3072 = 53 3a 20 45 78 70 6f 6e 65 6e 74 69 61 6c 0a 00 = "S: Exponential\n"` (xxd-verified).
The DVE DRAM also embeds the firmware's **own** source-file strings: `@0x3082 = "exponential\0"`
(the function name) and `@0x308e = "exponential.cpp\0"` (the file). `[HIGH/OBSERVED]`

### 5c. The canonical chain

```text
[SEQ: Exponential opcode 0x30]
   -> [DVE kernel_info slot  (runtime-bound descriptor — §5a GOTCHA)]
   -> funcVA 0xf698
   -> handler body  "S: Exponential"  -> DVE Tensor-Scalar worker + HW PWL
```

`opcode 0x30` is HEADER-grounded (HIGH); the `funcVA 0xf698` + body are byte-pinned (HIGH); the
`opcode → descriptor` literal is out-of-carve (LOW). `[HIGH for funcVA + body; descriptor MED/out-of-carve]`

---

## 6. The handler body — instruction-exact (MARIANA DVE `0xf698`)

The handler is **thin** (`0xf698..0xfaf4`, ~`0x45c` B, `retw.n @0xfaf4`). It logs, computes
config flags, programs the FLIX vector window, validates, and hands off to the DVE Tensor-Scalar
worker — it carries **no** RNG, **no** `−ln`, **no** software exp/log loop.

### 6a. Entry + log (`0xf698..0xf6aa`)

```asm
f698: entry  a1, 80              ; SMALL frame (thin handler; == Rand2 0xf2cc's frame 80)
f69b: j 0xf6a1
f6a1: const16 a10, 8
f6a4: const16 a10, 0x3072        ; "S: Exponential"
f6a7: call8  0x188a4             ; LOG self-name
```

### 6b. Config-flag compute (`0xf6c2..0xf6f1`)

```asm
f6c2: l32i.n a2,[a1+20] ; movi.n a3,0 ; saltu a2,a3,a2 ; s8i a2,[a1+28]   ; flag28 = (slot20 != 0)
f6cc: l32i.n a2,[a1+16] ; saltu a2,a3,a2 ; s8i a2,[a1+24]                 ; flag24 = (slot16 != 0)
f6d4: l8ui a2,[a1+28] ; l8ui a3,[a1+24] ; extui a12,a3,0,1 ; extui a13,a2,0,1   ; two 1-bit flags
f6e4: call8 0x9f2c               ; config helper (a10/a11 = 0)
f6ea: call8 0x99bc               ; the shared DVE setup helper (every DVE handler calls it)
f6f1: call8 0x15294              ; the DVE worker sub-dispatcher
```

The two flags (slot16/slot20 nonzero, reduced to 1-bit `a12`/`a13` via `extui`) are the
dtype-class / pattern-class selectors used by the worker hand-off (§6d). `0x99bc`/`0x9f2c`/`0x99c8`
are the same setup helpers [`Rand2`](rand2.md)'s handler calls — DVE handler boilerplate.
`[entry+call edges HIGH/OBSERVED; the precise flag semantics INFERRED from the `saltu`/`extui` shape]`

### 6c. FLIX vector-register-window programming (`0xf6ed..0xfa88`)

A long run of `s16i a11,[a4+176]` / `s16i a11,[a4+0x130]` / `extui a11,a{2,3},26,1` /
`and a8,a8,<mask>` / `slli a9,a9,{17,20,26,28}` / `add.n a8,a8,a9` packs per-lane control bits at
field positions 17/20/26/28 into a control word — the transcendental-datapath window. It is **more
extensive** than Rand2's setup (Rand2 was a single uniform-affine; Exponential programs the PWL
datapath window).

> **CORRECTION — these bundles are FLIX-desync artifacts, not real opcodes.** The flat DEBUG image
> ships no `.xt.prop` FLIX property table, so stock `xtensa-elf-objdump` desyncs across the VLIW
> bundle stream here (the SX-FW-00/26/27/41/48 limitation). The `.byte 0x2f/0xb3/0x8f` and
> `call0 0xfffc…` it prints in this range are **decode artifacts**, not instructions — the bundles
> are not byte-pinnable. The window-program *shape* (which control fields are packed, that it is more
> extensive than Rand2's) is `[HIGH/OBSERVED]`; the exact bundles are `[MED]`.

### 6d. Validation assert + worker hand-off + return (`0xfa8a..0xfaf4`)

```asm
fa8a: l32i.n a2,[a1+20] ; bnei a2,2,0xfa9c
faa8: l32i.n a2,[a1+12] ; bnei a2,1,0xfac8         ; the assert guard
fab1: const16 a10,8 ; const16 a10,0x3082           ; a10 = "exponential"      (func)
fab7: const16 a11,8 ; const16 a11,0x308e           ; a11 = "exponential.cpp"  (file)
fabd: movi.n a12,76                                ; a12 = 76  (LINE NUMBER)
fac5: call8 0xb800                                 ; assert: "exponential(exponential.cpp:76)"
…
fadc: call8 0xa184                                 ; -> "S: Tensor-Scalar" WORKER (str 0x1fd4)
fae9: movi.n a10,1 ; call8 0xa014                  ; -> the config worker
faf4: retw.n                                       ; *** Exponential handler RETURNS ***
```

- **The assert (`0xb800`)** loads the format strings `0x22c0` (`"S: Assertion failure! %s(%s:%u)"`)
  + `0x22e1` and calls the log/abort path. The triple `(func="exponential", file="exponential.cpp",
  line=76)` is the standard `func(file:line)` assert — pinning `exponential.cpp:76` as a device-side
  validation guard. `[HIGH/OBSERVED]`
- **The worker (`0xa184`)** self-names `"S: Tensor-Scalar"` (`DRAM 0x1fd4 = "S: Tensor-Scalar\n"`;
  sibling `0x1ff4 = "S: Tensor-Scalar-PTR\n"`). So Exponential's per-element compute is the DVE
  **Tensor-Scalar worker** — the SAME datapath as `TensorScalarArithOp`, here driven with
  `op0`/`op1 = Bypass` and the intrinsic exp transform engaged via the PWL (§7). This is the
  byte-level proof that `0x30` rides the Tensor-Scalar datapath. `[HIGH/OBSERVED the worker name]`

The flow: **log → config flags → program the FLIX window → validate (assert `exponential.cpp:76`)
→ dtype-branch to the Tensor-Scalar worker (`0xa184`) or the config worker (`0xa014`) → return.**

### 6e. Negative evidence — the smoking guns that REFUTE the sampler hypothesis

An exhaustive `const16`-xref scan of the handler body `0xf698..0xfaf4` finds:

- **NO RNG staging.** The body references **neither** the DVE `"MEMSET/RNG"` string (`DRAM 0x200e`),
  **nor** `"push GENERATE to DMA[%d]"` (`DRAM 0x3896`), **nor** `"Event_Semaphore Rng Clr"`
  (`0x2ea3`). Zero hits. (By contrast, [`Rand2`](rand2.md)'s pipeline is *built on* exactly this
  GENERATE-DMA staging.) `[HIGH/OBSERVED negative]`
- **NO Xorwow Weyl constant** `0x87c5` (= 362437) anywhere in the body. `[HIGH/OBSERVED negative]`
- **NO `−ln` / `ln(2)` magic constant** (`0x3f317218`, `0x42b8aa3b`, `0x3fb8aa3b`, …). `[HIGH/OBSERVED negative]`
- **NO `rand_algorithm`/`post_process`/`seed` field** in the struct (`S3D3_TS` has none). `[HIGH/OBSERVED — struct layout]`

So Exponential is a deterministic exp-family transform of an INPUT tensor, not a draw from an
exponential distribution. The "`U=0 → ln(0)=−∞` edge guard" a sampler would need **does not exist**
because there is no `ln` and no `U`. `[HIGH/OBSERVED]`

---

## 7. The transcendental — the DVE hardware PWL (piecewise-cubic), not a software exp

The exp() function is **not** a software polynomial in the handler — it is offloaded to the DVE's
hardware piecewise-cubic PWL table, the **exact** mechanism the
[Activation + Transcendental Table Engine](../../uarch/activation-transcendental-tables.md) page
(Engine B) decodes for the ACT engine.

### 7a. `0x30` is armed in the DVE PROF_CAM

The DVE ships its own activation profile tables — `MARIANA_NX_DVE_PROF_CAM` (`0x59e880/0x400`) +
`PROF_TABLE` (`0x59ec80/0x2000`) — in the same four-table format the activation page documents
(CAM / PROFILE / CONTROL / BUCKET). The `PROF_CAM` carries 48 populated 16-byte staging records
`{opcode_id:u32, mask=0xff:u32, enable=1:u32, rsvd:u32}`; entry `@0x2d0` reads
**`opcode_id = 0x30, enable = 1`** (byte-read this task). `0xe2` (Rand2) is armed at `@0x290`. So
`0x30` is a **CAM-matched activation-class op** the DVE routes through its piecewise-cubic
transcendental datapath. `[PROF_CAM-arming HIGH/OBSERVED]`

> **GOTCHA — the CAYMAN DVE PROF_CAM is the negative control.** The CAYMAN DVE `PROF_CAM`
> (`0x304ca0/0x400`) arms **neither** `0x30` **nor** `0xe2` — matching CAYMAN's missing
> `is_valid_exponential` and missing struct binding. The op is reserved but unwired on CAYMAN at
> *every* layer: header, struct, handler string, and CAM. `[HIGH/OBSERVED]`

### 7b. The PWL evaluation — segment-index → coefficient-fetch → Horner cubic

The DVE PWL is a four-table machine. The function input (after the affine fold) feeds the CONTROL
**bitfield extract** to produce a log-spaced bucket index; that indexes a 32-byte BUCKET holding a
degree-≤3 polynomial `{float d0, d1, d2, d3, x0}`; the HW evaluates the cubic at the local coordinate
`t = (x − x0)`. The annotated pseudocode (naming the recovered table structs from the activation page;
the cubic schedule is HW, the bucket *contents* are host-loaded):

```c
// DVE PWL evaluation of Exponential 0x30.
// Tables: aws_hal_stpb_act_{control,bucket}_entry_t (activation page §2.1, compile-verified).
// CONTROL = {act_tbl_base:11, extract_lsb:5, extract_size:4}; BUCKET = {float d0,d1,d2,d3,x0}.
static inline float dve_pwl_exp(uint32_t x_bits, const act_control_t *ctrl,
                                const act_bucket_t *bucket_pool, int8_t exponent_offset) {
    // 1. SEGMENT INDEX — extract `extract_size` bits starting at `extract_lsb` from the fp
    //    bit pattern (exponent + mantissa-MSB window => a NON-UNIFORM, LOG-SPACED segmentation),
    //    biased by act_tbl_base + the PROFILE.exponent_offset pre-bias.
    uint32_t biased = x_bits + ((uint32_t)exponent_offset << 23);   // PROFILE.exponent_offset
    uint32_t field  = (biased >> ctrl->extract_lsb) & ((1u << ctrl->extract_size) - 1u);
    uint32_t seg    = ctrl->act_tbl_base + field;                   // bucket index into the shared pool

    // 2. COEFFICIENT FETCH — one 32-byte BUCKET = the cubic for this segment.
    const act_bucket_t *b = &bucket_pool[seg];                      // {d0,d1,d2,d3,x0}  (host-loaded)

    // 3. HORNER-CUBIC EVAL — degree-<=3 polynomial at the local coordinate t = (x - x0).
    float t = fp_from_bits(x_bits) - b->x0;                         // segment-local coordinate
    float y = b->d3;                                                // Horner: d0 + t(d1 + t(d2 + t*d3))
    y = y * t + b->d2;
    y = y * t + b->d1;
    y = y * t + b->d0;
    return y;                                                       // raw exp() value, pre region/symmetry/affine
}
// The PROFILE region-split / symmetry-reflect / special-input substitution (func_rslt_for_{zero,
// nan,pos_inf,neg_inf}) and the scale*x+bias affine wrap this — exp's asymptotes (+inf at +x,
// 0 at -x) come from the special-input substitute, so the cubic never approximates the tails.
```

The presence of `d2`/`d3` (compile-verified at BUCKET offsets 8/12, all 5 gens) is what makes this a
**cubic**, not a linear breakpoint/slope PWL. `[HIGH/OBSERVED struct + extract spec; the Horner
schedule INFERRED-HIGH from the 4-coeff + breakpoint shape — see the activation page §2.2]`

### 7c. The firmware-side datapath primitives (set-up, not eval)

The DVE PERF IRAM (cleaner FLIX) carries the transcendental **set-up / convert** vocabulary as
well-formed VLIW bundles (byte-decoded): `ivp_recipqlin_2xf32_1` (fp32 reciprocal QLI seed,
`@0x662b`), `ivp_addexpmnxf16t` (fp16 add-exp/mantissa decompose — the fp-decompose that materialises
the PWL bucket index, `@0xd92e`), `ivp_ficeilnxf16` (`@0xc2a1`), `ivp_ufloatn_2x32t` (uint32→fp32
convert, `@0xd9d3`). There is **no** dedicated `ivp_log`/`ivp_exp`/`ivp_ln` op in the DVE image or the
ISA roster ([`sp_lookup`](../../isa/ref/b15-sp-lookup.md) has only the `recip0`/`rsqrt0` seeds +
`nexp01` range-split). So the firmware does the fp-**decompose** (index-extract) + the **convert** +
the **reciprocal**; the function **eval** is the silicon PWL. These PERF ops are not pinnable to the
Exponential handler specifically (PERF strips the `"S:"` logs and the bundles live in shared workers)
— the attribution is to the DVE transcendental datapath the handler drives. `[ops + presence
HIGH/OBSERVED; per-handler attribution MED through the FLIX desync]`

> **WALL — the host-loaded exp() PWL coefficients are NOT in the image.** The BUCKET `{d0..d3,x0}`
> cubics and the per-function PROFILE config words for exp/relu/gelu/… are staged at runtime per model
> via the [`0x23 ACTIVATION_TABLE_LOAD`](activate-pwl.md) DMA, never resident in the firmware blob
> (the activation page's one-sided host-content wall). What is OBSERVED is the **machine** (the table
> format + the CAM arming); what is out-of-corpus is the **content** (the exp coefficients). A
> reimplementer recovers the *evaluator* here; the *coefficients* arrive from the host KRT. `[—/CARRIED]`

### 7d. Why there is no `−ln` / inverse-CDF

A `−ln(U)/λ` exponential-distribution sampler would need (a) a uniform draw `U` (Exponential has none
— no RNG staging, §6e), (b) a natural-log (the DVE has no `ln`; only the PWL could compute one, and
the armed `0x30` function is `exp()`, not `ln`), and (c) a `1/λ` scale + a `U=0` edge guard (no `λ`
field exists; `imm0` is a generic transform immediate). **None** of these are present. The
exponential-distribution sampler, if needed, is composed host-side from [`Rand2`](rand2.md)
`UniformInRange` draws + (separately) this exp() transform. `[HIGH/OBSERVED — the absence chain]`

---

## 8. Per-generation presence + dtype matrix

### 8a. Per-gen presence (three independent streams agree)

(A) the `common.h` opcode (`0x30` defined ALL gens); (B) the `is_valid_exponential` validation
function + the `instruction_mapping.json` `S3D3_TS_STRUCT` binding; (C) the `"S: Exponential"` DVE
DEBUG string + the DVE `PROF_CAM` `0x30` arming.

| GEN | opcode `0x30` | `is_valid_exponential` | `S3D3_TS` binding | `"S: Exponential"` | `PROF_CAM 0x30` | wired? | Tag |
|-----|:-------------:|:----------------------:|:-----------------:|--------------------|:---------------:|:------:|-----|
| SUNDA (V1) | YES | NO | (none) | (no NX_DVE DEBUG image) | n/a | **NO** | HIGH/OBSERVED |
| CAYMAN (NC-v3) | YES | NO | (none) | ABSENT | NOT armed | **NO** | HIGH/OBSERVED |
| MARIANA (NC-v4) | YES | YES | YES | `@str 0x3072` | ARMED (`@0x2d0`) | **YES** (`0xf698`) | HIGH/OBSERVED |
| MARIANA_PLUS | YES | (mariana hdr) | YES | `@DRAM 0x6f02b2` | (corroborated) | **YES** | HIGH/OBSERVED string; body INFERRED |
| MAVERICK (NC-v5) | YES | YES | YES | `@str 0x30d2` | (DVE PWL) | **YES** (`0xfa8c`) | header-OBSERVED → **interior INFERRED** |

The registration stub trio per gen (byte-read): MARIANA DVE IRAM `0x2308→0xebe0` /
`0x2324→0xf2cc` / `0x2340→0xf698` / `0x235c→0xb35c`; MAVERICK DVE IRAM `0x2130→0xefd0` /
`0x2148→0xf6c0` / `0x2166→0xfa8c` / `0x217e→0xb020` (RandSetState / Rand2 / **Exponential** / +1).

> **MAVERICK (NC-v5) — header-OBSERVED → INFERRED.** The `"S: Exponential"` string (`@str 0x30d2`),
> the MAVERICK `is_valid_exponential` (read directly from the MAVERICK `s3d3_ts.h`), and the
> registration stub (`0x2166 → 0xfa8c`) are observed; the MAVERICK handler **body** was not fully
> disassembled (FLIX-desync'd). It is assumed identical-family by the byte-stable MARIANA decode + the
> matching MAVERICK string-load. **The MAVERICK interior is INFERRED, not byte-pinned.**

**The MAVERICK delta.** The *only* `is_valid_exponential` diff vs mariana is the channel-range gate:
mariana uses `has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS)`; maverick
uses `has_valid_active_channel_range_with_tile(num_active_channels, DVE_NUM_CHANNELS,
header.inst_flags)` — NC-v5 adds **tile-aware channel ranging**. Both counts = 128
(`POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`). The struct + the `op0`/`op1 = Bypass` + dtype
contracts are byte-identical (verified by `diff` of the two headers). Identical to
[`Rand2`](rand2.md)'s NC-v5 delta. `[HIGH/OBSERVED — header diff]`

### 8b. The dtype matrix

`is_valid_exponential` gates the dtypes with two opposite `DtypeAllowFP32R` flags:
`is_valid_dtype(in_dtype, DtypeAllowFP32R::False)` (input **bars** FP32R) and
`is_valid_dtype(out_dtype, DtypeAllowFP32R::True)` (output **admits** FP32R). The `exp_x_even_or_unpacked`
gate names FP32/UINT32/INT32 as the "unpacked" dtypes (parity waived, §4d); the packed path is the
fp16 family (2-wide lane pairs). DTYPE codes (`common.h` / SX-ABI-06):

| dtype | code | as `in_dtype`? | as `out_dtype`? | note |
|-------|-----:|:--------------:|:---------------:|------|
| `BFLOAT16` / `BF16` | `0x6` | YES | YES | packed (2-wide lane pairs; even-count gate when accum engaged) |
| `FP16` | `0x7` | YES | YES | packed (2-wide lane pairs) |
| `INT32` | `0x8` | YES | YES | **unpacked**; converted to fp for the transcendental (`ivp_float`) |
| `UINT32` | `0x9` | YES | YES | **unpacked**; converted to fp (`ivp_ufloat`) |
| `FP32` | `0xA` | YES | YES | unpacked |
| `FP32R` | `0xB` | **NO** | YES | input bars the partial-fp32 representation; output allows it |
| any other integer dtype | — | (per `is_valid_dtype`) | (per `is_valid_dtype`) | — |

Exponential is fundamentally an **fp transcendental** — it accepts integer inputs by converting them
to fp, bars `FP32R` on input (the partial-fp32 representation), and allows `FP32R` on output.
`[HIGH/OBSERVED header `is_valid_exponential` + DTYPE codes]`

---

## 9. The sampler-vs-transform reconciliation (and the do-not-repeat row)

### 9a. Why the name misleads, exhausted

The op is named **Exponential** and sits in the contiguous DVE registration trio next to
`RandSetState` and [`Rand2`](rand2.md), so an early reading naturally guessed
"non-uniform on-device distribution — an `Exp(λ)` sampler via `X = −ln(U)/λ`". Every structural and
byte-level test contradicts the sampler reading and confirms the transform reading:

| test | sampler would show | Exponential actually shows | verdict |
|------|--------------------|----------------------------|---------|
| operand struct | a `d3_rand`-style struct (algorithm/seed/post_process/min/max) | **`S3D3_TS`** (Tensor-Scalar: src tensor, in/out dtype, ALU ops) | TRANSFORM |
| input | no input tensor (draws are synthesised) | **reads `src_mem_pattern`** element-wise | TRANSFORM |
| RNG draw | a uniform `U` from the GENERATE-DMA pipeline | **no** `MEMSET/RNG`, `GENERATE-DMA`, `RngClr` xref; **no** Weyl `0x87c5` | TRANSFORM |
| function | a `−ln` (inverse-CDF) | **no** `ln`/`ln(2)` constant; the armed PWL function is `exp()` | TRANSFORM |
| rate parameter | a `λ` / `1/λ` field | **none** — `imm0` is a generic transform immediate | TRANSFORM |
| edge guard | a `U=0 → −∞` guard | **none** — there is no `U` and no `ln` | TRANSFORM |
| worker | an RNG consumer | **`"S: Tensor-Scalar"`** worker, `op0/op1=Bypass` | TRANSFORM |

The two framings reconcile cleanly: Exponential **is** "separate from the uniform-only RNG family"
(true — different opcode, different struct, different band), but it is **not a sampler** — it is an
`exp()` transform that merely shares the DVE engine + the adjacent registration slot with the RNG
family. The GPSIMD RNG subsystem stays **uniform-only** on device (see [`rand2.md`](rand2.md) §9);
Exponential is an **activation-family transform**. `[HIGH/OBSERVED]`

### 9b. Do-not-repeat row

> **CORRECTION — carry this row; it has already cost one analysis pass.**

- **Superseded.** "Exponential `0x30` is the on-device exponential-**distribution sampler** — it draws
  `X ~ Exp(λ)` via the inverse-CDF `X = −ln(U)/λ`, extending the uniform RNG pipeline with a
  non-uniform distribution; watch for the `U=0 → ln(0) = −∞` edge guard."
- **Correct.** Exponential `0x30` is the element-wise **exp() TRANSFORM** op, bound to the Tensor-Scalar
  struct `NEURON_ISA_TPB_S3D3_TS_STRUCT` (64 B) with `op0`/`op1` **forced to `AluOp::Bypass`** and
  `reverse_operands == None` (`has_valid_exponential_ops`). It reads an input tensor and writes the
  transformed output; the exp() is the DVE **hardware PWL piecewise-cubic** table (op armed in the DVE
  `PROF_CAM @0x2d0`). There is no RNG draw, no `λ`, no seed, no `−ln`, no edge guard. `[HIGH/OBSERVED]`
- **Evidence.** `instruction_mapping.json` (`OPCODE_EXPONENTIAL` ∈ `S3D3_TS_STRUCT`, 8th member);
  `has_valid_exponential_ops` (`op0==Bypass && op1==Bypass && reverse==None`); handler `0xf698`
  self-name `"S: Exponential"` + `"S: Tensor-Scalar"` worker hand-off (`0xa184`); exhaustive
  `const16`-xref of the body (no `0x200e`/`0x3896`/`0x2ea3`/`0x87c5`/`ln`-constant); `PROF_CAM @0x2d0`
  `opcode_id=0x30, enable=1`.
- **Affected pages.** [`rand2.md`](rand2.md) (the DVE sibling note), this page, the
  [opcode-catalog-ledger](opcode-catalog-ledger.md), and any per-op survey that lists the DVE trio —
  a re-introduction would re-mislabel `0x30` as the fourth RNG op.

---

## 10. Reimplementation checklist

A reimplementer building a Vision-Q7-compatible Exponential `0x30`:

1. **Decode** `0x30` against the 64-byte `S3D3_TS` struct (§4a), **not** any rand struct.
2. **Validate** with the `is_valid_exponential` chain (§4b): `op0`/`op1 = Bypass`, `reverse = None`;
   `in_dtype` admits any valid dtype **except `FP32R`**, `out_dtype` admits all incl. `FP32R`; the
   accum-cmd contract (§4c); the fp16 even-pair / indirect num_elements gate (§4d); channels `1..128`
   (NC-v5: tile-aware); src/dst valid in PSUM **and** SBUF.
3. **Dispatch** via the DVE `kernel_info` slot → the handler → the **Tensor-Scalar worker**
   (`op0`/`op1 = Bypass`, intrinsic exp engaged), optionally with the per-lane accumulator (softmax-style).
4. **Evaluate** the exp() through the DVE **PWL**: CONTROL bitfield-extract → BUCKET cubic
   `{d0,d1,d2,d3,x0}` → Horner at `t = x − x0` → PROFILE region/symmetry/special-input/affine (§7b). The
   coefficients are **host-loaded** via `0x23 ACTIVATION_TABLE_LOAD` (the wall, §7c).
5. **Convert** integer inputs to fp before the transcendental (`ivp_ufloat`/`ivp_float`); the
   firmware-side primitives are decompose + convert + reciprocal, not a software exp loop.

---

## 11. Honesty ledger

**HIGH / OBSERVED** (header read / disasm / byte read / arithmetic identity):

- `OPCODE_EXPONENTIAL = 0x30` (`common.h`, all four gens).
- The `S3D3_TS_STRUCT` binding (`instruction_mapping.json`, mariana + maverick) + the 64-byte layout
  (`accum_cmd@12, src TENSOR3D@16, in_dtype@32, out_dtype@33, num_active_channels@34, imm0_src@35,
  op0@36, op1@37, reverse@38, imm1_src@39, imm0@40, imm1@44, dst TENSOR3D@48`), `ISA_STATIC_ASSERT==64`.
- `is_valid_exponential`: `op0==Bypass && op1==Bypass && reverse==None`; `in_dtype` FP32R-disallowed,
  `out_dtype` FP32R-allowed; the accum-cmd contract; the num_elements parity/indirect gate; the
  channel-range gate; src/dst `AllowedInPSUM`+`AllowedInSBUF`.
- The registration stub trio `0x2308/0x2324/0x2340 → 0xebe0/0xf2cc/0xf698`; the Exponential stub
  (`const16 a2,0xf698; s32i a2,[a1+12]; call8 0x9920`); the maverick trio
  `0x2130/0x2148/0x2166/0x217e → 0xefd0/0xf6c0/0xfa8c/0xb020`.
- Handler `0xf698` entry `a1,80`; self-name `const16 a10,0x3072 → call8 0x188a4`; `DRAM @0x3072 =
  "S: Exponential\n"`, `@0x3082 = "exponential"`, `@0x308e = "exponential.cpp"` (xxd-verified); the
  `retw.n @0xfaf4` boundary.
- The assert `@0xfab1..0xfac5` (func/file/line = `exponential`/`exponential.cpp`/`76`, `call8 0xb800`);
  the worker hand-off `call8 0xa184 = "S: Tensor-Scalar"`; the config worker `0xa014`; the shared DVE
  setup helpers `0x99bc/0x9f2c/0x99c8/0x15294`.
- **No** RNG staging in the body (no `0x200e`/`0x3896`/`0x2ea3`, no Weyl `0x87c5`, no `ln`/`log`
  constant) — exhaustive `const16`-xref.
- `0x30` ARMED in the MARIANA DVE `PROF_CAM` (`@0x2d0`, `enable=1`); `0xe2` also armed; the CAYMAN DVE
  `PROF_CAM` arms NEITHER (negative control).
- Per-gen: opcode all gens; validation + mapping + handler + `PROF_CAM` only mariana/mariana_plus/
  maverick; cayman/sunda define-but-unwired; the maverick NC-v5 tile-aware channel-range delta.

**MED / INFERRED:**

- The exp() **function** is evaluated by the HW PWL (HIGH by FW-76 table-format + engine-family + the
  `0x30` `PROF_CAM` arming; the precise coefficients are host-loaded).
- The role of `imm0` (a transform scale/offset folded into the exp argument, the Activate `scale·x+bias`
  analogue) — INFERRED from the struct + the Activate model.
- The two config flags (`slot16`/`slot20 → a12`/`a13`) selecting the worker hand-off — the `saltu`/`extui`
  shape is OBSERVED, the precise field meaning INFERRED.
- The PERF transcendental bundles' attribution to the Exponential handler (PERF strips `"S:"` logs).

**CARRIED / LOW:**

- The exact `opcode 0x30 → funcVA 0xf698` descriptor byte (runtime-bound, out-of-carve; the `l32r`
  literals `0xfffc84b8`/`0xfffc3a38` resolve outside the carve).
- The host-loaded exp() PWL coefficients (never resident in the firmware image — the activation-page
  wall).
- The MARIANA_PLUS / MAVERICK handler **bodies** beyond the string- + stub-confirmed presence.
- The FLIX vector-register-window inner bundles (`0xf6ed..0xfa88`) — not byte-pinnable (no `.xt.prop`).

All facts read as derived from shipped-artifact static analysis (the disassembly, the `.rodata`/DRAM
bytes, the shipped C headers, and the `instruction_mapping.json`) — lawful interoperability RE.

---

## Cross-references

- [Activation + Transcendental Table Engine](../../uarch/activation-transcendental-tables.md) — the
  two table engines; Engine B (the ACT/DVE PWP piecewise-cubic) is the machine Exponential's exp() runs
  on (CAM/PROFILE/CONTROL/BUCKET format, the `{d0..d3,x0}` cubic, the host-content wall).
- [Tensor-Scalar (the `S3D3_TS` family)](tensor-scalar.md) *(planned)* — the struct Exponential reuses
  and the `"S: Tensor-Scalar"` worker its handler hands off to, with `op0`/`op1` *active* there and
  *forced Bypass* here.
- [Activate + the PWL Application Mechanism](activate-pwl.md) — the `0x23 ACTIVATION_TABLE_LOAD` install
  path that stages the host PWL coefficients, and the `0x24` accumulator drain Exponential can engage.
- [Rand2 (user random-tensor op)](rand2.md) — the DVE sibling that *is* random (`0xe2`, `d3_rand`,
  XORWOW + UniformInRange); the contrast that the do-not-repeat row (§9b) pins.
- [ISA Batch 14 — fp16 Transcendental Seeds (`hp_lookup`)](../../isa/ref/b14-hp-lookup.md) — the fp16
  seed-lookup side: `recip0`/`rsqrt0`/`nexp0`/`nexp01` seeds; there is no `ivp_exp` mnemonic, the exp is
  the firmware PWL op documented here.
- [ISA Batch 15 — fp32 Transcendental Seeds (`sp_lookup`)](../../isa/ref/b15-sp-lookup.md) — the fp32
  `nexp01` range-split + the seed roster; the firmware-side set-up primitives (§7c) draw from this family.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the FW-42 host-content
  wall, and the OBSERVED/INFERRED/CARRIED vocabulary every claim here carries.
- [The Do-Not-Repeat / Correction Ledger](../../reference/correction-ledger.md) — the curated register
  the §9b row belongs to (the sampler-vs-transform correction).
