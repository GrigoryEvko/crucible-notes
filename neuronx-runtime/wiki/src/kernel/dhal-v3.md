# DHAL V3 (Trn2)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. This page owns the V3 arch-ops cell — `v3/neuron_dhal_v3.c` (2032 lines), `v3/address_map.h` (229 lines), `v3/sdma.h` (113 lines) — all read in full; every register offset, mask, and constant below is transcribed verbatim. The vtable container it fills (`struct neuron_dhal`, `neuron_dhal.h`) is owned by [dhal-core](dhal-core.md); the worker that drives the reset slots is owned by [reset](reset.md); the pod-election callees are owned by [pod-election](pod-election.md). Other driver versions renumber these lines.*
> *Evidence grade: **Confirmed (source-anchored)** — the registrar, the platform/qemu/emu specialization, and every callback body are direct C, not reverse-engineered. · Part III — Kernel Driver, DEEP · [back to overview](overview.md)*

## Abstract

V3 is the DHAL leaf for **Trainium 2** (PCI device `0x7364`). It is one of three arch leaves below [dhal-core](dhal-core.md)'s `vc → arch` compose, and it is the most substantial of them: its entry point `ndhal_register_funcs_v3` (`v3/neuron_dhal_v3.c:1868`) fills every scalar and ~55 function-pointer slots of the global `ndhal` with V3 implementations, branches once on platform (qemu / emulator / real silicon) to swap the reset callbacks and DMA-engine counts, conditionally initializes pod election, then sub-dispatches once on `pci_device_id` to set the four sysfs name suffixes. Where [dhal-v2](dhal-v2.md) is a self-contained registrar with nothing layered above it, V3 carries a second role: it is also the **base for V4**. [dhal-core](dhal-core.md)'s `NEURON_ARCH_V4` case calls `ndhal_register_funcs_v3()` to fill the whole vtable with Trn2 ops, *then* `ndhal_register_funcs_v4()` to override ~8 slots ([dhal-v4](dhal-v4.md)). A reimplementer must treat `ndhal_register_funcs_v3` as both the Trn2 leaf and the Trn3 floor — everything V4 does not touch is V3.

The defining V3 facts are a **wider, doubled topology** and a set of quirks that follow from it. V3 is `2 die × 2 SENG/die × 2 NC/SENG = 8` NeuronCores (`address_map.h:45-52`) against V2's 2, with 4 HBM channels (24 GiB usable each, `address_map.h:92`) against V2's 2, and 132 DMA engines (128 SENG + 4 top-level H2D/D2H) against V2's 34. The doubling forces a sharing model absent in V2: each SENG has two cores but only **one** host-to-tensor (H2T) engine, so the engine is shared and the two cores disambiguate by queue (`nc % 2` → q0/q1, `:708-711`). The 32-bit reset bitmap is wider (8 cores, 8 SDMA groups, 8 TopSP groups, plus a conditional per-SENG top-DMA hi-word), and the post-reset path runs HBM-7200 perf-profile detection and pod election that V2 has no concept of. V3 also drops V2's `ndma_retry_memcpy` hardware-bug retry (`false`, `:1937` vs V2 `true`) because the Trn2 DMA-in-reset hang was fixed in silicon, and it raises `num_beats` to 2296 (`:1936`, "allow up to 288 outstanding writes") from V2's 1024.

The single most counter-intuitive V3 feature is the **die-flip**, and the trap is its name: it is **not** a reset-register sequence. There is no MMIO doorbell that "flips a die." The die-flip is a *software* logical→physical NeuronCore renumbering, applied only at the cdev nc-map ioctl boundary, that XORs each core index with `0x6` (`:1539`) when the device is the flipped half of a 2-node UltraServer pod (node_id 1 or 3, `:1554`). It touches no hardware. This page proceeds: the registrar and its platform/device-id specialization; the reset initiate + wait-poll bodies and the `tpb_reset_map` bit layout as annotated pseudocode; the 8-NC topology and H2T-sharing model; the `ndma_init_v3` SDMA register sequence; the slot/Function Map table; then the die-flip and HBM-7200 callouts.

For reimplementation, the contract is:

- **The single registrar, its dual role, and its specialization axes** — `ndhal_register_funcs_v3` fills all slots unconditionally, then a *platform* branch (qemu/emu/real) swaps the reset fns + DMA-engine counts + `dice_per_device`, then conditionally calls `npe_init()` for UltraServer, then a *device-id* switch sets the four sysfs suffixes. The same function is the V4 base; the order is fixed.
- **The reset `tpb_reset_map` build + wait-poll** — `nr_get_tpb_reset_map` packs a 32-bit per-core bitmask (lo: TPB | SDMA group | TopSP group; hi: conditional per-SENG top-DMA), hands the lo/hi pair to the FW handshake (no local MMIO on real HW), then the wait polls FW_STATUS, masks `DEVICE_READY (0x8)`, and backs off `100 ms × i` across 5 tries.
- **The 8-NC topology + H2T sharing** — `nc → seng → eng{128,129,130,131}`, even core → q0 / odd core → q1, and the even-core-only init rule (a partial reset re-inits the shared engine only when **both** cores of the SENG are in the reset set).
- **The `ndma_init_v3` register sequence** — per-engine UDMA base from the SENG/IO address map, UDMA init with the 65-descriptor packet cap, then the four SDMA writes (ROB←1, WOB←1, EVENT_ACCEL←1, ROBERT←0x26), then ring-id-error masking on SENG engines.
- **The die-flip software NC-remap** — XOR `0x6` on the logical core index for UltraServer node 1/3 or `force_die_flip`, applied to the base seng-swap table — and the recognition that this is *not* a reset register.

