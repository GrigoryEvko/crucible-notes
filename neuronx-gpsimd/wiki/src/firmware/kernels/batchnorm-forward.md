# BatchNormalize — Forward Statistics (`BatchNormStats2` / `Stats` / `Aggregate`)

This page decodes the **forward** half of the DVE (Data/Vector Engine, `engine_idx=3`)
**BatchNormalize** kernel family on the GPSIMD NeuronCore: the **statistics** path that
produces the per-channel `mean` and `variance` a batch-norm layer needs. It is the kernel
the device firmware itself **self-names** `S: BatchNormalize` — and the first thing this page
establishes is that this name is *not* the textbook forward affine
`y = γ·(x−μ)/√(σ²+ε) + β`. On the Neuron DVE that affine is **not a kernel at all**; the
"BatchNormalize" handler computes `mean` and `n·variance` and writes six fp32 scalars, and
the actual `scale·x + offset` apply is fused into an ACTIVATION-engine affine elsewhere. This
page proves that split byte-for-byte, decodes the `S4D2_BN` operand struct field-exact, pins
the 256-entry reciprocal **Parameter-RAM** that turns the divide-by-count into a single
multiply, and resolves the central reverse-engineering question — *where does the
`1/√(var+ε)` come from?* — to a hard, header-verbatim answer: **not on DVE**.

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD compute core's** own
firmware — windowed-ABI Xtensa code in the `ncore2gp` (Xtensa24, RI-2022.9, NX1.1.4, 32-byte
FLIX/VLIW) configuration — plus its NX/SEQ sequencer dispatch. Every device fact below is
byte-pinned to a carve re-derived **this pass** from `libnrtucode_internal.so` with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); every host-ISA fact is read out of the
`aws_neuron_isa_tpb_*.h` arch-isa headers shipped in the same customop-lib package; the seed
*value semantics* are proven by executing the in-package `libfiss-base.so` ISS value oracle.
The `extracted/` tree is gitignored — reach it with `fd --no-ignore` or absolute paths.
Confidence and evidence tags follow the project
[Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-execution, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]`
= re-used at a cited sibling report's confidence without re-reading the artifact this pass.

> **NOTE — what was carved this pass, and the exact objects used.** The firmware container is
> `…/custom_op/c10/lib/libnrtucode_internal.so` (`10,276,288 B`, sha256
> `b7c67e898a116454…`). The DVE images are `.rodata`-resident (so **file offset == device
> VA** for `.text`/`.rodata`; no `.data` delta applies to these carves). Carved with `dd`,
> disassembled with the native `xtensa-elf-objdump` (GNU Binutils 2.34.20200201 / Xtensa
> Tools 14.09, sha256 `d43bd4fa…`) shipped inside the gpsimd-tools package. IRAM offsets ==
> device IRAM VA (reset vector at byte 0); DRAM string offset == device DRAM VA − `0x80000`.
>
> | carved object | file off / size | sha256 (first 8) | role |
> |---|---|---|---|
> | `DVE_DEBUG_IRAM` | `0x16f660` / `0x1bcc0` | `259769ff` | the handler bodies + self-name logs |
> | `DVE_DEBUG_DRAM` | `0x18b320` / `0x6d60` | `c106642d` | the `S:` strings + dispatch table |
> | `DVE_PERF_IRAM` | `0x6f600` / `0x15c20` | `9fa066f4` | the production build (Newton `div`/`mulsone`) |
> | `DVE_PERF_DRAM` | `0x85220` / `0x2fc0` | `eb980f98` | the production dispatch table |
>
> Reset vector (DEBUG IRAM byte 0): `06 76 00 00` = `j 0x1dc` (the SEQ boot path,
> byte-identical across all five engines); secondary `86 77 00 00` = `j 0x1e8`. `objdump`
> exit 0, empty stderr; `DVE_DEBUG_IRAM.dis` = 44,989 lines, `DVE_PERF_IRAM.dis` = 35,342
> lines. The four sha256 reproduce the SX-IMG-04 anchor exactly. `[HIGH/OBSERVED]`

> **CORRECTION — this page corrects two task premises.** (1) "BatchNormalize forward apply"
> — there is **no** `γ·x+β` apply kernel on DVE; `S: BatchNormalize` is the **statistics**
> kernel (`BatchNormStats2`). (2) "rsqrt(var+eps) via `ivp_rsqrt0` + Newton on DVE" — there
> is **no `ivp_rsqrt0` anywhere in the DVE firmware** (grep count `0` in both DEBUG and
> PERF). `1/√(var+ε)` is **forward/host-precomputed** and handed to the back-prop kernels as
> a pointer-immediate. The only seed→Newton the DVE batch-norm path runs is **reciprocal
> division** for `sum ÷ count`. Both corrections are byte-grounded below. `[HIGH/OBSERVED]`

---

## 0. TL;DR — the forward-statistics model in eight facts

1. **`S: BatchNormalize` = `BatchNormStats2` (opcode `0x61`, struct `S4D2_BN`).** It streams
   ≤ 512 source elements and writes **six fp32 scalars**:
   `{even_count, even_mean, even_nvar, odd_count, odd_mean, odd_nvar}`. It is **not** the
   forward affine. `[HIGH/OBSERVED — S4D2_BN header + handler @0xb380]`
2. **`n·var`, not `var`.** Stats2 emits population-variance **× element-count** (`even_nvar`,
   `odd_nvar`), deliberately deferring the divide-by-N so a downstream `BatchNormAggregate`
   (`0x62`) can numerically fold partial streams. `[HIGH/OBSERVED header]`
3. **The mean's divide is a *multiply* against a 256-entry reciprocal Parameter-RAM.** The
   header mandates the DVE Parameter RAM be preloaded with `1, 1/2, 1/3, … 1/255` in fp32;
   `mean = sum × recip_RAM[N]`. The RAM is a dedicated HW bank,
   `TPB_0_DVE_BANK_PARAMETER_RAM @ 0x2802B87000`, size `0x400` = exactly 256 × fp32.
   `[HIGH/OBSERVED — header text + RTL address map; the "sum×recip" realisation MED/INFERRED]`
4. **No `rsqrt` on DVE.** `1/√(var+ε)` (`σb-0.5`) is the **immediate `imm1`** of
   `BatchNormGradAccumulate`, header-verbatim *"saved from forward propagation"*, supplied as
   a pointer-immediate. `grep` finds zero `ivp_rsqrt0`/`ivp_sqrt0`/`ivp_recip0` in either
   build. `[HIGH/OBSERVED]`
5. **The DVE's only seed→Newton is reciprocal *division*.** The PERF IRAM issues
   `ivp_div0nxf16t` (fp16 reciprocal SEED), `ivp_divnn_2xf32t` (fp32 Newton refine), and
   `ivp_mulsonenxf16t`/`ivp_mulsonen_2xf32t` (the fused `(1−d·x)` helper) — the machinery for
   `sum ÷ count` when no reciprocal-table entry applies. `[HIGH/OBSERVED — 6 real FLIX
   bundles in PERF]`
6. **fp32 compute hub.** Every input dtype (fp16/bf16/fp32, or i32 on the grad srcs) is
   **converted to fp32 before computation**; the accumulator and all stats math are fp32;
   output is one of `{fp32, bf16, fp16}`. `[HIGH/OBSERVED — S4D2_BN/S3S3D1_BN validity
   predicates]`
7. **Even/odd lane split.** `even` = `in[0],in[2],in[4],…`; `odd` = `in[1],in[3],…`. The
   split exists purely so `Aggregate` can recombine the two partial sums with no precision
   loss. `[HIGH/OBSERVED header worked-example]`
8. **The forward affine lives on ACTIVATION, not DVE.** `scale = γ·rsqrt(var+ε)`,
   `offset = β − γ·μ·rsqrt(var+ε)`, applied as one Activate/TensorScalar
   `OUT = func(scale·IN + bias)`. DVE produces the *inputs* to that fusion; it never applies
   it. `[INFERRED-HIGH — Aggregate header ("required to generate the batchnorm scale/offset")
   + the absence of a DVE apply kernel]`

---

## 1. The six handlers, five structs, one dispatch family

The DVE carries **six** batch-norm handlers (plus `LOAD_PARAMETER_RAM`) that the DEBUG build
`S:`-names. They bind to **five** distinct 64-byte arch-isa operand structs (every one
`ISA_STATIC_ASSERT(sizeof == 64)`), read from `aws_neuron_isa_tpb_common.h` (opcode enum) and
`instruction_mapping.json` (`struct2opcode`):

| opcode | name | struct | status | `S:` self-name (DRAM off) |
|---|---|---|---|---|
| `0x60` | `BATCH_NORM_STATS` | `S4D2_BN` | `n` (use Stats2) | — (shares family) |
| `0x61` | `BATCH_NORM_STATS2` | `S4D2_BN` | **Y** | `0x2261` `S: BatchNormalize` |
| `0x62` | `BATCH_NORM_AGGREGATE` | `S4D2_BN` | **Y** | — (shares family) |
| `0x63` | `BATCH_NORM_GRAD_ACCUMULATE` | `S3S3D1_BN` | `n` (use v2) | `0x22a0` `…GradAccum` |
| `0x64` | `BATCH_NORM_PARAM_LOAD` | `S2_BN` | `n` (use v2) | `0x22f0` `…ParamLoad` |
| `0x65` | `BATCH_NORM_BACK_PROP` | `S3S3D3_TT` | **Y** | `0x2350` `…BackProp` |
| `0x66` | `LOAD_PARAMETER_RAM` | — (loads the recip RAM) | **Y** | — |
| `0x82` | `TRANSPOSE_BATCH_NORM_STATS2` | `S4D2_BN` | **Y** | — (shares family) |
| `0x8e` | `BATCH_NORM_PARAM_LOAD2` | `S2_BNPL2` | **Y** | `0x230c` `…ParamLoad2` |
| `0x94` | `BATCH_NORM_GRAD_ACCUMULATE2` | `S3S3D1_BN2` | **Y** | `0x22bc` `…GradAccum2` |

`[HIGH/OBSERVED — opcode enum, struct2opcode, and all six S: strings re-read this pass at DRAM
0x2261/0x22a0/0x22bc/0x22f0/0x230c/0x2350 via strings -t x DVE_DEBUG_DRAM.bin]`

> **NOTE — the `S:` name is the *family* name, not the opcode name.** The firmware logs
> `S: BatchNormalize` for the **whole** `S4D2_BN` family entry; the arch-isa opcode behind it
> is `BATCH_NORM_STATS2`. `Stats`, `Aggregate`, and `TransposeStats2` share the *same* struct
> and the same decode trampoline, branching internally on the opcode byte — the "one struct,
> many opcodes" pattern the rest of the DVE ISA uses. `[HIGH/OBSERVED]`

**`struct2opcode` (the shared-wire-format truth, `jq` from `instruction_mapping.json`):**

```
S4D2_BN_STRUCT    -> { BATCH_NORM_STATS2, TRANSPOSE_BATCH_NORM_STATS2, BATCH_NORM_STATS,
                       BATCH_NORM_AGGREGATE, MAX8, FIND_INDEX8 }
