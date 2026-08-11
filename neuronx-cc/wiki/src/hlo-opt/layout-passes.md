# Layout Passes

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`. Addresses are virtual (as IDA reports them); the raw file offset is VA−0x200000 for `.rodata` / VA−0x201000 for `.text`. Other versions will differ.*

## Abstract

Three Neuron HLO passes pin the module onto XLA's **canonical descending (major-to-minor) layout** before the Tonga middle-end ever runs. A *descending* layout is one whose `minor_to_major` permutation is exactly `{rank−1, rank−2, …, 1, 0}` — dimension 0 is the slowest-varying, dimension `rank−1` the fastest. This is the layout XLA calls "default," and it is the only layout the downstream Tensorizer / Walrus stages are written to assume. The three passes here are what *make* that assumption safe: by the time the pipeline reaches layout-sensitive lowering, every entry parameter, every constant, every instruction shape, and every externally-visible output has been forced to (or validated as) descending.

The three are registered into the same `hloPassRegistry` `llvm::StringMap` that backs the `--passes` table, at three very different pipeline slots. `io-layout-normalization` (#23) runs **early**, ahead of the collective rewrites, and does the bulk work: it canonicalizes constant literals into descending storage (the only pass that touches literal bytes or inserts a transpose) and stamps a default layout onto every instruction's shape. `aws_neuron_ensure_descending_layout_in_root` (#71) and `aws_neuron_relax_collectives_layout_constraint` (#75) run **late**, immediately before the four collective combiners (#76–#79). #71 validates the entry parameters and forces the entry root tuple's leaves to descending by inserting `kCopy`. #75 clears the `constrain_layout` flag on every layout-constrained all-reduce — which is precisely the precondition that lets `NeuronAllReduceCombiner` (#79) fuse them. None of the three carries any tunable state; their registration lambdas just `operator new` the object and store its vtable.

This page reproduces each pass as annotated pseudocode anchored to its real `Run`/matcher/expander symbol, transcribes the descending-layout predicate (`IsMonotonicWithDim0Major`) and producer (`SetToDefaultLayout`), and resolves a layout-relevant offset subtlety: where `constrain_layout` and `use_global_device_ids` live on `HloAllReduceInstruction` versus `HloAllGatherInstruction`, because conflating the two corrupts the very rebuild that #75 performs.

For reimplementation, the contract is:

- The layout vocabulary: descending = `IsMonotonicWithDim0Major`; the entry-`ComputationLayout` "is this already default?" short-circuit; the TUPLE sentinel (`element_type == 0x0D`).
- The three rewrites and exactly what each inserts: #23 → new constant + transpose, then in-place shape mutation; #71 → `kCopy` per non-descending root-tuple leaf (deduped by `unique_id`); #75 → a clone of the all-reduce with one boolean flipped.
- The `HloAllReduceInstruction` field map (`constrain_layout` @+0x250, `use_global_device_ids` @+0x251, `channel_id` @+0x208, `CollectiveDeviceList` @+0x218) and why it diverges from `HloAllGatherInstruction`.

| | |
|---|---|
| **#23 `xla::IOLayoutNormalization`** | `name()` `io-layout-normalization` · vtable `0x40b8e8` · Run `0x1ed4aa0` (417 B) |
| **#71 `xla::hilo::EnsureDescendingLayoutInRoot`** | `name()` `aws_neuron_ensure_descending_layout_in_root` · vtable `0x411388` · Run `0x1f84f40` (2937 B) |
| **#75 `xla::hilo::RelaxCollectivesLayoutConstraint`** | `name()` `aws_neuron_relax_collectives_layout_constraint` · vtable `0x412f88` · match `0x20025e0` / expand `0x2002640` |
| **Descending predicate** | `LayoutUtil::IsMonotonicWithDim0Major(const Layout&)` @`0x97ca3e0` |
| **Descending producer** | `LayoutUtil::SetToDefaultLayout(Shape*)` @`0x97cc7e0` · `ComputationLayout::SetToDefaultLayout()` @`0x96fbec0` |
| **TUPLE sentinel** | `element_type == 0x0D` (13) |
| **Pipeline order** | #23 early (pre-collective-rewrite); #75 then #71 late, just before combiners #76–#79 |

---

## Layout Vocabulary

Every pass on this page reduces to one question: *is this shape's layout descending, and if not, make it so.* Three primitives encode that question.

**The predicate — `LayoutUtil::IsMonotonicWithDim0Major(const Layout&)` @`0x97ca3e0`.** It reads the Layout's `minor_to_major` inlined vector (size at `[L+0x10]`, data at `[L+0x18]` or inline) and returns TRUE iff the sequence is non-increasing across its whole length — i.e. `{rank−1, …, 1, 0}`. Empty or single-element layouts are trivially TRUE. Both #71's parameter check and #23's constant check call this by symbol (`call _ZN3xla10LayoutUtil24IsMonotonicWithDim0MajorERKNS_6LayoutE` from #71 Run @`0x1f84fd6` and from `canonicalizeConstant` @`0x1ed4861`).

**The producer — `SetToDefaultLayout`.** Two overloads: `LayoutUtil::SetToDefaultLayout(Shape*)` @`0x97cc7e0` mutates one array shape's layout to descending in place; `ComputationLayout::SetToDefaultLayout()` @`0x96fbec0` walks a `ComputationLayout`'s parameter `ShapeLayout` vector (stride 0x140, the size of a `Shape` in this build) and its result `ShapeLayout`, setting each to default.

**The TUPLE sentinel — `element_type == 0x0D`.** XLA's `PrimitiveType` byte `0x0D` (13) is `TUPLE`. Every pass tests it to decide between "this is an array shape, set/check its layout" and "this is a tuple, recurse per leaf." A `Shape` is 0x140 (320) bytes here — the stride of the parameter-shape vector copy in #71.

> **NOTE —** "descending / major-to-minor / default" are three names for the same thing throughout this subsystem. The predicate is named `IsMonotonicWithDim0Major`; the producer is named `SetToDefaultLayout`; the value is `minor_to_major = {rank−1,…,0}`. A reimplementer who keeps these as three distinct concepts will write three times the code XLA does.

---

## #23 IOLayoutNormalization — Run @ 0x1ed4aa0

### Purpose

Run early (registration slot 23, ahead of `DecomposeCCOps` and the collective rewrites) and make the *entire* entry computation speak descending layout, so every later pass sees a uniform world. It is the only one of the three that rewrites constant literal data and the only one that inserts a `kTranspose`.

### Entry Point

```text
IOLayoutNormalization::Run (0x1ed4aa0, 417 B)
  ├─ HloComputation::MakeInstructionPostOrder  (0x9634ab0)   ── sweep order (called twice)
  ├─ canonicalizeConstant (0x1ed47c0, 564 B)                 ── sweep 1 body
  │    ├─ LayoutUtil::IsMonotonicWithDim0Major (0x97ca3e0)
  │    ├─ xla::canonicalize(const Literal&)   (0x1ed4590)    ── re-lay-out literal bytes
  │    ├─ HloInstruction::CreateConstant
  │    ├─ createDecanonicalizingTranspose (0x1ed4480, 116 B) ── compensating transpose
  │    └─ HloInstruction::ReplaceAllUsesWith
  ├─ clearLayout (0x1ed3f20, recursive)                      ── sweep 2, tuple shapes
  └─ LayoutUtil::SetToDefaultLayout (0x97cc7e0)              ── sweep 2, array shapes
