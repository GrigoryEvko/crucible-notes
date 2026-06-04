# The net_router Emitter Pipeline

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`); `.data.rel.ro` carries a `0x200000` VMA→file delta. Other versions will differ.*

## Abstract

The `net_router` emitter pipeline is the **lowering side** of limited-ICI collective routing: the staged machinery that turns a collective's replica-group / device-assignment relationship into a flat list of `net_router::Transfer` records, hands that list to the hop-assignment solver, and finally replays the resulting schedule as a per-step inter-chip-interconnect (ICI) DMA program. It is one of two routing artifacts in the build — the *per-collective explicit schedule*, distinct from the *resilient per-link auto-route table* — and lives entirely in the `xla::jellyfish::net_router` namespace.

This page owns three of the pipeline's stages and the records that flow between them:

1. **The per-collective `Transfer`-set builders** — `CreateAllToAllTransfers`, `CreateAllGatherTransfers`, and (cross-referenced) `CreateCollectivePermuteTransfers`. Each decodes a flat device/replica id into a torus coordinate, maps the coordinate to a physical core id, and emits one or more 16-byte `{src_core, src_index, dst_core, dst_index}` `Transfer` records. The three differ only in *how* they enumerate the source→target relationship.
2. **The staged pipeline callbacks** — the three deferred `std::function` closures (`$_4` defer-at-step, `$_1` buffer-release, `$_2` commit-placement) that `CreateRoutingSchedule` builds and the discrete-event simulator drains. This page documents how the emitter *constructs and defers* them; the solver internals (the heap, the comparator, the candidate-direction generation) live on [Create Routing Schedule](create-routing-schedule.md).
3. **The emission driver** — `net_router::EmitRoutingCode`, which consumes the schedule (or runs `CreateRoutingSchedule` directly) and replays it per core per step: allocate sync flags, read the 4 direction columns, issue the per-step DMA + wait + prefetch around the `kPipelineFactor=3` window.

The *schedule* the builders feed — the heap walk, the `Schedule` record, `PointerType` — is on [Create Routing Schedule](create-routing-schedule.md). The auto-route per-link table and the `Direction[] → PerLinksRoutingTable` lowering are on [Unicast Route Emission](unicast-route-emission.md) and [Route-Table Generation](route-table-generation.md). This page links them and does not re-derive them.

For reimplementation, the contract is:

- **The `Transfer` record** — the 16-byte `{src_core@0, src_index@4, dst_core@8, dst_index@0xc}` quad every builder writes and `CreateRoutingSchedule` / `EmitRoutingCode` consume.
- **The three builders** — the shared id→coordinate→core machinery, and each collective's enumeration: A2A's bidirectional pair set over ordered replica-group positions, AG's `i→j` broadcast with an ordinals dedup table, CP's per-`source_target_pair` set.
- **The pipeline callbacks** — `$_4` as the deferral primitive, `$_1` as the buffer-release / in-flight tracker, `$_2` as the once-only commit; how the emitter builds the closures and defers them to the right step.
- **`EmitRoutingCode`** — the per-core base index, the 4-direction read, the per-step DMA / wait / prefetch sequence, and the `AllocateScopedSflag` sync barriers that enforce the pipeline window.

| | |
|---|---|
| **Namespace** | `xla::jellyfish::net_router` |
| **A2A builder** | `AllToAllEmitterBase::CreateAllToAllTransfers` @ `0x10f05580` |
| **AG builder** | `(anon)::CreateAllGatherTransfers` @ `0x1380ea20` |
| **CP builder** | `(anon)::CreateCollectivePermuteTransfers` @ `0x13470fe0` (see [create-routing-schedule](create-routing-schedule.md)) |
| **Pipeline callbacks** | `$_4` @ `0x13825b60` · `$_1` @ `0x13826dc0` · `$_2` @ `0x13827760` |
| **Solver** | `CreateRoutingSchedule` @ `0x1381c6a0` → `CreateRoutingScheduleLiteral` @ `0x13822400` |
| **Emission driver** | `net_router::EmitRoutingCode` @ `0x13819ca0` |
| **`Transfer` record** | 16 bytes `{src_core@0, src_index@4, dst_core@8, dst_index@0xc}` |
| **Pipeline factor** | `kPipelineFactor = 3` (3-stage DMA latency window) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile + demangled symbols |

---

## 1. The `Transfer` Record

### Purpose

A `net_router::Transfer` is the atomic unit shared by every stage of the pipeline. It names a single logical move of one buffer slot from a source core to a destination core, expressed in **physical core ids** — not chip coordinates, not device ids. The per-collective builders produce a `std::vector<Transfer>`; `CreateRoutingSchedule` consumes an `absl::Span<Transfer const>` (the span type is visible in the `EmitRoutingCode` mangled signature: `absl::Span<Transfer const>`); and the schedule literal serializes one record per `(core, step, direction)` slot.

### Layout

```text
net_router::Transfer (16 bytes)
  +0x00  int  src_core     physical core id of the source
  +0x04  int  src_index    buffer / slot ordinal within the source
  +0x08  int  dst_core     physical core id of the destination
  +0x0c  int  dst_index    buffer / slot ordinal within the destination
