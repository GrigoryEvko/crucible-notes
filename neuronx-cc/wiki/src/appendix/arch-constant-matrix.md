# Appendix — The Arch-Model Constant Matrix

> *All immediates, offsets, and addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp310/cp311/cp312 carry the geometry byte-identically — the `ctm` source is recompiled per wheel but the constructor immediates are the same). For `.text`/`.rodata` of `libwalrus.so` / `libBIR.so`, virtual address equals file offset. Other wheels drift; treat every address as version-pinned.*

## Abstract

This is the single consolidated ledger of every per-generation hardware constant the Neuron compiler bakes into its arch model. Each value is an immediate stamped into one of the four per-architecture constructor families (`<Arch>{Core, Statebuf, Psumbuf, Pe, Pool, Act, Dve, Device, Board}`) at library-build time — there is no device probe, no config file, no environment override. The compiler's entire idea of "what the chip is" is a static singleton object tree, one per generation, that `getArchModel(codename)` selects by alias string. This appendix collapses the four Part-1 hardware pages ([1.04](../arch/hardware-constant-matrix.md), [1.05](../arch/sbuf-psum-geometry.md), [1.06](../arch/dram-hbm-geometry.md), [1.03](../arch/vestigial-generations.md)) into one quantitative reference: SBUF and PSUM geometry, PE-array dimensions, the engine widths, the semaphore count, the per-core/per-device HBM split, the cost-model clocks, and the MX / FP4 / sparse availability axis — for every generation the binary models.

The model spans **five** generations on its ordinal ladder (`10/20/30/40/50` = CoreV1…CoreV5) but ships geometry for **four**. `getArchModel` dispatches ten codename aliases onto four `Board` singletons — gen1 Inferentia (`tonga`/`inferentia`/`inf1`), gen2 Sunda (`sunda`/`trainium`/`trn1`/`inf2`), gen3 Cayman (`cayman`/`gen3`), gen4 CoreV4 / "mariana" (`core_v4`) = 3+4+2+1 = **10** compare arms (`core_v5` is *not* an arm; its literal follows the `__assert_fail` string). The fifth rung, gen5 `core_v5`, is a forward-declaration stub: a reserved ordinal with **no** `Board`, so `getArchModel` falls through to `__assert_fail("0 && Unknown architecture")`. Its row in the master table below is therefore almost entirely INFERRED — projected from the `CoreV4 ▸ CoreV3 ▸ CoreV2` single-inheritance ladder and the dormant `< ArchLevel::core_v5` feature gates, never read from a constructor. The codename↔generation decode (which internal name maps to which marketed part) is owned by [1.02](../arch/codename-taxonomy.md); this page names the codename per column and otherwise stays numeric.

Every cell here is read off the `<Arch>` constructor immediate or the literal getter body rather than copied from the Part-1 prose. Two places where the natural reading diverges from the constructors are flagged inline: the idea that MX occupies a gen4 PE dtype slot (it does not exist in the geometry tree), and the Inferentia per-core-versus-device HBM arithmetic. The confidence ladder is the standard four-tier ([methodology](../methodology.md)); the `Conf` column tags each row, and the vestigial CoreV5 column carries its own per-cell tags.

| | |
|---|---|
| **Geometry source** | `ctm.cpython-3xx.so` + the byte-identical copy in `libwalrus.so` / `libBIR.so` — per-arch `<Arch>{Core,Statebuf,Psumbuf,Pe,Pool,Act,Dve}` ctors |
| **Field-name source-of-truth** | `data/include/hwm/ctm/ctm.hpp` — a shipped C++ header, so the field names are exact |
| **Dispatch entry** | `getArchModel(const string&)` @ `libwalrus 0x17344c0` / `libBIR 0x478f90` — alias → `Board*` (4 arms + assert) |
| **Object graph** | `Board +0x8→Device +0x10→Core +{0x00..0x28 engines, 0x30 dramSizeGb}` |
| **Cost model (cross-check)** | `TrainiumHwm` / `Gen3Hwm` / `CoreV4Hwm` in `walrus_driver` — independent re-derivation of bank size + clocks |
| **Generations** | gen1 Inferentia · gen2 Sunda · gen3 Cayman · gen4 CoreV4 · gen5 CoreV5 (stub) |

