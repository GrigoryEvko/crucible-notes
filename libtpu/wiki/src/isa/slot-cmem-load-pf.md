# cmem_load Slot (Pufferfish)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The **cmem_load** slot is the one Pufferfish-only slot in the [51-byte TensorCore bundle](bundle-pf-51b.md): a dedicated 16-bit region (absolute bits 103..118) that issues a read from the constant-memory (CMEM) pool, co-resident in the same bundle cycle as the regular VMEM `vector_load` slot. It exists on Pufferfish (TPU v4) and nowhere else, because Pufferfish is the only production codename that *models* CMEM at all — it is the only `Target` whose `MemBanks(kCmem)` returns a real bank count (32) and the only one with non-zero CMEM DMA bandwidth. On Jellyfish / Viperfish / Ghostlite / Ghostfish, `MemBanks(kCmem)` is a `LogFatal`, and no `*::isa::TensorCoreCmemLoad` class exists in those codecs. CMEM is a v4 feature, so the bundle slot that reads it is a v4 slot.

Functionally the slot is a second, independent memory **read port**. Its addressing fields — sublane mask, base-address selector, offset, stride — mirror the regular `vector_load` slot exactly; the only difference is that the address targets `MemorySpace = kCmem (= 4)` and is scaled by `CmemWordSizeBytes` rather than `VmemWordSizeBytes`. Because the cmem_load dedicated region (103..118) is disjoint from the vector_load region (119..140), a single bundle can issue one CMEM read and one VMEM read together — the v4 double-operand-fetch datapath that feeds the MXU operand pipe at twice the single-port rate. That is the reason CMEM, and this slot, exist.

This page documents the slot as a reimplementation target: its on-wire field map at absolute-bit precision (decoded byte-exact from `TensorCoreCmemLoadEncoder::Encode`), its three-way oneof tag (empty / Noop / CmemLoad) and the `kNeverExecute = 31` idle encoding, its draw from the bundle's shared Y-register and immediate operand pool, and the emit → proto → SlotMap → address-scale bridge that turns an `EmitVectorCmemLoad` call into bundle bits and a CMEM byte address. The CMEM pool, allocator, banking, and per-generation availability are owned by the [CMEM Pool](../memory/cmem-pool.md) page; this page owns the bundle slot.

For reimplementation, the contract is:

- **The dedicated region (103..118):** SublaneMask @103/3, BaseAddress @106/2, Offset @108/2, Stride @110/3, has-bit @113/1, Predication @114/5 — each written by one `BitCopy(buf, abs_bit, &field, 0, width)`.
- **The shared-pool draw:** three 5-bit Y-register selectors (@251/246/241 = Vs2/Vs1/Vs0) and up to four 16-bit immediates (@304/288/272/256), allocated from the same pool every other slot uses, gated by the submessage has-bits.
- **The oneof tag at proto+0x50:** `0` → empty (predication only), `5` → Noop (predication forced to 31 = `kNeverExecute`), `6` → CmemLoad (the issue variant, writes all addressing + operand fields).
- **The CMEM-address bridge:** `EmitVectorCmemLoad` → `EmitVectorLoadCommon<…CmemLoad>` → `proto_utils::Place*` SlotMap binders; the byte address scaled by `CmemAddrScaled` and constructed with `MakeCmemConstant(MemorySpace = kCmem = 4)`.
- **Why v4-only:** only `PufferfishTarget::MemBanks(kCmem)` returns a value (32); the dedicated slot makes CMEM a read port co-issuable with VMEM in one bundle.

