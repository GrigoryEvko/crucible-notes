# Stream Gather/Scatter

> *Every address, field offset, opcode value, and enum value on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). `.text` VA equals file offset at `0xe63c000`, `.rodata` at `0x84a0000` — both identity-mapped. Addresses are from the **gfc** (6acc60406 / TPU7x) instances unless tagged otherwise; the **glc** (Ghostlite, v6e) and **vfc** (Viperfish, v5) siblings carry the same schema at different addresses. Other versions differ.*

## Abstract

The SparseCore **Stream engine** is the indirect-DMA datapath: the hardware that turns a list of embedding ids into a stream of HBM gather (table → tile) or scatter (tile → table) transactions, optionally with an atomic read-modify-add at the destination. It is one VLIW slot — the **Stream sub-bundle** — encoded from a single proto message, `SparseCoreStream`, that appears identically in the SCS, TAC (vfc/glc only), and TEC bundles. The slot is what the [overview](overview.md)'s host-table → HBM → SC gather path actually executes: where the overview says "TAC/TEC stream slot issues tile-fetch DMA," this page is the bit layout of that slot, the descriptor the compiler builds for it, and the per-element address arithmetic the hardware runs.

`SparseCoreStream` is a shared header plus a four-way `oneof` of *addressing forms*: `LinearStream` (contiguous), `StridedStream` (one stride dimension), `IndirectStream` (the embedding gather/scatter — an id list drives the addresses), and `IndirectVregStream` (id list streamed from a vector register instead of memory; see [IndirectVregStream](indirect-vreg-stream.md)). All four share an identical tail of stream-control fields — sync flag, tile-local stride, filter, length-type, the `StreamOpcode` accumulate mode, the `OffTileMemoryType` pool selector, and two scalar operands. Only the leading operand group differs. The `oneof` discriminator becomes a 6-bit slot opcode: `LinearStream=0x3b`, `StridedStream=0x3a`, `IndirectStream=0x39`, `IndirectVregStream=0x38`.

The reimplementation surface has three layers, each documented here as its own unit: (1) the **slot encoding** — the `SparseCoreStream` proto, its shared header and common control tail, the per-form opcode, and the bit-exact encode/decode map of the `IndirectStream` form; (2) the **indirect-DMA descriptor** — the MLIR `sc_tpu.indirect_stream_start` op with its four operand groups `{Src, Dst, Offset, Sflag}Indices`, how `tpu.enqueue_indirect_dma` lowers into it, and the per-element address formula `addr_i = table_base + offset_list[i] * indirect_list_stride`; and (3) the **granule model** — how `IndirectListType` (word vs row offset), `OffTileMemoryType` (HBM vs HBM_4B), `IndirectLengthType` (fixed vs variable window), and `TileLocalStride` (32 B…2 KB) set the read/write granularity.

For reimplementation, the contract is:

- **One proto, three bundle slots.** `SparseCoreStream` is encoded by `SparseCoreStreamEncoder::Encode` (SCS bundle, 32 B), `SparseCoreTecStreamEncoder::Encode` (TEC bundle, 64 B), and `SparseCoreTacStreamEncoder::Encode` (TAC bundle, 64 B — vfc/glc only). All three take `SparseCoreStream const&`. Field *semantics* are identical across engines; only the slot's bit position inside the bundle differs.
- **The `oneof` is the opcode.** Selecting form `#8…#11` selects 6-bit opcode `0x3b/0x3a/0x39/0x38`. There is no separate opcode field beyond the form discriminator; the encoder writes the opcode at bundle bit 181.
- **`StreamOpcode` is the accumulate mode, orthogonal to the form.** GATHER/SCATTER move rows; the `*_INTEGER_ADD`/`*_FLOAT_ADD` variants do an atomic read-modify-add at the destination. `SCATTER_FLOAT_ADD` (6) into HBM is the embedding-gradient accumulation primitive the TensorCore MXU cannot do.
- **The descriptor is the MLIR op's operand groups.** The id list is the `OffsetIndices` group; the contiguous `Linear`/`Strided` forms drop it. The per-element row-stride multiply is intrinsic to the hardware Stream engine — there is no per-element `imul` in the lowering; the compiler only assigns the operand groups and the `indirect_list_stride` attribute.

