# DUS/DS Simplifier and the DynamicSlice Mover

> *All addresses on this page apply to neuronx-cc 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`. `.text`/`.rodata` are mapped VA==file-offset in this binary (no delta). Other versions will differ.*

## Abstract

Two HLO passes in the `hlo-opt` pipeline share a job that XLA normally folds into one peephole: cancelling a `dynamic-slice` taken from a `dynamic-update-slice` once the loop that generated their start indices has been unrolled. neuronx-cc splits it deliberately. Pass **#80 `neuron-dus-ds-index-simplifier`** (`NeuronDynamicUpdateSliceDynamicSliceSimplifier::Run` @`0x1f9a940`) does **not** rewrite the graph — it *materializes* the now-constant DUS/DS start offsets by cloning the module, inlining calls, and constant-folding the clone, then writes the discovered integers back onto the **original** instructions as text tokens (`dusidxnow=`, `dsidxnow=`, `iterationidx=`) inside `OpMetadata`. Pass **#87 `dynamic-slice-mover`** (`NeuronDynamicSliceMover::Run` @`0x1fa5730`) reads those tokens, lowers each index-resolved `dynamic-slice` to a static `slice`, and **hoists** it out of a `kCall` body across the call boundary so it runs on the caller over a narrower shape.

The coupling between them is a string protocol carried in `xla::OpMetadata`, not a data-structure handoff. This is the single most important fact for a reimplementer: if you build #80 as a classic `dus(base, ds(x)) → x` match-and-graft, you will produce a different (and, in this pipeline, wrong) compiler, because the structural exploitation is *deferred* to #87 and to `NeuronRepeatedDusToConcat` (#108), both of which drive entirely off the tokens #80 emits. The simplifier is a constant-fold-and-tag pass; the mover is a token-driven code-motion pass.

A second channel runs underneath: the mover assigns every instruction a temporary decimal `instruction_id` in `OpMetadata`'s `frontend_attributes` protobuf map, clones+inlines+DCEs the module, discovers the moves on the *clone*, then maps each move back to the original instruction by id. Both passes also strip `NeuronBoundaryMarker-Start`/`-End` custom-calls as a side errand. This page reconstructs both `Run` bodies, the exact metadata-string grammar, the index-tracking convention, the call-boundary hoist, and the `NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` kill-switch.

For reimplementation, the contract is:

- **The OpMetadata string protocol** — the exact `dusidxnow=`/`dsidxnow=`/`iterationidx=` CSV grammar, where it is stored (`OpMetadata` string field on `inst+0x200`), and how it is parsed (`find(key)` → `find(',')` → `substr` → `safe_strto32/64_base(base=10)`).
- **The clone-fold-tag mechanism of #80** — why the pass works on a *copy* and writes results back to the original by name/id, rather than rewriting in place.
- **The static-slice lowering and the call-boundary hoist of #87** — `dynamic-slice(op, i…) → slice(op, [i, i+size), stride 1)`, the single-use-GTE legality predicate, the `ReplaceParameter` + re-tuple + GTE-rebuild plumbing, and the env-var gate on param-sourced motion.

| | |
|---|---|
| **Pass #80 name()** | `neuron-dus-ds-index-simplifier` (cstr @`0x2ef800`) |
| **Pass #80 Run** | `NeuronDynamicUpdateSliceDynamicSliceSimplifier::Run` @`0x1f9a940` (10333 B, 521 bb) |
| **Pass #87 name()** | `dynamic-slice-mover` (cstr @`0x243e2e`) |
| **Pass #87 Run** | `NeuronDynamicSliceMover::Run` @`0x1fa5730` (8792 B, 475 bb) |
| **Index engine (#80)** | `xla::UpdateDynamicInstructionMetadataWithConstantIndices` @`0x1f98670` (8073 B, 371 bb) |
| **Motion engine (#87)** | `(anon)::MoveDynamicSliceOutsideCall` @`0x1fa1cc0` (14111 B, 652 bb) |
| **Pass class** | both plain `HloModulePass` (own `Run` at vptr+0x18); neither is an `OpExpander` |
| **IR level** | XLA HLO (`xla::hilo`), pre-Penguin |
| **Kill-switch** | `NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` (cstr @`0x2c2dd0`), gate via `EnvVarSetToOne` @`0x1ebe700` |
| **Pipeline order** | #80 runs **before** #87 (registration 80 → 87); tokens flow 80 → 87 → #108 |

---

## The Shared Metadata Protocol

### Purpose

Neither pass is a peephole over the live graph. Both compute their answer on a *cloned, constant-folded* copy of the module and write the answer back onto the original instructions as annotations. `xla::OpMetadata` is the scratchpad. Two distinct channels live inside it.

### Channel 1 — the CSV-in-a-string-field (the simplifier writes, the mover reads)

The simplifier builds a comma-delimited token string and stores it in a string field of the instruction's `OpMetadata`. The `OpMetadata` itself is reached at `inst+0x200`; the local copy is mutated, then committed via `OpMetadata::CopyFrom`. Three token keys exist, all verbatim from `.rodata`:

| Token (cstr) | Address | Meaning |
|---|---|---|
| `dusidxnow=` | `0x26e86b` | folded start offset of a `dynamic-update-slice` (per dim) |
| `,dusidxnow=` | `0x26e884` | comma-prefixed variant for appending to an existing CSV |
| `dsidxnow=` | `0x230955` | folded start offset of a `dynamic-slice` (per dim) |
| `,dsidxnow=` | `0x21cd8d` | comma-prefixed variant |
| `iterationidx=` | `0x26e876` | the surrounding loop's now-constant iteration number; read by **both** passes |

> **GOTCHA — each token key has two `.rodata` addresses.** Every DUS and DS token ships twice: a bare form and a comma-prefixed one — `dsidxnow=` at `0x230955`, `,dsidxnow=` at `0x21cd8d`. The pair is how the builder distinguishes "first token in the field" from "append to an existing CSV". Grabbing the wrong one silently produces a leading or missing comma.

The grammar a reimplementer must reproduce: a flat CSV where each entry is `<key><base-10 int>` and entries are joined by `,`. Multi-dimensional indices are emitted one entry per dimension and rejoined with `absl::JoinRange<vector<string>>(",")` (@`0x1f975e0`, confirmed as a callee of the index engine). A populated field reads like:

```text
...,dusidxnow=7,dsidxnow=3,iterationidx=12,...
```

### Channel 2 — the `instruction_id` frontend-attribute (the mover's clone↔orig key)

The mover needs to discover moves on a clone but apply them to the original. It does this by writing a decimal `instruction_id` (cstr @`0x20ca00`) into `OpMetadata`'s `frontend_attributes` — a *proper* `google::protobuf::Map<string,string>` (the inner key/value map), **not** the CSV string. `Clone()` preserves metadata, so an id written before cloning survives into the clone; after discovering a move on the clone, `FindInstructionById` resolves the same id back in the original module.

> **QUIRK —** the two channels are different protobuf shapes inside the same `OpMetadata`. Channel 1 is a single string field holding a hand-rolled CSV; Channel 2 is a real protobuf string→string map. A reimplementer who stuffs everything into the map, or everything into one string, will not interoperate with the parsers below.

### The parsers

```c
// NeuronDynamicUpdateSliceDynamicSliceSimplifier::ExtractIndexFromMetadata   // 0x1f97330 (309 B)
// returns std::pair<int32_t value, bool valid>
pair<int32,bool> ExtractIndexFromMetadata(const string& meta, const string& key):
    pos = meta.find(key)                       // std::string::find @0x1f97330 body
    if pos == npos: return {0, false}
    comma = meta.find(',', pos + key.size())    // next delimiter
    sv = meta.substr(pos + key.size(),          // value text between key and ','
                     comma - (pos + key.size())) // OOB -> __throw_out_of_range_fmt
    if !absl::numbers_internal::safe_strto32_base(sv, &value, /*base*/10):
        return {0, false}
    return {value, true}
