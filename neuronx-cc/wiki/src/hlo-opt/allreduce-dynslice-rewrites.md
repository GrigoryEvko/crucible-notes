# AllReduce→ReduceScatter & DynamicSlice Rewrites

> *All addresses on this page are virtual addresses (VMA) for `neuronxcc/starfish/bin/hlo-opt` from the `neuron_cc 2.24.5133.0+58f8de22` wheel (cp310); resolve via `objdump --start-address` or the VMA-keyed `disasm/` sidecars. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers); `.data`-resident structs differ again. Other builds will differ.*

## Abstract

Three `xla::hilo` HLO passes in `hlo-opt` pull collectives toward the cheapest equivalent form. Two of them — `RewriteAllReduceDynamicSlice` (registry key `aws_neuron_rewrite_all_reduce_dynamic_slice`) and `RewriteAllReduceDynamicSliceMultipleGroups` (key `…_multiple_groups`) — recognise the idiom *"all-reduce a tensor, then each rank dynamic-slices out its own 1/G shard"* and fold it into a single **reduce-scatter**, which performs the reduction and the sharding in one collective instead of an all-reduce (a full all-gather of partial sums) followed by per-rank slicing. The third, `DeletePermute` (key `aws_neuron_delete_permute`), despite its name does **not** touch transposes: it strips degenerate `all-reduce` / `collective-permute` ops by replacing them with their sole data operand.

All three are `xla::OpExpanderPass` subclasses and therefore share the base `OpExpanderPass::Run` @`0x29f0bb0` (post-order walk: for each instruction, if `InstructionMatchesPattern` then `ReplaceInstruction(inst, ExpandInstruction(inst))`). A pass is fully defined by its two virtuals: the **matcher** (the gate, vtable+0x40) and the **expander** (the emitter, vtable+0x48). The interesting structure here lives almost entirely in the matchers; the two AllReduce-DynamicSlice passes are byte-identical in their emitter and differ *only* in the gate.

This page reconstructs each matcher as annotated pseudocode, names the real symbols and the replica-group equality test, and walks the shared reduce-scatter emitter operand-by-operand. `DeletePermute`'s name-versus-behaviour mismatch gets its own GOTCHA, since it is the single easiest thing to get wrong here. The AllGather/parameter dedup companion (`NeuronDuplicateParameterAllGatherRemover`) is documented separately — see [§ Cross-References](#cross-references).

For reimplementation, the contract is:

- The 1-D-shard invariant (`GetSliceInfo1D`): a dynamic-slice that differs from its source in exactly one dimension yields a `(slice_dim, slice_size)` pair, which becomes the reduce-scatter's `scatter_dimension`.
- The two matcher predicates: the single-group structural gate, and the multiple-groups gate that additionally validates the runtime offset-computation chain against expected constant-pool ramp literals.
- The shared emitter: which all-reduce attributes (reduction computation, replica-groups, `constrain_layout`, `use_global_device_ids`) are forwarded verbatim, and which (channel-id) is regenerated.
- The `DeletePermute` opcode gate and identity rewrite, plus the fact that its provable-redundancy precondition is enforced by *scheduling*, not by any in-binary guard.

| | |
|---|---|
| **Namespace** | `xla::hilo` (RTTI `_ZTVN3xla4hilo28RewriteAllReduceDynamicSliceE`, `…42…MultipleGroups…`, `…13DeletePermuteE`) |
| **Base class / shared Run** | `xla::OpExpanderPass::Run` @`0x29f0bb0` |
| **Single-group matcher / expander** | `0x2004520` (385 B) / `0x2006600` (788 B) |
| **Multi-group matcher / expander** | `0x2004ae0` (6826 B) / **`0x2006600`** (same emitter) |
| **DeletePermute matcher / expander** | `0x1f79470` (19 B) / `0x1f79490` (45 B) |
| **Reduce-scatter constructor** | `xla::HloInstruction::CreateReduceScatter` @`0x9668310` |
| **1-D shard helper** | `hlo_utils::GetSliceInfo1D` @`0x1ebf010` → core @`0x1ebeec0` |
| **Source unit** | `hilo/hlo_passes/rewrite_all_reduce_dynamic_slice.cc` (both AR passes) |
| **IR level** | XLA HLO (stock opcodes), pre-Penguin |

