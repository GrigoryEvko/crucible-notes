# Frontend Invocation

`process_translation_unit` (`sub_7A40A0`, 1267 bytes at `0x7A40A0`, from EDG source file `trans_unit.c`) is the main frontend workhorse -- stage 5 of the [pipeline](./overview.md). Called once from `main()`, it orchestrates the entire transformation from `.cu` source text to a fully-built EDG IL tree. The function allocates a 424-byte translation unit descriptor, opens the source file via the lexer, drives the C++ parser to completion, runs semantic analysis on the parsed declarations, and finally performs per-TU wrapup (stop-token verification, class linkage checking, module finalization). By the time it returns, every declaration, type, expression, and statement from the source has been parsed into IL nodes, CUDA execution-space attributes have been resolved, and the TU is linked into the global TU chain ready for the 5-pass [fe_wrapup](./fe-wrapup.md) stage.

## Key Facts

| Property | Value |
|---|---|
| Function | `sub_7A40A0` (`process_translation_unit`) |
| Binary address | `0x7A40A0` |
| Binary size | 1267 bytes |
| EDG source | `trans_unit.c` |
| Confidence | DEFINITE (source path and function name embedded at lines 696, 725, 556) |
| Signature | `int process_translation_unit(char *filename, int is_recompilation, void *module_info)` |
| Direct callees | 27 |
| Debug trace entry | `"Processing translation unit %s\n"` |
| Debug trace exit | `"Done processing translation unit %s\n"` |
| TU descriptor size | 424 bytes (allocated via `sub_6BA0D0`) |
| TU stack entry size | 16 bytes (`[0]=next`, `[8]=tu_ptr`) |

## Annotated Decompilation

```c
int process_translation_unit(char *filename,       // source file path
                              int is_recompilation, // nonzero on error-retry pass
                              void *module_info)    // non-NULL for C++20 module TUs
{
    bool is_primary = (module_info == NULL);

    // --- Debug trace on entry ---
    if (debug_verbosity > 0 || (debug_enabled && trace_category("trans_unit")))
        fprintf(stderr, "Processing translation unit %s\n", filename);

    // --- Module-mode state validation ---
    // If this is a primary TU (no module_info) but we've already seen a module TU,
    // that's an internal consistency error.
    if (is_recompilation)
        goto skip_validation;
    if (!is_primary) {
skip_validation:
        if (module_info)
            has_seen_module_tu = 1;                          // dword_12C7A88
        goto proceed;
    }
    if (has_seen_module_tu)
        assertion_failure("trans_unit.c", 696, "process_translation_unit", 0, 0);

proceed:
    // --- Save previous TU state if any ---
    if (current_translation_unit)                            // qword_106BA10
        save_translation_unit_state(current_translation_unit);  // sub_7A3A50

    // --- Reset per-TU compilation state ---
    current_source_position = 0;                             // qword_126DD38
    is_recompilation_flag = is_recompilation;                 // dword_106BA08
    current_filename = filename;                             // qword_106BA00
    has_module_info = (module_info != NULL);                  // dword_106B9F8

    // --- Initialize error/parser state ---
    reset_error_state();                                     // sub_5EAEC0
    if (is_recompilation)
        fe_init_part_1();                                    // sub_585EE0

    // ==========================================================
    //  PHASE 1: Allocate and initialize TU descriptor (424 bytes)
    // ==========================================================
    registration_complete = 1;                               // dword_12C7A8C
    tu_descriptor *tu = allocate_storage(424);               // sub_6BA0D0
    tu->next_tu = NULL;                                      // [0]
    ++tu_count;                                              // qword_12C7A78
    tu->storage_buffer = allocate_storage(per_tu_storage_size); // [16], sub_6BA0D0
    tu->tu_name = NULL;                                      // [8]
    init_scope_state(tu + 24);                               // sub_7046E0, offsets [24..192]
    tu->field_192 = 0;
    tu->field_352 = 0;
    tu->field_184 = 0;
    memset(&tu->scope_decl_area, 0, ...);                    // [200..360] zeroed
    tu->field_360 = 0;
    tu->field_368 = 0;
    tu->field_376 = 0;
    tu->flags = 0x0100;                                      // [392] = "initialized"
    tu->error_severity_count = 0;                            // [408]
    tu->field_416 = 0;

    // --- Copy registered variable defaults into per-TU storage ---
    for (reg = registered_variable_list; reg; reg = reg->next) {
        if (reg->offset_in_tu)
            *(tu + reg->offset_in_tu) = reg->variable_value;
    }

    // --- Set module info pointer and primary flag ---
    tu->module_info_ptr = module_info;                       // [376]
    tu->is_primary = is_primary;                             // [392] byte 0

    // ==========================================================
    //  PHASE 2: Link TU into global chains
    // ==========================================================

    // --- Set as primary TU if this is the first ---
    if (primary_translation_unit == NULL) {                   // qword_106B9F0
        primary_translation_unit = tu;
        if (!is_recompilation)
            assertion_failure("trans_unit.c", 725, "process_translation_unit", 0, 0);
    }

    // --- Push onto TU stack ---
    current_translation_unit = tu;                           // qword_106BA10
    // (stack entry allocated from free list or via permanent alloc)
    stack_entry = alloc_stack_entry();                        // 16 bytes
    stack_entry->tu_ptr = tu;
    stack_entry->next = tu_stack_top;
    if (tu != primary_translation_unit)
        ++tu_stack_depth;                                    // dword_106B9E8
    tu_stack_top = stack_entry;                               // qword_106BA18

    // --- Append to TU linked list ---
    if (tu_chain_tail)                                       // qword_12C7A90
        tu_chain_tail->next_tu = tu;
    tu_chain_tail = tu;

    // ==========================================================
    //  PHASE 3: Source file setup + parse
    // ==========================================================

    if (module_info) {
        // --- Module compilation path ---
        // Extract header info from module descriptor
        module_id = module_info[7];
        module_info[2] = tu;                                 // back-link TU into module
        current_module_id = module_id;                       // qword_106C0B0
        // ... copy include paths, source paths from module descriptor ...
        source_dir = intern_directory_path(filename, 1);     // sub_5ADC60
        set_include_paths(source_dir, &include_list, &sys_list); // sub_5AD120
        fe_translation_unit_init(source_dir, &include_list); // sub_5863A0
        import_module = module_info[3];
        tu->error_severity_count = current_error_severity;   // [408]
        set_module_id(import_module);                        // sub_5AF7F0
        if (preprocessing_only)                              // dword_106C29C
            goto compile;
        goto compile_module;
    }

    // --- Standard (non-module) path ---
    fe_translation_unit_init(0, 0);                          // sub_5863A0
    tu->error_severity_count = current_error_severity;
    if (preprocessing_only)
        goto compile;

    // --- PCH header processing (optional) ---
    if (pch_enabled && !pch_skip_flag) {                     // dword_106BF18, dword_106B6AC
        setup_pch_source();                                  // sub_5861C0
        precompiled_header_processing();                     // sub_6F4AD0
    }

compile:
    // --- Main compilation: parse + build IL ---
    compile_primary_source();                                // sub_586240
    semantic_analysis();                                     // sub_4E8A60 (standard path)
    goto wrapup;

compile_module:
    compile_primary_source();                                // sub_586240
    module_compilation();                                    // sub_6FDDF0 (module path)

wrapup:
    // ==========================================================
    //  PHASE 4: Per-TU wrapup + stack pop
    // ==========================================================
    translation_unit_wrapup();                               // sub_588E90

    // --- Pop TU stack (inlined pop_translation_unit_stack) ---
    top = tu_stack_top;
    popped_tu = top->tu_ptr;
    if (popped_tu != current_translation_unit)
        assertion_failure("trans_unit.c", 556,
                          "pop_translation_unit_stack", 0, 0);
    if (popped_tu != primary_translation_unit)
        --tu_stack_depth;
    tu_stack_top = top->next;
    // (return stack entry to free list)
    if (tu_stack_top)
        switch_translation_unit(tu_stack_top->tu_ptr);       // sub_7A3D60

    // --- Debug trace on exit ---
    if (debug_verbosity > 0 || (debug_enabled && trace_category("trans_unit")))
        fprintf(stderr, "Done processing translation unit %s\n", filename);
}
```

