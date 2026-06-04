# Performance: PF

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`PufferfishPerformance` is the **first** of the heap-grid `Performance` objects — the architecture that Pufferfish (v4) introduced and that Viperfish/Ghostlite/`6acc60406` inherit. Where the older Jellyfish/Dragonfish family stores its costs in a single inline POD with an offset LUT, Pufferfish heap-allocates two objects: a flat per-instruction **latency array** (336 `int32`, indexed by `Performance::Instruction`) and a two-dimensional **Instruction × Resource** occupancy grid (336 rows × 20-wide `std::vector<int>`). `GetLatency(instr)` reads the array; `GetResourceUsage(instr, res)` reads `grid[instr][res]` — the cycle count for which that instruction holds micro-pipeline port `res`. This is the libtpu analog of an LLVM `SchedMachineModel`, recovered by decoding the constructor that fills it (`PufferfishPerformanceC1` `@0x1c8be080`) rather than read from a `.td` file.

Two facts make Pufferfish the pivot of the family. First, its read path — outer bound, 24-byte row stride, inner bound, `row.data[res]` — is **byte-identical** to the Viperfish/Ghostlite/`6acc60406` `GetResourceUsage`; one description ([`performance-overview`](performance-overview.md)) covers all four grid generations, and this page supplies only the PF-specific widths, cells, and the pre-Ghostlite column layout. Second, Pufferfish is the last generation with a **BarnaCore** embedding engine: `LatencyTablePufferfish` prices a `variant<PufferfishPerformance, PufferfishBarnaCorePerformance>`, so an instruction is dispatched to one of two distinct grids by its variant tag. The TensorCore grid (variant 0) is what this page dumps; the BarnaCore grid (variant 1, `PufferfishBarnaCorePerformance`) is a separate 0x30-byte object reached through the same `ResourceUsageFromInstruction` visitor.

The PF grid has 20 resource columns — the narrowest of the grid family (the progression is 20 → 28 → 31 → 31 across PF → VF → GL → GF). This page documents the 20-column layout named by occupant LLO class, the latency-array value distribution, the EUP push/pop occupancy, and the one structural divergence from every later gen: Pufferfish has **no direct Xlu grid column** and **no separate `MxuLatencyTable`** — its matmul/transpose occupancy is priced by the grid's matmul-throughput column (res 9) plus a separate `XluConflictPenaltyTable`. See [`mxu-latency-pf`](mxu-latency-pf.md) for that divergence in full.

For reimplementation, the contract is:

- The two-object layout: `int32 latency[336]` heap array + a 336-row grid of 20-wide `std::vector<int>` rows, with exact `new` sizes and field offsets.
- The `GetResourceUsage(instr, res)` / `GetLatency(instr)` read paths and their two bounds checks.
- The 20-column `Performance::Resource` layout, named by the LLO instruction class that deposits into each — in particular res 9 (matmul throughput) and res 6 (matrix-result/Xlu deposit).
- How an LLO opcode reaches a grid row through `GetPufferfishInstruction`, and how the MXU band fans a single matmul/matprep opcode out to many ordinals.
- The BarnaCore variant: the second grid (`PufferfishBarnaCorePerformance`) the same `LatencyTablePufferfish` prices.

| | |
|---|---|
| **Class** | `xla::pufferfish::PufferfishPerformance` (non-polymorphic value type) |
| **Ctor** | `PufferfishPerformanceC1` `@0x1c8be080` (object 0x30 B; ~2376-line ctor) |
| **Latency array** | `new 0x540` = 1344 B = 336 `int32`, `memset(_, 0xff, 1344)` then every slot overwritten |
| **Grid** | `new 0x1f80` = 8064 B = 336 rows × 24 B (`std::vector<int>`), each row `new 0x50` = 20 `int32` |
| **Read path** | `GetResourceUsage` `@0x1c8c3880` · `GetLatency` `@0x1c8c3860` · `GetResources` `@0x1c8c3840` |
| **Resource count** | 20 (`kResources` `@0xb43cd94`, 20-byte permutation of `{0..19}`) |
| **Populated cells** | 265 grid cells across 180 rows; all 336 latency slots written |
| **Classifier** | `GetPufferfishInstruction` `@0x1c8a1fe0` (LloValue → `Instruction`, variant-tagged) |
| **Xlu pricing** | res 6 (`kVectorMatres`) + `XluConflictPenaltyTable` via `XposeXLUReservationLatency` `@0x1c8a13e0` — **no** direct Xlu grid column |
| **Singleton** | `GetSharedPufferfishPerformance::pf_shared` `@0x22579a10` |

