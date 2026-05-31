# Type Node

The type node is the fundamental type representation in EDG's intermediate language (IL). Every C++ type -- from `int` to `const volatile std::vector<std::pair<int, float>>*&` -- is represented as a 176-byte type node allocated by `alloc_type` (`sub_5E3D40` in `il_alloc.c`). Type nodes form the backbone of the type system: every variable, field, routine, expression, and parameter carries a pointer to its type node. There are approximately 4,448 call sites across 128 type-query leaf functions in `types.c` alone.

The type node is a discriminated union. The `type_kind` byte at offset `+132` selects one of 22 type kinds (0-21), and certain type kinds trigger allocation of a separate supplementary structure (class_type_supplement, routine_type_supplement, etc.) that hangs off the type node at offset `+152`. The base 176 bytes contain the common header shared with all IL entries (96 bytes), the type discriminator, qualifier flags, size/alignment, and type-kind-specific inline payload fields.

## Key Facts

| Property | Value |
|---|---|
| Allocation size | 176 bytes (IL entry size) |
| Allocator | `sub_5E3D40` (`alloc_type`), `il_alloc.c` |
| Kind setter | `sub_5E2E80` (`set_type_kind`), 22 cases |
| In-place reinit | `sub_5E3590` (`init_type_fields`), no allocation |
| Counter global | `qword_126F8E0` |
| Stats label | `"type"` (176 bytes each) |
| Region | file-scope only (always `dword_126EC90`) |
| Type query functions | 128 leaf functions in `types.c` (`0x7A4940`-`0x7C02A0`) |
| Most-called query | `is_class_or_struct_or_union_type` (`sub_7A8A30`, 407 call sites) |
| Source files | `il_alloc.c` (allocation), `types.c` (queries/construction) |

## Memory Layout

### Raw Allocation vs Returned Pointer

Like all IL entries, the raw allocation includes a 16-byte prefix that is hidden from the returned pointer. The allocator returns `raw + 16`, so all field offsets documented below are relative to this returned pointer.

```text
Raw allocation (192 bytes total):
  raw+0   [8 bytes]   TU copy address (zeroed, ptr[-2])
  raw+8   [8 bytes]   next-in-list link (zeroed, ptr[-1])
  raw+16  [176 bytes] type node body (ptr+0 onward)

Prefix flags byte at ptr[-8]:
  bit 0 (0x01)  allocated
  bit 1 (0x02)  file_scope
  bit 3 (0x08)  language_flag (C++ mode)
  bit 7 (0x80)  keep_in_il (CUDA device marking)
```

### Complete Field Map

The 176 bytes of the type node body divide into three regions: the common IL header (bytes 0-95), the type discriminator and qualifier zone (bytes 96-135), and the type-kind-specific payload (bytes 136-175).

