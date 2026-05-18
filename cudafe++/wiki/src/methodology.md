# Methodology

This page documents the reverse engineering methodology used to produce every page in this wiki. The goal is full transparency: a reader should be able to reproduce any finding by following the same techniques against the same binary. Every claim in the wiki traces back to one of four evidence categories (CONFIRMED, HIGH, MEDIUM, LOW), and this page defines exactly what each level means, what tools produced the raw data, and how that data was refined into the structured documentation that follows.

## Toolchain

| Component | Version | Role |
|---|---|---|
| IDA Pro | 9.0 (64-bit) | Interactive disassembler and database host |
| Hex-Rays | x86-64 decompiler (IDA 9.0 bundled) | Pseudocode generation for all 6,501 functions |
| IDAPython | 3.x (IDA-embedded) | Scripted extraction via `analyze_cudafe++.py` (531 lines) |
| Target binary | `cudafe++` from CUDA Toolkit 13.0 | ELF 64-bit, statically linked, stripped, 8,910,936 bytes |
| IDA database | `cudafe++.i64` | 247 MB analysis state (all function boundaries, xrefs, type info, decompilation caches) |

The binary was loaded into IDA Pro 9.0 with default x86-64 analysis settings. IDA's auto-analysis resolved all code/data boundaries, generated function boundaries for 6,501 functions, and identified 52,489 string literals. The Hex-Rays decompiler was invoked on all 6,501 functions; the IDAPython extraction log reports 6,343 successful decompilations (the remaining 158 failures are exception personality routines, SoftFloat leaf functions, and tiny thunks where Hex-Rays cannot reconstruct a valid C AST). However, due to function-name collisions in the output filenames (multiple `sub_XXXXXX` entries mapping to the same sanitized name after `/` replacement), the actual decompiled output directory contains **6,202 unique `.c` files** -- the number used throughout this wiki.

## Extraction Script

All raw data was exported from the IDA database in a single automated pass using `analyze_cudafe++.py`, an IDAPython script that runs inside IDA's scripting environment. The script produces 12 output artifacts:

| Artifact | File | Records | Size | Description |
|---|---|---|---|---|
| String table | `cudafe++_strings.json` | 52,489 strings | 9.2 MB | Every string literal with address, type, and all cross-references |
| Function table | `cudafe++_functions.json` | 6,501 functions | 12 MB | Address, size, instruction count, callers, callees per function |
| Import table | `cudafe++_imports.json` | 142 imports | 16 KB | Imported PLT symbols (glibc wrappers in static binary) |
| Segment table | `cudafe++_segments.json` | 26 segments | 3.3 KB | ELF section addresses, sizes, types, permissions |
| Cross-reference table | `cudafe++_xrefs.json` | 1,243,258 xrefs | 154 MB | Every code and data xref with source function attribution |
| Comment table | `cudafe++_comments.json` | 22,911 comments | 2.0 MB | All IDA comments (regular + repeatable) |
| Name table | `cudafe++_names.json` | 54,771 names | 3.5 MB | All named locations (IDA auto-names + user-defined) |
| Call graph | `cudafe++_callgraph.json` + `.dot` | 67,756 edges | 7.4 MB | Complete inter-procedural call graph (5,057 unique callers, 5,382 unique callees) |
| `.rodata` dump | `cudafe++_rodata.bin` | 2,599,011 bytes | 2.5 MB | Raw bytes of the read-only data section |
| Disassembly | `disasm/<func>_<addr>.asm` | 6,342 files | 86 MB | Per-function annotated disassembly with hex bytes |
| CFG graphs | `graphs/<func>_<addr>.json` + `.dot` | 12,684 files | 184 MB | Per-function basic-block graph with instructions and edges (JSON + DOT) |
| Decompiled code | `decompiled/<func>_<addr>.c` | 6,202 files | 38 MB | Hex-Rays pseudocode per function |

### Script Architecture

The script is structured as a `main()` function that calls `idaapi.auto_wait()` to block until IDA's auto-analysis completes, then executes 12 extraction passes in a fixed order. Output is written to four directories: the root output directory (JSON databases), `graphs/` (per-function CFGs), `disasm/` (per-function disassembly), and `decompiled/` (per-function pseudocode). Directories are created if they do not exist.