S3S3D1_BN_STRUCT  -> { BATCH_NORM_GRAD_ACCUMULATE }
S3S3D1_BN2_STRUCT -> { BATCH_NORM_GRAD_ACCUMULATE2 }
S2_BN_STRUCT      -> { BATCH_NORM_PARAM_LOAD, MATCH_VALUE_LOAD,
                       TENSOR_SCALAR_IMM_LD_ARITH, TENSOR_SCALAR_IMM_LD_BITVEC }
S2_BNPL2_STRUCT   -> { BATCH_NORM_PARAM_LOAD2 }
S3S3D3_TT_STRUCT  -> { TENSOR_TENSOR_ARITH_OP, TENSOR_TENSOR_BITVEC_OP,
                       BATCH_NORM_BACK_PROP, COPY_PREDICATED, CAST_PREDICATED }
```

`[HIGH/OBSERVED]` The forward-statistics path is the first row; the back-prop side
(`GradAccum`/`GradAccum2`/`BackProp`/`ParamLoad`/`ParamLoad2`) is documented at its sibling
pages — see [GradAccum](batchnorm-gradaccum.md), [Back-Prop](batchnorm-backprop.md),
[ParamLoad](batchnorm-paramload.md).

---

## 2. The forward-statistics operand struct — `NEURON_ISA_TPB_S4D2_BN_STRUCT` (64 B)

Read field-exact from `aws_neuron_isa_tpb_s4d2_bn.h` (CAYMAN arch-isa header; its own
`// (NN)` offset column reproduced verbatim). Header title: *"Neuron 'S4D2_bn' Format — one 4d
SRC Tensor, one 2d DST Tensor. Use for: BatchNormStats / BatchNormStats2 / BatchNormAggregate
/ Max8 / FindIndex8."* `[HIGH/OBSERVED]`

