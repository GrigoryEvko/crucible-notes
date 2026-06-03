# CreateRoutingSchedule

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled symbol names are quoted verbatim. `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`CreateRoutingSchedule` is the compile-time **hop-assignment solver** inside the `net_router` collective emitter. Its input is a flat list of `net_router::Transfer` records — each a `{src_core, src_index, dst_core, dst_index}` quad naming a logical (source-core → destination-core) data movement that some collective (AllGather, AllToAll, CollectivePermute) needs to realize over the inter-chip interconnect (ICI). Its output is a `Schedule`: a per-step, per-destination, per-direction grid of DMA `Action`s that a later pass (`CreateRoutingScheduleLiteral`) serializes into the Type-5 route literal the runtime replays. This page documents the part of that transform that turns a generated path into a routing schedule: the hop-assignment loop, the per-hop source/destination endpoint and buffer handoff, and the `PointerType` enum that tags every hop's address kind.

The solver is best understood by its delta from a naive ring walk. It does **not** issue one hop per transfer sequentially. Instead it runs a priority-queue-driven greedy **dimension-order shortest-ring-walk** on the 2-D torus of chip coordinates: for each transfer it computes the candidate compass directions that reduce the torus distance to the destination, pushes the transfer onto a binary heap keyed by remaining ring distance, then repeatedly pops the highest-priority transfer, advances it one hop, and re-pushes it until it arrives. Every hop becomes an `Action` installed into the schedule at a `{destination-XY, step, direction}` cell. Crucially the walk is **software-pipelined** with a fixed latency factor of 3: a buffer a hop writes at step *S* is only readable at step *S+3*, so a multi-hop path is deliberately spread across steps rather than packed, and in-flight DMAs are tracked to serialize re-use of the same source block.

The per-hop endpoints are tagged with `net_router::PointerType`, a 2-bit enum with exactly three valid values: `kInput=0`, `kOutput=1`, `kAlloc=2`. Every multi-hop *relay* `Action` stages through `kAlloc` local scratch; only the collective's terminal endpoints carry `kInput`/`kOutput`. The address of each pointer is materialized at runtime by `RoutingCodeEmitter::GetDataPointerAddress`, which bounds-checks the type to `[0,3)` and routes `kAlloc` to a local scratch allocator while deferring `kInput`/`kOutput` to a caller-supplied address callback.

For reimplementation, the contract is:

- **The `Transfer` and `Schedule` record layouts** — the 16-byte transfer quad, the `IterationInfo` per-destination node, the `SchedulingQueueKey` heap element, and the `placement[transfer]` schedule record.
- **The hop-assignment loop** — candidate-direction generation (shortest way around each torus axis), the longest-remaining-distance-first binary heap, the per-direction ±1 torus step, and the three deferred callbacks that drive the pipeline.
- **The `PointerType` enum** — its three values, the `[0,3)` runtime bound, the `'ioa'` character tags, and how `GetDataPointerAddress` resolves each kind to an address.

| | |
|---|---|
| **Entry point** | `CreateRoutingSchedule(tpu::TpuTopology const&, absl::Span<Transfer const>)` @ `0x1381c6a0` |
| **TU (silent)** | `platforms/xla/service/jellyfish/lowering/net_router_emitter.cc` (path str @ `0x8760f44`) |
| **Heap push** | `std::__sift_up<_ClassicAlgPolicy, …$_0…, SchedulingQueueKey*>` @ `0x13825f60` (3 call sites) |
| **Pipeline factor** | `kPipelineFactor = 3` (validated in `LogAndValidatePaths` @ `0x13823dc0`) |
| **Direction enum** | `{N=0 (+Y), W=1 (-X), S=2 (-Y), E=3 (+X)}` — 4 compass ports |
| **PointerType enum** | `{kInput=0 'i', kOutput=1 'o', kAlloc=2 'a'}` — 2-bit, valid `[0,3)` |
| **Pointer address** | `RoutingCodeEmitter::GetDataPointerAddress` @ `0x13828220` |
| **Output consumer** | `CreateRoutingScheduleLiteral` @ `0x13822400` → Type-5 route literal |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile + symbols |

---

## The Transfer Input

### Purpose

A `Transfer` is the atomic unit the solver schedules: a single logical move of one buffer slot from a source core to a destination core. The per-collective transfer builders (AllGather, AllToAll, CollectivePermute) produce a `vector<Transfer>` from the collective's replica-group / `source_target_pairs` relationship; `CreateRoutingSchedule` consumes that span and is agnostic to which collective produced it. Transfer *generation* — how a replica group becomes a transfer set — is owned by the [net-router pipeline](net-router-pipeline.md) and the path-generation pages ([get-static-path](get-static-path.md), [randomized-toroidal-wildfirst](randomized-toroidal-wildfirst.md)); this page begins where the `Transfer` list is already built.

### Layout

```text
Transfer (16 bytes)
  +0x00  int  src_core     physical core id of the source
  +0x04  int  src_index    buffer / slot ordinal within the source
  +0x08  int  dst_core     physical core id of the destination
  +0x0c  int  dst_index    buffer / slot ordinal within the destination
