# Activate + the PWL Application Mechanism

This page decodes the **ACT (Activation) engine's activation-function compute family** on the
GPSIMD NeuronCore: the four firmware handlers `Activate` (opcode `0x21`),
`ActivateQuantize` (`0x22`), `ActivationTableLoad` (`0x23`) and `ActivationReadAccumulator`
(`0x24`), plus the two later-generation fused variants `ACTIVATE2` (`0x25`) and
`ACTIVATE_MULTIPASS` (`0x26`). The ACT engine is a **sibling TPB engine** to GPSIMD's POOL / PE /
DVE / SP engines — the same Cadence Tensilica **Vision-Q7 *Cairo* (`ncore2gp`)** NX-class
sequencer, distinguished only by *which* handler subset it carries and by the one piece of
silicon it drives: the **piecewise-cubic PWP activation datapath**.

The single most important fact a reimplementer must internalise is the **HW/FW split**: the
activation *function* (relu/gelu/sigmoid/tanh/exp/…) is **not firmware code**. There is no
`relu`/`gelu`/`exp` kernel anywhere in the ACT image. The function is a **host-loaded hardware
table** evaluated by a dedicated PWL silicon unit; the firmware merely **decodes the
instruction, applies the affine `scale·x+bias`, casts dtypes, programs the HW table address, and
streams tensors through it**. The HW table *engine* — the four-table CAM / PROFILE / CONTROL /
BUCKET machine and the cubic evaluation — is documented as silicon on the
[Activation + Transcendental Table Engine](../../uarch/activation-transcendental-tables.md) uarch
page (Engine B). **This page is the firmware driver of that engine**: the software path that
loads, configures and applies those tables. It references the HW table internals; it does not
re-derive them.

Everything below derives from static analysis of the shipped GPSIMD device image
(`libnrtucode_internal.so`, sha256 `b7c67e898a116454…`) read with the native Cadence
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Xtensa24 / RI-2022.9 / NX1.1.4 / 32-byte
FLIX-VLIW) plus stock binutils, and from the in-package CAYMAN..MAVERICK arch-isa interface
headers (`aws_neuron_isa_tpb_*.h`, `tpb_activation_entries.h`, `instruction_mapping.json`) and
the per-gen RTL address maps shipped in the `aws-neuronx-gpsimd-customop-lib` package. The
`extracted/` tree is gitignored — reach it with `fd --no-ignore` or absolute paths. Every claim
carries a confidence tag `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`; tags follow the project
[Confidence & Walls model](../../reference/confidence-model.md).

> **NOTE — v2–v4 are byte-grounded; v5/MAVERICK interiors are header-OBSERVED only.** The
> `0x21`–`0x24` handlers, the operand structs, the HW table format and the cayman/mariana table
> addresses are read byte-for-byte from the shipped artifacts. The `0x25`/`0x26` opcodes and
> their structs are OBSERVED in the MARIANA/MAVERICK headers, and the maverick **ACT→DVE
> hardware-region fold** is OBSERVED in the maverick `TPB_DVE.json` address map — but the *v5
> PWP-eval micro-sequence inside the DVE block* is silicon, flagged `[…/INFERRED]`. No vendor
> source was referenced; every fact reads as derived from the binary, the disassembly, the
> shipped C header, and the RTL address map.

---

## 1. Executive summary

* **The activation FUNCTION is HW, not firmware.** The DEBUG-build handler
  bodies at IRAM `0x93d8` (`Activate`) / `0x9524` (`ActivateQuantize`) / `0x954c`
  (`ActivationTableLoad`) / `0x9574` (`ActivationReadAccumulator`) are **log-and-return shims** —
  a `S: <OpName>` self-name log + three event/semaphore-field setups + `retw.n`. There is **no
  per-element activation loop and no `ivp_gather*` op anywhere in any ACT image**. The firmware
  decodes the instruction and programs the dedicated PWL silicon; the per-element math runs on
  that HW unit.

* **The application path is an affine PRE the function.** `Activate` (struct
  `S3D3_AC`, 64 B) computes, per element and per active channel:
  `OUT[k] = func_{activation_func}( scale_value · IN[k] + bias )` — a `scale·x+bias` affine
  (de-quant scale + bias-add) applied *before* the host-loaded function. The function is selected
  by a single byte `activation_func @35` = an index into the loaded PWP table set; the firmware
  never names relu/gelu/sigmoid.

* **`ActivateQuantize` fuses inline requantization.** `S3D3_AQ` (64 B) computes
  `OUT[k] = quant_scale_value · func( FP32(IN[k])·FP32(scale_value) + FP32(*bias_addr[k]) ) +
  quant_zero_value`, emitted as **UINT8** — "using the last FMA engine to do inline quantization
  instead of improving accuracy of the activation approximation" (header, verbatim). This is the
  classic `matmul→PSUM(INT32)→[dequant]→func→[requant]→SBUF(UINT8)` fold.

* **Two distinct accumulators `[HIGH/OBSERVED — CORRECTION]`.** `Activate`'s `src_mem_pattern` is
  `AllowedInPSUM::True`, so `Activate` **reads the PE PSUM directly** (the matmul accumulator,
  8 banks × 2048 fp32 — see [PE matmul](pe-matmul.md)), applies the affine+PWL, and writes SBUF.
  Separately, `ActivationReadAccumulator` (`D1_RD`, 64 B) drains the ACT engine's **own per-lane
  fp32 running accumulator** — the reduction state accumulated across an
  `Activate`/`ActivateQuantize` sequence under `accumulator_cmd` — **NOT** the PE PSUM. These are
  two different accumulators; the prior cross-page phrasing that ActivationReadAccumulator
  "evicts PSUM" is imprecise (see §6).

* **The table is DMA-staged.** `ActivationTableLoad` (`0x23`) lowers from the
  compiler pseudo-instruction `LoadActFuncSet`, which "initiates a DMA data-transfer by writing
  to the DMA tail pointer; the DMA transfer writes new PWP tables" — replaced by a `Write` by KRT.
  The four PWP tables live in dedicated HW SRAM (`TPB_n_ACT_{PROFILE_CAM, PROFILE_TABLE,
  BUCKET_TABLE, CONTROL_TABLE}` + `LOCAL_STORAGE` shadows), not in SBUF and not baked in the
  firmware image.

* **MAVERICK folds the ACT engine into DVE `[HIGH/OBSERVED region; INFERRED interior]`.** On v5
  there is **no standalone ACT engine image**; the activation PWL SRAM physically moved into the
  `TPB_DVE` block (`ACT_CONTROL_TABLE`/`PWP_CONTROL_TABLE`/`PWP_BUCKETS_TABLE`), and the
  read-accumulator is re-expressed as the DVE-native `DveReadAccumulator` (`0x9b`). The fold is a
  real hardware-region migration, not just a scheduling change.

---

## 2. The ACT opcode set + dispatch routing

### 2.1 Opcodes and operand structs

Read directly from `aws_neuron_isa_tpb_common.h` (CAYMAN lines 162–165; MAVERICK lines 172–173
for `0x25`/`0x26`) and cross-checked against `instruction_mapping.json` `struct2opcode`:

