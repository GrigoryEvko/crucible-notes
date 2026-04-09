# nvlink Reverse Engineering Reference

This wiki documents the internal architecture of **nvlink v13.0.88**, NVIDIA's CUDA device linker, based on static reverse engineering of the stripped x86-64 ELF binary. nvlink is shipped as part of **CUDA Toolkit 13.0** (release V13.0.88, build `cuda_13.0.r13.0/compiler.36424714_0`, August 2025).

| | |
|---|---|
| **Binary** | nvlink v13.0.88, 37 MB, x86-64, stripped ELF |
| **Build** | `cuda_13.0.r13.0/compiler.36424714_0` (Wed Aug 20, 2025) |
| **Functions** | 40,532 total, 552K call edges |
| **Strings** | 31K extracted |
| **Tool identity** | `NVIDIA (R) Cuda linker` |
| **Copyright** | 2005--2025 NVIDIA Corporation |
| **Supported SM** | 22 architectures: sm\_75 (Turing) through sm\_121 (Blackwell) |
| **Input formats** | cubin, PTX, fatbin, NVVM IR / LTO IR, archives (.a), host ELF (.o/.so) |
| **Output formats** | CUDA device ELF (cubin), capsule mercury (SM100+), host linker scripts |

**Target audience**: Senior C++ developers with ELF/linker experience seeking reimplementation-grade understanding of NVIDIA's device linking pipeline.

## What nvlink Is

nvlink is the CUDA device linker -- the tool that combines separately compiled GPU object files into a single device executable. It is invoked by `nvcc` (or directly by build systems) after `cicc` and `ptxas` have compiled individual translation units into cubin objects. nvlink resolves cross-TU symbol references, applies R\_CUDA relocations, merges `.nv.info` metadata, lays out shared memory, eliminates dead code, and emits the final device ELF.

But nvlink is not just a linker. It is a **hybrid linker-compiler**: a large fraction of the binary is an embedded GPU compiler backend that enables link-time optimization and JIT compilation of PTX and NVVM IR inputs. The tool name says "linker," but the binary contains a full assembler.

## The 95/5 Split

The single most important structural fact about nvlink is its size distribution:

| Component | Approximate size | Function count | Role |
|---|---|---|---|
| Embedded ptxas backend | ~24 MB (65%) | ~20,000 | PTX-to-SASS assembler, instruction selection, register allocation, scheduling, encoding |
| Instruction encoding tables | ~8 MB (22%) | ~8,000 | Per-SM binary encoding/decoding for SASS instructions |
| Linker core | ~1.2 MB (3%) | ~600 | ELF merge, symbol resolution, relocation, layout, output |
| LTO orchestration | ~1.5 MB (4%) | ~800 | IR collection, libnvvm dlopen, PTX compilation dispatch |
| Infrastructure | ~1.5 MB (4%) | ~1,100 | Memory arenas, option parsing, error reporting, compression |
| Mercury / FNLZR | ~0.8 MB (2%) | ~400 | SM100+ capsule mercury post-link transformation |

Approximately **95% of the binary is compiler backend** -- the same ptxas assembler that ships as a separate tool in the CUDA Toolkit, statically linked into nvlink to support LTO and PTX JIT compilation. Only about **5% (~1.2 MB) is the actual linker**. This has a direct consequence for reverse engineering: most functions in the binary are instruction selection patterns, SASS encoders, or register allocator internals, not linker logic.

## Architecture Overview

nvlink operates in a linear pipeline with two optional compiler paths:

```
Input Files (cubin, PTX, fatbin, NVVM IR, archives, host ELF)
  |
  +-- [File type detection: 56-byte header read, magic number dispatch]
  |
  +-- PTX input?  --> embedded ptxas JIT: PTX -> SASS cubin
  |
  +-- NVVM IR / LTO IR?  --> dlopen libnvvm.so, compile IR -> PTX,
  |                          then embedded ptxas: PTX -> SASS cubin
  |
  +-- fatbin?  --> extract members, recurse per-member
  |
  +-- archive (.a)?  --> iterate members, recurse
  |
  v
Merge Phase (merge_elf: 89KB function, iterates all sections)
  |
  +-- Symbol resolution, weak symbol selection
  +-- Section merging (.nv.global, .nv.shared, .nv.constant, .text)
  +-- Debug section merging (DWARF line tables, .nv.info)
  |
  v
Shared Memory Layout (65KB function, overlap set analysis)
  |
  v
Entry Property Computation (98KB function, register/barrier propagation)
  |
  v
Dead Code Elimination (callgraph reachability, removes unused functions)
  |
  v
Data Layout Optimization (constant dedup, overlapping data merge)
  |
  v
Layout Phase (section ordering, address assignment)
  |
  v
Relocation Phase (R_CUDA relocation application, UFT/UDT resolution)
  |
  v
Finalization (final relocation patching, ELF section generation)
  |
  v
Output (ELF serialization -> cubin file)
  |
  +-- SM100+?  --> FNLZR post-link transform -> capsule mercury
  +-- --gen-host-linker-script?  --> write SECTIONS { .nvFatBinSegment ... }
```