| off | size | field | type | role |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `{opcode, inst_word_len, debug_cmd, debug_hint}` |
| 4–11 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12–31 | 20 | `src_mem_pattern` | `TENSOR4D` | **INPUT** (4-D strided; ≤ 512 elems) |
| 32 | 1 | `in_dtype` | `DTYPE` | source element dtype (`AllowFP32R=False`) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype (`AllowFP32R=True`) |
| 34 | 1 | `num_active_channels` | `u8` | partition count, 1..128 |
| 35–39 | 5 | `reserved0[5]` | — | must be zero |
| 40–43 | 4 | `even_count` | `f32` | fp32 immediate, passed through to output |
| 44–47 | 4 | `odd_count` | `f32` | fp32 immediate, passed through to output |
| 48–59 | 12 | `dst_mem_pattern` | `TENSOR2D` | **OUTPUT** (must be 6 elements for Stats2) |
| 60–63 | 4 | `reserved1[4]` | — | must be zero |

```c
typedef struct NEURON_ISA_TPB_S4D2_BN_STRUCT {
    NEURON_ISA_TPB_HEADER   header;               //  0 -  3
    NEURON_ISA_TPB_EVENTS   events;               //  4 - 11
    NEURON_ISA_TPB_TENSOR4D src_mem_pattern;      // 12 - 31   ADDR4 start; int16 step[4]; u16 num[4]
    NEURON_ISA_TPB_DTYPE    in_dtype;             // 32
    NEURON_ISA_TPB_DTYPE    out_dtype;            // 33
    uint8_t                 num_active_channels;  // 34
    uint8_t                 reserved0[5];         // 35 - 39
    float                   even_count;           // 40 - 43
    float                   odd_count;            // 44 - 47
    NEURON_ISA_TPB_TENSOR2D dst_mem_pattern;      // 48 - 59   ADDR4 start; int16 step[2]; u16 num[2]
    uint8_t                 reserved1[4];         // 60 - 63
} NEURON_ISA_TPB_S4D2_BN_STRUCT;                  // ISA_STATIC_ASSERT == 64
```

`TENSOR4D` (20 B) = `{ADDR4 start_addr; int16 step_elem[4]; uint16 num_elem[4]}`; `TENSOR2D`
(12 B) = `{ADDR4 start_addr; int16 step_elem[2]; uint16 num_elem[2]}`. `start_addr` (`ADDR4`)
carries the SBUF/PSUM partition-offset encoding; both src and dst are
`AllowedInPSUM=True, AllowedInSBUF=True`. `[HIGH/OBSERVED common.h TENSOR4D/TENSOR2D]`

### 2.1 Device-side struct-read proof — handler `@IRAM 0xb380`

The `BatchNormStats2` handler decodes this struct **byte-exact**. Its prologue allocates a
**96-byte** windowed frame (the deepest of any BN handler — it spills 512-bit vector
registers across the stats fold), then `S:`-logs through the SEQ log-helper:

```
b380:  36c100      entry    a1, 96            ; deep frame: holds the 512-bit vector spills
b389:  a40800      const16  a10, 8            ; SEQ log shim: NOTIFY preamble
b38c:  a45622      const16  a10, 0x2256       ;   -> "S: NOTIFY"
b38f:  25c80c      call8    0x18010           ;   the 'S:'-log helper
...                                           ; (interrupt/notification preamble logs)
b46f:  229106      l16si    a2, a1, 12        ; read inst[12]  (TENSOR4D region)
b472:  225110      s16i     a2, a1, 32        ; *** stage in_dtype @ off 32
b475:  22010e      l8ui     a2, a1, 14        ; read inst[14]
b478:  224122      s8i      a2, a1, 34        ; *** stage num_active_channels @ off 34
b47b:  220123      l8ui     a2, a1, 35        ; *** reserved0/dtype byte @ off 35
       ...         and -32/-33/-65 + addi 18 + extui 0,7   ; mask/normalise the dtype nibble
b4a7:  224123      s8i      a2, a1, 35
...
b4fc:  a46122      const16  a10, 0x2261       ; *** "S: BatchNormalize" — the family self-name
b4ff:  25b10c      call8    0x18010           ;     log it
```

The byte-precise offsets (`32 = in_dtype`, `34 = num_active_channels`, `35 = reserved0`) match
the `S4D2_BN` layout exactly, confirming the handler decodes *this* struct. These are clean
scalar Xtensa instructions, **above** the FLIX-desync line — the heavy lane math that follows
is FLIX-scheduled and mis-decodes under stock linear sweep, so it is **not** quoted as
instructions here. `[HIGH/OBSERVED — entry/l8ui/s8i/const16/call8 read directly; the compute
body MED/INFERRED across the FLIX desync]`

> **GOTCHA — `S: BatchNormalize` is preceded by NOTIFY-class preamble logs.** The Stats2
> handler emits `S: sending interrupt` (`0x2227`), `S: sending notification` (`0x223d`), and
> `S: NOTIFY` (`0x2256`) *before* the `S: BatchNormalize` self-name (`0x2261`). A reader
> grepping for the first `const16 a10, 0x22xx` in the body lands on the **event/semaphore**
> preamble, not the kernel name. The kernel name is the one at `0xb4fc`. `[HIGH/OBSERVED —
> DRAM strings 0x2227/0x223d/0x2256/0x2261]`

### 2.2 What Stats2 actually computes (header semantics, verbatim sense)

> *"This instruction reads the `src_mem_pattern` (up to 512 elements), calculates the Mean of
> the even elements (`even_mean`), the Mean of the odd elements (`odd_mean`), the
> Population_Variance of the even elements × the number of even elements (`even_nvar`), the
> Population_Variance of the odd elements × the number of odd elements (`odd_nvar`). It writes
> six values to the destination tensor: `even_count` (fp32 immediate), `even_mean`,
> `even_nvar`, `odd_count` (fp32 immediate), `odd_mean`, `odd_nvar`."* — `s4d2_bn.h`

