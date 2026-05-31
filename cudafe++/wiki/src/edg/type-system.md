# Type System

The type system in cudafe++ is EDG 6.6's implementation of the C++ type representation, query, construction, comparison, and layout infrastructure. It lives primarily in `types.c` (250+ functions at `0x7A4940`--`0x7C02A0`) with type allocation in `il_alloc.c` (`0x5E2E80`--`0x5E45C0`), type construction helpers in `il.c` (`0x5D64F0`--`0x5D6DB0`), and class layout computation in `layout.c` (`0x65EA50`--`0x665B50`).

Every C++ entity -- variable, function parameter, expression result, template argument -- carries a type pointer. EDG represents types as 176-byte heap-allocated nodes organized by a `type_kind` discriminant, with supplementary structures for complex kinds (classes, functions, integers, typedefs, template parameters). Type identity in the IL is pointer-based: two types are the "same type" if and only if they resolve to the same canonical node after chasing typedef chains. This page documents the complete type node architecture, the 22 type kinds, the 130 leaf query functions, the MRU-cached type construction pipeline, and the Itanium ABI class layout engine.

## Key Facts

| Property | Value |
|---|---|
| Source file | `types.c` (250+ functions), `il_alloc.c` (allocators), `il.c` (construction), `layout.c` (class layout) |
| Address range | `0x7A4940`--`0x7C02A0` (types.c), `0x5E2E80`--`0x5E45C0` (alloc), `0x5D64F0`--`0x5D6DB0` (il.c), `0x65EA50`--`0x665B50` (layout) |
| Type node size | 176 bytes (raw allocation includes 16-byte IL prefix) |
| Type kind count | 22 values (`0x00`--`0x15`) |
| Leaf query functions | 130 at `0x7A6260`--`0x7A9F90` (3,648 total call sites across binary) |
| Class layout entry | `sub_662670` (`do_class_layout`), 2,548 lines |
| Type allocator | `sub_5E3D40` (`alloc_type`), 176-byte bump allocation |
| Kind dispatch | `sub_5E2E80` (`set_type_kind`), 22-way switch |
| Qualified type cache | `sub_5D64F0` (`f_make_qualified_type`), MRU linked list at type `+112` |
| Type comparison | `sub_7AA150` (`types_are_identical`), 636 lines |
| Top query by callers | `is_class_or_struct_or_union_type` at `0x7A8A30` (407 call sites) |
| Type counter global | `qword_126F8E0` (incremented on every `alloc_type`) |
| Void type singleton | `qword_126E5E0` |

## Type Node Layout (176 Bytes)

Every type in the IL is a 176-byte node allocated by `alloc_type` (`sub_5E3D40`). The allocator prepends a 16-byte IL prefix (8-byte TU-copy address + 8-byte next pointer), so the pointer returned to callers points at offset `+16` of the raw allocation. All offsets below are relative to the returned pointer.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 96 | common header | Copied from `xmmword_126F6A0..126F6F0` at allocation time |
| `+0` | 8 | `source_corresp` | Source position info |
| `+8` | 1 | `prefix_flags` | IL entry prefix: bit 0 = allocated, bit 1 = file-scope, bit 3 = language |
| `+112` | 8 | `qualified_chain` | Head of MRU linked list of cv-qualified variants |
| `+120` | 4 | `size_info` | Type size in target units (for constexpr value computation) |
| `+128` | 4 | `alignment` | Type alignment |
| `+132` | 1 | `type_kind` | Discriminant byte: 0--21 (22 values) |
| `+133` | 1 | `type_flags_1` | Bit 5 = is_dependent |
| `+134` | 1 | `elaboration_flags` | Low 2 bits = elaboration specifier kind |
| `+136` | 1 | `type_flags_3` | Bit 2 = bitfield flag, bit 5 = unqualified strip flag |
| `+144` | 8 | `referenced_type` | Points to base/element/return type (kind-dependent). For pointers: pointed-to type. For arrays: element type. For typedefs: underlying type |
| `+145` | 1 | `integer_subkind` | (overlaps `+144` byte 1; valid when kind==2) Bit 3 = scoped enum, bit 4 = bit-int capable |
| `+146` | 1 | `integer_flags` | (overlaps `+144` byte 2; valid when kind==2) Bit 2 = `_BitInt` |
| `+152` | 8 | `supplement_ptr` | Pointer to kind-specific supplement, or member-pointer class type (kind==6 with member bit, kind==13) |
| `+153` | 1 | `array_flags` | (overlaps `+152` byte 1; valid when kind==8) Bit 0 = dependent, bit 1 = VLA, bit 5 = star-modified |
| `+160` | 8 | `secondary_data` | Array bound (kind==8) / attribute info (kind==12) / enum underlying type (kind==2) |
| `+161` | 1 | `qualifier_or_class_flags` | Typeref: cv-qualifier bits (kind==12). Class: bit 0 = local, bit 4 = template, bit 5 = anonymous (kind==9/10/11) |
| `+163` | 1 | `class_flags_2` | (valid when kind==9/10/11) Bit 0 = empty class |
| `+164` | 1 | `feature_usage` | Copied to `byte_12C7AFC` by `record_type_features_used` |