| | |
|---|---|
| **Proto message** | `SparseCoreStream` — shared header (`#1…#7`) + 4-form `oneof` (`#8…#11`) |
| **SCS encoder** | `SparseCoreStreamEncoder::Encode` @ `0x1eb9b4c0` (gfc; 32 B bundle) |
| **TEC encoder** | `SparseCoreTecStreamEncoder::Encode` @ `0x1ebe33e0` (gfc; 64 B bundle) |
| **TAC encoder** | `SparseCoreTacStreamEncoder::Encode` @ `0x1e8ee040` (vfc; also glc @ `0x1ea338e0`; absent in gfc) |
| **Form opcodes (6-bit @ bit 181)** | Linear `0x3b` · Strided `0x3a` · Indirect `0x39` · IndirectVreg `0x38` |
| **Accumulate mode** | `StreamOpcode` (3-bit) — GATHER=0…SCATTER_FLOAT_ADD=6 |
| **Off-tile pool** | `OffTileMemoryType` (3-bit) — SPMEM=0, TILE_SPMEM_N=1, HBM=2, HBM_4B=3 |
| **Descriptor op** | `sc_tpu.indirect_stream_start` / `.indirect_stream_add_start` (`AtLeastNOperands<6>`) |
| **Per-element address** | `addr_i = table_base + offset_list[i] * indirect_list_stride` (HW; row-stride scaling intrinsic) |
| **Per-gen presence** | SCS+TEC Stream all 3 SC gens; TAC Stream vfc/glc only (gfc has no TAC) |
| **Confidence** | CONFIRMED (encode-offset / decode-shift / opcode-value double-checked vs decompile) unless a row says otherwise |

---

## The `SparseCoreStream` Proto and the Four Forms

### Purpose

`SparseCoreStream` is the single message that every Stream-engine slot is encoded from. Its job is to describe one address-generation stream — what to read or write, where the base is, how the per-element addresses are computed, and how completion is signalled — independent of which sub-engine carries it. The four `oneof` forms are four *address generators* sharing one control plane; the embedding gather/scatter is exactly the `IndirectStream` form.

### Message Layout

The shared header (`#1…#7`) and the four-form `oneof` (`#8…#11`):

```text
message SparseCoreStream {                          // the Stream sub-bundle slot
  // ---- shared header (all forms) ----
  #1  normal_predication            : SparsecoreNormalPredication  (3-bit)
  #2  normal_predication_inversion  : bool
  #3  rotate_predication            : SparsecoreRotatePredication  (4-bit, 16-entry ring)
  #4  is_rotate_predication         : bool
  #5  latency                       : uint32        // scheduling-only, not emitted
  #6  resource_usage                : map<string,uint32>   // scheduling-only
  #7  bit_width                     : uint32        // scheduling-only
  // ---- oneof addressing form ----
  #8  linear_stream                 : LinearStream         // opcode 0x3b
  #9  strided_stream                : StridedStream        // opcode 0x3a
  #10 indirect_stream               : IndirectStream       // opcode 0x39  ← gather/scatter
  #11 indirect_vreg_stream          : IndirectVregStream   // opcode 0x38
}
```

Fields `#5…#7` (`latency`, `resource_usage`, `bit_width`) are scheduling metadata; the encoder does not emit them into the bundle (they drive the SC instruction scheduler, not the hardware slot). The four forms differ only in their *leading* operands; they all end in the same control tail described below.

### The Common Control Tail

Every form ends in an identical block of stream-control fields. Shown with the `IndirectStream` field names (the numbering differs per form because of differing leading operands):

| Field | Type / width | Role |
|---|---|---|
| `sync_flag_count_type` | `SyncFlagCountType` (1) | completion-count unit: WORD_4B=0 vs DESCRIPTOR=1 |
| `set_done_bit` | bool | raise the stream-done flag on completion |
| `tile_local_stride` | `TileLocalStride` (3) | destination tile write granule (32 B…2 KB / NO_STRIDE) |
| `post_update_circular_buffer` | bool | advance the tile-local circular buffer per element |
| `indirect_list_type` | `IndirectListType` (1) | offset interpretation: WORD_OFFSET=0 vs ROW_OFFSET=1 |
| `indirect_list_stride` | uint32 (6-bit slot) | **the embedding row stride** — the per-element multiplier |
| `indirect_filter_en` | bool | enable id filtering |
| `indirect_filter_mode` | `IndirectFilterMode` (1) | SKIP=0 vs COMPACT=1 |
| `indirect_length_type` | `IndirectLengthType` (1) | FIXED=0 vs VARIABLE=1 lookup length |
| `indirect_offset_source` | `IndirectOffsetSource` (1) | offset/size from SREG=0 vs CBREG window=1 |
| `post_update_indirect_offset_circular_buffer` | bool | advance the CBREG offset per element |
| `trace_en` | bool | enable hardware trace |
| `indirect_mask` | uint32 (4-bit) | lane/id mask |
| `stream_opcode` | `StreamOpcode` (3) | accumulate mode (GATHER…SCATTER_FLOAT_ADD) |
| `gather_scatter_add_is_b16` | bool | bf16 vs f32 add-accumulate dtype |
| `tile_local_memory_type` | `TileLocalMemoryType` (1) | SMEM=0 vs TILE_SPMEM=1 destination |
| `tile_local_stream_type` | `TileLocalStreamType` (1) | LINEAR=0 vs CIRCULAR_BUFFER=1 destination layout |
| `off_tile_memory_type` | `OffTileMemoryType` (3) | the off-tile pool (HBM table / SPMEM / TILE_SPMEM_N) |
| `s0_x` / `s1_x` | uint32 (5-bit SREG each) | scalar operand 0/1 X (offset / size source register) |
| `s0_y` / `s1_y` | `ScalarY` (6-bit each) | scalar operand 0/1 Y (operand-source encoding) |

