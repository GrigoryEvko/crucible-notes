# MRB Chain Allocator

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, **not** stripped — every symbol below is a demangled C++ name). Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset; `.data` VMA − 0x400000 == file offset. Other versions will differ.*

## Abstract

`MrbChainAllocator` is the Stage-2 pass that decides **which matrix-result buffer (MRB) FIFO entry each matmul accumulation chain occupies, and for how long**. A TPU matmul does not write its result to a register; it pushes operands into the systolic array and the partial sum lands in one of a small pool of result-FIFO buffers (the MRB) many cycles later. When several matmuls accumulate into one running sum — a K-tiled reduction — they form an *accumulation chain* that must hold a single MRB entry from the chain's first matmul until its result is consumed. The allocator's job is to place every chain on the finite MRB pool over a program-order timeline, freeing each entry the instant its result retires so a later chain can reuse it.

The natural reference frame is **linear-scan register allocation over a single physical register class**, with one twist: the "registers" are a FIFO with a fixed retire latency, so eviction is keyed on *time*, not on liveness intervals. The allocator walks the chains in matmul issue order; at each step it advances a monotone clock (`AdvanceTimeTo`), which evicts every chain whose result became available before "now" and recycles that chain's MRB entry into a per-chunk free pool (`ReleaseMrbReservation`); it then reserves an entry for the current chain — either a fresh `insert` or, if the chain already holds an entry from an earlier matmul, a `replace_data` that extends the entry's unevictable-until time. A chain that would have to hold its entry past the available window is cut at the current time (`SplitAccumulationChain`) and its tail is deferred to be re-reserved later. This is the **MRB analog of the [MXU assignment bin-packer](mxu-assignment-binpacker.md)** — but where the MXU packer runs a greedy min-makespan search over N interchangeable lanes, the MRB allocator is a *deterministic program-order sweep* with a time-keyed eviction pool, because the result FIFO is an ordered fixed-retire-latency resource rather than N parallel matmul lanes.

This page documents the timeline algorithm and the chain-assignment logic: the central `chains_unevictable_until_` boost bimap (ProgramOrder-ordered on one side, chain-pointer-hashed on the other), the `AssignMrbEntriesToChains` driver loop, the `AdvanceTimeTo` eviction engine, the `ReleaseMrbReservation` per-chunk recycle pool, and the `SplitAccumulationChain` defer-tail mechanism. The per-step *cost* that advances the reservation end-times — `ExtendMrbReservation`'s two parallel `push_time + min(throughput, cap)` and `push_time + min(latency, cap)` advances — is the cost cell consumed by this timeline; the throughput and edge terms are priced by the [`CycleTable`](../cost/jf-cycletable.md) (`CycleTableInstruction`) and [`LatencyBetween`](../cost/bundle-aware-cost.md#the-dependency-latency-axis--latencybetween), each clamped from above by an option cap (`+0x20`/`+0x28`) and written into *separate* per-MXU timeline arrays, not summed. The second half of this page closes the public edge-latency surface: the **optional `LatencyBetween` jitter** (a flag-gated uniform-random perturbation, off by default) and the **trace-instruction edge clamps**.

For reimplementation, the contract is:

- The `chains_unevictable_until_` bimap relation type, and the two reservation arms (`insert` for a new chain, `replace_data` to extend an existing one).
- The driver loop discipline: introsort the accumulations by matmul program order, then per accumulation `Split → AdvanceTimeTo → FIFO-push rounding → Reserve → ExtendMrbReservation`.
- The eviction rule: advancing the clock frees every chain whose `matres_program_order` is strictly less than the new time, recycling each freed `MrbEntry` into the per-chunk free pool.
- The split rule: a chain cannot be split in the past; the cut keeps the head's already-consumed accumulations and defers the tail in a `btree_map` keyed by program order.
- The `LatencyBetween` wrapper: base edge → optional jitter (only if a `BitGen*` is installed) → trace-edge clamps.

| | |
|---|---|
| **Driver** | `(anon)::AssignMrbEntriesToChains` `@0x10f4ac60` (`Target&, CycleTable&, LatencyTable&, Span<const AccumulationChain>, int, MrbAccumulationOptions`) |
| **Per-step cost cell** | `MrbChainAllocator::ExtendMrbReservation` `@0x10f58800` (two parallel `push + min(throughput, cap)` / `push + min(latency, cap)` timeline advances; priced by `CycleTable`/`LatencyBetween`) |
| **Eviction engine** | `MrbChainAllocator::AdvanceTimeTo(StrongInt<ProgramOrder>)` `@0x10f5e9e0` |
| **Recycle pool** | `MrbChainAllocator::ReleaseMrbReservation(MrbEntry)` `@0x10f5f9e0` |
| **Split / defer-tail** | `MrbChainAllocator::SplitAccumulationChain` `@0x10f598e0` |
| **Central structure** | `chains_unevictable_until_` — boost `bimap<set_of<StrongInt<ProgramOrder>>, unordered_set_of<AccumulationChainAfterSplit*>, with_info<AccumulationWithOriginalChain>>` |
| **Jitter wrapper** | `LatencyTable::LatencyBetween(LloValue,LloValue)` `@0x1c89f820`; base ctor `@0x1c89f800` |
| **Source file** | `platforms/xla/service/jellyfish/mxu_accumulation.cc` (RetChecks at lines 1017, 1332, 1371) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Central Data Structure

