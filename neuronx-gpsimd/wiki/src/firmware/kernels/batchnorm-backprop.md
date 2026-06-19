# BatchNormalize — Back-Prop (`BatchNormBackProp`, opcode `0x65`, struct `S3S3D3_TT`)

This page decodes the **backward** half of the DVE (Data/Vector Engine, `engine_idx=3`)
**BatchNormalize** kernel family on the GPSIMD NeuronCore: the gradient pass that turns the
incoming activation gradient `∇y` (`d_y`) and the forward ofmap `Om` into the input gradient
`d_x`, reusing the **same saved forward statistics** (`μb` / `σb^-0.5` / `N`) the
[forward-statistics page](batchnorm-forward.md) established. The firmware **self-names** this
handler `S: BatchNormalizeBackProp` — and the first thing this page settles is *which engine*
runs it and *how the standard BN-backward algebra is split across two opcodes*. The answer:
back-prop is a **DVE-engine kernel** (not POOL, not ACTIVATION); the full
`d_x = (γ·inv_std/N)·(N·d_y − d_beta − x_norm·d_gamma)` is a **two-instruction
decomposition** — `BatchNormGradAccumulate` does the batch **reduce** (the three sums),
`BatchNormBackProp` does the per-element **apply**. This page pins the dispatch chain
byte-exact, decodes the shared 64-byte `S3S3D3_TT` Tensor-Tensor wire format field-exact from
the recovered arch-isa header, proves the saved-statistics reuse header-verbatim, and resolves
the central reimplementation question — *what does the `op` field carry for BackProp?* — to a
hard, predicate-grounded answer: **`AluOp::Bypass` (`0x00`), gated by `s3s3d3_tt_is_zero_op`.**

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD compute core's** own
firmware — windowed-ABI Xtensa code in the `ncore2gp` (Xtensa24, RI-2022.9, NX1.1.4, 32-byte
FLIX/VLIW) configuration — plus its NX/SEQ sequencer dispatch. Every device fact below is
byte-pinned to the same DVE carve the forward page used, re-disassembled **this pass** from
`libnrtucode_internal.so` with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); every
host-ISA fact is read out of the `aws_neuron_isa_tpb_*.h` arch-isa headers and
`instruction_mapping.json` shipped in the same customop-lib package. The `extracted/` tree is
gitignored — reach it with `fd --no-ignore` or absolute paths. Confidence and evidence tags
follow the project [Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / read-from-header this pass, `[MED/INFERRED]` = reasoned
over OBSERVED, `[…/CARRIED]` = re-used at a cited sibling page's confidence.

> **NOTE — the carve, the exact objects, and the offset conventions.** The firmware container
> is `…/custom_op/c10/lib/libnrtucode_internal.so`. The DVE images are `.rodata`-resident, so
> **file offset == device VA** for `.text`/`.rodata` (no `.data` delta applies to these
> carves); IRAM offset == device IRAM VA (reset vector at byte 0); DRAM string offset ==
> device DRAM VA − `0x80000`. Re-verified byte-identical to the forward page's carve this
> pass:
>
> | carved object | sha256 (first 8) | role |
> |---|---|---|
> | `DVE_DEBUG_IRAM` | `259769ff` | handler bodies + `S:` self-name logs |
> | `DVE_DEBUG_DRAM` | `c106642d` | the `S:` strings + dispatch table |
> | `DVE_PERF_IRAM` | `9fa066f4` | production build (the `div`/`mulsone` Newton bundles) |
> | `DVE_PERF_DRAM` | `eb980f98` | production dispatch table |
>
> Reset vector (DEBUG IRAM byte 0): `06 76 00 00` = `j 0x1dc` (the SEQ boot path).
> `objdump` exit 0, empty stderr; `DVE_DEBUG_IRAM.dis` = 44,989 lines, `DVE_PERF_IRAM.dis` =
> 35,342 lines. `[HIGH/OBSERVED]`

> **CORRECTION — the arch-isa headers ARE in the package; the `op` field is NOT a selector.**
> Two backing-report premises are overturned here by binary read. (1) The backing survey
> reported the `aws_neuron_isa_tpb_s3s3d3_tt.h` / `…s3s3d1_bn.h` arch-isa C headers as *absent
> from the checkout* (a mitigation it leaned on). They are **present** — `fd --no-ignore`
> finds them in `neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` — so the `S3S3D3_TT`
> layout, the `is_valid_batchnorm_backprop` validity predicate, and the GradAccum
> `imm0`/`imm1` math contract are read **field-exact** below (HIGH, not INFERRED). (2) The
> survey stated *"the `op` field selects the elementwise primitive the apply uses."* The
> header's own predicate `s3s3d3_tt_is_zero_op(i): i.s3s3d3_tt.op == AluOp::Bypass` is **part
> of `is_valid_batchnorm_backprop`** — so for BackProp the `op@14` byte is **forced to `0x00`
> (Bypass)**; it is *not* an arithmetic selector. The apply primitive is hard-wired into the
> BackProp handler body, not chosen by `op`. `[HIGH/OBSERVED — header predicate]`

---

## 0. TL;DR — back-prop in eight facts

1. **BackProp is a DVE-engine kernel.** Opcode `0x65` (`BATCH_NORM_BACK_PROP`) is in the DVE
   SEQ DRAM dispatch table (idx `0x24` → trampoline `0x30ae`) and **absent from the POOL
   kernel_info_table of every carved generation**. It is *not* on ACTIVATION (that runs the
   *forward inference affine*, [forward page §7](batchnorm-forward.md)) and *not* on the
   POOL/GPSIMD compute core. `[HIGH/OBSERVED — DVE table + POOL table enumeration]`
2. **It self-names `S: BatchNormalizeBackProp`** at DRAM `0x2350` (full bytes read), logged by
   the handler body at IRAM `0xb6f0`. `[HIGH/OBSERVED]`
3. **The math is a two-opcode decomposition.** `BatchNormGradAccumulate(2)` (`0x63`/`0x94`,
   `S3S3D1_BN`/`BN2`) is the **batch reduce** — it writes the **three sums** `d_x` needs;
   `BatchNormBackProp` (`0x65`, `S3S3D3_TT`) is the **per-element `d_x` apply**. `[HIGH struct
   roles header; INFERRED-HIGH the term split]`
4. **Saved-statistics reuse, header-verbatim.** GradAccum reads `imm0 = μb` (mean) and
   `imm1 = σb^-0.5` (inv_std) as **pointer-immediates** *"saved from forward propagation"*;
   BackProp never recomputes either. There is **no `rsqrt` on the DVE** (grep `0/0`).
   `[HIGH/OBSERVED — `s3s3d1_bn.h` verbatim]`
5. **`op@14` must be `AluOp::Bypass` (`0x00`).** The `is_valid_batchnorm_backprop` predicate
   includes `s3s3d3_tt_is_zero_op`. The shared wire format's `op` byte is *forced zero*, not a
   selector. `[HIGH/OBSERVED — validity predicate]`
6. **BackProp owns a distinct trampoline + handler.** It does **not** reuse the TensorTensor
   handler — `0x65 → tramp 0x30ae → thunk 0x1fe4 → body 0xb6f0`, distinct from
   `TensorTensorArith` (`0x41 → tramp 0x31e6`). It shares only the **64-byte `S3S3D3_TT` wire
   format** and the **`AluOp`/`alu_op.cpp` elementwise datapath**. `[HIGH/OBSERVED dispatch]`
7. **fp32 compute hub.** `src0`/`src1` ∈ `{fp16, bf16, fp32}` → converted to fp32; all
   gradient math is fp32; output ∈ `{fp32, bf16, fp16}`. `[HIGH/OBSERVED — `is_valid_batchnorm_backprop`]`
8. **The batch reduce is the DVE's own stream-accumulate, not a POOL `CrossLaneReduce`.** The
   `Σ_batch` for `d_gamma`/`d_beta` runs *inside GradAccum* on the DVE MAC / add-normalize
   datapath (`ivp_mulusp*` / `ivp_baddnormnx16`), the same fold geometry the forward Stats2
   uses. `[HIGH IVP vocab OBSERVED; INFERRED-HIGH attribution]`

---

## 1. The backward pass in one diagram

```
  forward (training):  Stats2/Aggregate ─► μb, σb²  ──host──►  inv_std = σb^-0.5  (rsqrt OFF-DVE)
                       ParamLoad ─► N staged ;  Om (ofmap) saved to SBUF/PSUM
        │                                                        │ (saved across the step)
        ▼ backward                                               ▼
  ┌──────────── STEP A — the REDUCE (the three batch sums) ─────────────────────────────────┐
  │  BatchNormGradAccumulate(2)   op 0x63 / 0x94   struct S3S3D1_BN / S3S3D1_BN2             │
  │     imm0=μb  imm1=σb^-0.5  (pointer-immediates, "saved from forward propagation")        │
  │     src0=Om (ofmap)   src1=∇O'm (incoming d_y)                                           │
  │     dst = EXACTLY 3 elements  ──►  { Σd_y , Σ(d_y·x_norm) , the mean-coupling term }     │
  │     reduce = DVE stream-accumulate (ivp_mulusp* MAC + ivp_baddnormnx16), NOT POOL CLR    │
  └──────────────────────────────────────────────┬──────────────────────────────────────────┘
                                                  │  (the 3 sums + the gamma·inv_std/N prefactor)
  ┌──────────── STEP B — the APPLY (per-element d_x) ──────────▼──────────────────────────────┐
  │  BatchNormBackProp   op 0x65   struct S3S3D3_TT   (op field forced AluOp::Bypass=0x00)    │
  │     SEQ 0x65 ─► DRAM table1[0x8a4]=0x30ae ─► thunk 0x1fe4 ─► body 0xb6f0 "S: …BackProp"   │
  │     streams per-element d_y + x_norm, combines with the 3 sums + (gamma·inv_std/N)        │
  │     ──►  d_x  = (gamma·inv_std/N)·( N·d_y − d_beta − x_norm·d_gamma )   element-wise       │
  │     elementwise math = the shared AluOp / alu_op.cpp datapath (the DVE TensorTensor path) │
  └───────────────────────────────────────────────────────────────────────────────────────────┘
```

`[diagram: dispatch bytes + struct fields HIGH/OBSERVED; the per-term apply wiring INFERRED-HIGH
across the FLIX-desynced apply body]`

---

## 2. Where back-prop lives — DVE, not POOL, not ACTIVATION

The forward page's §7 established that the **forward inference affine**
`y = γ·(x−μ)·rsqrt(var+ε) + β` is fused to `scale·x + offset` and run on the **ACTIVATION**
engine. That is the *inference apply* — **not** the training gradient. The whole DVE
"BatchNormalize" handler family (`Stats2` / `Aggregate` / `GradAccum` / `ParamLoad` /
`BackProp`) lives and dispatches on the **DVE engine** (`engine_idx=3`, the SEQ NX core).
BackProp is one of those handlers.

### 2.1 The self-name string — byte-exact

`strings -t x DVE_DEBUG_DRAM.bin | rg BatchNormalize` (re-read this pass):

```
 0x2261  S: BatchNormalize            (= BatchNormStats2, op 0x61 — the forward family name)
 0x22a0  S: BatchNormalizeGradAccum   (op 0x63)
 0x22bc  S: BatchNormalizeGradAccum2  (op 0x94)
 0x22f0  S: BatchNormalizeParamLoad   (op 0x64)
 0x230c  S: BatchNormalizeParamLoad2  (op 0x8e)
 0x2350  S: BatchNormalizeBackProp    (op 0x65)   <== THIS PAGE
```

`xxd -s 0x2350 -l 32 DVE_DEBUG_DRAM.bin` →
`53 3a 20 42 61 74 63 68 4e 6f 72 6d 61 6c 69 7a 65 42 61 63 6b 50 72 6f 70 0a` =
`"S: BatchNormalizeBackProp\n"`. `[HIGH/OBSERVED]`

### 2.2 The POOL compute engine carries NO batch-norm

The POOL/GPSIMD compute engine's `kernel_info_table` (carved from the POOL EXTISA image,
CAYMAN generation, section VMA `0x02000380`, file off `0x7400`, 8-byte records
`{u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA}`) enumerates **17 records** — unique opcode
set `{0x41, 0x45, 0x46, 0x47, 0x51, 0x52, 0x7b, 0x7c, 0x7d, 0x7e, 0xbe, 0xf0(×5 spec), 0xf2}`.
**No `0x65`. No `0x60`–`0x64`, no `0x66` — no batch-norm opcode at all.** The first record
(`0x7400`) is `0x7e IOTA`; the only tensor-tensor entries are `0x41`
(`TENSOR_TENSOR_ARITH_OP`, the sole arith TT) and `0x51` (`TENSOR_TENSOR_BITVEC_OP`). The POOL
EXTISA image carries **no** `batch`/`backprop`/`grad`/`BatchNorm` string anywhere. The smaller
EXTISA-3 table (CAYMAN_3/MARIANA_3, VMA `0x020008c8`, 9 records) is reduction/scan-only and
also has no `0x60`–`0x66`. `[HIGH/OBSERVED — kernel_info_table enumeration across CAYMAN_0/_3
+ MARIANA_3 + string scan this pass]` The SUNDA POOL opcode map likewise carries no `0x65` and
no `batch_norm` function — its only tensor-tensor entry is `pool_tensor_tensor_arith_op`
(`0x41`). `[CARRIED — SUNDA POOL descriptor read by the backing survey; the SUNDA-specific
POOL carve was not re-reproduced this pass]`

