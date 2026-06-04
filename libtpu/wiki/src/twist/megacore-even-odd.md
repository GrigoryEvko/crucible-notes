# Megacore Even/Odd Split

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — full C++ symbols, build `libtpu_lts_20260413_b_RC00`). `.text` VMA equals file offset (base `0xe63c000`); every address is a VMA. Each symbol cited is present in the full-symbol binary and cross-checked against the IDA decompile.*

## Abstract

A *megacore* chip carries two physical TensorCores that the runtime can fold into **one** XLA logical device. The twisted-torus all-gather (Phase1 of the two-phase replica-group build) has to decide, for every chip, whether that chip contributes **one** group member or **two** — and if two, which group each of the chip's cores joins. This page owns the byte-exact answer: the `Megacore() / CoresPerChip(0)` append gate inside `TwistedTorusND::GetPhase1ReplicaGroups` (`0x137d3de0`), the `LogicalDevicesPerChip(0)` getter that turns megacore mode into a group-count multiplier, and the per-core group-index assignment `core0 → 2m` (even) / `core1 → 2m+1` (odd).

The decisive structural result, decompile-confirmed and contrary to a loop-level first reading: the even/odd split is the **non-megacore 2-core** case, **not** the megacore case. A megacore chip presents `LogicalDevicesPerChip = 1`, so it collapses both physical cores into a single logical participant and joins a single group `m` (`2K` groups total). A non-megacore 2-core chip presents `LogicalDevicesPerChip = CoreCount = 2`, so its two cores fan out into the even/odd group pair `{2m, 2m+1}` (`4K` groups total). Phase0 (reduce-scatter) never splits — it always co-groups both cores of a chip into the *same* group.

This page is the byte-exact authority on three things a reimplementer cannot recover from the op surface or the loop structure alone:

- **The `LogicalDevicesPerChip(0)` gate.** `Target::LogicalDevicesPerChip(0)` (`0x1d615b00` → `TpuTopology::LogicalDevicesPerChip` `0x20ad3020`) returns `Megacore() ? 1 : CoreCount(0)` for `TpuCoreType=0` (TensorCore). It is the **single source** of the Phase1 group-count multiplier `LogicalDevicesPerChip(0) · 2K`, and it is the reason megacore yields `2K` groups while non-megacore 2-core yields `4K`.
- **The Phase1 append gate.** The two-branch `Megacore() / CoresPerChip(0) != 1` test that routes each `(m, i, k)` triple to either group `m` (single, both cores fold to one logical device) or the split `{2m, 2m+1}` (core0 even, core1 odd). The megacore arm's split condition is a logical contradiction (`CoresPerChip != 1 && CoresPerChip <= 1`) — a megacore chip therefore **always** takes the single-group path.
- **The per-core index.** `pair.first` (core0) → group `2m`; `pair.second` (core1) → group `2m+1`. The factor-2 LEAs (`v90 = 2*m`, `v91 = 2*m+1`) are precomputed once per outer-`m` iteration; the two cores land in adjacent groups so each core's all-gather walks a disjoint half of the plane's ICI links.

The Phase0/Phase1 replica-group **driver** (loop nests, group sizing, the `K`/`2K`/`R` scalars, the worked example) is on **[2-Phase Replica-Group Construction](replica-group-2phase.md)**; the coordinate fold that produces the `{core0, core1}` pair is on **[GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md)**; the shape classification that yields `K`/`2K` is on **[Shape Folds](shape-folds.md)**; the *workload* megacore fold (the runtime two-core barrier, the `MEGACORE(4)` marker, the SFLAG slot) is on **[Megacore Collective Fusion](../collectives/megacore-fusion.md)**. This page concentrates on the *group-assignment* even/odd split and the gate behind it.

