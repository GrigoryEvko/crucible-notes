# Per-Generation Hardware Geometry

> **Runtime lib:** `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` · **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64, unstripped, DWARF present; `.text`/`.rodata` VMA == file offset).
> **Kernel:** `aws-neuronx-dkms 2.27.4.0` (unstripped GPL-2.0 source; cited `file:line`).
> **Status:** Reimplementation-grade · **Evidence grade:** disasm/byte/offset/`file:line`-anchored · **Part I — Silicon & Architecture Model**, DEEP · [back to overview](overview.md)

## Abstract

Every accelerator runtime carries a hard-coded model of the chip it drives — how many cores, how many DMA engines, how big the SRAM and DRAM planes are — because *all* of its register-addressing math is derived from those constants. In the Neuron stack this model is not a config file or a probe: it is **a wall of single-instruction leaf functions** in `libnrt.so`, one per (property, generation) pair, each compiling down to a literal `mov $N, %eax; ret`. The arch dispatcher installs the generation's leaves into the `tdrv_arch_ops_1` vtable once (`tdrv_arch_ops_init` @`0x308e80`, [overview](overview.md#dispatch-detect-once-serve-forever)); thereafter every `tdrv_arch_get_*` getter dispatches through the installed slot and returns the baked-in number. This page is the **complete, byte-verified geometry table** for the three generations the runtime knows: **sunda** (V2 / Trn1+Inf2), **cayman** (V3 / Trn2), **mariana** (V4 / Trn3).

