# Debugging and Introspection

## Abstract

Tileiras exposes five debugging surfaces. PTX line info ties emitted instructions back to the source `.cu` lines. Full device debug widens that link into stepping, breakpoints, and local-variable inspection at the cost of forcing `-O0`. The MLIR IR-snapshot surface dumps the pipeline's intermediate state between any pair of passes. Diagnostic stack traces attach a backtrace to each emitted diagnostic so the source of an error can be pinpointed when no pass name appears in the message. Finally, the scheduler decision trace records every candidate placement the modulo scheduler considered, with the cost vector and rejection reason for each one.

Each surface answers a different question. PTX line info answers "what source line is this PTX instruction?". Device debug answers "let me step through". The IR-snapshot surface answers "what's the IR after pass *N*, and how does it differ from after pass *N-1*?". Stack-traced diagnostics answer "which pass emitted this warning?". The scheduler trace answers "why did the scheduler pick this layout?".

Each surface has its own cost. None should be on by default. This page describes the surfaces, their costs, and how to combine them on a real debugging session.

## Surface 1: PTX line info

The driver flag `--lineinfo` and the pipeline option `emit-line-info` are the two halves of the same mechanism. The driver flag toggles the option to the `FromInput` snapshot stage; the option can be set independently by integrators to `None`, `Frontend`, or `TileasBoundary`. The selected snapshot becomes the source IR whose locations populate the `.loc` directives in the emitted PTX.

The PTX-side cost is small. Every emitted instruction grows by one `.loc file line col` directive. SASS size is unchanged because the assembler attaches the line table out-of-band. The compile-time cost is one extra IR walk per emission point. Use this surface when the question is post-mortem: nvprof, ncu, cuobjdump, or any tool that needs to map back from SASS to source.

The choice of snapshot stage matters. `FromInput` uses the locations attached to the bytecode the driver received and answers the question most users care about — "which input-program line is this?". `Frontend` and `TileasBoundary` use the locations live in the IR after the named stage. The latter two are useful when the question is about an internal pass — for example "which TileAS-generated tile is this?" — but they will reference IR locations that are not in the original `.cu` source.

