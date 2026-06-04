# ReduceScatter

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — the RS ring loop / cost decomposition and the SC-offload flat-pin cross-checked against the IDA decompile · **Part XIII — On-Pod Collectives & Barriers** / Collective algorithms · [back to index](../index.md)

## Abstract

**ReduceScatter** is the half of an all-reduce that performs the reduction: it sums each
participant's contribution and leaves every participant holding a different *shard* of the
reduced result. On the physical torus this is realized as a **unidirectional ring** in which
each step receives a shard from the up-stream neighbor, reduces it **in place** into the local
accumulator, and forwards the result to the down-stream neighbor. After `P-1` steps (for a ring
of `P` participants) every participant owns exactly one fully-reduced `1/P` shard.

The binary does **not** carry a standalone hand-tuned reduce-scatter emitter. ReduceScatter is
realized through the **`AllReduce = ReduceScatter + AllGather`** decomposition that the
TensorCore collective stack is built around: dedicated HLO passes (`AllReduceReduceScatterReorder`,
`ReduceScatterReassociate`, `ReduceScatterLegalizer`) operate on exactly this boundary, and the
SparseCore-offload substrate builds a `ReduceScatterOffloadConfig` whose `CollectiveIciStrategyConfig` is produced by
the **same templated ring builder** as AllGather and AllReduce. This page documents the ring
reduce-scatter loop, the RS+AG = AR decomposition identity, the per-step reduce, and the
RS-specific SC-offload path; it links the AllGather half, the hierarchical AllReduce, and the
config builder rather than duplicating them.

Contract of ReduceScatter as observed in the binary:

- **The RS ring is unidirectional, length `P` (the ring participant count), `P-1` steps.** Each
  step transfers exactly **one `1/P` shard** (= the result-shape size), reduces it into the local
  accumulator, and forwards. The transfer volume per step is the *output* shape, not the operand
  shape — the operand is the full pre-scatter tensor.
- **ReduceScatter is the reduce phase of AllReduce.** `AllReduce(x) = AllGather(ReduceScatter(x))`:
  RS produces the per-participant reduced shard, AllGather concatenates the shards back to a full
  replicated tensor. The compiler treats RS and AR as two faces of one operation — the dedicated
  reorder/reassociate/legalize HLO passes operate on this boundary. The *dense* all-reduce cost
  branch charges `B = 2 · operand_size` (one `operand_size` for the RS phase + one for the AG
  phase), divided over the active torus axes; the *SC-offload* reduce-scatter cost is charged
  separately, over the per-color ring phases the config builder emits (`§3`).
- **The SC-offload ReduceScatter path is pinned FLAT.** `ConstructConfigForReduceScatterUniDirND`
  @`0x133ccbe0` calls the templated builder with `HierarchicalKind = 0x100` (engaged + false =
  explicitly flat) — RS gets one EXPLICIT-neighbor ring per torus axis, never the multi-phase
  hierarchical decomposition that only AllReduce can take in this build.
- **RS is validated to 1D/2D/3D ND-planes.** The wrapper rejects anything else
  (`"We only support 1D/2D/3D ReduceScatter for now."`) before it ever reaches the ring builder.

## At a glance

| Aspect | Value | Source |
|--------|-------|--------|
| HLO opcode | `reduce-scatter` = `93` | cost-model jump table @`0x130abfc0` (`v96 == 93`) |
| Decomposition identity | `AllReduce = AllGather ∘ ReduceScatter` | `AllReduceReduceScatterReorder` / `ReduceScatterReassociate` / `ReduceScatterLegalizer` passes (nm-confirmed); dense AR branch charges `B = 2·operand_size` |
| Cost branch | SC-offload ring charge (NOT the dense AR branch) | `GetCollectiveCycles` second switch `case 93` @ line 902; opcode `93` is absent from the first (dense) switch and falls to `goto LABEL_447` |
| Cost numerator | `v92 = ShapeSize(operand0)` (the `v96 == 93` branch reads operand 0), charged over the emitted per-color ring phases | `GetCollectiveCycles` (`v92 = GetShapeSize(v93+88)` @ line 868, `v93 = operand(a2,0)` @ line 853) |
| Ring shape | unidirectional, length `P`, `P-1` steps, `1/P` shard/step | AR-family decomposition + SC `IciStrategyRingType` UNIDIR |
| SC-offload builder | `ConstructConfigForCollectiveUniDirNDGroups<ReduceScatterOffloadConfig,…>` @`0x133cd800` | nm-confirmed symbol |
| SC-offload ND wrapper | `ConstructConfigForReduceScatterUniDirND` @`0x133ccbe0` | calls builder with `HierarchicalKind = 256` (FLAT) |
| SC-offload phase split | **pinned FLAT** (`0x100`) — never hierarchical | wrapper `…UniDirNDGroups<…>(…, 256, …)` @ line 80 |
| SC backend config | `ReduceScatterOffloadConfig` (sizeof `0x48`, vtable `0x21ce1c60`) | ctor @`0x1d6eebe0`; cost probe `has_ici_strategy_config` (hasbit `0x2`) |
| ND-plane validation | 1D / 2D / 3D only | `GetCollectiveNDPlaneDimensionCount`; `ValidateReduceScatterReplicaGroupsOrderOnNDPlane` @`0x133cce40` |

