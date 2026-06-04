# kDeviceTypeInfo Producer and Roofline Readers

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary). The binary is **not** stripped — every symbol is a demangled C++ name. `.text`, `.rodata`, and `.lrodata` map VMA == file offset; `kDeviceTypeInfo` lives at VMA `0x1c60480` in `.lrodata`. Other builds will differ.*

## Abstract

`xprof::kDeviceTypeInfo` is the profiler's per-generation hardware-spec table: a `17 × 0x448`-byte array of `DeviceTypeInfo` records, one per `DeviceType` ordinal, holding peak FLOP/s per precision, per-memory-space bandwidths, clocks, core geometry, DVFS ladders, perf-counter-set descriptors, and a firmware power/thermal bundle. The sibling page [Per-DeviceType Profiler Struct](per-devicetype-struct.md) owns the byte-level field layout; [v7x Perf-Counters](v7x-perf-counters.md) owns the counter-name descriptor fields. **This page owns two facts the others do not: who writes the table, and who reads the roofline-relevant numbers.**

The producer story is short and definitive: there is **no runtime producer**. The table is an `lea`-addressed scalar aggregate that lives in `.lrodata` — a segment mapped read-only-no-write (`perm 4`) — so no `.text` store can ever target it and the loader applies no relocations to it (zero `.rela.dyn` entries in `[0x1c60480, 0x1c64d48)`). Every field is an inline `int32`, `int32[8]`, or IEEE-754 `double` literal frozen at link time. The "producer" is the C++ front-end that emitted the `static const DeviceTypeInfo[17]` initializer; the 13 byte-identical copies scattered through `.lrodata` are identical-code-folding (ICF) data duplicates of the per-instantiation references, not independent tables.

The reader story is where the roofline lives — and the key finding is that **libtpu does not evaluate a roofline**. It *stamps the roofline's inputs*. Three reader families pull per-row fields and write them onto the XPlane as device-capability `XStat`s: `ConvertTpuTraceToXPlaneV2` emits the peak-TFLOP/s and per-memory-space bandwidth block (with a base-10 GB/s → GiB/s conversion on every bandwidth member); `ConvertTpuTraceToXPlane` feeds the v7x perf-counter-set masks into the counter-name resolver; and `ConvertFirmwareTraceEntriesToXPlane` feeds the power/thermal coefficient bundle into `FirmwareEventBuilder`. A fourth reader, the cost model's `XProfTpuCostAnalysis::HandleConvolution`, is the only in-binary consumer that does roofline-style *arithmetic* on the peak-FLOP/s fields — a min-over-operand-precisions divided by core count. Everything below `+0xb8` that is not on those paths (effective-HBM-bandwidth, latency, secondary-rate doubles) has **no in-binary reader at all**; the host-side xprof roofline tool consumes them after they are stamped.

For reimplementation, the contract is:

- The producer model: a compile-time `static const DeviceTypeInfo[17]` in read-only `.lrodata`, indexed `base + ordinal*0x448`, with **no** writer, **no** relocations, and **no** pointer fields — addressed through `lea GOT; movabs (copy − GOT); add; imul ordinal,0x448`.
- The device-capability stat-stamp path: the `StatType 0x62..0x6b` → table-offset map, the `×1.073741824` GB→GiB factor applied to the five bandwidth stats, and the `peak_*` stat-string set they populate.
- The cost-model reader: `HandleConvolution`'s per-element-type `vminsd` over `+0x60/+0x78/+0x80` divided by core count, sourced from `xprof_tpu_cost_analysis.cc:190`.
- The producer→table→reader provenance: `kDeviceTypeInfo` is an **independent** profiler spec table, *not* a byte-frozen copy of the compiler's `chip_parts` FlopsPerSecond — they share a hardware pedigree but diverge per generation.