### LTO Model

When invoked with `-lto`, nvlink performs link-time optimization:

1. **Collect**: Gather NVVM IR modules from all input objects and fatbin members
2. **Compile IR to PTX**: dlopen `libnvvm.so` (path from `--nvvmpath`), invoke the NVVM compiler API to lower IR to PTX. Supports both whole-program and partial LTO modes (`--force-whole-lto`, `--force-partial-lto`)
3. **Assemble PTX to SASS**: Feed the resulting PTX through the embedded ptxas backend. Supports split compilation with thread pool parallelism (`--split-compile-extended`)
4. **Link**: Merge the resulting cubin objects through the normal linking pipeline

The LTO pipeline reuses option forwarding (`--Xptxas`, `--Xnvvm`) to pass flags through to the embedded compiler stages.

### Mercury

Mercury is NVIDIA's internal codename for a new ISA format used on SM100+ (Blackwell and later) architectures. When `--arch sm_100` or higher is specified, nvlink:

- Sets an internal mercury mode flag (byte at global `+0x222`)
- Routes output through the FNLZR (Finalizer) post-link binary rewriter
- Produces "capsule mercury" output instead of traditional cubin
- Uses R\_MERCURY relocations alongside R\_CUDA relocations
- Generates `.nv.merc` and related mercury-specific ELF sections

The Mercury codename appears ROT13-encoded throughout the ptxas backend as `zrephel` (Mercury), part of NVIDIA's standard obfuscation of internal pass names and section identifiers.

### Supported Architectures

nvlink supports 22 SM architectures spanning five GPU generations:

| Generation | SM versions | Notes |
|---|---|---|
| Turing | sm\_75 | Minimum supported architecture |
| Ampere | sm\_80, sm\_86, sm\_87, sm\_89 | sm\_89 is Ada Lovelace (different die) |
| Hopper | sm\_90, sm\_90a | Cluster launch, WGMMA, TMA |
| Blackwell | sm\_100, sm\_100a, sm\_103, sm\_103a | Mercury format, capsule output |
| Next-gen | sm\_110, sm\_120, sm\_121 | Jetson Thor, consumer Blackwell, DGX Spark |

Architecture selection (`--arch`) gates: instruction encoding tables, relocation types, Mercury vs. SASS output path, FNLZR compatibility checks, and ptxas backend ISel dispatch via per-SM vtables.

## Key Data Structures

The linker's internal state revolves around a few central structures:

- **elfw (ELF wrapper)**: The primary data structure for both input and output ELF objects. Created by `elfw_create` (0x4438F0), contains sections, symbols, relocations, string tables. The merge phase copies sections between elfw objects.
- **Memory arenas**: Custom allocator (`arena_alloc` at 0x4307C0) used by nearly every function. Thread-safe, size-class-based free lists (625 buckets), large-block page pool. Named arenas ("nvlink option parser", "nvlink memory space", "elfw memory space") for diagnostics.
- **Callgraph**: Directed graph of function-to-function calls, built during merge and used for dead code elimination, register count propagation, and stack size computation.
- **Architecture profile**: Per-SM configuration record accessed from a global profile database in the 0x470000 region. Gates feature availability, encoding table selection, and compatibility checks.

## Binary Layout (Address Map)