---

## 1. The ring reduce-scatter loop

ReduceScatter is the canonical **reduce-into-place ring**. Consider a ring of `P` participants
`r0, r1, …, r(P-1)` connected unidirectionally (`r_i → r_{i+1 mod P}`), each holding a local copy
of the full operand `x_i`, conceptually partitioned into `P` shards `x_i[0], x_i[1], …, x_i[P-1]`.
The reduce-scatter runs `P-1` steps. At step `t` (`0 ≤ t < P-1`), participant `r_i`:

1. **Receives** one shard from its up-stream neighbor `r_{i-1}` — a partial accumulation of the
   shard index `(i - t - 1) mod P`.
2. **Reduces in place**: applies the collective's reduction op (sum, for the embedding-gradient
   case) into its own copy of that same shard index, accumulating the neighbor's partial sum into
   the local one.
3. **Forwards** that now-further-accumulated shard to the down-stream neighbor `r_{i+1}` on the
   next step.

```text
ReduceScatter over a unidirectional ring of P participants (reduction op = ⊕)
each participant starts with the FULL operand, partitioned into P shards [0..P-1]

step t          r_i receives shard s = (i - t - 1) mod P from r_{i-1}
                acc_i[s] ⊕= recv                       (REDUCE IN PLACE)
                send acc_i[s] to r_{i+1}               (FORWARD)

after P-1 steps  r_i holds the fully-reduced shard index (i + 1) mod P :
                 acc_i[(i+1) mod P] = ⊕_{j=0..P-1} x_j[(i+1) mod P]

  invariants
    • per-step transfer volume = ONE shard = (operand_size / P) = OUTPUT-shape size
    • total bytes moved per participant = (P-1)/P · operand_size  ≈ operand_size
    • each participant ends owning exactly ONE 1/P shard of the reduced result
```

The defining feature that distinguishes RS from all-gather is the **reduce in place** at every
step (step 2): the received shard is not merely staged into a growing buffer (that is all-gather)
but folded into the local accumulator with the reduction op. This is why the cost model treats
the RS phase and the AG phase as the **same transfer volume** — both move `(P-1)/P · operand_size`
bytes around the ring — but only RS performs the reduction arithmetic on each landed shard.

On the TPU torus the ring is laid over a torus axis (X/Y/Z). For a multi-dimensional reduce-scatter
the ring is run **per active torus axis** (one ring per dimension the replica-groups span) — the same
per-axis structure the dense all-reduce cost branch counts with its `num_dims = popcnt(active axes)`
term (`§2.1`). The per-axis ring direction is
unidirectional — the SC-offload substrate names it explicitly (`ICI_RING_TYPE_UNIDIR_CW` /
`ICI_RING_TYPE_UNIDIR_CCW`), and the dense substrate runs the same per-color ring decomposition via
`StrategyND`'s `UniDirectionNDRingStrategy`.