### Purpose

The allocator must answer two questions in O(1)/O(log n) at every loop step: *(a)* which chain is the next to retire (so the clock can evict it), and *(b)* does this chain pointer already hold a reservation (so the reserve becomes an extend rather than a new entry)? A single Boost bimap answers both: one side is ordered by retire time, the other is hashed by chain pointer.

### `chains_unevictable_until_`

The relation type is byte-exact, demangled from the `final_erase_` instantiation `@0x10f61d60` and re-confirmed inline in `SplitAccumulationChain` `@0x10f598e0`:

```c
// boost::bimaps::relation::mutant_relation<...> — chains_unevictable_until_
bimap<
    set_of<            StrongInt<ProgramOrder>,         std::less<...> >,   // LEFT  — ORDERED by retire time
    unordered_set_of<  AccumulationChainAfterSplit*,    boost::hash<...> >, // RIGHT — HASHED by chain pointer
    with_info<         AccumulationWithOriginalChain >                      // INFO  — next_accumulation
>;
```

- **LEFT key** = the chain's `matres_program_order`: the program-order time at which the chain's matrix result becomes available, i.e. the time *until which* its MRB entry is un-evictable. The LEFT index is an ordered (`set_of`) tree, so its front node is always the next chain to retire.
- **RIGHT key** = the `AccumulationChainAfterSplit*` chain pointer, in an `unordered_set_of` hashed by `boost::hash`. A reserve looks the chain up here to decide insert-vs-extend.
- **INFO** = an `AccumulationWithOriginalChain` (`next_accumulation`), the accumulation that follows the one currently reserving — carried alongside the relation rather than as a key.

> **NOTE —** the bimap is the *eviction* index; it is distinct from the per-MXU `chains_next_accumulating_at_[mxu_id]` array that `AdvanceTimeTo` post-checks (RetCheck string `"chains_next_accumulating_at_[mxu_id].left.begin()->first >= to"`, mxu_accumulation.cc:1371) and from the per-MXU `mrb_entries_[mxu_id][curr_level]` size-class block map (insert string `"mrb_entries_[mxu_id][curr_level].insert(MrbBlock{.offset = 2 * mrb_block.offset + 1}).second"`). Three structures coexist in one allocator: the time-ordered eviction bimap, the per-MXU next-accumulation index, and the per-MXU level-keyed block map. Only the first drives eviction; this page documents it.

### Allocator Object Layout

Reconstructed from the field accesses across the four traced methods. Field roles are CONFIRMED; struct-name bindings for the option caps and the chunk arrays are INFERRED (no struct-layout string).

| Offset | Field | Meaning | Confidence |
|---|---|---|---|
| `+0x00` | `Target*` | the JF `Target` (vtable gates `[+0x390]` MRB-support, `[+0x5e0]` FIFO granule) | CONFIRMED |
| `+0x08` | `CycleTable*` | `GetCyclesForThroughput` source — the throughput axis of `ExtendMrbReservation` | CONFIRMED |
| `+0x10` | `LatencyTable*` | `LatencyBetween` source — the edge axis | CONFIRMED |
| `+0x20` | `s64` throughput cap | `min(throughput, cap)` cell — copied from `MrbAccumulationOptions` | CONFIRMED (offset + the `min` direction); name INFERRED |
| `+0x28` | `s64` latency cap | `min(latency, cap)` cell — copied from `MrbAccumulationOptions` | CONFIRMED (offset + the `min` direction); name INFERRED |
| `+0x70` | `s64 latest_matmul_` | monotone clock; `AdvanceTimeTo`/`SplitAccumulationChain` RetCheck guard | CONFIRMED |
| `+0xd8`..`+0xe8` | `chains_unevictable_until_` | the eviction bimap (LEFT-ordered head at `+0xd8`, RIGHT-hashed at `+0xe0`) | CONFIRMED |
| `+0x7a0` | per-chunk free pool | `flat_hash_set<MrbEntry>` of recycled entries, bucketed by chunk_id (`ReleaseMrbReservation`) | CONFIRMED |
| — | `btree_map<long, unique_ptr<AccumulationChainAfterSplit>>` | the deferred split-off chain tails, keyed by program order | CONFIRMED |