| Address range | Size | Subsystem |
|---|---|---|
| `0x400000`--`0x470000` | 448 KB | main(), CLI options, memory arenas, ELF core, merge, layout, relocation, output |
| `0x470000`--`0x530000` | 768 KB | Architecture profiles, finalization, debug info, archive parsing, knobs, Mercury/capsule dispatch |
| `0x530000`--`0x620000` | 960 KB | IR node primitives, SM50--7x ISel backend, MercExpand engine |
| `0x620000`--`0x920000` | 3 MB | SASS instruction binary encoders (1000+ functions, 4--9 KB each) |
| `0x920000`--`0xA70000` | 1.3 MB | Instruction descriptor initialization tables |
| `0xA70000`--`0xCA0000` | 2 MB | Instruction codec: encoding + decoding infrastructure |
| `0xCA0000`--`0xDA0000` | 1 MB | SM80 (Ampere) instruction selection / encoding backend |
| `0xDA0000`--`0xF16000` | 1.5 MB | SM100+ (Blackwell) instruction encoder/decoder |
| `0xF16000`--`0x100C000` | 984 KB | SM75 (Turing) instruction selection / encoding backend |
| `0x100C000`--`0x11EA000` | 1.9 MB | Shared backend codegen + SM89/90 (Ada/Hopper) backend |
| `0x11EA000`--`0x12B0000` | 824 KB | PTX compiler frontend: ISel patterns, parsing, operand construction |
| `0x12B0000`--`0x1430000` | 1.5 MB | LTO compilation engine: ISel, DWARF, ELF emission, MMA lowering |
| `0x1430000`--`0x15C0000` | 1.6 MB | PTX assembler frontend: instruction handlers, builtin registration |
| `0x15C0000`--`0x16E0000` | 1.2 MB | CUDA device-code compilation backend (PTX-to-SASS pipeline) |
| `0x16E0000`--`0x1850000` | 1.5 MB | Backend compiler: 6 major subsystems |
| `0x1850000`--`0x1A00000` | 1.7 MB | Instruction scheduling, register allocation, codegen verification |
| `0x1A00000`--`0x1B60000` | 1.4 MB | Core SASS backend: operand emission, encoding |
| `0x1B60000`--`0x1D32172` | 1.8 MB | ISel + lowering, end of .text |

## Wiki Structure

This wiki is organized into 13 sections plus supporting reference pages. Every page is written at reimplementation-grade depth.

### Linking Pipeline

End-to-end flow from `main()` through output writing. Covers entry point, CLI parsing, mode dispatch, library resolution, input file loop, merge, layout, relocation, finalization, and output serialization. Start here for the linker-specific logic.

- [Pipeline Overview](pipeline/overview.md) -- End-to-end pipeline diagram with phase boundaries and key function addresses.
- [Entry Point & Main](pipeline/entry.md) -- The 58KB `main()` function: initialization, file dispatch, phase orchestration.
- [CLI Option Parsing](pipeline/cli-options.md) -- 60+ registered options, the option parser infrastructure, validation logic.
- [Mode Dispatch](pipeline/mode-dispatch.md) -- How input file type determines the processing path.
- [Library Resolution](pipeline/library-resolution.md) -- `-l`/`-L` search, libcudadevrt special handling, archive iteration.
- [Input File Loop](pipeline/input-loop.md) -- The linked-list input iteration with per-file type dispatch.
- [Merge Phase](pipeline/merge.md) -- The 89KB `merge_elf` function: section merging, symbol resolution, weak selection.
- [Layout Phase](pipeline/layout.md) -- Shared memory layout, section ordering, address assignment.
- [Relocation Phase](pipeline/relocate.md) -- R\_CUDA relocation application, UFT/UDT resolution.
- [Finalization Phase](pipeline/finalize.md) -- Final patching, entry property computation, register propagation.
- [Output Writing](pipeline/output.md) -- ELF serialization, host linker script generation, verbose stats.

### Input Processing

How each input format is identified, validated, and converted to an internal ELF representation.

- [File Type Detection](input/file-type-detection.md) -- 56-byte header read, magic number dispatch (ELF, fatbin 0xBA55ED50, PTX, NVVM).
- [ELF Parsing (Elf32/Elf64)](input/elf-parsing.md) -- Device ELF validation (e\_machine == 190), section enumeration.
- [Cubin Loading](input/cubin-loading.md) -- Architecture validation, elfw creation, section import.
- [Fatbin Extraction](input/fatbin-extraction.md) -- Container magic, member iteration, LZ4 decompression, type dispatch.
- [Archive Processing](input/archives.md) -- .a member iteration, thin archive support, libcudadevrt handling.
- [PTX Input & JIT](input/ptx-input.md) -- PTX-to-SASS compilation via embedded ptxas backend.
- [NVVM IR / LTO IR Input](input/nvvm-ir-input.md) -- IR module registration, `-lto` requirement, libdevice detection.
- [Host ELF Embedding](input/host-elf.md) -- .o/.so handling, `--use-host-info`, host symbol extraction.

### Linker Core

