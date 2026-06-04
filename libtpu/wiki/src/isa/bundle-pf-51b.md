# Pufferfish 51-Byte Bundle

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The Pufferfish TensorCore VLIW bundle is a **51-byte (408-bit)** issue word — the TPU v4 successor to the [41-byte Jellyfish bundle](bundle-jf-41b.md), and the architectural bridge between Jellyfish's direct-pack encoder and the V6+ GXC codec. It is best read as a delta against Jellyfish: same `kNeverExecute` empty-slot convention, same predicate semantics, same shared operand pool, but four new datapath capabilities (a second MXU, a widened VALU lane, a first-class constant-memory load slot, and a second result-drain lane) and a fundamentally different encoding mechanism.

Where Jellyfish builds the bundle with `shl`/`and`/`or` arithmetic on the qwords of a 53-byte scratch struct and then strips a 12-byte header, Pufferfish has **no scratch struct and no header strip**. `EncoderPfTensorCore::EncodeBundleInternal` (`0x1e8c5c40`) is a thin wrapper: it asks the codec for its byte size (51), `operator new`s and `memset`s a 51-byte buffer to zero, and hands the buffer to the codec's `Encode` method. The codec — `asic_sw::deepsea::pxc::isa::TensorCoreCodecBase<TensorCoreBundle, …, Predication>` (`Encode` @ `0x1d224300`) — walks the bundle proto slot-by-slot and, for each present slot, calls a per-slot `TensorCore{Slot}Encoder::Encode` that writes each field into the buffer with one call to the shared bit-packing primitive `BitCopy(void* dst, int dst_bit, const void* src, int src_bit, int nbits)` (`0x1fa0a900`). **The absolute bundle bit of a field is the literal `dst_bit` argument to its `BitCopy` call** — there is no shift arithmetic to invert and no header offset to subtract. Output byte N is buffer byte N directly.

This page documents the complete 51-byte slot map at absolute-bit precision, the twelve-slot has-bit dispatch keyed on `proto+0x10`, the per-slot `BitCopy` field arithmetic, the five shared Load/Store/Cmem operand sub-encoders that all three memory slots inline against one shared addressing submessage and operand pool, the `kNeverExecute=31` prefill, and the byte-exact Jellyfish→Pufferfish delta. For reimplementation, the contract is:

- The **direct-`BitCopy` model**: a 51-byte buffer, `memset` to zero, written field-by-field via `BitCopy(buf, abs_bit, &field, 0, width)`; output byte N == buffer byte N, with **no 12-byte strip and no scratch struct**.
- The **twelve-slot has-bit dispatch**: a 12-bit slot has-mask at `TensorCoreBundle` proto `+0x10`, per-slot submessage pointers at `proto+0x18..+0x70`, and the codec's `test [proto+0x10],bit / cmove default` substitution of a `_globals_` default on a clear bit.
- The **absolute bit base + width of every slot**: the low dedicated region (bits 17..240), the shared operand pool (241..353), and the two scalar slots at the top (354..407).
- The **five shared memory operand sub-fields**: base-address (2b), offset (2b), stride (3b), the 3-entry Y-register selector SlotMap (5b @241/246/251), and the 6-entry immediate SlotMap (16b @256/272/288/304/320/338), bound at proto-build time by `VectorYVEncode` / `ScalarYEncode` / `SetImmOrDie`.
- The **`kNeverExecute=31` prefill** that `FillDefaultBundle` (`0x1d222ee0`) stamps into every slot's predicate field, and the four-axis JF→PF delta (2nd MXU, wide VALU0, cmem_load, 2nd result).

