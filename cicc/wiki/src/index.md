# CICC v13.0 — Reverse Engineering Reference

CICC is NVIDIA's CUDA C-to-PTX compiler — the binary that transforms CUDA C++ source code (or LLVM bitcode) into PTX assembly for GPU execution. At 60 MB, it is one of the largest single compiler binaries in production use. This wiki documents its internal architecture, recovered from static analysis of the stripped x86-64 ELF binary using IDA Pro 8.x and Hex-Rays decompilation.

| | |
|---|---|
| **Binary** | cicc v13.0, 60,108,328 bytes, x86-64, stripped |
| **Build** | `cuda_13.0.r13.0/compiler.36424714_0` |
| **Decompilation** | 80,562 functions, 80,281 recovered (99.65%), IDA Pro 8.x + Hex-Rays |
| **Strings** | 188,141 extracted |
| **LLVM base** | LLVM 20.0.0 (internal), bitcode producer ID `"LLVM7.0.1"` (NVVM compat) |
| **LLVM pass classes** | ~402 standard + 35 NVIDIA custom |
| **CLI options** | ~1,689 registered via `cl::opt` + 221 NVVMPassOptions slots |
| **NVVM builtins** | 770 (IDs 1–770, wyhash open-addressing table) |
| **Default target** | `sm_75` (Turing) |
| **Supported SMs** | sm\_75 through sm\_121f (Turing through Blackwell — sm\_100 datacenter, sm\_103 Blackwell Ultra, sm\_110 Jetson Thor, sm\_120 consumer/RTX 50, sm\_121 DGX Spark) |

## Three Subsystems

CICC is not a monolithic compiler. It is composed of three largely independent subsystems, each with its own lineage, coding conventions, and internal data structures:

**1. EDG 6.6 C++ Frontend** (3.2 MB, `0x5D0000`–`0x8F0000`) — A licensed commercial frontend from Edison Design Group that parses CUDA C++ source code and emits transformed C code. It operates as a source-to-source translator: CUDA kernel launch syntax (`<<<>>>`) is lowered to CUDA runtime API calls, memory space qualifiers (`__shared__`, `__constant__`) are resolved to address space annotations, and C++ templates/constexpr are fully evaluated. The output is not LLVM IR — it is C code that feeds into a second compilation phase. See [EDG 6.6 Frontend](./pipeline/edg.md).

**2. NVVM Bridge** (~4 MB, `0x8F0000`–`0x12CFFFF`) — The glue layer between EDG and LLVM. It handles CLI parsing, architecture detection (23 SM variants with 3-column flag fan-out), the dual-path compilation dispatch (Path A via LibNVVM API, Path B standalone), the NVVMPassOptions knob system (221 per-pass configuration slots), and the 770-entry builtin resolution table. This layer is entirely NVIDIA-proprietary. See [Entry Point & CLI](./pipeline/entry.md) and [LLVM Optimizer](./pipeline/optimizer.md).

**3. LLVM 20.0.0 Backend** (~45 MB, `0x12D0000`–`0x3BFFFFF`) — A heavily modified LLVM fork that performs IR optimization and PTX code generation. NVIDIA has added 35 custom passes (MemorySpaceOpt, Rematerialization, BranchDist, LoopIndexSplit, Sinking2, etc.), a proprietary two-phase compilation model with per-function thread parallelism, and extensive modifications to the NVPTX backend for tensor core code generation across 5 GPU architecture generations. See [Code Generation](./pipeline/codegen.md) and [PTX Emission](./pipeline/emission.md).

Additionally, **jemalloc 5.3.x** (767 functions at `0x12FC000`–`0x133FFFF`) is statically linked, replacing the system allocator for improved memory allocation performance during compilation. See the dedicated [jemalloc Allocator](./infra/jemalloc.md) page.

## Key Discoveries

If you only read one paragraph, read this. The findings that most differentiate cicc from a plain LLVM build:

