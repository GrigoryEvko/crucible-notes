# kDeviceTypeInfo Spec-Constants

> *All addresses, offsets, and field values on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`xprof::kDeviceTypeInfo` (`0x1C60480`) is the profiler's master per-`DeviceType` spec table: a 17-entry array of a fixed `0x448`-byte (1096-byte) struct, indexed directly by the `xprof::DeviceType` ordinal. Each entry packs the device's clocks (kHz), per-chip core/tile geometry, two DVFS frequency ladders, and roughly forty IEEE-754 doubles of per-generation hardware spec constants — peak compute by precision, effective and per-memory-space bandwidth, latency, voltage, and firmware power/thermal coefficients.

The table is a **compile-time `static const` aggregate**, not a runtime-populated structure. Its symbol `_ZN5xprofL15kDeviceTypeInfoE` has internal linkage (the mangled `L`), it lives in `.lrodata` inside a read-only-executable `PT_LOAD` segment, and a `.rela.dyn` scan finds zero relocations across its whole extent — so it has no pointer fields and no runtime writer. The bytes are frozen at link time. Identical-code-folding leaves up to 13 byte-identical copies of the array, one per template/pass instantiation that references it; every consumer reads its own copy through a `GOT`-relative `movabs` base plus `idx * 0x448`.

This page documents:

- The **per-`DeviceType` struct**: the field-offset map, classified into clocks, geometry, peak-compute doubles, DVFS ladders, and the host-side-only spec block.
- The **producer**: why the table is a frozen compile-time const independent of the `chip_parts` capability blobs, joined to them only at the `DeviceType` ordinal.
- The **roofline readers**: which fields each profiler consumer reads, and how the device-capability stats are stamped onto the XPlane.
- The **`DeviceType` → codename binding**: the ordinal-to-silicon-generation map that gives each struct entry a name.

| | |
|---|---|
| **Symbol** | `_ZN5xprofL15kDeviceTypeInfoE` @ `0x1C60480` (`.lrodata`, internal linkage) |
| **Shape** | 17 entries × `0x448` B = 18632 B; ends `0x1C64D48`; up to 13 ICF copies |
| **Producer** | none — compile-time `static const`; zero relocs, no runtime writer |
| **Clock readers** | `GtcSpanConverter` (`0xF2CB6E0`, `+0x04`); `GetJobInfoFromResponse` (`0xF2C9AC0`, `+0x04`/`+0x50`/`+0x2F8` ×1000) |
| **Compute reader** | `XProfTpuCostAnalysis::HandleConvolution` (`0xF58E7C0`, `+0x60`/`+0x78`/`+0x80`) |
| **Geometry readers** | `HostCoreId::ToDeviceOrdinal` (`0xF69C580`); `GetCostAdjustmentFunction` (`0xF58EFC0`, `+0x2C4`) |
| **Codename binding** | `DeviceTypeFromDeviceIdentifiers` (`0xF6993A0`); `DeviceTypeString` (`0xF69C7C0`) |

---

## The Per-DeviceType Struct

The struct is indexed by the `DeviceType` ordinal: a consumer computes `base + ordinal * 0x448`, after a bounds check `ordinal < 0x11` (`ud1` trap otherwise). The `GtcSpanConverter` constructor is the canonical reader and pins the stride byte-exact:

```c
GtcSpanConverter::GtcSpanConverter(DeviceType dt):     // sub_F2CB6E0
    if (dt >= 0x11) BUG()                               //  17 entries
    base = GOT(0x224C2980) + (-8525971 .. )             //  -> kDeviceTypeInfo @ 0x1C60480
    converter[0] = base[dt * 274 + 1]                   //  274 int32 cols = 0x448; col 1 = +0x04 (kHz)
    converter[1..3] = 0
```

`274 * 4 = 0x448`, confirming 274 int32 columns per entry. The field-offset map below classifies each populated field. Status legend: **C** = a `.text` reader was disassembled; **P** = byte-exact value, no in-binary reader pinned; **I** = semantics inferred from per-generation value scaling against an independent source.