| | |
|---|---|
| **Encode wrapper** | `EncoderPfTensorCore::EncodeBundleInternal(TensorCoreBundle const&)` @ `0x1e8c5c40` |
| **Codec engine** | `TensorCoreCodecBase<…, Predication>::Encode` @ `0x1d224300` (12-slot dispatch) |
| **Bit primitive** | `BitCopy(void*, int dst_bit, void const*, int src_bit, int nbits)` @ `0x1fa0a900` — `dst_bit` == absolute bundle bit |
| **Wire width** | **51 bytes / 408 bits** (`EncoderPfTensorCore::BundleSizeBytes` @ `0x1d227740` returns `0x33`) |
| **Bit numbering** | **LSB-first** — bit 0 is the LSB of byte 0; `BitCopy`'s `dst_bit` is an LSB-first absolute index (matches [Bundle Model](bundle-model-overview.md)) |
| **Buffer model** | `operator new(51)` + `memset(buf,0,51)`; **output byte N == buffer byte N** (no header strip) |
| **Slot has-mask** | 12-bit word at `TensorCoreBundle` proto `+0x10`; one bit per slot |
| **Dispatchable slots** | 12 (scalar ×2, vector-ALU ×2, vstore, vload, **cmem_load**, vextended/MXU ×2, vresult ×2, misc) |
| **Shared operand pool** | 3× 5-bit Y-register selectors @241/246/251 + 6× 16-bit immediates @256/272/288/304/320/338 |
| **Empty-slot mark** | predicate `0x1f` (31 = `kNeverExecute`) stamped by `FillDefaultBundle` @ `0x1d222ee0` |
| **MC cross-check** | `TPUMCCodeEmitter::encodeInstruction` @ `0x13c74885` loads `0x198` = 408 bits = 51 bytes |
| **TpuVersion** | 2 = Pufferfish = TPU v4 (second width source: `PufferfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7ac0` returns `51`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Direct-BitCopy Model and the Absolute-Bit Law

Jellyfish encodes a bundle as read-modify-write arithmetic on a scratch struct, then copies a 41-byte window out of it. Pufferfish does neither. `EncodeBundleInternal` is a five-step wrapper around the codec, and the disassembly is the proof:

```c
// EncoderPfTensorCore::EncodeBundleInternal(TensorCoreBundle const&)  @ 0x1e8c5c40
codec = this->codec;                       // r13 = encoder; codec ptr from this
size  = codec->vtable[0x30]();             // call [rax+0x30] -> 51 (BundleSizeBytes)
buf   = operator new(size);                // _Znwm(51)        @ 0x1e8c5c6d
memset(buf, 0, size);                      // zero the whole buffer @ 0x1e8c5c7d
codec->vtable[0x18](buf, bundle, size);    // call [rax+0x18] -> TensorCoreCodecBase::Encode
result.data = buf; result.size = 51;       // StatusOr<vector<uint8>> of size 51
```

There is no `+0x0C` advance and no overlapping `vmovups` copy-out — the buffer the codec writes *is* the wire bundle. Every field lands at its absolute bundle bit directly, because the codec writes it there:

```c
// TensorCore{Slot}Encoder::Encode field write (every slot, every field)
BitCopy(/*dst=*/buf, /*dst_bit=*/ABS_BIT, /*src=*/&proto_field, /*src_bit=*/0, /*nbits=*/WIDTH);
```

`BitCopy` (`0x1fa0a900`, mangled `_Z7BitCopyPviPKvii`) takes the destination buffer in `rdi`, the **absolute destination bit in `esi`**, the source field pointer in `rdx`, the source start bit in `ecx` (always 0 for a fresh field), and the width in `r8d`. The bit index is **LSB-first**: bit 0 is the least-significant bit of buffer byte 0, bit 8 the LSB of byte 1, and so on — the same convention used everywhere in the encode path (see [Bundle Model §bit-numbering](bundle-model-overview.md)). There is no MSB-first ordering and nothing to invert.

> **NOTE —** this is the single fact that makes the whole bundle decodable. To recover any field's position, disassemble its per-slot encoder and read the `mov esi,0xNN` (absolute bit) and `mov r8d,0xNN` (width) immediates that precede each `call 0x1fa0a900`. The slot map below is the harvest of every such pair across the twelve per-slot encoders. The encode side is authoritative; the decode-side `TensorCore{Slot}Decoder::Decode` functions read the same bits as the inverse confirmation (see [Decode-Side: JF / PF](decode-side-jf-pf.md)).

The 408-bit width is cross-confirmed from an independent code path: the LLVM MC layer's `TPUMCCodeEmitter::encodeInstruction` loads `mov r15d,0x198` (`0x198 = 408`) at `0x13c74885` — a separate emitter from the proto codec, agreeing bit-for-bit. The 51 bytes partition into seven qwords by absolute bit:

```text
 qword0 = abs   0 .. 63    output bytes 0x00..0x07
 qword1 = abs  64 .. 127   output bytes 0x08..0x0F
 qword2 = abs 128 .. 191   output bytes 0x10..0x17
 qword3 = abs 192 .. 255   output bytes 0x18..0x1F
 qword4 = abs 256 .. 319   output bytes 0x20..0x27
 qword5 = abs 320 .. 383   output bytes 0x28..0x2F
 qword6 = abs 384 .. 407   output bytes 0x30..0x32   (partial, 3 bytes)
```

Field tops reach exactly bit 407 — the scalar_0 predicate at abs 403 plus its 5-bit width — so all 408 bits are accounted for at the top end.

---

## The Twelve-Slot Has-Bit Dispatch

The compiler-side `TensorCoreBundle` proto carries a 12-bit slot has-mask in the word at `proto+0x10` and a typed submessage pointer per slot at `proto+0x18..+0x70`. The codec `Encode` (`0x1d224300`) tests each has-bit in turn; on a set bit it encodes the present submessage, on a clear bit it substitutes that slot's `_globals_` default instance (a `cmove` to the default pointer). The dispatch order and the bit→slot map are byte-exact from the `test BYTE/DWORD PTR [r12+0x10],<bit>; cmove rax,<default>` ladder:

```c
// TensorCoreCodecBase<...>::Encode dispatch  @ 0x1d224300 (decompiled)
mask = *(uint16_t*)(proto + 0x10);                  // 12-bit slot has-mask @ proto+0x10
for each slot in dispatch order:                    // scalar_0 (0x1) .. misc (0x800)
    sub = (mask & slot.bit) ? *(proto + slot.proto_off)  // present submessage ptr
                            : slot.globals_default;       // _globals_ default
    if (sub == nullptr) sub = slot.globals_default;  // null-ptr also falls back to default
    slot.encoder->Encode(sub, buf);                  // per-slot BitCopy writes
```

The twelve `test [proto+0x10],N` immediates appear in strict ascending order — `0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800` — confirming a dense 12-bit mask with no gaps. Each slot's encoder writes into a disjoint dedicated bit region, so all twelve slots can co-exist in one bundle; operand-pool conflicts (more register/immediate operands than the 3 Y-reg / 6 imm slots can hold) are resolved earlier, at proto-build time, by the SlotMap packer (see [The Five Shared Operand Sub-Encoders](#the-five-shared-operand-sub-encoders)).

> **QUIRK — scalar_1 is encoded conditionally on scalar_0's opcode, not purely on its own has-bit.** The dispatch tests the scalar_1 has-bit (`0x2`) as expected, but the codec gates the entire scalar_1 encode behind `(scalar0.opcode_field − 17) >= 3` (the codec reads scalar_0's opcode oneof at submessage dword `+0x50` and skips scalar_1 when it falls in `{17,18,19}`). Those three scalar_0 opcodes are the wide forms that consume the scalar_1 bit window themselves; emitting a second scalar op alongside them would overwrite bits. Every other slot is gated purely on its own has-bit. A reimplementer must reproduce this scalar_0→scalar_1 interlock, not treat the two SPU slots as fully independent.