The per-form leading operands:

| Form | Opcode | Leading operands (before the common tail) |
|---|---|---|
| `LinearStream` | `0x3b` | `off_tile_start_offset[_valid]` — a single linear base |
| `StridedStream` | `0x3a` | `stride_size[_valid]`, `stride_length_and_offset[_valid]` — one stride dim |
| `IndirectStream` | `0x39` | `indirect_offset[_valid]` (5-bit), `indirect_size_and_hbm4b_offset[_valid]` (5-bit) |
| `IndirectVregStream` | `0x38` | `indirect_offsets`, `indirect_access_lengths` (VREG-sourced), `off_tile_start_offset[_valid]` |

> **NOTE —** the `Linear`/`Strided` forms are not part of the gather/scatter path proper; they are contiguous and strided address generators that share the same control plane (filter, length-type, sync flag) for non-indexed transfers — the bulk feed/drain around an embedding lookup. Only `IndirectStream` (and its VREG variant) consume an id list. The shared tail is why a reimplementer can encode all four from one struct: the form discriminator picks the leading operands; everything after is identical.

### Function Map

| Function | Address (gfc) | Role |
|---|---|---|
| `SparseCoreStreamEncoder::Encode` | `0x1eb9b4c0` | encode `SparseCoreStream` into the SCS 32 B bundle slot |
| `SparseCoreStreamDecoder::Decode` | `0x1eb96be0` | decode the SCS Stream slot back to `SparseCoreStream` |
| `SparseCoreTecStreamEncoder::Encode` | `0x1ebe33e0` | encode into the TEC 64 B bundle slot |
| `SparseCoreTecStreamDecoder::Decode` | `0x1ebdd440` | decode the TEC Stream slot |
| `SparseCoreTacStreamEncoder::Encode` | `0x1e8ee040` (vfc) | encode into the TAC 64 B bundle slot (vfc/glc only) |
| `BitCopy` (generic bit-field packer) | `0x1fa0a900` | little-endian field packer `(dst, dst_off, src, src_off, nbits)` |

---

## Stream-Engine Enums

### Purpose

The Stream slot carries eleven small enums plus the 6-bit `ScalarY` operand-source code. Their numeric values are fixed by the proto descriptors in `.rodata` and are part of the wire format — a reimplementer must reproduce them exactly. The full set is catalogued on the [Vector Opcode Enum](vector-opcode-enum.md) and [Scalar Opcode Enum](scalar-opcode-enum.md) pages where they overlap; the Stream-specific ones are here.

### Values

```text
StreamOpcode (3-bit) — the accumulate mode:
  GATHER=0  GATHER_INTEGER_ADD=1  GATHER_FLOAT_ADD=2  RESERVED_0=3
  SCATTER=4 SCATTER_INTEGER_ADD=5 SCATTER_FLOAT_ADD=6 RESERVED_1=7

OffTileMemoryType (3-bit) — the off-tile pool:
  SPMEM=0  TILE_SPMEM_N=1  HBM=2  HBM_4B=3  RESERVED_0..3=4..7

IndirectOffsetSource (1-bit):  SREG=0  CBREG=1
IndirectListType     (1-bit):  WORD_OFFSET=0  ROW_OFFSET=1
IndirectFilterMode   (1-bit):  SKIP=0  COMPACT=1
IndirectLengthType   (1-bit):  FIXED=0  VARIABLE=1
SyncFlagCountType    (1-bit):  WORD_4B=0  DESCRIPTOR=1
TileLocalMemoryType  (1-bit):  SMEM=0  TILE_SPMEM=1
TileLocalStreamType  (1-bit):  LINEAR=0  CIRCULAR_BUFFER=1
TileLocalStride      (3-bit):  32B=0 64B=1 128B=2 256B=3 512B=4 1024B=5 2048B=6 NO_STRIDE=7

SparsecoreNormalPredication (3-bit):  PREG0_IS_1=0 .. PREG6_IS_1=6, ALWAYS=7
SparsecoreRotatePredication (4-bit):  PREG0_IS_1=0 .. PREG15_IS_1=15   (16-entry rotating ring)
ScalarY (6-bit, ~110 values): SREG_0..N + immediate slots (IMM0..3)
                              + hardwired constants (ONE/TWO/FOUR/EIGHT/PI/E/HEX_*…)
```

> **QUIRK — `StreamOpcode` is orthogonal to the slot opcode.** The 6-bit slot opcode (`0x3b/0x3a/0x39/0x38`) selects the *addressing form*; the 3-bit `StreamOpcode` selects *what to do at the destination*. A single `IndirectStream` (slot opcode `0x39`) can be a plain GATHER (`StreamOpcode=0`), a gradient `SCATTER_FLOAT_ADD` (`StreamOpcode=6`), or a `GATHER_FLOAT_ADD` — all the same form. A reimplementer who conflates the two opcode fields will mis-decode every scatter-add. The `gather_scatter_add_is_b16` bit then narrows the add accumulate to bf16 vs f32.

