# CC-Op Decompose & Legalize Family

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`. Other versions and ABI tags will differ. VA == file-offset for `.text`/`.rodata`.*

## Abstract

Three sibling HLO passes in the `xla::hilo` pipeline take the *native* collective op-set — `kAllGather` (opcode 4), `kAllReduce` (7), `kReduceScatter` (0x57), and, for the CPU path, `kAllToAll` (0x0A), `kCollectiveBroadcast` (0x1C), `kCollectivePermute` (0x1D) — and rewrite each into a form a specific back-end's collective runtime will accept. They run *after* the forward conversion ([`ConvertCollectivesToCustomCall`](collectives-to-customcall.md), pass #11) has already established the native collective shapes, and they sit on the road to the Penguin hand-off where the IR stops being XLA HLO and becomes Neuron's penguin.ir. Despite the shared name stem, the three do unrelated work and a reimplementer should not treat them as three lowerings of one transform.

`DecomposeCCOps` (#26, `Run` @ `0x1e9f650`) is back-end-agnostic structural canonicalisation: a variadic (tuple-shaped) collective is split into one single-buffer collective clone per tuple element, and the per-element results are re-packed into a tuple named `cc-tuple`. `LegalizeCpuCCOps` (#12, `Run` @ `0x1ef2e90`) is the CPU normalizer: it *erases* channel ids and clears the global-device-id / layout flag bits so the CPU collective runtime sees plain replica-group collectives. `LegalizeCCOpsForTensorizer` (#50, `Run` @ `0x1ef12e0`) is the Tensorizer (Neuron device) normalizer: it detects collectives whose operand buffers carry mixed element types and *up-casts* every operand to the widest common dtype, rebuilds the collective at that width, then down-casts each result back to its original type with `convert` + `get-tuple-element` + `tuple`.

The page reconstructs each pass as annotated pseudocode against its real `Run` symbol, gives the two private helpers behind the Tensorizer up-cast (`CreateNewCCOp` @ `0x1eef970`, `GenerateNewGTEs` @ `0x1eeefe0`), and closes with a CPU-vs-Tensorizer divergence table and corrections to an earlier draft that conflated the three.

For reimplementation, the contract is:

- The shared opcode prefilter — an inline `setz/or` filter on `*(u8*)(inst+0x14)`, **not** a `CHECK`, present in all three `Run` bodies.
- `DecomposeCCOps`: the per-element `CloneWithNewOperands` → `CreateTuple` → `ReplaceInstruction` walk and the `cc-tuple` naming scheme.
- `LegalizeCpuCCOps`: the per-opcode flag/channel-id scrub (offsets `+0x210`, `+0x251`, `+0x260`), with `set_channel_id(nullopt)` as an *erase*.
- `LegalizeCCOpsForTensorizer`: the operand-dtype-mismatch trigger, the widest-common-width promotion rule (`WidthForType<kBitWidths>`), and the up-cast/rebuild/down-cast/re-tuple wrapper.

| | |
|---|---|
| **DecomposeCCOps::Run** | `0x1e9f650` (3182 B; cold @ `0x1e9f4f0`) — pass #26 |
| **LegalizeCpuCCOps::Run** | `0x1ef2e90` (513 B; cold @ `0x1ef3050`) — pass #12 |
| **LegalizeCCOpsForTensorizer::Run** | `0x1ef12e0` (4464 B) — pass #50 |
| **Up-cast helpers** | `CreateNewCCOp` @ `0x1eef970`, `GenerateNewGTEs` @ `0x1eeefe0` |
| **Token gate** | `xla::hilo::HasTokenEqualsTo` @ `0x1ec3530` (499 B) |
| **IR level** | XLA HLO (`xla::hilo`), native collective op-set, before Penguin hand-off |
| **Source units (`.rodata`)** | `hilo/hlo_passes/DecomposeCCOps.cc`, `hilo/hlo_passes/LegalizeCCOpsForTensorizer.cc` |

---

## The Shared Opcode Prefilter

All three passes select instructions with the same inline test, read straight off the `HloInstruction` opcode byte. It is a *filter*, not an assertion:

```c
// inline in every Run body; op read from [inst+0x14]
u8 op = *(u8*)(inst + 0x14);
bool is_native_cc = (op == 4)      // all-gather
                  | (op == 7)      // all-reduce
                  | (op == 0x57);  // reduce-scatter
