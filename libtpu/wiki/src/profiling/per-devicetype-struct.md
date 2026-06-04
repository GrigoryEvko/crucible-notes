# Per-DeviceType Profiler Struct

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary). The binary is **not** stripped — every symbol is a demangled C++ name. `.text`, `.rodata`, and `.lrodata` map VMA == file offset; `xprof::kDeviceTypeInfo` lives at VMA `0x1c60480` in `.lrodata`. Other builds will differ.*

## Abstract

The profiler holds one descriptor per silicon generation in a single baked array — `xprof::kDeviceTypeInfo`, 17 records of stride `0x448` (1096 bytes), indexed directly by the `xprof::DeviceType` enum ordinal. Each record is the per-`DeviceType` profiler struct: it carries the two clocks the trace pipeline divides by (the GTC clock at `+0x04` and the TensorCore/compute clock at `+0x50`), the GTC timestamp bit-width at `+0x08`, the per-chip core geometry at `+0x0c..+0x20`, two 8-point DVFS frequency ladders at `+0x28` and `+0x2d0`, and roughly forty IEEE-754 hardware-spec doubles. This page owns the **field layout** of that struct and the **codename binding** — how a captured TPU's PCI identity selects the right `DeviceType` ordinal, which then selects this struct instance, which then drives the trace codec's clock domain. The sibling page [kDeviceTypeInfo Producer and Roofline Readers](kdevicetypeinfo-producer-readers.md) owns the orthogonal question of *who reads each field*; this page owns *what each field is and which generation each row describes*.

The single most important structural fact is that the struct carries **no pointers**. A full `.rela.dyn` scan returns zero relocations inside `[0x1c60480, 0x1c64d48)` — every field is an inline `int32`, `int32[8]`, or `double` literal frozen at link time. That means the per-generation codename string and the per-generation trace-codec factory are **not** embedded in this struct. They are two parallel lookups keyed by the *same* captured device identity but stored elsewhere: the codename string lives in the `xprof::DeviceTypeString` pointer array at `0x21772f00`, and the codec factory lives in the per-family `std::map` reached through `GetTraceCodec` (`0xf5a2900`). The `DeviceType` ordinal selects the **clock** (this struct); the raw `DeviceIdentifiers` PCI tuple selects the **codec**; they join at trace-conversion time. A reimplementer who expects this struct to hold a codename or a codec function pointer will look for a field that does not exist.

The binding chain is a three-hop join, all anchored to disassembled functions. A captured trace's `DeviceIdentifiers` (a 12-byte PCI tuple) feeds `DeviceTypeFromDeviceIdentifiers` (`0xf6993a0`), which compares the tuple field-wise against the `kXxxChipIdentifiers` constants and yields a `DeviceType` ordinal in `{3,5,7,8,10,11,12,13}` — the eight real silicon generations. That ordinal feeds `GtcSpanConverter::GtcSpanConverter(DeviceType)` (`0xf2cb6e0`), which loads `kDeviceTypeInfo[ordinal] + 0x04` (the GTC kHz divisor), and `DeviceTypeString(DeviceType)` (`0xf69c7c0`), which yields the device-plane name (`"TPU v2"` … `"TPU v7x"`). The page walks that chain, decodes the field-offset map, and pins each ordinal to its codec-family codename (`jxc`/`pxc`/`vxc`/`gxc` and the per-gen `pfc`/`plc`/`vfc`/`vlc`/`glc`/`gfc` sub-families).

For reimplementation, the contract is:

- The `0x448`-byte field-offset map: the two clocks (`+0x04` GTC kHz, `+0x50` compute kHz), the `+0x08` GTC timestamp width, the `+0x0c..+0x20` geometry, the `+0x28`/`+0x2d0` DVFS ladders, and the per-gen spec-double regions — with the certain head fields separated from the inferred bulk.
- The `DeviceType` ordinal → struct-instance addressing: `base + ordinal*0x448`, the `ordinal >= 0x11` (17-row) bound, and the two byte-identical ICF copies at `0x1c60480` and `0x1c84d90`.
- The codename binding: `DeviceIdentifiers` PCI tuple → `DeviceType` ordinal (`DeviceTypeFromDeviceIdentifiers`) → `{clock, name}` and, in parallel, → codec (`GetTraceCodec`); the 8-generation master table.
- The "no pointer fields" invariant: zero relocations in the struct ⇒ codename and codec are keyed separately, not embedded.