```

The 16-byte size is byte-confirmed two ways: the builders index a `vector<Transfer>` with element stride `0x10` (A2A writes a `0x20`-byte *pair* slot — two adjacent `Transfer`s), and `CreateAllToAllRoutingScheduleTable` re-scales the count with `shl $0x4` (×16) before handing the span to `CreateRoutingScheduleLiteral`. The decompiled builders return `absl::StatusOr<std::vector<net_router::Transfer>>` (the demangled return type appears verbatim in the `MakeErrorStreamWithOutput<...vector<net_router::Transfer>...>` error-stream instantiations).

> **NOTE —** `src_index` / `dst_index` are **slot / rank ordinals within the address space the buffer lives in**, not core ids. The core ids occupy `+0x00` / `+0x08`. The semantic of the index field differs per collective (§2): A2A uses the within-group position; AG fixes `src_index=0` and sets `dst_index` to the source rank; CP derives it from the read/write buffer ordinal. The downstream `Pointer` carries this index alongside a `PointerType` tag (see [create-routing-schedule § PointerType](create-routing-schedule.md#the-pointertype-enum)).

### Function Map

| Function | VMA | Role | Confidence |
|---|---|---|---|
| `Transfer::ToString` | — (called for debug logs) | format `{src_core, src_index, dst_core, dst_index}` for `VLOG` | CERTAIN |
| `CreateAllToAllTransfers` | `0x10f05580` | A2A `vector<Transfer>` builder | CERTAIN |
| `CreateAllGatherTransfers` | `0x1380ea20` | AG `vector<Transfer>` builder | CERTAIN |
| `CreateCollectivePermuteTransfers` | `0x13470fe0` | CP `vector<Transfer>` builder | CERTAIN |

---

## 2. The Per-Collective `Transfer`-Set Builders

All three limited-ICI collectives build the **same** 16-byte `Transfer` list and feed it into the same solver; they differ only in how the source→target relationship is enumerated. The common machinery is identical across the three.

### 2.1 Shared machinery — id → coordinate → core

Every builder performs the same three-step decode for each participant:

1. **Group membership.** CP reads the HLO `source_target_pairs` (the user permutation). A2A reads `device_list()` and `has_replica_groups()`; AG reads `GetParticipatingDevicesGroups(DeviceAssignment, replica_groups, mode)` after `GetCollectiveOpGroupMode`. The result is a set of device groups, each a small `InlinedVector` of device ids.
2. **id → logical coordinate.** A flat device/replica id is decoded by integer division (`idiv`/`div`) against a `LogicalTopologyInfo` dimension, then a strided multi-dimensional linearization (an 8-wide `imul`/`add` dot-product against per-dim strides).
3. **coordinate → physical core id.** The coordinate is mapped through a coord→core-id table at `LogicalTopologyInfo+0x18` (CP uses `+0x10` on its own info struct). The resulting core id is what lands in `Transfer.src_core` / `Transfer.dst_core`.

`channel_id()` and `has_replica_groups()` select the group mode; `Target::ReplicaCount` @ `0x1d6141e0` bounds the iteration. This decode chain is byte-confirmed against the access pattern but the per-dimension stride *values* (the origin of the stride vector inside `LogicalTopologyInfo`) were read at the access level only — marked **HIGH** for the exact stride layout.

```c
// shared decode (schematic; per-builder offsets noted in §2.2–2.3)
for each device_group in groups:
    for each member id in device_group:
        coord = decode_coordinate(id, LogicalTopologyInfo.dims)   // idiv + strided linearization
        core  = coord_to_core_table[coord]                        // LogicalTopologyInfo+0x18
        // ... emit Transfer(s) using core ids ...
