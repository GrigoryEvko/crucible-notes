# Frontend Invocation

`process_translation_unit` (`sub_7A40A0`, 1267 bytes, 27 callees) is the main frontend workhorse of cudafe++. Called from `main()` as pipeline stage 5, it allocates a 424-byte translation unit descriptor, initializes 200+ keyword registrations, invokes the EDG parser, compiles all file-scope declarations, and runs semantic wrapup. The function lives in `trans_unit.c` (EDG 6.6 source) and manages all per-TU state through a linked list of descriptors, a save/restore stack, and a registered-variable mechanism that copies global state into per-TU storage on context switches.

## Key Facts

| Property | Value |
|---|---|
| Address | `0x7A40A0` |
| Size | 1267 bytes |
| Source file | `trans_unit.c` |
| Reconstructed name | `process_translation_unit` |
| Signature | `int process_translation_unit(char *filename, int is_recompilation, module_info_t *module_info)` |
| Direct callees | 27 |
| TU descriptor size | 424 bytes |
| Per-TU storage buffer | variable size, accumulated via `f_register_trans_unit_variable` (default ~16 bytes for 3 builtin vars) |
| Debug trace | `"Processing translation unit %s"` / `"Done processing translation unit %s"` |
| Assertion source | `trans_unit.c` lines 556, 696, 725 |

## Parameters

| Parameter | Register | Type | Meaning |
|---|---|---|---|
| `s` (filename) | `rdi` | `char *` | Source file path to compile (e.g., `"/tmp/tmpxft_xxx_input.cudafe1.cpp"`) |
| `a2` (is_recompilation) | `esi` | `int` | 1 if recompiling a TU (second pass), 0 for first pass |
| `a3` (module_info) | `rdx` | `module_info_t *` | C++20 module descriptor, NULL for normal compilation |

## Annotated Decompilation

