# Entry Point & CLI

The cicc binary has a surprisingly complex entry point. Rather than a straightforward `main → compile → exit` flow, it implements a **dual-path architecture** where the same binary can operate as either a LibNVVM-based compiler (Path A) or a standalone compiler (Path B), selected at runtime through environment variables and obfuscated string comparisons. This design allows NVIDIA to ship a single binary that serves both the `nvcc` toolchain and the LibNVVM API.

The entry point region (`0x8F0000`–`0x96FFFF`, ~520 KB) handles CLI parsing, architecture detection with a 3-column flag fan-out system, and dispatch into one of several compilation pipelines. A hidden "wizard mode" gated behind an environment variable with a magic number enables developer diagnostics that are otherwise completely inaccessible.

| | |
|---|---|
| **main() thunk** | `0x4396A0` (16 bytes) — `return sub_8F9C90(argc, argv, envp)` |
| **Real main** | `sub_8F9C90` (10,066 bytes, 1,990 lines) |
| **Wizard mode** | `getenv("NVVMCCWIZ") == 553282` → `byte_4F6D280 = 1` |
| **Default arch** | `compute_75` / `sm_75` (Turing) |
| **Flag catalog** | `sub_9624D0` (75KB, 2,626 lines, 4 output vectors) |
| **Architecture map** | `sub_95EB40` (38KB, 23 architectures, 3-column fan-out) |
| **Flag translation** | `sub_8FE280` (red-black tree at `qword_4F6D2A0`, 40+ nvcc→cicc mappings) |
| **Pipeline stages** | LNK → OPT → \[OPTIXIR\] → LLC |
| **Dual path** | Path A (`sub_905EE0`) / Path B (`sub_1265970`) |
| **Libdevice** | Path A: `unk_3EA0080` / Path B: `unk_420FD80` (455,876 bytes each) |
| **Arch bitmask** | `0x60081200F821` (validates SM 75–121) |

## Architecture

```
main (0x4396A0, 16B thunk)
  │
  └─ sub_8F9C90 (10KB, REAL MAIN)
       │
       ├─ getenv("NVVMCCWIZ") == 553282 → wizard mode
       ├─ sub_16C5290: extract program name from argv[0]
       │
       ├─ ARGUMENT LOOP (v15 = 1..argc)
       │    ├─ -o <file>              → v257 (output)
       │    ├─ -nvvmir-library <path> → v256 (libdevice)
       │    ├─ -lgenfe/-libnvvm/-lnk/-opt/-llc → v263 (mode)
       │    ├─ -arch/-mcpu/--nv_arch  → v242 (SM number)
       │    ├─ --emit-optix-ir        → v243=1, v258=1
       │    ├─ -nvc                   → v258=1
       │    ├─ -irversion             → print IR version, exit
       │    ├─ .bc/.ci/.i/.ii/.cup/.optixir → s (input file)
       │    └─ obfuscated option      → v253 (0 or 1)
       │
       ├─ v253 RESOLUTION (if still == 2)
       │    └─ getenv(obfuscated) → compare → set v253 = 0 or 1
       │
       ├─ DISPATCH (v263 × v253)
       │    ├─ v263==0, v253==1 → sub_902D10  (simple Path A)
       │    ├─ v263==0, v253==0 → sub_1262860 (simple Path B)
       │    ├─ v263==1          → sub_905E50 / sub_12658E0 (lgenfe)
       │    ├─ v263≥2, v253==1  → sub_905EE0  (multi-stage Path A)
       │    └─ v263≥2, v253==0  → sub_1265970 (multi-stage Path B)
       │
       └─ CLEANUP: free all vectors, strings, argv copy
```

## Real Main — `sub_8F9C90`

The exported `main()` at `0x4396A0` is a 16-byte thunk that immediately tail-calls `sub_8F9C90` — the actual entry point. This function is a monolithic CLI parser and dispatcher: it copies argv into a local buffer, checks for wizard mode, iterates over all arguments accumulating state in ~12 local variables, resolves the compilation path, and finally dispatches to the appropriate pipeline function. The entire function is a single 10KB basic-block-heavy control flow graph with ~80 branch targets.

| Field | Value |
|---|---|
| Address | `0x8F9C90`–`0x8FC3E2` |
| Size | 10,066 bytes |
| Stack frame | 0x978 bytes (2,424 bytes) |
| Local buffers | `v284[2096]` for argv copy (stack if argc ≤ 256, else heap) |

### Argument Handling and Argv Copy

The function begins with a defensive copy of `argv` into a local buffer. When `8 * argc` fits within 0x800 bytes (argc ≤ 256), the copy lives in `v284[2096]` on the stack. For larger argument lists -- which can occur during complex nvcc invocations with many pass-through flags -- it allocates heap memory via `sub_16CD150`. This copy is necessary because the argument loop modifies pointers (advancing `i` to skip flag values), and the caller's argv must not be disturbed.

```c
if (8 * argc > 0x800)
    v284 = sub_16CD150(8 * argc);   // heap alloc for large argc
// else use stack buffer v284[2096]
memcpy(v284, argv, 8 * argc);       // copy all pointers
```

After copying, `sub_16C5290` extracts the base program name from `argv[0]` -- stripping directory prefixes -- and stores it in `dest`. This name appears in error messages and verbose output throughout the pipeline.

### Key Local Variables

The function's behavior is controlled by two critical dispatch variables: `v253` (which compilation backend to use) and `v263` (which phase of the pipeline to invoke). These are accumulated during the argument loop and combined after parsing to select one of ~10 possible code paths. The interaction between them creates a matrix of behaviors that covers everything from simple single-file compilation to multi-stage LibNVVM pipeline processing.

| Variable | Init | Purpose |
|---|---|---|
| `v253` | 2 | Dispatch mode: 0=Path B, 1=Path A, 2=default (needs env resolution) |
| `v263` | 0 | Invocation mode: 0=default, 1=lgenfe, 2=libnvvm, 3=lnk, 4=opt, 6=llc |
| `v242` | 0 | Target architecture (SM number) |
| `v258` | 0 | NVC flag |
| `v243` | 0 | OptiX IR flag |
| `v259` | 0 | Verbose (only effective in wizard mode) |
| `v261` | 0 | Dryrun |
| `v262` | 0 | Keep intermediates (only effective in wizard mode) |
| `s` | NULL | Input file path |
| `v257` | NULL | Output file path |
| `v256` | NULL | NVVM IR library path |
| `v266` | vector | Pass-through options vector |

### Wizard Mode

```c
v10 = getenv("NVVMCCWIZ");                    // 0x8F9D36
if (v10 && strtol(v10, NULL, 10) == 553282)   // 0x8F9D92
    byte_4F6D280 = 1;
```

Global `byte_4F6D280` gates the effectiveness of `-v`, `-keep`, `-dryrun`. Without wizard mode, these flags are silently ignored — `v259` and `v262` stay 0. This is a deliberate anti-reverse-engineering measure: even if someone discovers the `-v` flag, it does nothing without the magic environment variable. The magic number 553282 (0x87142) appears to be arbitrary.

### Invocation Modes (`v263`)

The `v263` variable determines *which stage* of the compilation pipeline cicc enters. When nvcc invokes cicc directly, `v263` stays at 0 (default). But cicc can also be invoked in sub-pipeline mode — for example, `-lnk` runs only the linking phase, `-opt` runs only the optimizer, and `-llc` runs only code generation. This is how the multi-stage pipeline works: the outer driver calls cicc multiple times with different `-lXXX` flags, or a single invocation with `-libnvvm` runs all stages internally.

Each mode has its own format for the `-discard-value-names` flag, which tells the LLVM backend whether to strip IR value names (reducing memory usage). The different formats exist because each sub-pipeline stage has its own option namespace:

| v263 | Flag | Mode | discard-value-names format |
|---|---|---|---|
| 0 | (none) | Default (nvcc invocation) | `-discard-value-names` |
| 1 | `-lgenfe` | EDG frontend linkage | `--discard_value_names=1` (underscores) |
| 2 | `-libnvvm` | LibNVVM API | `-discard-value-names=1` (dashes) |
| 3 | `-lnk` | Linker | `-lnk-discard-value-names=1` |
| 4 | `-opt` | Optimizer | `-opt-discard-value-names=1` |
| 5 | (internal) | Undocumented (sets `v278` high byte) | — |
| 6 | `-llc` | Standalone LLVM codegen | — |

### Input File Extensions

Input files are identified by extension during the argument loop. The **last** matching file wins (`s` is overwritten each time). Unrecognized arguments are added to the `v266` pass-through vector and forwarded to sub-pipelines. The `.cup` extension has a special restriction — it's only accepted when the *preceding* argument is `--orig_src_path_name` or `--orig_src_file_name`, which are metadata flags inserted by nvcc to track the original source file.

| Extension | Format | Condition |
|---|---|---|
| `.bc` | LLVM bitcode | Always accepted |
| `.ci` | CUDA intermediate (preprocessed) | Always accepted |
| `.i` | Preprocessed C/C++ | Always accepted |
| `.ii` | Preprocessed C++ | Always accepted |
| `.cup` | CUDA source | Only after `--orig_src_path_name` or `--orig_src_file_name` |
| `.optixir` | OptiX IR | Always accepted |

### Obfuscated Strings

At `0x8F98A0`, `sub_8F98A0` decrypts strings using an XOR + ROT13-like cipher:

```c
v40 = v37 ^ (-109 * ((offset + 97) ^ 0xC5));
// then ROT13 on alphabetic characters
```

This hides an environment variable name and option prefix from static analysis. The decrypted strings control the `v253` (Path A vs Path B) resolution when no explicit mode is specified.

### Error Messages

