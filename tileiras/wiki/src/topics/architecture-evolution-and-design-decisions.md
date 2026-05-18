# Architecture Evolution and Design Decisions

## Abstract

Tileiras's shape — an MLIR substrate, a four-stage dialect cascade, a Rau-style modulo scheduler at the core, a CUTLASS-derived dialect family for tile primitives, and a wire-format-breaking bytecode boundary — is not arbitrary. Each layer is a deliberate response to a constraint that the older `cicc` pipeline could not solve cleanly. This page documents the choices and the alternatives they passed over, so a reimplementer can recover the intent behind the structure rather than only the structure itself.

The page is a retrospective, not a tutorial. It assumes the reader already has the mechanical picture from the dialect, scheduler, and lowering chapters and is asking the more fundamental question: why this shape, and not another?

## The MLIR Choice

`cicc`, tileiras's sibling in the CUDA 13.1 toolkit, accepts CUDA C++ source and reaches PTX through the NVVM bridge and the upstream NVPTX backend. That pipeline is mature, well understood, and entirely adequate for traditional CUDA C++. It is not adequate for tile-shaped computation, and the reason is structural rather than performance-driven.

Three classes of information disappear when a tile program is expressed in LLVM IR directly.

The first is tile typing. A statement like "this value is a 128x64 fragment with swizzle mode XOR-3 living in shared memory" has no native LLVM type. The closest LLVM construct is an opaque pointer with metadata; the swizzle, the layout algebra, and the per-thread fragment shape all become side data that every analysis pass must reconstruct. Once reconstructed, the analysis carries its own copy of the structure, and the structure drifts between passes.

The second is pipeline structure. "This loop is the producer side of a software-pipelined async copy; the consumer is in the same loop body" is something the modulo scheduler needs to see directly. In LLVM IR the pattern is a memory-token chain and a hand-marked instruction sequence; recovering the producer/consumer pairing requires re-running the analysis that originally placed the pattern.

The third is descriptor-vs-pointer typing. A WGMMA op takes operand A from shared memory through a 64-bit descriptor and operand B from the register file. In LLVM IR both look like ordinary loads. In `nv_tileas` and `nvvm` they have distinct operand types, and the scheduler, the register allocator, and the asm printer can each reason about them without consulting a side analysis.

MLIR's dialect mechanism keeps each level of abstraction explicit until the level below it is ready to consume it. Tileiras adds dialects exactly where structural information matters; it lowers down to LLVM only when the structural information has been fully exploited. The alternative — riding LLVM IR end-to-end like `cicc` does — would force a tile-aware emitter to encode every layout, every async-copy chain, and every pipeline boundary in metadata, then re-derive it at every pass that cares.

## The Dialect Cascade

The cascade has four stages and not one, and the answer to "why not collapse them?" is that each stage establishes invariants the next stage relies on.

```
cuda_tile          (input form, frontend-emitted)
   |
   | ConvertCudaTileToTileAA
   v
nv_tileaa          (analysis form, alias-aware, typed pointers + tokens)
   |
   | ConvertTileAAToTileAS
   v
nv_tileas          (scheduled form, pipeline regions, barrier slots)
   |
   | ConvertTileASToLLVM
   | ConvertNVGPUToNVVM
   v
nvvm + llvm        (codegen-ready, NVPTX-backend input)
```

| Stage | What it adds | What downstream relies on |
|---|---|---|
| `cuda_tile` | tile-typed values, abstract memops, frontend op surface | nothing earlier; this is the input shape |
| `nv_tileaa` | typed pointer/view types, memory tokens, alias-analysis attributes | every subsequent pass assumes tokens carry the alias relation |
| `nv_tileas` | scheduling annotations, pipeline-region markers, barrier slot bindings | LLVM lowering assumes each scheduled op knows its stage and slot |
| `nvvm` + `llvm` | NVVM intrinsics, LLVM IR shape | NVPTX backend assumes verifier-clean NVVM IR |

The alternative is one giant rewrite that takes `cuda_tile` and emits LLVM directly. That alternative would have to encode all the scheduling state, all the alias state, and all the layout state inside a single pass — the kind of monolithic transform that resists testing and inversion. The cascade trades total pass count against per-pass simplicity: each conversion only needs to understand two adjacent levels, never the full distance.

A second reason for the split is materialization order. `Pipe_` and `Mutex_` IR — the synchronized-handshake form that drives the runtime — is materialized inside the `tileaa` to `tileas` transition. If the cascade were collapsed, that materialization would have to be interleaved with the layout selection that precedes it and the lowering that follows it, making both harder to debug.