Note: Fields at offsets `+144`--`+164` form a union-like region. Different type kinds interpret these bytes differently. The overlap is intentional -- a pointer type uses `+152` for the class pointer while an array type uses `+153` for VLA flags, and so on.

The `type_kind` byte at `+132` is the single most frequently read field in the entire binary. Every type query function begins by checking it, and the canonical typedef-chase pattern reads it in a tight loop.

## Type Kind Enumeration (22 Values)

EDG uses 22 type kind values (`tk_*`), each with optional supplementary allocations for kind-specific metadata.

| Value | Name | Supplement | Supplement Size | Description |
|---|---|---|---|---|
| 0 | `tk_none` | -- | -- | Sentinel / uninitialized |
| 1 | `tk_void` | -- | -- | `void` type |
| 2 | `tk_integer` | `integer_type_supplement` | 32 B | All integer types: `bool`, `char`, `short`, `int`, `long`, `long long`, `__int128`, `_BitInt(N)`, and enumerations. Subkind at `+145` discriminates |
| 3 | `tk_float` | -- | -- | `float` (format byte at `+144` = 2) |
| 4 | `tk_double` | -- | -- | `double` |
| 5 | `tk_long_double` | -- | -- | `long double`, `__float128`, `_Float16`, `__bf16` |
| 6 | `tk_pointer` | -- | -- | Pointer to T. Bit 0 of `+152` distinguishes member pointers from object pointers |
| 7 | `tk_routine` | `routine_type_supplement` | 64 B | Function type. Supplement holds parameter list, calling convention, `this`-class pointer, exception specification |
| 8 | `tk_array` | -- | -- | Array of T. Bound at `+160`, element type at `+144` |
| 9 | `tk_struct` | `class_type_supplement` | 208 B | `struct` type |
| 10 | `tk_class` | `class_type_supplement` | 208 B | `class` type |
| 11 | `tk_union` | `class_type_supplement` | 208 B | `union` type |
| 12 | `tk_typeref` | `typeref_type_supplement` | 56 B | Typedef / elaborated type. References the underlying type at `+144`. This is the "chase me" kind |
| 13 | `tk_pointer_to_member` | -- | -- | Pointer-to-member. Member type at `+144`, class type at `+152` |
| 14 | `tk_template_param` | `templ_param_supplement` | 40 B | Unresolved template type parameter |
| 15 | `tk_typeof` | -- | -- | `typeof` / `__typeof__` expression type |
| 16 | `tk_decltype` | -- | -- | `decltype(expr)` type |
| 17 | `tk_pack_expansion` | -- | -- | Parameter pack expansion |
| 18 | `tk_auto` | -- | -- | `auto` / `decltype(auto)` placeholder |
| 19 | `tk_rvalue_reference` | -- | -- | Rvalue reference `T&&` |
| 20 | `tk_nullptr_t` | -- | -- | `std::nullptr_t` |
| 21 | `tk_reserved` | -- | -- | Reserved / unused (handled as no-op in `set_type_kind`) |

### The Integer Type Supplement (32 Bytes)

Integer types (kind 2) carry a 32-byte supplement allocated by `set_type_kind` and tracked by `qword_126F8E8`. This supplement discriminates the enormous variety of C++ integer types -- `bool`, `char`, `signed char`, `unsigned char`, `wchar_t`, `char8_t`, `char16_t`, `char32_t`, `short`, `int`, `long`, `long long`, `__int128`, `_BitInt(N)`, and all scoped/unscoped enumerations.

The integer subkind value (at byte `+145` of the parent type node) encodes:

| Value | Type Category |
|---|---|
| 1--10 | Standard integer types (`bool` through `unsigned long long`) |
| 11 | `_BitInt` / extended integer |
| 12 | `__int128` / extended |

Signedness is determined by a lookup table at `byte_E6D1B0`, indexed by the integer subkind value.

### The Routine Type Supplement (64 Bytes)

Function types (kind 7) carry a 64-byte supplement tracked by `qword_126F958`. Key fields:

| Offset (in supplement) | Size | Field |
|---|---|---|
| `+0` | 8 | Parameter type list head |
| `+8` | 8 | Exception specification |
| `+16` | 4 | Calling convention / noexcept flags |
| `+32` | 16 | Bitfield struct (ABI attributes, variadic flag) |
| `+40` | 8 | `this`-class pointer (for member functions) |

### The Class Type Supplement (208 Bytes)

Class/struct/union types (kinds 9/10/11) carry a 208-byte supplement tracked by `qword_126F948`. This is the largest supplement and contains the full class metadata:

| Offset (in supplement) | Size | Field |
|---|---|---|
| `+0` | 8 | Scope pointer (member declarations) |
| `+8` | 8 | Base class list head |
| `+16` | 8 | Virtual function table pointer |
| `+40` | 4 | Initialized to 1 by `init_class_type_supplement_fields` |
| `+86` | 1 | Bit 0 = has virtual bases, bit 3 = has user conversion |
| `+88` | 1 | Bit 5 = has flexible array / VLA member |
| `+100` | 4 | Class kind (9=struct, 10=class, 11=union) |
| `+128` | 8 | Scope block pointer |
| `+176` | 4 | Initialized to -1 (sentinel) |