| | |
|---|---|
| **Table symbol** | `xprof::kDeviceTypeInfo` (`_ZN5xprofL15kDeviceTypeInfoE`) @ `0x1c60480` |
| **Layout** | `17 × 0x448` = `18632` bytes; LOCAL OBJECT; ends `0x1c64d48` |
| **Section / perm** | `.lrodata` `[0x1884a00, 0x84931d0)`; `perm 4` (R, **no W**, **no X**) |
| **Producer** | compile-time `static const` aggregate; **no runtime writer**; zero relocations |
| **ICF copies** | 13 byte-identical data folds; addressed `base + ordinal*0x448` |
| **Cap-stat reader** | `ConvertTpuTraceToXPlaneV2<…jxc>::lambda` @ `0xf1da7c0` |
| **Cost reader** | `XProfTpuCostAnalysis::HandleConvolution` @ `0xf58e7c0` |
| **Perf-set reader** | `ConvertTpuTraceToXPlane<…jxc>` @ `0xf23f8c0` |
| **Firmware reader** | `ConvertFirmwareTraceEntriesToXPlane<…vlc>` @ `0xf27f140` |
| **GB→GiB factor** | `qword_A2E0210` = `1.073741824` (= 2³⁰ / 10⁹) |

---

## The Producer — A Compile-Time Static Const, No Runtime Writer

### Purpose

A reimplementer's first question is whether `kDeviceTypeInfo` is filled by a constructor at process start (which would have to be reproduced) or baked into the image (which only has to be transcribed). The binary settles it: **baked**. There is no producer function in libtpu; the bytes are link-time constants.

### Evidence

Three independent facts each suffice; together they are conclusive.

```text
1. Section + permission.  kDeviceTypeInfo @0x1c60480 lies in .lrodata,
   mapped [0x1884a00, 0x84931d0) with perm 4 (has_write=False, has_code=False).
   A runtime store to 0x1c60480 would fault — the page is never writable.

2. Relocations.  .rela.dyn carries ZERO entries in [0x1c60480, 0x1c64d48).
   No field is a relocated pointer; every field is an inline scalar/float
   literal.  ⇒ no codename-string ptr, no per-gen sub-table ptr — nothing
   the loader patches, nothing a constructor fills.

3. Symbol shape.  nm reports a LOCAL OBJECT of size 18632 = 17 × 0x448,
   exactly the 17-entry stride.  A runtime-built table would be in .bss
   or .data (writable); this one is rodata-class.
```

The symbol resolves cleanly in the name table:

```text
_ZN5xprofL15kDeviceTypeInfoE  → xprof::kDeviceTypeInfo  (13 entries, all same name, size 0x48c8 each)
   0x1c3a910 0x1c3f1e0 0x1c43ab0 0x1c48380 0x1c4e140 0x1c52a10 0x1c572e0
   0x1c5bbb0 0x1c60480 0x1c7bbf0 0x1c804c0 0x1c84d90 0x1c89660
```

nm reports thirteen LOCAL OBJECT entries, all carrying the **identical** mangled name `_ZN5xprofL15kDeviceTypeInfoE` (no `_0`/`_1` disambiguator suffix) and the identical size `0x48c8`; `0x1c60480` is the copy this page's reader VAs resolve against. The `L` in the mangling (`...L15kDeviceTypeInfoE`) marks it `static` (internal linkage) — a translation-unit-local `const` array, exactly what `static const DeviceTypeInfo kDeviceTypeInfo[17] = { … }` produces. Because it is internal-linkage and referenced from many distinct template/pass instantiations, the linker's ICF pass folded the identical *data* into duplicates rather than collapsing them to one symbol; the 13 copies are a folding artifact, not 13 different tables.

> **NOTE —** "no producer function" does not mean "no producer." The producer is the compiler emitting a `static const` aggregate initializer. A reimplementation reproduces it as a `constexpr`/`static const` array, not as an `__attribute__((constructor))` or a lazy-init singleton. Treating it as runtime-initialized would add a writer the original does not have.

### Addressing Mode — How Readers Find a Row

Every reader reaches a row the same way. The table base is materialized relative to the GOT (so the same code works against whichever ICF copy the linker assigned to that translation unit), and the row is selected by multiplying the `DeviceType` ordinal by the `0x448` stride.

