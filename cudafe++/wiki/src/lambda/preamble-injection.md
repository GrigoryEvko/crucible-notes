# Preamble Injection

The entire CUDA extended lambda template library — every `__nv_dl_wrapper_t`, every `__nv_hdl_wrapper_t`, every trait helper and detection macro — enters the compilation through a single function: `sub_6BCC20` (`nv_emit_lambda_preamble`). This 244-line function in `nv_transforms.c` accepts a `void(*emit)(const char*)` callback and produces raw C++ source text that is injected into the `.int.c` output stream. The preamble is emitted exactly once per translation unit, triggered by a sentinel type declaration named `__nv_lambda_preheader_injection`. The trigger mechanism lives in `sub_4864F0` (`gen_type_decl` in `cp_gen_be.c`), which string-compares each type declaration's name against the sentinel marker, emits a synthetic `#line` directive, and then calls the master emitter.

The preamble contains 20 logical emission steps, ranging from simple type traits (4 lines each) to bitmap-driven loops that generate hundreds of template specializations. The design is driven by a critical optimization: rather than emitting all 1024 possible capture-count specializations for each wrapper type, cudafe++ maintains two 1024-bit bitmaps (`unk_1286980` for device lambdas, `unk_1286900` for host-device lambdas) that track which capture counts were actually used during frontend parsing. The preamble emitter scans these bitmaps and generates only the specializations that the translation unit requires.

## Key Facts

| Property | Value |
|---|---|
| Master emitter | `sub_6BCC20` (`nv_emit_lambda_preamble`, 244 lines, `nv_transforms.c`) |
| Trigger function | `sub_4864F0` (`gen_type_decl`, 751 lines, `cp_gen_be.c`) |
| Emit callback (typical) | `sub_467E50` (raw text output to `.int.c` stream) |
| Sentinel type name | `__nv_lambda_preheader_injection` |
| Synthetic source file | `"nvcc_internal_extended_lambda_implementation"` |
| Enable flag | `dword_106BF38` (`--extended-lambda` / `--expt-extended-lambda`) |
| Device bitmap | `unk_1286980` (128 bytes = 16 x `uint64` = 1024 bits) |
| Host-device bitmap | `unk_1286900` (128 bytes = 16 x `uint64` = 1024 bits) |
| C++17 noexcept gate | `dword_126E270` (controls noexcept trait variants) |
| One-shot guarantee | Once emitted, the sentinel type is wrapped in `#if 0` / `#endif` |
| Max capture count | 1024 (bit index range 0..1023) |
| Array dimension range | 2D through 8D (7 specializations per wrapper) |

## Trigger Mechanism: sub_4864F0 (gen_type_decl)

The preamble is not emitted eagerly at the start of compilation. Instead, the EDG frontend inserts a synthetic type declaration named `__nv_lambda_preheader_injection` into the IL at the point where the lambda template library is needed. During backend code generation, `sub_4864F0` (the type declaration emitter in `cp_gen_be.c`) encounters this declaration and performs the following sequence:

```c
// sub_4864F0, decompiled lines 200-242
// Check: is this a type tagged with the preheader marker? (bit at v4-8 & 0x10)
if ((*(_BYTE *)(v4 - 8) & 0x10) != 0)
{
    if (dword_106BF38)                           // --extended-lambda enabled?
    {
        v18 = *(_QWORD *)(v4 + 8);              // get type name pointer
        if (v18)
        {
            // Compare name against "__nv_lambda_preheader_injection" (30 chars + NUL)
            v30 = "__nv_lambda_preheader_injection";
            v31 = 32;                             // comparison length
            do {
                if (!v31) break;
                v29 = *(_BYTE *)v18++ == *v30++;
                --v31;
            } while (v29);

            if (v29)                              // name matched
            {
                if (dword_106581C)                // pending newline needed
                    sub_467D60();                 // emit newline

                // Emit #line directive pointing to synthetic source file
                v32 = "#line";
                if (dword_126E1DC)                // shorthand mode
                    v32 = "#";
                sub_467E50(v32);
                sub_467E50(" 1 \"nvcc_internal_extended_lambda_implementation\"");

                if (dword_106581C)
                    sub_467D60();

                // THE CRITICAL CALL: emit entire lambda template library
                sub_6BCC20(sub_467E50);

                dword_1065820 = 0;                // reset line tracking state
                qword_1065828 = 0;
            }
        }
    }
    // Suppress the sentinel type from host compiler output
    sub_46BC80("#if 0");
    --dword_1065834;
    sub_467D60();
}
```

### Trigger Conditions

Three conditions must all be true for preamble emission:

1. **Marker bit set** — The type declaration node has bit `0x10` set at offset `-8` (the IL node header flags). This bit marks NVIDIA-injected synthetic declarations.

2. **Extended lambda mode active** — `dword_106BF38` is nonzero, meaning `--extended-lambda` (or `--expt-extended-lambda`) was passed to nvcc.

3. **Name matches sentinel** — The type's name at offset `+8` is byte-equal to `"__nv_lambda_preheader_injection"` (a 31-character string including NUL; the comparison loop runs up to 32 iterations).

### Synthetic Source File Context

Before calling `sub_6BCC20`, the trigger emits:

```c
#line 1 "nvcc_internal_extended_lambda_implementation"
```

This `#line` directive serves two purposes: it changes the apparent source file for any diagnostics emitted during template parsing, and it provides a recognizable marker in the generated `.int.c` file for debugging. All lambda template infrastructure appears to originate from `"nvcc_internal_extended_lambda_implementation"` rather than from the user's source file. The `dword_126E1DC` flag selects between `#line` and the shorthand `#` form for the line directive.

### One-Shot Guarantee and Sentinel Suppression

After the preamble is emitted, the sentinel type declaration is wrapped in `#if 0` / `#endif`. The `#if 0` is emitted immediately after the preamble call (line 239: `sub_46BC80("#if 0")`). The matching `#endif` is emitted later when `sub_4864F0` reaches the closing path for this declaration type (lines 736-745):

```c
else if ((*(_BYTE *)(v4 - 8) & 0x10) != 0)
{
    if (dword_106581C)
        sub_467D60();
    ++dword_1065834;
    sub_468190("#endif");
    --dword_1065834;
    sub_467D60();
    dword_1065820 = 0;
    qword_1065828 = 0;
}
```

The sentinel type `__nv_lambda_preheader_injection` never reaches the host compiler's type system — it exists solely as a positional marker in the IL. Because the EDG frontend inserts exactly one such declaration per translation unit, and the backend processes declarations sequentially, the preamble is guaranteed to be emitted exactly once.

After emission, `dword_1065820` (output line counter) and `qword_1065828` (output state pointer) are reset to zero, ensuring subsequent `#line` directives correctly track the user's source file.

## Master Emitter: sub_6BCC20

The function signature:

```c
__int64 __fastcall sub_6BCC20(void (__fastcall *a1)(const char *));
```

The single parameter `a1` is an output callback. In production, this is always `sub_467E50` — the function that writes raw text to the `.int.c` output stream. Every `a1("...")` call appends the given string literal to the output. The function has no other state parameters; all needed state (bitmaps, C++17 flag) is read from globals.

The 20 emission steps are executed unconditionally in a fixed order. Steps 6 and 9 contain bitmap-scanning loops that conditionally call sub-emitters based on which capture counts were registered during frontend parsing. Step 11 is gated on the C++17 noexcept flag.

### Step 1: Type Removal Traits and Wrapper Helper Macro

The first `a1(...)` call emits the largest single string literal in the function — three foundational metaprogramming utilities:

```cpp
#define __NV_LAMBDA_WRAPPER_HELPER(X, Y) decltype(X), Y

template <typename T>
struct __nvdl_remove_ref { typedef T type; };

template<typename T>
struct __nvdl_remove_ref<T&> { typedef T type; };

template<typename T>
struct __nvdl_remove_ref<T&&> { typedef T type; };

template <typename T, typename... Args>
struct __nvdl_remove_ref<T(&)(Args...)> {
  typedef T(*type)(Args...);
};

template <typename T>
struct __nvdl_remove_const { typedef T type; };

template <typename T>
struct __nvdl_remove_const<T const> { typedef T type; };
```

`__NV_LAMBDA_WRAPPER_HELPER(X, Y)` expands to `decltype(X), Y`. It provides the `<U, func>` pair for tag type construction from a single expression. At each lambda wrapper call site, the per-lambda emitter (`sub_47B890`) generates `__NV_LAMBDA_WRAPPER_HELPER(&Closure::operator(), &Closure::operator())`, which expands to `decltype(&Closure::operator()), &Closure::operator()`.

`__nvdl_remove_ref` strips lvalue and rvalue references, with a special case for function references (`T(&)(Args...)` -> `T(*)(Args...)`). `__nvdl_remove_const` strips top-level `const`. Both are used during capture type emission to normalize captured variable types before passing them as template arguments to wrapper structs.

### Step 2: Device Lambda Tag

```cpp
template <typename U, U func, unsigned>
struct __nv_dl_tag { };
```

