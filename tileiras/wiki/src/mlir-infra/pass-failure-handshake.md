# TileAS Pass-Failure Handshake

## Abstract

TileAS passes communicate failure through a shared status byte at offset +40 in the per-pass PassObject. Setting bit 2 of that byte (`0x04`) signals a soft failure: the pass completes its walk, the driver inspects the bit once the walk terminates, and dependent downstream passes either short-circuit or skip work that requires output from a failed predecessor. Failure does not throw, does not unwind, and does not abandon the IR. This page documents the convention.

The handshake appears across the entire D08-D13 TileAS pass family — async materialization, convert-layout materialization, schedule materialization, the unspecialized pipeline pass, the pipeline-region optimizer, and the convert-tileas-to-LLVM rewriter all set or read the same bit. It is the single most pervasive piece of inter-pass plumbing in TileAS.

## Convention

Every TileAS pass instance carries a status word in its PassObject. The byte at offset +40 is the failure-handshake byte; bit 2 (`0x04`) is the failure signal. Other bits of the same word may carry pass-specific flags (the upper bits are not reserved), but bit 2 is the cross-pass contract.

```c
typedef struct PassObject {
    /* ... pass-specific fields at +0 .. +39 ... */
    /*+0x28*/ uint32_t status_word;          /* bit 2 (0x04) = soft failure */
    /* ... pass-specific options and state ... */
} PassObject;

static inline void pass_mark_soft_failure(PassObject *self) {
    self->status_word |= 4;
}

static inline bool pass_soft_failed(const PassObject *self) {
    return (self->status_word & 4) != 0;
}
```

The pass-side use is uniform: when a pass body decides that its work cannot complete, it emits a diagnostic and ORs `4` into `self+40`, then keeps walking or returns `success()`. The driver inspects the bit after the walk and lifts it to a top-level pass-manager failure if the pass result is required, or leaves it as a recoverable miss if downstream passes know how to handle it.

## Why Not `signalPassFailure()`

The upstream MLIR PassManager exposes `signalPassFailure()` for hard pass failures. TileAS deliberately avoids that path in most places, for two reasons.

First, granularity. `signalPassFailure()` is whole-function: once a pass calls it, the pass-manager treats the whole function as failed and may stop running subsequent passes on it. TileAS often wants to fail one op or one loop without poisoning the rest of the function — for example, "this one loop could not be software-pipelined, leave it synchronous and continue". The handshake bit lets a pass record the partial-failure outcome while still producing valid IR the next pass can consume.

Second, downstream readability. When a TileAS pass communicates failure through `signalPassFailure()`, the next pass has no way to discover the reason — the failure is opaque, and the next pass would have to re-do whatever analysis the failed pass performed to decide what to skip. With the handshake bit, the failed pass leaves a clear and inspectable signal, and the dependent pass simply reads the status word and acts accordingly.

The bit is not a replacement for `signalPassFailure()`. Fatal contract violations — malformed IR, missing analyses that should always exist, sentinel pointer dereferences — still trap or call `report_fatal_error`. The handshake is for recoverable cases where one pass produces IR the next pass can either use or sidestep.

## Propagation

Downstream passes that depend on the success of a predecessor read the predecessor's status word through the PassManager's pass-result lookup. The dependent pass either short-circuits (if it has nothing to do when the predecessor failed) or runs a fallback (if it can still produce useful output).

The canonical example is `TileASOptimizePipelineRegion` (D13), which shrinks `produce_one` and `consume_one` regions after `TileASUnspecializedPipeline` (D11) has expanded the schedule. When D11 leaves a loop synchronous (its `Failed to pipeline loop` remark), it sets bit 2 of its own status word; D13 reads that bit and skips the shrinker on functions whose loops D11 refused to pipeline. The shrinker has no work to do on a synchronous loop — its regions were never materialised — so skipping is the correct behaviour, and the contract is one-bit-wide.

