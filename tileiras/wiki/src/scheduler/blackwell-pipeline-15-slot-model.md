# Blackwell Pipeline 15-Slot Model

## Abstract

The Blackwell pipeline model is the target-machine resource vocabulary that drives Tileiras scheduling. It maps scheduled operations onto issue, transport, tensor-memory, shared-memory, and MMA resource slots. Those slots become row bits in the modulo scheduler's Resource Reservation Table, so a candidate operation can be seated only when its footprint does not overlap resources already claimed in the same modulo cycle.

The model defines 24 slot identifiers. Eight are coarse families used for classification and grouping; fifteen primary slots feed the optimized scheduler's fine-grained pressure model; the remaining identifiers cover catch-all and test classes.

## Slot Identifiers

Slot identifiers are one-based. The RRT row bit for a slot is `slot_id - 1`.

| Slot | Name | Kind | Role |
|---:|---|---|---|
| 1 | `issue` | coarse | generic issue family |
| 2 | `xu` | coarse | transcendental or special-function unit family |
| 3 | `xu64` | coarse | 64-bit special-function variant |
| 4 | `fp32x2_fp16ultra` | fine | paired FP32x2 and packed FP16 issue |
| 5 | `alu` | coarse | scalar ALU family |
| 6 | `alu_or_fmaheavy` | fine | ALU or FMA-heavy issue group |
| 7 | `dual_alu` | fine | dual-ALU datapath |
| 8 | `lsu` | coarse | load/store family |
| 9 | `tmem` | coarse | tensor-memory family |
| 10 | `mma` | coarse | MMA family |
| 11 | `tc_and_mma` | fine | TensorCore and legacy MMA issue stage |
| 12 | `tma` | coarse | tensor memory accelerator family |
| 13 | `tp_gnic_rd` | fine | generic interconnect transport read |
| 14 | `tp_gnic_wr` | fine | generic interconnect transport write |
| 15 | `tp_smem_rd` | fine | shared-memory transport read |
| 16 | `tp_smem_wr` | fine | shared-memory transport write |
| 17 | `tp_tmem_rd` | fine | tensor-memory transport read |
| 18 | `tp_tmem_wr` | fine | tensor-memory transport write |
| 19 | `tp_mma` | fine | MMA transport |
| 20 | `unknown` | fine | unclassified fallback |
| 21 | `omitted_simt` | fine | deliberately omitted SIMT operation |
| 22 | `test_simt` | fine | scheduler self-test SIMT row |
| 23 | `test_mma` | fine | scheduler self-test MMA row |
| 24 | `test_dma` | test | scheduler self-test DMA row |

The fifteen primary fine slots are:

```text
fp32x2_fp16ultra
alu_or_fmaheavy
dual_alu
tc_and_mma
tp_gnic_rd
tp_gnic_wr
tp_smem_rd
tp_smem_wr
tp_tmem_rd
tp_tmem_wr
tp_mma
unknown
omitted_simt
test_simt
test_mma
```

Tensor-memory read and write slots are the clearest Blackwell markers — tcgen05-style tensor-memory load, store, copy, and MMA paths all hinge on them.

## Operation Footprints

Each scheduled operation has:

- a slot identifier or slot group;
- a duration in cycles;
- a row footprint describing which resources it occupies at each cycle;
- optional capacity pressure in a pool that cannot be represented by one bit.

```c
typedef struct ScheduledResourceUse {
    uint32_t slot_id;
    uint32_t duration;
    uint64_t rows[MAX_DURATION];
} ScheduledResourceUse;

uint64_t resource_row_bit(uint32_t slot_id) {
    return 1ull << (slot_id - 1);
}
```

The RRT probes the footprint. Dependency and cost calculation read the latency. The two concepts are related but distinct: a long-latency value can occupy an issue slot for only a moment, while a transport operation can hold transport resources across several cycles.

## Latency Families

The model groups operations into latency families that match the scheduler's rough machine model.

| Family | Typical latency | Typical slots |
|---|---:|---|
| dual ALU | 2 cycles | `dual_alu` |
| ordinary ALU or FMA-heavy | 4 cycles | `alu_or_fmaheavy`, `fp32x2_fp16ultra` |
| shared-memory or tensor-memory transport | 7 cycles | `tp_smem_*`, `tp_tmem_*`, `tp_gnic_*`, `tp_mma` |
| TensorCore or MMA issue | 8 cycles | `tc_and_mma` |
| far memory or cross-cluster anchors | thousands of cycles | modeled as scheduling anchors, not ordinary row duration |

