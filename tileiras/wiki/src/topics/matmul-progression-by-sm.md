# Matmul Progression by SM

## Abstract

NVIDIA's matrix-multiply abstraction has evolved across seven SM generations. Each generation adds capacity along one of three axes — concurrency model (warp-cooperative → warp-group → cluster-cooperative), operand storage class (register fragments → SMEM descriptors → tensor memory), or numerical range (FP16 → FP8 → MXFP4 with block scales). Some generations also remove resource classes that earlier ones introduced: Blackwell datacenter parts drop the register-resident accumulator that WGMMA used, and Blackwell consumer parts drop tensor memory entirely while keeping the block-scale operand encoding.

This page is the canonical cross-architecture overview. It supersedes the scattered per-tier discussions in `dialects/cute_nvgpu/mma-atoms-sm70-120.md` (the per-arch shape lattice), the WGMMA and tcgen05 topic pages (which focus on one generation each), and `codegen/tcgen05-wgmma-mbarrier-cluster.md`. Those pages keep their per-tier content; this page covers the cross-architecture story.

## SM70 / SM75: Warp-Cooperative `mma.sync`

SM70 (Volta) and SM75 (Turing) introduced the first generation of tensor cores. The MMA instruction is `mma.sync`: warp-cooperative (32 threads cooperate on one tile), synchronous (the result is visible to the warp immediately after the instruction returns), and entirely register-resident (both operands and the accumulator live in the warp's register file).

The tile shapes are fixed and small. SM70 supports `8 x 8 x 4` with FP16 inputs and FP16 or FP32 accumulators. SM75 adds `16 x 8 x 8` with FP16, BF16, and the integer low-bit forms. The operand layouts are pinned by the architecture: each lane carries a specific subset of the matrix tile, and the layout grammar in `cute_nvgpu` exists in large part to record these per-lane subsets without losing them across pipeline transformations.

```text
emit:  mma.sync.aligned.m16n8k8.row.col.f16.f16.f16.f16 { %d0, %d1 }, { %a0, %a1 }, { %b0 }, { %c0, %c1 };
       (warp-cooperative, synchronous, all operands and accumulator in registers)
```

## SM80 / SM86 / SM87 / SM89: Dense and Sparse `mma.sync`

SM80 (Ampere A100) keeps the same warp-cooperative synchronous model but expands the shape lattice substantially: `16 x 8 x 16` with FP16 / BF16 / TF32 and a sparse `mma.sp.sync` variant that halves the structurally-sparse operand and adds a metadata operand. The lower SM80 derivatives (SM86, SM87) keep the same operations with smaller tensor-core arrays.

SM89 (Ada L40) adds FP8 E4M3 and E5M2 inputs to the same warp-cooperative synchronous register-MMA model. FP8 inputs always accumulate into FP32; the FP8 shape is `16 x 8 x 32` and the K extent doubles compared to FP16 because each element takes half the bits.

```text
emit:  mma.sync.aligned.m16n8k16.row.col.f16.f16.f16.f16 ...   (SM80)
       mma.sp.sync.aligned.m16n8k32.row.col.s8.s8.s32 ...     (SM80 sparse)
       mma.sync.aligned.m16n8k32.row.col.e4m3.e4m3.f32 ...    (SM89 FP8)
```

None of the SM80-tier MMAs touch shared memory directly — they read operands from registers. The kernel is responsible for staging tiles into registers, typically via `ldmatrix` from shared memory and `cp.async` into shared memory upstream.

## SM90 / SM90a: Warp-Group Async WGMMA

SM90 (Hopper H100) introduces the first asynchronous MMA: `wgmma.mma_async`. Four warps now cooperate on one accumulator tile (warp-group cooperative, hence WGMMA). The instruction is asynchronous against the issuing warps — it returns immediately, and the accumulator is not visible until a wait-group instruction drains the in-flight cohort.

The operand storage class changes too. Operand B is always an SMEM descriptor — a packed 64-bit word encoding base address, leading byte offset, stride byte offset, base offset, and swizzle mode. Operand A may be a register fragment or an SMEM descriptor depending on the atom variant. The accumulator stays in the warp group's register file, but is invisible until drained.

The four-op emission protocol — fence → tile loop of `mma_async` → commit → wait — is the contract a correct lowering must preserve. See [wgmma-emission-protocol](wgmma-emission-protocol.md) for details.

Shapes range over `64 x N x K` where M is fixed at 64 per instruction, N steps in multiples of 8 up to 256, and K is the canonical `256 / elem_bits` per element type. The architecture-qualified `sm_90a` variant is mandatory — plain `sm_90` rejects WGMMA at NVVM verification.

```text
emit:  wgmma.fence.sync.aligned;
       wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16 {...}, %a, %b_desc, %scale, ...;
       wgmma.commit_group.sync.aligned;
       wgmma.wait_group.sync.aligned 0;
       (warp-group cooperative, asynchronous, B in SMEM descriptor, accumulator in RF)
```

## SM100 / SM103: Tensor Memory and `tcgen05.mma`

SM100 (Blackwell B200) and SM103 (Blackwell Ultra GB300) remove WGMMA and replace it with `tcgen05.mma`. The concurrency model stays warp-group cooperative; the accumulator moves out of the register file and into tensor memory (TMEM), a new on-chip memory class. Operand A becomes either an SMEM descriptor or a TMEM pointer; operand B stays as an SMEM descriptor.

TMEM is per-SM, dense (128 rows per region), and reachable only from the `tcgen05` instruction family. The accumulator residency change is the single biggest architectural shift between WGMMA and tcgen05: a kernel that reads the accumulator must use `tcgen05.ld` to copy TMEM back into registers, not just observe the SSA value as on Hopper.

SM100 also adds two new variant axes:

- **Block-scaled** MMA for microscale formats (FP4, FP6, FP8) with per-block E8M0 or E4M3FN scale factors stored in dedicated TMEM regions.
- **Weight-stationary** mode that pins operand A to its TMEM region across the K loop, amortising A-side bandwidth.

The cluster-cooperative variant `cta_group::2` lets two CTAs in a cluster share an MMA tile; CTA 0 holds half of TMEM rows, CTA 1 holds the other half. A 4-CTA copy variant exists on the staging-copy side but not on the MMA side — Blackwell's 4-CTA semantics is a copy-time fan-out, and the MMA that follows is a plain single-CTA instruction over its slice. See [tcgen05-tensor-memory-model](tcgen05-tensor-memory-model.md).

```text
emit:  tcgen05.alloc.shared %h, 256;           // allocate TMEM region
       tcgen05.cp.smem.tmem ...;               // stage operand into TMEM
       tcgen05.mma.cta_group::1 %h_d, %a_desc, %b_desc, %h_scale, 1;
       (warp-group cooperative, asynchronous, A in SMEM/TMEM, B in SMEM, D in TMEM)
```

## SM120 / SM121: Consumer Blackwell Block-Scaled MMA

SM120 (consumer RTX 50-series and enterprise Pro) and SM121 (DGX Spark) are a different lineage from datacenter Blackwell. They keep the block-scaled operand encoding but remove tensor memory. The MMA is once again warp-cooperative (32 threads, like SM70-SM89), synchronous (no wait-group), and entirely register-resident.

The instruction is a synchronous `mma.sync.aligned` with two new per-operand operands: `scale_a` and `scale_b`, both E8M0 register fragments. Each operand carries one scale factor per `vecSize` elements along K; the legal `(K, vecSize)` combinations are `(32, 32)` for the FP4/FP6/FP8 family and `(64, 16)` or `(64, 32)` for FP4-only inputs.

The accumulator stays in registers. The MMA is synchronous, so there is no wait-group barrier. The operand-encoding is closer to SM89 than to SM100 — block-scale is a numerical-range expansion of the register-MMA model, not a concurrency-model change.

```text
emit:  mma.sync.aligned.m16n8k32.row.col.f4.f4.f32.block_scale
           { %d0, %d1, %d2, %d3 },
           { %a0, %a1 },           // FP4 operand A
           { %b0 },                // FP4 operand B
           { %c0, %c1, %c2, %c3 },
           { %sa },                // E8M0 scale factor for A
           { %sb };                // E8M0 scale factor for B
       (warp-cooperative, synchronous, all operands and accumulator in registers,
        block-scale operands in dedicated register fragments)
```

## What Each Generation Adds and Removes

| Tier | Concurrency | Operand A | Operand B | Accumulator | Sync | New |
|---|---|---|---|---|---|---|
| SM70/75 | warp (32 lanes) | RF | RF | RF | sync | dense `mma.sync`, FP16 |
| SM80 | warp (32 lanes) | RF | RF | RF | sync | sparse `mma.sp.sync`, BF16, TF32 |
| SM89 | warp (32 lanes) | RF | RF | RF | sync | FP8 E4M3 / E5M2 inputs |
| SM90a | warp-group (4 warps) | RF or SMEM desc | SMEM desc | RF (async-visible) | async | warp-group MMA, SMEM operand descriptors |
| SM100/103 | warp-group, optional 2-CTA cluster | SMEM desc or TMEM | SMEM desc | TMEM | async | tensor memory, block-scale, weight-stationary, sparse block-scale |
| SM120/121 | warp (32 lanes) | RF | RF | RF | sync | block-scale on consumer parts, no TMEM, no async |

The progression is not monotonic. SM90a moves the accumulator out of registers (sort of: still in the RF, but async-visible only). SM100 moves it the rest of the way out, into TMEM. SM120 moves it back into registers, but keeps the block-scale operand encoding that SM100 added. The right way to read the table is one column at a time: concurrency grows up to SM100 and then resets for consumer Blackwell; operand storage class climbs steadily through SM100 and then resets; numerical range grows monotonically.

## Cross-References

[MMA Atoms SM70-SM120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) carries the per-arch shape lattice and the dialect-side atom contracts.
[WGMMA Emission Protocol](wgmma-emission-protocol.md) covers the SM90a four-op protocol.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) covers the SM100/103 model and the 10-variant taxonomy.
[Mode Pattern Verifiers](../dialects/cute_nvgpu/mode-pattern-verifiers.md) carries the kind-word verifier ladder that gates SM100 and SM120 block-scaled variants.
[Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) documents the cluster-cooperative copy patterns that stage TMEM operands for SM100.
[mbarrier State Machine](mbarrier-state-machine.md) is the synchronisation primitive every async generation builds its producer/consumer protocol on top of.
