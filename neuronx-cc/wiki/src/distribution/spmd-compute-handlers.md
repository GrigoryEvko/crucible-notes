# SPMD Compute-Op Partition Handlers and SpmdBuilder

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, front-end binary `neuronxcc/starfish/bin/hlo-opt` (cp310 wheel). The cp311/cp312 wheels carry the same symbols at the same VAs. Other versions will differ. Evidence is the IDA export (functions/names/strings/xrefs + per-function `context/*.md` callee tables); this build was decompiled with `NVOPEN_IDA_SKIP_DECOMPILE=1`, so the pseudocode below is reconstructed from callee/xref/string tables, not from Hex-Rays bodies.*

## Abstract

When `ShardingPropagation` ([13.2](sharding-propagation.md)) has labelled every HLO with
an `HloSharding`, the SPMD partitioner rewrites each op so that it runs on *one*
device's tile of the data. The rewrite is a per-opcode `SpmdPartitioningVisitor::Handle*`
visitor pass: each handler reshards its operands into the shardings the local op needs,
clones the op with a *per-partition* shape, and registers the result. This page documents
that compute-op surface — `HandleElementwise`, `HandleDot` (the windowed-einsum engine),
`HandleConvolution`, `HandleReduce`/`HandleReduceWindow`, `HandleBroadcast`,
`HandleDynamicSlice`, and `HandleGather` — plus the two primitives every handler composes
through: `SpmdBuilder::AddInstruction` (the single emit choke) and `PartitionedHlo::Reshard`
(the universal "make this value have target sharding" call).

The mental model is **GSPMD, faithfully**. Every symbol name, dispatch shape, and helper
set in this surface matches OpenXLA's `spmd_partitioner.cc`, `dot_handler.cc`,
`convolution_handler.cc`, and `gather_scatter_handler.cc` against the absl LTS `20230802`
tag the binary links. The handlers themselves contain **no Neuron-specific hardware logic**:
they decide *which reshard* and *which strategy*, then emit local ops. The only Neuron
touches visible at this layer are (1) `AddInstruction` strips the Shardy attribute
`xla.sdy.has_unreduced_axes` from every partitioned op, and (2) a small set of
custom-call handlers (`HandleCustomCallSPMDInternal_RotateRight`, `HandleCustomCallTopK`,
`HandleRaggedDot`) route through the same `AddInstruction`/`Reshard` surface. The Trainium
wire collectives are produced one layer down by `SPMDCollectiveOpsCreator`, which is
Neuron-customized and is [13.6](spmd-collective-emission.md)'s scope, not this page's.

The page is organized by the choke first (`SpmdBuilder`), then `HandleElementwise` (the
template every other handler follows), then `HandleDot`/windowed einsum as the centerpiece,
then the remaining handlers as deltas from those two, and finally the shared
`PartitionedHlo`/`Reshard` primitives.

For reimplementation, the contract is:

- **The emit choke**: what `AddInstruction` does to every newly built HLO — derive metadata,
  compute broadcast dims, strip the Shardy attr, take ownership, index it.
- **The handler template**: reshard-operands → clone-with-sharded-shape → register, and the
  `GetPartitionedHlo`/`SetPartitionedHlo`/`MakePartitionedShape` helpers it stands on.
- **The dot engine**: the recursive `PartitionDot` strategy dispatcher (batch /
  non-contracting / contracting / base-case) and the collective-matmul `while`-loop that
  `EmitWindowedDotGeneral` builds.
- **The reshard boundary**: `PartitionedHlo::Reshard` selects a reshard but never builds the
  collective itself — it calls into `state_.collective_ops_creator` ([13.6](spmd-collective-emission.md)).

| | |
|---|---|
| **Namespace** | `xla::spmd` (upstream XLA SPMD, absl LTS `20230802`) |
| **Emit choke** | `SpmdBuilder::AddInstruction` — `sub_2AA5360` (812 B, 46 bb, **53 callers**) |
| **Elementwise template** | `HandleElementwise` — `sub_2AC0CC0` (2215 B, 129 bb) |
| **Dot entry** | `HandleDot` — `sub_2A549E0` → `HandleDotHelper` `sub_2A54400` |
| **Strategy dispatcher** | `PartitionDot` — `sub_2A517D0` (10822 B, 464 bb, recursive) |
| **Windowed einsum** | `EmitWindowedDotGeneral` — `sub_2A49280` (19466 B, 665 bb) |
| **Reshard primitive** | `PartitionedHlo::Reshard` — `sub_2ABFEF0` (66 callers) |
| **Shardy attr stripped** | `xla.sdy.has_unreduced_axes` @ `0x485480` (in `AddInstruction`) |
| **Provenance** | stock GSPMD; Neuron = attr-strip + custom-call ops only |

---

## SpmdBuilder — the per-partition HloComputation builder

### Purpose

`SpmdBuilder` (derived from `xla::HloComputation::Builder`) is the per-partition
instruction factory. Every HLO that any handler emits is built into a `unique_ptr` and
handed to `SpmdBuilder::AddInstruction`, which is the single registration point. Beyond the
base builder's instruction vector, `SpmdBuilder` carries one extra field that the dot and
reshard logic read:

- `broadcast_dims_ : flat_hash_map<const HloInstruction*, flat_hash_set<long>>` — maps each
  added HLO to the set of output dims along which its value is provably *identical across all
  partitions* ("broadcast" / replicated dims). This is the bookkeeping that lets later
  collectives be elided when a sharded dim turns out to be uniform.

### Entry Point