The device lambda tag type. `U` is the type of the lambda's `operator()`, `func` is a non-type template parameter holding the pointer to that operator, and the `unsigned` disambiguates lambdas with identical operator types at different call sites within the same TU.

### Step 3: Array Capture Helpers (sub_6BC290)

`sub_6BCC20` calls `sub_6BC290(a1)`, which emits the `__nv_lambda_array_wrapper` and `__nv_lambda_field_type` infrastructure for C-style array captures. This is a separate 183-line function that generates templates for array dimensions 2 through 8.

Three template families are emitted:

**Primary template (static_assert trap):**

```cpp
template <typename T>
struct __nv_lambda_array_wrapper {
    static_assert(sizeof(T) == 0,
        "nvcc internal error: unexpected failure in capturing array variable");
};
```

**Per-dimension partial specializations (dimensions 2-8).** For each dimension D from 2 to 8, `sub_6BC290` generates a partial specialization with D `size_t` template parameters and a nested-for-loop constructor:

```cpp
// Example: 3D (v1 = 3)
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

**Field type trait specializations:**

```cpp
template <typename T>
struct __nv_lambda_field_type { typedef T type; };

// For each dimension D from 2 to 8:
template<typename T, size_t D1, ..., size_t DN>
struct __nv_lambda_field_type<T [D1]...[DN]> {
    typedef __nv_lambda_array_wrapper<T [D1]...[DN]> type;
};

template<typename T, size_t D1, ..., size_t DN>
struct __nv_lambda_field_type<const T [D1]...[DN]> {
    typedef const __nv_lambda_array_wrapper<T [D1]...[DN]> type;
};
```

The loop structure in `sub_6BC290` uses two stack buffers: `v33[1024]` for the nested-for-loop lines (each `sprintf` call formats four copies of the loop index variable) and `s[1064]` for dimension parameters and array subscript expressions. The outer loop runs from `v1 = 2` to `v1 = 8` (inclusive, 7 iterations). 1D arrays do not need a wrapper — they can be captured directly. Arrays of 9+ dimensions are unsupported (the primary template's `static_assert` fires).

See [Capture Handling](./capture-handling.md) for detailed documentation.

### Step 4: Primary `__nv_dl_wrapper_t` and Zero-Capture Specialization

```cpp
template <typename Tag, typename...CapturedVarTypePack>
struct __nv_dl_wrapper_t {
    static_assert(sizeof...(CapturedVarTypePack) == 0,
                  "nvcc internal error: unexpected number of captures!");
};

template <typename Tag>
struct __nv_dl_wrapper_t<Tag> {
    __nv_dl_wrapper_t(Tag) { }
    template <typename...U1>
    int operator()(U1...) { return 0; }
};
```

The primary template traps any instantiation with a non-zero capture count that lacks a matching specialization. The zero-capture specialization provides a trivial constructor and a dummy `operator()` returning `int(0)`. This return value is never used at runtime — the device compiler dispatches through the tag's encoded function pointer.

### Step 5: Trailing-Return Tag and Base Specialization

```cpp
template <typename U, U func, typename Return, unsigned>
struct __nv_dl_trailing_return_tag { };

template <typename U, U func, typename Return, unsigned Id>
struct __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id> > {
    __nv_dl_wrapper_t(__nv_dl_trailing_return_tag<U, func, Return, Id>) { }

    template <typename...U1> Return operator()(U1...) {
        __builtin_unreachable();
    }
};
```

For lambdas with explicit trailing return types (`-> ReturnType`), the tag carries the `Return` type as a template parameter. The `operator()` returns `Return` instead of `int`, with `__builtin_unreachable()` satisfying the compiler without generating actual return-value code.

The trailing-return tag and its zero-capture specialization are emitted as two separate `a1(...)` calls. The `__builtin_unreachable()` body is split: `a1("__builtin_unreachable(); }\n}; \n\n")`.

### Step 6: Device Lambda Bitmap Scan

Scans `unk_1286980` (the device lambda bitmap, 1024 bits) and calls `sub_6BB790` for each set bit with index greater than zero:

```c
// Decompiled from sub_6BCC20
v1 = (unsigned __int64 *)&unk_1286980;
v2 = 0;
do {
    v3 = *v1;                          // load 64-bit word
    v4 = v2 + 64;                      // word boundary
    do {
        if (v2 && (v3 & 1) != 0)       // skip bit 0, emit for set bits
            sub_6BB790(v2, a1);         // emit_device_lambda_wrapper_specialization
        ++v2;
        v3 >>= 1;
    } while (v4 != v2);
    ++v1;
} while (v4 != 1024);
```

Bit 0 is explicitly skipped (`if (v2 && ...)`). The zero-capture case is handled by the specializations in steps 4 and 5.

For each set bit N > 0, `sub_6BB790(N, a1)` emits two `__nv_dl_wrapper_t` partial specializations: one for `__nv_dl_tag` and one for `__nv_dl_trailing_return_tag`, each with N typed fields, a constructor taking N parameters, and an initializer list binding `inK` to `fK`. See [Device Lambda Wrapper](./device-wrapper.md) for full emitter logic.

This bitmap-driven approach is the critical compile-time optimization. A translation unit using lambdas with capture counts 1, 3, and 5 emits exactly 6 struct specializations rather than 2046 (1023 counts x 2 tag variants).

### Step 7: Host-Device Helper Class (`__nv_hdl_helper`)

Emitted inside an anonymous namespace:

```cpp
namespace {
template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
struct __nv_hdl_helper {
    typedef void * (*fp_copier_t)(void *);
    typedef OpFuncR (*fp_caller_t)(void *, OpFuncArgs...);
    typedef void (*fp_deleter_t)(void *);
    typedef OpFuncR (*fp_noobject_caller_t)(OpFuncArgs...);

