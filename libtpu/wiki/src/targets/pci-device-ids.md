# PCI Device IDs

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B). Other versions differ.*

## Abstract

Every Google TPU exposes itself on PCIe under vendor ID `0x1ae0` (Google Inc.). libtpu identifies the silicon generation not from a single device ID but from a 12-byte `asic_sw::DeviceIdentifiers` record that pins **two** device IDs — a header/function DID and a chip DID — plus a vendor ID, a chip-revision mask, and a revision value. A small family of recognizer functions in the `xprof::tpu` namespace compares an incoming record against the compiled-in `k*Identifiers` records and maps a match to an internal **device-type** integer; `DeviceTypeFromDeviceIdentifiers` is the single funnel that turns a record into that integer.

The device-type integer is a separate enum from `TpuVersion`. It is the profiler/identity axis: each codename (and each chip variant within a codename) gets its own device-type value, so the device-type space is wider than the six-value `TpuVersion` space. This page documents the record layout, the full DID table per codename and variant, and the recognizer → device-type funnel — enough to reimplement TPU PCIe enumeration and codename attribution from a raw config-space read.

For reimplementation, the contract is:

- The `DeviceIdentifiers` 12-byte wire layout and how each field is compared.
- The per-codename / per-variant DID + rev-mask table.
- The `DeviceTypeFromDeviceIdentifiers` dispatch order and the literal device-type constant each branch stores.

| | |
|---|---|
| **Vendor ID (all TPUs)** | `0x1ae0` (Google Inc.) |
| **Record type** | `asic_sw::DeviceIdentifiers`, 12 bytes |
| **Record layout** | `[VID:2][hdrDID:2][subVID:2][chipDID:2][revmask:1][rev:3]`, little-endian |
| **Dispatch funnel** | `xprof::tpu::DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0` |
| **Latest-gen recognizers** | `IsGlc` @ `0xf6992a0` (Ghostlite/v4), `IsGfc` @ `0xf699320` (6acc60406/v5) |
| **Identifier-record table base** | `0x0bdf3c0c` (`kJellyfishIdentifiers`) … `0x0bdf3ce8` (end tag `s_44716`) |

---

## DeviceIdentifiers Record Layout

Each `k*Identifiers` record is 12 contiguous bytes in `.rodata`. The recognizers read it as a single 64-bit dword (`*(_QWORD *)record`) plus a separate revision byte at offset 11. Reading the Ghostlite App PF record at `0x0bdf3ca0`:

```text
0bdf3ca0:  e0 1a | 6e 00 | e0 1a | d1 00 | 12 | 00 00 00
           VID     hdrDID   subVID  chipDID  rev  rev[3]
           0x1ae0  0x006e   0x1ae0  0x00d1   0x12
```

| Field | Offset | Width | Meaning |
|---|---|---|---|
| VID | 0 | 2 | PCI vendor ID — always `0x1ae0` (Google) |
| hdrDID | 2 | 2 | Header / PCI-function device ID (distinguishes PF / VF / management-PF) |
| subVID | 4 | 2 | Subsystem vendor ID — `0x1ae0` again |
| chipDID | 6 | 2 | Chip device ID — the silicon-generation discriminator |
| revmask | 8 | 1 | Chip-revision mask used as a fourth equality term |
| rev | 9 | 3 | Revision value (`A1` variants set the low byte of this field to `0x01`) |

> **GOTCHA —** the chip DID alone does **not** identify a device. All three Ghostlite functions (App PF / App VF / Mgt PF) share chip DID `0x00d1`; they differ only by hdrDID (`0x006e`/`0x006f`/`0x0070`). A reimplementation that keys on chip DID only will collapse the three PCI functions into one and mis-route the management interface. The recognizers compare the **full** record (VID, hdrDID via the 48-bit-shift term, chipDID, and the revision byte), not the chip DID in isolation.

