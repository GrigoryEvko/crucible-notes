# Performance: GF (GhPerf 465×31)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset; `.data` VMA − 0x400000 == file offset. Every cell integer, allocation size, and `kResources` byte on this page was read directly from the GF constructor and the binary's `.rodata`.*

## Abstract

This page dumps the **`6acc60406` (TPU7x / "GF" / `gfc`) `GhostlitePerformance` occupancy grid** — the per-generation **Instruction × Resource** 2D table that prices how many cycles each LLO instruction holds each intra-op micro-pipeline port. It is the libtpu analog of an LLVM `WriteRes`/`ProcResource` matrix, except it is reconstructed by reading the constructor that fills it, not declared in a `.td` file. The grid is `465 × 31`: 465 `GhostlitePerformance::Instruction` rows (the per-gen classifier output, == LloOpcode for every priced row) and 31 `GhostlitePerformance::Resource` columns (the EUP / cross-lane / Xlu / matrix-result micro-pipeline stages). Alongside it sits a flat 465-entry latency array indexed by the same `Instruction`. Both are heap objects owned by `GfcCycleTable`.

The GF grid is the `gfc` sibling of the Ghostlite (GL, v6e, 476-row) grid documented on [Performance: GL](performance-gl-ghperf.md): they are **two instances of one `GhostlitePerformance` class** — same `0x7c`-byte 31-int rows, same byte-identical `GetResourceUsage`/`GetLatency`/`GetResources` methods, and the **same single `kResources` static** at `@0xb43cdc4` (there is exactly one `GhostlitePerformance::kResources` symbol; GL and GF do not have distinct permutations). They differ in **row count** (465 vs 476 — `6acc60406` drops 11 v6e shift/saturate and BarnaCore-wait opcodes), in **cell values and base latencies** (GF EUP/transcendental ops latency 212/204 vs GL 192/182), and in being built by a **distinct constructor** (`sub_1C8D3740` for the `GfcCycleTable` instance vs `GhostlitePerformanceC1 @0x1c8cbc80` for the `GlcCycleTable` instance). The one runtime divergence in the Xlu read is not in the grid but in the *cost-table helper*: `GfcCycleTable::GetCyclesForThroughputHelper` reads res `0x10` (16) while `GlcCycleTable`'s reads res `0x0f` (15) — both indexing the same shared grid object.

This page is strictly the `GhostlitePerformance` grid. The companion `GfcCycleTable` `MxuLatencyTable` (the per-modifier × `MxuResource` reservation matrix, matmul/matpush throughput, and `ComputeDmaLevels`) is on [MXU Latency: GF](mxu-latency-gf.md); it is referenced here and not duplicated. The single number this grid contributes to the cost model is the convolution `R[2]` Xlu term: `grid[mxres-instr][res 0x10] = 4`, completing the `6acc60406` conv cost triple that [MXU Latency: GF](mxu-latency-gf.md) supplies `R[0]`/`R[1]` for.

For reimplementation, the contract is:

- The `GhostlitePerformance` object layout (latency array + 2D grid of `std::vector<int>` rows), the GF allocation sizes (`new 0x744` / `new 0x2b98` / `new 0x7c`), and the `0x1d1` counts.
- The `GetResourceUsage(instr, res)` read path: two bounds checks, the 24-byte row stride, the direct `row.data[res]` read; byte-identical to the GL accessor and to its inline twin `@0x1c8da1a0`.
- The 31 resource columns named by occupant LLO class, the row-classification into instruction bands, and the Xlu deposit column (res `0x10` = 4).
- The `0xff`-default fallback: only ~92 of 465 rows are written; the rest keep `latency = 255` and are priced by the gen-invariant `CycleTable::GetResource` path, not the grid.
- The GF-vs-GL grid differences (row count, base latencies, the constructor split, and the per-helper Xlu res index — *not* a difference in `kResources`, which is shared).