---

## How a constant is sourced

Every number on this page is sourced the same way: locate the `<Arch><SubObject>` constructor, read the immediate it passes to the base-class constructor. The base signatures come from `ctm.hpp`, so the argument *positions* have real names; the *values* are the per-arch `mov`/`push` immediates. For example `SundaPsumbuf::SundaPsumbuf` is, in full, `Psumbuf::Psumbuf(this, 8u, 0x80u, 0x800u)` — eight banks, 128 partitions, a 2048-byte bank.

```text
getArchModel(codename)                       @0x17344c0  (string-compare dispatch → Board*)
  Board   +0x08 → Device
  Device  +0x00 numCores   +0x08 dramSizeGb(device)   +0x10 → Core
  Core    +0x00 Pe   +0x08 Pool   +0x10 Act   +0x18 Dve
          +0x20 → Psumbuf   +0x28 → Statebuf   +0x30 dramSizeGb(per-core)   +0x34… 23 scalars
```

| Base class | `ctm.hpp` signature | Fields tabulated here | Read at |
|---|---|---|---|
| `Statebuf` | `(partitionSize, SOCStepSize, numPartitions, midPartition, Align&, reservedSize=16384)` | SBUF bytes/partition, partition count | `Statebuf+0x00 / +0x08` |
| `Psumbuf` | `(numBanks, numPartitions, partSize)` | PSUM bank count, partitions, bank bytes | `Psumbuf+0x00 / +0x04 / +0x08` |
| `PeDimensionsForDtype` | `(numRows, numCols, maxWeightStep, minWave)` | systolic rows × cols | `Pe.m16+0x00 / +0x04` |
| `Pool` / `Act` | `(numChannels)` | engine width (partitions) | `+0x00` |
| `Device` | `(Core&, numCores, dramSizeGb)` | cores/device, device HBM | `Device+0x00 / +0x08` |
| `Board` | `(Device&, numDevices)` | devices/board | `Board+0x00` |
| `Core` (`CoreParamSet`) | 24 scalars; first is `dramSizeGb // dram/hbm per core` | per-core HBM, semaphores | `Core+0x30`, `+0x34…` |

> **NOTE — these are baked compile-time constants.** Every immediate below is fixed per codename at library-build time. A reimplementation hard-codes the same constructor families; it does not read geometry from a runtime descriptor. The one consumer that *gates* compilation on a geometry value is `HBMUsage`, which reads `Core+0x30` (per-core HBM) and asserts `usage ≤ window` ([1.06](../arch/dram-hbm-geometry.md)).

---

## The master matrix

The deliverable. Rows are the geometry/parameter axes; the five columns are the generations; **Source** names the exact constructor argument; **Conf** tags the gen1–gen4 cells. The gen5 column is INFERRED throughout (no constructor exists) and is annotated inline.