```

### Algorithm

```c
StatusOr<bool> IOLayoutNormalization_Run(HloModule* module):     // 0x1ed4aa0
    entry = module->entry_computation_;                          // [module+0x38]
    CHECK(entry != nullptr);                                     // 0x2108c9 "nullptr != entry_computation_"
    // HARD precondition: the entry ComputationLayout must exist, else LOG(FATAL).
    CHECK(module->config().entry_computation_layout_.has_value());  // [config+0x158] != 0
        // cold @0x1ed4a4b: LogMessageFatal(hlo_module_config.h:132,
        //   "Check failed: entry_computation_layout_.has_value() ")   // 0x2956d0

    // --- Sweep 1: canonicalize every constant to descending storage ---
    changed = false;
    for inst in entry->MakeInstructionPostOrder():              // 0x1ed4afc
        changed |= canonicalizeConstant(entry, inst);          // 0x1ed4b26

    // --- Sweep 2: stamp default layout onto every instruction's shape ---
    for inst in entry->MakeInstructionPostOrder():             // 0x1ed4b5c (second order)
        s = &inst->shape();
        if (s->element_type() == TUPLE/0x0D)                   // recurse per leaf
            clearLayout(s);                                    // 0x1ed4b80
        else
            LayoutUtil::SetToDefaultLayout(s);                 // 0x1ed4b9b — in-place descending
    return Ok(changed);                                        // changed reflects sweep-1 only
