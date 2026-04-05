# Compilation Pipeline Overview

This page maps the complete end-to-end flow of a PTX assembly through ptxas v13.0.88, from the initial CLI invocation to the final ELF/cubin binary output. Each stage is a self-contained subsystem with its own address range, data structures, and failure modes. The links below lead to dedicated pages with reimplementation-grade detail for every stage.

## Pipeline Diagram

```
nvcc / cicc
  |  (PTX text file or --input-as-string)
  v
+================================================================+
| ptxas v13.0.88 (37.7 MB, ~40,000 functions)                   |
|                                                                |
|  1. Entry & CLI Parsing ----------> [entry.md]                 |
|     |  main -> sub_446240 -> sub_434320                        |
|     |  target arch, opt level, --maxrregcount, knobs           |
|     v                                                          |
|  2. PTX Lexer + Parser -----------> [ptx-parser.md]            |
|     |  sub_451730: Flex scanner, Bison grammar                 |
|     |  ROT13-decoded opcode table (900+ mnemonics)             |
|     |  30+ per-instruction semantic validators                 |
|     v                                                          |
|  3. PTX Directive Handling --------> [ptx-directives.md]       |
|     |  .version, .target, .entry, .func, .reg, .shared         |
|     |  register constraints, ABI configuration                 |
|     v                                                          |
|  4. PTX-to-Ori Lowering ----------> [ptx-to-ori.md]           |
|     |  PTX AST -> Ori IR (basic blocks, virtual registers)     |
|     |  address space annotation, special register mapping      |
|     v                                                          |
|  5. 159-Phase Optimization -------> [optimizer.md]             |
|     |  PhaseManager: sub_C62720 (constructor),                 |
|     |                sub_C64F70 (executor)                     |
|     |  10 stages, 17 AdvancedPhase hooks,                     |
|     |  8-phase Mercury encoding sub-pipeline                   |
|     |  per-kernel via sub_7FBB70 -> sub_7FB6C0                 |
|     v                                                          |
|  6. Register Allocation ----------> [../regalloc/overview.md]  |
|     |  Fatpoint algorithm, phase 101 (AdvancedPhaseAllocReg)   |
|     |  spill/fill insertion, ABI register reservations          |
|     v                                                          |
|  7. Instruction Scheduling -------> [../scheduling/overview.md]|
|     |  3-phase: pre-schedule (97), post-schedule (106),        |
|     |           post-fixup (111)                               |
|     |  scoreboard generation, dependency barriers              |
|     v                                                          |
|  8. SASS Encoding ----------------> [../codegen/encoding.md]   |
|     |  530 instruction encoding handlers (vtable dispatch)     |
|     |  Mercury format: phases 113-122                          |
|     |  Capsule Mercury (default on sm_100+)                    |
|     v                                                          |
|  9. ELF/Cubin Output -------------> [output.md]               |
|     |  sub_612DE0 (finalizer) -> sub_1C9F280 (ELF emitter)    |
|     |  section layout, symbol table, relocations               |
|     |  DWARF debug info, EIATTR attributes                     |
|     v                                                          |
|  OUTPUT: .cubin / .o (ELF)                                    |
+================================================================+

Side paths:
  * Capsule Mercury (--cap-merc) -----> [../codegen/capmerc.md]
  * Debug info (all stages) ----------> [../output/debug-info.md]
  * SASS text (--verbose) ------------> [../codegen/sass-printing.md]
```

## Timed Phases

The compilation driver `sub_446240` measures six timed phases per compile unit and reports them when `--compiler-stats` is enabled. The format strings are embedded directly in the binary:

| Phase | Format String | Subsystem |
|---|---|---|
| Parse-time | `"Parse-time            : %.3f ms (%.2f%%)\n"` | PTX lexer + Bison parser + semantic validation |
| CompileUnitSetup-time | `"CompileUnitSetup-time : %.3f ms (%.2f%%)\n"` | Target configuration, ABI setup, register constraints |
| DAGgen-time | `"DAGgen-time           : %.3f ms (%.2f%%)\n"` | PTX-to-Ori lowering, CFG construction, initial DAG formation |
| OCG-time | `"OCG-time              : %.3f ms (%.2f%%)\n"` | Optimized Code Generation: all 159 optimization phases, register allocation, instruction scheduling, SASS encoding |
| ELF-time | `"ELF-time              : %.3f ms (%.2f%%)\n"` | ELF construction, section layout, symbol table, relocations, EIATTR, file write |
| DebugInfo-time | `"DebugInfo-time        : %.3f ms (%.2f%%)\n"` | DWARF `.debug_info`/`.debug_line`/`.debug_frame` generation, LEB128 encoding |