| Message | Condition | Address |
|---|---|---|
| `"Missing output file\n"` | `-o` with no next argument | `0x8FA365` |
| `"Missing NVVM IR library file\n"` | `-nvvmir-library` with no next arg | `0x8FAB34` |
| `"Unparseable architecture: "` + value | Invalid arch string | Multiple |
| `"Missing input file\n"` | No recognized input file | `0x8FBEAD` |
| `"Recognized input file extensions are: .bc .ci .i .cup .optixir"` | After missing input | `0x8FBE97` |
| `"Error: Output file was not specified (See -o option).\n"` | Multi-stage without `-o` | `0x8FB655` |

## The `v253` Dispatch Variable

The `v253` variable is the single most important dispatch control in the entire entry point. It determines whether the compilation uses Path A (the EDG/PTX-producing pipeline) or Path B (the standalone LLVM-based pipeline). Understanding its resolution logic is essential to reproducing cicc's behavior.

### Initialization and Explicit Setting

`v253` begins at 2 (unresolved default). During the argument loop, obfuscated string matching can set it directly:

| Source | Value | Meaning |
|---|---|---|
| Initial default | 2 | Needs environment variable resolution |
| Obfuscated option suffix matches `byte_3C23AC3` | 1 | Path A explicitly requested |
| Obfuscated option suffix matches `byte_3C23AB4` | 0 | Path B explicitly requested |

### Environment Variable Resolution

When `v253` remains at 2 after argument parsing (the common case), cicc resolves it through the obfuscated environment variable `NV_NVVM_VERSION` (decrypted from `byte_3C23A9F`). The resolution has two sub-cases depending on the target architecture:

```
if (v253 == 2) {
    env = getenv(decrypt(byte_3C23A9F));   // NV_NVVM_VERSION
    if (env matches decrypt(byte_3C23A82))       // "nvvm-latest"
        v253 = 1;  // Path A
    else if (env matches decrypt(byte_3C23A7B))  // "nvvm70"
        v253 = 0;  // Path B
    else if (v242 > 99 && !v258)                 // SM >= 100, not -nvc
        v253 = 0;  // Path B (new architectures default to standalone)
    else
        v253 = 1;  // Path A (legacy default)
}
```

The architectural threshold at SM 100 (Blackwell) is notable: for SM < 100, the default is Path A (the EDG frontend path). For SM >= 100, unless the `-nvc` flag is present, the default switches to Path B. This suggests NVIDIA is migrating newer architectures toward the standalone LLVM pipeline, possibly as a precursor to eventually deprecating the EDG-based path.

### Version Strings Injected per Path

After `v253` is resolved and for multi-stage modes (`v263 >= 3`), the entry point injects a version string into the pass-through options:

| v253 | Injected string | Semantics |
|---|---|---|
| 1 (Path A) | `"-nvvm-version=nvvm-latest"` (25 bytes from `xmmword_3C23BC0`) | Targets the latest NVVM IR specification |
| 0 (Path B) | `"-nvvm-version=nvvm70"` (20 bytes) | Targets NVVM 7.0 IR (frozen at LLVM 7.0.1 bitcode format) |

This version string propagates through the entire pipeline, controlling bitcode compatibility, intrinsic name resolution, and metadata format expectations.

## Post-Parse Dispatch Logic

After the argument loop terminates, the dispatch logic combines `v253` and `v263` to select the target function. The combined keep-and-verbose flag `v260 = v262 & v259` is also computed -- both wizard-mode flags must be active for intermediate file retention and verbose logging to function simultaneously.

### Simple Dispatch (v263 == 0)

When cicc is invoked without any `-lXXX` mode flag (the standard nvcc invocation path):

```c
if (v253 == 1)
    v8 = sub_902D10(dest, 0, &v266, s, v257, v256, v260, v262, v261);
    // Path A: CLI → lgenfe → LibNVVM pipeline
else
    v8 = sub_1262860(dest, 0, &v266, s, v257, v256, v260, v262, v261);
    // Path B: CLI → standalone LLVM pipeline
```

Both functions receive identical parameter signatures: program name, zero (unused), pass-through options, input file, output file, libdevice path, verbose+keep, keep, and dryrun. The return value becomes the process exit code.

### lgenfe Dispatch (v263 == 1)

The `-lgenfe` mode builds a full `argv`-style array with the program name as the first entry, followed by all `v266` pass-through options. This argv is then passed to one of two function pairs:

| v253 | Init function | Pipeline function |
|---|---|---|
| 1 (Path A) | `sub_B6EEA0` (LLVMContext + metadata kind registration) | `sub_905880` (EDG lgenfe) |
| 0 (Path B) | `sub_1602D10` (standalone context initialization) | `sub_1265340` (standalone lgenfe) |

The init functions create the LLVM context and register the 42+ metadata kinds used throughout the pipeline (dbg, tbaa, prof, noalias, etc.). These must be registered before any IR construction begins.

### Multi-Stage Dispatch (v263 >= 2)

For `-libnvvm`, `-lnk`, `-opt`, and `-llc` modes, the dispatch constructs a `CompilationState` structure with input/output strings, extra arguments, and the `v278` mode byte, then calls:

| v253 | Function | Size | Role |
|---|---|---|---|
| 1 | `sub_905EE0` | 43 KB | Path A multi-stage pipeline driver |
| 0 | `sub_1265970` | 48 KB | Path B multi-stage pipeline driver |

For `-libnvvm` (v263 == 2), the extra args are taken directly from `v266` without prepending the program name. For `-lnk`/`-opt`/`-llc` (v263 >= 3), the appropriate version string (`nvvm-latest` or `nvvm70`) is appended to the pass-through options before dispatch.

### Cleanup

After the pipeline function returns, `sub_8F9C90` performs deterministic cleanup in reverse allocation order: the `v281` extra-argument char** array and each entry, the `v275` output string, the `s2` input string, each element of the `v266` pass-through vector, the vector's backing buffer, the `dest` program name, and the `v282` argv copy buffer (if heap-allocated). The return value `v8` is 0 on success, 1 on argument errors, or the pipeline function's return code (stored in `v264`).

## Path A — EDG → LibNVVM Pipeline

Path A is the full CUDA C++ compilation path. It starts with the EDG 6.6 C++ frontend parsing CUDA source code into an IL tree, then converts that IL into LLVM IR via the lgenfe (LLVM Generation Front End) stage, and finally runs the LibNVVM pipeline to optimize and lower the IR to PTX. This is the path taken when cicc is invoked by nvcc for `.cu` file compilation, and it represents the standard CUDA compilation flow that most users encounter.

### Path A Orchestrator — `sub_902D10`

The orchestrator is a 9 KB function that sequences the three major stages of Path A compilation. It acts as the conductor between the CLI processing layer, the EDG frontend, and the LibNVVM optimizer/codegen.

| Field | Value |
|---|---|
| Address | `0x902D10` |
| Size | ~9 KB |
| Timer | Creates 8-byte timer via `sub_22077B0` → `sub_B6EEA0` |

**Execution flow:**

1. **Timer creation.** Allocates and initializes an 8-byte timing context. The `sub_B6EEA0` init function also registers the 42+ LLVM metadata kinds (dbg=1, tbaa=2, prof=3, ... noalias.addrspace=42) that all subsequent IR construction depends on. This is why the timer creation happens first: the metadata registration is a side effect of context initialization.

2. **CLI processing.** Calls `sub_900130` (39 KB) to parse the accumulated CLI flags into structured forms: command buffer `v58`, emit-llvm-bc flag `v52`, architecture compute/SM numbers `v55`/`v56`, and file paths. On failure: `"Error processing command line: <cmd>\n"`.

3. **Include path setup.** If an input file is present (`v64`), calls `sub_C98ED0` to configure system and user include paths for the EDG frontend.

4. **EDG frontend (lgenfe).** Calls `sub_905880` with timer name `"CUDA C++ Front-End"`. This stage:
   - Allocates an 880-byte module object via `sub_BA8740`
   - Processes lgenfe CLI options from the options struct
   - In dryrun mode: skips execution, frees the module, returns null
   - On success: returns a module pointer and sets the output path

5. **LibNVVM pipeline.** If lgenfe succeeds (module pointer is non-null), calls `sub_905EE0` with the module for the full optimization and codegen pipeline.

6. **Time profiler output.** After pipeline completion, checks `sub_C96F30()` for active profiling. If profiling is enabled, writes timing data to the output file via `sub_C9C600`. Failure emits: `"Error: Failed to write time profiler data.\n"`.

7. **Cleanup.** Frees the timer (`sub_B6E710`), option strings, and option arrays.

### EDG Frontend Stage — `sub_905880`

The lgenfe stage bridges the EDG 6.6 C++ frontend to LLVM IR generation. This is where CUDA C++ source code becomes NVVM IR.

| Field | Value |
|---|---|
| Address | `0x905880` |
| Size | ~6 KB |
| Timer label | `"CUDA C++ Front-End"` |
| Module size | 880 bytes (allocated by `sub_BA8740`) |

The function reconstructs a verbose command line for diagnostic output (quoting paths for `--orig_src_file_name`, `--orig_src_path_name`, `--compiler_bindir`, `--sdk_dir`), builds an argument array, and calls `sub_908750(numArgs, argArray, opt_level)` to create the LLVM module. On success, it copies the output path into the module at offset `21*8` and, if the keep flag is set via `a3->byte[66]`, calls `sub_905860` to write intermediate files.

The actual EDG parsing and IL-to-IR conversion happens inside `sub_908750`, which eventually calls `sub_617BD0` — the `lgenfe_main` function documented in the [EDG Frontend](edg.md) page.

### EDG Module Binding — `sub_908850`

After the EDG frontend produces its IL tree, `sub_908850` (10 KB) bridges the output to the LLVM backend. This function performs the critical step of configuring the LLVM module's data layout and target triple based on the target architecture.