The 12 passes, in execution order:

1. **`export_all_strings()`** -- Enumerates `idautils.Strings()`, then for each string walks `XrefsTo(string_ea)` to record every function that references it. Each string entry captures the address, string value, string type code, and a list of xref records (`{from_addr, func_name, xref_type}`). This is the foundation for source attribution (see below).

2. **`export_all_functions()`** -- For each function in `idautils.Functions()`, records start/end address, size, instruction count (via `idc.is_code()` on each head), library flag (`FUNC_LIB`), thunk flag (`FUNC_THUNK`), and builds caller/callee lists. Callers are found via `XrefsTo(func_start)`; callees via `XrefsFrom(head)` filtered to call-type xrefs (`fl_CN` = type 17, `fl_CF` = type 19).

3. **`export_imports()`** -- Enumerates all imported modules via `idaapi.get_import_module_qty()` and `idaapi.enum_import_names()`. Records module name, symbol name, address, and ordinal for each of the 142 glibc imports.

4. **`export_segments()`** -- Iterates `idautils.Segments()` to record each ELF section's name, start/end address, size, type code, and permission bits.

5. **`export_xrefs()`** -- Full enumeration of all cross-references from every instruction head in every function. For each xref, records source address, source function, target address, target function (if any), and xref type code. Produces the 1,243,258-record xref table. The six xref type codes in the output:

   | Type | Code | Count | Meaning |
   |---|---|---|---|
   | `dr_O` | 1 | 29,631 | Data offset reference |
   | `dr_W` | 2 | 11,488 | Data write reference |
   | `dr_R` | 3 | 42,364 | Data read reference |
   | `fl_CN` | 17 | 67,756 | Code near call |
   | `fl_CF` | 19 | 189,364 | Code far/ordinary flow |
   | `fl_JN` | 21 | 902,655 | Code near jump (including fall-through) |

6. **`export_comments()`** -- Walks every instruction head in the database via `idautils.Heads()`, extracting both regular comments (`idc.get_cmt(ea, 0)`) and repeatable comments (`idc.get_cmt(ea, 1)`).

7. **`export_names()`** -- Iterates `idautils.Names()` to export all named locations (function names, data labels, IDA auto-generated names).

8. **`extract_rodata()`** -- Reads the raw bytes of the `.rodata` segment via `ida_bytes.get_bytes()` and writes them to a binary file. Used for offline string scanning and jump table analysis.

9. **`export_callgraph()`** -- Builds the 67,756-edge call graph by iterating every function and scanning its instruction heads for outgoing call xrefs (`fl_CN`, `fl_CF`). Output in both JSON (array of `{from, from_addr, to, to_addr}` edge records) and Graphviz DOT format (67,759 lines).

10. **`export_complete_disassembly()`** -- Per-function disassembly files. For each function, iterates all instruction heads within the function's address range, generating hex byte dumps alongside disassembly text via `idc.generate_disasm_line()`. Each file includes a header with function name, address range, and byte size.

11. **`export_function_graphs()`** -- Per-function control flow graphs via `idaapi.FlowChart()`. For each basic block: block ID, start/end address, size, and full instruction listing. Block-to-block edges (fall-through and branch targets) are extracted via `block.succs()`. Output as both JSON (structured blocks + edges) and DOT (for Graphviz visualization).

12. **`export_decompilation()`** -- Calls `idaapi.init_hexrays_plugin()` to initialize the Hex-Rays decompiler, then iterates all functions and calls `idaapi.decompile(func_ea)`. On success, the pseudocode string (`str(cfunc)`) is written to a `.c` file with a header comment containing the function name and address. Failures are silently caught via a bare `except Exception` and skipped.

The script is invoked via IDA's headless batch mode or interactive scripting console. It does not call `qexit()` at the end, allowing the IDA database to remain open for further interactive analysis after extraction. Total extraction time is approximately 30-45 minutes on a workstation-class machine, dominated by the 6,501 decompilation calls in pass 12.

## Source Attribution Technique

