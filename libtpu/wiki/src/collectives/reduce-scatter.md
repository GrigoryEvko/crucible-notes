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
TensorCore collective stack is built around: the cost model charges `reduce-scatter` (HLO opcode
`93`) through the **same AllReduce-family branch** as `all-reduce`, and the SparseCore-offload
substrate builds a `ReduceScatterOffloadConfig` whose `CollectiveIciStrategyConfig` is produced by
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
  replicated tensor. The cost model encodes this directly — the bandwidth term is `B = 2 ·
  operand_size` (one `operand_size` for the RS phase + one for the AG phase), divided over the
  active torus axes.
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
| Decomposition identity | `AllReduce = AllGather ∘ ReduceScatter` | cost `B = 2·operand_size`; `AllReduceReduceScatterReorder` / `ReduceScatterLegalizer` passes |
| Cost branch | AllReduce-family (`ComputeAllReduceCycles` @`0x130d0040`) | `GetCollectiveCycles` cases `9/11/93` |
| Bandwidth term | `B = 2 · ShapeSize(operand0)`, over `num_dims = popcnt(active axes)` | `GetCollectiveCycles` (`v460 = 2*ShapeSize`; `__popcnt(v465 & 7)`) |
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
the ring is run **per active torus axis** (one ring per dimension the replica-groups span), which is
where the `num_dims` term in the cost model (`§3`) comes from. The per-axis ring direction is
unidirectional — the SC-offload substrate names it explicitly (`ICI_RING_TYPE_UNIDIR_CW` /
`ICI_RING_TYPE_UNIDIR_CCW`), and the dense substrate runs the same per-color ring decomposition via
`StrategyND`'s `UniDirectionNDRingStrategy`.

> **[CONFIRMED]** The RS opcode (`93`) and its operand/output shape handling are byte-anchored in
> `CostModel::GetCollectiveCycles` @`0x130abfc0`: `v96 = *((_BYTE *)a2 + 12)` (the opcode), the
> `if (v96 == 93)` branch @ line 859 reads the **output** shape (`GetShapeSize(v93+88)`) for the
> per-shard size, distinct from the `v96 == 9` (all-reduce) branch which reads the operand at
> `a2+11`. The unidirectional per-axis ring decomposition is the shared AR-family ring (`§3`).
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

### 2.1 The cost model encodes `B = 2 · operand_size`

`reduce-scatter` (`93`), `all-reduce` (`9`), and `all-reduce-start` (`11`) all route to the **same**
AllReduce-family cost branch in `GetCollectiveCycles`. The bandwidth term computed there is `B = 2 ·
operand_size`, the sum of the RS-phase volume and the AG-phase volume:

```text
GetCollectiveCycles @0x130abfc0  (AllReduce-family branch, shared by RS opcode 93)
    v460 = 2 * ShapeSize;                 // B = 2 · operand_size  (RS phase + AG phase)
    ...
    ShapeSize *= 2;                       // the bidirectional doubling preserved through the ND path
    __popcnt((unsigned __int8)v465 & 7);  // num_dims = popcount of active torus axes (mask 0x7 = X/Y/Z)
    // cycles ≈ B / (num_dims · eff_Bps),  eff_Bps = IciGigabytesPerSecond()·0.5·1e9
```

The `2·` factor is the algebraic statement of the decomposition: every all-reduce (and therefore
every reduce-scatter, which is one half of it) pays one operand-size of ring traffic for the reduce
phase and one for the gather phase, spread over `num_dims` active torus axes. There is **no additive
latency term** in this branch — the AR-family cost is pure bandwidth (consistent with the overview's
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

> **[CONFIRMED]** The `2·` bandwidth factor (`v460 = 2 * ShapeSize`, then `ShapeSize *= 2`) and the
> `__popcnt(… & 7)` num-dims term are byte-anchored in `GetCollectiveCycles` @`0x130abfc0`. The
> reorder/legalize passes are nm-confirmed symbols. The byte-identical `ReduceScatterOffloadConfig`
> layout (sizeof `0x48`, vtable `0x21ce1c60`) is confirmed via its ctor @`0x1d6eebe0`.

---

## 3. The per-step reduce and the cost shape

The arithmetic that makes RS *reduce* rather than *gather* is the in-place fold at each landed
shard (`§1` step 2). Although the per-element reduction op runs on the TensorCore datapath rather
than appearing as a discrete cost term, the cost model accounts for the RS phase's transfer volume
explicitly, and the shard-size derivation in `GetCollectiveCycles` is RS-specific:

```text
GetCollectiveCycles @0x130abfc0  (shard-size derivation, lines ~853-888)
    v93   = HloInstruction::operand(a2, 0)                 // operand 0
    v94   = GetShapeSize(operand0)                         // full pre-scatter size
    v96   = opcode
    if (v96 == 93)            v92 = GetShapeSize(OUTPUT)   // RS: per-participant shard = output shape
    else if (v96 == 9)        v92 = GetShapeSize(a2+11)    // AR: full operand again
    else { /* opcode 6 = all-gather, else FATAL "Unsupported collective opcode" */ }
    // general N-shard path:
    v98 = result_size / operand_size                       // shard fraction
    v92 = operand0_size · (v98 - 1)                        // ring traffic = (P-1)/P · size
```

The `v96 == 93` branch reads the **output** shape for the per-shard size because reduce-scatter's
result is `1/P` of its operand — the output shape *is* the shard. (All-reduce, by contrast, has
output == operand, so it reads the operand again.) The `operand0_size · (v98 − 1)` form is the
`(P-1)/P · size` ring-traffic identity written out: a `P`-participant ring moves `P-1` shards.

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

> **[CONFIRMED]** The RS-specific shard-size branch (`v96 == 93` → output-shape size) and the
> `operand0_size · (v98 - 1)` ring-traffic form are byte-anchored at `GetCollectiveCycles`
> @`0x130abfc0` lines ~859 / ~888. The `case 93:` SC-offload probe reading
> `ReduceScatterOffloadConfig` with the `& 2` (`has_ici_strategy_config`) hasbit is at line ~902.

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
