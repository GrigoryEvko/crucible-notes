# Name Mangling

The name mangling subsystem in cudafe++ implements the Itanium C++ ABI name mangling specification, with NVIDIA-specific extensions for CUDA device lambda wrappers and host reference array registration. The mangling pipeline lives in `lower_name.c` (60+ functions spanning `0x69C980`--`0x6AB280`) and produces the `_Z` prefixed symbols that appear in `.int.c` output and PTX. A separate CUDA-aware demangler at `sub_7CABB0` (930 lines, statically linked, not EDG code) reverses the process with extensions for three NVIDIA vendor-specific mangled prefixes: `Unvdl`, `Unvdtl`, and `Unvhdl`. The glue between mangling and CUDA execution spaces is `nv_get_full_nv_static_prefix` in `nv_transforms.c`, which constructs scoped static prefixes for `__global__` template stubs destined for host reference arrays.

## Key Facts

| Property | Value |
|---|---|
| Source file | `lower_name.c` (60+ functions), `nv_transforms.c` (prefix builder) |
| Address range | `0x69C980`--`0x6AB280` (mangling), `0x6BE300` (static prefix) |
| Demangler | `sub_7CABB0` (930 lines, NVIDIA custom, not EDG) |
| ABI standard | Itanium C++ ABI (IA-64), extended with NVIDIA vendor types |
| Operator name table | `sub_69C980` (`mangled_operator_name`), 47 entries |
| Entity mangler | `sub_6A1F00` (`mangle_entity_name`), ~1000 lines |
| Expression mangler | `sub_6A8B10` (`mangled_expression`), ~700 lines |
| Scalable vector mangler | `sub_69CF10` (`mangled_scalable_vector_name`), 170 lines |
| Static prefix builder | `sub_6BE300` (`nv_get_full_nv_static_prefix`), 370 lines |
| Output buffer | `qword_127FCC0` (dynamic buffer with capacity tracking) |
| Demangling mode flag | `qword_126ED90` (non-zero = demangling/diagnostic mode) |
| Compressed mangling flag | `dword_106BC7C` (ABI version control) |
| ABI version selector | `qword_126EF98` (selects vendor-specific vs standard codes) |

## Architecture Overview

Name mangling occurs at two distinct points in the cudafe++ pipeline:

1. **Forward mangling** (IL lowering): EDG's `lower_name.c` converts entity nodes into Itanium ABI mangled names during the IL-to-text code generation phase. The entry point is `mangle_entity_name` (`sub_6A1F00`), which dispatches through 60+ helper functions to handle every C++ construct — namespaces, classes, templates, operators, expressions, lambdas, and vendor-extended types.

2. **Reverse demangling** (diagnostics): A statically linked demangler at `sub_7CABB0` converts mangled names back to human-readable form for error messages and debug output. This demangler is not EDG code — it is NVIDIA's custom implementation that wraps the standard Itanium ABI demangling algorithm with CUDA-specific extensions for device lambda wrapper types.

```text
Entity Node (IL)
  |
  +-- sub_69FF70 (check_mangling_special_cases)
  |     Checks: extern "C", linkage name override, builtin
  |     If special case handled, done.
  |
  +-- sub_6A1F00 (mangle_entity_name)          ~1000 lines
  |     |
  |     +-- sub_69C980 (mangled_operator_name)  47 operators
  |     +-- sub_69E740 (mangle_type_encoding)   type dispatch
  |     +-- sub_6A3B00 (mangle_function_encoding)
  |     +-- sub_6A41A0 (mangle_declaration)
  |     +-- sub_6A4920 (mangle_template_parameter)
  |     +-- sub_6A5DC0 (mangle_abi_tags)        B<tag> encoding
  |     +-- sub_6A6AF0 (mangle_template_args)
  |     +-- sub_6A78B0 (mangle_complete_type)
  |     +-- sub_6A8390 (mangled_nested_name_component)
  |     +-- sub_6A85E0 (mangled_entity_reference)
  |     +-- sub_6A8B10 (mangled_expression)     ~700 lines
  |     +-- sub_6AB280 (mangled_encoding_for_sizeof)
  |
  +-- Output buffer: qword_127FCC0
        [buffer_ptr, write_pos, capacity, overflow_flag, ...]
```

## Operator Name Table (sub_69C980)

`mangled_operator_name` at `0x69C980` is a pure lookup function: it takes an operator kind byte and an arity flag, and returns a pointer to the two-character Itanium ABI mangled operator code. The function covers all 47 overloadable C++ operators, including C++20 `co_await`.

Assert: `"mangled_operator_name: bad kind"` at `lower_name.c:11557`.

Four operators are context-sensitive — their mangled code depends on whether the usage is unary (arity `a2==1`) or binary:

| Kind | Unary | Binary | C++ Operator |
|---|---|---|---|
| 5 | `ps` | `pl` | `+` |
| 6 | `ng` | `mi` | `-` |
| 7 | `de` | `ml` | `*` |
| 11 | `ad` | `an` | `&` |