    static fp_copier_t fp_copier;
    static fp_deleter_t fp_deleter;
    static fp_caller_t fp_caller;
    static fp_noobject_caller_t fp_noobject_caller;
};

// Out-of-line static member definitions (4 members):
template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
typename __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_copier_t
    __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_copier;

// ... (fp_deleter, fp_caller, fp_noobject_caller follow the same pattern)
}
```

The anonymous namespace prevents ODR violations across TUs. The `Tag` parameter isolates function pointer storage per lambda site even when call signatures are identical. The entire struct definition plus all four out-of-line member definitions are emitted as a single `a1(...)` call.

| Pointer | Purpose |
|---|---|
| `fp_copier` | Heap-copies a `Lambda` from `void*` (used by copy constructor) |
| `fp_caller` | Casts `void*` to `Lambda*` and invokes `operator()` |
| `fp_deleter` | Casts `void*` to `Lambda*` and `delete`s it |
| `fp_noobject_caller` | Stores captureless lambda as raw function pointer |

### Step 8: Primary `__nv_hdl_wrapper_t`

```cpp
template <bool IsMutable, bool HasFuncPtrConv, bool NeverThrows,
          typename Tag, typename OpFunc, typename...CapturedVarTypePack>
struct __nv_hdl_wrapper_t {
    static_assert(sizeof...(CapturedVarTypePack) == 0,
        "nvcc internal error: unexpected number of captures "
        "in __host__ __device__ lambda!");
};
```

Same safety-net pattern as the device wrapper.

### Step 9: Host-Device Lambda Bitmap Scan

Scans `unk_1286900` (the host-device bitmap, 1024 bits). Unlike the device scan, this loop does **not** skip bit 0 — the zero-capture host-device case still requires distinct specializations for `HasFuncPtrConv=true` vs `HasFuncPtrConv=false`.

For each set bit N, four specialization calls are made:

```c
v5 = (unsigned __int64 *)&unk_1286900;
v6 = 0;
do {
    v7 = *v5;
    v8 = v6 + 64;
    do {
        while ((v7 & 1) == 0) {        // fast-skip unset bits
            ++v6;
            v7 >>= 1;
            if (v6 == v8) goto LABEL_13;
        }
        sub_6BBB10(0, v6, a1);         // IsMutable=false, HasFuncPtrConv=false
        sub_6BBEE0(0, v6, a1);         // IsMutable=true,  HasFuncPtrConv=false
        sub_6BBB10(1, v6, a1);         // IsMutable=false, HasFuncPtrConv=true
        v9 = v6++;
        v7 >>= 1;
        sub_6BBEE0(1, v9, a1);        // IsMutable=true,  HasFuncPtrConv=true
    } while (v6 != v8);
LABEL_13:
    ++v5;
} while (v6 != 1024);
```

Note the ordering asymmetry in the fourth call: `sub_6BBEE0(1, v9, a1)` uses the pre-increment value `v9` because `v6` has already been incremented by the `v9 = v6++` expression.

The inner `while ((v7 & 1) == 0)` loop provides fast skipping over consecutive unset bits without executing four function calls per zero bit. This is an optimization compared to the device scan loop.

| Call | `a1` | `a2` | IsMutable | HasFuncPtrConv | `operator()` qualifier |
|---|---|---|---|---|---|
| `sub_6BBB10(0, N, emit)` | 0 | N | false | false | `const noexcept(NeverThrows)` |
| `sub_6BBEE0(0, N, emit)` | 0 | N | true | false | `noexcept(NeverThrows)` (no const) |
| `sub_6BBB10(1, N, emit)` | 1 | N | false | true | `const noexcept(NeverThrows)` |
| `sub_6BBEE0(1, N, emit)` | 1 | N | true | true | `noexcept(NeverThrows)` (no const) |

The sole difference between `sub_6BBB10` and `sub_6BBEE0` is that `sub_6BBB10` emits `"false,"` for `IsMutable` and adds `a3("const ")` before the `noexcept` qualifier on `operator()`, while `sub_6BBEE0` emits `"true,"` and omits the `const`. They are otherwise structurally identical — 238 vs 236 lines, the 2-line difference being exactly the `a3("const ")` call.

See [Host-Device Lambda Wrapper](./host-device-wrapper.md) for the complete internal structure of each specialization.

### Step 10: `__nv_hdl_helper_trait_outer` (Base Specializations)

The deduction helper trait that extracts the wrapper type from a lambda's `operator()` signature:

```cpp
template <bool IsMutable, bool HasFuncPtrConv, typename ...CaptureArgs>
struct __nv_hdl_helper_trait_outer {
    template <typename Tag, typename Lambda>
    struct __nv_hdl_helper_trait
        : public __nv_hdl_helper_trait<Tag, decltype(&Lambda::operator())> { };

