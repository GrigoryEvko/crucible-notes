# SPMD Link-Count Divisor & Collective Cost Model

> *Addresses, vtable offsets, and `.rodata` constants apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All addresses are VMA; in this image `.text` VMA == file offset.*

## Abstract

The TPU collective cost model lives in two distinct estimators that share one set of ICI bandwidth constants but answer different questions. The **SPMD partitioner** estimator (`GetCommunicationTimeInMilliSec`) produces a wall-clock millisecond figure used to compare candidate *shardings* — it is the cost the [strategy / sharding picker](strategy-nd-picker.md) compares against alternatives. The **bundle** estimator (`CostModel::GetCollectiveCycles`) produces a TensorCore-cycle figure deposited into the 23-slot `ResourceVector`, which the scheduler's `MaxResourceCycles` reduction consumes — it is one of five pricing arms reached from [`GetHloResourcesImpl`](../cost/gethloresources-routing.md).

This page recovers the three pieces both estimators rest on:

1. The **per-link count divisor** in the SPMD ms formula. It is *not* a chip-parameter field and *not* a flat replica-group cardinality. It is **topology-derived**: the virtual `TpuSpmdPartitioningVisitor::GetCommunicationMultiplier` (visitor vtable slot `+0x488`) returns `ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims + 1` — the number of ND-plane torus-mesh dimensions the collective's replica-groups span, plus one.
2. The **SPMD wall-clock formula** itself, `time_ms = (bytes/1e9) / (link_count · ici_GBps) · 1000`, including the multi-slice `6.0 GB/s` inter-slice override.
3. The **full per-kind `GetCollectiveCycles` formula set** — all-gather, all-reduce, reduce-scatter, all-to-all, ragged-all-to-all, collective-permute — with each kind's bandwidth divisor, the shared effective-bandwidth term `eff_Bps = ici_GBps · 0.5 · 1e9`, the `seconds→cycles` scale, and the per-torus-dimension `ResourceVector` slot deposits (`R[13..18]`).

For reimplementation, the contract is:

- **The divisor identity.** `GetCommunicationMultiplier` (vtable `+0x488`, `@0x127a16c0`) → `ReplicaGroupsOnNDPlane(…, plane=2, false).num_mesh_dims + 1`, with the default-`1`, no-DeviceAssignment, and multi-slice-single-group short-circuits.
- **The SPMD ms formula** with the `6.0` GB/s DCN/OCS override and the `1e9` / `1000.0` constants.
- **The shared `eff_Bps` halving** (×0.5 = per-direction of a bidirectional ICI ring) and the `freq_MHz · 1e6` cycle scale (encoded as `· 1000.0` twice).
- **The `HloOpcode` dispatch** (jump table `@0xae0e9e0`, index = `opcode − 6`) and the zero-cost default for async shells and broadcast.
- **Each per-kind bandwidth divisor** (all-gather 1D ÷2 / 2D ÷4; all-reduce ÷`2·num_dims`; all-to-all ÷`links/per_link`; collective-permute ÷1) and the `R[13..18]` ICI slot deposit rule.

| | |
|---|---|
| **SPMD divisor (vtable +0x488)** | `TpuSpmdPartitioningVisitor::GetCommunicationMultiplier` @ `0x127a16c0` |
| **Divisor identity** | `ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims + 1` (topology-derived) |
| **SPMD ms estimator** | `TpuSpmdPartitioningVisitor::GetCommunicationTimeInMilliSec` @ `0x127a19e0` |
| **Bundle cycle estimator** | `CostModel::GetCollectiveCycles` @ `0x130abfc0` |
| **Opcode dispatch** | jump table @ `0xae0e9e0`, index = `opcode − 6` |
| **Effective bandwidth** | `eff_Bps = IciGigabytesPerSecond() · 0.5 · 1e9` |
| **ICI ResourceVector slots** | `R[13..18]` = `Ici{Y,X,Z}{Plus,Minus}` (3 torus dims × 2 directions) |
| **Cycle conversion** | `cycles = volume/divisor · TC_freq_MHz · 1e6` |
| **Constants** | `0.5` @ `0xa2df5c8` · `1e9` @ `0xa2de620` · `1000.0` @ `0xa2e0430` · `6.0` @ `0xa2de720` · `4.0` @ `0xa2de830` |

