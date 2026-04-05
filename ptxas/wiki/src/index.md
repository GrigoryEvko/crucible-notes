# PTXAS v13.0 — Reverse Engineering Reference

**Purpose:** reimplementation-grade documentation of NVIDIA's PTX-to-SASS assembler, recovered entirely from static analysis of the stripped x86-64 binary.

PTX (Parallel Thread Execution) is NVIDIA's virtual ISA for GPU compute. SASS (Shader Assembly) is the native machine code executed by GPU hardware. PTXAS is the binary that transforms PTX into SASS. At 37.7 MB stripped, it is a fully proprietary compiler with no LLVM code, no EDG frontend, and no third-party optimizer components. Every pass, every data structure, and every encoding table was built in-house by NVIDIA. This wiki documents its internal architecture using IDA Pro 8.x and Hex-Rays decompilation.

> **Version note:** All addresses and binary offsets in this wiki apply to ptxas v13.0.88 (CUDA Toolkit 13.0). Other versions will have different addresses.

| | |
|---|---|
| **Binary** | ptxas v13.0.88, 37,741,528 bytes, x86-64, stripped |
| **Build** | `cuda_13.0.r13.0/compiler.36424714_0` (Aug 20 2025) |
| **Decompilation** | 40,185 functions, IDA Pro 8.x + Hex-Rays |
| **Strings** | 30,632 extracted |
| **Call graph** | 548,693 edges |
| **Version string** | `Cuda compilation tools, release 13.0, V13.0.88` (`sub_612DE0`) |
| **LLVM code** | None — fully proprietary compiler |
| **Default target** | `sm_75` (Turing) |
| **Supported SMs** | sm\_75 through sm\_121f (Turing through DGX Spark) |
| **Internal codename** | OCG (Optimizing Code Generator), Mercury (SASS encoder) |

## Glossary

| Term | Meaning |
|---|---|
| **Ori IR** | PTXAS's internal intermediate representation — basic blocks containing an instruction DAG with typed virtual registers. Named after recovered debug strings; not an acronym. |
| **Mercury** | The SASS binary encoder subsystem. Converts abstract instruction objects into 128-bit packed machine words. Named in NVIDIA source paths and error strings. |
| **OCG** | Optimizing Code Generator — NVIDIA's internal name for the ptxas optimization+codegen pipeline (the 159-phase core). Appears in knob prefixes and timing strings. |
| **Fatpoint** | The register allocation algorithm used by ptxas. A fatpoint is a program point annotated with the set of simultaneously live virtual registers. The allocator works by computing these sets and mapping them to physical registers. |
| **Opex** | Operand expansion — a late pipeline stage that expands abstract operands into concrete SASS encoding fields. Converts virtual register references, immediates, and address modes into the bit patterns Mercury expects. |
| **Capmerc** | Capsule Mercury — an ELF section (`.nv.capmerc`) that embeds a secondary Mercury-encoded representation of the kernel alongside the primary `.text` section. Used for debug metadata and binary patching support. |
| **ELFW** | PTXAS's custom ELF writer (`sub_1C9F280`, 97 KB). Not a standard library — a bespoke emitter that builds CUBIN files with NVIDIA-specific sections, relocations, and symbol conventions. |
| **EIATTR** | Extended Info Attributes — per-kernel metadata encoded in `.nv.info` sections. Each attribute is a tag-length-value record carrying register counts, barrier usage, shared memory sizes, CRS stack depth, and other kernel properties consumed by the CUDA runtime and driver. |

## Three Subsystems

PTXAS is not a monolithic assembler. It decomposes into three largely independent subsystems with distinct coding conventions, data structures, and lineages:

**1. PTX Frontend** (~3 MB, `0x400000`--`0x5AA000`) — A Flex-generated DFA scanner (`sub_720F00`, 64 KB, ~552 rules) feeds tokens into a Bison-generated LALR(1) parser (`sub_4CE6B0`, 48 KB). The parser is driven from `sub_446240` (the real `main`, 11 KB), which orchestrates the full pipeline: parse, DAGgen, OCG, ELF, DebugInfo. The frontend also contains 1,141 instruction descriptors registered via `sub_46E000` (93 KB) that define accepted type combinations for every PTX opcode, 608 CUDA runtime intrinsics registered in `sub_5D1660` (46 KB), and a suite of per-instruction semantic validators (`0x460000`--`0x4D5000`) that check architecture requirements, type compatibility, and operand constraints before lowering. See [PTX Parser](./pipeline/ptx-parser.md) and [Entry Point & CLI](./pipeline/entry.md).

