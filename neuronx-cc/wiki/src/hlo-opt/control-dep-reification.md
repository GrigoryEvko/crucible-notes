# Control-Dependency Reification

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The HLO-side pass lives in `neuronxcc/starfish/bin/hlo-opt`; the MLIR simplifier and Penguin emitter live in `neuronxcc/starfish/bin/hlo2penguin`. Treat every address as version-pinned.*

## Abstract

A *control dependency* in XLA HLO is an ordering edge with no data flow: `B` must be scheduled after `A` even though `B` never reads `A`'s value. In `HloInstruction` these edges live in a side table (`control_predecessors()` / `control_successors()`), **not** in the operand list. That is fatal for the Neuron backend, because the lowering path HLO → MHLO/StableHLO → Penguin Python IR → BIR crosses MLIR, and MLIR tracks only SSA value operands. Export to MHLO, an upstream canonicalization, and re-import would silently drop every side-table control edge — and with it, every ordering guarantee the frontend established.

`PreserveControlDeps` (pass `#61`, registered name `preserve-control-deps`, `Run` @ `0x1f5c110`) solves this by **reifying** each control edge into a real SSA operand. For a successor `S` it builds a side-effecting `AwsNeuronControlDep` custom-call that takes the control-predecessors as leading operands and `S`'s original `operand(0)` as the trailing operand, then rewires `S.operand(0)` to consume that call. The edge is now part of the data-dependence graph, so it survives MHLO import, MLIR canonicalization, fusion, and scheduling — exactly the passes that would otherwise lose it. The custom-call's side-effect bit (`[cc+0x2B8]=1`) keeps DCE and the scheduler from deleting or freely reordering it.