> **NOTE —** opcode bytes on this page are decoded against the authoritative `xla::HloOpcodeString` switch @`0x96bb550` (123 cases): `7=all-reduce`, `0x1D(29)=collective-permute`, `0x24(36)=constant`, `0x25(37)=convert`, `0x30(48)=dynamic-slice`, `0x45(69)=multiply`, `0x4D(77)=partition-id`, `0x5A(90)=replica-id`, `0x5B(91)=reshape`. All CERTAIN — read straight from the switch.

---

## The 1-D Shard Invariant — `GetSliceInfo1D`

### Purpose

Both AllReduce-DynamicSlice passes hinge on one structural fact: the consuming `dynamic-slice` must carve the all-reduce result along **exactly one** dimension, producing a per-rank shard. `GetSliceInfo1D` is the predicate that proves this and extracts the scatter dimension and per-shard extent.

### Algorithm

```c
// hlo_utils::GetSliceInfo1D(HloDynamicSliceInstruction* ds)   @0x1ebf010 (98 B)
struct SliceInfo1D { bool valid; long slice_dim; long slice_size; };  // 24-byte POD {+0,+8,+0x10}

SliceInfo1D GetSliceInfo1D(HloDynamicSliceInstruction* ds):
    src_dims = ds->operand(0)->shape().dims();      // Span<long> — pre-slice extents
    slice_sz = Span{ ds+0x208, ds+0x210 };          // ds->dynamic_slice_sizes(), 8-byte stride
    return GetSliceInfo1D(src_dims, slice_sz);       // core @0x1ebeec0

// (anon)::GetSliceInfo1D(Span<long> a, Span<long> b)   @0x1ebeec0 (321 B)
SliceInfo1D GetSliceInfo1D(Span a, Span b):
    require a.size() == b.size();
    diff = [ i for i in range(a.size()) if a[i] != b[i] ];   // dims where slice shrinks the extent
    if len(diff) == 1:
        d = diff[0];
        return { valid=1, slice_dim=d, slice_size=b[d] };    // shard along d, extent b[d]
    return { valid=0, 0, 0 };                                // 0 or >1 differing dims → reject
```

`slice_dim` is the reduce-scatter `scatter_dimension`; `slice_size` is the per-rank extent in that dim. The sharding identity that both matchers later enforce is `group_size * slice_size == full_extent_on_slice_dim`. (CERTAIN — both helpers are named symbols; the field offsets `ds+0x208/+0x210` for `dynamic_slice_sizes` are read from the call.)

---

## Single-Group Matcher — `RewriteAllReduceDynamicSlice::InstructionMatchesPattern`

### Purpose

The narrow case: a single global replica group, where each rank's slice offset is `id * shard`. The gate is a silent structural predicate — no `VLOG`.

### Algorithm