// LegalizeCpuCCOps additionally matches 0x0A / 0x1C / 0x1D below
```

Opcode numbering is the alphabetical XLA `HloOpcode` enum (`xla::HloOpcodeString`); the values used on this page:

| op | hex | name | | op | hex | name |
|----|-----|------|-|----|-----|------|
| 4 | 0x04 | all-gather | | 0x1C | 28 | collective-broadcast |
| 7 | 0x07 | all-reduce | | 0x1D | 29 | collective-permute |
| 0x0A | 10 | all-to-all | | 0x25 | 37 | convert |
| 0x57 | 87 | reduce-scatter | | 0x3A | 58 | get-tuple-element |
| 0x78 | 120 | tuple | | 0x0D | 13 | tuple (element-type tag) |

> **CORRECTION (D-B13) —** an earlier draft (S2-02 §4.6) printed a `CHECK` reading `Check failed: ccop->opcode()==kAllReduce || kAllGather || kReduceScatter`. That string does not exist in the binary. The opcode triple is the inline `setz/or` filter above, never a `CHECK`. The only hard assertion in all three cold paths is `"nullptr != entry_computation_"` (`./xla/hlo/ir/hlo_module.h`:0xA4) — the module must have an entry computation. (CERTAIN — string present in `.rodata`, no opcode-assert string present.)

`HloInstruction` layout facts used by all three (read directly from offset accesses):

| Field | Offset | Meaning | Confidence |
|---|---|---|---|
| opcode | `+0x14` | u8 opcode | CERTAIN |
| operand count | `+0x18` | packed qword; real count = `qword >> 1` | CERTAIN |
| metadata | `+0x200` | `OpMetadata*` | HIGH |
| AG/channel flag | `+0x210` | u8 `use_global_device_ids` (HloChannelInstruction / HloAllGatherInstruction) | MEDIUM (inferred from class hierarchy) |
| AR flag | `+0x251` | u8 constrain_layout / global-device-id family (HloAllReduceInstructionBase) | MEDIUM |
| AG flag | `+0x260` | u8 flag cleared by the CPU pass (HloAllGatherInstruction) | MEDIUM |
| name | `+0x1D0`/`+0x1D8` | `std::string` `{ptr,len}` | HIGH |

---

## DecomposeCCOps — Variadic Tuple Split (#26)

### Purpose

Split a **variadic** (tuple-shaped) native collective — one whose output `Shape` is a `tuple{S0, S1, …}` over several buffers — into one single-buffer collective per tuple element, then re-pack the per-element results into a tuple. Downstream back-ends model only single-buffer collectives, so this canonicalises an N-ary collective into N unary collectives. It is *not* a CC-to-something lowering and it does **not** look at the back-end; it feeds both the CPU and Tensorizer normalizers.

### Entry Point

```text
xla::DecomposeCCOps::Run                     0x1e9f650 (3182 B)  ── two-phase collect/process
  ├─ HloComputation::MakeInstructionPostOrder           ── worklist source
  ├─ HloInstruction::CloneWithNewOperands               ── per-element narrowed clone
  ├─ HloInstruction::CreateTuple                        ── re-pack
  ├─ HloComputation::ReplaceInstruction                 ── splice + erase old cc
  └─ DecomposeCCOps::Run.cold              0x1e9f4f0     ── "nullptr != entry_computation_" CHECK