```c
// The canonical row-load, as it appears in every reader (e.g. 0xf1da7c0):
base = &GLOBAL_OFFSET_TABLE_ + (copy_VMA - GOT_anchor) / 8;   // lea + movabs fold
if (ordinal >= 0x11) trap();                                  // 17-entry bound check
row  = (char*)base + 0x448 * ordinal;                         // imul ordinal, 0x448
field = *(double*)(row + disp);                               // vmovsd [base+idx+disp]
```

> **GOTCHA —** the IDA decompiler renders the stride as `137 * ordinal` over an 8-byte-element pointer (`137 × 8 = 0x448`) or `274 * ordinal` over a 4-byte-element pointer (`274 × 4 = 0x448`). Both are the same `0x448` byte stride; do not read `137`/`274` as element counts. The recurring `ordinal >= 0x11` guard (`ud1` trap above it) is the 17-row bound, present at every load site.

### Provenance — Independent of `chip_parts`, Not a Copy

The compiler's per-generation hardware constants live in the embedded `chip_parts` protos (see [chip_parts.binarypb](../targets/chip-parts-binarypb.md)); the profiler's live here. They are **two parallel spec stores joined only by the `DeviceType` ordinal**, not one frozen copy. Decoding the `chip_parts` `FlopsPerSecond` tables and comparing against the `kDeviceTypeInfo +0x60/+0x78/+0x80` peak doubles shows the bf16 figure and *all* v5p slots diverge:

| Slot (`kDeviceTypeInfo` offset) | `kDeviceTypeInfo` TFLOP/s | `chip_parts` /1e12 | Ratio | Match | Confidence |
|---|---|---|---|---|---|
| v5p bf16 `+0x60` | 236.7 | 197.0 | 1.202 | NO | High |
| v5p int8 `+0x78` | 466.2 | 394.0 | 1.183 | NO | High |
| v5p int4 `+0x80` | 925.2 | 788.0 | 1.174 | NO | High |
| v6e bf16 `+0x60` | 946.7 | 918.0 | 1.031 | NO | High |
| v6e int8 `+0x78` | 1835.0 | 1835.0 | 1.000 | YES | High |
| v6e int4 `+0x80` | 3670.0 | 3670.0 | 1.000 | YES | High |

Only the v6e int8/int4 slots coincide byte-exactly — and that is because both derive from the same `systolic_dim² × clock` hardware spec, not because the profiler copied the compiler blob. The bf16 figures track a higher-clock derivation (the v6e `+0x60` 946.7 implies the raw `256² × clock` figure, where `chip_parts` 918 is the post-overhead public number). For a reimplementer this means the profiler's roofline ceiling and the compiler's cost model can legitimately disagree per generation; they must not be unified.

---

## Reader 1 — Device-Capability Stat Block (`ConvertTpuTraceToXPlaneV2`)

### Purpose

This is the central roofline-input producer. When the profiler converts a TPU device trace into an XPlane, it stamps a block of device-capability `XStat`s onto the device plane: peak TFLOP/s and the peak per-memory-space bandwidths. The host-side xprof roofline tool reads these stats back off the XPlane to draw the roofline. libtpu's job ends at stamping; it does no ceiling-vs-bandwidth classification itself.

### Entry Point

```text
ConvertTpuTraceToXPlaneV2<…jxc::PerformanceTraceEntry>   ── device-trace → XPlane (F-copy)
  └─ lambda @0xf1da7c0                                    ── stamps the capability stat block
       ├─ GetStatTypeStr(98..108)                         ── 0x1cf8c420, StatType → metadata key
       ├─ vmovsd [base + idx*0x448 + disp]                ── load the per-row spec double
       ├─ vmulsd ×qword_A2E0210 (=1.073741824)            ── GB/s → GiB/s, bandwidth members only
       └─ XStatsBuilder::SetStatValue                     ── write the XStat onto the plane
```