### The Typeref Supplement (56 Bytes)

Typedef types (kind 12) carry a 56-byte supplement tracked by `qword_126F8F0`. A typeref wraps another type, creating the alias chain that all query functions must chase. The supplement holds the typedef declaration entity, elaborated type specifier information, and attribute data.

## The Typedef Chase Pattern

The most pervasive code pattern in the entire binary is the typedef chase loop. Because C++ types may be wrapped in arbitrarily many typedef layers (`typedef int myint; typedef myint myint2;`), every function that inspects a type property must first resolve through all typedef indirections to reach the underlying canonical type.

The canonical pattern appears in every one of the 130 leaf query functions:

```c
// Canonical typedef chase — appears 130+ times in types.c
type_t *skip_typedefs(type_t *type) {
    while (type->type_kind == 12)   // 12 = tk_typeref
        type = type->referenced_type;  // offset +144
    return type;
}

bool is_class_or_struct_or_union_type(type_t *type) {
    type = skip_typedefs(type);
    int kind = type->type_kind;     // offset +132
    return kind == 9 || kind == 10 || kind == 11;
}
```

In x86-64 machine code, this compiles to a 3-instruction loop:

```asm
.loop:
    cmp  byte [rdi+132], 12       ; type->type_kind == tk_typeref?
    jne  .done
    mov  rdi, [rdi+144]           ; type = type->referenced_type
    jmp  .loop
.done:
```

#### Worked Example: Chasing a Three-Layer Alias

Given a source like:

```cpp
typedef int            I;            // I    -> int
using   CI           = const I;      // CI   -> typeref(const, I)
using   CIPtr        = CI*;          // CIPtr-> ptr -> typeref -> typeref -> int
CIPtr p;
```

The type pointer for `p` walks the IL graph as: `kind=6 (tk_pointer)` -> follow `+144` -> `kind=12 (tk_typeref CI)` cv-bits=const -> `+144` -> `kind=12 (tk_typeref I)` -> `+144` -> `kind=2 (tk_integer, int)`. `is_class_or_struct_or_union_type(typeof(p))` calls `skip_typedefs` which stops at the pointer (kind 6, not 12) and returns false in a single iteration -- the typedef layers under the pointer are never touched. By contrast, `type_pointed_to` peels the pointer first, then `skip_typedefs` strips both `tk_typeref` nodes to reach `int` while preserving the accumulated `const` qualifier.

### Why 130 Separate Functions?

A natural question: why does EDG have 130 individual query functions instead of a single `get_type_kind()` accessor? The answer is the EDG compilation model. Each function in `types.c` is a public API entry point that other source files (`parse.c`, `lower.c`, `templates.c`, etc.) can call without including the full type-system header. This provides:

1. **Encapsulation.** Callers never see the `type_kind` enum values or internal layout. They call `is_class_or_struct_or_union_type()` instead of checking `kind == 9 || kind == 10 || kind == 11`.

2. **Binary stability.** If EDG adds a new type kind or renumbers existing ones, only `types.c` needs recompilation. All callers are insulated.

3. **Fast-path optimization.** Each leaf function is tiny (10--30 bytes of machine code), fits in a single cache line, and branches on at most 2--3 constants. The branch predictor handles these trivially.

4. **Semantic naming.** `is_arithmetic_type()` is self-documenting where `kind >= 2 && kind <= 5` is not. This matters in a 2.5M-line codebase.

### Query Function Catalog (Top 30 by Caller Count)

