# Performance: GL (GhPerf 476×31)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other versions will differ.*

## Abstract

`xla::ghostlite::GhostlitePerformance` is the Ghostlite (TpuVer 4, v6e) instance of the grid-family `Performance` object framed in [`performance-overview`](performance-overview.md). It is one of two `GhostlitePerformance` constructors internally nicknamed **GhPerf** — the named v6e variant documented here and the mis-symbolized v7 Trillium variant on [`performance-gf-ghperf`](performance-gf-ghperf.md). It prices each LLO instruction two ways: a flat latency array indexed by `GhostlitePerformance::Instruction`, read by `GetLatency` to set the depth of a true-dependency edge; and a 2D **Instruction × Resource** occupancy grid, read by `GetResourceUsage(instr, res)` to charge how many cycles the instruction holds each intra-op micro-pipeline port. The grid is the libtpu analog of an LLVM `SchedMachineModel` `ProcResource`/`WriteRes` table, reconstructed by decoding the constructor that fills it.

The Ghostlite grid is `476 × 31`: 476 instruction rows (the v6e `GhostlitePerformance::Instruction` cardinality) and 31 resource columns (`GhostlitePerformance::Resource`, the EUP/Xlu/MXU-result micro-pipeline ports). The object layout, the `GetResourceUsage` read path with its two bounds checks and 24-byte row stride, and the resource count 31 are all shared with the Viperfish grid and the GF/Trillium twin; the constructor — `_ZN3xla9ghostlite20GhostlitePerformanceC1Ev @0x1c8cbc80`, the one GhPerf constructor that still carries a clean symbol — and the per-cell integers are v6e-specific. The grid does **not** price the MXU matmul/matpush reservation; that is a separate `MxuLatencyTable` ([`mxu-latency-gl`](mxu-latency-gl.md)). The GhPerf grid and the `MxuLatencyTable` agree on the per-dtype throughput magnitudes ({4 bf16 / 8 fp8}) but are read for different questions and live in different sub-objects of the owning `GlcCycleTable`.

This page documents the Ghostlite grid: the object the constructor builds; the byte-identical `GetResourceUsage`/`GetLatency` read paths; how an LLO opcode reaches a grid row through the `GetGhostliteInstruction @0x1c8b1740` classifier and its MXU/permute/transpose latch-mode fan-out; the 31 resource columns described by their occupant instruction class instead of dumped cell-by-cell; the Xlu/matrix-result deposit column (GL res `0x0f`) that the convolution cost model reads as its `R[2]` term; and the `0xff`-default fallback path that the ~344 unpriced rows take. The full per-cell dumps of the populated rows are described by band, not transcribed in full.

For reimplementation, the contract is:

- The `GhostlitePerformance` object layout: a heap latency array (`new 0x770`, 476 int32, memset `0xff`) and a heap 2D grid (`new 0x2ca0`, 476 × 24-byte `std::vector<int>`), each row a 31-wide zero-init vector.
- The `GetResourceUsage(instr, res)` and `GetLatency(instr)` read paths, including the two bounds checks (outer at `[this+0x20]`, inner at `[row+8]`) and the 24-byte row stride.
- The opcode → row classifier `GetGhostliteInstruction`: the binary-search WORD remap table plus the explicit MXU/permute/transpose/compare switch that fans one opcode into many rows.
- The 31 resource columns by band, and the Xlu/matrix-result deposit column GL res `0x0f` (cell `= 4` for every matrix-result op) that `GetXluPathReservation` reads as the conv `R[2]` cycle.
- The `0xff`-default fallback: the ~344 unpriced rows keep the memset sentinel and are priced through a separate `CycleTable` path, not the grid.