**2. Ori Optimizer** (~8 MB, `0x5AA000`--`0xC52000`) — A proprietary 159-phase optimization pipeline managed by the PhaseManager (`sub_C62720`). The phase factory at `sub_C60D30` is a 159-case switch that allocates polymorphic phase objects from a vtable table at `off_22BD5C8`. Each phase has virtual methods for `execute()`, `isNoOp()`, and `getName()`. Major subsystems include: a fatpoint-based register allocator (`sub_957160` core, `sub_95DC10` driver, `sub_926A30` interference graph builder), a 3-phase instruction scheduler (`sub_688DD0` with ReduceReg/DynBatch modes and 9 register pressure counters), copy propagation, strength reduction, predication (if-conversion), rematerialization, and GMMA/WGMMA pipelining. The pipeline reads its default phase ordering from a 159-entry table at `0x22BEEA0`. See [Optimization Pipeline](./pipeline/optimizer.md) and [Phase Manager](./passes/phase-manager.md).

**3. SASS Backend** (~14 MB, `0xC52000`--`0x1CE3000`) — The Mercury encoder generates native SASS binary code. Instruction encoding is handled by ~4,000 per-variant handler functions (683 + 678 = 1,361 in the SM100 Blackwell encoding tables alone at `0xED1000`--`0x107B000`, with additional tables for other SM generations). Each handler follows a rigid template: set opcode ID, load a 128-bit encoding format descriptor via SIMD, initialize a 10-slot register class map, register operand descriptors via `sub_7BD3C0`/`sub_7BD650`/`sub_7BE090`, finalize with `sub_7BD260`, then extract bitfields from the packed instruction word. The backend also contains 3 peephole optimizers (the `PeepholeOptimizer` class at `0x7A5D10` with `Init`, `RunOnFunction`, `RunOnBB`, `RunPatterns`, `SpecialPatterns`, `ComplexPatterns`, and `SchedulingAwarePatterns` methods), a capsule Mercury ELF embedder for debug metadata (`sub_1CB53A0`, section `.nv.capmerc`), and a custom ELF emitter (`sub_1C9F280`, 97 KB) that builds the final CUBIN output. See [SASS Code Generation](./pipeline/codegen.md), [Mercury Encoder](./codegen/mercury.md), and [Peephole Optimization](./codegen/peephole.md).

Additionally, the binary embeds a custom pool allocator (`sub_424070`, 3,809 callers), MurmurHash3-based hash maps (`sub_426150` insert / `sub_426D60` lookup), a thread pool with pthread-based parallel compilation support, and a GNU Make jobserver client for integration with build systems.

## Compilation Pipeline

Both standalone and library-mode invocations converge on the same pipeline, visible in the timing strings emitted by `sub_446240`:

```
PTX text (.ptx file or string)
  |
  +-- Flex Scanner (sub_720F00, 64KB)
  |     552-rule DFA, off_203C020 transition table
  |     Tokens: 340+ terminal symbols for Bison grammar
  |
  +-- Bison LALR(1) Parser (sub_4CE6B0, 48KB)
  |     Semantic validators: 0x460000-0x4D5000
  |     1,141 instruction descriptors via sub_46E000
  |
  +-- Ori IR Construction (DAGgen phase)
  |     Internal representation: basic blocks + instruction DAG
  |     608 CUDA runtime intrinsics (sub_5D1660)
  |
  +-- 159-Phase Optimization Pipeline (PhaseManager, sub_C62720)
  |     Phase factory: sub_C60D30 (159-case switch)
  |     Fatpoint register allocator (sub_957160)
  |     3-phase instruction scheduler (sub_688DD0)
  |     Copy propagation, CSE, strength reduction, predication,
  |     rematerialization, GMMA pipelining, late legalization
  |
  +-- Mercury SASS Encoder
  |     Instruction encoding: ~4000 per-variant handlers
  |     3 peephole optimizers (PeepholeOptimizer at 0x7A5D10)
  |     WAR hazard resolution (sub_6FC240)
  |     Operand expansion (Opex pipeline)
  |
  +-- ELF/CUBIN Output (sub_1C9F280, 97KB)
        Sections: .text, .nv.constant0, .nv.info, .symtab
        Capsule Mercury: .nv.capmerc (debug metadata)
        DWARF: .debug_line, .debug_info, .debug_frame
```