    // Match const operator() (non-mutable lambda):
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...) const> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, false,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };

    // Match non-const operator() (mutable lambda):
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...)> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, false,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };
```

The primary `__nv_hdl_helper_trait` inherits from a specialization on `decltype(&Lambda::operator())`. The compiler deduces the member function pointer type and pattern-matches against the const or non-const specialization. Both produce `NeverThrows=false`.

This block is emitted without a closing `};` — the noexcept variants (step 11) are conditionally appended before the closing brace.

### Step 11: C++17 Noexcept Trait Variants (Conditional)

Gated on `dword_126E270`:

```c
if (dword_126E270)
    a1(/* noexcept trait specializations */);
a1("\n};");  // close __nv_hdl_helper_trait_outer
```

When C++17 noexcept-in-type-system is active, two additional `__nv_hdl_helper_trait` specializations are emitted:

```cpp
    // Match const noexcept operator():
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...) const noexcept> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, true,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };

    // Match non-const noexcept operator():
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...) noexcept> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, true,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };
```

The noexcept specializations produce `NeverThrows=true`. In C++17, `R(C::*)(Args...) const noexcept` is a distinct type from `R(C::*)(Args...) const`, so without these specializations, noexcept lambdas would fail to match and the trait chain would break.

### Step 12: `__nv_hdl_create_wrapper_t` Factory

```cpp
template<bool IsMutable, bool HasFuncPtrConv, typename Tag,
         typename...CaptureArgs>
struct __nv_hdl_create_wrapper_t {
    template <typename Lambda>
    static auto __nv_hdl_create_wrapper(Lambda &&lam, CaptureArgs... args)
        -> decltype(
            __nv_hdl_helper_trait_outer<IsMutable, HasFuncPtrConv, CaptureArgs...>
                ::template __nv_hdl_helper_trait<Tag, Lambda>
                ::get(lam, args...))
    {
        typedef decltype(
            __nv_hdl_helper_trait_outer<IsMutable, HasFuncPtrConv, CaptureArgs...>
                ::template __nv_hdl_helper_trait<Tag, Lambda>
                ::get(lam, args...)) container_type;
        return container_type(Tag{}, std::move(lam), args...);
    }
};
```

This factory is the entry point called at each host-device lambda usage site. The trailing return type chains through the trait hierarchy to deduce the exact `__nv_hdl_wrapper_t` specialization. The body constructs the deduced wrapper with `Tag{}`, the moved lambda, and the capture arguments.

### Step 13: CV-Removal Traits

```cpp
template<typename T> struct __nv_lambda_trait_remove_const { typedef T type; };
template<typename T> struct __nv_lambda_trait_remove_const<T const> { typedef T type; };

template<typename T> struct __nv_lambda_trait_remove_volatile { typedef T type; };
template<typename T> struct __nv_lambda_trait_remove_volatile<T volatile> { typedef T type; };

template<typename T> struct __nv_lambda_trait_remove_cv {
    typedef typename __nv_lambda_trait_remove_const<
        typename __nv_lambda_trait_remove_volatile<T>::type>::type type;
};
```

These are distinct from the `__nvdl_remove_ref`/`__nvdl_remove_const` emitted in step 1. The step-1 traits are used during capture type normalization at wrapper call sites. The step-13 traits are used by the detection macros (steps 14-17) to strip CV qualifiers before testing whether a type is an extended lambda wrapper.

### Step 14: Device Lambda Detection Trait

```cpp
template <typename T>
struct __nv_extended_device_lambda_trait_helper {
    static const bool value = false;
};

