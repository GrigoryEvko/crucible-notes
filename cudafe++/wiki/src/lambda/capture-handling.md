# Capture Handling

C++ lambdas capture variables by creating closure-class fields -- one field per captured entity. For scalars this is straightforward: the closure stores a copy (or reference) of the variable. Arrays present a problem because C++ forbids direct value-capture of C-style arrays. CUDA extended lambdas compound the problem: the wrapper template that carries captures across the host/device boundary needs a uniform way to express every field's type, including multi-dimensional arrays and `const`-qualified variants. cudafe++ solves this with two injected template families: `__nv_lambda_field_type<T>` (a type trait that maps each captured variable's declared type to a storable type) and `__nv_lambda_array_wrapper<T[D1]...[DN]>` (a wrapper struct that holds a deep copy of an N-dimensional array with element-by-element copy in its constructor).

A separate subsystem handles the backend code generator's emission of capture type declarations and capture value expressions for each lambda. `nv_gen_extended_lambda_capture_types` (`sub_46E640`) walks the capture list and emits `decltype`-based template arguments wrapped in `__nvdl_remove_ref` / `__nvdl_remove_const` / `__nv_lambda_trait_remove_cv`. `sub_46E550` emits the corresponding capture values (variable names, `this`, `*this`, or init-capture expressions).

All of this is driven by a bitmap system that tracks which capture counts were actually used, so cudafe++ only emits the wrapper specializations that a given translation unit requires.

## Key Facts

| Property | Value |
|---|---|
| Field type trait | `__nv_lambda_field_type<T>` |
| Array wrapper | `__nv_lambda_array_wrapper<T[D1]...[DN]>` |
| Supported array dims | 1D (identity) through 7D (generated for ranks 2-8) |
| Array helper emitter | `sub_6BC290` (`emit_array_capture_helpers`) in `nv_transforms.c` |
| Capture type emitter | `sub_46E640` (`nv_gen_extended_lambda_capture_types`) in `cp_gen_be.c` |
| Capture value emitter | `sub_46E550` in `cp_gen_be.c` |
| Device bitmap | `unk_1286980` (128 bytes = 1024 bits) |
| Host-device bitmap | `unk_1286900` (128 bytes = 1024 bits) |
| Bitmap initializer | `sub_6BCBC0` (`nv_reset_capture_bitmasks`) |
| Bitmap setter | `sub_6BCBF0` (`nv_record_capture_count`) |

## __nv_lambda_field_type<T>

This is the type trait that maps every captured variable's declared type to a type suitable for storage in a wrapper struct field. For scalar types (and anything that is not an array), it is the identity:

```cpp
template <typename T>
struct __nv_lambda_field_type {
    typedef T type;
};
```

For array types, it maps to the corresponding `__nv_lambda_array_wrapper` specialization. cudafe++ generates partial specializations for dimensions 2 through 8, each in both non-const and const variants.

### Generated Specializations (Example: 3D)

```cpp
// Non-const array
template<typename T, size_t D1, size_t D2, size_t D3>
struct __nv_lambda_field_type<T [D1][D2][D3]> {
    typedef __nv_lambda_array_wrapper<T [D1][D2][D3]> type;
};

// Const array
template<typename T, size_t D1, size_t D2, size_t D3>
struct __nv_lambda_field_type<const T [D1][D2][D3]> {
    typedef const __nv_lambda_array_wrapper<T [D1][D2][D3]> type;
};
```

For 1D arrays (`T[D1]`), no specialization is generated. The primary template handles them -- 1D arrays decay to pointers in standard capture, so this is the identity case. The explicit specializations cover dimensions 2 through 8 (template parameter lists with `D1` through `D2`...`D7` respectively).

### Why Ranks 2-8

The loop in `sub_6BC290` runs with counter `v1` from 2 to 8 inclusive (`while (v1 != 9)`). Rank 1 is handled by the primary template. Rank 9+ triggers the `static_assert` in the unspecialized `__nv_lambda_array_wrapper` primary template. This bounds the maximum supported array dimensionality for lambda capture at 7D -- an extremely generous limit (standard CUDA kernels rarely exceed 3D arrays).

## __nv_lambda_array_wrapper<T[D1]...[DN]>