| Constant | gen1 Inferentia | gen2 Sunda | gen3 Cayman | gen4 CoreV4 | gen5 CoreV5 | Source (ctor → arg) | Conf |
|---|---|---|---|---|---|---|---|
| ArchLevel ordinal | 10 | 20 | 30 | 40 | 50 | `ArchLevel2string` switch | CERTAIN |
| Internal codename | `inferentia` | `sunda` | `gen3` | `core_v4` | `core_v5` | `getArchModel` aliases | CERTAIN |
| Runtime codename | inferentia | sunda | **cayman** | **mariana** | core_v5 | `ArchLevel2RuntimeTarget` | CERTAIN |
| Marketed device | Inf1 | Trn1 | Trn2 | Trn3 | (Trn4)ᵍ | `ArchLevel2ExternalString` | CERTAIN (g1–4) |
| CoreVN / codegen | CoreV1 (legacy) | CoreV2 | CoreV3 | CoreV4 | CoreV5 (stub) | `initCodegen` / `Codegen::codegen` | CERTAIN |
| Codegen target? | analysis-only | **yes** | **yes** | **yes** | no (throws) | `Codegen::codegen` arms `{20,30,40}` | CERTAIN |
| **SBUF B/partition** | 0x18000 = 96 KiB | 0x30000 = 192 KiB | 0x38000 = 224 KiB | 0x40000 = 256 KiB | ≥256 KiBⁱ | `Statebuf` arg0 `partitionSize` | CERTAIN |
| **SBUF total** (×128) | 12 MiB | 24 MiB | 28 MiB | 32 MiB | ≥32 MiBⁱ | arg0 × 128 | CERTAIN |
| SBUF partitions | 128 | 128 | 128 | 128 | (128)ⁱ | `Statebuf` arg2 = 0x80 | CERTAIN |
| SBUF SOC step | 0x20000 | 0x40000 | 0x40000 | 0x40000 | (0x40000)ⁱ | `Statebuf` arg1 `SOCStepSize` | CERTAIN |
| SBUF midPartition | 64 | 64 | 64 | 64 | (64)ⁱ | `Statebuf` arg3 | CERTAIN |
| SBUF reservedSize | 0x4000 | 0x4000 | 0x4008 | 0x4008 | (0x4008)ⁱ | `Statebuf` arg5 | CERTAIN |
| **PSUM bank count** | 4 | 8 | 8 | 8 | (8)ⁱ | `Psumbuf` arg0 `numBanks` | CERTAIN |
| **PSUM bank size** | 2048 B | 2048 B | 2048 B | 2048 B | (2048)ⁱ | `Psumbuf` arg2 = 0x800 | CERTAIN |
| PSUM partitions | 64 | 128 | 128 | 128 | (128)ⁱ | `Psumbuf` arg1 | CERTAIN |
| PSUM phys/partition | 8 KiB | 16 KiB | 16 KiB | 16 KiB | (16 KiB)ⁱ | banks × partSize | CERTAIN |
| PSUM phys total | 0.5 MiB | 2 MiB | 2 MiB | 2 MiB | (2 MiB)ⁱ | banks × partSize × parts | CERTAIN |
| **PE array rows × cols** | 128 × 64 | 128 × 128 | 128 × 128 | 128 × 128 | (128 × 128)ⁱ | `PeDimensionsForDtype` arg0×arg1 | CERTAIN |
| PE maxWeightStep | 2 | 2 | 2 | 2 | (2)ⁱ | `PeDimensionsForDtype` arg2 | CERTAIN |
| PE minWave | 128 | 128 | 128 | 128 | (128)ⁱ | `PeDimensionsForDtype` arg3 = 0x80 | CERTAIN |
| Pool channels | 64 | 128 | 128 | 128 | (128)ⁱ | `Pool` arg0 `numChannels` | CERTAIN |
| Act channels | 64 | 128 | 128 | 128 | (128)ⁱ | `Act` arg0 `numChannels` | CERTAIN |
| Act/Dve reorder win | 8 | 8 | 8 | **16** | (16)ⁱ | `<Arch>{Act,Dve}::getReorderWindowSize` | CERTAIN |
| Pool reorder win | 8 | 8 | 8 | 8 | (8)ⁱ | `<Arch>Pool::getReorderWindowSize` | CERTAIN |
| **Semaphores / core** | 256 | 256 | 256 | 256 | (256)ⁱ | `CoreParamSet.NumSemaphores` = 0x100 | CERTAIN |
| MaxRegNumPerEngine | 62 | 62 | 62 | 62 | (62)ⁱ | `NUM_REGISTERS − 2` | CERTAIN |
| **Per-core HBM** | 16 GiB | 16 GiB | 24 GiB | 36 GiB | ≥36 GiBⁱ | `Core::dramSizeGb` (`Core+0x30`) | CERTAIN |
| Device HBM | 32 GiB | 32 GiB | 24 GiB | 36 GiB | (≥36)ⁱ | `Device::dramSizeGb` (arg2) | CERTAIN |
| NeuronCores / device | 4 | 2 | 2 | 2 | (2)ⁱ | `Device` arg1 `numCores` | CERTAIN |
| Devices / board | 2 | 2 | 2 | 2 | (2)ⁱ | `Board` arg1 `numDevices` | CERTAIN |
| Per-engine count | 1 each | 1 each | 1 each | 1 each | (1)ⁱ | `CoreParamSet.*EngineCount` | CERTAIN |
| **Cost-model freq** PE/Pool/Act | (140)¹ | 140 | 120 | 120 | —ⁱ | `<Hwm>::getEngineFrequency` ord 1–3 | CERTAIN |
| **Cost-model freq** SP | (112)¹ | 112 | 96 | 120 | —ⁱ | `<Hwm>::getEngineFrequency` ord 5 | CERTAIN |
| DVE default opcodes | —² | 46 | 52 | 59 | —ⁱ | `dve_bin_gen{2,3,4}` opcode_table | CERTAIN |
| Sparse matmul | yes³ | yes³ | yes³ | yes³ | (yes)ⁱ | `InstMatmultSparse` opcodes 6/7 | HIGH |
| MX / FP4 microscaling | no | no | no | **yes⁴** | (yes)ⁱ | `CoreV4Hwm::getLatency(InstMatmultMx)` | HIGH |