> **GOTCHA — the same `S3S3D3_TT` wire format is decoded on TWO engines, but BackProp is only
> on one.** `instruction_mapping.json` binds the 64-byte `S3S3D3_TT` layout to *five* opcodes:
> `TensorTensorArith (0x41)`, `TensorTensorBitvec (0x51)`, `CopyPredicated`, `CastPredicated`,
> and `BatchNormBackProp (0x65)`. The POOL engine decodes its `0x41`/`0x51` against this
> layout; the DVE decodes `0x65` (and its own `0x41`/`Copy`/`Cast`) against the *same* layout.
> A reimplementer must not infer "shared struct ⇒ shared kernel": the struct binding is a
> **wire-format** statement, not an engine statement. `0x65` is **only** in the DVE table;
> `0x41` is in *both* (each engine decodes its own opcodes). `[HIGH/OBSERVED]`

---

## 3. Dispatch — entry → DRAM jump table → trampoline → thunk → handler

The BN opcodes route through the SEQ direct-indexed DRAM jump table: `key = opcode − 0x41`,
table1 base at DRAM file offset `0x814` (device VA `0x80814`). The BackProp word is read
byte-exact this pass.

### 3.1 The DRAM dispatch word

`xxd -s 0x89c -l 16 DVE_DEBUG_DRAM.bin` →
`9e30 0000  b630 0000  ae30 0000  e630 0000`:

| opcode | idx | table off | LE32 word | trampoline |
|---|---|---|---|---|
| `0x63` GradAccum | `0x22` | `0x89c` | `9e 30 00 00` | `0x309e` |
| `0x64` ParamLoad | `0x23` | `0x8a0` | `b6 30 00 00` | `0x30b6` |
| **`0x65` BackProp** | **`0x24`** | **`0x8a4`** | **`ae 30 00 00`** | **`0x30ae`** |
| `0x82` TransposeStats2 / `0x66` LoadParamRAM | … | `0x8a8` | `e6 30 00 00` | `0x30e6` |

For contrast, `TensorTensorArith` (`0x41`, idx `0x00`, table1[`0x814`]) → trampoline `0x31e6`,
and `TensorTensorBitvec` (`0x51`, idx `0x10`, table1[`0x854`]) → `0x31ee` — **distinct**
trampolines from BackProp's `0x30ae`. `[HIGH/OBSERVED — dispatch words read directly]`

### 3.2 The trampoline cluster `0x309e..0x30be`

Each BN trampoline is `call8 <register-handler thunk> ; j 0x3212` (8-byte aligned). The
BackProp trampoline `@0x30ae` disassembles byte-exact (`xtensa-elf-objdump`,
`XTENSA_CORE=ncore2gp`):

```
30ae:  65f3fe   call8  0x1fe4      ; BackProp register-handler thunk
30b1:  465700   j      0x3212      ; → the common Handler invoke
```

The five sibling trampolines (`0x309e` GradAccum→thunk `0x1fac`, `0x30a6` GradAccum2→`0x1fc8`,
`0x30ae` BackProp→`0x1fe4`, `0x30b6` ParamLoad→`0x2000`, `0x30be` ParamLoad2→`0x201c`) sit at
stride `0x1c` thunks apart — one register-handler shim per opcode. `[HIGH/OBSERVED]`

### 3.3 The register-handler thunk `@0x1fe4`

```
1fe4:  366100   entry    a1, 48
1fe7:  240000   const16  a2, 0
1fea:  24f0b6   const16  a2, 0xb6f0      ; *** loads the handler-body VA 0xb6f0
1fed:  226103   s32i     a2, a1, 12      ;     stage body pointer in the Handler-object frame
1ff1:  11da98   l32r     a1, …
1ff4:  a19c05   l32r     a10, …
1ff7:  655207   call8    0x951c          ; register/construct the Handler object
1ffa:  900000   retw
```

The thunk's only job is to bind the handler-object to body VA `0xb6f0` and register it. The
five thunks load adjacent body VAs (`const16 a2,…`, all read directly this pass):

| opcode | thunk | handler body | self-name log |
|---|---|---|---|
| `0x63` GradAccum | `0x1fac` | `0xb518` | `0x22a0` |
| `0x94` GradAccum2 | `0x1fc8` | `0xb55c` | `0x22bc` |
| **`0x65` BackProp** | **`0x1fe4`** | **`0xb6f0`** | **`0x2350`** |
| `0x64` ParamLoad | `0x2000` | `0xb62c` | `0x22f0` |
| `0x8e` ParamLoad2 | `0x201c` | `0xb694` | `0x230c` |

`[HIGH/OBSERVED — clean scalar instructions, all five thunk→body pairs read this pass]`

### 3.4 The handler body `@0xb6f0` — the thinnest frame in the family

```
b6f0:  364100   entry    a1, 32          ; *** thinnest BN frame (vs Stats2's a1,96)
b6f3:  860000   j        0xb6f9
b6f9:  a40800   const16  a10, 8          ; SEQ log shim preamble
b6fc:  a45023   const16  a10, 0x2350     ; *** "S: BatchNormalizeBackProp"
b6ff:  25910c   call8    0x18010         ;     the SEQ 'S:'-log helper
b702:  860000   j        0xb708
b708:  240800   const16  a2, 8
b70b:  247023   const16  a2, 0x2370      ; stage instruction context (literal-pool ptr)
b70e:  2f……     <FLIX-desync — F-format bundles, literal pool interleaved>
```

The BackProp body is the **thinnest** of the family (`entry a1, 32`). Every other BN handler
is `entry a1, 48` except Stats2 at `entry a1, 96`; only the *statistics* handler does the
heavy inline 512-bit lane fold. BackProp's thin frame is the structural signature of a
**decode-and-delegate shim**: it self-names, stages the `S3S3D3_TT` instruction context, then
hands the elementwise compute to the shared `AluOp`/`alu_op.cpp` datapath (the same path the
DVE `TensorTensor`/`CopyPredicated`/`CastPredicated` handlers use). It does **not** carry a
bespoke compute body. `[HIGH/OBSERVED — entry frames + self-name log through `call8 0x18010`;
the "delegates to AluOp" INFERRED-HIGH from the thin frame + the `S3S3D3_TT→BackProp` binding +
the `alu_op.cpp` asserts present in DRAM §8]`

> **GOTCHA — the `l8ui a2,a2,13` + `beqi 8 / bnei 10` chain right after the BackProp body is
> the *MOVE* handler, not BackProp.** Reading linearly past the BackProp body's FLIX-desync
> (`0xb70e` onward) lands on `0xb78b: l8ui a2,a2,13 ; beqi a2,8 ; … bnei a2,10` — which *looks*
> like an `out_dtype@13` (INT32=`0x8` / FP32=`0xA`) branch for BackProp. It is **not**: the
> `const16 a2,…` refs in that span point at DRAM `0x23b0 "S: MOVE"` and the assertion at
> `0x23b9` (`move.cpp:41`: `dtype == UINT32 || INT32 || FP32`). The byte-13 read belongs to
> the **adjacent MOVE handler**, which shares the dtype-byte offset by coincidence of the
> common header layout. Do *not* attribute it to BackProp. (The struct's `out_dtype@13` is
> HIGH from the header anyway — §4 — so nothing is lost.) `[HIGH/OBSERVED — the
> `move.cpp` assertion string + the const16 refs in the span]`

