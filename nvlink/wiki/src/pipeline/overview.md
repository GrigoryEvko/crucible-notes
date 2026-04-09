# Pipeline Overview

nvlink executes as a single-pass linear pipeline with 14 phases, two optional compiler detours (LTO and PTX JIT), and three distinct output code paths. All phases run inside `main()` at `0x409800` -- a 57,970-byte monolithic function that drives the entire tool from initialization through cleanup. This page documents the full pipeline sequence, the timing infrastructure woven through it, the three output code paths, and the data flow between phases.

## Pipeline Diagram

```
                         nvlink v13.0.88 pipeline
                         ========================

  Phase 1   INIT             arena_create_named (0x432020)
              |               "nvlink option parser"
              |               "nvlink memory space"
              v
  Phase 2   CLI PARSE        nvlink_parse_options (0x427AE0)
              |               ~60 options registered, extracted to globals
              v
  Phase 3   MODE DISPATCH    dword_2A77DC0 check
              |               0 = error
              |               1 = gen-linker-script-only  ----> HOST LINK PATH
              |               2 = augmented (passthru)    ----> HOST LINK PATH
              |               3 = device link             ----> DEVICE LINK PATH
              v
  Phase 4   LIBRARY RESOLVE  LIBRARY_PATH env + -L paths
              |               path_search_library (0x462870)
              |               resolve -l flags to files
              v
  Phase 5   CONTEXT CREATE   elfw_create (0x4438F0)
              |               output ELF wrapper, section tables,
              |               symbol table, "elfw memory space" arena
              v
  Phase 6   CONFIG           arch validation, feature flags,
              |               Mercury mode (sm>99), SASS mode (sm>89),
              |               reloc vtable creation (0x459640)
              v
  Phase 7   INPUT FILE LOOP  iterate qword_2A5F330 linked list
              |               for each file:
              |                 read 56-byte header
              |                 dispatch by magic:
              |                   cubin  -> validate arch (0x426570)
              |                   ptx    -> ptxas JIT (0x4BD760)  ----+
              |                   fatbin -> extract (0x42AF40)        |
              |                   nvvm   -> requires -lto (0x427A10)  |
              |                   ltoir  -> requires -lto             |  COMPILER
              |                   .a     -> iterate members           |  DETOUR
              |                 register module (0x42A680)            |
              v                                                      |
  Phase 8   LTO (if -lto)    collect IR (0x426CD0)              <----+
              |               dlopen libnvvm.so
              |               compile IR -> PTX (0x4BC6F0)
              |               split-compile thread pool (0x43FDB0)
              |               assemble PTX -> cubin (embedded ptxas)
              v
  Phase 9   MERGE            for each object:
              |                 merge_elf (0x45E7D0, 89KB)
              |                 weak symbol resolution (0x45D180)
              |               cudadevrt special-case removal
              |               dead code elimination (0x44AD40)
              v
  Phase 10  LAYOUT           shared memory layout (0x439830, 66KB)
              |               entry property computation (0x451D80, 98KB)
              |               register/barrier propagation (0x450ED0)
              |               data overlap merge (0x432B10 et al.)
              |               constant dedup (0x4339A0)
              |               section sort & layout (0x465720)
              |               bindless processing (0x438DD0)
              v
  Phase 11  RELOCATE         apply_relocations (0x469D60, 27KB)
              |               UFT/UDT setup (0x463F70)
              |               UFT reorder (0x4637B0)
              |               resolved rela emission (0x46ADC0)
              v
  Phase 12  FINALIZE         finalize_elf (0x445000, 56KB)
              |               final reloc patching per arch
              |               callgraph section build (0x44D200)
              |               Mercury FNLZR (0x4275C0) if sm>=100
              v
  Phase 13  WRITE            write_elf_to_buffer (0x45BF00)
              |               write to file (0x45C920)
              |               dot-file output (0x44CCF0)
              |               register-link-binaries output
              v
  Phase 14  CLEANUP          elfw_destroy (0x4475B0)
                              arena_destroy (0x431C70)
                              arena_dump_stats (0x431770) if verbose
```

## Phase Table

Every phase maps to a specific address range in `main()`. The function addresses listed are the primary entry points called from `main()` for each phase.