```

### Algorithm

```c
StatusOr<bool> DecomposeCCOps_Run(HloModule* module):    // 0x1e9f650
    HloComputation* comp = module->entry_computation();  // [module+0x38]; CHECK non-null (cold 0x1e9f4f0)
    bool changed = false;

    // --- Phase 1: collect native collectives (0x1e9f6d6 .. 0x1e9f784) ---
    vector<HloInstruction*> worklist;                    // var_280
    for (inst : comp->MakeInstructionPostOrder()):
        u8 op = inst->opcode();                          // [inst+0x14]
        if (op==4 || op==7 || op==0x57):                 // 0x1e9f72c
            worklist.push_back(inst);                    // _M_realloc_insert 0x1e9f768

    // --- Phase 2: split each tuple-shaped collective (0x1e9f7c7 .. 0x1e9fc78) ---
    uint k = 0;                                          // var_2DC pass-local tuple counter
    for (cc : worklist):
        if (cc->shape().element_type() != TUPLE/*0x0D*/): continue;  // 0x1e9f81e — skip non-variadic
        uint N = cc->shape().tuple_shapes_count();       // [cc+0x18]>>1 style bound, 0x1e9f860
        vector<HloInstruction*> elems;                   // var_260
        for (uint i = 0; i < N; ++i):
            HloInstruction* operand_i = cc->mutable_operand(i);        // 0x1e9f866
            const Shape& elt_shape   = cc->shape().tuple_shapes(i);   // 0x1e9f878
            // clone the SAME collective opcode, shape narrowed to elt_shape, 1 operand
            auto clone = cc->CloneWithNewOperands(elt_shape, Span{operand_i}, /*ctx*/0); // 0x1e9f8db
            string name = itoa(i) + "-clone";            // 0x1e9f996 to_chars; "-clone" @0x24b740, 0x1e9fa37
            HloInstruction* elem = comp->AddInstruction(move(clone), name);  // 0x1e9fbcf
            elems.push_back(elem);
        auto tuple = HloInstruction::CreateTuple(Span{elems});        // 0x1e9fc97
        string tname = "cc-tuple" + itoa(k);             // "cc-tuple" @0x20c97a, _M_replace 0x1e9fd6b
        HloInstruction* tup = comp->AddInstruction(move(tuple), tname); // 0x1e9fe1c
        Status st = comp->ReplaceInstruction(cc, tup);   // 0x1e9fe6f — splices + erases cc
        CHECK_OK(st, "computation->ReplaceInstruction(cc, tup...");    // 0x1e9fe7e
        k++; changed = true;
    return changed;
```

### cc-tuple Schema

For an input variadic collective `cc` with output `tuple{S0, …, S_{N-1}}`, the rewrite is:

```text
%<i>-clone   = <same-collective-opcode>(operand_i) : S_i        # i = 0..N-1, single-buffer
%cc-tuple<k> = tuple(%0-clone, %1-clone, …, %{N-1}-clone) : tuple{S0,…,S_{N-1}}
```

Each `-clone` inherits the full collective semantics of `cc` (replica groups, channel id, and the reduction sub-computation for AR/RS) through `CloneWithNewOperands`, with the shape narrowed to the single element `S_i` and the operand list reduced to `{operand_i}`. The re-tuple is byte-identical in shape to the original `cc` output, so every downstream `get-tuple-element` consumer is preserved transparently by `ReplaceInstruction`. Clone names are the decimal index suffixed `-clone` (`"0-clone"`, `"1-clone"`, …); the tuple name is `"cc-tuple"` plus a pass-local counter `k` incremented per replaced collective.

> **GOTCHA —** a collective whose output is *not* a tuple is collected in Phase 1 but skipped at the `element_type() != TUPLE` guard (`0x1e9f81e`) and passes through unchanged. The Phase-1 filter is by opcode only; the Phase-2 guard is what actually decides "is this variadic". A reimplementation that decomposes every collective it collects will wrongly wrap scalar collectives in a one-element tuple.

> **CORRECTION (D-B13) —** `DecomposeCCOps` does **not** perform `ReplaceOperandWith(lastArgIdx, zeroIn)` / `ReplaceAllUsesWith(operand)` / `RemoveInstruction` as an earlier draft claimed. There is no `ReplaceOperandWith` and no "zero-in" operand materialisation in this `Run` body. The `ReplaceAllUsesWith` + `RemoveInstruction` pattern belongs to `LegalizeCCOpsForTensorizer` (below); the draft conflated the two passes' op-lists. (CERTAIN — both bodies disassembled.)

---

## LegalizeCpuCCOps — The CPU Normalizer (#12)

### Purpose

Make the native collective set legal for the CPU / InferGoldens execution path by **clearing channel ids and the global-device-id / layout flag bits** so the CPU collective runtime sees plain replica-group collectives. It produces no new instructions, changes no shapes, and does not up-cast. It is an in-place mutator returning `StatusOr<bool>` where the `bool changed` accumulates in `r15b` and is written to `[ret+8]` (`0x1ef2fc7`).

### Entry Point

```text
xla::LegalizeCpuCCOps::Run                   0x1ef2e90 (513 B)   ── in-place per-opcode scrub
  ├─ HloComputation::MakeInstructionPostOrder           ── 0x1ef2ee6
  ├─ HloChannelInstruction::set_channel_id(nullopt)     ── 0x1ef2f9b / 0x1ef3036  (ERASE)
  └─ LegalizeCpuCCOps::Run.cold            0x1ef3050     ── "nullptr != entry_computation_" CHECK