The fundamental linking algorithms: symbol resolution, section merging, relocation processing, and optimization.

- [Symbol Resolution](linker/symbol-resolution.md) -- Global/local/weak binding, multi-definition detection, COMDAT handling.
- [Symbol Tables & Hash Maps](linker/hash-tables.md) -- Hash table infrastructure, string interning, O(1) symbol lookup.
- [Section Merging](linker/section-merging.md) -- Per-type merge rules for `.nv.global`, `.nv.shared`, `.nv.constant`, `.text`.
- [R\_CUDA Relocations](linker/r-cuda-relocations.md) -- CUDA-specific relocation types, per-architecture encoding.
- [Relocation Application Engine](linker/relocation-engine.md) -- The 27KB `apply_relocations` function.
- [Weak Symbol Handling](linker/weak-symbols.md) -- Register-count and PTX-version comparison for weak function selection.
- [Dead Code Elimination](linker/dead-code-elimination.md) -- Callgraph reachability analysis, address-taken function preservation.
- [Bindless Relocations](linker/bindless-relocations.md) -- Texture/surface bindless reference resolution.
- [Data Layout Optimization](linker/data-layout-opt.md) -- Constant deduplication, overlapping data merge, `.nv.global` optimization.

### Link-Time Optimization

The LTO pipeline: IR collection, libnvvm integration, compilation modes, and option forwarding.

- [LTO Overview](lto/overview.md) -- Architecture of the LTO pipeline, whole-program vs. relocatable compilation.
- [libnvvm Integration](lto/libnvvm-integration.md) -- dlopen mechanics, API surface, `__nvvmHandle` resolution.
- [Whole vs. Partial LTO](lto/whole-vs-partial.md) -- Mode selection, `--force-whole-lto`, `--force-partial-lto`, fallback behavior.
- [Split Compilation](lto/split-compilation.md) -- Thread pool dispatch, `--split-compile-extended`, per-function parallelism.
- [Option Forwarding to cicc](lto/option-forwarding.md) -- `--Xptxas`, `--Xnvvm`, `--maxrregcount` passthrough.
- [LTO IR Format Versions](lto/ir-format-versions.md) -- NVVM IR version detection, bitcode compatibility.

### Embedded ptxas

The ~24MB PTX assembler backend statically linked into nvlink. Covers ISel, register allocation, scheduling, and encoding.

- [Architecture Overview](ptxas/overview.md) -- Binary layout, subsystem decomposition, relationship to standalone ptxas.
- [Architecture Dispatch (vtables)](ptxas/arch-dispatch.md) -- Per-SM vtable-based dispatch for ISel, encoding, and feature queries.
- [Instruction Selection Hubs](ptxas/isel-hubs.md) -- PTX-to-IR lowering, pattern matching, SM-variant parametric clones.
- [Register Allocation](ptxas/register-allocation.md) -- Greedy RA, register pressure, `--maxrregcount` enforcement.
- [Instruction Scheduling](ptxas/scheduling.md) -- Latency-aware scheduling, scoreboard model, barrier insertion.
- [Peephole Optimization](ptxas/peephole.md) -- Post-RA peephole patterns, YIELD-to-NOP conversion, strength reduction.
- [IR Node Infrastructure](ptxas/ir-nodes.md) -- 22 leaf accessors, node types, DAG representation.
- [PTX Parsing](ptxas/ptx-parsing.md) -- PTX text-to-IR frontend, instruction handler registration, builtin table.

### Mercury

The SM100+ (Blackwell) capsule mercury format and associated infrastructure.

- [Mercury Overview](mercury/overview.md) -- What Mercury is, why it exists, the ROT13 obfuscation convention.
- [Capsule Mercury Format](mercury/capmerc-format.md) -- Container structure, capsule layout, relationship to cubin.
- [R\_MERCURY Relocations](mercury/r-mercury-relocations.md) -- Mercury-specific relocation types and processing.
- [Mercury ELF Sections](mercury/elf-sections.md) -- `.nv.merc`, HRKE/HRKI/HRCE/HRCI/HRDE/HRDI sections.
- [Mercury Compiler Passes](mercury/compiler-passes.md) -- MercExpand engine, Mercury-specific ISel patterns.
- [FNLZR (Finalizer)](mercury/fnlzr.md) -- Post-link binary rewriter, pre-link vs. post-link modes, capability masks.

### GPU Targets

Architecture profiles, compatibility logic, and per-generation feature details.

