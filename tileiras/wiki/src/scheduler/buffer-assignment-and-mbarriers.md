# Buffer Assignment and Named-Barrier Binding

## Abstract

Once the modulo scheduler has fixed `II` and the steady-state stage count, a post-pipelining pass binds each pipelined value to a concrete physical buffer (an SMEM region, a TMEM region, or a TMA descriptor slot) and to a named mbarrier slot for the producer/consumer handshake. The pass is `sub_13692E0`. It runs in four phases over the loop body and over every `nv_tileas.async.pipeline.create_pipeline` op.

It consumes the schedule analysis published by the modulo scheduler and produces a per-pipeline-value allocation record. Later materialization passes lower those records into the `Pipe_` and `Mutex_` IR documented in [Pipe_ and Mutex_ Value-Header Layout](pipe-mutex-value-layout.md).

## Phase Outline

The four phases run unconditionally in order. Phase 1 and Phase 2 are gating — either failure aborts the pass before any physical buffer is committed. Phase 3 walks pipeline values once and dispatches to the SMEM or TMEM binder. Phase 4 merges disjoint-lifetime pipelines so they can share one physical buffer.

| Phase | Worker | Diagnostic on failure |
|---|---|---|
| 1. resolve lifetime | `sub_1367080` | `"fails to resolve lifetime"` |
| 2. assign named barriers | `sub_13692A0` → `sub_1368BF0` | `"fails to assign named barrier"` |
| 3. pick buffer class and bind | `sub_13606F0`; SMEM via `sub_1356650` + `sub_13513A0`; TMEM via `sub_1360730` | `"fails to assign smem buffer"` / `"fails to assign tmem buffer"` |
| 4. share buffers (union-find) | `sub_1361790` | (no failure path; emits `"share pipeline buffer"`) |

```c
LogicalResult bufferAssign(FunctionOpInterface fn) {
    if (failed(resolveLifetime(fn)))            return emit("fails to resolve lifetime");      // Phase 1
    if (failed(assignNamedBarriers(fn)))        return emit("fails to assign named barrier"); // Phase 2
    for (PipelineValue *pv : pipelineValues(fn)) {                                            // Phase 3
        BufferClass cls = pickBufferClass(pv);
        if (cls == SMEM && failed(assignSmem(pv))) return emit("fails to assign smem buffer");
        if (cls == TMEM && failed(assignTmem(pv))) return emit("fails to assign tmem buffer");
    }
    sharePipelineBuffers(fn);                                                                  // Phase 4
    return success();
}
```

## Phase 1 — Resolve Lifetime

`sub_1367080` walks every `nv_tileas.async.pipeline.create_pipeline` op and computes the live range of its produced values across the loop body. The walk starts at the producer op and follows the SSA use-def chain through every consumer in the same region, terminating at the last use before the end of the loop body. For pipelined producers the live range crosses the iteration boundary in modulo space; the walker normalizes endpoints into `(stage, cycle)` pairs so Phase 4 can compare them.

Allocation-predecessor collection rides in Phase 1. `sub_135CD10` walks each pipeline value's producer chain — the back-cone of `AllocationOpInterface`-tagged ops — and records them in the lifetime computation, so the assigner sees the full set of buffers that must coexist at every point in the iteration. AllocationOpInterface dispatch routes through `sub_1365310`.

A lifetime that resists normalization (cyclic producer chain, missing iteration anchor, or producer-without-consumer) is fatal. The pass emits `"fails to resolve lifetime"` and aborts before any barrier or buffer is committed.

## Phase 2 — Assign Named Barriers

`sub_13692A0` delegates to `sub_1368BF0`, which walks the pipeline-value list and hands each producer/consumer pair one named mbarrier slot. Blackwell exposes 32 named mbarriers per CTA; the slot index is encoded as a small integer that the later materializer turns into a `bar.sync` operand.

The 32-slot pool is the binding constraint. The binder first tries to allocate a fresh slot for each pair. When the pool is exhausted, it falls back to reuse: two pairs whose lifetimes do not overlap in the steady-state schedule can share one slot. The overlap test reuses the `(stage, cycle)` endpoints computed in Phase 1. If neither fresh allocation nor reuse succeeds for some pair, the pass emits `"fails to assign named barrier"` and aborts.

```c
LogicalResult assignNamedBarriers(FunctionOpInterface fn) {
    NamedBarrierPool pool(32);
    for (PipelineValue *pv : pipelineValues(fn)) {
        if (int slot = pool.allocateFresh(); slot >= 0) {
            pv->namedBarrier = slot;
            continue;
        }
        if (int slot = pool.reuseDisjoint(pv->lifetime); slot >= 0) {
            pv->namedBarrier = slot;
            continue;
        }
        return failure();
    }
    return success();
}
```

The named-barrier index later lands in the `Mutex_` header documented in [Pipe_ and Mutex_ Value-Header Layout](pipe-mutex-value-layout.md).

## Phase 3 — Pick Buffer Class and Bind

`sub_13606F0` decides whether each pipeline value lives in SMEM or TMEM, then dispatches to the matching binder. The SMEM path runs `sub_1356650` (region selection) followed by `sub_13513A0` (offset assignment within the chosen region). The TMEM path runs `sub_1360730`, the tmem-binder, which allocates from the TMEM region and writes the handle into the pipeline-value record.

