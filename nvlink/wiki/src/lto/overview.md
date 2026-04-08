# LTO Overview

Link-Time Optimization (LTO) in nvlink v13.0.88 compiles NVVM IR into SASS machine code at link time instead of at translation-unit compile time. The design follows a delegation model: nvlink orchestrates the pipeline, `libnvvm.so` compiles IR to PTX, and an embedded ptxas backend assembles PTX into SASS. nvlink itself contains zero LLVM infrastructure -- no "LLVM" strings appear anywhere in the 26.2 MB binary. All IR-level optimization is offloaded to libnvvm via its public C API.

## Architecture: The Delegation Model

```
  nvlink (orchestrator)
    |
    |  1. Collect NVVM IR from inputs
    |  2. dlopen("libnvvm.so")            <-- external shared library
    |  3. nvvmCreateProgram()
    |  4. nvvmAddModuleToProgram()         <-- for each IR module
    |  5. nvvmCompileProgram(opts)         <-- IR -> PTX
    |  6. nvvmGetCompiledResult()          <-- PTX text out
    |  7. nvvmDestroyProgram()
    |
    |  8. Feed PTX to embedded ptxas       <-- ~25 MB of compiler backend
    |     (ISel, regalloc, scheduling,      inside the nvlink binary
    |      encoding, ELF emission)
    |
    v
  Final SASS cubin (device ELF)
```

Three distinct software components participate, but only one binary is involved at runtime:

| Component | Location | Role |
|---|---|---|
| nvlink | The tool binary itself (~1.2 MB of linker code) | Orchestrates pipeline, manages inputs, performs ELF merging, relocation, finalization |
| libnvvm.so | External shared library loaded via `dlopen` | Compiles NVVM IR (LLVM bitcode) into PTX text. Contains the LLVM-based optimizer |
| Embedded ptxas | ~25 MB of compiler backend statically linked into the nvlink binary | Assembles PTX into SASS: parsing, ISel, register allocation, scheduling, encoding, ELF emission |

The key insight is that nvlink does not embed LLVM. It delegates IR optimization to libnvvm (which does contain LLVM), then uses its own embedded ptxas copy for the PTX-to-SASS compilation step. This is the same ptxas backend shared by the standalone `ptxas` tool and `cicc`.

## LTO Pipeline in Detail

The LTO pipeline occupies Phase 8 of the main nvlink execution flow. It runs after all input files have been read and dispatched (Phase 7) and before the merge phase (Phase 9).

### Step 1: IR Collection

During the input file loop, NVVM IR modules are detected and registered rather than immediately compiled:

- Files with `.nvvm` or `.ltoir` extensions require `-lto` to be set (`byte_2A5F288`). If `-lto` is absent, nvlink emits a fatal error: `"should only see nvvm files when -lto"`.
- Fatbin members of type 8 (NVVM IR) are extracted and registered via `lto_add_module` at `0x4BD1F0`.
- IR is identified by scanning for embedded option strings: `"-inline-info"`, `"-ftz="`, `"-prec_div="`.
- The diagnostic trace prints: `"nvlink -lto-add-module %s.nvvm"` for each registered module.
- `libcudadevrt` receives special handling: when LTO covers all modules, libcudadevrt is stripped from the input list entirely (`"LTO on everything so remove libcudadevrt from list"`).

After the input loop completes, `lto_collect_ir_modules` at `0x426CD0` (7,040 bytes) gathers all registered IR modules into a compilation batch. If `-use-host-info` or `-kernels-used` is active, `lto_mark_used_symbols` at `0x426AE0` marks reachable symbols for dead-code elimination across LTO boundaries.

### Step 2: libnvvm Loading and Compilation

`nvvm_api_wrapper_init` at `0x4BC4A0` (2,548 bytes) loads `libnvvm.so` via `dlopen` and resolves function pointers via `dlsym`:

```
nvvmCreateProgram       nvvmAddModuleToProgram
nvvmCompileProgram      nvvmGetCompiledResultSize
nvvmGetCompiledResult   nvvmGetErrorString
nvvmGetProgramLogSize   nvvmGetProgramLog
nvvmDestroyProgram
```

The path to `libnvvm.so` comes from `--nvvmpath` (`qword_2A5F278`). This flag is required when `-lto` is active; validation in `nvlink_parse_options` enforces this.

`nvvm_compile_and_extract` at `0x4BC6F0` (13,602 bytes) drives the actual compilation:

1. Creates an NVVM program handle
2. Adds all collected IR modules to the program
3. Builds the cicc option string from nvlink's forwarding flags (`--Xnvvm`, `--maxrregcount`, `--ftz`, `--prec-div`, `--prec-sqrt`, `--fmad`)
4. Calls `nvvmCompileProgram` with the assembled options
5. Retrieves the compiled PTX text via `nvvmGetCompiledResult`
6. On error, retrieves and prints the compilation log via `nvvmGetProgramLog`
7. Destroys the program handle

The callback mechanism uses magic cookie values: `0xBEEF` for handle retrieval, `0xF00D` for callback registration. The nvvm handle is resolved via `dlsym("__nvvmHandle")` from the loaded library.

The `--force-device-c` flag is passed to force relocatable device-code compilation when partial LTO is in effect.

### Step 3: PTX Assembly

The PTX output from libnvvm is fed to the embedded ptxas backend. Depending on the compilation path:

- **Whole-program**: Single PTX module compiled via `sub_4BD4E0` into a monolithic cubin.
- **Relocatable**: Per-module PTX compiled via `sub_4BD760` with relocatable output.
- **Split-compile**: Thread pool (`sub_43FDB0`) dispatches parallel ptxas invocations via `sub_4264B0`.

The embedded ptxas backend (~25 MB of code starting at `0x530000`) performs:
- PTX parsing and validation
- Instruction selection (ISel) with per-SM-variant pattern clones
- Register allocation
- Instruction scheduling
- Binary encoding
- ELF section emission

### Step 4: Result Integration

Compiled cubins from the ptxas step are merged into the linker's output ELF via the normal merge pipeline (`merge_elf` at `0x45E7D0`). For Mercury targets (SM >= 100), each compiled cubin also passes through the FNLZR (Finalizer) post-link transform at `0x4275C0`.

## Pipeline Diagram

```
  Input files (.nvvm, .ltoir, fatbin with IR)
    |
    v
  [Phase 7: Input Loop]
    |  Register IR modules (lto_add_module, 0x4BD1F0)
    |  Detect IR via "-inline-info", "-ftz=" markers
    v
  [Phase 8: LTO Pipeline]
    |
    |  lto_collect_ir_modules (0x426CD0)
    |  lto_mark_used_symbols  (0x426AE0)  [if -use-host-info]
    |
    v
  dlopen libnvvm.so (0x4BC4A0)
    |  resolve: nvvmCreateProgram, nvvmAddModule, nvvmCompile, ...
    |  path from --nvvmpath (qword_2A5F278)
    v
  nvvmCompileProgram (0x4BC6F0)
    |  options: --arch, --ftz, --prec-div, --prec-sqrt, --fmad,
    |           --maxrregcount, --Xnvvm passthrough
    |  output: PTX text
    v
  +------+------+------+
  | Path A       Path B       Path C              |
  | Whole-prog   Relocatable  Split-compile        |
  | 0x4BD4E0     0x4BD760     thread pool 0x43FDB0 |
  |              (per-module)  parallel 0x4264B0    |
  +------+------+------+
    |         |         |
    v         v         v
  Embedded ptxas (PTX -> SASS)
    |  ISel, regalloc, scheduling, encoding
    |  ISel cloned 4-5x for sm_5x/7x/8x/9x/10x
    v
  SASS cubin(s)
    |
    |  [SM >= 100: FNLZR post-link transform (0x4275C0)]
    v
  [Phase 9: Merge into output ELF]
```