```

```c
// (anon)::GetInstructionId(const HloInstruction*)   // 0x1f9d7f0 (608 B, 22 bb)
// returns std::pair<bool ok, int64_t id>
pair<bool,int64> GetInstructionId(inst):
    map = inst->metadata().frontend_attributes()      // the string->string Map at metadata+0x70
    it  = map.InnerMap::FindHelper("instruction_id")
    if it == end: error "Instruction has no ID attribute: "   // also " has no instruction_id, skipping" @0x2d96d0
    if !safe_strto64_base(it->value, &id, 10):
        error "Invalid instruction ID format for instruction: "
    return {true, id}
```

`CreateStaticSliceFromDynamicSlice` parses `iterationidx=` with the `absl::ByChar::Find` + `substr` + `safe_strto64_base` path (int64) — see §`NeuronDynamicSliceMover`. The two parsers differ by integer width: index tokens are `int32` (`safe_strto32`), ids and iteration indices are `int64` (`safe_strto64`).

---

## NeuronDynamicUpdateSliceDynamicSliceSimplifier

### Purpose

Discover the now-constant DUS/DS start indices (constant because upstream loop unrolling — `unroll-while-loop` #25 / `while_loop_unroller` #112 — substituted literals for induction variables) and record them as `dusidxnow=`/`dsidxnow=`/`iterationidx=` tokens on the original instructions. Strip `NeuronBoundaryMarker` custom-calls. Emit no structural rewrite of the DUS∘DS chain itself: that is deferred to #87 and #108, which read the tokens.

Despite the name, this is not a structural peephole. The `Run` body never grafts `dus(base, ds(x)) → x` into the live graph — there is no `Replace*` of a DUS base anywhere in it. It is a clone-and-constant-fold **index-materialization** pass whose product is metadata. The workhorse is `UpdateDynamicInstructionMetadataWithConstantIndices` @`0x1f98670`, and the only live-module rewrite it performs is the GTE/tuple-user splice in §Run pass 4.

### Entry Point

```text
NeuronDynamicUpdateSliceDynamicSliceSimplifier::Run        0x1f9a940 (10333 B)
  ├─ HloComputation::MakeInstructionPostOrder              (pass 1 + pass 2 walks)
  ├─ OpMetadata(inst->metadata())   copy ctor              (per-inst local scratch)
  ├─ ArenaStringPtr::Set / OpMetadata::CopyFrom            (commit tokens to inst+0x200)
  ├─ HloModule::Clone("", {})                              0x1f9b631
  ├─ CallInliner::Run(clone, threads)                      0x1f9b728  (src line 406 on failure)
  ├─ HloConstantFolding::Run(clone, threads)               0x1f9c249
  └─ UpdateDynamicInstructionMetadataWithConstantIndices   0x1f98670  (the index engine)
       ├─ GetConstantOperandValue                          0x1f97ff0
       ├─ ExtractIndexFromMetadata                         0x1f97330  (idempotent re-parse)
       ├─ absl::JoinRange<vector<string>>(",")             0x1f975e0  (multi-dim token join)
       └─ FlatHashMap<string,string>::try_emplace          (op_name -> token side table)
