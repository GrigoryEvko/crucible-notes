# Performance Family Overview

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

Every TensorCore generation prices its instructions through a per-generation `Performance` object: a flat per-instruction latency array plus a two-dimensional **Instruction × Resource** occupancy grid. The grid is the libtpu analog of an LLVM `SchedMachineModel` — a `ProcResource`/`WriteRes` table — except it is reconstructed by reading the constructor that fills it, not declared in a `.td` file. Given an instruction's row and a resource column, `GetResourceUsage(instr, res)` returns how many cycles that instruction holds that micro-pipeline port; `GetLatency(instr)` returns the instruction's pipeline depth. These two reads feed the scheduler's throughput and dependency models.

Two distinct C++ class families implement this idea across six codenames. The **flat** family — `Jellyfish`/`Dragonfish` (`platforms_deepsea::jellyfish::isa::Performance`) — stores costs in a single 3584-byte inline POD and resolves an instruction to one cell through an offset LUT, with a separate `CycleTable::GetResource` LUT mapping each instruction to one of 7 resource columns; there is no 2D grid and no `GetResourceUsage`. The **grid** family — `Pufferfish`, `Viperfish`, and the two `GhostlitePerformance` variants (`Ghostlite`/v6e and `Trillium`/v7, internally "GhPerf") — heap-allocates a latency array and a 2D grid of `std::vector<int>` rows, read by a byte-identical `GetResourceUsage` across all four. The architecture changed at Pufferfish: the inline-POD-plus-offset-LUT model gave way to the heap-grid model that the rest of the line uses.

This page is the framing reference for the family. It documents the shared object layout, the `GetResourceUsage`/`GetLatency` read paths, the resource-count progression (7→7→20→28→31→31), the way an opcode reaches a grid row through the per-gen `Get<Gen>Instruction` classifier, and the matrix-result/Xlu deposit column that the convolution cost model reads (the conv `R[2]` cell). The per-generation grids — their populated cells, latency arrays, and column-by-column naming — get their own pages; this page explains the axes those pages dump. A parallel per-opcode metadata table, `opcode_produced_register_type`, classifies every LLO opcode by the register class its destination produces; it is documented here because it is the same kind of per-opcode lookup the grids key on, and because the convolution-window cost path gates a DMA-level merge on it.

For reimplementation, the contract is:

- The `Performance` object layout for both families: flat inline POD (JF/DF) vs heap latency-array + heap 2D grid (PF/VF/GL/GF), with exact sizes and field offsets.
- The `GetResourceUsage(instr, res)` and `GetLatency(instr)` read paths, including the two bounds checks and the 24-byte row stride.
- The resource-count progression and the per-gen Xlu/matrix-result deposit column (PF res 6 → VF res 0x0e → GL res 0x0f → GF res 0x10), with the `GetXluPathReservation` accessor that reads it.
- How an LLO opcode reaches a grid row (the `Get<Gen>Instruction` classifier, including the MXU matmul/matprep latch-mode fan-out) and how the throughput cells co-exist with the separate `MxuLatencyTable`.
- The `opcode_produced_register_type` taxonomy and the convolution-window DMA-level merge gate it drives.

| | |
|---|---|
| **Flat family** | `platforms_deepsea::jellyfish::isa::Performance` — `PerformanceJf` `@0x1d4930c0`, `PerformanceDf` `@0x1d493060` |
| **Grid family** | `Pufferfish`/`Viperfish`/`GhostlitePerformance` (Ghostlite v6e, Trillium v7 "GhPerf") |
| **Flat read path** | `JfCycleTable::GetCyclesForThroughput` `@0x1c89dce0`; `CycleTable::GetResource` LUT `@0xb438aec` |
| **Grid read path** | `GhostlitePerformance::GetResourceUsage` `@0x1c8d3700`; PF `@0x1c8c3880`; VF `@0x1c8cbc40` |
| **GF (Trillium) ctor** | GhPerf ctor `@0x1c8d3740` — latency `new 0x744` (465 int32), grid `new 0x2b98` (465 × 24), rows 31 wide |
| **GL (Ghostlite) ctor** | `GhostlitePerformanceC1` `@0x1c8cbc80` — latency `new 0x770` (476), grid `new 0x2ca0` (476 × 24), 31 wide |
| **Resource progression** | 7 (JF) → 7 (DF) → 20 (PF) → 28 (VF) → 31 (GL) → 31 (GF) |
| **Xlu deposit column** | PF res 6 (conflict-penalty) · VF res 0x0e · GL res 0x0f · GF res 0x10 — cell = 4 for matrix-result ops |
| **Per-opcode reg-type** | `xla::jellyfish::internal::opcode_produced_register_type` `@0x223a16c0` — byte[461], values 0–4 |

