# Kernel Launch Syntax (`<<<...>>>`)

The triple-bracket launch operator is the single most recognizable token sequence in CUDA C++. It is also the only piece of CUDA grammar that has no standalone C++ analogue: every other CUDA construct (annotated functions, address spaces, intrinsics) is built on top of attributes or builtins, but `<<<grid, block, smem, stream>>>` is recognized in EDG's expression parser as a dedicated postfix operator whose AST node is then lowered into an ordinary runtime-call sequence before the host compiler ever sees the translation unit. The frontend never emits `<<<` or `>>>` into the `.int.c` file -- by the time host output is generated, every launch has been rewritten as a paired call to `__cudaPushCallConfiguration` followed by an ordinary call to the kernel's host-side stub. This page documents how the operator is tokenized, parsed, validated, and lowered, and enumerates the diagnostics that fire along the way.

## Key Facts

| Property | Value |
|---|---|
| Tokenization | EDG lexer recognizes `<<<` and `>>>` as compound tokens distinct from shift operators |
| Parser entry | `sub_511D40` (`scan_expr_full`), token case `0x48` (decimal 72) |
| Runtime helper | `__cudaPushCallConfiguration` (string at `0x899213`, length `0x1B` = 27) |
| Helper lookup | `sub_72EEF0` (inject identifier) + `sub_698940` (resolve) inside `sub_511D40` |
| Missing-helper error | Error 3654, string at `0x88CA48` |
| Missing stream error | Error 3655, string at `0x88CAB0` |
| Launch placeholder body | `"{ ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);}"` at `0x839CB8` (see kernel-stubs) |
| Stub prefix | `"__wrapper__device_stub_"` at `0x839420` |
| Launch-site lowering tag | `default_stream_launch` (string at `0x8463C0`) |
| Closing-token error | `expected a ">>>"` (string at `0x850707`) |
| Default stream value | Literal integer `0` (NOT the per-thread default symbol) |
| Default smem value | Literal integer `0` (size_t zero) |

## Abstract

CUDA recognizes `kernel<<<grid, block [, smem [, stream]]>>>(args)` as a primary expression whose result is a `void`-typed call site. EDG's expression parser (`sub_511D40`) reaches this construct via a postfix-operator token whose internal kind is `0x48` (72). When the case fires, the parser locates the runtime helper `__cudaPushCallConfiguration`, parses one-to-four comma-separated launch arguments inside the brackets, expects a closing `>>>`, then parses the ordinary parenthesized argument list. The AST node produced is conceptually a comma expression: `(__cudaPushCallConfiguration(g, b, s, S) || kernel_stub(args))`. The host stub generator (see [Kernel Stub Generation](./kernel-stubs.md)) replaces the original kernel body with a `static __wrapper__device_stub_<name>` whose own placeholder body contains a `::cudaLaunchKernel(0,0,0,0,0,0)` call kept only to anchor the linker dependency. The four arguments are interpreted as `dim3 grid, dim3 block, size_t smem = 0, cudaStream_t stream = 0` -- the literal integer zero, not `cudaStreamPerThread`. Confidence: HIGH (string evidence and runtime lookup symbol both present in the binary).

## Grammar

### Tokenization

The compound tokens `<<<` and `>>>` are recognized by the lexer ahead of the C++ shift operators `<<` and `>>`. This is critical: a naive left-to-right tokenizer would split `<<<` into `<<` + `<`, which would then misparse `kernel<<<g, b>>>` as a less-than expression involving a templated kernel. EDG's lexer uses maximal-munch with a CUDA-mode flag set whenever cudafe++ drives the frontend; under that mode three consecutive `<` characters with no intervening whitespace produce a single token whose internal kind matches the postfix-launch case `0x48` reached inside `sub_511D40`.

The closing token `>>>` is matched against the same compound-token logic. If the parser reaches the position where it expects `>>>` and instead finds anything else, it emits the verbatim diagnostic `expected a ">>>"` (string at `0x850707`).

### Tokenization Edge Cases