`ⁱ` gen5 cells are INFERRED — projected from the `CoreV4` ladder; **no `CoreV5` constructor exists** (`getArchModel` asserts). `(parenthesized)` = inheritance projection; `≥` = a plausible non-regressing bound, not a binary literal.
`ᵍ` `Trn4` for arch 50 is the `ArchLevel2ExternalString` projection; the string is present in the binary but the part is unannounced — a compiler-side name, not a hardware fact.
`¹` gen1 ships no distinct `Hwm` subclass; the `140/112` figures belong to `TrainiumHwm` (the gen2 cost model). gen1 latency uses the base perf-sim path (see §Cost-model frequencies).
`²` Inferentia carries `InferentiaDve` geometry but no separate `dve_bin_gen1` opcode table in this build.
`³` sparse matmul (`InstMatmultSparse`, PE phase opcodes `6` LoadTags / `7`) is present on the codegen generations, but is not pinned to a per-gen geometry immediate.
`⁴` MX is a **feature**, not a geometry dimension — see §"MX / FP4 / sparse is not a PE column".

> **QUIRK — Inferentia is the only half-width part.** SBUF is 128-partition on *every* generation, gen1 included. But gen1's PSUM partitions, PE columns, Pool channels, and Act channels are all **64** — half the gen2+ width. "128 everywhere" holds for SBUF and PE *rows*, but the PSUM/Pool/Act/PE-cols second dimension is 64 on Inferentia. A reimplementer who hard-codes 128 across the board over-sizes every gen1 engine that is not SBUF or PE-rows.

---

## The Five Safety-Critical Cells

Five cells silently corrupt an allocator or encoder if you get them wrong. Each is traced here to its constructor body and to the consumer that reads it.

**1 — SBUF size per partition.** `<Arch>Statebuf` arg0 (`esi`, stored at `Statebuf+0x00`): `0x18000 / 0x30000 / 0x38000 / 0x40000` = **96 / 192 / 224 / 256 KiB**. Cross-checked at the consumer: `SB_Allocator::SB_Allocator` (@`0xa97750`) reads `getArchModel → Board+0x8 → Device+0x10 → Core+0x28(Statebuf) → Statebuf+0` and caches it at `allocator+0x2b8` (@`0xa97bbd`). The cache slot holds **bytes**, not a partition count — the 128 is the separate `Statebuf+0x8` field.

**2 — PSUM bank count.** `<Arch>Psumbuf` arg0 (`esi`, `Psumbuf+0x00`): `4 / 8 / 8 / 8`. This is read directly from the constructor immediate, not back-derived from the 16 KiB window. Consumer: `PSUM_Allocator::PSUM_Allocator` (@`0xad9970`) walks to `Core+0x20(Psumbuf) → Psumbuf+0` and caches at `allocator+0x0` (@`0xada13b`). Inferentia is the only 4-bank part.