```

### 2.2 AllToAll — bidirectional pairs over ordered positions

`AllToAllEmitterBase::CreateAllToAllTransfers` @ `0x10f05580` (TU `all_to_all_emitter_base.cc`, path str @ `0x878b4af`) computes the group structure, then emits a *symmetric pair* per ordered position pair within each replica group.

```c
function CreateAllToAllTransfers(hlo, target, topo_info):          // 0x10f05580
    devices = hlo.device_list()                                    // 0x1e5a95c0 (vtable +0x28)
    cid     = hlo.channel_id()                                     // 0x1e59ff80
    mesh_dim0 = topo_info+0x00;  mesh_dim1 = topo_info+0x04
    if hlo.has_replica_groups():                                   // 0x1e5a95e0
        group_size = *(group_vtable+0x18)
    else:
        group_size = (cid & 1) ? mesh_dim0 : mesh_dim1             // cmovne @0x10f055f3
    total_chips = ChipBounds.X(TpuTopology+0x58) * ChipBounds.Y(+0x5c)
    RET_CHECK total_chips % group_size == 0                        // str @0x9fc9afc (line ~158)
    num_groups = total_chips / group_size
    // allocate num_groups × 24-byte group records
    for each group in groups:
        RET_CHECK group.size() == group_size                       // str (line ~650)
        for src_pos in [0, group_size):
            for dst_pos in [0, group_size):                        // ordered pairs
                core_src = coord_to_core(group[src_pos])
                core_dst = coord_to_core(group[dst_pos])
                // emit a 0x20-byte PAIR slot = two adjacent Transfers
                Transfer A = { src_core=core_src, src_index=dst_pos, dst_core=core_dst, dst_index=src_pos }
                Transfer B = { src_core=core_dst, src_index=dst_pos, dst_core=core_src, dst_index=src_pos }
```

Each ordered position pair contributes a **forward and a reverse hop** (a `0x20`-byte slot holding two adjacent `Transfer`s), so the complete all-to-all permutation — every rank sends slot-*j* to rank-*j* and receives slot-*i* from rank-*i* — is expressed as a symmetric `core_i ↔ core_j` set. `src_index` / `dst_index` are the within-group rank ordinals (the loop position counters), not core ids.

The byte anchors: `device_list` @ call site `0x10f055a5`; `channel_id` @ `0x10f055cf`; `has_replica_groups` @ `0x10f055d9`; `group_size` `cmovne` @ `0x10f055f3`; `total_chips = X·Y` `imul` @ `0x10f05610`; `num_groups` `idiv` @ `0x10f05626`; the `total_chips % group_size == 0` RET_CHECK string @ `0x9fc9afc` (line `0x9e`); the coord→core table base at `LogicalTopologyInfo+0x18`; Transfer A write @ `0x10f05d84`; Transfer B writes @ `0x10f05d30` / `0x10f05e00` / `0x10f05ee7`; the `0x20` pair-stride advance @ `0x10f05d4a`.

> **GOTCHA —** the A2A loop is over **ordered** position pairs (`src_pos`, `dst_pos`) and emits both directions per slot. A reimplementation that iterates only `src_pos < dst_pos` and emits one direction will drop exactly half the all-to-all traffic — the reverse hops — and the schedule will be silently incomplete.

### 2.3 AllGather — `i→j` broadcast with ordinals dedup

`(anon)::CreateAllGatherTransfers` @ `0x1380ea20` (TU `all_gather_emitter.cc`, path str @ `0x8761218`) casts the HLO to `HloAllGatherInstruction`, resolves the group mode, fetches the participating device groups, then emits a single `Transfer` per `(source rank i, dest rank j)`.

```c
function CreateAllGatherTransfers(hlo, target, topo_info):         // 0x1380ea20
    ag   = cast<HloAllGatherInstruction>(hlo)                      // 0xf313920
    mode = GetCollectiveOpGroupMode(ag.use_global_device_ids,      // 0x1e52b8a0
                                    ag.channel_id.has_value)        //  (HLO+0xf0 bit, HLO+0xc8)
    groups = GetParticipatingDevicesGroups(device_assignment,      // 0x1e46bc20
                                           replica_groups, mode)
    ordinals[core] = -1   for all core                             // sentinel dedup table (r13)
    for each group in groups:
        for i in [0, group.size()):       // source rank
            core_i = coord_to_core(group[i])
            RET_CHECK group[i] >= 0                                // str @0x9fc6648
            RET_CHECK ordinals[core_i] < 0                         // str @0x9fd0a94 — not yet assigned
            for j in [0, group.size()):   // dest rank
                core_j = coord_to_core(group[j])
                RET_CHECK group[j] >= 0                            // str @0x9fc65a3
                Transfer = { src_core=core_i, src_index=0, dst_core=core_j, dst_index=i }
