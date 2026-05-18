# TMA Load Walkthrough

## Abstract

A single TMA load — the asynchronous bulk-tensor copy that moves a tile from global memory into shared memory on `sm_90a` and later — touches every layer of the tileiras cascade. It begins life as a tile-shaped `cuda_tile.load_view_tko`, picks up an alias-aware token in `nv_tileaa`, acquires a TMA descriptor handle and an mbarrier slot in `nv_tileas`, expands into an `nvvm.cp.async.bulk.tensor.shared.cluster.global.2d` intrinsic in LLVM, becomes a `CP_ASYNC_BULK_TENSOR_2D_*` machine instruction in NVPTX MIR, and surfaces as `cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes` in PTX text. The transaction-byte count — the number the consumer's `mbarrier.try_wait.parity` checks against — flows through each layer under a different name and at a different level of abstraction.

This page traces one load end-to-end. The kernel-wide walkthrough in [DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) shows the same kernel at every stage with all operations in place; this page narrows the focus to a single operation so the descriptor lifecycle, the mbarrier handshake, and the transaction-byte accounting are visible without GEMM scaffolding. Cross-reference targets remain the per-stage canonical pages: [cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md), [nv_tileaa to nv_tileas](../lowering/tileaa-to-tileas.md), [nv_tileas to LLVM](../lowering/tileas-to-llvm.md), [TileAS TMA and Memops Family](../passes/tileas/tma-and-memops-family.md), [mbarrier State Machine](mbarrier-state-machine.md), [WGMMA Emission Protocol](wgmma-emission-protocol.md), and [TMA, Tensormap, and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md).

Confidence: HIGH for IR shapes, mnemonic spellings, and the transaction-byte arithmetic; MED for the SSA value naming used in the worked example (the binary-derived examples in the source pages use slightly different temp names).

## The Operation

The walkthrough operation is one TMA bulk-tensor load of a 128-row × 128-column BF16 tile from a global tensor into shared memory, on `sm_90a`, with one mbarrier slot acting as the completion barrier. The element type is `bf16` (2 bytes), so the tile carries `128 × 128 × 2 = 32 768` bytes — that integer is the transaction-byte count every layer eventually publishes against the barrier. The CTA hosts a single warp group (128 threads) doing the load on behalf of a consumer that will read the shared-memory tile.

The frontend constructed:

```python
a_view = make_partition_view(A, [M, K], tile=(128, 128), dim_map=[0, 1])
a_tile = load(a_view, (block_m, block_k))   # tile<128x128xbf16>
```

`A` is a global-memory `bf16` tensor of shape `[M, K]` with row-major strides `[K, 1]`. `block_m` and `block_k` are CTA-supplied tile coordinates. The load completes asynchronously — the consumer side waits on an mbarrier before reading `a_tile`, but that wait is a separate operation. This page traces the load itself.

The transaction-byte arithmetic that runs through every stage:

```
tile_bytes = tile_rows * tile_cols * sizeof(bf16)
           = 128 * 128 * 2
           = 32768 bytes
```

