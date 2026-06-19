# BatchNormalize — ParamLoad (`BatchNormParamLoad` / `ParamLoad2`)

This page decodes the **parameter-staging** corner of the DVE (Data/Vector Engine,
`engine_idx=3`) **BatchNormalize** kernel family on the GPSIMD NeuronCore — the two ops the
device firmware self-names `S: BatchNormalizeParamLoad` (opcode `0x64`) and
`S: BatchNormalizeParamLoad2` (opcode `0x8e`). It **closes** the four-page batch-norm family
(forward statistics, back-prop apply, gradient accumulate, and this — param load). Where
[Stats2](batchnorm-forward.md) *computes* the forward mean and `n·var`, ParamLoad does the
opposite: it **stages five precomputed back-prop parameters** — `N`, `batch_mean` (μb), and the
three `A`/`B`/`C` affine coefficients — into the DVE per-lane datapath flops so the subsequent
[GradAccum](batchnorm-gradaccum.md) and [Back-Prop](batchnorm-backprop.md) apply can read them.
The first thing this page establishes byte-for-byte is what the op is **not**: it is **not** a
compute kernel. The handler bodies are 104 / 92 bytes of pure decode-and-stage — no MAC loop,
no reciprocal/`rsqrt` machinery — which directly answers the family's pinned question
(*where does `1/√(var+ε)` come from?*) the same way the forward page does: **not here, not on
the DVE at all.**

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD compute core's** own
firmware — windowed-ABI Xtensa code in the `ncore2gp` (Xtensa24, RI-2022.9, NX1.1.4, 32-byte
FLIX/VLIW) configuration — plus its NX/SEQ sequencer dispatch. Every device fact below is
byte-pinned to a DVE carve re-derived **this pass** from `libnrtucode_internal.so` with the
native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); every host-ISA fact is read out of the
`aws_neuron_isa_tpb_*.h` arch-isa headers shipped in the same customop-lib package and
**compile-verified with `gcc`** (`sizeof`/`offsetof`) this pass. The `extracted/` tree is
gitignored — reach it with `fd --no-ignore` or absolute paths. Confidence and evidence tags
follow the project [Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / proven-by-compile, `[MED/INFERRED]` = reasoned over
OBSERVED, `[…/CARRIED]` = re-used at a cited sibling report's confidence without re-reading the
artifact this pass.

> **NOTE — what was carved this pass, and the exact objects used.** The DVE firmware is the
> same `.rodata`-resident carve as [Stats2](batchnorm-forward.md) (so **file offset == device
> VA** for `.text`/`.rodata`; no `.data` delta applies to these carves), sha256-verified
> **identical** this pass: `DVE_DEBUG_IRAM` `259769ff…` (handler bodies + self-name logs),
> `DVE_DEBUG_DRAM` `c106642d…` (`S:` strings + dispatch table), `DVE_PERF_IRAM` `9fa066f4…`,
> `DVE_PERF_DRAM` `eb980f98…`. IRAM offsets == device IRAM VA (reset vector at byte 0); DRAM
> string offset == device DRAM VA − `0x80000`. The two operand structs were read field-exact
> from `aws_neuron_isa_tpb_s2_bn.h` / `…s2_bnpl2.h` and **compile-checked this pass** (`gcc`
> `offsetof`/`sizeof` reproduced in §3). `objdump` exit 0, empty stderr. `[HIGH/OBSERVED]`

> **CORRECTION — this page corrects a sibling-report premise.** An earlier survey
> (`SX-FW-37 §5.2`) placed `ParamLoad2`'s `src_mem_pattern` at **offset 16** (assuming it
> shares `S2_BN`'s leading `reserved0[4]`@12 + `src`@16 layout). The **compile-verified truth**
> is `src_mem_pattern`@**12** — `S2_BNPL2` **drops** the leading 4-byte reserved field and puts
> the src tensor first, pushing its `reserved0[4]` to @24 and `element_count` to @28. The
> `imm0_ptr`@40 / `imm1_ptr`@44 / `imm0_src`@48 / `imm1_src`@49 offsets the earlier survey cited
> **are** correct; only the `src` base was off by 4. The `S2_BN` (`ParamLoad`) layout was always
> correct. Both are re-proven by `gcc` below. `[HIGH/OBSERVED — gcc offsetof this pass]`

---

## 0. TL;DR — the param-staging model in nine facts

1. **Two ops, one staging step.** `BatchNormParamLoad` (`0x64`, struct `S2_BN`) and
   `BatchNormParamLoad2` (`0x8e`, struct `S2_BNPL2`) both **load five precomputed back-prop
   parameters** into the DVE per-lane datapath flops. `ParamLoad2` is the maintained refactor of
   `ParamLoad`. `[HIGH/OBSERVED — both 64-B headers + both handler bodies decoded]`
2. **The five values** (`s2_bn.h`, verbatim): `N` (an fp32 **instruction** immediate),
   `batch_mean` (μb — an fp32 **pointer** immediate read from psum/sbuf), and `A`/`B`/`C`
   (the three back-prop affine coefficients, in that order in the `src_mem_pattern`,
   `fp32`/`fp16`/`bf16`). `[HIGH/OBSERVED header]`
3. **No DST, no compute.** Neither struct has a destination tensor (`S2_BN` header: *"one 2d
   SRC, no DST"*); the destination is **internal DVE per-channel state** (the `DATAPATH_RAM`
   bank). The bodies are 104 / 92 B of decode + stage — **no `ivp_mulus*` MAC, no
   `ivp_baddnorm` accumulate, no `div`/`divn`**. The op *moves* host-built parameters into the
   datapath; the math that uses them is [Back-Prop](batchnorm-backprop.md)'s. `[HIGH/OBSERVED —
   body disasm, no-MAC]`
4. **`rsqrt` is in FORWARD, not here.** Neither op computes `inv_std = 1/√(var+ε)`.
   `rg ivp_(rsqrt0|recip0|sqrt0)` over **both** DVE builds = **0**. `σb-0.5` is
   forward/host-precomputed and **folded into `A`/`B`/`C`** (and threaded to GradAccum as its
   `imm1`). `[HIGH/OBSERVED — grep 0/0 both builds + the no-compute bodies]`
5. **Dispatch (byte-exact, both chains).** `0x64` → DRAM `table1[0x8a0]=0x30b6`; `0x8e` →
   `table1[0x948]=0x30be`; each a `call8 <thunk> ; j 0x3212` converging to the common Handler
   invoke @`0x3212`. `[HIGH/OBSERVED — bytes read this pass]`
6. **Structs (compile-verified, both 64 B).** `S2_BN`: `src`@16 (3 elems), `imm1=N`@28,
   `dtype`@32, `num_active_channels`@34, `imm0_ptr=batch_mean`@44, `imm0_src`@48. `S2_BNPL2`:
   `src`@12 (2 elems), `element_count`@28, `src_dtype`@32, `immediate_dtype`@33, `imm0_ptr`@40,
   `imm1_ptr`@44, `imm0_src`@48, `imm1_src`@49. `[HIGH/OBSERVED — gcc this pass]`
7. **The v1→v2 distinction is *repackaging*, not "add reg-ptr".** Unlike Stats→Stats2 or
   GradAccum→GradAccum2 (where v2 *added* register-pointer immediates), `S2_BN` (v1) **already**
   supports `RegPtrImmediate`. `ParamLoad2` instead **moves one param out of the src tensor into
   a second, independent pointer-immediate** (src 3→2 elems; pointer-immediates 1→2, each with
   its own src selector). The firmware witnesses this: `ParamLoad` has **one** `bnei a2,2` field
   test, `ParamLoad2` has **two**. `[HIGH/OBSERVED — struct + body disasm]`
8. **Dtype matrix.** src ∈ `{FP32, FP16, BFLOAT16}` → fp32 in the DVE (`AllowFP32R=False`);
   `N`/`element_count` is an fp32 **instruction** immediate; `batch_mean`/the ptr-imms are
   `{FP32,FP16,BFLOAT16}` pointers. **Narrower than GradAccum** (which also allows `INT32` src).
   `[HIGH/OBSERVED — validity predicates]`
9. **Per-gen presence.** Both opcodes (`0x64`/`0x8e`) and both structs present in
   `cayman / mariana / maverick / sunda`; the `S2_BN`/`S2_BNPL2` **typedef bodies are
   byte-identical cayman==sunda**. `S2_BN` is a **shared** wire format (4 opcodes); `S2_BNPL2`
   is bespoke (1:1). POOL carries no batch-norm opcode in any gen. `[HIGH/OBSERVED]`

---

## 1. The two ops, their structs, and the shared-wire-format truth

`ParamLoad`/`ParamLoad2` sit in the same DVE BN opcode block as the forward/grad/backprop
handlers. The opcode enum and its **deprecation markers** are read directly from
`aws_neuron_isa_tpb_common.h`:

```
NEURON_ISA_TPB_OPCODE_BATCH_NORM_PARAM_LOAD   = 0x64,   // n, use BatchNormParamLoad2 instead
NEURON_ISA_TPB_OPCODE_BATCH_NORM_PARAM_LOAD2  = 0x8e,   // Y
```

`[HIGH/OBSERVED — common.h opcode enum, lines 192 / 225; the `n`/`Y` markers are the verbatim
comment text]`. The `n` marker on `0x64` is the same deprecation pattern the whole family uses
(`Stats`→`Stats2`, `GradAccum`→`GradAccum2`), and §6 shows the PERF build acts on it: `0x64`
collapses onto a shared deprecated-cluster handler.

**`struct2opcode` (the shared-wire-format truth, `jq` from `instruction_mapping.json`):**

```
S2_BN_STRUCT    -> { BATCH_NORM_PARAM_LOAD, MATCH_VALUE_LOAD,
                     TENSOR_SCALAR_IMM_LD_ARITH, TENSOR_SCALAR_IMM_LD_BITVEC }   (SHARED — 4 opcodes)
