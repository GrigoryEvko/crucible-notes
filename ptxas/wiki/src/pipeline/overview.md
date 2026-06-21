# Compilation Pipeline Overview

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

This page maps the complete end-to-end flow of a PTX assembly through ptxas v13.0.88, from the initial CLI invocation to the final ELF/cubin binary output. Each stage is a self-contained subsystem with its own address range, data structures, and failure modes. The links below lead to dedicated pages with reimplementation-grade detail for every stage.

## Pipeline Diagram

```text
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
|  5. Optimization (159 reg / 157 run) -> [optimizer.md]        |
|     |  PhaseManager: sub_C62720 (registers 159),               |
|     |                sub_C64F70 (dispatches 157)               |
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

## Narrative Walk-Through: One Kernel, Start to Finish

A concrete trace of a single-kernel PTX module compiled for sm_100 at `-O2`:

**1. PTX text arrives** (~2–200 KB). Either read from a `.ptx` file or received in-memory via `--input-as-string` from nvcc. The driver `sub_446240` establishes a `setjmp` recovery point, parses CLI options into the 1,352-byte options block, and allocates the `"Top level ptxas memory pool"`.

**2. Lexer + Parser** (`sub_451730`). A Flex-generated scanner tokenizes the PTX text into a token stream. Tokens flow into a Bison-generated LALR parser that builds an AST. The opcode dispatch table (`sub_46E000`, 93 KB, 1,168 callees) routes each instruction mnemonic through ROT13 decoding, type resolution, and 30+ per-instruction semantic validators. For a 5 KB PTX kernel, the parser typically produces ~200–500 AST nodes with ~50 virtual register declarations. The `"PTX parsing state"` pool holds all AST memory.

**3. Directive processing and CompileUnitSetup.** `.version`/`.target` directives configure the SM profile via `sub_6765E0` (54 KB profile constructor). `.entry`/`.func` directives establish the kernel boundary. `.reg`/`.shared`/`.const` directives declare resources. `sub_43B660` computes the physical register budget from `.maxnreg`, `--maxrregcount`, and `.maxntid` constraints. The 1,936-byte profile object is now populated with codegen factory value (36864 for sm_100), scheduling parameters, and capability flags.

**4. PTX-to-Ori lowering (DAGgen).** `sub_6273E0` (44 KB) converts each AST instruction into an Ori IR node: a basic block with virtual registers, control flow edges, and memory space annotations. Special registers (`%ntid`, `%laneid`, `%smid`) map to internal IDs. Address computation uses a 6-bit operand type encoding. A 500-instruction PTX kernel typically produces ~600–1,200 Ori instructions (expansion from pseudo-ops, address calculations, and predicate materialization). The `"Permanent OCG memory pool"` is created here to hold all IR state.

**5. OCG phase pipeline** (`sub_C62720` registers 159 phase objects, `sub_C64F70` dispatches 157).

- Each phase is a 16-byte polymorphic object with `execute()`, `getIndex()`, and `isNoOp()` vtable methods.
- The driver iterates the 157-entry identity order table at `0x22BEEA0` (count from `sub_C60D20`'s `rdx`), calling `execute()` on every entry unconditionally; phases that do nothing return early inside their own body (the `isNoOp()` virtual only suppresses the Before/After banner).
- At `-O2`, roughly 80–100 of the dispatched phases do real work.
- **Typical expansion factors:** the initial 1,000 Ori instructions may grow to 1,200–1,500 after unrolling and intrinsic expansion, then shrink to 800–1,000 after CSE/DCE, then re-expand to 1,500–2,500 after register allocation spill/fill insertion.
- The PhaseManager logs `"Before <phase>"` / `"After <phase>"` strings (visible in the `sub_C64F70` decompile) when phase-wise compile-stats are enabled.

**6. Register allocation** (phase 101, `sub_971A90`). The Fatpoint algorithm attempts NOSPILL allocation first. If pressure exceeds the register budget, the spill guidance engine (`sub_96D940`, 84 KB) computes spill candidates across 7 register classes, and the retry loop makes up to N attempts (knob 638/639) with progressively more aggressive spilling. Physical register assignments are committed; spill/fill instructions are inserted into the Ori IR.

**7. Instruction scheduling** (phases 97, 106, 111). Three scheduling passes assign dependency barriers and reorder instructions for pipeline throughput. The scoreboard generator tracks 6 dependency barriers per warp. For a 1,500-instruction kernel, scheduling typically produces a ~2,000–3,000-entry instruction stream after barrier insertion and NOP padding.

**8. SASS encoding** (phases 113–122). Each Ori instruction is lowered to a 128-bit SASS binary instruction via the 530-handler vtable dispatch. The 1,280-bit (160-byte) encoding workspace at `instruction+544` is filled by `sub_7B9B80` (bitfield insert, 18,347 callers). A 2,000-instruction kernel produces ~32 KB of raw SASS binary. On sm_100+, Capsule Mercury (capmerc) is the default format, embedding PTX source alongside the SASS.

**9. ELF/cubin emission** (`sub_612DE0`, 47 KB decomp). The finalizer assembles the cubin: `.text.FUNCNAME` (SASS binary), `.nv.info.FUNCNAME` (EIATTR attributes), `.nv.shared.FUNCNAME` (shared memory layout), `.nv.constant0.FUNCNAME` (constant bank), plus global sections (`.shstrtab`, `.strtab`, `.symtab`). Section layout (`sub_1CABD60`, 11,856 B native / 66 KB decomp) assigns addresses; the master ELF emitter (`sub_1C9F280`, 15,263 B native / 97 KB decomp) writes headers, section tables, and program headers. A single-kernel cubin for a medium-complexity kernel is typically 40–120 KB.

**Approximate data sizes at each stage (medium kernel, sm_100, -O2):**

| Stage | Input | Output | Peak Memory |
|---|---|---|---|
| PTX text | — | 5–50 KB text | 100 KB (file buffer + parser state) |
| AST | Token stream | 200–500 nodes (~40–100 KB) | 200 KB |
| Ori IR (initial) | AST | 600–1,200 instructions (~100–250 KB) | 500 KB |
| Ori IR (post-OCG) | 1,200 instr | 1,500–2,500 instr (~300–600 KB) | 2–8 MB (peak during regalloc) |
| SASS binary | Scheduled IR | 32–128 KB | 1 MB |
| Cubin (ELF) | SASS + metadata | 40–120 KB | 2 MB |

## Timed Phases

The compilation driver `sub_446240` measures six timed phases per compile unit and reports them when `--compiler-stats` is enabled. The format strings are embedded directly in the binary:

| Phase | Format String | Subsystem |
|---|---|---|
| Parse-time | `"Parse-time            : %.3f ms (%.2f%%)\n"` | PTX lexer + Bison parser + semantic validation |
| CompileUnitSetup-time | `"CompileUnitSetup-time : %.3f ms (%.2f%%)\n"` | Target configuration, ABI setup, register constraints |
| DAGgen-time | `"DAGgen-time           : %.3f ms (%.2f%%)\n"` | PTX-to-Ori lowering, CFG construction, initial DAG formation |
| OCG-time | `"OCG-time              : %.3f ms (%.2f%%)\n"` | Optimized Code Generation: the 157 dispatched optimization phases, register allocation, instruction scheduling, SASS encoding |
| ELF-time | `"ELF-time              : %.3f ms (%.2f%%)\n"` | ELF construction, section layout, symbol table, relocations, EIATTR, file write |
| DebugInfo-time | `"DebugInfo-time        : %.3f ms (%.2f%%)\n"` | DWARF `.debug_info`/`.debug_line`/`.debug_frame` generation, LEB128 encoding |

Additional aggregate stats:

```text
CompileTime = %f ms (100%)
PeakMemoryUsage = %.3lf KB
```

The per-unit header prints `"\nCompile-unit with entry %s"` before each kernel's phase breakdown.

## Per-Kernel Parallelism

ptxas supports two compilation modes for multi-kernel PTX modules:

### Single-Threaded Mode (Default)

The compilation driver `sub_446240` iterates over compile units sequentially. For each kernel entry:

1. `sub_43CC70` — per-entry compilation unit processor, skips `__cuda_dummy_entry__`
2. `sub_7FBB70` — per-kernel entry point: calls `sub_C173E0` (DAG/codegen setup; bails if it returns 0), conditionally prints `"\nFunction name: "` + kernel name when `ctx+1428 < 0`, ORs `0x80` into `ctx+1416`, then tail-calls `sub_7FB6C0`
3. `sub_7FB6C0` — pipeline orchestrator: registers 159 phases via `sub_C62720`, dispatches the 157-entry order via `sub_C64F70` (default path), or takes the recipe path `sub_9F63D0` when OCG knob 298 is set
4. Cleanup: destroys 17 analysis data structures (live ranges, register maps, scheduling state)

Each kernel runs through the per-function backend pipeline independently. Cross-kernel state is limited to shared memory layout and the global symbol table.

## Backend Driver Call Chain

The per-function backend is reached from the CLI driver through a chain of C++ virtual dispatches. From the ELF entry to phase dispatch:

```text
start (0x42333C)                          ; ELF entry
  main (0x409460)                         ; setvbuf(stdout/stderr); -> sub_446240
    sub_446240 (0x446240)                 ; THE DRIVER: CLI parse + module compile
      |  argv -> parsed-options struct:
      |     -O (raw 0..3)  -> parsed-opts+0x70 (112)
      |     target/arch    -> parsed-opts+704 / +352 / +360
      |  [PTX parse -> IR build]  (module-level front-end)
      |
      +-- per-function backend  (C++ virtual dispatch; one target class per SM family)
          |   target backend class built by sub_607DB0; its 24-entry per-target
          |   trampoline table refs wrappers sub_608F20 / sub_609B40 .. sub_609F30
          v
          one of 24 wrappers (e.g. sub_608F20)  -> sub_663C30
            sub_663C30 (0x663C30)         ; per-function backend body
              +-- sub_662920 (0x662920)   ; BUILD per-function OCG context
              |     sub_7F7DC0 (0x7F7DC0) ; <<< OCG-CONTEXT CONSTRUCTOR
              |        reads parsed-options, writes the ~2140-byte ctx,
              |        including opt-level ctx+0x838, knob object ctx+0x680,
              |        options object ctx+0x1664
              +-- sub_7FBB70 (0x7FBB70)    ; backend entry (per-function ctx)
                    +-- sub_C173E0          ; DAG / codegen setup; bail if returns 0
                    +-- if (ctx+1428 < 0): emit "Function name: <name>"
                    +-- ctx+1416 |= 0x80
                    +-- sub_7FB6C0          ; <<< THE OPTIMIZATION DRIVER
