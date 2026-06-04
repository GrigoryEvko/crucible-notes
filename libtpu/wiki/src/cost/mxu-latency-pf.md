# MXU Latency: PF

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

This page documents how **Pufferfish (v4)** prices MXU pipeline occupancy — and the headline finding is a structural one: **Pufferfish has no separate `MxuLatencyTable`.** The reservation-matrix object that Viperfish, Ghostlite, and `6acc60406` each heap-allocate at their `LatencyTable` `+0x1d8` slot — a `flat_hash_map<Modifier, array<int,N>>` per MXU op family, indexed by a modifier key built from `MatmulDataFormat` — does **not** exist in the Pufferfish cost model. There is no `xla::pufferfish::MxuLatencyTable` class in the binary; the only `MxuLatencyTable` types are `xla::viperfish::`, `xla::ghostlite::`, and the anonymous-namespace `6acc60406` variant (lookup `@0x1c8bdb20`, `gf.cc`). Pufferfish therefore behaves like Jellyfish/Dragonfish: it folds MXU occupancy into its other cost objects rather than into a dedicated reservation table.

Concretely, Pufferfish prices MXU occupancy in two places. First, the **matmul throughput** lives as a column in the `PufferfishPerformance` grid: res 9, a single 96-cell column carrying value-set `{8, 16}` across the entire matmul band (`Instruction` 0x7a..0xd9), read by the byte-identical grid `GetResourceUsage` `@0x1c8c3880`. Second, the **transpose / cross-lane (Xlu) reservation** is priced by a separate `XluConflictPenaltyTable` — a 3-axis integer table embedded in `LatencyTablePufferfish` at `+0x18`, read through `XposeXLUReservationLatency` `@0x1c8a13e0`. This is exactly the split the [`performance-pf`](performance-pf.md) page describes from the grid side; this page describes it from the MXU-occupancy side and states why the per-`(MatmulMode × MatmulDataFormat)` reservation-matrix bodies the task brief asks for do not exist for v4.

The reference frame is the rest of the family. On Viperfish a matpush is reduced to a `MatpushModifier` key, looked up in a `flat_hash_map`, and yields a dense `array<int,19>` of per-`MxuResource` hold cycles ({2,1,1} bf16 → {8,7,6} int8-x8). On Pufferfish the same physical occupancy is expressed through the grid's matmul-throughput cells (8 narrow / 16 wide) plus the conflict-penalty matrix — the reservation **map** layer is simply absent. A reimplementation of the v4 cost model must not build an `MxuLatencyTable`; it must price MXU ops through `PufferfishPerformance` (res 9) and `XluConflictPenaltyTable`.

For reimplementation, the contract is:

- That Pufferfish carries **no** `MxuLatencyTable` object (no `this+0x00` matpush map, no `this+0x20` matmul map) — the brief's reservation-matrix layout describes Viperfish, not PF.
- Where PF actually prices MXU throughput: `PufferfishPerformance` res 9 ({8,16}) for matmul, the matprep band (res 10/11) for the latch, res 6 (`kVectorMatres`) for matrix-result deposit.
- The `XluConflictPenaltyTable` (3-axis `[XluInstrType][6][3]`) and `XposeXLUReservationLatency` that price the transpose/Xlu reservation in place of a grid column or a reservation map.
- The `MxuResource` column count question: PF has no `MxuResource` enum at all; its MXU columns are `PufferfishPerformance::Resource` ordinals within the 20-wide grid.

| | |
|---|---|
| **`MxuLatencyTable`** | **none** — no `xla::pufferfish::MxuLatencyTable` symbol exists in the binary |
| **MXU throughput** | `PufferfishPerformance` grid res 9 (96 cells, value-set `{8,16}`) `@0x1c8c3880` |
| **Matprep throughput** | grid res 10 (value 7), res 11/12 (transpose-binary / matprep result) |
| **Matrix-result deposit** | grid res 6 = `kVectorMatres` (0x77), value 8 |
| **Xlu / transpose pricing** | `XluConflictPenaltyTable` (3-axis) via `XposeXLUReservationLatency` `@0x1c8a13e0` |
| **Penalty table** | `LatencyTablePufferfish` `+0x18`; 10 `SetXluConflictPenaltyBetween` installs in ctor `@0x1c8a1960` |
| **`MxuResource` count** | n/a — PF uses `PufferfishPerformance::Resource` (20 columns), not a per-gen `MxuResource` enum |
| **Generation** | Pufferfish, TpuVersion 2 / v4 (`LatencyTablePufferfish`, object 0x1e0 B) |

---

## Pufferfish Has No MxuLatencyTable