Additional aggregate stats:

```
CompileTime = %f ms (100%)
PeakMemoryUsage = %.3lf KB
```

The per-unit header prints `"\nCompile-unit with entry %s"` before each kernel's phase breakdown.

## Per-Kernel Parallelism

ptxas supports two compilation modes for multi-kernel PTX modules:

### Single-Threaded Mode (Default)

The compilation driver `sub_446240` iterates over compile units sequentially. For each kernel entry:

1. `sub_43CC70` -- per-entry compilation unit processor, skips `__cuda_dummy_entry__`
2. `sub_7FBB70` -- per-kernel entry point, prints `"\nFunction name: "` + kernel name
3. `sub_7FB6C0` -- pipeline orchestrator: builds phases via `sub_C62720`, executes via `sub_C64F70`
4. Cleanup: destroys 17 analysis data structures (live ranges, register maps, scheduling state)

Each kernel runs through the entire 159-phase pipeline independently. Cross-kernel state is limited to shared memory layout and the global symbol table.

### Thread Pool Mode (`--split-compile`)

When `--allow-expensive-optimizations` or `--split-compile` is active, ptxas uses a pthread-based thread pool for per-kernel parallelism:

- **Pool constructor** (`sub_1CB18B0`): allocates a 184-byte pool struct (`0xB8`), spawns N detached worker threads via `pthread_create`, initializes mutex at +24, two condition variables at +64 and +112
- **Task submit** (`sub_1CB1A50`): allocates a 24-byte task node `{func_ptr, arg, next}`, enqueues via linked list, broadcasts on `cond_work`
- **Jobserver integration** (`sub_1CC7300`): reads `MAKEFLAGS` environment variable, parses `--jobserver-auth=` for either `fifo:` named pipes or pipe-based file descriptors, throttles thread count to respect GNU Make's `-j` slot limit

The thread pool is used throughout the OCG and ELF phases (stages 5-9 in the diagram). Each worker thread receives its own thread-local context (`sub_4280C0`, 280-byte TLS struct with per-thread error flags, memory pool pointer, diagnostic suppression state, and synchronization primitives).

### Thread-Local Context Layout

```
struct ThreadLocalContext {  // 280 bytes (0x118), per-thread via pthread_getspecific
    uint64_t error_flags;          // +0:   error/warning state
    uint64_t has_error;            // +8:   error flag
    // ... internal fields ...
    void*    memory_pool;          // +192: per-thread memory pool pointer
    // ... diagnostic suppression fields at +384..+416 ...
    pthread_cond_t  cond;          // +128: condition variable (48 bytes)
    pthread_mutex_t mutex;         // +176: mutex (40 bytes)
    sem_t           sem;           // +216: semaphore
};
```

Accessed by `sub_4280C0` (3,928 callers -- the single most-called function in the binary). On first call in a new thread, allocates and initializes via `malloc(0x118)` + `memset` + `pthread_cond_init` + `pthread_mutex_init` + `sem_init`.

## Key Function Call Chain

The top-level control flow from program entry to ELF output:

```
main (0x409460, 84 bytes)
  |  setvbuf(stdout/stderr, unbuffered)
  v
sub_446240 (0x446240, 11KB) ---- "Top-level compilation driver"
  |
  |-- sub_434320 (0x434320, 10KB) -- Parse CLI options, validate flags
  |     reads: --gpu-name, --maxrregcount, --opt-level, --verbose,
  |            --compiler-stats, --split-compile, --fast-compile
  |
  |-- [allocate "Top level ptxas memory pool"]
  |-- [allocate "Command option parser" pool]
  |
  |-- sub_445EB0 (setup) ----------- Target configuration, texturing mode
  |     sub_43A400 --------------- SM-specific defaults ("ptxocg.0.0")
  |     sub_43B660 --------------- Register/resource constraint calculation
  |
  |-- sub_451730 (0x451730, 14KB) -- Parser initialization
  |     |  "PTX parsing state" pool allocation
  |     |  Builtin symbol table: %ntid, %laneid, %smid, %clock64, ...
  |     |  sub_46E000 (93KB) ---- Opcode-to-handler dispatch table (1168 callees)
  |     v
  |   [Flex lexer + Bison parser: PTX text -> AST]
  |
  |-- for each compile unit:
  |     sub_4428E0 (0x4428E0, 14KB) -- PTX input validation
  |     |  .version/.target checks, ABI mode selection
  |     |  --extensible-whole-program, --compile-only handling
  |     |
  |     sub_43CC70 (5.4KB) --------- Per-entry unit processor
  |     |  skip __cuda_dummy_entry__
  |     |  generate .sass and .ucode sections
  |     |
  |     sub_7FBB70 (198 bytes) ----- Per-kernel entry point
  |       |
  |       sub_7FB6C0 (1.2KB) ------- Pipeline orchestrator
  |         |  check knob 298 (NamedPhases mode)
  |         |  if NamedPhases: delegate to sub_9F63D0
  |         |  else:
  |         |    sub_C62720 -- PhaseManager constructor (159 phases)
  |         |    sub_C60D20 -- get default phase table (at 0x22BEEA0)
  |         |    sub_C64F70 -- execute all phases
  |         |  cleanup: destroy 17 analysis data structures
  |         v
  |       [159-phase pipeline: optimization -> regalloc -> scheduling -> encoding]
  |
  |-- sub_612DE0 (0x612DE0, 47KB) -- Kernel finalizer / ELF builder
  |     |  "Finalizer fastpath optimization"
  |     |  version: "Cuda compilation tools, release 13.0, V13.0.88"
  |     |  build:   "Build cuda_13.0.r13.0/compiler.36424714_0"
  |     |
  |     sub_1CB53A0 (13KB) ------- ELF world initializer (672-byte object)
  |     |  "elfw memory space", .shstrtab, .strtab, .symtab
  |     |
  |     sub_1CB3570 (10KB) ------- Add .text.FUNCNAME sections (44 callers)
  |     sub_1CB68D0 (49KB) ------- Symbol table builder
  |     sub_1CABD60 (67KB) ------- Section layout & memory allocation
  |     sub_1CD48C0 (22KB) ------- Relocation resolver
  |     sub_1C9B110 (23KB) ------- Mercury capsule builder (capmerc)
  |     sub_1C9F280 (97KB) ------- Master ELF emitter (largest in range)
  |     sub_1CD13A0 (11KB) ------- Final file writer
  |
  v
[report CompileTime, PeakMemoryUsage, per-phase breakdown]
```

## Memory Pools

ptxas uses a custom hierarchical pool allocator (`sub_424070` / `sub_4248B0`, the most-called allocation functions with 3,809 and 1,215 callers respectively) instead of the system `malloc`/`free`. Three named pools are created during the top-level driver:

| Pool Name | Created By | Lifetime | Purpose |
|---|---|---|---|
| `"Top level ptxas memory pool"` | `sub_446240` | Entire compilation | Global allocations, cross-kernel data structures |
| `"Command option parser"` | `sub_446240` | Entire compilation | CLI option storage, flag validation state |
| `"Permanent OCG memory pool"` | OCG initialization | Per-kernel | Optimization phase state, instruction IR, register maps |

Additional per-subsystem pools exist:

- `"PTX parsing state"` -- created by `sub_451730`, holds the lexer/parser symbol tables and AST nodes
- `"elfw memory space"` -- created by `sub_1CB53A0`, holds the ELF world object (672 bytes) and section data

### Pool Allocator Internals

The allocator at `sub_424070` implements a dual-path design:

- **Small allocations** (up to 4,999 bytes / `0x1387`): 8-byte-aligned, size-class binned free lists at pool struct offset +2128. Pop from free list head on alloc, push back on free.
- **Large allocations** (above 4,999 bytes): boundary-tag allocator with coalescing of adjacent free blocks.
- **Thread safety**: `pthread_mutex_lock`/`unlock` around all pool operations, mutex at pool struct offset +7128.
- **OOM handling**: calls `sub_42BDB0` (3,825 callers) which triggers `longjmp`-based fatal abort via `sub_42F590`.

## Pipeline Stage Breakdown

### Stage 1: Parse (Parse-time)

The Flex-generated scanner and Bison-generated parser consume PTX text and produce an internal AST. The opcode dispatch table at `sub_46E000` (93KB, 1,168 callees) registers type-checking rules for every PTX instruction. Thirty separate validator functions (in `0x460000`-`0x4D5000`) enforce SM architecture requirements, PTX version constraints, operand types, and state space compatibility. See [PTX Parser](ptx-parser.md).

