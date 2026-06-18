# Summary

[PTXAS v13.0 — Reverse Engineering Reference](./index.md)

---

# Overview

- [Function Map](./function-map.md)
- [Binary Layout](./binary-layout.md)
- [Methodology](./methodology.md)
- [Version Tracking](./VERSIONS.md)

# Compilation Pipeline

- [Pipeline Overview](./pipeline/overview.md)
- [Entry Point & CLI](./pipeline/entry.md)
- [PTX Parser (Flex + Bison)](./pipeline/ptx-parser.md)
- [PTX Directive Handling](./pipeline/ptx-directives.md)
- [PTX-to-Ori Lowering](./pipeline/ptx-to-ori.md)
- [Optimization Pipeline (159 Phases)](./pipeline/optimizer.md)
- [SASS Code Generation](./pipeline/codegen.md)
- [ELF/Cubin Output](./pipeline/output.md)

# Ori IR — Internal Representation

- [IR Overview & Design](./ir/overview.md)
- [Instructions & Opcodes](./ir/instructions.md)
- [Basic Blocks & CFG](./ir/cfg.md)
- [Register Model (R/UR/P/UP)](./ir/registers.md)
- [Data Structure Layouts](./ir/data-structures.md)

# Optimization Passes

- [Pass Inventory & Ordering](./passes/index.md)
- [Phase Manager Infrastructure](./passes/phase-manager.md)
- [AnalyzeControlFlow (CFG Rebuild)](./passes/analyze-control-flow.md)
- [GeneralOptimize Bundles](./passes/general-optimize.md)
- [Branch & Switch Optimization](./passes/branch-switch.md)
- [Loop Passes](./passes/loop-passes.md)
- [Strength Reduction](./passes/strength-reduction.md)
- [Linear Replacement](./passes/linear-replacement.md)
- [Copy Propagation & CSE](./passes/copy-prop-cse.md)
- [Predication](./passes/predication.md)
- [Rematerialization](./passes/rematerialization.md)
- [Instruction Movement Engine](./passes/instruction-movement.md)
- [Liveness Analysis](./passes/liveness.md)
- [Synchronization & Barriers](./passes/sync-barriers.md)
- [Hot/Cold Partitioning](./passes/hot-cold.md)
- [GMMA/WGMMA Pipeline](./passes/gmma-pipeline.md)
- [Varying Propagation (Divergence Analysis)](./passes/varying-propagation.md)
- [Shader Constant Extraction](./passes/shader-const-extraction.md)
- [Uniform Register Optimization](./passes/uniform-regs.md)
- [Late Expansion & Legalization](./passes/late-legalization.md)

# Register Allocation

- [Allocator Architecture](./regalloc/overview.md)
- [Fatpoint Algorithm](./regalloc/algorithm.md)
- [Spilling](./regalloc/spilling.md)
- [GPU ABI & Calling Convention](./regalloc/abi.md)

# Instruction Scheduling

- [Scheduler Architecture](./scheduling/overview.md)
- [Scheduling Algorithm](./scheduling/algorithm.md)
- [Latency Model & HW Profiles](./scheduling/latency-model.md)
- [Scoreboards & Dependency Barriers](./scheduling/scoreboards.md)
- [Phase 110 — PostSchedule](./scheduling/post-schedule.md)
- [Legacy Backend A — `sub_A97600`](./scheduling/legacy-backend-a.md)

# SASS Code Generation

- [Code Generation Overview](./codegen/overview.md)
- [Instruction Selection](./codegen/isel.md)
- [SASS Instruction Encoding](./codegen/encoding.md)
- [SASS Encoding Dispatch Tables](./codegen/encoding-tables.md)
- [Peephole Optimization](./codegen/peephole.md)
- [Mercury Encoder](./codegen/mercury.md)
- [Capsule Mercury & Finalization](./codegen/capmerc.md)
- [Newton-Raphson Templates](./codegen/templates.md)
- [SASS Text Generation](./codegen/sass-printing.md)

# GPU Architecture Targets

- [SM Architecture Map](./targets/index.md)
- [Turing & Ampere (SM 75–88)](./targets/turing-ampere.md)
- [Ada & Hopper (SM 89–90a)](./targets/ada-hopper.md)
- [Blackwell (SM 100–121)](./targets/blackwell.md)
- [TCGen05 — 5th Gen Tensor Cores](./targets/tcgen05.md)

# CUDA Intrinsics

- [Intrinsic Table (608 Entries)](./intrinsics/index.md)
- [Prototype Emitter (1,080-Case Dispatch)](./intrinsics/prototype-emitter.md)
- [OCG Intrinsic System (44 Operations)](./intrinsics/ocg.md)
- [Math Intrinsics](./intrinsics/math.md)
- [Tensor Core Intrinsics](./intrinsics/tensor.md)
- [Sync & Warp Intrinsics](./intrinsics/sync-warp.md)

# ELF/Cubin Output

- [Custom ELF Emitter](./output/elf-emitter.md)
- [Section Catalog & EIATTR](./output/sections.md)
- [Debug Information](./output/debug-info.md)
- [Relocations & Symbols](./output/relocations.md)

# Configuration

- [CLI Options](./config/cli-options.md)
- [Knobs System (1,294 Knobs)](./config/knobs.md)
- [Optimization Levels](./config/opt-levels.md)
- [DUMPIR & NamedPhases](./config/dumpir.md)

# Infrastructure

- [Memory Pool Allocator](./infra/memory-pools.md)
- [Hash Tables & Bitvectors](./infra/hash-bitvector.md)
- [Thread Pool & Concurrency](./infra/threading.md)

# Reference

- [SASS Opcode Catalog](./reference/sass-opcodes.md)
- [PTX Instruction Table](./reference/ptx-instructions.md)
- [EIATTR Attribute Catalog](./reference/eiattr.md)
- [String-Pool Encryption](./reference/string-pool-cipher.md)
- [Glossary](./glossary.md)
