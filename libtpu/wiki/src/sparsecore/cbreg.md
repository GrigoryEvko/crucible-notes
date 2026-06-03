# CBREG Circular-Buffer Register

> *Every opcode value, bit shift, field width, and enum value on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). `.text` VA equals file offset at `0xe63c000`. Addresses are from the **gfc** (6acc60406) instances unless tagged `vfc` (Viperfish) or `glc` (Ghostlite); the three SC generations carry the same schema at different addresses. Other versions differ.*

## Abstract

A **CBREG** is the SparseCore's hardware-managed circular-buffer register: a single logical register that holds a sliding window over a linear memory region and produces a self-advancing, wrap-at-the-end address stream. It is the SC analog of a DMA descriptor ring — the mechanism that lets the embedding datapath walk a lookup-index list or stream embedding rows through a tile buffer without re-computing an address per element. Where a TensorCore loop would emit an `imul`/`add` per iteration, a CBREG emits the deterministic sequence `base, base+s, base+2·s, …, base+(N-1)·s, base, …` from one register the hardware increments and wraps for free.

Physically a CBREG is **three sub-registers** — `{base, offset, size}` — stored in three separate hardware banks (`SC_{SCS,TAC,TEC}_CBREGS_{BASE,OFFSET,SIZE}`) but addressed as one register through a single **4-bit CBREG index**, giving **16 CBREGs per sequencer bank**. `base` is a windowed pointer (into SMEM *or* TILE_SPMEM — the address space is fixed when the base is written); `size` is the modulus the offset wraps at; `offset` is the live position. An access reads/writes `base + (offset mod size)`; a *PostUpdate* access additionally advances `offset = (offset + step) mod size`. There is no modulo instruction in the binary — the wrap is a property of the OFFSET counter hardware, and "overflow" is the wrap by design, never a trap.

This page documents three reimplementable surfaces: (1) the **CBREG triple and the `CbregMetadata` sub-register selector** — the {0=BASE, 1=SIZE, 2=OFFSET} enum that Read/Write ops carry; (2) the **scalar-ALU CBREG-op bit layout** — the uniform `ScalarAlu1` slot encoding (opcode at bundle bit 154, three operand fields at slot-relative bits 10/15/21) shared across the SCS/TAC/TEC scalar lanes and across all three gens, with every opcode value; and (3) the **slot-reference binding** — how `AddCbreg` advances OFFSET, how the [Stream gather/scatter](stream-gather-scatter.md) `IndirectOffsetSource=CBREG` consumes the window, and how the TEC vector-store scatter-add references a CBREG. The opcode roster itself is owned by the [SCS Scalar Opcode Enumeration](scalar-opcode-enum.md) page (where `AddCbreg=0x33` lives); the bundle byte layout is owned by [SCS Engine](scs-engine.md). This page is the CBREG semantics those two pages point at.

For reimplementation, the contract is:

- **The register model.** One 4-bit CBREG index selects one of 16 registers per bank; each register is a `{base, offset, size}` triple in three banks, selected within an op by the 6-bit `CbregMetadata` immediate {0,1,2}.
- **The scalar-ALU op encoding.** The opcode is a 6-bit field at bundle bit 154 (`ScalarAlu1`, slot-relative bit 26); the three operand slots sit at slot-relative bits 10 (Dest, 5-bit), 15 (Meta/Y, 6-bit), 21 (X, 5-bit). Read/Write/Add/Move/SLD/SST all reuse these three slots.
- **The opcode values.** `Read=0x36`, `Write=0x35`, `Add=0x33`, `SLD/SST CircularBuffer` and their PostUpdate variants, and `Move=0x00`-primary + `0x1b`-sub (gfc only) — read from each op's `Matches()` predicate, gen-invariant.
- **The wrap and the binding.** `addr = base + (offset mod size)`; PostUpdate advances `offset = (offset + step) mod size` in hardware; `AddCbreg` is the explicit 2-operand `{cbreg, delta}` advance into OFFSET; the Stream/vector-store paths bind a CBREG by its 4-bit selector for indirect-offset windowing and scatter-add.

