# Per-Gen Remote-SFLAG Encoders

> *Every address, offset, immediate, bit shift, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`; not stripped — full C++ symbols). Other versions differ. Addresses are the binary's own VMA (`.text`/`.rodata` VMA == file offset; `.data.rel.ro` file offset = VMA − `0x200000`).*

## Abstract

A sync-flag write that targets a *peer chip's* SFLAG counter — the cross-chip half of an ICI barrier — cannot use a local SFLAG number. The peer's flag lives in that chip's VMEM, reached over the ICI fabric, and the write must carry a **remote address** that names both *which chip* and *which on-chip flag*. `libtpu` builds that address with one family of functions: the per-silicon-generation **remote-sync-flag encoders**, each registered under its `tpu::TpuVersion` key in a single `FunctionRegistry`. This page documents that family — the registry, the five per-gen encoder variants and their byte-exact bit layouts, the chip-id remap (`MapLogicalToPhysicalChipId`) applied before the JfDf encode, and how the two compose into the cross-chip remote-SFLAG VMEM address that `VsyncAddRemote` ultimately writes.

The decisive structural result is a **two-family split** keyed on `TpuVersion`. The V1 / coordinate-based encoder (`JfDf`, for `kJellyfish`=0 and `kDragonfish`=1) is the *only* gen that consumes a **physical** chip-id and supports multicast: it packs the chip X-coordinate at bit 20, the `MapLogicalToPhysicalChipId` output at bit 21, a `0x40` sync-flag segment at bits 12-17, a fixed remote marker at bit 18, and a conditional multicast bit at bit 19. The V2 / core-index-relative encoders (`Pufferfish`=2, `Viperfish`=3, `Ghostlite`=4) *drop* the physical chip-id and the multicast bool entirely; they mask the **logical** chip-coordinate (12 bits on Pufferfish, 14 bits on Viperfish/Ghostlite) into a single field and let the receive-side NIU + routing engine resolve physical placement. The chip field **widens 12→14 bits** between Pufferfish and Viperfish/Ghostlite — the same per-gen pod-address widening witnessed independently in the DMA-id and trace chip-id fields.

The SFLAG *number* formulas (the local reserved-block arithmetic) are on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the per-codename reserved integers are on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md); the cross-chip *data* memref (the analogous re-tag for non-SFLAG memory) is on [get_remote_memref](../collectives/get-remote-memref.md). This page owns the **per-gen remote-SFLAG encoders, the chip-id map, and the remote-address composition**.

For reimplementation, the contract is:

- **The remote SFLAG address is a bit-packed VMEM word, not a recomputed flat pointer.** It is built as a chain of LLO scalar ops (`SimmU32` const, `SandU32` mask, `SshllU32` shift-left, `SshrlU32` shift-right, `SaddS32` add, `SorU32` or) on a by-value `LloRegionBuilder`; the simplifier constant-folds when the chip-coord operands are compile-time constants. The result is tagged with annotation `"remote sync flag address"`.
- **Dispatch is keyed on `TpuVersion` via a `FunctionRegistry` populated by four sibling static initializers.** Five `Register` calls (keys `0/1/2/3/4`, split across the jellyfish/pufferfish/viperfish/ghostlite static-init TUs) map to four distinct encoders (JfDf is shared by `kJellyfish` and `kDragonfish`). An unknown version `LOG(FATAL)`s `"Unsupported version: "`.
- **Only JfDf consumes the physical chip-id and supports multicast.** The dispatcher always runs `MapLogicalToPhysicalChipId` and always passes the result + the peer `CoreLocationBase` to every encoder, but the V2 wrappers discard both and rebuild their chip field from the logical coordinate. Pufferfish `LOG(FATAL)`s on multicast; Viperfish/Ghostlite silently drop the bool.
- **`MapLogicalToPhysicalChipId` is a pure topology-coordinate transform, not a DeviceAssignment lookup.** It un-linearizes the logical chip-id to `(row, col, z)` over the program mesh, translates by the per-core subslice origin, bound-checks against the physical chip bounds, and re-linearizes over the physical bounds — feeding JfDf's bit-21 field only.

| | |
|---|---|
| **Consumer** | `VsyncAddRemote` @`0x1d522f40` → `EncodeRemoteSyncFlagAddress` + `CreateVectorSyncFlagAddRemote` |
| **Dispatcher** | `LloRegionBuilder::EncodeRemoteSyncFlagAddress` @`0x1d54da40` |
| **Registry** | `GetRemoteSyncFlagEncoderRegistry()::r` @`0x2257e488` (`FunctionRegistry<tpu::TpuVersion, …>`) |
| **Registrars** | `…_jellyfish.cc` @`0x2135b720` (keys 0,1 → JfDf) · `…_pufferfish.cc` @`0x2135bb30` (key 2) · `…_viperfish.cc` @`0x2135bc00` (key 3) · `ghostlite_dma_utils.cc` @`0x2135be70` (key 4) — 5 `Register` calls into the address registry, split across 4 TUs |
| **V1 encoder (gen 0/1)** | `EncodeRemoteSyncFlagAddressJfDf` @`0x1d5aa620` (coordinate-based, phys-chip + multicast) |
| **V2 encoders (gen 2/3/4)** | `pufferfish` @`0x1d5ae1a0` / `viperfish` @`0x1d5af9c0` / `ghostlite` @`0x1d5affc0` (core-index-relative) |
| **Chip-id remap** | `MapLogicalToPhysicalChipId` @`0x1d519f40` (3-D mixed-radix; JfDf-only) |
| **Version field** | `Target+0x398` (`tpu::TpuVersion`), via `[[[builder]+0x38]+0x10]+0x398` |
| **SFLAG segment const** | `DefaultSyncFlagSegmentId()` @`0x1d62da60` = `0x40` |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## 1. Where the encoder sits — `VsyncAddRemote` and the dispatcher

A cross-chip sync-flag bump is the LLO primitive `VectorSyncFlagAddRemote`. `LloRegionBuilder::VsyncAddRemote` @`0x1d522f40` is the thin builder that produces it, and its first act is to encode the remote address:

```c
// VsyncAddRemote(LloValue* sflag, CoreLocationBase const& peer, LloValue* value, bool multicast)  // 0x1d522f40
LloValue* addr = EncodeRemoteSyncFlagAddress(this, sflag, peer, multicast);   // the per-gen address encode
LloInstruction* inst = LloInstruction::CreateVectorSyncFlagAddRemote(addr, value, /*region=*/*this, …);
return LloRegion::AppendInstruction(*this, inst, 0, …);
```

So the encoded address is operand 0 of the remote SFLAG-add instruction; `value` (the increment) and the region are the rest. The encode is the only interesting work — everything downstream is a plain instruction append. (The granule-strided variant `VsyncAddRemoteInGranules` @`0x1d54e4e0` and the set variant `VsyncSetRemote` @`0x1d54e120` reach the same encoder.)

`LloRegionBuilder::EncodeRemoteSyncFlagAddress(LloValue* sflag, CoreLocationBase const& peer, bool multicast)` @`0x1d54da40` is the per-gen dispatcher. Re-confirmed byte-for-byte from the decompile, it runs four stages:

```c
function EncodeRemoteSyncFlagAddress(builder, sflag, peer, multicast):           // 0x1d54da40
    // [1] VALIDATE: sflag must be in the kSflag memory space (or a supported alias).
    if ((sflag[+0xb] & 0x7c) != 0x18:                                            // MS kSflag fast path
          && !(target.SupportsRemoteSyncFlagInTpuEmbeddingSpace()                //   vtable[+0x7b0]
                && ((sflag[+0xb]>>2)&0x1f) ∈ {0x9,0xa})                          //   kBarnaCoreS{mem,flag}
          && !(target.SupportsSparseCore()                                       //   vtable[+0x260]
                && (sflag[+0xb]&0x7c)==0x30)):                                    //   kSparseCoreSequencerSflag
        RetCheck("remote_sync_flag->memory_space() == MemorySpace::kSflag || …") // + ToMnemonic(sflag)

    // [2] REMAP: logical chip-id → physical (JfDf consumes this; V2 ignore it).
    phys_chip_id = MapLogicalToPhysicalChipId(builder, peer.CoreLocationBase[+0],
                                              /*operand_name=*/"EncodeRemoteSyncFlagAddress()"/*29 chars*/,
                                              /*multicast=*/false)               // 0x1d519f40
    // [3] DECOMPOSE peer for the encoder ABI.
    x_coord  = peer.CoreLocationBase[+8]                                         // the X chip-coord
    core_idx = (int)peer.CoreLocationBase[+0x10]                                 // the core index (0x18-byte POD)

    // [4] DISPATCH on TpuVersion.
    version = Target[+0x398]                          // [[[builder]+0x38]+0x10]+0x398, tpu::TpuVersion
    encoder = GetRemoteSyncFlagEncoderRegistry().Get(version)                    // 0x1d54e020
    if (encoder.empty()): LOG(FATAL) "Unsupported version: " << version          // 0x1d54dce9
    return encoder(sflag, &x_coord, multicast, phys_chip_id, /*builder copy=*/*this)