### 3.5 PERF (production) build caveat

In the PERF DRAM table `0x65` routes to `0x9090`, but that span overlaps the assertion
string-pool past idx `~0x40` (the tiny `0x2fc0` PERF DRAM); the `0x9090` target is the
deprecated/error-shim cluster, not the trusted PERF BackProp body. The **DEBUG** dispatch is
the reliable substrate; PERF is cited only for the IVP-vocab harvest (§7). `[MED — documented
PERF-DRAM overlap; the DEBUG chain is the HIGH path]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S3S3D3_TT_STRUCT` (64 B)

Read field-exact from the recovered `aws_neuron_isa_tpb_s3s3d3_tt.h` (CAYMAN arch-isa header,
`ISA header for NC-v3`; its own `// (NN)` offset column reproduced verbatim). The header's own
title comment names BackProp as a user: *"BatchNormBackProp Instruction: back-propagates the
gradients through a batch normalization layer."* `[HIGH/OBSERVED]`

| off | size | field | type | BackProp role |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `{opcode=0x65, inst_word_len, debug_cmd, debug_hint}` |
| 4–11 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 1 | `in0_in1_dtype` | `DTYPE_PAIR` | `dtype_lo`=src0, `dtype_hi`=src1 (packed 4b/4b) |
| 13 | 1 | `out_dtype` | `DTYPE` | the `d_x` output dtype |
| 14 | 1 | `op` | `ALU_OP` | **forced `AluOp::Bypass` (0x00)** for BackProp (§4.1) |
| 15 | 1 | `num_active_channels` | `u8` | partition count, 1..128 |
| 16–31 | 16 | `src0_mem_pattern` | `TENSOR3D` | backward input 0 (3-D strided) |
| 32–47 | 16 | `src1_mem_pattern` | `TENSOR3D` | backward input 1 (3-D strided) |
| 48–63 | 16 | `dst_mem_pattern` | `TENSOR3D` | `d_x` output stream (3-D strided) |

```c
typedef struct NEURON_ISA_TPB_S3S3D3_TT_STRUCT {
    NEURON_ISA_TPB_HEADER     header;             //  0 -  3
    NEURON_ISA_TPB_EVENTS     events;             //  4 - 11
    NEURON_ISA_TPB_DTYPE_PAIR in0_in1_dtype;      // 12        dtype_lo:4 (src0), dtype_hi:4 (src1)
    NEURON_ISA_TPB_DTYPE      out_dtype;          // 13
    NEURON_ISA_TPB_ALU_OP     op;                 // 14        BackProp: must be AluOp::Bypass
    uint8_t                   num_active_channels; // 15
    NEURON_ISA_TPB_TENSOR3D   src0_mem_pattern;   // 16 - 31
    NEURON_ISA_TPB_TENSOR3D   src1_mem_pattern;   // 32 - 47
    NEURON_ISA_TPB_TENSOR3D   dst_mem_pattern;    // 48 - 63
} NEURON_ISA_TPB_S3S3D3_TT_STRUCT;               // ISA_STATIC_ASSERT == 64
```

`TENSOR3D` (16 B) = `{ADDR4 start_addr; int16 step_elem[3]; uint16 num_elem[3]}` (read from
`common.h`); `start_addr` (`ADDR4`) carries the SBUF/PSUM partition-offset encoding; all three
patterns are `AllowedInPSUM=True, AllowedInSBUF=True`. `DTYPE_PAIR` is a single packed byte
`{dtype_lo:4, dtype_hi:4}`. `[HIGH/OBSERVED — `common.h` `TENSOR3D`/`DTYPE_PAIR`/`HEADER`]`

> **NOTE — header compile-verify.** All three sibling structs assert `sizeof == 64`
> (`ISA_STATIC_ASSERT`), the layout was read field-exact from the shipped header, and the
> struct→opcode binding was read from `instruction_mapping.json`
> (`NEURON_ISA_TPB_S3S3D3_TT_STRUCT → {…, NEURON_ISA_TPB_OPCODE_BATCH_NORM_BACK_PROP}`,
> `jq`-verified this pass). `[HIGH/OBSERVED]`

### 4.1 The validity predicate — the decisive `op == Bypass` gate

The header carries the full validity contract `is_valid_batchnorm_backprop(i)`. Reproduced
sense (the predicate names are verbatim from the header comment block):

```c
/* is_valid_batchnorm_backprop — the BackProp acceptance gate (s3s3d3_tt.h, verbatim names). */
bool is_valid_batchnorm_backprop(Inst i) {
    return has_valid_neuron_header(i)
        && has_valid_neuron_events(i)
        && has_batchnorm_backprop_opcode(i)                           /* opcode == 0x65        */
        && is_valid_dtype(dtype_lo(i.in0_in1_dtype), AllowFP32R=false) /* src0 dtype            */
        && is_valid_dtype(dtype_hi(i.in0_in1_dtype), AllowFP32R=false) /* src1 dtype            */
        && is_valid_dtype(i.out_dtype,               AllowFP32R=true)  /* d_x dtype             */
        && s3s3d3_tt_is_zero_op(i)        /* *** i.op == AluOp::Bypass (0x00) — NOT a selector  */
        && start_addr_active_channels(i.src0_mem_pattern.start_addr, i.num_active_channels)
        && start_addr_active_channels(i.src1_mem_pattern.start_addr, i.num_active_channels)
        && start_addr_active_channels(i.dst_mem_pattern.start_addr,  i.num_active_channels)
        && tensor3d_valid(i.src0_mem_pattern, dtype_lo(i.in0_in1_dtype), WriteTensor=false, …)
        && tensor3d_valid(i.src1_mem_pattern, dtype_hi(i.in0_in1_dtype), WriteTensor=false, …)
        && tensor3d_valid(i.dst_mem_pattern,  i.out_dtype,               WriteTensor=true,  …)
        && s3s3d3_tt_src_element_cnt_check(i)   /* prod(src0.num) == prod(src1.num)             */
        && s3s3d3_tt_dst_element_cnt_check(i)   /* prod(src0.num) == prod(dst.num)              */
        && check_active_channels(i.num_active_channels)
        && tt_valid_partitions(i.src0_mem_pattern.start_addr, i.src1_mem_pattern.start_addr);
}
/* s3s3d3_tt_is_zero_op(i): i.s3s3d3_tt.op == AluOp::Bypass   (Bypass == 0x00) */
```

The crucial line is `s3s3d3_tt_is_zero_op`: **BackProp's `op@14` byte must be `0x00`
(`AluOp::Bypass`)**. The same gate appears in `is_valid_copy_cast_predicated` — so the three
"non-arith" users of `S3S3D3_TT` (BackProp, CopyPredicated, CastPredicated) all force
`op == Bypass`, while only the two *real* TensorTensor opcodes (`0x41`/`0x51`) read `op` as a
live `AluOp` selector through `s3s3d3_tt_valid_op`. `[HIGH/OBSERVED — header predicate block]`