| | |
|---|---|
| **Register file** | 16 CBREGs per bank (4-bit selector), banks `SC_{SCS,TAC,TEC}_CBREGS_*` |
| **Per-register state** | `{base, offset, size}` triple in 3 hardware banks |
| **Sub-register selector** | `CbregMetadata` (6-bit field, 3 valid: BASE=0, SIZE=1, OFFSET=2) |
| **Scalar op slot** | `ScalarAlu1` lane; opcode `@bundle bit 154` width 6 (slot-rel 26) |
| **Operand fields** | Dest `@slot-rel 10/5` · Meta-or-Y `@15/6` · X `@21/5` (decode word `+0x18`) |
| **Scalar opcodes** | Read `0x36` · Write `0x35` · Add `0x33` · SLD `0x3f`/`0x3e` · SST `0x3d`/`0x3c` · Move `0x00`+`0x1b` |
| **Vector CBREG selector** | TEC VectorStore/Load `Cbreg` field `& 0xF` (4-bit → 16); shift gen-variant (vfc `>>21`, glc `>>22`, gfc `>>23`) |
| **Address spaces** | SMEM base (`wrcbreg.smem.base`) or TILE_SPMEM base (`wrcbreg.tilespmem.base`) |
| **Wrap** | `offset = (offset + step) mod size`, HW-implicit; no trap, wraps by design |
| **Per-gen presence** | v5+ only — VF (vfc, Viperfish), GL (glc, Ghostlite), GF (gfc, 6acc60406); none in jxc/pxc |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page owns CBREG semantics, not the opcode roster or the bundle layout.** `AddCbreg=0x33` and its sibling opcode values are catalogued on [Scalar Opcode Enum](scalar-opcode-enum.md); the 32-byte SCS bundle, the slot bases, and the 27-bit scalar-slot template are on [SCS Engine](scs-engine.md). The CBREG-as-Stream-offset-window and the scatter-add slot bit layout are on [Stream Gather/Scatter](stream-gather-scatter.md). This page documents what a CBREG *is*, how it is addressed, how it wraps, and how those ops bind it.

---

## The CBREG Register Triple

### Purpose

A CBREG is one logical circular-buffer register that the compiler allocates, initializes, and then lets the hardware drive. It exists so the SC can produce a wrapping address stream — for an embedding-id list, a gathered-row tile, or a gradient-accumulation region — without a per-element scalar address recompute. The register is *not* a memory location; it is a small hardware state object the address-generation logic and the scalar/vector ops reference by index.

### The Three Sub-Registers

A CBREG selects one of 16 logical registers; each holds three sub-register values living in three separate hardware banks, but addressed as one register via a single 4-bit index:

| Sub-register | Bank | `CbregMetadata` | Meaning |
|---|---|---:|---|
| `base` | `SC_{SCS,TAC,TEC}_CBREGS_BASE` | `0` (BASE) | window start address — in SMEM or TILE_SPMEM (address space fixed at write) |
| `size` | `SC_{SCS,TAC,TEC}_CBREGS_SIZE` | `1` (SIZE) | window length; the modulus for the wrap |
| `offset` | `SC_{SCS,TAC,TEC}_CBREGS_OFFSET` | `2` (OFFSET) | current live position within the window |