---

## Part 1 — The SPMD Link-Count Divisor

### The slot resolution

The SPMD sharding estimator `TpuSpmdPartitioningVisitor::GetCommunicationTimeInMilliSec` (`@0x127a19e0`) divides byte volume by a product of two terms read through virtual calls on the visitor `this`:

- `IciGigabytesPerSecond()` — the per-gen aggregate ICI bandwidth (`Target` vtable `+0x5d8`, decompiled as `(*(…)(*this + 1496))(this)`).
- the **link-count divisor** — the visitor's own vtable slot `+0x488`.

Itanium-ABI resolution of `+0x488`: the `vtable for TpuSpmdPartitioningVisitor` symbol sits at `0x218e2508`; the object's vptr is `sym + 0x10 = 0x218e2518`; slot `+0x488` is at `0x218e29a0`; the `R_X86_64_RELATIVE` reloc stored there points to `0x127a16c0` = `TpuSpmdPartitioningVisitor::GetCommunicationMultiplier(CollectiveDeviceListBase const&)`. The neighbouring slots confirm the layout: `+0x480 → 0x127a19e0` (the ms estimator itself) and `+0x478 → 0x127a15e0` (`GetComputationTimeInMilliSec`).

> **NOTE —** the divisor is a virtual method *on the visitor*, not on `Target`. There is no `Target::NumIciLinks` accessor in this build; the per-link bandwidth (`ICIPerLinkDataRate`, the ~2-SerDes-per-direction figure) is a separate *bandwidth* constant, not this *count*. The two multiply in the formula but are computed in different places. See [ICI Overview](../ici/overview.md).

### What `GetCommunicationMultiplier` computes

The decompile of `@0x127a16c0` is short and byte-exact:

```c
uint GetCommunicationMultiplier(visitor *this, CollectiveDeviceListBase &devList) {
    if (!this->device_assignment /* this+0x568 */)              // no DeviceAssignment
        return 1;                                               // default multiplier
    if (Target::GetMultiSliceTopology(this->target /* +0x558 */)) {
        // MULTI-SLICE: build the cross-slice transfer groups (mode 3)
        g = TransferStrategy::ConstructSliceTransferGroup(mode=3, devList, …);
        if (single_cross_slice_group)                           // g has one group
            return 1;                                           // one group ⇒ 1×
        localize → ToReplicaGroup(mode=3) →
        nd = ReplicaGroupsOnNDPlane(target, devAssign, devList, plane=2, false);
        return (uint8)nd.result_byte + 1;                       // [A]
    } else {
        // SINGLE-SLICE:
        nd = ReplicaGroupsOnNDPlane(target, devAssign, devList, plane=2, false);
        return (uint8)nd.result_byte + 1;                       // [B]
    }
}
```

In the IDA listing this is: `v2 = 1; if (!*((_QWORD*)this + 173)) return v2;` (field `+0x568` = `173·8`); the single-slice arm calls `ReplicaGroupsOnNDPlane(&v26, target, devAssign, a2, 2, 0)` then `v7 = v27[8]; return v7 + 1`; the multi-slice arm calls `ConstructSliceTransferGroup(…, 3, …)`, returns `1` when the group count check fails, else re-derives via `ReplicaGroupsOnNDPlane(…, 2, 0)` → `v7 = v24[16]; return v7 + 1`. Both arms pass `plane = 2` and `bool = false`, take the result struct's first byte (`movzbl`), and add one.

> **The divisor identity:** `link_count = ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims + 1`.

### Why it is topology-derived

`ReplicaGroupsOnNDPlane` (`@0x1c890960`) returns a small struct `{ok_byte, vector<MeshNDInfo>}`, memoized in a `NodeHashMap` keyed on `NDPlaneCacheKey`. It decomposes the collective's replica-groups onto the physical torus by:

- mapping each logical device id to its physical TensorCore location (`TensorCoreLocationForLogicalDeviceId` @ `0x1c8904e0`),
- reading each device's physical chip coordinates (`TpuCoreLocation::Chip` @ `0x20ad6720`),
- building one `MeshNDInfo` per torus-mesh *dimension* the groups span (`ReplicaGroupsOnNDPlaneImpl::$_0` @ `0x1c896400`; the implementation compares the resulting vector size against `2`).

