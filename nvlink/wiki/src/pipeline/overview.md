# Pipeline Overview

nvlink executes as a single-pass linear pipeline with 14 phases, two optional compiler detours (LTO and PTX JIT), and three distinct output code paths. All phases run inside `main()` at `0x409800` — a 57,970-byte monolithic function that drives the entire tool from initialization through cleanup. This page documents the full pipeline sequence, the timing infrastructure woven through it, the three output code paths, the data flow between phases, and the Mercury post-link transform that sits between finalization and output.

## Complete Pipeline Diagram

The diagram below shows all 14 phases for the full device-link path (mode 3), including both optional compiler detours (PTX JIT and LTO) and the Mercury/FNLZR post-link transform. Phases are numbered and labeled with their timing tag strings. Arrows show data flow between phases. The ASCII box edges show which phases execute conditionally.

```text
                           nvlink v13.0.88 pipeline
                      Full device-link path (mode 3)
                      ================================

  PHASE 1   INIT                    main() line 377
  +------------------------------+
  | arena_create("nvlink option  |  sub_432020 (2,161 B)
  |   parser")              v338 |  Creates option-parser arena
  | arena_create("nvlink memory  |  sub_432020 (2,161 B)
  |   space")               v339 |  Creates main working arena
  | timer_init(&v356)            |  sub_43D8C0
  | arena_snapshot(v339)    v340 |  sub_45CAE0
  +------------------------------+
              |
              v
  PHASE 2   CLI PARSE               main() line 384
  +------------------------------+
  | nvlink_parse_options(argc,   |  sub_427AE0 (30,272 B)
  |   argv)                      |  68 options --> ~80 globals
  | Sets: dword_2A77DC0 (mode),  |
  |   dword_2A5F314 (SM version),|
  |   byte_2A5F288 (LTO flag),  |
  |   byte_2A5F222 (Mercury),   |
  |   byte_2A5F225 (SASS mode)  |
  +------------------------------+
              |
              v
  PHASE 3   MODE DISPATCH            main() line 385
  +------------------------------+
  | if (dword_2A77DC0 - 1) > 1  |  Inline check in main()
  |   --> device link (mode 0/3) |  Falls through to Phase 4
  | else                         |
  |   mode 1 --> HOST SCRIPT ----+----------> write linker script
  |   mode 2 --> AUGMENTED  -----+----------> ld --verbose + write
  +------------------------------+            (skips Phases 4--12)
              |
              | (device-link path only)
              v
  PHASE 4   LIBRARY RESOLVE          main() lines 387--424
  +------------------------------+
  | library_search_create()      |  sub_4622D0
  | add -L paths from            |  sub_462500
  |   qword_2A5F300 list         |
  | add $LIBRARY_PATH dirs       |  getenv("LIBRARY_PATH")
  | for each -l flag:            |
  |   path_search_library()      |  sub_462870 (4,905 B)
  |   append to input file list  |  qword_2A5F330
  +------------------------------+
              |
              v
  PHASE 5   CONTEXT CREATE           main() lines 428--593
  +------------------------------+
  | cuda_api_version = sub_468560|
  | elfw = elfw_create(          |  sub_4438F0 (14,821 B)
  |   type, is_64bit, elf_class, |
  |   sm_version, debug_flag,    |
  |   cuda_api_ver, verbose,     |
  |   merge_flags, mercury_flag) |
  | Returns elfw object (v55)    |
  |   with .shstrtab, .strtab,  |
  |   .symtab, .note.nv.cuinfo, |
  |   .note.nv.tkinfo           |
  +------------------------------+
              |
              v
  PHASE 6   CONFIG                   main() lines 497--593
  +------------------------------+
  | Mercury mode: elfw[104] = 2  |  if byte_2A5F222
  | SM>72: sub_451920(elfw,9,..) |  ELF class 8 setup
  | legacy: sub_444710(elfw,..)  |  ELF class 7 setup
  | if LTO: load libdevice       |  sub_4BC470
  |   from nvvmpath + "/lib64"   |
  | if stack canary: sub_4389F0  |  stack protector init
  | if kernels-used: sub_43F360  |  load used-symbol list
  | if variables-used: sub_43F950|  load used-var list
  | if uidx-file: load via       |  sub_476BF0
  |   sub_463490                 |
  | if host-info: load via       |  sub_435B60
  |   sub_476BF0                 |
  | if SM>72: write version      |  sub_443730
  |   "Cuda compilation tools,   |
  |   release 13.0, V13.0.88"   |
  | trace("init")                |  sub_4279C0
  +------------------------------+
              |
              v
  PHASE 7   INPUT FILE LOOP          main() lines 595--1741
  +------------------------------+     +-----------------------------+
  | for each file in             |     |  COMPILER DETOUR: PTX JIT  |
  |   qword_2A5F330:             |     | +-------------------------+|
  |   read 56-byte header        |     | | sub_4BD760 (ptxas)      ||
  |   dispatch by extension:     |     | | PTX -> cubin            ||
  |                              |     | | timing: start/stop      ||
  |   "cubin" --> validate arch  |     | | around sub_45CCD0/CCE0  ||
  |     sub_43D970 (ELF magic)   |     | +-------------------------+|
  |     sub_426570 (arch check)  |     +-----------------------------+
  |     if Mercury: sub_4275C0---+-------> FNLZR pre-link transform
  |     sub_42A680 (register)    |
  |                              |     +-----------------------------+
  |   "ptx" -----+              |     |  COMPILER DETOUR: LTO IR  |
  |     sub_4BD760 (ptxas JIT)---+---->| collected in Phase 8       |
  |     validate + register      |     +-----------------------------+
  |                              |
  |   "fatbin" --> sub_42AF40    |  extract_and_process_fatbin
  |     (11,143 B)               |  iterate members, recurse
  |                              |
  |   "nvvm"/"ltoir"             |
  |     assert byte_2A5F288      |  "should only see nvvm files
  |     sub_427A10 (add IR)      |   when -lto"
  |                              |
  |   "bc" --> fatal error       |  "should never see bc files"
  |                              |
  |   archive (.a) -->           |
  |     sub_4BDAC0 (open)        |
  |     sub_4BDAF0 (iterate)     |
  |     sub_42AF40 per member    |
  |     cudadevrt deferral       |
  |                              |
  |   .so / unknown --> ignore   |  "ignore input %s"
  +------------------------------+
              |
              v
  PHASE 8   LTO (if -lto)            main() lines 910--1367
  +..................................+
  : 8a. Validate option conflicts    :  -lineinfo, -maxrregcount,
  :     -ftz, -prec-div, etc.        :  -prec-sqrt, -fmad, -split
  :                                  :
  : 8b. NVVM callback (if -vkeep)   :  dlsym("__nvvmHandle")
  :     handle(0xBEEF) --> callback  :  callback(ctx, sub_4299E0,
  :     register                     :    0, 0xF00D)
  :                                  :
  : 8c. Collect IR modules           :  sub_426CD0 (7,040 B)
  :     --> ir_modules, module_count :
  :                                  :
  : 8d. Compile IR --> PTX           :  sub_4BC6F0 (libnvvm)
  :     tag: "cicc-lto"              :  dlopen libnvvm.so
  :                                  :
  : 8e. Assemble PTX --> cubin       :
  :   +--- whole-program?            :  sub_4BD4E0  "whole program
  :   |    single PTX --> single     :     compile"
  :   |    cubin, write directly     :
  :   +--- single-module?            :  sub_4BD760  "relocatable
  :   |    sub_4BD760 (ptxas)        :     compile"
  :   +--- split-compile?            :
  :        sub_43FDB0 (thread pool)  :  sub_4264B0 per-thread
  :        sub_43FF50 (enqueue)      :     worker
  :        sub_43FFE0 (wait)         :
  :        sub_43FE70 (join)         :
  :   tag: "ptxas-lto"               :
  :                                  :
  : 8f. Post-LTO fixup               :
  :   if whole-program & all LTO:    :  "LTO on everything so
  :     remove libcudadevrt          :   remove libcudadevrt
  :   add compiled cubins to merge   :   from list"
  :   list                           :
  +..................................+
              |
              v
  PHASE 9   MERGE                     main() lines 1402--1607
  +------------------------------+
  | trace("read")                |  sub_4279C0
  | reverse module list          |  sub_4649E0
  |                              |
  | if -use-host-info / LTO:     |
  |   dead_code_eliminate()      |  sub_426AE0 (2,178 B)
  |     --> sub_44AD40 (22,503 B)|  callgraph-based sweep
  |                              |
  | for each module in v353:     |
  |   Mercury pre-link transform?|  sub_4275C0 (if sm>99
  |     check e_flags, call      |    and byte_2A5F221
  |     post_link_transform()    |    and byte_2A5F220)
  |                              |
  |   skip cudadevrt if not      |  sub_4448C0 checks refs
  |     needed? (no device refs) |  "ignore %s"
  |                              |
  |   merge_elf(elfw)            |  sub_45E7D0 (89,156 B)
  |     copy sections            |    weak resolution
  |     resolve symbols          |    sub_45D180 (26,816 B)
  |     merge .nv.info metadata  |
  | trace("merge")               |  sub_4279C0
  +------------------------------+
              |
              v
  PHASE 10  LAYOUT                    main() line 1429
  +------------------------------+
  | shared_memory_layout(elfw)   |  sub_439830 (65,776 B)
  |   per-entry shared mem       |    overlap set analysis
  |   allocation                 |    extern/local/reserved
  |                              |
  | (called from sub_439830):    |
  |   compute_entry_properties   |  sub_451D80 (97,969 B)
  |   register/barrier propagate |  sub_450ED0 (15,956 B)
  |   data overlap merge         |  sub_432B10 (11,683 B)
  |   constant dedup             |  sub_4339A0 (13,199 B)
  |   section sort & layout      |  sub_465720 (15,579 B)
  |   bindless processing        |  sub_438DD0 (12,779 B)
  | trace("layout")              |  sub_4279C0
  +------------------------------+
              |
              v
  PHASE 11  RELOCATE                  main() line 1432
  +------------------------------+
  | apply_relocations(elfw)      |  sub_469D60 (26,578 B)
  |   patch R_CUDA relocations   |
  |   in section data            |
  |                              |
  | (called from sub_469D60):    |
  |   UFT/UDT setup              |  sub_463F70 (3,978 B)
  |   UFT reorder                |  sub_4637B0 (10,141 B)
  |   resolved rela emission     |  sub_46ADC0 (11,515 B)
  | trace("relocate")            |  sub_4279C0
  +------------------------------+
              |
              v
  PHASE 12  FINALIZE                  main() line 1436
  +------------------------------+
  | finalize_elf(elfw)           |  sub_445000 (55,681 B)
  |   section predicate filter   |
  |   symbol reindexing          |
  |   section reindexing         |
  |   size validation            |
  |   entry property computation |
  |   resolved-rela emission     |
  |   section ordering + layout  |
  |   symbol section-idx patch   |
  |   ELF header finalization    |
  |                              |
  | (called from sub_445000):    |
  |   callgraph section build    |  sub_44D200 (8,545 B)
  |                              |
  | if verbose: dump_stats       |  sub_43D2A0
  | trace("finalize")            |  sub_4279C0
  +------------------------------+
              |
              |  +-- sm >= 100? (Mercury mode) -----------+
              |  |                                        |
              v  v                                        |
  PHASE 12.5 MERCURY FNLZR (post-link)                   |
  +..................................+                    |
  : (Mercury only, sm >= 100)        :                    |
  :                                  :                    |
  : elfw_calc_size(elfw)             :  sub_45C980        |
  : buffer = arena_alloc(size)       :  sub_4307C0        |
  : elfw_write_to_buffer(buf, elfw)  :  sub_45C950        |
  : post_link_transform(             :  sub_4275C0        |
  :   &buffer, filename, sm,         :    (3,989 B)       |
  :   &out_size, post_link=1)        :  FNLZR: capsule    |
  : fwrite(buffer, out_size, file)   :    mercury format   |
  +..................................+                    |
              |                                           |
              |  +-- sm < 100? (legacy ELF output) ------+
              |  |
              v  v
  PHASE 13  WRITE                     main() lines 1448--1491
  +------------------------------+
  | fopen(filename, "wb")        |
  |                              |
  | if Mercury (byte_2A5F222):   |
  |   [serialized by Phase 12.5] |
  |   fwrite(buffer, out_size)   |
  | else:                        |
  |   elfw_write_to_file(file,   |  sub_45C920
  |     elfw)                    |    calls sub_45BF00
  | fclose(file)                 |    (13,258 B)
  |                              |
  | if -register-link-binaries:  |  qword_2A5F2E0
  |   write DEFINE_REGISTER_FUNC |  fprintf per module
  |   header file                |
  |                              |
  | if -dot-file:                |  qword_2A5F2D0
  |   write callgraph .dot file  |  sub_44CCF0 (1,196 B)
  |                              |
  | trace("write")               |  sub_4279C0
  +------------------------------+
              |
              v
  PHASE 14  CLEANUP                   main() lines 1672--1688
  +------------------------------+
  | free module list             |  sub_464520
  | timer_cleanup(&v356)         |  sub_43D8E0
  | if byte_2A5F29C: cleanup     |  sub_468470 (temp files)
  |   temp files                 |
  | arena_destroy(v338, 0)       |  sub_431C70 (3,564 B)
  |   option parser arena        |
  | elfw_destroy(elfw)           |  sub_4475B0 (3,023 B)
  | arena_snapshot(v340, 0)      |  sub_45CAE0
  | arena_destroy(v339, 0)       |  sub_431C70
  |   memory space arena         |
  | if verbose: arena_dump_stats |  sub_431770 (8,491 B)
  |                              |
  | if errors: exit(-1)          |  checked via sub_44F410
  | else:      exit(0)           |
  +------------------------------+
```

