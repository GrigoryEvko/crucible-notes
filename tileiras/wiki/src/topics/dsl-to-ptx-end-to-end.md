# DSL to PTX End-to-End

## Abstract

The tileiras wiki documents each stage of the MLIR-to-PTX cascade on its own page. A reader following one kernel from a Triton-style frontend down to emitted PTX would otherwise have to traverse the per-stage pages and reconstruct the IR shape at every transition. This page is the walkthrough: a representative GEMM kernel rendered at every level of the pipeline, with each transition annotated by the pass that produced it. The per-stage canonical pages remain authoritative for pass internals, fold rules, and verifier contracts; this page focuses on the IR shape continuity that ties them together.

The kernel is a fused `D = A * B^T + C` operation targeting `sm_90a`. Inputs `A` and `B` are `tile<128x64xf16>` blocks staged through TMA into shared memory; `C` and `D` are `tile<128x128xf32>` blocks of the same row tile. The walkthrough follows one steady-state iteration of the K loop; descriptor construction, prologue, and epilogue are elided in favor of the producer/consumer body the scheduler operates on.

## The Kernel

A Triton-style DSL surface mirrors the public `cuda_tile` contract: structured control flow, tile-shaped SSA values, partition-view memory access, and tile-granular MMA. The illustrative source below is what a frontend constructs before lowering — the syntax is not real Triton, but the abstraction level is the same.

```python
@kernel
def gemm(A: ptr<f16>, B: ptr<f16>, C: ptr<f32>, D: ptr<f32>,
         M: i32, N: i32, K: i32):
    block_m = program_id(0)
    block_n = program_id(1)

    a_view = make_partition_view(A, [M, K], tile=(128, 64), dim_map=[0, 1])
    b_view = make_partition_view(B, [N, K], tile=(128, 64), dim_map=[0, 1])
    c_view = make_partition_view(C, [M, N], tile=(128, 128), dim_map=[0, 1])
    d_view = make_partition_view(D, [M, N], tile=(128, 128), dim_map=[0, 1])

    acc = zeros(tile<128x128xf32>)
    for k in range(0, K, 64):
        a_tile = load(a_view, (block_m, k // 64))     # tile<128x64xf16>
        b_tile = load(b_view, (block_n, k // 64))     # tile<128x64xf16>
        acc    = mmaf(a_tile, b_tile, acc)            # tile<128x128xf32>

    c_tile = load(c_view, (block_m, block_n))
    d_tile = addf(acc, c_tile)
    store(d_view, (block_m, block_n), d_tile)
```

The frontend serialises this as `cuda_tile` bytecode and hands it to `tileiras`. Everything below this point is internal IR.

## Stage 1: cuda_tile IR

The first IR the compiler sees is `cuda_tile` itself — the only public dialect in the cascade and the input contract documented in [cuda_tile Overview](../dialects/cuda_tile/overview.md). Tile values are shaped SSA primitives, memory access rides on `partition_view` operands with explicit `token` ordering, and `mmaf` describes intent without committing to an MMA atom.