The `+0x70` clock carries the canonical name `latest_matmul_`: both `AdvanceTimeTo` and `SplitAccumulationChain` read `*(this+0x70)` and name it `latest_matmul` in their RetCheck format strings (`@0x10f5ea08`, `@0x10f59920`).

---

## `AssignMrbEntriesToChains` — the Driver

### Purpose

`AssignMrbEntriesToChains` `@0x10f4ac60` is the entry point. It receives the matmul accumulation chains for a sequence group as `absl::Span<const AccumulationChain>` (demangled signature, line 1), copies them into a mutable working form, constructs an `MrbChainAllocator` on the stack, and drives the per-accumulation loop. The whole function is the allocator's `main`.

### Entry Point

```text
AssignMrbEntriesToChains  @0x10f4ac60   ── (Target&, CycleTable&, LatencyTable&, Span<AccumulationChain>, int, MrbAccumulationOptions)
  ├─ copy Span<AccumulationChain> → working vector<AccumulationChainAfterSplit>
  ├─ introsort accumulations by matmul program order        @0x10f4b092  (gtl::OrderBy<Accumulation::program_order, Less>)
  ├─ construct MrbChainAllocator (stack)                    @0x10f4b113
  │     ├─ copy MrbAccumulationOptions+0x4..+0x14 → allocator+0x1c..+0x2c   (16-byte vmovups)
  │     └─ __size_returning_new per-MXU chunk arrays         @0x10f4b2cf  (count clamped ≥ 8)
  └─ per accumulation (in program order):
        SplitAccumulationChain    @0x10f4c984
        AdvanceTimeTo(now)        @0x10f4c9c3
        FIFO-push rounding        @0x10f4ca06
        Reserve (insert | replace) → ExtendMrbReservation  @0x10f4d9a8 / @0x10f4dfca
```

### Algorithm

```c
function AssignMrbEntriesToChains(target, cycles, latency, chains, num_mxus, opts):  // @0x10f4ac60
    // 1. Materialize a mutable working list of accumulations.
    work = copy(chains)                          // 0x650-byte AccumulationChain → 0x70-byte AccumulationChainAfterSplit

    // 2. Process chains in matmul *issue* order, so the clock only ever moves forward.
    introsort(work.accumulations,                // @0x10f4b092
              OrderBy(Accumulation::matmul_program_order, Less))   // ascending

    // 3. Build the allocator; copy the two ExtendMrbReservation caps out of the options.
    alloc = MrbChainAllocator(target, cycles, latency, opts, num_mxus)   // @0x10f4b113
    //   alloc.throughput_cap (+0x20) and alloc.latency_cap (+0x28) copied from MrbAccumulationOptions
    //   alloc.chunks[max(num_mxus, 8)] = __size_returning_new(...)      // @0x10f4b2cf

    // 4. Main loop — one iteration per accumulation, already in program order.
    for accum in work.accumulations:
        chain = accum.chain
        now   = accum.matmul_program_order

        // 4a. If a held chain cannot keep its entry to its result time, cut it now and defer the tail.
        if chain_must_yield_entry(chain, accum):
            SplitAccumulationChain(chain, accum)      // @0x10f4c984

        // 4b. Advance the monotone clock; this evicts + recycles every retired chain.
        AdvanceTimeTo(now)                            // @0x10f4c9c3

        // 4c. Size the result-FIFO push and pick the MXU instance.
        push = LloInstructionPushesToResultFifo(accum.matmul)            // @0x1d4f3600
        if target.vtable[+0x390]() > 0:               // MRB-support gate  (line 1871)
            granule = target.vtable[+0x5e0]()         // FIFO-push granule (byte) (line 1874)
            push    = ceil(push / granule) * granule  // round up to a granule multiple (idiv)
        size_class = bsr(push)                        // log2 size class   (_BitScanReverse, line 1887)
        mxu        = accum.matmul.unit_id() & 3        // MXU-instance index from matmul unit_id (line 1864/1716)

        // 4d. Reserve — new entry or extend existing — then price the step.
        it = alloc.chains_unevictable_until_.right.find(&chain)
        if it == end:                                  // first matmul of this chain
            alloc.chains_unevictable_until_.right.insert(
                { &chain, accum.matres_program_order, accum.next_accumulation })
            ExtendMrbReservation(chain, accum.next_accumulation)         // @0x10f4d9a8
        else:                                          // chain absorbs another matmul
            alloc.chains_unevictable_until_.right.replace_data(
                it, accum.matres_program_order)
            ExtendMrbReservation(chain, accum.next_accumulation)         // @0x10f4dfca
```

