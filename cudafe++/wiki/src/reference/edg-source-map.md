# EDG Source File Map

This page is the definitive reference table mapping all 52 `.c` source files and 13 `.h` header files from EDG 6.6 to their binary addresses in the cudafe++ CUDA 13.0 build. Every column is derived from the `.rodata` string cross-reference database and verified against the 20 sweep reports (P1.01 through P1.20).

For narrative discussion of these files and their roles in the compilation pipeline, see the [Function Map](../function-map.md) and [EDG Overview](../edg/overview.md) pages.

## Build Path

All source files share the build prefix:

```text
/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/
```

## Coverage Summary

| Metric | Count |
|---|---|
| `.c` files with mapped functions | 52 |
| `.h` files with mapped functions | 13 |
| Total source files | 65 |
| Functions mapped via `.c` paths | 2,129 |
| Functions mapped via `.h` paths only | 80 |
| **Total mapped functions** | **2,209** |
| Unmapped functions in EDG region (`0x403300`--`0x7E0000`) | ~2,914 |
| C++ runtime / demangler (`0x7E0000`--`0x829722`) | ~1,085 |
| PLT stubs + init (`0x402A18`--`0x403300`) | ~283 |
| **Total functions in binary** | **~6,501** |
| **Mapping coverage** | **34.0%** |

The 34% mapping rate reflects the fact that only functions containing EDG `internal_error` assertions reference `__FILE__` strings. Functions below the assertion threshold, display-only code compiled without assertions, inlined leaf functions, and the statically-linked C++ runtime are all invisible to this technique.

## Column Definitions

| Column | Meaning |
|---|---|
| **#** | Row number, ordered by main body start address |
| **Source File** | Filename from the EDG source tree |
| **Origin** | `EDG` = standard Edison Design Group code; `NVIDIA` = NVIDIA-authored |
| **Total Funcs** | Unique functions referencing this file's `__FILE__` string (stubs + main) |
| **Stubs** | Assert wrapper functions in `0x403300`--`0x408B40` |
| **Main Funcs** | Functions in the main body region (after `0x409350`) |
| **Main Body Start** | Lowest xref address outside the stub region |
| **Main Body End** | Highest xref address outside the stub region |
| **Code Size** | `Main Body End - Main Body Start` in bytes; approximate (includes interleaved `.h` inlines and alignment padding) |

## Source File Table — 52 `.c` Files

Sorted by main body start address. This ordering reflects the binary layout, which is near-alphabetical with two exceptions noted below.

