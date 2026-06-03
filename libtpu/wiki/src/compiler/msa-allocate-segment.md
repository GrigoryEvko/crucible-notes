# MSA AllocateSegment

> *Addresses, struct offsets, and bitmask values apply to `libtpu.so` from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`MsaAlgorithm::AllocateSegment` (`0x1dc73ca0`) is the inner loop body of memory-space assignment. The driver — the buffer-interval sort, the colocation grouping, and the autotuner "IOR" replay surface — lives on [msa-overview.md](msa-overview.md); this page documents the per-segment placement *attempt* that the driver calls once for every `(AllocationValue, use)` pair, in sorted order. A "segment" is the live range of one value between its definition (or a prior allocation's end) and a single use; `AllocateSegment` decides where that segment's buffer physically sits — in alternate memory (VMEM) with or without a copy, or back in default memory (HBM) — and emits the `Allocation` objects (and any async `CopyStart`/`CopyDone` pair) that realize the decision.

The reader who knows the upstream XLA `MsaAlgorithm::AllocateSegment` will recognise the shape: reconcile required assignments, then run a fixed cascade of placement primitives that each return an `AllocationResult` bitmask, short-circuiting on the first `kSuccess`. What this build pins down byte-exactly is (1) the **six-stage cascade** order and its short-circuit/uncommit semantics, (2) the `~0x210`-byte **`AllocationRequest`** struct the cascade reads, recovered from the writer `CreateAllocationRequest` (`0x1dc72820`) cross-checked against every field read in the body, and (3) the **11-bit `AllocationResult`** failure bitmask, recovered from `SingleFailureResultToString` (`0x1dc7bc80`). The greedy character is concrete here: there is no backtracking inside `AllocateSegment` — a failed prefetch or evict that leaves pending chunks ORs in `FailRequiresUncommit` (`0x40`) so the *driver* rolls back, and the segment is retried only on a later driver retry with a widened window.

This page documents three things a reimplementer must reproduce: the cascade body and its stage gating, the alternate-memory fit check (`AllocateInAlternateMemoryNoCopy` and the prefetch chunk-candidate search), and the prefetch/eviction insertion (`Prefetch`/`Evict` and the `find_if` that supplies them their predecessor allocation). The interval-picker sweep that prefetch consults, the async-copy resource model, and the per-version numeric defaults are summarized here and detailed on sibling pages.

For reimplementation, the contract is:

- **The cascade.** Six stages — required-assignment reconcile → direct pin → free coloring-reserved + force-min-time → alternate no-copy → prefetch → evict → default fallback — each returning an `AllocationResult`, short-circuiting on `kSuccess`.
- **The request.** The `AllocationRequest` field layout the cascade reads; in particular the four trailing state bytes at `+0x208..+0x20b` that gate which stages run.
- **The result.** The 11-bit failure bitmask, its byte-exact bit→name map, and the `|0x40` (`FailRequiresUncommit`) rule that hands rollback to the driver.
- **The fit check + insertion.** How the no-copy path and the prefetch path each query the best-fit heap for a chunk, and how prefetch/evict find their predecessor allocation.

| | |
|---|---|
| **Method** | `xla::memory_space_assignment::MsaAlgorithm::AllocateSegment(AllocationRequest&)` |
| **Entry** | `0x1dc73ca0` (~5.5 KB; 1301-line decompile) |
| **Request writer** | `MsaAlgorithm::CreateAllocationRequest` @ `0x1dc72820` |
| **Result stringizer** | `MsaAlgorithm::SingleFailureResultToString` @ `0x1dc7bc80`; `ResultToString` @ `0x1dc68c80` |
| **No-copy fit** | `AllocateInAlternateMemoryNoCopy` @ `0x1dc7ca80` |
| **Prefetch / evict** | `Prefetch` @ `0x1dc7e4a0` · `Evict` @ `0x1dc7d660` |
| **Predecessor `find_if`** | `AllocateSegment::$_2` @ `0x1dc7e420` |
| **IR level** | XLA HLO (`HloValue` / `HloUse`), heap-simulator timeline (logical times) |
| **Memory spaces** | `kDefault` = HBM (`0`), `kAlternate` = VMEM (`1`); v7 VMEM budget 64 MiB / 512-B word |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The `AllocationRequest`

`CreateAllocationRequest` (`0x1dc72820`) builds one `AllocationRequest` per `(value, use)` and hands it to `AllocateSegment` by reference. The struct is `~0x210` bytes; the layout below was recovered from the writer's field-store region (`0x1dc730d8..0x1dc731e5`) cross-checked against every `[rbx+N]` / `[a2+N]` read in the cascade body. The decompile confirms the gating-byte offsets directly: `Evict` is called with `*((unsigned __int8 *)a2 + 520)` (= `+0x208`) as its force argument, and `Prefetch` with `*((unsigned __int8 *)a2 + 521)` (= `+0x209`) as its window argument.

| Offset | Field | Meaning | Confidence |
|---|---|---|---|
| `+0x00` | `inclusive_start_time` | `max(use_time, earliest_prefetch)` or preferred-override time | CONFIRMED |
| `+0x08` | `end_time` | use latest time bound | CONFIRMED |
| `+0x10` | `latest_prefetch_time` | corrected use time bound | CONFIRMED |
| `+0x18` | `require_start_time` | required def time | CONFIRMED |
| `+0x20` | `size` | `BufferInterval` size, bytes | CONFIRMED |
| `+0x28` | `allow_no_copy_alternate_mem` | bool | CONFIRMED |
| `+0x29` | `require_no_copy_alternate_mem` | bool | CONFIRMED |
| `+0x2a` | `require_copy_allocation` | bool (arg) | CONFIRMED |
| `+0x2b` | `no_copy_chunk_required` | bool | CONFIRMED |
| `+0x2c` | `allow_prefetch` | bool | CONFIRMED |
| `+0x30` | `earliest_prefetch_time` | packed: low byte flag, time `>>8` | HIGH |
| `+0x38` | `has_preferred_prefetch_time` | bool | CONFIRMED |
| `+0x40` | `preferred_prefetch_time` | packed (low-byte flag \| `time<<8`) | HIGH |
| `+0x48` | `prefer_no_copy` | bool | CONFIRMED |
| `+0x50` | `preferred_offset` | `AliasedOffset*` (arg) | CONFIRMED |
| `+0x58` | `use` | `const HloUse*` — the use being placed | CONFIRMED |
| `+0x60` | `allocation_value` | `AllocationValue*` | CONFIRMED |
| `+0x68` | `all_use_times` | 16-B span head/span | HIGH |
| `+0x88` | `required_copy_done_time` | int64 | HIGH |
| `+0x90` | `allow_sync_conversion` | bool (arg) | CONFIRMED |
| `+0xc0` | `split_shape` | `Shape`, constructed iff `+0x200 == 1` | HIGH |
| `+0x200` | `has_split_shape` | bool | HIGH |
| `+0x208` | *force-evict flag* | passed as `Evict`'s `bool force` | CONFIRMED |
| `+0x209` | *allow-prefetch-window flag* | passed as `Prefetch`'s `bool window` | CONFIRMED |
| `+0x20a`, `+0x20b` | *uncommit / retry state bits* | consulted to gate retry/evict/bail | HIGH |

The four trailing bytes at `+0x208..+0x20b` are the "already-attempted / requires-uncommit" state. `+0x208` (cmp 1) gates the `ForceAlternateMemoryAllocationForMinTime` path and is the `force` argument to `Evict`; `+0x209` is the `window` argument to `Prefetch`; `+0x20a|+0x20b` OR'd together gate the colored-reserved free path. The `kCopy` opcode (`0x28`) referenced inside `CreateAllocationRequest` is the sync-copy match that lets MSA convert an already-scheduled synchronous `kCopy` into an MSA-managed async copy (see [Async-copy resource and sync replacement](#async-copy-resource-and-sync-replacement)).

> **NOTE — `~0x210` not exact.** The struct size is bounded below (`≥0x210`) by the highest observed store; padding past `+0x20b` is not enumerated. Offsets `+0x68/+0xc0/+0x200` and the two packed-time fields are decoded from store widths and the conditional `Shape` ctor at `0x20cd6660`, hence HIGH rather than CONFIRMED.

---

## The `AllocationResult` bitmask

The return type is not an enum — it is an 11-bit failure bitset where `0` is success and each set bit is one failure reason. `SingleFailureResultToString` (`0x1dc7bc80`) masks the low byte (`0x7f`) through a 65-entry jump table at `0xb564d94` and tests the three high bits (`0x80/0x100/0x200`) with explicit compares; each arm stores its result string. `Success`, `FailOutOfMemory`, and `UnknownResult` are inline string literals in the decompile; the remaining names are stored via rodata pointers (so they do not surface as inline string args), consistent with the jump-table-plus-rodata structure.

| Bit | Name | Confidence |
|---|---|---|
| `0x000` | `Success` (no bits set) | CONFIRMED |
| `0x001` | `FailOutOfMemory` | CONFIRMED |
| `0x002` | `FailPrevAllocationNotInAlternateMem` | CONFIRMED |
| `0x004` | `FailLiveRangeTooLong` | CONFIRMED |
| `0x008` | `FailLiveRangeTooShort` | CONFIRMED |
| `0x010` | `FailOutOfAsyncCopies` | CONFIRMED |
| `0x020` | `FailViolatesAsyncCopyResource` | CONFIRMED |
| `0x040` | `FailRequiresUncommit` | CONFIRMED |
| `0x080` | `FailConflictingPreferredOffsets` | CONFIRMED |
| `0x100` | `FailSyncDataMoveReplacement` | CONFIRMED |
| `0x200` | `AllSlicesHaveTheSameStartTime` | CONFIRMED |
| else | `UnknownResult` | CONFIRMED |

A result with multiple bits set is decomposed and joined name-by-name by `ResultToString` (`0x1dc68c80`). Two predicate helpers read the mask: `result_requires_uncommit()` tests bit `0x40`; `result_failed_because_of_async_copy()` tests `0x10|0x20`. The semantics that matter for the cascade:

- **`FailRequiresUncommit` (`0x40`)** is not produced by a primitive — `AllocateSegment` ORs it in *after* a failed evict (and on the residual-fail fallback path) to tell the driver that pending chunks for this value must be rolled back before the next attempt. The decompile shows this OR at three sites (`v69 | 0x40`, `v240 | 0x40`, `v83 | 0x40`).
- **`FailOutOfAsyncCopies` (`0x10`)** vs **`FailViolatesAsyncCopyResource` (`0x20`)** distinguish "the configured outstanding-copy *cap* is hit" from "the copy cannot fit any free time-bucket window" — both come out of the async-copy resource model, not from a counter.

---

## The cascade

`AllocateSegment` runs a fixed sequence of placement attempts, each returning an `AllocationResult`, and returns at the first `kSuccess` (`== 0`). The stages, in body order:

```c
// MsaAlgorithm::AllocateSegment(AllocationRequest& req)            // 0x1dc73ca0
function AllocateSegment(req):
    // ---- STAGE 0: REQUIRED-ASSIGNMENT RECONCILIATION ----
    def_req   = RequiredMemoryAssignmentAt(value, def_time)        // 0x1dc48840
    use_req   = RequiredMemoryAssignmentAt(value, use_time)        // 0x1dc48840
    alias_req = AliasedRequiredAssignmentForUse(use)               // 0x1dc769c0
    if conflict(def_req, use_req):                                 // (memory_space, offset, padding)
        FATAL(def_req.ToString(), use_req.ToString(), use)         // 0x1dc47680
    required = reconcile(def_req, use_req, alias_req)

    // ---- STAGE 1: DIRECT PIN (colored / required alternate-memory) ----
    if required == kAlternate:
        a = new PinnedAllocation(position, kAlternate, chunk,      // 0x1dcdc400
                                 def_time, use_time)
        a->AddUse(use)                                             // 0x1dcda700
        value.allocation_sequence.push_back(a)                     // 0x1dc55740
        CreateOrAddToAliasedOffset(*a, req.preferred_offset)       // 0x1dc79160
        return kSuccess

    result = kSuccess                                              // sentinel cleared

    // ---- STAGE 2: free coloring-reserved + dual-live + force-min-time ----
    if req.allow_no_copy_alternate_mem        // +0x28
       or req.no_copy_chunk_required          // +0x2b
       or req.require_no_copy_alternate_mem:  // +0x29
        FreeAlternateMemoryColoringReservedAllocations(req)        // 0x1dc7c640
        if req.require_copy_allocation:        // +0x2a
            CheckAndUpdateForDualLiveAllocationValues(prev, req)   // 0x1dc7bf20
            if ForceAlternateMemoryAllocationForMinTime(req):      // 0x1dc7d460
                return result                  // non-zero -> propagate

        // ---- STAGE 3: ALTERNATE NO-COPY (the fit check) ----
        result = AllocateInAlternateMemoryNoCopy(req)              // 0x1dc7ca80
        if result == kSuccess: return kSuccess

    // ---- STAGE 4: PREFETCH (insert async copy) ----
    prev_alloc = *find_if(value.allocation_sequence, $_2)          // 0x1dc7e420
    if req[+0x209] or req.allow_prefetch:      // +0x2c
        result |= Prefetch(req, *prev_alloc, /*window=*/req[+0x209]) // 0x1dc7e4a0
        if result == kSuccess: return kSuccess

    // ---- STAGE 5: EVICT ----
    if has_prior_alternate_alloc:
        result = Evict(req, /*force=*/req[+0x208])                 // 0x1dc7d660
        if result == kSuccess: return kSuccess
        result |= 0x40   // FailRequiresUncommit -> driver rolls back pending
        return result

    // ---- STAGE 6: DEFAULT-MEMORY FALLBACK ----
    d = new PinnedAllocation(position, kDefault/*0*/, none, def, use) // 0x1dcdc400
    d->AddUse(use)                                                 // 0x1dcda700
    value.allocation_sequence.push_back(d)                         // 0x1dc55740
    return result   // may carry residual fail bits (also OR'd 0x40 on a fail path)
```

The decompile confirms the call order directly: `FreeAlternateMemoryColoringReservedAllocations` at line 717, `AllocateInAlternateMemoryNoCopy` at 723, `ForceAlternateMemoryAllocationForMinTime` at 749, `Evict(..., a2[+0x208])` at 829 (with `| 0x40` immediately after at 832), and `Prefetch(..., a2[+0x209])` at 988. The `PinnedAllocation` ctor (`0x1dcdc400`) and `AddUse` (`0x1dcda700`) each appear four times — twice for Stage 1's direct alternate pin and twice for the Stage 6 default fallback.

> **NOTE — no in-function backtracking.** Stages 3–6 do not undo each other's side effects. Once a primitive commits chunks to the heap, only the driver's uncommit path (triggered by `FailRequiresUncommit`) reclaims them. This is the difference between "greedy with retry" (what MSA is) and a true backtracking search.

### Stage 0 — reconcile required assignments

Three required-assignment lookups run first. `RequiredMemoryAssignmentAt(value, t)` (`0x1dc48840`) returns any hard memory-space pin the value carries at logical time `t` — one for the def, one for the use. `AliasedRequiredAssignmentForUse(use)` (`0x1dc769c0`) folds in any pin inherited through a colocation/alias group (e.g. a while-loop carried value, or a buffer the layout pass colored). If the def and use disagree on `(memory_space, offset, padding)`, the pass is in an unsatisfiable state and emits a `LogMessageFatal` printing all three `ToString()`s — a genuine conflict is a compiler bug, not a fallback. Otherwise the consistent assignment becomes `required`.

### Stage 1 — direct pin

If `required == kAlternate`, the value *must* live in VMEM (it was colored or carries a required alternate pin), so there is nothing to decide: build a `PinnedAllocation` over `[def_time, use_time]` in `kAlternate`, attach the use, push it onto the value's allocation sequence, and thread the `AliasedOffset` through `CreateOrAddToAliasedOffset` so every member of the colocation group shares one VMEM offset. Returns `kSuccess` unconditionally — the chunk reservation happened during coloring.

### Stage 2 — free reserved + force-min-time

Reached only if the request allows alternate memory at all (`+0x28 || +0x2b || +0x29`). `FreeAlternateMemoryColoringReservedAllocations` releases VMEM that coloring had reserved but that this segment may now claim. When the request *requires* a copy allocation (`+0x2a`), `CheckAndUpdateForDualLiveAllocationValues` handles the case where the value is simultaneously live in two places, then `ForceAlternateMemoryAllocationForMinTime` attempts to force the buffer into VMEM for at least its minimum live interval; if that returns non-zero it propagates straight out (no further stages).

---

## The fit check — `AllocateInAlternateMemoryNoCopy`

Stage 3 is the cheapest placement: keep the buffer in VMEM across `[def, use]` with **no copy at all**, which is possible only if a free VMEM chunk of the requested size persists for the whole interval. `AllocateInAlternateMemoryNoCopy` (`0x1dc7ca80`) queries the best-fit heap for that chunk (the decompile shows one `FindChunkCandidate`-class call). The heap is the `GlobalDecreasingSizeBestFitHeap` that MSA maintains over the VMEM byte budget; on v7 that budget is 64 MiB (`67,108,864` B) minus any scoped reservation, and every chunk offset/size aligns to the 512-byte VMEM word — see [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md) and the allocator detail on [../memory/vmem-allocator.md](../memory/vmem-allocator.md).

```c
// AllocateInAlternateMemoryNoCopy(req)                            // 0x1dc7ca80
result = kSuccess
chunk  = heap.FindChunkCandidate(/*size=*/req.size,               // best-fit over the
                                 /*interval=*/[def, use])         //   VMEM byte budget
if not chunk:
    return FailOutOfMemory            // 0x01 (no persistent free chunk)
heap.Commit(chunk)
a = new PinnedAllocation(position, kAlternate, chunk, def, use)
a->AddUse(use)
value.allocation_sequence.push_back(a)
return kSuccess
```

The fit check is *temporal*, not just spatial: the chunk must be free for the entire `[def, use]` interval on the heap-simulator timeline, because a no-copy allocation occupies VMEM continuously. If no single chunk spans the whole interval the result is `FailOutOfMemory` (`0x01`) and the cascade falls through to prefetch.

---

## Prefetch and eviction insertion

If the buffer cannot live in VMEM for free across the whole interval, MSA tries to *copy it into VMEM just in time* (prefetch) or, if the value already has an older VMEM allocation, *copy it back out to HBM* (evict). Both primitives need the value's predecessor allocation — the one whose interval abuts the current use — which `AllocateSegment` locates with a `find_if` over `value.allocation_sequence` using the lambda predicate `$_2` (`0x1dc7e420`).

### Prefetch (Stage 4)

`Prefetch` (`0x1dc7e4a0`) decides whether copying the buffer from HBM into VMEM ahead of the use is profitable and feasible:

```c
// Prefetch(req, prev_allocation, window)                          // 0x1dc7e4a0
benefit = CostAnalysis::GetAlternateMemoryBenefit(use)            // 0x1dcec480
        // weighed against OperandBytesAccessed / OutputBytesAccessed
        //   (0x1dceb340 / 0x1dceb360)
picker.Begin(use, earliest_prefetch, latest_prefetch, preferred)  // 0x1dcd7a00
while not picker.Done():
    t_start = picker.Next()                                       // 0x1dcd7e20
    chunk   = FindBestChunkCandidate(req, [t_start, use_time])    // 0x1dc7fb00
            //   -> GlobalDecreasingSizeBestFitHeap::FindChunkCandidate (0x1e48f960)
    if not chunk: continue
    if not AsyncCopyResource.HasEnoughResource(t_start, use_time, cost):
        result |= FailViolatesAsyncCopyResource  // 0x20
        continue
    emit CopyAllocation(kCopyStart @ t_start, kCopyDone @ use_time)
    //   or SlicedCopyAllocation (make_unique @ 0x1dc7f7e0) on the sliced path
    return kSuccess
return result   // accumulated fail bits
```

The interval picker sweeps candidate copy-start times outward from a preferred time using two cursors (an increasing one seeded from `earliest_prefetch_time`, a decreasing one from `latest_prefetch_time`) and a direction flag, integrating bandwidth-idle time so the copy is placed where it best hides behind compute. For each candidate start time, `FindBestChunkCandidate` (`0x1dc7fb00`) asks the VMEM heap for a chunk over `[t_start, use_time]` (the chunk only needs to be free from the copy's *arrival*, not from the def, which is why prefetch can succeed where no-copy failed), and the async-copy resource model verifies the copy fits the DMA-bandwidth time budget. The picker, its three overlap ratios, and the `/100.0` bandwidth-idle integration step are detailed on [msa-overview.md](msa-overview.md); see the per-version ratio defaults on [msa-per-version-defaults.md](msa-per-version-defaults.md).

> **CONFIDENCE — picker integration constant.** The `/100.0` (float at `0x84a2fb8`) integration granularity in the picker `Next` was traced at the disassembly level; the C decompile renders the float arithmetic opaquely (the constant does not surface as a literal in pseudocode), so the *constant value* is HIGH, while the picker functions' existence and signatures are CONFIRMED by their demangled symbols.

### Evict (Stage 5)

Eviction is the inverse: a value that already has a VMEM (`kAlternate`) allocation may need to be copied back to HBM to make room. `Evict` (`0x1dc7d660`) is reached only when `has_prior_alternate_alloc` holds, and takes the `force` flag from `AllocationRequest +0x208`:

```c
// Evict(req, force = req[+0x208])                                 // 0x1dc7d660
prev = the prior kAlternate allocation for this value
emit CopyAllocation(kCopyStart in VMEM @ prev.end,
                    kCopyDone in HBM)         // copy buffer OUT to default memory
if not AsyncCopyResource.HasEnoughResource(...):
    return FailViolatesAsyncCopyResource      // 0x20 (or FailOutOfAsyncCopies 0x10)
return kSuccess
```

On evict failure the cascade does **not** fall through silently — it ORs `FailRequiresUncommit` (`0x40`) into the result and returns, signalling the driver to roll back any chunks this segment pended. This is the only place a primitive's failure is escalated to a forced driver uncommit rather than a fall-through to the next stage.

### Stage 6 — default-memory fallback

If the value never had a prior VMEM allocation (so evict is not applicable) and prefetch failed, the buffer simply stays in HBM: build a `PinnedAllocation` in `kDefault` (`0`) over the use, attach the use, push it. The returned `result` may still carry residual fail bits from the prefetch attempt (and, on the fail path, an OR'd `0x40`), which the driver interprets — a non-success result here means MSA could not improve on HBM placement for this segment, not that compilation failed.

---

## Async-copy resource and sync replacement

Both prefetch and evict gate their copies through `AsynchronousCopyResource`, a per-logical-time free-resource bucket model (not a simple counter). `HasEnoughResource(start, end, resource)` (`0x1dc78d40`) walks the buckets over `[start, end)` checking the copy's cost can be drawn from the available DMA-bandwidth windows; `ConsumeResource` (`0x1dc77540`) debits them; `AddCopy`/`RemoveCopy` (`0x1dc78580` / `0x1dc787c0`) commit/undo. A copy that fits no bucket window yields `FailViolatesAsyncCopyResource` (`0x20`); exceeding the configured outstanding-copy *cap* yields `FailOutOfAsyncCopies` (`0x10`).

The `kCopy` opcode (`0x28`) match inside `CreateAllocationRequest` feeds the sync-copy-replacement path: an already-scheduled synchronous `kCopy` (or `kSlice`) can be converted into an MSA-managed async copy when alternate-memory benefit justifies it, gated by `xla_msa_enable_sync_copy_replacement` / `_sync_slice_replacement` and bounded by `copies_limit_for_sync_mem_op_conversion`. A failed conversion surfaces as `FailSyncDataMoveReplacement` (`0x100`). The resource-model loop body and the sync-replacement state machine are not fully decoded at the basic-block level (see [Limits](#limits)).

---

## Worked example — one while-body activation buffer

Trace one while-body activation `%act` (size 2 MiB, defined by a matmul at logical `t=100`, consumed at `t=160`) through the v7 pipeline (VMEM = 64 MiB, 512-B word):

1. **Driver sort.** The buffer-interval comparator ranks `%act` by `memory_boundedness_score × size`; the producing matmul is bandwidth-bound, so the score is high and `%act` is visited early (see [msa-overview.md](msa-overview.md)).
2. **Request.** `CreateAllocationRequest` builds the `AllocationRequest`: `+0x00` start = `earliest_prefetch`, `+0x08` end = `160`, `+0x20` size = 2 MiB, `+0x2c` `allow_prefetch = 1`, `+0x58` use = `%act@160`.
3. **Stage 3 fit check.** `AllocateInAlternateMemoryNoCopy` asks the heap for a 2-MiB chunk free across `[100, 160]`. If one persists, a `kAlternate` `PinnedAllocation` is placed → `kSuccess`, done.
4. **Stage 4 prefetch.** Otherwise `picker.Begin(use=%act@160, earliest, latest)`; `Next()` sweeps candidate copy-start times; for each, `FindBestChunkCandidate` asks for a 2-MiB chunk over `[t_start, 160]` (only needs to be free from the copy's arrival), and `AsyncCopyResource.HasEnoughResource(t_start, 160, cost)` verifies the copy's DMA window. On success a `CopyAllocation` (`kCopyStart @ t_start`, `kCopyDone @ 160`) is emitted → `kSuccess`. Both chunks are 512-B-aligned.
5. **Stage 5 evict / Stage 6 fallback.** If prefetch fails and an older `kAlternate` allocation exists, `Evict` copies an older buffer out; if that fails, `result |= 0x40` and the driver rolls back pending chunks. If no prior VMEM allocation exists, `%act` stays in HBM (`kDefault` `PinnedAllocation`).
6. **Handoff.** The chosen `Chunk{offset, size}` rides in the `Allocation`; after the driver's `Process()` it becomes a static program-memory offset that the on-device allocator pins and never relocates — see [../memory/on-device-compaction.md](../memory/on-device-compaction.md).

---

## Limits

What this page does **not** pin down byte-exactly:

- **Numeric ratio/cap defaults.** The literal `min/preferred/max_overlap_to_async_copy_ratio` and `max_outstanding_prefetches/evictions` per TPU version live in the per-version `TpuCompilationEnvironment` proto field-default overlays, not in code; the flags are all wired (see [msa-per-version-defaults.md](msa-per-version-defaults.md)). Same gap for `inefficient_use_to_copy_ratio` / `memory_budget_ratio`. (LOW for the numeric values; the flag names are CONFIRMED.)
- **`ConsumeResource` spill/delay loop.** Decoded to the call level; the inner loop that spreads a copy across later buckets and records the induced delay is not enumerated. (HIGH.)
- **Sync-replacement state machine.** The `AsyncConversionResult` value layout and the exact `copies_limit_for_sync_mem_op_conversion` enforcement point are not fully decoded. (HIGH.)
- **Picker integration constant** — see the CONFIDENCE callout in [Prefetch](#prefetch-stage-4). (HIGH for the `100.0` value.)

---

## Cross-References

- [msa-overview.md](msa-overview.md) — the MSA driver: buffer-interval sort, colocation grouping, the autotuner IOR replay surface, and the prefetch interval picker / async-copy resource model in full.
- [msa-per-version-defaults.md](msa-per-version-defaults.md) — overlap ratios and outstanding-copy caps per TPU generation; the 5-variant per-family flag override scheme.
- [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md) — the VMEM byte budget, scoped reservations, and the HBM fallback policy this cascade feeds.
- [layout-assignment.md](layout-assignment.md) — the pass that stamps `memory_space` and colors buffers, producing the required-assignment pins Stage 0 reconciles.
- [compile-phases.md](compile-phases.md) — where MSA sits in the compilation pipeline.
- [../memory/vmem-allocator.md](../memory/vmem-allocator.md) — the VMEM best-fit allocator the no-copy and prefetch fit checks query.
- [../memory/on-device-compaction.md](../memory/on-device-compaction.md) — how the static chunk offsets MSA assigns are pinned at runtime.