The recognizer comparison is a four-term `&&` (seen verbatim in `IsGfc`): the low dword equals the matching record's low dword (VID+hdrDID), the chip-VID word `WORD2 == 0x1ae0` (decimal `6880`), an `(record ^ expected) >> 48 == 0` term that pins the chip DID in the high word, and `rev == expected_rev`. The same shape repeats for every branch in the dispatch funnel.

---

## DID Table

Read directly from the `.rodata` identifier-record block at `0x0bdf3c0c`–`0x0bdf3ce8`. Header/chip DIDs and rev-mask are decoded from the bytes; the device-type column is the literal constant stored by `DeviceTypeFromDeviceIdentifiers` (see below). VID is `0x1ae0` for every row. The parenthetical in the first column is the **external display name** (`TPU vN`), not the internal `TpuVersion` integer — for the codename ↔ `TpuVersion` mapping see [Codename Matrix](tpu-version-codename-matrix.md).

| Codename / variant | hdrDID | chipDID | rev-mask | Device-type | Record addr | Confidence |
|---|---|---|---|---|---|---|
| Jellyfish (v2) | `0x0027` | `0x004e` | `0xff` | **3** | `0xbdf3c0c` | CERTAIN |
| Dragonfish (v3) | `0x0027` | `0x004f` | `0xff` | **5** | `0xbdf3c18` | CERTAIN |
| Pufferfish B0 Mfg (v4) | `0x005e` | `0x0050` | `0xff` (+rev `0x10`) | **7** | `0xbdf3c28` | CERTAIN |
| Pufferfish B0 Water (v4) | `0x005e` | `0x0051` | `0xff` (+rev `0x10`) | **7** | `0xbdf3c34` | CERTAIN |
| Pufferfish B0 Air (v4) | `0x005e` | `0x0052` | `0xff` (+rev `0x10`) | **7** | `0xbdf3c40` | CERTAIN |
| Puffylite (pxc::plc) | `0x0056` | `0x007b` | `0xff` | **8** | `0xbdf3c4c` | CERTAIN |
| Viperlite A0 PF | `0x0063` | `0x00ae` | `0xff` | **11** | `0xbdf3c58` | CERTAIN |
| Viperlite A0 VF | `0x0063` | `0x00ae` | `0xff` | **11** | `0xbdf3c64` | CERTAIN |
| Viperlite A1 PF | `0x0063` | `0x00af` | `0xff` (rev `0x01`) | **11** | `0xbdf3c70` | CERTAIN |
| Viperlite A1 VF | `0x0063` | `0x00af` | `0xff` (rev `0x01`) | **11** | `0xbdf3c7c` | CERTAIN |
| Viperfish PF (TPU v5) | `0x0062` | `0x00ac` | `0xff` | **10** | `0xbdf3c88` | CERTAIN |
| Viperfish VF (TPU v5) | `0x0062` | `0x00ad` | `0xff` | **10** | `0xbdf3c94` | CERTAIN |
| Ghostlite App PF (v6 lite) | `0x006e` | `0x00d1` | `0x12` | **13 (0xd)** | `0xbdf3ca0` | CERTAIN |
| Ghostlite App VF (v6 lite) | `0x006f` | `0x00d1` | `0x12` | **13 (0xd)** | `0xbdf3cac` | CERTAIN |
| Ghostlite Mgt PF (v6 lite) | `0x0070` | `0x00d1` | `0x12` | **13 (0xd)** | `0xbdf3cb8` | CERTAIN |
| 6acc60406 PF (TPU7x) | `0x0075` | `0x00f2` | `0xff` | **12 (0xc)** | `0xbdf3cc4` | CERTAIN |
| 6acc60406 VF (TPU7x) | `0x0076` | `0x00f2` | `0xff` | **12 (0xc)** | `0xbdf3cd0` | CERTAIN |
| 6acc60406 Mgt PF (TPU7x) | `0x0077` | `0x00f2` | `0xff` | **12 (0xc)** | `0xbdf3cdc` | CERTAIN |

