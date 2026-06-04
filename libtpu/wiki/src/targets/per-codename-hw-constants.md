# Per-Codename Constant Table

> *All constant values on this page were decoded byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

This page is the consolidated per-generation hardware-constant table for every TPU codename the binary knows: Jellyfish (v2), Dragonfish (v3), Pufferfish (v4), Viperfish (v5p/v5e), Ghostlite (v6e), and `6acc60406` (v7x). It is reference-table-centric: the master table is the point of the page, and the prose around it exists only to name the source of each row and flag the confidence.

Two source classes feed the table. The first and dominant is the embedded `<codename>_chip_parts.binarypb` proto blob, decoded directly from `.rodata` (see [chip_parts.binarypb Decode](chip-parts-binarypb.md) for the schema and resolution path). Every memory size, core count, MXU geometry integer, clock, register count, and DMA constant below comes from those bytes, materialized as `bytes_per_word × word_count` or read as a scalar field. The second is the small set of constants the proto does *not* carry — the VMEM/SMEM/CMEM bank counts — which are C++ literals in the per-codename `*Target::MemBanks` overrides.

All nine blobs were carved from `.rodata`, md5-verified against their `FileWrapper` descriptor fingerprints, and walked field-by-field against the schema recovered from `protodesc_cold`. The decode reproduces, byte-for-byte, the relationships a reimplementer would expect (e.g. peak BF16 = 2 × `mxu_count` × 128² × `frequency_mhz` for the 128×128 generations), and every row carries a Confidence column with its source.

| | |
|---|---|
| **Source (capability)** | nine `*_chip_parts.binarypb` blobs, `.rodata` `0x0BDF29A0..0x0BDF3AB8` |
| **Source (bank counts)** | `*Target::MemBanks` C++ overrides (addresses below) |
| **Decode method** | md5-verified carve + field-walk against `protodesc_cold` schema |
| **Generations** | jellyfish/dragonfish/pufferfish/viperfish/ghostlite/6acc60406 (TpuVersionProto 1..6) |
| **Confidence** | CONFIRMED unless a cell is annotated otherwise |

---

## Master Hardware-Constant Table

All values are per TensorCore unless noted. "std" is the full part; "lite" is the `viperfish_lite` (v5e) or `pufferfish_lite` (v4 lite) blob, distinguished at resolution by the `variant_name` field. `6acc60406` is shown as die / full-chip (the `tensornode` blob vs the full two-die package). HBM size cells show the exact byte product; the GiB figure is `bytes / 2^30`.