```

```c
bool canonicalizeConstant(HloComputation* comp, HloInstruction* inst):  // 0x1ed47c0
    if (inst->opcode() != kConstant/0x24) return false;        // 0x1ed47ee: cmp byte [inst+0x14],0x24
    s = inst->shape();
    if (s.has_array_state() && s.array_state().has_layout()
        && IsMonotonicWithDim0Major(s.layout()))               // 0x1ed4861
        return false;                                          // already descending — leave it

    // Non-descending constant. Physically re-lay the literal into descending
    // storage, then re-expose the ORIGINAL logical view via a transpose so no
    // existing user observes a layout change.
    lit2 = xla::canonicalize(inst->literal());                 // 0x1ed489b — reorder bytes
    c2   = comp->AddInstruction(CreateConstant(lit2), "");     // 0x1ed48ad — new descending constant
    t    = comp->AddInstruction(
              createDecanonicalizingTranspose(comp, /*orig=*/s, c2), "");  // 0x1ed4918
    CHECK_OK(inst->ReplaceAllUsesWith(t, ""));                 // 0x1ed4975
        // assert literal @0x1ed4986: "constant->ReplaceAllUsesWith(transpose)"   // 0x3116e8
    return true;
```

```c
HloInstruction* createDecanonicalizingTranspose(comp, origShape, newConst):  // 0x1ed4480
    perm = LayoutUtil::MakeLogicalToPhysical(origShape.layout());  // 0x1ed44a6
    return HloInstruction::CreateTranspose(origShape, newConst, perm);  // 0x1ed44c3