```

### Algorithm

```c
StatusOr<bool> LegalizeCpuCCOps_Run(HloModule* module):     // 0x1ef2e90
    bool changed = false;
    for (inst : module->entry_computation()->MakeInstructionPostOrder()):  // 0x1ef2ee6
        u8 op = inst->opcode();
        switch (op):
          case 4: /* all-gather */                          // 0x1ef2f58
              auto* ag = Cast<HloAllGatherInstruction>(inst);
              ag->field[0x260] = 0;                          // clear AG flag (0x1ef2f20)
              if (ag->field[0x210] /*use_global_device_ids*/):
                  ag->set_channel_id(nullopt);  changed = true;   // → 0x1ef3025

          case 7:        /* all-reduce */                   // 0x1ef2f5c
          case 0x57:     /* reduce-scatter (derives HloAllReduceInstructionBase) */ // 0x1ef3040 → 0x1ef2f60
              auto* ar = Cast<HloAllReduceInstructionBase>(inst);    // 0x1ef2f63
              if (ar->field[0x251]):  ar->field[0x251] = 0;  changed = true;  // 0x1ef2f6b
              if (ar->field[0x210]):  ar->set_channel_id(nullopt);  changed = true; // 0x1ef2f80

          case 0x0A:  /* all-to-all */                      // 0x1ef3000
          case 0x1C:  /* collective-broadcast */
          case 0x1D:  /* collective-permute */
              auto* ci = Cast<HloChannelInstruction>(inst);  // 0x1ef300f
              if (ci->field[0x210]):  ci->set_channel_id(nullopt);  changed = true; // 0x1ef3025

          default: break;
    return changed;   // [ret+8]=changed, [ret+0]=OK (0x1ef2fc7)