## 14-Phase Pipeline Table

Every phase maps to a specific address range in `main()`. The "Entry function" column shows the primary function called from `main()` for each phase. The "Decompiled line" column references `decompiled/main_0x409800.c`. The "Size" column is the decompiled source size in bytes (a proxy for compiled function complexity). The "Timing tag" column shows the string passed to `sub_4279C0` at phase boundaries.

| # | Phase | Entry function | Address | Decompiled line | Size | Timing tag | What it does | Key sub-functions | Skip conditions |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Init | `arena_create_named` | `0x432020` | 377--381 | 2,161 B | `"init"` (shared) | Creates two named memory arenas ("nvlink option parser" and "nvlink memory space") and initializes the timing system | `sub_43D8C0` (timer init), `sub_45CAE0` (arena snapshot) | Never skipped |
| 2 | CLI parse | `nvlink_parse_options` | `0x427AE0` | 384 | 30,272 B | `"init"` (shared) | Parses 68 command-line options into ~80 global variables controlling all subsequent phases | — | Never skipped |
| 3 | Mode dispatch | inline in `main()` | `0x409800` | 385 | — | `"init"` (shared) | Checks `dword_2A77DC0`: values 1/2 branch to host linker script paths; value 0 (or >= 3) falls through to device link | — | Never skipped (but gates all subsequent phases) |
| 4 | Library resolve | `path_search_library` | `0x462870` | 387--424 | 4,905 B | `"init"` (shared) | Searches `-L` paths and `$LIBRARY_PATH` to resolve `-l` library flags into file paths, appends to input file list | `sub_4622D0` (create search ctx), `sub_462500` (add path), `sub_44EC40` (parse colon-separated) | Skipped in modes 1 and 2 |
| 5 | Context create | `elfw_create` | `0x4438F0` | 485--496 | 14,821 B | `"init"` (shared) | Creates the output ELF wrapper (`elfw`) with initial sections (.shstrtab, .strtab, .symtab, .note.nv.cuinfo, .note.nv.tkinfo) and "elfw memory space" arena | `sub_468560` (CUDA API version), `sub_451920` / `sub_444710` (ELF class setup) | Skipped in modes 1 and 2 |
| 6 | Config | inline in `main()` + callees | `0x409800` | 497--593 | varies | `"init"` | Configures Mercury mode, loads libdevice (LTO), sets stack canary, loads used-symbol lists, UIDX file, host info ELF, writes version string; emits `"init"` timing trace | `sub_4BC470` (libdevice), `sub_4389F0` (stack canary), `sub_43F360` / `sub_43F950` (used symbols), `sub_443730` (version) | Skipped in modes 1 and 2 |
| 7 | Input file loop | per-type dispatch | `0x409800` | 595--1741 | varies | `"read"` | Iterates input file list; reads 56-byte header; dispatches by file type (cubin/ptx/fatbin/nvvm/ltoir/bc/archive); registers modules; runs PTX JIT and FNLZR pre-link as needed | `sub_4BD760` (ptxas JIT), `sub_42AF40` (fatbin, 11,143 B), `sub_426570` (arch validate, 7,427 B), `sub_42A680` (register module, 11,939 B), `sub_4275C0` (FNLZR), `sub_427A10` (LTO add) | Always runs in mode 0/3; mode 2 runs it for module IDs only |
| 8 | LTO | `lto_collect_ir` / `lto_compile` | `0x426CD0` / `0x4BC6F0` | 910--1367 | 7,040 B / varies | `"cicc-lto"` / `"ptxas-lto"` | Collects IR modules, compiles via libnvvm (IR->PTX), assembles via ptxas (PTX->cubin), optionally using split-compile thread pool; removes libcudadevrt if whole-program | `sub_4BD4E0` (whole-program ptxas), `sub_4BD760` (single-module ptxas), `sub_43FDB0` (thread pool create), `sub_4264B0` (split worker), `sub_43FF50`/`sub_43FFE0`/`sub_43FE70` (pool ops) | Only if `byte_2A5F288` (-lto) is set |
| 9 | Merge | `merge_elf` | `0x45E7D0` | 1402--1607 | 89,156 B | `"merge"` | Reverses module list; optionally runs DCE; iterates modules and calls `merge_elf` for each (copies sections, resolves symbols, merges metadata); handles cudadevrt skip | `sub_45D180` (weak resolution, 26,816 B), `sub_44AD40` (DCE, 22,503 B), `sub_426AE0` (DCE wrapper, 2,178 B), `sub_4448C0` (device refs check) | Skipped in modes 1 and 2 |
| 10 | Layout | `shared_memory_layout` | `0x439830` | 1429 | 65,776 B | `"layout"` | Computes shared memory offsets per entry, propagates register/barrier counts through callgraph, deduplicates constants, sorts and lays out sections, processes bindless textures | `sub_451D80` (entry properties, 97,969 B), `sub_450ED0` (reg/bar propagate, 15,956 B), `sub_432B10` (data overlap, 11,683 B), `sub_4339A0` (const dedup, 13,199 B), `sub_465720` (section layout, 15,579 B), `sub_438DD0` (bindless, 12,779 B) | Skipped in modes 1 and 2 |
| 11 | Relocate | `apply_relocations` | `0x469D60` | 1432 | 26,578 B | `"relocate"` | Patches all R_CUDA and R_MERCURY relocations in section data bytes, sets up and reorders UFT/UDT unified function/data tables, emits resolved relocation entries | `sub_463F70` (UFT/UDT setup, 3,978 B), `sub_4637B0` (UFT reorder, 10,141 B), `sub_46ADC0` (resolved rela emission, 11,515 B) | Skipped in modes 1 and 2 |
| 12 | Finalize | `finalize_elf` | `0x445000` | 1436 | 55,681 B | `"finalize"` | Reindexes symbols and sections, computes final sizes and offsets, sorts sections into canonical ELF order, writes ELF header geometry/flag fields (not `e_type` — that was set by `elfw_create`), builds callgraph section | `sub_44D200` (callgraph build, 8,545 B), `sub_439640` (shared mem fixup, executable `ET_EXEC` branch only), `sub_44DB00` (metadata creation), `sub_438BD0` (virtual section remap, Mercury `0xFF00` branch only) | Skipped in modes 1 and 2 |
| 12.5 | Mercury FNLZR | `post_link_transform` | `0x4275C0` | 1454--1482 | 3,989 B | (within `"finalize"`) | Serializes the finalized ELF to a buffer, then runs the FNLZR finalizer with `post_link=1` to convert SASS cubin into capsule mercury format | `sub_45C980` (calc size), `sub_4307C0` (alloc), `sub_45C950` (write to buffer) | Only if `byte_2A5F222` (Mercury, sm >= 100) |
| 13 | Write | `elfw_write_to_file` / `fwrite` | `0x45C920` / `0x45BF00` | 1448--1671 | 13,258 B | `"write"` | Writes the output ELF (or Mercury capsule) to disk; optionally writes register-link-binaries C header and callgraph .dot file | `sub_45BF00` (serialize ELF, 13,258 B), `sub_44CCF0` (dot output, 1,196 B) | Output type varies by mode (ELF/script/C source) |
| 14 | Cleanup | `arena_destroy` / `elfw_destroy` | `0x431C70` / `0x4475B0` | 1672--1688 | 3,564 B / 3,023 B | — | Frees module list, destroys timer, cleans temp files, destroys option parser and memory space arenas, destroys elfw; exits with 0 or -1 | `sub_464520` (free list), `sub_43D8E0` (timer), `sub_468470` (temp files), `sub_431770` (arena stats dump) | Never skipped |