```c
// xla::hilo::RewriteAllReduceDynamicSlice::InstructionMatchesPattern   @0x2004520 (385 B)
bool InstructionMatchesPattern(HloInstruction* inst):
    if inst->opcode() != 0x30 /*kDynamicSlice*/:  return false;     // cmp [rsi+14h],0x30  @0x2004520
    ar = inst->mutable_operand(0);                                   // @0x2004549
    if ar->opcode() != 0x07 /*kAllReduce*/:       return false;     // cmp [rax+14h],7     @0x200454e

    si = GetSliceInfo1D(cast<HloDynamicSlice>(inst));               // @0x2004582
    if !si.valid:                                 return false;     // not a 1-D shard

    full = ar->shape().dims()[ si.slice_dim ];                      // full extent on sliced dim (r15)
    groups = cast<HloAllReduce>(ar)[ar+0x218].replica_groups();    // CollectiveDeviceList @ar+0x218
    if (groups.end - groups.begin) != 0x28:       return false;    // EXACTLY ONE ReplicaGroup
                                                                    //   sizeof(ReplicaGroup)=0x28=40
    group_size = groups[0].replica_ids_count;                      // movsxd [grp+0x10]  @0x20045ec
    if group_size * si.slice_size != full:        return false;    // sharding identity

    // --- offset leaf: the DS start index for slice_dim must reduce to id * scalar-const ---
    start = inst->mutable_operand( index_of(slice_dim) );          // pos>size() guard @0x2004697
    if start->opcode() == 0x25 /*kConvert*/:                       // peel one bitcast-convert
        start = start->operand(0);
    if start->opcode() != 0x4D /*kPartitionId*/:  return false;    // cmp al,0x4D
    return IsEffectiveScalarConstant<int>( start->operand(?), si.slice_size );  // tail @0x2003e90
```

The `[r12+18h]/[r12+20h]` arithmetic at `0x20045fd` selects which DS start-index operand corresponds to `slice_dim` (DS start indices are operands `1..rank`), guarded by a `pos > size()` `ThrowStdOutOfRange` at `0x2004697`.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `RewriteAllReduceDynamicSlice::InstructionMatchesPattern` | `0x2004520` | 385 B | single-group structural gate | CERTAIN |
| `RewriteAllReduceDynamicSlice::name` | `0x2003860` | 11 B | `"aws_neuron_rewrite_all_reduce_dynamic_slice"` (43) | CERTAIN |
| `(anon)::IsEffectiveScalarConstant<int>` | `0x2003e90` | 1668 B | `kConstant` ∧ effective-scalar ∧ value==arg | CERTAIN |

### Considerations

`IsEffectiveScalarConstant<int>(inst, val)` @`0x2003e90` is the offset-factor verifier: `inst->opcode()==0x24 (kConstant)` ∧ `ShapeUtil::TrueNumDimensions(shape)==0` (effective scalar) ∧ `LiteralBase::IsAll(LiteralUtil::CreateR0<int>(val))` — i.e. the constant's value equals `val`. Its `VLOG` reasons ("Shape is not an effective scalar. Actual shape: ", "Literal values do not match. Expected value: …, Actual literal: ", "Instruction is not a constant. Actual opcode: …, Expected opcode: ") are shared with the multiple-groups matcher.

> **GOTCHA —** the offset leaf is `kPartitionId` (`0x4D`), not `kReplicaId` (`0x5A`). The single optional `kConvert` (`0x25`) wrapper is peeled before the check. A reimplementer matching only `replica-id` will miss every real instance in this binary; the partition-id form is what the upstream lowering actually emits. (Only the `0x25→0x4D` leaf is read byte-exact; the start-index selection formula for rank>1 is reconstructed.)

---

## Multiple-Groups Matcher — `RewriteAllReduceDynamicSliceMultipleGroups::InstructionMatchesPattern`

### Purpose

The general case: ≥1 replica groups, all of equal width, where the per-rank shard offset is not a bare `id * shard` but a **partition-id-indexed constant pool**: `reshape( mul( const_shard, dynamic-slice(pool, partition-id) ) )`. The matcher must prove that the constant pool encodes the within-group rank ramp before it is safe to collapse to a reduce-scatter over the same groups. It is `VLOG`-instrumented throughout — every failure logs a verbatim reason keyed on `rewrite_all_reduce_dynamic_slice.cc`.

### Algorithm