---

## Stream Slot Bit Encoding — `IndirectStream` Form

### Purpose

This is the bit-exact map of the gather/scatter slot, given from both sides of the codec: the **encode** side (bundle-relative bit offsets the encoder writes via `BitCopy`) and the **decode** side (decoded-struct word + shift the `…Field::GetConcatenatedValue()` accessors read). The two describe the same physical slot; both confirm the field widths.

### Encode Side (bundle-relative)

`SparseCoreStreamEncoder::Encode` @ `0x1eb9b4c0` writes into the 32-byte (256-bit) SCS bundle. Bit offsets are the `esi` immediates passed to `BitCopy`:

```text
Shared header (emitted first):
  normal_predication            @ bit 187, width 3
  normal_predication_inversion  @ bit 190, width 1
  rotate_predication            @ bit 187, width 4   (overlaps normal when is_rotate)
  is_rotate_predication         @ bit 191, width 1
Per-form opcode (oneof discriminator selects branch):
  stream-form opcode            @ bit 181, width 6
      LinearStream=0x3b  StridedStream=0x3a  IndirectStream=0x39  IndirectVregStream=0x38
Form-leading operands (IndirectStream):
  indirect_offset_valid                  @ bit 110, width 1
  indirect_offset                        @ bit 105, width 5   (5-bit SREG)
  indirect_size_and_hbm4b_offset_valid   @ bit 104, width 1
  indirect_size_and_hbm4b_offset         @ bit  99, width 5
Common control tail (selected fields):
  off_tile_memory_type      @ bit 111, width 3
  sync_flag_count_type      @ bit 127, width 1
  set_done_bit              @ bit 128, width 1
  post_update_circular_buffer @ bit 131, width 1
  indirect_list_type        @ bit 132, width 1
  indirect_list_stride      @ bit 133, width 4   (slot-narrowed from proto uint32)
  tile_local_stride         @ bit 137, width 3
  indirect_filter_en        @ bit 140, width 1
  indirect_filter_mode      @ bit 141, width 1
  indirect_length_type      @ bit 142, width 1
  s0_x                      @ bit 143, width 6
  s0_y                      @ bit 149, width 5   (encode narrows the 6-bit ScalarY to 5)
  indirect_offset_source    @ bit 155, width 1
  post_update_indirect_offset_cb @ bit 156, width 1
  trace/mask region         @ bit 157, width 3   then bit 160, width 1 / 161, width 1 / 162, width 6
  tile_local_memory_type    @ bit 168, width 1
  tile_local_stream_type    @ bit 169, width 1
  s1_y                      @ bit 170, width 6
  s1_x                      @ bit 176, width 5
```

> **NOTE — the TEC slot is the same field set at a different base.** `SparseCoreTecStreamEncoder::Encode` @ `0x1ebe33e0` encodes from the same `SparseCoreStream` struct into the 64-byte (512-bit) TEC bundle; the form opcodes `0x3b/0x3a/0x39/0x38` are unchanged. The one delta is `IndirectVregStream`'s `indirect_offsets` operand, which the TEC bundle places at bit 322 (width 6) in its high region. The TAC slot (vfc/glc) is again the same field set at the TAC bundle's base.

### Decode Side (decoded-struct relative)

The `IndirectStream` `…Field::GetConcatenatedValue()` accessors are the authoritative field labels. The struct is read as 64-bit words; `+0x10` is QWORD index 2, `+0x18` is QWORD index 3. Each row below was confirmed by reading the accessor body:

| Field | Struct word | Shift | Width | Accessor addr (gfc) |
|---|---|---|---|---|
| `IndirectOffsetSource` | `+0x18` | 0 | 1 | `0x1eb9b320` |
| `PostUpdateIndirectOffsetCircularBuffer` | `+0x18` | 3 | 1 | — |
| `TraceEn` | `+0x18` | 4 | 1 | — |
| `IndirectMask` | `+0x18` | 5 | 4 | — |
| `StreamOpcode` | `+0x18` | 9 | 3 | `0x1eb9b3a0` |
| `GatherScatterAddIsB16` | `+0x18` | 12 | 1 | `0x1eb9b3c0` |
| `TileLocalMemoryType` | `+0x18` | 13 | 1 | — |
| `TileLocalStreamType` | `+0x18` | 14 | 1 | — |
| `S1Y` | `+0x18` | 15 | 6 | — |
| `S1X` | `+0x18` | 21 | 5 | — |
| `SyncFlagCountType` | `+0x18` | 27 | 1 | — |
| `SetDoneBit` | `+0x18` | 28 | 1 | — |
| `TileLocalStride` | `+0x18` | 29 | 3 | `0x1eb9b240` |
| `PostUpdateCircularBuffer` | `+0x18` | 32 | 1 | — |
| `IndirectListType` | `+0x18` | 33 | 1 | `0x1eb9b280` |
| `IndirectListStride` | `+0x18` | 34 | 6 | `0x1eb9b2a0` |
| `IndirectFilterEn` | `+0x18` | 40 | 1 | — |
| `IndirectFilterMode` | `+0x18` | 41 | 1 | `0x1eb9b2e0` |
| `S0Y` | `+0x18` | 42 | 6 | — |
| `IndirectSizeAndHbm4bOffset` | `+0x10` | 35 | 5 | — |
| `IndirectSizeAndHbm4bOffsetValid` | `+0x10` | 40 | 1 | — |
| `IndirectOffset` | `+0x10` | 41 | 5 | `0x1eb9b1a0` |
| `IndirectOffsetValid` | `+0x10` | 46 | 1 | `0x1eb9b180` |
| `OffTileMemoryType` | `+0x10` | 47 | 3 | `0x1eb9b420` |
| `IndirectLengthType` | `+0x10` | 63 | 1 | `0x1eb9b300` |
| `S0X` | `+0x1e` (u16) | 0 | 5 | `0x1eb9b440` |