A heuristic table decides buffer class. Tile-shaped values with element types of at least 8 bits and total size above 16 KB land in TMEM; everything else stays in SMEM. The threshold reflects Blackwell's TMEM geometry — TMEM is the high-capacity tile store and is too coarse for sub-tile or small-element traffic. The Blackwell `tmem` subtarget feature gates TMEM allocation; on subtargets that do not advertise it, `sub_13606F0` collapses to SMEM. The feature flag is the same one documented in [NVPTX Subtarget and Feature Matrix](../codegen/nvptx-subtarget-and-feature-matrix.md).

Each pipeline value gets a 0x348-byte record allocated via `sub_44A8C20(0x348)`. The record carries the producer-op pointer, the variadic list of consumer-op pointers, the buffer-class enum (SMEM/TMEM/named-barrier-only), the SMEM byte offset or TMEM handle, the named-barrier index from Phase 2, the steady-state stage count, and the `(stage, cycle)` lifetime endpoints. TMA descriptor traffic also lands in this record; the TMA path is documented in [TMA, Tensormap and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md).

A binder failure is fatal: the pass emits `"fails to assign smem buffer"` or `"fails to assign tmem buffer"` and aborts. Common causes are SMEM exhaustion at the chosen stage count, an oversize tile that exceeds the TMEM region, or an alignment requirement that cannot be satisfied at the candidate offset.

## Phase 4 — Share Buffers

Phase 4 walks the union-find pipeline-id helper `sub_1361790` and merges pipelines whose lifetimes are disjoint. Two pipeline values qualify as merge candidates when their lifetimes do not overlap in steady-state `(stage, cycle)` space and they agree on buffer class, element type, and footprint. The merge collapses two records into one, keeping a single SMEM offset or TMEM handle.

Each successful merge emits the diagnostic `"share pipeline buffer"`. Failures here are not fatal — an unmerged pipeline simply keeps its own buffer. Phase 4 exists to recover SMEM and TMEM capacity in deep pipelines, where the modulo scheduler can produce many pipeline values whose lifetimes never actually coexist at any one cycle.

```c
void sharePipelineBuffers(FunctionOpInterface fn) {
    UnionFind uf = buildPipelineIdHelper(fn);                  // sub_1361790
    for (auto [a, b] : candidatePairs(fn)) {
        if (!disjointLifetimes(a, b))            continue;
        if (a->bufferClass != b->bufferClass)    continue;
        if (a->footprint  != b->footprint)       continue;
        uf.merge(a, b);
        emit("share pipeline buffer");
    }
}
```

## Per-Record Allocation

The 0x348-byte record is the canonical unit of buffer-assignment state. Phase 1 allocates it up front, Phases 2 and 3 populate it, and Phase 4 may merge it with another.

| Field | Source phase |
|---|---|
| producer-op pointer | Phase 1 |
| consumer-op pointers (variadic) | Phase 1 |
| `(stage, cycle)` lifetime endpoints | Phase 1 |
| stage count | Phase 1 (from schedule analysis) |
| named-barrier index | Phase 2 |
| buffer-class enum | Phase 3 |
| SMEM offset / TMEM handle | Phase 3 |
| union-find parent | Phase 4 |

The record is consumed downstream by the `Pipe_` and `Mutex_` materializer, which copies the named-barrier index and buffer-class enum into the 808-byte value header documented in [Pipe_ and Mutex_ Value-Header Layout](pipe-mutex-value-layout.md).

## Usage and Contract

The pass runs once per function after `TileASGenerateSchedule` produces a valid `ScheduleAnalysis` and before `MaterializeSchedule` rewrites IR. It consumes the per-op `(stage, order)` assignment, the steady-state `II` and stage count, every `nv_tileas.async.pipeline.create_pipeline` op in the function body, and the Blackwell `tmem` subtarget feature flag from the target description. It emits the 0x348-byte per-pipeline-value allocation records — one per pipeline value, populated incrementally across the four phases — and the union-find merge map that tells the materializer which records share a physical buffer. Failures from any of Phases 1–3 abort the function before any IR is rewritten; Phase 4 failures are silently ignored because the worst case is a less efficient but still correct schedule.

## Diagnostics

A buffer-assignment failure should include enough state to distinguish the four phases:

- the candidate `II` and stage count;
- the failing phase and the matching diagnostic string (`"fails to resolve lifetime"`, `"fails to assign named barrier"`, `"fails to assign smem buffer"`, `"fails to assign tmem buffer"`);
- the pipeline-value id and its computed `(stage, cycle)` endpoints;
- the current occupancy of the 32-slot named-barrier pool;
- the SMEM region or TMEM region offset map at the point of failure;
- the buffer-class decision and the element-type / footprint inputs that produced it.

Together they let users separate an impossible loop body from a heuristic failure that can be retuned by changing the stage count, the tile size, or the buffer-class threshold.

## Cross-References

[Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) publishes the `II` and stage count consumed here. [Pipe_ and Mutex_ Value-Header Layout](pipe-mutex-value-layout.md) documents the 808-byte header that carries the buffer-class enum and named-barrier index downstream. [NVPTX Subtarget and Feature Matrix](../codegen/nvptx-subtarget-and-feature-matrix.md) defines the Blackwell `tmem` gate consulted by Phase 3. [TMA, Tensormap and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md) covers the TMA descriptors that share this allocation record.
