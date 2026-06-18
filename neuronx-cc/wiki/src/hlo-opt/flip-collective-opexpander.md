# Flip-Collective OpExpander Family

> *All addresses on this page are virtual addresses (VMA) for `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel, `neuronxcc/starfish/bin/hlo-opt`, not stripped); they resolve directly in `disasm/` and `*_strings.json` (VMA-keyed) and via `objdump --start-address`. VA ≠ raw file offset here: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). The all-gather field offsets (+0x250/+0x258/+0x260) are pinned from the XLA `HloAllGatherInstruction` ctor/accessor and are HIGH-confidence, not re-bit-verified in this skip-decompiled binary. Other versions will differ.*

## Abstract

Six passes in the `hlo-opt` pipeline **push a value op across a collective** so the op runs on the un-gathered (or un-reduced) operand, and so that the collective lands in a canonical form. Four flip an **all-gather** upward past a consumer — `FlipAllGatherConvert` (#96), `FlipAllGatherReshape` (#97), `FlipAllGathersBinary` (#98), `NeuronFlipAllGatherDynamicSlice` (#92); two flip a **reduce-style** collective — `NeuronFlipReduceConvertAdd` (#102, reduce-scatter *or* all-reduce) and `FlipReduceScatterTranspose` (#104). All six are XLA `OpExpanderPass` subclasses: each overrides the virtual `InstructionMatchesPattern` (vtable+0x28) plus a non-virtual `ExpandInstruction`, and shares the stock driver `xla::OpExpanderPass::Run` @0x29f0bb0 (862 B), which walks every computation, tests the matcher, and on a hit calls `ExpandInstruction` then `ReplaceAllUsesWith`. None of the six carries a cost model — each is an unconditional local rewrite justified by algebraic equivalence.

The thesis of the family is **uniform keying for the combiners**. The downstream `NeuronAllReduceCombiner`/`NeuronReduceScatterCombiner`/`NeuronAllGatherCombiner` (passes #77–79, [4.5](collective-combiners.md)) fuse sibling collectives only when their *key* matches — for all-gather the key is `(opcode, element-type, domain, two bools, replica-groups, all_gather_dimension)`. A `convert(all_gather(x))` chain hides the all-gather behind a per-element op, so two such all-gathers feeding two different converts are never adjacent and never combine. Push the all-gather above the convert and now both gather the same dtype, sit in the same region, and carry the same key. The binary flip goes further: it collapses **two identical all-gathers** into the operands of one. The reduce flips serve a second purpose — they put the collective into the **preferred dtype** (`#102` widens the reduction precision) or **preferred layout** (`#104` exposes a contiguous scatter axis), again funnelling toward a canonical, combinable form.