The `--lineinfo` to `emit-line-info` mapping is described in [Driver CLI Options — Pipeline Options](../driver/cli-options.md#pipeline-options). The IR-to-DWARF lowering is described in [Lowering: Target and Debug Info — Lineinfo vs Device-Debug](../lowering/target-and-debuginfo.md#lineinfo-vs-device-debug).

## Surface 2: Full device debug

Full device debug is enabled by `--device-debug` or its alias `-g`. The driver validator rejects the combination of `--device-debug` and any non-zero `--opt-level` with the verbatim diagnostic:

```text
optimized debugging is not supported, change optimization level to 0 or disable full debug info
```

The validator's source is [Driver CLI Options — Validation Algorithm](../driver/cli-options.md#validation-algorithm). The rule is not cosmetic. Full device debug injects libNVVM options that disable several code-motion, value-fold, and block-merge transforms. The driver refuses to silently downgrade an optimised build rather than emit code whose optimisation level is unclear.

The PTX-side cost is substantial. `--lineinfo` adds `.loc` directives. `--device-debug` adds DWARF sections, full name preservation, `dbg.value` intrinsics, and the `llvm.nvvm.move` value pins that keep debugged values visible across passes. Expect PTX size on the order of ten times larger, compile time several times slower, and SASS that mirrors the unoptimised IR closely enough that cuda-gdb can step through it.

Use this surface when the question is interactive: setting breakpoints in cuda-gdb, watching local variables, stepping through control flow. For post-mortem mapping back to source, `--lineinfo` is enough and an order of magnitude cheaper.

## Surface 3: MLIR IR snapshots

MLIR's standard print-IR flags expose the pipeline's intermediate state. The flags reach Tileiras through the MLIR pass-manager surface; they apply to any MLIR-based compiler, but the scopes named in the output are the Tileiras-specific scopes enumerated in [Instrumentation and Action Handler — Scope tree the binary emits](../pipeline/instrumentation-and-action-handler.md#scope-tree-the-binary-emits).

| Flag | What it prints |
|---|---|
| `--mlir-print-ir-after-all` | IR after every pass that mutates it |
| `--mlir-print-ir-before-all` | IR before every pass |
| `--mlir-print-ir-after-failure` | IR only when the pipeline fails |
| `--mlir-print-ir-module-scope` | Print the whole module, not just the changed function |
| `--mlir-print-ir-after-change` | Print only when the IR actually changed |

The combination most useful during bring-up is `--mlir-print-ir-after-all --mlir-print-ir-module-scope`. The default per-pass scope prints only the immediate operation that changed, which truncates context when the pass operates on a single `gpu.func` inside a multi-function module.

The compile-time cost is dominated by AsmPrinter throughput. The cost scales with module size and pass count; on a multi-kernel module with `O3` it is normal for IR printing to dominate compile time. The cost is purely diagnostic — IR printing does not affect emitted PTX.

`--mlir-print-ir-after-failure` is the cheapest of the three. It prints only when the pipeline reports failure, which makes it the right default for batch runs where the question is "what did the pipeline look like when it broke?".

## Surface 4: Diagnostic stack traces

MLIR diagnostics carry an MLIR `Location` but no compiler-side call stack by default. The flag `--mlir-print-stacktrace-on-diagnostic` toggles the engine into attaching a child diagnostic with the literal text `"diagnostic emitted with trace:\n"` followed by a backtrace of the C++ frame that emitted the diagnostic. The mechanism is documented in [Diagnostic ABI and Helpers](../mlir-infra/diagnostic-abi-and-helpers.md).

The cost is per-diagnostic: each emitted diagnostic walks the C++ stack and resolves symbols. For a clean compile the cost is zero. For a compile that emits many warnings, every warning pays the trace cost.

Use this surface when the question is "which pass emitted this diagnostic". The MLIR `Location` answers "where in the IR" but not "where in the compiler". A scheduler diagnostic that reports a resource violation might come from any of the four placement arms (see [Modulo Scheduler and Rau — Placement Arms](../scheduler/modulo-scheduler-and-rau.md#placement-arms)); the stack trace pins the source frame.

## Surface 5: Scheduler decision trace

The pipeline option `schedule-trace-file=PATH` writes a Chrome-timeline-style JSON file recording every decision the cost-based scheduler made. The writer is the `DumpTraceImpl` instrumentation enumerated in [Instrumentation and Action Handler — Scope tree the binary emits](../pipeline/instrumentation-and-action-handler.md#scope-tree-the-binary-emits). The option is read once when the pass manager installs instrumentation; setting it after the pipeline starts has no effect.

The trace records the four placement arms — permute, fuse, retry, cost-based — and the per-candidate decisions inside each one: which `(op, cycle)` pair was tried, which cost vector it produced, which gate rejected it (G1, G2, G3, or G4), and which seat finally committed. The arms are described in [Serial vs Cost-Based Generators](../scheduler/serial-vs-cost-based-generators.md); the gate ladder is described in the same page's "Pre-commit Gates" section.

The cost is one per-decision JSON record plus a tail write at trace close. On a heavily pipelined kernel the trace is in the tens of megabytes. The format is loadable in Chrome's `chrome://tracing` UI, but a `jq`-style filter pass is usually faster than scrolling the timeline view.

Use this surface when the question is "why did the scheduler pick this `II`?", "why did this op end up at that stage?", or "which gate rejected this candidate seat?".

## MlirAction-based instrumentation

The MLIR pass manager exposes two more flags for users who already understand the pass timing model:

| Flag | Effect |
|---|---|
| `--mlir-pass-timing` | Emits a per-pass wall-clock and CPU breakdown at compile end |
| `--mlir-pass-statistics` | Emits the pass-internal statistics counters |

Pass timing exposes the pass-instrumentation scope tree directly. Each scope name in the output is one of the scopes enumerated in [Instrumentation and Action Handler](../pipeline/instrumentation-and-action-handler.md). The same scope tree is exposed through the C++ instrumentation API for integrators who want callbacks rather than printed reports.

The action surface — the `MlirAction` mechanism described in [Instrumentation and Action Handler — MLIR Actions](../pipeline/instrumentation-and-action-handler.md#mlir-actions) — is the lower-level handle. Each rewrite, pattern application, and greedy-driver iteration emits an action. A context-level action handler can observe every one of them; without a handler the action surface is a no-op. The mechanism is the right one for tools that need to instrument pattern application without modifying the pass list.

## Worked debugging session: wrong WGMMA shape

A user reports that their kernel emits a `wgmma.mma_async.sync.aligned.m64n128k16` where they expected `m64n256k16`. The mismatch shows up in the emitted PTX. The question is which pass is responsible.

**Step 1 — bisect the snapshot range.** Run with `--mlir-print-ir-after-all --mlir-print-ir-module-scope`. Search the output for the first `wgmma` operation. Note which pass produced it. If the `wgmma` shape is wrong at first appearance, the responsible pass is upstream of the emission point — typically the WGMMA atom-selection logic. If the shape was correct at emission and is later rewritten, the responsible pass is downstream — typically a canonicalisation or layout-refinement pass.

**Step 2 — pin the placement.** If the wrong shape appears at WGMMA emission, the source is the atom registry in [cute_nvgpu — MMA Atoms SM70-120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md). Re-run with `--schedule-trace-file=/tmp/trace.json` to see the scheduler's decision. If the scheduler picked a candidate that the atom registry should have rejected, the issue is registry-side. If the scheduler never saw the candidate the user expected, the issue is upstream of the atom registry.

**Step 3 — attribute a stray diagnostic.** If the IR snapshot output contains an unexpected warning that does not name the emitting pass, re-run with `--mlir-print-stacktrace-on-diagnostic`. The attached backtrace pins the C++ frame that emitted it.

**Step 4 — bisect by opt-level.** Re-run with `--opt-level=0`, `--opt-level=1`, `--opt-level=2`, `--opt-level=3` in turn. The first level at which the wrong shape appears identifies the pass band — the segments added at that opt-level boundary are listed in [Driver and Opt Levels](../pipeline/driver-and-opt-levels.md) and [Pipeline Options Mapping](../pipeline/options-mapping.md). Combining the bisect result with the snapshot output of Step 1 typically isolates the responsible pass within one or two candidates.

## Tunables decision matrix

| Symptom | Surface to enable | Cost |
|---|---|---|
| Wrong PTX line in profiler output | `--lineinfo` | Small (`.loc` directives) |
| Need to step through with cuda-gdb | `--device-debug` (forces `-O0`) | Large (~10x PTX, ~Nx compile) |
| IR is wrong mid-pipeline | `--mlir-print-ir-after-all --mlir-print-ir-module-scope` | Compile time dominated by AsmPrinter |
| IR may be wrong only on failure | `--mlir-print-ir-after-failure` | Zero on success |
| Diagnostic source unclear | `--mlir-print-stacktrace-on-diagnostic` | Per-diagnostic backtrace resolution |
| Schedule looks wrong | `--schedule-trace-file=PATH` | Tens of MB JSON per kernel |
| Compile is slow | `--mlir-pass-timing` | Negligible |
| Want only kernel-side output | `--dump-host=PATH` | Negligible; writes host code separately |
| Want pipeline statistics | `--mlir-pass-statistics` | Negligible |

The matrix's first principle is to pick the cheapest surface that answers the question. `--lineinfo` beats `--device-debug` for post-mortem mapping. `--mlir-print-ir-after-failure` beats `--mlir-print-ir-after-all` for batch runs that succeed most of the time. The scheduler trace beats general IR printing when the question is specifically about placement.

## Caveats

`--device-debug` with any `--opt-level` other than `0` is a hard error. The driver validator emits the verbatim diagnostic shown above and refuses to start the compile. There is no override; an integrator who wants optimised builds with debug-style symbols must use `--lineinfo` instead, which preserves source locations without disabling optimisation.

`--lineinfo` and `--device-debug` set different libNVVM flag dictionaries. The `-g` flag is added to the libNVVM option channel only under `--device-debug`; `--lineinfo` alone does not set it. The flag dictionary is documented in [Lowering: Target and Debug Info — Generated Target Fields](../lowering/target-and-debuginfo.md#generated-target-fields).

`schedule-trace-file` is read once at instrumentation install time. Setting it via `--pass-pipeline="tileir{schedule-trace-file=...}"` after the pipeline has been constructed has no effect. The option must be passed at the same layer as `--opt-level`.

`emit-line-info` and `--lineinfo` are not aliases. The driver flag toggles the pipeline option to one specific enum value (`FromInput`); the pipeline option has three enum values. An integrator who wants snapshot-tagged line info from a different IR stage must set `emit-line-info` directly.

`--mlir-print-ir-after-all` without `--mlir-print-ir-module-scope` prints only the operation that changed. On a multi-kernel module this can suppress the surrounding context the user actually wants to see. Module-scope is almost always the better default for debugging.

`--mlir-pass-timing` reports walltime that includes the time spent printing IR if any of the print-IR flags are also enabled. To get a clean pass-timing report, disable the print flags.

## Cross-references

[Driver CLI Options](../driver/cli-options.md#diagnostics-surface) enumerates the driver-level debugging flags and their pipeline-option counterparts. [Instrumentation and Action Handler](../pipeline/instrumentation-and-action-handler.md) documents the scope tree the `--mlir-pass-timing` flag exposes and the action surface that `MlirAction`-aware tooling consumes. [Lowering: Target and Debug Info](../lowering/target-and-debuginfo.md) documents how `--lineinfo` and `--device-debug` become LLVM debug-info constructs and which libNVVM options each sets. [Diagnostic ABI and Helpers](../mlir-infra/diagnostic-abi-and-helpers.md) documents the `--mlir-print-stacktrace-on-diagnostic` engine path. [Serial vs Cost-Based Generators](../scheduler/serial-vs-cost-based-generators.md) and [Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md) describe the scheduler whose decisions `--schedule-trace-file` records. [Testing and Observability](testing-and-observability.md) is the companion page that takes the surfaces enumerated here and applies them to differential, regression, and golden-output test patterns an integrator can build outside the compiler.