```

The entry loop reads `Transfer+0x08` (`dst_core`) and resolves it to a chip coordinate:

```c
function CreateRoutingSchedule(topology, transfers):     // 0x1381c6a0
    RET_CHECK !transfers.empty()                          // str "!transfers.empty()" @0x1381c70a (line ~702)
    X = topology.ChipBounds.X                             // movslq topology+0x58
    Y = topology.ChipBounds.Y                             // movslq topology+0x5c
    x_wrap = topology+0xa0 & 1                             // per-axis torus-wrap flag (X)
    y_wrap = (topology+0xa2 << 16) & 0x100                // per-axis torus-wrap flag (Y)
    for each transfer in transfers:
        loc = topology.CoreForId(0, transfer.dst_core)    // 0x1381c7aa
        (dx, dy) = loc.chip_coordinates()                 // 0x1381c7b5 — destination XY
        // group destination XYs into a std::map<XY, IterationInfo> scoreboard …
```

> **NOTE —** the solver works in **chip coordinates**, not core ids. The per-collective builder writes physical core ids into `Transfer`; `CoreForId` + `chip_coordinates` is the bridge into the 2-D torus the ring-walk operates on. The decompile confirms the `CoreForId` / `chip_coordinates` pair at two sites (lines 742–744 and 957–958).

### Function Map

| Function | VMA | Role | Confidence |
|---|---|---|---|
| `CreateRoutingSchedule` | `0x1381c6a0` | Hop-assignment solver entry (~0x5c00 bytes) | CERTAIN |
| `TpuTopology::CoreForId` | — (called @ `0x1381c7aa`) | core id → `TpuCoreLocation` | CERTAIN |
| `TpuCoreLocation::chip_coordinates` | — (called @ `0x1381c7b5`) | location → torus XY | CERTAIN |

---

## The Per-Destination Scoreboard

### Purpose

Before walking, the solver groups all transfers by their destination XY into a `std::map<XY, IterationInfo>`. The map key is the destination coordinate; the value collects every transfer (iteration tag) headed for that coordinate. This scoreboard is the per-step argument threaded through the three deferred callbacks (below), and it is what the heap comparator indexes when computing remaining distance.

### Layout

```text
net_util::XY (8 bytes)                IterationInfo (0x40-byte node; new $0x40 @0x1381c952)
  +0x00  int  x                         +0x20  XY  key {x@0x20, y@0x24}
  +0x04  int  y                         +0x28  InlinedVector<int,1>  per-destination transfer/iteration tags
                                                  (SOO size field read/+=2 @0x1381c9a8/0x1381c9da)