> **QUIRK — the back-prop apply primitive is invisible in the wire format.** Because `op` is
> forced to `Bypass`, a reimplementer staring at the 64-byte instruction word cannot see what
> arithmetic BackProp performs — the `op` byte is *zero*. The multiply-subtract-scale chain
> that forms `d_x` is **hard-coded in the handler body** (`@0xb6f0`), keyed off the opcode
> `0x65` alone, not off `op`. This is the opposite of TensorTensor, where the entire kernel
> *is* the `op` selector. `[HIGH/OBSERVED — the `op==Bypass` gate; the hard-coded apply
> INFERRED-HIGH]`

> **GOTCHA — BackProp sources forbid FP32R **and** 64-bit ints; TensorTensor allows them.**
> The sibling `is_valid_tensor_tensor` predicate uses `is_valid_dtype_64(…, AllowU64=true,
> AllowI64=true)` for its operands; `is_valid_batchnorm_backprop` uses plain
> `is_valid_dtype(…, AllowFP32R=false)` — **no** `AllowU64`/`AllowI64`. So `u64`/`i64`, valid
> on the POOL TensorTensor path, are **invalid** for DVE BackProp. The same struct, different
> dtype envelope per opcode. `[HIGH/OBSERVED — the two predicates side by side]`

---

## 5. The backward math — the `d_x` / `d_gamma` / `d_beta` decomposition

### 5.1 The textbook BN-backward (the algebra the firmware realises)

With `N` = the normalization count, `x_norm = (x − μb)·inv_std`, `inv_std = σb^-0.5`:

```
  d_gamma = Σ_batch ( d_y · x_norm )
  d_beta  = Σ_batch ( d_y )
  d_x     = (gamma · inv_std / N) · ( N·d_y − d_beta − x_norm·d_gamma )
```

Both `μb` and `inv_std` are **reused from the forward statistics**, not recomputed on the DVE.

### 5.2 How the Neuron DVE splits it — two opcodes

The Neuron DVE realises this as **two composable opcodes**, not one monolithic kernel:

* **STEP A — the SUMS (the reduce-over-batch): `BatchNormGradAccumulate(2)`** (`0x63`/`0x94`,
  struct `S3S3D1_BN`/`BN2`). The recovered `s3s3d1_bn.h` is verbatim: *"the DVE accumulates
  three separate results required for batch normalization backpropagation … the dst tensor
  must have exactly 3 elements."* It streams `src0 = Om` (the forward ofmap, *"saved from
  forward propagation"*) and `src1 = ∇O'm` (the incoming gradient `d_y`), consumes
  `imm0 = μb` and `imm1 = σb^-0.5`, and writes the **three** accumulated sums that
  `d_gamma = Σ(d_y·x_norm)`, `d_beta = Σ(d_y)`, and the mean-coupling term need. **This is
  where the reduce lives.** `[HIGH/OBSERVED — `s3s3d1_bn.h` verbatim]`

* **STEP B — the APPLY (the per-element `d_x`): `BatchNormBackProp`** (`0x65`, struct
  `S3S3D3_TT`). It streams the per-element `d_y` and `x_norm` and combines them with the three
  GradAccum'd sums and the `(gamma·inv_std/N)` prefactor to emit `d_x` element-wise on the
  shared `AluOp` datapath. `[HIGH dispatch this pass; INFERRED-HIGH the exact term mapping
  across the FLIX-desynced apply body]`

So: **GradAccum = the sums; BackProp = the element-wise apply that consumes them.** The two
compose into the full backward.

### 5.3 The two structs side by side — the SUM step vs the APPLY step

The GradAccum (`S3S3D1_BN`, 64 B) layout is the *other* half of the pair; reproduced from the
recovered `aws_neuron_isa_tpb_s3s3d1_bn.h`:

| off | field | type | GradAccum role |
|---|---|---|---|
| 0–3 | `header` | `HEADER` | opcode `0x63` |
| 4–11 | `events` | `EVENTS` | sync |
| 12–15 | `imm0` | `IMM_VAL_INST_FIELD` | **`μb` (mean) pointer-immediate** |
| 16–31 | `src0_mem_pattern` | `TENSOR3D` | `Om` (forward ofmap) |
| 32–47 | `src1_mem_pattern` | `TENSOR3D` | `∇O'm` (incoming `d_y`) |
| 48–55 | `dst_mem_pattern` | `TENSOR1D` | **exactly 3 elements** (the 3 sums) |
| 56–59 | `imm1` | `IMM_VAL_INST_FIELD` | **`σb^-0.5` (inv_std) pointer-immediate** |
| 60 | `in0_in1_dtype` | `DTYPE_PAIR` | src0/src1 dtypes |
| 61 | `immediate_dtype` | `DTYPE` | imm dtype (`Invalid ⇒ fp32`) |
| 62 | `out_dtype` | `DTYPE` | the sums' dtype |
| 63 | `num_active_channels` | `u8` | 1..128 |

