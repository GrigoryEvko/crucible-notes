# cudafe++ v13.0 -- Reverse Engineering Reference

`cudafe++` is NVIDIA's CUDA frontend compiler -- the first stage of the CUDA compilation pipeline. It is built on the Edison Design Group (EDG) C++ Front End v6.6, a commercial compiler frontend licensed by compiler vendors worldwide. NVIDIA ships `cudafe++` as a statically-linked, stripped ELF binary inside every CUDA Toolkit installation. This binary accepts `.cu` source files, parses them as C++ with CUDA extensions, separates device code from host code, and produces two outputs: an EDG Intermediate Language (IL) stream consumed by `cicc` (the NVIDIA PTX code generator), and a transformed `.int.c` host file consumed by the system C++ compiler (gcc, clang, or cl.exe).

This wiki documents the complete internals of the `cudafe++` binary from CUDA Toolkit 13.0, reverse-engineered through static analysis (IDA Pro + Hex-Rays decompilation) of all 6,483 functions. The goal is reimplementation-grade documentation: every page should give a senior compiler engineer enough information to build equivalent functionality from scratch.

## Binary Identity

| Property | Value |
|---|---|
| Binary | `cudafe++` from CUDA Toolkit 13.0 |
| Format | ELF 64-bit LSB executable, x86-64, statically linked, stripped |
| File size | 8,910,936 bytes (8.5 MB) |
| EDG base | Edison Design Group C++ Front End v6.6 |
| Build path | `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/` |
| Total functions | 6,483 |
| Functions mapped to source | 2,208 (34%) |

## Segment Layout

| Section | Start | End | Size | Description |
|---|---|---|---|---|
| `.text` | `0x403300` | `0x829722` | 4,351,010 bytes (4.15 MB) | Executable code |
| `.rodata` | `0x829740` | `0xAA3FA3` | 2,599,011 bytes (2.48 MB) | Read-only data (string tables, jump tables, constants) |
| `.data` | `0xD46480` | `0xE7EFF0` | 1,280,880 bytes (1.22 MB) | Initialized global variables |
| `.bss` | `0xE7F000` | `0x12D6F20` | 4,554,528 bytes (4.34 MB) | Zero-initialized globals |
| `.eh_frame` | `0xCB1210` | `0xD3F398` | 582,024 bytes | Exception handling unwind tables |
| `.data.rel.ro` | `0xD428C0` | `0xD45E00` | 13,632 bytes | Relocation-read-only (vtables, GOT-relative) |

## Role in the CUDA Toolchain

```
  input.cu
     |
     v
 cudafe++  ──────── THIS BINARY ────────
     |                                   |
     v                                   v
  device.gpu  (EDG IL)             input.int.c  (transformed host C++)
     |                                   |
     v                                   v
   cicc                            gcc / clang / cl.exe
     |                                   |
     v                                   v
  device.ptx                       host.o
     |                                   |
     v                                   v
   ptxas                              ld
     |                                   |
     v                                   v
  device.cubin ──────────────────> final executable
```

`cudafe++` is a source-to-source compiler. It never generates machine code directly. Its job is to take a single `.cu` translation unit, understand which code is device (`__device__`, `__global__`) and which is host, then:

1. **For the device track:** Emit EDG IL -- a typed, scope-linked intermediate representation containing every declaration, type, expression, and statement. This IL is consumed by `cicc`, which lowers it through LLVM to PTX assembly.

2. **For the host track:** Emit a `.int.c` file -- valid C++ source where device function bodies are suppressed inside `#if 0`/`#endif`, `__global__` kernels are replaced by `__wrapper__device_stub_<name>()` forwarding functions, and CUDA runtime registration boilerplate is appended.

The binary runs as a single-threaded, single-pass-per-stage pipeline with 8 stages: pre-init, CLI parsing (276 flags), one-time init (38 subsystem initializers), TU state reset, frontend parse (EDG parser + CUDA extensions), 5-pass IL finalization, backend `.int.c` emission, and exit. See [Pipeline Overview](./pipeline/overview.md) for the full stage diagram.

## Source Attribution

The binary embeds `__FILE__` strings from the EDG build system, revealing the original source file structure. From these strings plus address-range analysis of decompiled code, 52 `.c` source files and 13 `.h` header files have been identified:

| Category | Files | Functions Mapped | Description |
|---|---|---|---|
| EDG core parser | 15 `.c` | ~800 | Lexer, expression/declaration parser, statement handling |
| EDG type system | 6 `.c` | ~350 | Type representation, checking, conversion |
| EDG templates | 5 `.c` | ~300 | Template parsing, instantiation, deduction |
| EDG IL subsystem | 8 `.c` | ~250 | IL node types, allocation, walking, display, comparison |
| EDG infrastructure | 12 `.c` | ~400 | Memory management, error handling, name mangling, scope management |
| EDG code generation | 3 `.c` | ~150 | Backend `.int.c` emission, ASM handling |
| NVIDIA additions | 3 `.c` | ~110 | CUDA transforms, attribute validation, lambda wrappers |
| Headers | 13 `.h` | (inline) | Shared constants, struct layouts, macro definitions |

The NVIDIA-specific source files are:

- **`nv_transforms.c`** (~34 functions, ~14 KB of `.text`): The heart of CUDA support. Implements device/host-device lambda wrapper template generation (`__nv_dl_wrapper_t`, `__nv_hdl_wrapper_t`, `__nv_hdl_create_wrapper_t`), CUDA attribute validation (`__launch_bounds__`, `__cluster_dims__`, `__block_size__`, `__maxnreg__`), host reference array emission (`.nvHRKI`/`.nvHRDE`/`.nvHRCE` ELF sections), lambda preamble injection (`sub_6BCC20`), and array capture helper generation.

- **`nv_transforms.h`**: Header with NVIDIA-specific declarations, type trait template names, and bitmask table definitions.

