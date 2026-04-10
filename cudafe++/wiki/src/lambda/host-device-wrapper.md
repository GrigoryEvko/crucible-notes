# Host-Device Lambda Wrapper

The `__nv_hdl_wrapper_t` template is cudafe++'s type-erased wrapper for `__host__ __device__` extended lambdas. Unlike the device-only `__nv_dl_wrapper_t` which is a simple aggregate of captured fields, the host-device wrapper must operate on both the host (through the host compiler) and the device (through ptxas). This dual requirement forces a fundamentally different design: the wrapper uses `void*`-based type erasure with a `manager<Lambda>` inner struct that provides `do_copy`, `do_call`, and `do_delete` operations as static function pointers. The Lambda type is known only inside the constructor -- after construction, all operations go through the type-erased function pointer table stored in `__nv_hdl_helper`.

A second, lightweight path exists for lambdas that have no captures and can convert to a raw function pointer. When `HasFuncPtrConv=true`, the wrapper skips heap allocation entirely and stores the lambda directly as a function pointer via `fp_noobject_caller`, providing a `operator __opfunc_t*()` conversion operator.

Both paths are generated as raw C++ source text by two nearly-identical emitter functions in `nv_transforms.c`: `sub_6BBB10` (non-mutable, `IsMutable=false`, `const operator()`) and `sub_6BBEE0` (mutable, `IsMutable=true`, non-const `operator()`). For each capture count N observed during frontend parsing, the preamble emitter (`sub_6BCC20`) calls both functions twice -- once with `HasFuncPtrConv=0` and once with `HasFuncPtrConv=1` -- producing four partial specializations per capture count per mutability, for a total of eight template specializations per capture count.

## Key Facts

| Property | Value |
|---|---|
| Full template signature | `__nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, NeverThrows, Tag, OpFunc, Captures...>` |
| Source file | `nv_transforms.c` (EDG 6.6) |
| Non-mutable emitter | `sub_6BBB10` (238 lines, `IsMutable=false`) |
| Mutable emitter | `sub_6BBEE0` (236 lines, `IsMutable=true`) |
| Helper class | `__nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>` (anonymous namespace) |
| Factory | `__nv_hdl_create_wrapper_t<IsMutable, HasFuncPtrConv, Tag, CaptureArgs...>` |
| Trait deduction | `__nv_hdl_helper_trait_outer<IsMutable, HasFuncPtrConv, CaptureArgs...>` |
| Bitmap | `unk_1286900` (128 bytes, 1024 bits) |
| Primary template static_assert | `"nvcc internal error: unexpected number of captures in __host__ __device__ lambda!"` |
| Specializations per capture count | 8 (2 mutability x 2 HasFuncPtrConv x 2 emitter functions) |
| Noexcept variants | Additional 2 trait specializations when `dword_126E270` is set (C++17) |

## Template Parameters

```cpp
template <bool IsMutable,       // false = const operator(), true = non-const
          bool HasFuncPtrConv,  // true = captureless, function pointer path
          bool NeverThrows,     // maps to noexcept(NeverThrows)
          typename Tag,         // unique tag type per lambda site
          typename OpFunc,      // operator() signature as R(Args...)
          typename... CapturedVarTypePack>  // captured variable types F1..FN
struct __nv_hdl_wrapper_t;
```

| Parameter | Role |
|---|---|
| `IsMutable` | Controls whether `operator()` is `const`. `false` for lambdas without `mutable` keyword (the common case), `true` for `mutable` lambdas. Emitted as `"false,"` by `sub_6BBB10` and `"true,"` by `sub_6BBEE0`. |
| `HasFuncPtrConv` | `true` when the lambda has no captures and can be implicitly converted to a function pointer. Enables the lightweight `fp_noobject_caller` path instead of heap allocation. Passed as `a1` to the emitter functions. |
| `NeverThrows` | Propagated to `noexcept(NeverThrows)` on `operator()`. Set to `true` only when `dword_126E270` is active (C++17 noexcept-in-type-system) and the lambda's `operator()` is declared `noexcept`. |
| `Tag` | A unique type tag generated per lambda call site, used to give each `__nv_hdl_helper` instantiation its own static function pointer storage. Same tag system as device lambdas. |
| `OpFunc` | The lambda's call signature decomposed as `OpFuncR(OpFuncArgs...)`. Used to type the function pointers in `__nv_hdl_helper` and the wrapper's `operator()`. |
| `CapturedVarTypePack` | `F1, F2, ..., FN` -- one type per captured variable. Each becomes a field `typename __nv_lambda_field_type<Fi>::type fi` in the wrapper struct. |