| | |
|---|---|
| **Owns** | `v3/neuron_dhal_v3.c` (2032 ln) · `v3/address_map.h` (229 ln) · `v3/sdma.h` (113 ln), all read in full |
| **Registrar** | `ndhal_register_funcs_v3` (`:1868-2032`) — fills all slots, branches platform, inits pod election, sub-dispatches device-id; **also the V4 base** (`neuron_dhal.c:44`) |
| **Device-id sub-dispatch** | `:2009-2024` — `TRN2_DEVICE_ID0 (0x7364)` → `ndhal_register_funcs_trn2` (`:220`); `TRN3_DEVICE_ID0/1` → no-op (V4 reuse); else `-EINVAL` |
| **Platform branch** | `:1970-1997` — qemu (retry×1000, qemu reset fns, `dice=1`) · emu (retry×1000, emu reset fns, NC/eng from params, `dice=1`) · real (real reset fns) |
| **Hardware shape** | 2 die, 4 SENG, 8 NeuronCores, 4 HBM (24 GiB each = 96 GiB usable), 16 TopSP, 5 TPB eng/NC, 16 DMA eng/NC + 4 top-level = 132 DMA eng/dev, 16 q/eng (`address_map.h:45-92`) |
| **Reset status poll** | `bar0 + V3_MMAP_BAR0_APB_IO_0_MISC_RAM_OFFSET (0x6c84000) + V2_FW_IO_REG_FW_STATUS_OFFSET (0x808)`; ready mask `0x8`; backoff `100 ms × i`, 5 tries (`:358-378`) |
| **Reset deadline / retry** | `initiate_max_wait_time = 1000*480 = 480000 ms` (`:31,:1893`) · `retry_count = NR_RESET_RETRY_COUNT (5)` (`:1894`; `×1000` on qemu/emu) |
| **H2T sharing** | 4 top engines `{128,129,130,131}` (`:692-696`), one per SENG, shared by 2 cores; `qid = nc % 2` (`:711`); even-core-only init (`:734`) |
| **SDMA bring-up** | ROB(`0x1c`)←1 · WOB(`0x20`)←1 · EVENT_ACCEL(`0x0`)←1 · ROBERT_TXDF(`0x800`)←`0x26` (`sdma.h:67-76`) |
| **UDMA scalars** | `num_queues = DMA_MAX_Q_V4 (16)` (`:1935`) · `num_beats = 2296` (`:1936`) · `ndma_retry_memcpy = false` (`:1937`, V2 = true) |
| **Die-flip** | software NC-remap, **not** a register — XOR `neuron_nc_map_die_flip_mask = 0x6` (`:1539`) for UltraServer node 1/3 / `force_die_flip` (`:1541-1583`) |

---

## 1. The Registrar, the V4 Base Role, and the Specialization Axes

### Purpose

`ndhal_register_funcs_v3` (`v3/neuron_dhal_v3.c:1868-2032`) is the only exported function of the cell. [dhal-core](dhal-core.md)'s `neuron_dhal_init` calls it once for the host after the unconditional `vc` base, when the latched arch is `NEURON_ARCH_V3` (`neuron_dhal.c:41`) — **and** as the base layer for `NEURON_ARCH_V4` (`neuron_dhal.c:44`, before the V4 overrides run). It is straight-line pointer assignment into the live global, returning `-EINVAL` only when `ndhal` is NULL (`:1871-1874`), `npe_init()` fails on an UltraServer (`:2001-2004`), the device-id is neither Trn2 nor a Trn3 reuse-id (`:2021-2023`), or the `dev_nc_map` sanity check trips (`:2026-2029`).

The function has four phases in fixed order: **(1)** fill the `ndhal_address_map` scalars and every vtable slot with the V3 implementation (`:1876-1967`); **(2)** a *platform* branch that swaps the two reset callbacks, the DMA-engine counts, and (for qemu/emu) `dice_per_device`, NC count, and `dev_nc_map` (`:1970-1997`); **(3)** a *pod-election* init that calls `npe_init()` only when `platform_type == ULTRASERVER` (`:1999-2007`); and **(4)** a *device-id* switch that sets the four sysfs suffix strings (`:2009-2024`). The two non-trivial axes are independent: platform decides *how* the device is reached and emulated; device-id decides only *what it is called* in sysfs.

> **QUIRK —** `ndhal_register_funcs_v3` is the **base for V4**, so its slots are not all final on Trn3. [dhal-core](dhal-core.md) runs it first for `NEURON_ARCH_V4`, then `ndhal_register_funcs_v4` overrides ~8 slots (platform-type, device-id, neighbor engine ids, the four HBM-size-touching slots, and `perf_update_hbm_7200_supported`). A reimplementer reading "the V3 registrar sets `mpset_set_dram_and_mpset_info_v3` with 24 GiB HBM" must remember that on Trn3 that exact slot is immediately re-pointed at the 36 GiB V4 body. The V3 device-id switch even has explicit `TRN3_DEVICE_ID0/1` cases that fall through to no-op (`:2017-2020`, comment *"remove once v4 dhal stops re-using v3"*) precisely so the V3 base does not reject a Trn3 device before V4 runs. Everything V4 does **not** override is the V3 body documented here.

### Entry Point

```text
neuron_dhal_init (neuron_dhal.c:10)            [dhal-core]
  └─ ndhal_register_funcs_vc()        (:34)    ── version-common base (4 tpb slots)
  └─ ndhal_register_funcs_v3()        (:41 V3 ; :44 as V4 base)  ── THIS cell (v3/neuron_dhal_v3.c:1868)
       ├─ platform_type = ndhal_platform_type_v3()  ── :1876   (strncmp instance name → STD|ULTRASERVER|PDS)
       ├─ fill address_map scalars             ── :1877-1892   pci_host_base, nc_per_device=8, dram_channels=4, ...
       ├─ fill ~55 vtable slots                ── :1893-1967   reset/topsp/nc/nq/.../tpb/perf/ext_cleanup
       ├─ platform branch  qemu | emu | real   ── :1970-1997   (reset fns, dma counts, dice_per_device)
       ├─ if ULTRASERVER → npe_init()          ── :1999-2004   pod election
       └─ device-id switch                     ── :2009-2024
            ├─ 0x7364 → ndhal_register_funcs_trn2 (:220)  ── sysfs suffix = "Trn2"/"Trainium2"
            ├─ 0x7564/0x7565 (Trn3) → no-op    ── :2018-2020 (V4 reuse; overrides applied later by v4)
            └─ default → -EINVAL               ── :2022-2023
       └─ dev_nc_map sanity: dev_nc_map < (1<<nc_per_device) ── :2026-2029
```

### Algorithm

The specialization tail, modelling `ndhal_register_funcs_v3` (`:1969-2031`):

