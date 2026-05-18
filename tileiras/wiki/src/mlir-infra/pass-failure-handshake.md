# TileAS Pass-Failure Handshake

## Abstract

TileAS passes communicate failure through a shared status byte at offset +40 in the per-pass PassObject. Setting bit 2 of that byte (`0x04`) signals a soft failure: the pass completes its walk, the driver inspects the bit once the walk terminates, and dependent downstream passes either short-circuit or skip work that requires output from a failed predecessor. Failure does not throw, does not unwind, and does not abandon the IR.

The handshake appears across the entire D08-D13 TileAS pass family — async materialization, convert-layout materialization, schedule materialization, the unspecialized pipeline pass, the pipeline-region optimizer, and the convert-tileas-to-LLVM rewriter all set or read the same bit. It is the central piece of inter-pass plumbing in TileAS.

This page documents the handshake convention specifically. For the broader
three-layer error-handling architecture — MLIR diagnostic engine,
pass-failure handshake, and driver-level exit codes — see [Error Handling
and Diagnostics](../topics/error-handling-and-diagnostics.md).

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

## Soft handshake vs hard fatal error

The TileAS pipeline carries three failure paths at three different
severities. The soft handshake is the lightest; `signalPassFailure()` is
the middle weight; `report_fatal_error` is the heavy one. The three are
visually similar inside a pass body — each is a call paired with a
diagnostic — but their downstream consequences diverge sharply.

| Path                       | Mechanism                                | What stays running                                                                | User outcome                                                  |
|----------------------------|------------------------------------------|------------------------------------------------------------------------------------|---------------------------------------------------------------|
| Soft handshake             | `*(self+40) |= 4` after `op->emitRemark` or `op->emitError`; pass returns `success()` | The pass-manager keeps running; downstream passes peek at the bit and skip dependent work | A diagnostic appears on stderr; driver exit code typically remains 0 unless an Error was emitted by another pass |
| `signalPassFailure()`      | MLIR pass-manager-side failure flag after `op->emitError`               | The current pass completes its walk, then the pass-manager returns `failure()`     | An Error-class diagnostic appears; driver exit code 5         |
| `llvm::report_fatal_error` | LLVM-tier fatal-error handler                                            | Nothing — the process aborts through the fatal-error handler                       | A bare diagnostic on stderr; process abort, no clean exit code |

The handshake is the only path on which the user can still get a usable
artifact: a function whose pipelining failed under D11 still compiles
correctly, just synchronously, and the driver returns 0 if no other pass
escalated. `signalPassFailure()` always aborts the compile; the difference
between it and the handshake is whether the next pass gets a chance to run
at all. `report_fatal_error` skips even the pass-manager's failure
propagation — the LLVM-side handler runs immediately, the process exits
through `abort()`, and the driver cannot translate the result into an exit
code because `main` never returns.

The choice between the three is structural, not stylistic. A reimplementer
should pick the soft handshake when downstream passes can plausibly run on
the un-rewritten IR, `signalPassFailure()` when they cannot but the
pipeline state is still consistent, and `report_fatal_error` only when the
IR or an internal invariant has been corrupted beyond what subsequent
passes can describe. The TileAS family uses all three; the canonical
async-pipeline path uses the soft handshake for unpipelinable loops and
`report_fatal_error` for the trap that fires when a sentinel pointer
escapes its expected scope.

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

## Stickiness: the OR-only word

The status word at +40 is monotonic for the lifetime of one pass run. Every
write is an OR — `*(self+40) |= 4` — and there is no corresponding clear
inside the pass body. A pass that detects ten unpipelinable loops sets the
bit ten times; the second through tenth writes are no-ops at the
bit-pattern level but cost nothing and keep the call sites uniform. The
driver, not the pass, owns the lifecycle: it zeroes the word before the
pass walk begins and reads it once after the walk completes.