| Address | Callers | Function | Returns |
|---|---|---|---|
| `0x7A8A30` | 407 | `is_class_or_struct_or_union_type` | kind in {9,10,11} |
| `0x7A9910` | 389 | `type_pointed_to` | `ptr->referenced_type` (kind==6) |
| `0x7A9E70` | 319 | `get_cv_qualifiers` | Accumulated cv-qualifier bits (`& 0x7F`) |
| `0x7A6B60` | 299 | `is_dependent_type` | Bit 5 of byte `+133` |
| `0x7A7630` | 243 | `is_object_pointer_type` | kind==6 and not member pointer |
| `0x7A8370` | 221 | `is_array_type` | kind==8 |
| `0x7A7B30` | 199 | `is_member_pointer_or_ref` | kind==6 with member bit |
| `0x7A6AC0` | 185 | `is_reference_type` | kind==7 |
| `0x7A8DC0` | 169 | `is_function_type` | kind==14 |
| `0x7A6E90` | 140 | `is_void_type` | kind==1 |
| `0x7A7C40` | 132 | `is_trivially_copy_constructible` | Recursive triviality check |
| `0x7A9350` | 126 | `array_element_type` (deep) | Strips arrays+typedefs to element |
| `0x7A7010` | 85 | `is_enum_type` | kind==2 with scoped check |
| `0x7A71B0` | 82 | `is_integer_type` | kind==2 |
| `0x7A8020` | 77 | `type_size_and_alignment` | Computes sizeof/alignof |
| `0x7A7810` | 77 | `is_member_pointer_flag` | kind==6, bit 0 of `+152` |
| `0x7A8270` | 77 | `get_mangled_type_encoding` | Type encoding for name mangling |
| `0x7A8D90` | 76 | `is_pointer_to_member_type` | kind==13 |
| `0x7A73F0` | 70 | `is_long_double_type` | kind==5 |
| `0x7A7950` | 68 | `is_member_ptr_with_both_bits` | kind==6, bits 0 and 1 of `+152` |
| `0x7A70F0` | 62 | `is_scoped_enum_type` | kind==2, bit 3 of `+145` |
| `0x7A6EF0` | 56 | `is_rvalue_reference_type` | kind==19 (rvalue reference `T&&`) |
| `0x7A9310` | 51 | `array_element_type` (shallow) | One-level array to element |
| `0x7A6B90` | 46 | `is_simple_function_type` | kind==8, specific flag pattern |
| `0x7A7220` | 43 | `is_bit_int_type` | kind==2, bit 2 of `+146` |
| `0x7A7300` | 42 | `is_floating_point_type` | kind in {3,4,5} |
| `0x7A7750` | 40 | `is_non_member_ptr_type` | kind==6, no member bit |
| `0x7A6EC0` | 39 | `is_nullptr_t_type` | kind==20 |
| `0x7A99D0` | 37 | `pm_member_type` | kind==13, extracts member type at `+152` |
| `0x7A8F10` | 34 | `is_unresolved_function_type` | kind==14, constraint check |

Total: 128 unique query functions, 4,448 call sites, average 34.75 callers per function.

### Typedef Stripping Variants

Six specialized typedef-stripping functions exist, each stopping at a different boundary:

| Address | Function | Behavior |
|---|---|---|
| `0x7A68F0` | `skip_typedefs` | Strips all typedef layers, preserves cv-qualifiers |
| `0x7A6930` | `skip_named_typedefs` | Strips typedefs that have no name |
| `0x7A6970` | `skip_to_attributed_typedef` | Stops at typedef with attribute flag set |
| `0x7A69C0` | `skip_typedefs_and_attributes` | Strips both typedef and attribute layers |
| `0x7A6A10` | `skip_to_elaborated_typedef` | Stops at typedef with elaborated-type-specifier flag |
| `0x7A6A70` | `skip_non_attributed_typedefs` | Stops at typedef with any attribute bits |

These variants exist because C++ semantics sometimes care about intermediate typedef layers. For example, `[[deprecated]] typedef int bad_int;` attaches the attribute to the typedef itself, not to `int`. A function checking for deprecation must stop at the attributed typedef layer rather than chasing through to `int`.

### Duplicate Query Functions

Several functions are exact binary duplicates with distinct addresses:

- `0x7A7630` = `0x7A7670` = `0x7A7750` (`is_non_member_pointer` / `is_object_pointer_type`)
- `0x7A7B00` = `0x7A7B70` (`is_pointer_type`)
- `0x7A78D0` = `0x7A7910` (`is_non_const_ref`)

These duplicates exist because EDG uses distinct function names for semantic clarity even when the implementation is identical. The function-level linker does not merge them because they have distinct symbols with different ABI meanings: callers of `is_object_pointer_type()` and `is_non_member_pointer_type()` conceptually ask different questions even though the current answer is the same. If a future C++ revision changed pointer semantics, only one function would need updating.

## Type Allocation

Type nodes are allocated by `alloc_type` (`sub_5E3D40`), which follows the standard IL allocation pattern used by all node allocators in `il_alloc.c`:

```c
type_t *alloc_type(int type_kind) {
    // 1. Optional debug trace
    if (dword_126EFC8)
        trace_enter("alloc_type");

    // 2. Bump-allocate 176 bytes from the current region
    void *raw = region_alloc(dword_126EC90, 176);

    // 3. Set up IL prefix (16 bytes before the returned pointer)
    // raw[0..7] = TU-copy address (0 if not in copy mode)
    // raw[8..15] = next pointer (0)
    if (!dword_106BA08) {
        ++qword_126F7C0;         // orphan prefix count
        *(raw + 0) = 0;          // TU-copy addr
    }
    ++qword_126F750;             // IL entry count
    *(raw + 8) = 0;              // next pointer

    // 4. Set prefix flags byte
    byte flags = 1;              // bit 0 = allocated
    if (!dword_106BA08)
        flags |= 2;             // bit 1 = file-scope
    if (dword_126E5FC & 1)
        flags |= 8;             // bit 3 = C++ mode
    *(raw + 8) = flags;

    // 5. Increment type counter
    ++qword_126F8E0;

    // 6. Copy 96-byte common IL header
    type_t *result = raw + 16;
    memcpy(result, &xmmword_126F6A0, 96);

    // 7. Dispatch to set_type_kind
    set_type_kind(result, type_kind);

    // 8. Optional debug trace
    if (dword_126EFC8)
        trace_leave();

    return result;
}
```