The array wrapper is a struct that owns a copy of an N-dimensional C-style array. Since arrays cannot be value-captured in C++ (they decay to pointers), this wrapper provides the deep-copy semantics that CUDA extended lambdas need.

### Primary Template (Trap)

The unspecialized primary template contains only a `static_assert` that always fires:

```cpp
template <typename T>
struct __nv_lambda_array_wrapper {
    static_assert(sizeof(T) == 0,
        "nvcc internal error: unexpected failure in capturing array variable");
};
```

This catches any array dimensionality that falls outside the range [2, 8]. Since `sizeof(T)` is never zero for a real type, the assertion always fails if the primary template is instantiated.

### Generated Specializations

For each rank N from 2 through 8, `sub_6BC290` generates a partial specialization:

```cpp
// Example: rank 3
template<typename T, size_t D1, size_t D2, size_t D3>
struct __nv_lambda_array_wrapper<T [D1][D2][D3]> {
    T arr[D1][D2][D3];
    __nv_lambda_array_wrapper(const T in[D1][D2][D3]) {
        for(size_t i1 = 0; i1 < D1; ++i1)
        for(size_t i2 = 0; i2 < D2; ++i2)
        for(size_t i3 = 0; i3 < D3; ++i3)
            arr[i1][i2][i3] = in[i1][i2][i3];
    }
};
```

The constructor takes a `const T in[D1]...[DN]` parameter and performs element-by-element copy via nested for-loops. Each loop variable is named `i1` through `iN` and iterates from 0 to `D1` through `DN` respectively. The assignment `arr[i1]...[iN] = in[i1]...[iN]` copies each element.

### Reconstructed Output for Rank 4

What `sub_6BC290` actually emits for a 4-dimensional array (directly from the decompiled string fragments):

```cpp
template<typename T, size_t D1, size_t D2, size_t D3, size_t D4>
struct __nv_lambda_array_wrapper<T [D1][D2][D3][D4]> {
    T arr[D1][D2][D3][D4];
    __nv_lambda_array_wrapper(const T in[D1][D2][D3][D4]) {
        for(size_t i1 = 0; i1  < D1; ++i1)
        for(size_t i2 = 0; i2  < D2; ++i2)
        for(size_t i3 = 0; i3  < D3; ++i3)
        for(size_t i4 = 0; i4  < D4; ++i4)
            arr[i1][i2][i3][i4] = in[i1][i2][i3][i4];
    }
};
```

Note the double-space before `<` in the `for` condition -- this is present in the actual emitted code (visible in the decompiled `sprintf` format string `"for(size_t i%u = 0; i%u  < D%u; ++i%u)"`).

## sub_6BC290: emit_array_capture_helpers

Address `0x6BC290`, 183 decompiled lines, in `nv_transforms.c`. Takes a single argument: `void (*a1)(const char *)`, the text emission callback.

### Algorithm

The function has two major loops, each iterating rank from 2 to 8.

**Loop 1 -- Array wrapper specializations:**

```
for rank = 2 to 8:
    emit "template<typename T"
    for d = 1 to rank-1:
        emit ", size_t D{d}"
    emit ">\nstruct __nv_lambda_array_wrapper<T "
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit "> {T arr"
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit ";\n__nv_lambda_array_wrapper(const T in"
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit ") {"
    for d = 1 to rank-1:
        emit "\nfor(size_t i{d} = 0; i{d}  < D{d}; ++i{d})"
    emit " arr"
    for d = 1 to rank-1:
        emit "[i{d}]"
    emit " = in"
    for d = 1 to rank-1:
        emit "[i{d}]"
    emit ";\n}\n};\n"
```

**Loop 2 -- Field type specializations:**

First emits the primary `__nv_lambda_field_type`:
```
emit "template <typename T>\nstruct __nv_lambda_field_type {\ntypedef T type;};"
```

Then for each rank from 2 to 8, emits two specializations (non-const and const):