### The Two Reserve Arms

The reserve is the inlined `ReserveMrbEntry` (a two-arm `absl::Overload` visitor, lambdas `@0x222f5f10`/`@0x222f5f40`). The arm is selected by whether the chain pointer is already in `chains_unevictable_until_.right`:

| Arm | Condition | Bimap op | `ExtendMrbReservation` site | Source string |
|---|---|---|---|---|
| **new reservation** | chain absent (first matmul) | `right.insert({&chain, matres_program_order, next_accum})` | `@0x10f4d9a8` | guarded by `RetCheck("!chain->mrb_entry.has_value()")` cc:1542 — the chain must not already hold an entry |
| **extend reservation** | chain present (absorbs a matmul) | `right.replace_data(it, matres_program_order)` | `@0x10f4dfca` | `"chains_unevictable_until_.right.replace_data(it, curr_accumulation.accumulation.matres_program_order)"` `@0xa104fb1` |

> **QUIRK —** the two arms differ only in *how* they touch the bimap, not in what they price. Both call `ExtendMrbReservation` to advance the chain's reservation end-time by the per-step cost. The `replace_data` arm overwrites the LEFT key (`matres_program_order`) so the eviction order tracks the *latest* matmul of a multi-matmul chain. A reimplementation that re-`insert`s instead of `replace_data` on the extend path would leave a stale duplicate in the ordered index and free the entry too early.

### The FIFO-Push Rounding

Confirmed byte-exact in the driver (lines 1869–1887). The number of result-FIFO slots a matmul pushes is `LloInstructionPushesToResultFifo(matmul)` `@0x1d4f3600`; when the target supports MRB (gate `vtable[+0x390]() > 0`, line 1871), the push is rounded up to a multiple of a per-gen granule (`vtable[+0x5e0]()`, a byte, line 1874) via an integer divide; `bsr(push)` then yields a log2 size class (with a `RetCheck(absl::has_single_bit(entries_needed))` at cc:875 guarding power-of-two, and `target_level < kLevelsInMrbAllocator`). The MXU instance is `accumulation.matmul->unit_id() & 3` — low 2 bits of the matmul's unit id, **not** its program order (line 1864; the unit id is read at line 1716 and `RetCheck`-guarded by `"accumulation.matmul->unit_id().has_value()"` at cc:1518).

> **NOTE —** the `Target` vtable layer here is **not** the `JellyfishTarget` base `@0x21cc6bc0`; the named accessors for `[+0x390]` (MRB-support count) and `[+0x5e0]` (FIFO-push granule) were not resolved. The slot offsets, the `>0` gate, and the round-up arithmetic are byte-exact; the accessor *names* are INFERRED.

---

## `AdvanceTimeTo` — the Eviction Engine

### Purpose

`AdvanceTimeTo(StrongInt<ProgramOrder> new_time)` `@0x10f5e9e0` is what makes the allocator a *timeline*. Advancing the clock to the current matmul's program order frees every MRB entry whose result has already retired — that is, every chain whose `matres_program_order` is strictly less than `new_time` — recycling each freed entry into the per-chunk pool and erasing it from the eviction bimap.

### Algorithm

```c
function AdvanceTimeTo(new_time):                        // @0x10f5e9e0
    // 1. The clock is monotone: you may never advance backward.
    if this.latest_matmul_ /*+0x70*/ > new_time:         // line 128
        FATAL "MrbEntries must be reserved in program order "
              "(latest_matmul=%v, current_matmul=%v)"    // mxu_accumulation.cc:1332  @0xa0f5d91

    // 2. Evict every chain whose result became available before now.
    while front = chains_unevictable_until_.left.begin():    // ordered-by-ProgramOrder head  @+0xd8
        if front.matres_program_order >= new_time:           // line 175 (cmp [node-0x88], to)
            break                                            // nothing left retires before now
        chain = front.chain
        if front.complete /* relation-node byte *(node-8) != 1 → chain is done */:
            ReleaseMrbReservation(chain.mrb_entry)            // recycle  (lines 178-205)
            // chain+0x2c is RetChecked nonzero here (BUG() if 0) before reading mrb_entry @chain+0x20
        else: // *(node-8) == 1: chain is now "evictable, next Accumulation is ..." (deferred, not released)
        chains_unevictable_until_.final_erase_(front)         // remove from bimap  @0x10f61d60

    // 3. Post-condition: each per-MXU next-accumulation index head is now >= new_time.
    for mxu in 0 .. num_mxus:                                 // lines 706-739
        RetCheck(chains_next_accumulating_at_[mxu].left.begin()->first >= new_time)  // cc:1371

    this.latest_matmul_ = new_time                            // commit the clock  (line 754)
```