```text
Offset  Size  Field                  Description
------  ----  -----                  -----------
+0      96    common_il_header       Shared with all IL entry types (see below)
+96     24    (continuation of       Source position, declaration metadata,
              common header area)    scope/name linkage -- varies by IL kind
+120    8     type_size              Computed size of this type in bytes
+128    4     alignment              Required alignment (bytes)
+132    1     type_kind              Type discriminator (0-21, see table below)
+133    1     type_flags_1           bit 5 = is_dependent
+134    1     type_qual_flags        bit 0 = const, bit 1 = volatile
                                    (cleared by sub_5E3580: *(a1+134) &= 0xFC)
+135    1     (padding/reserved)
+136    8     (reserved/varies)      Kind-dependent inline storage
+144    8     referenced_type        For tk_pointer/tk_reference/tk_typedef:
                                      -> pointed-to/referenced/aliased type
                                    For tk_pointer_to_member: -> class type
                                    For tk_function: return type enum (2=void)
                                    For tk_integer (enum): underlying type ptr
+145    1     enum_flags             For tk_integer:
                                      bit 3 = scoped_enum
                                      bit 4 = is_bit_int_capable
+146    1     extended_int_flags     For tk_integer:
                                      bit 2 = is_BitInt
+147    1     (padding)
+148    1     (varies by kind)       For tk_class/struct/union: access default
                                    set_type_kind initializes to 1
+149    1     kind_init_byte         Flags initialized during set_type_kind
+150    2     (cleared by init)      Zeroed by init_type_fields_and_set_kind
+152    8     supplement_ptr         For tk_class/struct/union: -> class_type_supplement
                                    For tk_routine: -> routine_type_supplement
                                    For tk_integer: -> integer_type_supplement
                                    For tk_typeref: -> typeref_type_supplement
                                    For tk_template_param: -> templ_param_supplement
                                    For tk_pointer: member_pointer_flag
                                      bit 0 = is_member_pointer
                                      bit 1 = extended_member_ptr
                                    For tk_array: bound expression pointer
+153    1     array_flags            For tk_array:
                                      bit 0 = dependent_bound
                                      bit 1 = is_VLA
                                      bit 5 = star_modifier
+154    6     (varies)
+160    8     typedef_attr_kind      For tk_typeref: attribute kind value
                                    For tk_array: numeric bound value
+161    1     class_flags_1          For tk_class/struct/union:
                                      bit 0 = is_local_class
                                      bit 2 = no_name_linkage
                                      bit 4 = is_template_class
                                      bit 5 = is_anonymous
                                      bit 7 = has_nested_types
+162    1     typedef_flags          For tk_typeref:
                                      bit 0 = is_elaborated
                                      bit 6 = is_attributed
                                      bit 7 = has_addr_space
+163    1     class_flags_2          For tk_class/struct/union:
                                      bit 0 = extern_template_inst
                                      bit 3 = alignment_set
                                      bit 4 = is_scoped (for union)
+164    2     feature_flags          Target feature requirements
                                    (copied to byte_12C7AFC by
                                     record_type_features_used)
+166    2     (reserved)
+168    4     alignment_attr         Explicit alignment / packed attribute
+172    4     (tail padding)
```

### Common IL Header (Bytes 0-95)

The first 96 bytes are copied verbatim from the template globals (`xmmword_126F6A0` through `xmmword_126F6F0`) during allocation. This template captures the current source file, line, and column position, and is refreshed as the parser advances. The header contains:

```text
xmmword_126F6A0  [+0..+15]    scope/class pointer, name pointer (zeroed)
xmmword_126F6B0  [+16..+31]   declaration metadata (high qword zeroed)
xmmword_126F6C0  [+32..+47]   reserved (zeroed)
xmmword_126F6D0  [+48..+63]   reserved (zeroed)
xmmword_126F6E0  [+64..+79]   source position (from qword_126EFB8)
xmmword_126F6F0  [+80..+95]   low word = 4 (access default), high zeroed
qword_126F700    [+96..+103]  current source file reference
```

The source position at bytes `+64..+79` allows error messages and diagnostics to reference the exact declaration point for each type.

## Type Kind Enumeration

The type kind byte at offset `+132` holds one of 22 values. The `set_type_kind` function (`sub_5E2E80`, 279 lines, `il_alloc.c:2334`) dispatches on this value to initialize type-kind-specific fields and allocate supplement structures where needed.