- [Architecture Profiles](targets/arch-profiles.md) -- Profile database structure, per-SM feature flags, capability masks.
- [Compatibility Checking](targets/compatibility.md) -- Cross-architecture linking rules, family matching, version validation.
- [SM75 Turing](targets/sm75-turing.md) -- Minimum supported architecture, Turing-specific encoding.
- [SM80--88 Ampere](targets/sm80-ampere.md) -- Ampere backend, GA100/GA10x variants.
- [SM89 Ada](targets/sm89-ada.md) -- Ada Lovelace specifics, shared backend with SM90.
- [SM90 Hopper](targets/sm90-hopper.md) -- Cluster launch, WGMMA, TMA, asynchronous barriers.
- [SM100 Blackwell](targets/sm100-blackwell.md) -- Mercury output, FNLZR integration, new MMA shapes.
- [SM103 / SM110 / SM120 / SM121](targets/sm103-121.md) -- Blackwell Ultra, Jetson Thor, consumer RTX 50-series, DGX Spark.

### CUDA Device ELF

The output format: NVIDIA's CUDA device ELF (cubin) with its proprietary sections and metadata.

- [Device ELF Format](elf/device-elf-format.md) -- e\_machine 190 (EM\_CUDA), ELF32/64, section layout conventions.
- [NVIDIA Section Types](elf/nvidia-sections.md) -- `.nv.global`, `.nv.shared`, `.nv.constant`, `.nv.info`, `.nv.local`.
- [.nv.info Metadata](elf/nv-info.md) -- EIATTR entries, register counts, barrier counts, CRS attributes.
- [Constant Banks (.nv.constant)](elf/constant-banks.md) -- Bank numbering, per-kernel constants, `ptx.const0.size`.
- [Unified Function Tables](elf/uft.md) -- UFT/UDT structure, UUID-keyed mapping, `__UFT_OFFSET`/`__UDT_OFFSET`.
- [Program Headers](elf/program-headers.md) -- LOAD segments, alignment, entry point metadata.
- [ELF Serialization](elf/serialization.md) -- The `write_elf_to_buffer` function, section ordering, size validation.

### Debug Information

DWARF processing, line table merging, and NVIDIA-specific debug extensions.

- [DWARF Processing](debug/dwarf-processing.md) -- Debug section handling during merge, DWARF abbreviation tables.
- [Line Table Merging](debug/line-tables.md) -- Cross-TU line table merge, file table construction.
- [NVIDIA Debug Extensions](debug/nvidia-extensions.md) -- NVIDIA-proprietary debug section formats.
- [Mercury Debug Sections](debug/mercury-debug.md) -- Debug representation in capsule mercury output.
- [Debug Options & Levels](debug/options.md) -- `-g`, `--suppress-debug-info`, debug section generation control.

### Infrastructure

Shared services used across the linker: memory management, error handling, threading, and utilities.

- [Memory Management (Arenas)](infra/memory-arenas.md) -- The custom arena allocator, size-class free lists, page pools.
- [Error Reporting System](infra/error-reporting.md) -- Diagnostic infrastructure, warning/error/info message emission.
- [Thread Pool](infra/thread-pool.md) -- pthread-based pool for split compilation, barrier synchronization.
- [Library Search](infra/library-search.md) -- `-L` path management, library file resolution algorithm.
- [Timing Infrastructure](infra/timing.md) -- Phase timing (`sub_45CCD0`), profiling support, `--verbose` output.
- [Linker Script Generation](infra/linker-scripts.md) -- `--gen-host-linker-script`, `.nvFatBinSegment` SECTIONS output.

### Data Structures

Detailed layouts of the key internal structures.

- [Linker Context Object](structs/linker-context.md) -- Global state, input file list, target architecture, flags.
- [ELF Writer (elfw)](structs/elf-writer.md) -- Section array, symbol table, relocation lists, string tables.
- [Symbol Record](structs/symbol-record.md) -- Per-symbol metadata: binding, type, section, value, size.
- [Section Record](structs/section-record.md) -- Per-section metadata: type, flags, data pointer, relocations.
- [Architecture Profile](structs/arch-profile.md) -- Per-SM capability record, feature flags, encoding table pointers.

### Configuration

Command-line interface, environment variables, and embedded ptxas option forwarding.

- [CLI Flags Reference](config/cli-flags.md) -- Complete catalog of 60+ registered options with types and defaults.
- [Environment Variables](config/env-vars.md) -- `CAN_FINALIZE_DEBUG` and other environment-based controls.
- [Embedded ptxas Options](config/ptxas-options.md) -- `--Xptxas` forwarding, `--maxrregcount`, optimization levels.