The driver at `sub_446240` reports per-stage timing: `Parse-time`, `CompileUnitSetup-time`, `DAGgen-time`, `OCG-time`, `ELF-time`, `DebugInfo-time`, plus `PeakMemoryUsage` in KB. For multi-entry PTX files, each compile unit is processed independently with the header `"\nCompile-unit with entry %s"`.

## Dual Compilation Modes

PTXAS operates in two modes selected at invocation:

| | Standalone CLI | Library Mode |
|---|---|---|
| **Invocation** | `ptxas [options] file.ptx` | Called from nvcc/nvlink as a subprocess |
| **Entry** | `main` at `0x409460` | `sub_9F63D0` (library/ftrace entry) |
| **Real driver** | `sub_446240` (11 KB) | Same pipeline, alternate setup |
| **Input** | PTX file on disk | PTX string via `--input-as-string` |
| **Output** | `.cubin` / `.o` file | Binary blob returned to caller |
| **Usage string** | `"Usage  : %s [options] <ptx file>,...\n"` | N/A |

The `main` function (`0x409460`, 84 bytes) is a thin wrapper: it stores `argv[0]`, sets stdout/stderr to unbuffered via `setvbuf`, and delegates to `sub_446240`. The `--input-as-string` flag enables accepting PTX source directly as a CLI argument rather than reading from a file.

## Configuration

PTXAS exposes three layers of configuration:

**CLI Options** (~100 flags) — Registered in `sub_432A00` and parsed by `sub_434320`. Key options include `--gpu-name` (target SM), `--maxrregcount` (register limit), `--opt-level` (0--4), `--verbose`, `--warn-on-spills`, `--warn-on-local-memory-usage`, `--fast-compile`, `--fdevice-time-trace` (Chrome trace JSON output), `--compile-as-tools-patch` (sanitizer mode), and `--extensible-whole-program`. Help is printed by `sub_403588` which calls `sub_1C97640` to enumerate all registered options.

**Internal Knobs** (1,294 ROT13-encoded entries) — A separate configuration system implemented in `generic_knobs_impl.h` (source path recovered: `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h`). The knob table is populated by two massive static constructors: `ctor_005` at `0x40D860` (80 KB, ~2,000 general OCG knobs) and `ctor_007` at `0x421290` (8 KB, 98 Mercury scheduler knobs). All knob names are ROT13-obfuscated in the binary. Examples after decoding: `MercuryUseActiveThreadCollectiveInsts`, `MercuryTrackMultiReadsWarLatency`, `MercuryPresumeXblockWaitBeneficial`, `ScavInlineExpansion`, `ScavDisableSpilling`. Knobs are read from environment variables and knob files via `ReadKnobsFile` (`sub_79D070`) which parses `[knobs]`-header INI files. Lookup is performed by `GetKnobIndex` (`sub_79B240`) with inline ROT13 decoding and case-insensitive comparison. See [Knobs System](./config/knobs.md).

**SM Profile Tables** — Per-architecture capability maps initialized by `sub_607DB0` (14 KB) which creates 7 hash maps indexing `sm_XX` / `compute_XX` strings to handler functions. Profile objects are constructed by `sub_6765E0` (54 KB) with architecture-to-family mappings (sm\_75 -> Turing, sm\_80/86/87/88 -> Ampere, sm\_89 -> Ada Lovelace, sm\_90/90a -> Hopper, sm\_100/100a/100f -> Blackwell, sm\_103/103a/103f -> Blackwell Ultra, sm\_110/110a/110f -> Jetson Thor, sm\_120/120a/120f -> RTX 50xx, sm\_121/121a/121f -> DGX Spark). See [SM Architecture Map](./targets/index.md).

