# VectorStore Slot

> *Every opcode value, mask immediate, field shift/width, and per-generation count on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the per-op `SparseCoreTecVectorStore<OpName>Opcode::Matches()` compare immediates, the `<OpName><Field>Field::GetConcatenatedValue()` accessor shifts, and the `SparseCoreTecVectorStoreEncoder::Encode` `BitCopy` destination-bit immediates. Other versions differ.*

## Abstract

`VectorStore` is one of the seven vector slots of the [TEC](tec-engine.md) bundle — the slot that writes a VREG back into per-tile SRAM (`TILE_SPMEM`). Unlike a scalar store it is not one op with an address mode; it is a **6-bit opcode field that encodes a two-axis product**, *element-type × store-mode*, and the opcode value *is* the choice of dtype and mode. The slot is the back half of the SparseCore embedding-reduce datapath: where the [VectorLoad](vectorload-slot.md) slot pulls gathered rows out of `TILE_SPMEM` into VREGs and the [VectorExtended](vectorextended-vex.md) slot reduces them, `VectorStore` writes the result — and, crucially, can write it as an *atomic scatter-add*, which is the building block of embedding-table gradient accumulation. A reimplementer who models `VectorStore` as "store a vector to an address" will produce a slot that cannot express the one operation the slot exists for: read-modify-add into a tile location.

The opcode space on 6acc60406 (gfc) is **33 ops, contiguous 0..32**, read byte-exactly from each op's `Matches()` predicate (`opcode = cmp_immediate >> 33`, field 6-bit @ bit 33 of the 8-byte word at struct offset `0x30`). Those 33 values are not a flat list — they are the cells of a matrix whose axes are the four accumulate dtypes (`S32`, `F32`, `S16`, `Bf16`) and the store-mode lattice (plain overwrite · scatter-ADD · circular-buffer-windowed · post-update · per-element indexed · indexed fetch-and-add). The store-mode is decoded not by a mode sub-field but by *which operand fields the op carries*: `+Cbreg` = circular-buffer window, `+Index` = per-element scatter, `+Dest` = fetch-and-add return value, and the `Add` in the name = atomic accumulate rather than overwrite. This page documents the slot field map, enumerates the 33-entry type×mode matrix with its byte-exact opcode values, the per-mode operand field set, the shared `VstSource` fusion path that lets a [VectorExtended](vectorextended-vex.md) reduction stream straight into the store mux, and the Viperfish→Ghostlite generic-to-typed rename that grew the slot from 15 ops to 33.

For reimplementation, the contract is:

- **The opcode IS the (element-type × store-mode) product.** A 6-bit field @ bit 33 of word `0x30`; `opcode = Matches_cmp_immediate >> 33`; contiguous 0..32 on gfc/glc. There is no separate dtype operand and no separate mode operand — both are baked into the opcode value, ordered `{S32,F32}` interleaved first then `{S16,Bf16}` (the dtype split), within each the store-mode lattice.
- **The store-mode is decoded by the operand-field SET, not a mode bit.** Plain `{Source,BaseAddress,Offset,Stride,Mask}`; `+Cbreg` (4-bit, → 16 [CBREGs](cbreg.md)) for circular-buffer forms; `+Index` (6-bit VREG) for indexed scatter; `+Dest` (6-bit VREG @ word `0x28`) for the fetch-and-add return. The densest form (`IndexedCircularBufferReturnValueAdd*`) carries all of them with no overlap.
- **`Add` means atomic read-modify-add, not overwrite.** Plain `TileSpmemStore`/`…Indexed`/`…CircularBuffer` overwrite the addressed words; every `…Add{dt}` form accumulates into the existing `TILE_SPMEM` location. This is the embedding-gradient accumulator; the cross-HBM equivalent is the [Stream](stream-gather-scatter.md) slot's `SCATTER_FLOAT_ADD`.
- **The slot shares its `Source` bit position with `VectorExtended`'s `VstSource`.** Both sit at word `0x30` >> 27 & 0x3f, so a scan/reduce result can drive the store-source mux directly — the reduced row is written back without a separate `VectorStore`-slot round-trip.