The single most powerful technique in this analysis is **source attribution via `__FILE__` strings**. The EDG C++ frontend uses C-style assertions throughout its codebase. When an assertion fires, the handler receives the source file path, line number, and function name as compile-time string constants embedded by the `__FILE__`, `__LINE__`, and `__func__` macros. Because the binary is stripped (no `.symtab`), these assertion strings are the only surviving link to the original source tree.

### The Assert Handler

The central assert handler is `sub_4F2930`, located in `error.c`. It is a `__noreturn` function that formats and emits an internal compiler error message, then terminates the process. A total of **2,139 functions** in the binary call `sub_4F2930`, with **5,178 total call sites** (many functions have multiple assertion points throughout their bodies).

The highest-density callers are the **235 assert stubs** in the region `0x403300`--`0x408B40`. Each stub is exactly 29 bytes: three register loads (source file path via `lea rdi`, line number via `mov esi`, function name via `lea rdx`) followed by a `call` to `sub_4F2930`:

```
sub_403300:         ; assert stub for is_aliasable (attribute.c:10897)
  lea  rdi, aAttributeC    ; "/dvs/p4/.../EDG_6.6/src/attribute.c"
  mov  esi, 10897           ; line number (integer, not string)
  lea  rdx, aIsAliasable   ; "is_aliasable"
  call sub_4F2930           ; internal_error(__FILE__, __LINE__, __func__)
```

Of the 235 stubs, 200 reference `.c` file paths and 35 reference `.h` file paths (inlined assertions from header files). The stubs are sorted approximately by source file name within the stub region -- the linker grouped them from all 52 `.c` compilation units into one contiguous block.

Beyond the dedicated stubs, 1,904 additional functions contain inline assertion checks: the `lea rdi, <file_path>` instruction appears within the function body at the assertion site, not in a separate stub. These inline assertions provide the same source-file attribution as the stubs.

### The Attribution Chain

The attribution chain works in three steps:

1. **String discovery.** Extract all strings matching the EDG build path prefix `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/`. This yields one string per source file, each cross-referenced by the assert stubs that load it.

2. **Xref tracing.** For each assert stub, follow `XrefsTo()` to find which main-body functions call it. A function at `0x40DFD0` that calls the `attribute.c:5108` stub was compiled from `attribute.c`. This attributes the caller to the source file.

3. **Range extension.** Assert stubs are sparse -- not every function contains an assertion. Once a set of functions in a contiguous address range are attributed to the same source file, the entire range is assigned to that file. This works because the linker places all object code from a single `.c` file contiguously, and the files are arranged roughly alphabetically by filename.

This technique attributed 2,209 functions (34.0% of the binary) to specific source files. The remaining 4,292 functions fall into three categories: C++ runtime code (1,085 functions from libstdc++/glibc, identifiable by address range), PLT/init stubs (283 functions), and unmapped EDG functions (2,924 functions that contain no assertions and cannot be confidently attributed).

### Build Path

The full build path embedded in the binary is:

```
/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/
```

This reveals the NVIDIA internal Perforce depot structure (`/dvs/p4/`), the release branch (`r13.0`), and the EDG version (`EDG_6.6`). It confirms the binary was built from EDG C++ Front End version 6.6, licensed from Edison Design Group.

## Confidence Levels

Every identification in the raw sweep reports and wiki pages carries one of four confidence levels:

| Level | Tag | Criteria | Example |
|---|---|---|---|
| **CONFIRMED** | Direct match | The function's identity is proven by an assertion string that encodes the exact function name, source file, and line number. No ambiguity. | `sub_403300` loads `"is_aliasable"` + `"attribute.c"` + `"10897"` -- it is the assertion stub for `is_aliasable()` in `attribute.c` at line 10897. |
| **HIGH** | String + callgraph | The function references a distinctive string (error message, format string, keyword literal) AND its position in the call graph is consistent with a single plausible identity. | `sub_459630` references 276 CLI flag strings and is called from `main()` at the position where command-line processing occurs -- identified as `proc_command_line()`. |
| **MEDIUM** | Pattern + context | The function matches a known EDG pattern (struct layout access, IL node walking, type query) and its address falls within the expected source file range, but no string or assertion directly confirms the identity. | A function at `0x5B3000` accesses the IL node kind field at the expected struct offset and falls within the `il.c` address range -- likely an IL accessor, but the specific function name is inferred. |
| **LOW** | Address proximity | The function's address falls within a source file's range, but no internal evidence (strings, struct accesses, callees) distinguishes it from neighboring functions. The attribution is based solely on the linker's contiguous placement of object code. | A small leaf function at `0x5B2F80` sits between two `il.c`-attributed functions -- probably from `il.c`, but it could be an inlined header function. |