**Data layout strings** are selected based on `unk_4F06A68` (address space width):

| Width | p3 flag | Data layout string |
|---|---|---|
| 8 (64-bit) | `unk_4D0461C` set | `"e-p:64:64:64-p3:32:32:32-i1:8:8-..."` (167 chars) |
| 8 (64-bit) | Not set | `"e-p:64:64:64-i1:8:8-..."` (155 chars) |
| 4 (32-bit) | — | `"e-p:32:32:32-i1:8:8-..."` (155 chars) |

The `p3:32:32:32` component enables 32-bit pointers in address space 3 (shared memory), which is critical for SM architectures where shared memory accesses use 32-bit addressing even in 64-bit compilation mode.

**Target triple** is set to `"nvptx64-nvidia-cuda"` for 64-bit or `"nvptx-nvidia-cuda"` for 32-bit. The function also:

- Creates a 496-byte target info structure via `sub_AE3F70`
- Iterates global function declarations, marking device functions for compilation via `sub_91CA00`
- Iterates global variables, processing initializers for device-side storage via `sub_9172F0`
- Runs LLVM module verification via `sub_B89FE0` -- on failure: `"there was an error in verifying the lgenfe output!"`
- Stores the module globally at `unk_4F6D2F8`

### LibNVVM Pipeline Driver — `sub_905EE0`

This 43 KB function is the core of Path A. It orchestrates the full compilation through 14 sequential phases, using an interesting indirection mechanism: rather than calling LibNVVM API functions directly, it resolves them at runtime through `sub_12BC0F0(id)` — a dispatch function that takes a numeric ID and returns a function pointer.

| Field | Value |
|---|---|
| Address | `0x905EE0` |
| Size | 43KB (1,268 lines) |
| Timer | `"LibNVVM"` |
| Orchestrator | `sub_902D10` (simple mode) |

### 14-Phase Compilation Flow

The compilation proceeds through these phases sequentially. Phases 2.1–2.14 are the core compilation unit lifecycle: create, populate, configure, compile, extract results, destroy. The `-keep` flag (when wizard mode is active) causes intermediate `.lnk.bc` and `.opt.bc` files to be written to disk, which is invaluable for debugging the pipeline.

| Phase | Action |
|---|---|
| 0 | Verbose command-line reconstruction |
| 1 | Input file handling — NVVM container (`sub_9047E0`) or regular bitcode |
| 2.1 | Pipeline infrastructure: verbose stream, address space query, module prep |
| 2.2 | `nvvmCreateCU` (dispatch ID 2151) |
| 2.3 | `nvvmCUAddModule` (dispatch ID 4660) |
| 2.4 | Check for `-opt`/`-llc` direct stage invocation |
| 2.5 | Parse and add additional modules from extra args |
| 2.6 | Libdevice linking: external file or embedded `unk_3EA0080` (455,876 bytes) |
| 2.7 | Intermediate file path construction (`.lnk.bc`, `.opt.bc`) |
| 2.8 | Option parsing via `sub_9685E0` → 4 stage vectors (lnk/opt/lto/llc) |
| 2.9 | Verbose stage logging + callback registration |
| 2.10 | Load 37 LLVM options from `off_4B90FE0` via `nvvmSetOptionStrings` (ID 0xFEED) |
| 2.11 | `nvvmCUCompile` (ID 0xBEAD) with phase code `57069` (0xDEED) |
| 2.12 | Get compilation log/warnings |
| 2.13 | Get PTX output |
| 2.14 | `nvvmDestroyCU` (ID 21257 = 0x5309) |
| 3 | Error/warning output to caller |
| 4 | Write output file (text or binary detection via ELF magic) |
| 5 | Timer stop |

### Input File Handling — Phase 1

Phase 1 has two sub-paths based on the `a3->byte[65]` container flag:

**Path 1A: NVVM IR Container.** When the input is an NVVM container (a binary format wrapping IR plus compilation options), `sub_9047E0` (10 KB) parses it. The container format encodes the target SM version, FTZ mode, precision settings, and IEEE mode. The parser extracts these and converts them to LLVM CLI flags:

```c
// Pseudo-code for container option extraction
push("-march=nvptx");
push("-mcpu=sm_" + str(container->sm_version / 10));
if (container->flags[200] & 0x20) push("-nvptx-f32ftz");
if (container->flags[200] & 0x80) push("-nvptx-prec-sqrtf32=1");
else                               push("-nvptx-prec-sqrtf32=0");
push(container->flags[204] ? "-nvvm-ieee-mode=S" : "-nvvm-ieee-mode=T");
if (container->mode == 2) push("--device-c");  // relocatable compilation
```

If parsing fails, the error message is `"Invalid NVVM IR Container"` (error code 259).

**Path 1B: Regular LLVM bitcode.** For raw `.bc` files, the function creates a timer object, configures the SM architecture via `sub_B6F950`, opens the file via `sub_C7EAD0`, and parses it into an LLVM module via `sub_A01950`.

### LibNVVM API Dispatch IDs

Internal function `sub_12BC0F0(id)` returns API function pointers by numeric ID. This indirection exists because the LibNVVM API is implemented within the same binary — these aren't dynamically-linked external functions but rather internal call points resolved through a dispatch table. The hex IDs double as a form of internal documentation:

| ID | Hex | Function |
|---|---|---|
| 2151 | 0x0867 | `nvvmCreateCU` |
| 4111 | 0x100F | `nvvmGetCompiledResult` |
| 4660 | 0x1234 | `nvvmCUAddModule` |
| 17185 | 0x4321 | `nvvmCUSetExtraArgs` |
| 21257 | 0x5309 | `nvvmDestroyCU` |
| 41856 | 0xA380 | `nvvmGetCompilationLog` |
| 46903 | 0xB737 | `nvvmGetCompiledResultLog` |
| 46967 | 0xB777 | `nvvmGetErrorString` |
| 48813 | 0xBEAD | `nvvmCUCompile` |
| 48879 | 0xBEEF | Callback registrar |
| 61451 | 0xF00B | `nvvmGetCompiledResultSize` |
| 62298 | 0xF37A | `nvvmCUAddModuleFromBuffer` |
| 65261 | 0xFEED | `nvvmCUSetOptions` |

The complete dispatch table in `sub_12BC0F0` contains 25 entries implemented as a binary search tree on the ID value:

| ID | Hex | Target | Semantic Name |
|---|---|---|---|
| 2151 | 0x0867 | `sub_12BB090` | `nvvmCreateCU` |
| 2167 | 0x0877 | `sub_12BB090` | (alias) |
| 3911 | 0x0F47 | `sub_12BBF40` | `nvvmCUSetProgressCallback` |
| 4111 | 0x100F | `sub_12BA8F0` | `nvvmGetCompiledResult` |
| 4606 | 0x11FE | `sub_12BA330` | `nvvmCULinkModule` |
| 4660 | 0x1234 | `sub_12BC650` | `nvvmCUAddModule` |
| 8320 | 0x2080 | `sub_12BB400` | `nvvmCUSetOption` |
| 11245 | 0x2BED | `sub_12BB290` | `nvvmCUGetLog` |
| 17185 | 0x4321 | `sub_12BBD80` | `nvvmCUSetExtraArgs` |
| 21257 | 0x5309 | `sub_12B9C40` | `nvvmDestroyCU` |
| 23294 | 0x5AFE | `sub_12BAF10` | `nvvmVerify` |
| 41856 | 0xA380 | `sub_12BA220` | `nvvmGetCompiledResultSize` |
| 45242 | 0xB0BA | `sub_12BAB40` | `nvvmCUGetWarnings` |
| 46903 | 0xB737 | `sub_12BA7C0` | `nvvmGetCompiledResultLog` |
| 46967 | 0xB777 | `sub_12B9980` | `nvvmGetErrorString` |
| 48813 | 0xBEAD | `sub_12BA110` | `nvvmCUCompile` |
| 48879 | 0xBEEF | `sub_12BACF0` | `nvvmCURegisterCallback` |
| 49522 | 0xC172 | `sub_12BA470` | `nvvmCUGetIR` |
| 51966 | 0xCAFE | `sub_12B9A50` | `nvvmGetVersion` |
| 56495 | 0xDCEF | `sub_12B9A40` | (unknown) |
| 57005 | 0xDEAD | `sub_12B9C00` | `nvvmInit` |
| 61451 | 0xF00B | `sub_12BA560` | `nvvmGetCompiledResultPTXSize` |
| 61453 | 0xF00D | `sub_12BA6A0` | `nvvmCURegisterLNKCallback` |
| 61806 | 0xF16E | `sub_12BAA30` | `nvvmCUGetOptIR` |
| 62298 | 0xF37A | `sub_12BC8B0` | `nvvmCUAddModuleFromBuffer` |
| 65261 | 0xFEED | `sub_12B9AB0` | `nvvmSetOptionStrings` |

### Public LibNVVM API vs Internal CU API

The dispatch table above reveals a critical architectural detail: cicc's internal API uses **compilation unit** semantics (`nvvmCreateCU`, `nvvmCUAddModule`, `nvvmCUCompile`), while the public LibNVVM shared library (`libnvvm.so`) exports a different API surface using **program** semantics (`nvvmCreateProgram`, `nvvmAddModuleToProgram`, `nvvmCompileProgram`). The public API is documented in NVIDIA's `nvvm.h` header; the internal API exists only within cicc and is never exported.

Evidence for this mapping comes from nvlink's `-dlto` code path, which dynamically loads `libnvvm.so` via `dlsym()` and resolves symbols by their public names:

```c
// nvlink sub_4BC290 — loads libnvvm.so for device LTO
dlsym(handle, "nvvmCreateProgram");    // → internally nvvmCreateCU
dlsym(handle, "nvvmCompileProgram");   // → internally nvvmCUCompile
dlsym(handle, "nvvmGetCompiledResultSize");
dlsym(handle, "nvvmGetCompiledResult");
dlsym(handle, "nvvmDestroyProgram");   // → internally nvvmDestroyCU
```

The complete mapping between the public `libnvvm.so` API (as used by external callers like nvlink and user programs) and cicc's internal CU dispatch IDs:

| Public API (`libnvvm.so`) | Internal Name | Dispatch ID | Hex | Target |
|---|---|---|---|---|
| `nvvmCreateProgram` | `nvvmCreateCU` | 2151 | 0x0867 | `sub_12BB090` |
| `nvvmAddModuleToProgram` | `nvvmCUAddModule` | 4660 | 0x1234 | `sub_12BC650` |
| `nvvmLazyAddModuleToProgram` | `nvvmCUAddModuleFromBuffer` | 62298 | 0xF37A | `sub_12BC8B0` |
| `nvvmCompileProgram` | `nvvmCUCompile` | 48813 | 0xBEAD | `sub_12BA110` |
| `nvvmVerifyProgram` | `nvvmVerify` | 23294 | 0x5AFE | `sub_12BAF10` |
| `nvvmGetCompiledResultSize` | `nvvmGetCompiledResultPTXSize` | 61451 | 0xF00B | `sub_12BA560` |
| `nvvmGetCompiledResult` | `nvvmGetCompiledResult` | 4111 | 0x100F | `sub_12BA8F0` |
| `nvvmGetProgramLogSize` | `nvvmGetCompiledResultSize` | 41856 | 0xA380 | `sub_12BA220` |
| `nvvmGetProgramLog` | `nvvmGetCompiledResultLog` | 46903 | 0xB737 | `sub_12BA7C0` |
| `nvvmDestroyProgram` | `nvvmDestroyCU` | 21257 | 0x5309 | `sub_12B9C40` |

Note the naming confusion in the internal API: `nvvmGetCompiledResultSize` (ID 0xA380) returns the **log** size, while `nvvmGetCompiledResultPTXSize` (ID 0xF00B) returns the actual PTX output size. The public API resolves this with clearer names (`nvvmGetProgramLogSize` vs `nvvmGetCompiledResultSize`).

The internal-only API entries have no public equivalents:

| Internal Name | Dispatch ID | Hex | Target | Purpose |
|---|---|---|---|---|
| `nvvmInit` | 57005 | 0xDEAD | `sub_12B9C00` | One-time initialization of LLVM infrastructure |
| `nvvmGetVersion` | 51966 | 0xCAFE | `sub_12B9A50` | Returns internal NVVM version tuple |
| `nvvmGetErrorString` | 46967 | 0xB777 | `sub_12B9980` | Maps `nvvmResult` code to human-readable string |
| `nvvmSetOptionStrings` | 65261 | 0xFEED | `sub_12B9AB0` | Bulk-loads LLVM CLI option table (37 entries) |
| `nvvmCUSetExtraArgs` | 17185 | 0x4321 | `sub_12BBD80` | Passes additional argc/argv to compilation |
| `nvvmCUSetOption` | 8320 | 0x2080 | `sub_12BB400` | Sets a single compilation option |
| `nvvmCUSetProgressCallback` | 3911 | 0x0F47 | `sub_12BBF40` | Registers progress/cancellation callback |
| `nvvmCURegisterCallback` | 48879 | 0xBEEF | `sub_12BACF0` | Registers stage-boundary callback (verbose output) |
| `nvvmCURegisterLNKCallback` | 61453 | 0xF00D | `sub_12BA6A0` | Registers LNK-stage-specific callback |
| `nvvmCUGetLog` | 11245 | 0x2BED | `sub_12BB290` | Alternative log retrieval interface |
| `nvvmCUGetWarnings` | 45242 | 0xB0BA | `sub_12BAB40` | Retrieves warning-only messages |
| `nvvmCUGetIR` | 49522 | 0xC172 | `sub_12BA470` | Retrieves intermediate LLVM IR after linking |
| `nvvmCUGetOptIR` | 61806 | 0xF16E | `sub_12BAA30` | Retrieves optimized IR (post-OPT stage); also used by `-irversion` |
| `nvvmCULinkModule` | 4606 | 0x11FE | `sub_12BA330` | Explicit module linking (separate from add-then-compile) |
| (unknown) | 56495 | 0xDCEF | `sub_12B9A40` | Unknown (one byte smaller than `nvvmGetVersion`) |
| (alias) | 2167 | 0x0877 | `sub_12BB090` | Alias for `nvvmCreateCU` (same target, different ID) |

The `nvvmCUGetOptIR` function at `sub_12BAA30` serves double duty: it is both the post-optimization IR retrieval API and the target of `sub_12BC0E0` (a thunk called from `sub_8F9C90` for the `-irversion` flag). When the user passes `-irversion`, the real main calls `sub_12BC0E0` which dispatches to `sub_12BAA30`, which returns the IR version tuple as `major * 100 + minor`. This value is printed to stdout and the process exits immediately.

### The `sub_12BC0F0` Dispatch Mechanism

`sub_12BC0F0` is a ~3 KB function at `0x12BC0F0` that implements a binary search tree over the 25 dispatch IDs. The function takes a single `unsigned int` argument (the ID) and returns a function pointer (`void*`). The tree is hardcoded as a series of comparison-and-branch instructions, not as a data-driven lookup table.

```c
// Pseudocode for sub_12BC0F0(unsigned int id)
void* nvvm_dispatch(unsigned int id) {
    // Binary search over 25 IDs
    if (id < 17185) {
        if (id < 4660) {
            if (id == 2151 || id == 2167) return sub_12BB090;
            if (id == 3911) return sub_12BBF40;
            if (id == 4111) return sub_12BA8F0;
            if (id == 4606) return sub_12BA330;
        } else {
            if (id == 4660)  return sub_12BC650;
            if (id == 8320)  return sub_12BB400;
            if (id == 11245) return sub_12BB290;
        }
    } else {
        // ... upper half of the tree
        if (id == 48813) return sub_12BA110;   // 0xBEAD
        if (id == 65261) return sub_12B9AB0;   // 0xFEED
        // etc.
    }
    return NULL;  // unknown ID
}
```

The hex IDs are deliberately memorable patterns used as a form of internal documentation: `0xDEAD` = init, `0xBEAD` = compile, `0xBEEF` = callback, `0xCAFE` = version, `0xFEED` = options, `0xF00D` = LNK callback, `0xF00B` = result size. The secondary ID `0x0877` (2167) is an alias for `0x0867` (2151) and dispatches to the same `sub_12BB090` target, suggesting an internal API version migration where both old and new IDs must remain functional.

### Dual-Path Initialization

The two compilation paths (Path A and Path B) use independent initialization sequences, creating a **dual-path initialization** architecture where the same underlying LLVM infrastructure is bootstrapped through different entry points. This is why two copies of libdevice, two LLVM options tables, and two sets of verbose callbacks exist.

```
Path A initialization (EDG → LibNVVM):
  sub_B6EEA0  — Creates LLVMContext + registers 42+ metadata kinds
                 (dbg=1, tbaa=2, prof=3, ... noalias.addrspace=42)
  sub_900130  — 39 KB CLI parser for Path A flags
  sub_905880  — EDG frontend produces LLVM module (880-byte object)
  sub_908850  — Binds module to target: data layout, triple, verification
  → sub_905EE0 enters LibNVVM pipeline with module

Path B initialization (Standalone):
  sub_1602D10 — Creates standalone LLVMContext (no EDG metadata assumptions)
  sub_125FB30 — 8 KB CLI parser for Path B flags
  sub_1265340 — Pre-compilation setup (configure output path, timer)
  → sub_1265970 enters LibNVVM pipeline with bitcode input
```

The version resolver `sub_12B9F70` at `0x12B9F70` is shared between both paths and determines which NVVM IR compatibility mode to use. It reads two obfuscated environment variables in sequence:

```c
// Pseudocode for sub_12B9F70(unsigned int sm_version)
int nvvm_version_resolve(unsigned int sm_version) {
    // Try NV_NVVM_VERSION first (decrypted from 0x3C23A90)
    char *env = getenv(decrypt("NV_NVVM_VERSION"));
    if (!env) {
        // Fallback: try LIBNVVM_NVVM_VERSION (decrypted from 0x42812F0)
        env = getenv(decrypt("LIBNVVM_NVVM_VERSION"));
    }
    if (env) {
        if (strcmp(env, "nvvm70") == 0)      return 0;  // Path B mode
        if (strcmp(env, "nvvm-latest") == 0)  return 1;  // Path A mode
    }
    // Default: SM >= 100 uses Path B, SM < 100 uses Path A
    return (sm_version > 99) ? 0 : 1;
}
```

This function is called from both `sub_8F9C90` (the real main, for `v253` resolution) and `sub_12BB580` (inside the LibNVVM compilation unit initialization). The dual call-site ensures that the version mode is consistent regardless of whether the compiler was invoked via CLI or via the LibNVVM API.

The `nvvmInit` function (ID 0xDEAD, `sub_12B9C00`) performs one-time LLVM infrastructure initialization. It is called implicitly during `nvvmCreateCU` (`sub_12BB090`) via a `pthread_once` guard at `dword_4F92D9C`. The initialization includes:

1. Registering LLVM target triples (`nvptx64-nvidia-cuda`, `nvptx-nvidia-cuda`)
2. Initializing the NVPTX target machine factory
3. Setting up the LLVM pass registry
4. Configuring thread-safety based on `LIBNVVM_DISABLE_CONCURRENT_API` (`byte_4F92D70`)

