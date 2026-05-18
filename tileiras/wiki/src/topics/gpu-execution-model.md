# GPU Execution Model

## Abstract

NVIDIA GPUs execute kernels through a five-tier hierarchy: thread, warp, CTA, cluster, grid. Each tier has its own sync primitive, its own resource limit, and its own compiler-controlled launch attribute. Tileiras emits PTX directives at the kernel boundary that fix the launch contract, and intrinsics inside the body that target each tier's sync primitive. The choice of directive constrains every downstream decision — register allocation, occupancy, cluster-aware copy partitioning, and warp-group instruction legality all depend on the thread shape written at the `.entry` header.

The fence/arrive/wait protocols documented elsewhere assume this hierarchy is already established. This page is the canonical reference for the hierarchy itself: where each tier comes from, what synchronisation it offers, and how tileiras chooses the directive that pins the kernel to its shape.

## The Five-Tier Hierarchy

A kernel launch is a grid of clusters of CTAs of warps of threads. Each tier has a defined cardinality cap, a sync primitive, and a resource binding that lives at that tier.

| Tier | Max size | Sync primitive | Sync scope | Resource binding |
|---|---|---|---|---|
| Thread | 1 | sequential program order | — | per-thread register file slice |
| Warp | 32 threads | `shfl.sync`, `vote.sync`, `match.sync`, `redux.sync` | intra-warp | per-warp register file partition |
| CTA / thread-block | 1024 threads (32 warps) | `bar.sync` (16 NamedBarrier slots), `mbarrier.arrive`, `mbarrier.try_wait` | intra-CTA | per-CTA shared memory |
| Cluster (SM90+) | 8 CTAs | `cluster.arrive.relaxed` + `cluster.wait`, DSMEM read/write through `mapa` | intra-cluster | per-cluster DSMEM windows |
| Grid | unbounded | cooperative-groups grid sync (host-coordinated launch only) | grid-wide | global memory |

Three structural facts follow from the table. First, every sync primitive is intra-tier — there is no hardware `warp-to-warp` sync inside a CTA other than going through a CTA-level barrier, and no hardware `CTA-to-CTA` sync inside a cluster other than going through cluster arrive/wait. Second, every tier above the warp has a cardinality cap that the compiler must verify against the requested launch shape. Third, the resource binding follows the tier: registers belong to the warp, shared memory belongs to the CTA, DSMEM belongs to the cluster, and global memory is the only resource visible grid-wide.

The CTA cap of 1024 threads is fixed by hardware: every SM90+ device exposes the same 32-warp upper bound. The cluster cap of 8 CTAs is the SM90 portable maximum; portable cluster-launchable kernels must declare `.maxclusterrank 8` if they want to opt out of the cap. Below SM90 the cluster tier does not exist and the hierarchy collapses to four levels.

## Launch ABI

The path from host code to a running CTA crosses three layers: the CUDA driver, the GPU scheduler, and the SM front-end.

The host calls `cuLaunchKernel` (driver API) or `cuLaunchKernelEx` (extended API with cluster shape), or uses the runtime-API triple-chevron `kernel<<<grid, block, smem, stream>>>(args...)`. The driver packs kernel parameters into the GPU's per-launch parameter memory (the `.param` address space, AS=101 in NVVM IR) and dispatches the launch descriptor to the GPU's grid scheduler. The grid scheduler enumerates CTAs (or clusters of CTAs on SM90+) and dispatches each onto an SM that has enough free SMEM and warp slots to host it. Inside the SM the warp scheduler picks warps from the resident CTAs and feeds them into the issue pipeline; the per-thread register file is statically partitioned among the resident warps at CTA dispatch time.

```text
     host                       driver                       GPU
  ┌────────┐    cuLaunch    ┌──────────┐    submit       ┌────────────┐
  │ kernel │───────────────▶│ pack     │────────────────▶│ grid       │
  │ <<<>>> │                │ params   │                 │ scheduler  │
  └────────┘                │ AS=101   │                 └─────┬──────┘
                            └──────────┘                       │ dispatch CTA/cluster
                                                               ▼
                                                        ┌────────────┐
                                                        │ SM         │
                                                        │ - warp pool│
                                                        │ - SMEM     │
                                                        │ - regs     │
                                                        └─────┬──────┘
                                                              │ issue warps
                                                              ▼
                                                        ┌────────────┐
                                                        │ execution  │
                                                        │ pipeline   │
                                                        └────────────┘
```

