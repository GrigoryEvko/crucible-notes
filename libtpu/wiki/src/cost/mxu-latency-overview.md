# MXU Latency Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset.*

## Abstract

`MxuLatencyTable` is the **MXU half of the per-generation cost model**. The base `Performance` model (the per-opcode latency grid of [`performance-overview`](performance-overview.md)) gives the *total* cycle latency of every LLO instruction; what it cannot express is how a matrix-unit op *occupies the MXU pipeline* — how many cycles each internal sub-unit (gain array, the two matrix-staging registers, the matrix-result buffer read port) is held while the op is in flight. Two back-to-back matmul pushes do not simply add their latencies; they queue on the shared MXU sub-resources, and a later push stalls until the resource it needs is free. `MxuLatencyTable` is the reservation model that prices exactly that occupancy.

The familiar reference frame is an LLVM `TargetSchedModel` / `MCSchedModel`: an instruction reserves a set of `ProcResource`s for a number of cycles, and the list scheduler advances the issue cursor only when every reserved resource is available. `MxuLatencyTable` is the same idea specialised to one functional unit. Each MXU op is reduced to a small **modifier key** (which captures its data format, transpose flag, and which matrix-staging register it touches), the key indexes a `flat_hash_map` whose value is a fixed-length `array<int,N>`, and `array[resource]` is the hold-cycle count for that `MxuResource`. `N` is 19 on Viperfish (`array<int,19>`) and 11 on Ghostlite / TPU7x (`array<int,11>`) — the resource set is per-generation, so each gen owns a distinct subclass and its own table. The base scalar `Performance::GetResourceUsage` returns a single cycle count per resource; `MxuLatencyTable::GetResourceUsage` overrides it for MXU ops to return the table cell.

This page documents the **model and the index scheme** that every generation shares: the four op families and their modifier keys, the reservation-array layout, the `find → array[resource]` read, the per-gen default fallback, and how the issue-stall logic in [`mxu-opholdissues-stall`](mxu-opholdissues-stall.md) consumes the result. The actual integer reservation matrices live on the per-generation pages ([JF/DF](mxu-latency-jf-df.md), [PF](mxu-latency-pf.md), [VF](mxu-latency-vf.md), [GL](mxu-latency-gl.md), [GF](mxu-latency-gf.md)); the modifier ordinals and format bindings live on [`matmul-mode-modifiers`](matmul-mode-modifiers.md).

For reimplementation, the contract is:

- The per-gen `MxuLatencyTable` object layout: one `flat_hash_map<Modifier, array<int,N>>` per op family, plus the per-resource default fallback.
- The modifier-key construction for each of the matmul / matpush (latch) / matres / vlxmr families: which bytes encode format, transpose, and the matrix-staging register.
- The `GetResourceUsage(Instruction, Resource)` lookup: select family by opcode → build key → `find` → bounds-check `resource < N` → read `array[resource]`.
- That JF/DF have **no** `MxuLatencyTable` — their MXU occupancy is folded into the inline 15-field latency model.

| | |
|---|---|
| **Class** | `xla::<gen>::MxuLatencyTable` (one per gen: `viperfish`, `ghostlite`, TPU7x anon-ns) |
| **VF ctor / table** | `xla::viperfish::MxuLatencyTable::MxuLatencyTable` `@0x1c8a52c0` · `array<int,19>` |
| **VF lookup** | `xla::viperfish::MxuLatencyTable::GetResourceUsage` `@0x1c8ae5c0` |
| **GL lookup** | `xla::ghostlite::MxuLatencyTable::GetResourceUsage` `@0x1c8b7560` · `array<int,11>` |
| **TPU7x lookup** | `MxuLatencyTable::GetResourceUsage` `@0x1c8bdb20` · `array<int,11>` |
| **Issue-stall consumer** | `MxuOpHoldIssues` `@0x1c8ad3a0`, `MxuOpResourceReservations` `@0x1c8ad080` |
| **Resource count** | `MxuResource::kNumMxuResources` = 19 (VF) / 11 (GL, GF) — CHECK-anchored |
| **Where the table lives** | pointer at `LatencyTable` `this+0x1d8` (= `+472`), set from a process-wide `GetSharedMxuLatencyTable()` singleton (PF/VF/GL/GF only) |

---

## The Generation Split

