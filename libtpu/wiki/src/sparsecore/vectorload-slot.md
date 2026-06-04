# VectorLoad Slot

> *Every opcode value, mask immediate, field shift/width, and per-generation count on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the per-op `SparseCoreTecVectorLoadTileSpmemLoad<Form>Opcode::Matches()` compare immediates, the `…<Form><Field>Field::GetConcatenatedValue()` accessor shifts, and the `SparseCoreTecVectorExtended…SourceOneField` / `GetVexSourcePortEncoding` decode. Addresses apply to this build; other versions differ.*

## Abstract

`VectorLoad` is the read-side mirror of the [VectorStore](vectorstore-slot.md) slot in the 64-byte [TEC](tec-engine.md) bundle: the slot that pulls a gathered row out of per-tile SRAM (`TILE_SPMEM`) into a VREG so the [VectorExtended](vectorextended-vex.md) scan engine can reduce it. Where `VectorStore` is a *type×mode* opcode matrix (it must carry the accumulate dtype), `VectorLoad` is **addressing-mode-only**: 5 ops over a 3-bit opcode field, all dtype-agnostic. The element type is set by the consuming op, not by the load — a reimplementer who looks for an `S32`/`F32`/`S16`/`Bf16` split in the load roster (as exists on the store side) will not find one, and must not synthesize it.

The decisive structural fact is that the load packs **every operand field into ONE word** — the 8-byte word at struct offset `0x28` — because the load produces a `Dest` (a VREG written) rather than driving a store-source mux. The store puts its data fields in word `0x30` (`Source@27`); the load puts its `Dest@52` and `Index@27` and the address-build fields all in word `0x28`. The two slots are deliberately symmetric at the bit level: the load `Dest` (`word0x28 >> 52 & 0x3f`) is the *exact same* mux position the store's `IndexedReturnValueAdd` fetch-and-add returns its pre-add value through — which is *why* fetch-and-add returns the pre-add value (it is a load-then-add through the load-Dest mux).

This page also owns three pieces of the embedding-reduce datapath that bracket the load: the **`SourceOne` seed enum** (`VexSourcePortEncoding`, 8 values) that selects the scan's carry-in source bus; the **segmented-scan boundary operand** (the per-segment reset id, bound as the second SSA operand of `SegmentedScanOp`); and the **fetch-and-add scatter ordering/dedup** semantics on the load/return path. These are documented here because all three are read *through* the load-Dest mux and the V read ports the load feeds; the scan opcode roster itself lives in [VectorExtended](vectorextended-vex.md).

For reimplementation, the contract is:

- **The opcode is addressing-mode only; 5 ops, 3-bit field @ word `0x28` bit 58 (gfc/glc).** `opcode = Matches_cmp_immediate >> 58`, contiguous 0..4. There is no dtype in the load opcode — `VectorLoad` is dtype-agnostic, unlike the [VectorStore](vectorstore-slot.md) `Add` forms. The mode lattice is `plain · CircularBuffer · CircularBufferPostUpdate · Indexed · IndexedCircularBuffer`.
- **All operand fields live in word `0x28`, contiguous bit 27..60, no overlap, no gap.** `Index@27/6` (Indexed forms), `Mask@33/5`, `Stride@38/4`, `Offset@42/3`, `BaseAddress@45/3`, `Cbreg@48/4` (CB forms), `Dest@52/6`, `Opcode@58/3`. The store-side sibling proves the [bit-position symmetry](#load-store-bit-symmetry): load `Dest@word0x28 bit52` == store fetch-and-add `Dest@word0x28 bit52`.
- **`SourceOne` is a source-bus SELECTOR, not a constant.** A 3-bit field @ word `0x28` bit 13 selecting a `VexSourcePortEncoding` ∈ {`VST_SOURCE=0`, `V0_Y_VREG=1`, `V0_X=2`, `V1_Y_VREG=3`, `V1_X=4`, `V2_Y_VREG=5`, `V2_X=6`, `V3_Y_VREG=7`}. It routes the EUP scan-input/carry-in mux to a VREG read port; the reduction identity (0, ±inf) is a *value placed in that port*, not a hardwired code.
- **Segmented scans bind the segment id as the second SSA operand.** `sparse_core::SegmentedScanOp` = 2 operands (operand 0 = data, operand 1 = segment_ids); plain `ScanOp` = 1 operand. The segment id is register-allocated to a free V read port; the per-segment reset reads operand 1.
- **The fetch-and-add returns the pre-add value, and dedup makes the ordering moot.** The return is captured through the load-Dest mux; the [dedup pipeline](dedup-multiplicity.md) (Sort → Uniquify → DuplicateCount) collapses duplicate ids before the scatter so each unique row is touched once with its multiplicity folded in.

| | |
|---|---|
| **Slot** | `VectorLoad` — `TecVectorLoad` slot of the 64-byte [TEC bundle](tec-engine.md#the-tec-bundle-64-bytes) |
| **Opcode field** | 3-bit @ bit 58 of word `0x28` (gfc/glc); 3-bit @ bit 56 (vfc) |
| **Opcode → mnemonic source** | per-op `SparseCoreTecVectorLoadTileSpmemLoad<Form>Opcode::Matches()` immediate (`>> 58`) |
| **Op count (per gen)** | vfc **5** · glc **5** · gfc **5** |
| **Mode lattice** | plain · `CircularBuffer` · `CircularBufferPostUpdate` · `Indexed` · `IndexedCircularBuffer` |
| **Data word** | word `0x28` (ALL fields — `Dest`, `Index`, address-build, opcode) |
| **dtype** | none — addressing-mode only (set by the consuming op) |
| **SourceOne seed** | 3-bit @ word `0x28` bit 13 → `VexSourcePortEncoding` (8 values) |
| **Confidence** | CONFIRMED (decompile / `Matches`-immediate & accessor-shift anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the `VectorLoad` opcode roster, field decode, the `SourceOne` seed enum, the segment-operand binding, and the fetch-and-add return ordering. The 64-byte bundle layout lives in [TEC Engine](tec-engine.md); the VEX scan opcode roster lives in [VectorExtended](vectorextended-vex.md); the dedup pipeline lives in [Dedup Multiplicity](dedup-multiplicity.md).** They are linked, not repeated.

---

## The Slot Field Map

### Purpose

The `VectorLoad` slot reads one row out of `TILE_SPMEM` into a `Dest` VREG. The address is built from `{BaseAddress, Offset, Stride}` (or a [CBREG](cbreg.md) window via `Cbreg`), under a lane `Mask`, optionally gathered per-element by an `Index` VREG. Unlike the store side there is no `Source` (no store mux) and no accumulate dtype: the load only chooses *where to read* and *which VREG to write*. Every field — including the opcode and the destination — lives in the 8-byte word at struct offset `0x28`.

### Field Layout

Confirmed byte-exact against the gfc *plain* `TileSpmemLoad` field accessors (`SparseCoreTecVectorLoadTileSpmemLoad<Field>Field::GetConcatenatedValue`), each a single `(word0x28 >> shift) & mask`. In the decompiled accessors `*((_QWORD*)this + 5)` is the 8-byte word at byte offset `0x28` (5 × 8 = 0x28):

```text
VectorLoad slot — word 0x28 (8 bytes), gfc plain TileSpmemLoad form
 word0x28 bit: 13      27       33       38     42    45     48     52       58
              ┌───────┬────────┬────────┬──────┬─────┬──────┬──────┬────────┬───────┐
              │SrcOne │ Index  │ Mask   │Stride│Offs.│Base  │Cbreg │ Dest   │Opcode │
              │ 3b    │ 6b     │ 5b     │ 4b   │ 3b  │ 3b   │ 4b   │ 6b     │ 3b    │
              │ @13   │ @27    │ @33    │ @38  │ @42 │ @45  │ @48  │ @52    │ @58   │
              └───────┴────────┴────────┴──────┴─────┴──────┴──────┴────────┴───────┘
              (VEX scan  Indexed*   ◄──── address build ────►  CB*   the VREG  the
               seed sel) only                                  only  written   mode
```

> **NOTE — `SourceOne@13` is a `VectorExtended` (scan) field, not a `VectorLoad` field; it shares word `0x28` and is drawn here to show the seam.** The load proper carries `{Dest, Cbreg, BaseAddress, Offset, Stride, Mask, Index}` at bits 27..60. `SourceOne` at bit 13 belongs to the scan op that consumes the loaded VREG (see [§The SourceOne Seed Enum](#the-sourceone-seed-enum)); it is byte-confirmed in the same word from the `…AddScanS32SourceOneField` accessor.

| Field | Word | Shift | Width | Present in modes | Accessor (gfc) |
|---|---:|---:|---:|---|---|
| `Opcode` | `0x28` | 58 | 3 | all | (Matches predicate, `byte+0x2f & 0x1c`) |
| `Dest` | `0x28` | 52 | 6 | all (the loaded row's VREG) | `…TileSpmemLoadDestField` `0x1ecb9b20` |
| `Cbreg` | `0x28` | 48 | 4 | `CircularBuffer*` only (→ 16 [CBREGs](cbreg.md)) | `…CircularBufferCbregField` `0x1ecb9be0` |
| `BaseAddress` | `0x28` | 45 | 3 | all (`TILE_SPMEM` tile base / alt to CBREG) | `…TileSpmemLoadBaseAddressField` `0x1ecb9b40` |
| `Offset` | `0x28` | 42 | 3 | all (within-window offset) | `…TileSpmemLoadOffsetField` `0x1ecb9b60` |
| `Stride` | `0x28` | 38 | 4 | all (per-element address stride) | `…TileSpmemLoadStrideField` `0x1ecb9b80` |
| `Mask` | `0x28` | 33 | 5 | all (lane predicate / vmask) | `…TileSpmemLoadMaskField` `0x1ecb9ba0` |
| `Index` | `0x28` | 27 | 6 | `Indexed*` only (per-element gather VREG) | `…TileSpmemLoadIndexedIndexField` `0x1ecb9dc0` |

The shifts above were read from the *plain* `TileSpmemLoad` op-form — the canonical reference, because it carries the address-build fields with no `Cbreg`/`Index`. The accessor bodies decode exactly:

```c
// SparseCoreTecVectorLoadTileSpmemLoadDestField::GetConcatenatedValue        (gfc 0x1ecb9b20)
return (*((uint64_t *)this + 5) >> 52) & 0x3F;   // word0x28 bit52, 6-bit  — the loaded-row VREG
// …TileSpmemLoadBaseAddressField::GetConcatenatedValue                        (gfc 0x1ecb9b40)
return (*((uint64_t *)this + 5) >> 45) & 0x7;     // word0x28 bit45, 3-bit  — TILE_SPMEM tile base
// …TileSpmemLoadOffsetField::GetConcatenatedValue                             (gfc 0x1ecb9b60)
return (*((uint64_t *)this + 5) >> 42) & 0x7;     // word0x28 bit42, 3-bit  — within-window offset
// …TileSpmemLoadStrideField::GetConcatenatedValue                             (gfc 0x1ecb9b80)
return (*((uint64_t *)this + 5) >> 38) & 0xF;     // word0x28 bit38, 4-bit  — per-element stride
// …TileSpmemLoadMaskField::GetConcatenatedValue                               (gfc 0x1ecb9ba0)
return (*((uint64_t *)this + 5) >> 33) & 0x1F;    // word0x28 bit33, 5-bit  — lane predicate
// …TileSpmemLoadIndexedIndexField::GetConcatenatedValue                       (gfc 0x1ecb9dc0)
return (*((uint64_t *)this + 5) >> 27) & 0x3F;    // word0x28 bit27, 6-bit  — gather index VREG
// …TileSpmemLoadCircularBufferCbregField::GetConcatenatedValue                (gfc 0x1ecb9be0)
return *((uint16_t *)this + 23) & 0xF;            // 16-bit @ byte 0x2e = bits48..63, &0xf => bit48/4
```

The densest op-form, `TileSpmemLoadIndexedCircularBuffer` (`Index` accessor `0x1ecb9ea0`, `Cbreg` `0x1ecb9e20`, `Dest` `0x1ecb9e00`), carries *all* of `{Dest@52, Cbreg@48, BaseAddress@45, Offset@42, Stride@38, Mask@33, Index@27}` plus `Opcode@58` in word `0x28` — verified to fit contiguous bit 27..60 with no overlap and no gap.

> **QUIRK — every load field is in word `0x28`; there is no second data word.** The [VectorStore](vectorstore-slot.md) slot spreads its fields across word `0x30` (`Source`/address-build) with `Dest` borrowed into word `0x28`. The load has no store mux to drive, so it packs `Dest`, `Index`, the address-build fields, *and* the opcode into the single word `0x28`. A reimplementer porting the store-side decoder must re-base every shift off `0x28`, not `0x30`, and re-map every position — the field *order* differs (store has `Source@27`; load has `Index@27`, `Dest@52`).

> **GOTCHA — the load has no dtype field.** The 5 load ops are addressing-mode only; there is no `S32`/`F32`/`S16`/`Bf16` split as on the [VectorStore](vectorstore-slot.md) `Add` forms. The loaded element type is fixed by the consuming [VectorExtended](vectorextended-vex.md)/`VectorAlu` op, not by the load. Modeling `VectorLoad` with a dtype operand will collide with the consuming op's type and mis-decode. Whether a separate load-width control bit exists elsewhere in the bundle (vs word `0x28`) was not searched — `LOW` for that one negative.

<a id="load-store-bit-symmetry"></a>
### Load/Store bit-position symmetry

The two slots are deliberately mirror-symmetric, and the symmetry is the mechanism behind fetch-and-add:

```text
                      word         packed fields
 VectorStore (0x30):  Source@27  Mask@8   Stride@13  Offset@17  Base@20  Cbreg@23  Index@2  Opcode@33
                      + Dest @ word0x28 bit52  (ReturnValueAdd forms only)
 VectorLoad  (0x28):  Index@27   Mask@33  Stride@38  Offset@42  Base@45  Cbreg@48  Dest@52  Opcode@58
                                                                                    ▲
   store fetch-and-add Dest @word0x28 bit52  ════════════════════════════════════════╝  SAME mux
```

The store puts its data fields in word `0x30` because it drives the store mux; the load packs everything in word `0x28`. The *one* field they share at the same bit position is `Dest@word0x28 bit52` — the load writes its row there, and the store's `IndexedReturnValueAdd` family returns its pre-add value through the *identical* position. All 8 gfc store `ReturnValueAdd` ops were confirmed (on the [store page](vectorstore-slot.md#the-fetch-and-add-return-path)) to read `Dest` from word `0x28` bit 52. This bit-position identity is the structural proof that the fetch-and-add is a load-then-add (see [§The Fetch-and-Add Return Path](#the-fetch-and-add-return-path)).

---

## The 5-Op Addressing-Mode Roster

### The opcode-recovery model

Each load op-form is a distinct C++ type `SparseCoreTecVectorLoadTileSpmemLoad<Form>Opcode` carrying a `Matches() const` predicate that masks the opcode field out of the decoded-instruction word and compares it to the op's signature. The field is **3-bit @ bit 58 of word `0x28`** (mask `0x1c00000000000000`), so `opcode = cmp_immediate >> 58`. The base op (`TileSpmemLoad`, 0) is tested via `testb $0x1c, 0x2f(%rdi); sete` — the byte at `+0x2f` is bits 56..63 of word `0x28`, masking `0x1c` isolates the 3 opcode bits (58..60), all-zero ⇒ op 0. Byte-exact:

```c
// SparseCoreTecVectorLoadTileSpmemLoadOpcode::Matches                 (gfc 0x1ecb9a00)
return (*((uint8_t *)this + 47) & 0x1C) == 0;            // byte +0x2f, opcode 0
// …TileSpmemLoadIndexedOpcode::Matches                                (gfc 0x1ecb9a60)
return (*((uint64_t *)this + 5) & 0x1C00000000000000) == 0xC00000000000000;   // 0xc…>>58 = 3
```

`*((uint64_t*)this + 5)` is the word at byte offset `0x28`. All 5 gfc load-op `Matches()` immediates were enumerated; the resulting opcode set is **contiguous 0..4, no gaps, no duplicates**.

### The full roster (gfc, 5 ops, byte-confirmed)

| op | mnemonic (`TileSpmem…`) | mode | extra fields | `Matches` immediate (`>>58`) |
|---:|---|---|---|---|
| 0 | `Load` | plain | — | `byte+0x2f & 0x1c == 0` |
| 1 | `LoadCircularBuffer` | CB-windowed | `Cbreg` | `0x04…>>58 = 1` |
| 2 | `LoadCircularBufferPostUpdate` | CB + advance | `Cbreg` | `0x08…>>58 = 2` |
| 3 | `LoadIndexed` | indexed gather | `Index` | `0x0c…>>58 = 3` |
| 4 | `LoadIndexedCircularBuffer` | indexed gather, CB | `Index`,`Cbreg` | `0x10…>>58 = 4` |

Per-form field sets:

| op-form | field set |
|---|---|
| `TileSpmemLoad` | `{Dest, BaseAddress, Offset, Stride, Mask}` |
| `+CircularBuffer` / `+CircularBufferPostUpdate` | adds `{Cbreg}` |
| `+Indexed` | adds `{Index}` |
| `+IndexedCircularBuffer` (densest) | adds `{Cbreg, Index}` |

### The mode lattice

| Mode | Marker in name | Behavior | Extra field | Embedding role |
|---|---|---|---|---|
| **plain load** | (no qualifier) | read addressed word(s) into `Dest` | — | gathered-row load |
| **CircularBuffer** | `CircularBuffer` | address via a [CBREG](cbreg.md) window (16 CBREGs, 4-bit selector) | `Cbreg` | windowed minibatch tile read |
| **+PostUpdate** | `…PostUpdate` | advance the CBREG offset after the load | `Cbreg` | streaming tile read without a separate pointer bump |
| **Indexed** | `Indexed` | per-element gather offset read from a VREG | `Index` | per-id row gather |
| **IndexedCircularBuffer** | `Indexed…CircularBuffer` | per-element gather within a CBREG window | `Index`,`Cbreg` | windowed per-id gather |

The load mode lattice is the read-side subset of the [VectorStore](vectorstore-slot.md) lattice (plain / CircularBuffer / PostUpdate / Indexed), *minus* the `Add`/`ReturnValue` accumulate semantics — those only make sense on a write. The `Indexed` forms supply a 6-bit `Index` VREG (`word0x28 bit27`) so each lane reads `addr + Index[lane]` — the per-id gather that pairs with the [Stream](stream-gather-scatter.md) `GATHER` stage feeding `TILE_SPMEM`.

> **QUIRK — `CircularBufferPostUpdate` exists for the non-indexed CB form but not for the indexed one.** The roster has `LoadCircularBufferPostUpdate` (op 2) but there is no `LoadIndexedCircularBufferPostUpdate`. This mirrors the [VectorStore](vectorstore-slot.md) sparsity rule (PostUpdate only pairs with non-indexed CB). A reimplementer must not synthesize an indexed post-update load; the 5 entries are exactly the reachable cells.

> **NOTE — the consuming-stage dispatch counts 18 opcodes mapping to these 5 ops.** The TEC bundle consumer (`ConsumeOneTecBundleInstruction`, gfc `utils::` band; glc `0x13a08e00`) routes a 18-entry inner jump table to these 5 `EmitVectorLoadOrStore<…SparseCoreTecVectorLoad_<Form>>` leaves — the multiple opcodes per op are pred / non-pred / circular-mask MCInst forms distinguished by the operand fields, not by the op identity. The opcode→op map is 18→5; the field decode above is the same for every form. (Dispatch detail is in the code-gen pages; this page owns the field decode.)

---

## The SourceOne Seed Enum

### What it selects

The reduction stage downstream of the load reads its scan seed / accumulator carry-in through a 3-bit field named `SourceOne`, byte-confirmed at **word `0x28` bit 13** (op-invariant across the scan ops; read from `…AddScanS32SourceOneField` gfc `0x1eca7c40`):

```c
// SparseCoreTecVectorExtendedAddScanS32SourceOneField::GetConcatenatedValue  (gfc 0x1eca7c40)
return (uint8_t)HIBYTE(*((uint16_t *)this + 20)) >> 5;
// *((uint16_t*)this + 20) is the 16-bit at byte 0x28; HIBYTE is byte 0x29 (bits 8..15 of word0x28);
// >>5 takes the top 3 bits of that byte = bits 13..15 of word0x28 → 3-bit @ bit13.
```

The value is an `asic_sw::deepsea::gxc::gfc::isa::VexSourcePortEncoding`. It is **NOT a constant-pool index** ({0, +inf, -inf}); it is a *source-bus selector* — it routes the EUP scan-input/carry-in mux to one of the V0/V1/V2/V3 VREG read ports (an `X` selector or a `Y_VREG` selector sub-port) or to the `VST` source bus.

### The 8 values

| value | enum name | role |
|---:|---|---|
| 0 | `VEX_SOURCE_PORT_ENCODING_VST_SOURCE` | the VST (vector-store) source bus |
| 1 | `VEX_SOURCE_PORT_ENCODING_V0_Y_VREG` | V0 operand, `Y_VREG` sub-port |
| 2 | `VEX_SOURCE_PORT_ENCODING_V0_X` | V0 operand, `X` sub-port |
| 3 | `VEX_SOURCE_PORT_ENCODING_V1_Y_VREG` | V1 operand, `Y_VREG` sub-port |
| 4 | `VEX_SOURCE_PORT_ENCODING_V1_X` | V1 operand, `X` sub-port |
| 5 | `VEX_SOURCE_PORT_ENCODING_V2_Y_VREG` | V2 operand, `Y_VREG` sub-port |
| 6 | `VEX_SOURCE_PORT_ENCODING_V2_X` | V2 operand, `X` sub-port |
| 7 | `VEX_SOURCE_PORT_ENCODING_V3_Y_VREG` | V3 operand, `Y_VREG` sub-port |

The enum is cross-confirmed by `xla::ghostlite::GhostliteProtoUtils::GetVexSourcePortEncoding(common_proto_utils::VregReadPort)` (gfc `0x1c5ee280`), a switch that maps the compiler's logical `VregReadPort` onto these encodings 1:1 — cases 0..7 return encodings 0..7 with an OK status, and the two higher ports are rejected: **case 8 (`V3_X`) returns `InvalidArgument` ("The V3_X slot (port number 8) cannot be used by a VEX instruction.")** and **case 9 (`MISC_AUX`) returns `InvalidArgument` ("MISC_AUX not supported on GLC")**:

```c
// xla::ghostlite::GhostliteProtoUtils::GetVexSourcePortEncoding(common_proto_utils::VregReadPort)  (gfc 0x1c5ee280)
switch (port) {
  case 0: *(int*)(this+2) = 0; *(uint64_t*)this = 1 /*OK*/; return this;  // VST_SOURCE
  case 1: *(int*)(this+2) = 1; goto ok;                                   // V0_Y_VREG
  case 2: *(int*)(this+2) = 2; goto ok;                                   // V0_X
  // … cases 3..7 → encodings 3..7 (V1_Y_VREG, V1_X, V2_Y_VREG, V2_X, V3_Y_VREG)
  case 8: /* V3_X — InvalidArgument: cannot be used by a VEX instruction */
  case 9: /* MISC_AUX — InvalidArgument: not supported on GLC */
}
```

The `SparsecoreVregReadPort` proto enum (the descriptor strings are stored in declaration order) is `{VST_SOURCE=0, V0_Y_VREG=1, V0_X=2, V1_Y_VREG=3, V1_X=4, V2_Y_VREG=5, V2_X=6, LANE_ID=7}` — the same `Y_VREG`-before-`X` order as the encoding table. The wider `common_proto_utils::VregReadPort` this function actually accepts extends past that proto: ports 8 (`V3_X`) and 9 (`MISC_AUX`) exist as inputs and are rejected, and input port 7 maps to encoding 7 (`V3_Y_VREG`).

### Why a selector, not a constant

`SourceOne` selects *which read port (or the VST bus) feeds the scan's first/seed input*. The reduction identity element (0 for `Add`, ±inf for `Min`/`Max`) is whatever value is *placed in that port* — not a hardwired `SourceOne` code. This is the mechanism that chains a scan's accumulator across tiles / across the V operand frame: write the previous partial to a chosen V port, set `SourceOne` to that port, and the scan resumes with that carry-in.

> **QUIRK — there are 4 source-bus encodings (V0..V3) but only 3 V operand pairs in the VEX frame.** The [VectorExtended](vectorextended-vex.md) operand frame has 3 V operand pairs (V0/V1/V2, each a `Y_VREG` + `X` selector), yet `SourceOne` can select `V3_Y_VREG` (enc 7). Whether V3 is a separate physical read port (a chained-accumulator / VST-feedback path reachable *only* via `SourceOne`) or an alias of the VST source is `HIGH` — structurally confirmed as a reachable encoding, micro-architecturally inferred. `HasVexSourceBuses()` is a per-target predicate (Ghostlite gfc / Jellyfish / Viperfish / Pufferfish), so the bus set is gen-dependent.

---

## The Segmented-Scan Boundary Operand

The reduction stage the load feeds is a [VectorExtended](vectorextended-vex.md) scan; for embedding *sum* the scan is *segmented* — it resets the running accumulator at per-sample boundaries. The boundary is bound as an SSA operand at the MLIR level, byte-confirmed from the op trait packs:

| MLIR op | operands | results | reduction kind |
|---|---|---|---|
| `sparse_core::ScanOp` | `AtLeastNOperands<1>` (data) | `OneResult` | `reduction_op` attr |
| `sparse_core::SegmentedScanOp` | `NOperands<2>` (data, segment_ids) | `OneResult` | `reduction_op` attr |

`SegmentedScanOp::build(OpBuilder, OperationState, Type result, Value input, Value segment, StringAttr reduction_op)` (gfc `0x145fd4a0`) issues two `addOperands` calls **unconditionally** (operand 0 = input, operand 1 = segment) — the exact-2 form `NOperands<2>`. The sibling `ScanOp::build(OpBuilder, OperationState, Type, Value, Value, StringAttr)` (gfc `0x145f92e0`/`0x145f9480`) guards its FIRST `addOperands` behind `if (first_value)` and adds the second unconditionally — i.e. the first operand is optional, the `AtLeastNOperands<1>` form. The `reduction_op` `StringAttr` is stored as a property on both. So:

- **operand 0** = the data / value to scan.
- **operand 1** = the **segment ids** — the scan resets the running accumulator wherever the segment id changes. This is the per-segment boundary the task asks for.

The `reduction_op` string is decoded by byte-comparison in `SegmentedScanOpLowering::matchAndRewrite` (gfc `0x13589d40`): `"sum"`, `"max"`, `"min"`. The lowering chain splits on the segmented-vs-plain op identity, and the segmentation is carried in the intrinsic name as a `.seg.` infix:

```text
HLO embedding-sum
  → sparse_core::SegmentedScanOp        (2 operands: data, segment_ids)
    → SegmentedScanOpLowering (gfc 0x1358 9d40) / ScanOpLowering<SegmentedScanOp,…> (0x135f3000)
      → LLVM intrinsic llvm.tpu.{add,max,min}[.full/.half][.seg].scan{1x,2x}[.index]   (.seg = segmented)
   vs sparse_core::ScanOp               (1+ operands: data, optional second)
      → ScanOpLowering (0x1358 ab00) / ScanOpLowering<ScanOp,…> (0x135f2580)
      → llvm.tpu.{add,max,min}[.full/.half].scan{1x,2x}[.index]                        (no .seg infix)
```

The SC isa_emitter then register-allocates each intrinsic operand to a **free V read port** via `FindAndEmitToUnusedPort<SparsecoreVregReadPort, …>` and writes it into the matching slot V-field (a port→slot jump table). So the **segment-id operand lands on whichever V0/V1/V2 read port is free at allocation time** — a register-ALLOCATED V operand, not a fixed slot. The binding is by SSA operand #1; the placement is by the read-port allocator.

> **NOTE — the `.seg.` infix is the segmentation marker; `scan1x`/`scan2x` is a width/throughput marker, NOT an operand count.** The intrinsic family is `llvm.tpu.{add,max,min}[.full/.half][.seg].scan{1x,2x}[.index]` (all byte-confirmed in the binary: `llvm.tpu.add.seg.scan1x`, `llvm.tpu.add.full.seg.scan2x`, `llvm.tpu.max.seg.index.scan2x`, etc.). The cleanest structural marker that a scan is segmented (a per-sample embedding sum) is the `.seg.` infix together with the second SSA operand (the segment ids) on `SegmentedScanOp`; `scan1x` vs `scan2x` and `.full` vs `.half` are independent throughput/lane-width axes, not the operand count. The full `reduction_op` attr set is confirmed as `{sum, max, min}`; whether `mean`/`sqrtn` exist as attrs vs being a post-scan divide was not enumerated — `LOW` for that boundary.

---

## The Fetch-and-Add Return Path

### Pre-add return through the load-Dest mux

The [VectorStore](vectorstore-slot.md) `IndexedReturnValueAdd*` family is the fetch-and-add: it accumulates `Source` into `mem[addr + Index]` *and* returns the value present **before** the add into a `Dest` VREG. The decisive fact for *this* page is that the return `Dest` is read from **word `0x28` bit 52 — the identical mux position `VectorLoad` writes its loaded row through**. All 8 gfc store `ReturnValueAdd` ops were confirmed to read `Dest` from word `0x28` bit 52 (the [store page](vectorstore-slot.md#the-fetch-and-add-return-path) owns that table). The return therefore captures the pre-add value because the op is structurally a load-then-add:

```c
// fetch-and-add structural model — the Dest is the LOAD mux, so it captures pre-add
function FetchAndAddStore(slot):                  // e.g. TileSpmemStoreIndexedReturnValueAddF32
    addr  = BuildAddress(BaseAddress, Offset, Stride, Cbreg?)   // word0x30 fields (store side)
    addr += Index[lane]                            // per-element scatter (Index @ word0x30 bit2)
    old   = mem[addr]                              // read THROUGH the load-Dest mux (word0x28 bit52)
    Dest[lane] = old                               // return the PRE-add value (== VectorLoad Dest@52)
    mem[addr] = old + Source[lane]                 // atomic accumulate (word0x30 Source @bit27)
```

At the MLIR level the fetch-and-add is `sparse_core::VectorLoadStoreIdxAddOp`, which **has a result Type** — confirmed from `VectorLoadStoreIdxAddOp::build(OpBuilder, OperationState, Type, Value, Value, ValueRange, Value)` (gfc `0x1459d840`), whose leading `Type` argument is the result type (the returned pre-add value). The plain scatter-add `sparse_core::VectorStoreIdxOp` has **no result Type**. The lowering (`VectorLoadStoreIdxAddOpLowering`, gfc `0x135c3…`) computes a strided element pointer (`Mul`/`Add`/`InsertElement`) then emits `LLVM::AddOp` (the read-modify-add). The LLVM intrinsic naming confirms: `llvm.tpu.vst.msk.idx.ret.add.np` / `…ret.add.e4m3.np` / `…ret.add.e5m2.np` (all byte-confirmed; `ret.add` = return-then-add, all carry the `.np` suffix) vs `llvm.tpu.vst.[cb.]msk.idx.add[.e4m3/.e5m2][.np]` (plain indexed scatter-add, no `ret`, and also available in a `.cb.` circular-buffer form).

### Scatter ordering and dedup on the load path

```text
DLRM embedding scatter-add — ordering is made moot by collapsing duplicates first
  gathered ids ──► SortInteger ──► Uniquify ──► DuplicateCount ──► scatter-add (fetch-and-add)
                   (VEX 20/21)     (26/27)       (24/25)            each UNIQUE row touched ONCE
                                       │               │                     ▲
                          WithLaneIds inverse-map      count× multiplicity ──┘
                          (routes result back)         (forward: /count mean · backward: ×count grad)
```

For *concurrent* same-address fetch-and-adds within one vector (true duplicate indices), the per-lane commit order is hardware behavior not present in the C++. In the embedding path it never fires: the [dedup pipeline](dedup-multiplicity.md) — `SortInteger` → `Uniquify` → `DuplicateCount` (VEX scan ops, byte-confirmed op set, all `NOperands<2>`) — collapses duplicate ids *before* the scatter, so each unique row is fetched/updated exactly once. The `WithLaneIds` variants (`UniqueWithLaneIdsOp` / `DuplicateCountWithLaneIdsOp`, `NResults<3>`) emit a third result: the per-input → unique-representative lane-id map (an inverse permutation) that routes each per-input result back to its original position. The `DuplicateCount` multiplicity scales the gradient (×count backward; the mean divisor forward), so a collapsed duplicate accumulates the correct count× contribution. The dedup *is* the correctness mechanism; the fetch-and-add ordering only matters for a raw, deduplication-disabled scatter, gated by `xla_tpu_enable_sparse_core_computation_deduplication`.

> **NOTE — the in-vector duplicate-index commit order is silicon (LOW for raw scatter); the count→weight arithmetic was not bit-traced.** Two facts remain inferred: (1) the exact HW serialization of concurrent same-address fetch-and-adds within one vector is not in the C++ (moot for the dedup path, unspecified for a raw scatter); (2) the exact arithmetic that folds the `DuplicateCount` i32 result into the gradient before the `IndexedReturnValueAdd` was not bit-traced — the op set and result counts are CONFIRMED, the `count×gradient` fold is the lowering body and is `LOW`.

---

## The Embedding-Reduce Composition

`VectorLoad` is stage 2 of the SparseCore embedding-reduce datapath. The full composition (the other stages owned by the linked pages):

```text
SparseCore embedding lookup + reduce — 5-stage TEC datapath
  1. GATHER   Stream IndirectStream          id list → HBM[base+id*stride] → TILE_SPMEM   (stream-gather-scatter)
  2. LOAD     VectorLoad TileSpmemLoad*       TILE_SPMEM rows → VREGs  (Dest/Index/Base/Offset/Stride/Mask/Cbreg
                                              all @word0x28)                                ◄── THIS PAGE
  3. REDUCE   VectorExtended SegmentedAddScan operand0=data, operand1=segment_ids; SourceOne seed selects the
                                              carry-in source bus (V0..V3 / VST)            (vectorextended-vex)
  4. DRAIN    VectorResult EupResult/PopXrf*  XRF (extended-result FIFO) → VRF              (vector-opcode-enum)
  5. SCATTER  VectorStore …Add{dt}            atomic scatter-add gradient INTO the row; fetch-and-add returns
                                              pre-add (Dest@word0x28 bit52 — the load mux)  (vectorstore-slot)
```

Stage 2 (this page) pulls the gathered rows into VREGs; stage 3's `SourceOne` (this page) selects the scan seed, and its segment-id operand (this page) supplies the per-sample reset; stage 5's fetch-and-add returns its pre-add value through *this page's* load-Dest mux. The load is dtype-agnostic so the same 5 ops serve every embedding width; the type is fixed at stage 3.

---

## Function Map

| Symbol (gfc) | Address | Role |
|---|---|---|
| `…VectorLoadTileSpmemLoadOpcode::Matches` | `0x1ecb9a00` | op 0 predicate (`byte+0x2f & 0x1c == 0`) — the base op |
| `…VectorLoadTileSpmemLoadIndexedOpcode::Matches` | `0x1ecb9a60` | op 3 (`cmp 0xc00000000000000` → `>>58 = 3`) |
| `…VectorLoadTileSpmemLoadDestField::GetConcatenatedValue` | `0x1ecb9b20` | `Dest` @ word `0x28` >> 52 & 0x3f |
| `…VectorLoadTileSpmemLoadBaseAddressField` | `0x1ecb9b40` | `BaseAddress` @ word `0x28` >> 45 & 0x7 |
| `…VectorLoadTileSpmemLoadOffsetField` | `0x1ecb9b60` | `Offset` @ word `0x28` >> 42 & 0x7 |
| `…VectorLoadTileSpmemLoadStrideField` | `0x1ecb9b80` | `Stride` @ word `0x28` >> 38 & 0xf |
| `…VectorLoadTileSpmemLoadMaskField` | `0x1ecb9ba0` | `Mask` @ word `0x28` >> 33 & 0x1f |
| `…VectorLoadTileSpmemLoadCircularBufferCbregField` | `0x1ecb9be0` | `Cbreg` @ word `0x28` >> 48 & 0xf (CB forms) |
| `…VectorLoadTileSpmemLoadIndexedIndexField` | `0x1ecb9dc0` | `Index` @ word `0x28` >> 27 & 0x3f (Indexed forms) |
| `…VectorLoadTileSpmemLoadIndexedCircularBufferDestField` | `0x1ecb9e00` | densest-form `Dest` (same @52) |
| `…VectorExtendedAddScanS32SourceOneField::GetConcatenatedValue` | `0x1eca7c40` | `SourceOne` @ word `0x28` >> 13 (3-bit), op-invariant |
| `GhostliteProtoUtils::GetVexSourcePortEncoding` | `0x1c5ee280` | `VregReadPort` → `VexSourcePortEncoding` 1:1 (8 values) |
| `mlir::sparse_core::SegmentedScanOp::build` | `0x145fd4a0` | 2-operand build (operand0=data, operand1=segment_ids) |
| `mlir::sparse_core::VectorLoadStoreIdxAddOp::build` | `0x1459d840` | fetch-and-add op; leading `Type` arg = result (pre-add) |

Cross-gen anchors: the vfc `VectorLoad` opcode field is **3-bit @ word `0x28` bits[56:58]** (base op via `testb $0x7, 0x2f`; `CircularBuffer` via `mask 0x07<<56 cmp 0x01<<56` → 1), with `TileSpmemIndexedLoad*` (Indexed-*prefix*) naming; gfc/glc are **3-bit @ bits[58:60]** with `TileSpmemLoadIndexed*` (Indexed-*suffix*) naming — a 2-bit upward shift (GF inserted 2 bits below the opcode). The decode VALUES 0..4 and the operand-field semantics are gen-stable; the per-gen count is **5 ops in vfc/glc/gfc**. The 2 bits GF inserted below the opcode were not identified — `LOW`.

> **NOTE — `TensorCoreVectorLoad*` is a different engine, excluded from the 5-count.** The `0x1f9e9880+` `VectorLoad` symbols (`TensorCoreVectorLoad0/1`, `TensorCoreVectorMiscVectorLoad`) belong to the TensorCore datapath, not the SparseCore TEC. Only the `SparseCoreTecVectorLoad*` types contribute to this slot's 5-op roster; a reimplementer grepping for `VectorLoad` must filter to the `SparseCoreTec` prefix.

---

## Considerations

- **Re-base every shift off word `0x28`.** Porting the store-side decoder to the load means re-basing all shifts off `0x28` (not `0x30`) *and* re-mapping the order: store `Source@27`, load `Index@27`; store opcode @33, load opcode @58. The load's single-word packing is the structural difference.
- **No dtype on the load.** Decode `VectorLoad` as addressing-mode only; the element type comes from the consuming [VectorExtended](vectorextended-vex.md)/`VectorAlu` op. A dtype operand on the load is a modeling error.
- **`SourceOne` is the cross-tile scan-chaining seam.** It selects the carry-in source bus, so the reduction identity is a value placed in a V port, never a hardwired code. Treating `SourceOne` as a constant-pool index mis-models the accumulator chaining.
- **The segment boundary is operand 1, register-allocated to a free V port.** Bind the segment id as the second SSA operand of `SegmentedScanOp`; the placement (which V0/V1/V2 port) is the read-port allocator's choice, recorded in the slot V-field.
- **Fetch-and-add ordering is silicon (LOW for raw scatter).** The pre-add return is structurally confirmed (Dest = load mux @52, `.ret.add` intrinsic, result-typed `VectorLoadStoreIdxAddOp`); the in-vector duplicate-index commit order is not in the C++ and is undefined here for a non-deduplicated scatter. It is moot for the embedding path (the [dedup](dedup-multiplicity.md) collapses duplicates first).
- **Unmapped (LOW/inferred).** The `count×gradient` multiplicity fold (lowering body); the V3_Y_VREG physical-port identity (4th read port vs VST alias); the 2 bits GF inserted below the opcode (bits[56:58] → [58:60] vs vfc); whether a separate load-width control bit exists outside word `0x28`; the full `reduction_op` attr set beyond `{sum, max, min}`.

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreTecVectorLoadTileSpmemLoad*Opcode::Matches` | the 5 per-op predicates that define the opcode values (0..4 @ word `0x28` bit 58) |
| `SparseCoreTecVectorStore*` (`0x1ecc9f40+`) | the store counterpart; shares the `Dest` mux (word `0x28` bit 52) the fetch-and-add returns through |
| `SparseCoreTecVectorExtended*::SourceOne` (`0x1eca7c40` gfc) | the scan seed-port selector field in the same word `0x28` |
| `GhostliteProtoUtils::GetVexSourcePortEncoding` (`0x1c5ee280`) | maps logical `VregReadPort` → the 8 `VexSourcePortEncoding` values |
| `mlir::sparse_core::SegmentedScanOp` (`build 0x145fd4a0`) | the 2-operand segmented scan; operand 1 = the segment-id boundary |
| `mlir::sparse_core::VectorLoadStoreIdxAddOp` (`build 0x1459d840`) | the result-typed fetch-and-add; returns the pre-add value |

## Cross-References

- [TEC (Vector) Engine](tec-engine.md) — owns the 64-byte bundle, the slot bases, and the encoder-dispatch model.
- [VectorStore Slot](vectorstore-slot.md) — the store-side mirror; the bit-position symmetry (load `Dest@52` == store fetch-and-add `Dest@52`) and the 33-op type×mode matrix.
- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/reduce slot the load feeds; the home of the scan opcode roster and the `SourceOne`/`VstSource` fields.
- [Segmented Scan](segmented-scan.md) — the per-segment-reset scan the embedding sum uses; the 2-operand binding documented here in summary.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the `VectorAlu` opcode roster and the opcode-recovery model this page reuses; the `VectorResult` XRF-drain slot.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the `GATHER` stage that fills `TILE_SPMEM` before the load; the cross-HBM `SCATTER_FLOAT_ADD` atomic the on-tile scatter-add mirrors.
- [CBREG](cbreg.md) — the 16-CBREG circular-buffer register triple the `Cbreg` selector indexes; the wrap arithmetic the CB load modes inherit.
- [Dedup Multiplicity](dedup-multiplicity.md) — the Sort→Uniquify→DuplicateCount path that precedes the scatter-add and makes the fetch-and-add ordering moot.
- [SparseCore Overview](overview.md) — the three SC engine classes, per-gen presence, and where the TEC vector slots sit.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