The byte block confirming the two newest generations, read straight from the binary:

```text
0bdf3ca0: e01a 6e00 e01a d100 1200 0000  Ghostlite App PF  hdr 006e chip 00d1 rev 12
0bdf3cac: e01a 6f00 e01a d100 1200 0000  Ghostlite App VF  hdr 006f chip 00d1 rev 12
0bdf3cb8: e01a 7000 e01a d100 1200 0000  Ghostlite Mgt PF  hdr 0070 chip 00d1 rev 12
0bdf3cc4: e01a 7500 e01a f200 ff00 0000  6acc60406 PF      hdr 0075 chip 00f2 rev ff
0bdf3cd0: e01a 7600 e01a f200 ff00 0000  6acc60406 VF      hdr 0076 chip 00f2 rev ff
0bdf3cdc: e01a 7700 e01a f200 ff00 0000  6acc60406 Mgt PF  hdr 0077 chip 00f2 rev ff
0bdf3ce8: 73 5f 34 34 37 31 36 00       "s_44716\0"  (block-end tag)
```

> **QUIRK —** Ghostlite (v6 lite) is the only generation whose rev-mask is `0x12` rather than `0xff`. Every other production codename masks the full revision byte (`0xff`); the recognizers compare the masked revision against a per-record constant. The Ghostlite App PF record is the source of the `0x12` value compared in `IsGlc`. A reimplementation that hard-codes `0xff` for all generations will fail to recognize Ghostlite silicon.

> **NOTE —** the Ghostlite records are named symbols — `asic_sw::deepsea::gxc::glc::kGhostliteChip{AppPF,AppVF,MgtPF}Identifiers` — and `IsGlc` references them by symbol. The 6acc60406 records are **anonymous** (no `k6acc60406*Identifiers` symbol); `IsGfc` compares against literal immediates (`0x751ae0`, `0x761ae0`, the chip-DID term `0x00f2`) loaded inline. The trailing `s_44716` tag string at `0xbdf3ce8` marks the end of the anonymous gfc records.

---

## Recognizers — `IsGlc` and `IsGfc`

### Purpose

The two latest-generation recognizers answer "is this record a Ghostlite (glc) device?" and "is this record a 6acc60406 (gfc) device?" They are the leaf comparators the dispatch funnel falls through to after the older codenames fail to match. Both return a `bool`.

### Algorithm

```c
bool IsGfc(record):                          // 0xf699320
    low  = *(uint64_t*)record                // VID|hdrDID|subVID|chipDID
    rev  = record[11]                         // revision byte
    // PF: hdrDID 0x0075, chip 0x00f2, expected dword 0x...751ae0
    if (low&0xffffffff)==0x751ae0 && WORD2(low)==0x1ae0
       && ((low ^ 0xF21AE000751AE0) >> 48)==0 && rev==revPF:   // chip DID 0x00f2 pinned in high word
        return true
    // VF: hdrDID 0x0076, chip 0x00f2, expected dword 0x...761ae0
    if (low&0xffffffff)==0x761ae0 && WORD2(low)==0x1ae0
       && ((low ^ 0xF21AE000761AE0) >> 48)==0 && rev==revVF:
        return true
    return false                              // (Mgt PF 0x0077 handled in same chain)

bool IsGlc(record):                           // 0xf6992a0
    // identical shape, comparing against the NAMED
    // kGhostliteChipAppPFIdentifiers / kGhostliteChipAppVFIdentifiers
    // records (chip DID 0x00d1, rev-mask 0x12)
    ...
```

