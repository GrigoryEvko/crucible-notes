# PCI Device IDs

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B). Other versions differ.*

## Abstract

Every Google TPU exposes itself on PCIe under vendor ID `0x1ae0` (Google Inc.). libtpu identifies the silicon generation not from a single device ID but from a 12-byte `asic_sw::DeviceIdentifiers` record that pins **two** device IDs — a header/function DID and a chip DID — plus a vendor ID, a chip-revision mask, and a revision value. A small family of recognizer functions in the `xprof::tpu` namespace compares an incoming record against the compiled-in `k*Identifiers` records and maps a match to an internal **device-type** integer; `DeviceTypeFromDeviceIdentifiers` is the single funnel that turns a record into that integer.

The device-type integer is a separate enum from `TpuVersion`. It is the profiler/identity axis: each codename (and each chip variant within a codename) gets its own device-type value, so the device-type space is wider than the six-value `TpuVersion` space. This page documents the record layout, the full DID table per codename and variant, and the recognizer → device-type funnel — enough to reimplement TPU PCIe enumeration and codename attribution from a raw config-space read.

For reimplementation, the contract is:

- The `DeviceIdentifiers` 12-byte record layout and which fields each recognizer actually compares (the 8-byte ID dword and the offset-11 revision byte — not the offset-8 mask).
- The per-codename / per-variant hdrDID + chipDID + revision table, including the two management-PF records that no recognizer matches.
- The `DeviceTypeFromDeviceIdentifiers` dispatch order and the literal device-type constant each branch stores.

| | |
|---|---|
| **Vendor ID (all TPUs)** | `0x1ae0` (Google Inc.) |
| **Record type** | `asic_sw::DeviceIdentifiers`, 12 meaningful bytes (8-byte ID dword + 4-byte rev word) |
| **Record layout** | `[VID:2][hdrDID:2][subVID:2][chipDID:2][revmask:1][pad:2][rev:1]`, little-endian |
| **Dispatch funnel** | `xprof::tpu::DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0` |
| **Called recognizers** | `IsGlc` @ `0xf6992a0` (Ghostlite, `TpuVersion` 4), `IsGfc` @ `0xf699320` (6acc60406, `TpuVersion` 5) |
| **Reverse map** | `xprof::tpu::DeviceIdentifiersFromDeviceType` @ `0xf6996e0` — device-type int → canonical record |
| **Identifier-record table base** | `0x0bdf3c0c` (`kJellyfishIdentifiers`) … `0x0bdf3ce8` (end tag `s_44716`) |

---

## DeviceIdentifiers Record Layout

Each `k*Identifiers` record carries 12 meaningful bytes in `.rodata`: an 8-byte ID dword (VID‖hdrDID‖subVID‖chipDID) followed by a 4-byte revision word. `asic_sw::driver::deepsea::jxc::Driver::GetDeviceIdentifiers` (`0xe794560`) copies exactly these into a live record — `*(_QWORD *)v3` (the 8-byte ID dword) and `*(_DWORD *)(v3 + 8)` (the 4-byte rev word) — confirming the 8+4 split. The recognizers read the record back as one 64-bit dword (`*(_QWORD *)record`, the ID dword) plus a single revision byte at **offset 11** (`*((_BYTE *)record + 11)`). Reading the Ghostlite App PF record at `0x0bdf3ca0`:

```text
0bdf3ca0:  e0 1a | 6e 00 | e0 1a | d1 00 | 12 | 00 00 | 00
           VID     hdrDID   subVID  chipDID  msk  pad     rev
           0x1ae0  0x006e   0x1ae0  0x00d1  0x12 ----   0x00
           offset 0  2       4        6       8   9 10    11
```

| Field | Offset | Width | Meaning |
|---|---|---|---|
| VID | 0 | 2 | PCI vendor ID — always `0x1ae0` (Google) |
| hdrDID | 2 | 2 | Header / PCI-function device ID (distinguishes PF / VF / management-PF) |
| subVID | 4 | 2 | Subsystem vendor ID — `0x1ae0` again |
| chipDID | 6 | 2 | Chip device ID — the silicon-generation discriminator |
| revmask | 8 | 1 | Chip-revision mask — `0xff` for every codename except Ghostlite (`0x12`) |
| (pad) | 9 | 2 | Zero padding inside the 4-byte rev word |
| rev | 11 | 1 | Revision-equality byte — the term the recognizers compare (`v4 == record[11]`) |

