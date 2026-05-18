# Instrumentation and Action Handling

## Abstract

Tileiras exposes four orthogonal tracing surfaces wired into one shared `PassManager`. **Pass instrumentation** records named scopes around pipeline stages and forms the spine of pass-timing and IR-printing. **The IRPrinter instrumentation** layered on top emits `*** IR Dump Before/After ***` banners for `--mlir-print-ir-before-all` / `--print-after-all`. **PassTiming** consumes the same scope tree and renders either a flat list or a nested tree, in text or JSON, under `--mlir-timing`. **MLIR actions** are a lower-level mechanism for tracing individual rewrites and pattern applications; they are visible through `mlir::ApplyPatternAction` / `GreedyPatternRewriteIteration` records and through the context's action-handler callback. All four surfaces interact with a single `PassInstrumentation` chain that the pipeline builder owns.

This page covers the chain itself: the algorithm an instrumentation hook follows, the canonical scope tree the binary actually emits, the MLIR action surface and its printable records, the pass-timing report grammar, the IR-printing banner protocol, the crash reproducer, opt-bisect, and debug-counter — plus the four NVIDIA-private hook points where the chain ties into the scheduler.

Confidence on the existence and ordering of the inner scopes is HIGH (every scope name is a verbatim `.rodata` string referenced from the scheduler band of `.text`). Confidence on the exact algorithmic decomposition of `runOnOperation` between the instrumentation chain and the action handler is MED — the upstream MLIR shape is preserved unchanged in `tileiras`, and the reverse-engineered call graph matches it bucket-for-bucket, but specific function offsets are not the public contract.

## The PassInstrumentation chain

`PassManager` owns a `PassInstrumentor` whose only payload is an ordered list of `PassInstrumentation*` hooks. Each hook is a polymorphic object with eight virtual entry points the pass manager calls at well-defined moments around every pass invocation, every pipeline run, and every analysis computation:

```c
struct PassInstrumentation {
    // Pass-execution boundary callbacks.
    virtual void runBeforePass(Pass *, Operation *) = 0;
    virtual void runAfterPass(Pass *, Operation *) = 0;
    virtual void runAfterPassFailed(Pass *, Operation *) = 0;

    // Pipeline-nesting callbacks (one per nested OpPassManager).
    virtual void runBeforePipeline(OperationName, const PipelineParentInfo &) = 0;
    virtual void runAfterPipeline(OperationName, const PipelineParentInfo &) = 0;

    // Analysis-cache callbacks.
    virtual void runBeforeAnalysis(StringRef, TypeID, Operation *) = 0;
    virtual void runAfterAnalysis (StringRef, TypeID, Operation *) = 0;
};
```

The pass-execution callbacks bracket every `Pass::runOnOperation` call. The pipeline callbacks fire once at the entry and exit of each nested `OpPassManager` invocation, regardless of how many passes run inside it. The analysis callbacks fire around any `AnalysisManager::getAnalysis<T>` cache miss; cache hits fire neither.

The instrumentor walks its hook list in registration order for the `Before*` callbacks and in reverse order for the `After*` callbacks. The reverse traversal is what gives nested timing the right semantics — an outer timer's `runAfterPass` fires after every inner timer has already closed.

```c
LogicalResult run_pass_with_instrumentation(
        PassManager &pm, Pass &pass, Operation *anchor) {

    PassInstrumentor *instr = pm.getInstrumentor();
    for (auto &h : instr->hooks_forward()) h->runBeforePass(&pass, anchor);

    LogicalResult result = pass.runOnOperation(anchor);

    if (failed(result)) {
        for (auto &h : instr->hooks_reverse()) h->runAfterPassFailed(&pass, anchor);
    } else {
        for (auto &h : instr->hooks_reverse()) h->runAfterPass(&pass, anchor);
    }
    return result;
}
```

When no instrumentation is installed the instrumentor's hook list is empty and entering and exiting the bracketed region is a single null check — the cost of a no-op `PassInstrumentation` chain is two function-pointer-array length tests per pass invocation, which is far below the cost of any non-trivial pass.

