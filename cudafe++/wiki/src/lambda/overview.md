# Extended Lambda Overview

Extended lambdas are the most complex NVIDIA addition to the EDG frontend. Standard C++ lambdas produce closure classes with host linkage only -- they cannot appear in `__global__` kernel launches or `__device__` function calls because the closure type has no device-side instantiation. The `--extended-lambda` flag (`dword_106BF38`) enables a transformation pipeline that wraps each annotated lambda in a device-visible template struct, making the closure class callable across the host/device boundary.

Two wrapper types exist. `__nv_dl_wrapper_t` handles device-only lambdas (annotated `__device__`). `__nv_hdl_wrapper_t` handles host-device lambdas (annotated `__host__ __device__`). The wrappers are parameterized template structs that store captured variables as typed fields, providing the device compiler with a concrete, instantiatable type for each lambda's captures. The wrapper templates do not exist in any header file -- they are synthesized as raw C++ text and injected into the compilation stream by the backend code generator.

## Key Facts

| Property | Value |
|---|---|
| Enable flag | `dword_106BF38` (`--extended-lambda` / `--expt-extended-lambda`) |
| Source files | `class_decl.c` (scan), `nv_transforms.c` (emit), `cp_gen_be.c` (gen) |
| Device wrapper type | `__nv_dl_wrapper_t<Tag, CapturedVarTypePack...>` |
| Host-device wrapper type | `__nv_hdl_wrapper_t<IsMutable, HasFuncPtrConv, NeverThrows, Tag, OpFunc, CapturedVarTypePack...>` |
| Device bitmap | `unk_1286980` (128 bytes, 1024 bits) |
| Host-device bitmap | `unk_1286900` (128 bytes, 1024 bits) |
| Max captures supported | 1024 per wrapper type |
| `lambda_info` allocator | `sub_5E92A0` |
| Preamble injection marker | Type named `__nv_lambda_preheader_injection` |

## End-to-End Flow

The extended lambda system spans the entire cudafe++ pipeline -- from parsing through backend emission. Five major functions form the chain:

```
  FRONTEND (class_decl.c)              BACKEND (cp_gen_be.c + nv_transforms.c)
  ========================             ========================================

  sub_447930 scan_lambda               sub_47ECC0 gen_template (dispatcher)
       |                                    |
       +-- detect annotations               +-- sees __nv_lambda_preheader_injection
       |   (bits at lambda+25)              |
       +-- validate constraints             +-- sub_4864F0 gen_type_decl
       |   (35+ error codes)                |       triggers preamble emission
       |                                    |
       +-- record capture count             +-- sub_6BCC20 nv_emit_lambda_preamble
           in bitmap                        |       emits ALL __nv_* templates
                                            |
                                            +-- sub_47B890 gen_lambda
                                                    emits per-lambda wrapper call
```

### Stage 1: scan_lambda (`sub_447930`, 2113 lines)

The frontend entry point for all lambda expressions. Called from the expression parser when it encounters `[`. For extended lambdas, this function performs three critical operations:

1. **Execution space detection** -- Walks up the scope stack looking for `scope_kind == 17` (function body). Reads execution space byte at offset +182: bit 4 = `__device__`, bit 5 = `__host__`. Sets `can_be_host` and `can_be_device` flags.

2. **Annotation processing** -- Parses the `__nv_parent` specifier (NVIDIA extension for closure-to-parent linkage) and `__host__`/`__device__` attribute annotations on the lambda expression itself. Sets decision bits at `lambda_info + 25`.

3. **Validation** -- When `dword_106BF38` is set, validates that the lambda's execution space is compatible with its enclosing context. Emits errors 3592-3634 and 3689-3690 for violations. Records the capture count in the appropriate bitmap via `sub_6BCBF0`.

### Stage 2: Annotation Detection (Decision Bits)

The `scan_lambda` function sets bits at `lambda_info + 25` that control all downstream behavior:

| Bit | Mask | Meaning | Set when |
|---|---|---|---|
| bit 3 | `0x08` | Device lambda wrapper needed | Lambda has `__device__` annotation |
| bit 4 | `0x10` | Host-device lambda wrapper needed | Lambda has `__host__ __device__` |
| bit 5 | `0x20` | Has `__nv_parent` | `__nv_parent` pragma parsed in capture list |

Additional flags at `lambda_info + 24`:

| Bit | Mask | Meaning |
|---|---|---|
| bit 4 | `0x10` | Capture-default is `=` |
| bit 5 | `0x20` | Capture-default is `&` |

