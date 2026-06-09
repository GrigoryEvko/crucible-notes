# EDG 6.6 Overview

cudafe++ is built on top of Edison Design Group's (EDG) commercial C++ frontend, version 6.6. EDG provides the complete C++ language implementation — lexer, preprocessor, parser, semantic analysis, type system, template instantiation, overload resolution, constant evaluation, and Itanium ABI name mangling. NVIDIA licenses this frontend and compiles it from source with CUDA-specific modifications injected at three distinct integration levels: a dedicated NVIDIA source file (`nv_transforms.c`), surgical modifications to EDG source files that call into NVIDIA headers, and a large layer of CUDA property-query leaf functions that permeate every compilation phase.

The build path embedded in the binary is:

```text
/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/
```

## Source Tree

The binary contains debug path references to 52 `.c` files and 13 `.h` files. Together these constitute the entire EDG frontend plus NVIDIA's single dedicated source file.

### Source Files (.c)

| # | File | Pipeline role |
|---|---|---|
| 1 | `attribute.c` | C++11/GNU/CUDA attribute parsing and validation |
| 2 | `class_decl.c` | Class/struct/union declaration processing, lambda scanning |
| 3 | `cmd_line.c` | Command-line argument parsing (276 flags) |
| 4 | `const_ints.c` | Compile-time integer constant evaluation |
| 5 | `cp_gen_be.c` | **Backend** — `.int.c` code generation, source sequence walking |
| 6 | `debug.c` | Debug output and IL dump infrastructure |
| 7 | `decl_inits.c` | Declaration initializer processing |
| 8 | `decl_spec.c` | Declaration specifier parsing (storage class, type qualifiers) |
| 9 | `declarator.c` | Declarator parsing (pointers, arrays, function signatures) |
| 10 | `decls.c` | General declaration processing |
| 11 | `disambig.c` | Syntactic disambiguation (expression vs. declaration) |
| 12 | `error.c` | Diagnostic message formatting and emission (3,795 messages) |
| 13 | `expr.c` | Expression parsing and semantic analysis |
| 14 | `exprutil.c` | Expression utility functions (coercion, evaluation) |
| 15 | `extasm.c` | Extended inline assembly parsing |
| 16 | `fe_init.c` | Frontend initialization (36 subsystem init routines) |
| 17 | `fe_wrapup.c` | Frontend finalization (5-pass wrapup sequence) |
| 18 | `float_pt.c` | Floating-point literal parsing |
| 19 | `floating.c` | IEEE 754 constant folding (arbitrary precision) |
| 20 | `folding.c` | General constant folding |
| 21 | `func_def.c` | Function definition processing |
| 22 | `host_envir.c` | Host environment interface (file I/O, exit, signals) |
| 23 | `il.c` | IL node creation, linking, and management |
| 24 | `il_alloc.c` | IL arena allocator (region-based, 64KB blocks) |
| 25 | `il_to_str.c` | IL-to-string conversion for debug display |
| 26 | `il_walk.c` | IL tree walking with 5 callback functions |
| 27 | `interpret.c` | Constexpr interpreter (compile-time evaluation engine) |
| 28 | `layout.c` | Struct/class memory layout computation |
| 29 | `lexical.c` | Lexer / tokenizer (357 token kinds) |
| 30 | `literals.c` | String and numeric literal processing |
| 31 | `lookup.c` | Name lookup (unqualified, qualified, ADL) |
| 32 | `lower_name.c` | Itanium ABI name mangling |
| 33 | `macro.c` | Preprocessor macro expansion |
| 34 | `mem_manage.c` | Internal memory management (arena allocator, tracking) |
| 35 | `modules.c` | C++20 module support (mostly stubs in CUDA build) |
| 36 | **`nv_transforms.c`** | **NVIDIA-authored** — CUDA AST transforms, lambda wrappers |
| 37 | `overload.c` | C++ overload resolution |
| 38 | `pch.c` | Precompiled header support |
| 39 | `pragma.c` | Pragma processing (43 pragma kinds) |
| 40 | `preproc.c` | Preprocessor directives (#include, #ifdef, etc.) |
| 41 | `scope_stk.c` | Scope stack management |
| 42 | `src_seq.c` | Source sequence (declaration ordering for emission) |
| 43 | `statements.c` | Statement parsing and semantic analysis |
| 44 | `symbol_ref.c` | Symbol reference tracking |
| 45 | `symbol_tbl.c` | Symbol table operations (hash-based lookup) |
| 46 | `sys_predef.c` | System predefinitions (built-in types, macros) |
| 47 | `target.c` | Target configuration (data model, ABI) |
| 48 | `templates.c` | Template instantiation, specialization, deduction |
| 49 | `trans_copy.c` | Translation unit IL deep copy |
| 50 | `trans_corresp.c` | Cross-TU type correspondence verification (RDC) |
| 51 | `trans_unit.c` | Translation unit lifecycle (the main entry point) |
| 52 | `types.c` | C++ type system (22 type kinds, queries, construction) |

### Header Files (.h)

| # | File | Contents |
|---|---|---|
| 1 | `decls.h` | Declaration node structure definitions |
| 2 | `float_type.h` | Floating-point type descriptors |
| 3 | `il.h` | IL entry kind enums, node structure definitions |
| 4 | `lexical.h` | Token kind enums, lexer state |
| 5 | `mem_manage.h` | Memory allocator interface |
| 6 | `modules.h` | Module system declarations |
| 7 | **`nv_transforms.h`** | **NVIDIA-authored** — CUDA transform API, called from EDG files |
| 8 | `overload.h` | Overload resolution structures |
| 9 | `scope_stk.h` | Scope stack interface |
| 10 | `symbol_tbl.h` | Symbol table interface |
| 11 | `types.h` | Type node structure, type kind enum |
| 12 | `util.h` | General utility macros and inline functions |
| 13 | `walk_entry.h` | IL walking callback signatures |

## Code Breakdown

The binary contains approximately 6,300 identifiable functions in the EDG portion of the code:

| Category | Functions | % of binary | Description |
|---|---|---|---|
| Attributed to source files | ~2,200 | ~35% | Matched to one of the 52 `.c` files via assert strings, source path references, or address-range mapping |
| Unmapped EDG functions | ~2,900 | ~46% | EDG code without source file attribution (inlined, optimized, or from headers) |
| C++ runtime / ABI | ~1,200 | ~19% | Itanium ABI runtime, exception handling, `std::` library, operator new/delete |

### Top 10 Source Files by Function Count

Counts below give main-body function totals derived from `__FILE__` cross-references in `.rodata`. For the full per-file breakdown (stubs vs main body, address ranges, code sizes) see the [Function Map](../function-map.md). Confidence: HIGH.

| Rank | File | Main Funcs | Primary responsibility |
|---|---|---|---|
| 1 | `expr.c` | 528 | Expression parsing, operator semantics, implicit conversions |
| 2 | `templates.c` | 443 | Template instantiation worklist, SFINAE, deduction |
| 3 | `il.c` | 342 | IL node creation, entry kind dispatch, node linking |
| 4 | `exprutil.c` | 286 | Expression coercion, arithmetic conversions, lvalue analysis |
| 5 | `overload.c` | 281 | Candidate set construction, ICS ranking, best viable function |
| 6 | `class_decl.c` | 264 | Class body parsing, member declarations, lambda scanning |
| 7 | `interpret.c` | 211 | Constexpr interpreter (compile-time evaluation engine) |
| 8 | `decls.c` | 202 | General declaration processing |
| 9 | `cp_gen_be.c` | 201 | Backend emission, `.int.c` generation, device stub writing |
| 10 | `decl_inits.c` | 192 | Declaration initializer processing |

## Architecture: Classic Frontend Pipeline

EDG implements a textbook multi-pass compiler frontend. cudafe++ drives it in a single-threaded, sequential pipeline from `main()` at `0x408950`:

```text
  source.cu
     |
     v
  +-----------+     lexical.c, macro.c, preproc.c, literals.c
  |  Lexer /  |     357 token kinds, trigraph handling, raw string
  |  Preproc  |     adjustment, __CUDA_ARCH__ macro injection
  +-----------+
     |  token stream
     v
  +-----------+     expr.c, declarator.c, decl_spec.c, statements.c,
  |  Parser   |     class_decl.c, disambig.c, func_def.c, extasm.c
  |           |     Recursive-descent with disambiguation
  +-----------+
     |  parse tree
     v
  +-----------+     overload.c, exprutil.c, lookup.c, templates.c,
  | Semantic  |     types.c, attribute.c, const_ints.c, folding.c
  | Analysis  |     Type checking, overload resolution, template
  |           |     instantiation, constexpr evaluation
  +-----------+
     |  annotated AST
     v
  +-----------+     il.c, il_alloc.c, il_walk.c, scope_stk.c,
  |  IL Build |     symbol_tbl.c, src_seq.c, trans_unit.c
  |           |     Scope-linked graph of all declarations, types,
  |           |     expressions, statements, templates
  +-----------+
     |  IL graph
     v
  +-----------+     fe_wrapup.c, lower_name.c, trans_corresp.c
  |  Wrapup   |     5-pass finalization: dead code marking,
  |           |     name lowering, cross-TU correspondence (RDC)
  +-----------+
     |  finalized IL
     v
  +-----------+     cp_gen_be.c, nv_transforms.c, host_envir.c
  |  Backend  |     Walk source sequence, emit .int.c file,
  | Emission  |     inject CUDA stubs, lambda wrappers, host
  |           |     reference arrays, managed variable boilerplate
  +-----------+
     |
     v
  output.int.c
```

The `process_translation_unit` function (`sub_7A40A0` in `trans_unit.c`) is the main entry point for compilation. It allocates a 424-byte TU descriptor, opens the source file, and orchestrates the parse-to-IL sequence. For the main compilation path, it calls:

1. `sub_586240` — parse the translation unit (drives lexer + parser)
2. `sub_4E8A60` — standard compilation finalization (IL completion)
3. `sub_588F90` — `fe_wrapup` (5-pass IL finalization)
4. `sub_489000` — backend entry (`.int.c` emission, "Back end time")

## NVIDIA Modifications

NVIDIA's CUDA integration is organized in three layers, from most isolated to most pervasive.

### Level 1: NVIDIA-Authored Source (`nv_transforms.c` + `nv_transforms.h`)

A single dedicated NVIDIA source file at address range `0x6BAE70`--`0x6BE4A0`, containing approximately 34 functions in ~14KB of code. This file implements all CUDA-specific AST transformations:

| Function | Address | Purpose |
|---|---|---|
| `nv_init_transforms` | `0x6BAE70` | Zero all NVIDIA transform state at startup |
| `emit_device_lambda_wrapper` | `0x6BB790` | Generate `__nv_dl_wrapper_t<Tag, F1..FN>` partial specialization |
| `emit_hdl_wrapper` (non-mutable) | `0x6BBB10` | Generate `__nv_hdl_wrapper_t<false, ...>` type-erased wrapper |
| `emit_hdl_wrapper` (mutable) | `0x6BBEE0` | Same as above but `operator()` is non-const |
| `emit_array_capture_helpers` | `0x6BC290` | Generate `__nv_lambda_array_wrapper` for 2D-8D arrays |
| `nv_validate_cuda_attributes` | `0x6BC890` | Validate `__launch_bounds__`, `__cluster_dims__`, `__maxnreg__` |
| `nv_reset_capture_bitmasks` | `0x6BCBC0` | Zero device/host-device capture bitmasks per TU |
| `nv_record_capture_count` | `0x6BCBF0` | Set bit N in capture bitmap for wrapper generation |
| `nv_emit_lambda_preamble` | `0x6BCC20` | Master emitter: inject all `__nv_*` templates into compilation |
| `nv_find_parent_lambda_function` | `0x6BCDD0` | Walk scope chain for enclosing device/global function |
| `nv_emit_host_reference_array` | `0x6BCF80` | Generate `.nvHRKE`/`.nvHRDI`/etc. ELF section arrays |
| `nv_get_full_nv_static_prefix` | `0x6BE300` | Build scoped name + register entity in host ref arrays |

The companion header `nv_transforms.h` declares the API surface that EDG source files call into. This is the primary NVIDIA integration point — EDG code never calls `nv_transforms.c` functions directly; it calls through the header's declarations.

Key data structures managed by `nv_transforms.c`:

| Global | Size | Purpose |
|---|---|---|
| `unk_1286980` | 128 bytes (1024 bits) | Device lambda capture-count bitmap |
| `unk_1286900` | 128 bytes (1024 bits) | Host-device lambda capture-count bitmap |
| `qword_12868F0` | pointer | Entity-to-closure ID hash table |
| `qword_1286A00` | pointer | Cached anonymous namespace name (`_GLOBAL__N_<file>`) |
| `qword_1286760` | pointer | Cached static name prefix string |
| `unk_1286780`--`unk_12868C0` | 6 lists | Host reference array symbol lists (one per section type) |
| `dword_126E270` | 4 bytes | C++17 noexcept-in-type-system flag |

### Level 2: NVIDIA-Modified EDG Files

Three EDG source files contain direct calls into `nv_transforms.h` functions, making them the "NVIDIA-aware" EDG files:

**`cp_gen_be.c`** — The backend code generator. When it encounters a type named `__nv_lambda_preheader_injection` during source sequence walking, it calls `nv_emit_lambda_preamble` (`sub_6BCC20`) to inject the entire `__nv_*` template library. It also calls NVIDIA functions for host reference array emission, managed variable boilerplate, and device stub generation.

**`class_decl.c`** — The class/struct declaration processor. The `scan_lambda` function (`sub_447930`, 2113 lines) detects `__host__`/`__device__` annotations on lambda expressions, validates CUDA-specific constraints (35+ error codes in range 3592--3690), and records capture counts in the bitmaps via `nv_record_capture_count`.

**`statements.c`** — The statement parser. Calls NVIDIA transform functions for statement-level CUDA validation, such as checking that `__syncthreads()` is not called in divergent control flow within `__global__` functions.

### Level 3: CUDA Property Query Layer

The most pervasive integration layer consists of 104 small leaf functions clustered at addresses `0x7A6000`--`0x7AA000` (within `types.c`). These are type-system query functions that answer questions like "is this type a `__device__` pointer?", "does this class have `__shared__` storage?", "is this a kernel function type?".

Each follows a canonical pattern:

```c
bool is_<property>_type(type_node *t) {
    while (t->kind == 12)       // 12 = tk_typedef
        t = t->referenced_type; // strip typedef layers
    return <check on underlying type>;
}
```

These 104 accessors account for 3,648 total call sites across the binary. The top callers by call-site count:

| Address | Callers | Identity | Returns |
|---|---|---|---|
| `0x7A8A30` | 407 | `is_class_or_struct_or_union_type` | kind in {9, 10, 11} |
| `0x7A9910` | 389 | `type_pointed_to` | `ptr->referenced_type` (kind == 6) |
| `0x7A9E70` | 319 | `get_cv_qualifiers` | accumulated cv-qual bits (& 0x7F) |
| `0x7A6B60` | 299 | `is_dependent_type` | bit 5 of byte +133 |
| `0x7A7630` | 243 | `is_object_pointer_type` | kind == 6 && !(bit 0 of +152) |
| `0x7A8370` | 221 | `is_array_type` | kind == 8 |
| `0x7A7B30` | 199 | `is_member_pointer_or_ref` | kind == 6 && (bit 0 of +152) |
| `0x7A6AC0` | 185 | `is_reference_type` | kind == 7 |
| `0x7A8DC0` | 169 | `is_function_type` | kind == 14 |
| `0x7A6E90` | 140 | `is_void_type` | kind == 1 |

CUDA integration is pervasive because these tiny accessors are called from every phase of compilation — the parser checks execution space during declaration, semantic analysis validates cross-space calls, the type system queries CUDA qualifiers during overload resolution, and the backend reads them during IL emission. There is no isolated "CUDA layer"; the CUDA awareness is distributed across the entire frontend through these leaf functions.

### Type Kind Constants

The type query functions operate on a `type_node` structure (176 bytes, IL entry kind 6). The `kind` field at offset +132 encodes:

| kind | Name | Description |
|---|---|---|
| 0 | `tk_none` | Null/invalid |
| 1 | `tk_void` | `void` |
| 2 | `tk_integer` | All integer types including `bool`, `char`, enums |
| 3 | `tk_float` | `float` |
| 4 | `tk_double` | `double` |
| 5 | `tk_long_double` | `long double` |
| 6 | `tk_pointer` | Pointer types (object and member) |
| 7 | `tk_reference` | Lvalue reference (`T&`) |
| 8 | `tk_array` | Array types (`T[]`, `T[N]`) |
| 9 | `tk_struct` | `struct` |
| 10 | `tk_class` | `class` |
| 11 | `tk_union` | `union` |
| 12 | `tk_typedef` | Typedef alias (stripped by all query functions) |
| 13 | `tk_pointer_to_member` | Pointer-to-member (`T C::*`) |
| 14 | `tk_function` | Function type |
| 15 | `tk_bitfield` | Bit-field |
| 16 | `tk_pack_expansion` | Parameter pack expansion |
| 17 | `tk_pack_expansion` | Alternate pack expansion form |
| 18 | `tk_auto` | `auto` / `decltype(auto)` placeholder |
| 19 | `tk_rvalue_reference` | Rvalue reference (`T&&`) |
| 20 | `tk_nullptr_t` | `std::nullptr_t` |

## Memory Management

EDG uses a custom region-based arena allocator implemented in `mem_manage.c` (address range `0x6B5E40`--`0x6BA230`). Key characteristics:

- **Block size:** 64KB (0x10000) per block
- **Region model:** Multiple numbered regions (file-scope = region 1, per-function = region N)
- **Free list recycling:** Freed blocks go to `qword_1280730` for reuse before new allocation
- **Trim threshold:** Blocks with more than 1,887 unused bytes are split; remainder goes to free list
- **Tracking:** All allocations recorded for watermark monitoring (`qword_1280718` = total, `qword_1280710` = peak)
- **Dual mode:** Malloc-based (mode 0) or mmap-based (mode 1), selected by `dword_1280728` from CLI flag

Block structure (48+ bytes header per 64KB block):

| Offset | Type | Field |
|---|---|---|
| +0 | `void*` | Next pointer (block chain) |
| +8 | `void*` | Current allocation pointer |
| +16 | `void*` | High-water mark within block |
| +24 | `void*` | End-of-block pointer |
| +32 | `int64` | Block total size (0 if sub-block) |
| +40 | `byte` | Trimmed flag |
| +48 | — | Start of usable data |

The `free_fe` function (`sub_6BA230`, 533 lines) implements a hash-table-based deduplicating allocator for front-end object deallocation, using open addressing with linear probing.

## C++20 Modules (Stubs)

The `modules.c` file (address range `0x7C0C60`--`0x7C2560`) contains approximately 20 functions implementing the C++20 module import/export interface. CUDA does not support C++20 modules, so most functions are stubs that return 0:

- `has_pending_template_definition_from_module` — returns 0
- `has_pending_template_specializations_from_module` — returns 0
- Seven additional stub functions at `0x7C2350`--`0x7C2410` — all return 0

The non-stub functions handle the binary module interface file format (magic header `{0x9A, 0x13, 0x37, 0x7D}`) and basic module name matching, likely preserved from the EDG baseline for future CUDA module support.

## Cross-TU Correspondence (RDC Mode)

When compiling with Relocatable Device Code (`--rdc`), multiple translation units are processed sequentially. The `trans_corresp.c` file (address range `0x7A00D0`--`0x7A38A0`) implements structural equivalence checking between types from different TUs:

- `verify_class_type_correspondence` (`sub_7A00D0`, 703 lines) — Deep comparison of class types: base classes, friend declarations, member functions, nested types, template parameters
- `verify_enum_type_correspondence` (`sub_7A0E10`) — Enum underlying type and enumerator list comparison
- `verify_function_type_correspondence` (`sub_7A1230`) — Parameter list and return type comparison
- `set_type_correspondence` (`sub_7A1460`) — Links two corresponding types across TUs

The `trans_unit.c` file manages TU lifecycle with a stack-based model:

| Global | Purpose |
|---|---|
| `qword_106BA10` | Current translation unit pointer |
| `qword_106B9F0` | Primary (first) translation unit |
| `qword_106BA18` | TU stack top |
| `dword_106B9E8` | TU stack depth (excluding primary) |

`process_translation_unit` (`sub_7A40A0`) allocates a 424-byte TU descriptor and drives the parse-to-completion sequence. `switch_translation_unit` (`sub_7A3D60`) saves/restores per-TU state (registered variables, scope stack, file scope) when switching between TUs during RDC compilation.

## Cross-References

- [Pipeline Overview](../pipeline/overview.md) — How EDG stages map to the 8-stage pipeline
- [IL Overview](../il/overview.md) — The 85 entry kinds that EDG produces
- [Extended Lambda Overview](../lambda/overview.md) — The `nv_transforms.c` lambda pipeline in detail
- [Type System](type-system.md) — Deep dive on 22 type kinds and class layout
- [Template Engine](template-engine.md) — Template instantiation worklist
- [Name Mangling](name-mangling.md) — Itanium ABI encoding with CUDA extensions
- [Lexer](lexer.md) — Tokenizer and keyword registration
- [Overload Resolution](overload-resolution.md) — Candidate evaluation and ICS ranking
- [Diagnostics Overview](../diagnostics/overview.md) — The 3,795 error message system