```

`src_index` is **always 0** — each rank contributes its single input shard. `dst_index = i` is the slot the source rank's data occupies in the gathered output. The `ordinals` sentinel table (per-core, `-1` = unseen) deduplicates self/duplicate source-core assignment: `RET_CHECK ordinals[group[i]] < 0` fails if the same source core is enumerated twice. The builder also emits two `VLOG` lines for debugging — `"transfers: "` (str @ `0xa232d61`) and `"ordinals: "` (str @ `0xa23572f`) — each formatting `Transfer`s via `Transfer::ToString`.

Byte anchors: `HloAllGatherInstruction` cast @ `0x1380ea44`; `GetCollectiveOpGroupMode` @ `0x1380ea81`; `GetParticipatingDevicesGroups` @ `0x1380eabf`; member list `(%r8)[idx]` 4-byte reads @ `0x1380efe8`; `Transfer` write `{src@0=r10d, idx@4=$0, dst@8=r12d, idx@0xc=r9d}` @ `0x1380f000`, second site @ `0x1380f0e3`; ordinals dedup `r13[core]` @ `0x1380f042`/`0x1380f04e`; the three RET_CHECK strings @ `0x9fc65a3` / `0x9fc6648` / `0x9fd0a94`.

> **NOTE —** AllGather has a *second* lowering path (the ND-ring `MeshNDInfo` table) that does **not** go through `CreateAllGatherTransfers`. `AllGatherEmitter::GenerateConstants` @ `0x13801be0` gates on `ShouldUseExplicitRouting` @ `0x13803aa0`: when true it runs `CreateAllGatherTransfers → CreateRoutingScheduleLiteral` (this page); when false it builds an ND-ring replica-info table via `CreateStaticNDRingReplicaInfoTable` @ `0x1c69e900` / `CreateNDRingReplicaInfoTable` @ `0x1c69e7e0`. The ND-ring path is out of scope here.

### 2.4 CollectivePermute — per-`source_target_pair`

`(anon)::CreateCollectivePermuteTransfers` @ `0x13470fe0` is the third builder. It enumerates the HLO `source_target_pairs` (the user-specified permutation) and emits one `Transfer` per `(pair × buffer × read/write)`, with `src`/`dst` resolved through the same id→coord→core machinery. Its full derivation — including the `src_index = read_write_idx · NumReadWritesPerBuffer + buffer` index convention — is on [Create Routing Schedule](create-routing-schedule.md); it is named here only to complete the trio.

### 2.5 The three builders compared

| Collective | Builder (VMA) | Source of pairs | `Transfer` shape per element | Confidence |
|---|---|---|---|---|
| CollectivePermute | `CreateCollectivePermuteTransfers` `0x13470fe0` | HLO `source_target_pairs` | one Transfer per `(pair × buffer × r/w)` | CERTAIN |
| AllToAll | `CreateAllToAllTransfers` `0x10f05580` | replica-group ordered position pairs | bidirectional **PAIR** (`i→j` and `j→i`); `src/dst_index` = within-group position | CERTAIN |
| AllGather | `CreateAllGatherTransfers` `0x1380ea20` | replica-group members `i, j` | `i→j` broadcast; `src_index=0`, `dst_index=i` | CERTAIN |

> **GOTCHA —** AllReduce is **not** in this table. The full-text caller xref of `CreateRoutingScheduleLiteral` finds only AllGather, AllToAll, and CollectivePermute. AllReduce reaches the per-step program through `EmitRoutingCode`'s direct `CreateRoutingSchedule` call (§4, the runtime non-literal path), so it does not use these per-collective `Transfer` builders at all. Confidence: HIGH (consistent with the [overview](overview.md#42-explicit-schedule--the-net_router-route-program)).

---

## 3. The Staged Pipeline Callbacks

`CreateRoutingSchedule` drives its `kPipelineFactor=3` software pipeline as a **discrete-event simulator** over a per-step callback vector. The pipeline is realized by three deferred `std::function<Status(map<XY, IterationInfo>&)>` closures. This page documents how the emitter *constructs and defers* them and what each does on the lowering side; the heap walk that *fires* them and the `Schedule` record they populate are on [Create Routing Schedule § The Per-Hop Buffer Handoff](create-routing-schedule.md#the-per-hop-buffer-handoff).

All three share one homogeneous signature so they can live in one vector. The `map<XY, IterationInfo>&` argument is the per-step destination-XY scoreboard; `$_1` and `$_2` ignore it (they act on captured state) — it exists only to make the deferred-callback vector uniformly typed.

### 3.1 `$_4` — the deferral primitive

```c
function defer_at_step(extra_actions, index, cb):                  // 0x13825b60
    // extra_actions = vector<optional<vector<function<Status(map<XY,IterationInfo>&)>>>>
    //   outer element stride 0x20: vector{ptr@0,size@8,cap@0x10} + optional has_value byte @0x18
    if index < extra_actions.size:
        RET_CHECK extra_actions[index].has_value()                 // str @0xa171d66 (line 0x691)
        extra_actions[index].value.emplace_back(cb)                // 32-byte ymm payload move @0x13825bba
    else:
        grow extra_actions to index+1, engaging empty optional<vector<function>> slots
        // vmovups xmm0 + movq 0,+0x10 + movb 1,+0x18 @0x13825c10..0x13825c27