And at `lambda_info + 25` lower bits:

| Bit | Mask | Meaning |
|---|---|---|
| bit 0 | `0x01` | Is generic lambda |
| bit 1 | `0x02` | Has `__host__` execution space |
| bit 2 | `0x04` | Has `__device__` execution space |

### Stage 3: Preamble Trigger (`sub_4864F0`, gen_type_decl)

During backend code generation, `sub_47ECC0` (the master source sequence dispatcher) encounters a type declaration whose name matches `__nv_lambda_preheader_injection`. This sentinel type is never used by user code -- it exists solely as a trigger. When matched:

1. The backend emits `#line 1 "nvcc_internal_extended_lambda_implementation"`.
2. It calls `sub_6BCC20` (`nv_emit_lambda_preamble`) to inject the entire __nv_* template library.
3. It wraps the trigger type in `#if 0` / `#endif` so it never reaches the host compiler.

### Stage 4: Preamble Emission (`sub_6BCC20`, 244 lines)

This is the single point where all CUDA lambda support templates enter the compilation. It takes a `void(*emit)(const char*)` callback and emits raw C++ source text. The exact emission order, verified against the decompiled binary, is:

1. `__NV_LAMBDA_WRAPPER_HELPER` macro, `__nvdl_remove_ref` (with `T&`, `T&&`, `T(&)(Args...)` specializations), and `__nvdl_remove_const` trait helpers
2. `__nv_dl_tag` template (device lambda tag type)
3. Array capture helpers via `sub_6BC290` (`__nv_lambda_array_wrapper` primary + dimension 2-8 specializations, `__nv_lambda_field_type` primary + array/const-array specializations)
4. Primary `__nv_dl_wrapper_t` with `static_assert` + zero-capture `__nv_dl_wrapper_t<Tag>` specialization (emitted as a single string literal)
5. `__nv_dl_trailing_return_tag` definition + its zero-capture wrapper specialization with `__builtin_unreachable()` body (emitted as two consecutive string literals)
6. **Device bitmap scan** -- iterates `unk_1286980` (1024 bits). For each set bit N > 0, calls `sub_6BB790(N, emit)` to generate two `__nv_dl_wrapper_t` specializations (standard tag + trailing-return tag) for N captures
7. `__nv_hdl_helper` class (anonymous namespace, with `fp_copier`, `fp_deleter`, `fp_caller`, `fp_noobject_caller` static members + out-of-line definitions)
8. Primary `__nv_hdl_wrapper_t` with `static_assert`
9. **Host-device bitmap scan** -- iterates `unk_1286900` (1024 bits). For each set bit N (including 0), emits four wrapper specializations per N: `sub_6BBB10(0, N)` (non-mutable, HasFuncPtrConv=false), `sub_6BBEE0(0, N)` (mutable, HasFuncPtrConv=false), `sub_6BBB10(1, N)` (non-mutable, HasFuncPtrConv=true), `sub_6BBEE0(1, N)` (mutable, HasFuncPtrConv=true)
10. `__nv_hdl_helper_trait_outer` with `const` and non-const operator() specializations, plus conditionally (when `dword_126E270` is set for C++17 noexcept-in-type-system) `const noexcept` and non-const `noexcept` specializations -- all inside the same struct, closed by `\n};`
11. `__nv_hdl_create_wrapper_t` factory
12. Type trait helpers: `__nv_lambda_trait_remove_const`, `__nv_lambda_trait_remove_volatile`, `__nv_lambda_trait_remove_cv` (composed from the first two)
13. `__nv_extended_device_lambda_trait_helper` + `#define __nv_is_extended_device_lambda_closure_type(X)` (emitted together in one string)
14. `__nv_lambda_trait_remove_dl_wrapper` (unwraps device lambda wrapper to get inner tag)
15. `__nv_extended_device_lambda_with_trailing_return_trait_helper` + `#define __nv_is_extended_device_lambda_with_preserved_return_type(X)` (emitted together)
16. `__nv_extended_host_device_lambda_trait_helper` + `#define __nv_is_extended_host_device_lambda_closure_type(X)` (emitted together)