Treat these values as scheduling weights — not a complete microarchitectural latency table, and never source-language semantics.

## Capacity Pools

Some resources are modeled by capacities in addition to row bits. The important capacity pools are:

| Pool | Meaning |
|---|---|
| ALU-or-FMA-heavy issue width | limits how many FMA-heavy operations can be admitted in one cycle |
| dual-ALU issue width | limits dual-ALU pressure |
| shared-memory byte budget | constrains shared-memory allocation and spill pressure |
| tensor-memory budget | constrains tensor-memory-backed operations |
| register-bank pairing | models paired register-bank pressure |
| transport singleton slots | keep shared, tensor, and interconnect transports from overbooking |

A debug configuration treats shared memory as effectively unbounded. It isolates whether a scheduling failure comes from shared-memory pressure or from a different resource.

The capacity-pool probe mirrors the row-bit check: count current usage at the modulo cycle, add the candidate's requested count, compare against the cap. Pools with cap `1` behave as singleton resources — the second op claiming the same pool in the same cycle is rejected outright.

```c
bool capacity_pools_allow(const ResourceTable *table,
                          const ScheduledResourceUse *use,
                          uint32_t t) {
    for (uint32_t k = 0; k < use->duration; ++k) {
        uint32_t row = (t + k) % table->ii;
        for (uint32_t p = 0; p < table->n_pools; ++p) {
            uint32_t requested = use->pool_counts[p];
            if (requested == 0) continue;
            if (table->pool_usage[row][p] + requested > table->pool_caps[p]) {
                return false;
            }
        }
    }
    return true;
}
```

## Rau Cost Weight Tables

Six lookup tables anchor the cost model. The placement driver `sub_981D50` and its four arms consult them before reserving a slot in the global RRT. The tables do not live in a single rodata blob — they split across the forward-walk seeder `sub_12C8DF0`, the backward-walk variant `sub_12CBDD0`, the dispatcher `sub_12CEBF0`, and the slot-id hashmap builder `sub_12CF910`. Three tables hold per-op latencies, two hold per-slot cycle anchors, and one holds the per-resource-class capacities. The placement arms read these tables through stable offsets into the 444-byte `SchedulerResourcePool` and through XMM-word loads from rodata at `0x4CC9980..0x4CC9D70`.

### Per-Op Latency Table