| | |
|---|---|
| **Class** | `xla::ghostlite::GhostlitePerformance` (GF variant; constructor unnamed/mis-symbolized) |
| **Owner / allocation** | `GfcCycleTable this+0x10` — `operator new(0x30)` then `sub_1C8D3740` |
| **Ctor** | `@0x1c8d3740` — latency `new 0x744` (465 int32, memset `0xff`), grid `new 0x2b98` (465 × 24), rows `new 0x7c` (31 int32) |
| **Read path** | `GhostlitePerformance::GetResourceUsage` `@0x1c8d3700`; inline twin `@0x1c8da1a0` |
| **Latency read** | `GhostlitePerformance::GetLatency` `@0x1c8d36e0` |
| **Column order** | `GhostlitePerformance::GetResources` `@0x1c8d36c0` → `kResources` `@0xb43cdc4` (31 bytes, perm of `0..30`) — shared by GL and GF |
| **Grid shape** | 465 rows (`0x1d1`) × 31 resources (`0x1f`); 285 populated cells across 92 rows |
| **Xlu deposit column** | res `0x10` (16) — cell = **4** for every matrix-result / Cmem / transpose-result op (conv `R[2]`) |
| **Default** | unwritten rows: latency `0xff` = 255, grid cells 0 — priced by `CycleTable::GetResource`, not the grid |

---

## Object Layout and Build

### Purpose

The grid is built exactly once, when `GfcCycleTable` is constructed. The cycle table heap-allocates two objects: the `GhostlitePerformance` grid at `this+0x10` and the `MxuLatencyTable` at `this+0x18`. Each has its own constructor. This page covers the first.

```c
// GfcCycleTable::GfcCycleTable @0x1c89eec0
v4 = operator new(0x30u); sub_1C8D3740(v4);   // GhostlitePerformance grid → this+0x10
v6 = operator new(0xA0u); sub_1C8BB1C0(v6);   // MxuLatencyTable          → this+0x18
```

The `0x30`-byte object is the six-qword `GhostlitePerformance` header (two `{ptr, size, cap}` triples); the grid *body* is allocated separately on the heap.

### Structure

`GhostlitePerformance` is the shared grid-family layout (identical across PF/VF/GL/GF, only widths differ — see [Performance Family Overview](performance-overview.md)):

```c
struct GhostlitePerformance {        // 0x30-byte header, body on the heap
    int32*       latency;            // +0x00 ; new 0x744 = 1860 B = 465 int32, memset 0xff
    u64          latency_size;       // +0x08 ; = 0x1d1 = 465
    u64          latency_cap;        // +0x10 ; = 0x1d1
    vector<int>* grid;               // +0x18 ; new 0x2b98 = 11160 B = 465 × 24
    u64          grid_outer_count;   // +0x20 ; = 0x1d1 = 465  (the GetResourceUsage outer bound)
    u64          grid_outer_cap;     // +0x28 ; = 0x1d1
    // each grid row is a 24-B std::vector<int>:
    //   { int* data (new 0x7c = 124 B = 31 int32, zero-init), size = 0x1f = 31, cap = 0x1f }
};
```

The constructor `sub_1C8D3740` does not decompile (Hex-Rays returns no `cfunc` for this 27 143-byte fill function); every fact below is read from its disassembly. The opening sequence is byte-exact:

```text
; sub_1C8D3740 @0x1c8d3740 (head)
new(0x744)               ; latency array → [rbx+0]
[rbx+0x10] = 0x1d1       ; latency cap   = 465
memset(latency, 0xff, 0x744)
[rbx+0x08] = 0x1d1       ; latency size  = 465
new(0x2b98)              ; grid body     → [rbx+0x18]
[rbx+0x28] = 0x1d1       ; grid cap      = 465
loop r13 = 0x10 .. 0x2ba8 step 0x18:    ; 465 rows, 24-B stride
    new(0x7c)            ; row data = 31 int32
    vxorps + 4× vmovups  ; zero-init 124 B
    [row+0x10] = 0x1f    ; row cap  = 31
    [row+0x08] = 0x1f    ; row size = 31
[rbx+0x20] = 0x1d1       ; grid outer count = 465
; then 465 latency stores + 285 grid-cell stores follow
```

