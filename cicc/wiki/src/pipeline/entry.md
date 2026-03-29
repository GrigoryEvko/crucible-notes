# Entry Point & CLI

Real main function, command-line processing, dual-path compilation dispatch, and architecture detection. Address range `0x8F0000`–`0x96FFFF` (~520 KB of code).

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

| Field | Value |
|---|---|
| Address | `0x8F9C90`–`0x8FC3E2` |
| Size | 10,066 bytes |
| Stack frame | 0x978 bytes (2,424 bytes) |
| Local buffers | `v284[2096]` for argv copy (stack if argc ≤ 256, else heap) |

### Key Local Variables

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

Global `byte_4F6D280` gates the effectiveness of `-v`, `-keep`, `-dryrun`. Without wizard mode, these flags are silently ignored — `v259` and `v262` stay 0.

### Invocation Modes (`v263`)

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

| Field | Value |
|---|---|
| Address | `0x905EE0` |
| Size | 43KB (1,268 lines) |
| Timer | `"LibNVVM"` |
| Orchestrator | `sub_902D10` (simple mode) |

### 14-Phase Compilation Flow

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

Internal function `sub_12BC0F0(id)` returns API function pointers by numeric ID:

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

| Path | Address | Size | Purpose |
|---|---|---|---|
| Path A | `unk_3EA0080` | 455,876 bytes | Default libdevice for LibNVVM mode |
| Path B | `unk_420FD80` | 455,876 bytes | Default libdevice for standalone mode |

Used when no `-nvvmir-library` path is provided. Contains ~400+ math functions (`__nv_*`, `__nvvm_*`).

## Path B — Standalone cicc Pipeline (`sub_1265970`)

| Field | Value |
|---|---|
| Address | `0x1265970` |
| Size | ~48KB (1,371 lines) |
| Timer | `"LibNVVM"` (same name) |
| Version string | `-nvvm-version=nvvm70` |

### 4-Stage Pipeline Orchestrator — `sub_12C35D0`

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

- Validates bitcode magic: `0xDE,0xC0,0x17,0x0B` (LLVM) or `0x42,0x43,0xC0,0xDE` (wrapper)
- Triple validation: must start with `"nvptx64-"`
- IR version check via `sub_12BFF60`: reads `"nvvmir.version"` metadata, `NVVM_IR_VER_CHK` env var override
- Symbol size matching across modules (type codes: 1=half, 2=float, 3=double, 7=ptr, 0xB=integer, 0xD=struct, 0xE=array)

## Architecture Detection — `sub_95EB40`

Builds a `std::map<string, ArchTriple>` in a red-black tree at `a1+248`. Each entry maps a CLI flag to three forwarded strings:

| Column | Target | Example |
|---|---|---|
| Column 1 | EDG frontend | `-R __CUDA_ARCH=750` |
| Column 2 | Optimizer | `-opt-arch=sm_75` |
| Column 3 | LLC backend | `-mcpu=sm_75` |

### Architecture Validation Bitmask

```c
offset = arch_number - 75;
if (offset > 0x2E || !_bittest64(&0x60081200F821, offset))
    → ERROR: "is an unsupported option"
```

Valid architectures (bit positions in `0x60081200F821`):

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
| 35 | 110 | Post-Blackwell |
| 45 | 120 | Post-Blackwell |
| 46 | 121 | Post-Blackwell |

Suffix handling: `a` and `f` variants share the base SM number for validation but get distinct `-mcpu=sm_XXa`/`-mcpu=sm_XXf` strings.

## Flag Catalog — `sub_9624D0`

| Field | Value |
|---|---|
| Address | `0x9624D0` |
| Size | 75KB (2,626 lines) |
| Mode cookie | `a4`: `0xABBA`=CUDA, `0xDEED`=OpenCL |
| Output vectors | lnk, opt, lto, llc (32-byte std::string elements with SSO) |

### -Ofast-compile Levels

| Level String | Internal Value | Effect |
|---|---|---|
| `"max"` | 2 | Most optimizations skipped, forces `-lsa-opt=0 -memory-space-opt=0` |
| `"mid"` | 3 | Medium speedup |
| `"min"` | 4 | Minimal speedup |
| `"0"` | 1 → reset to 0 | Disabled |

Error: `"libnvvm : error: -Ofast-compile specified more than once"`

### Flag-to-Pipeline Routing (Selected)

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

Red-black tree at `qword_4F6D2A0` (populated once, guarded by `qword_4F6D2C8`). Selected mappings:

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
