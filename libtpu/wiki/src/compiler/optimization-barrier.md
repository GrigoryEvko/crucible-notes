# Optimization Barrier

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

The XLA optimization barrier — `HloOpcode::kOptimizationBarrier`, opcode **78** (`0x4E`), mnemonic `opt-barrier` — is the TPU compiler's *scheduling and fusion fence*. It is a single-operand identity op whose result has exactly the same shape (possibly a tuple) as its operand, and whose only job is to sit on the data-flow graph as an opaque node that no pass knows how to fuse, common-subexpression-eliminate, reorder, or hoist across. Ops on the producer side and ops on the consumer side of a barrier are separated by a real `producer → barrier → consumer` edge, and because every XLA pass already respects data dependencies, that one structural fact is enough to pin everything in place — *without* the op carrying any explicit "do-not-cross" flag.

The reader who knows LLVM should think of it as a cross between an `llvm.assume`-style optimization fence and a scheduling pin: it has no side effect, emits no instruction, and consumes zero cost in the cost model, yet its mere presence on the dependence graph stops the [fusion passes](fusion-patterns.md), CSE/GVN, the latency-hiding scheduler, and LICM from moving compute through it. On the TPU backend the barrier is purely an HLO-level hint: it is honoured through the entire HLO optimization and scheduling pipeline and then **erased** — never lowered to a hardware sync — by a late pass whose `name()` is, tellingly, `"cse_barrier_expander"`. After that pass, plus the `TupleSimplifier` that always follows it, the program reaching LLO contains no opt-barrier at all.

This page documents the three halves of the barrier's lifecycle on the TPU backend: (1) its **semantics** — the structural invariants and the twelve `Handle…` handlers that honour it as an opaque pass-through node; (2) its **insertion sites** — where the barrier enters a TPU `HloModule` (front end, the StableHLO→HLO converter, and the upstream producer passes including the rematerialization / host-offload / collective-pipeliner family that the memory-pressure machinery in [MSA](msa-overview.md) leans on); and (3) its **lowering** — the byte-exact erasure algorithm of `OptimizationBarrierExpander::RunImpl`, the three pipeline sites that add it, and the scheduler-mode gate under which the TensorCore async-op scheduler stage drops the barrier once it has served its scheduling purpose. It does **not** re-derive the latency-hiding scheduler, the fusion passes themselves, or the unrelated collective/megascale/bundle-packer "barriers" — those are named here only to draw the boundary.

For reimplementation, the contract is:

- **The op is a single-operand, same-shape identity** with no memory effect and zero cost; it introduces no new `HloValue` and aliases its operand's buffer at the points-to level.
- **It blocks optimization structurally, not by a flag.** Fusion can't fuse through an opaque op; CSE/GVN won't merge across distinct instructions with distinct users; the default visitor routes it to `DefaultAction` so passes that don't override it treat it as a generic opaque op.
- **Twelve classes provide an explicit handler**; all are pass-through except `AlgebraicSimplifier`, which is permitted to prune *dead tuple elements* through the barrier (but never to reorder or merge compute across it).
- **It is erased late, not lowered.** `OptimizationBarrierExpander` (name `"cse_barrier_expander"`) reconnects each user to `operand(0)`, migrates control deps onto the operand, rewrites the scheduled sequence to drop the barrier, and deletes the instruction. No hardware sync is emitted.