This page traces the full seam. Section 2 is the on-wire op and its operand convention. Section 3 is the three-phase `Run` algorithm on the HLO side. Section 4 is the MHLO/StableHLO handoff: `NeuronControlDepTupleSimplifier` (`flatten-control-dep-tuple-operands`, `runOnOperation` @ `0x20f6a40`) un-bundles the `mhlo.tuple` the importer wraps the predecessor operands in, and `MhloToPythonPrinter::printControlDeps` (@ `0x20b8480`) finally emits a Penguin `.add_dep_edge(` call. The recurring trap — answered up front in [§2.3](#23-the-tuple-is-an-importer-artifact-not-an-hlo-construct) — is that the HLO pass emits **flat variadic** operands; the "tuple" in the simplifier's name is an MHLO-import artifact, not something the HLO pass produces.

For reimplementation, the contract is:

- The wire op: `AwsNeuronControlDep` custom-call, empty opaque, `api_version=1`, side-effect flagged, result shape = forwarded operand shape.
- The operand convention: `(p0, …, p_{k-1}, D)` — control-predecessors first, the data value last; `S.operand(0)` rewired to the call.
- The filtering and module-stamp rules: drop `kParameter` predecessors; stamp module frontend attribute `has_control_deps="1"` iff ≥1 edge was reified.
- The MLIR read-back: recognize the call by `call_target_name == "AwsNeuronControlDep"`, flatten any `mhlo.tuple` / `mhlo.get_tuple_element` wrapper, rebuild the call, and emit a Penguin dependency edge.

| | |
|---|---|
| **HLO pass body** | `xla::hilo::PreserveControlDeps::Run` @ `0x1f5c110` (HloModulePass) |
| **Pass name** | `preserve-control-deps` (`name()` @ `0x1f5b0d0`); registry slot `#61` |
| **Factory** | `RegisterPreserveControlDeps()::lambda` `_M_invoke` @ `0x1e70910` |
| **Wire op** | `mhlo.custom_call` target `AwsNeuronControlDep` (len `0x13`=19), side-effecting |
| **MLIR simplifier** | `hilo::NeuronControlDepTupleSimplifier::runOnOperation` @ `0x20f6a40` (arg `flatten-control-dep-tuple-operands`) |
| **Recognizer** | `hilo::isControlDep(Operation*)` @ `0x21c0370` (`call_target_name == "AwsNeuronControlDep"`) |
| **Penguin emitter** | `mlir::MhloToPythonPrinter::printControlDeps` @ `0x20b8480` → `.add_dep_edge(` |
| **IR levels** | XLA HLO → MHLO/StableHLO → Penguin Python IR (`neuronxcc.starfish.penguin.ir.Dependency`) |

---

## The Wire Op and Operand Convention

### The on-wire op: `AwsNeuronControlDep`

The reified edge is an XLA custom-call whose target name is the literal `AwsNeuronControlDep` (hlo-opt rodata @ `0x26e82c`, length 19; hlo2penguin @ `0x272822`). The name is loaded into `HloInstruction::CreateCustomCall` with an explicit length of `0x13`=19 — disasm `0x1f5c5ec: mov r8d, 13h` immediately before the call site at `0x1f5c602`.

| Field | Value | Evidence | Confidence |
|---|---|---|---|
| `custom_call_target` | `"AwsNeuronControlDep"` | str `0x26e82c`; `mov r8d, 13h` @ `0x1f5c5ec` | CERTAIN |
| `opaque` / `backend_config` | `""` (empty) | empty-string arg into `CreateCustomCall`; edge is structural | HIGH |
| `api_version` | `1` | `push 1` @ `0x1f5c5e6` (last `CreateCustomCall` arg) | HIGH |
| side-effect flag | `1` | `mov byte ptr [rax+2B8h], 1` @ `0x1f5c68a` after `Cast<HloCustomCallInstruction>` | CERTAIN (offset); MEDIUM (flag name) |
| result shape | `operand(0).shape()` | `shape()` feeds the `CreateCustomCall` shape arg | HIGH |

The instruction is **transparent**: it returns the same shape it forwards (the data value's shape), so substituting it for `S.operand(0)` is type-preserving. The side-effect bit at `HloCustomCallInstruction+0x2B8` is the mechanism that makes the reification stick — see [§5](#scheduling-and-ordering-interaction).

> **GOTCHA — the side-effect flag is the whole point, and it is set inline.** There is no named setter symbol; the body `Cast<HloCustomCallInstruction>`s the freshly-built call (@ `0x1f5c685`) and writes the boolean directly: `0x1f5c68a: mov byte ptr [rax+2B8h], 1`. A reimplementation that builds an *un-flagged* `AwsNeuronControlDep` call will see HloDCE delete it (no users care about an effect-free, single-use forwarder) and lose every control edge it was meant to pin. The offset `+0x2B8` is read from the disassembly; the field *name* `custom_call_has_side_effect_` is **[INFERRED]** from the single inline boolean write.

### Operand convention

For a successor `S` whose filtered control-predecessors are `P = {p0, …, p_{k-1}}` ([§3.1](#phase-1--build-the-predecessor-map)), the pass builds a **flat variadic** operand list with the predecessors first and the data value last:

```text
AwsNeuronControlDep( p0, p1, …, p_{k-1}, S.operand(0) )
                     └────── k preds ──────┘  └─ data, index k (LAST) ─┘
```

Disasm evidence:

- The `k`-predecessor pointers are `memcpy`'d into the operand `SmallVector` when the predecessor count `ebx != 0`; the count dword is written from `ebx`. The `>6` path widens the inline storage via `SmallVectorBase::grow_pod`.
- The data value is appended last: `mutable_operand(0)` is read (@ `0x1f5c569`) and pushed at index `[size]`, giving total size `k+1` (`lea r12d,[rax+1]` @ `0x1f5c5b7`). So the data operand sits at index `k`.
- `CreateCustomCall(shape = op0.shape, operands = span{…, count = k+1}, "AwsNeuronControlDep", "", 1)` @ `0x1f5c602`.
- The call is added to the *same* computation with an empty name, then `S.ReplaceOperandWith(0, cc)` @ `0x1f5c6a0` rewires the successor. The `TF_CHECK_OK` guard carries the literal `successor->ReplaceOperandWith(0, controlDepCall)` (str @ `0x393bd0`, referenced @ `0x1f5c6af`).

Net structural transform per successor group:

```text
BEFORE:  S.operand(0) = D ;  control edge p_i → S         (side table, invisible to MLIR)
AFTER:   cc = AwsNeuronControlDep[side_effect=1](p0, …, p_{k-1}, D)
         S.operand(0) = cc                                 (edge now an SSA operand chain)
```

### The "tuple" is an importer artifact, not an HLO construct

The HLO pass emits a flat `Span<HloInstruction* const>`; there is **no** `CreateTuple` call anywhere in `Run` — the only constructor on the path is `CreateCustomCall`. The word *tuple* in the downstream pass name `flatten-control-dep-tuple-operands` refers to the `mhlo.tuple` that the **HLO→MHLO importer** wraps the variadic predecessor bundle into ([§4](#hlomlir-handoff--neuroncontroldeptuplesimplifier)). The simplifier flattens *that*, not anything this pass built.

> **QUIRK —** a reader who sees `flatten-control-dep-tuple-operands` and assumes `PreserveControlDeps` emits a `kTuple` operand will look for a `CreateTuple` that does not exist and mis-model the HLO operand list. The HLO list is flat; the tuple appears only after MHLO import, and only sometimes (the importer's tuple-wrapping is not statically provable to fire for every predecessor count). The simplifier handles both the tuple-wrapped and the already-flat shapes defensively.

---

## Preservation Algorithm — `PreserveControlDeps::Run`

`Run` (@ `0x1f5c110`, 7290 bytes, 282 basic blocks, 36 callees) is a stateless `HloModulePass`: the factory `new(8)`s an object holding only a vtable pointer (`off_410110`), with no tunable flags. It operates over **all** computations of the module — not just the entry computation. The body is three phases; each is anchored by a verbatim `NeuronLogger` DEBUG string tagged to the source file `hilo/hlo_passes/PreserveControlDeps.cc` (@ `0x2b8088`).

> **NOTE —** `Run` was extracted without Hex-Rays decompilation; the phases below are reconstructed from disassembly plus the verbatim rodata strings and callee sidecars. Operand order and the side-effect flag are read directly off instructions; the semantic of the phase-1 skip flag is **[INFERRED]**, as noted in place.

### Phase 1 — build the predecessor map

```c
// loop @0x1f5c168..0x1f5c33f
function BuildPredMap(module):                          // [module@rdx] computations at +0x40..+0x48
    predMap = MapVector<HloInstruction*, SmallVector<HloInstruction*,6>>()   // operator[] @0x1f5b6d0
    for comp in module.computations():                  // stride 0x10
        for inst in comp.instructions():                // node->inst at +8
            if inst.flags[0x15] & 0x8:  continue        // test byte ptr[rdx+15h],8 @0x1f5c1df — skip (MED)
            preds = inst.rare[0x30].control_predecessors()   // kEmptyRare sentinel if null
            filtered = []
            for p in preds:
                if p.opcode_byte[0x14] == 0x4C:  continue    // cmp byte[r14+14h],4Ch @0x1f5c284 — kParameter
                filtered.push_back(p)
            predMap[inst] = filtered                    // insertion-ordered → deterministic worklist
```

- The opcode lives at `HloInstruction+0x14` (low byte); `0x4C` is `kParameter`. The compare is `cmp byte ptr [r14+14h], 4Ch ; 'L'` @ `0x1f5c284`. **Parameter predecessors are never reified** — a control edge from a graph input is meaningless, since inputs are available before any compute begins ([§5](#scheduling-and-ordering-interaction)).
- `[inst+0x15] & 8` is a fast-path skip (`test byte ptr [rdx+15h], 8` @ `0x1f5c1df`). It gates whether an instruction is considered at all; the precise meaning is **[INFERRED]** — most consistent with "carries no rare control metadata" or a dead/removed marker.
- The container is `llvm::MapVector` (operator[] @ `0x1f5b6d0`, 1136 bytes), which is **insertion-ordered**. That makes the phase-2 worklist deterministic in computation/instruction order — important for reproducible builds.

### Phase 2 — reify each map entry into a custom-call

```c
// worklist loop @0x1f5c4e9..0x1f5c6c0
function ReifyEdges(predMap, comp):
    DEBUG("Worklist-size = " << predMap.size() << "]")          // str @0x27e813
    for (S, preds) in predMap:                                  // var_2A8 base, var_2A0 count, stride 0x48
        DEBUG("  Handling Pair:\n[\n  Successor [" S.ToString() ...   // strs @0x23bfde / @0x253051
              "  Predecessors: [" preds... "]")                       // str @0x25305d
        operands = []
        if preds.size() != 0:                                   // loc_1F5D7B0
            operands = memcpy(preds)                            // k entries; grow_pod if >6
        operands.push_back(S.mutable_operand(0))               // data value LAST (index k)
        cc = HloInstruction::CreateCustomCall(                 // @0x1f5c602
                 shape    = S.operand(0).shape(),
                 operands = operands,                          // {p0..p_{k-1}, D}
                 target   = "AwsNeuronControlDep",             // r8d=13h @0x1f5c5ec
                 opaque   = "",
                 api_ver  = 1)                                 // push 1 @0x1f5c5e6
        cc = comp.AddInstruction(cc, /*name*/"")
        Cast<HloCustomCallInstruction>(cc)                     // @0x1f5c685
            ->custom_call_has_side_effect_ = 1                 // mov byte[rax+2B8h],1 @0x1f5c68a
        TF_CHECK_OK( S.ReplaceOperandWith(0, cc) )             // @0x1f5c6a0; check str @0x393bd0
        DEBUG("ControlDep custom-call [" cc "]")               // str @0x256f7c
```

The DEBUG block is heavily duplicated in the disassembly — one `getInstance` / `shouldLogToFile` / `shouldLogToConsole` `NeuronLogger` triplet per `<<` token, all gated, all source-tagged `hilo/hlo_passes/PreserveControlDeps.cc` with `setSourceLine(0x31)`=49 at the post-rewire log site. The severity is DEBUG (`setCurLogLevel(1)`). None of this fires in a non-verbose build, but the strings are the firmest anchors for the phase boundaries.

### Phase 3 — stamp the module `has_control_deps` attribute

```c
// @0x1f5daac..0x1f5dce0
function StampModule(module, anyReified):
    if not anyReified:  return false                       // fast exit @0x1f5dce5, result byte = 0
    FrontendAttributes fa                                  // ctor @0x1f5dac1
    fa.mutable_map()["has_control_deps"] = "1"             // key = 16-byte xmmword @0x406140 (movdqa @0x1f5db15); value 0x31
                                                           //   key NAME [INFERRED]; the load and the value are read from disasm
    module.frontend_attributes()[0xBA0].MergeFrom(fa)      // CopyFrom @0x1f5dcc3 / InternalSwap @0x1f5dcff
    return true                                            // changed
```

After all computations are processed, if **any** edge was reified the pass records a module-level frontend attribute whose 16-byte key is loaded from the rodata constant @ `0x406140` (`movdqa xmm0, cs:xmmword_406140` @ `0x1f5db15`, size `0x10`); the value is the single character `"1"` (`0x31`, length 1). It is merged into `HloModule` frontend attributes at offset `+0xBA0` via a tagged-pointer `CopyFrom`-vs-`InternalSwap` fast path.

> **GOTCHA — the attribute key is not in the string table.** Because it is exactly 16 bytes, the key is embedded as an `xmmword` constant and loaded with a single `movdqa` @ `0x1f5db15`, so a `strings`-style extractor never surfaces it as a standalone entry. The load and the merge into module frontend attributes at `+0xBA0` are read from the disassembly; the literal text `"has_control_deps"` is **[INFERRED]** — the 16-byte length matches the `xmmword`, and it is the attribute key downstream fusion gates read. Which passes read it is [UNRESOLVED] — see [§5](#scheduling-and-ordering-interaction).

The bool `Run` returns is `changed = (≥1 cc inserted)`. An empty module, or one with no non-parameter control edges, takes the fast exit at `0x1f5dce5` and returns `false` ("no change").

---

## HLO→MLIR Handoff — `NeuronControlDepTupleSimplifier`

Once HLO is exported and re-imported as MHLO (or StableHLO), the variadic `AwsNeuronControlDep` operand list may be wrapped: the importer can surface the multi-operand predecessor bundle as a single `mhlo.tuple` operand, or as chains of `mhlo.get_tuple_element`. `NeuronControlDepTupleSimplifier` (arg `flatten-control-dep-tuple-operands` @ `0x348e50`) undoes that, restoring a flat operand list before the Penguin printer reads it.

The pass exists in two byte-twin forms differing only in `mhlo::` ↔ `stablehlo::` op TypeIDs:

| Role | MHLO | StableHLO |
|---|---|---|
| `runOnOperation` | `0x20f6a40` | `0x2132980` |
| `replaceTuples` | `0x20f5d40` | `0x2131c80` |
| `getArgument` | `0x20f5620` (`flatten-control-dep-tuple-operands`, str `0x348e50`) | `0x2131560` (`stablehlo-flatten-control-dep-tuple-operands`, str `0x348fa8`) |
| walk callback lambda | `0x20f59c0` | `0x2131900` |
| factory | `mlir::createNeuronControlDepTupleSimplifierPass` @ `0x20f68a0` | `mlir::createStableHLONeuronControlDepTupleSimplifierPass` @ `0x21327e0` |
| Penguin printer | `MhloToPythonPrinter::printControlDeps` @ `0x20b8480` | `StableHLOToPythonPrinter::printControlDeps` @ `0x2153360` |

All addresses resolve in `hlo2penguin`'s `function_addresses.json` (mangled symbols `_ZN4hilo31NeuronControlDepTupleSimplifier…` and the `StableHLO`-prefixed twin).

### `runOnOperation` — collect the control-dep calls

```c
function NeuronControlDepTupleSimplifier::runOnOperation():     // @0x20f6a40
    func = hilo::getMainFunction(module)                        // @0x21c1e70
    hits = SmallVector<Operation*>()
    func.walk<mhlo::CustomCallOp>(op):                          // forward walk; lambda @0x20f59c0
        if hilo::isControlDep(op):  hits.push_back(op)          // @0x21c0370
    replaceTuples(hits)                                         // @0x20f5d40
```

`hilo::isControlDep(Operation*)` (@ `0x21c0370`, 399 bytes) is the **shared** control-dep recognizer: it returns true iff the op's `call_target_name` attribute equals `"AwsNeuronControlDep"` (str @ `0x425360`). It is the single source of truth for "is this a control edge" — called by both tuple-simplifier walk lambdas and by the Penguin printers (`printControlDeps`, `printControlDepCustomCall`, and the operand/source printers). A reimplementation must use the exact target-name match; nothing else distinguishes the op.

### `replaceTuples` — the actual flattening

```c
function NeuronControlDepTupleSimplifier::replaceTuples(hits):  // @0x20f5d40
    for op in hits:                                             // each AwsNeuronControlDep mhlo::CustomCallOp
        flat = []
        for operand in op.operands():
            def = operand.getDefiningOp()                       // @0x20f5e71 / 0x20f6317 / 0x20f635d
            if isa<mhlo::TupleOp>(def):                         // TypeID cmp @0x20f5e96 / 0x20f639d
                for elem in def.operands():  flat.push_back(elem)   // inline the tuple's predecessors
            elif isa<mhlo::GetTupleElementOp>(def):             // TypeID cmp @0x20f633a
                flat.push_back(unwrap GTE source)               // OpResultImpl::getNextResultAtOffset @0x20f623a
            else:
                flat.push_back(operand)                         // already flat
        newOp = mhlo::CustomCallOp::build(                      // @0x20f60bf → builder @0x8fa9c60
                    builder, state,
                    TypeRange(op.result_types),
                    ValueRange(flat),                           // FLATTENED operands
                    { "call_target_name" = "AwsNeuronControlDep" })   // NamedAttr; key str @0x25a1fb
        op.replaceAllUsesWith(newOp)
        op.erase()                                              // Operation::erase @0x20f6179
```

The rebuild uses `mhlo::CustomCallOp::build(OpBuilder&, OperationState&, TypeRange, ValueRange, ArrayRef<NamedAttribute>)` (@ `0x8fa9c60`), with `Builder::getNamedAttr` (@ `0x9ae5590`), `getStringAttr` (@ `0x9ae6060`), and `RegisteredOperationName::lookup` resolving the `mhlo.custom_call` TypeID (op name str @ `0x232721`, "Building op `" diag @ `0x286e24`). The net MHLO transform:

```text
mhlo.custom_call{AwsNeuronControlDep}( mhlo.tuple(p0,…,p_{k-1}), D )
  →  mhlo.custom_call{AwsNeuronControlDep}( p0, …, p_{k-1}, D )
```

restoring exactly the flat `(preds…, data)` shape the HLO pass authored.

### Terminal consumer — the Penguin emitter

The flattened `AwsNeuronControlDep` calls are **not** lowered to a Penguin op. Instead `MhloToPythonPrinter::printControlDeps` (@ `0x20b8480`; StableHLO twin @ `0x2153360`) recognizes them via `isControlDep` and prints Python dependency edges into `neuronxcc.starfish.penguin` — emission string `.add_dep_edge(` (str @ `0x262878`), constructing `neuronxcc.starfish.penguin.ir.Dependency` edges. The round-trip in full:

```text
HLO side-table edge  p_i → S
   │  PreserveControlDeps (#61)
   ▼
AwsNeuronControlDep[side_effect=1]( p0,…,p_{k-1}, D )     (flat SSA operands)
   │  HLO → MHLO import  (predecessors may be tuple-wrapped)
   ▼
mhlo.custom_call{AwsNeuronControlDep}( mhlo.tuple(p…), D )
   │  NeuronControlDepTupleSimplifier  (flatten-control-dep-tuple-operands)
   ▼
mhlo.custom_call{AwsNeuronControlDep}( p0,…,p_{k-1}, D )
   │  MhloToPythonPrinter::printControlDeps
   ▼
penguin.ir.Dependency : successor.add_dep_edge(predecessor)
   │  Penguin scheduling
   ▼
BIR ordering constraint
```

The Penguin emission step is anchored to the `.add_dep_edge(` string and the `printControlDeps` symbol; the precise Python argument order is **[UNRESOLVED]** — it was not traced.

---

## Scheduling and Ordering Interaction

Why reification preserves order, mechanism by mechanism:

- **Side-effect bit is the anti-DCE pin.** `[cc+0x2B8]=1` ([§2.1](#the-on-wire-op-awsneuroncontroldep)) marks the call side-effecting, so HloDCE will not delete it (even though it is a single-use, effect-free-looking forwarder) and layout/schedule passes treat it as a node whose operands must stay live and whose position must be honored.
- **Edges become data edges.** Because the predecessors are real operands of a node feeding `S.operand(0)`, the former control edges now live *inside* the data-dependence graph. Any topological scheduler — HLO, MLIR, or Penguin — honors them automatically; no separate control-edge tracking is needed downstream. This is the whole reason for the reification: it converts an MLIR-invisible side table into an MLIR-native operand chain.
- **Parameter edges are pruned.** A control edge `p → S` where `p` is a graph input is dropped in phase 1 ([§3.1](#phase-1--build-the-predecessor-map)); inputs are available before any compute, so no ordering pin is needed and reifying one would create a spurious dependency on a parameter.
- **Module attribute is a cheap signal.** `has_control_deps="1"` ([§3.3](#phase-3--stamp-the-module-has_control_deps-attribute)) lets later stages detect "this module reified control edges" without re-scanning instructions. The blob @ `0x406140` is rodata-adjacent to a `"Don't fuse instr…"` string, suggesting a fusion gate reads it, but the read site is **[UNRESOLVED]** — it was not traced.
- **Placement is deliberate.** `#61` sits immediately before `NeuronHloInstCombine` (`#62`) and the fusion cluster, just after `#60 UpcastAllToFP`. Pinning the edges into operands *before* any peephole/fusion pass runs is what prevents those passes from dropping or reordering the edges. (Registry order from the `--passes` table — see [Pass Registry](pass-registry.md).)

---

## Reconstructed Structures and Signatures

```cpp
// ── HLO side (hlo-opt) — stateless HloModulePass, no flags ─────────────────
class xla::hilo::PreserveControlDeps : public xla::HloModulePass {   // vtable 0x410100 (slots at +0x10)
  absl::string_view name() const { return {"preserve-control-deps", 21}; }   // 0x1f5b0d0
  StatusOr<bool> Run(HloModule*,
                     const absl::flat_hash_set<std::string_view>& exec_threads);   // 0x1f5c110
};
// factory: RegisterPreserveControlDeps()::lambda → operator new(8){ vptr = off_410110 }   // 0x1e70910

// HloInstruction field offsets used (read from disasm unless noted):
//   +0x14  uint16 opcode (low byte);  0x4C = kParameter
//   +0x15  flags byte (bit 0x8 = phase-1 skip; semantic [INFERRED])
//   +0x30  rare-metadata ptr → control_predecessors() vector  (kEmptyRare sentinel if null)
// HloCustomCallInstruction:
//   +0x2B8 byte custom_call_has_side_effect_  (set 1 on the reified op)   // offset read; name [INFERRED]
// HloModule:
//   +0xBA0 FrontendAttributes  (recipient of has_control_deps="1")

xla::HloInstruction::CreateCustomCall(const Shape&, absl::Span<HloInstruction* const>,
        std::string_view target, std::string opaque, CustomCallApiVersion);   // called @0x1f5c602

// ── MLIR side (hlo2penguin) ────────────────────────────────────────────────
class hilo::NeuronControlDepTupleSimplifier
    : mlir::PassWrapper<…, mlir::OperationPass<mlir::ModuleOp>> {
  StringRef getArgument() { return "flatten-control-dep-tuple-operands"; }   // 0x20f5620
  void runOnOperation();                                                     // 0x20f6a40
  void replaceTuples(llvm::SmallVectorImpl<mlir::Operation*>&);              // 0x20f5d40
};
bool hilo::isControlDep(mlir::Operation*);   // call_target_name == "AwsNeuronControlDep"   // 0x21c0370
mhlo::CustomCallOp::build(OpBuilder&, OperationState&, TypeRange, ValueRange,
        ArrayRef<NamedAttribute>{ "call_target_name" = "AwsNeuronControlDep" });   // 0x8fa9c60
```

---

## Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `xla::hilo::PreserveControlDeps::Run` | `0x1f5c110` | HLO pass body — reify control edges | CERTAIN |
| `…::Run` `.cold` clone | `0x1f5bf62` | exception/unwind tail | CERTAIN |
| `xla::hilo::PreserveControlDeps::name` | `0x1f5b0d0` | returns `"preserve-control-deps"` (len 0x15) | CERTAIN |
| `RegisterPreserveControlDeps()::lambda` `_M_invoke` | `0x1e70910` | factory; `new(8)`, vptr `off_410110` | CERTAIN |
| `MapVector<…>::operator[]` | `0x1f5b6d0` | per-instruction predecessor bucket | CERTAIN |
| `hilo::NeuronControlDepTupleSimplifier::runOnOperation` | `0x20f6a40` | walk + collect control-dep calls | CERTAIN |
| `…::replaceTuples` | `0x20f5d40` | flatten `mhlo.tuple`/GTE; rebuild; erase | CERTAIN |
| walk callback lambda | `0x20f59c0` | `isControlDep` filter into `SmallVector` | CERTAIN |
| `…::getArgument` | `0x20f5620` | `"flatten-control-dep-tuple-operands"` | CERTAIN |
| `mlir::createNeuronControlDepTupleSimplifierPass` | `0x20f68a0` | pass factory | CERTAIN |
| `hilo::isControlDep` | `0x21c0370` | `call_target_name == "AwsNeuronControlDep"` | CERTAIN |
| StableHLO twin `runOnOperation` / `replaceTuples` / `getArgument` | `0x2132980` / `0x2131c80` / `0x2131560` | `stablehlo-` arg twins | CERTAIN |
| `mlir::MhloToPythonPrinter::printControlDeps` | `0x20b8480` | emit Penguin `.add_dep_edge(` | HIGH |
| `mlir::StableHLOToPythonPrinter::printControlDeps` | `0x2153360` | StableHLO emitter twin | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `NeuronHloInstCombine` (#62) | runs immediately after #61; the reification pins edges before its peephole rewrites |
| `UpcastAllToFP` (#60) | the prior registry slot |
| MHLO/StableHLO importer | wraps the variadic predecessor bundle in `mhlo.tuple` that the simplifier then flattens |
| `MhloToPythonPrinter` / `StableHLOToPythonPrinter` | terminal consumers; turn the flattened call into `penguin.ir.Dependency` edges |

---

## Cross-References

- [Control-Dep Tuple-Flatten (MLIR)](controldep-tuple-flatten-mlir.md) — 4.38; the MHLO/StableHLO `flatten-control-dep-tuple-operands` simplifier in depth
- [Pass Registry (the `--passes` Table)](pass-registry.md) — where `preserve-control-deps` sits at slot #61
- [HLO/mhlo/stablehlo Ingestion Boundary](hlo-ingestion-boundary.md) — the `xla::hilo::` (HLO) vs lowercase `hilo::` (MLIR) split that puts these passes in different binaries
- [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) — the sibling `AwsNeuron*` custom-call family this op belongs to
- [Penguin Dependency Model — DependencyEdge & EdgeKind](../penguin/dependency-model.md) — Part 5; the `DependencyEdge`/`EdgeKind` model behind `neuronxcc.starfish.penguin.ir.Dependency` and the `.add_dep_edge(` calls the printer emits on the Penguin side
- [BIR EdgeKind](../bir/edge-kinds.md) — Part 7; the BIR-level ordering edge the reified control dependency ultimately becomes