```c
int process_translation_unit(char *filename, int is_recompilation,
                             module_info_t *module_info)
{
    bool no_module = (module_info == NULL);

    // ── Debug trace ──
    if (trace_level > 0 || (trace_enabled && trace_matches("trans_unit")))
        fprintf(stderr, "Processing translation unit %s\n", filename);

    // ── Module-TU state validation ──
    if (is_recompilation) {
        if (module_info)
            has_seen_module_tu = 1;            // dword_12C7A88
    } else if (!no_module) {
        if (module_info)
            has_seen_module_tu = 1;
    } else {
        // a3==NULL && a2==0: assert we haven't seen module TUs before
        if (has_seen_module_tu)
            ASSERTION("trans_unit.c", 696, "process_translation_unit");
    }

    // ── Save previous TU state ──
    if (current_tu)                             // qword_106BA10
        save_translation_unit_state(current_tu);// sub_7A3A50

    // ── Reset parser error state ──
    error_severity = 0;                         // LODWORD(qword_126DD38)
    error_column = 0;                           // WORD2(qword_126DD38)
    is_recompilation_flag = is_recompilation;    // dword_106BA08
    current_filename = filename;                // qword_106BA00
    saved_error_state = error_severity;         // qword_126EDE8
    has_module_info = (module_info != NULL);     // dword_106B9F8

    // ── Initialize parser state ──
    reset_error_state();                        // sub_5EAEC0
    if (is_recompilation)
        fe_init_part_1();                       // sub_585EE0

    // ── Lock variable registration ──
    registration_complete = 1;                  // dword_12C7A8C

    // ══════════════════════════════════════════
    //  TU DESCRIPTOR ALLOCATION (424 bytes)
    // ══════════════════════════════════════════
    tu_desc_t *tu = alloc_storage(424);         // sub_6BA0D0(424)
    ++tu_count;                                 // qword_12C7A78

    // Per-TU storage buffer (holds saved registered variables)
    tu->storage_buffer = alloc_storage(per_tu_storage_size);
                                                // sub_6BA0D0(qword_12C7A98)

    tu->next = NULL;                            // [0] = 0
    tu->scope_info = NULL;                      // [8] = 0

    // Initialize scope state (20 qwords zeroed at tu+24)
    init_scope_state(tu + 24);                  // sub_7046E0

    // Zero member region [184..360]
    tu->source_file_info = NULL;                // [184] = 0
    tu->some_ptr_192 = NULL;                    // [192] = 0
    tu->field_352 = NULL;                       // [352] = 0
    memset(aligned(tu+200), 0, ...);            // bulk zero [200..360]

    // Zero trailing state
    tu->field_360 = 0;                          // [360] = 0
    tu->field_368 = 0;                          // [368] = 0
    tu->module_info = NULL;                     // [376] = 0
    tu->flags = 0x100;                          // [392] = 256 (initialized)
    tu->error_count = 0;                        // [408] = 0
    tu->field_416 = 0;                          // [416] = 0

    // ── Copy registered variable defaults ──
    for (reg = registered_variable_list; reg; reg = reg->next) {
        if (reg->tu_offset)
            *(qword*)(tu + reg->tu_offset) = reg->value_ptr;
    }

    // ── Set module info and primary-TU flag ──
    tu->module_info = module_info;              // [376]
    tu->flags[0] = no_module;                   // [392] byte 0 = is_primary

    // ── Link as primary TU if first ──
    if (!primary_tu) {                          // qword_106B9F0
        primary_tu = tu;
        if (!is_recompilation)
            ASSERTION("trans_unit.c", 725, "process_translation_unit");
    }

    // ── Set as current TU ──
    current_tu = tu;                            // qword_106BA10

    // ── Push onto TU stack ──
    // (inline push_translation_unit_stack code)
    stack_entry = pop_free_list_or_alloc(16);   // from qword_12C7AB8 or sub_6B7340
    stack_entry->next = tu_stack_top;
    stack_entry->tu_ptr = tu;
    if (current_tu != tu)
        switch_translation_unit(tu);            // sub_7A3D60
    if (tu != primary_tu)
        ++tu_stack_depth;                       // dword_106B9E8
    tu_stack_top = stack_entry;                  // qword_106BA18

    // ── Link into TU chain ──
    if (tu_chain_tail)                          // qword_12C7A90
        tu_chain_tail->next = tu;
    tu_chain_tail = tu;

    // ══════════════════════════════════════════
    //  COMPILATION SEQUENCE
    // ══════════════════════════════════════════
    if (module_info) {
        // ── Module compilation path ──
        source_info = open_source_file(filename, 1);   // sub_5ADC60
        set_include_paths(source_info, &include_state); // sub_5AD120
        keyword_init(source_info, &include_state);      // sub_5863A0
        import_declarations(module_info->decl_list);    // sub_5AF7F0
        tu->error_count = global_error_count;           // dword_126EC90
        if (is_module_compilation)                      // dword_106C29C
            goto parse_and_compile_module;
    } else {
        // ── Normal compilation path ──
        keyword_init(...);                              // sub_5863A0
        tu->error_count = global_error_count;
    }

    // ── Optional preprocessing-only path ──
    if (preprocessing_mode && !suppress_preprocessing) {
        preprocessing_open_file();                      // sub_5861C0
        preprocessing_header_setup();                   // sub_6F4AD0
    }

    // ── Main parse ──
    main_parse_driver();                                // sub_586240

    // ── Compile declarations ──
    compile_declarations();                             // sub_4E8A60
    goto wrapup;

parse_and_compile_module:
    main_parse_driver();                                // sub_586240
    module_finalize();                                  // sub_6FDDF0

wrapup:
    // ══════════════════════════════════════════
    //  TRANSLATION UNIT WRAPUP
    // ══════════════════════════════════════════
    translation_unit_wrapup();                          // sub_588E90

    // ── Pop TU stack (inline) ──
    entry = tu_stack_top;
    if (entry->tu_ptr != current_tu)
        ASSERTION("trans_unit.c", 556, "pop_translation_unit_stack");
    if (entry->tu_ptr != primary_tu)
        --tu_stack_depth;
    tu_stack_top = entry->next;
    entry->next = free_list;                    // return to free list
    free_list = entry;
    if (tu_stack_top)
        switch_translation_unit(tu_stack_top->tu_ptr);  // sub_7A3D60

    // ── Debug trace ──
    if (trace_level > 0 || (trace_enabled && trace_matches("trans_unit")))
        fprintf(stderr, "Done processing translation unit %s\n", filename);
}
```

## Translation Unit Descriptor

Each TU is represented by a 424-byte descriptor allocated via `sub_6BA0D0` (the EDG general-purpose storage allocator). The allocator rounds sizes to 8-byte alignment and draws from a slab allocator backed by large arenas. The descriptor structure is reconstructed as follows:

### Descriptor Layout