| | |
|---|---|
| **Class** | `xla::ghostlite::GhostlitePerformance` (internally "GhPerf", v6e variant) |
| **Constructor** | `GhostlitePerformanceC1Ev` `@0x1c8cbc80` (31284 B; clean symbol; latency `new 0x770`, grid `new 0x2ca0`) |
| **Read path** | `GhostlitePerformance::GetResourceUsage` `@0x1c8d3700`; `GetLatency` `@0x1c8d36e0`; `GetResources` `@0x1c8d36c0` |
| **Grid shape** | 476 rows (`Instruction`, count `0x1dc`) × 31 columns (`Resource`); 358 populated cells across 132 rows |
| **Latency array** | `new 0x770` = 1904 B = 476 int32; memset `0xff` (255 default for ~344 unpriced rows) |
| **Row classifier** | `ghostlite::GetGhostliteInstruction` `@0x1c8b1740` (WORD remap + MXU latch-mode fan-out) |
| **Xlu deposit column** | GL res `0x0f` (15); cell `= 4` for every matrix-result/cmem/transpose-result op |
| **kResources order** | `@0xb43cdc4` (31 bytes, all of {0..30}; `0x0f` last) |
| **Owning CycleTable** | `GlcCycleTable` `@0x1c89e7e0` — grid at `this+0x10`, `MxuLatencyTable` at `this+0x18` |
| **Source file** | `…/jellyfish/target/ghostlite/latency_table_gl.cc` (CHECK/LogFatal anchors) |

---

## The Performance Object

### Purpose

The constructor builds two heap allocations and fills only the priced cells. Everything the read path touches — the latency array, its count, the grid pointer, the outer count, and each row's `std::vector<int>` header — is laid out by `GhostlitePerformanceC1Ev @0x1c8cbc80`. The unpriced rows are deliberately left at a sentinel so a downstream caller can tell "this op is not priced by the grid" from "this op holds zero cycles".

### Structure

The object is the grid-family layout shared by Pufferfish, Viperfish, and both GhPerf variants; only the widths differ (Ghostlite uses 476 rows / 31 columns):

```c
struct GhostlitePerformance {       // built by GhostlitePerformanceC1Ev @0x1c8cbc80
    int32*  latency;                // +0x00 ; new 0x770 (476 int32), memset 0xff
    u64     latency_size;           // +0x08 ; = 0x1dc (476) — GetLatency bound
    u64     latency_cap;            // +0x10 ; = 0x1dc (476)
    vector<int>* grid;              // +0x18 ; new 0x2ca0 (476 × 24-byte vector<int>)
    u64     grid_outer_count;       // +0x20 ; = 0x1dc (476) — GetResourceUsage outer bound
    u64     grid_outer_cap;         // +0x28 ; = 0x1dc (476)
    // each grid row (24-byte std::vector<int>): { int* data (new 0x7c = 31 int32, zero-init),
    //                                             size = 0x1f (31), cap = 0x1f (31) }
};
```

The constructor prologue is byte-confirmed at `@0x1c8cbc80`:

```c
function GhostlitePerformanceC1Ev(this):                 // @0x1c8cbc80
    this.latency = new(0x770)                            // @0x1c8cbc96 — 1904 B = 476 int32
    this.latency_cap  = 0x1dc                            // [this+0x10] = 476
    memset(this.latency, 0xff, 0x770)                    // @0x1c8cbcb3 — sentinel fill
    this.latency_size = 0x1dc                            // [this+0x08] = 476
    this.grid = new(0x2ca0)                              // @0x1c8cbcd2 — 12192 B = 476 × 24
    this.grid_outer_cap = 0x1dc                          // [this+0x28] = 476
    for r in 0 .. 475:                                   // loop bound r15 == 0x2cb0 (0x18 + 476*24)
        row.data = new(0x7c)                             // @0x1c8cbcf8 — 124 B = 31 int32
        zero(row.data, 0x7c)                             // vxorps + vmovups ×4 (@0x1c8cbd02..)
        row.size = 0x1f ; row.cap = 0x1f                 // [row+8]=[row+0x10]=31
    this.grid_outer_count = 0x1dc                        // [this+0x20] = 476
    // ... then 834 DWORD-immediate stores: 476 latency entries + 358 grid cells
```

After the headers are set, the body issues exactly **834** `mov DWORD PTR [addr], imm` stores: 476 write every latency slot (the first three, `latency[0]=1`, `latency[1]=1`, `latency[2]=2`, are stored at `@0x1c8cbd50/64/79` into `[this+0x00]`), and the remaining 358 write the populated grid cells. The store count being exactly `476 + 358` is the integrity check that every store was classified.

> **QUIRK —** the GL constructor at `@0x1c8cbc80` carries the clean symbol `_ZN3xla9ghostlite20GhostlitePerformanceC1Ev`, but the GF (Trillium) GhPerf constructor at `@0x1c8d3740` is mis-symbolized in the binary; it is the `GfcCycleTable`-allocated variant, structurally the same `GhostlitePerformance` layout (31-wide rows) with a 465-row instruction set instead of 476. The two are distinct constructors with distinct cell values and distinct base latencies (GL EUP/transcendental 192/182 vs GF 212/204), not a shared instance. See [`performance-gf-ghperf`](performance-gf-ghperf.md).