| Constant | v2 Jellyfish | v3 Dragonfish | v4 Pufferfish (std / lite) | v5p/v5e Viperfish (std / lite) | v6e Ghostlite | v7x 6acc60406 (die / full) | Source · Confidence |
|---|---|---|---|---|---|---|---|
| TpuVersionProto | 1 | 2 | 3 | 4 | 5 | 6 | proto f1 · CONFIRMED |
| `driver_abi_version` | 1 | 1 | 1 | 1 | 1 | 1 | proto f9 · CONFIRMED |
| **HBM size** | 16 GiB | 32 GiB | 32 GiB / 8 GiB | 96 GiB / 16 GiB | 31.5 GiB | 95 GiB / 190 GiB | `SharedMemory[HBM]` bpw×wc · CONFIRMED |
| HBM stacks × per-stack | 2 × 8 GiB | 2 × 16 GiB | 1 × 32 GiB / 1 × 8 GiB | 1 × 96 GiB / 1 × 16 GiB | 1 × 31.5 GiB | 1 / 2 × 95 GiB | `SharedMemory.count` · CONFIRMED |
| HBM word (`bytes_per_word`) | 1024 B | 1024 B | 512 B | 32 B / 512 B | 32 B | 32 B | proto f3 · CONFIRMED |
| HBM clock | 1400 MHz | 1800 MHz | 2400 MHz | 3600 / 3200 MHz | 6400 MHz | 7200 MHz | proto f5 · CONFIRMED |
| HBM bandwidth / stack | 0.317 TB/s | 0.430 TB/s | 0.982 / 0.492 TB/s | 2.350 / 0.738 TB/s | 1.638 TB/s | 3.686 TB/s | `bytes_per_second` · CONFIRMED |
| **VMEM / TensorCore** | 16 MiB | 16 MiB | 16 MiB | 64 / 128 MiB | 128 MiB | 64 MiB | `Memory[VMEM]` bpw×wc · CONFIRMED |
| VMEM word | 512 B | 512 B | 512 B | 512 B | 512 B | 512 B | proto f5 · CONFIRMED |
| **SMEM (TensorCore)** | 16 KiB | 16 KiB | 1 MiB | 1 MiB | 1 MiB | 1 MiB | `Memory[SMEM]` bpw×wc · CONFIRMED |
| SMEM word | 4 B | 4 B | 4 B | 4 B | 4 B | 4 B | proto f5 · CONFIRMED |
| **SFLAG (TensorCore)** | 1 KiB | 1 KiB | 2 KiB | 2 KiB | 2 KiB | 16 KiB | `Memory[SFLAG]` bpw×wc · CONFIRMED |
| SFLAG word | 4 B | 4 B | 4 B | 4 B | 4 B | 4 B | proto f5 · CONFIRMED |
| **CMEM (SharedMemory)** | absent | absent | 128 MiB / 128 MiB | absent | absent | absent | `SharedMemory[CMEM]` · CONFIRMED |
| CMEM word / clock / bw | — | — | 512 B / 1050 MHz / 2.151 TB/s | — | — | — | proto · CONFIRMED |
| **MXU lane × sublane** | 128 × 8 | 128 × 8 | 128 × 8 | 128 × 8 | 128 × 8 | 128 × 8 | `VectorIsa` f2/f3 · CONFIRMED |
| **MXU count / TensorCore** | 1 | 2 | 4 | 4 | 2 | 2 | `VectorIsa.mxu_count` f5 · CONFIRMED |
| XLU count / TensorCore | 1 | 1 | 2 | 3 | 2 | 2 | `VectorIsa.xlu_count` f6 · CONFIRMED |
| IAR count / TensorCore | 2 | 2 | 2 | 2 | 2 | 2 | `VectorIsa.iar_count` f7 · CONFIRMED |
| MXU systolic dim | 128×128 | 128×128 | 128×128 | 128×128 | 256×256 | 256×256 | `*Target` C++ override · CONFIRMED |
| **TensorCore freq** | 700 MHz | 940 MHz | 1050 MHz | 1750 / 1500 MHz | 1750 MHz | 1900 MHz | `Core[TC].frequency_mhz` f5 · CONFIRMED |
| TensorCores / chip (std/lite) | 2 | 2 | 2 / 1 | 2 / 1 | 1 | 1 / 2 | `Core[TC].count` f3 · CONFIRMED |
| **Reg file SREG/VREG/PREG/VMREG** | 32/32/15/8 | 32/32/15/8 | 32/32/15/8 | 32/64/14/16 | 32/64/14/16 | 32/64/14/16 | `Register.count` · CONFIRMED |
| **Accelerator core type** | BARNA_CORE | BARNA_CORE | BARNA_CORE | SPARSE_CORE | SPARSE_CORE | SPARSE_CORE | `Core.type` f1 · CONFIRMED |
| accelerator count / chip (std/lite) | 2 | 2 | 4 / 0 | 4 / 0 | 2 | 2 / 4 | `Core.count` f3 · CONFIRMED |
| SparseCore freq | — | — | — | 1475 MHz | 1350 MHz | 1750 MHz | `Core[SC].frequency_mhz` · CONFIRMED |
| SC sequencers (SEQ/TAC/TEC) | — | — | — | 1 / 16 / 16 | 1 / 16 / 16 | 1 / 0 / 16 | `Sequencer.count` · CONFIRMED |
| SC TEC VectorIsa lane × sublane | — | — | — | 8 × 1 | 8 × 1 | 16 × 1 | `VectorIsa` (SC_TEC) · CONFIRMED |
| SC SPMEM / TILESPMEM | — | — | — | 8 MiB / 512 KiB | 4 MiB / 256 KiB | 8 MiB / 512 KiB | `Memory` bpw×wc · CONFIRMED |
| SC SMEM (SCS) / SFLAG (SCS) | — | — | — | 64 KiB / 28 KiB | 64 KiB / 28 KiB | 64 KiB / 28 KiB | `Memory` bpw×wc · CONFIRMED |
| SC `tile_hbm_bw` / `stream_granule` | — | — | — | 32 B/cyc / 4 B | 32 B/cyc / 4 B | 64 B/cyc / 4 B | `SparseCore` f3/f4 · CONFIRMED |
| **DMA granule bytes** | 1024 B | 1024 B | 512 B | 32 B / 512 B | 32 B | 32 B | `DmaRequirements.granule_bytes` f3 · CONFIRMED |
| DMA host / device align | 16 / 1024 | 16 / 1024 | 32 / 512 | 32 / 32 (lite 32 / 512) | 32 / 32 | 32 / 32 | `DmaRequirements` f1/f2 · CONFIRMED |
| `sync_flag_granule` | 1024 B | 1024 B | 512 B | 32 B | 32 B | 32 B | `DmaRequirements` f4 · CONFIRMED |
| `max_single_host_dma` | 8 MiB | 16 MiB | 2 GiB | 128 GiB | 64 GiB | 32 GiB | `DmaRequirements` f5 · CONFIRMED |
| misc: extra_done / host_async / count_dones | n/n/n | n/n/n | y/n/n | y/y/y (lite y/y/n) | y/y/y | y/y/y | `MiscPropertiesProto` · CONFIRMED |