## Execution Flow

```text
process_translation_unit (sub_7A40A0)
  |
  |-- [1] Debug trace: "Processing translation unit %s"
  |-- [2] Module-state validation (assert at trans_unit.c:696)
  |-- [3] Save previous TU state (sub_7A3A50)
  |-- [4] Reset error state (sub_5EAEC0)
  |-- [5] If recompilation: re-run fe_init_part_1 (sub_585EE0)
  |
  |-- [6] Allocate 424-byte TU descriptor (sub_6BA0D0)
  |       |-- Allocate per-TU storage buffer (sub_6BA0D0(per_tu_storage_size))
  |       |-- Initialize scope state at [24..192] (sub_7046E0)
  |       |-- Zero remaining fields [192..416]
  |       |-- Copy registered variable defaults
  |       |-- Set module_info_ptr [376] and flags [392]
  |
  |-- [7] Set as primary TU if first (assert at trans_unit.c:725)
  |-- [8] Push onto TU stack, link into TU chain
  |
  |-- [9] Module path? (module_info != NULL)
  |       |-- YES: Extract module header info
  |       |        sub_5ADC60 (intern_directory_path)
  |       |        sub_5AD120 (set_include_paths)
  |       |        sub_5863A0 (fe_translation_unit_init)
  |       |        sub_5AF7F0 (set_module_id)
  |       |
  |       |-- NO:  sub_5863A0 (fe_translation_unit_init) with NULL args
  |                sub_5861C0 + sub_6F4AD0 (PCH processing, if enabled)
  |
  |-- [10] sub_586240  -- compile_primary_source (parser entry)
  |
  |-- [11] Post-parse semantic analysis:
  |        |-- Module path: sub_6FDDF0 (module_compilation)
  |        |-- Standard path: sub_4E8A60 (translation_unit / semantic analysis)
  |
  |-- [12] sub_588E90 -- translation_unit_wrapup
  |
  |-- [13] Pop TU stack (assert at trans_unit.c:556)
  |-- [14] Debug trace: "Done processing translation unit %s"
```