```mlir
cuda_tile.module {
  cuda_tile.entry @gemm(%A: !cuda_tile.ptr<f16>, %B: !cuda_tile.ptr<f16>,
                       %C: !cuda_tile.ptr<f32>, %D: !cuda_tile.ptr<f32>,
                       %M: i32, %N: i32, %K: i32) {
    %tok0 = cuda_tile.make_token : !cuda_tile.token
    %bm = cuda_tile.get_tile_block_id { axis = 0 : i32 } : i32
    %bn = cuda_tile.get_tile_block_id { axis = 1 : i32 } : i32

    %a_view = cuda_tile.tensor_view %A, shape = [%M, %K], stride = [%K, 1]
            : !cuda_tile.tensor_view<128x64xf16>
    %b_view = cuda_tile.tensor_view %B, shape = [%N, %K], stride = [%K, 1]
            : !cuda_tile.tensor_view<128x64xf16>
    %c_view = cuda_tile.tensor_view %C, shape = [%M, %N], stride = [%N, 1]
            : !cuda_tile.tensor_view<128x128xf32>
    %d_view = cuda_tile.tensor_view %D, shape = [%M, %N], stride = [%N, 1]
            : !cuda_tile.tensor_view<128x128xf32>

    %a_part = cuda_tile.partition_view %a_view, tile = [128, 64], dim_map = [0, 1]
            : !cuda_tile.partition_view<128x64xf16>
    %b_part = cuda_tile.partition_view %b_view, tile = [128, 64], dim_map = [0, 1]
            : !cuda_tile.partition_view<128x64xf16>

    %zero  = cuda_tile.constant dense<0.0> : !cuda_tile.tile<128x128xf32>
    %k_end = arith.muli %K, %K : i32
    %c0    = arith.constant 0  : i32
    %c64   = arith.constant 64 : i32

    %acc_out = cuda_tile.for %k = %c0 to %K step %c64 iter_args(%acc = %zero)
             -> !cuda_tile.tile<128x128xf32> {
      %kt = arith.divsi %k, %c64 : i32
      %a, %tok_a = cuda_tile.load_view_tko %a_part, [%bm, %kt], %tok0
                 : !cuda_tile.tile<128x64xf16>, !cuda_tile.token
      %b, %tok_b = cuda_tile.load_view_tko %b_part, [%bn, %kt], %tok_a
                 : !cuda_tile.tile<128x64xf16>, !cuda_tile.token
      %acc_n = cuda_tile.mmaf %a, %b, %acc { fastmath = "contract" }
             : !cuda_tile.tile<128x64xf16>, !cuda_tile.tile<128x64xf16>,
               !cuda_tile.tile<128x128xf32>
      cuda_tile.yield %acc_n : !cuda_tile.tile<128x128xf32>
    }

    %c_part = cuda_tile.partition_view %c_view, tile = [128, 128], dim_map = [0, 1]
            : !cuda_tile.partition_view<128x128xf32>
    %d_part = cuda_tile.partition_view %d_view, tile = [128, 128], dim_map = [0, 1]
            : !cuda_tile.partition_view<128x128xf32>

    %c_tile, %tok_c = cuda_tile.load_view_tko %c_part, [%bm, %bn], %tok0
                    : !cuda_tile.tile<128x128xf32>, !cuda_tile.token
    %d_tile = cuda_tile.addf %acc_out, %c_tile : !cuda_tile.tile<128x128xf32>
    %tok_s  = cuda_tile.store_view_tko %d_part, [%bm, %bn], %d_tile, %tok_c
            : !cuda_tile.token
    cuda_tile.return
  }
}
```

Distinctive markers at this tier: tile types are `!cuda_tile.tile<...>`, memory ops carry the `_tko` token-ordered suffix, the K loop uses `cuda_tile.for` with explicit `iter_args`, and `mmaf` carries a `fastmath` attribute rather than an atom selection. The verifier contract enforces power-of-two tile dimensions and a 16-million-element ceiling per tile, both of which the `128x128xf32` accumulator satisfies.

## Stage 2: nv_tileaa IR

`ConvertCudaTileToTileAA` rewrites every public operation into the alias-aware internal dialect. The three-populator structure documented in [cuda_tile to tileaa](../lowering/cuda-tile-to-tileaa.md) drives the rewrite: Part A handles arithmetic and control flow, Part B handles memory and views, Part C specialises `mmaf` and the reductions. Tile types collapse to plain `tensor<...>`, token types become `!nv_tileaa.token`, and pointer arithmetic becomes explicit through `addptr` and `make_memref`.