```

The key callee is `xla::HloChannelInstruction::set_channel_id(std::optional<long> const&)` invoked with an **empty** optional (`[var_1E8]=0` set immediately before the call at `0x1ef2f9b` / `0x1ef3036`). Channel id is *erased*, not reassigned.

> **NOTE —** this is the inverse of `NeuronUniqueChannelIdEnforcer` (#82, @ `0x1fecb40`), which *assigns* fresh unique channel ids. The CPU pass deletes ids for a runtime that wants none; the enforcer creates them for one that needs them unique. Because #12 runs long before #82 in registration order, the two are ordering-sensitive — an erase here is re-populated only if the enforcer runs afterward on the same path. (MEDIUM — exact runtime pass-list interleave is driver-side, not traced here.)

There are no diagnostic strings in this body; the only `.rodata` reference is the shared `"nullptr != entry_computation_"` at the cold path.

---

## LegalizeCCOpsForTensorizer — Mixed-Dtype Up-Cast (#50)

### Purpose

The Tensorizer (Neuron device) collective HW kernel requires **all operands of a variadic collective to share one element type**. This pass detects collectives whose operand buffers have mixed dtypes, up-casts every operand to the widest common element type by inserting `convert` ops, rebuilds the native collective at that wider type, then down-casts each result back to its original type via per-element `get-tuple-element` + `convert`, re-tupling the lot. Unlike the other two it walks **all** computations of the module, not just the entry computation: the outer loop iterates `HloModule.computations()` (`[module+0x40]..[module+0x48]`, stride 0x10), the inner loop the instructions of each.

### Entry Point

```text
xla::hilo::LegalizeCCOpsForTensorizer::Run   0x1ef12e0 (4464 B)  ── all-computation up-cast
  ├─ xla::hilo::HasTokenEqualsTo             0x1ec3530 (499 B)    ── frontend-attr token gate ('1')
  ├─ CreateNewCCOp  (anon ns)               0x1eef970  (~5.6 KB)  ── up-cast engine
  │    ├─ primitive_util::WidthForType<kBitWidths>(PrimitiveType) ── widest-width pick
  │    ├─ ShapeUtil::ChangeElementType                            ── 0x1eefc36
  │    ├─ HloInstruction::CreateConvert                           ── 0x1eefc51  (up-cast in)
  │    ├─ HloInstruction::CreateAllReduce/AllGather/ReduceScatter ── 0x1ef0b57/0x1ef0cca/0x1ef0e27
  │    └─ OpMetadata::CopyFrom                                    ── 0x1eeff5c
  └─ GenerateNewGTEs (anon ns)              0x1eeefe0             ── down-cast wrapper
       ├─ HloInstruction::CreateGetTupleElement                  ── 0x1eef1cd
       ├─ HloInstruction::CreateConvert                          ── 0x1eef282  (down-cast out)
       └─ HloInstruction::CreateTuple                            ── re-tuple
```

### Algorithm

```c
StatusOr<bool> LegalizeCCOpsForTensorizer_Run(HloModule* module):  // 0x1ef12e0
    vector<HloInstruction*> worklist;                    // var_248
    bool needsUpcast = false;                            // var_2C1

    // --- Phase 1: collect mixed-dtype variadic collectives ---
    for (comp : module->computations()):                 // outer 0x1ef132e, stride 0x10
      for (inst : comp->instructions()):                 // inner 0x1ef1398
        u8 op = inst->opcode();
        if (!(op==4 || op==7 || op==0x57)) continue;     // 0x1ef13a4 prefilter
        if (operand_count_packed(inst) <= 3) continue;   // [inst+0x18] qword<=3 → too-few operands (0x1ef13ba)

        PrimitiveType elt0 = inst->operand(0)->shape().element_type();   // var_294
        // probe operand0's frontend-attribute map for token "1" (ascii '1' written @0x1ef1777)
        HasTokenEqualsTo(/*token=*/"1", inst);            // 0x1ec3530, gate @0x1ef17a6
        for (uint j = 1; j < operand_count(inst); ++j):  // 0x1ef180d
            if (inst->operand(j)->shape().element_type() != elt0):       // 0x1ef1820 mismatch
                worklist.push_back(inst);                // 0x1ef184c / 0x1ef20b8
                break;

    // --- Phase 2: rewrite each worklisted collective ---
    for (ccop : worklist):                               // 0x1ef1ce8
        DenseMap<uint,Shape>            shapeMap;         // var_210 — original per-index output shapes
        DenseMap<uint,HloInstruction*> gteMap;           // var_1F0 — produced GTE/convert nodes
        HloInstruction* newccop = CreateNewCCOp(ccop, shapeMap);            // 0x1eef970
        HloInstruction* retuple = GenerateNewGTEs(ccop, newccop, shapeMap, gteMap); // 0x1eeefe0
        Status st = ccop->ReplaceAllUsesWith(retuple, "");                  // 0x1ef1d4a
        CHECK_OK(st);
        if (comp->IsSafelyRemovable(ccop, /*ignore_ctrl_deps*/false, nullopt)): // 0x1ef1cb3
            Status st2 = comp->RemoveInstruction(ccop);                     // 0x1ef2071
            CHECK_OK(st2, "computation->RemoveInstruction(ccop)");          // 0x1ef2084
        needsUpcast = true;

    if (needsUpcast):                                    // 0x1ef1c33
        LOG(WARNING) << "We up-casted some collective-communication ops to ensure that all "
                        "operands have the same type. This may result in a performance penalty.\n";
                     // @LegalizeCCOpsForTensorizer.cc:0x113(=275), 0x1ef1c45/0x1ef1c63
    return needsUpcast;