### Five Largest Functions in the Pipeline

These are the five largest functions by decompiled source size, all in the linker core:

| Rank | Function | Address | Size | Phase | Role |
|---|---|---|---|---|---|
| 1 | `compute_entry_properties` | `0x451D80` | 97,969 B | 10 (Layout) | Register/barrier count propagation through callgraph |
| 2 | `merge_elf` | `0x45E7D0` | 89,156 B | 9 (Merge) | Full section merge, symbol resolution, metadata merge |
| 3 | `shared_memory_layout` | `0x439830` | 65,776 B | 10 (Layout) | Overlap set analysis, per-entry shared memory allocation |
| 4 | `main` | `0x409800` | 57,970 B | All | 14-phase orchestrator (1,936 decompiled lines) |
| 5 | `finalize_elf` | `0x445000` | 55,681 B | 12 (Finalize) | Symbol/section reindexing, ELF header finalization |

## Timing Infrastructure

nvlink has a built-in timing system activated by an internal timing file path (global `qword_2A5F290`). The timing calls bracket each pipeline phase with string tags.

**Timing functions**:
- `sub_45CCD0` — start timer for a named phase
- `sub_45CCE0` — stop timer, record elapsed time

**Phase tag strings** (embedded in `main()` and referenced by `sub_4279C0`):

