# Memory-Store Slot

> *Every address, bit offset, opcode value, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). `.text`/`.rodata` are mapped VA == file offset. Other wheel versions differ.*

## Abstract

The memory-store slot is the bundle slot that moves a compute register **out** to on-chip memory inside a single VLIW issue word — the write-side mirror of the [Memory-Load Slot](slot-memory-load.md). It is distinct from intra-chip DMA: the store *slot* moves one vector register into VMEM (or one scalar register into SMEM) in one bundle cycle, whereas DMA moves tier→tier blocks via descriptors. Like the load slot it carries no tier-selector bit — the destination tier is a function of `(slot, sub-opcode)`.

A store slot is a discriminated union whose sub-opcode selects both the destination tier and the addressing mode. The per-gen `<Bundle><Slot>StoreEncoder::Encode(<msg> const&, absl::Span<uint8_t>)` is the canonical byte codec, and on Pufferfish and all V5+ generations each field is written with one call to the universal bit packer `BitCopy(dst, bit_offset, src, 0, width)` — the literal `bit_offset` argument *is* the absolute bundle bit, so the store-slot field map is read off the encoder disassembly with no shift arithmetic to invert. The store-data source register is always written first; the sub-opcode discriminator (read from the proto at `+0x50`/`+0x4c`) then selects which addressing fields follow.

Two structural facts dominate the per-generation story. First, the store slot is the read-and-write twin of the load slot — same dedicated bundle slot, same paired Encoder/Decoder in the same `TensorCoreCodecBase` template, same addressing-mode taxonomy (base+offset, base-register NoOffset, strided, indexed/scatter) — but with three write-only additions: a sublane/vmask write-enable, the SparseCore reduce-add (read-modify-write) family, and the vector-store fence ordering barrier. Second, the per-gen deltas are concrete: the bundle grows 41 → 51 → 64 bytes; Pufferfish adds CMEM-store and 8 hardware vmask registers; Viperfish shrinks the store-data field 5 → 4 bits while growing base/offset 5 → 6 bits and adds the SparseCore TEC tile-SPMEM store with reduce-add; Ghostlite doubles the write ports 1 → 2; Trillium moves the store-fence into the misc slot and reaches a 34-sub-op SparseCore store family with atomic SMEM stores.

For reimplementation, the contract is: the `LloOpcodeIsVectorStore` opcode window, the per-gen store Encoder/Decoder address table, the `BitCopy` absolute-bit field maps for PF/VF/GFC, the slot-selects-tier model, the two orthogonal masking mechanisms (bundle predicate vs vmask), and the per-gen deltas.

| | |
|---|---|
| **Slot role** | move one register's worth of data into VMEM/CMEM/SPMEM (vector) or SMEM (scalar) |
| **Tier select** | by `(slot, sub-opcode)` (no tier bit) |
| **PXC store encoder** | `pxc::isa::TensorCoreVectorStoreEncoder::Encode` @ `0x1ee3b440` (51-B bundle) |
| **VXC store encoder** | `vxc::isa::TensorCoreVectorStoreEncoder::Encode` @ `0x1f01ff60` (64-B bundle) |
| **GFC store encoder** | `gxc::gfc::isa::TensorCoreVectorStoreEncoder::Encode` @ `0x1fa08920` (64-B bundle) |
| **JF store encoder** | `jellyfish::isa::EncoderJf::EncodeVectorStoreInstruction` @ `0x1e868c40` (41-B, monolithic) |
| **Bit primitive** | `BitCopy(dst, abs_bit, src, 0, width)` — `abs_bit` == absolute bundle bit (PF/V5+) |
| **Opcode classifier** | `xla::jellyfish::LloOpcodeIsVectorStore` @ `0x14024920` → window `{63,64,65,68,69,70}` + `460` |
| **Store-data field** | 5-bit on JF/PF, **4-bit on VF/GL/GFC**; base/offset 5-bit → **6-bit** at v5 |
| **Write ports** | `MaxVectorStoreSlots` = 1 on JF/PF/VF, **2 on GL/GFC** |
| **Confidence** | CONFIRMED (byte-anchored) unless a cell says otherwise |

---

## The Store Op List (LLO Opcodes)