Pufferfish does **not** own an `MxuLatencyTable`. `LatencyTablePufferfish` (ctor `@0x1c8a1960`) allocates none: it stores `GetSharedPufferfishPerformance` at `[this+0x1d0]` and `GetSharedPufferfishBarnaCorePerformance` at `[this+0x1d8]` (two `Performance` grids, not a reservation map), and builds an inline `XluConflictPenaltyTable` at `[this+0x18]`. No `xla::pufferfish::MxuLatencyTable` class, ctor, or `GetResourceUsage` exists anywhere in the symbol table.

> **GOTCHA —** The reservation-matrix model (matpush map at `this+0x00`, matmul map at `this+0x20`, `array<int,19>` bodies, the `{2,1,1}/{4,3,2}/{8,7,6}` value-sets) is the **Viperfish** model ([`mxu-latency-vf`](mxu-latency-vf.md)); it first appears at v5p, not v4. Viperfish, Ghostlite, and `6acc60406` carry the table; Jellyfish, Dragonfish, **and Pufferfish** do not. Do not assume the `LatencyTable + 0x1d8` slot is a reservation map on PF — it is a second `Performance` grid.

### What the decompile shows

The Pufferfish `LatencyTable` ctor zero-fills its 0x1e0-byte body, sets the vtable, then wires two `Performance` singletons and a conflict table — there is no map construction, no `SetReservations`, no `flat_hash_map` of modifier keys:

```c
function LatencyTablePufferfish(this, version):              // @0x1c8a1960
    LatencyTable_base(this, version)                          // zero 0x1e0 body, set vptr off_21C20330
    [this+0x1d0] = GetSharedPufferfishPerformance()           // TensorCore grid (pf_shared @0x22579a10)
    [this+0x1d8] = GetSharedPufferfishBarnaCorePerformance()  // BarnaCore grid (pf_bc_shared)
    // inline std::vector<int>-shaped scratch at +0x20/+0x74/+0xC8/+0x11C/+0x168/+0x1BC (size/cap 1 or 9)
    InitializeConflictLatency(this)
    SetXluConflictPenaltyBetween(this, 0, 2, 0, 56)           // 10 penalty installs into the +0x18 table
    SetXluConflictPenaltyBetween(this, 5, 2, 0, 46)
    SetXluConflictPenaltyBetween(this, 0, 5, 0, 17)
    SetXluConflictPenaltyBetween(this, 2, 5, 0, 96)
    SetXluConflictPenaltyBetween(this, 2, 0, 0, 86)
    SetXluConflictPenaltyBetween(this, 0, 2, 1, 56)           // second hi-plane duplicates the first five
    SetXluConflictPenaltyBetween(this, 5, 2, 1, 46)
    SetXluConflictPenaltyBetween(this, 0, 5, 1, 17)
    SetXluConflictPenaltyBetween(this, 2, 5, 1, 96)
    SetXluConflictPenaltyBetween(this, 2, 0, 1, 86)
```

Compare the Viperfish ctor (`@0x1c8a52c0`, ~27 KB), which `new`s an `MxuLatencyTable` and fills four `flat_hash_map`s via `SetReservations<MatpushModifier>`/`<MatmulModifier>`/`<MatresModifier>`/`<VlxmrModifier>`. Pufferfish's ctor is two orders of magnitude smaller because it has no such maps to build. The MXU occupancy is already baked into the `PufferfishPerformance` grid the ctor merely points at.

> **NOTE —** the inline `std::vector<int>`-shaped fields the PF ctor zero-fills at `+0x20`, `+0x74`, `+0xC8`, `+0x11C`, `+0x168`, `+0x1BC` (each set to `{ptr=0, size=cap=1}` or `{..=9}`) are the base `LatencyTable`'s per-XLU scratch vectors, not MXU reservation maps. They hold `{1,1,1,9,1,1}` size/cap seeds — the `xlu_count=2` Pufferfish XLU-edge scratch — and are unrelated to the matmul/matpush reservation families of the v5p+ `MxuLatencyTable`.

---

## Where Pufferfish Prices MXU Occupancy

### Matmul and matprep throughput — the grid

The matmul throughput that Viperfish stores in its `MatmulModifier` reservation array, Pufferfish stores as grid cells. `PufferfishPerformance::GetResourceUsage(instr, res)` `@0x1c8c3880` reads `grid[instr][res]`; the matmul band occupies res 9:

| MXU role | Grid column | Cells | Value(s) | Meaning |
|---|---|---:|---|---|
| matmul throughput | res 9 | 96 | `{8, 16}` | per-issue hold; 8 = narrow format, 16 = wide |
| matprep throughput | res 10 | 20 | `7` | per-issue matprep latch hold |
| matprep / transpose result A | res 11 | 54 | `{1, 8, 16}` | matprep + transpose-binary + permute/rotate result |
| transpose / permute result B | res 12 | 34 | `{1, 8, 16}` | second transpose/permute result stage |
| matrix-result (Xlu) deposit | res 6 | 1 | `8` | `kVectorMatres` (0x77) — the conv `R[2]` analog |