| off | type | field | meaning / consumer | Confidence |
|---:|---|---|---|---|
| `+0x00` | i32 | `core_multi_flag` | BYTE-read by `GetJobInfoFromResponse` (`cmp BYTE,1`); 1 on multi-core / SC gens | C |
| `+0x04` | i32 | `gtc_freq_khz` | GTC clock; `GtcSpanConverter` divisor + `GetJobInfoFromResponse` ×1000 → Hz | C |
| `+0x08` | i32 | `gtc_ts_width_bits` | GTC timestamp width `{48,45,64}`; matches the trace-codec `GetBits64` widths | P |
| `+0x0C` | i32 | `cores_per_chip` | divisor in `ToDeviceOrdinal`/`HandleConvolution`/`GetCostAdjustment`/utilization | C |
| `+0x10`/`+0x14` | i32 | `logical_devices_a/b` | `ToDeviceOrdinal` `idiv [+0x10 or +0x14] / [+0x0C]`; `+0x14` = SparseCore count on SC gens | C |
| `+0x18`/`+0x1C` | i32 | `geom_c` / `tile_count` | per-chip multiplier / escalating tile-engine count | P |
| `+0x20` | i32 | `sc_present_flag` | 1 on the 45-bit SC gens (DT10..13) | P |
| `+0x28` | i32[8] | `dvfs_ladder_A_khz` | 8-point frequency ladder, DT12-only populated | P |
| `+0x50` | i32 | `tensorcore_clk_khz` | TensorCore/compute clock; `GetJobInfoFromResponse` ×1000 → Hz | C |
| `+0x58` | f64 | `peak_bf16_per_LD` | per-LD/sustained bf16 rate; no in-binary reader | I |
| `+0x60` | f64 | `peak_flops_bf16` | per-precision peak (TFLOP/s); `HandleConvolution` + XPlane stat `0x62` | C |
| `+0x68` | f64 | `peak_flops_int8_v7x` | v7x-only alternate int8 slot (1992.0) | I |
| `+0x78` | f64 | `peak_flops_int8/fp8` | ≈2× `+0x60`; `HandleConvolution` (integer/fp8 element types) | C |
| `+0x80` | f64 | `peak_flops_int4/fp4` | ≈4× `+0x60`; `HandleConvolution` (int4/fp4 element types) | C |
| `+0xB8`..`+0xC8` | f64 | `eff_hbm_bw_0..2` | effective HBM bandwidth (GB/s class); host-side only | I |
| `+0xD0` | f64 | `peak_hbm_bw` | HBM bandwidth; XPlane stat `0x63` (×1.073741824 GB→GiB) — byte-exact vs `chip_parts` HBM TB/s | C |
| `+0xD8`..`+0xF0` | f64 | `mem_latency_0..3` | latency/cycle class; host-side only | I |
| `+0xF8`..`+0x130` | f64 | per-mem-space bandwidth | `+0x100`/`+0x108` (SRAM/VMEM bw, stats `0x66`/`0x67`); `+0x120`/`+0x128` (CMEM bw, stats `0x64`/`0x65`, v4-only); `+0xF8`/`+0x110`/`+0x118`/`+0x130` host-side only | C / I |
| `+0x138`..`+0x150` | f64 | `rate_b_0..3` | secondary throughput rate (v4+); host-side only | I |
| `+0x178`..`+0x190` | f64 | `rate_c_0..3` | secondary peak-rate table (per-precision); host-side only | I |
| `+0x2B8` | i32 | `packed_geom` | packed `{a,b,a,b}` byte descriptor; BYTE-read by `ProcessCounter` | P |
| `+0x2C4` | i32 | `megacore_flag` | `GetCostAdjustment` `cmp BYTE[+0x2C4],1`; XPlane stat `0x6B` (`has_megacore`) | C |
| `+0x2C8` | i32 | perf-counter-set mask | `ConvertTpuTraceToXPlane` → `GetPerformanceCounterNames<28>` (v7x) | C |
| `+0x2D0` | i32[8] | `dvfs_ladder_B_khz` | second 8-point ladder, DT12-only populated | P |
| `+0x2F8` | i32 | `sparsecore_clk_khz` | SparseCore clock; `GetJobInfoFromResponse` ×1000 → Hz | C |
| `+0x300`/`+0x308` | f64 | `core_voltage/power_0/1` | voltage/power class; host-side only | I |
| `+0x340` | i32 | `sc_lane_count` | 16 on SC gens; read at 18 SparseCore subscriber sites | C |
| `+0x348`/`+0x350`/`+0x358` | i32 | perf-counter-set masks | `GetPerformanceCounterNames<28>` (v7x) | C |
| `+0x360`..`+0x378` | f64 | power/thermal coeffs | `ConvertFirmwareTraceEntriesToXPlane` → `FirmwareEventBuilder` → "power"/"temperature" stats | C |
| `+0x388`/`+0x390`/`+0x398` | i32 | firmware-event counts | `FirmwareEventBuilder` ulong args | C |
| `+0x438`/`+0x440` | f64 | `tail_v7x` | DT12-only small packed sub-fields | I |

