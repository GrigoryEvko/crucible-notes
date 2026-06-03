# ResourceType Taxonomy

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.text`/`.rodata` addresses are virtual; for this binary `.text` VMA == file offset `0xe63c000` and `.rodata` VMA == file offset `0x84a0000`.*

## Abstract

The `LatencyHidingScheduler` decides whether two async ops may co-issue by asking which *physical resource* each one consumes, and how many of that resource the hardware has. The resource names form a single flat integer enum — `ResourceType` — that runs `0..46` (47 IDs). The first 13 are the stock XLA `AsyncTracker` collective classes (`kNoResource`, `kAllReduce`, `kReduceScatter`, …); the remaining 34 are TPU-target resources added by `jellyfish::TpuAsyncTracker` (the six directional ICI ring links, DCN bandwidth, the two host-DMA taps, six SparseCore engine classes, VMEM, and sixteen user custom-collective lanes). This page recovers the full enum value→name table from the two `GetResourceName` lookup paths, the `GetResourceTypeForOp` opcode→id switch and the six `MayAdd*` producers that classify an HLO op, the `GetNumAvailableResources` per-resource concurrency-cap source table, the `GetResourceHazardType` overlap-class table, and the per-pass tracker selection that decides which `AsyncTracker` subclass models a computation.

The reader must not confuse this `ResourceType` enum with the cost-model `ResourceVector::Resource` enum documented on [Resource Enum](../cost/resource-enum.md). They are two distinct "resource" abstractions in the same binary. The cost-model `Resource` (23 slots, nested in `ResourceVector`) measures **cycle weight** — how many cycles a functional unit is busy — and feeds `MaxResourceCycles`. This `ResourceType` (47 IDs, an `AsyncTracker` enum) measures **concurrency limits** — how many async ops of a kind may be in flight at once — and feeds `GetNumAvailableResources`. They overlap on exactly one physical concept (the six ICI ring links: cost-model slots `R[13..18]` vs scheduler ids `14..19`), but they are separate enums with separate value spaces, separate name tables, and separate consumers. This page documents `ResourceType`; the cost-model `Resource` is its sibling.

For reimplementation, the contract is:

- The 47-ID `ResourceType` enum: base XLA `{0..12}` from `AsyncTracker::GetResourceName`, and the jellyfish target `{13..46}` from `TpuAsyncTracker::GetResourceName`, with two unnamed catch-alls (ids 28 and 46) and the shared `kCustomCollective` string for ids 30..45.
- `GetResourceTypeForOp` — the base opcode→id switch (the canonical XLA collective map) — plus the jellyfish orchestrator `GetResourcesFromInstructionImpl` and its six `MayAdd*` producers that add the target ids on top.
- `GetNumAvailableResources` — the per-id concurrency cap and its source: TCE knobs, one hardware core-count (id 22, `CoresPerChip(SC)/LDPC(SC)`), and constants.
- `GetResourceHazardType` — the overlap class (unsharable / serial / nonextendable / shareable) per id, including the config-gated collective-serialization override.
- The tracker-selection gate: three `AsyncTracker` subclasses (jellyfish `TpuAsyncTracker` for the TensorCore LHS; `SparseCoreAsyncTracker` and `SparseCoreResourceAwareAsyncTracker` for the SparseCore-offload sub-passes) coexist by pass, not by mutual exclusion.

| | |
|---|---|
| **Enum** | `xla::ResourceType` (base `{0..12}`) extended by `xla::jellyfish::TpuResourceType` (`{13..46}`) |
| **ID count** | 47 (base 13 + target 34; `GetNumTargetDefinedResources` returns 34) |
| **Base name source** | `AsyncTracker::GetResourceName` @ `0x13616500` (ptr table `off_21920270`) |
| **Target name source** | `TpuAsyncTracker::GetResourceName` @ `0x10fff420` (ptr table `off_2181E148`) |
| **Op→id (base)** | `AsyncTracker::GetResourceTypeForOp` @ `0x13612240` (opcode switch) |
| **Op→id (target)** | `TpuAsyncTracker::GetResourcesFromInstructionImpl` @ `0x11001040` + six `MayAdd*` |
| **Concurrency cap** | `TpuAsyncTracker::GetNumAvailableResources` @ `0x10fff600` |
| **Overlap class** | `TpuAsyncTracker::GetResourceHazardType` @ `0x110015e0` (table `dword_AC0B2C0`) |
| **Tracker install** | `GetTpuAsyncTracker` @ `0x10975520` (TC LHS); SC trackers via `SparseCoreCompiler::RunHloScheduler` @ `0x1306f820` |
| **Source file** | `platforms/xla/service/jellyfish/latency_scheduler_cost_models_tpu.cc` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Two Resource Enums — Do Not Merge

| | Cost-model `Resource` ([Resource Enum](../cost/resource-enum.md)) | Scheduler `ResourceType` (this page) |
|---|---|---|
| Enum | `ResourceVector::Resource` | `ResourceType` / `TpuResourceType` |
| Values | 23 (`R[0..22]`) | 47 (base `{0..12}` + target `{13..46}`) |
| Measures | cycle weight (how busy a unit is) | concurrency cap (how many co-issue) |
| Name source | `ResourceVectorToString` @ `0x1c89bde0` | `GetResourceName` @ `0x13616500` (base) / `0x10fff420` (TPU) |
| Per-op map | `CycleTable::GetResource` (LLO opcode → slot) | `GetResourceTypeForOp` / `MayAdd*` (HLO opcode → id) |
| Consumer | `MaxResourceCycles` → bundle issue cost | `GetNumAvailableResources` → co-issue throttle |

The one physical concept both enums name is the six ICI ring links. The cost model deposits *cycle weight* into `R[13..18]` (`Ici{Y,X,Z}{Plus,Minus}`); the scheduler caps *concurrent issue* of the same links via `ResourceType` ids `14..19` (also `kIci{Y,X,Z}{Plus,Minus}`). The link is direct, not coincidental: `MayAddIciLinks` reads the cost model's six ICI `ResourceVector` slots and emits scheduler resource id `slot+1` for each slot the cost model deposited a nonzero cycle count into (see [Resource Classification](#resource-classification-getresourcetypeforop--the-six-mayadd-producers) below).

> **GOTCHA — same name, different number.** A reimplementer who hard-codes "the ICI resources are 13..18" from the cost-model page will be one off in the scheduler. The cost-model slot index is `0xd..0x12` (13..18); the scheduler `ResourceType` id is `slot+1` = `0xe..0x13` (14..19). The off-by-one is the deliberate `id = slot + 1` mapping in `MayAddIciLinks`, because scheduler id 13 is already taken by `kDCNbw`.

---

## The 47-ID ResourceType Enum

The enum is recovered from two `GetResourceName` lookup functions, each a `{name-ptr table, length-table}` pair indexed by the resource id.

### Base XLA Resources `{0..12}` — `AsyncTracker::GetResourceName` @ `0x13616500`

```c
// AsyncTracker::GetResourceName(resource)  @ 0x13616500
const char *GetResourceName(unsigned long r) {
    if (r > 0xc) return "Not a valid default resource";   // out-of-range sentinel string
    return off_21920270[r];                               // .data.rel.ro ptr table, [rax + r*8]
}
```

This is the stock XLA `AsyncTracker` collective taxonomy. The id is the canonical XLA `ResourceType` enum value.

| id | name | Confidence |
|---|---|---|
| 0 | `kNoResource` | CONFIRMED |
| 1 | `kAllToAll` | CONFIRMED |
| 2 | `kAllGather` | CONFIRMED |
| 3 | `kAllReduce` | CONFIRMED |
| 4 | `kCollectivePermute` | CONFIRMED |
| 5 | `kCopy` | CONFIRMED |
| 6 | `kReduceScatter` | CONFIRMED |
| 7 | `kSendRecv` | CONFIRMED |
| 8 | `kSendHost` | CONFIRMED |
| 9 | `kRecvHost` | CONFIRMED |
| 10 | `kCollectiveBroadcast` | CONFIRMED |
| 11 | (`"Not a valid default resource"` sentinel; no enumerator) | CONFIRMED |
| 12 | `kRaggedAllToAll` | CONFIRMED |

> **NOTE — id 11 is a hole.** The name-ptr table has no real enumerator at index 11; the out-of-range branch (`r > 0xc`) and a stray index 11 both resolve to the `"Not a valid default resource"` string. Ids `{2, 3, 6}` (`kAllGather`, `kAllReduce`, `kReduceScatter`) are the three the `SetConcurrentResourceLimits` block reads from the `SchedulerConfig` (`xla_max_concurrent_async_all_gathers` / `all_reduces` / `reduce_scatters`); the base resources have no entry in the target-defined availability loop (`AsyncTracker::GetNumAvailableResources` returns 0).

### Jellyfish Target Resources `{13..46}` — `TpuAsyncTracker::GetResourceName` @ `0x10fff420`

```c
// TpuAsyncTracker::GetResourceName(resource)  @ 0x10fff420  (src line 1152)
char *GetResourceName(long r) {
    CHECK(r <= 46);                                          // "resource_type < ...kTpuResourceTypeEnd"
    if (r < 13)  return AsyncTracker::GetResourceName(r);    // 0..12 → base table above
    if ((0x17FFF >> (r - 13)) & ((r - 13) < 0x11))           // 13..29, with the gap at idx 28
        return off_2181E148[r - 13];                         // TPU name table, [rax + r*8 - 0x68]
    if ((r - 30) < 0x10) return "kCustomCollective";         // 30..45 share one string
    return &nptr;                                            // 46 → empty (final catch-all)
}
```

`GetNumTargetDefinedResources` @ `0x10fff5e0` returns `34`, fixing the target range at `[13, 13+34) = [13, 46]` inclusive.

| id | name | functional resource | Confidence |
|---|---|---|---|
| 13 | `kDCNbw` | DCN (cross-slice) network bandwidth | CONFIRMED |
| 14 | `kIciYPlus` | ICI ring link +Y | CONFIRMED |
| 15 | `kIciYMinus` | ICI ring link −Y | CONFIRMED |
| 16 | `kIciXPlus` | ICI ring link +X | CONFIRMED |
| 17 | `kIciXMinus` | ICI ring link −X | CONFIRMED |
| 18 | `kIciZPlus` | ICI ring link +Z | CONFIRMED |
| 19 | `kIciZMinus` | ICI ring link −Z | CONFIRMED |
| 20 | `kHostToDevice` | host→device DMA tap | CONFIRMED |
| 21 | `kDeviceToHost` | device→host DMA tap | CONFIRMED |
| 22 | `kSparseCore` | general SparseCore engine (per-core) | CONFIRMED |
| 23 | `kSparseCoreGather` | SC gather op-class | CONFIRMED |
| 24 | `kSparseCoreScatter` | SC scatter op-class | CONFIRMED |
| 25 | `kSparseCoreDataFormatting` | SC data-formatting op-class | CONFIRMED |
| 26 | `kSparseCoreKernel` | SC kernel op-class | CONFIRMED |
| 27 | `kSparseCoreSort` | SC sort op-class | CONFIRMED |
| 28 | (unnamed; no reloc, len 0 — SC catch-all) | SC general/catch-all | MEDIUM |
| 29 | `kVmem` | VMEM-resident op | CONFIRMED |
| 30..45 | `kCustomCollective` | 16 user custom-collective lanes (one shared string) | CONFIRMED |
| 46 | (unnamed; len 0 — final catch-all) | tail sentinel | MEDIUM |

> **NOTE — ids 28 and 46 are anonymous but valid.** The name-ptr table at `off_2181E148` has no relocation for index 28 (gap at slot `0x2181E1C0`), and id 46 falls through to `&nptr` (the empty string). Both are real resource ids that `GetNumAvailableResources` and `GetResourceHazardType` accept; only the print path leaves them blank. Id 28 is the SparseCore "catch-all" category (the `enum1` arm), id 46 the tail catch-all. A reimplementer must size the resource tables at 47, not 45.

> **GOTCHA — the kIci name order is not the cost-model slot order.** `GetResourceName` orders the directions `Y+, Y−, X+, X−, Z+, Z−` (ids 14..19). The cost-model `IciResource` slot labels are ordered `X, X, Y, Y, Z, Z`. The two orderings are independent naming conventions; the runtime relation is purely `id = slot + 1`, so the *physical link* a resource id denotes is whichever cost-model slot the per-collective cost was deposited into upstream, not what the name string implies. Do not assume id 14 == cost slot for X.

---

## Resource Classification — `GetResourceTypeForOp` + the Six `MayAdd*` Producers

Classification is two-layered. Layer A (base XLA) maps the raw HLO opcode to a base id `{0..12}` via a single switch. Layer B (jellyfish) calls the base, then *adds* target ids `{13..45}` via six producers, each with its own selection rule.

### Layer A — `AsyncTracker::GetResourceTypeForOp` @ `0x13612240`

```c
// AsyncTracker::GetResourceTypeForOp(HloOpcode op)  @ 0x13612240
long GetResourceTypeForOp(int op) {
    switch (op) {
        case 6:   return 2;    // all-gather           → kAllGather
        case 9:   return 3;    // all-reduce           → kAllReduce
        case 12:  return 1;    // all-to-all   (0xc)   → kAllToAll
        case 33:  return 10;   // collective-broadcast (0x21) → kCollectiveBroadcast
        case 34:  return 4;    // collective-permute   (0x22) → kCollectivePermute
        case 44:  return 5;    // copy                 (0x2c) → kCopy
        case 86:  return 12;   // ragged-all-to-all    (0x56) → kRaggedAllToAll
        case 93:  return 6;    // reduce-scatter       (0x5d) → kReduceScatter
        default:  return 0;    //                      → kNoResource
    }
}
```

The opcode integers are the XLA `HloOpcode` enum values. Note ids 86 (`ragged-all-to-all`) and 93 (`reduce-scatter`) are handled by explicit `default`-block compares rather than the dense jump table over `op - 6`.

> **CORRECTION (RT-1) —** an earlier reading attributed opcode `0x56` (ragged-all-to-all) to id 6 and implicitly bound scheduler key 6 to "ragged-a2a". The byte-exact switch overturns it: `0x56 → 12` (`kRaggedAllToAll`) and `0x5d → 6` (`kReduceScatter`). With the authoritative names read, the `SetConcurrentResourceLimits` knob→key bindings are self-consistent: key 2 ← `kAllGather`, key 3 ← `kAllReduce`, key 6 ← `kReduceScatter`.

### Layer B — `TpuAsyncTracker::GetResourcesFromInstructionImpl` @ `0x11001040`

```c
// TpuAsyncTracker::GetResourcesFromInstructionImpl(hlo)  @ 0x11001040  (VLOG src 1780)
void GetResourcesFromInstructionImpl(const HloInstruction &hlo, vector<pair<id,usage>> *out) {
    AsyncTracker::GetResourcesFromInstructionImpl(hlo, out);   // (0) base ids {0..12}
    // (*) async-start/done over an all-reduce-scatter fusion → kReduceScatter (id 6)
    if ((hlo.opcode & 0xfe) == 0x10 &&                         //   async-start (0x10) | async-done (0x11)
        IsAllReduceScatterFusion(hlo.async_wrapped_instruction()))
        out->push_back({6, (this->byte208 ^ (opcode == 0x11)) + 1});
    MayAddDcnBw(hlo, out);            // (1) → id 13
    MayAddIciLinks(hlo, out);         // (2) → ids 14..19
    MayAddHostTransfers(hlo, out);    // (3) → ids 20, 21
    MayAddSparseCoreResource(hlo, out);// (4) → ids 22..27
    MayAddVmem(hlo, out);             // (5) → id 29
    MayAddCustomCollective(hlo, out); // (6) → ids 30..45
}
```

The producers run in this fixed order. Each emits `pair{resource_id, ResourceUsageType}` where the usage is `kResourceOccupy` (`2 - byte208`) on an async-start and `kResourceRelease` (`byte208 + 1`) on an async-done; `byte208` (`this+0x208`) is the start/done canonical-swap bit.

| Producer | Address | Emits ids | Selection rule | Confidence |
|---|---|---|---|---|
| `MayAddDcnBw` | `0x10fff6e0` | 13 | cross-slice collective (opcode in mask `{89..111}` ∩ `0x600003`); looks up `CrossSliceCollectiveInfoTracker`; emits id `13` | CONFIRMED |
| `MayAddIciLinks` | `0x10fffb20` | 14..19 | builds a `CostModel`, runs `GetCycles` into a 23-slot `ResourceVector`, scans the six ICI slots `{0xd..0x12}` (heap table `{13,14,15,16,17,18}`); for each slot with cost ≠ 0 emits id `slot+1` | CONFIRMED |
| `MayAddHostTransfers` | `0x11000280` | 20, 21 | host send/recv → `20` (H2D) / `21` (D2H) | CONFIRMED |
| `MayAddSparseCoreResource` | `0x11000480` | 22..27 | thread-name "sparsecore" gate, then `GetSparseCoreConfig` op-type enum `{2..7}` → ids `{23,24,25,26,27}`; separately emits id `22` once per SC core (`GetNumSparseCoresUsed`), gated `this+0x13b == 1` | CONFIRMED |
| `MayAddVmem` | `0x11000c00` | 29 | VMEM-resident op (opcode 10/11/16/17 with all-reduce-scatter-fusion gate) → id `29` | CONFIRMED |
| `MayAddCustomCollective` | `0x11000d20` | 30..45 | `IsCustomCallAsync{Start,Done}` gate; `CustomCallConfig.collective_id` (field 3, `cfg+0x78`, hasbit `cfg+0x10 & 0x40`); emits id `0x1e + collective_id`, bounded `[0,15]` | CONFIRMED |

> **QUIRK — collectives overlap by ICI *direction*, not by one "collective" counter.** `MayAddIciLinks` does not read the opcode to pick a direction; it inspects which ICI `ResourceVector` slots the cost model deposited cycles into and emits the matching scheduler resource for each. Two collectives that ride *different* ICI axes (e.g. an all-reduce on +X and an all-gather on +Y) consume *different* resource ids and overlap freely; two on the *same* axis serialize. A reimplementation that models a single "collective overlap" counter will incorrectly serialize them. The opcode pre-filter skips `{6, 9, 0x22}` (all-gather / all-reduce / collective-permute, which have their own ring path) and `0x5d` (reduce-scatter).

> **QUIRK — kCustomCollective is keyed by a numeric id, not a target string.** `MayAddCustomCollective` reads `CustomCallConfig.collective_id` (an `int64`, field 3) and computes `resource_id = 0x1e + collective_id`, bounded to `[0, 15]` (a `CHECK` against `kCustomCollectiveEnd` fatals on out-of-range, message *"Use lower numbers of collective ids"*). So up to 16 *distinct* user custom-collectives are scheduled on separate resources by an explicit numeric id in the backend config — the custom-call target name is irrelevant to the resource assignment.

---

## Per-Resource Concurrency Cap — `GetNumAvailableResources` @ `0x10fff600`

`GetNumAvailableResources(id)` returns how many async ops of that resource may be in flight. Base ids `{0..12}` return 0 from this loop (they are bounded by the fixed `SetConcurrentResourceLimits` key block, not the target loop). Target ids `{13..46}` read precomputed tracker fields wired by the `TpuAsyncTracker` ctor (`GetTpuAsyncTracker` @ `0x10975520`).

```c
// TpuAsyncTracker::GetNumAvailableResources(id)  @ 0x10fff600  (src line 1243)
long GetNumAvailableResources(long id) {
    CHECK(id <= 46);                                  // "...kTpuResourceTypeEnd"
    if (id < 13) return AsyncTracker::GetNumAvailableResources(id);  // base → 0
    switch (id) {
        case 13:           return this->[+0x128];     // kDCNbw
        case 20: case 21:  return this->[+0x130];     // host transfer
        case 22:           return this->[+0x140];     // kSparseCore
        case 23:           return this->[+0x148];
        case 24:           return this->[+0x150];
        case 25:           return this->[+0x158];
        case 26:           return this->[+0x160];
        case 27:           return this->[+0x168];
        case 29:           return 1;                  // kVmem — hardcoded 1
        default:                                      // 14..19, 28, 46  → [+0x170]
            if ((unsigned)(id - 30) > 0xf) return this->[+0x170];   // ici_overlap_limit
            else                          return this->[+0x178];    // 30..45 kCustomCollective
    }
}
```

| id(s) | name | tracker field | available-count source | Confidence |
|---|---|---|---|---|
| 13 | `kDCNbw` | `+0x128` | `xla_tpu_dcn_overlap_limit` (`int64`, TCE `+0x11d8`) | HIGH |
| 14..19 | `kIci{Y,X,Z}{±}` | `+0x170` | field 1130 `xla_tpu_sparse_core_ici_overlap_limit` | CONFIRMED |
| 20, 21 | `kHostToDevice`/`kDeviceToHost` | `+0x130` | field 803 `xla_tpu_host_transfer_overlap_limit` | HIGH |
| 22 | `kSparseCore` | `+0x140` | `CoresPerChip(SC) / LogicalDevicesPerChip(SC)` (TpuTopology, per-gen) | CONFIRMED |
| 23 | `kSparseCoreGather` | `+0x148` | field 1088 `..._gather_overlap_limit` | HIGH |
| 24 | `kSparseCoreScatter` | `+0x150` | field 1089 `..._scatter_overlap_limit` | HIGH |
| 25 | `kSparseCoreDataFormatting` | `+0x158` | field 1090 `..._data_formatting_overlap_limit` | HIGH |
| 26 | `kSparseCoreKernel` | `+0x160` | field 1091 `..._kernel_overlap_limit` | HIGH |
| 27 | `kSparseCoreSort` | `+0x168` | field 1092 `..._sort_overlap_limit` | HIGH |
| 28 | (SC catch-all) | `+0x170` | field 1130 (shared with ICI) | HIGH |
| 29 | `kVmem` | const | hardcoded `1` | CONFIRMED |
| 30..45 | `kCustomCollective` | `+0x178` | constant `1` (ctor `push 1`) | HIGH |
| 46 | (catch-all) | `+0x170` | field 1130 (shared with ICI) | HIGH |

### Field 1130 — one knob caps the ICI links *and* the SC catch-all

The `+0x170` field is wired from compilation-environment field **1130 = `xla_tpu_sparse_core_ici_overlap_limit`**, an `AutoProto` (`AutoOr<long>`) wrapper. The field number is byte-exact (`_InternalSerialize` writes `edi = 0x46a` for the value at TCE `+0xa88`), and the carved `FieldDescriptorProto` gives name (`0x25`-byte string), number (`0xea08` = 1130), type (`TYPE_MESSAGE` → `.xla.jellyfish.AutoProto`). In the LHS path the oneof is unset, so `AutoOr<long>::FromProtoOrDie` returns `INT64_MAX` (no cap).

| attribute | value |
|---|---|
| field number | 1130 (`0x46a`) |
| field name | `xla_tpu_sparse_core_ici_overlap_limit` |
| proto type | `TYPE_MESSAGE` (`.xla.jellyfish.AutoProto`) → `AutoOr<long>` |
| TCE `_impl_` offset | `0xa88` → tracker `+0x170` (Create arg17) |
| AUTO fallback | `INT64_MAX` (LHS path; no cap) |
| resources capped | ids 14..19 (all six ICI directions) + 28 + 46 |

> **NOTE — the "ici" in the knob name is literal but its reach is wider.** `xla_tpu_sparse_core_ici_overlap_limit` caps all six physical ICI ring resources (the 3 torus dims × 2 directions) *and* the SparseCore catch-all (id 28) *and* the tail catch-all (id 46) — a single shared concurrency budget for ICI-link-bearing and SC-catch-all async ops. The DCN-bandwidth resource (id 13) has its own `int64` cap; the five named SC sub-categories (23..27) each have their own AutoProto knob.

### Id 22 (`kSparseCore`) is the only hardware-derived cap

```c
// GetTpuAsyncTracker @ 0x10975520 — the id-22 (kSparseCore) count, per-gen
arg11 = 1;                                                       // default fallthrough is 1, not 0
if (EnableSparseCoreOffloadQueuingInLhs())                       // @0x1d6b81e0
    arg11 = SparseCoreOffloadQueuingOverlapLimit();              // @0x1d6b8320 — a TCE knob