- **3 modified EDG files**: `cmd_line.c` (CUDA CLI flags spliced into EDG's flag table), `fe_init.c` (CUDA-specific initialization at stage 3), and `cp_gen_be.c` (device stub generation, lambda wrapper emission, registration table output in the backend).

## Key Discoveries

All entries in this section carry an evidence rating using the scheme defined in [Methodology](./methodology.md): **HIGH** = decompiled handler logic + string evidence, **MED** = consistent cross-references but indirect, **LOW** = inferred from patterns.

### Execution Space Bitfield (HIGH)

Every entity node in the EDG IL carries CUDA execution-space information at byte offset +182 (relative to the entity node base). The bitfield encoding:

| Bit | Mask | Meaning |
|---|---|---|
| 4-5 | `0x30` | Execution space: 0=none, 1=`__host__`, 2=`__device__`, 3=`__host__ __device__` |
| 6 | `0x40` | Device/global flag (set for `__device__` and `__global__` functions) |
| 7 | `0x80` | `__global__` kernel flag |

This bitfield is checked throughout the pipeline -- in cross-space call validation, device/host code separation, the keep-in-IL predicate, and backend stub generation.

### Lambda Wrapper Template Injection (HIGH)

CUDA extended lambdas (`__device__` and `__host__ __device__` lambdas) cannot be passed directly across the host/device boundary. `cudafe++` solves this by injecting a library of template wrapper structs into the compilation at backend time. The master emitter `sub_6BCC20` (`nv_emit_lambda_preamble`) generates all `__nv_*` templates in a single function call, driven by two 1024-bit bitmasks that record which capture counts were actually needed during parsing:

- `unk_1286980`: Device lambda capture counts (bit N = need `__nv_dl_wrapper_t` for N captures)
- `unk_1286900`: Host-device lambda capture counts (need `__nv_hdl_wrapper_t` for N captures)

Only the required specializations are emitted, keeping the generated code minimal.

### CUDA Error Catalog (HIGH)

The binary contains 3,795 diagnostic messages in the EDG error table. Of these, 338 are CUDA-specific (error numbers in the 20000+ range and the 3500-3800 range). These cover:

- Execution space violations (calling `__device__` from `__host__` and vice versa)
- `__global__` function constraints (no return value, no variadic args, no virtual)
- Lambda restrictions (35+ distinct error categories for extended lambda misuse)
- Attribute conflicts (`__launch_bounds__` + `__maxnreg__` mutual exclusion)
- RDC mode restrictions (user-defined copy constructors in kernel arguments)
- Architecture feature gates (feature X requires SM_YY or higher)

### IL Entry Kind System (HIGH)

The EDG IL uses 85 defined entry kinds (0-84), each representing a distinct node type in the typed, scope-linked IL graph. Key node types include: `routine` (288 bytes, functions/methods), `variable` (232 bytes), `type` (176 bytes, 22 sub-kinds), `expr_node` (72 bytes, 36 sub-kinds), `statement` (80 bytes, 26 sub-kinds), and `scope` (288 bytes, 9 sub-kinds). All nodes live in a region-based arena allocator with 64 KB blocks. See [IL Overview](./il/overview.md) for the complete entry kind table.

### CLI Flag Inventory (HIGH)

`cudafe++` accepts 276 command-line flags parsed in `sub_459630` (`cmd_line.c`). These control:

- Language mode and C++ standard version (`__cplusplus` value)
- Host compiler identity (MSVC, GCC, Clang) and version
- CUDA-specific modes: extended lambdas, RDC, JIT, architecture target
- Diagnostic suppression and promotion
- Include paths and macro definitions
- Output format and timing

Flags are passed from `nvcc` via the `-Xcudafe` forwarding mechanism. Many flags are undocumented EDG internals.

## Wiki Structure

This wiki is organized into 10 sections covering the binary from top-level pipeline down to individual data structures.

### Overview

- [Function Map](./function-map.md) -- address-to-identity table for all 2,208 mapped functions
- [Binary Layout](./binary-layout.md) -- segment map, memory regions, address space organization
- [Methodology](./methodology.md) -- RE tools, approach, confidence scoring, and the [Terminology](./methodology.md#terminology) glossary (IDA anchors, EDG vocabulary, CUDA additions)

### [Compilation Pipeline](./pipeline/overview.md)

The 8-stage pipeline from `main()` at `0x408950` through exit. Covers initialization, CLI parsing, EDG frontend invocation, 5-pass IL finalization, backend `.int.c` emission, and exit code mapping.

### CUDA Execution Model

How `cudafe++` handles `__device__`, `__host__`, and `__global__` execution spaces. Device/host code separation, cross-space call validation, kernel stub generation, RDC (relocatable device code) mode, JIT mode, and SM architecture feature gating.

### CUDA Attributes

The internal attribute system: `__global__` function constraints, `__launch_bounds__` / `__cluster_dims__` / `__block_size__` / `__maxnreg__` validation, `__grid_constant__` parameter handling, `__managed__` variable support, and minor attributes (`__nv_pure__`, `__nv_register_params__`).

### Lambda Transformations

Extended lambda support architecture: device lambda wrapper (`__nv_dl_wrapper_t`), host-device lambda wrapper (`__nv_hdl_wrapper_t` / `__nv_hdl_create_wrapper_t`), capture handling (field types, array wrappers for up to 8D), preamble injection (`sub_6BCC20`), and the 35+ lambda restriction error categories.

### [EDG Intermediate Language](./il/overview.md)

The 85-entry-kind IL format: node allocation (region-based arena), tree walking (5 callback traversal), [device code selection](./il/keep-in-il.md) (keep-in-IL predicate), display (debug dump), and comparison/copy operations.

### Host Output Generation

The `.int.c` file format, CUDA runtime boilerplate (`__nv_managed_rt` initialization, `crt/host_runtime.h` inclusion), host reference arrays (`.nvHRKI`/`.nvHRDE`/`.nvHRCE` ELF sections for device symbol registration), and CRC32-based module ID generation.

### EDG Frontend Internals

The stock EDG 6.6 subsystems: lexer/tokenizer (357 token kinds), expression parser, declaration parser, overload resolution, template engine (instantiation worklist), CUDA-specific template restrictions, constexpr interpreter, Itanium ABI name mangling with CUDA extensions, and the type system (176-byte type node, 22 type kinds).

### Error & Diagnostic System

The 3,795-entry diagnostic table, CUDA-specific error catalog (338 entries), format specifier system (`%t`/`%s`/`%n`/`%sq`/`%p`/`%d`), and SARIF output / pragma control.

### Data Structures

Byte-level layouts for the core IL node types: entity node (execution/memory space at +182), scope entry (784 bytes), translation unit descriptor (424 bytes), type node (176 bytes, 22 kinds), and template instance record (128 bytes).

### Configuration

CLI flag inventory (276 flags by category), EDG build configuration (compile-time constants baked into the binary), architecture detection (`--nv_arch` and SM version mapping), and experimental feature flags.

### Reference

EDG source file map (52 `.c` + 13 `.h`), global variable index, token kind table (357 types), full error message catalog, and virtual override mismatch matrix.

## Navigating This Wiki

**If you want to understand the compilation pipeline:** Start with [Pipeline Overview](./pipeline/overview.md), then follow the stage-by-stage links.

**If you want to understand CUDA-specific behavior:** Start with the CUDA Execution Model section. The execution spaces page explains the fundamental bitfield encoding that everything else depends on.

**If you want to understand lambda transformations:** Start with the Lambda Transformations overview. Lambda support is the most complex NVIDIA addition and involves template injection, capture-count bitmasks, and 5 distinct wrapper template families.

**If you want to understand the IL format:** Start with [IL Overview](./il/overview.md) for the 85 entry kinds, then [Keep-in-IL](./il/keep-in-il.md) for how device code is selected.

**If you want to look up a specific function:** The Function Map provides address-to-identity mappings for all 2,208 identified functions. The EDG Source File Map shows which source file each address range belongs to.

## Data Sources

This wiki is derived from:

- **6,202 Hex-Rays decompiled C pseudocode files** -- one per function with recognizable control flow
- **6,342 x86-64 disassembly files** -- full instruction-level coverage
- **9.5 MB strings database** with cross-references to every function that uses each string
- **161 MB cross-reference database** -- complete caller/callee and data-reference mappings
- **7.7 MB call graph** in JSON and DOT format
- **6,483 control flow graphs** with basic block boundaries
- **247 MB IDA Pro database** (.i64)

All analysis was performed on the binary shipped with CUDA Toolkit 13.0, obtained from NVIDIA's public distribution channels.

## Top-Level Pre-Wiki Docs Status

The `cudafe++/` directory contains nine `*.md` files that predate this wiki. They were the initial bulk-extraction reports and design notes; the wiki is the canonical successor. Status of each (confidence HIGH unless noted):

| File | Status | Canonical wiki location |
|---|---|---|
| `README.md` | **Keep** -- repository landing page, links into wiki | -- |
| `wiki_design.md` | **Keep (historical)** -- original MkDocs design sketch, superseded by the mdBook `SUMMARY.md` | [`SUMMARY.md`](./SUMMARY.md) |
| `ARCHITECTURE_MATRIX.md` | **Migrate or delete** -- SM_30/60/70/90 feature table | [`cuda/arch-gating.md`](./cuda/arch-gating.md), [`config/arch-detection.md`](./config/arch-detection.md) |
| `COMPILER_FLAGS.md` | **Migrate raw data** -- 515-flag catalog (auto-generated dump) | [`config/cli-flags.md`](./config/cli-flags.md), [`config/edg-build-config.md`](./config/edg-build-config.md) |
| `CUDA_ATTRIBUTES.md` | **Migrate raw data** -- 29 attributes + 114 `__nv_*` intrinsics | [`attributes/overview.md`](./attributes/overview.md), [`attributes/nv-builtin-intrinsics.md`](./attributes/nv-builtin-intrinsics.md), [`attributes/minor-attributes.md`](./attributes/minor-attributes.md) |
| `OPTIMIZATION_GUIDE.md` | **Delete (out of scope)** -- developer-facing CUDA tuning guide, not binary analysis | -- |
| `INTEGRATION_SUMMARY.md` | **Delete** -- changelog for the deleted `wiki/docs/` MkDocs tree (commit 444e4b9a9bc) | -- |
| `FORMATTING_REPORT.md` | **Delete** -- one-shot script report for the deleted `wiki/docs/` tree | -- |
| `REVERSE_COMPILATION_ANALYSIS.md` | **Migrate residual content** -- pipeline, lambda, IL, output sections largely covered; "Reverse Compilation Feasibility" and "Alternative Compiler Roadmap" sections are unique | [`pipeline/overview.md`](./pipeline/overview.md), [`il/overview.md`](./il/overview.md), [`lambda/overview.md`](./lambda/overview.md), [`output/int-c-format.md`](./output/int-c-format.md) |

The README link list above is the migration roadmap. Each non-Keep file carries a banner pointing to its canonical wiki page; full migration and removal is tracked separately.
