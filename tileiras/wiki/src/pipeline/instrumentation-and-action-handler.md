# Instrumentation and Action Handling

## Abstract

Tileiras exposes two tracing surfaces. Pass instrumentation records named scopes around pipeline
stages, scheduling stages, and serialization. MLIR actions are a lower-level mechanism for tracing
rewrites and pattern application. The pass instrumentation surface is the one users and embedders are
most likely to observe through timing, callbacks, or profiling.

## Pass Instrumentation Scopes

Pass scopes form a tree. Outer scopes cover whole compilation phases; inner scopes cover scheduling
and TileAS preparation substages.

| Scope | Purpose |
| --- | --- |
| `CompileNVVM` | Entire MLIR-to-NVVM/NVPTX compile run. |
| `SerializeGPUModule` | GPU module serialization and downstream assembler handoff. |
| `IRWalk::findTargetForLoops` | Search the IR for loops eligible for schedule materialization. |
| `Schedule::unrollStaticForLoop` | Emit static loop unrolling during schedule materialization. |
| `TileASGenerateSchedule` | Schedule constraint generation. |
| `TileASPrepareForScheduling` | TileAS preparation before schedule solving. |
| `legalizeLoopScheduleForMaterialization` | Loop-shape cleanup before materializing a schedule. |
| `DumpTraceImpl::run` | Write a scheduler trace when `schedule-trace-file` is set. |
| `unrollSmallLoopsForScheduling` | Unroll small loops before schedule construction. |
| `decomposeSingleOp` | Decompose a single complex op for schedule-friendly IR. |
| `loopUnrollByFactor` | Apply an explicit unroll factor. |
| `loopUnrollByHeuristic` | Apply heuristic loop unrolling. |
| `decomposeTiledLoadStoreView` | Split tiled view loads/stores into scheduler-friendly forms. |
| `refineVecSizeOfAtoms` | Refine vector sizes for atom operations. |
| `sliceAndFuse` | Slice and fuse loops or regions for scheduling. |
| `runCanonicalizer` | Run canonicalization inside a scheduler preparation stage. |
| `compactMemLayout` | Compact memory layout metadata. |
| `refreshBoxDim` | Refresh box dimensions after layout changes. |
| `ResourceConstraintBuilder::tryAddConstraintToAvoidRegSpilling` | Add scheduling constraints to avoid spills (see [Resource Constraint Builder and RRT](../scheduler/resource-constraint-builder-and-rrt.md)). |

These names should remain stable because external timing reports and callback integrations may depend
on them.

## Scope Algorithm

Instrumentation scopes should be exception-safe and nest correctly.

```c
void with_scope(Instrumentation *instr, StringRef name, void (*body)(void *), void *arg) {
    ScopeToken token = instrumentation_enter(instr, name);

    bool completed = false;
    try {
        body(arg);
        completed = true;
    } finally {
        instrumentation_exit(instr, token, completed);
    }
}
```

When no instrumentation handler is installed, entering and exiting a scope should be a cheap no-op.

## MLIR Actions

Actions describe fine-grained compiler events such as greedy rewrite iterations or pattern
applications. An action has an identity, a tag, and optional payload. A context-level handler can
intercept it.

```c
void execute_action(Context *ctx, Action action, void (*work)(void *), void *arg) {
    if (!ctx->action_handler) {
        work(arg);
        return;
    }

    ctx->action_handler(ctx, action, work, arg);
}
```

Actions are orthogonal to pass timing. A build can have pass instrumentation enabled while actions are
unhandled, or vice versa.

## Callback Integration

The same compile instrumentation surface feeds the TileIR callback emission path. Callback emission
materializes well-known module symbols and launch-site hooks so a runtime can patch instrumentation at
module load time. The driver-level ABI is documented in [TILEIR_CALLBACKS ABI](../driver/tileir-callbacks-abi.md).

## Cross-References

[Driver Entry and Optimization Levels — Serialization Scopes](driver-and-opt-levels.md#serialization-scopes) names the two outer scopes (`CompileNVVM`, `SerializeGPUModule`) that this page enumerates. [Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md) is the scheduler whose stages drive most of the inner scopes. [Resource Constraint Builder and RRT](../scheduler/resource-constraint-builder-and-rrt.md) is what `ResourceConstraintBuilder::tryAddConstraintToAvoidRegSpilling` is part of.