## When LTO Activates

LTO activation depends on both explicit flags and implicit architecture thresholds:

| Condition | Effect |
|---|---|
| `--lto` / `-lto` passed | Sets `byte_2A5F288`. Enables IR input acceptance and LTO compilation pipeline |
| `--dlto` passed | Sets `byte_2A5F287`. Distributed LTO mode (IR modules compiled on remote workers) |
| SM > 89 | Sets `byte_2A5F225` (SASS mode). Compilation mode (`dword_2A5B528`) becomes 6. Targets from sm_90 onward require SASS output, which means the embedded compiler backend always runs |
| SM > 99 | Sets `byte_2A5F222` (Mercury mode). Adds FNLZR post-link step to the pipeline |
| No IR inputs present | LTO pipeline skipped even if `-lto` is set. The flag only enables IR acceptance |

The compilation mode global `dword_2A5B528` encodes the active mode:

| Value | Mode | Description |
|---|---|---|
| 0 | Normal | Standard linking, no embedded compilation |
| 2 | Passthrough | Archive pass-through mode |
| 4 | LTO | Link-time optimization via libnvvm + embedded ptxas |
| 6 | SASS | Direct SASS output (SM > 89). Implies embedded ptxas is active |

For architectures SM 90 and above (Hopper, Blackwell, and beyond), the SASS output mode is mandatory. This means the embedded compiler backend is always involved for these targets, regardless of whether `-lto` is explicitly passed. The `-lto` flag controls whether IR-level whole-program optimization through libnvvm occurs.

## Whole-Program vs Partial vs Split-Compile

nvlink supports three distinct compilation paths after IR compilation. The choice is controlled by input composition and explicit flags:

### Whole-Program Compile

Active when all input modules have LTO IR and no relocatable output is needed:
- Diagnostic trace: `"whole program compile"`
- Single PTX compilation via `sub_4BD4E0`
- Produces one monolithic cubin with all functions
- Enables cross-module inlining and dead-code elimination
- Controlled by `--force-whole-lto` (`byte_2A5F284`)

### Partial LTO (Relocatable)

Active when some inputs have LTO IR and others have pre-compiled cubins:
- Diagnostic trace: `"relocatable compile"`
- Warning: `"requested LTO but '%s' not built for LTO so doing partial LTO"`
- Sets `byte_2A5F286` (partial LTO flag)
- Passes `--force-device-c` to libnvvm for relocatable output
- Compiled modules merged with pre-compiled cubins in Phase 9
- Controlled by `--force-partial-lto` (`byte_2A5F285`)

### Split Compilation

Active when `--split-compile` or `--split-compile-extended` is specified:
- Thread pool created via `sub_43FDB0`
- Thread count from `dword_2A5B514` (extended) or `dword_2A5B518` (nvvm)
- Each module compiled independently via `sub_4264B0` dispatched to pool workers
- Error reporting: `"Cannot allocate pthread data"`, `"Unable to create thread pool"`, `"error in LTO callback"`
- Enables parallel compilation of independent translation units
- Reduces wall-clock time at the cost of losing some cross-module optimization

## LTO-Specific CLI Options

| Option | Short | Type | Global | Description |
|---|---|---|---|---|
| `--link-time-opt` | `-lto` | bool | `byte_2A5F288` | Enable LTO. Required for IR inputs |
| `--dlto` | — | bool | `byte_2A5F287` | Distributed LTO mode |
| `--force-partial-lto` | — | bool | `byte_2A5F285` | Force partial LTO even when whole-program is possible |
| `--force-whole-lto` | — | bool | `byte_2A5F284` | Force whole-program LTO |
| `--nvvmpath` | — | string | `qword_2A5F278` | Path to `libnvvm.so`. Required with `-lto` |
| `--emit-ptx` | — | bool | `byte_2A5F29A` | Emit intermediate PTX instead of SASS |
| `--split-compile` | — | int | `dword_2A5F260` | Split compilation mode |
| `--split-compile-extended` | — | int | `dword_2A5B514` | Extended split-compile thread count |
| `--Xnvvm` | — | string (multi) | `qword_2A5F230` | Pass-through options to libnvvm/cicc |
| `--Xptxas` | — | string (multi) | `qword_2A5F238` | Pass-through options to embedded ptxas |
| `--maxrregcount` | — | int | `dword_2A5F22C` | Maximum register count per thread |
| `--Ofast-compile` | `-Ofc` | string | `qword_2A5F258` | Compilation speed vs quality tradeoff. Values: `"0"`, `"min"`, `"mid"`, `"max"` |

