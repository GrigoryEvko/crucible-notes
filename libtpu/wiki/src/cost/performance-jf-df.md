# Performance: JF / DF

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ. Every integer below was read out of `.rodata` by hand and re-resolved against the IDA decompile; the verification status is in the Confidence columns.*

## Abstract

The **Jellyfish (TPU v2)** and **Dragonfish (TPU v3)** generations price every TensorCore instruction through a single `Performance` object: `platforms_deepsea::jellyfish::isa::Performance`. Unlike every later generation — which heap-allocates a `latency[]` array plus a 2-D `Instruction × Resource` reservation grid read by `GetResourceUsage` (the model that begins at Pufferfish, see [Performance Family Overview](performance-overview.md)) — JF/DF use **one fixed `0xe00`-byte (3584-byte) inline POD struct** with no separate latency array, no 2-D grid, and no `GetResourceUsage` method at all. Both the throughput cells and the latency-feeding cells live as scattered `int32` slots inside that one buffer, and a per-instruction offset LUT picks the right slot. This is the simplest cost geometry of all six codenames — fitting for the 1-MXU JF — and it is also the only one where the entire next-generation cost delta is **two integers**.

The flat model has two read paths into the one struct:

- **Throughput** — `JfCycleTable::GetCyclesForThroughput(Instruction)` reads `Performance[offsetLUT[Instruction]]`, gated by a 33-bit valid-instruction mask. Sixteen of the 33 `CycleTable::Instruction` ordinals are priced from the struct; the other seventeen short-circuit to the default `1`. The seven priced MXU matprep/matmul/matrix-result ports cost **8 cycles**; the nine priced vector/EUP result stages cost **1**. A companion flat LUT, `CycleTable::GetResource(Instruction)`, maps each ordinal to one of **7** `Resource` columns — the first seven slots of the 23-slot `ResourceVector` (`Matpush`, `Matmul`, `Xlu`, `VectorAlu0`, `VectorAlu1`, `VectorAluAny`, `VectorEup`).
- **The DF delta** — `PerformanceDf` is `PerformanceJf` plus a vtable swap and exactly **one** quadword store: `[+0x28] = 0xD00000042`, which writes `[+0x28] = 0x42 = 66` (matmul base) and `[+0x2c] = 0x0D = 13` (matprep base). Neither cell is an `offsetLUT` target, so the throughput grid is byte-identical JF/DF; the entire v2→v3 cost-model difference is these two latency-feeding integers.

This page is the JF/DF half of the `Performance` family: the inline-POD layout, the full per-instruction latency/throughput grid (all 33 ordinals), the 7-column `Resource` naming, the pre-baked `.rodata` constant blocks that fill the struct, and the 2-cell JF→DF delta. The latency axis that consumes these cells — the `LatencyTableJellyfish` 15-field copy map and the matmul/matprep base latencies — folds into this same `Performance` struct and is documented in full on [MXU Latency: JF / DF](mxu-latency-jf-df.md); this page links it rather than re-deriving it.

For reimplementation, the contract is:

- The `Performance` object layout: one `0xe00`-byte inline POD, vtable + `DeviceIdentifiers` head, an 890-`int32` cost buffer over `[+0x18 .. +0xdf8]`, default-filled with the sentinel `0x7fffffff`.
- The `GetCyclesForThroughput` read path: the `< 0x21` bound, the 33-bit valid mask `0x19FFC0821`, the 33-entry `int64` offset LUT, and the `Performance[offset]`-else-`1` resolution.
- The full per-instruction grid: all 33 `CycleTable::Instruction` ordinals → (`Resource` column, `offsetLUT` byte offset, priced flag, cycle value), JF and DF.
- The 7 `Resource` columns named as the first seven `ResourceVector` slots, via the `AccumulateInstructionUsage → Acc` consumer.
- The 2-cell DF override (`[+0x28]`/`[+0x2c]`) and the proof that the throughput grid is otherwise byte-identical.