| Tag | Emitted at line | Pipeline phases covered |
|---|---|---|
| `"init"` | 593 | Phases 1--6: arena creation, option parsing, library resolution, context setup, config |
| `"read"` | 1403 | Phase 7 + 8 + 9: input file loop, PTX JIT, LTO compilation, merge loop |
| `"cicc-lto"` | 1100 | Phase 8 (IR compile): NVVM IR to PTX compilation via libnvvm |
| `"ptxas-lto"` | 1286 | Phase 8 (assembly): PTX to SASS assembly via embedded ptxas |
| `"merge"` | 1426 | Phase 9 boundary: after merge loop, before layout |
| `"layout"` | 1431 | Phase 10 boundary: after `sub_439830`, before relocate |
| `"relocate"` | 1434 | Phase 11 boundary: after `sub_469D60`, before finalize |
| `"finalize"` | 1440 | Phase 12 boundary: after `sub_445000`, before output |
| `"write"` | 1671 | Phase 13 boundary: after output is written, before cleanup |

The debug trace function `sub_4279C0` emits these tag strings to stderr when verbose debugging is enabled (`dword_2A5F308 & 0x20`), producing output of the form: `nvlink: phase <tag>`.

The timing tag structure reveals an important subtlety: the trace points are emitted at phase *boundaries*, not phase *starts*. Specifically, `"merge"` is emitted *after* the merge loop completes, and `"layout"` is emitted *after* `sub_439830` returns. This means each tag marks the transition *out of* its named phase. The `"init"` tag is the exception — it is emitted at the *end* of Phase 6, marking the transition from initialization to the input file loop.

## Three Code Paths

nvlink's mode dispatch (Phase 3) selects one of three fundamentally different code paths based on the global `dword_2A77DC0`. This global is set during option parsing based on `--gen-host-linker-script`, `--shared`, and the implicit device-link default.

### Path 1: Device Linking (mode 0 / default)

The default and most complex path. Runs the full 14-phase pipeline from Phase 1 through Phase 14. This is the path taken when `nvcc` invokes nvlink to combine separately compiled `.cubin` files into a final device executable.

```text
Input cubins --> merge --> layout --> relocate --> finalize --> write cubin
                                                     |
                                              sm>=100? --> FNLZR --> capsule mercury
```

Key characteristics:
- All 14 phases execute (Phase 8 conditional on `-lto`)
- The merge function (89KB) runs once per input object
- LTO Phase 8 interleaves if `-lto` is active
- Mercury FNLZR post-link transform applies for sm >= 100
- Output is a CUDA device ELF (cubin) or capsule mercury binary

### Path 2: Host Linker Script — Absolute (mode 1)

When `--gen-host-linker-script=lcs-abs` is specified, nvlink skips the core linking pipeline entirely and generates a host linker script containing `.nvFatBinSegment` section definitions. This script is consumed by the host `ld` to embed fat binaries into the host executable.

```text
Phases 1-3 --> write fixed SECTIONS { .nvFatBinSegment ... } --> exit(0)
```

The generated script:
```text
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin : { *(__nv_relfatbin) }
    .nv_fatbin : { *(.nv_fatbin) }
}
```

Key characteristics:
- Phases 4--12 are skipped entirely
- No merge, no relocation, no ELF output
- Output is a text linker script, not a binary
- Writes to output file (or stdout if no `-o`)
- Used by `nvcc`'s host compilation stage

### Path 3: Host Linker Script — Augmented (mode 2)

When `--gen-host-linker-script=lcs-aug` is active, nvlink generates a host linker script by running `ld --verbose` to extract the system linker's default script, then appending NVIDIA-specific sections. A validation step ensures the generated script is syntactically correct.

```text
Phases 1-3 --> construct gcc/collect2 flag extraction pipeline
           --> run ld --verbose to extract default script
           --> append .nvFatBinSegment sections
           --> validate with ld -T
           --> exit(0 or -1)
```

The shell pipeline constructed:
```bash
$(gcc -v 2>&1 | grep collect2 | grep -wo -e -pie -e "-z ..." -e "-m ..." | tr "\n" " ")
ld --verbose $(flags) | grep -Fvx -e "$(ld -V)" | sed '1,2d;$d' > output_file
ld -T output_file 2>&1 | grep 'no input files' > /dev/null  # validation
```

Key characteristics:
- Phases 4--12 are skipped entirely
- Invokes host `gcc` and `ld` via shell pipelines
- Falls back to mode 1 if validation fails
- More complex but produces a complete linker script

### Path Selection Logic

```text
dword_2A77DC0 value    Condition                              Code path
---------------------------------------------------------------------------
0                      (default)                               Full device link
1                      --gen-host-linker-script=lcs-abs        Host linker script (absolute)
2                      --gen-host-linker-script=lcs-aug        Host linker script (augmented)
```

The dispatch at line 385 uses `(unsigned int)(dword_2A77DC0 - 1) > 1` which is true for values 0 and >= 3 (device-link path) and false for values 1 and 2 (host-script paths).

## Data Flow Between Phases

The pipeline communicates through a small set of global data structures that accumulate state as phases execute. The diagram below traces the producer-consumer relationships.