## Phase 1: Error State Reset -- `sub_5EAEC0`

Before any parsing begins, `sub_5EAEC0` resets the parser's error recovery state. This is a tiny function (22 bytes) that configures the error-recovery token scan depth based on whether this is a recompilation pass:

```c
void reset_error_state(void) {
    if (is_recompilation) {            // dword_106BA08
        error_scan_depth = 8;          // dword_126F68C -- shallower scan on retry
        error_scan_mode = 0;           // dword_126F688
        error_recovery_kind = 16;
    } else {
        error_recovery_kind = 24;      // full recovery on first pass
    }
    error_token_limit = error_recovery_kind;  // dword_126F694
    error_count_local = 0;                     // dword_126F690
}
```

The different `error_recovery_kind` values (16 vs 24) control how aggressively the parser attempts to resynchronize after encountering a syntax error. On recompilation (error-retry), the compiler uses a smaller recovery window to avoid cascading errors.

## Phase 2: TU Descriptor Allocation

The 424-byte TU descriptor is the central data structure tracking a single translation unit's state during compilation. It is allocated from EDG's permanent storage pool via `sub_6BA0D0` and linked into two separate data structures: the TU linked list and the TU stack.

### Translation Unit Descriptor Layout (424 bytes)

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 8 | `next_tu` | Singly-linked list pointer: chains all TUs in processing order. `qword_106B9F0` (primary TU) is the head; `qword_12C7A90` is the tail. |
| 8 | 8 | `tu_name` | Initially NULL. Set later by the parser to the TU's internal identifier. |
| 16 | 8 | `storage_buffer` | Pointer to a dynamically-sized buffer holding per-TU copies of all registered global variables. Size = `qword_12C7A98` (accumulated during `f_register_trans_unit_variable` calls). |
| 24-192 | 168 | `scope_state` | Initialized by `sub_7046E0`. Contains the TU's scope stack snapshot: file scope descriptor, scope nesting state, using-directive lists. Saved/restored during TU switching by `sub_7A3A50`/`sub_7A3D60`. |
| 184 | 8 | `source_file_entry` | Set to `*(qword_126DDF0 + 64)` after the source file is opened -- the file descriptor from the source file manager. |
| 192 | 8 | (cleared) | Zero-initialized. |
| 200-352 | ~160 | `scope_decl_area` | Bulk-zeroed via `memset`. Holds scope-level declaration state that accumulates during parsing. The zero-init ensures clean state for a new TU. |
| 352 | 8 | (cleared) | Zero-initialized. |
| 360-376 | 24 | `additional_state` | Three qwords, all zeroed. Purpose unclear; possibly reserved for future EDG versions. |
| 376 | 8 | `module_info_ptr` | Pointer to the C++20 module descriptor (`a3` parameter). NULL for standard compilation. When set, the TU participates in modular compilation. |
| 392 | 2 | `flags` | Byte 0: `is_primary` (1 if this is the first TU, 0 otherwise). Byte 1: initialized marker (always 1 = `0x100` in the word). |
| 408 | 4 | `error_severity_count` | Snapshot of `dword_126EC90` at TU creation time. Compared during wrapup to detect new errors introduced during this TU's compilation. |
| 416 | 8 | (cleared) | Zero-initialized. |

### Registered Variable Mechanism

EDG's multi-TU infrastructure requires certain global variables to be saved and restored when switching between translation units (e.g., during relocatable device code compilation). The mechanism works as follows:

1. **Registration phase** (during initialization, before any TU processing): Subsystem initializers call `f_register_trans_unit_variable` (`sub_7A3C00`) to register global variables that need per-TU state. Each registration creates a 40-byte entry:

| Offset | Size | Field |
|--------|------|-------|
| 0 | 8 | `next` -- linked list pointer |
| 8 | 8 | `variable_address` -- pointer to the global variable |
| 16 | 8 | `variable_name` -- debug name string (e.g., `"is_recompilation"`) |
| 24 | 8 | `prior_accumulated_size` -- offset into per-TU storage buffer |
| 32 | 8 | `field_offset_in_tu` -- if nonzero, the offset within the TU descriptor where the default value lives |

2. **Accumulated size tracking**: Each registration pads the variable's size to 8-byte alignment and adds it to `qword_12C7A98` (per-TU storage size). The linked list head is `qword_12C7AA8`, tail is `qword_12C7AA0`.

3. **TU creation**: When a TU descriptor is allocated, a storage buffer of `per_tu_storage_size` bytes is allocated alongside it at offset [16]. Default values from the `field_offset_in_tu` entries are copied into the TU descriptor's own fields.

4. **TU switching**: `save_translation_unit_state` (`sub_7A3A50`) iterates the registered variable list, copying each variable's current value from its global address into the outgoing TU's storage buffer. `switch_translation_unit` (`sub_7A3D60`) does the reverse: copies from the incoming TU's storage buffer back to the global addresses.