> **[CONFIRMED]** The RS opcode (`93`) and its shape handling are byte-anchored in
> `CostModel::GetCollectiveCycles` @`0x130abfc0`: `v96 = *((_BYTE *)a2 + 12)` (the opcode), the
> `if (v96 == 93)` branch @ line 859 reads **operand 0's** shape (`v154 = *(v93+88)`,
> `v93 = operand(a2,0)` @ line 853; `v92 = GetShapeSize(v154)` @ line 868) for the cost numerator,
> then `goto LABEL_113`. The `v96 == 9` (all-reduce) branch instead reads the instruction's own
> result shape at `a2+88` (`*((_QWORD *)a2 + 11)`) — which for all-reduce equals the operand. The
> unidirectional per-axis ring decomposition is carried by the SC-offload ring config (`§4`).
> **[LOW]** The exact `(i - t - 1) mod P` shard-index schedule and the `P-1` step count are the
> standard ring-reduce-scatter algorithm — confirmed *structurally* (UNIDIR ring + per-shard
> output-size transfer + the `2·operand_size` AR-family cost) but the precise per-step shard
> rotation index is not separately emitted as a constant in the cost path traced here; it is
> carried by the per-color `RingLocation` neighbor schedule (see [Routing](../routing/overview.md)).

---

## 2. The AllReduce = ReduceScatter + AllGather identity

The collective stack is built around the textbook bandwidth-optimal all-reduce decomposition:

```text
AllReduce(x)  ≡  AllGather( ReduceScatter(x) )

  ReduceScatter :  P participants, each holds full x  ──►  each holds reduced shard s_i = ⊕_j x_j[i]
  AllGather     :  each holds shard s_i               ──►  each holds the concatenation ⊕_j x_j (full)

  total ring traffic per participant = 2 · (P-1)/P · operand_size   ≈  2 · operand_size
                                       └── RS phase ──┘ └── AG phase ──┘
```

This identity is not merely descriptive — it is **encoded in three places** in the binary:

### 2.1 The dense all-reduce branch encodes `B = 2 · operand_size`

`all-reduce` (`9`) and `all-reduce-start` (`11`) share one jump-table branch (`case 9:`/`case 11:`)
in the *first* (dense) switch of `GetCollectiveCycles`. The bandwidth term computed there is `B = 2 ·
operand_size`, the algebraic sum of the RS-phase volume and the AG-phase volume — the cost-model
expression of the `AllReduce = AllGather ∘ ReduceScatter` decomposition:

```text
GetCollectiveCycles @0x130abfc0  (dense all-reduce branch, case 9/11 — NOT reduce-scatter)
    ShapeSize = GetShapeSize(operand0);   // @line 582 (case 9/11), operand 0 size
    v460 = 2 * ShapeSize;                 // @line 674: B = 2 · operand_size  (RS phase + AG phase)
    ...
    ShapeSize *= 2;                       // @line 707: the doubling preserved through the ND path
    __popcnt((unsigned __int8)v465 & 7);  // @line 727: num_dims = popcount of active axes (mask 0x7 = X/Y/Z)
    // cycles ≈ B / (num_dims · eff_Bps)
```

Reduce-scatter (opcode `93`) does **not** enter this branch: `93` is absent from the first switch
and would fall to its `goto LABEL_447` exit. RS reaches the cost model only as a SparseCore-offload
instruction, through the *second* switch (`case 93:`, `§3`), where the cost numerator is
`v92 = ShapeSize(operand0)` charged over the emitted per-color ring phases — there is no `2·` factor
on the RS path. The `2·` factor on the AR path is nevertheless the algebraic statement of the
decomposition: an all-reduce pays one operand-size of ring traffic for the reduce phase and one for
the gather phase, spread over `num_dims` active torus axes. There is **no additive
latency term** in the AR branch — it is pure bandwidth (consistent with the overview's
"no additive latency term in any collective branch"). The full per-kind formula and the ICI
resource-slot deposits live in [SPMD Link-Count Cost](spmd-link-count-cost.md).

### 2.2 The HLO passes reorder/legalize across the boundary

The compiler carries dedicated HLO passes that operate on exactly this decomposition boundary,
confirming the stack treats RS and AR as two faces of one operation:

- **`AllReduceReduceScatterReorder`** (`HloPassFix` @`0x109611a0` / `0x10961100`) — reorders an
  `all-reduce` followed by a slice into a `reduce-scatter` (the canonical reassociation that turns
  `AllReduce` then per-shard use into the cheaper `ReduceScatter`).
- **`ReduceScatterReassociate`** (`HloPassFix` @`0x109603c0` / `0x10960320`) — hoists reduce-scatter
  through associative arithmetic.
- **`ReduceScatterLegalizer`** (pipeline pass @`0x10969540`) — the jellyfish backend legalizer that
  lowers a `reduce-scatter` into the ring-emittable form the TensorCore path consumes.

### 2.3 The SC-offload backend config is structurally identical to AllReduce/AllGather