### set_type_kind: The 22-Way Dispatch

`set_type_kind` (`sub_5E2E80`) writes the kind byte and allocates any required supplement structure:

```c
void set_type_kind(type_t *type, int kind) {
    type->type_kind = kind;      // byte at +132

    switch (kind) {
    case 0:  case 1:             // tk_none, tk_void
    case 17: case 18:            // pack expansions
    case 19: case 20: case 21:   // auto, rvalue_ref, nullptr_t
        break;                   // no supplement needed

    case 2:                      // tk_integer
        type->referenced_type = 5;  // default integer subkind
        type->supplement_ptr = alloc_permanent(32);
        ++qword_126F8E8;        // integer supplement counter
        // Store source position at supplement+16
        break;

    case 3: case 4: case 5:     // tk_float, tk_double, tk_long_double
        type->referenced_type = 2;  // format byte
        break;

    case 6:                      // tk_pointer
        type->supplement_ptr = 0;   // zero class-pointer field
        type->secondary_data = 0;
        break;

    case 7:                      // tk_routine (function type)
        type->supplement_ptr = alloc_permanent(64);
        ++qword_126F958;        // routine supplement counter
        // Initialize calling convention bitfield at supplement+32
        break;

    case 8:                      // tk_array
        // Zero size and flags fields
        break;

    case 9: case 10: case 11:   // tk_struct, tk_class, tk_union
        type->supplement_ptr = alloc_permanent(208);
        ++qword_126F948;        // class supplement counter
        init_class_type_supplement_fields(type->supplement_ptr);
        type->supplement_ptr->class_kind = kind;  // at supplement+100
        break;

    case 12:                     // tk_typeref
        type->supplement_ptr = alloc_permanent(56);
        ++qword_126F8F0;        // typeref supplement counter
        break;

    case 13:                     // tk_pointer_to_member
        // Zero member/class fields
        break;

    case 14:                     // tk_template_param
        type->supplement_ptr = alloc_permanent(40);
        ++qword_126F8F8;        // template param supplement counter
        break;

    case 15: case 16:           // tk_typeof, tk_decltype
        // Zero expression pointer fields
        break;

    default:
        internal_error("set_type_kind: bad type kind");
    }
}
```

## Qualified Type Construction: The MRU Cache

When the compiler needs a `const int` given an `int`, it calls `f_make_qualified_type` (`sub_5D64F0`). This function is called extremely frequently -- every variable declaration, function parameter, and expression type computation may need cv-qualified variants. EDG optimizes this with a move-to-front (MRU) linked list cache on each type node.

```c
type_t *f_make_qualified_type(type_t *base_type, int qualifiers) {
    // qualifiers bitmask: bit 0 = const, bit 1 = volatile,
    //                     bit 2 = restrict, bits 3-6 = address space

    // 1. Array special case: cv-qualify the element type, not the array
    if (base_type->type_kind == 8) {   // array
        type_t *elem = base_type->referenced_type;
        type_t *qual_elem = f_make_qualified_type(elem, qualifiers);
        return rebuild_array_type(base_type, qual_elem);
    }

    // 2. Strip existing qualifiers that already match
    int existing = get_cv_qualifiers(base_type) & 0x7F;
    int needed = qualifiers & ~existing;
    if (needed == 0)
        return base_type;           // already qualified as requested

    // 3. Search the MRU cache at base_type->qualified_chain (+112)
    type_t *prev = NULL;
    type_t *cur = base_type->qualified_chain;
    while (cur) {
        if (cur->type_kind == 12 &&             // must be typeref
            (cur->class_flags_1 & 0x7F) == qualifiers) {
            // Cache hit -- move to front if not already there
            if (prev) {
                prev->next = cur->next;
                cur->next = base_type->qualified_chain;
                base_type->qualified_chain = cur;
            }
            return cur;
        }
        prev = cur;
        cur = cur->next;
    }

    // 4. Cache miss -- allocate new qualified type
    type_t *qual = alloc_type(12);              // tk_typeref
    qual->referenced_type = base_type;          // +144 = underlying type
    qual->class_flags_1 = qualifiers & 0x7F;    // +161 = qualifier bits
    setup_type_node(qual);                       // sub_5B3DE0

    // 5. Insert at head of cache list
    qual->next = base_type->qualified_chain;
    base_type->qualified_chain = qual;

    return qual;
}
```

The MRU optimization is critical because type construction is highly skewed: `const T` is needed far more often than `volatile const restrict T`. By moving the most recently matched qualified variant to the front of the chain, subsequent lookups for the same qualification find it immediately.

The same MRU pattern appears in `ptr_to_member_type_full` (`sub_5DB220`), which caches pointer-to-member types on the member type's qualification chain at `+112`.

### CV-Qualifier Bitmask

| Bit | Mask | Qualifier |
|---|---|---|
| 0 | `0x01` | `const` |
| 1 | `0x02` | `volatile` |
| 2 | `0x04` | `__restrict` |
| 3--6 | `0x78` | Address space qualifier (CUDA/OpenCL) |