```

> **NOTE —** the disassembly shows four near-identical `CreateNewCCOp / GenerateNewGTEs / ReplaceAllUsesWith` code copies at `0x1ef1cf0`, `0x1ef1da8`, `0x1ef1e60`, `0x1ef1f12`. These are compiler-emitted status/cleanup variants of one loop body, **not** four distinct dtype handlers — the actual dtype dispatch lives inside `CreateNewCCOp`. Do not reimplement four code paths.

### The Up-Cast Engine — CreateNewCCOp

```c
HloInstruction* CreateNewCCOp(HloInstruction* ccop, DenseMap<uint,Shape>& shapeMap):  // 0x1eef970
    // 1. widest common element-width across operands
    int wide = 0; PrimitiveType wideType;
    for (operand : ccop->operands()):
        int w = primitive_util::WidthForType<kBitWidths>(operand->shape().element_type()); // 0x1eefe00
        if (w > wide): { wide = w; wideType = operand element type; }  // CSWTCH_750 gates float/int class

    // 2. up-cast each operand to wideType, insert a convert, remember the ORIGINAL shape
    vector<HloInstruction*> wideOperands;
    for (uint i; operand_i : ccop->operands()):
        Shape ws = ShapeUtil::ChangeElementType(operand_i->shape(), wideType);  // 0x1eefc36
        auto cvt = HloInstruction::CreateConvert(ws, operand_i);                // 0x1eefc51
        comp->AddInstruction(move(cvt), "");                                    // 0x1eefc67
        wideOperands.push_back(cvt);
        shapeMap[i] = operand_i->shape();      // original element shape for the down-cast (0x1eefd23)

    // 3. rebuild the native collective at the wide type, copying metadata
    Shape newshape = MakeTupleShape(wide element shapes);
    switch (ccop->opcode()):                                                    // 0x1eeff33
      case 7:    new = CreateAllReduce(newshape, wideOperands, reduce_comp,
                          device_list, constrain_layout, channel_id, use_gdi);  // 0x1ef0b57
      case 4:    new = CreateAllGather(newshape, wideOperands, all_gather_dim,
                          device_list, constrain_layout, channel_id, use_gdi);  // 0x1ef0cca
      case 0x57: new = CreateReduceScatter(newshape, wideOperands, reduce_comp,
                          device_list, constrain_layout, channel_id, use_gdi,
                          scatter_dim);                                          // 0x1ef0e27
    OpMetadata::CopyFrom(new->metadata, ccop->metadata);                        // 0x1eeff5c
    return new;