The three offload backend-config messages — `AllGatherOffloadConfig`, `AllReduceOffloadConfig`,
`ReduceScatterOffloadConfig` — are **byte-identical in layout** (sizeof `0x48`, generated from the
same field set), differing only in their vtable (`0x21ce1ce0` / `0x21ce1ca0` / **`0x21ce1c60`** for
RS) and typeinfo (`0x21ce6a90` for RS). All three nest the same `ici_strategy_config :
CollectiveIciStrategyConfig` (field 2) carrying the same per-color `phase_rings :
IciStrategyRingConfig` tree. The structural identity is the proto-level expression of the
RS/AG/AR family sharing one ring representation. The full layout is on
[SC-Offload Config Builder](sc-offload-config-builder.md).

> **[CONFIRMED]** The `2·` bandwidth factor (`v460 = 2 * ShapeSize` @ line 674, then `ShapeSize *= 2`
> @ line 707) and the `__popcnt(… & 7)` num-dims term @ line 727 are byte-anchored in
> `GetCollectiveCycles` @`0x130abfc0` — in the dense `case 9:`/`case 11:` (all-reduce) branch, not on
> the reduce-scatter path. The `AllReduceReduceScatterReorder` / `ReduceScatterReassociate` /
> `ReduceScatterLegalizer` passes are nm-confirmed symbols. The byte-identical
> `ReduceScatterOffloadConfig` layout (sizeof `0x48`, vtable `0x21ce1c60`) is confirmed via its ctor
> @`0x1d6eebe0`.

---

## 3. The per-step reduce and the cost shape

The arithmetic that makes RS *reduce* rather than *gather* is the in-place fold at each landed
shard (`§1` step 2). The per-element reduction op runs on the TensorCore datapath rather than
appearing as a discrete cost term; the cost model accounts only for the transfer volume. The
size-derivation `switch` in `GetCollectiveCycles` is shared by the three SC-offload collectives
(opcodes `93`, `9`, `6`), and reduce-scatter takes the simplest leg of it:

```text
GetCollectiveCycles @0x130abfc0  (size derivation, lines 853-888)
    v93 = HloInstruction::operand(a2, 0)                   // operand 0          (line 853)
    v94 = v95 = GetShapeSize(operand0)                     // full pre-scatter size (line 854)
    v96 = opcode                                           // a2+12              (line 858)
    if (v96 == 93)       v92 = GetShapeSize(v93+88) ──────►// RS: OPERAND 0 size  (line 868) ; goto LABEL_113
    else if (v96 == 9)   v92 = GetShapeSize(a2+88)  ──────►// AR: result shape (== operand) ; goto LABEL_113
    else if (v96 != 6)   FATAL("Unsupported collective opcode")
    // opcode 6 = all-gather only — falls through to the shard-fraction path:
    v97 = GetShapeSize(a2+88)                              // AG: result shape   (line 883)
    v98 = v97 / v95                                        // shard fraction = result / operand
    v92 = GetShapeSize(operand0) · (v98 - 1)               // AG ring traffic    (line 888)
```

The `v96 == 93` branch reads **operand 0's** shape (`v93+88`, `v93 = operand(a2,0)`) and jumps
straight to `LABEL_113` — the cost numerator for reduce-scatter is the full operand size, which the
later loop divides over the emitted ring phases. (All-reduce reads `a2`'s own result shape at
`a2+88`; for AR that equals the operand.) The `operand0_size · (v98 − 1)` shard-fraction form is the
**all-gather** leg (opcode `6`), reached only when neither `93` nor `9` matches — it is *not* on the
reduce-scatter path.

For the **SparseCore-offload** path, the cost model probes `GetCollectiveOffloadConfig`
@`0x133e1740` and, for opcode `93`, dereferences the `reduce_scatter_offload_config()` and asserts
`has_ici_strategy_config()` (hasbit `0x2` on the config message):

```text
GetCollectiveCycles @0x130abfc0  (SC-offload probe, case 93, lines ~890-908)
    GetCollectiveOffloadConfig(&cfg, a2)
    CHECK(collective_offload_config != nullopt)            // line 688: "!= std::nullopt"
    switch (opcode) {
      case 93:                                              // reduce-scatter
        cfg = cfg ?: &ReduceScatterOffloadConfig_globals_
        if ((cfg[2] & 2) == 0)                              // has_ici_strategy_config hasbit
          CHECK("…reduce_scatter_offload_config().has_ici_strategy_config()")
        break;                                              // charge the SC ring operating point
      case 9: … // all-reduce reads AllReduceOffloadConfig
    }
```

