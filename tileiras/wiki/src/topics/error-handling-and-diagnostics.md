# Error Handling and Diagnostics

## Abstract

Three error-handling layers cooperate across tileiras's compilation pipeline.
MLIR's diagnostic engine carries structured messages from verifier and pattern
sites through a context-anchored handler chain. The TileAS pass family layers a
soft-failure handshake on top of that engine so a broken pass can stop the
pipeline without throwing. The driver consumes accumulated diagnostic severity
into a small set of integer exit codes that the caller acts on. The result is
a system where a failed compile produces both a precise verbatim message for
the user and a machine-readable signal for downstream passes and embedding
hosts — without ever leaving the IR in a partially mutated state.

## The three layers

### MLIR diagnostic engine

Every user-visible error, warning, note, and remark produced by tileiras lives
inside a 208-byte `Diagnostic` body. Verifiers, parsers, conversion patterns,
pass drivers, and dialect-init routines all seed that body through one of
three constructors — the operation-aware `emitOpError` form, the
location-only `emitError` form, and the generic location-plus-severity form
— stream fragments into a 4-slot inline argument buffer, and rely on an
`InFlightDiagnostic` RAII wrapper to flush the body through a
context-registered handler at scope exit.

The handler chain is owned by the `MLIRContext`. A diagnostic produced inside
the pipeline locks the engine's pthread mutex, walks the intrusive handler
list, and offers the diagnostic body to each handler in turn. The first
handler that returns `true` consumes the diagnostic. If no handler consumes
it, the default handler prefixes `error: `, `warning: `, `note: `, or
`remark: ` on the formatted output (selecting from the severity class in the
packed flag word at offset `+0x10` of the body), renders each argument
through the argument-printer dispatch, and flushes to whichever
`raw_ostream` the body's sink points at — by default `llvm::errs()`.

The five canonical severity words that appear in the binary are `0x101`,
`0x103`, `0x104`, `0x302`, and `0x503`. The low byte names the class; bit 8
sets the op-name prefix; bit 9 marks a child trace note. A verifier failure
emitted through `emitOpError` writes `0x103` — Error with op prefix; a remark
that carries a stack-trace child writes `0x104`; the inliner emits `0x302`
and `0x503` when it walks call-context traces. The bit layout is documented
in detail on [Diagnostic ABI and Helpers](../mlir-infra/diagnostic-abi-and-helpers.md).

### TileAS pass-failure handshake

MLIR's pass-manager exposes `signalPassFailure()` for hard pass failures, but
the TileAS pass family wants a softer signal. Hopper and Blackwell pipelines
routinely contain loops that one pass cannot transform — a loop whose
producer/consumer graph is not pipelinable, for instance, or whose layout
does not match the target spec — and the next pass still has useful work to
do on the rest of the function. The fix is a one-byte handshake at offset
`+40` of each pass's `PassObject`: bit 2 (`0x04`) is the soft-failure flag.

A pass that decides it cannot complete its rewrite emits an MLIR diagnostic
first, then ORs `4` into its status word, then keeps walking or returns
`success()`. The pass manager treats `success()` as a normal return — the
next pass still runs — but a downstream pass that depends on this one's
output peeks at the status word and skips the dependent work. The bit is
cumulative within one pass run; the driver clears it before the pass starts
and inspects it once the pass returns. The full contract, including the
ordering rule that the diagnostic must always precede the bit-set, is
documented on [Pass-Failure Handshake](../mlir-infra/pass-failure-handshake.md).

### Driver-level exit codes

The driver's public C API exposes five non-zero exit codes. Each is a fixed
integer that the embedding host can switch on, paired with one of a small
catalog of verbatim diagnostic strings routed through the standard MLIR
engine. The numbering is stable across `tileirasProgramCreate`,
`tileirasProgramCompile`, `tileirasProgramGetOutput`, and
`tileirasProgramRelease`.

| Code | Class                       | Trigger                                            |
|------|-----------------------------|----------------------------------------------------|
| 0    | success                     | (no error)                                         |
| 1    | allocation failure          | program-handle allocation returned `NULL`          |
| 2    | configuration rejection     | null pointer, out-of-range option, unsupported GPU |
| 3    | bytecode parse failure      | magic or version mismatch on the input buffer      |
| 4    | handle-state rejection      | null or uncompiled handle passed to a getter       |
| 5    | compile failure             | pass manager returned `failure()`                  |