---

## Object Layout

### Purpose

`PufferfishPerformance` answers the scheduler's two per-instruction questions — pipeline depth (`GetLatency`, raised on a true-dependency edge) and per-port occupancy (`GetResourceUsage`, accumulated into per-bundle throughput pressure). It stores both in heap objects so the same class shape scales to wider resource sets in later gens.

### Structure

The constructor `@0x1c8be080` allocates and fills two heap regions. The object itself is small — `operator new(0x30)` in `LatencyTablePufferfish` — holding only the array/grid pointers and their counts:

```c
struct PufferfishPerformance {        // object 0x30 B; built by PufferfishPerformanceC1 @0x1c8be080
    int32*       latency;             // +0x00 ; new 0x540 = 336 int32, memset 0xff
    u64          latency_size;        // +0x08 ; = 336
    u64          latency_cap;         // +0x10 ; = 336
    vector<int>* grid;                // +0x18 ; new 0x1f80 = 336 × 24-B vector<int>
    u64          grid_outer_count;    // +0x20 ; = 336  (the GetResourceUsage outer bound)
    u64          grid_outer_cap;      // +0x28 ; = 336
    // each row is a 24-B std::vector<int>: { int* data (new 0x50 = 20 int32, zero-init), size=20, cap=20 }
};
```

The row-allocation loop is byte-exact in the ctor: `for (i = 16; i != 8080; i += 24)` allocates `operator new(0x50)` per row, zero-fills it (`vmovups` of `ymm0` over the 80 bytes), and sets `[row+8] = [row-8+24] = 20` (the `std::vector<int>` size and capacity). The latency array is `memset` to `0xff` (255) and then **every** slot is overwritten — unlike Ghostlite/`6acc60406` where the `0xff` default survives on unpriced rows. So a reimplementation must write all 336 PF latency slots; none fall back to a sentinel.

> **NOTE —** the latency-array `memset` value is `0xff` here (255), confirmed by `memset(v4, 255, 1344)` in the ctor — distinct from the flat Jellyfish family, whose POD is memset to the `0x7fffffff` INT_MAX sentinel. On PF the sentinel never reaches a consumer because all 336 slots are written.

### Sample cells (byte-anchored)

The ctor writes latency by `[latency + 4*instr]` and grid cells by `[grid + 24*instr]` then `[row.data + 4*res]`. Three cells fix the layout against the raw bytes:

```text
latency[0x67] = 7   ; store [latency + 0x19C], 0x19C = 0x67*4    (rsqrt EUP push)
latency[0x7A] = 83  ; store [latency + 0x1E8], 0x1E8 = 0x7A*4    (matmul band base, format A)
grid[0x67][2] = 1   ; row 0x9A8 = 0x67*24, [row.data + 8]  = res 2 (EUP-prep stage A)
grid[0x67][3] = 2   ; row 0x9A8,           [row.data + 12] = res 3 (EUP-prep stage B)
grid[0x7A][9] = 8   ; row 0xB70 = 0x7A*24, [row.data + 36] = res 9 (matmul throughput)
```

---

## The Read Path

### Algorithm

`GetResourceUsage` `@0x1c8c3880` is the single accessor for a grid cell. It takes the `Instruction` ordinal and the `Resource` column, bounds-checks both, computes the 24-byte row stride with a `lea`, and reads the cell:

```c
function PufferfishPerformance_GetResourceUsage(perf, instr, res):   // @0x1c8c3880
    if perf.grid_outer_count <= instr:        // [perf+0x20] = 336 ; outer bound
        BUG()                                  // trap (ud1 / BUG)
    row = perf.grid                            // [perf+0x18]
    v4 = 3 * instr                             // row = grid + instr*24  (3*instr then *8)
    if row[v4*8 + 8].size <= res:              // [row+8] = inner bound = 20
        BUG()
    return *(int*)(row[v4*8].data + 4*res)     // grid[instr][res]
```

```c
function PufferfishPerformance_GetLatency(perf, instr):   // @0x1c8c3860
    if perf.latency_size <= instr:             // [perf+0x8] = 336
        BUG()
    return *(int*)(perf.latency + 4*instr)     // latency[instr]
```

Both bounds checks trap (`BUG()`) on overflow rather than returning a default — there is no safe out-of-range read. `GetResources` `@0x1c8c3840` returns the `kResources` traversal order `@0xb43cd94` (20 bytes, a permutation of `{0..19}`), the order in which a row is iterated to sum its per-port holds.

> **GOTCHA —** the outer index is the per-gen `Performance::Instruction`, **not** the raw LLO opcode. `GetPufferfishInstruction` `@0x1c8a1fe0` classifies an `LloValue` into the `Instruction` ordinal (jump table `@0xb43927c`, index = opcode − 2), and the MXU band fans a single matmul/matprep opcode out to ~0x60 ordinals via a secondary latch-mode WORD table. A reimplementation that indexes the grid directly by LLO opcode mis-reads every MXU row.

### The variant dispatch — TensorCore vs BarnaCore

`LatencyTablePufferfish` does not hold one `Performance`; it holds two singletons — `pf_shared` (`PufferfishPerformance`, `[table+0x1d0]`) and `pf_bc_shared` (`PufferfishBarnaCorePerformance`, `[table+0x1d8]`) — and prices an instruction through a `variant<PufferfishPerformance::Instruction, PufferfishBarnaCorePerformance::Instruction>`. `ResourceUsageFromInstruction` dispatches the variant:

```c
// variant index 0 — TensorCore op  (@0x1c8a3180)
return PufferfishPerformance::GetResourceUsage(table.pf_shared, instr, res);

// variant index 1 — BarnaCore op   (@0x1c8a31a0)
if (instr.tag == 2):
    return PufferfishBarnaCorePerformance::GetResourceUsage(table.pf_bc_shared, instr, 0);
else:
    return 0;
```

So a single PF cost query routes to one of two grids by variant tag. This page documents variant 0 (the TensorCore grid). The BarnaCore grid (`PufferfishBarnaCorePerformance::GetResourceUsage` `@0x1c8c4800`, ctor near `@0x1c8c38c0`) is a separate 0x30-byte object with its own latency array and grid; it is the last generation's embedding-engine cost model and is not dumped here.

> **QUIRK —** Pufferfish is the **only** grid generation that prices two distinct cores from one `LatencyTable`. Viperfish onward have a single `Performance` per gen. A reimplementation that models PF with one grid will silently mis-cost the 13 BarnaCore (variant-1) opcodes that `GetPufferfishInstruction` tags with high-16 = 1.

---

## The 20 Resource Columns

### Purpose

The grid's inner axis is the `PufferfishPerformance::Resource` enum — the intra-op micro-pipeline reservation ports (EUP-prep, matmul/matprep throughput, cross-lane/transpose result, sync, etc.). The enum has **no** `ToString` in the binary, so the columns are named functionally: by reading which LLO-instruction class deposits cycles into each (the outer index being the opcode) and anchoring against the named accessor `XposeXLUReservationLatency`. These names are reimplementation-grade in *meaning* (which physical port each column reserves), not literal symbol strings.

> **NOTE —** this 20-column `Performance::Resource` enum is a **different, lower-level** enum than the 23-slot per-bundle `ResourceVector` ([`resource-enum`](resource-enum.md)). The grid prices intra-op micro-pipeline-stage holds; the `ResourceVector` is the per-bundle functional-unit accumulator the higher-level cost model deposits into. `kResources` `@0xb43cd94` gives the grid's column traversal order, not the `ResourceVector` slot order.

### The columns