| | |
|---|---|
| **Opcode** | `HloOpcode::kOptimizationBarrier` == `78` (`0x4E`); mnemonic `opt-barrier` |
| **Opcode-string table** | `HloOpcodeString` @ `0x1e5ef000` — `off_21D30500[opcode + 0x80]` → `"opt-barrier"` |
| **Front-end builder** | `xla::XlaBuilder::OptimizationBarrier(XlaOp)` @ `0x1e409da0` (AddInstruction opcode 78, 1 operand) |
| **Free-function wrapper** | `xla::OptimizationBarrier(XlaOp)` @ `0x1e421e40` |
| **StableHLO→mhlo importer** | `StablehloToHloOpConverter<OptimizationBarrierOp>::matchAndRewrite` @ `0x16b5c540` |
| **Erasure pass** | `xla::OptimizationBarrierExpander::RunImpl` @ `0x164b3ba0`; `name()` @ `0x164b4380` → `"cse_barrier_expander"` |
| **Pass-add trampoline** | `HloPassPipeline::AddPass<OptimizationBarrierExpander>` @ `0x10972e60` (3 callers) |
| **Erasure gate (TPU)** | `*(DWORD*)(GetTpuCompEnv(...) + 5348) == 2` in `jellyfish::RunHloScheduler` @ `0x1096fac0`, line 1052 |
| **Handlers honouring it** | 12 distinct `HandleOptimizationBarrier` / value-set updaters (table below) |
| **Lowers to hardware sync?** | **No** — erased before LLO; the program reaching [LLO](compile-phases.md) has no opt-barrier |
| **Opt-barrier-specific flag** | `FLAGS_xla_tpu_aggressive_opt_barrier_removal` (only one; decision site not isolated) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Op Itself: Opcode 78

The opcode value is confirmed three independent ways in the binary, and all twelve honouring handlers gate on the same byte.

1. **Every handler tests `*(BYTE*)(inst + 12) == 78`** — the opcode byte lives at offset `+12` of an `HloInstruction`. This is the literal test in, e.g., the erasure loop (`if ( *(_BYTE *)(v21 + 12) == 78 )`) and the value-set updater's `CHECK`.
2. **`XlaBuilder::OptimizationBarrier` (`0x1e409da0`)** builds the instruction proto through the vtable `AddInstruction` slot with `opcode = 78` and `operand_count = 1`:

   ```c
   // 0x1e409da0, decompiled (vtable AddInstruction at +280)
   (*(...)( *(_QWORD *)a1 + 280LL))(&v19, a1, v15, /*opcode=*/78, v21, /*nops=*/1, v6);
   ```

3. **`HloOpcodeString` (`0x1e5ef000`)** is a flat table lookup `off_21D30500[(opcode + 0x80)]`; the `.rodata` mnemonic at slot `78` is the string `"opt-barrier"`.

### Structural invariants

The verifier and the analyses enforce a minimal contract:

- **Exactly one operand.** `ShapeVerifier::HandleOptimizationBarrier` (`0x1e441ce0`) calls `CheckOperandCount(inst, 1)` and, on failure, attaches source location `hlo_verifier.cc:342`. `HloDimensionInfoPropagation` (`0x10a07d80`) repeats the same check with the human-readable message `"Optimization barrier must have exactly one operand."` anchored at `hlo_dimension_analysis.cc:371`.
- **Result shape == `operand(0)` shape** (which may be a tuple). `ShapeVerifier` follows the operand-count check with `CheckShape(inst, operand(0)->shape())`.
- **Pass-through value identity.** The op introduces no new `HloValue`. It forwards the operand's value set, points-to set, evaluated literal, and value-semantics unchanged (see the handler table). Buffer assignment and MSA therefore treat the barrier's output buffer as the same buffer family as its operand.

> **NOTE —** the `+12` opcode-byte offset and the `+88` shape-pointer offset (`*(const xla::Shape **)(v3 + 88)` in `ShapeVerifier`) are layout facts of this build's `HloInstruction`; they are stable across the handlers on this page but are not an ABI promise across libtpu versions.

---

## MLIR Dialect Surfaces

Before the program reaches XLA HLO it can carry the op in any of four MLIR dialects, all of them variadic identity ops with `PairwiseSameOperandAndResultType` + `InferTypeOpInterface`. Shape/type inference is the identity map: `mlir::hlo::inferOptimizationBarrierOp` (`0x18145ae0`) sets each result type to the corresponding operand type.