| opcode | enum | struct | handler | gens | role |
|---|---|---|---|---|---|
| `0x21` | `ACTIVATE` | `S3D3_AC` | `Activate` | v2–v5 | `func(scale·x+bias)`, 3-D, `accumulator_cmd` |
| `0x22` | `ACTIVATE_QUANTIZE` | `S3D3_AQ` | `ActivateQuantize` | v2–v5 | act + inline requantize → UINT8 |
| `0x23` | `ACTIVATION_TABLE_LOAD` | `CTRL_NO` | `ActivationTableLoad` | v2–v5 | the PWP-table DMA install (`LoadActFuncSet`) |
| `0x24` | `ACTIVATION_READ_ACCUMULATOR` | `D1_RD` | `ActivationReadAccumulator` | v2–v5 | drain the per-lane fp32 ACT accumulator |
| `0x25` | `ACTIVATE2` | `S2D2_AC` | `Activate2` | v4+ | fused act + dual-ALU + reduce, 2-D |
| `0x26` | `ACTIVATE_MULTIPASS` | `S1S2D2_AM` | *(none — dormant)* | v5 | act + prev-pass 1-D accumulator, 2-D |

`instruction_mapping.json` binds `CTRL_NO_STRUCT` to the **shared** control-no-operand family
`{ACTIVATION_TABLE_LOAD, NOP, HALT, DRAIN, INSTRUCTION_FLUSH, NOTIFY}`, and binds
`D1_RD_STRUCT` to `{ACTIVATION_READ_ACCUMULATOR, DVE_READ_ACCUMULATOR}` — the same 64-byte
read-back format the [DVE state-read page](dve-read-state.md) covers from the DVE side. The two
support ops (`Cast 0x47` / `Copy 0x46` / `TensorScalar 0x43`) the ACT engine also carries are
documented on the [Cast/Copy](cast-copy.md) and [ALU-op matrix](alu-op-matrix.md) pages.

> **NOTE — `0x25`/`0x26` are not in the CAYMAN header at all.** A `rg` of the CAYMAN
> `common.h` returns only `0x21`–`0x24`; `ACTIVATE2 = 0x25` and `ACTIVATE_MULTIPASS = 0x26`
> first appear in the MARIANA(+) / MAVERICK `common.h` (lines 172–173). `ACTIVATE2` is a v4+
> addition; `ACTIVATE_MULTIPASS` is v5-only and image-dormant (§7).

### 2.2 The DEBUG dispatch compare-chain `[HIGH/OBSERVED route; MED desynced converge]`

The four ACT opcodes are all below `0x41`, so they are **not** in the `+0x41`-indexed 178-entry
handler table band; they route through the DEBUG-build segmented compare-chain (the same
mechanism the SEQ dispatch uses). Decoded byte-exact at DEBUG IRAM `0x2b35`:

```asm
2b33: l32i.n  a2, [a1+8]            ; a2 = decoded opcode
2b35: movi.n  a3, 33 ; bne a2,a3,. ; j 0x2d82   ; 0x21 Activate
2b40: movi.n  a3, 34 ; bne a2,a3,. ; j 0x2d8a   ; 0x22 ActivateQuantize
2b4b: movi.n  a3, 35 ; bne a2,a3,. ; j 0x2d92   ; 0x23 ActivationTableLoad
2b56: movi.n  a3, 36 ; bne a2,a3,. ; j 0x2d9a   ; 0x24 ActivationReadAccumulator
2b6d: movi.n  a3, 70 ; bne a2,a3,. ; j 0x2da2   ; 0x46 Copy
```

All four arms converge (in the FLIX-desynced region `0x2d82..0x2d9a`) into the common
Handler-object invocation at `0x2dcd` that loads the registered `Handler` and calls `execute()`
through a vtable thunk — the same C++ `Handler`/vtable indirection every SEQ-style engine uses.
The handler objects are built by registration trampolines that store each `execute()` pointer and
call the register thunk `0x90c4`:

```asm
1f74: entry ; const16 a2, 0x93d8 ; s32i a2,[a1,12] ; call8 0x90c4   ; Activate
1f90: entry ; const16 a2, 0x9524 ; ...             ; call8 0x90c4   ; ActivateQuantize
1fac: entry ; const16 a2, 0x954c ; ...             ; call8 0x90c4   ; ActivationTableLoad
1fc8: entry ; const16 a2, 0x9574 ; ...             ; call8 0x90c4   ; ActivationReadAccumulator
```

The `execute()` entries self-name via baked DEBUG strings (DRAM string offset = device DRAM VA −
`0x80000`): `S: Activate` @ DRAM `0x16ae41`, `S: ActivateQuantize` @ `0x16ae70`, and the
production (PERF) and MARIANA build copies at DRAM `0x404681` / `0x6cb001`.

> **GOTCHA — the DEBUG `execute()` bodies are shims; do not mistake them for the compute.**
> `Activate`'s visible DEBUG body (`0x93d8..0x9530`) is the name-log + three near-identical
> event/semaphore field setups (`s8i a2,[a1,28]` wait/update-mode, `s8i a3,[a1,24]`) + `retw.n`.
> Only ~6 IVP ops appear in it, and they are FLIX literal-pool desync artifacts, not a real
> activation loop. This shim shape **is** the byte-level proof of HW offload: the firmware
> programs the PWL silicon and returns; the per-element evaluation is not software. `[HIGH/OBSERVED
> that the bodies are shims; HW-offload INFERRED-HIGH from the shim + the dedicated PWL HW regions §5]`

---

## 3. `Activate` (`0x21`) — the application path

### 3.1 The `S3D3_AC` operand struct (64 B)

Read field-exact from `aws_neuron_isa_tpb_s3d3_ac.h` (`ISA_STATIC_ASSERT(sizeof == 64)`). The
header comment is verbatim: *"The Activate instruction applies a scalar function on a set of
input elements (in an element-wise manner). Activate also supports integrated scale-and-add on
every input element, which is typically used to perform de-quantization and bias-add
respectively."*

| off | size | field | type | role |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `{opcode=0x21, inst_word_len, dbg}` |
| 4–11 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 1 | `imm_src` | `IMM_SRC` | source of `imm` |
| 13 | 1 | `scale_src` | `IMM_SRC` | source of `scale_value` |
| 14 | 1 | `accumulator_cmd` | `ACCUM_CMD` | ACT running-accumulator command (§6) |
| 15 | 1 | `bias_src_n` | `IMM_SRC_N` | source of `bias` (**inverted polarity**) |
| 16–31 | 16 | `src_mem_pattern` | `TENSOR3D` | INPUT 3-D strided pattern (PSUM **or** SBUF) |
| 32 | 1 | `in_bias_dtype` | `DTYPE_PAIR` | `dtype_lo`=input, `dtype_hi`=bias |
| 33 | 1 | `out_dtype` | `DTYPE` | OUTPUT element dtype (`AllowFP32R`) |
| 34 | 1 | `num_active_channels` | `u8` | partition count `1..128` |
| 35 | 1 | `activation_func` | `u8` | **THE FUNCTION SELECTOR — index into the PWP table** |
| 36–39 | 4 | `scale_value` | `ACTIVATION_IMM` (union) | fp32 imm \| imm_ptr \| imm_reg |
| 40–43 | 4 | `bias` | `ACTIVATION_IMM` (union) | fp32 imm \| imm_ptr \| imm_reg |
| 44–47 | 4 | `imm` | `ACTIVATION_IMM` (union) | a third generic immediate |
| 48–63 | 16 | `dst_mem_pattern` | `TENSOR3D` | OUTPUT 3-D strided pattern (PSUM **or** SBUF) |