`kResources` `@0xb43cd94` lists the 20 columns in fill order `13 11 08 00 05 02 03 12 0a 06 09 0e 04 01 0b 10 07 0c 0d 0f` (all of `{0..19}` exactly once). Naming each by its dominant occupant LLO class (cell count, value-set, occupant):

| Col | Cells | Value(s) | Occupant LLO band (classifier-named) | Physical port (functional) | Confidence |
|---|---|---|---|---|---|
| r0 | 1 | 5 | `kDma` (instr 0x32) | DMA address/issue port A | HIGH |
| r1 | 1 | 5 | `kDma` (instr 0x32) | DMA address/issue port B | HIGH |
| r2 | 6 | 1 | EUP push 0x67..0x6c (rsqrt/pow2/log2/tanh/recip/erf) | EUP transcendental-prep stage A | CERTAIN |
| r3 | 6 | 2 | same EUP push band | EUP transcendental-prep stage B | CERTAIN |
| r4 | 2 | 7, 9 | `kVectorSetRngSeed` (0x6d), `kVectorPrng` (0x6f) | RNG seed / PRNG setup port | HIGH |
| r5 | 1 | 1 | `kVectorEupResult` (0x76 = EUP pop) | EUP-result-pop drain port | CERTAIN |
| r6 | 1 | 8 | **`kVectorMatres` (0x77)** | **matrix-result (Xlu) deposit port** | CERTAIN |
| r7 | 1 | 8 | `kVectorXlaneResult`/permute/transpose-result (0x78) | cross-lane / transpose result port | HIGH |
| r8 | 1 | 2 | `kVectorCmemResult` (0x79) | Cmem-result port | HIGH |
| r9 | 96 | 8, 16 | MXU matmul band (0x7a..0xd9; latch-mode ords) | **MXU matmul throughput port** (×96) | CERTAIN |
| r10 | 20 | 7 | MXU matprep band (0xdc..0xef) | MXU matprep throughput port | HIGH |
| r11 | 54 | 1, 8, 16 | matprep; `SetSegmentPattern`(0xf3); transpose-binary(0xfc); permute(0x104); rotate(0x106) | matprep / transpose-binary result A | HIGH |
| r12 | 34 | 1, 8, 16 | same transpose/permute/rotate band | transpose / permute result B | HIGH |
| r13 | 4 | 47, 55 | `kVectorPermute`(0x104), `kVectorRotate`(0x106) | permute/rotate extended-result stage | MEDIUM |
| r14 | 10 | 18 | `kVector{Add,Max,Min,..}ReduceF32` (0x108..0x111) | reduce-result stage A | HIGH |
| r15 | 10 | 57 | same reduce band | reduce-result stage B | HIGH |
| r16 | 11 | 1 | `kVectorSyncFlag{Set,Add}*` / `kVectorWait*` (0x113..) | sync-flag / wait port | HIGH |
| r17 | 2 | 4 | `kVectorCmemStore` (0x135..0x136) | Cmem-store port | MEDIUM |
| r18 | 3 | 4 | `kVectorSetIar{Lane,Sublane,Raw}` (0x14b..0x14d) | SetIar (index-addr-reg) port | MEDIUM |
| r19 | 1 | 2 | `kVectorCmemLoad` (0x14f) | Cmem-load port | MEDIUM |

Per-column cell counts (sum 265): `r0:1 r1:1 r2:6 r3:6 r4:2 r5:1 r6:1 r7:1 r8:1 r9:96 r10:20 r11:54 r12:34 r13:4 r14:10 r15:10 r16:11 r17:2 r18:3 r19:1`.

### The matmul-throughput column (res 9) and the matrix-result column (res 6)

Res 9 is the single matmul-throughput port — 96 cells, value-set `{8, 16}`, occupying the entire MXU matmul band (`Instruction` 0x7a..0xd9). The whole per-format matmul throughput collapses into this one column on PF; the later gens spread it across a multi-stage matprep group (VF res 3 + r4..r7, GL/GF the wider EUP-AndPop FIFO). The cell value (8 for the narrow format, 16 for the wide) is the per-issue throughput hold a matmul of that format imposes.