```

> **NOTE —** the dispatcher passes `multicast=false` *into* `MapLogicalToPhysicalChipId` (stage 2) regardless of the caller's `multicast` argument; the caller's `multicast` is forwarded only to the *encoder* (stage 4). The remap's own multicast parameter gates one of its passthrough conditions (§4) and is unrelated to the address-level multicast bit.

> **GOTCHA —** the `Target+0x398` version read is the type-witness for the registry key. The `LOG(FATAL)` at line 8252 (`"Unsupported version: " << LogMessage<tpu::TpuVersion>(…)`) types the key as `tpu::TpuVersion` and is the only behaviour on a registry miss — there is no fallback encoder, so a chip whose version is not one of the five registered keys cannot emit a remote sync-flag write.

---

## 2. The registry and the five `TpuVersion` registrations

`GetRemoteSyncFlagEncoderRegistry()::r` @`0x2257e488` (guard @`0x2257e490`) is a lazy singleton `FunctionRegistry<tpu::TpuVersion, LloValue*(LloValue* sflag, CoreLocationBase const&, bool multicast, LloValue* phys_chip_id, LloRegionBuilder)>` — internally an `absl::flat_hash_map<TpuVersion, shared_ptr<MapValue>>`. The `Get` path @`0x1d54e020` takes a shared mutex (`Mutex::lock_shared`), does a `raw_hash_set::find`, and returns the default empty-`std::function` on miss (which the dispatcher's `LOG(FATAL)` then catches).

It is populated across **four** sibling static-init TUs, each running `FunctionRegistry::Register` @`0x1d5aa7a0` once or twice into the address registry (`r` @`0x2257e488`) — for **five** total registrations:

- `_GLOBAL__sub_I_remote_sync_flag_encoder_jellyfish.cc` @`0x2135b720` registers JfDf @`0x1d5aa620` **twice** — key `1` (`movl $0x1` @`0x2135b74d`) then key `0` (`movl $0x0` @`0x2135b7be`) — and also seeds the sibling core-id and DMA-overrides registries.
- `_GLOBAL__sub_I_remote_sync_flag_encoder_pufferfish.cc` @`0x2135bb30` registers Pufferfish @`0x1d5af8a0` for key `2` (`movl $0x2` @`0x2135bb51`).
- `_GLOBAL__sub_I_remote_sync_flag_encoder_viperfish.cc` @`0x2135bc00` registers Viperfish @`0x1d5af900` for key `3` (`movl $0x3` @`0x2135bc21`).
- `_GLOBAL__sub_I_ghostlite_dma_utils.cc` @`0x2135be70` registers Ghostlite @`0x1d5b01e0` for key `4` (`movl $0x4` @`0x2135be95`).

The `TpuVersion` key is the third `Register` argument (stack-`movl` immediate); the `Register` bool return is discarded into per-gen `kRegister…`/`kUnused…` globals.

| `TpuVersion` key | mnemonic | encoder registered (registry wrapper) | arithmetic impl | family |
|---|---|---|---|---|
| 0 | `kJellyfish` | `EncodeRemoteSyncFlagAddressJfDf` @`0x1d5aa620` | (self — coordinate-based) | V1 |
| 1 | `kDragonfish` | `EncodeRemoteSyncFlagAddressJfDf` @`0x1d5aa620` | **same encoder as `kJellyfish`** | V1 |
| 2 | `kPufferfish` | `EncodeRemoteSyncFlagAddressPufferfish` @`0x1d5af8a0` | `pufferfish::dma_utils::…` @`0x1d5ae1a0` (12-bit chip) | V2 |
| 3 | `kViperfish` | `EncodeRemoteSyncFlagAddressViperfish` @`0x1d5af900` | `viperfish::dma_utils::…` @`0x1d5af9c0` (14-bit chip) | V2 |
| 4 | `kGhostlite` | `EncodeRemoteSyncFlagAddressGhostlite` @`0x1d5b01e0` | `ghostlite::dma_utils::…` @`0x1d5affc0` (14-bit, byte-identical to VF) | V2 |

`Ghostlite` is the `glc`-family v6e (the marketing name is "Trillium"). The keys `0/1/2/3/4` were byte-confirmed from each registrar's `[rbp]`-stored `movl` immediates (§2); the `LEA` of each encoder address in the full `.text` matches *only* its registrar.

> **NOTE —** the registry wrappers (the symbols named `EncodeRemoteSyncFlagAddress{Pufferfish,Viperfish,Ghostlite}`) carry the full 5-argument registry signature `(LloValue*, CoreLocationBase const&, bool, LloValue*, LloRegionBuilder)`. The per-gen *arithmetic impls* in the `dma_utils` namespaces carry the slimmer 3-argument signature `(LloValue* sflag, CoreLocationBase const&, LloRegionBuilder)` — no `phys_chip_id`, no `multicast`. The wrappers exist purely to discard those two extra arguments (and, for Pufferfish, to `LOG(FATAL)` if `multicast` is set) before tail-calling the impl. `EncodeRemoteSyncFlagAddressViperfish` @`0x1d5af900` and `…Ghostlite` @`0x1d5b01e0` are bare one-line tail-calls.

---

## 3. The per-gen bit-packing formulas

All encoders build the address as a chain of LLO scalar ops. **Only JfDf tags the result** `set_annotation_if_not_constant("remote sync flag address")`; the V2 `dma_utils` impls (@`0x1d5ae1a0`/`0x1d5af9c0`/`0x1d5affc0`) carry **no** annotation call — byte-confirmed. The two families differ in what they pack.

### 3.1 The two-family table

| gen (`TpuVersion`) | address formula | chip source | chip field | core/segment field | multicast | uses `phys_chip_id` |
|---|---|---|---|---|---|---|
| Jellyfish 0 / Dragonfish 1 (`JfDf` @`0x1d5aa620`) | `sflag \| (CLB[+8] << 0x14) \| (phys_chip_id << 0x15) \| 0x40000 \| (0x40 << 0xc) \| (mc ? 0x80000 : 0)` | `phys_chip_id` (bit 21) + `CLB[+8]` X-coord (bit 20) | bit 21 (physical) | seg `0x40` @ bits 12-17, marker bit 18, mc bit 19 | YES (bit 19, conditional) | YES (bit 21) |
| Pufferfish 2 (@`0x1d5ae1a0`) | `sflag \| (0x8000 \| (core_sub << 0xe)) \| ((CLB[+0] & 0xfff) << 0x12)` | `CLB[+0]` logical chip-coord | `0xfff` (12-bit) @ bit 18 | `core_sub` @ bit 14, seg `0x8000` @ bit 15 | NO (`LOG(FATAL)` `"is_multicast == false"`) | NO |
| Viperfish 3 (@`0x1d5af9c0`) | `sflag \| (0x8000 \| (core_sub << 0xe)) \| ((CLB[+0] & 0x3fff) << 0x11)` | `CLB[+0]` logical chip-coord | `0x3fff` (14-bit) @ bit 17 | `core_sub` @ bit 14, seg `0x8000` @ bit 15 | NO (bool dropped) | NO |
| Ghostlite 4 (@`0x1d5affc0`) | (byte-identical to Viperfish) | `CLB[+0]` logical chip-coord | `0x3fff` (14-bit) @ bit 17 | `core_sub` @ bit 14, seg `0x8000` @ bit 15 | NO (bool dropped) | NO |

where, for the V2 encoders:

- `core_sub = (CoreLocationBase[+8] & 3)`, then `+2` **iff** the sflag operand is in an sflag-class `MemorySpace`. The PF gate is `((sflag[+0xb]>>2)&0x1f) − 9 ≤ 1` (i.e. MS ∈ `{0x9,0xa}`); the VF/GL gate is `(((sflag[+0xb]>>2)&0x1d) | 2) == 0xe`.
- The fold `(0x20000 + (core_sub << 0x10)) >> 2` equals `(0x8000 | (core_sub << 0xe))` arithmetically (verified for `core_sub` 0..5). The V2 encoders literally compute the `0x20000`-add / `>>2` form (`SimmU32(0x20000)`, `SshllU32(core,0x10)`, `SaddS32`, `SshrlU32(.,2)`), which the simplifier resolves into the bit `0x8000 | core_sub<<14`.

`DefaultSyncFlagSegmentId()` @`0x1d62da60` is `asic_sw::deepsea::jxc::DefaultSyncFlagSegmentId()` and is a constant `0x40` (`mov eax,0x40; ret`); JfDf shifts it left by `0xc` to land it at bits 12-17.

### 3.2 JfDf — the V1 coordinate encoder (byte-exact)

```c
// EncodeRemoteSyncFlagAddressJfDf(LloValue* sflag, CoreLocationBase const& peer,
//                                 bool multicast, LloValue* phys_chip_id, LloRegionBuilder)   // 0x1d5aa620
x_coord = peer[+8]
addr = SorU32(sflag,        SshllU32(x_coord,      SimmU32(0x14)))   // | X << 20
addr = SorU32(addr,         SshllU32(phys_chip_id, SimmU32(0x15)))   // | phys_chip << 21
addr = SorU32(addr,         SimmU32(0x40000))                        // | remote marker (bit 18)
addr = SorU32(addr,         SimmU32(DefaultSyncFlagSegmentId() << 0xc))  // | 0x40 << 12  (bits 12..17)
if (multicast):
    VLOG(1) "Set multicast in EncodeRemoteSyncFlagAddress"           // remote_sync_flag_encoder_jellyfish.cc:30
    addr = SorU32(addr,     SimmU32(0x80000))                        // | multicast (bit 19)  CONDITIONAL