The store family in the `LloOpcode` enum (internal `k`-prefixed names; proto `LloOpcodeProto` `OPCODE_`-prefixed names):

| LloOpcode internal | LloOpcodeProto name | Destination tier | Slot | Confidence |
|--------------------|---------------------|------------------|------|------------|
| `kVectorStore` | `OPCODE_VECTOR_STORE` | VMEM | `VECTOR_STORE` | CONFIRMED |
| `kVectorStoreMasked` | `OPCODE_VECTOR_STORE_MASKED` | VMEM (masked) | `VECTOR_STORE` | CONFIRMED |
| `kVectorStoreIndexed` | `OPCODE_VECTOR_STORE_INDEXED` | VMEM (scatter) | `VECTOR_STORE` | CONFIRMED |
| `kVectorStoreIndexedMasked` | `OPCODE_VECTOR_STORE_INDEXED_MASKED` | VMEM (scatter+mask) | `VECTOR_STORE` | CONFIRMED |
| `kVectorStoreSublaneShuffle` | `OPCODE_VECTOR_STORE_SUBLANE_SHUFFLE` | VMEM (shuffled) | `VECTOR_STORE` | CONFIRMED |
| `kVectorStoreEvenOddSublanes` | `OPCODE_VECTOR_STORE_EVEN_ODD_SUBLANES` | VMEM (even/odd) | `VECTOR_STORE` | CONFIRMED |
| `kVectorStoreFence` | `OPCODE_VECTOR_STORE_FENCE` | (ordering barrier) | `VECTOR_MISC` (GFC) | CONFIRMED |
| `kVectorCmemStore` | `OPCODE_VECTOR_CMEM_STORE` | CMEM | `VECTOR_STORE` | CONFIRMED |
| `kVectorCmemStorePseudo` | `OPCODE_VECTOR_CMEM_STORE_PSEUDO` | CMEM (pseudo) | `VECTOR_STORE` | CONFIRMED |
| `kBarnaCoreVectorStore` | `OPCODE_BARNA_CORE_VECTOR_STORE` | BarnaCore VMEM | (BCS channel) | CONFIRMED |
| `kScalarStore` | `OPCODE_SCALAR_STORE` | SMEM | `SCALAR_0/1` | CONFIRMED |

The classifier `xla::jellyfish::LloOpcodeIsVectorStore` (@ `0x14024920`) is byte-exact: it returns true when `(opcode - 63) <= 7 && _bittest(0xE7, opcode - 63)` **or** `opcode == 460`. `0xE7 = 0b1110_0111` selects offsets `{0,1,2,5,6,7}`, i.e. internal opcodes `{63,64,65,68,69,70}` plus `460`. Scalar-store, CMEM-store and BarnaCore-store are classified by separate predicates.

> **NOTE —** the vector-store fence is **not** a store; it is an ordering barrier. On Trillium it is encoded in the **VectorMisc** slot (`gxc::gfc::isa::TensorCoreVectorMisc0Compact::vector_store_fence` accessor), not the store slot. The bundle packer's `RequiresVectorStoreFence` (@ `0x14020900`) gates it behind `Target::SupportsVectorStoreFence()`. See [Per-Gen Deltas](#per-gen-deltas).

---

## Store-Slot Encoder/Decoder Address Table

Each gen has a `<Bundle><Slot>StoreEncoder::Encode` and a matching `<…>StoreDecoder::Decode`. The decoder is the symmetric inverse `BitExtract` of the encoder.

### TensorCore vector-store slot

| Gen | Namespace | Encode @ | Decode @ | Confidence |
|-----|-----------|----------|----------|------------|
| Jellyfish | `jellyfish::isa::EncoderJf::EncodeVectorStoreInstruction` | `0x1e868c40` | (paired in `DecoderJf`) | CONFIRMED |
| Pufferfish | `pxc::isa::TensorCoreVectorStore{En,De}coder` | `0x1ee3b440` | `0x1ee2dae0` | CONFIRMED |
| Viperfish | `vxc::isa::TensorCoreVectorStore{En,De}coder` | `0x1f01ff60` | `0x1f01a560` | CONFIRMED |
| Ghostlite | `gxc::glc::isa::TensorCoreVectorStore{En,De}coder` | `0x1f3c3200` | `0x1f3bd780` | CONFIRMED |
| Trillium | `gxc::gfc::isa::TensorCoreVectorStore{En,De}coder` | `0x1fa08920` | `0x1fa02f00` | CONFIRMED |