> **NOTE —** the constructor backs the cleanly-symbolized accessors `xla::ghostlite::GhostlitePerformance::GetResourceUsage` / `GetResources` / `GetLatency`, so the *class* is correctly named in the binary; only the GF-variant *constructor* is unnamed (`sub_1C8D3740`). The named ctor symbol `_ZN3xla9ghostlite20GhostlitePerformanceC1Ev` belongs to the **GL** variant (`@0x1c8cbc80`, 476 rows). The two are distinct functions with distinct allocation sizes (`0x744`/`0x2b98` vs `0x770`/`0x2ca0`) and distinct cell values — confirm them per-gen.

### Store accounting

The constructor emits exactly **750** `mov DWORD PTR [reg+disp], imm` stores: 465 latency entries + 285 grid cells. Every latency store is guarded by `cmp [rbx+8], idx; jbe <trap>` and writes `latency[idx]` at file-relative offset `idx*4`, which fixes the latency-array index as a dense `0..464`. The latency store for instruction `0x151`, for example, is `mov dword ptr [rax+0x544], 0x7f` — and `0x544 = 0x151 * 4`, so `latency[0x151] = 127`. This direct-offset addressing is the byte-level proof that the **outer grid index is the LloOpcode** for every priced row (the classifier `GetGhostliteInstruction` is an identity on these rows).

---

## The GetResourceUsage Read Path

### Purpose

`GetResourceUsage(instr, res)` is the single accessor every grid reader goes through. It is **byte-identical** to the GL accessor and the rest of the grid family — two bounds checks, a `lea`-computed 24-byte row stride, a direct cell read — so one description covers GF, GL, VF, and PF.

### Algorithm

```c
// xla::ghostlite::GhostlitePerformance::GetResourceUsage @0x1c8d3700
__int64 GetResourceUsage(perf, u32 instr, u8 res) {
    if (perf[0x20] <= instr)               // outer bound: grid_outer_count = 465
        BUG();                              // ud2
    row_base = perf[0x18];                  // grid body ptr
    v = 3 * instr;                          // lea rax+rax*2
    if (row_base[8*v + 8] <= res)           // inner bound: row.size = 31
        BUG();
    return *(u32*)(row_base[8*v] + 4*res);  // grid[instr][res]
}
```

`8 * (3 * instr)` is the 24-byte row stride (each `std::vector<int>` header is 24 bytes); `row_base[8*v]` is the row's `data` pointer, `row_base[8*v+8]` is its `size`. The inline twin `sub_1C8DA1A0` — reached from the cost-table throughput dispatch — is instruction-for-instruction identical.

`GetLatency(instr)` `@0x1c8d36e0` is the simpler sibling: `if (perf[1] <= instr) BUG(); return latency[instr]` (bound `[perf+8]`, returns the pipeline depth the scheduler raises a true-dependency edge to).

```c
// xla::ghostlite::GhostlitePerformance::GetLatency @0x1c8d36e0
__int64 GetLatency(perf, u32 instr) {
    if (perf[1] <= instr) BUG();            // latency_size = 465
    return *(u32*)(perf[0] + 4*instr);
}
```

> **GOTCHA —** the outer index is `GhostlitePerformance::Instruction`, the output of the per-gen classifier `GetGhostliteInstruction @0x1c8b1740` (a binary search over a WORD table, range-seeded at 258, reached via GOT relocation), **not** the raw LLO opcode read off the instruction word. For the priced rows the classifier resolves to the same ordinal as the LloOpcode (proven by the `latency[0x151]` direct-offset store above), so the axis *is* the opcode for those rows; but a reimplementation that indexes the grid by the raw opcode without the classifier will mis-key the unpriced and MXU-fanned rows.

### Column order — `GetResources`

`GetResources()` `@0x1c8d36c0` returns the static `kResources` byte array `@0xb43cdc4` (the `lea` target, `edx = 0x1f = 31`): the 31 resource indices in fill / traversal order. The bytes, read from the binary:

```text
GhostlitePerformance kResources @0xb43cdc4 (31 B) — shared by GL and GF:
  1d 1c 06 03 04 1a 07 08 09 0a 14 10 18 1e 0b 11 00 19 12 13 0c 15 0d 16 0e 17 1b 01 02 05 0f
```

This is a permutation of `{0..30}` (all 31 indices present, no repeats). The Xlu deposit index `0x10` is at **traversal position 11** (the array ends in `…02 05 0f`, i.e. res `0x0f` is the last column visited). `GetResourceLatency @0x1c8b1e60` iterates this array — it calls `GetResources()` then loops `GetResourceUsage(instr, kResources[i])` per column — to reduce a row into a single per-instruction latency (with a per-column overlap rule — most columns take a plain max, an MXU resource hits the fatal `"Did not expected to have MXU resource for "` log, and res `0x19` carries a special-cased FIFO-push/pop adjustment).

> **NOTE —** there is a single `xla::ghostlite::GhostlitePerformance::GetResources()::kResources` symbol in the binary, at `@0xb43cdc4`. Both the GL (476-row) and GF (465-row) instances are the same class and return this same array from the same `GetResources` method; they do **not** have distinct per-gen permutations. The sibling classes have their own arrays at adjacent addresses: `PufferfishPerformance::kResources @0xb43cd94` (20 B), `ViperfishPerformance::kResources @0xb43cda8` (28 B). The address `0xb43cde3` is simply `0xb43cdc4 + 0x1f` — the byte immediately past the 31-entry GhostlitePerformance array, not a second array. Bind the column order to `@0xb43cdc4`.

---

## The 31 Resource Columns

### Purpose

The grid's inner axis is `GhostlitePerformance::Resource`, a 31-value enum of intra-op EUP / cross-lane / Xlu / matrix-result micro-pipeline ports. There is **no `Resource::ToString`** in the binary, so the columns are named functionally — by reading which LLO-instruction class deposits cycles into each (the outer index being the opcode) and by anchoring against the two named accessors `GetXluPathReservation` and `GetResourceLatency`. These names are reimplementation-grade in *meaning* (what physical port each column reserves) but are not literal symbol names.

> **NOTE —** this 31-wide `GhostlitePerformance::Resource` enum is a *different, lower-level* enum than the 23-slot per-bundle `ResourceVector` ([Resource Enum](resource-enum.md)). The grid prices the intra-op micro-pipeline-stage holds; the `ResourceVector` is the per-bundle functional-unit accumulator the higher-level cost model deposits into. `kResources` gives the grid's column traversal order, not the `ResourceVector` slot order.

### The columns, by occupant LLO class

| col | role (functional name) | occupant LLO band (GF) | example cells |
|---|---|---|---|
| r0 | address / indexed load-store generation | `kParameterAddress`, `kVectorLoadIndexed` | 2 |
| r1 | vector load/store sublane-shuffle (Cmem) | `kVectorLoadSublaneShuffle`, `kVectorLoadIndexed` | 2 |
| r2 | scalar store / sync-flag (SFRF) | `kScalarStore`, `kVectorSyncFlag{Add,Set}*` | 1, 2 |
| r3..r6 | EUP transcendental-prep stages A–D | `kVector{Subtract,Pack,Tanh,Pow2,Rsqrt,…}` F32/Bf16 | 16/4/9/3, 20/8/13/7 |
| r7 | EUP gain-latch / AndPop setup | `kVector{SigShft,Sinq,Cosq,Erf}F32AndPop` | 52 |
| r8..r11 | EUP-result-pop FIFO stages A–D | `kVector{Tanh,Pow2,Reciprocal,…}Bf16AndPop` | 2/1/3/9, 6/5/7/13 |
| r12..r15 | cross-lane / transpose-result stages A–D | `kVector{EupResult,XlaneResult}*`, `kVectorMultiply*` | 35/34/40/18 |
| **r16** | **Xlu / matrix-result deposit port** | `kVectorCmemResult`, `kVectorMatresAdd`, `kVectorTransposeClear`, `kVectorMultiplyF32` | **4** |
| r17..r20 | mxres-result sub-stages A–D (cmem/matres extended) | `kVectorCmemResult`/`MatresAdd`/`TransposeClear` | 40/44/25/3 |
| r21..r24 | pack / extract / U64-multiply stages A–D | `kVector{Xor,PseudoPack,MultiplyU64,Extract}*` | 21/50/50/32 |
| r25 | scalar-move | `kScalarMove` | 1 |
| r26..r27 | vector-min (bf16 / u32) | `kVectorMinimum{Bf16,U32}` | 4, 1 |
| r28..r30 | BarnaCore scalar-sync-wait / public-access read | `kVsetptstate`, `kBarnaCoreScalarWait{Lt,Ge,Gt,Eq,Ne}*`, `…SyncPublicAccessRead` | 5/7/3 |