Code 2 covers every front-end configuration gate and uses severity `0x503`
(class 3 with the trace bit set). Codes 1 and 5 use severity `0x103` —
Error with op prefix — because the failure carries a structural message
about the IR or the allocator. Code 3 uses severity `0x104` (class 4, the
Remark flavour) with an MLIR-bytecode tail heuristic appending
`(it looks like MLIR bytecode instead)` when the input looks like an MLIR
container instead of a TileIR one. The full code catalog with the verbatim
strings is on [Driver Program Handle](../driver/program-handle.md#public-error-codes).

## Severity to behavior mapping

The packed severity byte at offset `+0x10` of the diagnostic body drives every
downstream decision. The table below collapses the per-layer behavior into a
single view.

| Class       | Engine behavior                            | Pass-failure layer        | Driver behavior                                  |
|-------------|---------------------------------------------|---------------------------|--------------------------------------------------|
| 1 — Note    | Attached to a parent; never printed alone   | Never sets the bit         | Never directly affects exit code                 |
| 2 — Warning | Printed with `warning: ` prefix             | Never sets the bit         | Returns 0; the user sees the text on stderr      |
| 3 — Error   | Printed with `error: ` prefix               | Pass typically sets bit 2 | Pipeline run returns `failure()`; exit code 5    |
| 4 — Remark  | Printed with `remark: ` prefix when enabled | May set the bit (soft miss) | Returns 0 unless paired with a separate Error  |

The Warning and Remark classes never alone cause a non-zero exit. A pass
that emits a Remark, sets bit 2 of its status word, and returns `success()`
produces no diagnostic the driver sees as fatal; the bit is for downstream
passes only, and the exit code is `0`. The driver returns a non-zero exit
code only when the pass manager itself returns `failure()`, which happens
when at least one Error-class diagnostic flushed through the engine.

## The verifier ladder

Three concentric verifier layers wrap each pass invocation. The innermost
fires while the pass is still mutating IR; the middle fires immediately
after; the outermost fires only when a named verifier pass reaches its slot.

The operation-name verifier is the layer 1 check. Op construction inside the
pass body — every `builder.create<...>(...)` call — implicitly runs the
verifier registered for that op name. Operand counts, result counts, region
counts, required attribute presence, and trait-driven type constraints all
fire here, before the constructed op has been linked into its parent. A
break at this layer typically propagates as an `InFlightDiagnostic` returned
from the rewrite, which the pattern driver flushes to the engine and turns
into a `failure()` return.

The between-pass verifier is layer 2. When `verify-each` is on (the default
for non-Release builds), the pass manager runs `verify(anchor, /*recursive=*/true)`
after every pass that returned `success()`. The catch is broader than layer
1: cross-op invariants — a use that escapes its defining region, a
terminator whose successor list does not line up with its target — show up
here even when the individual op constructions all passed.

The named-verifier-pass layer is layer 3. Three explicit verifier passes
appear in the pipeline at fixed slots: the TileIR operation analysis (before
LLVM conversion), the TileAA agent verifier (warp-specialized path), and
the NVVM IR verifier (after target conversion). These passes enforce
whole-module or target-context invariants that the lower layers cannot see —
the NVVM verifier's parameter-space ceiling is the canonical example,
because the ceiling depends on the resolved `#nvvm.target` attribute and the
verifier needs the post-conversion address-space metadata to walk the
parameter list. The full ladder is documented on
[Pipeline Invariants and Verifiers](../pipeline/invariants-and-verifiers.md).

## Verbatim diagnostic catalogs

The verbatim strings that flow through the engine are spread across the
per-dialect verifier pages. The catalog below points at each verifier's
canonical home; the strings themselves stay where they live so that the
verifier code and its diagnostics remain colocated.

| Layer / source                                                                                                  | Examples                                                                          |
|-----------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| [cuda_tile verifiers](../dialects/cuda_tile/verifiers.md)                                                       | `expect non-empty block`, `expect 0-rank tile type at index: N`                   |
| [nv_tileaa types/attrs/verifiers](../dialects/nv_tileaa/types-attrs-verifiers.md)                               | Tile-attr alignment, layout-shape invariants                                      |
| [nv_tileas verifiers](../dialects/nv_tileas/verifiers.md)                                                       | Memory-op ordering and schedule-region structure                                  |
| [cute verifiers](../dialects/cute/verifiers.md)                                                                 | Layout-algebra rank and stride consistency                                        |
| [cute_nvgpu mode-pattern verifiers](../dialects/cute_nvgpu/mode-pattern-verifiers.md)                           | Copy-atom mode patterns, atom-vs-SM compatibility                                 |
| [cute_nvgpu TMA atoms](../dialects/cute_nvgpu/tma-atoms.md)                                                     | Descriptor-shape and address-space rules                                          |
| [passes/tileas/tma-and-memops-family](../passes/tileas/tma-and-memops-family.md)                                | `LowerTMALoadStoreToAsync: missing or invalid KernelSpecAttr on function`         |
| [passes/tileas/async-pipeline-family](../passes/tileas/async-pipeline-family.md)                                | `Failed to pipeline loop`, `Alias is not expected here.`                          |
| [nvptx-passes/nvvm-ir-verifier](../nvptx-passes/nvvm-ir-verifier.md)                                            | `Formal parameter space overflowed (X bytes required, max Y bytes allowed) in function Z`, `a function that is not __global__ cannot be launched` |

The wording is part of the public contract. Frontends and tests key off the
exact string to distinguish "I emitted illegal IR" from "I hit a compiler
bug." A reimplementer must reproduce the strings verbatim or break
downstream tooling.

## Worked example: malformed kernel parameter buffer

Consider a kernel whose by-value parameter struct overflows the SM's
parameter-space ceiling. The trace below follows a single diagnostic from
emission through to exit code.

The user compiles a TileIR module containing the equivalent of

```c
struct Heavy {
    double  scale;       //   8 B
    char    tag;         //   1 B (+ 7 B padding)
    int     data[10000]; //  40000 B
};

__global__ void big_kernel(struct Heavy h) { /* ... */ }
```

against an sm_75 target. The early lowering passes promote `h` into a
parameter-space pointer; the front end accepts it because nothing earlier
in the pipeline knows the target's parameter-space ceiling. The NVVM IR
verifier eventually picks it up.

```c
// Inside NVVMIRVerifier (layer-3 verifier, runs after MLIR-to-LLVM lowering).
LogicalResult check_parameter_space(Function &fn, TargetInfo *target) {
    uint64_t total = 0;
    for (Argument &arg : fn.args()) {
        uint64_t sz = size_of_param(describe(arg), target);
        total = align_to(total, align_of(describe(arg), target));
        total += sz;
    }
    uint64_t limit = param_space_limit_for(target->sm);
    if (total > limit) {
        return fn.emitOpError()
            << "Formal parameter space overflowed ("
            << total << " bytes required, max "
            << limit << " bytes allowed) in function "
            << fn.getName();
    }
    return success();
}
```

The 21-tag NVVM sizer descends through `Heavy` and walks out at 40016 bytes.
For sm_75 the ceiling is 1024 bytes. The check fails, and `emitOpError`
runs.

`emitOpError` allocates a 208-byte `Diagnostic` body, zero-fills it, writes
the function's location to `+0x00`, writes packed severity `0x103` to
`+0x10` (Error with op-prefix), initializes the argument buffer at `+0x28`,
and streams in five fragments: the literal `Formal parameter space overflowed (`,
the integer `40016`, the literal ` bytes required, max `, the integer
`1024`, the literal ` bytes allowed) in function `, and finally the function
name `big_kernel`. The first four arguments fit in the inline slots; if the
function name pushed the count past four, the streamer would promote the
buffer to the heap and rewrite `args_begin`.

When the `InFlightDiagnostic` wrapper goes out of scope, its destructor
calls the engine entry. The engine takes its mutex, walks the registered
handler chain, and offers the body to each handler. The default handler
prints

```text
error: Formal parameter space overflowed (40016 bytes required, max 1024 bytes allowed) in function big_kernel
```

to `llvm::errs()`, then flushes the sink. The verifier itself follows the
`emitOpError` with `signalPassFailure()`, which marks the pass-manager
result as `failure()`.

Control returns up the stack. The NVVM verifier pass returns `failure()` to
the pass manager. The pass manager propagates `failure()` to
`tileirasProgramCompile`, which sees a non-success result, emits its own
generic `failed to compile Tile IR program` diagnostic at severity `0x103`,
and returns exit code 5. `main` propagates 5 to its caller. The user sees
both diagnostics on stderr, in emission order, and the calling tool can
distinguish "compile failed" from "input rejected" by inspecting the exit
code alone.

The same kernel compiled against sm_80 takes the same emission path through
the verifier, but the sizer compares 40016 against 32760 (the sm_80 limit),
still fails, and prints the limit appropriate to the target. The verifier
text is parametric on `target->sm`; the rest of the trace is identical.

## Failure modes

The table below catalogs the principal failure classes a user can hit,
ordered roughly by the pipeline depth at which they fire.

| Failure                                  | Layer            | What the user sees                                                               | Exit code |
|------------------------------------------|------------------|-----------------------------------------------------------------------------------|-----------|
| Bytecode magic mismatch                  | Driver, pre-pipeline | `input does not correspond to Tile IR bytecode` (with MLIR-tail suffix if matched) | 3         |
| Unsupported GPU / opt / host config       | Driver, pre-pipeline | `unsupported GPU target`, `invalid optimization level`, `unsupported host operating system` | 2     |
| Dialect not registered for op in bytecode | Parser            | MLIR's unresolved-dialect diagnostic on the offending op                          | 5         |
| Op verification failure (per-dialect)    | Layer 1 / 2      | The verbatim string from the relevant verifier page                               | 5         |
| Pass-failure soft handshake              | TileAS layer    | A per-pass diagnostic (`Failed to pipeline loop`, etc.); downstream passes skip dependent work | 0 or 5    |
| Kernel parameter-space overflow          | Layer 3 (NVVM)  | `Formal parameter space overflowed (X bytes required, max Y bytes allowed) in function Z` | 5     |
| Non-kernel device launch target          | Layer 3 (NVVM)  | `a function that is not __global__ cannot be launched`                            | 5         |
| Codegen catastrophe (unsupported ISel)   | LLVM backend     | LLVM's `report_fatal_error` text; abort signal                                    | abort     |
| Subprocess failure (`nvdisasm`, `ptxas`) | Driver post-pipeline | Wrapper's exit-status diagnostic                                                  | 5         |

Two failure modes do not produce a clean exit. A `report_fatal_error` from
the LLVM backend ends the process through `abort()`, not through `main`'s
return path; the driver cannot translate it into a friendly exit code
because the fatal-error handler runs ahead of any cleanup. The other is the
parser fall-through on a malformed dialect symbol, which produces a stderr
diagnostic but reaches the driver as a generic compile failure (code 5)
because the parser's failure surface is opaque to the driver wrapper.

The soft-handshake case is the only entry in the table whose exit code
depends on what other passes do. A pass that sets bit 2 and emits a Remark
produces exit code 0; the same pass emitting an Error produces exit code 5
even though the bit-set behavior is identical. The bit is for the pipeline;
the severity is for the user and the driver.

## Reimplementer notes

A reimplementation must preserve four structural invariants for the
error-handling architecture to round-trip with the recovered binary.

The `Diagnostic` body is exactly 208 bytes and packs severity into the
16-bit word at offset `+0x10` using the same class-in-low-byte plus
op-prefix-at-bit-8 plus trace-at-bit-9 encoding. The four-slot inline
argument buffer at offset `+0x28` must precede any heap spill; downstream
consumers walk `args_begin` at a 24-byte stride and rely on the small
buffer staying live until the spill threshold is crossed. The
`InFlightDiagnostic` RAII wrapper must flush through the engine on scope
exit unless the body's location pointer has been cleared by a move; the
double-flush guard is a single byte at the body's end and a corrupted byte
either drops the diagnostic or emits it twice.

The pass-failure handshake bit sits at offset `+40` of every TileAS
`PassObject` and means `soft failure` only when set as bit 2. The bit is
cumulative within one pass run, never cleared mid-run, and the driver
clears it once before pass entry. A pass-object layout that places the
status word at a different offset cannot participate in the handshake.

The driver-level exit codes form a flat namespace from 0 to 5. Codes are
assigned per call site, not per error category, so a single code can cover
multiple verbatim strings (code 4 covers every null-handle and
not-yet-compiled rejection) and a single error category can produce
different codes from different entry points (a null input pointer is code 2
during create and code 4 during get-output). The numbering is stable; the
strings change.

The interaction between the layers is one-directional: a layer-1 verifier
failure surfaces as a layer-2 `failure()` return, which becomes a layer-3
`failure()` on the pass-manager handle, which becomes exit code 5 from
`main`. No backward propagation. The bit-set handshake is the only path
that does not propagate failure upward — a pass that sets the bit and
returns `success()` is, from the layer above, indistinguishable from a pass
that did clean work, and a reimplementer that conflates the two will
silently turn a soft miss into a compile abort.

## Cross-references

[Diagnostic ABI and Helpers](../mlir-infra/diagnostic-abi-and-helpers.md) is
the canonical reference for the 208-byte body layout, the 24-byte
`DiagnosticArg` 3-tuple, the severity-word bit encoding, and the
constructor / streamer / destructor triad. [Pass-Failure
Handshake](../mlir-infra/pass-failure-handshake.md) covers the soft-failure
convention used across the TileAS pass family and the reasoning behind
choosing it over `signalPassFailure()`. [Pipeline Invariants and
Verifiers](../pipeline/invariants-and-verifiers.md) documents the
three-layer verifier ladder and the explicit verifier passes that occupy
layer 3. [Driver Program Handle](../driver/program-handle.md) catalogs the
five driver-level exit codes and the verbatim strings each one carries.
[NVVM IR Verifier](../nvptx-passes/nvvm-ir-verifier.md) is the worked-example
target: it is where the parameter-space overflow diagnostic comes from, and
its per-SM ceiling table is the canonical reference for the limit values.
[Pass Manager Internals](../pipeline/pass-manager-internals.md) covers the
pass-manager dispatch model that ties the diagnostic engine, the handshake
bit, and the driver exit codes together.
[Troubleshooting and Known Issues](troubleshooting-and-known-issues.md)
turns this page's architecture inside-out for the user: it indexes by the
verbatim diagnostic the user sees and points back at the layer that
emitted it, the exit code it carries, and the change that resolves it.