| # | Phase | Primary function | Address | Size | Timing tag |
|---|---|---|---|---|---|
| 1 | Init | `arena_create_named` | `0x432020` | 2,161 B | `"init"` |
| 2 | CLI parse | `nvlink_parse_options` | `0x427AE0` | 30,272 B | `"init"` |
| 3 | Mode dispatch | inline in `main()` | `0x409800` | -- | `"init"` |
| 4 | Library resolve | `path_search_library` | `0x462870` | 4,905 B | `"init"` |
| 5 | Context create | `elfw_create` | `0x4438F0` | 14,821 B | `"init"` |
| 6 | Config | `reloc_vtable_create` | `0x459640` | 16,109 B | `"init"` |
| 7 | Input file loop | `extract_and_process_fatbin_member` | `0x42AF40` | 11,143 B | `"read"` |
| 8 | LTO | `lto_collect_ir_modules` / nvvm call | `0x426CD0` / `0x4BC6F0` | 7,040 B / varies | `"cicc-lto"` / `"ptxas-lto"` |
| 9 | Merge | `merge_elf` | `0x45E7D0` | 89,156 B | `"merge"` |
| 10 | Layout | `shared_memory_layout` / `compute_entry_properties` | `0x439830` / `0x451D80` | 65,776 B / 97,969 B | `"layout"` |
| 11 | Relocate | `apply_relocations` | `0x469D60` | 26,578 B | `"relocate"` |
| 12 | Finalize | `finalize_elf` | `0x445000` | 55,681 B | `"finalize"` |
| 13 | Write | `write_elf_to_buffer` | `0x45BF00` | 13,258 B | `"write"` |
| 14 | Cleanup | `arena_destroy` / `elfw_destroy` | `0x431C70` / `0x4475B0` | 3,564 B / 3,023 B | -- |

The five largest functions in the pipeline are all in the linker core, not the compiler backend:

1. `compute_entry_properties` -- 97,969 bytes (500+ locals, register/barrier propagation)
2. `merge_elf` -- 89,156 bytes (450+ locals, full section merge)
3. `shared_memory_layout` -- 65,776 bytes (overlap set analysis, per-entry allocation)
4. `finalize_elf` -- 55,681 bytes (final reloc patching, architecture-specific handlers)
5. `main` -- 57,970 bytes (14-phase orchestrator)

## Timing Infrastructure

nvlink has a built-in timing system activated by an internal timing file path (global `qword_2A5F290`). The timing calls bracket each pipeline phase with string tags.

**Timing functions**:
- `sub_45CCD0` -- start timer for a named phase
- `sub_45CCE0` -- stop timer, record elapsed time

**Phase tag strings** (embedded in `main()` and referenced by `sub_4279C0`):

| Tag | Pipeline phases covered |
|---|---|
| `"init"` | Phases 1--6: arena creation, option parsing, library resolution, context setup |
| `"read"` | Phase 7: input file loop, format detection, PTX JIT, fatbin extraction |
| `"cicc-lto"` | Phase 8 (IR compile): NVVM IR to PTX compilation via libnvvm |
| `"ptxas-lto"` | Phase 8 (assembly): PTX to SASS assembly via embedded ptxas |
| `"merge"` | Phase 9: merge_elf loop, weak resolution, dead code elimination |
| `"layout"` | Phase 10: shared memory, entry properties, section layout |
| `"relocate"` | Phase 11: relocation application, UFT/UDT processing |
| `"finalize"` | Phase 12: final reloc patching, Mercury FNLZR |
| `"write"` | Phase 13: ELF serialization, output file writing |

The debug trace function `sub_4279C0` emits these tag strings to stderr when verbose debugging is enabled (`dword_2A5F308`), producing output of the form: `nvlink: phase <tag>`.

## Three Code Paths

nvlink's mode dispatch (Phase 3) selects one of three fundamentally different code paths based on the global `dword_2A77DC0`. This global is set during option parsing based on `--gen-host-linker-script`, `--shared`, and the implicit device-link default.

### Path 1: Device Linking (mode 3)

The default and most complex path. Runs the full 14-phase pipeline from Phase 1 through Phase 14. This is the path taken when `nvcc` invokes nvlink to combine separately compiled `.cubin` files into a final device executable.

```
Input cubins --> merge --> layout --> relocate --> finalize --> write cubin
                                                     |
                                              sm>=100? --> FNLZR --> capsule mercury
```

Key characteristics:
- All 14 phases execute
- The merge function (89KB) runs once per input object
- LTO phases 8 interleaves if `-lto` is active
- Mercury FNLZR post-link transform applies for sm>=100
- Output is a CUDA device ELF (cubin) or capsule mercury

### Path 2: Host Linker Script Generation (mode 1)

When `--gen-host-linker-script` is specified, nvlink skips the core linking pipeline entirely and generates a host linker script containing `.nvFatBinSegment` section definitions. This script is consumed by the host `ld` to embed fat binaries into the host executable.