### The memset-0xff default

The latency array is memset to `0xff` (255) before any cell is written. Unlike Pufferfish and Viperfish — where every latency slot is subsequently overwritten and the default never survives — Ghostlite writes only the ~132 priced rows. The remaining ~344 instructions keep `0xff = 255` in both the latency array and (implicitly) leave their entire 31-wide grid row at the zero-init value. Those unpriced instructions are **not** priced by the grid: their cost comes from the gen-invariant `CycleTable::GetResource` path (`@0x1c89ce20`) or the `JfCycleTable` default of 1 cycle. A reimplementation must keep 255 as a distinguishable sentinel — a caller that reads a `0xff` latency knows to fall back, whereas a zero would be indistinguishable from a real zero-latency op.

> **GOTCHA —** the grid is built per `GlcCycleTable`, and `GlcCycleTable` is constructed from a `tpu::TpuVersion` (the only caller of this ctor besides `GlcCycleTable` is `LatencyTableGhostlite::LatencyTableGhostlite(tpu::TpuVersion)`). The 476-row Ghostlite set and the 465-row Trillium set are selected by which `CycleTable` subclass is instantiated, not by a runtime branch inside one shared object. Do not key the grid by a single global instruction enum across generations.

---

## The GetResourceUsage Read Path

### Purpose

`GetResourceUsage(instr, res)` is the single accessor the throughput model and the `GetXluPathReservation` accessor go through to read one grid cell. It is byte-identical across Pufferfish, Viperfish, and both GhPerf variants — the same two bounds checks and the same `lea`-computed 24-byte row stride — which is why one description covers all four grid generations.

### Algorithm

The decompiled body at `@0x1c8d3700` is short and exact:

```c
function GhostlitePerformance::GetResourceUsage(this, instr, res):   // @0x1c8d3700
    if this.grid_outer_count <= instr:        // [this+0x20] ; outer bound = 476
        BUG()                                  // ud2 — out-of-range instruction
    row_base = this.grid                       // [this+0x18]
    row = row_base + instr*24                  // v4 = 3*instr ; row = base + 8*v4 → 24-byte stride
    if row.size <= res:                        // [row+8] ; inner bound = row width (31)
        BUG()
    return row.data[res]                       // *(u32*)([row+0] + 4*res) = grid[instr][res]
```

`GetLatency(instr) @0x1c8d36e0` is the simpler sibling — `latency[instr]` bounded by `[this+0x08]` (= 476) — returning the instruction's pipeline depth (the value the scheduler raises a true-dependency edge to):

```c
function GhostlitePerformance::GetLatency(this, instr):   // @0x1c8d36e0
    if this.latency_size <= instr:            // [this+0x08]
        BUG()
    return this.latency[instr]                // *(u32*)([this+0x00] + 4*instr)
```

`GetResources() @0x1c8d36c0` returns the `kResources` traversal order — a `.rodata` byte array at `@0xb43cdc4` listing the 31 resource indices in fill order: `1d 1c 06 03 04 1a 07 08 09 0a 14 10 18 1e 0b 11 00 19 12 13 0c 15 0d 16 0e 17 1b 01 02 05 0f`. All of `{0..30}` appear exactly once; the Xlu deposit column `0x0f` is last in traversal order. `GetResourceLatency @0x1c8b1e60` iterates this array to sum a row (see below).

> **GOTCHA —** the OUTER index is the v6e `GhostlitePerformance::Instruction`, not the raw LLO opcode. Every populated row's index resolves to a coherent LLO opcode (so the axis *is* the opcode for the priced rows — row `0x2d` = `kParameterAddress`, `0x77` = `kScalarLoad`, `0x16b` = `kScalarCompare`, `0x1db` = a barnacore-wait extension), but the mapping is the per-gen classifier `GetGhostliteInstruction`, and the MXU/permute band fans a single opcode out to many ordinals via a latch-mode lookup. A reimplementation that indexes the grid directly by LLO opcode will mis-read every MXU row.

### How an opcode reaches a row — GetGhostliteInstruction