| Dialect / op | Mnemonic | Notes (verified) | Confidence |
|---|---|---|---|
| `mlir::TF::XlaOptimizationBarrierOp` | `tf.XlaOptimizationBarrier` | Traits incl. `ConditionallySpeculatable`, `AlwaysSpeculatableImplTrait`, `MemoryEffectOpInterface`, `DerivedAttributeOpInterface`; `getEffects()` @ `0x1041f600` is **empty** (no declared memory effect) | CONFIRMED |
| `mlir::mhlo::OptimizationBarrierOp` | `mhlo.optimization_barrier` | build/create/parse/print @ `0x17da03c0`/`04e0`/`0580`/`0760` | CONFIRMED |
| `mlir::stablehlo::OptimizationBarrierOp` | `stablehlo.optimization_barrier` | reference interpreter @ `0x12645ee0` copies operands → results (identity) | CONFIRMED |
| `mlir::vhlo::OptimizationBarrierOpV1` | `vhlo.optimization_barrier_v1` | versioned serialization form | CONFIRMED |

The TF op's `foldHook` (`0x1012fac0`) is the generic op-fold trampoline; it does **not** fold the barrier away to its operand (there is no const-fold of the barrier). Because the op carries `AlwaysSpeculatableImpl` and an **empty** `getEffects`, its barrier semantics at the MLIR level come *purely from being a structural data-flow node* (operand → result dependency), not from a side-effect or memory-fence attribute. The fence is the edge, not a flag.

---

## Why the Barrier Blocks Optimization (the Mechanism)

This is the crux for a reimplementer: there is **no explicit "do-not-cross" bit** that every pass consults. The barrier blocks fusion / CSE / reorder / LICM through three properties that every XLA pass already respects.

### 1. It is a data-flow node, not a no-op

A chain `A → barrier → B` puts a real producer→consumer edge between `A` and `B`. The TPU [fusion passes](fusion-patterns.md) (`TpuInstructionFusion` / `TpuMultiOutputFusion`) walk producer-consumer pairs; the barrier is an opaque op they do not know how to fuse through, so `A` and `B` can never be merged into a single fused computation. The latency-hiding scheduler honours the same edges: it can neither hoist `B` above the barrier nor sink `A` below it, because that would violate the dependency.

### 2. It aliases its operand's buffer but is a distinct value holder

`TuplePointsToAnalysis::HandleOptimizationBarrier` (`0x1e387940`) takes `operand(0)` and calls `CreateCopiedPointsToSet(barrier, operand(0))`:

```c
// 0x1e387940, decompiled
v4 = (const xla::HloInstruction *)xla::HloInstruction::operand(a2, 0);
xla::TuplePointsToAnalysis::CreateCopiedPointsToSet(this, a2, v4, a3, a4);
```

The barrier's points-to set is a *copy* of the operand's, so buffer assignment / [MSA](msa-overview.md) treat the barrier output as the same buffer family as the operand — the barrier does **not** force a materializing copy by itself. In parallel, `HloDataflowAnalysis::UpdateOptimizationBarrierValueSet` (`0x1e4d18c0`) copies `operand(0)`'s value set into the barrier, asserting the opcode first:

```c
// 0x1e4d18c0, decompiled CHECK
CHECK("barrier->opcode() == HloOpcode::kOptimizationBarrier");  // hlo_dataflow_analysis.cc:674
```

Even though the buffers alias, CSE/GVN compare *value numbers*, and the barrier is a distinct instruction with its own users. Two identical subgraphs that each flow into a separate barrier are therefore **not merged across the barrier**. The erasure pass is named `cse_barrier_expander` precisely because CSE is the thing the barrier suppresses until late.

### 3. The default visitor routes it to `DefaultAction`

`DfsHloVisitorWithDefaultBase<…>::HandleOptimizationBarrier` (`0x10946240` for the `HloInstruction*` instantiation, `0x10c5b560` for the const-pointer instantiation) forwards to the vtable `DefaultAction` slot at offset `+1120`. Any pass that does **not** override `HandleOptimizationBarrier` — which is most of them, including LayoutAssignment, MSA, and BufferAssignment — therefore treats the barrier as a generic opaque op, respecting the dependency and threading layout / buffers through it without trying to simplify it away.