In practice, approximately 34% of functions are CONFIRMED (via assert strings), ~20% are HIGH (via distinctive strings or unique callgraph positions), ~25% are MEDIUM, and ~21% are LOW or unattributed.

## Terminology

The wiki uses a fixed vocabulary drawn from three sources: EDG's internal source tree, NVIDIA's CUDA extensions, and IDA Pro / Hex-Rays naming conventions. Every recurring term is defined in the dedicated [Glossary](./glossary.md) page, which groups entries by domain and cross-links to the wiki pages that develop each concept in depth.

## Call Graph Analysis

The complete call graph contains **67,756 edges** connecting the 6,501 functions. This graph is the primary tool for understanding system architecture -- which subsystems call which, where the hot paths are, and how NVIDIA's additions integrate with the EDG base.

### Hub Identification

Hub functions -- those with exceptionally high in-degree (many callers) or out-degree (many callees) -- reveal the architectural spine of the compiler:

| Hub Type | Function | Description | Degree |
|---|---|---|---|
| Top callee | `sub_4F2930` | `internal_error` handler | 235+ callers (every assert stub) |
| Top callee | Type query functions (104 total) | `is_class_or_struct_or_union_type`, etc. | 407 call sites for top query |
| Top caller | `sub_7A40A0` | `process_translation_unit` | Calls into parser, IL, type system |
| Top caller | `sub_459630` | `proc_command_line` (4,105-line monster) | Touches 276 flag variables |
| Top caller | `sub_585DB0` | `fe_one_time_init` | 36 subsystem initializer calls |
| Cross-module bridge | `sub_6BCC20` | Lambda preamble injection (NVIDIA) | Called from EDG statement handlers |

### Graph Structure

The call graph exhibits a layered structure typical of compiler frontends:

1. **Entry layer.** `main()` at `0x408950` calls exactly 8 stage functions in sequence.
2. **Stage layer.** Each stage function (init, CLI, parse, wrapup, backend) fans out to dozens of subsystem entry points.
3. **Core layer.** The parser (`expr.c`, `decls.c`, `statements.c`) calls into the type system (`types.c`, `exprutil.c`), IL builder (`il.c`, `il_alloc.c`), and name lookup (`lookup.c`, `scope_stk.c`).
4. **Leaf layer.** Memory management (`mem_manage.c`), error reporting (`error.c`), and type queries form the bottom of the call hierarchy, referenced from almost every subsystem.

NVIDIA's `nv_transforms.c` sits as a lateral extension at the core layer: it is called from `class_decl.c`, `cp_gen_be.c`, and `statements.c` (via `nv_transforms.h` inlines), but does not itself call back into the EDG parser. This clean separation suggests NVIDIA modifies the EDG source minimally, preferring to hook into existing EDG extension points rather than fork the core.

## String-Based Discovery

The binary contains **52,489 strings** in `.rodata`. These strings are the second most important evidence source after the assertion paths. Major categories:

| Category | Approximate Count | Usage |
|---|---|---|
| EDG assertion paths (`/dvs/p4/...`) | 65 (52 `.c` + 13 `.h`) | Source attribution |
| CUDA keyword strings | ~300 | Keyword table initialization, CLI flag names |
| Error message templates | ~3,800 | Diagnostic emission (`off_88FAA0` error table, 3,795 entries) |
| C/C++ keyword strings | ~200 | Lexer token recognition |
| Format strings (`%s`, `%d`, etc.) | ~500 | Output formatting in `.int.c` emission and diagnostics |
| IL kind names | ~200 | IL node type display (`off_E6DD80` table) |
| Type name fragments | ~400 | Mangling output, type display |
| CUDA architecture names (`sm_XX`) | ~50 | Architecture feature gating |
| Internal EDG config strings | ~200 | Build configuration, feature flags |

