# Correctness Layers

## Abstract

Tileiras wraps four concentric verification layers around its pass pipeline, and a fifth external layer — ptxas — closes the loop after the PTX text leaves the process. Each layer catches a distinct class of bug. The per-op verifier catches structural malformations the moment an op is built. The pass-level verifier catches mismatches a single pass was supposed to repair but didn't. The module-level verifier catches whole-module invariants the pipeline as a whole is supposed to establish. The NVVM IR verifier catches NVPTX-specific errors that upstream LLVM's generic verifier knows nothing about. Anything that slips past all four lands on ptxas, which re-parses the PTX text and rejects programs the producer's invariants missed.

A reimplementer who omits any one layer creates a different class of wrong-output bug. Omitting the per-op verifier produces malformed IR that propagates silently into later passes and triggers obscure crashes far from the source. Omitting the NVVM verifier produces PTX that ptxas rejects with a generic syntax error, robbing users of the actionable diagnostic the higher layer would have emitted. The layers are not redundant; they are positioned to surface each bug class at the point where the producer still has the structural context needed to explain it.

## The Five Layers

### Layer 1: Per-Op Verifier

Every MLIR op carries a `verify()` method anchored in its `OperationName` slot at header offset `+0x40`; the per-op verifier hook is documented in detail in [Operation Layout — Pointer-Identity Dispatch](../mlir-infra/operation-layout.md#pointer-identity-dispatch). The hook fires every time an op is built through `OpBuilder::create<OpT>` and again after every pass when verify-each is on. The body checks per-op structural invariants — operand count, operand types, result count, result types, region structure, terminator kind, attribute presence, trait predicates such as `IsolatedFromAbove`.

The per-op verifier cannot reach across operations. It sees only the op handed to it and the values dangling off its operand list; it cannot inspect the parent block, the enclosing function, or another op that produced one of its operands. Cross-op invariants require a higher layer.

A concrete example. The TileAS TMA load op encodes the source coordinate list as operands 2..R+1, where R is the descriptor's box rank stored on operand 0. When a builder constructs the op with the wrong number of coordinate operands, the per-op verifier walks the operand list and emits:

```text
'nv_tileas.async.tiled_tma_load' op expects 3 coordinates, but got 2
```

The error surfaces inside the pass that built the op, before the pass's transformation pattern even returns. The pass-failure handshake propagates the diagnostic up to the driver and aborts the pipeline at the pass that introduced the malformed op rather than at a later consumer that would have seen incomprehensible IR.

### Layer 2: Pass-Level Verifier

The pass-manager between-pass verifier fires after each pass when verify-each is on (the default for non-Release builds), running the full `verify()` on the anchor operation, which in turn walks every op in the anchor's region and re-runs each per-op verifier. It catches invariants the pass was supposed to establish but didn't — partial rewrites, terminators left behind by an aborted pattern application, region asymmetries from a rewrite that fired once on a producer but failed on its matching consumer.

A concrete example. `MaterializeAsync` rewrites every pipeline op into a pair of producer and consumer regions. If the pass aborts a rewrite halfway through, leaving an `nv_tileas.async.pipeline.consume_one` op without its paired `produce_one`, the between-pass verifier walks the body and the pipeline-region verifier fires:

```text
'nv_tileas.async.pipeline.consume_one' op expects region arguement types to match with producer types [...], but got: [...]
```

(The typo `"arguement"` is verbatim in the binary; see [nv_tileas Verifiers — Region-Op Verifier Template](../dialects/nv_tileas/verifiers.md#region-op-verifier-template) for the full set of region-op invariants.) The pass-level verifier surfaces the broken state at the boundary of the pass that produced it, not at the boundary of the next pass that consumes it.

### Layer 3: Module-Level Verifier

The module-level verifier fires at named verifier passes (TileIR operation analysis, TileAA agent verifier, NVVM IR verifier) and at the end of the pipeline. It checks whole-module invariants: every kernel-marked function carries `nvvm.kernel` metadata, every symbol referenced by a `func.call` resolves to a function in the module, every type carried in an attribute dictionary belongs to the resolved type table, and every metadata-attached kernel entry has consistent launch-bound directives.

A concrete example. A late LLVM-tier pass strips function attributes during cleanup. If the strip pass runs after `KernelAttrPass` but before the NVVM verifier, a kernel function loses its `nvvm.kernel` attribute. The module-level verifier walks the function list, sees a function with kernel-shaped signature but no kernel metadata, and emits a diagnostic. The pipeline aborts before the NVPTX backend reaches instruction selection, which would otherwise have generated correctly typed but non-kernel-emittable code.

### Layer 4: NVVM IR Verifier

The NVVM IR verifier is the LLVM-tier sibling of the module verifier. It runs after MLIR-to-LLVM conversion and catches NVPTX-specific invariants the upstream LLVM `Verifier` knows nothing about: formal parameter-space overflow per the active SM target, launch-argument address-space mismatches that would be undefined at runtime, intrinsics used below their introducing SM, kernel metadata required on functions the launch operator references. The check anatomy is documented in [NVVM IR Verifier](../nvptx-passes/nvvm-ir-verifier.md).

A concrete example. A kernel argument list whose accumulated size exceeds the SM's parameter-space limit:

```text
Formal parameter space overflowed (40016 bytes required, max 1024 bytes allowed) in function big_kernel
```

Upstream LLVM's verifier accepts this function unconditionally because parameter space is an NVPTX concept; the NVVM verifier is the only place this becomes a hard error rather than a silent truncation in the backend.

### Layer 5: ptxas

The final correctness net is external. Tileiras emits PTX as ASCII text and hands it to ptxas through the subprocess harness described in [Handoff Protocol — Subprocess argv](../boundaries/ptxas-handoff-protocol.md#subprocess-argv). ptxas re-parses the PTX, applies its own validators, and assembles to cubin. Anything tileiras's four internal layers missed gets caught here: PTX-syntax violations from a buggy AsmPrinter template, instruction-availability mismatches against the `-arch` flag, virtual-to-physical register allocation conflicts, launch-bound directive collisions (`.maxntid` and `.reqntid` together, for instance, per [Handoff Protocol — Producer-side bug flagged](../boundaries/ptxas-handoff-protocol.md#producer-side-bug-flagged)).

ptxas diagnostics surface through the subprocess's stderr pipe and reach the user through the harness's diagnostic callback. The bug class ptxas catches uniquely is PTX-text well-formedness: tileiras's structural verifiers know what they emitted, ptxas knows what was actually serialized. A bug in the serializer that produces well-formed IR but malformed PTX is invisible to every internal layer.

## What Each Layer Catches

| Bug class | Layer 1 | Layer 2 | Layer 3 | Layer 4 | ptxas |
|---|---|---|---|---|---|
| Wrong operand count | YES | — | — | — | — |
| Wrong operand type | YES | — | — | — | — |
| Wrong attribute type | YES | — | — | — | — |
| Region missing terminator | YES | — | — | — | — |
| Region-asymmetric after pass | — | YES | — | — | — |
| Partial-rewrite leftover op | — | YES | — | — | — |
| Missing kernel metadata after pipeline | — | — | YES | — | — |
| Unresolved symbol reference | — | — | YES | — | — |
| Parameter-space overflow | — | — | — | YES | YES (different msg) |
| Use of sm_100 intrinsic with sm_90 target | — | — | — | YES | YES |
| Launch argument in wrong addrspace | — | — | — | YES | — |
| Launch target not a kernel | — | — | — | YES | — |
| PTX syntax error from AsmPrinter | — | — | — | — | YES |
| Invalid PTX register allocation | — | — | — | — | YES |
| `.maxntid` + `.reqntid` together | — | — | — | — | YES |
| Wrong-output (silent data race) | — | — | — | — | — |
| Wrong-output (numerical drift) | — | — | — | — | — |
| Wrong-output (scheduler picks bad II) | — | — | — | — | — |

Two columns repeat. Parameter-space overflow and SM-versioned intrinsics fire at the NVVM layer with a tileiras-shaped diagnostic and again at ptxas with a PTX-shaped diagnostic. The duplication is intentional: the higher-layer diagnostic names the MLIR-level construct that caused the overflow, which the PTX-level diagnostic cannot, so the higher-layer message is the actionable one.

## The Unverifiable

Some bug classes pass through every layer without being caught.

Data races in user-written warp-cooperative algorithms. The verifier infrastructure proves IR well-formedness; it does not prove execution behavior. A `cute_nvgpu` algorithm that reads a shared-memory buffer before the producer warp has signalled completion is structurally valid IR and produces structurally valid PTX. Race detection requires output testing under thread-perturbing instrumentation, not invariant checking.

Numerical-precision mismatches. A pass that picks the wrong rounding mode, or that fuses a multiply-add where the fused form differs from the source-level non-fused form, produces output that compiles cleanly and runs correctly in the sense that no instruction faults. The wrong number reaching the wrong destination is invisible to every structural verifier.

Performance regressions. The scheduler may pick an initiation interval one cycle larger than optimal; the layout-conversion pass may insert a stride-1 reshape where a stride-2 path was reachable; the register allocator may spill where a feasible coloring existed. None of these is a correctness violation, so no verifier fires. Performance regressions surface through end-to-end timing, not invariants.

Bugs in tileiras's own optimization decisions. The scheduler's cost model may rank a worse placement above a better one; the canonicalizer may rewrite an expression into a form the next pass cannot fold; the fusion pass may merge two ops whose fused form has worse register pressure than either alone. These are decision bugs, not correctness bugs, and they require human review or differential output testing to catch.

The four-plus-one layer model is exhaustive only for the bug class it was designed to catch: structural violations of typed IR. Everything else is the responsibility of output testing.

## Verifier-Ladder Algorithm

The pass manager invokes the four internal layers in a fixed sequence around each pass. The pseudocode below collapses the upstream `OpPassManager` machinery into the single contract a reimplementer must preserve.

```c
LogicalResult run_pipeline_with_verifiers(ModuleOp module, PassManager *pm) {
    for (Pass *pass : pm->passes) {
        // Layer 1 fires implicitly inside pass->run() every time the pass
        // constructs or mutates an op through OpBuilder. No explicit call
        // is needed at the driver level — the per-op verifier is part of
        // op construction itself and short-circuits pass->run on failure.
        if (failed(pass->run(module))) {
            return failure;
        }

        // Layer 2 fires after every pass when verify-each is on. The full
        // verify() walks every op in the anchor's region and re-runs each
        // per-op verifier, but it also catches cross-op invariants by
        // running region-bearing verifiers on parents.
        if (pm->verify_each) {
            if (failed(verify(module, /*verifyRecursively=*/true))) {
                return failure;
            }
        }

        // Layer 3 is scheduled as a named pass and runs only when the
        // pipeline reaches it; the loop above invokes it like any other
        // pass through pass->run().
    }

    // Layer 4 is the NVVM IR verifier, scheduled as an LLVM FunctionPass
    // after MLIR-to-LLVM conversion. It runs through the LLVM pass
    // manager's normal path and reports failure through the pass result.
    if (failed(run_nvvm_ir_verifier(module->target))) {
        return failure;
    }

    // Layer 5 is external. The PTX serializer produces ASCII PTX and the
    // subprocess harness hands it to ptxas. ptxas failures surface via
    // the harness's stderr capture and the driver's diagnostic callback.
    if (failed(serialize_and_invoke_ptxas(module))) {
        return failure;
    }

    return success;
}
```

Two ordering rules tie the layers together. Layer 1 always fires before `pass->run` returns; if it fires, the pass-failure handshake propagates and layer 2 is never reached. Layer 2 always fires before the next pass begins; if it fires, the pipeline aborts before any later layer sees the broken state. Layer 4 fires only once at the end of the LLVM-tier pipeline, after every MLIR-tier pass has had a chance to repair its own output. Layer 5 fires only when layers 1–4 have all signalled success.

## Failure Recovery

What happens after each layer fires.

**Layer 1.** `OperationName::create` or `OpBuilder::create<OpT>` returns null. The pass body sees a null result and either signals pass failure explicitly or returns without producing the expected new op, at which point the rewrite driver detects the missing replacement and signals pass failure on its own. The diagnostic is attached to the op being constructed, so it points at the source location of the input op the pass was trying to rewrite. See [Pass-Failure Handshake](../mlir-infra/pass-failure-handshake.md) for the propagation mechanism.

**Layer 2.** The between-pass verifier emits a diagnostic identifying the pass that produced the broken state and the op that violates the invariant. The pass-failure handshake propagates the failure up to the driver and the pipeline aborts.

**Layer 3.** The named verifier pass emits a diagnostic identifying the module-level invariant that failed and the symbol or attribute involved. The driver returns a non-zero exit code; no later pass runs.

**Layer 4.** The NVVM IR verifier calls `signalPassFailure()` on the LLVM `FunctionPass`. The LLVM pass manager picks the failure up on the `Pass::run` return path and aborts before the NVPTX backend reaches instruction selection.

**Layer 5.** ptxas exits non-zero. The subprocess harness reads ptxas's stderr through the captured pipe and forwards the text to the driver's diagnostic callback. The driver itself returns the harness's failure to its own caller; no cubin reaches the user.

In every case the diagnostic carries the highest-context layer's view of the bug — the per-op verifier names the op kind, the pass-level verifier names the pass, the module verifier names the module-wide invariant, the NVVM verifier names the SM target and the offending size, ptxas names the PTX line. The architecture trades early detection (most bugs surface at layer 1 or 2) for actionable context (the diagnostic that fires names the construct the user can change).

## Cross-References

[Pipeline Invariants and Verifiers](../pipeline/invariants-and-verifiers.md) walks the three internal verifier layers as the pass manager invokes them and is the primary reference for the in-pipeline contract. [NVVM IR Verifier](../nvptx-passes/nvvm-ir-verifier.md) covers layer 4 in depth, including the parameter-space sizer and the per-SM limit table. [Operation Layout — Pointer-Identity Dispatch](../mlir-infra/operation-layout.md#pointer-identity-dispatch) documents the per-op verifier hook anchored in the `OperationName` slot. [Pass-Failure Handshake](../mlir-infra/pass-failure-handshake.md) covers the propagation mechanism every layer uses to signal failure. [Handoff Protocol — Subprocess argv](../boundaries/ptxas-handoff-protocol.md#subprocess-argv) documents the layer-5 boundary, and [Handoff Protocol — Producer-side bug flagged](../boundaries/ptxas-handoff-protocol.md#producer-side-bug-flagged) covers the canonical bug class that escapes layers 1–4 and surfaces only at ptxas. [nv_tileas Verifiers — Region-Op Verifier Template](../dialects/nv_tileas/verifiers.md#region-op-verifier-template) is the canonical layer-1 example covered above.
[Troubleshooting and Known Issues](troubleshooting-and-known-issues.md)
maps each layer's canonical verbatim diagnostic to a symptom-driven
index — useful when a user has a stderr line and needs to identify
which verifier layer produced it before consulting the ladder here.
[Testing and Observability](testing-and-observability.md) covers the
test patterns that pin diagnostics from each verifier layer as golden
strings, and identifies which of the unverifiable bug classes from the
section above remain invisible to observable-behavior testing.