The `TENSOR3D` (16 B) is `{ADDR4 start_addr ; int16 step_elem[3] ; uint16 num_elem[3]}` — the
standard 3-D strided access pattern; `start_addr` (an `ADDR4`) carries the SBUF/PSUM
partition-offset encoding. The validator IR (`is_valid_activate`) confirms **both** src and dst
are valid in PSUM AND SBUF
(`tensor3d_valid(…, AllowedInPSUM::True, AllowedInSBUF::True)`), the src/dst element counts must
match (`same_element_count_t3d`), `num_active_channels ∈ [1, ACTIVATION_NUM_CHANNELS=128]`, and
the start addresses are quadrant-checked against `num_active_channels`. `[HIGH/OBSERVED]`

### 3.2 The per-element semantics

```c
// Activate (opcode 0x21, struct NEURON_ISA_TPB_S3D3_AC_STRUCT).
// Per active channel `p` (0..num_active_channels-1), per element `k` of the strided pattern:
//   OUT[p][k] = func_{activation_func}( scale_value[p] · IN[p][k] + bias[p] )
// The PWL FUNCTION is silicon (the host-loaded CAM/PROFILE/CONTROL/BUCKET tables, §5);
// the firmware programs the table base and streams src->dst through the HW.
void activate(const NEURON_ISA_TPB_S3D3_AC_STRUCT *a) {
    // 1) resolve the affine operands per the IMM_SRC enums (§3.3).
    fp32 scale = resolve_imm(a->scale_value, a->scale_src,  /*dtype*/FP32);       // de-quant scale
    fp32 bias  = resolve_imm_n(a->bias,      a->bias_src_n, a->in_bias_dtype.dtype_hi); // bias-add (inverted polarity)

    // 2) program the HW activation datapath: the CAM is keyed on (opcode, func_id=activation_func)
    //    -> selects the PROFILE (region/symmetry/special-input/affine config),
    //       the CONTROL (the fp -> bucket-index bitfield extract), and the BUCKET run (cubic coeffs).
    stpb_act_select_func(a->activation_func);   // (§5) — drives the dedicated PWL silicon.

    // 3) stream the 3-D src pattern (PSUM or SBUF) through the HW unit, casting in_dtype->FP32 calc->out_dtype,
    //    writing the dst pattern. The ACT running accumulator is updated per accumulator_cmd (§6).
    for_each_active_channel_and_element(a->src_mem_pattern, a->dst_mem_pattern, a->num_active_channels) {
        fp32 x   = to_fp32(load(IN[p][k]), a->in_bias_dtype.dtype_lo);   // dequant input from PSUM/SBUF
        fp32 y   = scale * x + bias;                                     // the affine PRE the function
        fp32 f   = stpb_act_pwl_eval(y);                                // HW: CONTROL extract -> BUCKET cubic -> PROFILE
        store(OUT[p][k], from_fp32(f, a->out_dtype));                   // cast to out_dtype, write SBUF/PSUM
        accumulate_act(p, f, a->accumulator_cmd);                       // (§6) per-lane running accumulator
    }
}
```

The function selector byte `activation_func` is a **raw uint8 index** — there is no ISA-named
activation enum (gelu/sigmoid/silu/swish/softplus are 0 hits across the arch-isa headers; the
host KRT loads the functions as PWP table *data* via `0x23` and selects them by index). The
PWL evaluation (`stpb_act_pwl_eval`) is the silicon documented as Engine B on the
[uarch table page](../../uarch/activation-transcendental-tables.md). `[HIGH/OBSERVED struct; HW
eval CARRIED from the uarch page]`

### 3.3 dtype / bias / scale handling

The affine operands each have an independent source selector. The `IMM_SRC` enum (`common.h:1207`)
is `{INSTRUCTION_IMMEDIATE=0, POINTER_IMMEDIATE=1, REG_PTR_IMMEDIATE=2}`; `bias_src_n` uses the
**inverted-polarity** `IMM_SRC_N` (`common.h:1231`) where `POINTER_IMMEDIATE=0,
INSTRUCTION_IMMEDIATE=1, REG_PTR_IMMEDIATE=2`.

| stage | source | dtype | role |
|---|---|---|---|
| **INPUT** | `src TENSOR3D` | `in_bias_dtype.dtype_lo` (any valid minus FP32R) | typically FP32/INT32 from PSUM; cast → FP32 for the calc |
| **SCALE** | `scale_src` | FP32 (instruction imm \| per-partition fp32 pointer \| reg pointer) | per-element multiply PRE func = **de-quantization** |
| **BIAS** | `bias_src_n` (inverted) | `in_bias_dtype.dtype_hi` (bf16 / fp16 / fp32) | per-element add PRE func = **bias-add** |
| **FUNC** | `activation_func @35` | — (HW table index) | the host-loaded PWL function |
| **OUTPUT** | `dst TENSOR3D` | `out_dtype` (any valid incl. FP32R) | written to SBUF (or PSUM) |

When `scale`/`imm` are pointer-sourced (`POINTER_IMMEDIATE`), the validator requires the pointer
be **FP32 4-byte-aligned**, a valid SBUF/PSUM address, and quadrant-correct for
`num_active_channels`; when `bias` is pointer-sourced, the alignment check is against
`dtype_hi` (bf16/fp16 ⇒ 2-byte aligned, else 4-byte). The instruction-immediate path carries no
address check. `[HIGH/OBSERVED — validator IR s3d3_ac_{scale,imm,bias}_checks]`

> **QUIRK — `bias_src_n` is the only ACT operand with inverted source polarity.** `scale_src` and
> `imm_src` are `IMM_SRC` (`0=instruction`); `bias_src_n` is `IMM_SRC_N` (`0=pointer,
> 1=instruction`). A reimplementer copying the scale decode onto bias will read the wrong source.
> The `_n` suffix on the field name is the only in-struct warning.

> **NOTE — the affine can also be table-resident.** The PROFILE entry independently carries
> `bias_fuse_en`/`scale_fuse_en` + `bias_const`/`scale_const` (§5.3). So `scale·x+bias` may come
> from the *instruction* (the fields above) OR be *fused from the table*. The de-quant + bias-add
> is therefore either instruction-resident or PROFILE-resident, selected per loaded function.
> `[HIGH/OBSERVED PROFILE fields]`

---

## 4. `ActivateQuantize` (`0x22`) — the activation+quantize fusion

### 4.1 The `S3D3_AQ` struct (64 B) + the fused math

Read field-exact from `aws_neuron_isa_tpb_s3d3_aq.h`. The header gives the **exact fused math**
(verbatim):

```
OUT[k] = quant_scale_value · func( [FP32(IN[k]) · FP32(scale_value)] + FP32(*(bias_addr[k])) ) + quant_zero_value
```