---

## The Two Performance Architectures

### Purpose

`Performance` answers two questions the scheduler needs for every instruction: *how deep is its pipeline* (`GetLatency`, used on true-dependency edges) and *how many cycles does it occupy each functional-unit port* (`GetResourceUsage`, used to accumulate per-bundle throughput pressure). The two architectures answer the same questions but store the answers differently, and the change point — Pufferfish — marks a real silicon-era boundary, not just a refactor: the oldest generations (1-MXU Jellyfish, 2-MXU Dragonfish) have so few priced instructions that a flat cell-per-instruction LUT suffices; the wider MXU/EUP pipelines of Pufferfish onward need an explicit per-port grid.

### The flat family (Jellyfish, Dragonfish)

JF/DF use one fixed `0xe00`-byte (3584 B) inline POD, `platforms_deepsea::jellyfish::isa::Performance`. There is no separate latency array and no 2D grid; instead, both the latency model and the throughput cells live as scattered `int32` slots inside the one buffer, and a per-instruction offset LUT picks the right slot.

```c
struct Performance {                      // 0xe00 bytes, built by CreateTensorCore @0x1d4927e0
    void*  vtable;                        // +0x00
    u64    device_id_lo;                  // +0x08
    u32    device_id_hi;                  // +0x10
    bool   is_tensorcore;                 // +0x14  (=1 from CreateTensorCore)
    int32  buf[890];                      // +0x18 .. +0xdf8 ; default = 0x7fffffff (INT_MAX) sentinel
};
```

The base ctor `@0x1d492900` memsets `buf` to the sentinel `0x7fffffff` (note: not `0xffffffff` — distinct from the heap family). `PerformanceJf @0x1d4930c0` then overwrites the populated cells by copying pre-baked 16-byte `.rodata` constant blocks (e.g. `{4,105,7,92}` into `+0x18`, `{88,8,4,1}` into `+0x28`); 116 store operations cover 419 of the 890 slots, leaving 471 at the sentinel. `PerformanceDf @0x1d493060` is `PerformanceJf` plus a vtable swap and exactly **one** quadword store — `[+0x28] = 0xd00000042` — making `[+0x28]=66` (matmul base) and `[+0x2c]=13` (matprep base). DF differs from JF in those two `int32` cells and nothing else.

The read path is `JfCycleTable::GetCyclesForThroughput(Instruction) @0x1c89dce0`:

```c
function JfCycleTable_GetCyclesForThroughput(table, instr):   // @0x1c89dce0
    // valid set = {0,5,11,18,19,20,21,22,23,24,25,26,27,28,31,32} (16 of 33)
    if (instr < 0x21) && ((0x19FFC0821 >> instr) & 1):
        return Performance[ offsetLUT[instr] ]                // offsetLUT @0xb438b70 (33 × i64)
    return 1                                                  // unpriced → default 1 cycle
```

The companion `CycleTable::GetResource(instr) @0x1c89ce20` is a plain LUT read, `[LUT@0xb438aec + instr*4]`, mapping each of the 33 `CycleTable::Instruction` ordinals onto one of 7 resource columns (0–6). The consumer `AccumulateInstructionUsage @0x144fd720` accumulates `(GetResource(I), GetCyclesForThroughput(I))` into the first 7 slots of the 23-slot per-bundle `ResourceVector`.