| | |
|---|---|
| **Struct symbol** | `xprof::kDeviceTypeInfo` (`_ZN5xprofL15kDeviceTypeInfoE`) @ `0x1c60480` (`.lrodata`) |
| **Layout** | 17 records × `0x448` = `18632` bytes; ends `0x1c64d48` (next sym `xla::gpu::kDeviceHloOpProfiles` @ `0x1c64d50`) |
| **Second copy** | byte-identical ICF fold @ `0x1c84d90` (read by `NormalizeGtcTimestamps`) |
| **Index** | `xprof::DeviceType` ordinal (0..16); 8 real gens = `{3,5,7,8,10,11,12,13}` |
| **Clock consumer** | `GtcSpanConverter::GtcSpanConverter(DeviceType)` @ `0xf2cb6e0` (reads `+0x04` only) |
| **Codename bind** | `DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0`; `DeviceTypeString` @ `0xf69c7c0` |
| **Codec bind (parallel)** | `GetTraceCodec` @ `0xf5a2900` (keyed by `DeviceIdentifiers`, not by this struct) |
| **Pointer fields** | none — zero relocations in `[0x1c60480, 0x1c64d48)` |

---

## The Struct — Layout and Addressing

### Purpose

`kDeviceTypeInfo` is the profiler's per-generation descriptor table. One `0x448`-byte record per `DeviceType` ordinal holds everything the trace pipeline needs to know about a silicon generation that is *not* in the trace stream itself: the clock frequencies it must divide raw counter ticks by, the timestamp counter width it must mask against, the core geometry, and a block of per-generation hardware-spec doubles. Reaching a record is a single multiply-and-bound: the ordinal times the `0x448` stride, guarded by a 17-row check.

### Entry Point

```text
DeviceIdentifiers (PCI tuple, from the captured JfTrace)
  └─ DeviceTypeFromDeviceIdentifiers (0xf6993a0)   ── PCI tuple → DeviceType ordinal
       └─ GtcSpanConverter::GtcSpanConverter(ordinal) (0xf2cb6e0)
            └─ row = kDeviceTypeInfo + ordinal*0x448 ; clk = row[+0x04]
```

### Addressing

Every reader reaches a record the same way: materialize the table base relative to the GOT (so the code works against whichever ICF copy the linker assigned), bound-check the ordinal at 17, multiply by the `0x448` stride. The canonical load is the `GtcSpanConverter` constructor, which reads exactly one field.

```c
function GtcSpanConverter_ctor(GtcSpanConverter* this, uint ordinal):   // 0xf2cb6e0
    if ordinal >= 0x11:                  // 17-row bound (ud1 trap)
        trap()
    // 274 int32 elements == 0x448 bytes; the trailing offset lands on +0x04
    this[0] = *(int32*)(kDeviceTypeInfo + 274*ordinal + 1)   // == row[+0x04], GTC kHz
    this[1] = 0                                              // GtcSpan vector cleared
    this[2] = 0
    this[3] = 0
```

> **NOTE —** the IDA decompiler renders the row stride as `274 * ordinal` over an `int*` (so `274 × 4 = 0x448` bytes) and the constant fold `+ 274*ordinal − 8525971` resolves to `kDeviceTypeInfo + ordinal*0x448 + 4`. Do not read `274` as a field count; it is the `0x448` byte stride expressed in `int32` elements. The same `>= 0x11` guard appears at every load site.

### The Two ICF Copies

`nm` lists `_ZN5xprofL15kDeviceTypeInfoE` (and a run of `_0`, `_1`, … suffixed folds) at several VMAs. Two of them are the live tables this page is about: the primary at `0x1c60480` (read by `GtcSpanConverter`) and a byte-identical fold at `0x1c84d90` (read by the `NormalizeGtcTimestamps<...>` family). The clock column of the two copies was compared dword-for-dword across all 17 rows and is identical. The `L` in the mangling marks internal linkage — a translation-unit-local `static const` array — and because it is referenced from many template/pass instantiations, the linker's identical-code-folding pass duplicated the *data* rather than collapsing it to one symbol. The copies are a folding artifact, not independent tables.