Res 6 is the matrix-result deposit (`kVectorMatres` 0x77, value 8) — the PF analog of the conv `R[2]` Xlu term. It is the deposit column, but PF does **not** read it through a `GetXluPathReservation` accessor the way VF/GL/GF read their Xlu column. Instead, the conv/transpose Xlu reservation is priced by a separate `XluConflictPenaltyTable` — the structural divergence detailed below and on [`mxu-latency-pf`](mxu-latency-pf.md).

> **QUIRK —** the MXU matmul band ordinals 0x7a..0xd9 carry **no** jump-table arm in `GetPufferfishInstruction` — they are reached only via the secondary latch-mode WORD table that fans a single matmul/matprep opcode out across the band. The grid rows exist and carry cells, but the row identity is the `(matmul-opcode, GainLatchMode)` pair, not a directly-classified opcode. A reimplementation must reproduce the latch-mode fan-out, not just the direct-arm classifier.

---

## The Latency Array

Both the latency array and the EUP push/pop edge are read straight from the ctor stores. The 336-entry array has 14 distinct values:

| Latency | Count | Meaning |
|---:|---:|---|
| 1 | 145 | cheap vector / scalar ops |
| 83 | 48 | MXU matmul base latency, format A |
| 101 | 48 | MXU matmul base latency, format B |
| 2 | 30 | sync / wait |
| 7 | 26 | EUP push + matprep |
| 126 | 16 | transpose-binary |
| 79 | 10 | reduce |
| 5 | 4 | (mid-cost) |
| 4 | 2 | (mid-cost) |
| 69 | 2 | permute |
| 77 | 2 | rotate |
| 3 | 1 | (single op) |
| 30 | 1 | (single mid-cost op) |
| 53 | 1 | Cmem-load |

The two MXU base latencies — 83 and 101 — are the two matmul data-format pipeline depths (the format-A vs format-B path); they sit at the head of the matmul band (`latency[0x7A]=83`, byte-confirmed). The EUP push latency is uniform at 7 across all six classified F32 EUP functions (rsqrt = pow2 = log2 = tanh = recip = pushErf, `Instruction` 0x67..0x6c); the EUP pop (`Instruction` 0x76) is 1.

### The EUP push grid occupancy

Pufferfish is distinctive in that its EUP push reserves **grid** ports in addition to its 7-cycle latency: each EUP push row writes `grid[push][2] = 1` and `grid[push][3] = 2` — two EUP-prep micro-pipeline ports (stage A and stage B). Viperfish, by contrast, prices its EUP push (latency 6) through the latency array alone and reserves no grid cells. The PF EUP unit is also half-rate: `PufferfishTarget::VectorEupReservationCycles` `@0x1d494cc0` returns 2 (VF returns 1).

| Gen | EUP push `Instruction` | Push latency | Pop `Instruction` | Pop latency | Push grid occupancy | `VectorEupReservationCycles` |
|---|---|---:|---|---:|---|---:|
| PF | 0x67..0x6c | 7 | 0x76 | 1 | r2:1, r3:2 | 2 (half-rate) |
| VF | 0xcc..0xd2 | 6 | 0x168 | 1 | (none) | 1 |

> **NOTE —** the push→pop dependency edge weight is `GetLatency(push) = 7` on PF, returned **unmodified** — it is *not* scaled by `VectorEupReservationCycles`. The reservation (PF = 2, the half-rate EUP issue rate) is an orthogonal axis bounding push→push spacing, not the push→pop window; the two compose as a `max`, never a product (see [EUP Latency Overview](eup-latency-overview.md) and [EUP Per-Gen Integers](eup-per-gen-integers.md)). On VF the edge is 6 with reservation 1. A reimplementation that multiplies the 7-cycle latency by the 2-cycle reservation over-costs every PF transcendental; one that models the EUP as full-rate (reservation 1) on PF under-costs back-to-back chains.

---

## The Pufferfish Xlu Divergence