template <typename T1, typename...Pack>
struct __nv_extended_device_lambda_trait_helper<__nv_dl_wrapper_t<T1, Pack...> > {
    static const bool value = true;
};

#define __nv_is_extended_device_lambda_closure_type(X) \
    __nv_extended_device_lambda_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type>::value
```

SFINAE detection for device lambda wrappers. The macro strips CV qualifiers first, ensuring `const __nv_dl_wrapper_t<...>` is also detected. Used by CUDA runtime headers for conditional behavior on extended lambda types.

### Step 15: Device Lambda Wrapper Unwrapper

```cpp
template<typename T> struct __nv_lambda_trait_remove_dl_wrapper { typedef T type; };
template<typename T> struct __nv_lambda_trait_remove_dl_wrapper<__nv_dl_wrapper_t<T> > {
    typedef T type;
};
```

Extracts the inner tag type from a zero-capture device lambda wrapper. Only matches `__nv_dl_wrapper_t<T>` with a single template parameter (the tag). Used to access `__nv_dl_tag` or `__nv_dl_trailing_return_tag` for device function dispatch resolution.

### Step 16: Trailing-Return Device Lambda Detection

```cpp
template <typename T>
struct __nv_extended_device_lambda_with_trailing_return_trait_helper {
    static const bool value = false;
};

template <typename U, U func, typename Return, unsigned Id, typename...Pack>
struct __nv_extended_device_lambda_with_trailing_return_trait_helper<
    __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id>, Pack...> > {
    static const bool value = true;
};

#define __nv_is_extended_device_lambda_with_preserved_return_type(X) \
    __nv_extended_device_lambda_with_trailing_return_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type>::value
```

Detects whether a device lambda wrapper uses the trailing-return tag variant. Needed because trailing-return lambdas require different handling during device compilation — the return type is explicit and must be preserved, rather than deduced.

### Step 17: Host-Device Lambda Detection Trait

The final emission:

```cpp
template <typename>
struct __nv_extended_host_device_lambda_trait_helper {
    static const bool value = false;
};

template <bool B1, bool B2, bool B3, typename T1, typename T2, typename...Pack>
struct __nv_extended_host_device_lambda_trait_helper<
    __nv_hdl_wrapper_t<B1, B2, B3, T1, T2, Pack...> > {
    static const bool value = true;
};

#define __nv_is_extended_host_device_lambda_closure_type(X) \
    __nv_extended_host_device_lambda_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type>::value