| | |
|---|---|
| **Performance class** | `platforms_deepsea::jellyfish::isa::Performance` (one inline POD, no 2-D grid) |
| **Object size** | `0xe00` (3584 B); 890-`int32` cost buffer `[+0x18 .. +0xdf8]`; default sentinel `0x7fffffff` |
| **Factory** | `Performance::CreateTensorCore` `@0x1d4927e0` — `new 0xe00`; JF/DF device-id dispatch |
| **JF ctor** | `PerformanceJf::PerformanceJf` `@0x1d4930c0` — 116 store ops → 419 of 890 `int32` slots (vtable `@0x21cc74b8`) |
| **DF ctor** | `PerformanceDf::PerformanceDf` `@0x1d493060` — `= Jf` + vtable swap (`@0x21cc7468`) + one qword store `[+0x28]=0xD00000042` |
| **Throughput reader** | `JfCycleTable::GetCyclesForThroughput(Instruction)` `@0x1c89dce0` |
| **Throughput formula** | `valid = (I < 0x21) && ((0x19FFC0821 >> I) & 1)`; `valid ? Performance[offsetLUT[I]] : 1` |
| **offsetLUT** | `@0xb438b70` (33 × `int64`, `Instruction → Performance` byte offset) |
| **Resource reader** | `CycleTable::GetResource(Instruction)` `@0x1c89ce20` → `resLUT[I]` |
| **resLUT** | `@0xb438aec` (33 × `int32`, values 0..6 = first 7 `ResourceVector` slots) |
| **Priced ordinals** | 16 of 33: `{0x00,0x05,0x0b,0x12,0x13,0x14,0x15,0x16,0x17,0x18,0x19,0x1a,0x1b,0x1c,0x1f,0x20}` |
| **MXU throughput** | 8 cycles/op (7 priced matprep/matmul/matres cells); vector/EUP result = 1 |
| **JF→DF delta** | exactly 2 `int32` cells: `[+0x28]` 88→66 (matmul base), `[+0x2c]` 8→13 (matprep base) |

---

## The Performance Object — Flat Inline POD

### Purpose

`Performance` answers the two questions the scheduler needs per instruction — how many cycles an instruction occupies each functional-unit port (throughput, read by `GetCyclesForThroughput`) and how deep its pipeline is (latency, read out of the same struct by `LatencyTableJellyfish`). JF/DF answer both from one fixed buffer rather than the heap latency-array-plus-2D-grid that Pufferfish onward use. There are so few priced TensorCore instructions on the 1-MXU JF that a flat cell-per-`Instruction` LUT into a single struct suffices.

### Layout

The object is `0xe00` bytes. The decompile of the base constructor and `CreateTensorCore` pins the layout:

```c
struct Performance {                 // 0xe00 bytes (3584 B); built by CreateTensorCore @0x1d4927e0
    void*  vtable;                   // +0x00
    u64    device_id_lo;             // +0x08  DeviceIdentifiers low qword
    u32    device_id_hi;             // +0x10  DeviceIdentifiers dword
    bool   is_tensorcore;            // +0x14  (=1, the bool ctor arg from CreateTensorCore)
    int32  buf[890];                 // +0x18 .. +0xdf8 ; default = 0x7fffffff (INT_MAX) sentinel
};
```

`Performance::CreateTensorCore` `@0x1d4927e0` is the factory: it `operator new(0xE00u)`s the object, then dispatches on the `DeviceIdentifiers` — `kJellyfishIdentifiers` → `PerformanceJf`, `kDragonfishIdentifiers` → `PerformanceDf`, anything else `LogFatal("Don't know how to create performance for …")`. The two device-id records differ in one byte (JF `…e01a4e00…` vs DF `…e01a4f00…`). The singleton is cached in the file-scope `TpuPerformanceTable(version)::table`, built once via a `_cxa_guard`-protected lazy init inside the `LatencyTableJellyfish` constructor.