```text
Phase 1-2  INIT/CLI
    |
    |  Produces:
    |    - Option parser arena       v338  (transient, freed after extraction)
    |    - Main memory arena         v339  "nvlink memory space"
    |    - ~80 global config flags   byte_2A5F2xx / dword_2A5Fxxx
    |    - Input file linked list    qword_2A5F330  (singly-linked: [next][filename])
    |    - Library search paths      qword_2A5F300 (-L paths), qword_2A5F2F8 (-l libs)
    v
Phase 4  LIBRARY RESOLVE
    |
    |  Consumes: qword_2A5F2F8 (unresolved -l flags)
    |  Mutates: qword_2A5F330 (appends resolved library paths)
    |  Uses: library_search context (transient, via sub_4622D0)
    v
Phase 5  CONTEXT CREATE
    |
    |  Produces:
    |    - Output ELF wrapper (elfw) v55, returned from elfw_create
    |      Contains: .shstrtab, .strtab, .symtab,
    |                .note.nv.cuinfo, .note.nv.tkinfo
    |    - elfw memory arena         "elfw memory space"
    |    - merge_flags bitfield      v44, assembled from ~15 option flags
    v
Phase 7  INPUT FILE LOOP
    |
    |  Consumes: input file linked list (qword_2A5F330)
    |  Produces:
    |    - Per-file: parsed ELF structures, validated arch
    |    - Module list (v353): singly-linked list of 80-byte records
    |      [0]=next, [8]=filename, [16]=cubin_data
    |    - Register-link module IDs (v354): for --register-link-binaries
    |    - LTO: collected IR module list (via sub_427A10)
    |    - JIT: compiled cubin objects (from PTX/fatbin members via sub_4BD760)
    v
Phase 8  LTO (optional)
    |
    |  Consumes: IR module list from Phase 7
    |  Produces: compiled cubin objects appended to v353 module list
    |  Side effects: may remove cudadevrt from v353
    v
Phase 9  MERGE
    |
    |  Consumes: v353 module list (all cubins from input + LTO + JIT)
    |  Mutates: output elfw (v55)
    |    - Copies sections from each input into output
    |    - Resolves symbols (global, weak, local) via sub_45D180
    |    - Merges .nv.info metadata
    |    - Removes dead code via sub_44AD40 if -use-host-info / -kernels-used
    v
Phase 10  LAYOUT
    |
    |  Consumes: merged elfw with all sections and symbols
    |  Mutates: elfw section addresses and properties
    |    - Shared memory: offset assignment per entry function (sub_439830)
    |    - Callgraph: register/barrier count propagation (sub_451D80, sub_450ED0)
    |    - Constants: deduplication via hash table (sub_4339A0)
    |    - Data: overlap merge (sub_432B10)
    |    - Sections: final ordering and address assignment (sub_465720)
    |    - Bindless: texture/surface resolution (sub_438DD0)
    v
Phase 11  RELOCATE
    |
    |  Consumes: laid-out elfw with resolved addresses
    |  Mutates: elfw section data (patches instruction/data bytes)
    |    - Processes all R_CUDA and R_MERCURY relocation entries
    |    - Sets up UFT (Unified Function Table) and UDT (Unified Data Table)
    |    - Reorders UFT entries for runtime dispatch
    |    - Emits resolved relocation entries for relocatable output
    v
Phase 12  FINALIZE
    |
    |  Consumes: relocated elfw
    |  Mutates: elfw structure (final pass)
    |    - Renumbers symbols and sections into canonical order
    |    - Computes final sizes and offsets for all sections
    |    - Builds .nv.callgraph section (sub_44D200)
    |    - Writes ELF header fields (e_shoff, e_phoff, etc.)
    |    - Mercury FNLZR-specific: virtual section index remapping
    v
Phase 12.5  MERCURY FNLZR (sm >= 100 only)
    |
    |  Consumes: finalized elfw
    |  Produces: capsule mercury binary buffer
    |    - Serializes elfw to byte buffer via sub_45C950
    |    - Passes buffer through sub_4275C0 with post_link=1
    |    - FNLZR converts SASS ELF sections into capsule mercury format
    |    - Result is a new buffer with Mercury section headers
    v
Phase 13  WRITE
    |
    |  Consumes: finalized elfw (or Mercury capsule buffer)
    |  Produces:
    |    - Output ELF file (via sub_45C920 -> sub_45BF00 -> fwrite)
    |      OR Mercury capsule binary (via fwrite of transformed buffer)
    |    - Optional: register-link-binaries .c header (DEFINE_REGISTER_FUNC)
    |    - Optional: callgraph .dot file (via sub_44CCF0)
    v
Phase 14  CLEANUP
    |
    |  Destroys: v353 module list (sub_464520)
    |  Destroys: timer context (sub_43D8E0)
    |  Destroys: elfw (sub_4475B0)
    |  Destroys: option parser arena (sub_431C70, v338)
    |  Destroys: memory space arena (sub_431C70, v339)
    |  Optional: arena_dump_stats (sub_431770) if verbose
```

### The Central Data Structure: `elfw`

The output ELF wrapper (`elfw`) is the single most important data structure in the pipeline. Created in Phase 5, it accumulates state across all subsequent phases:

- **Phase 5** (create): initialized with 5 built-in sections (.shstrtab, .strtab, .symtab, .note.nv.cuinfo, .note.nv.tkinfo)
- **Phase 6** (config): Mercury mode flag set, ELF class configured, version string written
- **Phase 7** (input loop): cubins validated against elfw's target arch via `sub_426570`
- **Phase 9** (merge): sections, symbols, and relocations are copied into it from each input object
- **Phase 10** (layout): section addresses are assigned, shared memory offsets are computed, properties are propagated
- **Phase 11** (relocate): relocation entries are resolved against the laid-out addresses, UFT/UDT tables constructed
- **Phase 12** (finalize): final patches are applied, sections renumbered, ELF header written
- **Phase 12.5** (Mercury): the finalized elfw is serialized and passed through FNLZR
- **Phase 13** (write): the elfw (or Mercury capsule) is serialized to a byte buffer and written to disk

The elfw is allocated on the "elfw memory space" arena created by `elfw_create`. Key fields include: `elfw[16]` (ELF type: 1=EXEC, 2=REL, 0xFF00=Mercury), `elfw[48]` (arch flags bitfield), `elfw[64]` (verbose/debug flags), `elfw[104]` (Mercury mode: 0/1/2).

## Mercury/FNLZR Post-Link Transform

For architectures with SM >= 100 (Blackwell and later), nvlink invokes the FNLZR (Finalizer) via `sub_4275C0` (3,989 bytes) at up to three distinct points in the pipeline. This is the mechanism by which nvlink produces capsule mercury binaries instead of plain SASS cubins.

### FNLZR Invocation Points

```text
  Point 1: Per-input cubin (Phase 7, lines 726-727, 834-835)
  +-----------------------------------------------+
  | Triggered when: sm > 0x59 AND byte_2A5F225    |
  |   AND sub_43DA40(cubin) returns mercury-capable|
  |   AND the is_mercury output flag is not set    |
  | Mode: pre-link (post_link=0)                   |
  | Purpose: Transforms individual input cubins    |
  |   before they enter the merge phase            |
  +-----------------------------------------------+
              |
              v
  Point 2: Per-LTO output (Phase 8, lines 1267-1269, 1309-1313)
  +-----------------------------------------------+
  | Triggered when: same conditions as Point 1    |
  |   applied to each LTO-compiled cubin           |
  | Mode: pre-link (post_link=0)                   |
  | Purpose: Transforms LTO-compiled cubins       |
  |   before merge                                 |
  +-----------------------------------------------+
              |
              v
  Point 3: Final output (Phase 12.5, lines 1454-1482)
  +-----------------------------------------------+
  | Triggered when: byte_2A5F222 is set (Mercury) |
  | Mode: post-link (post_link=1)                  |
  | Purpose: Converts the fully linked and        |
  |   finalized SASS cubin into capsule mercury    |
  |   format. This is the final transform before  |
  |   the binary is written to disk.               |
  | Flow:                                          |
  |   1. sub_45C980(elfw) -> size                  |
  |   2. sub_4307C0(0, size) -> buffer             |
  |   3. sub_45C950(buffer, elfw) -> serialize     |
  |   4. sub_4275C0(&buffer, name, sm,             |
  |        &out_size, 1) -> transform              |
  |   5. fwrite(buffer, 1, out_size, file)         |
  +-----------------------------------------------+
```

The distinction between pre-link (Points 1-2) and post-link (Point 3) is significant:

- **Pre-link** (`post_link=0`): Transforms individual cubin inputs to prepare their SASS sections for Mercury-aware merging. The FNLZR adjusts section headers and relocation types but does not produce the final capsule mercury container.