```
for rank = 2 to 8:
    // Non-const specialization
    emit "template<typename T"
    for d = 1 to rank-1:
        emit ", size_t D{d}"
    emit ">\nstruct __nv_lambda_field_type<T "
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit "> {\ntypedef __nv_lambda_array_wrapper<T "
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit "> type;\n};\n"

    // Const specialization
    emit "template<typename T"
    for d = 1 to rank-1:
        emit ", size_t D{d}"
    emit ">\nstruct __nv_lambda_field_type<const T "
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit "> {\ntypedef const __nv_lambda_array_wrapper<T "
    for d = 1 to rank-1:
        emit "[D{d}]"
    emit "> type;\n};\n"
```

### Stack Usage

Two stack buffers: `v33[1024]` for the `for`-loop lines (the sprintf format includes four `%u` substitutions) and `s[1064]` for the dimension fragments (smaller format: `"%s%u%s"` with prefix/suffix).

### Emission Order in Preamble

`sub_6BC290` is called from `sub_6BCC20` (`nv_emit_lambda_preamble`) at step 3, after `__nvdl_remove_ref`/`__nvdl_remove_const` trait helpers and `__nv_dl_tag`, but before the primary `__nv_dl_wrapper_t` definition. This ordering is critical: `__nv_dl_wrapper_t` field declarations reference `__nv_lambda_field_type`, which in turn references `__nv_lambda_array_wrapper`, so both must be defined first.

## Capture Type Emission (sub_46E640)

Address `0x46E640`, approximately 400 decompiled lines, in `cp_gen_be.c`. Confirmed identity: `nv_gen_extended_lambda_capture_types` (assert string at line 17368 of `cp_gen_be.c`).

This function emits the template type arguments that appear in a wrapper struct instantiation. For a device lambda wrapper `__nv_dl_wrapper_t<Tag, F1, F2, ..., FN>`, this function generates the `F1` through `FN` types. Each type must precisely match the declared type of the captured variable, with references and top-level const stripped.

### Input

Takes `__int64 **a1` -- a pointer to the lambda info structure. The capture list is a linked list starting at `*a1` (offset +0 of the lambda info). Each capture entry is a node with:

| Offset | Size | Field |
|---|---|---|
| +0 | 8 | `next` pointer (linked list) |
| +8 | 8 | `variable_entity` -- pointer to the captured variable's entity node |
| +24 | 8 | `init_capture_scope` -- scope for init-capture expressions |
| +32 | 1 | `flags_byte_1` -- bit 0 = init-capture, bit 7 = has braces/parens |
| +33 | 1 | `flags_byte_2` -- bit 0 = paren-init (vs brace-init) |

The variable entity at offset +8 has:
- Offset +8: name string (null if `*this` capture)
- Offset +163: sign bit (bit 7) -- if set, this is a `*this` or `this` capture

### Algorithm: Three Capture Kinds

The function walks the capture list and for each entry, dispatches on two conditions: the init-capture flag (`i[4] & 1`) and the `*this` flag (byte at entity+163 sign bit).

**Case 1: Regular variable capture** (`i[4] & 1 == 0` and entity+163 >= 0)

Emits:
```cpp
, typename __nvdl_remove_ref<decltype(varname)>::type
```

Where `varname` is the string at entity+8. This strips reference qualification from the variable's type. The `decltype(varname)` ensures the type is deduced from the actual declaration, not from any decay.

**Case 2: `*this` capture** (`i[4] & 1 == 0` and entity+163 < 0)

Two sub-cases depending on whether this is an explicit `this` capture (C++23 deducing this) versus traditional `*this`:

If `i[4] & 8` (explicit this):
```cpp
, decltype(this) const
```

Otherwise (traditional `*this`):
```cpp
, typename __nvdl_remove_const<typename __nvdl_remove_ref<decltype(*this) > ::type> :: type
```

If the lambda is non-const (mutable), `const` is not appended. The mutable check reads `(byte)a1[3] & 2` -- if clear, appends ` const`.

**Case 3: Init-capture** (`i[4] & 1 != 0`)

Emits:
```cpp
, typename __nv_lambda_trait_remove_cv<typename __nvdl_remove_ref<decltype({expr})>::type>::type
```

Where `{expr}` is the init-capture expression, emitted by calling `sub_46D910` (the expression code generator). The expression is wrapped in `{...}` (brace-init) or `(...)` (paren-init) depending on byte+33 bit 0. The additional `__nv_lambda_trait_remove_cv` wrapper strips top-level const and volatile from the deduced type.