set_annotation_if_not_constant(addr, "remote sync flag address")
return addr
```

This is the only encoder that reads `phys_chip_id` (the `MapLogicalToPhysicalChipId` output, argument 4) and the only one whose multicast bit is conditional. The `VLOG(1)` site (`remote_sync_flag_encoder_jellyfish.cc:30`) is the only diagnostic on the multicast path.

> **GOTCHA —** the `0x80000` multicast bit is **not** fixed: it is gated on the `multicast` argument (`if (v8) … SorU32(.., 0x80000)`). A non-multicast JfDf remote write does *not* set bit 19.

### 3.3 Pufferfish / Viperfish / Ghostlite — the V2 core-index-relative encoders (byte-exact)

```c
// pufferfish::dma_utils::EncodeRemoteSyncFlagAddress(LloValue* sflag,
//                                                    CoreLocationBase const& peer, LloRegionBuilder)  // 0x1d5ae1a0
core_sub = SandU32(peer[+8], SimmU32(3))                             // CLB[+8] & 3
if (((sflag[+0xb] >> 2) & 0x1f) ∈ {9,10}):                          // sflag-class MS gate (PF)
    core_sub = SaddS32(core_sub, SimmS32(2))                         // + 2

chip = SshllU32( SandU32(peer[+0], SimmU32(0xfff)),  SimmU32(0x12))  // (CLB[+0] & 0xfff) << 18
seg  = SshrlU32( SaddS32(SimmU32(0x20000), SshllU32(core_sub, SimmU32(0x10))), SimmU32(2))
                                                                    // = 0x8000 | (core_sub << 14)