## Scope tree the binary emits

Every named scope the binary actually emits as a `.rodata` string is enumerated below. The scopes form a strict tree: outer scopes cover whole compilation phases; inner scopes cover scheduling and TileAS preparation substages. The order in which they fire on a typical compile is top-to-bottom in the table — `CompileNVVM` opens first, `SerializeGPUModule` closes last, and every scheduler scope nests inside `TileASGenerateSchedule` or `TileASPrepareForScheduling`.

| Scope | Layer | Purpose |
|---|---|---|
| `CompileNVVM` | Outer | Entire MLIR-to-NVVM/NVPTX compile run. |
| `SerializeGPUModule` | Outer | GPU module serialization and downstream assembler handoff. |
| `IRWalk::findTargetForLoops` | Schedule prep | Search the IR for loops eligible for schedule materialization. |
| `Schedule::unrollStaticForLoop` | Schedule prep | Emit static loop unrolling during schedule materialization. |
| `TileASGenerateSchedule` | Schedule | Schedule constraint generation. |
| `TileASPrepareForScheduling` | Schedule | TileAS preparation before schedule solving. |
| `legalizeLoopScheduleForMaterialization` | Schedule | Loop-shape cleanup before materializing a schedule. |
| `DumpTraceImpl::run` | Schedule | Write a scheduler trace when `schedule-trace-file` is set. |
| `unrollSmallLoopsForScheduling` | Schedule prep | Unroll small loops before schedule construction. |
| `decomposeSingleOp` | Schedule prep | Decompose a single complex op for schedule-friendly IR. |
| `loopUnrollByFactor` | Schedule prep | Apply an explicit unroll factor. |
| `loopUnrollByHeuristic` | Schedule prep | Apply heuristic loop unrolling. |
| `decomposeTiledLoadStoreView` | Schedule prep | Split tiled view loads/stores into scheduler-friendly forms. |
| `refineVecSizeOfAtoms` | Schedule prep | Refine vector sizes for atom operations. |
| `sliceAndFuse` | Schedule prep | Slice and fuse loops or regions for scheduling. |
| `TileASSliceAndFuse` | Schedule prep | Wrapper scope around the TileAS-specific slice-and-fuse stage. |
| `runCanonicalizer` | Schedule prep | Run canonicalization inside a scheduler preparation stage. |
| `compactMemLayout` | Schedule prep | Compact memory layout metadata. |
| `refreshBoxDim` | Schedule prep | Refresh box dimensions after layout changes. |
| `ResourceConstraintBuilder::tryAddConstraintToAvoidRegSpilling` | Schedule constraint | Add scheduling constraints to avoid spills (see [Resource Constraint Builder and RRT](../scheduler/resource-constraint-builder-and-rrt.md)). |