The returned `result_byte` is that **mesh-dimension count** — how many torus axes the collective's groups extend across. The `+1` floors a 0-dimensional / degenerate group at `1×` bandwidth.

The semantic interpretation ("number of ND-plane mesh dimensions the replica-groups span") is read from the worker's structure, not from a label; the *byte = mesh-dim count, then `+1`* is byte-exact, and the chip-coordinate call chain is fully traced. The exact field-by-field layout of `MeshNDInfo` and the inner dimension-collapse arithmetic were not decoded (see [Confidence & Open Items](#confidence--open-items)).

### The SPMD wall-clock formula

`GetCommunicationTimeInMilliSec` (`@0x127a19e0`) assembles:

```c
ici_GBps   = target.IciGigabytesPerSecond();                 // Target vtable +0x5d8
if (multi_slice && single_cross_slice_transfer_group)
    ici_GBps = 6.0;                                          // const 0xa2de720 — DCN/OCS inter-slice rate
link_count = this->GetCommunicationMultiplier(devList);      // visitor vtable +0x488
           = ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims + 1;
time_ms    = (bytes / 1e9) / (link_count * ici_GBps) * 1000.0;
//           const 0xa2de620 = 1e9 ; const 0xa2e0430 = 1000.0
```

In the decompile the tail is `vdivsd ÷0xa2de620(=1e9)`, `vmulsd ×var_30(=link_count·ici)`, `vdivsd`, `vmulsd ×0xa2e0430(=1000.0)` — bytes→GB, divide by the product, milliseconds. The `0xa2de720 = 6.0` multi-slice override appears only in this estimator; the bundle estimator never reads it.

> **GOTCHA —** the `6.0` override fires only when there is a *single* cross-slice transfer group on a multi-slice topology — that is the DCN/OCS inter-slice link rate, far below the intra-slice ICI rate. When the collective spans multiple cross-slice groups, the regular `ReplicaGroupsOnNDPlane(plane=2)+1` divisor applies. A reimplementation that always uses `6.0` on multi-slice will mis-price multi-group cross-slice collectives.

| Function | Address | Role |
|---|---|---|
| `GetCommunicationTimeInMilliSec` | `0x127a19e0` | SPMD sharding ms estimator; formula + `6.0` override |
| `GetCommunicationMultiplier` | `0x127a16c0` | vtable `+0x488` divisor = `ReplicaGroupsOnNDPlane(plane=2)+1` |
| `ReplicaGroupsOnNDPlane` | `0x1c890960` | `plane=2` mesh-dim count; memoized `NDPlaneCacheKey` |
| `ReplicaGroupsOnNDPlaneImpl::$_0` | `0x1c896400` | per-replica-group mesh-dim worker (vec size vs `2`) |
| `TensorCoreLocationForLogicalDeviceId` | `0x1c8904e0` | logical id → physical TC location |
| `TpuCoreLocation::Chip` | `0x20ad6720` | physical chip coordinates |
| `ConstructSliceTransferGroup` | `0x14b8ca20` | multi-slice cross-slice transfer groups (mode 3) |
| `Target::GetMultiSliceTopology` | `0x1d617980` | multi-slice topology probe |

---

## Part 2 — The Bundle Cost Model: `GetCollectiveCycles`

`CostModel::GetCollectiveCycles` (`@0x130abfc0`) is the **bundle** collective estimator. It is reached from the collective arm of [`GetHloResourcesImpl`](../cost/gethloresources-routing.md) (`IsSupportedCollectiveHlo` → `GetCollectiveCycles`, depositing into `rv[+8]` as scalar cycles plus the per-link `ResourceVector` slots). Unlike the SPMD estimator it models the **intra-slice per-torus-dimension ICI rings** and never applies the `6.0` multi-slice override.

### The shared effective-bandwidth term

The function opens by reading the per-gen ICI rate and halving it:

```c
ici_GBps = target.IciGigabytesPerSecond();   // Target vtable +0x5d8 (1496) @0x130abfe3
eff_Bps  = ici_GBps * 0.5 * 1e9;             // const 0xa2df5c8 = 0.5 ; 0xa2de620 = 1e9
// seconds → cycles scale = TC_freq_MHz * 1e6, encoded as (· 1000.0) twice (0xa2e0430)
```