| | |
|---|---|
| **Phase1 builder (owns the gate)** | `TwistedTorusND::GetPhase1ReplicaGroups` `0x137d3de0` |
| **Phase0 builder (always co-groups)** | `TwistedTorusND::GetPhase0ReplicaGroups` `0x137d3560` |
| **Group-count multiplier** | `Target::LogicalDevicesPerChip(0)` `0x1d615b00` → `TpuTopology::LogicalDevicesPerChip` `0x20ad3020` |
| **Megacore mode bit** | `TpuChipConfig::Megacore()` `0x20afca00` = `byte[TpuChipConfig+0x8]` |
| **Cores-per-chip** | `Target::CoresPerChip(t)` `0x1d615b40` = `int32[TpuTopology+0x7c + t·12]` |
| **Core count source** | `TpuChipParts::CoreCount(t)` `0x20b198e0` (`Megacore ? 1 : this`) |
| **Phase1 group count** | `LogicalDevicesPerChip(0) · 2K` — `2K` (megacore) or `4K` (non-megacore 2-core) |
| **Per-core index (split)** | `core0 (pair.first) → 2m`, `core1 (pair.second) → 2m+1` |
| **Confidence** | HIGH — gate jump senses, getter bodies, and `2m`/`2m+1` LEAs all decompile-verified |

---

## 1. The `LogicalDevicesPerChip` Gate

The Phase1 group count is `LogicalDevicesPerChip(0) · 2K`, computed once near the top of `GetPhase1ReplicaGroups`. `LogicalDevicesPerChip(0)` is the entire distinction between megacore (`2K` groups, no split) and non-megacore 2-core (`4K` groups, split). It is **not** read inside the per-`(m,i,k)` append loop — it only sizes the `vector<ReplicaGroup>`. The per-element split decision is made separately by the append gate (§2), but the two are consistent: the gate splits exactly when `LogicalDevicesPerChip(0) = 2`.

### `Target::LogicalDevicesPerChip` — the delegating wrapper

```c
// Target::LogicalDevicesPerChip(TpuCoreType) — 0x1d615b00
__int64 Target::LogicalDevicesPerChip(Target *this, __int64 core_type) {
    return (int)tpu::TpuTopology::LogicalDevicesPerChip(
        *(_QWORD *)(this + 952),   // Target+0x3b8 == the TpuTopology*
        core_type);
}
```

`Target+0x3b8` (decimal `952`) is the `TpuTopology*` the whole twist subsystem reads through; the same field feeds `CoresPerChip` and the `Megacore()` gate (§2). Phase1 calls this with `core_type = 0` (TensorCore).

### `TpuTopology::LogicalDevicesPerChip` — where megacore becomes 1

```c
// tpu::TpuTopology::LogicalDevicesPerChip(TpuCoreType) — 0x20ad3020 (condensed)
__int64 TpuTopology::LogicalDevicesPerChip(__int64 self, __int64 core) {
    cc = TpuChipParts::CoreCount(*(_QWORD *)(self + 8), core);
    if (!cc) return 0;                                  // core type absent on this chip
    if (core != 2) {                                    // TensorCore (0) path
        if (core != 0) FATAL("Unsupported core type"); //   tpu_topology.cc:538
        if (TpuChipConfig::Megacore(*(_QWORD *)(self + 24)))
            return 1;                                   // MEGACORE: 1 logical device
        return TpuChipParts::CoreCount(*(_QWORD *)(self + 8), 0);  // else: CoreCount
    }
    /* core == 2 (SparseCore) path: Megachip / SharedMemoryCount-based — out of scope */
}
```

The TensorCore arm (`core == 0`) reduces to exactly:

```c
LogicalDevicesPerChip(0) = Megacore() ? 1 : CoreCount(0)
```

`TpuChipConfig::Megacore()` (`0x20afca00`) is a one-byte read:

```c
// TpuChipConfig::Megacore() — 0x20afca00
__int64 TpuChipConfig::Megacore(TpuChipConfig *this) {
    return *((unsigned __int8 *)this + 8);   // byte[TpuChipConfig+0x8]
}
```

So a megacore chip with two physical TensorCores returns `1` (the two cores are one logical device); a non-megacore chip returns `CoreCount(0)` (2 for a 2-core chip, 1 for a single-core chip).

> **GOTCHA —** the SparseCore arm (`core == 2`) of `TpuTopology::LogicalDevicesPerChip` is a *different* formula (gated on `Megachip()` and `SharedMemoryCount`), not `Megacore ? 1 : CoreCount`. Phase1 only ever calls `LogicalDevicesPerChip(0)` (TensorCore), so the TensorCore split is what this page documents. Whether the SparseCore collective splits its Phase1 groups by the same rule is not exercised by `TwistedTorusND` — it lives in the `sparse_core::collective` topology builder. See [SC-Side Twist](sc-side-twist.md).

### The group-count multiply