- **Dual binary copies.** Path A (LibNVVM API) at `0x90xxxx` and Path B (standalone) at `0x126xxxx` contain two complete, independently-compiled copies of the EDG-to-PTX pipeline. Same logic, different translation units, selected by `v253` in `sub_8F9C90`. See [Dual-Path Architecture](#dual-path-architecture) below and [Entry Point & CLI](./pipeline/entry.md).
- **Two-phase compilation.** Phase I optimizes the whole module (inlining, IPMSP, global decisions); Phase II re-optimizes each function independently with a thread pool gated by GNU Jobserver tokens. The split is non-obvious and changes which knobs affect what. See [LLVM Optimizer](./pipeline/optimizer.md) and [Concurrent Compilation](./infra/concurrent-compilation.md).
- **35 proprietary passes** interleaved with stock LLVM 20.0.0: MemorySpaceOpt, Rematerialization, BranchDist, IPMSP, Sinking2, LoopIndexSplit, IV Demotion, Dead Barrier/Sync Elimination, CSSA, etc. Several have no upstream equivalent. See [NVIDIA Custom Passes](./passes/index.md).
- **770-entry builtin table.** Wyhash open-addressed lookup at `sub_90AEE0` / `sub_126A910` maps every CUDA builtin to its LLVM intrinsic or lowering routine. See [NVVM Builtins](./builtins/index.md).
- **Three independent knob systems.** ~1,689 `cl::opt` flags, 221 `NVVMPassOptions` slots, ~70 codegen knobs. Effects overlap and sometimes contradict. See [Configuration](./config/knobs.md).
- **455 KB embedded libdevice bitcode** (twice — once per path) provides the `__nv_*` math library. See [Libdevice Linking](./infra/libdevice-linking.md).

## Reading This Wiki

Pages are organized around the compilation pipeline and written at reimplementation-grade depth for senior C++ developers with LLVM backend experience. The left sidebar (`SUMMARY.md`) is the full table of contents — use it as the spine. The reading paths below are curated sequences for common goals.

### Quick Start (15 minutes)

If you have never opened this wiki before, read these three pages in order before anything else:

1. **[Pipeline Overview](./pipeline/overview.md)** — Establishes the 10-stage flow and address ranges every other page assumes.
2. **[Entry Point & CLI](./pipeline/entry.md)** — How invocation reaches the real main, the dual-path switch, and the 1,689-flag CLI.
3. **[NVIDIA Custom Passes — Overview](./passes/index.md)** — The inventory table that makes everything proprietary visible at a glance.

Then pick one of the four deep paths below based on your goal.

## Dual-Path Architecture

A distinctive feature of cicc is its **dual-path design** — two complete copies of the compilation backend exist within the same binary, selected at runtime:

| | Path A (`0x90xxxx`) | Path B (`0x126xxxx`) |
|---|---|---|
| **Purpose** | LibNVVM API mode | Standalone mode |
| **Simple compile** | `sub_902D10` | `sub_1262860` |
| **Multi-stage** | `sub_905EE0` (9 KB native) | `sub_1265970` (9 KB native) |
| **CLI parsing** | `sub_900130` | `sub_125FB30` |
| **Builtin table** | `sub_90AEE0` (33 KB native) | `sub_126A910` (34 KB native) |
| **Libdevice** | `unk_3EA0080` (455KB) | `unk_420FD80` (455KB) |
| **Version string** | `-nvvm-version=nvvm-latest` | `-nvvm-version=nvvm70` |

Runtime selection is controlled by `v253` in `sub_8F9C90` (the real main function). The default value (2) triggers an environment variable lookup through an obfuscated string comparison to determine which path to take. This design allows a single binary to serve both the `nvcc` driver toolchain and the LibNVVM runtime compilation API.

## Compilation Pipeline

Both paths converge on the same 5-stage pipeline:

```text
CUDA C++ Source (.cu / .ci / .i)
  │
  ├─ EDG 6.6 Frontend (sub_5D2A80)
  │   ├─ lgenfe_main (sub_617BD0): 282-case CLI, 737 #defines
  │   ├─ Parser: recursive-descent + declaration specifier state machine
  │   ├─ Constexpr evaluator: 317KB tree-walking interpreter
  │   └─ Backend: "Generating NVVM IR" → .int.c / .device.c / .stub.c
  │
  └─ NVVM/LLVM Pipeline
      │
      ├─ IRGEN:  EDG IL → LLVM IR translation (cicc's equivalent of Clang CodeGen)
      │            Type translation (fixed-point iteration, address space mapping)
      │            Expression/statement/function codegen (recursive AST walk)
      │            CUDA semantic lowering (threadIdx→intrinsics, printf→vprintf, etc.)
      │            Kernel metadata emission (nvvm.annotations)
      │            Two copies: Path A (0x90xxxx) and Path B (0x126xxxx)
      │
      ├─ LNK:     Module linking + libdevice (455KB embedded bitcode)
      │            Triple validation (must be nvptx64-)
      │            IR version check (nvvmir.version metadata)
      │
      ├─ OPT:     Two-phase compilation (Phase I: whole-module, Phase II: per-function)
      │            ~150 pass insertions via sub_12E54A0
      │            Three language paths: "ptx" / "mid" / default
      │            35 NVIDIA custom passes interleaved with standard LLVM
      │            Optional: concurrent per-function compilation (thread pool + jobserver)
      │
      ├─ OPTIXIR:  OptiX IR generation (optional, --emit-optix-ir)
      │
      └─ LLC:     NVPTX backend code generation
                   SelectionDAG lowering (2.3 MB NVPTXTargetLowering)
                   19 MMA shapes × 11 data types for tensor core codegen
                   9 PTX register classes
                   StructurizeCFG (mandatory for PTX structured control flow)
                   → .ptx output
```

## Subsystem Address Map

| Subsystem | Address Range | Size | Key Entry Point |
|---|---|---|---|
| jemalloc stats | `0x40D000`–`0x41FFFF` | ~80KB | `sub_40D5CA` (vsnprintf) |
| Global constructors | `0x430000`–`0x5CFFFF` | ~1.6 MB | `cl::opt` registration (~1,689 options) |
| EDG 6.6 Frontend | `0x5D0000`–`0x8EFFFF` | 3.2 MB | `sub_5D2A80` (orchestrator) |
| CLI / Real Main | `0x8F0000`–`0x96FFFF` | 520 KB | `sub_8F9C90` (real main) |
| Bitcode reader | `0x9F0000`–`0xAFFFFF` | ~1 MB | `sub_9F2A40` (parseFunctionBody) |
| LLVM verifier | `0xBF0000`–`0xC6FFFF` | 500 KB | `sub_BFC6A0` (visitCallInst) |
| LLVM passes | `0xC00000`–`0x12CFFFF` | ~7 MB | InstCombine, GVN, DSE, LICM, etc. |
| PassManager / NVVM bridge | `0x12D0000`–`0x16FFFFF` | 4.2 MB | `sub_12E54A0` (pipeline assembly) |
| Backend / machine passes | `0x1700000`–`0x1EFFFFF` | 8 MB | MRPA, Block Remat, Mem2Reg |
| SelectionDAG | `0x1F00000`–`0x20FFFFF` | 2 MB | `sub_20019C0` (LegalizeTypes, 348KB) |
| NVPTX emission | `0x2100000`–`0x21FFFFF` | 1 MB | `sub_215A3C0` (function headers) |
| New PM / pass registration | `0x2340000`–`0x23FFFFF` | 768 KB | `sub_2342890` (2,816-line registrar) |
| Loop passes | `0x2A00000`–`0x2DFFFFF` | 4 MB | LoopVectorize, SLP, Unroll, etc. |
| NVPTX ISel + lowering | `0x3000000`–`0x36FFFFF` | 7 MB | `sub_33B0210` (intrinsic switch, 343KB) |
| Embedded libdevice | `0x3EA0080` / `0x420FD80` | 456 KB × 2 | LLVM bitcode (~400 math functions) |

## Section Index and Deep Reading Paths

The Quick Start above (Pipeline Overview → Entry & CLI → Custom Passes Overview) is the on-ramp. The four paths below are deeper sequences, each tuned to a specific goal. The Section Index lists every top-level page; the sidebar holds the full TOC.

### Section Index

- **[Pipeline Overview](./pipeline/overview.md)** — End-to-end compilation flow diagram with links to every stage.
- **[Entry Point & CLI](./pipeline/entry.md)** — CLI parsing, dual-path dispatch, architecture detection.
- **[EDG 6.6 Frontend](./pipeline/edg.md)** — CUDA C++ to transformed C source-to-source translation.
- **[NVVM IR Generation](./pipeline/ir-generation.md)** — EDG IL tree to LLVM Module: types, expressions, statements, functions.
- **[LLVM Optimizer](./pipeline/optimizer.md)** — Two-phase compilation, pipeline assembly, NVVMPassOptions.
- **[Code Generation](./pipeline/codegen.md)** — SelectionDAG, ISel, register allocation, scheduling.
- **[PTX Emission](./pipeline/emission.md)** — AsmPrinter, directive emission, PTX body output.
- **[NVIDIA Custom Passes](./passes/index.md)** — 35 proprietary passes not in upstream LLVM.
- **[LLVM Pass Pipeline & Ordering](./llvm/pipeline.md)** — Complete pass registration, execution order per O-level, tier system.
- **[NVVM Builtins](./builtins/index.md)** — 770-entry builtin table: hash structure, ID inventory, category breakdown.
- **[GPU Targets](./targets/index.md)** — SM feature gates, architecture detection, sm\_75 through sm\_121f.
- **[Data Structures](./structs/ir-node.md)** — IR node layout, pattern database, DAG node, symbol table, NVVM container.
- **[Infrastructure](./infra/alias-analysis.md)** — Alias analysis, MemorySSA, AsmPrinter, debug verification, NVPTX target.
- **[LTO & Module Optimization](./lto/index.md)** — Cross-TU inlining, devirtualization, GlobalOpt, ThinLTO import.
- **[Configuration](./config/knobs.md)** — Three knob systems: ~1,689 `cl::opt` flags, 221 NVVMPassOptions slots, ~70 codegen knobs.
- **[Reference](./reference/address-spaces.md)** — Address spaces, register classes, NVPTX opcodes, GPU execution model.
- **[Function Map](./function-map.md)** — Address-to-identity lookup for ~350 key functions with confidence levels.
- **[Binary Layout](./binary-layout.md)** — Subsystem address map at pass granularity.
- **[Methodology](./methodology.md)** — How this analysis was performed and how to assess confidence.

### Reading Path 1: End-to-End Pipeline Understanding

Goal: understand how CUDA source becomes PTX, what each stage does, and how control flows between subsystems.

Read in this order:

1. **[Pipeline Overview](./pipeline/overview.md)** — The complete flow diagram. Establishes the 10 stages and their address ranges. Read this first to build the mental model that all other pages assume.
2. **[Entry Point & CLI](./pipeline/entry.md)** — How cicc is invoked, the 1,689-flag CLI, dual-path dispatch (Path A LibNVVM vs. Path B standalone), and the `sub_8F9C90` real-main function.
3. **[nvcc-to-cicc Interface](./pipeline/nvcc-interface.md)** — The flag translation layer between nvcc and cicc. The 40+ flag mappings and 3-column architecture fan-out. Necessary context for understanding why certain flags exist.
4. **[EDG 6.6 Frontend](./pipeline/edg.md)** — The commercial C++ frontend. How CUDA syntax is lowered to C, the 737 configuration `#define`s, and the `.int.c` / `.device.c` / `.stub.c` output split.
5. **[NVVM IR Generation](./pipeline/ir-generation.md)** — The EDG-to-LLVM bridge. Then follow the four sub-pages: [Type Translation](./pipeline/irgen-types.md) → [Expressions](./pipeline/irgen-expressions.md) → [Statements](./pipeline/irgen-statements.md) → [Functions](./pipeline/irgen-functions.md).
6. **[Libdevice Linking](./infra/libdevice-linking.md)** — The embedded 455KB bitcode library with 352 `__nv_*` math functions. Triple validation, version checking.
7. **[LLVM Optimizer](./pipeline/optimizer.md)** — The two-phase compilation model, the 49.8KB pipeline assembler (`sub_12E54A0`), pass ordering, and the NVVMPassOptions knob system. This is the longest and densest stage.
8. **[Pipeline & Pass Ordering](./llvm/pipeline.md)** — The exact pass execution order at each O-level, the tier system, and the 526 registered passes.
9. **[Code Generation](./pipeline/codegen.md)** — SelectionDAG lowering, instruction selection, register allocation, instruction scheduling. Hub page with links to deep dives.
10. **[PTX Emission](./pipeline/emission.md)** — AsmPrinter, directive headers, PTX body output, metadata emission.

Optional extensions after the core path:

- **[OptiX IR Generation](./pipeline/optix-ir.md)** — The alternative output mode for ray tracing workloads.
- **[Debug Info Pipeline](./pipeline/debug-info-pipeline.md)** — How `-g` debug metadata survives the optimizer.
- **[LTO & Module Optimization](./lto/index.md)** — Cross-module optimization when compiling multiple translation units.
- **[Concurrent Compilation](./infra/concurrent-compilation.md)** — The Phase II thread pool and GNU Jobserver integration.
- **[GPU Execution Model](./gpu-execution-model.md)** — Background on warps, divergence, shared memory, and address spaces if you are new to GPU architecture.

### Reading Path 2: Reimplementing a Specific Pass

Goal: reproduce the exact behavior of one NVIDIA custom pass or understand an LLVM pass modification deeply enough to write a compatible replacement.

For an **NVIDIA custom pass** (e.g., MemorySpaceOpt, Rematerialization, BranchDist):

1. **[NVIDIA Custom Passes — Overview](./passes/index.md)** — Locate the pass in the inventory table. Note its category (module/function/loop/machine), its pipeline position, and its controlling knobs.
2. **The pass's dedicated page** (e.g., [MemorySpaceOpt](./passes/memory-space-opt.md), [Rematerialization](./passes/rematerialization.md), [Branch Distribution](./passes/branch-distribution.md)). Every dedicated page contains the function address, decompiled algorithm, data flow description, controlling knobs, and diagnostic strings.
3. **[NVVMPassOptions](./config/nvvm-pass-options.md)** — The 221-slot struct that controls per-pass enable/disable toggles and parametric thresholds. Find which slots your target pass reads.
4. **[Pipeline & Pass Ordering](./llvm/pipeline.md)** — Determine exactly where the pass runs in the pipeline. Identify what analyses it depends on (must run before it) and what passes consume its results (run after it).
5. **[Optimization Levels](./config/optimization-levels.md)** — Determine at which O-levels the pass is enabled, disabled, or parameterized differently.
6. **[Function Map](./function-map.md)** — Cross-reference the pass's internal function addresses with the master function map for confidence levels.

For a **modified LLVM pass** (e.g., InstCombine, GVN, DSE, LICM, LoopVectorize):

1. **The pass's dedicated page** (e.g., [InstCombine](./llvm/instcombine.md), [GVN](./llvm/gvn.md), [DSE](./llvm/dse.md), [LICM](./llvm/licm-real.md)). These pages document NVIDIA's modifications relative to upstream LLVM 20.0.0.
2. **[Alias Analysis & NVVM AA](./infra/alias-analysis.md)** — The custom alias analysis chain. Nearly every optimization pass depends on AA, and NVIDIA's GPU-aware AA behaves differently from upstream (address-space-aware `NoAlias` for disjoint spaces, `__restrict__` propagation).
3. **[MemorySSA](./infra/memoryssa.md)** — The memory dependence representation used by DSE, LICM, and other memory-sensitive passes.

For a **machine-level pass** (e.g., Block Remat, MRPA, Machine Mem2Reg):

1. **[Machine-Level Passes](./llvm/machine-passes.md)** — The complete machine pass pipeline with per-pass algorithm descriptions.
2. **[Register Allocation](./llvm/register-allocation.md)** — The greedy RA algorithm with NVIDIA's occupancy-driven spill heuristics.
3. **[Register Classes](./reference/register-classes.md)** — The 9 PTX register classes and their constraints.
4. **[NVPTX Machine Opcodes](./reference/nvptx-opcodes.md)** — The MachineInstr opcode reference.

Supporting references for any pass reimplementation:

- **[IR Node Layout](./structs/ir-node.md)** — The internal IR data structures that passes operate on.
- **[Address Spaces](./reference/address-spaces.md)** — GPU address space semantics that many passes must respect.
- **[NVPTX Target Infrastructure](./infra/nvptx-target.md)** — TargetMachine, TTI hooks, and target feature queries.
- **[Diagnostics](./infra/diagnostics.md)** — The three diagnostic systems (EDG, LLVM remarks, profuse framework) for reproducing pass-level reporting.

### Reading Path 3: Debugging Correctness

Goal: diagnose a miscompilation, a crash, or incorrect PTX output by tracing the problem to a specific pass or pipeline stage.

Start with instrumentation and observability:

1. **[Diagnostics & Optimization Remarks](./infra/diagnostics.md)** — The three independent diagnostic layers: EDG frontend errors, LLVM optimization remarks (`-opt-bisect-limit`, `-Rpass=`, `-Rpass-missed=`), and NVIDIA's profuse framework (`profuseinline`, `profusegvn`). This page tells you how to make cicc talk about what it is doing.
2. **[Debug Info Verification](./infra/debug-verify.md)** — The three verification modes (`verify-each`, `debugify-each`, and JSON delta reporting). Use `verify-each` to detect the first pass that corrupts debug metadata.
3. **[CLI Flags](./config/cli-flags.md)** — Locate the flags for dumping IR at specific pipeline points: `--print-after-all`, `--print-before-all`, `--filter-print-funcs=`, `--opt-bisect-limit=`. Also the `--passes=` interface for running individual passes in isolation.
4. **[Optimization Levels](./config/optimization-levels.md)** — Compare the pass pipeline at different O-levels. If a bug appears at `-O2` but not `-O1`, the diff between their pipelines identifies the suspect passes.

Then isolate the pipeline stage:

5. **[Pipeline Overview](./pipeline/overview.md)** — Determine which stage produces the incorrect output. The pipeline is linear: EDG → IR Generation → Libdevice Linking → Optimizer → Codegen → Emission. The stage boundary where output first goes wrong narrows the search.
6. **[NVVM IR Verifier](./passes/nvvm-verify-deep.md)** — The 230KB three-layer verifier (module + function + intrinsic). It validates triples, address spaces, atomic restrictions, pointer cast rules, and architecture-gated intrinsic availability. A verification failure after a specific pass is a strong signal.
7. **[Bitcode I/O](./infra/bitcode-io.md)** — If the problem is in bitcode reading/writing (corrupted input, version mismatch), this page documents the reader at `sub_9F2A40` and the writer.

Then investigate the suspect pass:

8. **[NVIDIA Custom Passes](./passes/index.md)** or the relevant **[LLVM pass page](./llvm/pipeline.md)** — Read the algorithm description for the suspect pass. Look for documented edge cases, known limitations, and diagnostic strings that would appear in verbose output.
9. **[NVVMPassOptions](./config/nvvm-pass-options.md)** — Check whether the suspect pass has enable/disable knobs or threshold parameters that could be adjusted to confirm or rule it out.
10. **[Environment Variables](./config/env-vars.md)** — Some passes are gated by environment variables (including obfuscated ones). Check whether any are influencing behavior.

For correctness issues specific to GPU semantics:

- **[Address Spaces](./reference/address-spaces.md)** — Incorrect address space resolution is a common source of silent miscompilation. Global vs. shared vs. local aliasing rules differ from CPU memory models.
- **[MemorySpaceOpt](./passes/memory-space-opt.md)** — This pass resolves generic pointers to specific address spaces. If it infers the wrong space, downstream code will access the wrong memory.
- **[Alias Analysis](./infra/alias-analysis.md)** — If the alias analysis returns `NoAlias` for pointers that do alias, DSE/LICM/GVN will misoptimize. The `process-restrict` propagation is a known source of aggressive alias assumptions.
- **[StructurizeCFG](./llvm/structurizecfg.md)** — PTX requires structured control flow. If structurization produces incorrect flow blocks, the kernel will execute the wrong path.
- **[Dead Barrier Elimination](./passes/dead-barrier-elim.md)** and **[Dead Synchronization Elimination](./passes/dead-sync-elimination.md)** — Incorrect elimination of barriers or synchronization can cause race conditions that only manifest under specific warp configurations.

### Reading Path 4: Tuning Performance

Goal: understand what cicc does at each optimization level, which passes are the performance-critical ones, and what knobs control their aggressiveness.

Start with the tuning infrastructure:

1. **[Optimization Levels](./config/optimization-levels.md)** — The four standard levels (O0--O3) and three fast-compile tiers (Ofcmin/Ofcmid/Ofcmax). This page shows the exact pass pipeline diff between levels, including which passes are added, removed, or reparameterized at each step.
2. **[NVVMPassOptions](./config/nvvm-pass-options.md)** — The 221-slot per-pass configuration system. This is the primary tuning mechanism. The page documents every slot's type (boolean/integer/string), its default value, and which pass reads it.
3. **[CLI Flags](./config/cli-flags.md)** — The flag-to-pipeline routing tables. Locate flags that control pass thresholds (`--inline-threshold=`, `--unroll-count=`, etc.) and pass enable/disable toggles.
4. **[LLVM Knobs](./config/knobs.md)** — The ~1,689 `cl::opt` flags with their defaults, types, and controlling constructors.
5. **[Environment Variables](./config/env-vars.md)** — Runtime environment overrides, including the obfuscated variables.

Then study the high-impact optimization passes:

6. **[LLVM Optimizer](./pipeline/optimizer.md)** — Understand the two-phase model. Phase I (whole-module) determines inlining decisions, inter-procedural memory space propagation, and global optimization. Phase II (per-function, potentially concurrent) does register-pressure-driven rematerialization and instruction scheduling. Tuning decisions in Phase I cascade into Phase II.
7. **[Inliner Cost Model](./lto/inliner-cost.md)** — Inlining is typically the single highest-impact optimization decision. This page documents the cost model thresholds, the caller/callee size heuristics, and NVIDIA's kernel-specific adjustments.
8. **[LoopVectorize & VPlan](./llvm/loop-vectorize.md)** — Loop vectorization for GPU SIMT. The VPlan infrastructure, cost model, and the NVIDIA TTI hooks that influence vectorization width decisions.
9. **[Loop Unrolling](./llvm/loop-unroll.md)** — Unrolling thresholds, the NVIDIA-specific unroll heuristics, and the interaction with register pressure.
10. **[Rematerialization](./passes/rematerialization.md)** — NVIDIA's IR-level rematerialization pass (67KB). Trades recomputation for register pressure reduction, which directly affects occupancy on GPU.
11. **[Register Allocation](./llvm/register-allocation.md)** — The greedy RA with occupancy-driven spill heuristics. Register count directly determines maximum occupancy.
12. **[Instruction Scheduling](./llvm/scheduling.md)** — The scheduler subsystems and their interaction with hardware latency models.

For tensor core workloads specifically:

- **[Tensor / MMA Codegen](./llvm/mma-codegen.md)** — 19 MMA shapes across 11 data types. The instruction selection patterns, register allocation constraints, and WGMMA code generation for Hopper and Blackwell.
- **[Tensor / MMA Builtins](./builtins/tensor-mma.md)** — The builtin-to-intrinsic lowering for `wmma`, `mma`, and `wgmma` operations.
- **[SM 90 — Hopper](./targets/sm90-hopper.md)** — Hopper-specific features: TMA, WGMMA, asynchronous barriers, cluster launch.
- **[SM 100 — Blackwell](./targets/sm100-blackwell.md)** — Blackwell-specific features: new MMA shapes, FP4/FP6 support, sparsity.

For understanding performance at the target level:

- **[GPU Targets](./targets/index.md)** — The SM feature gate matrix. Which features are enabled at each architecture level, and how architecture detection routes to different codegen paths.
- **[NVPTX Target Infrastructure](./infra/nvptx-target.md)** — The TTI hooks that passes query for target-specific costs (memory latency, instruction throughput, register file size).
- **[Concurrent Compilation](./infra/concurrent-compilation.md)** — If compile time itself is the bottleneck, understand the Phase II thread pool and GNU Jobserver integration to maximize parallelism.