The form opcode itself is a 6-bit field at `+0x18` bit 53 (a contiguous part of the decode word). The opcode matcher `SparseCoreStreamIndirectStreamOpcode::Matches` @ `0x1eb9aaa0` reads `(*((QWORD*)this + 3) & 0x7E0000000000000) == 0x720000000000000`, i.e. bits 53–58 == `0x39`.

> **GOTCHA — `indirect_list_stride` is 6 bits in the decoded struct but 4 bits in the SCS encode slot.** The decode accessor masks `& 0x3F` (6-bit) at `+0x18>>34`; the SCS `BitCopy` writes only 4 bits at bundle bit 133. The proto field is `uint32`. The narrowest physical width seen is 4 bits in the SCS bundle. A reimplementer must treat the stride as range-limited at encode time, not assume the full proto `uint32` reaches hardware. The exact physical counter width per generation was not pinned (LOW — see Limits).

---

## The Indirect-DMA Descriptor

### Purpose

The descriptor is how an embedding-id list becomes HBM gather addresses. It is not a separate in-memory struct the compiler builds; it is the operand structure of the MLIR `sc_tpu.indirect_stream_start` op, which lowers to the Stream slot fields above. The framework op `tpu.enqueue_indirect_dma` (TensorCore side) lowers into it, the tiled-memref index expansion flattens the operand groups, and at hardware issue the Stream engine walks the offset list element by element.

### The SC-Side Op and Its Operand Groups

```text
sc_tpu.indirect_stream_start / sc_tpu.indirect_stream_add_start
  Traits: ZeroResults, AtLeastNOperands<6>, AttrSizedOperandSegments
  Operand groups (MutableOperandRange getters):
    getSrcIndicesMutable()    @ 0x145cd9a0  — HBM table-base memref + indices (the table)
    getDstIndicesMutable()    @ 0x145cda60  — TILE_SPMEM destination tile memref + indices
    getOffsetIndicesMutable() @ 0x145cdc40  — the EMBEDDING-ID / offset list memref
    getSflagIndicesMutable()  @ 0x145cdb40  — completion sync-flag operand(s)
  Attributes (getters):
    getIndirectListType()         @ 0x145cf400   — WORD_OFFSET vs ROW_OFFSET
    getHbm4b()                    @ 0x145cf360   — 4-byte-granule HBM addressing
    getTileLocalLengthPerStride() @ 0x145cf3a0   — destination granule per stride
    getUpd()                      @ 0x145cf340   — post-update (advance circular buffer)
    getEnableTrace()              @ 0x145cf380   — hardware trace enable
```

The contiguous siblings `LinearStreamStartOp` / `StridedStreamStartOp` have **no** `OffsetIndices` group — they carry no per-element id list. The `_add_start` variant selects an accumulate `StreamOpcode` (the `*_FLOAT_ADD` / `*_INTEGER_ADD` modes).

### Lowering and Index Expansion

```c
// tpu.enqueue_indirect_dma (TC side; AtLeastNOperands<4>, getAdd() = atomic-add mode)
//   lowered by mlir::tpu::LowerMemrefToMlo::lowerEnqueueIndirectDma
//   shapes verified by getVerifiedIndirectDmaShapes
//   target SC partition picked by getRemoteDeviceAndSparseCoreIds
//   -> sc_tpu.indirect_stream_start

function expandSCStreamStart<IndirectStreamStartOp>(op):   // 0x134ead00
    // ExpandTiledMemRefsPass flattens every tiled-layout memref index
    for group in {Src, Dst, Offset, Sflag}Indices:
        expanded = expandTiledIndices(group, tiles, loc, builder)  // 0x134e1f20
                                            // tiled memref index -> flat word offset
        group.assign(expanded)                                     // MutableOperandRange::assign
```

`tpu.wait_indirect_dma` (`NOperands<3>`) is the completion-side framework op. `ExpandTiledMemRefsPass::addPattern<IndirectStreamStartOp>` registers the expansion (confirmed in the decompile). The `_add_start`, `IndirectVectorStreamStartOp`, and `StridedStreamStartOp` expansions are sibling instantiations of `expandSCStreamStart<…>`.