```c
// xla::hilo::RewriteAllReduceDynamicSliceMultipleGroups::InstructionMatchesPattern  @0x2004ae0 (6826 B)
bool InstructionMatchesPattern(HloInstruction* inst):
    log("Checking if instruction matches pattern: " + inst->ToString());
 1. if inst->opcode() != 0x30:  log("Instruction is not a dynamic slice.");          return false;
 2. ar = inst->operand(0);
    if ar->opcode() != 0x07:    log("Dynamic slice operand is not an all-reduce.");  return false;
 3. si = GetSliceInfo1D(inst);
    if !si.valid:               log("Dynamic slice is not 1D.");                      return false;
 4. full = ar->shape().dims()[si.slice_dim];
    g0 = ar.replica_groups()[0].size;                                                // r14d
    for grp in ar.replica_groups():               // stride 0x28
        if grp.size != g0:      log("Replica group sizes are not consistent.");      return false;
 5. if g0 * si.slice_size != full: log("Dynamic slice size does not match operand size."); return false;
 6. if !slice_dim_in_bounds:    log("Slice dimension is out of bounds.");             return false;
 7. idx = ds_start_operand(si.slice_dim);
    if idx->opcode() != 0x5B /*kReshape*/:   log("Index operand is not a reshape.");  return false;
 8. mul = idx->operand(0);
    if mul->opcode() != 0x45 /*kMultiply*/:  log("Reshape operand is not a multiply.");return false;
 9. if !IsEffectiveScalarConstant<int>(mul->operand(0 or 1), si.slice_size):          // tries both
        log("Multiply operand is not an effective scalar constant.");                return false;
    log("Effective scalar constant is OK.");
10. ids = mul->operand(1);
    if ids->opcode() != 0x30: log("Multiply operand is not a dynamic slice.");        return false;
11. inner = cast<HloDynamicSlice>(ids);
    if inner.slice_sizes not 1-D: log("Pool slice sizes are not 1D.");                return false;
    if inner.slice_sizes[0] != 1: log("Pool slice size is not 1.");                   return false;
12. if inner->operand(0)->opcode() != 0x4D /*kPartitionId*/:
        log("Slice pool operand is not a partition ID.");                            return false;
13. pool = inner->operand(1);
    if pool->opcode() != 0x24 /*kConstant*/:  log("Slice pool operand is not a constant.");return false;
14. // validate the constant offset pool against expected ramp literals (G groups, g=g0 width, s=slice_size)
    match = LiteralBase::Equal(pool.literal(), pool_identity(G,g))         // [0,1,…,G*g-1]
         || LiteralBase::Equal(pool.literal(), pool_multiplied(G,g,s))     // per-group: s*j, j∈[0,g)
         || LiteralBase::Equal(pool.literal(), pool_repeated(G,g));        // per-group: grp repeated g×
    return match;                                                          // r15b
```

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `…MultipleGroups::InstructionMatchesPattern` | `0x2004ae0` | 6826 B | multi-group gate + offset-chain validation | CERTAIN (opcodes/strings); HIGH (pool labels) |
| `…MultipleGroups::name` | `0x2003870` | 11 B | `"…_multiple_groups"` (59) | CERTAIN |
| `LiteralBase::Equal` | — | — | full-literal compare of pool vs expected ramp | CERTAIN |

### Considerations

The three pools are built with `std::vector<int>` + `LiteralUtil::CreateR1<int>` and compared with `LiteralBase::Equal`; the running boolean is the return value. The human-readable labels *identity / multiplied / repeated* (builders at `0x20056c2`, `0x20058ba`, `0x2005a32`/`0x2005de2`) are reconstructed from the loop index arithmetic; no symbol names them.

> **NOTE —** the "multiple groups" name refers to the matched *replica-group topology*, not to a different emitted reduction. There is **no** extra cross-group reduce op. The cross-group structure is preserved simply by passing the all-reduce's full `replica_groups` straight into the reduce-scatter, so each group reduces within itself exactly as the all-reduce did.

---

## Shared Emitter — `RewriteAllReduceDynamicSlice::ExpandInstruction`

### Purpose

Both AR passes emit one `reduce-scatter` through **the same** function @`0x2006600`. The MultipleGroups vtable slot +0x48 is byte-identical to the single-group one; only the matcher (vtable+0x40) differs.