The 7-bit mask (`& 0x7F`) at offset `+161` of a typeref node encodes the full cv-qualification. `get_cv_qualifiers` (`sub_7A9E70`, 319 callers) accumulates these bits by chasing the typedef chain:

```c
int get_cv_qualifiers(type_t *type) {
    int quals = 0;
    while (type->type_kind == 12) {     // chase typedefs
        quals |= type->class_flags_1 & 0x7F;
        type = type->referenced_type;
    }
    return quals;
}
```

## Type Comparison

`sub_7AA150` (`types_are_identical`, 636 lines) is the main structural type comparison function. It handles all 22 type kinds with recursive descent into component types. The algorithm:

1. Chase typedefs on both operands to reach canonical types.
2. If pointer-equal after chasing, return true (the common fast path).
3. If kinds differ, return false.
4. Dispatch on kind:
   - Integer (kind 2): Compare integer subkind values.
   - Pointer (kind 6): Recursively compare pointed-to types.
   - Array (kind 8): Compare bounds and recursively compare element types.
   - Function (kind 7): Compare return type, then parameter-by-parameter.
   - Class (kind 9/10/11): Pointer equality only (nominal typing).
   - Template param (kind 14): Compare parameter index and depth.
   - Pointer-to-member (kind 13): Compare both class and member types.

The comparison is structural for most types but nominal for classes. Two distinct `struct Foo` definitions in different scopes are different types even if they have identical members.

### Cross-TU Type Correspondence

For relocatable device code (RDC) compilation, cudafe++ must match types across translation units. `sub_7B2260` (`types_are_equivalent_for_correspondence`, 688 lines) performs a deep structural comparison that tolerates certain cross-TU differences (different typedef layers, different source positions) while requiring identical essential structure.

## Type Construction Functions

Beyond `f_make_qualified_type`, several other type construction functions use the same cache pattern:

| Address | Function | Creates | Cache Location |
|---|---|---|---|
| `0x5D64F0` | `f_make_qualified_type` | `const T`, `volatile T`, etc. | Type `+112` chain |
| `0x5D6770` | `make_vector_type` | `__attribute__((vector_size(N)))` T | Allocated fresh |
| `0x5D68E0` | `character_type` | `char[N]` string literal types | Hash table at `qword_126F2F8` (81-slot per kind) |
| `0x5DB220` | `ptr_to_member_type_full` | `T Class::*` | Member type `+112` chain (MRU) |
| `0x7AB9B0` | `construct_function_type` | `R(Args...)` | Allocated fresh (423 lines) |
| `0x7A6320` | `make_cv_combined_type` | Combines cv-quals from two types | Allocated fresh |

### Character Type Cache

String literal types (`char[5]`, `wchar_t[12]`, etc.) are extremely common in C++ programs. `character_type` (`sub_5D68E0`) uses a hash-table cache at `qword_126F2F8` with 81 slots per character kind (5 kinds: `char`, `wchar_t`, `char8_t`, `char16_t`, `char32_t`), covering array sizes 0 through 80. For sizes exceeding 80, no caching is performed and a fresh array type is allocated every time.

## Class Layout: do_class_layout

`sub_662670` (`do_class_layout`, 2,548 lines) is the most complex function in the type system. It implements the Itanium C++ ABI class layout algorithm with GNU extensions, MSVC compatibility mode, and CUDA-specific adjustments. It is called exactly once per class definition from `sub_442680` (class definition processing).

### What do_class_layout Computes

For each class/struct/union, the function determines:

- **`sizeof`**: Total class size including padding.
- **`alignof`**: Required alignment, incorporating `alignas`, `__attribute__((aligned))`, and `#pragma pack`.
- **Member offsets**: Byte offset of each non-static data member.
- **Base class offsets**: Byte offset of each non-virtual base class subobject.
- **Virtual base offsets**: Byte offset of each virtual base class subobject (stored in the vtable).
- **Vtable pointer placement**: Where `_vptr` is placed (offset 0 for primary base, elsewhere for secondary).
- **Empty base optimization (EBO)**: Whether empty base classes can share address with data members.
- **Bit-field packing**: How bit-fields are packed into allocation units.
- **Tail padding reuse**: Whether derived classes can place members in base class tail padding (non-POD only).

### Pseudocode: Itanium ABI Layout

