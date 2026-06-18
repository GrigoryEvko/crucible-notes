# SPMD Collective / Communication Emission

> *All addresses on this page are virtual addresses (VMA) for the `hlo-opt` binary inside `neuronx_cc-2.24.5133.0+58f8de22` (cp310 wheel, `neuronxcc/starfish/bin/hlo-opt`); resolve via `objdump --start-address` or the VMA-keyed IDA tables. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). Symbols are from the not-stripped `nm` table plus the IDA function/string/xref sidecars. Other builds will differ.*

## Abstract

When the SPMD partitioner ([13.1](spmd-partitioner-driver.md)) needs to move data between devices — to all-reduce a contracting dimension, gather a sharded operand back to full, scatter a reshard, or exchange a convolution halo — it does **not** call `HloInstruction::CreateAllReduce` directly. It calls one of **eight `std::function` callbacks** packed into a 256-byte `SPMDCollectiveOpsCreator` struct that the partitioner carries in its `PartitioningState`. Each callback marshals replica groups and a channel id, then invokes the matching stock `HloInstruction::Create*` factory and records the result through `SpmdBuilder::AddInstruction`. This page reconstructs that struct, the factory that fills it (`GetDefaultCollectiveOpsCreator @0x2a930a0`), the body of each callback, the reshard and halo-exchange paths that drive them, and the channel-id / replica-group plumbing that decides *which* devices each collective names.

The entire surface is **stock upstream XLA** (`xla/service/spmd/{spmd_partitioner,spmd_partitioner_util}.cc`); every symbol lives in `namespace xla::spmd` or `xla::`, none in a Neuron/Aws namespace. The decisive evidence that Neuron does **not** author a custom emitter is structural: the only caller of `GetDefaultCollectiveOpsCreator` is the stock `SpmdPartitioner` ctor; there is **no** `set_collective_ops_creator` setter symbol anywhere in the binary; and the only two partitioner classes present are stock `xla::spmd::SpmdPartitioner` and `xla::spmd::StatefulRngSpmdPartitioner` — the registered subclass inherits the ctor that builds the default creator and passes it straight through. (All four facts re-grep-confirmed against `functions.json`/`strings.json`.)

Neuron intervenes on **two sides** of this stock surface, never inside it. *Before* partitioning, `AwsNeuronLNCShardingConstraint` + `--distribution-strategy` + `lnc_splitter` decide the tile assignment and therefore which replica groups the stock callbacks ultimately encode ([13.8 LNC sharding constraint]). *After* partitioning emits stock `kAllReduce`/`kAllGather`/`kAllToAll`/`kCollectivePermute` HLOs, a cluster of `xla::hilo` passes rewrites and combines them — that is the downstream lowering target, owned by [§4.x collective combiners](../hlo-opt/collective-combiners.md) and [CollectivePermute→AllGather](../hlo-opt/collectivepermute-to-allgather.md), and it is where this page hands off. This page draws the boundary explicitly so a reimplementer knows the emission is generic GSPMD and the Neuron flavour is upstream (input) and downstream (rewrite), not in the emitter itself.

For reimplementation, the contract is:

- The **`SPMDCollectiveOpsCreator` callback struct**: 8 `std::function` members at fixed 0x20-stride offsets, the exact signature of each (which signature is which collective), and the factory that populates them.
- The **per-collective creator body**: how each callback turns `(groups | iota, channel_id, dim)` into a `CollectiveDeviceList` and a `HloInstruction::Create*` call.
- The **reshard / halo dispatch**: which reshard routine reads which callback offset out of `PartitioningState`, and how `ExchangeHalo*` issues collective-permutes for windowed ops.
- The **channel-id / replica-group / `use_global_device_ids` plumbing**: where the channel-id counter is seeded, how the two replica-group encodings (explicit-vector vs iota) converge on `CollectiveDeviceList`, and which group-mode the partition collectives select.
- The **stock-vs-Neuron boundary** and the downstream `xla::hilo` hand-off.