| Value | Name | C++ Constructs | Supplement | Payload |
|---|---|---|---|---|
| 0 | `tk_none` | Placeholder / uninitialized | None | no-op |
| 1 | `tk_void` | `void` | None | no-op |
| 2 | `tk_integer` | `bool`, `char`, `short`, `int`, `long`, `long long`, `__int128`, `_BitInt(N)`, all `unsigned` variants, `wchar_t`, `char8_t`, `char16_t`, `char32_t`, enumerations | 32-byte `integer_type_supplement` | `+144`=5 (default) |
| 3 | `tk_float` | `float`, `_Float16`, `__bf16` | None | format byte = 2 |
| 4 | `tk_double` | `double` | None | format byte = 2 |
| 5 | `tk_long_double` | `long double`, `__float128` | None | format byte = 2 |
| 6 | `tk_pointer` | `T*`, member pointers (bit 0 of `+152`) | None | 2 payload fields zeroed |
| 7 | `tk_routine` | Function type `int(int, float)` | 64-byte `routine_type_supplement` | calling convention, params init |
| 8 | `tk_array` | `T[N]`, `T[]`, VLAs | None | size+flags zeroed |
| 9 | `tk_struct` | `struct S` | 208-byte `class_type_supplement` | kind stored at supplement+100 |
| 10 | `tk_class` | `class C` | 208-byte `class_type_supplement` | kind stored at supplement+100 |
| 11 | `tk_union` | `union U` | 208-byte `class_type_supplement` | kind stored at supplement+100 |
| 12 | `tk_typeref` | `typedef`, `using` alias, elaborated type specifiers | 56-byte `typeref_type_supplement` | -- |
| 13 | `tk_typeof` | `typeof(expr)`, `__typeof__` | None | zeroed |
| 14 | `tk_template_param` | `typename T`, template type/non-type/template parameters | 40-byte `templ_param_supplement` | -- |
| 15 | `tk_decltype` | `decltype(expr)` | None | zeroed |
| 16 | `tk_pack_expansion` | `T...` (parameter pack expansion) | None | zeroed |
| 17 | `tk_pack_expansion_alt` | Alternate pack expansion form | None | no-op |
| 18 | `tk_auto` | `auto`, `decltype(auto)` | None | no-op |
| 19 | `tk_rvalue_reference` | `T&&` (rvalue reference) | None | no-op |
| 20 | `tk_nullptr_t` | `std::nullptr_t` | None | no-op |
| 21 | `tk_reserved_21` | Reserved / unused | None | no-op |

**Reconciling set_type_kind with types.c query functions**: There is an apparent conflict between the `set_type_kind` dispatch (where case 7 allocates a routine supplement, case 0xD/13 is typeof, case 0xE/14 is template_param) and the `types.c` query function catalog (where `is_reference_type` tests kind==7, `is_pointer_to_member_type` tests kind==13, `is_function_type` tests kind==14). The `set_type_kind` switch is the authoritative source for allocation behavior -- it is a 279-line DEFINITE-confidence function with the embedded error string `"set_type_kind: bad type kind"`. The types.c catalog was reconstructed from runtime query patterns and may reflect a different numbering or the fact that type kind values are reassigned after initial allocation. The table above follows the `set_type_kind` dispatch numbering. The types.c query mappings are documented in the query function catalog below for cross-reference.

### set_type_kind Dispatch Summary

```c
switch (type_kind) {
  case 0, 1, 17..21:   // tk_none, tk_void, alt-pack, auto, rvalue_ref, nullptr, reserved
    break;             // no-op: simple types with no extra state

  case 2:              // tk_integer
    type->+144 = 5;    // default integer subkind
    supplement = alloc_in_file_scope_region(32);   // integer_type_supplement
    ++qword_126F8E8;
    supplement->+16 = source_position;
    type->+152 = supplement;
    break;

  case 3, 4, 5:        // tk_float, tk_double, tk_long_double
    type->format_byte = 2;   // IEEE format indicator
    break;

  case 6:              // tk_pointer
    type->+144 = 0;    // pointed-to type (to be set later)
    type->+152 = 0;    // member-pointer flags (cleared)
    break;

  case 7:              // tk_routine
    supplement = alloc_in_file_scope_region(64);   // routine_type_supplement
    ++qword_126F958;
    init_bitfield_struct(supplement+32);            // calling convention defaults
    type->+152 = supplement;
    break;

  case 8:              // tk_array
    type->+120 = 0;    // array total size (unknown)
    type->+152 = 0;    // bound expression (none)
    type->+153 &= mask; // clear array flags
    type->+160 = 0;    // numeric bound (none)
    break;

  case 9, 10, 11:      // tk_struct, tk_class, tk_union
    supplement = alloc_in_file_scope_region(208);  // class_type_supplement
    ++qword_126F948;
    init_class_type_supplement_fields(supplement);
    supplement->+100 = type_kind;                  // remember which class flavor
    type->+152 = supplement;
    break;

  case 12 (0xC):       // tk_typeref (typedef / using alias)
    supplement = alloc_in_file_scope_region(56);   // typeref_type_supplement
    ++qword_126F8F0;
    type->+152 = supplement;
    break;

  case 13 (0xD):       // tk_typeof
    type->+144 = 0;
    type->+152 = 0;
    break;

  case 14 (0xE):       // tk_template_param
    supplement = alloc_in_file_scope_region(40);   // templ_param_supplement
    ++qword_126F8F8;
    type->+152 = supplement;
    break;

  case 15 (0xF):       // tk_decltype
    type->+144 = 0;
    type->+152 = 0;
    break;

  case 16 (0x10):      // tk_pack_expansion
    type->+144 = 0;
    type->+152 = 0;
    break;

  default:
    internal_error("set_type_kind: bad type kind");
}
type->+132 = type_kind;
```