```c
// GetPhase1ReplicaGroups — 0x137d3de0, prologue (decompiled, condensed)
v18 = *((_QWORD *)a2 + 190);                      // 2K  (max_dim, [obj+0x5f0])
v93 = Target::CoresPerChip(a3, 0);                // cores_per_chip (held for the gate)
v20 = v18 * Target::LogicalDevicesPerChip(a3, 0); // group count = 2K · LDPC(0)
v23 = operator new(48 * v20);                     // vector<ReplicaGroup>(group_count)
```

`v18` is `2K`; `Target::LogicalDevicesPerChip(a3, 0)` is `1` (megacore) or `2` (non-megacore 2-core); the product `v20` is the group count `2K` or `4K`. The `48 * v20` allocation (`ReplicaGroup` is 48 bytes) is the only place `LogicalDevicesPerChip` is read — confirming that the *count* is set by the multiplier while the *routing* is set by the per-element gate (§2). A reimplementer who allocates from the plane extent alone (without the `LDPC` factor) under-allocates by `2×` in the non-megacore 2-core case and indexes group `2m+1` out of bounds.

| Mode | `Megacore()` | `CoreCount(0)` | `LogicalDevicesPerChip(0)` | Phase1 group count |
|---|---|---|---|---|
| megacore | `1` | `2` | `1` | `2K` |
| non-megacore, 1 core/chip | `0` | `1` | `1` | `2K` |
| non-megacore, 2 cores/chip | `0` | `2` | `2` | `4K` |

---

## 2. The Phase1 Append Gate — Byte-Exact

The per-element routing is a two-branch test on `Megacore()` and `CoresPerChip(0)`, evaluated once per `(m, i, k)` triple after the coordinate fold returns `{pair.first = core0, pair.second = core1}`. The gate is the live byte sequence at `0x137d4348..0x137d4462`; the decompiled form is the clearest statement of it.

### The decompile

```c
// GetPhase1ReplicaGroups — 0x137d3de0, the append gate (decompiled, condensed)
// per-m precompute: v90 = 2*m (even), v91 = 2*m+1 (odd), v94 = 48*m (single group m)
// per-(m,i,k): ReplicaPair3DOnTwistedTorus = pair.first (core0);  v43 = pair.second (core1)

if ( TpuChipConfig::Megacore(*(_QWORD *)(*((_QWORD *)v88 + 119) + 24)) ) {   // Target+0x3b8 -> TpuChipConfig
    if ( v93 != 1 && *(int *)(*((_QWORD *)v88 + 119) + 124) <= 1 )           // CoresPerChip(0)!=1 && [+0x7c]<=1
        goto SPLIT;                                                          //   (DEAD: contradiction, see below)
    // megacore, no split -> fall through to SINGLE group m
} else {                                                                     // non-megacore
    if ( v93 != 1 )                                                          // CoresPerChip(0) != 1
        goto SPLIT;                                                          //   2-core chip -> split
    // non-megacore single-core -> fall through to SINGLE group m
}

// ---- SINGLE: append core0 (pair.first) into group m (offset 48*m) ----
groups[m].add(pair.first);                                                   // -> group m
goto DONE;

SPLIT:
groups[2*m  ].add(pair.first );   // core0 -> group 2m   (even)
groups[2*m+1].add(pair.second);   // core1 -> group 2m+1 (odd)
DONE:
```

`v88` is the `Target*`; `*((_QWORD *)v88 + 119)` is `Target+0x3b8` (`119·8 = 952 = 0x3b8`), the `TpuTopology*`; `+24` reaches the `TpuChipConfig` for the `Megacore()` read; `+124` is `TpuTopology+0x7c`, which is `CoresPerChip(0)` read inline. `v93` is the same `CoresPerChip(0)` value already loaded in the prologue.

### Truth table

| `Megacore()` | `CoresPerChip(0)` | branch taken | routing |
|---|---|---|---|
| `1` (megacore) | `2` | megacore split test `2 != 1 && 2 <= 1` = **false** → fall through | **single** group `m` |
| `1` (megacore) | `1` | megacore split test `1 != 1 && …` = **false** → fall through | **single** group `m` |
| `0` (non-megacore) | `1` | `1 != 1` = false → fall through | **single** group `m` |
| `0` (non-megacore) | `2` | `2 != 1` = **true** → SPLIT | **split** core0→`2m`, core1→`2m+1` |