The `S3S3D1_BN` header's `bnga_dst_element_cnt_check` predicate is hard:
`dst.num_elem[0] == 3` — the **3-element** sum vector. `GradAccumulate2` (`S3S3D1_BN2`, op
`0x94`) is the register-immediate variant: it replaces `reserved`/byte-61 with
`immediate_out_dtype` (a `DTYPE_PAIR` packing imm-dtype-lo / out-dtype-hi) and adds
`imm0_imm1_src` at off 62 (an `IMM_SRC_PAIR`: lower-4 bits select imm0's source, upper-4
imm1's — *"pointers coming from registers"*), so the saved stats can arrive **from a register
pointer** rather than an instruction-field pointer. `[HIGH/OBSERVED — `s3s3d1_bn.h` /
`s3s3d1_bn2.h` field-exact]`

The instruction-exact GradAccum / GradAccum2 **body** decode is the
[GradAccum page](batchnorm-gradaccum.md)'s subject; this page states the boundary and uses the
struct/role to pin the reduce↔apply split. `[boundary stated]`

### 5.4 The reuse of the saved statistics — header-verbatim

`aws_neuron_isa_tpb_s3s3d1_bn.h`, the immediate contract (verbatim sense):

> *"This instruction gets two immediates from immediate_pointers in imm0 and imm1: imm0: the
> mini-batch mean (μb), saved from forward propagation. imm1: the reciprocal of the square
> root of the mini-batch variance (σb-0.5), saved from forward propagation. These values come
> from immediate pointers only, and must be in fp32/bf16/fp16 format. … src0: the ofmap values
> (Om), saved from forward propagation. src1: the ofmap gradients (∇O'm) being
> back-propagated."*

So **`μb`, `inv_std = σb^-0.5`, and `Om` are all forward-saved**; BackProp/GradAccum reuse
them, never recompute. The `+ε` is folded into the host's variance **before** the rsqrt — it
is **not** a field anywhere in the BN structs. `N` is staged by `BatchNormParamLoad` (the
[ParamLoad page](batchnorm-paramload.md)) and the `1/N` divide is the same 256-entry
reciprocal Parameter-RAM the [forward page §3](batchnorm-forward.md) documents. `[HIGH/OBSERVED
— `s3s3d1_bn.h` verbatim + `bnga_imm_check`]`

> **CORRECTION — the header spells it `σb-0.5`, not `σb^-0.5`.** The recovered arch-isa comment
> uses plain text `σb-0.5` (no caret); it denotes `(σb²)^(−1/2)` = the reciprocal square root
> of the mini-batch variance. Semantically identical to the conventional `σb^(-0.5)`.
> `[HIGH/OBSERVED — exact header bytes]`

### 5.5 The `d_x` apply, reconstructed as annotated C

The apply combines the per-element streams with the GradAccum'd sums and the prefactor. The
exact instruction schedule sits in the FLIX-desynced body, so the term wiring is INFERRED-HIGH
from the two structs' roles + the standard BN-backward algebra + the surviving IVP vocabulary
(§7):

```c
/* BatchNormBackProp per-element apply  (op 0x65, struct S3S3D3_TT, handler @0xb6f0).
   STEP A (BatchNormGradAccumulate, op 0x63) already wrote the three sums into a 3-elem dst:
        d_beta        = Σ_batch( d_y )                         // sum[0]
        d_gamma       = Σ_batch( d_y · x_norm )                // sum[1]
        (mean-coupling term)                                   // sum[2]
   plus the saved stats imm0=μb, imm1=inv_std, and N (ParamLoad).  All math is fp32-internal. */

float prefactor = gamma * inv_std * recip_RAM[N];   /* (gamma·inv_std)/N  — recip_RAM = 1/N table  */
                                                    /* fp32 scale: ivp_muln_2xf32t (§7)             */
for (each element e of the S3S3D3_TT stream, per active channel) {          /* ivp_lvn_* loads      */
    float d_y    = to_fp32(src1[e]);                /* incoming gradient (cast to fp32)            */
    float x_norm = to_fp32(src0[e]);               /* x_norm == (x−μb)·inv_std, or the saved Om   */
    /*   d_x = prefactor · ( N·d_y − d_beta − x_norm·d_gamma )                                     */
    float t1 = (float)N * d_y;                       /* N·d_y          : ivp_muln_2xf32t            */
    float t2 = t1 - d_beta;                          /* − d_beta       : ivp_subn_2xf32t            */
    float t3 = x_norm * d_gamma;                     /* x_norm·d_gamma : ivp_muln_2xf32t            */
    float t4 = t2 - t3;                              /* − x_norm·d_gamma : ivp_subn_2xf32t          */
    dst[e]   = from_fp32(prefactor * t4, out_dtype); /* · prefactor    : ivp_muln_2xf32t + cast     */
}
/* op@14 is forced AluOp::Bypass (0x00): the multiply/subtract chain is keyed off opcode 0x65,
   NOT off the op field — the apply is hard-wired in the handler body. */
```

`[struct + IVP vocab HIGH/OBSERVED; the per-term op→intrinsic binding INFERRED-HIGH across the
FLIX desync — the apply body is densely-scheduled FLIX VLIW that desyncs under stock objdump]`

### 5.6 The batch reduce — DVE stream-accumulate, NOT a POOL `CrossLaneReduce`

The `Σ_batch` for `d_gamma`/`d_beta` is done **inside GradAccum** on the DVE NX datapath via
the sum-of-products MAC family (`ivp_mulusp*`/`ivp_muluspa*` — multiply-and-sum-into-the-wide-
accumulator, summing the `d_y·x_norm` products for `d_gamma`) and the add-normalize accumulate
(`ivp_baddnormnx16` — the running `Σ d_y` for `d_beta`) — the **same streaming-accumulate
geometry the forward Stats2 uses** for its `sum` / `sum-of-squares` fold. The POOL
`CrossLaneReduce` (opcodes `0x7c`/`0x7d`, with `reduce_op ∈ {ADD, AVG, MAX, OR, AND, XOR}`) is
a **separate** primitive on a **separate** engine — the intra-vector / cross-partition fold —
and is *not* the carrier of BN-backward's batch reduce. `[HIGH IVP vocab OBSERVED §7;
INFERRED-HIGH the GradAccum-internal-reduce attribution — the GradAccum body is FLIX-desynced
so the exact fold schedule is structural]`

---

## 6. The DVE multiply / subtract / accumulate / Newton vocabulary

Harvested across the DVE DEBUG + PERF IRAM `.dis` (`rg -c`, OBSERVED counts this pass; these
are whole-IRAM family counts for the DVE engine, not BackProp-body-specific — the FLIX desync
prevents a body-scoped count):

| IVP op | DEBUG | PERF | role in the backward |
|---|---|---|---|
| `ivp_mulusp2n8xr16` | 5 | 39 | sum-of-products MAC — `d_y·x_norm` fold for `d_gamma` |
| `ivp_muluspn16xr16` | 2 | 31 | sum-of-products MAC (16-wide) |
| `ivp_muluspa2n8xr16` | 4 | 17 | MAC, accumulating form |
| `ivp_muluspan16xr16` | 1 | 10 | MAC, accumulating 16-wide |
| `ivp_mul4t*` | 57 | 195 | 4-term dot MAC (the batch products) |
| `ivp_muln_2xf32t` | 2 | 13 | **fp32 scale multiply** — `gamma·inv_std/N`, `N·d_y`, `x_norm·d_gamma` |
| `ivp_subsnx16` | 3 | 17 | subtract-saturate (term subtraction) |
| `ivp_subn_2x32t` | 3 | 8 | 32-bit subtract |
| `ivp_subn_2xf32t` | 2 | 11 | **fp32 subtract** — `− d_beta`, `− x_norm·d_gamma` |
| `ivp_baddnormnx16` | 8 | 26 | add-normalize accumulate — the running `Σ d_y` |
| `ivp_raddu2nx8t` | 1 | 0 | reduce-add unsigned (a cross-lane fold leg) |
| `ivp_div0nxf16t` | 0 | 2 | fp16 reciprocal/division **SEED** (the `sum÷count` means) |
| `ivp_divnn_2xf32t` | 0 | 1 | fp32 Newton division **refine** |
| `ivp_mulsone*` | 0 | 3 | the fused `(1 − d·x)` Newton helper |
| `ivp_rsqrt0`/`ivp_sqrt0`/`ivp_recip0` | **0** | **0** | *(absent — no rsqrt/recip SEED on DVE)* |

`[HIGH/OBSERVED — counts `rg -c` this pass; the per-term op binding INFERRED-HIGH across the
FLIX desync]`

> **NOTE — the only DVE seed→Newton is reciprocal *division*, for the means, not rsqrt.** The
> `ivp_div0nxf16t → ivp_divnn_2xf32t / ivp_mulsone*` triple is the DVE's reciprocal-division
> Newton step (`x_{n+1} = x_n·(2 − d·x_n)`, expressed with the `mulsone` "one-minus" helper),
> used for `sum ÷ count` when no reciprocal-RAM entry applies — it is **not** `rsqrt`.
> `ivp_rsqrt0`/`sqrt0`/`recip0` grep `0/0` in both builds confirms `σb^-0.5` is
> forward/host-precomputed, never a DVE op. `[HIGH/OBSERVED — grep 0/0 + the
> [forward page §4](batchnorm-forward.md) Newton decode, CARRIED]`

---

## 7. Why the `S3S3D3_TT` is shared but the apply is on the DVE `AluOp` path

The shared `AluOp` / `alu_op.cpp` elementwise-execute path is resident in the DVE DRAM and is
what the thin BackProp body delegates to. The `alu_op.cpp` assertion / log strings are read
directly from `DVE_DEBUG_DRAM.bin` this pass:

```
 0x2b93  /opt/workspace/NeuronUcode/src/decode/alu_op.cpp:262 0
 0x2bca  /opt/workspace/NeuronUcode/src/decode/alu_op.cpp:196 0 && "not supported op"
 0x2c17  /opt/workspace/NeuronUcode/src/decode/alu_op.cpp:141 0 && "not supported op"
 0x2c64  /opt/workspace/NeuronUcode/src/decode/alu_op.cpp:231 0 && "not supported dtype"
 0x2cb4  /opt/workspace/NeuronUcode/src/decode/alu_op.cpp:220 0 && "not supported op"
 0x2d01  S: AluOp
```

Alongside the family's own self-names — `0x2060 "S: Tensor-Tensor"`, `0x2900
"S: CopyPredicated"`, `0x2913 "S: CastPredicated"` — these prove the whole `S3S3D3_TT` family
(including BackProp) runs the **same `AluOp` elementwise machinery on the DVE**, while the
POOL engine's `pool_tensor_tensor_arith_op` (`0x41`) is a *separate* implementation of the
same wire format on the POOL GPSIMD core. The two never collide: `0x65` is only in the DVE
table, `0x41` is decoded by each engine against its own table. For BackProp the `op` byte is
`Bypass`, so the `alu_op.cpp` op-selector is *not* the carrier of the `d_x` arithmetic — the
multiply-subtract-scale chain is the opcode-`0x65` body's own. `[HIGH/OBSERVED — `alu_op.cpp`
strings + the S3S3D3_TT family self-names; INFERRED-HIGH the BackProp-delegates-to-AluOp-for-
the-elementwise-load/store-shell, op-keyed compute hard-coded]`

---

## 8. The dtype matrix

`NEURON_ISA_TPB_DTYPE` (4-bit, read from `common.h` this pass): `INVALID 0x0`, `UINT64 0x1`,
`INT8 0x2`, `UINT8 0x3`, `INT16 0x4`, `UINT16 0x5`, `BFLOAT16 0x6`, `FP16 0x7`, `INT32 0x8`,
`UINT32 0x9`, `FP32 0xA`, `FP32R 0xB`, `INT64 0xC`, `FP8_EXP3 0xD`, `FP8_EXP4 0xE`,
`FP8_EXP5 0xF`.

| stage | dtypes | notes (predicate) |
|---|---|---|
| **BackProp src0/src1** (`in0_in1_dtype@12`) | fp16/bf16/fp32 | `is_valid_dtype(…, AllowFP32R=false)`; **no** U64/I64 (unlike TensorTensor); cast to fp32 |
| **BackProp out** (`out_dtype@13`) | fp32/fp16/bf16 | `is_valid_dtype(…, AllowFP32R=true)` |
| **BackProp op** (`op@14`) | `AluOp::Bypass` only | `s3s3d3_tt_is_zero_op` — forced `0x00` |
| **COMPUTE** | fp32 throughout | inputs cast to fp32 first (like forward Stats2) |
| **GradAccum src0/src1** (`S3S3D1_BN`) | fp16/bf16/fp32/**i32** | `bnga_valid_input_type` — `i32` also allowed here |
| **GradAccum imm** (`μb`/`inv_std`) | fp16/bf16/fp32; `Invalid ⇒ fp32` | `bnga_valid_immediate_type` |
| **GradAccum out** | fp32/fp16/bf16; dst == 3 elems | `bnga_valid_output_type` + `bnga_dst_element_cnt_check` |

`num_active_channels` is capped at `POOLING_NUM_CHANNELS = 128` (`s3s3d1_bn_channels`:
`!= 0 && <= 128`). `[HIGH/OBSERVED — `common.h` DTYPE enum + the two structs' validity
predicates, read this pass]` See the [unified dtype model](dtype-model.md) for the full code
space.

---

## 9. Per-generation presence

The two BackProp-relevant structs (`s3s3d3_tt`, `s3s3d1_bn` + `s3s3d1_bn2`) and the BN opcode
enum are present byte-for-byte across the arch-isa header trees, `fd`/`rg`-verified this pass:
`s3s3d3_tt.h` + `s3s3d1_bn.h` exist and `BATCH_NORM_BACK_PROP = 0x65` (marked `// Y` =
active) + the `s3s3d3_tt_is_zero_op` predicate are present in **sunda / cayman / mariana /
maverick** (`maverick` header self-labels `ISA header for NC-v5`). `mariana_plus` ships no
separate `neuron_mariana_plus_arch_isa` dir in this checkout — it shares the `mariana` headers.