> **NOTE —** the table carries **no pointer fields** (zero relocations across `[0x1C60480, 0x1C64D48)`). The device codename string and the trace-codec factory are keyed *separately* by the same captured device identity: `DeviceTypeString`'s pointer array at `0x21772F00` (indexed by ordinal) for the name, and the per-family `DeviceIdentifiers` `std::map` factory for the codec. The ordinal selects the clock/spec (this struct); the PCI tuple selects the codec.

### DVFS Ladders

The two 8-entry `int32[8]` ladders at `+0x28` (A) and `+0x2D0` (B) are kHz operating-point tables, populated *only* on DT12 (TPU v7x); older generations leave them all-zero and use the fixed scalar clocks at `+0x50` and `+0x2F8`. No in-binary iterator over either ladder was found — the host DVFS/power model consumes them — so the core-vs-SC labeling is positional:

- **Ladder A** (`+0x28`): `{1.60, 1.70, 1.80, 1.90, 2.00, 2.05, 2.10, 2.20}` GHz. Tops out above the v7x TensorCore clock (`+0x50` = 1900 MHz) — the core/TensorCore-domain ladder with turbo/boost states. (I)
- **Ladder B** (`+0x2D0`): `{1.40, 1.50, 1.60, 1.75, 1.75, 1.80, 1.85, 1.90}` GHz. Its repeated 1.75 GHz point equals the v7x SparseCore clock (`+0x2F8` = 1750 MHz) — the SC/memory-domain ladder. (I)

---

## The Producer: A Frozen Compile-Time Const

There is no runtime producer function. The symbol is a `LOCAL OBJECT` of size 18632 in section `.lrodata` (flags `AMSl`, no write bit), mapped by a `PT_LOAD` segment with `p_flags = R E`. Any runtime store to `0x1C60480` would fault in a read-only-executable segment, and a `.rela.dyn` scan returns zero relocations in the table's range — so it has no pointer fields and cannot be patched at load. The bytes are a hand-written / codegen'd `static const DeviceTypeInfo[17]` emitted by the C++ front end. The up-to-13 byte-identical copies (each referenced through a per-copy `movabs` base = `copy_VMA − GOT(0x224C2980)`) are identical-code-folding artifacts of the distinct template/pass instantiations.

The peak-compute doubles are an **independent xprof spec table**, not a byte-frozen copy of the `chip_parts` `FlopsPerSecond` constants. Cross-validation against the decoded `chip_parts` numbers shows the bf16 and all v5p slots diverge, while only the v6e int8/int4 slots coincide — because both derive from the same `systolic_dim² × clk` hardware pedigree, not because one was copied from the other:

| slot | kDeviceTypeInfo (TFLOP/s) | chip_parts /1e12 | ratio | match |
|---|---:|---:|---:|:--:|
| v5p bf16 `+0x60` | 236.7 | 197.0 | 1.20 | no |
| v5p int8 `+0x78` | 466.2 | 394.0 | 1.18 | no |
| v5p int4 `+0x80` | 925.2 | 788.0 | 1.17 | no |
| v6e bf16 `+0x60` | 946.7 | 918.0 | 1.03 | no |
| v6e int8 `+0x78` | 1835.0 | 1835.0 | 1.00 | yes |
| v6e int4 `+0x80` | 3670.0 | 3670.0 | 1.00 | yes |

So `chip_parts.binarypb` is the compiler/`Target` spec source and `kDeviceTypeInfo` is the parallel xprof profiler spec source; the two are joined only at the `DeviceType` ordinal and are not interchangeable.

---

## Roofline Readers

The profiler does not evaluate a roofline in-binary; it *stamps the roofline inputs* — a device-capability stat record — onto the XPlane, from which the out-of-binary host xprof/roofline tool computes the ceiling. The readers:

- **Clocks → device-info.** `GetJobInfoFromResponse` (`0xF2C9AC0`) checks `core_multi_flag` (`+0x00`), then lifts `gtc_freq_khz` (`+0x04`), `tensorcore_clk_khz` (`+0x50`), and `sparsecore_clk_khz` (`+0x2F8`) ×1000 into the `Task` proto's `gtc_freq_hz`/`tensor_core_freq_hz`/`sparse_core_freq_hz` fields. The decompiled site reads `[base + 1096*ord + col]` with `col[1]`=+0x04, `col[20]`=+0x50, `col[190]`=+0x2F8, each `1000LL *`.
- **Peak compute → cost model.** `XProfTpuCostAnalysis::HandleConvolution` (`0xF58E7C0`) starts at `DBL_MAX`, maps each conv operand's `Shape::element_type` through a 28-entry jump table to load `+0x60` (bf16), `+0x78` (int8/fp8), or `+0x80` (int4/fp4), takes the running `vminsd` (the slowest operand precision dominates), then divides by the core count. This is the byte-exact proof that `+0x60`/`+0x78`/`+0x80` are the per-precision peak-FLOPS table.
- **Geometry → ordinal / cost.** `HostCoreId::ToDeviceOrdinal` (`0xF69C580`) divides `logical_devices_a/b` (`+0x10`/`+0x14`) by `cores_per_chip` (`+0x0C`) to map `(host, core)` to a device ordinal. `GetCostAdjustmentFunction` (`0xF58EFC0`) reads `megacore_flag` (`+0x2C4`, `cmp BYTE,1`) to enable `AdjustCostForMegacoreFunction` and `cores_per_chip` (`+0x0C`).
- **Device-capability XStats → XPlane.** `ConvertTpuTraceToXPlaneV2` stamps `+0x60` (peak TFLOP/s, stat `0x62`), `+0xD0`/`+0x100`/`+0x108`/`+0x120`/`+0x128` (per-memory-space bandwidth, stats `0x63`–`0x67`, each ×1.073741824 to convert base-10 GB/s to GiB/s), and `+0x2C4` (`has_megacore`, stat `0x6B`). `ConvertTpuTraceToXPlane` feeds the `+0x2C8`/`+0x348`/`+0x350`/`+0x358` masks to `GetPerformanceCounterNames<28>`; `ConvertFirmwareTraceEntriesToXPlane` feeds the `+0x360`..`+0x378` doubles and `+0x388`/`+0x390`/`+0x398` ints into `FirmwareEventBuilder`, which stamps "power"/"temperature" stats.

> **QUIRK —** the `+0xD0` HBM-bandwidth member, after the ×1.073741824 conversion, is byte-exact to the `chip_parts` HBM bandwidth (v7x 3433 → 3686.2 GiB/s = 3.686 TB/s; v6e 1525.5 → 1638.0 GiB/s = 1.638 TB/s). The `+0xB8` group is *not* the authoritative HBM-bandwidth stamp — it is a separate effective-bandwidth spec with no in-binary reader. A reimplementation reading `+0xB8` as the HBM peak would be reading the wrong field.