```

### Considerations

Sweep 1 is the subtle one. A constant whose literal was stored in a non-descending physical order cannot simply have its layout field overwritten — that would silently relabel the bytes and corrupt the value. So the pass *moves* the data: `xla::canonicalize(Literal)` produces a literal whose physical bytes are in descending order, a fresh `kConstant` wraps it, and a `kTranspose` whose permutation is the original layout's logical→physical map restores the exact logical tensor every existing user expected. Net behavior unchanged; storage now canonical.

Sweep 2 is pure shape mutation — no graph edit. It walks the same post-order again and overwrites the layout field of every instruction's `Shape` (recursing through tuples via `clearLayout`, which for each `tuple_shapes()` element recurses on nested tuples and `SetToDefaultLayout`s array leaves). The returned `changed` boolean reflects only the sweep-1 constant rewrites, because sweep 2 never reports whether it altered anything.

> **GOTCHA —** a missing entry `ComputationLayout` is a **`LOG(FATAL)`** here (`hlo_module_config.h:132`), but the very same `[config+0x158]` flag is treated by #71 as "nothing to do, proceed with an empty layout." Same flag, opposite policy. A reimplementation that copies #71's tolerance into #23 will mask a real driver bug instead of aborting on it.

> **QUIRK —** #23 is the *only* layout pass that inserts a transpose or touches literal bytes. #71 inserts `kCopy`; #75 inserts nothing. If your reimplementation finds itself emitting a transpose anywhere but the constant-canonicalization path, it has diverged.

---

## #71 EnsureDescendingLayoutInRoot — Run @ 0x1f84f40

### Purpose

Run late (slot 71, just before the combiners) and guarantee the module's externally-visible contract: entry parameters are already descending (validated, hard error otherwise) and the entry root tuple emits every leaf in descending order (forced via `kCopy`). This is what makes the compiled module's I/O layouts match what the caller's `ComputationLayout` promised, regardless of how internal layout assignment shuffled things.

### Entry Point

```text
EnsureDescendingLayoutInRoot::Run (0x1f84f40, 2937 B, 158 bb)
  ├─ LayoutUtil::IsMonotonicWithDim0Major (0x97ca3e0)             ── Phase A: validate params
  ├─ HloModuleConfig(const&) (0x1e85dd0)                          ── Phase B: trial copy
  ├─ ComputationLayout::SetToDefaultLayout (0x96fbec0)            ── Phase B: build default
  ├─ ComputationLayout::operator== (0x96fc450)                   ── Phase B: short-circuit
  ├─ ShapeUtil::MakeValidatedShapeWithDescendingLayout (0x97e32c0)── Phase C: target shape
  ├─ Shape::Equal (0x97d73c0)
  ├─ MakeCopyHlo (0x90f0c50)                                     ── Phase C: insert kCopy
  └─ HloInstruction::ReplaceOperandWith (0x965ee20)
```

### Algorithm

```c
StatusOr<bool> EnsureDescendingLayoutInRoot_Run(HloModule* module):  // 0x1f84f40
    entry = module->entry_computation_;                          // [module+0x38]
    CHECK(entry != nullptr);                                     // 0x2108c9

    // --- Phase A: VALIDATE every entry parameter is already descending ---
    for p in entry->instructions where p->opcode() == kParameter:
        s = p->shape();
        if (!s.has_array_state()) continue;                     // no layout to check
        if (!IsMonotonicWithDim0Major(s.array_state().layout())):   // 0x1f84fd6
            return InvalidArgument("layout of parameter ", p->name(),
                                   " is not MonotonicWithDim0Major");
                // 0x247b2a "layout of parameter " · 0x2ce858 " is not MonotonicWithDim0Major"

    // --- Phase B: short-circuit if the entry ComputationLayout is already default ---
    if (module->config().entry_computation_layout_.has_value()):   // [config+0x158] != 0
        cfg  = HloModuleConfig(module->config());               // 0x1e85dd0 trial copy (no mutation)
        trial = copy of cfg.entry_computation_layout_;          // param ShapeLayouts stride 0x140 + result
        trial.SetToDefaultLayout();                             // 0x96fbec0 — all descending
        if (trial == cfg.entry_computation_layout_)             // 0x96fc450
            return Ok(false);                                   // entry layout already default — no-op
    // (absent entry layout: treat as nothing-to-do, fall through with empty layout)

    // --- Phase C: force every entry-root TUPLE leaf to descending via kCopy ---
    root = entry->root_instruction();                           // [entry+8]
    if (root->shape().element_type() != TUPLE/0x0D)             // 0x1f852e3: cmp dword [rax],0x0D
        return InvalidArgument("root of computation ", entry->name(), " is not a tuple");
                // 0x21cd78 "root of computation " · 0x2825d4 " is not a tuple"

    SwissTable<int unique_id, HloInstruction* copy> dedup;      // shared-operand dedup
    changed = false;
    for i in 0 .. root->operand_count()-1:
        op = root->mutable_operand(i);
        os = op->shape();
        if (os.element_type() == TUPLE/0x0D)                    // 0x1f855f9: cmp dword [rax],0x0D
            return InvalidArgument("root of computation ", entry->name(),
                                   " contains nested tuple");   // 0x27a804
        desc = ShapeUtil::MakeValidatedShapeWithDescendingLayout(   // 0x1f8563b
                   os.element_type(), os.dimensions());
        if (!Shape::Equal()(os, desc)):                        // 0x1f855..  not already descending
            copy = dedup[op->unique_id()];                     // reuse if same operand seen before
            if (miss) copy = MakeCopyHlo(op, desc);            // 0x1f8573e — kCopy w/ descending layout
            root->ReplaceOperandWith(i, copy);                 // 0x1f855ae
            changed = true;
        CHECK(op->unique_id() < 2147483647);                   // 0x27e834 "unique_id_ < (2147483647)"
    return Ok(changed);