## Key Functions

| Address | Size | Name | Role |
|---|---|---|---|
| `0x426CD0` | 7,040 B | `lto_collect_ir_modules` | Gathers registered IR modules into compilation batch |
| `0x426AE0` | 2,178 B | `lto_mark_used_symbols` | Marks reachable symbols for cross-module DCE |
| `0x4BC4A0` | 2,548 B | `nvvm_api_wrapper_init` | Loads `libnvvm.so`, resolves API function pointers |
| `0x4BC6F0` | 13,602 B | `nvvm_compile_and_extract` | Drives libnvvm: create program, add modules, compile, extract PTX |
| `0x4BD1F0` | ~800 B | `lto_add_module` | Registers a single IR module during input processing |
| `0x4BD4E0` | varies | `ptxas_compile_whole` | Whole-program PTX-to-SASS compilation |
| `0x4BD760` | varies | `ptxas_compile_relocatable` | Per-module relocatable PTX-to-SASS compilation |
| `0x43FDB0` | varies | `thread_pool_create` | Creates pthread-based thread pool for split compilation |
| `0x4264B0` | varies | `split_compile_worker` | Per-module compilation dispatched to thread pool |
| `0x1406B40` | 6,725 B | `lto_create_compilation_context` | Allocates 272-byte context: SM version, debug flags, optimization level |
| `0x1407FC0` | 26,791 B | `lto_compile_function` | Per-function compilation driver (ISel, regalloc, emission) |
| `0x14091C0` | 23,593 B | `lto_link_and_emit` | Links compiled functions, emits final ELF sections |
| `0x140A1C0` | 5,270 B | `lto_finalize_output` | Finalizes LTO compilation output |
| `0x140A6B0` | 5,462 B | `lto_report_resource_usage` | Prints register/memory/barrier statistics per kernel |

## Key Globals

| Address | Size | Name | Role |
|---|---|---|---|
| `byte_2A5F288` | 1 | `lto_enabled` | Master LTO enable flag |
| `byte_2A5F287` | 1 | `dlto_enabled` | Distributed LTO flag |
| `byte_2A5F286` | 1 | `relocatable_compile` | Partial/relocatable LTO output flag |
| `byte_2A5F285` | 1 | `force_partial_lto` | Force partial LTO |
| `byte_2A5F284` | 1 | `force_whole_lto` | Force whole-program LTO |
| `byte_2A5F225` | 1 | `is_sass_mode` | SM > 89 flag. SASS output required |
| `byte_2A5F222` | 1 | `is_mercury_mode` | SM > 99 flag. Mercury post-link enabled |
| `dword_2A5B528` | 4 | `compilation_mode` | 0=normal, 2=passthru, 4=lto, 6=sass |
| `dword_2A5B514` | 4 | `split_compile_ext_threads` | Thread count for extended split compile |
| `dword_2A5B518` | 4 | `split_compile_nvvm_threads` | Thread count for nvvm split compile |
| `qword_2A5F278` | 8 | `nvvmpath` | Path to `libnvvm.so` |
| `qword_2A5F230` | 8 | `xnvvm_options` | Forwarded options for libnvvm |
| `qword_2A5F238` | 8 | `xptxas_options` | Forwarded options for embedded ptxas |
| `qword_2A5F258` | 8 | `ofast_compile_level` | Compilation speed tradeoff (`"0"`/`"min"`/`"mid"`/`"max"`) |

