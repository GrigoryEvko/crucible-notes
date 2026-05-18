# tcgen05.mma Walkthrough

## Abstract

A single Blackwell `tcgen05.mma` — the asynchronous warp-group matrix multiply that consumes a TMEM accumulator and writes its result back into the same TMEM region — touches every layer of the tileiras cascade. It begins as a tile-shaped `cuda_tile.mmaf`, picks up an `sm100.umma` copy/MMA atom witness in `nv_tileaa`, materializes a TMEM-handle SSA value plus an SMEM-to-TMEM staging copy in `nv_tileas`, expands into the `nvvm.tcgen05.mma.cta_group::1` intrinsic in LLVM IR, becomes a `TCGEN05_MMA_*` machine instruction with packed control word and collector word in NVPTX MIR, and surfaces as `tcgen05.mma.cta_group::1.kind::f16` in PTX text. The 9-bit kind word — `cta_group`, `scale_vec_size`, `scale_input_acc`, `block_scale`, and `mma_kind` — flows through each layer under a different name, and the TMEM-handle SSA value persists from allocation through dealloc with verifier-enforced dominance.

This page traces one MMA end-to-end on `sm_100a` (Blackwell B200 / GB200 datacenter). The kernel-wide walkthrough in [DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) shows the same kind of trace at every stage for an `sm_90a` WGMMA GEMM; this page is its `sm_100a` companion. The TMA-load focused walkthrough in [TMA Load Walkthrough](tma-load-walkthrough.md) traces the producer side of the same pipeline; this page traces the consumer side that reads what TMA staged. Cross-reference targets remain the per-stage canonical pages: [cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md), [nv_tileaa to nv_tileas](../lowering/tileaa-to-tileas.md), [nv_tileas to LLVM](../lowering/tileas-to-llvm.md), [tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md), [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier), and [tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md).

Confidence: HIGH for IR shapes, mnemonic spellings, kind-word bit layout, and the verifier rule order; MED for the exact SSA naming used in the worked example (the binary-derived examples in the source pages use slightly different temp names).

## The Operation

The walkthrough operation is one warp-group dense `tcgen05.mma` of a `64 × 128 × 16` BF16 tile with FP32 accumulator, on `sm_100a`, `cta_group::1` (single-CTA dispatch), no block-scale, no sparsity, no weight-stationary mode, `collector::a::fill` for an initial accumulation. The kernel consumes one TMEM accumulator region for `D` (64 rows × 128 columns of FP32 = 32 KiB = 16 TMEM columns out of the 256 the SM owns), one SMEM-resident A operand staged through TMA, and one SMEM-resident B operand likewise staged. The accumulator stays in TMEM across the K loop — no register-fragment accumulator, no `mma.sync` style register fan-out.

The frontend constructed:

```python
a_tile = load(a_view, (block_m, block_k))         # tile<64x16xbf16>
b_tile = load(b_view, (block_n, block_k))         # tile<16x128xbf16>
acc    = mmaf(a_tile, b_tile, acc)                # tile<64x128xf32>
```