Three core variables are always registered (by `sub_7A4690`):

| Variable | Address | Size | Name |
|----------|---------|------|------|
| `is_recompilation` | `dword_106BA08` | 4 | `"is_recompilation"` |
| `current_filename` | `qword_106BA00` | 8 | `"current_filename"` |
| `has_module_info` | `dword_106B9F8` | 4 | `"has_module_info"` |

Additional variables are registered by other subsystem initializers (trans_corresp registers 3 more via `sub_7A3920`).

## Phase 3: TU Linking and Stack Management

### TU Linked List

Translation units are linked in processing order through the `next_tu` field at offset [0]:

```text
qword_106B9F0 (primary_translation_unit)
  |
  v
  TU_0 --[next_tu]--> TU_1 --[next_tu]--> TU_2 --[next_tu]--> NULL
                                                       ^
                                                       |
                                          qword_12C7A90 (tu_chain_tail)
```

`qword_106B9F0` always points to the first (primary) TU. `qword_12C7A90` always points to the last. The chain is walked by `fe_wrapup` (`sub_588F90`) during its 5-pass finalization.

### TU Stack

The TU stack tracks the active compilation context. Each stack entry is a 16-byte structure:

| Offset | Size | Field |
|--------|------|-------|
| 0 | 8 | `next` -- points to the entry below on the stack |
| 8 | 8 | `tu_ptr` -- pointer to the TU descriptor |

Stack entries are allocated from a free list (`qword_12C7AB8`); when the free list is empty, a new 16-byte block is allocated via `sub_6B7340` (permanent allocator).

```text
qword_106BA18 (tu_stack_top)
  |
  v
  entry_N: [next=entry_N-1, tu_ptr=current_tu]
  entry_N-1: [next=entry_N-2, tu_ptr=prev_tu]
  ...
  entry_0: [next=NULL, tu_ptr=primary_tu]
```

The stack depth counter `dword_106B9E8` tracks how many non-primary TUs are stacked. It is incremented on push (if `tu != primary_tu`) and decremented on pop.

The pop operation at the end of `process_translation_unit` includes an assertion (at `trans_unit.c:556`) verifying that the top-of-stack TU matches `current_translation_unit`. This guards against mismatched push/pop sequences, which would corrupt the multi-TU state:

```c
if (stack_top->tu_ptr != current_translation_unit)
    assertion_failure("trans_unit.c", 556, "pop_translation_unit_stack", 0, 0);
```

## Phase 4: Source File Setup

The source file setup differs between standard compilation and C++20 module compilation.

### Standard Path (module_info == NULL)

1. **`sub_5863A0`** (`fe_translation_unit_init` / `keyword_init`, 1113 lines, `fe_init.c`): The largest initialization function in the binary. Performs two tasks in sequence:

   - **Token state reset**: Zeros `qword_126DD38` (6-byte source position) and `qword_126EDE8` (mirror).
   - **Per-TU subsystem reinit**: Calls 15+ subsystem re-initializers to prepare for a new compilation unit (source file manager, scope system, preprocessor, diagnostics, etc.).
   - **Keyword registration**: Registers 200+ C/C++ keywords via `sub_7463B0` (`enter_keyword`), including all C89/C99/C11/C23 keywords, C++ keywords through C++26, GNU extensions, MSVC extensions, Clang extensions, 60+ type traits, and three NVIDIA CUDA-specific type trait keywords (`__nv_is_extended_device_lambda_closure_type`, `__nv_is_extended_host_device_lambda_closure_type`, `__nv_is_extended_device_lambda_with_preserved_return_type`). Keyword registration is version-gated by the language mode (`dword_126EFB4`) and C++ standard version (`dword_126EF68`).
   - **File scope creation**: Calls `sub_7047C0(0)` to push the initial file scope onto the scope stack.
   - **C++ builtins**: For C++ mode, registers namespace `std`, `operator new`/`operator delete` allocation functions, `std::align_val_t`.

2. **PCH processing** (optional, if `dword_106BF18` is set): Calls `sub_5861C0` to open the source file with minimal setup (same as `sub_586240` but without the recompilation logic), followed by `sub_6F4AD0` (`precompiled_header_processing`, 721 lines, `pch.c`) which searches for an applicable `.pch` file, validates memory allocation history, and restores saved variable state from the precompiled header.

### Module Path (module_info != NULL)

When compiling a C++20 module unit, the module descriptor (passed as `a3`) provides pre-computed configuration:

```c
module_info[2] = tu;              // back-link TU into module descriptor
qword_106C0B0 = module_info[7];  // module identifier
qword_126EE98 = module_info[4];  // include path list
qword_126EE78 = module_info[6];  // system include path list
qword_126EE90 = module_info[5];  // additional path list
```

The module path then calls:
- `sub_5ADC60(filename, 1)` -- intern the source directory path (cached allocation)
- `sub_5AD120(source_dir, &include_list, &sys_list)` -- configure include search paths from the module descriptor
- `sub_5863A0(source_dir, &include_list)` -- `fe_translation_unit_init` with module-specific paths
- `sub_5AF7F0(module_info[3])` -- set the module identifier for this TU (asserts not already set)