- **Post-link** (`post_link=1`): Transforms the fully linked ELF into capsule mercury format. The FNLZR replaces SASS code sections with Mercury binary sections, adds Mercury-specific section headers (sh_type values in the `0x70000000+` range), and produces the final binary format consumed by the CUDA runtime and driver.

The pre-link/post-link architecture means that for Mercury targets, the merge and layout phases operate on cubin sections that have already been partially transformed (Point 1), while the final Mercury formatting happens only after all linking is complete (Point 3). This two-phase approach avoids the need for Mercury-aware merge logic — the merge phase sees cubin-like sections with standard relocation types.

### Verbose-keep FNLZR output

When `--verbose-keep` (`byte_2A5F29B`) is active, Point 3 additionally extracts the pre-FNLZR ELF and writes it to a side file. The code at lines 1463-1479 saves the serialized buffer before FNLZR runs:

```c
printf("nvlink -extract %s -m%d -arch=%s -o %s\n", ...)
fwrite(filenameb, 1, v328, v334)    // pre-FNLZR cubin
sub_4275C0(&v367, filename, sm, ptr, 1)  // FNLZR transform
fwrite(v367, 1, ptr[0], v155)       // post-FNLZR mercury capsule
```

## LTO Pipeline Detail

When `-lto` is active, Phase 8 expands into a multi-step sub-pipeline that involves loading an external shared library and optionally spawning threads:

```text
Phase 8 LTO sub-pipeline
=========================

8a. Validate LTO options       lines 945--982
        |                      Check for incompatible flags:
        |                        -lineinfo (if mode==3)
        |                        -maxrregcount (mode conflicts)
        |                        -ftz, -prec-div, -prec-sqrt
        |                        -fmad, -split-compile
        |
8b. NVVM callback (if -vkeep) lines 985--1008
        |                      dlsym("__nvvmHandle") from libnvvm
        |                      handle(0xBEEF) -> callback_fn
        |                      callback_fn(ctx, sub_4299E0, 0, 0xF00D)
        |
8c. Collect IR modules         sub_426CD0 (7,040 B)    line 1010
        |                      Gather NVVM IR from all inputs
        |                      Returns module list + count
        |
8d. Compile IR to PTX          sub_4BC6F0              line 1014
        |                      dlopen libnvvm.so from --nvvmpath
        |                      Call nvvm API: IR -> PTX
        |                      Tag: "cicc-lto"
        |
8e. Assemble PTX to cubin      dispatch by mode:
        |
        +-- whole-program       sub_4BD4E0              line 1165
        |   (byte_2A5F286==0)   Single PTX -> single cubin
        |                       "whole program compile"
        |
        +-- single-module       sub_4BD760              line 1190
        |   (dword_2A5B514==1)  Single module -> relocatable cubin
        |                       "relocatable compile"
        |
        +-- split-compile       sub_43FDB0 + threads    line 1210
            (multiple modules)  sub_43FDB0 (create thread pool)
                                sub_43FF50 (enqueue sub_4264B0)
                                sub_43FFE0 (wait all)
                                sub_43FE70 (join all)
                                Each thread: PTX -> cubin
                                Tag: "ptxas-lto"
        |
8f. Post-LTO fixup             lines 1290--1367
        |                      If whole-program & all inputs had IR:
        |                        remove libcudadevrt from module list
        |                        "LTO on everything so remove
        |                         libcudadevrt from list"
        |                      Add compiled cubins to merge list
```

The LTO pipeline distinguishes two compilation strategies based on flags:

- **Whole-program LTO** (`--force-whole-lto` or auto-detected when `byte_2A5F286 == 0`): All IR modules are compiled as a single unit. The string `"whole program compile"` is emitted. Output is a non-relocatable cubin.
- **Partial LTO** (`--force-partial-lto` or auto-detected): Modules are compiled individually in relocatable mode. Useful when not all inputs have IR. The string `"relocatable compile"` is emitted.

A special case: when all inputs have LTO IR and whole-program compilation succeeds, nvlink removes `libcudadevrt` from the link list entirely (string: `"LTO on everything so remove libcudadevrt from list"`), since the device runtime is compiled directly into the output.

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

Most phases check for accumulated errors before proceeding to the next phase via `*(_BYTE *)(sub_44F410(ptr) + 1)`. Key error strings emitted during the pipeline:

| Phase | Error string | Descriptor |
|---|---|---|
| 7 (input) | `"cubin not an elf?"` | `unk_2A5B670` |
| 7 (input) | `"cubin not a device elf?"` | `unk_2A5B670` |
| 7 (input) | `"fatbin wrong format?"` | `unk_2A5B670` |
| 7 (input) | `"should only see nvvm files when -lto"` | `unk_2A5B670` |
| 7 (input) | `"should never see bc files"` | `unk_2A5B670` |
| 8 (LTO) | `"could not find __nvvmHandle"` | `unk_2A5B670` |
| 8 (LTO) | `"could not find CALLBACK Handle"` | `unk_2A5B670` |
| 8 (LTO) | `"error in LTO callback"` | `unk_2A5B670` |
| 8 (LTO) | `"Unable to create thread pool"` | `unk_2A5B670` |
| 8 (LTO) | `"Call to ptxjit failed in extended split compile mode"` | `unk_2A5B670` |
| 8 (LTO) | `"Cannot allocate pthread data"` | `unk_2A5B670` |
| 9 (merge) | `"merge_elf failed"` | `unk_2A5B670` |
| 9 (merge) | `"unexpected object after cudadevrt"` | `unk_2A5B670` |

## Key Global State

The pipeline's control flow and data flow depend on approximately 80 global variables set during Phase 2 (option parsing). The most architecturally significant ones:

| Global | Type | Set by | Controls |
|---|---|---|---|
| `dword_2A77DC0` | int | `-ghls` option | Linker mode: 0=device-link, 1=script-abs, 2=script-aug |
| `dword_2A5F314` | int | `--arch` | SM version number (e.g., 90, 100) |
| `byte_2A5F222` | bool | derived (sm>99) | Mercury mode — triggers FNLZR and capsule mercury output |
| `byte_2A5F225` | bool | derived (sm>89) | SASS mode — forces SASS output format |
| `byte_2A5F224` | bool | derived (sm>72) | New-style ELF flag — changes ELF class from 7 to 8 |
| `byte_2A5F288` | bool | `-lto` | LTO active — enables IR input acceptance and Phase 8 |
| `byte_2A5F286` | bool | derived | Partial LTO — set when LTO produces relocatable output |
| `byte_2A5F284` | bool | `--force-whole-lto` | Forces whole-program LTO compilation |
| `byte_2A5F285` | bool | `--force-partial-lto` | Forces partial (relocatable) LTO compilation |
| `byte_2A5F1E8` | bool | `-r` | Relocatable link — produces ET_REL instead of executable |
| `byte_2A5F2C1` | bool | derived | Output-is-archive flag |
| `byte_2A5F2C2` | bool | `-r` (variant) | Relocatable link flag (second copy) |
| `qword_2A5F330` | ptr | option parsing | Input file linked list head |
| `qword_2A5F278` | ptr | `--nvvmpath` | Path to libnvvm.so for LTO |
| `qword_2A5F2E0` | ptr | `--register-link-binaries` | Output path for DEFINE_REGISTER_FUNC header |
| `qword_2A5F2D0` | ptr | `--dot-file` | Output path for callgraph .dot file |
| `dword_2A5B528` | int | derived | Compilation mode enum: 0=normal, 2=archive, 4=lto, 6=SASS |
| `dword_2A5B514` | int | `--split-compile-extended` | LTO split-compile thread count (1=single-threaded) |
| `byte_2A5F2D8` | bool | `-v` | Verbose output |
| `dword_2A5F308` | int | various | Debug/verbose flags bitfield |
| `byte_2A5F29B` | bool | `-vkeep` | Verbose-keep mode (dump intermediates) |
| `byte_2A5F29A` | bool | `--emit-ptx` | Stop LTO after PTX generation |
| `byte_2A5F214` | bool | derived | DCE enabled (use-host-info or kernels-used) |
| `qword_2A5F290` | ptr | internal | Timing context (non-NULL when timing enabled) |
| `qword_2A5F318` | ptr | `--arch` | Architecture name string (e.g., "sm_100") |
| `dword_2A5F30C` | int | `--machine` | Machine word size (32 or 64) |