```

The OCG context built by `sub_7F7DC0` is the ~2140-byte per-function struct that carries the resolved optimization level (`ctx+0x838`), the OCG knob/config object (`ctx+0x680`), and the options view (`ctx+0x1664`). Every per-phase opt-level and knob decision reads from this context. The per-function virtual-dispatch site above `sub_663C30` is C++ vtable dispatch (the 24 trampolines and 5 target-class builders appear in the static call graph only as data-xref reachable), with the actual `call *vtable[slot]` living in the module-compile loop reached from `sub_446240`.

### Thread Pool Mode (`--split-compile`)

When `--allow-expensive-optimizations` or `--split-compile` is active, ptxas uses a pthread-based thread pool for per-kernel parallelism:

- **Pool constructor** (`sub_1CB18B0`): allocates a 184-byte pool struct (`0xB8`), spawns N detached worker threads via `pthread_create`, initializes mutex at +24, two condition variables at +64 and +112
- **Task submit** (`sub_1CB1A50`): allocates a 24-byte task node `{func_ptr, arg, next}`, enqueues via linked list, broadcasts on `cond_work`
- **Jobserver integration** (`sub_1CC7300`): reads `MAKEFLAGS` environment variable, parses `--jobserver-auth=` for either `fifo:` named pipes or pipe-based file descriptors, throttles thread count to respect GNU Make's `-j` slot limit

The thread pool is used throughout the OCG and ELF phases (stages 5-9 in the diagram). Each worker thread receives its own thread-local context (`sub_4280C0`, 280-byte TLS struct with per-thread error flags, diagnostic suppression state, Werror flag, and synchronization primitives).

### Thread-Local Context Layout

```c
struct ThreadLocalContext {  // 280 bytes (0x118), per-thread via pthread_getspecific
    uint64_t error_flags;          // +0:   error/warning state flags
    uint64_t has_error;            // +8:   nonzero if error occurred
    // +16..+48: internal fields (jmp_buf pointer, pool pointer, counters)
    uint8_t  diag_suppress;        // +49:  diagnostic suppression flag
    uint8_t  werror_flag;          // +50:  --Werror promotion flag
    // +51..+127: reserved / internal state
    pthread_cond_t  cond;          // +128: condition variable (48 bytes)
    pthread_mutex_t mutex;         // +176: per-thread mutex (40 bytes)
    sem_t           sem;           // +216: semaphore (32 bytes)
    // +248..+279: linked-list pointers (global thread list at +256/+264)
};
```

Accessed by `sub_4280C0` (3,928 callers — the single most-called function in the binary).

- **First call in a new thread** allocates and initializes via `malloc(0x118)` + `memset` + `pthread_cond_init` + `pthread_mutex_init` + `sem_init`.
- **280-byte size confirmed** in the decompiled code: `v5 = malloc(0x118u)`, followed by `memset(v5, 0, 0x118u)`, `pthread_cond_init(v5 + 128)`, `pthread_mutex_init(v5 + 176)`, `sem_init(v5 + 216)`.
- **Global list insertion.** After initialization, the struct is inserted into a global doubly-linked list (offsets +256 and +264 hold prev/next pointers, protected by a global mutex).
- **TLS store.** The `pthread_setspecific(key, v5)` call stores the pointer for subsequent `pthread_getspecific` retrieval.

## Key Function Call Chain

The top-level control flow from program entry to ELF output:

```text
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
  |         |    sub_C60D20 -- get default order table (0x22BEEA0, count 157)
  |         |    sub_C64F70 -- dispatch the 157 ordered phases
  |         |  cleanup: destroy 17 analysis data structures
  |         v
  |       [157 dispatched phases: optimization -> regalloc -> scheduling -> encoding]
  |
  |-- sub_612DE0 (0x612DE0, 47KB) -- Kernel finalizer / ELF builder
  |     |  "Finalizer fastpath optimization"
  |     |  version: "Cuda compilation tools, release 13.0, V13.0.88"
  |     |  build:   "Build cuda_13.0.r13.0/compiler.36424714_0"
  |     |
  |     sub_1CB53A0 (13KB decomp) - ELF world initializer (672-byte object)
  |     |  "elfw memory space", .shstrtab, .strtab, .symtab
  |     |
  |     sub_1CB3570 (10KB decomp) - Add .text.FUNCNAME sections (44 callers)
  |     sub_1CB68D0 (49KB decomp) - Symbol table builder
  |     sub_1CABD60 (66KB decomp) - Section layout & memory allocation
  |     sub_1CD48C0 (20KB decomp) - Relocation resolver
  |     sub_1C9B110 (23KB decomp) - Mercury capsule builder (capmerc)
  |     sub_1C9F280 (97KB decomp) - Master ELF emitter (largest in range)
  |     sub_1CD13A0 (11KB decomp) - Final file writer
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