```mlir
nv_tileaa.func @gemm(%A: !llvm.ptr<1>, %B: !llvm.ptr<1>,
                    %C: !llvm.ptr<1>, %D: !llvm.ptr<1>,
                    %M: i32, %N: i32, %K: i32) {
  %tok0 = nv_tileaa.create_mem_token : !nv_tileaa.token
  %bm = nv_tileaa.get_program_id { axis = 0 : i32 } : i32
  %bn = nv_tileaa.get_program_id { axis = 1 : i32 } : i32

  %a_ref = nv_tileaa.make_memref %A, shape = [%M, %K], stride = [%K, 1],
                                 space = #nv_tileaa.global
         : !nv_tileaa.memref<?x?xf16>
  %b_ref = nv_tileaa.make_memref %B, shape = [%N, %K], stride = [%K, 1],
                                 space = #nv_tileaa.global
         : !nv_tileaa.memref<?x?xf16>
  %c_ref = nv_tileaa.make_memref %C, shape = [%M, %N], stride = [%N, 1],
                                 space = #nv_tileaa.global
         : !nv_tileaa.memref<?x?xf32>
  %d_ref = nv_tileaa.make_memref %D, shape = [%M, %N], stride = [%N, 1],
                                 space = #nv_tileaa.global
         : !nv_tileaa.memref<?x?xf32>

  %zero = nv_tileaa.constant_tensor dense<0.0> : tensor<128x128xf32>

  %acc_out = scf.for %k = %c0 to %K step %c64
             iter_args(%acc = %zero) -> tensor<128x128xf32> {
    %off_a = nv_tileaa.addptr %a_ref, [%bm, %k]
           : !nv_tileaa.memref<?x?xf16>
    %a, %tok_a = nv_tileaa.tiled_load %off_a, %tok0
               { copy_atom = #cute_nvgpu.copy_atom<sm90_tma_load_2d_f16> }
               : !nv_tileaa.memref<?x?xf16> -> tensor<128x64xf16>,
                 !nv_tileaa.token
    %off_b = nv_tileaa.addptr %b_ref, [%bn, %k]
           : !nv_tileaa.memref<?x?xf16>
    %b, %tok_b = nv_tileaa.tiled_load %off_b, %tok_a
               { copy_atom = #cute_nvgpu.copy_atom<sm90_tma_load_2d_f16> }
               : !nv_tileaa.memref<?x?xf16> -> tensor<128x64xf16>,
                 !nv_tileaa.token
    %acc_n = nv_tileaa.dot %a, %b, %acc
           { input_precision = "tf32", fastmath = "contract" }
           : tensor<128x64xf16>, tensor<128x64xf16>, tensor<128x128xf32>
              -> tensor<128x128xf32>
    scf.yield %acc_n : tensor<128x128xf32>
  }

  %off_c = nv_tileaa.addptr %c_ref, [%bm, %bn] : !nv_tileaa.memref<?x?xf32>
  %c_tile, %tok_c = nv_tileaa.tiled_load %off_c, %tok0
                  { copy_atom = #cute_nvgpu.copy_atom<sm90_tma_load_2d_f32> }
                  : !nv_tileaa.memref<?x?xf32> -> tensor<128x128xf32>,
                    !nv_tileaa.token

  %d_tile = arith.addf %acc_out, %c_tile : tensor<128x128xf32>

  %off_d = nv_tileaa.addptr %d_ref, [%bm, %bn] : !nv_tileaa.memref<?x?xf32>
  %tok_s = nv_tileaa.tiled_store %off_d, %d_tile, %tok_c
         { copy_atom = #cute_nvgpu.copy_atom<sm90_tma_store_2d_f32> }
         : tensor<128x128xf32>, !nv_tileaa.memref<?x?xf32>, !nv_tileaa.token

  nv_tileaa.return
}
```

Three changes carry the most weight downstream. Tile types are now plain MLIR `tensor<...>`, which lets ordinary tensor passes and the shared LLVM `TypeConverter` see through them. Every memory operation produces or consumes a `!nv_tileaa.token`, giving the scheduler an SSA representation of memory ordering. And every `tiled_load`/`tiled_store` carries a `copy_atom` witness attribute, picked from the [SM-Tier Roster and Copy Atom Registry](../dialects/cute_nvgpu/sm-tier-roster-and-copy-atom-registry.md); that witness is what the next stage uses to select a concrete hardware copy primitive.

## Stage 3: nv_tileas IR (after scheduling)

`ConvertTileAAToTileAS` keeps the alias-aware shape but rewrites memory and compute into operational forms the scheduler can reason about. The scheduling passes — modulo scheduler ([Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md)), buffer assignment, async-pipeline materialization — then turn the linear K loop into an explicit producer/consumer pipeline. After the [TileAS pass family](../passes/tileas/async-pipeline-family.md) runs, the loop body is wrapped in an `async.pipeline` region with TMA-based producer loads, an mbarrier-coordinated handshake, and a WGMMA-based consumer body.