else if (ShouldEnableConcurrentSparseCoreOffloading()) {        // @0x1d6b6f80
    long ldpc = Target::LogicalDevicesPerChip(kSparseCore);      // @0x1d615b00
    if (ldpc <= 0) arg11 = 0;                                    // guard against div-by-zero
    else arg11 = Target::CoresPerChip(kSparseCore) / ldpc;       // @0x1d615b40  (idiv)
}
// else: arg11 stays 1
```

> **CORRECTION (RT-2) —** an earlier reading gave the third (neither-queuing-nor-concurrent) arm as `arg11 = 0`. The byte-exact `GetTpuAsyncTracker` @ `0x10975520` sets the default to `1` (`v15 = 1`) *before* the concurrent-offload branch, and only writes `0` inside that branch when `LogicalDevicesPerChip(SC) <= 0`. So when SparseCore offload is disabled altogether, id 22's cap is `1`, not `0`.

`Target::CoresPerChip(kSparseCore)` reads `Target[+0x3b8]` (the `tpu::TpuTopology*`, off 952) at `topo + coreType*0xc + 0x7c` (coreType 2 = SparseCore → offset `0x94` = 148) — a per-core-type `int32` in the topology struct ([TPU Topology Struct](../targets/tpu-topology-struct.md)). `Target::LogicalDevicesPerChip(kSparseCore)` calls `TpuTopology::LogicalDevicesPerChip` → `TpuChipParts::CoreCount` + `TpuChipConfig::Megacore`, so the divisor is the megacore collapse (`ldpc(SC) == 2` on megacore parts). The result — physical SC cores per chip divided by logical devices per chip — is the only target resource whose cap is a hardware count rather than a config knob.

> **QUIRK — id 22's cap changes meaning under offload-queuing.** When `EnableSparseCoreOffloadQueuingInLhs` is set (the common embedding production config), id 22's cap becomes a TCE knob (`SparseCoreOffloadQueuingOverlapLimit`) instead of the topology core-count. The three-way select is byte-present; the offload-queuing knob's field number was not decoded (PARTIAL).

---

## Overlap Class — `GetResourceHazardType` @ `0x110015e0`

Whether two ops contending on the same resource may overlap is the resource's *hazard class*. `GetResourceHazardType(id)` returns a small integer code.

```c
// TpuAsyncTracker::GetResourceHazardType(id)  @ 0x110015e0  (src line 1848)
long GetResourceHazardType(long id) {
    CHECK(id <= 46);
    if (id >= 13) {
        if ((0x109FF >> (id - 13)) & ((id - 13) < 0x11))
            return dword_AC0B2C0[id - 13];                    // table for 13..29
        return 3 * (unsigned)((id - 30) >= 0x10) + 1;         // 30..45 kCustomCollective → 1; 46 → 4
    }
    // base ids 0..12:
    if (this->byte202 /*track_sync_op_resource*/ != 1)
        return AsyncTracker::GetResourceHazardType(id);       // default base: 4*(id != 5)
    // TPU collective-serialization override:
    if (id == 3 /*kAllReduce*/ || id == 6 /*kReduceScatter*/ ||
        (id == 2 /*kAllGather*/ && this->byte314))
        return 3;                                             // kSerial
    return AsyncTracker::GetResourceHazardType(id);
}
```

The hazard codes:

| code | meaning | overlap behaviour |
|---|---|---|
| 0 | unsharable | single-issue; no two of this resource overlap |
| 1 | serial | one in flight (FIFO ordering) |
| 2 | nonextendable | cannot be deferred past its window (`kVmem`) |
| 3 | serial (TPU collective override) | collective engine single-occupancy |
| 4 | shareable | overlap up to the per-kind `GetNumAvailableResources` limit |

For the target ids 13..29, the table is `dword_AC0B2C0 = [0,1,1,1,1,1,1,0,0,0,0,2,0,0,0,0,2]`:

| id | name | hazard | reading |
|---|---|---|---|
| 13 | `kDCNbw` | 0 | unsharable |
| 14..19 | `kIci{Y,X,Z}{±}` | 1 | serial per direction |
| 20, 21 | host transfers | 0 | unsharable |
| 22 | `kSparseCore` | 2 | nonextendable |
| 23..27 | SC sub-categories | 0 | unsharable |
| 28 | (SC catch-all) | 0 | unsharable |
| 29 | `kVmem` | 2 | nonextendable |
| 30..45 | `kCustomCollective` | 1 | serial |
| 46 | (catch-all) | 4 | shareable |

> **QUIRK — every base resource is shareable except `kCopy`.** The base `AsyncTracker::GetResourceHazardType` returns `4 * (id != 5)` — every base collective class is shareable (hazard 4) *except* `kCopy` (id 5), which is hazard 0 (unsharable): async copies serialize on the copy engine. The TPU override only flips `kAllReduce`/`kReduceScatter`/`kAllGather` from shareable to serial (3), and only when the `track_sync_op_resource` byte (`this+0x202`) is set; for all-gather an additional byte (`this+0x314`) gates it.

---

## Tracker Selection — Three Trackers, Three Sub-Passes

The `ResourceType` model above is the jellyfish `TpuAsyncTracker`. It is not the only `AsyncTracker` in the binary: two SparseCore variants exist, and all three coexist within one compile, owned by *different* scheduling sub-passes — not selected by a single flag.

| Tracker | Installer / call site | Resource space | Confidence |
|---|---|---|---|
| jellyfish `TpuAsyncTracker` | `GetTpuAsyncTracker` @ `0x10975520`, from jellyfish `RunHloScheduler` (1st pass + field-1202 rerun) | base `{0..12}` + target `{13..46}` (this page) | CONFIRMED |
| `SparseCoreAsyncTracker` | `RunSparseCoreLatencyHidingScheduler` @ `0x1306e020` | base `AsyncTracker` + SC overrides (not decoded here) | HIGH |
| `SparseCoreResourceAwareAsyncTracker` | `RunSparseCoreCostModelLatencyHidingScheduler` @ `0x1306f040` (make_shared @ `0x1306f1bb`) | distinct `{13..17}` space, hardcoded caps | CONFIRMED |

The TensorCore LHS always uses the jellyfish `TpuAsyncTracker`. The two SparseCore-offload schedulers run only when the SparseCore gate holds (`SparseCoreCompiler::RunHloScheduler` @ `0x1306f820`):

```c
// SparseCoreCompiler::RunHloScheduler gate  @ 0x1306f820
runSC =  TpuChipConfig::Megachip(Target[+0x3b8][+0x18])
      && Target::CoresPerChip(kSparseCore) > 0                  // topo[+0x94] > 0
      && (Target[+0x628] & 4  ||  Target[+0x540] != 0)          // SC-offload-enable bits
      && offloader_util::ModuleContainsLEMSparseCoreInstruction(M)
      && FLAGS_xla_sc_enable_latency_hiding_scheduler;
