# Resource Enum

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The TPU cost model models a VLIW bundle as a vector of functional-unit occupancies. `ResourceVector` is a flat array of 23 `double` slots, one per abstract hardware resource: the two MXU pipes (matmul-issue and gain-push), the cross-lane unit, the two-plus-one vector ALU lanes, the vector EUP / load / store ports, four memory-transfer terms, six ICI ring links, and three SparseCore engines. Each LLO instruction deposits its throughput cycles into the slot named by `CycleTable::GetResource`, and the bundle's issue cost is a structured reduction — `MaxResourceCycles` — over the whole vector. This page recovers the enum value→name table, the per-gen slot counts, and the reduction's overlap model.

The reader should not confuse this with the scheduler's `ResourceType` enum. They are two different "resource" abstractions in the same binary, and conflating them is the central trap. The cost-model `Resource` (this page, 23 slots, an enum nested in `ResourceVector`) measures **cycle weight** — how many cycles a unit is busy. The scheduler `ResourceType` ([ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md), 47 IDs, an `AsyncTracker` enum) measures **concurrency limits** — how many async collectives of a kind may co-issue. They overlap semantically (both name the six ICI links), but they are distinct enums with distinct value spaces, distinct print paths, and distinct consumers. This page documents the cost-model `Resource`; the contrast with `ResourceType` is drawn at the end.

The names come from one place: `ResourceVectorToString` @ `0x1c89bde0` reads the 22 leading doubles in physical-offset order and formats them with a single `printf` template, so the string order *is* the enum order. The 23rd slot (`R[22]`) is a real, `Acc`-writable, `MaxResourceCycles`-read slot, but `ToString` never prints it — it has no name string in the binary.

For reimplementation, the contract is:

- The 23-slot `Resource` enum value→name→offset table and the `Acc` bound that fixes the count at 23.
- The `MaxResourceCycles` reduction: which slots overlap (plain MAX), which blend at 50% (the vector-ALU port balance), and which serialize (the memory group).
- The per-gen slot counts (PF=20, VF=28, GL=31 from named `kResources` symbols; a fourth 31-slot table is present but its gen attribution is unconfirmed) and how the wider gens extend the enum past `R[22]`.
- The gen-invariant `CycleTable::GetResource` op→slot table.
- The boundary between this enum and the scheduler's `ResourceType` enum.

| | |
|---|---|
| **Enum** | `xla::jellyfish::ResourceVector::Resource` (nested) |
| **Slot count** | 23 (`Acc` bound `< 0x17`); `ToString` names 22 |
| **Vector layout** | 23 × `double` (`+0x00..+0xb0`) + operand-bytes map `+0xb8` + output-bytes map `+0xd8` |
| **Name source** | `ResourceVectorToString` @ `0x1c89bde0` (format string @ `0x87f40ea`) |
| **Deposit** | `ResourceVector::Acc(Resource, double)` @ `0x1c89adc0` |
| **Reduction** | `ResourceVector::MaxResourceCycles()` @ `0x1c89b9e0` |
| **Op→slot map** | `CycleTable::GetResource(Instruction)` @ `0x1c89ce20` (table @ `0xb438aec`) |
| **Per-gen counts** | PF=20, VF=28, GL=31 (named symbols); GF=31 (inferred, UNVERIFIED) (`kResources` permutations) |

---

## The 23-Slot Resource Enum

### Names

`ResourceVectorToString` @ `0x1c89bde0` feeds the 22 leading doubles into the format string at `.rodata 0x87f40ea` in physical-offset order. The decompile confirms the full template byte-for-byte:

```text
RV[Matpush: %.0f, Matmul: %.0f, Xlu: %.0f, VectorAlu0: %.0f, VectorAlu1: %.0f,
   VectorAluAny: %.0f, VectorEup: %.0f, VectorLoad: %.0f, VectorStore: %.0f,
   MemXferInputLatency: %.0f, MemXferInputBandwidth: %.0f, MemXferOutputLatency: %.0f,
   MemXferOutputBandwidth: %.0f, IciYPlus: %.0f, IciYMinus: %.0f, IciXPlus: %.0f,
   IciXMinus: %.0f, IciZPlus: %.0f, IciZMinus: %.0f, ScScs: %.0f, ScTile: %.0f,
   ScCollective: %.0f]
```

Since each `double` is an 8-byte slot read in order, the enum value equals the array index equals `offset / 8`:

| idx | offset | name | functional unit | Confidence |
|---|---|---|---|---|
| R[0] | +0x00 | `Matpush` | MXU gain/latch-push (matpush) pipe | CERTAIN |
| R[1] | +0x08 | `Matmul` | MXU matmul-issue (matprep) pipe | CERTAIN |
| R[2] | +0x10 | `Xlu` | cross-lane unit (matres read / EUP / transcendental staging) | CERTAIN |
| R[3] | +0x18 | `VectorAlu0` | vector ALU lane 0 (dedicated) | CERTAIN |
| R[4] | +0x20 | `VectorAlu1` | vector ALU lane 1 (dedicated) | CERTAIN |
| R[5] | +0x28 | `VectorAluAny` | vector ALU "any" (load-balanced) lane | CERTAIN |
| R[6] | +0x30 | `VectorEup` | vector extended-precision unit | CERTAIN |
| R[7] | +0x38 | `VectorLoad` | vector load port | CERTAIN |
| R[8] | +0x40 | `VectorStore` | vector store port | CERTAIN |
| R[9] | +0x48 | `MemXferInputLatency` | input-DMA startup-latency term | CERTAIN |
| R[10] | +0x50 | `MemXferInputBandwidth` | input-DMA per-byte bandwidth term | CERTAIN |
| R[11] | +0x58 | `MemXferOutputLatency` | output-DMA startup-latency term | CERTAIN |
| R[12] | +0x60 | `MemXferOutputBandwidth` | output-DMA per-byte bandwidth term | CERTAIN |
| R[13] | +0x68 | `IciYPlus` | ICI ring link +Y | CERTAIN |
| R[14] | +0x70 | `IciYMinus` | ICI ring link −Y | CERTAIN |
| R[15] | +0x78 | `IciXPlus` | ICI ring link +X | CERTAIN |
| R[16] | +0x80 | `IciXMinus` | ICI ring link −X | CERTAIN |
| R[17] | +0x88 | `IciZPlus` | ICI ring link +Z | CERTAIN |
| R[18] | +0x90 | `IciZMinus` | ICI ring link −Z | CERTAIN |
| R[19] | +0x98 | `ScScs` | SparseCore SCS sequencer | CERTAIN |
| R[20] | +0xa0 | `ScTile` | SparseCore tile-execute core | CERTAIN |
| R[21] | +0xa8 | `ScCollective` | SparseCore collective engine | CERTAIN |
| R[22] | +0xb0 | (unnamed; valid) | reserved/scalar — read by `MaxResourceCycles`, written by `Acc`; never printed | MEDIUM |

`ResourceVector::Acc(Resource, double)` @ `0x1c89adc0` bounds-checks `resource >= 0x17` (23) with a trapping `ud1`, then does `vector[resource] += cycles` over an 8-byte stride. So 23 slots are valid (R[0..22]); the print path covers only the first 22. The total vector body is `0xf8` bytes: 23 doubles (`+0x00..+0xb0`), an operand-bytes `flat_hash_map` (`+0xb8`), and an output-bytes `flat_hash_map` (`+0xd8`).

> **GOTCHA —** `R[22]` is real but anonymous. `Acc` accepts it (its bound is `< 23`, not `< 22`) and `MaxResourceCycles` reads `[+0xb0]` in the plain-MAX group, yet `ResourceVectorToString` stops at 22 arguments. A reimplementer who sizes the vector at 22 from the printf template alone will buffer-overrun on the first deposit into slot 22 and silently drop a cycle term in the reduction. Size it at 23. Its semantic role (likely a scalar-sequencer / reserved count) has no string evidence (MEDIUM).

### The MaxResourceCycles Reduction

`MaxResourceCycles` @ `0x1c89b9e0` is not a flat maximum — it is three rules layered over the slots, and the layering is the heart of the cost model's overlap assumptions:

```c
function MaxResourceCycles(rv):                     // sub_1C89B9E0
    a = rv[3]; b = rv[4]; c = rv[5]                 // VectorAlu0, VectorAlu1, VectorAluAny
    if c > 0:                                        // (A) 50% port-balance blend
        if a > b: d = min(a-b, c); c -= d; b += d    //   fill the less-busy dedicated lane
        elif b > a: d = min(b-a, c); c -= d; a += d
        c *= 0.5                                     //   const @0xa2df5c8
        a += c; b += c
    vec_alu = max(a, b)
    mem = rv[9] + rv[10] + rv[11] + rv[12]           // (B) MemXfer serial sum
    acc = max(vec_alu, mem)
    for r in {0,1,2,6,7,8, 13..22}:                  // (C) plain MAX (independent units overlap)
        acc = max(acc, rv[r])
    return acc
```

Three groups:

- **(A) 50% blend on `{R[3], R[4], R[5]}`** — the two-lane vector ALU. "Any"-lane work (`R[5]`) load-balances across the two dedicated lanes (`R[3]`, `R[4]`); the residual that cannot balance perfectly overlaps at 50% (the `0.5` constant). This models a 2-lane ALU, not an XLU blend.
- **(B) serial sum on `{R[9..12]}`** — the memory subsystem. Input latency + input bandwidth + output latency + output bandwidth **add**: a transfer's startup, payload, the store's startup, and its payload happen in sequence.
- **(C) plain MAX over everything else**, including `R[0]=Matpush`, `R[1]=Matmul`, and `R[22]`.