```

Detects any `__nv_hdl_wrapper_t` instantiation. The partial specialization matches all six template parameters (`B1`=IsMutable, `B2`=HasFuncPtrConv, `B3`=NeverThrows, `T1`=Tag, `T2`=OpFunc, `Pack`=captures).

`sub_6BCC20` returns the result of this final `a1(...)` call.

## Bitmap Infrastructure

### Registration: sub_6BCBF0 (nv_record_capture_count)

During frontend parsing, `scan_lambda` (`sub_447930`) records each lambda's capture count:

```c
__int64 __fastcall sub_6BCBF0(int a1, unsigned int a2)
{
    unsigned __int64 *result;
    if (a1)
        result = (unsigned __int64 *)&unk_1286900;  // host-device bitmap
    else
        result = (unsigned __int64 *)&unk_1286980;  // device bitmap
    result[a2 >> 6] |= 1ULL << a2;
    return (__int64)result;
}
```

The function selects the bitmap based on `a1` (`0` = device, nonzero = host-device), computes the word index as `a2 >> 6` (divide by 64), and sets the bit via bitwise OR. No synchronization is needed because the frontend is single-threaded.

### Reset: sub_6BCBC0 (nv_reset_capture_bitmasks)

Before each translation unit, both bitmaps are zeroed:

```c
void sub_6BCBC0(void)
{
    memset(&unk_1286980, 0, 128);  // device bitmap
    memset(&unk_1286900, 0, 128);  // host-device bitmap
}
```

### Scan Algorithm Differences

| Aspect | Device scan (step 6) | Host-device scan (step 9) |
|---|---|---|
| Bitmap | `unk_1286980` | `unk_1286900` |
| Bit 0 | Skipped (`if (v2 && ...)`) | Processed |
| Skip strategy | Tests every bit individually | Inner `while` fast-skips consecutive zeros |
| Calls per set bit | 1 (`sub_6BB790`) | 4 (`sub_6BBB10` x2 + `sub_6BBEE0` x2) |
| Specializations per set bit | 2 (standard + trailing-return) | 4 (IsMutable x HasFuncPtrConv) |

The device scan skips bit 0 because the zero-capture case is handled by the always-emitted primary template. The host-device scan processes bit 0 because the zero-capture case requires explicit specializations for the `HasFuncPtrConv` and `IsMutable` dimensions — the always-emitted primary template contains only a `static_assert` trap.

## Complete Emission Order Summary

| Step | Content | Emitter | Templates Produced |
|---|---|---|---|
| 1 | Ref/const removal traits | inline string | `__NV_LAMBDA_WRAPPER_HELPER`, `__nvdl_remove_ref`, `__nvdl_remove_const` |
| 2 | Device tag | inline string | `__nv_dl_tag` |
| 3 | Array helpers | `sub_6BC290` | `__nv_lambda_array_wrapper` (dim 2-8), `__nv_lambda_field_type` specializations |
| 4 | Device wrapper primary | inline string | `__nv_dl_wrapper_t` primary + zero-capture |
| 5 | Trailing-return tag | inline string | `__nv_dl_trailing_return_tag` + zero-capture specialization |
| 6 | Device bitmap scan | loop + `sub_6BB790` | N-capture `__nv_dl_wrapper_t` (2 per set bit N > 0) |
| 7 | HD helper | inline string | `__nv_hdl_helper` (anonymous namespace, 4 static FPs) |
| 8 | HD wrapper primary | inline string | `__nv_hdl_wrapper_t` primary with static_assert |
| 9 | HD bitmap scan | loop + `sub_6BBB10` x2 + `sub_6BBEE0` x2 | N-capture `__nv_hdl_wrapper_t` (4 per set bit) |
| 10 | Trait outer | inline string | `__nv_hdl_helper_trait_outer` (const + non-const specializations) |
| 11 | C++17 noexcept | conditional inline | Noexcept `__nv_hdl_helper_trait` specializations |
| 12 | Factory | inline string | `__nv_hdl_create_wrapper_t` |
| 13 | CV traits | inline string | `__nv_lambda_trait_remove_const/volatile/cv` |
| 14 | Device detection | inline string | `__nv_extended_device_lambda_trait_helper` + macro |
| 15 | Wrapper unwrap | inline string | `__nv_lambda_trait_remove_dl_wrapper` |
| 16 | Trailing-return detection | inline string | `__nv_extended_device_lambda_with_trailing_return_trait_helper` + macro |
| 17 | HD detection | inline string | `__nv_extended_host_device_lambda_trait_helper` + macro |

## Output Size Characteristics

The preamble size depends on the number of distinct capture counts used:

| Component | Fixed/Variable | Approximate Size |
|---|---|---|
| Steps 1-5 (fixed templates) | Fixed | ~1.5 KB |
| Step 3 (array helpers, dim 2-8) | Fixed | ~4 KB |
| Step 6 (device, per capture count) | Variable | ~0.8 KB per count |
| Steps 7-8 (HD helper + primary) | Fixed | ~1.5 KB |
| Step 9 (HD, per capture count) | Variable | ~6 KB per count (4 specializations) |
| Steps 10-17 (traits, macros) | Fixed | ~3 KB |

A typical translation unit with 3-5 distinct capture counts produces approximately 30-50 KB of injected C++ text.

## Design Rationale

### Text Emission vs AST Construction

The preamble is emitted as raw C++ source text rather than constructed as AST nodes in the EDG IL. This trades correctness-by-construction for implementation simplicity:

- **Avoids IL complexity.** Constructing proper AST nodes for template partial specializations, static member definitions, anonymous namespaces, and macros would require deep integration with the EDG IL construction API.
- **Matches output format.** The `.int.c` file is plain C++ text consumed by the host compiler. Since the templates must eventually become text, generating them as text from the start eliminates a serialize-deserialize round trip.
- **Self-documenting.** The emitted text is directly readable in the `.int.c` file. `grep` for `__nv_dl_wrapper_t` to see exactly what was produced.

The cost is that the templates exist only as generated text, not as first-class IL entities. They cannot be analyzed or transformed by other EDG passes. This is acceptable because the preamble templates are infrastructure — they are never the target of user-facing diagnostics or transformations.

### Why Bitmaps Instead of Lists

The 1024-bit bitmap offers constant-time set (`O(1)` via shift-and-OR) and linear-time scan (`O(1024)` = effectively constant for a fixed-size structure). The bitmap has zero dynamic allocation, fits in two cache lines (128 bytes), and the scan loop compiles to simple shift-and-test instructions. Alternative representations (sorted lists, hash sets) would add allocation overhead and complexity for negligible benefit given the fixed 128-byte size.

### Why Bit 0 Is Skipped for Device but Not Host-Device

The device lambda zero-capture case is fully handled by the primary template's zero-capture specialization (step 4), which is always emitted. No per-capture-count specialization is needed because the zero-capture wrapper has no fields, no constructor parameters, and no specialization-specific behavior.

The host-device zero-capture case requires distinct specializations for `HasFuncPtrConv=true` (lightweight function pointer path) and `HasFuncPtrConv=false` (heap-allocated type erasure path). These paths have fundamentally different internal structure. The always-emitted primary template contains only a `static_assert` trap, not a working implementation, so bit 0 must be processed to generate the actual zero-capture specializations.

## Function Map

| Address | Name (recovered) | Source | Lines | Role |
|---|---|---|---|---|
| `sub_6BCC20` | `nv_emit_lambda_preamble` | `nv_transforms.c` | 244 | Master emitter: 17-step template injection pipeline |
| `sub_4864F0` | `gen_type_decl` | `cp_gen_be.c` | 751 | Trigger: detects sentinel, emits `#line`, calls master emitter |
| `sub_467E50` | `emit_string` | `cp_gen_be.c` | ~29 | Output callback: writes string char-by-char via `putc()` |
| `sub_467D60` | `emit_newline` | `cp_gen_be.c` | ~15 | Emits `\n`, increments line counter |
| `sub_6BC290` | `emit_array_capture_helpers` | `nv_transforms.c` | 183 | Step 3: `__nv_lambda_array_wrapper` for dim 2-8 |
| `sub_6BB790` | `emit_device_lambda_wrapper_specialization` | `nv_transforms.c` | 191 | Step 6: N-capture `__nv_dl_wrapper_t` (both tag variants) |
| `sub_6BBB10` | `emit_hdl_wrapper_nonmutable` | `nv_transforms.c` | 238 | Step 9: `__nv_hdl_wrapper_t<false,...>` specialization |
| `sub_6BBEE0` | `emit_hdl_wrapper_mutable` | `nv_transforms.c` | 236 | Step 9: `__nv_hdl_wrapper_t<true,...>` specialization |
| `sub_6BCBF0` | `nv_record_capture_count` | `nv_transforms.c` | 13 | Sets bit N in device or HD bitmap |
| `sub_6BCBC0` | `nv_reset_capture_bitmasks` | `nv_transforms.c` | 9 | Zeroes both 128-byte bitmaps before each TU |
| `sub_46BC80` | `emit_preprocessor_directive` | `cp_gen_be.c` | — | Emits `#if 0` / `#endif` suppression blocks |