- `"PTX parsing state"` — created by `sub_451730`, holds the lexer/parser symbol tables and AST nodes
- `"elfw memory space"` — created by `sub_1CB53A0`, holds the ELF world object (672 bytes) and section data

### Pool Allocator Internals

The allocator at `sub_424070` implements a dual-path design:

- **Small allocations** (up to 4,999 bytes / `0x1387`): 8-byte-aligned, size-class binned free lists at pool struct offset +2128. Pop from free list head on alloc, push back on free.
- **Large allocations** (above 4,999 bytes): boundary-tag allocator with coalescing of adjacent free blocks.
- **Thread safety**: `pthread_mutex_lock`/`unlock` around all pool operations, mutex at pool struct offset +7128.
- **OOM handling**: calls `sub_42BDB0` (3,825 callers) which triggers `longjmp`-based fatal abort via `sub_42F590`.

## Pipeline Stage Breakdown

> **Terminology note.** The 6 stages below (Parse, CompileUnitSetup, DAGgen, OCG, ELF, DebugInfo) correspond to the 6 **timed phases** measured by `--compiler-stats`. They cover the entire program lifecycle. The OCG stage (Stage 4 here) is itself subdivided into 10 internal stages in the [Pass Inventory](../passes/index.md), numbered OCG-Stage 1–10. To avoid confusion, cross-references use "timed phase" for the 6 whole-program stages and "OCG stage" for the 10 optimizer sub-stages.

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