addr = SorU32(seg, sflag)                                            // | sflag (RAW)
return SorU32(chip, addr)                                            // | chip field
```

`viperfish::dma_utils::EncodeRemoteSyncFlagAddress` @`0x1d5af9c0` is identical in shape, with two differences: the MS gate is `(((sflag[+0xb]>>2)&0x1d)|2) == 0xe`, and the chip field is `(CLB[+0] & 0x3fff) << 0x11` (14-bit mask, shift 17). `ghostlite::dma_utils::EncodeRemoteSyncFlagAddress` @`0x1d5affc0` is **byte-identical** to Viperfish (mask `0x3fff` @`0x1d5b0052`, shift `0x11` @`0x1d5b0070`).

The Pufferfish *wrapper* @`0x1d5af8a0` is where multicast is rejected:

```c
// EncodeRemoteSyncFlagAddressPufferfish(LloValue* sflag, CoreLocationBase const&,
//                                       bool multicast, LloValue* /*phys, discarded*/, LloRegionBuilder)  // 0x1d5af8a0
if (multicast):
    LOG(FATAL) << MakeCheckOpString(1,0,"is_multicast == false")     // remote_sync_flag_encoder_pufferfish.cc:13
return pufferfish::dma_utils::EncodeRemoteSyncFlagAddress(sflag, peer, builder)   // drops phys + multicast
```

> **GOTCHA —** the masked V2 field is the **chip coordinate** `CLB[+0]` (`& 0xfff` on PF, `& 0x3fff` on VF/GL), shifted, with the `sflag` OR'd in *raw* — it is not `sflag & 0xfff`. The `CoreIndex() << 0xd` shift belongs to a *different* function — see §3.4.

### 3.4 What the address encoder is NOT — the sibling core-id encoder

Each V2 `dma_utils` TU also defines `RemoteSyncFlagCoreIdEncoder(TpuSequencerType, sflag, core, builder)` (PF @`0x1d5ae2e0` / VF @`0x1d5afb00` / GL @`0x1d5b0100`): `SimmU32(seq==TC?2 : seq==SC?4 : FATAL)` → `SaddS32(., core ?: CoreIndex())` → `SshllU32(., 0xd)` → `SorU32(sflag, .)`. This builds the **core-selector** field of the *DMA descriptor's* sflag slot (the `CoreIndex() << 0xd`, bit 13), and it is registered in a **separate** registry, `GetRemoteSyncFlagCoreIdEncoderRegistry()::r` @`0x2257e468` (guard @`0x2257e470`) — not the address registry documented here. The two are easy to conflate: the **address** encoder (this page) packs the *chip coordinate*; the **core-id** encoder packs the *core index*. They are distinct functions in distinct registries.

### 3.5 The 12→14 chip-field widening

The V2 chip field widens from 12 bits (Pufferfish, mask `0xfff`, shift 18) to 14 bits (Viperfish/Ghostlite, mask `0x3fff`, shift 17). This is the same per-generation pod-address widening seen in two other independent chip-id fields in the binary — the DMA-id chip field (11→14) and the trace chip-id field (12→14). The remote-SFLAG-address chip field is a third independent witness of the same widening. JfDf is structurally outside this pattern: it carries a *two-field* chip scheme (X-coord at bit 20, physical chip-id at bit 21) rather than a single masked chip field, so the "width grows per gen" observation applies to the V2 (PF→VF/GL) family, with JfDf as the V1 coordinate baseline.

---

## 4. `MapLogicalToPhysicalChipId` — the chip-id remap (JfDf-only)

Before dispatch, the dispatcher always runs `LloRegionBuilder::MapLogicalToPhysicalChipId(LloValue* chip_id, string_view operand_name, bool multicast)` @`0x1d519f40` on the peer's logical chip-coordinate. The result is fed as `phys_chip_id` to every encoder, but only JfDf reads it (bit 21); the V2 encoders ignore it and use `CLB[+0]` directly.

It is a **mixed-radix 3-D topology-coordinate transform**, not a `DeviceAssignment` array index. It takes no `DeviceAssignment`; it operates purely on the chip-id `LloValue`, the `Target` network-mesh radix, and per-core SMEM topology words. The logical chip-id it consumes was produced upstream by the replica-group flatten (the binomial/flat info-table resolver), not here.

### 4.1 The gate (else passthrough)

```c
function MapLogicalToPhysicalChipId(chip_id, operand_name, multicast):           // 0x1d519f40
    if (chip_id == null): return null
    if (Target[+0x930] != 1): return chip_id                  // remap disabled for this target
    cap = Target_subobject.vtable[+0x18]()                    // "is logical==physical / non-subslice" virtual
    if (!multicast && cap): return chip_id                    // remap is a no-op for this topology
    // IDEMPOTENCE: if chip_id is a const/opcode in {0xdb..0xe4} or opcode 0x2c whose annotation
    //              is already kSubslicePhysicalChipId/kSubsliceLogicalChipId → already mapped.
    //   (the (chip_id_opcode − 219) >= 0xA && != 44 test, then the annotation guard
    //    LloCheckForFailure "inst->annotation() != kSubslicePhysicalChipId")
    …  // (remap below)