The Xlu/matrix-result deposit column (res `0x10`) is the only column the convolution cost model reads through the throughput dispatch, and its cell is uniformly **4**. It is named CERTAIN because two independent anchors agree: the disassembly stores `4` into it for every matrix-result op, and the dedicated accessor `GetXluPathReservation` (the GL twin) reads exactly this column.

---

## The Xlu Deposit Column and the Throughput Read

### Purpose

One column carries the matrix-result (Xlu) throughput that the convolution cost model reads as its `R[2]` term. On GF the cost-table helper reads **res `0x10`** (= 16); on GL the corresponding helper reads res `0x0f` (= 15). This is a hardcoded difference between the two `GetCyclesForThroughputHelper` bodies, not a difference in the grid or in `kResources` (which are shared): each helper passes its own literal `res` to the same `GetResourceUsage`. The constructor writes `4` into the res-`0x10` cell for every matrix-result / Cmem / transpose-result instruction:

```text
; per matrix-result row in sub_1C8D3740:
mov dword ptr [row + 0x40], 4    ; row[0x40] = grid[instr][res 0x10] = 4
```

(`0x40 = res 16 * 4 bytes`.) The six convolution-relevant mxres rows write this cell at `@0x1c8d7722 / 7866 / 79aa / 7bea / 7ce6 / 82ce`; many more matrix-result rows in the cross-lane band (0x14d..0x158) write it too — 28 cells carry res `0x10` in total.

### The cost-table binding

The GF cost path does **not** reach res `0x10` through `LatencyTableGhostlite::GetXluPathReservation` (that accessor reads res `0x0f` — see the contrast below). It reaches it through the throughput dispatch `GfcCycleTable::GetCyclesForThroughputHelper @0x1c89f400`, whose Xlu-family cases call the inline `GetResourceUsage` twin with `res = 16`:

```c
// GfcCycleTable::GetCyclesForThroughputHelper @0x1c89f400 (Xlu/mxres cases)
case 23: instr = 344 /*0x158*/; goto xlu;   // CT 0x17
case 27: instr = 351 /*0x15f*/; goto xlu;   // CT 0x1b
case 28: instr = 337 /*0x151*/; goto xlu;   // CT 0x1c
case 29: instr = 339 /*0x153*/; goto xlu;   // CT 0x1d
case 30: instr = 341 /*0x155*/; goto xlu;   // CT 0x1e
case 31: instr = 345 /*0x159*/; goto xlu;   // CT 0x1f
xlu:    result = sub_1C8DA1A0(perf /*GfcCycleTable+0x10*/, instr, 16);   // res 0x10
```

So `thru(CT 0x17/0x1b/0x1c/0x1d/0x1e/0x1f) = grid[instr][0x10] = 4` for all six. The six mxres rows and their full populated cells, byte-exact from the constructor:

| `Instruction` | LLO name | `GetLatency` | populated cells (res:cy) |
|---|---|---|---|
| `0x151` | `kVectorCmemResult` | 127 | r16:4 r17:40 r18:44 r19:25 r20:3 |
| `0x153` | `kVectorMatresAdd` | 101 | r16:4 r17:8 r18:11 r19:35 r20:3 |
| `0x155` | `kVectorTransposeClear` | 113 | r16:4 r17:4 r18:20 r19:39 r20:3 |
| `0x158` | `kVectorMultiplyF32` | 127 | r12:35 r13:34 r14:40 r15:18 r16:4 |
| `0x159` | `kVectorMultiplyU32` | 114 | r12:35 r13:34 r14:40 r15:18 r16:4 |
| `0x15f` | `kVectorXorU32` | 141 | r16:4 r21:21 r22:50 r23:50 r24:32 |

> **CONTRAST —** `LatencyTableGhostlite::GetXluPathReservation @0x1c8b21c0` tail-calls `GetResourceUsage(perf, GetGhostliteInstruction(v), 15)` — res `0x0f` — after a direct-handled case for `kVectorSetPermutePattern` (opcode `0x8b` = 139, returns `3·(is_transpose != 0) + 1`). This `LatencyTableGhostlite` accessor (shared `ghostlite` namespace) is *not* the GF convolution path; the GF conv `R[2]` is read by `GfcCycleTable::GetCyclesForThroughputHelper` with res `0x10` (16), while `GlcCycleTable::GetCyclesForThroughputHelper @0x1c89ed20` reads res `0x0f` (15). The Xlu column index is therefore bound per **cost-table helper**, not per `kResources` array: GF helper `0x10`, GL helper `0x0f`, both indexing the one shared 31-column grid.

### The conv `R[2]` term

The `6acc60406` convolution cost triple is now fully numeric. The other two terms come from the [GF MXU reservation matrix](mxu-latency-gf.md):

- `R[0]` matpush = matpush_count · `array[8]` = **2** (bf16) / **4** (fp8)
- `R[1]` matmul = op_count · `array[3]` · 0.5 / Target / EPF, with `array[3]` = **4** (bf16) / **8** (fp8)
- `R[2]` Xlu = `grid[mxres-instr][0x10]` · ChunksPerTile · rem = **4** · ChunksPerTile · rem (dtype-independent)

---

## Row Classification and Column Population

### Purpose

Only ~92 of the 465 rows are populated; the grid is sparse. The populated rows cluster into recognizable instruction bands that mirror the EUP / matrix-result micro-pipeline. This section gives the band structure (not all 465 rows — the dense bands are the EUP/transcendental and matrix-result clusters, opcodes `0x121..0x16x`) and the per-column cell counts.

### The bands (GF)

| band | rows (GhPerf::Instruction) | columns | latency | role |
|---|---|---|---|---|
| address / load-store / sync | 0x2d, 0x30, 0x32, 0x78..0x7f | r0..r2 | 1–3 | address gen, scalar store, sync-flag |
| EUP transcendental-prep | 0x121..0x13b | r3..r6 | 212 / 204 | per-dtype prep stages (carry {4 bf16 / 8 fp8}) |
| EUP gain-push setup | 0x140..0x143 | r7 | 1 | `AndPop` gain-latch setup (52) |
| EUP-result-pop FIFO | 0x144..0x14b | r8..r11 | 4 / 8 | drain stages after a transcendental |
| cross-lane / transpose result | 0x14c..0x159 | r12..r15 | 1, 114–127 | permute / rotate / multiply result stages |
| **Xlu / matrix-result deposit** | 0x14d..0x158 (matres ops) | **r16** | — | the conv `R[2]` cell (= 4) |
| mxres-result sub-stages | 0x151/0x153/0x155/0x157 | r17..r20 | 101–141 | cmem / matres / transpose extended |
| pack / extract / U64 | 0x15f..0x168 | r21..r24 | 141, 145 | `Xor`/`PseudoPack`/`MultiplyU64`/`Extract` |
| tail (move / min / BarnaCore) | 0x17b, 0x1a1..0x1b6 | r25..r30 | 1, 3, 7 | scalar-move, vector-min, BarnaCore scalar-sync-wait |

### Per-column cell counts (GF, total 285)