## Supplement Structures

Five type kinds trigger allocation of a supplementary structure. The supplement pointer lives at type node offset `+152` and points to a separately-allocated block in the file-scope region.

### class_type_supplement (208 bytes)

Allocated for `tk_struct` (kind 9), `tk_class` (kind 10), and `tk_union` (kind 11). This is the richest supplement, carrying the full class definition metadata. Initialized by `init_class_type_supplement_fields` (`sub_5E2D70`, 40 lines) and `init_class_type_supplement` (`sub_5E2C70`, 42 lines).

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | scope_ptr | Pointer to the class scope (288-byte scope node) |
| `+8` | 8 | base_class_list | Head of linked list of base class entries (112 bytes each) |
| `+16` | 8 | friend_decl_list | Head of friend declaration list |
| `+24` | 8 | member_list_head | Member entity list (routines, variables, nested types) |
| `+32` | 8 | nested_type_list | Nested type definitions |
| `+40` | 4 | default_access | 1 = public (struct/union), 2 = private (class) |
| `+44` | 4 | (reserved) | |
| `+48` | 8 | using_decl_list | Using declarations in class scope |
| `+56` | 8 | static_data_members | Static data member list |
| `+64` | 8 | template_info | Template instantiation info (if template class) |
| `+72` | 8 | virtual_base_list | Virtual base class chain |
| `+80` | 4 | (flags) | |
| `+84` | 2 | (reserved) | |
| `+86` | 1 | class_property_flags | bit 0 = has_virtual_bases, bit 3 = has_user_conversion |
| `+88` | 1 | extended_flags | bit 5 = has_flexible_array |
| `+96` | 8 | vtable_ptr | Virtual function table pointer |
| `+100` | 4 | class_kind | Copy of type_kind (9, 10, or 11) |
| `+104` | 8 | destructor_ptr | Pointer to destructor entity |
| `+112` | 8 | copy_ctor_ptr | Copy constructor entity |
| `+120` | 8 | move_ctor_ptr | Move constructor entity |
| `+128` | 8 | (scope chain) | |
| `+136` | 8 | conversion_functions | User-defined conversion operator list |
| `+144` | 8 | befriending_classes | List of classes that befriend this class |
| `+152` | 8 | deduction_guides | Deduction guide list (C++17) |
| `+160` | 8 | (reserved) | |
| `+168` | 8 | (reserved) | |
| `+176` | 4 | vtable_index | Virtual function table index, initialized to -1 (0xFFFFFFFF) |
| `+180` | 4 | (padding) | |
| `+184` | 8 | (reserved) | |
| `+192` | 8 | (reserved) | |
| `+200` | 8 | (reserved) | |

Counter: `qword_126F948`, stats label `"class type supplement"`.

### routine_type_supplement (64 bytes)

Allocated for `tk_routine` (kind 7) by `set_type_kind`. Encodes the function signature metadata.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | param_type_list | Head of parameter type linked list (80 bytes each) |
| `+8` | 8 | return_type | Return type pointer |
| `+16` | 8 | exception_spec | Exception specification pointer (16 bytes) |
| `+24` | 8 | (reserved) | |
| `+32` | 4 | calling_convention | Calling convention bitfield (initialized by set_type_kind) |
| `+36` | 4 | param_count | Number of parameters |
| `+40` | 4 | flags | Variadic, noexcept, trailing-return, etc. |
| `+44` | 4 | (reserved) | |
| `+48` | 8 | (reserved) | |
| `+56` | 8 | (reserved) | |