> **GOTCHA —** the chip DID alone does **not** identify a device. All three Ghostlite functions (App PF / App VF / Mgt PF) share chip DID `0x00d1`; they differ only by hdrDID (`0x006e`/`0x006f`/`0x0070`). A reimplementation that keys on chip DID only will collapse the three PCI functions into one and mis-route the management interface. The recognizers compare the **full** record (VID, hdrDID via the low dword, chipDID via the 48-bit-shift term, and the offset-11 revision byte), not the chip DID in isolation.

> **GOTCHA —** the byte the recognizers test for revision is at offset **11**, not offset 8. Offset 8 holds the chip-revision *mask* (`revmask`); offsets 9–10 are zero padding; offset 11 is the revision-equality byte. For Ghostlite that distinction is sharp: `revmask` (offset 8) is `0x12`, while the revision-equality byte (offset 11) is `0x00`. A reimplementation that compares offset 8 against a per-record revision will mis-match Ghostlite, which is the one generation whose mask differs from `0xff`.

The recognizer comparison is a four-term `&&` (seen verbatim in `IsGfc`): the low dword equals the matching record's low dword (VID+hdrDID), the chip-VID word `WORD2 == 0x1ae0` (decimal `6880`), an `(record ^ expected) >> 48 == 0` term that pins the chip DID in the high word, and `record[11] == expected_rev`. The same shape repeats inline for every branch in the dispatch funnel.

---

## DID Table

Read directly from the `.rodata` identifier-record block at `0x0bdf3c0c`–`0x0bdf3ce8`. Header/chip DIDs and the two revision bytes are decoded from the bytes; the device-type column is the literal constant stored by `DeviceTypeFromDeviceIdentifiers` (see below). VID is `0x1ae0` for every row. The `External name` column is the `TpuVersionToExternalName` display string, **not** the internal `TpuVersion` integer (Ghostlite is `TpuVersion` 4 but displays as `TPU v6 lite`; 6acc60406 is `TpuVersion` 5 but displays as `TPU7x`) — for the codename ↔ `TpuVersion` ↔ external-name mapping see [Codename Matrix](tpu-version-codename-matrix.md).

The `rev-mask` column is the byte at record offset 8; `rev[11]` is the revision-equality byte at offset 11 that the recognizer actually compares (see the layout above). Every record's `revmask`/`rev[11]` pair was read directly from `.rodata`. The `External name` is the nominal `TpuVersionToExternalName` string for the row's `TpuVersion`; the `lite` discriminator is a *runtime* variant `string_view` (not part of the PCI record), so the `lite` codenames (Puffylite, Viperlite) display as `… lite` only when that variant arg is supplied.

| Codename / variant | External name | hdrDID | chipDID | rev-mask | rev[11] | Device-type | Record addr | Recognized? |
|---|---|---|---|---|---|---|---|---|
| Jellyfish | TPU v2 | `0x0027` | `0x004e` | `0xff` | `0x00` | **3** | `0xbdf3c0c` | funnel (inline) |
| Dragonfish | TPU v3 | `0x0027` | `0x004f` | `0xff` | `0x00` | **5** | `0xbdf3c18` | funnel (inline) |
| Pufferfish B0 Mfg | TPU v4 | `0x005e` | `0x0050` | `0xff` | `0x10` | **7** | `0xbdf3c28` | funnel (inline) |
| Pufferfish B0 Water | TPU v4 | `0x005e` | `0x0051` | `0xff` | `0x10` | **7** | `0xbdf3c34` | funnel (inline) |
| Pufferfish B0 Air | TPU v4 | `0x005e` | `0x0052` | `0xff` | `0x10` | **7** | `0xbdf3c40` | funnel (inline) |
| Puffylite (pxc::plc) | TPU v4 lite | `0x0056` | `0x007b` | `0xff` | `0x00` | **8** | `0xbdf3c4c` | funnel (inline) |
| Viperlite A0 PF | TPU v5 lite | `0x0063` | `0x00ae` | `0xff` | `0x00` | **11** | `0xbdf3c58` | funnel (inline) |
| Viperlite A0 VF | TPU v5 lite | `0x0063` | `0x00ae` | `0xff` | `0x01` | **11** | `0xbdf3c64` | funnel (inline) |
| Viperlite A1 PF | TPU v5 lite | `0x0063` | `0x00af` | `0xff` | `0x00` | **11** | `0xbdf3c70` | funnel (inline) |
| Viperlite A1 VF | TPU v5 lite | `0x0063` | `0x00af` | `0xff` | `0x01` | **11** | `0xbdf3c7c` | funnel (inline) |
| Viperfish PF | TPU v5 | `0x0062` | `0x00ac` | `0xff` | `0x00` | **10** | `0xbdf3c88` | funnel (inline) |
| Viperfish VF | TPU v5 | `0x0062` | `0x00ad` | `0xff` | `0x00` | **10** | `0xbdf3c94` | funnel (inline) |
| Ghostlite App PF | TPU v6 lite | `0x006e` | `0x00d1` | `0x12` | `0x00` | **13 (0xd)** | `0xbdf3ca0` | `IsGlc` |
| Ghostlite App VF | TPU v6 lite | `0x006f` | `0x00d1` | `0x12` | `0x00` | **13 (0xd)** | `0xbdf3cac` | `IsGlc` |
| Ghostlite Mgt PF | TPU v6 lite | `0x0070` | `0x00d1` | `0x12` | `0x00` | (none) | `0xbdf3cb8` | not recognized |
| 6acc60406 PF | TPU7x | `0x0075` | `0x00f2` | `0xff` | `0x00` | **12 (0xc)** | `0xbdf3cc4` | `IsGfc` |
| 6acc60406 VF | TPU7x | `0x0076` | `0x00f2` | `0xff` | `0x00` | **12 (0xc)** | `0xbdf3cd0` | `IsGfc` |
| 6acc60406 Mgt PF | TPU7x | `0x0077` | `0x00f2` | `0xff` | `0x00` | (none) | `0xbdf3cdc` | not recognized |

