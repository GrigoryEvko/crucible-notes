# Function Map

Every function in the cudafe++ binary that triggers an EDG assertion encodes three pieces of data in the assertion string: the source file path, the line number, and the enclosing function name. These strings survive in `.rodata` and cross-reference back to the compiled functions, providing a ground-truth mapping from binary address to EDG source file. This page catalogs that mapping for all 52 `.c` source files and 13 `.h` header files identified in the CUDA 13.0 build of cudafe++ (EDG 6.6).

The mapping was produced by extracting all string literals matching `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/*.c` and `*.h` from the binary's `.rodata` section, then tracing their cross-references to determine which functions load each path. A function that references `attribute.c` in an assertion string was compiled from `attribute.c`. Functions that reference no source path at all (the "unmapped" pool) are either too small to contain assertions, are inlined from headers, or belong to the statically-linked C++ runtime.

## Coverage Summary

| Category | Functions | Percentage |
|---|---|---|
| Mapped via `.c` file paths | 2,129 | 32.7% |
| Mapped via `.h` file paths only | 80 | 1.2% |
| **Total mapped** | **2,209** | **34.0%** |
| Unmapped in EDG region (`0x403300`--`0x7E0000`) | 2,924 | 45.0% |
| C++ runtime / demangler (`0x7E0000`--`0x829722`) | 1,085 | 16.7% |
| PLT stubs + init (`0x402A18`--`0x403300`) | 283 | 4.4% |
| **Total functions in binary** | **6,501** | 100% |

The 2,924 unmapped functions in the EDG region include inlined header expansions (e.g., `util.h` vector/hash helpers, `types.h` type queries), small leaf functions below the assertion threshold, switch-table dispatch fragments, and functions from translation units compiled without assertions enabled (notably `il_to_str.c` display routines and parts of `floating.c`).

## Binary Layout

The EDG `.text` region (`0x403300`--`0x7E0000`) has a three-part structure:

1. **Assert stub region** (`0x403300`--`0x408B40`): 235 small `__noreturn` functions, one per assertion site. Each encodes a source file path, line number, and function name, then calls `sub_4F2930` (the internal error handler). These stubs are sorted by source file name -- the linker grouped them from all 52 `.c` files into one contiguous block. 198 stubs resolve to a single source file (181 to `.c` files, 17 to `.h` files inlined into `.c` compilation units); the remaining 37 are unattributed -- their `__FILE__` argument is either folded into a shared string or supplied by the caller and is not visible as a direct cross-reference.

2. **Constructor region** (`0x408B40`--`0x409350`): 15 C++ static constructor functions (`ctor_001` through `ctor_015`) that initialize global tables at program startup.

3. **Main body region** (`0x409350`--`0x7DFFF0`): The bulk of the compiler. Source files are laid out roughly in alphabetical order by filename, a consequence of the linker processing object files in directory-listing order. The alphabetical ordering holds across the entire range: `attribute.c` starts at `0x409350`, `class_decl.c` at `0x419280`, progressing through to `types.c` at `0x7A4940`, `modules.c` at `0x7C0C60`, and `floating.c` at `0x7D0EB0`.

## Source File Address Table

The table below lists all 52 `.c` source files sorted by their main body start address. "Total Funcs" counts all functions referencing the file (stubs + main body). "Stubs" counts assert stubs in `0x403300`--`0x408B40`. "Main Funcs" counts functions in the main body region.