Jellyfish's TensorCore store is not a standalone codec class; it is the monolithic `EncoderJf::EncodeVectorStoreInstruction(VectorStoreInstruction const&, Bundle const&)` (@ `0x1e868c40`) that fills the slot bits inline.

### SparseCore TEC tile-SPMEM store slot (V5+ only)

| Gen | Namespace | Encode @ | Decode @ | Confidence |
|-----|-----------|----------|----------|------------|
| Viperfish | `vxc::vfc::isa::SparseCoreTecVectorStore{En,De}coder` | `0x1e9c2760` | `0x1e9bbf40` | CONFIRMED |
| Ghostlite | `gxc::glc::isa::SparseCoreTecVectorStore{En,De}coder` | `0x1eb50ac0` | `0x1eb42300` | CONFIRMED |
| Trillium | `gxc::gfc::isa::SparseCoreTecVectorStore{En,De}coder` | `0x1eccbe20` | `0x1ecbd7a0` | CONFIRMED |

There is no Jellyfish/Pufferfish SparseCore TEC store — the SparseCore TEC sequencer exists only from Viperfish onward. The **BarnaCore** channel vector-store (Pufferfish only) is `pxc::pfc::isa::BarnaCoreChannelVectorStore{En,De}coder` (Encode `0x1e8c5640`, Decode `0x1e8c4d40`).

### Scalar-store slot (SREG → SMEM)

Routed through the scalar-slot encoder, not the vector-store slot. Jellyfish: `ScalarStoreOperands` proto, emitted by `EncoderJf::EncodeScalarInstruction` (@ `0x1e862060`). Pufferfish: `TensorCoreScalar1_ScalarStoreSmemAbsolute` via `ScalarYEncode` (@ `0x1c471c60`) + `SetImmOrDie` (@ `0x1c550660`); BarnaCore-sequencer variant via `0x1c471fc0`/`0x1c550e00`. Viperfish/Ghostlite: `…ScalarAlu_ScalarStoreXToSmemY`. Trillium adds `…ScalarStoreXToSmemSumDestAndY` (atomic add), `…ScalarStoreCircularBuffer`, and `SmemFetchAndAdd` (via `EmitFetchAndAddOp` @ `0x13a3a300`).

---

## Store Slot Position in the Bundle

The store slot is one fixed slot in the per-gen VLIW bundle; the slot order is the per-gen `TensorCoreCodecBase<…>` template-arg order:

```text
ScalarAlu0 / ScalarAlu1 / [Predicates] / Immediates / VectorScalar /
Dma / VectorAlu0 / VectorAlu1 / [VectorAlu2 / VectorAlu3] /
**VectorStore** / VectorLoad0 / [VectorLoad1] / VectorMisc /
VectorExtended0 / [VectorExtended1] / VectorResult0 / [VectorResult1]
```

(Pufferfish carries `VectorAlu0/1 + VectorStore + VectorLoad + CmemLoad + …`.) The slot-enumeration constants name it: `isa::SLOT_VECTOR_STORE` (TC), `isa::SLOT_SCALAR_0/1` and `SLOT_SCALAR_ALU_0/1` (scalar stores), `isa::SLOT_VECTOR_MISC` (overlay target / store fence), `llvm::TPU::SparseCoreMCSlot::SLOT_VST` and `SLOT_SST` (SparseCore vector/scalar store). Bundle widths: Jellyfish/Dragonfish 41 B, Pufferfish 51 B, V5+ TensorCore 64 B; SparseCore SCS 32 B, TEC 64 B.

On Pufferfish the `vector_store` slot occupies absolute bundle bits **142..166** (see [Pufferfish 51B Bundle](bundle-pf-51b.md)), disjoint from the `vector_load` slot at 119..140 — so a load and a store can co-exist in one bundle. The constraint `"SCALAR_STORE_ABSOLUTE not allowed in slot 0."` forces a scalar-store absolute on Jellyfish/Pufferfish into `SCALAR_1`, never `SCALAR_0`. The accounting predicate `store_and_misc_slots == module.target().MaxVectorStoreSlots()` ties the number of store-bearing slots to the per-gen write-port count.