```

### Algorithm

```c
StatusOr<bool> Run(HloModule* m, const flat_hash_set<string_view>& threads):   // 0x1f9a940
    changed = false

    // PASS 1 — annotate every dynamic op with its (still-symbolic) DUS/DS index tokens.
    for inst in m->computation->MakeInstructionPostOrder():        // 0x1f9aa68
        local_md = OpMetadata(inst->metadata())                   // copy ctor 0x1f9aaea
        s = ""                                                    // CSV builder (_M_append/_M_replace)
        if inst is kDynamicUpdateSlice:  s += ",dusidxnow=<idx>"   // str 0x26e884, per operand dim
        if inst is kDynamicSlice:        s += ",dsidxnow=<idx>"    // str 0x21cd8d
        ArenaStringPtr::Set(local_md.op_name, s, arena)            // 0x1f9b1c9 / .50d / .b6d / 0x1f9c8c9
        (*(OpMetadata*)(inst+0x200)).CopyFrom(local_md)            // commit 0x1f9b1e3
        changed = true
        ~local_md                                                 // 0x1f9b216

    // PASS 2 — strip NeuronBoundaryMarker custom-calls (2nd postorder @0x1f9b25a).
    for inst in postorder2:
        if inst->opcode() == kCustomCall                          // byte [inst+0x14]==0x2B @0x1f9b88f
           and (inst->custom_call_target() == "NeuronBoundaryMarker-Start"   // 0x25f0c8, ::compare 0x1f9b8ae
             or inst->custom_call_target() == "NeuronBoundaryMarker-End"):   // 0x27a6fd, ::compare 0x1f9b8d3
            CHECK exactly 1 operand   // "NeuronBoundaryMarker-Start expected to have 1 operand but has " 0x2fbc78
            op0 = inst->mutable_operand(0)                        // 0x1f9bc33
            inst->ReplaceAllUsesWith(op0)                         // 0x1f9bc4d  (fail: 0x28a750)
            comp->RemoveInstruction(inst)                        // 0x1f9bc77  (fail: 0x34c0a8)

    // PASS 3 — clone, inline calls, constant-fold to realize the constant indices.
    CHECK(m->entry_computation() != nullptr)                     // "nullptr != entry_computation_" 0x2108c9
    clone = m->Clone("", {})                                     // 0x1f9b631
    st = CallInliner::Run(clone, threads)                        // 0x1f9b728 (line 406)
        if !st.ok(): LOG "Failed to run CallInliner: ";  return st
    st = HloConstantFolding::Run(clone, threads)                 // 0x1f9c249
        if !st.ok(): LOG "Failed to run HloConstantFolding: ";  return st

    // PASS 4 — read folded indices back onto the ORIGINAL, splice GTE/tuple users.
    UpdateDynamicInstructionMetadataWithConstantIndices(m, clone) // 0x1f9c30a  (workhorse below)
    for gte in tuple-element users that folded to constants:     // tuple_index() @0x1f9c0ef
        CHECK(tuple_index in [0, element_count))                 // else "Expected tuple instruction, got " (line 241)
        gte->ReplaceAllUsesWithDifferentShape(folded_const)      // 0x1f9c07a
        comp->RemoveInstruction(gte)                             // 0x1f9c09b
    return changed