Of the six TPU generations the cost model serves, only **four** carry an `MxuLatencyTable`. The JF/DF latency class (`LatencyTableJellyfish`, the v2/v3 subclass, object size 0x58) prices MXU occupancy directly inside its inline 15-field latency table — there is no separate reservation object. Pufferfish (v4), Viperfish (v5/v5e), Ghostlite (v6e), and TPU7x each get a `LatencyTable` whose `+0x1d8` (`+472`) slot holds a pointer to an `MxuLatencyTable`; the `LatencyTableViperfish` ctor `@0x1c8a3f20` sets this slot from the `GetSharedMxuLatencyTable()` process-wide singleton (`a1 + 472 = mxu_latency_shared`), so the reservation maps are built once and shared, not rebuilt per `LatencyTable`. See [`cycletable-family`](cycletable-family.md) for the per-gen cost-model subclass selection.

| Gen | TpuVer | `MxuLatencyTable` | Array width `N` | Lookup `@addr` | Source file |
|---|---|---|---|---|---|
| Jellyfish / Dragonfish | 0 / 1 | none — inline 15-field model | — | — | — |
| Pufferfish | 2 | yes | (PF page) | — | `mxu_latency_table_pf.cc` |
| Viperfish | 3 | yes | 19 | `@0x1c8ae5c0` | `mxu_latency_table_vf.cc` |
| Ghostlite | 4 | yes | 11 | `@0x1c8b7560` | `mxu_latency_table_gl.cc` |
| TPU7x (6acc60406) | 5 | yes | 11 | `@0x1c8bdb20` | `6acc60406/mxu_latency_table_gf.cc` (own file; matmul opcodes 289/295/301/307) |

> **NOTE —** the array width is the **resource count**, not a row count. `MxuResource` is a per-gen enum; `kNumMxuResources` widens to 19 on Viperfish (the most XLU-rich gen, `xlu_count=3`) and narrows to 11 on Ghostlite/TPU7x. The two generations therefore use *different resource indices for the same physical sub-unit*, and a reimplementation must keep one enum per gen — not a shared one. This is confirmed by the bounds-check CHECK string (below), which differs only in the literal it compares against (`0x13`=19 vs `0xB`=11).

---

## Object Layout

### Purpose

The table is built once per process, lazily by `GetSharedMxuLatencyTable`, via the `MxuLatencyTable` constructor (`viperfish` `@0x1c8a52c0`, ~27 KB of straight-line `try_emplace`s) that fills several `flat_hash_map`s, one per MXU op family. The maps are the only state the lookup reads.

### Structure

Each `MxuLatencyTable` is a small set of Abseil flat hash maps. The lookup distinguishes families by the key *type* (the C++ template argument to `find`), not by a discriminator field, so the maps sit at fixed offsets:

```text
MxuLatencyTable (shared singleton, pointer stored at owning LatencyTable + 0x1d8)
  this + 0x00   flat_hash_map<MatpushModifier, array<int,N>>   ── latch / matprep ops
  this + 0x20   flat_hash_map<MatmulModifier,  array<int,N>>   ── matmul ops
  this + ...    flat_hash_map<MatresModifier,  array<int,N>>   ── matrix-result-read ops
  this + ...    flat_hash_map<VlxmrModifier,   array<int,N>>   ── vector-latch-into-MRB ops
```

The matpush map is at `this+0x00` (the `find<MatpushModifier>` call passes the bare `this`/`a2`); the matmul map is at `this+0x20` (`find<MatmulModifier>(a2 + 32, ...)`) — both offsets are read directly in `GetResourceUsage` `@0x1c8ae5c0`. The matres and vlxmr maps sit at later offsets, populated by the dedicated `SetReservations<MatresModifier>` `@0x1c8acea0` and `SetReservations<VlxmrModifier>` `@0x1c8accc0` helpers.

### How a row is built — `SetReservations`

Every map entry is built the same way, by a templated `SetReservations<Modifier>` (VF: matpush `@0x1c8abde0`, vlxmr `@0x1c8accc0`, matres `@0x1c8acea0`; GL: `@0x1c8b5d80`/`@0x1c8b5f60`/`@0x1c8b6140`). It takes a `Modifier` key and a small `flat_hash_map<MxuResource,int>` (the sparse "this op holds resource k for c cycles" set), expands it into a dense `array<int,N>`, and inserts it:

```c
function SetReservations<Modifier>(key, resource_to_cycles_map):   // VF @0x1c8abde0
    array<int,N> res_vector = {0};                  // zero-init all N slots
    for (resource_index, cycles) in resource_to_cycles_map:
        CHECK(resource_index < N);                   // "resource_index < to_underlying(
                                                     //  MxuResource::kNumMxuResources)" — vf.cc:58
        res_vector[resource_index] = cycles;
    CHECK(target_map->try_emplace(key, res_vector).second);   // vf.cc:71 — keys are unique
```

The zero-init means **any resource not named by the op holds it for 0 cycles** — a resource the op never touches imposes no stall. The CHECK at vf.cc:58 (`MakeCheckOpString(resource_index, 19, ...)`) hard-bounds the index to `kNumMxuResources`, and the CHECK at vf.cc:71 enforces that no `(Modifier)` key is inserted twice. Both are byte-confirmed in the decompiled `SetReservations<MatpushModifier>`.

> **QUIRK —** the value array is read out of the hash bucket with raw `vmovups` loads in `GetResourceUsage`, copying the whole `array<int,N>` onto the stack before indexing it. The matmul read is `vmovups [rdx+8]` (8-byte `MatmulModifier` key precedes the value); the matpush read is `vmovups [rdx+4]` (4-byte `MatpushModifier` key). A reimplementation that assumes a uniform key width will read the array at the wrong offset for one of the two families.

---

## Modifier Keys

### Purpose

The modifier key is the compression of an MXU op down to the small set of attributes that change its pipeline occupancy. It is the *index into the row space*; everything about the op that does not affect occupancy is discarded.

### The four families and their keys

`GetResourceUsage` selects the family by the instruction opcode, then constructs the key from helper functions. On Viperfish the opcode→family mapping and key construction are (byte-confirmed in `@0x1c8ae5c0`):

| Family | VF opcode(s) | Key type | Key construction | Map |
|---|---|---|---|---|
| matmul | 230 (also 212→fmt1, 218→fmt2) | `MatmulModifier` | byte[0] = format (6 for the plain matmul case), byte[1..] = 0 | `this+0x20` |
| matpush / matprep | 267 | `MatpushModifier` | byte[0]=`GainLatchModeToMatmulDataFormat(latch_mode)`, byte[1..2]=`LatchModeIsTranspose(latch_mode)`, byte[3]=`LatchOpcodeToMsr(0x8F)` | `this+0x00` |
| matpush (xpose pass) | 271 | `MatpushModifier` | as above, with `latch_mode ^= 0xB` (XOR-flip selects the transposed staging path) | `this+0x00` |
| matpush (wide pass) | 277 | `MatpushModifier` | as above, with `latch_mode \|= 0x14` (OR-set selects the x8/wide bucket) | `this+0x00` |
| matres | (matres opcode) | `MatresModifier` | result-chunk index | matres map |
| vlxmr | (vlxmr opcode) | `VlxmrModifier` | latch-into-MRB variant | vlxmr map |

The matpush key is the interesting one: it is assembled from three helpers — `GainLatchModeToMatmulDataFormat` `@0x1d629260` (maps the LLO `GainLatchMode` attribute to a `MatmulDataFormat` code), `LatchModeIsTranspose` `@0x1d628ea0` (the transpose bit), and `LatchOpcodeToMsr(0x8F)` `@0x1c8a1300` (which matrix-staging register the latch targets). The opcode-specific pre-transform of the latch mode (`^0xB` for 271, `|0x14` for 277) is what routes the same physical latch into the transposed or wide reservation bucket. The complete `MatmulMode`/`MatmulDataFormat`/modifier ordinal tables are on [`matmul-mode-modifiers`](matmul-mode-modifiers.md).

> **GOTCHA —** the matmul opcode is **not** stable across generations. Viperfish matches matmul on opcode 230 (`0xE6`) with matmul-format cases 212/218; Ghostlite matches the matmul family on opcode 292 (`0x124`). The opcode numbering shifted between gens, so the family dispatch is per-gen. Bind the family by the per-gen opcode list, not by a hardcoded constant.

---

## The Lookup

### Algorithm