| | |
|---|---|
| **Slot** | `VectorStore` — `TecVectorStore` slot of the 64-byte [TEC bundle](tec-engine.md#the-tec-bundle-64-bytes) |
| **Bundle base / opcode bit** | base @328, opcode @353 (absolute bundle bit; `Source` @347) |
| **Opcode field** | 6-bit @ bit 33 of word `0x30` (gfc/glc); 4-bit @ bit 31 (vfc) |
| **Opcode → mnemonic source** | per-op `SparseCoreTecVectorStore<Op>Opcode::Matches()` immediate (`>> 33`) |
| **Op count (per gen)** | vfc **15** · glc **33** · gfc **33** |
| **Matrix axes** | dtype {`S32`,`F32`,`S16`,`Bf16`} × mode {plain · Add · CB · CB-PostUpdate · Indexed · IndexedReturnValue} |
| **Data word** | word `0x30` (all fields) except `Dest` @ word `0x28` bit 52 (RVA forms only) |
| **Encoder** | `SparseCoreTecVectorStoreEncoder::Encode` (gfc `0x1eccbe20`) |
| **Confidence** | CONFIRMED (decompile / `Matches`-immediate anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the `VectorStore` opcode roster and field decode; the 64-byte bundle layout lives in [TEC Engine](tec-engine.md).** The bundle byte map, the slot base (@328) and the absolute opcode bit (@353), the encoder-dispatch model, and the no-check-trailer rule are documented there and not repeated. The `VectorExtended` scan family that feeds this slot is [VectorExtended (VEX)](vectorextended-vex.md); the load counterpart is [VectorLoad](vectorload-slot.md).

---

## The Slot Field Map

### Purpose

The `VectorStore` slot writes one VREG (the `Source`) into `TILE_SPMEM` at an address built from `{BaseAddress, Offset, Stride}` (or a [CBREG](cbreg.md) window), under a lane `Mask`, optionally scattered by a per-element `Index` VREG, optionally accumulating (`Add`), and optionally returning the pre-add value to a `Dest` VREG. Every data field lives in the 8-byte word at struct offset `0x30` — the same word that holds the opcode — except the fetch-and-add `Dest`, which lives in word `0x28` (the load-Dest mux, see [§The Fetch-and-Add Return Path](#the-fetch-and-add-return-path)).

### Field Layout

Confirmed byte-exact against the gfc plain `TileSpmemStore` field accessors (`SparseCoreTecVectorStoreTileSpmemStore<Field>Field::GetConcatenatedValue`), each a single `(word >> shift) & mask`. The opcode shares word `0x30` with the data fields:

```text
VectorStore slot — word 0x30 (8 bytes) + Dest in word 0x28 (gfc)
 word0x30 bit: 2        8        13       17    20    23     27       33
              ┌────────┬────────┬────────┬─────┬─────┬──────┬────────┬────────┐
              │ Index  │ Mask   │ Stride │Offs.│Base │Cbreg │ Source │ Opcode │
              │ 6b     │ 5b     │ 4b     │ 3b  │ 3b  │ 4b   │ 6b     │ 6b     │
              │ @2     │ @8     │ @13    │ @17 │ @20 │ @23  │ @27    │ @33    │
              └────────┴────────┴────────┴─────┴─────┴──────┴────────┴────────┘
              Indexed* only      ◄──── address build ────►  CB* only  the VREG  the type×mode
                                                                       stored

 word0x28 bit 52:  Dest 6b   ── IndexedReturnValue* only (fetch-and-add return VREG; == VectorLoad Dest)
```

| Field | Word | Shift | Width | Present in modes | Accessor (gfc) |
|---|---:|---:|---:|---|---|
| `Opcode` | `0x30` | 33 | 6 | all | (Matches predicate) |
| `Source` | `0x30` | 27 | 6 | all | `…StoreSourceField` `0x1ecca3e0` |
| `Cbreg` | `0x30` | 23 | 4 | `CircularBuffer*` only (→ 16 [CBREGs](cbreg.md)) | `…CbregField` |
| `BaseAddress` | `0x30` | 20 | 3 | all (explicit base / alt to CBREG base) | `…BaseAddressField` |
| `Offset` | `0x30` | 17 | 3 | all (within-window offset) | `…OffsetField` |
| `Stride` | `0x30` | 13 | 4 | all (per-element address stride) | `…StrideField` |
| `Mask` | `0x30` | 8 | 5 | all (lane predicate / vmask) | `…MaskField` `0x1ecca460` |
| `Index` | `0x30` | 2 | 6 | `Indexed*` only (per-element scatter VREG) | `…IndexField` `0x1eccaf00` |
| `Dest` | `0x28` | 52 | 6 | `IndexedReturnValue*` only (fetch-and-add return) | `…DestField` `0x1eccb860` |

The shifts above were read from the *plain* `TileSpmemStore` op-form, which is the canonical reference: it carries the address-build fields with no `Cbreg`/`Index`/`Dest`. The densest op-form, `IndexedCircularBufferReturnValueAddS32` (`Source` accessor `0x1eccb780`, `Dest` `0x1eccb860`), carries *all* of `{Source@27, Cbreg@23, BaseAddress@20, Offset@17, Stride@13, Mask@8, Index@2}` in word `0x30` plus `Dest@52` in word `0x28` plus `Opcode@33` — verified to fit with no overlap and no gap. The `{Source,BaseAddress,Offset,Stride,Mask}` positions match the [Stream](stream-gather-scatter.md) slot's scatter-add descriptor decode (`Source` @>>0x1b, `Cbreg` @>>0x17), cross-confirming the address-build field order.

> **GOTCHA — the store-mode is not a sub-field; it is the opcode value plus the operand-field SET.** There is no "mode" bits the decoder reads to pick plain-vs-CB-vs-indexed. The 6-bit opcode *is* the mode (and the dtype), and the operand fields a given opcode carries follow from it: a `CircularBuffer*` opcode reads a `Cbreg`, an `Indexed*` opcode reads an `Index`, a `ReturnValue*` opcode reads a `Dest`. A reimplementer must key the field-extraction off the opcode, not look for an orthogonal mode encoding — there is none.

> **QUIRK — the fetch-and-add `Dest` lives in word `0x28`, the load-Dest mux, not in word `0x30` with the rest of the store fields.** The `IndexedReturnValueAdd*` forms write the pre-add value into a VREG through the *exact same* bit position the [VectorLoad](vectorload-slot.md) slot uses for its `Dest` (`word0x28 >> 52 & 0x3f`). All 8 gfc `ReturnValueAdd` ops were confirmed to read `Dest` from word `0x28` bit 52. The store puts its data in word `0x30` because it drives the store mux; when it *also* returns a value it borrows the load-Dest mux — the structural reason fetch-and-add returns the *pre-add* value (it is a load-then-add). See [§The Fetch-and-Add Return Path](#the-fetch-and-add-return-path).

---

## The 33-Entry Type×Mode Scatter Matrix

### The opcode-recovery model

Each store op-form is a distinct C++ type `SparseCoreTecVectorStore<OpName>Opcode` carrying a `Matches() const` predicate that masks the opcode field out of the decoded-instruction word and compares it to the op's signature. The cmp immediate *is* the opcode (shifted): the field is 6-bit @ bit 33 of the 8-byte word at struct offset `0x30` (`mask 0x7e00000000`), so `opcode = cmp_immediate >> 33`. The base op (`TileSpmemStore`, 0) is tested with `testb $0x7e, 0x34(%rdi); sete` (the byte at `+0x34` is bits 32..39 of word `0x30`; masking `0x7e` isolates the 6 opcode bits, all-zero ⇒ op 0). Byte-exact examples:

```c
// SparseCoreTecVectorStoreTileSpmemStoreOpcode::Matches            (gfc 0x1ecc9f40)
return (*((uint8_t *)this + 52) & 0x7E) == 0;            // byte +0x34, opcode 0
// …TileSpmemStoreCircularBufferOpcode::Matches                     (gfc 0x1ecc9f60)
return (*((uint64_t *)this + 6) & 0x7E00000000) == 0x200000000;   // 0x2…>>33 = 1
// …TileSpmemStoreAddS32Opcode::Matches                             (gfc 0x1ecc9fa0)
return (*((uint64_t *)this + 6) & 0x7E00000000) == 0x600000000;   // 0x6…>>33 = 3
// …TileSpmemStoreAddF32Opcode::Matches                             (gfc 0x1ecca060)
return (*((uint64_t *)this + 6) & 0x7E00000000) == 0xC00000000;   // 0xc…>>33 = 6
// …IndexedCircularBufferReturnValueAddBf16Opcode::Matches          (gfc 0x1ecca340)
return (*((uint64_t *)this + 6) & 0x7E00000000) == 0x4000000000;  // 0x40…>>33 = 32
```

`*((uint64_t*)this + 6)` is the word at byte offset `0x30`. All 33 gfc store-op `Matches()` immediates were enumerated and decoded; the resulting opcode set is **contiguous 0..32, no gaps, no duplicates**, and the opcode→mnemonic map matches the matrix below row for row.

### The two axes

The 6-bit opcode enumerates the cells of a (dtype × store-mode) grid. The ordering is not lexical — it is the dtype-split that Ghostlite introduced (see [§VF→GF Evolution](#the-vfgf-generic-to-typed-evolution)): the gen-stable base/indexed-plain ops first, then the `{S32,F32}` Add families interleaved, then the `{S16,Bf16}` Add families:

| Axis | Values | Decoded by |
|---|---|---|
| **Element type** (accumulate dtype) | `S32` · `F32` · `S16` · `Bf16` (the `Add` forms only; plain/indexed-plain are dtype-agnostic) | the opcode value (no dtype operand) |
| **Store mode** | plain overwrite · scatter-`Add` · `CircularBuffer` (+`Cbreg`) · `+PostUpdate` (advance CBREG) · `Indexed` (+`Index`) · `IndexedReturnValue` (+`Dest`, fetch-and-add) | the opcode value + which operand fields it carries |

### The full matrix (gfc, 33 ops, byte-confirmed)

| op | mnemonic (`TileSpmem…`) | dtype | store mode | extra fields |
|---:|---|---|---|---|
| 0 | `Store` | — | plain overwrite | — |
| 1 | `StoreCircularBuffer` | — | plain, CB-windowed | `Cbreg` |
| 2 | `StoreCircularBufferPostUpdate` | — | plain, CB + advance | `Cbreg` |
| 3 | `StoreAddS32` | S32 | scatter-ADD | — |
| 4 | `StoreCircularBufferAddS32` | S32 | scatter-ADD, CB | `Cbreg` |
| 5 | `StoreCircularBufferPostUpdateAddS32` | S32 | scatter-ADD, CB+adv | `Cbreg` |
| 6 | `StoreAddF32` | F32 | scatter-ADD | — |
| 7 | `StoreCircularBufferAddF32` | F32 | scatter-ADD, CB | `Cbreg` |
| 8 | `StoreCircularBufferPostUpdateAddF32` | F32 | scatter-ADD, CB+adv | `Cbreg` |
| 9 | `IndexedStore` | — | indexed scatter (overwrite) | `Index` |
| 10 | `StoreIndexedCircularBuffer` | — | indexed scatter, CB | `Index`,`Cbreg` |
| 11 | `StoreIndexedAddS32` | S32 | indexed scatter-ADD | `Index` |
| 12 | `StoreIndexedCircularBufferAddS32` | S32 | indexed scatter-ADD, CB | `Index`,`Cbreg` |
| 13 | `StoreIndexedAddF32` | F32 | indexed scatter-ADD | `Index` |
| 14 | `StoreIndexedCircularBufferAddF32` | F32 | indexed scatter-ADD, CB | `Index`,`Cbreg` |
| 15 | `StoreIndexedReturnValueAddS32` | S32 | indexed fetch-and-add | `Index`,`Dest` |
| 16 | `StoreIndexedCircularBufferReturnValueAddS32` | S32 | indexed fetch-and-add, CB | `Index`,`Cbreg`,`Dest` |
| 17 | `StoreIndexedReturnValueAddF32` | F32 | indexed fetch-and-add | `Index`,`Dest` |
| 18 | `StoreIndexedCircularBufferReturnValueAddF32` | F32 | indexed fetch-and-add, CB | `Index`,`Cbreg`,`Dest` |
| 19 | `StoreAddS16` | S16 | scatter-ADD | — |
| 20 | `StoreCircularBufferAddS16` | S16 | scatter-ADD, CB | `Cbreg` |
| 21 | `StoreCircularBufferPostUpdateAddS16` | S16 | scatter-ADD, CB+adv | `Cbreg` |
| 22 | `StoreAddBf16` | Bf16 | scatter-ADD | — |
| 23 | `StoreCircularBufferAddBf16` | Bf16 | scatter-ADD, CB | `Cbreg` |
| 24 | `StoreCircularBufferPostUpdateAddBf16` | Bf16 | scatter-ADD, CB+adv | `Cbreg` |
| 25 | `StoreIndexedAddS16` | S16 | indexed scatter-ADD | `Index` |
| 26 | `StoreIndexedCircularBufferAddS16` | S16 | indexed scatter-ADD, CB | `Index`,`Cbreg` |
| 27 | `StoreIndexedAddBf16` | Bf16 | indexed scatter-ADD | `Index` |
| 28 | `StoreIndexedCircularBufferAddBf16` | Bf16 | indexed scatter-ADD, CB | `Index`,`Cbreg` |
| 29 | `StoreIndexedReturnValueAddS16` | S16 | indexed fetch-and-add | `Index`,`Dest` |
| 30 | `StoreIndexedCircularBufferReturnValueAddS16` | S16 | indexed fetch-and-add, CB | `Index`,`Cbreg`,`Dest` |
| 31 | `StoreIndexedReturnValueAddBf16` | Bf16 | indexed fetch-and-add | `Index`,`Dest` |
| 32 | `StoreIndexedCircularBufferReturnValueAddBf16` | Bf16 | indexed fetch-and-add, CB | `Index`,`Cbreg`,`Dest` |

> **QUIRK — the matrix is sparse on purpose; not every (dtype × mode) cell exists.** Three cells the grid would predict are *absent*, and a reimplementer must not synthesize them: there is **no `PostUpdate` for any `Indexed` form** (post-update only pairs with non-indexed CB stores — ops 2/5/8/21/24), **no `S16`/`Bf16` `IndexedReturnValueAdd` was dropped** (they exist, ops 29..32), but there is **no fetch-and-add for the non-indexed forms** (`ReturnValue` only ever appears with `Indexed`). The plain/indexed-plain overwrites (0,1,2,9,10) are dtype-agnostic, so they have no `S32/F32/S16/Bf16` variants. The 33 entries are exactly the reachable cells; the full Cartesian product would be larger.

> **GOTCHA — the dtype is part of the opcode, not an operand.** `StoreAddS32` and `StoreAddBf16` are *different opcodes* (3 vs 22), not one `StoreAdd` op with a dtype operand. A reimplementer who emits a single add-store and a separate dtype field will collide with the opcode-encoded dtype and mis-decode. The accumulate width is fixed by the opcode value; the `Source` VREG carries the data in that width.

---

## The Store-Mode Roster

The store-mode lattice is the second axis. Each mode is a behavior of the read-modify-write into `TILE_SPMEM`, decoded (as [§The Slot Field Map](#the-slot-field-map) shows) by the operand-field set the opcode carries:

| Mode | Marker in name | Behavior | Extra field | Embedding role |
|---|---|---|---|---|
| **plain store** | (no `Add`) | overwrite the addressed word(s) | — | activation / state write-back |
| **scatter-ADD** | `Add` | atomic read-modify-add into the location | — | embedding-gradient accumulate |
| **CircularBuffer** | `CircularBuffer` | address via a [CBREG](cbreg.md) window (16 CBREGs, 4-bit selector) | `Cbreg` | windowed minibatch tile |
| **+PostUpdate** | `…PostUpdate` | advance the CBREG offset after the store | `Cbreg` | streaming tile write without a separate pointer bump |
| **Indexed** | `Indexed` | per-element scatter offset read from a VREG | `Index` | per-id gradient scatter |
| **IndexedReturnValue** | `…ReturnValueAdd` | fetch-and-add: return the pre-add value to a `Dest` VREG | `Index`,`Dest` | atomic accumulate that also yields the old value |

The store mode and the dtype together pick the opcode; the opcode picks the field set. The mode lattice is the same one the [VectorLoad](vectorload-slot.md) slot uses on its read side (plain / CircularBuffer / PostUpdate / Indexed), minus the `Add`/`ReturnValue` accumulate semantics that only make sense on a write.

### The `Add` semantics — atomic accumulate

The `Add` in a name is the difference between an overwrite and an atomic read-modify-add. A plain `TileSpmemStore` (op 0) writes `Source` over the addressed words; `TileSpmemStoreAddF32` (op 6) computes `mem[addr] += Source` atomically. This is the per-tile counterpart of the cross-HBM atomic the [Stream](stream-gather-scatter.md) slot carries (`STREAM_OPCODE_SCATTER_FLOAT_ADD`): the backward pass of an embedding lookup accumulates each row's gradient into the embedding table, and on-tile that accumulate is a `VectorStore …Add{dt}`.

### The Circular-Buffer window and PostUpdate

The `CircularBuffer` forms address through one of 16 [CBREGs](cbreg.md) (a 4-bit `Cbreg` selector @ word `0x30` bit 23) rather than an explicit base — the CBREG holds the window base+offset, and the hardware wraps within the window. The `…PostUpdate` variant *advances* the CBREG offset after the store, so a loop streaming successive tiles into a windowed buffer needs no separate pointer-bump instruction. The wrap arithmetic is intrinsic to the CBREG hardware; the store op carries only the 4-bit selector, never the wrap bounds (those live in the CBREG triple — see [CBREG](cbreg.md)).

### The Indexed scatter

The `Indexed` forms add a 6-bit `Index` VREG selector (@ word `0x30` bit 2) that supplies a per-element scatter offset: instead of a single strided write, each lane writes (or accumulates) to `addr + Index[lane]`. This is the per-id scatter of an embedding gradient — the gathered ids index back into the table, and the indexed scatter-add writes each row's gradient to its row. The indexed-plain forms (9, 10) scatter an *overwrite*; the indexed-Add forms (11..14, 25..28) scatter an *accumulate*.

---

## The Fetch-and-Add Return Path

### Purpose

The `IndexedReturnValueAdd*` family (8 ops: `{S32,F32,S16,Bf16}` × `{plain-indexed, CB-indexed}`, ops 15..18, 29..32) is the fetch-and-add: it accumulates `Source` into `mem[addr + Index]` *and* returns the value that was present **before** the add into a `Dest` VREG. All 8 gfc `ReturnValueAdd` ops were confirmed to read `Dest` from word `0x28` bit 52 — the identical bit position the [VectorLoad](vectorload-slot.md) slot uses for its load destination.

### Why the pre-add value, and why it usually does not matter

```c
// fetch-and-add structural model — the Dest is the LOAD mux, so it captures pre-add
function FetchAndAddStore(slot):                  // e.g. TileSpmemStoreIndexedReturnValueAddF32 (op 17)
    addr  = BuildAddress(BaseAddress, Offset, Stride, Cbreg?)   // word0x30 fields
    addr += Index[lane]                            // per-element scatter (Index @ word0x30 bit2)
    old   = mem[addr]                              // read THROUGH the load-Dest mux (word0x28 bit52)
    Dest[lane] = old                               // return the PRE-add value
    mem[addr] = old + Source[lane]                 // atomic accumulate (word0x30 Source @bit27)
```

The returned value is the *pre-add* value because the op is structurally a load-then-add: the `Dest` is written through the same mux a plain load uses, and the lowering targets the LLVM intrinsic `llvm.tpu.vst.msk.idx.ret.add.np` (plus the `.e4m3.np`/`.e5m2.np` FP8 forms; `ret.add` = return-then-add) versus the plain indexed scatter-add `llvm.tpu.vst.[cb.]msk.idx.add`. At the MLIR level the fetch-and-add is `VectorLoadStoreIdxAddOp` (it *has* a result type) versus `VectorStoreIdxOp` (no result).

> **NOTE — the in-vector duplicate-index commit order is silicon, but the dedup path makes it moot.** For *concurrent* same-address fetch-and-adds within one vector (true duplicate indices), the per-lane commit order is hardware behavior not present in the C++. In the embedding path it never fires: the [dedup pipeline](dedup-multiplicity.md) (Sort → Uniquify → DuplicateCount) collapses duplicate ids *before* the scatter so each unique row is touched exactly once, with its multiplicity folded into the gradient. The dedup *is* the correctness mechanism; the fetch-and-add ordering only matters for a raw, deduplication-disabled scatter (gated by `xla_tpu_enable_sparse_core_computation_deduplication`).

---

## The VstSource Fusion

The [VectorExtended](vectorextended-vex.md) slot — the scan/sort/reduce engine — carries a `VstSource` field that sits at the **exact bit position** the `VectorStore` slot uses for its `Source`: word `0x30` >> 27 & 0x3f. Byte-confirmed on the gfc VectorExtended scan ops (`MinScanU32` `VstSourceField` `0x1eca7d80` reads `word0x30 >> 27 & 0x3f`) — identical to the gfc `TileSpmemStore` `Source` accessor (`0x1ecca3e0`).

```text
fused reduce → store (one TEC bundle, no VectorStore round-trip)
   VectorExtended slot          VectorStore slot
   (scan / segmented-scan)      (TileSpmemStore…Add{dt})
        │  result                     │
        └── VstSource @word0x30>>27 ───┘ Source @word0x30>>27   ◄── SAME bit position
            (the scan/reduce output drives the store-source mux directly)
```

Because the reduce output and the store source occupy the same field, a [VectorExtended](vectorextended-vex.md) reduction can drive the store-source mux directly: the reduced embedding row streams to `TILE_SPMEM` without an intervening `VectorStore`-slot instruction. The result also drains via the XRF → [VectorResult](vector-opcode-enum.md) path; the two are not mutually exclusive. This fusion is the reason the embedding-reduce inner loop fits in fewer bundles than a naive load-scan-store sequence would suggest — the scan *is* the store source.

> **QUIRK — `VstSource` is a `VectorExtended` field that names a `VectorStore` resource.** A reimplementer reading the `VectorExtended` slot template will find a 6-bit field whose meaning is "the store source the scan result feeds," not a scan operand. It is the structural seam between the two slots; treating it as an ordinary VEX operand will mis-model the fused write-back.

---

## The VF→GF Generic-to-Typed Evolution

### The growth, in counts

The slot grew from **15 ops on Viperfish to 33 on Ghostlite and 6acc60406**, and the growth is a dtype split plus a new fetch-and-add family, not new addressing modes:

| Quantity | Viperfish (vfc, v5) | Ghostlite (glc, v6e) | 6acc60406 (gfc, TPU7x) |
|---|---:|---:|---:|
| `VectorStore` op count | **15** | **33** | **33** |
| Opcode field | 4-bit @ word `0x30` bit 31 | 6-bit @ bit 33 | 6-bit @ bit 33 |
| dtype naming | `Float`/`Integer` (generic) | `S32`/`F32`/`S16`/`Bf16` | `S32`/`F32`/`S16`/`Bf16` |
| `IndexedReturnValue` (fetch-and-add) family | — | yes | yes |
| Operand frame (`Source`/`Base`/`Offset`/`Stride`/`Mask`) | same | same | same |

### What changed and what stayed

Viperfish used **type-generic names** — `TileSpmemFloatStoreAdd` and `TileSpmemIntegerStoreAdd` — folding all integer widths into `Integer` and all float widths into `Float`, with a **4-bit** opcode field @ word `0x30` bit 31. Ghostlite split each `Float`/`Integer` Add form into the four concrete dtypes (`S32`/`F32`/`S16`/`Bf16`), added the entire `IndexedReturnValue` fetch-and-add family (the 8 ops the VF set lacked), and **widened the opcode field to 6 bits @ bit 33** to hold the larger roster. The low opcodes are gen-stable across the rename: VF `TileSpmemIntegerStoreAdd` = 3 ↔ GF `StoreAddS32` = 3; VF `TileSpmemFloatStoreAdd` = 6 ↔ GF `StoreAddF32` = 6; the base/plain ops 0..2 are identical.

```text
VF 15-op roster (generic names) → GL/GF 33-op roster (typed)
  VF: TileSpmemStore                         (0)  ─┐ gen-stable
      TileSpmemStoreCircularBuffer           (1)   │ 0..2 identical
      TileSpmemStoreCircularBufferPostUpdate (2)  ─┘
      TileSpmemIntegerStoreAdd               (3)  → StoreAddS32        (3)   ┐ Integer →
      TileSpmemIntegerStoreAdd{CB,CB+adv}    (4,5)→ {S32 CB forms}            │ {S32,S16}
      TileSpmemFloatStoreAdd                 (6)  → StoreAddF32        (6)   ┐ Float →
      TileSpmemFloatStoreAdd{CB,CB+adv}      (7,8)→ {F32 CB forms}            │ {F32,Bf16}
      TileSpmemIndexedStore                  (9)  → IndexedStore       (9)
      TileSpmemIndexedStoreCircularBuffer    (10) → StoreIndexedCircularBuffer (10)
      TileSpmem{Integer,Float}IndexedStoreAdd[CB] (11..14)  → split into {S32,F32,S16,Bf16} indexed-Add
      (no fetch-and-add)                           → + IndexedReturnValueAdd family (8 new ops)
```

> **QUIRK — the VF opcode field is one bit narrower and two bits lower than GF.** Viperfish packs the opcode in 4 bits @ bit 31; Ghostlite/6acc60406 in 6 bits @ bit 33. A reimplementer targeting Viperfish must decode `(word0x30 >> 31) & 0xf` (and use the `Float`/`Integer` generic names), not the GF `(word0x30 >> 33) & 0x3f`. The 4-bit field exactly holds 15 ops (0..14); the dtype split and fetch-and-add family overflow it, which is why GF widened the field — the same 4→6 / 7→8 width pressure that grew the [VectorAlu](vector-opcode-enum.md) opcode field.

---

## The Embedding-Reduce Composition

`VectorStore` is stage 5 of the SparseCore embedding-reduce datapath; it is the slot that closes the loop. The full composition (the other stages owned by the linked pages):

```text
SparseCore embedding lookup + reduce — 5-stage TEC datapath
  1. GATHER   Stream IndirectStream          id list → HBM[base+id*stride] → TILE_SPMEM   (stream-gather-scatter)
  2. LOAD     VectorLoad TileSpmemLoad*       TILE_SPMEM rows → VREGs                       (vectorload-slot)
  3. REDUCE   VectorExtended SegmentedAddScan per-sample prefix sum / max / dedup            (vectorextended-vex)
  4. DRAIN    VectorResult EupResult/PopXrf*  XRF (extended-result FIFO) → VRF              (vector-opcode-enum)
  5. SCATTER  VectorStore …Add{dt}            atomic scatter-add the gradient INTO the row  ◄── THIS PAGE
```

The forward sum-lookup reduces gathered rows with a [VectorExtended](vectorextended-vex.md) `SegmentedAddScan` and writes the per-sample result; the backward gradient is a `VectorStore …Add{dt}` scatter-add into the windowed tile (or a [Stream](stream-gather-scatter.md) `SCATTER_FLOAT_ADD` straight into HBM). The [VstSource fusion](#the-vstsource-fusion) lets stage 3 feed stage 5's mux directly, and the [dedup pipeline](dedup-multiplicity.md) precedes the scatter so each unique row is touched once. The `IndexedReturnValueAdd` fetch-and-add (stage 5) yields the pre-add value when the gradient math needs the old accumulator.

---

## Function Map

| Symbol (gfc) | Address | Role |
|---|---|---|
| `…VectorStoreTileSpmemStoreOpcode::Matches` | `0x1ecc9f40` | op 0 predicate (`testb $0x7e,0x34`) — the base op |
| `…VectorStoreTileSpmemStoreCircularBufferOpcode::Matches` | `0x1ecc9f60` | op 1 (`cmp 0x200000000`) |
| `…VectorStoreTileSpmemStoreAddS32Opcode::Matches` | `0x1ecc9fa0` | op 3 (`cmp 0x600000000`) |
| `…VectorStoreTileSpmemStoreAddF32Opcode::Matches` | `0x1ecca060` | op 6 (`cmp 0xC00000000`) |
| `…IndexedCircularBufferReturnValueAddBf16Opcode::Matches` | `0x1ecca340` | op 32 (`cmp 0x4000000000`) — the densest op |
| `…TileSpmemStoreSourceField::GetConcatenatedValue` | `0x1ecca3e0` | `Source` @ word `0x30` >> 27 & 0x3f |
| `…TileSpmemStoreMaskField::GetConcatenatedValue` | `0x1ecca460` | `Mask` @ word `0x30` >> 8 & 0x1f |
| `…TileSpmemIndexedStoreIndexField::GetConcatenatedValue` | `0x1eccaf00` | `Index` @ word `0x30` >> 2 & 0x3f |
| `…IndexedCircularBufferReturnValueAddS32SourceField` | `0x1eccb780` | densest-form `Source` (same @27) |
| `…IndexedCircularBufferReturnValueAddS32DestField` | `0x1eccb860` | `Dest` @ word `0x28` >> 52 & 0x3f (RVA fetch result) |
| `SparseCoreTecVectorStoreEncoder::Encode` | `0x1eccbe20` | slot encoder; opcode `BitCopy(a3, 353, …, 6)` @ bundle bit 353, `Source` `BitCopy(…,347,…,6)`, slot base @328 |
| `…VectorExtendedMinScanU32VstSourceField` | `0x1eca7d80` | `VstSource` @ word `0x30` >> 27 — the fused store-source (== `Source`) |

Cross-gen anchors: vfc `VectorStore` opcode field is **4-bit @ word `0x30` bit 31** (base op via `movzwl 0x33`; `CircularBuffer` via `mask 0x780000000 cmp 0x80000000` → 1), generic `Float`/`Integer` names; glc/gfc are 6-bit @ bit 33 with the four-dtype split. The full per-gen counts — vfc 15, glc 33, gfc 33 — were re-confirmed by per-namespace `Matches`-symbol enumeration in the decompile.

> **NOTE — `TensorCoreVectorStore*` is a different engine, excluded from the 33-count.** The `0x1fa07dc0+` `VectorStore` symbols belong to the TensorCore datapath, not the SparseCore TEC. Only the `SparseCoreTecVectorStore*` types contribute to this slot's 33-op roster; a reimplementer grepping for `VectorStore` must filter to the `SparseCoreTec` prefix.

---

## Considerations

- **Opcode-keyed field extraction.** The decoder must read the opcode first, then extract `{Cbreg, Index, Dest}` only when the opcode is a `CircularBuffer*` / `Indexed*` / `ReturnValue*` form. There is no orthogonal mode encoding; reading `Index` from a non-indexed op reads garbage (those bits hold the low end of the address-build fields).
- **dtype is in the opcode.** Encode the accumulate width by selecting the right opcode (`AddS32` vs `AddBf16`), never by a separate field; the `Source` VREG supplies the data in that width.
- **Viperfish is the narrow, generic form.** 4-bit opcode @ bit 31, `Float`/`Integer` names, no fetch-and-add. A VF-targeted reimplementation decodes a 4-bit field and has only the 15 ops; the dtype split and `IndexedReturnValue` family are GL/GF-only.
- **The fetch-and-add ordering is silicon (LOW for raw scatter).** The pre-add return value is structurally confirmed (Dest = load mux, `.ret.add` intrinsic); the in-vector duplicate-index commit order is not in the C++ and is undefined here for a non-deduplicated scatter. It is moot for the embedding path (the [dedup](dedup-multiplicity.md) collapses duplicates first), but a reimplementer enabling raw scatter must treat duplicate-index ordering as unspecified.
- **Unmapped (LOW/inferred).** The exact arithmetic that folds a `DuplicateCount` multiplicity into the gradient before the scatter-add was not bit-traced (the op set and result counts are confirmed; the count×gradient fold is the lowering body). The address-build semantics of `BaseAddress`/`Offset`/`Stride` relative to a CBREG window are decoded as field positions but the exact wrap arithmetic is intrinsic CBREG hardware (see [CBREG](cbreg.md)).

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreTecVectorStoreEncoder::Encode` (`0x1eccbe20` gfc) | the slot encoder; writes the 6-bit opcode at bundle bit 353 (and `Source` at bit 347) |
| `SparseCoreTecVectorStoreTileSpmemStore*Opcode::Matches` | the 33 per-op predicates that define the matrix opcode values |
| `SparseCoreTecVectorLoad*` (`0x1ecb9a00+`) | the load counterpart; shares the `Dest` mux (word `0x28` bit 52) the fetch-and-add returns through |
| `SparseCoreTecVectorExtended*::VstSource` (`0x1eca7d80` gfc) | the fused store-source field, identical bit position to `Source` |
| `SparseCoreStream` `SCATTER_FLOAT_ADD` | the cross-HBM atomic accumulate the on-tile `…Add{dt}` mirrors |

## Cross-References

- [TEC (Vector) Engine](tec-engine.md) — owns the 64-byte bundle, the `VectorStore` slot base (@328) and opcode bit (@353), and the encoder-dispatch model.
- [VectorLoad Slot](vectorload-slot.md) — the load-side mirror; shares the `Dest` mux and the address-mode lattice (plain / CB / PostUpdate / Indexed).
- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/reduce slot that feeds this one through the shared `VstSource` field; the reduce stage of the embedding pipeline.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the `VectorAlu` opcode roster and the opcode-recovery model this page reuses; the `VectorResult` XRF-drain slot.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the cross-HBM `SCATTER_FLOAT_ADD` atomic the on-tile scatter-add mirrors; the gather stage of the embedding pipeline.
- [CBREG](cbreg.md) — the 16-CBREG circular-buffer register triple the `Cbreg` selector indexes; the wrap arithmetic the CB store modes inherit.
- [Dedup Multiplicity](dedup-multiplicity.md) — the Sort→Uniquify→DuplicateCount path that precedes the scatter-add and makes the fetch-and-add ordering moot.
- [SparseCore Overview](overview.md) — the three SC engine classes, per-gen presence, and where the TEC vector slots sit.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