```c
void do_class_layout(type_t *class_type) {
    class_info_t *info = class_type->supplement_ptr;
    int sizeof_val = 0;
    int alignof_val = 1;
    int dsize = 0;          // data size (excludes tail padding)

    // PHASE 1: Lay out non-virtual base classes
    for (base_t *base = info->base_list; base; base = base->next) {
        if (base->is_virtual)
            continue;       // defer virtual bases

        int base_size = base->type->size_info;
        int base_align = base->type->alignment;

        // Empty base optimization
        if (is_empty_class(base->type)) {
            int offset = 0;
            while (empty_base_conflict(class_type, base->type, offset))
                offset += base_align;
            set_base_class_offset(base, offset);
            // sizeof may not increase for empty bases
        } else {
            // Align dsize up to base alignment
            dsize = ALIGN_UP(dsize, base_align);
            set_base_class_offset(base, dsize);
            dsize += base_size;
        }

        alignof_val = MAX(alignof_val, base_align);
    }

    // PHASE 2: Place vptr if needed
    if (class_has_virtual_functions(class_type) &&
        !has_primary_base_with_vptr(class_type)) {
        // vptr at current offset (usually 0)
        dsize = ALIGN_UP(dsize, POINTER_ALIGN);
        dsize += POINTER_SIZE;
        alignof_val = MAX(alignof_val, POINTER_ALIGN);
    }

    // PHASE 3: Lay out non-static data members
    for (field_t *field = info->first_field; field; field = field->next) {
        int field_align = alignment_of_field_full(field);
        int field_size = field->type->size_info;

        if (field->is_bitfield) {
            align_offsets_for_bit_field(field, &dsize, &alignof_val);
            continue;
        }

        dsize = ALIGN_UP(dsize, field_align);

        // Warn if field lands in tail padding of a base class
        warn_if_offset_in_tail_padding(class_type, dsize, field);

        field->offset = dsize;
        dsize += field_size;
        alignof_val = MAX(alignof_val, field_align);
    }

    // PHASE 4: Lay out virtual base classes
    for (base_t *base = info->base_list; base; base = base->next) {
        if (!base->is_virtual)
            continue;

        int base_align = base->type->alignment;
        if (is_empty_class(base->type)) {
            int offset = sizeof_val;
            while (subobject_conflict(class_type, base->type, offset))
                offset += base_align;
            set_virtual_base_class_offset(base, offset);
        } else {
            sizeof_val = ALIGN_UP(sizeof_val > dsize ? sizeof_val : dsize,
                                  base_align);
            set_virtual_base_class_offset(base, sizeof_val);
            sizeof_val += base->type->size_info;
        }
    }

    // PHASE 5: Finalize
    sizeof_val = MAX(sizeof_val, dsize);
    sizeof_val = ALIGN_UP(sizeof_val, alignof_val);
    if (sizeof_val == 0)
        sizeof_val = 1;     // C++ requires sizeof >= 1

    compute_empty_class_bit(class_type);
    trailing_base_does_not_affect_gnu_size(class_type);
    check_explicit_alignment(class_type);

    class_type->size_info = sizeof_val;
    class_type->alignment = alignof_val;

    // Debug: dump_layout() if debug flag set
    if (dword_126EFC8)
        dump_layout(class_type);
}
```

### Key Sub-Functions

| Address | Function | Purpose |
|---|---|---|
| `0x65EA50` | `trailing_base_does_not_affect_gnu_size` | Checks if trailing empty base affects GNU-compatible size vs dsize |
| `0x65EE70` | `empty_base_conflict` | Self-recursive: detects two empty bases of same type at same address |
| `0x65F410` | `increment_field_offsets` | Advances offset counters; warns about tail-padding overlap |
| `0x65F9F0` | `last_user_field_of` | Finds last user-declared (non-compiler-generated) field |
| `0x65FC20` | `subobject_conflict` | Generalizes empty_base_conflict to all subobjects |
| `0x6610B0` | `set_base_class_offsets` | Assigns offsets to non-virtual base class subobjects |
| `0x6614A0` | `set_virtual_base_class_offset` | Assigns offsets to virtual base class subobjects |
| `0x6621E0` | `alignment_of_field_full` | Computes field alignment considering packed, aligned, pragma pack |

### Empty Base Optimization

The EBO is one of the most subtle parts of C++ layout. The C++ standard requires that two distinct subobjects of the same type have different addresses. But empty base classes (no data members, no virtual functions, all bases empty) can be placed at offset 0 without consuming space -- unless another subobject of the same type already occupies that address.

`empty_base_conflict` (`sub_65EE70`, 240 lines) is self-recursive: it walks the entire base class hierarchy checking for address collisions. When a conflict is detected, the layout engine advances the offset by the base's alignment until no conflict exists.

### Alignment Computation

`alignment_of_field_full` (`sub_6621E0`, 193 lines) computes the effective alignment of a data member considering all alignment modifiers in priority order:

1. Natural alignment of the field's type.
2. `__attribute__((aligned(N)))` -- increases alignment.
3. `__attribute__((packed))` -- reduces alignment to 1.
4. `#pragma pack(N)` -- caps alignment at N.
5. `__declspec(align(N))` -- MSVC mode alignment.

The interaction between these modifiers follows complex ABI rules. For example, `#pragma pack(4)` on a struct with a `double` member reduces the `double`'s alignment from 8 to 4, but `__attribute__((aligned(16)))` on the same member overrides the pack to 16.

## Type Trait Evaluation

`sub_7BDCB0` (`evaluate_type_trait`, 510 lines) implements the compiler built-in type traits: `__is_trivially_copyable`, `__is_constructible`, `__has_unique_object_representations`, `__is_aggregate`, `__is_empty`, etc. These are dispatched via a switch on trait ID and return boolean results by inspecting the class type supplement flags and calling recursive property checks.