| | |
|---|---|
| **Encoder** | `pxc::isa::TensorCoreCmemLoadEncoder::Encode(TensorCoreCmemLoad const&, Span<uint8>)` @ `0x1ecf89a0` |
| **Bit primitive** | `BitCopy(void*, int dst_bit, void const*, int src_bit, int nbits)` @ `0x1fa0a900` — `dst_bit` == absolute bundle bit |
| **Dedicated region** | abs bits 103..118 (16 bits) — disjoint from `vector_load` @119..140 |
| **Oneof tag** | proto `+0x50`: `0`=empty, `5`=Noop (pred=31), `6`=CmemLoad (issue) |
| **Submessage defaults** | `TensorCoreCmemLoad_globals_` @ `0x223fb410` (outer), `TensorCoreCmemLoad_CmemLoad_globals_` @ `0x223fb3c8` (inner) |
| **Emit bridge** | `PufferfishTensorCoreEmitter::EmitVectorCmemLoad` @ `0x14120a40` → `EmitVectorLoadCommon<…CmemLoad>` @ `0x14120f40` |
| **One-per-bundle gate** | `EmitVectorCmemLoad` calls `CurrentBundle` + `PopulatedSlotsInBundle` @ `0x1d2ea840` |
| **Address scale** | `LloRegionBuilder::CmemAddrScaled` @ `0x1d539980` (byte→word by `CmemWordSizeBytes`) |
| **Constant address** | `LloAddress::MakeCmemConstant` @ `0x1d60ba20` → `LloAddress(MemorySpace=4=kCmem, off)` |
| **Why v4-only** | `PufferfishTarget::MemBanks(kCmem)` @ `0x1d493900` = 32; JF/VF/GL/GF `LogFatal` |
| **Has-bit (bundle dispatch)** | `0x040` at `TensorCoreBundle` proto `+0x48` (7th slot) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## On-Wire Field Map

The slot is encoded by `TensorCoreCmemLoadEncoder::Encode` (`0x1ecf89a0`), which writes each field with one call to the shared bit-packing primitive `BitCopy(buf, abs_bit, &field, 0, width)`. As across the whole Pufferfish bundle, **the absolute bundle bit of a field is the literal `dst_bit` argument** — there is no shift arithmetic to invert and no header offset to subtract (see [Pufferfish 51B Bundle §The Direct-BitCopy Model](bundle-pf-51b.md)). The dedicated region is sixteen bits, 103..118; the operand registers and immediates ride in the bundle's shared pool, not in the dedicated region.

```c
// pxc::isa::TensorCoreCmemLoadEncoder::Encode(proto, buf)  @ 0x1ecf89a0  (decoded byte-exactly)
pred = proto[+0x20];
BitCopy(buf, 114, &pred, 0, 5);              // Predication @114/5 — written first, unconditionally
tag = proto[+0x50];                           // oneof discriminator
if (tag == 0) return OK;                      // empty slot: only predication written
if (tag == 5) { pred = 31; BitCopy(buf, 114, &pred, 0, 5); return OK; }   // Noop = kNeverExecute
// tag == 6  (CmemLoad, the issue variant):
one = 1;       BitCopy(buf, 113, &one,    0, 1);   // has-bit (slot-present marker) = const 1
inner = proto[+0x48];                          // the CmemLoad submessage (or _globals_ default if clear)
if (inner.has[0x10] & 0x01) BitCopy(buf, 110, &inner.Stride,      0, 3);   // Stride
if (inner.has[0x10] & 0x02) BitCopy(buf, 108, &inner.Offset,      0, 2);   // Offset
if (inner.has[0x10] & 0x04) BitCopy(buf, 106, &inner.BaseAddress, 0, 2);   // BaseAddress
if (inner.has[0x10] & 0x08) BitCopy(buf, 103, &inner.SublaneMask, 0, 3);   // SublaneMask
// ----- shared operand pool (co-allocated with the other slots) -----
if (inner.has[0x10] & 0x10) BitCopy(buf, 251, &inner.Vs2, 0, 5);   // Vs2 register selector
if (inner.has[0x10] & 0x20) BitCopy(buf, 246, &inner.Vs1, 0, 5);   // Vs1
if (inner.has[0x10] & 0x40) BitCopy(buf, 241, &inner.Vs0, 0, 5);   // Vs0  (dest VREG / base)
if (inner.has[0x10] & 0x80) BitCopy(buf, 304, &inner.Imm, 0, 16);  // shared immediate word
if (inner.has[0x11] & 0x01) BitCopy(buf, 288, &inner.Imm, 0, 16);  // shared immediate word
if (inner.has[0x11] & 0x02) BitCopy(buf, 272, &inner.Imm, 0, 16);  // shared immediate word
if (inner.has[0x11] & 0x04) BitCopy(buf, 256, &inner.Imm, 0, 16);  // shared immediate word
```