The MMA itself does not specify TMEM, the kind word, the collector mode, or the CTA group selector. Those decisions are downstream — the same `mmaf` op on `sm_90a` becomes a WGMMA with a register-resident accumulator, and on `sm_80` becomes a series of `mma.sync` instructions. The capability cross-check in [Matmul Progression by SM — SM100 / SM103](matmul-progression-by-sm.md#sm100--sm103-tensor-memory-and-tcgen05mma) covers the divergent lowering paths.

The 9-bit kind word that this MMA encodes:

```
mma_kind         = f16   (value 3)   bits 6..8
block_scale      = 0                  bit 5
scale_input_acc  = 0                  bit 4
scale_vec_size   = 0     (1X, implicit) bits 2..3
cta_group        = 1     (1-CTA)     bits 0..1
ws bit overlay   = 0     (no weight-stationary)
                                     ─────────
                              raw   = 0b011000001 = 0xC1
```

That single integer — the encoded kind word — is what the LLVM intrinsic carries as its first immediate operand, what the MIR opcode encodes in its packed control word, and what `ptxas` decodes from the printed `.cta_group::1.kind::f16` modifier set.

## Stage 1: cuda_tile IR

The first IR the compiler sees comes out of the frontend's bytecode. The MMA is a `cuda_tile.mmaf` — token-free MMA over three tile-typed SSA values — and the verifier contract on the operation is the standard cuda_tile contract: power-of-two tile dimensions, a 16-million-element ceiling, conforming `M × K` / `K × N` / `M × N` shapes between A / B / C, and an optional `fastmath` attribute that records the precision-relaxation budget the lowering may exploit.

```mlir
%a_tile : !cuda_tile.tile<64x16xbf16>
%b_tile : !cuda_tile.tile<16x128xbf16>
%acc_in : !cuda_tile.tile<64x128xf32>

%acc_out = cuda_tile.mmaf %a_tile, %b_tile, %acc_in
         { fastmath = "contract" }
         : !cuda_tile.tile<64x16xbf16>,
           !cuda_tile.tile<16x128xbf16>,
           !cuda_tile.tile<64x128xf32>
```

There is no TMEM, no kind word, no CTA-group selector, no collector mode, and no `tcgen05` mention. `cuda_tile` is the public surface and deliberately stays target-agnostic: three tile-typed SSA values plus an optional fast-math hint is all the frontend has to publish. The atom selection — dense vs sparse, block-scaled vs plain, single-CTA vs two-CTA, weight-stationary vs streamed — is downstream of the layout-assignment pre-pass that runs between Stage 1 and Stage 2.

> ⚡ **QUIRK — `cuda_tile.mmaf` carries no TMEM, no kind word, no CTA group, no collector**
> The public dialect has no syntax for a TMEM handle, no syntax for the 9-bit kind word, no `cta_group::*` selector, and no `collector::a::*` modifier. Every `tcgen05`-specific noun first appears in `nv_tileaa` (the `sm100.umma` atom witness) or `nv_tileas` (the TMEM-handle SSA, the staging copy, the kind word as a packed attribute). A reimplementer who tries to express any of those on the public surface has misread the contract — `cuda_tile.mmaf` is a tile-algebra op, not a tensor-memory-shaped op. The promotion to `tcgen05.mma` is a downstream decision driven by the copy/MMA atom registry, not a frontend gesture.

## Stage 2: nv_tileaa IR

`ConvertCudaTileToTileAA` rewrites the MMA through the three-populator structure documented in [cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md). Part C of that structure owns MMA and reductions, so the rewrite for this op lives in the Part-C `nv_tileaa.dot` pattern. The tile types become MLIR `tensor<...>`, the result name flips from `mmaf` to `dot`, and — the key change for this walkthrough — the op picks up an MMA atom witness. The witness is an attribute that names the hardware MMA primitive selected by the layout-assignment pre-pass; for an `sm_100a` BF16 input with FP32 accumulator the witness family is `cute_nvgpu.arch.mma.SM100.umma` ("Unified MMA," the dialect-side name for the `tcgen05.mma` family).

```mlir
%acc_out = nv_tileaa.dot %a_tile, %b_tile, %acc_in
         { atom         = #cute.mma_atom<sm100_umma_m64n128k16_f32_bf16_bf16>,
           input_precision = "bf16",
           fastmath     = "contract" }
         : tensor<64x16xbf16>, tensor<16x128xbf16>, tensor<64x128xf32>
              -> tensor<64x128xf32>
```

The MMA atom witness `sm100_umma_m64n128k16_f32_bf16_bf16` names the `(M, N, K) = (64, 128, 16)` `tcgen05.mma` shape for BF16 inputs with FP32 accumulator. A different witness in the same slot — `sm100_umma_m64n128k16_f32_bf16_bf16_sp` for the 2:4-sparse variant, `sm100_umma_m64n128k16_f32_mxf8f6f4_mxf8f6f4_bs` for the block-scaled FP8 variant, `sm90_wgmma_m64n128k16_f32_bf16_bf16` for the Hopper fallback — would steer the next stage's rewrite into a different lowering path. Layout assignment runs before this pass and is what consults the [MMA Atoms SM70-SM120 — SM100 UMMA Layout Grammar](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) registry; after this pass the witness travels verbatim down to the LLVM lowering.

Three things are *not* yet visible at this stage. The accumulator residency is still implicit in the operand types (a plain `tensor<64x128xf32>` makes no commitment to register, SMEM, or TMEM placement). The CTA-group selector is implicit in the atom name (no `_2cta` suffix means single-CTA dispatch). And the kind-word bits are still derived: `mma_kind = f16`, `block_scale = 0`, `scale_vec_size = 0`, `cta_group = 1` all flow from the atom's element types and the absence of any per-op modifier attribute. The packing into a single 9-bit word happens at Stage 3.

## Stage 3: nv_tileas IR

`ConvertTileAAToTileAS` keeps the same operand shape but renames the op, updates the dialect namespace, and — for the SM100 path — splits the single `dot` op into a four-instruction sequence: TMEM allocation, SMEM-to-TMEM staging copy for the A operand (if A is TMEM-resident in the selected atom), MMA proper, and TMEM read-back at use sites. The [TileAS layout and buffer family](../passes/tileas/layout-and-buffer-family.md) and [TileAS scheduling glue](../passes/tileas/scheduling-glue.md) drive the split; the [tcgen05 Tensor Memory Model — Allocation Grain and Lifetime](tcgen05-tensor-memory-model.md#allocation-grain-and-lifetime) page documents the TMEM allocator contract this stage materialises.

```mlir
// ---- TMEM allocation, hoisted to function entry
%tmem_d = nv_tileas.alloc_tmem { num_columns = 16 : i32 }
        : !nv_tileas.tmem<64x128xf32>

// ---- SMEM-resident B descriptor (built once per K iteration, see TMA Load Walkthrough)
%b_desc = nv_tileas.make_umma_smem_desc %smem_b,
            layout = #cute_nvgpu.umma_k_layout<base_offset=0, lbo=128, sbo=2048,
                                                swizzle=128B>
        : !nv_tileas.umma_smem_desc<16x128xbf16>

// ---- SMEM-resident A descriptor (A operand for this walkthrough; the kernel
//      could equally stage A into TMEM via tcgen05.cp.smem.tmem and use the
//      TMEM-resident A path)
%a_desc = nv_tileas.make_umma_smem_desc %smem_a,
            layout = #cute_nvgpu.umma_mn_layout<base_offset=0, lbo=16, sbo=512,
                                                swizzle=128B>
        : !nv_tileas.umma_smem_desc<64x16xbf16>

// ---- tcgen05.mma with packed kind word; D is the TMEM accumulator
%tok_mma = nv_tileas.umma %tmem_d, %a_desc, %b_desc
           { kind            = #nvvm.tcgen05_mma_kind<f16>,
             cta_group       = #nvvm.tcgen05_group<cta1>,
             scale_vec_size  = #nvvm.tcgen05_mma_scale_vec<1X>,
             scale_input_acc = false,
             block_scale     = false,
             collector_a     = #nvvm.tcgen05_mma_collectorop<fill>,
             ashift          = false,
             atom            = #cute.mma_atom<sm100_umma_m64n128k16_f32_bf16_bf16> }
         : !nv_tileas.tmem<64x128xf32>,
           !nv_tileas.umma_smem_desc<64x16xbf16>,
           !nv_tileas.umma_smem_desc<16x128xbf16>
         -> !nv_tileas.async_token

// ---- TMEM read-back when the accumulator is needed by the epilogue
%acc_reg = nv_tileas.tmem_load %tmem_d
         { shape = #nvvm.tcgen05_ldst_shape<m64n8x32b> }
         : !nv_tileas.tmem<64x128xf32> -> tensor<64x128xf32>

// ---- TMEM deallocation, sunk to function exit
nv_tileas.dealloc_tmem %tmem_d : !nv_tileas.tmem<64x128xf32>
```

Five new entities appear at this stage. First, the **TMEM handle** `%tmem_d` is a first-class SSA value of opaque dialect type `!nv_tileas.tmem<64x128xf32>` — its 32-bit handle encodes `base_column` and `num_columns` (the layout the [tcgen05 Tensor Memory Model — Allocation Grain and Lifetime](tcgen05-tensor-memory-model.md#allocation-grain-and-lifetime) page documents). Second, the **UMMA SMEM descriptor** `%a_desc` / `%b_desc` is the same 64-bit packing the WGMMA descriptor uses on `sm_90a` (documented in [WGMMA Emission Protocol — SMEM Descriptor Bit Layout](wgmma-emission-protocol.md#smem-descriptor-bit-layout)) — `tcgen05.mma` reuses the bit format verbatim. Third, the **kind word** is now an explicit packed attribute carrying the five orthogonal fields the verifier inspects. Fourth, the **collector mode** is exposed as a separate attribute (the [tcgen05 Tensor Memory Model — Control Word Layout](tcgen05-tensor-memory-model.md#control-word-layout) collector word). Fifth, **TMEM read-back** is a separate op (`cute_nvgpu.atom.tmem_load`) that the epilogue or the next MMA must emit — the accumulator does not become a register-resident SSA value through the MMA itself, it stays in TMEM until explicitly read.

> ⚡ **QUIRK — TMEM-handle SSA propagation requires dominance over every consumer**
> The TMEM handle `%tmem_d` produced by `alloc_tmem` is an SSA value, but it does *not* behave like a value-typed accumulator. It is a *handle* to a per-SM TMEM region whose contents the MMA mutates in-place. The verifier on `cute_nvgpu.arch.sm100.dealloc_tmem` requires that every `umma`, `tmem_load`, and `tmem_store` op that names `%tmem_d` be dominated by the matching `alloc_tmem` and dominate the matching `dealloc_tmem`. A reimplementation that hoists the MMA op above the alloc, sinks it below the dealloc, or — most subtly — places the alloc inside a conditional branch the MMA escapes, builds IR that passes the dialect verifier but produces a kernel where the MMA reads garbage from TMEM rows the allocator has already returned to the free pool. The dominance contract is the only protection: TMEM regions do not survive the SM context reset between CTAs, so any out-of-lifetime read sees whatever the next CTA-on-this-SM wrote.

> ⚡ **QUIRK — `cta_group::1` and `cta_group::2` encode in the same kind-word bits but reject different operand shapes**
> The CTA-group selector lives in bits 0..1 of the kind word: `cta_group = 1` (binary `01`) selects single-CTA dispatch, `cta_group = 2` (binary `10` — *not* `3`, the historical 4-CTA reservation) selects two-CTA cooperative dispatch. Verifier rule 8 in [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) rejects `cta_group::2` whenever the weight-stationary bit is set, with the diagnostic `"cta_group::2 is not supported with weight stationary"`. The 4-CTA value `3` exists in the encoding range but has no MMA-side variant — the 4-CTA semantics is a copy-time partition on `tcgen05.cp` only; the consuming MMA over each partition is a plain single-CTA instruction. A reimplementation that emits `cta_group::3` for a 4-CTA dispatch builds an opcode the verifier rejects with a different rule from the documented ladder.

> ⚡ **QUIRK — `scale_vec_size` bit-packing is per-`mma_kind`, with verifier rules 11/12/13 fencing each off**
> The `scale_vec_size` field at bits 2..3 of the kind word is a 2-bit selector (`0 = 1X (16-element)`, `1 = 2X (32-element)`, `2 = 4X (64-element)`, `3 = reserved`). The verifier ladder rules 11, 12, and 13 (see [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier)) each pin a single `mma_kind` to a specific subset of legal `scale_vec_size` values: `mxf8f6f4` only accepts `1X` (rule 11 rejects `2X` and `4X`), `mxf4nvf4` only accepts `2X` or `4X` (rule 12 rejects `1X`), and `mxf4` only accepts `2X` (rule 13 rejects `1X` and `4X`). Outside the arch-conditional surface, rule 3 globally forbids any non-zero `scale_vec_size`. For this walkthrough's `kind::f16` MMA, `scale_vec_size = 0` is the only legal value — block-scale is off, so the field is unused and the verifier doesn't fire on it, but a frontend that sets a non-zero value on a non-block-scale kind passes the dialect verifier and is silently miscompiled at PTX emission time.

### TMEM Allocation Lifecycle

The `cute_nvgpu.arch.sm100.alloc_tmem` op carves a 32 KiB region (16 columns of the SM's 256 TMEM columns) out of the per-SM TMEM allocator's free pool. The [Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) pass is what decides the `base_column` value the handle encodes; for this walkthrough's accumulator the allocator picks column 0 (no other TMEM users) and the handle becomes `{base_column = 0, num_columns = 16}`. The allocator is per-SM, not per-CTA: every warp in every resident CTA sees the same TMEM address space, but each region is pinned to one logical owner for its issue lifetime.

The lifecycle has three named operations:

| Op | Role | Verifier requirement |
|---|---|---|
| `cute_nvgpu.arch.sm100.alloc_tmem` | Reserves `num_columns` of TMEM, returns a handle | Must dominate every consumer that names the handle |
| `cute_nvgpu.arch.mma.SM100.umma` | Mutates the TMEM region in place; reads C, writes D | Handle operand must come from a dominating `alloc_tmem` |
| `cute_nvgpu.atom.tmem_load` / `cute_nvgpu.atom.tmem_store` | Reads / writes TMEM into / out of register tensors | Same dominance requirement |
| `cute_nvgpu.arch.sm100.dealloc_tmem` | Returns the columns to the free pool | Must post-dominate every consumer |

The allocator does not allow re-allocating a region across function boundaries — there is no global TMEM heap and no out-of-function handle propagation. A kernel that wants to chain MMAs across iterations keeps the TMEM allocation alive across the loop body, with `alloc_tmem` hoisted to function entry and `dealloc_tmem` sunk to function exit. The [tcgen05 Tensor Memory Model — Allocation Grain and Lifetime](tcgen05-tensor-memory-model.md#allocation-grain-and-lifetime) page covers the full grain and lifetime model.

### SMEM-to-TMEM Staging Copy (Optional A Path)

The walkthrough above uses A as an SMEM descriptor — the simpler residency. The TMEM-resident A path uses a `tcgen05.cp.smem.tmem` staging copy to move the A operand into a TMEM region before the MMA reads it. The staging form looks like:

```mlir
%tmem_a = nv_tileas.alloc_tmem { num_columns = 8 : i32 }
        : !nv_tileas.tmem<64x16xbf16>

nv_tileas.umma_smem_to_tmem_cp %smem_a, %tmem_a
    { shape = #nvvm.tcgen05_cp_shape<m64n128b>,
      multicast = #nvvm.tcgen05_cp_multicast<warpx2_01_23>,
      src_fmt = #nvvm.tcgen05_cp_src_fmt<b32x2> }
  : !nv_tileas.smem<64x16xbf16>, !nv_tileas.tmem<64x16xbf16>
```

The `tcgen05.cp` family supports shape codes `m64n128b`, `m64n256b`, `m32x128b`, and `m64x128b`, each pairing with a different multicast mask. The 4-CTA copy variant uses `multicast = warpx4` against shape `m32x128b`; the 2-CTA variants use `warpx2_01_23` or `warpx2_02_13` against shape `m64x128b`. The verifier strings `"Shape 64x128b requires multicast warpx2_01_23 or warpx2_02_13 for tcgen05.cp Op"` and `"Shape 32x128b requires multicast warpx4 for tcgen05.cp Op"` enforce the pairing. This walkthrough sticks with the SMEM-descriptor A path to keep the trace focused on the MMA itself; the [Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) page covers the cluster-side copy patterns in detail.

> ⚡ **QUIRK — `ashift` is rejected with block-scale and with `collector::a::use`/`fill`**
> The `ashift` modifier on the collector word advances the A operand's column index by one before the MMA reads it — a single-instruction prefetch-like optimisation for inner loops that walk A's columns in lockstep with K. Verifier rule 7 in [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) rejects `ashift` whenever `block_scale = 1` (the diagnostic is `"ashift is not supported with tcgen05.mma.block_scale variants"`). Verifier rule 10 rejects `ashift` whenever `collector::a::use` or `collector::a::fill` is set (the diagnostic is `"Cannot use collector::a::use or colletor::a::fill with ashift"` — with the verbatim `colletor` typo preserved). The conjunction is a single bit position in the encoding: bit 2 of the collector word overlays both the `ashift` flag and the high bit of the `collector_a` field, so the encoder treats them as mutually exclusive at the byte level. A reimplementation that emits both flags simultaneously builds a collector word with ambiguous semantics and the verifier rejects it at the first failure encountered.

## Stage 4: NVVM Intrinsic in LLVM IR

`ConvertTileASToLLVM` is the terminal MLIR-side lowering, and its nine-phase body conversion documented in [tileas to LLVM](../lowering/tileas-to-llvm.md) carries the MMA to LLVM. The TMEM allocation lowers to `nvvm.tcgen05.alloc`, the SMEM-to-TMEM staging copy (if present) lowers to `nvvm.tcgen05.cp`, the MMA proper lowers to `nvvm.tcgen05.mma.cta_group::1`, and the read-back lowers to `nvvm.tcgen05.ld`. The kind word collapses into a packed `i32` immediate carrying the same five fields the dialect attribute exposed.

```llvm
; ---- TMEM allocation (one column-base handle per accumulator region)
%tmem_d_handle = call i32 @llvm.nvvm.tcgen05.alloc.shared(
    i32 16)                              ; num_columns

; ---- (optional) SMEM-to-TMEM staging copy for the A operand
;      skipped in this walkthrough — A rides the SMEM-descriptor path

; ---- UMMA SMEM descriptor encode (64-bit packed, same bit layout as WGMMA)
%a_desc = call i64 @llvm.nvvm.tcgen05.mma_smem_desc.encode(
    ptr addrspace(3) %smem_a, i32 512, i32 16, i32 0, i32 1)
%b_desc = call i64 @llvm.nvvm.tcgen05.mma_smem_desc.encode(
    ptr addrspace(3) %smem_b, i32 2048, i32 128, i32 0, i32 1)

; ---- tcgen05.mma with packed kind word
;      i32 kind:     0xC1 = mma_kind::f16 + cta_group::1
;      i32 collector: 0x01 = collector::a::fill, ashift=0
call void @llvm.nvvm.tcgen05.mma.cta_group__1(
    i32 %tmem_d_handle,                  ; D (TMEM handle, also reads C in place)
    i64 %a_desc,                         ; A operand (SMEM descriptor)
    i64 %b_desc,                         ; B operand (SMEM descriptor)
    i32 193,                             ; kind word: 0xC1
    i32 1,                               ; collector word: collector::a::fill
    i1 true)                             ; enable_input_d (analogue of WGMMA scale_d)

; ---- TMEM read-back when the epilogue needs the accumulator in registers
%acc_reg = call <128 x float> @llvm.nvvm.tcgen05.ld.m64n8x32b(
    i32 %tmem_d_handle)

; ---- TMEM deallocation (at function exit)
call void @llvm.nvvm.tcgen05.dealloc(i32 %tmem_d_handle, i32 16)
call void @llvm.nvvm.tcgen05.relinquish_alloc_permit()
```

Five things change at the LLVM boundary. The MMA atom witness is consumed — the intrinsic name `llvm.nvvm.tcgen05.mma.cta_group__1` encodes the CTA-group selector and the variant family (`cta_group__2` for the two-CTA form, the `sp` / `block_scale` / `ws` family suffixes for the sparse / block-scaled / weight-stationary variants), so no attribute is needed at the LLVM op level. The kind word becomes a single `i32` immediate (193 = `0xC1` for our walkthrough, with `mma_kind::f16 + cta_group::1`). The collector word is a separate `i32` operand carrying the collector-A mode and the ashift bit. The TMEM handle is an `i32` SSA value threaded through every consumer. And the SMEM descriptors are `i64` SSA values produced by `llvm.nvvm.tcgen05.mma_smem_desc.encode`, packing the same 64-bit bit field that WGMMA uses on Hopper — the encoding is genuinely shared between the two MMA families.

The MMA does *not* automatically wait for itself. The producer-side instruction is asynchronous; the consumer-side `nvvm.tcgen05.wait` (lowering of `tcgen05.wait.cta_group::1`) is what drains the MMA before the accumulator is read. The two are independent instructions tied together by the TMEM handle:

```llvm
; ---- After the K loop body completes, drain the asynchronous MMA queue
call void @llvm.nvvm.tcgen05.wait.cta_group__1()

; ---- Now safe to read the TMEM accumulator
%acc_reg = call <128 x float> @llvm.nvvm.tcgen05.ld.m64n8x32b(
    i32 %tmem_d_handle)
```

See [tcgen05 / WGMMA / mbarrier / Cluster Emission — End-To-End Lowering](../codegen/tcgen05-wgmma-mbarrier-cluster.md#end-to-end-lowering) for the full asynchronous-MMA wait protocol and the mbarrier-completion variant that pairs the MMA with an mbarrier.

## Stage 5: NVPTX MIR

The NVPTX backend's instruction selector ([ISelDAG and MatcherTable](../codegen/iseldag-and-matchertable.md)) consumes the LLVM intrinsics and produces a `MachineFunction` instruction. The `tcgen05.mma` family of opcodes is a set of `TCGEN05_MMA_*` machine instructions, one per (cta_group, sparsity, block_scale, weight_stationary) tuple — the closed-range `10521..10530` opcode set the verifier in [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) selects. For the single-CTA dense non-block-scale form, the opcode is `TCGEN05_MMA_CTA_GROUP1_DENSE`.

```text
bb.entry:
  ; --- TMEM allocation: handle in a 32-bit virtual register
  %tmem_d_handle:b32 = TCGEN05_ALLOC_SHARED imm:16        ; 16 columns

bb.loop:
  ; --- UMMA SMEM descriptor encode (same opcode as WGMMA on sm_90a)
  %a_desc:b64 = TCGEN05_MMA_SMEM_DESC_ENCODE
      %smem_a:b64, imm:512, imm:16,  imm:0, imm:1
  %b_desc:b64 = TCGEN05_MMA_SMEM_DESC_ENCODE
      %smem_b:b64, imm:2048, imm:128, imm:0, imm:1

  ; --- tcgen05.mma with packed control word + collector word
  TCGEN05_MMA_CTA_GROUP1_DENSE
      d:        %tmem_d_handle               ; TMEM handle (in-place accumulate)
      a:        %a_desc                      ; A descriptor (SMEM)
      b:        %b_desc                      ; B descriptor (SMEM)
      ctrl:     imm:193                      ; 0xC1 = kind::f16 + cta_group::1
      collector:imm:1                        ; collector::a::fill
      scale_d:  imm:1                        ; enable_input_d = true
      ; opcode index: 10522 (dense, non-block-scale, cta_group::1, ws=0)

  ; --- Loop body continues with next K tile, same TMEM handle...

bb.epi:
  ; --- Drain the asynchronous MMA queue before reading TMEM
  TCGEN05_WAIT_CTA_GROUP1

  ; --- Read the TMEM accumulator into a register vector
  %acc:v128_f32 = TCGEN05_LD_M64N8X32B %tmem_d_handle

  ; --- Deallocate TMEM at function exit
  TCGEN05_DEALLOC %tmem_d_handle, imm:16
  TCGEN05_RELINQUISH_ALLOC_PERMIT
```

Four observations matter at MIR level. First, the opcode encodes the CTA-group selector, sparsity, block-scale, and weight-stationary bits in its name — `TCGEN05_MMA_CTA_GROUP1_DENSE` is one opcode; `TCGEN05_MMA_CTA_GROUP2_DENSE`, `TCGEN05_MMA_CTA_GROUP1_SPARSE`, `TCGEN05_MMA_CTA_GROUP1_BLOCK_SCALED_DENSE`, and seven other variants are each a separate opcode in the NVPTX `.td` files. The verifier in `verify_tcgen05_mma` (documented in [tcgen05 / WGMMA / mbarrier / Cluster Emission — Verifier Rules](../codegen/tcgen05-wgmma-mbarrier-cluster.md#verifier-rules)) reads the packed control word out of the immediate operand and re-checks every constraint the dialect verifier already checked, because arch-conditional flags (`is_arch_cond`) and subtarget features (`has_scale_input_accumulator`, `has_arch_conditional`) only become fully visible after target selection. Second, the kind word `0xC1 = 193` is a literal immediate, with bits decoded as `mma_kind = 011 (f16)`, `block_scale = 0`, `scale_input_acc = 0`, `scale_vec_size = 00 (1X)`, `cta_group = 01 (1-CTA)` — the encoding documented in [tcgen05 / WGMMA / mbarrier / Cluster Emission — Control-Word Bit Layout](../codegen/tcgen05-wgmma-mbarrier-cluster.md#control-word-bit-layout). Third, the TMEM handle is a single 32-bit virtual register threaded through every consumer (alloc → MMA → ld → dealloc), and the MIR register allocator pins it to a single physical register for the entire lifetime — there is no spill path for TMEM handles because the TMEM region cannot move. Fourth, the `TCGEN05_WAIT_CTA_GROUP1` opcode has no operand — it drains the per-CTA asynchronous MMA queue globally, not per-handle.

The kind word has now flowed through five levels of representation: implicit shape-and-element-type in `cuda_tile`, implicit atom-name in `nv_tileaa`, explicit five-attribute group in `nv_tileas`, explicit packed `i32 193` immediate to `llvm.nvvm.tcgen05.mma.cta_group__1` in LLVM IR, and explicit immediate operand `imm:193` to `TCGEN05_MMA_CTA_GROUP1_DENSE` in MIR.

## Stage 6: PTX Text

The AsmPrinter ([AsmPrinter and Per-SM Windows](../codegen/asm-printer-monster-and-windows.md)) walks the `MachineFunction` and renders each instruction. The single-CTA dense `tcgen05.mma` with FP16 kind prints as `tcgen05.mma.cta_group::1.kind::f16`, with the collector and ashift modifiers in their own qualifier slots.

```ptx
//
// Generated by tileiras 13.1, target sm_100a
//
.version 8.6
.target sm_100a
.address_size 64

.extern .shared .align 16 .b8 global_smem[];

.entry gemm_blackwell(
    .param .u64 gemm_param_0,
    .param .u64 gemm_param_1,
    // ...
)
.reqntid 128, 1, 1
{
    .reg .pred      %p<8>;
    .reg .b32       %r<48>;
    .reg .b64       %rd<24>;
    .reg .f32       %f<128>;

    // ---- TMEM allocation in shared-prefix scratch
    tcgen05.alloc.cta_group::1.sync.aligned.shared::cta.b32 [%rd_tmem_scratch], 16;
    ld.shared.b32       %r_tmem_d, [%rd_tmem_scratch];

    // ---- UMMA SMEM descriptors for A and B (encoded once before the K loop)
    // (descriptor build elided; same bit layout as wgmma.descriptor.encode.smem)

    mov.u32             %r_k, 0;

LBB_loop:
    // ---- (TMA loads for A and B into smem stages, see TMA Load Walkthrough)
    // ---- (mbarrier wait on producer barriers, see mbarrier State Machine)

    // ---- tcgen05.mma proper
    //   modifier set:
    //     .cta_group::1   selector (single-CTA dispatch)
    //     .kind::f16      element-type family for A/B/D
    //     .collector::a::fill   load A from TMEM/SMEM, cache for the next call
    //
    //   operands (in PTX operand order):
    //     [%r_tmem_d]    TMEM accumulator handle
    //     %rd_a_desc     A operand (SMEM descriptor)
    //     %rd_b_desc     B operand (SMEM descriptor)
    //     idesc          packed instruction descriptor (kind + cta_group + flags)
    //     enable-input-d predicate
    tcgen05.mma.cta_group::1.kind::f16.collector::a::fill
        [%r_tmem_d],                  // D = C += A * B, TMEM in-place
        %rd_a_desc,                   // A: SMEM descriptor
        %rd_b_desc,                   // B: SMEM descriptor
        %r_idesc,                     // instruction descriptor (kind word)
        1;                            // enable_input_d (scale_d analogue)

    add.u32         %r_k, %r_k, 16;
    setp.lt.u32     %p_done, %r_k, %r_k_end;
    @%p_done bra    LBB_loop;

    // ---- After the K loop: drain the asynchronous MMA queue
    tcgen05.wait.cta_group::1.sync.aligned;

    // ---- TMEM read-back into the warp's register file (epilogue uses the
    //      register-resident accumulator for downstream addf / TMA store)
    tcgen05.ld.sync.aligned.16x64b.x32.b32
        {%f0,  %f1,  %f2,  %f3, ..., %f31},
        [%r_tmem_d];

    // ---- TMEM dealloc + relinquish at function exit
    tcgen05.dealloc.cta_group::1.sync.aligned.b32 [%r_tmem_d], 16;
    tcgen05.relinquish_alloc_permit.cta_group::1.sync.aligned;

    ret;
}
```

The mnemonic encodes seven independent decisions. `tcgen05.mma` is the family. `.cta_group::1` is the CTA-group selector (versus `.cta_group::2` for the two-CTA form). `.kind::f16` is the element-type family (versus `.kind::tf32`, `.kind::i8`, `.kind::f8f6f4`, `.kind::mxf8f6f4`, `.kind::mxf4`, `.kind::mxf4nvf4`). `.collector::a::fill` is the collector mode (versus `.collector::a::use`, `.collector::a::lastuse`, or absence-of-collector for the discard path). Optional modifiers — `.sp` for sparsity, `.block_scale` for the microscale variants, `.ws` for weight-stationary, `.ashift` for the A-shift modifier — are concatenated in a fixed order. Each modifier maps back to a specific bit in the kind word or collector word that travelled from `cute_nvgpu.arch.mma.SM100.umma` through the LLVM intrinsic name into the MIR opcode suffix and finally into the printed mnemonic.

The instruction descriptor operand (`%r_idesc`) is the packed kind word re-materialised as a runtime register value when the kernel needs to vary the kind across iterations; for constant-kind MMA the compiler folds the descriptor into the mnemonic modifiers and the operand becomes a constant `imm` to PTX. The dual-form encoding — modifiers on the mnemonic versus an immediate operand — is the same kind of trade-off WGMMA makes for `scale_d` on `sm_90a`.

The TMEM accumulator operand `[%r_tmem_d]` is bracketed because it is *not* a register operand — it is a TMEM handle whose syntactic form mirrors a memory address. The PTX assembler reads the brackets as a hint to use the TMEM-addressed form of the instruction; the unbracketed form `%r_tmem_d` would dispatch a different opcode entirely.

> ⚡ **QUIRK — `tcgen05.wait` is per-CTA, not per-handle**
> The `tcgen05.wait.cta_group::1.sync.aligned` instruction has no operand. It drains *every* outstanding asynchronous `tcgen05.mma` issued by the warp group; there is no "wait for this specific MMA" form. A kernel that issues multiple MMAs against different TMEM handles and wants to drain only one must serialize the issue order, because the wait is global. The verifier emits `"tcgen05.wait supported only on arch-conditional or family-conditional variants from SM100 onwards."` on non-arch-conditional targets. A reimplementation that tries to emit a per-handle wait builds a kernel that compiles cleanly but races against the asynchronous MMA in production. The companion mbarrier-completion variant of `tcgen05.mma` — the `tcgen05.commit` family — provides per-MMA completion semantics through a paired mbarrier; see [tcgen05 / WGMMA / mbarrier / Cluster Emission — mbarrier Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md#mbarrier-emission) for the protocol.

## Kind Word: Cross-Stage Flow

The packed 9-bit kind word `0xC1` is the canonical thread tying the MMA's variant choice through every layer. It is computed exactly once — five orthogonal field decisions packed at Stage 3 — but lives under different names and at different levels of abstraction at every stage. Its journey:

| Stage | Form | Carrier | Source |
|---|---|---|---|
| 1 — `cuda_tile` | implicit | tile element types + `fastmath` attr | derived at lower time |
| 2 — `nv_tileaa` | implicit | MMA atom name `sm100_umma_m64n128k16_f32_bf16_bf16` | derived from layout assignment |
| 3 — `nv_tileas` | explicit 5-attribute group | `kind`, `cta_group`, `scale_vec_size`, `scale_input_acc`, `block_scale` on `umma` op | computed by atom desugar |
| 4 — LLVM IR | explicit i32 immediate | `i32 193` argument to `llvm.nvvm.tcgen05.mma.cta_group__1` | packed by `ConvertTileASToLLVM` |
| 5 — NVPTX MIR | explicit immediate | `imm:193` operand to `TCGEN05_MMA_CTA_GROUP1_DENSE` | selected through ISelDAG |
| 6 — PTX text | explicit modifier set | `.cta_group::1.kind::f16.collector::a::fill` qualifiers | rendered by AsmPrinter |

The transition from implicit (stages 1–2) to explicit (stages 3–6) happens in the layout-assignment-to-atom-desugar pipeline, the same point where the MMA atom witness is committed. Until that point runs, the kind-word bits exist only as derivable consequences of the tile element types and the absence of per-op modifier attributes; after that point, they are first-class attributes that travel verbatim through every subsequent lowering. The verifier in [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) walks the 13-rule ladder at each of stages 3 and 5; the LLVM stage 4 inherits Stage 3's verifier output through the intrinsic name selection (the family is encoded in the intrinsic name, so a malformed kind word that survived stage 3 lands on a syntactically wrong intrinsic and fails LLVM IR verification).

## Stage 7: SASS

Past the PTX text, the path leaves tileiras and enters `ptxas`'s territory through the boundary documented in [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md). The assembler renders the `tcgen05.mma.cta_group::1.kind::f16.collector::a::fill` mnemonic into the SASS instruction stream — instruction encodings, register allocation across the TMEM-handle lifetime, and the interleaving of asynchronous MMA issue against the producer-side TMA loads are entirely `ptxas`'s decision. The TMEM handle becomes a specific 32-bit register, the kind word becomes an immediate field in the SASS encoding, and the collector mode contributes to the operand-select fields.

That layer is out of scope for tileiras's documentation. The wiki covers the path up to PTX text; everything below the handoff is `ptxas` territory, including the SASS opcode encoding for the `UTMA` / `UMMA` family and the SM scheduling decisions that interleave the asynchronous MMA issue against the warp group's TMA-driven operand staging.

## Capability Cross-Check

The walkthrough above targets `sm_100a`. The same `cuda_tile.mmaf` would produce a different cascade on every other supported architecture; the table below summarises the divergence so a reimplementer can predict what to expect under a different `--compute-capability` value.

| Compute capability | MMA atom witness | Accumulator residency | Stage-6 PTX mnemonic |
|---|---|---|---|
| `sm_80` (Ampere) | `sm80_mma_m16n8k16_f32_bf16_bf16` | register fragments | `mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32` (tiled to 64 instructions) |
| `sm_89` (Ada) | `sm80_mma_m16n8k16_f32_bf16_bf16` | register fragments | same Ampere mnemonic |
| `sm_90a` (Hopper) | `sm90_wgmma_m64n128k16_f32_bf16_bf16` | warp-group registers (`<32 x float>` × 4 warps) | `wgmma.mma_async.sync.aligned.m64n128k16.f32.bf16.bf16` |
| `sm_100a` (Blackwell datacenter) | `sm100_umma_m64n128k16_f32_bf16_bf16` | TMEM (16 columns × 128 rows) | `tcgen05.mma.cta_group::1.kind::f16.collector::a::fill` |
| `sm_103a` (Blackwell Ultra GB300) | `sm100_umma_m64n128k16_f32_bf16_bf16` (inherited) | TMEM | same Blackwell mnemonic |
| `sm_110` (Jetson Thor) | no MMA atom registered | register fallback via universal-FMA | (no `tcgen05.mma`; falls back to `mma.sync` family) |
| `sm_120` (consumer Blackwell) | `sm120_mma_m16n8k32_f32_bf16_bf16` (no TMEM) | register fragments | `mma.sync.aligned.m16n8k32.row.col.f32.bf16.bf16.f32` |

The transition between `sm_90a` and `sm_100a` is where the accumulator moves out of the register file and into TMEM, the kind word enters the encoding, the `cta_group` selector becomes a first-class modifier, and the collector cache enters the instruction operand set. Below that boundary the MMA writes registers; above that boundary it writes TMEM. See [Matmul Progression by SM — SM100 / SM103](matmul-progression-by-sm.md#sm100--sm103-tensor-memory-and-tcgen05mma) for the parallel progression and [tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) for the structural model of the TMEM-resident accumulator.

## Verifier Surface at Each Stage

Each stage's verifier catches a different class of malformed MMA. The same operation must satisfy every verifier on its path; an MMA that survives Stage 2 because Stage 1's verifier didn't notice an issue still fails at Stage 3 once the kind-word ladder runs its 13 rules. The catalog by stage:

| Stage | Verifier | Sample diagnostics |
|---|---|---|
| 1 — `cuda_tile` | `cuda_tile.mmaf` verifier | "tile dimensions must conform: …", "tile would exceed the maximum of …", "fastmath attribute is not one of {reassoc, contract, …}" |
| 2 — `nv_tileaa` | `nv_tileaa.dot` verifier (inherited tile invariants) | "atom output type does not match accumulator: …", "atom input precision incompatible with operand type: …" |
| 3a — `cute_nvgpu.arch.mma.SM100.umma` | inherited from TileAS `verify_umma_canonical_layout` | "Not a canonical UMMA_MN Layout: Expected stride failure.", "Not a canonical UMMA_K Layout: Expected MN-size multiple of …" |
| 3b — `cute_nvgpu.arch.mma.SM100.umma` kind-word | the 13-rule ladder in [Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) | "INT8 type is supported only on arch-conditional variants.", "Scale input accumulator can only be used with f16 and tf32 types", "Block scale is not supported for f16, tf32, f8f6f4, and i8 types", "cta_group::2 is not supported with weight stationary" |
| 3c — `cute_nvgpu.arch.sm100.alloc_tmem` | TMEM allocator verifier | "allocated tmem out of resource: …", "failed to find scratch smem to allocate tmem", "failed to init tmem" |
| 4 — LLVM IR | shared `TypeConverter` + intrinsic-arity check | (catch-all for arity mismatches against `llvm.nvvm.tcgen05.mma.*` declarations) |
| 5 — NVPTX MIR | `verify_tcgen05_mma` (see [tcgen05 / WGMMA / mbarrier / Cluster Emission — Verifier Rules](../codegen/tcgen05-wgmma-mbarrier-cluster.md#verifier-rules)) | "tcgen05.mma supported only on arch-conditional or family-conditional variants from SM100 onwards.", "ashift is not supported with tcgen05.mma.block_scale variants", "Cannot use collector::a::use or colletor::a::fill with ashift" (verbatim `colletor` typo) |
| 6 — PTX text | `ptxas` directive verifier | (out of scope; documented under [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md)) |

The 13-rule kind-word ladder at Stage 3b is the most important to flag: a kind word that fails any of the 13 rules — INT8 outside arch-conditional, MXF4 sparse outside arch-conditional, explicit `scale_vec_size` outside arch-conditional, scale-input-accumulator on non-SM100A, scale-input-accumulator with non-f16/tf32, block-scale with f16/tf32/f8f6f4/i8, ashift with block-scale, `cta_group::2` with weight-stationary, weight-stationary with `mxf8f6f4`/`f8f6f4`/`mxf4`, collector use/fill with ashift (preserving the verbatim `colletor` typo), `mxf8f6f4` with `scale_vec_size > 1`, `mxf4nvf4` with `1X`, `mxf4` with `1X` or `4X` — rejects the MMA with a diagnostic that names the rule but not the surrounding context. A reimplementation that emits a `tcgen05.mma` op without first walking the same 13-rule ladder builds opcodes that the backend verifier rejects at lowering time with diagnostics that are deliberately hard to map back to the originating MLIR op.

## Reimplementation Checklist

Anyone reproducing a one-shot `tcgen05.mma` from a higher-level IR should walk the same six gates this page traces, in order. The checklist mirrors the cascade:

1. Pick an MMA atom whose interface tag (`SM100UmmaAtomTypeInterface`) marks it as a `tcgen05.mma` candidate. The atom name encodes the variant (dense / sparse / block-scaled, weight-stationary or not). Anything else stays on a different MMA path.
2. Verify the UMMA canonical layout invariants: K-size must be a multiple of `256 / sizeof_bits(elem)` for dense or `512 / sizeof_bits(elem)` for sparse, MN-size must be a multiple of the atom-imposed stride, and the descriptor's swizzle mode must match the atom's expected residency. The `verify_umma_canonical_layout` ladder catches every violation.
3. Allocate TMEM through `alloc_tmem` at function entry, sized in units of 128-row columns (16 bytes per row, 2 KiB per column). Reserve exactly `num_columns` columns for the accumulator; the SM has 256 columns total.
4. Pack the 9-bit kind word with `cta_group` in bits 0..1, `scale_vec_size` in bits 2..3, `scale_input_acc` in bit 4, `block_scale` in bit 5, `mma_kind` in bits 6..8. Walk the 13-rule verifier ladder before emitting the op; a kind word that passes any subset of the rules without passing all is silently miscompiled.
5. Pack the collector word with the `collector_a` mode (`fill`, `use`, `lastuse`, or absence-as-discard) and the `ashift` bit, ensuring that `ashift` and `collector::a::use`/`fill` are mutually exclusive (rule 10) and that block-scale opcodes reject `ashift` (rule 7).
6. Pair every issue with a downstream `tcgen05.wait.cta_group::N` (matching the issue's `cta_group`) before any `tcgen05.ld` reads the accumulator. The wait is *not* a property of the MMA — it is a separate operation, and it drains every outstanding MMA in the warp group rather than the specific issue.

Skipping any of these six steps yields a kernel that either fails verifier mid-pipeline, fails `ptxas` at SASS time, races against the asynchronous MMA queue at runtime, or — worst — reads stale data from a TMEM region the allocator has already returned to the free pool. The QUIRK callouts above flag the most error-prone of the six.

Two further constraints are worth flagging because they are easy to miss when working backward from a PTX dump. First, the `cute_nvgpu.arch.sm100.alloc_tmem` op must dominate every `umma`, `tmem_load`, and `tmem_store` op that names its handle, and the matching `cute_nvgpu.arch.sm100.dealloc_tmem` must post-dominate them; placing the alloc inside a conditional that the consumer escapes produces IR that passes the dialect verifier but reads garbage from TMEM at runtime. Second, the `tcgen05.relinquish_alloc_permit` op must be issued before the kernel exits even if no MMA was actually emitted — the allocator-permit token is per-CTA, not per-region, and a CTA that exits with an outstanding permit prevents the next CTA-on-this-SM from allocating.

## Cross-References

[DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) is the kernel-wide walkthrough this page mirrors; it traces the same kind of cascade for an `sm_90a` WGMMA GEMM and stays a useful reference for the producer/consumer pipeline structure that wraps the MMA.
[TMA Load Walkthrough](tma-load-walkthrough.md) is the producer-side companion: the TMA bulk-tensor load that stages the A and B operands into SMEM before the `tcgen05.mma` reads them, with the mbarrier transaction-byte handshake the consumer-side wait depends on.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) is the canonical reference for the TMEM model, the ten-variant taxonomy, the per-variant operand contracts (which operand rides SMEM descriptor versus TMEM), the control word bit layout, the collector cache model, the block-scale operand layout, and the weight-stationary mode contract.
[Mode Pattern Verifiers — tcgen05.mma Kind-Word Verifier](../dialects/cute_nvgpu/mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) is the 13-rule kind-word verifier this walkthrough exercises at Stage 3b, with the verbatim diagnostic strings (including the preserved `colletor` typo) and the worked-example tables.
[tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) covers the backend-side machine-form validation, the packed control-word/collector-word format, the subtarget feature probe, and the mbarrier-completion variant for per-MMA completion semantics.
[Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) covers the cluster-side copy patterns (`tcgen05.cp` with `warpx2_*` and `warpx4` multicast masks) that stage operands into the cooperating CTAs' TMEM regions, including the rank predicate and the cluster-sibling pairing protocol.
[WGMMA Emission Protocol](wgmma-emission-protocol.md) is the Hopper predecessor; comparing the four-op WGMMA protocol to the alloc / MMA / wait / dealloc tcgen05 protocol shows why the accumulator moved from registers to TMEM at the SM90→SM100 boundary, and the [WGMMA SMEM Descriptor Bit Layout](wgmma-emission-protocol.md#smem-descriptor-bit-layout) section documents the descriptor format that tcgen05 reuses verbatim.
[Matmul Progression by SM](matmul-progression-by-sm.md) places this walkthrough in the broader SM70-to-SM121 lineage and shows how the same `cuda_tile.mmaf` lowers to a different cascade on every supported architecture.
[mbarrier State Machine](mbarrier-state-machine.md) is the synchronisation reference for the mbarrier-completion variant of `tcgen05.mma` (the `tcgen05.commit` family) that provides per-MMA completion semantics through a paired mbarrier — the alternative to the global `tcgen05.wait` this walkthrough uses.
[MMA Atoms SM70-SM120 — SM100 UMMA Layout Grammar](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) catalogues the atom witness shapes for every supported `tcgen05.mma` variant and the layout-assignment pre-pass that picks among them.
[Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) covers the TMEM-column allocation strategy at Stage 3, including the column-base assignment and the lifetime-aware reuse across pipelined K iterations.