| # | Source File | Origin | Total Funcs | Stubs | Main Funcs | Main Body Start | Main Body End | Sweep |
|---|---|---|---|---|---|---|---|---|
| 1 | `attribute.c` | EDG | 177 | 7 | 170 | `0x409350` | `0x418F80` | P1.01 |
| 2 | `class_decl.c` | EDG | 273 | 9 | 264 | `0x419280` | `0x447930` | P1.01--02 |
| 3 | `cmd_line.c` | EDG | 44 | 1 | 43 | `0x44B250` | `0x459630` | P1.02--03 |
| 4 | `const_ints.c` | EDG | 4 | 1 | 3 | `0x461C20` | `0x4659A0` | P1.03 |
| 5 | `cp_gen_be.c` | EDG | 226 | 25 | 201 | `0x466F90` | `0x489000` | P1.03--04 |
| 6 | `debug.c` | EDG | 2 | 0 | 2 | `0x48A1B0` | `0x48A1B0` | P1.04 |
| 7 | `decl_inits.c` | EDG | 196 | 4 | 192 | `0x48B3F0` | `0x4A1540` | P1.04--05 |
| 8 | `decl_spec.c` | EDG | 88 | 3 | 85 | `0x4A1BF0` | `0x4B37F0` | P1.05 |
| 9 | `declarator.c` | EDG | 64 | 0 | 64 | `0x4B3970` | `0x4C00A0` | P1.05 |
| 10 | `decls.c` | EDG | 207 | 5 | 202 | `0x4C0910` | `0x4E8C40` | P1.05--06 |
| 11 | `disambig.c` | EDG | 5 | 1 | 4 | `0x4E9E70` | `0x4EC690` | P1.06 |
| 12 | `error.c` | EDG | 51 | 1 | 50 | `0x4EDCD0` | `0x4F8F80` | P1.06 |
| 13 | `expr.c` | EDG | 538 | 10 | 528 | `0x4F9870` | `0x5565E0` | P1.07--08 |
| 14 | `exprutil.c` | EDG | 299 | 13 | 286 | `0x558720` | `0x583540` | P1.08--09 |
| 15 | `extasm.c` | EDG | 7 | 0 | 7 | `0x584CA0` | `0x585850` | P1.09 |
| 16 | `fe_init.c` | EDG | 6 | 1 | 5 | `0x585B10` | `0x5863A0` | P1.09 |
| 17 | `fe_wrapup.c` | EDG | 2 | 0 | 2 | `0x588D40` | `0x588F90` | P1.09 |
| 18 | `float_pt.c` | EDG | 79 | 0 | 79 | `0x589550` | `0x594150` | P1.09--10 |
| 19 | `folding.c` | EDG | 139 | 9 | 130 | `0x594B30` | `0x5A4FD0` | P1.10 |
| 20 | `func_def.c` | EDG | 56 | 1 | 55 | `0x5A51B0` | `0x5AAB80` | P1.10 |
| 21 | `host_envir.c` | EDG | 19 | 2 | 17 | `0x5AD540` | `0x5B1E70` | P1.10 |
| 22 | `il.c` | EDG | 358 | 16 | 342 | `0x5B28F0` | `0x5DFAD0` | P1.10--11d |
| 23 | `il_alloc.c` | EDG | 38 | 1 | 37 | `0x5E0600` | `0x5E8300` | P1.11a--11e |
| 24 | `il_to_str.c` | EDG | 83 | 1 | 82 | `0x5F7FD0` | `0x6039E0` | P1.11f--12 |
| 25 | `il_walk.c` | EDG | 27 | 1 | 26 | `0x603FE0` | `0x620190` | P1.12 |
| 26 | `interpret.c` | EDG | 216 | 5 | 211 | `0x620CE0` | `0x65DE10` | P1.12--13 |
| 27 | `layout.c` | EDG | 21 | 2 | 19 | `0x65EA50` | `0x665A60` | P1.13 |
| 28 | `lexical.c` | EDG | 140 | 5 | 135 | `0x666720` | `0x689130` | P1.13--14 |
| 29 | `literals.c` | EDG | 21 | 0 | 21 | `0x68ACC0` | `0x68F2B0` | P1.14 |
| 30 | `lookup.c` | EDG | 71 | 2 | 69 | `0x68FAB0` | `0x69BE80` | P1.14 |
| 31 | `lower_name.c` | EDG | 179 | 11 | 168 | `0x69C980` | `0x6AB280` | P1.14--15 |
| 32 | `macro.c` | EDG | 43 | 1 | 42 | `0x6AB6E0` | `0x6B5C10` | P1.15 |
| 33 | `mem_manage.c` | EDG | 9 | 2 | 7 | `0x6B6DD0` | `0x6BA230` | P1.15 |
| 34 | `nv_transforms.c` | **NVIDIA** | 1 | 0 | 1 | `0x6BE300` | `0x6BE300` | P1.15 |
| 35 | `overload.c` | EDG | 284 | 3 | 281 | `0x6BE4A0` | `0x6EF7A0` | P1.15--16 |
| 36 | `pch.c` | EDG | 23 | 3 | 20 | `0x6F2790` | `0x6F5DA0` | P1.16 |
| 37 | `pragma.c` | EDG | 28 | 0 | 28 | `0x6F61B0` | `0x6F8320` | P1.16 |
| 38 | `preproc.c` | EDG | 10 | 0 | 10 | `0x6F9B00` | `0x6FC940` | P1.16 |
| 39 | `scope_stk.c` | EDG | 186 | 6 | 180 | `0x6FE160` | `0x7106B0` | P1.16--17 |
| 40 | `src_seq.c` | EDG | 57 | 1 | 56 | `0x710F10` | `0x718720` | P1.17 |
| 41 | `statements.c` | EDG | 83 | 1 | 82 | `0x719300` | `0x726A50` | P1.17 |
| 42 | `symbol_ref.c` | EDG | 42 | 2 | 40 | `0x726F20` | `0x72CEA0` | P1.17 |
| 43 | `symbol_tbl.c` | EDG | 175 | 8 | 167 | `0x72D950` | `0x74B8D0` | P1.17--18 |
| 44 | `sys_predef.c` | EDG | 35 | 1 | 34 | `0x74C690` | `0x751470` | P1.18 |
| 45 | `target.c` | EDG | 11 | 0 | 11 | `0x7525F0` | `0x752DF0` | P1.18 |
| 46 | `templates.c` | EDG | 455 | 12 | 443 | `0x7530C0` | `0x794D30` | P1.18 |
| 47 | `trans_copy.c` | EDG | 2 | 0 | 2 | `0x796BA0` | `0x796BA0` | P1.18 |
| 48 | `trans_corresp.c` | EDG | 88 | 6 | 82 | `0x796E60` | `0x7A3420` | P1.18--19 |
| 49 | `trans_unit.c` | EDG | 10 | 0 | 10 | `0x7A3BB0` | `0x7A4690` | P1.19 |
| 50 | `types.c` | EDG | 88 | 5 | 83 | `0x7A4940` | `0x7C02A0` | P1.19 |
| 51 | `modules.c` | EDG | 22 | 3 | 19 | `0x7C0C60` | `0x7C2560` | P1.19 |
| 52 | `floating.c` | EDG | 50 | 9 | 41 | `0x7D0EB0` | `0x7D59B0` | P1.19 |