| Field | Abs bit | Width | proto off | has-bit | Role | Confidence |
|---|---:|---:|---|---|---|---|
| SublaneMask | 103 | 3 | `+0x24` | `[0x10]&0x08` | sublane predicate mask | CONFIRMED |
| BaseAddress | 106 | 2 | `+0x20` | `[0x10]&0x04` | `BaseAddressEncoding`: ZERO / vs0 / vs1 / vs2 | CONFIRMED |
| Offset | 108 | 2 | `+0x1c` | `[0x10]&0x02` | `OffsetEncoding` mode selector | CONFIRMED |
| Stride | 110 | 3 | `+0x18` | `[0x10]&0x01` | stride / feature-length mode | CONFIRMED |
| has-bit | 113 | 1 | (const 1) | — | slot-present marker | CONFIRMED |
| Predication | 114 | 5 | `+0x20`(outer) | — | `0..14` pred reg / `15` always / `31` never (default `0x1f`) | CONFIRMED |
| Vs0 | 241 | 5 | `+0x30` | `[0x10]&0x40` | Y-register selector — dest VREG / base (shared pool) | CONFIRMED |
| Vs1 | 246 | 5 | `+0x2c` | `[0x10]&0x20` | Y-register selector (shared pool) | CONFIRMED |
| Vs2 | 251 | 5 | `+0x28` | `[0x10]&0x10` | Y-register selector (shared pool) | CONFIRMED |
| Imm | 304 | 16 | `+0x34` | `[0x10]&0x80` | shared immediate word (offset / index) | CONFIRMED |
| Imm | 288 | 16 | `+0x38` | `[0x11]&0x01` | shared immediate word | CONFIRMED |
| Imm | 272 | 16 | `+0x3c` | `[0x11]&0x02` | shared immediate word | CONFIRMED |
| Imm | 256 | 16 | `+0x40` | `[0x11]&0x04` | shared immediate word | CONFIRMED |

The decode-side accessors confirm the widths independently: `TensorCoreCmemLoad{Field}::GetConcatenatedValue` (`0x1ecf8820..0x1ecf8980`) read the proto-internal struct with `shr`+`and` masks whose pop-counts match the on-wire `BitCopy` widths exactly — 5-bit predication, 3-bit sublane/stride, 2-bit base/offset, 5-bit Y-regs, 16-bit immediates. Round-trip width agreement is the strongest single-binary confirmation.

> **NOTE —** the field that the [Pufferfish 51B Bundle](bundle-pf-51b.md) page lists as "(sub-field) @103/3" is the **SublaneMask** — this page's encoder decode resolves it. The bundle page's @103..118 region matches this layout bit-for-bit; the difference is only that the per-field roles are named here from the dedicated `TensorCoreCmemLoad{Field}` accessor symbols rather than inferred from position.

---

## The Oneof Tag and the Idle Encoding

The `TensorCoreCmemLoad` submessage carries a oneof discriminator at proto `+0x50`, read first by the encoder, that selects one of three behaviours:

| Tag | Variant | Encoder behaviour | Confidence |
|---:|---|---|---|
| `0` | empty | only Predication @114 is written; encoder returns | CONFIRMED |
| `5` | Noop | Predication forced to `31` (`kNeverExecute`); return | CONFIRMED |
| `6` | CmemLoad | the issue variant — has-bit @113 set, all addressing + operand fields written | CONFIRMED |

An idle slot therefore carries Predication = `31` (`kNeverExecute`), the identical empty-slot value the rest of the Pufferfish bundle uses. When the bundle codec finds the slot's has-bit clear in the [twelve-slot dispatch](bundle-pf-51b.md), it substitutes the `TensorCoreCmemLoad_globals_` default instance (`0x223fb410`); within the issue variant, a field whose per-field has-bit is clear takes the `TensorCoreCmemLoad_CmemLoad_globals_` default (`0x223fb3c8`). The two-level default — one for an absent slot, one for an absent field inside a present slot — is what lets the encoder run branch-free over a partially-populated submessage.