> **GOTCHA —** the guard fires when `latest_matmul_ > new_time` (line 128), i.e. **time must be non-decreasing** (FATAL on `current > new`, success on `current <= new`). Because the driver feeds accumulations in introsorted program order, `new_time` never regresses and the guard never trips in normal operation; it is a structural invariant check, not a runtime branch. The eviction comparison in the loop, by contrast, is strict `<`: a chain whose result lands *exactly* at `new_time` is not yet retired and keeps its entry.

> **NOTE —** the eviction loop branches on the relation-node "complete" byte at `*(node−8)`: when it is **≠ 1** the chain is done, so its MRB entry is recycled via `ReleaseMrbReservation` (VLOG "… releasing MRB entry, as it is complete", cc:1355); when it is **== 1** the chain still has a pending next-accumulation, so it is logged as "Chain holding … is now evictable, next Accumulation is …" (mxu_accumulation.cc:1347) and erased *without* a release. On the release path the chain's own `*(chain+0x2c)` validity byte is asserted nonzero (`BUG()` if 0) before the `MrbEntry` is read from `chain+0x20`/`chain+0x28`.

---

## `ReleaseMrbReservation` — the Recycle Pool

### Purpose

`ReleaseMrbReservation(MrbEntry)` `@0x10f5f9e0` returns a freed result-FIFO entry to a per-chunk pool of available entries, so a later reservation can reuse it instead of allocating a new chunk slot. The MRB pool is a true FIFO with fixed retire latency; recycling preserves that ordering.

### `MrbEntry` and the Pool

An `MrbEntry` is a packed `{ short chunk_id, int fifo_index, byte format }`. The pool at `this+0x7a0` is an Abseil `flat_hash_set<MrbEntry>` whose buckets are addressed by `chunk_id`, and each bucket holds an `absl::linked_hash_set<MrbEntry>` (insertion-ordered, so the recycle list itself is a FIFO).

```c
function ReleaseMrbReservation(entry):                   // @0x10f5f9e0
    // 1. Address the per-chunk bucket: chunk_id selects a 0x40-byte sub-table.
    bucket = pool_base(this+0x7a0) + (entry.chunk_id << 6)    // line 33: (int16)entry << 6

    // 2. Hash the whole {chunk_id, fifo_index, format} with the absl CRC32 mixer.
    h = MixingHashState(kSeed @0x22042400)               // _mm_crc32_u64 over the 3 fields (lines 42-43)

    // 3. Insert a recycle node; on first use, grow the SOO table.
    node = operator new(0x20)                            // line 111
    node.entry  = entry                                  // {chunk_id, fifo_index, format}
    node.format = entry.format
    push_front(bucket.free_list /* head @+0x28 */, node) // lines 114-119
    ++bucket.count                                       // free-entry count @+0x38 (line 118)
    return node
    // VLOG "Freeing MRB entry " @0xa1b5c8d
```

> **QUIRK —** the recycle node is `operator new(0x20)` and linked into a *doubly-linked* `linked_hash_set` per chunk, not pushed onto a plain free stack. The linked hash set both dedups (an entry cannot be freed twice into the same chunk) and preserves recycle order. A reimplementation that uses a bare LIFO free list will reuse entries in the wrong order and diverge from the binary's deterministic FIFO reuse.

---

## `SplitAccumulationChain` — Cut and Defer

### Purpose

`SplitAccumulationChain(AccumulationChainAfterSplit&, AccumulationWithOriginalChain const&)` `@0x10f598e0` handles the contention case: a chain that cannot keep its MRB reservation up to the current matmul is cut at the split point. The head retains the accumulations consumed so far; the tail becomes a fresh chain deferred to be re-reserved later in program order.

### Algorithm