The driver never sees the warp tier. Warp scheduling is internal to the SM and follows the launch's thread-shape declaration. If the kernel's `.reqntid` says 128 threads per CTA, the SM dispatches four 32-thread warps for every CTA it admits. If the kernel's `.maxnreg` says 168 registers per thread, the SM partitions the register file so that at most `register_file_size / (168 × 32)` warps from this kernel can be resident on the SM at one time.

Tileiras emits no host-side code. The host is responsible for the launch call; tileiras only writes the kernel's static contract into the cubin via `.entry` directives, and the runtime/driver reads those directives back when packing the launch descriptor.

## Cluster Execution Mechanics (SM90+)

Hopper introduces the cluster — a group of up to 8 CTAs scheduled together on adjacent SMs so they can read each other's shared memory and synchronise without going through global memory. The cluster is the only tier above the CTA that has hardware sync support; everything grid-wide must round-trip through cooperative-groups grid sync, which is host-coordinated and substantially more expensive.

Inside a cluster every CTA can:

- Read its position via `%cluster_ctarank` (0..7).
- Read the cluster size via `%cluster_dim` (one to eight).
- Map a local SMEM pointer into a peer's DSMEM window with `nvvm.mapa`, producing a pointer the issuing CTA can read/write but that physically lives in the peer's SMEM.
- Issue `nvvm.cluster.arrive.relaxed` to mark itself as ready, then `nvvm.cluster.wait` to block until every peer has arrived.

The DSMEM window has the same address space (`addrspace(3)`, SMEM) as a CTA's own shared memory; `mapa` returns a pointer to the peer's bank in that address space. Reads and writes use ordinary `ld.shared` / `st.shared` instructions — the hardware routes them across the cluster network when the address falls inside a peer window. The rendezvous protocol that consumes this — `mbarrier.expect_tx` paired with `cluster.arrive.relaxed` and `cluster.wait` — is documented in [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md).

Cluster shape is declared at the `.entry` header through `.cluster_dim X, Y, Z` (plus `.explicitcluster` and optionally `.maxclusterrank` and `.blocksareclusters`). The driver reads the declared cluster shape and packs CTAs into clusters of that size before dispatch; CTAs in the same cluster are guaranteed to land on SMs that share a cluster network, which is what makes DSMEM physically routable.

## Warp-Group Execution (SM90+)

Some SM90+ instructions are warp-group instructions: they require a 128-thread cooperating group (four contiguous warps) and read or write the warp group's register file as a unit. WGMMA on Hopper and `tcgen05.mma` on Blackwell are the canonical examples — they consume a 4-warp register block as the accumulator and one or two SMEM descriptors as inputs.

A warp-group instruction has three structural requirements:

1. The CTA's thread count must be a multiple of 128, since warps must align onto 4-warp groups without partials.
2. The warp group must be coherent — all four warps must reach the instruction together, or the ISA contract is broken.
3. Per-thread register usage must leave room for the warp group's accumulator fragment, since the accumulator lives in the register file.

Tileiras enforces (1) by emitting `.reqntid` (or `.maxntid`) with an X dimension that is a multiple of 128. The downstream lowering pass that emits WGMMA refuses to emit a `wgmma.mma_async` instruction when the kernel-spec's thread count is not a 128-multiple — the four-op protocol covered in [WGMMA Emission Protocol](wgmma-emission-protocol.md) needs four warps per group, and the scheduler's resource model assumes warp groups are atomic. Requirement (2) is the source of the `wgmma.fence.aligned` / `wgmma.commit_group.sync.aligned` / `wgmma.wait_group.sync.aligned` triple — each is `.aligned` precisely because it requires warp-group convergence. Requirement (3) is what drives the `.maxnreg` choice: a kernel that emits an `m64n256k16` WGMMA needs at least 32 FP32 registers per thread just for the accumulator slice, before counting descriptor and loop-index registers.