| # | Source File | Origin | Total Funcs | Stubs | Main Funcs | Main Body Start | Main Body End | Code Size |
|---|---|---|---:|---:|---:|---|---|---:|
| 1 | `attribute.c` | EDG | 177 | 7 | 170 | `0x409350` | `0x418F80` | 64,560 |
| 2 | `class_decl.c` | EDG | 273 | 9 | 264 | `0x419280` | `0x447930` | 190,160 |
| 3 | `cmd_line.c` | EDG | 44 | 1 | 43 | `0x44B250` | `0x459630` | 58,336 |
| 4 | `const_ints.c` | EDG | 4 | 1 | 3 | `0x461C20` | `0x4659A0` | 15,744 |
| 5 | `cp_gen_be.c` | EDG | 226 | 25 | 201 | `0x466F90` | `0x489000` | 139,376 |
| 6 | `debug.c` | EDG | 2 | 0 | 2 | `0x48A1B0` | `0x48A1B0` | <1 KB |
| 7 | `decl_inits.c` | EDG | 196 | 4 | 192 | `0x48B3F0` | `0x4A1540` | 90,448 |
| 8 | `decl_spec.c` | EDG | 88 | 3 | 85 | `0x4A1BF0` | `0x4B37F0` | 72,704 |
| 9 | `declarator.c` | EDG | 64 | 0 | 64 | `0x4B3970` | `0x4C00A0` | 50,480 |
| 10 | `decls.c` | EDG | 207 | 5 | 202 | `0x4C0910` | `0x4E8C40` | 164,656 |
| 11 | `disambig.c` | EDG | 5 | 1 | 4 | `0x4E9E70` | `0x4EC690` | 10,272 |
| 12 | `error.c` | EDG | 51 | 1 | 50 | `0x4EDCD0` | `0x4F8F80` | 45,744 |
| 13 | `expr.c` | EDG | 538 | 10 | 528 | `0x4F9870` | `0x5565E0` | 380,528 |
| 14 | `exprutil.c` | EDG | 299 | 13 | 286 | `0x558720` | `0x583540` | 175,648 |
| 15 | `extasm.c` | EDG | 7 | 0 | 7 | `0x584CA0` | `0x585850` | 2,992 |
| 16 | `fe_init.c` | EDG | 6 | 1 | 5 | `0x585B10` | `0x5863A0` | 2,192 |
| 17 | `fe_wrapup.c` | EDG | 2 | 0 | 2 | `0x588D40` | `0x588F90` | 592 |
| 18 | `float_pt.c` | EDG | 79 | 0 | 79 | `0x589550` | `0x594150` | 44,032 |
| 19 | `folding.c` | EDG | 139 | 9 | 130 | `0x594B30` | `0x5A4FD0` | 66,464 |
| 20 | `func_def.c` | EDG | 56 | 1 | 55 | `0x5A51B0` | `0x5AAB80` | 22,992 |
| 21 | `host_envir.c` | EDG | 19 | 2 | 17 | `0x5AD540` | `0x5B1E70` | 18,736 |
| 22 | `il.c` | EDG | 358 | 16 | 342 | `0x5B28F0` | `0x5DFAD0` | 184,800 |
| 23 | `il_alloc.c` | EDG | 38 | 1 | 37 | `0x5E0600` | `0x5E8300` | 31,488 |
| 24 | `il_to_str.c` | EDG | 83 | 1 | 82 | `0x5F7FD0` | `0x6039E0` | 47,632 |
| 25 | `il_walk.c` | EDG | 27 | 1 | 26 | `0x603FE0` | `0x620190` | 115,120 |
| 26 | `interpret.c` | EDG | 216 | 5 | 211 | `0x620CE0` | `0x65DE10` | 250,160 |
| 27 | `layout.c` | EDG | 21 | 2 | 19 | `0x65EA50` | `0x665A60` | 28,688 |
| 28 | `lexical.c` | EDG | 140 | 5 | 135 | `0x666720` | `0x689130` | 141,328 |
| 29 | `literals.c` | EDG | 21 | 0 | 21 | `0x68ACC0` | `0x68F2B0` | 17,904 |
| 30 | `lookup.c` | EDG | 71 | 2 | 69 | `0x68FAB0` | `0x69BE80` | 50,128 |
| 31 | `lower_name.c` | EDG | 179 | 11 | 168 | `0x69C980` | `0x6AB280` | 59,648 |
| 32 | `macro.c` | EDG | 43 | 1 | 42 | `0x6AB6E0` | `0x6B5C10` | 42,288 |
| 33 | `mem_manage.c` | EDG | 9 | 2 | 7 | `0x6B6DD0` | `0x6BA230` | 13,408 |
| 34 | `nv_transforms.c` | **NVIDIA** | 1 | 0 | 1 | `0x6BE300` | `0x6BE300` | ~22 KB[^1] |
| 35 | `overload.c` | EDG | 284 | 3 | 281 | `0x6BE4A0` | `0x6EF7A0` | 201,472 |
| 36 | `pch.c` | EDG | 23 | 3 | 20 | `0x6F2790` | `0x6F5DA0` | 13,840 |
| 37 | `pragma.c` | EDG | 28 | 0 | 28 | `0x6F61B0` | `0x6F8320` | 8,560 |
| 38 | `preproc.c` | EDG | 10 | 0 | 10 | `0x6F9B00` | `0x6FC940` | 11,840 |
| 39 | `scope_stk.c` | EDG | 186 | 6 | 180 | `0x6FE160` | `0x7106B0` | 75,600 |
| 40 | `src_seq.c` | EDG | 57 | 1 | 56 | `0x710F10` | `0x718720` | 30,736 |
| 41 | `statements.c` | EDG | 83 | 1 | 82 | `0x719300` | `0x726A50` | 55,120 |
| 42 | `symbol_ref.c` | EDG | 42 | 2 | 40 | `0x726F20` | `0x72CEA0` | 24,448 |
| 43 | `symbol_tbl.c` | EDG | 175 | 8 | 167 | `0x72D950` | `0x74B8D0` | 122,688 |
| 44 | `sys_predef.c` | EDG | 35 | 1 | 34 | `0x74C690` | `0x751470` | 19,936 |
| 45 | `target.c` | EDG | 11 | 0 | 11 | `0x7525F0` | `0x752DF0` | 2,048 |
| 46 | `templates.c` | EDG | 455 | 12 | 443 | `0x7530C0` | `0x794D30` | 285,808 |
| 47 | `trans_copy.c` | EDG | 2 | 0 | 2 | `0x796BA0` | `0x796BA0` | <1 KB |
| 48 | `trans_corresp.c` | EDG | 88 | 6 | 82 | `0x796E60` | `0x7A3420` | 50,112 |
| 49 | `trans_unit.c` | EDG | 10 | 0 | 10 | `0x7A3BB0` | `0x7A4690` | 2,784 |
| 50 | `types.c` | EDG | 88 | 5 | 83 | `0x7A4940` | `0x7C02A0` | 112,480 |
| 51 | `modules.c` | EDG | 22 | 3 | 19 | `0x7C0C60` | `0x7C2560` | 6,400 |
| 52 | `floating.c` | EDG | 50 | 9 | 41 | `0x7D0EB0` | `0x7D59B0` | 19,200 |
| | **TOTALS** | | **5,338** | **200** | **5,138** | `0x409350` | `0x7D59B0` | **~3.57 MB** |