### Complete Operator Kind Table

| Kind | Code | Operator | Kind | Code | Operator |
|---|---|---|---|---|---|
| 1 | `nw` | `new` | 26 | `ls` | `<<` |
| 2 | `dl` | `delete` | 27 | `rs` | `>>` |
| 5 | `ps`/`pl` | `+` (unary/binary) | 28 | `rS` | `>>=` |
| 6 | `ng`/`mi` | `-` (unary/binary) | 29 | `lS` | `<<=` |
| 7 | `de`/`ml` | `*` (unary/binary) | 30 | `eq` | `==` |
| 9 | `rm` | `%` | 31 | `ne` | `!=` |
| 11 | `ad`/`an` | `&` (unary/binary) | 32 | `le` | `<=` |
| 12 | `or` | `\|` | 33 | `ge` | `>=` |
| 13 | `co` | `~` | 34 | `ss` | `<=>` |
| 14 | `nt` | `!` | 37 | `pp` | `++` |
| 16 | `lt` | `<` | 40 | `pm` | `->*` |
| 17 | `gt` | `>` | 41 | `pt` | `->` |
| 24 | `aN` | `%=` | 42 | `cl` | `()` |
| 43 | `ix` | `[]` | 44 | `qu` | `?:` |
| 45 | `v23min` | vendor `min` | 46 | `v23max` | vendor `max` |
| 47 | `aw` | `co_await` (C++20) | | | |

Kinds 3, 4, 8, 10, 15, 18–23, 25, 28–29, 35–36, 38–39 return pointers to `.rodata` string constants (`unk_A7C560` etc.) that encode the remaining standard operators (`dv`, `eo`, `aS`, `pL`, `mI`, `mL`, `dV`, `eO`, `aa`, `oo`, `mm`, `cm`).

Note kinds 45 and 46: these are vendor-extended operators using the `v<length><name>` Itanium ABI encoding. `v23min` and `v23max` are NVIDIA/CUDA-specific min/max operators with a length prefix of `23` — this encodes the string `"min"` (3 chars) and `"max"` (3 chars) as vendor-qualified identifiers.

## Entity Name Mangling (sub_6A1F00)

`mangle_entity_name` at `0x6A1F00` is the master mangling function. It produces the complete Itanium ABI mangled name for any entity node. At roughly 1000 decompiled lines, it handles every C++ entity kind through a multi-level dispatch.

### Demangling Mode Early Exit

The function begins with a demangling-mode check:

```c
if (qword_126ED90) {          // demangling / diagnostic mode
    emit_char(1, output);     // '?'
    emit_string("?", output);
    return;
}
```

When `qword_126ED90` is non-zero, the function emits `"?"` and returns immediately. This mode is used during diagnostic output when the compiler needs a placeholder rather than a real mangled name.

### Pre-dispatch: Special Cases (sub_69FF70)

Before the main dispatch, `sub_69FF70` (`check_mangling_special_cases`, 447 lines at `0x69FF70`) screens for entities that bypass normal mangling:

- **Linkage name override**: If the entity has an explicit `asm("name")` or `[[gnu::alias("name")]]`, the override name is used directly.
- **`extern "C"` linkage**: Returns the unmangled source name.
- **Builtin entities**: Special-cased to avoid generating bogus mangled names.

### Main Dispatch Structure

After special-case screening, `mangle_entity_name` dispatches on the entity kind byte at entity node offset `+132`:

| Entity Kind | Handler | Encoding |
|---|---|---|
| Regular function | `sub_6A3B00` (`mangle_function_encoding`) | `_Z<encoding>` |
| Regular variable | Direct type mangling | `_Z<name><type>` |
| Namespace member | `sub_6A0740` (`mangle_namespace_prefix`) | `N<qual>..E` |
| Class member | `sub_6A0A80` (`mangle_class_prefix`) | `N<class><name>E` |
| Template specialization | `sub_6A6AF0` (`mangle_template_args`) | `I<args>E` |
| Operator function | `sub_69C980` (`mangled_operator_name`) | operator codes |
| Constructor/destructor | `sub_69FE30` | `C1`/`C2`/`C3`/`D0`/`D1`/`D2` |
| Lambda closure | Lambda-specific path | `Ul<sig>E<disc>_` |
| Local entity | `sub_69F830` (`mangle_local_name`) | `Z<func>E<entity>` |
| Special (vtable etc.) | `sub_69FBC0` (`mangle_special_name`) | `TV`/`TI`/`GV` etc. |

### Type Encoding Subpipeline

Type mangling is handled by `sub_69E740` (`mangle_type_encoding`, 177 lines at `0x69E740`), which dispatches on type kind to produce Itanium ABI type codes:

| Type | Code | Type | Code |
|---|---|---|---|
| `void` | `v` | `bool` | `b` |
| `char` | `c` | `signed char` | `a` |
| `unsigned char` | `h` | `short` | `s` |
| `unsigned short` | `t` | `int` | `i` |
| `unsigned int` | `j` | `long` | `l` |
| `unsigned long` | `m` | `long long` | `x` |
| `unsigned long long` | `y` | `float` | `f` |
| `double` | `d` | `long double` | `e` |
| `__int128` | `n` | `unsigned __int128` | `o` |
| `wchar_t` | `w` | `char8_t` | `Du` |
| `char16_t` | `Ds` | `char32_t` | `Di` |
| `_Float16` | `DF16_` | `__float128` | `g` |
| `std::nullptr_t` | `Dn` | `auto` | `Da` |
| `decltype(auto)` | `Dc` | | |

Pointer and reference types are encoded with prefix qualifiers: `P` (pointer), `R` (lvalue reference), `O` (rvalue reference). CV-qualifiers use `K` (const), `V` (volatile), `r` (restrict).

#### Worked Example

A namespace-qualified template function with mixed argument kinds exercises every encoding piece — the `N..E` namespace wrapper, template-argument brackets `I..E`, builtin type codes, pointer and cv-qualifier prefixes, and substitution references:

```cpp
namespace ns {
    template<typename T>
    void foo(const T* p, int n);
}

// Instantiation: ns::foo<float>(const float*, int)
// Mangled:       _ZN2ns3fooIfEEvPKT_i
```

Decomposition: `_Z` (Itanium prefix) `N 2ns 3foo` (namespace `ns::foo`) `I f E` (template arg `float`) `E` (close namespace) `v` (return `void`) `P K T_` (`const T*` -> pointer-to-const-template-parameter-0) `i` (`int`). Using `T_` instead of re-emitting `f` is a substitution: the template parameter has already been bound, so the back-reference is shorter.

The builtin type mangler at `sub_6A13A0` (396 lines) includes CUDA-specific type detection through `dword_106C2C0` (GPU mode flag) to handle CUDA-extended types.

### Substitution Mechanism

The Itanium ABI uses substitution sequences (`S_`, `S0_`, `S1_`, ...) to compress repeated type references. The substitution infrastructure in `lower_name.c` centers on:

- `sub_69F0D0` (`mangle_substitution_check`): Checks whether a type/name component has already been emitted and should use a substitution reference.
- `sub_69F150` (`mangle_with_substitution`, 87 lines): Handles `S_` encoding, including the well-known substitutions `Sa` (`std::allocator`), `Sb` (`std::basic_string`), `Ss` (`std::string`), `Si` (`std::istream`), `So` (`std::ostream`), `Sd` (`std::iostream`).

### Template Argument Mangling

Template arguments are enclosed in `I...E` and handled by:

- `sub_69ED40` (`mangle_template_args`, 86 lines): Iterates the template argument list, emitting `I` prefix and `E` suffix.
- `sub_69EEE0` (`mangle_template_arg`, 109 lines): Mangles individual template arguments, dispatching between type arguments (direct type encoding), non-type arguments (expression or literal encoding), and template template arguments.
- `sub_6A4920` (`mangle_template_parameter`, 277 lines): Encodes template parameter references (`T_`, `T0_`, `T1_`, ...).

### ABI Tag Mangling (sub_6A5DC0)

`sub_6A5DC0` (643 lines at `0x6A5DC0`) handles `[[gnu::abi_tag("...")]]` attribute propagation per the Itanium ABI extensions. ABI tags are encoded as `B<length><tag>` suffixes and must be propagated through template instantiations and inline namespaces (e.g., `std::__cxx11::basic_string` with tag `cxx11`). This is one of the more complex mangling functions due to the transitive nature of tag propagation.

### Constructor/Destructor Encoding (sub_69FE30)

Constructors and destructors use the Itanium ABI's multi-variant encoding:

| Code | Meaning |
|---|---|
| `C1` | Complete object constructor |
| `C2` | Base object constructor |
| `C3` | Complete object allocating constructor |
| `D0` | Deleting destructor |
| `D1` | Complete object destructor |
| `D2` | Base object destructor |

### Special Name Mangling (sub_69FBC0)

`sub_69FBC0` (125 lines) produces mangled names for compiler-generated symbols:

| Prefix | Symbol |
|---|---|
| `_ZTV` | Virtual table |
| `_ZTT` | VTT (construction vtable) |
| `_ZTI` | `typeinfo` structure |
| `_ZTS` | `typeinfo` name string |
| `_ZGV` | Guard variable for static initialization |
| `_ZTH` | Thread-local initialization function |
| `_ZTW` | Thread-local wrapper function |

## Expression Mangling (sub_6A8B10)

`mangled_expression` at `0x6A8B10` is the second-largest function in `lower_name.c` at roughly 700 decompiled lines. It produces the Itanium ABI encoding for arbitrary C++ expressions appearing in template arguments, `noexcept` specifications, and `decltype` contexts.

