# HLO Misc & Cleanup Sweep

> *All addresses on this page apply to neuronx_cc `2.24.5133.0+58f8de22` (cp310, `neuronxcc/starfish/bin/hlo-opt`). Other versions will differ.*

## Abstract

After the heavyweight collective, layout, and fusion passes have run, the Neuron HLO pipeline closes with a long tail of ~28 small passes — the plumbing tier. None of them is individually large enough to earn its own page, but collectively they do the unglamorous work that makes the IR backend-legal: they reshape the entry tuple, fold weight parameters and RNG to constants, peel optimization barriers, strip trivial single-device collectives, lower a handful of unsupported idioms (variadic argmax-reduce, offloaded dropout, scatter-partition), legalize device assignment on every collective, and flatten predicated conditionals to `select`. This page is the consolidated reference for that closer.

Two registration kinds dominate. Most passes are plain `xla::HloPass`-derived, whose virtual `Run(HloModule*, …)` (vptr+0x18) is the real entry. A second group are `xla::OpExpanderPass` subclasses: they share one `Run` at `0x29f0bb0` and supply two per-pass virtuals — `InstructionMatchesPattern` (vptr+0x28, the matcher) and `ExpandInstruction` (the rewriter). For an OpExpander pass the table below cites the matcher address with the `ExpandInstruction` body in parentheses, since the matcher is what the registry row points at but the `Expand` body is where the algorithm lives.