When `byte_4F92D70 == 1` (concurrent API disabled), the pipeline operates in single-threaded mode — no `pthread_mutex` locks are acquired around compilation unit operations, and Phase II concurrent optimization is disabled regardless of the module's function count.

### Internal API Usage Sequence

The complete sequence of dispatch table calls during a standard Path A compilation (from `sub_905EE0`):

```
1.  sub_12BC0F0(2151)   → nvvmCreateCU(&handle)
    Creates compilation unit. Calls nvvmInit via pthread_once on first use.

2.  sub_12BC0F0(46967)  → nvvmGetErrorString
    Saved for later error message formatting.

3.  sub_12BC0F0(4660)   → nvvmCUAddModule(handle, IR_data, IR_size, NULL)
    Adds the user's LLVM bitcode module.

4.  sub_12BC0F0(21257)  → nvvmDestroyCU
    Saved as cleanup function pointer (not called yet).

5.  sub_12BCB00 [thunk]  → nvvmCUAddModuleFromBuffer(handle, buf, size, NULL)
    Called N times: once per additional module from extra args,
    once for libdevice (embedded or external).

6.  sub_12BC0F0(48879)  → nvvmCURegisterCallback
    Registers verbose stage callbacks:
      sub_903BA0 with ID 61453 (LNK stage)
      sub_903730 with ID 47710 (LLC stage)
    When -keep mode active, also registers:
      sub_9085A0 with ID 64222 (OPT output → .opt.bc file)
      sub_908220 with ID 56993 (LLC output → final file)

7.  sub_12BC0F0(65261)  → nvvmSetOptionStrings(opts_table, 37)
    Loads 37 LLVM backend configuration strings from off_4B90FE0.
    Calls sub_1C31130() internally to register/reset LLVM options.

8.  sub_12BC0F0(48813)  → nvvmCUCompile(handle, 57069)
    Main compilation. Phase code 57069 (0xDEED) triggers full
    LNK → OPT → [OPTIXIR] → LLC pipeline in sub_12C35D0.

9.  sub_12BC0F0(17185)  → nvvmCUSetExtraArgs(handle, argc, argv)
    Passes additional arguments collected from the CLI.

10. sub_12BC0F0(41856)  → nvvmGetCompiledResultSize(handle, &log_size)
    Queries the compilation log size.

11. sub_12BC0F0(46903)  → nvvmGetCompiledResultLog(handle, log_buf)
    Retrieves the compilation log (warnings/errors).

12. sub_12BC0F0(61451)  → nvvmGetCompiledResultPTXSize(handle, &ptx_size)
    Queries the PTX output size.

13. sub_12BC0F0(4111)   → nvvmGetCompiledResult(handle, ptx_buf)
    Copies the generated PTX into the caller's buffer.

14. sub_12BC0F0(21257)  → nvvmDestroyCU(&handle)
    Destroys the compilation unit, frees all internal resources.
```

Path B (`sub_1265970`) follows the identical sequence but uses `off_4C6EEE0` for the options table (step 7), `unk_420FD80` for the embedded libdevice (step 5), and appends `"-nvvm-version=nvvm70"` instead of `"-nvvm-version=nvvm-latest"` to the pipeline arguments.

### nvvmResult Error Codes

The `nvvmGetErrorString` function (ID 0xB777, `sub_12B9980`) maps integer result codes from all API functions to descriptive strings:

| Code | Constant | Description |
|---|---|---|
| 0 | `NVVM_SUCCESS` | Operation completed successfully |
| 1 | `NVVM_ERROR_OUT_OF_MEMORY` | Memory allocation failed |
| 2 | `NVVM_ERROR_PROGRAM_CREATION_FAILURE` | Failed to create compilation unit |
| 3 | `NVVM_ERROR_IR_VERSION_MISMATCH` | Incompatible NVVM IR version detected |
| 4 | `NVVM_ERROR_INVALID_INPUT` | Malformed input (bad bitcode, wrong magic) |
| 5 | `NVVM_ERROR_INVALID_PROGRAM` | Null or invalid compilation unit handle |
| 6 | `NVVM_ERROR_INVALID_IR` | IR failed verification |
| 7 | `NVVM_ERROR_INVALID_OPTION` | Unrecognized compilation option |
| 8 | `NVVM_ERROR_NO_MODULE_IN_PROGRAM` | Compilation unit has no modules added |
| 9 | `NVVM_ERROR_COMPILATION` | Compilation failed (linker, optimizer, or codegen error) |
| 10 | `NVVM_ERROR_CANCELLED` | Compilation cancelled by user callback |

The pipeline orchestrator `sub_12C35D0` maps its internal return codes to these: 0 → `NVVM_SUCCESS`, 7 → `NVVM_ERROR_INVALID_OPTION`, 9 → `NVVM_ERROR_COMPILATION`, 10 → `NVVM_ERROR_CANCELLED`, 100 → `NVVM_ERROR_COMPILATION` (post-pipeline verification failure).

### 37 LLVM Options from `off_4B90FE0`

Phase 2.10 loads a hardcoded table of 37 LLVM option strings from `off_4B90FE0` (296 bytes = 37 pointers). These are static, compiled-in LLVM backend configuration flags that are injected into every compilation unit via `nvvmSetOptionStrings` (ID 0xFEED). The options include target architecture flags (`-march=nvptx64`, `-mcpu=sm_XX`), math precision controls (`-nvptx-f32ftz`, `-nvptx-prec-sqrtf32=`), optimization levels, debug info flags, and NVPTX-specific feature knobs. The sub_12B9AB0 target function calls `sub_1C31130()` -- the LLVM option registration/reset function -- to apply them.

### Embedded Libdevice

A key design decision: **two identical copies** of the libdevice bitcode are statically embedded in the binary. Each is 455,876 bytes (~445 KB) of LLVM bitcode containing ~400+ math functions (`__nv_sin`, `__nv_cos`, `__nv_exp`, `__nv_log`, `__nv_sqrt`, etc.) plus atomic operation helpers and FP16/BF16 conversion routines. The duplication exists because Path A and Path B have separate initialization sequences and the linker didn't deduplicate the `.rodata` sections.

When the user provides `-nvvmir-library <path>`, the external file is used instead. This allows overriding the built-in math library — useful for testing custom libdevice builds.

| Path | Address | Size | Purpose |
|---|---|---|---|
| Path A | `unk_3EA0080` | 455,876 bytes | Default libdevice for LibNVVM mode |
| Path B | `unk_420FD80` | 455,876 bytes | Default libdevice for standalone mode |

### Verbose Callbacks and Intermediate Files

Phase 2.9 registers callback functions that fire at pipeline stage boundaries. When verbose mode is active, these callbacks produce reconstructed command-line output for each stage:

```
[ "<src>" -lnk -nvvmir-library "<path>" "<input>" -o "<file>.lnk.bc" <opts> -nvvm-version=nvvm-latest ]
[ "<src>" -llc "<llc_path>" -o "<output>" <opts> -nvvm-version=nvvm-latest ]
```

The callback registration uses `sub_12BC0F0(48879)` (ID 0xBEEF = `nvvmCURegisterCallback`) with stage-specific callback IDs:

| Callback | ID | Stage |
|---|---|---|
| `sub_903BA0` | 61453 | LNK stage output |
| `sub_903730` | 47710 | LLC stage output |
| `sub_9085A0` | 64222 | OPT output (keep mode) |
| `sub_908220` | 56993 | LLC output (keep mode) |