```

### Considerations

#71 does **not** rewrite parameters — Phase A only *validates* them and aborts with `InvalidArgument` (status `'6'`) if any is non-descending. That is a deliberate contract split: parameters are the caller's responsibility (the driver is expected to hand in descending inputs), while outputs are the compiler's, so the root tuple is the one thing #71 actively repairs.

Phase B is a cheap escape hatch: rather than diff every root leaf, it builds a *trial* default `ComputationLayout` off a refcounted copy of the module config (no mutation of the real module), calls `SetToDefaultLayout()` on the copy, and compares. If the real entry layout already equals the all-default layout, the whole pass is a no-op and returns `false`. Only on inequality does it fall through to the per-leaf rewrite.

Phase C's insert is a **`kCopy`**, never a transpose or bitcast. `MakeValidatedShapeWithDescendingLayout(elem_type, dims)` builds the canonical target shape; `Shape::Equal` decides if a copy is even needed; the `unique_id → copy` SwissTable ensures that if the same operand feeds the root tuple twice, a single shared copy is inserted, not two. The pass swaps operands only — it does not run HloDCE, so any now-dead producers are left for a later DCE.

> **NOTE —** the two TUPLE element-type tests in Phase A/C (`0x1f852e3`, `0x1f855f9`) are 32-bit compares — `cmp dword ptr [rax], 0Dh` — against the `element_type` slot, not byte compares. The sentinel value is `0x0D`/TUPLE either way; only the operand width differs from the byte tests elsewhere in this subsystem.

> **NOTE —** despite running among the collective passes, #71 has nothing to do with collectives. It is named for *layout in root*; its sole graph edit is the root-tuple `kCopy`. Its placement late in the pipeline is so that it sees the final post-layout-assignment shapes, not collective semantics.

---

## #75 RelaxCollectivesLayoutConstraint — match 0x20025e0 / expand 0x2002640

### Purpose

Clear the `constrain_layout` flag on every layout-constrained all-reduce. `constrain_layout` is XLA's marker that a collective's operand/result layouts are *pinned* — layout assignment must not change them, and the Neuron all-reduce combiner refuses to merge them. #75 runs at slot 75, immediately before the combiners (#76 dup-remover, #77–#79 the three collective combiners), so clearing the flag is the enabling step that lets `NeuronAllReduceCombiner` (#79) fuse the all-reduces it would otherwise bail on.

This is an `xla::OpExpanderPass`: it shares `OpExpanderPass::Run` @`0x29f0bb0` and overrides only the matcher (`InstructionMatchesPattern`) and the rewriter (`ExpandInstruction`); `Run` calls `ReplaceInstruction` with whatever `ExpandInstruction` returns.

### Algorithm

```c
bool InstructionMatchesPattern(HloInstruction* inst):           // 0x20025e0 (37 B)
    if (inst->opcode() != kAllReduce/7) return false;           // cmp byte [inst+0x14], 7
    ar = Cast<HloAllReduceInstruction>(inst);                   // 0x1eeebf0
    return ar->constrain_layout;                                // movzx eax, byte [ar+0x250]