**3 — PE array dimensions.** `<Arch>Pe` builds one `PeDimensionsForDtype(numRows, numCols, maxWeightStep, minWave)`: `InferentiaPe = (0x80, 0x40, 2, 0x80)` = **128 × 64**; `Sunda/Cayman/CoreV4Pe = (0x80, 0x80, 2, 0x80)` = **128 × 128**, byte-identical across gen2/3/4. `ctm.hpp` declares exactly **one** `const PeDimensionsForDtype &m16` — there is no per-dtype array, and no gen4 third dtype slot (see below).

**4 — SBUF partition count.** `<Arch>Statebuf` arg2 (`ecx`, `Statebuf+0x08`) = `0x80` = **128 on all four generations**, including Inferentia. This is the half-width trap's exception: SBUF stays full-width while gen1's PSUM/PE-cols/Pool/Act drop to 64. `Statebuf.numPartitions` (128) and `Psumbuf.numPartitions` (64 on gen1) are *different fields* — never conflate them.

**5 — DRAM split (per-core vs per-device HBM).** `Core::dramSizeGb` (`Core+0x30`, from `CoreParamSet+0x60`) = **16 / 16 / 24 / 36 GiB**; `Device::dramSizeGb` (`Device+0x08`, arg2) = **32 / 32 / 24 / 36 GiB**. They are independent immediates: on gen1, `numCores=4 × 16 = 64 ≠ 32` device — the device figure is its own constant, not a product. The budget gate `HBMUsage::run` (@`0x16b94b0`) reads `Core+0x30 << 30` (per-core, @`0x16ba327`), **never** `Device+0x8` — the gate enforces the smaller per-core window.

---

## MX / FP4 / sparse is not a PE column

> **GOTCHA — there is no gen4 third PE dtype slot.** A libBIR-side PE constructor carries an extra `0x10 = 16` immediate that reads like a third `PeDimensionsForDtype` entry on gen4, suggesting MX/microscaling is a gen4-only PE geometry dimension. It is not. `ctm.hpp` declares exactly one `const PeDimensionsForDtype &m16`, and `CoreV4Pe` builds exactly **one** `PeDimensionsForDtype(128, 128, 2, 128)` — byte-identical to `SundaPe`/`CaymanPe`. Give gen4 the same 128×128 PE as gen2/gen3.

MX / FP4 microscaling and sparse matmul are real per-generation capabilities, but they live at the HLO / BIR / cost-model layers, **not** in the geometry tree:

- **MX / FP4** is the OCP-MXFP path: 32-element blocks (`block_size = 32`) sharing one E8M0 scale byte, lowered to BIR `InstMatmultMx` (opcode 95) / `InstQuantizeMx`, encoded as the `LdWeightMx` (`0x1009`) / `MatmultMx` (`0x100A`) bundle pair. Its gen4-specificity surfaces as a *cost-model override*: `CoreV4Hwm::getLatency(const bir::InstMatmultMx&)` exists (@`0x483de0`) with **no** matching `Gen3Hwm` / `TrainiumHwm` override, so MX matmul has a per-gen latency handler only on gen4. Legality is enforced by `checkMatmultMxInputs` (@`0x1007420`) and `checkMatmultMxInstruction` (@`0x10139a0`) — including the hard `K_weights ≤ 512` bound and PSUM-bank capacity check. Full numeric contract: [MX matmul legality](../numerics/mx-matmul-legality.md).
- **Sparse** matmul (`InstMatmultSparse`) rides the dense PE path with extra phase opcodes `6` (LoadTags) and `7`, present on the codegen generations. It is not pinned to any geometry immediate.

The "MX = gen4" / "sparse on the codegen gens" associations are correct; their mechanism is the legalize-pass + BIR + `Hwm` pipeline, not an extra systolic-array column. A reimplementer building the geometry tree gives every gen2+ part the same 128×128 PE and looks for MX/sparse support in the legalize passes and `Hwm` overrides.

---

## Cost-model frequencies