## The __nv_hdl_helper Class

Before any `__nv_hdl_wrapper_t` specialization is emitted, `sub_6BCC20` emits the `__nv_hdl_helper` class inside an anonymous namespace. This class holds the static function pointers that enable type erasure -- the Lambda type is known when the constructor assigns the pointers, but the `operator()`, copy constructor, and destructor access them without knowing the concrete Lambda type.

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

// Out-of-line static definitions (one per member, templated):
template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
typename __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_copier_t
    __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_copier;

template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
typename __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_deleter_t
    __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_deleter;

template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
typename __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_caller_t
    __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_caller;

template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
typename __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_noobject_caller_t
    __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_noobject_caller;
}
```

The anonymous namespace is critical: it gives each translation unit its own copy of the static function pointers, preventing ODR violations when multiple TUs use the same lambda tag type. The `Tag` parameter ensures that different lambda call sites within the same TU get independent function pointer storage even if they share the same `OpFuncR(OpFuncArgs...)` signature.

### Function Pointer Roles

| Pointer | Type | Set by | Used by | Purpose |
|---|---|---|---|---|
| `fp_copier` | `void*(*)(void*)` | Constructor (capturing path) | Copy constructor | Heap-allocates a new `Lambda` copy from `void*` buffer |
| `fp_caller` | `OpFuncR(*)(void*, OpFuncArgs...)` | Constructor (capturing path) | `operator()` | Casts `void*` back to `Lambda*` and invokes it |
| `fp_deleter` | `void(*)(void*)` | Constructor (capturing path) | Destructor | Casts `void*` to `Lambda*` and `delete`s it |
| `fp_noobject_caller` | `OpFuncR(*)(OpFuncArgs...)` | Constructor (non-capturing path) | `operator()` + conversion operator | Stores the lambda directly as a function pointer |

## The Capturing Path (HasFuncPtrConv=false)

When `HasFuncPtrConv=false` (the `a1=0` path in the emitter), the wrapper uses heap allocation for type erasure. This is the full-weight path for lambdas that capture state.

### Reconstructed Template (N captures, non-mutable)

The following is the complete C++ output reconstructed from `sub_6BBB10` with `a1=0` (HasFuncPtrConv=false) and `a2=N` captures:

```cpp
template <bool NeverThrows, typename Tag, typename OpFuncR,
          typename... OpFuncArgs, typename F1, typename F2, /* ...FN */>
