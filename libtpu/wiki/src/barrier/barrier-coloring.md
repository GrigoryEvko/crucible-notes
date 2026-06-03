# BarrierColoring

> *Every address, field offset, opcode value, and enum value on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`; 781,691,048 B, not stripped — full C++ symbols). `.text` VA equals file offset at `0xe63c000`; `.rodata`/`.lrodata` are identity-mapped. All addresses are VMA. Other wheel versions differ.*

## Abstract

`BarrierColoring` is the **TensorCore (TC) barrier-deduplication engine**: a greedy graph-coloring pass that decides which concurrent TC collectives may *share* a barrier sync flag and which must be forced onto *distinct* ones. Where the SparseCore barrier pass dedups purely on a static ring-config hash (no notion of schedule overlap), the TC pass builds an **interference graph** over the *live ranges* of async collectives and colors it: two collectives that share a `TensorCoreBarrierKey` but whose async start..done windows overlap in the call-graph-ordered schedule get an interference edge and are colored apart. The [overview](overview.md) describes the SFLAG-based barrier model and the `BarrierType` enum; the [SFLAG number binding](barrier-to-sflag-binding.md) describes the compiler-barrier → hardware-SFLAG-number map; [`InferBarrierConfig`](infer-barrier-config.md) describes the per-gen SFLAG-map source. This page owns the **greedy coloring engine** (the `Run` → `AssignColorForConflictingOp` → `VisitNodes`/`CollectConflictInfo` → `AssignColorsGreedy` pipeline, the conflict-edge construction, and the first-fit color scan) plus the **`BarrierConfig` → chip-SFLAG lowering** (`CustomKernelEmitter::Emit` → `MaybeInsertGlobalBarrier` / `RunPasses`) that turns a colored barrier into a concrete sync-flag memref.

The engine is templated as `xla::jellyfish::BarrierColoring<Policy<TensorCoreBarrierKey>>` and instantiated twice — once for **collective-permute** (`AsyncCollectivePermutePolicy`) and once for **async-barrier'd all-to-all** (`AsyncAllToAllWithAsyncBarrierPolicy`). The two instantiations share *all* algorithm code and differ only in six policy predicates (what counts as an async start/done, how to derive the key). The output of each pass is a pair: a `map<HloInstruction*, long>` assigning every TC collective a color, and a `flat_hash_set<HloInstruction*>` of the ops that took a *non-zero* color — the "must get a distinct barrier" set that feeds the `has_conflict` argument of `DetermineBarrierConfigForKey` in the [barrier-assignment producer](overview.md) (`@ 0x109c6fa0`).

For reimplementation, the contract is:

- **Dedup is graph coloring, not hashing.** Color 0 is the default/shared barrier; colors `>= 1` are additional distinct barriers forced by conflicts. The chromatic number of a key's interference graph equals the max concurrent in-flight collectives of that key, which is exactly how many distinct barrier ids that key consumes.
- **The conflict predicate is live-range overlap.** Two async collectives conflict iff their `async-start … async-done` windows overlap in schedule order. The schedule must be walked via a CallGraph DFS (not unordered iteration) so the builder can track which collectives are currently open at each program point.
- **The color search is deterministic first-fit.** For each node, build the set of neighbor colors, then scan `0, 1, 2, …` and take the first integer not used by a neighbor (smallest-available-color). Deterministic input order (a `std::map`) + deterministic search ⇒ reproducible coloring, which matters for cache-stable compiles.
- **The decision and the lowering are joined only through the proto.** The chosen `BarrierConfig {type, id}` is written into the HLO's `BackendConfig` by the assignment pass and read back at emit time by `CustomKernelEmitter::Emit`. There is no shared in-memory state between coloring and lowering.
- **Both barrier arms end at the same SFLAG primitive.** A `type 1` *global* (`GLOBAL`) barrier lowers to `AllocateAtOffsetOp(MemorySpace::sflag)` wrapped in `scf.for` loops over `tpu.sem_signal`/`tpu.sem_wait` (the TC tree barrier); a `type 2`/`3` *per-key* (`REPLICA`/`CUSTOM`) barrier flows through `RunPasses` to the same `GetSyncFlagForBarrierId` → `AllocateAtOffsetOp(sflag)` path the SparseCore per-ring barrier uses. Same chip SFLAG block; different atomic op family. The `type` is the literal `BarrierConfig.type` proto integer the lowering compares (`== 3` ⇒ per-key); the enum names are `GLOBAL=1`/`REPLICA=2`/`CUSTOM=3` (see [overview](overview.md)).

| | |
|---|---|
| **Engine class** | `xla::jellyfish::BarrierColoring<Policy<TensorCoreBarrierKey>>` (TU `barrier_assignment.cc`) |
| **ACP `Run`** | `@ 0x109cf600` (collective-permute policy) |
| **A2A `Run`** | `@ 0x109d1a60` (async-barrier'd all-to-all policy) |
| **Orchestrator** | `AssignColorForConflictingOp` `@ 0x109cf6c0` (ACP) / `@ 0x109d1b20` (A2A) |
| **Schedule walk** | `VisitNodes` `@ 0x109d1240` → `VisitNodesInternal` `@ 0x109d1420` (CallGraph DFS) |
| **Interference builder** | `CollectConflictInfo` `@ 0x109d4a20` (ACP) / `@ 0x109d5a60` (A2A) |
| **Color search** | `AssignColorsGreedy` `@ 0x109d0c80` + inner lambda `@ 0x109d0de0` (first-fit) |
| **Conflict node** | `ConflictInfo`, `sizeof == 0xb0`, allocated `@ 0x109cfd56` |
| **SFLAG lowering** | `CustomKernelEmitter::Emit` `@ 0x1321ad60` → `MaybeInsertGlobalBarrier` `@ 0x1321ac20` / `RunPasses` `@ 0x13202780` |
| **Global SFLAG primitive** | `sparse_core::AllocateAtOffsetOp::create` `@ 0x145a5aa0` (`MemorySpace::sflag`) |
| **TC barrier ops** | `tpu::SemaphoreSignalOp` `@ 0x14b442e0` / `tpu::SemaphoreWaitOp` `@ 0x14b45460` |
| **Confidence** | CONFIRMED (engine + lowering bodies decompiled and double-checked) unless a row says otherwise |

---

## The Engine Class and Its Two Instantiations

### Purpose

`BarrierColoring` exists because two TC collectives that share the same `TensorCoreBarrierKey` are *not* automatically safe to share a barrier sync flag. If both are in flight at the same time (e.g. a double-buffered collective-permute pipeline, where one CP's `start..done` window opens while a prior CP of the same key is still live), they must signal on *different* sync flags or they would corrupt each other's completion count. The engine detects exactly this case by modeling each async collective as an interval (`start` opens, `done` closes) and coloring the resulting interference graph.

The class is a function-object template:

```cpp
// xla::jellyfish (TU platforms/xla/service/jellyfish/barrier_assignment.cc)
template <typename Policy>   // Policy = AsyncXxxPolicy<TensorCoreBarrierKey>
class BarrierColoring {
 public:
  // returns {color map, conflict-op set}
  StatusOr<std::pair<std::map<HloInstruction*, long>,
                     flat_hash_set<HloInstruction*>>>
  Run(HloModule* module);