The OUTER index is produced by `ghostlite::GetGhostliteInstruction @0x1c8b1740`. It first does a branchless binary search over a 258-entry WORD remap table (a `(LloOpcode, GhPerf::Instruction)` pair table reached GOT-relative), and on a hit returns the paired remap word. Opcodes that miss the remap table fall through to an explicit `switch` that handles the bands a single opcode cannot map 1:1 — the MXU latch ops, the permute/transpose ops, and the compare op — fanning each into many rows:

```c
function GetGhostliteInstruction(value):                 // @0x1c8b1740 — latency_table_gl.cc
    opcode = value.opcode                                // WORD[value]
    // 1. Branchless binary search over the 258-entry WORD remap table (GOT-relative).
    hit = bsearch(remap_table, opcode)                   // 0x1c8b1762..17a1
    if hit valid && opcode >= hit.key:
        return hit.remapped_instruction                  // paired WORD

    // 2. Fall-through switch on the raw opcode (jt @0xb43b34c, bound opcode-1 ≤ 0xa5).
    switch (opcode):
        case 1,2,3,4:                                    // kVectorReadIar family — keyed on iar()
            CHECK(iar.has_value())                       // gl.cc:603/609/615/621
            return base ± (iar != 0)                     // e.g. 471 - (iar==0)
        case 139:                                        // kVectorSetPermutePattern (0x8b)
            return set_permute_pattern_mode==0 ? 363 : 364
        case 141,143,145,147,149:                        // matpush latch band
            mode = latch_mode()
            CHECK(target.SupportsGainLatchMode(mode))    // gl.cc:477
            return word_B43BDF4[mode]                    // latch-mode → row WORD
        case 142,144,146,148,150:                        // matprep latch band
            return latch_table[latch_mode() - 10]        // gl.cc:514
        case 155,156,163:                                // matmul band A — keyed on data format
            return matmul_table_A[matmul_data_format() - 1]
        case 159,160,164:                                // matmul band B
            return matmul_table_B[matmul_data_format() - 1]
        case 166:                                        // transpose — keyed on vxpose_mode()
            return {0:368, 1:370, 2:372}[vxpose_mode()]
        case 359:                                        // compare — keyed on comparison()
            return compare_table[comparison.kind][comparison.dir]
        default: LogFatal("Operation not supported")     // gl.cc:761
```

The fall-through `switch` is the MXU/permute/transpose/compare fan-out. A single matmul opcode (`155/156/163`) maps to up to eight rows by `MatmulDataFormat`; a single latch opcode (`141/143/145/147/149`) maps to a row by `GainLatchMode` through `word_B43BDF4[]`; the permute opcode `139` (`0x8b`) maps to one of two rows by `set_permute_pattern_mode()`. This is why the grid has 476 *Instruction* rows but the LLO ISA has far fewer MXU opcodes: the latch/format modifiers expand them. The source file is `latency_table_gl.cc`, and the `CHECK`/`LogFatal` line numbers (477, 514, 603–621, 761) are the in-binary anchors.

> **QUIRK —** the classifier *traps* (`LogFatal`) on any opcode it does not handle rather than returning a default. So the grid OUTER index is total over the *priced* opcode set but partial over the LLO ISA: an opcode that is neither in the 258-entry remap table nor in the fall-through switch is a hard error, not a `0xff`-default lookup. The `0xff` default is the *latency-array* fallback for instructions that do classify to a row but whose row was never written, not a classifier fallback.

---

## The Resource Columns

### Purpose

The grid's INNER axis is the `GhostlitePerformance::Resource` enum: 31 intra-op EUP/MXU/Xlu micro-pipeline reservation ports. The enum has **no** `ToString` in the binary, so the columns are named functionally — by reading which LLO-instruction class deposits cycles into each one (the OUTER index being the opcode) and by anchoring against the two named accessors `GetXluPathReservation` and `GetResourceLatency`. These names are reimplementation-grade in *meaning* (which physical port each column reserves) but are not literal symbol names (MEDIUM).

> **NOTE —** the per-gen `Resource` enum (31 columns) is a different, lower-level enum than the 23-slot per-bundle `ResourceVector` ([`resource-enum`](resource-enum.md)). The grid prices the intra-op micro-pipeline-stage holds; the `ResourceVector` is the per-bundle functional-unit accumulator the higher-level cost model deposits into. `kResources @0xb43cdc4` gives the grid's column *traversal* order, not the `ResourceVector` slot order.