Where `even = in[0],in[2],in[4],…` and `odd = in[1],in[3],…` (the header's own 5-element
worked example: 3 even, 2 odd; `even_count = 3.0`, `odd_count = 2.0`). The `*_count`
immediates are **passed through verbatim** to the output (the kernel does not recount them —
the host supplies the element counts as fp32, and they must be finite: `pos_inf/neg_inf/NaN`
forbidden by `has_bns_finite_even_count`/`…_odd_count`). `[HIGH/OBSERVED header + validity
predicates]`

**Constraints (validity predicates, OBSERVED):** `src` num-elems ≤ 512; `dst` num-elems == 6;
`out_dtype ∈ {fp32, bf16, fp16}`. `[HIGH/OBSERVED has_bnstats2_src_element_cnt (≤ 512),
has_bnstats2_dst_element_cnt (== 6), s4d2_bn_stats_out_type]`

> **QUIRK — `even_count`/`odd_count` are zero for the *siblings* of Stats2.** For
> `Aggregate`, `Max8`, and `FindIndex8` the validity gate is `s4d2_bn_zero_counts`:
> `even_count == 0 && odd_count == 0`. The count immediates are **live** only for `Stats`/
> `Stats2`; the same struct slots must be zeroed for the other four opcodes that share the
> wire format. A reimplementer reusing the encoder across the family must clear these for
> non-stats ops. `[HIGH/OBSERVED s4d2_bn_zero_counts vs has_bns_finite_*_count]`

### 2.3 `BatchNormStats` (`0x60`, deprecated) and `TransposeBatchNormStats2` (`0x82`)

`Stats` is the **single-set** variant: `src` ≤ 256, `dst` == 3 elements
(`{count, mean, n_pvar}`), no even/odd split. It is marked `n` ("use Stats2"). The narrower
≤ 256 source bound mirrors the 256-entry reciprocal RAM (§3) — a single Stats stream consumes
one reciprocal table per set. `TransposeStats2` (`0x82`) is *identical* to Stats2 with two
extra constraints — `num_active_channels % 32 == 0` and `src_num_elems % 32 == 0` — and shares
trampoline `0x308e`. `[HIGH/OBSERVED has_bnstats_*_element_cnt (≤ 256, == 3) +
is_valid_transpose_batchnorm_stats2]`

### 2.4 `BatchNormAggregate` (`0x62`) — folding subsets into the final variance

`Aggregate` is what converts the deferred `n·var` form into the true population variance. Its
`src` is a multiple of 3 elements, each a `{count, mean, n_pvar}` subset tuple (the exact
output shape of Stats); `dst` == 2 elements: `{mean_full, variance_full}`.

> *"Input: 'n_pvar', the number of elements in the subset multiplied by the population
> variance of that subset, the form output by BatchNormStats2. Output: 'variance', the
> population variance of the aggregated set, required to generate the batchnorm
> scale/offset."* — `s4d2_bn.h`

That last clause is the decisive forward-affine evidence (§7): the DVE produces the
**inputs** to the `scale`/`offset` fusion; it never forms the affine. **All** elements of an
`Aggregate` `src` (including the `count`) must be the same dtype, *"which is nonintuitive,
because all will go through the same type conversion."* `[HIGH/OBSERVED header +
has_bnstats_agg_*_element_cnt (%3==0, ==2) + s4d2_bn_agg_in_type/…_out_type]`

---

## 3. The mean / variance SOURCE — the 256-reciprocal Parameter-RAM

The header is explicit about how the divide-by-count happens — and it is **not** a hardware
divide:

> *"the DVE Parameter RAM must be loaded with 256 reciprocals: 1, 1/2, 1/3, 1/4, 1/5 … 1/255,
> in that order, in fp32 format."* — `s4d2_bn.h`

This is a dedicated hardware bank in the RTL address map (`address_map_flat.yaml`,
`cayman-arch-regs`), replicated per TPB:

| HW region | base (TPB_0) | size | meaning |
|---|---|---|---|
| `TPB_0_DVE_BANK_PARAMETER_RAM` | `0x2802B87000` | `0x400` | **256 × fp32 reciprocals** `1 … 1/255` |
| `TPB_0_DVE_BANK_DATAPATH_RAM` | `0x2802B82000` | `0x4000` | per-lane datapath state (ParamLoad target) |
| `TPB_0_DVE_BANK_OPCODE_RAM` | `0x2802B86000` | `0x400` | per-engine opcode microstore |
| `TPB_0_DVE_NX_IRAM` / `NX_DRAM` | `0x2802B20000` / `0x2802B40000` | — | the carved firmware |

`0x400` bytes ÷ 4 (fp32) = exactly **256 entries** — the size *is* the proof of the layout.
`[HIGH/OBSERVED — header text + RTL map; both re-read this pass]`

The RAM is loaded by a **separate** opcode, `LOAD_PARAMETER_RAM` (`0x66`), issued once before
a batch of Stats instructions; the reciprocal table is **resident HW state**, not part of the
per-instruction operand struct. `[HIGH/OBSERVED opcode enum]`

### 3.1 The realisation — `mean = sum × recip_RAM[N]`

```c
/* BatchNormStats2 forward-statistics finalisation (per even/odd half, per channel).
   N = element count of this half; sum/sumsq accumulated in fp32 across the stream.
   The divide-by-N is a TABLE MULTIPLY against the preloaded reciprocal RAM —
   no hardware divide is issued for the mean.                                       */

float recip_N = PARAMETER_RAM[N];          /* 1.0f/N, fp32, preloaded entry N (N in 1..255) */
float mean    = sum * recip_N;             /* one fp32 multiply, not a divide               */

/* Variance is kept in the "n*var" representation so the /N is DEFERRED to Aggregate:
       n_var = sum_of_squares - N * mean*mean            (population variance * N)
   Aggregate later multiplies the COMBINED n_var sum by recip_RAM[N_total] to get var.       */
