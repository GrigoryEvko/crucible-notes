# Summary

[Introduction](index.md)
[Binary Layout](binary-layout.md)
[Function Map](function-map.md)
[Methodology](methodology.md)
[Versions](versions.md)

# Linking Pipeline

- [Pipeline Overview](pipeline/overview.md)
- [Entry Point & Main](pipeline/entry.md)
- [CLI Option Parsing](pipeline/cli-options.md)
- [Mode Dispatch](pipeline/mode-dispatch.md)
- [Library Resolution](pipeline/library-resolution.md)
- [Input File Loop](pipeline/input-loop.md)
- [Merge Phase](pipeline/merge.md)
- [Layout Phase](pipeline/layout.md)
- [Relocation Phase](pipeline/relocate.md)
- [Finalization Phase](pipeline/finalize.md)
- [Output Writing](pipeline/output.md)

# Input Processing

- [File Type Detection](input/file-type-detection.md)
- [ELF Parsing (Elf32 / Elf64)](input/elf-parsing.md)
- [Cubin Loading](input/cubin-loading.md)
- [Fatbin Extraction](input/fatbin-extraction.md)
- [Archive Processing](input/archives.md)
- [PTX Input & JIT](input/ptx-input.md)
- [NVVM IR / LTO IR Input](input/nvvm-ir-input.md)
- [Host ELF Embedding](input/host-elf.md)
- [168-Byte Input Container](input/container-struct.md)

# Linker Core

- [Symbol Resolution](linker/symbol-resolution.md)
  - [Symbol Addition](linker/symbol-addition.md)
  - [Symbol Resolution Walkthrough](linker/symbol-resolution-walkthrough.md)
  - [Extended Symbol Resolution](linker/extended-symbol-resolution.md)
- [Symbol Tables & Hash Maps](linker/hash-tables.md)
- [Section Merging](linker/section-merging.md)
  - [Section Layout Engine](linker/section-layout-engine.md)
- [R_CUDA Relocations](linker/r-cuda-relocations.md)
- [Relocation Application Engine](linker/relocation-engine.md)
- [Weak Symbol Handling](linker/weak-symbols.md)
- [Dead Code Elimination](linker/dead-code-elimination.md)
- [Bindless Relocations](linker/bindless-relocations.md)
- [Data Layout Optimization](linker/data-layout-opt.md)
- [Sanitizer & Stack-Protector Integration](linker/sanitizer-injection.md)

# Link-Time Optimization

- [LTO Overview](lto/overview.md)
- [libnvvm Integration](lto/libnvvm-integration.md)
- [Module-Add Path (sub_4CE070)](lto/module-add-path.md)
- [Whole vs Partial LTO](lto/whole-vs-partial.md)
- [Split Compilation](lto/split-compilation.md)
- [Option Forwarding to cicc](lto/option-forwarding.md)
- [LTO Profile Tags & Architecture Mapping](lto/ir-format-versions.md)

# Embedded ptxas

- [Architecture Overview](ptxas/overview.md)
- [Compilation Driver (sub_1112F30)](ptxas/embedded-driver.md)
- [Architecture Dispatch (vtables)](ptxas/arch-dispatch.md)
- [Instruction Selection Hubs](ptxas/isel-hubs.md)
- [Register Allocation](ptxas/register-allocation.md)
- [Instruction Scheduling](ptxas/scheduling.md)
- [Peephole Optimization](ptxas/peephole.md)
- [IR Node Infrastructure](ptxas/ir-nodes.md)
- [PTX Parsing](ptxas/ptx-parsing.md)

# Mercury

- [Mercury Overview](mercury/overview.md)
- [Capsule Mercury Format](mercury/capmerc-format.md)
- [R_MERCURY Relocations](mercury/r-mercury-relocations.md)
- [Mercury ELF Sections](mercury/elf-sections.md)
- [Mercury Compiler Passes](mercury/compiler-passes.md)
- [Section Content-Equality Dedup](mercury/section-comdat-dedup.md)
- [FNLZR (Finalizer)](mercury/fnlzr.md)

# GPU Targets

- [Architecture Profiles](targets/arch-profiles.md)
- [Compatibility Checking](targets/compatibility.md)
- [SM75 Turing](targets/sm75-turing.md)
- [SM80-88 Ampere](targets/sm80-ampere.md)
- [SM89 Ada](targets/sm89-ada.md)
- [SM90 Hopper](targets/sm90-hopper.md)
- [SM100 Blackwell](targets/sm100-blackwell.md)
- [SM103 / SM110 / SM120 / SM121](targets/sm103-121.md)

# CUDA Device ELF

- [Device ELF Format](elf/device-elf-format.md)
- [NVIDIA Section Types](elf/nvidia-sections.md)
- [.nv.info Metadata](elf/nv-info.md)
- [Constant Banks (.nv.constant)](elf/constant-banks.md)
- [Unified Function Tables](elf/uft.md)
- [Program Headers](elf/program-headers.md)
- [ELF Serialization](elf/serialization.md)

# Debug Information

- [DWARF Processing](debug/dwarf-processing.md)
- [Line Table Merging](debug/line-tables.md)
- [NVIDIA Debug Extensions](debug/nvidia-extensions.md)
- [Mercury Debug Sections](debug/mercury-debug.md)
- [Debug Options & Levels](debug/options.md)

# Infrastructure

- [Memory Management (Arenas)](infra/memory-arenas.md)
- [Error Reporting System](infra/error-reporting.md)
- [Thread Pool](infra/thread-pool.md)
- [Library Search](infra/library-search.md)
- [Timing Infrastructure](infra/timing.md)
- [Linker Script Generation](infra/linker-scripts.md)

# Data Structures

- [Linker Context Object](structs/linker-context.md)
- [ELF Writer (elfw)](structs/elf-writer.md)
- [Symbol Record](structs/symbol-record.md)
- [Section Record](structs/section-record.md)
- [Architecture Profile](structs/arch-profile.md)

# Configuration

- [CLI Flags Reference](config/cli-flags.md)
- [Environment Variables](config/env-vars.md)
- [Embedded ptxas Options](config/ptxas-options.md)

# Reference

- [R_CUDA Relocation Catalog](reference/r-cuda-catalog.md)
- [R_MERCURY Relocation Catalog](reference/r-mercury-catalog.md)
- [NVIDIA ELF Section Catalog](reference/section-catalog.md)
- [elfLink Error Codes](reference/elflink-errors.md)
- [ROT13-Encoded Pass Names](reference/rot13-passes.md)