| Offset | Size | Type | Identity | Source |
|---|---|---|---|---|
| 0 | 8 | `tu_desc_t *` | `next` -- next TU in chain | decompiled: `*v7 = 0` |
| 8 | 8 | `scope_entry_t *` | `scope_info` -- file scope descriptor | `sub_5B89F0` sets this |
| 16 | 8 | `void *` | `storage_buffer` -- per-TU variable storage | `sub_6BA0D0(per_tu_storage_size)` |
| 24 | 160 | struct | scope state block (20 qwords, zeroed by `sub_7046E0`) | `init_scope_state` |
| 184 | 8 | `void *` | source file info pointer | zeroed, then set to `*(qword_126DDF0 + 64)` |
| 192 | 8 | `void *` | reserved / internal ptr | zeroed |
| 200 | 152 | bytes | bulk-zeroed member region | `memset(aligned, 0, ...)` |
| 352 | 8 | `void *` | reserved | zeroed |
| 360 | 8 | `void *` | additional state 1 | zeroed |
| 368 | 8 | `void *` | additional state 2 | zeroed |
| 376 | 8 | `module_info_t *` | module info pointer (NULL for non-module TUs) | `a3` parameter stored here |
| 384 | 8 | padding | alignment | -- |
| 392 | 2 | `uint16_t` | flags (initialized = `0x100`, byte[0] = is_primary) | `*(_WORD *)(tu + 392) = 256` |
| 394 | 14 | bytes | padding/reserved | -- |
| 408 | 4 | `int32_t` | error count at TU start | `dword_126EC90` copied here |
| 412 | 4 | padding | -- | -- |
| 416 | 8 | `int64_t` | additional state | zeroed |

### Scope State Block (offsets 24-183)

`sub_7046E0` initializes the scope state region at `tu + 24` by zeroing 20 qwords (160 bytes) across offsets 0-152 relative to that base. This block stores the file-scope symbol table index, scope chain pointers, and namespace tracking. The exact sub-layout:

| Relative Offset | Absolute (from tu) | Identity |
|---|---|---|
| 0-120 | 24-144 | 16 qwords zeroed (scope chain, symbol tables) |
| 120 | 144 | qword zeroed |
| 128 | 152 | qword zeroed |
| 136 | 160 | qword zeroed |
| 144 | 168 | qword zeroed |
| 152 bit 0 | 176 | bit cleared (`&= ~1u`) -- scope initialization flag |

### Registered Variable System

The per-TU storage buffer holds copies of global variables that must be saved and restored when switching between TUs (critical for RDC multi-TU compilation). Registration happens before any TU processing begins:

`f_register_trans_unit_variable` (`sub_7A3C00`) allocates a 40-byte registration record:

| Offset | Size | Type | Field |
|---|---|---|---|
| 0 | 8 | `void *` | next pointer (linked list) |
| 8 | 8 | `void *` | variable address |
| 16 | 8 | `size_t` | variable size (padded to 8-byte alignment) |
| 24 | 8 | `size_t` | offset within per-TU buffer |
| 32 | 8 | `size_t` | offset within TU descriptor (for direct copy) |

Three builtin variables are registered by `sub_7A4690` (`register_builtin_trans_unit_variables`):

| # | Global | Address | Size | Identity |
|---|---|---|---|---|
| 1 | `dword_106BA08` | `0x106BA08` | 4 | `is_recompilation` |
| 2 | `qword_106BA00` | `0x106BA00` | 8 | `current_filename` |
| 3 | `dword_106B9F8` | `0x106B9F8` | 4 | `has_module_info` |

Additional subsystems register their own variables via the same mechanism, growing `per_tu_storage_size` (`qword_12C7A98`). The total buffer size is the sum of all registered variable sizes (each padded to 8 bytes).

## TU Linked List & Stack

The function maintains two distinct data structures for tracking translation units:

### TU Chain (linked list)

A singly-linked list of all TU descriptors ever created, in creation order. Used for iteration during wrapup and statistics.

| Global | Address | Type | Role |
|---|---|---|---|
| `qword_106B9F0` | `0x106B9F0` | `tu_desc_t *` | `primary_tu` -- first (primary) TU, head of chain |
| `qword_12C7A90` | `0x12C7A90` | `tu_desc_t *` | `tu_chain_tail` -- tail for O(1) append |
| `qword_106BA10` | `0x106BA10` | `tu_desc_t *` | `current_tu` -- currently active TU |