The exact byte products behind the headline HBM and VMEM cells: Jellyfish HBM `1024 × 8,388,608 = 8,589,934,592 B` per stack × 2; Pufferfish HBM `512 × 67,108,864 = 34,359,738,368 B` (32 GiB) + CMEM `512 × 262,144 = 134,217,728 B` (128 MiB); Viperfish HBM `32 × 3,221,225,472 = 103,079,215,104 B` (exactly 96 GiB); Ghostlite HBM `32 × 1,056,964,608 = 33,822,867,456 B` (31.5 GiB, 32 GiB nominal less ECC); 6acc60406 HBM `32 × 3,187,671,040 = 102,005,473,280 B` (95 GiB per die).

### Bank counts (not a proto field — `*Target::MemBanks` C++ literals)

| MemBanks(space) | v2 JF | v3 DF | v4 PF | v5p VF | v6e GL | v7x | Confidence |
|---|---|---|---|---|---|---|---|
| VMEM (space 3) | 8 | 8 | 16 | 32 | 32 | 32 | CONFIRMED |
| CMEM (space 4) | FATAL | FATAL | 32 | FATAL | FATAL | FATAL | CONFIRMED |
| SMEM (space 5) | 2 | 2 | 8 | 8 | 8 | 8 | CONFIRMED |

`JellyfishTarget::MemBanks` @ `0x1d48fc80` returns 8 for space 3, 2 for space 5, and `LOG(FATAL)` otherwise (`target_jellyfish.h:215`). `PufferfishTarget::MemBanks` @ `0x1d493900` indexes the table at `.rodata` `0xb5305c8 = {16, 32, 8}` for spaces 3/4/5 (`target_pufferfish.h:228`). `ViperfishTarget::MemBanks` @ `0x1d4999c0` and `GhostliteTarget::MemBanks` @ `0x1d4969c0` return 32 / 8 / FATAL. Dragonfish overrides none of these and inherits Jellyfish's 8 / 2.

> **Note:** Pufferfish is the only generation whose `MemBanks` ladder has a CMEM (space 4) entry, and it is the only generation whose `chip_parts` has a `SharedMemory[CMEM]` (128 MiB). Every other generation `LOG(FATAL)`s on the CMEM space *and* has no CMEM shared memory — two independent encodings of "CMEM is first-class only on v4." See [Memory Hierarchy](memory-hierarchy.md).

---

## Reading the Table

### The BarnaCore → SparseCore pivot