> **NOTE —** the `Performance::Instruction` axis is *not* the raw LLO opcode. `CycleTableInstruction @0x1c89ca80` classifies only the MXU band — matmul opcodes `0x8d..0x96` via `latch_mode()`, matprep/matpush `0x9b..0xa5` via `matmul_data_format()` — into the 33-value enum; all other opcodes are priced through other `CycleTable` paths. The MXU throughput cell is 8 cycles uniformly across JF/DF; the JF→DF speedup lives in the matmul base latency (88→66), not the throughput.

### The grid family (Pufferfish, Viperfish, Ghostlite, Trillium)

PF/VF/GL/GF heap-allocate two objects: a flat latency array indexed by `Instruction`, and a 2D grid of one `std::vector<int>` per instruction, each vector being `N` resources wide. The object layout is identical across all four, only the widths differ:

```c
struct GridPerformance {            // built by the per-gen ctor (table below)
    int32*  latency;                // +0x00 ; new (rows*4), memset 0xff
    u64     latency_size;           // +0x08 ; = rows
    u64     latency_cap;            // +0x10 ; = rows
    vector<int>* grid;              // +0x18 ; new (rows*24), one vector<int> per row
    u64     grid_outer_count;       // +0x20 ; = rows  (read as the GetResourceUsage outer bound)
    u64     grid_outer_cap;         // +0x28 ; = rows
    // each row (24 B std::vector<int>): { int* data (new N*4, zero-init), size=N, cap=N }
};
```

| Gen | Ctor | Latency alloc | Rows | Grid alloc | Row width (resources) | Populated cells |
|---|---|---|---|---|---|---|
| PF (Pufferfish) | `@0x1c8be080` | `new 0x540` | 336 | `new 0x1f80` (336 × 24) | `new 0x50` = 20 | 265 |
| VF (Viperfish) | `@0x1c8c4840` | `new 0x600` | 384 | `new 0x2400` (384 × 24) | `new 0x70` = 28 | 378 |
| GL (Ghostlite, v6e) | `@0x1c8cbc80` | `new 0x770` | 476 | `new 0x2ca0` (476 × 24) | `new 0x7c` = 31 | 358 |
| GF (Trillium, v7) | `@0x1c8d3740` | `new 0x744` | 465 | `new 0x2b98` (465 × 24) | `new 0x7c` = 31 | 285 |

The latency array is memset to `0xff` (255). On PF/VF every slot is subsequently overwritten — the default does not survive. On GL/GF only the priced instructions (~92 GF / ~132 GL rows) are written; the rest keep `0xff = 255`, and those unpriced instructions take their cost from a fallback `CycleTable::GetResource` path rather than the grid.

> **QUIRK —** the GL ctor at `@0x1c8cbc80` carries the clean symbol `_ZN3xla9ghostlite20GhostlitePerformanceC1Ev`, but the GF ctor at `@0x1c8d3740` is mis-symbolized in the binary; it is the `GfcCycleTable`-allocated GhPerf variant, structurally a `GhostlitePerformance` (same layout, 31-wide rows) with a 465-row instruction set instead of 476. The two are distinct constructors with distinct cell values and distinct base latencies (GF EUP/transcendental 212/204 vs GL 192/182), not a shared instance.

---

## The GetResourceUsage Read Path

### Purpose

`GetResourceUsage(instr, res)` is the single accessor the throughput model and the Xlu-reservation accessor go through to read a grid cell. It is byte-identical across PF/VF/GL/GF — the same two bounds checks and the same `lea`-computed 24-byte row stride — which is why one description covers all four grid generations.

### Algorithm

```c
function GhostlitePerformance_GetResourceUsage(perf, instr, res):   // @0x1c8d3700 (GF/GL twin)
    if perf.grid_outer_count <= instr:        // [perf+0x20] ; outer bound (e.g. 465 GF / 476 GL)
        trap()                                 // ud2 / BUG
    row_base = perf.grid                       // [perf+0x18]
    row = row_base + instr*24                  // lea rax+rax*2 (×3), then ×8 → 24-byte stride
    if row.size <= res:                        // [row+8] ; inner bound = row width (31 / 28 / 20)
        trap()
    return row.data[res]                       // *(int*)([row+0] + res*4) = grid[instr][res]
```