**Totals:** 5,338 cross-references across 52 `.c` files, resolving to 2,129 unique functions. With `.h` file references added, 2,209 unique functions are mapped.

### Largest Source Files by Function Count

| Source File | Main Body Funcs | Approximate Code Size |
|---|---|---|
| `expr.c` | 528 | ~373 KB (`0x4F9870`--`0x5565E0`) |
| `templates.c` | 443 | ~282 KB (`0x7530C0`--`0x794D30`) |
| `il.c` | 342 | ~185 KB (`0x5B28F0`--`0x5DFAD0`) |
| `exprutil.c` | 286 | ~175 KB (`0x558720`--`0x583540`) |
| `overload.c` | 281 | ~200 KB (`0x6BE4A0`--`0x6EF7A0`) |
| `class_decl.c` | 264 | ~187 KB (`0x419280`--`0x447930`) |
| `interpret.c` | 211 | ~241 KB (`0x620CE0`--`0x65DE10`) |
| `decls.c` | 202 | ~165 KB (`0x4C0910`--`0x4E8C40`) |
| `cp_gen_be.c` | 201 | ~141 KB (`0x466F90`--`0x489000`) |
| `decl_inits.c` | 192 | ~91 KB (`0x48B3F0`--`0x4A1540`) |

## Header File Cross-References

Thirteen `.h` header files appear in assertion strings. These are headers that contain non-trivial inline functions or macros that expand to assertion-bearing code. When a function compiled from `decls.c` triggers an assertion whose `__FILE__` is `types.h`, that assertion was inlined from `types.h` into the `decls.c` compilation unit.