| Has-bit | `proto+` | `_globals_` default @ | Slot (role) | Per-slot encoder @ | Abs bits (region) |
|---|---|---|---|---|---|
| `0x001` | `+0x18` | `0x223fd378` | scalar_0 (SPU) | `0x1ed16dc0` | 381..407 (27b) |
| `0x002` | `+0x20` | `0x223fef38` | scalar_1 (SPU) | `0x1ed2a0e0` | 354..380 (27b) |
| `0x004` | `+0x28` | `0x22400f90` | vector_alu_0 (VPU, **wide lane**) | `0x1ed45060` | 198..240 (43b) |
| `0x008` | `+0x30` | `0x224036b8` | vector_alu_1 (VPU, narrow lane) | `0x1ed68d80` | 167..197 (31b) |
| `0x010` | `+0x38` | `0x22411eb0` | vector_store (mem-store) | `0x1ee3b440` | 142..166 (25b) |
| `0x020` | `+0x40` | `0x22410b88` | vector_load (mem-load) | `0x1ee287e0` | 119..140 (22b) |
| `0x040` | `+0x48` | `0x223fb410` | **cmem_load** (const-mem load, NEW) | `0x1ecf89a0` | 103..118 (16b) |
| `0x080` | `+0x50` | `0x22407210` | vector_extended_0 / MXU0 | `0x1edb0900` | 83..102 (20b) |
| `0x100` | `+0x58` | `0x2240cf50` | vector_extended_1 / MXU1 (−20 twin) | `0x1ee08060` | 63..82 (20b) |
| `0x200` | `+0x60` | `0x22410fd8` | vector_result_0 (EUP-pop lane 0) | `0x1ee2b1c0` | 52..62 (11b) |
| `0x400` | `+0x68` | `0x22411428` | vector_result_1 (EUP-pop lane 1) | `0x1ee2d180` | 41..51 (11b) |
| `0x800` | `+0x70` | `0x223fbce8` | misc (mask / rotate / imm-set) | `0x1ed03500` | 17..40 (24b) |