| off | size | field | type | role |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `opcode=0x22` |
| 4–11 | 8 | `events` | `EVENTS` | sync |
| 12–15 | 4 | `quant_scale_value` | `float` | **output requant scale** |
| 16–31 | 16 | `src_mem_pattern` | `TENSOR3D` | INPUT (PSUM or SBUF) |
| 32 | 1 | `in_bias_dtype` | `DTYPE_PAIR` | in low / bias high |
| 33 | 1 | `out_dtype` | `DTYPE` | recommended **UINT8** |
| 34 | 1 | `num_active_channels` | `u8` | `1..128` |
| 35 | 1 | `activation_func` | `u8` | the PWL function index |
| 36–39 | 4 | `dequant_scale_value` | `float` | **input de-quant scale** (the `scale_value` role) |
| 40–43 | 4 | `bias_addr` | `PARTITION_OFFSET` | per-partition bias pointer |
| 44–47 | 4 | `quant_zero_value` | `float` | **output requant zero-point** |
| 48–63 | 16 | `dst_mem_pattern` | `TENSOR3D` | OUTPUT (PSUM or SBUF) |

```c
// ActivateQuantize (opcode 0x22, struct NEURON_ISA_TPB_S3D3_AQ_STRUCT).
// The PSUM-INT32 matmul output is de-quantized inline, activated, then re-quantized to UINT8.
void activate_quantize(const NEURON_ISA_TPB_S3D3_AQ_STRUCT *q) {
    for_each_active_channel_and_element(q->src_mem_pattern, q->dst_mem_pattern, q->num_active_channels) {
        fp32 x   = to_fp32(load(IN[p][k]), q->in_bias_dtype.dtype_lo);  // FP32 or INT32 src (from PSUM)
        fp32 b   = to_fp32(load(*q->bias_addr + p), q->in_bias_dtype.dtype_hi); // per-partition bias pointer
        fp32 a   = x * q->dequant_scale_value + b;                      // INLINE DE-QUANT + bias-add (PRE func)
        fp32 f   = stpb_act_pwl_eval_func(a, q->activation_func);       // HW PWL function
        fp32 r   = f * q->quant_scale_value + q->quant_zero_value;      // INLINE RE-QUANT (the "last FMA" stage)
        store(OUT[p][k], saturate_u8(r));                              // emit UINT8
    }
}
```

The header note is explicit: ActivateQuantize *"trades-off activation accuracy and performance,
by using the last FMA engine to do inline quantization instead of improving accuracy of the
activation approximation"* — i.e. it spends one PWL-accuracy FMA stage on the output requant
instead. The validator (`s3d3_aq_type_check`) constrains the flows to exactly two: `IN=FP32,
CALC=FP32, OUT=UINT8` and `IN=INT32 (mixed-precision matmul), inline dequant → FP32 calc →
UINT8 requant`; the bias dtype must be fp32/bf16/fp16. Both `quant_scale_value` and
`dequant_scale_value` must be **non-zero** (`s3d3_aq_scale_check`). The header note also warns
this fusion is **not supported for Parametric-ReLU**. `[HIGH/OBSERVED]`

> **CORRECTION — `ActivateQuantize`'s `dst` is also valid in PSUM.** The validator
> (`is_valid_activateq`) carries `tensor3d_valid(…dst…, AllowedInPSUM::True, AllowedInSBUF::True)`
> for *both* src and dst — the UINT8 requant output may be written to PSUM as well as SBUF, not
> SBUF-only. The recommended `out_dtype` is UINT8, but the address space is unconstrained.

> **GOTCHA — the AQ scale roles are split across two fields with opposite jobs.** `scale_value` in
> the verbatim math is the field named `dequant_scale_value @36` (input de-quant, PRE func), while
> `quant_scale_value @12` is the *output* requant scale (POST func). Two distinct `float`s, both
> "scale", at opposite ends of the pass. Do not collapse them.

---

## 5. The PWL application mechanism — the four host-loaded tables

The activation function is applied by a dedicated HW unit ("STPB act engine") through **four
host-loaded tables** whose format is read byte-exact from
`arch-headers/<gen>/tpb_activation_entries.h` (sha256 `8f6f5f49…` byte-identical
cayman/mariana/mariana_plus/maverick; `dbdca26b…` for SUNDA). **The HW table *engine* — the
cubic evaluation, the symmetry/region logic, the index-extract silicon — is documented on the
[uarch table page](../../uarch/activation-transcendental-tables.md) (Engine B).** This section
documents the *format the firmware loads and the addresses it programs*; it does not re-derive the
silicon eval.

### 5.1 The four-table quad

`sizeof` compile-verified (`gcc offsetof`, all gens): **CAM 32 B, PROFILE 128 B, CONTROL 32 B,
BUCKET 32 B**.

* **CAM** (`aws_hal_stpb_act_cam_entry_t`, 9 B used / 32 B stride): `{opcode:8, func_id:8, rsvd:16,
  opcode_mask:8, func_id_mask:8, rsvd:16, valid:1, …}`. Matches `(opcode, func_id)` → SELECTS the
  PROFILE/CONTROL/BUCKET slot. Region `0x1000` (4 KiB) / 32 B = **128 CAM entries**. The `func_id`
  key + 32 B stride are the structural separator from the per-engine `PROF_CAM` *profiler* (16 B,
  no `func_id`).
* **PROFILE** (`aws_hal_stpb_act_profile_entry_t`, 128 B, dense to bit ~754): the per-function PWL
  config — `pwl_bypass`, the region split (`{small,large}×{pos,neg}` thresholds + 20-bit per-region
  PWL control + mantissa thresholds), the **symmetry** optimisation (`symmetry_opt_en`,
  `symmetry_point`, `func_sym_inv_sign` — evaluate one half + reflect for odd/even funcs),
  `exponent_offset`, the bias/scale fuse (`bias_fuse_en`/`scale_fuse_en` + `bias_const`/
  `scale_const`), the FMA control (`fma_bypass`, `bias_scale_fma_bypass`, `fma_src_sel0..5`,
  `fma_const0/1`), and the **special-input results** `func_rslt_for_{zero,nan,pos_inf,neg_inf}`
  (the exact fp32 outputs the HW substitutes for 0/NaN/±Inf so the PWL never approximates the
  asymptotes). Region `0x2000` (8 KiB) / 128 B = **64 func slots**.
* **CONTROL** (`aws_hal_stpb_act_control_entry_t`, 4 B used / 32 B stride): the fp→bucket-index
  extract — `{act_tbl_base:11, extract_lsb:5, extract_size:4}`. The HW extracts `extract_size` bits
  starting at `extract_lsb` from the input's bit pattern, offset by `act_tbl_base`, as the bucket
  index. Region `0x1000` (4 KiB) / 32 B = **128 control entries**.
* **BUCKET** (`aws_hal_stpb_act_bucket_entry_t`, 32 B): the per-segment cubic
  `{float d0; float d1; float d2; float d3; float x0; uint32 unused[3]}`. Region `0x10000`
  (64 KiB) / 32 B = **2048 buckets** (+ a 512 KiB `LOCAL_STORAGE` shadow).