| | |
|---|---|
| **Creator factory** | `xla::spmd::GetDefaultCollectiveOpsCreator(long,long)` — `0x2a930a0` (470 B) |
| **Creator struct** | `SPMDCollectiveOpsCreator` — 256 B = 8 × `std::function` (32 B), offsets f0..f7 @ 0x00..0xE0 |
| **Only factory caller** | `SpmdPartitioner::SpmdPartitioner(ll, SpmdPartitionerOptions)` — `0x2a93280` |
| **Channel-id seed** | `xla::hlo_query::NextChannelId(HloModule&)` — `0x8ab1ac0` (347 B) |
| **Group collectors** | `AllReduceAlongShardingDims` `0x2a94e60` · `AllGatherShards` `0x2aa39d0` |
| **Reshard entries** | `ReshardWithCollectivePermute` `0x2aa3b40` · `ReshardWithAllToAll` `0x2ab9730` |
| **Halo emission** | `ExchangeHalo` `0x2af1150` · `ExchangeHaloCompact` `0x2afe1e0` |
| **Stock vs Neuron** | **All stock XLA** (`xla::spmd`/`xla::`); no Neuron emitter, no setter symbol |
| **IR level** | HLO (`xla::HloModule`), inside the SPMD partition pass; pre-penguin |

---

## The SPMDCollectiveOpsCreator Struct

### Purpose

`SPMDCollectiveOpsCreator` is the indirection layer that lets the partitioner emit collectives without naming a concrete `Create*` factory. The partitioner holds one creator; every reshard, group-collector, and halo helper calls through it. The indirection exists so that a *grouped* sharding (a partial-replicate sub-mesh) can swap in a creator that rewrites device numbering (§Sub-Group Restriction) while the calling code stays identical. Upstream XLA declares it in `spmd_partitioner.h`.

### Layout

Each member is a libstdc++ `std::function` = 32 bytes: `_M_functor` (`_Any_data`, 16 B — holds captured longs inline or a heap-block pointer) at +0x00, `_M_manager` (a `_Function_handler::_M_manager` fn-ptr) at +0x10, `_M_invoke` (the dispatch fn-ptr) at +0x18. Eight members → **256 bytes (0x100)** with field bases at a uniform 0x20 stride:

```text
SPMDCollectiveOpsCreator (256 B)
  f0 @0x00  create_partition_id
  f1 @0x20  all-reduce  (explicit replica groups)
  f2 @0x40  all-reduce  (iota replica-group list)
  f3 @0x60  collective-permute
  f4 @0x80  all-to-all  (explicit groups)        — Span<operand> + optional split_dim
  f5 @0xA0  all-to-all  (iota groups)
  f6 @0xC0  all-gather  (explicit groups)
  f7 @0xE0  all-gather  (iota groups)
```

The factory writes the eight `_M_invoke` slots at 0x18, 0x38, 0x58, 0x78, 0x98, 0xB8, 0xD8, 0xF8 — strictly monotonic in struct-offset order, so binary field order equals declaration order. (CONFIRMED from `GetDefaultCollectiveOpsCreator` disasm.)

> **NOTE —** the offsets and signatures are CONFIRMED (the mangled `_Function_handler<...>` instantiations give the exact call signature of each field). The upstream *member names* (`create_cross_partition_all_reduce`, …) are STRONG — matched to fields by signature, not proven from a source string. Throughout, identity is keyed on the signature, not the name.

### Signature → collective identity