The three `Hwm` ("hardware/cost model", **not** a high-water-mark) subclasses in `walrus_driver` supply per-engine throughput divisors for the perf simulator. These are cost-model cycle figures (used as `floor((NEP·mult + base)·100 / freq)`), **not MHz**. Read off `getEngineFrequency(EngineType)`:

```c
TrainiumHwm::getEngineFrequency(t):  // gen2 / Sunda
    if (1 <= t <= 3) return 140;     // PE, Pool, Act
    if (t == 5)      return 112;     // SP
Gen3Hwm::getEngineFrequency(t):      // gen3 / Cayman
    if (1 <= t <= 3) return 120;
    if (t == 5)      return 96;
CoreV4Hwm::getEngineFrequency(t):    // gen4 / CoreV4
    if (1 <= t <= 3) return 120;
    if (t == 5)      return 120;     // SP == compute on gen4
```

> **NOTE — the perf-sim `Hwm` families are a coarser 3-tier grouping.** The cost model collapses the four codegen tiers into three latency buckets; gen1 ships *no* distinct `Hwm` (the `140/112` pair belongs to `TrainiumHwm`/gen2, a frequent misattribution — `getArchModel` groups `{sunda, trainium, trn1, inf2}` onto Sunda). PE is the slower clock domain on gen2/gen3 (PE 140 vs SP 112; 120 vs 96) and equal to SP on gen4 (both 120). The codegen keeps the generations distinct (`CoreV2Gen` vs `CoreV3Gen`); do not read the shared `Hwm` family as a generation merge.

---

## What is *not* in the matrix

Three things a reimplementer might expect per-generation are structurally absent from the compiler binaries, confirmed by symbol-table search (not assumed):

- **ICI / inter-chip link count** — no `getIci`-style symbol in either binary's dynamic symbol table; the CTM model covers only on-NeuronCore TPB resources.
- **Marketed NeuronCores-per-chip** — the compiler pins `numCores` *per device* (4/2/2/2) and `numDevices` *per board* (2 everywhere), plus the in-binary truth that a logical NeuronCore spans 1–2 physical cores (`assert("LNC_SIZE <= 2")`, default `lnc_size = 1`). The full marketed per-chip core count is a public-spec figure, SPECULATIVE if treated as exact.
- **DMA queue count** — queues are allocated dynamically per-`Module` (`bir::Module::addQueue`), clamped by `optNumDMAEngines`, not a fixed per-generation immediate.

A row listing any of these with concrete per-gen numbers would be inventing them.

---

## Related Components

| Name | Relationship |
|---|---|
| `getArchModel` | the dispatch that selects which of the four `Board` trees this table tabulates |
| `<Arch>Core` ctors | the four constructor families that *are* the geometry |
| `ctm.hpp` | the shipped header that names every field the matrix grounds |
| `HBMUsage` | the one consumer that gates compilation on a geometry value (`Core::dramSizeGb`) |
| `TrainiumHwm` / `Gen3Hwm` / `CoreV4Hwm` | the independent cost-model source corroborating bank size + clocks |

## Cross-References

- [1.04 Per-Generation Hardware-Constant Matrix](../arch/hardware-constant-matrix.md) — the Part-1 source page this appendix distills; the per-row constructor evidence.
- [1.05 SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the SBUF/PSUM rows in depth, with the allocator read-chains and the half-width case.
- [1.06 DRAM / HBM Geometry](../arch/dram-hbm-geometry.md) — the per-core/per-device HBM split, the `HBMUsage` budget gate, and the `split_huge_dram_tensor` pass.
- [1.02 Codename Taxonomy](../arch/codename-taxonomy.md) — the codename ↔ ArchLevel ↔ marketed-device decode named per column here.
- [1.03 Vestigial Generations](../arch/vestigial-generations.md) — why gen1 is analysis-only and gen5 is a stub with no `Board`; the basis for the INFERRED CoreV5 column.
- [MX Matmul Legality](../numerics/mx-matmul-legality.md) — the MX / FP4 numeric contract the "MX = gen4 feature" row points to.
- [Methodology & the Confidence Model](../methodology.md) — the four-tier ladder used in the Conf column.