### Algorithm

The lambda walks a fixed run of stat IDs. For each, it fetches the stat-name metadata via `GetStatTypeStr(id)`, loads one `kDeviceTypeInfo` field, optionally multiplies by the GB→GiB factor, and appends the `XStat`.

```c
function StampDeviceCapabilities(plane_builder, device_type):     // 0xf1da7c0
    bound_check(device_type < 0x11)                               // 17-row guard
    base = kDeviceTypeInfo                                        // GOT-relative ICF fold
    row  = base + device_type * 0x448

    emit(0x62, row[+0x60])                          // peak_teraflops_per_second  — no scaling
    emit(0x63, row[+0xd0] * GiB)                    // peak_hbm_bw_…              — ×1.073741824
    emit(0x64, row[+0x120] * GiB)                   // peak_*_bw (CMEM-class, v4) — ×GiB
    emit(0x65, row[+0x128] * GiB)                   // peak_*_bw (CMEM-class, v4) — ×GiB
    emit(0x66, row[+0x100] * GiB)                   // peak_sram/vmem_*_bw_…      — ×GiB
    emit(0x67, row[+0x108] * GiB)                   // peak_sram/vmem_*_bw_…      — ×GiB
    emit_byte(0x6b, byte(row[+0x2c4]))              // has_megacore               — BYTE flag
    // 0x68/0x69 read a DIFFERENT sub-indexed slot (see GOTCHA below), not this ladder
    // 0x6c.. continue with non-table stats (core counts, gtc, …)
```

The GB→GiB factor `1.073741824` is exactly `2³⁰ / 10⁹`: the spec table stores base-10 GB/s (`10⁹` bytes/s), and the stat is emitted in GiB/s (`2³⁰` bytes per GiB), i.e. bytes/ns. The peak-TFLOP/s stat (`0x62`) is *not* scaled because TFLOP/s is already a base-10 rate with no binary-prefix reinterpretation.

### StatType → Offset Map

Each row was confirmed by the disassembled `vmovsd` displacement at the cited site; the multiply is present iff the member is a bandwidth.

| StatType | `GetStatTypeStr` arg | Table offset | Scaling | Stat string (`.rodata`) | Site | Confidence |
|---|---|---|---|---|---|---|
| `0x62` | 98 | `+0x60` | none | `peak_teraflops_per_second` (`0x86ed48f`) | `0xf1daa9b` | High |
| `0x63` | 99 | `+0xd0` | ×GiB | `peak_hbm_bw_gigabytes_per_second` (`0x86ed53b`) | `0xf1dab40` | High |
| `0x64` | 100 | `+0x120` | ×GiB | a `peak_*_bw_…` (CMEM-class, v4-only nonzero) | `0xf1dabe6` | Medium |
| `0x65` | 101 | `+0x128` | ×GiB | a `peak_*_bw_…` (CMEM-class, v4-only nonzero) | `0xf1dac8c` | Medium |
| `0x66` | 102 | `+0x100` | ×GiB | a `peak_sram/vmem_*_bw_…` | `0xf1dad32` | Medium |
| `0x67` | 103 | `+0x108` | ×GiB | a `peak_sram/vmem_*_bw_…` | `0xf1dadd8` | Medium |
| `0x6b` | 107 | `+0x2c4` (BYTE) | none | `has_megacore` (`0x86a4474`) | `0xf1db020` | High |

The eight `peak_*` stat strings are contiguous in `.rodata`:

```text
0x86ed48f  peak_teraflops_per_second
0x86ed4cc  peak_vmem_wr_bw_gigabytes_per_second
0x86ed4f1  peak_cmem_wr_bw_gigabytes_per_second
0x86ed516  peak_sram_wr_bw_gigabytes_per_second
0x86ed53b  peak_hbm_bw_gigabytes_per_second
0x86ed55c  peak_vmem_rd_bw_gigabytes_per_second
0x86ed581  peak_cmem_rd_bw_gigabytes_per_second
0x86ed5a6  peak_sram_rd_bw_gigabytes_per_second
```