The mangled handler types are the authoritative discriminator. Two structural cues do all the work: a **`Span<HloInstruction* const>` operand list plus `std::optional<long> split_dimension`** is the all-to-all shape (XLA all-to-all is multi-operand with an optional split dim); a **single operand plus `const Shape& ag_shape` plus an `all_gather_dimension` long** is the all-gather shape. Each collective appears twice — once taking an explicit `std::vector<std::vector<int64_t>>` groups, once taking a compact `xla::IotaReplicaGroupList` (XLA's post-2023 iota-device-list encoding).

| Field | Offset | Signature (after `SpmdBuilder*`) | Collective | Group form |
|---|---|---|---|---|
| f0 | 0x00 | `()` | partition-id | — |
| f1 | 0x20 | `(HloInstr*, HloComputation*, vec<vec<long>>&, long)` | all-reduce | explicit |
| f2 | 0x40 | `(HloInstr*, HloComputation*, IotaReplicaGroupList&, long)` | all-reduce | iota |
| f3 | 0x60 | `(HloInstr*, vec<pair<long,long>>&, long)` | collective-permute | — |
| f4 | 0x80 | `(Span<HloInstr*const>, vec<vec<long>>&, long, optional<long>)` | all-to-all | explicit |
| f5 | 0xA0 | `(Span<HloInstr*const>, IotaReplicaGroupList&, long, optional<long>)` | all-to-all | iota |
| f6 | 0xC0 | `(HloInstr*, Shape&, vec<vec<long>>&, long, long)` | all-gather | explicit |
| f7 | 0xE0 | `(HloInstr*, Shape&, IotaReplicaGroupList&, long, long)` | all-gather | iota |

> **QUIRK —** the f4/f5 all-to-all signature (operand *span*, optional split dim) is easy to misread as an all-gather. The disambiguator is exactly the `optional<split_dimension>` argument and the absence of an `ag_shape`/`all_gather_dim` pair. A reimplementation that wires the span-form into an all-gather slot will emit the wrong collective for every all-to-all reshard.

---

## GetDefaultCollectiveOpsCreator — The Factory

### Purpose

`GetDefaultCollectiveOpsCreator(long num_partitions, long num_replicas)` (`0x2a930a0`, 470 B, 7 BB, 4 try-blocks) constructs and returns the 256-byte creator. It is the **only** producer of the struct, and its only caller is the stock `SpmdPartitioner` ctor (`0x2a93280`). Calling convention seen: `rdi` = return slot (`creator*`), `rsi` = `num_partitions`, `rdx` = `num_replicas`. The two world-size longs are captured into the small lambdas' functors. STOCK-XLA (`spmd_partitioner.cc`).

### Algorithm

```c
SPMDCollectiveOpsCreator* GetDefaultCollectiveOpsCreator(  // 0x2a930a0
        SPMDCollectiveOpsCreator* out, long num_partitions, long num_replicas):

    // f0 partition-id — captures (num_replicas, num_partitions) INLINE in _M_functor
    out->f0._M_invoke  = lambda5_invoke;        // 0x2aa5310
    out->f0._M_manager = lambda5_manager;       // 0x2a8d380
    store_inline(out->f0._M_functor, num_replicas, num_partitions);  // @+0x20,+0x28

    // f1/f2 all-reduce — share one heap capture block: operator new(0x28)
    blk = operator new(0x28);                   // {num_replicas, num_partitions, ...}
    out->f1 = { lambda6_invoke /*0x2aa8620*/, lambda6_manager /*0x2a8dbf0*/, blk };
    out->f2 = { lambda7_invoke /*0x2aa8690*/, lambda7_manager /*0x2a8db60*/, blk };

    // f3 collective-permute — small capture inline
    out->f3 = { lambda8_invoke /*0x2aaa150*/, lambda8_manager /*0x2a8db20*/, ... };

    // f4/f5 all-to-all — heap capture block new(0x28)
    out->f4 = { lambda9_invoke  /*0x2aaa080*/, lambda9_manager  /*0x2a8d3a0*/, ... };
    out->f5 = { lambda10_invoke /*0x2aa92b0*/, lambda10_manager /*0x2a8da90*/, ... };

    // f6/f7 all-gather — f7 heap capture block new(0x28)
    out->f6 = { lambda11_invoke /*0x2aa7c30*/, lambda11_manager /*0x2a8da50*/, ... };
    out->f7 = { lambda12_invoke /*0x2aa7c90*/, lambda12_manager /*0x2a8d9c0*/, ... };
    return out;
```

The lambda numbering (#5..#12) is the source order of the captures; the factory writes them into f0..f7 monotonically. The factory does **not** heap-allocate for the small lambdas (it stores captured longs inline in `_M_functor`); it calls `operator new(0x28)`/`new(0x20)` only for the larger captures and stores the heap pointer in `_M_functor[0]`. The four `try`-blocks unwind those `new`s if a later member's construction throws. (CONFIRMED from disasm; capture-block field contents at the byte level are INFERRED from the `new` sizes and register stores.)

### What each callback emits

Each callback is a thin marshaller over a stock `HloInstruction::Create*` factory. The explicit-group form builds a `std::vector<xla::ReplicaGroup>` (proto) then `CollectiveDeviceList(Span<ReplicaGroup>)`; the iota form builds `CollectiveDeviceList(IotaReplicaGroupList)` without materialising the group lists. All wrap the result with `SpmdBuilder::AddInstruction`.

```c
// f1 all-reduce (explicit groups) — lambda#6 body @0x2aa7fa0
HloInstruction* create_all_reduce(SpmdBuilder* b, HloInstruction* operand,
        HloComputation* reduction, vec<vec<long>>& groups, long channel_id):
    rg   = build_replica_groups(groups);                 // vector<ReplicaGroup>
    cdl  = CollectiveDeviceList(rg);                     // 0x9667ef0 / 0x1f7aa90
    comp = module->AddComputationAndUnifyNamesAndIds(reduction->Clone());
    ar   = HloInstruction::CreateAllReduce(             // 0x964d530 (DevList overload)
              operand->shape(), Span{operand}, comp, cdl,
              /*constrain_layout=*/false,
              /*channel_id=*/optional<long>(channel_id),
              /*use_global_device_ids=*/true);          // see §Plumbing
    return b->AddInstruction(ar);

// f3 collective-permute — lambda#8 invoke @0x2aaa150
HloInstruction* create_collective_permute(SpmdBuilder* b, HloInstruction* operand,
        vec<pair<long,long>>& src_dst, long channel_id):
    cp = HloInstruction::CreateCollectivePermute(       // 0x964d8d0
            operand->shape(), operand, src_dst, optional<long>(channel_id));
    return b->AddInstruction(cp);

// f4/f5 all-to-all — iota body @0x2aa9ba0
HloInstruction* create_all_to_all(SpmdBuilder* b, Span<HloInstruction*const> ops,
        CDL_or_groups groups, long channel_id, optional<long> split_dim):
    a2a = HloInstruction::CreateAllToAll(               // 0x964d730
            result_shape, ops, CollectiveDeviceList(groups),
            /*constrain_layout=*/false, optional<long>(channel_id), split_dim);
    return b->AddInstruction(a2a);

// f6/f7 all-gather — groups body @0x2aa77b0
HloInstruction* create_all_gather(SpmdBuilder* b, HloInstruction* operand,
        const Shape& ag_shape, CDL_or_groups groups, long channel_id, long ag_dim):
    ag = HloInstruction::CreateAllGather(               // 0x964d3d0
            ag_shape, Span{operand}, ag_dim, CollectiveDeviceList(groups),
            /*constrain_layout=*/false, optional<long>(channel_id),
            /*use_global_device_ids=*/true);
    return b->AddInstruction(ag);
```

> **NOTE —** every builder has a second overload that takes a raw `Span<ReplicaGroup>` (the pre-`CollectiveDeviceList` form), living in the `0x9668xxx` range (e.g. `CreateAllGather` ReplicaGroup form `@0x96680b0`). The default creator always calls the **`CollectiveDeviceList`** overloads (`0x964d3d0`/`0x964d530`/`0x964d730`/`0x964d8d0`), because both group encodings funnel through `CollectiveDeviceList` first. (CONFIRMED — both overload addresses present in the symbol table.)

### Function map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `GetDefaultCollectiveOpsCreator(ll)` | `0x2a930a0` | Builds the 256 B creator; sole producer | CERTAIN |
| f0 partition-id invoke / body | `0x2aa5310` / `0x2aa51a0` | `CreatePartitionId(MakeShape(U32,{}))` | CONFIRMED |
| f1 all-reduce(grp) body / invoke | `0x2aa7fa0` / `0x2aa8620` | explicit-group all-reduce | CONFIRMED |
| f2 all-reduce(iota) invoke | `0x2aa8690` | iota all-reduce | CONFIRMED |
| f3 collective-permute invoke | `0x2aaa150` | collective-permute | CONFIRMED |
| f4 all-to-all(grp) invoke | `0x2aaa080` | explicit-group all-to-all | CONFIRMED |
| f5 all-to-all(iota) body / handler | `0x2aa9ba0` / `0x2aa92b0` | iota all-to-all | CONFIRMED |
| f6 all-gather(grp) body / invoke | `0x2aa77b0` / `0x2aa7c30` | explicit-group all-gather | CONFIRMED |
| f7 all-gather(iota) invoke | `0x2aa7c90` | iota all-gather | CONFIRMED |
| `HloInstruction::CreatePartitionId` | `0x9665aa0` | builder | CONFIRMED |
| `HloInstruction::CreateAllReduce` (DevList) | `0x964d530` | builder | CONFIRMED |
| `HloInstruction::CreateAllGather` (DevList) | `0x964d3d0` | builder | CONFIRMED |
| `HloInstruction::CreateAllToAll` (DevList) | `0x964d730` | builder | CONFIRMED |
| `HloInstruction::CreateCollectivePermute` | `0x964d8d0` | builder | CONFIRMED |

---

## Reshard Emission — Choosing the Collective

### Purpose

`PartitionedHlo::Reshard*` moves a tensor from one `HloSharding` to another, picking the cheapest collective. Every reshard reads the `SPMDCollectiveOpsCreator` out of the `PartitioningState` and dispatches through one of its callbacks by **indirect call to a fixed struct offset**. STOCK-XLA (`spmd_partitioner.cc`).

The creator embedded in `PartitioningState` begins at `state+~0x168`, so the indirect call targets seen at the reshard sites resolve as: `state+0x1E0` = f3 (`0x168+0x78`), `state+0x200` = f4 (`0x168+0x98`), `state+0x220` = f5 (`0x168+0xB8`) — the 0x20 stride between consecutive callbacks is exactly one `std::function`, which is what nails the field identities. (CONFIRMED from the call sites in `ReshardWith*`.)

### Algorithm

```c
// PartitionedHlo::ReshardWithCollectivePermute(target) — 0x2aa3b40 (1822 B)
HloInstruction* ReshardWithCollectivePermute(HloSharding target):
    if !CanReshardWithCollectivePermute(source, target):    // 0x2ae7230 predicate
        // true iff shardings differ ONLY by a permutation of tile-assignment
        // device ids (same tile shape) => one collective-permute suffices
        return <other path>;
    creator = copy(state.collective_ops_creator);           // ctor/dtor at site
    pairs   = build_src_dst_pairs(source, target);          // vector<pair<long,long>>
    return (*creator.f3)(state.builder, hlo, pairs,         // call [rbx+0x1E0]
                         *state.next_channel_id);

// PartitionedHlo::ReshardWithAllToAll(target, dims, ...) — 0x2ab9730 (7752 B)
HloInstruction* ReshardWithAllToAll(target, Span<pair<long,long>> dims, bool):
    for (src_dim, dst_dim) in dims:                          // recurses on remainder
        a2a_shape = ShapeInference::InferAllToAllShape(...);
        out = is_iota_groups
              ? (*creator.f5)(b, Span{hlo}, iota_groups,     // call [r15+0x220]
                              channel, split_dim)
              : (*creator.f4)(b, Span{hlo}, groups,          // call [r15+0x200]
                              channel, split_dim);
        out = CreateReshape(out);                            // land new layout
        out = CreateTranspose(out, InferTransposeShape(...));
        hlo = out;
    // last hop falls back to ReshardWithCollectivePermute
    return ReshardWithCollectivePermute(target);
```

`ReshardToPartialReplicateWithAllGather` (`0x2aae160`) all-gathers along the replicated dims via `AllGatherShards` (f6/f7); its inverse `ReshardFromPartialReplicateWithDynamicSlice` (`0x2aa4340`) uses a plain `dynamic-slice` (no collective). `ReshardAsWindowedInput` (`0x2ad4f30`) produces a haloed operand via the halo-exchange path below.

### Function map

| Symbol | Address | Size | Callback used | Confidence |
|---|---|---|---|---|
| `ReshardWithCollectivePermute` | `0x2aa3b40` | 1822 | f3 | CONFIRMED |
| `ReshardWithAllToAll` | `0x2ab9730` | 7752 | f4 / f5 (→f3 last hop) | CONFIRMED |
| `ReshardToPartialReplicateWithAllGather` | `0x2aae160` | — | f6 / f7 (via `AllGatherShards`) | STRONG |
| `ReshardFromPartialReplicateWithDynamicSlice` | `0x2aa4340` | — | none (dynamic-slice) | STRONG |
| `ReshardAsWindowedInput` | `0x2ad4f30` | — | f3 (via `ExchangeHalo*`) | STRONG |
| `CanReshardWithCollectivePermute` | `0x2ae7230` | — | predicate | CONFIRMED |

---

## Halo-Exchange Emission

### Purpose

Windowed ops — convolution, reduce-window, select-and-scatter, gather/scatter — need each partition to read a margin ("halo") of its neighbours' data. `ExchangeHalo*` (in `spmd_partitioner_util.cc`) emits the cross-partition movement of those margins as **collective-permutes** through the creator's f3 callback, stitched together with slices and concatenations. Every entry takes `const SPMDCollectiveOpsCreator&` + `long* next_channel_id` + `SpmdBuilder*`. STOCK-XLA.

### Algorithm

```c
// ExchangeHalo — 0x2af1150 (5236 B, 203 BB)
HloInstruction* ExchangeHalo(HloInstruction* hlo,
        OffsetCalculation left_halo, OffsetCalculation right_halo, long dim,
        HloSharding target, const SPMDCollectiveOpsCreator& creator,
        long* next_channel_id, SpmdBuilder* b):
    // size the margin slices from the piecewise OffsetCalculation
    w_left  = left_halo.MaxInRange(0, num_partitions);      // 0x2af0f80
    w_right = right_halo.MaxInRange(0, num_partitions);
    left_slice  = CreateSlice(hlo, ...);                    // 0x964dfb0 — margin to send L
    right_slice = CreateSlice(hlo, ...);                    //            margin to send R
    // CROSS-PARTITION movement: shift each margin to the neighbour via f3
    left_recv   = (*creator.f3)(b, left_slice,  pairs_shift_right, *next_channel_id);
    right_recv  = (*creator.f3)(b, right_slice, pairs_shift_left,  *next_channel_id);
    return CreateConcatenate({left_recv, hlo, right_recv}, dim);   // 0x964e190

// ExchangeHaloAndGetValidData — 0x2b03f30 (4535 B)
//   = ExchangeHalo (or ExchangeHaloCompact) THEN mask out-of-range elements:
//     pred = CreateCompare(CreateIota, CreateBroadcast(limits));
//     return CreateTernary(Select, pred, exchanged, pad_value);
//   This is the function the conv/window compute-handlers (13.5) call.
```

`ExchangeHaloCompact` (`0x2afe1e0`, 17802 B, 741 BB) is the gather-everything variant: it builds the halo as one region using `CreateIota`/`CreateBroadcast`/`CreateCompare`/`CreateDynamicSlice`/`CreatePad`/`CreateTernary`, and — uniquely on this page — calls `GetPerGroupCollectiveOpsCreator` (§Sub-Group Restriction) to restrict the creator to the exchange's device sub-group before issuing the collective.

### Function map

| Symbol | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `ExchangeHalo` | `0x2af1150` | 5236 | slice + f3 permute + concat | CONFIRMED |
| `ExchangeHaloCompact` | `0x2afe1e0` | 17802 | gathered-region variant; uses per-group creator | CONFIRMED |
| `ExchangeHaloAndGetValidData` | `0x2b03f30` | 4535 | halo + validity mask | CONFIRMED |
| `PadEachPartitionWithHaloExchange` | `0x2a63e30` | — | uniform padded shard size | STRONG |
| `PadFromPartialReplicateShape` | `0x2b03350` | — | pad on partial-replicate | STRONG |
| `HaloExchangeToPadOnLeft` | `0x2b07ce0` | — | left-only pad | STRONG |
| `OffsetCalculation::MaxInRange` | `0x2af0f80` | — | max halo over a partition range | CONFIRMED |
| `CreateSlice` / `CreateConcatenate` | `0x964dfb0` / `0x964e190` | — | margin cut / stitch | CONFIRMED |

---

## Per-Dim Group Collectors

Two helpers form the device groups from sharding dims and then fan into the right callbacks. They are the **only** path to f1/f2 (all-reduce) and f6/f7 (all-gather). STOCK-XLA.

```c
// SpmdPartitioner::AllReduceAlongShardingDims — 0x2a94e60
//   -> AllReduceAlongShardingDimsInternal 0x2a947f0
HloInstruction* AllReduceAlongShardingDims(b, operand, sharding,
        long* next_channel_id, Span<const long> dims,
        const SPMDCollectiveOpsCreator& creator, HloComputation* reduction):
    groups = device_groups_over(sharding, dims);   // groups formed by tile-assignment dims
    return is_iota(groups)
           ? (*creator.f2)(b, operand, reduction, iota(groups), *next_channel_id) // [+0x58]
           : (*creator.f1)(b, operand, reduction, groups,       *next_channel_id);// [+0x38]

// SpmdPartitioner::AllGatherShards — 0x2aa39d0
//   -> AllGatherShardsInternal 0x2aa2b30
HloInstruction* AllGatherShards(b, operand, sharding, long* next_channel_id,
        Span<const long> dims, const SPMDCollectiveOpsCreator& creator):
    g = is_iota ? (*creator.f7)(...)      // [+0xF8]
                : (*creator.f6)(...);     // [+0xD8]
    return CreateReshape(g);              // merge gathered shards into the full dim
```

> **NOTE —** the callback reachability is a clean partition: **f1/f2** only via `AllReduceAlongShardingDims`; **f6/f7** only via `AllGatherShards`; **f3** via `ReshardWithCollectivePermute` and the halo paths; **f4/f5** via `ReshardWithAllToAll`; **f0** (partition-id) pervasively, to build the dynamic-slice offsets every per-partition slice indexes by. (CONFIRMED from the indirect-call offsets.)

---

## Sub-Group Restriction — GetPerGroupCollectiveOpsCreator

### Purpose

`xla::spmd::(anon)::GetPerGroupCollectiveOpsCreator(const SPMDCollectiveOpsCreator&, const DeviceGroupTileAssignment&)` (`0x2ae83e0`, 4028 B, 140 BB) builds a **brand-new** `SPMDCollectiveOpsCreator` whose 8 callbacks capture the original creator by reference plus the `DeviceGroupTileAssignment`. On each invocation they translate the requested replica groups / partition-id from the sub-group's *local* device numbering into *global* numbering, then forward to the original callback. This is how a nested/grouped sharding (a partial-replicate sub-mesh) emits collectives that name only the devices in its group. Callers: `ExchangeHaloCompact`, `CreatePerGroupPartitioningState` (`0x2b06d90`). STOCK-XLA — the grouping machinery is `xla::hlo_sharding_util`; the translation table comes from `DeviceGroupTileAssignment`, which [13.7 mesh→replica-group](mesh-replica-groups.md) derives from the sharding tile assignment. Neuron influences only *which* sharding/mesh reaches here, via the LNC sharding constraint.

---

## Wiring — Ctor → Visitor → PartitioningState

The creator is built once in the partitioner ctor and threaded by value/const-ref down to every reshard site; the channel-id counter is threaded by **pointer**.

```text
SpmdPartitioner::SpmdPartitioner(num_partitions, num_replicas, options)  0x2a93280
  └─ GetDefaultCollectiveOpsCreator(num_partitions, num_replicas)        0x2a930a0
       => stores 256 B creator as member collective_ops_creator_
SpmdPartitioner::Run(module, threads)                                    0x2ab76d0
  └─ hlo_query::NextChannelId(*module)  0x8ab1ac0  -> seed next_channel_id (1+max)
  └─ PartitionComputation(entry, sharding, &next_channel_id, ...)        0x2a944e0
       └─ CreateVisitor(comp, np, nr, creator&, &next_channel_id, ...)   0x2aa4ff0
            (StatefulRng override: 0x2a10a20 — passes inherited creator through)
            └─ SpmdPartitioningVisitor ctor                              0x2aa4b30
                 └─ MakePartitioningState()                             0x2a92b00
                      => copies creator (operator= 0x2a92580) into
                         PartitioningState; stores &next_channel_id, builder, module
```

`StatefulRngSpmdPartitioner` is the registered subclass (RTTI confirmed in [13.1](spmd-partitioner-driver.md)); its `CreateVisitor` (`0x2a10a20`) passes the inherited default creator straight through. There is **no** Neuron/Aws `SpmdPartitioner` subclass and **no** `set_collective_ops_creator` setter — so the default creator is always the one used. (Both negatives re-grep-confirmed against `functions.json`/`strings.json`.)

---

## channel_id / replica_group / use_global_device_ids Plumbing

### channel_id

Seeded in `SpmdPartitioner::Run` by `hlo_query::NextChannelId(module)` = `1 + max` existing `channel_id` in the module. Threaded as `int64_t* next_channel_id` through `PartitionComputation → CreateVisitor → visitor → PartitioningState`. Each callback receives the *current* value as its trailing `long channel_id`; `HloInstruction::Create{AllReduce,AllGather,AllToAll,CollectivePermute}` stores it as `std::optional<long>`, and the caller post-increments the counter. A **non-`nullopt` channel id means cross-partition (not cross-replica) semantics** — exactly what SPMD partition collectives need.

> **CORRECTION (cross-check 13.6 / 4.x) —** the `NextChannelId @0x8ab1ac0` here is the **stock SPMD seed** during partitioning. It is *not* the Neuron channel-id machinery. The downstream Neuron `xla::hilo` passes `NeuronCollectiveStreamIdInjector` (`name()` `@0x1f94c00`) and `NeuronUniqueChannelIdEnforcer` **re-stamp** `stream_id`/`channel_id` *after* this emission and also call `NextChannelId` to mint fresh ids on collision (see [collective stream/channel-id](../hlo-opt/collective-stream-channel-id.md)). No contradiction: the SPMD partitioner assigns ids at emission, the Neuron passes repair/partition them at lowering. Both are byte-confirmed and live in different layers.

### Two replica-group encodings, one CollectiveDeviceList

```c
// GROUP form  (f1,f4,f6): explicit, materialised
vec<ReplicaGroup> rg = build_from(vec<vec<long>> groups);     // proto groups
cdl = CollectiveDeviceList(Span<ReplicaGroup>(rg));           // 0x9667ef0/0x1f7aa90

// IOTA form   (f2,f5,f7): compact, lazy
cdl = CollectiveDeviceList(IotaReplicaGroupList{num_groups, group_size, [reshape/transpose]});
//   replica_groups() (0x96240f0) lazily ExpandIota()s only when an explicit
//   list is demanded — the post-2023 XLA compact encoding.
```

The choice of group-vs-iota is made by the **caller** (compute handler / reshard) based on whether the device groups are an affine iota pattern; [13.7](mesh-replica-groups.md) owns turning mesh + sharding dims into either the `vector<vector<long>>` or the `IotaReplicaGroupList`. `CollectiveDeviceList` ctors: `0x9667ef0` / `0x1f7aa90` / `0x9623620`; `replica_groups()` accessor `0x96240f0`.

### use_global_device_ids / constrain_layout

The `CreateAllReduce` / `CreateAllGather` `CollectiveDeviceList` overloads take, after the device list: `bool constrain_layout`, `optional<long> channel_id`, `bool use_global_device_ids`. At the f1 all-reduce-groups call site (`@0x2aa8344`) the fixed immediates are `constrain_layout = false` and `use_global_device_ids = true` — i.e. default-creator collectives are emitted with **global device ids on**, which is required whenever a channel id is present and replica groups span partitions. `CreateCollectivePermute`/`CreateAllToAll` have no `use_global_device_ids` bool (not applicable).

This pairs with [13.7](mesh-replica-groups.md)'s `GetCollectiveOpGroupMode(has_channel_id, use_global_device_ids)` truth table: `(channel && global==true) ⇒ kFlattenedID` (flat global device ids). SPMD partition collectives therefore select `kFlattenedID`. (Tag: the true/false immediates are STRONG — read off the call-site constants; the group-mode mapping is CONFIRMED in [13.7](mesh-replica-groups.md).)

---

## Stock-XLA vs Neuron — The Boundary

**Everything on this page is stock upstream XLA.** No function documented here is Neuron-authored; all live in `namespace xla` / `xla::spmd`. Neuron touches this surface from two sides only:

- **Input (before emission):** `AwsNeuronLNCShardingConstraint` + `--distribution-strategy` + `lnc_splitter` decide the tile assignment, hence which replica groups the stock callbacks encode. Neuron does **not** replace the creator — the default creator is used (sole-caller, no-setter, no-subclass evidence above).
- **Output (after emission):** the partitioner emits stock `kAllReduce`/`kAllGather`/`kAllToAll`/`kCollectivePermute`, then a suite of `xla::hilo` passes rewrites/combines them. These create *new* collectives and re-mint channel ids, but they are strictly downstream and belong to the lowering pages, not to emission.

| Downstream `xla::hilo` pass | Anchor | Relationship |
|---|---|---|
| `NeuronAllReduceCombiner` | `Run @0x1f8f990` | combines emitted all-reduces |
| `NeuronCollectivePermuteToAllGather` | `name() @0x1f8ffa0`, `Run @0x1f931d0` | rewrites f3 output to all-gather + dynamic-slice |
| `NeuronCollectiveStreamIdInjector` | `name() @0x1f94c00` | stamps `stream_id` post-emission |
| `NeuronAllGatherCombiner` / `NeuronCombiner` / `RewriteCollectivePermute` / `DecomposeIntAllReduce` / `NeuronWhileLoopAllReduceCodeMotion` / … | (xla::hilo) | combine / rewrite / hoist collectives |

(CONFIRMED these symbols exist in `xla::hilo`; their internals are out of scope here.)

---

## Cross-References

- [The SpmdPartitioner Driver & Options](spmd-partitioner-driver.md) — builds the creator in its ctor, seeds `next_channel_id`, runs the pass (13.1)
- [SPMD Compute Handlers](spmd-compute-handlers.md) — dot/conv/reduce handlers that call these resharders and `ExchangeHaloAndGetValidData` (13.5)
- [Mesh → Replica-Group Math](mesh-replica-groups.md) — `IotaReplicaGroupList`/`CollectiveDeviceList`, the `GetCollectiveOpGroupMode` truth table, group encoding (13.7)
- [Collective Stream-ID & Channel-ID Family](../hlo-opt/collective-stream-channel-id.md) — the Neuron `xla::hilo` re-stamping that runs *after* this emission
- [CollectivePermute → AllGather Lowering](../hlo-opt/collectivepermute-to-allgather.md) — the downstream rewrite of f3 output (`Run @0x1f931d0`)
- [Collective Combiners](../hlo-opt/collective-combiners.md) — the `xla::hilo` combiner cluster that consumes emitted collectives