These names are part of the stable surface — external timing reports and callback integrations match on them verbatim. `TileASSliceAndFuse` and `TileASGenerateSchedule` are the two most expensive scopes on a typical O2 compile; together they dominate the wall-clock budget for any schedule-intensive kernel. The `DumpTraceImpl::run` scope only opens when [`schedule-trace-file`](options-mapping.md#scheduler-options) is set in [`TileIRPipelineOptions`](options-mapping.md).

> ⚡ **QUIRK — typo-stable scope names**
> Several scopes carry their upstream typos verbatim. `tryAddConstraintToAvoidRegSpilling` is the canonical example: the spelling is the cross-build contract, not a guideline. Downstream timing-report ingestion matches on this exact string. A reimplementation that silently corrects the spelling breaks every log-scraping pipeline that consumes the timing tree.

## Scope Algorithm

Instrumentation scopes are exception-safe and nest correctly through an RAII helper. The exit always fires, even when the body throws — the chain's `runAfterPassFailed` hooks are the failure path.

```c
class ScopeGuard {
public:
    ScopeGuard(PassInstrumentor *p, StringRef name)
        : p_(p), token_(p ? p->enter(name) : INVALID_TOKEN) {}

    ~ScopeGuard() {
        if (p_) p_->exit(token_, std::uncaught_exceptions() == 0);
    }
private:
    PassInstrumentor *p_;
    ScopeToken        token_;
};

// Caller side:
{
    ScopeGuard g(instr, "TileASGenerateSchedule");
    if (failed(generate_schedule_constraints(...))) return failure();
    // ScopeGuard destructor fires runAfterPass/runAfterPassFailed as appropriate.
}
```

When no instrumentation handler is installed `enter` returns `INVALID_TOKEN` and `exit` is a single integer compare against that sentinel — entering and exiting a scope costs roughly the same as a function call's prologue and epilogue.

> ⚡ **QUIRK — scope tokens are not pointer-comparable**
> Scope tokens are opaque integers, not pointers into the hook chain. Two independent scopes can return the same token after one closes — the chain reuses retired slots. Code that tries to memoize the token across multiple scope-aware components must use the scope **name**, not the token, as the key.

## Pass Timing Report Format

`--mlir-timing` enables a `TimingInstrumentation` hook that accumulates per-scope wall time and user+system time. The hook prints its report at process exit through an `llvm::Timer`-shaped grammar. Two display modes and two output formats are available:

| `mlir-timing-display` | `mlir-output-format` | Report grammar |
|---|---|---|
| `list` (default) | `text` (default) | Flat list of `Wall time | User+System | Pass-or-scope name`, sorted by wall time descending. |
| `tree` | `text` | Nested tree, indentation per scope depth; preserves pass-manager nesting. |
| `list` | `json` | JSON array of `{wall, user_system, name}` triples. |
| `tree` | `json` | JSON tree of `{wall, user_system, name, children}` nodes. |

The header strings are stable across builds and are matched by downstream log-scrapers: `   ---User Time---`, `  ----User Time----`, `   ---Wall Time---`, `  ----Wall Time----  ----Name----`, and `  ---User+System--`. The leading-whitespace count is part of the contract.

```text
===-------------------------------------------------------------------------===
                              Pass execution timing report
===-------------------------------------------------------------------------===
  Total Execution Time: 4.812 seconds

   ---Wall Time---   ---User+System--    Name
   2.013 (41.8%)     2.009 (41.7%)       TileASGenerateSchedule
   1.144 (23.8%)     1.143 (23.8%)       TileASSliceAndFuse
   0.401 ( 8.3%)     0.401 ( 8.3%)       Canonicalizer
   ...
   4.812 (100.0%)    4.811 (100.0%)      Total
```

The list-mode report's percentage column is computed against the **total**, which is the root scope's wall time, not the sum of children — `Canonicalizer` invocations nested under `TileASGenerateSchedule` contribute to both rows. The tree mode disambiguates by showing the percentages relative to each parent.

> ⚡ **QUIRK — typo-preserved enum value**
> The `--mlir-timing-display=tree` description string in the binary reads `display the results ina with a nested tree view` — the dropped 'i' after `in` is preserved verbatim from upstream MLIR. The cl::opt help text matches the upstream `Pass.cpp` byte-for-byte. Do not "fix" this when reimplementing; downstream regression suites match the typo.

`--enable-statistics` and `--stats-json` enable a parallel statistics pass that walks every registered `llvm::Statistic` after compilation and prints counters and frequencies. This surface is independent of the timing instrumentation — a build can have timing on while statistics are off, or vice versa.

## IR-Printing Instrumentation

The IR-printer hook intercepts the same pass-execution callbacks the timing hook uses. Where the timing hook accumulates a duration, the IR-printer emits a textual MLIR snapshot of the anchor operation either before or after the pass body runs. The hook gates its emission on a per-pass filter, a per-anchor scope, and a change detector. The full surface partitions into eight knobs the user enables through `MLIRContext::setPrintIR…` or through the corresponding driver flags.

| Knob | Default | Effect |
|---|---|---|
| `--mlir-print-ir-before-all` | off | Snapshot before every pass that runs. |
| `--mlir-print-ir-after-all` | off | Snapshot after every pass that runs. |
| `--mlir-print-ir-after-change` | off | Suppress the after-snapshot unless the pass actually mutated the anchor. |
| `--mlir-print-ir-after-failure` | off | Snapshot only when a pass returns failure. |
| `--mlir-print-ir-module-scope` | off | Use the enclosing `builtin.module` as the snapshot root rather than the anchor. |
| `filter-print-funcs=<name>` | empty | Limit the per-function snapshot to named functions. |
| `--mlir-elide-elementsattrs-if-larger=<n>` | per-MLIR | Truncate large `DenseElementsAttr` payloads. |
| `--mlir-print-skip-regions` | off | Skip every region body — print operations alone. |

Each snapshot is bracketed by a verbatim banner: `*** IR Dump Before <pass-name> ***` or `*** IR Dump After <pass-name> ***`. The pass name is the registered argument name (e.g., `cse`, `tileas-generate-schedule-constraints`). When `--mlir-print-ir-module-scope` is enabled, the banner is followed by the full module text; without module-scope, only the anchor operation is printed, which on a multi-kernel module can suppress context that the user actually needs to see (see [Debugging and Introspection — IR Printing](../topics/debugging-and-introspection.md) for the user-facing recipe).

```c
void IRPrinterInstrumentation::runAfterPass(Pass *pass, Operation *anchor) {
    if (suppressed_by_filter(pass)) return;
    if (require_change_ && !pass_changed_anchor(anchor)) return;

    Operation *root = use_module_scope_ ? containing_module(anchor) : anchor;
    raw_ostream &os = printer_stream();

    os << "*** IR Dump After " << pass->getArgument() << " ***\n";
    OpPrintingFlags flags;
    if (print_debug_info_)     flags.enableDebugInfo();
    if (print_op_generic_)     flags.printGenericOpForm();
    if (skip_regions_)         flags.skipRegions();
    if (elide_threshold_ >= 0) flags.elideLargeElementsAttrs(elide_threshold_);

    root->print(os, flags);
    os << "\n";
}
```

The `pass_changed_anchor` predicate compares a hash captured at `runBeforePass` against a hash captured at `runAfterPass`. Hash collisions are not handled — an IR change that produces an identical hash will appear as "no change" to the change detector, but the failure mode is conservative (snapshots are suppressed, never duplicated).

The AsmPrinter knobs that govern the textual form of the printed IR are documented under [Dialect Asm-Printer Status — Alias Resolution](../bytecode/asm-printer-status.md#alias-resolution); they include `mlir-print-debuginfo`, `mlir-print-local-scope`, `mlir-print-op-generic`, `mlir-print-skip-regions`, `mlir-print-unique-ssa-ids`, `mlir-print-value-users`, and `mlir-use-nameloc-as-prefix`. Each one is a process-wide cl::opt that the IRPrinter hook reads to construct its `OpPrintingFlags` per snapshot.

> ⚡ **QUIRK — `print-changed` requires a baseline**
> The IRPrinter only emits an "after" snapshot under `--mlir-print-ir-after-change` if `runBeforePass` ran first and stashed a baseline. If both `--mlir-print-ir-after-change` and `--mlir-print-ir-after-failure` are passed without `--mlir-print-ir-before-all`, the change detector has no baseline to compare against and falls back to "always emit after a failure." This is invisible in the help text but the actual behavior any reproducer must match.

## MLIR Actions

Actions are a finer-grained surface than pass instrumentation. Where instrumentation brackets pass-level work, an action brackets a single transformation event — typically one greedy-rewrite iteration or one pattern application — and gives a registered handler the option to inspect, skip, log, or replace the work. Three action types ship in the binary:

| Action type | Class | Fires per |
|---|---|---|
| `mlir::ApplyPatternAction` | RTTI string in `.rodata` | Single rewrite-pattern invocation inside the greedy or dialect-conversion driver. |
| `mlir::GreedyPatternRewriteIteration` | RTTI string in `.rodata` | One sweep of the greedy worklist. |
| `mlir::SetMaxRegisterActionAttr` | RTTI string in `.rodata` | NVVM `setmaxnreg` rewrite at NVVM lowering time. |

An action has an identity, a tag, and an optional payload pointer that the handler interprets. A context-level handler installed through `MLIRContext::registerActionHandler` intercepts every action that runs inside that context:

```c
LogicalResult MLIRContext::executeAction(
        const Action &action, function_ref<void()> work) {

    if (!action_handler_) {
        work();
        return success();
    }

    return action_handler_(action, [&] { work(); return success(); });
}
```

The handler receives the action's IRPrinting state and may print, defer, drop, or stack-trace the work before calling the supplied `work` closure. Three call sites in the binary thread actions through this dispatch: the greedy-pattern-rewrite driver (one action per iteration plus one per pattern attempt), the dialect-conversion driver (one action per attempted conversion), and the NVVM lowering pass (one action per `setmaxnreg` rewrite).

The action-record dump format begins with the verbatim banner `>> Action Record `. The handler then prints `  Action: <name>` followed by the action's payload formatted by the action class itself. Greedy-rewrite actions print their root op and the matched pattern's RTTI name; dialect-conversion actions print the source-and-target op pair. The handler can also emit `  Action: cleanup` for the deferred-rewrites cleanup pass.

> ⚡ **QUIRK — actions are independent of pass instrumentation**
> A pass with no instrumentation hooks installed still emits actions when a handler is registered. Conversely, a pass with instrumentation but no action handler emits scopes but no action records. The two surfaces share neither a thread nor a buffer — they are wired in parallel through `MLIRContext` and `PassManager` respectively. This is what lets a build run pass timing without the per-pattern noise of action tracing, or trace patterns without timing overhead.

## Opt-Bisect

`opt-bisect-limit` is LLVM's bisect-by-pass-index harness. Set to a positive integer, it caps the number of passes that run; passes above the cap are skipped, and the IR captured at the cap is the bisect output. Tileiras inherits the LLVM-side instrumentation hook unchanged. Four cl::opts control its behavior:

| Knob | Type | Default | Effect |
|---|---|---|---|
| `opt-bisect-limit` | `int` | `INT_MAX` | Skip every pass whose index exceeds the limit. |
| `opt-bisect-funcs` | `string list` | empty | Restrict bisection to a comma-separated function list. |
| `opt-bisect-verbose` | `bool` | `false` | Print each pass index and decision to stderr. |
| `opt-bisect-print-ir-path` | `string` | empty | Write the IR to this path when the limit is reached. |

Opt-bisect runs in the same instrumentation chain as the IR printer and the timer. Its `runBeforePass` hook increments a counter, compares against the limit, and either lets the pass through or replaces it with a no-op. The reduced search space — pass-by-pass binary search instead of textual diff — is what makes opt-bisect viable on the 200+-pass tileiras pipeline.

A related but independent surface is `debug-counter`, which gates individual transformations within a pass by name (e.g., `--debug-counter=dce-counter=10` lets DCE run for ten elimination attempts before short-circuiting). Two knobs apply:

| Knob | Type | Default | Effect |
|---|---|---|---|
| `print-debug-counter` | `bool` | `false` | Print the post-run counter summary. |
| `debug-counter-break-on-last` | `bool` | `false` | Trap into the debugger on the final allowed attempt. |

The `Requested --debug-counter in LLVM build without assertions. This is a no-op.` warning is emitted at startup when the build does not carry the assertion-time counter table; tileiras's release build is one such build, so debug-counter is silently a no-op unless the build is rebuilt with assertions.

## Crash Reproducer

When a pass aborts or fails late in the pipeline, the pass manager can emit a self-contained reproducer — the IR captured immediately before the failing pass plus a `--pass-pipeline=` string that names every pass run up to the failure. The reproducer is written when the pipeline builder calls `enableCrashReproducerGeneration` with a path; the file path then surfaces as the verbatim diagnostic `reproducer generated at \`<path>\``. The reproducer is gated on a single guard at the pass-manager level — once enabled, it stays enabled for every subsequent pass invocation in that manager.

Crash-reproducer output has two flavors:

| Flavor | When | What it captures |
|---|---|---|
| Local | Default | The single pass that failed plus the IR seen by that pass; minimal context. |
| Pipeline | `genLocalReproducer=false` | The full pipeline from the failure backward to the last `cse` or `canonicalize` checkpoint; replays the path that produced the failing IR. |

Neither flavor captures host-side `cl::opt` state — a reproducer built against the binary needs to be re-run with the same CLI flags. The "Reproducer" banner in the captured file is emitted before the IR payload as a textual delimiter.

> ⚡ **QUIRK — reproducer paths are per-PassManager, not per-context**
> Each `PassManager` instance carries its own reproducer-path setting. Two pass managers running in the same MLIRContext can have different reproducer paths, or one can have it enabled and the other not. This is intentional — it lets the outer driver capture a reproducer for the device-module pipeline without bloating the host-wrapper pipeline's reproducer with kilobytes of unrelated MLIR.

## NVIDIA-Private Hook Points

Four hook points on the instrumentation chain are NVIDIA-private:

1. **`DumpTraceImpl::run`** — Activated by [`schedule-trace-file`](options-mapping.md#scheduler-options). The hook installs itself on the inner `gpu.func` (TileAS-stage) adaptor and writes a per-pass scheduler trace in the Chrome `chrome://tracing` format. Each pass invocation produces one event with name, timestamp, and duration; each scheduler decision produces a child event under its enclosing pass. The trace is closed at process exit through `__cxa_atexit`.

2. **`schedule-trace-file` consumer** — Reads the option at pipeline-construction time and refuses to install the hook if the file cannot be opened. The diagnostic `failed to legalizeLoopScheduleForMaterialization` fires when the legalisation phase rejects a loop the scheduler expected to consume; it is the most common visible scheduler-side failure and rides in the same instrumentation tree.

3. **TileIR Callbacks** — Five `__CUDA_TILEIR_*` env vars (see [Env Vars and Runtime Gates](../driver/env-vars-and-runtime-gates.md) and the [TILEIR_CALLBACKS ABI](../driver/tileir-callbacks-abi.md)) modify the instrumentation surface at module load time. `__CUDA_TILEIR_CALLBACKS_ON_PRE_LOAD` registers a host-side pre-load hook that the instrumentation chain notifies before any compilation begins; `__CUDA_TILEIR_FUNC_CALLBACKS` and `__CUDA_TILEIR_FUNC_ON_ARGUMENTS_CHANGE` register per-function callbacks that fire on argument-buffer mutation.

4. **Action attributes** — `mlir::NVVM::SetMaxRegisterActionAttr` is the only NVIDIA-private `Action` class. It fires when the NVVM lowering rewrites a `gpu.func` to add the `setmaxnreg` PTX-level directive; the action wraps the rewrite so an outside observer can log the per-function maxnreg decision without modifying the pass itself.

## Verify-Each and Action Composition

The `verify-each` knob (off by default for release builds, on by default for assert builds) runs the full `verify()` on the anchor operation between every pair of passes. Verify-each is implemented as a `PassInstrumentation` hook that fires from `runAfterPass`; it is in the same chain as the IR printer and the timer, so its cost is roughly proportional to the size of the anchor operation. See [Pipeline Invariants and Verifiers — Verifier Layers](invariants-and-verifiers.md#verifier-layers) for the layered verifier model and why between-pass verification catches a class of bugs the explicit verifier passes cannot.

Mixing all five hooks — timer, IR printer, verify-each, opt-bisect, and a custom action handler — is supported by the chain. The interleaving order matters for one specific case: if both `--mlir-print-ir-after-all` and `verify-each` are enabled, the IR printer's snapshot is taken **before** verify-each runs, so a failed verification produces both the post-pass IR snapshot (from the printer hook) and the failure diagnostic (from verify-each). The snapshot reflects the IR that triggered the verification failure, which is exactly what the user wants when bisecting a between-pass invariant violation.

## Callback Integration

The same compile instrumentation surface feeds the TileIR callback emission path. Callback emission materialises well-known module symbols and launch-site hooks so a runtime can patch instrumentation at module load time. The host-side symbols `__CUDA_TILEIR_CALLBACKS`, `__CUDA_TILEIR_CALLBACKS_ON_PRE_LOAD`, `__CUDA_TILEIR_FUNC_CALLBACKS`, `__CUDA_TILEIR_FUNC_ON_ARGUMENTS_CHANGE`, and `__CUDA_TILEIR_ON_PRE_LOAD` are wired through the same pipeline-builder logic that installs `DumpTraceImpl`; the driver-level ABI is documented in [TILEIR_CALLBACKS ABI](../driver/tileir-callbacks-abi.md), and the format strings the callbacks emit (`[TileIR Callback] Argument %d: offset = %ld, size = %ld`, `[TileIR Callback] CUdeviceptr: %p`, `[TileIR Callback] DESC_TMA512: ...`) are part of the public log format.

## Reimplementation Notes

```text
build_instrumentation_chain(pm, opts):
    chain = PassInstrumentor()
    if opts.timing_enabled:
        chain.push(TimingInstrumentation(opts.timing_display, opts.timing_format))
    if opts.ir_printing_enabled:
        chain.push(IRPrinterInstrumentation(opts.print_filter, opts.print_flags))
    if pm.verify_each:
        chain.push(VerifyEachInstrumentation(pm))
    if opts.opt_bisect_limit < INT_MAX:
        chain.push(OptBisectInstrumentation(opts.opt_bisect_limit, opts.opt_bisect_funcs))
    if opts.schedule_trace_file:
        chain.push(DumpTraceImpl(opts.schedule_trace_file))
    pm.set_instrumentor(chain)

run_pass(pm, pass, anchor):
    for h in chain.forward(): h.run_before(pass, anchor)
    result = pass.run_on_operation(anchor)
    for h in chain.reverse():
        if failed(result): h.run_after_failed(pass, anchor)
        else:              h.run_after(pass, anchor)
    return result
```

The ordering rule: the timer goes first because it must envelop every other hook's cost; the IR printer goes second so its snapshot is taken before verify-each potentially rejects; verify-each goes third; opt-bisect goes fourth so it can short-circuit before the scheduler-trace hook runs; the trace hook goes last so it sees only the passes opt-bisect allows through. Reversing this order silently changes the meaning of every reported number.

## Cross-References

[Driver Entry and Optimization Levels — Serialization Scopes](driver-and-opt-levels.md#serialization-scopes) names the two outer scopes (`CompileNVVM`, `SerializeGPUModule`) that this page enumerates. [Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md) is the scheduler whose stages drive most of the inner scopes. [Resource Constraint Builder and RRT](../scheduler/resource-constraint-builder-and-rrt.md) is what `ResourceConstraintBuilder::tryAddConstraintToAvoidRegSpilling` is part of. [Debugging and Introspection](../topics/debugging-and-introspection.md) is the user-facing guide that turns the scope tree and action surface documented here into a workflow for diagnosing pipeline issues. [Testing and Observability](../topics/testing-and-observability.md) covers how external test suites consume the timing and snapshot streams. [Pass Manager Internals — Threading Model](pass-manager-internals.md#threading-model) covers the threading contract this chain runs inside. [cl::opt Full Catalog — Layer 6](../reference/cl-opt-full-catalog.md) is the canonical catalog for every `--mlir-*` knob mentioned here.