```

`$_4` is the deferral primitive: append callback `cb` into `extra_actions[index]`, growing the outer vector and engaging an empty `optional<vector<function>>` if `index` is past the end. When the sim advances to step *k* it drains `extra_actions[k]`'s callbacks in order. The element types are pinned by the `__throw_length_error` symbols: `vector<optional<vector<function<Status(map<XY,IterationInfo>&)>>>>` @ `0x13825f30` and the inner `vector<function<...>>` @ `0x13825f35`. The two `$_4` call sites (`0x13820ae8` for `$_1`, `0x13820fd1` for `$_2`) are its **only** callers.

### 3.2 `$_1` — buffer-release / in-flight tracking

When a hop's DMA lands in a `kAlloc` scratch buffer, `$_1` (the deferred closure's `__call_func`, @ `0x13826dc0`) marks that buffer available and records the in-flight DMA. Its capture is a flat `0x28`-byte POD (built @ `0x13820aa1` with `new $0x28`; `__large_clone` @ `0x13827700`, `__large_destroy` @ `0x13827740`; relro-relocated @ `0x21924d58`):

```c
function buffer_release(capture):                                  // 0x13826dc0
    // capture (0x28 POD): {Allocator-set-ptr@0, XY-key@8, deque-ctx@0x18, int available_at@0x20}
    entry = FlatHashMap<XY, Allocator>.find_or_prepare_insert(set, &capture.XY)   // 0x13826de1
    available_at = capture.available_at + 1                        // inc @0x13826e14
    RET_CHECK available.empty() || available.back().second <= available_at        // sorted by step; str @0x8509fa3 (line 0x185)
    RET_CHECK ptr.type == PointerType::kAlloc                      // str @0x873065f (line 0x186)
    RET_CHECK ptr.index.has_value()                                // str @0xa16fa09 (line 0x187)
    RET_CHECK *ptr.index < size                                    // str @0x8672033 (line 0x18c)
    RET_CHECK c_none_of(available, e -> e.first == *ptr.index)     // NO double-release; str @0xa0f3a0c (line 0x18b)
    available.push_back((*ptr.index, available_at))
    latest_dma_out.push_back(*ptr.index | (available_at << 32))    // deque<pair<int,int>>; __add_back_capacity @0x13826f4d