```

If none of the passthrough conditions fire, the remap runs and re-annotates the result `"subslice-physical-chip-id"`, which the idempotence guard then recognises on any subsequent call.

### 4.2 The remap (mixed-radix re-linearization)

```c
    // [1] un-linearize the logical chip-id over the program mesh (column-fastest)
    (row, col, z) = ToChipCoordinates(chip_id)                // 0x1d51a3a0
        // col ← chip_id      % Target::NetworkColumns()       (0x1d6158c0)   → struct[+8]
        // row ← (chip_id/NC) % Target::NetworkRows()          (0x1d615880)   → struct[+0]
        // z   ← (chip_id/NC) / NetworkRows()                                 → struct[+0x10]
        //   ⇒ logical chip_id = z*(NRows*NCol) + row*NCol + col

    // [2] add the per-core subslice origin (where THIS slice sits in the physical pod)
    origin = LoadSubsliceOffset()                             // 0x1d519280; base-0x400 SMEM triple
    C0 = row + origin[0]                                      // physical row
    C1 = col + origin[1]                                      // physical column
    C2 = z   + origin[2]                                      // physical z / slice plane

    // [3] bound-check against the physical mesh bounds (per-sequencer-type)
    bounds = LoadPhysicalChipBounds()                         // 0x1d518dc0; base-0x400 SMEM triple
        // B0 = rows_bound, B1 = cols_bound, B2 = z_bound
    ScheckLt(C1, B1)   FATAL "Invalid logical column: …"
    ScheckLt(C0, B0)   FATAL "Invalid logical row: …"
    ScheckLt(C2, B2)   FATAL "Invalid logical z: …"           // hint "topology must be 2d for limited ICI routing"

    // [4] re-linearize with the PHYSICAL bounds as radix (column-fastest)
    phys = ((C2 * B0 + C0) * B1) + C1                         // = z_phys*(rows_bound*cols_bound)
                                                              //   + row_phys*cols_bound + col_phys
    set_annotation_if_not_constant(phys, "subslice-physical-chip-id")
    return phys