The pass-order numbers (`#NN`) are the registration index in `RegisterHiloHloPasses` and serve as a strong proxy for default-pipeline order: output/IO shaping runs early (#14–#16, #54, #59), algebraic peepholes and canonicalizers in the middle (#17, #24, #66, #67, #90, #109, #110, #55), and metadata/legalization/debug late (#20–#22, #52, #69, #70). Every Run/matcher address on this page was re-pulled from the per-function disassembly and matched its demangled symbol exactly — all CONFIRMED. Confidence labels below reflect *rewrite-logic* certainty (callee + string evidence strength), not address certainty.

For reimplementation, the contract is:

- The OpExpander dispatch split: one shared `Run`, per-pass matcher + `ExpandInstruction`. Drive the pass off the matcher predicate, not the opcode alone.
- The five passes with real algorithms — `InlineWeights`, `ReplaceRng`, `DeviceAssignmentLegalization`, `DynamicSliceTranspose`, `RewriteScatterPartition`, plus `HiloConditionalToSelect` — given as annotated pseudocode below.
- The honest gaps: `PartitionEmbedingOps` sharding mechanics and `RewriteModuleDtype`'s from/to dtype pair are not string-anchored.

| | |
|---|---|
| **Binary** | `hlo-opt` (neuronx_cc 2.24.5133.0+58f8de22, cp310) |
| **Pass count** | ~28 (the cleanup/metadata/legalize tail of 112 registered HLO passes) |
| **OpExpander shared Run** | `0x29f0bb0` (`xla::OpExpanderPass::Run`) |
| **Order source** | registration index in `RegisterHiloHloPasses` |
| **Most substantive pass** | `InlineWeights::Run` `0x1ee8c60` (10176 B, 494 bb) |
| **Method** | static disassembly; demangled-symbol + callee + string verification (no Hex-Rays on hlo-opt) |

---

## 1. The 28-pass table

`Run / matcher addr` is the registry entry point. For OpExpander rows the `ExpandInstruction` body addr follows in parentheses. All class identities are symbol-CONFIRMED; `Conf` is rewrite-logic confidence.

| Pass (class) | # | Run / matcher addr | Match condition | Rewrite | Conf |
|---|---|---|---|---|---|
| `RemovePassthroughOutputs` | 14 | `0x1f60f40` | root-tuple element that directly passes through an aliased entry-parameter (`GetAliasedParameter`) | drop those outputs, rebuild `CreateTuple`, re-`SetUpAlias`+`Verify` the I/O alias config; WARNING | HIGH |
| `RemoveGradiantOutputs` | 15 | `0x1f5fdb0` | root-tuple element that `OutputHasAlias` and aliases a parameter (gradient/donated buffer) | remove gradient outputs, rebuild tuple + alias config; WARNING | HIGH |
| `PartitionEmbedingOps` | 16 | `0x1f59390` | embedding/gather op partitionable across TP (3158 B, 171 bb) | shard the embedding table per-partition and stitch results (mechanics a GAP — §3.6) | MED |
| `EliminateRedundantCompare` | 17 | `0x1ea1510` | post-order `match::` compare-of-compare whose `comparison_direction()` makes the outer compare redundant | replace redundant compare with its inner operand | HIGH |
| `ConfigLowering` | 20 | `0x1e90020` | per-computation `HloOpcode` set (`Rb_tree<HloOpcode>`, `HloOpcodeString`) | reports/validates opcode inventory (`"HLO Ops used in computation:"`); 723 B — substantive config lowering is driver-side | MED |
| `MetadataNaming` | 21 | `0x1f484e0` | post-order over entry computation | set each inst's `OpMetadata` name via 3-arg `StrCat` (deterministic `<…>.<op>.<id>`) | HIGH |
| `HiloConditionalToSelect` | 22 | `0x1eb0dd0` → `DoConditionalToSelect` `0x1eb1160` | `CallGraph::VisitNodes` finds `kConditional` with side-effect-free branches | inline both branches, build per-element `MakeSelectHlo`, re-tuple, replace (§3.3) | HIGH |
| `ReplaceMinimumConstant` | 24 | `0x1f62990` | scalar `f32` constant feeding a min/clamp/DUS mask whose value is −inf/lowest (`shouldReplaceConstant`) | replace with dtype's largest-negative *finite* value via `hlo_utils::GetMinNonInfValue` → `CreateConstant`+`ReplaceInstruction` (§3.7) | HIGH |
| `OptBarrierRemoval` | 29 | `0x1f56240` | post-order `get-tuple-element(optimization-barrier(tuple))` | `ReplaceInstruction(gte, tuple->mutable_operand(index))` — peel GTE through opt-barrier-of-tuple | HIGH |
| `TrivialCCRemoval` | 32 | `0x1f71e20` | `isTrivialCCOp` — collective custom-call over a degenerate (single-device) replica group | `ReplaceAllUsesWith(operand)` + `RemoveInstruction`; tuple-CC rewires `user→ccop->operand(i)`; NCC_IARE001 | HIGH |
| `CollectiveTokenRemoval` | 33 | `0x1e8e9e0` | `instIsCCOp` + `TokenAnalyzer::analyze` + `getGTEUsesLastResult`: collective output tuple carrying a trailing token | strip token from result tuple (`CloneWithNewOperands`, `ReplaceInstructionWithDifferentShape`); kill dead token producers | HIGH |
| `TransformVariadicReduce` | 35 | `0x1f71950` → `maxArgMax` `0x1f6c890` | thin post-order driver; `maxArgMax` `match::AllOf` detects variadic (value+index) max-and-argmax reduce | lower to native `MakeReduceHlo(kMax)` + iota/compare/select argmax (§3.5) | HIGH |
| `EmitOffloadedDropout` | 36 | `0x1ea3b80` | post-order; multi-instruction dropout sequence (rng→threshold→mask, nested `match::`) | replace with `AwsNeuronDropout` CC; RNG state threaded as tuple elem; `"Replaced N dropout sequences"` | HIGH |
| `InlineWeights` | 39 | `0x1ee8c60` | entry-parameter listed as a weight in `<dir>/metadata.json` (`model_files`/`weights`) | parse JSON → `npy2literal` → `CreateConstant`, then `RemoveParameter`; rebuild layout (§3.1) | CERTAIN |
| `ReplaceRng` | 40 | `0x1f63280` | post-order; HLO `rng` (array or tuple-shaped) | materialize a host-sampled constant literal (`mt19937`), `CreateConstant`(+`CreateTuple`), replace (§3.2) | CERTAIN |
| `AddLogitOutput` | 43 | `0x1e87070` | root tuple lacks logits; trace from token-ID output / detect softmax pattern | replace module output with the logits tensor; no-op if already present | HIGH |
| `InjectNumericalErrors` | 52 | `0x1ed9780` | targeted instruction list (ctor config; `"No targets specified."` if empty) | add constant error (`CreateBroadcast`+`CreateBinary(kAdd)`), rename `<x>.inj_error`, replace; `"Modified N"` | CERTAIN |
| `ConvertInputsToOptimalShape` | 54 | `0x1e97850` | entry parameter whose layout is not backend-optimal (per-input map; `strtol` config) | insert `CreateReshape`/`CreateTranspose`, `ReplaceAllUsesWithDifferentShape`; rebuild ProgramShape; thread `FrontendAttributes` | HIGH |
| `DynamicSliceTranspose` | 55 | `0x1ea02d0` (`0x1ea0570`) | OpExpander: `transpose(dynamic-slice(...))` | push transpose below DS via `InversePermutation`+`PermuteDimensions` (§3.4) | HIGH |
| `RewriteModuleDtype` | 59 | `0x1f644e0` | every instruction/IO shape whose element type matches the from-type (`Shape::Equal` filter) | `ReplaceElementType(shape, from, to)` module-wide + `UpdateEntryComputationLayout`; from/to pair a GAP | HIGH |
| `CommonInstructionElimination` | 66 | `0x1f74f20` | `flat_hash_map<inst,inst>` CSE keyed by op + `ShapeUtil::Equal` shape per computation | replace later identical inst with the first; classic GVN/CSE | HIGH |
| `ResolveSelfComparison` | 67 | `0x20028c0` (`0x2002d00`) | OpExpander: `compare(x, x)` — operands share `unique_id()`, array shape | `HloEvaluator::Evaluate` → `MakeScalarLike<bool>` constant; VLOG `"resolving"` | HIGH |
| `DeviceAssignmentLegalization` | 69 | `0x1f7c570` | every collective (`neuron::IsCollective`: AG/AR/RS/AllToAll/CP) | recreate with explicit `CollectiveDeviceList` from the device assignment (§3.8) | HIGH |
| `InjectPrints` | 70 | `0x1edaa10` | post-order; instructions selected for device-print instrumentation | insert `AwsNeuronDevicePrint` host-transfer `HloSendInstruction`; remove stale prints | HIGH |
| `ConstantSliceClampSimplifier` | 90 | `0x1f76970` (`0x1f75b70`) | OpExpander: slice/clamp over a constant (opcodes 2/4/8/26) | 48-byte Expand: returns `mutable_operand(0)` — strips the wrapper to its inner operand | HIGH |
| `RewriteScatterPartition` | 93 | `0x2009240` (`0x20093c0`) | OpExpander: collective scatter-partition idiom (`pattern_one`/`pattern_two`) | rebuild as `CreateAllGather`+`CreateReshape`, rewrite replica groups (§3.9) | HIGH |
| `ConstantSliceConvertCanonicalizer` | 109 | `0x1f76c90` (`0x1f77b10`) | OpExpander: slice/convert over a readable constant (`literal().data<u32>`; `TrueNumDimensions`) | `MakeDynamicSliceHlo` indexed by a materialized `MakeR1ConstantHlo<int>`, cast to `HloDynamicSliceInstruction`; copy `OpMetadata` | HIGH |
| `DynamicSliceReshapeCanonicalizer` | 110 | `0x1f80fa0` (`0x1f81270`) | OpExpander: `reshape(dynamic-slice(...))` / DS-of-reshape | reorder via `MakeReshapeHlo`+`MakeBinaryHlo`+`MakeR1ConstantHlo` (recompute indices); VLOG `"expanding"` | HIGH |

> **NOTE —** `HiloConditionalToSelect` (`0x1eb0dd0`) is the *Neuron* variant with worker `xla::DoConditionalToSelect` (`xlaL21…`, file-local). The stock `xla::ConditionalToSelect::Run` at `0x2956eb0` is also linked into the binary but is **not** in the Neuron registry — do not conflate. Likewise `MetadataNaming` (#21) is plain-XLA op-naming, distinct from `aws_neuron_default_metadata` (#65, an OpExpander that attaches default `OpMetadata`).

---

## 2. OpExpander dispatch

Eight passes in the closer are `OpExpanderPass` subclasses. They never override `Run`; the base `Run` at `0x29f0bb0` walks the module post-order and, for each instruction, calls the subclass virtuals.

```text
xla::OpExpanderPass::Run (0x29f0bb0)          ── shared driver, all OpExpander passes
  for each computation, post-order each inst:
    if this->InstructionMatchesPattern(inst):  ── vptr+0x28, per-pass matcher
        new = this->ExpandInstruction(inst)     ── per-pass rewriter (the algorithm)
        computation->ReplaceInstruction(inst, new)
```

> **GOTCHA —** the registry row for an OpExpander pass points at `InstructionMatchesPattern`, not `Run`. A reimplementer who treats that address as the pass driver will model the matcher predicate as the whole pass and miss the `ExpandInstruction` rewrite. The matcher returns a bool; the rewrite lives in the sibling `ExpandInstruction` body cited in §1.

OpExpander members of the closer: `DynamicSliceTranspose` (#55), `ResolveSelfComparison` (#67), `ConstantSliceClampSimplifier` (#90), `RewriteScatterPartition` (#93), `ConstantSliceConvertCanonicalizer` (#109), `DynamicSliceReshapeCanonicalizer` (#110). The `ExpandInstruction` addresses (`0x1ea0570`, `0x2002d00`, `0x1f75b70`, `0x20093c0`, `0x1f77b10`, `0x1f81270`) were each recovered as non-virtual functions reached from the shared Run — all symbol-CONFIRMED.

---

## 3. Algorithm notes — the non-trivial passes

### 3.1 InlineWeights — `Run` `0x1ee8c60` (10176 B, 494 bb) — CERTAIN

The most substantive pass in the closer. It compile-time-folds weight *parameters* into HLO constants by reading an out-of-band model directory. The asm references `metadata.json`, `model_files`, `nlohmann`, `npy2literal`, `RemoveParameter`, `CreateConstant`, and the `NCC_INL` diagnostic family — all confirmed in the disassembly.

```c
function InlineWeights_Run(module, dir):              // 0x1ee8c60
    cfg     = nlohmann::json::parse(ifstream(dir + "/metadata.json"))   // basic_ifstream open/close
    weights = cfg["model_files"] / cfg["weights"]      // map<param-name, npy-path>; strtol on indices
    for each entry-parameter p with a weights[p] entry:
        lit = hilo::npy2literal(weights[p])            // _ZN4hilo11npy2literalE…
        require ShapeUtil::Compatible(lit.shape(), p->shape())   // else NCC_INL001 "mismatching layout for parameter"
        require numpy_dtype(lit) == p->element_type()            // else NCC_INL002 "numpy file type mismatch"
        p->ReplaceAllUsesWith( HloInstruction::CreateConstant(lit) )
        computation->RemoveParameter(p->parameter_number())      // checks 0 <= no < param_instructions_.size()
    // re-derive entry ComputationLayout now that parameters are gone
    module->set_entry_computation_layout( ComputeProgramShape() )   // entry_computation_layout_.has_value() CHECK
```

Diagnostics emitted: `NCC_INL001`, `NCC_INL002`, `NCC_MOD003`; hard errors `"Corrupted input dir, model file does not exist!"` and `"Computation … has no parameter number …"`, plus the JSON type-error path (`"type must be string, but is "`). The npy loader is `hilo::npy2literal`; the JSON lib is the vendored `nlohmann::json_abi_v3_11_3`. The `CreateConstant` literals this pass plants — together with `ReplaceRng`'s host-sampled literals — are what the later BIR const-materialization stage (Part 12.8) lowers into device constant buffers.

> **NOTE —** `RemoveParameter` shifts every later parameter's `parameter_number()`. The loop must re-resolve parameter indices (or iterate highest-index-first) or it will delete the wrong parameter. The bounds check `0 <= no < param_instructions_.size()` is the only guard the binary keeps.

### 3.2 ReplaceRng — `Run` `0x1f63280` (2821 B, 140 bb) — CERTAIN

Replaces HLO `rng` ops with **statically sampled constant tensors**, so the backend never executes an RNG op. The asm confirms a `std::mt19937` engine (`mersenne_twister_engine` with the `1812433253` initialization multiplier), `uniform_int_distribution`, a `std::random_device` seed, `CreateConstant`, and `CreateTuple`.

```c
function ReplaceRng_Run(module):                      // 0x1f63280
    rd  = std::random_device()                        // _M_getval / _M_fini
    gen = std::mt19937(rd())                           // mersenne_twister_engine<…,1812433253>
    for each inst in post-order:
        if inst->opcode() != kRng: continue
        shape = inst->shape()
        if shape.IsTuple():                            // (state, value) rng
            elems = []
            for i in range(shape.tuple_shapes_size()):
                lit_i = sample_literal(shape.tuple_shapes(i), gen)   // uniform_int_distribution into Piece::buffer
                elems.push( CreateConstant(lit_i) )
            new = CreateTuple(elems)
        else:
            new = CreateConstant( sample_literal(shape, gen) )
        parent->ReplaceInstruction(inst, new)
```

> **GOTCHA —** the seed comes from `std::random_device`, so two compiles of the same module produce **different** folded RNG constants — compilation is non-deterministic at this pass unless an upstream seed override is wired in (not located in this binary; GAP). A reimplementer reproducing bit-for-bit golden outputs must pin the seed.

### 3.3 HiloConditionalToSelect — `Run` `0x1eb0dd0` (494 B) → `DoConditionalToSelect` `0x1eb1160` (2347 B) — HIGH

Flattens a predicated 2-branch `kConditional` into a per-element `select`, removing control flow for the data-parallel TPB engines. `Run` walks the call graph (`CallGraph::VisitNodes`, `"Running conditional-to-select pass"`); the worker does the surgery. The asm confirms `HasSideEffect`, `CallInliner`, `MakeSelectHlo`, and `CreateTuple`.

```c
function DoConditionalToSelect(cond):                 // 0x1eb1160
    for each branch b in cond->branch_computations():
        if b->HasSideEffect():
            return  // "Not transforming conditional; branches have side effects:"
    CallInliner::Inline(cond->true_computation())      // splice both branches inline
    CallInliner::Inline(cond->false_computation())
    pred = cond->operand(0)
    results = []
    for each leaf element of cond->shape():
        p   = convert_to_pred(pred, leaf)              // CreateConvert / ChangeElementType to PRED
        sel = MakeSelectHlo(p, true_val(leaf), false_val(leaf))
        results.push(sel)
    new = (cond->shape().IsTuple()) ? CreateTuple(results) : results[0]
    parent->ReplaceInstruction(cond, new)
```

### 3.4 DynamicSliceTranspose — matcher `0x1ea02d0`, Expand `0x1ea0570` — HIGH

OpExpander. Matcher: `transpose(dynamic-slice(...))`. The Expand pushes the transpose below the dynamic-slice so the slice operates on the original (pre-transpose) layout. Asm confirms `InversePermutation`, `PermuteDimensions`, `CreateDynamicSlice`, `CreateTranspose`.

```c
function DynamicSliceTranspose_Expand(transpose):     // 0x1ea0570
    ds    = transpose->operand(0)                      // the dynamic-slice
    perm  = transpose->dimensions()                    // transpose permutation
    inv   = InversePermutation(perm)
    // permute the slice sizes and start indices into the operand's frame
    sizes = ShapeUtil::PermuteDimensions(inv, ds->dynamic_slice_sizes())
    starts = permute(ds->index_operands(), inv)
    new_ds = CreateDynamicSlice(ds->operand(0), starts, sizes)
    return CreateTranspose(new_ds, perm)               // transpose now applies to the smaller, sliced tensor
```

### 3.5 TransformVariadicReduce — `Run` `0x1f71950` (149 B) → `maxArgMax` `0x1f6c890` (20653 B) — HIGH

`Run` is a 4-callee post-order dispatcher calling `xla::maxArgMax` on each instruction. `maxArgMax` is the 20 KB worker: nested `match::AllOf` patterns recognize a variadic `kReduce` that jointly computes a max value and its arg-max index (a `to_apply()` doing compare+select+index-update over a 2-tuple). It lowers this single variadic reduce into native ops: `MakeReduceHlo(kMax)` for the value, and an iota/compare/select arg-max chain (`MakeCompareHlo` with a `comparison_direction()`, `MakeSelectHlo`, `MakeBroadcastHlo`, `LiteralUtil::MaxValue` sentinel), re-tuples via `CreateTuple`, and `ReplaceInstruction(inst, newTuple)`. The exact comparator topology (tie-break direction) is a minor GAP.

### 3.6 PartitionEmbedingOps — `Run` `0x1f59390` (3158 B, 171 bb) — MED

A large body with no distinctive strings; callees are generic shape/instruction builders. By name and pipeline position (immediately after the output-shaping passes, before `EliminateRedundantCompare`) it partitions embedding/gather ops across the tensor-parallel degree, sharding the embedding-table operand and stitching the per-shard gather results. The exact gather→per-shard-gather construction is **not string-anchored** here (MED — name + size + position only; rewrite mechanics a GAP).

### 3.7 ReplaceMinimumConstant — `Run` `0x1f62990` — HIGH

Despite the name, this pass does **not** rewrite a `minimum` opcode. It rewrites a scalar *constant* — the −inf/lowest sentinel that feeds a min/clamp/dynamic-update-slice mask-fill — to the dtype's largest-negative *finite* value, so the masked positions survive downstream arithmetic that would otherwise propagate NaN/inf. The per-dtype finite floor comes from `hlo_utils::GetMinNonInfValue` (confirmed callee), wrapped by `CreateConstant` and applied by `ReplaceInstruction`. The matcher gate is `shouldReplaceConstant` (confirmed). The "min" in the name is the sentinel it *replaces*, not the consuming opcode.

> **CORRECTION (B32-4) —** an earlier reading treated this as a `minimum`-op rewriter. The binary shows the replaced instruction is the scalar constant operand of a min/clamp/DUS, retyped to a finite floor via `GetMinNonInfValue` — not the min op itself.

### 3.8 DeviceAssignmentLegalization — `Run` `0x1f7c570` (6591 B, 350 bb) — HIGH

Rewrites every collective's *logical* replica groups into a *physical* `CollectiveDeviceList` derived from the module's device assignment. Asm confirms `IsCollective`, `GetGlobalDeviceReplicaGroups`, `GetParticipatingDevicesGroupsForSourceTargetPairs`, `CollectiveDeviceList`, `CreateAllGather`, `CreateCollectivePermute`, and the `"Unhandled Collective"` FATAL path.

```c
function DeviceAssignmentLegalization_Run(module):    // 0x1f7c570
    for each inst:
        if !neuron::IsCollective(inst): continue       // AG/AR/RS/AllToAll/CollectivePermute
        switch inst->opcode():
          case kCollectivePermute:
            devs = GetParticipatingDevicesGroupsForSourceTargetPairs(inst)
            new  = CreateCollectivePermute(inst->shape(), inst->operand(0),
                                           CollectiveDeviceList(devs), inst->channel_id())
          case kAllGather / kAllReduce / kReduceScatter / kAllToAll:
            CHECK(inst->operands().size() == 1)         // single-operand path
            devs = GetGlobalDeviceReplicaGroups(inst)
            new  = CreateAllGather/AllReduce/…(inst->shape(), inst->operand(0),
                                               CollectiveDeviceList(devs),
                                               inst->channel_id(), inst->to_apply())
          default:
            FATAL("Unhandled Collective instruction:")  // every collective opcode must be handled
        parent->ReplaceInstruction(inst, new)           // VLOG "Collective before/after Device Assignment Legalization:"
```

### 3.9 RewriteScatterPartition — matcher `0x2009240`, Expand `0x20093c0` (2614 B) — HIGH

OpExpander. The matcher delegates to anonymous-namespace `pattern_one` (`0x2008f90`) / `pattern_two` (`0x20090e0`), which walk an operand chain via `mutable_operand` and test opcode constants for a scatter-style partition built from collectives + reshapes around a loop: all-gather (4), all-reduce (7), collective-permute (0x1D), GTE (0x3A), reshape (0x5B), while (0x79). The Expand rebuilds it as a single `CreateAllGather`+`CreateReshape`, recomputing the replica-group list and casting through `HloAllGatherInstruction`. Asm confirms `CreateAllGather`, `CreateReshape`, `HloAllGatherInstruction`, `ReplaceOperandWithDifferentShape`, and the VLOG strings `"Num groups"`, `"Num per group"`, `"reshapes tuple elemnt"` (sic — the typo is in the binary).

```c
function RewriteScatterPartition_Expand(inst):        // 0x20093c0
    match = pattern_one(inst) || pattern_two(inst)     // walk mutable_operand chain
    if !match: FATAL("Instruction does not match any known pattern")
    groups = recompute_replica_groups(inst)            // RepeatedField<long>::Reserve; "Num groups N"/"Num per group N"
    ag = CreateAllGather(target_shape, inst->operand(0), groups, channel_id)  // VLOG "Before/After all-gather"
    ag = static_cast<HloAllGatherInstruction*>(ag)
    rs = CreateReshape(final_shape, ag)                // "reshapes tuple elemnt"
    inst->ReplaceOperandWithDifferentShape(idx, rs)    // "After operand"
    return rs
```

---

## 4. Adversarial self-verify

The five strongest claims on this page, re-challenged against the binary:

1. **All 28 Run/matcher addresses resolve to their named classes.** Re-pulled each `_0xADDR.asm` filename from `disasm/`; every one demangles to the expected `xla::<Class>::Run` (or `…::InstructionMatchesPattern`). CONFIRMED — e.g. `0x1ee8c60`→`xla::InlineWeights::Run`, `0x1f7c570`→`xla::hilo::DeviceAssignmentLegalization::Run`, `0x20028c0`→`xla::hilo::ResolveSelfComparison::InstructionMatchesPattern`. No failures.

2. **InlineWeights reads `metadata.json` and folds via `npy2literal`+`CreateConstant`+`RemoveParameter`.** `rg` over the InlineWeights asm returned `metadata.json`, `model_files`, `nlohmann`, `npy2literal`, `RemoveParameter`, `CreateConstant`, `NCC_INL` — all present. CONFIRMED.

3. **ReplaceRng uses a `std::mt19937` (1812433253) seeded from `random_device`.** Asm contains `mersenne_twister`, `1812433253`, `uniform_int_distribution`, `random_device`, `CreateConstant`, `CreateTuple`. CONFIRMED. The non-determinism caveat is therefore a real GOTCHA, not speculation. The internal `Piece::buffer` write detail is INFERRED from the literal-build pattern (not individually traced).

4. **DeviceAssignmentLegalization FATALs on an unhandled collective and uses two device-group sources.** Asm contains `GetGlobalDeviceReplicaGroups`, `GetParticipatingDevicesGroupsForSourceTargetPairs`, `CollectiveDeviceList`, `Unhandled Collective`, `IsCollective`. CONFIRMED. The `CHECK(operands().size()==1)` is on the single-operand path; the per-opcode switch shape is STRONG (callee set), the exact branch ordering INFERRED.

5. **`HiloConditionalToSelect` is the Neuron pass, distinct from stock `xla::ConditionalToSelect` at `0x2956eb0`.** Worker symbol is `xlaL21DoConditionalToSelect` (file-local `L`), and asm confirms `HasSideEffect`, `CallInliner`, `MakeSelectHlo`. CONFIRMED distinct from `0x2956eb0`.

Tagged-INFERRED/GAP items, not fabricated: `PartitionEmbedingOps` sharding mechanics (§3.6, MED); `RewriteModuleDtype` from/to dtype pair (ctor literal, no strings); `InjectNumericalErrors` target-source flag/env key (only `"No targets specified."` located); `TransformVariadicReduce` tie-break direction; the OpExpander matcher opcode-value guards (read from callees, not decompiled control flow).

---

## Related Components

| Name | Relationship |
|---|---|
| [Pass Registry](pass-registry.md) | the `--passes` table / `RegisterHiloHloPasses` ordering these 28 passes index into |
| [Collectives → Custom-Call](collectives-to-customcall.md) | upstream collective forward-conversion; `DeviceAssignmentLegalization`/`TrivialCCRemoval` clean up after it |
| [DUS/DS Simplifier & DynamicSlice Mover](dus-ds-simplifier.md) | sibling dynamic-slice canonicalizers; this page's #55/#90/#109/#110 are the OpExpander cousins |
| Const materialization (Part 12.8) | where `InlineWeights`/`ReplaceRng`-folded constants are lowered to BIR device buffers |

## Cross-References

- [Pass Registry](pass-registry.md) — Part 4.1: registry rows (Run/matcher addr, vtable, factory) for every pass tabulated here
- [Collectives → Custom-Call](collectives-to-customcall.md) — the collective-conversion pass family that `DeviceAssignmentLegalization` (#69) and the CC-removal passes (#32/#33) legalize and prune
- [DUS/DS Simplifier & DynamicSlice Mover](dus-ds-simplifier.md) — the dynamic-slice pass family this page's OpExpander canonicalizers (#55/#90/#109/#110) belong to