If the launch shape cannot satisfy these requirements, the legal options are to lower to a synchronous `mma.sync` form (slower, but available on every SM70+) or to refuse to compile and emit a diagnostic. Tileiras takes the second path when the kernel-spec explicitly requested a WGMMA atom but the thread shape disagrees.

## Kernel Attribute Decision

Every PTX directive at the `.entry` header has an MLIR attribute counterpart and a rule for when tileiras emits it. The verifier rules in [Host Launch ABI + ptxas Knobs](../driver/host-launch-and-ptxas-knobs.md) cover the well-formedness checks; the table below covers the decision policy — what input tileiras consults when picking each value.

| PTX directive | Source attribute | Tileiras input | Emission policy |
|---|---|---|---|
| `.entry kernel_name` | `nvvm.kernel` (UnitAttr) | LLVM function carries the marker | always emit when present; controls `.entry` vs `.func` |
| `.maxntid X, Y, Z` | `nvvm.maxntid` (1..3 i32) | upper bound from kernel-spec or user `__maxnreg__`-style hint | emitted when the upper bound matters for register budgeting |
| `.reqntid X, Y, Z` | `nvvm.reqntid` (1..3 i32) | user-declared block shape, or 128-multiple forced by WGMMA/tcgen05 emission | emitted when the kernel relies on an exact shape (warp-group or specialized warps) |
| `.minnctapersm N` | `nvvm.minctasm` (i32) | occupancy hint from kernel-spec | emitted when the user requested an occupancy floor |
| `.maxnreg N` | `nvvm.maxnreg` (i32) | per-thread register cap from kernel-spec | emitted to bound register usage and let ptxas trade registers for occupancy |
| `.explicitcluster` | implied by `nvvm.cluster_dim` presence | any `nvvm.cluster_dim` attribute on the function | always emitted with `.reqnctapercluster` on SM90+ |
| `.reqnctapercluster X, Y, Z` | `nvvm.cluster_dim` (exactly 3 i32) | user-declared cluster shape | emitted on SM90+ when `nvvm.cluster_dim` is present |
| `.maxclusterrank N` | `nvvm.maxclusterrank` (i32) | portability cap from kernel-spec | emitted on SM90+ when the user wants a portable cluster cap |
| `.blocksareclusters` | `nvvm.blocksareclusters` (UnitAttr) | only legal when `nvvm.reqntid` and `nvvm.cluster_dim` are also present | emitted on SM90+ when the cluster shape is `(1, 1, 1)` and the user opts in |

The driver decides which directives apply per target: SM89 and earlier suppress every cluster directive, even when the IR carries `nvvm.cluster_dim`, because ptxas would reject them. Cluster-shaped kernels are not portable to pre-SM90 targets without a recompile that drops the cluster directives entirely.

The `.maxntid` versus `.reqntid` distinction is the most subtle: `.maxntid` is an upper bound that lets ptxas size the per-thread register fragment without committing to an exact launch shape, while `.reqntid` is a hard contract — a launch with a different shape is rejected by the driver. Tileiras emits `.maxntid` for kernels that adapt to launch shape, and `.reqntid` for kernels whose lowering already baked in a specific thread count (every WGMMA-using kernel, since the four-warp group is mandatory; every warp-specialized kernel, since the producer/consumer split partitions named warps).

## Worked Example: WGMMA Kernel Launch

Consider a Hopper GEMM kernel launched with the following host triple-chevron call:

```cpp
gemm_kernel<<<dim3(2, 1, 1),       // grid: 2 clusters along X
              dim3(128, 1, 1),     // block: 128 threads = 4 warps = 1 warp-group
              48 * 1024,           // dynamic SMEM: 48 KiB per CTA
              stream,
              dim3(2, 1, 1)        // cluster: 2 CTAs per cluster (cuLaunchKernelEx)
             >>>(A, B, C, D, M, N, K);
```

The launch shape decomposes to:

- One grid of 2 clusters along X.
- Each cluster has 2 CTAs (the X dimension of the cluster shape).
- Each CTA has 128 threads (one warp group).
- Total: 4 CTAs × 128 threads = 512 threads in 2 clusters.