 private:
  StatusOr<...> AssignColorForConflictingOp(
      HloModule* module, const flat_hash_set<string_view>& computation_filter);
  void VisitNodes(HloModule*, FunctionRef<Status(HloInstruction*, ConflictMap&)>,
                  ConflictMap*);
  Status CollectConflictInfo(HloModule*, ConflictMap&, HloInstruction*);
  std::map<HloInstruction*, long> AssignColorsGreedy(
      const std::map<HloInstruction*, std::set<HloInstruction*>>& adjacency);
};
```

### The two policies

The two instantiations are reached through a 6-slot vtable. Only the predicates differ; the algorithm is byte-identical (the A2A variants of every function — e.g. `AssignColorsGreedy @ 0x109d30e0`, `CollectConflictInfo @ 0x109d5a60` — have the same stack frame and the same loops as the ACP variants).

| vtable slot | method | `AsyncCollectivePermutePolicy` | `AsyncAllToAllWithAsyncBarrierPolicy` | Confidence |
|---|---|---|---|---|
| `+0x10` | `IsAsyncStart` | `opcode == 0x24` (CP-start) `@ 0x109cc080` | `IsCustomCall(kBarrierStart)` `@ 0x109cc640` | CONFIRMED |
| `+0x18` | `IsAsyncDone` | `opcode == 0x23` (CP-done) `@ 0x109cc0a0` | `opcode == 0x11 && GetBarrierStartFromAsyncStart != 0` `@ 0x109cc660` | CONFIRMED |
| `+0x20` | `GetKey` (build key) | normalise→start `@ 0x109d3ec0` | `async_wrapped_instruction` → key `@ 0x109d4460` | CONFIRMED |
| `+0x28` | `GetStartForDone` | `done->operand(0)` `@ 0x109cc0e0` | `GetBarrierStartFromAsyncStart(done)` `@ 0x109cc680` | CONFIRMED |
| `+0x30` | `GetCollectiveForStart` | identity `@ 0x109cc0c0` | `@ 0x109d4400` | CONFIRMED |
| `+0x38` | `ShouldForceGlobalBarrier` | degenerate-CP gate `@ 0x109d3f20` | base `false` `@ 0x109cac00` (inherited) | CONFIRMED |

> **NOTE — verified opcodes.** The decompiled `IsAsyncStart` body for ACP is literally `return *(_BYTE *)(op + 0xc) == 36;` (36 = `0x24`, collective-permute-start) and `IsAsyncDone` is `== 35;` (`0x23`, collective-permute-done). The async-barrier helpers are `async_barrier_util::kBarrierStart @ 0xabe9620`, `GetBarrierStartFromAsyncStart @ 0x11007c80` (HLO async-start `0x11`, backend kind `== 2`, `operand(0)` opcode `0x81` = barrier-start), and `GetAsyncStartFromBarrierStart @ 0x11007d20`.

The two policies need *separate* passes because their async start/done definitions are disjoint: the CP path keys on the HLO collective-permute-start/done opcodes directly, whereas the A2A path keys on an `all-to-all` wrapped in an async-barrier custom call (`kBarrierStart`) inside an HLO async-start/done pair. The async-barrier full op family is documented on the [tensorcore-barrier](tensorcore-barrier.md) page.

---

## `Run` — The Thin Wrapper

`Run` (`@ 0x109cf600` ACP / `@ 0x109d1a60` A2A) is boilerplate: it zero-initialises an empty `flat_hash_set<string_view>` (the "restrict coloring to these computations" name filter — empty here means *all* TC-thread computations), constructs it via the `raw_hash_set` ctor `@ 0x10912d00`, then tail-calls `AssignColorForConflictingOp(module, &name_set) @ 0x109cf6c0`. On return it frees the set's backing array (`DeallocateBackingArray @ 0x21118960`) and returns the `StatusOr<pair<map<HloInstruction*,long>, flat_hash_set<HloInstruction*>>>` that the orchestrator built in `*this`. The algorithm is entirely in `AssignColorForConflictingOp`.

---

## `AssignColorForConflictingOp` — The Orchestrator

`AssignColorForConflictingOp` (`@ 0x109cf6c0` ACP / `@ 0x109d1b20` A2A) receives `(this = result, module, name_set)` and runs five phases.

### 1 — Enumerate computations

`HloModule::computations(name_set) @ 0x10944b40` produces the TC-thread computation view, copied onto the stack frame.

### 2 — Group by key into `ConflictInfo` (`@ 0x109cf810 … 0x109d0404`)

Walk every instruction of every computation. For each op, build its `TensorCoreBarrierKey` inline (read opcode; copy replica groups from `[op+0xd0]`/`[op+0xd8]` via the `ReplicaGroup` ctor `@ 0x20e43de0`; copy src-tgt pairs; channel parity), then `__emplace` into a `std::map<TensorCoreBarrierKey, ConflictInfo>` rooted at frame `-0x108`. On a **new** key, heap-allocate a `ConflictInfo` node (`mov edi, 0xb0; call _Znwm` `@ 0x109cfd56`) and copy the key's replica-group / src-tgt vectors into it — the node carries its own copy of the key payload so it survives the key's lifetime. The map is ordered by `TensorCoreBarrierKey::operator< @ 0x109d6620`. Each `ConflictInfo` thereby groups all collectives that share a key.

The `0xb0`-byte `ConflictInfo` node sub-layout (offsets proven by the construction stores `@ 0x109cfd5b … 0x109cff7b`; sub-member *semantics* inferred from use):

| Offset | Member | Confidence |
|---|---|---|
| `+0x10` | map-node key back-pointer | INFERRED |
| `+0x20` | key opcode byte mirror | INFERRED |
| `+0x28 … +0x38` | replica-group vector (copied) | INFERRED |
| `+0x40 … +0x50` | src-tgt-pair vector (copied) | INFERRED |
| `+0x58 … +0x78` | copied key tail | INFERRED |
| `+0x80` | per-key adjacency `map<HloInstruction*, set<HloInstruction*>>` root | CONFIRMED (used by `AssignColorsGreedy`) |
| `+0x98` | per-key colored `map<HloInstruction*, long>` root | INFERRED |

### 3 — Build the interference graph (`@ 0x109d0469`)

Set up the `CollectConflictInfo` callback (`FunctionRef`; `InvokeObject @ 0x109d4a00`) and call `VisitNodes(module, callback, &conflict_map) @ 0x109d1240`. This populates each `ConflictInfo`'s adjacency map at node `+0x80` with the per-key interference edges.

### 4 — Per-key greedy coloring (`@ 0x109d04dd … 0x109d066c`)

For each `ConflictInfo` entry (map traversal, the adjacency map root at `node+0x80`), call `AssignColorsGreedy(this, &out_colors, &node->adjacency) @ 0x109d0c80`, then merge the returned per-key coloring into the module-wide color map via `__insert_range_unique @ 0x109cc380` into the frame `-0x2b8` tree.

### 5 — Emit the conflict-op set (`@ 0x109d0885 … 0x109d0880`)

For ops whose assigned color is `> 0` (they could not take color 0 = the shared/default barrier, i.e. they conflict with a same-key peer), insert the op into the returned `flat_hash_set<HloInstruction*>` (`find_or_prepare_insert_large @ 0xe63dbe0`).

> **NOTE — `RET_CHECK`.** A `RET_CHECK` at line `0xdf` (`@ 0x109d05f6`) guards the orchestrator against a malformed coloring intermediate.

The output is `(color map, conflict-op set)`. The [barrier-assignment producer](overview.md) consumes both: the conflict set feeds the `has_conflict` argument of `DetermineBarrierConfigForKey @ 0x109c6fa0`, which is exactly how a graph conflict turns a shared `REPLICA(2)` barrier into a fresh `CUSTOM(3)` one — see [How the Coloring Feeds `BarrierConfig`](#how-the-coloring-feeds-barrierconfig).

---

## `VisitNodes` / `VisitNodesInternal` — The Schedule Walk

`VisitNodes @ 0x109d1240`:

1. `CallGraph::Build(module, name_set) @ 0x1e5579e0` — build the call graph over the TC-thread computations.
2. `CallGraph::GetNode(entry_computation) @ 0x1e556600` — the entry node.
3. `VisitNodesInternal(module, callback, call_graph, entry_node, visited_map, conflict_map) @ 0x109d1420` (`@ 0x109d3880` for A2A) — a recursive CallGraph DFS keyed by a `flat_hash_map<CallGraphNode*, long>` of visited stamps, walking each computation's instructions **in schedule order** and invoking the callback per instruction.

The traversal order is essential: `CollectConflictInfo` detects conflicts by tracking which async collectives are *currently open* at each program point, so it must see instructions in schedule order — hence the CallGraph DFS rather than an unordered map iteration.

---

## `CollectConflictInfo` — The Conflict Predicate (Interference-Graph Builder)

`CollectConflictInfo` (`@ 0x109d4a20` ACP / `@ 0x109d5a60` A2A) has signature `lambda(HloInstruction* op, map<TensorCoreBarrierKey, ConflictInfo>& conflict_map) → Status`. It reaches the policy through the vtable `[op_policy]`, using the slots from the table above (`IsAsyncStart [vt+0x10]` `@ 0x109d4a4c`, `IsAsyncDone [vt+0x18]` `@ 0x109d4a62`, `GetStartForDone [vt+0x28]` `@ 0x109d4ac4`, `GetCollectiveForStart [vt+0x30]` `@ 0x109d4b01`).

Per visited op:

- **`IsAsyncStart(op)`** → the op **opens** an async-collective live range. Compute its key (the policy `GetKey` chain), look up its `ConflictInfo` in `conflict_map` (`TensorCoreBarrierKey::operator< @ 0x109d6620` tree walk). Add `op` to the **live-async set** — a local SwissTable, membership hashed on the `HloInstruction*` pointer via the crc32-based `MixingHashState` path (`_mm_crc32_u64`, `@ 0x109d4c7b … 0x109d4cf3`).
- For every **other** currently-live async collective in the set: add a **bidirectional interference edge** — insert each into the other's `ConflictInfo` adjacency `set<HloInstruction*>` (`HloPtrComparator`-ordered set insert, `@ 0x1e5a7b20`, via `__emplace_unique`). Two async collectives whose `start..done` ranges overlap therefore **conflict** and cannot share a barrier sync flag.
- **`IsAsyncDone(op)`** → `GetStartForDone(op) [vt+0x28]` → **close** that start's live range (remove from the live set).
- **`ShouldForceGlobalBarrier(op) [vt+0x38]`** — when true (the ACP degenerate-collective-permute case `@ 0x109d3f20`), the op is steered toward the global barrier irrespective of coloring.
- A `RET_CHECK` (`MakeErrorStream @ 0x20cf6b80`, `add_ret_check_failure @ 0x20cf6be0`, line `0x9a` `@ 0x109d4b84`) fires on a malformed async pair (a done with no matching start, or vice versa).

```text
schedule order →  ... CP-start(A) ... CP-start(B) ... CP-done(A) ... CP-done(B) ...
live set:         { }   {A}            {A,B}          {B}            { }
edges added:                 A—B   (A and B overlap → interference edge)
```

This is the structural difference from the SparseCore barrier pass: the SC pass dedups on a static ring-config key via a `FlatHashMap` with **no notion of schedule overlap**; the TC pass dedups on the **live async-collective interference graph**, so even two collectives with the *same* `TensorCoreBarrierKey` are split apart if they are concurrently in flight.

---

## `AssignColorsGreedy` — First-Fit Smallest-Available-Color

`AssignColorsGreedy` (`@ 0x109d0c80` ACP / `@ 0x109d30e0` A2A) takes one `ConflictInfo`'s interference graph — `const std::map<HloInstruction*, set<HloInstruction*>>& adjacency` — and returns a `std::map<HloInstruction*, long>` (node → color), `HloPtrComparator`-ordered.

**Outer loop** (`@ 0x109d0c80`): iterate the adjacency map; for each node take its neighbor set (`rbx+0x28`) and call the inner lambda `@ 0x109d0de0`, then insert `(node_ptr = [rbx+0x20], color)` into the output color map via `__try_key_extraction_impl @ 0x109d47e0` / `__emplace_unique`.

**Inner lambda** (`@ 0x109d0de0`) — the minimal-free-color search, confirmed byte-for-byte:

1. Build a local `flat_hash_set<long>` `used` (`raw_hash_set`, policy `@ 0x21616168`).
2. For each neighbor in the neighbor set (walked in `HloPtrComparator` order): look the neighbor up in the *so-far* output color map (`__try_key_extraction_impl`); if it already has a color, insert that color into `used` — the SwissTable insert uses `_mm_crc32_u64` to hash the `long` color (`@ 0x109d0e6e` region), `PrepareInsertLarge` / `GrowSooTableToNextCapacityAndPrepareInsert` on growth.
3. Scan `candidate = 0, 1, 2, …`: probe `used` for membership of `candidate` (SwissTable group scan, `@ 0x109d1100 … 0x109d1199`); if **not present**, return `candidate`; else `candidate++` (the `*v6 = v7 + 1` increment, `@ 0x109d11b9` region) and re-probe. Return the **first gap**.

```text
inner_color(node, used_set):
    used = {}
    for nb in neighbors(node):           # HloPtrComparator order
        c = color_map.get(nb)
        if c is not None: used.insert(c)
    candidate = 0
    while candidate in used:             # SwissTable membership (crc32 hash)
        candidate += 1
    return candidate                     # smallest-available-color