### Reference

Lookup tables and catalogs for quick reference during reverse engineering.

- [R\_CUDA Relocation Catalog](reference/r-cuda-catalog.md) -- All R\_CUDA relocation types with semantics and encoding.
- [R\_MERCURY Relocation Catalog](reference/r-mercury-catalog.md) -- Mercury-specific relocation types.
- [NVIDIA ELF Section Catalog](reference/section-catalog.md) -- All `.nv.*` section names, types, and purposes.
- [elfLink Error Codes](reference/elflink-errors.md) -- Error code catalog with diagnostic strings.
- [ROT13-Encoded Pass Names](reference/rot13-passes.md) -- Decoder ring for obfuscated internal identifiers.

## Reading Order

The wiki contains 85+ pages. These four reading paths guide you through the material based on your goal. Each path lists pages in the recommended order.

### Path 1: New to GPU Linking

Goal: understand what a CUDA device linker does and how nvlink transforms input cubins into a final device executable.

1. **[Pipeline Overview](pipeline/overview.md)** -- The end-to-end flow diagram. Establishes all phases and their relationships. Read this first to build the mental model that every other page assumes.
2. **[Entry Point & Main](pipeline/entry.md)** -- How nvlink is invoked, the 58KB `main()` function, initialization sequence, and phase orchestration.
3. **[File Type Detection](input/file-type-detection.md)** -- The 56-byte header read and magic number dispatch. Understand how nvlink decides whether an input is a cubin, PTX, fatbin, NVVM IR, archive, or host ELF.
4. **[Cubin Loading](input/cubin-loading.md)** -- The most common input path: how device ELF objects are validated, parsed, and imported into the internal `elfw` representation.
5. **[Merge Phase](pipeline/merge.md)** -- The 89KB `merge_elf` function that combines multiple input ELFs. This is the heart of the linker: section merging, symbol resolution, and weak symbol selection.
6. **[Symbol Resolution](linker/symbol-resolution.md)** -- How global, local, and weak symbols are resolved across translation units. COMDAT handling and multi-definition detection.
7. **[R\_CUDA Relocations](linker/r-cuda-relocations.md)** -- CUDA-specific relocation types and how they differ from standard ELF relocations.
8. **[Layout Phase](pipeline/layout.md)** -- Shared memory layout, section ordering, address assignment. The overlap-set analysis that makes `.nv.shared` work.
9. **[Relocation Phase](pipeline/relocate.md)** -- How relocations are applied to produce position-dependent binary code.
10. **[Output Writing](pipeline/output.md)** -- Final ELF serialization, host linker script generation, and the Mercury output path for SM100+.
11. **[Device ELF Format](elf/device-elf-format.md)** -- The output format: `e_machine` 190, NVIDIA-proprietary sections, and the differences from standard ELF.

Optional extensions:

- **[Dead Code Elimination](linker/dead-code-elimination.md)** -- How unreachable functions are stripped via callgraph reachability.
- **[CLI Option Parsing](pipeline/cli-options.md)** -- The 60+ registered options and how they control pipeline behavior.
- **[.nv.info Metadata](elf/nv-info.md)** -- EIATTR attribute encoding: register counts, barrier usage, CRS stack depth.

### Path 2: LTO Deep-Dive

Goal: understand nvlink's link-time optimization pipeline -- how NVVM IR modules are collected, compiled to PTX via libnvvm, assembled to SASS via the embedded ptxas, and merged back into the normal linking flow.

1. **[LTO Overview](lto/overview.md)** -- Architecture of the LTO pipeline: whole-program vs. relocatable compilation, the three-phase flow (collect IR, compile IR to PTX, assemble PTX to SASS).
2. **[NVVM IR / LTO IR Input](input/nvvm-ir-input.md)** -- How NVVM IR modules are detected in input files, the `-lto` requirement, and libdevice detection.
3. **[libnvvm Integration](lto/libnvvm-integration.md)** -- The dlopen/dlsym mechanics for loading `libnvvm.so`, the API surface nvlink calls, and `__nvvmHandle` resolution.
4. **[Whole vs. Partial LTO](lto/whole-vs-partial.md)** -- How `--force-whole-lto` and `--force-partial-lto` change compilation scope, and the fallback behavior when LTO fails.
5. **[Option Forwarding to cicc](lto/option-forwarding.md)** -- How `--Xptxas`, `--Xnvvm`, and `--maxrregcount` are forwarded through the LTO pipeline.
6. **[Split Compilation](lto/split-compilation.md)** -- Thread pool dispatch for `--split-compile-extended`, work item lifecycle, and per-function parallelism.
7. **[PTX Input & JIT](input/ptx-input.md)** -- How the PTX output from libnvvm is fed into the embedded ptxas backend for assembly.
8. **[Merge Phase](pipeline/merge.md)** -- The LTO-compiled cubins re-enter the normal merge flow. Understanding the merge phase is necessary to see where LTO output rejoins the pipeline.