The `+0xd0` HBM-bandwidth member is the authoritative one and is byte-exact against the compiler's `chip_parts` HBM bandwidth: v7x `+0xd0` = 3433 GB/s × 1.073741824 = **3686.2 GiB/s** == `chip_parts` 3.686 TB/s; v6e 1525.5 × 1.073741824 = **1638.0 GiB/s** == 1.638 TB/s. This is the cross-check that pins `+0xd0` as `peak_hbm_bw_gigabytes_per_second` rather than the value-scaled `+0xb8` group (which is *not* read here).

> **CORRECTION (KDTI-1) —** an earlier reading labeled the `+0xf8/+0x100/+0x108/+0x120/+0x128` group "peak-count" (peak ops / systolic cells). The disassembly shows the members that are read (`+0x100/+0x108/+0x120/+0x128`) are **per-memory-space bandwidths in GB/s**, each multiplied by the `1.073741824` GiB factor before stamping; the v4-only `+0x120/+0x128` line up with CMEM bandwidth (v4 is the only CMEM generation). `+0xf8/+0x110/+0x118/+0x130` are not read by this build.

> **GOTCHA —** StatType `0x68`/`0x69` (`GetStatTypeStr(104)`/`(105)`) do **not** read the simple `row + disp` bandwidth ladder. They compute `&base[0x448*idx] + 32*lut[idx-7] + {0xa0|0xa8}`, where `lut = qword_AB58328` is a 7-entry per-generation sub-index. The `+0xa0/+0xa8` slots are zero across the table in this build, so these two stats emit zero; a reimplementer copying the `0x62..0x67` pattern verbatim onto `0x68/0x69` would mis-address them.

> **NOTE —** which of the six `peak_{sram,vmem,cmem}_{rd,wr}_bw_…` strings each of `0x64/0x65/0x66/0x67` binds to is **not** byte-resolved: `GetStatTypeStr` is a lazy hash map seeded by an inline `initializer_list` in `.text.startup`, and that seed order was not decoded. The HBM (`+0xd0`) and CMEM (`+0x120/+0x128`, v4-only) bindings are unambiguous from value cross-check; the SRAM-vs-VMEM split inside `0x66/0x67` is Medium confidence.

---

## Reader 2 — Cost-Model Peak FLOP/s (`HandleConvolution`)

### Purpose

This is the only in-binary reader that performs roofline-style *arithmetic* on the peak-FLOP/s fields rather than just stamping them. `XProfTpuCostAnalysis::HandleConvolution` computes a convolution's peak achievable rate by taking, over all operand precisions, the **slowest** (minimum) peak-FLOP/s the hardware offers for that precision, then dividing by the core count — the per-op compute ceiling the cost model charges against.

### Entry Point

```text
XProfTpuCostAnalysis::HandleConvolution  @0xf58e7c0   ── src: xprof_tpu_cost_analysis.cc:190
  ├─ load row[+0x60]  (init, bf16/fp32-class ceiling)
  ├─ for each operand: switch(Shape::element_type - 2)
  │     ├─ int8-class  → vminsd against row[+0x78]   (≈2× +0x60)
  │     └─ int4-class  → vminsd against row[+0x80]   (≈4× +0x60)
  └─ vdivsd by core count / fixed divisors (6.0, 3.0)
```

### Algorithm

```c
function HandleConvolution(this, conv):                       // 0xf58e7c0
    if not ShapeUtil::IsConvolution(conv):                    // 190 → AddSourceLocationImpl
        return error("xprof_tpu_cost_analysis.cc:190")
    idx   = this.device_type                                  // *((int*)this + 115)
    base  = kDeviceTypeInfo + idx*0x448
    peak  = base[+0x60]                                       // bf16/fp32-class peak (TFLOP/s)
    acc   = DBL_MAX                                           // qword_A2DEAF0 — vminsd seed
    for operand in conv.operands:
        et = Shape::element_type(operand) - 2                 // 28-way switch
        switch et:
            case int8-class:  acc = min(acc, base[+0x78])     // +0x78  (cmp ecx,6 sub-index)
            case int4-class:  acc = min(acc, base[+0x80])     // +0x80  (cmp ecx,3 sub-index)
            // fp32 / bf16 paths keep base[+0x60]
    rate = min(acc, peak)
    return rate / 6.0 / 3.0                                   // qword_A2DE720, qword_A2DF930 — ÷cores
```