float n_var   = sumsq - (float)N * (mean * mean);   /* still scaled by N — NOT divided here  */
```

The 256-entry bound (`1 … 1/255`) caps a single Stats2 even/odd half at ≤ 256 elements (512
total), which is *why* the source bound is ≤ 512 and the deprecated single-set `Stats` is
≤ 256: each half indexes one reciprocal-table slot. `[header semantics + RAM size + element
bounds HIGH/OBSERVED; the sum×recip / sumsq−N·mean² finalisation MED/INFERRED — the IVP fold
schedule desyncs under stock objdump]`

> **NOTE — why `n·var` and not `var`.** Emitting `population_variance × N` (rather than the
> finished variance) makes `Aggregate` a *linear* fold: it sums the per-subset `count`,
> `count·mean`, and `n_pvar` contributions and divides **once** at the end. Dividing each
> subset's variance early would force `Aggregate` to re-weight by count and lose the exact
> recombination. This is the standard Welford/Chan parallel-variance trick, lowered onto the
> reciprocal-RAM datapath. `[INFERRED-HIGH — header "n_pvar → variance" + the parallel-merge
> structure]`

### 3.2 When a *true* divide is needed (no table entry / fp normalisation)

If `N > 255` for a fused stream, or an fp normalisation falls outside the table, the kernel
falls back to the reciprocal/Newton **division** machinery (§4) rather than the table. This is
the only place the DVE batch-norm path runs a seed→Newton iteration. `[HIGH/OBSERVED — the
div0/divn/mulsone ops are present in PERF (§4); MED/INFERRED that the Stats fallback is the
caller]`

---

## 4. The reciprocal / rsqrt Newton machinery — what runs WHERE

This is the section the task's premise turns on. The honest, byte-grounded split:

### 4.1 On DVE: reciprocal *division* only (grep-verified, both builds)

`rg 'ivp_(rsqrt0|sqrt0|recip0|div0|divn|mulsone)'` over `DVE_DEBUG_IRAM.dis` **and**
`DVE_PERF_IRAM.dis`:

| op | DEBUG | PERF | role |
|---|---|---|---|
| `ivp_rsqrt0*` / `ivp_sqrt0*` / `ivp_recip0*` | **0** | **0** | *(absent — no rsqrt/recip SEED on DVE)* |
| `ivp_div0nxf16t` | 0 | 2 | fp16 reciprocal/division **SEED** |
| `ivp_divnn_2xf32t` | 0 | 1 | fp32 Newton **division refine** |
| `ivp_mulsonenxf16t` / `ivp_mulsonen_2xf32t` | 0 | 3 | the fused `(1 − d·x)` Newton helper |

Real FLIX bundles (PERF, well-formed with `vbool` predicates), addresses re-read this pass:

```
517e:  { bne.w15 a0,a0,0x86c2; ivp_lvn_2x16s_i v2,a6,0x18c0; ivp_mulnx16 wv0,v23,v16;
         ivp_div0nxf16t v2,v6,vb0; ivp_bsubnormnx16 vb11,v23,v12,v0 }          ; fp16 recip seed
a752:  { ivp_sv2nx8t_i v18,a0,0xd80,vb5; nop; ivp_divnn_2xf32t v14,v1,v5,vb13 } ; fp32 Newton div
e173:  { ...; ivp_mulsonen_2xf32t v30,v0,v30,vb3; ... }                         ; fp32 (1-d*x)
12219: { ...; ivp_div0nxf16t v26,v6,vb5; ... }                                  ; fp16 recip seed
```

`[HIGH/OBSERVED — counts grep-verified 0/0 for rsqrt/recip, 6 div/mulsone in PERF; the quoted
bundles read directly]`

The reciprocal-division Newton step the DVE issues is the standard fixed-point recurrence
`x_{n+1} = x_n·(2 − d·x_n)`, expressed with the `mulsone` "one-minus" helper:

```c
/* DVE reciprocal-division Newton refine  (computes 1/d, then sum/count = sum * (1/d)).
   ivp_div0nxf16t  -> SEED  x0 ~= 1/d   (table-indexed, ~ a few bits)
   ivp_mulsone*    -> r  = mulsone(d, x) = 1 - d*x   (the residual, "one minus product")
   ivp_divnn_2xf32t-> x  = x + x*r        (one Newton-Raphson iteration: x*(2 - d*x))       */