Assert: `"mangled_encoding_for_expression_full"` at `lower_name.c:6870`, `"mangled_expr_operator_name: bad operator"` at `lower_name.c:11873`, `"mangled_call_operation"` at `lower_name.c:6132`.

### Expression Kind Dispatch

The function first calls `sub_69E740` to classify the expression node, then dispatches on the expression kind byte at node offset `+24`:

| Kind | Description | ABI Encoding |
|---|---|---|
| 0 | Error/unknown expression | `?` (demangling mode only) |
| 1 | Operator expression | Dispatches on operator byte at `+40` |
| 2 | Literal value | `L<type><value>E` |
| 3 | Entity reference | `L_Z<encoding>E` or substitution |
| 4 | Template parameter | `T_`/`T0_` etc. |
| 5 | sizeof/alignof/typeid/noexcept | Delegated to `sub_6AB280` |
| 6 | Cast expression | `sc`/`dc`/`rc`/`cv` prefix |
| 7 | Call expression | `cl<callee><args>E` or `cp<args>E` |
| 8 | Member access | `dt`/`pt` prefix |
| 9 | Conditional expression | `qu<cond><then><else>` |
| 10 | Pack expansion | `sp<pattern>` |

### Operator Sub-dispatch (Kind 1)

When the expression is an operator expression, the function reads the operator byte at node offset `+40` and performs a large switch covering 100+ cases. For standard binary and unary operators, it calls `sub_69C980` (`mangled_operator_name`) to get the two-character ABI code, then recursively processes operands. Notable special cases:

- **Cast operators** (kinds `0x05`--`0x13`): Dispatches between `sc` (static_cast), `dc` (dynamic_cast), `rc` (reinterpret_cast), and `cv` (C-style cast) based on cast flags at node offset `+25` and `+42`. The compressed mangling flag `dword_106BC7C` forces `cv` for all casts when set.
- **Vendor extensions** (`0x21`, `0x22`): `__real__` and `__imag__` complex number operations, encoded as `v18__real__` and `v18__imag__` using the vendor-extended operator format.
- **Increment/decrement** (kinds `0x23`--`0x26`): Pre/post increment (`pp`) and decrement (`mm`). Post-increment/decrement append `_` suffix per Itanium ABI.
- **Call expressions** (kinds `0x69`--`0x6D`, `0x16`--`0x17`, `0x69`): Dispatches to `mangled_call_operation` which determines the callee encoding and emits `cl` (call) or `cp` (non-dependent call) prefix.

### sizeof/alignof/typeid/noexcept (sub_6AB280)

`mangled_encoding_for_sizeof` at `0x6AB280` (130 lines) handles the sizeof-family of operators:

| ABI Code | Operator | Variant |
|---|---|---|
| `sz` | `sizeof(expr)` | Expression operand |
| `st` | `sizeof(type)` | Type operand |
| `az` | `alignof(expr)` | Expression operand |
| `at` | `alignof(type)` | Type operand |
| `te` | `typeid(expr)` | Expression operand |
| `ti` | `typeid(type)` | Type operand |
| `nx` | `noexcept(expr)` | Expression operand |

For older ABI versions (controlled by `dword_106BC7C` and `qword_126EF98`), the function emits vendor-specific codes `v17alignof` and `v18alignofe` instead of the standard `at`/`az` codes.

## Scalable Vector Name Mangling (sub_69CF10)

`mangled_scalable_vector_name` at `0x69CF10` (170 lines) returns mangled names for ARM SVE and RISC-V V extension scalable vector types. EDG supports these types natively, and they must be mangled using the vendor-specific Itanium ABI encoding.

Assert: `"mangled_scalable_vector_name"` at `lower_name.c:10473` and `lower_name.c:10440`.

The function dispatches on the type node's kind byte at offset `+132`:

### Dispatch Logic

1. **Kind 12 (elaborated type)**: Unwraps through the elaboration chain (offset `+144` points to the underlying type).
2. **Kind 3 (typedef/alias)**: Dispatches on subkind at offset `+144`:
   - Subkind 1: `svint` variants (signed integer vectors)
   - Subkind 2: `svfloat` variants (floating-point vectors)
   - Subkind 4: `svbool` variants (predicate vectors)
   - Subkind 9: `svcount` variants
3. **Kind 18 (mfloat8)**: `mfloat8x` types for ML inference.
4. **Kind 2 (plain vector)**: Dispatches on element type byte at offset `+144`, handling 8 element widths (cases 1–8).

Each type category has 4 mangling variants selected by the `a2` parameter (values 1–4), corresponding to different vector widths or tuple sizes (e.g., `svint8_t`, `svint8x2_t`, `svint8x3_t`, `svint8x4_t`). The actual mangled strings are stored in `.rodata` pointer tables (`off_A7E950` through `off_A7EA18`).

There is also special handling for `svboolx4_t` via `sub_7A7220`, which detects the specific boolean-tuple-of-4 predicate type and returns a dedicated mangling string.

## Mangling Output Buffer