These globals are read throughout the pipeline to gate code paths. For example, Phase 8 (LTO) only executes when `byte_2A5F288` is set, Phase 12.5 only runs when `byte_2A5F222` (Mercury mode) is true, and DCE (in Phase 9) only runs when `byte_2A5F214` is set.

## Phase Dependencies and Skip Conditions

Not all phases run in every invocation. The table below shows which phases execute in each mode:

```text
Phase      Device link   Host script    Augmented     Cond. in
           (mode 0)      (mode 1)       (mode 2)      device link
------     -----------   -----------    ---------      -----------
1  Init    YES           YES            YES            always
2  CLI     YES           YES            YES            always
3  Mode    YES           YES            YES            always
4  Lib     YES           no             no             always
5  Ctx     YES           no             no             always
6  Config  YES           no             no             always
7  Input   YES           no             YES (partial)  always
8  LTO     conditional   no             no             byte_2A5F288
9  Merge   YES           no             no             always
10 Layout  YES           no             no             always
11 Reloc   YES           no             no             always
12 Final   YES           no             no             always
12.5 Merc  conditional   no             no             byte_2A5F222
13 Write   YES (ELF)     YES (script)   YES (C src)    always (varies)
14 Clean   YES           YES            YES            always
```

In device-link mode (mode 0), all 14 phases execute with Phase 8 and 12.5 conditional. In host-linker-script mode (mode 1), only Phases 1--3, 13 (script generation), and 14 execute. In augmented mode (mode 2), Phases 1--3, 7 (partial — for module ID extraction), 13 (C source generation and script), and 14 execute.

### Conditional Phase Details

**Phase 8 (LTO) conditions**: Requires `byte_2A5F288` to be set. Additionally, if no IR modules were collected during Phase 7 (`!dword_2A5F280`), a warning is emitted and LTO is disabled (`byte_2A5F288 = 0`). Several option-conflict checks (for `-lineinfo`, `-maxrregcount`, etc.) gate the LTO sub-steps.

**Phase 9 (Merge) sub-conditions**:
- Dead code elimination runs only if `byte_2A5F214` is set AND (`byte_2A5F288` is false OR `byte_2A5F285` is true)
- Per-module Mercury pre-link transform runs if `byte_2A5F221` AND `byte_2A5F220`
- cudadevrt is skipped if `!byte_2A5F2C2` AND no device refs (`sub_4448C0` returns false)

**Phase 12.5 (Mercury FNLZR) conditions**: Requires `byte_2A5F222` (Mercury mode, sm >= 100). When this is false, Phase 13 writes the ELF directly via `sub_45C920`.

## Performance Characteristics

The pipeline is single-threaded except for two points:

1. **LTO split compilation** (Phase 8): `sub_43FDB0` creates a pthread thread pool, and `sub_4264B0` is dispatched to each thread for parallel PTX-to-SASS compilation. Thread count is controlled by `dword_2A5B514` (`--split-compile-extended`). If not set, `sub_43FD90` queries the available CPU count.

2. **Memory arena allocation** (all phases): the arena allocator (`sub_4307C0`) is thread-safe with per-arena mutexes, supporting concurrent allocation from the LTO thread pool.

### Bottleneck Analysis

| Phase | Complexity | Bottleneck characteristics |
|---|---|---|
| 7 (Input) | O(files) | Dominated by file I/O and ptxas JIT compilation for PTX inputs |
| 8 (LTO) | O(IR size) | Dominated by libnvvm compile time (`"cicc-lto"`) and ptxas assembly (`"ptxas-lto"`); parallelizable via split-compile |
| 9 (Merge) | O(files * sections) | `merge_elf` (89KB) runs once per input; each call traverses the input's full section table with symbol resolution |
| 10 (Layout) | O(functions^2) | Callgraph propagation in `compute_entry_properties` (97KB) is the theoretical bottleneck; shared memory overlap analysis is O(functions * overlapping_sets) |
| 11 (Relocate) | O(relocations) | Linear in the number of relocation entries |
| 12 (Finalize) | O(sections + symbols) | Linear in output size |

For typical workloads (small-to-medium cubin count, no LTO), the `"merge"` timing tag dominates. For LTO builds, `"cicc-lto"` and `"ptxas-lto"` dominate overwhelmingly since they invoke full compiler backends. For large CUDA applications with many separately-compiled kernels, the `"layout"` phase can become significant due to the O(functions^2) callgraph propagation.

## Cross-References

### Pipeline Phase Pages
- [Entry Point & Main](entry.md) — `main()` at `0x409800`: the 57,970-byte orchestrator function, with per-phase line-by-line walkthrough
- [CLI Option Parsing](cli-options.md) — Phase 2: parser infrastructure, option entry layout, global variable map (68 registered options)
- [Mode Dispatch](mode-dispatch.md) — Phase 3: device link vs. host linker script vs. augmented; `dword_2A77DC0` encoding, compilation mode enum
- [Library Resolution](library-resolution.md) — Phase 4: `LIBRARY_PATH` env search, `-L`/`-l` flag resolution, `sub_462870` search algorithm
- [Input File Loop](input-loop.md) — Phase 7: file type detection (56-byte header), per-format dispatch, module registration, PTX JIT path
- [Merge Phase](merge.md) — Phase 9: `merge_elf` (89KB), weak symbol resolution (`sub_45D180`), section/symbol merging, cudadevrt handling
- [Layout Phase](layout.md) — Phase 10: shared memory overlap analysis (`sub_439830`), entry property computation (`sub_451D80`), constant dedup, section layout
- [Relocation Phase](relocate.md) — Phase 11: `apply_relocations` (27KB), R_CUDA/R_MERCURY dispatch, UFT/UDT processing, resolved rela emission
- [Finalization Phase](finalize.md) — Phase 12: `finalize_elf` (56KB), symbol/section reindexing, callgraph build (`sub_44D200`), ELF header finalization
- [Output Phase](output.md) — Phase 13: ELF serialization (`sub_45BF00`), Mercury capsule write path, dot-file output, register-link-binaries header

### Input Processing Pages
- [File Type Detection](../input/file-type-detection.md) — 56-byte header probe and magic number classification
- [Cubin Loading](../input/cubin-loading.md) — cubin validation, arch checking (`sub_426570`), FNLZR pre-link dispatch
- [Fatbin Extraction](../input/fatbin-extraction.md) — fatbin container format (`0xBA55ED50` magic), architecture matching, member extraction
- [PTX Input & JIT](../input/ptx-input.md) — embedded ptxas compilation path (`sub_4BD760`) for PTX inputs
- [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md) — IR module registration (`sub_427A10`) and LTO prerequisites
- [Archive Processing](../input/archives.md) — `.a` archive iteration (`sub_4BDAC0`/`sub_4BDAF0`) and libcudadevrt handling