fp x = ivp_div0(d);                 /* seed:  x0 ~ 1/d                                       */
for (int it = 0; it < N_ITERS; ++it) {
    fp r = ivp_mulsone(d, x);       /* r = 1 - d*x      (==0 at the fixed point)             */
    x = ivp_divn(x, r);             /* x = x + x*r  ==  x*(2 - d*x)   (quadratic convergence) */
}
/* mean = sum * x   when the reciprocal RAM has no 1/N entry (N>255) or an fp norm is needed. */
```

`[HIGH/OBSERVED — the three ops present; the iteration *form* INFERRED from the
seed/mulsone/divn triple and the executable oracle §4.3]`

### 4.2 The rsqrt SOURCE — a forward-saved immediate, header-verbatim

`1/√(var+ε)` arrives at the DVE as the **immediate `imm1`** of `BatchNormGradAccumulate`
(`S3S3D1_BN`), not as a computed value. The header is unambiguous:

> *"imm0: the mini-batch mean (μb), saved from forward propagation. imm1: the reciprocal of
> the square root of the mini-batch variance (σb-0.5), saved from forward propagation. These
> values come from immediate pointers only, and must be in fp32/bf16/fp16 format."* —
> `aws_neuron_isa_tpb_s3s3d1_bn.h`

So `μb` (`imm0 @ off 12`) and `σb-0.5` (`imm1 @ off 56`) are both **pointer-immediates** read
from SBUF/PSUM, *"saved from forward propagation."* The DVE never recomputes either. The `+ε`
is folded into the host's variance **before** the rsqrt — it is **not** a DVE instruction
field anywhere in the BN structs. `[HIGH/OBSERVED — header verbatim; this is the decisive
evidence that rsqrt(var) is forward/host-precomputed]`

> **CORRECTION — the header spells it `σb-0.5`, not `σb^-0.5`.** The arch-isa comment uses
> plain text `σb-0.5` (no caret); it denotes `(σb²)^(−1/2)` = the reciprocal square root of
> the variance. Semantically identical to the conventional `σb^(-0.5)`. `[HIGH/OBSERVED —
> exact header bytes]`

### 4.3 The reference `rsqrt0` seed — decoded, but NOT invoked by the DVE BN path

The seed→Newton bodies *do* exist in the in-package ISS value oracle `libfiss-base.so`
(`.text` VMA == file offset), as the executable reference for what an `rsqrt0` *would*
compute — but the DVE BN kernel does not call them:

```
0x878900  module__xdref_rsqrt0_1_1_32f_32f          (fp32 rsqrt SEED)
0x8785f0  module__xdref_recip0_1_1_32f_32f          (fp32 recip SEED)
0x878340  module__xdref_div0_32f_32f                (fp32 div SEED — DVE issues the fp16 form)
0x878c30  module__xdref_divn_1_1_1_32f_32f_32f_32f_2 (fp32 Newton div REFINE — DVE issues this)
0x5b8290  module__xdref_mulsone_1_1_1_1_32f_32f_32f_2 (fp32 (1-d*x) helper — DVE issues this)
```

`rsqrt0_32f @0x878900` is **integer-only soft-float — zero hardware FP** (proven by disasm):
it extracts the IEEE binary32 fields and table/poly-seeds them. First-pass disasm confirms the
classic field-extract:

```
878907:  shr  $0x17, %edi        ; exponent  = bits[30:23]   (>> 23)
878919:  and  $0x7fffff, %r9d    ; mantissa  = bits[22:0]    (& 0x7fffff)
878951:  shr  $0x1f, %r14d       ; sign      = bit[31]       (>> 31)
878984:  shr  $0x16, %edx        ; binade-parity index into the seed table
```

This is the "magic-constant + table lookup" rsqrt seed (the `table__RSQRT_Data8` 128-entry
seed table, accuracy ≈ 7.5 bits → **two Newton iterations** to reach fp32's 24 bits), refined
by the `divn`/`mulsone` Newton step — see
[B15 fp32 transcendental seeds](../../isa/ref/b15-sp-lookup.md) for the bit-exact seed value
semantics. The `div0`/`divn`/`mulsone` siblings **are** the ops the DVE issues (§4.1), so the
DVE's reciprocal-division Newton math is pinned by these oracle bodies; the `rsqrt0` body is
reference-only here. `[HIGH/OBSERVED — symbol addresses + the shr/and field-extract shape; the
DVE-issues-div-not-rsqrt split HIGH/OBSERVED from §4.1; the seed accuracy CARRIED from B15]`

> **WALL — the exact `rsqrt0`/`div0` seed-table coefficients are not enumerated here.** The
> named SEED primitives and the field-extract shape are OBSERVED; the full 128-entry seed LUT
> is *callable* via the libfiss oracle but not transcribed on this page (it is owned by
> [B15](../../isa/ref/b15-sp-lookup.md)). This is a closable wall — drive the
> `module__xdref_rsqrt0_…` leaf live via `ctypes` to extract any single seed value.
> `[LOW/CARRIED — consistent with the B15/ISS-13 seed-LUT scope]`

---

## 5. The SIMD loop + dtype handling

The DVE NX core carries the full Vision-Q7 IVP vector datapath. The BN-relevant IVP
vocabulary, harvested across `DVE_PERF_IRAM.dis` (OBSERVED counts, top families):

| IVP family | count | role in the stats loop |
|---|---|---|
| `ivp_lsn_*`/`ivp_lvn_*`/`ivp_lvnx*` | ~236 | strided vector **LOADS** for the `TENSOR4D` src pattern |
| `ivp_mul*` | ~195 | per-lane multiply (scale, `sum×recip`, `N·mean²`) |
| `ivp_sel*`/`ivp_dsel*` | ~303 | lane select — the **even/odd split** + strided address-gen |
| `ivp_dextrprn_2x32` | ~118 | extract/recombine — fp32 lane packing |
| `ivp_mulus*`/`ivp_mulusp*` | ~100 | multiply-**accumulate** — sum-of-squares for `n·var` |
| `ivp_baddnormnx16` | ~26 | add-normalise — the running **sum** accumulate |
| `ivp_rep*` | ~75 | broadcast a per-lane scalar (the count/mean) across elements |
| `ivp_bmax*`/`ivp_bmin*` | ~140 | the `Max8`/`FindIndex8` siblings (same struct) |
| `ivp_div0nxf16t`/`ivp_divnn_2xf32t`/`ivp_mulsone*` | 6 | reciprocal-division Newton (§4) |

`[HIGH/OBSERVED vocabulary; the exact per-op FLIX schedule desyncs under stock objdump → MED]`

### 5.1 The streaming loop (structure)

```c
/* BatchNormStats2 streaming fold — one DVE NX core, num_active_channels (1..128) lanes.
   All inputs are CONVERTED TO fp32 first; sum/sumsq accumulate in fp32 (no fp widening).   */