### Stage 2: CompileUnitSetup (CompileUnitSetup-time)

Target configuration via `sub_43A400`: sets SM-specific defaults (texturing mode, cache policies, `def-load-cache`, `force-load-cache`), applies `--fast-compile` shortcuts, configures ABI (parameter registers, return address register, scratch registers). Register constraints computed by `sub_43B660` from `.maxnreg`, `--maxrregcount`, `.minnctapersm`, and `.maxntid` directives. See [Entry Point & CLI](entry.md).

### Stage 3: DAGgen (DAGgen-time)

Lowers the validated PTX AST into the Ori intermediate representation: basic blocks with a control flow graph, virtual registers, and memory space annotations. Special PTX registers (`%ntid`, `%laneid`, `%smid`, `%ctaid`, etc.) are mapped to internal identifiers. Operand processing at `sub_6273E0` (44KB) handles address computation with a 6-bit operand type encoding. See [PTX-to-Ori Lowering](ptx-to-ori.md).

### Stage 4: OCG (OCG-time)

The core of ptxas: the 159-phase Optimized Code Generation pipeline. This single timed phase encompasses:

- **Early optimization** (phases 13-36): general optimization, branch/switch, loop simplification, strength reduction, unrolling, pipelining, barrier removal
- **Mid-level optimization** (phases 37-58): GVN/CSE, reassociation, commoning, late expansion, speculative hoisting
- **Late optimization** (phases 59-95): loop fusion, predication, GMMA propagation, legalization
- **Register allocation** (phase 101): Fatpoint algorithm
- **Instruction scheduling** (phases 97, 106, 111): pre-schedule, post-schedule, post-fixup
- **Mercury encoding** (phases 113-122): SASS binary format generation

The PhaseManager (`sub_C62720`) instantiates phases via a 159-case factory switch (`sub_C60D30`), each phase a 16-byte polymorphic object with a vtable providing `execute()`, `isNoOp()`, and `getName()` methods. See [Optimization Pipeline](optimizer.md).

### Stage 5: ELF (ELF-time)

The finalizer `sub_612DE0` (47KB) assembles the NVIDIA ELF/cubin from the compiled SASS. Section layout (`sub_1CABD60`, 67KB) assigns addresses to shared memory, constant banks (with OCG deduplication), local memory, and reserved shared memory (`.nv.reservedSmem.begin/cap/offset0`). The master ELF emitter `sub_1C9F280` (97KB) constructs headers, section tables, and program headers. Three binary output modes exist:

1. **mercury** -- traditional SASS binary format
2. **capmerc** -- Capsule Mercury (default on sm_100+), embeds PTX source in `.nv.merc.*` sections
3. **sass** -- direct SASS output

See [ELF/Cubin Output](output.md).

### Stage 6: DebugInfo (DebugInfo-time)

DWARF debug information generation: `.debug_info`, `.debug_line`, `.debug_frame` sections. The LEB128 encoder at `sub_45A870` handles all variable-length integer encoding. Source location tracking uses the location map (hash map at `sub_426150`/`sub_426D60`) with file offset caching every 10 lines for fast random access. Labels follow the pattern `.L__$locationLabel$__%d`. See [Debug Information](../output/debug-info.md).

## Architecture Dispatch

An architecture vtable factory at `sub_1CCEEE0` (17KB, 244 callees) constructs a 632-byte vtable object (79 function pointers) based on the target SM version. The version dispatch ranges:

| Range | Architecture | Generation |
|---|---|---|
| sm_30-39 | Kepler | 1st gen |
| sm_50-59 | Maxwell | 2nd gen |
| sm_60-69 | Pascal | 3rd gen |
| sm_70-79 | Volta / Turing | 4th gen |
| sm_80-89 | Ampere / Ada | 5th gen |
| sm_90 | Hopper | 6th gen |
| sm_100-109 | Blackwell | 7th gen |
| sm_120-121 | Consumer / DGX Spark | 7th gen (desktop) |

Each vtable entry is a function pointer to an SM-specific implementation of a codegen or emission primitive. This is the central dispatch mechanism for all architecture-dependent behavior in the backend.

## Obfuscation: ROT13 Encoding

All internal identifiers in ptxas's static initializers are ROT13-encoded:

- **Opcode table** (`ctor_003` at `0x4095D0`, 17KB): 900+ PTX opcode mnemonics. Example: `NPDOHYX` decodes to `ACQBULK`, `SZN` decodes to `FMA`, `RKVG` decodes to `EXIT`.
- **General knob table** (`ctor_005` at `0x40D860`, 80KB): 2,000+ Mercury/OCG tuning knob names with hex default values. Example: `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` decodes to `MercuryUseActiveThreadCollectiveInsts`.
- **Scheduler knob table** (`ctor_007` at `0x421290`, 8KB): 98 scheduler-specific knob names. Example: `XBlockWaitOut`, `ScavInlineExpansion`.

The ROT13 decoding is performed inline during lookup (in `sub_79B240`, `GetKnobIndex`) using character-range detection: bytes in `A-M` get +13, bytes in `N-Z` get -13, with case-insensitive comparison via `tolower()`.

## Cross-References

- [Binary Layout](../binary-layout.md) -- address ranges for every subsystem
- [Function Map](../function-map.md) -- master index of recovered function addresses
- [CLI Options](../config/cli-options.md) -- complete flag catalog
- [Knobs System](../config/knobs.md) -- 1,294 internal tuning parameters
- [Optimization Levels](../config/opt-levels.md) -- what changes at `-O0`/`-O1`/`-O2`/`-O3`
- [Phase Manager](../passes/phase-manager.md) -- PhaseManager object layout and dispatch
- [Memory Pool Allocator](../infra/memory-pools.md) -- pool struct layout and allocation algorithm
- [Thread Pool & Concurrency](../infra/threading.md) -- thread pool struct, task submit, jobserver

## Function Map

| Address | Size | Callers | Identity | Confidence |
|---|---|---|---|---|
| `0x409460` | 84 B | -- | `main` (entry point) | CERTAIN |
| `0x446240` | 11 KB | 1 | Top-level compilation driver | HIGH |
| `0x434320` | 10 KB | 1 | CLI option parser + validator | HIGH |
| `0x445EB0` | -- | 1 | Target configuration setup | HIGH |
| `0x43A400` | 4.7 KB | 1 | SM-specific default configuration | HIGH |
| `0x43B660` | 3.8 KB | 1 | Register/resource constraint calculator | HIGH |
| `0x451730` | 14 KB | 1 | Parser init + special register setup | HIGH |
| `0x46E000` | 93 KB | 1 | Opcode dispatch table builder (1,168 callees) | HIGH |
| `0x4428E0` | 14 KB | 1 | PTX input validation + preprocessing | HIGH |
| `0x43CC70` | 5.4 KB | 1 | Per-entry compilation unit processor | HIGH |
| `0x7FBB70` | 198 B | vtable | Per-kernel entry point | CERTAIN |
| `0x7FB6C0` | 1.2 KB | 1 | Pipeline orchestrator | CERTAIN |
| `0xC62720` | 4.7 KB | 1 | PhaseManager constructor | VERY HIGH |
| `0xC60D30` | 3.6 KB | 1 | Phase factory (159-case switch) | VERY HIGH |
| `0xC64F70` | -- | 1 | Phase executor | HIGH |
| `0x9F63D0` | 342 B | 1 | NamedPhases executor | VERY HIGH |
| `0x612DE0` | 47 KB | 1 | Kernel finalizer / ELF builder | HIGH |
| `0x1C9F280` | 97 KB | 1 | Master ELF emitter | HIGH |
| `0x1CB53A0` | 13 KB | 1 | ELF world initializer (672-byte object) | HIGH |
| `0x1CABD60` | 67 KB | 1 | Section layout & memory allocator | HIGH |
| `0x1CD13A0` | 11 KB | 2 | Final ELF file writer | HIGH |
| `0x1CB18B0` | ~200 B | 1 | Thread pool constructor | HIGH |
| `0x1CB1A50` | ~200 B | N | Thread pool task submit | HIGH |
| `0x1CC7300` | 8 KB | 1 | GNU Make jobserver client | HIGH |
| `0x1CCEEE0` | 17 KB | 3 | Architecture vtable factory | HIGH |
| `0x424070` | 2.1 KB | 3,809 | Pool allocator: alloc(pool, size) | HIGH |
| `0x4248B0` | 923 B | 1,215 | Pool allocator: free(ptr) | HIGH |
| `0x4280C0` | 597 B | 3,928 | Thread-local context accessor | HIGH |
| `0x426150` | 2.5 KB | 2,800 | Hash map: put(map, key, value) | HIGH |
| `0x42FBA0` | 2.4 KB | 2,350 | Diagnostic message emitter | HIGH |