`GetLatency(instr)` is the simpler sibling — `latency[instr]` bounded by `[perf+0x8]` — returning the instruction's pipeline depth (the value the scheduler raises a true-dependency edge to). `GetResources()` returns the `kResources` traversal order (a `.rodata` byte array listing the resource indices in fill order: GF `@0xb43cde3`, GL `@0xb43cdc4`, VF `@0xb43cda8`, PF `@0xb43cd94`), which `GetResourceLatency @0x1c8b1e60` iterates to sum a row.

> **GOTCHA —** the OUTER index is the per-gen `Performance::Instruction`, not the raw LLO opcode. Every populated row's index resolves to a coherent LLO opcode (so the axis *is* the opcode for the priced rows), but the mapping is a per-gen classifier (`GetGhostliteInstruction @0x1c8b1740`, `GetViperfishInstruction @0x1c8a3300`, `GetPufferfishInstruction @0x1c8a1fe0`), and the MXU band fans a single matmul/matprep opcode out to many ordinals via a secondary latch-mode WORD table. A reimplementation that indexes the grid directly by LLO opcode will mis-read every MXU row.

### The matrix-result / Xlu deposit column

One grid column is special: it holds the matrix-result (Xlu) throughput that the convolution cost model reads as its `R[2]` term. The column index tracks the MXU geometry across generations — it is **res 6** on PF, **res 0x0e** on VF, **res 0x0f** on GL, **res 0x10** on GF — and the populated cell is `4` for every matrix-result/cmem/transpose-result instruction. The dedicated accessor reads exactly this column:

```c
function LatencyTableGhostlite_GetXluPathReservation(this, value):   // @0x1c8b21c0 (GL)
    if value.opcode == 0x8b:                       // kVectorSetPermutePattern, handled directly
        return 3 * (is_transpose(value) ? 1 : 0) + 1
    instr = GetGhostliteInstruction(value)
    return GhostlitePerformance::GetResourceUsage(this.perf /*[this+0x1d0]*/, instr, 15)   // res 0x0f
```

The VF accessor `@0x1c8a3200` is the same shape with `res = 0x0e` and a hardcoded `8`/`1` for the permute-pattern opcode. PF has no direct Xlu grid column at all: its conv/transpose Xlu cost is priced by a separate `XluConflictPenaltyTable` (`XposeXLUReservationLatency @0x1c8a13e0`), the per-gen structural difference. The Xlu cell (=4) is the missing numeric for the Trillium conv cost triple, whose other two terms come from the [MXU reservation matrix](mxu-latency-gf.md): `R[0]` matpush {2 bf16 / 4 fp8}, `R[1]` matmul {4 bf16 / 8 fp8}, `R[2]` Xlu {4}.

---

## The Resource Columns

### Purpose

The grid's INNER axis is a per-gen `Performance::Resource` enum: the intra-op EUP/MXU/Xlu micro-pipeline reservation ports. No generation has a `Resource::ToString` in the binary, so the columns are named functionally — by reading which LLO-instruction class deposits cycles into each one (the OUTER index being the opcode) and by anchoring against the two named accessors `GetXluPathReservation` and `GetResourceLatency`. These names are reimplementation-grade in *meaning* (what physical port each column reserves) but are not literal symbol names.

> **NOTE —** the per-gen `Performance::Resource` enum (20/28/31 columns) is a different, lower-level enum than the 23-slot per-bundle `ResourceVector` ([Resource Enum](resource-enum.md)). The grid prices the intra-op micro-pipeline-stage holds; the `ResourceVector` is the per-bundle functional-unit accumulator that the higher-level cost model deposits into. The `kResources` byte array gives the grid's column traversal order, not the `ResourceVector` slot order.

### The column families and progression

The columns group into recognizable bands that widen as the MXU/EUP pipeline grew across generations. The matmul-throughput representation is the clearest signal: a single column (res 9, 96 cells) on PF; a 51-cell column plus a 4-stage matprep group (res 3 + res 4..7) on VF; and the wider EUP-AndPop FIFO depth plus a BarnaCore tail on GL/GF.