> **QUIRK —** the two management-PF records (`0x0070`/Ghostlite, `0x0077`/6acc60406) are present in the `.rodata` table but **no device-type recognizer matches them**. `IsGlc` compares only `kGhostliteChipAppPFIdentifiers` (`0x006e`) and `kGhostliteChipAppVFIdentifiers` (`0x006f`); `IsGfc` compares only the `0x0075` (PF) and `0x0076` (VF) dwords. `kGhostliteChipMgtPFIdentifiers` is referenced from the HAL `hardware_attributes_factory.cc` init module, not from the `xprof::tpu` recognizers — the management function is registered for HAL routing but is intentionally not assigned a profiler device-type. A reimplementation that feeds a Mgt-PF record through `DeviceTypeFromDeviceIdentifiers` gets the `"Unsupported device identifiers"` error, by design.

The byte block confirming the two newest generations, read straight from the binary:

```text
                  ID dword          rev word          decode
0bdf3ca0: e01a 6e00 e01a d100  1200 0000  Ghostlite App PF  hdr 006e chip 00d1 msk 12 rev[11] 00
0bdf3cac: e01a 6f00 e01a d100  1200 0000  Ghostlite App VF  hdr 006f chip 00d1 msk 12 rev[11] 00
0bdf3cb8: e01a 7000 e01a d100  1200 0000  Ghostlite Mgt PF  hdr 0070 chip 00d1 msk 12 rev[11] 00
0bdf3cc4: e01a 7500 e01a f200  ff00 0000  6acc60406 PF      hdr 0075 chip 00f2 msk ff rev[11] 00
0bdf3cd0: e01a 7600 e01a f200  ff00 0000  6acc60406 VF      hdr 0076 chip 00f2 msk ff rev[11] 00
0bdf3cdc: e01a 7700 e01a f200  ff00 0000  6acc60406 Mgt PF  hdr 0077 chip 00f2 msk ff rev[11] 00
0bdf3ce8: 73 5f 34 34 37 31 36 00         "s_44716\0"  (block-end tag)
```

> **QUIRK —** Ghostlite (`TpuVersion` 4) is the only generation whose offset-8 `revmask` is `0x12` rather than `0xff`. That mask byte is a stored field — it is **not** read by the recognizers, which key on the offset-11 revision-equality byte (which is `0x00` for every Ghostlite record). The `0x12` is nonetheless a reliable Ghostlite fingerprint in the static record. A reimplementation that mirrors the recognizer logic should compare byte 11, not byte 8; one that wants to validate the whole stored record should expect `0x12` at byte 8 for Ghostlite and `0xff` elsewhere.

> **NOTE —** the Ghostlite records are named symbols — `asic_sw::deepsea::gxc::glc::kGhostliteChip{AppPF,AppVF,MgtPF}Identifiers` (all three, including Mgt PF) — and `IsGlc` references the App PF / App VF symbols. The 6acc60406 records are **anonymous** (no `k6acc60406*Identifiers` symbol exists in the binary); `IsGfc` compares against literal immediates (`0x751ae0` for PF, `0x761ae0` for VF, the chip-DID term `0x00f2` folded into the `0xf21ae000761ae0` xor mask) loaded inline. The reverse map `DeviceIdentifiersFromDeviceType` reaches the anonymous v5 record through `&dword_BDF3CD0` (the VF record at `0xbdf3cd0`), which is the only handle the binary keeps to it as a symbol. The trailing `s_44716` tag string at `0xbdf3ce8` marks the end of the anonymous gfc records.