> **GOTCHA — `0x2004ae0` is the MultipleGroups *matcher*, not a second rewrite.** A pass
> registry that lists MultipleGroups with `EntryAddr=0x2004ae0` names only its gate. The
> binary carries exactly one `ExpandInstruction` body for this class,
> `RewriteAllReduceDynamicSlice::ExpandInstruction` @`0x2006600`, and it holds the sole
> `CreateReduceScatter` call site; both passes reuse it.

### Algorithm

```c
// xla::hilo::RewriteAllReduceDynamicSlice::ExpandInstruction(HloInstruction* ds)   @0x2006600 (788 B)
StatusOr<unique_ptr<HloInstruction>> ExpandInstruction(HloInstruction* ds):
    si = GetSliceInfo1D(cast<HloDynamicSlice>(ds));               // @0x200663e
    if !si.valid:                                                 // @0x2006643 — sole error path
        return InvalidArgumentError(<prefix> + ds->ToString());  // @0x200669a

    ar      = cast<HloAllReduce>( ds->operand(0) );               // the all-reduce
    value   = ar->operand(0);                                     // un-reduced reduce input
    module  = ds->GetModule();
    comp    = ds->parent();                                       // ds+0x48 → computation to add into

    cl      = (bool) ar[ar+0x250];                               // constrain_layout
    ugdi    = (bool) ar[ar+0x251];                               // use_global_device_ids
    chan    = hlo_query::NextChannelId(*module);                 // FRESH channel id   @0x2006775
    groups  = ar[ar+0x218].replica_groups();                     // AR's full groups, verbatim
    reduce  = ar->called_computations()[0];                      // +/sum reduction comp (untag &~3, +0x10)

    rs = HloInstruction::CreateReduceScatter(                    // @0x200683f
           /*shape=*/                 ds->shape(),               // per-shard output shape
           /*operands=*/              {value},                   // Span length 1
           /*reduce_computation=*/    reduce,
           /*replica_groups=*/        groups,                    // passed through unchanged
           /*constrain_layout=*/      cl,
           /*channel_id=*/            chan,
           /*use_global_device_ids=*/ ugdi,
           /*scatter_dimension=*/     si.slice_dim);
    rs = comp->AddInstruction(rs, /*name=*/"");                  // @0x200685d (empty name)
    return rs;   // OpExpanderPass::Run then ReplaceAllUsesWith(ds, rs); ds & offset sub-tree DCE'd
```

The demangled call target at `0x200683f` confirms the argument order: `CreateReduceScatter(Shape const&, Span<HloInstruction* const>, HloComputation*, Span<ReplicaGroup const>, bool, optional<long> const&, bool, long)`. The `Span<ReplicaGroup>` overload @`0x9668310` builds a `CollectiveDeviceList` from the span and forwards to the device-list overload that constructs the `kReduceScatter` node.

### Emitted graph

```text
%value = T[…, full = G*s, …]                 ar's operand 0 (un-reduced)
%rs    = reduce-scatter(%value),
           to_apply              = <AR's reduction computation>,
           replica_groups        = <AR's replica_groups, verbatim>,
           scatter_dimension     = si.slice_dim,
           constrain_layout      = AR.constrain_layout,
           channel_id            = NextChannelId(module),     // fresh, not the AR's
           use_global_device_ids = AR.use_global_device_ids,
           shape                 = ds.shape()                 // operand shape with dim[scatter_dim]=s
# replaces the dynamic-slice; the all-reduce and the partition-id offset sub-tree go dead → DCE'd.
```

Semantics: an all-reduce followed by "each rank slices out its own 1/G shard" *is* a reduce-scatter. The rewrite removes the redundant full materialisation of the all-reduce result plus the runtime partition-id offset math, replacing the whole idiom with the native collective. (CERTAIN — `CreateReduceScatter`, `NextChannelId`, and `InvalidArgumentError` are all named call sites inside `0x2006600`; the `ar+0x250/+0x251/+0x218` field offsets are read from the loads.)