```
primary_tu ──> [TU_0] ──> [TU_1] ──> [TU_2] ──> NULL
               ^                       ^
               |                       |
            first TU             tu_chain_tail
```

### TU Stack

A LIFO stack of 16-byte entries used for nested TU processing (e.g., when parsing a module import triggers compilation of another TU). Each stack entry:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | `next` -- previous stack entry |
| 8 | 8 | `tu_ptr` -- pointer to TU descriptor |

| Global | Address | Type | Role |
|---|---|---|---|
| `qword_106BA18` | `0x106BA18` | `stack_entry_t *` | `tu_stack_top` -- top of TU stack |
| `dword_106B9E8` | `0x106B9E8` | `int` | `tu_stack_depth` -- depth excluding primary TU |
| `qword_12C7AB8` | `0x12C7AB8` | `stack_entry_t *` | `stack_entry_free_list` -- recycled entries |

The stack push/pop operations are inlined in `process_translation_unit` but also exist as standalone functions:

**Push** (`sub_7A3EF0`, `push_translation_unit_stack`): Takes a stack entry from the free list (`qword_12C7AB8`) or allocates 16 bytes via `sub_6B7340`. Stores `tu_ptr` and links to previous top. If the pushed TU differs from the current TU, calls `switch_translation_unit` (`sub_7A3D60`) to save/restore global state. Increments `tu_stack_depth` unless the pushed TU is the primary.

**Pop** (`sub_7A3F70`, `pop_translation_unit_stack`): Asserts that the top entry's `tu_ptr` matches `current_tu` (assertion at `trans_unit.c:556`). Returns the entry to the free list. If a previous entry exists on the stack, switches to its TU via `sub_7A3D60`.

**Switch** (`sub_7A3D60`, `switch_translation_unit`): The core context-switch routine. When target != current:
1. Saves current TU state via `sub_7A3A50`: copies all registered global variables into the current TU's storage buffer via `memcpy`, saves scope chain pointers (`qword_126EB70`, `qword_126EBA0`, `qword_126EBE0`), and recomputes file scope indices via `sub_704490`
2. Restores target TU state: copies all registered variables back from the target TU's storage buffer, restores scope chain pointers, rebuilds file scope index

This mechanism enables multi-TU compilation (RDC mode) where multiple translation units share a single process lifetime.

## Compilation Sequence

### Step 1: Reset Error State -- `sub_5EAEC0`

A tiny 22-byte function that resets the parser error tracking globals:

```c
void reset_error_state(void)
{
    if (is_recompilation) {
        error_severity_limit = 8;    // dword_126F68C
        error_count_limit = 0;       // dword_126F688
        initial_severity = 16;
    } else {
        initial_severity = 24;
    }
    some_error_flag = initial_severity;  // dword_126F694
    error_accumulator = 0;               // dword_126F690
}
```

In recompilation mode, the severity threshold is lowered (8 instead of 24), allowing the recompilation pass to catch errors that the first pass suppressed.

### Step 2: Keyword Initialization -- `sub_5863A0`

`keyword_init` / `fe_translation_unit_init` is a 1113-line function that performs two major tasks:

**Part A: Per-TU subsystem initialization (24 calls)**

The first 24 lines call subsystem initializers that must run for every translation unit. These zero per-TU state in various subsystems:

| # | Address | Identity | Subsystem |
|---|---|---|---|
| 1 | `sub_6BCBC0` | scope_init | Scope management |
| 2 | `sub_6BCE50` | hash_init | Hash table subsystem |
| 3 | `sub_419070` | class_decl_init | Class declaration state |
| 4 | `sub_6BAF70` | storage_init | Storage allocator per-TU |
| 5 | `sub_5B1E60` | host_envir_init | Host environment per-TU |
| 6 | `sub_4ED730` | decl_init | Declaration processing |
| 7 | `sub_5A5190` | pragma_init | Pragma handler |
| 8 | `sub_5CFE20` | error_msg_init | Error message tables |
| 9 | `sub_65DC10` | lex_init | Lexer per-TU state |
| 10 | `sub_4E8EA0` | parse_init | Parser per-TU state |
| 11 | `sub_4A1BB0` | const_eval_init | Constant evaluation |
| 12 | `sub_689EB0` | diagnostic_init | Diagnostic output |
| 13 | `sub_74BDA0` | name_lookup_init | Name lookup tables |
| 14 | `sub_710CC0` | overload_init | Overload resolution |
| 15 | `sub_76D6E0` | template_init | Template engine |
| 16 | `sub_7A3960` | trans_corresp_init | TU correspondence (RDC) |
| 17 | `sub_510C50` | semantic_init | Semantic analysis |
| 18 | `sub_56DD70` | conversion_init | Conversion tables |
| 19 | `sub_726E80` | access_check_init | Access checking |
| 20 | `sub_4477E0` | class_linkage_init | Class linkage |
| 21 | `sub_665A40` | source_mgr_init | Source file manager |
| 22 | `sub_6B6740` | mem_region_init | Memory region tracking |
| 23 | `sub_6FE050` | pch_init | PCH subsystem |
| 24 | `sub_7514B0` | attribute_init | Attribute processing |