Counter: `qword_126F958`, stats label `"routine type supplement"`.

Each parameter in the `param_type_list` is an 80-byte `param_type` node (allocated by `alloc_param_type`, `sub_5E1D40`, free-list recycled from `qword_126F678`). Parameter types form a singly-linked list through their `+0` field.

### integer_type_supplement (32 bytes)

Allocated for `tk_integer` (kind 2). Represents the properties of integral and enumeration types.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 4 | integer_subkind | Subkind identifier (values 1-12, default 5) |
| `+4` | 4 | bit_width | Width in bits (for `_BitInt(N)`) |
| `+8` | 4 | signedness | 0=unsigned, 1=signed (lookup via `byte_E6D1B0`) |
| `+12` | 4 | (reserved) | |
| `+16` | 8 | source_position | Source position at allocation time |
| `+24` | 8 | underlying_type | For enums: pointer to the underlying integer type |

Counter: `qword_126F8E8`, stats label `"integer type supplement"`.

The `integer_subkind` field distinguishes between the various integer types. Known subkind values from type query analysis:

| Subkind | Type |
|---|---|
| 1-10 | Standard integer types (`bool` through `long long`) |
| 11 | `_Float16` / extended |
| 12 | `__int128` / extended |

### typeref_type_supplement (56 bytes)

Allocated for `tk_typeref` (kind 12 = 0xC in `set_type_kind`). Links the typedef/using-alias to its referenced declaration and tracks elaborated type specifier properties.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | referenced_decl | The declaration this typedef names |
| `+8` | 8 | original_type | The original type before typedef expansion |
| `+16` | 8 | scope_ptr | Scope in which the typedef was declared |
| `+24` | 8 | (reserved) | |
| `+32` | 8 | attribute_info | Attribute specifier chain |
| `+40` | 8 | template_info | Template argument list (for alias templates) |
| `+48` | 8 | (reserved) | |

Counter: `qword_126F8F0`, stats label `"typeref type supplement"`.

The elaborated type specifier kind is encoded in `type_node+162`:
- bit 0: is_elaborated (uses `struct`/`class`/`union`/`enum` keyword)
- bit 6: is_attributed (carries `[[...]]` attributes)
- bit 7: has_addr_space (CUDA address space attribute)

The constant `0x18C2` (= bits {1,6,7,11,12}) is used as a bitmask in `is_incomplete_type_deep` (`sub_7A6580`) to identify the set of elaborated type specifier kinds.

### templ_param_supplement (40 bytes)

Allocated for `tk_template_param` (kind 14 = 0xE in `set_type_kind`). Represents a template type parameter (`typename T`), non-type template parameter, or template template parameter.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 4 | param_index | Zero-based index in the template parameter list |
| `+4` | 4 | param_depth | Nesting depth (0 for outermost template) |
| `+8` | 4 | param_kind | 0=type, 1=non-type, 2=template-template |
| `+12` | 4 | (reserved) | |
| `+16` | 8 | constraint | Associated constraint expression (C++20 concepts) |
| `+24` | 8 | default_arg | Default template argument (type or expression) |
| `+32` | 8 | (reserved) | |

Counter: `qword_126F8F8`, stats label `"templ param supplement"`.

## Type Qualifier Encoding

CV-qualifiers are not stored as separate type nodes (unlike some compiler designs). Instead, they are encoded as bit flags within the type node itself. The primary qualifier storage is at offset `+134`:

```text
Byte at type+134 (type_qual_flags):
  bit 0 (0x01)   const
  bit 1 (0x02)   volatile
```

The function `clear_type_qualifier_bits` (`sub_5E3580`) performs `*(a1+134) &= 0xFC` to strip both const and volatile.

Additional qualifier information is accessed through the prefix flags byte at `ptr[-8]`:
- bit 5 (0x20): `__restrict` qualifier (`has_restrict_qualifier`, `sub_7A7850`)
- bit 6 (0x40): `volatile` qualifier duplicate (`has_volatile_qualifier`, `sub_7A7890`)