```c
function MxuLatencyTable::GetResourceUsage(instr, resource, is_throughput):  // VF @0x1c8ae5c0
    // 1. Per-resource default fallback, taken before the table lookup.
    if resource == 3:  default_cycle = 15            // VF: latch/issue-slot seed
    elif resource == 11: default_cycle = 0           // VF: matmul-issue slot seed
    else: return InvalidArgument("Unsupported kind of resource")   // vf.cc, status path

    // 2. Select family + build key from the opcode.
    switch (instr.opcode):
        case 212: key = MatmulModifier{format=1};  map = this+0x20      // family = matmul
        case 218: key = MatmulModifier{format=2};  map = this+0x20
        case 230: key = MatmulModifier{format=6};  map = this+0x20
        case 267: key = MatpushKey(latch_mode);            map = this+0x00  // matpush
        case 271: key = MatpushKey(latch_mode ^ 0xB);      map = this+0x00
        case 277: key = MatpushKey(latch_mode | 0x14);     map = this+0x00
        default:  LogFatal("Unsupported opcode")           // vf.cc:578

    // 3. find + bounds-check + read.
    entry = map.find(key)
    if entry not found: throw out_of_range            // raw_hash_map::at
    array<int,N> res_vector = entry.value             // vmovups copy, key-width-dependent offset
    CHECK(resource < N)                               // VF: < 0x13 (19); GL: < 0xB (11)
    return res_vector[resource]                       // the hold-cycle count for this resource
```

The default fallback (step 1) is what makes the table sparse-by-default: for the seed resources the lookup returns a fixed cycle even before consulting any map, and for an unsupported resource it returns an error `Status` rather than reading out of bounds. The bounds-check in step 3 (`v9 >= 0x13` → `BUG()` on VF) is the same `kNumMxuResources` guard `SetReservations` enforces on the write side.

> **GOTCHA —** The per-resource default keys are **per-generation**, not shared. On Viperfish the defaults are `resource==3 → 15` and `resource==11 → 0` (`@0x1c8ae5c0`, `a4==3`/`a4==11`). On Ghostlite they are `resource==4 → 3` and `resource==9 → 9` (`@0x1c8b7560`, `a4==4`/`a4==9`). A reimplementation must read the default-resource keys from the per-gen lookup; the GL/GF integer matrices on their own pages reflect the 11-resource indexing, not the VF 19-resource one.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `viperfish::MxuLatencyTable::MxuLatencyTable` | `0x1c8a52c0` | VF ctor — fills all four family maps (~27 KB) | CERTAIN |
| `viperfish::MxuLatencyTable::GetResourceUsage` | `0x1c8ae5c0` | VF lookup — family dispatch + `find` + `array[resource]` | CERTAIN |
| `ghostlite::MxuLatencyTable::MxuLatencyTable` | `0x1c8b2920` | GL ctor — `array<int,11>` | CERTAIN |
| `ghostlite::MxuLatencyTable::GetResourceUsage` | `0x1c8b7560` | GL lookup — defaults `res4→3`, `res9→9` | CERTAIN |
| TPU7x `MxuLatencyTable::GetResourceUsage` | `0x1c8bdb20` | TPU7x lookup — `array<int,11>`; own `gf.cc`, CHECK `mxu_resource_idx < kNumMxuResources` (11) at gf.cc:415 | CERTAIN |
| `viperfish::SetReservations<MatpushModifier>` | `0x1c8abde0` | densify `{resource→cycles}` → `array<int,19>`, `try_emplace` | CERTAIN |
| `viperfish::SetReservations<VlxmrModifier>` | `0x1c8accc0` | vlxmr family row builder | CERTAIN |
| `viperfish::SetReservations<MatresModifier>` | `0x1c8acea0` | matres family row builder | CERTAIN |
| `viperfish::AddOverrunCheckReservations` | `0x1c8abfe0` | inserts the four `kMsr{A,B}OverrunCheck0..3` slots (cycles `5/13/21/29`); `Msr` arg selects the A-set vs B-set | CERTAIN |
| `GainLatchModeToMatmulDataFormat` | `0x1d629260` | matpush key byte[0] — `GainLatchMode` → format code | CERTAIN |
| `LatchModeIsTranspose` | `0x1d628ea0` | matpush key byte[1..2] — transpose flag | HIGH |
| `LatchOpcodeToMsr` | `0x1c8a1300` | matpush key byte[3] — staging-register selector | HIGH |
| `MxuOpHoldIssues` | `0x1c8ad3a0` | issue-stall recurrence — the table's consumer | HIGH |
| `MxuOpResourceReservations` | `0x1c8ad080` | resource-reservation accumulation over a window | HIGH |

