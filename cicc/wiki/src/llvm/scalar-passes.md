# Scalar Passes: SROA, EarlyCSE & JumpThreading

Three LLVM scalar optimization passes play outsized roles in cicc's GPU pipeline. Each is a stock LLVM implementation with NVIDIA configuration overrides (and in EarlyCSE's case, binary-level modifications). Each appears multiple times in the pipeline at different tier levels, and each can be independently disabled via `NVVMPassOptions` flags.

## [SROA (Scalar Replacement of Aggregates)](./sroa.md)

SROA eliminates `alloca` instructions by decomposing aggregates into individual SSA values that the register allocator can place in registers. On a GPU this is existential: every surviving alloca becomes a spill to [`.local` memory](../gpu-execution-model.md#memory-hierarchy) (DRAM-backed, 200-800 cycle latency on cache miss versus zero for a register). A single un-promoted alloca in a hot loop can degrade kernel throughput by 10-50x. SROA also eliminates the [`.param` space](../gpu-execution-model.md#the-param-calling-convention) copies generated for byval struct parameters, preventing round-trips through local memory.

**[Full SROA analysis >>>](./sroa.md)**

## [EarlyCSE (Early Common Subexpression Elimination)](./early-cse.md)

Cicc's EarlyCSE is **not** stock LLVM. The binary contains four CUDA-specific extensions: barrier-aware memory versioning that prevents CSE across `__syncthreads()` and other synchronization points, shared memory address space 7 protection against unsafe store-to-load forwarding between threads, a dedicated NVVM intrinsic call CSE handler with fast-path recognition for thread-invariant special register reads (`threadIdx.x`, etc.), and a PHI operand limit of 5 for compile-time control. It also adds a fourth scoped hash table (store-forwarding) that upstream LLVM lacks.

**[Full EarlyCSE analysis >>>](./early-cse.md)**

## [JumpThreading](./jump-threading.md)

JumpThreading duplicates basic blocks so that predecessors with statically-determinable branch conditions jump directly to the correct successor, eliminating warp divergence. The pass is fundamentally at odds with PTX's requirement for reducible control flow: block duplication can create irreducible cycles. Cicc addresses this through loop header protection (`jump-threading-across-loop-headers` defaults to false), conservative duplication thresholds (6-instruction block limit), and a late-pipeline [StructurizeCFG](./structurizecfg.md) safety net that catches any irreducibility that slips through. NVIDIA provides a separate `"disable-jump-threading"` kill switch (distinct from upstream's `"disable-JumpThreadingPass"`), with an OCG experiment annotation suggesting architecture-specific cases where the CFG disruption outweighs the benefit.

**[Full JumpThreading analysis >>>](./jump-threading.md)**

## Cross-References

- [Pipeline & Ordering](./pipeline.md) -- tier-dependent scheduling of all three passes
- [Register Allocation](./register-allocation.md) -- surviving allocas after SROA become register pressure; failed promotion leads to `.local` memory spills
- [StructurizeCFG](./structurizecfg.md) -- the safety net that catches irreducible CFG created by JumpThreading or other passes
- [GVN](./gvn.md) -- GVN performs load CSE and redundancy elimination complementary to EarlyCSE, running later in the pipeline with more expensive analysis
- [MemorySpaceOpt](../passes/memory-space-opt.md) -- resolves generic pointers to specific address spaces; interacts with EarlyCSE's address-space-aware load forwarding
- [DSE](./dse.md) -- Dead Store Elimination complements EarlyCSE's within-block store-to-load forwarding with cross-block dead store detection