## Reading This Wiki

The wiki is organized around the compilation pipeline. Every page is written at reimplementation-grade depth for an audience of senior C++ developers with GPU compiler experience.

### Section Index

**Overview**
- [Function Map](./function-map.md) — Address-to-identity lookup for key functions with confidence levels.
- [Binary Layout](./binary-layout.md) — Subsystem address map at pass granularity.
- [Methodology](./methodology.md) — How this analysis was performed.
- [Version Tracking](./VERSIONS.md) — Cross-version address deltas.

**Compilation Pipeline**
- [Pipeline Overview](./pipeline/overview.md) — End-to-end PTX-to-SASS flow diagram with links to every stage.
- [Entry Point & CLI](./pipeline/entry.md) — CLI parsing, `main` at `0x409460`, the real driver at `sub_446240`.
- [PTX Parser (Flex + Bison)](./pipeline/ptx-parser.md) — 552-rule Flex DFA scanner, Bison LALR(1) parser, instruction descriptor table.
- [PTX Directive Handling](./pipeline/ptx-directives.md) — `.version`, `.target`, `.entry`, `.func`, `.reg`, `.shared`, `.const` processing.
- [PTX-to-Ori Lowering](./pipeline/ptx-to-ori.md) — How parsed PTX is lowered into the Ori internal representation.
- [Optimization Pipeline (159 Phases)](./pipeline/optimizer.md) — PhaseManager, phase factory, default phase ordering, per-phase timing.
- [SASS Code Generation](./pipeline/codegen.md) — Mercury encoder, instruction selection, operand expansion.
- [ELF/Cubin Output](./pipeline/output.md) — Custom ELF emitter, section layout, capsule Mercury, DWARF generation.

**Ori IR — Internal Representation**
- [IR Overview & Design](./ir/overview.md) — Instruction DAG, basic blocks, typed virtual registers.
- [Instructions & Opcodes](./ir/instructions.md) — Ori opcode set and instruction encoding.
- [Basic Blocks & CFG](./ir/cfg.md) — Control flow graph construction and manipulation.
- [Register Model (R/UR/P/UP)](./ir/registers.md) — Four register classes and their constraints.
- [Data Structure Layouts](./ir/data-structures.md) — Memory layout of key IR objects.

**Optimization Passes**
- [Pass Inventory & Ordering](./passes/index.md) — All 159 phases with names, addresses, and pipeline positions.
- [Phase Manager Infrastructure](./passes/phase-manager.md) — Phase factory, vtable dispatch, execute/isNoOp/getName.
- [GeneralOptimize Bundles](./passes/general-optimize.md) — Mega-pass bundles that group related sub-passes.
- [Loop Passes](./passes/loop-passes.md) — Unrolling, LICM, induction variable optimization, strength reduction.
- [Copy Propagation & CSE](./passes/copy-prop-cse.md) — Value forwarding and common subexpression elimination.
- [Predication](./passes/predication.md) — If-conversion for GPU divergence control.
- [Rematerialization](./passes/rematerialization.md) — Recomputing values to reduce register pressure.
- [Synchronization & Barriers](./passes/sync-barriers.md) — Barrier insertion and dead barrier elimination.
- [Late Expansion & Legalization](./passes/late-legalization.md) — Final lowering before codegen.

**Register Allocation**
- [Allocator Architecture](./regalloc/overview.md) — Fatpoint algorithm, interference graph, spilling, ABI constraints.
- [Fatpoint Algorithm](./regalloc/algorithm.md) — Core allocation loop and heuristics.
- [Spilling](./regalloc/spilling.md) — Spill cost model and spill code generation.
- [GPU ABI & Calling Convention](./regalloc/abi.md) — Register assignment rules and caller/callee contracts.