---

## Recognizers — `IsGlc` and `IsGfc`

### Purpose

The two latest-generation recognizers answer "is this record a Ghostlite (glc) device?" and "is this record a 6acc60406 (gfc) device?" They are the only two recognizers the dispatch funnel **calls** — every older codename is matched by an inline comparison in the funnel body (see below), and the older sub-core families have parallel standalone recognizers (`IsDfc`, `IsPlc`, `IsPfc`, `IsVlc`, `IsVfc`) that the funnel does not use. Both return a `bool`. Each recognizes the **App PF** and **App VF** functions only; neither matches the management-PF record.

### Algorithm

```c
bool IsGfc(record):                          // 0xf699320
    id   = *(uint64_t*)record                // VID|hdrDID|subVID|chipDID dword
    rev  = record[11]                         // revision-equality byte (offset 11)
    // VF first (the fast branch): hdrDID 0x0076, chip 0x00f2
    if (id & 0xffffffff)==0x761ae0 && WORD2(id)==0x1ae0
       && ((id ^ 0xF21AE000761AE0) >> 48)==0 && rev==*(byte*)0xbdf3cdb:   // VF rev[11] = 0x00
        return true                           // chip DID 0x00f2 pinned in high word
    // PF fallback: hdrDID 0x0075, chip 0x00f2
    return (id & 0xffffffff)==0x751ae0 && WORD2(id)==0x1ae0
       && ((id ^ 0xF21AE000751AE0) >> 48)==0 && rev==*(byte*)0xbdf3ccf    // PF rev[11] = 0x00
    // Mgt PF (hdrDID 0x0077) is NOT compared — falls through to false

bool IsGlc(record):                           // 0xf6992a0
    // identical shape, comparing against the NAMED
    // kGhostliteChipAppVFIdentifiers (fast branch, hdrDID 0x006f)
    // and kGhostliteChipAppPFIdentifiers (fallback, hdrDID 0x006e),
    // chip DID 0x00d1, rev[11]==0x00 for both.
    // Mgt PF (hdrDID 0x0070) is NOT compared.
    ...
```

The `0xF21AE000761AE0` immediate decodes little-endian as `e0 1a 76 00 00 e0 1a f2`: VID `0x1ae0`, hdrDID `0x0076`, subVID `0x1ae0`, chipDID `0x00f2`. The `>> 48` term isolates the top 16 bits (the chip DID `0x00f2`), so the comparison pins both the function DID and the chip DID at once. The decimal forms the decompiler shows are `7740128` (`0x761ae0`, the VF fast branch) and `7674592` (`0x751ae0`, the PF fallback).

### Function Map

| Function | Address | Role |
|---|---|---|
| `xprof::tpu::IsGlc` | `0xf6992a0` | Recognize Ghostlite App PF/VF records — chip `0x00d1` (`TpuVersion` 4) |
| `xprof::tpu::IsGfc` | `0xf699320` | Recognize 6acc60406 App PF/VF records — chip `0x00f2` (`TpuVersion` 5) |
| `xprof::tpu::DeviceTypeFromDeviceIdentifiers` | `0xf6993a0` | Map a record to its device-type integer (calls `IsGlc`/`IsGfc` last) |
| `xprof::tpu::DeviceIdentifiersFromDeviceType` | `0xf6996e0` | Reverse: device-type int → one canonical record per generation |
| `xprof::tpu::{IsDfc,IsPlc,IsPfc,IsVlc,IsVfc}` | `0xf699000`, `0xf699040`, `0xf699080`, `0xf699140`, `0xf699220` | Standalone per-codename recognizers (not called by the funnel) |

> **NOTE —** `DeviceIdentifiersFromDeviceType` (`0xf6996e0`) is the inverse map. For each device-type integer it returns one representative record: 3→Jellyfish, 5→Dragonfish, 7→Pufferfish B0 Water, 8→Puffylite, 10→Viperfish VF, 11→Viperlite A0 VF, 12→the anonymous 6acc60406 VF record (`&dword_BDF3CD0`), 13→Ghostlite App VF. An unknown device-type returns `"Unsupported device type"` (`device_identifiers_utils.cc:191`). Note it is not a true inverse — a generation with several variant records collapses to a single canonical one (e.g. all three Pufferfish B0 variants map back only to Water).

---

## `DeviceTypeFromDeviceIdentifiers` — the Funnel

### Purpose