All mangling functions write into a shared output buffer managed through `qword_127FCC0`. The buffer structure:

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `reserved` | Not used during mangling |
| `+8` | 8 | `capacity` | Allocated buffer size |
| `+16` | 8 | `write_pos` | Current write position (length of mangled name so far) |
| `+24` | 8 | `unused` | Reserved |
| `+32` | 8 | `buffer_ptr` | Pointer to character buffer |

Key buffer operations:

- `sub_69D850` (`append_char_to_buffer`): Appends a single character, calls `sub_6B9B20` to grow the buffer if `write_pos + 1 > capacity`.
- `sub_69D530` (`append_string`): Appends a string to the buffer.
- `sub_69D580` (`append_number`): Appends a base-36 encoded number.
- `sub_6B9B20` (`ensure_output_buffer_space`): Grows the buffer (doubles capacity).

The `sub_69DAA0` function (`mangle_number`, 63 lines) writes numbers in base-36 encoding as required by the Itanium ABI for substitution indices and discriminators.

## Mangling Type Marks

The mangling pipeline uses a mark-and-sweep mechanism to track which types have been referenced during signature mangling (needed for substitution sequence generation):

- `sub_69CCB0` (`set_signature_mark`, 76 lines): Marks types in a function signature for mangling. Handles function types (`a2=7`) and template functions (`a2=11`) by calling `sub_5CF440` for type traversal.
- `sub_69CE10` (`ttt_mark_entry`, 36 lines): Sets or clears the mangling mark on individual type entities. Uses bit 7 of byte at entity offset `+81`. The direction (mark vs unmark) is controlled by `dword_127FC70`.

## CUDA Demangler Extensions (sub_7CABB0)

The CUDA-aware demangler at `sub_7CABB0` (930 decompiled lines at `0x7CABB0`) is a statically linked NVIDIA implementation, not part of EDG. It implements a full Itanium ABI C++ name demangler with three NVIDIA vendor-type extensions for CUDA lambda wrappers.

### Function Signature

```c
unsigned char* sub_7CABB0(
    unsigned char *mangled_name,   // a1: input cursor into mangled name
    int64_t       qualifier_out,   // a2: output qualifier struct (24 bytes)
    char          flags,           // a3: behavior flags
    int64_t       output_ctx       // a4: output buffer context
);
```

### Output Buffer Context (a4)

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `buffer_ptr` | Output character buffer |
| `+8` | 8 | `write_pos` | Current output position |
| `+16` | 8 | `capacity` | Buffer capacity |
| `+24` | 4 | `error_flag` | Set to 1 on buffer overflow |
| `+28` | 4 | `overflow` | Redundant overflow indicator |
| `+32` | 8 | `suppress_level` | When >0, output is suppressed (for dry-run parsing) |
| `+48` | 8 | `error_count` | Cumulative parse error counter |
| `+64` | 8 | `skip_template` | When set, suppresses template argument output |

### Qualifier Output (a2)

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 4 | `has_template_args` | Set to 1 when template arguments were parsed |
| `+4` | 4 | `cv_qualifiers` | bit 0=const, bit 1=volatile, bit 2=restrict |
| `+8` | 4 | `ref_qualifier` | 0=none, 1=lvalue `&`, 2=rvalue `&&` |
| `+16` | 8 | `template_depth` | Template nesting depth |

### Flags (a3)

| Bit | Meaning |
|---|---|
| 0 | Static-from mode: wraps output in `[static from ...]...[C++]` |
| 1 | Suppress-scope mode: increments suppress level |

### Parsing Dispatch

The demangler handles these Itanium ABI top-level prefixes:

| Prefix Byte | ASCII | ABI Meaning | Handler |
|---|---|---|---|
| `0x42` | `B` | EDG block-scope static entity | Block-scope handler (offset + length) |
| `0x4E` | `N` | Nested name (qualified) | `sub_7CA440` (nested-name parser) |
| `0x5A` | `Z` | Local entity | `sub_7CEAE0` (encoding parser) + local suffix |
| `0x53` | `S` | Substitution | `sub_7CD7B0` (substitution resolver) |
| `0x53 0x74` | `St` | `std::` prefix | Emits `std::` + `sub_7CD0B0` (unqualified-name) |
| other | | Unqualified name | `sub_7CD0B0` (unqualified-name parser) |

After parsing the name, the function checks for `I` (template argument list, `0x49`) and dispatches to `sub_7C9D30` (template-args parser). A template argument cache at `qword_12C7B48`/`12C7B40`/`12C7B50` stores parsed entries using a dynamic array that grows by 500 entries via `malloc`/`realloc`.

### CUDA Vendor Type Extensions

The key NVIDIA extensions are triggered when the demangler encounters the vendor-extended type prefix `U` followed by `nv` (bytes `0x55 0x6E 0x76`). Three patterns are recognized:

#### Unvdl — Device Lambda Wrapper

Pattern: `Unvdl<arity><encoding><type>...`

