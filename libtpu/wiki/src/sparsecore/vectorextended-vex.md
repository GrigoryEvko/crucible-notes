# VectorExtended (VEX)

> *Every opcode value, mask immediate, field shift/width, and per-generation count on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from each per-op `SparseCoreTecVectorExtended<Op>Opcode::Matches()` compare immediate, the `…<Op><Field>Field::GetConcatenatedValue()` accessor shifts, and the `ConsumeOneTecVexBundleInstruction` dispatch into `FindAndEmitToUnusedPort` / `EmitVectorSort`. Addresses apply to this build; other versions differ.*

## Abstract

`VectorExtended` (VEX) is the scan/sort/reduce slot of the 64-byte [TEC](tec-engine.md) bundle — the SparseCore's cross-lane datapath. Where [VectorAlu](vector-opcode-enum.md) is per-lane SIMD and [VectorLoad](vectorload-slot.md)/[VectorStore](vectorstore-slot.md) move rows between `TILE_SPMEM` and the VREG file, VEX is the *reduction* engine: it computes an inclusive prefix scan, an arg-extremum scan, a key+payload sort, or a duplicate-count/uniquify across the vector lanes in one bundle. It is the stage-3 reduce of the embedding-reduce pipeline (gather → load → **reduce** → drain → scatter-add), and the one op family whose result feeds the store mux *directly* through the shared `VstSource` field.

The decisive structural fact is that VEX is a **uniform operand frame with a single varying opcode**. All ops share one bit layout — three V operand pairs (`V0`/`V1`/`V2`, each a `Y_VREG` + `X` selector), a `SourceOne` seed-port selector, a `Vmask` lane predicate, and a `VstSource` fused-store port — and the only field that changes per op is the 6-bit opcode at `word0x28` bits 16..21. The op *is* the function; the operands are positionally fixed. Two ops break the frame: the four `Sort` variants add a `SourceTwo` second source (key+payload), and `VectorMoveConstrained` (the gfc-only op 52) replaces `Vmask` with a three-destination fan-out (`VexDest` + `VresDestOne` + `VresDestTwo`).