```

Two invariants form the buffer handoff: **availability** (a buffer index enters a per-destination-XY ordered list keyed by release step; the next hop reads it only after it appears, which combined with the pipeline factor is why the next hop runs `kPipelineFactor` steps later) and **in-flight serialization** (the `(index, step)` pair is pushed onto the `latest_dma_out` deque, and the conflict invariant `!latest_dma_out.contains({src, block})` — checked in `LogAndValidatePaths` — forbids a second DMA from the same source block while one is in flight).

> **QUIRK —** the relay buffers tracked here are **always `kAlloc`** (`RET_CHECK ptr.type == PointerType::kAlloc`, string `"ptr.type == PointerType::kAlloc"` @ `0x873065f`). The collective's real `kInput`/`kOutput` endpoints never enter the in-flight tracker; only intermediate scratch hops do.

### 3.3 `$_2` — commit-placement

When a hop's endpoints are fixed, `$_2` (`__call_func` @ `0x13827760`) writes the 16-byte `Action` into the schedule's `placement` array for each transfer arriving at this step. Uniquely among the three, its capture is a `0x30`-byte object that **owns** an `absl::InlinedVector<int,1>` (the transfer-id set), built @ `0x13820f6f` with `new $0x30`; `__large_clone` @ `0x13827840` deep-copies the InlinedVector via `Storage<int,1>::InitFrom` @ `0x13826580`; `__large_destroy` @ `0x138278c0`; relro-relocated @ `0x21924d88`:

```c
function commit_placement(capture):                                // 0x13827760
    // capture (0x30): {placement-ctx@0, InlinedVector<int,1> transfer_ids@8, Action16 payload@0x20}
    list = (capture[8] & 1) ? &capture[0x10] (inline) : capture[0x10] (heap)
    for t in transfer_ids:
        RET_CHECK t < placement.size()                             // cmp @0x138277a7, ud2 @0x138277e0
        RET_CHECK !placement[t].has_value()                        // place ONCE; str @0xa171d87/d88 (line 0x6d9)
        placement[t][0..0x10) = capture.Action16                   // vmovups @0x138277c0
        placement[t].has_value = 1                                 // movb $1,0x10 @0x138277c9
    // placement record stride 0x14 (5×4): {int@0,int@4,int@8,int step@0xc,byte has_value@0x10}