The decompile shows exactly this: `vmulsd xmm0, qword_A2DF5C8` (×0.5) immediately after the vtable-`+0x5d8` call, then `vmulsd xmm1, qword_A2DE620` (×1e9). Each per-kind branch converts seconds to cycles with `TensorCoreFrequencyInMegaHertz()` and `vmulsd …, 0xa2e0430` applied twice (`·1000·1000 = ·1e6`).

> **NOTE —** the **×0.5** halving models the ICI ring as bidirectional, charging only per-direction aggregate bandwidth. There is **no additive latency term** in any collective branch — no `InitialDmaLatency` call, no fixed-ns add. The bundle collective cost is pure bandwidth, expressed in TensorCore cycles.

### The opcode dispatch

After the `eff_Bps` setup, dispatch is a `switch(opcode)` compiled to a self-relative jump table at `.rodata 0xae0e9e0` with index `opcode − 6` (`v17 = (uint)(v15 - 6)` in the decompile). `HloOpcode` integers are verified byte-exact via the `HloOpcodeString` length table.

| opcode | name | branch |
|---:|---|---|
| 6 | `all-gather` | AllGather branch @ `0x130ac06c` |
| 8 | `all-gather-start` | AllGather branch @ `0x130ac06c` |
| 9 | `all-reduce` | AllReduce branch @ `0x130ac14c` |
| 11 | `all-reduce-start` | AllReduce branch @ `0x130ac14c` |
| 12 | `all-to-all` | tail-call `ComputeAllToAllCycles` @ `0x130ae8e0` |
| 34 | `collective-permute` | CollectivePermute branch @ `0x130ac40f` |
| 36 | `collective-permute-start` | CollectivePermute branch @ `0x130ac40f` |
| 86 | `ragged-all-to-all` | tail-call `ComputeRaggedAllToAllCycles` @ `0x130aea80` |
| 93 | `reduce-scatter` | AllReduce-family path (`ComputeAllReduceCycles`) |
| 7, 10, 33, 35 | `-done` / `collective-broadcast` / `cp-done` | default @ `0x130ae546` ⇒ **0 cycles** |

> **GOTCHA —** the async shells (`-start` / `-done`) and `collective-broadcast` contribute zero ICI bundle cost; the cost is charged on the *data-carrying* opcode. Opcodes 7/10/13..33/35 all `goto` the zero-cost label. A reimplementation that charges both the `-start` and the `-done` shell double-counts.

### The per-torus-dimension ICI slots

`GetResourceFromIciResource` (`@0x1c894c00`) maps the `IciResource` enum to a `ResourceVector` slot index. The decompile returns `v1 + 0x10000000D` for a valid resource — i.e. base slot `0xD` (= 13) in the low dword plus a `0x100000000` "present" flag in the high dword. Callers test `& 0x100000000` for validity and use the low dword as the slot:

| slot | offset | name | torus axis / direction |
|---:|---|---|---|
| `R[13]` (`0xd`) | `+0x68` | `IciYPlus` | dim Y, + |
| `R[14]` (`0xe`) | `+0x70` | `IciYMinus` | dim Y, − |
| `R[15]` (`0xf`) | `+0x78` | `IciXPlus` | dim X, + |
| `R[16]` (`0x10`) | `+0x80` | `IciXMinus` | dim X, − |
| `R[17]` (`0x11`) | `+0x88` | `IciZPlus` | dim Z, + |
| `R[18]` (`0x12`) | `+0x90` | `IciZMinus` | dim Z, − |

These are exactly the six ICI ring-link slots named in the [Resource Enum (23-slot)](../cost/resource-enum.md): three torus dimensions × two ring directions. The per-kind branches deposit cycles into these slots with `ResourceVector::Acc(a3, slot, cycles)`. The [degraded-axis path](degraded-axis.md) (`GetDegradedAxis` @ `0x1c894c20`, `Target::Is{X,Y,Z}Degraded` @ `0x1d615940`/`0x1d615960`/…) drops a dimension's two slots when that torus axis is degraded.

---

## Part 3 — Per-Kind Formulas