```c
/* Driver-side wrapper around one pass invocation. */
LogicalResult driver_run_pass(PassObject *pass, FuncOp func) {
    pass->status_word = 0;               /* clear sticky bits before walk */
    LogicalResult walk_result = pass->run(pass, func);
    if (pass->status_word & 4) {
        /* The pass set the soft-failure bit at least once during the walk.
         * Record it in the per-function pass-result map so downstream
         * passes can inspect it via pass_soft_failed(predecessor). */
        record_soft_failure(func, pass);
    }
    return walk_result;
}
```

Stickiness matters because TileAS passes walk the IR with op-level
granularity. A single function may contain a dozen loops; pipelining might
fail on three and succeed on the rest. The pass returns `success()` at the
function level (the IR is still valid), but the bit records that at least
one loop missed. The downstream reader does not need to know which loop
failed — only that the function is not fully pipelined and therefore that
the regional shrinker in D13 has reduced work to do. A multi-bit failure
count would carry no extra information given the binary nature of the
downstream skip decision.

The same pattern appears at wider granularity in the
[`ConvertTileASToLLVM` rewriter](../passes/tileas/convert-tileas-to-llvm.md):
when a single op fails to lower, the rewriter ORs `4` into its own status
word and continues with the next op rather than abandoning the function.
The driver lifts the bit to a hard failure only if the post-walk
verifier rejects the IR — typical for a partial lowering — but the
diagnostic stream still preserves every per-op error message.

## Worked Examples

### D11 unpipelinable loop

The most-walked failure path. `TileASUnspecializedPipeline` (D11) tries
each loop in a function and records a per-loop result.

```c
LogicalResult d11_run(PassObject *self, FuncOp func) {
    self->status_word = 0;
    func.walk([&](scf::ForOp loop) {
        if (failed(check_pipelinable_shape(loop))) {
            loop->emitRemark()
                << "Failed to pipeline loop";          /* severity 3, remark */
            self->status_word |= 4;                    /* sticky soft fail */
            return WalkResult::skip();                 /* leave loop intact */
        }
        rewrite_to_pipelined_form(loop);
        return WalkResult::advance();
    });
    return success();                                  /* IR still valid */
}
```

D11 returns `success()` even when every loop in the function fails — the
function compiles, the loops simply stay synchronous. D13 reads the bit
afterwards and skips its region shrinker on this function.

### D08 conflicting producer ops

The hard-but-recoverable case. `TileASMaterializeAsync` (D08) emits
severity-259 (error) diagnostics rather than remarks but still uses the
handshake bit instead of `signalPassFailure()`, so that the rest of the
compilation can produce a best-effort artifact for inspection.

```c
LogicalResult d08_check_pipeline(PassObject *self, PipelineOp pipe) {
    Operation *first = nullptr;
    for (Operation *producer : pipe.producers()) {
        if (!first) { first = producer; continue; }
        if (!same_instruction_kind(first, producer)) {
            producer->emitError()
                << "there are two `produce-one-like` operations using "
                << "different instructions to generate data into the "
                << "same pipeline. It's a bug of MaterializeAsync Pass.";
            self->status_word |= 4;
            return failure();                          /* skip this pipe */
        }
    }
    return success();
}
```

The diagnostic text is verbatim from the binary, including the
self-attributing `"It's a bug of MaterializeAsync Pass"` — TileAS treats
this as an internal inconsistency the user is unlikely to be able to fix,
but still recoverable enough to keep the pass-manager running.

### D13 downstream skip

The reader side. `TileASOptimizePipelineRegion` (D13) consults D11's bit
before walking — there is nothing to shrink on a function whose loops
stayed synchronous.

```c
LogicalResult d13_run(PassObject *self, FuncOp func) {
    PassObject *d11 = pass_manager_lookup(self->pm, "TileASUnspecializedPipeline");
    if (d11 && (d11->status_word & 4)) {
        /* D11 left at least one loop synchronous in this function. The
         * shrinker would walk produce_one/consume_one regions that were
         * never materialised; nothing to do. */
        return success();
    }
    func.walk([&](PipelineRegionOp region) {
        shrink_region(region);
    });
    return success();
}
```

Note that D13 does not set its own bit in the skip path: the skip is not a
failure, it is the absence of work. A downstream pass reading D13's bit
gets a clean signal that D13 had nothing to escalate.