```

> **NOTE —** the source-line constants are embedded as the second argument to the failure `LogMessage` (`mov edx,<line>`): `0x196`=406 (CallInliner failure path), `0xF1`=241 (tuple/GTE bounds error). The driver `ecx=2` selects `LogSeverity` ERROR/FATAL. These pin the failure branches even though `Run` was decompile-skipped (its `decompiled/*.c` is a 7-line stub).

### The index engine — `UpdateDynamicInstructionMetadataWithConstantIndices` @`0x1f98670`

This is the equality/normalization engine (371 bb). It walks the *folded* clone, and for each dynamic instruction whose index operands are now `kConstant`, reads the scalar and writes the canonical token onto the matching *original* instruction.

```c
void UpdateDynamicInstructionMetadataWithConstantIndices(HloModule* orig, HloModule* folded):  // 0x1f98670
    name_to_token = FlatHashMap<string,string>()              // try_emplace @0x1f97d50: op_name -> token
    for inst in folded->postorder:
        if inst is kDynamicUpdateSlice or kDynamicSlice:
            per_dim = vector<string>()
            for d in index-operand dims:
                v = GetConstantOperandValue(inst, first_index_operand + d)   // 0x1f97ff0
                per_dim.emplace_back(format(v))                             // emplace_back @0x1f97940
            joined = absl::JoinRange(per_dim, ",")                          // 0x1f975e0
            existing = ExtractIndexFromMetadata(inst->metadata(), key)      // 0x1f97330 (idempotency)
            name_to_token.try_emplace(inst->op_name(), token(joined))       // keyed by op_name
    // write tokens back onto matching originals by op_name / iteration token
    for o in orig->postorder:
        if name_to_token.contains(o->op_name()):
            o->metadata().<csv field> = name_to_token[o->op_name()]         // dusidxnow=/dsidxnow=/iterationidx=
```

`GetConstantOperandValue(inst, operand_idx)` @`0x1f97ff0` (1169 B, 66 bb) is the constant-index reader: it takes `inst->mutable_operand(idx)->literal()`, guards that the shape is a dense array scalar (`Shape::array_state()` + `Shape::layout()`), then dispatches on `PrimitiveType`, reads `LiteralBase::Piece::buffer()`, and widens the scalar to `int64` across the integer/index dtypes. The 66-bb body is a per-PrimitiveType branch ladder; the exact accepted dtype set is integer/index types (the closed enumeration row-by-row is not extracted — *MED*).

> **NOTE —** the DUS∘DS cancellation *predicate* — when `dsidxnow == dusidxnow` over all sliced dims implies the DS reduces to the inserted operand vs. reading the DUS base — is decided numerically here on the folded literals, but the closed-form interval-intersection rule that *acts* on the equality is not in this pass. It lives in the token consumers (#87 `CreateStaticSliceFromDynamicSlice`, #108 `NeuronRepeatedDusToConcat`). This pass folds-then-tags; it does not graft. (Mechanism CERTAIN; exact downstream predicate MED.)

### Diagnostics

`"Expected tuple instruction, got "`, `"Failed to run CallInliner: "`, `"Failed to run HloConstantFolding: "`, `"Failed to remove NeuronBoundaryMarker: "` (`0x34c0a8`), `"Failed to replace uses of NeuronBoundaryMarker: "` (`0x28a750`), `"NeuronBoundaryMarker-Start expected to have 1 operand but has "` (`0x2fbc78`), `"nullptr != entry_computation_"` (`0x2108c9`). Opcode constant `kCustomCall = 0x2B` (byte-compare at `inst+0x14`).

---

## NeuronDynamicSliceMover

### Purpose

Hoist a `dynamic-slice` out of a called computation (a `kCall` body — typically an unrolled loop body or a fusion over a tuple parameter) across the call boundary, so the slice executes on the caller side over a narrower shape. Where the iteration index is now constant (via the `iterationidx=` token from #80), additionally lower the `dynamic-slice` to a static `slice`. Legality is a single-use-GTE predicate; motion rewires the call's tuple/parameter/GTE plumbing.

The motion is one-directional: a `dynamic-slice` moves *outward* from a `kCall` body to its caller, via param-replace plus a tuple/GTE rebuild, with static-slice lowering when the iteration index is constant. There is no sink direction. The engine is `MoveDynamicSliceOutsideCall` @`0x1fa1cc0`.

### Entry Point

```text
NeuronDynamicSliceMover::Run                              0x1fa5730 (8792 B)
  ├─ AssignUniqueInstructionIds(m)                        0x1f9e140  (write "instruction_id" map entries)
  ├─ HloModule::Clone("clone", {})                        0x1fa57c4  (str "clone")
  ├─ CallInliner::Run(clone, threads)                     0x1fa58a1  (inline so DS sources are traceable)
  ├─ TraceAndReplaceSourceForDynamicSlice                 0x1f9eb50  (per clone DS — walk op0 to real source)
  ├─ GetInstructionId / FindInstructionById               0x1f9d7f0 / 0x1f9da90  (clone -> orig)
  ├─ FixGteShapeMismatches(m, clone)                      0x1fa64a1 -> 0x1fa01f0
  ├─ HloDCE::Run(m, threads)                              0x1fa6512
  ├─ RemoveInstructionIds(m)                              0x1f9e770  (strip temporary ids)
  └─ MoveDynamicSliceOutsideCall                          0x1fa1cc0  (the motion engine)
       └─ CreateStaticSliceFromDynamicSlice               0x1f9f210  (index rewrite, reads iterationidx=)
```

### Algorithm

```c
StatusOr<bool> Run(HloModule* m, const flat_hash_set<string_view>& threads):   // 0x1fa5730
    AssignUniqueInstructionIds(m)                          // 0x1fa576b -> 0x1f9e140
    clone = m->Clone("clone", {})                          // 0x1fa57c4
    CallInliner::Run(clone, threads)                       // 0x1fa58a1
    for ds in clone's dynamic-slices:
        TraceAndReplaceSourceForDynamicSlice(ds)           // 0x1fa5cb3 -> 0x1f9eb50
        id   = GetInstructionId(ds)                        // 0x1fa5e84
        if !id.ok:  // " to have instruction_id, but it doesnt, so ignoring optimization for dynamic-slice=" 0x393ca8
            continue
        orig = FindInstructionById(m, id)                  // 0x1fa5eab
        orig->ReplaceOperandWithDifferentShape(k, new_src) // 0x1fa6001
    FixGteShapeMismatches(m, clone)                        // 0x1fa64a1 -> 0x1fa01f0
    HloDCE::Run(m, threads)                                // 0x1fa6512
    // strip NeuronBoundaryMarker-{Start,End} (same idiom as #80)
    for bm in custom-calls matching the two targets:       // compares @0x1fa5a69 / 0x1fa5a82
        CHECK 1 operand   // "NeuronBoundaryMarker expected to have 1 operand but has " 0x37bce0
        bm->ReplaceAllUsesWith(bm->operand(0))             // 0x1fa6635
        comp->RemoveInstruction(bm)                        // 0x1fa6655
    RemoveInstructionIds(m)                                // 0x1fa682f -> 0x1f9e770
    // late: apply the discovered cross-boundary moves on the ORIGINAL, mapped by id
    id = GetInstructionId(...); src = FindInstructionById(m, id)   // 0x1fa6ac0 / 0x1fa6c4e
    MoveDynamicSliceOutsideCall(ds, call, gte, comp)       // 0x1fa76a5 -> 0x1fa1cc0
    LOG "Completed handling dynamic slices outside calls." // 0x2d96f8
    return changed
```

### The motion engine — `MoveDynamicSliceOutsideCall` @`0x1fa1cc0`

14111 B, 652 bb, 63 callees. Legality first, then the re-tupling motion.

**Legality (verbatim guard strings, all referenced from this function):**

1. The call's operand must be a tuple — `"Call operand is not a tuple"` (`0x25b05f`).
2. The dynamic-slice's input must be a GTE of that tuple — `"Dynamic slice inside call doesn't use GTE as input"`.
3. **Single-consumer GTE** — the GTE feeding the DS may only be used by {the dynamic-slice, the root/output tuple, a `NeuronBoundaryMarker-End`}: `"Dynamic slice input GTE has users that are not dynamic-slice, root tuple, or NeuronBoundaryMarker-End"` (`0x31ca98`) / `"... has other users than ..."` (`0x3f17d0`). Detail diagnostics: `" dynamic-slice users for GTE "`, `" which is not a dynamic-slice, root, or output tuple"`, `"Cannot move dynamic slice outside call: GTE "`.
4. **Env-var kill-switch** — `EnvVarSetToOne("NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM")`. When set, logs `"NOTICING NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM=1"` and `"NOT MOVING DYNAMIC-SLICE FROM PRARAMS AS NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM IS ENABLED."` (`0x3f1740`), plus `"NOT MOVING ds= "` (`0x2348ae`), and the **param-sourced** move is skipped — GTE/non-param moves still proceed. Guard: `param_no < (int64)param_instructions_.size()`.

> **GOTCHA —** the kill-switch string contains the typo **`PRARAMS`** (for "PARAMS"), preserved verbatim at `0x306430`/`0x3f1740`. A grep for `PARAMS` will miss it. The env-var *name* itself is spelled correctly: `NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` (`0x2c2dd0`). `EnvVarSetToOne` @`0x1ebe700` is called by exactly one function in this binary — `MoveDynamicSliceOutsideCall` — so this gate is the only consumer of that env var here.

**Motion / re-tupling (callee evidence — HIGH):** the slice is pulled up from inside the called body to the caller. The body parameter is replaced by its already-sliced (smaller) version (`ReplaceParameter` with `"updated_param"`); the call's input tuple is rebuilt to feed the sliced operand (`MakeValidatedTupleShape` / `CreateTuple` / `CreateCall`); the output tuple/GTE chain is reshaped (`CreateGetTupleElement`, `set_root_instruction(accept_different_shape=true)`, `ReplaceOperandWithDifferentShape` / `ReplaceAllUsesWithDifferentShape`); shape repair via `FixTupleShapeMismatch` @`0x1fa0ff0` and `ShapeUtil::Compatible`; the old instruction is removed. Errors: `"Cannot move dynamic slice outside call: GTE "`, `"Did Not Get equivalent ds instruction=...inside simplified module."`

### The index rewrite — `CreateStaticSliceFromDynamicSlice` @`0x1f9f210`

3509 B, 170 bb. This is where the dynamic offset becomes a literal `[idx, idx+size)` interval per dim. Its callee set is confirmed: `CreateSlice`, `dynamic_slice_sizes`, `safe_strto64_base`, `ByChar::Find`, `OpMetadata::CopyFrom`, `AddInstruction`.

```c
StatusOr<HloInstruction*> CreateStaticSliceFromDynamicSlice(ds, gte, comp):   // 0x1f9f210
    md = ds->metadata()                                    // the CSV string from #80
    // parse the constant start index out of "iterationidx=<...>":
    p   = absl::ByChar::Find(md, "iterationidx=")           // str 0x26e876, ByChar::Find @0x9912260
    end = absl::ByChar::Find(md, ',', from = p+len)         // next delimiter
    sv  = md.substr(p+len, end - (p+len))                   // OOB -> throw
    if !safe_strto64_base(sv, &idx, /*base*/10):            // int64 per dim
        error  // "Not enough indices for instruction=" 0x295868 / "Not enough indices." 0x263073
    sizes = ds->dynamic_slice_sizes()                       // per-dim slice extent
    for d in dims:
        starts[d]  = idx[d]
        limits[d]  = starts[d] + sizes[d]
        strides[d] = 1
    new = HloInstruction::CreateSlice(ds->shape(), source, starts, limits, strides)   // 0x964dfb0
    new->metadata().CopyFrom(ds->metadata())                // preserve tokens
    comp->AddInstruction(new, name)
    return new