```

This is classic first-fit graph coloring. Color 0 is the default/shared barrier; colors `>= 1` are additional distinct barriers needed because of conflicts. The number of colors used per key equals the chromatic number of that key's interference graph (≈ the max concurrent in-flight collectives of that key), and that is exactly how many distinct barrier ids that key consumes. Because the adjacency input is a `std::map` (deterministic key order) and the search is deterministic first-fit, the coloring is **reproducible run-to-run** — no randomness — which matters for cache-stable compiles.

---

## How the Coloring Feeds `BarrierConfig`

The two coloring passes' conflict sets are merged into one `flat_hash_set<HloInstruction*>` in the [barrier-assignment producer](overview.md) (`TensorCoreBarrierAssignment::Run`). In the per-key assignment loop, the entry's first instruction is looked up in that merged conflict set → `has_conflict`, the third argument to `DetermineBarrierConfigForKey @ 0x109c6fa0`:

- **color 0, NOT in conflict set** → `DetermineBarrierConfigForKey` may produce a shared `REPLICA(2)` barrier or a `GLOBAL(1)` barrier per its global-barrier-beneficial heuristic.
- **non-zero color / IN conflict set** → forced to a fresh per-key `CUSTOM(3)` barrier (new id).

This is the precise mechanism by which "two same-key TC collectives are forced apart": the coloring proves their live ranges overlap → they land in the conflict set → `DetermineBarrierConfigForKey` gives them distinct `CUSTOM(3)` `BarrierConfig`s. The `BarrierType` enum (`GLOBAL=1`/`REPLICA=2`/`CUSTOM=3`/`MEGACORE=4`) and the global-barrier semantics are detailed on the [overview](overview.md) and the [global-barrier window](global-barrier-window.md) pages; the resulting SFLAG *number binding* is on the [SFLAG binding](barrier-to-sflag-binding.md) page.

---

## `BarrierConfig` → Chip-SFLAG Lowering

The chosen `BarrierConfig {type, id}` is written into each collective's `BackendConfig` proto by the assignment pass, then read back and lowered at kernel-emission time. Decision and lowering communicate **only** through the proto field — there is no shared in-memory state.

### `CustomKernelEmitter::Emit` — reads `BarrierConfig`, calls the two lowerers

`Emit @ 0x1321ad60` (TU `custom_kernel_emitter.cc`) prepares the kernel module (`GetCustomCallConfig @ 0x13e308c0`, parse the embedded MLIR module, `SetArgLayouts @ 0x13219e80`), then:

- `HloInstruction::backend_config<BackendConfig> @ 0xf58e6c0` + `BackendConfig::CopyFrom @ 0x1d6e7400` → extract the `BarrierConfig` submessage written by the assignment pass. `r13` holds `&BarrierConfig` (defaulting to `BarrierConfig_globals_ @ 0x223a9450` if absent); `rbp-0x548` holds `&CustomCallConfig`.
- call `MaybeInsertGlobalBarrier(module, &CustomCallConfig, &BarrierConfig) @ 0x1321b2cb`.
- call `RunPasses(module, b1, b2, &BarrierConfig) @ 0x1321b2f2` (the two `b` args are emit-mode bools).

The decompile confirms both call sites and the `backend_config` + `CopyFrom` read on the same path.

### `MaybeInsertGlobalBarrier` — type-1 global → SFLAG tree barrier

`MaybeInsertGlobalBarrier @ 0x1321ac20` takes `(ModuleOp by value, CustomCallConfig const* cc, BarrierConfig const* bc)`. The gate (decompiled byte-exact):

```c
v4 = *(int*)(cc + 0x10);                      // CustomCallConfig hasbits
// is_communicating valid iff hasbit 0x200; skip_device_barrier valid iff hasbit 0x2000
v5 = (v4 & 0x200) ? *(char*)(cc + 0x90) : 0;  // [cc+0x90] = is_communicating
v6 = (v4 & 0x2000)? *(char*)(cc + 0x94) : 0;  // [cc+0x94] = skip_device_barrier
v7 = bc ? (*(int*)(bc + 0x20) == 3) : 0;      // BarrierConfig type == 3  ⇒ fresh per-key, NOT global
// barrier-request bit = (v4 & 0x40)
```

Three `RET_CHECK` exits (`MakeErrorImpl<3>`, error strings read directly from `.rodata`, lines/offsets confirmed):

| Line | VA | `.rodata` error string | Trigger |
|---|---|---|---|
| `0xde8` (3560) | `@ 0x1321ac9a` | `"Custom barrier requested for non-communicating custom call."` (`@ 0xa05ba1c`) | barrier requested (`0x40`) but the custom call doesn't communicate |
| `0xdf4` (3572) | `@ 0x1321acbf` | `"The compiler failed to allocate a barrier semaphore and Mosaic wasn't allowed to perform a global barrier due to skip_device_barrier."` (`@ 0xa03b568`) | per-key SFLAG alloc failed AND `skip_device_barrier` blocked the global fallback |
| `0xe61` (3681) | `@ 0x1321ad48` | `"Requested barrier for unsupported core type"` (`@ 0x86b0f74`) | the func-op walk found no op of an expected `CoreType` |

If `BarrierConfig.type == 3` (per-key) the gate returns early — no global barrier; the per-key barrier is emitted by the regular `RunPasses` pipeline. Otherwise, when a global barrier is required and allowed, fall into the insertion path: `HasAnyCoreType @ 0x13e30700` selects a per-core-type barrier shape via `index = 2 - has_core`, then `mlir::detail::walk @ 0xea26de0` over func ops invokes the `$_0` callback `@ 0x1322a7e0` per matching func op:

- `TPUDialect::GetCoreTypeAttr @ 0x14aa6020` (must match, else `WalkInterrupt` → the "unsupported core type" `RET_CHECK`).
- `arith::ConstantIntOp(0)` / `ConstantIntOp(1) @ 0x1caca3a0` (count / increment operands) + a module-config-derived const (`GetModule()->config [+0x20]`).
- `sparse_core::MemorySpaceAttr::get(ctx, sflag) @ 0x1458ff20` + `MemRefType::get @ 0x1d897680` + `arith::MulIOp @ 0x1caf0c40` (per-core SFLAG stride) → `AllocateAtOffsetOp::create @ 0x145a5aa0` = **the global-barrier SFLAG memref** (`MemorySpace::sflag`).
- `UnrealizedConversionCastOp @ 0x1d8880e0` (bridge the sflag memref into the TC semaphore type) + `tpu::MemorySpaceAttr::get @ 0x14a9db60`.
- two `scf::ForOp @ 0x17866d60` (loop over peer cores) wrapping `tpu::SemaphoreSignalOp::create @ 0x14b442e0` (bump the global SFLAG on every peer) then `tpu::SemaphoreWaitOp::create @ 0x14b45460` (wait until all peers signalled).

The TC global barrier is therefore a per-core SFLAG, signalled to all peers via `tpu.sem_signal` and waited on via `tpu.sem_wait` inside `scf.for` loops over the core set — a **signal-all-then-wait tree barrier**. This is the TC analog of the SparseCore reserved global-barrier SFLAG: same SFLAG number space and the same `AllocateAtOffsetOp(MemorySpace::sflag)` primitive, with a different atomic op family (`tpu.sem_*` on TC vs `sc_tpu.sync_*` on SC). The reserved-block integers are on the [per-codename reserved SFLAG](per-codename-compiler-reserved.md) and [special-purpose sync flags](special-purpose-sync-flags.md) pages.

> **LOW — the literal global-barrier slot index.** The `AllocateAtOffsetOp` offset `Value` is built from `GetModule()->config`-derived constants plus the per-core `MulIOp` stride. The `AllocateAtOffsetOp(MemorySpace::sflag)` primitive and the per-core stride are CONFIRMED, but the offset `Value`'s exact source — a const vs a `Target::GetGlobalBarrierSyncFlagNumber` accessor call (`@ 0x1d60f420`) — was not isolated at this call site. The literal slot is INFERRED to be the reserved global slot.

### `RunPasses` — the per-key (type 2/3) lowering pipeline

`RunPasses(module, b1, b2, &BarrierConfig) @ 0x13202780` saves the `BarrierConfig` (`[rbp-0x220] = r8 @ 0x13202794`), builds an `mlir::PassManager @ 0x1cb700a0`, and runs the `$_0` closure `@ 0x132048e0`, which installs IR-printing (`enableIRPrinting @ 0x1cb66120`, gated by VLOG), a `PassErrorDiagnosticHandler`, and the pass pipeline: `mlir::tpu::createConvertIntegerMemrefsPass @ 0x132bca60` added via `OpPassManager::addPass @ 0x1cb6c000`, then a nested pass via `OpPassManager::nest @ 0x1cb6d3e0`. The decompile confirms the `PassManager` construction, the `$_0` invocation, and the `ConvertIntegerMemrefsPass` add.

The per-key SFLAG emission for the SC kernel goes through `CollectiveEmitterBase::EmitCustomBarrierFromConfig @ 0x13352cc0` / `EmitCustomBarrierStart @ 0x13352fc0` → `GetSyncFlagForBarrierId @ 0x133e9dc0` → `AllocateAtOffsetOp(sflag)` — the same path the SparseCore per-ring barrier uses. The TC per-key barrier (`BarrierConfig` type 2/3) and the SC per-ring barrier therefore draw from the **same reserved chip-SFLAG block**; the [SFLAG binding](barrier-to-sflag-binding.md) page documents how a colored `BarrierConfig.id` maps to a concrete SFLAG number.

> **LOW — the `RunPasses` nested-pass body.** `RunPasses` captures the `BarrierConfig` and the SC kernel emission reuses the `EmitCustomBarrierFromConfig` / `GetSyncFlagForBarrierId` path, but the exact nested pass (added via `OpPassManager::nest`) that reads `BarrierConfig` type 2/3 and selects the per-key SFLAG number (vs the global slot) was not separately disassembled. The `{lambda(Pass*,Operation*)#1} @ 0x1321cb40` on this path is the IR-print should-print predicate, not the barrier-lowering pass body.

> **INFERRED — `CustomCallConfig` field names.** The bool field names `[cc+0x90] = is_communicating` and `[cc+0x94] = skip_device_barrier` are attributed from the two `RET_CHECK` error strings gating exactly those byte reads; the offsets are CONFIRMED, the names are not confirmed against the proto descriptor field numbers. Likewise the `CustomCallConfig` hasbits `0x200`/`0x2000`/`0x40` and the `BarrierConfig` type at `[bc+0x20]` are proven as offsets/masks, not field-named against the proto.

---

## TC vs SC: Decision Differs, SFLAG Sink Is Shared

| Stage | TensorCore (this page + the [assignment producer](overview.md)) | SparseCore | Confidence |
|---|---|---|---|
| Dedup mechanism | GREEDY GRAPH COLORING (`BarrierColoring::Run`) over the live-async interference graph, first-fit color | `FlatHashMap<SparseCoreBarrierKey, long>` on the static ring key | CONFIRMED |
| Conflict predicate | live-range OVERLAP of two async collectives (`CollectConflictInfo`) | n/a (no schedule notion) | CONFIRMED |
| Coloring | `AssignColorsGreedy @ 0x109d0c80` — smallest available color | monotonic `GetNextUniqueSyncFlagId` | CONFIRMED |
| Async start/done | ACP: CP-start `0x24` / CP-done `0x23`; A2A: `kBarrierStart` custom-call / async-start `0x11` | sparsecore custom-call | CONFIRMED |
| Config produced | `BarrierConfig {type 1 global / 2 shared / 3 fresh}` → `BackendConfig` | `barrier_id` + hasbit | CONFIRMED |
| Read back at emit | `CustomKernelEmitter::Emit @ 0x1321ad60` → `backend_config<BackendConfig>` | `UniDirRingStrategy::BarrierId` | CONFIRMED |
| GLOBAL lowering | `MaybeInsertGlobalBarrier @ 0x1321ac20` → `AllocateAtOffsetOp(sflag)` + `scf.for(sem_signal/sem_wait)` tree barrier | `AssignGlobalBarrier` reserved slot → same SFLAG block | CONFIRMED |
| PER-KEY lowering | `RunPasses @ 0x13202780` → `EmitCustomBarrierFromConfig @ 0x13352cc0` → `GetSyncFlagForBarrierId`-style `AllocateAtOffsetOp(sflag)` | `EmitCustomBarrierStart @ 0x13352fc0` → `GetSyncFlagForBarrierId @ 0x133e9dc0` | CONFIRMED |
| Atomic op family | `tpu.sem_signal` / `tpu.sem_wait` (TC sequencer) | `sc_tpu.sync_add` / `sync_wait` (SC sequencer) | CONFIRMED |
| SFLAG sink | `AllocateAtOffsetOp(MemorySpace::sflag)` → chip SFLAG block — **shared by both arms** | same | CONFIRMED |

The decision differs (TC = live-conflict coloring; SC = static ring-hash); the hardware SFLAG sink is identical. The TC global barrier uses the TC tree barrier (`sem_signal`/`wait`); the per-key barriers reuse the SC `sync_add` emission path.

---

## Cross-References

- [Overview](overview.md) — the SFLAG-based barrier model and the `BarrierType` enum (1 global / 2 shared / 3 fresh).
- [Barrier → SFLAG Number Binding](barrier-to-sflag-binding.md) — how a colored `BarrierConfig {type, id}` maps to a concrete hardware SFLAG number.
- [InferBarrierConfig](infer-barrier-config.md) — the per-gen SFLAG-map source feeding the binding.
- [TensorCore Barrier](tensorcore-barrier.md) — the async-barrier op family (opcode `0x81` barrier-start) the A2A policy keys on.
- [Global-Barrier Window](global-barrier-window.md) — the type-1 global-barrier semantics this page lowers.
- [Per-Codename `compiler_reserved` SFLAG](per-codename-compiler-reserved.md) — the literal `{base, count}` integers of the reserved chip-SFLAG block both barrier arms allocate from.
- [SpecialPurposeSyncFlags](special-purpose-sync-flags.md) — the runtime SFLAG sink and overlay semantics.
- [Overview — the barrier-assignment producer](overview.md) — `TensorCoreBarrierAssignment::Run` runs these two `BarrierColoring` passes and turns the conflict set into a `BarrierConfig` via `DetermineBarrierConfigForKey @ 0x109c6fa0`.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`) — [back to index](../index.md).
