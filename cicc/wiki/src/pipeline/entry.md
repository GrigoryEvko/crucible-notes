# Entry Point & CLI

Real main function, command-line processing, dual-path compilation dispatch, and architecture detection. Address range `0x8F0000`–`0x96FFFF` (~520 KB of code).

| | |
|---|---|
| **main() thunk** | `0x4396A0` (16 bytes) — `return sub_8F9C90(argc, argv, envp)` |
| **Real main** | `sub_8F9C90` (56KB, 1990 lines) |
| **Wizard mode** | `getenv("NVVMCCWIZ") == 553282` → `byte_4F6D280 = 1` |
| **Default arch** | `compute_75` / `sm_75` |
| **Flag catalog** | `sub_9624D0` (75KB, 142 unique flags) |
| **Architecture map** | `sub_95EB40` (38KB, 30 architectures) |
| **Pipeline stages** | LNK → OPT → \[OPTIXIR\] → LLC |
| **Dual path** | Path A (`sub_905EE0`, LibNVVM) / Path B (`sub_1265970`, standalone cicc) |
| **Libdevice** | Embedded at `unk_3EA0080` (455,876 bytes) or external file |

## Architecture

Two compilation paths converge on the same 4-stage pipeline:

```
main (0x4396A0, 16B thunk)
  │
  └─ sub_8F9C90 (56KB, REAL MAIN)
       │
       ├─ v253 == 1 ──→ PATH A (LibNVVM mode)
       │    ├─ sub_900130 (39KB, CLI processing)
       │    ├─ sub_8FE280 (35KB, nvcc→cicc flag mapping)
       │    └─ sub_905EE0 (43KB, LibNVVM pipeline driver)
       │         ├─ sub_12BC0F0 (module load, builtin link, compile)
       │         ├─ Libdevice: unk_3EA0080 (456KB embedded) or external
       │         └─ Outputs: .lnk.bc → .opt.bc → .ptx
       │
       ├─ v253 == 0 ──→ PATH B (standalone cicc mode)
       │    └─ sub_1265970 (48KB, LibNVVM API entry)
       │         ├─ Read inputs, create compilation unit
       │         ├─ Add modules, link with builtins
       │         └─ sub_12C35D0 (41KB, pipeline orchestrator)
       │              ├─ LNK:     "LibNVVM module linking step."
       │              ├─ OPT:     "LibNVVM optimization step."
       │              ├─ OPTIXIR: "LibNVVM Optix IR step."
       │              └─ LLC:     "LibNVVM code-generation step."
       │
       └─ v253 == 2 ──→ DEFAULT (EDG frontend first, then Path A or B)
            └─ sub_5D2A80 (EDG orchestrator) → sub_905EE0 or sub_1265970
```

## Real Main — `sub_8F9C90`

| Field | Value |
|---|---|
| Address | `0x8F9C90` |
| Size | 56KB (1,990 lines) |
| Strings | 50+ CLI flag names |

### Wizard Mode

```c
if (getenv("NVVMCCWIZ") && atoi(getenv("NVVMCCWIZ")) == 553282)
    byte_4F6D280 = 1;  // wizard mode enabled
```

### Dispatch Variable (`v253`)

| Value | Mode | Handler |
|---|---|---|
| 2 | Default (full pipeline) | EDG → NVVM → LLVM → PTX |
| 1 | Path A (LibNVVM) | `sub_902D10` / `sub_905EE0` |
| 0 | Path B (standalone) | `sub_1262860` / `sub_1265970` |

### CLI Flags (Real Main)

| Flag | Purpose |
|---|---|
| `-o` | Output file path |
| `-nvvmir-library` | Path to NVVM IR library (libdevice) |
| `-v` | Verbose mode |
| `-dryrun` | Dry run (parse only) |
| `-keep` | Keep intermediate files (.lnk.bc, .opt.bc) |
| `-arch` / `--nv_arch` | Target architecture (compute_XX) |
| `-mcpu=sm_XX` | Backend target CPU |
| `--emit-optix-ir` | OptiX IR output mode |
| `-lgenfe` / `-libnvvm` / `-lnk` / `-opt` / `-llc` | Phase selectors (1/2/3/4/6) |

### Input Extensions

| Extension | Format |
|---|---|
| `.bc` | LLVM bitcode |
| `.ci` / `.i` / `.ii` | Preprocessed C/C++ |
| `.cup` | CUDA preprocessed |
| `.optixir` | OptiX IR |

## Path A — LibNVVM Pipeline

### CLI Processing — `sub_900130`

| Field | Value |
|---|---|
| Address | `0x900130` |
| Size | 39KB |

Default architecture: `compute_75` / `sm_75`. Processes:

| Flag | Purpose |
|---|---|
| `--emit-llvm-bc` | Emit LLVM bitcode instead of PTX |
| `-maxreg` | Maximum register count |
| `-split-compile` | Split compilation mode |
| `--Xlgenfe` / `--Xlibnvvm` / `--Xlnk` / `--Xopt` / `--Xllc` | Pass-through to sub-phases |
| `-covinfo` | Coverage info generation |
| `-extra-device-vectorization` | Enable extra device vectorization |
| `-gen-lto` | Generate LTO bitcode |

### Flag Mapping Table — `sub_8FE280`

| Field | Value |
|---|---|
| Address | `0x8FE280` |
| Size | 35KB |
| Data structure | Red-black tree at `qword_4F6D2A0` |

Maps nvcc-facing flags to internal cicc equivalents:

| nvcc Flag | cicc Equivalent |
|---|---|
| `-ftz` | `-nvptx-f32ftz` |
| `-prec_sqrt` | `-nvptx-prec-sqrtf32=` |
| `-prec_div` | `-nvptx-prec-divf32=` |
| `-fmad` | `-nvptx-fma-level=` |
| `-O0` / `-O1` / `-O2` / `-O3` | Optimization level |
| `-Osize` | Size optimization |
| `-Om` | Memory optimization |
| `-Ofast-compile` | Fast-compile tiers |

### LibNVVM Pipeline Driver — `sub_905EE0`

| Field | Value |
|---|---|
| Address | `0x905EE0` |
| Size | 43KB |
| Timer name | `"LibNVVM"` |

Calls `sub_12BC0F0` for the compilation lifecycle:
1. Module load
2. Builtin linking (libdevice at `unk_3EA0080`, 455,876 bytes)
3. Compile (invokes the full LNK → OPT → LLC pipeline)
4. Output retrieval

Creates intermediate files: `.lnk.bc` and `.opt.bc`.
Loads 37 LLVM options from table at `off_4B90FE0`.

## Path B — Standalone cicc Pipeline

### LibNVVM API Entry — `sub_1265970`

| Field | Value |
|---|---|
| Address | `0x1265970` |
| Size | 48KB |

The outermost entry point for LibNVVM compilation. Orchestrates:
1. Read input files
2. Create compilation unit
3. Add modules
4. Link with builtins (`-nvvmir-library`)
5. Run pipeline via `sub_12C35D0`
6. Extract PTX output
7. Handle errors and verbose mode (`-keep`, `-v`)

Passes `-nvvm-version=nvvm70` to optimizer.

### Compilation Orchestrator — `sub_12C35D0`

| Field | Value |
|---|---|
| Address | `0x12C35D0` |
| Size | 41KB |
| Stages | 4 (LNK, OPT, OPTIXIR, LLC) |

| Stage | Timer String | Purpose |
|---|---|---|
| LNK | `"LibNVVM module linking step."` | Link IR modules + libdevice |
| OPT | `"LibNVVM optimization step."` | Run LLVM optimizer pipeline |
| OPTIXIR | `"LibNVVM Optix IR step."` | OptiX IR generation (optional) |
| LLC | `"LibNVVM code-generation step."` | Backend codegen → PTX |

Each stage uses `sub_16D8B50` for timing and supports optional progress callback.

### Module Linker — `sub_12C06E0`

| Field | Value |
|---|---|
| Address | `0x12C06E0` |
| Size | 63KB |

Validates:
- Triple must start with `nvptx64-`
- IR version compatibility: `"incompatible IR detected. Possible mix of compiler/IR from different releases."`
- Symbol size matching across modules

### NVVM IR Version Checker — `sub_12BFF60`

Reads `"nvvmir.version"` metadata. Checks `NVVM_IR_VER_CHK` environment variable for version override. Validates `"llvm.dbg.cu"` debug info presence.

## Architecture Detection — `sub_95EB40`

| Field | Value |
|---|---|
| Address | `0x95EB40` |
| Size | 38KB |
| Architectures | 30 (compute_75 through compute_121f) |
| Triple | `nvptx64-nvidia-cuda` (hardcoded at `0x3f0f5cd`) |

Maps `-arch=compute_XX` to three independent flag columns:

| Architecture | `__CUDA_ARCH` | `-opt-arch=` | `-mcpu=` |
|---|---|---|---|
| `compute_75` | 750 | `sm_75` | `sm_75` |
| `compute_80` | 800 | `sm_80` | `sm_80` |
| `compute_86` | 860 | `sm_86` | `sm_86` |
| `compute_87` | 870 | `sm_87` | `sm_87` |
| `compute_88` | 880 | `sm_88` | `sm_88` |
| `compute_89` | 890 | `sm_89` | `sm_89` |
| `compute_90` | 900 | `sm_90` | `sm_90` |
| `compute_90a` | 900 | `sm_90a` | `sm_90a` |
| `compute_100` | 1000 | `sm_100` | `sm_100` |
| `compute_100a` | 1000 | `sm_100a` | `sm_100a` |
| `compute_100f` | 1000 | `sm_100f` | `sm_100f` |
| `compute_103` | 1030 | `sm_103` | `sm_103` |
| `compute_103a` | 1030 | `sm_103a` | `sm_103a` |
| `compute_103f` | 1030 | `sm_103f` | `sm_103f` |
| `compute_110` | 1100 | `sm_110` | `sm_110` |
| `compute_110a` | 1100 | `sm_110a` | `sm_110a` |
| `compute_110f` | 1100 | `sm_110f` | `sm_110f` |
| `compute_120` | 1200 | `sm_120` | `sm_120` |
| `compute_120a` | 1200 | `sm_120a` | `sm_120a` |
| `compute_120f` | 1200 | `sm_120f` | `sm_120f` |
| `compute_121` | 1210 | `sm_121` | `sm_121` |
| `compute_121a` | 1210 | `sm_121a` | `sm_121a` |
| `compute_121f` | 1210 | `sm_121f` | `sm_121f` |

Also maps general flags: `-g`, `-generate-line-info`, `-opt=0/1/2/3`, `-Osize`, `-Om`, `-ftz`, `-prec-sqrt`, `-prec-div`, `-fma`, `-unsafe-math`, `-fast-math`, `-disable-inlining`, `-aggressive-inline` (budget=40000), `-restrict`, `-new-nvvm-remat`, `-disable-nvvm-remat`, `--emit-optix-ir`.

## NVVM Flag Catalog — `sub_9624D0`

| Field | Value |
|---|---|
| Address | `0x9624D0` |
| Size | 75KB |
| Unique flags | 142 |
| Output vectors | 4 (lnk, opt, lto, llc) |

### Phase Routing

| Phase ID | Name | Option Vector |
|---|---|---|
| 1 | LNK (linker) | `-lnk` flags |
| 2 | OPT (optimizer) | `-opt` flags |
| 3 | LLC (codegen) | `-llc` flags |
| 4 | LibNVVM | All phases |

### Key Flag Categories

| Category | Example Flags |
|---|---|
| **Architecture** | `-arch=compute_XXX` (validated against bitmask `0x60081200F821`) |
| **Math precision** | `-ftz`, `-prec-sqrt`, `-prec-div`, `-fma`, `-opt-fdiv`, `-unsafe-math` |
| **Optimization** | `-Ofast-compile=0/min/mid/max` (stored at offset+1640) |
| **Custom pipeline** | `-opt-passes=<pipeline>` (stored at offset+1512) |
| **Inlining** | `-disable-inlining`, `-inline-budget=40000`, `-aggressive-inline`, `-inline-info` |
| **LTO** | `-lto` (0x23), `-gen-lto` (0x21), `-gen-lto-and-llc`, `-link-lto` (0x26) |
| **Pass-through** | `-Xopt`, `-Xllc`, `-Xlnk`, `-Xlto` |
| **Register limit** | `-maxreg=N` (offset+1192, forwarded to opt+llc) |
| **Split compile** | `-split-compile=N` (offset+1480) |
| **OptiX** | `--emit-optix-ir` (disables ip-msp and licm) |

## NVVM Builtin Resolution — `sub_90AEE0`

| Field | Value |
|---|---|
| Address | `0x90AEE0` |
| Size | 109KB |
| Builtins | 770 entries via `sub_90ADD0` / `sub_C92610` |

Pre-optimization builtin resolution table. Surface/texture builtins visible (e.g., `__nvvm_sust_b_2d_v2i64_zero`, ID 612). See [Builtin Table](../builtins/index.md) for the full inventory.

## Data Layout Strings

| Mode | Layout String |
|---|---|
| 64-bit (shared mem) | `e-p:64:64:64-p3:32:32:32-i1:8:8-...-n16:32:64` |
| 64-bit (no shared) | `e-p:64:64:64-i1:8:8-...-n16:32:64` |
| 32-bit | `e-p:32:32:32-i1:8:8-...-n16:32:64` |

`p3:32:32:32` = address space 3 (shared memory) uses 32-bit pointers even in 64-bit mode.

## Key Global Variables

| Variable | Purpose |
|---|---|
| `byte_4F6D280` | Wizard mode flag |
| `qword_4F6D2A0` | Flag mapping red-black tree root |
| `unk_3EA0080` | Embedded libdevice bitcode (455,876 bytes) |
| `off_4B90FE0` | LLVM options table (37 entries) |
| `qword_4F076F0` | Input source filename |
| `unk_4D045E8` | SM number (from `-arch`) |
| `unk_4D045E4` | `a` suffix flag |
| `unk_4D045E0` | `f` suffix flag |