---

## Bit-Field Layout (`BitCopy` Field Maps)

Each per-slot Encoder fills the slot via `BitCopy(dst, abs_bit, src, 0, width)`; the store-data source register is written first, then the sub-opcode discriminator selects the addressing fields. The maps below are harvested directly from the `BitCopy(a3, …)` call sites in the decompiled encoders — the second argument is the absolute bundle bit.

### Pufferfish (PXC) `TensorCoreVectorStore` — Encode @ `0x1ee3b440` (51-B bundle)

Byte-exact from the encoder body (`BitCopy(a3, 162, …, 5)` etc.):

| Field | abs bit | width | meaning | Confidence |
|-------|--------:|------:|---------|------------|
| source vreg | 162 | 5 | store-data vreg (proto `+28`); = 31 ⇒ NOOP (`kNeverExecute`) | CONFIRMED |
| sub-opcode | 157 | 5 | `VmemStore`=0 / `CmemStore`≠0 discriminator | CONFIRMED |
| base address / Vmem offset | 152 | 5 | VregNumber base | CONFIRMED |
| offset | 149 | 3 | immediate-offset slot index | CONFIRMED |
| stride | 147 | 2 | stride select | CONFIRMED |
| sublane-mask / vmask | 145 | 2 | sublane/vmask select | CONFIRMED |
| (CMEM full-address words) | 304 / 288 / 272 | 16 | CMEM 16-bit immediate address (CmemStore only) | CONFIRMED |
| (CMEM address regs) | 251 / 246 / 241 | 5 | shared Y-register selectors | CONFIRMED |

The PF store slot region (abs 142..166) matches the [Pufferfish 51B Bundle](bundle-pf-51b.md) page exactly. Sub-opcode dispatch (proto `+0x50`): `0`=Noop, `5`=NoopAlt, `6`=CmemStore, `7`=CmemStoreNoOffset, `8`=VmemStore, `9`=VmemStoreNoOffset, `0xA-0x11`=VmemStoreVmsk0..7, `0x12`=VmemStoreIndexed, `0x13`=VmemStoreIndexedNoOffset, `0x14-0x1B`=VmemStoreIndexedVmsk0..7, `0x1C`=SetIarLane, `0x1D`=SetIarSublane, `0x1E`=SetIarRaw, `0x1F`=PushV2s. 32 sub-ops total.

### Viperfish (VXC) `TensorCoreVectorStore` — Encode @ `0x1f01ff60` (64-B bundle)

Byte-exact (`BitCopy(a3, 170, …, 4)` etc.):

| Field | abs bit | width | meaning | Confidence |
|-------|--------:|------:|---------|------------|
| source vreg | 170 | 4 | store-data vreg (proto `+28`) | CONFIRMED |
| sub-opcode discriminator | 167 | 3 | addr-mode/family | CONFIRMED |
| secondary opcode / vmsk | 163 | 4 | mask / sub-variant | CONFIRMED |
| base address / offset | 157 | 6 | VregNumber base | CONFIRMED |
| stride (Base variants) | 153 | 4 | stride select | CONFIRMED |
| (Base-variant field) | 151 | 2 | — | CONFIRMED |
| (trailing field) | 148 | 3 | Vs select / mask | CONFIRMED |
| address / mask field | 144 | 4 | — | CONFIRMED |

The data-vreg @170 w4 and base @157 w6 match the [Viperfish 64B Bundle](bundle-vf-64b.md) page exactly. Sub-opcode dispatch (proto `+0x50`): `0`=Noop, `5`=VectorStore, `6`=VectorStoreBase, `7`=VectorStoreMasked, `8`=VectorStoreBaseMasked, `9`=VectorStoreShuffled, `0xA`=VectorStoreShuffledBase, `0xB`=VectorStoreShuffledMasked, `0xC`=VectorStoreShuffledBaseMasked, `0xD`=VectorStoreIndexed0, `0xE`=VectorStoreIndexed1, `0xF`=VectorStoreIndexed0Masked, `0x10`=VectorStoreIndexed1Masked, `0x11-0x16`=SetLaneIar0/1·SetSublaneIar0/1·SetRawIar0/1, `0x17`=PushV2s, `0x18`=SetPrng. 25 sub-ops.