When the SC config is present the cost charges the per-color UNIDIR ring set the builder emitted,
not the dense TC operating point — the same probe-and-charge the overview describes for the SC
substrate.

> **[CONFIRMED]** The RS size branch (`v96 == 93` → `GetShapeSize(operand0)`, line 868, jumping
> straight to `LABEL_113`) is byte-anchored at `GetCollectiveCycles` @`0x130abfc0` lines 859/868;
> the `operand0_size · (v98 - 1)` shard-fraction form @ line 888 belongs to the **all-gather**
> (opcode `6`) leg, not RS. The `case 93:` SC-offload probe reading `ReduceScatterOffloadConfig`
> with the `& 2` (`has_ici_strategy_config`) hasbit is at decompile line 902 (source `cost_model.cc`
> line 702).

---

## 4. The SC-offload ReduceScatter path (pinned FLAT)

The SparseCore-offload substrate builds a `ReduceScatterOffloadConfig` through the **same templated
builder** as AllGather and AllReduce —
`ConstructConfigForCollectiveUniDirNDGroups<ReduceScatterOffloadConfig, HloReduceScatterInstruction>`
@`0x133cd800` — driven by the public ND wrapper `ConstructConfigForReduceScatterUniDirND`
@`0x133ccbe0`. The wrapper's job is to validate the instruction and then call the builder with a
**fixed `HierarchicalKind`**.

### 4.1 The wrapper: validation then a flat-pinned builder call

```text
ConstructConfigForReduceScatterUniDirND @0x133ccbe0
  1. RetCheck(reduce_scatter != nullptr)                              // line 1832
  2. IsSupportedReduceScatter(target, hlo)         else bail          // line 1833
  3. GetCollectiveNDPlaneDimensionCount(...) == 1   (must be a single ND-plane)
       else bail (line 1837)
  4. dim_count ∈ {1, 2, 3}   else RetCheck                            // line 1838
       "We only support 1D/2D/3D ReduceScatter for now."
  5. ValidateReduceScatterReplicaGroupsOrderOnNDPlane(...)  @0x133cce40   else bail (line 1843)
  6. ConstructConfigForCollectiveUniDirNDGroups<ReduceScatterOffloadConfig,…>(
         …, /* HierarchicalKind */ 256 /* = 0x100 = engaged+false = FLAT */, …)
```

The literal **`256` (= `0x100`)** at step 6 is the byte-anchored proof that RS is **pinned flat**:
`HierarchicalKind & 0x101 == 0x100` is "engaged + false" — explicitly the single-phase flat ring
path. The builder's flat-vs-hierarchical dispatch (`and $0x101 ; cmp $0x100`) therefore always
selects FLAT for reduce-scatter. RS can **never** reach the multi-phase hierarchical deque walk;
only AllReduce can, and only when `xla_tpu_enable_sparse_core_hierarchical_all_reduce` is engaged
+ true. The `HierarchicalKind` packing is documented on [HierarchicalKind](hierarchical-kind.md).

### 4.2 What the flat RS builder emits

Because RS is flat, the builder emits **one EXPLICIT-neighbor ring per torus axis** (no D2D
intra-chip multi-phase split beyond the megacore-gated phase-0 ring shared by all three kinds).
Per axis the emitted `IciStrategyRingConfig` carries:

| Field (offset) | RS flat value | Meaning |
|----------------|---------------|---------|
| `ring_neighbor` (`0x24`) | `ICI_RING_NEIGHBOR_EXPLICIT` = `1` | neighbor schedule via a precomputed table |
| `core_count` (`0x18`) | computed ring length | the per-axis ring participant count |
| `ring_neighbor_table_offset` (`0x28`) | from `RingConfigAttributes[ringDim]` | offset into the neighbor-reorder table |
| `has_reordering_map` (`0x3d`) | from `RingConfigAttributes[ringDim]` | whether a reorder map applies |
| `explicit_strategy_ring_dim` (`0x48`) | the per-axis `ringDim` | which torus axis this ring runs over |
| `partner_transfers_outside_the_ring` (`0x3e`) | `false` | — |