### String Mining Techniques

Three string mining techniques are used throughout the analysis:

1. **Error message tracing.** CUDA-specific error messages (e.g., `"calling a __host__ function from a __device__ function is not allowed"`) are grepped from the string table, their xrefs traced to the emitting function, and the emitting function's callers analyzed to understand the validation logic that triggers the error.

2. **Keyword enumeration.** The keyword initialization function (`sub_5863A0`) loads 200+ string constants in sequence. By reading the strings in load order, the complete CUDA keyword vocabulary is recovered -- including internal-only keywords not documented in the CUDA C++ Programming Guide.

3. **Format string analysis.** Format strings in the backend (`cp_gen_be.c`) reveal the exact syntax of `.int.c` output. A string like `"static void __device_stub__%s("` tells us the precise naming convention for device stub wrapper functions.

## Decompilation Quality

Hex-Rays produces readable pseudocode for the vast majority of functions, but several systematic limitations affect the analysis:

### Control Flow Artifacts

Hex-Rays occasionally introduces control flow constructs that do not exist in the original source. The most prominent example is the `while(1)` loop in `main()` (`sub_408950`): the decompiler wraps the entire function body in an infinite loop because a `setjmp`-based error recovery mechanism creates a backward edge in the CFG. In reality, `main()` executes linearly and returns -- the `while(1)` is a decompiler artifact, not a real loop.

Similar artifacts appear in functions with complex switch statements (EDG uses computed gotos for performance), where Hex-Rays may produce nested `if-else` chains instead of the flat dispatch table the original code uses.

### Lost Preprocessor Logic

The original EDG source makes heavy use of preprocessor conditionals (`#if CUDA_SUPPORT`, `#ifdef FRONT_END_CPFE`, etc.). The compiled binary contains only the taken branch -- the preprocessor evaluated all conditions at build time. This means the decompiled code shows the CUDA-enabled configuration only; any host-only or non-CUDA EDG behavior is invisible.

Similarly, C macros that wrap common patterns (assertion macros, IL access macros, type query macros) are fully expanded in the binary. The decompiled output shows the expanded form -- a sequence of struct field accesses and conditional jumps -- rather than the concise macro invocation the original source used.

### Unnamed Variables

The binary is stripped. All local variable names are lost. Hex-Rays assigns synthetic names (`v1`, `v2`, `a1`, `a2`) based on register allocation and stack slot positions. Function parameters are named `a1` through `aN` in declaration order. During analysis, meaningful names are sometimes manually applied in the IDA database, but most decompiled output uses the synthetic names.

Structure field accesses appear as byte-offset expressions (`*((_BYTE *)a1 + 182)`) rather than named fields (`entity->execution_space`). Reconstructing the structure layouts from these offset patterns is a core part of the analysis -- see the [Entity Node Layout](./structs/entity-node.md) page for the most extensively reconstructed structure.

### Decompilation Failures

The IDAPython extraction log reports 6,343 successful decompilations out of 6,487 attempts (140 failures). Due to filename collisions in the output directory (functions with identical sanitized names at different addresses overwrite each other), the actual output directory contains **6,202 unique `.c` files**. The 281 "missing" files break down as:

| Category | Count | Reason |
|---|---|---|
| Hex-Rays decompilation failure | ~140 | Exception personality routines, SoftFloat leaf functions, tiny thunks, irreducible CFG |
| Filename collisions (overwritten) | ~141 | Multiple functions with the same IDA name (after `/` to `_` sanitization) write to the same output path |

The 140 true decompilation failures are concentrated in the C++ runtime region (`0x7DF400`--`0x829722`), particularly in the libstdc++ locale facet implementations (complex template instantiations with deeply nested virtual dispatch) and Berkeley SoftFloat 3e functions (pure arithmetic with non-standard calling conventions). For these functions, analysis relies on the raw disassembly output in `disasm/` instead.

## Phase 1: Address-Range Sweeps