| # | Header File | Xrefs | Stubs | Main Funcs | Address Range | Inlined Into |
|---|---|---|---|---|---|---|
| 1 | `decls.h` | 1 | 0 | 1 | `0x4E08F0` | `decls.c` |
| 2 | `float_type.h` | 63 | 0 | 63 | `0x7D1C90`--`0x7DEB90` | `floating.c` |
| 3 | `il.h` | 5 | 2 | 3 | `0x52ABC0`--`0x6011F0` | `expr.c`, `il.c`, `il_to_str.c` |
| 4 | `lexical.h` | 1 | 0 | 1 | `0x68F2B0` | `lexical.c` / `literals.c` boundary |
| 5 | `mem_manage.h` | 4 | 0 | 4 | `0x4EDCD0` | `error.c` |
| 6 | `modules.h` | 5 | 0 | 5 | `0x7C1100`--`0x7C2560` | `modules.c` |
| 7 | `nv_transforms.h` | 3 | 0 | 3 | `0x432280`--`0x719D20` | `class_decl.c`, `cp_gen_be.c`, `src_seq.c` |
| 8 | `overload.h` | 1 | 0 | 1 | `0x6C9E40` | `overload.c` |
| 9 | `scope_stk.h` | 4 | 0 | 4 | `0x503D90`--`0x574DD0` | `expr.c`, `exprutil.c` |
| 10 | `symbol_tbl.h` | 2 | 1 | 1 | `0x7377D0` | `symbol_tbl.c` |
| 11 | `types.h` | 17 | 4 | 13 | `0x469260`--`0x7B05E0` | Many files (scattered type queries) |
| 12 | `util.h` | 124 | 10 | 114 | `0x430E10`--`0x7C2B10` | All major `.c` files |
| 13 | `walk_entry.h` | 51 | 0 | 51 | `0x604170`--`0x618660` | `il_walk.c` |

### Notable Header Patterns

**`util.h`** is the most widely-included header, with 124 cross-references (114 in main body) spanning nearly the entire EDG `.text` region from `0x430E10` to `0x7C2B10`. It provides generic container templates (dynamic arrays, hash tables, sorted sets) used by every major subsystem. The EDG linker inlined these templates into each compilation unit, creating many small `util.h`-attributed functions scattered across the binary.

**`float_type.h`** is concentrated in a single 52 KB block at `0x7D1C90`--`0x7DEB90`, immediately after `floating.c`. It contains 63 template instantiations for IEEE 754 floating-point type operations (comparison, conversion, arithmetic) for each target floating-point width. These templates were instantiated in the `floating.c` compilation unit.

**`walk_entry.h`** contributes 51 functions in the tight range `0x604170`--`0x618660`, all within the `il_walk.c` region. These are the per-entry-kind callback dispatch functions generated by preprocessor macros in the IL walker header.

**`nv_transforms.h`** is NVIDIA-specific. Its 3 cross-references appear in `class_decl.c` (`sub_432280` at `0x432280`), `cp_gen_be.c` (`sub_47ECC0` at `0x47ECC0`), and `src_seq.c` (`sub_719D20` at `0x719D20`). These are the integration points where NVIDIA's CUDA transform hooks are called from standard EDG code paths -- class definition processing, backend code generation, and source sequence ordering.

## NVIDIA-Specific Files

### `nv_transforms.c`

The only NVIDIA-authored `.c` file in the EDG source tree. Despite having only **1 mapped function** via `__FILE__` (`sub_6BE300` at `0x6BE300`), the sweep analysis of the `0x6BAE70`--`0x6BE4A0` range identified approximately 40 functions compiled from this file. The discrepancy exists because `nv_transforms.c` uses NVIDIA's own assertion macros (not EDG's standard `internal_error` path), so most functions do not reference the EDG-style `__FILE__` string.

Functions confirmed in the `nv_transforms.c` region:

| Address | Identity | Purpose |
|---|---|---|
| `0x6BAE70` | `nv_init_transforms` | Zero all NVIDIA transform state at startup |
| `0x6BAF70` | `alloc_mem_block` | 64 KB memory block allocator for NV region pools |
| `0x6BB290` | `reset_mem_state` | Emergency OOM recovery -- clear memory tracking |
| `0x6BB350` | `init_memory_regions` | Bootstrap region 0 and region 1 with initial blocks |
| `0x6BB790` | `emit_device_lambda_wrapper` | Generate `__nv_dl_wrapper_t<>` specialization |
| `0x6BCC20` | `emit_lambda_preamble` | Inject lambda wrapper preamble declarations |
| `0x6BD490` | `emit_host_device_lambda_wrapper` | Generate `__nv_hdl_wrapper_t<>` specialization |
| `0x6BE300` | (mapped function) | Single function with EDG-style `__FILE__` reference |

Key infrastructure in this file:
- `__nv_dl_wrapper_t<>` / `__nv_hdl_wrapper_t<>` struct template generation
- Host reference array emission (`.nvHRKE`, `.nvHRKI`, `.nvHRDE`, `.nvHRDI`, `.nvHRCE`, `.nvHRCI`)
- Capture count bitmask tables: `unk_1286980` (device) and `unk_1286900` (host-device), 128 bytes each
- Lambda-to-closure entity mapping via hash table at `qword_12868F0`

### `nv_transforms.h`

NVIDIA's hook header, `#include`-d from three EDG source files. It declares the functions that bridge standard EDG processing to NVIDIA's CUDA transform layer. The three inclusion sites represent the three points where EDG's standard C++ frontend cedes control to NVIDIA-specific logic:

1. **`class_decl.c`** (`sub_432280` at `0x432280`): Called during class definition processing to apply CUDA execution-space attributes to closure types and validate lambda capture constraints.

2. **`cp_gen_be.c`** (`sub_47ECC0` at `0x47ECC0`): Called during backend code generation to emit CUDA-specific output constructs (device stubs, host reference arrays, registration calls).

3. **`src_seq.c`** (`sub_719D20` at `0x719D20`): Called during source sequence processing to inject NVIDIA preamble declarations and wrapper type definitions into the correct position in the declaration order.

## Unmapped Regions (Gap Analysis)

Several address ranges within the EDG `.text` region contain functions that could not be mapped to any source file via `__FILE__` strings. The major gaps and their probable contents:

| Gap Range | Size | Probable Content | Evidence |
|---|---|---|---|
| `0x408B40`--`0x409350` | ~2 KB | Static constructors (`ctor_001`--`ctor_015`) | No source path; global table initializers |
| `0x447930`--`0x44B250` | ~13 KB | `class_decl.c` / `cmd_line.c` boundary helpers | Between confirmed ranges |
| `0x459630`--`0x461C20` | ~34 KB | `cmd_line.c` tail + `const_ints.c` preamble | Unmapped option handlers |
| `0x5E8300`--`0x5F7FD0` | ~87 KB | IL display routines (`il_to_str.c` early body) | No assertions (display-only code) |
| `0x665A60`--`0x666720` | ~3 KB | `layout.c` / `lexical.c` boundary | Small gap between confirmed ranges |
| `0x689130`--`0x68ACC0` | ~7 KB | `lexical.c` tail + `literals.c` preamble | Token/literal conversion helpers |
| `0x6AB280`--`0x6AB6E0` | ~1 KB | `lower_name.c` / `macro.c` boundary | Mangling helpers |
| `0x6BA230`--`0x6BAE70` | ~3 KB | `mem_manage.c` / `nv_transforms.c` boundary | Memory infrastructure |
| `0x6EF7A0`--`0x6F2790` | ~12 KB | `overload.c` / `pch.c` boundary | Overload resolution helpers |
| `0x6FC940`--`0x6FE160` | ~6 KB | `preproc.c` / `scope_stk.c` boundary | Preprocessor tail |
| `0x751470`--`0x7525F0` | ~7 KB | `sys_predef.c` / `target.c` boundary | Predefined macro infrastructure |
| `0x7A4690`--`0x7A4940` | ~1 KB | `trans_unit.c` / `types.c` boundary | Translation unit helpers |
| `0x7C2560`--`0x7D0EB0` | ~59 KB | Type-name mangling / encoding for output | Between `modules.c` and `floating.c` |
| `0x7D1C90`--`0x7DEB90` | ~52 KB | `float_type.h` template instantiations | Confirmed via `.h` path strings |
| `0x7DFFF0`--`0x82A000` | ~304 KB | C++ runtime, demangler, soft-float, EH | Statically-linked libstdc++/libgcc |