After these, if not in recompilation mode, two flags are set:

```c
if (!is_recompilation) {
    suppress_preprocessing = 1;  // dword_106B6AC
    pch_active = 0;              // dword_106B690
}
```

**Part B: Keyword registration (200+ keywords)**

The rest of the function is a massive conditional tree that calls `sub_7463B0` (register_keyword) with (token_id, keyword_string) pairs. The registrations are gated by language mode flags, C/C++ standard version, and feature flags. The keywords fall into these categories:

| Category | Count | Examples |
|---|---|---|
| C89 base keywords | ~28 | `auto`, `break`, `case`, `char`, `continue`, `default`, `do`, `double`, `else`, `enum`, `extern`, `float`, `for`, `goto`, `if`, `int`, `long`, `register`, `return`, `short`, `sizeof`, `static`, `struct`, `switch`, `typedef`, `union`, `unsigned`, `void`, `while` |
| C99 additions | ~8 | `_Bool`, `_Complex`, `_Imaginary`, `_Generic`, `restrict`, `inline` |
| C11 additions | ~5 | `_Noreturn`, `_Atomic`, `_Alignof`, `_Alignas`, `_Static_assert`, `_Thread_local` |
| C23 additions | ~10 | `bool`, `true`, `false`, `alignof`, `alignas`, `static_assert`, `thread_local`, `typeof`, `typeof_unqual`, `nullptr` |
| C++ keywords | ~30 | `catch`, `class`, `const_cast`, `delete`, `dynamic_cast`, `explicit`, `friend`, `inline`, `mutable`, `namespace`, `new`, `operator`, `private`, `protected`, `public`, `reinterpret_cast`, `static_cast`, `template`, `this`, `throw`, `try`, `typeid`, `typename`, `using`, `virtual` |
| C++ operator alternatives | 11 | `and`, `and_eq`, `bitand`, `bitor`, `compl`, `not`, `not_eq`, `or`, `or_eq`, `xor`, `xor_eq` |
| C++11 additions | ~8 | `char16_t`, `char32_t`, `constexpr`, `decltype`, `noexcept`, `nullptr`, `static_assert` |
| C++20 additions | ~6 | `co_yield`, `co_return`, `co_await`, `requires`, `concept`, `consteval`, `constinit` |
| Type trait intrinsics | ~65 | `__is_class`, `__is_enum`, `__is_abstract`, `__has_trivial_copy`, `__is_trivially_constructible`, `__is_aggregate`, `__is_same`, `__is_convertible`, etc. |
| GCC builtins | ~15 | `__builtin_offsetof`, `__builtin_types_compatible_p`, `__builtin_addressof`, `__builtin_bit_cast`, `__builtin_complex`, `__builtin_shuffle`, `__builtin_shufflevector`, `__builtin_convertvector`, `__builtin_has_attribute` |
| MSVC extensions | ~6 | `__declspec`, `__int8`, `__int16`, `__int32`, `__int64`, `__int128` |
| NVIDIA CUDA keywords | 3 | `__nv_is_extended_device_lambda_closure_type`, `__nv_is_extended_host_device_lambda_closure_type`, `__nv_is_extended_device_lambda_with_preserved_return_type` |
| EDG internal keywords | ~10 | `__edg_type__`, `__edg_size_type__`, `__edg_ptrdiff_type__`, `__edg_bool_type__`, `__edg_wchar_type__`, `__edg_opnd__`, `__edg_throw__`, `__edg_vector_type__`, `__edg_neon_vector_type__`, `__edg_neon_polyvector_type__`, `__edg_scalable_vector_type__` |
| ARM intrinsics | 5 | `__builtin_arm_ldrex`, `__builtin_arm_ldaex`, `__builtin_arm_addg`, `__builtin_arm_irg`, `__builtin_arm_ldg` |
| Float type keywords | 5 | `_Float32`, `_Float32x`, `_Float64`, `_Float64x`, `_Float128` |