### Trillium (GFC) `TensorCoreVectorStore` — Encode @ `0x1fa08920` (64-B bundle)

Byte-exact (`BitCopy(a3, 169, …, 2)` etc.):

| Field | abs bit | width | Confidence |
|-------|--------:|------:|------------|
| sub-opcode top field | 169 | 2 | CONFIRMED |
| sub-opcode | 166 | 3 | CONFIRMED |
| secondary opcode | 162 | 4 | CONFIRMED |
| base address / offset | 156 | 6 | CONFIRMED |
| stride / mask | 152 | 4 | CONFIRMED |
| (Base-variant field) | 150 | 2 | CONFIRMED |
| (field) | 147 | 3 | CONFIRMED |
| (field) | 143 | 4 | CONFIRMED |

Sub-opcode dispatch is identical to Viperfish. Ghostlite's `TensorCoreVectorStoreEncoder` (@ `0x1f3c3200`) is structurally identical to VXC/GFC (same sub-op set, same template) with gen-shifted bit offsets; its field map was not separately dumped — **HIGH** by template identity.

### Trillium (GFC) `SparseCoreTecVectorStore` — Encode @ `0x1eccbe20` (64-B TEC bundle)

Byte-exact (`BitCopy(a3, 359, …, 3)` etc.), store slot in the upper bit region — the richest store family:

| Field | abs bit | width | Confidence |
|-------|--------:|------:|------------|
| opcode | 359 | 3 (4 alt) | CONFIRMED |
| normal-predication | 362 | 1 | CONFIRMED |
| rotate-predication | 363 | 1 | CONFIRMED |
| base / source | 353 | 6 | CONFIRMED |
| offset | 347 | 6 | CONFIRMED |
| stride | 340 | 3 | CONFIRMED |
| mask | 337 | 3 | CONFIRMED |
| (field) | 333 | 4 | CONFIRMED |

Sub-opcode dispatch (proto `+0x4c`, 34 cases): `0`=Noop, `8`=TileSpmemStore, `9`=TileSpmemStoreCircularBuffer, then the reduce-add matrix — `TileSpmemStoreAdd{S32,S16,F32,Bf16}`, `*CircularBufferAdd*`, `*CircularBufferPostUpdateAdd*`, `TileSpmemIndexedStore`, `*IndexedCircularBuffer*`, `*IndexedAdd{S32,S16,F32,Bf16}*`, `*IndexedReturnValueAdd*`, `*IndexedCircularBufferReturnValueAdd{S32,S16,F32,Bf16}*`. The per-variant inner field widths beyond the shared positions above are not exhaustively dumped (**HIGH**).

### Jellyfish `EncodeVectorStoreInstruction` — @ `0x1e868c40` (41-B bundle)

Byte-exact dispatch: encodes predication first (`EncodePredication<VectorStoreInstruction>` @ the call before the switch), then `switch(*((_DWORD*)a2 + 12))` — the sub-opcode at proto `+12`:

- **family A** `{0,6,0x10-0x17}`: sublane-mask + stride store (`EncodeVectorSublaneMaskEncoding`, `EncodeVectorStrideEncoding`).
- **family B** `{1,7,0x18-0x1F}`: offset + base + sublane-mask + stride store (`EncodeVectorOffsetEncoding`, `EncodeVectorBaseAddressEncoding`, plus the two above).
- **family C** `{2,3,4}`: indexed store (`SetIndexedAddressOperands`, routed by `v36[3]&1`).
- `case 5`: NOOP. `default`: `"Not implemented yet."` fatal.

The per-field encoders write into shared immediate slots: an inner `switch(v12[8])` with cases 0-3 chooses which of the 6 immediate slots holds the 5-bit register number, written at Bundle bytes 31..39. The store-data vreg is packed at byte 22 (5-bit) / byte 13; the TTU operand at byte 19/63. The absolute slot base inside the 41-byte bundle comes from the Bundle byte map (LOW — not pinned here).

---

## Addressing Modes

Four addressing modes, selected by sub-opcode — the same taxonomy as the load slot:

- **(a) base + immediate-offset** (`VmemStore` / `VectorStore` with offset): base is a granule-scaled VMEM byte offset; offset is an immediate placed in an immediate slot.
- **(b) base + register-offset** (`VmemStoreNoOffset` / `VectorStoreBase`): base held in a Vs register, no immediate-offset field. Pufferfish `PlaceVsRegister<BaseAddress, VmemStore>` places the base into one of 3 Vs slots.
- **(c) strided**: every store carries a stride field. Pufferfish `StrideToSyU32<VmemStore>` (@ `0x1d2970a0`) encodes the stride as either an Sreg number or a U32 immediate (`variant<SregNumber, ImmediateValueU32>`); the destination address moves by `stride` granules between sublanes.
- **(d) indexed / scatter** (`VmemStoreIndexed` PXC / `VectorStoreIndexed0/1` VXC/GXC / `TileSpmemIndexedStore` SC-TEC): per-lane destination addresses come from an index-address register (IAR) staged via `SetIar*`; the store scatters vector lanes to per-lane addresses. VXC/GXC have two index ports (Indexed0/Indexed1) for two coexisting scatter streams; PXC has one.

The IAR is set by dedicated store-slot sub-ops: PXC `SetIarLane`/`SetIarSublane`/`SetIarRaw` (cases `0x1C-0x1E`); VXC/GXC `SetLaneIar0/1`/`SetSublaneIar0/1`/`SetRawIar0/1` (cases `0x11-0x16`). The guard `kVectorStoreEvenOddSublanesIar < target.IarsPerTensorCore()` confirms the IAR count is per-gen.

---

## Memory-Tier Destination Encoding

The destination tier is selected by **which sub-opcode** (not a tier bit), within the single VectorStore slot:

| Destination tier | Sub-op family / path | Confidence |
|------------------|----------------------|------------|
| VMEM | `VmemStore*` (PXC 8-0x1B) / `VectorStore*` (VXC/GXC 5-0x10) | CONFIRMED |
| CMEM | `CmemStore`/`CmemStoreNoOffset` (PXC cases 6-7; 16-bit imm address @304/288/272) | CONFIRMED |
| SPMEM | `TileSpmemStore*` (SparseCore TEC slot) | CONFIRMED |
| SMEM | `ScalarStoreSmemAbsolute`/`ScalarStoreXToSmemY` (SCALAR/SCALAR_ALU slot, not vector-store) | CONFIRMED |
| BarnaCore VMEM | `BarnaCoreChannelVectorStore` (Pufferfish PFC channel slot) | CONFIRMED |

The LLO opcode (`kVectorStore` vs `kVectorCmemStore` vs `kScalarStore`) carries the tier choice down from the IR; the per-gen lowering maps it to the matching sub-opcode in the matching slot. This is the same slot-selects-tier model the load slot uses, keyed off the runtime [MemorySpace Enum](memory-space-enum.md); the SMEM tier is asserted by the buffer-assignment invariant `dest_address->memory_space() == MemorySpace::kSmem`. CMEM-store sub-ops exist only on Pufferfish; higher gens fold CMEM into VMEM-class addressing or lower it via `kVectorCmemStorePseudo`.

---

## Store Granularity

The store moves one full vector register per cycle, modulated by:

- **SublaneMask** (`sublane_mask` proto field): a per-sublane write-enable. On VXC/GXC the Masked sub-op variants add an explicit mask; on PXC the 8 `VmemStoreVmsk0..7` variants select one of 8 hardware vector-mask registers.
- **Even/odd-sublane split** (`kVectorStoreEvenOddSublanes`): store only even or only odd sublanes (sublane-stride 2) — VXC/GXC via the sublane-stride field, Jellyfish via family A's stride encoder.
- **Indexed/scatter** (per-lane): the finest granularity; each lane writes an independent address from the IAR.
- **Reduce-add** (SparseCore TEC only, V5+): `TileSpmemStoreAdd{S32,S16,F32,Bf16}` — store-with-accumulate (`SPMEM[addr] += vector`), plus circular-buffer post-update and return-value variants. This is the only store family that *reads* the destination (read-modify-write).

There is no per-element count field beyond the sublane mask and stride; the store always processes the full SIMD width. The validation string `"VectorStore mask length is not equal to target SIMD width."` enforces that the mask covers exactly the lane/sublane count.