for (int ch = 0; ch < num_active_channels; ++ch) {        /* partitioned across the lanes   */
    fp32 sum_e = 0, sum_o = 0, ssq_e = 0, ssq_o = 0;
    int  n_e = 0, n_o = 0;
    for (each strided vector chunk of src_mem_pattern[ch]) {   /* ivp_lvn_* strided loads    */
        vec x = to_fp32(load_strided(chunk));                  /* in_dtype -> fp32           */
        /* even/odd split by element index (ivp_sel/ivp_dsel on the lane-index parity)       */
        vec xe = select_even(x), xo = select_odd(x);
        sum_e += hreduce_add(xe);  sum_o += hreduce_add(xo);   /* ivp_baddnorm + reduce      */
        ssq_e += hreduce_add(xe*xe); ssq_o += hreduce_add(xo*xo); /* ivp_mulus MAC           */
        n_e += count(xe);  n_o += count(xo);
    }
    fp32 mean_e = sum_e * PARAMETER_RAM[n_e];   /* table multiply, NOT a divide (§3)         */
    fp32 mean_o = sum_o * PARAMETER_RAM[n_o];
    fp32 nvar_e = ssq_e - (fp32)n_e * mean_e * mean_e;   /* "n*var" — divide-by-N DEFERRED   */
    fp32 nvar_o = ssq_o - (fp32)n_o * mean_o * mean_o;
    /* 6 fp32 outputs, broadcast (ivp_rep) and stored to dst_mem_pattern (TENSOR2D, 6 elems) */
    store6(dst[ch], even_count /*imm, passthrough*/, mean_e, nvar_e,
                    odd_count  /*imm, passthrough*/, mean_o, nvar_o);
}
```

The cross-element fold reuses the [cross-lane reduce](cross-lane-reduce.md) machinery (the
`ivp_r*` reduce ops fold a vector register to a scalar; fp32-math accumulator, no widening for
floats). `[struct + IVP vocab HIGH/OBSERVED; the exact fold/loop bounds MED across FLIX
desync]`

### 5.2 dtype handling

| stage | dtypes | notes |
|---|---|---|
| **INPUT** (`in_dtype`) | any valid dtype **except FP32R** (`AllowFP32R=False`) | fp32/bf16/fp16 typical; **all** inputs (incl. `Aggregate`'s count) converted to fp32 first |
| **COMPUTE** | fp32 throughout | sum/var math is fp32; no hardware FP, no fp64 — soft-float bodies |
| **OUTPUT** (`out_dtype`) | `{fp32, bf16, fp16}` (`AllowFP32R=True`) | the `*_count` immediates are fp32, passed through verbatim; must be finite |

For the grad path (`S3S3D1_BN`): `src0`/`src1` ∈ `{fp16, bf16, fp32, i32}` → fp32; immediate
dtype ∈ `{fp16, bf16, fp32}` or `Invalid (0x0) ⇒ fp32 default`. See the
[unified dtype model](dtype-model.md) for the full code space. `[HIGH/OBSERVED — the
s4d2_bn/s3s3d1_bn validity predicates: is_valid_dtype(…, AllowFP32R::False/True),
bnga_valid_input_type, the immediate_dtype == Invalid ⇒ fp32 rule]`

---

## 6. Dispatch — entry → DRAM jump table → trampoline → Handler

The six BN opcodes route through the SEQ direct-indexed DRAM jump table: `key = opcode − 0x41`,
table1 base at DRAM file offset `0x814` (device VA `0x80814`). The trampoline targets read
**byte-exact** this pass from `DVE_DEBUG_DRAM.bin` (LE32 words at `0x814 + 4·idx`):

| opcode | idx | table off | LE32 word | trampoline |
|---|---|---|---|---|
| `0x60` Stats | `0x1f` | `0x890` | `8e 30 00 00` | `0x308e` (shared S4D2_BN) |
| `0x61` Stats2 | `0x20` | `0x894` | `8e 30 00 00` | `0x308e` (shared S4D2_BN) |
| `0x62` Aggregate | `0x21` | `0x898` | `8e 30 00 00` | `0x308e` (shared S4D2_BN) |
| `0x63` GradAccum | `0x22` | `0x89c` | `9e 30 00 00` | `0x309e` |
| `0x64` ParamLoad | `0x23` | `0x8a0` | `b6 30 00 00` | `0x30b6` |
| `0x65` BackProp | `0x24` | `0x8a4` | `ae 30 00 00` | `0x30ae` |

Raw bytes (`xxd -s 0x890 -l 0x20`): `8e30 0000 8e30 0000 8e30 0000 9e30 0000 / b630 0000 ae30
0000 e630 0000 0732 0000`. The whole `S4D2_BN` family (`Stats`/`Stats2`/`Aggregate`/
`TransposeStats2`/`Max8`) converges on trampoline `0x308e` — one decode entry for the one
shared struct, branching internally on the opcode. The trampolines are FLIX-desynced under
linear sweep but each carries `… call8 <invoke thunk> ; j 0x3212`, converging to the common
Handler-object `execute()` invocation at IRAM `0x3212`. `[HIGH/OBSERVED — dispatch words read
directly; the per-tramp call8 target MED across the FLIX desync]`

**Handler entry prologues** (`DVE_DEBUG_IRAM.dis`, OBSERVED `entry` widths):

| handler | entry | frame | self-name log |
|---|---|---|---|
| `BatchNormStats2` | `0xb380` | `entry a1, 96` | `const16 a10, 0x2261` @`0xb4fc` → `S: BatchNormalize` |
| `GradAccum` | `0xb518` | `entry a1, 48` | `0x22a0` |
| `GradAccum2` | `0xb55b` | — | `0x22bc` |
| `ParamLoad` | `0xb62c` | `entry a1, 48` | `0x22f0` |
| `ParamLoad2` | `0xb694` | `entry a1, 48` | `0x230c` |
| `BackProp` | (shared `S3S3D3_TT`) | — | `0x2350` |

> **NOTE — Stats2's 96-byte frame is the deepest.** Every other BN handler uses `entry a1,
> 48`; only Stats2 allocates `entry a1, 96`. The extra 48 bytes hold the 512-bit vector spills
> of the even/odd sum/sum-of-squares fold — independent confirmation that Stats2 is the only
> *heavy compute* handler in the family (ParamLoad/GradAccum mostly stage immediates).
> `[HIGH/OBSERVED — entry widths re-read this pass]`

The PERF (production) build confirms the same routing (`0x61 → 0x835a`, `0x62 → 0x8363`; the
three deprecated variants `0x63/0x64/0x65 → 0x9090` share one handler). `[HIGH for
0x61/0x62; MED for the obscured high-idx PERF rows where the tiny 0x2fc0 PERF DRAM overlaps
the assertion string pool]`

---

## 7. Where the forward affine actually runs (and why it is NOT here)

The inference-time apply `y = γ·(x−μ)·rsqrt(var+ε) + β` is **fused** into a single affine and
run on the **ACTIVATION** engine, not the DVE:

```
scale  = γ · rsqrt(var + ε)                 ; the batch-norm "scale"
offset = β − γ · μ · rsqrt(var + ε)         ; the batch-norm "offset"
y      = scale · x + offset                 ; one Activate/TensorScalar affine:
                                            ;   OUT = func(scale*IN + bias), func = identity/pwl_bypass