The geometry leaves live in three contiguous `.text` bands, one per arch: the *generic-getter constant variants* at `0x30af10..0x30afff` (sunda) / `0x30c680..0x30c6af` (cayman) / `0x30d7a0..0x30d7cf` (mariana), plus the register-offset-math leaves documented in the per-arch device cells. The [overview](overview.md#per-generation-geometry) byte-verified the headline `num_tpb` (2/8/8) at `0x30b540`/`0x30ba40`/`0x30cb70`; this page extends that to every dimension — sequencer count, DMA-engine count, queues-per-engine, TopSP count, HBM channels, the SBUF/PSUM region geometry — and pins **each cell to its own arch's returning symbol and address**, not a shared anchor. Where a number is not a runtime constant (DMA-engine totals, HBM stack sizes, SBUF dims) it is pinned to the kernel's `address_map.h` macro that the data plane uses.

The shape splits cleanly. **sunda is the small chip**: 2 NeuronCores, 1 sequencer engine *per core* sense (`num_seng=2`, `tpb_per_seng=1`), 1 TPB-per-HBM, 6 TopSP, 34 DMA engines, 2 HBM channels. **cayman and mariana share the big shape**: 8 NeuronCores = 4 SENG × 2 TPB, 2 TPB-per-HBM, 16 TopSP, 132 DMA engines, 4 HBM channels — differing in exactly *one* hardware value, the HBM stack size (24 GB → 36 GB), plus a software NeuronCore renumber (the die-flip). The rest of this page is that table, the getter map behind it, and the two callouts a reimplementer trips on: the V4 HBM bump and the die-flip XOR.

For reimplementation, the contract this page fixes is:

- **The geometry dimension table** — the exact value of every per-generation HW constant, each anchored to the leaf function (symbol + address, with the disassembled `mov $N` byte) or the kernel macro that produces it.
- **The getter dispatch shape** — that these are constant leaves reached *only* through the installed vtable slot, so a reimplementer must install before querying, and the lazy-init path that catches a too-early call.
- **The derived addressing rules** — `hbm_index = tpb_idx/2` (cayman/mariana) vs identity (sunda), the `>>1` seng-pair / `>>2` quadrant index decomposition, and the DMA-engine numbering split (dense `[0..N)` SENG engines vs the special off-TPB H2D engines).
- **The two version-specific deltas** — the V4 HBM 24→36 GB bump (the *sole* HW value change cayman→mariana) and the die-flip NeuronCore remap (XOR `0x6`), with the die/mesh neighbor topology.

## At a glance

| | |
|---|---|
| **Geometry source** | per-arch constant leaves, `tdrv/{sunda,cayman}/tdrv_arch_*.c` + `tdrv/encd/archs/mariana.c` |
| **Constant-getter bands** | sunda `0x30af10`–`0x30afff` · cayman `0x30c680`–`0x30c6af` + `0x30ba10`–`0x30bb03` · mariana `0x30d7a0`–`0x30d7cf` + `0x30cbd0`–`0x30d416` |
| **Headline `num_tpb`** | sunda **2** @`0x30b540` · cayman **8** @`0x30ba40` · mariana **8** @`0x30cb70` (byte-verified `mov $N,%eax;ret`) |
| **Dispatch** | constant leaf → installed `tdrv_arch_ops_1` slot → generic `tdrv_arch_get_*` (zero direct callers; reached only via vtable) |
| **Kernel geometry macros** | `v2/address_map.h` (Trn1/Inf2), `v3/address_map.h` (Trn2), `v4/` deltas (Trn3) — DKMS 2.27.4.0 |
| **DMA-engine totals** | V2 **34** · V3/V4 **132** (`V{2,3}_NUM_DMA_ENG_PER_DEVICE`) |
| **HBM active size / stack** | V2 16 GB · V3 **24 GB** (`0x600000000`) · V4 **36 GB** (`0x900000000`) |
| **NeuronCore remap (die-flip)** | software XOR `0x6` on logical→physical NC index (`neuron_nc_map_die_flip_mask`, `v3:1539`) |

---

## The geometry dimension table

This is the core artifact of the page. Rows are geometry **properties**; columns are the three **generations**. Every cell carries the source constant — the runtime leaf that *returns* it (symbol + address, with the verified return value) or the kernel `address_map.h` macro that the data plane derives from. All runtime values were read from the disassembled leaf body (`mov $N,%eax; ret`); all kernel values are `file:line`-cited verbatim.

| Property | sunda (V2 / Trn1+Inf2) | cayman (V3 / Trn2) | mariana (V4 / Trn3) | Source anchor (per cell) |
|---|---|---|---|---|
| **NeuronCores / device** (`num_tpb`) | **2** | **8** | **8** | `tdrv_arch_get_num_tpb_{sunda@0x30b540, cayman@0x30ba40, mariana@0x30cb70}` — `mov $0x2/$0x8/$0x8` |
| **Sequencer engines** (`num_seng`) | **2** | **4** | **4** | `tdrv_arch_get_num_seng_{sunda@0x30af20=2, cayman@0x30ba20=4, mariana@0x30cb50=4}` |
| **TPB per SENG** (`num_tpb_per_seng`) | **1** | **2** | **2** | `tdrv_arch_get_num_tpb_per_seng_{sunda@0x30af30=1, cayman@0x30ba30=2, mariana@0x30cb60=2}` |
| **DMA engines / TPB** (`num_dma_per_tpb`) | **16** | **16** | **16** | `tdrv_arch_get_num_dma_per_tpb_{sunda@0x30af10, cayman@0x30ba10, mariana@0x30cb40}` — `mov $0x10` |
| **DMA queues / engine** | **16** | **16** | **16** | `tdrv_arch_get_num_dma_queues_per_engine_{sunda@0x30b530, cayman@0x30c680, mariana@0x30d7a0}` — `mov $0x10` |
| **Total DMA engines / device** | **34** | **132** | **132** | `V2_NUM_DMA_ENG_PER_DEVICE`=34 (32 SENG + D2H@32 + H2D@33); `V3_NUM_DMA_ENG_PER_DEVICE`=132 (128 SENG + 4 H2D/D2H @128–131, `address_map.h`) |
| **TopSP / device** (`num_topsp`) | **6** | **16** | **16** | `tdrv_arch_get_num_topsp_{sunda@0x30af40=6, cayman@0x30c690=16, mariana@0x30d7b0=16}` — `mov $0x6/$0x10/$0x10` |
| **TPB per HBM** (`num_tpb_per_hbm`) | **1** | **2** | **2** | `tdrv_arch_get_num_tpb_per_hbm_{sunda@0x30b550=1, cayman@0x30c6a0=2, mariana@0x30d7c0=2}` |
| **HBM index of TPB** (`default_hbm_index`) | `idx` (identity) | `idx/2` | `idx/2` | `tdrv_arch_get_default_hbm_index_{sunda@0x30af90 `mov %edi`, cayman@0x30ba90, mariana@0x30cbc0}` (signed `>>1`) |
| **HBM channels / device** | **2** | **4** | **4** | `V2_MAX_DRAM_CHANNELS`=2; `V3_MAX_DRAM_CHANNELS`=4 (`mpset_set_dram_and_mpset_info_v3`, `v3:641`) |
| **HBM active size / stack** | 16 GB (`0x400000000`) | **24 GB** (`0x600000000`) | **36 GB** (`0x900000000`) | `V2_HBM_{0,1}_SIZE`; `V3_HBM_ACTIVE_SIZE`; `V4_HBM_ACTIVE_SIZE` (`v3:114`, `v4:201`) |
| **HBM per-stack window** (`hbm_dist`) | — | **64 GB** (`0x1000000000`) | 64 GB | `V3_HBM_SIZE` = `V4_HBM_SIZE` = `0x1000000000` (`mmap_get_bar4_offset_v3`, `v3:966`) |
| **SBUF (state buffer) / NC** | 32 MiB (`0x2000000`) | 32 MiB | 32 MiB | `V2_SBUF_SIZE`=`0x2000000` (`v2/address_map.h:26`); per-TPB BAR0 SBUF region `dm_mmap_special_*` TPB-SBUF row |
| **PSUM region / NC** | TPB+`0x2000000` | 32 MiB (`0x2000000`) | 32 MiB | `bar0_tpb_psum_offsets[i]` = `tpb[i]+0x2000000`, size `0x2000000` (`csr_register_device_{cayman,mariana}`) |
| **H2D off-TPB DMA engine ids** | {32, 33} (D2H/H2D) | {128,129,130,131} | {128,129,130,131} | `V2_{D2H,H2D}_IDX`=32/33; `{cayman,mariana}_h2d_dma_eng_id` @`0x9deb20`/`0x9df200` = `{128,129,130,131,0,0,0,0}` |
| **Q7-POOL core-type id** | **6** | **13** | **21** | `tdrv_arch_get_q7_pool_core_type_{sunda@0x30af70=6, cayman@0x30ba70=0xd, mariana@0x30cba0=0x15}` |
| **nx-core-type LUT** | `CSWTCH_16` @`0x9de3e0` | `CSWTCH_16_0` @`0x9de900` = `{10,7,9,8,11}` | `CSWTCH_16_1` @`0x9defe0` | `tdrv_arch_get_nx_core_type_{sunda@0x30af50, cayman@0x30ba50, mariana@0x30cb80}` (`[eng]`, eng>SP→−1) |
| **Die / socket count** | 1 die | 1 (2 SENG/die nominal) | **2 die** (mesh; flip-eligible) | sunda `address_map.h` "1 die"; mariana 4-quadrant mesh PA scheme, die-flip XOR `0x6` (`v3:1541`) |

### Reading the table

A handful of derivations a reimplementer needs to internalize, because the rest of the runtime's addressing math assumes them:

- **sunda is the outlier.** 2 NeuronCores, **1 TPB per HBM** (`num_tpb_per_hbm_sunda`=1, so `default_hbm_index_sunda` is the *identity* `mov %edi, %eax` — the HBM index *is* the TPB index), and only **6 TopSP** vs 16 on the big chips. Its DMA-engine total is **34** (32 per-NC SENG engines + the two top-level D2H/H2D at indices 32/33), not 132.
- **cayman and mariana are the same shape.** 8 NeuronCores = `num_seng`(4) × `num_tpb_per_seng`(2). `default_hbm_index` is signed `idx/2` (the `shr $31; add; sar $1` idiom in the disasm — two TPB share one HBM). 16 TopSP, 132 DMA engines (128 SENG + 4 off-TPB H2D/D2H @128–131). The only HW *value* that differs between them is the HBM stack size; everything else mariana inherits.
- **`num_dma_per_tpb` (16) is per-NeuronCore; the device total is `num_tpb × 16 + N_top`.** sunda: 2×16 + 2 = 34. cayman/mariana: 8×16 + 4 = 132. The "+N_top" engines are the off-TPB host-DMA engines, numbered *out of band* (32/33 on V2; 128–131 on V3/V4) — see the H2D quirk below.
- **The TPB index decomposes into address fields.** `device_tpb_idx` (the only field of `physical_core_t` the geometry leaves read, at `+0x04`) is sliced `>>1` for the SENG-pair, `>>2` for the PCIe/quadrant half, `&1` for parity, `/num_tpb_per_seng` for the H2D-engine bucket. This is why the leaf bodies guard `device_tpb_idx < tdrv_arch_get_num_tpb()` before *every* offset computation (a `__assert_fail` on violation — `"pcore->device_tpb_idx < tdrv_arch_get_num_tpb()"`).
- **SBUF/PSUM are per-NeuronCore BAR0 regions, not geometry constants.** The 32 MiB state buffer (`V2_SBUF_SIZE 0x2000000`) and the PSUM region (TPB base + `0x2000000`, same 32 MiB) are registered as BAR0 sub-windows by `csr_register_device_*`, one TPB / TPB-PSUM pair per NeuronCore — there is no `get_sbuf_size` leaf, so the value is pinned to the kernel macro and the `bar0_tpb*_offsets` stride.

> **NOTE —** the runtime never exposes a single "engines per device" leaf; the count is implicit in `num_tpb × num_dma_per_tpb` plus the architecture's fixed top-engine set. The kernel makes it explicit (`V{2,3}_NUM_DMA_ENG_PER_DEVICE` = 34/132) and the two layers agree. A reimplementer building the engine table from `num_dma_per_tpb` alone will be short the 2 (V2) or 4 (V3/V4) off-TPB host engines.

---

## Function map — the geometry getters

Each property in the table is one constant leaf per generation, installed into a `tdrv_arch_ops_1` slot by `tdrv_arch_register_{sunda,cayman,mariana}` and dispatched through the matching generic getter (e.g. `tdrv_arch_get_num_tpb` @`0x309050`). The bodies are 6 bytes (`mov $imm32,%eax; ret`) except the two arithmetic ones noted. Confidence is **CERTAIN** for the constant leaves (the immediate is in the instruction; cross-verified by `nm` + disasm).

| Getter (generic) | sunda | cayman | mariana | Return / form | Confidence |
|---|---|---|---|---|---|
| `num_tpb` | `0x30b540`=2 | `0x30ba40`=8 | `0x30cb70`=8 | `mov $N` const | CERTAIN |
| `num_seng` | `0x30af20`=2 | `0x30ba20`=4 | `0x30cb50`=4 | `mov $N` const | CERTAIN |
| `num_tpb_per_seng` | `0x30af30`=1 | `0x30ba30`=2 | `0x30cb60`=2 | `mov $N` const | CERTAIN |
| `num_dma_per_tpb` | `0x30af10`=16 | `0x30ba10`=16 | `0x30cb40`=16 | `mov $0x10` const | CERTAIN |
| `num_dma_queues_per_engine` | `0x30b530`=16 | `0x30c680`=16 | `0x30d7a0`=16 | `mov $0x10` const | CERTAIN |
| `num_topsp` | `0x30af40`=6 | `0x30c690`=16 | `0x30d7b0`=16 | `mov $N` const | CERTAIN |
| `num_tpb_per_hbm` | `0x30b550`=1 | `0x30c6a0`=2 | `0x30d7c0`=2 | `mov $N` const | CERTAIN |
| `default_hbm_index` | `0x30af90` | `0x30ba90` | `0x30cbc0` | sunda `idx`; cayman/mariana signed `idx/2` | CERTAIN |
| `q7_pool_core_type` | `0x30af70`=6 | `0x30ba70`=13 | `0x30cba0`=21 | `mov $N` const | CERTAIN |
| `get_nx_core_type` | `0x30af50` | `0x30ba50` | `0x30cb80` | `eng>SP?−1:CSWTCH_16*[eng]` | HIGH |
| `get_ptpbs_from_hbm_index` | `_sunda` | `0x30baa0` | `0x30cbd0` | `{2·hbm, 2·hbm+1}`, count=2 | HIGH |
| `get_h2d_dma_engine_id` | `_sunda` | `0x30c1c0` | `0x30d2e0` | `h2d_dma_eng_id[idx/num_tpb_per_seng]` | HIGH |
| `is_h2d_engine` | `_sunda` | `0x30bbb0` | `0x30ccd0` | `(eng−128)<=3` (V3/V4) | HIGH |

> **QUIRK —** these leaves have **zero direct callers** in the callgraph. They are reached *only* through the function-pointer slot that `tdrv_arch_register_*` installs into `tdrv_arch_ops_1`. A reimplementer who calls `num_tpb` semantics before `tdrv_arch_ops_init` has run will hit the generic getter's lazy-init path (`tdrv_arch_get_num_tpb` @`0x309050` lazily triggers `ops_init`), not a fixed constant — install must precede query. This is the same indirection the [overview](overview.md#dispatch-detect-once-serve-forever) documents; it is why the constants are unobservable by tracing call sites and must be read from the leaf bodies.

### The nx-core-type LUT row

`get_nx_core_type` is the one geometry getter that is a table lookup rather than a constant: it maps the hardware engine index `al_hal_tpb_eng_type` (PE=0, ACT=1, POOL=2, DVE=3, SP=4) to the microcode core-type `nrtucode_coretype_t`. Each arch has its own LUT, addressed by a PC-relative `lea` in the leaf:

```c
// tdrv_arch_get_nx_core_type_{sunda,cayman,mariana} @0x30af50 / 0x30ba50 / 0x30cb80
int get_nx_core_type(al_hal_tpb_eng_type eng):
    if (unsigned)eng > SP(4): return -1            // cmp $4,%edi; ja → mov $-1
    return CSWTCH_16_n[eng]                          // mov (lut,%rdi,4),%eax
    //  sunda   → CSWTCH_16   @0x9de3e0   (gen block base 0)
    //  cayman  → CSWTCH_16_0 @0x9de900 = {10,7,9,8,11}   (gen block base 7; byte-decoded)
    //  mariana → CSWTCH_16_1 @0x9defe0   (gen block base 15)
```

Only the cayman LUT is byte-decoded here (`{10,7,9,8,11}` — the declaration-order remap of `{PE,ACT,POOL,DVE,SP}`). The sunda/mariana LUT *addresses* are pinned from the leaf's `lea` displacement (`0x30af5e + 0x6d3482 = 0x9de3e0`; `0x30cb8e + 0x6d2452 = 0x9defe0`), but their contents and the full per-generation declaration-order permutation are the subject of [Coretype Numbering Reconciliation](coretype-numbering.md). **(MEDIUM** on the exact sunda/mariana LUT bytes — addresses certain, values not re-decoded on this page.**)**

---

## QUIRK — the V4 HBM 24 → 36 GB bump

mariana (V4 / Trn3) differs from cayman (V3 / Trn2) in exactly **one hardware value**: the HBM stack size. Everything else V4 inherits from the V3 vtable — `neuron_dhal.c` registers the full V3 op table and then patches only 8 slots, and of those eight, the only one carrying a *data-plane* value change is the HBM size.

```c
// The single HW delta, threaded through 4 inherited-but-overridden slots:
V3_HBM_ACTIVE_SIZE = 0x600000000   // 24 GiB   (cayman)
V4_HBM_ACTIVE_SIZE = 0x900000000   // 36 GiB   (mariana)
// HBM_0..3 BASE addresses are UNCHANGED; only the active size grows.
// hbm_dist (per-stack window) stays V{3,4}_HBM_SIZE = 0x1000000000 (64 GiB) either way.
```

> **QUIRK —** the 24→36 GB bump is **byte-for-byte the only HW value** that changes between Trn2 and Trn3 in the DHAL. `mpset_set_dram_and_mpset_info_v4` (`v4:201`) is identical to its V3 form except every `V3_HBM_*`→`V4_HBM_*` macro; `mmap_get_bar4_offset_v4` (`v4:251`), `dm_mmap_special_v4[]` (`v4:28`), and `ncdev_mem_regions_v4[]` (`v4:91`) are the same logic with HBM rows sized 36 GB so the 24–36 GB band of each stack now validates. The other 7 V4 overrides are device-identity / platform-naming (SKU strings, routing-id, `npe_neighbor_eng_ids`, sysfs suffixes) — **not** geometry. A reimplementer who treats "V4" as a new arch and re-derives the whole geometry table will duplicate V3 exactly except this one constant. Build V4 as *V3-base + the HBM size patch*, not from scratch.

A second, non-geometry V4 delta worth flagging because it touches a "geometry-adjacent" capability: V4 **hardcodes** `supports_hbm_7200 = 0` (`perf_update_hbm_7200_supported_v4`, `v4:419`) — it never issues the `fw_io_get_available_profiles` probe that V3 uses (`v3:1729`). The Trn3 HBM-7200 perf profile is simply not advertised. This is a perf-profile capability, not a geometry number, but it shares the HBM lineage.

---

## QUIRK — the die-flip NeuronCore remap (XOR 0x6)

On the 8-core chips, the runtime/kernel can present the 8 NeuronCores in a **flipped** physical order depending on which die faces "up" in an UltraServer node. This is *not* a register sequence and *not* part of reset — it is a pure software renumbering of the logical→physical NeuronCore map, applied at the cdev nc-map ioctl boundary.

```c
// ncdev_logical_to_physical_nc_map_v3 (v3:1560) — the renumber
mapping = nc_mapping_v0_seng_swap[128]          // base seng-swap table
for entry_idx in 0..min(max_entries,128):
    core_idx = entry_idx
    if ndhal_die_flipped():                      // v3:1541
        core_idx ^= 0x6                          // neuron_nc_map_die_flip_mask, v3:1539
        //   XOR 0x6 toggles index bits {1,2} → swaps the NC quad ordering
        //   0->6, 1->7, 2->4, 3->5, 4->2, 5->3, 6->0, 7->1
    map->mappings[entry_idx] = mapping[core_idx]

// ndhal_die_flipped() (v3:1541) is true iff:
//   module_param force_die_flip, OR
//   platform==ULTRASERVER && pod_state==ULTRASERVER && node_id in {1,3}
```

> **QUIRK —** "die flip" is a **logical→physical index permutation, not a hardware mode**. There is no MMIO write. For an UltraServer whose `node_id` is 1 or 3 (the flipped half of a 2-node P2P pod), each logical NeuronCore index is XOR-ed with `0x6` before indexing the base seng-swap table, so both dies' cores present in a single consistent global order regardless of which physical die is "up". A reimplementer who hard-codes the un-flipped map will mis-place every model on the odd nodes of an UltraServer.

The V4 path diverges here: on **PDS** platforms V4 uses a *static, pre-baked* swap table `nc_mapping_v0_seng_swap_pds[128]` with **no runtime XOR** (`ncdev_logical_to_physical_nc_map_v4`, `v4:397`) — the flip is baked into the per-device table content rather than applied dynamically (e.g. ND1 row: V3 base `{2,3,0,1,4,5,6,7}` vs V4-PDS `{6,7,4,5,0,1,2,3}`, the XOR-0x6 image). On non-PDS V4, the V3 dynamic-XOR map is inherited unchanged.

### Die / mesh neighbor topology

The 8-core mesh is `2 die × 2 SENG/die × 2 NC/SENG`. The pod-election layer discovers inter-die neighbors by a fixed pair of engine-ID tables, and Trn3 re-laid-out those inter-die link engines — the *only* topology number that moves cayman→mariana besides HBM:

```text
npe_neighbor_eng_ids        V3 / cayman              V4 / mariana
──────────────────────────────────────────────────────────────────
  Left  link  (L)           {36, 68}                 {40, 72}
  Right link  (R)           { 4, 100}                { 8, 104}
  source                    v3:214                   v4:132 (npe_neighbor_eng_ids_v4)
```

```c
// SDMA base 4-quadrant mesh scheme (mariana_sdma_base @0x9df220, 8x16 u64)
//   tpb0: 0x1002000000     tpb2: 0x5004000000     tpb4: 0x801002000000   tpb6: 0x805004000000
//   tpb1: 0x1003000000     tpb3: 0x5005000000     tpb5: 0x801003000000   tpb7: 0x805005000000
//   high bits 0x80.. select the FAR socket/die quadrant; idx>>1 picks the SENG-pair,
//   the in-pair parity (..0x2.. even / ..0x3.. odd) picks the TPB within the pair.
```

The `0x80...` high bits in the apb_se / SDMA bases (`mariana_apb_se_bases @0x9df1e0 = {0x10..,0x50..,0x80100..,0x80500..}`) are the mesh/socket routing — the two dies live in the low (`0x10/0x50`) and high (`0x80100/0x80500`) address quadrants. The full BAR0/SDMA atlas is owned by [Memory Hierarchy](memory-hierarchy.md); only the geometry-relevant quadrant split is shown here.

---

## Cross-References

- [Silicon & Architecture Model — Runtime Lens](overview.md) — the two-vocabulary generation model, the `tdrv_arch_ops` vtable, the dispatch shape this page's leaves live in (do not re-derive the high-level framing here)
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](generations-enum.md) — the `neuron_arch` / `al_hal_tpb_arch_type` enum derivation and the emulation/QEMU sizing gate that overrides the HBM constants
- [Coretype Numbering Reconciliation](coretype-numbering.md) — the full `al_hal_tpb_eng_type` ↔ `nrtucode_coretype_t` LUT decode (sunda `CSWTCH_16`, mariana `CSWTCH_16_1`), Q7/CCE/TopSP off-by-ones behind the `q7_pool_core_type` and `nx_core_type` rows
- [Memory Hierarchy, BAR Layout and State Buffer](memory-hierarchy.md) — the complete BAR0 CSR atlas, the SBUF/PSUM region geometry, the 4-quadrant mesh PA scheme behind the die-flip topology
- [Per-Arch Device Layer: Geometry and Address Map](../runtime/arch-geometry.md) — the runtime-side per-arch offset-math leaves (CSR/notific/mem APB windows, SDMA/sem/evt address tables) that consume these constants
- [DHAL V3 (Trn2)](../kernel/dhal-v3.md) — the kernel-side V3 callback bodies, the V4 = V3-base + 8-override model, and the die-flip / HBM-size deltas in their `file:line` source form