That single integer is the value `nv_tileas.async.tiled_tma_load` carries as its `tx_count` attribute, `nvvm.mbarrier.arrive.expect_tx` publishes against the barrier, and the consumer side sees as `expected_txn` in the mbarrier state machine documented in [mbarrier State Machine — State Machine](mbarrier-state-machine.md#state-machine).

## Stage 1: cuda_tile IR

The first IR the compiler sees comes out of the frontend's bytecode. The load is a `cuda_tile.load_view_tko` — token-ordered tile load from a partition view — and the verifier contract on the operation is the standard cuda_tile contract: power-of-two tile dimensions, a 16-million-element ceiling per tile, a token operand for ordering, and an explicit tile-typed SSA result.

```mlir
%a_view  = cuda_tile.tensor_view %A, shape = [%M, %K], stride = [%K, 1]
         : !cuda_tile.tensor_view<128x128xbf16>
%a_part  = cuda_tile.partition_view %a_view, tile = [128, 128], dim_map = [0, 1]
         : !cuda_tile.partition_view<128x128xbf16>

%tok0    = cuda_tile.create_null_token : !cuda_tile.token
%a_tile, %tok_a = cuda_tile.load_view_tko %a_part, [%bm, %bk], %tok0
                : !cuda_tile.tile<128x128xbf16>, !cuda_tile.token
```

There is no descriptor, no mbarrier, no transaction-byte count, and no TMA mention. `cuda_tile` is the public surface and deliberately stays target-agnostic: a partition view plus tile coordinates plus an ordering token is all the frontend has to publish. The `_tko` suffix denotes the token-ordered shape — the load consumes an input token and produces an output token, so subsequent loads and stores can be scheduled against the ordering edge rather than against explicit fences. The optional `allow_tma` attribute (defaulting to true on `sm_90a` and later) is what the next pass reads to decide whether the load becomes a TMA copy or falls back to plain `ldg`. See [cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md) for the conversion target this op is illegal against.

> ⚡ **QUIRK — `cuda_tile` carries no descriptor, no mbarrier, no tx-count**
> The public dialect has no syntax for a TMA descriptor, no syntax for an mbarrier slot, and no transaction-byte attribute. Every TMA-specific noun first appears in `nv_tileaa` (the copy-atom witness) or `nv_tileas` (the descriptor handle, the mbarrier slot, the `tx_count`). A reimplementer who tries to express any of those on the public surface has misread the contract — `cuda_tile` is a tile-algebra dialect, not a TMA-shaped dialect. The promotion to TMA is a downstream decision driven by the copy-atom registry, not a frontend gesture.

## Stage 2: nv_tileaa IR

`ConvertCudaTileToTileAA` rewrites the load through the three-populator structure documented in [cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md). Part B of that structure owns the memory and view families, so the rewrite for this load lives in the Part-B `tiled_load` pattern. The partition view dissolves into an `nv_tileaa.make_memref` plus an `nv_tileaa.addptr`, the tile type becomes an MLIR `tensor<128x128xbf16>`, and — the key change for this walkthrough — the load picks up a CopyAtom witness. The witness is an attribute that names the hardware copy primitive selected by the layout-assignment pre-pass; for an `sm_90a` load that meets the TMA eligibility rules (the box-dim invariants documented in [TileAS TMA and Memops Family — Descriptor Builders and Verifiers](../passes/tileas/tma-and-memops-family.md#descriptor-builders-and-verifiers)) the witness is `sm90_tma_load_2d_bf16`.

```mlir
%a_ref = nv_tileaa.make_memref %A, shape = [%M, %K], stride = [%K, 1],
                               space = #nv_tileaa.global
       : !nv_tileaa.memref<?x?xbf16>
%off_a = nv_tileaa.addptr %a_ref, [%bm, %bk]
       : !nv_tileaa.memref<?x?xbf16>

%a_tile, %tok_a = nv_tileaa.tiled_load %off_a, %tok0
                { atom = #cute_nvgpu.copy_atom<sm90_tma_load_2d_bf16>,
                  in_bounds = array<i1: true, true>,
                  mem_semantic = #nv_tileaa<mem_semantic relaxed>,
                  mem_scope = #nv_tileaa<mem_scope cluster> }
                : !nv_tileaa.memref<?x?xbf16> -> tensor<128x128xbf16>,
                  !nv_tileaa.mem_token
```

The CopyAtom witness is the single most consequential change. `nv_tileaa.tiled_load` does not commit to a particular hardware primitive — that decision rides on the attribute. A different atom in the same slot (`sm80_cp_async_4_bf16`, `sm70_ldg_128_bf16`, `sm90_tma_load_2d_bf16_mcast`) would steer the next stage's rewrite into a different lowering path. Layout assignment runs before this pass and is what consults the copy-atom registry documented in [SM-Tier Roster and Copy Atom Registry](../dialects/cute_nvgpu/sm-tier-roster-and-copy-atom-registry.md); after this pass the witness travels verbatim down to the LLVM lowering.

Token ordering also takes its alias-aware form. The output token `%tok_a` is now an `!nv_tileaa.mem_token`, threaded into the next memory op so the scheduler can reason about read-after-write and write-after-read edges without inserting fences. The `mem_semantic = relaxed` and `mem_scope = cluster` operands together declare that the load is observably ordered against other `cluster`-scope traffic but does not impose an acquire fence.

The transaction-byte count is still implicit. The element type (`bf16`) and the tile shape (`128 × 128`) determine it, but no attribute yet records the integer.

### CopyAtom Witness Selection

The witness attached to the `tiled_load` is not free-form text. The dialect interface `CopyAtomAttrInterface` constrains every valid witness to one of the entries in the SM-tier copy atom registry, and the layout-assignment pre-pass picks among them by reading three pieces of information from the operand context: the compute capability (the `--compute-capability` driver option, parsed once and threaded onto the function as `nv_tileaa.compute_capability`), the source-memref's address space (global versus shared), and the destination tile's shape and element type.

For this walkthrough's load, layout assignment sees `sm_90a`, address-space-1 (global) source, and a `128 × 128 × bf16` destination. The matching atom in the SM-tier registry is `sm90_tma_load_2d_bf16` with `swizzle_128B` — the swizzle mode is selected so that consecutive K-axis vectors in the destination shared-memory tile fall in different SMEM banks, avoiding bank conflicts when the consumer's WGMMA reads through 128-bit `ldmatrix.sync.aligned`-style fragments. On Ada Lovelace (`sm_89`) the same source `cuda_tile.load_view_tko` would resolve to `sm80_cp_async_4_bf16` because TMA hardware is not available; on Ampere (`sm_80`) the same atom; on Volta or earlier, a plain `sm70_ldg_128_bf16`.

The witness is also the gate `LowerTMALoadStoreToAsync` reads in phase 2 of its eight-phase walk. Atoms that do not implement `TmaAtomTypeInterface` (the plain `ldg`, `stg`, `ldgsts` family) are skipped in phase 2 without rewrite, so the TMA expansion documented in Stage 3 of this page never runs for them. The atom-to-interface dispatch is the central decision that pins the rest of the lowering — picking a non-TMA atom in the witness slot keeps the load as a synchronous `tiled_load` all the way down to the LLVM stage.

## Stage 3: nv_tileas IR

`ConvertTileAAToTileAS` keeps the same operand shape but renames the op and updates dialect namespaces. The TileAS rewrite documented in [TileAA to TileAS — `tiled_load` Witness Hand-Off](../lowering/tileaa-to-tileas.md) preserves the CopyAtom witness verbatim, swaps the mnemonic prefix from `nv_tileaa` to `nv_tileas`, and leaves the rest of the operand vector unchanged.

```mlir
%a_tile, %tok_a = nv_tileas.tiled_load %off_a, %tok0
                { atom = #cute_nvgpu.copy_atom<sm90_tma_load_2d_bf16>,
                  in_bounds = array<i1: true, true>,
                  mem_semantic = #nv_tileas<mem_semantic relaxed>,
                  mem_scope = #nv_tileas<mem_scope cluster> }
                : !nv_tileas.memref<?x?xbf16> -> tensor<128x128xbf16>,
                  !nv_tileas.mem_token
```

Then the [TileAS TMA and Memops Family](../passes/tileas/tma-and-memops-family.md) pipeline runs, with `LowerTMALoadStoreToAsync` doing the heavy work. The eight-phase walk (KernelSpec gate, TMA-eligibility scan, `tmaIdx` assignment, descriptor bind, async op materialization, mbarrier emission, wait sinking, diagnostic finalization) is documented in [TileAS TMA and Memops Family — LowerTMALoadStoreToAsync](../passes/tileas/tma-and-memops-family.md#lowertmaloadstoretoasync). The output is the four-op sequence the downstream lowering expects: descriptor build, async TMA op, mbarrier expect-tx, mbarrier wait.

```mlir
// ---- descriptor materialized by phase 4
%desc_a = nv_tileas.make_tiled_tma_desc %a_ref, box = [128, 128],
          atom = #cute_nvgpu.tma_atom<load_2d_bf16, swizzle_128B>,
          tmaIdx = 0 : i32
        : !nv_tileas.tma_desc<128x128xbf16>

// ---- mbarrier reserved by phase 6 (one slot, count=1, expecting one arrival)
%mbar_a = nv_tileas.alloc_mbarrier { count = 1 : i32 }
        : !nv_tileas.mbarrier

// ---- async TMA load: phase 5 emission
%tok_a = nv_tileas.async.tiled_tma_load
           %desc_a, %a_smem[%bm, %bk], %mbar_a
           { atom = #cute_nvgpu.tma_atom<load_2d_bf16, swizzle_128B>,
             tx_count = 32768 : i32,
             tmaIdx = 0 : i32 }
         : !nv_tileas.tma_desc<128x128xbf16>,
           !nv_tileas.smem<128x128xbf16>,
           index, index,
           !nv_tileas.mbarrier
         -> !nv_tileas.AsyncToken
```

Three new entities appear at this stage. First, the **TMA descriptor** is a first-class SSA value — `%desc_a` is the result of `make_tiled_tma_desc`, which captures tensor shape, stride, padding mode, descriptor mode (tiled), element type, and the `cute_nvgpu.tma_atom` witness with its swizzle. The descriptor's `tmaIdx` attribute is the per-function counter assigned in phase 3 of `LowerTMALoadStoreToAsync`; downstream the `AttachTMADescriptorArgs` pass (documented in [TileAS TMA and Memops Family — Descriptor ABI](../passes/tileas/tma-and-memops-family.md#descriptor-abi)) reads it to wire host descriptor preparation back to device descriptor consumption. Second, the **mbarrier slot** is an explicit `!nv_tileas.mbarrier` SSA value with an `arrive_count` of 1 — one producer agent will publish completion. Third, the **transaction-byte count** is a concrete `tx_count = 32768 : i32` attribute on the async load. That integer is the byte count the consumer's `mbarrier.try_wait.parity` will check against.

> ⚡ **QUIRK — transaction-byte count is per-atom, not per-mbarrier**
> A single mbarrier slot can receive transaction-byte updates from multiple TMA loads (one per operand in a multi-input WGMMA, for instance). The `tx_count` attribute stamped on each `nv_tileas.async.tiled_tma_load` is the byte count *that load* will contribute; the consumer's `expected_txn` field on the mbarrier is the sum across all loads that arrive on the same barrier. A reimplementation that publishes a per-mbarrier total at descriptor-build time and ignores per-atom byte counts produces a barrier whose `expected_txn` never matches the actual transaction total, and `try_wait.parity` either fires early (if the published total is too small) or hangs forever (if too large). The per-atom accounting is what makes the multi-operand WGMMA producer/consumer handshake work.

> ⚡ **QUIRK — CopyAtom witness vs concrete PTX form**
> The CopyAtom witness `sm90_tma_load_2d_bf16` does not name the PTX mnemonic the load eventually becomes. It names a *family*: the basic 2D tile load is `cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes`, but the multicast variant of the same atom prints `…multicast::cluster`, the L2-cache-hint variant prints `…L2::cache_hint`, and the im2col atom variant prints `…im2col`. The witness names the legal shape; per-load options the rewriter discovers (an attached multicast mask, an attached cache hint, an im2col mode flag) select among the printable variants. A naive name-to-mnemonic mapping in a reimplementation skips the variant gates the codegen page documents in [TMA, Tensormap, and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md).

### Descriptor Lifecycle and the Host/Device Split

The `nv_tileas.make_tiled_tma_desc` op materialized above is one of two possible descriptor origins. `AttachTMADescriptorArgs` and `SeparateHostTMA` (documented in [TileAS TMA and Memops Family — Descriptor ABI](../passes/tileas/tma-and-memops-family.md#descriptor-abi)) split the descriptor population between host and device. When the descriptor depends only on values the runtime can supply through the launch ABI (the kernel's global pointer arguments, the tensor shape and stride parameters, the box-dim attribute that came from the partition_view), the pass hoists descriptor construction into a host-side companion module that builds a 64-byte CUDA tensormap once per launch. When the descriptor depends on values only the device knows (a runtime-computed shape, a divergent stride), it stays on the device and the kernel emits a `cp.async.bulk.tensor.encode` sequence inline before the load.

For this walkthrough, the descriptor depends only on the kernel's `%A`, `%M`, and `%K` arguments, so the host-side path wins. The kernel ABI grows a `.param` slot holding a pointer to the descriptor, the slot carries the `cute_nvgpu.grid_constant` argument attribute (later lifted to `nvvm.grid_constant`), and the device-side `make_tiled_tma_desc` op survives only as a marker the next stage's pattern looks up by `tmaIdx = 0`. The runtime's per-launch descriptor-emit callback runs once per launch and writes 64 bytes per descriptor into a scratch buffer the kernel reads through its `.param` slot.

The descriptor's 64-byte payload encodes the eleven fields the [TileAS TMA and Memops Family — Tensormap Mutators](../passes/tileas/tma-and-memops-family.md#tensormap-mutators) page documents: global base address, per-axis dimension sizes, per-axis strides, element type, rank, format (tiled / im2col / im2col_at / tiled_at), box shape, swizzle mode, fill mode, OOB fill value, and interleave layout. Eight of the eleven are immutable on device (set at construction, never replaced); only the global base pointer, per-axis sizes, and non-leading strides can be replaced via `tensormap.replace.{global_address,dim_size,stride_size}` if the kernel needs to vary them across iterations.

## Stage 4: NVVM Intrinsic in LLVM IR

`ConvertTileASToLLVM` is the terminal MLIR-side lowering, and its nine-phase body conversion documented in [tileas to LLVM](../lowering/tileas-to-llvm.md) carries the load to LLVM. The TMA-load rewrite specifically follows the five-step pattern in [tileas-to-llvm — `async.tiled_tma_load`](../lowering/tileas-to-llvm.md#asynctiled_tma_load--nvvmcpasyncbulktensorsharedglobal): the descriptor becomes an `llvm.ptr<1>` to its global-memory home, the destination view becomes an `llvm.ptr<3>` to the shared-memory base address, the per-axis coordinates flow through unchanged as `i32` values, and the mbarrier slot becomes an `llvm.ptr<3>` to the completion barrier.

```llvm
; ---- descriptor pointer (taken from the kernel's grid-constant .param slot)
%desc_a_ptr = call ptr addrspace(1) @llvm.nvvm.tma.get.descriptor.address(i32 0)

; ---- mbarrier publishes expected transaction-byte count
call void @llvm.nvvm.mbarrier.arrive.expect_tx.shared(
    ptr addrspace(3) %mbar_a, i32 32768)

; ---- TMA bulk-tensor load: shared <- global, through the descriptor,
;      coordinate order reversed to inner-axis-first (column-major)
call void @llvm.nvvm.cp.async.bulk.tensor.shared.cluster.global.2d(
    ptr addrspace(3) %a_smem_dst,    ; destination: shared base address
    ptr addrspace(1) %desc_a_ptr,    ; source: TMA descriptor in global
    i32 %bk_coord,                   ; coord[0]: inner-axis (was %bm)
    i32 %bm_coord,                   ; coord[1]: outer-axis (was %bk)
    ptr addrspace(3) %mbar_a)        ; completion barrier
```

Four things change at the LLVM boundary. The CopyAtom witness is consumed — the intrinsic name `llvm.nvvm.cp.async.bulk.tensor.shared.cluster.global.2d` encodes everything the witness named (2D tile, shared destination, global source, mbarrier completion), so no attribute is needed. The transaction-byte count is published in its own instruction: `llvm.nvvm.mbarrier.arrive.expect_tx.shared` writes `32768` into the barrier's `expected_txn` field. Coordinate order **reverses** — TileAS lists coordinates outer-axis-first (`[%bm, %bk]`) to match layout-assignment's row-major convention, but the LLVM intrinsic expects inner-axis-first (`[%bk, %bm]`) to match the PTX instruction. And the `AsyncToken` SSA result becomes an `i32` zero constant: the token does not carry hardware state, only an IR-level data-dependence edge, so the lowering replaces it with a placeholder whose only purpose is keeping the SSA dataflow connected.

The mbarrier's role is decoupled here. The `mbarrier.arrive.expect_tx.shared` call is what *publishes* the byte count; the `cp.async.bulk.tensor` call is what *issues* the asynchronous transfer that will update the barrier's `txn_count` field asynchronously as bytes arrive in shared memory; a downstream `mbarrier.try_wait.parity.shared` (emitted by the consumer-side pattern, not by the load itself) is what *gates* the consumer on both arrival and transaction-byte completion. The three are independent instructions tied together only by the shared barrier pointer. See [mbarrier State Machine — Kinds: Ordinary, Transaction, Cluster](mbarrier-state-machine.md#kinds-ordinary-transaction-cluster) for the TMA-transaction kind's full state-machine view.

> ⚡ **QUIRK — failed transactions are silent UB**
> If a TMA load fails mid-flight — out-of-bounds coordinate, malformed descriptor, multicast mask referencing a CTA outside the cluster — the hardware does not raise an exception, does not signal the barrier, and does not abort the kernel. The transaction-byte count simply never reaches `expected_txn`, and the consumer's `mbarrier.try_wait.parity` spins forever (or, with a non-zero `ns` timeout, returns failure). No diagnostic surfaces from MLIR, LLVM, or `ptxas`; the failure mode is a hang. Reimplementations that assume any kind of error reporting from the load itself will misdiagnose this class of bug as a barrier mis-init. The only defense is the descriptor verifier — see [TileAS TMA and Memops Family — Descriptor Builders and Verifiers](../passes/tileas/tma-and-memops-family.md#descriptor-builders-and-verifiers) for the catalog.

## Stage 5: NVPTX MIR

The NVPTX backend's instruction selector ([ISelDAG and MatcherTable](../codegen/iseldag-and-matchertable.md)) consumes the LLVM intrinsic and produces a `MachineFunction` instruction. The TMA family of opcodes is a set of `CP_ASYNC_BULK_TENSOR_*_*` machine instructions, one per (rank, mode, destination, options) tuple. For the 2D tile load with mbarrier completion, the opcode is `CP_ASYNC_BULK_TENSOR_2D_SHARED_CLUSTER_GLOBAL_MBARRIER`.

```text
bb.loop:
  ; --- mbarrier arrives with expected transaction byte count
  MBARRIER_ARRIVE_EXPECT_TX_SHARED %mbar_a:b64, 32768

  ; --- TMA load: shared destination, global source via descriptor, mbarrier completion
  CP_ASYNC_BULK_TENSOR_2D_SHARED_CLUSTER_GLOBAL_MBARRIER
      %a_smem_dst:b64,                ; destination shared address (b64 SMEM ptr)
      %desc_a:b64,                    ; descriptor address (b64 global ptr)
      %bk:b32, %bm:b32,               ; coordinates, inner-axis first
      %mbar_a                         ; completion barrier (b64 SMEM ptr)
```

Three observations matter at MIR level. First, the opcode encodes the address spaces in its name — `_SHARED_CLUSTER_GLOBAL_` selects the variant whose destination is SMEM, whose source is global via descriptor, and whose completion handshake is the cluster-scope mbarrier transaction. A 1D variant would have `_1D_` in the slot; an im2col variant would have `_IM2COL_`; a multicast variant would have `_MULTICAST_`. Each is a distinct opcode in the NVPTX `.td` files, picked up by the AsmPrinter table to render the corresponding PTX modifier set. Second, the `MBARRIER_ARRIVE_EXPECT_TX_SHARED` instruction is the in-flight `expect_tx` publish: it writes `32768` into the barrier's `expected_txn` slot. The number is a literal immediate at this level, not a register; the rewriter has folded it from the `tx_count = 32768` attribute that originated in `nv_tileas.async.tiled_tma_load`. Third, the AsyncToken is gone — the LLVM-side i32 zero placeholder dies during instruction selection because no MIR opcode consumes it.

The transaction-byte count has now flowed through four levels of representation: implicit shape-and-element-type in `cuda_tile`, still implicit in `nv_tileaa`, explicit as `tx_count = 32768 : i32` attribute in `nv_tileas`, explicit as `i32 32768` operand to `llvm.nvvm.mbarrier.arrive.expect_tx.shared` in LLVM IR, and explicit as immediate operand `32768` to `MBARRIER_ARRIVE_EXPECT_TX_SHARED` in MIR.

## Stage 6: PTX Text

The AsmPrinter ([AsmPrinter and Per-SM Windows](../codegen/asm-printer-monster-and-windows.md)) walks the `MachineFunction` and renders each instruction. The 2D TMA load with mbarrier completion prints as `cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes`, the canonical Hopper TMA tile-load mnemonic.

```ptx
    // ---- mbarrier publishes the 32768-byte expectation
    mbarrier.arrive.expect_tx.shared.b64 _, [%rd_mbar], 32768;

    // ---- TMA bulk-tensor 2D load
    cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes
        [%rd_smem_dst],                 // destination: SMEM address of tile
        [%rd_desc_a, {%r_bk, %r_bm}],   // source: descriptor + coords (inner-axis first)
        [%rd_mbar];                     // completion barrier address
```

The mnemonic encodes seven independent decisions. `cp.async.bulk.tensor` is the family. `2d` is the descriptor rank. `shared::cluster.global` is the address-space pair — destination in CTA-scope shared memory visible to the cluster, source in global memory via descriptor. `tile` is the descriptor mode (versus `im2col`, `gather4`, etc.). `mbarrier::complete_tx::bytes` is the completion mechanism — the bytes-based transaction barrier that pairs with `mbarrier.arrive.expect_tx` and `mbarrier.try_wait.parity`. Each modifier maps back to a specific attribute that traveled from `nv_tileas.async.tiled_tma_load` through the LLVM intrinsic name into the MIR opcode suffix and finally into the printed mnemonic.

The coordinate operands `{%r_bk, %r_bm}` are inner-axis-first; the original `nv_tileas` form was outer-axis-first `[%bm, %bk]`. The reversal happened in the LLVM lowering, propagated through MIR, and surfaces here at print time.

The transaction-byte count `32768` is the literal immediate to `mbarrier.arrive.expect_tx`. The load itself does not carry a byte-count operand — the load's job is to issue the transfer and have the hardware update the barrier's `txn_count` field; the byte expectation was set out-of-band by the `expect_tx` instruction.

> ⚡ **QUIRK — `mbarrier::complete_tx::bytes` is a load modifier, not an mbarrier modifier**
> The `mbarrier::complete_tx::bytes` qualifier appears in the `cp.async.bulk.tensor.*` mnemonic, not in the `mbarrier.*` mnemonic. It selects the transaction-completion behaviour of the bulk-tensor instruction — the load updates an mbarrier's `txn_count` field as bytes arrive — and does *not* describe how the barrier itself is built or armed. The barrier's `expected_txn` is established by a *separate* `mbarrier.arrive.expect_tx` instruction issued before the load. Reimplementations that emit only the bulk-tensor instruction and rely on the mnemonic to "publish" the byte count produce a barrier whose `expected_txn` stays at zero, so `try_wait.parity` returns success immediately (the `txn_count >= expected_txn` check is vacuous at zero), and the consumer reads garbage from a still-uncopied destination.

## Transaction-Byte Count: Cross-Stage Flow

The single integer `32768` is the canonical thread tying the load to the consumer. It is computed exactly once — `128 rows × 128 cols × 2 bytes/element = 32 768 bytes` — but lives under different names and at different levels of abstraction at every stage. Its journey:

| Stage | Form | Carrier | Source |
|---|---|---|---|
| 1 — `cuda_tile` | implicit | tile shape `<128x128xbf16>` | derived at lower time |
| 2 — `nv_tileaa` | implicit | tensor shape `<128x128xbf16>` plus CopyAtom witness | derived at lower time |
| 3 — `nv_tileas` | explicit attr | `tx_count = 32768 : i32` on `async.tiled_tma_load` | computed by `LowerTMALoadStoreToAsync` phase 5 |
| 4 — LLVM IR | explicit i32 operand | argument to `llvm.nvvm.mbarrier.arrive.expect_tx.shared` | folded from `tx_count` attribute |
| 5 — NVPTX MIR | explicit immediate | operand to `MBARRIER_ARRIVE_EXPECT_TX_SHARED` | selected through ISelDAG |
| 6 — PTX text | explicit literal | last operand to `mbarrier.arrive.expect_tx.shared.b64` | rendered by AsmPrinter |

The transition from implicit (stages 1–2) to explicit (stages 3–6) happens in phase 5 of `LowerTMALoadStoreToAsync`, the same phase that materializes the async TMA op itself. Until that phase runs, the byte count exists only as a derivable consequence of the tile shape and the element type; after that phase runs, it is a first-class attribute that travels verbatim through every subsequent lowering. The consumer side reads it through the mbarrier's `expected_txn` field, exactly as the [mbarrier State Machine](mbarrier-state-machine.md#state-machine) `try_wait.parity` predicate documents.

> ⚡ **QUIRK — tx_count diverges from naive byte count under swizzle**
> The byte count published to `expect_tx` is the number of bytes the TMA hardware will actually deposit into shared memory, which equals the tile size in bytes when the descriptor's swizzle mode is `none`. When the swizzle mode is `128B`, `64B`, or `32B`, the hardware deposits the *unswizzled* byte count — the data after swizzle reordering still occupies the same number of bytes, even though their layout in SMEM is permuted. A reimplementer who computes `tx_count = tile_rows * tile_cols * sizeof(elem)` is correct for tiled mode regardless of swizzle, but the same formula does *not* generalize to im2col mode (where padded rows expand the byte count beyond `rows × cols × sizeof`) or to multicast (where the byte count is per receiving CTA, not aggregated). The phase-5 computation in `LowerTMALoadStoreToAsync` reads the atom's descriptor metadata to get the right answer for each mode.

## Stage 7: SASS

Past the PTX text, the path leaves tileiras and enters `ptxas`'s territory through the boundary documented in [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md). The assembler renders the `cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes` mnemonic into the SASS instruction stream — instruction encodings, register allocation, and scheduling are entirely `ptxas`'s decision. The byte count `32768` becomes part of the encoded immediate operand of the SASS `MBARRIER` instruction; the load itself decomposes into a sequence of `UTMALDG` and related SASS instructions that issue the transfer and arm the L1 cache fill paths.

That layer is out of scope for tileiras's documentation. The wiki covers the path up to PTX text; everything below the handoff is `ptxas` territory, including the SASS opcode encoding for the bulk-tensor family and the SM scheduling decisions that interleave the asynchronous TMA issue against the warp group's compute.

## Coordinate Reversal in Detail

The coordinate operands flip between outer-axis-first and inner-axis-first ordering exactly once in the cascade — between Stage 3 (`nv_tileas`) and Stage 4 (LLVM IR). Tracking which axis ordering each stage uses is necessary for any reimplementation that wants to walk an IR dump and validate the coordinate operand order without re-reading the source patterns.

The convention at each stage:

| Stage | Coordinate order | Why |
|---|---|---|
| 1 — `cuda_tile.load_view_tko` | outer-axis-first `[%bm, %bk]` | matches Python-style tile indexing in the frontend bytecode |
| 2 — `nv_tileaa.tiled_load` | outer-axis-first `[%bm, %bk]` | preserved verbatim through Part-B rewrite for SSA-edge stability |
| 3 — `nv_tileas.async.tiled_tma_load` | outer-axis-first `[%bm, %bk]` | layout-assignment writes coordinates in row-major order |
| 4 — `nvvm.cp.async.bulk.tensor.shared.cluster.global.2d` | **inner-axis-first** `[%bk, %bm]` | matches the PTX instruction operand order |
| 5 — `CP_ASYNC_BULK_TENSOR_2D_SHARED_CLUSTER_GLOBAL_MBARRIER` | inner-axis-first `[%bk, %bm]` | survives ISel verbatim |
| 6 — `cp.async.bulk.tensor.2d.shared::cluster.global.tile…` | inner-axis-first `[%bk, %bm]` | PTX consumes inner-axis-first |

The reversal happens inside the `async.tiled_tma_load` → `nvvm.cp.async.bulk.tensor.*` pattern in [tileas-to-llvm — `async.tiled_tma_load`](../lowering/tileas-to-llvm.md#asynctiled_tma_load--nvvmcpasyncbulktensorsharedglobal). The pattern walks the operand list and emits the operands in reverse coordinate order as part of the intrinsic call construction. Im2col variants get the same reversal applied to their coordinate vector, with the K-offset prefix preserved at the head of the operand list.

## Verifier Surface at Each Stage

Each stage's verifier catches a different class of malformed load. The same operation must satisfy every verifier on its path; a load that survives Stage 2 because Stage 1's verifier didn't notice an issue still fails at Stage 3 once the descriptor builder runs its 128-byte alignment check. The catalog by stage:

| Stage | Verifier | Sample diagnostics |
|---|---|---|
| 1 — `cuda_tile` | `cuda_tile.load_view_tko` verifier | "all dimensions must be positive constants, got …", "all dimensions must be powers of two, got …", "tile would exceed the maximum of …" |
| 2 — `nv_tileaa` | `nv_tileaa.tiled_load` verifier (inherited tile-dim invariants) | "expects N coordinates, but got M", "expects CoordType is same as memref index type", "view elementType not equal with tensor element type: …" |
| 3a — `nv_tileas.tiled_load` | inherited from TileAS shared `verify_tiled_memop` | "unsupported mem_semantic: acquire" (loads forbid acquire), "incorrect number of in_bounds elements: expected …" |
| 3b — `nv_tileas.make_tiled_tma_desc` | descriptor builder verifier | "expected tma descriptor pointer to have alignment at least 128", "tma boxDims[0] * elemTypeBitWidth is not a multiple of 16 bytes", "smem layout is not TMA compatible", "TmaLoad only support zero padding now" |
| 3c — `nv_tileas.async.tiled_tma_load` | post-rewrite atom verifier | "expect a tma_load atom type", "tmaBoxDim and atomBoxDim length mismatch", "mcast is not supported for TMA load with less than 128bytes per atom" |
| 4 — LLVM IR | shared `TypeConverter` + intrinsic-arity check | (catch-all for arity mismatches against `llvm.nvvm.cp.async.bulk.tensor.*` declarations) |
| 5 — NVPTX MIR | LLT-typed operand check at ISel | (rejects malformed b64/b32 operand bundles before they reach the AsmPrinter) |
| 6 — PTX text | `ptxas` directive verifier | (out of scope; documented under [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md)) |

The 128-byte descriptor alignment check at Stage 3b is the one most worth flagging: TMA descriptors *must* be 128-byte aligned, which is why the kernel ABI marks descriptor-pointer arguments as `grid_constant` (placed in `.param`, naturally 128-byte aligned) rather than `.global` (only 16-byte aligned in the general case). A reimplementation that drops the `grid_constant` attribute keeps the IR well-formed all the way through verifier, but `ptxas` rejects the resulting `cp.async.bulk.tensor` load with an alignment diagnostic at SASS-generation time.

## Address-Space Trail

Every operand of the load lives in a specific GPU address space, and the address-space attribution flows through the cascade in a different shape at each level. The trail for this walkthrough's operands:

| Operand | Address space at each stage |
|---|---|
| Source tensor `%A` | space-1 (global) at every stage |
| TMA descriptor `%desc_a` | space-1 (global), with `grid_constant` mark routing it to `.param` |
| Destination tile (SMEM) | space-3 (shared) starting at Stage 3, allocated from `global_smem` |
| Mbarrier slot `%mbar_a` | space-3 (shared), 64-bit aligned, lives in `global_smem` |
| Loop iterator / coordinates | space-0 (generic register) |

The descriptor's address-space story is the subtlest of the five. `make_tiled_tma_desc` produces a value typed `!nv_tileas.tma_desc<…>`, an opaque type that the [Shared LLVM Type Converter](../lowering/pattern-set-and-typeconverter.md#shared-llvm-type-converter) lowers to `!llvm.ptr<1>` — an LLVM global-space pointer. The descriptor itself lives in global memory at launch time, but the *pointer* to it is passed through the kernel ABI in `.param` space. The `cute_nvgpu.grid_constant` argument attribute is what tells the codegen "this argument is a `.param` slot containing a pointer that, when dereferenced, lands in global memory." The downstream cute-to-llvm lowering at `sub_1698C20` lifts that attribute to `nvvm.grid_constant`, which is the form `ptxas` reads for the `.param` placement decision.

The mbarrier slot's address-space story is the simplest: it is always `.shared`. `nvvm.mbarrier.init.shared`, `nvvm.mbarrier.arrive.expect_tx.shared`, `nvvm.mbarrier.try_wait.parity.shared` — every member of the 21-op NVVM family for the cases this walkthrough exercises takes the `.shared` variant, because mbarrier hardware lives in CTA-scope SMEM. The non-`.shared` variants exist for cluster-scope mbarriers reached through `nvvm.mapa` (peer-CTA address translation), but those are out of scope for a single-CTA tile load.

## Mbarrier Slot Allocation and Reuse

The `nv_tileas.alloc_mbarrier` op produced in Stage 3 carves a 64-bit barrier out of the kernel's SMEM arena. The buffer-assignment pass documented in [Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) is what decides the offset; for this walkthrough the slot lives at a fixed offset past the tile-storage region of `global_smem`. The barrier is initialized once at kernel prologue with `nvvm.mbarrier.init.shared` — the `arrive_count` matches the number of producer arrivals the load contributes (one, for a single-issue load), and the `expected_txn` field starts at zero and is set by the first `mbarrier.arrive.expect_tx` that publishes against it.

In a pipelined kernel — the steady-state shape documented in [DSL to PTX End-to-End — Stage 3: nv_tileas IR](dsl-to-ptx-end-to-end.md#stage-3-nv_tileas-ir-after-scheduling) — the same mbarrier slot is reused across multiple iterations under different phase parities. The phase bit on the barrier flips on every completion, so iteration `i` and iteration `i+1` see opposite parities on the same slot; the consumer's `try_wait.parity` reads the iteration-derived parity from the loop's pipeline iterator state. For a depth-`D` pipeline the slot at stage index `s` carries phase `(i / D) & 1` on iteration `i`, and the producer's `expect_tx` flips the phase implicitly through the arrive-with-expect-tx machinery the [mbarrier State Machine — State Machine](mbarrier-state-machine.md#state-machine) page documents.

This is why the load alone does not need to specify a phase: the load increments the in-flight transaction byte count, and `mbarrier_arrive` flips the phase when `pending` reaches zero — exactly one arrival is enough for the single-producer load. The consumer reads the iteration phase from its pipeline iterator and asks `try_wait.parity` for that phase. The phase invariant is what makes a single barrier slot reusable across iterations without ABA hazards.

## Capability Cross-Check

The walkthrough above targets `sm_90a`. The same `cuda_tile.load_view_tko` would produce a different cascade on every other supported architecture; the table below summarises the divergence so a reimplementer can predict what to expect under a different `--compute-capability` value.

| Compute capability | CopyAtom witness | Stage-3 op | Stage-6 PTX mnemonic |
|---|---|---|---|
| `sm_70` (Volta) | `sm70_ldg_128_bf16` | `nv_tileas.tiled_load` (no rewrite) | `ld.global.nc.v4.b32` (per-thread vectorised) |
| `sm_75` (Turing) | `sm70_ldg_128_bf16` | same | `ld.global.nc.v4.b32` |
| `sm_80` (Ampere) | `sm80_cp_async_4_bf16` | `nv_tileas.async.cp_async` | `cp.async.ca.shared.global.4` |
| `sm_89` (Ada) | `sm80_cp_async_4_bf16` | same | same |
| `sm_90a` (Hopper) | `sm90_tma_load_2d_bf16` | `nv_tileas.async.tiled_tma_load` | `cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes` |
| `sm_100` (Blackwell) | `sm90_tma_load_2d_bf16` (inherited) | `nv_tileas.async.tiled_tma_load` | same TMA mnemonic; consumer is `tcgen05.mma` not WGMMA |
| `sm_120` (consumer Blackwell) | `sm90_tma_load_2d_bf16` | same | same |

The transition between `sm_89` and `sm_90a` is where the TMA descriptor, the mbarrier transaction-byte machinery, and the asynchronous `cp.async.bulk.tensor` instruction first enter the cascade. Below that boundary the load is synchronous (`ldg`) or coroutine-style async with per-thread `cp.async`; above that boundary it is the bulk-tensor instruction the rest of this page traces. See [Matmul Progression by SM](matmul-progression-by-sm.md) for the parallel progression on the WGMMA / tcgen05 consumer side.

## Consumer-Side Pairing

The load alone is half the story. Once the producer issues `cp.async.bulk.tensor` and `mbarrier.arrive.expect_tx`, the consumer needs to wait until the destination tile is ready. That wait is a separate operation — `nv_tileas.async.pipeline.consumer_wait` at the TileAS level, lowering to `nvvm.mbarrier.try_wait.parity.shared` at the LLVM level, and surfacing as `mbarrier.try_wait.parity.shared.b64` in PTX. The consumer wait is documented in detail in [DSL to PTX End-to-End — Stage 4: LLVM IR with NVVM intrinsics](dsl-to-ptx-end-to-end.md#stage-4-llvm-ir-with-nvvm-intrinsics) and in [mbarrier State Machine — Phase Parity](mbarrier-state-machine.md#phase-parity).

The wait's release predicate is exactly what the state-machine table in [mbarrier State Machine — State Machine](mbarrier-state-machine.md#state-machine) shows: `phase == want_phase && pending == arrive_count && txn_count >= expected_txn`. The third clause is what the transaction-byte machinery in this walkthrough sets up — the load updates `txn_count` asynchronously, and the wait releases only once that field crosses the `expected_txn = 32768` threshold the `expect_tx` instruction published.

The WGMMA consumer that typically reads the loaded tile is documented in [WGMMA Emission Protocol](wgmma-emission-protocol.md). The end-to-end producer/consumer pipeline that wires the TMA load to the WGMMA consumer through a multi-stage mbarrier ring is documented in [DSL to PTX End-to-End — Stage 3: nv_tileas IR](dsl-to-ptx-end-to-end.md#stage-3-nv_tileas-ir-after-scheduling).

## Reimplementation Checklist

Anyone reproducing a one-shot TMA load from a higher-level IR should walk the same six gates this page traces, in order. The checklist mirrors the cascade:

1. Pick a CopyAtom whose interface tag (`TmaAtomTypeInterface`) marks it as a TMA candidate. Anything else stays synchronous.
2. Verify the box-dim invariants: leading dim's bit-width must be a 16-byte multiple, descriptor pointer 128-byte aligned, smem layout TMA-compatible. The descriptor builder verifier catches every violation.
3. Compute `tx_count` from atom metadata, not from the naive `rows × cols × sizeof(elem)` shortcut. Im2col and multicast change the formula.
4. Reserve exactly one mbarrier slot per pipeline stage, not per producer or per atom. Multiple atoms publishing to the same slot is normal; the slot's `expected_txn` is the sum.
5. Reverse coordinate order at the TileAS-to-LLVM boundary and only there. Earlier reversals corrupt scheduling decisions; later reversals corrupt the PTX operand order.
6. Pair every load with a downstream consumer wait on the same mbarrier and the same parity. The wait is *not* a property of the load — it is a separate operation, emitted by a separate pattern, against a separately-tracked phase.

Skipping any of these six steps yields a kernel that either fails verifier mid-pipeline, fails `ptxas` at SASS time, or hangs forever at runtime. The QUIRK callouts above flag the most error-prone of the six.

Two further constraints are worth flagging because they are easy to miss when working backward from a PTX dump. First, the `nv_tileaa.kernel_spec` attribute must be present on the function before `LowerTMALoadStoreToAsync` runs — its absence fires `"LowerTMALoadStoreToAsync: missing or invalid KernelSpecAttr on function"` and skips every TMA rewrite, leaving the IR with `tiled_load` ops that the downstream verifier rejects. Second, the function-level `nv_tileas.num-host-tmas` and `nv_tileas.num-device-tmas` counters must agree with the actual `tmaIdx` range; `"tmaIdx exceed tmaHostNum."` and `"tmaIdx exceed tmaDeviceNum."` are emitted by the ABI verifier when they don't.

## Cross-References

[DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) is the kernel-wide walkthrough this page narrows; it shows the same kernel at every stage with all operations in place and traces the full producer/consumer pipeline through scheduling.
[mbarrier State Machine](mbarrier-state-machine.md) is the canonical reference for the barrier object's state machine, the 21-op NVVM family that touches it, and the three barrier kinds (ordinary, TMA-transaction, cluster-transaction); this walkthrough exercises the TMA-transaction kind end-to-end.
[WGMMA Emission Protocol](wgmma-emission-protocol.md) is the consumer-side companion: the four-op `wgmma.fence` / `mma_async` / `commit_group` / `wait_group` sequence runs once the `try_wait.parity` on the load's mbarrier releases.
[TileAS TMA and Memops Family](../passes/tileas/tma-and-memops-family.md) covers the eight-phase `LowerTMALoadStoreToAsync` pass, descriptor builders, ABI separation between host and device descriptors, and the full diagnostic catalog from the verifier.
[cuda_tile to nv_tileaa](../lowering/cuda-tile-to-tileaa.md) documents the first lowering stage (Stage 1 → Stage 2 in this walkthrough), the three-populator structure, and the CopyAtom-attaching layout-assignment pre-pass.
[nv_tileaa to nv_tileas](../lowering/tileaa-to-tileas.md) covers the alias-aware-to-assembler-near rewrite (Stage 2 → Stage 3) and the witness hand-off shape that preserves the CopyAtom attribute verbatim.
[nv_tileas to LLVM](../lowering/tileas-to-llvm.md) is the terminal MLIR-side lowering (Stage 3 → Stage 4), with the five-step TMA-load rewrite, the coordinate reversal, and the nine-phase body conversion.
[TMA, Tensormap, and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md) is the codegen-side reference for the full `cp.async.bulk.tensor.*` mnemonic family, descriptor mutator ABI, and proxy-fence rules.
[TMA Atoms](../dialects/cute_nvgpu/tma-atoms.md) catalogues the `cute_nvgpu.tma_atom` witness shapes for every supported (rank, mode, element-type, swizzle, multicast) tuple, and the `make_exec_tma` binding step that pairs each atom with its mbarrier.
[SM-Tier Roster and Copy Atom Registry](../dialects/cute_nvgpu/sm-tier-roster-and-copy-atom-registry.md) is the registry the layout-assignment pre-pass consults to pick `sm90_tma_load_2d_bf16` over the alternatives.
[Matmul Progression by SM](matmul-progression-by-sm.md) and [Capability Matrix](../codegen/per-sm-emission-templates.md#capability-matrix) explain why the lowering chose the `sm_90a` TMA path; on Ampere or earlier the same `cuda_tile.load_view_tko` would lower to `cp.async` or plain `ldg` with no TMA descriptor, no mbarrier transaction kind, and no `tx_count` attribute at all.