[^1]: `nv_transforms.c` has only 1 function with an EDG-style `__FILE__` reference, but sweep analysis confirms ~40 functions in the `0x6BAE70`--`0x6BE4A0` region (~22 KB). Most use NVIDIA's own assertion macros instead of EDG's `internal_error` path.

## Source File Table — 13 `.h` Header Files

Header files appear in assertion strings when an inline function or macro defined in the header triggers an `internal_error` call. The function itself is compiled within the `.c` file's translation unit, but `__FILE__` resolves to the header path. These functions are scattered across the binary, interleaved with the `.c` file that `#include`-d them.

| # | Header File | Total Funcs | Stubs | Main Funcs | Min Address | Max Address | Primary Host |
|---|---|---:|---:|---:|---|---|---|
| 1 | `decls.h` | 1 | 0 | 1 | `0x4E08F0` | `0x4E08F0` | `decls.c` |
| 2 | `float_type.h` | 63 | 0 | 63 | `0x7D1C90` | `0x7DEB90` | `floating.c` |
| 3 | `il.h` | 5 | 2 | 3 | `0x52ABC0` | `0x6011F0` | `expr.c`, `il.c`, `il_to_str.c` |
| 4 | `lexical.h` | 1 | 0 | 1 | `0x68F2B0` | `0x68F2B0` | `lexical.c` / `literals.c` boundary |
| 5 | `mem_manage.h` | 4 | 0 | 4 | `0x4EDCD0` | `0x4EDCD0` | `error.c` |
| 6 | `modules.h` | 5 | 0 | 5 | `0x7C1100` | `0x7C2560` | `modules.c` |
| 7 | `nv_transforms.h` | 3 | 0 | 3 | `0x432280` | `0x719D20` | `class_decl.c`, `cp_gen_be.c`, `src_seq.c` |
| 8 | `overload.h` | 1 | 0 | 1 | `0x6C9E40` | `0x6C9E40` | `overload.c` |
| 9 | `scope_stk.h` | 4 | 0 | 4 | `0x503D90` | `0x574DD0` | `expr.c`, `exprutil.c` |
| 10 | `symbol_tbl.h` | 2 | 1 | 1 | `0x7377D0` | `0x7377D0` | `symbol_tbl.c` |
| 11 | `types.h` | 17 | 4 | 13 | `0x469260` | `0x7B05E0` | Many `.c` files (scattered type queries) |
| 12 | `util.h` | 124 | 10 | 114 | `0x430E10` | `0x7C2B10` | All major `.c` files |
| 13 | `walk_entry.h` | 51 | 0 | 51 | `0x604170` | `0x618660` | `il_walk.c` |
| | **TOTALS** | **281** | **17** | **264** | | | |

The stub column sums to 217 across the two tables (200 from `.c` files + 17 from `.h` files). The 198 figure quoted in the narrative counts *distinct* stub functions referencing any source path — each stub references exactly one file, but 19 stub functions are the target of multiple cross-references (the same stub is reused at multiple assertion call sites that all encode the same source location), inflating column sums above the unique-stub count.