| Band | PF cols | VF cols | GL/GF cols | Role |
|---|---|---|---|---|
| Address / load-store / sync | r0..r2 | r0..r1, r21 | r0..r2 | DMA/address generation, scalar store, sync-flag |
| EUP transcendental-prep | r2..r3 | (via latency) | r3..r6 (GF) | per-dtype prep stages (carry the {4 bf16 / 8 fp8} magnitudes) |
| MXU matmul / matprep throughput | r9..r12 | r2..r8 | (EUP-prep cols) | the per-format matmul/matprep cycle cells |
| EUP-result-pop FIFO | r5 | r9..r11 | r8..r11 (GF) | drain stages after an EUP transcendental |
| Cross-lane / transpose result | r7, r11..r13 | r12..r17 | r12..r15 (GF) | permute/rotate/broadcast/transpose result stages |
| **Xlu / matrix-result deposit** | **r6** | **r0x0e** | **r0x0f / r0x10** | the conv `R[2]` cell (=4 for matrix-result ops) |
| Reduce result | r14..r15 | r18 | — | `kVector*ReduceF32` result stages |
| Pack / extract / U64 | — | — | r21..r24 (GF) | `kVector{Xor,PseudoPack,MultiplyU64,Extract}` stages |
| Tail (CCF / RNG / sync-wait / BarnaCore) | r16..r19 | r19..r27 | r25..r30 (GF) | CCF push/pop, PRNG, sync-flag wait, BarnaCore scalar-sync |

The full per-cell dumps live on the per-gen pages: [JF/DF](performance-jf-df.md) (flat 7-column model), [PF](performance-pf.md) (20), [VF](performance-vf.md) (28), [GL](performance-gl-ghperf.md) and [GF](performance-gf-ghperf.md) (31 each).

---

## opcode_produced_register_type

### Purpose

Parallel to the per-gen `Performance::Instruction` keying, libtpu carries a single gen-invariant per-opcode byte table that classifies every LLO opcode by the register class its destination produces. It is documented on this page because it is the same kind of per-opcode lookup the grids key on, and because the convolution-window cost path uses it to decide whether two contiguous window axes may merge into one DMA descriptor level — a decision that directly changes the DMA fragment count and therefore the efficiency multiplier the cost model applies.

### The table and its taxonomy

`xla::jellyfish::internal::opcode_produced_register_type @0x223a16c0` is a `byte[461]` in `.data` (file offset `0x21fa16c0`), indexed by LLO opcode `0..0x1cc`. The bound `0x1cd = 461` matches both the `ComputeDmaLevels` range check and `LloOpcodeName`. Each byte is the destination register class:

| Value | Class | Count | Examples |
|---|---|---|---|
| 0 | No destination register | 216 | `kEvent`, stores, pushes, fences, barriers, sync-flag, DMA-issue |
| 1 | Predicate register | 11 | `kPredicate*`, `kScalarCompare`, `kScalarAddCarryU32` |
| 2 | Scalar register (SREG) | 58 | `kScalarAddressCalculation`, `kAllocationAddress`, `kScalarLoad`, `kScalarMultiplyU32`, `kRelocatableConstant` |
| 3 | Vector-mask register | 19 | `kVectorMask*`, `kVectorCompare`, `kVectorCreate*Mask` |
| 4 | Vector register | 157 | the bulk vector ALU/load/result ops |

The taxonomy is definitive — derived from cross-joining the byte table with `LloOpcodeName::opcode_name @0x21ccfef0` (461 `char*`, resolved via `R_X86_64_RELATIVE` relocations) and inspecting the named opcode sets. The histogram `{0:216, 1:11, 2:58, 3:19, 4:157}` sums to 461.

### The DMA-level merge gate

`ComputeDmaLevels` (`@0x1c86b1a2..1d8`) reads the per-axis stride-level operand at `window_desc+0xe0[axis]` (an `LloValue*`), takes its opcode, and gates a contiguous-axis merge on the register type:

```c
opcode  = WORD[stride_level_operand]
if opcode >= 0x1cd: fatal()                         // bound check, same 461
regtype = opcode_produced_register_type[opcode]
if regtype != 2 && regtype != 4:                    // cmp 2 je / cmp 4 jne break
    break                                            // contiguity break → extra DMA descriptor level
// else (Scalar or Vector) the axes may merge if KnownEq(stride) holds
```