```

The map is a red-black tree; the decompile shows `__tree_balance_after_insert` @ `0x1381c99c` keyed on the `{x@0, y@4}` pair. The `InlinedVector<int,1>` uses small-object optimization: a single tag lives inline, more spill to the heap.

---

## The Hop-Assignment Loop

This is the heart of the solver. It has three phases: candidate-direction generation, the priority-queue greedy schedule, and the per-direction torus step. All three are byte-confirmed in the `CreateRoutingSchedule` decompile.

### The Direction enum

`net_router::(anon)::Direction` is a 4-value compass enum. The values are byte-confirmed from a relocated `char*` direction-name table @ `0x21924ed8` (`.data.rel.ro`; four `R_X86_64_RELATIVE` slots) used by the iteration logger:

| dir | char | name | torus step (jump table @ `0xae5dec8`, dispatched @ `0x13820b34`) | Confidence |
|---|---|---|---|---|
| 0 | `'N'` | North (+Y) | `y' = (y + 1)       mod Y` | CERTAIN |
| 1 | `'W'` | West (-X) | `x' = (x + (X - 1)) mod X` | CERTAIN |
| 2 | `'S'` | South (-Y) | `y' = (y + (Y - 1)) mod Y` | CERTAIN |
| 3 | `'E'` | East (+X) | `x' = (x + 1)       mod X` | CERTAIN |

The negative step uses the `(dim - 1)` addend trick (`X-1` @ `-0x2a0(rbp)`, `Y-1` @ `-0x2a8(rbp)`) so that a wrap-around decrement stays non-negative before the modulo. The two divisors are `ChipBounds.X` (E/W, `idivl` @ `0x13820b6a`) and `ChipBounds.Y` (N/S, `idivl` @ `0x13820b51`).

> **NOTE —** this binds the four columns of the Type-5 route literal to a concrete compass: column *k* of the literal is the `{N,W,S,E}[k]` ICI output port. The column-to-port mapping is owned by [route-table-generation](route-table-generation.md); this page owns the enum that names the columns.

### Phase A — candidate directions (shortest way around)

For each transfer destination XY the solver picks, per axis, the *shorter* of the two ways around the torus ring:

```c
// per-axis candidate-direction choice (decompile @ 0x1381d850..0x1381d942)
forward_x = (dst_x - src_x) mod X         // idiv ChipBounds.X @0x1381d875
if src_x != dst_x:
    if forward_x <= X / 2:  candidate += E (dir 3)   // cmp X/2 @0x1381d87b
    else:                   candidate += W (dir 1)
forward_y = (dst_y - src_y) mod Y
if src_y != dst_y:
    if forward_y <= Y / 2:  candidate += N (dir 0)
    else:                   candidate += S (dir 2)
RET_CHECK !directions.back().empty()       // str "!directions.back().empty()" @line 2927
RET_CHECK directions.back().size() <= 4    // str "directions.back().size() <= 4"  @line 2958
```

The half-perimeters `X/2` (`-0x2c0(rbp)`) and `Y/2` (`-0x2b8(rbp)`) are set up once @ `0x1381d12a`/`0x1381d143`. When `src == dst` on an axis, that axis contributes no direction. The result — an `InlinedVector<Direction,4>` of up to two directions — is the minimal-hop step set toward the destination. This is the `net_router` analog of the wild-first shortest path ([randomized-toroidal-wildfirst](randomized-toroidal-wildfirst.md)), but operating on the core/chip grid and dimension-ordered rather than randomized.

> **GOTCHA —** the comparison is `forward <= dim/2`, not `forward < dim/2`. On an even-diameter ring the tie (`forward == dim/2`) deterministically prefers the *positive* direction (E / N). A reimplementation that flips the tie-break will produce a different — but equally optimal-length — schedule, and will mismatch the Type-5 literal byte-for-byte.

### Phase B — longest-remaining-distance-first priority queue