> **QUIRK —** `Matpush` and `Matmul` are in the **plain-MAX** group, so back-to-back MXU operations in one bundle **overlap** (the MXU is pipelined). The only thing that serializes is the memory transfer (`R[9..12]`). A naive cost model that serializes matmuls will over-cost MXU-bound bundles by a large factor on every generation. Trust the MAX.

### Add / Scale — the Amortized-DMA Model

Two more reductions handle loop scaling. `Add` (@ `0x1c89b820`) MAX-combines the two DMA *latency* slots `{R[9], R[11]}` while every other slot ADDs — you pay a DMA startup once across combined sub-vectors, but bytes-moved accumulates. `ScaleResource` (@ `0x1c89b6a0`) skip-scales those same two latency slots when multiplying a per-iteration bundle by a trip count: bandwidth and compute scale linearly, DMA startup does not. The result is a clean amortized-startup model — latency paid once, bandwidth paid per iteration — which the loop emitter exploits via `GetSubset` (a prologue subset that keeps only the input-DMA slots, a tail subset that keeps compute + output-DMA). See [Bundle-Aware Cost](bundle-aware-cost.md) for the loop composition.

---

## Op→Slot Mapping — `CycleTable::GetResource`

Each LLO instruction's throughput cycles are deposited into one named slot. The op→slot map is a single **gen-invariant** flat-table lookup — `CycleTable::GetResource(Instruction)` @ `0x1c89ce20` is literally `return table[Instruction]` over `.rodata 0xb438aec` (4 bytes/entry), not a per-gen virtual. The `Instruction` value is the ~33-bucket collapse of the LLO opcode space ([CycleTable Family](cycletable-family.md)).

| `CycleTable::Instruction` family | example buckets | → `Resource` slot |
|---|---|---|
| matprep (matmul-issue) | 0..4 (bf16 / fp8 / int8 matprep) | R[1] `Matmul` |
| gain-latch / push | 5..16 (bf16/int4/fp8 latches, incl. transposed) | R[0] `Matpush` |
| matrix-result read + EUP + sin/cos | 23, 27, 28, 29, 30, 31 (matres TC/0/1, eup0/1, sincos) | R[2] `Xlu` |
| input rotate (RotIn/RotOut) | 18, 19 | R[4] `VectorAlu1` |
| shuffle / permute | 20 | R[3] `VectorAlu0` |
| broadcast / reduce / cross-lane / tan | 21, 22, 25, 32 | R[5] `VectorAluAny` |
| extended-precision / lane-compare | 17, 24, 26 | R[6] `VectorEup` |

`RecordHloCycles` @ `0x130bbfe0` ties it together: `r = GetResource(Instruction)` (the named slot, gen-invariant), `c = vtable[+0x10](Instruction)` (per-gen `GetCyclesForThroughput`), then `Acc(r, c)`. Memory ops bypass this table — the `cost_model_util::RecordMemXfer*` family deposits directly into `R[9..12]`.

> **NOTE —** the rotate/shuffle/broadcast/reduce family fans across exactly the three `VectorAlu` slots that the `MaxResourceCycles` 50%-blend group reduces (`R[3]/R[4]/R[5]`). That is by design: shuffle pins `VectorAlu0`, rotate pins `VectorAlu1`, and the symmetric "any" ops (broadcast/reduce/cross-lane/tan) go to `VectorAluAny` so the blend can load-balance them.

---

## Per-Gen Slot Counts

The 23-slot enum is the TensorCore `ResourceVector` print surface, but the per-gen `Performance::GetResources()::kResources` arrays declare how many slots each gen actually populates — and in what iteration order. Each `kResources` is a 1-byte-per-entry **permutation** of the contiguous range `[0..N-1]`, with `N` = the per-gen resource count. The first three rows are byte-anchored to named symbols (`nm -C` resolves each address to a `…Performance::GetResources() const::kResources` symbol); the decoded bytes are permutations of exactly `[0..N-1]`:

| Gen | `kResources` address | slot count `N` | extends past R[22]? | Confidence |
|---|---|---|---|---|
| Pufferfish | `0xb43cd94` (named `PufferfishPerformance` symbol) | 20 | no (no SparseCore / extra-ICI slots) | CERTAIN |
| Viperfish | `0xb43cda8` (named `ViperfishPerformance` symbol) | 28 | yes (adds ICI / Sc sub-slots) | CERTAIN |
| Ghostlite | `0xb43cdc4` (named `GhostlitePerformance` symbol) | 31 | yes | CERTAIN |
| 6acc60406 (GF) | `0xb43cde3` (unnamed; inferred) | 31 | yes | UNVERIFIED |