A contiguous 12-byte stride array seeded by `sub_12C8DF0` starting at offset 0 of the resource pool holds the per-op latency table. It carries 23 entries laid out as `{u32 latency; u32 op_tag; u32 reserved}` covering offsets 0..395. Each entry pairs a Blackwell op tag (the dialect's internal opcode classifier) with the cycles the cost model charges for a single issue of that tag. The per-op latency assigner reads this table to fill in `Op.latency` for every candidate before the cost-sort runs.

| Offset | Latency | Op Tag | Slot | Family |
|---:|---:|---:|---:|---|
| `+0` | 4 | `0x0B` | 6 | FMA-heavy |
| `+12` | 2 | `0x0C` | 7 | dual ALU |
| `+24` | 4 | `0x09` | 6 | FMA-heavy |
| `+36` | 4 | `0x0A` | 6 | FMA-heavy |
| `+48` | 4 | `0x0D` | 4 | paired FP32x2 / FP16 |
| `+60` | 4 | `0x0E` | 4 | paired FP32x2 / FP16 |
| `+72` | 4 | `0x0F` | 4 | paired FP32x2 / FP16 |
| `+84` | 4 | `0x10` | 4 | paired FP32x2 / FP16 |
| `+208` | 2 | `0x03` | 7 | dual ALU |
| `+228` | 2 | `0x01` | 7 | dual ALU |
| `+240` | 2 | `0x02` | 7 | dual ALU |
| `+252` | 4 | `0x15` | 6 | FMA-heavy |
| `+264` | 4 | `0x16` | 6 | FMA-heavy |
| `+276` | 4 | `0x17` | 6 | FMA-heavy |
| `+288` | 7 | `0x1C` | 15 / 16 | SMEM transport |
| `+300` | 7 | `0x1E` | 17 | TMEM read transport |
| `+312` | 7 | `0x1D` | 16 | SMEM write transport |
| `+324` | 7 | `0x1F` | 18 | TMEM write transport |
| `+336` | 8 | `0x18` | 11 | TC+MMA issue |
| `+348` | 8 | `0x19` | 11 | TC+MMA issue |
| `+360` | 7 | `0x20` | 19 | MMA transport |
| `+372` | 7 | `0x21` | 13 | gnic read transport |
| `+384` | 7 | `0x22` | 14 | gnic write transport |

High tag ids that the linear stride cannot reach (`0x11..0x1B` plus a handful of secondary tags) live in XMM-word continuations at rodata `0x4CC9980..0x4CC9A10` for the forward walk and `0x4CC9A20..0x4CC9AE0` for the backward walk. Each XMM word packs two `{lat, op_tag}` pairs into four `i32` lanes; the backward-walk table mirrors the forward-walk encoding but carries reverse-dataflow weights consumed by `sub_12CBDD0`.

### Cycle Anchor Table

Rodata `0x4CC9D10..0x4CC9D70` holds the cycle anchor table — per-slot cycle weights that fix the stage-start cycle every candidate seat time must clear before its slot stays admissible. The slot-cycle-weight reader `sub_12CBDD0` consults this table during the dispatcher pass and enforces two architectural ceilings: 5000 cycles for HBM3e bandwidth saturation, 7000 cycles for cross-cluster transfers. Both ceilings land as inline 3-word vectors in `self[16]` and `self[20]` of the resource pool. Secondary fence anchors at 1600 and 2000 cycles cover near-SM SMEM spill and intra-cluster fences.

| Rodata | Slot Range | Cycle Weights |
|---|---|---|
| `0x4CC9D10` | 1..4 (issue, xu, xu64, fp32x2_fp16ultra) | 100, 100, 110, 101 |
| `0x4CC9D20` | 5..8 (alu, alu_or_fmaheavy, dual_alu, lsu) | 102, 102, 103, 103 |
| `0x4CC9D30` | 9..12 (tmem, mma, tc_and_mma, tma) | 120, 104, 121, 104 |
| `0x4CC9D40` | 13..16 (gnic rd/wr, smem rd/wr) | 200, 400, 800, 900 |
| `0x4CC9D50` | 17..20 (tmem rd/wr, mma transport, unknown) | 1500, 2000, 2400, 3000 |
| `0x4CC9D60` | misc (test_* and `omitted_simt` scratch) | 50, 100, 200, 360 |
| `0x4CC9D70` | secondary anchors | 600, 800, 1000, 1200 |

The 5000-cycle HBM3e ceiling is the absolute round-trip budget the scheduler attributes to a worst-case far-memory dependence; the 7000-cycle ceiling is the same budget for TMA traffic that crosses the cluster boundary. Both serve as Big-M terms — every candidate's accumulated latency must stay below them or the placement is rejected outright before any RRT probe runs.

### Pool Capacity Vector

A 9-element capacity vector `{37, 4, 7, 37, 5, 1, 3, 6, 2}` tells the per-cycle pressure summariser `sub_12CEBF0` how much of each resource is available in a single modulo cycle. Pool construction installs the vector through nine explicit calls to the capacity rounder `sub_12BB050`.

| Pool | Capacity | Role |
|---:|---:|---|
| 0 | 37 | op-tag → latency table, first 37 distinct op tags |
| 1 | 4 | ALU-or-FMA-heavy issue cap |
| 2 | 7 | `dual_alu` slot fan-out |
| 3 | 37 | shadow of pool 0 for backward-walk |
| 4 | 5 | per-slot issue-width for coarse families |
| 5 | 1 | singleton global lock for SMEM capacity |
| 6 | 3 | dual-issue cap |
| 7 | 6 | `alu_or_fmaheavy` slot fan-out |
| 8 | 2 | register-bank pairing |

Caps of 1 on transport pools and the SMEM byte budget are what make the `tp_smem_*`, `tp_tmem_*`, `tp_gnic_*`, and `tp_mma` slots behave as singleton resources — the modulo scheduler rejects any second op claiming the same transport row in the same RRT cycle. The capacity rounder rounds each request up to the next power of two times four-thirds before allocation, so the rodata values are the live counts before rounding.

### Tier-2 Global Capacity Struct

`sub_12C8DF0` writes a small struct at the same resource pool that holds five hard inequalities every committed schedule must respect. The struct lives at the start of the pool's secondary section and encodes per-tag caps as packed `u64` words.

| Offset | Op Tag | Cap | Interpretation |
|---:|---:|---:|---|
| `ptr[ 0]` | 2 | 262144 | TMEM / register-file byte budget |
| `ptr[ 8]` | 1 | 3 | max concurrent ALU issue per warp slot |
| `ptr[16]` | — | 232448 or `INT_MAX` | SMEM byte budget per SM |
| `ptr[20]` | 1 | 4 | max concurrent ALU-or-FMA-heavy issue |
| `ptr[28]` | 8 | 2048 | register-bank width across 8 banks |

The SMEM byte budget at `ptr[16]` is the 227 KiB Blackwell floor (232448 bytes). Setting `TILE_AS_DEBUG_UNLIMITED_SMEM="1"` toggles this cell to `INT_MAX`, letting the developer isolate whether a scheduling failure comes from SMEM pressure or from another resource. The check runs once at pass-init time inside `sub_12C8DF0`; later admission attempts read the rewritten cell directly.

### Cost Table Consumers

Each of the three readers pulls from a single table and produces one class of cost-model input. The split keeps the per-op latency view, the per-slot cycle anchor view, and the per-class capacity view independently addressable from both placement arms and the cost reducer.

| Cost lookup table | Rodata / Offset | Consumer | Role |
|---|---|---|---|
| Per-op latency, 23 packed entries | `SchedulerResourcePool +0..+395` | `sub_12C8DF0` | per-op latency assigner; fills `Op.latency` |
| Forward-walk XMM continuations | `0x4CC9980..0x4CC9A10` | `sub_12C8DF0` | high-tag latency lookups (tags `0x11..0x1B`) |
| Backward-walk XMM continuations | `0x4CC9A20..0x4CC9AE0` | `sub_12CBDD0` | reverse-dataflow latency view |
| Per-slot cycle anchor weights | `0x4CC9D10..0x4CC9D70` | `sub_12CBDD0` | slot-cycle-weight reader; applies 5000/7000 ceilings |
| 9-element pool capacity vector | inline arguments to `sub_12BB050` | `sub_12CEBF0` | per-cycle pressure summariser |
| Tier-2 global capacity struct | `SchedulerResourcePool +0..+28` | `sub_12C8DF0` | installs hard inequalities (TMEM, ALU, SMEM, regbank) |

The cost reducer that drives `CostBasedScheduleGenerator::generateOrRefineScheduleWithConstraint` (`sub_980290`) reaches all three views through the same resource-pool pointer, so the lexicographic cost vector it produces — latency, slot pressure, structural distance — comes from a single coherent snapshot of the tables.

## Axis and Buffer Inputs

Names alone do not classify operations. The scheduler consumes analysis facts:

- contiguity, divisibility, and constancy from axis analysis;
- scalar bounds and memory ranges for index expressions;
- buffer lifetime records for shared memory, tensor memory, and auxiliary scratch;
- leader groups and pipeline identifiers from buffer assignment;
- allocation sizes and live ranges from the layout and buffer passes.

Axis analysis decides whether a vector load, TMA coordinate, or pointer expression is aligned and compact enough for a particular resource class. Buffer lifetime decides whether two memory operations share a live resource and must be coupled or separated.

## Worked Example: Four-Op Loop Body

The clearest way to read the slot model is to walk a loop body small enough to fit in one RRT and rich enough to touch the transport, MMA, and SMEM rows simultaneously. The body below is the steady-state shape of a software-pipelined matmul inner loop:

```text
%0 = nv_tileas.async.tiled_tma_load %desc, %coord : !smem_ref
%1 = nv_tileas.async.smem_write     %src        : !smem_ref
%2 = nv_tileas.async.wgmma          %a, %b, %c  : !tmem_ref
%3 = nv_tileas.async.smem_read      %0          : !reg
```

Each op's resource vector is the triple `(slot_id, duration, occupancy)` produced by the constraint builder. The classifier reads the op's MLIR opcode plus its operand types, picks the slot from the table at the top of this page, and reads the duration from the latency family.

| Op | Slot | Duration | Occupancy | Family |
|---|---|---:|---:|---|
| `tiled_tma_load %0` | 12 (`tma`) + 16 (`tp_smem_wr`) | 8 cycles | 1 each | TMA + SMEM write transport |
| `smem_write %1` | 16 (`tp_smem_wr`) | 7 cycles | 1 | SMEM write transport |
| `wgmma %2` | 11 (`tc_and_mma`) + 19 (`tp_mma`) | 8 cycles | 1 each | MMA issue + transport |
| `smem_read %3` | 15 (`tp_smem_rd`) | 7 cycles | 1 | SMEM read transport |

The TMA load is the only op that claims two slots simultaneously: the descriptor stays parked on the `tma` row while the tensor payload flows through the SMEM write transport. The cost reducer sees two row contributions for one op, which is why the per-op latency table at offset `+288` of the resource pool charges both `0x1C` (SMEM transport) and `0x1D` (SMEM write transport) variants for the same source-level operation.

Suppose the candidate `II` is `8`. The scheduler probes the four ops in dataflow order and seats each at the earliest legal cycle. The resulting RRT — one 24-bit row per modulo cycle, drawn here only over the slots the example touches — is:

```text
cycle  tc_and_mma  tma  tp_smem_rd  tp_smem_wr  tp_mma
  0       .         X       .           X         .      ← tiled_tma_load occupies tma + smem_wr
  1       .         X       .           X         .
  2       .         X       .           X         .
  3       .         X       .           X         .
  4       .         X       .           X         .
  5       .         X       .           X         .
  6       .         X       .           X         .
  7       .         X       .           X         .

  // smem_write seats at cycle 0 of next iteration; in the modulo
  // view it overlays the same RRT, claiming tp_smem_wr at cycles
  // [0..6] mod 8. The probe fails — tp_smem_wr is already busy.
  //
  // The placement driver bumps smem_write forward; the only legal
  // start is cycle 8 mod 8 = 0 of the iteration *after* the TMA
  // tail drains, which the modulo scheduler models as a stage-1
  // seat with order 0.
```

The example shows two things at once: (i) singleton transports (`tp_smem_wr` pool cap = 1) force the modulo scheduler to spread overlapping iterations across stages rather than packing them onto the same cycle, and (ii) the per-op latency table's split between `0x1C` and `0x1D` exists precisely so the TMA load and the loose SMEM write can be charged at different per-cycle weights — the TMA load's 8-cycle hold is what makes it the structural bottleneck, while the SMEM write's 7-cycle hold lets it slip into the gap one cycle later.

The cost reducer ranks this schedule against any alternative by reading the per-slot cycle weights from rodata `0x4CC9D40` for slots 13..16: `200` for gnic-rd, `400` for gnic-wr, `800` for smem-rd, `900` for smem-wr. A schedule that doubled-up on `tp_smem_wr` would multiply that `900` by the second user's surcharge; a schedule that kept the SMEM transports balanced pays the base weight once and clears the gate.

## Admission Rule

An operation is legal at cycle `t` when every occupied row is conflict-free.

```c
bool resource_admit(ResourceTable *table,
                    const ScheduledResourceUse *use,
                    uint32_t t) {
    for (uint32_t k = 0; k < use->duration; ++k) {
        uint32_t row = (t + k) % table->ii;
        if ((table->rows[row] & use->rows[k]) != 0) {
            return false;
        }
    }

    return capacity_pools_allow(table, use, t);
}
```

Commit is the same loop with OR assignment after all probes pass.

## Cross-References

[Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md) consumes the slot identifiers documented here as row bits in its qword footprint stack. [Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) drives the RRT probe and commit against these slots. [Modulo Driver and 4-Arm OR-Chain](modulo-driver-or-chain.md) consults the per-op latency table and the 9-element pool-capacity vector during cost ranking. [Schedule Solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) reads the per-pool caps `4` (TMEM) and `3` (named-barrier) from indices 1 and 6 of the pool-capacity vector. [Performance and Cost Model](../topics/performance-and-cost-model.md) walks the roofline calculation that turns these slot costs into a stage count.