```c
function SplitAccumulationChain(chain, split_point):     // @0x10f598e0
    // 1. You cannot split in the past — the cut must be at or after the clock.
    if split_point.matmul_program_order /*accum+0x8*/ < this.latest_matmul_ /*+0x70*/:   // line 63
        FATAL "Cannot split an AccumulationChain in the past"   // mxu_accumulation.cc:1017  @0x84dcedc
        // RetCheck expr: "split_point.accumulation.matmul_program_order >= latest_matmul_"  @0x87b7fa6

    // 2. If the chain still occupies an MRB entry, free it — the split forces a wait.
    if chain.holds_mrb_entry /* *(chain+0x2c) == 1 */:                   // line 74
        chains_unevictable_until_.final_erase_(chain)                    // line 251
        ReleaseMrbReservation(chain.mrb_entry)                           // line 287
        // VLOG "Freeing MRB entry ..., since the occupying chain has been split at ..."  @0xa1daa56 (cc:1033)

    // 3. Cut the accumulation span at the split point.
    consumed   = split_point.matmul - chain.begin /*chain+0x8*/         // line 120
    CHECK(consumed <= chain.len /*chain+0x18*/)                         // "pos > size()" guard
    tail_begin = split_point.matmul                                     // line 119 (a4[13])
    tail_span  = chain.span_ptr /*chain+0x10*/ + 0x60 * consumed        // 0x60-byte stride
    tail_len   = chain.len - consumed
    chain.len  = consumed        /* the head keeps the already-consumed accumulations */   // line 128

    // 4. Defer the tail: store it as a heap-owned chain keyed by program order.
    tail = operator new(0x30) { begin=tail_begin, span=tail_span, len=tail_len, holds_mrb=false }  // line 195
    deferred_tails /* btree_map<long, unique_ptr<AccumulationChainAfterSplit>> */
        .insert({ split_point.matmul_program_order, tail })            // @0x10f5d2e0 (line 204)
```

> **GOTCHA —** step 3 keeps the *head* short and emits the *tail* as the new deferred chain — the head's `len` is set to `consumed` (`*(chain+0x18) = consumed`, line 128), not to `len − consumed`; the heap-owned tail node gets `len − consumed`. The split point is the boundary: everything before it stays in place, everything from it onward is deferred and re-reserved when the clock later reaches `split_point.matmul_program_order`.

> **NOTE —** the tail is owned by a `btree_map<long, unique_ptr<AccumulationChainAfterSplit>>` keyed by program order, so deferred remainders are walked back into the timeline in order. This is the one place the allocator allocates a chain on the heap; the original chains live in the driver's working vector.

---

## Net Policy and the MXU Contrast

The allocator is a single forward sweep with three time-keyed effects per step: split-if-contended, advance-and-evict, reserve-or-extend. There is **no makespan search and no backtracking**. The per-step advance is `ExtendMrbReservation`'s two parallel writes — `push_time + min(CycleTableInstruction, cap@+0x20)` into the per-MXU throughput timeline and `push_time + min(LatencyBetween, cap@+0x28)` into the per-MXU result-entry timeline — the throughput axis from the [`CycleTable`](../cost/jf-cycletable.md), the edge axis from [`LatencyBetween`](../cost/bundle-aware-cost.md#the-dependency-latency-axis--latencybetween), each clamped *from above* by an option cap (`+0x20`/`+0x28`). The two are not summed.

| Aspect | MRB Chain Allocator (this page) | [MXU Assignment Bin-Packer](mxu-assignment-binpacker.md) |
|---|---|---|
| Resource shape | one ordered FIFO with fixed retire latency | N interchangeable MXU passes |
| Discipline | deterministic program-order sweep | greedy min-makespan search |
| Eviction | time-keyed (`AdvanceTimeTo` evict-by-`matres_program_order`) | n/a — places, never evicts |
| Scoring | none — placement is forced by time | `LatchLatencyChangeAfterAdding` per candidate |
| Contention handling | split + defer tail (`SplitAccumulationChain`) | choose a different MXU pass |
| Shared cost inputs | `CycleTable` matmul throughput + `LatencyBetween` edge | same |

The structural difference is the point: the result FIFO is an *ordered* resource whose entries retire on a clock, so the optimal placement is forced by time and no search is needed. The MXU lanes are *interchangeable*, so a search over assignments pays off. They share the cost cells but not the allocation algorithm.

---

## `LatencyBetween` — the Public Edge Wrapper

### Purpose