Keywords are registered via `sub_7463B0` (which hashes the string and inserts into the keyword hash table) and `sub_585B10` (which sets context-sensitive-keyword flags). Some identifiers are registered as both keywords and operators via `sub_749600` (which sets the token kind at offset +88 and the operator-keyword flag at offset +90).

**Part C: File scope setup (tail)**

After keyword registration, the function:
1. Calls `sub_6B5E50` to initialize the built-in file identifier
2. Creates the file scope entry via `sub_5B89F0(dword_126DFE8)` and stores at `tu + 8`
3. Calls `sub_7047C0(0)` to enter the file scope
4. In C++ mode: initializes `std::` namespace types via `sub_733110`, `sub_732F00`, creates operator new/delete placeholders via `sub_736860`, creates `std::align_val_t` if needed
5. Calls `sub_74EE40` for final attribute setup
6. In recompilation mode: calls `sub_689130` for diagnostic state reset

### Step 3: Main Parse Driver -- `sub_586240`

The parse driver opens the source file, invokes the recursive-descent parser, and enters the file scope:

```c
void main_parse_driver(void)
{
    if (is_recompilation)
        *(qword *)&xmmword_126EB60 = 0;   // reset scope xmm state

    // Duplicate filename into parser-managed string storage
    char *name_copy = alloc_string(strlen(current_filename) + 1);
    strcpy(name_copy, current_filename);

    // Open file and push onto input stack (10-parameter call)
    open_file_and_push_input_stack(name_copy, 0, 0, 0, 0, 0, 0, 0, 0, 0);
    // sub_66E6E0 in lexical.c -- opens file, creates source buffer,
    // initializes token stream

    // Save source file reference into TU descriptor
    current_tu->source_file_info = *(qword *)(qword_126DDF0 + 64);

    // If not in PCH mode, enter declaration parsing context
    if (!pch_active) {
        parsing_declarations = 1;         // dword_126C708
        module_declaration = qword_126EEC0;
        enter_scope();                     // sub_66E920
    }

    // Begin scope tracking for file-scope
    begin_file_scope(1, 0);                // sub_6702F0

    // Recompilation: set up compilation timestamp string
    if (is_recompilation) {
        char *ts = alloc_perm_string(4);
        *(int32_t *)ts = 0x363636;         // magic timestamp value
        qword_126EB78 = ts;
        qword_126EB80 = strcpy(alloc_perm_string(len), byte_106B5C0);
        dword_126EBF8 = dword_126E4A8;
    }

    // PCH mode: call global module init
    if (pch_active)
        init_global_module(byte_106B5C0);  // sub_6B5C10
}
```

The parser entry point `sub_66E6E0` (`open_file_and_push_input_stack` from `lexical.c`) takes 10 parameters controlling file opening mode, include search behavior, and guard detection. In this context all optional parameters are 0 (direct file open, not an `#include`).

### Step 4: Compile Declarations -- `sub_4E8A60`

`compile_declarations` (from `decls.c`, line 23975) runs the top-level declaration loop. Its internal structure:

```c
void compile_declarations(void)
{
    // Module system integration
    if (pch_active)
        module_process_pch();               // sub_6FC900
    if (module_declaration)
        module_finalize_pending();          // sub_6FDD60

    // Enter declaration compilation mode
    in_declaration_compilation = 1;         // dword_126C704

    // Parse all file-scope declarations
    parse_declarations(...);                // sub_676860

    in_declaration_compilation = 0;

    // Post-parse: handle pending instantiations
    if (has_pending_instantiations)
        process_pending_instantiations();   // sub_6F4A10

    // Main declaration loop: keep parsing until EOF (token 9)
    while (current_token != EOF_TOKEN) {    // word_126DD58 != 9
        // C version / standard gating for module/linkage features
        if (c_plus_plus || c_version > 199900 || module_mode)
            check_deferred_linkage();       // sub_6FBCD0
        parse_one_declaration(1, 0);        // sub_4E6F80
    }

    // Post-EOF: emit pending diagnostics
    if (c_plus_plus) {
        if (!header_stop_pending)
            emit_deferred_diagnostics();    // sub_6F81D0
    }
}
```