The `0xF21AE000751AE0` immediate decodes little-endian as `e0 1a 75 00 00 e0 1a f2`: VID `0x1ae0`, hdrDID `0x0075`, subVID `0x1ae0`, chipDID `0x00f2`. The `>> 48` term isolates the top 16 bits (the chip DID `0x00f2`), so the comparison pins both the function DID and the chip DID at once.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `xprof::tpu::IsGlc` | `0xf6992a0` | Recognize Ghostlite (v4) records — chip `0x00d1`, rev-mask `0x12` | CERTAIN |
| `xprof::tpu::IsGfc` | `0xf699320` | Recognize 6acc60406 (v5) records — chip `0x00f2`, rev-mask `0xff` | CERTAIN |
| `xprof::tpu::DeviceTypeFromDeviceIdentifiers` | `0xf6993a0` | Map a record to its device-type integer | CERTAIN |

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
    if IsGlc(record):                      return 13   // 0xd — Ghostlite / v4
    if IsGfc(record):                      return 12   // 0xc — 6acc60406 / v5
    return Error("Unsupported device identifiers")      // :152
```

> **CORRECTION (DID-01) —** earlier analysis listed the Ghostlite→`0xd` / 6acc60406→`0xc` device-type binding as cross-referenced (not pinned to a literal store), and recorded the v5 chip DID as "not recovered as a direct PCI record". Both are now byte-level facts. `DeviceTypeFromDeviceIdentifiers` stores the constants directly — `*(_DWORD*)(result+8) = 13` on the `IsGlc` branch and `= 12` on the `IsGfc` branch — and the 6acc60406 records exist in `.rodata` at `0xbdf3cc4`–`0xbdf3ce8` (anonymous, no symbol). The chip DID `0x00f2` is confirmed both in those bytes and in the `IsGfc` immediates.

### Device-Type Map

The full device-type integer space recovered from the funnel. Note device-type is denser than `TpuVersion`: chip variants within one codename collapse to one device-type (all Pufferfish B0 → 7; all Viperlite → 11), and pre-production Puffylite gets its own value (8).

| Device-type | Codename / family | TpuVersion | Confidence |
|---|---|---|---|
| 3 | Jellyfish | 0 | CERTAIN |
| 5 | Dragonfish | 1 | CERTAIN |
| 7 | Pufferfish (B0 Mfg/Water/Air) | 2 | CERTAIN |
| 8 | Puffylite (pxc::plc pre-production) | (2 family) | CERTAIN |
| 10 | Viperfish (PF/VF) | 3 | CERTAIN |
| 11 | Viperlite (A0/A1 PF/VF) | (3 family) | CERTAIN |
| 12 (`0xc`) | 6acc60406 (gfc) | 5 | CERTAIN |
| 13 (`0xd`) | Ghostlite (glc) | 4 | CERTAIN |

> **QUIRK —** the device-type integers are not in `TpuVersion` order, and Ghostlite (v4) gets the **higher** device-type (13) while 6acc60406 (v5) gets 12. Device-type is assigned by recognizer evaluation order and identity convenience, not by chronology. A reimplementation must not infer generation ordering from the device-type value; use the chip-DID → `TpuVersion` mapping for that.

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

The codename → `TpuVersion` direction is fixed by the `TpuVersionToString` rel.ro pointer table at `0x22011BF0` (six `R_X86_64_RELATIVE` relocations naming `jellyfish`…`6acc60406` in enum order); see [Codename Matrix](tpu-version-codename-matrix.md). The chip-DID → codename binding is what this page's DID table supplies.

---

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — the `TpuVersion` enum ↔ codename source-of-truth table the device-type column maps into
- [Marketing / Cloud Naming](marketing-cloud-naming.md) — codename ↔ Cloud-TPU display name (the v6e/TPU7x cross-walk)
- [Superseded-Label Correction List](codename-superseded-labels.md) — the v5 chip-DID and device-type pinning corrections
- [HAL Families](hal-families.md) — the JXC/PXC/VXC factory routing that consumes the recognized device
- [Chip Parts binarypb](chip-parts-binarypb.md) — the embedded per-chip proto that carries DID and core-count metadata
