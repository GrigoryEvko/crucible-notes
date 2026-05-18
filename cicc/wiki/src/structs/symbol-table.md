# DenseMap, Symbol Table, and EDG Frontend Structures

The EDG 6.6 frontend layered on LLVM's DenseMap maintains its own declaration nodes, type nodes, and scope stack for C/C++/CUDA semantic analysis. This page documents the EDG-level structures that ride on top of the DenseMap. For the DenseMap implementation itself -- layout, hash function, probing, sentinel values, and growth policy -- see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md).

The EDG symbol tables in this subsystem use the NVVM-layer sentinel pair (-8 / -16) and the pointer hash `(ptr >> 9) ^ (ptr >> 4)`. See the [sentinel reference table](../infra/hash-infrastructure.md#which-sentinel-set-to-expect) for other subsystems.

---

## EDG Declaration Node Layout

The EDG 6.6 frontend represents every C/C++ declaration as a variable-length structure. The canonical declaration node layout was recovered from the top-level declarator parser `sub_662DE0` and the declaration-specifier resolver `sub_7C0F00`.

### Declaration Node (a_decl_node) -- 456+ bytes

| Offset | Size | Type | Field | Evidence |
|--------|------|------|-------|----------|
| +0 | 8B | `ptr` | `decl_id` / entity pointer | `*v31` in `sub_662DE0` |
| +8 | 8B | `uint64_t` | `decl_flags` bitfield (see below) | `v31[1]` |
| +16 | 8B | `uint64_t` | `decl_extra_flags` | `v31[2]` |
| +24 | 16B | | `name` / identifier info | `v31[3..4]` |
| +40 | 8B | | name string for "main" check | strcmp target |
| +72 | 4B | `uint32_t` | `saved_specifier_word1` | v239 in `sub_662DE0` |
| +76 | 2B | `uint16_t` | `saved_specifier_word2` | v240 |
| +80 | 1B | `uint8_t` | `entity_kind` (for scope dispatch) | checked in `sub_860B80` |
| +120 | 1B | `uint8_t` | `accessibility` (bits 0-6, bit 7 reserved) | v241 = `*(a1+120) & 0x7F` |
| +124 | 1B | `uint8_t` | `context_flags_124` | bit 5=explicit_spec, bit 6=class_member |
| +125 | 1B | `uint8_t` | `context_flags_125` | bit 5=was_friend, bit 6=in_class_body, bit 7=template_decl_head |
| +126 | 1B | `uint8_t` | `state_flags` (see below) | mask tests throughout `sub_662DE0` |
| +127 | 1B | `uint8_t` | `extra_state` | bit 0=class_scope_pushed, bit 1=needs_deferred_parse |
| +128 | 8B | `ptr` | `entity_ptr` / scope pointer | compared early in `sub_739430` |
| +130 | 1B | `uint8_t` | `modifier_flags` | bit 5=deferred_parse, bit 6=virtual_specifier |
| +131 | 1B | `uint8_t` | inline/constexpr flag | bit 4 |
| +132 | 1B | `uint8_t` | needs_semicolon_check | bit 1 |
| +140 | 1B | `uint8_t` | `type_kind` (for type_def nodes) | switch discriminant in `sub_766570` case 6 |
| +160 | 8B | `ptr` | `underlying_type` (for typedef) | typedef unwrap chain |
| +168 | 8B | `ptr` | `flags_ptr` | bit 3 checked for fn-pointer |
| +173 | 1B | `uint8_t` | `elaborate_kind` | primary switch in `sub_739430` |
| +176 | var | | `elaborate_sub_kind` / secondary | sub-switch in case 12 |
| +184 | 8B | `ptr` | `parm_list` | `v31[23]` via `sub_5CC190(1)` |
| +224 | 4B | `uint32_t` | `init_kind` | bit 0 = brace-init |
| +256 | 4B | `uint32_t` | `additional_flags` | |
| +268 | 1B | `uint8_t` | `decl_kind_enum` | 0=variable, 4=function, 6=namespace |
| +269 | 1B | `uint8_t` | `storage_class_kind` | 0=none, 1=extern, 2=static |
| +272 | 8B | `ptr` | `decl_type` | `v31[34]` |
| +280 | 8B | `ptr` | `result_type` | `v31[35]` |
| +288 | 8B | `ptr` | `entity_type` / return_type | `v31[36]` |
| +304 | 8B | `ptr` | `template_info` | |
| +352 | 8B | `ptr` | `body_ptr` | `v31[44]` |
| +360 | 8B | `ptr` | `scope_or_context` | `v31[45]` |
| +368 | 8B | `ptr` | `forward_decl_chain` | linked list |
| +416 | 8B | `ptr` | `pending_list` | `v31[52]` |
| +456 | 8B | `ptr` | `extra_entity` | `v31[57]` |

### decl_flags (+8) Bit Definitions

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | 0x1 | `is_definition` / linkage related |
| 1 | 0x2 | `has_initializer` / needs init check |
| 4 | 0x10 | `is_typedef` |
| 5 | 0x20 | `is_template_decl` / friend declaration |
| 6 | 0x40 | `is_inline` |
| 7 | 0x80 | `is_extern` |
| 14 | 0x4000 | `structured_binding` / decomposition decl |

### state_flags (+126) Bit Definitions

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | 0x1 | `has_saved_tokens` |
| 1 | 0x2 | `abstract_declarator_mode` |
| 2 | 0x4 | `has_leading_attributes` |
| 3 | 0x8 | `no_declarator_needed` (typedef etc.) |
| 4 | 0x10 | `suppress_error_recovery` |
| 5 | 0x20 | `in_declarator_parsing` (set on entry) |
| 6 | 0x40 | `in_multi_declarator_loop` |
| 7 | 0x80 | `scope_pushed` |

### entity_kind (+80) Dispatch Values

Used by `sub_860B80` and `sub_7C0F00` phase 3:

| Value | Entity Kind |
|-------|-------------|
| 3 | class |
| 4 | enum (variant A) |
| 5 | enum (variant B) |
| 6 | namespace |
| 10 | function |
| 11 | variable |
| 16 | typedef |
| 17 | template |
| 19 | class template |
| 22 | dependent name |
| 23 | using-declaration |
| 24 | injected-class-name |

### Declaration Node Allocation

`sub_84DCB0` allocates 152-byte declaration entries from a free-list at `qword_4D03C68`, with fallback to the global allocator `sub_823970(152)`. The full node size table at `qword_4B6D500` provides per-tag sizes for all 87 IL node types; the declaration tag (6) indexes into this table for `memcpy` during template instantiation.

---

## EDG Type Node Layout

Type nodes are the central representation for C/C++ types throughout the EDG frontend. Two distinct layouts exist: the **IL-level type node** used by the tree walker (`sub_7506E0`) and the **semantic type node** used by the type comparison engine (`sub_7386E0`). The type translation system (`sub_91AED0`) bridges between these and LLVM types.

### IL-Level Type Node (from `sub_7506E0` tree walker)

The IL tree walker addresses fields as `a1[N]` (8-byte indexed), with byte-level sub-kind tags at specific offsets:

| Offset | Size | Type | Field | Evidence |
|--------|------|------|-------|----------|
| -16 | 8B | `ptr` | `parent_ptr` / owner | shared-node check path |
| -8 | 1B | `uint8_t` | `flags_byte` | bit 0=shared, bit 2=visit-mark |
| +0..+N*8 | var | `ptr[]` | child pointers (typed per kind) | `a1[0]..a1[N]` |
| +24 | 1B | `uint8_t` | expression sub-kind (case 13) | switch discriminant |
| +28 | 1B | `uint8_t` | scope sub-kind (case 23) | 18 sub-kinds |
| +40 | 1B | `uint8_t` | declaration sub-kind (case 21) | 25 sub-kinds |
| +48 | 1B | `uint8_t` | template_arg sub-kind (case 30) | 9 sub-kinds |
| +140 | 1B | `uint8_t` | `type_def_sub_kind` (case 6) | 17 sub-kinds |
| +161 | 1B | `uint8_t` | `type_def_flags` | |
| +168-177 | var | | type sub-kind / sub-sub-kind | |
| +173 | 1B | `uint8_t` | `type_main_kind` (case 2) | 14 sub-kinds by `+173` |
| +176 | 1B | `uint8_t` | `type_sub_sub_kind` | case 6 elaborated |

### Semantic Type Node (from `sub_7386E0` comparison engine)

| Offset | Size | Type | Field | Evidence |
|--------|------|------|-------|----------|
| +0 | 8B | `ptr` | `associated_decl` | `*v10 == *v14` comparison |
| +24 | 1B | `uint8_t` | `type_kind` (0..37) | primary switch discriminant |
| +25 | 1B | `uint8_t` | `cv_qualifiers` | bits 0-1 = const/volatile, bit 6 = restrict |
| +26 | 1B | `uint8_t` | `type_flags_1` | bit 2 compared |
| +27 | 1B | `uint8_t` | `type_flags_2` | bit 1 compared (case 1) |
| +56 | 8B | | `type_payload` / sub_kind | case 1: char at +56 = base_type_kind |
| +58 | 1B | `uint8_t` | `type_extra_flags` | case 1: bits 0x3A compared |
| +64 | 8B | | varies per kind | case 30: word at +64 |
| +72 | 8B | `ptr` | `type_child` / pointer | case 1 integer path |
| +80 | 8B | `ptr` | `linkage_chain` | case 33: namespace list `+80` = next |

### EDG-to-LLVM Type Translation Node (from `sub_918E50`)

The type translation system reads a third view of the type node with offsets optimized for LLVM type construction:

| Offset | Size | Type | Field | Evidence |
|--------|------|------|-------|----------|
| -72 | 8B | `ptr` | `grandparent_type` | nested lookups |
| -48 | 8B | `ptr` | `parent_type_A` | |
| -24 | 8B | `ptr` | `parent_type_B` / first child | |
| -8 | 8B | `ptr` | `indirect_child_array` | if flag 0x40 at +23 |
| +0 | 8B | `ptr` | `llvm_type_descriptor` | `*node` -> LLVM type info |
| +8 | 8B | `ptr` | `member_chain_head` | linked list of class members |
| +16 | 1B | `uint8_t` | `type_kind` | see kind table below |
| +18 | 2B | `uint16_t` | `qualifier_word` | bits 0-14: qualifier ID, bit 15: negation |
| +20 | 4B | `uint32_t` | `child_count` | low 28 bits masked `& 0xFFFFFFF` |
| +23 | 1B | `uint8_t` | `flags` | bit 6 (0x40) = indirect children |
| +24 | 8B | | type-specific data | varies by kind |
| +32 | 8B | | `bitwidth` | enum/integer types |
| +33 | 1B | `uint8_t` | `additional_flags` | bit 5 (0x20) = special treatment |
| +36 | 4B | `uint32_t` | `sub_kind_discriminator` | nested types |
| +40 | 8B | `ptr` | `scope_linkage_ptr` | |
| +48 | 8B | `ptr` | `member_list_head` | linked list |

### type_kind Enumeration (semantic type comparison)

The full enumeration recovered from `sub_7386E0`:

| Value | Name | Comparison Strategy |
|-------|------|-------------------|
| 0 | `tk_none` / void | trivially equal |
| 1 | `tk_fundamental` | sub_kind + base type + class scope |
| 2 | `tk_pointer` | delegate to `sub_739430` on pointee |
| 3 | `tk_class` | scope identity, unique_id, template args |
| 4 | `tk_enum` | scope identity, unique_id |
| 5 | `tk_function` | `sub_73A280` pair compare |
| 6 | `tk_bitfield` | width + base compare |
| 7 | `tk_member_pointer` | multi-field descriptor |
| 8 | `tk_reference` | referent descriptor |
| 10 | `tk_array` | element type recursion |
| 11 | `tk_qualified` | child + qualifier bit |
| 12 | `tk_elaborated` | sub_kind switch (typedef/class/enum) |
| 13 | `tk_pack_expansion` | sub_kind switch |
| 14 | `tk_typeof_expr` | sub_kind switch |
| 15 | `tk_decltype` | sub_kind switch |
| 16 | `tk_nullptr` | trivially equal |
| 17 | `tk_auto` | identity on entity |
| 18 | `tk_function_alt` | `sub_73A280` |
| 20 | `tk_dependent_name` | scope identity + unique_id |
| 22 | `tk_unresolved` | `sub_8D97D0` decl compare |
| 23 | `tk_attributed` | attribute kind + child |
| 24 | `tk_decltype_auto` | identity on entity |
| 25 | `tk_paren` | child list compare |
| 26 | `tk_adjusted` | child type recursion |
| 27 | `tk_typeof_decl` | resolve decl -> type, recurse |
| 30 | `tk_complex` | element type(s) recursion |
| 32 | `tk_template_template_param` | identity + template args |
| 33 | `tk_using_decl` | child list + base class hash table |
| 34 | `tk_atomic` | child + qualifier bit |
| 35 | `tk_vla` | element type recursion |
| 37 | `tk_concept_constraint` | identity on entity |

### EDG-to-LLVM Type Kind Encoding (byte at node+16)

| Value | Hex | Kind |
|-------|-----|------|
| 0-16 | 0x00-0x10 | Primitive / scalar types |
| 17 | 0x11 | Void (special) |
| 5 | 0x05 | Qualified type (const/volatile/restrict) |
| 13 | 0x0D | Enum type |
| 14 | 0x0E | Function type |
| 26 | 0x1A | Array type (subscript form) |
| 27 | 0x1B | Compound type (struct/union/class) |
| 50 | 0x32 | Union variant A |
| 51 | 0x33 | Union variant B |
| 54 | 0x36 | Typedef / using declaration |
| 55 | 0x37 | Using declaration variant |
| 75 | 0x4B | Pointer type |
| 76 | 0x4C | Reference type (lvalue or rvalue) |
| 77 | 0x4D | Member pointer type |
| 78 | 0x4E | Dependent / nested type |

### Qualifier Word Values (node+18 & 0x7FFF)

| Value | CUDA Memory Space |
|-------|------------------|
| 1 | Address space 1 (global memory) |
| 9 | Address space 9 (generic, gated by `sub_5F3280`) |
| 14 | Function / method qualifier |
| 26 | Array subscript context A |
| 27 | Array subscript context B |
| 32 | Address space 32 (shared memory) |
| 33 | Address space 33 (constant memory) |

### Type Canonicalization -- `sub_72EC50`

Before any type comparison, both sides are canonicalized by stripping non-template typedef aliases:

```
fn edg_canonicalize_type(type) -> type:
    while type.type_kind == 2:               // tk_elaborated
        scope = type.payload_at_56
        if scope.elaborate_kind != 12:       // not typedef_name
            break
        if scope.elaborate_sub_kind != 1:    // not single-member typedef
            break
        if scope.class_flags & 0x10:         // has template specialization
            break
        type = sub_72E9A0(type)              // unwrap one layer
    return type
```

This peels through chains like `typedef int MyInt; typedef MyInt YourInt;` down to the fundamental type. Template specialization aliases are never unwrapped.

---

## EDG Scope Stack

The scope stack is a global array of 776-byte entries, indexed by a scope depth counter. It represents the C++ scope nesting at parse time (file scope -> namespace -> class -> function -> block).

### Global State

| Address | Type | Name | Purpose |
|---------|------|------|---------|
| `qword_4F04C68` | `ptr` | Scope stack base | heap-allocated array of 776B entries |
| `dword_4F04C64` | `int32_t` | Current scope index | top of the scope stack |
| `dword_4F04C5C` | `int32_t` | Previous scope index | saved parent index |
| `dword_4F04C44` | `int32_t` | Namespace scope index | deepest enclosing namespace |
| `dword_4F04C34` | `int32_t` | Class scope index | deepest enclosing class |
| `dword_4F04C40` | `int32_t` | Another scope index | auxiliary scope tracking |
| `dword_4F04C3C` | `int32_t` | Module linkage flag | C++20 module scope state |
| `unk_4F04C48` | `int32_t` | Parent scope check | used by using-declaration handler |

### Scope Stack Entry Layout (776 bytes)

Each entry at `qword_4F04C68[0] + 776 * index`:

| Offset | Size | Type | Field |
|--------|------|------|-------|
| +0 | 4B | `uint32_t` | `scope_id` |
| +4 | 2B | `uint16_t` | `scope_kind` (see table below) |
| +6 | 1B | `uint8_t` | `flags_a` |
| +7 | 1B | `uint8_t` | `flags_b` |
| +8 | 1B | `uint8_t` | `flags_c` |
| +9 | 1B | `uint8_t` | `flags_d` |
| +10 | 1B | `uint8_t` | `flags_e` |
| +24 | 8B | `ptr` | `name_list_head` |
| +32 | 8B | `ptr` | `name_list_tail` |
| +208 | 8B | `ptr` | `class_type_ptr` |
| +232 | 8B | `ptr` | `deferred_list` |
| +328 | 8B | `ptr` | `template_info` |
| +552 | 4B | `int32_t` | `parent_scope_index` |
| +624 | 8B | `ptr` | `declaration_ptr` |
| +680 | 8B | | field used by `sub_7C0F00` |
| +688 | 4B | `uint32_t` | `entity_number_counter` (for mangling) |
| +696 | 4B | `uint32_t` | `entity_number_counter_2` |

### scope_kind Values

| Value | Scope Kind |
|-------|------------|
| 5 | namespace |
| 6 | class |
| 7 | function |
| 8 | block (compound statement) |
| 9 | enum |
| 12 | template parameter |

### Push / Pop Operations

```
sub_854590(0)   // push_scope -- increments dword_4F04C64, initializes new entry
sub_854430()    // pop_scope  -- decrements dword_4F04C64, restores parent
sub_854AB0(...) // pop_declarator_scope (context-specific cleanup)
sub_854B40()    // push_declarator_scope (declarator-specific init)
```

The scope depth counter at `qword_4F061C8 + 64` is bumped independently for declarator nesting depth tracking. Class scope depth lives at `qword_4F061C8 + 81`.

---

## Scope Chain Traversal Algorithm

The declaration-specifier resolver `sub_7C0F00` performs scope chain traversal to resolve qualified names. The algorithm was recovered from Phase 4 (lines 1197-1600) of that function.

### Unqualified Name Lookup

```
fn lookup_unqualified(name, scope_index) -> entity:
    // Phase 2 of sub_7C0F00
    // Try each lookup strategy in priority order:

    result = sub_7D5DD0(name)                    // unqualified lookup in current scope
    if result:
        return result

    result = sub_7D2AC0(name, flags)             // lookup with specific flags
    if result:
        return result

    result = sub_7ACA80(name)                    // ADL / ambiguity resolution
    return result
```

### Qualified Name Lookup (`A::B::C`)

The scope iteration loop at LABEL_282/283/285/288 walks the scope chain:

```
fn lookup_qualified(base_entity, remaining_name) -> entity:
    current = base_entity
    while true:
        // Check if "::" follows the current entity
        if current_token != TK_SCOPE_RESOLUTION:  // token 37
            return current

        consume_token()  // sub_7B8B50

        // Classify the current entity
        kind = current.entity_kind  // byte at +80
        switch kind:
            case 6:   // namespace
                result = sub_7D4A40(current, remaining_name)  // namespace lookup
            case 3:   // class
            case 19:  // class template
                result = sub_7D2AC0(current, remaining_name, MEMBER_FLAG)
            case 17:  // template
                result = sub_830940(current, remaining_name)  // class template lookup
            default:
                result = sub_7D4600(current, remaining_name)  // generic qualified lookup

        if !result:
            // Error: member not found in scope
            sub_6851C0(error_code, context)
            return null

        current = result

        // Check member access, visibility, redeclaration
        sub_8841F0(current, scope_entry)  // access check for C++ members
```

### Self-Recursive Qualified Resolution

When the declaration-specifier resolver encounters a `::` after resolving a name, it recurses into itself at `sub_7C0F00(20, a2)` where flags=20 decodes as:

- bit 2 (0x04) = nested declarator sub-parse context
- bit 4 (0x10) = restrict parse to type-specifiers only

This handles arbitrarily deep qualified names like `A::B::C::D`. Recursion depth is bounded by the nesting depth of the qualified name.

### Scope Chain Walking for Declaration Resolution

`sub_868D90` (ADL / instantiation lookup) walks the scope chain upward:

```
fn walk_scope_chain(start_index) -> entity:
    index = start_index
    while index >= 0:
        entry = scope_table_base + 776 * index
        // Check if this scope contains the target declaration
        // ... name lookup within the scope's name list ...

        // Move to parent scope
        index = entry.parent_scope_index  // at offset +552
```

---

## Type Comparison Engine

`sub_7386E0` implements structural type comparison for the EDG frontend. It performs a parallel tree walk over two type nodes, comparing them field-by-field with mode-dependent strictness.

### Calling Convention

```
sub_7386E0(packed_pair: __int128, flags: int) -> bool
    packed_pair.low  = type_A pointer
    packed_pair.high = type_B pointer
    flags bits:
        0-1: cv_compare_mode (0=strict, 1=relaxed, 2=overload)
        2:   template_matching_mode
        5:   anonymous_class_structural_compare
```

### Comparison Algorithm

```
fn compare_types(type_A, type_B, flags) -> bool:
    // 1. Null handling
    if both null: return true
    if either null: return false

    // 2. Canonicalize (strip non-template typedefs)
    type_A = sub_72EC50(type_A)
    type_B = sub_72EC50(type_B)

    // 3. Quick-reject on header bytes
    if type_A.type_kind != type_B.type_kind: return false
    if (type_A.cv_quals ^ type_B.cv_quals) & 0x43: return false  // const/volatile/restrict
    if (type_A.flags_1 ^ type_B.flags_1) & 0x04: return false

    // 4. Type-specific structural comparison
    switch type_A.type_kind:
        case 3 (class):
            if type_A.scope == type_B.scope: return true     // identity shortcut
            if unique_id_enabled:
                if scope_A.unique_id == scope_B.unique_id: return true
            if template_mode:
                return sub_89BAF0(...)  // template arg list compare
            if anonymous_mode && both_anonymous:
                return sub_739430(member_list_A, member_list_B)

        case 7 (member_pointer):
            // Compare: flags, class ptr, scope ptr, return type,
            // params, exception spec -- 6 sub-comparisons

        case 33 (using_decl) in overload mode:
            // Hash table lookup at qword_4D03BF8 for base class lists
            // Element-by-element comparison of 24-byte triples

        // ... 35 other cases ...

    // 5. Post-switch: declaration pointer compare
    if type_A.decl != type_B.decl:
        if !sub_8D97D0(type_A.decl, type_B.decl): return false
    return true
```

### Helper Functions

| Address | Role | Purpose |
|---------|------|---------|
| `sub_7386E0` | Type-tree comparator (top-level) | Top-level structural compare |
| `sub_739370` | Type-list comparator | Linked-list comparator (next at +16) |
| `sub_739430` | Declaration-type comparator | Declaration-level comparator (661 lines) |
| `sub_73A280` | Type-pair trivial wrapper | Trivial wrapper: null=equal |
| `sub_72EC50` | Type canonicalizer | Strip typedef / elaborated aliases |
| `sub_8D97D0` | Decl-identity comparator | Name/entity identity comparison |
| `sub_8C7520` | Class template-identity check | Same primary class template check |
| `sub_89AB40` | Template-args comparator | Template argument list comparison |
| `sub_89BAF0` | Template-args full-context comparator | Full template context compare |

### Key Global: `dword_4F07588` -- unique_id optimization

When set, enables O(1) identity comparison via the `unique_id` field at `scope+32`. This avoids recursive structural comparison for named classes and enums. The field is compared as a non-null integer; matching non-null values prove the two types refer to the same entity.

---

## IL Tree Walker and Copier

### Tree Walker -- `sub_7506E0` (37 KB native; 7,283 lines decomp)

The generic IL tree walker visits every node in the EDG intermediate representation. It dispatches on 83 node kinds (1-86 with gaps at 24-26) using a massive switch statement.

**Callback table** at `.bss` `0x4F08014..0x4F08040`:

| Address | Type | Callback | Call Sites |
|---------|------|----------|------------|
| `dword_4F08014` | `bool` | `skip_shared_nodes` | flag |
| `dword_4F08018` | `bool` | `clear_back_pointers` | 49 sites |
| `qword_4F08020` | `fn(node, kind) -> node` | `list_node_rewrite_fn` | 206 sites |
| `qword_4F08028` | `fn(node, kind) -> node` | `child_rewrite_fn` | 926 sites |
| `qword_4F08030` | `fn(node, kind) -> bool` | `pre_visit_fn` | 2 sites |
| `qword_4F08038` | `fn(str, kind, len)` | `string_visitor_fn` | 80 sites |
| `qword_4F08040` | `fn(node, kind)` | `post_visit_fn` | 14 sites |

**Visit-mark protocol**: Each node has a flag byte at `node[-8]`. Bit 2 tracks "visited in current pass" with polarity toggled per walk pass via `dword_4D03B64`. This avoids clearing visited marks between walks.

**Linked-list traversal pattern** (60+ lists walked):

```
for cursor = node.field; cursor; cursor = cursor.next:
    if list_node_rewrite_fn:
        cursor = list_node_rewrite_fn(cursor, child_kind)
    if cursor:
        walk_il_node(cursor, child_kind)
        cursor = node.field  // re-read (rewrite may have changed it)
```

Next-pointer stride varies by node kind: +0, +16, +24, +32, +56, +112, +120 bytes.

### Tree Copier -- `sub_766570` (24 KB native; 5,187 lines decomp)

The copier is driven by template instantiation (`sub_8C5CD0` -> `sub_8C4EC0` -> `sub_8C2C50` -> `sub_766570`). It uses the walker's callback infrastructure:

- `sub_8C38E0` = `copy_ref` callback: resolves pending copy destinations
- `sub_8C3810` = `copy_scope` callback: resolves scope-level copies
- Node sizes from `qword_4B6D500[tag]` (87+ entries, one per IL node type)

**Copy protocol** using flag bits at `node[-8]`:

| Bits | Meaning |
|------|---------|
| 0x1 | needs copy, not yet started |
| 0x2 | copy in progress |
| 0x3 | pending copy (both bits) |
| 0x4 | copy destination allocated |

Copy destination stored at `*(node - 24)`. When both bits 0 and 1 are set, `sub_8C3650` forces the copy by allocating `qword_4B6D500[tag]` bytes and performing `memcpy` followed by pointer rewriting.

---

## EDG-to-LLVM Type Translation System

Entry: `sub_91AED0` -> `sub_91AB30`. Uses a worklist-driven fixed-point iteration.

### Translation Context Object (at `a1+160`)

| Offset | Size | Field |
|--------|------|-------|
| +0x000 | 8B | `debug_logger` |
| +0x008 | 8B | `pass_list_ptr` |
| +0x038 | 8B | `edg_node_map` (DenseMap: EDG -> LLVM values) |
| +0x058 | 8B | `visited_set` (DenseSet for dedup) |
| +0x060 | 4B | `visited_count` |
| +0x064 | 4B | `visited_capacity` |
| +0x068 | 4B | `bucket_count` |
| +0x090 | 8B | `type_cache` (DenseMap: EDG type -> LLVM Type*) |
| +0x168 | 4B | `threshold` |
| +0x2A0 | 8B | `pending_replacements` |
| +0x2A8 | 4B | `pending_count` |

### Fixed-Point Algorithm

```
fn translate_all_types(ctx, module):
    // Phase 1: iterate module members
    for member in module.member_list:
        sub_AA3700(member)  // gather initial flags

    // Phase 2: fixed-point iteration
    do:
        ordering = sub_919CD0(module)        // topological sort (10-level BFS)
        for type in ordering.reverse():
            sub_913880(ctx, type)            // invalidate stale cache entries
        for type in ordering.reverse():
            changed |= sub_9197C0(ctx, type) // process single declaration
    while changed

    // Phase 3: optional late fixup (byte_3C35480-gated)
    if optimization_enabled:
        do:
            changed = sub_917E30(ctx)
        while changed

    // Phase 4: cleanup
    sub_909590(ctx)
```

### Bitmask for Scope-Tracking Types

The expression `0x100000100003FF >> (kind - 25)` selects which type kinds in the range [25..78] require scope tracking during translation. This covers compound types, pointer types, and dependent types that carry CUDA address-space qualifiers.

---

## Usage Across the Compiler

DenseMap instances appear at these known locations:

- **NVVM context object**: 8+ tables for IR node uniquing (opcodes 0x10..0x1F), plus sub-function tables for opcodes 0x04..0x15.
- **SelectionDAG builder context**: Map A (+120), Map B (+152), Set C (+184) for node deduplication and worklist.
- **Per-node analysis**: embedded DenseSet at +72 inside analysis structures created during DAG construction.
- **Instruction constraint table**: the global `word_3F3E6C0` array is a flat table rather than a DenseMap, but the constraint emission functions use DenseMaps for lookup caching.
- **EDG type translation**: 5 distinct caches -- visited set, type cache, type-value map, scope table, and type index table.
- **Base class comparison**: `qword_4D03BF8` hash table for overload-resolution base class triple lookup.

The consistency of the hash function, sentinel values, and growth policy across all instances is documented in [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md).

## Cross-References

- [IR Node Layout](ir-node.md) -- NVVM IR node structure and operand access
- [DAG Node](dag-node.md) -- SelectionDAG builder that consumes DenseMap instances
- [Pattern Database](pattern-db.md) -- instruction selection patterns indexed by DenseMap
- [Address Spaces](../reference/address-spaces.md) -- CUDA memory space qualifier values
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- comprehensive DenseMap documentation
- [EDG Frontend](../pipeline/edg.md) -- EDG tokenizer and keyword dispatch
- [IRGen Types](../pipeline/irgen-types.md) -- EDG-to-LLVM type translation detail

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_662DE0` | `sub_662DE0` | -- | Top-level EDG declarator parser |
| `sub_672A20` | `sub_672A20` | -- | EDG decl-specifier while/switch token dispatcher |
| `sub_7C0F00` | `sub_7C0F00` | -- | EDG scope-chain + qualified-name resolver |
| `sub_7386E0` | `sub_7386E0` | -- | Structural type-tree comparison |
| `sub_739370` | `sub_739370` | -- | Linked-list type comparator |
| `sub_739430` | `sub_739430` | -- | Declaration-level type comparator |
| `sub_72EC50` | `sub_72EC50` | -- | Typedef / elaborated-alias stripper |
| `sub_74A390` | `sub_74A390` | -- | Type-to-string for diagnostics |
| `sub_7506E0` | `sub_7506E0` | -- | 190KB IL tree walker (297 recursive calls) |
| `sub_766570` | `sub_766570` | -- | 148KB IL tree copier |
| `sub_854590` | `sub_854590` | -- | Push scope stack entry |
| `sub_854430` | `sub_854430` | -- | Pop scope stack entry |
| `sub_82BDA0` | `sub_82BDA0` | -- | Scope-chain emission |
| `sub_7D5DD0` | `sub_7D5DD0` | -- | Unqualified name lookup |
| `sub_7D4600` | `sub_7D4600` | -- | Qualified name lookup (after `::`) |
| `sub_7D2AC0` | `sub_7D2AC0` | -- | Lookup with specific mode flags |
| `sub_7D4A40` | `sub_7D4A40` | -- | Lookup in namespace scope |
| `sub_8D97D0` | `sub_8D97D0` | -- | Entity-identity comparison |
| `sub_91AED0` | `sub_91AED0` | -- | Top-level EDG-to-LLVM type-translation entry |
| `sub_91AB30` | `sub_91AB30` | -- | Type-translation fixed-point iteration driver |
| `sub_918E50` | `sub_918E50` | -- | Type-kind dispatch for translation |
| `sub_911D10` | `sub_911D10` | -- | Core type-pair comparison + replacement |
| `sub_84DCB0` | `sub_84DCB0` | -- | 152-byte declaration-node allocator |