```c
function ndhal_register_funcs_v3():                       // v3/neuron_dhal_v3.c:1868
    if (ndhal == NULL): return -EINVAL                     // :1871-1874
    ndhal_arch.platform_type = ndhal_platform_type_v3()    // :1876  STD | ULTRASERVER | PDS
    fill_address_map_scalars()                             // :1877-1892  nc_per_device=8, dram_channels=4, ...
    fill_all_vtable_slots()                                // :1893-1967  reset/ndmar/ndma/fw_io/.../perf/tpb

    // ── AXIS 1: PLATFORM — swap reset fns + DMA-engine counts + dice ─────────────
    if (narch_is_qemu()):                                  // :1970
        ndhal_reset.retry_count *= 1000                    // :1971  wait far longer under qemu (5000 tries)
        ndhal_reset.nr_initiate_reset            = nr_initiate_reset_v3_qemu             // :1972
        ndhal_reset.nr_wait_for_reset_completion = nr_wait_for_reset_completion_v3_qemu // :1973
        seng_dma_eng_per_nd = V3_NC_PER_DEVICE * V3_DMA_ENG_PER_NC   // :1974   = 8*16 = 128
        h2d_dma_eng_per_nd  = V3_NUM_H2D_DMA_PER_DEVICE             // :1975   = 4
        dice_per_device     = 1                            // :1976   qemu models a single die
        nmetric_log_posts   = 0                            // :1979   metrics off on qemu
    else if (narch_is_emu()):                              // :1980
        ndhal_reset.retry_count *= 1000                    // :1981
        ndhal_reset.nr_initiate_reset            = nr_initiate_reset_v3_emu             // :1982 (== real)
        ndhal_reset.nr_wait_for_reset_completion = nr_wait_for_reset_completion_v3_emu // :1983 (== real)
        seng_dma_eng_per_nd = nc_per_dev_param * V3_DMA_ENG_PER_NC  // :1984   emulator overrides NC count
        h2d_dma_eng_per_nd  = nc_per_dev_param             // :1985
        nc_per_device       = nc_per_dev_param             // :1986
        dev_nc_map          = dev_nc_map                   // :1987   from module param
        dice_per_device     = 1                            // :1988
        nmetric_log_posts   = 0                            // :1991
    else:                                                  // :1992   real silicon
        ndhal_reset.nr_initiate_reset            = nr_initiate_reset_v3                 // :1993
        ndhal_reset.nr_wait_for_reset_completion = nr_wait_for_reset_completion_v3      // :1994
        seng_dma_eng_per_nd = V3_NC_PER_DEVICE * V3_DMA_ENG_PER_NC   // :1995   = 128
        h2d_dma_eng_per_nd  = V3_NUM_H2D_DMA_PER_DEVICE             // :1996   = 4

    // ── AXIS 2: POD ELECTION — only UltraServer brings up the election state ─────
    if (platform_type == NEURON_PLATFORM_TYPE_ULTRASERVER):       // :1999
        ret = npe_init()                                   // :2000   BOUNDARY [pod-election]
        if (ret): pr_err("failed ... pod election"); return ret   // :2001-2004
    else if (platform_type == NEURON_PLATFORM_TYPE_PDS):   // :2005   TODO PDS — no init yet

    // ── AXIS 3: DEVICE-ID — set only the four sysfs suffix strings ───────────────
    switch (ndhal->pci_device_id):                         // :2009
        case TRN2_DEVICE_ID0 (0x7364):                     // :2010
            ret = ndhal_register_funcs_trn2()              // :2011  nd/nc="v3" inst="Trn2" name="Trainium2"
        case TRN3_DEVICE_ID0, TRN3_DEVICE_ID1:             // :2018-2019  V4 reuse — fall through, no-op
            break                                          // :2020
        default:                                           // :2021
            pr_err("Unknown HW architecture. Can't init neuron_dhal."); return -EINVAL  // :2022-2023

    // ── SANITY: the configured core bitmap must fit the core count ──────────────
    if (dev_nc_map >= (1 << nc_per_device)):               // :2026  note: >=, reject if it does NOT fit
        pr_err("Invalid nc map for device"); return -EINVAL // :2028
    return ret                                             // :2031
```

`ndhal_platform_type_v3` (`:240-263`) decides the platform by string-matching the instance type name: four UltraServer SKUs (`trn2p.48xlarge`, `trn2eu.48xlarge`, `trn2u.48xlarge`, `trn2u-ac.24xlarge`, `:234-237`, `:246-250`) → `ULTRASERVER`; `trn2es.48xlarge` (`:238`, `:251`) → `PDS`; anything else → `STD`. The `force_userver` module param overrides any result to `ULTRASERVER` (`:258-260`).

### Function Map

The complete V3 slot fill. Slot names are the `ndhal_<area>.<member>` the registrar assigns; `Body` is the implementation definition line, `Reg` the assignment line. Confidence is HIGH throughout — open GPL source, no decompilation.