The loop calls `sub_4E6F80` (`parse_one_declaration`) repeatedly until the lexer returns EOF (token kind 9). Between declarations, `sub_6FBCD0` handles deferred linkage specification processing for C++ and C23 modules.

### Step 5: Translation Unit Wrapup -- `sub_588E90`

The per-TU wrapup function runs semantic checks and cleanup after all declarations are compiled:

```c
void translation_unit_wrapup(void)
{
    if (trace_enabled)
        trace_enter(1, "translation_unit_wrapup");

    // 1. Check stop-token array is fully reset (357 entries)
    check_all_stop_token_entries_are_reset(qword_126DB48 + 8);  // sub_675DA0
    // Iterates all 357 token entries; if any non-zero, asserts
    // with "stop token array not all zero" at lexical.c:17680

    // 2. Check class linkage (unless module compilation)
    if (!is_module_compilation) {
        if (check_overloads_enabled || check_linkage_enabled)
            check_class_linkage(...);       // sub_446F80
            // Walks file-scope type list looking for class/struct/union
            // types that need external linkage. Uses sub_4194B0 to
            // check if a type has internal linkage and promotes it
            // if it has externally-visible members.
    }

    // 3. Finalize module imports
    finalize_module_imports();              // sub_7C24D0

    // 4. Scope cleanup
    scope_cleanup(...);                     // sub_709250

    // 5. Exit file scope
    exit_file_scope(1);                     // sub_7047C0

    // 6. Reset TU correspondence (non-module mode)
    if (!is_module_compilation)
        reset_tu_correspondence();          // sub_7A2FE0

    // 7. Compute module ID (CRC32)
    make_module_id(0);                      // sub_5AF830

    // 8. C mode cleanup (non-recompilation, non-module)
    if (c_mode && !is_recompilation && !is_module_compilation)
        c_wrapup();                         // sub_76C910

    if (trace_enabled)
        trace_exit();                       // sub_48AFD0
}
```

## Compilation Flow Diagram

```
process_translation_unit (sub_7A40A0)
  |
  |── save previous TU state (sub_7A3A50)
  |── reset error state (sub_5EAEC0)
  |── [recompilation only] fe_init_part_1 (sub_585EE0)
  |
  |── ALLOCATE 424-byte TU descriptor (sub_6BA0D0)
  |   |── allocate per-TU storage buffer
  |   |── zero scope state (sub_7046E0)
  |   |── zero member region
  |   |── copy registered variable defaults
  |   └── link into TU chain
  |
  |── PUSH onto TU stack
  |
  |── keyword_init (sub_5863A0)
  |   |── 24 subsystem initializers
  |   |── 200+ keyword registrations
  |   └── file scope setup (std::, operators)
  |
  |── [module path]
  |   |── open_source_file (sub_5ADC60)
  |   |── set_include_paths (sub_5AD120)
  |   |── import_declarations (sub_5AF7F0)
  |   |── main_parse_driver (sub_586240)
  |   └── module_finalize (sub_6FDDF0)
  |
  |── [normal path]
  |   |── [optional] preprocessing_open_file (sub_5861C0)
  |   |── [optional] pch_processing (sub_6F4AD0)
  |   |── main_parse_driver (sub_586240)
  |   │   |── open_file_and_push_input_stack (sub_66E6E0)
  |   │   |── enter_scope (sub_66E920)
  |   │   └── begin_file_scope (sub_6702F0)
  |   └── compile_declarations (sub_4E8A60)
  |       └── parse_one_declaration loop until EOF
  |
  |── translation_unit_wrapup (sub_588E90)
  |   |── check_stop_tokens (sub_675DA0)
  |   |── check_class_linkage (sub_446F80)
  |   |── finalize_module_imports (sub_7C24D0)
  |   |── scope_cleanup (sub_709250)
  |   |── exit_file_scope (sub_7047C0)
  |   |── reset_tu_correspondence (sub_7A2FE0)
  |   |── make_module_id (sub_5AF830)
  |   └── [C mode] c_wrapup (sub_76C910)
  |
  └── POP TU stack
      └── [if stack not empty] switch_translation_unit (sub_7A3D60)
```

## Global Variables

### TU Management Globals