Notation: `B` = byte volume via `GetShapeSize` (`@0x130aec20`); `eff_Bps` as in Part 2; `cyc(x) = x · TC_freq_MHz · 1e6`. Deposits land in the `R[13..18]` slots active for the collective's topology dimensions.

### All-gather (opcode 6 / 8) — `@0x130ac06c`

```c
n = LogicalDeviceCount ratio = out_shape_size / in_shape_size;   // idiv @0x130ac141
B = (n - 1) * output_shape_size;                                 // (n-1) shards transferred
if (UseAllGather2D)   c = cyc(B / (4 * eff_Bps));                // const 0xa2de830 = 4.0
else                  c = cyc(B / (2 * eff_Bps));                // 1D bidirectional ring (×2)
// deposit c into R[13/14], R[15/16], R[17/18] gated by topology dim bitmask &1/&2/&4
```

`AllGatherEmitter::UseAllGather2D` (`@0x13801740`) selects the path: the 2D ring uses 2 torus dims × 2 directions = 4 concurrent links (divisor `4`), the 1D ring uses one dim's 2 directions (divisor `2`). In the decompile the 2D path multiplies by `qword_A2DE830` (4.0); the 1D path does `vaddsd xmm1,xmm1,xmm1` (×2). Deposits are gated by `& 1` → `Acc(13)`+`Acc(14)`, `& 2` → `Acc(15)`+`Acc(16)`, `& 4` → `Acc(17)`+`Acc(18)`.

### All-reduce (opcode 9 / 11) — `@0x130ac14c`

```c
B = 2 * operand_shape_size;                          // ReduceScatter + AllGather = 2× volume
if (DoesReplicaGroupFormNDPlane(...)) {               // fills NDTopologyInfo dim bitmask
    num_dims = popcnt(NDtopo_dims & 7);               // active torus axes (__popcnt(&7))
    c = cyc(B / (2 * num_dims * eff_Bps));
    // deposit c into the 2 slots of EACH active dim (test &1→13,14 ; &2→15,16 ; &4→17,18)
} else {                                              // cross-module / non-ND-plane fallback
    c = ComputeAllReduceCycles::$_0(...);             // @0x130d0040
    //  B' = AllReduce.size_field ; c = cyc(B' / (2 * eff_Bps))   // single bidirectional ring
    //  links = EstimatePhysicalLinksUsed(...) ; deposit into all 6 slots by resource
}
```

`IsCrossModuleReduceInstruction` (`@0x137dac40`) and `DoesReplicaGroupFormNDPlane` (`@0x1c88bfc0`) decide the path; the decompile shows `v460 = 2 * ShapeSize`, `__popcnt((v465)&7)`, then `vmulsd …, var_58(eff_Bps)`, `vdivsd`. The cross-module fallback delegates to the `ComputeAllReduceCycles::$_0` lambda which uses `EstimatePhysicalLinksUsed` and deposits across all six slots.

### Reduce-scatter (opcode 93) — AllReduce-family

Handled in the AllReduce path (the `opcode == 93` shape-ratio arm and the SparseCore `GetCollectiveOffloadConfig` probe at `0x133e1740`). It is modeled as the **reduce-scatter phase** of the all-reduce decomposition: the same per-dim ring deposit with the same `eff_Bps / num_dims` term — the half-cost twin of all-reduce (the all-gather phase is the complementary half). There is no separate jump-table slot.

### All-to-all (opcode 12) and ragged-all-to-all (opcode 86)

Both tail-call into `ComputeAllToAllCyclesHelper` (`@0x130d02a0`); the ragged variant sets a `bool` flag but shares the formula:

```c
B        = operand_shape_size * group_size;          // imul
links    = EstimatePhysicalLinksUsed(...);           // @0x1c8939c0 — |sorted distinct IciResource set|
per_link = per_link_table[is2D];                     // table {1D→2.0, 2D→4.0}
c        = cyc(B * per_link / links / eff_Bps);      // vmul [tbl+is2D*8] ; ÷links ; ÷eff_Bps
// deposit c into ALL 6 slots R[13..18] — all-to-all saturates every ICI link
```

The decompile: `vmulsd xmm2, qword [rax+rcx*8]` (per-link table indexed by `is2D`), `vdivsd xmm1, xmm2, xmm1` (÷`links`), `vdivsd xmm0, xmm0, xmm1` (÷`eff_Bps`), with `EstimatePhysicalLinksUsed` called just above. The `4.0` constant (`0xa2de830`) appears as the 2D table entry.