```
Input files --> generate SECTIONS { .nvFatBinSegment ... } --> write script
```

The generated script looks like:
```
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    ...
}
```

Key characteristics:
- Phases 7--12 are skipped entirely
- No merge, no relocation, no ELF output
- Output is a text linker script, not a binary
- Used by `nvcc`'s host compilation stage

### Path 3: Register-Link-Binaries (augmented mode 2)

When `--register-link-binaries` is active, nvlink generates C source code containing `DEFINE_REGISTER_FUNC()` macro invocations that register compiled binaries with the CUDA runtime. This is an augmented passthrough mode where nvlink processes input files to extract module identifiers, then emits registration code.

```
Input files --> extract module_id --> write DEFINE_REGISTER_FUNC(name) --> output .c file
```

The output contains lines of the form:
```c
DEFINE_REGISTER_FUNC(module_name)
```

Key characteristics:
- Runs input file loop (Phase 7) to extract module identifiers via `register_module_for_linking` (`0x42A680`)
- Skips merge/layout/relocate/finalize phases
- Output is C source, not a binary

### Path Selection Logic

```
dword_2A77DC0 value    Condition                    Code path
----------------------------------------------------------------------
0                      invalid / error state        abort with diagnostic
1                      --gen-host-linker-script      Host linker script
2                      --register-link-binaries      Register-link augmented
3                      (default)                     Full device link
```

## Data Flow Between Phases

The pipeline communicates through a small set of global data structures that accumulate state as phases execute.

```
Phase 1-2  INIT/CLI
    |
    |  Produces:
    |    - Option parser arena       (transient, freed after extraction)
    |    - Main memory arena          qword at "nvlink memory space"
    |    - ~80 global config flags    byte_2A5F2xx / dword_2A5Fxxx
    |    - Input file linked list     qword_2A5F330
    |    - Library search paths       qword_2A5F300 / qword_2A5F2F8
    v
Phase 5  CONTEXT CREATE
    |
    |  Produces:
    |    - Output ELF wrapper (elfw)  returned from elfw_create
    |      Contains: .shstrtab, .strtab, .symtab,
    |                .note.nv.cuinfo, .note.nv.tkinfo
    |    - elfw memory arena          "elfw memory space"
    v
Phase 7  INPUT FILE LOOP
    |
    |  Consumes: input file linked list, library search paths
    |  Produces:
    |    - Per-file: parsed ELF structures, validated arch
    |    - Per-file: module registration records (80 bytes each)
    |    - LTO: collected IR module list
    |    - JIT: compiled cubin objects (from PTX/fatbin members)
    v
Phase 8  LTO (optional)
    |
    |  Consumes: IR module list from Phase 7
    |  Produces: compiled cubin objects fed back to merge list
    v
Phase 9  MERGE
    |
    |  Consumes: all cubin objects (from input + LTO + JIT)
    |  Mutates: output elfw
    |    - Copies sections from each input into output
    |    - Resolves symbols (global, weak, local)
    |    - Merges .nv.info metadata
    |    - Removes dead code if -use-host-info / -kernels-used
    v
Phase 10  LAYOUT
    |
    |  Consumes: merged elfw with all sections and symbols
    |  Mutates: elfw section addresses and properties
    |    - Shared memory: offset assignment per entry function
    |    - Callgraph: register/barrier count propagation
    |    - Constants: deduplication, overlap merge
    |    - Sections: final ordering and address assignment
    v
Phase 11-12  RELOCATE / FINALIZE
    |
    |  Consumes: laid-out elfw with resolved addresses
    |  Mutates: elfw section data
    |    - Patches relocation entries in section data
    |    - Builds .nv.callgraph section
    |    - UFT/UDT setup and reordering
    |    - Mercury FNLZR in-place transform (sm>=100)
    v
Phase 13  WRITE
    |
    |  Consumes: finalized elfw
    |  Produces:
    |    - Output ELF file (via write_elf_to_buffer -> fwrite)
    |    - Optional: .dot callgraph file
    |    - Optional: register-link-binaries .c file
    v
Phase 14  CLEANUP
    |
    |  Destroys: elfw, all arenas
    |  Optional: arena_dump_stats if verbose
```

### The Central Data Structure: `elfw`

The output ELF wrapper (`elfw`) is the single most important data structure in the pipeline. Created in Phase 5, it accumulates state across all subsequent phases:

- **Phase 9** (merge): sections, symbols, and relocations are copied into it from each input object
- **Phase 10** (layout): section addresses are assigned, shared memory offsets are computed, properties are propagated
- **Phase 11** (relocate): relocation entries are resolved against the laid-out addresses
- **Phase 12** (finalize): final patches are applied, architecture-specific encoding is performed
- **Phase 13** (write): the elfw is serialized to a byte buffer and written to disk

The elfw is allocated on the "elfw memory space" arena created by `elfw_create`. It contains section header string table (`.shstrtab`), string table (`.strtab`), symbol table (`.symtab`), and the CUDA-specific note sections (`.note.nv.cuinfo`, `.note.nv.tkinfo`) as initial sections. All other sections are added during merge.

## LTO Pipeline Detail

When `-lto` is active, Phase 8 expands into a multi-step sub-pipeline that involves loading an external shared library and spawning threads:

```
Phase 8 LTO sub-pipeline
=========================

8a. Collect IR modules       sub_426CD0     Gather NVVM IR from all inputs
        |
8b. Mark used symbols        sub_426AE0     If -use-host-info or -kernels-used:
        |                                     run dead code elimination on IR
        |
8c. Whole-program compile    sub_4BC6F0     dlopen libnvvm.so from --nvvmpath
        |                                    Call nvvm API: IR -> PTX
        |                                    Modes: whole-program or relocatable
        |
8d. Split compilation        sub_43FDB0     If --split-compile-extended:
        |                    sub_4264B0       create thread pool
        |                                     dispatch per-module compilation
        |                                     each thread: PTX -> cubin
        |
8e. Process outputs                          Add compiled cubins to merge list
                                             Remove cudadevrt if full LTO
```

The LTO pipeline distinguishes two compilation strategies based on flags:

- **Whole-program LTO** (`--force-whole-lto`): all IR modules are compiled as a single unit. The string `"whole program compile"` is emitted.
- **Partial LTO** (`--force-partial-lto`): modules are compiled individually. Useful when not all inputs have IR. The string `"relocatable compile"` is emitted.

A special case: when all inputs have LTO IR, nvlink removes `libcudadevrt` from the link list entirely (string: `"LTO on everything so remove libcudadevrt from list"`), since the device runtime is compiled directly into the output.

## Error Handling

The pipeline uses a centralized diagnostic system (`sub_467460` -> `sub_467A70`) with five severity levels:

| Prefix | Meaning | Behavior |
|---|---|---|
| `"info    "` | Informational | Suppressed by `--disable-infos` |
| `"warning "` | Warning | Suppressed by `--disable-warnings`; promoted to error by `-Werror` |
| `"error   "` | Recoverable error | Accumulated, linking continues |
| `"error*  "` | Hard error | Accumulated, may abort phase |
| `"fatal   "` | Fatal error | Immediate termination |

Error descriptors are stored in a table at `unk_2A5Bxxx`. Each call to `sub_467460` passes a pointer to a specific descriptor plus format arguments for the error message.

Most phases check for accumulated errors before proceeding to the next phase. The merge phase in particular validates extensively: `"cubin not an elf?"`, `"cubin not a device elf?"`, `"fatbin wrong format?"`, `"merge_elf failed"`, `"efh not found"`, `"symsec not found"`, `"strsec not found"`.

## Key Global State

The pipeline's control flow and data flow depend on approximately 80 global variables set during Phase 2 (option parsing). The most architecturally significant ones:

| Global | Type | Controls |
|---|---|---|
| `dword_2A77DC0` | int | Linker mode: 0=error, 1=script, 2=augmented, 3=device-link |
| `dword_2A5F314` | int | SM version number (e.g., 90, 100) |
| `byte_2A5F222` | bool | Mercury mode (sm > 99) |
| `byte_2A5F225` | bool | SASS mode (sm > 89) |
| `byte_2A5F288` | bool | LTO active (`-lto`) |
| `byte_2A5F1E8` | bool | Relocatable link (`-r`) |
| `qword_2A5F330` | ptr | Input file linked list head |
| `qword_2A5F278` | ptr | nvvmpath (libnvvm.so location) |
| `dword_2A5B528` | int | Compilation mode: 0=normal, 2=passthru, 4=lto, 6=sass |
| `byte_2A5F2D8` | bool | Verbose output (`-v`) |
| `dword_2A5F308` | int | Debug/verbose flags |

These globals are read throughout the pipeline to gate code paths. For example, Phase 8 (LTO) only executes when `byte_2A5F288` is set, and Phase 12 only invokes the FNLZR when `byte_2A5F222` (Mercury mode) is true.

## Phase Dependencies

Not all phases run in every invocation. The dependency structure:

```
Phase     Condition                          Always?
-----     ---------                          -------
1-6       unconditional                      yes
7         unconditional                      yes
8         byte_2A5F288 (-lto)                no
9         mode == 3 (device link)            no
10        mode == 3                           no
11        mode == 3                           no
12        mode == 3                           no
13        mode == 3 (or mode 1/2 for script)  yes (but output type varies)
14        unconditional                      yes
```

In device-link mode (mode 3), all 14 phases execute. In host-linker-script mode (mode 1), only phases 1--6, 13 (script generation), and 14 execute. In register-link mode (mode 2), phases 1--7, 13 (source generation), and 14 execute.

## Performance Characteristics

The pipeline is single-threaded except for two points:

1. **LTO split compilation** (Phase 8): `sub_43FDB0` creates a pthread thread pool, and `sub_4264B0` is dispatched to each thread for parallel PTX-to-SASS compilation. Thread count is controlled by `dword_2A5B514` (`--split-compile-extended`).

2. **Memory arena allocation** (all phases): the arena allocator (`sub_4307C0`) is thread-safe with per-arena mutexes, supporting the LTO thread pool.

The merge phase (Phase 9) is the single largest bottleneck for link time in typical workloads: `merge_elf` at 89KB is called once per input object, and each call performs a full traversal of the input's section table with symbol resolution, relocation copying, and metadata merging.

The five timing tags that typically dominate are `"read"` (file I/O and format detection), `"merge"` (the O(n * sections) merge loop), and `"layout"` (the O(functions^2) callgraph propagation). For LTO builds, `"cicc-lto"` and `"ptxas-lto"` dominate overwhelmingly, since they invoke full compiler backends.

## Cross-References

### Pipeline Phase Pages
- [Entry Point & Main](entry.md) -- `main()` at `0x409800`: the 57,970-byte orchestrator function
- [CLI Option Parsing](cli-options.md) -- Phase 2: parser infrastructure, option entry layout, global variable map
- [Mode Dispatch](mode-dispatch.md) -- Phase 3: device link vs. host linker script vs. register-link
- [Library Resolution](library-resolution.md) -- Phase 4: `LIBRARY_PATH` search and `-l` flag resolution
- [Input File Loop](input-loop.md) -- Phase 7: file type detection, per-format dispatch, module registration
- [Merge Phase](merge.md) -- Phase 9: `merge_elf` (89KB), weak resolution, section/symbol merging
- [Layout Phase](layout.md) -- Phase 10: shared memory, constant dedup, section address assignment
- [Relocation Phase](relocate.md) -- Phase 11: `apply_relocations` (27KB), UFT/UDT processing
- [Finalization Phase](finalize.md) -- Phase 12: final reloc patching, Mercury FNLZR, callgraph build
- [Output Phase](output.md) -- Phase 13: ELF serialization, dot-file output

### Input Processing Pages
- [File Type Detection](../input/file-type-detection.md) -- 56-byte header probe and magic number classification
- [Cubin Loading](../input/cubin-loading.md) -- cubin validation, arch checking, FNLZR dispatch
- [Fatbin Extraction](../input/fatbin-extraction.md) -- fatbin container format, architecture matching, member extraction
- [PTX Input & JIT](../input/ptx-input.md) -- embedded ptxas compilation path for PTX inputs
- [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md) -- IR module registration and LTO prerequisites
- [Archive Processing](../input/archives.md) -- `.a` archive iteration and libcudadevrt handling

### Supporting Subsystems
- [CLI Flags Reference](../config/cli-flags.md) -- all 68 flags with types, defaults, visibility
- [Timing Infrastructure](../infra/timing.md) -- CSV timing output and phase tag strings
- [Error Reporting](../infra/error-reporting.md) -- the five-level diagnostic system
- [Memory Arenas](../infra/memory-arenas.md) -- arena-based allocation backing the pipeline
- [LTO Overview](../lto/overview.md) -- Phase 8 LTO sub-pipeline detail
- [Mercury Overview](../mercury/overview.md) -- Mercury/CapMerc processing for sm >= 100

### Sibling Wikis
- **ptxas wiki**: [Pipeline Overview](../../../../ptxas/wiki/src/pipeline/overview.md) -- standalone ptxas 159-phase compilation pipeline; the same compiler is embedded in nvlink for PTX JIT and LTO assembly
- **cicc wiki**: [Pipeline Overview](../../../../cicc/wiki/src/pipeline/overview.md) -- cicc CUDA compiler pipeline; its `libnvvm.so` is loaded via `dlopen` during LTO Phase 8