Intermediate file paths (`.lnk.bc` for linked-but-unoptimized, `.opt.bc` for optimized-but-not-yet-codegen'd) are always constructed as strings, but the actual files are only written to disk when the `-keep` flag is active in wizard mode.

### Path A Error Messages

All errors from `sub_905EE0` are written to stderr via `sub_223E0D0`. Error categories:

| Category | Prefix | Example |
|---|---|---|
| File I/O | `"<src>: "` | `"error in open <file>"`, `"input file <f> read error"` |
| LibNVVM API | `"libnvvm: error: "` | `"failed to create the libnvvm compilation unit"` |
| Output | `"<src>: "` | `"IO error: <system_error_msg>"` |
| Fatal | (none) | `"basic_string::append"` (std::string overflow at 0x3FFFFFFFFFFFFFFF) |

The error code from LibNVVM API calls maps to `nvvmResult`: 0 = success, 1 = out of memory, 4 = invalid input, 5 = invalid compilation unit (null handle).

## Path B — Standalone cicc Pipeline (`sub_1265970`)

Path B is the standalone compilation path used when cicc is invoked with LLVM bitcode input (`.bc` files), by the LibNVVM API directly, or as the default for SM >= 100 architectures. Despite the different entry point, it shares the same underlying LLVM infrastructure as Path A — the difference is in how modules are loaded and how the pipeline stages are orchestrated. Path B appends `-nvvm-version=nvvm70` to the optimizer arguments, indicating it targets the NVVM 7.0 IR specification (corresponding to LLVM 7.0.1 bitcode format, the version NVIDIA froze their IR compatibility at).

The 4-stage pipeline (LNK → OPT → OPTIXIR → LLC) runs in-memory: each stage takes an LLVM Module, transforms it, and passes it to the next stage. The OPTIXIR stage is optional and only active when `--emit-optix-ir` is specified. A user-provided cancellation callback can abort compilation between stages (return code 10).

| Field | Value |
|---|---|
| Address | `0x1265970` |
| Size | ~48KB (1,371 lines) |
| Timer | `"LibNVVM"` (same name as Path A) |
| Version string | `-nvvm-version=nvvm70` |

### Path B Entry — `sub_1262860`

`sub_1262860` (418 lines) is the command-line entry point for Path B, analogous to `sub_902D10` for Path A. It parses CLI flags, initializes the compilation context, and calls `sub_1265970` for the actual compilation.

| Field | Value |
|---|---|
| Address | `0x1262860` |
| Timer init | `sub_1602D10` (standalone context, contrasted with Path A's `sub_B6EEA0`) |
| CLI parser | `sub_125FB30` (Path B's equivalent of Path A's `sub_900130`) |

The flow is: allocate timer handle → parse CLI via `sub_125FB30` → configure output path → call `sub_1265340` for pre-compilation setup → call `sub_1265970` for compilation → write output. Output can go to stdout if the output path is `"-"`, handled by `sub_125C500`. On failure: `"\n Error processing command line: <details>"`.

### Path B Compilation Orchestrator — `sub_1265970`

This 48 KB function mirrors `sub_905EE0`'s role but with Path B's initialization and context. It handles both LibNVVM API invocations (when `a11 = 1`) and CLI invocations (when `a11 = 0`), with the same 14-phase structure as Path A but using Path B's context objects and the `nvvm70` version string.

**Key behavioral differences from Path A:**

1. **Context initialization.** Path B uses `sub_1602D10` for context init (rather than `sub_B6EEA0`), which creates a standalone LLVM context without the EDG frontend's metadata registration assumptions.

2. **NVVM IR container handling.** Container parsing is performed by `sub_12642A0` (Path B's container parser) rather than `sub_9047E0`.

3. **Embedded libdevice address.** Uses `unk_420FD80` (the second copy) rather than `unk_3EA0080`.

4. **LLVM options table.** Loads 37 options from `off_4C6EEE0` (Path B's copy) rather than `off_4B90FE0`.

5. **Verbose callbacks.** Registers `sub_1263280` (ID 61453) and `sub_12636E0` (ID 47710) for LNK and OPT stage output respectively, and `sub_1268040`/`sub_1267CC0` for keep-mode output.

6. **Version string.** Always appends `"-nvvm-version=nvvm70"` rather than `"-nvvm-version=nvvm-latest"`.

### 4-Stage Pipeline Orchestrator — `sub_12C35D0`

The orchestrator creates two backend objects — `nvopt` (512 bytes, the optimizer) and `nvllc` (480 bytes, the code generator) — and wires them together with the stage dispatch structure. Each stage is controlled by a bit in a stage bitmask derived from `sub_12D2AA0`, which parses architecture and options into per-stage configuration.

| Field | Value |
|---|---|
| Address | `0x12C35D0` |
| Size | 41KB (1,446 lines) |
| Backend objects | `nvopt` (512 bytes) + `nvllc` (480 bytes) |

| Stage | Bit | Timer String | Core Function |
|---|---|---|---|
| LNK | 0x01 | `"LNK"` / `"LibNVVM module linking step."` | `sub_12C06E0` (63KB, module linker) |
| OPT | 0x80 | `"OPT"` / `"LibNVVM optimization step."` | `sub_12E7E70` (full LLVM pipeline) |
| OPTIXIR | 0x40 | `"OPTIXIR"` / `"LibNVVM Optix IR step."` | `sub_12F9270` (OptiX IR gen) |
| LLC | 0x04 | `"LLC"` / `"LibNVVM code-generation step."` | `sub_12F5100` (SelectionDAG codegen) |

Pipeline stage bitmask (from `sub_12D2AA0`): bit 0=LNK, bit 2=LLC, bit 5=verify, bit 6=OPTIXIR, bit 7=OPT.

Return codes: 0=success, 7=parse failure, 9=link/layout/verification error, 10=cancelled, 100=post-pipeline verification failure.

### Backend Object Initialization

The orchestrator allocates and initializes two backend objects with distinct vtables:

```c
// nvllc — code generator backend (480 bytes)
v8 = sub_22077B0(480);
sub_12EC960(v8, "nvllc", 5);
v8->vtable = &unk_49E7FF0;

// nvopt — optimizer backend (512 bytes)
v10 = sub_22077B0(512);
sub_12EC960(v10, "nvopt", 5);
v10->vtable = &unk_49E6A58;
v10->sub_vtable = &unk_49E6B20;    // at offset +60*8
v10->plugin_slots[0..2] = 0;       // offsets 61-63 cleared
```

A stage dispatch structure (vtable `&unk_49E6B38`) links the OPT output to the LLC input and stores the cancellation callback pointer.

### Cancellation Callback

Between every pipeline stage, the orchestrator checks an optional user-provided cancellation callback stored at `state[26]`:

```c
cancellation_fn = state[26];
if (cancellation_fn && cancellation_fn(state[27], 0))
    return 10;   // CANCELLED
```

This mechanism allows the LibNVVM API caller to abort a long-running compilation. Return code 10 propagates up through the entire call chain, causing `sub_8F9C90` to return 10 as the process exit code.

### Two-Phase Optimization (OPT Stage)

The OPT stage calls `sub_12E7E70`, which implements a **two-phase optimization protocol**. Both phases call the same underlying pipeline function `sub_12E54A0`, but a TLS variable `qword_4FBB3B0` is set to 1 or 2 to indicate which phase is active:

| Phase | TLS value | Purpose |
|---|---|---|
| Phase I | 1 | Analysis + early IR optimization (module-level, CGSCC, function passes) |
| Phase II | 2 | Backend optimization + codegen preparation (lowering, legalization) |
| Complete | 3 | Compilation finished for this module |

Between phases, `sub_12D4250` checks **concurrency eligibility**: if the module contains more than one defined function (non-declaration), and the options permit it, Phase II can run with multiple threads. Thread count is determined from `opts[1026]` or falls back to `get_nprocs()`. When concurrency is enabled, `sub_12E7B90` is the concurrent worker entry point.

For single-function modules, the optimizer skips the two-phase protocol entirely and runs a single un-phased call to `sub_12E54A0` -- no phase counter is set, and the optimizer executes both analysis and backend passes in one invocation.

### Data Layout Validation

After the LLC stage but before returning, the orchestrator validates the module's data layout string. If the module has no data layout:

```
"DataLayoutError: Data Layout string is empty"
→ return 9
```

On layout mismatch, it produces a detailed diagnostic:

```
"<error details>\nExample valid data layout:\n64-bit: <reference_layout>"
```

The reference layout string is loaded from `off_4CD4948[0]`.

### Module Linker — `sub_12C06E0`

The LNK stage's core function (63KB) links multiple LLVM bitcode modules into a single module. This is where user code gets linked with the libdevice math library and any additional modules. The linker performs several validation steps to catch incompatible IR early — before the expensive optimization and codegen stages:

- **Bitcode magic validation**: checks for `0xDE,0xC0,0x17,0x0B` (raw LLVM bitcode) or `0x42,0x43,0xC0,0xDE` (bitcode wrapper). Anything else → error code 9.
- **Triple validation**: every module's target triple must start with `"nvptx64-"`. Modules without a triple get a clear error: `"Module does not contain a triple, should be 'nvptx64-'"`.
- **IR version compatibility**: `sub_12BFF60` reads `"nvvmir.version"` metadata (2 or 4 element tuples: major.minor or major.minor.debug_major.debug_minor). The `NVVM_IR_VER_CHK` environment variable can disable this check entirely (set to `"0"`), useful when mixing IR from different CUDA toolkit versions.
- **Symbol size matching**: for multi-module linking, compares the byte sizes of identically-named globals across modules. Size computation uses type codes (1=half(16b), 2=float(32b), 3=double(64b), 7=ptr, 0xB=integer, 0xD=struct, 0xE=array). A mismatch produces: `"Size does not match for <sym> in <mod> with size X specified in <other> with size Y."`

**Single-module fast path:** When only one module is present (after adding user code and libdevice), the linker returns it directly via `sub_1C3DFC0` without invoking the full linking machinery.

**Multi-module linking:** For N > 1 modules, the linker copies the primary module's target triple to all secondary modules, then calls `sub_12F5610` to perform the LLVM link. After user modules are linked, builtin modules (from `a1[3..4]`) are linked via `sub_1CCEBE0`, followed by target feature configuration via `sub_1CB9110` and `sub_1619140`.

### NVVM IR Version Checker — `sub_12BFF60`

The version checker reads `"nvvmir.version"` named metadata and validates it against the compiler's expected version range.

| Field | Value |
|---|---|
| Address | `0x12BFF60` |
| Size | ~9 KB (362 lines) |
| Metadata key | `"nvvmir.version"` |
| Debug metadata | `"llvm.dbg.cu"` |

Version tuples come in two forms:
- **2-element**: `(major, minor)` — IR version only. Special case: `(2, 0)` always passes.
- **4-element**: `(major, minor, debug_major, debug_minor)` — IR version plus debug info version. Special case: `debug_major == 3, debug_minor <= 2` always passes.

The `NVVM_IR_VER_CHK` environment variable is checked **multiple times** throughout the validation. When set to `"0"`, all version checks are bypassed, returning 0 (compatible). This is a critical escape hatch for mixing bitcode from different CUDA toolkit versions.

## Memory Management

### jemalloc — The Global Allocator

cicc statically links a **jemalloc 5.x** allocator in the address range `0x12FC000`–`0x131FFFF` (~400 functions). This replaces the system `malloc`/`free` entirely. The jemalloc configuration parser (`sub_12FCDB0`, 131,600 bytes -- the largest single function in this range) handles the `MALLOC_CONF` environment variable and `/etc/malloc.conf` symlink, supporting dozens of tuning options: `abort`, `cache_oblivious`, `metadata_thp`, `trust_madvise`, `retain`, `dss`, `tcache`, `narenas`, `percpu_arena`, `background_thread`, `san_guard_small`, `san_guard_large`, and more.

The choice of jemalloc over glibc's allocator is significant for compiler workloads. jemalloc's thread-local caching (`tcache`) and arena-per-CPU design (`percpu_arena`) reduce contention during the concurrent Phase II optimization, where multiple threads may be simultaneously allocating and freeing IR nodes, instruction objects, and analysis results.

The jemalloc stats subsystem (functions at `0x400000`–`0x42FFFF`) provides comprehensive per-arena statistics including allocation counts, active/dirty/muzzy page tracking, mutex contention metrics, and HPA hugify counts. These can be triggered via `MALLOC_CONF="stats_print:true"`.

### EDG Memory Regions — `sub_822260`

The EDG 6.6 frontend uses a custom memory region system configured with `USE_MMAP_FOR_MEMORY_REGIONS = 1`. During post-parse validation in `sub_617BD0` (lgenfe_main), `sub_822260()` is called 11 times to initialize memory regions 1 through 11. These regions serve as arena-style allocators for different categories of EDG internal data:

- **Token buffers** (preprocessor token storage)
- **IL node pools** (intermediate language tree nodes)
- **Symbol tables** (name→declaration mappings)
- **Type representations** (structural type information)

The mmap-backed regions grow by mapping additional pages on demand, avoiding the fragmentation problems that would occur with individual `malloc` calls for the millions of small, short-lived objects the frontend creates during parsing. Region cleanup happens in bulk when the frontend completes -- all pages for a region are unmapped at once rather than individually freed.

The EDG heap allocator cluster at `0x821000`–`0x823FFF` includes tracked allocation (`sub_822B10`/`sub_822B90`) with a 1024-entry inline tracking array (`unk_4F19620`, 1024 * 24 bytes) that overflows to heap when exceeded. The tracking count is maintained in `dword_4F19600`. The finalization function `sub_823310` walks bucket chains to free all tracked allocations.

### Large Argument Lists

The argv copy in `sub_8F9C90` uses a threshold-based allocation strategy:

```c
if (8 * argc <= 0x800)   // argc <= 256
    v284 = stack_buffer;  // 2096 bytes on stack
else
    v284 = sub_16CD150(8 * argc);  // heap allocation
```

This avoids heap allocation for the common case (most cicc invocations have fewer than 256 arguments) while handling the worst case gracefully. The heap path uses `sub_16CD150` (a realloc-like wrapper), and the buffer is freed during cleanup if it was heap-allocated.

## Signal Handling and Crash Recovery

### EDG Signal Handler

The EDG frontend registers a signal handler at `0x723610` during initialization:

```c
// signal handler (0x723610)
void handler(int sig) {
    write(STDERR_FILENO, "\n", 1);
    dword_4F0790C = 1;    // set "interrupted" flag
    sub_7235F0(9);         // initiate orderly shutdown
}
```

This handler is registered for SIGINT, allowing the compiler to be interrupted gracefully during long frontend operations (template instantiation, constexpr evaluation). The global `dword_4F0790C` flag is checked periodically by the parser loop, enabling cooperative cancellation.

### LLVM Crash Recovery

The LLVM infrastructure provides its own crash handling via the `print-on-crash` and `print-on-crash-path` CLI options (registered in the `0x4F0000`–`0x51FFFF` range). When enabled, the LLVM pass manager dumps the current IR to a specified path on any unhandled signal (SIGSEGV, SIGABRT, etc.). This is separate from the EDG handler and covers the optimization and codegen phases.

### Concurrent API Protection

The global constructor at `0x4A5810` checks `LIBNVVM_DISABLE_CONCURRENT_API`. When set (to any value), `byte_4F92D70 = 1` disables thread-safe LibNVVM API usage. The pipeline orchestrator (`sub_12C35D0`) uses `pthread_once(&dword_4F92D9C, init_routine)` for one-time setup, and TLS at `__readfsqword(0)-24` stores exception handling stack frames while `__readfsqword(0)-32` stores the cleanup function `sub_12BCC20`. These TLS slots ensure that concurrent compilations in the same process do not corrupt each other's state.

## Timer Infrastructure

Compilation timing is implemented through a hierarchical timer system. Timer creation (`sub_C996C0`) takes a label and context string; timer stop (`sub_C9AF60`) records the elapsed time. The timer hierarchy is:

```
"CUDA C++ Front-End"     ← EDG parsing + IL-to-IR conversion (Path A only)
  └─ "LibNVVM"           ← Full optimization + codegen pipeline
       ├─ "LNK"          ← Module linking (sub_12C06E0)
       ├─ "OPT"          ← LLVM optimization (sub_12E7E70)
       │    ├─ "Phase I"  ← Analysis + early optimization
       │    └─ "Phase II" ← Backend optimization + codegen prep
       ├─ "OPTIXIR"      ← OptiX IR generation (optional)
       └─ "LLC"          ← SelectionDAG codegen (sub_12F5100)
```

The profiler is controlled by `sub_C96F30()` (returns nonzero when active). Timer data is written to the output file after compilation via `sub_C9C600` (Path A) or `sub_16DD960` (Path B). The `-time` flag or environment variable controls activation. The timer names appear in the profiler output, making them essential for identifying compilation bottlenecks.

## Architecture Detection — `sub_95EB40`

One of the most important functions in cicc: the architecture detection system translates a single user-facing flag like `-arch=compute_90a` into **three independent flag strings**, one for each pipeline stage. This 3-column fan-out is necessary because the EDG frontend, the LLVM optimizer, and the LLVM backend each use different flag formats to specify the target architecture. The mapping is stored in a `std::map<string, ArchTriple>` in a red-black tree at `a1+248`.

| Column | Target | Example |
|---|---|---|
| Column 1 | EDG frontend | `-R __CUDA_ARCH=750` |
| Column 2 | Optimizer | `-opt-arch=sm_75` |
| Column 3 | LLC backend | `-mcpu=sm_75` |

### Architecture Validation Bitmask

Before the 3-column mapping is consulted, the architecture number is validated against a hardcoded 64-bit bitmask. This is a fast rejection filter: the SM number minus 75 gives a bit index, and if that bit isn't set in the constant `0x60081200F821`, the architecture is rejected. This means cicc v13.0 has a **fixed, compile-time-determined** set of supported architectures — you cannot add new SM targets without rebuilding the binary.

```c
offset = arch_number - 75;
if (offset > 0x2E || !_bittest64(&0x60081200F821, offset))
    → ERROR: "is an unsupported option"
```

Valid architectures (bit positions in `0x60081200F821`). Note the gaps — SM 81–85, 91–99, 101–102, 104–109, 111–119 are all absent:

| Bit | SM | Generation |
|---|---|---|
| 0 | 75 | Turing |
| 5 | 80 | Ampere |
| 11 | 86 | Ampere |
| 12 | 87 | Ampere (Jetson Orin) |
| 13 | 88 | Ada (undocumented) |
| 14 | 89 | Ada Lovelace |
| 15 | 90 | Hopper |
| 25 | 100 | Blackwell |
| 28 | 103 | Blackwell |
| 35 | 110 | Jetson Thor |
| 45 | 120 | Blackwell (sm120) — RTX 50xx / Pro |
| 46 | 121 | Blackwell (sm120) — DGX Spark |

Suffix handling: `a` and `f` variants share the base SM number for validation but get distinct `-mcpu=sm_XXa`/`-mcpu=sm_XXf` strings.

### Architecture Parsing in the EDG Frontend

The EDG frontend (`sub_617BD0`, option ID 0x52 = `--nv_arch`) performs its own independent architecture parsing that produces three global variables:

| Global | Address | Purpose |
|---|---|---|
| `unk_4D045E8` | `0x4D045E8` | SM compute version (integer: 75, 80, ..., 121) |
| `unk_4D045E4` | `0x4D045E4` | Accelerated flag (1 if suffix `a`) |
| `unk_4D045E0` | `0x4D045E0` | Fast flag (1 if suffix `f`; also sets accelerated=1) |

The `f` suffix (fast-mode) is new to SM >= 100 architectures. When present, it implies a forward-compatible feature set that may not exactly match the base SM version's capabilities.

## Flag Catalog — `sub_9624D0`

The flag catalog is the second-largest function in the entry point range at 75KB. It takes the raw CLI arguments and sorts them into **four output vectors** — one per pipeline stage (lnk, opt, lto, llc). This is the translation layer between user-facing flags and the internal per-stage options that each pipeline component understands.

A clever detail: the function takes a "mode cookie" parameter (`a4`) that distinguishes CUDA compilation (`0xABBA`) from OpenCL compilation (`0xDEED`). Several flags behave differently depending on this cookie — for example, `-prec-div=0` maps to `-nvptx-prec-divf32=1` in CUDA mode but `-nvptx-prec-divf32=0` in OpenCL mode, reflecting the different default precision expectations of the two languages.

| Field | Value |
|---|---|
| Address | `0x9624D0` |
| Size | 75KB (2,626 lines) |
| Mode cookie | `a4`: `0xABBA`=CUDA, `0xDEED`=OpenCL |
| Output vectors | lnk, opt, lto, llc (32-byte std::string elements with SSO) |

### -Ofast-compile Levels

NVIDIA's `-Ofast-compile` is a compile-time vs runtime-performance tradeoff. At "max" level, it disables memory space optimization and LSA optimization entirely — these are expensive analysis passes that improve runtime performance but slow compilation significantly. The "mid" and "min" levels provide intermediate points. This feature is targeted at iterative development workflows where compile speed matters more than code quality.

| Level String | Internal Value | Effect |
|---|---|---|
| `"max"` | 2 | Most optimizations skipped, forces `-lsa-opt=0 -memory-space-opt=0` |
| `"mid"` | 3 | Medium speedup |
| `"min"` | 4 | Minimal speedup |
| `"0"` | 1 → reset to 0 | Disabled |

Error: `"libnvvm : error: -Ofast-compile specified more than once"`. Only one `-Ofast-compile` per compilation is allowed.

### Flag-to-Pipeline Routing (Selected)

This table shows how a single user-facing flag gets split into per-stage options. The pattern reveals NVIDIA's compilation architecture: the LNK stage communicates via `-R` macro definitions (these become `#define`s visible to the linker), the OPT stage uses NVIDIA-specific optimizer flags (`-opt-use-*`), and the LLC stage uses LLVM backend flags (`-nvptx-*`). Some flags like `-ftz=1` propagate to all three stages, while others like `-aggressive-inline` only affect the optimizer.

| User Flag | LNK Forward | OPT Forward | LLC Forward |
|---|---|---|---|
| `-ftz=1` | `-R __CUDA_FTZ=1` | `-nvptx-f32ftz` | `-nvptx-f32ftz` |
| `-prec-div=1` (CUDA) | `-R __CUDA_PREC_DIV=1` | `-opt-use-prec-div=true` | `-nvptx-prec-divf32=2` |
| `-prec-div=0` (CUDA) | — | `-opt-use-prec-div=false` | `-nvptx-prec-divf32=1` |
| `-prec-sqrt=1` | `-R __CUDA_PREC_SQRT=1` | — | `-nvptx-prec-sqrtf32=1` |
| `-fma=1` | — | — | `-nvptx-fma-level=1` |
| `-fast-math` (CUDA) | `-R __CUDA_USE_FAST_MATH=1` | `-opt-use-fast-math` | — |
| `-unsafe-math` | `-R FAST_RELAXED_MATH=1 -R __CUDA_FTZ=1` | `-opt-use-fast-math -nvptx-f32ftz` | `-nvptx-fma-level=1 -nvptx-f32ftz` |
| `-aggressive-inline` | — | `-inline-budget=40000` | — |
| `-new-nvvm-remat` | — | — | `-enable-new-nvvm-remat=true -nv-disable-remat=true -rp-aware-mcse=true` |

### nvcc→cicc Flag Translation — `sub_8FE280`

When cicc is invoked by nvcc (the CUDA compiler driver), the flags arrive in nvcc's format and need to be translated to cicc's internal format. This translation happens through a red-black tree at `qword_4F6D2A0`, populated once on first use (guarded by `qword_4F6D2C8`). Each entry maps an nvcc flag to a pair: an EDG passthrough string and a cicc internal string. Some flags only affect one side — for example, `-fmad=1` has no EDG equivalent (FMA is a backend concern) but maps to cicc's `-fma=1`. Others are dual-mapped: `-O0` becomes both `--device-O=0` for EDG and `-opt=0` for cicc.

| nvcc Flag | EDG Passthrough | cicc Internal |
|---|---|---|
| `-O0`..`-O3` | `--device-O=N` | `-opt=N` |
| `-fmad=1` | — | `-fma=1` |
| `-prec_sqrt=1` | — | `-prec-sqrt=1` |
| `-Ofast-compile=max` | — | `-Ofast-compile=max` |
| `-Ofc=max` | — | `-Ofast-compile=max` (alias) |
| `--emit-optix-ir` | `--emit-lifetime-intrinsics` | `--emit-optix-ir` |
| `-discard-value-names` | `--discard_value_names=1` | `-discard-value-names=1` |

## Environment Variables

cicc checks 20 distinct environment variables across its subsystems. The six NVIDIA-specific variables are the most important for understanding and reimplementing the entry point behavior:

| Variable | Function | Effect |
|---|---|---|
| `NVVMCCWIZ` | `sub_8F9C90` | Set to `553282` → enables wizard mode (`byte_4F6D280 = 1`) |
| `NVVM_IR_VER_CHK` | `sub_12BFF60` | Set to `"0"` → disables NVVM IR version checking |
| `LIBNVVM_DISABLE_CONCURRENT_API` | ctor at `0x4A5810` | Any value → disables thread-safe API (`byte_4F92D70 = 1`) |
| `NV_NVVM_VERSION` | `sub_8F9C90`, `sub_12B9F70` | `"nvvm70"` or `"nvvm-latest"` → controls Path A/B default and IR compat mode |
| `LIBNVVM_NVVM_VERSION` | `sub_12B9F70` | Same as `NV_NVVM_VERSION` (checked as fallback) |
| `LLVM_OVERRIDE_PRODUCER` | ctors at `0x48CC90`, `0x4CE640` | Overrides the producer string in output bitcode metadata |

The `NV_NVVM_VERSION` and `LIBNVVM_NVVM_VERSION` variables are **obfuscated** in the binary using the same XOR+ROT13 cipher as the CLI option strings. They are decrypted from `0x3C23A90` and `0x42812F0` respectively.

## Key Global Variables

These globals persist across the entire compilation and are accessed from multiple subsystems. The wizard mode flag and flag mapping tree are set during CLI parsing and read throughout the pipeline. The embedded libdevice addresses are compile-time constants (`.rodata`), while the data model width is set during architecture configuration.

| Variable | Purpose |
|---|---|
| `byte_4F6D280` | Wizard mode flag (gates `-v`, `-keep`) |
| `qword_4F6D2A0` | Flag mapping red-black tree root |
| `qword_4F6D2C8` | Tree initialization guard |
| `byte_4F6D2D0` | `--partial-link` active flag |
| `byte_4F6D2DC` | `--force-llp64` active flag |
| `unk_3EA0080` | Embedded libdevice bitcode (Path A, 455,876 bytes) |
| `unk_420FD80` | Embedded libdevice bitcode (Path B, 455,876 bytes) |
| `off_4B90FE0` | LLVM options table (Path A, 37 entries) |
| `off_4C6EEE0` | LLVM options table (Path B, 37 entries) |
| `unk_4F06A68` | Data model width (8=64-bit, 4=32-bit) |
| `unk_4D0461C` | Enable `p3:32:32:32` in data layout (shared mem 32-bit ptrs) |
| `byte_4F92D70` | Concurrent API disabled flag |
| `dword_4F92D9C` | pthread_once guard for one-time pipeline setup |
| `qword_4FBB3B0` | TLS: optimization phase counter (1=Phase I, 2=Phase II, 3=done) |
| `unk_4F6D2F8` | Global module pointer (set by `sub_908850` after EDG binding) |

## Function Map — Entry Point Cluster

| Address | Size | Identity |
|---|---|---|
| `0x4396A0` | 16 B | `main()` thunk → `sub_8F9C90` |
| `0x8F98A0` | ~512 B | String deobfuscation (XOR + ROT13) |
| `0x8F9C20` | ~128 B | Push string to `std::vector<std::string>` |
| `0x8F9C90` | 10,066 B | Real main — CLI parser + dispatcher |
| `0x8FE280` | ~4 KB | nvcc→cicc flag translation (red-black tree) |
| `0x900130` | 39 KB | Path A CLI processing |
| `0x902D10` | ~9 KB | Path A orchestrator (simple mode) |
| `0x903730` | ~5 KB | LLC stage verbose callback |
| `0x903BA0` | ~5 KB | LNK stage verbose callback |
| `0x9047E0` | 10 KB | NVVM IR container parser (Path A) |
| `0x905880` | ~6 KB | CUDA C++ Front-End (lgenfe stage) |
| `0x905E50` | ~256 B | lgenfe single-stage wrapper (Path A) |
| `0x905EE0` | 43 KB | LibNVVM pipeline driver (Path A) |
| `0x908850` | 10 KB | Backend SM config + EDG module binding |
| `0x95EB40` | 38 KB | Architecture detection (3-column fan-out) |
| `0x9624D0` | 75 KB | Flag catalog (4 output vectors) |
| `0x9685E0` | ~8 KB | Pipeline option parser (4 stage vectors) |
| `0x125FB30` | ~8 KB | Path B CLI processing |
| `0x1262860` | ~4 KB | Path B entry (simple mode) |
| `0x1263280` | ~1 KB | Path B LNK verbose callback |
| `0x12636E0` | ~1 KB | Path B OPT verbose callback |
| `0x12642A0` | ~3 KB | NVVM container parser (Path B) |
| `0x1265340` | ~4 KB | Path B pre-compilation setup |
| `0x12658E0` | ~256 B | lgenfe single-stage wrapper (Path B) |
| `0x1265970` | 48 KB | LibNVVM compilation entry (Path B) |
| `0x12BC0F0` | ~3 KB | LibNVVM API dispatch table (25 entries) |
| `0x12BCB00` | ~64 B | Thunk → `sub_12BC8B0` (nvvmCUAddModuleFromBuffer) |
| `0x12BFF60` | ~9 KB | NVVM IR version checker |
| `0x12C06E0` | 63 KB | Module linker (LNK stage core) |
| `0x12C35D0` | 41 KB | 4-stage pipeline orchestrator |
| `0x12D2AA0` | ~4 KB | Stage bitmask parser |
| `0x12D4250` | ~2 KB | Concurrency eligibility check |
| `0x12E7E70` | ~8 KB | Two-phase optimizer entry |
| `0x12E7B90` | ~4 KB | Concurrent worker entry point |
| `0x12F5100` | ~12 KB | LLC core (SelectionDAG codegen) |
| `0x12F9270` | ~6 KB | OptiX IR generator |
| `0x1602D10` | ~2 KB | Path B context initialization |

## Cross-References

- [EDG Frontend](edg.md) — `sub_617BD0` (lgenfe_main), the 282-case CLI dispatch inside the EDG 6.6 frontend
- [NVVM Container Format](../structs/nvvm-container.md) — Container parsing by `sub_9047E0` (Path A) and `sub_12642A0` (Path B)
- [Optimizer Pipeline](optimizer.md) — The OPT stage driven by `sub_12E7E70` (two-phase optimization)
- [IR Generation](ir-generation.md) — Module creation via `sub_908850` (EDG module binding)
- [PTX Emission](../infra/asmprinter.md) — The LLC stage's PTX output via `sub_12F5100`