```text
r0:2  r1:2  r2:7  r3:15 r4:27 r5:27 r6:27 r7:4  r8:8  r9:8  r10:8 r11:8
r12:11 r13:11 r14:11 r15:11 r16:28 r17:4 r18:4 r19:4 r20:4
r21:10 r22:10 r23:10 r24:10 r25:1 r26:1 r27:1 r28:4 r29:6 r30:1
```

The EUP-prep stages r4/r5/r6 (27 cells each) and the Xlu deposit r16 (28 cells) are the most-populated columns — the grid is overwhelmingly an EUP/transcendental + matrix-result pricing table. Note the EUP-prep column r4 carries the same `{4 bf16 / 8 fp8}` per-dtype magnitude as the `MxuLatencyTable` matmul cell `array[3]` ([MXU Latency: GF](mxu-latency-gf.md)): the two cost tables agree on the per-dtype throughput integers.

---

## The 0xff-Default Fallback

### Purpose

The latency array is `memset` to `0xff` (255) before any stores. On the wide GhPerf generations (GL/GF) only the priced rows are subsequently overwritten — ~92 of 465 on GF. The other ~373 rows keep `latency = 255` and their grid rows stay all-zero. Those instructions are **not** priced by this grid; their cost comes from a different path.

### The fallback path

An instruction whose `GhostlitePerformance::Instruction` row is unwritten resolves its cost through the gen-invariant `CycleTable::GetResource` op→slot table (`@0x1c89ce20`, table `@0xb438aec`) plus the per-gen `GetCyclesForThroughput`, deposited into the 23-slot `ResourceVector` ([Resource Enum](resource-enum.md)); the `JfCycleTable` default for an unpriced op is 1 cycle. The grid is consulted only for the EUP / matrix-result / cross-lane family it actually prices.

> **GOTCHA —** the `0xff` default is a real, observable value, not a sentinel the read path filters. `GetLatency(instr)` for an unwritten row returns 255 — a deliberately large pipeline depth that keeps an unrecognized op from being scheduled aggressively. A reimplementation that zero-inits the latency array (instead of `0xff`-filling it) will under-cost every instruction the grid does not explicitly price, with no bounds error to signal the mistake. Initialize to 255.

---

## GF vs GL — the Two GhPerf Grids

The GF (`6acc60406`, v7) and GL (Ghostlite, v6e) grids are the **same `GhostlitePerformance` class** with the same methods, the same single `kResources` array, but two distinct heap instances built by two distinct constructors:

| | GF (`6acc60406` / TPU7x) | GL (Ghostlite / v6e) |
|---|---|---|
| Constructor | `sub_1C8D3740` (unnamed) | `GhostlitePerformanceC1` `@0x1c8cbc80` |
| Latency alloc | `new 0x744` → 465 int32 | `new 0x770` → 476 int32 |
| Grid alloc | `new 0x2b98` → 465 × 24 | `new 0x2ca0` → 476 × 24 |
| Row width | `new 0x7c` = 31 | `new 0x7c` = 31 |
| Populated | 285 cells / 92 rows | 358 cells / 132 rows |
| `kResources` | `@0xb43cdc4` (one shared array) | `@0xb43cdc4` (same array) |
| Xlu res read by helper | res `0x10` (`GfcCycleTable` helper) | res `0x0f` (`GlcCycleTable` helper) |
| EUP/transcendental latency | 212 (bf16) / 204 (fp8/F32) | 192 / 182 |
| Owner cycle table | `GfcCycleTable` `@0x1c89eec0` | `GlcCycleTable` `@0x1c89e7e0` |

The **+11 rows** GL has (476 vs 465) follow from the GL ctor's `new 0x770`/`new 0x2ca0` (476 entries) vs GF's `new 0x744`/`new 0x2b98` (465); the GL ctor writes `latency[465]` at `[rax+0x744]` — an index that is out of bounds for GF's 465-entry array. The extra opcodes are the v6e shift/saturate and BarnaCore-wait rows that lie past GF's last valid opcode; `6acc60406` drops them. The two share the class contract (object layout, the three accessor methods, the single `kResources`, the 31-wide enum, the EUP/matrix-result band structure) and the conv `R[2]` value of 4; what differs is the cell integers, base latencies, row count, and which `res` literal each cost-table helper passes to the shared `GetResourceUsage`.