```

### The Down-Cast Wrapper — GenerateNewGTEs

```c
HloInstruction* GenerateNewGTEs(HloInstruction* ccop, HloInstruction* newccop,
                                DenseMap<uint,Shape>& shapeMap,
                                DenseMap<uint,HloInstruction*>& gteMap):         // 0x1eeefe0
    vector<HloInstruction*> results;
    for (uint i = 0; i < tuple_count(newccop); ++i):
        auto gte = HloInstruction::CreateGetTupleElement(
                       newccop->shape().tuple_shapes(i), newccop, i);            // 0x1eef1cd
        comp->AddInstruction(move(gte), "");                                     // 0x1eef1e7
        if (shapeMap[i].element_type() != gte->shape().element_type()):  // was up-cast
            auto down = HloInstruction::CreateConvert(shapeMap[i], gte);         // 0x1eef282 — back to original
            comp->AddInstruction(move(down), "");                               // 0x1eef29c
            results.push_back(down);
        else:
            results.push_back(gte);
    return HloInstruction::CreateTuple(results);   // shape == ccop's original tuple shape
```

### Worked Rewrite

A two-buffer mixed-dtype all-reduce `{f32, bf16}` (widest common width = 32, so `wideType = f32`) becomes:

```text
%c0  = convert(op0) : f32        %c1  = convert(op1) : f32     # both up-cast to widest = f32
%ar  = all-reduce(%c0, %c1) : tuple{f32, f32}                  # rebuilt at uniform f32
%g0  = gte(%ar, 0) : f32         %g1  = gte(%ar, 1) : f32
                                 %d1  = convert(%g1) : bf16     # down-cast %g1 back; %g0 stays f32