S2_BNPL2_STRUCT -> { BATCH_NORM_PARAM_LOAD2 }                                    (bespoke — 1:1)
```

`[HIGH/OBSERVED — jq this pass]`. `S2_BN` is the DVE's generic *"load N values into the engine's
storage flops"* wire format: the **same 64-byte layout** is the operand of `MatchValueLoad`
(which loads `mv[0..7]` for `FindIndex8`/`MatchReplace8`) and of `TensorScalarImmLd[Arith/Bitvec]`
(which loads ≤ 8 per-channel scalars for a following `TensorScalarPtrMulti`). The **opcode byte
selects the meaning**; the handler body @`0xb62c` is the `BatchNormParamLoad` arm. `S2_BNPL2`,
by contrast, exists **only** for `ParamLoad2`.

> **GOTCHA — the same struct, four very different validity gates.** Because `S2_BN` is shared,
> the *same wire bytes* are validated by **four different predicate sets**. For
> `BatchNormParamLoad` the src **must be 3 elements** (`has_bn_param_load_src_element_cnt`),
> `imm1`/`imm0_ptr` are **live**; for `MatchValueLoad` the src **must be 8 elements**
> (`has_match_value_load_src_element_cnt`) and `imm1`/`imm0_ptr` **must be zero**
> (`has_s2_bn_zero_immediates`); for `TensorScalarImmLd*` the src is **1..8 elements** and the
> immediates again must be zero. A reimplementer reusing the `S2_BN` encoder across these four
> opcodes **must** clear `imm1`/`imm0_ptr` for the non-BN ops — leaving the BN immediates in
> place fails the `…zero_immediates` gate. `[HIGH/OBSERVED — the three `is_valid_*` predicate
> bodies in `s2_bn.h`]`

| opcode | name | struct | status | `S:` self-name (DRAM off) | body (entry) |
|---|---|---|---|---|---|
| `0x64` | `BATCH_NORM_PARAM_LOAD` | `S2_BN` | `n` (use v2) | `0x22f0` `S: BatchNormalizeParamLoad` | `0xb62c` (`entry a1, 48`) |
| `0x8e` | `BATCH_NORM_PARAM_LOAD2` | `S2_BNPL2` | **Y** | `0x230c` `S: BatchNormalizeParamLoad2` | `0xb694` (`entry a1, 48`) |

`[HIGH/OBSERVED — opcode enum + struct2opcode + both `S:` strings re-read this pass at DRAM
0x22f0/0x230c via `strings -t x DVE_DEBUG_DRAM.bin`; both `entry` widths read from
`DVE_DEBUG_IRAM`]`. The self-name bytes at `0x22f0` are
`53 3a 20 42 61 74 63 68 4e 6f 72 6d 61 6c 69 7a 65 50 61 72 61 6d 4c 6f 61 64 0a`
= `"S: BatchNormalizeParamLoad\n"`; `0x230c` is the same prefix with `…ParamLoad2\n`; the
[Back-Prop](batchnorm-backprop.md) self-name `S: BatchNormalizeBackProp` follows at `0x2350`.

---

## 2. What is loaded, and where — the "five values into each pipeline"

The `s2_bn.h` header documents the contract in plain text (verbatim sense):

> *"This instruction loads **five values** into **each** of the DVE pipelines to supply
> parameters to the batchnorm back propagation operation. The five values loaded:
> `N` — an immediate embedded in the instruction, fp32 format; `batch_mean` — an immediate
> pointer in the instruction, value read from psum/sbuf, fp32 format; `A/B/C` — the A/B/C terms
> will be in that order in the `src_mem_pattern` in fp32/fp16/bf16 format."* — `s2_bn.h`

So the **source** of every parameter is the **host-built instruction encoding** plus
sbuf/psum pointers — there is **no** bulk `γ`/`β`/running-stats DMA or LUT resident in the DVE
firmware. `[HIGH/OBSERVED — header]`

### 2.1 WHERE the five values land — the per-lane `DATAPATH_RAM` flops

Neither struct carries a destination tensor — the `S2_BN` header title is literally
*"one 2d SRC, no DST"*, and the validity predicate calls `tensor2d_valid(…, WriteTensor::False,
…)` on the **src** (read-only). The destination is **internal DVE state**, addressed per active
channel via `num_active_channels`@34 (`1..POOLING_NUM_CHANNELS`, where
`POOLING_NUM_CHANNELS = 128U`). The hardware bank is the DVE datapath state RAM in the RTL
address map:

| HW region | base (TPB_0) | size | role for ParamLoad |
|---|---|---|---|
| `TPB_0_DVE_BANK_DATAPATH_RAM` | `0x2802B82000` | `0x4000` | **the ParamLoad target** — per-lane datapath flops |
| `TPB_0_DVE_BANK_PARAMETER_RAM` | `0x2802B87000` | `0x400` | the 256×fp32 reciprocal RAM (Stats2's mean divide; *not* ParamLoad's) |

`[HIGH/OBSERVED — header "no DST" + the `WriteTensor::False` src predicate + the RTL map
re-read this pass; the **exact** per-lane flop addressing within `DATAPATH_RAM` is
INFERRED-HIGH — the header says "into each of the DVE pipelines" but does not enumerate a
slot map]`. The [Back-Prop](batchnorm-backprop.md) apply (`op 0x65`, `S3S3D3_TT`) is what
**reads** this staged per-channel state back out.

> **NOTE — "five values into *each* pipeline" is per-lane broadcast, not a tensor write.** The
> phrasing distinguishes ParamLoad from every *other* DVE op that has a `dst_mem_pattern`:
> ParamLoad does not write SBUF/PSUM at all. It distributes the five scalars across the
> `num_active_channels` active lanes' datapath flops — internal engine state that the next BN
> instruction in the same datapath consumes. This is why the body has no store-to-tensor and no
> `dst` field. `[HIGH/OBSERVED — "no DST" + the absence of a dst tensor; the per-lane
> distribution mechanism INFERRED from the header wording]`

### 2.2 The `A`/`B`/`C` terms = the precomputed BN-backprop affine coefficients

The standard batch-norm backward gradient

```
d_x = (γ · inv_std / N) · ( N·d_y − Σd_y − x_norm · Σ(d_y · x_norm) )
```

is realised on Neuron as a **per-element affine** in the saved ofmap and incoming gradient with
**three host-precomputed coefficients** — the `A`/`B`/`C` ParamLoad stages (a 3-element src),
**plus** the count `N` and the mean `μb`. The DVE does **not** derive `A`/`B`/`C`: the
host/compiler folds `γ`, `inv_std = σb-0.5`, `N`, and the GradAccum sums into them and ships
them ready-made. `[the `A`/`B`/`C`-are-the-backprop-affine-coefficients identity is
INFERRED-HIGH — from the header naming ("supply parameters to the batchnorm back propagation
operation") + the standard BN-backward algebra + the [Back-Prop](batchnorm-backprop.md) apply
that consumes them; the header references an internal design doc (a `quip-amazon.com` link, not
in the binary) for the exact `A=…`/`B=…`/`C=…` definitions, so the precise algebraic form of
each coefficient is INFERRED]`

### 2.3 The `rsqrt` determination — it is **not** here

This is the question the BN family turns on. The answer for ParamLoad is the same as for
[Stats2](batchnorm-forward.md), and proven the same way:

* **Neither op computes `inv_std`.** The bodies are 104 / 92-byte decode+stage routines with
  **no MAC and no reciprocal machinery** (§4). `[OBSERVED — body disasm]`
* **`rg ivp_(rsqrt0|recip0|sqrt0)` over both DVE builds = 0** (re-confirmed this pass: `0` in
  DEBUG, `0` in PERF). The DVE's *only* seed→Newton is reciprocal **division** —
  `ivp_div0nxf16t ×2`, `ivp_divnn_2xf32t ×1`, `ivp_mulsonen_2xf32t ×1`, `ivp_mulsonenxf16t ×2`
  in PERF — used for `sum ÷ count`, **not** `rsqrt`. `[HIGH/OBSERVED — grep counts re-run this
  pass]`
* **`σb-0.5` is forward/host-precomputed.** It arrives downstream as
  [GradAccum](batchnorm-gradaccum.md)'s `imm1` (*"saved from forward propagation"*, the
  `s3s3d1_bn.h` header), and it is **folded into ParamLoad's `A`/`B`/`C`** host-side. So
  `inv_std` is computed **exactly once, host-side in forward** — never in Stats2 (which emits
  `mean` + `n·var` only), never in ParamLoad, never in GradAccum. `[HIGH/OBSERVED — the grep +
  the headers + the no-compute body; the exact forward locus (ACT-PWL rsqrt table vs host CPU)
  is INFERRED, external to the DVE image]`

---

## 3. The operand structs — compile-verified (`gcc` `offsetof`/`sizeof`)

Both headers ship in this checkout (`aws_neuron_isa_tpb_s2_bn.h`,
`aws_neuron_isa_tpb_s2_bnpl2.h`), each with an `ISA_STATIC_ASSERT(sizeof == 64)`. A standalone
`gcc` `offsetof`/`sizeof` probe over the CAYMAN headers **passed** this pass — output:

```
S2_BN sizeof=64
  src=16 imm1=28 dtype=32 nac=34 imm0_ptr=44 imm0_src=48
S2_BNPL2 sizeof=64
  src=12 reserved0=24 element_count=28 src_dtype=32 imm_dtype=33 nac=34
         imm0_ptr=40 imm1_ptr=44 imm0_src=48 imm1_src=49
TENSOR2D=12 IMM_VAL=4 IMM_SRC=1
```

`[HIGH/OBSERVED — gcc `offsetof`/`sizeof` over the shipped CAYMAN headers, run this pass]`

### 3.1 `NEURON_ISA_TPB_S2_BN_STRUCT` (op `0x64`, ParamLoad) — *"one 2d SRC, no DST"*

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `HEADER` | `{opcode 0x64, inst_word_len, debug}` |
| 4 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 4 | `reserved0[4]` | — | must be zero |
| 16 | 12 | `src_mem_pattern` | `TENSOR2D` | the **A/B/C** terms (3 elements, in order) |
| 28 | 4 | `imm1` | `IMM_VAL_INST_FIELD` | **N** (fp32 **instruction** immediate, `imm_arith_fp32`) |
| 32 | 1 | `dtype` | `DTYPE` | A/B/C dtype ∈ `{FP32,FP16,BF16}` (`AllowFP32R=False`) |
| 33 | 1 | `reserved1[1]` | — | must be zero |
| 34 | 1 | `num_active_channels` | `u8` | `1..POOLING_NUM_CHANNELS` (128) |
| 35 | 9 | `reserved2[9]` | — | must be zero |
| 44 | 4 | `imm0_ptr` | `IMM_VAL_INST_FIELD` | **batch_mean** (μb, `PartitionOffset` ptr, fp32) |
| 48 | 1 | `imm0_src` | `IMM_SRC` | `0`=InstImm \| `1`=PointerImm \| `2`=RegPtr |
| 49 | 15 | `reserved3[15]` | — | must be zero |

```c
typedef struct NEURON_ISA_TPB_S2_BN_STRUCT {
    NEURON_ISA_TPB_HEADER             header;               //  0 -  3   opcode 0x64
    NEURON_ISA_TPB_EVENTS             events;               //  4 - 11
    uint8_t                           reserved0[4];         // 12 - 15
    NEURON_ISA_TPB_TENSOR2D           src_mem_pattern;      // 16 - 27   A/B/C (3 elements)
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm1;                 // 28 - 31   N (fp32 inst-immediate)
    NEURON_ISA_TPB_DTYPE              dtype;                // 32        A/B/C dtype
    uint8_t                           reserved1[1];         // 33
    uint8_t                           num_active_channels;  // 34        1..128
    uint8_t                           reserved2[9];         // 35 - 43
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm0_ptr;             // 44 - 47   batch_mean (ptr)
    NEURON_ISA_TPB_IMM_SRC            imm0_src;             // 48        src of imm0_ptr
    uint8_t                           reserved3[15];        // 49 - 63
} NEURON_ISA_TPB_S2_BN_STRUCT;                              // ISA_STATIC_ASSERT == 64
```

**Constraints (validity predicates, OBSERVED in `s2_bn.h`):**
`has_bn_param_load_src_element_cnt` — src `num_elem` must be `{3×1}` or `{1×3}` (the A/B/C in
order), **or** `shape_from_register`. **One** `imm0_src` field (single pointer-immediate).
`has_valid_bn_param_load_imm0` — if `imm0_src` is `PointerImmediate` (or `0`), `imm0_ptr` must
be 4-byte-aligned and address an active channel; if `RegPtrImmediate`, `imm0_ptr.imm_reg` must
be a valid register. `[HIGH/OBSERVED]`

> **GOTCHA — `imm0_src == 0` is "pointer from instruction", not "instruction-immediate".** The
> `IMM_SRC` enum names `INSTRUCTION_IMMEDIATE=0`, but for `imm0_ptr` the predicate
> `has_valid_bn_param_load_imm0` treats `imm0_src==0` **as a pointer-immediate** (`(imm0_src ==
> PointerImmediate) || (imm0_src == 0)`). The header comment is explicit: *"imm0_src used to be
> a reserved field (must be zero); allow zero imm0_src just so old binaries don't fail this ISA
> check; imm0_src == 0 means imm0 is a pointer from instruction."* So `batch_mean` is **always**
> a pointer (instruction-supplied when `0`/`1`, register-supplied when `2`); only `N` (`imm1`)
> is a true in-instruction fp32 scalar. `[HIGH/OBSERVED — header comment + predicate]`

### 3.2 `NEURON_ISA_TPB_S2_BNPL2_STRUCT` (op `0x8e`, ParamLoad2)

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `HEADER` | opcode `0x8e` |
| 4 | 8 | `events` | `EVENTS` | sync |
| **12** | 12 | `src_mem_pattern` | `TENSOR2D` | the **2-element** param tensor (src=2) |
| 24 | 4 | `reserved0[4]` | — | must be zero |
| 28 | 4 | `element_count` | `IMM_VAL_INST_FIELD` | the N/count (fp32 **instruction** immediate) |
| 32 | 1 | `src_dtype` | `DTYPE` | `{FP32,FP16,BF16}` (`AllowFP32R=False`) |
| 33 | 1 | `immediate_dtype` | `DTYPE` | `{FP32,FP16,BF16}` (both ptr-imms) |
| 34 | 1 | `num_active_channels` | `u8` | `1..128` |
| 35 | 5 | `reserved1[5]` | — | must be zero |
| 40 | 4 | `imm0_ptr` | `IMM_VAL_INST_FIELD` | ptr-imm #0 (must be pointer, dtype-aligned, active ch.) |
| 44 | 4 | `imm1_ptr` | `IMM_VAL_INST_FIELD` | ptr-imm #1 (must be pointer, dtype-aligned, active ch.) |
| 48 | 1 | `imm0_src` | `IMM_SRC` | `0/1/2` (inst/ptr/reg) for imm0 |
| 49 | 1 | `imm1_src` | `IMM_SRC` | `0/1/2` (inst/ptr/reg) for imm1 |
| 50 | 14 | `reserved2[14]` | — | must be zero |

```c
typedef struct NEURON_ISA_TPB_S2_BNPL2_STRUCT {
    NEURON_ISA_TPB_HEADER             header;               //  0 -  3   opcode 0x8e
    NEURON_ISA_TPB_EVENTS             events;               //  4 - 11
    NEURON_ISA_TPB_TENSOR2D           src_mem_pattern;      // 12 - 23   *** src FIRST (2 elements)
    uint8_t                           reserved0[4];         // 24 - 27
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD element_count;        // 28 - 31   N (fp32 inst-immediate)
    NEURON_ISA_TPB_DTYPE              src_dtype;            // 32
    NEURON_ISA_TPB_DTYPE              immediate_dtype;      // 33        both ptr-imms
    uint8_t                           num_active_channels;  // 34
    uint8_t                           reserved1[5];         // 35 - 39
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm0_ptr;             // 40 - 43   ptr-imm #0
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm1_ptr;             // 44 - 47   ptr-imm #1
    NEURON_ISA_TPB_IMM_SRC            imm0_src;             // 48
    NEURON_ISA_TPB_IMM_SRC            imm1_src;             // 49
    uint8_t                           reserved2[14];        // 50 - 63
} NEURON_ISA_TPB_S2_BNPL2_STRUCT;                           // ISA_STATIC_ASSERT == 64
```

**Constraints (validity predicates, OBSERVED in `s2_bnpl2.h`):**
`has_bnpl2_src_element_cnt` — src `num_elem` must be `{1×2}` or `{2×1}` (src=2), **or**
`shape_from_register`. `element_count` must be an instruction immediate, **fp32**. Each of
`imm0_ptr`/`imm1_ptr` is validated by the **same** `has_valid_s2_bnpl2_imm_ptr` helper against
its own `imm{0,1}_src` and the **shared** `immediate_dtype`: pointer (or `0`) → must be
dtype-aligned and address an active channel; `RegPtrImmediate` → must be a valid register.
**Two** independent `imm_src` fields. `[HIGH/OBSERVED]`

### 3.3 Component types (compile-verified)

`TENSOR2D` = `{ADDR4 start_addr (4); int16 step_elem[2] (4); uint16 num_elem[2] (4)}` = **12 B**.
`IMM_VAL_INST_FIELD` = a **4-B** union `{uint32 imm_ptr; IMM_REG imm_reg; float imm_arith_fp32}`
— `imm_ptr` when `PointerImmediate`, `imm_reg` when `RegPtrImmediate`, `imm_arith_fp32` when
`InstructionImmediate`. `IMM_SRC` = **1-B** enum `{INSTRUCTION_IMMEDIATE=0,
POINTER_IMMEDIATE=1, REG_PTR_IMMEDIATE=2}`. `[HIGH/OBSERVED — gcc sizeof + the enum body in
common.h lines 1207-1211]`

---

## 4. The two dispatch chains — byte-exact (DVE DEBUG build)

Both opcodes route through the SEQ direct-indexed DRAM jump table: `key = opcode − 0x41`,
`table1` base at DRAM file offset `0x814` (device VA `0x80814`), word = LE32 at `0x814 + 4·key`.
Read **byte-exact** this pass (`python3 struct.unpack_from`):

| opcode | idx | table off | LE32 word | trampoline |
|---|---|---|---|---|
| `0x64` ParamLoad | `0x23` | `0x8a0` | `b6 30 00 00` | `0x30b6` |
| `0x8e` ParamLoad2 | `0x4d` | `0x948` | `be 30 00 00` | `0x30be` |

*(context: `0x63` GradAccum → `0x309e`; `0x65` BackProp → `0x30ae`; `0x66` LoadParameterRAM →
`0x30e6`; `0x94` GradAccum2 → `0x30a6`.)* `[HIGH/OBSERVED — bytes read from `DVE_DEBUG_DRAM.bin`]`

### 4.1 Trampolines → register-handler thunks (OBSERVED byte-exact this pass)

```
0x30b6 ParamLoad : a5 f4 fe   call8 0x2000 ;  46 55 00   j 0x3212
0x30be ParamLoad2: e5 f5 fe   call8 0x201c ;  46 53 00   j 0x3212
```

The two thunks sit at stride `0x1c` (`0x2000` / `0x201c`); each stages the handler **body VA**
into the Handler object frame at off 12 via the Xtensa `const16`-hi/`const16`-lo idiom:

```
0x2000 ParamLoad :  entry a1,48 ; const16 a2,0 ; const16 a2,0xb62c ; s32i a2,a1,12 ;
                    [cf] l32r a1,_ ; l32r a10,_ ; call8 0x951c ; retw
0x201c ParamLoad2:  entry a1,48 ; const16 a2,0 ; const16 a2,0xb694 ; s32i a2,a1,12 ;
                    [cf] l32r a1,_ ; l32r a10,_ ; call8 0x951c ; retw
```

`[HIGH/OBSERVED — disassembled natively this pass; the `cf`-prefixed `l32r` pair is a FLIX
bundle, the `const16`/`s32i`/`call8` are clean scalar]`

### 4.2 The ParamLoad body @`0xb62c` (104 B) — one `imm0_src` test

```
b62c:  366100   entry    a1, 48
b635:  a40800   const16  a10, 8
b638:  a4f022   const16  a10, 0x22f0    ; *** "S: BatchNormalizeParamLoad"
b63b:  659d0c   call8    0x18010        ;     the 'S:'-log helper
b644:  240800   const16  a2, 8
b647:  244023   const16  a2, 0x2340     ; *** stage the per-op decode descriptor (word[0]=1)
b64a:  2f...                            ; *** FLIX-VLIW decode/stage region (desyncs under sweep)
b658:  2931     s32i.n   a2, a1, 12     ;     write the staged imm-src into the inst context
b65a:  2831     l32i.n   a2, a1, 12     ;     read the imm0_src field back
b65c:  66220c   bnei     a2, 2, 0xb66c  ; *** imm0_src == RegPtrImmediate(=2) ? -> reg-ptr path
b66c:  1df0     retw.n                  ;     (the inst/ptr path falls through)
b670:  366100   entry    a1, 48         ; *** the inline reg-pointer-fetch sub-routine
```

`[HIGH through the log + the `const16 0x2340` descriptor + the single `bnei a2,2`@0xb65c + the
inline reg-fetch `entry`@0xb670; the interior FLIX literal interleave @0xb64a is MED — reported
structurally only]`

### 4.3 The ParamLoad2 body @`0xb694` (92 B) — **two** `imm{0,1}_src` tests

```
b694:  366100   entry    a1, 48
b6a0:  a40c23   const16  a10, 0x230c    ; *** "S: BatchNormalizeParamLoad2"
b6a3:  e5960c   call8    0x18010
b6af:  243023   const16  a2, 0x2330     ; *** stage v2 descriptor (word[0]=0xb)
b6cd:  66220b   bnei     a2, 2, 0xb6dc  ; *** imm0_src == 2 ?  ->  b6d4: call8 0x9d48 (imm0 reg-fetch)
b6de:  66220a   bnei     a2, 2, 0xb6ec  ; *** imm1_src == 2 ?  ->  b6e4: call8 0x9d6c (imm1 reg-fetch)
```

The two reg-fetch sub-handlers `0x9d48` (imm0) / `0x9d6c` (imm1) are near-identical (stride
`0x24`, `entry a1,48`), differing at byte +4 (`18 d6` `l32i.n a1,a6,52` vs `1c d6`
`movi.n a6,29`) — which register slot the immediate pointer is read from. `[HIGH/OBSERVED —
the self-name + descriptor + the **two** `bnei a2,2` tests + the two `call8` reg-fetch targets,
disassembled natively this pass; the interior FLIX literal MED]`

> **GOTCHA — the FLIX literal interleave desyncs the *count* of `bnei a2,2` under stock
> objdump.** A linear `rg -c 'bnei a2,2'` over the ParamLoad body returns **2**, not 1 — because
> the 32-byte FLIX/literal span at `0xb64a` re-decodes into a spurious second `66 22 ..` byte
> pattern. The **byte-grounded** truth is the single clean `bnei a2,2`@`0xb65c` (the `66 22 0c`
> at a real instruction boundary). The doubled-test signature is real **only** for `ParamLoad2`,
> where both `0xb6cd` and `0xb6de` are clean boundaries with distinct `call8` targets. Trust the
> boundary-aligned bytes, not the linear count. `[HIGH/OBSERVED — both decodes run this pass]`

### 4.4 The staged per-op descriptors

Each body `const16`-stages a 16-byte decode-spec block from the DRAM string-table region
(read byte-exact this pass):

```
ParamLoad  @0x2340 = [ 0x00000001  0x5c000000  0x16015c36  0x02820001 ]   word[0]=1
ParamLoad2 @0x2330 = [ 0x0000000b  0x6c000000  0x160c2034  0x02818001 ]   word[0]=0xb
```

`word[0]` is op-specific (`1` / `0xb`); `word[1]` high byte (`0x5c`=92 / `0x6c`=108) and
`word[2..3]` encode the per-op inst-word-len / field-decode flags. **Unlike** the GradAccum
descriptors (whose `word[0]=3` is the dst element count), ParamLoad has **no dst**, so `word[0]`
here is a decode-class / inst-word-len flag, **not** a dst count. `[HIGH/OBSERVED bytes; the
precise field semantics of `word[0..3]` are INFERRED — they are the op's decode-spec]`

### 4.5 Convergence

Both chains converge at the common Handler invoke @`0x3212` = `j 0x2e87` (the C++
`Handler::execute()` path, shared by every BN op). `[HIGH/OBSERVED]`

---

## 5. The ParamLoad-vs-ParamLoad2 distinction — decisive (with a twist)

Applying the family's v1/v2 lens — and finding it differs from the GradAccum case. The
`s2_bnpl2.h` header states the intent verbatim:

> *"The `BatchNormParamLoad2` instruction simplifies the `BatchNormParamLoad` instruction. This
> instruction **combines the 5 different parameter values** needed for `BatchNormParamLoad`
> **into one 2D tensor and three immediates**. Doing this will allow the compiler to perform
> optimizations on the parameter values more easily."* — `s2_bnpl2.h`

Eliminating the wrong hypotheses by evidence:

* **NOT a different parameter set.** Both load the same five back-prop params. `[HIGH/OBSERVED —
  both headers]`
* **NOT "v2 ADDS reg-pointer immediates" (the GradAccum story).** `S2_BN` (v1) **already** has
  `imm0_src`@48 with `RegPtrImmediate` support, and the v1 body @`0xb62c` **already** has a
  `bnei a2,2` reg-ptr test (@`0xb65c`). Register-sourcing is **not** the v2 novelty here.
  `[HIGH/OBSERVED — struct + body]`
* **NOT a dtype-path change.** Both allow src/imm ∈ `{FP32,FP16,BF16}`. `[HIGH/OBSERVED]`

**The actual difference — parameter packaging + pointer-immediate count:**

| | `S2_BN` (v1, `0x64`) | `S2_BNPL2` (v2, `0x8e`) |
|---|---|---|
| `src_mem_pattern` | **3 elements** (A/B/C) @16 | **2 elements** @12 |
| instruction immediate | `imm1 = N` @28 | `element_count` @28 |
| pointer immediates | **one** (`imm0_ptr=batch_mean` @44) | **two** (`imm0_ptr` @40 + `imm1_ptr` @44) |
| imm src selectors | **one** (`imm0_src` @48) | **two** (`imm0_src` @48 + `imm1_src` @49) |
| `bnei a2,2` tests in body | **1** (@`0xb65c`) | **2** (@`0xb6cd`, @`0xb6de`) |

Net: **v2 moves one of the A/B/C terms out of the src tensor and into a second, independent
pointer-immediate** (src `3→2` elems; pointer-immediates `1→2`), and gives **each**
pointer-immediate its own inst/ptr/reg source selector — so *"the compiler can perform
optimizations on the parameter values more easily."* The firmware witnesses this **byte-exact**:
the **doubled** `bnei a2,2` machinery in the `ParamLoad2` body (two tests → two distinct
reg-fetch sub-handlers `0x9d48`/`0x9d6c`) is the signature of the second pointer-immediate's
src selector. `[HIGH/OBSERVED — compile-verified structs + both body disasms]`

**Why v2 exists:** `0x64` is marked `n, use BatchNormParamLoad2 instead` and `0x8e` is `Y` — the
same deprecation pattern as `Stats`→`Stats2` and `GradAccum`→`GradAccum2`. `[HIGH/OBSERVED —
common.h markers]`

---

## 6. dtype matrix + the PERF (production) build

### 6.1 dtype matrix (compile-verified validity predicates)

`DTYPE` ordinals (`common.h`): `BFLOAT16=0x6`, `FP16=0x7`, `INT32=0x8`, `FP32=0xA`,
`FP32R=0xB`.

| op | field | dtypes | source |
|---|---|---|---|
| **ParamLoad** | src A/B/C (`dtype` @32) | `{FP32, FP16, BFLOAT16}` (`AllowFP32R=False`) → fp32 | `s2_bn_dtype` |
| | `imm1 = N` | fp32 **instruction** immediate (`imm_arith_fp32`, must be finite) | `has_valid_bn_param_load_imm0` |
| | `imm0_ptr = batch_mean` | fp32 `PartitionOffset` pointer (4-B aligned, active channel) | predicate |
| | `imm0_src` | `{InstImm=0, PointerImm=1, RegPtr=2}` | enum gate |
| **ParamLoad2** | src (`src_dtype` @32) | `{FP32, FP16, BFLOAT16}` (`AllowFP32R=False`) → fp32 | `has_valid_s2_bnpl2_dtype` |
| | `imm0_ptr` / `imm1_ptr` (`immediate_dtype` @33) | `{FP32, FP16, BFLOAT16}` (`AllowFP32R=False`) | `has_valid_s2_bnpl2_dtype` |
| | `element_count` | fp32 **instruction** immediate | predicate |
| | `imm0_src` @48 / `imm1_src` @49 | `{0,1,2}` per-immediate | enum gate |

All src dtypes are **converted to fp32 in the DVE** before use. `[HIGH/OBSERVED — the
`s2_bn`/`s2_bnpl2` validity predicate comment bodies]`

> **NOTE — ParamLoad's dtype set is NARROWER than GradAccum's.** ParamLoad src ∈
> `{FP32,FP16,BF16}` **only** — **no `INT32`** — whereas [GradAccum](batchnorm-gradaccum.md)'s
> src additionally allows `INT32` (`{FP16,BF16,FP32,INT32}`). The ParamLoad parameters are
> already floating-point affine coefficients; there is no integer staging path. This is also why
> ParamLoad's `S2_BN` cousins `TensorScalarImmLd[Arith/Bitvec]` carry their **own** wider dtype
> gate (`tsm_immld_dtypes`, including `UINT8`/`INT8`/`FP8_EXP{3,4,5}`/`UINT16`/`UINT32`) — the
> `dtype`@32 byte means different things per opcode on this shared struct. `[HIGH/OBSERVED —
> `s2_bn_dtype` vs `tsm_immld_dtypes`]`

### 6.2 The PERF build

PERF strips the `S:` logs. Its DRAM dispatch (table @ `0x814`, read this pass) confirms the
deprecation collapse:

| opcode | idx | PERF off | word | note |
|---|---|---|---|---|
| `0x63` GradAccum | `0x22` | `0x89c` | `0x9090` | shared deprecated-cluster handler |
| `0x64` ParamLoad | `0x23` | `0x8a0` | `0x9090` | **same** shared handler (consistent with `n, use v2`) |
| `0x65` BackProp | `0x24` | `0x8a4` | `0x9090` | shared deprecated-cluster handler |
| `0x8e` ParamLoad2 | `0x4d` | `0x948` | `0x16001840` | **unusable** — idx > `~0x40` overlaps the assertion string pool |

The three deprecated variants `0x63`/`0x64`/`0x65` all route to **one** handler `0x9090` in
PERF; the `0x8e` PERF row overlaps the tiny `0x2fc0` PERF DRAM's assertion string pool and is
**not** a trustworthy handler VA — the DEBUG dispatch (§4) is the reliable substrate for
ParamLoad2. `[HIGH for `0x63`/`0x64`/`0x65` — bytes read this pass; the `0x8e` PERF row is
MED/unusable, flagged honestly — same caveat as GradAccum2's PERF row]`

---

## 7. Per-generation presence

| GEN | opcode `0x64` / `0x8e` | `s2_bn.h` / `s2_bnpl2.h` | typedef body | DVE handler bodies |
|---|---|---|---|---|
| **SUNDA** (v2) | present (`n`/`Y`) | present | **byte-identical to cayman** | not separately diffed |
| **CAYMAN** (v3) | present — **the carve substrate** | present | — | **OBSERVED** (`0xb62c`/`0xb694` decoded) |
| **MARIANA** (v4) | present | present | structurally identical | not separately diffed |
| **MAVERICK** (v5) | present | present | **header-OBSERVED → interior INFERRED** | not carved |

`[HIGH/OBSERVED — per-gen opcode + header presence (`rg`/`fd`-verified this pass: `0x64`/`0x8e`
in `common.h` and both struct headers exist in all four gens); the CAYMAN DVE bodies decoded
this pass; the cayman==sunda typedef-body equality verified this pass]`

> **CORRECTION — "byte-identical cayman==sunda" applies to the *typedef body*, not the whole
> file.** A `diff` of the full `s2_bn.h` between CAYMAN (v3) and SUNDA (v2) is **non-empty**: the
> header tag differs (`ISA header for NC-v3.` vs `NC-v2.`) and the v3 validity-predicate **prose**
> is a *superset* — CAYMAN adds `shape_from_register(…)` to the src-element-count predicate and a
> `s2_bn_zgen_restriction` function that SUNDA lacks. But the **`typedef struct … }` body is
> byte-identical** across the two (the wire layout is stable; only the surrounding comment-form
> validity rules grew). So the **operand layout is portable v2→v3**; the *predicate set* is not.
> `[HIGH/OBSERVED — `diff` of the typedef bodies (empty) vs the full files (header tag + 3
> predicate hunks), run this pass]`

> **MAVERICK (v5) interior — header-OBSERVED only → INFERRED.** Per the
> [generation-grounding policy](../../reference/confidence-model.md), the v5 `0x64`/`0x8e`
> opcode + `s2_bn`/`s2_bnpl2` struct **presence** is OBSERVED (the headers ship in the
> `neuron_maverick_arch_isa` tree), but the v5 DVE handler **interior** is reasoned from the
> CAYMAN carve + the header equality, **not** separately carved and decoded. Treat v5 ParamLoad's
> `entry`-width, descriptor bytes, and dispatch words as **INFERRED** until a v5 DVE image is
> carved and diffed. `[HIGH/OBSERVED header; MED/INFERRED interior]`

POOL engine carries **no** batch-norm opcode in any gen — ParamLoad is a **DVE-only** kernel.
`[HIGH/CARRIED — the POOL `kernel_info_table` enumeration from the Back-Prop/GradAccum surveys;
the POOL ExtISA `.so` was not re-located by name this pass]`

---

## 8. The BN-family roll-up — closing the family

ParamLoad is the last of **four** DVE batch-norm sub-systems. The whole family is **six DVE
handlers → five 64-byte structs** (every one `ISA_STATIC_ASSERT == 64`, compile-verified), plus
the forward affine that is **not** a DVE kernel:

| op | name | struct | role | body |
|---|---|---|---|---|
| `0x61` | `BatchNormStats2` | `S4D2_BN` | FORWARD STATS: emits 6 fp32 `{count,mean,n·var}×{even,odd}` | `0xb380` (`entry a1,96`) |
| `0x62` | `BatchNormAggregate` | `S4D2_BN` | folds `n·var` subsets → variance | (shared `0x308e`) |
| `0x64` | `BatchNormParamLoad` | `S2_BN` | **PARAM STAGE: N + batch_mean + A/B/C → DATAPATH-RAM flops** | `0xb62c` (`entry a1,48`) |
| `0x8e` | `BatchNormParamLoad2` | `S2_BNPL2` | **maintained refactor: 2-elem src + 2 ptr-imms** | `0xb694` (`entry a1,48`) |
| `0x63` | `BatchNormGradAccum` | `S3S3D1_BN` | GRAD ACCUMULATE: Σ_batch of the 3 backprop sums | `0xb518` (`entry a1,48`) |
| `0x94` | `BatchNormGradAccum2` | `S3S3D1_BN2` | v2: per-imm RegPtr + DTYPE_PAIR packing | `0xb55c` (`entry a1,64`) |
| `0x65` | `BatchNormBackProp` | `S3S3D3_TT` | the per-element `d_x` APPLY (reuses Tensor-Tensor) | `0xb6f0` (`entry a1,32`) |

`[HIGH/OBSERVED — opcode enum + struct2opcode + the `entry` widths; the per-op body addresses
re-read across this and the sibling surveys]`

**How the four ops compose for one training-backward step:**

```
 1. FORWARD  (Stats2 -> Aggregate, FW-37):  μb + n·var -> variance;
             the HOST computes  inv_std = σb-0.5 = 1/sqrt(var+eps)   (NOT on DVE)
 2. ParamLoad/2  (THIS page):  STAGE  N, μb (batch_mean), and the precomputed A/B/C affine
             coefficients (A/B/C already FOLD gamma * inv_std / N)  ->  DATAPATH-RAM flops
 3. GradAccum/2  (gradaccum):  stream saved ofmap (src0) + incoming gradient (src1),
             consuming  μb (imm0) + σb-0.5 (imm1),  accumulate the 3 batch sums
 4. BackProp     (backprop):   apply per-element d_x using the staged A/B/C/N/μb (from 2)
             and the 3 sums (from 3),  with the 1/N + gamma*inv_std prefactor
```

The `inv_std = σb-0.5` is computed **exactly once, host-side in forward**, and threaded through
as GradAccum's `imm1` **and** folded into ParamLoad's `A`/`B`/`C` — the single shared piece of
state binding forward to backward. **The DVE never recomputes it** (no `ivp_rsqrt0` anywhere).
`[engine + dispatch + the rsqrt-once verdict HIGH/OBSERVED; the apply-side algebra binding the
four ops INFERRED-HIGH from the headers + the standard BN-backward algebra]`

**The forward affine** `y = γ·(x−μ)·σ-0.5 + β` is **not** a DVE kernel — it is fused
(`scale = γ·σ-0.5`, `offset = β − γ·μ·σ-0.5`) and run on the **ACTIVATION** engine's
Activate/TensorScalar datapath. DVE produces the *inputs* (stats, gradients, staged params); it
never forms the affine. `[INFERRED-HIGH — the Aggregate header "required to generate the
batchnorm scale/offset" + the absence of a DVE apply kernel]`

**The uniform v1/v2 deprecation pattern:** every un-suffixed op is deprecated for its `2`
successor — `Stats(0x60,n)→Stats2(0x61,Y)`, `GradAccum(0x63,n)→GradAccum2(0x94,Y)`,
`ParamLoad(0x64,n)→ParamLoad2(0x8e,Y)`. The successors uniformly add **immediate-source
flexibility** and **tighter field packing**: GradAccum2 packs `(imm_dtype,out_dtype)` into one
byte + an `IMM_SRC_PAIR`; ParamLoad2 splits one src element into a second independent
pointer-immediate with its own src selector. In PERF the three deprecated grad/param/backprop
variants (`0x63`/`0x64`/`0x65`) collapse onto **one** shared handler (`0x9090`). `[HIGH/OBSERVED
markers + dispatch]`

**BN FAMILY: CLOSED.** forward stats / back-prop apply / grad accumulate / param load are all
decoded instruction/struct-exact — six DVE handlers, five 64-byte structs, the shared
μ/σ-0.5/N state flow, and the v1/v2 deprecation pattern, all pinned.

---

## 9. Confidence ledger

**HIGH / OBSERVED (disassembly, byte read, header read, or compile-verify this pass):**

* DVE carves reproduce the sibling anchors byte-identically (sha256 `259769ff`/`c106642d`/
  `9fa066f4`/`eb980f98`); `objdump` exit 0.
* Self-names `S: BatchNormalizeParamLoad` @DRAM `0x22f0`, `…ParamLoad2` @`0x230c` (full bytes
  read this pass).
* Both dispatch chains byte-exact: `0x64` → `table1[0x8a0]=0x30b6` → `call8 0x2000` (thunk
  `const16 a2,0xb62c`) → body `0xb62c` (`entry a1,48`; `const16 a10,0x22f0`; `call8 0x18010`);
  `0x8e` → `table1[0x948]=0x30be` → `call8 0x201c` (`const16 a2,0xb694`) → body `0xb694`
  (`entry a1,48`; `const16 a10,0x230c`); both converge at Handler invoke `0x3212` → `j 0x2e87`.
* The ParamLoad body's **one** `bnei a2,2`@`0xb65c` (+ inline reg-fetch `entry`@`0xb670`); the
  ParamLoad2 body's **two** `bnei a2,2`@`0xb6cd`(→`call8 0x9d48`)/@`0xb6de`(→`call8 0x9d6c`) (+
  the two reg-fetch sub-handlers `0x9d48`/`0x9d6c`, differing at byte +4). Both bodies
  `entry a1,48`.
* The staged descriptors @DRAM `0x2340` (ParamLoad, `word[0]=1`) / `0x2330` (ParamLoad2,
  `word[0]=0xb`).
* Both structs `sizeof == 64` (gcc); `S2_BN` `src@16`/`imm1@28`/`dtype@32`/`nac@34`/
  `imm0_ptr@44`/`imm0_src@48`; `S2_BNPL2` `src@12`/`reserved0@24`/`element_count@28`/
  `src_dtype@32`/`immediate_dtype@33`/`nac@34`/`imm0_ptr@40`/`imm1_ptr@44`/`imm0_src@48`/
  `imm1_src@49`. `TENSOR2D=12`/`IMM_VAL=4`/`IMM_SRC=1`. (Corrects the earlier `src@16` premise →
  truth `@12` for `S2_BNPL2`.)
* Opcode markers `0x64`=`n`/`0x8e`=`Y`; `struct2opcode` (`S2_BN` shared by 4 opcodes; `S2_BNPL2`
  1:1); `POOLING_NUM_CHANNELS = 128`; `IMM_SRC {0,1,2}`; DTYPE ordinals.
* **No** `ivp_rsqrt0`/`recip0`/`sqrt0` in DVE (grep `0` both builds, re-run this pass); the
  reciprocal-**division** ops present in PERF (`div0nxf16t ×2`, `divnn_2xf32t ×1`,
  `mulsonen_2xf32t ×1`, `mulsonenxf16t ×2`); the 104/92-B bodies are decode+stage (no MAC).
* Per-gen opcode + struct presence (`cayman`/`mariana`/`maverick`/`sunda`); cayman==sunda
  typedef-body equality.

**MED / INFERRED:**

* WHERE the params land **precisely** — the `DATAPATH_RAM` per-lane flop addressing (the
  "no DST" + the RTL map are OBSERVED; the exact flop slot layout is INFERRED-HIGH).
* The `A`/`B`/`C` = the BN-backprop affine coefficients (header naming + standard algebra +
  the Back-Prop apply HIGH; the exact `A=…`/`B=…`/`C=…` algebra INFERRED — the header cites an
  internal doc not in the binary).
* The interior FLIX decode/stage region of both bodies desyncs under stock objdump; the
  dispatch skeleton + `bnei` tests + reg-fetch calls are HIGH, the literal interleave is
  structural only.
* The forward affine on ACT; the v5 MAVERICK interior; the `0x8e` PERF dispatch row (string-pool
  overlap — DEBUG is the reliable substrate).
* The exact host/forward locus that computes `σb-0.5` (ACT-PWL rsqrt table vs host CPU) —
  external to the DVE image.

**LOW / NOT CLAIMED:**

* Per-gen byte-diff of the DVE ParamLoad handler **bodies** (only the cayman DVE carved).
* The precise register slots the reg-fetch sub-handlers read the imm pointer from.
* The precise semantics of descriptor `word[0..3]` beyond "per-op decode-spec".

---

## See also

* [BatchNormalize — Forward Statistics](batchnorm-forward.md) — Stats2/Aggregate, the
  256-reciprocal Parameter-RAM, and the *same* rsqrt-is-forward verdict.
* [BatchNormalize — GradAccum](batchnorm-gradaccum.md) — the back-prop gradient sums that
  consume `μb` and `σb-0.5` as immediates, alongside the params ParamLoad stages.
* [BatchNormalize — Back-Prop](batchnorm-backprop.md) — the per-element `d_x` apply that **reads**
  the ParamLoad-staged `N`/`batch_mean`/`A`/`B`/`C`.
* [The unified datatype model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` code space.
* [The Confidence & Walls model](../../reference/confidence-model.md) — what the tags mean.