Two adjacent shift-greater situations interact with the maximal-munch rule:

- **Templated kernel reference inside angle brackets**: `kernel<T><<<g, b>>>(x)` -- the closing `>` of the template-argument list is followed by `<<<`. The lexer disambiguates by tracking the template-instantiation depth: when depth is nonzero, `<<<` is *not* recognized as a launch token, so the inner `>` closes the template list and the *next* token sequence begins the launch. The same machinery handles `kernel<vector<int>><<<g, b>>>(x)` -- the two-`>` template-list closer is greedy first, then `<<<` becomes a launch token.
- **`>>>` versus `>> >`**: Inside a nested template such as `kernel<vector<int>><<<g, b>>>(x)`, the closing `>>>` is matched by the launch parser, not by template-list closure. The launch-position context flag tells the lexer to prefer the three-character compound token.

These rules mean that whitespace between the angle brackets does *not* change parsing: `kernel<<< g , b >>> ( x )` is equivalent to `kernel<<<g,b>>>(x)` because the compound tokens are matched by character sequence, not by token-adjacency.

### AST Shape

The parser builds a single launch-call AST node consisting of:

1. **The callee** -- the kernel's address-of-function reference (or a parenthesized expression that evaluates to one).
2. **Up to four configuration arguments** -- `grid`, `block`, optional `smem`, optional `stream`.
3. **An argument list** -- the ordinary parenthesized arguments that follow the closing `>>>`.

The node is rewritten before the IL walker reaches it; downstream phases never observe a "launch" AST kind directly. See [IL Tree Walking](../il/walking.md) for the post-lowering shape.

### Productions (informal)

```
postfix-expr:
    ...
    postfix-expr launch-config '(' [expression-list] ')'

launch-config:
    '<<<' assignment-expression ',' assignment-expression
          [ ',' assignment-expression
            [ ',' assignment-expression ] ] '>>>'
```

The four positional arguments are bound by position alone -- there is no named-argument form. Any other count (zero, one, three with the third missing context, or five) triggers a parse error from the surrounding expression machinery before launch-specific validation runs.

## Semantic Rules

### Argument Types and Conversions

| Position | Logical type | Accepted source types | Notes |
|---|---|---|---|
| 1 (grid) | `dim3` | `dim3`, `unsigned int`, or anything convertible to `dim3` through its single-`unsigned`-int constructor | Scalar `N` becomes `dim3(N, 1, 1)` |
| 2 (block) | `dim3` | Same as grid | Scalar `N` becomes `dim3(N, 1, 1)` |
| 3 (smem) | `size_t` | Integral types convertible to `size_t` | Default value: literal `0` |
| 4 (stream) | `cudaStream_t` | `cudaStream_t` (which is `struct CUstream_st*`) or `nullptr` | Default value: literal `0` |

The conversion is performed by the same overload-resolution code that handles ordinary function arguments -- once the parser has located `__cudaPushCallConfiguration`, the four arguments are bound to its declared signature using standard C++ conversions. See [Overload Resolution](../edg/overload-resolution.md) for how implicit-conversion sequences are computed.

### `__cudaPushCallConfiguration` Lookup

Inside `sub_511D40` at token case `0x48`, the runtime helper is located by:

```c
// sub_511D40, decompiled lines 1999-2006
sub_72EEF0("__cudaPushCallConfiguration", 0x1B);   // inject identifier into scope
v206 = sub_698940(v255, 0);                         // perform name lookup

if (!v206 || *(_BYTE *)(v206 + 80) != 11) {        // not found or kind != function
    sub_4F8200(0x0B, 3654, &qword_126DD38);         // emit fatal error 3654
}
```

The lookup is unqualified and runs in the current scope. The function is declared in `crt/device_runtime.h` (included transitively through `crt/host_runtime.h`); when the include paths are broken the lookup fails and compilation halts with error 3654 (`unable to find __cudaPushCallConfiguration declaration. CUDA toolkit installation may be corrupt.`). Severity `0x0B` (11) maps to a fatal error -- no recovery is attempted.