The dynamic-slice flip (#92) is the family's outlier: its payoff is **volume reduction, not fusion**. By slicing the local operand *before* the gather and re-gathering only the surviving extent, the collective transports less data. Its guard "the all-gather dimension is not among the sliced dims" keeps the gather semantics intact.

For reimplementation, the contract is:

- The **OpExpander skeleton** every pass shares: matcher predicate + `ExpandInstruction` body + the stock `Run` walk, and the `HloAllGatherInstruction` field layout the expanders read.
- The **six match→rewrite rules**, each as annotated pseudocode naming the real symbol, the matcher predicate, and the construction callees (`MakeConvertToHlo`, `MakeReshapeHlo`, `MakeBinaryHlo`, `CreateDynamicSlice`, `MakeTransposeHlo`, `CreateAllGather`/`CreateReduceScatter`/`CreateAllReduce`).
- The **channel-id discipline**: which flips reuse the source collective's channel and which mint a fresh `hlo_query::NextChannelId`.
- The **combiner payoff**: why pushing the op across the collective makes the key uniform.

| | |
|---|---|
| **Passes** | #92 `aws_neuron_flip_all_gather_dynamic_slice`, #96 `…convert`, #97 `…reshape`, #98 `aws_neuron_flip_all_gathers_binary`, #102 `aws_neuron_flip_reduce_convert_add`, #104 `aws_neuron_flip_reduce_scatter_transpose` |
| **Base class** | `xla::OpExpanderPass` — shared `Run` @0x29f0bb0 (matcher = vtable+0x28; `ExpandInstruction` non-virtual) |
| **Source files** | `hilo/hlo_passes/flip_all_gather_unary.cc` (#96/#97/#98 @0x39f600), `…/neuron_flip_all_gather_dynamic_slice.cc` (#92 @0x2c2e38), `…/neuron_flip_reduce_convert_add.cc` (#102 @0x387b38), `…/flip_reduce_scatter_transpose.cc` (#104 @0x3dace8) |
| **AllGather fields** | `constrain_layout` byte @+0x250 · `all_gather_dimension` int64 @+0x258 (== `dimensions()[0]`) · `use_global_device_ids` byte @+0x260 (HIGH) |
| **Fresh-channel flips** | #98 (binary), #102 (reduce/convert/add) — both call `hlo_query::NextChannelId` @0x8ab1ac0 |
| **Reuse-channel flips** | #96, #97, #92, #104 — no `NextChannelId` callee |
| **Downstream consumer** | [`NeuronAllGatherCombiner`](collective-combiners.md) Run @0x1f8add0 / `CombineAllGathers` @0x1f8b750 |

---

## The OpExpander Skeleton

### Purpose

Every pass on this page is a subclass of XLA's `OpExpanderPass`, the same generic "match one instruction, build a replacement, splice it in" framework that backs the stock `GatherExpander`, `RngExpander`, `BatchNormExpander`, etc. (all present in this binary). Understanding the skeleton once means the six rewrites differ only in two functions each.

### Entry Point

```text
xla::OpExpanderPass::Run                     @0x29f0bb0  (862 B)  ── STOCK driver
  for each computation, for each instruction:
    └─ (*vtable[+0x28])(this, inst)  == InstructionMatchesPattern  ── per-pass MATCHER
       if matched:
         └─ ExpandInstruction(this, inst)                          ── per-pass REWRITE (non-virtual)
              └─ inst->ReplaceAllUsesWith(result)                  ── splice replacement
```

The matcher is the only virtual the framework calls (slot +0x28); `ExpandInstruction` is reached directly from `Run`'s match arm, so its address is resolved from the symbol table, not a vtable slot. Each pass also has a one-line `name()` at vtable+0x10 returning the registry key string.

> **NOTE —** this binary was decompile-skipped (`NVOPEN_IDA_SKIP_DECOMPILE`), so the rewrite bodies below are reconstructed from disassembly plus the verified callee/string/opcode evidence. Control flow is HIGH; the callee set, the opcode bytes, and the construction calls are CERTAIN (cross-checked against each function's `callees` list in `*_functions.json`).

### The `HloAllGatherInstruction` field layout the expanders read

All four all-gather flips read three fields off the matched (or operand) all-gather and forward them to `CreateAllGather`. The layout (pinned from the ctor that stores them and the `dimensions()` accessor returning `Span{this+0x258, len=1}`):

| Field | Offset | Type | Feeds `CreateAllGather` arg | Confidence |
|---|---|---|---|---|
| (base `HloCollectiveInstruction`: replica-groups / channel-id) | +0x208.. | — | `replica_groups`, `channel_id` | HIGH |
| `constrain_layout` | +0x250 | byte | 5th arg | HIGH |
| `all_gather_dimension` | +0x258 | int64 | `long all_gather_dimension` (== `dimensions()[0]`) | HIGH |
| `use_global_device_ids` | +0x260 | byte | 7th (trailing) arg | HIGH |

> **GOTCHA —** a naive reader eyeballing the expander disasm will call `byte[+0x260]` "the dimension" because it is the *third-from-last* byte fed to `CreateAllGather`. It is **`use_global_device_ids`**. The dimension is the **int64 at +0x258**, which `dimensions()` returns. Treating +0x260 as the dim silently scrambles every flip.

The `CreateAllGather` overload all four flips call is the `Span<ReplicaGroup>` form `HloInstruction::CreateAllGather(Shape, Span<HloInstruction*>, long dim, Span<ReplicaGroup>, bool constrain, optional<long> channel, bool global)` @0x96680b0 — confirmed as a callee of #96/#97/#98/#92 expanders.

---

## #96 — FlipAllGatherConvert

### Purpose

`convert(all_gather(x))` → `all_gather(convert(x, dst_dtype))`. The convert is per-element, so it commutes with the gather unconditionally; pushing it down both reduces the gathered volume (when `dst` is narrower) and — the real motive — *unifies the gathered dtype* so the downstream all-gather-combiner can key two siblings together.

### Match

```c
bool FlipAllGatherConvert::InstructionMatchesPattern(inst):   // 0x1f81670
    if inst->opcode() != 0x25 (kConvert):  return false       // [inst+0x14] == 0x25
    return hlo_utils::IsAllGather(inst->operand(0))            // 0x1ebe790: [op0+0x14] == 4
```

`hlo_utils::IsAllGather` @0x1ebe790 is the single-byte opcode test `[this+0x14]==4`. No further shape/dtype guard — every convert-over-all-gather matches.

### Rewrite

```c
HloInstruction* FlipAllGatherConvert::ExpandInstruction(conv):   // 0x1f82590 (412 B)
    ag  = conv->mutable_operand(0);                              // the all-gather
    x   = ag->mutable_operand(0);                                // pre-gather input
    dst = conv->shape().element_type();
    nc  = MakeConvertToHlo(x, dst, ag->metadata());              // 0x90f7ab0 — convert the INPUT
    AG  = Cast<HloAllGatherInstruction>(ag);
    g   = CreateAllGather(conv->shape(), {nc},                   // 0x96680b0
              /*dim*/       AG->dimensions()[0],                 // int64 [AG+0x258]
              /*groups*/    AG->replica_groups(),
              /*constrain*/ byte[AG+0x250],
              /*channel*/   AG->channel_id(),                    // REUSED — no NextChannelId
              /*global*/    byte[AG+0x260]);
    g->metadata().CopyFrom(conv->metadata());
    return g;                                                    // Run then ReplaceAllUsesWith(conv → g)
```

Confirmed callees: `MakeConvertToHlo` (@0x90f7ab0) and the `Span<ReplicaGroup>` `CreateAllGather` only — **no `NextChannelId`**, so the channel id is copied verbatim from the source all-gather. (CERTAIN — callee list of 0x1f82590.) The new convert is created empty-named.

---

## #97 — FlipAllGatherReshape

### Purpose

`reshape(all_gather(x))` → `all_gather(reshape(x, dims'))`. Legal only when the reshape does not *split* the all-gather dimension across multiple output dims — otherwise the new gather has no single axis to scatter along. The flip rescales the AG-dim extent back to its pre-gather local size at the mapped output dim, and re-points the new all-gather's dimension at that mapped output dim.

### Match

```c
bool FlipAllGatherReshape::InstructionMatchesPattern(inst):       // 0x1f82510
    if inst->opcode() != 0x5B (kReshape):  return false
    if !IsAllGather(inst->operand(0)):     return false
    if ReshapeOnlyMergeDims(inst):                                 // 0x1f81780 (319 B)
        return IsSafeMergeReshape(inst)                            // 0x1f823c0 (331 B)
    else:
        return AllGatherDimNotSplit(inst)                          // 0x1f82320 (130 B)
```

The two legality helpers both pivot on `GetReshapeDimMapping` @0x1f81d30 (1072 B), which builds an operand-dim → list-of-output-dims map via `xla::CommonFactors(reshape.dims, operand.dims)`:

- `ReshapeOnlyMergeDims` @0x1f81780 — true iff every CommonFactors block spans exactly one output dim (the reshape only collapses, never splits).
- `AllGatherDimNotSplit` @0x1f82320 — `GetReshapeDimMapping[ag_dim].size() == 1` (the AG dim maps to a single output dim).
- `IsSafeMergeReshape` @0x1f823c0 — `GetReshapeDimMapping[ag_dim].size()==1` **and** at most one *other* non-singleton dim is merged into the AG dim's output group.

### Rewrite

```c
HloInstruction* FlipAllGatherReshape::ExpandInstruction(rs):      // 0x1f82810 (HIGH)
    // VLOG: "Running flip all-gather reshape for instruction: "  (@0x3dacb0)
    ag       = rs->mutable_operand(0);
    AG       = Cast<HloAllGatherInstruction>(ag);
    d        = AG->dimensions()[0];                               // [AG+0x258]
    out_dim  = GetReshapeDimMapping(rs)[d][0];                    // mapped reshape output dim
    new_dims = rs->shape().dims with new_dims[out_dim] scaled
               back to the PRE-gather local extent on the AG axis;
    nr       = MakeReshapeHlo(new_dims, ag->operand(0));          // 0x90f1xx — reshape the INPUT
    g        = CreateAllGather(rs->shape(), {nr},
                  /*dim*/ out_dim,                                // NEW all-gather dimension
                  AG->replica_groups(), byte[AG+0x250],
                  AG->channel_id(),                               // REUSED
                  byte[AG+0x260]);
    g->metadata().CopyFrom(rs->metadata());
    return g;
```

Confirmed callees: `MakeReshapeHlo` and `CreateAllGather`, **no `NextChannelId`** (channel reused). (CERTAIN — callee list of 0x1f82810.) The extent-rescale arithmetic over CommonFactors groups is summarised, not enumerated dim-by-dim (skip-decompiled — see Gaps).

> **QUIRK —** the new all-gather's dimension is **not** the original `all_gather_dimension`; it is `GetReshapeDimMapping[old_dim][0]`, the position the AG axis lands at after the reshape. Carry the dim through the dim-map or the rewrite gathers along the wrong axis.

---

## #98 — FlipAllGathersBinary

### Purpose

`binary(all_gather(a), all_gather(b))` → `all_gather(binary(a, b))`. This is the one flip with an **immediate 2→1 fusion**: it requires both operands to be all-gathers *and* the two to be identical, so it folds two collectives into one in a single rewrite — the most direct payoff in the family.

### Match

```c
bool FlipAllGathersBinary::InstructionMatchesPattern(inst):       // 0x1f83370
    if !IsElementWiseBinary(inst):  return false                  // 0x1ec7430
    ag0 = inst->operand(0);  ag1 = inst->operand(1);
    if !IsAllGather(ag0) || !IsAllGather(ag1):  return false      // BOTH must be all-gathers
    return ag0->IdenticalInternal(ag1, …, /*flags*/ 1,1,0,1)      // same shape/dim/groups/channel/dtype
```

`IsElementWiseBinary` @0x1ec7430 is a `flat_hash_set<HloOpcode>` membership test over the nine elementwise binaries — `add`(0x01), `and`(0x0B), `divide`(0x2C), `maximum`(0x43), `minimum`(0x44), `multiply`(0x45), `or`(0x49), `remainder`(0x59), `subtract`(0x72). (HIGH — opcode set from the set's init immediates; the function's `constants_used` shows `1` and `114`=0x72 directly.)

### Rewrite

```c
HloInstruction* FlipAllGathersBinary::ExpandInstruction(bin):     // 0x1f83480 (541 B)
    a   = bin->operand(0)->operand(0);                            // input of AG0
    b   = bin->operand(1)->operand(0);                            // input of AG1
    nb  = MakeBinaryHlo(bin->opcode(), a, b, bin->metadata(), nullptr);  // 0x… MakeBinaryHlo
    AG0 = Cast<HloAllGatherInstruction>(bin->operand(0));
    g   = CreateAllGather(bin->shape(), {nb}, AG0->dimensions()[0],
              AG0->replica_groups(), byte[AG0+0x250],
              /*channel*/ hlo_query::NextChannelId(*bin->GetModule()),  // FRESH id  (0x8ab1ac0)
              byte[AG0+0x260]);
    g->metadata().CopyFrom(bin->metadata());
    return g;
```

Confirmed callees: `MakeBinaryHlo`, **`hlo_query::NextChannelId` @0x8ab1ac0**, and `CreateAllGather`. (CERTAIN — callee list of 0x1f83480.)

> **QUIRK —** this is the only all-gather flip that **mints a fresh channel id**. The combined collective is a brand-new operation that subsumes two distinct source channels, so reusing either source id would alias a channel; `NextChannelId` allocates a unique one. The convert/reshape/dynamic-slice flips keep one all-gather (one channel) and reuse it.

---

## #92 — NeuronFlipAllGatherDynamicSlice

### Purpose

`dynamic_slice(all_gather(x), idx…)` → `all_gather(dynamic_slice(x, idx…, sizes'))`. Here the goal inverts the rest of the family: not fusion but **moving less data**. Slice the un-gathered operand first; the all-gather then transports only the surviving extent. The AG dimension is *preserved* at full local size (re-gathered), while the dims the DS reduces are cut before the collective.

### Match

```c
bool NeuronFlipAllGatherDynamicSlice::InstructionMatchesPattern(inst):  // 0x1fa7c80
    // VLOG: "Found pattern: all-gather -> dynamic slice" / "… All Gather -> Dynamic Slice"
    if inst->opcode() != 0x30 (kDynamicSlice):  return false
    if !IsAllGather(inst->operand(0)):          return false
    if inst->shape().array_state()[0] <= 5:     return false     // rank guard ≈ rank ≥ 3 (MED)
    sliced = find_slice_dimensions(inst);                        // 0x1fa7a30
    if sliced.size() <= 1:                       return false    // need >1 sliced dim
    if ag_dim ∈ sliced:                          return false    // AG dim must be UN-sliced
    return true
```

`find_slice_dimensions` @0x1fa7a30 = `{ i : ds.slice_sizes(i) != ds.operand(0).shape.dim(i) }` — the dims the DS actually cuts. The all-gather dimension must be a full (un-cut) dim, or moving the slice below the gather would change semantics.

### Rewrite

```c
HloInstruction* NeuronFlipAllGatherDynamicSlice::ExpandInstruction(ds):  // 0x1fa81a0 (2396 B)
    // VLOGs: "Entering ExpandInstruction" · "old all-gather " · "old dynamic-slice "
    AG  = Cast<HloAllGatherInstruction>(ds->operand(0));
    d   = AG->dimensions()[0];                                   // all_gather_dimension
    loc = ShapeUtil::GetDimension(AG->operand(0)->shape(), d);   // pre-gather LOCAL size on d
    ns  = ds->shape();          ns.set_dimensions(d, loc);       // DS no longer cuts dim d
    sizes = ds->dynamic_slice_sizes();  sizes[d] = loc;          // slice the AG dim at full local extent
    nds = CreateDynamicSlice(ns, AG->operand(0), ds->index_operands(), sizes);  // slice the INPUT
    // VLOG: "Created new dynamic-slice "  (@0x220cb7)
    g   = CreateAllGather(ds->shape(), {nds}, d, AG->replica_groups(),
              byte[AG+0x250], AG->channel_id(),                  // REUSED
              byte[AG+0x260]);
    // VLOG: "Created new all-gather "  (@0x22c7fa)
    ds->ReplaceAllUsesWith(g);
    return g;
```

Confirmed callees: `CreateDynamicSlice`, `CreateAllGather`, and `HloInstruction::ReplaceAllUsesWith` — **no `NextChannelId`** (channel reused). (CERTAIN — callee list of 0x1fa81a0.)

> **NOTE —** the rank guard reads `inst->shape().array_state()[0] > 5`, where `array_state()[0]` is the tagged dims header `(size<<1)|inline_flag`. `>5` is roughly "rank ≥ 3"; the exact rank threshold is MED (the inline-flag bit muddies the decode).

---

## #102 — NeuronFlipReduceConvertAdd

### Purpose

The optimizer/gradient-accumulation idiom `add( convert_wide( RS|AR_narrow(grad) ), gte(parameter) )` → `add( RS|AR_wide( convert_wide(grad) ), gte(parameter) )`. The convert is **flipped before** the collective so the cross-device reduction runs in the wider accumulate precision (less round-off), and the collective output is already the accumulator dtype (the trailing `add` needs no separate convert). Handles **both** reduce-scatter and all-reduce.

### Match

```c
bool NeuronFlipReduceConvertAdd::InstructionMatchesPattern(inst):  // 0x1fa8c50 (355 B)
    // VLOG: "Found pattern: reduce-scatter/all-reduce, convert, add, with
    //        get-tuple-element on parameter tuple"  (@0x3e69d0)
    if inst->opcode() != 0x01 (kAdd):  return false
    convert = first add-operand with opcode 0x25 (convert);       require non-null
    gte     = first add-operand with opcode 0x3A (get-tuple-element); require non-null
    rop = convert->operand(0)->opcode();
    if rop != 0x57 (reduce-scatter) && rop != 0x07 (all-reduce):  return false
    if gte->operand(0)->opcode() != 0x4C (parameter):  return false
    return true
```

The accumulator side must be a GTE off a `parameter` tuple — the loop-carried optimizer state. This restriction is what makes the up-cast numerically sound: the reduction is a `kAdd`-tree into a wider float, which commutes with the up-cast.

### Rewrite

```c
HloInstruction* NeuronFlipReduceConvertAdd::ExpandInstruction(add):  // 0x1fa8ff0 (3841 B)
    // .cold fragment at 0x1fa8db4 — the real body is 0x1fa8ff0
    convert = the convert operand;  other = the gte accumulator;
    reduce  = convert->operand(0);                                // RS or AR
    WIDE    = convert->shape().element_type();                    // wide accumulate dtype

    new_convert = CreateConvert(                                  // 0x… CreateConvert — up-cast the INPUT
        ShapeUtil::ChangeElementType(reduce->operand(0)->shape(), WIDE),
        reduce->operand(0));

    if reduce->opcode() == 0x07 (all-reduce):
        ar = Cast<HloAllReduceInstruction>(reduce);
        new_reduce = CreateAllReduce(                            // output now WIDE
            ShapeUtil::ChangeElementType(ar->shape(), WIDE),
            {new_convert}, ar->called_computations()[0],
            ar->replica_groups(), byte[ar+0x251] /*constrain*/,
            /*channel*/ NextChannelId(module),                   // FRESH
            byte[ar+0x250] /*global*/);
        // VLOG: "Created new_reduce_op (AllReduce): "
    else:  // 0x57 reduce-scatter
        rs = Cast<HloReduceScatterInstruction>(reduce);
        new_reduce = CreateReduceScatter(
            ShapeUtil::ChangeElementType(rs->shape(), WIDE),
            {new_convert}, rs->called_computations()[0],
            rs->replica_groups(), byte[rs+0x251],
            /*channel*/ NextChannelId(module),                   // FRESH
            byte[rs+0x250], /*scatter_dim*/ int64[rs+0x258]);    // scatter dim PRESERVED
        // VLOG: "Created new_reduce_op (ReduceScatter): "

    new_add = CreateBinary(add->shape(), kAdd /*1*/, new_reduce, other);
    return new_add;
```

Confirmed callees: `CreateConvert`, **`CreateAllReduce` AND `CreateReduceScatter`** (both branches present), `ShapeUtil::ChangeElementType`, `CreateBinary`, and `hlo_query::NextChannelId`. (CERTAIN — callee list of 0x1fa8ff0.) The reduction sub-computation (`called_computations()[0]`), replica-groups, constrain-layout, use-global-device-ids, and (RS) scatter-dimension are copied verbatim; only the element dtype widens and the channel id is fresh.

> **CORRECTION (FLIP-1) —** earlier summaries cited `NeuronFlipReduceConvertAdd::ExpandInstruction @0x1fa8db4`. That address is the **`.cold`** clone (30 B); the real body is **@0x1fa8ff0** (3841 B). The matcher (OpExpander entry, vtable+0x28) is **@0x1fa8c50**.

> **NOTE —** this pass's vtable is `_ZTVN3xla4hilo26NeuronFlipReduceConvertAddE` @0x411bc0, with `_ZTI…` @0x411ba8 and `_ZTS…` @0x411b80 immediately preceding (binary `names.json`). The #104 vtable @0x4112e0 and the #92 vtable @0x411b08 are distinct objects.

---

## #104 — FlipReduceScatterTranspose

### Purpose

`transpose( [convert]? reduce_scatter(X) )` → `reduce_scatter_{d'}( transpose( [convert(X)]? ) )`, where the scatter dimension is remapped through the permutation: `d' = perm⁻¹(d)`. The transpose is sunk below the collective so the reduce-scatter operates on the transposed (and optionally up-cast) layout, exposing a contiguous scatter axis and letting the transpose fuse into the RS producer idiom.

### Match

```c
bool FlipReduceScatterTranspose::InstructionMatchesPattern(inst):  // 0x1f838f0 (999 B)
    if inst->opcode() != 0x76 (kTranspose):  return false
    op = inst->operand(0);
    if op->opcode() == 0x25 (convert):  op = op->operand(0);       // optional convert
    if op->opcode() != 0x57 (reduce-scatter):  return false
    if reduce_scatter->operand_count() != 1:  return false         // SINGLE-operand only
        // else REJECT: "Reject Reduce Scatter (Convert) Transpose Pair because Reduce Scatter "
        //              "has operand count " <N> " more than 1 supported by this pass"
    // producer-shape guard:
    src = the reduce-scatter's data source;
    if src->opcode() == 0x5B (reshape):
        require src->operand(0)->opcode() == 0x31 (dynamic-update-slice)
    elif src->opcode() == 0x01 (add):
        require (add.op0 or add.op1) is 0x3A (gte) whose operand(0) is 0x4C (parameter)
    return matched
    // VLOG on success: "Found Reduce Scatter (Convert) Transpose Pair: "  (@0x2fbc48)
```

The pass is gated to **single-operand** reduce-scatters fed by one of two producer idioms — `reshape(dynamic-update-slice(…))` or `add(gte(parameter), …)`. Variadic reduce-scatters are explicitly rejected (string @0x2c2d20).

### Rewrite

```c
HloInstruction* FlipReduceScatterTranspose::ExpandInstruction(t):  // 0x1f83e50 (1591 B)
    op   = t->operand(0);
    if op->opcode() == 0x25 (convert):                            // convert branch
        rs  = Cast<HloReduceScatterInstruction>(op->operand(0));
        src = CreateConvert(                                      // up-cast first
                ShapeUtil::ChangeElementType(rs->operand(0)->shape(), op->element_type()),
                rs->operand(0));
    else:                                                          // plain branch
        rs  = Cast<HloReduceScatterInstruction>(op);
        src = rs->operand(0);

    perm = t->dimensions();                                       // permutation array
    new_transpose = MakeTransposeHlo(src, perm);                  // 0x90f1590 — transpose the RS input

    old_d = int64[rs+0x258];                                      // scatter_dimension
    new_d = (i such that perm[i] == old_d);                       // = perm⁻¹(old_d)
    // VLOG: "New scatter dim is " <new_d>  (@0x210a0b)
    if no such i:  FATAL "Failed to find new scatter dim in pass FlipReduceScatterTranspose"  (@0x333fc0)

    new_rs = CreateReduceScatter(
        t->shape(),                                              // output == original transpose shape
        {new_transpose}, rs->called_computations()[0],
        rs->replica_groups(), byte[rs+0x250],
        rs->channel_id(),                                        // REUSED — no NextChannelId
        use_global_device_ids, /*scatter_dim*/ new_d);
    return new_rs;
```

Confirmed callees: `CreateConvert`, **`MakeTransposeHlo` @0x90f1590**, `ShapeUtil::ChangeElementType`, and `CreateReduceScatter` — and **no `NextChannelId`** (channel reused). (CERTAIN — callee list of 0x1f83e50.) The output shape is pinned to the original transpose's shape, so downstream users see an identically-shaped value.

> **GOTCHA —** the scatter-dim remap is a *hard FATAL*, not a silent skip. If the permutation does not contain the old scatter dim (impossible for a well-formed transpose, but a reimplementation bug could produce it) the pass aborts with `"FATAL: Failed to find new scatter dim …"` @0x333fc0. Do not swallow the not-found case — XLA does not.

---

## Combiner Payoff — Why the Reorder Helps

### The uniform-keying argument

The four all-gather flips all feed [`NeuronAllGatherCombiner`](collective-combiners.md) (#77, Run @0x1f8add0; core `NeuronCombiner::CombineAllGathers` @0x1f8b750, 4257 B). The combiner groups all-gathers by a **key tuple** — `(opcode, element-type, domain, constrain-layout, use-global-device-ids, replica-groups, all_gather_dimension)` — and fuses a group until the count (default 256) or byte (default 1 GiB) threshold. Two all-gathers combine **only when their keys match**.

The flips manufacture matching keys:

- **Convert flip** (#96) — unifies the **element-type** axis. Two all-gathers feeding two converts to the same dtype have, pre-flip, possibly different gathered dtypes; gathering *after* the convert makes both gather the destination dtype, matching the dtype component of the key.
- **Reshape flip** (#97) — pulls the all-gather above the reshape into the consumer's region, so siblings carry the **same `all_gather_dimension` and replica-groups** — exactly the key's discriminators.
- **Binary flip** (#98) — the most direct: it collapses **two `IdenticalInternal` all-gathers into the operands of one**, a 2→1 fusion in the rewrite itself, with a fresh `NextChannelId` so the merged collective owns a unique channel.
- **Dynamic-slice flip** (#92) — the exception: its payoff is **volume**, not key uniformity. Slicing before gathering shrinks the transported tensor; the AG dim is preserved so the gather is unchanged.

### The reduce flips' payoff

`#102` and `#104` canonicalize the *collective's own form*: `#102` widens the reduction to the accumulate dtype (precision plus key-dtype uniformity for the reduce-scatter/all-reduce combiners), and `#104` rotates the layout so the scatter axis is contiguous and the producer can fuse. Both are precondition shaping for later combining and lowering, not cost-modelled transforms.

> **NOTE —** order matters: the convert/reshape/binary trio is registered as a block (#96→#97→#98) and feeds a *later* combine opportunity, while the dynamic-slice flip (#92) runs earlier. The exact executed subset/order is driver-supplied (`HLOToTensorizer`/Python), not traced from this binary — the registry order is a proxy (MED).

---

## Pass Map

| # | name() key | Class | name()@ | Matcher@ | Expand@ | vtable@ | Channel | Conf |
|---|---|---|---|---|---|---|---|---|
| 96 | `aws_neuron_flip_all_gather_convert` | `FlipAllGatherConvert` | 0x1f81650 | 0x1f81670 | 0x1f82590 | 0x411140 | reuse | CERTAIN |
| 97 | `aws_neuron_flip_all_gather_reshape` | `FlipAllGatherReshape` | 0x1f81660 | 0x1f82510 | 0x1f82810 | 0x4111a0 | reuse | HIGH |
| 98 | `aws_neuron_flip_all_gathers_binary` | `FlipAllGathersBinary` | 0x1f83210 | 0x1f83370 | 0x1f83480 | 0x411240 | **fresh** | CERTAIN |
| 92 | `aws_neuron_flip_all_gather_dynamic_slice` | `NeuronFlipAllGatherDynamicSlice` | 0x1fa7990 | 0x1fa7c80 | 0x1fa81a0 | 0x411b08 | reuse | HIGH |
| 102 | `aws_neuron_flip_reduce_convert_add` | `NeuronFlipReduceConvertAdd` | 0x1fa8b00 | 0x1fa8c50 | 0x1fa8ff0 | 0x411bc0 | **fresh** | CERTAIN |
| 104 | `aws_neuron_flip_reduce_scatter_transpose` | `FlipReduceScatterTranspose` | 0x1f836a0 | 0x1f838f0 | 0x1f83e50 | 0x4112e0 | reuse | CERTAIN |

> **NOTE —** the `name()` accessor and the vtable are distinct objects: e.g. for #102, `name()` @0x1fa8b00 is a `.text` function (11 B, returns the StringRef), the vtable @0x411bc0 is `.rodata` (preceded by `_ZTI…` @0x411ba8, `_ZTS…` @0x411b80). All six vtables are distinct addresses (0x411140 / 0x4111a0 / 0x411240 / 0x411b08 / 0x411bc0 / 0x4112e0) — re-resolve each by its mangled `_ZTV…` symbol, not by eyeballing nearby `.rodata`. (CERTAIN — `names.json`.)

---

## Verbatim String Evidence

```text
name() keys / source files:
  0x370598  "aws_neuron_flip_all_gather_convert"
  0x3705c0  "aws_neuron_flip_all_gather_reshape"
  0x28a728  "aws_neuron_flip_all_gathers_binary"
  0x2abfa0  "aws_neuron_flip_all_gather_dynamic_slice"
  0x3581d8  "aws_neuron_flip_reduce_convert_add"
  0x3c2680  "aws_neuron_flip_reduce_scatter_transpose"
  0x39f600  "hilo/hlo_passes/flip_all_gather_unary.cc"            (#96/#97/#98)
  0x2c2e38  "hilo/hlo_passes/neuron_flip_all_gather_dynamic_slice.cc"
  0x387b38  "hilo/hlo_passes/neuron_flip_reduce_convert_add.cc"
  0x3dace8  "hilo/hlo_passes/flip_reduce_scatter_transpose.cc"
flip logs:
  0x3dacb0  "Running flip all-gather reshape for instruction: "
  0x2c2e70  "Found pattern: All Gather -> Dynamic Slice"
  0x2c2ea0  "Found pattern: all-gather -> dynamic slice"
  0x220cb7  "Created new dynamic-slice "      0x22c7fa  "Created new all-gather "
  0x3e69d0  "Found pattern: reduce-scatter/all-reduce, convert, add, with get-tuple-element on parameter tuple"
  0x2fbc48  "Found Reduce Scatter (Convert) Transpose Pair: "
  0x2c2d20  "Reject Reduce Scatter (Convert) Transpose Pair because Reduce Scatter "
  0x210a0b  "New scatter dim is "
  0x333fc0  "FATAL: Failed to find new scatter dim in pass "
combiner stop conditions (downstream):
  0x2b8118  "Combined count threshold reached."
```

---

## Gaps & Confidence

- **Skip-decompiled binary** — all bodies are disassembly-reconstructed (`NVOPEN_IDA_SKIP_DECOMPILE`). The callee set, opcode bytes, and construction calls are CERTAIN (verified against each function's `callees` list); exact control flow inside the reshape extent-rescale and the #104 convert-branch operand order are HIGH.
- **AllGather field offsets** (+0x250/+0x258/+0x260) and the RS/AR `scatter_dimension`/`constrain_layout`/`use_global_device_ids` offsets (+0x258/+0x251/+0x250) are pinned from the XLA ctor/accessor and are consistent across #79/#99/#102/#104, but not independently re-bit-verified in *this* binary (HIGH).
- **All six vtables are distinct and verified** in `names.json`: #96 @0x411140, #97 @0x4111a0, #98 @0x411240, #104 @0x4112e0, #92 @0x411b08, #102 @0x411bc0 (CERTAIN). An earlier draft transiently conflated #92 and #102 at 0x411b08; that is wrong — #102 is 0x411bc0.
- **Run/registration order** — the executed pass subset is driver-supplied, not traced; the registry order is a proxy (MED).
- **#92 rank guard** `array_state()[0] > 5` decodes to ≈ rank ≥ 3 but the tagged inline-flag bit leaves the exact rank threshold MED.

---

## Cross-References

- [AllReduce / ReduceScatter / AllGather Combiners & Threshold Model](collective-combiners.md) — passes #77–79, the downstream fuser these flips feed; the key-tuple and threshold model that the uniform-keying payoff targets.
- [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) — `hlo_query::NextChannelId` and channel-id assignment, the discipline #98/#102 invoke for the merged collective.
- [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) — how the resulting collectives are lowered after combining.
- [The hlo-opt Pass Registry](pass-registry.md) — registry rows #92/#96/#97/#98/#102/#104 and the `OpExpanderPass` dispatch.