The function `get_cv_qualifiers` (`sub_7A9E70`, 319 call sites) accumulates cv-qualifier bits by walking through typedef chains, applying a `& 0x7F` mask to collect all qualifier bits from each layer.

## Type Query Function Catalog

The `types.c` file (address range `0x7A4940`-`0x7C02A0`) contains approximately 250 functions. Of these, 128 are tiny leaf functions that query type node properties. They follow a canonical pattern:

```c
// Canonical type query pattern
bool is_<property>_type(type_node *type) {
    while (type->type_kind == 12)        // skip typedefs
        type = type->referenced_type;
    return type->type_kind == <expected>;  // or flag check
}
```

### Most-Referenced Query Functions

Sorted by call site count across the entire binary:

| Callers | Function | Address | Test |
|---|---|---|---|
| 407 | `is_class_or_struct_or_union_type` | `0x7A8A30` | kind in {9, 10, 11} |
| 389 | `type_pointed_to` | `0x7A9910` | kind==6, return `+144` |
| 319 | `get_cv_qualifiers` | `0x7A9E70` | accumulate qualifier bits (& 0x7F) |
| 299 | `is_dependent_type` | `0x7A6B60` | bit 5 of byte `+133` |
| 243 | `is_object_pointer_type` | `0x7A7630` | kind==6 && !(bit 0 of `+152`) |
| 221 | `is_array_type` | `0x7A8370` | kind==8 |
| 199 | `is_member_pointer_or_ref` | `0x7A7B30` | kind==6 && (bit 0 of `+152`) |
| 185 | `is_reference_type` | `0x7A6AC0` | kind==7 |
| 169 | `is_function_type` | `0x7A8DC0` | kind==14 |
| 140 | `is_void_type` | `0x7A6E90` | kind==1 |
| 126 | `array_element_type` (deep) | `0x7A9350` | strips arrays+typedefs recursively |
| 85 | `is_enum_type` | `0x7A7010` | kind==2 (with scoped check) |
| 82 | `is_integer_type` | `0x7A71B0` | kind==2 |
| 77 | `is_member_pointer_flag` | `0x7A7810` | kind==6, bit 0 of `+152` |
| 76 | `is_pointer_to_member_type` | `0x7A8D90` | kind==13 |
| 70 | `is_long_double_type` | `0x7A73F0` | kind==5 |
| 62 | `is_scoped_enum_type` | `0x7A70F0` | kind==2, bit 3 of `+145` |
| 56 | `is_rvalue_reference_type` | `0x7A6EF0` | kind==19 |

### Typedef Stripping Functions

Six functions strip typedef layers with different stopping conditions:

| Function | Address | Behavior |
|---|---|---|
| `skip_typedefs` | `0x7A68F0` | Strips all typedef layers, preserves cv-qualifiers |
| `skip_named_typedefs` | `0x7A6930` | Stops at unnamed typedefs |
| `skip_to_attributed_typedef` | `0x7A6970` | Stops at typedef with attribute flag |
| `skip_typedefs_and_attributes` | `0x7A69C0` | Strips both typedefs and attributed-typedefs |
| `skip_to_elaborated_typedef` | `0x7A6A10` | Stops at typedef with elaborated flag |
| `skip_non_attributed_typedefs` | `0x7A6A70` | Stops at typedef with any attribute bits |

### Compound Type Predicates

| Function | Address | Type Kinds |
|---|---|---|
| `is_arithmetic_type` | `0x7A7560` | {2, 3, 4, 5} |
| `is_scalar_type` | `0x7A7BA0` | {2, 3, 4, 5, 6(non-member), 13, 19, 20} |
| `is_aggregate_type` | `0x7A8B40` | {8, 9, 10, 11} |
| `is_floating_point_type` | `0x7A7300` | {3, 4, 5} |
| `is_pack_or_auto_type` | `0x7A7420` | {16, 17, 18} |
| `is_pack_expansion_type` | `0x7A6BE0` | {16, 17} |
| `is_complete_type` | `0x7A6DA0` | Not void, not reference, not incomplete class |