The first phase of analysis consists of **20 address-range sweeps** that collectively cover the entire `.text` section from `0x403000` to `0x82A000`. Each sweep examines a contiguous address range of 128--256 KB, documenting every function within that range.

### Sweep Index

| Sweep | Address Range | Size | Primary Source Files | Key Findings |
|---|---|---|---|---|
| P1.01 | `0x403000`--`0x425000` | 136 KB | `attribute.c`, `class_decl.c` | Assert stub region, CUDA attribute handlers |
| P1.02 | `0x425000`--`0x450000` | 172 KB | `class_decl.c`, `cmd_line.c` | Virtual override checking, execution space propagation |
| P1.03 | `0x450000`--`0x478000` | 160 KB | `cmd_line.c`, `const_ints.c`, `cp_gen_be.c` | 4,105-line CLI parser, 276 flags |
| P1.04 | `0x478000`--`0x4A0000` | 160 KB | `cp_gen_be.c`, `decl_inits.c` | Backend `.int.c` emission, device stub generation |
| P1.05 | `0x4A0000`--`0x4C8000` | 160 KB | `decl_inits.c`, `decl_spec.c`, `declarator.c`, `decls.c` | Declaration parsing pipeline |
| P1.06 | `0x4C8000`--`0x4F8000` | 192 KB | `decls.c`, `disambig.c`, `error.c` | Error table (`off_88FAA0`, 3,795 entries) |
| P1.07 | `0x4F8000`--`0x530000` | 224 KB | `expr.c` | Expression parser (528 functions) |
| P1.08 | `0x530000`--`0x560000` | 192 KB | `expr.c`, `exprutil.c` | Expression utilities, operator overloads |
| P1.09 | `0x560000`--`0x598000` | 224 KB | `exprutil.c`, `extasm.c`, `fe_init.c`, `fe_wrapup.c` | Initialization chain, 5-pass wrapup |
| P1.10 | `0x598000`--`0x5C8000` | 192 KB | `float_pt.c`, `folding.c`, `func_def.c`, `host_envir.c` | Constant folding, timing infrastructure |
| P1.11a--f | `0x5C8000`--`0x5F8000` | 192 KB | `il.c`, `il_alloc.c` | IL node creation, arena allocator |
| P1.12 | `0x5F8000`--`0x628000` | 192 KB | `il_to_str.c`, `il_walk.c`, `interpret.c` | IL display, tree walking, constexpr |
| P1.13 | `0x628000`--`0x668000` | 256 KB | `interpret.c`, `layout.c`, `lexical.c` | Constexpr interpreter, struct layout, lexer |
| P1.14 | `0x668000`--`0x6A8000` | 256 KB | `lexical.c`, `literals.c`, `lookup.c`, `lower_name.c` | Name lookup, name mangling |
| P1.15 | `0x6A8000`--`0x6D0000` | 160 KB | `lower_name.c`, `macro.c`, `mem_manage.c`, `nv_transforms.c`, `overload.c` | NVIDIA transforms, memory management |
| P1.16 | `0x6D0000`--`0x708000` | 224 KB | `overload.c`, `pch.c`, `pragma.c`, `preproc.c`, `scope_stk.c` | Overload resolution, scope stack |
| P1.17 | `0x708000`--`0x740000` | 224 KB | `scope_stk.c`, `src_seq.c`, `statements.c`, `symbol_ref.c`, `symbol_tbl.c` | Statement parsing, symbol table |
| P1.18 | `0x740000`--`0x7A0000` | 384 KB | `symbol_tbl.c`, `sys_predef.c`, `templates.c` | Template engine (443 functions) |
| P1.19 | `0x7A0000`--`0x7E0000` | 256 KB | `trans_unit.c`, `types.c`, `modules.c`, `trans_corresp.c` | Type system, TU processing |
| P1.20 | `0x7E0000`--`0x82A000` | 304 KB | (C++ runtime) | libstdc++, SoftFloat, CRT, demangler |

The P1.11 sweep was subdivided into six sub-sweeps (11a through 11f) because the `il.c` region is dense and complex, containing the core IL node creation and manipulation functions that are referenced from nearly every other source file.

### Sweep Report Format