```

`ToChipCoordinates` @`0x1d51a3a0` does the two `SdivmodU32` decodes by `NetworkColumns`/`NetworkRows`. `LoadSubsliceOffset` @`0x1d519280` reads `Target::SubsliceOriginLocationWordOffset()` @`0x1d617ce0` from per-core SMEM and unpacks a base-`0x400` (10-bit) triple via two `SdivmodU32`-by-`0x400` (annotation `"encoded subslice offset"`). `LoadPhysicalChipBounds` @`0x1d518dc0` selects the word offset by sequencer type (`Target[+0x268]`: TensorCore=0 → `PhysicalChipBoundsLocationWordOffset` @`0x1d617c60`; BarnaCore=1 → `BarnaCorePhysicalChipBoundsLocationWordOffset` @`0x1d6183e0`; else FATAL) and unpacks the same base-`0x400` triple. The re-linearize is two `SmulU32` + three `SaddS32` (the column add is the final term).

> **NOTE —** the linearization byte-confirmed in the decompile is `SaddS32(SmulU32(SmulU32(C2, B0)+C0 via SaddS32, B1), C1)` — i.e. `((z·rows_bound + row)·cols_bound) + col`, with `B0`=row-bound, `B1`=col-bound. The `z` bound-check carries the hint `"topology must be 2d for limited ICI routing"`, implying `z` must be 0 (a 2-D pod) on limited-ICI topologies.

### 4.3 How it composes — V1 vs V2

The remap's output reaches the address only through **JfDf's bit-21 field**. The V2 encoders carry the logical chip-coordinate (`CLB[+0]`) directly into their single masked chip field and let the receive-side NIU + routing engine resolve physical placement at run time. This is the per-family divergence:

| | V1 (JfDf, gen 0/1) | V2 (PF/VF/GL, gen 2/3/4) |
|---|---|---|
| chip-id baked into address? | **physical** (remap output, bit 21) | **logical** (CLB[+0], masked) |
| where physical placement resolved | at compile time (the remap) | at run time (on-chip NIU + routing) |
| chip-coord scheme | two fields: X@20 + phys@21 | one field: chip@17/18 |
| multicast | conditional bit 19 | unsupported (PF FATAL; VF/GL drop) |

> **GOTCHA — the V2 NIU placement.** Because the V2 encoders never consume `phys_chip_id`, the `MapLogicalToPhysicalChipId` pre-pass is *dead work* for Pufferfish/Viperfish/Ghostlite — the dispatcher computes it (and could even FATAL on a bound violation) but the V2 wrappers discard it. The V2 address therefore names the chip by its *logical* coordinate; translating that logical coordinate to a physical fabric endpoint is the on-chip NIU/routing engine's job, not the compiler's. The cross-chip *data*-memref path makes the same choice — see [get_remote_memref](../collectives/get-remote-memref.md), where the peer core-id rides as a separate operand rather than being folded into the pointer.

---

## 5. The end-to-end datapath

Putting the dispatcher, the remap, and the per-gen encoders together, the cross-chip remote-SFLAG address is built in this order:

| stage | function (VMA) | output |
|---|---|---|
| consumer | `VsyncAddRemote` @`0x1d522f40` | calls the dispatcher, then `CreateVectorSyncFlagAddRemote(addr, value)` |
| MS-kSflag validate | `EncodeRemoteSyncFlagAddress` @`0x1d54da40` | RetCheck if `sflag` not in kSflag (or supported alias) MS |
| logical→physical chip remap | `MapLogicalToPhysicalChipId` @`0x1d519f40` | `phys_chip = ((z+oz)·Brow + (row+orow))·Bcol + (col+ocol)` |
| ↳ logical mesh decode | `ToChipCoordinates` @`0x1d51a3a0` | `(row,col,z)` via NCol/NRows divmod (column-fastest) |
| ↳ subslice origin | `LoadSubsliceOffset` @`0x1d519280` | base-`0x400` origin triple (per-core SMEM) |
| ↳ physical bounds | `LoadPhysicalChipBounds` @`0x1d518dc0` | base-`0x400` bounds triple (per-sequencer-type SMEM) |
| version dispatch | `GetRemoteSyncFlagEncoderRegistry().Get(ver)` @`0x1d54e020` | per-`TpuVersion` encoder closure |
| JfDf encode (gen 0/1) | `EncodeRemoteSyncFlagAddressJfDf` @`0x1d5aa620` | `sflag\|X<<20\|phys<<21\|0x40000\|0x40<<12\|(mc?0x80000)` |
| Pufferfish encode (gen 2) | `pufferfish::dma_utils::…` @`0x1d5ae1a0` | `sflag\|(0x8000\|core<<14)\|((chip&0xfff)<<18)` |
| Viperfish encode (gen 3) | `viperfish::dma_utils::…` @`0x1d5af9c0` | `sflag\|(0x8000\|core<<14)\|((chip&0x3fff)<<17)` |
| Ghostlite encode (gen 4) | `ghostlite::dma_utils::…` @`0x1d5affc0` | (byte-identical to Viperfish) |
| annotate (JfDf only) | `set_annotation_if_not_constant` | `"remote sync flag address"` (V2 impls do not annotate) |
| (consumer) emit | `CreateVectorSyncFlagAddRemote` | the remote SFLAG-add instruction with the encoded address as operand 0 |

---

## 6. Verification notes

> **[CONFIRMED]** Byte-exact in `libtpu.so` v0.0.40:
> - **Dispatcher** `EncodeRemoteSyncFlagAddress` @`0x1d54da40`: the `(sflag[+0xb]&0x7c)==0x18` kSflag gate with the SupportsRemoteSyncFlagInTpuEmbeddingSpace (vtable+0x7b0, MS ∈ {9,10}) and SupportsSparseCore (vtable+0x260, MS `0x30`) alternates; the RetCheck string `"remote_sync_flag->memory_space() == MemorySpace::kSflag || …kBarnaCoreSmem … kBarnaCoreSflag … kSparseCoreSequencerSflag"`; `MapLogicalToPhysicalChipId(…, operand "EncodeRemoteSyncFlagAddress()" [29 chars], multicast=0)`; `version = Target[+0x398]`; the registry `Get` then invoke with `(sflag, &x_coord, multicast, phys_chip_val, &builder)`; `LOG(FATAL) "Unsupported version: "` at line 8252 — exact.
> - **`VsyncAddRemote`** @`0x1d522f40`: `EncodeRemoteSyncFlagAddress` then `CreateVectorSyncFlagAddRemote(addr, value)` then `AppendInstruction` — exact.
> - **JfDf** @`0x1d5aa620`: `SshllU32(CLB[+8],0x14)`, `SshllU32(phys_chip,0x15)`, `0x40000`, `DefaultSyncFlagSegmentId()<<0xc`, conditional `0x80000` under `if(multicast)` with the `VLOG(1) "Set multicast in EncodeRemoteSyncFlagAddress"` site, annotation `"remote sync flag address"` — exact (multicast bit is CONDITIONAL).
> - **Pufferfish** @`0x1d5ae1a0`: `core_sub = CLB[+8]&3`, MS-gate `((sflag[+0xb]>>2)&0x1f)−9 ≤ 1` → `+2`, chip `(CLB[+0]&0xfff)<<0x12`, fold `(SimmU32(0x20000)+core<<0x10)>>2`, `SorU32(.,sflag)`, `SorU32(chip,.)` — exact.
> - **Viperfish** @`0x1d5af9c0`: MS-gate `(((sflag[+0xb]>>2)&0x1d)|2)==0xe`, chip `(CLB[+0]&0x3fff)<<0x11` — exact; **Ghostlite** @`0x1d5affc0` byte-identical (`0x3fff`/`<<0x11`).
> - **PF wrapper** @`0x1d5af8a0`: `if(multicast) LOG(FATAL) "is_multicast == false"` (pufferfish.cc:13) then 3-arg tail-call; **VF/GL wrappers** @`0x1d5af900`/`0x1d5b01e0` bare tail-calls dropping `phys`+`multicast`.
> - **Registrars** (address registry `r` @`0x2257e488`, `Register` @`0x1d5aa7a0`): five registrations across four TUs — `…_jellyfish.cc` @`0x2135b720` (JfDf @`0x1d5aa620`, keys `1` @`0x2135b74d` & `0` @`0x2135b7be`), `…_pufferfish.cc` @`0x2135bb30` (key `2` @`0x2135bb51`), `…_viperfish.cc` @`0x2135bc00` (key `3` @`0x2135bc21`), `ghostlite_dma_utils.cc` @`0x2135be70` (key `4` @`0x2135be95`, encoder LEA `0x1d5b01e0` @`0x2135beb2`) — exact. The jellyfish TU also seeds the core-id registry (`r` @`0x2257e468`, `Register` @`0x1d5ae3c0`) and the DMA-overrides registry (`r` @`0x2257e478`). Only JfDf annotates its result; the V2 `dma_utils` impls carry no `set_annotation` call.
> - **`MapLogicalToPhysicalChipId`** @`0x1d519f40`: gate `Target[+0x930]==1`, `!multicast && vtable[+0x18]()` passthrough, the `(opcode−219)>=0xA && !=44` + `kSubslicePhysicalChipId` annotation idempotence guard, `ToChipCoordinates → LoadSubsliceOffset → 3× SaddS32 → LoadPhysicalChipBounds → 3× ScheckLt (column/row/z) → SmulU32/SaddS32/SmulU32/SaddS32 → annotation "subslice-physical-chip-id"` — exact.
> - **`DefaultSyncFlagSegmentId`** @`0x1d62da60` = `0x40` (`mov eax,0x40; ret`).
>
> **[HIGH / not separately field-named]**
> - The exact bit boundary between `core_sub` (bits 14-16) and the `0x8000` segment marker (bit 15) in the V2 fold: the fold `(0x8000 | core_sub<<0xe)` is arithmetically exact, but whether bit 15 is a fixed segment marker independent of `core_sub` (which can itself reach 5 → set bits 15/16) was not separately pinned. The encoder OR/ADDs them, so they coexist in the same field region.
> - The `Target+0x930` enable byte and the `vtable[+0x18]` capability virtual on the `Target` config sub-object: the offset and the call are byte-confirmed; the field/method *names* (a "map logical→physical enable" and an "is non-subslice / logical==physical" predicate) are attributed from the `kSubslice*` annotation use, not from a proto/RTTI source.
> - The admitted-MS set for the VF/GL `+2` gate (`(((MS>>2)&0x1d)|2)==0xe`) vs PF's `{9,10}` is byte-confirmed but not enumerated against the full `MemorySpace` enum.

---

## Cross-References

**Barrier subsystem (this section)**
- [Barriers and Sync-Flags — Section Map](overview.md) — the SFLAG-barrier model and the producer→normaliser→lowering flow this encoder is the cross-chip leg of.
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — the local SFLAG *number* formulas (`base+count+N`); this page is their cross-chip *address* companion.
- [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) — the per-`(codename, deployment)` reserved SFLAG integers that the local sflag operand draws from.
- [Replica Barrier](replica-barrier.md) — the within-replica-group tree barrier whose cross-chip arm uses these encoders.

**Sibling subsystems**
- [get_remote_memref](../collectives/get-remote-memref.md) — the cross-chip *data* memref (base-pointer re-tag only; peer id rides as a separate operand) — the same "logical id at the boundary, NIU resolves placement" model for data rather than sflags.
- [StartRemoteDma](../collectives/start-remote-dma.md) — the all-to-all remote-DMA producer + `SubsliceToFullSliceGlobalCoreId`, the transfer that consumes a remote base.
- [back to index](../index.md)