> **NOTE — the descriptor carries indices, not addresses; the multiply is hardware.** The lowering only reassigns operand groups and sets the `indirect_list_stride` attribute. There is no per-element `imul` emitted. The row-stride scaling (`offset_list[i] * indirect_list_stride`) is intrinsic to the Stream engine's per-element address generator. This is HIGH-confidence (inferred from field names + absence of a per-element multiply in the lowering), not bit-confirmed from a hardware register description.

---

## The Gather/Scatter Granule Model

### Purpose

Three independent knobs set how much data each indirect element touches and where it lands. They are what a reimplementer tunes to match an embedding table's row width, its HBM alignment, and its variable-length-lookup behavior.

### The Per-Element Address Formula

```c
// HW per-element gather/scatter, driven by the slot fields:
for i in 0 .. num_indices:
    if indirect_filter_en and filtered(offset_list[i]):
        continue                                 // SKIP, or COMPACT out the slot
    if indirect_list_type == ROW_OFFSET:
        addr_i = table_base + offset_list[i] * indirect_list_stride   // offset is a ROW index
    else: // WORD_OFFSET
        addr_i = table_base + offset_list[i]                          // offset is a byte/word offset
    row = read_or_write(addr_i, granule = tile_local_stride)
    if stream_opcode in {GATHER_*ADD, SCATTER_*ADD}:
        atomic_add(addr_i, row, dtype = gather_scatter_add_is_b16 ? bf16 : f32)
    if indirect_offset_source == CBREG and post_update_indirect_offset_cb:
        advance_cbreg_offset_mod_size()          // sliding embedding-table window
```

### The Three Granule Knobs

| Knob | Field | Values | What it sizes |
|---|---|---|---|
| **Offset interpretation** | `IndirectListType` | WORD_OFFSET=0 / ROW_OFFSET=1 | whether `offset_list[i]` is a byte/word offset or a row index scaled by `indirect_list_stride` |
| **HBM addressing granule** | `OffTileMemoryType` | HBM=2 / HBM_4B=3 | HBM_4B selects 4-byte-granule HBM addressing (the `indirect_size_and_hbm4b_offset` operand carries the sub-word offset) |
| **Destination write granule** | `TileLocalStride` | 32 B…2 KB / NO_STRIDE | the per-element write stride into the destination tile |
| **Lookup length** | `IndirectLengthType` | FIXED=0 / VARIABLE=1 | FIXED: every lookup pulls the same row count; VARIABLE: per-row variable length (the variable-window lookup) |

The row stride `indirect_list_stride` (the embedding row width) is the per-element multiplier under `ROW_OFFSET`. Under `WORD_OFFSET` the offset is already a word/byte offset and the stride does not scale it.

> **QUIRK — `indirect_size_and_hbm4b_offset` is a shared operand.** The same 5-bit `IndirectStream` operand carries the lookup *size* in the normal HBM case and the sub-word *offset* in the HBM_4B case. The interpretation is gated by `OffTileMemoryType==HBM_4B`. The field is named for both roles precisely because the hardware repurposes it. A reimplementer must branch on `OffTileMemoryType` when packing this operand.

### Offset Source and the Sliding Window

`IndirectOffsetSource` chooses where the per-element offset/size come from:

- **SREG (0):** offset/size are read from the scalar registers named by `s0_x`/`s1_x` (5-bit SREG selectors) with `s0_y`/`s1_y` (6-bit `ScalarY`) operand-source codes. Static, per-stream.
- **CBREG (1):** offset/size come from a circular-buffer register window; `post_update_indirect_offset_circular_buffer` advances the CBREG offset per element, modulo the window size. This is the sliding embedding-table window — the id list is consumed through a CBREG ring rather than a flat SREG. See [CBREG](cbreg.md) for the circular-buffer register layout.

### Filtering and Bounds

`indirect_filter_en` + `indirect_filter_mode` gate which ids fire: **SKIP** drops a filtered id (leaving a gap), **COMPACT** removes it (compacting the output). The filter *value* is set out of band by the SCS op `sc_tpu.set_indirect_filter_value` (`SetIndirectFilterValueOp`, lowered via the `SparseCoreScalarAlu_SetIndirectFilterValue` scalar op). The id-range bounds checks are inserted by `AddStreamBoundChecksPass::runOnOperation` @ `0x134c3fa0` (its `AddStreamBufferChecks` has Linear/Strided/Indirect overloads) and `IndirectStreamBoundsCheckPass`; an out-of-range id triggers the "does not match number of TPU embedding rows" diagnostic.

---

## The Scatter-Add Slot (Embedding-Gradient Accumulation)

### Purpose

