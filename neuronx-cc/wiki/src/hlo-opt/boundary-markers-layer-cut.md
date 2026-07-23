# Boundary Markers & Layer-Cut Analysis

> *All addresses on this page apply to neuronx-cc `hlo-opt` / `hlo2penguin` build `2.24.5133.0+58f8de22` (cp310). The cp311/cp312 wheels reorder a handful of symbols; the names and the algorithm are identical. Addresses are VAs as IDA reports them (`.text` is VA==file-offset in this binary).*

## Abstract

The **boundary-marker subsystem** is how neuronx-cc tags *spans* of an XLA/HLO graph so that later stages can cut the program at semantically meaningful seams — most importantly the **autograd forward/backward split** and the **per-layer pipeline cuts** that gradient-checkpointing and pipeline-parallel partitioning need. A marker is not an op that computes anything: it is a paired `Start`/`End` `kCustomCall` (opcode `0x2B`) sentinel wrapped around a value, carrying a `boundaryCount=<N>` backend-config so a consumer can pair an opening marker with its close. Markers are structural metadata that ride through the optimizer and are stripped before codegen.

Two marker *families* share one detector. **Module markers** — `AwsNeuronModuleMarker{Start,End}-{Forward,Backward}` — are emitted *upstream* (the PyTorch-XLA / frontend autograd instrumentation; no pass in either binary creates them) and delimit the forward vs. backward training sub-graph and each layer's span. **Loop-body markers** — `NeuronBoundaryMarker-{Start,End}` — are emitted by `NeuronAddBoundaryMarker` inside `hlo-opt` itself, bracketing every `kWhile` body root as a side-effect of while-loop unrolling. Both families are recognized by the single predicate `hlo_utils::isMarker` (@0x1ebdfa0), which is the union of all six target names.