| GEN | `s3s3d3_tt` + `s3s3d1_bn` headers | opcode `0x65` (`// Y`) | DVE BackProp dispatch/handler |
|---|---|---|---|
| **SUNDA** (v2) | present (NC-v? header) | present | not separately carved/diffed this pass |
| **CAYMAN** (v3) | present — **the carve substrate** | present | **OBSERVED** (`0x65 → 0x30ae → 0x1fe4 → 0xb6f0` byte-exact) |
| **MARIANA** (v4) | present | present | structurally identical SEQ NX engine |
| **MARIANA_PLUS** (v4+) | shares MARIANA headers | present | structurally identical SEQ NX engine |
| **MAVERICK** (v5) | present (NC-v5 header) | present | **header-OBSERVED → interior INFERRED** |

The DVE firmware is the same `cayman/seq` NX SEQ engine across the Trainium-class generations
that ship the DVE engine; the **POOL engine carries no batch-norm opcode (`0x60`–`0x66`) in
any carved generation** (CAYMAN `kernel_info_table` + SUNDA opcode map, §2.2). `[HIGH/OBSERVED
— per-gen header presence (fd-verified) + the CAYMAN DVE dispatch decoded this pass; the
non-CAYMAN DVE BackProp handler-body byte-equality not exhaustively diffed → INFERRED]`