A stride operand that produces a **Scalar** (address/index, type 2) or a **Vector** (type 4) value is eligible to merge a window axis into a single DMA level; a predicate (1), a mask (3), or a no-result op (0) forces a contiguity break — an extra `DmaLevel`, a larger fragment product, and a worse efficiency multiplier. The canonical conv-window stride operands (`kScalarAddressCalculation @0x86`, `kAllocationAddress`, `kParameterAddress`, `kScalarLoad`, `kScalarMultiplyU32`, `kRelocatableConstant`) are all type 2, hence mergeable; an unusual mask-typed stride operand (e.g. `kVectorMaskMove @0x199`, type 3) fragments the DMA.

> **NOTE —** the `kVectorReadIar @0x1` is type 4 and `kBarnaCoreVectorStore @0x1cc` (the last valid opcode) is type 0; type-4 spans most of the high opcode ranges (`0x102..0x127`, `0x13b..0x166`, `0x180..0x1a2`, `0x1c9..0x1cb`). The companion `opcode_info @0x223a1320` (461 × 2 bytes) holds a second per-opcode info word that pairs with the register type; its field meaning is not decoded.

---

## Family Summary

| Gen | Codename | TpuVersion | Performance model | Resource cols | Xlu deposit column |
|---|---|---|---|---|---|
| JF | Jellyfish | 0 (v2) | flat inline POD `0xe00` + offset LUT | 7 | (flat; res 2 MXU-result band) |
| DF | Dragonfish | 1 (v3) | = JF + 2 cells | 7 | (flat; = JF) |
| PF | Pufferfish | 2 (v4) | heap latency[336] + grid 336×20 | 20 | res 6 (conflict-penalty) |
| VF | Viperfish | 3 (v5p) | heap latency[384] + grid 384×28 | 28 | res 0x0e (`GetXluPathReservation`) |
| GL | Ghostlite | 4 (v6e) | heap latency[476] + grid 476×31 | 31 | res 0x0f |
| GF | Trillium | 5 (v7) | heap latency[465] + grid 465×31 | 31 | res 0x10 |

The matmul/matprep throughput cells in these grids co-exist with a *separate* per-gen `MxuLatencyTable` (a modifier × `MxuResource` reservation matrix); the two cost tables agree on the per-dtype throughput integers (the GF EUP-prep column carries the same {4 bf16 / 8 fp8} magnitude as the `MxuLatencyTable` matmul cell). The reservation matrix and the back-to-back stall it computes are documented under [MXU Latency Overview](mxu-latency-overview.md) and [MxuOpHoldIssues Stall](mxu-opholdissues-stall.md).

## Cross-References

- [Resource Enum](resource-enum.md) — the 23-slot per-bundle `ResourceVector`, distinct from the per-gen `Performance::Resource` micro-pipeline columns
- [Performance: JF/DF](performance-jf-df.md) — the flat 7-column inline-POD model and the {88,8,4,1}→{66,13,4,1} JF→DF delta
- [Performance: PF](performance-pf.md) — the 20-column Pufferfish grid and its conflict-penalty Xlu pricing
- [Performance: VF](performance-vf.md) — the 28-column Viperfish grid and `GetXluPathReservation` res 0x0e
- [Performance: GL (GhPerf 476×31)](performance-gl-ghperf.md) — the Ghostlite v6e grid, Xlu deposit res 0x0f
- [Performance: GF (GhPerf 465×31)](performance-gf-ghperf.md) — the Trillium v7 grid, Xlu deposit res 0x10
- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuResource` enum and the reservation-matrix cost model that co-exists with the grid
- [MxuOpHoldIssues Stall](mxu-opholdissues-stall.md) — the back-to-back matmul stall recurrence and balancing gate
- [MXU Latency: GF](mxu-latency-gf.md) — the Trillium MXU reservation matrix and the conv `R[0]`/`R[1]`/`R[2]` triple
- [MXU Slot](../isa/slot-mxu.md) — the physical MXU sub-units the resource columns reserve