### Collective-permute (opcode 34 / 36) — `@0x130ac40f`

```c
B = GetShapeSize(operand);
c = cyc(B / eff_Bps);                                // single-direction point-to-point (NO ×2)
if (AllPairsUseSameIciLink(target, devAssign, source_target_pairs, channel_id)) {
    r = GetResourceFromIciResource(resource_id);     // narrow to ONE ICI slot
    Acc(a3, r, c);
} else {
    Acc(a3, 13, c); Acc(a3, 14, c); Acc(a3, 15, c); Acc(a3, 16, c); Acc(a3, 17, c); Acc(a3, 18, c);
}
```

Collective-permute is the only kind that divides by `eff_Bps` alone (no `×2` factor) — a directed point-to-point transfer along one ring direction. `AllPairsUseSameIciLink` (`@0x1c88de40`) detects whether every `(src,dst)` pair rides one ICI link; if so the deposit narrows to the single resource slot returned by `GetResourceFromIciResource`, otherwise the cost is spread across the lower ICI slots.

---

## Per-Kind Formula Summary

| collective (opcode) | volume `B` | bandwidth divisor | dirs | slots deposited |
|---|---|---|---:|---|
| all-gather (6/8) 1D | `(n−1)·out_size` | `2 · eff_Bps` | 2 | per-dim `R[13..18]` |
| all-gather (6/8) 2D | `(n−1)·out_size` | `4 · eff_Bps` | 4 | per-dim (2 dims) |
| all-reduce (9/11) ND-plane | `2 · operand_size` | `2·num_dims · eff_Bps` | 2/dim | per active dim (2 each) |
| all-reduce cross-module | `AR.size_field` | `2 · eff_Bps` | 2 | all 6 (by `links`) |
| reduce-scatter (93) | `operand_size` (RS phase) | `2·num_dims · eff_Bps` | 2/dim | per active dim |
| all-to-all (12) | `op_size·group_size` | `links / per_link · eff_Bps` | var | all 6 `R[13..18]` |
| ragged-all-to-all (86) | `op_size·group_size` | `links / per_link · eff_Bps` | var | all 6 |
| collective-permute (34/36) | `operand_size` | `1 · eff_Bps` | 1 | `R[13/15/17]±` or 1 slot |
| `*-done` / broadcast / cp-done | — | — | — | 0 cycles (default) |

`eff_Bps = IciGigabytesPerSecond · 0.5 · 1e9`; `cycles = volume/divisor · TC_freq_MHz · 1e6`. `num_dims = popcnt(active torus axes)`; `links = EstimatePhysicalLinksUsed`; `per_link = {1D→2.0, 2D→4.0}`.

| Function | Address | Role |
|---|---|---|
| `CostModel::GetCollectiveCycles` | `0x130abfc0` | bundle collective estimator; `eff_Bps` + opcode dispatch |
| `ComputeAllToAllCycles` | `0x130ae8e0` | opcode-12 entry |
| `ComputeRaggedAllToAllCycles` | `0x130aea80` | opcode-86 entry |
| `ComputeAllToAllCyclesHelper` | `0x130d02a0` | `B·per_link/links/eff_Bps`; per-link table `{2,4}` |
| `ComputeAllReduceCycles::$_0` | `0x130d0040` | cross-module AR fallback; all-6-slot deposit |
| `GetResourceFromIciResource` | `0x1c894c00` | `IciResource → slot 0xD..0x12` + present flag |
| `EstimatePhysicalLinksUsed` | `0x1c8939c0` | topology link counter (chip-coordinate walk) |
| `AllGatherEmitter::UseAllGather2D` | `0x13801740` | 1D ÷2 vs 2D ÷4 selection |
| `DoesReplicaGroupFormNDPlane` | `0x1c88bfc0` | fills `NDTopologyInfo` dim bitmask |
| `AllPairsUseSameIciLink` | `0x1c88de40` | collective-permute single-link narrowing |
| `GetShapeSize` | `0x130aec20` | byte volume `B` |

---

## Confidence & Open Items