> **MAVERICK (v5) interior — header-OBSERVED only → INFERRED.** Per the
> [generation-grounding policy](../../reference/confidence-model.md), the v5 BackProp
> struct/opcode/predicate *presence* is OBSERVED (its `aws_neuron_isa_tpb_s3s3d3_tt.h` carries
> `NC-v5` and the `0x65` opcode + `is_zero_op` gate), but the v5 DVE handler **interior** is
> reasoned from the CAYMAN carve plus the header equality, **not** separately carved and
> decoded here. Treat v5 BackProp's `entry`-width, trampoline VA, and apply schedule as
> **INFERRED** until a v5 DVE image is carved and diffed. `[HIGH/OBSERVED header; MED/INFERRED
> interior]`

---

## 10. Confidence ledger

**HIGH / OBSERVED (disassembly, byte read, or header read this pass):**

* The DVE carves reproduce the forward page's anchor byte-identically (sha `259769ff` /
  `c106642d` / `9fa066f4` / `eb980f98`); reset vector `06 76 00 00` (`j 0x1dc`).
* `S: BatchNormalizeBackProp` @ DRAM `0x2350` (full 26 bytes read).
* The dispatch chain byte-exact: `0x65 → DRAM table1[0x8a4] = 0x30ae` (xxd `ae 30 00 00`) →
  trampoline `0x30ae` (`call8 0x1fe4 ; j 0x3212`, bytes `65 f3 fe / 46 57 00`) → thunk `0x1fe4`
  (`entry a1,48 ; const16 a2,0xb6f0 ; s32i a2,a1,12 ; call8 0x951c`) → body `0xb6f0`
  (`entry a1,32 ; const16 a10,0x2350 ; call8 0x18010`); common Handler invoke `0x3212`.
* The five-sibling thunk→body map (GradAccum `0x1fac→0xb518`, GradAccum2 `0x1fc8→0xb55c`,
  BackProp `0x1fe4→0xb6f0`, ParamLoad `0x2000→0xb62c`, ParamLoad2 `0x201c→0xb694`) + the
  `entry` frame widths (Stats2 `a1,96` fat vs BackProp `a1,32` thinnest).
* BackProp's trampoline + handler are DISTINCT from TensorTensorArith (`0x41 → 0x31e6`).
* The `S3S3D3_TT` struct field-exact (`ISA_STATIC_ASSERT == 64`); the `instruction_mapping.json`
  binding `S3S3D3_TT → {…, BATCH_NORM_BACK_PROP}` (`jq`).
* The `is_valid_batchnorm_backprop` predicate, including `s3s3d3_tt_is_zero_op`
  (`op == AluOp::Bypass = 0x00`) and the FP32R-false / no-U64-I64 source-dtype envelope.
* The GradAccum `S3S3D1_BN` field-exact: `imm0=μb` @12, `imm1=σb^-0.5` @56, `dst == 3 elems`
  (`bnga_dst_element_cnt_check`), *"saved from forward propagation"* verbatim; the
  `S3S3D1_BN2` register-immediate variant (`imm0_imm1_src@62`, `immediate_out_dtype@61`).
* No `ivp_rsqrt0`/`sqrt0`/`recip0` in the DVE (grep `0/0` both builds); the
  `ivp_div0nxf16t`/`divnn_2xf32t`/`mulsone*` reciprocal-division Newton triple (PERF-only) +
  the MAC/`muln_2xf32t`/`subn_2xf32t`/`baddnormnx16` vocabulary (counts this pass).
* The `alu_op.cpp` assert strings (DRAM `0x2b93`/`0x2bca`/`0x2c17`/`0x2c64`/`0x2cb4`,
  `S: AluOp` @`0x2d01`) + the S3S3D3_TT family self-names.
* POOL `kernel_info_table` (CAYMAN, VMA `0x02000380`, file `0x7400`, 17 records) has **no**
  `0x65` / `0x60`–`0x66` (verified across CAYMAN_0/_3 + MARIANA_3 this pass); POOL has no
  `BatchNorm` string; `0x41` is its sole TT-arith entry. SUNDA POOL map (no `0x65`) CARRIED.
* `DTYPE` enum + `POOLING_NUM_CHANNELS = 128` read from `common.h`.
* Per-gen header presence (s3s3d3_tt + s3s3d1_bn + `0x65` + is_zero_op) in
  sunda/cayman/mariana/maverick; maverick = `NC-v5`.

**MED / INFERRED:**

* The BackProp apply body's exact term-by-term wiring
  `d_x = (gamma·inv_std/N)·(N·d_y − d_beta − x_norm·d_gamma)` — the body is FLIX-desynced past
  `0xb70e`; the decomposition is INFERRED-HIGH from the two structs' roles + the standard
  BN-backward algebra + the surviving IVP mul/sub/scale vocabulary.
* The "GradAccum does the `Σ_batch` internally (DVE stream-accumulate, not POOL CrossLaneReduce)"
  attribution (IVP MAC/`baddnorm` vocabulary OBSERVED; the exact fold schedule FLIX-desynced).
* The "BackProp delegates the elementwise load/store shell to AluOp; the op-keyed compute is
  hard-coded" framing (thin frame + struct binding + alu_op.cpp asserts OBSERVED).
* `x_norm` vs `Om` identity (the forward-pass contract; INFERRED-HIGH from the `S3S3D1_BN`
  `src0=Om` header note).
* Non-CAYMAN DVE BackProp handler-body byte-equality → MAVERICK interior INFERRED.

**LOW / WALL:**

* The PERF-build BackProp handler body (`0x65 → 0x9090` overlaps the PERF-DRAM assertion pool;
  the DEBUG dispatch is the trusted substrate).
* The byte-13 dtype read at `0xb78b`/`0xb796` belongs to the **adjacent MOVE handler**
  (`move.cpp:41` assertion), not BackProp — the struct's `out_dtype@13` is HIGH from the
  header regardless.

---

## See also

* [BatchNormalize — Forward Statistics](batchnorm-forward.md) — Stats2/Aggregate, the
  256-reciprocal Parameter-RAM, and the proof that `rsqrt(var+ε)` is forward/host-precomputed.
* [BatchNormalize — GradAccum](batchnorm-gradaccum.md) — the SUM step (`0x63`/`0x94`,
  `S3S3D1_BN`/`BN2`): the three-element batch reduce BackProp consumes.
* [BatchNormalize — ParamLoad](batchnorm-paramload.md) — staging `N` / `batch_mean` into the
  per-lane datapath flops.
* [Tensor-Tensor arith](tensor-tensor.md) — the sibling that reads the *same* 64-byte
  `S3S3D3_TT` wire format with `op` as a live `AluOp` selector.
* [The unified datatype model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` code space.
* [Cross-lane reduce](cross-lane-reduce.md) — the POOL `0x7c`/`0x7d` fold that BN-backward's
  batch reduce is *not*.
* [The Confidence & Walls model](../../reference/confidence-model.md) — what the tags mean.