`LatencyTable::LatencyBetween(LloValue from, LloValue to)` `@0x1c89f820` is the public dispatcher for the read-after-write edge latency that every scheduler stage prices against — including the `ExtendMrbReservation` edge axis above. It wraps the per-gen `LatencyBetweenInternal` (the base model, documented on [`bundle-aware-cost`](../cost/bundle-aware-cost.md#the-dependency-latency-axis--latencybetween)) with two gen-invariant corrections: an **optional random jitter** and the **trace-instruction edge clamps**. This page owns those two corrections.

### Algorithm

```c
function LatencyBetween(from, to):                       // @0x1c89f820
    raw = this->vtable[+0x18](from, to)                  // base: per-gen LatencyBetweenInternal

    // --- OPTIONAL JITTER ---  (line 27)
    bitgen = this->jitter_bitgen  /* this+0x10 */
    if bitgen != null:                                   // null by default → skip
        raw += Uniform(bitgen, 0, 101)                   // bounds (0,101) passed to absl UniformDistributionWrapper<int>
                                                         // via DistributionCaller<BitGen> @0xfa7c9a0 (Randen PRNG)

    // --- TRACE-EDGE CLAMPS ---  (lines 33-52)
    if from.opcode == 0x84 /*trace-arg*/ and to.opcode == 0x84:
        cfg = AutoOr<int>::FromProtoOrDie(AutoProto)     // @0x10979760  ([from+0x10]→[+0x38]→deref→[+0xc78])
        edge = cfg.is_set ? cfg.value : 16               // default 16
        raw  = max(raw, edge)
    else if from.opcode == 0x82 /*set-tracemark*/ and (to.opcode - 0x82) <= 2:   // to ∈ {0x82,0x83,0x84}
        raw = max(raw, 2)                                // min-2 floor on tracemark → trace* edges

    return raw                                            // VLOG latency_table.cc:83 @0x878d9cb
```

### The Jitter Knob

The jitter `BitGen*` lives at `LatencyTable+0x10` and is **initialized to 0** in the base constructor `@0x1c89f800` (`*(this+0x10) = 0`, decompiled), and none of the per-gen derived constructors set it — so jitter is **off by default**. It is installed only when the command-line flag `FLAGS_xla_jf_random_latency` (flag name `"xla_jf_random_latency"` `@0x84bcebc`; flag global `@0x223b47c8`) is enabled.

| Knob | Type | Default | Description |
|---|---|---|---|
| `xla_jf_random_latency` | BOOL/BitGen install | **off (`+0x10` == null)** | When set, installs a Randen-AES `BitGen*` at `LatencyTable+0x10`; every edge then gains a uniform-random perturbation drawn with bounds `(0, 101)` |

> **QUIRK —** the jitter is a *robustness/fuzzing* knob, not a hardware latency term. With it on, the scheduler is exercised against latency variation so its decisions are stress-tested for sensitivity to edge-latency noise — it does not model any real silicon delay. A reimplementer pricing actual TPU cycles must leave `+0x10` null; the deterministic `LatencyBetweenInternal` is the hardware model.

> **NOTE —** the site that wires the flag to the `BitGen*` install was not byte-walked; the null default in the base ctor and the flag's name/global are CONFIRMED, but *when* the install happens (which factory path reads `GetFlag(xla_jf_random_latency)`) is not traced (LOW). The `AutoOr<int>` proto field for the `0x84/0x84` trace-arg edge is decoded structurally (`@0x10979760`); its default of 16 is byte-exact, but the proto field *name* is not pinned (LOW).

### The Trace-Edge Clamps

The clamps keep profiling-marker instruction edges from collapsing onto each other in the schedule. The three opcodes (resolved from the `opcode_string` table `@0x21cd0d60` via `.rela.dyn` RELATIVE-addend decode) are `0x82` = set-tracemark, `0x83` = trace, `0x84` = trace-arg.

| `from` opcode | `to` opcode | Floor applied | Source |
|---|---|---|---|
| `0x84` trace-arg | `0x84` trace-arg | `max(raw, AutoOr<int> default 16)` | line 36–46 |
| `0x82` set-tracemark | `0x82` / `0x83` / `0x84` | `max(raw, 2)` | line 49–51 |

A trace-arg → trace-arg edge takes a configurable floor (default 16 cycles); a set-tracemark → any-trace edge takes a fixed min-2 floor. Both ensure inserted trace markers retain a minimum separation so they do not pile into one bundle, which would corrupt the emitted trace stream.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `(anon)::AssignMrbEntriesToChains` | `0x10f4ac60` | driver: copy → introsort → allocator ctor → main loop | CONFIRMED |
| `MrbChainAllocator::ExtendMrbReservation` | `0x10f58800` | per-step cost cell (throughput and edge, each capped via `min`, written to separate per-MXU timelines) | CONFIRMED |
| `MrbChainAllocator::AdvanceTimeTo` | `0x10f5e9e0` | monotone clock + evict-by-`matres_program_order` | CONFIRMED |
| `MrbChainAllocator::ReleaseMrbReservation` | `0x10f5f9e0` | recycle a freed `MrbEntry` into the per-chunk pool | CONFIRMED |
| `MrbChainAllocator::SplitAccumulationChain` | `0x10f598e0` | cut a chain at the clock; defer the tail | CONFIRMED |
| `bimap final_erase_` | `0x10f61d60` | the `chains_unevictable_until_` relation type (demangled) | CONFIRMED |
| `btree_map<long, unique_ptr<…>>::insert` | `0x10f5d2e0` | store the deferred split-off chain tail | CONFIRMED |
| `LloInstructionPushesToResultFifo` | `0x1d4f3600` | FIFO-push count, rounded to the per-gen granule | CONFIRMED |
| `Target::vtable[+0x390]` | — | MRB-support count gate (`> 0`) | HIGH (slot byte-exact; name INFERRED) |
| `Target::vtable[+0x5e0]` | — | FIFO-push rounding granule (byte) | HIGH (slot byte-exact; name INFERRED) |
| `LatencyTable::LatencyBetween` | `0x1c89f820` | base edge → optional jitter → trace clamps | CONFIRMED |
| `LatencyTable::LatencyTable(TpuVersion)` | `0x1c89f800` | base ctor — `+0x10 = 0` (jitter off) | CONFIRMED |
| `DistributionCaller<BitGen>::Impl<UniformDistributionWrapper<int>>` | `0xfa7c9a0` | the `Uniform(0,101)` jitter draw (Randen PRNG) | CONFIRMED |
| `AutoOr<int>::FromProtoOrDie` | `0x10979760` | trace-arg `0x84/0x84` edge config (default 16) | HIGH (default byte-exact; proto field name LOW) |
| `LloOpcodeString` | `0x1d631360` | opcode → name table `@0x21cd0d60` (0x82/0x83/0x84) | CONFIRMED |

---

## Considerations

- **Program-order is the only ordering.** Every guard (`AdvanceTimeTo`, `SplitAccumulationChain`) keys off `matmul_program_order` and the `latest_matmul_` clock. The introsort at `@0x10f4b092` is a precondition for correctness, not an optimization: if the accumulations are not in ascending matmul order, the monotone-clock RetCheck (cc:1332) fires.
- **Eviction is strict `<`, the clock guard is `>`.** A chain whose `matres_program_order == new_time` is *not* evicted this step (its result has not retired); but the clock may advance *to* that time. The two comparisons use different relations deliberately.
- **The bimap and the per-MXU indices must stay consistent.** `AdvanceTimeTo` post-checks every `chains_next_accumulating_at_[mxu]` front (cc:1371) after eviction. A reimplementation maintaining only the eviction bimap will pass the eviction loop but fail this invariant.
- **`MrbChunkState` / `AccumulationChainAfterSplit` layouts are functional.** The 0x70-byte `AccumulationChainAfterSplit` body (`+0x8` begin, `+0x10` span ptr, `+0x18` len, `+0x20`/`+0x28` the `MrbEntry`, `+0x2c` "has mrb entry" byte) and the per-MXU `MrbChunkState` element are reconstructed from access patterns, not a struct-layout string (LOW on exact byte layout; the field *roles* are CONFIRMED from use).

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-assignment-binpacker` | the MXU sibling — greedy min-makespan over N lanes vs. this deterministic FIFO sweep |
| `mxu-sequence-struct` | the `MxuSequence`/`SequenceInfo` record whose accumulation chains feed this allocator |
| `mrb-fifo-msr-placement` | the next layer — turns the reserved `(chunk_id, fifo_index)` `MrbEntry`s into physical FIFO/MSR addresses |
| `bundle-aware-cost` | owns the base `LatencyBetweenInternal` edge and `MaxResourceCycles` throughput this allocator prices against |
| `jf-cycletable` | the `GetCyclesForThroughput` cell that is the throughput axis of `ExtendMrbReservation` |

---

## Cross-References

- [TPU Scheduling Pipeline](overview.md) — Stage 2 placement of this allocator between the HLO scheduler and the bundle packer.
- [MXU Assignment Bin-Packer](mxu-assignment-binpacker.md) — the MXU sibling allocator; the contrast that explains why MRB needs no makespan search.
- [MxuSequence / SequenceInfo](mxu-sequence-struct.md) — the per-sequence record holding the accumulation chains this pass consumes.
- [MRB FIFO / MSR Placement](mrb-fifo-msr-placement.md) — the downstream pass that materializes the reserved `MrbEntry`s into result-FIFO and MSR addresses.
- [Latch Assignment & Overrun](latch-assignment-overrun.md) — the latch-index commit that the bundle packer reads as a slot-legality input.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — the base `LatencyBetween`/`LatencyBetweenInternal` edge axis and `MaxResourceCycles` throughput this allocator's cost cell uses.
- [JF CycleTable](../cost/jf-cycletable.md) — the per-gen matmul throughput cycles that drive the `ExtendMrbReservation` recurrence.
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — the MXU occupancy reservation model that complements this result-buffer allocator.
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instructions (matmul/matpush/matres) whose accumulation chains this allocator places.
- [LLO Opcode Enum](../isa/llo-opcode-enum.md) — the opcode numbering behind the `0x82`/`0x83`/`0x84` trace-instruction identities.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