```mlir
nv_tileas.func @gemm(...) attributes { nv_tileaa.kernel_spec = #ks } {
  %desc_a = nv_tileas.make_tiled_tma_desc %a_ref, box = [128, 64],
            atom = #cute_nvgpu.tma_atom<load_2d_f16, swizzle_128B>
          : !nv_tileas.tma_desc<128x64xf16>
  %desc_b = nv_tileas.make_tiled_tma_desc %b_ref, box = [128, 64],
            atom = #cute_nvgpu.tma_atom<load_2d_f16, swizzle_128B>
          : !nv_tileas.tma_desc<128x64xf16>

  %smem_a = nv_tileas.alloc_tensor { stages = 3 : i32 }
          : !nv_tileas.smem<3x128x64xf16>
  %smem_b = nv_tileas.alloc_tensor { stages = 3 : i32 }
          : !nv_tileas.smem<3x128x64xf16>

  %pipe = nv_tileas.async.pipeline.create_pipeline
            stages = 3, producer = #ag_p, consumer = #ag_c
          : !nv_tileas.pipeline_token
  %iter0 = nv_tileas.async.pipeline.create_iterator %pipe
         : !nv_tileas.pipeline_iter<i32>

  %acc_out, %iter_end = scf.for %k = %c0 to %K step %c64
      iter_args(%acc = %zero, %iter = %iter0)
      -> (tensor<128x128xf32>, !nv_tileas.pipeline_iter<i32>) {

    // ---- producer agent: TMA bulk loads into stage-local SMEM
    nv_tileas.async.pipeline.produce_one %pipe, %iter {
      %ptok = nv_tileas.async.pipeline.producer_acquire %pipe, %iter
            : !nv_tileas.producer_token
      %ptok2 = nv_tileas.async.pipeline.producer_write %ptok, %iter {
        nv_tileas.async.tiled_tma_load %desc_a, [%bm, %k], %smem_a, %iter
        nv_tileas.async.tiled_tma_load %desc_b, [%bn, %k], %smem_b, %iter
        nv_tileas.async.pipeline.yield
      }
      nv_tileas.async.pipeline.producer_commit %ptok2
      nv_tileas.async.pipeline.yield
    }

    // ---- consumer agent: WGMMA reads the same stage
    %acc_n = nv_tileas.async.pipeline.consume_one %pipe, %iter
             consumer_idx = 0 : i32 {
      %ctok = nv_tileas.async.pipeline.consumer_wait %pipe, %iter, 0
            : !nv_tileas.consumer_token
      %ctok2, %acc_loop = nv_tileas.async.pipeline.consumer_read %ctok, %iter {
        %a_stage = nv_tileas.view %smem_a, %iter
                 : !nv_tileas.smem<128x64xf16>
        %b_stage = nv_tileas.view %smem_b, %iter
                 : !nv_tileas.smem<128x64xf16>
        %acc_w = nv_tileas.dot %a_stage, %b_stage, %acc
               { atom = #cute_nvgpu.mma_atom<sm90_wgmma_m64n128k16_f32_f16_f16> }
               : !nv_tileas.smem<128x64xf16>, !nv_tileas.smem<128x64xf16>,
                 tensor<128x128xf32> -> tensor<128x128xf32>
        nv_tileas.async.pipeline.yield %acc_w : tensor<128x128xf32>
      }
      nv_tileas.async.pipeline.consumer_release %ctok2
      nv_tileas.async.pipeline.yield %acc_loop : tensor<128x128xf32>
    }

    %iter_n = nv_tileas.async.pipeline.inc_iter %iter
            : !nv_tileas.pipeline_iter<i32>
    scf.yield %acc_n, %iter_n
        : tensor<128x128xf32>, !nv_tileas.pipeline_iter<i32>
  }

  // epilogue: load C, add, store D (TMA store)
  %c_tile = nv_tileas.tiled_load %c_ref, [%bm, %bn]
          { atom = #cute_nvgpu.copy_atom<sm90_ldg_128_f32> }
          : tensor<128x128xf32>
  %d_tile = arith.addf %acc_out, %c_tile : tensor<128x128xf32>
  %desc_d = nv_tileas.make_tiled_tma_desc %d_ref, box = [128, 128],
            atom = #cute_nvgpu.tma_atom<store_2d_f32, swizzle_128B>
          : !nv_tileas.tma_desc<128x128xf32>
  nv_tileas.async.tiled_tma_store %desc_d, [%bm, %bn], %d_tile
  nv_tileas.return
}
```

The K loop is no longer a flat sequence of loads and an MMA. It is an async pipeline with three rotating stages, one producer agent owning the TMA loads, and one consumer agent owning the WGMMA. The pipeline iterator threads through `scf.for` via the type-propagation rule documented in [nv_tileas Overview](../dialects/nv_tileas/overview.md). Each MMA invocation carries a concrete `sm_90a` WGMMA atom; descriptor construction is a first-class operation with its own SSA result, not a hidden side effect of the load. The kernel-spec attribute on the function records `numWarps`, `clusterDim`, and per-stage SMEM size for the downstream LLVM lowering to lift onto `nvvm.*` discardable attributes.

## Stage 4: LLVM IR with NVVM intrinsics