Each `(destination, candidate-directions)` becomes a `SchedulingQueueKey` (0x28 bytes = a `{step:int, …, InlinedVector<Direction,4>}` payload) pushed onto a `std::vector`-backed binary heap via `std::__sift_up<_ClassicAlgPolicy, …$_0…>` @ `0x13825f60`. The decompile confirms three push sites (lines 1183, 2528, 5007). The comparator `$_0` orders keys by **remaining ring distance**:

```c
// SchedulingQueueKey comparator $_0  (inlined at __sift_up 0x13825f60)
remaining = 0
for each dir still in key.directions:
    (dx, dy) = dst_xy[dir.id] - current_xy          // per-dir target read @0x13826009
    if x_wrap: rx = min(|dx|, X - |dx|) else rx = |dx|   // ChipBounds.X @0x13826037, wrap bit @0x1382602b
    if y_wrap: ry = min(|dy|, Y - |dy|) else ry = |dy|   // ChipBounds.Y @0x1382606d, wrap bit 0xa2<<16 @0x13826065
    remaining = max(remaining, rx + ry)             // cmovg accumulate @0x13825ffc
// higher remaining → higher priority (longest-path-first / critical-path greedy)
```

The main loop pops the highest-priority transfer, advances it one hop in its chosen direction (Phase C), installs the resulting `Action`, and re-pushes the transfer with its new coordinate and shrunken candidate set — until it arrives. Scheduling the longest-remaining path first keeps the critical path from being starved by short transfers competing for the same ports.

> **QUIRK —** the ordering *metric* (remaining ring distance) and the `__sift_up` direction are byte-confirmed, but the heap-pop polarity (strictly longest-first vs shortest-first) depends on the `pop_heap`/`sort_heap` convention the main loop uses, which was not re-derived from the disassembly. The comparator's `cmovg`/`max` accumulation reads as a *max-by-distance* heap, hence "longest-first"; treat the polarity as **HIGH confidence**, the metric as **CERTAIN**.

### Phase C — the per-direction torus step

Advancing one hop is a jump table @ `0xae5dec8` (4 int32 self-relative offsets) indexed by the `Direction` value, dispatched @ `0x13820b34`. Each case advances the current chip coordinate by ±1 on one axis with modular wraparound, exactly as the Direction table above shows. The chosen direction is also the **column** the hop's `Action` is written into (one of the four per-destination `Action` slots), so the direction simultaneously names the next coordinate and the literal column.

---

## The Per-Hop Buffer Handoff

The schedule is driven as a discrete-event simulator. Three deferred `std::function<Status(map<XY, IterationInfo>&)>` closures implement the per-hop source/destination endpoint assignment and the buffer handoff between hops. The map argument is the per-step destination-XY scoreboard; `$_1`/`$_2` ignore it (they act on captured state) — it exists only because the deferred-callback vector is homogeneously typed.

### `$_4` — defer a callback to a future step

```c
function defer_at_step(extra_actions, index, cb):      // 0x13825b60
    // extra_actions = vector<optional<vector<function<…>>>>, element stride 0x20,
    //                 optional has_value byte @+0x18
    if index < extra_actions.size:
        RET_CHECK extra_actions[index].has_value()      // str "extra_actions[index].has_value()" @0xa171d66 (line 1681)
        extra_actions[index].value.emplace_back(cb)      // 32-byte std::function payload move @0x13825bba
    else:
        grow extra_actions to index+1, engaging empty optional<vector<function>> slots
```

`$_4` is the deferral primitive: "run callback `cb` when the sim reaches step `index`." It is the **only** mechanism that spreads a multi-hop path across steps. The two call sites (`0x13820ae8` for `$_1`, `0x13820fd1` for `$_2`) are its only callers.

### `$_1` — buffer-release / in-flight tracking (the handoff)

When a hop's DMA *lands* in a destination scratch buffer, `$_1` marks that buffer available for the next hop to read and records the in-flight DMA so a re-use conflict can be detected:

```c
function buffer_release(capture):                       // 0x13826dc0
    // capture (0x28 POD): {Allocator-set-ptr@0, XY-key@8, deque-ctx@0x18, int available_at@0x20}
    entry = FlatHashMap<XY, Allocator>.find_or_prepare_insert(set, &capture.XY)  // 0x13826de1
    available_at = capture.available_at + 1              // inc @0x13826e14
    RET_CHECK available.empty() || available.back().second <= available_at       // sorted by release step; str @0x8509fa3 (line 389)
    RET_CHECK ptr.type == PointerType::kAlloc            // str "type != net_router::PointerType::kAlloc" @0x873065f (line 390)
    RET_CHECK ptr.index.has_value()                      // str @0xa16fa09 (line 391)
    RET_CHECK *ptr.index < size                          // str @0x8672033 (line 396)
    RET_CHECK c_none_of(available, e -> e.first == *ptr.index)  // NO double-release; str @0xa0f3a0c (line 395)
    available.push_back((*ptr.index, available_at))
    latest_dma_out.push_back(*ptr.index | (available_at << 32))  // deque<pair<int,int>>; __add_back_capacity @0x13826f4d
```

Two invariants are the buffer handoff:
- **Availability** — a buffer index is added to a per-destination-XY ordered `available` list keyed by release step. A hop reads a buffer only after it appears in this list, which (combined with the pipeline factor) is why the next hop runs `kPipelineFactor` steps later.
- **In-flight serialization** — the `(index, step)` pair is pushed onto the `latest_dma_out` deque. The conflict invariant `!latest_dma_out.contains({src, block})` (checked in `LogAndValidatePaths`) forbids launching a second DMA from the same source block while one is still in flight on that path.

> **QUIRK —** the relay buffers tracked here are **always `kAlloc`** — the `RET_CHECK ptr.type == PointerType::kAlloc` (string `"type != net_router::PointerType::kAlloc"`, confirmed at decompile line 23 of `$_1`) rejects any other kind. The collective's real input/output buffers (`kInput`/`kOutput`) never enter the in-flight tracker; only intermediate scratch hops do.

### `$_2` — commit the per-hop endpoint placement

When a hop's endpoints are fixed, `$_2` writes the 16-byte `Action` (the `{src_ptr, dst_ptr}` quad) into the schedule's `placement` array for each transfer that arrives at this step:

```c
function commit_placement(capture):                     // 0x13827760
    // capture (0x30): {placement-ctx@0, InlinedVector<int,1> transfer_ids@8, Action16 payload@0x20}
    for t in capture.transfer_ids:
        RET_CHECK t < placement.size()                   // cmp @0x138277a7, ud2 @0x138277e0
        RET_CHECK !placement[t].has_value()              // place exactly once; str "placement[transfer].has_value()" @0xa171d88 (line 1753)
        placement[t][0..0x10) = capture.Action16         // vmovups @0x138277c0
        placement[t].has_value = 1                       // movb $1,0x10 @0x138277c9
```

This is the per-hop source/destination endpoint write: the 16-byte payload is the `Action` endpoint quad (the two `Pointer`s), and the captured int-list is the set of transfer ids committed at this step. The committed `Action` is what later lands in the per-`{core, step, direction}` slot the Type-5 literal serializes.

### The schedule record

```text
placement[transfer] record (0x14 bytes = 5×4; laid 8-per-0xa0 block, memset @0x1381cac0)
  +0x00  int   src endpoint field
  +0x04  int   dst endpoint field
  +0x08  byte  (flag)
  +0x0c  int   step
  +0x10  byte  has_value     // set once by $_2; RET_CHECK !has_value before write
```