```

The captured int-list is the set of transfer ids committed at this step; the `0x10`-byte payload is the `Action` endpoint quad (two `Pointer`s). The committed `Action` is what later lands in the per-`{core, step, direction}` slot the Type-5 literal serializes.

### 3.4 Closure construction in the main loop

The emitter builds and defers the two closures inside the `CreateRoutingSchedule` main loop:

| closure | capture alloc | capture build sites | deferred via `$_4` at |
|---|---|---|---|
| `$_1` (buffer-release) | `new $0x28` @ `0x13820aa1` | payload @ `0x13820ab8`, ctx `r12=0x20(*(-0x30))` @ `0x13820ac5`, `int -0xa8` step @ `0x13820ad0` | `0x13820ae8` (then free @ `0x13820af5`) |
| `$_2` (commit-placement) | `new $0x30` @ `0x13820f6f` | IV head @ `0x13820f90`, IV body @ `0x13820f9a`, `Action16` @ `0x13820fb0` | `0x13820fd1` (cleanup @ `0x13820fe0`) |

Both are heap (`"large"`) `std::function` policies. The defer step is read as `-0xa8(rbp)` at both sites; `$_1` adds `+1` internally. The precise arithmetic relating these to the popped step and `kPipelineFactor` was read at the immediate level only — whether `available_at = step+3` exactly or `step+1` with the `+3` enforced solely in `LogAndValidatePaths` is not isolated to a single constant. Confidence: HIGH for the exact defer-step computation.

| callback | VMA | role | capture (bytes) | key CHECK (str / line) | Confidence |
|---|---|---|---|---|---|
| `$_4` | `0x13825b60` | defer cb to step *k* | (operator args) | `extra_actions[index].has_value()` `0xa171d66` / `0x691` | CERTAIN |
| `$_1` | `0x13826dc0` | buffer-release / in-flight | `0x28` flat POD | `available.back().second<=available_at` `0x8509fa3`/`0x185`; `kAlloc` `0x873065f`/`0x186` | CERTAIN |
| `$_2` | `0x13827760` | commit-placement | `0x30` (owns `IV<int,1>`) | `!placement[transfer].has_value()` `0xa171d87`/`0x6d9` | CERTAIN |

---

## 4. The Emission Driver — `EmitRoutingCode`

`net_router::EmitRoutingCode` @ `0x13819ca0` is the pipeline's terminal stage: it turns the schedule into actual `Llo` IR — the per-step ICI DMA program a core replays at runtime. Its mangled signature (confirmed in `*_functions.json`) takes an `LloRegionBuilder`, a `MemUnit`, an `absl::Span<Transfer const>`, an optional `MemorySpace` span, a `ProgramSharedRegistry*`, a `variant` of address/barrier callbacks (the `function<LloMemoryAddress(PointerType, PointerType, LloValue*)>` resolver among them), an optional `BarrierConfig`, and a `LogRecorder*`.

### 4.1 Driver structure

```c
function EmitRoutingCode(builder, …, transfers, …, callbacks, …): // 0x13819ca0
    schedule = CreateRoutingSchedule(topology, transfers)          // 0x13820… direct call @ line 427
    // allocate the per-step sync flags
    sflag0 = builder.AllocateScopedSflag(0, 0)                     // @0x… (line 662)
    sflag4 = builder.AllocateScopedSflags(4, 0, 0)                 // 4-wide (line 663)
    sflag8 = builder.AllocateScopedSflags(8, 0, 0)                 // 8-wide (line 664)
    for each core this program emits for:
        base = GetLimitedIciRoutingTableIndex(core, …, "net-router", …)   // net_util:: (line 1056)
        for each step:
            RoutingTableStartDma(emitter, …)                       // issue the step's DMA  (line 1105)
            RoutingTableWaitForDmaInFlight(emitter, …)             // wait on the pipeline window (line 1106)
            e0 = GetRoutingTableElement(emitter, …, base, 0)       // N column
            e1 = GetRoutingTableElement(emitter, …, base, 1)       // W column
            e2 = GetRoutingTableElement(emitter, …, base, 2)       // S column
            e3 = GetRoutingTableElement(emitter, …, base, 3)       // E column   (lines 1212–1215)
            RoutingTableStartPrefetchIfNeeded(emitter, …, base)    // prefetch next  (line 1108/1222)