> **GOTCHA —** the `NormalizeGtcTimestamps<...>` instantiations read the **second** copy (`0x1c84d90`), not the primary. `NormalizeGtcTimestamps<pxc::profiler::TraceEntry>` (`0xf59b080`) loads `*(int*)(GLOBAL_OFFSET_TABLE_ + 274*ordinal − 136378107)`, which resolves to `0x1c84d90 + ordinal*0x448 + 4` — the same `+0x04` GTC-clock field, off the folded copy. A reimplementation has one table; the two-copy split is a link-time detail, and both copies are read for the identical purpose (the `+0x04` clock).

---

## Field-Offset Map

### Purpose

This is the byte-level decode of the `0x448` record. Only two fields have a directly traced in-binary consumer that reads *this struct by name* in a clock context — `+0x04` (the GTC clock, read by `GtcSpanConverter` and `NormalizeGtcTimestamps`) and, by value cross-check, `+0x08` (the GTC width, byte-matching the codec's `GetBits64` widths). Everything below the two clocks is byte-exact and per-generation-monotonic but its member *names* are inferred from value scaling, because the in-libtpu readers that consume those fields (catalogued on the [sibling page](kdevicetypeinfo-producer-readers.md)) do so via raw displacements, not named accessors. Confidence is labelled accordingly.

### The Head Scalars and Clocks

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `core_multi_flag` | `+0x00` | `int32` (BYTE-used) | `1` on the 2-core / mega / SparseCore gens (`DT 3,5,7,8,10,11,12,13`), `0` on single-core placeholder slots | High |
| `gtc_freq_khz` | `+0x04` | `int32` (kHz) | GTC (Global Time Counter) frequency. The picosecond timebase divides by `(khz << 4)`. **The only field `GtcSpanConverter` reads.** | CERTAIN |
| `gtc_ts_width_bits` | `+0x08` | `int32` (bits) | GTC timestamp counter width `{48, 45, 64}`. Byte-matches the per-family codec `GetBits64` widths; wrap period = `2^width / (khz·1000)` s | High |
| `cores_per_chip` | `+0x0c` | `int32` | `2` on the megacore gens (`DT 3,5,7,10,12`), `1` elsewhere — TensorCore count | High |
| `geom_a` | `+0x10` | `int32` | `{1,2,4}` on pre-SparseCore gens, `0` on SC gens — a TensorCore-side lane/MXU group count | Inferred |
| `geom_b` | `+0x14` | `int32` | nonzero only on SC gens (`DT10=4, DT12=4, DT13=2`) — a SparseCore-side count | Inferred |
| `geom_c` | `+0x18` | `int32` | mostly `1`; `2` on `DT3/DT5` — a per-chip multiplier | Inferred |
| `geom_d` | `+0x1c` | `int32` | escalating `{1,2,4,6,8}` (`DT7=4, DT9=2, DT10=6, DT12=8`) — a per-gen tile/engine count | Inferred |
| `sc_present_flag` | `+0x20` | `int32` | `1` only on the 45-bit SC gens (`DT10..13`), `0` else | Inferred |
| `dvfs_ladder_1` | `+0x28` | `int32[8]` (kHz) | 8-point DVFS operating-point ladder; populated on `DT12` (`{1600000..2200000}`) | Inferred |
| `compute_clk_khz` | `+0x50` | `int32` (kHz) | TensorCore/compute clock, distinct from the GTC clock at `+0x04`; escalates per gen | High |

> **QUIRK —** the GTC clock (`+0x04`) and the compute clock (`+0x50`) are different frequencies on most generations and must not be conflated. `DT12` (v7x) runs a `833000` kHz GTC counter but a `1900000` kHz compute clock; `DT5` (v3) a `700000` kHz GTC clock against a `940000` kHz compute clock. The trace timestamps are in the GTC domain (`+0x04`); the compute clock (`+0x50`) is the cycle clock the cost model uses. A reimplementation that timestamps off the compute clock will skew every event.

### The Spec-Double and DVFS Regions

Below `+0x50` the record is dominated by IEEE-754 doubles and a second DVFS ladder. These are per-generation hardware-spec constants — peak compute, memory bandwidth, latency, and DVFS/voltage class — frozen per row. Their exact member identity is not recoverable from this binary (no `ToString`, no per-field named accessor in a profiler-clock context), so the regions are described by their *shape* and per-gen scaling, not transcribed row-by-row.

| Region | Offset range | Type | Character (per-gen scaling) | Confidence |
|---|---|---|---|---|
| Compute-class doubles | `+0x58 .. +0x80` | `double` | monotonic peak-compute metrics; `+0x60` tracks a TFLOP/s-like figure (`24.3` DT3 → `1029` DT12), `+0x78`/`+0x80` ≈ 2×/4× of `+0x60` (precision tiers) | Inferred |
| Bandwidth-class doubles | `+0xb8 .. +0xd0` | `double` | GB/s-like bandwidths; `+0xb8` `280`(DT3) → `3433`(DT12); the authoritative HBM figure is `+0xd0` | Inferred |
| Latency-class doubles | `+0xd8 .. +0xf0` | `double` | latency/cycle-class; `+0xe8`==`+0xe0` and `+0xf0`==`+0xd8` (per-gen duplicates) | Inferred |
| Count-class doubles | `+0xf8 .. +0x130` | `double` | large counts (peak-ops / systolic-cell scale); per-precision variants | Inferred |
| Secondary-rate groups | `+0x138 .. +0x194` | `double` | DT7+ only (`56.6`, `453` families at `+0x13c`/`+0x17c`) | Inferred |
| `packed_geom` | `+0x2b8` | `int32` | packed `{a,b,a,b}` 4-byte geometry descriptor (e.g. `DT3 = 01 08 01 08`) | Inferred |
| `has_megacore`-class | `+0x2c4` | `int32` (BYTE) | `1` on `DT7/DT10` — a megacore-style flag | Inferred |
| Perf-counter-set bases | `+0x2c8 / +0x348 / +0x350 / +0x358 / +0x438 / +0x440` | `int32` | packed enum bases for the v7x perf-counter sets (nonzero on `DT12` only) — owned by [v7x Perf-Counters](v7x-perf-counters.md) | High |
| `dvfs_ladder_2` | `+0x2d0` | `int32[8]` (kHz) | second 8-point DVFS ladder; `DT12` = `{1400000..1900000}` | Inferred |
| `dvfs_nominal_khz` | `+0x2f8` | `int32` (kHz) | nominal DVFS / SparseCore operating point (`1750000` DT12, `0` on pre-SC gens) | Inferred |
| Voltage/power doubles | `+0x300 / +0x308` | `double` | voltage/power-class (`3.6`/`5.85` DT12) | Inferred |
| `sc_lane_count` | `+0x340` | `int32` | `16` on SC gens (`DT10/12/13`), `0` else | Inferred |
| Firmware calib bundle | `+0x360 .. +0x398` | `double`×4 + `ulong`×4 | per-gen power/thermal coefficients fed to `FirmwareEventBuilder` | High |

> **CORRECTION (PDT-1) —** the `+0x438`/`+0x440` tail was provisionally read as a small packed sub-field or hash. Cross-checking against the resolver call sites shows these two `int32` slots are the **perf-counter-set enum bases** for the v7x ICR (`+0x438`) and CMNUR/HBM (`+0x440`) counter sets — the same six descriptor fields the [v7x Perf-Counters](v7x-perf-counters.md) page recovers from the `DT12` row at `0x1c637e0`. They are nonzero only on `DT12`, contain no pointer (high dword is zero), and are *not* roofline doubles.

### The Picosecond Timebase

The `+0x04` clock is consumed by `GtcSpanConverter::TimespanFromGtcSpan` (`0xf2cb7e0`) as the divisor of the GTC→host-ns conversion. The arithmetic is exact and pins the field's role and units.

```c
function TimespanFromGtcSpan(this, gtc_ticks):                 // 0xf2cb7e0
    khz   = this[0]                          // == kDeviceTypeInfo[ord]+0x04
    scale = 16 * khz                         // the GtcSpan ×16 fixed-point (low 4 bits fractional)
    // value_ns = round( gtc_x16 * 1e9 / (khz << 4) )
    numer = (scale >> 1) + 1000000000 * (gtc_ticks - anchor)   // 0x3B9ACA00 == 1e9
    ns    = udiv128(numer, scale)
    return ns + 1000 * base_ns               // bracketed by the GtcSpan rb-tree node
```

> **NOTE —** the unit proof is byte-arithmetic. With the GtcSpan carrying a ×16 fixed-point value, `value_ps = gtc_x16 · 1e9 / (khz << 4)`. For one integer tick (`gtc_x16 = 16`): `700000 → 1429 ps` (`1/(700·1e6)·1e12 = 1428.57`), `800000 → 1250`, `833000 → 1200`, `1333000 → 750` — each matches `1/(khz·1000)·1e12` to under 1 ps. GTC wrap periods follow from `2^width / (khz·1000)`: the 48-bit/700 MHz counter wraps every ~111.7 h; the 45-bit/800 MHz counter every ~12.2 h; the `DT9` 64-bit/1.333 GHz slot is effectively non-wrapping. The `+0x04` clock is therefore the *authoritative* GTC divisor, baked per generation — not a runtime `Task.gtc_freq_hz` field.

---

## The DeviceType → Codename Binding

### Purpose

This struct is indexed by `DeviceType` ordinal, but the ordinal itself is derived from the captured hardware's PCI identity. The binding is a function family that maps a `DeviceIdentifiers` tuple to an ordinal and back, and an ordinal to a public name. Together they pin each row of `kDeviceTypeInfo` to a concrete silicon generation and its codec-family codename.

### Entry Point

```text
DeviceTypeFromDeviceIdentifiers (0xf6993a0)   ── PCI tuple → DeviceType ordinal (StatusOr)
  ├─ field-compare vs jxc::kJellyfishIdentifiers, kDragonfishIdentifiers
  ├─ field-compare vs pxc::plc::kPuffyliteChipIdentifiers
  ├─ field-compare vs pxc::pfc::kPufferfishChipB0{Mfg,Water,Air}Identifiers
  ├─ field-compare vs vxc::vlc::kViperliteChip{A0,A1}{VF,PF}Identifiers
  ├─ field-compare vs vxc::vfc::kViperfishChip{VF,PF}Identifiers
  ├─ IsGlc (0xf6992a0)  → ordinal 13   ── gxc::glc (Ghostlite)
  ├─ IsGfc (0xf699320)  → ordinal 12   ── gxc::gfc (Ghostfish)
  └─ default → MakeErrorImpl<3>("Unsupported device identifiers")
DeviceTypeString (0xf69c7c0)                  ── ordinal → public name array @0x21772f00
DeviceIdentifiersFromDeviceType (0xf6996e0)   ── ordinal → representative PCI tuple (inverse)
```

### Algorithm

`DeviceTypeFromDeviceIdentifiers` compares the captured 12-byte tuple field-wise against the `kXxxChipIdentifiers` constants and stores the matching ordinal. The Ghostlite/Ghostfish families dispatch through `IsGlc`/`IsGfc` predicates rather than inline constants (each covers several App/Mgt SKUs). No match yields error code 3.

```c
function DeviceTypeFromDeviceIdentifiers(out, const DeviceIdentifiers* id):  // 0xf6993a0
    if id == jxc::kJellyfishIdentifiers:          out.ordinal = 3;  return ok   // TPU v2
    if id == jxc::kDragonfishIdentifiers:         out.ordinal = 5;  return ok   // TPU v3
    if id == pxc::plc::kPuffyliteChipIdentifiers: out.ordinal = 8;  return ok   // TPU v4 Lite
    if id in pxc::pfc::kPufferfishChipB0{Mfg,Water,Air}:
                                                  out.ordinal = 7;  return ok   // TPU v4
    if id in vxc::vlc::kViperliteChip{A0,A1}{VF,PF}:
                                                  out.ordinal = 11; return ok   // TPU v5 Lite
    if id in vxc::vfc::kViperfishChip{VF,PF}:      out.ordinal = 10; return ok   // TPU v5
    if IsGlc(id):                                 out.ordinal = 13; return ok   // TPU v6 Lite
    if IsGfc(id):                                 out.ordinal = 12; return ok   // TPU v7x
    return MakeErrorImpl<3>("Unsupported device identifiers")  // device_identifiers_utils.cc:152
```

`DeviceTypeString` is the ordinal→name map: `ord-1` indexes the pointer array at `0x21772f00` (valid `0..0xC`, i.e. ordinals `1..13`); any other ordinal returns the constant `"Cloud TPU"`. `DeviceTypeToHardwareType` (`0xf69c7a0`) is the parallel ordinal→hardware-class map (int32 array at `0xAB8A2F4`): GPU=2, TPU=3, and 0/1 for the placeholders.

```c
function DeviceTypeString(int ordinal):           // 0xf69c7c0
    if (uint)(ordinal - 1) > 0xC:  return "Cloud TPU"
    return off_21772F00[ordinal - 1]              // "GPU","TPU v2",...,"TPU v6 Lite"
```

### The Master Table

The eight real silicon generations are exactly the ordinals `DeviceTypeFromDeviceIdentifiers` can return (all hardware-type 3 = TPU). The remaining ordinals are GPU/placeholder/reserved slots that carry a default clock but no codename and no PCI tuple. Public names, codec families, clocks, and timestamp widths below are byte-confirmed against `DeviceTypeString`, the codec namespaces in `DeviceTypeFromDeviceIdentifiers`, and a direct read of the `+0x04`/`+0x08` columns of the struct.

| DT | Public name | Codename | Codec family | GTC kHz (`+0x04`) | ts-width (`+0x08`) | compute kHz (`+0x50`) | hwtype | Confidence |
|---|---|---|---|---|---|---|---|---|
| 1 | GPU | (host GPU plane) | — | 700000 | 48 | 700000 | 2 (GPU) | High |
| 2 | Cloud TPU | (generic placeholder) | — | 700000 | 48 | 700000 | 0 | High |
| 3 | TPU v2 | Jellyfish | `jxc` | 700000 | 48 | 700000 | 3 (TPU) | CERTAIN |
| 4 | Cloud TPU | (placeholder; bind → err) | — | 700000 | 48 | 700000 | 1 | High |
| 5 | TPU v3 | Dragonfish | `jxc` | 700000 | 48 | 940000 | 3 | CERTAIN |
| 6 | Cloud TPU | (generic placeholder) | — | 700000 | 48 | 700000 | 0 | High |
| 7 | TPU v4 | Pufferfish | `pxc::pfc` | 700000 | 48 | 1050000 | 3 | CERTAIN |
| 8 | TPU v4 Lite | Puffylite | `pxc::plc` | 700000 | 48 | 1050000 | 3 | CERTAIN |
| 9 | Cloud TPU | (reserved 64-bit slot) | — | 1333000 | 64 | 1333000 | 0 | High |
| 10 | TPU v5 | Viperfish | `vxc::vfc` | 800000 | 45 | 1750000 | 3 | CERTAIN |
| 11 | TPU v5 Lite | Viperlite | `vxc::vlc` | 800000 | 45 | 1500000 | 3 | CERTAIN |
| 12 | TPU v7x | Ghostfish | `gxc::gfc` | 833000 | 45 | 1900000 | 3 | CERTAIN |
| 13 | TPU v6 Lite | Ghostlite | `gxc::glc` | 800000 | 45 | 1750000 | 3 | CERTAIN |
| 14–16 | Cloud TPU | (legacy placeholders) | — | 700000 | 48 | 700000 | — | High |

> **QUIRK —** `DeviceType` 12 ("TPU v7x", Ghostfish/`gfc`) and `DeviceType` 13 ("TPU v6 Lite", Ghostlite/`glc`) are sibling members of the same `gxc` chip family but distinct `DeviceType`s with distinct GTC clocks (833 vs 800 MHz) and distinct compute clocks (1.9 vs 1.75 GHz). Both dispatch through predicate functions (`IsGfc`/`IsGlc`) rather than a single constant, because each spans several App/Mgt PCI SKUs. A reimplementation that treats the whole `gxc` family as one `DeviceType` will pick the wrong clock divisor for half of them.

> **GOTCHA —** `DeviceType` 9 is a reserved slot: `1333000` kHz GTC clock, a 64-bit (effectively non-wrapping) timestamp, `"Cloud TPU"` as its name, and **no** PCI tuple — `DeviceIdentifiersFromDeviceType(9)` returns an error. It is not one of the eight named generations; its clock/width are the only non-placeholder fields. Do not bind it to a codec; nothing in this build does.

### The Inverse and the PCI Tuples

`DeviceIdentifiersFromDeviceType` (`0xf6996e0`) is the inverse: a `switch` on the ordinal (cases `3,5,7,8,10,11,12,13`) that returns a representative `kXxxChipIdentifiers` tuple; ordinals `4/6/9` (and anything outside the switch) return `MakeErrorImpl<3>("Unsupported device type")`. The tuples are 12-byte PCI descriptors — `+0 vendor`, `+2 device`, `+4 subsys_vendor`, `+6 subsys_device`, `+8 class`, `+9 subclass`, `+0xa prog_if`, `+0xb revision` — all with `vendor_id == 0x1AE0` (Google) and `subsys_vendor == 0x1AE0`. They live contiguously at `0xbdf3c0c..0xbdf3cd0`. The field-compare in `DeviceTypeFromDeviceIdentifiers` masks the comparison so the `WORD2 == 6880` (`0x1AE0`) subsystem-vendor check and a revision-byte check gate each match.

```c
function DeviceIdentifiersFromDeviceType(out, int ordinal):   // 0xf6996e0
    switch ordinal:
        case 3:  tuple = jxc::kJellyfishIdentifiers              // TPU v2
        case 5:  tuple = jxc::kDragonfishIdentifiers             // TPU v3
        case 7:  tuple = pxc::pfc::kPufferfishChipB0WaterIdentifiers
        case 8:  tuple = pxc::plc::kPuffyliteChipIdentifiers
        case 10: tuple = vxc::vfc::kViperfishChipVFIdentifiers
        case 11: tuple = vxc::vlc::kViperliteChipA0VFIdentifiers
        case 12: tuple = <gfc tuple @0xbdf3cd0>                  // device 0x76 / subsys 0xf2
        case 13: tuple = gxc::glc::kGhostliteChipAppVFIdentifiers
        default: return MakeErrorImpl<3>("Unsupported device type")  // ...utils.cc:191
    out = { ok, tuple.lo64, tuple.dword2 }
```

---

## Why the Struct Carries No Codename or Codec Pointer

A `.rela.dyn` scan for any relocation with `r_offset` inside `[0x1c60480, 0x1c64d48)` returns **zero**. The `0x448` record is pure scalar/float data; nothing in it is a relocated pointer. Two consequences follow, both already used above:

- The **codename string** is not in the struct. It is in the `DeviceTypeString` pointer array at `0x21772f00` (in `.data.rel.ro`, where the pointers *are* relocated), indexed by the same `DeviceType` ordinal. The ordinal joins the clock (this struct) to the name (that array).
- The **trace-codec factory** is not in the struct. It is registered in a per-family `std::map` keyed by the 12-byte `DeviceIdentifiers` value, reached through `GetTraceCodec` (`0xf5a2900`). The `DeviceType` ordinal selects the *clock*; the raw PCI tuple selects the *codec*. They are two parallel lookups keyed by the same captured device identity, joined at trace-conversion time.

So the full master bridge — from a captured trace to its decoded, time-converted, named events — is:

```text
captured JfTrace.device_identifiers (12-byte PCI tuple)
  ├─ GetTraceCodec(tuple)                    → per-gen TraceCodecInterface (the packet decoder)
  └─ DeviceTypeFromDeviceIdentifiers(tuple)  → DeviceType ordinal
       ├─ GtcSpanConverter(ordinal)          → kDeviceTypeInfo[ordinal]+0x04 (GTC ps timebase)
       └─ DeviceTypeString(ordinal)          → device-plane name ("TPU vN")
```

> **NOTE —** the parallel-lookup design is the reason the struct is pointer-free. If the codec factory or the codename were embedded, the struct would carry relocations and could not be a pure `static const` aggregate in `.lrodata`. Keeping clock (ordinal-keyed) and codec (tuple-keyed) separate lets the table stay relocation-free and lets the same `DeviceType` ordinal serve as the join key into three independent arrays. A reimplementation can store the codename in the struct, but the binary deliberately does not — and the "no relocations" invariant is the proof.

---

## Open Questions

These are the fields and behaviors that are byte-exact but whose semantics or consumers are not pinned from this binary alone.

- **Bulk-field member names.** The ~40 spec doubles (`+0x58..+0x194`, `+0x300/+0x308`, `+0x360..`) and the integer geometry (`+0x0c..+0x20`, `+0x2b8`, `+0x340`, `+0x388..`) are per-gen-monotonic (compute / bandwidth / DVFS / count classes) but have no named accessor in a profiler-clock path; their member identity is inferred from value scaling. The [sibling reader page](kdevicetypeinfo-producer-readers.md) traces which displacements the stat-stamp and cost-model paths consume.
- **Whether the codec reads `+0x08`.** The GTC width `{48/45/64}` byte-matches the per-family `GetBits64` codec constants exactly, but the codec's width appears baked into the family-templated `DecodeEntry`, not loaded from `kDeviceTypeInfo[ord]+0x08` at runtime — so `+0x08` may be an authoritative-but-informational copy rather than the live source.
- **`DeviceType` 9 and 14–16.** The reserved 64-bit/1.333 GHz slot (`DT9`) and the three 700 MHz/48-bit placeholders (`DT14–16`) carry clocks and pre-SC geometry but no codename and no PCI tuple; which silicon (if any) they anticipate is not recoverable here.

---

## Related Components

| Component | Address | Relationship |
|---|---|---|
| `GtcSpanConverter::GtcSpanConverter(DeviceType)` | `0xf2cb6e0` | the canonical reader; loads `kDeviceTypeInfo[ord]+0x04` as the GTC ps divisor |
| `GtcSpanConverter::TimespanFromGtcSpan` | `0xf2cb7e0` | applies the `×16 / 1e9` ps conversion using the `+0x04` clock |
| `NormalizeGtcTimestamps<...>` | `0xf59b080` (pxc) | reads the second ICF copy `0x1c84d90 + ord*0x448 + 4` for the same clock |
| `DeviceTypeFromDeviceIdentifiers` | `0xf6993a0` | PCI tuple → `DeviceType` ordinal (the binding into this struct's index) |
| `DeviceIdentifiersFromDeviceType` | `0xf6996e0` | inverse: ordinal → representative PCI tuple |
| `DeviceTypeString` | `0xf69c7c0` | ordinal → public name (`0x21772f00` ptr array) — the codename's separate home |
| `DeviceTypeToHardwareType` | `0xf69c7a0` | ordinal → hardware class (`0xAB8A2F4` int32 array) |
| `GetTraceCodec` | `0xf5a2900` | parallel tuple-keyed codec lookup — selects the decoder, not the clock |
| `IsGlc` / `IsGfc` | `0xf6992a0` / `0xf699320` | predicate dispatch for the `gxc::glc` (DT13) and `gxc::gfc` (DT12) families |

## Cross-References

- [kDeviceTypeInfo Producer and Roofline Readers](kdevicetypeinfo-producer-readers.md) — sibling page; who writes the table and who reads each roofline-relevant field
- [v7x Perf-Counters](v7x-perf-counters.md) — owns the `+0x2c8/+0x348/+0x350/+0x358/+0x438/+0x440` perf-counter descriptor fields and the `DeviceType == 12` gate
- [Profiling and Telemetry](overview.md) — the XPlane/XStat pipeline the per-gen struct feeds
- [Task Proto](task-proto.md) — the runtime device-info clock fields cross-checked against `+0x04`/`+0x50`
- [Riegeli Trace Container](riegeli-trace-container.md) — the `JfTrace` envelope that carries the `DeviceIdentifiers` tuple the binding starts from
- [TPU Profiler ABI](tpu-profiler-abi.md) — the profiler entry points that construct the `GtcSpanConverter` per captured device
- [Payload — VFC/VLC/GFC](payload-vfc-vlc-gfc.md) — the per-family packet codecs whose GTC widths match the `+0x08` column
- [Payload — JXC Legacy](payload-jxc-legacy.md) — the `jxc` (Jellyfish/Dragonfish) codec for the 48-bit, 700 MHz generations (DT3/DT5)