```

```c
HloInstruction* ExpandInstruction(HloInstruction* inst):        // 0x2002640 (332 B, 11 bb)
    ar   = Cast<HloAllReduceInstruction>(inst);
    comp = inst->parent();                                      // [inst+0x48]
    chan = ar->channel_id;                                      // movdqu xmm0,[ar+0x208]  (optional<long>, 16 B)
    dev  = &ar->collective_device_list;                        // lea r14,[ar+0x218]  (replica_groups)
    ugdi = ar->use_global_device_ids;                          // movzx [ar+0x251]
    red  = inst->called_computations()[0];                     // reduction computation
    shp  = inst->shape();
    ops  = { inst->operands(), inst->operand_count() };

    new_ar = HloInstruction::CreateAllReduce(                  // 0x2002700
                shp, ops, /*reduction=*/red, /*device_list=*/dev,
                /*constrain_layout=*/false,                    // <<< push 0 @0x20026f8 — THE RELAXATION
                /*channel_id=*/chan,                           // preserved
                /*use_global_device_ids=*/ugdi);               // preserved
    added = comp->AddInstruction(move(new_ar), /*name=*/"");   // 0x2002711
    added->metadata().CopyFrom(inst->metadata());              // 0x2002754  OpMetadata::CopyFrom, [..+0x200]
    return added;                                              // Run does ReplaceInstruction(inst, added)