```c
void run_optimize_pipeline_region(FuncOp func, PassObject *self, PassObject *d11) {
    if (pass_soft_failed(d11)) {
        /* D11 left this function synchronous; no pipeline regions to shrink. */
        return;
    }
    /* ... walk and shrink ... */
}
```

A pass that ignores a predecessor's soft failure is not buggy by itself — the IR is still valid — but it may waste cycles walking regions that have nothing useful to do. The convention is to read the bit whenever a pass has a cheap reason to skip work.

## The Diagnostic-Emit Pattern

A pass that sets the handshake bit always pairs it with a diagnostic. The two are written in a fixed order: emit the diagnostic, then set the bit.

```c
LogicalResult run_one_pass(PassObject *self, Operation *op) {
    if (failed(do_work(op))) {
        op->emitError() << "verbatim diagnostic explaining the structural reason";
        pass_mark_soft_failure(self);
        return failure();
    }
    return success();
}
```

The diagnostic gives the user the structural reason for the failure — what shape the pass expected, what it found, what the user could change to make the pass succeed. The bit gives the pass manager a machine-readable signal that downstream passes can read without parsing the diagnostic stream.

Diagnostics typically come through `sub_446CE00` (the standard Tileiras diagnostic emitter) at severity 259 (`0x103`, "Error"); a recoverable miss like `TileASUnspecializedPipeline`'s `Failed to pipeline loop` uses severity 3 (Remark) instead. Both severity levels set the same bit — the user-facing message is what changes, not the inter-pass signal.

## Where the Handshake Appears

The convention is used across the entire TileAS pipeline. The list below covers the principal callers:

| Pass | Trigger | Verbatim diagnostic |
|---|---|---|
| `TileASMaterializeAsync` (D08) | conflicting producer-like ops on one pipeline | `"there are two `produce-one-like` operations using different instructions to generate data into the same pipeline. It's a bug of MaterializeAsync Pass."` |
| `TileASMaterializeConvertLayout` (D09) | target-spec lookup failure | `"failed to query target spec for convert_layout"` |
| `TileASMaterializeSchedule` (D10) | missing ScheduleAnalysis or alias contract violation | `"Alias is not expected here."` |
| `TileASUnspecializedPipeline` (D11) | non-pipelinable loop shape | `"Failed to pipeline loop"` |
| `TileASOptimizePipelineRegion` (D13) | reads D11's bit; never sets its own | (skips work, no diagnostic) |
| `ConvertTileASToLLVM` | various lowering failures | varies by op family |

Most TileAS passes both read predecessors' bits and set their own. The convention is recursive: a pass's status word is part of its public contract with every subsequent pass.

## Implementation Constraints

A reimplementation must preserve three invariants.

First, the bit must be at the same offset and meaning across every pass. A pass whose PassObject lays out its status word at a different offset cannot participate in the handshake — the downstream-read pattern hard-codes `+40`.

Second, the diagnostic must precede the bit-set. If the bit is set before the diagnostic, a pass-manager that early-exits on bit-set may never publish the diagnostic to the user, and the failure becomes invisible.

Third, the bit is cumulative within one pass run. Multiple op-level failures inside one pass keep ORing `4` into the same word; the word never gets cleared mid-run. The driver clears the word before the pass starts and inspects it once the pass returns.

## Cross-References

[TileAS Async and Pipeline Family](../passes/tileas/async-pipeline-family.md) is the canonical example, with the handshake appearing in five of its passes.
[Pass Manager Internals](../pipeline/pass-manager-internals.md) covers the PassObject layout and the driver-side pass-result lookup the handshake rides on.
[Diagnostic Helpers](../infra/diagnostic-helpers.md) documents the diagnostic emitter that all these passes call before setting the bit.
[Invariants and Verifiers](../pipeline/invariants-and-verifiers.md) covers the cross-pass invariants the handshake protects.