> **GOTCHA — predication 0 is a live op, not "skip".** The bundle buffer is `memset` to zero before encode, but predicate `0` is a valid predicate-register reference, not the empty marker. A reimplementation that leaves an unused cmem_load slot at the zeroed default issues a CMEM read gated on predicate register 0. The empty value is the explicit `31` written by tag-0/tag-5 (or the `_globals_` default's stamped 31), never the zeroed buffer.

---

## The Shared Operand Pool

The cmem_load slot owns its addressing fields (sublane / base / offset / stride) in the dedicated region, but its **register and immediate operands come from the bundle's shared pool** — the same three 5-bit Y-register selectors (abs 241/246/251) and six 16-bit immediate words (abs 256/272/288/304/320/338) that the two VALU lanes and the other two memory slots draw from. The encoder writes into the pool slots (Vs2 @251, Vs1 @246, Vs0 @241, immediates @304/288/272/256) but does **not** decide *which* pool entry each operand uses; that binding happens earlier, at the proto-build layer, through `xla::pufferfish::proto_utils::Place*` template functions parameterized on `TensorCoreCmemLoad_CmemLoad`:

```c
// proto_utils binders for cmem_load (proto-build layer) — same SlotMap pool as VALU / vector_load / vector_store
PlaceSublaneMask<CmemLoad>(optional<SregnoOrImm>, SlotMap<ulong,3>&, SlotMap<ImmValue,6>&, *);  // @0x1c492080
PlaceBaseAddressRegister<CmemLoad>(uint, SlotMap<ulong,3>&, *);                                 // @0x1c492b60
PlaceVsRegister<BaseAddress,CmemLoad>(uint, SlotMap<ulong,3>&, *);                              // @0x1c492be0
PlaceOffsetImmediate<CmemLoad>(ImmValue, SlotMap<ImmValue,6>&, *);                              // @0x1c4937a0
PlaceStride<CmemLoad>(SregnoOrImm, SlotMap<ulong,3>&, …);                                       // @0x1c4a9420
```

The `Vs0` selector (abs 241) receives the loaded data — the destination VREG — while `BaseAddress` (2-bit) picks ZERO / vs0 / vs1 / vs2 as the address base from the same Y-register pool, and the offset/index immediate rides one of the 16-bit shared slots. This is the same `SlotMap<ulong,3>` (three Y-register selectors) and `SlotMap<ImmValue,6>` (six immediates) the rest of the bundle shares.

> **GOTCHA — the shared pool, not the dedicated region, bounds co-issue.** The cmem_load dedicated region (103..118) is disjoint from vector_load (119..140), so the two slots co-exist freely on the wire. But both draw their registers and immediates from the *same* three-Y-register / six-immediate pool. A bundle that issues a cmem_load plus a vector_load plus two VALU lanes can name at most three distinct Y-registers and six distinct immediates across all of them combined. The SlotMap allocator enforces this at proto-build time and rejects an over-subscribed bundle before `Encode` ever runs.

---

## The Emit → Proto → SlotMap → Address Bridge

A CMEM load reaches the slot through `PufferfishTensorCoreEmitter::EmitVectorCmemLoad` (`0x14120a40`), whose signature is `(SregnoOrImm base, SregnoOrImm, ImmValue offset, optional<SregnoOrImm> sublane_mask)`. The emit path is:

```c
// PufferfishTensorCoreEmitter::EmitVectorCmemLoad  @ 0x14120a40  (decoded)
bundle = CurrentBundle();                              // @0x140fea80 — the bundle under construction
slots  = PopulatedSlotsInBundle(bundle);               // @0x1d2ea840 — one-cmem-load-per-bundle legality
// (rejects a second cmem_load in the same bundle — the "bundle has cmem load instruction already" diagnostic)
sub = DefaultConstruct<TensorCoreCmemLoad>();          // build the slot submessage
sub.SetPredication(is_always, is_never);               // 0x1d5b22e0 / 0x1d5b2300
EmitVectorLoadCommon<TensorCoreCmemLoad_CmemLoad>(sub, base, offset, sublane_mask);   // @0x14120f40
```

`EmitVectorLoadCommon<…CmemLoad>` (`0x14120f40`) is the shared field-placement routine — the *same* one the regular `vector_load` slot uses, templated on the CmemLoad submessage — which calls the `Place*` SlotMap binders above. The slot's addressing submessage is the same shape as vector_load's; the only divergence is the address space.

The CMEM byte address itself is produced separately and enters the bundle as the offset immediate plus the base Y-register:

```c
// LloAddress::MakeCmemConstant(long off)  @ 0x1d60ba20  (decoded)
return LloAddress(/*MemorySpace=*/4 /*=kCmem*/, off);   // calls LloAddress(MS, long) with esi = 0x4

// LloRegionBuilder::CmemAddrScaled(LloValue* base, LloValue* idx, long k)  @ 0x1d539980
return AddrScaled(base, idx, k, /*Granule=*/CmemWordSizeBytes);   // @0x1d538880, with a kCmem failure guard
```

`MakeCmemConstant` constructs an `LloAddress` with `MemorySpace = 4 = kCmem` — confirmed byte-exact (the constructor is called with `esi = 4`). `CmemAddrScaled` performs the byte→word scale by `CmemWordSizeBytes` so the bundle carries a word offset (the byte→word conversion happens in the bundle packer, not at issue time). The scaled word offset becomes the `Offset` immediate (via `PlaceOffsetImmediate`), and the CMEM base SREG (from `llo.saddr.cmem` / `ScalarAddressCmem`) becomes the `BaseAddress` Y-register (via `PlaceBaseAddressRegister`). There is no separate "CmemAllocator" — the address derivation shares the `ProgramMemoryAllocator(MS = kCmem)` / `BestFitAllocator` path described on the [CMEM Pool](../memory/cmem-pool.md) page.

---

## Why the Slot Is Pufferfish-Only

The cmem_load slot exists on Pufferfish and on no other codename because Pufferfish is the only `Target` that models the CMEM tier. The structural evidence is decisive:

- **`PufferfishTarget::MemBanks(kCmem)`** (`0x1d493900`) returns `32` — it indexes a `{16, 32, 8}` rodata table at `0xb5305c8` by `(MemorySpace − 3)`, so `kCmem (= 4)` → index 1 → 32 banks. `JellyfishTarget`, `ViperfishTarget`, and `GhostliteTarget` all `LogFatal` on `MemBanks(kCmem)` — the decompiled `PufferfishTarget::MemBanks` is `v4 = MS − 3; if (v4 >= 3) LogFatal("Unsupported memory space specified for MemBanks()"); return table[v4];`, and the others handle only `kVmem (= 3)` and `kSmem (= 5)`.
- Only `PufferfishTarget` overrides the `LocalDmaBandwidth*Cmem` virtuals (1121 GB/s VMEM→CMEM, 2339 GB/s CMEM→VMEM, 50 ns initial DMA latency). Every other codename inherits the base zero, which the cost model reads as "not modelled" and which gates MSA from placing anything in CMEM.
- No `pxc`-analog `TensorCoreCmemLoad` class exists on any other codename. Viperfish has `EmitVectorCmemLoad` emitter *symbols* but lowers them into the regular `VectorLoad` / `VectorStore` slots — there is no `vxc::vfc::isa::TensorCoreCmemLoad`. Ghostlite (`gxc::glc`) and Ghostfish (`gxc::gfc`) have neither the slot nor the emitters.

Because the dedicated cmem_load region (103..118) is disjoint from the vector_load region (119..140), a Pufferfish bundle can co-issue a CMEM read and a VMEM read in the same cycle. That doubling of operand-fetch bandwidth into the MXU pipe is the reason CMEM exists on v4, and the dedicated slot is its bundle-level expression.

---

## What Is Not Yet Pinned

- **The cmem_load addressing-mode subset.** The slot reuses the `vector_load` addressing submessage; whether it supports all four vector_load addressing modes (Vmem / Shuffled / Indexed-Iar0 / Indexed-Iar1) or a CMEM-restricted subset was not separated branch-by-branch. The field bases are CONFIRMED; the per-mode role split is HIGH.
- **The immediate-slot partition.** The encoder writes four of the six shared immediate slots (@256/272/288/304) for cmem_load; whether it can ever claim the @320/338 slots is decided at the SlotMap-allocation layer (`PlaceOffsetImmediate` over `SlotMap<ImmValue,6>`), not visible in the encoder. MEDIUM.
- **The decode-side gap behaviour.** The encoder's field positions are confirmed against the `GetConcatenatedValue` width accessors, but the `TensorCoreCmemLoadDecoder::Decode` body (the on-wire→proto inverse) was not disassembled; the round-trip is confirmed by width agreement, not by reading the decoder.
- **The literal CMEM word granule.** 16 bytes is inferred from the `Cmq16BIndirectStateFactory` naming; the exact value is supplied by the embedded `chip_parts.binarypb` and read at boot into `Target +0x510`. MEDIUM.

---

## Cross-References

- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the full 51-byte slot map this slot sits in (region 103..118), the twelve-slot has-bit dispatch (has-bit `0x040`, proto `+0x48`), and the direct-`BitCopy` absolute-bit model.
- [Memory-Load Slot](slot-memory-load.md) — the regular VMEM `vector_load` slot (abs 119..140) whose addressing submessage cmem_load mirrors, and the shared `EmitVectorLoadCommon` placement routine.
- [CMEM Pool](../memory/cmem-pool.md) — the constant-memory pool, the `ProgramMemoryAllocator(MS=kCmem)` / `BestFitAllocator` path, banking, per-generation availability, and why Pufferfish alone models CMEM.