### Header Distribution Patterns

The 13 headers fall into three distinct patterns:

**Localized headers** — functions cluster in a single `.c` file's address range:
- `float_type.h` (63 funcs in 52 KB at `0x7D1C90`--`0x7DEB90`, all within `floating.c`)
- `walk_entry.h` (51 funcs in 90 KB at `0x604170`--`0x618660`, all within `il_walk.c`)
- `modules.h` (5 funcs in 5 KB at `0x7C1100`--`0x7C2560`, all within `modules.c`)
- `decls.h`, `lexical.h`, `overload.h`, `symbol_tbl.h` (1–2 funcs each, single site)
- `mem_manage.h` (4 funcs, single site in `error.c`)

**Moderately scattered headers** — functions appear in 2–3 `.c` files:
- `il.h` (5 funcs across `expr.c`, `il.c`, `il_to_str.c`)
- `scope_stk.h` (4 funcs across `expr.c`, `exprutil.c`)
- `nv_transforms.h` (3 funcs across `class_decl.c`, `cp_gen_be.c`, `src_seq.c`)

**Pervasive headers** — functions inlined into most `.c` files:
- `util.h` (124 xrefs spanning `0x430E10`--`0x7C2B10`, nearly the entire EDG region)
- `types.h` (17 funcs spanning `0x469260`--`0x7B05E0`, scattered type queries)

## Assert Stub Region