**Instruction Scheduling**
- [Scheduler Architecture](./scheduling/overview.md) — 3-phase scheduler, ReduceReg/DynBatch modes.
- [Scheduling Algorithm](./scheduling/algorithm.md) — Priority list scheduling with register pressure tracking.
- [Latency Model & HW Profiles](./scheduling/latency-model.md) — Per-SM instruction latency tables.
- [Scoreboards & Dependency Barriers](./scheduling/scoreboards.md) — WAR hazard resolution, barrier allocation.

**SASS Code Generation**
- [Code Generation Overview](./codegen/overview.md) — Instruction selection, encoding, peephole, Mercury.
- [Instruction Selection](./codegen/isel.md) — Pattern-based DAG-to-SASS lowering.
- [SASS Instruction Encoding](./codegen/encoding.md) — 128-bit instruction word format and bitfield packing.
- [Peephole Optimization](./codegen/peephole.md) — Three peephole dispatchers with SM-variant patterns.
- [Mercury Encoder](./codegen/mercury.md) — Per-variant handler architecture, encoding tables.
- [Capsule Mercury & Finalization](./codegen/capmerc.md) — `.nv.capmerc` section, debug metadata embedding.
- [SASS Text Generation](./codegen/sass-printing.md) — Disassembly-format printing for `--verbose` output.

**GPU Architecture Targets**
- [SM Architecture Map](./targets/index.md) — SM feature gates from sm\_75 through sm\_121f.
- [Turing & Ampere (SM 75--88)](./targets/turing-ampere.md) — Feature delta between generations.
- [Ada & Hopper (SM 89--90a)](./targets/ada-hopper.md) — Async copy, TMA, distributed shared memory.
- [Blackwell (SM 100--121)](./targets/blackwell.md) — TCGen05, fifth-gen tensor cores, new SM variants.
- [TCGen05 — 5th Gen Tensor Cores](./targets/tcgen05.md) — Blackwell tensor core instruction set.

**CUDA Intrinsics**
- [Intrinsic Table (608 Entries)](./intrinsics/index.md) — Math, tensor, sync, warp intrinsics.
- [Math Intrinsics](./intrinsics/math.md) — Fast-math, Newton-Raphson, special functions.
- [Tensor Core Intrinsics](./intrinsics/tensor.md) — WMMA, GMMA, WGMMA instruction families.
- [Sync & Warp Intrinsics](./intrinsics/sync-warp.md) — Barrier, vote, shuffle, match.

**ELF/Cubin Output**
- [Custom ELF Emitter](./output/elf-emitter.md) — ELFW internals, section construction, symbol table.
- [Section Catalog & EIATTR](./output/sections.md) — `.nv.info` attribute encoding, per-kernel metadata.
- [Debug Information](./output/debug-info.md) — DWARF generation for GPU debugging.
- [Relocations & Symbols](./output/relocations.md) — CUBIN relocation types and symbol conventions.

**Configuration**
- [CLI Options](./config/cli-options.md) — ~100 flags registered in `sub_432A00`.
- [Knobs System (1,294 Knobs)](./config/knobs.md) — ROT13 knob table, environment variables, INI files.
- [Optimization Levels](./config/opt-levels.md) — O-level to phase mapping, `--fast-compile` tiers.
- [DUMPIR & NamedPhases](./config/dumpir.md) — Dumping IR at specific pipeline points.

**Infrastructure**
- [Memory Pool Allocator](./infra/memory-pools.md) — `sub_424070`, 3,809 callers, arena-style allocation.
- [Hash Tables & Bitvectors](./infra/hash-bitvector.md) — MurmurHash3-based maps, bitvector liveness sets.
- [Thread Pool & Concurrency](./infra/threading.md) — pthread pool, GNU Make jobserver client.

**Reference**
- [SASS Opcode Catalog](./reference/sass-opcodes.md) — Complete SASS opcode enumeration.
- [PTX Instruction Table](./reference/ptx-instructions.md) — All PTX instructions with type signatures.
- [EIATTR Attribute Catalog](./reference/eiattr.md) — Tag-length-value format for `.nv.info` attributes.

### Reading Path 1: End-to-End Pipeline Understanding

Goal: understand how PTX text becomes SASS binary, what each stage does, and how control flows between subsystems.