`ConvertTileASToLLVM` (the nine-phase body conversion documented in [tileas to LLVM](../lowering/tileas-to-llvm.md)) is the terminal MLIR-side lowering. Pipeline structure flattens into integer phase tokens and `nvvm.mbarrier.*` operations; the WGMMA region expands into the four-op fence/MMA/commit/wait protocol from [WGMMA Emission Protocol](wgmma-emission-protocol.md); TMA loads expand into `cp.async.bulk.tensor` intrinsics. The kernel function picks up `nvvm.reqntid`, `nvvm.cluster_dim`, and `nvvm.maxnreg` attributes from the kernel-spec.

```llvm
define void @gemm(ptr addrspace(1) %A, ptr addrspace(1) %B,
                  ptr addrspace(1) %C, ptr addrspace(1) %D,
                  i32 %M, i32 %N, i32 %K)
    #0 !nvvm.kernel !1 {
entry:
  ; ---- TMA descriptor construction (one per operand, hoisted to entry)
  %desc_a = alloca [128 x i8], align 64, addrspace(5)
  call void @llvm.nvvm.cp.async.bulk.tensor.encode.2d(
      ptr addrspace(5) %desc_a, ptr addrspace(1) %A,
      i32 128, i32 64, i32 %K, i32 1, i32 1)  ; box, stride, swizzle=128B
  %desc_b = alloca [128 x i8], align 64, addrspace(5)
  call void @llvm.nvvm.cp.async.bulk.tensor.encode.2d(
      ptr addrspace(5) %desc_b, ptr addrspace(1) %B,
      i32 128, i32 64, i32 %K, i32 1, i32 1)

  ; ---- shared-memory backing for the 3-stage pipeline
  %smem_a = getelementptr inbounds i8,
                          ptr addrspace(3) @global_smem, i32 0
  %smem_b = getelementptr inbounds i8,
                          ptr addrspace(3) @global_smem, i32 49152

  ; ---- mbarriers (one per stage, init by warp 0)
  %mbar_full = getelementptr i8, ptr addrspace(3) @global_smem, i32 98304
  call void @llvm.nvvm.mbarrier.init.shared(
      ptr addrspace(3) %mbar_full, i32 1)         ; thread-count arrival

  br label %loop

loop:
  %k    = phi i32             [ 0, %entry ], [ %k_next, %loop ]
  %stg  = phi i32             [ 0, %entry ], [ %stg_next, %loop ]
  %ph   = phi i32             [ 0, %entry ], [ %ph_next, %loop ]
  %acc0 = phi <128 x float>   [ zeroinitializer, %entry ], [ %acc4, %loop ]
  ; (real lowering carries the accumulator as 16 lanes of <8 x float>,
  ;  one per WGMMA atom slice; we elide the fragment split here.)

  ; ---- producer: TMA bulk load into smem_a[stg], smem_b[stg]
  %smem_a_stg = getelementptr i8, ptr addrspace(3) %smem_a,
                                  i32 %stg_off_a
  %smem_b_stg = getelementptr i8, ptr addrspace(3) %smem_b,
                                  i32 %stg_off_b
  call void @llvm.nvvm.cp.async.bulk.tensor.shared.cluster.global.2d(
      ptr addrspace(3) %smem_a_stg, ptr addrspace(5) %desc_a,
      i32 %bm, i32 %k, ptr addrspace(3) %mbar_full)
  call void @llvm.nvvm.cp.async.bulk.tensor.shared.cluster.global.2d(
      ptr addrspace(3) %smem_b_stg, ptr addrspace(5) %desc_b,
      i32 %bn, i32 %k, ptr addrspace(3) %mbar_full)

  ; ---- consumer wait: parity-encoded transaction barrier
  %parity = and i32 %ph, 1
  %arrived = call i1 @llvm.nvvm.mbarrier.try_wait.parity.shared(
      ptr addrspace(3) %mbar_full, i32 %parity)

  ; ---- WGMMA region: fence, async MMAs across the K tile, commit, wait
  call void @llvm.nvvm.wgmma.fence.sync.aligned()
  %da = call i64 @llvm.nvvm.wgmma.descriptor.encode.smem(
      ptr addrspace(3) %smem_a_stg, i32 2048, i32 0, i32 0, i32 1)
  %db = call i64 @llvm.nvvm.wgmma.descriptor.encode.smem(
      ptr addrspace(3) %smem_b_stg, i32 2048, i32 0, i32 0, i32 1)
  %acc1 = call <32 x float>
      @llvm.nvvm.wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16(
          i64 %da, i64 %db, <32 x float> %acc0, i32 1, i32 1, i32 1)
  ; ... three more atom slices along N to cover the 128 output columns ...
  call void @llvm.nvvm.wgmma.commit_group.sync.aligned()
  call void @llvm.nvvm.wgmma.wait_group.sync.aligned(i32 0)

  ; ---- end-of-stage bookkeeping
  %k_next   = add i32 %k, 64
  %stg_next = urem i32 (add i32 %stg, 1), 3
  %ph_next  = xor i32 %ph, 1
  %done     = icmp uge i32 %k_next, %K
  br i1 %done, label %epi, label %loop

epi:
  ; ---- C load, add, TMA store of D
  ...
  ret void
}

attributes #0 = {
  "nvvm.reqntid"="128,1,1"
  "nvvm.cluster_dim"="2,1,1"
  "nvvm.maxnreg"="168"
  "nvvm.kernel"
}
```