The single value-set `{8, 16}` on res 9 is the PF counterpart of the VF `{2,1,1}/{4,3,2}/{8,7,6}` reservation triplets: the narrow (bf16-class) matmul holds the throughput port 8 cycles, the wide (int8/x8-class) matmul holds it 16. There are no separate matpush/matmul/matres/vlxmr maps because the grid's outer `Instruction` axis already fans the matmul/matprep opcode out across the band per data format (via the latch-mode WORD table in `GetPufferfishInstruction`). The matmul base **latency** (the two byte-confirmed format depths 83 and 101 at the head of the band, `latency[0x7A]=83`; the narrow/wide assignment of the two is inferred, not byte-enumerated — see [`performance-pf`](performance-pf.md)) is the total op depth; the res-9 cell (8 / 16) is the throughput hold that gates back-to-back issue.

> **QUIRK —** the matmul-throughput representation widens generation by generation, and Pufferfish is the narrowest: a single res-9 column (96 cells) on PF, a res-3 column plus a 4-stage matprep group (res 4..7) on VF, the wider EUP-AndPop FIFO on GL/GF. The same physical occupancy is one column on PF and seven on VF. A reimplementation that ports the VF multi-stage matprep group onto PF will model resource ports that PF's 20-column grid does not have.

### Transpose / Xlu reservation — the conflict-penalty table

The transpose and cross-lane (Xlu) reservation that VF/GL/GF read from an Xlu grid column (`GetXluPathReservation`), Pufferfish prices from a dedicated `XluConflictPenaltyTable`. `XposeXLUReservationLatency` `@0x1c8a13e0`:

```c
function XposeXLUReservationLatency(this, mode, earlier, later, a, b):  // @0x1c8a13e0
    CHECK(IsTranspose(earlier))                          // latency_table_pf.cc:62
    v = (b - a) + XluConflictPenaltyBetween(&this.penalty /* +0x18 */, earlier, later_lo, later_hi)
    if v < -5: v = -6
    return v + 7
```

`XluConflictPenaltyBetween` `@0x1c8a0180` is a 3-axis lookup — `penalty[XluInstrType][lo<6][hi<3]`, stride `72*type + 12*lo + 4*hi + 8`, returning the stored `penalty + 1`. `IsTranspose(type)` is `(type - 2) < 3`, so `XluInstrType` ordinals `{2,3,4}` are the transpose ops; the `CHECK` enforces that the *earlier* op in a pair is a transpose. The 10 penalty pairs the ctor installs price the conflict cycles between a transpose-result producer and a later Xlu consumer (`(0→2)=56`, `(2→5)=96`, `(2→0)=86`, `(5→2)=46`, `(0→5)=17`, each duplicated across the two `hi` planes).

| Accessor | Address | Role | Confidence |
|---|---|---|---|
| `XposeXLUReservationLatency` | `0x1c8a13e0` | transpose/Xlu reservation = `(b−a) + penalty + 7`, clamped | CERTAIN |
| `LatencyBetweenXposeInstrAndResult` | `0x1c8a1520` | transpose-instr → result edge via the penalty table | HIGH |
| `XluConflictPenaltyBetween` | `0x1c8a0180` | 3-axis read `[type][6][3]`, returns `penalty+1` | CERTAIN |
| `XluConflictPenaltyTable::IsTranspose` | `0x1c8a04e0` | `(type − 2) < 3` → types {2,3,4} | CERTAIN |
| `SetXluConflictPenaltyBetween` (PF) | `0x1c8a17e0` | installs a penalty; `CHECK(!IsPacked(earlier) && !IsPacked(later))` | CERTAIN |

> **GOTCHA —** the conflict-penalty model is **directional and pairwise**, not a per-op reservation. A VF/GL/GF `GetXluPathReservation` returns one number for one op (the cycles it holds the Xlu deposit port); the PF `XluConflictPenaltyBetween` returns the penalty *between* an earlier transpose and a later Xlu op. A reimplementation that maps the PF penalty matrix onto a single-op Xlu reservation will lose the conflict structure entirely — PF models the Xlu hazard as a producer→consumer edge, the later gens as a port hold.

---

## The MxuResource Question