| Slot (`ndhal_…`) | V3 impl fn | Body | Reg | Conf |
|---|---|---|---|---|
| `arch.platform_type` (value) | `ndhal_platform_type_v3` (STD/ULTRASERVER/PDS) | `:240` | `:1876` | HIGH |
| `address_map.*` (16 scalars) | `nc_per_device=8`, `dram_channels=4`, `dice=2`, … | — | `:1877-1892` | HIGH |
| `reset.{initiate_max_wait_time,retry_count}` | `480000 ms` / `NR_RESET_RETRY_COUNT (5)` | `:31` | `:1893-1894` | HIGH |
| `reset.nr_post_reset_config` | `nr_post_reset_config_v3` (HBM-7200 + pod election) | `:413` | `:1895` | HIGH |
| `reset.nr_initiate_reset` (real) | `nr_initiate_reset_v3` | `:316` | `:1993` | HIGH |
| `reset.nr_wait_for_reset_completion` (real) | `nr_wait_for_reset_completion_v3` | `:358` | `:1994` | HIGH |
| `topsp.ts_nq_{init,destroy_one,get_nqid,set_hwaddr}` | `ts_nq_*_v3` | `:491,:539,:441,:458` | `:1896-1899` | HIGH |
| `nc.{nc_get_semaphore_base,nc_get_event_addr}` | `nc_get_*_v3` | `:561,:577` | `:1900-1901` | HIGH |
| `nq.{nnq_get_nqid,nnq_set_hwaddr}` | `nnq_*_v3` | `:595,:610` | `:1902-1903` | HIGH |
| `mpset.mpset_set_dram_and_mpset_info` | `mpset_set_dram_and_mpset_info_v3` (4×24 GiB) | `:639` | `:1906` | HIGH |
| `ndmar.{get_h2t_eng_id,get_h2t_def_qid,is_h2t_def_q,nr_init_h2t_eng,is_nx_ring,quiesce_queues}` (6) | `ndmar_*_v3` / `nr_init_h2t_eng_v3` | `:690-778` | `:1907-1912` | HIGH |
| `fw_io.{topology,readless_read_region,read_csr_array,execute_request,post_metric}` (5) | `fw_io_*_v3` | `:852-951` | `:1913-1917` | HIGH |
| `mmap.{dm_mmap_special,mmap_get_bar4_offset}` | `dm_mmap_special_v3[]` / `mmap_get_bar4_offset_v3` | `:51,:966` | `:1918-1919` | HIGH |
| `sysfs_metrics.{root_tbl_cnt,root_tbl,ecc,hbm_error,tensor_engine}` (5) | `*_v3` | `:990,:986,:1003,:1024,:1082` | `:1920-1924` | HIGH |
| `pci.{axi_bar,apb_bar,dram_bar}` | `BAR_UNUSED` / `0` / `4` | — | `:1925-1927` | HIGH |
| `pci.{get_device_id,device_id_to_rid_map}` | `neuron_pci_*_v3` | `:1139,:1203` | `:1928-1929` | HIGH |
| `cdev.{mem_regions,bar0_write_blocked_addrs,compatible_version,logical_to_physical_nc_map,get_default_tpbs_for_hbm}` (5) | `ncdev_*_v3` (compat 10..11) | `:114,:154,:1268,:1560,:1274` | `:1930-1934` | HIGH |
| `udma.{num_queues,num_beats}` | `DMA_MAX_Q_V4 (16)` / `2296` | — | `:1935-1936` | HIGH |
| `ndma.ndma_retry_memcpy` | `false` (V2 = true — Trn2 fixed the DMA-in-reset hang) | — | `:1937` | HIGH |
| `ndma.{wait_for_completion_time,validate_pa,init,is_bar0_write_blocked,get_m2m_barrier_type,get_engines_with_host_connectivity}` (6) | `ndma_*_v3` | `:1293-1590` | `:1938-1943` | HIGH |
| `npe.*` (9: notify/info/status/ctrl + 4 shows + neighbor_eng_ids) | `npe_*_v3` / `npe_neighbor_eng_ids_v3[2][2]` | `:1616-1808,:214` | `:1944-1952` | HIGH |
| `perf.{set,get,get_supported,update_hbm_7200}` (4) | `perf_*_v3` | `:1694-1729` | `:1953-1956` | HIGH |
| `tpb.pe_{xbus,row_grp,col_grp}_count` | `9 / 4 / 4` (V2 xbus = 5) | — | `:1957-1959` | HIGH |
| `tpb.pe_perf_reg_grp_size` | `V3_TPB_ARR_SEQ_QUEUE_PERF_SIZE (0x30)` | — | `:1960` | HIGH |
| `tpb.pe_*_cntr_offsets` (4 tables) | `ntpb_pe_*_cntr_offsets_v3[8]` | `:166-202` | `:1961-1964` | HIGH |
| `tpb.pe_get_aggregated_wl_cycle_cnt` | `ntpb_pe_get_aggregated_wl_cycle_cnt_v3` | `:1820` | `:1965` | HIGH |
| `ext_cleanup` | `ndhal_ext_cleanup_v3` (UltraServer → `npe_cleanup`) | `:1844` | `:1967` | HIGH |
| `sysfs_metrics.arch_*_suffix` | `ndhal_register_funcs_trn2` | `:220` | `:2011` | HIGH |

> **GOTCHA —** the `dev_nc_map` sanity check at `:2026` reads `if (dev_nc_map >= (1 << nc_per_device)) return -EINVAL` — it rejects a map that does **not** fit the core count. With `nc_per_device = 8` (`:1885`) the legal range is `0..0xff` and the registrar pre-sets `dev_nc_map = (1<<8)-1 = 0xff` (`:1886`), so the gate passes on real silicon; it can only trip under the emulator path, which overwrites both `nc_per_device` and `dev_nc_map` from module params (`:1986-1987`). The comparison is `>=`, not `<` — read it as "reject when the bitmap has a bit above the highest core." This mirrors V2's identical gate but with 8 cores instead of 2.

---

## 2. Reset — `tpb_reset_map` Build and Wait-Poll

### Purpose

The reset slots are driven by the arch-neutral worker in [reset](reset.md): stage 1 calls `ndhal_reset.nr_initiate_reset`, stage 2 calls `ndhal_reset.nr_wait_for_reset_completion`, and the worker finishes with `ndhal_reset.nr_post_reset_config`. V3 supplies three variants of each initiate/wait pair — real, qemu, emulator — selected by the platform branch (§1). On real silicon, **initiate does no MMIO of its own**: it builds the 32-bit reset bitmap and hands the lo/hi pair to `nr_initiate_reset_via_fw` (owned by [reset](reset.md)), which pokes BAR0 and drives the FW. **Wait** is the V3-specific poll loop that reads FW_STATUS and tests `DEVICE_READY`. **Post-reset** is where V3 diverges sharply from V2: it lazily detects HBM-7200 support and, on non-STD platforms, kicks pod election.

### Algorithm

The reset-map builder plus the real-path initiate, the wait poll, and post-reset, modelling `nr_get_tpb_reset_map` / `nr_initiate_reset_v3` / `nr_wait_for_reset_completion_v3` / `nr_post_reset_config_v3` (`:283-428`):