```text
Input:  "Unvdl" + <numeric_arity> + <function_encoding> + <captured_types>...
Output: "__nv_dl_wrapper_t<__nv_dl_tag<(& :: <scope>), <arity>, <type1>, ...> >"
```

Decoded step by step:
1. Emit `__nv_dl_wrapper_t<`
2. Emit `__nv_dl_tag<`
3. Parse numeric arity via `sub_7C3180`, subtract 2 to get actual capture count
4. Parse one type (`sub_7CE590`) for the wrapped function type
5. Emit `,(` + `& :: ` + recursively demangle scope (calling `sub_7CABB0` with flags=2)
6. Emit `), `
7. Parse remaining captured types (count from step 3)
8. Emit `> >`

#### Unvdtl — Trailing Return Device Lambda

Pattern: `Unvdtl<arity><return_type><encoding><captured_types>...`

```text
Input:  "Unvdtl" + <arity> + <type> + <func_encoding> + <captured_types>...
Output: "__nv_dl_wrapper_t<__nv_dl_trailing_return_tag<...>, <return_type>, ...>"
```

Same as `Unvdl` except:
1. Emit `__nv_dl_wrapper_t<`
2. Emit `__nv_dl_trailing_return_tag<` (instead of `__nv_dl_tag<`)
3. After the scope demangling, parse an additional return type via `sub_7CE590`
4. Parse a function type via `sub_7CE5D0` (adds 1 to result pointer for the `E` terminator)
5. Then parse remaining captured types

#### Unvhdl — Host-Device Lambda Wrapper

Pattern: `Unvhdl<bool1><bool2><bool3><arity><encoding><captured_types>...`

```text
Input:  "Unvhdl" + <IsMutable> + <HasFuncPtrConv> + <NeverThrows> + <arity> + ...
Output: "__nv_hdl_wrapper_t<true/false, true/false, true/false,
          __nv_dl_tag<(& :: <scope>), <arity>, <type1>, ...> >"
```

The three boolean template parameters are decoded first:
1. Parse numeric value via `sub_7C3180` — if value != 2 (i.e., `false` in the encoding), emit `true,`; otherwise emit `false,`
2. Repeat for `HasFuncPtrConv` (second boolean)
3. Repeat for `NeverThrows` (third boolean)
4. Then proceed identically to `Unvdl` (emit `__nv_dl_tag<`, parse captures, etc.), but with `v68=1` flag marking the host-device variant

The boolean encoding convention: `2` encodes `false`, any other value (typically `0` or `1`) encodes `true`. This is the reverse of the usual convention and matches the internal encoding used by `nv_transforms.c` when generating the mangled names.

### Block-Scope Static Handling

When the input starts with `B` (ASCII 0x42), the demangler handles EDG's block-scope static entity encoding:

1. If flags bit 0 is set and suppress_level is 0: emit `[static from `
2. Parse an optional negative sign (`n`) followed by a decimal length
3. Skip ahead by that length (the block-scope name)
4. If suppress_level is 0: emit `] ` followed by `[C++] ` (the closing bracket and C++ marker)
5. If flags bit 0 is not set: decrement suppress_level

### Instance Suffix

After parsing the main name, if the next character is `_` followed by digits (or `__` followed by digits), the demangler parses an instance discriminator and emits `(instance N)` suffix in the output, where N = parsed_value + 2.

### Default Argument Suffix

For local entities (after `Z...E`), the discriminator prefix `d` triggers special handling:
- `d_` or `d<number>_`: emits `[default argument N (from end)]::` where N = parsed_value + 2
- `dn<number>_`: negative-index variant

### Call Graph

The demangler calls into specialized sub-parsers:

| Address | Function | Purpose |
|---|---|---|
| `sub_7CA440` | Nested-name parser | Handles `N...E` qualified names |
| `sub_7CEAE0` | Encoding parser | Top-level `<encoding>` production |
| `sub_7CD0B0` | Unqualified-name parser | `<source-name>` and operator names |
| `sub_7CD7B0` | Substitution resolver | `S_`/`S0_` back-references |
| `sub_7C9D30` | Template-args parser | `I<args>E` |
| `sub_7CE590` | Type parser | Full type demangling |
| `sub_7CE5D0` | Function-type parser | Function signature types |
| `sub_7C3180` | Numeric literal parser | Decimal number extraction |
| `sub_7C30C0` | Arity emitter | Outputs numeric arity values |
| `sub_7C2FB0` | String emitter | Emits literal strings to output buffer |
| `sub_7C3030` | Signed number parser | Handles negative numbers |

## Static Prefix for __global__ Templates (sub_6BE300)

`nv_get_full_nv_static_prefix` at `0x6BE300` (370 lines) in `nv_transforms.c` constructs unique prefix strings for `__global__` function templates with static/internal linkage. These prefixes are used to register device symbols in host reference arrays (the `.nvHR*` ELF sections that the CUDA runtime uses for symbol discovery).