---

## DeviceType → Codename Binding

The `kDeviceTypeInfo` index is the 1-based `xprof::DeviceType` enum. `DeviceTypeFromDeviceIdentifiers` (`0xF6993A0`) maps a captured 12-byte PCI tuple (all `vendor_id == 0x1AE0`, Google) to the ordinal, and `DeviceTypeString` (`0xF69C7C0`) maps the ordinal to the public name (`return DeviceTypeString[ord-1]`, default `"Cloud TPU"` for `ord-1 > 0xC`). The eight real silicon generations:

| ordinal | public name | codename | family | GTC clk (kHz) | ts width | Confidence |
|---:|---|---|---|---:|---:|---|
| 3 | TPU v2 | Jellyfish | jxc | 700000 | 48 | CONFIRMED |
| 5 | TPU v3 | Dragonfish | jxc | 700000 | 48 | CONFIRMED |
| 7 | TPU v4 | Pufferfish | pxc/pfc | 700000 | 48 | CONFIRMED |
| 8 | TPU v4 Lite | Puffylite | pxc/plc | 700000 | 48 | CONFIRMED |
| 10 | TPU v5 | Viperfish (v5p) | vxc/vfc | 800000 | 45 | CONFIRMED |
| 11 | TPU v5 Lite | Viperlite (v5e) | vxc/vlc | 800000 | 45 | CONFIRMED |
| 12 | TPU v7x | Ghostfish (gfc) | gxc | 833000 | 45 | CONFIRMED |
| 13 | TPU v6 Lite | Ghostlite (v6e) | gxc/glc | 800000 | 45 | CONFIRMED |

`DeviceTypeFromDeviceIdentifiers` matches each codename's `kXxxChipIdentifiers` tuple in turn (Jellyfish→3, Dragonfish→5, Puffylite→8, the three Pufferfish B0 SKUs→7, the four Viperlite SKUs→11, the two Viperfish SKUs→10) and dispatches the two Ghost families via `IsGlc`→13 and `IsGfc`→12. Ordinals 1/2/4/6/9/14..16 are the host-GPU plane and "Cloud TPU" placeholder/reserved slots (DeviceType 9 is a reserved 64-bit-timestamp, 1.333 GHz slot with no PCI tuple). `DeviceTypeToHardwareType` (`0xF69C7A0`) confirms the split: the eight named gens map to hardware-type 3 (TPU), the placeholders to 0/1, the GPU plane to 2.

> **NOTE —** the profiler's `DeviceType` ordinal is a *third* numbering distinct from both `TpuVersion` (0-based internal) and `TpuVersionProto` (1-based wire). It is keyed off the captured PCI tuple, not off `TpuVersion`. Use the [Codename Matrix](tpu-version-codename-matrix.md) to reconcile the three.

---

## Related Components

| Name | Relationship |
|---|---|
| `GtcSpanConverter` (`0xF2CB6E0`) | canonical `+0x04` GTC-clock reader; pins the `0x448` stride and the 17-entry bound |
| `GetJobInfoFromResponse` (`0xF2C9AC0`) | lifts the three clocks ×1000 into the `Task` device-info proto |
| `HandleConvolution` (`0xF58E7C0`) | reads `+0x60`/`+0x78`/`+0x80` peak FLOPS for the cost model |
| `DeviceTypeFromDeviceIdentifiers` (`0xF6993A0`) | PCI tuple → `DeviceType` ordinal → codename binding |
| `FirmwareEventBuilder` (`0xF284D20`) | consumes the `+0x360` power/thermal coefficient bundle |

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — reconciles the `DeviceType` ordinal with `TpuVersion` and the codename roster
- [Per-DeviceType Struct](../profiling/per-devicetype-struct.md) — the full `0x448` field dump and unit/wrap-period proof
- [kDeviceTypeInfo Producer / Readers](../profiling/kdevicetypeinfo-producer-readers.md) — the compile-time-const producer argument and the XPlane device-capability stamp path