```

So `dynamic-slice(operand, i0, i1, …)` with sizes `S` and a *known* iteration index becomes `slice(operand, starts = i, limits = i + S, strides = 1)`. This is the index-tracking convention's payoff: #80 resolves and records `iterationidx`; #87 reads it and converts the runtime offset into a compile-time interval.

### Source-tracing and ID correlation

`TraceAndReplaceSourceForDynamicSlice` @`0x1f9eb50` (978 B) walks `ds->operand(0)` up the producer chain (across GTE/tuple/bitcast) to the true source whose shape equals the slice input, then `ReplaceOperandWith(0, real_source)`, guarded by `ShapeUtil::Equal` / `ShapeUtil::HumanString`. This makes the single-use-GTE legality test decidable — it answers "what does the DS actually read?". `FixGteShapeMismatches` @`0x1fa01f0` (177 bb) is the post-move cleanup: it repairs GTEs whose declared shape no longer matches their tuple parent's element shape (because the move narrowed an element).

The id helpers exist because the pass mutates a *clone* and must apply moves to the *original*:

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `AssignUniqueInstructionIds` | `0x1f9e140` | 1548 B | write `instruction_id=<seq>` into each inst's frontend map | CERTAIN |
| `GetInstructionId` | `0x1f9d7f0` | 608 B | read it back (`map@metadata+0x70`, `safe_strto64`) | CERTAIN |
| `FindInstructionById` | `0x1f9da90` | 786 B | linear search; `"No instruction found with ID: "` | CERTAIN |
| `RemoveInstructionIds` | `0x1f9e770` | 789 B | strip the temporary ids before return | CERTAIN |

---

## Evidence summary and limits of this reading

Every function address and size on this page resolves to the named demangled symbol in the
symbol table, with the stated size and basic-block count: `0x1f9a940`, `0x1fa5730`,
`0x1f98670`, `0x1f97ff0`, `0x1f97330`, `0x1fa1cc0`, `0x1f9f210`, `0x1f9eb50`, `0x1fa01f0`,
`0x1fa0ff0`, `0x1f9e140`, `0x1f9d7f0`, `0x1f9da90`, `0x1f9e770`, `0x1ebe700`.

The negative claim about #80 — that it grafts nothing structural — is anchored to the callee
sets of `0x1f9a940` and `0x1f98670`. The only live-module mutations in `Run` are the
boundary-marker strip in pass 2 and the GTE/tuple-user splice in pass 4; the index engine
writes `OpMetadata` and calls `try_emplace` / `JoinRange` / `GetConstantOperandValue`, with
no `Replace*` of a DUS base anywhere.

The kill-switch story is equally tight: the cstr sits at `0x2c2dd0`, and `EnvVarSetToOne`
@`0x1ebe700` has exactly one caller, `MoveDynamicSliceOutsideCall` @`0x1fa1cc0`, which is
also the function referencing the "PRARAMS" log strings. The static-slice lowering is
visible in `CreateStaticSliceFromDynamicSlice` @`0x1f9f210`, whose callee set includes
`HloInstruction::CreateSlice`, `dynamic_slice_sizes`, `safe_strto64_base`, `ByChar::Find`,
`OpMetadata::CopyFrom`, and `AddInstruction`.

Three things stay coarse. `GetConstantOperandValue`'s 66-block `PrimitiveType` dispatch is
not enumerated dtype by dtype. The field-by-field token assembly inside the 371-block index
engine is inferred from its callees rather than transcribed. And because the `Run` bodies
ship as decompile-skipped stubs, control flow throughout is reconstructed from call-target
order plus embedded source-line constants — the evidence for *what* happens is strong, the
exact branch structure less so.

---

## Related Components

| Phase | Name | Relationship |
|---|---|---|
| #25 / #112 | `unroll-while-loop` / `while_loop_unroller` | substitute constant iteration vars upstream; their literals are what #80 folds and tags |
| #80 | `neuron-dus-ds-index-simplifier` | writes `dusidxnow=`/`dsidxnow=`/`iterationidx=` tokens; strips boundary markers |
| #87 | `dynamic-slice-mover` | reads `iterationidx=` → static slice; hoists DS out of `kCall` |
| #108 | `neuron_repeated_dus_to_concat` | consumes the same constant indices to fold repeated DUS → concat |

## Cross-References

- [Concatenation Optimizations](concat-optimizations.md) — Part 4.18, the other consumer of `iterationidx=`/`dusidxnow=` tokens; shares the constant-index convention
- [Dynamic-Shape Front-End](mlir-dynamic-shape-frontend.md) — Part 4.31, where dynamic-slice index shapes originate before unrolling resolves them
- [Boundary Markers & Layer-Cut Analysis](boundary-markers-layer-cut.md) — the `NeuronBoundaryMarker-Start/-End` custom-calls both passes strip
- [While-Loop Unroll & All-Gather Trip-Count Rewrite](whileloop-unroll-tripcount.md) — upstream loop unrolling that makes the indices constant