> **QUIRK — the megacore split arm is dead.** The megacore branch's split condition is `CoresPerChip(0) != 1 && CoresPerChip(0) <= 1`, which can never be true for any integer (`v93` is the very same `CoresPerChip(0)`). A megacore chip therefore *always* reaches the single-group-`m` path, regardless of how many physical cores it has. The condition is structurally present (the compiler emitted both `cmp [rbp-0xb0],1; je 0x4462` and `cmp [TpuTopology+0x7c],1; jg 0x4462` jumps), but only the non-megacore arm's `CoresPerChip(0) != 1` test can ever route to the split. This is the byte-level confirmation that **megacore never splits**: it is not that megacore *chooses* single-group, it is that the only reachable split path is the non-megacore one.

### Why the split routes core0 even / core1 odd

The two factor-2 group indices are precomputed once per outer `m` (`v90 = 2*m`, `v91 = 2*m+1`), so the split appends are a pair of fixed offsets `48·2m` and `48·(2m+1)`. The coordinate fold returns the chip's `{core0, core1}` device-id pair; the split sends `core0` to the even group and `core1` to the odd group. The two cores of a non-megacore 2-core chip thus all-gather over **disjoint** group halves: every even group `2m` carries only the core0 of each chip in slice `m`, every odd group `2m+1` only the core1. Each core's all-gather traffic uses a distinct slice of the plane's ICI links instead of both cores contending on one group, which balances bandwidth across the two logical devices. In megacore mode the chip is *one* logical device, so there is one all-gather participant per chip and one group `m` — the workload split across the two physical cores happens inside that single logical device, by the runtime fold ([Megacore Collective Fusion](../collectives/megacore-fusion.md)), not by the replica-group construction.

---

## 3. Phase0 Always Co-Groups — the Contrast

Phase0 (reduce-scatter along the `2K` ring) runs the *same* `Megacore() / CoresPerChip(0)` test, but its two appends — when both fire — write the **same** group, never an even/odd pair. The split that Phase1 performs has no Phase0 analogue.

```c
// GetPhase0ReplicaGroups — 0x137d3560, the second-core gate (decompiled, condensed)
// first append already done: groups[g].add(pair.first)  // core0, group offset 48*g

if ( TpuChipConfig::Megacore(*(_QWORD *)(*((_QWORD *)v86 + 119) + 24)) ) {
    if ( v88 == 1 || *(int *)(*((_QWORD *)v86 + 119) + 124) > 1 )   // CoresPerChip(0)==1 || [+0x7c]>1
        goto SKIP_SECOND;                                          //   megacore 2-core: skip core1
} else {
    if ( v88 == 1 )                                                // single-core: skip core1
        goto SKIP_SECOND;
}
groups[g].add(pair.second);   // core1 -> SAME group g  (offset 48*g, identical to core0's)
SKIP_SECOND:
```

`v88` is `CoresPerChip(0)`. The second append (`pair.second`, core1) targets the identical group offset `48·g` as the first (`pair.first`, core0) — both write into the group computed from the single index `g = k·R + i`, which does not depend on the core. So the reduce-scatter ring keeps a chip's two cores **co-resident** on the same ring; they do not fan out until the all-gather.

The Phase0 gate skips the second-core append in two cases:

| `Megacore()` | `CoresPerChip(0)` | second append? | reason |
|---|---|---|---|
| `1` (megacore) | `2` | **skipped** (`CoresPerChip(0) > 1`) | one logical device — `pair.first` already covers it |
| `1` (megacore) | `1` | skipped (`== 1`) | single core |
| `0` (non-megacore) | `2` | **appended** | both cores join the ring group |
| `0` (non-megacore) | `1` | skipped (`== 1`) | single core |