---

## Masked / Predicated Store

Two orthogonal masking mechanisms:

1. **Bundle predication** — every store slot carries a predicate-register reference (5-bit on Jellyfish via `EncodePredication<VectorStoreInstruction>`; a predication enum on V5+). Values 0..14 = predicate register, 15 = `kAlwaysExecute`, 31 = `kNeverExecute` (NOP); a false predicate suppresses the entire store. The proto field is `predication` (`TensorcorePredication` on PXC, `Predication` on glc/gfc; SparseCore TEC additionally splits `normal_predication` @362 and `rotate_predication` @363).
2. **Vector write-mask** (sublane/vmask) — a per-sublane (or per-lane) write enable, **independent** of the bundle predicate. PXC has 8 hardware vmask registers (`Vmsk0..7`, sub-ops `VmemStoreVmsk0..7` and `VmemStoreIndexedVmsk0..7`). VXC/GXC carry an explicit `mask` field (`SparsecoreVectorMask` on TEC) and the Masked sub-op variants.

Predicated store = bundle-predicate gating; masked store = vmask/sublane write-enable. Both can apply at once (e.g. a predicated `VmemStoreVmsk3`).

---

## Per-Gen Deltas

| Property | JF (v3) | PF (v4) | VF (v5e) | GL (v5p) | GFC (v6e) |
|----------|:-------:|:-------:|:--------:|:--------:|:---------:|
| TC bundle width (B) | 41 | 51 | 64 | 64 | 64 |
| `MaxVectorStoreSlots` (write ports) | 1 | 1 | 1 | **2** | 2* |
| `NumVsSlots` (read/base ports) | 3 | 3 | 4 | 4 | 4* |
| Store-data vreg field width | 5 | 5 | 4 | 4 | 4 |
| Base/offset field width | 5 | 5 | 6 | 6 | 6 |
| `CanOverlayInMiscSlot` | false | (n/a) | a2==1 | true | true* |
| Hardware vmask registers | sublane only | 8 (`Vmsk0..7`) | mask field | mask field | mask field |
| Indexed/scatter ports | 1 | 1 | 2 (Idx0/1) | 2 | 2 |
| CMEM store sub-ops | no | **yes** | no | no | no |
| SparseCore TEC tile-SPMEM store | n/a | n/a | yes | yes | yes (+RMW) |
| SPMEM reduce-add (S32/S16/F32/Bf16) | n/a | n/a | yes | yes | **yes (34 sub-ops)** |
| Scalar-store SMEM atomic add | no | no | no | no | **yes (SumDestAndY / FetchAndAdd)** |
| `SupportsVectorStoreFence` | false | false | false | false | misc-slot |