```c
function nr_get_tpb_reset_map(nc_map, *lo, *hi):           // v3/neuron_dhal_v3.c:283  (pure bitmask, NO MMIO)
    if (nc_map == NEURON_NC_MAP_DEVICE): return            // :289  whole-device: leave map 0, FW resets all
    for i in [0 .. MAX_NC_PER_DEVICE):                     // :290
        if (!((1 << i) & nc_map)): continue                // :291
        *lo |= (1 << i)                                    // :293  TPB i        → bit i      (bits 0..7)
        *lo |= (1 << (i + 8))                              // :294  SDMA group i → bit i+8   (bits 8..15)
        *lo |= (1 << (i + 16))                             // :295  TOPSP group  → bit i+16  (bits 16..23)
        // bits 24..31 (CC_TOP groups, 4b/grp per the :273-280 comment) are NEVER set here
    if (reset_top_dma):                                    // :300  module param gates the hi-word entirely
        for i in [0 .. V3_SENG_PER_DEVICE(4)):             // :301
            seng_mask = ((1 << V3_NC_PER_SENG(2)) - 1) << (i * V3_NC_PER_SENG)   // :302  = 0b11 << 2i
            if ((nc_map & seng_mask) == seng_mask):        // :303  BOTH cores of SENG i present
                *hi |= (1 << i)                            // :304  reset SENG i's top DMA

function nr_initiate_reset_v3(nd, nc_map):                 // v3/neuron_dhal_v3.c:316  (real path)
    if (no_reset): return 0                                 // :321
    lo = 0; hi = 0
    nr_get_tpb_reset_map(nc_map, &lo, &hi)                 // :324
    // NO MMIO here on real HW — the trigger is the FW handshake in [reset]; lo AND hi delivered.
    return nr_initiate_reset_via_fw(nd, nc_map, lo, hi)    // :326  BOUNDARY neuron_reset.c

function nr_wait_for_reset_completion_v3(nd):              // v3/neuron_dhal_v3.c:358  (real path)
    if (no_reset): return 0                                 // :360
    // FW_STATUS register, host-BAR0 byte offset = MISC_RAM(0x6c84000) + FW_STATUS(0x808):
    addr = bar0 + V3_MMAP_BAR0_APB_IO_0_MISC_RAM_OFFSET    // :364  0x6c84000  (FIXME: V2_-named FW_STATUS macro)
              + V2_FW_IO_REG_FW_STATUS_OFFSET              //       0x808
    for i in [0 .. retry_count(5)):                        // :366  retry_count *= 1000 on qemu/emu
        reset_in_progress = true
        if (fw_io_read_csr_array(&addr, &status, 1, false) == 0):       // :370  readless CSR read
            reset_in_progress = status & V2_FW_IO_REG_FW_STATUS_DEVICE_READY_MASK  // :371  mask 0x8
        if (!reset_in_progress): return 0                  // :372  success ⇔ read OK and READY-bit clear
        if (nr_msleep_stoppable(nd, NR_RESET_RETRY_SLEEP_MS * i)): return -1  // :374  100ms*i; i=0 → 0ms
    return -1                                              // :377  5 tries exhausted → timeout

function nr_post_reset_config_v3(nd, reset_successful, is_no_reset):    // v3/neuron_dhal_v3.c:413
    if (reset_successful && !is_no_reset):                 // :415
        if (nd->supports_hbm_7200 == -1):                  // :416  sentinel: detect ONCE, lazily
            perf_update_hbm_7200_supported(nd)             // :417  V3: fw_io query ; V4: hardcode 0
    else:
        nd->supports_hbm_7200 = 0                          // :420  failed/no-reset → assume unsupported
    if (platform_type == NEURON_PLATFORM_TYPE_STD): return 0   // :423  STD has no pod
    npe_election_exec_on_rst(nd, reset_successful)         // :427  BOUNDARY [pod-election]
    return 0                                               // :428
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `nr_get_tpb_reset_map` | `:283-309` | pack lo (TPB · SDMA · TopSP per core) + hi (per-SENG top-DMA, gated by `reset_top_dma`); no MMIO | HIGH |
| `nr_initiate_reset_v3` | `:316-331` | real path: build lo/hi, hand to FW (no local MMIO) | HIGH |
| `nr_initiate_reset_v3_qemu` | `:333-346` | qemu: `writel(lo)` to `bar0 + APB_IO_0 + RESERVED2 + 0x10`; **hi ignored** | HIGH |
| `nr_initiate_reset_v3_emu` | `:348-351` | tail-call `nr_initiate_reset_v3` (identical to real) | HIGH |
| `nr_wait_for_reset_completion_v3` | `:358-378` | poll `bar0+0x6c84000+0x808`, mask `0x8`, `100ms*i` backoff, 5 tries | HIGH |
| `nr_wait_for_reset_completion_v3_qemu` | `:380-400` | `readl` of RESERVED2+0x10, flat `msleep(2000)`/iter | HIGH |
| `nr_post_reset_config_v3` | `:413-428` | lazy HBM-7200 detect + (non-STD) pod election | HIGH |
| `nr_initiate_reset_via_fw` | `neuron_reset.c` | BAR0 trigger + FW drive (boundary, [reset](reset.md)) | HIGH |
| `fw_io_read_csr_array_v3` | `:910` | readless CSR read used by the wait loop | HIGH |

### Considerations

The wait loop's backoff is `100 ms × i` with `i` running `0..4`, so the first iteration sleeps **zero** (`i=0`) and the worst case is `0+100+200+300+400 = 1000 ms` across 5 reads — far below the `initiate_max_wait_time` of 480 s, which bounds the *initiate* FW handshake in [reset](reset.md), not this wait. On qemu and emulator the platform branch multiplies `retry_count` by 1000 (`:1971,:1981`) so the wait tolerates 5000 reads; the qemu wait variant replaces the FW_STATUS read with a `readl` of the qemu doorbell at `bar0 + APB_IO_0 + RESERVED2 + 0x10` and a flat `msleep(2000)` per iteration (`:386-398`).

The lazy HBM-7200 detection is a one-shot keyed on the `-1` sentinel in `nd->supports_hbm_7200`: the very first successful, non-no-reset reset queries the firmware once and caches the result; every later reset that finds the field already set (`!= -1`) skips the query. A failed reset forces the field to `0` (`:420`), so a device that never resets cleanly is treated as not supporting the high-bandwidth profile.

> **GOTCHA —** the wait loop's local is named `reset_in_progress` but is assigned the `DEVICE_READY` masked bit, and the loop returns success on `if (!reset_in_progress)` (`:371-372`). Read literally: it returns 0 when the CSR read **succeeds** and the `DEVICE_READY (0x8)` bit is **clear**. The variable name inverts the FW-status semantic. The behavior is unambiguous from the code; only the name is confusing. A reimplementation should preserve the literal logic (`success ⇔ read_ok && !(status & 0x8)`), not the variable's English. This is byte-identical to V2's wait — the `V2_FW_IO_REG_*` macro names are reused, with a source FIXME (`:364,:371`) noting they should be abstracted into a V3 header. *(MEDIUM on the human interpretation only; the code path is HIGH.)*

> **NOTE —** the qemu reset *initiate* writes **only the lo word** of the bitmap to RESERVED2+0x10 (`:343`); the hi word (per-SENG top-DMA) is computed but discarded on qemu. On real silicon both words travel to the FW via `nr_initiate_reset_via_fw`. The hi word is itself conditional — `reset_top_dma` is an `extern int` module param (`:28`); its default is owned by [reset](reset.md) and not set in this cell, so whether the top-DMA reset bits are ever delivered on a stock load is **MEDIUM** (the bit math is HIGH).

---

## 3. The 8-NC Topology and H2T-Engine Sharing

### Purpose

V3's device is `2 die × 2 SENG/die × 2 NC/SENG = 8` NeuronCores (`address_map.h:45-52`). Each NC owns 16 DMA engines, so the four SENGs hold `4 × 32 = 128` SENG DMA engines (`V3_NUM_SENG_DMA_PER_DEVICE`, `address_map.h:60`); on top sit 4 top-level host↔device engines (idx `128..131`, `address_map.h:61`), for 132 total (`address_map.h:62`). Host-to-tensor (H2T) data movement uses **only** the 4 top engines — and because a SENG has two cores but only one top engine, the engine is shared. The arch-neutral ring layer resolves "which engine and which queue does this core's H2T use" through four V3 callbacks; the sharing model lives entirely in them.

### Algorithm

The H2T engine/queue resolution and the even-core init rule, modelling `ndmar_get_h2t_eng_id_v3` / `ndmar_get_h2t_def_qid_v3` / `nr_init_h2t_eng_v3` / `ndmar_is_nx_ring_v3` (`:690-766`):

```c
function ndmar_get_h2t_eng_id_v3(nd, nc_id):              // v3/neuron_dhal_v3.c:690
    const h2d_dma_eng_id[4] = { V3_D2H_0_IDX(128),        // :692-696  one entry per SENG
                                V3_H2D_0_IDX(129),
                                V3_D2H_1_IDX(130),
                                V3_H2D_1_IDX(131) }
    seng_id = nc_id / V3_NC_PER_SENG(2)                   // :698  nc0,1→0  nc2,3→1  nc4,5→2  nc6,7→3
    return h2d_dma_eng_id[seng_id]                        // :699  → 128 / 129 / 130 / 131