Optional extensions:

- **[LTO IR Format Versions](lto/ir-format-versions.md)** -- NVVM IR version detection and bitcode compatibility across CUDA toolkit releases.
- **[Embedded ptxas Overview](ptxas/overview.md)** -- How the embedded ptxas backend relates to the standalone `ptxas` binary.
- **[Embedded ptxas Options](config/ptxas-options.md)** -- The `--Xptxas` forwarding mechanism and how optimization levels propagate.

### Path 3: Mercury Investigation

Goal: understand the SM100+ (Blackwell) capsule mercury format -- the new ISA container that replaces traditional cubin output for Blackwell and later architectures.

1. **[Mercury Overview](mercury/overview.md)** -- What Mercury is, why it exists, the ROT13 obfuscation convention (`zrephel` = Mercury), and the global mode flag that gates Mercury output.
2. **[Mercury ELF Sections](mercury/elf-sections.md)** -- The `.nv.merc`, HRKE/HRKI/HRCE/HRCI/HRDE/HRDI sections. How Mercury data is embedded within the ELF container.
3. **[Capsule Mercury Format](mercury/capmerc-format.md)** -- The container structure, capsule layout, and relationship to traditional cubin.
4. **[Mercury Compiler Passes](mercury/compiler-passes.md)** -- The MercExpand engine (204KB at `sub_5B1D80`) and Mercury-specific ISel patterns.
5. **[FNLZR (Finalizer)](mercury/fnlzr.md)** -- The post-link binary rewriter, the 10-phase finalization pipeline, pre-link vs. post-link modes, and capability masks.
6. **[R\_MERCURY Relocations](mercury/r-mercury-relocations.md)** -- Mercury-specific relocation types, how they differ from R\_CUDA, and the relocation application flow.

Optional extensions:

- **[SM100 Blackwell](targets/sm100-blackwell.md)** -- Mercury output format, FNLZR integration, new MMA shapes.
- **[Mercury Debug Sections](debug/mercury-debug.md)** -- How debug information is represented in capsule mercury output.
- **[ROT13-Encoded Pass Names](reference/rot13-passes.md)** -- The decoder ring for Mercury-related obfuscated identifiers.

### Path 4: Architecture Hacker

Goal: understand how nvlink supports 22 SM architectures (sm\_75 through sm\_121), how per-architecture dispatch works, and how the embedded ptxas backend selects encoding tables and ISel patterns per target.

1. **[Architecture Profiles](targets/arch-profiles.md)** -- The profile database structure: per-SM feature flags, capability masks, and encoding table pointers.
2. **[Compatibility Checking](targets/compatibility.md)** -- Cross-architecture linking rules: family matching, version validation, and the constraints on mixing objects from different SM versions.
3. **[Architecture Dispatch (vtables)](ptxas/arch-dispatch.md)** -- Per-SM vtable-based dispatch for ISel, encoding, and feature queries. How the binary selects the correct code paths for each target.
4. **[SM75 Turing](targets/sm75-turing.md)** -- The minimum supported architecture, baseline feature set, and Turing-specific encoding.
5. **[SM90 Hopper](targets/sm90-hopper.md)** -- Cluster launch, WGMMA, TMA, asynchronous barriers. The last pre-Mercury architecture.
6. **[SM100 Blackwell](targets/sm100-blackwell.md)** -- The Mercury transition: new output format, FNLZR integration, new MMA shapes.
7. **[SM103 / SM110 / SM120 / SM121](targets/sm103-121.md)** -- Blackwell Ultra, Jetson Thor, consumer RTX 50-series, DGX Spark. How newer architectures derive from SM100.
8. **[Embedded ptxas Overview](ptxas/overview.md)** -- The ~24MB embedded compiler backend: subsystem decomposition and relationship to standalone ptxas.
9. **[Instruction Selection Hubs](ptxas/isel-hubs.md)** -- The five ISel mega-functions (160--280KB each) and how they dispatch per SM variant.