The driver packs the seven scalar parameters into PMEM, encodes the cluster shape `(2, 1, 1)` into the launch descriptor, and dispatches both clusters to the GPU scheduler. The scheduler picks two SMs that share a cluster network — say SM 0 and SM 1 — and places cluster 0's CTAs (rank 0 on SM 0, rank 1 on SM 1) on them. Cluster 1 lands on a different SM pair (say SM 2 and SM 3). Each SM partitions its register file to leave 168 registers per thread, allowing the 32-FP32 WGMMA accumulator slice to fit alongside the descriptor and loop-index registers.

Inside cluster 0, CTA 0 (`%cluster_ctarank = 0`) and CTA 1 (`%cluster_ctarank = 1`) cooperate on the multicast TMA load that feeds the WGMMA. The producer warp on CTA 0 issues a multicast `cp.async.bulk.tensor` whose destination addresses span CTA 0's SMEM and CTA 1's DSMEM window; the rendezvous goes through the transaction-mbarrier handshake covered in [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md). Once the handshake clears, each CTA's four-warp warp group runs the four-op WGMMA protocol on its own SMEM tile.

Tileiras emits the kernel header for this kernel as:

```ptx
.version 8.4
.target sm_90a
.address_size 64

.entry gemm_kernel(
    .param .u64 gemm_param_0,            // A
    .param .u64 gemm_param_1,            // B
    .param .u64 gemm_param_2,            // C
    .param .u64 gemm_param_3,            // D
    .param .u32 gemm_param_4,            // M
    .param .u32 gemm_param_5,            // N
    .param .u32 gemm_param_6             // K
)
.reqntid 128, 1, 1                       // four-warp warp group, mandatory
.maxnreg 168                             // accumulator fits, leaves occupancy room
.explicitcluster
.reqnctapercluster 2, 1, 1               // pair CTAs into 2-CTA clusters
{
    ; ... TMA descriptor encode, mbarrier init ...
    ; ... cluster.arrive + WGMMA four-op protocol per K tile ...
    ; ... epilogue: load C, add, TMA store of D ...
}
```

Five directives form one coherent launch contract. `.entry` declares the symbol as a kernel. `.reqntid 128, 1, 1` commits the launch to a 128-thread CTA, which fixes the warp count at four and lets WGMMA emission succeed. `.maxnreg 168` reserves enough registers for the accumulator fragment plus working registers. `.explicitcluster` and `.reqnctapercluster 2, 1, 1` tell the driver to dispatch two-CTA clusters so that DSMEM addresses across `%cluster_ctarank XOR 1` resolve.

If any directive disagrees with the launch — say the host calls with `block = (96, 1, 1)` — the driver rejects the launch at submission time because `.reqntid` is a hard contract. If the kernel were compiled with `.maxntid 128, 1, 1` instead, the launch would succeed for `block = (96, 1, 1)` but the WGMMA would silently consume an incomplete warp group, racing or hanging on the `wgmma.commit_group.sync.aligned`. The choice between `.maxntid` and `.reqntid` is therefore not a stylistic preference: WGMMA-using kernels must commit to `.reqntid` to make the contract enforceable.

## Cross-References

[Host Launch ABI + ptxas Knobs](../driver/host-launch-and-ptxas-knobs.md) documents the verifier rules and PTX directive emission order for every kernel attribute the policy table above references.
[Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) covers the cluster-tier rendezvous protocol — the `cluster.arrive.relaxed` / `cluster.wait` pair and its DSMEM transaction-byte extension — that this page treats as a black box.
[Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) shows the cluster-side copy fan-out that consumes the cluster shape declared at the `.entry` header.
[WGMMA Emission Protocol](wgmma-emission-protocol.md) documents the four-op fence/MMA/commit/wait sequence that the warp-group tier requires.
[mbarrier State Machine](mbarrier-state-machine.md) defines the 64-bit shared-memory object that the cluster handshake reads and writes through `nvvm.mbarrier.*` operations.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) covers the Blackwell successor to WGMMA, where the warp-group accumulator moves out of the register file and into tensor memory.
[DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) walks a representative kernel through every stage of the cascade, including the `.entry` header that this page focuses on.