function ndmar_get_h2t_def_qid_v3(nc_id):                // v3/neuron_dhal_v3.c:708
    // H2T engine is SHARED by the 2 cores of a SENG: even core → q0, odd core → q1
    return nc_id % V3_NC_PER_SENG(2)                      // :711

function nr_init_h2t_eng_v3(nc_idx, nc_map):             // v3/neuron_dhal_v3.c:734
    if (nc_idx % V3_NC_PER_SENG(2) != 0): return false   // :737  only the EVEN/first core owns init
    if (nc_map == NEURON_NC_MAP_DEVICE): return true     // :741  device reset always re-inits the engine
    // partial reset: re-init the shared engine ONLY if BOTH cores of the SENG are being reset,
    // so it is not yanked out from under a still-running peer core
    seng_id  = nc_idx / V3_NC_PER_SENG                   // :747
    seng_map = ((1 << V3_NC_PER_SENG) - 1) << (V3_NC_PER_SENG * seng_id)   // :748  = 0b11 << 2*seng_id
    return ((nc_map & seng_map) == seng_map)             // :749

function ndmar_is_nx_ring_v3(eng_id, q_id):             // v3/neuron_dhal_v3.c:758
    // last queue (15) = collectives; second-to-last (14) on engs {0,1,2,3,4,7,8} = NX cores
    return (q_id == V3_DMA_QUEUE_PER_ENG - 2 /* 14 */) &&        // :762
           ( ((eng_id % V3_DMA_ENG_PER_NC) < V3_TPB_ENG_PER_NC(5))  // :763  engs 0..4
             || ((eng_id % V3_DMA_ENG_PER_NC) == 7)                 // :764
             || ((eng_id % V3_DMA_ENG_PER_NC) == 8) )               // :765
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `ndmar_get_h2t_eng_id_v3` | `:690-700` | `nc → seng → eng{128,129,130,131}` | HIGH |
| `ndmar_get_h2t_def_qid_v3` | `:708-712` | shared-engine queue: `nc % 2` (even q0 / odd q1) | HIGH |
| `ndmar_is_h2t_def_q_v3` | `:722-725` | predicate: `used_for_h2t && (qid==0 || qid==1)` | HIGH |
| `nr_init_h2t_eng_v3` | `:734-750` | even-core-only init; both-cores-of-SENG for partial reset | HIGH |
| `ndmar_is_nx_ring_v3` | `:758-766` | reserved-ring predicate: `q==14` on engs `{0..4,7,8}` | HIGH |
| `ndmar_quiesce_queues_v3` | `:778` | pause all 16 rings on all 16 engines of an NC (boundary udma) | HIGH |

### Considerations

The sharing model is the whole reason V3 needs a distinct `nr_init_h2t_eng` callback at all — V2's two cores each own a private top engine, so V2 has no "init ownership" question. On V3 the even/first core of each SENG owns the one-time bring-up of the shared engine; the odd core never initializes it, only uses queue 1 of it. The partial-reset guard (`:748-749`) is the subtle part: re-initializing the shared engine while the peer core is still running would clobber the peer's in-flight H2T queue, so a partial reset re-inits only when its `nc_map` covers **both** cores of the SENG. A reimplementation that re-inits the engine on any reset that includes the even core will corrupt the odd core's transfers.

> **QUIRK —** the engine-id table interleaves D2H and H2D names per SENG: `{D2H_0=128, H2D_0=129, D2H_1=130, H2D_1=131}` (`:692-696`), indexed by `seng_id` 0..3. The four top engines are not "two D2H then two H2D" — they alternate, so SENG 0 maps to the `D2H_0` index and SENG 1 to the `H2D_0` index. A reimplementer who lays the four top engines out as `{D2H_0, D2H_1, H2D_0, H2D_1}` and indexes by `seng_id` will route SENG 1 and SENG 2 to the wrong engines. Use the literal table order.

---

## 4. DMA Engine Init — `ndma_init_v3` and the SDMA Block

### Purpose

`ndma_init_v3` (`:1337-1411`) brings up one DMA engine: it resolves the engine's UDMA base from the SENG/IO address map, derives the SDMA register block at a fixed offset above it, initializes the UDMA engine, writes the four SDMA reorder/event-accel/backpressure registers, and (for SENG engines only) masks spurious ring-id-error interrupts. It is the slot the DMA-rings bring-up calls per engine (`ndhal_ndma.ndma_init`, `:1940`). The engine index space is `0..131`: `0..127` are the four SENGs' DMA engines (32 per SENG), `128..131` are the top-level H2D/D2H engines.

### Algorithm

The base resolution plus the SDMA bring-up sequence, modelling `ndma_init_v3` (`:1337-1411`) and the `sdma.h` helper it calls:

```c
function ndma_init_v3(bar0, udma, eng_id):                // v3/neuron_dhal_v3.c:1337
    d2h_0 = (eng_id == V3_D2H_0_IDX(128));  h2d_0 = (eng_id == V3_H2D_0_IDX(129))   // :1341-1342
    d2h_1 = (eng_id == V3_D2H_1_IDX(130));  h2d_1 = (eng_id == V3_H2D_1_IDX(131))   // :1343-1344

    if (h2d_0 || d2h_0):                                   // :1348  top engine on IO half 0
        base = (h2d_0 ? V3_APB_IO_0_H2D_UDMA_BASE : V3_APB_IO_0_D2H_UDMA_BASE)      // :1349
        relbase = base - V3_APB_IO_0_BASE + V3_PCIE_BAR0_APB_IO_0_OFFSET            // :1350  device-PA → BAR0 off
    else if (h2d_1 || d2h_1):                              // :1351  top engine on IO half 1
        base = (h2d_1 ? V3_APB_IO_1_H2D_UDMA_BASE : V3_APB_IO_1_D2H_UDMA_BASE)      // :1352
        relbase = base - V3_APB_IO_1_BASE + V3_PCIE_BAR0_APB_IO_1_OFFSET            // :1353
    else:                                                  // :1354  SENG engine 0..127
        seng_id     = eng_id / V3_NUM_DMA_ENG_PER_SENG(32)     // :1355
        eng_in_seng = eng_id % V3_NUM_DMA_ENG_PER_SENG         // :1356
        relbase = V3_APB_SE_{seng}_SDMA_0_BASE + eng_in_seng * V3_APB_SDMA_DIST(0x100000)  // :1358..1379
        relbase = relbase - V3_APB_SE_{seng}_BASE + V3_PCIE_BAR0_APB_SE_{seng}_OFFSET       // device-PA → BAR0 off
        se_user_fis_relbase = ... USER_FIS errtrig window per-engine ...            // :1360..1384

    udma_base = bar0 + relbase                             // :1388
    sdma_base = udma_base + V3_APB_SDMA_MISC_OFFSET(0x40000)   // :1389  SDMA regs sit 0x40000 above UDMA

    snprintf(udma_name, "UDMA_ENG_%d", eng_id)             // :1391
    ret = udma_m2m_init_engine(udma, udma_base, DMA_MAX_Q_MAX, udma_name, 0,
                               V3_ALLOWED_DESC_PER_PACKET + 1 /* =65 */, false)   // :1392-1393
    if (ret): goto done                                    //         "+1 to allow for MD descriptor"

    ret = sdma_init_engine(sdma_base):                     // :1398   (sdma.h:61)
        if (sdma_base == NULL): return -EFAULT             // sdma.h:63
        reg_write32(sdma_base + REG_SDMA_ROB_CFG_OFFSET     /*0x1c*/, 1)     // sdma.h:67  enable Read Reorder Buffer
        reg_write32(sdma_base + REG_SDMA_WOB_CFG_OFFSET     /*0x20*/, 1)     // sdma.h:70  enable Write Reorder Buffer
        reg_write32(sdma_base + REG_SDMA_EVENT_ACCEL_OFFSET /*0x0*/, 1)      // sdma.h:73  enable event acceleration
        reg_write32(sdma_base + REG_SDMA_MODEL_ROBERT_TXDF  /*0x800*/, 0x26) // sdma.h:76  backpressure: 0x26 beats
    if (ret): goto done                                    // :1399-1402

    if (eng_id < V3_NUM_SENG_DMA_PER_DEVICE(128)):         // :1404  SENG engines only
        udma_m2m_mask_ring_id_error(udma, bar0 + se_user_fis_relbase)   // :1406  mask spurious ring-id-err IRQ
done:
    return ret                                             // :1410
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `ndma_init_v3` | `:1337-1411` | per-engine bring-up: base resolve → UDMA init → SDMA regs → ring-id mask | HIGH |
| `sdma_init_engine` | `sdma.h:61-79` | ROB←1 (`0x1c`), WOB←1 (`0x20`), EVENT_ACCEL←1 (`0x0`), ROBERT_TXDF←`0x26` (`0x800`) | HIGH |
| `sdma_configure_broadcast_v3` | `sdma.h:93-113` | broadcast group mask + last-node flag; **not wired into a DHAL slot here** | MEDIUM |
| `udma_m2m_init_engine` | boundary (`udma/`) | UDMA engine init; receives the `65` packet cap | HIGH |
| `udma_m2m_mask_ring_id_error` | boundary (`udma/`) | mask spurious ring-id-error interrupts (SENG engines) | HIGH |

### Considerations

The SDMA register block sits at `udma_base + 0x40000` (`V3_APB_SDMA_MISC_OFFSET`, `:1389`), a fixed offset above the UDMA block — there is no separate SDMA base table as V2 has; V3 derives both blocks from the one computed `relbase`. The four SDMA writes are unconditional `reg_write32` enables/values; there is no read-modify-write, so a reimplementer must not assume the config registers carry other live bits to preserve. The `sdma.h:15-39` bit-field comments document `force_inorder`, `clear`, `use_rid_base`, `rid_base`, `powerdown` on the ROB/WOB registers, but V3 writes a flat `1`, enabling only bit 0 (`en`).

The fourth SDMA write — `REG_SDMA_MODEL_ROBERT_TXDF (0x800) = 0x26` (`sdma.h:76`) — is the V3 addition over V2, which writes only three SDMA registers. The `0x26` value (decimal 38) lands in the `0:9 overhead_beats_outstanding` field (`sdma.h:46-49`) and is the backpressure tuning knob; it is consistent with the `num_beats = 2296` / "288 outstanding writes" scalar the registrar sets (`:1936`), both raising V3's outstanding-write budget over V2.

> **NOTE —** `sdma_configure_broadcast_v3` (`sdma.h:93-113`) is defined in the owned `sdma.h` but is **not** assigned to any `ndhal` slot in this cell — `ndma_init_v3` does not call it (unlike V2's `ndma_init_v2`, which configures broadcast inline). It builds a group mask by clearing the top `eng_id+1` bits of `U16_MAX` and flags `last_node = U16_MAX` for `eng_id ∈ {0,3,6,9,15}`, writing `GROUP (+0x100)` / `LAST_NODE (+0x104)` at `base + V3_APB_SENG_0_SDMA_0_APP_RELBASE`. Its caller is the collectives/broadcast setup, outside this cell — body HIGH, wiring MEDIUM.

---

## 5. The Die-Flip — A Software NC-Remap, Not a Register

### Purpose

The die-flip is the V3 feature most likely to be mis-modelled, because its name implies a hardware action. It is a **logical→physical NeuronCore renumbering** applied at the cdev nc-map ioctl boundary (`ndhal_cdev.ncdev_logical_to_physical_nc_map`, `:1933`), and it touches no MMIO. Trn2 UltraServer pairs two nodes in a P2P pod; the two nodes' dies face opposite directions, so for the cores of both nodes to present in a consistent global order, the "flipped" node renumbers its cores. The renumbering is a single XOR.

### Algorithm

The flip predicate and the remap, modelling `ndhal_die_flipped` / `ncdev_logical_to_physical_nc_map_v3` (`:1541-1584`):

```c
const neuron_nc_map_die_flip_mask = 0x6                   // v3/neuron_dhal_v3.c:1539  (toggles index bits {1,2})