### The column bands

The 31 columns group into recognizable bands that track the EUP/Xlu/MXU-result micro-pipeline. The table below names each band by its occupant LLO class rather than dumping all 358 cells. Column indices are GL-specific (the GF/Trillium variant has a +1 shift on the result bands because its EUP-prep group is 4 columns wide vs GL's 3):

| GL cols | Band | Occupant LLO class | Typical cells | Confidence |
|---|---|---|---|---|
| r0–r2 | Address / load-store / sync | `kVectorStoreIndexed/Masked`, `kVectorCmemStore`, `kScalarLoad/Store` | 2–3 | HIGH |
| r3–r5 | EUP transcendental-prep | `kVector{Subtract,Compose,Pack,Tanh,Pow2,Rsqrt,…}` F32/Bf16 | {13,4,3} / {17,8,7} / {25,16,15} | HIGH |
| r6 | EUP gain-push setup | `kVectorMultiplyU32/U16` | 48 | HIGH |
| r7–r10 | EUP-result-pop FIFO (4 stages) | `kVector{…}Bf16AndPop`, `kVectorXorU32/AndU32` | {2,1,3,19} / {6,5,7,39} | HIGH |
| r11–r14 | Cross-lane / transpose result (4 stages) | `kScalar{Compare,AddCarry,Multiply}` | {35,34,40,18} | HIGH |
| **r15** | **Xlu / matrix-result deposit** | `kScalarMultiplyU32/F32`, `kScalarAddS32`, `kScalarSubtractF32` (matres) | **4** | CERTAIN |
| r16–r19 | mxres-result sub-stages (4) | matres-result extensions (`kScalar{Multiply,Add,Subtract}` F32/S32) | {40,44,25,3} / {8,11,35,3} | HIGH |
| r20–r23 | Pack / extract / U64 stages (4) | `kScalar{Ceil,CLZ}`, `kVectorCLZ`, extract/U64 ops | {21,25}/{48,50}/{50,54}/{32,36} | HIGH |
| r24 | Mask-move | `kVectorMaskMove` | 1 | HIGH |
| r25 | Shift / saturate (v6e-only band) | `kVectorShift{RightLogical,RightArithmetic,LeftLogical}`, `kScalar*Max` | 1 (×14) | HIGH |
| r26–r27 | BarnaCore scatter-gradients | `kBarnaCore{Global,Local}ScatterGradients` | 5 / 4 | MEDIUM |
| r28–r30 | BarnaCore scalar-sync-wait tail | barnacore-wait band (opcodes `0x1d0..0x1db`) | 5 / 7 / 3 | MEDIUM |

The defining column is **r15** (`0x0f`): the Xlu / matrix-result deposit port. Its cell is `4` for every matrix-result, cmem-result, and transpose-result op — `kScalarMultiplyU32` (`0x16f`), `kScalarMultiplyF32` (`0x170`), `kScalarAddS32` (`0x172`), `kScalarSubtractF32` (`0x174`). This is the value the convolution cost model reads as its `R[2]` Xlu term. The full per-cell rows are not transcribed here; the structure above lets a reimplementer reconstruct any populated row from its band and its occupant class.

### The matrix-result / Xlu deposit column

`GetXluPathReservation @0x1c8b21c0` is the dedicated accessor that reads exactly column `0x0f`. It special-cases the permute-pattern opcode and otherwise tail-calls `GetResourceUsage` on res 15:

```c
function LatencyTableGhostlite::GetXluPathReservation(this, value):   // @0x1c8b21c0
    if value.opcode == 0x8b:                       // kVectorSetPermutePattern, handled directly
        return 3 * (is_transpose(value) ? 1 : 0) + 1
    instr = GetGhostliteInstruction(value)
    return GhostlitePerformance::GetResourceUsage(this.perf /*[this+0x1d0]*/, instr, 15)   // res 0x0f
```

This decompiled body is the anchor that fixes the Xlu column at `0x0f` for Ghostlite: the `res = 15` literal is the third argument to `GetResourceUsage`, and the `[this+0x1d0]` (= `[this + 58*8]`) is the `GhostlitePerformance` sub-object inside the `LatencyTableGhostlite`. The Xlu cell (`= 4`) is the GL value for the conv cost triple `R[2]`; the matpush and matmul halves come from the separate [`mxu-latency-gl`](mxu-latency-gl.md) reservation matrix. The progression across generations is res 6 (PF, conflict-penalty) → res `0x0e` (VF) → res `0x0f` (GL) → res `0x10` (GF), tracking the MXU geometry.

### GetResourceLatency — the row-summing consumer

`GetResourceLatency @0x1c8b1e60` is the consumer that turns the grid into a hazard-cycle count for an instruction pair. It calls `GetResources()` for the 31-column traversal order, and for each column reads `GetResourceUsage(instr, res)` for both instructions, accumulating a max over the columns both instructions touch:

```c
function LatencyTableGhostlite::GetResourceLatency(this, producer, consumer):  // @0x1c8b1e60
    pi = GetGhostliteInstruction(producer)
    ci = GetGhostliteInstruction(consumer)
    cols = GetResources()                            // kResources @0xb43cdc4, 31 entries
    result = 0
    for res in cols:                                 // traversal order
        a = GetResourceUsage(perf, pi, res)
        if a == 0: continue
        if GetResourceUsage(perf, ci, res) == 0: continue
        switch (res):
            case 0,1,0x0f,0x18,0x1c,0x1d,0x1e:        // "max" columns — take the larger hold
                result = max(result, a)
            case 0x19:                                // shift/saturate band — +1 for one opcode range
                result = max(result, a + (consumer.opcode-389 < 14))
            case 0x1b:                                // FIFO column — only if push/pop same FIFO
                if LloInstructionsPushOrPopSameFifo(producer, consumer):
                    result = max(result, a)
            case 2:        LogFatal("Unimplemented")          // gl.cc:1040
            case 3..0x0a, 0x1a: LogFatal("Did not expect MXU resource")   // gl.cc:1066
    return result
```

The per-column `switch` is the proof that the 31 columns are not interchangeable: columns `0..1`, `0x0f` (Xlu), `0x18`, `0x1c..0x1e` are simple "take the larger hold" columns; column `0x1b` is a FIFO column that only counts when both ops touch the same FIFO (`LloInstructionsPushOrPopSameFifo`); columns `3..0x0a` and `0x1a` are MXU-reservation columns that this path explicitly refuses to handle (`"Did not expect to have MXU resource for …"`, `gl.cc:1066`) — those are priced by the `MxuLatencyTable`, not here. Column `0x19` is the v6e shift/saturate band that adds one extra cycle for a specific consumer opcode range (`opcode - 389 < 14`).

> **NOTE —** the `LogFatal` on the MXU columns (`3..0x0a`, `0x1a`) is a deliberate assertion, not dead code: a producer/consumer pair that both deposit into an MXU column should never reach `GetResourceLatency`, because the MXU hazard is computed by the separate `MxuOpHoldIssues` recurrence over the `MxuLatencyTable`. The grid columns `3..0x0a` carry cells (the EUP-prep band) but are read by other paths, never by this pairwise hazard sum.

---

## The 476-vs-465 Instruction-Set Delta

Ghostlite (v6e) has 476 `Instruction` rows; Trillium (v7) has 465 — a `+11` v6e surplus. The extra v6e opcodes surface in two places in the grid: (a) the GL-only populated **r25** shift/saturate band (14 cells, the shift-right/left and scalar/vector-max overflow-saturate ops `0x19a..0x1bb`, which GF either fuses or routes elsewhere — GF's r25-equivalent has a single cell); and (b) the extended BarnaCore-wait opcodes `0x1d0..0x1db`, which are *past* the 461-entry LLO opcode-name range (the last named LLO opcode is `0x1cc = kBarnaCoreVectorStore`). So the `+11` v6e-extra instructions are the additional shift/saturate plus barnacore-wait variants; Trillium drops them for a smaller 465-row set. The names of the `0x1d0..0x1db` rows are inferred from their value pattern matching the GF barnacore-wait band (MEDIUM); they were not individually resolved.

---

## Worked Example — the conv R[2] Xlu cell

A Ghostlite convolution cost computation reads the Xlu throughput per matrix-result op. `GetXluPathReservation @0x1c8b21c0` classifies the op via `GetGhostliteInstruction`, then reads `GetResourceUsage(perf, instr, 15)`. For each matres-result op the grid holds `grid[instr][0x0f] = 4`:

```text
opcode kScalarMultiplyF32 (0x170) → GhPerf row 0x170, lat 123 → grid[..][0x0f] = 4
opcode kScalarAddS32      (0x172) → GhPerf row 0x172, lat  97 → grid[..][0x0f] = 4
opcode kScalarSubtractF32 (0x174) → GhPerf row 0x174, lat 109 → grid[..][0x0f] = 4
```

So the GL conv `R[2]` Xlu term is `4 · ChunksPerTile · rem`. The `R[0]` matpush and `R[1]` matmul halves come from the [`mxu-latency-gl`](mxu-latency-gl.md) reservation matrix (matpush {2 bf16 / 4 fp8}, matmul {4 bf16 / 8 fp8}). The per-op base latency (123/97/109 here, and 192/182 for the EUP-prep band) is the v6e silicon's pipeline depth — lower than Trillium's 212/204, the visible silicon-generation divergence. The Xlu cell `4` itself is dtype-independent and matches the Trillium value; only the surrounding latencies differ.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ghostlite::GhostlitePerformance::GhostlitePerformanceC1Ev` | `0x1c8cbc80` | GL grid ctor — latency `new 0x770`, grid `new 0x2ca0`; 834 stores (476 lat + 358 grid) | CERTAIN |
| `ghostlite::GhostlitePerformance::GetResourceUsage` | `0x1c8d3700` | grid read — outer bound `[this+0x20]`, 24-byte stride, inner bound `[row+8]` | CERTAIN |
| `ghostlite::GhostlitePerformance::GetLatency` | `0x1c8d36e0` | latency read — `latency[instr]`, bound `[this+0x08]` | CERTAIN |
| `ghostlite::GhostlitePerformance::GetResources` | `0x1c8d36c0` | returns `kResources @0xb43cdc4` (31-byte traversal order) | CERTAIN |
| `ghostlite::GetGhostliteInstruction` | `0x1c8b1740` | LLO opcode → `Instruction` row; WORD bsearch + MXU latch fan-out (jt `@0xb43b34c`) | HIGH |
| `ghostlite::LatencyTableGhostlite::GetXluPathReservation` | `0x1c8b21c0` | reads Xlu column res `0x0f`; conv `R[2]` accessor | CERTAIN |
| `ghostlite::LatencyTableGhostlite::GetResourceLatency` | `0x1c8b1e60` | pairwise hazard sum over the 31 columns; per-column `switch` | HIGH |
| `GlcCycleTable::GlcCycleTable` | `0x1c89e7e0` | owning CycleTable — grid at `this+0x10`, `MxuLatencyTable` at `this+0x18` | CERTAIN |
| `CycleTable::GetResource` | `0x1c89ce20` | fallback cost path for the `0xff`-default unpriced rows | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `performance-overview` | the family framing: flat vs grid `Performance`, the shared read path, the resource progression |
| `performance-gf-ghperf` | the Trillium (v7) GhPerf twin — 465×31, Xlu res `0x10`, base latency 212/204 |
| `mxu-latency-gl` | the separate Ghostlite `MxuLatencyTable` (matmul/matpush reservation) that co-exists with this grid |
| `resource-enum` | the 23-slot per-bundle `ResourceVector`, distinct from the 31-wide `GhostlitePerformance::Resource` |
| `slot-mxu` | the physical MXU sub-units the resource columns reserve |

## Cross-References

- [Performance Family Overview](performance-overview.md) — the grid-family object layout, the shared `GetResourceUsage`/`GetLatency` read paths, and the 7→7→20→28→31→31 resource progression
- [Performance: GF (GhPerf 465×31)](performance-gf-ghperf.md) — the Trillium v7 GhPerf twin; Xlu deposit res `0x10`, base latency 212/204, and the 476-vs-465 instruction-set delta
- [MXU Latency: GL (Ghostlite)](mxu-latency-gl.md) — the separate v6e `MxuLatencyTable` reservation matrix; the conv `R[0]`/`R[1]` matpush/matmul halves to this grid's `R[2]`
- [Resource Enum (23-slot)](resource-enum.md) — the per-bundle `ResourceVector`, distinct from the per-gen `Performance::Resource` micro-pipeline columns
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot whose latch-mode/format modifiers drive the `GetGhostliteInstruction` fan-out
