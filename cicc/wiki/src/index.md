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
| **CLI options** | ~1,689 registered via `cl::opt` + 222 NVVMPassOptions slots |
| **NVVM builtins** | 770 (IDs 1–770, wyhash open-addressing table) |
| **Default target** | `sm_75` (Turing) |
| **Supported SMs** | sm\_75 through sm\_121f (Turing through Blackwell (sm120)) |

## Three Subsystems

CICC is not a monolithic compiler. It is composed of three largely independent subsystems, each with its own lineage, coding conventions, and internal data structures:

**1. EDG 6.6 C++ Frontend** (3.2 MB, `0x5D0000`–`0x8F0000`) — A licensed commercial frontend from Edison Design Group that parses CUDA C++ source code and emits transformed C code. It operates as a source-to-source translator: CUDA kernel launch syntax (`<<<>>>`) is lowered to CUDA runtime API calls, memory space qualifiers (`__shared__`, `__constant__`) are resolved to address space annotations, and C++ templates/constexpr are fully evaluated. The output is not LLVM IR — it is C code that feeds into a second compilation phase. See [EDG 6.6 Frontend](./pipeline/edg.md).

**2. NVVM Bridge** (~4 MB, `0x8F0000`–`0x12CFFFF`) — The glue layer between EDG and LLVM. It handles CLI parsing, architecture detection (23 SM variants with 3-column flag fan-out), the dual-path compilation dispatch (Path A via LibNVVM API, Path B standalone), the NVVMPassOptions knob system (221 per-pass configuration slots), and the 770-entry builtin resolution table. This layer is entirely NVIDIA-proprietary. See [Entry Point & CLI](./pipeline/entry.md) and [LLVM Optimizer](./pipeline/optimizer.md).

**3. LLVM 20.0.0 Backend** (~45 MB, `0x12D0000`–`0x3BFFFFF`) — A heavily modified LLVM fork that performs IR optimization and PTX code generation. NVIDIA has added 35 custom passes (MemorySpaceOpt, Rematerialization, BranchDist, LoopIndexSplit, Sinking2, etc.), a proprietary two-phase compilation model with per-function thread parallelism, and extensive modifications to the NVPTX backend for tensor core code generation across 5 GPU architecture generations. See [Code Generation](./pipeline/codegen.md) and [PTX Emission](./pipeline/emission.md).

Additionally, **jemalloc 5.3.x** (~400 functions at `0x12FC000`) is statically linked, replacing the system allocator for improved memory allocation performance during compilation.

## Dual-Path Architecture

A distinctive feature of cicc is its **dual-path design** — two complete copies of the compilation backend exist within the same binary, selected at runtime:

| | Path A (`0x90xxxx`) | Path B (`0x126xxxx`) |
|---|---|---|
| **Purpose** | LibNVVM API mode | Standalone mode |
| **Simple compile** | `sub_902D10` | `sub_1262860` |
| **Multi-stage** | `sub_905EE0` (43KB) | `sub_1265970` (48KB) |
| **CLI parsing** | `sub_900130` | `sub_125FB30` |
| **Builtin table** | `sub_90AEE0` (109KB) | `sub_126A910` (123KB) |
| **Libdevice** | `unk_3EA0080` (455KB) | `unk_420FD80` (455KB) |
| **Version string** | `-nvvm-version=nvvm-latest` | `-nvvm-version=nvvm70` |

Runtime selection is controlled by `v253` in `sub_8F9C90` (the real main function). The default value (2) triggers an environment variable lookup through an obfuscated string comparison to determine which path to take. This design allows a single binary to serve both the `nvcc` driver toolchain and the LibNVVM runtime compilation API.

## Compilation Pipeline

Both paths converge on the same 5-stage pipeline:

```
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

## Reading This Wiki

The wiki is organized around the compilation pipeline:

- **[Compilation Pipeline](./pipeline/entry.md)** — Start here. Entry point, CLI, dual-path dispatch, then follow through [EDG](./pipeline/edg.md) → [IR Generation](./pipeline/ir-generation.md) → [Optimizer](./pipeline/optimizer.md) → [Codegen](./pipeline/codegen.md) → [Emission](./pipeline/emission.md).
- **[NVIDIA Custom Passes](./passes/index.md)** — The 35 proprietary passes not found in upstream LLVM: MemorySpaceOpt, Rematerialization, BranchDist, etc.
- **[NVVM Builtins](./builtins/index.md)** — The 770-entry builtin table: hash table structure, complete ID inventory, category breakdown.
- **[GPU Targets](./targets/index.md)** — SM feature gates, architecture detection, 45 SM variants from sm\_20 to sm\_121f.
- **[Configuration](./config/knobs.md)** — Three knob systems: ~1,689 `cl::opt` flags, 222 NVVMPassOptions slots, ~70 codegen knobs.
- **[Function Map](./function-map.md)** — Address-to-identity lookup for ~350 key functions with confidence levels.