> **QUIRK — the proto-pointer order and the on-wire bit order are reversed.** The has-bit / `proto+` order runs scalar_0 → misc (bit 0x1 to 0x800), but the on-wire bit layout runs the *other way*: misc sits at the bottom of the bundle (abs 17) and scalar_0 at the very top (abs 381..407). A reimplementer who lays slots into the buffer in has-bit order, low-to-high, inverts the entire bundle. The has-bit is a proto-occupancy index; the absolute bit comes only from the `BitCopy dst_bit`, never from the slot's ordinal.

---

## The Three On-Wire Regions

The 408-bit bundle is not twelve contiguous slot fields. It is three regions:

```text
 abs   0 ..  16   header / reserved   (not written by any per-slot encoder; buffer stays 0)
 abs  17 .. 240   LOW dedicated region: one disjoint sub-range per non-scalar slot
 abs 241 .. 353   SHARED operand pool: 3 Y-reg selectors + 6 immediate words
 abs 354 .. 407   the two scalar slots (SPU), packed at the TOP
```

The low dedicated region packs the ten non-scalar slots disjointly, low-to-high:

```text
 abs  17 ..  40 (24b)  misc
 abs  41 ..  51 (11b)  vector_result_1
 abs  52 ..  62 (11b)  vector_result_0
 abs  63 ..  82 (20b)  vector_extended_1 / MXU1
 abs  83 .. 102 (20b)  vector_extended_0 / MXU0
 abs 103 .. 118 (16b)  cmem_load                (NEW in Pufferfish)
 abs 119 .. 140 (22b)  vector_load
 abs 142 .. 166 (25b)  vector_store
 abs 167 .. 197 (31b)  vector_alu_1             (narrow lane)
 abs 198 .. 240 (43b)  vector_alu_0             (WIDE lane, +12b)
```

Because every dedicated sub-range is disjoint, all twelve slots can be populated in a single bundle. What is *not* disjoint is the shared operand pool: the three memory slots and both VALU lanes all draw their register and immediate operands from the same 3 Y-register selectors (abs 241/246/251) and 6 immediate words (abs 256/272/288/304/320/338). That sharing is what bounds co-issuability — a bundle can name at most three distinct Y-registers and six distinct immediates across all of its present slots combined.