```

The DVE's role is **statistics** (Stats2 → mean, n·var; Aggregate → variance) and the training
**gradient** (GradAccum). The `Aggregate` header's own clause — its variance output is
*"required to generate the batchnorm scale/offset"* — names the handoff: DVE produces the
inputs, the host/compiler fuses `scale`/`offset` and emits an Activate/TensorScalar to apply
it. This is precisely why **no** `γ`/`β`/`sqrt` apply-kernel exists on DVE. `[INFERRED-HIGH —
Aggregate header + the affine-apply pattern on ACT + the *absence* of a DVE apply kernel; the
per-op host fusion sequence is not in this firmware image]`

This statistics→apply split mirrors the forward-image flow in
[the MARIANA × DVE image walkthrough](../../images/mariana-dve.md) — *forward-link; that page
is planned for Part 6 and not yet authored.* `[NOTE — planned cross-link]`

---

## 8. Per-generation presence

The five BN operand structs (`s4d2_bn`, `s3s3d1_bn`, `s3s3d1_bn2`, `s2_bn`, `s2_bnpl2`) and
the opcode enum are present across the `cayman / mariana / mariana_plus / maverick / sunda`
arch-isa header trees (each header file exists per gen, fd-verified). The DVE firmware is the
same `cayman/seq/` NX SEQ engine; the BN handler subset and the 256-reciprocal Parameter-RAM
model are stable across the Trainium-class generations that ship the DVE engine.

| GEN | `S4D2_BN`/BN struct headers | DVE BN handler bodies | Parameter-RAM model |
|---|---|---|---|
| **SUNDA** (v2) | present (per-gen header) | not separately diffed this pass | header-stated `256×fp32` |
| **CAYMAN** (v3) | present — **the carve substrate** (handler `@0xb380` decoded byte-exact) | **OBSERVED** (Stats2 entry + struct reads + dispatch) | **OBSERVED** (`0x2802B87000`/`0x400`) |
| **MARIANA** (v4) | present (per-gen header) | structurally identical SEQ engine | header-stated |
| **MARIANA_PLUS** (v4+) | present (per-gen header) | structurally identical SEQ engine | header-stated |
| **MAVERICK** (v5) | present (per-gen header) | **header-OBSERVED → interior INFERRED** | header-stated |

`[HIGH/OBSERVED — per-gen header presence (fd-verified) + the CAYMAN DVE carve decoded this
pass; MED/INFERRED for the non-CAYMAN handler-body byte-equality, which was not exhaustively
diffed]`

> **MAVERICK (v5) interior — header-OBSERVED only → INFERRED.** Per the
> [generation-grounding policy](../../reference/confidence-model.md), the v5 BN struct/opcode
> *presence* is OBSERVED (its `aws_neuron_isa_tpb_s4d2_bn.h` and the opcode enum ship in the
> `neuron_maverick_arch_isa` tree), but the v5 DVE handler **interior** is reasoned from the
> CAYMAN carve plus the header equality, **not** separately carved and decoded here. Treat
> v5 Stats2's `entry`-width, field-read offsets, and FLIX schedule as **INFERRED** until a v5
> DVE image is carved and diffed. `[HIGH/OBSERVED header; MED/INFERRED interior]`

---

## 9. Confidence ledger

**HIGH / OBSERVED (disassembly, byte read, header read, or oracle execution this pass):**

* The four carves reproduce SX-IMG-04 byte-identically (sha256 `259769ff`/`c106642d`/
  `9fa066f4`/`eb980f98`); reset vector `06 76 00 00` (`j 0x1dc`); `objdump` exit 0.
* The six BN `S:` handler names at DRAM `0x2261`/`0x22a0`/`0x22bc`/`0x22f0`/`0x230c`/`0x2350`;
  the Stats2 self-name `const16 a10, 0x2261` @`0xb4fc` + `call8 0x18010` @`0xb4ff`.
* The Stats2 handler `@0xb380` (`entry a1, 96`) decoding `in_dtype@32`/`num_active_channels@34`/
  `reserved0@35` byte-exact (clean scalar instructions, above the FLIX line).
* The `S4D2_BN` struct field-exact (`ISA_STATIC_ASSERT==64`); the six-output even/odd
  count/mean/n·var semantics; the `s4d2_bn_zero_counts` (Aggregate/Max8/FindIndex8) vs
  finite-count (Stats2) gate.
* The mean/var SOURCE: the 256×fp32 reciprocal Parameter-RAM
  (`TPB_0_DVE_BANK_PARAMETER_RAM @0x2802B87000`, size `0x400`) per the header + RTL map;
  `LOAD_PARAMETER_RAM` (`0x66`).
* `rsqrt(var) = σb-0.5` is a FORWARD/HOST-precomputed pointer-immediate (`imm1`) to GradAccum
  (`S3S3D1_BN` header, verbatim) — NOT computed on DVE.
* No `ivp_rsqrt0`/`sqrt0`/`recip0` in DVE (grep `0/0` both builds); DVE issues
  `ivp_div0nxf16t`/`ivp_divnn_2xf32t`/`ivp_mulsone*` (reciprocal-division Newton) — 6 real
  PERF bundles.
* The reference `rsqrt0`/`recip0`/`div0`/`divn`/`mulsone` seed bodies in `libfiss-base`
  (`rsqrt0_32f @0x878900` = integer-only soft-float IEEE field-extract).
* The dispatch: BN opcodes in DRAM table1 @`0x814` (idx `0x1f`–`0x24`), S4D2_BN family →
  trampoline `0x308e`, converging to Handler invoke @`0x3212`; dispatch words read byte-exact.

**MED / INFERRED:**

* The `mean = sum × recip_RAM[N]` and `n_var = sumsq − N·mean²` finalisation (header semantics
  + recip-RAM + IVP vocab OBSERVED; the exact FLIX loop schedule desyncs under stock objdump).
* The reciprocal-division Newton *iteration form* (the three ops are OBSERVED; the
  `x·(2−d·x)` recurrence is reasoned from the seed/`mulsone`/`divn` triple + the oracle body).
* The forward affine on ACT (`scale=γ·rsqrt`, `offset=β−γ·μ·rsqrt`) — INFERRED-HIGH from the
  `Aggregate` header + the apply pattern + the absence of a DVE apply kernel.
* Non-CAYMAN DVE BN handler-body byte-equality (header presence OBSERVED; bodies not
  exhaustively diffed) → MAVERICK interior INFERRED.

**LOW / WALL:**

* The exact `rsqrt0`/`div0` seed-table coefficients (named primitives + field-extract shape
  OBSERVED; full 128-entry LUT owned by [B15](../../isa/ref/b15-sp-lookup.md), callable via
  the libfiss oracle but not transcribed here).
* The exact host/forward locus that computes `σb-0.5` (ACT-PWL rsqrt table vs host CPU rsqrt)
  — external to the DVE firmware image.

---

## See also

* [BatchNormalize — GradAccum](batchnorm-gradaccum.md) — the back-prop gradient sums that
  consume `μb` and `σb-0.5` as immediates.
* [BatchNormalize — ParamLoad](batchnorm-paramload.md) — staging `N`/`batch_mean`/`A,B,C` into
  the per-lane datapath flops.
* [BatchNormalize — Back-Prop](batchnorm-backprop.md) — the element-wise apply over the shared
  `S3S3D3_TT` Tensor-Tensor format.
* [B15 — fp32 transcendental seeds (`sp_lookup`)](../../isa/ref/b15-sp-lookup.md) — the
  `rsqrt0`/`recip0`/`div0` seed value semantics the Newton machinery refines.
* [The unified datatype model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` code space.
* [Cross-lane reduce](cross-lane-reduce.md) — the `ivp_r*` fold the sum/mean is built on.
* [The Confidence & Walls model](../../reference/confidence-model.md) — what the tags mean.