## Type Deduction

`sub_7B9670` (`deduce_template_argument_type`, 459 lines) handles template argument deduction from function arguments to template parameters. This is separate from the template engine's `substitute_in_type` (`sub_7BCDE0`, 800 lines), which performs the reverse operation: given concrete template arguments, produce the substituted type.

## Global Type Singletons

Several frequently-used types are cached as global pointers to avoid repeated allocation:

| Global | Type |
|---|---|
| `qword_126E5E0` | `void` type |
| `qword_126F2F0` | `void` type (duplicate reference) |
| `qword_126F1A0` | `std::source_location::__impl` (cached on first use) |

## Statistics Tracking

Every type-related allocation increments a per-kind counter. `print_trans_unit_statistics` (`sub_7A45A0`) dumps these counters via `fprintf`:

| Counter | What it counts | Per-entry size |
|---|---|---|
| `qword_126F8E0` | Type nodes allocated | 176 B |
| `qword_126F8E8` | Integer type supplements | 32 B |
| `qword_126F958` | Routine type supplements | 64 B |
| `qword_126F948` | Class type supplements | 208 B |
| `qword_126F8F0` | Typeref supplements | 56 B |
| `qword_126F8F8` | Template param supplements | 40 B |
| `qword_126F280` | Pointer-to-member types constructed | -- |

## CUDA-Specific Type Extensions

### Address Space Qualifiers

CUDA's `__shared__`, `__constant__`, and `__device__` memory spaces are represented as address-space qualifiers in the cv-qualifier bitmask (bits 3--6 at `+161`). The attribute kind values `{1, 6, 11, 12}` (bitmask `0x1842`) are checked in `compare_attribute_specifiers` (`sub_7A5E10`) to detect incompatible address-space qualified typedefs.

### Feature Usage Tracking

`record_type_features_used` (`sub_7A4F10`) records GPU feature requirements based on types encountered:

- `_BitInt` types (integer subkind 11/12): sets bit 0 of `byte_12C7AFC`
- `__float128` / `__bf16` types: sets bit 2
- Bit-fields: sets bit 1
- Class types: copies feature bits from `+164`

This information feeds into architecture gating, ensuring that code using `_BitInt(128)` targets a GPU architecture that supports it.

### Constexpr Type Size Limits

The constexpr interpreter (`sub_628DE0`, `f_value_bytes_for_type`) enforces a 64 MB limit (`0x4000000` bytes) on types used in constexpr evaluation. This prevents compile-time memory exhaustion from expressions like `constexpr std::array<char, 1'000'000'000> x{};`.

## Function Map

| Address | Lines | Function | Source |
|---|---|---|---|
| `0x5D64F0` | 340 | `f_make_qualified_type` | il.c |
| `0x5DB220` | 63 | `ptr_to_member_type_full` | il.c |
| `0x5E2E80` | -- | `set_type_kind` | il_alloc.c |
| `0x5E3D40` | -- | `alloc_type` | il_alloc.c |
| `0x65EA50` | 105 | `trailing_base_does_not_affect_gnu_size` | layout.c |
| `0x65EE70` | 240 | `empty_base_conflict` | layout.c |
| `0x65FC20` | 271 | `subobject_conflict` | layout.c |
| `0x6610B0` | 196 | `set_base_class_offsets` | layout.c |
| `0x6614A0` | 204 | `set_virtual_base_class_offset` | layout.c |
| `0x6621E0` | 193 | `alignment_of_field_full` | layout.c |
| `0x662670` | 2548 | `do_class_layout` | layout.c |
| `0x7A4B40` | -- | `ttt_is_type_with_no_name_linkage` | types.c |
| `0x7A4F10` | -- | `record_type_features_used` | types.c |
| `0x7A5E10` | -- | `compare_attribute_specifiers` | types.c |
| `0x7A6260` | -- | `type_has_flexible_array_or_vla` | types.c |
| `0x7A6320` | -- | `make_cv_combined_type` | types.c |
| `0x7A68F0`--`0x7A9F90` | -- | 130 leaf query functions | types.c |
| `0x7AA150` | 636 | `types_are_identical` | types.c |
| `0x7AB9B0` | 423 | `construct_function_type` | types.c |
| `0x7AE680` | 541 | `adjust_type_for_templates` | types.c |
| `0x7B2260` | 688 | `types_are_equivalent_for_correspondence` | types.c |
| `0x7B3400` | 905 | `standard_conversion_sequence` | types.c |
| `0x7B5210` | 441 | `require_complete_type` | types.c |
| `0x7B6350` | 1107 | `compute_type_layout` | types.c |
| `0x7B7750` | 784 | `compute_class_properties` | types.c |
| `0x7B9670` | 459 | `deduce_template_argument_type` | types.c |
| `0x7BDCB0` | 510 | `evaluate_type_trait` | types.c |
| `0x7BF630` | 348 | `format_type_for_diagnostic` | types.c |
| `0x7C02A0` | -- | `compatible_ms_bit_field_container_types` | types.c |