This page owns three things: the **VEX slot field map** (the op-invariant operand frame, byte-exact), the **embedding-reduce op roster** (the contiguous 0..52 gfc opcode space, the dtype/segmented/sort/dedup families, and the per-gen vfc-28 / glc-52 / gfc-53 delta), and the **load/store fusion** (`VstSource` driving the [VectorStore](vectorstore-slot.md) source mux; `VexSource` routing a V read port into the scan-input mux via the [VexSourcePortEncoding](vectorload-slot.md#the-sourceone-seed-enum)). The per-port read-allocator body (`FindAndEmitToUnusedPort`) is owned by [VEX Operand-Port Binding](vex-operand-port.md); the scan-input mask consumption and `ScanOp` lowering by [Scan Datapath](scan-datapath.md). They are linked, not repeated.

For reimplementation, the contract is:

- **The opcode is a flat 6-bit function selector @ `word0x28` bits 16..21 (gfc); 53 ops, contiguous 0..52.** `opcode = Matches_cmp_immediate >> 16`. The op ordering is structured by family — 32-bit scans (0..9), 32-bit segmented scans (10..19), sort/dup/uniquify (20..27), 16-bit/bf16 scans (28..39), 16-bit/bf16 segmented scans (40..51), `VectorMoveConstrained` (52). There is no separate dtype or mode operand; both are baked into the opcode value.
- **The operand frame is op-INVARIANT.** Every scan/segmented op carries the identical `{SourceOne, Vmask, VstSource, V0/V1/V2}` layout — byte-confirmed identical for `AddScanS32` and `MaxScanU32`. A reimplementer decodes one frame and reuses it for all 50 scan ops; only `Sort` (+`SourceTwo`) and `VectorMoveConstrained` (multi-dest) differ.
- **The scan result fuses the store path through `VstSource`.** `VstSource` (`word0x30 >> 27 & 0x3f`) sits at the *exact* bit position the [VectorStore](vectorstore-slot.md) slot reads its `Source` from — so a reduce result drives the store-source mux directly, without an intervening `VectorStore`-slot instruction.
- **The scan *input* fuses the load path through a V read port.** `SourceOne` (`word0x28 >> 13 & 7`) is a `VexSourcePortEncoding` selector routing the scan's carry-in to a `V0..V3`/`VST` read port the [VectorLoad](vectorload-slot.md) feeds; the reduction identity (0, ±inf) is a value placed in that port, not a hardwired code.
- **The Segmented family resets the accumulator at a per-sample boundary.** The 24 `Segmented*` ops carry the same frame as their plain twin; the segment-id boundary is bound as the second SSA operand of `SegmentedScanOp` (operand 1) and register-allocated to a free V read port.

| | |
|---|---|
| **Slot** | `VectorExtended` (VEX) — `TecVectorExtended` slot of the 64-byte [TEC bundle](tec-engine.md#the-tec-bundle-64-bytes) |
| **Opcode field** | 6-bit @ `word0x28` bits 16..21 (gfc); 6-bit @ bit 15 (glc) — see [§The Per-Gen Delta](#the-per-gen-delta) |
| **Opcode → mnemonic source** | per-op `SparseCoreTecVectorExtended<Op>Opcode::Matches()` immediate (`>> 16`) |
| **Op count (per gen)** | vfc **28** · glc **52** · gfc **53** (`Matches`-symbol roster); glc **36** dispatched in the VEX jump table |
| **Op families** | AddScan · Min/MaxScan · Min/MaxIndexScan · Segmented* · Sort · DuplicateCount · Uniquify · VectorMoveConstrained |
| **Operand frame** | op-INVARIANT: `{SourceOne@13/3, Vmask@5/5}` (word0x28) · `{VstSource@27/6, V2*}` (word0x30) · `V0/V1` (word0x38/0x40) |
| **Fused store** | `VstSource` @ `word0x30 >> 27 & 0x3f` == [VectorStore](vectorstore-slot.md) `Source` position |
| **Scan-emit template** | `FindAndEmitToUnusedPort<…VregReadPort, …VectorExtended_<Op>>` (scans) · `EmitVectorSort<…>` (sort) |
| **Orchestrator** | `ConsumeOneTecVexBundleInstruction` (glc `0x13a15ba0`, vfc `0x139a9de0`) |
| **Confidence** | CONFIRMED (decompile / `Matches`-immediate & accessor-shift anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the VEX opcode roster, the op-invariant operand frame, and the load/store fusion seam (`VstSource` / `SourceOne`-`VexSource`).** The 64-byte bundle layout lives in [TEC Engine](tec-engine.md); the read-port allocator body in [VEX Operand-Port Binding](vex-operand-port.md); the `ScanOp`/`SegmentedScanOp` MLIR lowering in [Scan Datapath](scan-datapath.md) and [Segmented Scan](segmented-scan.md); the `SourceOne` seed enum (8 `VexSourcePortEncoding` values) in [VectorLoad](vectorload-slot.md#the-sourceone-seed-enum). They are linked, not repeated.

---

## The Slot Field Map

### Purpose

The `VectorExtended` slot drives the SparseCore's EUP (Extended Unit Pipeline) cross-lane reduction. It reads up to three V operand pairs out of the VREG read ports, runs the opcode's reduction across the lanes, gates the result with a lane `Vmask`, and routes the result two ways: to the XRF (Extended Result FIFO, drained by the [VectorResult](vector-opcode-enum.md) slot) and — through `VstSource` — to the [VectorStore](vectorstore-slot.md) source mux. The seed/carry-in input is selected by `SourceOne`. The whole frame is fixed; only the 6-bit opcode varies.

### Field Layout

Confirmed byte-exact against the gfc `AddScanS32` field accessors (`SparseCoreTecVectorExtendedAddScanS32<Field>Field::GetConcatenatedValue`), and verified byte-identical for `MaxScanU32` — the frame is op-invariant. The fields straddle four 8-byte struct words (`0x20`, `0x28`, `0x30`, `0x38`/`0x40`). In the decompiled accessors `*((_DWORD*)this + 10)` is the 4-byte low half of word `0x28` (10 × 4 = 0x28) and `*((_QWORD*)this + 6)` is word `0x30`:

```text
VectorExtended slot — op-invariant scan frame (gfc; AddScanS32 reference)
 word0x28 bit:  5         10        13      16        22
               ┌─────────┬─────────┬───────┬─────────┬──────────────┐
               │ Vmask   │ SrcTwo* │SrcOne │ Opcode  │ Predication  │
               │ 5b @5   │ 3b @10  │ 3b @13│ 6b @16  │ (preg hdr)   │
               └─────────┴─────────┴───────┴─────────┴──────────────┘
                          Sort only  seed    the op

 word0x30 bit:  27                       50
               ┌─────────────────────────┬──────────┐
               │ VstSource 6b @27         │ V2YVreg  │  (V2X straddles word0x30 hi / word0x38)
               │ (== VectorStore Source)  │ 6b @50   │
               └─────────────────────────┴──────────┘

 word0x38/0x40: V0YVreg (shld 4: word0x38 hi / word0x40)  · V0X @word0x40 bit8
                V1YVreg @word0x38 bit23  · V1X @word0x38 bit35

 word0x20 bit:  47          53           (VectorMoveConstrained only)
               ┌───────────┬───────────┐
               │ VresDestTwo│VresDestOne│  6b each — multi-destination fan-out
               └───────────┴───────────┘
```

| Field | Word | Shift | Width | Present in | Accessor (gfc) | Confidence |
|---|---:|---:|---:|---|---|---|
| `Opcode` | `0x28` | 16 | 6 | all | (Matches predicate, `byte+0x2a & 0x3f`) | CONFIRMED |
| `SourceOne` | `0x28` | 13 | 3 | all (scan seed-port selector) | `…AddScanS32SourceOneField` `0x1eca7c40` | CONFIRMED |
| `Vmask` | `0x28` | 5 | 5 | all *except* `VectorMoveConstrained` | `…AddScanS32VmaskField` `0x1eca7d40` | CONFIRMED |
| `VstSource` | `0x30` | 27 | 6 | all (fused store source) | `…AddScanS32VstSourceField` `0x1eca7c60` | CONFIRMED |
| `V0YVreg` | `0x38`/`0x40` | (shld 4) | 6 | all (operand-0 VREG) | `…AddScanS32V0YVregField` `0x1eca7c80` | CONFIRMED |
| `V1YVreg` | `0x38` | 23 | 6 | all (operand-1 VREG) | `…AddScanS32V1YVregField` `0x1eca7cc0` | CONFIRMED |
| `V2YVreg` | `0x30` | 50 | 6 | all (operand-2 VREG) | `…AddScanS32V2YVregField` `0x1eca7d00` | CONFIRMED |
| `SourceTwo` | `0x28` | 10 | 3 | `Sort*` only (key+payload 2nd src) | `…SortIntegerAscendingSourceTwoField` `0x1ecaade0` | CONFIRMED |
| `VexDest` | `0x28` | 10 | **1** | `VectorMoveConstrained` only | `…VectorMoveConstrainedVexDestField` `0x1ecab840` | CONFIRMED |
| `VresDestOne` | `0x20` | 53 | 6 | `VectorMoveConstrained` only | `…VectorMoveConstrainedVresDestOneField` `0x1ecab860` | CONFIRMED |
| `VresDestTwo` | `0x20` | 47 | 6 | `VectorMoveConstrained` only | `…VectorMoveConstrainedVresDestTwoField` `0x1ecab880` | CONFIRMED |

The accessor bodies decode exactly:

```c
// SparseCoreTecVectorExtendedAddScanS32SourceOneField::GetConcatenatedValue  (gfc 0x1eca7c40)
return (uint8_t)HIBYTE(*((uint16_t *)this + 20)) >> 5;
// *((uint16_t*)this+20) is the 16-bit @ byte 0x28; HIBYTE = byte 0x29 (bits 8..15);
// >>5 takes the top 3 bits = bits 13..15 of word0x28 → 3-bit @ bit13 (the seed-port selector).

// …AddScanS32VstSourceField::GetConcatenatedValue                            (gfc 0x1eca7c60)
return (*((uint64_t *)this + 6) >> 27) & 0x3F;     // word0x30 bit27, 6-bit — SAME position as VectorStore Source

// …AddScanS32VmaskField::GetConcatenatedValue                                (gfc 0x1eca7d40)
return (*((uint32_t *)this + 10) >> 5) & 0x1F;     // word0x28 bit5, 5-bit — lane predicate mask

// …AddScanS32V2YVregField::GetConcatenatedValue                              (gfc 0x1eca7d00)
return (*((uint64_t *)this + 6) >> 50) & 0x3F;     // word0x30 bit50, 6-bit — operand-2 VREG

// …AddScanS32V0YVregField::GetConcatenatedValue                              (gfc 0x1eca7c80)
return (*(__int128 *)((char *)this + 56) >> 60) & 0x3F;  // straddles word0x38(byte56)/0x40 — shld 4
```

> **QUIRK — `VstSource` is a VEX field that names a `VectorStore` resource.** A reimplementer reading the VEX op template finds a 6-bit field whose meaning is "the store source the scan result feeds," not a scan operand. It is the structural seam between the two slots; treating it as an ordinary VEX operand mis-models the fused write-back. See [§The Load/Store Fusion](#the-loadstore-fusion).

> **CORRECTION (VEX-1) —** the `VexDest` field of `VectorMoveConstrained` was originally documented as a 3-bit field at `word0x28` bit 10. The decompiled accessor (`0x1ecab840`) reads `(word0x28 >> 10) & 1` — it is a **1-bit** field. It shares bit 10 with the `Sort` ops' 3-bit `SourceTwo`, but `VectorMoveConstrained` carries a *different* op semantics in that position (a single-bit EUP/XRF destination select), and the two never co-occur (a bundle is one or the other op). The 6-bit `VresDestOne`/`VresDestTwo` at `word0x20` bits 53/47 are confirmed unchanged.

> **NOTE — `SourceTwo` and `VexDest` share `word0x28` bit 10, but they belong to different ops and never collide.** Only `Sort*` carries `SourceTwo` (3-bit); only `VectorMoveConstrained` carries `VexDest` (1-bit). The decoder keys field extraction off the opcode, so the same bit region is interpreted per op — the same opcode-keyed extraction rule the [VectorStore](vectorstore-slot.md#the-slot-field-map) slot uses for its mode fields.

---

## The Embedding-Reduce Op Roster

### The opcode-recovery model

Each VEX op-form is a distinct C++ type `SparseCoreTecVectorExtended<Op>Opcode` carrying a `Matches() const` predicate that masks the opcode field out of the decoded-instruction word and compares it to the op's signature. The field is **6-bit @ bit 16 of word `0x28`** (gfc; mask `0x3f0000`), so `opcode = cmp_immediate >> 16`. The base op (`AddScanS32`, 0) is tested with `testb $0x3f, 0x2a(%rdi)` (the byte at `+0x2a` is bits 16..23 of word `0x28`; masking `0x3f` isolates the 6 opcode bits, all-zero ⇒ op 0). Byte-exact:

```c
// SparseCoreTecVectorExtendedAddScanS32Opcode::Matches            (gfc 0x1eca7520)
return (*((uint8_t *)this + 42) & 0x3F) == 0;                  // byte +0x2a, opcode 0
// …MaxScanU32Opcode::Matches                                      (gfc 0x1eca7560)
return (*((uint32_t *)this + 10) & 0x3F0000) == 0x20000;       // 0x20000 >> 16 = 2
// …SortFloatDescendingOpcode::Matches                             (gfc 0x1eca7b00)
return (*((uint32_t *)this + 10) & 0x3F0000) == 0x170000;      // 0x170000 >> 16 = 23
// …VectorMoveConstrainedOpcode::Matches                           (gfc 0x1eca7ba0)
return (*((uint32_t *)this + 10) & 0x3F0000) == 0x340000;      // 0x340000 >> 16 = 52
```

`*((uint32_t*)this + 10)` is the low half of word `0x28`. All 53 gfc op `Matches()` immediates were enumerated and decoded; the resulting opcode set is **contiguous 0..52, no gaps, no duplicates**.

### The full roster (gfc, 53 ops, byte-confirmed)

The opcode value orders the families: 32-bit scans first, then 32-bit segmented, then the sort/dedup block, then the 16-bit/bf16 scans and their segmented forms, then the constrained move. Every row is the byte-exact `Matches` immediate `>> 16`:

| op | mnemonic (`…VectorExtended…`) | family | dtype | reduction |
|---:|---|---|---|---|
| 0 | `AddScanS32` | AddScan | S32 | inclusive prefix sum |
| 1 | `MinScanU32` | MinScan | U32 | prefix min |
| 2 | `MaxScanU32` | MaxScan | U32 | prefix max |
| 3 | `MinIndexScanU32` | MinIndexScan | U32 | prefix arg-min (value + lane idx) |
| 4 | `MaxIndexScanU32` | MaxIndexScan | U32 | prefix arg-max |
| 5 | `AddScanF32` | AddScan | F32 | prefix sum |
| 6 | `MinScanF32` | MinScan | F32 | prefix min |
| 7 | `MaxScanF32` | MaxScan | F32 | prefix max |
| 8 | `MinIndexScanF32` | MinIndexScan | F32 | prefix arg-min |
| 9 | `MaxIndexScanF32` | MaxIndexScan | F32 | prefix arg-max |
| 10..19 | `Segmented{Add,Min,Max,MinIndex,MaxIndex}Scan{U32,F32}` | Segmented | U32/F32 | per-segment-reset scan |
| 20 | `SortIntegerAscending` | Sort | int | key+payload sort ↑ |
| 21 | `SortIntegerDescending` | Sort | int | key+payload sort ↓ |
| 22 | `SortFloatAscending` | Sort | float | key+payload sort ↑ |
| 23 | `SortFloatDescending` | Sort | float | key+payload sort ↓ |
| 24 | `DuplicateCountInteger` | DuplicateCount | int | per-value multiplicity |
| 25 | `DuplicateCountFloat` | DuplicateCount | float | per-value multiplicity |
| 26 | `UniquifyInteger` | Uniquify | int | compact to unique reps |
| 27 | `UniquifyFloat` | Uniquify | float | compact to unique reps |
| 28 | `AddScanS16PartialSumS16` | AddScan | S16→S16 | prefix sum, narrow partial |
| 29 | `AddScanS16PartialSumS32` | AddScan | S16→S32 | prefix sum, widened partial |
| 30..33 | `{Min,Max}{,Index}ScanU16` | scan | U16 | prefix min/max/arg |
| 34 | `AddScanBf16PartialSumBf16` | AddScan | Bf16→Bf16 | prefix sum, narrow partial |
| 35 | `AddScanBf16PartialSumF32` | AddScan | Bf16→F32 | prefix sum, widened partial |
| 36..39 | `{Min,Max}{,Index}ScanBf16` | scan | Bf16 | prefix min/max/arg |
| 40..51 | `Segmented…{S16,Bf16,U16,…}` | Segmented | 16-bit/bf16 | per-segment-reset scan |
| 52 | `VectorMoveConstrained` | move | — | multi-dest EUP/VRES move (gfc-only) |

The contiguity (0..52, no gaps/dups) was verified across the full `Matches`-immediate enumeration. Decompile spot-checks confirm representative rows from each family: op 0 (`AddScanS32`, `testb 0x3f`), op 2 (`MaxScanU32`, `0x20000`), op 23 (`SortFloatDescending`, `0x170000`), op 35 (`AddScanBf16PartialSumF32`, `0x230000`), op 46 (`SegmentedAddScanBf16PartialSumBf16`, `0x2e0000`), op 51 (`SegmentedMaxIndexScanBf16`, `0x330000`), op 52 (`VectorMoveConstrained`, `0x340000`).

> **NOTE — the opcode *order* is the dtype-split history, not a logical grouping.** The 32-bit families come first (the original v5 set), the sort/dedup block sits in the middle (gen-stable), and the 16-bit/bf16 families plus `VectorMoveConstrained` are appended at the high opcodes — exactly the [VectorStore](vectorstore-slot.md#the-vfgf-generic-to-typed-evolution) dtype-split pattern, where new dtypes extend the opcode space upward rather than reordering it. A reimplementer must encode by the opcode *value*, never by lexical name order.

### The op families — cross-lane semantics

Each family is a distinct cross-lane reduction. The names + the operand frame + the [XRF drain](vector-opcode-enum.md) model determine the semantics; the per-cycle lane datapath (the systolic scan / bitonic-sort network) is silicon, not in the C++ (`HIGH` for the micro-architecture, `CONFIRMED` for the op→semantics map).

```text
VEX cross-lane reduction families
  AddScan{S32,F32}, AddScan{S16,Bf16}PartialSum{narrow,wide}
        out[i] = SUM(in[0..i]).  PartialSum widens a narrow input (S16/Bf16) into a wider
        partial (S16/S32 · Bf16/F32) — the precision-preserving embedding-row sum.
        SourceOne selects the scan seed / carry-in source bus.
  MinScan / MaxScan {U32,F32,U16,Bf16}     out[i] = MIN/MAX(in[0..i]).
  MinIndexScan / MaxIndexScan {U32,F32,U16,Bf16}
        prefix arg-extremum: carries the LANE INDEX of the running min/max (recover WHICH
        gathered embedding row was the extremum — top-k / argmax over a window).
  Segmented<scan>   same reduction, but the running accumulator RESETS at segment boundaries
        (the segment-id boundary is operand 1 of SegmentedScanOp; see below).
  SortInteger/Float Ascending/Descending   key+payload sort: SourceOne=key, SourceTwo=payload
        (sort the gathered ids, carry the associated value/row index).
  DuplicateCount{Integer,Float}   count repeated values (the multiplicity of each id).
  Uniquify{Integer,Float}         compact duplicate values to unique representatives.
  VectorMoveConstrained           multi-destination EUP/VRES move (VexDest + VresDest{One,Two}).
```

The Segmented family resets the running accumulator at per-sample boundaries — the per-segment prefix reduction that is the multi-lookup / multi-sample embedding sum, where one tile holds gathered rows for several samples and each output row aggregates a contiguous run. The Sort/Uniquify/DuplicateCount trio is the dedup-lookup pre-processing: sort the ids, uniquify so each unique row is fetched once, count duplicates so the [scatter-add](vectorstore-slot.md#the-fetch-and-add-return-path) knows the multiplicity (the [Dedup Multiplicity](dedup-multiplicity.md) path).

---

## The Load/Store Fusion

VEX is wired into the load and store slots at *both* ends of the reduction. The fusion is two distinct seams: the scan **output** drives the [VectorStore](vectorstore-slot.md) source mux (`VstSource`), and the scan **input** is routed from a V read port the [VectorLoad](vectorload-slot.md) fills (`SourceOne` → `VexSource`).

### Output seam — `VstSource` into the store mux

The `VstSource` field (`word0x30 >> 27 & 0x3f`) sits at the **exact bit position** the [VectorStore](vectorstore-slot.md) slot reads its `Source` from — byte-confirmed identical: the gfc VEX accessor `0x1eca7c60` reads `(word0x30 >> 27) & 0x3f`, and the gfc `TileSpmemStore` `Source` accessor (`0x1ecca3e0`) reads the same `word0x30 >> 27 & 0x3f`.

```text
fused reduce → store (one TEC bundle, no VectorStore round-trip)
   VectorExtended slot          VectorStore slot
   (scan / segmented-scan)      (TileSpmemStore…Add{dt})
        │  result                     │
        └── VstSource @word0x30>>27 ───┘ Source @word0x30>>27   ◄── SAME bit position
            (the scan/reduce output drives the store-source mux directly)
```

Because the reduce output and the store source occupy the same field, a VEX reduction can drive the store-source mux directly: the reduced embedding row streams to `TILE_SPMEM` without an intervening `VectorStore`-slot instruction. The result also drains via the XRF → [VectorResult](vector-opcode-enum.md) path; the two are not mutually exclusive. This is why the embedding-reduce inner loop fits in fewer bundles than a naive load-scan-store sequence — the scan *is* the store source.

### Input seam — `SourceOne` selects a V read port (`VexSource`)

`SourceOne` (`word0x28 >> 13 & 7`) is **not a constant-pool index** — it is a 3-bit `VexSourcePortEncoding` *selector* that routes the scan's first/seed input to one of the V0..V3 VREG read ports or the `VST` source bus. The 8 encodings (`VST_SOURCE`, `V0_Y_VREG`, `V0_X`, `V1_Y_VREG`, `V1_X`, `V2_Y_VREG`, `V2_X`, `V3_Y_VREG`) are owned and tabulated in [VectorLoad §The SourceOne Seed Enum](vectorload-slot.md#the-sourceone-seed-enum); they are cross-confirmed by `GhostliteProtoUtils::GetVexSourcePortEncoding(VregReadPort)` (gfc `0x1c5ee280`), a 1:1 switch.

The reduction identity element (0 for `Add`, ±inf for `Min`/`Max`) is whatever value is *placed in the selected port* — not a hardwired `SourceOne` code. This is the mechanism that chains a scan's accumulator across tiles: write the previous partial to a chosen V port, set `SourceOne` to that port, and the scan resumes with that carry-in. The per-op read-port allocation (which physical port each operand lands on) is performed by `FindAndEmitToUnusedPort<…VregReadPort, …VectorExtended_<Op>>` and documented in [VEX Operand-Port Binding](vex-operand-port.md).

### Segmented boundary — operand 1, register-allocated

For embedding *sum* the scan is segmented; the per-sample reset boundary is bound as the **second SSA operand** of `mlir::sparse_core::SegmentedScanOp` (operand 0 = data, operand 1 = segment_ids), versus plain `ScanOp`'s single operand. The SC isa_emitter register-allocates each intrinsic operand to a free V read port (`FindAndEmitToUnusedPort`), so the segment-id lands on whichever `V0/V1/V2` port is free — a register-allocated operand, not a fixed slot. The full lowering (the `reduction_op` ∈ {`sum`, `max`, `min`} byte-comparison, the `…_seg_scan2xN` vs `…_scan1xN` intrinsic-name operand count) is owned by [Segmented Scan](segmented-scan.md) and summarized on the [VectorLoad](vectorload-slot.md#the-segmented-scan-boundary-operand) page.

---

## The Scan-Emit Dispatch

`VectorExtended` has **no standalone `Consume<Slot>Instruction`** — unlike `VectorAlu`/`VectorStore` (each its own consumer with its own jump table), VEX is consumed *inline* by the TEC VEX orchestrator. `ConsumeOneTecVexBundleInstruction` (glc `0x13a15ba0`, vfc `0x139a9de0`) reads the MCInst opcode, indexes the main VEX jump table, and dispatches each arm to either the scan-emit allocator or the sort emitter:

```text
ConsumeOneTecVexBundleInstruction  (glc 0x13a15ba0)
  read opcode → main VEX jump table (base 0x106b)
  per non-default arm:
    GetVectorExtendedSlot + EmitPredicationToSlot<…VectorExtended>
    cmp [slot+0x50], <oneof-tag>          ── the arm's SparseCoreTecVectorExtended oneof tag
    DefaultConstruct<…VectorExtended_<Op>>
    GetVectorMask + GetVregno
    └─ scan ops  → FindAndEmitToUnusedPort<…SparsecoreVregReadPort, …VectorExtended_<Op>>
       sort ops  → EmitVectorSort<…SparsecoreVectorMask, …VregReadPort, …VectorExtended_Sort{…}>
    VresMove (opcode 0x10de) → EmitVectorResultMove   ── a VectorResult op, not VEX
  default arm → MakeError "Unsupported Vector Extended or Vector Result opcode: $0 : $1"
```

`EmitVectorSort` is templated over `{SparsecoreVectorMask, SparsecoreVregReadPort, SparseCoreTecVectorExtended_Sort{Integer,Float}{Ascending,Descending}}` — confirmed present in the decompile (e.g. vfc `0x139d2dc0`/`0x139d2b80`). `FindAndEmitToUnusedPort` has 111 per-op-typed `…VregReadPort`/`…VectorExtended_<Op>` instantiations across the engines. The orchestrator also hosts one [VectorResult](vector-opcode-enum.md) op (`VresMove`, opcode `0x10de`) inline; the rest of the `VectorResult` family (`EupResult`, `PopXrf*`) is dispatched from the *other* TEC orchestrator.

> **GOTCHA — the VEX `Matches` roster (52/53 ops) is larger than the set the glc jump table dispatches (36).** The proto/`Matches` op family is the full per-gen declared set (glc 52, gfc 53), but the glc VEX jump table reaches only **36** of them (32 scan/dedup arms via `FindAndEmitToUnusedPort` + 4 `Sort` arms via `EmitVectorSort`); the rest land on the `MakeError "Unsupported Vector Extended or Vector Result opcode"` default. A reimplementer building a *decoder* needs the full `Matches` roster (any of these opcodes can appear in a bundle and must decode); building an *emitter* needs only the reachable set its lowering produces. The two counts measure different things — do not conflate them. The reachable-set census and the jump-table layout are owned by the code-gen pages; this page owns the `Matches` roster and the field decode.

---

## The Per-Gen Delta

The slot exists on all three wired generations with the *same* operand frame and orchestrator structure, but the op roster and opcode-field position differ. The delta is the same dtype-merged → dtype-split ISA evolution the [VectorStore](vectorstore-slot.md#the-vfgf-generic-to-typed-evolution) and [VectorAlu](vector-opcode-enum.md) slots show.

| Quantity | Viperfish (vfc, v5) | Ghostlite (glc, v6e) | 6acc60406 (gfc, TPU7x) |
|---|---:|---:|---:|
| `VectorExtended` op count (`Matches` symbols) | **28** | **52** | **53** |
| Ops dispatched in the VEX jump table | 28 | **36** | (full emitter set) |
| Opcode field | 6-bit @ `word0x28` bit 16 | 6-bit @ bit 15 | 6-bit @ bit 16 |
| dtype naming | `Float`/`Integer` (merged) | explicit `S32`/`F32`/`S16`/`U32`/`U16`/`Bf16` | explicit, + `PartialSum` forms |
| `PartialSum` widening forms | — | yes | yes |
| `VectorMoveConstrained` (53rd op) | — | — | yes |
| Operand frame (`SourceOne`/`Vmask`/`VstSource`/`V0..V2`) | same | same | same |

Viperfish uses **coarse Float/Integer-merged names** — `FloatAddScan`, `IntegerMaxScan`, `SegmentedFloatMinScan`, `IntegerMinIndexScan` — folding every dtype into `Float` or `Integer` (28 ops). Ghostlite splits each into per-dtype variants (`AddScanF32` / `AddScanS32` / `MaxScanU32` / `MaxScanU16` / `MaxScanBf16` …) and adds the `PartialSum` widening forms (52 ops). 6acc60406 adds the 53rd op, `VectorMoveConstrained`. The gfc opcode field is 6-bit @ `word0x28` bit 16; the **glc field is at bit 15** (the base-op predicate masks `0x1F80` of the 16-bit at byte 41 — a 1-bit-lower position than gfc). The decode VALUES and the operand-frame semantics are otherwise gen-stable.

> **QUIRK — the glc opcode field is one bit lower than gfc.** glc `AddScanS32::Matches` masks `(*(uint16_t*)(this+41)) & 0x1F80` (bits 15..20 of word `0x28`); gfc masks `byte+0x2a & 0x3f` (bits 16..21). A glc-targeted decoder must read `(word0x28 >> 15) & 0x3f`, not the gfc `>> 16` — a 1-bit shift the field width hides if you only check the op count. The 1-bit shift was confirmed from the base-op predicate immediates; what occupies gfc bit 15 / glc bit 21 was not separately traced (`LOW` for the adjacent-bit reassignment).

---

## Function Map

| Symbol (gfc) | Address | Role | Confidence |
|---|---|---|---|
| `…VectorExtendedAddScanS32Opcode::Matches` | `0x1eca7520` | op 0 predicate (`byte+0x2a & 0x3f == 0`) — the base op | CONFIRMED |
| `…VectorExtendedMaxScanU32Opcode::Matches` | `0x1eca7560` | op 2 (`& 0x3f0000 == 0x20000`) | CONFIRMED |
| `…VectorExtendedSortFloatDescendingOpcode::Matches` | `0x1eca7b00` | op 23 (`0x170000 >> 16`) | CONFIRMED |
| `…VectorExtendedVectorMoveConstrainedOpcode::Matches` | `0x1eca7ba0` | op 52 (`0x340000 >> 16`) — gfc-only | CONFIRMED |
| `…VectorExtendedAddScanS32SourceOneField` | `0x1eca7c40` | `SourceOne` @ `word0x28 >> 13 & 7` (seed-port selector) | CONFIRMED |
| `…VectorExtendedAddScanS32VstSourceField` | `0x1eca7c60` | `VstSource` @ `word0x30 >> 27 & 0x3f` (== VectorStore `Source`) | CONFIRMED |
| `…VectorExtendedAddScanS32VmaskField` | `0x1eca7d40` | `Vmask` @ `word0x28 >> 5 & 0x1f` | CONFIRMED |
| `…VectorExtendedAddScanS32V2YVregField` | `0x1eca7d00` | `V2YVreg` @ `word0x30 >> 50 & 0x3f` | CONFIRMED |
| `…VectorExtendedAddScanS32V0YVregField` | `0x1eca7c80` | `V0YVreg` straddling `word0x38`/`0x40` (shld 4) | CONFIRMED |
| `…VectorExtendedSortIntegerAscendingSourceTwoField` | `0x1ecaade0` | `SourceTwo` @ `word0x28 >> 10 & 7` (Sort key+payload) | CONFIRMED |
| `…VectorExtendedVectorMoveConstrainedVexDestField` | `0x1ecab840` | `VexDest` @ `word0x28 >> 10 & 1` (1-bit; see VEX-1) | CONFIRMED |
| `…VectorExtendedVectorMoveConstrainedVresDestOneField` | `0x1ecab860` | `VresDestOne` @ `word0x20 >> 53 & 0x3f` | CONFIRMED |
| `…VectorExtendedVectorMoveConstrainedVresDestTwoField` | `0x1ecab880` | `VresDestTwo` @ `word0x20 >> 47 & 0x3f` | CONFIRMED |
| `ConsumeOneTecVexBundleInstruction` (glc) | `0x13a15ba0` | inline VEX orchestrator; main jump table dispatch | CONFIRMED |
| `EmitVectorSort<…>` (vfc) | `0x139d2dc0` | the 4-arm sort emitter | CONFIRMED |

Cross-gen anchors: glc `AddScanS32::Matches` (`0x1eb2cd20`) masks `0x1F80` of the 16-bit at byte 41 → opcode field 6-bit @ `word0x28` bit 15 (vs gfc bit 16). The per-gen `Matches`-symbol counts — vfc 28, glc 52, gfc 53 — were re-confirmed by per-namespace enumeration; the vfc names are the coarse `Float`/`Integer`-merged set (`FloatAddScan`, `IntegerMaxScan`, `SegmentedFloatMinScan`).

> **NOTE — `TensorCoreVectorExtended*` would be a different engine; only `SparseCoreTecVectorExtended*` types contribute to this roster.** As on the [load](vectorload-slot.md) and [store](vectorstore-slot.md) pages, a reimplementer grepping for `VectorExtended` must filter to the `SparseCoreTec` prefix; the per-gen namespaces are `gxc::gfc`, `gxc::glc`, `vxc::vfc`.

---

## Considerations

- **Decode one frame, reuse it for all 50 scan ops.** The operand frame is op-invariant (`AddScanS32` ≡ `MaxScanU32` byte-for-byte). Only the 6-bit opcode varies; only `Sort*` adds `SourceTwo` and only `VectorMoveConstrained` swaps `Vmask` for the multi-dest fan-out. A per-op field table is wasted effort — key off the family.
- **`VstSource` is the store seam, not a scan operand.** Decode it as the fused store-source port (== `VectorStore` `Source`). Modeling it as an ordinary V operand mis-models the fused write-back.
- **`SourceOne` is a port selector, not a constant.** The reduction identity is a value placed in the selected V port; treating `SourceOne` as a constant-pool index ({0, ±inf}) mis-models the cross-tile accumulator chaining.
- **The glc opcode field is at bit 15, gfc at bit 16.** A glc decoder reads `(word0x28 >> 15) & 0x3f`; gfc reads `>> 16`. The op *values* are gen-stable; the field *position* shifted 1 bit.
- **`Matches` roster ≠ jump-table reachable set.** A decoder needs all 52/53 `Matches` ops; an emitter produces only the reachable 36 (glc). Do not size the decoder off the emitter's roster.
- **Unmapped (LOW/inferred).** The per-cycle systolic scan / bitonic-sort lane datapath (silicon, not in the C++); which `V0/V1/V2` port the read-port allocator assigns each operand ([VEX Operand-Port Binding](vex-operand-port.md)); the `count×gradient` multiplicity fold ([Dedup Multiplicity](dedup-multiplicity.md)); the adjacent-bit reassignment between gfc bit 15 and glc bit 21; the `V3_Y_VREG` physical-port identity (a 4th read port vs a VST alias, `HIGH`).

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreTecVectorExtended<Op>Opcode::Matches` | the per-op predicates defining the opcode values (0..52 @ `word0x28` bit 16) |
| `SparseCoreTecVectorExtendedAddScanS32<Field>Field` (`0x1eca7c40+` gfc) | the op-invariant operand-frame accessors |
| `SparseCoreTecVectorStore*::Source` (`0x1ecca3e0` gfc) | the store-source field `VstSource` fuses into (same `word0x30` bit 27) |
| `SparseCoreTecVectorLoad*` (`0x1ecb9a00+` gfc) | the load feeding the V read ports `SourceOne` selects |
| `GhostliteProtoUtils::GetVexSourcePortEncoding` (`0x1c5ee280`) | maps `VregReadPort` → the 8 `VexSourcePortEncoding` values |
| `ConsumeOneTecVexBundleInstruction` (`0x13a15ba0` glc) | the inline orchestrator; dispatches scans to `FindAndEmitToUnusedPort`, sorts to `EmitVectorSort` |
| `mlir::sparse_core::SegmentedScanOp` | the 2-operand segmented scan; operand 1 = the segment-id boundary |

## Cross-References

- [TEC (Vector) Engine](tec-engine.md) — owns the 64-byte bundle, the slot bases, and the encoder-dispatch model.
- [VectorStore Slot](vectorstore-slot.md) — the store this slot fuses into via the shared `VstSource`/`Source` field; the 33-op type×mode scatter matrix.
- [VectorLoad Slot](vectorload-slot.md) — the read side feeding the V ports; the home of the `SourceOne` seed enum and the segment-operand binding.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the `VectorAlu` roster, the opcode-recovery model this page reuses, and the `VectorResult` XRF-drain slot the scan result drains through.
- [VEX Operand-Port Binding](vex-operand-port.md) — the `FindAndEmitToUnusedPort` read-port allocator body the scan-emit template invokes.
- [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md) — the mask field and sub-opcode map below the VEX opcode dispatch.
- [Scan Datapath](scan-datapath.md) — the scan-input mask consumption and `ScanOp` lowering this slot's reductions feed.
- [Segmented Scan](segmented-scan.md) — the per-segment-reset scan and its `reduction_op` ∈ {sum, max, min} lowering.
- [Segmented-Add-Scan](segmented-add-scan.md) — the newer-gen segment-reduce family and its `VpackFormat`/partial-sum format attributes.
- [Dedup Multiplicity](dedup-multiplicity.md) — the Sort → Uniquify → DuplicateCount pre-processing the dedup-lookup path runs through these VEX ops.
- [SparseCore Overview](overview.md) — the three SC engine classes, per-gen presence, and where the TEC vector slots sit.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