Each sweep report follows a consistent format:

```
================================================================================
P1.XX SWEEP: Address range 0xNNNNNN - 0xMMMMMM
================================================================================
Range: 0xNNNNNN - 0xMMMMMM
Functions found: N
EDG source files:
  - file.c (assert stub range, main body range)
  ...

### 0xAAAAAA -- sub_AAAAAA (NN bytes / NN lines)
**Identity**: function_name (source_file.c:NNNN)
**Confidence**: CONFIRMED / HIGH / MEDIUM / LOW
**EDG Source**: source_file.c
**Notes**: Additional observations about behavior, callers, callees
```

Every function in the sweep range gets an entry. Functions are documented in address order. The identity field records the inferred function name and source location. The confidence field uses the four-level system defined above. Notes capture anything unusual -- unexpected callers, CUDA-specific behavior, undocumented error codes, or connections to other subsystems.

## Phase 2: Targeted Deep Dives

After the Phase 1 sweep establishes the complete function map and identifies all source files, Phase 2 produces the detailed wiki pages. Each wiki page corresponds to one W-series work report that focuses on a specific subsystem or topic.

### Deep Dive Methodology

Each W-series report follows a consistent process:

1. **Scope definition.** Identify the set of functions relevant to the topic. For example, W012 (Execution Spaces) requires the CUDA attribute application handlers in `attribute.c`, the execution space checking functions in `nv_transforms.c`, and the virtual override validator in `class_decl.c`.

2. **Decompilation review.** Read the full Hex-Rays pseudocode for every function in scope. For complex functions, also review the raw disassembly to catch decompiler artifacts.

3. **String evidence collection.** Grep the string table for all strings referenced by the in-scope functions. Error messages reveal validation rules; format strings reveal output patterns; keyword strings reveal accepted syntax.

4. **Call graph traversal.** Starting from the in-scope functions, walk callers and callees to understand the full data flow. Who calls `apply_nv_global_attr`? What does it call? How does data arrive and where does it go?

5. **Struct layout reconstruction.** When decompiled code accesses struct fields via byte offsets, reconstruct the field layout by collecting all access patterns across all functions that touch the same struct. Cross-validate offsets across multiple functions.

6. **Pseudocode reconstruction.** Translate the Hex-Rays output into readable C-like pseudocode with meaningful variable names, proper control flow, and comments explaining the logic. This reconstructed pseudocode appears in the wiki pages.

7. **Cross-reference synthesis.** Link findings to other wiki pages and W-series reports. Every page should situate itself within the overall architecture.

### W-Series Report Index

As of this writing, 28 W-series reports have been produced, each backing one or more wiki pages:

| Report | Topic | Wiki Page(s) |
|---|---|---|
| W001 | Index page | `index.md` |
| W002 | Function map | `function-map.md` |
| W003 | Binary layout | `binary-layout.md` |
| W004 | Methodology | `methodology.md` (this page) |
| W005 | Pipeline overview | `pipeline/overview.md` |
| W006 | Entry point | `pipeline/entry.md` |
| W010 | Backend code gen | `pipeline/backend.md` |
| W012 | Execution spaces | `cuda/execution-spaces.md` |
| W014 | Cross-space validation | `cuda/cross-space-validation.md` |
| W015 | Device/host separation | `cuda/device-host-separation.md` |
| W016 | Kernel stubs | `cuda/kernel-stubs.md` |
| W020 | Attribute system | `attributes/overview.md` |
| W021 | \_\_global\_\_ constraints | `attributes/global-function.md` |
| W026 | Lambda overview | `lambda/overview.md` |
| W027 | Device wrapper | `lambda/device-wrapper.md` |
| W028 | Host-device wrapper | `lambda/host-device-wrapper.md` |
| W029 | Capture handling | `lambda/capture-handling.md` |
| W032 | IL overview | `il/overview.md` |
| W033 | IL allocation | `il/allocation.md` |
| W035 | Keep-in-IL | `il/keep-in-il.md` |
| W038 | .int.c format | `output/int-c-format.md` |
| W042 | EDG overview | `edg/overview.md` |
| W047 | Template engine | `edg/template-engine.md` |
| W052 | Diagnostics overview | `diagnostics/overview.md` |
| W053 | CUDA errors | `diagnostics/cuda-errors.md` |
| W056 | Entity node layout | `structs/entity-node.md` |
| W061 | CLI flags | `config/cli-flags.md` |
| W065 | EDG source map | `reference/edg-source-map.md` |
| W066 | Global variables | `reference/global-variables.md` |