The `vminsd`-accumulate-over-operands is the key idea: a convolution mixing a bf16 input and an int8 weight is charged at the int8 ceiling only if int8 is the *slower* path — the running minimum picks whichever precision the hardware sustains worst. The `÷6.0 ÷3.0` chain is the per-core normalization (the v7x `cores × logical-devices` divisor). The byte-exact `+0x78 ≈ 2×` and `+0x80 ≈ 4×` ratios versus `+0x60` (1.94–1.97× and 3.88–3.92× across v5p/v5e/v6e) confirm these are the int8/fp8 and int4/fp4 peak-FLOP/s slots.

> **NOTE —** the v7x row stores its int8 figure 1992 at *both* `+0x68` and `+0x80`, while `+0x78` holds 996; the v7x element-type dispatch differs slightly from the v5/v6 path (it min's a bf16 operand against a literal `1992.0`, `qword_A2DECF8`). Whether `+0x68` is an alternate int8 slot or a double-pumped fp8 rate is not resolved (Low confidence) — for v5/v6, `+0x78` is the 2× slot.

This reader is shared with the XLA cost model; see [TpuHloCostAnalysis](../cost/tpu-hlo-cost-analysis.md) for the surrounding analysis and [Memory Bandwidth & Latency Model](../cost/memory-bandwidth-latency-model.md) for the bandwidth-side cost path.

---

## Reader 3 — Perf-Counter-Set Masks (`ConvertTpuTraceToXPlane`)

### Purpose

The G-copy converter reads four `+0x2c8/+0x348/+0x350/+0x358` dwords (nonzero only on the v7x row) and passes each as the per-set enum **base** to `GetPerformanceCounterNames<N>`, which turns user-selected counter ordinals into the on-die register names stamped onto the v7x counter timelines. This page establishes only that the *table fields feed the resolver*; the resolver itself and its `DeviceType == 12` gate are documented in [v7x Perf-Counters](v7x-perf-counters.md).

### Entry Point

```text
ConvertTpuTraceToXPlane<…jxc::PerformanceTraceEntry>  @0xf23f8c0   (G-copy)
  ├─ GetPerformanceCounterNames<28>(dt, idxs, n>>1, row[+0x2c8], out)   @0xf240980
  ├─ GetPerformanceCounterNames<28>(dt, …,         row[+0x348], out)
  ├─ GetPerformanceCounterNames<28>(dt, …,         row[+0x350], out)
  ├─ GetPerformanceCounterNames<28>(dt, …,         row[+0x358], out)
  ├─ GetPerformanceCounterNames<3> (dt, …,         row[+0x440], out)    @0xf240ac0
  └─ GetPerformanceCounterNames<12>(dt, …,         row[+0x438], out)    @0xf240c00
```

The six fields are passed as the resolver's `base` argument (`a4`), which the resolver uses as `PerformanceCounterNameToString(base + 8*ordinal)`. Mapping the six call-site GOT displacements back through `base + ordinal*0x448` resolves them to exactly `+0x2c8/+0x348/+0x350/+0x358` (the four `<28>` TensorCore/SparseCore sets), `+0x440` (the `<3>` CMNUR/HBM set), and `+0x438` (the `<12>` ICR set) — the same six descriptor fields the sibling page recovered from the DT12 row at `0x1c637e0`.

| Counter set | Resolver | Table field | Confidence |
|---|---|---|---|
| TCS / SCS / SCTC / SCTD | `<28>` @ `0xf240980` | `+0x2c8` / `+0x348` / `+0x350` / `+0x358` | High |
| CMNUR (HBM/mem-net) | `<3>` @ `0xf240ac0` | `+0x440` | High |
| ICR (router) | `<12>` @ `0xf240c00` | `+0x438` | High |

---

## Reader 4 — Firmware Power/Thermal Bundle (`ConvertFirmwareTraceEntriesToXPlane`)

### Purpose

The firmware-trace converter reads the `+0x360..+0x398` calibration bundle — four doubles and four ulongs — and passes them verbatim into the `FirmwareEventBuilder` constructor, which stores them and uses them to stamp `power` and `temperature` `XStat`s on firmware events (the per-generation DVFS/thermal model).

### Entry Point

```text
ConvertFirmwareTraceEntriesToXPlane<…vlc>  @0xf27f140   (G-copy)
  └─ FirmwareEventBuilder<…vlc>::FirmwareEventBuilder(   @0xf284d20
         TpuXPlaneBuilder*, d,d,d,d, m,m,m,m)
       ├─ doubles  ← row[+0x360 / +0x368 / +0x370 / +0x378]   (XMM args)
       ├─ ulongs   ← row[+0x380 / +0x388 / +0x390 / +0x398]   (GP args)
       └─ stamps "power" / "temperature" XStats per firmware event
```

The constructor signature is byte-confirmed by its mangling: `…FirmwareEventBuilder…C2EPNS_16TpuXPlaneBuilderEddddmmmm` — a `TpuXPlaneBuilder*` followed by four `double`s and four `unsigned long`s. The reader loads the four doubles into `xmm0..xmm3` from `row[+0x360..+0x378]` and the four ulongs into the GP argument registers from `row[+0x380..+0x398]`, then constructs the builder. The builder stores them at `obj+0x10..+0x48` and consumes them in its power/temperature formulas. The precise role of each coefficient (voltage², current, thermal slope; the ulongs as P-state ids or thermal thresholds) is not decoded here — see [v7x Perf-Counters § Firmware Event Model](v7x-perf-counters.md) for the builder internals.

---

## The Consumed-Offset Census

Combining the four readers above with the geometry/clock readers, the table partitions into **fields with an in-binary reader** and **fields read only by the host roofline/power model**. The latter are spec constants this binary stamps but does not interpret.

### Read by libtpu (CONFIRMED-CONSUMED)

| Offset(s) | Field | Reader | Confidence |
|---|---|---|---|
| `+0x00` | core/multi flag (BYTE) | plane setup, `GetJobInfoFromResponse` | High |
| `+0x04` / `+0x50` / `+0x2f8` | GTC / TensorCore / SparseCore clock (kHz) | `GetJobInfoFromResponse` (`×1000` → Hz) | High |
| `+0x0c` / `+0x10` / `+0x14` | cores/chip, logical-device counts | `ToDeviceOrdinal` (`÷`), `HandleConvolution`, `ExtractUtilizationCounters` | High |
| `+0x60` / `+0x78` / `+0x80` | peak FLOP/s bf16 / int8 / int4 | `HandleConvolution` (`vminsd`), V2 stat `0x62` | High |
| `+0xd0` | peak HBM bandwidth (GB/s) | V2 stat `0x63` (`×GiB`) | High |
| `+0x100` / `+0x108` / `+0x120` / `+0x128` | per-mem-space bandwidth (GB/s) | V2 stats `0x66/0x67/0x64/0x65` (`×GiB`) | High |
| `+0x2c4` | has_megacore (BYTE) | V2 stat `0x6b`, `GetCostAdjustmentFunction` | High |
| `+0x2c8` / `+0x348` / `+0x350` / `+0x358` / `+0x438` / `+0x440` | perf-counter-set bases | `GetPerformanceCounterNames<28/12/3>` | High |
| `+0x360..+0x378` (d) / `+0x380..+0x398` (m) | power/thermal bundle | `FirmwareEventBuilder` | High |
| `+0x2b8` / `+0x2f8` / `+0x340` | packed geom, SC clock, SC lane count | `ProcessCounter`, SC subscribers | High |

### Read only off-binary (INFERRED — no in-libtpu reader)

```text
+0x28, +0x2d0   two 8-point DVFS frequency ladders (v7x-only)
+0x58, +0x68    per-LD bf16 / v7x int8-alt peak
+0xb8..+0xc8    effective HBM bandwidth class (value-scaled to bw; authoritative bw is +0xd0)
+0xd8..+0xf0    memory-latency class
+0xf8/+0x110/+0x118/+0x130   extra bandwidth/count members (not stamped this build)
+0x138..+0x150  secondary rate "B"
+0x178..+0x190  secondary rate "C"
+0x300/+0x308   core voltage / power
+0x438/+0x440 tail — verified perf-counter-set bases above, NOT a roofline double
```

No `[base + ordinal*0x448 + disp]` load with these displacements exists anywhere in `.text` (94-site reference census). They are the host xprof roofline/utilization/power model's inputs, stamped onto the XPlane (the bandwidth/peak subset) or carried as spec the host tool sub-selects.

> **QUIRK —** the answer to "where is the roofline?" is that libtpu is the **producer of the roofline's inputs, not its evaluator**. It stamps `peak_teraflops_per_second` + the four `peak_*_bw_gigabytes_per_second` stats + `has_megacore` + the perf-counter sets + the power/thermal bundle onto the device XPlane; the TFLOP/s-ceiling-vs-effective-bandwidth classifier runs in the host xprof tool that reads the plane. A reimplementation that looks for an in-libtpu roofline-arithmetic function over `+0xb8/+0xd8/+0x138` will not find one — those doubles leave the binary unread.

---

## Related Components

| Component | Relationship |
|---|---|
| `GetJobInfoFromResponse` @ `0xf2c9ac0` | reads `+0x00/+0x04/+0x50/+0x2f8`, lifts the three clocks `×1000` kHz→Hz into the device-info record |
| `ToDeviceOrdinal` @ `0xf69c580` | reads `+0x0c/+0x10/+0x14` geometry to map `(host, core)` → device ordinal |
| `GetCostAdjustmentFunction` @ `0xf58efc0` | reads `+0x2c4` megacore flag + `+0x0c` cores/chip to pick the cost-adjust function |
| `ExtractUtilizationCounters` @ `0xf0f7fa0` | reads `+0x0c` as a `double` divisor for utilization denominators |
| `FirmwareEventBuilder` ctor @ `0xf284d20` | stores the `+0x360..+0x398` bundle; emits `power`/`temperature` |

## Cross-References

- [Per-DeviceType Profiler Struct](per-devicetype-struct.md) — sibling page; the byte-level `0x448` field layout and `DeviceType`↔codename map this page reads from
- [v7x Perf-Counters](v7x-perf-counters.md) — owns the `+0x2c8/+0x348/+0x350/+0x358/+0x438/+0x440` descriptor fields, the `DeviceType == 12` gate, and the `FirmwareEventBuilder` event model
- [Profiling and Telemetry](overview.md) — the XPlane/XStat pipeline this stamping feeds
- [Task Proto](task-proto.md) — the device-info clock fields (`gtc/tensor_core/sparse_core_freq_hz`) populated from `+0x04/+0x50/+0x2f8`
- [TpuHloCostAnalysis](../cost/tpu-hlo-cost-analysis.md) — the cost model that shares `HandleConvolution`'s peak-FLOP/s read
- [Memory Bandwidth & Latency Model](../cost/memory-bandwidth-latency-model.md) — the bandwidth-side cost path; the host-roofline consumer frame
- [LocalDmaBandwidth](../cost/local-dma-bandwidth.md) — the compiler-side per-gen bandwidth accessors that parallel `+0xb8/+0xd0`
- [chip_parts.binarypb](../targets/chip-parts-binarypb.md) — the *other* per-gen spec source; `FlopsPerSecond` cross-validated against `+0x60/+0x78/+0x80` here
