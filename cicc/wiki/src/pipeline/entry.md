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

## Path A — LibNVVM Pipeline (`sub_905EE0`)

Path A is the primary compilation path when cicc is invoked through the LibNVVM API (e.g., by nvcc or by applications using the CUDA Driver API's runtime compilation). The driver function `sub_902D10` first processes CLI flags, then optionally runs the EDG frontend (the "CUDA C++ Front-End" stage timed as `"CUDA C++ Front-End"`), and finally hands the resulting LLVM module to `sub_905EE0` — the 43KB pipeline driver that orchestrates the full compilation through 14 sequential phases.

The pipeline uses an interesting indirection mechanism: rather than calling LibNVVM API functions directly, it resolves them at runtime through `sub_12BC0F0(id)` — a dispatch function that takes a numeric ID and returns a function pointer. The IDs appear to be deliberately chosen as memorable hex values (0xFEED, 0xBEAD, 0xDEED, 0xBEEF) — likely internal jokes by the NVIDIA compiler team.

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
| 46967 | 0xB797 | `nvvmGetErrorString` |
| 48813 | 0xBEAD | `nvvmCUCompile` |
| 48879 | 0xBEEF | Callback registrar |
| 61451 | 0xF00B | `nvvmGetCompiledResultSize` |
| 62298 | 0xF37A | `nvvmCUAddModuleFromBuffer` |
| 65261 | 0xFEED | `nvvmCUSetOptions` |

### Embedded Libdevice

A key design decision: **two identical copies** of the libdevice bitcode are statically embedded in the binary. Each is 455,876 bytes (~445 KB) of LLVM bitcode containing ~400+ math functions (`__nv_sin`, `__nv_cos`, `__nv_exp`, `__nv_log`, `__nv_sqrt`, etc.) plus atomic operation helpers and FP16/BF16 conversion routines. The duplication exists because Path A and Path B have separate initialization sequences and the linker didn't deduplicate the `.rodata` sections.

When the user provides `-nvvmir-library <path>`, the external file is used instead. This allows overriding the built-in math library — useful for testing custom libdevice builds.

| Path | Address | Size | Purpose |
|---|---|---|---|
| Path A | `unk_3EA0080` | 455,876 bytes | Default libdevice for LibNVVM mode |
| Path B | `unk_420FD80` | 455,876 bytes | Default libdevice for standalone mode |

## Path B — Standalone cicc Pipeline (`sub_1265970`)

Path B is the standalone compilation path used when cicc is invoked directly (without the LibNVVM intermediary). Despite the different entry point, it shares the same underlying LLVM infrastructure as Path A — the difference is in how modules are loaded and how the pipeline stages are orchestrated. Path B appends `-nvvm-version=nvvm70` to the optimizer arguments, indicating it targets the NVVM 7.0 IR specification (corresponding to LLVM 7.0.1 bitcode format, the version NVIDIA froze their IR compatibility at).

The 4-stage pipeline (LNK → OPT → OPTIXIR → LLC) runs in-memory: each stage takes an LLVM Module, transforms it, and passes it to the next stage. The OPTIXIR stage is optional and only active when `--emit-optix-ir` is specified. A user-provided cancellation callback can abort compilation between stages (return code 10).

| Field | Value |
|---|---|
| Address | `0x1265970` |
| Size | ~48KB (1,371 lines) |
| Timer | `"LibNVVM"` (same name) |
| Version string | `-nvvm-version=nvvm70` |

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

### Module Linker — `sub_12C06E0`

The LNK stage's core function (63KB) links multiple LLVM bitcode modules into a single module. This is where user code gets linked with the libdevice math library and any additional modules. The linker performs several validation steps to catch incompatible IR early — before the expensive optimization and codegen stages:

- **Bitcode magic validation**: checks for `0xDE,0xC0,0x17,0x0B` (raw LLVM bitcode) or `0x42,0x43,0xC0,0xDE` (bitcode wrapper). Anything else → error code 9.
- **Triple validation**: every module's target triple must start with `"nvptx64-"`. Modules without a triple get a clear error: `"Module does not contain a triple, should be 'nvptx64-'"`.
- **IR version compatibility**: `sub_12BFF60` reads `"nvvmir.version"` metadata (2 or 4 element tuples: major.minor or major.minor.debug_major.debug_minor). The `NVVM_IR_VER_CHK` environment variable can disable this check entirely (set to `"0"`), useful when mixing IR from different CUDA toolkit versions.
- **Symbol size matching**: for multi-module linking, compares the byte sizes of identically-named globals across modules. Size computation uses type codes (1=half(16b), 2=float(32b), 3=double(64b), 7=ptr, 0xB=integer, 0xD=struct, 0xE=array). A mismatch produces: `"Size does not match for <sym> in <mod> with size X specified in <other> with size Y."`

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
| `off_4B90FE0` | LLVM options table (37 entries) |
| `unk_4F06A68` | Data model width (8=64-bit, 4=32-bit) |
| `unk_4D0461C` | Enable `p3:32:32:32` in data layout (shared mem 32-bit ptrs) |