The cost model agrees the op is free: `HloCostAnalysis::HandleOptimizationBarrier` (`0x1e4812e0`) returns OK with zero added cost. The barrier does not bias fusion ranking against the surrounding ops; it simply isn't fusable through.

---

## Handlers That Honour the Barrier

Twelve classes provide an explicit handler for opcode 78. Every one is pass-through / opaque **except** `AlgebraicSimplifier`, which may prune dead tuple legs (but not reorder or merge compute). Each handler is byte-anchored to a decompiled function at the named address.

| # | Class / handler | Addr | Behaviour (recovered) | Anchor | Confidence |
|---|---|---|---|---|---|
| 1 | `ShapeVerifier` | `0x1e441ce0` | `CheckOperandCount(inst, 1)`; then `CheckShape(inst, operand(0)->shape())` | `hlo_verifier.cc:342` | CONFIRMED |
| 2 | `HloDataflowAnalysis::UpdateOptimizationBarrierValueSet` | `0x1e4d18c0` | barrier value set := `operand(0)` value set; `CHECK` opcode == `kOptimizationBarrier` | `hlo_dataflow_analysis.cc:674` | CONFIRMED |
| 3 | `TuplePointsToAnalysis` | `0x1e387940` | `CreateCopiedPointsToSet(barrier, operand(0))` — alias operand buffers | — | CONFIRMED |
| 4 | `HloCostAnalysis` | `0x1e4812e0` | return OK; zero cost | — | CONFIRMED |
| 5 | `HloEvaluator` | `0x1ddb0040` | `SetEvaluatedLiteralFor(barrier, Clone(GetEvaluatedLiteralFor(operand(0))))` (const-fold passthrough) | — | CONFIRMED |
| 6 | `AlgebraicSimplifierVisitor` | `0x1dd27360` | dead-tuple-element pruning: walk GTE (opcode 64) users, rebuild a smaller tuple operand + re-index GTEs; `CHECK use->opcode() == kGetTupleElement` | `algebraic_simplifier.cc:5299,5302` | CONFIRMED |
| 7 | `SpmdPartitioningVisitor` | `0x1c7f3540` | thunks to `HandleElementwise` (sharding flows through unchanged) | — | CONFIRMED |
| 8 | `HloValueSemanticsPropagation` | `0x111bece0` | `DeepCopyHloValueSemantics(barrier, GetInstructionSemantics(operand(0)))` | — | CONFIRMED |
| 9 | `HloDimensionInfoPropagation` | `0x10a07d80` | copy `operand(0)` DimensionInfo through; `CHECK operand_count == 1` | `hlo_dimension_analysis.cc:371,378` | CONFIRMED |
| 10 | `jellyfish::XPrecisionRewriteVisitor` (TPU) | `0x1115be20` | on precision rewrite, set barrier output shape := `operand(0)` new shape | `xprecision_rewriter.cc:4465` | CONFIRMED |
| 11 | `jellyfish::semantics_guided_sharding::DimLabelPropagation` (TPU) | `0x11197a80` | forward dim labels through | — | CONFIRMED |
| 12 | `DfsHloVisitorWithDefaultBase` (ptr / const-ptr) | `0x10946240` / `0x10c5b560` | route to `DefaultAction` (vtable `+1120`) — generic opaque handling | — | CONFIRMED |

### The one rewriting handler: AlgebraicSimplifier dead-tuple pruning

Handler #6 is the only pass that rewrites *around* the barrier, and it does so without ever crossing it. When the barrier wraps a tuple, the simplifier performs XLA's standard "shrink the barrier's input tuple to only the live elements" transform. The decompile shows it walking the barrier's users while they are GTEs (`while ( *((_BYTE *)*v9 + 12) == 64 )`, opcode 64 = `kGetTupleElement`), marking an index live if the GTE has more than one user, or the indexed operand is the module entry root, or the operand is itself side-effecting. Dead legs are dropped, a new smaller `kTuple` is created (`HloInstruction::CreateGetTupleElement` is used to re-index surviving GTEs), and the barrier's operand-0 is replaced with the smaller tuple (`ReplaceOperandWithDifferentShape`). A defensive `CHECK use->opcode() == HloOpcode::kGetTupleElement` (`algebraic_simplifier.cc:5302`) guards the assumption that every user is a GTE:

```c
// 0x1dd27360, decompiled fragments
while ( *((_BYTE *)*v9 + 12) == 64 )            // walk GTE users (kGetTupleElement)
  ...
TupleElement = xla::HloInstruction::CreateGetTupleElement(v25, v21, v20);   // re-index
...
CHECK("use->opcode() == HloOpcode::kGetTupleElement");   // algebraic_simplifier.cc:5302
```

This proves the barrier is opaque to fusion/CSE but still permits dead-code-style cleanup of unused tuple legs. It does **not** permit reordering or merging compute across the fence.

> **NOTE —** the absence of a `HandleOptimizationBarrier` override in `MemorySpaceAssignment` / `LayoutAssignment` / `BufferAssignment` is itself evidence: those passes use the `DefaultAction` opaque path (handler #12). The barrier aliases its operand's buffer (handler #3), so MSA treats the two as one buffer family — but whether MSA refuses to prefetch *across* a barrier is **inferred** from the opaque-op path, not directly traced. Treat the cross-barrier prefetch policy as **LOW confidence** pending a direct MSA trace; see [MSA overview](msa-overview.md).

---

## Insertion: Where Barriers Enter a TPU Module

The barrier reaches a TPU `HloModule` from two surfaces — the front end / IR import, and the upstream producer passes — both of which call (a possibly inlined) `HloInstruction::CreateOptimizationBarrier` with opcode arg 78.

### Front-end / IR surface (fully recovered)

```text
jax.lax.optimization_barrier / XLA client
  → xla::XlaBuilder::OptimizationBarrier(XlaOp)   0x1e409da0     [opcode 78, 1 operand]

OR via MLIR:
  stablehlo.optimization_barrier
    → StablehloToHloOpConverter<OptimizationBarrierOp>::matchAndRewrite   0x16b5c540
      → mlir::mhlo::OptimizationBarrierOp::create
    → mhlo → HLO importer → HLO kOptimizationBarrier
```

The free function `xla::OptimizationBarrier(XlaOp)` (`0x1e421e40`) is a thin wrapper that forwards to the builder method.

### Producer passes (partial — create call is inlined)

The `CreateOptimizationBarrier` factory is inlined into the helper methods of several upstream passes, so it could not be isolated as a distinct symbol. The candidate producers are identified by behaviour and by the presence of opcode-78 construction sites; each is present as a decompiled function in this build:

| Producer pass | Entry | Why it wraps a value in a barrier (recovered behaviour) | Confidence |
|---|---|---|---|
| `HloRematerialization::RunImpl` | `0x1d6c8300` | rematerialization under memory pressure — a barrier pins a recomputed value so it is not re-fused/CSE'd back into the original, forcing the recompute to materialize at the chosen point | HIGH |
| `HostOffloader::RunImpl` | `0x1107d5e0` | host-offload buffer wrapping — host buffers get wrapped to force materialization across the offload boundary | HIGH |
| `CollectivePipeliner::RunPipeliner` | `0x12ffb100` | collective pipelining ordering — a barrier pins the start/done boundary of a pipelined collective so it cannot be reordered into the steady state | HIGH |
| `WhileLoopUnroller::RunImpl` | `0x12eeb500` | while-loop unrolling — barriers separate unrolled iteration bodies that must not be re-merged by CSE | MEDIUM |

> **WARNING —** the inlined `CreateOptimizationBarrier` create-site could **not** be isolated as a distinct symbol in ~12 decompile greps. The producer list above is recovered by behaviour and by the presence of these passes in the binary, not by a clean producer-by-producer trace of the opcode-78 construction. A precise producer trace is a follow-up; treat the per-producer *rationale* as HIGH/MEDIUM as marked, not byte-exact.

The memory-pressure tie-in is the reason this op matters to the compiler back end: rematerialization (the classic "recompute instead of spill" trade) is exactly the consumer of [MSA](msa-overview.md)'s memory-pressure signal, and it uses opt-barriers to make recomputed values *stay* recomputed rather than being optimized back together. The barrier is the contract that keeps a deliberately-duplicated computation duplicated.

---

## Lowering: the Barrier Is Erased, Not Synced

The TPU optimization barrier **never reaches LLO**. It is removed by the late HLO pass `xla::OptimizationBarrierExpander`, whose `name()` (`0x164b4380`) returns `"cse_barrier_expander"` — confirmed verbatim in the decompile:

```c
// 0x164b4380
absl::string_view name() const { return "cse_barrier_expander"; }
```

### The erasure algorithm (RunImpl @ 0x164b3ba0)

`RunImpl` (1981 bytes, 99 basic blocks) collects opcode-78 instructions, rewrites the scheduled sequence to drop them, then reconnects and deletes each barrier. The decompile confirms every step: `MakeNonfusionComputations` (line 85), the opcode `== 78` test (line 153), `HloSchedule::set_sequence` (lines 373/384), and the `CopyAllControlDepsTo` → `ReplaceAllUsesWith` → `DropAllControlDeps` → `RemoveInstruction` tail (lines 427/433/439/445).

```cpp
// xla::OptimizationBarrierExpander::RunImpl   @ 0x164b3ba0
// name() == "cse_barrier_expander"   (optimization_barrier_expander.cc)
absl::StatusOr<bool> RunImpl(
    HloModule* m,
    const absl::flat_hash_set<absl::string_view>& execution_threads) {
  std::vector<HloInstruction*> barriers;
  // NB: MakeNonfusionComputations — fusion bodies are skipped on purpose.
  for (HloComputation* comp : m->MakeNonfusionComputations(execution_threads)) {
    bool comp_has_barrier = false;
    for (HloInstruction* inst : comp->instructions())
      if (inst->opcode() == HloOpcode::kOptimizationBarrier) {     // *(BYTE*)(inst+12) == 78
        barriers.push_back(inst);
        comp_has_barrier = true;
      }
    if (comp_has_barrier && m->has_schedule()) {                   // gated on module schedule flag
      // Rebuild the computation's scheduled sequence WITHOUT the barriers.
      std::vector<HloInstruction*> seq;
      for (HloInstruction* i : m->schedule().sequence(comp).instructions())
        if (i->opcode() != HloOpcode::kOptimizationBarrier) seq.push_back(i);
      m->mutable_schedule()->set_sequence(comp, seq);              // 0x164b4165 / 0x164b412a
    }
  }
  for (HloInstruction* b : barriers) {
    HloInstruction* op = b->mutable_operand(0);
    TF_RETURN_IF_ERROR(b->CopyAllControlDepsTo(op, op));           // preserve ordering edges
    TF_RETURN_IF_ERROR(b->ReplaceAllUsesWith(op));                // users read the operand directly
    TF_RETURN_IF_ERROR(b->DropAllControlDeps());
    TF_RETURN_IF_ERROR(b->parent()->RemoveInstruction(b));        // delete the barrier
  }
  return !barriers.empty();
}
```

At lowering time the barrier is therefore **erased**: every user is reconnected to the barrier's operand, control dependencies are migrated onto the operand so the relative ordering survives, and the instruction is deleted. It is purely an HLO-level scheduling / optimization hint with **no hardware sync emitted** — confirming the hypothesis that it serves its scheduling purpose and then disappears. (Contrast with the collective and bundle-packer barriers below, which *do* emit real hardware sync.)

> **NOTE —** the `MakeNonfusionComputations` walk means barriers that ended up inside a fusion body are not touched here. In practice a fusion never absorbs a barrier (mechanism point #1), so this is not a gap; it would only matter if some other pass had buried a barrier in a fusion body, which is not observed.

### How erasure preserves async-window pinning

Pinning of async-copy / collective windows is a consequence of the structural data dependency plus control dependencies, not a dedicated mechanism — and the erasure pass is careful to keep it after the barrier is gone:

- During scheduling, an async op is a start/done pair (`kAsyncStart` opcode 17 / `kAsyncDone`, or collective-start / collective-done). If a barrier sits between the consumer of an async result and a later op, the latency-hiding scheduler cannot move that op above the barrier, so the async window cannot be illegally collapsed.
- The schedule produced *with* the barrier present is already frozen by `set_sequence` (the barrier is filtered out of the sequence in place, leaving the surrounding order intact).
- `CopyAllControlDepsTo(operand)` migrates any control edges the barrier carried onto its operand, so the relative ordering survives even after the barrier instruction is deleted.

This is the precise difference from the collective / async-tree barrier (below): the optimization barrier pins ordering *at compile time in the HLO schedule* and then vanishes; the collective barrier emits an actual runtime synchronization.

---

## Where the Expander Runs (Pipeline Placement)

`HloPassPipeline::AddPass<OptimizationBarrierExpander>` (`0x10972e60`) has three callers; all three target functions are present as decompiled symbols.

| Caller | Entry | Role | Relevant to TPU? |
|---|---|---|---|
| `xla::cpu::CpuCompiler::RunHloPassesThroughLayoutAssn` | `0x14bb99e0` | CPU backend | No — ignore for TPU |
| `xla::jellyfish::(anon)::RunHloScheduler` | `0x1096fac0` | TPU scheduler stage; AddPass inside `RunTensorCoreAsyncOpScheduler`, gated, then `TupleSimplifier` | **Yes** |
| `xla::jellyfish::(anon)::PostOptimizationPipeline` | `0x1093fd40` | TPU late-cleanup stage; the other erasure site | **Yes** |

### The scheduler-stage gate (verified)

Inside `RunHloScheduler` the expander is added only when a scheduler-mode field of the TPU compilation environment equals 2, and is immediately followed by a `TupleSimplifier`. The decompile shows the gate at line 1052 and the two `AddPass` calls at 1054–1055, sitting just before `AsyncCollectiveMerger` at 1058–1059:

```c
// 0x1096fac0 — jellyfish::RunHloScheduler, lines 1052..1059
if ( *(_DWORD *)(xla::jellyfish::GetTpuCompEnv(v389[0], ptr) + 5348) == 2 ) {
  xla::HloPassPipeline::AddPass<xla::OptimizationBarrierExpander>(&v341);   // line 1054
  xla::HloPassPipeline::AddPass<xla::TupleSimplifier>(&v341);               // line 1055
}
xla::jellyfish::AsyncCollectiveMerger::PreLatencyHidingSchedulerConfig(...); // line 1058
xla::HloPassPipeline::AddPass<xla::jellyfish::AsyncCollectiveMerger,...>(...); // line 1059
```

So the expander runs **after** the LatencyHidingScheduler / AsyncOpScheduler have already used the barrier as a scheduling pin, and **before** the async-collective merge step that no longer needs it. The `TupleSimplifier` right after the expander matters: because the barrier typically wraps a tuple and consumers reach its legs through GTEs, erasing the barrier leaves `GetTupleElement(Tuple(...))` chains that `TupleSimplifier` then collapses.

### Sequence intuition for the TPU path

```text
... LayoutAssignment → fusion (P-3-76, fusion-patterns) → scheduling
    (LatencyHidingScheduler / AsyncOpScheduler honour the barrier edges to
     pin async-copy / collective boundaries)
  → cse_barrier_expander + TupleSimplifier   (barrier erased; GTE/tuple
     chains it left behind are cleaned up)
  → AsyncCollectiveMerger
  → LLO lowering — sees NO opt-barrier
```

### The one opt-barrier-specific knob

`FLAGS_xla_tpu_aggressive_opt_barrier_removal` is the only optimization-barrier-specific flag in the binary. It controls how aggressively optimization barriers are stripped (presumably allowing earlier or more removal than the default scheduler-gated site).

> **WARNING —** the `xla_tpu_aggressive_opt_barrier_removal` decision site (the branch that reads the flag) was **not** isolated, and the `GetTpuCompEnv` field at `+5348` is a scheduler-mode enum whose other values and field name are unrecovered. Treat the `== 2` gate as the *observed* enabling value, not a documented enum — **LOW confidence** on the enum's full meaning.

---

## Not This Op: the Other Three "Barriers"

The word "barrier" in libtpu names four unrelated mechanisms. Only #1 is this page's subject; the others are listed only to draw the boundary, and unlike the optimization barrier they emit real hardware sync.

| Variant | What it is | Representative symbols / addrs | Emits real sync? |
|---|---|---|---|
| **1. HLO optimization barrier** (this page) | opcode 78; compile-time CSE/fusion/reorder blocker; erased late | `OptimizationBarrierExpander` `0x164b3ba0` (name `cse_barrier_expander`); all `HandleOptimizationBarrier` handlers | **No** — erased before LLO |
| **2. Collective / async-tree barrier** | `CustomCall("BarrierStart")` / `"BarrierDone"`; pins collective start/done; megascale & ICI | `async_barrier_util::GetBarrierStartFromAsyncStart` `0x11007c80`; `AllToAllEmitterBase::EmitBarrierStartImpl` `0x10f07240` / `EmitBarrierDoneImpl` `0x10f07d40`; `BarrierStartEmitter` `0x10e8ece0` | Yes — collective sync |
| **3. Megascale / barrier-assignment config** | `BarrierConfig` proto with `barrier_type()`; per-core sflag/DMA barrier | `InferBarrierConfig` `0x1376c240`; `BarrierAssignment::BarrierAssignment` `0x109c6e40`; `TensorCoreBarrierAssignment::ForEachCollective` `0x109c7060`; `SparseCoreBarrierAssignment::AssignBarriersForKernels` `0x109c6080` | Yes — runtime sync (sflag/DMA) |
| **4. LLO bundle-packer scheduling barrier** | a VLIW-bundle "point of no return" the packer cannot move ops across | `BundlePacker::BarrierHere` `0x14021de0` (bundle_packer.h:207); `BundlePacker::EstablishBarrier` `0x14021d00`; `SlotTracker::UpdatePointOfNoReturn` | N/A — packer-internal fence |

Distinguishing opcode tests in the binary:

- opt-barrier: `inst.opcode() == 78`
- async-start (collective barrier context): `inst.opcode() == 17`
- barrier-start tuple wrapping: operand `opcode() == 0x81` (`kTuple`), `IsCustomCall(…, "BarrierStart")`

There are 25 barrier-family flags in the binary; **only one** (`xla_tpu_aggressive_opt_barrier_removal`) concerns the optimization barrier. The other 24 (`xla_tpu_enable_megascale_barrier`, `xla_tpu_force_global_barriers`, `xla_tpu_use_custom_tree_barrier`, `xla_tpu_use_dma_for_custom_barrier`, etc.) all configure the collective/megascale/hardware-sync barriers — variants 2–4, not this op.

---

## Cross-References

- [Compiler overview](overview.md) — the five-phase spine and IR layer stack the barrier travels through.
- [Compile phases](compile-phases.md) — where HLO optimization, scheduling, and LLO lowering sit; the barrier is erased before the LLO crossing.
- [Fusion patterns](fusion-patterns.md) — the `TpuInstructionFusion` / `TpuMultiOutputFusion` passes the barrier is opaque to.
- [HLO pre-passes](hlo-pre-passes.md) — the early HLO-level transforms that run before scheduling.
- [MSA overview](msa-overview.md) — the memory-pressure / rematerialization consumer that drives barrier insertion, and the buffer-family aliasing the barrier's points-to copy preserves.