The largest unmapped gap within EDG code is the IL display region at `0x5E8300`--`0x5F7FD0` (87 KB). These functions were compiled from `il_to_str.c` but contain no assertions because the display/dump subsystem was built without assertion macros -- it is purely diagnostic code that formats IL trees to stdout.

The `float_type.h` block at `0x7D1C90`--`0x7DEB90` (52 KB) is technically mapped via `.h` cross-references but has no `.c` file attribution because the template instantiations carry only the header's `__FILE__` path.

## Alphabetical Ordering Observation

The files are laid out in the binary in rough alphabetical order, consistent with a build system that compiles object files in directory-listing order and a linker that processes them sequentially:

```text
0x409350  attribute.c      (a)
0x419280  class_decl.c     (c)
0x44B250  cmd_line.c       (c)
0x461C20  const_ints.c     (c)
0x466F90  cp_gen_be.c      (c)
0x48A1B0  debug.c          (d)
0x48B3F0  decl_inits.c     (d)
0x4A1BF0  decl_spec.c      (d)
0x4B3970  declarator.c     (d)
0x4C0910  decls.c          (d)
0x4E9E70  disambig.c       (d)
0x4EDCD0  error.c          (e)
0x4F9870  expr.c           (e)
0x558720  exprutil.c       (e)
0x584CA0  extasm.c         (e)
0x585B10  fe_init.c        (f)
0x588D40  fe_wrapup.c      (f)
0x589550  float_pt.c       (f)
0x594B30  folding.c        (f)
0x5A51B0  func_def.c       (f)
0x5AD540  host_envir.c     (h)
0x5B28F0  il.c             (i)
0x5E0600  il_alloc.c       (i)
0x5F7FD0  il_to_str.c      (i)
0x603FE0  il_walk.c        (i)
0x620CE0  interpret.c      (i)
0x65EA50  layout.c         (l)
0x666720  lexical.c        (l)
0x68ACC0  literals.c       (l)
0x68FAB0  lookup.c         (l)
0x69C980  lower_name.c     (l)
0x6AB6E0  macro.c          (m)
0x6B6DD0  mem_manage.c     (m)
0x6BAE70  nv_transforms.c  (n)  [region start; mapped func at 0x6BE300]
0x6BE4A0  overload.c       (o)
0x6F2790  pch.c            (p)
0x6F61B0  pragma.c         (p)
0x6F9B00  preproc.c        (p)
0x6FE160  scope_stk.c      (s)
0x710F10  src_seq.c        (s)
0x719300  statements.c     (s)
0x726F20  symbol_ref.c     (s)
0x72D950  symbol_tbl.c     (s)
0x74C690  sys_predef.c     (s)
0x7525F0  target.c         (t)
0x7530C0  templates.c      (t)
0x796BA0  trans_copy.c     (t)
0x796E60  trans_corresp.c  (t)
0x7A3BB0  trans_unit.c     (t)
0x7A4940  types.c          (t)
0x7C0C60  modules.c        (m)  [breaks alphabetical order]
0x7D0EB0  floating.c       (f)  [breaks alphabetical order]
```

Two files break the alphabetical pattern: `modules.c` at `0x7C0C60` and `floating.c` at `0x7D0EB0`. Both appear after `types.c` instead of in their expected positions (between `mem_manage.c` and `nv_transforms.c` for `modules.c`, between `float_pt.c` and `folding.c` for `floating.c`). This suggests these two files are compiled as separate translation units outside the main EDG source directory, or are added to the link line after the alphabetically-sorted EDG objects.

## Data Source

All mappings were extracted from the binary's `.rodata` string table. The extraction command:

```bash
jq '[.[] | select(.value | test("/dvs/p4/.*\\.c$")) |
  {file: (.value | split("/") | last),
   xrefs: [.xrefs[].func] | length}
] | sort_by(.file)' cudafe++_strings.json
```

The full build path for every source file is:

```text
/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/<filename>
```

Address ranges were verified against the 20 sweep reports (P1.01 through P1.20) produced during the binary analysis phase.