The evaluation pipeline (silicon) is: `CAM(opcode,func_id)` → func slot → `CONTROL` bitfield
extract (+ `act_tbl_base`, + `PROFILE.exponent_offset` bias) → log-spaced bucket index →
`BUCKET[idx] = {d0..d3,x0}` → cubic `d0 + d1·t + d2·t² + d3·t³` with `t=(x−x0)` →
`PROFILE` region/symmetry/special-input/affine. The **cubic** shape (`d2`/`d3` present ⇒ NOT a
linear breakpoint/slope/intercept PWL) is the key format fact — see the
[uarch page §2.2](../../uarch/activation-transcendental-tables.md) for the eval detail.
`[HIGH/OBSERVED format; cubic eval CARRIED from uarch page]`

> **GOTCHA — the CONTROL header comment says "16 bytes"; the real stride is 32.** The struct
> comment block reads "Control Table Entry / 16 bytes", but the `unused2[7]` tail makes
> `sizeof == 32`. A reimplementer walking CONTROL must use a **32-byte** stride. (The
> [uarch page](../../uarch/activation-transcendental-tables.md) flags this stale comment too.)
> Likewise PROFILE's comment says "90 bytes" but `unused2[8]` pads it to 128.

### 5.2 The HW table memory map (CAYMAN)

From `cayman-arch-regs_tgz/output/address_map/address_map_flat.yaml`, the `TPB_0` ACT regions
(replicated per `TPB_0..7`, SoC-ordered):

| region | SoC base | size | what |
|---|---|---|---|
| `TPB_0_ACT_PROFILE_CAM` | `0x2802470000` | `0x1000` | 128 CAM entries (4 KiB) |
| `TPB_0_ACT_PROFILE_TABLE` | `0x2802471000` | `0x2000` | 64 profile (PWL config) entries (8 KiB) |
| `TPB_0_ACT_BUCKET_TABLE` | `0x28024C0000` | `0x10000` | 2048 PWL buckets (64 KiB) |
| `TPB_0_ACT_CONTROL_TABLE` | `0x28024E0000` | `0x1000` | 128 control (index-extract) entries (4 KiB) |
| `TPB_0_ACT_CONTROL_TABLE_LOCAL_STORAGE` | `0x2802540000` | `0x10000` | control shadow |
| `…ACT_PROF_CAM/TABLE/BUCKETS_TABLE_LOCAL_STORAGE` | … | up to `0x80000` | the staged shadows |

These are dedicated HW SRAM regions, **not SBUF and not baked in the firmware DRAM image**. The
firmware's own staged copies (`ACT_PROF_CAM` 1 KiB, `ACT_PROF_TABLE` 8 KiB, carved from the
image) are the *default/initial* set, not the active host-supplied set. `[HIGH/OBSERVED]`

### 5.3 `ActivationTableLoad` (`0x23`) — the DMA staging

`ActivationTableLoad` decodes the shared `CTRL_NO` struct (no operand payload); the DEBUG handler
`0x954c` is a log-and-return shim. The **real** staging is described by the pseudo-instruction it
lowers from, `aws_neuron_isa_tpb_pseudo_load_act_func_set.h`
(`NEURON_ISA_TPB_PSEUDO_LOAD_ACT_FUNC_SET_STRUCT`, 64 B):

```c
typedef struct {
    NEURON_ISA_TPB_HEADER header;          // 0-3
    NEURON_ISA_TPB_EVENTS events;          // 4-11
    int8_t                act_set_name[32];// 12-43   the named activation-function SET
    uint8_t               reserved0[20];   // 44-63
} NEURON_ISA_TPB_PSEUDO_LOAD_ACT_FUNC_SET_STRUCT;
```

The header comment is verbatim: *"LoadActFuncSet initiates a DMA data-transfer, by writing to the
DMA tail pointer. The DMA transfer writes new PWP tables. LoadActFuncSet is a pseudo-instruction
generated by compiler. It is replaced by Write, by KRT."*

```c
// ActivationTableLoad (opcode 0x23) — install a new PWP table set by DMA.
// Lowered by the compiler from LoadActFuncSet, replaced by a Write by KRT.
void activation_table_load(const char act_set_name[32]) {
    // 1) host/KRT selects an activation-function SET by name (relu/gelu/sigmoid/... coefficient set).
    // 2) the compiler emits a Write that bumps the DMA tail pointer (LOCAL_REG.dma_tx_base/start_ctrl).
    // 3) a DMA engine streams the four PWP tables (CAM + PROFILE + CONTROL + BUCKET, host-supplied)
    //    into the dedicated ACT HW SRAM regions (§5.2) and their *_LOCAL_STORAGE shadows.
    dma_write_tail_pointer(/*src*/ host_pwp_table_blob(act_set_name),
                           /*dst*/ TPB_n_ACT_PROFILE_CAM /* + PROFILE/CONTROL/BUCKET */);
}
```

The compiler-supplied table CONTENT (the per-function `{d0..d3,x0}` cubics + PROFILE config words
for relu/gelu/sigmoid/tanh/exp/…) is **host-loaded per model and out-of-corpus** — the
host-content wall documented on the
[uarch page §3](../../uarch/activation-transcendental-tables.md). The firmware ships the table
MACHINE (format + HW regions), not the function COEFFICIENTS. `[HIGH/OBSERVED format + map; the
DMA-stream detail OBSERVED from the pseudo-inst header]`

---

## 6. `ActivationReadAccumulator` (`0x24`) — the accumulator drain

### 6.1 The two accumulators `[HIGH/OBSERVED — CORRECTION]`

There are **two distinct accumulators** in the activation data flow, and conflating them is the
most common error:

1. **The PE PSUM** — the matmul partial-sum banks (`TPB_n_PSUM_BUF`, 8 banks × 2048 fp32; see
   [PE matmul §4.2](pe-matmul.md)). `Activate`/`ActivateQuantize` **read** PSUM directly via their
   `src_mem_pattern` (`AllowedInPSUM::True`); `Activate`'s `accumulator_cmd=LOAD_ACCUMULATE` can
   also *seed* the ACT accumulator from PSUM.
2. **The ACT engine's per-lane running accumulator** — a single fp32 scalar **per active
   lane/partition**, accumulated across an `Activate`/`ActivateQuantize`/`TensorScalar` sequence
   under `accumulator_cmd`. This is the ACT engine's reduction output (e.g. a per-row sum used as a
   softmax denominator). **`ActivationReadAccumulator` drains THIS one** — not the PE PSUM.

> **CORRECTION vs the cross-page phrasing.** The PE-matmul page notes *"the ACT engine later
> evicts PSUM via ActivationReadAccumulator (op 0x24)"*. Precisely: `ActivationReadAccumulator`
> drains the **ACT per-lane running accumulator** (the `D1_RD` 1-element-per-lane read); the **PE
> PSUM is drained by `Activate`'s `src` TENSOR3D** (`AllowedInPSUM`), which evicts PSUM, applies
> affine+PWL, and writes SBUF. Both accumulators participate, but `0x24` reads the ACT
> accumulator, not the matmul PSUM. `[HIGH/OBSERVED from the D1_RD validator + the S3D3_AC src
> AllowedInPSUM flag]`

The `ACCUM_CMD` enum (`common.h:777`) that controls the ACT running accumulator:

| value | name | effect |
|---|---|---|
| 0 | `IDLE` | no accumulator update |
| 1 | `ZERO` | zero the accumulator |
| 2 | `ACCUMULATE` | add the per-element result to the running accumulator |
| 3 | `ZERO_ACCUMULATE` | zero then accumulate |
| 4 | `LOAD_ACCUMULATE` | seed from memory (PSUM) then accumulate |

### 6.2 The `D1_RD` struct (64 B)

Read field-exact from `aws_neuron_isa_tpb_d1_rd.h` (shared by `ActivationReadAccumulator` and
`DveReadAccumulator`):

| off | size | field | type | role |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `opcode=0x24` |
| 4–11 | 8 | `events` | `EVENTS` | sync |
| 12–27 | 16 | `reserved0[16]` | — | must be 0 |
| 28–31 | 4 | `dst_element_count` | `u32` | **fixed == 1** |
| 32 | 1 | `dtype` | `DTYPE` | output dtype |
| 33 | 1 | `negated` | `u8` | optional negate (see CORRECTION) |
| 34 | 1 | `num_active_channels` | `u8` | `1..128` |
| 35–43 | 9 | `reserved1[9]` | — | must be 0 |
| 44–51 | 8 | `dst_mem_pattern` | `TENSOR1D` | 1-D dst, `num_elem[0] == 1` |
| 52–63 | 12 | `reserved2[12]` | — | must be 0 |

```c
// ActivationReadAccumulator (opcode 0x24, struct NEURON_ISA_TPB_D1_RD_STRUCT).
// "Read the fp32 value of the accumulator in each lane to a 1-D dst tensor with a single element."
void activation_read_accumulator(const NEURON_ISA_TPB_D1_RD_STRUCT *r) {
    // fixed-form: dst_element_count == 1 && dst_mem_pattern.num_elem[0] == 1.
    for (int p = 0; p < r->num_active_channels; p++) {        // one fp32 per active lane
        fp32 acc = act_running_accumulator[p];                // the ACT per-lane reduction scalar
        store_1d(r->dst_mem_pattern, p, from_fp32(acc, r->dtype));  // cast to dtype (FP16/BF16/FP32/FP32R)
    }
}
```

The header doc-comment lists the legal output dtypes for `ActivationReadAccumulator` as
**FP16, BFLOAT16, FP32**; the validator (`activation_read_accumulator_type_check`) additionally
admits **FP32R** (`is_valid_dtype(…, DtypeAllowFP32R::True)`). `num_active_channels` is validated
against `POOLING_NUM_CHANNELS` (128). The dst is `AllowedInPSUM::True` + `AllowedInSBUF::True` —
the per-lane accumulator may be written to either. `[HIGH/OBSERVED]`

> **CORRECTION — the `negated` flag belongs to `DveReadAccumulator`, NOT
> `ActivationReadAccumulator`.** Both ops share the `D1_RD` format, but the validators differ:
> `ActivationReadAccumulator` requires `negated == 0` (`d1_rd_has_zero_negated_field`), while
> `DveReadAccumulator` allows `negated ∈ {0,1}` (`d1_rd_has_valid_negated_field`). The optional
> output-negate is a DVE feature; on the ACT read-accumulator the byte must be zero. (The
> [DVE state-read page](dve-read-state.md) owns the negate-enabled side.) `[HIGH/OBSERVED — the
> D1_RD validator IR]`

---

## 7. The fused variants — `ACTIVATE2` (`0x25`) and `ACTIVATE_MULTIPASS` (`0x26`)

### 7.1 `ACTIVATE2` (`0x25`, `S2D2_AC`, 64 B) `[HIGH/OBSERVED struct; MED/INFERRED ordering]`

A v4+ fused activation that folds a dual TensorScalar ALU pair, the PWL function, and an
integrated reduction into one 2-D pass. The struct (read from
`aws_neuron_isa_tpb_s2d2_ac.h`, "ISA header for NC-v5") adds, beyond a base activate: `op0`/`op1`
(@29/@30, `ALU_OP`), `reduce_cmd` (@26, `REDUCE_CMD`) + `reduce_op` (@31, `ALU_OP`), three
immediates `imm0`/`imm1`/`relu_param` (@36/@40/@44) with their `*_src` selectors, and
`activation_func @35`. `op0`/`op1`/`reduce_op` draw from the general `ALU_OP` table (`BYPASS=0x00,
ADD=0x04, SUBTRACT=0x05, MULT=0x06, MAX=0x08, MIN=0x09, RE_LU=0x22, SQUARE=0x23,
SYMMETRIC_CLAMP=0x24`).

```c
// ACTIVATE2 (opcode 0x25, struct S2D2_AC) — the fused dual-ALU + PWL + reduce 2-D pass.
// STAGE A: dual TensorScalar affine/elementwise   (op0=MULT,imm0=scale ; op1=ADD,imm1=bias => scale·x+bias)
// STAGE B: the PWL function (activation_func index; relu_param = parametric-ReLU negative slope, RTL imm12)
// STAGE C: integrated reduction into the ACT per-lane accumulator (reduce_cmd/reduce_op; drained by 0x24)
void activate2(const NEURON_ISA_TPB_S2D2_AC_STRUCT *a) {
    for_each_active_channel_and_element(a->src_mem_pattern, a->dst_mem_pattern, a->num_active_channels) {
        fp32 s = alu(load_fp32(IN[p][k]), imm0(a), a->op0);   // Stage A.1 (e.g. MULT = scale)
        fp32 t = alu(s, imm1(a), a->op1);                     // Stage A.2 (e.g. ADD  = bias)
        fp32 f = stpb_act_pwl_eval_func(t, a->activation_func, /*relu_slope*/ a->relu_param);  // Stage B
        store(OUT[p][k], from_fp32(f, a->out_dtype));
        reduce_act(p, f, a->reduce_op, a->reduce_cmd);        // Stage C
    }
}
```

The `REDUCE_CMD` enum (`common.h:785`) is `{IDLE=0, RESET=1, REDUCE=2, RESET_REDUCE=3}` — `RESET`
zeroes the accumulator first, `RESET_REDUCE` zeroes-then-accumulates. The validator
(`is_valid_activate2`) validates `num_active_channels` against `DVE_NUM_CHANNELS` (sizing the
family to the DVE channel grid even at v4 — consistent with the v5 fold), `in_dtype` is non-FP32R,
`out_dtype` is FP32R-allowed. The **stage ordering (affine → PWL → reduce)** is the
contract-consistent reading of the field groups (base-Activate is affine-PRE-func; the ACT
accumulator is a POST-func reduction) but is **not device-body byte-pinned** — the FLIX desync
prevents pinning. `[HIGH/OBSERVED struct/IR; MED/INFERRED ordering]`

> **NOTE — `relu_param` is the parametric-ReLU negative slope, instruction-resident.** The
> `relu_param_src @24` field comment is "byte 24-27 → RTL imm12"; for a parametric/leaky-ReLU
> function the negative-side slope rides this immediate, not the PWL table. ActivateQuantize
> explicitly does **not** support parametric-ReLU (§4).