## Global State

| Variable | Address | Type | Purpose |
|---|---|---|---|
| `unk_1286980` | `0x1286980` | `uint64_t[16]` | Device lambda capture-count bitmap (1024 bits) |
| `unk_1286900` | `0x1286900` | `uint64_t[16]` | Host-device lambda capture-count bitmap (1024 bits) |
| `dword_106BF38` | `0x106BF38` | `int32` | `--extended-lambda` mode flag |
| `dword_126E270` | `0x126E270` | `int32` | C++17 noexcept-in-type-system flag |
| `dword_126E1DC` | `0x126E1DC` | `int32` | EDG native mode flag (`#` vs `#line` format) |
| `dword_106581C` | `0x106581C` | `int32` | Output column counter |
| `dword_1065820` | `0x1065820` | `int32` | Output line counter (reset after preamble) |
| `qword_1065828` | `0x1065828` | `int64` | Output state pointer (reset after preamble) |
| `dword_1065818` | `0x1065818` | `int32` | Pending indentation flag |
| `dword_1065834` | `0x1065834` | `int32` | Preprocessor nesting depth counter |

## Related Pages

- [Extended Lambda Overview](./overview.md) — end-to-end pipeline architecture and bitmap system
- [Device Lambda Wrapper](./device-wrapper.md) — `__nv_dl_wrapper_t` template structure, `sub_6BB790` emitter
- [Host-Device Lambda Wrapper](./host-device-wrapper.md) — `__nv_hdl_wrapper_t` type erasure design, `sub_6BBB10`/`sub_6BBEE0`
- [Capture Handling](./capture-handling.md) — `__nv_lambda_field_type`, `__nv_lambda_array_wrapper`, `sub_6BC290`
- [Lambda Restrictions](./restrictions.md) — validation rules and error codes