## The Rau-Style Modulo Scheduler

[Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md) covers the mechanics; the question here is why this specific scheduler.

Rau modulo scheduling is the canonical software-pipelining algorithm from the VLIW era. GPUs are not VLIW machines, but tile-based kernels share the relevant structure: a small loop body that the compiler wants to overlap across iterations, a small number of architectural resources with explicit capacity, and a clear separation between producer-side and consumer-side operations. Three properties make Rau a natural fit.

Modulo placement gives each operation a `(stage, cycle mod II)` coordinate, which directly encodes overlap. An async-copy producer in stage 0 and a WGMMA consumer in stage 2 share the same `cycle mod II` slot but occupy different stages; the schedule writes them down in a single coordinate system without auxiliary bookkeeping.

The Resource Reservation Table extends naturally to GPU-specific resources. Stock Rau tracks issue slots; tileiras tracks TMA channels, WGMMA pipeline lanes, shared-memory bank groups, and the named-barrier pool through the same RRT shape. The probe-then-commit discipline holds across all of them.

The `II` search starts at the maximum of resource MII, recurrence MII, fine-density MII, and dependency MII, then increments until placement succeeds. This is the standard Rau outer loop, and it has the property a production compiler needs: it terminates in bounded time with a deterministic schedule.

Two alternatives were available and rejected. An ILP-based scheduler — formulating placement as an integer-program and handing it to a solver — would find more optimal schedules but at unacceptable compile-time cost. A list-scheduling pass with manual pipeline-region annotation, the style used by some Triton-derived backends, would be simpler but would require the frontend to commit to a pipeline shape before the scheduler runs. Rau lets the scheduler discover the shape from the loop body itself.

## The CUTLASS-in-MLIR Family

The `cute`, `cute_nvgpu`, and `cutlass` dialects look on first inspection like a redundant layer: the tile primitives they expose already exist in CUTLASS upstream. The redundancy is intentional.

CUTLASS upstream is a C++ template library. Its layout algebra, its copy atoms, and its MMA atoms are abstractions that work well inside a C++ kernel but cannot be inspected by an IR-level pass. A pass that wants to ask "does this copy atom use a TMA descriptor or a generic load?" must run C++ template instantiation; a pass that wants to fuse two MMA atoms must understand C++ template specialization.

Porting those abstractions into MLIR dialects has three immediate consequences. Layout algebra becomes first-class IR operations — `cute.local_tile`, `cute.partition`, `cute.divide`, `cute.size`, `cute.cosize` are inspectable, verifiable, and foldable. Copy atoms become MLIR ops with explicit operand contracts — a TMA atom and a generic-load atom are different ops with different verifiers, not different template instantiations. MMA atoms become ops the scheduler can reason about — the operand sources, the latency, and the resource footprint are op-level facts.

The same dialect family drives both CUTLASS-style kernels and Triton-style kernels. A CUTLASS frontend lowers C++ kernels into `cute` and `cutlass` ops, then through the rest of the cascade. A Triton-style frontend lowers tile-shaped kernels into `cuda_tile`, which lowers into `nv_tileaa`, which interacts with the same `cute` and `cutlass` primitives at the scheduling layer. One MLIR substrate, two frontends.

The alternative — leaving CUTLASS as a C++ library and lowering kernels through it at the source level — is exactly what `cicc` does, and it is the reason `cicc` cannot reason about the tile-shape structure that the modulo scheduler needs.

## The Wire-Format-Breaking Bytecode

Tileiras's MLIR bytecode reader dispatches `AttrTag` and `TypeTag` values through a table whose ordering and case set do not match upstream MLIR. A bytecode file produced by upstream `mlir-translate --serialize-bytecode` will not parse cleanly in tileiras, and the reverse is also true. The question is whether this is policy or accident.

Two readings of the evidence are consistent.

The first is an intentional ABI fork. NVIDIA's binary is a hermetic distribution: users go through frontends that produce conformant bytecode, and the bytecode itself is internal to NVIDIA's pipeline. Reserving the right to add private attribute kinds, reorder the dispatch table for code-density reasons, or freeze a particular tag layout is a reasonable internal-format decision. The frontend is the contract; the binary format is implementation.

The second is snapshot drift. Tileiras was forked from a pre-release MLIR snapshot, and upstream's `AttrTag` table evolved differently after the fork. The wire-format incompatibility is then incidental rather than designed, and it persists because nothing in the toolkit needs the formats to match.