The builder shares the identical body, deque-of-`tuple<IciStrategyRingDim,long,long>`, twisted-torus
gate, and per-color appender lambda (`@0x133e0c00` for RS) with the AllGather/AllReduce
instantiations. The RS instantiation differs only in the `*OffloadConfig` type it constructs
(vtable `0x21ce1c60`) and in being reached exclusively through the flat-pinned wrapper. The full
field map, the per-color emission loop, and the `GetDimensionRings` per-axis partitioner are on
[SC-Offload Config Builder](sc-offload-config-builder.md).

> **[CONFIRMED]** `ConstructConfigForReduceScatterUniDirND` @`0x133ccbe0` calls the templated
> builder with the literal `256` (FLAT) — byte-anchored at the decompiled call site. The 1D/2D/3D
> gate (`"We only support 1D/2D/3D ReduceScatter for now."`), `IsSupportedReduceScatter`,
> `GetCollectiveNDPlaneDimensionCount`, and `ValidateReduceScatterReplicaGroupsOrderOnNDPlane`
> @`0x133cce40` are all present in the wrapper. The RS builder @`0x133cd800` is nm-confirmed and
> shares the AllReduce builder body (same `HierarchicalKind` param, deque, twisted-torus path,
> `across_cores_on_chip` D2D ring).

---

## 5. Relationship to the rest of the collective stack

| Component | This page | Sibling page |
|-----------|-----------|--------------|
| The reduce-scatter ring loop + per-step reduce | **here (§1, §3)** | — |
| `AllReduce = RS + AG` decomposition identity | **here (§2)** | — |
| The all-gather half (`GetShardIndex`/`GetOffset`, 2D/3D ring) | linked | [AllGather ND-Ring](allgather-nd-ring.md) |
| The hierarchical multi-phase AllReduce (`0x101` path, pincer fusion) | linked | [AllReduce Hierarchical / Pincer](allreduce-hierarchical-pincer.md) |
| The latency-bound AR emitters (binomial / recursive-doubling) | linked | [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) |
| The SC-offload config builder (the templated ring builder + proto) | linked | [SC-Offload Config Builder](sc-offload-config-builder.md) |
| The `HierarchicalKind` flat-vs-hierarchical flag | linked | [HierarchicalKind](hierarchical-kind.md) |
| The dense ICI all-reduce primitive | linked | [ICI All-Reduce Primitive](../ici/all-reduce-primitive.md) |
| Per-kind cost formulas + ICI resource slots | linked | [SPMD Link-Count Cost](spmd-link-count-cost.md) |

ReduceScatter sits at the center of the all-reduce family: it is the **reduce** half whose output
shards the AllGather half then re-concatenates. On the dense TensorCore substrate it is selected and
emitted by the shared `StrategyND` machinery (see [SelectNDStrategy](strategy-nd-picker.md)); on the
SparseCore-offload substrate it is the flat-pinned instantiation of the shared config builder. Either
way the reduce-scatter primitive itself — receive a shard, reduce in place, forward — is the
invariant that the surrounding routing, twist, and barrier subsystems schedule around.

---

## Cross-References

**The all-reduce family**
- [AllGather ND-Ring](allgather-nd-ring.md) — the all-gather half of the decomposition (`GetShardIndex`/`GetOffset`, 2D/3D selector)
- [AllReduce Hierarchical / Pincer](allreduce-hierarchical-pincer.md) — the multi-phase `0x101` all-reduce + pincer fusion
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — latency-bound all-reduce emitters
- [ICI All-Reduce Primitive](../ici/all-reduce-primitive.md) — the dense ICI all-reduce on the fabric

**SparseCore-offload substrate**
- [SC-Offload Config Builder](sc-offload-config-builder.md) — `ConstructConfigForCollectiveUniDirNDGroups<*>` and the `*OffloadConfig` proto / `IciStrategyRingConfig` field map
- [HierarchicalKind](hierarchical-kind.md) — the `AutoOr<bool>` flat (`0x100`) vs hierarchical (`0x101`) phase split

**Cost & selection**
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — `GetCollectiveCycles` per-kind formulas, the `2·operand_size` AR-family term, ICI resource slots
- [SelectNDStrategy](strategy-nd-picker.md) — the dense ND-strategy picker that emits the per-color ring decomposition

**Section map**
- [On-Pod Collectives — Section Map](overview.md) — the substrate split, end-to-end flow, and op-family dispatch
- [back to index](../index.md)