| Global | Address | Type | Initial | Identity |
|---|---|---|---|---|
| `qword_106BA10` | `0x106BA10` | `tu_desc_t *` | 0 | `current_tu` -- currently active translation unit |
| `qword_106B9F0` | `0x106B9F0` | `tu_desc_t *` | 0 | `primary_tu` -- first TU (never popped) |
| `qword_12C7A90` | `0x12C7A90` | `tu_desc_t *` | 0 | `tu_chain_tail` -- tail of TU linked list |
| `qword_106BA18` | `0x106BA18` | `stack_entry_t *` | 0 | `tu_stack_top` -- top of TU stack |
| `dword_106B9E8` | `0x106B9E8` | `int` | 0 | `tu_stack_depth` -- depth (excluding primary) |
| `qword_106BA00` | `0x106BA00` | `char *` | 0 | `current_filename` -- source file path |
| `dword_106BA08` | `0x106BA08` | `int` | 0 | `is_recompilation` -- set if second pass |
| `dword_106B9F8` | `0x106B9F8` | `int` | 0 | `has_module_info` -- set if module TU |

### Registration & Tracking Globals

| Global | Address | Type | Identity |
|---|---|---|---|
| `qword_12C7AA8` | `0x12C7AA8` | `reg_var_t *` | Registered variable list head |
| `qword_12C7AA0` | `0x12C7AA0` | `reg_var_t *` | Registered variable list tail |
| `qword_12C7A98` | `0x12C7A98` | `size_t` | Per-TU storage buffer total size |
| `dword_12C7A8C` | `0x12C7A8C` | `int` | Registration-complete flag (1 after first TU starts) |
| `dword_12C7A88` | `0x12C7A88` | `int` | Has-seen-module-TU flag |
| `qword_12C7AB8` | `0x12C7AB8` | `stack_entry_t *` | Stack entry free list |
| `qword_12C7AB0` | `0x12C7AB0` | `void *` | Correspondence record free list |

### Statistics Counters

| Global | Address | Type | Identity |
|---|---|---|---|
| `qword_12C7A78` | `0x12C7A78` | `int64_t` | Total TU descriptors allocated |
| `qword_12C7A80` | `0x12C7A80` | `int64_t` | Total stack entries allocated |
| `qword_12C7A68` | `0x12C7A68` | `int64_t` | Total registration records allocated |
| `qword_12C7A70` | `0x12C7A70` | `int64_t` | Total correspondence records allocated |

These counters are reported by `sub_7A45A0` (`print_trans_unit_statistics`) in verbose mode, with per-record sizes: 424 bytes (TU), 16 bytes (stack entry), 40 bytes (registration), 24 bytes (correspondence).

## Assertions

| File | Line | Function | Condition |
|---|---|---|---|
| `trans_unit.c` | 556 | `pop_translation_unit_stack` | Top-of-stack TU must match `current_tu` |
| `trans_unit.c` | 696 | `process_translation_unit` | Cannot have `module_info==NULL` when `has_seen_module_tu` is set and `is_recompilation==0` |
| `trans_unit.c` | 725 | `process_translation_unit` | First TU (setting `primary_tu`) must have `is_recompilation!=0` |
| `lexical.c` | 17680 | `check_all_stop_token_entries_are_reset` | All 357 stop-token entries must be zero after parsing |
| `fe_init.c` | 1597 | `keyword_init` | Cannot enable both `export` modes simultaneously |
| `fe_init.c` | 2373 | `fe_translation_unit_init` | File scope index must match expected value |

## Cross-References

- [Pipeline Overview](./overview.md) -- stage 5 in the 8-stage pipeline
- [Entry Point & Initialization](./entry.md) -- `main()` calls `sub_7A40A0` after `reset_tu_state`
- [Frontend Wrapup](./fe-wrapup.md) -- `sub_588F90` runs after `process_translation_unit` returns
- [Backend Code Generation](./backend.md) -- consumes the IL tree built during frontend
- [CLI Processing](./cli.md) -- sets the language mode flags that gate keyword registration
- [EDG Lexer](../edg/lexer.md) -- keyword hash table populated by `sub_7463B0`
- [Translation Unit Descriptor](../structs/translation-unit.md) -- full 424-byte layout
- [IL Allocation](../il/allocation.md) -- `sub_6BA0D0` is the same arena allocator used for IL nodes
- [RDC Mode](../cuda/rdc-mode.md) -- multi-TU compilation uses the save/restore mechanism
- [EDG Source File Map](../reference/edg-source-map.md) -- `trans_unit.c` identity and address ranges