The finalizer `sub_612DE0` (47 KB decomp) assembles the NVIDIA ELF/cubin from the compiled SASS. Section layout (`sub_1CABD60`, 11,856 B native / 66 KB decomp) assigns addresses to shared memory, constant banks (with OCG deduplication), local memory, and reserved shared memory (`.nv.reservedSmem.begin/cap/offset0`). The master ELF emitter `sub_1C9F280` (15,263 B native / 97 KB decomp) constructs headers, section tables, and program headers. Three binary output modes exist:

1. **mercury** — traditional SASS binary format
2. **capmerc** — Capsule Mercury (default on sm_100+), embeds PTX source in `.nv.merc.*` sections
3. **sass** — direct SASS output

See [ELF/Cubin Output](output.md).

### Stage 6: DebugInfo (DebugInfo-time)

DWARF debug information generation: `.debug_info`, `.debug_line`, `.debug_frame` sections. The LEB128 encoder at `sub_45A870` handles all variable-length integer encoding. Source location tracking uses the location map (hash map at `sub_426150`/`sub_426D60`) with file offset caching every 10 lines for fast random access. Labels follow the pattern `.L__$locationLabel$__%d`. See [Debug Information](../output/debug-info.md).

## Error Paths and Recovery

ptxas uses `setjmp`/`longjmp` as its sole error recovery mechanism — there are no C++ exceptions (the binary is compiled as C). Three nested recovery points exist, each catching progressively more localized failures.