```text
SpmdBuilder::AddInstruction              sub_2AA5360  (812 B, 46 bb)   ── the emit choke
  ├─ HloInstruction::SetupDerivedInstruction   sub_9677D80 (2475 B)    ── copy metadata/sharding hints
  ├─ SpmdBuilder::SetBroadcastDimsForAddedHlo  sub_2A9A400 (616 B)     ── compute broadcast_dims_[hlo]
  │    ├─ SetBroadcastDimsForElementwise       sub_2A99F10
  │    ├─ SetBroadcastDimsForSlice             sub_2A97C80
  │    ├─ SetBroadcastDimsForReshape           sub_2A98A10  (largest, 119 bb)
  │    ├─ SetBroadcastDimsForTranspose         sub_2A99620
  │    └─ SetBroadcastDimsForPad               sub_2A99B40
  ├─ HloInstruction::erase_frontend_attribute  sub_2A9C8D0  ── strip "xla.sdy.has_unreduced_axes"
  ├─ vector<HloInstruction*>::_M_realloc_insert         sub_1E7F550  ── raw ptr into comp list
  ├─ vector<unique_ptr<HloInstruction>>::_M_realloc_insert sub_1F01F50 ── take ownership
  └─ _Rb_tree<...>::_M_emplace_hint_unique     sub_2A97510  ── derived-instructions index
```

### Algorithm

`AddInstruction`'s body is fully recoverable from its callee table (the 15-callee list and
the string `xla.sdy.has_unreduced_axes` at `0x485480` are both present in
`context/...AddInstruction...0x2aa5360.md`).

```c
HloInstruction* SpmdBuilder::AddInstruction(unique_ptr<HloInstruction> inst):  // sub_2AA5360
    HloInstruction* raw = inst.get();

    // 1. Copy metadata + sharding hints from the visitor's current "derived-from" HLO.
    SetupDerivedInstruction(raw);                       // sub_9677D80

    // 2. Compute this HLO's replicated-dims set into broadcast_dims_[raw].
    SetBroadcastDimsForAddedHlo(*raw);                  // sub_2A9A400 (see below)

    // 3. NEURON: clear the Shardy partial-reduction marker on the partitioned op.
    raw->erase_frontend_attribute("xla.sdy.has_unreduced_axes");  // sub_2A9C8D0, str @0x485480

    // 4. Insert the raw ptr into the base computation's instruction list, then take
    //    ownership of the unique_ptr in the parallel owning vector.
    base_instructions_.push_back(raw);                  // vector<HloInst*>::_M_realloc_insert sub_1E7F550
    instructions_added_.push_back(std::move(inst));     // vector<unique_ptr>::_M_realloc_insert sub_1F01F50

    // 5. Register into the derived-instructions reverse index, keyed by HloPtrComparator.
    derived_instructions_[derived_from_].emplace_hint(...);  // _Rb_tree _M_emplace_hint_unique sub_2A97510

    // 6. A caller may stamp a sharding (set_sharding is in the callee table, sub_28E8A30).
    return raw;
```

> **NOTE —** the 53-caller list of `AddInstruction` *is* the proof that this is the choke
> point: every `Handle*` (`HandleElementwise`, `HandleReduce`, `HandleBroadcast`,
> `HandleDynamicUpdateSlice`, …), `DefaultAction`, and `EmitWindowedDotGeneral` appear in
> it. There is no second path from a handler to a registered instruction. [CONFIRMED — caller table]

> **GOTCHA —** step 3 is the only Neuron deviation in this whole subsystem, and it is easy
> to miss: a reimplementation that keeps the `xla.sdy.has_unreduced_axes` frontend attribute
> on partitioned ops will leave a stale Shardy partial-reduction marker that downstream
> Shardy-aware passes may act on. Strip it here, on every emitted op. [CONFIRMED — string `0x485480`]

### `SetBroadcastDimsForAddedHlo` — the replicated-dims propagator

`SetBroadcastDimsForAddedHlo` (`sub_2A9A400`, single caller = `AddInstruction`) dispatches on
the op category to populate `broadcast_dims_[hlo]`. A dim `d` is "broadcast" if the operation
provably keeps it identical across partitions.

```c
void SpmdBuilder::SetBroadcastDimsForAddedHlo(const HloInstruction& hlo):  // sub_2A9A400
    if (!primitive_util::IsArrayType(hlo.element_type())) return;  // only array-typed ops
    if (hlo.IsElementwise())            SetBroadcastDimsForElementwise(hlo);  // sub_2A99F10
    else if (hlo.opcode() == kSlice)    SetBroadcastDimsForSlice(hlo);        // sub_2A97C80
    else if (hlo.opcode() == kReshape)  SetBroadcastDimsForReshape(hlo);      // sub_2A98A10 (119 bb)
    else if (hlo.opcode() == kTranspose)SetBroadcastDimsForTranspose(hlo);    // sub_2A99620
    // kPad has its own SetBroadcastDimsForPad sub_2A99B40, called from the pad path.
```