Verified target overrides: `JellyfishTarget::MaxVectorStoreSlots`=1 (@`0x1d4916a0`), Pufferfish=1 (@`0x1d495be0`), Viperfish=1 (@`0x1d49c0a0`), **Ghostlite=2** (@`0x1d498ca0`). `CanOverlayInMiscSlot`: Jellyfish=false (@`0x1d491680`), Ghostlite=true (@`0x1d498c80`). `SupportsVectorStoreFence`=false on JF/PF/VF/GL (@`0x1d48f660`/`0x1d493fa0`/`0x1d499f00`/`0x1d497060`). Dragonfish (v3') shares the Jellyfish codec.

Structural deltas:
- **v3→v4 (JF→PF)**: bundle 41→51 B; CMEM-store sub-ops added; 8 explicit hardware vmask registers (`VmemStoreVmsk0..7`).
- **v4→v5e (PF→VF)**: bundle 51→64 B; store-data field 5→4 bits while base/offset 5→6 bits; CMEM-store dropped from the TC slot; sublane-shuffle store added; 2 scatter index ports; SparseCore TEC tile-SPMEM store family (with reduce-add) introduced.
- **v5e→v5p (VF→GL)**: `MaxVectorStoreSlots` 1→2 (two VMEM write ports); `CanOverlayInMiscSlot` fully true — the store slot can overlay the misc slot, sharing the vmask (`CheckVectorStoreSlotAndMiscSlotShareVmsk` in `TensorCoreCodecBase`).
- **v5p→v6e (GL→GFC)**: store-fence moves into the VectorMisc compact message; scalar SMEM stores gain atomic add (`ScalarStoreXToSmemSumDestAndY`, `SmemFetchAndAdd`); SparseCore TEC store reaches 34 sub-ops (full reduce-add + circular-buffer + indexed + return-value matrix).

(*) Trillium gfc store reuses the gxc `EncoderBase` template; the V5+ property values are confirmed for VF/GL via the Target overrides, and the gfc-specific values marked `*` follow from the shared template plus the verified gfc store-encoder bit map (`0x1fa08920`).

---

## Load / Store Slot Symmetry and Asymmetries

The store slot is the structural mirror of the [Memory-Load Slot](slot-memory-load.md):

- **Mirror**: both occupy a dedicated bundle slot (`SLOT_VECTOR_STORE`/`SLOT_VECTOR_LOAD` on TC; `SLOT_VST`/`SLOT_VLD` on SparseCore), decode through paired Encoder/Decoder classes in the same per-gen `TensorCoreCodecBase` template, and share the addressing-mode taxonomy (base+immediate-offset, base+register-offset NoOffset, strided, indexed). The `SetIar*` sub-ops live in the store slot but the IAR they set can be consumed by a subsequent indexed *load* and vice-versa. `kVectorLoad` and `kVectorStore` are adjacent in the internal `LloOpcode` enum (the store window starts at internal opcode 63).
- **Asymmetry 1 — port counts**: loads have `NumVsSlots` read ports (3/3/4/4), stores have `MaxVectorStoreSlots` write ports (1/1/1/2). VMEM is read-wide, write-narrow until Ghostlite doubles the write ports.
- **Asymmetry 2 — write-enable**: the store slot adds the sublane-mask/vmask write-enable, a write-side concept with no load analogue.
- **Asymmetry 3 — RMW**: only the store side has the SparseCore TEC reduce-add (read-modify-write) family.
- **Asymmetry 4 — fence**: store→load ordering to the same VMEM region is protected by the vector-store fence (`RequiresVectorStoreFence` @ `0x14020900`, `IsVmemReadPrecededByVmemStoreFence` @ `0x14020820`) — a load following a store needs a fence; the reverse does not.

---

## What Is Not Yet Pinned

- **The absolute byte offset of the store slot inside the Jellyfish 41-B bundle.** The V5+ `BitCopy` offsets are bundle-absolute; the Jellyfish per-field encoders route to shared immediate slots (Bundle bytes 31..39) whose absolute base needs the full Jellyfish Bundle byte map. LOW.
- **The 34 SparseCore-TEC reduce-add inner field widths.** The dispatch and the shared positions (359/353/347/340/337/333) are CONFIRMED; the per-variant Add/CircularBuffer/ReturnValue inner widths are not exhaustively dumped. HIGH.
- **The Ghostlite glc store `BitCopy` field map** — structurally identical to vxc/gfc (same sub-ops, same template), not separately dumped. HIGH.
- **The full LloOpcode internal→proto integer for `kScalarStore`/`kVectorCmemStore`/`kBarnaCoreVectorStore`/`kVectorStoreFence`** (the `kVectorStore*` window `{63,64,65,68,69,70,460}` is CONFIRMED; the others need the `ProtoToLloOpcode` switch decode).
- **`Target::IarsPerTensorCore()` numeric value per gen** (gates the indexed-store IAR count). LOW.

---

## Cross-References

- [Memory-Load Slot](slot-memory-load.md) — the read-side mirror; shared addressing-mode taxonomy, IAR sharing, and the load/store asymmetries above.
- [MemorySpace Enum](memory-space-enum.md) — the 17-value runtime enum the store slot tier-selects on, and the proto↔enum remap.
- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths (41/51/64) and slot taxonomy this slot plugs into.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the absolute bundle bits of the `vector_store` slot (142..166) on PXC.
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the V5+ per-slot `Encoder::Encode` + `BitCopy` model the VXC/GLC/GFC store slots are written under (data-vreg @170, base @157).
- [MC-Emitter](mc-emitter.md) — the MC-layer store mnemonics and the register encoding table.
- [Memory Subsystem Overview](../memory/overview.md) — the tier model (HBM/VMEM/SMEM/CMEM/SPMEM) the store slot writes to.