### Single-group vs multiple-groups — contrast

| Aspect | `RewriteAllReduceDynamicSlice` | `…MultipleGroups` |
|---|---|---|
| Matcher addr / size | `0x2004520` / 385 B | `0x2004ae0` / 6826 B |
| Logging | none (silent) | full `VLOG` (~18 reasons + value dumps) |
| Replica-group gate | EXACTLY ONE group (span == `0x28`) | ≥1 groups, all SAME width ("…not consistent") |
| Sharding identity | `group_size · slice_size == full` | `g0 · slice_size == full` (uniform group) |
| Offset leaf | `(convert?)→partition-id × scalar-const` | `reshape(mul(scalar-const, dynamic-slice(pool, partition-id)))` |
| Offset-pool check | scalar const `== slice_size` | const POOL literal `== {identity \| ·s \| repeated}` ramp |
| ExpandInstruction | `0x2006600` | **`0x2006600` (identical)** |
| Constructor | trivial: `new(0x30)` + vptr store | trivial: `new(0x30)` + vptr store |
| `name()` | `aws_neuron_rewrite_all_reduce_dynamic_slice` | `…_multiple_groups` |

> **NOTE —** both passes are trivial-ctor: the registrar lambdas (`RegisterRewriteAllReduceDynamicSlice…` @`0x1e71cd0` / `…MultipleGroups…` @`0x1e71d10`) do `operator new(0x30)` + a vptr store and read **no** flag struct — unlike the combiner/permute passes that read `[rbx+0xDA0/0xE90/0xF48]`. Neither AR pass takes constructor arguments.

---

## DeletePermute — Degenerate-Collective Stripper

### Purpose

`DeletePermute` (key `aws_neuron_delete_permute`, namespace `xla::hilo`) is an `OpExpanderPass` whose matcher gates `all-reduce` (`7`) **or** `collective-permute` (`0x1D`) and whose expander replaces the matched op with its sole data operand. It is the *complement* of the rewriters above: those rewrite real collectives toward reduce-scatter; `DeletePermute` deletes collectives that are already the identity.

> **GOTCHA — the name is a historical label, not a description.** "Permute" here means
> `collective-permute`, never array permutation: the matcher does **not** touch
> `transpose` (`0x76`), `bitcast` (`0x13`), or `reshape` (`0x5B`). Its whole 19-byte body
> gates `opcode==7` (all-reduce) ‖ `opcode==0x1D` (collective-permute) and nothing else.
> Both the matcher and the 45-byte expander are read byte-exact below; opcodes decoded
> from `HloOpcodeString` @`0x96bb550`.

### Algorithm

```c
// xla::hilo::DeletePermute::InstructionMatchesPattern   @0x1f79470 (19 B — full body)
//   0x1f79470  movzx edx,[rsi+0x14]      ; opcode
//   0x1f79474  cmp   dl,7  ; setz al     ; all-reduce
//   0x1f7947a  cmp   dl,0x1D; setz dl    ; collective-permute
//   0x1f79480  or    eax,edx ; ret       ; match = (opcode==7) || (opcode==0x1D)
bool InstructionMatchesPattern(HloInstruction* inst):
    return inst->opcode() == 0x07 || inst->opcode() == 0x1D;   // bare opcode gate, NO other guard

// xla::hilo::DeletePermute::ExpandInstruction   @0x1f79490 (45 B — full body)
//   xor esi,esi ; call HloInstruction::mutable_operand ; wrap operand in StatusOr (ok-status, +8=op)
StatusOr<HloInstruction*> ExpandInstruction(HloInstruction* inst):
    return inst->mutable_operand(0);   // OpExpanderPass::Run then ReplaceInstruction(inst, operand0)
```