### 7.2 `ACTIVATE_MULTIPASS` (`0x26`, `S1S2D2_AM`, 64 B) — image-dormant

The v5-only multipass variant. It is the same fusion as `0x25` (op0/op1 + reduce_cmd/reduce_op)
but **drops the third immediate (`relu_param`)** and **adds `prev_pass_mem_pattern` (TENSOR1D
@40)** — a READ tensor of `out_dtype`, `start_addr` aligned to `num_active_channels`. The
validator constrains **src AND dst AND prev_pass all SBUF-only** (`AllowedInPSUM::False`, no PSUM
anywhere), distinguishing it from the single-shot PSUM-drain of `0x21`/`0x25`.

The op is **fully image-dormant** — a firmware-wide string search returns:

* `"Multipass"` (case-insensitive): **0 occurrences**.
* `"Activate2"`: exactly 2, both `RS: Activate2` at byte offsets `0x40509e` and `0x6cba1e` — both
  in the MARIANA(_plus) ACT image band (below the maverick blob range), confirming the named
  `Activate2` handler is a MARIANA(+) artifact (`S: Activate2` entry @ MARIANA IRAM `0xc0bd`).

So `0x26` is spec-present (the maverick enum line 173 + the `S1S2D2_AM` struct + the validator)
but has **no firmware handler, no self-name string, and is emitted by no shipped kernel**. The
all-SBUF constraint + the dormancy read as a compiler-emitted-but-unused (or RTL-only)
large-tensor streaming primitive: a tensor too large for one PSUM-drained pass is activated in
multiple passes, each reading the prior pass's 1-D accumulator (`prev_pass`) and folding it via
`reduce_cmd`. `[HIGH/OBSERVED struct + string counts; "compiler-but-unused" reading MED/INFERRED]`

---

## 8. The SIMD datapath — why there is no software LUT-gather

The carved ACT NX core carries a full Vision-Q7 IVP vector datapath (the PERF build shows 632 IVP
op instances, 299 distinct). The decisive structural fact for the HW-offload conclusion:

* **There is NO `ivp_gather*` op anywhere in any ACT image** (DEBUG or PERF) — there is no
  vector-LUT-gather loop in the firmware. A software activation would gather coefficients from an
  SBUF-resident table; the ACT firmware never does. This is the evidence the LUT-application is HW
  (the dedicated PWL datapath of §5), not a software vector gather.
* **The fp-decompose / index-extract primitives ARE present** — `ivp_addexpmnxf16t` (add
  exponent+mantissa), `ivp_fitruncnxf16t` (float→int truncate), plus the int↔fp converts
  (`ivp_float16nx16t`, `ivp_trunc16nxf16`, `ivp_utruncn_2xf32t`, …). These are exactly what the
  firmware needs to *set up / corner-case* the HW bucket index (the CONTROL bitfield extract is a
  fp exponent/mantissa slice), not to evaluate the function.
* **One real activation FLIX bundle** (PERF `0xf4a4`, inside a `loopgtz` vector loop @ `0xf492`)
  carries the signature `{ store ; load ; predicate ; ivp_addexpmnxf16t (fp decompose) ;
  ivp_bmaxnx16 (max = ReLU/clamp) }` with a nearby fp32 literal — load → fp-decompose/affine →
  max-clamp → store, the per-element streaming the firmware drives around the HW unit.

So the firmware's vector engine does the AFFINE `scale·x+bias`, the fp/int dtype CASTS
(`in_dtype → FP32 calc → out_dtype`), the index-EXTRACT setup, the lane-select/permute for the
strided `TENSOR3D` address-gen, and the strided LOAD/STORE; **the PWL function evaluation itself
is the HW STPB datapath**. The "SIMD LUT-gather" does not exist as a software op — it is replaced
by the HW PWL unit. `[HIGH/OBSERVED census + the no-gather negative]`

---

## 9. The MAVERICK ACT→DVE fold `[HIGH/OBSERVED region; INFERRED interior]`

On CAYMAN/MARIANA (v3/v4) the four PWL tables are an **ACT-engine block** (`TPB_n_ACT_{PROFILE_CAM,
PROFILE_TABLE, BUCKET_TABLE, CONTROL_TABLE}` + `LOCAL_STORAGE` shadows, §5.2), with a standalone NX
ACT sequencer image and named ACT handlers. On **MAVERICK (v5) there is no standalone ACT engine
image**; the activation PWL SRAM physically **moved into the `TPB_DVE` block**. From
`maverick/vpc-mirror/arch-regs/src/address_map/TPB_DVE.json`:

| block | AddressOffset | size | note |
|---|---|---|---|
| `DVE_PROFILE_CAM` | `0x8D000` | `0x1000` | the DVE HW-decode profiler (every engine has this) |
| `DVE_PROFILE_TABLE` | `0x8E000` | `0x2000` | (the profiler — NOT the activation PWL) |
| `ACT_CONTROL_TABLE` | `0xA0000` | `0x2000` | the activation control region, now in DVE |
| `PWP_CONTROL_TABLE` | `0xB0000` | `0x10000` (64 KiB) | the relocated CONTROL pool |
| `PWP_BUCKETS_TABLE` | `0xC0000` | `0x80000` (512 KiB) | the relocated BUCKET cubic pool |

So the v5 fold is a **real hardware-region migration**, not just a scheduling change: the DVE
engine HOSTS the activation PWL datapath itself, with the bucket/control SRAM renamed to the
`PWP_*` family (PWP = Piece-Wise Polynomial). The PWP table FORMAT is **byte-identical** across the
fold (`tpb_activation_entries.h` sha `8f6f5f49…` cayman..maverick). On the scheduling side, the
maverick DVE `PROF_CAM` arms `0x23 ACTIVATION_TABLE_LOAD` + `0x25 ACTIVATE2` (which the mariana
DVE PROF did not), and the read-accumulator is re-expressed as the DVE-native
`DveReadAccumulator` (`0x9b`).

> **NOTE — the v5 PWP-eval *interior* is INFERRED; only the region migration is OBSERVED.** The
> maverick `TPB_DVE.json` memory blocks (the `ACT_CONTROL`/`PWP_CONTROL`/`PWP_BUCKETS` presence
> inside DVE) and the format identity are OBSERVED. That the DVE lane *re-uses the same relocated
> PWP machine* — rather than re-implementing the cubic eval differently — is INFERRED-HIGH from the
> identical `PWP_*` naming + the byte-identical header format; the exact v5 PWP eval micro-sequence
> is silicon. The maverick coarse map names no separate `PWP_PROFILE` region (the 128 B per-func
> config either folds into the 8 KiB `ACT_CONTROL_TABLE`, or the map is coarser than cayman's
> four-way split) — region presence OBSERVED, placement INFERRED. `[HIGH/OBSERVED region;
> INFERRED interior]`

---

## 10. Per-gen evolution

| axis | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | MARIANA+ | MAVERICK (v5) |
|---|---|---|---|---|---|
| `0x21`–`0x24` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `0x25 ACTIVATE2` | — | — | ✓ (`S2D2_AC`) | ✓ | ✓ |
| `0x26 ACTIVATE_MULTIPASS` | — | — | — | — | spec-only (dormant) |
| table sha256 | `dbdca26b…` | `8f6f5f49…` | `8f6f5f49…` | `8f6f5f49…` | `8f6f5f49…` |
| PWL home | ACT engine | ACT engine | ACT engine | ACT engine | **DVE block** (`PWP_*`) |
| named ACT handlers | ✓ | ✓ | ✓ (+`Activate2`) | ✓ | none (folded to DVE) |