The backward pass needs to add a gradient slice into an embedding row. Two paths exist: (a) accumulate into a CBREG-windowed TILE_SPMEM region with a TEC vector-store op, or (b) issue a Stream `SCATTER_FLOAT_ADD` directly into the HBM embedding row. Path (b) is the in-HBM atomic FP-add the [overview](overview.md) calls the keystone primitive.

### The TEC Vector-Store Add Slot

`TileSpmemStoreCircularBufferPostUpdateAddF32` (gfc) — the TILE_SPMEM circular-buffer post-update add store. Decoded-struct word `+0x30` (DWORD index 12 / QWORD index 6), confirmed by reading the accessor bodies:

| Field | Shift | Width | Accessor addr (gfc) | Meaning |
|---|---|---|---|---|
| `Mask` | 8 | 5 | `0x1eccac40` | lane predicate / vmask |
| `Stride` | 13 | 4 | `0x1eccac20` | per-element address stride |
| `Offset` | 17 | 3 | `0x1eccac00` | within-window offset |
| `BaseAddress` | 20 | 3 | `0x1eccabc0` | explicit base (alternative to CBREG base) |
| `Cbreg` | 23 | 4 | `0x1eccabe0` | which CBREG (→ 16 CBREGs) |
| `Source` | 27 | 6 | `0x1eccaba0` | source VREG being scatter-added |

The op exists per accumulate dtype — `Add{Bf16,F32,S16,S32}` — across the store-form family (plain, `CircularBuffer`, `CircularBufferPostUpdate`, `Indexed`, …). The `Cbreg` 4-bit width (16 CBREGs) at slot `+0x30` matches the vfc (Viperfish) layout, confirming it for gfc (6acc60406). See [VectorStore Slot](vectorstore-slot.md) for the full store-slot family and [VectorLoad Slot](vectorload-slot.md) for the gather-load counterpart.

### Gradient Flow

```text
TEC vector-loads gathered rows (TileSpmemLoadCircularBuffer[PostUpdate])
  -> applies optimizer math (stochastic round-to-bf16 / FP8 pack on gfc TEC)
  -> EITHER (a) TileSpmemStoreCircularBufferPostUpdateAdd{dt}   (CBREG-windowed TILE_SPMEM)
     OR     (b) Stream SCATTER_FLOAT_ADD into HBM               (StreamOpcode=6, OffTileMemoryType=HBM)
```

Path (b) is the atomic FP-add directly into HBM (`StreamOpcode=6`, `gather_scatter_add_is_b16` selecting the accumulate dtype) — the operation the TensorCore's systolic array cannot perform.

---

## The SCS / TAC / TEC Engine Handshake

### Purpose

The Stream slot lives in all three engine bundles, but a given gather/scatter is *issued* by one engine. Which one is per-generation, and the engines coordinate through SMEM-published parameters and cross-engine sync primitives. This is the slot-level view of the pipeline the [architecture](architecture.md) page describes end to end.

### Forward Lookup

```text
1. SCS computes per-lookup addressing params (table base, offset-list base, row
   stride, window CBREG triple) and publishes them to SMEM.
2. ACCESS engine issues the indirect gather Stream op:
     VF/GL:    TAC bundle Stream slot  (SparseCoreTacStreamEncoder @ 0x1e8ee040 vfc)
     gfc:      TEC bundle Stream slot  (no TAC; SparseCoreTecStreamEncoder @ 0x1ebe33e0)
               gated by TileWaitScsSmemOp  (TEC waits for SCS's SMEM value)
   The gather reads offset_list[i] (SREG or CBREG window),
   computes HBM[table_base + offset*stride], DMAs the row -> TILE_SPMEM,
   and (CBREG case) post-updates the CBREG offset mod size.
3. EXECUTE engine (TEC) vector-loads from TILE_SPMEM, reduces (sum / weighted /
   max via the VectorExtended scan/segscan slots), emits the result tile.
4. Result tile DMA'd HBM->VMEM for MXU consumption; SC raises an SFLAG;
   the TC's tpu.sem_wait advances.
```

### Sync Primitives

```text
Cross-sub-engine (xla::tpu::sparse_core::collective::OffloadFactory):
  SyncScsWithTec(builder, value, CoreKind)        @ 0x133e9260
  SyncTecWithScs(builder, v1, v2, CoreKind)       @ 0x133e8fe0
  CoreKind {kScs, kTec, kTac(VF/GL)}
gfc TAC-replacement:
  TileWaitScsSmemOp / sc_tpu.tile_wait_scs_smem   (TEC waits until SCS writes a
    designated SMEM value, then proceeds with fetch+compute — collapses the
    SCS<->TAC<->TEC three-way sync to a single SCS<->TEC boundary)
Cross-CORE (SC<->TC):
  SFLAG memory (sflag / sflag_scs / sflag_tile), tpu.sem_wait / sem_signal /
  fetch_and_add_sync; double-buffered (AllocateSflag(..., 2)).
Tile-task program model:
  sc_tpu.tile_task (Access+Execute regions) launched via launch_tile_task /
  prefetch_tile_task / tile_task_wait.
```