1. **[Pipeline Overview](./pipeline/overview.md)** — The complete flow diagram. Establishes all stages and their address ranges.
2. **[Entry Point & CLI](./pipeline/entry.md)** — How ptxas is invoked, the ~100 CLI flags, and the `sub_446240` driver function.
3. **[PTX Parser](./pipeline/ptx-parser.md)** — The Flex scanner and Bison parser. How PTX text becomes an internal parse tree.
4. **[PTX-to-Ori Lowering](./pipeline/ptx-to-ori.md)** — How the parse tree is lowered to Ori IR (basic blocks + instruction DAG).
5. **[Optimization Pipeline](./pipeline/optimizer.md)** — The 159-phase PhaseManager. Phase factory, ordering, timing infrastructure.
6. **[SASS Code Generation](./pipeline/codegen.md)** — Mercury encoder, instruction selection, operand expansion, peephole.
7. **[ELF/Cubin Output](./pipeline/output.md)** — Custom ELF emitter, section layout, DWARF debug info, capsule Mercury.

### Reading Path 2: Reimplementing a Specific Pass

Goal: reproduce the exact behavior of one optimization phase deeply enough to write a compatible replacement.

1. **[Pass Inventory & Ordering](./passes/index.md)** — Locate the phase in the 159-entry table. Note its index, vtable address, and pipeline position.
2. **The phase's dedicated page** (e.g., [Copy Propagation & CSE](./passes/copy-prop-cse.md), [Predication](./passes/predication.md)). Every dedicated page contains the function address, decompiled algorithm, data flow, and controlling knobs.
3. **[Knobs System](./config/knobs.md)** — Find which ROT13 knobs control the phase's behavior (enable/disable toggles, thresholds).
4. **[Ori IR Overview](./ir/overview.md)** — Understand the IR data structures the phase operates on.
5. **[Register Model](./ir/registers.md)** — The R/UR/P/UP register classes and their constraints.
6. **[Function Map](./function-map.md)** — Cross-reference internal function addresses with the master function map.

### Reading Path 3: Debugging Correctness

Goal: diagnose a miscompilation, crash, or incorrect SASS output by tracing the problem to a specific phase.

1. **[DUMPIR & NamedPhases](./config/dumpir.md)** — How to dump IR at specific pipeline points. Use `DUMPIR` to observe the IR before and after each phase.
2. **[Optimization Levels](./config/opt-levels.md)** — Compare phase pipelines at different O-levels. If a bug appears at `-O2` but not `-O1`, the diff identifies suspect phases.
3. **[Pipeline Overview](./pipeline/overview.md)** — The pipeline is linear: Parse -> DAGgen -> OCG (159 phases) -> Mercury -> ELF. The stage where output first goes wrong narrows the search.
4. **[Knobs System](./config/knobs.md)** — Check whether the suspect phase has enable/disable knobs. Toggle them to confirm or rule out the phase.
5. **[Instruction Scheduling](./scheduling/overview.md)** and **[Scoreboards & Dependency Barriers](./scheduling/scoreboards.md)** — If the generated SASS hangs or produces wrong results under specific warp configurations, the scheduler or barrier insertion may be at fault.

### Reading Path 4: Tuning Performance

Goal: understand what ptxas does at each optimization level and what knobs control aggressiveness.

1. **[Optimization Levels](./config/opt-levels.md)** — The O-level to phase mapping, including `--fast-compile` tiers.
2. **[Knobs System](./config/knobs.md)** — The 1,294 ROT13-encoded internal tuning parameters. The primary mechanism for fine-grained control.
3. **[Register Allocation](./regalloc/overview.md)** — The fatpoint allocator directly determines register count, which determines maximum occupancy.
4. **[Instruction Scheduling](./scheduling/overview.md)** — The scheduler's ReduceReg and DynBatch modes, WAR hazard resolution, and interaction with register pressure.
5. **[Peephole Optimization](./codegen/peephole.md)** — The 3 peephole dispatchers that perform late SASS-level rewrites.
6. **[SM Architecture Map](./targets/index.md)** — Per-SM feature gates that influence code generation decisions.