struct __nv_hdl_wrapper_t<false, false, NeverThrows, Tag,
                           OpFuncR(OpFuncArgs...), F1, F2, /* ...FN */> {
    // --- Captured fields ---
    typename __nv_lambda_field_type<F1>::type f1;
    typename __nv_lambda_field_type<F2>::type f2;
    // ...
    typename __nv_lambda_field_type<FN>::type fN;

    typedef OpFuncR(__opfunc_t)(OpFuncArgs...);

    // --- Data member for type-erased lambda ---
    void *data;

    // --- Type erasure manager ---
    template <typename Lambda>
    struct manager {
        static void *do_copy(void *buf) {
            auto ptr = static_cast<Lambda *>(buf);
            return static_cast<void *>(new Lambda(*ptr));
        };
        static OpFuncR do_call(void *buf, OpFuncArgs... args) {
            auto ptr = static_cast<Lambda *>(buf);
            return (*ptr)(std::forward<OpFuncArgs>(args)...);
        };
        static void do_delete(void *buf) {
            auto ptr = static_cast<Lambda *>(buf);
            delete ptr;
        }
    };

    // --- Constructor: heap-allocate Lambda, register function pointers ---
    template <typename Lambda>
    __nv_hdl_wrapper_t(Tag, Lambda &&lam, F1 in1, F2 in2, /* ...FN inN */)
        : f1(in1), f2(in2), /* ...fN(inN), */
          data(static_cast<void *>(new Lambda(std::move(lam)))) {
        __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_copier
            = &manager<Lambda>::do_copy;
        __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_deleter
            = &manager<Lambda>::do_delete;
        __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_caller
            = &manager<Lambda>::do_call;
    }

    // --- Call operator: delegate through type-erased fp_caller ---
    OpFuncR operator()(OpFuncArgs... args) const noexcept(NeverThrows) {
        return __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>
            ::fp_caller(data, std::forward<OpFuncArgs>(args)...);
    }

    // --- Copy constructor: delegate through fp_copier ---
    __nv_hdl_wrapper_t(const __nv_hdl_wrapper_t &in)
        : f1(in.f1), f2(in.f2), /* ...fN(in.fN), */
          data(__nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>
               ::fp_copier(in.data)) { }

    // --- Move constructor: steal void* pointer ---
    __nv_hdl_wrapper_t(__nv_hdl_wrapper_t &&in)
        : f1(std::move(in.f1)), f2(std::move(in.f2)), /* ...fN(std::move(in.fN)), */
          data(in.data) { in.data = 0; }

    // --- Destructor: delegate through fp_deleter ---
    ~__nv_hdl_wrapper_t(void) {
        __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_deleter(data);
    }

    // --- Copy assignment: deleted ---
    __nv_hdl_wrapper_t & operator=(const __nv_hdl_wrapper_t &in) = delete;
};
```

### Key Design Decisions

**Heap allocation in constructor.** The lambda is `std::move`d into a heap-allocated copy via `new Lambda(std::move(lam))`. This erases the concrete type -- the wrapper only holds a `void*` afterward. The `manager<Lambda>` static methods are assigned to the `__nv_hdl_helper` static function pointers during construction, preserving the type information as function pointer values rather than as template parameters.

**Static function pointers instead of vtable.** Rather than using virtual functions, the wrapper stores the type-erasure operations in static function pointers on `__nv_hdl_helper`. This is an unconventional choice -- it means all wrappers with the same `Tag` share the same function pointer storage. This works because within a single translation unit, each tag corresponds to exactly one lambda closure type. The approach avoids vtable overhead (no virtual destructor, no vptr in the wrapper) at the cost of not being safe across multiple lambda types sharing a tag.

**Move constructor steals pointer.** The move constructor copies the `void* data` pointer and sets the source to `0` (null). The destructor unconditionally calls `fp_deleter(data)`, so a null data pointer after move must be handled by the deleter. Since `delete` on a null pointer is a no-op in C++, the moved-from wrapper's destructor call is safe.

**Copy assignment is deleted.** Only copy construction and move construction are supported. This avoids the complexity of managing the `void*` lifetime during assignment (which would require deleting the old data and copying the new).

### Zero-Capture Specialization

When `a2=0` (no captures), the emitter skips the field declarations and the field portions of the member initializer lists. The wrapper degenerates to holding only `void* data` with no `fN` fields. The constructor takes only `(Tag, Lambda&&)` with no capture arguments. The copy and move constructors handle only the `data` member.

## The Lightweight Path (HasFuncPtrConv=true)

When `HasFuncPtrConv=true` (the `a1=1` path), the lambda has no captures and can be implicitly converted to a raw function pointer. The emitter produces a drastically simpler wrapper:

```cpp
template <bool NeverThrows, typename Tag, typename OpFuncR,
          typename... OpFuncArgs>
