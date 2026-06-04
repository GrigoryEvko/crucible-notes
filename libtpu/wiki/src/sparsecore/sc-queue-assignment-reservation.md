# SC Queue Assignment & Reservation

> *Every address, offset, field number, and constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). `.text` VMA == file offset (`0xE63C000`); `.rodata` VMA == file offset (`0x84A0000`); `.data.rel.ro` VMA − `0x200000` == file offset. Other versions differ.*

## Abstract

This page is the **per-resource reservation map** that `xla::jellyfish::SparseCoreQueueAssignment` stages at member offset `[this+0xC0]`: an `absl::btree_map<long resource_id, long limit>` keyed by the nine SparseCore / async-collective scheduling **resource-type IDs** `{2, 3, 6, 23, 24, 25, 26, 27, 28}`, each carrying a concurrency / overlap **limit** sourced from a named `TpuCompilationEnvironment` (TCE) compile-knob. The map is the SparseCore back-end's *intended* statement of "how many of each resource may be in flight." This page documents three things, all byte-anchored:

1. **The reservation `btree_map<long,long>`** — its nine keys, the per-key value source (the pass object's `+0x18..+0x58` window), and where it is built (`RunImpl`) and freed (the destructor).
2. **The TCE `AutoOr<long>` field labels** that seed each reservation value — the field numbers `{288, 924, 925, 1088, 1089, 1090, 1091, 1092}` (key 28 is a hardcoded `INT64_MAX`), their proto field names, and the `AUTO → 1` fallback polarity at the build site.
3. **The real consumer that reads `[this+0xC0]`** — a *definitive negative result*. No member of `SparseCoreQueueAssignment` reads the map. It is built once and destroyed; the assignment path (`GetAllowedCores` / `SelectCores` / `AssignQueueIDsToAsyncStart`) never queries it. The map is a **staged / unwired** member in v0.0.40: it holds the configured per-resource caps but does not gate core selection. The *enforcement* of those same caps lives elsewhere — in the `LatencyHidingScheduler`'s `AsyncTracker` resource-limit table, built from the **same** TCE fields under the **same** resource IDs (see [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md)).

The mental model a reimplementer needs is that this `[this+0xC0]` map is a **vestigial duplicate** of the live scheduler concurrency table, not a missing feature. The reservation *values* are resolved fully and correctly from the named knobs; only the *reader* is absent in the `SparseCoreQueueAssignment` translation unit.

For reimplementation, the contract is:

- **The reservation map is a `btree_map<long,long>` at `[this+0xC0]`** with `map_params_impl<long,long>` node parameters and the shared empty-node sentinel `0x2177A4B0`. Keys are resource-type IDs; values are concurrency / overlap limits.
- **Nine fixed keys, in build order: `{3, 6, 23, 24, 25, 26, 27, 28}` plus key `2`.** Each value is read from the pass object's `+0x18..+0x58` qword window, which the `AddPass<…>` constructor `vmovups`-copied from the caller's `SparseCoreQueueAssignmentConfig` stack struct.
- **Seven of the nine values come from `AutoOr<long>` TCE knobs (AUTO → 1 here).** Key 2 is a raw proto3 `int32` (default 0); key 28 is a hardcoded `INT64_MAX` literal (no TCE field). The seven `AutoOr<long>` reads use the AUTO-unset → `1` polarity *at this build site* — distinct from the live scheduler's AUTO → `INT64_MAX`.
- **The map has no consumer in v0.0.40.** It is built once in `RunImpl` (`0x10FE4000`) and freed in `~SparseCoreQueueAssignment` (`0x10FE4BA0`). No `internal_find` / `lower_bound` / `operator[]` on `[this+0xC0]` exists anywhere in the pass band. The reservation is **staged**, not enforced, by this pass.
- **The enforcing path is the `AsyncTracker` concurrency table, not this map.** The same keys `{2,3,6,23..28}` and the same fields `{288,924,925,1088..1092}` are read again by `GetTpuAsyncTracker` / `GetSchedulerConfig` and turned into co-issue caps by `TpuAsyncTracker::GetNumAvailableResources` (`0x10FFF600`).

| | |
|---|---|
| **Reservation map** | `btree_map<long,long>` at `[SparseCoreQueueAssignment + 0xC0]` |
| **Node params** | `map_params_impl<long,long>`; empty-node sentinel `0x2177A4B0` |
| **Keys (resource IDs)** | `{2, 3, 6, 23, 24, 25, 26, 27, 28}` |
| **Value window** | pass object `+0x18..+0x58` (9 qwords; copied from `Config[+0x00..+0x40]`) |
| **Built in** | `SparseCoreQueueAssignment::RunImpl` (`0x10FE4000`) — nine `insert_hint_unique` |
| **Inserter** | `btree<map_params_impl<long,long>>::insert_hint_unique` (`0x10FE6E40`) |
| **Root store** | `*(this+0xC0) = built-tree-root` (`@0x10FE42B9`) |
| **Freed in** | `~SparseCoreQueueAssignment::D2` (`0x10FE4BA0`) → `btree_node::clear_and_delete` (`0xF7D0400`) |
| **Config builder** | `(anon)::RunHloScheduler` (`0x109718C0` region) — reads TCE, fills `Config[-0x138]` |
| **Config → pass copy** | `HloPassPipeline::AddPass<SparseCoreQueueAssignment,…>` (`0x10975FC0`) — three `vmovups` |
| **Map consumer** | **NONE** in v0.0.40 (definitive negative; see units below) |
| **Live enforcer (elsewhere)** | `TpuAsyncTracker::GetNumAvailableResources` (`0x10FFF600`) — see [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) |
| **Confidence** | CONFIRMED (decompile + binary-byte anchored) unless a row or callout says otherwise |

---

## The Reservation `btree_map<long,long>` at `[this+0xC0]`

### Purpose

`SparseCoreQueueAssignment` reserves a slot per resource type: a `btree_map<long resource_id, long limit>` stored at the pass object's `+0xC0`. The keys are the AsyncTracker / SparseCore scheduling resource-type IDs a collective can occupy (the same `{0,23..28}` space `GetSparseCoreResources` produces, plus the base async-collective IDs `{2,3,6}`); the values are the per-resource concurrency or overlap limits the compile environment configured. The intent is a per-resource cap on how many of that resource may be queued; the realization in v0.0.40 is a built-then-freed member with no reader (documented under *The Map Consumer* below).

### The map structure

The map is an `absl::btree_map` with `map_params_impl<long,long>` — byte-confirmed in the decompile by the `btree<…map_params_impl<long,long>…>` template instantiations the build and free sites reference. A fresh/empty map's root points at the shared empty-node sentinel `EmptyNode()::empty_node` (relocated address `0x2177A4B0`); the constructor seeds `[this+0xC0]` with that sentinel and `[this+0xC8]` (the size/rightmost fields) with zero.

```text
SparseCoreQueueAssignment object (sizeof 0xF8, _Znwm 0xF8 @0x10975FD5):
  [this+0x00]  vptr (= 0x2181D8C8 + 0x10, @0x10976020)
  [this+0x08]  Target*            (= r12 @0x10976023)
  [this+0x10]  unique_ptr<LatencyEstimator>
  [this+0x18 .. +0x58]  the 9-qword reservation VALUE window  (Config copy; see below)
  [this+0x60]  bool   (the 10th Config field; NOT a map value — see callout)
  ...
  [this+0xC0]  btree_map<long,long> root pointer  ← the RESERVATION MAP
  [this+0xC8 .. +0xD7]  btree size / rightmost-leaf fields
```

### Algorithm — the build (`RunImpl`)

`RunImpl` (`0x10FE4000`) assembles nine `{key, value}` stack pairs and inserts each into a fresh btree via `insert_hint_unique` (`0x10FE6E40`), then moves the built tree's root into `[this+0xC0]`. The decompile shows the pair setup verbatim (`a2` = the pass `this`):

```c
// SparseCoreQueueAssignment::RunImpl  @0x10FE4000  (a2 = this)
v80    = *(_QWORD *)(a2 + 24);   //  key 2  value  ← obj[+0x18]   (Config[+0x00])
v81[0] = 3;  v81[1] = *(_QWORD *)(a2 + 32);   //  key 3  ← obj[+0x20]  (Config[+0x08])
v82[0] = 6;  v82[1] = *(_QWORD *)(a2 + 40);   //  key 6  ← obj[+0x28]  (Config[+0x10])
v83[0] = 23; v83[1] = *(_QWORD *)(a2 + 48);   //  key 23 ← obj[+0x30]  (Config[+0x18])
v84[0] = 24; v84[1] = *(_QWORD *)(a2 + 56);   //  key 24 ← obj[+0x38]  (Config[+0x20])
v85[0] = 25; v85[1] = *(_QWORD *)(a2 + 64);   //  key 25 ← obj[+0x40]  (Config[+0x28])
v86[0] = 26; v86[1] = *(_QWORD *)(a2 + 72);   //  key 26 ← obj[+0x48]  (Config[+0x30])
v87[0] = 27; v87[1] = *(_QWORD *)(a2 + 80);   //  key 27 ← obj[+0x50]  (Config[+0x38])
v88[0] = 28; v88[1] = *(_QWORD *)(a2 + 88);   //  key 28 ← obj[+0x58]  (Config[+0x40])

v100 = &btree<map_params_impl<long,long>>::EmptyNode()::empty_node;   // fresh root
// nine insert_hint_unique<long, pair<long,long> const&>(&tree, &root, ...):
btree<…>::insert_hint_unique(&v96, &v100, v101, *(u8*)(v101+10), v80_pair, …);   // key 2
btree<…>::insert_hint_unique(&v96, &v100, v101, *(u8*)(v101+10), v81, v81);      // key 3
// … keys 6,23,24,25,26,27,28 …
*(_QWORD *)(a2 + 192) = v100;    // root → [this+0xC0]   (@0x10FE42B9)
```

The arithmetic ties out byte-exact: `a2 + 24 = obj[+0x18]`, `a2 + 88 = obj[+0x58]`, and `a2 + 192 = [this+0xC0]`. The nine values are the qword window the `AddPass` constructor copied from the caller's `SparseCoreQueueAssignmentConfig` (next unit).

> **NOTE —** key `2` is built as the bare pair `v80` (the first insert), while keys `{3,6,23..28}` are the `vNN[0]=key; vNN[1]=value` pairs. All nine flow through the same `insert_hint_unique` (`0x10FE6E40`) into the same root. The build is unconditional — there is no branch that skips a key — so the map always has exactly nine entries after `RunImpl`.

### Algorithm — the free (destructor)

`~SparseCoreQueueAssignment::D2` (`0x10FE4BA0`) frees the map by calling `btree_node::clear_and_delete` (`0xF7D0400`) on `[this+0xC0]`:

```c
// ~SparseCoreQueueAssignment::D2  @0x10FE4BA0
btree_node<map_params_impl<long,long>>::clear_and_delete(*((_QWORD **)this + 24));  // [this+0xC0]
// ( *((_QWORD*)this + 24) == this+0x180? no — index 24 * 8 = 0xC0 )
```

`*((_QWORD*)this + 24)` is `this + 24*8 = this + 0xC0`. The destructor also frees the two Swiss tables the pass holds at `[this+0xA0]` and `[this+0x80]` (the `flat_hash_set`/grouping state from `GetAllowedCores`), but only the `[this+0xC0]` `clear_and_delete` touches the reservation map.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `SparseCoreQueueAssignment::RunImpl` | `0x10FE4000` | builds the nine-entry reservation map; root → `[this+0xC0]` | CONFIRMED |
| `btree<map_params_impl<long,long>>::insert_hint_unique` | `0x10FE6E40` | the per-key inserter (×9) | CONFIRMED |
| `btree<map_params_impl<long,long>>::EmptyNode` | `0x2177A4B0` | empty-node sentinel for the fresh root | CONFIRMED |
| `~SparseCoreQueueAssignment::D2` | `0x10FE4BA0` | frees the map via `clear_and_delete` | CONFIRMED |
| `btree_node<map_params_impl<long,long>>::clear_and_delete` | `0xF7D0400` | recursive btree free | CONFIRMED |

---

## The TCE Field Labels That Seed the Values

### Purpose

Each reservation value originates in a named `TpuCompilationEnvironment` (TCE) proto field. The data path is three hops, all byte-confirmed: the caller reads TCE and builds a `SparseCoreQueueAssignmentConfig` on its stack; the `AddPass` constructor `vmovups`-copies that Config into the pass object's `+0x18..` window; `RunImpl` reads that window as the nine map values. This unit names each field — its proto **field number** (decoded from `TpuCompilationEnvironment::_InternalSerialize`, `0x1DB41DC0`), its **field name** (from the carved `FileDescriptorProto`), its **type**, and its **AUTO fallback** at this build site.

### Hop A — the Config build (caller)

The caller (the SparseCore scheduler setup inside `RunHloScheduler`, region `0x109718C0..0x10971A99`) obtains the TCE via `GetTpuCompEnv` (`0x1D73DD20`, the `GetMutableEnv<TpuCompilationEnvironment>` `_impl_`) and assembles `SparseCoreQueueAssignmentConfig` at `[rbp-0x138]`:

```text
key 2  (int32, raw):   movslq 0xF78(TCE),rax        → Config[+0x00]    field 288
keys 3,6,23..27 (AutoOr<long>):
   rdi = TCE[+OFF]; if null → AutoProto_globals_ (0x223C8968, cmove %r15);
   AutoOr<long>::FromProtoOrDie (0x1092F7E0) → {value rax, has-bit dl};
   test $1,%dl ; mov $1,%r13d ; cmove %r13,%rax    ⇒ AUTO (oneof unset) → 1
       TCE+0x460 → Config[+0x08]   field 924
       TCE+0x468 → Config[+0x10]   field 925
       TCE+0x940 → Config[+0x18]   field 1088
       TCE+0x948 → Config[+0x20]   field 1089
       TCE+0x950 → Config[+0x28]   field 1090
       TCE+0x958 → Config[+0x30]   field 1091
       TCE+0x960 → Config[+0x38]   field 1092
key 28 (constant):    movabs $0x7FFFFFFFFFFFFFFF   → Config[+0x40]    (no TCE field — INT64_MAX)
```

### Hop B — the `AddPass` `vmovups` copy

`HloPassPipeline::AddPass<SparseCoreQueueAssignment,…>` (`0x10975FC0`) allocates the pass (`_Znwm 0xF8`) and copies the Config arg (`rcx`/`r14`) into the object with three overlapping 32-byte moves:

```text
vmovups (%r14),%ymm0      → obj[+0x18..+0x37]   (= Config[+0x00..+0x1F])
vmovups 0x20(%r14),%ymm1  → obj[+0x38..+0x57]   (= Config[+0x20..+0x3F])
vmovups 0x30(%r14),%ymm2  → obj[+0x48..+0x67]   (= Config[+0x30..+0x4F])
```

So `obj[+0x18] = Config[+0x00]`, …, `obj[+0x58] = Config[+0x40]` (the overlap `obj[+0x48..+0x57]` is written identically by `ymm1` and `ymm2`). The constructor also stores the `Target` pointer (`obj[+0x08]`), the `LatencyEstimator` `unique_ptr` (`obj[+0x10]`), the vtable (`0x2181D8C8+0x10`), and seeds the empty map root (`obj[+0xC0] = empty_node 0x2177A4B0`).

### The full field table

The `TCE _impl_ off` column and the `TCE field name` column are CONFIRMED (the byte-offset from `RunHloScheduler`, the name from the matching `AbslFlagDefaultGenFor<name>` symbol). The `TCE field #` column is the proto-source label (MEDIUM — see the note after the table).

| Map key | obj off | Config off | TCE `_impl_` off | TCE field # | TCE field name | Proto type | AUTO fallback (here) |
|---|---|---|---|---|---|---|---|
| 2 | `+0x18` | `+0x00` | `0xF78` | 288 | `xla_max_concurrent_async_all_gathers` | int32 (raw) | proto3-zero `0` |
| 3 | `+0x20` | `+0x08` | `0x460` | 924 | `xla_max_concurrent_async_all_reduces` | `AutoOr<long>` | `1` (cmove) |
| 6 | `+0x28` | `+0x10` | `0x468` | 925 | `xla_max_concurrent_async_reduce_scatters` | `AutoOr<long>` | `1` (cmove) |
| 23 | `+0x30` | `+0x18` | `0x940` | 1088 | `xla_tpu_sparse_core_gather_overlap_limit` | `AutoOr<long>` | `1` (cmove) |
| 24 | `+0x38` | `+0x20` | `0x948` | 1089 | `xla_tpu_sparse_core_scatter_overlap_limit` | `AutoOr<long>` | `1` (cmove) |
| 25 | `+0x40` | `+0x28` | `0x950` | 1090 | `xla_tpu_sparse_core_data_formatting_overlap_limit` | `AutoOr<long>` | `1` (cmove) |
| 26 | `+0x48` | `+0x30` | `0x958` | 1091 | `xla_tpu_sparse_core_kernel_overlap_limit` | `AutoOr<long>` | `1` (cmove) |
| 27 | `+0x50` | `+0x38` | `0x960` | 1092 | `xla_tpu_sparse_core_sort_overlap_limit` | `AutoOr<long>` | `1` (cmove) |
| 28 | `+0x58` | `+0x40` | — | — | (hardcoded `INT64_MAX`; not a TCE field) | constant | `0x7FFFFFFFFFFFFFFF` |

Each field's `_impl_` byte-offset is byte-confirmed from the `RunHloScheduler` Config-build region (the `GetTpuCompEnv(…) + N` loads: `+3960`, `+1120`, `+1128`, `+2368`, `+2376`, `+2384`, `+2392`, `+2400` — see Hop A), and each offset binds to a distinct `AutoOr<long>` proto field whose flag-symbol name (`AbslFlagDefaultGenFor<name>`) matches the table below. The proto **field numbers** are recorded from `TpuCompilationEnvironment::_InternalSerialize` (`0x1DB41DC0`); that function does not produce Hex-Rays pseudocode and its serializer tags are mostly pre-shifted varints rather than bare `mov $imm` immediates, so treat the field-*number* column as MEDIUM confidence — the offset↔name binding (CONFIRMED) is what a reimplementer needs, the number is the proto-source label. The `925` tag (`0x39D`) is observable in the serializer body; the int32 key 2 carries wire tag `0x1280` (varint `0x1280` → 2304 → field 288, wire-type 0).

> **NOTE — AUTO polarity is call-site-local.** The seven `AutoOr<long>` reads here resolve an *unset* (AUTO) oneof to **`1`** (the `test $1,%dl ; cmove $1` shape). This is the polarity for the `AddPass` Config-build site only. The *live* scheduler reads the **same** five SC overlap knobs (`1088..1092`) in `GetTpuAsyncTracker` (`0x10975520`) with AUTO → **`INT64_MAX`** (no cap) instead — same fields, opposite default, different call site. A reimplementer must not assume one global default for these knobs. The semantic default in the enforcing path is "no cap"; the `1` here is moot because this map has no reader.

> **GOTCHA — the 10th Config field is not a map value.** The Config also carries a tenth field, a bool from `AutoOr<bool>(TCE[+0xC88])` (TCE field **1202** `xla_tpu_rerun_latency_hiding_scheduler_post_sc_assignment`, the `and $0x101; cmp $0x101; setne` AUTO-on idiom). It is copied into `obj[+0x60]`, *beyond* the `+0x18..+0x58` map-value window, and `RunImpl` does not read it for the map. It gates the post-SC-assignment `LatencyHidingScheduler` rerun, not any reservation value. See *The Live Enforcer* below.

### Resource-ID → collective correspondence

The keys are AsyncTracker scheduling resource-type IDs. The base IDs `{2,3,6}` and the SC IDs `{23..28}` index the same resource space `GetSparseCoreResources` (`0x10FDC0A0`) and `MayAddSparseCoreResource` (`0x11000480`) produce. The authoritative `kSparseCore*` names and the opcode→id switch (including the `0x56 → 12`, `0x5d → 6` mapping) live on [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md); the SC categories the knob names attribute are:

| key | resource category | TCE knob (field) | unit | default-at-build |
|---|---|---|---|---|
| 2 | base async-collective | `xla_max_concurrent_async_all_gathers` (288) | max-concurrent | 0 |
| 3 | base async-collective | `xla_max_concurrent_async_all_reduces` (924) | max-concurrent | 1 (AUTO) |
| 6 | base async-collective | `xla_max_concurrent_async_reduce_scatters` (925) | max-concurrent | 1 (AUTO) |
| 23 | `kSparseCoreGather` | `xla_tpu_sparse_core_gather_overlap_limit` (1088) | overlap-limit | 1 (AUTO) |
| 24 | `kSparseCoreScatter` | `xla_tpu_sparse_core_scatter_overlap_limit` (1089) | overlap-limit | 1 (AUTO) |
| 25 | `kSparseCoreDataFormatting` | `xla_tpu_sparse_core_data_formatting_overlap_limit` (1090) | overlap-limit | 1 (AUTO) |
| 26 | `kSparseCoreKernel` | `xla_tpu_sparse_core_kernel_overlap_limit` (1091) | overlap-limit | 1 (AUTO) |
| 27 | `kSparseCoreSort` | `xla_tpu_sparse_core_sort_overlap_limit` (1092) | overlap-limit | 1 (AUTO) |
| 28 | catch-all | (none — hardcoded `INT64_MAX`) | unlimited | `INT64_MAX` |

> **NOTE — name vs producing-opcode.** The value source *names* (`all_gathers` → key 2, `all_reduces` → key 3, `reduce_scatters` → key 6) do not 1:1 match each resource-type's producing-opcode name; the binding is by the numeric resource-type ID the Config author chose, which is what `RunImpl` encodes. The names are recorded as the byte-exact value source, not re-attributed to opcodes. The opcode→resource-type switch is on [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `(anon)::RunHloScheduler` (Config-build region) | `0x109718C0..0x10971A99` | reads TCE; builds `SparseCoreQueueAssignmentConfig[-0x138]` | CONFIRMED |
| `GetTpuCompEnv` | `0x1D73DD20` | returns `TpuCompilationEnvironment` `_impl_` | CONFIRMED |
| `AutoOr<long>::FromProtoOrDie` | `0x1092F7E0` | packed `{value rax, has-bit dl}` reader | CONFIRMED |
| `HloPassPipeline::AddPass<SparseCoreQueueAssignment,…>` | `0x10975FC0` | three `vmovups`; Config → `obj[+0x18..]` | CONFIRMED |
| `TpuCompilationEnvironment::_InternalSerialize` | `0x1DB41DC0` | field-number decode source | CONFIRMED |
| `AutoProto_globals_` | `0x223C8968` | null-TCE fallback default instance | CONFIRMED |

---

## The Map Consumer — Definitive Negative Result

### The finding

**No member of `SparseCoreQueueAssignment` reads `[this+0xC0]`.** The reservation map is built once (`RunImpl`) and freed once (the destructor); nothing in between queries it. A full member-by-member scan of the pass band (`0x10FDA3C0..0x10FE4BA0`) for any `internal_find` / `lower_bound` / `operator[]` on `[this+0xC0]`, and for any `lea 0xC0(this)` / `add $0xC0, this` that would pass the map by reference to a helper, found only the `RunImpl` build and the destructor free. The reservation is a **staged / unwired** member in v0.0.40: it carries the configured per-resource caps but does not exclude, cap, or partition candidate cores in the shipping core-selection path.

### The decompile cross-check

The four selection-path members plus the per-computation driver were scanned; none reads `[this+0xC0]`:

| Member function | Address | Lines | `[this+0xC0]` reads |
|---|---|---|---|
| `GetAllowedCores` | `0x10FDA3C0` | 1511 | **0** on `this` (one `+192` at decompile line 748 is on a local device-walk pointer, `[hlo+0xC0]`, not `this`) |
| `SelectCores` | `0x10FDC4E0` | — | 0 |
| `AssignQueueIDsToAsyncStart` | `0x10FDF480` | 833 | **0** |
| `GetLogicalAssignmentGroups` | `0x10FE0820` | — | 0 |
| `AssignQueueIDsForComputation` | `0x10FE1D20` | — | 0 |
| `RunImpl` | `0x10FE4000` | 557 | build only (root store `@0x10FE42B9`) |
| `~SparseCoreQueueAssignment::D2` | `0x10FE4BA0` | — | free only (`clear_and_delete`) |

> **GOTCHA — `[hlo+0xC0]` is not `[this+0xC0]`.** `GetAllowedCores` does carry a single `+0xC0` access (decompile line 748: `v199 = (long*)((char*)v220 + 192)`), but `v220` is a *local* pointer into the device-assignment / replica-group walk — the second pass over the instruction's structure — **not** the pass `this`. The disassembly `add $0xC0` there (`@0x10FDAE39`) operates on `[hlo+0xC0]`, the device-assignment walk. There is no `[this+0xC0]` (reservation-map) read anywhere. The collision of the two `0xC0` offsets is a coincidence of layout, not a use of the map.

### What actually excludes candidate cores

The candidate-core exclusion that *does* run in `GetAllowedCores` is the Swiss-table grouping (the `flat_hash_map<resource_id, btree_set<chip_id>>` at `0x2181D940` and the per-chip occupancy `flat_hash_map<chip_id, long>` at `0x21639C10`) plus the per-resource thread-local budget gate (`__tls_get_addr(&qword_22048D78)`, gated `>= 2`). That mechanism — not this map — is the active reservation-like filter in the selection path; see [SC Core Selection](sc-core-selection.md). The thread-local budget there is documented as a separate, byte-confirmed gate; this `[this+0xC0]` map is *not* its backing store (the budget's seed site is open on that page, but it is not seeded from this map).

### The Live Enforcer — where the same caps take effect

The reservation map is dead, but the per-resource caps it stages are **enforced elsewhere** — in the `LatencyHidingScheduler`'s `AsyncTracker` resource-limit table, built from the **same** TCE fields under the **same** resource IDs:

- **Base collectives `{2,3,6}` ← fields `{288,924,925}`** via `GetSchedulerConfig` → `SchedulerConfig[+0x20/+0x28/+0x30]` → `TpuAsyncTracker` ctor → `tracker[+0x68/+0x70/+0x78]` → `AsyncTracker::SetConcurrentResourceLimits` (`0x13615800`) keys `{2,3,6}`.
- **SC categories `{23..27}` ← the same offsets `+2368..+2400` (fields `{1088..1092}`)** read again in `GetTpuAsyncTracker` (`0x10975520`, lines 118–151) — but here with AUTO → `INT64_MAX`, byte-confirmed — plus one further `AutoOr<long>` knob at TCE offset `+2696` (`0xA88`); these flow through `TpuAsyncTracker::Create` (line 179) into the tracker and out via `TpuAsyncTracker::GetNumAvailableResources` (`0x10FFF600`) per resource id.

The scheduler refuses to co-issue more than `limit` async ops of a given resource type. The field-1202 gate (the 10th Config bool) controls a *second* LHS pass after SparseCore queue assignment, which builds a fresh `TpuAsyncTracker` reading the **same** overlap limits — so the caps are applied before *and* after queue assignment. This page documents only the dead `SparseCoreQueueAssignment` map; the live table is on [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) and [LatencyHidingScheduler Core](../sched/latency-hiding-scheduler-core.md).

> **NOTE — staged duplicate, not a missing feature.** The `[this+0xC0]` map keys the *identical* `{2,3,6,23..28}` from the *identical* fields `{288,924,925,1088..1092}` that the live `AsyncTracker` table uses. It is a redundant / vestigial copy of the scheduler's concurrency table, resolved correctly but read by nobody in this pass. A reimplementer can build the map for fidelity, but should understand that enforcement happens in the scheduler, and that the runtime default for an unset SC overlap knob in the *enforcing* path is `INT64_MAX` (no cap), not the `1` this build site stages.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `SparseCoreQueueAssignment::GetAllowedCores` | `0x10FDA3C0` | candidate-mask build; reads `[hlo+0xC0]`, not `[this+0xC0]` | CONFIRMED |
| `SparseCoreQueueAssignment::AssignQueueIDsToAsyncStart` | `0x10FDF480` | per-collective driver; no map read | CONFIRMED |
| `TpuAsyncTracker::GetNumAvailableResources` | `0x10FFF600` | live per-resource co-issue cap (the real enforcer) | CONFIRMED |
| `AsyncTracker::SetConcurrentResourceLimits` | `0x13615800` | builds the live `resource_type→limit` map | CONFIRMED |
| `(anon)::GetTpuAsyncTracker` | `0x10975520` | reads `1088..1092`/`1130` (AUTO → `INT64_MAX`) for the tracker | CONFIRMED |

---

## Per-Generation Notes

Nothing in the reservation map's build, the field reads, or the (absent) consumer is generation-branched in code. The map is always nine entries; the AUTO → 1 polarity at the build site is constant; the resource-type IDs are constant. The *enforcing* scheduler path keys on `TpuVersion` for some SC-offload concurrency defaults (the `ShouldEnableConcurrentSparseCoreOffloading` basis, `TpuVersion == 5`), but that is the live `AsyncTracker` path, not this map; see [GetSparseCoreConfig](getsparsecoreconfig.md) for the per-generation SC-offload gate. The map's existence and its nine named knobs document the *intended* reservation surface across silicon; its absence of a reader documents that the enforcement is not wired through this pass in this build.

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreQueueAssignment::RunImpl` (`0x10FE4000`) | builds the reservation map; root → `[this+0xC0]` |
| `HloPassPipeline::AddPass<SparseCoreQueueAssignment,…>` (`0x10975FC0`) | copies the Config struct into the pass object's value window |
| `(anon)::RunHloScheduler` (`0x109718C0` region) | reads the TCE knobs; builds the Config struct |
| `GetSparseCoreResources` (`0x10FDC0A0`) | produces the same `{0,23..28}` resource-type space the map keys |
| `TpuAsyncTracker::GetNumAvailableResources` (`0x10FFF600`) | the live consumer of the same caps (the real enforcer) |
| `AsyncTracker::SetConcurrentResourceLimits` (`0x13615800`) | the live `resource_type→limit` table builder |

## Cross-References

- [SC Core Selection](sc-core-selection.md) — the `GetAllowedCores` / `SelectCores` policy this map was meant to feed; the `AssignQueueIDsToAsyncStart` caller chain and the thread-local budget that *does* exclude cores (not this map).
- [GetSparseCoreConfig](getsparsecoreconfig.md) — the offload op-type enum and the SC-offload scheduler gate; the source of the `{23..28}` resource-type categories.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the SC-MLO pass pipeline the queue-assignment pass runs inside.
- [SparseCore Hardware Architecture](architecture.md) — the geometry and the 4:1 SC:TC ratio that bounds the physical core count the policy selects from.
- [SparseCore Overview](overview.md) — the navigational entry for Part IX.
- [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) — the **live** `AsyncTracker` resource-limit table: `SetConcurrentResourceLimits`, `GetNumAvailableResources`, the `kSparseCore*` names, and the same `{2,3,6,23..28}` keys / `{288,924,925,1088..1092}` fields enforced for real.
- [LatencyHidingScheduler Core](../sched/latency-hiding-scheduler-core.md) — the scheduler whose `AsyncTracker` actually throttles concurrent async issue, and the field-1202-gated post-SC-assignment rerun.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore back-end — [back to index](../index.md)