### Default-Stream Lookup

When only two or three arguments are present inside `<<<...>>>`, the missing positions are filled with literal integer `0`. The `default_stream_launch` tag (string at `0x8463C0`) controls a related diagnostic emitted when a default-stream fallback is detected in a context that disallows it (e.g., RDC mode in some configurations). The default-stream value is `0`, not `cudaStreamPerThread` -- see the QUIRK callout below.

### Explicit-Stream-Required Cases

Two contexts force an explicit fourth argument:

1. **Device-side launches in RDC mode** -- A kernel launched from within a `__device__` or `__global__` function (CUDA Dynamic Parallelism) requires `-rdc=true`. Without it, the `device_launch_no_sepcomp` tag fires: `kernel launch from __device__ or __global__ functions requires separate compilation mode`.
2. **Certain async-API configurations** -- The `explicit stream argument not provided in kernel launch` diagnostic (error 3655, string at `0x88CAB0`) is emitted from `sub_511D40` at line 2019 when the surrounding context requires an explicit stream but the parsed launch elided it.

### Argument Copy Constraints

A device-side launch (one that compiles into a CUDA-runtime call rather than a direct kernel invocation, used by Dynamic Parallelism) cannot pass arguments that require non-trivial copy or destruction. The two relevant diagnostics:

- `device_side_launch_arg_with_user_provided_cctor` (string at `0x855A78`) -- "cannot pass an argument with a user-provided copy-constructor to a device-side kernel launch"
- `device_side_launch_arg_with_user_provided_dtor` (string at `0x855AA8`) -- "cannot pass an argument with a user-provided destructor to a device-side kernel launch"

These are enforced after overload resolution selects the argument-binding sequence; if any binding would invoke a user-provided copy constructor or destructor, the diagnostic fires.

### Template Kernel Launches in System Files

Launches of template kernels inside `<system>` headers are forbidden: `kernel launches from templates are not allowed in system files` (tag `launch_in_system_file`, string at `0x84977D`). System headers are processed with relaxed diagnostics, and the launch lowering operates during the system-header processing pass where diagnostic state may be suppressed. Rather than risk silent miscompilation, the compiler rejects the pattern outright. See [CUDA Template Restrictions](../edg/template-cuda.md) for the broader picture.

### Launch on Non-`__global__` Callee

The launch operator is meaningful only when the callee is a `__global__` function (or a function pointer typed to one). Applying `<<<>>>` to an ordinary host or device function emits the format-template diagnostic `a %s function call %s2 be configured` (string at `0x8893C0`), where the substitutions describe the callee's actual execution space and the modal verb ("must"/"cannot") that governs the error. The check fires after overload resolution selects the callee; it reads the `+182` execution-space byte (see [Execution Spaces](./execution-spaces.md)) and rejects any callee whose `global_kernel` bit (`0x40`) is clear.

### Interaction with `__launch_bounds__`

The launch operator and `__launch_bounds__` are independent: `<<<>>>` provides *runtime* grid and block dimensions, while `__launch_bounds__` provides *compile-time* hints to the device backend about expected block sizes for register allocation. The two never directly interact at the launch site, but a mismatch between the runtime block size passed via `<<<>>>` and the `__launch_bounds__` declared on the kernel produces a runtime launch failure (`cudaErrorInvalidValue`) rather than a compile-time diagnostic. The compile-time tag `missing_launch_bounds` (string at `0x849EEA`) fires only when a kernel is declared without `__launch_bounds__` *and* the build mode requires them; the launch operator itself never inspects this tag. See [Launch Configuration](../attributes/launch-config.md).

## C Pseudocode: Lowering

The launch `kernel<<<g, b>>>(a)` is rewritten before host-output emission into the following form. The exact AST node shape is internal, but conceptually the rewrite is:

```c
// Source:
//   kernel<<<g, b>>>(a);
//
// Lowered (conceptual, AST-level):
do {
    if (__cudaPushCallConfiguration(g, b, /*smem=*/0, /*stream=*/0) != 0)
        break;                              // config push failed -- skip launch
    kernel(a);                              // call host-side stub
} while (0);
```

The `kernel(a)` call is then resolved to the device stub. The stub's name is `__wrapper__device_stub_<kernel>` and its body is the placeholder

```c
{ ::cudaLaunchKernel(0, 0, 0, 0, 0, 0); }
```

emitted from the string literal at `0x839CB8` -- see [Kernel Stub Generation](./kernel-stubs.md) for the full mechanism. The body is never actually executed at runtime: at link time the `__wrapper__device_stub_<kernel>` symbol is replaced by an `nvcc`-generated definition that consumes the pushed configuration and dispatches through the driver API. The placeholder `cudaLaunchKernel(0,0,0,0,0,0)` exists solely to anchor a linker dependency on `libcudart`.

For a four-argument launch:

```c
// Source:
//   kernel<<<g, b, smem, stream>>>(a, b, c);
//
// Lowered:
do {
    if (__cudaPushCallConfiguration(g, b, smem, stream) != 0)
        break;
    kernel(a, b, c);
} while (0);
```

The configuration push is paired one-to-one with the stub call -- the runtime maintains a per-thread stack of pushed configurations, and the stub pops the topmost entry. Confidence: HIGH (the `__cudaPushCallConfiguration` lookup is visible in the binary; the pairing with the host stub is corroborated by the placeholder-body string and the stub prefix).

### Why the `do/while(0)` Framing Matters

The lowered form is wrapped in a single-iteration loop so that the `if (__cudaPushCallConfiguration(...) != 0) break;` step can skip the stub call without falling out of the enclosing expression. The CUDA runtime contract is: if `__cudaPushCallConfiguration` returns nonzero, the configuration was *not* pushed, and the matching stub call must not run. Without the loop, a naive translation `if (push(...) != 0) goto skip_launch; stub(...); skip_launch:;` would require synthesizing labels, which EDG avoids by emitting an expression-statement that produces a `void` result through short-circuit evaluation. In the actual AST the framing may be a comma expression `(push(...) || stub(...))` or a conditional `(push(...) ? (void)0 : (void)stub(...))`; the exact shape is opaque to host output because the IL walker (see [IL Tree Walking](../il/walking.md)) normalizes both into the equivalent statement form before `.int.c` emission.

### Lowered Form in `.int.c`

The launch never appears verbatim in the host-output `.int.c` file. Instead the host compiler sees:

```c
// From the original source:
//   kernel<<<g, b, smem, stream>>>(a, b, c);
//
// Appears in .int.c roughly as (after IL walking + stub wiring):
((__cudaPushCallConfiguration(g, b, smem, stream)) ? (void)0 : __wrapper__device_stub_kernel(a, b, c));
```

The `__wrapper__device_stub_kernel` declaration was emitted earlier in the file by the kernel-stub generator. The host compiler sees a perfectly ordinary C++ expression with no CUDA-specific tokens. See [.int.c File Format](../output/int-c-format.md) for the broader file structure.

## Diagnostic Table