struct __nv_hdl_wrapper_t<false, true, NeverThrows, Tag,
                           OpFuncR(OpFuncArgs...)> {
    typedef OpFuncR(__opfunc_t)(OpFuncArgs...);

    // --- Constructor: store lambda as function pointer ---
    template <typename Lambda>
    __nv_hdl_wrapper_t(Tag, Lambda &&lam) {
        __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_noobject_caller = lam;
    }

    // --- Call operator: invoke through stored function pointer ---
    OpFuncR operator()(OpFuncArgs... args) const noexcept(NeverThrows) {
        return __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>
            ::fp_noobject_caller(std::forward<OpFuncArgs>(args)...);
    }

    // --- Function pointer conversion operator ---
    operator __opfunc_t *() const {
        return __nv_hdl_helper<Tag, OpFuncR, OpFuncArgs...>::fp_noobject_caller;
    }

    // --- Copy assignment: deleted ---
    __nv_hdl_wrapper_t & operator=(const __nv_hdl_wrapper_t &in) = delete;
};
```

No `void* data` member. No `manager` struct. No heap allocation. No copy constructor, move constructor, or destructor (the compiler-generated defaults suffice). The lambda is stored directly as a function pointer in `fp_noobject_caller`, and the wrapper provides an implicit conversion to `__opfunc_t*` -- the raw function pointer type matching the lambda's signature.

This path is selected when `gen_lambda` (`sub_47B890`) detects that the lambda has no capture list (`*(_QWORD *)a1 == 0`, the capture head pointer is null) and the lambda does not use capture-default `=` (bit 4 at `byte[24]` is clear). Additional conditions involving `dword_126EFAC`, `dword_126EFA4`, and `qword_126EF98` (a version threshold at `0xEB27` = 60199, likely a CUDA toolkit version) gate this detection, suggesting the function-pointer conversion path was added in a specific toolkit release.

## Mutable vs Non-Mutable (sub_6BBB10 vs sub_6BBEE0)

The two emitter functions are structurally identical. The sole differences:

| Aspect | `sub_6BBB10` (non-mutable) | `sub_6BBEE0` (mutable) |
|---|---|---|
| First template bool emitted | `"false,"` | `"true,"` |
| `operator()` qualifier | `a3("const ")` before `noexcept` | No `"const "` emission |
| Binary difference | Line 190: emits `"const "` | Line 188: skips to `noexcept` |

In the decompiled binary, the two functions are 238 and 236 lines respectively. The 2-line difference is exactly the `a3("const ")` call present in `sub_6BBB10` but absent from `sub_6BBEE0`.

For a `mutable` lambda, the C++ standard says `operator()` is non-const, allowing the lambda body to modify captured-by-value variables. The wrapper faithfully propagates this: `sub_6BBEE0` generates `operator()` without the `const` qualifier. In the capturing path, this means the `do_call` function pointer invokes a non-const Lambda, which is sound because the lambda is heap-allocated and accessed through a mutable `void*`.

### Emitter Call Matrix

`sub_6BCC20` emits all four combinations for each set bit N in the host-device bitmap:

```c
sub_6BBB10(0, N, emit);  // IsMutable=false, HasFuncPtrConv=false
sub_6BBEE0(0, N, emit);  // IsMutable=true,  HasFuncPtrConv=false
sub_6BBB10(1, N, emit);  // IsMutable=false, HasFuncPtrConv=true
sub_6BBEE0(1, N, emit);  // IsMutable=true,  HasFuncPtrConv=true
```

This produces partial specializations for all eight combinations of `(IsMutable, HasFuncPtrConv, N-captures)` per set bitmap bit. The `NeverThrows` parameter remains a template parameter (not a partial-specialization value), handled at instantiation time.

## The __nv_hdl_helper_trait_outer Deduction Helper

After the per-capture-count specializations, `sub_6BCC20` emits a trait class that deduces the wrapper return type from the lambda's `operator()` signature:

```cpp
template <bool IsMutable, bool HasFuncPtrConv, typename ...CaptureArgs>
struct __nv_hdl_helper_trait_outer {
    // Primary: extract operator() signature via decltype(&Lambda::operator())
    template <typename Tag, typename Lambda>
    struct __nv_hdl_helper_trait
        : public __nv_hdl_helper_trait<Tag, decltype(&Lambda::operator())> { };

    // Specialization for const operator() (non-mutable lambda):
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...) const> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, false,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };

    // Specialization for non-const operator() (mutable lambda):
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...)> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, false,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };

    // C++17 noexcept variants (only when dword_126E270 is set):
    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...) const noexcept> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, true,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };

    template <typename Tag, typename C, typename R, typename... OpFuncArgs>
    struct __nv_hdl_helper_trait<Tag, R(C::*)(OpFuncArgs...) noexcept> {
        template <typename Lambda>
        static auto get(Lambda lam, CaptureArgs... args)
            -> __nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, true,
                                   Tag, R(OpFuncArgs...), CaptureArgs...>;
    };
};
```

The trick here is the primary `__nv_hdl_helper_trait` inheriting from a specialization on `decltype(&Lambda::operator())`. The compiler deduces the member function pointer type of `operator()`, which pattern-matches against one of the four specializations. The non-noexcept specializations pass `NeverThrows=false`; the noexcept specializations pass `NeverThrows=true`. This is how the `NeverThrows` template parameter gets its value -- through trait deduction, not through an explicit argument.

The C++17 noexcept variants are gated on `dword_126E270`. In C++17, `noexcept` became part of the type system, so `R(C::*)(Args...) noexcept` is a distinct type from `R(C::*)(Args...)`. Without the additional specializations, the compiler would fail to match noexcept member function pointers.

## The __nv_hdl_create_wrapper_t Factory

The factory struct ties everything together. It provides a single static method that the backend emits at each host-device lambda usage site:

```cpp
template <bool IsMutable, bool HasFuncPtrConv,
          typename Tag, typename... CaptureArgs>
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

The trailing return type uses `decltype` to invoke the trait chain and deduce the exact `__nv_hdl_wrapper_t` specialization. The body constructs that deduced type with `Tag{}` (a value-initialized tag), the moved lambda, and the capture arguments.