### Recovery Point Hierarchy

```text
sub_446240 (top-level driver)
  setjmp(jmp_buf_global)         // Level 1: catches any fatal anywhere
    |
    sub_43A400 (per-kernel worker)
      setjmp(jmp_buf_kernel)     // Level 2: catches per-kernel fatals
        |
        sub_432500 (finalization bridge)
          setjmp(jmp_buf_local)  // Level 3: catches OCG pipeline fatals
            |
            [157 dispatched phases, regalloc, encoding, ELF]
```

**Level 1 (global).** Established by `sub_446240` at function entry. If any code path anywhere in ptxas calls `sub_42FBA0` with severity >= 6 (fatal), execution longjmps back here. The handler cleans up global resources and returns a non-zero exit code. This is the last-resort handler.

**Level 2 (per-kernel).** Established by `sub_43A400` before the OCG pipeline runs. On longjmp, the handler destroys the partially-compiled kernel's state, clears the error flags in the TLS context, and continues to the next kernel. This allows multi-kernel compilations to survive a single kernel's failure.

**Level 3 (finalization).** Established by `sub_432500`, which saves and replaces the TLS `jmp_buf` pointer for nested recovery. On longjmp: restores the previous `jmp_buf`, sets `error_flags = 1`, releases output buffers, and calls `report_internal_error()`. Execution returns `false` to the Level 2 handler.

### Parse Error Recovery

Parse errors in `sub_451730` (the Flex/Bison parser) invoke `sub_42FBA0` with the message `"syntax error"`:

- **Severity 4–5 (non-fatal error):** The error is printed with file:line location, and the parser attempts to continue via Bison's error recovery rules. Multiple non-fatal parse errors can accumulate. After parsing completes, if the error count > 0, the compilation is aborted before entering the OCG pipeline.
- **Severity 6 (fatal):** Triggers `longjmp` to the Level 1 handler immediately. The parser state pool is leaked (accepted trade-off since the process is about to exit).

Bison error recovery operates through the `error` token in the grammar. When the parser encounters a token that matches no production, it discards tokens until it finds one that allows the `error` production to reduce, then resumes parsing. This provides reasonable error recovery for common mistakes (missing semicolons, misspelled opcodes) but can cascade badly for structural errors (mismatched braces, corrupt PTX).

### Phase Failure in PhaseManager

The phase executor `sub_C64F70` runs each phase by calling its vtable `execute()` method. There is no explicit per-phase error check — phases that detect internal errors call the diagnostic emitter `sub_42FBA0` directly. The error handling cascade:

1. **Non-fatal phase error (severity 3–5):** The error is printed and the error flag is set in the TLS context. The PhaseManager continues executing subsequent phases. This allows multiple diagnostics to be collected in a single run.
2. **Fatal phase error (severity 6):** Triggers `longjmp` to Level 2 or Level 3. The current kernel's compilation is aborted. The PhaseManager's loop is unwound non-locally — no cleanup of intermediate phase state occurs. Resources are reclaimed when the per-kernel memory pool is destroyed.
3. **OOM during phase execution:** Any allocation failure calls `sub_42BDB0` (3,825 callers), which forwards to `sub_42F590` with a severity-6 descriptor at `unk_29FA530`. This always triggers `longjmp`.

The PhaseManager logs phase transitions using `"Before <phase>"` and `"After <phase>"` string construction (visible in `sub_C64F70`). When `DUMPIR` is set to a phase name, the IR is dumped to a file after that phase completes. This enables bisection of phase failures: `--knob DUMPIR=<phase_name>` isolates which phase corrupted the IR.

### Register Allocation Failure and Retry

The register allocator has its own retry mechanism that operates *within* the normal pipeline (not via longjmp). The retry driver `sub_971A90` (355 lines) wraps the Fatpoint allocator in a two-phase strategy:

**Phase 1 — NOSPILL.** Attempt allocation without spilling. If the allocator fits within the register budget, proceed directly to finalization.

**Phase 2 — SPILL retry loop.** If NOSPILL fails:
1. The spill guidance engine `sub_96D940` (84 KB) computes per-register-class spill candidates
2. The allocator retries with progressively more aggressive spilling, up to N attempts (controlled by knobs 638/639)
3. Each attempt prints: `"-CLASS NOSPILL REGALLOC: attemp %d, used %d, target %d"` (note: the typo "attemp" is in the binary)
4. The best result across all attempts is tracked by `sub_93D070`
5. The finalization function `sub_9714E0` (290 lines) commits the best result or emits a fatal error

**On allocation failure** (all retry attempts exhausted):

```text
Register allocation failed with register count of '%d'.
Compile the program with a higher register target
```

This error is emitted by `sub_9714E0` through two paths: with source location (via `sub_895530`, including function name and PTX line number) or without source location (via `sub_7EEFA0`, generic). After this error, `sub_9714E0` returns with `HIBYTE(status)` set, causing the retry driver to clear all register assignments to `-1` and propagate the failure.