The SUNDA→CAYMAN PWP-format step is a **minor config-bit growth** — CAYMAN+ adds
`bias_scale_fma_bypass:1` (@435) and `batchnorm_accumulator_rd:1` (@754), and swaps the CAM
`opcode_mask`/`func_id_mask` byte order — over the **same** 32/128/32/32 B sizes; the core PWL
machine (CONTROL extract + BUCKET cubic + region/symmetry config) is unchanged v2→v5. The
activation datapath HOME is the only structural change: **ACT-engine v2–v4 → DVE-block v5**.
`[HIGH/OBSERVED — sha + compile-verify both gens + the maverick map]`

---

## 11. Adversarial self-verification — the strongest claims, re-challenged

Each headline claim is re-tested against the binary + the shipped header; a claim survives
only if a second independent witness agrees.

1. **The PWL is a four-table CAM/PROFILE/CONTROL/BUCKET quad, BUCKET a 32 B cubic.**
   *Challenge:* could `d2`/`d3` be padding, or the strides drift? *Re-test:* `tpb_activation_entries.h`
   reads `float d0,d1,d2,d3,x0` at offsets 0/4/8/12/16 with a 3-word tail; `sizeof` =
   32/128/32/32 compile-verified; sha `8f6f5f49…` cayman==maverick, `dbdca26b…` sunda. A linear PWL
   would carry `{d0,d1,x0}` only. **Survives.**

2. **The application path is `OUT=func(scale·IN+bias)`.** *Challenge:* is the affine PRE or POST
   the function? *Re-test:* the `s3d3_ac.h` header comment ("integrated scale-and-add … de-quantization
   and bias-add") + the verbatim AQ math `func(FP32(IN)·scale + bias)` both put the affine strictly
   PRE `func`. **Survives.**

3. **`ActivationReadAccumulator` drains the ACT per-lane accumulator, not the PE PSUM.**
   *Challenge:* the PE-matmul page says it "evicts PSUM"? *Re-test:* the `D1_RD` header reads "the
   fp32 value of the accumulator in each lane to a 1-D dst with a single element"; the PSUM eviction
   is `Activate`'s `src AllowedInPSUM::True`. Two different accumulators — the `0x24` op reads the
   reduction accumulator. **Survives, with a CORRECTION logged (§6).**

4. **`ActivateQuantize` fuses dequant·x+bias → func → requant → UINT8.** *Challenge:* could the
   quant be a separate op? *Re-test:* the `s3d3_aq.h` header gives the single-line fused math
   verbatim + "the last FMA engine to do inline quantization"; the validator forces `out_dtype ==
   UINT8`. **Survives.**

5. **The MAVERICK fold is a real HW-region migration.** *Challenge:* is it only a scheduling fold?
   *Re-test:* the maverick `TPB_DVE.json` carries `ACT_CONTROL_TABLE`/`PWP_CONTROL_TABLE`/
   `PWP_BUCKETS_TABLE` *inside* the DVE block; there is no `ACT_*` engine block on maverick. The
   SRAM moved. **Survives** (region OBSERVED; the v5 eval interior flagged INFERRED). `[HIGH/OBSERVED
   region]`

No claim here rests on a raw dump or a single uncorroborated witness: every struct fact is
compile-verifiable against the shipped header, and every address is in the shipped RTL map.

---

## 12. Confidence ledger

**HIGH / OBSERVED:** the opcodes `0x21`–`0x24` + structs + `struct2opcode` bindings; the DEBUG
dispatch compare-chain (`movi a3,33/34/35/36 ; bne ; j 0x2d82/8a/92/9a`) and the handler
registration trampolines / `execute()` entries / `S:` self-name logs (the DEBUG bodies are
shims); the `S3D3_AC` / `S3D3_AQ` / `D1_RD` / `PSEUDO_LOAD_ACT_FUNC_SET` full field layouts +
verbatim header math; the `ACCUM_CMD`/`REDUCE_CMD`/`IMM_SRC`/`IMM_SRC_N` enums; the four-table
PWP format (CAM 32 / PROFILE 128 / CONTROL 32 / BUCKET 32 `{d0..d3,x0}`) compile-verified; the
CAYMAN ACT table memory map; the firmware string counts (`Multipass`=0, `Activate2`=2× `RS:` at
`0x40509e`/`0x6cba1e`); the maverick `TPB_DVE.json` `ACT_CONTROL`/`PWP_CONTROL`/`PWP_BUCKETS` fold;
the absence of `ivp_gather*`; the `0x25`/`0x26` structs (header-OBSERVED).

**MED / INFERRED:** the HW-offload of the function eval (INFERRED-HIGH from the shim bodies + the
dedicated PWL regions + the no-gather negative); the `ACTIVATE2` stage ordering
(affine→PWL→reduce, not byte-pinned); the PSUM↔ACT-accumulator interplay
(`LOAD_ACCUMULATE` seeding); the `0x26` multipass streaming-loop semantics; the v5 DVE-block PWP
eval micro-sequence (region OBSERVED, interior INFERRED).

**CARRIED:** the HW table *engine* internals — the cubic eval, the symmetry/region silicon, the
CONTROL-extract HW split — documented on the
[uarch table page](../../uarch/activation-transcendental-tables.md) (the FW-42 wall on the refine
interior; the host-content wall on the PWP coefficients).

**LOW / NOT CLAIMED:** the host-supplied per-function PWP table CONTENT (relu/gelu/sigmoid/… cubic
coefficients + PROFILE config words) — out-of-corpus, the host-content wall; the per-bit silicon
behaviour of every PROFILE field; the per-function bucket count (shared pool, host-set via
`act_tbl_base`); whether any non-shipped maverick config emits `0x26` (none observed).

---

## Cross-references

* [Activation + Transcendental Table Engine](../../uarch/activation-transcendental-tables.md) — the
  HW table *engine* this kernel drives: the four-table CAM/PROFILE/CONTROL/BUCKET machine, the
  cubic eval, the index-extract silicon, the symmetry/region logic, and the host-content wall. This
  page is the firmware driver of that engine.
* [PE Matrix-Multiply Path (LdWeight/Matmul/ManageSeed)](pe-matmul.md) — the PE systolic array and
  the **PSUM accumulator** (8 banks × 2048 fp32) that `Activate` drains via its `src` TENSOR3D.
* [DVE State Read-Back (DveReadAccumulator/Indices)](dve-read-state.md) — the sibling `D1_RD`
  read-back op (`0x9b`) that shares the format and owns the negate-enabled accumulator drain.
* [The Unified Datatype Model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum (including FP32R)
  the affine, the cast, and the requant flow through.
* [Cast and Copy](cast-copy.md) / [The ALU-Op Datapath + Dtype Matrix](alu-op-matrix.md) — the
  support ops (`0x46`/`0x47`/`0x43`) the ACT engine also carries.