### Backend Emission at Lambda Call Site

When `gen_lambda` (`sub_47B890`) encounters a host-device lambda (bit 4 set at `byte[25]`), it emits:

```cpp
__nv_hdl_create_wrapper_t< IsMutable, HasFuncPtrConv, Tag, CaptureTypes... >
    ::__nv_hdl_create_wrapper( /* lambda expression */, capture_args... )
```

The `IsMutable` decision comes from `byte[24] & 0x02` (mutable keyword present). The `HasFuncPtrConv` decision involves three conditions tested in sequence:

1. The lambda has no captures (capture list head pointer is null).
2. If `dword_126EFAC` is set and `dword_126EFA4` is clear, and the toolkit version `qword_126EF98` is at most `0xEB27` (60199), `HasFuncPtrConv` is forced to `true`.
3. Otherwise, if the lambda has no capture-default `=` (bit 4 at `byte[24]` is clear), `HasFuncPtrConv` is `true`.

This logic is visible at `sub_47B890` lines 62-77 of the decompiled binary.

## SFINAE Detection Traits

At the end of the preamble, `sub_6BCC20` emits a detection trait and macro for identifying host-device lambda wrappers:

```cpp
template <typename>
struct __nv_extended_host_device_lambda_trait_helper {
    static const bool value = false;
};

template <bool B1, bool B2, bool B3, typename T1, typename T2, typename... Pack>
struct __nv_extended_host_device_lambda_trait_helper<
    __nv_hdl_wrapper_t<B1, B2, B3, T1, T2, Pack...>> {
    static const bool value = true;
};

#define __nv_is_extended_host_device_lambda_closure_type(X) \
    __nv_extended_host_device_lambda_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type>::value
```

This allows compile-time detection of whether a type is a host-device lambda wrapper, used internally by the CUDA runtime headers and by `nvcc` to apply special handling to extended host-device lambda closure types.

## Emission Sequence in sub_6BCC20

The host-device wrapper infrastructure is emitted in steps 7-12 of the 20-step preamble emission sequence:

| Step | Content | Function |
|---|---|---|
| 7 | `__nv_hdl_helper` class (anonymous namespace, 4 static function pointer members + out-of-line definitions) | `sub_6BCC20` inline |
| 8 | Primary `__nv_hdl_wrapper_t` with `static_assert` (catches unexpected capture counts) | `sub_6BCC20` inline |
| 9 | Per-capture-count specializations: for each bit N set in `unk_1286900`, emit 4 calls: `sub_6BBB10(0,N)`, `sub_6BBEE0(0,N)`, `sub_6BBB10(1,N)`, `sub_6BBEE0(1,N)` | `sub_6BBB10`, `sub_6BBEE0` |
| 10 | `__nv_hdl_helper_trait_outer` deduction helper (2 or 4 trait specializations depending on C++17) | `sub_6BCC20` inline |
| 11 | C++17 noexcept trait variants (conditional on `dword_126E270`) | `sub_6BCC20` inline |
| 12 | `__nv_hdl_create_wrapper_t` factory | `sub_6BCC20` inline |

The bitmap scan loop for host-device wrappers differs from the device-lambda loop in one important way: bit 0 IS emitted. The device-lambda loop skips bit 0 (the zero-capture case is handled by the primary template), but the host-device loop processes every set bit including 0. This is because the zero-capture host-device wrapper still requires distinct specializations for the `HasFuncPtrConv=true` and `HasFuncPtrConv=false` paths.

```c
// sub_6BCC20, host-device bitmap scan (decompiled)
v5 = (unsigned __int64 *)&unk_1286900;
v6 = 0;
do {
    v7 = *v5;
    v8 = v6 + 64;
    do {
        while ((v7 & 1) == 0) {    // skip unset bits
            ++v6;
            v7 >>= 1;
            if (v6 == v8) goto LABEL_13;
        }
        sub_6BBB10(0, v6, a1);     // non-mutable, HasFuncPtrConv=false
        sub_6BBEE0(0, v6, a1);     // mutable,     HasFuncPtrConv=false
        sub_6BBB10(1, v6, a1);     // non-mutable, HasFuncPtrConv=true
        v9 = v6++;
        v7 >>= 1;
        sub_6BBEE0(1, v9, a1);     // mutable,     HasFuncPtrConv=true
    } while (v6 != v8);
LABEL_13:
    ++v5;
} while (v6 != 1024);
```

## Comparison with Device Lambda Wrapper