The byte-exact, decompile-confirmed core (this page's primary claims):

- The vtable `+0x488` → `GetCommunicationMultiplier` reloc resolution and its control flow (default `1`, no-DeviceAssignment short-circuit, multi-slice single-group `1×`, `ReplicaGroupsOnNDPlane(plane=2).result_byte + 1`). **CERTAIN.**
- The SPMD ms formula, the `6.0` multi-slice override, and the `1e9` / `1000.0` constants. **CERTAIN.**
- The `eff_Bps = ici · 0.5 · 1e9` term, the `opcode − 6` jump table, every per-kind bandwidth divisor (AG 1D÷2 / 2D÷4, AR ÷`2·num_dims`, all-to-all ÷`links/per_link`, CP ÷1), and the `R[13..18]` slot deposits. **CERTAIN.**
- The `HloOpcode` integer → name map for all collective opcodes (length-verified). **CERTAIN.**

Items confirmed via formula shape rather than a named constant, and items not field-decoded:

- The exact integer `ReplicaGroupsOnNDPlaneImpl::$_0` returns as the "plane count" — proved to be the `vector<MeshNDInfo>` size (compared against `2`) and `+1`'d, but the per-field `MeshNDInfo` encoding (`MeshDim` stride/size/origin) and the dimension-collapse arithmetic were not field-by-field decoded. **HIGH** on the count semantics, the field layout is open.
- `EstimatePhysicalLinksUsed` link count: **resolved** — the function builds a *deduplicated, sorted set* of directional `IciResource`s (`1..6` = 3 torus dims × 2 directions; `absl` hash-set insert + `std::__introsort<IciResource*>` in the decompile) and the divisor is the set cardinality `|EstimatePhysicalLinksUsed(...)|`, **not** a per-dimension extent product. The multi-slice cross-slice branch (`EstimatePhysicalLinksUsed::$_0` @ `0x1c894ac0`, global→local logical-id resolver) was seen but not expanded. **HIGH** on the set-cardinality semantics; see [SC-side Twist §3](../twist/sc-side-twist.md#3-estimatephysicallinksused--the-physical-ici-link-estimator).
- The per-link table beyond `{0,1} = {2.0, 4.0}`: index ≥2 decodes as garbage (only 1D/2D exercised); whether a 3D all-to-all uses a real index-2 entry is untested. **LOW** on a hypothetical 3D entry.
- The `AllPairsUseSameIciLink` resource-id derivation from `source_target_pairs` geometry (the 1-slot-vs-3-slot predicate). **HIGH** on the branch existing, the geometry mapping is open.
- The SparseCore `GetCollectiveOffloadConfig` (`@0x133e1740`) alternate cost reroute for offloaded reduce-scatter/all-reduce: the probe is read but its alternate path was not separately decoded. **HIGH** on the probe, the offload cost path is open. See [SC offload config](sc-offload-config-builder.md) / [SC core selection](sc-core-selection-offload.md).

---

## Cross-References

- [Collectives Overview](overview.md) — the strategy picker and the collective-algorithm family this cost model serves
- [SelectNDStrategy (picker)](strategy-nd-picker.md) — the sharding / algorithm decision that *consumes* these costs; the `GetCommunicationTimeInMilliSec` ms figure is the cost-vs-cost signal it compares
- [Degraded-Axis Ingest](degraded-axis.md) — `GetDegradedAxis` / `Is{X,Y,Z}Degraded` drop a torus axis's two ICI slots from the deposit
- [GetHloResources Routing](../cost/gethloresources-routing.md) — the `GetHloResourcesImpl` collective arm that dispatches into `GetCollectiveCycles`
- [Resource Enum (23-slot)](../cost/resource-enum.md) — the `ResourceVector` and the `R[13..18]` `Ici{Y,X,Z}{Plus,Minus}` slots this cost model deposits into
- [Cost Model Overview](../cost/overview.md) — the per-gen `Performance` / `CycleTable` families and the `TensorCoreFrequencyInMegaHertz` cycle conversion these formulas use
- [ICI Overview](../ici/overview.md) — the physical ICI ring links whose per-direction bandwidth the `×0.5` factor models
- [Twist Overview](../twist/overview.md) — the twisted-torus replica-pair geometry that feeds `ReplicaGroupsOnNDPlane` / `NDTopologyInfo`