---

## Worked Example — a `6acc60406` bf16 Conv Xlu Deposit

A `6acc60406` bf16 convolution prices its matrix-result (Xlu) term by reading `kVectorCmemResult` (`Instruction 0x151`, the conv mxres op for CT `0x1c`):

1. The cost-table dispatch `GfcCycleTable::GetCyclesForThroughputHelper @0x1c89f400` case 28 sets `instr = 0x151`, `res = 16`.
2. `GetResourceUsage(perf, 0x151, 16)` bounds-checks `0x151 < 465` and `16 < 31`, computes `row = grid + 0x151*24`, and returns `row.data[16] = 4`.
3. The conv cost model multiplies: `R[2] = 4 · ChunksPerTile · rem`.
4. Combined with the MXU reservation matrix ([MXU Latency: GF](mxu-latency-gf.md)): `R[0] = 2`, `R[1] = 4` for bf16 — the triple is `{2, 4, 4}`.

The op's `GetLatency(0x151) = 127` separately bounds its pipeline depth on dependency edges. For fp8 the matmul/matpush halves double to `{4, 8}` but the Xlu cell stays 4 — the grid cell is dtype-independent.

---

## Function and Data Map

| Name | Address | Role |
|---|---|---|
| `GfcCycleTable::GfcCycleTable` | `0x1c89eec0` | allocs `0x30` perf grid + `0xa0` MxuLatency |
| GF `GhostlitePerformance` ctor | `0x1c8d3740` | fills 465 latency + 285 grid cells (unnamed; no decompile) |
| `GhostlitePerformance::GetResourceUsage` | `0x1c8d3700` | `grid[instr][res]`; outer `[+0x20]`, 24-B stride, inner `[row+8]` |
| inline `GetResourceUsage` twin | `0x1c8da1a0` | byte-identical; the CT-throughput call target |
| `GhostlitePerformance::GetLatency` | `0x1c8d36e0` | `latency[instr]`, bound `[+0x8]` |
| `GhostlitePerformance::GetResources` | `0x1c8d36c0` | returns `kResources` |
| `GhostlitePerformance::…::kResources` | `0xb43cdc4` | 31-byte column traversal order (shared GL/GF) |
| `GfcCycleTable::GetCyclesForThroughputHelper` | `0x1c89f400` | Xlu cases → `GetResourceUsage(instr, 16)` |
| `GetGhostliteInstruction` | `0x1c8b1740` | LloOpcode → `GhPerf::Instruction` (binary search) |
| `LatencyTableGhostlite::GetResourceLatency` | `0x1c8b1e60` | iterates all 31 columns, per-column overlap reduce |
| `LatencyTableGhostlite::GetXluPathReservation` | `0x1c8b21c0` | `ghostlite` accessor — reads res `0x0f`; not the GF conv path |
| `CycleTable::GetResource` | `0x1c89ce20` | fallback op→slot table for `0xff`-default rows |

---

## Cross-References

- [Performance Family Overview](performance-overview.md) — the grid-family object layout, the shared `GetResourceUsage` read path, and the resource-count progression
- [Performance: GL (GhPerf 476×31)](performance-gl-ghperf.md) — the Ghostlite v6e grid; +11 rows, Xlu deposit res `0x0f`, base latency 192/182
- [MXU Latency: GF (6acc60406)](mxu-latency-gf.md) — the `MxuLatencyTable` reservation matrix, the conv `R[0]`/`R[1]` terms, and `ComputeDmaLevels`
- [Resource Enum](resource-enum.md) — the 23-slot per-bundle `ResourceVector`, distinct from the 31-wide `GhostlitePerformance::Resource` micro-pipeline enum
- [MXU Slot](../isa/slot-mxu.md) — the physical MXU sub-units the matrix-result resource columns reserve