Pufferfish's 20-slot permutation never references `R[20]`/`R[21]`/`R[22]` — it has no SparseCore or extra-ICI resources. The wider gens (VF/GL) extend the enum past `R[22]` with additional ICI sub-links and SparseCore sub-units; those high-index slots have **no name** in the TensorCore `ResourceVectorToString` (it only prints 22). Naming them would require a SparseCore-side print path that this build does not expose through the TensorCore estimator (LOW / not recovered).

> **UNVERIFIED —** the fourth row (`0xb43cde3`) is **not** a symbol-anchored gen. `nm` resolves exactly three `Performance::GetResources()::kResources` symbols (PF/VF/GL); the next named symbol after Ghostlite is `kProgramSharedRegistryInitialized` @ `0xb43ce10`. The 31 bytes at `0xb43cde3` are a real second permutation of `[0..30]` (immediately following Ghostlite's 31-byte table, both under the single Ghostlite symbol — i.e. `kResources` is a `[2][31]` array Ghostlite indexes by a sub-version discriminant), but no symbol or `TpuVersion` reference in this build attributes it to 6acc60406 / TPU7x. The 6acc60406 label is a plausible inference from the adjacency and slot count, not a recovered fact.

---

## Boundary — Cost-Model `Resource` vs Scheduler `ResourceType`

The binary has a second, larger "resource" enum that a reimplementer will inevitably meet: `ResourceType`, the `LatencyHidingScheduler`'s `AsyncTracker` taxonomy. They are not the same enum and must not be merged.

| | Cost-model `Resource` (this page) | Scheduler `ResourceType` |
|---|---|---|
| Enum | `ResourceVector::Resource` | `AsyncTracker` `ResourceType` |
| Values | 23 (R[0..22]) | 47 (base {0..12} + target {13..46}) |
| Measures | cycle weight (how busy a unit is) | concurrency cap (how many co-issue) |
| Name source | `ResourceVectorToString` @ `0x1c89bde0` | `GetResourceName` @ `0x13616500` (base) / `0x10fff420` (TPU) |
| Consumer | `MaxResourceCycles` → bundle issue cost | `GetNumAvailableResources` → co-issue throttle |
| Per-op map | `CycleTable::GetResource` (LLO opcode → slot) | `GetResourceTypeForOp` (HLO opcode → rt) |

The two enums overlap on one physical concept — the six ICI ring links. The cost model deposits *cycle weight* into `R[13..18]` (`Ici{Y,X,Z}{Plus,Minus}`); the scheduler caps *concurrent issue* of the same links via `ResourceType` ids 14..19 (also `kIci{Y,X,Z}{Plus,Minus}`), bounded by the `xla_tpu_sparse_core_ici_overlap_limit` knob (field 1130). A bundle that uses an ICI link both costs cycles (this model) and consumes a concurrency slot (the other model). The scheduler enum, its producers, and its available-count sources are documented in full on the dedicated [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) page; the contrast is summarized here only to fix the boundary.

> **GOTCHA —** In the `MaxResourceCycles` reduction, the 0.5 blend on `{3,4,5}` is the `VectorAlu0/1/Any` port-balance group (not an XLU blend), and the serial sum on `{9..12}` is the `MemXfer{Input,Output}{Latency,Bandwidth}` memory group (not an MXU group). The MXU pipes `R[0]`/`R[1]` are in the plain-MAX group and overlap, not serialize.

---

## Related Components

| Component | Relationship |
|---|---|
| [CycleTable Family](cycletable-family.md) | Provides `GetResource` (op→slot) and `GetCyclesForThroughput` (slot weight) |
| [Bundle-Aware Cost](bundle-aware-cost.md) | The `GetSubset` / loop composition that consumes `MaxResourceCycles` |
| [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) | The scheduler's distinct 47-ID concurrency-cap enum |
| [Cost Model Overview](overview.md) | The three class families that build the per-gen `Performance` slot data |

## Cross-References

- [Cost Model Overview](overview.md) — the factory dispatch and per-gen class table that produce `ResourceVector` data
- [CycleTable Family](cycletable-family.md) — the `Instruction` bucket enum and the per-gen throughput cycles deposited into slots
- [Bundle-Aware Cost](bundle-aware-cost.md) — `GetSubset` prologue/tail partition and the software-pipelined loop cost
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen cycle values that fill each slot
- [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) — the scheduler's 47-ID `AsyncTracker` resource model (concurrency, not cycle weight)
- [Scheduler Overview](../sched/overview.md) — the `LatencyHidingScheduler` consumer of both resource models
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU opcodes that map to `Matmul` / `Matpush` / `Xlu` slots