The region `0x403300`--`0x408B40` contains 235 small `__noreturn` functions. Each encodes a single assertion site: the source file path, line number, and enclosing function name. When the assertion condition fails, the stub calls `sub_4F2930` (EDG's `internal_error` handler) and does not return. Every stub is 29 bytes.

Of the 235 stubs, 198 carry a resolvable source-path reference (181 to `.c` files, 17 to `.h` files); the remaining 37 are unattributed — their `__FILE__` argument is either folded into a shared string or supplied by the caller, and they do not appear as targets in the per-file cross-reference table below.

### Stub Distribution by Source File

| Source File | Stub Count | Source File | Stub Count |
|---|---:|---|---:|
| `cp_gen_be.c` | 25 | `macro.c` | 1 |
| `il.c` | 16 | `mem_manage.c` | 2 |
| `exprutil.c` | 13 | `modules.c` | 3 |
| `templates.c` | 12 | `overload.c` | 3 |
| `lower_name.c` | 11 | `pch.c` | 3 |
| `expr.c` | 10 | `preproc.c` | 0 |
| `class_decl.c` | 9 | `scope_stk.c` | 6 |
| `folding.c` | 9 | `src_seq.c` | 1 |
| `floating.c` | 9 | `statements.c` | 1 |
| `attribute.c` | 7 | `symbol_ref.c` | 2 |
| `symbol_tbl.c` | 8 | `symbol_tbl.h` | 1 |
| `trans_corresp.c` | 6 | `sys_predef.c` | 1 |
| `lexical.c` | 5 | `target.c` | 0 |
| `decls.c` | 5 | `trans_copy.c` | 0 |
| `types.c` | 5 | `trans_unit.c` | 0 |
| `decl_inits.c` | 4 | `types.h` | 4 |
| `interpret.c` | 3 | `util.h` | 10 |
| `decl_spec.c` | 3 | `il.h` | 2 |
| `host_envir.c` | 2 | `debug.c` | 0 |
| `layout.c` | 2 | `extasm.c` | 0 |
| `lookup.c` | 2 | `fe_wrapup.c` | 0 |
| `cmd_line.c` | 1 | `float_pt.c` | 0 |
| `const_ints.c` | 1 | `declarator.c` | 0 |
| `disambig.c` | 1 | `pragma.c` | 0 |
| `error.c` | 1 | `nv_transforms.c` | 0 |
| `fe_init.c` | 1 | `literals.c` | 0 |
| `func_def.c` | 1 | | |
| `il_alloc.c` | 1 | | |
| `il_to_str.c` | 1 | | |
| `il_walk.c` | 1 | | |

After the stubs, addresses `0x408B40`--`0x409350` contain 15 C++ static constructor functions (`ctor_001` through `ctor_015`) that initialize global tables at program startup. These have no source file attribution.

## Gap Analysis — Unmapped Regions

The following address ranges within the EDG `.text` region contain functions that could not be mapped to any source file via `__FILE__` strings. Each gap represents functions that either lack assertions entirely, use non-EDG assertion macros, or are compiler-generated (vtable thunks, exception handlers, template instantiation artifacts).

| # | Gap Range | Size | Between | Probable Content |
|---|---|---:|---|---|
| 1 | `0x408B40`--`0x409350` | 2 KB | stubs / `attribute.c` | Static constructors (`ctor_001`--`ctor_015`) |
| 2 | `0x447930`--`0x44B250` | 13 KB | `class_decl.c` / `cmd_line.c` | Boundary helpers, small inlines |
| 3 | `0x459630`--`0x461C20` | 34 KB | `cmd_line.c` / `const_ints.c` | Unmapped option handlers, flag tables |
| 4 | `0x4659A0`--`0x466F90` | 6 KB | `const_ints.c` / `cp_gen_be.c` | Constant integer helpers |
| 5 | `0x489000`--`0x48A1B0` | 5 KB | `cp_gen_be.c` / `debug.c` | Backend emission tail |
| 6 | `0x48A1B0`--`0x48B3F0` | 5 KB | `debug.c` / `decl_inits.c` | Debug infrastructure |
| 7 | `0x5E8300`--`0x5F7FD0` | 87 KB | `il_alloc.c` / `il_to_str.c` | IL display routines (no assertions) |
| 8 | `0x620190`--`0x620CE0` | 3 KB | `il_walk.c` / `interpret.c` | Walk epilogue |
| 9 | `0x65DE10`--`0x65EA50` | 3 KB | `interpret.c` / `layout.c` | Interpreter tail |
| 10 | `0x665A60`--`0x666720` | 3 KB | `layout.c` / `lexical.c` | Layout/lexer boundary |
| 11 | `0x689130`--`0x68ACC0` | 7 KB | `lexical.c` / `literals.c` | Token conversion helpers |
| 12 | `0x6AB280`--`0x6AB6E0` | 1 KB | `lower_name.c` / `macro.c` | Mangling helpers |
| 13 | `0x6BA230`--`0x6BAE70` | 3 KB | `mem_manage.c` / `nv_transforms.c` | Memory infrastructure |
| 14 | `0x6EF7A0`--`0x6F2790` | 12 KB | `overload.c` / `pch.c` | Overload resolution tail |
| 15 | `0x6FC940`--`0x6FE160` | 6 KB | `preproc.c` / `scope_stk.c` | Preprocessor tail functions |
| 16 | `0x751470`--`0x7525F0` | 7 KB | `sys_predef.c` / `target.c` | Predefined macro infrastructure |
| 17 | `0x7A4690`--`0x7A4940` | 1 KB | `trans_unit.c` / `types.c` | TU helpers |
| 18 | `0x7C2560`--`0x7D0EB0` | 59 KB | `modules.c` / `floating.c` | Type-name encoding, module helpers |
| 19 | `0x7D59B0`--`0x7DEB90` | 37 KB | `floating.c` tail | `float_type.h` template instantiations |
| 20 | `0x7DFFF0`--`0x82A000` | 304 KB | post-EDG | C++ runtime, demangler, soft-float, EH |
| | **Total unmapped** | **~582 KB** | | |

The largest unmapped gap within EDG code proper is the IL display region at `0x5E8300`--`0x5F7FD0` (87 KB). These functions were compiled from `il_to_str.c` but contain no assertions because the display/dump subsystem was built without assertion macros — it is purely diagnostic code that formats IL trees to stdout.

## Alphabetical Layout Observation

Source files are laid out in the binary in near-alphabetical order by filename, a consequence of the build system compiling `.c` files in directory-listing order and the linker processing them sequentially. The sequence is strictly alphabetical from `attribute.c` through `types.c` (rows 1–50).

Two files break this pattern:

| File | Expected Position | Actual Position | Offset |
|---|---|---|---|
| `modules.c` | Between `mem_manage.c` and `nv_transforms.c` (#33--#34) | After `types.c` (#51, at `0x7C0C60`) | +47 rows late |
| `floating.c` | Between `float_pt.c` and `folding.c` (#18--#19) | After `modules.c` (#52, at `0x7D0EB0`) | +34 rows late |

Both files appear after the main alphabetical sequence, placed at the very end of the EDG region. The most likely explanation is that `modules.c` and `floating.c` are compiled as separate translation units outside the main EDG build directory — perhaps in a subdirectory or a secondary build target — and are appended to the link line after the alphabetically-sorted main objects. The `modules.c` file implements C++20 module support (mostly stubs in the CUDA build), and `floating.c` implements arbitrary-precision IEEE 754 arithmetic — both are semi-independent subsystems that could plausibly be compiled separately.

Note that `floating.c` is followed immediately by its private header `float_type.h` (63 template instantiations at `0x7D1C90`--`0x7DEB90`), confirming they share a compilation unit.

## Binary Region Map

```text
0x402A18 +--------------------------+
         | PLT stubs / init (283)   |  3 KB
0x403300 +--------------------------+
         | Assert stubs (198)       |  22 KB
0x408B40 +--------------------------+
         | Constructors (15)        |  2 KB
0x409350 +--------------------------+
         | attribute.c              |  65 KB
0x419280 | class_decl.c             | 190 KB
         | cmd_line.c               |  58 KB
         | const_ints.c             |  16 KB
         | cp_gen_be.c              | 139 KB
         | debug.c                  |  <1 KB
         | decl_inits.c             |  90 KB
         | decl_spec.c              |  73 KB
         | declarator.c             |  50 KB
         | decls.c                  | 165 KB
         | disambig.c               |  10 KB
         | error.c                  |  46 KB
         | expr.c                   | 381 KB
         | exprutil.c               | 176 KB
         | extasm.c                 |   3 KB
         | fe_init.c                |   2 KB
         | fe_wrapup.c              |  <1 KB
         | float_pt.c               |  44 KB
         | folding.c                |  66 KB
         | func_def.c               |  23 KB
         | host_envir.c             |  19 KB
         | il.c                     | 185 KB
         | il_alloc.c               |  31 KB
         | [il display gap]         |  87 KB  (unmapped)
         | il_to_str.c              |  48 KB
         | il_walk.c                | 115 KB
         | interpret.c              | 250 KB
         | layout.c                 |  29 KB
         | lexical.c                | 141 KB
         | literals.c               |  18 KB
         | lookup.c                 |  50 KB
         | lower_name.c             |  60 KB
         | macro.c                  |  42 KB
         | mem_manage.c             |  13 KB
         | nv_transforms.c          |  22 KB
         | overload.c               | 201 KB
         | pch.c                    |  14 KB
         | pragma.c                 |   9 KB
         | preproc.c                |  12 KB
         | scope_stk.c              |  76 KB
         | src_seq.c                |  31 KB
         | statements.c             |  55 KB
         | symbol_ref.c             |  24 KB
         | symbol_tbl.c             | 123 KB
         | sys_predef.c             |  20 KB
         | target.c                 |   2 KB
         | templates.c              | 286 KB
         | trans_copy.c             |  <1 KB
         | trans_corresp.c          |  50 KB
         | trans_unit.c             |   3 KB
         | types.c                  | 112 KB
         |  --- alphabetical break ---
         | modules.c                |   6 KB
         |  --- gap (59 KB) ---
0x7D0EB0 | floating.c               |  19 KB
         | float_type.h inlines     |  52 KB
0x7DFFF0 +--------------------------+
         | C++ runtime / demangler  | 304 KB
0x82A000 +--------------------------+
```

## Reproduction

To regenerate the source file list from the strings database:

```bash
jq '[.[] | select(.value | test("/dvs/p4/.*\\.[ch]$")) |
  {file: (.value | split("/") | last),
   xrefs: (.xrefs | length)}
] | group_by(.file) |
  map({file: .[0].file,
       total_xrefs: (map(.xrefs) | add)}) |
  sort_by(.file)' cudafe++_strings.json
```

To extract address ranges per file:

```python
import json
from collections import defaultdict

with open('cudafe++_strings.json') as f:
    data = json.load(f)

files = defaultdict(list)
for entry in data:
    val = entry.get('value', '')
    if '/dvs/p4/' not in val:
        continue
    if not (val.endswith('.c') or val.endswith('.h')):
        continue
    fname = val.split('/')[-1]
    for xref in entry.get('xrefs', []):
        files[fname].append(int(xref['from'], 16))

for fname in sorted(files):
    addrs = sorted(files[fname])
    print(f"{fname:25s}  {hex(addrs[0]):>12s} - {hex(addrs[-1]):>12s}"
          f"  ({len(addrs)} xrefs)")
```