The accelerator-core row is the generational hinge. v2/v3/v4 carry `BARNA_CORE` cores (the pre-SparseCore embedding/dedup engine); v5p/v6e/v7 carry `SPARSE_CORE` cores with `SC_SEQ` + (16× `SC_TAC` on v5p/v6e only) + 16× `SC_TEC` sequencers, plus the SPMEM/TILESPMEM/TEC memory family. `6acc60406` drops the separate `SC_TAC` sequencer (its SparseCore has `SC_SEQ` + `SC_TEC×16` only) and widens the `SC_TEC` VectorIsa lane from 8 to 16. The lite parts (pufferfish_lite, viperfish_lite) carry *neither* BarnaCore nor SparseCore — they are TensorCore-only single-core dies.

The BarnaCore sequencer composition is not uniform across v2/v3/v4: Jellyfish's BarnaCore carries `BC_ADDR ×16` *only* (no `BC_SEQ` entry in its blob), while Dragonfish and Pufferfish add a `BC_SEQ ×1` master sequencer alongside the 16 `BC_ADDR` handlers. A reimplementer enumerating Jellyfish BarnaCore sequencers must not assume a `BC_SEQ` that is absent from the v2 proto.

### The register-file widening at v5p

v2/v3/v4 TensorCore sequencers report SREG 32, VREG 32, PREG 15, VMREG 8. From Viperfish (v5p) onward the file is SREG 32, VREG 64, PREG 14, VMREG 16 — VREG doubled, VMREG doubled, PREG dropped by one. This is a clean proto-visible discontinuity at the v4→v5p boundary that a reimplementer must respect when allocating the per-generation register sets.

### MXU count vs systolic dimension

`mxu_count` rises 1→2→4→4 across v2..v5p, then *drops* to 2 for v6e/v7. The drop is compensated by the systolic-array dimension: v6e/v7 use 2 × 256×256 arrays (the `GhostliteTarget` C++ override `MxuContractingSize`/`MxuNoncontractingSize` = 256, byte-confirmed at `0x1d497840`/`0x1d497860`; base `Target` returns 128), where v2..v5p use up-to-4 × 128×128. The 256 dimension is the one MXU geometry value that is a C++ literal, not a proto field — the proto carries only `lane_count=128` and `mxu_count` — but the literal itself is byte-exact, so the systolic-dim row is CONFIRMED, flagged as a C++-override source rather than a proto field. The 128×128 generations cross-validate: peak BF16 = `2 × mxu_count × 128² × frequency_mhz` reproduces the published per-chip FLOPS for v2 (22.9 T at 1 MXU × 700 MHz), v3 (61.6 T at 2 × 940), and v4 (137.6 T at 4 × 1050) to within 1%.

> **Note:** the proto's `sublane_count` is 8 for *every* generation, including v4 — the `chip_parts` `VectorIsa.sublane_count` field is unambiguously 8, not 16. The tile dimension a tiling pass consumes is `Tile(SublaneCount, LaneCount) = (8, 128)` on every gen in this build (the `Target::SublaneCount` accessor reads exactly this proto value). A reimplementation that hardcodes a 16-sublane v4 tile diverges from the loaded geometry.

---

## Related Components

| Name | Relationship |
|---|---|
| `TpuChipParts::FromProto` | parses each blob; the proto fields above become `Target` capability fields |
| `*Target::MemBanks` | the C++ source of the bank-count rows (the only non-proto integers here) |
| `TpuChipConfig::Create` | parallel resolver for the *mode* configs; not the source of any row on this page |

## Cross-References

- [chip_parts.binarypb Decode](chip-parts-binarypb.md) — the proto schema, blob locations, and resolution path these constants are decoded from
- [TpuChipConfig](tpu-chip-config.md) — how these constants assemble into the runtime `Target` config and who reads it
- [Codename Matrix](tpu-version-codename-matrix.md) — TpuVersion ↔ codename ↔ marketing-name mapping
- [Per-Gen Comparison Matrix](../appendix/per-gen-comparison-matrix.md) — cross-page consolidated per-generation comparison
- [Memory Hierarchy](memory-hierarchy.md) — the HBM/VMEM/SMEM/SFLAG/CMEM tier model these sizes populate
- [Cost Model Overview](../cost/overview.md) — consumer of the frequency and bandwidth rows
- [ISA Overview](../isa/overview.md) — consumer of the MXU lane/sublane and register-file rows