Both readings produce the same consequence: a tileiras-compatible reimplementation cannot use upstream `mlir-translate` as a substitute for the tileiras bytecode reader. It must either implement a tileiras-aware writer or use text MLIR and the tileiras parser. The [MLIR Bytecode Format](../bytecode/mlir-bc-format.md) page enumerates the specific tag-table deltas.

## Decisions Visible in the Binary

Several smaller decisions show up in the binary itself and have entries elsewhere in the wiki; collecting them here lets a reader see the design as a whole.

**LLVM 21 base.** Tileiras embeds a stock LLVM 21 snapshot, statically linked. The ten-fingerprint argument is in the [LLVM Fingerprint Table](../reference/llvm-fingerprint-table.md). The decision is to track upstream LLVM closely rather than maintain a heavily forked private LLVM; private behavior is concentrated in the NVPTX backend's peephole passes and a small set of TableGen additions, not in the core IR.

**XOR-3 mnemonic-pool obfuscation.** The NVPTX asm printer's instruction mnemonic table is XOR-encoded at rest and decoded once at program start through a `pthread_once`-guarded init. The encoding is weak; it raises the cost of trivial `strings` extraction without claiming any cryptographic guarantee. The wiki nonetheless documents the decoder so a reader can recover the full mnemonic pool from the binary.

**Static linkage of LLVM and MLIR.** Tileiras carries no shared-library dependency on `libLLVM.so` or `libMLIR.so`. The binary is hermetic. The trade-off is binary size for distribution simplicity: a CUDA toolkit shipped to a customer machine cannot rely on a system LLVM being present, in compatible shape, or even installed.

**No GPU dependency for tileiras itself.** Tileiras runs on CPU and emits PTX. The `ptxas` subprocess it spawns also needs no GPU to produce SASS. Both compilers can build for an SM target that is not physically present on the host. This is a design property, not an accident; it makes cross-compilation, CI builds, and offline kernel libraries straightforward.

**Stripped binary.** The shipped binary has its `.symtab` removed; only dynamic symbols remain. This is standard distribution practice for a production toolchain and not a security claim. The wiki's job — and the [String Evidence and Confidence Policy](../reference/string-evidence-and-confidence-policy.md)'s job — is to recover from the strip with confidence-tagged claims rather than treat the binary as opaque.

## Trade-Offs and Remaining Questions

An honest framing of the design needs to admit where the choices cost.

**Compile time.** A four-stage dialect cascade plus a modulo scheduler is slower per-kernel than a direct LLVM IR-to-PTX descent. For AI workloads where a kernel is compiled once and runs many times, the cost amortizes and the optimized schedule pays back many times over. For one-off compilations — small test programs, exploratory kernels — the cost is real.

**Per-SM atom catalogs.** The `cute_nvgpu` dialect carries SM70-through-SM120 atom rosters, each with a TMA family, an MMA family, a WGMMA family (where applicable), and a tcgen05 family (where applicable). New SM architectures require updates to the atom catalogs, the scheduler resource models, and the verifier patterns. The cost of adding an SM is non-trivial.

**Wire-format incompatibility.** Whatever its cause, the bytecode delta makes interop with non-NVIDIA MLIR tooling effortful. A user who wants to debug a tileiras bytecode file with upstream tools must first round-trip through text MLIR; a user who wants to feed an upstream-tool-produced bytecode file into tileiras must first round-trip the other way.

**OSS preview is a subset.** The public `cuda-tile` repository covers part of the `cuda_tile` dialect surface — types, attributes, ops, two helper passes, a standalone driver. It does not cover `nv_tileaa`, `nv_tileas`, the TileAS pass family, the `cute_nvgpu` SM rosters, the modulo scheduler, or the NVPTX peephole additions. A reimplementation that wants the full system must recover those pieces from the binary, with the wiki as accelerator.

None of these are fatal. They are the consequences of decisions made deliberately, and they are visible enough in the binary that a reader can weigh them against the alternatives the design rejected.

## Cross-References

The boundaries with neighboring tools are documented in [cicc Comparison](../boundaries/cicc-comparison.md), [Position in nvcc 13.1](../boundaries/nvcc-13-1-position.md), and [Toolchain Integration](../boundaries/toolchain-integration.md). The relationship to the public `cuda-tile` source preview is the [OSS Comparison Overview](../oss/overview.md). The binary-level evidence behind the decisions on this page is [Binary Anatomy and RE Methodology](binary-anatomy-and-re-methodology.md) and the [Program Layout](../binary-layout.md). The canonical depth pages for the scheduler and the cascade are [Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md), [Pipeline Overview](../pipeline/overview.md), and [Lowering Overview](../lowering/overview.md).