## Phase 5: Compilation Driver -- `sub_586240`

`sub_586240` (`fe_init.c`, 63 lines) is the compilation driver that opens the source file and launches the parser. It is called for both standard and module compilation paths.

```c
void compile_primary_source(void) {
    // If recompilation: reset file-scope scope pointer
    if (is_recompilation)
        *(uint64_t *)&xmmword_126EB60 = 0;

    // Allocate mutable copy of filename for the lexer
    char *fn_copy = temp_allocate(strlen(current_filename) + 1);  // sub_5E0460
    strcpy(fn_copy, current_filename);

    // --- Open source file and push onto input stack ---
    open_file_and_push_input_stack(fn_copy, 0, 0, 0, 0, 0, 0, 0, 0, 0);  // sub_66E6E0

    // Record source file descriptor in TU
    current_tu->source_file_entry = *(source_file_descriptor + 64);  // [184]

    // --- Scope handling ---
    if (!pch_mode) {                                         // dword_106B690
        init_global_scope_flag = 1;                          // dword_126C708
        global_scope_decl_list = global_decl_chain;          // qword_126C710
        finalize_scope();                                    // sub_66E920
    }
    open_scope(1, 0);                                        // sub_6702F0

    // --- PCH recompilation metadata ---
    if (is_recompilation) {
        // Allocate 4-byte version marker (3550774 = "6.6\0")
        char *ver = temp_allocate(4);
        *(uint32_t *)ver = 3550774;                          // EDG 6.6 version tag
        edg_version_ptr = ver;                               // qword_126EB78
        // Copy compilation timestamp
        char *ts = temp_allocate(strlen(byte_106B5C0));
        compilation_timestamp_copy = strcpy(ts, byte_106B5C0); // qword_126EB80
        dialect_version_snapshot = dialect_version;           // dword_126EBF8
    }

    // --- PCH header loading ---
    if (pch_mode) {
        load_precompiled_header(byte_106B5C0);               // sub_6B5C10
        pch_header_loaded = 1;                               // dword_106B6B0
    }
}
```

### Parser Entry: `sub_66E6E0` (open_file_and_push_input_stack)

`sub_66E6E0` (`lexical.c`, 95 lines) is the gateway from file-level compilation into the EDG lexer/parser. It takes 10 parameters controlling how the source file is opened:

| Parameter | Position | Typical Value | Meaning |
|-----------|----------|---------------|---------|
| `filename` | a1 | source path | Path to the `.cu` file |
| `include_mode` | a2 | 0 | 0 = primary source, nonzero = `#include` |
| `search_type` | a3 | 0 | 0 = absolute path, nonzero = search include dirs |
| `is_system` | a4 | 0 | System header flag |
| `guard_flag` | a5 | 0 | Include guard checking mode |
| `is_pragma` | a6 | 0 | Pragma-include flag |
| `embed_mode` | a7 | 0 | `#embed` processing flag |
| `line_adjust` | a8 | 0 | Line number adjustment |
| `recovery` | a9 | 0 | Error recovery mode |
| `result_out` | a10 | 0 | Output: set to 1 if file was skipped (guard) |

The function delegates to `sub_66CBD0` which resolves the file path, opens the file handle, and creates the file descriptor. Then `sub_66DFF0` pushes the opened file onto the lexer's input stack, making it the active source for tokenization. The lexer reads from this stack via `get_next_token` (`sub_676860`, 1995 lines).

At debug verbosity > 3, it prints: `"open_file_and_push_input_stack: skipping guarded include file %s\n"` when an include guard causes the file to be skipped.

## Phase 6: Semantic Analysis -- `sub_4E8A60`

After parsing completes, `sub_4E8A60` (`translation_unit`, `decls.c`, 77 lines) performs semantic analysis on the parsed declarations. This function is called only on the standard (non-module) compilation path.

```c
void translation_unit(void) {
    // PCH mode: additional scope finalization
    if (pch_mode)
        finalize_pch_scope();                                // sub_6FC900
    if (global_decl_chain)
        process_pending_declarations();                      // sub_6FDD60

    // --- Main declaration processing loop ---
    declaration_processing_active = 1;                       // dword_126C704
    parse_declaration_seq();                                  // sub_676860 (get_next_token)
    declaration_processing_active = 0;

    // Header-unit stop detection
    if (header_unit_mode)
        finalize_header_unit();                              // sub_6F4A10

    // --- Top-level declaration loop ---
    // Repeatedly processes declarations until token 9 (EOF) is reached.
    // For C++ (dword_126EFB4 == 2) with C++14+ (dword_126EF68 > 201102):
    //   calls sub_6FBCD0 (deferred template processing)
    //   then sub_4E6F80(1, 0) (process next declaration)
    while (current_token != 9) {  // 9 = EOF token
        if (is_cpp && (cpp_version > 201102 || has_cpp20_features))
            process_deferred_templates();                    // sub_6FBCD0
        if (declaration_enabled)
            process_declaration(1, 0);                       // sub_4E6F80
    }

    // --- Post-parse validation ---
    if (!header_unit_mode) {
        if (is_cpp && (cpp_version > 201102 || has_cpp20_features))
            process_deferred_templates();                    // sub_6FBCD0 final pass
        finalize_module_interface();                         // sub_6F81D0
    } else {
        // Header-unit mode assertion: stop position must be found
        assertion_failure("decls.c", 23975, "translation_unit",
                          "translation_unit:", "header stop position not found");
    }
}
```