### GCC Diagnostic Guards

When `dword_126E1E8` is set (indicating the host compiler is GCC-based), the init-capture path wraps the `decltype` expression in pragma guards:

```cpp
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunevaluated-expression"
decltype({expr})
#pragma GCC diagnostic pop
```

This suppresses GCC warnings about using `decltype` on expressions that are not evaluated. The flag `dword_126E1E8` is likely set when the target host compiler is GCC rather than MSVC or Clang.

### Character-by-Character Emission

The decompiled code reveals that `sub_46E640` does not use `sub_467E50` (emit string) for all output. For short constant strings like `", "`, `"typename __nvdl_remove_ref<decltype("`, etc., it emits character-by-character via `putc(ch, stream)` with a manual loop. This is a common pattern in EDG's code generator where inline string emission avoids function-call overhead for fixed text.

The character counter `dword_106581C` tracks the column position for line-wrapping decisions. Each emission path increments it by the string length.

## Capture Value Emission (sub_46E550)

Address `0x46E550`, 60 decompiled lines, in `cp_gen_be.c`. This function emits the actual values passed to the wrapper constructor -- the runtime expressions that initialize each captured field.

### Algorithm

Walks the same capture linked list. For each entry, emits `, ` followed by:

| Condition | Output |
|---|---|
| Regular variable (`byte+32 & 1 == 0`, entity+163 >= 0) | Variable name string from entity+8 |
| Explicit this (`byte+32 & 8`, entity+163 < 0) | `this` |
| Traditional *this (`byte+32 & 8 == 0`, entity+163 < 0) | `*this` |
| Init-capture (`byte+32 & 1`) | The init-capture expression via `sub_46D910` |

For init-captures, the expression is wrapped in `(...)` or `{...}` based on bit 0 of byte+33:
- Bit 0 set: paren-init `(expr)`
- Bit 0 clear: brace-init `{expr}`

### Relationship to Type Emission

`sub_46E550` and `sub_46E640` are called in sequence by the per-lambda wrapper emitter (`sub_47B890`, `gen_lambda`). The type emission produces the template type parameters; the value emission produces the constructor arguments. Together they construct an expression like:

```cpp
__nv_dl_wrapper_t<
    __nv_dl_tag<decltype(&Closure::operator()), &Closure::operator(), 42>,
    typename __nvdl_remove_ref<decltype(x)>::type,
    typename __nvdl_remove_ref<decltype(y)>::type
>(tag, x, y)
```

## Bitmap System

Rather than generating wrapper specializations for all possible capture counts (0 through 1023), cudafe++ maintains two 1024-bit bitmaps that record which counts were actually observed during frontend parsing. During preamble emission, only the specializations for set bits are generated.

### Memory Layout

```
unk_1286980 (device lambda bitmap):
    Address: 0x1286980
    Size:    128 bytes = 16 x uint64_t = 1024 bits
    Bit N:   __nv_dl_wrapper_t specialization for N captures needed

unk_1286900 (host-device lambda bitmap):
    Address: 0x1286900
    Size:    128 bytes = 16 x uint64_t = 1024 bits
    Bit N:   __nv_hdl_wrapper_t specializations for N captures needed
```

### sub_6BCBC0: nv_reset_capture_bitmasks

Address `0x6BCBC0`, 9 decompiled lines. Called before each translation unit.

```c
memset(&unk_1286980, 0, 0x80);   // Clear device bitmap (128 bytes)
memset(&unk_1286900, 0, 0x80);   // Clear host-device bitmap (128 bytes)
```

### sub_6BCBF0: nv_record_capture_count

Address `0x6BCBF0`, 13 decompiled lines. Called from `scan_lambda` (`sub_447930`) after counting captures.

```c
_QWORD *result = &unk_1286900;          // Default: host-device bitmap
if (!a1)
    result = &unk_1286980;              // a1 == 0: device bitmap
result[a2 >> 6] |= 1LL << a2;          // Set bit a2
```

Parameters:
- `a1` (int): Bitmap selector. `0` = device, non-zero = host-device.
- `a2` (unsigned): Capture count (0-1023).