> **NOTE —** three small bit gaps are never written by any of the twelve per-slot encoders: the leading 17 bits (abs 0..16), bit 141 (between vector_load and vector_store), and bits 336..337 (inside the immediate-pool region, between imm slot 4 and imm slot 5). The buffer is `memset` to zero, so they ship as zero. Whether abs 0..16 carries a bundle-level sequencer/loop prefix (as the SparseCore sequencer bundle does) or is pure alignment was not isolated — no `BitCopy` in any of the twelve encoders targets it. Marked LOW; see [What Is Not Yet Pinned](#what-is-not-yet-pinned).

---

## Per-Slot Field Arithmetic

Each per-slot encoder writes its fields with `BitCopy(buf, abs_bit, &proto_field, 0, width)`. The positions below are the `esi`/`r8d` immediates harvested directly from the `call 0x1fa0a900` sites in each encoder; they are absolute bundle bits, no transform applied.

### Scalar slots (abs 354..407) — `0x1ed16dc0` (scalar_0), `0x1ed2a0e0` (scalar_1)

The two scalar slots are identical in layout, scalar_0 sitting exactly 27 bits above scalar_1. Pufferfish replaces Jellyfish's single `ScalarInstruction` proto with roughly fifty per-opcode oneof submessages (`TensorCoreScalar0_ScalarMove`, `_ScalarIntAdd`, `_ScalarDmaSimple`, `_ScalarHalt`, …); the opcode field is written from two of those oneof branches but always lands at the same base, and the scalar's Y/immediate operand is bound through `ScalarYEncode` into the shared immediate pool, *not* written inline into the scalar region.

| Field | scalar_0 abs | scalar_1 abs | Width |
|---|---|---|---|
| operand (Scalar Y / operand) | 381 | 354 | 11 |
| X reg | 386 | 359 | 6 |
| opcode | 397 | 370 | 6 |
| predicate | 403 | 376 | 5 |

The scalar_0 harvest reads literally: `BitCopy(buf,0x193,…,5)` (pred @403), `BitCopy(buf,0x18d,…,6)` (opcode @397), `BitCopy(buf,0x182,…,6)` (X @386), `BitCopy(buf,0x17d,…,11)` (operand @381), plus four 16-bit immediate writes into the shared pool (abs 288/304/320/338).

### Vector-ALU lanes — `0x1ed45060` (lane 0, wide), `0x1ed68d80` (lane 1, narrow)

Lane 0 is the **wide lane** (43 bits, abs 198..240); lane 1 is the narrow lane (31 bits, abs 167..197). Both carry a 6-bit opcode matching the 56-value `VectorAluOpcode` enum (see [VPU Slot](slot-vpu.md)).

| Field | lane0 (wide) abs | lane1 (narrow) abs | Width |
|---|---|---|---|
| (lane0 first operand) | 198 | — | 5 |
| dest | 203 | 167 | 5 |
| (wide-only field region) | 208..219 | — | 12 |
| Vx | 220 | 177 | 5 |
| y | 225 | 172 | 5 |
| x2 | — | 182 | 5 |
| opcode | 230 | 187 | 6 |
| predicate | 236 | 193 | 5 |

Both lanes also write their register operands into the shared Y-register selectors (abs 241/246/251) and immediates into the shared pool.

> **QUIRK — the wide VALU0 lane has a 12-bit field region (abs 208..219) that VALU1 lacks, and no primary opcode branch writes it.** The 12-bit extent is CONFIRMED (it is the exact width difference between the 43-bit VALU0 region and the 31-bit VALU1 region), but no `mov esi,0x..` in the harvested VALU0 `BitCopy` calls targets bits 208..219 — they are written by a per-opcode oneof branch (the wide-only ops, `TensorCoreVectorAlu0_*`), not the common path. Its role — a second vector source, a wide-immediate selector, or a vmask/select operand — is inferred from the lane-width delta and marked LOW. A reimplementer must reserve the 12 bits but should treat their meaning as unresolved.

### Vector-Extended / MXU slots — `0x1edb0900` (MXU0), `0x1ee08060` (MXU1)

Pufferfish doubles Jellyfish's single VectorExtended slot into two independent MXU control slots. MXU1 (abs 63..82) is a **bit-for-bit twin of MXU0 (abs 83..102), offset exactly −20 bits**.

| Field | MXU0 abs | MXU1 abs | Width |
|---|---|---|---|
| sub-op | 83 | 63 | 3 |
| mode / mxu-num | 89 | 69 | 2 |
| opcode | 91 | 71 | 7 |
| predicate | 98 | 78 | 5 |

The MXU0 harvest reads `BitCopy(buf,0x62,…,5)` (pred @98), `BitCopy(buf,0x5b,…,7)` (opcode @91), `BitCopy(buf,0x59,…,2)` (mode @89). The 7-bit opcode field selects the matmul / PushGains / transpose roster; for matmul the field widens to 9 bits (abs 89..97) so the low two bits @89..90 carry the physical MXU number (0..3). The full opcode roster and the −20 twin are documented on the [MXU Slot](slot-mxu.md) page and the [Decode-Side: JF / PF](decode-side-jf-pf.md) page.

### Vector-Result slots — `0x1ee2b1c0` (result_0), `0x1ee2d180` (result_1)

Two EUP-result drain lanes, one per VALU lane, each 11 bits. result_0 (abs 52..62) sits exactly 11 bits above result_1 (abs 41..51).

| Field | result_0 abs | result_1 abs | Width |
|---|---|---|---|
| which-destination | 52 | 41 | 2 |
| (mode block) | 54 | 43 | 2 |
| valid | 54 | 43 | 1 |
| result-format | 56 | 45 | 2 |
| predicate | 58 | 47 | 5 |

The `which-destination` field routes the deferred transcendental/EUP result to a dest VREG drawn from the shared register references (`V0_DEST` / `V1_DEST` / `VLD_DEST` routing); see [ResultFifo](resultfifo-archregister.md) and [EUP / Transcendental Slot](slot-eup-transcendental.md).

### cmem_load slot (abs 103..118) — `0x1ecf89a0`

The first-class constant-memory load slot, new in Pufferfish. Its addressing operand is read from the addressing submessage at `proto+0x48` (the same structure `vector_load` uses) and written with the shared memory operand shapes.

| Field | abs bit | Width |
|---|---|---|
| sublane-mask | 103 | 3 |
| base-address | 106 | 2 |
| offset | 108 | 2 |
| stride | 110 | 3 |
| has-bit | 113 | 1 |
| predicate | 114 | 5 |

The harvest reads `BitCopy(buf,0x72,…,5)` (pred @114), `BitCopy(buf,0x71,…,1)` (has @113), `BitCopy(buf,0x6e,…,3)` (stride @110), `BitCopy(buf,0x6c,…,2)` (offset @108), `BitCopy(buf,0x6a,…,2)` (base @106), `BitCopy(buf,0x67,…,3)` (sublane-mask @103). The field names match the [cmem_load Slot](slot-cmem-load-pf.md) page byte-for-byte. The index/destination registers come from the shared Y-register selectors (abs 241/246/251) and up to four 16-bit immediates (abs 256/272/288/304) from the shared pool. Full coverage of the addressing modes and the constant-memory path is on the [cmem_load Slot](slot-cmem-load-pf.md) page.

### vector_load (abs 119..140) — `0x1ee287e0`, vector_store (abs 142..166) — `0x1ee3b440`, misc (abs 17..40) — `0x1ed03500`

`vector_load` carries predicate @136 (5b), the addr-mode/oneof discriminator @134 (2b), dest @129 (5b), stride @126 (3b), offset @122 (2b), and base @134 (2b, mode-dependent); it has four addressing-mode oneof branches (Vmem / Shuffled / Indexed-Iar0 / Indexed-Iar1) that reuse the same field bases. `vector_store` carries source register fields at abs 162/157/152 (5b each), stride/feature fields at abs 149/142 (3b each), base @145 (2b), and offset @147 (2b). `misc` carries three 3-bit fields at abs 22/25/28, a 5-bit sub-op @31, and predication @36 (5b). All three pull their register operands from the shared Y-register selectors and immediates from the shared pool. The per-mode field-role differences for the four vector_load addressing modes were not separated branch-by-branch (the bases are CONFIRMED; the per-mode semantics are HIGH); see [Memory-Load](slot-memory-load.md) and [Memory-Store](slot-memory-store.md).

---

## The Five Shared Operand Sub-Encoders

In Jellyfish, the memory-operand sub-fields are written by five standalone `EncodeVector{SublaneMask,BaseAddress,Offset,Shuffle,Stride}Encoding` helper functions. Pufferfish has **no standalone helpers** — `vector_load`, `vector_store`, and `cmem_load` write the same five sub-field shapes *inline*, all reading from one common addressing submessage (`proto+0x48` for vector_load / cmem_load) and writing into the same shared operand pool. The five shared sub-fields:

| Sub-field | Width | Abs bit(s) | Source / role |
|---|---|---|---|
| base-address | 2 | vload @134 · vstore @145 · cmem @106 | `BaseAddressEncoding` (ZERO / vs0 / vs1 / vs2) |
| offset | 2 | vload @122 · vstore @147 · cmem @108 | `OffsetEncoding` mode selector |
| stride | 3 | vload @126 · vstore @142/149 · cmem @110/103 | stride / feature-length mode |
| index/mask register | 5 | shared @241 / 246 / 251 | 3-entry Y-register selector SlotMap |
| index/offset immediate | 16 | shared @256 / 272 / 288 / 304 / 320 / 338 | 6-entry immediate SlotMap |

The **operand source** — which of the three Y-register slots or six immediate slots a given memory operand uses — is not decided by the encoder. It is bound earlier, at the proto-build layer, by a family of `xla::pufferfish::proto_utils` template functions that take a `SlotMap<unsigned long, 3>&` (the three Y-register selectors) and a `SlotMap<ImmValue, 6>&` (the six immediates):

```c
// xla::pufferfish::proto_utils — operand-source binding (proto-build layer)
VectorYVEncode<Slot>(VregnoOrImm, SlotMap<ulong,3>&, SlotMap<ImmValue,6>&, Slot*);  // vector Y reg-or-imm
VectorYSEncode<Slot>(SregnoOrImm, SlotMap<ulong,3>&, SlotMap<ImmValue,6>&, Slot*);  // vector Y sreg-or-imm
ScalarYEncode<Slot>(SregnoOrImm,  Slot*, SlotMap<ImmValue,6>&);                     // scalar Y immediate
SetImmOrDie<...>(int slot, ImmValue, Slot*);                                        // writes one imm slot
VisitImmediateSlots<0..5>(TensorCoreBundle*, optional<uint>);                       // the 6 imm-slot visitors
```

The packer assigns each present slot's register/immediate operands to a free SlotMap entry; if more operands are requested than there are slots (more than 3 distinct Y-registers, or more than 6 distinct immediates, across the whole bundle), the binding is rejected at proto-build time. This is the Pufferfish analog of Jellyfish's `FindFreeSlot` per-lane search — here it is a SlotMap allocation over one shared pool rather than per-lane submessage search.

> **GOTCHA — the shared pool is the real co-issue constraint, not the dedicated regions.** A reimplementation that treats each slot as fully independent (because the dedicated regions are disjoint) will let, say, two memory slots and both VALU lanes each demand a fourth distinct Y-register and silently overwrite each other's selector. The dedicated regions co-exist freely; the *operands* must fit in 3 Y-register selectors and 6 immediate words combined. The SlotMap allocator is what enforces this, and it runs before `Encode`.

---

## The kNeverExecute Prefill

An absent slot must be a defined no-op, not garbage. `EncodeBundleInternal` zeroes the whole buffer, and the slot-default mechanism supplies the no-op value: `FillDefaultBundle` (`0x1d222ee0`) default-constructs each slot's submessage, sets all twelve has-bits, and stamps the predicate value `0x1f` (31 = `kNeverExecute`) into every slot's predicate field, plus a default `Scalar0_ScalarHalt` op. The disassembly shows the stamp directly — `mov DWORD PTR [rax+0x1c],0x1f` and `mov DWORD PTR [rax+0x20],0x1f`, repeated once per slot, writing the proto predicate field (at submessage offset +0x1c or +0x20 depending on slot type).

When a slot's has-bit is clear, the codec substitutes that slot's `_globals_` default instance (via the `cmove` in the dispatch ladder), whose predicate field is the `FillDefaultBundle`-stamped 31. The per-slot encoder then writes predicate 31 into the slot's predicate field. A present slot's encoder instead writes the real 5-bit `Predication` value: 0..14 a predicate-register reference, 15 `kAlwaysExecute`, +16 the negated form, 31 `kNeverExecute`.

> **GOTCHA — empty is predicate-31, not all-zero, even though the buffer starts at zero.** The buffer is `memset` to 0, but predicate 0 is a *valid* predicate-register reference, not "skip". A reimplementation that fills only active slots and leaves the rest at the zeroed default turns every empty slot into a live op gated on predicate register 0. The empty-slot value is the explicit `0x1f` stamp, identical to Jellyfish's empty-slot encoding. See [NOP / Unused-Slot Canonical Encoding](nop-canonical.md) and [Predicate Slot](slot-predicate.md).

---

## The Jellyfish → Pufferfish Delta

Pufferfish grows the bundle from 41 to 51 bytes (+10 bytes / +80 bits) and changes the encoder mechanism. The four datapath additions and the one mechanism change, all byte-exact:

| Change | Jellyfish | Pufferfish | Bit cost |
|---|---|---|---|
| **Encoder mechanism** | scratch struct + `shl`/`and`/`or` + 12-byte strip | 51-byte buffer + `BitCopy` absolute-bit, no strip | — |
| **2nd MXU** | one VectorExtended slot | MXU0 (abs 83..102) + MXU1 (abs 63..82), −20 twin | +20b |
| **Wide VALU lane** | two near-symmetric VALU lanes | VALU0 wide (43b) + VALU1 narrow (31b) | +12b |
| **cmem_load slot** | none | first-class const-mem load (abs 103..118) | +16b |
| **2nd result slot** | one VectorResult | result_0 (abs 52..62) + result_1 (abs 41..51) | +11b |
| **Operand sub-encoders** | 5 standalone helpers | 5 inline `BitCopy` writes into one shared pool | (larger pool) |

The mechanism change is the deeper one. Jellyfish's encoder is monolithic and personality-specific; Pufferfish's is a generic `TensorCoreCodecBase` template parameterized over the twelve `{Slot}Decoder`/`{Slot}Encoder` pairs plus a `Predication` policy. That same codec template, with a different slot list (four VALU lanes, two loads, vector-scalar + DMA slots), is the next-generation GXC codec — Pufferfish is the v4 origin of the codec design that carries through to V6+.

> **QUIRK — the second MXU is selected by an opcode field, not a bundle slot, even though there are two MXU *slots*.** Pufferfish has two MXU control slots in the bundle (MXU0/MXU1, the −20 twin) *and* four physical MXU arrays. The two are orthogonal: a matmul op's 9-bit opcode field carries the physical MXU number (0..3) in its low two bits (abs 89..90 for MXU0), so the bundle slot picks the *control lane* while the opcode picks the *physical array*. A reimplementer must not conflate "which MXU slot" with "which of the four MXUs".

---

## HBM / DMA Framing

The 51-byte width is the *issue* width. Stored in HBM, each bundle is framed by the shared codec-metadata table keyed on `(TpuVersion, TpuSequencerType)`, not by a hardcoded Pufferfish constant. The framing helpers on `EncoderPfTensorCore` all delegate to that table:

| Helper | @ addr | Result / delegate |
|---|---|---|
| `BundleSizeBytes()` | `0x1d227740` | `0x33` = 51 (on-wire issue width) |
| `BundleSizeBytesForDma()` | `0x1d227760` | reads version `[encoder+0x8]` → tail-jumps `codec_metadata::BundleSizeBytesForHbm(ver, seqtype)` @ `0x1ecf71a0` |
| `HasCheckByteForDma()` | `0x1d227780` | → `codec_metadata::HasCheckByteForHbm` @ `0x1ecf71c0` |
| `BundleCheckByte()` | `0x1d2277a0` | → `codec_metadata::BundleCheckByte` @ `0x1ecf71e0` |
| `BundleCheckByteMask()` | `0x1d2277c0` | → `codec_metadata::BundleCheckByteMask` @ `0x1ecf7200` |
| `DmaEncodingBytesRequired(n)` | `0x1d2277e0` | `n + BundleSizeBytesForHbm()` (per-bundle stride) |
| `MinimumBundlesRequiredToEncodeToDma()` | `0x1d227920` | `0xa` = 10 |
| `EncodeProgramForHbmInternal` | `0x1e8c5ce0` | thin wrapper → codec vtable `[+0x20]`, per-bundle loop over `TensorCoreProgram.bundles` |

> **NOTE —** the codec-metadata table lives in `platforms_deepsea::jellyfish::isa::codec_metadata` and is shared across generations — Pufferfish (`TpuVersion = 2`) is one row of a `(TpuVersion, TpuSequencerType)` lookup, so its HBM stride and check-byte are *not* compiled into `EncoderPfTensorCore`. The exact per-bundle framing bytes (the check byte and any pad) are written by the program-level `EncodeProgramForHbmInternal` and are not individually pinned on this page.

---

## What Is Not Yet Pinned

- **The VALU0 12-bit field region (abs 208..219).** Extent CONFIRMED, role LOW — written by a `TensorCoreVectorAlu0_*` per-opcode oneof branch, not the common path; a second vector source, wide-immediate selector, or vmask is the inferred candidate set.
- **The header / reserved bits (abs 0..16, bit 141, abs 336..337).** Not written by any of the twelve per-slot encoders; ship as zero. Whether abs 0..16 is a bundle-level sequencer prefix or pure alignment is LOW.
- **The per-opcode oneof submessage rosters.** Pufferfish replaces each scalar/VALU/MXU opcode field with roughly fifty typed oneof submessages; the opcode-field bit base+width is CONFIRMED, but the enum-value → oneof mapping is a separate (large) deliverable, not enumerated here.
- **The four vector_load addressing-mode branches.** The shared field bases are CONFIRMED; the per-mode role split (Vmem / Shuffled / Indexed-Iar0 / Indexed-Iar1, discriminated by the 2-bit oneof @134) is HIGH.

---

## Cross-References

- [Jellyfish 41B Bundle](bundle-jf-41b.md) — the v3 predecessor this page deltas against: the scratch-struct / 12-byte-strip encoder, the `slot_mask@proto+0x10` dispatch, and the single MXU / VALU / result slots Pufferfish doubles or widens.
- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths (41 / 51 / 64) and the slot taxonomy this page instantiates for Pufferfish.
- [cmem_load Slot](slot-cmem-load-pf.md) — the new v4 constant-memory load slot (abs 103..118) and its addressing submessage in full.
- [MXU Slot](slot-mxu.md) — the vector-extended/MXU opcode roster, the −20 twin, and the matmul / PushGains / transpose families the 7-bit opcode @91/71 selects.
- [MXU Latency: Pufferfish](../cost/mxu-latency-pf.md) — the cost model that consumes the MXU-slot fields decoded here.
- [Decode-Side: JF / PF](decode-side-jf-pf.md) — the inverse `TensorCore{Slot}Decoder::Decode` path that reads these same bits, the independent confirmation of every slot-map base.
- [Record Format](record-format.md) — the 239-bit MC `APInt`; why the proto codec path is distinct from the MC layer that cross-confirms the 408-bit width.
- [MC-Emitter](mc-emitter.md) — `TPUMCCodeEmitter::encodeInstruction` and the `0x198` = 408-bit constant that independently fixes the bundle width.
