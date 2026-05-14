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
    if (failed(resolveLifetime(fn)))           return emit("fails to resolve lifetime");      // Phase 1
    if (failed(assignNamedBarriers(fn)))       return emit("fails to assign named barrier"); // Phase 2
    for (PipelineValue *pv : pipelineValues(fn)) {                                            // Phase 3
        BufferClass cls = classify_buffer(pv);
        if (cls == SMEM && failed(assignSmem(pv))) return emit("fails to assign smem buffer");
        if (cls == TMEM && failed(assignTmem(pv))) return emit("fails to assign tmem buffer");
    }
    sharePipelineBuffers(fn);                                                                  // Phase 4
    return success();
}
```

## Phase 1 — Resolve Lifetime

Phase 1 walks every `nv_tileas.async.pipeline.create_pipeline` op and computes the live range of its produced values across the loop body. The walk starts at the producer op and follows the SSA use-def chain through every consumer in the same region, terminating at the last use before the end of the loop body. For pipelined producers the live range crosses the iteration boundary in modulo space, so the walker normalises endpoints into `(stage, cycle)` pairs that Phase 4 can later compare.

Alongside the live-range walk, Phase 1 builds an alias table that records which pipeline values must share storage because they refer to the same underlying buffer. The table is keyed on a memory-flow root — the upstream `AllocationOpInterface` op that produced the buffer — and seeded by walking every pipeline value's back-cone of allocation ops, then probing the table with find-or-insert semantics.

```c
LogicalResult resolveLifetime(FunctionOpInterface fn) {
    AliasTable alias = alias_table_create();

    for (PipelineValue *pv : pipelineValues(fn)) {
        Operation *root = walk_memory_flow_to_alloc(pv->producer);
        if (root == NULL) return failure();                  // unanchored back-cone

        // Find-or-insert. probe() returns the existing slot or installs a new one.
        AliasSlot *slot = alias_table_probe(&alias, root);
        if (slot->head == NULL) {
            slot->head = pv;
        } else {
            link_into_alias_chain(slot->head, pv);
        }

        if (failed(compute_modulo_lifetime(pv))) return failure();
    }

    return success();
}
```

The alias-table probe uses the same DenseMap shape every scheduler intern table uses: hash `(root>>9) ^ (root>>4)` against a power-of-two capacity, stride-1 linear probe, and the canonical `-4096` / `-8192` sentinels in slot byte 0 (see [Container Fingerprints — LLVM DenseMap and DenseSet](../mlir-infra/container-fingerprints.md#llvm-densemap-and-denseset)). Phase 4 reads the resulting chains to decide which pipeline values are eligible for buffer sharing — two values that share an alias chain trivially share storage.

A lifetime that resists normalisation — a cyclic producer chain, a missing iteration anchor, or a producer with no consumer — is fatal. The pass emits `"fails to resolve lifetime"` and aborts before any barrier or buffer is committed.

## Phase 2 — Assign Named Barriers

Phase 2 walks the pipeline-value list and hands each producer/consumer pair one NamedBarrier slot. NamedBarriers are the 32-slot `bar.sync` mechanism per CTA — distinct from the transactional mbarrier object that other pipeline pages discuss. See [mbarrier State Machine](../topics/mbarrier-state-machine.md) for the structural disambiguation. The slot index is encoded as a small integer that the later materializer turns into a `bar.sync` operand.

The 32-slot pool is the binding constraint. The binder maintains a 32-entry table of currently-bound `(stage, cycle)` lifetime ranges, one per slot. For each pipeline value, it scans slots in index order looking first for an unbound slot (the fresh-allocate path), then for a slot whose recorded lifetime does not overlap the candidate's lifetime in steady-state `(stage, cycle)` space (the reuse path). The overlap test is the standard interval check on the modulo-normalised endpoints Phase 1 produced.

```c
LogicalResult assignNamedBarriers(FunctionOpInterface fn) {
    SlotState slots[32] = {0};                             // all slots start unbound

    for (PipelineValue *pv : pipelineValues(fn)) {
        // Fresh-allocate pass: pick the lowest-indexed unbound slot.
        int chosen = -1;
        for (int s = 0; s < 32; ++s) {
            if (!slots[s].bound) { chosen = s; break; }
        }
        // Reuse pass: pick the lowest-indexed slot whose lifetime is disjoint.
        if (chosen < 0) {
            for (int s = 0; s < 32; ++s) {
                if (lifetimes_disjoint(slots[s].lifetime, pv->lifetime)) {
                    chosen = s;
                    break;
                }
            }
        }
        if (chosen < 0) return failure();                  // pool exhausted

        slots[chosen].bound = true;
        slots[chosen].lifetime = merge_lifetimes(slots[chosen].lifetime, pv->lifetime);
        pv->namedBarrier = chosen;
    }
    return success();
}
```

Index-order scanning keeps the allocation stable across builds — two compilations of the same function produce the same slot assignments. Reuse stays correct because `lifetimes_disjoint` works on the modulo-normalised endpoints: two pairs whose live ranges never coexist in the steady state can share one hardware slot without producing a barrier collision.

When neither fresh allocation nor reuse succeeds for some pair, the pass emits `"fails to assign named barrier"` and aborts. The named-barrier index later lands in the `Mutex_` header documented in [Pipe_ and Mutex_ Value-Header Layout](pipe-mutex-value-layout.md).

## Phase 3 — Pick Buffer Class and Bind

Phase 3 decides whether each pipeline value lives in SMEM or TMEM, then dispatches to the matching binder. The SMEM path first selects a region inside the SMEM allocation pool, then assigns an offset within that region. The TMEM path allocates a handle from the TMEM region and writes it into the pipeline-value record.

Buffer-class selection is a deterministic function of the value's shape, element type, and producer/consumer pattern. The class names the storage domain; the producer/consumer pattern picks the correct binder mode within that domain.

```c
BufferClass classify_buffer(const PipelineValue *pv) {
    Shape s = pv->tile_shape;
    Type  e = pv->element_type;

    // Subtarget gate: without the Blackwell tmem feature there is no TMEM domain.
    if (!subtarget_has(TMEM_FEATURE)) {
        return SMEM;
    }

    // Tile-shaped values with byte-element types and a footprint above the
    // TMEM threshold land in TMEM; everything else stays in SMEM.
    bool tile_shaped   = s.rank >= 2 && shape_is_2d_tile(s);
    bool byte_elements = element_bits(e) >= 8;
    size_t footprint   = shape_bytes(s, e);

    if (tile_shaped && byte_elements && footprint > TMEM_FOOTPRINT_THRESHOLD) {
        return TMEM;
    }
    return SMEM;
}
```

The threshold reflects Blackwell's TMEM geometry. TMEM is the high-capacity tile store and is too coarse for sub-tile or small-element traffic, so anything that is not a full byte-element tile drops back to SMEM. The Blackwell `tmem` subtarget feature is the gate documented in [NVPTX Subtarget and Feature Matrix](../codegen/nvptx-subtarget-and-feature-matrix.md); subtargets without it collapse the classifier to SMEM-only.

Once the class is fixed, the binder allocates a per-value record. The record carries the producer-op pointer, the variadic list of consumer-op pointers, the buffer-class enum (SMEM, TMEM, or named-barrier-only), the SMEM byte offset or TMEM handle, the named-barrier index from Phase 2, the steady-state stage count, and the `(stage, cycle)` lifetime endpoints. TMA descriptor traffic also lands in this record; the TMA path is documented in [TMA, Tensormap and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md).

A binder failure is fatal. The pass emits `"fails to assign smem buffer"` or `"fails to assign tmem buffer"` and aborts. Common causes are SMEM exhaustion at the chosen stage count, an oversize tile that exceeds the TMEM region, or an alignment requirement that cannot be satisfied at the candidate offset.

## Phase 4 — Share Buffers

Phase 4 pools pipeline values into shared physical buffers. The pool is a union-find keyed on pipeline-value identity; each equivalence class names one physical buffer. Two pipeline values qualify to merge when their buffer class, element type, and footprint agree exactly and their `(stage, cycle)` lifetimes are disjoint in the steady-state schedule. Buffer-class agreement is the legality gate; lifetime disjointness is the correctness gate.

The lifetime overlap test mirrors Phase 2's: a merged class records the union of its members' live ranges, and a new member joins only when its live range stays disjoint from that union. That keeps the merge associative — merging `(a, b)` and then `(ab, c)` produces the same outcome as merging `(b, c)` first.

```c
void sharePipelineBuffers(FunctionOpInterface fn) {
    UnionFind uf = uf_init(pipelineValueCount(fn));

    for (auto [a, b] : candidatePairs(fn)) {
        if (a->bufferClass   != b->bufferClass)   continue;     // legality gate
        if (a->element_type  != b->element_type)  continue;
        if (a->footprint     != b->footprint)     continue;

        Lifetime la = uf_class_lifetime(&uf, a);
        Lifetime lb = uf_class_lifetime(&uf, b);
        if (!lifetimes_disjoint(la, lb))          continue;     // correctness gate

        uf_union(&uf, a, b);
        emit("share pipeline buffer");
    }
}
```

Each successful merge emits the diagnostic `"share pipeline buffer"`. Failures here are not fatal — an unmerged pipeline simply keeps its own buffer. Phase 4 exists to recover SMEM and TMEM capacity in deep pipelines, where the modulo scheduler can produce many pipeline values whose lifetimes never actually coexist at any one cycle.

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