```text
Schedule (returned)
  +0x00  vector<Step>                     per-step Action grid
  +0x08  int  step_count
  +0x10  int  step_cap
  +0x18  deque<pair<int,int>>             latest_dma_out (in-flight DMAs)
  +end   FlatHashMap<XY, Allocator>       per-destination buffer occupancy
```

### The pipeline factor

`LogAndValidatePaths` @ `0x13823dc0` enforces the latency window: `last_location.back().second <= step - kPipelineFactor`, with the immediate inlined as `add $0xfffffffffffffffd` (= `step - 3`) @ `0x13824518`. Hence **`kPipelineFactor = 3`**: a buffer a hop writes at step *S* is only legal to read at step ≥ *S+3*, modeling a 3-stage DMA latency. This is *why* a multi-hop path is spread across steps rather than packed into consecutive ones, and why `$_1`'s `available_at` is offset from the write step. The validator also checks monotone steps (str `@0x85c67d1`) and source-chaining (`last_location.back().first == src_ptr`, str `@0x8596174`).

---

## The PointerType Enum

### Purpose

Every endpoint of every schedule `Action` is a `net_router::Pointer` whose `type` field is a `net_router::PointerType`. The type tells the runtime *which address space* the buffer lives in: a collective input, a collective output, or local scratch. The enum is a 2-bit field with exactly three valid values; value 3 is rejected.

### Values

| value | char tag | name | resolution in `GetDataPointerAddress` | Confidence |
|---|---|---|---|---|
| 0 | `'i'` (`0x69`) | `kInput` | caller callback `function<LloMemoryAddress(PointerType, PointerType, LloValue*)>(0, …)` | HIGH |
| 1 | `'o'` (`0x6f`) | `kOutput` | caller callback `(…)(1, …)` | HIGH |
| 2 | `'a'` (`0x61`) | `kAlloc` | `GetLocalDataAllocAddress` @ `0x13828460` (local scratch) | CERTAIN |
| 3 | — (`'?'`/`0x00`) | (undefined) | rejected by `ScheckLt(type, 3)` bound | CERTAIN |

The character tags are byte-confirmed from `Pointer::ToString` @ `0x13826c60`, which packs the tag from the 3-byte literal `0x616F69` ("ioa") and selects `byte = (0x616F69 >> (8 * type)) & 0xff` (decompile line 24: `LOWORD(v14) = (unsigned __int8)(0x616F69u >> (8 * v4))`, with `v4 = *type`). The name `kAlloc` is additionally confirmed verbatim by the assertion string `"type != net_router::PointerType::kAlloc"` (in `$_1`). The names `kInput`/`kOutput` for values 0/1 are the structural reading — `GetDataPointerAddress` routes value 0 and value 1 to the external input/output address callback and value 2 to local scratch — but were not extracted from a spelled-out enumerator string, hence HIGH rather than CERTAIN.

> **NOTE —** the `Pointer` carries an optional `index` alongside the type (`ToString` prints `type-char` then either the index via `to_string`, or `'?'` (`63`) when the index is absent — decompile lines 25–35). The index is the buffer/slot ordinal within the address space the type names.

### Resolving an address

`RoutingCodeEmitter::GetDataPointerAddress` @ `0x13828220` turns a `(PointerType type, index)` into an `LloMemoryAddress`:

```c
function GetDataPointerAddress(builder, ptr_type, ptr_index, value, callback):  // 0x13828220
    ScheckGe(ptr_type, SimmU32(0))             // "Pointer type must be >= 0"; str @0x9fc5d68
    ScheckLt(ptr_type, SimmU32(3))             // "Pointer type must be < 3";  str @0x9c15936
    alloc_addr  = GetLocalDataAllocAddress(...)            // 0x13828460 — the kAlloc address

    is_input  = Predicated(SeqS32(ptr_type, SimmU32(0)))  // type == 0 ?
    in_addr   = callback(0, …)                            // (*callback)(kInput,  …)  @ (a6+16)
    sel0      = Phi(alloc_addr, in_addr)                  // pick alloc vs input

    is_output = Predicated(SeqS32(ptr_type, SimmU32(1)))  // type == 1 ?
    out_addr  = callback(1, …)                            // (*callback)(kOutput, …)
    return Phi(sel0, out_addr)                            // pick (alloc|input) vs output
```