The C++ standard version checks (`dword_126EF68 > 201102`) gate C++14+ features like deferred template instantiation. The value `201102` corresponds to C++11 (`__cplusplus` value). For C++14 and later, `sub_6FBCD0` handles deferred template processing between declaration groups.

## Phase 7: Translation Unit Wrapup -- `sub_588E90`

`sub_588E90` (`translation_unit_wrapup`, `fe_wrapup.c`, 36 lines) performs per-TU finalization after parsing and semantic analysis are complete. It is the last step before the TU stack is popped.

```c
void translation_unit_wrapup(void) {
    if (debug_enabled)
        trace_enter(1, "translation_unit_wrapup");

    // [1] Stop-token verification
    check_all_stop_token_entries_are_reset(                  // sub_675DA0
        file_scope_stop_tokens + 8);                         // qword_126DB48 + 8

    // [2] Class linkage checking (conditional)
    if (!preprocessing_only) {
        if (rdc_enabled || rdc_alt_enabled)                  // dword_106C2BC, dword_106C2B8
            check_class_linkage();                           // sub_446F80
    }

    // [3] Module import finalization
    finalize_module_imports();                                // sub_7C24D0

    // [4] IL output
    complete_scope();                                        // sub_709250

    // [5] Close file scope
    close_file_scope(1);                                     // sub_7047C0

    // [6] Module correspondence finalization (non-preprocessing)
    if (!preprocessing_only)
        process_verification_list();                         // sub_7A2FE0

    // [7] Write compilation unit boundary
    make_module_id(0);                                       // sub_5AF830

    // [8] Namespace cleanup (C++ only, non-PCH, non-preprocessing)
    if (is_cpp && !is_recompilation && !preprocessing_only)
        namespace_cleanup();                                 // sub_76C910

    if (debug_enabled)
        trace_leave();                                       // sub_48AFD0
}
```

### Sub_675DA0: check_all_stop_token_entries_are_reset

Iterates all 357 entries in the stop-token array. If any nonzero entry is found, logs `"stop_tokens[\"%s\"] != 0\n"` (using `off_E6D240` as the token name table) and asserts with `"stop token array not all zero"` at `lexical.c:17680`. This catches lexer state corruption where a stop-token (used during error recovery and tentative parsing) was not properly cleared.

### Sub_446F80: check_class_linkage

Called only when relocatable device code (RDC) compilation is enabled (`dword_106C2BC` or `dword_106C2B8`). Iterates file-scope type entities looking for class/struct/union types (kind 9-11) and scoped enums (kind 2, bit 3 of +145) that need external linkage for cross-TU visibility. For qualifying types, calls `sub_41F800` (`make_class_externally_linked`) to set the linkage bits at offset +80 to `0x20` (external linkage flag). The function performs a two-pass scan:

1. **Pass 1**: Identify types needing external linkage. Checks whether the type is used by externally-visible definitions, has nested types with external linkage requirements, or has member functions with non-inline definitions.

2. **Pass 2**: If any types were promoted, propagates linkage to member functions and nested class template instantiations via `sub_41FD90`.

### Sub_7A2FE0: process_verification_list (Module Finalization)

`sub_7A2FE0` (`trans_corresp.c`, 69 lines) processes the deferred correspondence verification list for multi-TU compilation. This is the mechanism EDG uses to verify that declarations shared across translation units are structurally compatible (One Definition Rule checking for RDC).

```c
void process_verification_list(void) {
    if (is_recompilation || error_count != saved_error_count)
        goto skip;  // skip if new errors appeared

    correspondence_active = 1;                               // dword_106B9E4
    source_seq = *(current_tu + 8);                          // TU source sequence

    prepare_correspondence(source_seq);                      // sub_79FE00
    verify_correspondence(source_seq);                       // sub_7A2CC0

    // Process pending verification items
    while (pending_list) {                                   // qword_12C7790
        pending_list_snapshot = pending_list;
        pending_list = NULL;
        for (item = pending_list_snapshot; item; item = next) {
            next = item->next;
            switch (item->kind) {                            // byte at [8]
                case 0:  break;                              // no-op
                case 2:  verify_typedef_correspondence(item->data);          // sub_7986A0
                case 6:  verify_friend_correspondence(item->data);           // sub_7A1830
                case 7:  verify_nested_class_correspondence(item->data);     // sub_798960
                case 8:  verify_enum_member_correspondence(item->data);      // sub_798770
                case 11: verify_member_function_correspondence(item->data);  // sub_7A1DB0
                case 28: verify_using_declaration_correspondence(item->data);// sub_7982C0
                case 58: verify_base_class_correspondence(item->data);       // sub_7A27B0
                default: assertion_failure("trans_corresp.c", 7709, ...);
            }
            // Return item to free list
            item->next = corresp_free_list;
        }
    }

    correspondence_active = 0;
    correspondence_complete = 1;                             // dword_106B9E0

skip:
    correspondence_complete = 1;
}
```