Assert: `"nv_get_full_nv_static_prefix"` at `nv_transforms.c:2164`.

### Entry Conditions

The function checks two conditions on the entity node:
1. Bit `0x40` at entity offset `+182` must be set (marks `__global__` functions)
2. A name string at entity offset `+8` must be non-null

### Internal vs External Linkage Paths

The function takes different paths based on entity linkage:

**Internal linkage** (bits `0x12` at offset `+179` set, or storage class `0x10` at offset `+80`):

1. Build scoped name prefix via `sub_6BD2F0` (`nv_build_scoped_name_prefix`), which recursively walks the scope chain (offset `+40` -> parent scope at offset `+28`) to build `Namespace1::Namespace2::` style prefixes. Anonymous namespaces insert `_GLOBAL__N_<filename>`.
2. Hash the entity name via `sub_6BD1C0` (`format_string_to_sso`) using `vsnprintf` with a format string at address `8573734`.
3. Build the full prefix string using `snprintf`:

```c
snprintf(qword_1286760, n, "%s%lu_%s_", off_E7C768, strlen(filename), filename);
```

Where `off_E7C768` is a global prefix string (likely `"_nv_static_"`), the `%lu` is the filename length, and `%s` is the filename from `sub_5AF830(0)`. The result is cached in `qword_1286760` for reuse across entities in the same translation unit.

4. Concatenate prefix + `"_"` separator + entity scoped name
5. Register the full string in `qword_12868C0` (kernel internal-linkage host reference list)

**External linkage**:

1. Build name with `" ::"` scope prefix (the leading space is intentional — it matches the demangler output format)
2. Walk scope chain via `sub_6BD2F0` if the entity has a parent scope with kind 3 (namespace)
3. Hash the entity name via `sub_6BD1C0`
4. Append `"_"` separator
5. Register in `qword_1286880` (kernel external-linkage host reference list)

### Host Reference Arrays

The prefixes generated by this function end up in six global lists, one per combination of {kernel, device, constant} x {external, internal} linkage:

| Global | Section | Array Name |
|---|---|---|
| `unk_1286780` | `.nvHRDE` | `hostRefDeviceArrayExternalLinkage` |
| `unk_12867C0` | `.nvHRDI` | `hostRefDeviceArrayInternalLinkage` |
| `unk_1286800` | `.nvHRCE` | `hostRefConstantArrayExternalLinkage` |
| `unk_1286840` | `.nvHRCI` | `hostRefConstantArrayInternalLinkage` |
| `unk_1286880` | `.nvHRKE` | `hostRefKernelArrayExternalLinkage` |
| `unk_12868C0` | `.nvHRKI` | `hostRefKernelArrayInternalLinkage` |

These are emitted by `sub_6BCF80` (`nv_emit_host_reference_array`) as weak `extern "C"` byte arrays in the specified ELF sections.

## Related Mangling Infrastructure

### Type Mangling Subsystem (0x7C3000–0x7D0E00)

A separate type mangling subsystem exists in the `0x7C3000`--`0x7D0E00` range, used for diagnostic output and type encoding (distinct from the `lower_name.c` mangling used for symbol generation). Key functions:

| Address | Function | Lines | Description |
|---|---|---|---|
| `sub_7C3480` | `encode_operator_name` | 716 | Operator name encoding for diagnostics |
| `sub_7C5650` | `encode_type_for_mangling` | 794 | Full type encoding dispatcher |
| `sub_7C6290` | `encode_expression` | 2519 | Largest function — expression encoding |
| `sub_7C8BE0` | `encode_special_expression` | 674 | Special expression forms |
| `sub_7CBB90` | `encode_builtin_type` | 1314 | All builtin type mappings |
| `sub_7CEAE0` | `encode_template_args` | 1417 | Template argument encoding |
| `sub_7CFFC0` | `encode_nullptr` | 484 | nullptr-related type encoding |

The `encode_expression` function at `sub_7C6290` (2519 lines) is the largest single function in the entire type mangling subsystem and handles the full range of C++ expressions including `dynamic_cast`, `const_cast`, `reinterpret_cast`, `safe_cast`, `static_cast`, `subscript`, and `throw`.

### Nested Name Components (sub_6A8390)

`mangled_nested_name_component` at `0x6A8390` (101 lines) handles the intermediate components within `N...E` nested name encodings. It emits ABI substitution codes:

- `dn`: Destructor name
- `co`: Coercion operator
- `sr`: Unresolved scope resolution
- `L_ZN`: Local scope nested name
- `D1Ev`: Destructor suffix (complete object destructor, void return)

When in compressed mode (`dword_106BC7C` set), the function checks for `std::` namespace via `sub_7BE9E0` (`is_std_namespace`) and uses shortened forms.

### Entity Reference Mangling (sub_6A85E0)

`mangled_entity_reference` at `0x6A85E0` (197 lines) is the central dispatch for mangling entity references within expressions. It handles:

- Qualified scope resolution (bit 2 at entity offset `+81`)
- Address-of expressions (`ad` prefix)
- Compressed vs full mangling paths
- Class member vs free-function encoding

Assert: `"mangled_entity_reference"` at `lower_name.c:4183`.

### Mangling Discriminators (sub_69DBE0)

`mangle_discriminator` at `0x69DBE0` (72 lines) writes discriminators for local entities. Itanium ABI uses `_` for discriminator 0, `_<number>_` for higher discriminators, where the number is encoded in base-36.

## Global State Summary

| Global | Type | Purpose |
|---|---|---|
| `qword_127FCC0` | Buffer* | Primary mangling output buffer |
| `qword_126ED90` | qword | Demangling/diagnostic mode flag |
| `dword_106BC7C` | dword | Compressed/vendor-ABI mode flag |
| `qword_126EF98` | qword | ABI version selector |
| `dword_127FC70` | dword | Mark/unmark direction for type marks |
| `qword_1286760` | char* | Cached static prefix string |
| `qword_1286A00` | char* | Cached anonymous namespace name |
| `dword_12C6A24` | dword | Block-scope suppress level (demangler) |
| `qword_12C7B48` | qword | Template argument cache index |
| `qword_12C7B40` | qword | Template argument cache capacity |
| `qword_12C7B50` | qword | Template argument cache pointer |
| `off_E7C768` | char* | Static prefix base string |

## Function Address Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x69C830` | 24 | `init_lower_name` | LOW |
| `0x69C980` | 168 | `mangled_operator_name` | HIGH |
| `0x69CCB0` | 76 | `set_signature_mark` | HIGH |
| `0x69CE10` | 36 | `ttt_mark_entry` | HIGH |
| `0x69CF10` | 170 | `mangled_scalable_vector_name` | HIGH |
| `0x69D530` | — | `append_string` | MEDIUM |
| `0x69D580` | — | `append_number` | MEDIUM |
| `0x69D850` | — | `append_char_to_buffer` | MEDIUM |
| `0x69DAA0` | 63 | `mangle_number` | MEDIUM |
| `0x69DBE0` | 72 | `mangle_discriminator` | MEDIUM |
| `0x69E380` | 116 | `mangle_cv_qualifiers` | MEDIUM |
| `0x69E5F0` | 79 | `mangle_ref_qualifier` | MEDIUM |
| `0x69E740` | 177 | `mangle_type_encoding` | MEDIUM-HIGH |
| `0x69EA40` | 150 | `mangle_function_type` | MEDIUM |
| `0x69ED40` | 86 | `mangle_template_args` | MEDIUM |
| `0x69EEE0` | 109 | `mangle_template_arg` | MEDIUM |
| `0x69F0D0` | 28 | `mangle_substitution_check` | LOW |
| `0x69F150` | 87 | `mangle_with_substitution` | MEDIUM |
| `0x69F320` | 78 | `mangle_nested_name` | MEDIUM |
| `0x69F830` | 54 | `mangle_local_name` | MEDIUM |
| `0x69F930` | 60 | `mangle_unscoped_name` | MEDIUM |
| `0x69FA90` | 58 | `mangle_source_name` | MEDIUM |
| `0x69FBC0` | 125 | `mangle_special_name` | MEDIUM |
| `0x69FE30` | 78 | `mangle_constructor_destructor` | MEDIUM |
| `0x69FF70` | 447 | `check_mangling_special_cases` | MEDIUM-HIGH |
| `0x6A0740` | 189 | `mangle_namespace_prefix` | MEDIUM |
| `0x6A0A80` | 88 | `mangle_class_prefix` | MEDIUM |
| `0x6A0FB0` | 245 | `mangle_pointer_type` | MEDIUM |
| `0x6A13A0` | 396 | `mangle_builtin_type` | MEDIUM-HIGH |
| `0x6A1C80` | 97 | `mangle_expression` | MEDIUM |
| `0x6A1F00` | ~1000 | `mangle_entity_name` | HIGH |
| `0x6A4920` | 277 | `mangle_template_parameter` | MEDIUM |
| `0x6A5DC0` | 643 | `mangle_abi_tags` | MEDIUM-HIGH |
| `0x6A78B0` | 297 | `mangle_complete_type` | MEDIUM |
| `0x6A7F20` | 232 | `mangle_initializer` | MEDIUM |
| `0x6A8390` | 101 | `mangled_nested_name_component` | HIGH |
| `0x6A85E0` | 197 | `mangled_entity_reference` | HIGH |
| `0x6A8B10` | ~700 | `mangled_expression` | HIGH |
| `0x6AA030` | 30 | `mangled_expression_list` | HIGH |
| `0x6AB280` | 130 | `mangled_encoding_for_sizeof` | HIGH |
| `0x6BE300` | 370 | `nv_get_full_nv_static_prefix` | VERY HIGH |
| `0x7CABB0` | 930 | CUDA demangler (top-level) | HIGH |