```

The byte anchors: the direct `CreateRoutingSchedule(&v343, …)` call @ decompile line 427 (this is the runtime non-literal path AllReduce also uses); `AllocateScopedSflag(0,0)` @ line 662 and `AllocateScopedSflags(4,…)` / `AllocateScopedSflags(8,…)` @ lines 663–664; `GetLimitedIciRoutingTableIndex` with the `"net-router"` / `"net-router-send"` tags @ lines 1056 / 1391; the `RoutingCodeEmitter::RoutingTableStartDma` / `RoutingTableWaitForDmaInFlight` / `RoutingTableStartPrefetchIfNeeded` triple @ lines 1105–1108 and 1204–1222; the four `GetRoutingTableElement(…, 0/1/2/3)` reads @ lines 1212–1215.

### 4.2 The 4-direction read

Each step reads **four** `GetRoutingTableElement` columns — one per ICI compass port `{N=0, W=1, S=2, E=3}` (the `Direction` enum on [create-routing-schedule § Direction](create-routing-schedule.md#the-direction-enum)). The four columns are the four `Action` slots committed by `$_2` for that `(core, step)` cell; each non-zero element is a DMA to issue on that port, each zero element means "no DMA on this core/step/port." For the literal path, `GetRoutingTableElement` decodes the packed `s32` element (`SerializeAction` layout, on [create-routing-schedule § Schedule literal](create-routing-schedule.md) and [route-table-generation](route-table-generation.md)); for the direct path, the schedule's `placement` is read in memory.

### 4.3 The sync-flag pipeline window

The `AllocateScopedSflag` / `AllocateScopedSflags(4)` / `AllocateScopedSflags(8)` allocations are the runtime barriers that enforce the `kPipelineFactor=3` window the `$_1` / `latest_dma_out` tracking models at compile time. `RoutingTableWaitForDmaInFlight` blocks until a step's in-flight DMA has retired far enough that its scratch buffer is readable — the runtime realization of the `available` list and the `!latest_dma_out.contains(...)` invariant. `RoutingTableStartPrefetchIfNeeded` overlaps the next step's source fetch with the current DMA, sustaining the 3-stage pipeline depth. The mapping from the compile-time `placement` / `latest_dma_out` output to the exact runtime sflag indices was not byte-traced here (the sflag allocation is confirmed; its per-step index assignment is **LOW**).

> **NOTE —** the schedule literal answers *which buffer pointers a core DMAs between* at one step on one port; it says nothing about *which physical chips the bytes traverse*. That multi-hop link path comes from the resilient per-link route table, resolved by the on-chip routing engine when the descriptor carries the destination chip id. See [Unicast Route Emission](unicast-route-emission.md), [Intra-Chip Descriptor](../dma/intra-chip-descriptor.md), and [overview § 1.1](overview.md#11-two-outputs-one-section-route-table-vs-route-schedule).

---

## 5. The Pipeline At A Glance

| stage | function / site (VMA) | output | Confidence |
|---|---|---|---|
| CP `Transfer` set | `CreateCollectivePermuteTransfers` @ `0x13470fe0` | `vector<Transfer>` (`source_target_pairs`) | CERTAIN |
| A2A `Transfer` set | `CreateAllToAllTransfers` @ `0x10f05580` | `vector<Transfer>` (bidir replica pairs) | CERTAIN |
| AG `Transfer` set | `CreateAllGatherTransfers` @ `0x1380ea20` | `vector<Transfer>` (`i→j` broadcast) | CERTAIN |
| build schedule (solver) | `CreateRoutingSchedule` @ `0x1381c6a0` | `Schedule{Step[]·{XY→Action[4]}}` | CERTAIN |
| ↳ defer cb to step | `$_4` @ `0x13825b60` | `extra_actions[step] += function` | CERTAIN |
| ↳ buffer-release / in-flight | `$_1` @ `0x13826dc0` | `available` list + `latest_dma_out` deque | CERTAIN |
| ↳ commit placement | `$_2` @ `0x13827760` | `placement[transfer]` `Action` + `has_value` | CERTAIN |
| validate (pipeline factor 3) | `LogAndValidatePaths` @ `0x13823dc0` | `Schedule` metrics | CERTAIN |
| Type-5 route literal | `CreateRoutingScheduleLiteral` @ `0x13822400` | `s32[X·Y·steps·4+4]` | CERTAIN |
| A2A schedule table | `CreateAllToAllRoutingScheduleTable` @ `0x10f061c0` | literal (RET_CHECK device_assignment) | CERTAIN |
| AG constants (explicit gate) | `AllGatherEmitter::GenerateConstants` @ `0x13801be0` | literal *or* ND-ring table | CERTAIN |
| runtime replay | `EmitRoutingCode` @ `0x13819ca0` | per-step ICI DMA program | CERTAIN |

---

## Cross-References

- [Routing Overview](overview.md) — the route-table-vs-route-schedule split and the end-to-end pipeline this emitter sits inside
- [Create Routing Schedule](create-routing-schedule.md) — the hop-assignment solver: the heap walk, the `SchedulingQueueKey` comparator, the `Schedule` record, the `PointerType` enum, and CP's `CreateCollectivePermuteTransfers` index convention — the *consumer* of the `Transfer` lists this page builds
- [Unicast Route Emission](unicast-route-emission.md) — the `Direction[] → PerLinksRoutingTable` row and the routing-table index that resolves the descriptor's multi-hop link path
- [Route-Table Generation](route-table-generation.md) — the Type-5 route literal column → ICI-port mapping and the resilient table generation
- [Intra-Chip Descriptor](../dma/intra-chip-descriptor.md) — the per-step DMA descriptor whose routing field the schedule's `Action` endpoints supply
- [back to index](../index.md)