Every later grid generation reads an explicit Xlu/matrix-result column through a `GetXluPathReservation` accessor (VF res 0x0e, GL res 0x0f, GF res 0x10). **Pufferfish does not.** Its `LatencyTablePufferfish` ctor `@0x1c8a1960` builds an `XluConflictPenaltyTable` (at the table's `+0x18` inner object) via 10 `SetXluConflictPenaltyBetween` calls, and prices the conv/transpose Xlu reservation through `XposeXLUReservationLatency` `@0x1c8a13e0`:

```c
function XposeXLUReservationLatency(table, mode, earlier_type, later_type, a, b):  // @0x1c8a13e0
    CHECK(IsTranspose(earlier_type))                          // latency_table_pf.cc:62
    v = (b - a) + XluConflictPenaltyBetween(&table.penalty,   //  +0x18 inner table
                                            earlier_type, later_type_lo, later_type_hi)
    if v < -5: v = -6
    return v + 7
```

The `XluConflictPenaltyTable` is a 3-axis integer table, indexed `[XluInstrType][lo<6][hi<3]` with stride `72*type + 12*lo + 4*hi + 8` (`XluConflictPenaltyBetween` `@0x1c8a0180`); stored values are `penalty + 1`. `IsTranspose(type)` is `(type - 2) < 3` — `XluInstrType` ordinals `{2,3,4}` are the transpose ops. The 10 penalty pairs the PF ctor installs (e.g. `(0→2)=56`, `(2→5)=96`, `(2→0)=86`, each duplicated for the second `hi` plane) are the conflict cycles between a transpose-result producer and a later Xlu consumer.

This is the **per-gen structural difference**: PF prices Xlu by a conflict matrix, VF/GL/GF by a grid column. It is the same architectural divide that separates the matmul occupancy model — Pufferfish has no separate `MxuLatencyTable` reservation object either, folding MXU occupancy into the grid (res 9) + this penalty table.

---

## How an Opcode Reaches a Row — `GetPufferfishInstruction`

`GetPufferfishInstruction` `@0x1c8a1fe0` (anonymous namespace) maps an `LloValue*` to a `Performance::Instruction` ordinal. It is a jump table `@0xb43927c` (index = LloOpcode − 2, bound 0x1ca) whose arms `mov eax, IMM32` where the low 16 bits are the `Instruction` ordinal and the high 16 are the **variant tag** (0 = TensorCore, 1 = BarnaCore). The bulk of the ~195 priced opcode arms resolve to variant-0 ordinals; exactly **13** arms set the high-16 variant tag to 1 (BarnaCore — opcodes `0x1ac`-`0x1ad`, `0x1af`-`0x1b5`, `0x1b8`, `0x1c9`/`0x1ca`, `0x1cc`; e.g. `kBarnaCoreVectorLoad` 0x1c9 returns `0x10084` → ins 0x84, var1). The remaining 9 arms (e.g. `0x1ae`) are `LogMessageFatal` "unsupported for Pufferfish" stubs. The MXU band is the exception: the matmul/matprep arm reads a secondary latch-mode WORD table that expands a single matmul/matprep opcode into the ~0x60-ordinal band the grid prices per data format.

| Stage | Mechanism | Anchor |
|---|---|---|
| LloValue → opcode | read `WORD[value]` | inline |
| opcode → `Instruction` + variant | jt `@0xb43927c`, idx = op − 2 | `GetPufferfishInstruction` `@0x1c8a1fe0` |
| MXU opcode → band ordinal | secondary latch-mode WORD table (per `GainLatchMode`) | matmul arm in the classifier |
| `Instruction` → latency / cells | `latency[instr]` / `grid[instr][res]` | `@0x1c8c3860` / `@0x1c8c3880` |

> **NOTE —** the band/result-pop rows reached via a shared-epilogue jump (rather than a direct `mov`-IMM arm) were named functionally by occupant LLO class, not by following the jump chain ordinal-by-ordinal (MEDIUM confidence on those rows). The band identity (MXU matmul/matprep), its base latencies (83/101), and its throughput cells are byte-exact; the per-ordinal `(opcode, latch_mode)` mapping inside the band is mechanism, not enumerated.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `PufferfishPerformance::PufferfishPerformance` | `0x1c8be080` | ctor — fills 336-entry latency array + 336×20 grid (265 cells) | CERTAIN |
| `PufferfishPerformance::GetResourceUsage` | `0x1c8c3880` | grid read — outer/inner bound + 24-B stride + `row.data[res]` | CERTAIN |
| `PufferfishPerformance::GetLatency` | `0x1c8c3860` | `latency[instr]`, bound `[perf+8]` | CERTAIN |
| `PufferfishPerformance::GetResources` | `0x1c8c3840` | returns `kResources` traversal order (count 20) | CERTAIN |
| `kResources` (PF) | `0xb43cd94` | 20-byte column traversal order, permutation of `{0..19}` | CERTAIN |
| `GetPufferfishInstruction` | `0x1c8a1fe0` | LloValue → `Instruction` + variant tag (jt `@0xb43927c`) | HIGH |
| `LatencyTablePufferfish::LatencyTablePufferfish` | `0x1c8a1960` | ctor — installs both grids + the XluConflictPenaltyTable | CERTAIN |
| `ResourceUsageFromInstruction` (variant 0) | `0x1c8a3180` | TensorCore → `PufferfishPerformance::GetResourceUsage` | CERTAIN |
| `ResourceUsageFromInstruction` (variant 1) | `0x1c8a31a0` | BarnaCore → `PufferfishBarnaCorePerformance::GetResourceUsage` | CERTAIN |
| `PufferfishBarnaCorePerformance::GetResourceUsage` | `0x1c8c4800` | BarnaCore grid read (variant-1 ops) | HIGH |
| `XposeXLUReservationLatency` | `0x1c8a13e0` | conv/transpose Xlu pricing via penalty table | CERTAIN |
| `XluConflictPenaltyBetween` | `0x1c8a0180` | 3-axis penalty-table read `[type][6][3]` | CERTAIN |
| `XluConflictPenaltyTable::IsTranspose` | `0x1c8a04e0` | `(type - 2) < 3` | CERTAIN |
| `PufferfishTarget::VectorEupReservationCycles` | `0x1d494cc0` | `= 2` (half-rate EUP) | HIGH |
| `GetSharedPufferfishPerformance` singleton | `0x22579a10` | `pf_shared` TensorCore grid instance | CERTAIN |

---

## Related Components

| Name | Relationship |
|---|---|
| `performance-overview` | the family framing — the shared grid layout + byte-identical read path |
| `performance-vf` / `-gl-ghperf` / `-gf-ghperf` | the wider 28/31-column grids that inherit this layout |
| `mxu-latency-pf` | the PF MXU occupancy correction — no separate `MxuLatencyTable`, grid res 9 + conflict-penalty |
| `resource-enum` | the 23-slot per-bundle `ResourceVector`, distinct from the 20-column `Performance::Resource` |
| `matmul-mode-modifiers` | the matmul format codes the MXU band rows are keyed on |

---

## Cross-References

- [Performance Overview](performance-overview.md) — the two `Performance` architectures, the shared grid layout, and the resource-count progression 20→28→31→31
- [Performance: VF](performance-vf.md) — the 28-column Viperfish grid and the `GetXluPathReservation` res 0x0e PF lacks
- [Performance: GL (GhPerf 476×31)](performance-gl-ghperf.md) — the Ghostlite grid, Xlu deposit res 0x0f
- [Performance: GF (GhPerf 465×31)](performance-gf-ghperf.md) — the `6acc60406` grid, Xlu deposit res 0x10
- [MXU Latency: PF](mxu-latency-pf.md) — why Pufferfish has no separate `MxuLatencyTable` and prices MXU occupancy through this grid + the conflict-penalty table
- [MXU Latency Overview](mxu-latency-overview.md) — the per-gen `MxuLatencyTable` reservation model the later gens carry
- [Resource Enum (23-slot)](resource-enum.md) — the higher-level per-bundle `ResourceVector`, distinct from this micro-pipeline `Resource` enum
- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the `MatmulDataFormat` codes the matmul band ordinals encode
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot whose ops the matmul band (res 9) prices