The bit-set logic: `a2 >> 6` selects the uint64_t word (divides by 64), and `1LL << a2` sets the appropriate bit within that word. Since `a2` is an `unsigned int`, the shift `1LL << a2` uses only the low 6 bits of `a2` on x86-64, so the word index and bit index are consistent.

Note the mapping inversion: `a1 == 0` maps to `unk_1286980` (device), while `a1 != 0` maps to `unk_1286900` (host-device). This is counterintuitive but confirmed by the decompiled code.

### Bitmap Scan in nv_emit_lambda_preamble

The scan loop in `sub_6BCC20` processes each bitmap as 16 uint64_t words:

```c
// Device lambda bitmap scan
uint64_t *ptr = (uint64_t *)&unk_1286980;
unsigned int idx = 0;
do {
    uint64_t word = *ptr;
    unsigned int limit = idx + 64;
    do {
        if (idx != 0 && (word & 1))
            sub_6BB790(idx, callback);   // emit_device_lambda_wrapper_specialization
        ++idx;
        word >>= 1;
    } while (limit != idx);
    ++ptr;
} while (limit != 1024);

// Host-device lambda bitmap scan
ptr = (uint64_t *)&unk_1286900;
idx = 0;
do {
    uint64_t word = *ptr;
    unsigned int limit = idx + 64;
    do {
        while ((word & 1) == 0) {    // Skip unset bits
            ++idx;
            word >>= 1;
            if (idx == limit) goto next_word;
        }
        sub_6BBB10(0, idx, callback);    // Non-mutable, HasFuncPtrConv=false
        sub_6BBEE0(0, idx, callback);    // Non-mutable, HasFuncPtrConv=true
        sub_6BBB10(1, idx, callback);    // Mutable, HasFuncPtrConv=false
        sub_6BBEE0(1, idx++, callback);  // Mutable, HasFuncPtrConv=true
        word >>= 1;
    } while (idx != limit);
next_word:
    ++ptr;
} while (idx != 1024);
```

Key differences between the two scans:
- The device scan skips bit 0 (`if (idx != 0 && ...)`). The zero-capture case is handled by the primary template and its explicit `<Tag>` specialization already emitted as static text.
- The host-device scan does not skip bit 0 -- zero-capture host-device lambdas (stateless lambdas with `__host__ __device__`) still need wrapper specializations because the host-device wrapper has function-pointer-conversion variants.
- Each set bit in the host-device bitmap triggers four emitter calls (non-mutable/mutable x HasFuncPtrConv false/true), compared to one call per bit for device lambdas.

## How Fields Use __nv_lambda_field_type

When `sub_6BB790` (`emit_device_lambda_wrapper_specialization`) generates a wrapper struct for N captures, each field is declared as:

```cpp
typename __nv_lambda_field_type<F1>::type f1;
typename __nv_lambda_field_type<F2>::type f2;
// ... through fN
```

This indirection through `__nv_lambda_field_type` means:
- If `F1` is `int`, the field type is `int` (identity via primary template).
- If `F1` is `float[3][4]`, the field type is `__nv_lambda_array_wrapper<float[3][4]>`, which stores a deep copy.
- If `F1` is `const double[2][2]`, the field type is `const __nv_lambda_array_wrapper<double[2][2]>`.

The constructor mirrors this pattern:

```cpp
__nv_dl_wrapper_t(Tag, F1 in1, F2 in2, ..., FN inN)
    : f1(in1), f2(in2), ..., fN(inN) { }
```

For array captures, the `f1(in1)` initialization invokes `__nv_lambda_array_wrapper`'s constructor, which performs the element-by-element copy. For scalar captures, it is a trivial copy/move.

## End-to-End Example

Given user code:

```cpp
int x = 42;
float matrix[3][4];
auto lam = [x, matrix]() __device__ { /* use x and matrix */ };
```

cudafe++ produces:

1. **Frontend** (`scan_lambda`): Counts 2 captures. Calls `sub_6BCBF0(0, 2)` to set bit 2 in the device bitmap.

2. **Preamble emission** (`sub_6BCC20`): Scans the device bitmap, finds bit 2 set. Calls `sub_6BB790(2, emit)` which generates:

```cpp
template <typename Tag, typename F1, typename F2>
struct __nv_dl_wrapper_t<Tag, F1, F2> {
    typename __nv_lambda_field_type<F1>::type f1;
    typename __nv_lambda_field_type<F2>::type f2;
    __nv_dl_wrapper_t(Tag, F1 in1, F2 in2) : f1(in1), f2(in2) { }
    template <typename...U1>
    int operator()(U1...) { return 0; }
};
```

3. **Per-lambda emission** (`sub_47B890` calling `sub_46E640` and `sub_46E550`):

```cpp
__nv_dl_wrapper_t<
    __nv_dl_tag<decltype(&ClosureType::operator()), &ClosureType::operator(), 0>,
    typename __nvdl_remove_ref<decltype(x)>::type,        // int
    typename __nvdl_remove_ref<decltype(matrix)>::type     // float[3][4]
>(tag, x, matrix)
```

4. **Template instantiation**: The host compiler instantiates the wrapper. `F1 = int` so `__nv_lambda_field_type<int>::type = int` (identity). `F2 = float[3][4]` so `__nv_lambda_field_type<float[3][4]>::type = __nv_lambda_array_wrapper<float[3][4]>`, which triggers the rank-2 specialization with its nested double for-loop constructor.

## Function Map

| Address | Name (recovered) | Source | Lines | Role |
|---|---|---|---|---|
| `sub_6BC290` | `emit_array_capture_helpers` | `nv_transforms.c` | 183 | Emit `__nv_lambda_array_wrapper` (ranks 2-8) and `__nv_lambda_field_type` specializations |
| `sub_6BCBC0` | `nv_reset_capture_bitmasks` | `nv_transforms.c` | 9 | Zero both 128-byte bitmaps at translation unit start |
| `sub_6BCBF0` | `nv_record_capture_count` | `nv_transforms.c` | 13 | Set bit N in device or host-device bitmap |
| `sub_6BCC20` | `nv_emit_lambda_preamble` | `nv_transforms.c` | 244 | Master emitter -- scans bitmaps, calls all sub-emitters |
| `sub_6BB790` | `emit_device_lambda_wrapper_specialization` | `nv_transforms.c` | 191 | Emit `__nv_dl_wrapper_t<Tag, F1..FN>` for N captures |
| `sub_46E640` | `nv_gen_extended_lambda_capture_types` | `cp_gen_be.c` | ~400 | Emit `decltype`-based template type args for each capture |
| `sub_46E550` | (capture value emitter) | `cp_gen_be.c` | ~60 | Emit variable names / `this` / `*this` / init-capture exprs |
| `sub_46D910` | (expression code generator) | `cp_gen_be.c` | -- | Called by both `sub_46E640` and `sub_46E550` for init-captures |
| `sub_467E50` | (emit string to output) | `cp_gen_be.c` | -- | String emission helper used by code generator |
| `sub_467DA0` | (column tracking helper) | `cp_gen_be.c` | -- | Called when `dword_1065818` is set for line-length management |

## Global State

| Variable | Address | Size | Purpose |
|---|---|---|---|
| `unk_1286980` | `0x1286980` | 128 bytes | Device lambda capture-count bitmap |
| `unk_1286900` | `0x1286900` | 128 bytes | Host-device lambda capture-count bitmap |
| `dword_106581C` | `0x106581C` | 4 bytes | Column counter for output line tracking |
| `dword_1065818` | `0x1065818` | 4 bytes | Line-length management enabled flag |
| `dword_126E1E8` | `0x126E1E8` | 4 bytes | GCC-compatible host compiler flag (enables diagnostic pragmas) |
| `stream` | (global) | 8 bytes | Output FILE* for code generation |

## Related Pages

- [Extended Lambda Overview](./overview.md) -- end-to-end lambda pipeline and `lambda_info` structure
- [Device Lambda Wrapper](./device-wrapper.md) -- `__nv_dl_wrapper_t` template anatomy
- [Host-Device Lambda Wrapper](./host-device-wrapper.md) -- `__nv_hdl_wrapper_t` type-erased design
- [Preamble Injection](./preamble-injection.md) -- `sub_6BCC20` emission sequence in full detail
- [Lambda Restrictions](./restrictions.md) -- validation errors for malformed captures