The address is built as an SSA `Predicated`/`Phi` chain (the `LloRegionBuilder` IR), not a runtime branch: it materializes all three candidate addresses and selects among them by predicate, so the same emitted code is correct for any of the three types. The decompile confirms the dual `SeqS32(type, 0)` / `SeqS32(type, 1)` predicates, the two callback indirect calls through `(*(callback+16))`, and the `Phi` selection.

> **GOTCHA —** the bound is `ScheckLt(type, 3)`, i.e. the valid range is `[0, 3)` = `{0, 1, 2}` only. A reimplementation that allows a 2-bit field's full `[0, 4)` range will admit an undefined value-3 pointer that `GetDataPointerAddress` would reject at runtime — and `ToString` would print as the bare `'?'` index marker with no type char.

> **NOTE —** the route-schedule **relay** `Action`s all carry `kAlloc` on both `src_ptr` and `dst_ptr` (the per-step `$_1` `RET_CHECK`). `kInput`/`kOutput` appear only on the collective's *terminal* `Transfer` endpoints, resolved at runtime by the caller's address callback. Where those terminal endpoints get tagged `kInput` vs `kOutput` is in the per-collective emitter (AllGather / AllToAll / CollectivePermute), not in `CreateRoutingSchedule`; that assignment was not traced on this page.

---

## The Solver Pipeline At A Glance

| stage | function / site (VMA) | output |
|---|---|---|
| gather destination XYs | `CreateRoutingSchedule` loop @ `0x1381c770` | `std::map<XY, IterationInfo>` scoreboard |
| candidate directions (shortest ring) | `@0x1381d850..0x1381d942` | `InlinedVector<Direction,4>` per dest |
| build / push priority queue | `__sift_up<…$_0…>` @ `0x13825f60` (×3) | heap of `SchedulingQueueKey` (0x28) |
| ↳ distance comparator | `$_0` @ `0x13825f60` (`min(|d|, dim-|d|)`/axis) | remaining-ring-distance key |
| per-direction torus hop | jump table @ `0xae5dec8` / `@0x13820b34` | next chip coordinate (mod ChipBounds) |
| defer callback to step *k* | `$_4` @ `0x13825b60` | `extra_actions[step] += function` |
| buffer release / in-flight record | `$_1` @ `0x13826dc0` | `available` list + `latest_dma_out` deque |
| commit `Action` placement (per direction) | `$_2` @ `0x13827760` | `placement[transfer]` `Action` slot |
| validate (pipeline / source / step order) | `LogAndValidatePaths` @ `0x13823dc0` | `Schedule` (`kPipelineFactor=3`) |
| resolve a pointer address (runtime) | `GetDataPointerAddress` @ `0x13828220` | `LloMemoryAddress` (scratch or callback) |

---

## Cross-References

- [Routing Overview](overview.md) — the route-generation → cache → emission pipeline this solver sits inside
- [Net-Router Pipeline](net-router-pipeline.md) — the per-collective `Transfer`-set builders that feed `CreateRoutingSchedule`
- [GetStaticPath & Multipod](get-static-path.md) — inter-pod path generation; the upstream path source
- [Randomized Toroidal WildFirst](randomized-toroidal-wildfirst.md) — the random-walk path analog; this solver is the dimension-order torus counterpart
- [Route Table Generation](route-table-generation.md) — the Type-5 route literal `CreateRoutingSchedule` produces, and the column → ICI-port mapping
- [Intra-Chip Descriptor](../dma/intra-chip-descriptor.md) — the per-step DMA descriptor whose routing field the schedule's `Action` endpoints supply