The brief asks for the PF `MxuResource` column count, by analogy with Viperfish's `array<int,19>` and Ghostlite's `array<int,11>`. **Pufferfish has no `MxuResource` enum.** The `MxuResource` enum is the value-array index space of the `MxuLatencyTable`, and PF has no `MxuLatencyTable`. The MXU sub-units are instead columns of the `PufferfishPerformance::Resource` enum — the 20-wide grid axis ([`performance-pf`](performance-pf.md)) — where res 9 is matmul throughput, res 10 matprep, res 6 matrix-result deposit. There is no `kNumMxuResources` CHECK constant for PF (the `0x13`=19 / `0xB`=11 bounds that gate the VF/GL lookups), because there is no per-MxuResource read to bound.

So the resource-count answer for Pufferfish is: the MXU occupancy lives in the **20-column** `PufferfishPerformance::Resource` grid, not in a separate `MxuResource`-indexed reservation array. The `MxuResource` model begins at Viperfish.

| Gen | `MxuLatencyTable`? | `MxuResource` count | MXU throughput home |
|---|---|---|---|
| Jellyfish / Dragonfish | no | n/a | inline 15-field latency model |
| **Pufferfish** | **no** | **n/a** | **`PufferfishPerformance` grid res 9 + `XluConflictPenaltyTable`** |
| Viperfish | yes | 19 (`array<int,19>`) | `MxuLatencyTable` `@this+0x1d8` |
| Ghostlite | yes | 11 (`array<int,11>`) | `MxuLatencyTable` `@this+0x1d8` |
| `6acc60406` (TPU7x) | yes | 11 (`array<int,11>`) | `MxuLatencyTable` `@this+0x1d8` (anon-ns, lookup `@0x1c8bdb20`) |

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LatencyTablePufferfish::LatencyTablePufferfish` | `0x1c8a1960` | ctor — wires both `Performance` grids + the conflict table; **no** `MxuLatencyTable` | CERTAIN |
| `PufferfishPerformance::GetResourceUsage` | `0x1c8c3880` | the MXU throughput read — `grid[instr][res 9]` etc. | CERTAIN |
| `PufferfishPerformance::PufferfishPerformance` | `0x1c8be080` | the grid ctor — res 9 matmul column (96 cells, `{8,16}`) | CERTAIN |
| `XposeXLUReservationLatency` | `0x1c8a13e0` | the transpose/Xlu reservation in place of a reservation map | CERTAIN |
| `XluConflictPenaltyBetween` | `0x1c8a0180` | 3-axis penalty table read | CERTAIN |
| `SetXluConflictPenaltyBetween` (PF) | `0x1c8a17e0` | the 10 ctor penalty installs | CERTAIN |
| `ResourceUsageFromInstruction` (variant 0/1) | `0x1c8a3180` / `0x1c8a31a0` | TC/BarnaCore grid dispatch — the analog of an `MxuLatencyTable` family select | CERTAIN |
| `GetSharedPufferfishPerformance` singleton | `0x22579a10` | the grid PF prices MXU ops through | CERTAIN |
| `viperfish::MxuLatencyTable::MxuLatencyTable` | `0x1c8a52c0` | the v5p reservation-table ctor PF does **not** have — for contrast | CERTAIN |

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-overview` | the per-gen `MxuLatencyTable` model — PF is the v4 exception that lacks it |
| `performance-pf` | the `PufferfishPerformance` grid that prices PF MXU throughput (res 9) + the conflict table |
| `mxu-latency-vf` | the Viperfish `array<int,19>` reservation matrix — the model PF predates |
| `matmul-mode-modifiers` | the `MatmulDataFormat` codes; on PF they select the grid band ordinal, not a modifier key |
| `resource-enum` | the 23-slot per-bundle `ResourceVector`, distinct from the PF 20-column grid |

---

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the per-gen `MxuLatencyTable` reservation model; Pufferfish is the v4 generation that, like JF/DF, lacks it
- [Performance: PF](performance-pf.md) — the `PufferfishPerformance` grid (res 9 matmul throughput, res 6 matrix-result) and the `XluConflictPenaltyTable` that together carry PF MXU occupancy
- [MXU Latency: VF](mxu-latency-vf.md) — the Viperfish `array<int,19>` reservation matrix and the `{2,1,1}/{4,3,2}/{8,7,6}` value-sets first introduced at v5p
- [MXU Latency: GL (Ghostlite)](mxu-latency-gl.md) — the Ghostlite `array<int,11>` reservation matrix
- [MXU Latency: GF (6acc60406)](mxu-latency-gf.md) — the `6acc60406` `array<int,11>` reservation matrix (lookup `@0x1c8bdb20`)
- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the `MatmulDataFormat` codes; on later gens they build the reservation-map key, on PF they pick the grid band ordinal
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot whose ops PF prices through the grid
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the latch/matprep ops behind the matpush family the later-gen tables key on