## Numerical Summary

| Metric | Value |
|---|---|
| Binary file size | 8,910,936 bytes (8.5 MB) |
| Total functions in binary | 6,501 |
| Decompiled functions (log-reported) | 6,343 |
| Decompiled files (actual on disk) | 6,202 |
| Disassembly files | 6,342 |
| CFG files (JSON + DOT) | 12,684 |
| Functions attributed to source files | 2,209 (34.0%) |
| Functions calling `sub_4F2930` (assert handler) | 2,139 |
| Total call sites to `sub_4F2930` | 5,178 |
| Assert stubs (`0x403300`--`0x408B40`) | 235 |
| Source files identified (`.c`) | 52 |
| Header files identified (`.h`) | 13 |
| EDG build-path strings in `.rodata` | 65 |
| String literals extracted | 52,489 |
| Cross-references extracted | 1,243,258 |
| Call graph edges | 67,756 (5,057 callers, 5,382 callees) |
| Named locations | 54,771 |
| IDA comments | 22,911 |
| Imported glibc symbols | 142 |
| ELF segments | 26 |
| `.rodata` raw dump | 2,599,011 bytes |
| IDA database (`.i64`) | 247 MB |
| Phase 1 sweep reports | 28 files (20 ranges + 8 sub-sweeps), 38,221 lines |
| Phase 2 deep-dive reports (W-series) | 28 |
| Wiki pages | 55 |
| Error table entries (`off_88FAA0`) | 3,795 |
| CLI flags documented | 276 |
| Total exported data | ~500 MB |

## Limitations and Caveats

### What This Analysis Cannot Determine

1. **Preprocessor-disabled code.** Any EDG code behind `#if 0`, `#ifndef CUDA_SUPPORT`, or similar guards was compiled out. The binary reflects only the CUDA-enabled, Linux x86-64, EDG 6.6 configuration. Other EDG frontend features (e.g., Fortran support, Windows target, older C++ standards) are not present.

2. **Inlined function boundaries.** When the compiler inlines a function, its code merges with the caller. The binary may contain hundreds of inlined instances of small EDG utility functions (type queries, IL accessors) that are invisible as separate entities. The 6,501 function count represents only the non-inlined functions.

3. **Original variable names.** All local and most global variable names are lost. The wiki uses reconstructed names based on semantics (e.g., `execution_space_byte` for `*((_BYTE *)entity + 182)`), but these are analyst-assigned, not original.

4. **Exact source line mapping.** While assertion strings encode line numbers, these are the assertion site's line number, not the calling function's line number. The analyst can determine that `is_aliasable` in `attribute.c` has an assertion at line 10897, but cannot determine the start line of `is_aliasable` itself.

5. **NVIDIA-internal documentation.** Any design documents, code comments, commit messages, or internal wikis that informed the original development are unavailable. All behavioral descriptions in this wiki are inferred from the binary alone.

### Reproducibility

Every finding in this wiki can be reproduced by:

1. Obtaining `cudafe++` from CUDA Toolkit 13.0 (version string embedded in binary as the build path prefix `r13.0`).
2. Loading it into IDA Pro 9.0 (64-bit) with default x86-64 analysis settings. Wait for auto-analysis to complete (5-10 minutes).
3. Running `analyze_cudafe++.py` via File > Script File to extract all raw data (30-45 minutes).
4. Querying the exported JSON files with `jq` to trace cross-reference chains, string lookups, and callgraph paths.
5. Reading the decompiled `.c` files and raw `.asm` files for behavioral analysis.

No proprietary tools beyond IDA Pro + Hex-Rays are required. The analysis does not depend on NVIDIA source code access, NDA-protected documentation, or insider knowledge. Every claim is derived from the publicly distributed binary.