This is the single function that turns a `DeviceIdentifiers` record into the profiler's device-type integer. It is a flat `if/else if` chain of inline record comparisons; the older codenames are tested first by direct equality, and the two newest generations fall through to `IsGlc` / `IsGfc`. On no match it returns an `absl::Status` error (`"Unsupported device identifiers"`, `device_identifiers_utils.cc:152`).

### Algorithm

```c
DeviceType DeviceTypeFromDeviceIdentifiers(record):    // 0xf6993a0
    if matches(kJellyfishIdentifiers):     return 3
    if matches(kDragonfishIdentifiers):    return 5
    if matches(kPuffyliteChipIdentifiers): return 8
    if matches(PufferfishB0{Mfg,Water,Air}): return 7
    if matches(Viperlite{A0,A1}{PF,VF}):   return 11
    if matches(Viperfish{PF,VF}):          return 10
    if IsGlc(record):                      return 13   // 0xd — Ghostlite (TpuVersion 4)
    if IsGfc(record):                      return 12   // 0xc — 6acc60406 (TpuVersion 5)
    return Error("Unsupported device identifiers")      // :152
```

> **NOTE —** the Ghostlite→`0xd` / 6acc60406→`0xc` device-type binding is pinned to a literal store: `DeviceTypeFromDeviceIdentifiers` writes the constants directly — `*(_DWORD*)(result+8) = 13` on the `IsGlc` branch and `= 12` on the `IsGfc` branch. The 6acc60406 PCI records exist in `.rodata` at `0xbdf3cc4`–`0xbdf3ce8` (anonymous, no symbol), and the v5 chip DID `0x00f2` is confirmed both in those bytes and in the `IsGfc` immediates.

### Device-Type Map

The full device-type integer space recovered from the funnel. Note device-type is denser than `TpuVersion`: chip variants within one codename collapse to one device-type (all Pufferfish B0 → 7; all Viperlite → 11), and pre-production Puffylite gets its own value (8).

| Device-type | Codename / family | TpuVersion |
|---|---|---|
| 3 | Jellyfish | 0 |
| 5 | Dragonfish | 1 |
| 7 | Pufferfish (B0 Mfg/Water/Air) | 2 |
| 8 | Puffylite (pxc::plc pre-production) | (2 family) |
| 10 | Viperfish (PF/VF) | 3 |
| 11 | Viperlite (A0/A1 PF/VF) | (3 family) |
| 12 (`0xc`) | 6acc60406 (gfc) | 5 |
| 13 (`0xd`) | Ghostlite (glc) | 4 |

> **QUIRK —** the device-type integers are not in `TpuVersion` order, and Ghostlite (`TpuVersion` 4) gets the **higher** device-type (13) while 6acc60406 (`TpuVersion` 5) gets 12. Device-type is assigned by recognizer evaluation order and identity convenience, not by chronology. A reimplementation must not infer generation ordering from the device-type value; use the chip-DID → `TpuVersion` mapping for that. Likewise, the funnel tests Puffylite (8) *before* the Pufferfish B0 variants (7) — the inline-comparison order does not follow the device-type integers either.

---

## DID → TpuVersion Lookup Path

A raw config-space read resolves to a `TpuVersion` through this chain:

```text
PCI config (VID 0x1ae0, hdrDID, chipDID, rev)
  └─ build asic_sw::DeviceIdentifiers (12 B)
       └─ DeviceTypeFromDeviceIdentifiers (0xf6993a0)  ── record → device-type int
            ├─ direct record compares (Jellyfish … Viperfish)
            └─ IsGlc (0xf6992a0) / IsGfc (0xf699320)    ── Ghostlite / 6acc60406
       device-type → codename → TpuVersion (table above)
```

The codename → `TpuVersion` direction is fixed by the `TpuVersionToString` rel.ro pointer table at `0x22011bf0` (six `R_X86_64_RELATIVE` relocations naming `jellyfish`…`6acc60406` in enum order); see [Codename Matrix](tpu-version-codename-matrix.md). The chip-DID → codename binding is what this page's DID table supplies.

---

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — the `TpuVersion` enum ↔ codename source-of-truth table the device-type column maps into
- [Marketing / Cloud Naming](marketing-cloud-naming.md) — codename ↔ Cloud-TPU display name (the v6e/TPU7x cross-walk)
- [Superseded-Label Correction List](codename-superseded-labels.md) — the v5 chip-DID and device-type label pinning
- [HAL Families](hal-families.md) — the JXC/PXC/VXC factory routing that consumes the recognized device
- [Chip Parts binarypb](chip-parts-binarypb.md) — the embedded per-chip proto that carries DID and core-count metadata
