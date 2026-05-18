# Compilation Pipeline Overview

This page maps the complete end-to-end flow of a CUDA compilation through cicc v13.0, from the initial CLI invocation to the final PTX text output. Each stage is a self-contained subsystem with its own address range, data structures, and failure modes. The links below lead to dedicated pages with reimplementation-grade detail for every stage.

## Pipeline Diagram

```
nvcc
  |
  v
+===========================================================+
| cicc (60 MB, 80,562 functions)                            |
|                                                           |
|  1. CLI Parsing & Dispatch -----> [entry.md]              |
|     |  argv/envp, flag translation, arch detection        |
|     |  dual-path select: Path A (LibNVVM) / Path B        |
|     v                                                     |
|  2. nvcc-to-cicc Interface -----> [nvcc-interface.md]     |
|     |  flag tree (40+ mappings), 3-column arch fan-out    |
|     |  mode cookies: 0xABBA=CUDA, 0xDEED=OpenCL          |
|     v                                                     |
|  3. EDG 6.6 Frontend -----------> [edg.md]                |
|     |  CUDA C++ --> transformed C (.int.c/.device.c)      |
|     |  737 config #defines, GCC 8.1 / Clang 9.1 emu      |
|     v                                                     |
|  4. NVVM IR Generation ---------> [ir-generation.md]      |
|     |  EDG IL tree --> LLVM Module (NVVM IR)              |
|     |  address spaces, kernel metadata, builtins          |
|     v                                                     |
|  5. Libdevice Linking ----------> [../infra/libdevice-linking.md]
|     |  embedded 455KB bitcode, 352 __nv_* math fns        |
|     |  target triple validation, NVVM version check       |
|     v                                                     |
|  6. LLVM Optimizer -------------> [optimizer.md]          |
|     |  two-phase model (analysis -> codegen-oriented)     |
|     |  49.8KB pipeline assembler, ~150 pass insertions    |
|     |  concurrent per-function Phase II                   |
|     v                                                     |
|  7. LTO Pipeline ---------------> [../lto/index.md]       |
|     |  cross-TU inlining, devirt, GlobalOpt               |
|     |  closed-world GPU model: no dlopen, no .so          |
|     v                                                     |
|  8. Code Generation ------------> [codegen.md]            |
|     |  SelectionDAG, ISel, RegAlloc, MachineIR passes     |
|     |  37 MB of code, largest subsystem                   |
|     v                                                     |
|  9. PTX Emission ---------------> [emission.md]           |
|     |  .entry/.func headers, register decls, .loc/.file   |
|     |  AsmPrinter, GenericToNVVM addrspace rewrite        |
|     v                                                     |
|  OUTPUT: .ptx file (or NVVM bitcode, or OptiX IR)        |
+===========================================================+

Side paths:
  * OptiX IR (--emit-optix-ir) ----> [optix-ir.md]
  * Debug info (all stages) -------> [debug-info-pipeline.md]
```

## Stage Descriptions

### 1. Entry Point & CLI Parsing

The real `main` (`sub_8F9C90`, 10KB) parses `argv`, detects wizard mode via `NVVMCCWIZ=553282`, selects the target architecture (default `sm_75`), and dispatches into one of two compilation paths. Path A serves the LibNVVM API; Path B serves standalone `nvcc` invocations. Both paths are functionally identical but duplicated in the binary at different address ranges. See [Entry Point & CLI](./entry.md).

### 2. nvcc-to-cicc Interface

The flag translation layer (`sub_8FE280`) rewrites nvcc-facing flags into cicc-facing flags through a `std::map` red-black tree, then a second stage (`sub_95EB40`) fans each flag out into three columns targeting EDG, OPT, and LLC separately. Mode cookies (`0xABBA` for CUDA, `0xDEED` for OpenCL) select language-specific behavior. See [nvcc-to-cicc Interface](./nvcc-interface.md).

### 3. EDG 6.6 Frontend

A licensed commercial frontend (3.2 MB, `0x5D0000`--`0x8F0000`) parses CUDA C++ source and emits transformed C code into `.int.c`, `.device.c`, and `.stub.c` files. CUDA syntax (`<<<>>>`, `__shared__`, `__device__`) is fully resolved in this stage. The output is C source, not LLVM IR. See [EDG 6.6 Frontend](./edg.md).

### 4. NVVM IR Generation