> **NOTE —** the asymmetry is the whole point. In megacore mode the reduce-scatter group appends only `pair.first` (the single logical device's core0 id) because both physical cores are *one* device on the ring; the all-gather likewise uses a single group `m`. In non-megacore 2-core mode the reduce-scatter co-groups *both* core ids into one ring (they share the chip's ICI ring position, so there is no bandwidth gain from separating them), but the all-gather splits them into even/odd halves (where separating them *does* balance the orthogonal plane's links). So Phase0 and Phase1 read identical inputs and reach opposite group shapes: RS co-groups, AG splits.

---

## 4. The Three Topology Getters

The gate reads three runtime topology values through the `Target` object's `TpuTopology*` at `Target+0x3b8`. All three are small leaf functions; a reimplementer must reproduce them exactly because the gate's correctness depends on the contradiction-and-fallthrough structure of §2, which in turn depends on these returning the right field.

### `Target::CoresPerChip(t)` — `0x1d615b40`

```c
// Target::CoresPerChip(TpuCoreType) — 0x1d615b40
__int64 Target::CoresPerChip(Target *this, unsigned int core_type) {
    if (core_type >= 3) BUG();
    return *(int *)(*(_QWORD *)(this + 952) + 12LL * core_type + 124);
    //              Target+0x3b8 (TpuTopology*)  + 0x7c + core_type·12
}
```

`CoresPerChip(0)` is `int32[TpuTopology + 0x7c]` — a per-`TpuCoreType` array with a 12-byte stride, the *physical* core count of the chip (not the logical count). For a megacore 2-core chip this is `2` even though `LogicalDevicesPerChip(0) = 1`; that mismatch (`CoresPerChip = 2`, `LDPC = 1`) is precisely what the gate distinguishes.

### `TpuChipConfig::Megacore()` — `0x20afca00`

`byte[TpuChipConfig + 0x8]` (§1). The `TpuChipConfig` is reached via `Target+0x3b8` (`TpuTopology*`) `+ 0x18` (`TpuChipConfig*`).

### `TpuTopology::LogicalDevicesPerChip(0)` — `0x20ad3020`

`Megacore() ? 1 : CoreCount(0)` (§1), wrapped by `Target::LogicalDevicesPerChip` (`0x1d615b00`). `CoreCount` resolves through `TpuChipParts::CoreCount(t)` (`0x20b198e0`).

| Getter | Address | Returns | Field |
|---|---|---|---|
| `Target::CoresPerChip(t)` | `0x1d615b40` | physical cores of type `t` | `int32[TpuTopology+0x7c + t·12]` |
| `TpuChipConfig::Megacore()` | `0x20afca00` | megacore mode bit | `byte[TpuChipConfig+0x8]` |
| `Target::LogicalDevicesPerChip(t)` | `0x1d615b00` | (wrapper) | delegates to `0x20ad3020` |
| `TpuTopology::LogicalDevicesPerChip(t)` | `0x20ad3020` | logical devices/chip | `Megacore ? 1 : CoreCount(t)` (TC arm) |
| `TpuChipParts::CoreCount(t)` | `0x20b198e0` | core count of type `t` | per-`TpuChipParts` field |

---

## 5. The Megacore Split Table — Group Count and Routing

The complete decision, combining the §1 group-count multiplier with the §2 append gate, for the TensorCore Phase1 build:

| Mode | `Megacore()` | `CoresPerChip(0)` | `LogicalDevicesPerChip(0)` | Phase1 groups | Per-`(m,i,k)` routing |
|---|---|---|---|---|---|
| megacore | `1` | `2` | `1` | `2K` | group `m` (both cores → one logical device) |
| non-megacore, 1 core | `0` | `1` | `1` | `2K` | group `m` (single core) |
| **non-megacore, 2 cores** | `0` | `2` | `2` | **`4K`** | **core0 → `2m` (even), core1 → `2m+1` (odd)** |

Read alongside the Phase0 contrast: Phase0 group count is always `K·R` (it uses `CoresPerChip`, not `LogicalDevicesPerChip`, and never doubles); both cores of a chip land in the *same* Phase0 group when both are appended. The `2K`-vs-`4K` doubling is a Phase1-only consequence of `LogicalDevicesPerChip(0)`, and the even/odd routing is a Phase1-only consequence of the non-megacore `CoresPerChip(0) != 1` arm. See [2-Phase Replica-Group Construction](replica-group-2phase.md) §4–§5 for the matched group products and a worked `K, K, 2K` sizing.

> **CORRECTION (TWIST-1) —** a loop-level first reading (where `Megacore()` appears in the split guard's first branch) labelled the Phase1 even/odd split as "megacore: core0 → even / core1 → odd". The byte-exact gate (§2) shows the opposite: the megacore arm's split condition is a contradiction and never fires, so megacore always uses the single group `m` (`2K` groups). The even/odd split is reached only through the **non-megacore** `CoresPerChip(0) != 1` arm (`LogicalDevicesPerChip(0) = CoreCount = 2 → 4K` groups). The group count `LogicalDevicesPerChip(0) · 2K` (the `imul`), the `Megacore ? 1 : CoreCount` getter, the gate jump senses, and the `2m`/`2m+1` LEAs are all decompile-confirmed. Marked **HIGH**.

---

## 6. Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TwistedTorusND::GetPhase1ReplicaGroups` | `0x137d3de0` | owns the even/odd append gate; group count `LDPC(0)·2K` | HIGH (gate + multiply decompile-verified) |
| `TwistedTorusND::GetPhase0ReplicaGroups` | `0x137d3560` | always co-groups both cores into the same group | HIGH (both appends share group offset) |
| `Target::LogicalDevicesPerChip` | `0x1d615b00` | wrapper → `TpuTopology::LogicalDevicesPerChip` | HIGH (delegating body verified) |
| `TpuTopology::LogicalDevicesPerChip` | `0x20ad3020` | `Megacore ? 1 : CoreCount` (TC arm) — the gate's source | HIGH (TC arm verified) |
| `TpuChipConfig::Megacore` | `0x20afca00` | `byte[TpuChipConfig+0x8]` megacore mode bit | HIGH (one-byte read verified) |
| `Target::CoresPerChip` | `0x1d615b40` | `int32[TpuTopology+0x7c + t·12]` physical core count | HIGH (offset + stride verified) |
| `TpuChipParts::CoreCount` | `0x20b198e0` | per-core-type physical core count | MEDIUM (located; body not transcribed) |
| `GetReplicaPair3DOnTwistedTorus` | `0x1c893400` | produces the `{core0, core1}` pair the gate routes | HIGH (own page) |

---

## 7. What Was Not Resolved

- **The `CoreCount(0)` literal per chip generation.** `LogicalDevicesPerChip(0) = Megacore ? 1 : CoreCount(0)` is byte-exact; the literal `CoreCount(0)` value (2 for a megacore/2-core TPU, 1 for single-core) comes from `TpuChipParts` populated from the chip-config proto. The getter formula is confirmed; the per-codename constant is a proto dependency, not extracted here. MEDIUM.
- **The SparseCore Phase1 split.** Phase1 reads only `LogicalDevicesPerChip(0)` / `CoresPerChip(0)` (TensorCore, `t=0`). Whether the SparseCore collective splits its Phase1 groups by the same `Megacore ? 1 : CoreCount` rule (the `core == 2` arm of `0x20ad3020` is a *different*, `Megachip`/`SharedMemoryCount`-based formula) is not exercised by `TwistedTorusND`. See [SC-Side Twist](sc-side-twist.md). LOW.
- **The `arg ≥ 1` multi-shard path.** The gate is decoded for the live `arg == 0` single-phase collective; the coordinate fold's `arg ≥ 1` entry is CHECK-unreachable behind the shard gate (`GetPerColorShardIdTable`, [2-Phase Replica-Group Construction](replica-group-2phase.md) §6). Whether a future multi-shard build changes the per-core routing is unexercised. LOW.

---

## Cross-References

**Twist algorithms (this section)**
- [Twisted Torus — Section Map](overview.md) — the subsystem map; cites this page for the `LogicalDevicesPerChip`-keyed split
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — the Phase0/Phase1 driver, group sizing, and the `K`/`2K`/`R` scalars (links here for the byte-exact gate)
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the coordinate fold that produces the `{core0, core1}` pair this gate routes
- [Shape Folds](shape-folds.md) — where `K`, `2K`, and the `R = num-2K-axes ≥ 2 ? 2K : K` plane dimension come from
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the per-color ring-neighbour emission side these device-id lists complement
- [SC-Side Twist](sc-side-twist.md) — the SparseCore twisted-torus topology builder (the `core == 2` `LogicalDevicesPerChip` arm)

**Sibling sections**
- [Megacore Collective Fusion](../collectives/megacore-fusion.md) — the *workload* megacore fold (runtime two-core barrier, `MEGACORE(4)` marker, SFLAG slot) — the logical-device-internal split a megacore chip uses *instead* of the even/odd group split
- [Physical-Core Placement](../collectives/physical-core-placement.md) — how logical devices map onto physical cores
- [back to index](../index.md)