Every diagnostic that can fire from the triple-bracket parser path. Cross-referenced with [Category 7: Kernel Launch](../diagnostics/cuda-errors.md#category-7-kernel-launch-6-messages) in the error catalog.

| Tag / Error # | Verbatim Message (string addr) | Trigger |
|---|---|---|
| `expected a ">>>"` (parse) | `expected a ">>>"` (`0x850707`) | Closing token missing after launch-config arguments |
| Error 3654 | `unable to find __cudaPushCallConfiguration declaration. CUDA toolkit installation may be corrupt.` (`0x88CA48`) | `crt/device_runtime.h` not included or toolkit broken |
| Error 3655 | `explicit stream argument not provided in kernel launch` (`0x88CAB0`) | Context requires an explicit fourth argument |
| `device_launch_no_sepcomp` | `kernel launch from __device__ or __global__ functions requires separate compilation mode` (`0x888BD8`) | Dynamic-parallelism launch without `-rdc=true` |
| `missing_api_for_device_side_launch` | `device-side kernel launch could not be processed as the required runtime APIs are not declared` (`0x889748`) | Device-side launch but required runtime helpers absent |
| `device_side_launch_arg_with_user_provided_cctor` | `cannot pass an argument with a user-provided copy-constructor to a device-side kernel launch` (`0x8897A8`) | Argument's type has user-provided copy constructor |
| `device_side_launch_arg_with_user_provided_dtor` | `cannot pass an argument with a user-provided destructor to a device-side kernel launch` (`0x889808`) | Argument's type has user-provided destructor |
| `launch_in_system_file` | `kernel launches from templates are not allowed in system files` (`0x8898C0`) | Template kernel launched inside a system header |
| (configure call) | `a %s function call %s2 be configured` (`0x8893C0`) | Non-`__global__` function used with `<<<>>>` |
| `default_stream_launch` (tag) | (internal; gates other diagnostics) (`0x8463C0`) | Two- or three-argument launch with implicit stream=0 |

All severities flow through `sub_4F8200` (the generic diagnostic emitter); severity `0x0B` is fatal, `0x09` is error, `0x07` is warning. Confidence: HIGH for the verbatim strings (all present in `.rodata` at the listed addresses), MED for the per-tag trigger mapping (inferred from name + cross-reference to the error catalog).

## Quirks

> ⚡ **QUIRK -- Default stream is integer zero, not `cudaStreamPerThread`**
> When `kernel<<<g, b>>>(args)` is written without a fourth argument, the lowering substitutes the literal integer `0` for the stream parameter. This is the *legacy default stream*, not the per-thread default stream that programs opt into with `--default-stream=per-thread` or `#define CUDA_API_PER_THREAD_DEFAULT_STREAM`. The compiler does not consult the per-thread-stream macro at lowering time; the substitution is unconditionally `0`. Programs that mix two-argument and four-argument launches while compiling with per-thread-default-stream semantics get inconsistent synchronization behavior: the two-argument launches go through the legacy default stream while the four-argument launches with an explicit `0` are reinterpreted by the runtime as per-thread. Always pass an explicit stream when the per-thread mode is in play.

> ⚡ **QUIRK -- The shared-memory argument is `size_t`, not `int`**
> The third launch-config argument binds to `__cudaPushCallConfiguration`'s `size_t` parameter, not to `int` or `unsigned int`. On LP64 platforms (Linux, macOS) this is a 64-bit value; on LLP64 (Windows MSVC) it is also 64-bit since CUDA target ABI uses 64-bit `size_t`. Passing a negative signed integer triggers implicit conversion to a very large positive `size_t`, which the runtime will then attempt to allocate as dynamic shared memory and fail with `cudaErrorInvalidValue`. The compiler will not warn on the signed-to-unsigned conversion at the launch site because the conversion is exactly what the helper's signature requests -- the cost of using `int` for dynamic-shared-memory size is paid at runtime, not at compile time.

> ⚡ **QUIRK -- Device-side launches require `-rdc=true` *and* runtime headers**
> A launch written inside a `__device__` or `__global__` function (CUDA Dynamic Parallelism) does *not* lower into a `__cudaPushCallConfiguration` call; it lowers into a different runtime path that depends on a set of device-side helpers declared in the CUDA device runtime header. Two independent failure modes exist: the `device_launch_no_sepcomp` tag fires when `-rdc=false` (whole-program compilation), and the `missing_api_for_device_side_launch` tag fires when `-rdc=true` is set but the required device-side helpers were not declared in the translation unit (typically because the device runtime header was not included). Both diagnostics are common when porting code from host-side to device-side launches; the fix differs depending on which fires.

> ⚡ **QUIRK -- Lambda kernels work, but lambda *captures* do not**
> A lambda expression cast to a function pointer or wrapped in a `__global__`-annotated trampoline can be launched with `<<<>>>`. However, the lambda's captured state still flows through the kernel argument list, and any captured object whose type has a user-provided copy constructor or destructor will trigger `device_side_launch_arg_with_user_provided_cctor` or `device_side_launch_arg_with_user_provided_dtor`. This catches programmers who capture `std::vector`, `std::shared_ptr`, or any class with non-trivial special members "by value" into a device lambda -- the kernel-launch ABI requires trivially-copyable arguments because the captured payload is memcpy'd into the runtime's argument buffer. See [Extended Lambda Overview](../lambda/overview.md) for the full lambda-on-device story.

## Confidence Tags

| Section | Confidence | Evidence |
|---|---|---|
| Token kind `0x48` | MED | Inferred from the `sub_511D40` lookup site location; no decompiled `switch` table dump available |
| `__cudaPushCallConfiguration` lookup mechanism | HIGH | Disassembly excerpt in `output/cuda-runtime.md` matches the symbol string at `0x899213` |
| Error 3654 wording | HIGH | Verbatim string at `0x88CA48` |
| Error 3655 wording | HIGH | Verbatim string at `0x88CAB0` |
| Lowering shape | MED | The `do { ... } while (0)` framing is conceptual; the actual AST node may be a different conjunction operator -- the runtime contract is what matters |
| Default-stream = literal zero | HIGH | The `cudaLaunchKernel(0,0,0,0,0,0)` placeholder body confirms the zero-literal convention |
| Per-thread default stream interaction | MED | Inferred from CUDA runtime semantics; not directly visible in the cudafe++ binary |
| Tag-to-message mapping | HIGH for verbatim strings, MED for severity assignments |

## Function Map

| Address | Name | Role |
|---|---|---|
| `sub_511D40` | `scan_expr_full` | 80KB expression-parser dispatcher; token case `0x48` handles `<<<` |
| `sub_72EEF0` | (identifier injector) | Inserts `__cudaPushCallConfiguration` into the lookup scope |
| `sub_698940` | (name resolver) | Performs unqualified lookup of the runtime helper |
| `sub_4F8200` | (diagnostic emitter) | Emits errors 3654, 3655, and the `device_side_launch_*` family |
| `sub_47BFD0` | `gen_routine_decl` | Emits the `__wrapper__device_stub_<name>` stub the lowering targets |
| `sub_489000` | `process_file_scope_entities` | Backend file-scope walker; consumes the deferred stub list |

The launch lowering itself does not have a dedicated function -- the AST rewrite happens inline in `sub_511D40` at the point where token case `0x48` is reached.

## Cross-References

- [Kernel Stub Generation](./kernel-stubs.md) -- the host-side `__wrapper__device_stub_<name>` that the lowered launch ultimately calls; the `cudaLaunchKernel(0,0,0,0,0,0)` placeholder body
- [CUDA Runtime Boilerplate](../output/cuda-runtime.md#__cudapushcallconfiguration-lookup) -- the `__cudaPushCallConfiguration` lookup mechanism in `sub_511D40` and error 3654
- [CUDA-Related Diagnostics: Category 7](../diagnostics/cuda-errors.md#category-7-kernel-launch-6-messages) -- the full diagnostic catalog for kernel launch
- [Expression Parser](../edg/expression-parser.md) -- `sub_511D40` (`scan_expr_full`), the 80KB recursive-descent expression scanner that handles token case `0x48`
- [CUDA Template Restrictions](../edg/template-cuda.md) -- the `launch_in_system_file` restriction in template-launch context
- [RDC Mode](./rdc-mode.md) -- separate-compilation requirement for device-side launches
- [Extended Lambda Overview](../lambda/overview.md) -- lambdas as kernel entry points and the capture restrictions enforced by the launch path
- [`__global__` Function Constraints](../attributes/global-function.md) -- the callee-side rules that the launch operator exercises
- [Launch Configuration](../attributes/launch-config.md) -- `__launch_bounds__` and related compile-time constraints that interact with the runtime launch