What looked like a queue in `nv_tileas` is now a flat loop carrying a `stg` index, a `parity` bit, and a vector accumulator phi node. WGMMA descriptors are SSA `i64` values produced by `llvm.nvvm.wgmma.descriptor.encode.smem`, packing the bit fields documented in [WGMMA Emission Protocol — SMEM Descriptor Bit Layout](wgmma-emission-protocol.md#smem-descriptor-bit-layout). The kernel attributes — `nvvm.reqntid=128`, `nvvm.cluster_dim=2`, `nvvm.maxnreg=168` — are the [tileas-to-llvm Phase 3 translations](../lowering/tileas-to-llvm.md#phase-3-kernel-attribute-transfer) of the `nv_tileaa.kernel_spec` block.

## Stage 5: NVPTX MIR

The NVPTX backend selector ([ISelDAG and MatcherTable](../codegen/iseldag-and-matchertable.md)) consumes the LLVM IR and produces a `MachineFunction` whose instructions are NVPTX target opcodes. Parameter loads become `NVPTXISD::LoadParam` SDNodes resolved into `LD_PARAM_v*`. TMA tensor copies become `CP_ASYNC_BULK_TENSOR_*` machine instructions. WGMMA becomes a `WGMMA_MMA_ASYNC_*` machine instruction that the AsmPrinter renders as the `wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16` mnemonic.

```text
bb.0.entry:
  liveins: $r0, $r1, $r2, $r3, $r4, $r5, $r6, $r7, $r8

  ; .param block for the kernel entry — emitted by the call-prototype
  ; printer, not by individual MIR instructions in the body
  %rd0:b64 = LD_PARAM_64 0, gemm_param_0       ; A
  %rd1:b64 = LD_PARAM_64 0, gemm_param_1       ; B
  %rd2:b64 = LD_PARAM_64 0, gemm_param_2       ; C
  %rd3:b64 = LD_PARAM_64 0, gemm_param_3       ; D
  %r0:b32  = LD_PARAM_32 0, gemm_param_4       ; M
  %r1:b32  = LD_PARAM_32 0, gemm_param_5       ; N
  %r2:b32  = LD_PARAM_32 0, gemm_param_6       ; K

  ; --- TMA descriptor encode (writes 128B of shared)
  CP_ASYNC_BULK_TENSOR_2D_ENCODE_SHARED_GLOBAL
      %smem_desc_a:b64, %rd0, 128, 64, %r2, 1, 1
  CP_ASYNC_BULK_TENSOR_2D_ENCODE_SHARED_GLOBAL
      %smem_desc_b:b64, %rd1, 128, 64, %r2, 1, 1

bb.1.loop:
  successors: %bb.1, %bb.2

  %k:b32   = PHI 0, %bb.0, %k_next:b32, %bb.1
  %stg:b32 = PHI 0, %bb.0, %stg_next:b32, %bb.1
  %ph:b32  = PHI 0, %bb.0, %ph_next:b32, %bb.1

  ; --- TMA load: shared <- global through SMEM descriptor
  CP_ASYNC_BULK_TENSOR_2D_SHARED_CLUSTER_GLOBAL_MBARRIER
      %smem_a_stg:b64, %smem_desc_a, %bm:b32, %k, %mbar_full:b64
  CP_ASYNC_BULK_TENSOR_2D_SHARED_CLUSTER_GLOBAL_MBARRIER
      %smem_b_stg:b64, %smem_desc_b, %bn:b32, %k, %mbar_full

  ; --- transaction barrier wait, parity-encoded
  %parity:b32 = AND_b32 %ph, 1
  %p0:pred    = MBARRIER_TRY_WAIT_PARITY_SHARED %mbar_full, %parity

  ; --- WGMMA four-op sequence
  WGMMA_FENCE_SYNC_ALIGNED

  %da:b64 = WGMMA_DESCRIPTOR_ENCODE_SMEM %smem_a_stg, 2048, 0, 0, 1
  %db:b64 = WGMMA_DESCRIPTOR_ENCODE_SMEM %smem_b_stg, 2048, 0, 0, 1

  WGMMA_MMA_ASYNC_SYNC_ALIGNED_M64N128K16_F32_F16_F16
      dst:   %f0:f32, %f1:f32, ..., %f31:f32
      src_a: %da
      src_b: %db
      src_c: %f0, %f1, ..., %f31                  ; in-place accumulate
      scale_d: 1, trans_a: 1, trans_b: 1

  WGMMA_COMMIT_GROUP_SYNC_ALIGNED
  WGMMA_WAIT_GROUP_SYNC_ALIGNED 0

  %k_next:b32   = ADD_b32  %k, 64
  %stg_next:b32 = REM_b32  ADD_b32(%stg, 1), 3
  %ph_next:b32  = XOR_b32  %ph, 1
  %done:pred    = ICMP_UGE %k_next, %r2
  BRCOND %done, %bb.2
  BR %bb.1
```

Three things are worth noting at the MIR level. First, the `LD_PARAM_*` opcodes are NVPTX-specific pseudo-ops that the AsmPrinter renders as `ld.param.*` — they cannot be expressed as generic `ISD::LOAD` because the PTX `.param` space disallows aliasing and arbitrary access patterns. Second, the WGMMA accumulator is materialised as 32 physical FP32 registers (one per thread per output element of the `64x128xf32` tile / 32 lanes per warp / 4 warps), all alive across the MMA instruction; this is what drives the `nvvm.maxnreg=168` budget the kernel-spec sets. Third, the `MBARRIER_TRY_WAIT_PARITY_SHARED` form encodes the producer/consumer handshake as a single predicate-producing instruction — the `i1` result drives the conditional branch that retries the wait.

## Stage 6: PTX text

The AsmPrinter ([AsmPrinter and Per-SM Windows](../codegen/asm-printer-monster-and-windows.md)) walks the `MachineFunction` and renders each instruction through its print shape. The result is the PTX text that `ptxas` consumes.

```ptx
//
// Generated by tileiras 13.1, target sm_90a
//
.version 8.4
.target sm_90a
.address_size 64

.extern .shared .align 16 .b8 global_smem[];

.entry gemm(
    .param .u64 gemm_param_0,
    .param .u64 gemm_param_1,
    .param .u64 gemm_param_2,
    .param .u64 gemm_param_3,
    .param .u32 gemm_param_4,
    .param .u32 gemm_param_5,
    .param .u32 gemm_param_6
)
.reqntid 128, 1, 1
.maxnreg 168
.cluster_dim 2, 1, 1
{
    .reg .pred      %p<8>;
    .reg .b32       %r<48>;
    .reg .b64       %rd<24>;
    .reg .f32       %f<128>;

    ld.param.u64    %rd0, [gemm_param_0];        // A
    ld.param.u64    %rd1, [gemm_param_1];        // B
    ld.param.u64    %rd2, [gemm_param_2];        // C
    ld.param.u64    %rd3, [gemm_param_3];        // D
    ld.param.u32    %r0,  [gemm_param_4];        // M
    ld.param.u32    %r1,  [gemm_param_5];        // N
    ld.param.u32    %r2,  [gemm_param_6];        // K

    mov.u32         %r3, %ctaid.x;               // bm
    mov.u32         %r4, %ctaid.y;               // bn

    // ---- TMA descriptor construction (one .b1024 tensormap per operand)
    cp.async.bulk.tensor.encode.2d.global
        [%rd10], [%rd0], {128, 64}, {%r2, 1}, 1, 1;
    cp.async.bulk.tensor.encode.2d.global
        [%rd11], [%rd1], {128, 64}, {%r2, 1}, 1, 1;

    // ---- mbarrier init by warp 0
    @%p0 mbarrier.init.shared.b64 [%rd12], 1;

    mov.u32         %r5, 0;                      // k
    mov.u32         %r6, 0;                      // stg
    mov.u32         %r7, 0;                      // ph

LBB_loop:
    // ---- TMA load A and B into stage-local SMEM
    cp.async.bulk.tensor.2d.shared::cluster.global.mbarrier::complete_tx::bytes
        [%rd20], [%rd10, {%r3, %r5}], [%rd12];
    cp.async.bulk.tensor.2d.shared::cluster.global.mbarrier::complete_tx::bytes
        [%rd21], [%rd11, {%r4, %r5}], [%rd12];

    // ---- consumer wait: try-wait drives a retry loop
    and.b32         %r8, %r7, 1;
LBB_wait:
    mbarrier.try_wait.parity.shared.b64 %p1, [%rd12], %r8;
    @!%p1 bra       LBB_wait;

    // ---- WGMMA four-op protocol
    wgmma.fence.sync.aligned;

    wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16
        {%f0,  %f1,  %f2,  %f3,  %f4,  %f5,  %f6,  %f7,
         %f8,  %f9,  %f10, %f11, %f12, %f13, %f14, %f15,
         %f16, %f17, %f18, %f19, %f20, %f21, %f22, %f23,
         %f24, %f25, %f26, %f27, %f28, %f29, %f30, %f31},
        %rd22,                                    // A descriptor
        %rd23,                                    // B descriptor
        1, 1, 1;                                  // scale_d, trans_a, trans_b

    wgmma.commit_group.sync.aligned;
    wgmma.wait_group.sync.aligned 0;

    add.u32         %r5, %r5, 64;                 // k += 64
    add.u32         %r9, %r6, 1;
    rem.u32         %r6, %r9, 3;                  // stg = (stg+1) % 3
    xor.b32         %r7, %r7, 1;                  // ph ^= 1
    setp.lt.u32     %p2, %r5, %r2;
    @%p2 bra        LBB_loop;

    // ---- epilogue: C load, add, TMA store of D
    // ...

    ret;
}
```

This is exactly the PTX text that `tileiras` ships across argv to `ptxas`. The `.reqntid`, `.maxnreg`, and `.cluster_dim` directives are the lifted kernel-spec attributes; the WGMMA fence/MMA/commit/wait sequence is the four-op contract documented in [WGMMA Emission Protocol — The Four-Op Sequence](wgmma-emission-protocol.md#the-four-op-sequence); the `mbarrier.try_wait.parity` form is the parity-encoded handshake whose state machine is documented in [mbarrier State Machine](mbarrier-state-machine.md).

## Stage 7: SASS (ptxas output)

The PTX text in Stage 6 is the final artefact tileiras produces. `ptxas`, running as a separate subprocess over the boundary documented in [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md), assembles the PTX into the SASS (Streaming Assembler) instruction stream — the hardware-level encoding the SM actually executes. SASS includes register allocation across the full live range of the WGMMA accumulator, instruction scheduling that interleaves the producer warps' TMA-issue with the consumer warps' WGMMA, and the exact 128-bit instruction encodings the GPU front-end decodes.

That layer is out of scope for tileiras's documentation. The wiki covers the path up to PTX text; everything below the handoff is `ptxas` territory. The argv shape, knob-file structure, and stdout-cubin convention are documented at the boundary page.

## Cross-References

The per-stage canonical pages remain authoritative for everything this walkthrough abbreviates. [cuda_tile Overview](../dialects/cuda_tile/overview.md), [nv_tileaa Overview](../dialects/nv_tileaa/overview.md), and [nv_tileas Overview](../dialects/nv_tileas/overview.md) cover the three tile dialects' operation rosters, type contracts, and verifier rules. [cuda_tile to tileaa](../lowering/cuda-tile-to-tileaa.md), [tileaa to tileas](../lowering/tileaa-to-tileas.md), and [tileas to LLVM](../lowering/tileas-to-llvm.md) cover the three partial-conversion passes that move IR between those dialects. [Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md) and [Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) cover the scheduling work that turns the linear K loop into a three-stage pipeline. [WGMMA Emission Protocol](wgmma-emission-protocol.md), [TMA, Tensormap and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md), and [mbarrier State Machine](mbarrier-state-machine.md) cover the three hardware contracts the consumer body relies on. [Per-SM Emission Templates](../codegen/per-sm-emission-templates.md) and [AsmPrinter and Per-SM Windows](../codegen/asm-printer-monster-and-windows.md) cover the NVPTX backend's PTX emission. [Matmul Progression by SM](matmul-progression-by-sm.md) and [Capability Matrix](../codegen/per-sm-emission-templates.md#capability-matrix) explain why the lowering chose `sm_90a` WGMMA and what the same kernel produces on Ampere, Ada, and Blackwell. [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md) closes the loop by describing the argv-over-subprocess interface where the PTX text leaves tileiras.