A dedicated DUMPIR hook exists: `"Please use -knob DUMPIR=AllocateRegisters for debugging"` — this string (found at `sub_9714E0`'s error path) directs users to dump the IR state before the allocator runs.

### Fatal Error Handler Chain

The complete chain from any error site to process termination:

```text
[any function, 2,350 call sites]
  sub_42FBA0(descriptor, location, ...)   // central diagnostic emitter
    |  checks descriptor[0] for severity
    |  severity 0: silently ignored
    |  severity 1-2: prints "info    " message
    |  severity 3: prints "warning " (or "error   " if TLS[50] Werror flag set)
    |  severity 4-5: prints "error   " / "error*  " (non-fatal)
    |  severity 6: prints "fatal   " then:
    v
  longjmp(tls->jmp_buf, 1)
    |  unwinds to nearest setjmp recovery point
    v
  [Level 3] sub_432500 -> restore jmp_buf, set error_flags, return false
  [Level 2] sub_43A400 -> cleanup kernel state, continue to next kernel
  [Level 1] sub_446240 -> cleanup global state, exit(non-zero)
```

**Resource leak note.** Because `longjmp` bypasses normal stack unwinding, all heap allocations made between the `setjmp` and the fatal error are leaked unless tracked in a pool. This is why ptxas uses pool allocators — the per-kernel pool can be destroyed wholesale at the Level 2 recovery point, reclaiming all leaked memory without tracking individual allocations.

## Architecture Dispatch

An architecture vtable factory at `sub_1CCEEE0` (17KB, 244 callees) constructs a 632-byte vtable object (79 function pointers) based on the target SM version. The version dispatch ranges:

| Range | Architecture | Generation | Status in v13.0.88 |
|---|---|---|---|
| sm_30-39 | Kepler | 1st gen | **Validation only** — accepted by `bsearch` in `unk_1D16220`, but no codegen factory, no capability dispatch handlers, and no SASS encoders ship for these targets. Compilation fails after parsing. |
| sm_50-59 | Maxwell | 2nd gen | **Validation only** — same as Kepler. Present in the base validation table for backward-compatible PTX version/target checking, but no backend support. |
| sm_60-69 | Pascal | 3rd gen | **Validation only** — same as above. The codegen factory value 24576 (`6 << 12`) is referenced in comparison thresholds but no Pascal-specific encoder tables exist. |
| sm_70-73 | Volta | 4th gen | **Validation only** — sm_70, sm_72, sm_73 are in the base table but have no active capability dispatch handlers in `sub_607DB0`. |
| sm_75 | Turing | 4th gen | **Active** — lowest SM with full codegen support (factory 24577). |
| sm_80-89 | Ampere / Ada | 5th gen | **Active** — factory 28673. |
| sm_90 | Hopper | 6th gen | **Active** — factory 32768. |
| sm_100-110 | Blackwell | 7th gen | **Active** — factory 36864. |
| sm_120-121 | Consumer / DGX Spark | 7th gen (desktop) | **Active** — factory 36864 (shared with Blackwell datacenter). |

The distinction between "validation only" and "active" is critical:

- **Validation table** `unk_1D16220` contains 32 entries including all legacy SMs back to sm_20, allowing ptxas to parse PTX files that declare `.target sm_30` without immediately rejecting them.
- **Active handlers.** The capability dispatch initializer `sub_607DB0` only registers handler functions for sm_75 through sm_121 (13 base targets).
- **Fatal on unregistered SM.** Attempting to compile code for an unregistered SM produces a fatal error during codegen factory lookup — the architecture vtable factory `sub_1CCEEE0` cannot construct a backend object for these targets.

The legacy codegen factory values (12288 for sm_30, 16385/20481 for sm_50, 24576 for sm_60) survive as comparison constants in feature-gating checks throughout the backend (e.g., `if (factory_value > 28673)` gates sm_90+ features), but the code paths they would activate no longer exist.

Each vtable entry is a function pointer to an SM-specific implementation of a codegen or emission primitive. This is the central dispatch mechanism for all architecture-dependent behavior in the backend.

## Obfuscation: ROT13 Encoding

All internal identifiers in ptxas's static initializers are ROT13-encoded:

- **Opcode table** (`ctor_003` at `0x4095D0`, 17KB): 900+ PTX opcode mnemonics. Example: `NPDOHYX` decodes to `ACQBULK`, `SZN` decodes to `FMA`, `RKVG` decodes to `EXIT`.
- **General knob table** (`ctor_005` at `0x40D860`, 80KB): 2,000+ Mercury/OCG tuning knob names with hex default values. Example: `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` decodes to `MercuryUseActiveThreadCollectiveInsts`.
- **Scheduler knob table** (`ctor_007` at `0x421290`, 8KB): 98 scheduler-specific knob names. Example: `XBlockWaitOut`, `ScavInlineExpansion`.

The ROT13 decoding is performed inline during lookup (in `sub_79B240`, `GetKnobIndex`) using character-range detection: bytes in `A-M` get +13, bytes in `N-Z` get -13, with case-insensitive comparison via `tolower()`.

## Cross-References

- [Binary Layout](../binary-layout.md) — address ranges for every subsystem
- [Function Map](../function-map.md) — master index of recovered function addresses
- [CLI Options](../config/cli-options.md) — complete flag catalog
- [Knobs System](../config/knobs.md) — 1,294 internal tuning parameters
- [Optimization Levels](../config/opt-levels.md) — what changes at `-O0`/`-O1`/`-O2`/`-O3`
- [Phase Manager](../passes/phase-manager.md) — PhaseManager object layout and dispatch
- [Memory Pool Allocator](../infra/memory-pools.md) — pool struct layout and allocation algorithm
- [Thread Pool & Concurrency](../infra/threading.md) — thread pool struct, task submit, jobserver

## Function Map

| Address | Size | Callers | Identity | Confidence |
|---|---|---|---|---|
| `0x409460` | 84 B | — | `main` (entry point) | CERTAIN |
| `0x446240` | 11 KB | 1 | Top-level compilation driver | HIGH |
| `0x434320` | 10 KB | 1 | CLI option parser + validator | HIGH |
| `0x445EB0` | — | 1 | Target configuration setup | HIGH |
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
| `0xC64F70` | — | 1 | Phase executor | HIGH |
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