i.e. `all_reduce(x) → x` and `collective_permute(x) → x`. Both collectives are single-operand here (operand 0 is the value reduced/permuted), so this is the identity rewrite **assuming the collective is a no-op**.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `DeletePermute::InstructionMatchesPattern` | `0x1f79470` | 19 B | `opcode==7 ‖ opcode==0x1D` | CERTAIN |
| `DeletePermute::ExpandInstruction` | `0x1f79490` | 45 B | return `mutable_operand(0)` | CERTAIN |
| `DeletePermute::name` | `0x1f79460` | 11 B | `"aws_neuron_delete_permute"` (25) | CERTAIN |

### Considerations

The matcher carries **no** replica-group cardinality, shape, or single-device test — it is the only per-instruction gate. The pass is therefore correct *only* when scheduled in a context where those collectives are provably the identity: single-logical-NeuronCore / single-replica lowering (`replica_count==1`, all replica-groups singletons), where an all-reduce over one rank and a collective-permute whose source equals its target are both copies of the input.

> **GOTCHA —** the provable-redundancy precondition is enforced by **when the pass is added to the run-list**, not by any in-binary check. The conditional scheduling lives upstream (`HLOToTensorizer.so` / Penguin Python), outside this binary. A reimplementer who schedules `DeletePermute` unconditionally will silently delete *real* data-moving collectives and corrupt multi-rank programs. The absence of any in-binary guard is read directly from the 19-byte matcher; the single-device scheduling rationale is reconstructed.

---

## Replica-Group Semantics (binary-derived)

These offsets are shared by all three passes and are the data-layout contract a reimplementer must reproduce.

| Field | Location | Meaning | Confidence |
|---|---|---|---|
| `ReplicaGroup` size | `0x28` (40 B) | one protobuf `ReplicaGroup` object | CERTAIN |
| `replica_ids` count | `ReplicaGroup+0x10` | int32 `RepeatedField` size = group width | CERTAIN |
| group COUNT | `(vec.end - vec.begin) / 0x28` | divide-by-5 reciprocal (`imul 0xCCCC…CCCD` after `/8`) @`0x20067a4` | CERTAIN |
| `CollectiveDeviceList` | `ar+0x218` | `replica_groups()` @`0x96240f0` returns `vector<ReplicaGroup>&` | CERTAIN |
| `constrain_layout` | `ar+0x250` (bool) | forwarded verbatim to reduce-scatter | CERTAIN |
| `use_global_device_ids` | `ar+0x251` (bool) | forwarded verbatim | CERTAIN |
| `channel_id` | — | NOT forwarded; reduce-scatter gets a fresh `NextChannelId` | CERTAIN |
| reduction computation | `ar->called_computations()[0]` (untag `&~3`, `+0x10`) | reused unchanged | CERTAIN |

Single-group requires `|groups| == 1`; multiple-groups requires all `|group[i]|` equal and preserves the partition into the reduce-scatter, so each group reduces within itself exactly as the all-reduce did.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::OpExpanderPass::Run` (`0x29f0bb0`) | base post-order driver; applies matcher then expander; does the `ReplaceAllUsesWith`/erase |
| Collective combiners (#77/#78/#86) | rewrite/fuse *real* collectives; AR-DynamicSlice rewriters collapse the all-reduce∘slice idiom; `DeletePermute` strips trivial ones |
| `aws_neuron_rewrite_collective_permute` (#94) | rewrites real collective-permutes; `DeletePermute` strips degenerate ones |
| `CollectivePermuteToAllGather` (#86) | the inverse-direction idiom (CP → all-gather + id-indexed dynamic-slice); AR-DynamicSlice is the collapse pass for that emitted family |

---

## Cross-References

- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](collective-combiners.md) — the cost-model combiner family; `CreateReduceScatter` is the same constructor this rewrite targets
- [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) — where collectives leave the stock-HLO world; these rewrites run before that boundary
- [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) — `hlo_query::NextChannelId` and the channel-id allocation these rewrites consume
- **Part 4.21 — Kernel/Parameter Dedup** (`NeuronDuplicateParameterAllGatherRemover`) — the companion pass: that page covers the all-gather/parameter CSE, this page covers only the `DeletePermute` degenerate-collective strip