The per-op rules (each takes an intersection/remap of the operands' `broadcast_dims_` sets):

| Op | Rule for `broadcast_dims_[out]` | Symbol | Confidence |
|---|---|---|---|
| Elementwise | `∩` over operands of `broadcast_dims_[operand]` — uniform in result only if uniform in *every* operand | `sub_2A99F10` (1257 B) | STRONG |
| Transpose | permute operand's broadcast set through the transpose permutation | `sub_2A99620` (1225 B) | STRONG |
| Reshape | a result dim is broadcast iff *all* contributing operand dims are (split/merge bookkeeping via `xla::Product`) | `sub_2A98A10` (3004 B, 119 bb) | STRONG |
| Slice | a dim stays broadcast iff it is **not** among the sliced (limited) dims | `sub_2A97C80` (452 B) | STRONG |

These are stock-XLA `SpmdBuilder` helpers (`spmd_partitioner.cc`). [INFERRED stock XLA from
symbol set; bodies not decompiled.]

---

## HandleElementwise — the handler template

### Purpose

`HandleElementwise` (`sub_2AC0CC0`, 2215 B, 129 bb) is `DefaultAction`'s body for every
elementwise opcode, and it is *re-used wholesale* by several non-elementwise handlers
(`HandleAllReduce`, `HandleBitcastConvert`, `HandleCollectivePermute`,
`HandleOptimizationBarrier`, and the replicated `HandleCustomCall` path — all in its caller
list). It establishes the template every compute handler follows: reshard all operands to a
common (output) sharding, then rebuild the op per-partition with sharded shapes. Read this
once and the other handlers are deltas from it.

### Algorithm

```c
void SpmdPartitioningVisitor::HandleElementwise(HloInstruction* hlo):  // sub_2AC0CC0
    HloSharding sharding = hlo->sharding();             // HloInstruction::sharding

    vector<HloInstruction*> new_operands;
    for (operand i in hlo->operands()):
        PartitionedHlo po = GetPartitionedHlo(operand(i));        // sub_2A21540 (Rb_tree find)
        if (po.sharding() != sharding):                          // HloSharding::operator==
            po = po.Reshard(sharding, /*pad=*/nullopt);          // sub_2ABFEF0 — inserts collective/copy
        new_operands.push_back(po.hlo());

    Shape shard_shape = MakePartitionedShape(hlo->shape(), sharding);  // sub_2AEE040 — full/tile
    auto new_hlo = hlo->CloneWithNewOperands(shard_shape, new_operands, nullptr);

    AddInstruction(std::move(new_hlo));                          // sub_2AA5360 — register + strip attr
    SetPartitionedHlo(hlo, PartitionedHlo(new_hlo, shard_shape, MakePartitioningState()));  // sub_2AB2B30
```

`MakePartitioningState()` (`sub_2A92B00`) snapshots `{partition_count, partition_id,
SPMDCollectiveOpsCreator, module, builder, next_channel_id, reshard_cache, partitioner}`
into the `PartitionedHlo` — that snapshot is what `Reshard` later reads to build collectives.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `HandleElementwise` | `sub_2AC0CC0` | reshard operands → clone with sharded shape → register | CONFIRMED |
| `GetPartitionedHlo` | `sub_2A21540` | lookup HLO → `PartitionedHlo` in visitor's map (HloPtrComparator) | CONFIRMED |
| `SetPartitionedHlo` (value) | `sub_2AB2B30` | record the partitioned result | CONFIRMED |
| `SetPartitionedHlo` (lazy) | `sub_2A21AC0` | `FunctionRef<HloInstruction*()>` builder form | CONFIRMED |
| `MakePartitionedShape` | `sub_2AEE040` | per-partition (tile) shape | CONFIRMED |
| `MakePartitioningState` | `sub_2A92B00` | snapshot the partitioning state into a `PartitionedHlo` | CONFIRMED |
| `PartitionedHlo::Reshard` | `sub_2ABFEF0` | bring a value to a target sharding (the reshard engine) | CONFIRMED |

### `HandleElementwiseWithDimsToReplicate` — the forced-replicate variant

`HandleElementwiseWithDimsToReplicate` (`sub_2AC28E0`) is the same template, but it first
*forces a set of dims to be replicated* before treating the op as elementwise. Its callers
are `HandleCholesky`, `HandleConcatenate`, and `HandleTriangularSolve` — ops that cannot be
partitioned along certain (triangular / concat) dims, so those dims must be replicated per
partition. The mechanism (distinct callees prove it):

```c
void HandleElementwiseWithDimsToReplicate(HloInstruction* hlo, Span<long> dims):  // sub_2AC28E0
    HloSharding s = hlo->sharding();
    if (!s.IsTileMaximal()):                                     // skip already-replicated
        // produce s' = s with the listed dims un-tiled (tiles folded into replicated dim)
        s = hlo_sharding_util::PartiallyReplicateTiledShardingOnDims(s, dims);  // sub_90DD5D0
        // (uses MoveAndMergeShardingTiles sub_90D3080 + GetFirstTargetDimToMoveShardingTiles sub_90CCF80)
    // then: HandleElementwise template with target sharding s' for every operand.
```

[CONFIRMED callers + callee `PartiallyReplicateTiledShardingOnDims`; tile-folding detail STRONG.]

---

## HandleDot — the windowed-einsum / dot-partitioning engine

This is stock-XLA `dot_handler.cc`, present in full. It is the centerpiece of the page: it is
the only compute handler whose partitioning involves a *cross-partition loop of collectives*
(the collective matmul), and every transformer's tensor-parallel matmul flows through here.

### Entry and the local-dot lambda

`HandleDot` (`sub_2A549E0`, 65 bb) does four things, then hands off:

```c
void SpmdPartitioningVisitor::HandleDot(HloInstruction* hlo):  // sub_2A549E0
    DotConvolutionDimsInfo dnums =
        dot_as_convolution_util::ParseDotGeneralFromDot(hlo);   // sub_90ECBD0
        //  unified dot/conv descriptor: {batch_dims, contracting_dims, lhs/rhs non-contracting}
    auto* dot = Cast<HloDotInstruction>(hlo);
    auto sparsity = dot->sparsity_descriptors();                // structured-sparsity metadata, copied

    // create_sharded_dot: emit ONE local per-partition dot. sub_2A37F80.
    auto create_sharded_dot =
        [&](HloInstruction* l, HloInstruction* r, SpmdBuilder* b, const Window& w) {
            Shape s = InferDotOpShape(l->shape(), r->shape(), dnums, /*preferred*/, sparsity);  // sub_961DBA0
            return HloInstruction::CreateDot(s, l, r, dnums.dot_dims, precision, sparsity, meta);// sub_9668450
        };

    HandleDotHelper(hlo, dnums, create_sharded_dot);            // sub_2A54400
```

`create_sharded_dot` (`sub_2A37F80`) is a `FunctionRef<StatusOr<HloInstruction*>(lhs, rhs,
SpmdBuilder*, Window&)>` — every strategy below calls it to materialize the *actual matmul on
local shards*. The strategy code only arranges the resharded operands, the reduction
collective, and (for windowed einsum) the loop around it; the matmul emission is this lambda.

### `HandleDotHelper` — shared by dot and conv

```c
StatusOr<HloInstruction*> HandleDotHelper(HloInstruction* hlo,
                                          const DotConvolutionDimsInfo& dnums,
                                          FunctionRef create_dot):  // sub_2A54400
    PartitionedHlo lhs = GetPartitionedHlo(hlo->operand(0));
    PartitionedHlo rhs = GetPartitionedHlo(hlo->operand(1));
    if (hlo->sharding().UniqueDevice().has_value()):            // single-device trivial path
        return MakeACopyAndReturnItsPartitionedHlo(...);
    return PartitionDot(lhs, rhs, hlo->shape(), hlo->sharding(), dnums,
                        num_partitions, create_dot, conv_window, module,
                        hlo, options, builder, &windowed_dot_general_loops, this);  // sub_2A517D0
```

`HandleDotHelper`'s caller list contains **both** `HandleDot` and `HandleConvolution` — the
convolution handler reuses this exact dispatcher (see [§ HandleConvolution](#handleconvolution)).

### `PartitionDot` — the recursive strategy dispatcher

`PartitionDot` (`sub_2A517D0`, 10822 B, 464 bb) peels **one partitioned dimension group at a
time** and recurses on the residual per-group problem. It chooses among four group strategies
by inspecting how the operands and output are tiled. The selection helpers are cost models.

```c
StatusOr<HloInstruction*> PartitionDot(PartitionedHlo lhs, PartitionedHlo rhs, ...):  // sub_2A517D0
    auto pcounts = GetPartitionsForDims(sharding, dims, DotComponent{Batch|Lhs|Rhs|Contracting}); // sub_2A2ABB0

    if (batch dims sharded):
        return PartitionDotGroupOnBatch(...);            // sub_2A558D0 — pure data parallelism
    if (an LHS/RHS non-contracting dim sharded):
        // cost model: which operand to keep sharded to avoid reshard cost
        bool lhs_matching = LhsIsBestMatchForNonContractingPartitioning(...);  // sub_2A3DD90
        return PartitionDotGroupOnNonContracting(lhs_matching, ...);  // sub_2A5AEB0 — no reduce
    if (contracting dim sharded):
        // cost model: prefer contracting-dim partition (AllReduce) vs non-contracting?
        if (PrioritizeContractingDimensionsPartitioning(...)):       // sub_2A3EEE0
            return PartitionDotGroupOnContracting(...);  // sub_2A5C740 — needs AllReduce
    if (conv spatial dim sharded):
        return PartitionConv(...);                       // sub_2A5A470 — halo path
    return PartitionBaseCase(...);                       // sub_2A4E2D0 — terminal: local dot (+windowed)
```

Grouping is done with `hlo_sharding_util::GroupShardingOnDims` / `UngroupSharding` /
`PartiallyReplicateTiledShardingOnDims`, and per-group sub-partitioners are built with
`CreatePerGroupPartitioningState` / `AlignGroupsWith` / `GetPerGroupBaseShape`.

#### The four group strategies

| Strategy | Addr | Sharded dim class | Collective needed | Confidence |
|---|---|---|---|---|
| `PartitionDotGroupOnBatch` | `sub_2A558D0` (10688 B, 276 bb) | batch dims | **none** — each group owns whole batch slices | STRONG |
| `PartitionDotGroupOnNonContracting` | `sub_2A5AEB0` (4341 B, 183 bb) | LHS/RHS output dim | **none** — output naturally sharded | STRONG |
| `PartitionDotGroupOnContracting` | `sub_2A5C740` (7535 B, 204 bb) | contracting dim (`K`) | **AllReduce** of partial products | CONFIRMED |
| `PartitionBaseCase` | `sub_2A4E2D0` (9098 B, 322 bb) | none left | optional windowed einsum | CONFIRMED |

The contracting-dim case is the tensor-parallel matmul (each device holds `1/k` of `K`):

```c
StatusOr<HloInstruction*> PartitionDotGroupOnContracting(...):  // sub_2A5C740
    auto group_shardings = GetDotGroupPartitionContractingLhsRhsShardings(...);   // sub_2A34B10
    bool needs_all_reduce; vector<long> reduce_dims;
    auto out_sharding = GetDotGroupPartitionContractingOutputShardings(           // sub_2A351E0
        ..., &needs_all_reduce, &reduce_dims);
    // group on the contracting dim, build per-group partitioner, recurse to a local partial dot:
    HloInstruction* partial = PartitionDot(/*contracting-replicated sub-problem*/...);
    if (needs_all_reduce):
        // sum partial products across the contracting-dim device groups via the state's
        // SPMDCollectiveOpsCreator (the concrete AllReduce emit is 13.6's scope).
        partial = AllReduceAlongShardingDims(builder, partial, sharding, &next_channel_id,
                                             reduce_dims, collective_ops_creator, kAdd_comp);  // sub_2A94E60
    return partial;
```

> **NOTE —** `PartitionDot` is the cross-link to [0.7 (matmul sharding)](../front/worked-example-matmul.md):
> the choice it makes here (batch vs non-contracting vs contracting) *is* the difference
> between data-parallel, weight-stationary, and reduce-after-matmul tensor parallelism. The
> handler only decides and emits the reshards + reduce; the wire collective is [13.6](spmd-collective-emission.md).

### `PartitionBaseCase` and the windowed-einsum decision

Once no partitioned dim group remains, `PartitionBaseCase` (`sub_2A4E2D0`) either emits a
plain local dot (operands resharded to replicated, `create_dot`) **or** chooses *windowed
einsum* (collective matmul) when an operand is large enough that overlapping the matmul with
a collective-permute ring beats replicating it.

```c
StatusOr<HloInstruction*> PartitionBaseCase(...):  // sub_2A4E2D0
    auto cfg = GetWindowedEinsumConfiguration(lhs, rhs, options, ...);  // sub_2A3BF70 (3637 B)
        //  decides from operand byte sizes vs thresholds:
        //   "xla_gpu_threshold_for_windowed_einsum_mib"       (default 100000)
        //   "xla_gpu_operand_bytes_threshold_for_windowed_einsum"
        //  if overhead > benefit: logs "Overhead outweighs benefit. Skipping windowed einsum"
    if (cfg.is_windowed):
        return EmitWindowedDotGeneral(lhs, rhs, ..., cfg, DotDimensionIndexMapping{...}, ...);  // sub_2A49280
    // else: reshard operands to replicated and emit one plain create_dot.
```

The three windowed-einsum threshold strings are present verbatim in `strings.json`
(`xla_gpu_threshold_for_windowed_einsum_mib`, default documented as `100000`;
`xla_gpu_operand_bytes_threshold_for_windowed_einsum`; `Overhead outweighs benefit. Skipping
windowed einsum`). [CONFIRMED — strings.]

### `EmitWindowedDotGeneral` — the collective-matmul ring loop

This is the centerpiece. `EmitWindowedDotGeneral` (`sub_2A49280`, 19466 B, 665 bb) builds a
`kWhile` loop that runs the matmul `num_partitions` times, each iteration computing a local
shard's contribution and rotating the moving operand one step around a device ring. Its sole
caller is `PartitionBaseCase`; the callee table proves every structural piece.

```c
StatusOr<HloInstruction*> EmitWindowedDotGeneral(
        PartitionedHlo lhs, PartitionedHlo rhs, const Shape& out, const HloSharding& out_sharding,
        const DotConvolutionDimsInfo& dnums, long num_partitions, long /*chan*/,
        FunctionRef create_dot, const Window& w, HloModule* m, HloInstruction* orig,
        const SpmdPartitionerOptions& opts, SpmdBuilder* b,
        vector<WindowedDotGeneralLoop>* loops, const WindowedEinsumConfig& cfg,
        DotDimensionIndexMapping idx_map,
        optional<HloSharding> o0, o1, o2, o3, o4, o5):  // sub_2A49280
    // BODY computation, built once and run num_partitions times:
    //   - local matmul on the current shard          -> create_dot(cur_lhs, cur_rhs, body_b, w)
    //   - rotate the moving operand one ring step     -> CollectivePermute (state collective creator)
    //   - accumulate the running result               -> CreateBinary(kAdd)         sub_9665360
    HloComputation* body = build_loop_body(...);                 // emits create_dot + permute + kAdd
    HloComputation* cond = build_loop_cond(num_partitions);      // i < num_partitions

    HloInstruction* loop = HloInstruction::CreateWhile(loop_shape, cond, body, init);  // sub_9666250
    AddInstruction(loop);                                        // sub_2AA5360

    // Record the loop so the windowed-einsum loop optimizer can fuse/pipeline it later.
    loops->push_back(WindowedDotGeneralLoop{ loop, /*windowed_in_contracting=*/cfg.in_contracting,
                                             /*operands_sharded_dim=*/..., ... });   // sub_2A37500
    return extract_result(loop);
```

Two loop variants are selected by the six trailing `optional<HloSharding>` parameters in the
signature (confirmed in the demangled signature): an **all-gather windowed** variant (gather
the partitioned operand across the ring) and a **reduce-scatter windowed** variant (scatter
partial outputs). An experimental **all-to-all** windowed-einsum variant is gated by the knob
string `xla_gpu_experimental_enable_alltoall_windowed_einsum`. [variants STRONG; all-to-all knob CONFIRMED string.]

> **QUIRK —** `EmitWindowedDotGeneral` does **not** emit a fused collective-matmul op — it
> emits an ordinary `kWhile` whose body contains a local dot, a collective-permute, and an
> add, and *records the loop* in `loops` for a later pass to recognize and pipeline. A
> reimplementation that looks for a single "collective matmul" HLO will not find one; the
> structure is a while-loop plus a side-table entry (`WindowedDotGeneralLoop`). [CONFIRMED —
> `CreateWhile` + `WindowedDotGeneralLoop` vector insert in the callee table.]

> **NOTE —** the ring structure is stock GSPMD, but the `CollectivePermute` it builds is
> produced by the state's `SPMDCollectiveOpsCreator`, which is where Neuron customizes the
> Trainium wire collective. That creator is [13.6](spmd-collective-emission.md), not this page.

---

## HandleConvolution

`HandleConvolution` (`sub_2A15230`, 19 bb) reuses the dot engine via
`dot_as_convolution_util`. Convolution's batch/feature/contracting dims map onto the same
`DotConvolutionDimsInfo` roles a dot uses, so they go through `PartitionDot` exactly like a
dot. Only *spatial* dims (where halo exchange is needed) divert to `PartitionConv`.

```c
void SpmdPartitioningVisitor::HandleConvolution(HloInstruction* hlo):  // sub_2A15230
    DotConvolutionDimsInfo dnums =
        dot_as_convolution_util::ParseConvolutionDimsInfo(hlo);  // sub_90ED360
    auto create_sharded_conv =                                   // sub_2A1A1C0
        [&](HloInstruction* l, HloInstruction* r, SpmdBuilder* b, const Window& w) {
            if (is_dot_as_conv_form):
                return CreateShardedConvForDotGeneralConvolution(...);  // sub_90EDC60
            else:
                return CreateShardedConvolution(..., adjusted_window);  // sub_2A16F50
        };
    HandleDotHelper(hlo, dnums, create_sharded_conv);            // sub_2A54400 — SAME dispatcher as dot
```

When the partitioned dim is a spatial/feature/batch-group conv dim, `PartitionConv` routes to
`PartitionConvolutionBaseCase`, which selects among the conv-only strategies:

| Conv strategy | Addr | Confidence |
|---|---|---|
| `PartitionConvolutionWithBatchGroupCount` | `sub_2A15500` | CONFIRMED |
| `PartitionConvolutionWithFeatureGroupCount` | `sub_2A161D0` | CONFIRMED |
| `PartitionConvolutionWithSpatialDimensionHaloExchangeOnLHS` | `sub_2A17F90` (8678 B, 328 bb) | CONFIRMED |
| `PartitionConvolutionWithSpatialDimensionHaloExchangeOnRHS` | `sub_2A1AE70` (9722 B, 351 bb) | CONFIRMED |

The spatial-halo variants call `PartitionedHlo::ReshardAsWindowedInput` (`sub_2AD4F30`,
13772 B, 479 bb) to reshard the input so each partition holds its local window **plus the
overlapping halo region** the conv window needs, adjust the `Window` for the per-partition
stride/pad, then call `create_sharded_conv`. The actual halo collective (collective-permute
exchange of edge slices) is emitted *inside* `ReshardAsWindowedInput`/`ExchangeHalo` — owned
by [13.6](spmd-collective-emission.md). This handler only chooses the strategy and computes
the local windowed shape. [CONFIRMED.]

---

## HandleReduce and HandleReduceWindow

### HandleReduce — local reduce + AllReduce over reduced-sharded dims

`HandleReduce` (`sub_2ACC080`, body lambda `sub_2ACD6E0`) is the canonical XLA reduce
partition: reduce locally, then AllReduce across the device groups that share a *reduced*
sharded dim (with the same reduction op).

```c
void SpmdPartitioningVisitor::HandleReduce(HloInstruction* hlo):  // sub_2ACC080
    auto [reduced_dims, kept_dims] = split_dims(hlo);
    // kept dims keep the output sharding; reduced dims are partitioned consistently.
    for (input/init operand):
        po = GetPartitionedHlo(operand);
        po = po.Reshard(/*reduced dims partially replicated*/                       // sub_90DD5D0
                        PartiallyReplicateTiledShardingOnDims(sharding, reduced_dims));

    HloInstruction* local = HloInstruction::CreateReduce(                           // sub_9651180
        local_shape, inputs, inits, reduced_dims, reduce_computation);

    HloSharding out_sharding =
        hlo_sharding_util::RemoveShapeDimensions(sharding, reduced_dims);           // sub_90DD240

    if (a reduced dim was sharded):
        // local reduce only covered this partition's slice -> sum/max across device groups
        local = SpmdPartitioner::AllReduceAlongShardingDimsInternal(                // sub_2A947F0
            builder, local, sharding, &next_channel_id, reduced_sharded_dims,
            collective_ops_creator, reduce_computation, /*per_dim=*/...);
    SetPartitionedHlo(hlo, local);
```

`AllReduceAlongShardingDimsInternal` (`sub_2A947F0`, via the public
`AllReduceAlongShardingDims` `sub_2A94E60`) builds the device replica groups with
`GetPartitionGroupsForReplication` (`sub_2AE9DB0`) / `GetIotaPartitionGroupsForReplication`
(`sub_2AEB450`), then emits an AllReduce with the **same** reduction computation
(`add`/`max`/`and`/…) over those groups via `collective_ops_creator`, assigning a fresh
channel id. Variadic/multi-output reduces use `HloSharding::Tuple`. [CONFIRMED.]

### HandleReduceWindow — halo, no AllReduce

`HandleReduceWindow` (`sub_2AD86F0`) partitions a `kReduceWindow` sharded along windowed
dims. Unlike `HandleReduce`, it needs **no** AllReduce — the output is element-local once the
halo is gathered. The only cross-partition traffic is the halo collective-permute inside
`ReshardAsWindowedInput`.

```c
void SpmdPartitioningVisitor::HandleReduceWindow(HloInstruction* hlo):  // sub_2AD86F0
    auto* rw = Cast<HloReduceWindowInstruction>(hlo);
    Window window = rw->window();
    // each partition gets its local region PLUS the halo overlap the window needs:
    WindowedInputShardReturnValue in = ReshardAsWindowedInput(window, target_sharding,
                                            pad_value, /*mask_invalid=*/, /*force_mask=*/);  // sub_2AD4F30
        //  returns { sharded_input, shard_window, dynamic_slice_index }
    HloInstruction* local = emit local kReduceWindow on in.sharded_input
                                with in.shard_window;            // InferReduceWindowShape
    SetPartitionedHlo(hlo, local);                              // no AllReduce
```

> **NOTE —** `WindowedInputShardReturnValue` is the shared currency between conv-spatial-halo,
> reduce-window, and select-and-scatter handlers. All three reshape-with-halo through
> `ReshardAsWindowedInput`; the difference is only the local op emitted on top.

---

## HandleBroadcast, HandleDynamicSlice, HandleGather

These three illustrate the three reshard *rules* that recur across the simpler handlers:
strip-broadcast-dims, replicate-the-sliced-dim, and strategy-dispatch-with-index-masking.

### HandleBroadcast — local, no collective

```c
void SpmdPartitioningVisitor::HandleBroadcast(HloInstruction* hlo):  // sub_2AC91A0
    HloSharding out_sharding = hlo->sharding();   // may tile both broadcast and carried dims
    // strip the broadcast-introduced dims from the output sharding to get the operand sharding:
    HloSharding op_sharding =
        hlo_sharding_util::RemoveShapeDimensions(out_sharding, broadcast_output_dims);  // sub_90DD240
    PartitionedHlo po = GetPartitionedHlo(hlo->operand(0)).Reshard(op_sharding);
    Shape shard_shape = MakePartitionedShape(hlo->shape(), out_sharding);               // sub_2AEE040
    // each partition broadcasts its operand shard into its output tile — NO collective.
    SetPartitionedHlo(hlo, /*lambda sub_2AA6130*/ emit local kBroadcast with same
                            broadcast_dimensions and sharded shapes);
```

[CONFIRMED — `RemoveShapeDimensions` + `MakePartitionedShape` callees; broadcast is purely local.]

### HandleDynamicSlice — sliced dims must be replicated

```c
void SpmdPartitioningVisitor::HandleDynamicSlice(HloInstruction* hlo):  // sub_2AC9630
    // Dims sliced with a runtime start index CANNOT be split across a partition boundary
    // (would need halo) -> force them replicated; passthrough dims (full-size slice) keep sharding.
    for (each dynamically-sliced dim d):
        sharding = PartiallyReplicateTiledShardingOnDims(sharding, {d});   // operand resharded
    Shape shard_shape = MakePartitionedShape(hlo->shape(), sharding);      // sub_2AEE040
    SetPartitionedHlo(hlo, /*lambda sub_2AA6270*/ HloInstruction::CreateDynamicSlice(      // sub_964E070
                            shard_shape, operand_shard, start_indices, slice_sizes));
```

The sibling `HandleDynamicUpdateSlice` (not in this set, but a caller of `AddInstruction`)
mirrors this with a select/mask over the partition that owns the updated region. [CONFIRMED.]

### HandleGather — strategy dispatch with index masking

`HandleGather` (`sub_2A78FA0`, 57 bb) casts to `HloGatherInstruction`, builds
`PartitionedHlo`s for operand and indices, and delegates to the recursive `PartitionGather`
dispatcher (`sub_2A77A80`, 4973 B, 205 bb). `PartitionGather` tries strategies in a fixed
order, each recursing on the residual problem (the order is the dispatcher's callee order):

| Order | Strategy | Addr | What it handles | Collective |
|---|---|---|---|---|
| 1 | `PartitionGatherParallelDimensions` | `sub_2A7B640` | operand & index share a tiled "parallel" batch dim | none (local) |
| 2 | `PartitionGatherIndexPassthroughDimensions` | `sub_2A797D0` | index sharded on a dim passing through to output | AllReduce over index groups if operand must replicate |
| 3 | `PartitionGatherOperandPassthroughDimensions` | `sub_2A7A680` | an operand slice dim fully copied to output is sharded | none — output keeps tile |
| 4 | `PartitionGatherTrivialSlicedOperandDimensions` | `sub_2A89460` | operand sharded on a sliced (`slice_size==1`) dim | mask + AllReduce/select |
| — | fallthrough | — | replicate operand + indices, gather | (replicate) |

Strategy 4 is the subtle one — it must mask out indices that fall outside the local slice:

```c
// PartitionGatherTrivialSlicedOperandDimensions  sub_2A89460
auto bounds = IndexBoundsForGatherScatterOperandPartitionedOnTrivialSliceDims(...);  // sub_2A87030
indices_local = indices - partition_offset;            // subtract this partition's slice offset
mask = CreateCompare(indices_local, bounds, kLt/kGe);  // sub_964D150 — out-of-range -> masked
result_local = gather(operand_shard, masked_indices);
result = AllReduce/select over operand partitions(result_local);  // combine across operand tiles
```

[CONFIRMED — strategy set + order from the dispatcher callee table; `CreateCompare` +
`IndexBoundsForGatherScatterOperandPartitionedOnTrivialSliceDims` confirm the masking.]
The sibling `HandleScatter` (not in this set) mirrors this with `PartitionScatter*` and an
update-combine reduction. These are stock GSPMD gather/scatter handlers.

---

## Shared Reshard / Builder Infrastructure Functions

Every handler above composes through the same `PartitionedHlo`/`SpmdBuilder` primitives.
**Boundary**: the *collective emit* inside `Reshard`/halo/AllReduce is
[13.6](spmd-collective-emission.md); this section documents only the helpers the compute
handlers invoke and how they *choose* a reshard.

### `PartitionedHlo` and `PartitionedHlo::Reshard`

`PartitionedHlo` is the value type flowing between handlers: `{ HloInstruction* hlo_; Shape
base_shape_; PartitioningState state_; }` (constructed at `sub_2A11AF0`). The state snapshot
(`MakePartitioningState`, `sub_2A92B00`) holds `{partition_count, partition_id, builder,
module, SPMDCollectiveOpsCreator, next_channel_id, ReshardCache*, SpmdPartitioner*}`.

`PartitionedHlo::Reshard` (`sub_2ABFEF0`, demangled `xla::spmd::PartitionedHlo::Reshard(
HloSharding const&, optional<Literal>)`, **66 callers**) is the universal "make this value
have target sharding" primitive. It caches in `ReshardCache`, else dispatches to the cheapest
reshard:

| Reshard kind | What it does | Confidence |
|---|---|---|
| `Replicate()` | AllGather to full replication | CONFIRMED |
| `ReplicatePartial(dims)` | AllReduce/AllGather a subset of dims | CONFIRMED |
| `ReshardToPartialReplicateWithAllGather` / `…FromPartialReplicateWithDynamicSlice` | tile ↔ partial-replicate (all-gather up / dynamic-slice down) | CONFIRMED |
| `ReshardPartialReplicateWithAllToAll` / `TryComplexReshardHandling` | dim permutations via all-to-all | CONFIRMED |
| `ReshardAsWindowedInput` | halo-exchange reshard for windowed ops | CONFIRMED |

> **GOTCHA —** the compute handlers **never** call the collective creator directly — they
> always go through `Reshard`. A reimplementation that wires a handler straight to an
> AllGather emitter will duplicate the `ReshardCache` dedup that `Reshard` provides and may
> emit redundant collectives. The single chokepoint for "change this value's sharding" is
> `Reshard`; the single chokepoint for "register a new HLO" is `AddInstruction`.

### Shape, offset, and constant helpers

| Helper | Addr | Role | Confidence |
|---|---|---|---|
| `MakePartitionedShape` | `sub_2AEE040` | per-partition tile shape: per dim `ceil(dim / tile_count)`; tuples recurse; uneven → `GetPaddedShapeForUnevenPartitioning` | CONFIRMED |
| `MakePartitionOffsets` | `sub_2AF98B0` | per tiled dim, start offset = `ordinal[dim] * tile_size[dim]` | CONFIRMED |
| `MakeTiledPartitionOrdinals` | `sub_2AF9CB0` | linear `partition_id` → multi-dim tile coordinate | CONFIRMED |
| `AllReduceAlongShardingDims` | `sub_2A94E60` | device-group AllReduce used by reduce + `ReplicatePartial` | CONFIRMED |
| `AllGatherShards` | `sub_2AA39D0` | the dual of the above, used by `Reshard`'s all-gather paths | CONFIRMED |
| `CreateZero` | `sub_2AAA0B0` | zero of element type (sum init / pad) | CONFIRMED |
| `PadToShape` | `sub_2A43C30` | pad an HLO up to a target shape (uneven tiles before a collective) | CONFIRMED |

---

## Neuron-Specific vs Stock XLA

The entire compute-handler surface — `HandleDot`/`Conv`/`Reduce`/`Broadcast`/`Elementwise`/
`DynamicSlice`/`Gather`, the `PartitionDot` engine, windowed einsum, the gather/scatter
strategies, and `PartitionedHlo`/`Reshard` — is **stock upstream XLA GSPMD**. Symbol names,
dispatch structure, and helper set match OpenXLA (`spmd_partitioner.cc`, `dot_handler.cc`,
`convolution_handler.cc`, `gather_scatter_handler.cc`) against absl LTS `20230802`.

| Element | Provenance | Evidence |
|---|---|---|
| All `Handle*` compute handlers + `PartitionDot` + windowed einsum | **stock XLA** | symbol names match OpenXLA exactly |
| `xla.sdy.has_unreduced_axes` stripped in `AddInstruction` | **Neuron / Shardy** | string `0x485480` in `AddInstruction` callee chain |
| `HandleCustomCallSPMDInternal_RotateRight` | **Neuron** SPMD-internal op | symbol in `names.json`; routes through `AddInstruction` |
| `HandleCustomCallTopK`, `HandleRaggedDot` | recent upstream / custom-call | symbols present; same `AddInstruction`/`Reshard` surface |
| Concrete wire collectives (`SPMDCollectiveOpsCreator`) | **Neuron** (Trainium channels, replica groups) | [13.6](spmd-collective-emission.md)'s scope |

The compute handlers themselves are faithful stock XLA: they decide reshard + strategy and
emit local ops; they contain no Neuron-specific hardware logic. [STRONG — symbol-set match;
the three custom-call handlers and the attr strip are the only confirmed Neuron deltas.]

---

## Related Components

| Name | Relationship |
|---|---|
| `SpmdPartitioner` / `DefaultAction` ([13.1](spmd-partitioner-driver.md)) | drives the visitor; `DefaultAction` is the 53rd `AddInstruction` caller (the elementwise fallback) |
| `SPMDCollectiveOpsCreator` ([13.6](spmd-collective-emission.md)) | builds the AllReduce / AllGather / CollectivePermute / halo wire ops that `Reshard` and the dot/reduce handlers request |
| `ShardingPropagation` ([13.2](sharding-propagation.md)) | assigns the `HloSharding` each handler reads off `hlo->sharding()` |
| `HloSharding` algebra ([13.3](sharding-algebra.md)) | `PartiallyReplicateTiledShardingOnDims`, `GroupShardingOnDims`, `RemoveShapeDimensions` the handlers call |

## Cross-References

- [SPMD Partitioner Driver](spmd-partitioner-driver.md) — the visitor pass that invokes every handler on this page; owns `DoPartition` / `DefaultAction`
- [Sharding Propagation](sharding-propagation.md) — produces the `HloSharding` annotations these handlers partition against
- [Sharding Algebra](sharding-algebra.md) — the `hlo_sharding_util` tile-move / group / partially-replicate helpers used throughout
- [SPMD Collective Emission](spmd-collective-emission.md) — `SPMDCollectiveOpsCreator`, `ReshardNoCache`, `ExchangeHalo`: the wire collectives this page's handlers request but never build
- [Worked Example: Matmul Sharding](../front/worked-example-matmul.md) — how the `PartitionDot` batch/non-contracting/contracting choice maps to data- vs tensor-parallel matmul