The kind codes (0, 2, 6, 7, 8, 11, 28, 58) correspond to EDG declaration kinds: typedef (2), friend (6), nested class (7), enum member (8), member function (11), using declaration (28), base class (58).

## Module vs Standard Compilation Path

The control flow diverges based on `dword_106C29C` (preprocessing-only mode) and the presence of `module_info`:

```text
                          module_info?
                         /            \
                       YES             NO
                        |               |
                  sub_5ADC60         sub_5863A0(0,0)
                  sub_5AD120              |
                  sub_5863A0       PCH enabled?
                  sub_5AF7F0        /        \
                        |         YES         NO
                        |          |           |
                        |     sub_5861C0       |
                        |     sub_6F4AD0       |
                        |          |           |
                        +-----+----+-----+-----+
                              |          |
                         sub_586240  sub_586240
                              |          |
                      preprocessing_only?
                        /            \
                      YES             NO
                       |               |
                  sub_6FDDF0      sub_4E8A60
                  (module comp)   (standard comp)
                       |               |
                       +-------+-------+
                               |
                         sub_588E90
                    (translation_unit_wrapup)
```

Note: `sub_6FDDF0` is the module compilation driver (59 lines, `lower_il.c`). It enters a loop calling `sub_676860` (`get_next_token`) until EOF (token 9), processing module import/export declarations. Between module units, it calls `sub_66EA70` to close the current input source and advance to the next module partition.

## Global State Variables

### Translation Unit Tracking

| Variable | Address | Type | Description |
|----------|---------|------|-------------|
| `current_translation_unit` | `qword_106BA10` | `tu_descriptor*` | Points to the TU currently being compiled. Set during TU creation and switching. |
| `primary_translation_unit` | `qword_106B9F0` | `tu_descriptor*` | Points to the first TU. Set exactly once. Never changes after that. |
| `tu_chain_tail` | `qword_12C7A90` | `tu_descriptor*` | Tail of the TU linked list. Used for O(1) append of new TUs. |
| `tu_stack_top` | `qword_106BA18` | `stack_entry*` | Top of the TU stack. Each entry is a 16-byte `{next, tu_ptr}` node. |
| `tu_stack_depth` | `dword_106B9E8` | `int` | Number of non-primary TUs on the stack. Incremented on push, decremented on pop. |
| `current_filename` | `qword_106BA00` | `char*` | Path of the `.cu` file being compiled. Per-TU variable (saved/restored on switch). |
| `is_recompilation` | `dword_106BA08` | `int` | Nonzero during error-retry recompilation pass. Per-TU variable. |
| `has_module_info` | `dword_106B9F8` | `int` | 1 if the current TU is a C++20 module unit. Per-TU variable. |

### Registration Infrastructure

| Variable | Address | Type | Description |
|----------|---------|------|-------------|
| `registered_variable_list_head` | `qword_12C7AA8` | `reg_entry*` | Head of the registered variable linked list. Built during initialization. |
| `registered_variable_list_tail` | `qword_12C7AA0` | `reg_entry*` | Tail of the registered variable list. Used for O(1) append. |
| `per_tu_storage_size` | `qword_12C7A98` | `size_t` | Accumulated size of all registered variables (8-byte aligned). Determines the storage buffer size at TU descriptor offset [16]. |
| `registration_complete` | `dword_12C7A8C` | `int` | Set to 1 at the start of `process_translation_unit`. After this, no more variables can be registered. |
| `has_seen_module_tu` | `dword_12C7A88` | `int` | Set to 1 when a module-info TU is processed. Guards against mixing module and non-module TUs. |
| `stack_entry_free_list` | `qword_12C7AB8` | `stack_entry*` | Free list for recycling 16-byte TU stack entries. |

### Statistics Counters

| Variable | Address | Description |
|----------|---------|-------------|
| `qword_12C7A78` | `tu_count` | Total TU descriptors allocated (424 bytes each) |
| `qword_12C7A80` | `stack_entry_count` | Total stack entries allocated (16 bytes each) |
| `qword_12C7A68` | `registration_count` | Total variable registration entries (40 bytes each) |
| `qword_12C7A70` | `corresp_count` | Total correspondence entries (24 bytes each) |

These counters are reported by `sub_7A45A0` (`print_trans_unit_statistics`), which prints formatted memory usage:

```text
trans. unit corresps          N x 24 bytes
translation units             N x 424 bytes
trans. unit stack entry       N x 16 bytes
variable registration         N x 40 bytes
```

## Assertions

The function contains three assertion checks, each producing a fatal diagnostic via `sub_4F2930`:

| Line | Condition | Message | Meaning |
|------|-----------|---------|---------|
| `trans_unit.c:696` | Primary TU (no module_info) but `has_seen_module_tu` is set | *(none)* | Cannot process a non-module TU after a module TU has been seen |
| `trans_unit.c:725` | `primary_translation_unit` is set but `is_recompilation` is false | *(none)* | First TU must be on the initial compilation pass, not a retry |
| `trans_unit.c:556` | Stack top's TU pointer does not match `current_translation_unit` | *(none)* | TU stack push/pop mismatch -- corrupted compilation state |

## Callee Reference Table

| Address | Identity | Source | Role in Pipeline |
|---------|----------|--------|------------------|
| `sub_48A7E0` | `trace_category` | `error.c` | Check if debug category `"trans_unit"` is enabled |
| `sub_5EAEC0` | `reset_error_state` | `parse.c` | Reset parser error recovery state |
| `sub_585EE0` | `fe_init_part_1` | `fe_init.c` | Re-run per-unit init on recompilation |
| `sub_6BA0D0` | `allocate_storage` | `il_alloc.c` | Permanent storage allocator (424-byte TU, per-TU buffer) |
| `sub_7046E0` | `init_scope_state` | `scope_stk.c` | Initialize scope fields at TU descriptor [24..192] |
| `sub_6B7340` | `permanent_alloc` | `il_alloc.c` | Allocate 16-byte TU stack entry |
| `sub_7A3A50` | `save_translation_unit_state` | `trans_unit.c` | Save current TU's registered variables and scope state |
| `sub_7A3D60` | `switch_translation_unit` | `trans_unit.c` | Restore a TU's state (inverse of save) |
| `sub_5ADC60` | `intern_directory_path` | `host_envir.c` | Cache directory path string (module path) |
| `sub_5AD120` | `set_include_paths` | `host_envir.c` | Configure include search paths from module descriptor |
| `sub_5863A0` | `fe_translation_unit_init` | `fe_init.c` | Per-TU init + keyword registration (1113 lines) |
| `sub_5AF7F0` | `set_module_id` | `host_envir.c` | Set module identifier for current TU |
| `sub_5861C0` | `setup_pch_source` | `fe_init.c` | Open source file for PCH mode |
| `sub_6F4AD0` | `precompiled_header_processing` | `pch.c` | Find/load applicable PCH file (721 lines) |
| `sub_586240` | `compile_primary_source` | `fe_init.c` | Open source, launch parser, build IL |
| `sub_66E6E0` | `open_file_and_push_input_stack` | `lexical.c` | Open source file, push onto lexer input stack (10 params) |
| `sub_676860` | `get_next_token` | `lexical.c` | Main tokenizer (1995 lines) |
| `sub_6702F0` | `open_scope` | `scope_stk.c` | Push a new scope onto the scope stack |
| `sub_6FDDF0` | `module_compilation` | `lower_il.c` | Module compilation driver (EOF-driven loop) |
| `sub_4E8A60` | `translation_unit` | `decls.c` | Standard compilation: semantic analysis + declaration loop |
| `sub_588E90` | `translation_unit_wrapup` | `fe_wrapup.c` | Per-TU finalization (8 sub-steps) |
| `sub_675DA0` | `check_all_stop_token_entries_are_reset` | `lexical.c` | Verify all 357 stop-tokens are cleared |
| `sub_446F80` | `check_class_linkage` | `class_decl.c` | RDC: promote class types to external linkage |
| `sub_7C24D0` | `finalize_module_imports` | `modules.c` | C++20 module import finalization |
| `sub_709250` | `complete_scope` | `il.c` | IL scope completion |
| `sub_7047C0` | `close_file_scope` | `scope_stk.c` | Pop file scope, activate using-directives |
| `sub_7A2FE0` | `process_verification_list` | `trans_corresp.c` | ODR verification for multi-TU (RDC) |
| `sub_76C910` | `namespace_cleanup` | `cp_gen_be.c` | C++ namespace state cleanup |
| `sub_4F2930` | `assertion_failure` | `error.c` | Fatal assertion handler (prints source path + line) |

## Cross-References

- [Pipeline Overview](./overview.md) -- complete 8-stage pipeline diagram showing where `process_translation_unit` fits
- [Entry Point & Initialization](./entry.md) -- stages 1-3 that execute before this function
- [Frontend Wrapup](./fe-wrapup.md) -- the 5-pass `fe_wrapup` (stage 6) that runs after this function
- [Backend Code Generation](./backend.md) -- stage 7 that consumes the IL tree built here
- [CLI Processing](./cli.md) -- all 276 flags that configure the compilation mode
- [Timing & Exit](./timing-exit.md) -- exit code mapping and timing infrastructure
- [EDG Overview](../edg/overview.md) -- EDG 6.6 source tree and NVIDIA modifications
- [Execution Spaces](../cuda/execution-spaces.md) -- how `__device__`/`__host__`/`__global__` attributes are recorded during parsing
- [Device/Host Separation](../cuda/device-host-separation.md) -- how the backend filters device vs host code from the IL tree