The lifecycle is **add → canonicalize → remove**. `NeuronAddBoundaryMarker` stamps loop-body markers; `CanonicalizeBoundaryMarker` (pass #30) normalizes every marker into the canonical single-tuple-operand form `marker(tuple(ops…))` with `get-tuple-element` projections, so removal and the partitioner can rely on one uniform shape; `BoundaryMarkerRemoval` (pass #31) deletes every marker and splices its wrapped value straight back to the consumers. The penguin partitioner in `hlo2penguin` runs *its own* copy of canonicalize+remove, but only **after** `MarkerAnalyzer::analyzeLayerBoundary` has already read the Start/End spans and turned them into layer cut-points. This page reconstructs all of that from the binaries.

For reimplementation, the contract is:

- **The custom-call wire schema** for all six marker targets: opcode, operand arity, shape rule, the `custom_call_target` taxonomy, the `boundaryCount=<N>` opaque, and the `api_version` value that distinguishes emit from canonicalize.
- **The three hlo-opt passes** — `NeuronAddBoundaryMarker::Run` / `TransformWhileLoop`, `CanonicalizeBoundaryMarker::Run` (#30), `BoundaryMarkerRemoval::Run` (#31) — as annotated pseudocode, including the two removal modes and the `NCC_ITUP001` legality rule.
- **The consumer contract**: how `MarkerAnalyzer::analyzeLayerBoundary` and `MarkerSplitter::split` in `hlo2penguin` use `isMarkerStart`/`isMarkerEnd`/`getMarkerBackendConfig` to derive fwd/bwd and layer cut points, and *why* those cuts feed checkpointing and pipeline partitioning.

| | |
|---|---|
| **Producer (loop-body)** | `xla::hilo::NeuronAddBoundaryMarker::Run` @0x2002370 (313 B); `TransformWhileLoop` @0x2001ff0 (892 B) |
| **Canonicalize (#30)** | `xla::CanonicalizeBoundaryMarker::Run` @0x1e8bfa0 (1849 B); `name()` @0x1e8bd00 → `canonicalize-boundary-marker` |
| **Remove (#31)** | `xla::BoundaryMarkerRemoval::Run` @0x1e8b1a0 (2909 B); `name()` @0x1e8a430 → `boundary-marker-removal` |
| **Detectors (hlo-opt)** | `hlo_utils::isMarker` @0x1ebdfa0; `hlo_utils::isMarkerEnd` @0x1ebdf20 |
| **Backend-config parse** | `hilo::ExtractBoundaryCountFromBackendConfig` @0x1fcb170 |
| **Consumers (hlo2penguin)** | `MarkerSplitter::split`, `MarkerAnalyzer::analyzeLayerBoundary`, `preprocessBoundaryMarker`, `canonicalizeAndRemoveBoundaryMarker`, `removeBoundaryMarker`; `isMarkerStart`, `getMarkerBackendConfig` |
| **IR level** | XLA/HLO (entry computation, post-order), before penguin lowering |
| **Marker opcode** | `kCustomCall` = `0x2B`; opaque token `boundaryCount=` |

---

## 1. The Two Marker Families

A marker brackets a value `v` with two `kCustomCall` instructions: `Start` consumes `v` and every former consumer of `v` is redirected to read `Start`; `End` consumes `Start`. The pair is the open/close of a span. Direction (which sub-graph) is carried *only* in the target-name suffix; pairing across a span is carried by the `boundaryCount=<N>` opaque.

### Target-name taxonomy

All six names are verbatim string literals in the binary (`isMarker`/`isMarkerEnd` compare against them with `std::string::compare`). The suffix grammar is `{Start|End}` × `{-Forward|-Backward}` for module markers; loop-body markers have only `{Start|End}`.

| `custom_call_target` | Family | Direction | Producer | Confidence |
|---|---|---|---|---|
| `NeuronBoundaryMarker-Start` | loop-body | — | `NeuronAddBoundaryMarker` (this binary) | CERTAIN |
| `NeuronBoundaryMarker-End` | loop-body | — | `NeuronAddBoundaryMarker` (this binary) | CERTAIN |
| `AwsNeuronModuleMarkerStart-Forward` | module | forward | upstream (PyTorch-XLA / frontend) [INFERRED] | CERTAIN |
| `AwsNeuronModuleMarkerStart-Backward` | module | backward | upstream [INFERRED] | CERTAIN |
| `AwsNeuronModuleMarkerEnd-Forward` | module | forward | upstream [INFERRED] | CERTAIN |
| `AwsNeuronModuleMarkerEnd-Backward` | module | backward | upstream [INFERRED] | CERTAIN |

> **NOTE —** there is no `CreateCustomCall("AwsNeuronModuleMarker…")` site in either `hlo-opt` or `hlo2penguin`. The four module markers *arrive in the input HLO* from the driver/frontend autograd instrumentation; this binary only consumes them. The `-Forward`/`-Backward` suffix is the only carrier of the autograd direction — there is no separate dtype suffix the way the collective/softmax custom-calls have one, because a marker's shape is simply the wrapped value's shape.

### The shared detector

```c
// hlo_utils::isMarker(const HloInstruction* inst)            // 0x1ebdfa0  (124 B)
bool isMarker(inst):
    if opcode(inst) != kCustomCall:          // byte (inst+0x14) != 0x2B
        return isMarkerEnd(inst);             // tail-call — a non-CustomCall is never a Start
    t = inst->custom_call_target();
    return t == "NeuronBoundaryMarker-Start"
        || t == "AwsNeuronModuleMarkerStart-Forward"
        || t == "AwsNeuronModuleMarkerStart-Backward"
        || isMarkerEnd(inst);                 // fall through to the 3 End names

// hlo_utils::isMarkerEnd(const HloInstruction* inst)         // 0x1ebdf20  (117 B)
bool isMarkerEnd(inst):
    if opcode(inst) != kCustomCall: return false;
    t = inst->custom_call_target();
    return t == "NeuronBoundaryMarker-End"
        || t == "AwsNeuronModuleMarkerEnd-Forward"
        || t == "AwsNeuronModuleMarkerEnd-Backward";
```

So `isMarker` ≡ "is any of the six marker targets (Start or End, loop-body or module)" and `isMarkerEnd` ≡ "is one of the three End targets".

*Anchors: both string-compare chains transcribed byte-for-byte from `0x1ebdfa0` / `0x1ebdf20`; the opcode gate in each is `cmpb $0x2B, 0x14(inst)`.*

> **QUIRK —** `hlo-opt` has *only* `isMarker` and `isMarkerEnd`. The companion `hlo_utils::isMarkerStart` and `hlo_utils::getMarkerBackendConfig` exist **only in `hlo2penguin`** — because only the partitioner needs to read direction and backend-config explicitly. In `hlo-opt`, canonicalize and removal treat all six names uniformly, so a Start-vs-End or fwd-vs-bwd distinction is never needed.

---

## 2. The Custom-Call Wire Schema

A marker custom-call, on the wire, is:

| Field | Value | Confidence |
|---|---|---|
| **opcode** | `kCustomCall` = `0x2B` | CERTAIN |
| **operands** | exactly 1 (the wrapped value); after canonicalization, 1 *tuple* | CERTAIN |
| **shape** | the wrapped value's shape; after canonicalization, the tuple's shape | CERTAIN |
| **custom_call_target** | one of the six names in §1 | CERTAIN |
| **opaque / backend_config** | literal `"boundaryCount=<N>"` (token `boundaryCount=` + base-10 int via `FastIntToBuffer`) for loop-body markers; module-marker config is read by `getMarkerBackendConfig` and its full schema is upstream | CERTAIN (loop-body) |
| **api_version** | `2` (`API_VERSION_STATUS_RETURNING`) on emit; `1` (`API_VERSION_ORIGINAL`) after canonicalize-rebuild | CERTAIN |

The `<N>` in `boundaryCount=<N>` is a monotone per-module counter (the pass's `[this+8]` field, incremented after each while-loop). It is the **pairing key**: a consumer pairs an opening marker with its closing marker by matching `boundaryCount`. The dedicated parser `hilo::ExtractBoundaryCountFromBackendConfig` (@0x1fcb170) recovers `<N>` from the backend-config string. The symbol is present in the function table; that it parses the `boundaryCount=` token follows from the token string and this function being the only two `boundaryCount` sites in the binary.

The `CustomCallApiVersion` enum is verbatim in `.rodata`: `API_VERSION_UNSPECIFIED` (0), `API_VERSION_ORIGINAL` (1), `API_VERSION_STATUS_RETURNING` (2), `API_VERSION_STATUS_RETURNING_UNIFIED`, `API_VERSION_TYPED_FFI` (3) — all five appear as strings.

> **GOTCHA —** the `api_version` *drops* from 2 to 1 when `CanonicalizeBoundaryMarker` rebuilds a marker. A reimplementation that asserts `api_version == 2` on every marker will reject every canonicalized one. The version is not meaningful to the marker's semantics — it is an artifact of which `CreateCustomCall` call site produced the instruction.

---

## 3. NeuronAddBoundaryMarker — the Loop-Body Producer

### Purpose

Stamp a `NeuronBoundaryMarker-Start`/`-End` pair around every `kWhile` body's root, so the unroller and the downstream index-materialization machinery can identify one per-iteration boundary. This is internal, transient instrumentation that is removed before codegen.

### Entry Point

```text
NeuronWhileLoopUnroller::Run  @0x20019df          ── #112 while_loop_unroller
  └─ (stack-constructs the pass; vtable 0x412ef0 stored at -0x260)
       └─ NeuronAddBoundaryMarker::Run  @0x2002370
            └─ NeuronAddBoundaryMarker::TransformWhileLoop  @0x2001ff0
```

> **GOTCHA —** `NeuronAddBoundaryMarker` looks like a registry pass — it has a `name()` stub @0x2001e30 returning `neuron_add_boundary_marker` — but it is not one. No `RegisterNeuronAddBoundaryMarker` factory exists, and the only caller of `Run` is `NeuronWhileLoopUnroller::Run`, which stack-constructs the pass (vtable `_ZTVN3xla4hilo23NeuronAddBoundaryMarkerE` @0x412ee0, vptr +0x10 = 0x412ef0) and invokes it directly on the module. Loop-body markers are therefore stamped as a side-effect of while-loop unrolling, and searching the `--passes` table for them turns up nothing.

### Algorithm

```c
// NeuronAddBoundaryMarker::Run(HloModule* m, threads)        // 0x2002370 (313 B)
bool Run(m, threads):
    changed = false
    for comp in m->computations():                  // [m+0x40]..[m+0x48], stride 8 — ALL computations
        n = (comp.instr_end - comp.instr_begin) >> 4 // [comp+0x40]/[comp+0x48], stride 0x10
        for i in 0..n:
            inst = comp.instructions[i]
            if opcode(inst) == kWhile:               // cmpb $0x79, (inst+0x14)
                TransformWhileLoop(inst)             // 0x2001ff0
                changed = true
    return changed                                   // writes {nullptr, changed} to the StatusOr out

// NeuronAddBoundaryMarker::TransformWhileLoop(HloInstruction* while_inst)  // 0x2001ff0 (892 B)
void TransformWhileLoop(while_inst):
    wb   = while_inst->while_body()                  // [while_inst] → computation
    root = wb->root_instruction()                    // via [wb+0x28] root index
    cnt  = this->boundaryCount                        // [this+8]
    opaque = StrCat("boundaryCount=", FastIntToBuffer(cnt))

    // --- Start marker: wraps the body root ---
    start = CreateCustomCall(shape       = root->shape(),
                             operands    = Span{root},                 // size 1
                             target      = "NeuronBoundaryMarker-Start",
                             opaque      = opaque,
                             api_version = 2 /*STATUS_RETURNING*/)
    wb->AddInstruction(start, /*name*/ "")
    root->ReplaceAllUsesWith(start)                  // every old user of root now reads start

    // --- End marker: wraps the Start marker ---
    end = CreateCustomCall(shape       = start->shape(),
                           operands    = Span{start},                  // size 1
                           target      = "NeuronBoundaryMarker-End",
                           opaque      = opaque,                        // SAME boundaryCount
                           api_version = 2)
    wb->AddInstruction(end, "")
    wb->set_root_instruction(end, /*accept_different_shape=*/ false)
    this->boundaryCount += 1                          // add qword [this+8], 1
```

Result graph for the while body: the former root `r` becomes `End(Start(r))`, with `r`'s previous consumers redirected through `Start`. Both markers carry the same `boundaryCount=N` opaque and the same shape (= `r`'s shape).

*Anchors: full disassembly of `0x2002370` and `0x2001ff0`. The `CreateCustomCall` register setup reads `rcx`=1 operand, target length/pointer in `r8`/`r9`, stack-pushed `2` for api_version and the opaque below it; `set_root_instruction`'s second argument is 0. `Run`'s constant pool is `{1,4,8,16,40,121}`, where 121 = `0x79` = `kWhile`.*

> **NOTE —** `Run` iterates **all** computations (`[m+0x40]`) to find while-loops, whereas the canonicalize/remove passes walk only the **entry** computation (`[m+0x38]`). The asymmetry is correct: while-bodies are nested computations, while the module markers live in the entry computation.

---

## 4. CanonicalizeBoundaryMarker (#30) — Normalize to Tuple Form

### Purpose

Rewrite any marker whose result is consumed as N loose values into the single-tuple-operand form `marker(tuple(ops…))` + `get-tuple-element` projections, so that **after** this pass *every* marker has exactly one operand and that operand is a tuple. Removal (§5) and the partitioner depend on this invariant.

### Algorithm

```c
// CanonicalizeBoundaryMarker::Run(HloModule* m, threads)     // 0x1e8bfa0 (1849 B)
bool Run(m, threads):
    comp = m->entry_computation()                    // [m+0x38]; CHECK "nullptr != entry_computation_"
    // PASS 1 — collect markers whose operand(0) is NOT already a tuple
    to_fix = []
    for inst in comp->MakeInstructionPostOrder():
        if isMarker(inst)                            // 0x1ebdfa0
           and inst->operand(0)->shape().element_type() != TUPLE:   // cmpl $0xD — skip tuple ops
            to_fix.push(inst)
    // PASS 2 — rewrite each collected marker
    for m_inst in to_fix:
        if m_inst->operand_count() != 1:             // [m_inst+0x18] — the multi-operand case
            tuple = CreateTuple(m_inst->operands())                  // 0x9665520
            comp->AddInstruction(tuple, m_inst->metadata())          // 0x9637330 (with OpMetadata*)
            bc = CloneBackendConfigProto(m_inst->backend_config)     // Mutex-guarded, 0x96cc620
            new_marker = CreateCustomCall(shape       = tuple->shape(),
                                          operands    = Span{tuple},  // size 1
                                          target      = m_inst->custom_call_target(),  // verbatim
                                          opaque      = bc,           // BackendConfigWrapper 0x96cc880
                                          api_version = 1 /*ORIGINAL*/)
            comp->AddInstruction(new_marker, m_inst->metadata())
            if m_inst->operand_count() == 1:         // single-result marker → reproject via GTE
                gte = CreateGetTupleElement(new_marker, 0)           // 0x964cbf0
                comp->AddInstruction(gte, "")
                m_inst->ReplaceAllUsesWith(gte)      // CHECK "marker->ReplaceAllUsesWith(gte)"
            comp->RemoveInstruction(m_inst)          // CHECK "computation->RemoveInstruction(marker)"
    return changed
```

A marker that wrapped N loose operands `M(a,b,c)` becomes `M'(tuple(a,b,c))`, and its uses are re-expressed through `get-tuple-element`s. The **target name and backend-config are preserved** — the backend-config is deep-cloned under a Mutex via `CloneBackendConfigProto` and re-wrapped in a `BackendConfigWrapper`. Only `api_version` drops to `1` on the rebuilt custom-call.

*Anchors: the `CreateTuple`, `CreateCustomCall`, `CreateGetTupleElement`, and clone/wrapper callees, plus both CHECK strings; the `TUPLE` PrimitiveType code `0xD` is byte-compared at the shape's element-type word.*

Verbatim diagnostics: `"marker->ReplaceAllUsesWith(gte)"`, `"computation->RemoveInstruction(marker)"`, `"nullptr != entry_computation_"`.

> **QUIRK —** the backend-config clone is **Mutex-guarded** (`CloneBackendConfigProto` @0x96cc620 takes a lock). The marker's proto backend-config is a shared, lazily-parsed object inside the `HloInstruction`; deep-copying it onto the rebuilt instruction without the lock would race the lazy-parse path. A reimplementation that copies the opaque as a raw string sidesteps the lock but loses the structured proto that `getMarkerBackendConfig` later reads.

---

## 5. BoundaryMarkerRemoval (#31) — Strip Every Marker

### Purpose

Delete every marker (both families) and splice its wrapped value straight to the former consumers, so no marker survives into penguin lowering / BIR codegen.

### Algorithm

```c
// BoundaryMarkerRemoval::Run(HloModule* m, threads)          // 0x1e8b1a0 (2909 B)
bool Run(m, threads):
    comp = m->entry_computation()                    // [m+0x38]; CHECK "nullptr != entry_computation_"
    for inst in comp->MakeInstructionPostOrder():
        if !isMarker(inst): continue                 // 0x1ebdfa0
        op0 = inst->operand(0)
        if opcode(op0) != kTuple:                     // (op0+0x14) != 0x78
            // --- MODE A: simple passthrough (loop-body & non-tuple module markers) ---
            inst->ReplaceAllUsesWith(inst->mutable_operand(0))
            //          CHECK "marker->ReplaceAllUsesWith(marker->mutable_operand(0))"
            comp->RemoveInstruction(inst)             // CHECK "computation->RemoveInstruction(marker)"
        else:
            // --- MODE B: canonicalized tuple-operand marker → rewire each GTE user ---
            tuple = op0
            for user in inst->users():
                if opcode(user) != kGetTupleElement:  // (user+0x14) != 0x3A
                    record_error(NCC_ITUP001);        // " used by non-GTE instruction "
                    continue                          // -> "Use GetTupleElement to access tuple
                                                      //     components in boundary markers"
                idx = user->tuple_index()             // 0x9663790
                user->ReplaceAllUsesWith(tuple->mutable_operand(idx))
                //   CHECK "gte->ReplaceAllUsesWith(tuple->mutable_operand(index))"
                comp->RemoveInstruction(user)
            comp->RemoveInstructionAndUnusedOperands(inst)   // 0x963e430
    return changed
```

**Mode A** is the trivial 1-operand strip: the wrapped value flows directly to former consumers. **Mode B** unwraps a canonicalized `marker(tuple(…))` by replacing each `gte(marker, i)` with `tuple.operand(i)`, then drops the marker and its now-unused tuple. A marker output reaching a **non-GTE** user is the error `NCC_ITUP001` — i.e. after canonicalization, markers *must* be consumed via `get-tuple-element`.

*Anchors: `kTuple`=`0x78` and `kGetTupleElement`=`0x3A` byte-compared at `(op+0x14)`; all four CHECK strings, `NCC_ITUP001`, the diagnostic text `" used by non-GTE instruction "`, and the resolution `"Use GetTupleElement to access tuple components in boundary markers"`.*

> **GOTCHA —** loop-body markers are stripped in **at least three places**: this pass (#31) *and* the DUS/DS index simplifier (#80) *and* the slice-mover (#87), which inline-strip `NeuronBoundaryMarker-Start/-End` themselves (see [4.11](whileloop-unroll-tripcount.md)). The redundancy is defensive — multiple passes run after the unroller emits the markers, and any of them may need to see through a marker to its operand. The slice-mover's legality check explicitly whitelists `NeuronBoundaryMarker-End` as an allowed extra user of a dynamic-slice GTE (string: *"Dynamic slice input GTE has users that are not dynamic-slice, root tuple, or NeuronBoundaryMarker-End"*). A reimplementation that strips markers in only one pass will trip that check.

---

## 6. The Consumer — Layer-Cut Analysis in hlo2penguin

The module markers exist to be read by the **penguin partitioner** (`hlo2penguin`, namespace `xla::partition`, `MarkerSplitter`). The Start/End spans become **layer cut-points** that split one module into per-layer sub-modules for pipeline / sequential-layer execution, and the `-Forward`/`-Backward` direction separates the autograd forward graph from the backward graph.

### Entry Point

```text
MarkerSplitter::split(HloModule*)                         ── the partition driver
  ├─ preprocessBoundaryMarker(HloModule*)        @0x1f211c0
  │     └─ internal HloPassManager; on malformed markers -> NCC_MOD001
  │        ("Pipeline preprocess-boundary-marker failed to execute. Status: %s")
  ├─ MarkerAnalyzer::analyzeLayerBoundary()      @0x1f21c10   ── THE cut-point analyzer
  │     ├─ hlo_utils::isMarkerStart / isMarkerEnd / isMarker
  │     ├─ hlo_utils::getMarkerBackendConfig    (reads boundaryCount + direction)
  │     ├─ DEBUG "[analyzeLayerBoundary] cutMarkers size = "
  │     └─ DEBUG "[analyzeLayerBoundary] found cut point "
  └─ canonicalizeAndRemoveBoundaryMarker(HloModule*, bool)  @0x1f20660
        └─ removeBoundaryMarker(HloModule*)      @0x1f1fb70
           ("Writing out post_remove_markers module to ")
```

### How a span becomes a cut

`analyzeLayerBoundary` post-orders the entry computation and collects every marker into a `cutMarkers` set (logged at DEBUG via `"cutMarkers size = "`). Each emitted cut point (`"found cut point "`) corresponds to a Start/End span: the partitioner uses `getMarkerBackendConfig` → `boundaryCount` to pair an opening marker with its close, and the `-Forward`/`-Backward` target suffix to bucket the cut into the forward or backward sub-graph. The collected cuts are the **layer boundaries**: the partitioner slices the module at each, producing one sub-module per layer.

> **NOTE —** `hlo2penguin` runs its own canonicalize+remove (`canonicalizeAndRemoveBoundaryMarker` → `removeBoundaryMarker`) **after** the split, mirroring #30/#31 from `hlo-opt`. The order matters: the markers must survive the analysis (so spans are visible) but must not survive into codegen. In `hlo-opt`'s own registration order — #29 OptBarrierRemoval → **#30 canonicalize-boundary-marker → #31 boundary-marker-removal** → #32 trivial-cc-removal — the markers are normalized and immediately removed before the bulk of the optimizer, because `hlo-opt` is not the stage that splits on them.

### Why layer cuts matter

The whole subsystem exists to serve two transformations that need to know "where one layer ends and the next begins":

- **Gradient checkpointing (activation recomputation).** Forward markers delimit each layer's forward span. A checkpointing scheme keeps only the boundary activations and recomputes the interior of each span during the backward pass. The `-Forward`/`-Backward` direction tells the partitioner which span is the cheap-to-recompute forward and which is the gradient it pairs against. See the norm/checkpoint kernels in [6.7.5](../nki/normalization-kernels.md).
- **Pipeline-parallel partitioning.** The cut points are exactly the seams at which a model is sliced into pipeline stages, each stage assigned to a device. `boundaryCount` orders the stages; the Start/End pairs delimit each stage's instructions.

In both cases the marker is pure structural metadata — it carries no profiling payload beyond `boundaryCount=`. The `OpMetadata` cloned through canonicalize (and preserved onto the tuple/GTE) is the channel by which span provenance survives the rewrites.

---

## 7. Reconstructed Signatures

```cpp
// Producer (NOT a --passes pass; invoked from NeuronWhileLoopUnroller::Run @0x20019df)
StatusOr<bool> xla::hilo::NeuronAddBoundaryMarker::Run(HloModule*, const flat_hash_set<string_view>&);  // 0x2002370
void           xla::hilo::NeuronAddBoundaryMarker::TransformWhileLoop(HloInstruction* while_inst);       // 0x2001ff0
absl::string_view xla::hilo::NeuronAddBoundaryMarker::name() const;  // 0x2001e30 → "neuron_add_boundary_marker"

// #30 / #31 (registered passes)
StatusOr<bool> xla::CanonicalizeBoundaryMarker::Run(HloModule*, const flat_hash_set<string_view>&);      // 0x1e8bfa0
StatusOr<bool> xla::BoundaryMarkerRemoval::Run(HloModule*, const flat_hash_set<string_view>&);           // 0x1e8b1a0
absl::string_view xla::CanonicalizeBoundaryMarker::name() const;  // 0x1e8bd00 → "canonicalize-boundary-marker"
absl::string_view xla::BoundaryMarkerRemoval::name() const;       // 0x1e8a430 → "boundary-marker-removal"

// detectors / config (hlo-opt)
bool xla::hlo_utils::isMarker(const HloInstruction*);     // 0x1ebdfa0  (any of the 6 marker targets)
bool xla::hlo_utils::isMarkerEnd(const HloInstruction*);  // 0x1ebdf20  (any of the 3 End targets)
int64 xla::hilo::ExtractBoundaryCountFromBackendConfig(const std::string&);  // 0x1fcb170

// consumer detectors / config / driver (hlo2penguin only)
bool        xla::hlo_utils::isMarkerStart(const HloInstruction*);
std::string xla::hlo_utils::getMarkerBackendConfig(const HloInstruction*);
void        xla::partition::preprocessBoundaryMarker(HloModule*);              // 0x1f211c0
void        xla::partition::MarkerAnalyzer::analyzeLayerBoundary();            // 0x1f21c10
void        xla::partition::canonicalizeAndRemoveBoundaryMarker(HloModule*, bool);  // 0x1f20660
void        xla::partition::removeBoundaryMarker(HloModule*);                  // 0x1f1fb70
bool        xla::partition::MarkerSplitter::split(HloModule*);
```

Opcodes (byte `(inst+0x14)`): `kCustomCall`=`0x2B`, `kWhile`=`0x79`, `kTuple`=`0x78`, `kGetTupleElement`=`0x3A`. `TUPLE` PrimitiveType=`0xD`, byte-compared at the shape's element-type word.

---

## 8. Evidence summary

| Claim | Anchor |
|---|---|
| `NeuronAddBoundaryMarker` is not a registry pass; its only caller is `NeuronWhileLoopUnroller::Run` | function table holds `NeuronAddBoundaryMarker::Run` @0x2002370 and `::TransformWhileLoop` @0x2001ff0 but no `RegisterNeuronAddBoundaryMarker` factory; the only `Register…` factories in this area are `RegisterCanonicalizeBoundaryMarker` and `RegisterBoundaryMarkerRemoval`, each with its own `_M_invoke`/`_M_manager` lambdas |
| Exactly six marker target names, and `isMarker` is their union | all six strings present; `isMarker` @0x1ebdfa0 and `isMarkerEnd` @0x1ebdf20 are the only detector symbols in `hlo-opt` |
| Removal Mode B errors on a non-GTE user with `NCC_ITUP001` | `NCC_ITUP001`, `" used by non-GTE instruction "`, and `"Use GetTupleElement to access tuple components in boundary markers"` all present, alongside the four `ReplaceAllUsesWith` / `RemoveInstruction` CHECK strings |
| The penguin partitioner derives layer cut-points in `MarkerAnalyzer::analyzeLayerBoundary` | `MarkerSplitter::split`, `MarkerAnalyzer::analyzeLayerBoundary`, `preprocessBoundaryMarker`, `canonicalizeAndRemoveBoundaryMarker`, `removeBoundaryMarker`, `isMarkerStart`, `getMarkerBackendConfig` all in the `hlo2penguin` function table; DEBUG strings `"[analyzeLayerBoundary] cutMarkers size = "` and `"[analyzeLayerBoundary] found cut point "` in its string pool |
| `CustomCallApiVersion` enum values | `API_VERSION_ORIGINAL` = 1 and `API_VERSION_STATUS_RETURNING` = 2 present verbatim in `.rodata` |

## 9. Limits of this reading

- The emit→2 / canonicalize→1 `api_version` split is read from the `CreateCustomCall` call-site register setup rather than from a re-disassembly of both call sites end to end. The enum values themselves are directly observed; the assignment of each to a call site is one step removed.
- The upstream producer of the four `AwsNeuronModuleMarker*` custom-calls is not identified — neither binary contains a creation site, so they must arrive in the input HLO.
- The module-marker backend-config schema beyond `boundaryCount` is unknown; the content is produced upstream and only read here via `getMarkerBackendConfig`.
- The precise Start↔End nesting and pairing rule inside `analyzeLayerBoundary` is sketched from callee and string evidence rather than traced; see Marker Splitter & Penguin Partition for the deeper treatment.

---

## Related Components

| Name | Relationship |
|---|---|
| `NeuronWhileLoopUnroller` (#112) | Sole caller of `NeuronAddBoundaryMarker`; emits loop-body markers as an unroll side-effect |
| DUS/DS simplifier (#80), slice-mover (#87) | Inline-strip `NeuronBoundaryMarker-*`; whitelist `-End` as a legal extra GTE user |
| OptBarrierRemoval (#29) / trivial-cc-removal (#32) | Pipeline neighbours of #30/#31 in the `hlo-opt` registration order |
| `MarkerSplitter` / `MarkerAnalyzer` (hlo2penguin) | Principal consumer; turns Start/End spans into layer cut-points |

## Cross-References

- [While-Loop Unroll & All-Gather Trip-Count Rewrite](whileloop-unroll-tripcount.md) — 4.11; the while-loop unroller that produces loop-body markers, and the DUS/DS passes that inline-strip them
- [Pass Registry](pass-registry.md) — the `--passes` table where #30 `canonicalize-boundary-marker` and #31 `boundary-marker-removal` are registered
- Marker Splitter & Penguin Partition — Part 5; the layer-cut algorithm in `hlo2penguin` that consumes the module markers
- [Norm & Checkpoint Kernels](../nki/normalization-kernels.md) — 6.7.5; gradient-checkpointing kernels whose recompute spans are delimited by the forward/backward markers