function ndhal_die_flipped():                            // v3/neuron_dhal_v3.c:1541
    if (force_die_flip): return true                     // :1546  module param force
    if (platform_type != NEURON_PLATFORM_TYPE_ULTRASERVER): return false   // :1549  only UltraServer flips
    npe_get_pod_status(&state, &node_id)                 // :1553  BOUNDARY [pod-election]
    return (state == NEURON_POD_E_STATE_ULTRASERVER)     // :1554  flipped iff this is the
           && (node_id == 1 || node_id == 3)             //        odd-numbered node of the 2-node pod

function ncdev_logical_to_physical_nc_map_v3(map, max_num_entries, version):  // v3/neuron_dhal_v3.c:1560
    if (version != NEURON_IOCTL_NC_MAPPING_TYPE_V0): return -EINVAL           // :1567
    apply_dieflip   = ndhal_die_flipped()                // :1562
    entries_to_copy = min(max_num_entries, NC_MAPPING_MAX_CORE_COUNT_V3(128)) // :1564
    mapping         = nc_mapping_v0_seng_swap[128]        // :1571  base seng-swap table (:1518)
    for entry_idx in [0 .. entries_to_copy):              // :1573
        core_idx = entry_idx                              // :1574
        if (apply_dieflip): core_idx ^= 0x6               // :1575-1576  XOR toggles bits {1,2}
        WARN_ONCE(core_idx >= 128, ...)                   // :1578
        map->mappings[entry_idx] = mapping[core_idx]      // :1579  index the base table with the flipped idx
    map->num_entries = entries_to_copy                    // :1581
    return 0                                              // :1583
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `ndhal_die_flipped` | `:1541-1558` | flip predicate: `force_die_flip` OR (UltraServer ∧ pod-state-UltraServer ∧ node∈{1,3}) | HIGH |
| `ncdev_logical_to_physical_nc_map_v3` | `:1560-1584` | copy base seng-swap table, XOR `0x6` on the index when flipped | HIGH |
| `nc_mapping_v0_seng_swap[128]` | `:1518-1535` | base logical→physical core table (per-device 8-core rows) | HIGH |
| `npe_get_pod_status` | `neuron_pelect.c` | pod state + node_id source (boundary, [pod-election](pod-election.md)) | HIGH |

### Considerations

The base table `nc_mapping_v0_seng_swap` (`:1518`) is 128 entries — 16 devices × 8 cores — and a `static_assert` (`:1538`) pins its size to `NC_MAPPING_MAX_CORE_COUNT_V3 (128)`. Each 8-entry row is the per-device logical→physical core swap; e.g. ND1's row is `{2,3,0,1,4,5,6,7}` (`:1520`). The flip never rewrites the table; it only changes which row-entry a logical index reads, by XORing the index with `0x6` before the lookup. `0x6 = 0b110` toggles index bits 1 and 2, swapping the NC-quad ordering so the two dies' cores land in a consistent global order regardless of which physical die faces "up."

> **QUIRK —** the "die-flip" is **not a reset register and pokes no hardware**. A reimplementer scanning the reset path for a die-flip MMIO sequence will find none — the flip is purely the index XOR (`0x6`) in `ncdev_logical_to_physical_nc_map_v3` (`:1576`), reached only through the nc-map ioctl. It is gated on *pod topology* (UltraServer node 1 or 3, learned from `npe_get_pod_status`, `:1553`), not on any device register, and the only way to force it outside a real pod is the `force_die_flip` module param (`:1546`). Model it as a software renumbering at the ioctl boundary; do not look for a "flip die" doorbell. [dhal-v4](dhal-v4.md) handles the same need differently on PDS — a static pre-baked swap table (`nc_mapping_v0_seng_swap_pds`) with **no** runtime XOR — so the dynamic-XOR mechanism here is V3-base / V4-non-PDS only.

> **NOTE —** post-reset, V3 lazily probes whether the device's HBM supports the high-bandwidth "HBM-7200" performance profile: `perf_update_hbm_7200_supported_v3` (`:1729-1750`) calls `fw_io_get_available_profiles(FW_IO_AVAILABLE_PERF_PROFILES_HBM_7200, …)` and sets `nd->supports_hbm_7200 = 1` if **any** bit of the returned profile bitmap is set, else `0`; a fw_io error also yields `0` (`:1735-1737`). This is one of the slots [dhal-v4](dhal-v4.md) overrides — Trn3 hardcodes `supports_hbm_7200 = 0` with no firmware query at all, because the HBM-7200 profile is not advertised on Trn3. A reimplementer must keep the V3 firmware probe and the V4 hardcode as distinct bodies behind the one `perf_update_hbm_7200_supported` slot.

---

## Cross-References

- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the `struct neuron_dhal` container this registrar fills, the unconditional `vc` base, and the `vc → v3 → v4` three-layer compose that makes this registrar both the Trn2 leaf and the V4 base
- [DHAL V2 (Trn1 / Inf2)](dhal-v2.md) — the peer leaf: the shared FW_STATUS poll structure and the `V2_FW_IO_REG_*` macros V3 reuses; contrast the 2-NC single-engine H2T model with V3's shared-engine `nc%2` sharing
- [DHAL V4 (Trn3)](dhal-v4.md) — the override layer stacked on this V3 base; the 8+1 slots it replaces (HBM 24→36 GiB, hardcoded `supports_hbm_7200=0`, static PDS NC-swap with no die-flip XOR)
- [Reset Engine](reset.md) — the arch-neutral worker that drives `nr_initiate_reset` / `nr_wait_for_reset_completion` / `nr_post_reset_config`, `nr_initiate_reset_via_fw` (the real reset trigger V3 hands its lo/hi bitmap to), and the `reset_top_dma` / `no_reset` module params
- [Pod Election (UltraServer)](pod-election.md) — `npe_init`, `npe_election_exec_on_rst`, and `npe_get_pod_status` (the pod state + node_id source the die-flip predicate reads), plus the `npe_*_v3` STD/PDS/UltraServer branches