The register-class member names appear in the binary as `CBREG_BASE` / `CBREG_SIZE` / `CBREG_OFFSET`; the bank suffixes as `SC_<engine>_CBREGS_<sub>`. The `base` is a *windowed pointer*: when written through `wrcbreg.smem.base` it points into the SCS SMEM tier, and through `wrcbreg.tilespmem.base` into per-tile TILE_SPMEM (see [Addressing](#addressing-and-wraparound)). `size` and `offset` are dimensionless word/element counts within that buffer.

### The `CbregMetadata` Selector

Read/Write CBREG ops carry an immediate that names *which* of the three sub-registers the op touches. The selector occupies a 6-bit slot in the scalar instruction but only `{0,1,2}` are valid — effectively 2 bits used, the upper encodings reserved. The validator is `GetCbMetadata<…::CbregMetadata>` (vfc `0x13998fe0`, glc `0x13a046c0`, gfc `0x13a7a6a0`); its body maps the immediate exactly:

```c
// GetCbMetadata<vfc::CbregMetadata>(out, MCOperand operand)   // 0x13998fe0
//   operand must be an immediate (else LogFatal "operand.isImm()")
if (operand.imm == 2)   metadata = 2;       // OFFSET
else if (operand.imm == 1) metadata = 1;    // SIZE
else if (operand.imm != 0)                  // any other value:
    return MakeErrorImpl(
        "CB Register Metadata operand must be an immediate with a value of "
        "0 (Base), 1 (Size) or 2 (Offset). Provided value: " + imm);
else                    metadata = 0;       // BASE  (the imm == 0 fall-through)
```

The proto enum (`CbregMetadata`) agrees: `CBREG_METADATA_BASE=0`, `CBREG_METADATA_SIZE=1`, `CBREG_METADATA_OFFSET=2`, then `CBREG_METADATA_RESERVED_0..6` (seven reserved encodings). The runtime decode-side carries a matching diagnostic: `"invalid cbreg metadata: %d"` and `"Field cbreg_metadata of SparseCoreScalarAlu CbregMetadata is an enum and value 0x%x does not match any encodings."`

> **QUIRK — `AddCbreg` carries NO metadata field; it always targets OFFSET.** `Add` is the offset-advance primitive — its operand set is `{which CBREG, delta}` with no `CbregMetadata` selector, so it implicitly writes the OFFSET sub-register. Only `Read`/`Write` need an explicit metadata immediate to touch `base`/`size`/`offset`. A reimplementer who treats `AddCbreg` as a generic "add to a CBREG sub-register" will look for a metadata operand that the encoding does not have.

---

## Scalar-ALU CBREG Op Encoding

### Purpose

The scalar path mutates and reads a CBREG (set its base/size, zero its offset, advance it, copy it) and performs scalar circular-buffer loads/stores. All of these are `SparseCoreScalarAlu1` lane-1 ops; they share one uniform slot layout across the three sequencer banks (SCS, TAC, TEC) and across the three generations. This is the encoding a reimplementer must reproduce to emit a CBREG instruction.

### The Uniform Slot Layout

The scalar slot's decoded word is the struct DWORD at `+0x18` (DWORD index 6); the opcode is a 6-bit field at slot-relative bit 26, which is bundle-absolute bit 154 (the `ScalarAlu1` opcode position from [SCS Engine](scs-engine.md)). The three operand fields are reused by every CBREG op; the per-op meaning differs, the bit positions do not. Bit shifts below are read from each op's `…Field::GetConcatenatedValue()` accessor (vfc TAC instances `0x1e8e53c0..0x1e8e54a0`; gfc SCS `0x1eb7cac0..0x1eb7cbe0`):

| Field | Slot-rel bits | Width | Bundle-abs bit | Decode (from accessor) |
|---|---|---:|---:|---|
| `Dest` | `[10:14]` | 5 | 138 | `(word_0x18 >> 10) & 0x1F` |
| `field@15` | `[15:20]` | 6 | 143 | `(word_0x18 >> 15) & 0x3F` (CbregMetadata, or ScalarY) |
| `field@21` | `[21:25]` | 5 | 149 | `(word_0x18 >> 21) & 0x1F` (X / CBREG selector) |
| `opcode` | `[26:31]` | 6 | 154 | masked `& 0xFC000000`, value `>> 26` |

### Per-Op Operand Mapping

Each CBREG op assigns the three operand slots differently. Operand `X@21/5` and `Dest@10/5` are physically 5-bit but the CBREG file is 16, so CBREG selection uses only the low 4 bits (the binding 4-bit constraint is the vector path's selector — see [Per-Bank CBREG Count](#per-bank-cbreg-count)).

```text
ReadCbreg  (0x36):  SREG[Dest] <- CBREG[X][metadata]
    Dest @10/5  destination SREG          Meta @15/6  sub-register {0,1,2}    X @21/5  which CBREG

WriteCbreg (0x35):  CBREG[Dest][metadata] <- ScalarY
    Dest @10/5  which CBREG               Meta @15/6  sub-register             Y @21/5  ScalarY source

AddCbreg   (0x33):  CBREG[Dest].offset += ScalarY   (mod size)
    Dest @10/5  which CBREG               Y    @15/6  delta (ScalarY)          (no metadata — implicit OFFSET)

MoveCbreg  (0x00 primary + 0x1b sub @21):  CBREG[Dest] <- CBREG[src]   (gfc only)
    Dest @10/5  destination CBREG         src  @15/6  source CBREG            (copies whole {base,offset,size})

SLD CircularBuffer (0x3f) / PostUpdate (0x3e):  SREG[Dest] <- CB[CBREG][offset]
SST CircularBuffer (0x3d) / PostUpdate (0x3c):  CB[CBREG][offset] <- ScalarY
    Dest @10/5                           CB-sel @15/6                          index @21/5
    PostUpdate: after the access, offset = (offset + step) mod size
```

### The Opcode Values

Each op's `Matches()` predicate masks the 6-bit opcode out of the `ScalarAlu1` slot word and compares against a signature; the signature *is* the opcode. The values are gen-invariant — the gfc and vfc/glc predicates are byte-identical. Confirmed values:

| Opcode | Mnemonic | `Matches()` evidence (gfc) | Confidence |
|---:|---|---|---|
| `0x36` | `ReadCbreg` | `(word6 & 0xFC000000) == 0xD8000000`; `0xD8000000>>26 = 0x36` (gfc `0x1eb7b560`) | CONFIRMED |
| `0x35` | `WriteCbreg` | `(word6 & 0xFC000000) == 0xD4000000`; `0xD4000000>>26 = 0x35` | CONFIRMED |
| `0x33` | `AddCbreg` | `(word6 & 0xFC000000) == 0xCC000000`; `0xCC000000>>26 = 0x33` | CONFIRMED |
| `0x3f` / `0x3e` | `ScalarLoadCircularBuffer` / `…PostUpdate` | opcode field; PostUpdate VF/GL only | HIGH |
| `0x3d` / `0x3c` | `ScalarStoreCircularBuffer` / `…PostUpdate` | opcode field; PostUpdate VF/GL only | HIGH |
| `0x00`+`0x1b` | `MoveCbreg` | `(word6 & 0xFFE00000) == 0x3600000`; `>>26 = 0x00` primary, `>>21 = 0x1b` sub; gfc only | CONFIRMED |

> **QUIRK — `MoveCbreg` is a two-level (escape) opcode, not a flat 6-bit value.** Its `Matches()` masks an 11-bit field (`& 0xFFE00000`, bits 21–31) and compares `== 0x3600000`. The primary 6-bit opcode part (`>>26`) is `0x00` and the 5-bit X field (`>>21`) carries the sub-opcode `0x1b` (27). A reimplementer who reads only the 6-bit primary will see `Move` as opcode `0x00` and collide it with the control class; the discriminating bits are the X-field sub-opcode. `Move` exists only in gfc (6acc60406); VF/GL have no scalar `MoveCbreg`.

A slot encoding-mode tag distinguishes the plain Read/Write/Add/LD/ST forms from the PostUpdate forms — the mode byte carries the auto-increment bit that tells the hardware to advance OFFSET after the access. The PostUpdate-vs-plain discriminator is the separate `…PostUpdate` opcode predicate; the exact mode-byte values are not bit-decoded here (HIGH confidence).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `GetCbMetadata<vfc::CbregMetadata>` | `0x13998fe0` | validate metadata immediate {0,1,2}; LogFatal otherwise | CONFIRMED |
| `GetCbMetadata<glc::CbregMetadata>` / `<gfc::…>` | `0x13a046c0` / `0x13a7a6a0` | same, per gen | CONFIRMED |
| `EmitReadCbregOp<vfc::CbregMetadata, …ReadCbreg>` | `0x139983c0` | lower `ReadCbreg` MCInst → scalar slot | CONFIRMED |
| `EmitWriteCbregOp<…, ScsBundle, …WriteCbreg>` (vfc) | `0x139e6420` | write CBREG, SCS bundle | CONFIRMED |
| `EmitWriteCbregOp<…, TacBundle / TecBundle, …>` (vfc) | `0x13998b60` / `0x139d0d00` | write CBREG, TAC / TEC bundle | CONFIRMED |
| `EmitMoveCbregOp<…, ScsBundle / TecBundle, …MoveCbreg>` (gfc) | `0x13ac9540` / `0x13a73c80` | copy CBREG triple; gfc only, no TAC | CONFIRMED |
| `…TacScalarAlu1ReadCbregDestField::GetConcatenatedValue` | `0x1e8e5400` | Dest `>>10 & 0x1F` | CONFIRMED |
| `…TacScalarAlu1ReadCbregCbregMetadataField::…` | `0x1e8e53c0` | Meta `>>15 & 0x3F` | CONFIRMED |
| `…TacScalarAlu1ReadCbregXField::…` | `0x1e8e53e0` | X `>>21 & 0x1F` | CONFIRMED |
| `…ScalarAlu1AddCbregOpcode::Matches` (gfc) | `0x1eb7b5a0` | `(word6 & 0xFC000000)==0xCC000000` → `0x33` | CONFIRMED |
| `…ScalarAlu1MoveCbregOpcode::Matches` (gfc) | `0x1eb7b5c0` | `(word6 & 0xFFE00000)==0x3600000` → `0x00`+`0x1b` | CONFIRMED |
| `BitCopy` | `0x1fa0a900` | LE bit-field packer `(dst, dst_off, src, src_off, nbits)` | CONFIRMED |

---

## Addressing and Wraparound

### Purpose

A CBREG turns its `{base, offset, size}` triple into an effective address and, for PostUpdate ops, into a self-advancing address stream. This is the model a reimplementer encodes when allocating a circular buffer and the model the hardware executes per access.

### The Effective-Address and Advance Formulas

```c
// Per-access effective address (conceptual; the wrap is HW-implicit):
addr = base + (offset mod size);          // read or write this element

// PostUpdate access (mode tag 0x60) advances after the access:
offset = (offset + step) mod size;        // wrap at the SIZE sub-register
```

`SIZE` is the modulus. An ordinary (non-PostUpdate) access leaves OFFSET unchanged; only the PostUpdate forms (`scSLDCBREG…PostUpdate`, `scSST…PostUpdate`, the TEC vector-store `…PostUpdateAdd*`, and the Stream `post_update_indirect_offset_circular_buffer`) advance and wrap it. The explicit advance op `AddCbreg` / `llvm.tpu.cbreg.add.offset` is a 2-operand functional op `{cbreg, delta} → new_offset`; the `.in.place` variant mutates the CBREG in place.

A CBREG initialized to `{base, offset=0, size=N, step=s}` therefore emits the deterministic ring:

```text
base, base+s, base+2·s, …, base+(N-1)·s, base, base+s, …
```

This is the address-sequence prediction the address-generation logic needs to stream embedding rows or lookup indices without a per-element scalar address computation — the SC's hardware-managed DMA-descriptor-ring analog.

> **QUIRK — there is no modulo instruction; the wrap is the OFFSET counter, and overflow never traps.** No explicit modulo opcode exists in the binary. The OFFSET sub-register *is* a modulo-SIZE counter; the compiler-visible advance ops carry only `{cbreg, delta}` and never an explicit `% size`. The wrap formula `offset = (offset + step) mod size` is therefore inferred from the op shape plus the "circular buffer" semantics (HIGH confidence — the explicit modulo is intrinsic to the hardware, not transcribed). "Overflow" of a CBREG is the wrap by design — unlike the SREG file (which overflows into LSRA spill), a CBREG never traps; it wraps.

### Dual Address Space — SMEM vs TILE_SPMEM

The `base` sub-register has two typed accessors, fixing which memory tier the window points into. The choice is made when the base is written — there is one CBREG hardware register, but the address space is determined by which `wrcbreg.*.base` intrinsic set it:

```text
llvm.tpu.rdcbreg.smem.base      / .wrcbreg.smem.base       -> SCS SMEM tier
llvm.tpu.rdcbreg.tilespmem.base / .wrcbreg.tilespmem.base  -> per-tile TILE_SPMEM tier
```

The scalar `ScalarLoadCircularBuffer` (`scSLDCBREG`) windows the SCS SMEM tier; the TEC vector `TileSpmemLoadCircularBuffer` windows TILE_SPMEM. The `smem.base` and `tilespmem.base` accessor strings are both present in the binary. This resolves the SMEM-address-space question raised by the SCS scalar-memory work: a CBREG is a register holding a *windowed pointer* into SMEM or TILE_SPMEM, selected at base-write time.

---

## Per-Bank CBREG Count

The CBREG file is **16 entries per sequencer bank**. The binding evidence is the 4-bit selector on the TEC vector load/store `Cbreg` field, confirmed across glc and gfc:

| Bank | Count | Selector width | Evidence |
|---|---:|---|---|
| `SC_SCS_CBREGS_*` | 16 | 4-bit | scalar X/Dest field 5-bit (low 4 used); vector Cbreg `& 0xF` |
| `SC_TAC_CBREGS_*` | 16 | 4-bit | same encoder family; TAC present on VF/GL only |
| `SC_TEC_CBREGS_*` | 16 | 4-bit | `…TileSpmemStoreCircularBuffer…CbregField::GetConcatenatedValue` returns `(word12 >> shift) & 0xF`; shift is gen-variant (vfc `>>21` `0x1e9c1d00`, glc `>>22` `0x1eb4f160`, gfc `>>23` `0x1ecca4c0`) — `& 0xF` (16) is invariant |

> **NOTE — the scalar field is 5-bit but the CBREG file is 16, set by the vector path's 4-bit selector.** The scalar-ALU `Dest@10` and `X@21` fields are physically 5-bit (wide enough for 32), but the TEC vector Cbreg field is a hard `& 0xF` (16 entries). The high bit of the scalar 5-bit field is unused for CBREG selection. A reimplementer must size the CBREG file at 16 — the 5-bit scalar slot is not evidence of 32 registers. TAC has CBREGs on VF/GL only (`EmitWriteCbregOp` is instantiated for Tac under vfc/glc but not gfc).

---

## Slot-Reference Binding — How Other Slots Name a CBREG

### Purpose

A CBREG is referenced by index from three places: the scalar-ALU ops above, the Stream-engine indirect-offset source, and the TEC vector-store scatter-add. The last two are what make a CBREG the embedding-table window. This section is the binding surface; the bit layouts of those two slots live on their own pages.

### Stream Indirect-Offset Source

The [Stream gather/scatter](stream-gather-scatter.md) engine selects where its per-element offset/size come from via the proto enum `IndirectOffsetSource`:

```text
IndirectOffsetSource (1-bit):  SREG = 0    CBREG = 1
```

When `CBREG` is selected, the index/offset list is read *through a CBREG window* and `post_update_indirect_offset_circular_buffer` advances the CBREG offset (mod size) per element — the CBREG is the sliding window over the lookup-index buffer. The Stream slot has no CBREG-index field of its own; the windowing CBREG is the one the SCS set up before issuing the stream (the indirect-offset path is bound to the engine's CBREG state, not re-selected per stream).

### TEC Vector-Store Scatter-Add

The embedding-gradient scatter-add writes the accumulated gradient into a CBREG-windowed TILE_SPMEM region. The TEC vector-store family `TileSpmemStoreCircularBuffer[PostUpdate]Add{Bf16,F32,S16,S32}` carries an explicit 4-bit `Cbreg` field that names which of the 16 CBREGs windows the destination tile:

```text
// TileSpmemStoreCircularBufferAdd{Bf16,F32,S16,S32} CbregField, decode word12 (+0x30):
Cbreg = (word12 >> shift) & 0xF;         // which CBREG (16); & 0xF is gen-invariant
//   shift gen-variant: vfc >>21 (0x1e9c1d00), glc >>22 (0x1eb4f160), gfc >>23 (0x1ecca4c0)
```

The full store-slot field set (`{Mask, Stride, Offset, BaseAddress, Cbreg, Source}`) and the gradient-flow detail are on [Stream Gather/Scatter](stream-gather-scatter.md) and [VectorStore Slot](vectorstore-slot.md). The binding point relevant here: the vector store *names* a CBREG by its 4-bit selector, post-updates its offset, and so streams the gradient tile through the buffer one element at a time.

### The Embedding-Lookup Flow (where the references compose)

```text
1. SCS allocates a CBREG and writes its triple:
     wrcbreg.smem.base / wrcbreg.tilespmem.base  <- index-list / tile base
     wrcbreg.size                                <- per-window word count
     wrcbreg.offset                              <- 0
2. Stream gather runs with IndirectOffsetSource = CBREG:
     reads next index through the CBREG window,
     computes HBM[table_base + index*row_stride], DMAs the row -> TILE_SPMEM,
     post-updates the CBREG offset (mod size) so the next gather reads the next index.
3. TEC vector-loads the gathered rows (TileSpmemLoadCircularBuffer[PostUpdate]),
   reduces, and on the backward pass
   TileSpmemStoreCircularBufferPostUpdateAddF32 scatter-adds the gradient back
   into the CBREG-windowed region, advancing the offset per store.
```

The compiler driver `LinearStreamStartOpLowering::rewriteSparseCoreStreamOpToLLVM` (and the `…AddStartOpLowering` scatter-add sibling) takes a `WideOffset` variant that resolves to either an SREG value or a CBREG-sourced offset — the resolution point where a stream becomes CBREG-windowed.

---

## LLVM / MLIR Intrinsic Surface

The compiler-facing ops that drive a CBREG. All names below are present as strings in the binary; the read/write intrinsics carry per-sub-register and per-address-space variants.

| Op-name string | Role | Confidence |
|---|---|---|
| `llvm.tpu.allocate.cbreg` | allocate a CBREG (16 per bank) | CONFIRMED |
| `llvm.tpu.cbreg.add.offset` | `offset += delta` → new offset, wrap mod size | CONFIRMED |
| `llvm.tpu.cbreg.add.offset.in.place` | same, mutates the CBREG | CONFIRMED |
| `llvm.tpu.copy.cbreg` | copy whole CBREG triple → `MoveCbreg` | CONFIRMED |
| `llvm.tpu.rdcbreg.offset` / `.size` | read OFFSET / SIZE sub-register | CONFIRMED |
| `llvm.tpu.rdcbreg.smem.base` / `.tilespmem.base` | read BASE (per address space) | CONFIRMED |
| `llvm.tpu.wrcbreg.offset` / `.size` | write OFFSET / SIZE | CONFIRMED |
| `llvm.tpu.wrcbreg.smem.base` / `.tilespmem.base` | write BASE (per address space) | CONFIRMED |
| `sc_tpu.advance_cb_offset` / `read_cb_offset` | textual MLIR op names for advance / read | CONFIRMED |

MLIR `sc_tpu` op creators back these (`tpu_allocate_cbreg::create` `0x146d6d80`, `tpu_cbreg_add_offset::create` `0x146d7e60` with `OneResult`/`NOperands<2>`, `tpu_cbreg_add_offset_in_place::create` `0x146d7f60`, `tpu_rdcbreg_offset` `0x14734820`, `tpu_wrcbreg_offset`/`_size` `0x14a30fe0`/`0x14a310e0`). The SC ISel matches the reads via `matchReadCbreg<13419u,13417u,13418u>` (`0x13b39200`) and `<13420u,…>` (`0x13b39620`) — intrinsic IDs `0x3469..0x346c` for the `rdcbreg.{offset,size,base}` family.

### Config Knobs

| Knob / string | Meaning |
|---|---|
| `circular_buffer_size` (default `1000`) | XLA emitter window-size *estimate*, not a HW capacity |
| `min_circular_buffer_byte_count` | minimum CB buffer to allocate; `"Invalid min_circular_buffer_byte_count value"` validator |
| `" row size exceeds circular buffer capacity within SparseMapRow Emitter"` | emitter error when a lookup row exceeds the CBREG SIZE |
| `HardwareManagedCircularBufferMinSizeBytes` (`0x10e4a660`) | legacy **BarnaCore** HW-managed CB minimum — distinct from the v5+ SC CBREG |
| `is_circular_buffer` flag | keys the SC tile allocator's VFC HW-bug guard (cannot place a CB in the last TILE_SPMEM entry) |

> **NOTE — `circular_buffer_size=1000` is an emitter estimate, not the SIZE sub-register width.** The default window size the XLA emitter uses to size a buffer is a software knob; the physical CBREG SIZE sub-register bit-width is chip_parts geometry, not in the C++. Do not confuse the 1000-word emitter default with a hardware capacity. `HardwareManagedCircularBufferMinSizeBytes` belongs to the legacy BarnaCore circular buffer, a different mechanism from the v5+ SC CBREG documented here.

---

## Per-Generation Presence

CBREG is a v5+ SparseCore feature. No `Cbreg` ops appear under the `jxc` (Jellyfish) or `pxc` (Pufferfish) namespaces — those generations have no SparseCore CBREG. Among the three SC gens, the deltas are concentrated in the scalar PostUpdate (dropped on gfc, moved to the TEC vector path) and the scalar `MoveCbreg` (added on gfc).

| Mechanism | VF (vfc, Viperfish) | GL (glc, Ghostlite) | GF (gfc, 6acc60406) |
|---|:---:|:---:|:---:|
| CBREG file (16 per SCS/TAC/TEC bank) | yes | yes | yes (no TAC) |
| `ReadCbreg` / `WriteCbreg` / `AddCbreg` (scalar) | yes (`0x36`/`0x35`/`0x33`) | yes | yes |
| `MoveCbreg` (scalar) | — | — | yes (`0x00`+`0x1b`) |
| `ScalarLoad/StoreCircularBuffer` | yes (`0x3f`/`0x3d`) | yes | yes |
| `ScalarLoad/StoreCircularBufferPostUpdate` | yes (`0x3e`/`0x3c`) | yes | — (moved to TEC vector) |
| TEC `TileSpmemLoad/StoreCircularBuffer` | yes | yes | yes |
| TEC `…StoreCircularBufferPostUpdateAdd{dt}` | yes (int/float) | yes (4 dtypes) | yes (4 dtypes) |
| Stream `IndirectOffsetSource = CBREG` | yes | yes | yes |
| `smem.base` + `tilespmem.base` CBREG variants | yes | yes | yes |

The gen split is byte-confirmed by namespace presence: `MoveCbreg` files exist only under `gxc/gfc` (20 instances, `EmitMoveCbregOp` for Scs and Tec bundles only, e.g. `0x13ac9540` / `0x13a73c80`); `EmitWriteCbregOp` is instantiated for Scs/Tac/Tec under vfc/glc but only Scs/Tec under gfc (no TAC under gfc). Zero `Cbreg` files under `jxc`/`pxc`.

---

## Limits and Open Items

| Item | Status | Confidence |
|---|---|---|
| `CbregMetadata` enum {BASE=0, SIZE=1, OFFSET=2} | proto + validator agree, body read | CONFIRMED |
| Scalar-ALU CBREG slot layout (opcode `@154/6`, fields `@138/5`, `@143/6`, `@149/5`) | accessor shifts read; bundle-abs from SCS slot base | CONFIRMED |
| Opcode values Read `0x36` / Write `0x35` / Add `0x33` / Move `0x00`+`0x1b` | `Matches()` immediates read, gen-invariant | CONFIRMED |
| 16-CBREG-per-bank (4-bit selector) | TEC vector Cbreg `& 0xF` (shift gen-variant: vfc/glc/gfc `>>21`/`>>22`/`>>23`) | CONFIRMED |
| Dual address space (SMEM base / TILE_SPMEM base) | both accessor strings present; two `wrcbreg.*.base` variants | CONFIRMED |
| `IndirectOffsetSource` SREG=0 / CBREG=1 + scatter-add Cbreg binding | enum + vector-store Cbreg field | CONFIRMED |
| Per-gen presence (v5+ only; Move gfc-only; PostUpdate not gfc-scalar) | namespace file presence | CONFIRMED |
| The wrap arithmetic `offset = (offset + step) mod size` | inferred from op shape + circular-buffer semantics; no explicit modulo op | HIGH |
| SLD/SST `0x3f/0x3e/0x3d/0x3c` opcode values | opcode-field roster; PostUpdate-bit not fully bit-decoded | HIGH |
| Physical OFFSET/SIZE sub-register bit widths | chip_parts geometry, not in C++ | LOW |
| The PostUpdate `step` source (element-size vs fixed-1 vs operand Stride) | vector form has a 4-bit `Stride`; scalar step source not bit-confirmed | LOW |
| Whether writing SIZE mid-loop is legal vs in-flight PostUpdates (HW ordering) | proto allows `WriteCbreg[SIZE]`; ordering not recovered | LOW |
| `scIMPLICIT_CBREG` operand role (likely the default CB0 for SLD/SST) | inferred, not decoded | LOW |

---

## Related Components

| Name | Relationship |
|---|---|
| `GetCbMetadata<…::CbregMetadata>` (`0x13998fe0`) | validates the metadata immediate {0,1,2}; the BASE/SIZE/OFFSET decode |
| `EmitReadCbregOp` / `EmitWriteCbregOp` / `EmitMoveCbregOp` | per-bundle lowering MCInst → scalar CBREG slot |
| `…ScalarAlu1AddCbregOpcode::Matches` (`0x1eb7b5a0`) | the `0x33` opcode signature read back from the slot |
| TEC `…StoreCircularBufferPostUpdateAdd{dt}` `CbregField` | the 4-bit CBREG selector that binds a scatter-add to a window |
| `BitCopy` (`0x1fa0a900`) | the LE bit-field packer every scalar/vector encoder writes through |

## Cross-References

- [SCS Scalar Opcode Enumeration](scalar-opcode-enum.md) — the `ScalarAlu1` opcode roster where `AddCbreg=0x33`, `ReadCbreg`, `WriteCbreg`, `MoveCbreg` are catalogued.
- [SCS (Scalar) Engine](scs-engine.md) — the 32-byte bundle and the 27-bit `ScalarAlu1` slot template the CBREG-op opcode field (`@154`) sits in.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the `IndirectOffsetSource=CBREG` indirect-offset window and the TEC vector-store scatter-add `Cbreg` field that reference a CBREG.
- [SparseCore Architecture](architecture.md) — the embedding datapath end to end; where CBREG windowing sits in the gather/compute/scatter pipeline.
- [SparseCore Overview](overview.md) — the engine classes, per-gen SC presence, and the host-table → HBM → SC gather path.
- [TEC Engine](tec-engine.md) — the vector engine that issues `TileSpmemLoad/StoreCircularBuffer` against a CBREG-windowed tile.
- [TAC Engine](tac-engine.md) — the VF/GL access engine that also carries the CBREG scalar slot (no TAC CBREG emitter under gfc/6acc60406).
- [VectorStore Slot](vectorstore-slot.md) / [VectorLoad Slot](vectorload-slot.md) — the full TEC vector circular-buffer slot family the `Cbreg` field belongs to.
- [M-Register Predicate Word](m-register-predicate.md) — the predication header that overlays each scalar slot above the CBREG-op opcode field.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