```

When `runSC`, the SparseCore-offload schedule is produced first: `RunSparseCoreLatencyHidingScheduler` (plain, `SparseCoreAsyncTracker`) runs, and on success `RunSparseCoreCostModelLatencyHidingScheduler` (`SparseCoreResourceAwareAsyncTracker` + `EmbeddingBackwardPassLatencyEstimator`) refines it. Then the jellyfish `TpuAsyncTracker` is installed for the TensorCore LHS in the same `RunHloScheduler`. When the gate fails, the SparseCore pass falls back to the generic `DFSMemoryScheduler` (no SC tracker) and only the TensorCore LHS runs.

### The SparseCore-resource-aware tracker is a separate 5-resource space

`SparseCoreResourceAwareAsyncTracker` does **not** share the jellyfish `{13..46}` enum. Its `GetNumTargetDefinedResources` @ `0x134a7420` returns 5, and its resources `{13..17}` have their own names and hardcoded (non-config) limits from a `.rodata` table:

```c
// SparseCoreResourceAwareAsyncTracker::GetNumAvailableResources(id)  @ 0x134a7b20  (src line 261)
long GetNumAvailableResources(long id) {
    CHECK(id <= 17);                                  // "...kSparseCoreResourceTypeEnd"
    if (id < 13) return AsyncTracker::GetNumAvailableResources(id);
    return qword_AE344F8[id - 13];                    // {1, 20, 5, 1, 1}
}
```

| id | name (`GetResourceName` @ `0x134a7440`) | hardcoded limit |
|---|---|---|
| 13 | `SCS` | 1 |
| 14 | `SCT` | 20 (`0x14`) |
| 15 | `ICI` | 5 |
| 16 | `LocalReduction` | 1 |
| 17 | `2DAllToAll` | 1 |

> **NOTE — these are live, but only for the SC-offload cost-model sub-pass.** The `{1, 20, 5, 1, 1}` caps are reachable (megachip part + SC cores > 0 + the offload bits + an LEM instruction + the flag), but they govern the SparseCore-offload latency-hiding pass — *not* the main TensorCore LHS, which is the jellyfish `TpuAsyncTracker`. A reimplementer must keep the two resource spaces (`{13..46}` jellyfish vs `{13..17}` SC-resource-aware) entirely separate; they reuse the same low integer ids for completely different resources.

---

## Worked Example — Two Collectives, One Schedule Step

A fragment with two independent async collectives on a megacore part:

```text
%ag   = all-gather-start(%x)        ; ICI ride deposits cost into one ICI slot
%ag.d = all-gather-done(%ag)
%ar   = all-reduce-start(%y)        ; ICI ride deposits cost into a different ICI slot
%ar.d = all-reduce-done(%ar)
```

Walking the classifier and the cap model:

- `GetResourceTypeForOp` maps `all-gather` (opcode 6) → base id 2 and `all-reduce` (opcode 9) → base id 3.
- For `%ag`, `MayAddIciLinks` builds a `CostModel`, runs `GetCycles`, and for each ICI slot `s` in `{0xd..0x12}` with cost ≠ 0 emits resource id `s + 1`. Say the cost model deposited into slot `0xe` → resource id `0xf = 15`. For `%ar`, say it deposited into slot `0xf` → resource id `0x10 = 16`. (Which slot a given collective uses is decided upstream by the cost model; the resource *name* ordering `Y,X,Z` is independent of the slot ordering — see the GOTCHA above.)
- The two collectives consume *different* resource ids (15 vs 16). `GetResourceHazardType(15) = 1` and `GetResourceHazardType(16) = 1` (serial *per direction*), and `GetNumAvailableResources(15/16) = INT64_MAX` (field 1130 AUTO) — so the scheduler lets both fly **concurrently**: they ride different ICI rings.
- Had both deposited into the *same* slot (both → resource 15), the serial hazard would force them in sequence, even with the `INT64_MAX` cap, because two ops of a serial resource cannot be in flight together.
- If `xla_tpu_sparse_core_ici_overlap_limit` (field 1130) were set to a finite N, no more than N ICI-link-bearing async ops (across all six directions and the SC catch-all) could be outstanding at once — the shared budget.

This is exactly why the resource model keys collectives by ICI *direction*, not by a single "collective" class: the physical bottleneck is the per-direction ICI link, and the cost model already knows which link each collective uses.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Enum is 47 IDs: base `{0..12}` + target `{13..46}` | `GetNumTargetDefinedResources` @ `0x10fff5e0` = 34; `GetResourceName` `r <= 46` CHECK | CONFIRMED |
| Base names `{0..12}` (kNoResource..kRaggedAllToAll, id 11 sentinel) | `AsyncTracker::GetResourceName` @ `0x13616500`, table `off_21920270` | CONFIRMED |
| Target names `{13..46}` (kDCNbw, 6× kIci, host, 6× SC, kVmem, 16× kCustomCollective, 2 catch-alls) | `TpuAsyncTracker::GetResourceName` @ `0x10fff420`, table `off_2181E148` | CONFIRMED |
| Ids 28 and 46 are valid but unnamed | name-ptr table gap at slot 28; id 46 → `&nptr` | MEDIUM |
| Op→id switch (op6→2, 9→3, 12→1, 33→10, 34→4, 44→5, 86→12, 93→6, else 0) | `GetResourceTypeForOp` @ `0x13612240` | CONFIRMED |
| Six `MayAdd*` producers in fixed order; usage = occupy/release via `byte208` | `GetResourcesFromInstructionImpl` @ `0x11001040` | CONFIRMED |
| `MayAddIciLinks` emits id `slot+1` from nonzero ICI `ResourceVector` slots | `MayAddIciLinks` @ `0x10fffb20`, slot table `{13..18}` | CONFIRMED |
| `MayAddCustomCollective` id = `0x1e + collective_id`, bound `[0,15]` | `MayAddCustomCollective` @ `0x11000d20`, CHECK `kCustomCollectiveEnd` | CONFIRMED |
| `GetNumAvailableResources` id→field map (`+0x128..+0x178`, id 29 const 1) | `0x10fff600`, switch byte-decoded | CONFIRMED |
| Field 1130 = `xla_tpu_sparse_core_ici_overlap_limit` caps ids 14..19, 28, 46 | `_InternalSerialize` `edi=0x46a`; FieldDescriptorProto carve | CONFIRMED |
| Id 22 (`kSparseCore`) cap = `CoresPerChip(SC)/LDPC(SC)` (TpuTopology, per-gen) | `GetTpuAsyncTracker` @ `0x10975520` idiv branch | CONFIRMED |
| Hazard table `[0,1,1,1,1,1,1,0,0,0,0,2,0,0,0,0,2]`; base `4*(id!=5)`; override→3 | `GetResourceHazardType` @ `0x110015e0`, `dword_AC0B2C0` | CONFIRMED |
| Three trackers coexist by sub-pass; SC gate predicate | `SparseCoreCompiler::RunHloScheduler` @ `0x1306f820` | CONFIRMED |
| SCRAAT distinct `{13..17}` = SCS/SCT/ICI/LocalReduction/2DAllToAll, caps `{1,20,5,1,1}` | `0x134a7b20` (table `qword_AE344F8`) / `0x134a7440` | CONFIRMED |
| TCE field numbers 803/1088..1092 for ids 20/21/23..27 | descriptor names (not separately byte-anchored here) | HIGH |
| Id 13 DCN cap field# (507 vs 508) at TCE `+0x11d8` | `int64` type + name confirmed; slot pairing not isolated | PARTIAL |
| Offload-queuing branch field# for id 22 | three-way select byte-present; knob field# not decoded | PARTIAL |

---

## Cross-References

- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — the list scheduler and the `TpuAsyncTracker` dispatch that consumes this enum; the comparator's resource-conflict keys.
- [Scheduler Overview](overview.md) — where LHS sits in the TPU scheduling pipeline.
- [LHS ILP variant](lhs-ilp-variant.md) — swaps the async classifier ahead of the comparator; the `ResourceType` model is unchanged.
- [Resource Enum](../cost/resource-enum.md) — the cost-model `ResourceVector::Resource` (23 slots, cycle weight) — the sibling enum this page must not be conflated with.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — the `MaxResourceCycles` bundle cost and `LatencyBetween` latency that the cost model behind `MayAddIciLinks` produces.
- [GetHloResources Routing](../cost/gethloresources-routing.md) — the cost-side resource routing that deposits ICI cycles into the slots `MayAddIciLinks` reads.
- [TPU Topology Struct](../targets/tpu-topology-struct.md) — the `TpuTopology` struct whose `CoresPerChip(SC)/LDPC(SC)` sets the id-22 (`kSparseCore`) availability count.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