## Timing Trace Points

When timing is enabled (`qword_2A5F290` is non-NULL), the LTO phase records two timing points:

| Phase Name | Description |
|---|---|
| `"cicc-lto"` | Time spent in libnvvm IR compilation (Step 2) |
| `"ptxas-lto"` | Time spent in embedded ptxas assembly (Step 3) |

These appear in the debug trace alongside the standard phase names: `"init"`, `"read"`, `"merge"`, `"layout"`, `"relocate"`, `"finalize"`, `"write"`.

## Resource Usage Reporting

`lto_report_resource_usage` at `0x140A6B0` prints per-kernel statistics after LTO compilation:

```
Used %d registers, %lld bytes smem, %lld bytes lmem
%lld bytes gmem, %lld bytes cmem[0..17]
%d barriers, %d samplers, %d surfaces, %d textures
%d bytes cumulative stack size
Compile time = %.3f ms
```

Constant memory banks are enumerated from `0x70000004` through `0x70000016` (18 banks). This output appears when verbose mode is active and routes through the diagnostic subsystem at `dword_2A5DC90`.

## Embedded Compiler Backend Layout

The embedded ptxas backend within nvlink spans approximately `0x530000` to `0x1D32172` (~24.7 MB). The LTO-specific compilation engine occupies a 1.5 MB region at `0x12B0000`--`0x1430000`, organized as:

| Range | Size | Subsystem |
|---|---|---|
| `0x12B0000`--`0x12BA000` | 40 KB | PTX operand/type system, special registers, symbol table |
| `0x12BA000`--`0x12D0000` | 88 KB | ISel lowering passes (~200 functions) |
| `0x12D0000`--`0x12D5000` | 20 KB | DWARF debug line info generator |
| `0x12D5000`--`0x1400000` | 11 MB | ISel pattern matchers (parametric clones per SM variant) |
| `0x1400000`--`0x1430000` | 192 KB | Top-level LTO pipeline, ELF emission, MMA lowering |

ISel patterns are instantiated 4--5 times for different architecture targets:
- Base (sm_5x): `0x12BA000`--`0x12D0000`
- sm_8x clone: `0x13D6B10`--`0x13DED20`
- sm_9x clone: `0x13EC1E0`--`0x13FE860`
- sm_10x clone: `0x140AFE0`--`0x1418220`

Each clone set contains 50--60 functions implementing identical lowering logic specialized for the target's instruction set.

## Related Pages

- [libnvvm Integration](libnvvm-integration.md) -- API loading, callback mechanism, error handling
- [Whole vs Partial LTO](whole-vs-partial.md) -- Decision logic, flag interactions, partial LTO warnings
- [Split Compilation](split-compilation.md) -- Thread pool, parallel dispatch, synchronization
- [Option Forwarding to cicc](option-forwarding.md) -- How `--Xnvvm` and `--Xptxas` options are assembled and passed
- [LTO IR Format Versions](ir-format-versions.md) -- NVVM IR bitcode detection and version constraints
- [Pipeline Overview](../pipeline/overview.md) -- Full 14-phase pipeline context
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- SM-variant vtable selection for ISel clones
- [Merge Phase](../pipeline/merge.md) -- post-LTO merge that integrates compiled cubins into the output ELF
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- linker-level DCE suppressed during full LTO, active during partial LTO
- [Symbol Resolution](../linker/symbol-resolution.md) -- symbol handling for LTO-compiled modules merged into the output

### Sibling Wiki

- **cicc wiki**: [LTO & Module Optimization](../../../../cicc/wiki/src/lto/index.md) -- compiler-side LTO pipeline (five-pass IR optimization, inliner cost model, cross-module import). nvlink delegates IR compilation to cicc via libnvvm; this page documents what cicc does with the IR