### Duplicate Functions

EDG uses distinct function names for semantic clarity even when the implementation is identical. The compiler does not merge them:

- `0x7A7630` == `0x7A7670` == `0x7A7750` (all: `is_non_member_pointer` / `is_object_pointer_type`)
- `0x7A7B00` == `0x7A7B70` (both: `is_pointer_type`)
- `0x7A78D0` == `0x7A7910` (both: `is_non_const_ref`)

## Type Construction

### alloc_type (`sub_5E3D40`)

The primary type allocation function. Takes a single argument: the type kind. Returns a pointer to a fully-initialized 176-byte type node with the appropriate supplement structure allocated and linked.

Protocol:
1. Trace enter (if `dword_126EFC8` set)
2. Allocate 176 bytes via `region_alloc` in file-scope region
3. Write 16-byte prefix (TU copy addr, next link, flags byte)
4. Increment `qword_126F8E0` (type counter)
5. Copy 96-byte common IL header from template globals
6. Set default access to 1 at `+148`
7. Dispatch `set_type_kind` switch for the requested kind
8. Trace leave (if tracing)
9. Return `raw + 16`

### init_type_fields (`sub_5E3590`)

Re-initializes an existing type node in-place without allocating new memory. Used when a type node needs to change kind after initial allocation (rare but occurs during template instantiation). Copies the template header and dispatches the same `set_type_kind` switch.

### make_cv_combined_type (`sub_7A6320`)

Constructs a new type that combines cv-qualifiers from two source types. Recursively handles arrays (recurses on element type) and pointer-to-member (recurses on member type). Allocates a fresh type node via `alloc_type`, copies the base type via `sub_5DA0A0`, then applies the combined qualifiers via `sub_5D64F0`.

## Type Comparison

### types_are_identical (`sub_7AA150`)

The main type comparison function (636 lines). Handles all 22 type kinds with deep structural comparison. For class types, delegates to the class scope comparison infrastructure. For function types, compares parameter lists, return types, and calling conventions.

### types_are_equivalent_for_correspondence (`sub_7B2260`)

A 688-line function used during multi-TU compilation (CUDA RDC mode). Compares types across translation units for structural equivalence, called from `verify_class_type_correspondence` (`sub_7A00D0`).

### compatible_ms_bit_field_container_types (`sub_7C02A0`)

The last function in `types.c`. Checks if two integer types are compatible for MSVC bit-field container layout rules: both must be kind==2 (integer) with matching size at offset `+120`.

## Pointer and Reference Encoding

Pointers use type kind 6 (`tk_pointer`), with member-pointer status distinguished by flag bits at offset `+152`:

```text
tk_pointer (kind 6):
  +144  referenced_type   The pointed-to / referenced type
  +152  bit 0 = 0         Object pointer (T*)
        bit 0 = 1         Member pointer (T C::*)
        bit 1             Extended member pointer flag
```

The types.c query functions use the following kind tests for pointer/reference classification. Note that the kind values tested here correspond to the types.c query numbering (see reconciliation note in the type kind table):

| Query | Kind Test | +152 Test | Matches |
|---|---|---|---|
| `is_pointer_type` | kind==6 | -- | `T*`, `T C::*` |
| `is_object_pointer_type` | kind==6 | !(bit 0) | `T*` only |
| `is_member_pointer_flag` | kind==6 | bit 0 | `T C::*` only |
| `is_reference_type` | kind==7 | -- | `T&` (lvalue reference) |
| `is_rvalue_reference_type` | kind==19 | -- | `T&&` |
| `is_pointer_to_member_type` | kind==13 | -- | `T C::*` (alternate encoding) |

The `pm_class_type` (`sub_7A9A10`) and `pm_member_type` (`sub_7A99D0`) access `+144` and `+152` respectively for kind-13 nodes.

## Array Type Encoding

Array types (kind 8) store bounds inline in the type node:

```text
tk_array (kind 8):
  +120  type_size          Total array size in bytes (0 if unknown)
  +128  alignment          Element alignment
  +144  element_type       Pointer to the element type node
  +152  bound_expr         Bound expression pointer (for VLAs and dependent)
  +153  array_flags:
          bit 0 = dependent_bound    (template-dependent array size)
          bit 1 = is_VLA             (C99 variable-length array)
          bit 5 = star_modifier      (C99 [*] syntax)
  +160  numeric_bound      Compile-time bound value (when not VLA/dependent)
```

The function `identical_array_type_level` (`sub_7A4E10`, `types.c:6779`) compares two array types by checking the VLA flag, dependent flag, and then either bound expressions (via `sub_5D2160`) or numeric bounds at `+160`.

## Class Type Flags

Class types (kinds 9, 10, 11) carry two flag bytes at offsets `+161` and `+163` in the type node, plus property flags in the class_type_supplement at supplement offset `+86`:

### type_node+161 (class_flags_1)

| Bit | Mask | Field | Query Function |
|---|---|---|---|
| 0 | `0x01` | is_local_class | `is_local_class_type` (`0x7A8EE0`) |
| 2 | `0x04` | no_name_linkage | `ttt_is_type_with_no_name_linkage` (`0x7A4B40`) |
| 4 | `0x10` | is_template_class | `is_template_class_type` (`0x7A8EA0`) |
| 5 | `0x20` | is_anonymous | `is_non_anonymous_class_type` tests !(bit 5) (`0x7A8A90`) |
| 7 | `0x80` | has_nested_types | -- |

### type_node+163 (class_flags_2)

| Bit | Mask | Field | Query Function |
|---|---|---|---|
| 0 | `0x01` | extern_template_inst | `is_empty_class` checks this (`0x7A` range) |
| 3 | `0x08` | alignment_set | -- |
| 4 | `0x10` | is_scoped | `is_scoped_union_type` (`0x7A8B00`) |

### class_type_supplement+86

| Bit | Mask | Field | Query Function |
|---|---|---|---|
| 0 | `0x01` | has_virtual_bases | `class_has_virtual_bases` (`0x7A8BC0`) |
| 3 | `0x08` | has_user_conversion | `class_has_user_conversion` (`0x7A8C00`) |

## Type Size and Layout

`type_size_and_alignment` (`sub_7A8020`, 132 lines) computes the size and alignment of a type for ABI purposes. The computed size is stored at type_node offset `+120` and alignment at `+128`.

For class types, the major layout computation is performed by `compute_type_layout` (`sub_7B6350`, 1107 lines), which handles:
- Base class sub-object placement
- Virtual base class offsets
- Member field alignment and padding
- Bit-field packing (with MSVC compatibility via `compatible_ms_bit_field_container_types`)
- Empty base optimization

## Integration with Other IL Nodes

Type nodes are referenced from virtually every other IL entity:

| IL Node | Offset | Description |
|---|---|---|
| Variable entity (232B) | `+112` | Variable's declared type |
| Field entity (176B) | `+112` | Field's declared type |
| Routine entity (288B) | `+112` | Function's type (kind 7 with routine_type_supplement) |
| Expression node (72B) | `+16` | Expression result type |
| Parameter type (80B) | `+8` | Parameter's declared type |
| Constant (184B) | `+112` | Constant's type |
| Template argument (64B) | `+32` | Type argument value (when kind=0) |

## Allocation Statistics

In a typical CUDA compilation, the stats dump (`sub_5E99D0`) reports type node counts in the thousands. The supplement allocation counts track closely:

```text
type                    176 bytes each   (qword_126F8E0)
integer type supplement  32 bytes each   (qword_126F8E8)
routine type supplement  64 bytes each   (qword_126F958)
class type supplement   208 bytes each   (qword_126F948)
typeref type supplement  56 bytes each   (qword_126F8F0)
templ param supplement   40 bytes each   (qword_126F8F8)
param type               80 bytes each   (qword_126F960, free-list recycled)
```

Type nodes are always allocated in the file-scope region (persistent for the entire translation unit) because types must outlive any individual function body. This contrasts with expression nodes and statements which can be allocated in per-function regions and freed after each function is processed.