| Aspect | `__nv_dl_wrapper_t` | `__nv_hdl_wrapper_t` |
|---|---|---|
| Type erasure | None -- concrete fields only | `void*` + `manager<Lambda>` function pointers |
| Heap allocation | Never | Yes (capturing path) or never (HasFuncPtrConv path) |
| Copy semantics | Trivially copyable aggregate | Custom copy ctor via `fp_copier`; copy assign deleted |
| Move semantics | Default | Custom move ctor stealing `void*`; moved-from nulled |
| Destructor | Trivial | Calls `fp_deleter(data)` |
| `operator()` body | `return 0;` / `__builtin_unreachable()` (placeholder) | Delegates through `fp_caller` or `fp_noobject_caller` |
| Function pointer conversion | Not supported | `operator __opfunc_t*()` when HasFuncPtrConv=true |
| Specializations per N | 1 (+ 1 trailing-return) | 4 (mutability x HasFuncPtrConv) per emitter pair = 8 total |
| Template params (partial spec) | `Tag, F1..FN` | `IsMutable, HasFuncPtrConv, NeverThrows, Tag, OpFuncR(OpFuncArgs...), F1..FN` |

The host-device wrapper is fundamentally more complex because it must produce a callable object that works on both host and device. The device-only wrapper can use placeholder operator bodies (`return 0`) because the device compiler sees the original lambda body through a different mechanism. The host-device wrapper must actually call the lambda through the type-erased function pointer table.

## Emitter Function Signature

Both `sub_6BBB10` and `sub_6BBEE0` share the same prototype:

```c
__int64 __fastcall sub_6BBB10(int a1, unsigned int a2,
                               void (__fastcall *a3)(const char *));
```

| Parameter | Role |
|---|---|
| `a1` | `HasFuncPtrConv` flag. `0` = full type-erased path. `1` = lightweight function pointer path. |
| `a2` | Number of captured variables (0 to 1023). |
| `a3` | Emit callback. Called with C++ source text fragments that are concatenated to form the output. |

The functions use a 1080-byte stack buffer (`v28[1080]`) for `sprintf` formatting of per-capture template parameters and field declarations. The buffer is large enough for field names up to `F1023` / `f1023` / `in1023` with surrounding template syntax.

## Key Functions

| Address | Name | Lines | Role |
|---|---|---|---|
| `sub_6BBB10` | `emit_hdl_wrapper_nonmutable` | 238 | Emit `__nv_hdl_wrapper_t<false, ...>` specialization |
| `sub_6BBEE0` | `emit_hdl_wrapper_mutable` | 236 | Emit `__nv_hdl_wrapper_t<true, ...>` specialization |
| `sub_6BCC20` | `nv_emit_lambda_preamble` | 244 | Master emitter; calls both for each bitmap bit |
| `sub_47B890` | `gen_lambda` | 336 | Per-lambda site emission of `__nv_hdl_create_wrapper_t::__nv_hdl_create_wrapper(...)` call |
| `sub_6BCBF0` | `nv_record_capture_count` | 13 | Sets bit in `unk_1286900` bitmap during frontend scan |
| `sub_6BCBC0` | `nv_reset_capture_bitmasks` | 9 | Zeroes both bitmaps before each TU |

## Global State

| Variable | Address | Purpose |
|---|---|---|
| `unk_1286900` | `0x1286900` | Host-device lambda capture-count bitmap (128 bytes, 1024 bits) |
| `dword_126E270` | `0x126E270` | C++17 noexcept-in-type-system flag; gates noexcept trait variants |
| `dword_126EFAC` | `0x126EFAC` | Influences HasFuncPtrConv deduction in `gen_lambda` |
| `dword_126EFA4` | `0x126EFA4` | Secondary gate for HasFuncPtrConv path |
| `qword_126EF98` | `0x126EF98` | Toolkit version threshold for HasFuncPtrConv (compared against `0xEB27`) |
| `dword_106BF38` | `0x106BF38` | Extended lambda mode flag (`--extended-lambda`) |

## Related Pages

- [Extended Lambda Overview](./overview.md) -- end-to-end pipeline and bitmap system
- [Device Lambda Wrapper](./device-wrapper.md) -- `__nv_dl_wrapper_t` simpler aggregate approach
- [Capture Handling](./capture-handling.md) -- `__nv_lambda_field_type` and array capture helpers
- [Preamble Injection](./preamble-injection.md) -- `sub_6BCC20` full emission sequence
- [Lambda Restrictions](./restrictions.md) -- validation rules and error codes