The base constructor `Performance::Performance(DeviceIdentifiers&, bool)` `@0x1d492900` sets the vtable + `DeviceIdentifiers` head, then `memset`-fills the whole cost buffer `[+0x18 .. +0xdf8]` with the sentinel **`0x7fffffff`** — broadcast from `.rodata @0x84a2f60` (read byte-exact as `0x7fffffff`, decimal 2147483647). This is `INT_MAX`, **not** `0xffffffff` — distinct from the `0xff`-memset latency arrays of the heap family. A sentinel cell means "unset / use the default `1`"; in practice it is never read for an unpriced ordinal because the valid mask short-circuits first.

> **NOTE —** the 890-slot buffer is far larger than the 33 throughput ordinals or the 15 latency-table cells need. The JF ctor writes only 419 of the 890 slots; the other 471 stay at the sentinel. Of the 419 populated cells, only ~31 are bound to a known consumer here (16 offset-LUT targets + 15 `LatencyTableJellyfish` copies); the rest are byte-exact in the reconstructed image but their per-cell cost-model role is not traced (LOW — see [Open Items](#open-items)).

### The JF→DF Delta — Two Cells

`PerformanceDf` `@0x1d493060` is `PerformanceJf` plus a vtable swap and exactly one quadword store, verbatim from the decompile:

```c
// platforms_deepsea::jellyfish::isa::PerformanceDf::PerformanceDf  @0x1d493060  (verified)
PerformanceJf::PerformanceJf(this, dev);          // build the full JF image first
*(uint64_t*)this        = off_21CC7478;           // swap the vtable (PerformanceDf)
*((uint64_t*)this + 5)  = 0xD00000042uLL;         // store at this+0x28 (= int32[5..6])
```

`this + 5` (qword) is `Performance[+0x28]`. The qword `0xD00000042` writes `[+0x28] = 0x42 = 66` and `[+0x2c] = 0x0D = 13`:

| cell | JF | DF | role | Confidence |
|---|---|---|---|---|
| `Performance[+0x28]` | 88 | **66** | MXU matmul base latency (→ `LatencyTable[+0x50]`) | CERTAIN |
| `Performance[+0x2c]` | 8 | **13** | MXU matprep base latency (→ `LatencyTable[+0x4c]`) | CERTAIN |

A diff of the two reconstructed in-memory images shows **exactly these two cells** change; every other one of the 419 populated slots is byte-identical. Because neither `+0x28` nor `+0x2c` is an `offsetLUT` target, `GetCyclesForThroughput` returns the same value on JF and DF for all 16 priced ordinals — the entire v2→v3 cost difference is these two latency-feeding integers, consumed downstream by `LatencyTableJellyfish` (covered on [MXU Latency: JF / DF](mxu-latency-jf-df.md)).

> **NOTE — the v2→v3 MXU change shows up only in the matmul base latency.** Dragonfish doubles the MXU count (1→2) and raises the TensorCore clock. The throughput cell stays at 8 cycles/op; the speedup is encoded as a *lower* matmul base latency (88→66) with a slightly *higher* matprep base (8→13). A reimplementation that scales the throughput cell for DF would double-count the v3 advantage; the throughput grid below is shared verbatim by both gens.

---

## The Throughput Read Path

### GetCyclesForThroughput

`JfCycleTable::GetCyclesForThroughput` is a four-line function. It bounds the `Instruction` ordinal at `< 0x21`, tests it against a 33-bit valid mask, and on a hit indexes the `Performance` struct (held at `JfCycleTable+0x10`) by a byte offset taken from a 33-entry `int64` LUT. On a miss it returns the default `1`. Verbatim from the decompile:

```c
// xla::jellyfish::JfCycleTable::GetCyclesForThroughput  @0x1c89dce0  (verified)
__int64 JfCycleTable::GetCyclesForThroughput(this, unsigned int instr) {
    if ( ((instr < 0x21) & (uint8_t)(0x19FFC0821uLL >> instr)) == 1 )
        return *(uint32_t*)( *(uint64_t*)(this + 0x10)        // Performance*
                             + qword_B438B70[instr] );         // offsetLUT[instr]
    return 1;                                                   // default
}
```

The valid mask `0x19FFC0821` selects sixteen priced ordinals; the other seventeen short-circuit to `1`. The `offsetLUT` slot for every *unpriced* ordinal is literally `0x0`, which is harmless because the mask test always fails before the read is reached.

> **GOTCHA —** a reimplementation must apply the bound `< 0x21` *and* the 33-bit mask. Relying on the `offsetLUT` alone would read `Performance[0]` (the vtable pointer) for every unpriced ordinal, since their LUT slot is `0x0`. The bound and mask are not redundant: the bound guards the 33-entry LUT, the mask selects the priced subset.

### GetResource — the Resource Column

`CycleTable::GetResource` is a single flat lookup — each `Instruction` maps to one of seven columns:

```c
// xla::jellyfish::CycleTable::GetResource  @0x1c89ce20  (verified)
__int64 CycleTable::GetResource(this, int instr) {
    return dword_B438AEC[instr];          // resLUT[instr], 33 x int32, values 0..6
}
```

The seven distinct values `0..6` are not a private enum — they are slot indices into the 23-slot `ResourceVector`. The only consumer, `AccumulateInstructionUsage` `@0x144fd720`, calls `ResourceVector::Acc(GetResource(I), (double)GetCyclesForThroughput(I))`, and `Acc` `@0x1c89adc0` indexes `[ResourceVector + Resource*8]` with a hard bound of 23:

```c
// xla::jellyfish::ResourceVector::Acc  @0x1c89adc0  (verified)
__int64 ResourceVector::Acc(this, unsigned int resource, double cycles) {
    if ( resource >= 0x17 ) __ud1();      // bound 0x17 = 23 ResourceVector slots
    this[resource] += cycles;             // vaddsd [rdi + resource*8]
    return resource;
}
```

So the seven JF/DF `Resource` columns are the **first seven** `ResourceVector` slots. The JF/DF cost model populates only the MXU/vector head of the 23-slot accumulator; the memory, ICI, and SparseCore slots `R[7..22]` are deposited into by other cost paths, not by this flat LUT. See [Resource Enum](resource-enum.md) for the full 23-slot vector and `MaxResourceCycles` reduction.

| Res | `ResourceVector` slot | name | occupant JF `Instruction` band | Confidence |
|---|---|---|---|---|
| r0 | `R[0]` `+0x00` | `Matpush` | matmul/latch ops (`Instr 0x05..0x10`; `GainLatchMode` expansion) | CERTAIN |
| r1 | `R[1]` `+0x08` | `Matmul` | matprep ops (`Instr 0x00..0x04`; `MatmulDataFormat` expansion) | CERTAIN |
| r2 | `R[2]` `+0x10` | `Xlu` | matrix-result / cross-lane (`Instr 0x17, 0x1b..0x1f`) | CERTAIN |
| r3 | `R[3]` `+0x18` | `VectorAlu0` | vector ALU lane 0 (`Instr 0x14`) | CERTAIN |
| r4 | `R[4]` `+0x20` | `VectorAlu1` | vector ALU lane 1 (`Instr 0x12, 0x13`) | CERTAIN |
| r5 | `R[5]` `+0x28` | `VectorAluAny` | vector ALU "any" lane (`Instr 0x15, 0x16, 0x19, 0x20`) | CERTAIN |
| r6 | `R[6]` `+0x30` | `VectorEup` | vector extended-precision (`Instr 0x11, 0x18, 0x1a`) | CERTAIN |

> **CORRECTION (PERF-JFDF-1) —** an earlier reading labeled the columns functionally as "MXU matmul-issue" (r0) and "MXU matprep" (r1). The binding-confirmed names overturn the pairing: the `resLUT` maps matprep (`Instr 0x00`) → `r1` `Matmul` and the matmul/latch ordinals (`Instr 0x05`) → `r0` `Matpush`. The names above come from the `AccumulateInstructionUsage → Acc` consumer path and match `ResourceVectorToString` `@0x1c89bde0` slot-for-slot; the column index *is* the `ResourceVector` slot index.

---

## The Per-Instruction Grid

This is the full reconstruction of the JF/DF throughput grid over all 33 `CycleTable::Instruction` ordinals. The `offsetLUT` (`@0xb438b70`) and `resLUT` (`@0xb438aec`) columns were read byte-for-byte out of `.rodata`; the cycle value is the priced cell resolved in the reconstructed `PerformanceJf` in-memory image. JF and DF are identical for every cell (none of the priced offsets is `+0x28` or `+0x2c`, the only two DF overrides), so one column serves both.

| `Instr` | `offsetLUT[I]` | `Res` | `ResourceVector` slot | priced | JF/DF cyc | source modifier (MXU band) | Confidence |
|---|---|---|---|---|---|---|---|
| `0x00` | `0x910` | r1 | `Matmul` | yes | **8** | matprep · `MatmulDataFormat=0` | CERTAIN |
| `0x01` | `0x000` | r1 | `Matmul` | no | 1 | matprep · fmt 1,2,3,10 | CERTAIN |
| `0x02` | `0x000` | r1 | `Matmul` | no | 1 | matprep · fmt 8 | CERTAIN |
| `0x03` | `0x000` | r1 | `Matmul` | no | 1 | matprep · fmt 9 | CERTAIN |
| `0x04` | `0x000` | r1 | `Matmul` | no | 1 | matprep · fmt 4,5,6,7 | CERTAIN |
| `0x05` | `0x92c` | r0 | `Matpush` | yes | **8** | matmul · `GainLatchMode` 0x0,0x2,0x4 | CERTAIN |
| `0x06` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0xb,0xe,0x10 | CERTAIN |
| `0x07` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0x30 | CERTAIN |
| `0x08` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0x32 | CERTAIN |
| `0x09` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0xc,0x12,0x14,0x16,0x18 | CERTAIN |
| `0x0a` | `0x000` | r0 | `Matpush` | no | 1 | (unmapped) | CERTAIN |
| `0x0b` | `0x92c` | r0 | `Matpush` | yes | **8** | matmul · `GainLatchMode` 0x1,0x3,0x5 | CERTAIN |
| `0x0c` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0xa,0xf,0x11 | CERTAIN |
| `0x0d` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0x31 | CERTAIN |
| `0x0e` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0x33 | CERTAIN |
| `0x0f` | `0x000` | r0 | `Matpush` | no | 1 | matmul · latch 0xd,0x13,0x15,0x17,0x19 | CERTAIN |
| `0x10` | `0x000` | r0 | `Matpush` | no | 1 | (unmapped) | CERTAIN |
| `0x11` | `0x000` | r6 | `VectorEup` | no | 1 | (non-MXU) | CERTAIN |
| `0x12` | `0x33c` | r4 | `VectorAlu1` | yes | 1 | (non-MXU; EUP/vector-result) | CERTAIN |
| `0x13` | `0x340` | r4 | `VectorAlu1` | yes | 1 | (non-MXU) | CERTAIN |
| `0x14` | `0x344` | r3 | `VectorAlu0` | yes | 1 | (non-MXU; cross-lane) | CERTAIN |
| `0x15` | `0x39c` | r5 | `VectorAluAny` | yes | 1 | (non-MXU; vector-ALU) | CERTAIN |
| `0x16` | `0x398` | r5 | `VectorAluAny` | yes | 1 | (non-MXU) | CERTAIN |
| `0x17` | `0x954` | r2 | `Xlu` | yes | **8** | (MXU matrix-result) | CERTAIN |
| `0x18` | `0x3f8` | r6 | `VectorEup` | yes | 1 | (non-MXU) | CERTAIN |
| `0x19` | `0x368` | r5 | `VectorAluAny` | yes | 1 | (non-MXU) | CERTAIN |
| `0x1a` | `0x3f4` | r6 | `VectorEup` | yes | 1 | (non-MXU) | CERTAIN |
| `0x1b` | `0x960` | r2 | `Xlu` | yes | **8** | (MXU matrix-result) | CERTAIN |
| `0x1c` | `0x94c` | r2 | `Xlu` | yes | **8** | (MXU matrix-result) | CERTAIN |
| `0x1d` | `0x000` | r2 | `Xlu` | no | 1 | (MXU-result, default) | CERTAIN |
| `0x1e` | `0x000` | r2 | `Xlu` | no | 1 | (MXU-result, default) | CERTAIN |
| `0x1f` | `0x958` | r2 | `Xlu` | yes | **8** | (MXU matrix-result) | CERTAIN |
| `0x20` | `0x39c` | r5 | `VectorAluAny` | yes | 1 | (non-MXU) | CERTAIN |

The sixteen priced ordinals are exactly `{0x00, 0x05, 0x0b, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1f, 0x20}`. The seven **8-cycle** cells are the MXU matprep/matmul/matrix-result throughput ports (`0x00`, `0x05`, `0x0b`, `0x17`, `0x1b`, `0x1c`, `0x1f`); the nine **1-cycle** priced cells are vector-ALU / EUP result stages.

> **QUIRK — two ordinals share one cell.** `Instr 0x15` and `Instr 0x20` both read `offsetLUT = 0x39c` (`VectorAluAny`, value 1). The flat-cell model does not require distinct offsets per ordinal; reservation columns and throughput cells are decoupled. All seven 8-cycle cells likewise alias only a handful of distinct offsets (`0x910`, `0x92c`, `0x94c`, `0x954`, `0x958`, `0x960`), and `0x92c` is shared by `Instr 0x05` and `0x0b`.

The `Instr 0x00..0x10` band (the MXU ordinals) is produced by the MXU classifier `CycleTableInstruction` `@0x1c89ca80`, which maps matmul opcodes `0x8d..0x96` through a `GainLatchMode → Instruction` LUT (`@0xb4389f4`, valid mask `0xf000003fffc3f`) and matprep/matpush opcodes `0x9b..0xa5` through a `MatmulDataFormat → Instruction` LUT (`@0xb438ac4`); all other opcodes `LogFatal` in this classifier. The non-MXU band `0x11..0x20` is emitted by the HLO-level cost model as direct ordinal immediates (a non-MXU `CycleTable` path; its producing opcode set is not decoded — MEDIUM). Both classifier LUTs are transcribed byte-for-byte on [JfCycleTable](jf-cycletable.md).

> **NOTE — the throughput cell is NOT the latency.** The 8-cycle MXU throughput cell is how many cycles the matmul port is held per op; the matmul *pipeline depth* (88 on JF, 66 on DF) is a separate cell (`+0x28`) copied into `LatencyTableJellyfish`. A back-to-back matmul costs 8 throughput cycles each (the MXU is pipelined, plain-MAX in the bundle reduction), but a matmul→consumer dependency edge waits the full 88/66 base latency. The two never alias in this grid.

---

## The `.rodata` Constant Blocks

`PerformanceJf` `@0x1d4930c0` overwrites the sentinel-filled buffer by copying pre-baked 16-byte `.rodata` blocks (`vmovaps → vmovups`), interspersed `vbroadcastss` fills (scalars `1 @0x84a2b08`, `2 @0x84a2854`, `8 @0x84a2d0c`), and immediate stores (`movabs 0x100000001` / `0x800000001` / `0x800000008`; `mov DWORD 0x1` / `0x8`). The store-count integrity holds exactly: 17 block copies (×4 = 68 dwords) + 81 broadcast fills (×4 = 324) + 9 `movabs` qwords (×2 = 18) + 9 dword immediates (×1 = 9) = **419** distinct `int32` slots, zero overlap. The other 471 stay at `0x7fffffff`.

The 15 distinct 16-byte blocks, all read byte-exact out of `.rodata`:

| `.rodata` block | bytes (4 × `int32`) | lands at | role | Confidence |
|---|---|---|---|---|
| `@0xa2c8a30` | `{4, 105, 7, 92}` | `Performance[+0x18]` | RPU producer / matres-self conflict floors (latency-table head) | CERTAIN |
| `@0xa2dcd30` | `{88, 8, 4, 1}` | `Performance[+0x28]` | matmul base (`+0x28`), matprep base (`+0x2c`), **EUP push→pop edge** (`+0x30`=4) | CERTAIN |
| `@0xa2db650` | `{1, 1, 2, 2}` | `Performance[+0x3c]`, `[+0x114]` | vector-result floors | CERTAIN |
| `@0xa2c8a40` | `{2, 1, 1, 1}` | `Performance[+0x4c]`, `[+0x178]` | vector-result floors | CERTAIN |
| `@0xa2d2df0` | `{1, 1, 1, 2}` | `Performance[+0xbc]` | vector-result floors | CERTAIN |
| `@0xa2cea00` | `{2, 2, 1, 1}` | `Performance[+0xcc]` | vector-result floors | CERTAIN |
| `@0xa2c5b90` | `{1, 1, 1, 4}` | `Performance[+0x168]` | vector-result floors | CERTAIN |
| `@0xa2d7660` | `{1, 1, 8, 1}` | `Performance[+0x410]` | branch-op cell (`[+0x418]`=8) | CERTAIN |
| `@0xa2d3c30` | `{8, 1, 1, 1}` | `Performance[+0x420]` | branch-op cell (`[+0x420]`=8) | CERTAIN |
| `@0xa2da220` | `{8, 8, 8, 1}` | `Performance[+0x910]` | MXU throughput band head (`Instr 0x00` at `+0x910`) | CERTAIN |
| `@0xa2cf810` | `{8, 1, 1, 8}` | `Performance[+0x940]` | MXU throughput cell (`Instr 0x1c` at `+0x94c`) | CERTAIN |
| `@0xa2c5ba0` | `{1, 1, 5, 5}` | `Performance[+0xa0c]` | deep conflict floors | CERTAIN |
| `@0xa2c2f40` | `{5, 1, 1, 1}` | `Performance[+0xa1c]` | deep conflict floors | CERTAIN |
| `@0xa2d2090` | `{1, 1, 4, 4}` | `Performance[+0xb08]` | deep conflict floors | CERTAIN |
| `@0xa2daf10` | `{4, 2, 1, 1}` | `Performance[+0xb18]` | deep conflict floors | CERTAIN |

The xpose-result cells `[+0x71c]=8` / `[+0x720]=8` come from a `movabs 0x800000001` qword (`[+0x718]=1`, `[+0x71c]=8`) plus an immediate (`[+0x720]=8`); the MXU `0x920..0x98c` and `0x990..0x998` runs are `8`-broadcasts and `movabs 0x800000008`. The head block `@0xa2dcd30 = {88,8,4,1}` is the one block the DF override touches — its first two elements are the matmul/matprep base latencies, its third is the EUP push→pop edge (=4), its fourth is an unused `1`.

> **NOTE — the EUP edge and the latency-table cells live in the same head blocks.** `Performance[+0x18..+0x24] = {4,105,7,92}` and `[+0x28..+0x34] = {88,8,4,1}` are the two blocks `LatencyTableJellyfish` reads from (15 cells total). The EUP push→pop edge `[+0x30]=4` is `{88,8,4,1}[2]`, copied into `LatencyTable[+0x1c]`. This page documents only the *source* cells; the copy map, the edge predicate, and the matmul/matprep base latencies are on [MXU Latency: JF / DF](mxu-latency-jf-df.md).

---

## Family Position

The flat one-cell-per-`Instruction` model is unique to v2/v3. From Pufferfish onward, `Performance` becomes a heap `latency[]` array plus a 2-D `GetResourceUsage(Instruction, Resource)` grid, the resource-column count widens, and `PfCycleTable::GetCyclesForThroughput` `@0x1c89de60` wraps `GetResourceUsage` calls rather than a flat offset-LUT read.

| Gen | Codename | TpuVer | Performance model | Resource cols | grid cells | JF→ next delta | Confidence |
|---|---|---|---|---|---|---|---|
| **JF** | Jellyfish | 0 (v2) | flat inline POD `0xe00` + offset LUT | **7** | 16 priced (1 cell each) | — | CERTAIN |
| **DF** | Dragonfish | 1 (v3) | = JF + 2 cells | **7** | 16 priced (= JF) | 2 cells (`+0x28`/`+0x2c`) | CERTAIN |
| PF | Pufferfish | 2 (v4) | heap `latency[336]` + grid 336×20 | 20 | 265 | architecture change | HIGH |
| VF | Viperfish | 3 (v5p) | heap `latency[384]` + grid 384×28 | 28 | 378 | — | HIGH |
| GL | Ghostlite | 4 (v6e) | heap `latency[476]` + grid 476×31 | 31 | 358 | — | HIGH |
| GF | Trillium | 5 (v7) | heap `latency[465]` + grid 465×31 | 31 | 285 | — | HIGH |

The resource-column progression is **7 → 7 → 20 → 28 → 31 → 31** (JF→DF→PF→VF→GL→GF). The architecture changed at Pufferfish: the inline-POD-plus-offset-LUT model (no 2-D grid, no `GetResourceUsage`) gave way to the heap latency-array-plus-grid model the rest of the line uses. The per-generation grids — populated cells, latency arrays, column-by-column naming — get their own pages; see [Performance Family Overview](performance-overview.md) for the framing and the per-gen page index.

> **QUIRK — the v3 cost model is the smallest delta in the family.** DragonfishTarget inherits almost everything from JellyfishTarget, and `PerformanceDf` mirrors that: it inherits the full `PerformanceJf` image and overrides only the matmul/matprep base latencies. The 2-MXU, higher-clock v3 silicon is encoded as a *lower* matmul base latency, not a wider grid or a different throughput cell. No other generation pair shares a buffer this completely.

---

## Open Items

- The literal enum strings for the 7 `CycleTable::Resource` columns and the 33 `CycleTable::Instruction` ordinals. The columns are named via the binding-confirmed `ResourceVector R[0..6]` (CERTAIN), but neither cost enum has a `ToString` in the binary; the deeper micro-port semantics remain functional (MEDIUM).
- The producing classifier for `Instr 0x11..0x20` (priced from the LUT but emitted by a non-MXU `CycleTable` path, not by the MXU-only `CycleTableInstruction`). Their `Resource`/offset/value are byte-pinned; the originating LLO opcode set is not decoded (MEDIUM).
- The cost-model role of the ~388 populated `Performance` slots not referenced by the `offsetLUT` or the `LatencyTableJellyfish` copy map (the `{1,1,2,2}` / `{5,1,1,1}` / `{1,1,4,4}` blocks scattered through `[+0x5c..+0xd0c]`). Byte-exact in the reconstructed image, but their per-cell consumer is unbound (LOW).
- The JF/DF BarnaCore (variant-1) cost path (the pre-SparseCore embedding engine). This page is the TensorCore `Performance`; BarnaCore has its own model, not swept here (out of scope).

---

## Cross-References

- [Performance Family Overview](performance-overview.md) — the two `Performance` architectures (flat JF/DF vs heap PF/VF/GL/GF), the family layout, and the per-gen page index
- [MXU Latency: JF / DF](mxu-latency-jf-df.md) — the latency axis that consumes these cells: the `LatencyTableJellyfish` 15-field copy map, the EUP push→pop edge, and the matmul/matprep base latencies (the JF→DF 88→66 / 8→13 delta)
- [JfCycleTable](jf-cycletable.md) — the full `offsetLUT`/`resLUT` byte transcription and the MXU-modifier `GainLatchMode`/`MatmulDataFormat → Instruction` classifier LUTs
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen cycle values that fill the later-gen grid slots
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` whose first 7 slots are the JF/DF `Resource` columns, and the `MaxResourceCycles` reduction
- [Performance: PF](performance-pf.md), [VF](performance-vf.md), [GL (GhPerf 476×31)](performance-gl-ghperf.md), [GF (GhPerf 465×31)](performance-gf-ghperf.md) — the later-gen heap grids JF/DF predate
- [MXU Slot](../isa/slot-mxu.md) — the physical MXU sub-units the `Matpush`/`Matmul`/`Xlu` columns reserve, and the opcodes that feed `CycleTableInstruction`