---

## How the Scheduler Consumes the Reservation

The table is read by two methods that turn per-resource hold-cycles into an issue cursor. `MxuOpResourceReservations` `@0x1c8ad080` accumulates, for an instruction window, the per-`MxuResource` reservation each op imposes (calling `GetResourceUsage` per resource). `MxuOpHoldIssues` `@0x1c8ad3a0` then computes how many cycles the *next* MXU op must wait before it can issue, given the resources still held by ops already in flight — the stall recurrence detailed on [`mxu-opholdissues-stall`](mxu-opholdissues-stall.md).

The division of labour mirrors `MCSchedModel`: the base `Performance` grid supplies the *latency* (when the result is ready), and `MxuLatencyTable` supplies the *occupancy* (when the unit is free to accept the next op). For a back-to-back matmul stream the throughput is gated by the longest-held resource, not by the per-op latency: a low-precision push that holds its staging register for only a cycle or two pipelines at near-issue-rate while the multi-hundred-cycle systolic latency is hidden across the array depth, whereas a wide/high-precision push that holds the same register for several cycles throttles the stream proportionally. The overrun-check ladder above (`5/13/21/29`) is the concrete in-binary expression of that growing hold; the full per-gen reservation integers are on the per-gen pages.

> **NOTE —** `AddOverrunCheckReservations` `@0x1c8abfe0` does not add a single cell — it inserts a **bank of four** overrun-check reservations into the per-op `flat_hash_map<MxuResource,int>` before it is densified. For its `Msr` argument it picks one of two banks: `Msr==0` → `kMsrAOverrunCheck0..3`, `Msr==1` → `kMsrBOverrunCheck0..3` (any other value is a `CHECK`-fatal "Invalid MSR." at vf.cc:95). Both banks carry the same cycle ladder `5 / 13 / 21 / 29` (byte-confirmed `try_emplace` literals in `@0x1c8abfe0`, vf.cc:78–93). These slots model the staircase of stall as a matpush overruns the matrix-staging register and the drain depth grows; they are folded into the same map `SetReservations` densifies, so the consumer reads them as ordinary array cells. `kMsrAOverrunCheck0..3` occupy `MxuResource` indices 2–5 and `kMsrBOverrunCheck0..3` occupy 6–9.

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-jf-df` | JF/DF have no `MxuLatencyTable` — occupancy is in the inline 15-field model |
| `mxu-latency-pf` / `-vf` / `-gl` / `-gf` | the per-gen integer reservation matrices indexed by this model |
| `matmul-mode-modifiers` | the modifier ordinals, format codes, and family→reservation bindings |
| `resource-enum` | the higher-level 23-slot `Resource` vector — distinct from `MxuResource` |
| `mxu-opholdissues-stall` | the issue-stall recurrence that consumes the reservation arrays |

---

## Cross-References

- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the 16 `MatmulMode` ordinals, `MatpushModifier`/`MatmulModifier` arrays, `MatmulDataFormat` → reservation-group binding
- [MXU Latency: JF / DF](mxu-latency-jf-df.md) — the generations with no separate reservation table
- [MXU Latency: PF](mxu-latency-pf.md) — Pufferfish reservation matrix
- [MXU Latency: VF](mxu-latency-vf.md) — Viperfish `array<int,19>` integer matrix
- [MXU Latency: GL (Ghostlite)](mxu-latency-gl.md) — Ghostlite `array<int,11>` integer matrix
- [MXU Latency: GF (6acc60406)](mxu-latency-gf.md) — TPU7x `array<int,11>` integer matrix
- [MxuOpHoldIssues Stall Recurrence](mxu-opholdissues-stall.md) — the consumer that turns reservations into issue stalls
- [Resource Enum (23-slot)](resource-enum.md) — the higher-level `Resource` vector, not the MXU-internal `MxuResource`
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot whose ops this table prices
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the matprep/latch ops behind the matpush modifier family
- [Performance Overview](performance-overview.md) — the base per-opcode latency grid this table augments
- [CycleTable Family](cycletable-family.md) — per-gen cost-model subclass family; the `LatencyTable` `+0x1d8` table slot