> **NOTE — on gfc (6acc60406) the gather migrates from TAC to TEC.** With TAC removed (zero `SparseCoreTacStream*` functions in the gfc namespace, against 68 in vfc / 71 in glc), the access engine's role — issuing the indirect gather Stream op — folds into the TEC, synchronized through `TileWaitScsSmemOp` instead of the three-way SCS/TAC/TEC handshake. The Stream slot's *encoding* is unchanged; only the issuing engine and the sync topology differ. Which engine carries a given Stream op is decided in `LowerMemrefToMlo::getSequencerType`; the exact per-shape decision was not bit-traced (see [getSequencerType](getsequencertype.md)).

---

## Per-Generation Presence

| Mechanism | VF (vfc, Viperfish v5) | GL (glc, Ghostlite v6e) | GF (gfc, 6acc60406 / TPU7x) |
|---|:---:|:---:|:---:|
| SCS-bundle Stream slot (`SparseCoreStream`) | yes | yes | yes |
| TAC-bundle Stream slot (`TacStream`) | yes | yes | **— (no TAC)** |
| TEC-bundle Stream slot (`TecStream`) | yes | yes | yes |
| `LinearStream` (opcode `0x3b`) | yes | yes | yes |
| `StridedStream` (opcode `0x3a`) | yes | yes | yes |
| `IndirectStream` (opcode `0x39`) gather/scatter | yes | yes | yes |
| `IndirectVregStream` (opcode `0x38`) | yes | yes | yes |
| `StreamOpcode` 8-value set (GATHER…SCATTER) | yes | yes | yes |
| `IndirectOffsetSource` SREG/CBREG | yes | yes | yes |
| `OffTileMemoryType` {SPMEM, TILE_SPMEM, HBM, HBM_4B} | yes | yes | yes |
| Indirect gather issued from ACCESS engine | TAC | TAC | TEC |
| Scatter-add CB store `…PostUpdateAdd{dt}` | int/float | 4 dtypes | 4 dtypes |

The `TacStream` function count is the discriminator: **gfc = 0**, **vfc = 68**, **glc = 71** decompiled `SparseCoreTacStream*` functions. No `SparseCoreStream` schema appears under the `jxc` (Jellyfish) or `pxc` (Pufferfish) namespaces — those generations have no SparseCore.

---

## Limits and Open Items

| Item | Status |
|---|---|
| `SparseCoreStream` proto + 4-form `oneof`, all field numbers/names/types | decoded from descriptor; 3 copies (vfc/glc/gfc) |
| All Stream enums with numeric values | proto-descriptor confirmed |
| Per-form opcodes `0x3b/0x3a/0x39/0x38` @ bit 181 | bit-exact from SCS + TEC encoders |
| `IndirectStream` encode offsets + decode shifts | both sides agree; accessor bodies read |
| Scatter-add slot `{Mask,Stride,Offset,BaseAddress,Cbreg,Source}` @ `+0x30` | accessor bodies read |
| Indirect-DMA descriptor operand groups + attribute getters | op getters located in decompile |
| Per-element address formula (`table_base + offset*stride`, HW multiply) | inferred from field names; no per-element `imul` in lowering |
| Which engine issues a given Stream op per gen | `getSequencerType` located, not bit-traced |
| Physical HW width of `indirect_list_stride` / SREG operands | proto `uint32`; slot encodes 4-bit @ SCS, 6-bit decode; HW counter width unknown |
| `IndirectVregStream` VREG-read micro-op datapath | field positions decoded; VREG source not traced |
| `ROW_OFFSET` vs logical-replica row sharding (per-shard vs global row) | not resolved |
| Absolute bit base of the SCS Stream slot within the 256-bit bundle | opcode @ bit 181 known; full SCS slot-base partition not cross-checked |

---

## Cross-References

- [SparseCore Overview](overview.md) — the host-table → HBM → SC gather data path this slot executes; the keystone in-HBM atomic add.
- [SparseCore Architecture](architecture.md) — engine roles and the embedding datapath in full depth.
- [IndirectVregStream](indirect-vreg-stream.md) — the `0x38` form: id list streamed from a vector register instead of a memory list.
- [VectorLoad Slot](vectorload-slot.md) — the TILE_SPMEM gather-load counterpart that consumes the streamed rows.
- [VectorStore Slot](vectorstore-slot.md) — the store-slot family that the scatter-add `…PostUpdateAdd{dt}` belongs to.
- [CBREG Circular-Buffer Register](cbreg.md) — the sliding-window register the CBREG offset source advances.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC selection that picks the issuing engine.
- [Vector Opcode Enum](vector-opcode-enum.md) / [Scalar Opcode Enum](scalar-opcode-enum.md) — the surrounding opcode rosters.
- [GetSparseCoreConfig](getsparsecoreconfig.md) — the offload op-type configuration source.
- [SparseCore vs Neuron MatmultSparse](sparsecore-vs-neuron-matmultsparse.md) — cross-vendor comparison of the indirect-gather primitive.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore pointers & DMA — [back to index](../index.md)