```

The `CreateAllReduce` signature (demangled from the call target) is
`CreateAllReduce(const Shape&, Span<HloInstruction* const>, HloComputation* reduction, const CollectiveDeviceList&, bool constrain_layout, const optional<long>& channel_id, bool use_global_device_ids)`.
The literal `push 0` at `0x20026f8`, sitting in the argument-build sequence right before the stacked `channel_id`/`use_global_device_ids` operands, is the `bool constrain_layout = false`.

### Considerations

The rewrite is a verbatim clone with exactly one bit flipped. Replica groups (`CollectiveDeviceList` @+0x218), the reduction computation, `channel_id` (@+0x208), and `use_global_device_ids` (@+0x251) are all carried across unchanged, and `OpMetadata` is copied over. No copy, bitcast, or transpose is inserted — only the boolean. This matters because once `constrain_layout` is false, layout assignment is free to pick the descending layout for the all-reduce's operands and result, and #79's `ContainsLayoutConstrainedCollective` bail no longer fires.

> **GOTCHA —** despite the plural class name *Collectives*, the in-binary matcher fires on **`kAllReduce` (opcode 7) only**. The 37-byte body is a single opcode compare; there is no all-gather, reduce-scatter, all-to-all, or collective-permute branch. A reimplementation that relaxes `constrain_layout` on every collective is *over*-relaxing — only all-reduces are touched here.

### The constrain_layout / use_global_device_ids offset map

The matcher and expander pin down where layout-relevant collective fields live on `HloAllReduceInstruction`. These offsets are what disambiguate the all-reduce rebuild — and they are *not* shared verbatim with `HloAllGatherInstruction`.

| Offset | Field | Evidence |
|---|---|---|
| +0x14 | opcode (`kAllReduce` = 7) | matcher `cmp byte [inst+0x14], 7` |
| +0x18 / +0x20 | operands InlinedVector (count >> 1 / data) | expander operand span |
| +0x48 | parent computation | `[inst+0x48]` |
| +0x200 | `OpMetadata` | `OpMetadata::CopyFrom([inst+0x200])` |
| +0x208 | `channel_id` `optional<long>` (16 B) | `movdqu [ar+0x208]` |
| +0x218 | `CollectiveDeviceList` (replica_groups) | `lea r14,[ar+0x218]` |
| +0x250 | `constrain_layout` (byte) | matcher returns `[ar+0x250]` |
| +0x251 | `use_global_device_ids` (byte) | expander reads `[ar+0x251]` |

> **GOTCHA —** `HloAllReduceInstruction` and `HloAllGatherInstruction` share the `HloCollectiveInstruction` base prefix, so `constrain_layout` lands at +0x250 on **both**. But the *trailing* fields differ: on `HloAllReduceInstruction`, `use_global_device_ids` is the very next byte at **+0x251**, and `channel_id` is at +0x208. On `HloAllGatherInstruction`, `use_global_device_ids` sits at **+0x260** (the all-gather subclass interposes its `all_gather_dimension` member). Reading `use_global_device_ids` from +0x260 on an all-reduce — or +0x251 on an all-gather — reads an adjacent field and silently mis-builds the clone. The `use_global_device_ids` flag selects how replica IDs in `replica_groups` are interpreted (global device IDs vs. per-replica), so getting its offset wrong directly changes which devices a collective spans. Read it from the correct subclass offset, full stop.

---

## Synthesis — the layout model the pipeline assumes

Canonical layout = descending = major-to-minor = `minor_to_major {rank−1,…,1,0}` (predicate `IsMonotonicWithDim0Major`, producer `SetToDefaultLayout`). The three passes enforce it at three scopes, with three distinct edit kinds:

| Scope | Pass | Mechanism | Inserts |
|---|---|---|---|
| every instruction shape (entry comp) | #23 sweep 2 | in-place `SetToDefaultLayout` / `clearLayout` per shape | nothing (shape mutate) |
| every constant (entry comp) | #23 sweep 1 | reorder literal → descending; compensating transpose | new constant + **kTranspose** |
| entry root-tuple leaves | #71 Phase C | `MakeValidatedShapeWithDescendingLayout` + `MakeCopyHlo` + `ReplaceOperandWith` | **kCopy** per non-descending leaf |
| entry parameters | #71 Phase A | validate only — `InvalidArgument` if not descending | nothing (hard error) |
| all-reduce constraint flag | #75 | rebuild all-reduce with `constrain_layout=false` | nothing (clone, 1 bool) |

The pipeline's later layout-sensitive stages (Tensorizer legalization, penguin layout assignment, Walrus tiling) are written against a world where every shape is already descending. #23 establishes that world early; #71 re-asserts it on the externally-visible boundary after internal layout assignment has run; #75 is the targeted exception that *removes* a layout constraint so the collective combiners can do their work. Read together: #23 makes the assumption true, #71 keeps it true at the I/O edge, and #75 relaxes the one place where holding the constraint would block a downstream fusion.

---

## Related Components

| Component | Relationship |
|---|---|
| `NeuronAllReduceCombiner` (#79) | bails on `ContainsLayoutConstrainedCollective(kAllReduce)`; #75 clears `constrain_layout` first so it can fuse |
| Collective dup-remover / combiners (#76–#78) | run immediately after #75; the relaxed constraint enables their merges too |
| Penguin layout assignment (Part 5) | consumes the descending shapes #23/#71 establish; never re-derives non-default layouts |
| [Static I/O transpose handling](../frontend/static-io-transpose.md) (3.15) | the transpose-elimination view of the same I/O-layout canonicalization #23 performs |

## Cross-References

- [Static I/O Transpose Handling](../frontend/static-io-transpose.md) — 3.15, the input/output transpose handling that pairs with #23's literal canonicalization
- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](collective-combiners.md) — #76–#79, the combiners #75 unblocks by clearing `constrain_layout`; also carries the `ContainsLayoutConstrainedCollective` bail
- [The hlo-opt Pass Registry (the --passes Table)](pass-registry.md) — where #23/#71/#75 are registered and ordered
- Part 5 (penguin middle-end) — the layout-assignment consumer that assumes the descending shapes these passes establish; never re-derives non-default layouts
- Part 13 (distribution & collectives) — the `replica_groups` / `use_global_device_ids` model whose offsets #75 must preserve verbatim when it rebuilds an all-reduce