### Supporting Subsystems
- [CLI Flags Reference](../config/cli-flags.md) — all 68 flags with types, defaults, visibility
- [Timing Infrastructure](../infra/timing.md) — CSV timing output format, `sub_45CCD0`/`sub_45CCE0` start/stop, phase tag strings
- [Error Reporting](../infra/error-reporting.md) — the five-level diagnostic system (`sub_467460`), descriptor table at `unk_2A5Bxxx`
- [Memory Arenas](../infra/memory-arenas.md) — arena-based allocation (`sub_4307C0`) backing the pipeline, thread-safe with per-arena mutexes
- [LTO Overview](../lto/overview.md) — Phase 8 LTO sub-pipeline detail: libnvvm integration, split compilation, whole-program vs. partial
- [Mercury Overview](../mercury/overview.md) — Mercury/CapMerc processing for sm >= 100, capsule mercury binary format

### Sibling Wikis
- **ptxas wiki**: [Pipeline Overview](../../ptxas/pipeline/overview.html) — standalone ptxas 159-phase compilation pipeline; the same compiler is embedded in nvlink for PTX JIT and LTO assembly
- **cicc wiki**: [Pipeline Overview](../../cicc/pipeline/overview.html) — cicc CUDA compiler pipeline; its `libnvvm.so` is loaded via `dlopen` during LTO Phase 8

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| 14-phase pipeline structure with named phases | **HIGH** | All phase functions verified in `decompiled/`; timing tags confirmed in `nvlink_strings.json` |
| `main()` at `0x409800`, 57,970 bytes | **HIGH** | `decompiled/main_0x409800.c` exists, 1,936 lines |
| Phase table function addresses and sizes | **HIGH** | All addresses verified against decompiled files: `sub_432020` (2,161 B), `sub_427AE0` (30,272 B), `sub_4438F0` (14,821 B), `sub_462870` (4,905 B), `sub_42AF40` (11,143 B), `sub_45E7D0` (89,156 B), `sub_439830` (65,776 B), `sub_469D60` (26,578 B), `sub_445000` (55,681 B), `sub_45BF00` (13,258 B) |
| Decompiled line numbers for each phase | **HIGH** | Cross-verified against `main_0x409800.c`: line 377 (init), 384 (parse), 385 (dispatch), 387-424 (lib resolve), 485 (elfw_create), 595 (input loop), 910 (LTO), 1402 (merge), 1429 (layout), 1432 (relocate), 1436 (finalize), 1454-1482 (Mercury FNLZR), 1448 (write), 1672 (cleanup) |
| Five largest functions ranking | **HIGH** | `compute_entry_properties` = 97,969 B, `merge_elf` = 89,156 B, `shared_memory_layout` = 65,776 B, `main` = 57,970 B, `finalize_elf` = 55,681 B — all verified |
| Mode dispatch: `dword_2A77DC0` values 0/1/2 | **HIGH** | Verified in `main_0x409800.c` line 385: `(dword_2A77DC0 - 1) > 1` dispatches 0 to device-link, 1 and 2 to host-script paths; mode-dispatch.md confirms 0=device, 1=abs, 2=aug |
| Timing tag strings and emission lines | **HIGH** | All 9 timing tags verified in decompiled source: `"init"` (line 593), `"read"` (1403), `"cicc-lto"` (1100), `"ptxas-lto"` (1286), `"merge"` (1426), `"layout"` (1431), `"relocate"` (1434), `"finalize"` (1440), `"write"` (1671) |
| Timing functions `sub_45CCD0` / `sub_45CCE0` | **HIGH** | Both files exist in `decompiled/` |
| Error severity levels | **HIGH** | All five prefix strings found in `nvlink_strings.json` at consecutive addresses |
| Error system at `sub_467460` -> `sub_467A70` | **HIGH** | Both files exist in `decompiled/` |
| Mercury FNLZR three invocation points | **HIGH** | Lines 726-727 (per-input), 1267-1269 (per-LTO), 1454-1482 (final output) all verified in `main_0x409800.c`; `sub_4275C0` confirmed at 3,989 B |
| Mercury capsule mercury output flow (serialize -> FNLZR -> fwrite) | **HIGH** | Line 1454: `sub_45C980` (calc size), line 1456: `sub_4307C0` (alloc), line 1462: `sub_45C950` (write to buffer), line 1481: `sub_4275C0` (FNLZR with post_link=1), line 1482: `fwrite` |
| LTO sub-pipeline: `sub_426CD0` (7,040 B), `sub_4BC6F0`, `sub_43FDB0` | **HIGH** | All files exist in `decompiled/`; split-compile worker `sub_4264B0` confirmed |
| `"whole program compile"` / `"relocatable compile"` strings | **HIGH** | Both found in `nvlink_strings.json` |
| `"LTO on everything so remove libcudadevrt from list"` | **HIGH** | String at line 1350 of `main_0x409800.c`, verified in `nvlink_strings.json` |
| DCE at `sub_44AD40` (22,503 B) with wrapper `sub_426AE0` (2,178 B) | **HIGH** | Both decompiled files exist with matching sizes |
| Weak symbol resolution at `sub_45D180` (26,816 B) | **HIGH** | Decompiled file exists with matching size |
| Thread pool for split compilation (`sub_43FDB0`, `sub_4264B0`) | **HIGH** | Both decompiled files exist; thread pool API (`sub_43FF50` enqueue, `sub_43FFE0` wait, `sub_43FE70` join) all confirmed |
| Layout sub-functions (sub_451D80 97,969 B, sub_450ED0 15,956 B, sub_432B10 11,683 B, sub_4339A0 13,199 B, sub_465720 15,579 B, sub_438DD0 12,779 B) | **HIGH** | All decompiled files exist with matching sizes |
| Relocate sub-functions (sub_463F70 3,978 B, sub_4637B0 10,141 B, sub_46ADC0 11,515 B) | **HIGH** | All decompiled files exist with matching sizes |
| Finalize sub-function (sub_44D200 callgraph build, 8,545 B) | **HIGH** | Decompiled file exists with matching size |
| `elfw` created in Phase 5, used through Phase 13 | **HIGH** | `elfw_create` at `0x4438F0` verified; elfw object (`v55`) referenced in every subsequent phase function call |
| Arena allocator `sub_4307C0` is thread-safe | **MEDIUM** | Function exists; thread-safety inferred from mutex calls in decompiled code but not directly confirmed via code audit |
| Phase dependency table (which phases run conditionally) | **HIGH** | Conditional execution traced through `main_0x409800.c` control flow: mode check at line 385, LTO check at line 911, Mercury check at line 1452 |
| 68 registered options in Phase 2 | **HIGH** | Verified via option registration count in `sub_427AE0` |
| Data flow diagram between phases | **MEDIUM** | Structural match: globals set in Phase 2, elfw created in Phase 5, sections merged in Phase 9, etc. Individual field offsets within elfw are editorial interpretation based on access patterns |
| Performance bottleneck analysis (O-notation) | **MEDIUM** | Complexity classes inferred from loop structures in decompiled code; actual runtime depends on input characteristics |