%tup = tuple(%g0, %d1) : tuple{f32, bf16}                      # replaces original all-reduce
```

> **QUIRK —** the new collective, the convert ops, and the wrapper tuple are all created with an **empty name** (`""`), not `cc-tuple`. Only `DecomposeCCOps` mints the `cc-tuple` name. If you key any later pass off the `cc-tuple` name to recognise a decomposed/legalized collective, you will miss every Tensorizer-up-cast site.

> **GOTCHA —** the operand-count gate reads the *packed* qword at `[inst+0x18]` and compares it raw to `3` (`0x1ef13ba`). With the `>>1` unpacking convention used elsewhere this is effectively "more than one real operand", i.e. it targets variadic collectives. Whether the literal threshold is "3 packed" or exactly "1 real operand" was not byte-verified against a known instruction. (MEDIUM.)

---

## CPU vs Tensorizer Divergence

| dimension | LegalizeCpuCCOps (#12) | LegalizeCCOpsForTensorizer (#50) |
|---|---|---|
| scope | entry computation, post-order | **all** module computations, instr order |
| trigger | every AG/AR/RS/AllToAll/CB/CP | AG/AR/RS with operand_count>3 **and** mixed operand dtypes |
| transform | erase channel_id; clear flags `+0x260`/`+0x251`/`+0x210`-gated | up-cast operands to widest common dtype |
| new instrs | none (in-place mutate) | `convert(in)` ×N + native collective + `gte` ×N + `convert(out)` + `tuple` |
| dtype | unchanged | promoted to `max(WidthForType(operand_i))`, then restored |
| removes orig | no | yes (`RemoveInstruction(ccop)` after `ReplaceAllUsesWith`, guarded by `IsSafelyRemovable`) |
| diagnostic | none | `LOG(WARNING)` up-cast performance-penalty message |
| rationale | CPU collective runtime wants plain replica-group collectives without channel ids | Tensorizer collective kernel requires uniform operand element type |

Both target the **same native op-set** (4/7/0x57; the CPU pass additionally 0x0A/0x1C/0x1D), which is the only sense in which they "lower the same CC set for CPU vs Tensorizer" — but the work is opposite in kind (id/flag scrub vs dtype unification), not two lowerings of one IR-to-IR transform. `DecomposeCCOps` (#26) sits between them in registration order and is back-end-agnostic, feeding both.

> **NOTE —** in registration order #12 (CPU) runs *before* #26 (Decompose) and #50 (Tensorizer). On the Tensorizer path a variadic collective is therefore potentially up-cast at #50 *after* having been split into unary clones at #26 — so #50's `operand_count>3` gate mostly catches collectives that were still variadic before #26 ran, or that #26 left intact (non-tuple-shaped). The exact *runtime* pass-list interleave is driver-side and not traced here. (MEDIUM.)

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **`DecomposeCCOps::Run` @ `0x1e9f650` (3182 B).** *Re-check:* `functions.json` row `_ZN3xla14DecomposeCCOps3RunE…` → `addr 0x1e9f650`, `size 3182`. The cold-half `…3RunE….cold` exists separately. **CONFIRMED.**
2. **The three pass-name strings exist verbatim, and the opcode triple is a filter not a `CHECK`.** *Re-check:* `_rodata.bin` contains `decompose-cc-ops`, `legalize-cpu-cc-ops`, `legalize-ccops-for-tensorizer`, `cc-tuple`, `-clone`, and `nullptr != entry_computation_`; no opcode-assert string is present. **CONFIRMED** (filter-not-CHECK is STRONG — absence-of-string plus the inline `setz/or` disassembly).
3. **`set_channel_id` is an *erase* (empty optional), not a reassign.** *Re-check:* callee symbol `xla::HloChannelInstruction::set_channel_id(std::optional<long> const&)` exists; an empty optional is built (`[var_1E8]=0`) immediately before the calls at `0x1ef2f9b`/`0x1ef3036`. **CONFIRMED** (the empty-optional construction is STRONG from the disassembly).
4. **The up-cast helpers exist with the cited DenseMap signatures.** *Re-check:* `xla::hilo::(anon)::CreateNewCCOp(HloInstruction*, DenseMap<uint,Shape,…>&)` and `…GenerateNewGTEs(HloInstruction*, HloInstruction*, DenseMap<uint,Shape>&, DenseMap<uint,HloInstruction*>&)` both present in `functions.json`; `WidthForType<kBitWidths>(PrimitiveType)` returns `int`. Both symbols and addresses independently re-confirmed against `_names.json` (`CreateNewCCOp` @ `0x1eef970`, `GenerateNewGTEs` @ `0x1eeefe0`). **CONFIRMED.**
5. **The exact float-vs-int promotion rule.** *Re-challenge:* only that the **max bit-width** operand type is chosen (`WidthForType<kBitWidths>` + `CSWTCH_750` class gate) is observed. Whether `{s8,f16}` resolves to `f16` or to a wider int was **not** exhaustively decoded. Tagged **INFERRED / MEDIUM** in §CreateNewCCOp; do not treat the worked `{f32,bf16}→f32` example as a proof of the general rule beyond "widest wins".

Remaining gaps (honest): the `+0x251`/`+0x260`/`+0x210` flag *identities* are inferred from the `HloAllReduceInstructionBase` / `HloAllGatherInstruction` / `HloChannelInstruction` class hierarchy, not from verbatim field-name strings (MEDIUM); `HasTokenEqualsTo`'s attribute *key* string was not extracted, only that it probes for token value `"1"` (MEDIUM); the operand-count threshold packing (`3` vs `1 real`) is unverified against a concrete instruction (MEDIUM).

---

## Related Components

| Name | Relationship |
|---|---|
| [`ConvertCollectivesToCustomCall`](collectives-to-customcall.md) | Pass #11 — the *forward* native→CC conversion; runs before all three here |
| `NeuronUniqueChannelIdEnforcer` (#82, `0x1fecb40`) | Inverse of the CPU pass — *assigns* unique channel ids; ordering-sensitive with #12 |
| [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) | Sibling channel-id machinery; the CPU erase is the negative end of that spectrum |

## Cross-References

- [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) — pass #11, establishes the native collective shapes these three normalize
- [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) — pass #4 family; CPU normalizer erases what these assign
- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](collective-combiners.md) — the same native AG/AR/RS op-set, combined upstream
- [Lower Local Collectives](../distribution/lower-local-collectives.md) — Part 8/13 device-side lowering that consumes the Tensorizer-legalized, single-dtype collectives
- [Distribution & Collectives](../distribution/overview.md) — Part 13 overview of the collective stack these passes feed into before the Penguin hand-off