Translates the EDG intermediate language (IL) tree into an LLVM `Module` with proper NVPTX address space annotations, `nvvm.annotations` kernel metadata, and lowered builtins. This is cicc's equivalent of Clang's `lib/CodeGen`, but operates on EDG's proprietary IL node format. See [NVVM IR Generation](./ir-generation.md) and its sub-pages for [expressions](./irgen-expressions.md), [statements](./irgen-statements.md), [functions](./irgen-functions.md), and [types](./irgen-types.md).

### 5. Libdevice Linking

A 455,876-byte LLVM bitcode library containing 352 GPU-optimized math functions (`__nv_sinf`, `__nv_expf`, etc.) is embedded directly in the cicc binary. The linker validates the `nvptx64-` target triple, checks NVVM IR version metadata, and merges the library into the compilation module. No filesystem access is required. See [Libdevice Linking](../infra/libdevice-linking.md).

### 6. LLVM Optimizer

A proprietary two-phase pipeline (`sub_12E54A0`, 49.8KB) runs ~150 passes: Phase I performs module-wide analysis, Phase II performs codegen-oriented transforms with optional per-function parallelism using a jobserver or thread pool. All behavior is controlled by the 221-slot [NVVMPassOptions](../config/nvvm-pass-options.md) system. See [LLVM Optimizer](./optimizer.md) and [Pipeline & Ordering](../llvm/pipeline.md).

### 7. LTO Pipeline

Exploits the GPU's closed-world compilation model (no `dlopen`, no shared libraries, no symbol interposition) for aggressive cross-TU inlining, whole-program devirtualization, and global variable promotion. Activated in separate compilation mode (`nvcc -dc`), but GlobalOpt and the inliner run even in single-TU mode. See [LTO & Module Optimization](../lto/index.md).

### 8. Code Generation

The largest subsystem (37 MB, `0x1700000`--`0x35EFFFF`) lowers optimized LLVM IR to NVPTX `MachineInstr` through SelectionDAG construction, type legalization, instruction selection via a three-level pattern match engine (900KB), pressure-driven greedy register allocation, and ~30 machine-level passes including tensor core codegen for HMMA/IMMA/WGMMA/tcgen05. See [Code Generation](./codegen.md).

### 9. PTX Emission

The AsmPrinter (`sub_31EC4F0`, 72KB) walks the final `MachineFunction` and emits PTX text: `.entry`/`.func` headers with kernel attributes, register declarations for 9 register classes, `.loc`/`.file` debug directives, and instruction mnemonics. A `GenericToNVVM` pass rewrites any remaining generic address space references before emission. See [PTX Emission](./emission.md).

### Side Paths

**OptiX IR** -- When `--emit-optix-ir` is passed, the pipeline replaces LLC with an OPTIXIR stage that serializes the optimized LLVM module for the OptiX ray tracing runtime's continuation-based execution model. See [OptiX IR Generation](./optix-ir.md).

**Debug Info** -- Debug metadata flows through all stages: generated in IR-gen, preserved or stripped in the optimizer (5 stripping passes), verified after each pass, and emitted as `.loc`/`.file` PTX directives. See [Debug Info Pipeline](./debug-info-pipeline.md).

## Internal Pipeline Encoding

Internally, cicc represents the active pipeline stages as a bitmask:

| Stage | Internal Name | Bit | Description |
|---|---|---|---|
| LNK | Libdevice link | `0x01` | Merge embedded math library |
| OPT | Optimizer | `0x02` | LLVM IR optimization (Phase I + II) |
| OPTIXIR | OptiX IR | `0x40` | OptiX serialization (mutually exclusive with LLC) |
| LLC | Code generation | `0x04` | SelectionDAG through PTX emission |

The standard CUDA compilation bitmask is `LNK | OPT | LLC` = `0x07`. OptiX mode uses `0x43`.

## Cross-References

- [Binary Layout](../binary-layout.md) -- address ranges for every subsystem
- [Function Map](../function-map.md) -- master index of recovered function addresses
- [CLI Flags](../config/cli-flags.md) -- complete flag catalog
- [Optimization Levels](../config/optimization-levels.md) -- what changes at `-O0`/`-O1`/`-O2`/`-O3`
- [NVIDIA Custom Passes](../passes/index.md) -- 35 proprietary passes inserted into the LLVM pipeline
- [NVPTX Target Infrastructure](../infra/nvptx-target.md) -- TargetMachine, TTI, SubtargetFeatures