## Implementation Constraints

A reimplementation must preserve four invariants.

First, the bit must be at the same offset and meaning across every pass. A pass whose PassObject lays out its status word at a different offset cannot participate in the handshake — the downstream-read pattern hard-codes `+40`.

Second, the diagnostic must precede the bit-set. If the bit is set before the diagnostic, a pass-manager that early-exits on bit-set may never publish the diagnostic to the user, and the failure becomes invisible.

Third, the bit is cumulative within one pass run. Multiple op-level failures inside one pass keep ORing `4` into the same word; the word never gets cleared mid-run. The driver clears the word before the pass starts and inspects it once the pass returns.

Fourth, the bit is per-pass-instance, not per-function. The driver owns the
clear-before-run; a pass that re-runs on a second function under the same
pass-manager instance gets a fresh zero. A reimplementation that caches
PassObjects across runs must clear the word at run entry, not at
constructor time.

## QUIRKs

### QUIRK: the bit lives at `+40` even when the PassObject is shorter

Several TileAS passes have PassObjects whose pass-specific tail ends well
before offset 40 — the field is padded out specifically to host the
handshake word at the conventional offset. A reimplementation that
size-optimises the PassObject layout and moves the status word inward
breaks the cross-pass read pattern: D13 and the rest of the family
hard-code `*((uint32_t *)((char *)pred + 40)) & 4` and will read garbage
from the displaced field. The offset is part of the binary contract.

### QUIRK: `pass[5] |= 4` reads as a u32 store at +20, not +40

A handful of disassembled call sites express the bit-set as
`pass[5] |= 4` where `pass` is a `uint32_t *` — that is, a 32-bit store at
offset 20, not 40. Both forms appear in the binary. They refer to the
**same** status word: the PassObject base pointer used in the `[5]` form
is offset 20 bytes into the structure compared with the base used in the
`+40` form (the inner pointer skips the pass-manager prelude and lands at
the body). A reimplementer reading the disassembly must check which base
pointer each call site is working from before deciding whether the bit
write targets the handshake word or some unrelated u32 — they look
identical at the instruction level. The handshake word is always the same
physical location regardless of which base the call site indexes.

### QUIRK: severity 259 still sets the same bit as severity 3

The handshake bit does not distinguish between an error-class diagnostic
(severity 259 / `0x103`) and a remark (severity 3). D08's "It's a bug of
MaterializeAsync Pass" and D11's "Failed to pipeline loop" both set the
same bit through the same `|= 4` write. The downstream reader cannot
recover the severity from the bit alone — it must consult the diagnostic
engine's recorded messages, or simply accept that "the predecessor had a
non-success outcome" is the only information the handshake carries. This
deliberate flattening keeps the inter-pass protocol one-bit-wide; severity
is a user-facing concept, not an inter-pass concept.

## Cross-References

[Error Handling and Diagnostics](../topics/error-handling-and-diagnostics.md)
is the canonical end-to-end page tying the handshake together with the
MLIR diagnostic engine and the driver-level exit codes.
[TileAS Async and Pipeline Family](../passes/tileas/async-pipeline-family.md) is the canonical example, with the handshake appearing in five of its passes.
[Pass Manager Internals](../pipeline/pass-manager-internals.md) covers the PassObject layout and the driver-side pass-result lookup the handshake rides on.
[Diagnostic Helpers](../infra/diagnostic-helpers.md) documents the diagnostic emitter that all these passes call before setting the bit.
[Diagnostic ABI and Helpers](diagnostic-abi-and-helpers.md) is the
body-layout reference for the diagnostics that pair with each bit-set.
[Invariants and Verifiers](../pipeline/invariants-and-verifiers.md) covers the cross-pass invariants the handshake protects.
[Common Compiler Patterns and Idioms](../topics/common-compiler-patterns-and-idioms.md) places the
`*(self+40) |= 4` convention in the catalogue of recurring structural moves alongside PIMPL,
vtable banks, and dispatcher tables.