Optional extensions:

- **[SM80--88 Ampere](targets/sm80-ampere.md)** and **[SM89 Ada](targets/sm89-ada.md)** -- Per-generation feature details.
- **[Peephole Optimization](ptxas/peephole.md)** -- Post-RA peephole patterns that vary by architecture.
- **[R\_CUDA Relocation Catalog](reference/r-cuda-catalog.md)** -- Architecture-dependent relocation types.

## Relationship to Other CUDA Toolchain Wikis

nvlink is one component in a four-tool CUDA compilation pipeline. Each tool in the pipeline has its own reverse engineering wiki in this project, and together they provide complete coverage of device code compilation from CUDA C++ source to linked device executable.

### The Pipeline

```
CUDA C++ source (.cu)
  |
  v
cudafe++  -->  host .int.c  +  device IL
  |
  v
cicc  -->  PTX assembly (.ptx)
  |
  v
ptxas  -->  device object (.cubin)
  |
  v
nvlink  -->  linked device executable (cubin or capsule mercury)
```

### Cross-Wiki References

**[cudafe++ wiki](../cudafe++/index.html)** (8.5 MB binary, 6,483 functions) -- Documents the CUDA frontend compiler built on EDG 6.6. cudafe++ separates device code from host code and emits the EDG Intermediate Language consumed by cicc. The execution-space bitfield encoding (`__device__`, `__global__`, `__host__`) defined by cudafe++ determines which symbols appear in the device objects that nvlink eventually merges. The lambda wrapper templates (`__nv_dl_wrapper_t`, `__nv_hdl_wrapper_t`) injected by cudafe++ produce the weak symbols that nvlink resolves during merge.

**[cicc wiki](../cicc/index.html)** (60 MB binary, 80,562 functions) -- Documents the CUDA C-to-PTX compiler built on EDG 6.6 + LLVM 20.0.0. cicc transforms CUDA source into PTX assembly or NVVM IR bitcode. When nvlink operates in LTO mode, it loads `libnvvm.so` (a shared library containing cicc's LLVM backend) to compile NVVM IR modules back to PTX. The `--Xnvvm` option forwarding documented in this wiki's [Option Forwarding](lto/option-forwarding.md) page passes flags to cicc's LLVM pipeline.

**[ptxas wiki](../ptxas/index.html)** (37.7 MB binary, 40,185 functions) -- Documents the PTX-to-SASS assembler. nvlink embeds a copy of the ptxas backend statically linked into its binary (~24 MB, ~20,000 functions). The embedded ptxas section of this wiki provides a summary, but the ptxas wiki covers the same compiler in far greater depth: the 159-phase optimization pipeline, the fatpoint register allocator, the Mercury encoder architecture, and the 1,294 ROT13-encoded internal knobs. When investigating nvlink's embedded compiler behavior, consult the ptxas wiki for detailed pass descriptions.

### When to Use Which Wiki

| Question | Wiki |
|---|---|
| How does nvlink resolve cross-TU device symbols? | **nvlink** -- [Symbol Resolution](linker/symbol-resolution.md) |
| How does the PTX-to-SASS compiler work internally? | **ptxas** -- the 74-page PTX assembler wiki |
| How does LTO compile NVVM IR to PTX? | **nvlink** [LTO Overview](lto/overview.md) + **cicc** LLVM optimizer |
| What CUDA C++ syntax maps to which device IL? | **cudafe++** -- EDG frontend, execution spaces |
| How are Mercury/capsule sections structured? | **nvlink** [Mercury](mercury/overview.md) + **ptxas** capsule mercury |
| How does `--Xptxas` affect code generation? | **nvlink** [Embedded ptxas Options](config/ptxas-options.md) + **ptxas** knobs system |
| What are the 159 optimization phases? | **ptxas** -- [Pass Inventory & Ordering](../ptxas/passes/index.html) |
| How does register allocation determine occupancy? | **ptxas** -- [Allocator Architecture](../ptxas/regalloc/overview.html) |

## Methodology

All analysis was performed by static reverse engineering of the stripped nvlink binary using IDA Pro 9.x and Hex-Rays decompilation. No dynamic analysis was used. Of the 40,532 functions in the binary, 40,366 (99.6%) were successfully decompiled. The remaining 168 are CRT thunks, computed-jump trampolines, and mega-functions exceeding decompiler limits.

For full details on the reverse engineering approach, confidence scoring, and data sources, see [Methodology](methodology.md).