Note: each SFINAE trait and its corresponding detection macro are emitted as a single `a1()` call in the decompiled code, not as separate steps. The device bitmap scan skips bit 0 (zero-capture handled by step 4's specialization), but the host-device bitmap scan processes bit 0 (zero-capture host-device wrappers require distinct HasFuncPtrConv specializations).

### Stage 5: Per-Lambda Wrapper Emission (`sub_47B890`, gen_lambda, 336 lines)

For each lambda expression in the translation unit, the backend emits the wrapper call. The decision depends on the bits at `lambda_info + 25`:

**Device lambda** (bit 3 set, `byte[25] & 0x08`):

```cpp
__nv_dl_wrapper_t< /* closure type tag */ >(/* captured values */)
```

The original lambda body is wrapped in `#if 0` / `#endif` so it is invisible to the host compiler. The device compiler sees the wrapper struct which provides the captured values as typed fields.

**Host-device lambda** (bit 4 set, `byte[25] & 0x10`):

```cpp
__nv_hdl_create_wrapper_t<IsMutable, HasFuncPtrConv, Tag, CaptureTypes...>
    ::__nv_hdl_create_wrapper( /* lambda expression */, capture_args... )
```

The lambda expression is emitted inline as the first argument (binds to `Lambda &&lam` in the factory). The factory internally calls `std::move(lam)` when heap-allocating. Unlike the device lambda path, the original lambda body is NOT wrapped in `#if 0` -- it must be visible to both host and device compilers.

**Neither bit set** (plain lambda or `byte[25] & 0x06 == 0x02`):

Standard lambda emission with no wrapping. If `byte[25] & 0x06 == 0x02`, emits an empty body placeholder `{ }` with the real body in `#if 0` / `#endif`.

## Bitmap System

Rather than generating all 1024 possible capture-count specializations for each wrapper type, cudafe++ tracks which capture counts were actually used during frontend parsing. This is a critical compile-time optimization.

### Bitmap Layout

```
unk_1286980 (device lambda bitmap):
  128 bytes = 16 x uint64 = 1024 bits
  Bit N set  =>  __nv_dl_wrapper_t specialization for N captures is needed

unk_1286900 (host-device lambda bitmap):
  128 bytes = 16 x uint64 = 1024 bits
  Bit N set  =>  __nv_hdl_wrapper_t specializations for N captures are needed
```

### Bitmap Operations

| Function | Address | Operation |
|---|---|---|
| `nv_reset_capture_bitmasks` | `sub_6BCBC0` | Zeroes both 128-byte bitmaps. Called before each translation unit. |
| `nv_record_capture_count` | `sub_6BCBF0` | Sets bit `capture_count` in the appropriate bitmap. `a1 == 0` targets device, `a1 != 0` targets host-device. Implementation: `result[a2 >> 6] \|= 1LL << a2`. |
| Scan in `sub_6BCC20` | inline | Iterates each uint64 word, shifts right to test each bit, calls the wrapper emitter for each set bit. |

The scan loop in `sub_6BCC20` processes 64 bits at a time:

```c
uint64_t *ptr = (uint64_t *)&unk_1286980;
unsigned int idx = 0;
do {
    uint64_t word = *ptr;
    unsigned int limit = idx + 64;
    do {
        if (idx != 0 && (word & 1))
            emit_device_lambda_wrapper(idx, callback);  // sub_6BB790
        ++idx;
        word >>= 1;
    } while (limit != idx);
    ++ptr;
} while (limit != 1024);
```

Note that bit 0 is never emitted as a specialization -- the zero-capture case is handled by the primary template itself.

## The __nv_parent Pragma

`__nv_parent` is a NVIDIA-specific capture-list extension that provides closure-to-parent class linkage. It appears in the lambda capture list as a special identifier:

```cpp
auto lam = [__nv_parent = ParentClass, x, y]() __device__ { /* ... */ };
```

### Processing in scan_lambda

During capture list parsing (Phase 3 of `sub_447930`, around line 584):

1. The parser checks for a token matching the string `"__nv_parent"` at address `0x82e284`.
2. If found, calls `sub_52FB70` to resolve the parent class by name lookup.
3. Sets `lambda_info + 25 |= 0x20` (bit 5 = has `__nv_parent`).
4. Stores the resolved parent class pointer at `lambda_info + 32`.
5. If `__nv_parent` is specified more than once, emits error 3590.
6. If `__nv_parent` is specified without `__device__`, emits error 3634.

The `__nv_parent` class reference is used during device code generation to establish the relationship between the lambda's closure type and its enclosing class, which is necessary for the device compiler to properly resolve member accesses through the closure.

## lambda_info Structure

Allocated by `sub_5E92A0`. This is the per-lambda metadata node created during `scan_lambda` and consumed during backend generation.

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `captured_variable_list` | Head of linked list of capture entries |
| +8 | 8 | `closure_class_type_node` | Pointer to the closure class type in the IL |
| +16 | 8 | `call_operator_symbol` | Pointer to the `operator()` routine entity |
| +24 | 1 | `flags_byte_1` | bit 0 = has captures, bit 3 = `__host__`, bit 4 = `__device__`, bit 5 = has `__nv_parent`, bit 6 = is opaque, bit 7 = constexpr const |
| +25 | 1 | `flags_byte_2` | bit 0 = is generic, bit 1 = `__host__` exec space, bit 2 = `__device__` exec space, bit 3 = device wrapper needed, bit 4 = host-device wrapper needed, bit 5 = has `__nv_parent` |
| +32 | 8 | `__nv_parent_class` | Parent class pointer (NVIDIA extension) |
| +40 | 4 | `lambda_number` | Unique lambda index within scope |
| +44 | 4 | `source_location` | Source position of lambda expression |

## Key Functions

| Address | Name (recovered) | Source | Lines | Role |
|---|---|---|---|---|
| `sub_447930` | `scan_lambda` | `class_decl.c` | 2113 | Frontend: parse lambda, validate constraints, record capture count |
| `sub_42FE50` | `scan_lambda_capture_list` | `class_decl.c` | 524 | Frontend: parse `[...]` capture list, handle `__nv_parent` |
| `sub_42EE00` | `make_field_for_lambda_capture` | `class_decl.c` | 551 | Frontend: create closure class fields for captures |
| `sub_42D710` | `scan_lambda_capture_list` (inner) | `class_decl.c` | 1025 | Frontend: process individual capture entries |
| `sub_42F910` | `field_for_lambda_capture` | `class_decl.c` | ~200 | Frontend: resolve capture field via hash lookup |
| `sub_436DF0` | Lambda template decl helper | `class_decl.c` | 65 | Frontend: propagate execution space to call operator template |
| `sub_6BCC20` | `nv_emit_lambda_preamble` | `nv_transforms.c` | 244 | Backend: emit ALL `__nv_*` template infrastructure |
| `sub_6BB790` | `emit_device_lambda_wrapper_specialization` | `nv_transforms.c` | 191 | Backend: emit `__nv_dl_wrapper_t<Tag, F1..FN>` for N captures |
| `sub_6BBB10` | `emit_host_device_lambda_wrapper` (const) | `nv_transforms.c` | 238 | Backend: emit `__nv_hdl_wrapper_t` non-mutable variant |
| `sub_6BBEE0` | `emit_host_device_lambda_wrapper` (mutable) | `nv_transforms.c` | 236 | Backend: emit `__nv_hdl_wrapper_t` mutable variant |
| `sub_6BC290` | `emit_array_capture_helpers` | `nv_transforms.c` | 183 | Backend: emit `__nv_lambda_array_wrapper` for dim 2-8 |
| `sub_6BCBC0` | `nv_reset_capture_bitmasks` | `nv_transforms.c` | 9 | Init: zero both 128-byte bitmaps |
| `sub_6BCBF0` | `nv_record_capture_count` | `nv_transforms.c` | 13 | Record: set bit in device or host-device bitmap |
| `sub_6BCDD0` | `nv_find_parent_lambda_function` | `nv_transforms.c` | 33 | Query: find enclosing host/device function for nested lambda |
| `sub_6BC680` | `is_device_or_extended_device_lambda` | `nv_transforms.c` | 16 | Query: test if entity qualifies as device lambda |
| `sub_47B890` | `gen_lambda` | `cp_gen_be.c` | 336 | Backend: emit per-lambda wrapper construction call |
| `sub_4864F0` | `gen_type_decl` | `cp_gen_be.c` | 751 | Backend: detect preamble trigger, invoke emission |
| `sub_47ECC0` | `gen_template` (dispatcher) | `cp_gen_be.c` | 1917 | Backend: master source sequence dispatcher |
| `sub_489000` | `process_file_scope_entities` | `cp_gen_be.c` | 723 | Backend: entry point, emits lambda macro defines in boilerplate |

### Additional Recovered Function Names

The following EDG/NVIDIA function names appear as `__FUNCTION__` / assertion / log-tag strings in the binary. They are part of the lambda subsystem in `class_decl.c` but have not yet been mapped to specific `sub_*` addresses. Listed here so downstream agents can recognize them when crawling the binary:

| Recovered name | Subsystem role |
|---|---|
| `decl_call_operator_for_lambda` | Creates the `operator()` entity on the closure class and propagates execution-space bits from `lambda_info+25` (Stage 5 of the pipeline above). |
| `scan_lambda_declarator` | Parses the optional `(params)` declarator and the `mutable` / `constexpr` / execution-space specifiers between the capture list and the body. |
| `scan_optional_lambda_declarator` | Wrapper that handles the parameter-less form `[]{ ... }` where the declarator is elided. |
| `scan_lambda_template_param_list` | Parses C++20 explicit template parameter lists (`[]<typename T>(T x){...}`). |
| `finish_lambda_routine_processing` | Closes out `operator()` body processing, finalizes deduced return type, links the closure class into its enclosing scope. |
| `lambda_capture_for_variable` / `lambda_capture_for_init_capture` | IL constructors that allocate a capture node bound to a variable / init-capture expression. |
| `r_add_lambda_capture` / `add_lambda_capture` | Append a capture entry to the lambda's capture list (`r_`-prefix variant takes a resolved entity, plain variant takes a name to look up). |
| `make_field_for_lambda_capture` | Creates the closure-class field corresponding to a capture entry (sub_42EE00). |
| `field_for_lambda_capture` / `find_lambda_capture` | Look-up helpers that map a captured entity back to its closure-class field. |
| `enclosing_lambda_of` | Walks the scope stack to find the enclosing closure class given an entity defined inside a lambda body. |
| `generic_lambda_prototype` | Returns the template signature of a generic lambda's `operator()`. |
| `function_contains_generic_lambda` / `distinct_lambda_signatures` | Predicates queried by the device-code emitter to decide whether per-instantiation wrapper specializations are needed. |
| `nv_gen_extended_lambda_capture_types` | Backend emitter for the `F1..FN` template arguments (sub_46E640, see [Capture Handling](./capture-handling.md)). |

## Global State

| Variable | Address | Purpose |
|---|---|---|
| `dword_106BF38` | `0x106BF38` | Extended lambda mode flag (`--extended-lambda`) |
| `dword_106BF40` | `0x106BF40` | Lambda host-device mode flag |
| `unk_1286980` | `0x1286980` | Device lambda capture-count bitmap (128 bytes) |
| `unk_1286900` | `0x1286900` | Host-device lambda capture-count bitmap (128 bytes) |
| `qword_12868F0` | `0x12868F0` | Entity-to-closure mapping hash table |
| `dword_126E270` | `0x126E270` | C++17 noexcept-in-type-system flag (controls noexcept wrapper variants) |
| `qword_E7FEC8` | `0xE7FEC8` | Lambda hash table (Robin Hood, 16 bytes/slot, 1024 entries) |
| `ptr` (E7FE40 area) | `0xE7FE40` | Red-black tree root for lambda numbering per source position |
| `dword_E7FE48` | `0xE7FE48` | Red-black tree sentinel node |
| `dword_E85700` | `0xE85700` | `host_runtime.h` already included flag |
| `dword_106BDD8` | `0x106BDD8` | OptiX mode flag (triggers error 3689 on incompatible lambdas) |

### Related EDG Option Anchors

cudafe++ exposes several option keys that gate the lambda parser independently of `--extended-lambda`. They appear as raw strings in the binary and are surfaced through the standard EDG `--enable`/`--disable` infrastructure:

| String anchor | Role |
|---|---|
| `DEFAULT_LAMBDAS_ENABLED` | Default master switch for C++11 lambda parsing (independent of extended-lambda). Read by the front-end before `scan_lambda` is invoked. |
| `no_lambdas` | Diagnostic tag emitted when a lambda is parsed under a dialect where lambdas are disabled (pre-C++11 or `--no-lambdas`). |
| `disable_ext_lambda_cache` | EDG-level toggle that suppresses caching of extended-lambda resolution results across translation units (used in incremental rebuilds). |
| `extended-lambda` | The literal flag name string used by `--extended-lambda` / `--expt-extended-lambda` argument processing. |

## Concrete End-to-End Example

Consider a user writing this CUDA code with `--extended-lambda`:

```cpp
// user.cu
#include <cstdio>
__global__ void kernel(int *out) {
    int scale = 2;
    auto f = [=] __device__ (int x) { return x * scale; };
    out[threadIdx.x] = f(threadIdx.x);
}
```

Here is the transformation at each stage.

### Stage 1: scan_lambda detects the lambda

The frontend parser encounters `[=] __device__ (int x) { ... }`. `sub_447930` runs:

1. Finds `__device__` annotation on the lambda expression.
2. Sets `lambda_info + 25 |= 0x08` (bit 3: device wrapper needed) and `lambda_info + 25 |= 0x04` (bit 2: has `__device__` exec space).
3. Sets `lambda_info + 24 |= 0x10` (bit 4: capture-default is `=`).
4. Counts one capture (`scale`). Calls `sub_6BCBF0(0, 1)` to set bit 1 in the device bitmap `unk_1286980`.
5. Creates a closure class (compiler-generated name like `__lambda_17_16`) with one field of type `int` for the captured `scale`.

### Stage 2: Preamble injection

When the backend encounters the sentinel type `__nv_lambda_preheader_injection`, `sub_6BCC20` emits the template library. Because bit 1 is set in the device bitmap, it calls `sub_6BB790(1, emit)` which generates a one-capture specialization:

```cpp
template <typename Tag, typename F1>
struct __nv_dl_wrapper_t<Tag, F1> {
    typename __nv_lambda_field_type<F1>::type f1;
    __nv_dl_wrapper_t(Tag, F1 in1) : f1(in1) { }
    template <typename...U1>
    int operator()(U1...) { return 0; }
};

template <typename U, U func, typename Return, unsigned Id, typename F1>
struct __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id>, F1> {
    typename __nv_lambda_field_type<F1>::type f1;
    __nv_dl_wrapper_t(__nv_dl_trailing_return_tag<U, func, Return, Id>, F1 in1)
        : f1(in1) { }
    template <typename...U1>
    Return operator()(U1...) { __builtin_unreachable(); }
};
```

### Stage 3: Per-lambda wrapper emission

`sub_47B890` (gen_lambda) reads `byte[25] & 0x08` (device lambda flag is set) and emits the wrapper construction call. The lambda body is hidden from the host compiler:

```cpp
// Output in .int.c (what the host compiler sees):
__nv_dl_wrapper_t< __nv_dl_tag<
    __NV_LAMBDA_WRAPPER_HELPER(&__lambda_17_16::operator(), 0u)>,
    int>(
    __nv_dl_tag<
        __NV_LAMBDA_WRAPPER_HELPER(&__lambda_17_16::operator(), 0u)>{},
    scale)
#if 0
[=] __device__ (int x) { return x * scale; }
#endif
```

The `__NV_LAMBDA_WRAPPER_HELPER(X, Y)` macro expands to `decltype(X), Y`, giving the tag its two non-type parameters: the function pointer type and the pointer itself.

### What each compiler sees

**Host compiler** sees a `__nv_dl_wrapper_t<Tag, int>` struct with field `f1` holding the captured `scale`. The `operator()` returns `int(0)` (never actually called on host). The original lambda body is inside `#if 0`.

**Device compiler** sees the same wrapper struct but resolves the tag's encoded function pointer `&__lambda_17_16::operator()` to call the actual lambda body. The wrapper's `f1` field provides the captured `scale` value.

## Architecture: Text Template Approach

NVIDIA's lambda support uses a raw text emission pattern rather than constructing AST nodes. The template infrastructure is generated as C++ source text strings, passed through a callback function:

```c
emit("template <typename Tag, typename...CapturedVarTypePack>\n"
     "struct __nv_dl_wrapper_t {\n"
     "static_assert(sizeof...(CapturedVarTypePack) == 0,"
     "\"nvcc internal error: unexpected number of captures!\");\n"
     "};\n");
```

This text is emitted to the `.int.c` output file and subsequently parsed by the host compiler. The device compiler receives the same text through a parallel path. This design is architecturally simpler than building proper AST nodes for the wrapper templates, at the cost of the templates existing only as generated text rather than first-class IL entities.

The preamble injection point is controlled by a sentinel type declaration: when the backend encounters a type named `__nv_lambda_preheader_injection`, it emits the entire template library and wraps the sentinel in `#if 0`. This guarantees the templates appear exactly once, before any lambda expression that references them, regardless of declaration ordering in the user's source.

## Related Pages

- [Device Lambda Wrapper](./device-wrapper.md) -- `__nv_dl_wrapper_t` template structure in detail
- [Host-Device Lambda Wrapper](./host-device-wrapper.md) -- `__nv_hdl_wrapper_t` type-erased design
- [Capture Handling](./capture-handling.md) -- `__nv_lambda_field_type`, `__nv_lambda_array_wrapper`
- [Preamble Injection](./preamble-injection.md) -- `sub_6BCC20` emission pipeline step by step
- [Lambda Restrictions](./restrictions.md) -- 35+ error categories and validation rules
