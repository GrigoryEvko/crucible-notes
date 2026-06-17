# DHAL V2 (Trn1 / Inf2)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. This page owns the V2 arch-ops cell — `v2/neuron_dhal_v2.c` (1522 lines), `v2/address_map.h` (199 lines), `v2/sdma.h` (105 lines) — all read in full; every register offset, mask, and constant below is transcribed verbatim. The vtable container it fills (`struct neuron_dhal`, `neuron_dhal.h`) is owned by [dhal-core](dhal-core.md); the worker that drives the reset slots is owned by [reset](reset.md). Other driver versions renumber these lines.*
> *Evidence grade: **Confirmed (source-anchored)** — the registrar, the sub-dispatch, and every callback body are direct C, not reverse-engineered. · Part III — Kernel Driver, DEEP · [back to overview](overview.md)*

## Abstract

V2 is the DHAL leaf for the first Neuron generation the driver supports: **Trainium 1** (PCI device `0x7164`) and **Inferentia 2** (PCI device `0x7264`). It is one of three arch leaves below [dhal-core](dhal-core.md)'s `vc → arch` compose, and it is the simplest of the three — V3 (Trn2) is a peer leaf, V4 (Trn3) is a thin override layer stacked on V3, but V2 is a single self-contained registrar with no layering above it. Its entry point `ndhal_register_funcs_v2` (`v2/neuron_dhal_v2.c:1377`) fills every scalar field and ~50 function-pointer slots of the global `ndhal` with V2 implementations, branches once on platform (qemu / emulator / real silicon) to swap the reset callbacks and `apb_bar`, then sub-dispatches once on `pci_device_id` to set the four sysfs name suffixes that distinguish Trn1 from Inf2 (`ndhal_register_funcs_trn1` `:92` / `ndhal_register_funcs_inf2` `:104`).

The defining V2 fact is that **Trn1 and Inf2 share one vtable**. The two silicon parts run byte-identical address maps, reset sequences, DMA bring-up, and HBM geometry; the entire Trn1/Inf2 divergence in the source is three things — (a) the sysfs suffix strings, (b) a PCI routing-id → user-id renumber table applied *only* on Trn1 (`v2_routing_id_to_user_id[]` `:846`, consumed `:894`), and (c) the FWIO neighbor tables, which are keyed on instance type (`trn1_32xl` / `inf2_48xl` / `inf2_24xl`, `:576-617`) because V2 silicon cannot self-detect torus neighbors (`:633-634`). Everything else — the `bar0+0x30fa0808` reset-status poll, the SDMA ROB/WOB/event-accel bring-up, the BAR4 HBM remap — is one code path for both parts.

Two V2 hardware bugs are worked around in this cell, and a reimplementer who omits either ships a driver that hangs or corrupts. **BUG-A**: a DMA started inside a NeuronCore-reset window can hang, so V2 sets `ndma_retry_memcpy = true` (`:1444`), which arms a rebuild-and-retry loop in the DMA layer (`neuron_dma.c:378`) and, as a side effect, disables zerocopy on V2. **BUG-B**: the UDMA descriptor prefetcher mis-fires when its threshold equals the 128-entry prefetch buffer size, so V2 caps descriptors-per-packet at 64 (`V2_ALLOWED_DESC_PER_PACKET`, `address_map.h:65-71`) and passes `64+1=65` to engine init (`:1097`). The page proceeds: the registrar and its two-axis sub-dispatch; the reset initiate + wait-poll bodies as annotated pseudocode; the `ndma_init` SDMA register sequence; the slot/Function Map table; then the BUG-A and BUG-B callouts.

For reimplementation, the contract is:

- **The single registrar and its two sub-dispatch axes** — `ndhal_register_funcs_v2` fills all slots unconditionally, then a *platform* branch (qemu/emu/real) swaps the reset fns + `apb_bar`, then a *device-id* switch sets only the four sysfs suffixes. The order is fixed: scalars → slots → platform → device-id → `dev_nc_map` sanity.
- **The reset initiate + wait-poll bodies** — initiate builds a TPB+TopSP reset bitmap and hands it to the FW handshake (no MMIO on real HW); wait polls FW_STATUS at `bar0+0x30fa0808`, masks `DEVICE_READY (0x8)`, and backs off `100 ms × i` across 5 tries.
- **The `ndma_init` register sequence** — per-engine SDMA broadcast config, then UDMA engine init with the 65-descriptor BUG-B guard, then the three SDMA register writes (ROB←1, WOB←1, EVENT_ACCEL←1), then arm AXI-error abort.
- **The two HW-bug workarounds** — `ndma_retry_memcpy = true` (BUG-A) and the 64-cap descriptor threshold (BUG-B), and the constants that encode each.

| | |
|---|---|
| **Owns** | `v2/neuron_dhal_v2.c` (1522 ln) · `v2/address_map.h` (199 ln) · `v2/sdma.h` (105 ln), all read in full |
| **Registrar** | `ndhal_register_funcs_v2` (`:1377-1522`) — fills all slots, branches platform, sub-dispatches device-id |
| **Device-id sub-dispatch** | `:1496-1514` — `TRN1_DEVICE_ID0 (0x7164)` → `ndhal_register_funcs_trn1` (`:92`); `INF2_DEVICE_ID0 (0x7264)` → `_inf2` (`:104`); else `-EINVAL` |
| **Platform branch** | `:1473-1494` — qemu (apb_bar=2, qemu reset fns) · emu (retry×1000, apb_bar=0) · real (apb_bar=0) |
| **Hardware shape** | 1 die, 2 NeuronCores, 2 HBM (16 GiB each = 32 GiB), 6 TopSP, 5 TPB eng/NC, 16 DMA eng/NC + 2 top-level = 34 DMA eng/dev, 16 q/eng (`address_map.h:38-63`) |
| **Reset status poll** | `bar0 + 0x30fa0808` (`IOFAB 0xe00000 + MISC_RAM 0x1a0000 + FW_STATUS 0x808`); ready mask `0x8`; backoff `100 ms × i`, 5 tries (`:188-208`) |
| **BUG-A workaround** | `ndma_retry_memcpy = true` (`:1444`) — DMA-in-reset retry; disables zerocopy on V2 |
| **BUG-B workaround** | `V2_ALLOWED_DESC_PER_PACKET = 64` (`address_map.h:71`); `ndma_init` passes `65` (`:1097`) |
| **Reset deadline / retry** | `initiate_max_wait_time = 1000*120 = 120000 ms` (`:28,:1401`) · `retry_count = NR_RESET_RETRY_COUNT (5)` (`:1402`) |
| **UDMA scalars** | `num_queues = DMA_MAX_Q_V4 (16)` (`:1442`) · `num_beats = 1024` (`:1443`) |

---

## 1. The Registrar and the Two-Axis Sub-Dispatch

### Purpose

`ndhal_register_funcs_v2` (`v2/neuron_dhal_v2.c:1377-1522`) is the only exported function of the cell. [dhal-core](dhal-core.md)'s `neuron_dhal_init` calls it once for the host after the unconditional `vc` base, when the latched arch is `NEURON_ARCH_V2` (`neuron_dhal.c:38`). It does straight-line pointer assignment into the live global — no allocation, no failure path except the unknown-device-id default — so it is structurally close to infallible, returning `-EINVAL` only when `ndhal` is NULL (`:1380`), the device-id is neither Trn1 nor Inf2 (`:1511-1513`), or the `dev_nc_map` sanity check trips (`:1516-1518`).

The function has three phases in fixed order: **(1)** fill the `ndhal_address_map` scalars and every vtable slot with the V2 implementation (`:1385-1471`), **(2)** a *platform* branch that swaps the two reset callbacks, the DMA-engine counts, and `apb_bar` for qemu / emulator / real silicon (`:1473-1494`), and **(3)** a *device-id* switch that calls `ndhal_register_funcs_trn1` or `ndhal_register_funcs_inf2` to set only the sysfs suffix strings (`:1496-1514`). The two sub-dispatch axes are independent: platform decides *how* the device is reached (which BAR is APB, whether the reset doorbell is real or simulated), device-id decides only *what it is called* in sysfs.

### Entry Point

```text
neuron_dhal_init (neuron_dhal.c:10)            [dhal-core]
  └─ ndhal_register_funcs_vc()        (:34)    ── version-common base (4 tpb slots)
  └─ ndhal_register_funcs_v2()        (:38)    ── THIS cell  (v2/neuron_dhal_v2.c:1377)
       ├─ fill address_map scalars             ── :1385-1400
       ├─ fill ~50 vtable slots                ── :1403-1471
       ├─ platform branch  qemu | emu | real   ── :1473-1494   (reset fns, apb_bar, dma counts)
       └─ device-id switch                     ── :1496-1514
            ├─ 0x7164 → ndhal_register_funcs_trn1 (:92)   ── sysfs suffix = "Trn1"/"Trainium1"
            └─ 0x7264 → ndhal_register_funcs_inf2 (:104)  ── sysfs suffix = "Inf2"/"Inferentia2"
       └─ dev_nc_map sanity: dev_nc_map < (1<<nc_per_device) ── :1516-1518
```

### Algorithm

The two-axis sub-dispatch tail, modelling `ndhal_register_funcs_v2` (`:1472-1519`):

```c
function ndhal_register_funcs_v2():                       // v2/neuron_dhal_v2.c:1377
    if (ndhal == NULL): return -EINVAL                     // :1380
    fill_address_map_scalars()                             // :1385-1400  pci_host_base, nc_per_device=2, ...
    fill_all_vtable_slots()                                // :1403-1471  reset/topsp/nc/nq/.../tpb/ext_cleanup

    // ── AXIS 1: PLATFORM — swap reset fns + apb_bar + DMA-engine counts ──────────
    if (narch_is_qemu()):                                  // :1473
        ndhal_reset.nr_initiate_reset            = nr_initiate_reset_v2_qemu            // :1474
        ndhal_reset.nr_wait_for_reset_completion = nr_wait_for_reset_completion_v2_qemu // :1475
        ndhal_pci.apb_bar = 2                              // :1478   APB on BAR2 under qemu
    else if (narch_is_emu()):                              // :1479
        ndhal_reset.retry_count *= 1000                    // :1480   wait far longer on emulator (5000 tries)
        ndhal_reset.nr_initiate_reset            = nr_initiate_reset_v2_emu            // :1481  (== real)
        ndhal_reset.nr_wait_for_reset_completion = nr_wait_for_reset_completion_v2_emu // :1482 (== real)
        ndhal_address_map.nc_per_device = nc_per_dev_param // :1485   emulator overrides NC count
        ndhal_address_map.dev_nc_map    = dev_nc_map       // :1486
        ndhal_pci.apb_bar = 0                              // :1487
    else:                                                  // :1488   real silicon
        ndhal_reset.nr_initiate_reset            = nr_initiate_reset_v2                // :1489
        ndhal_reset.nr_wait_for_reset_completion = nr_wait_for_reset_completion_v2     // :1490
        ndhal_pci.apb_bar = 0                              // :1493

    // ── AXIS 2: DEVICE-ID — set only the four sysfs suffix strings ───────────────
    switch (ndhal->pci_device_id):                         // :1496
        case TRN1_DEVICE_ID0 (0x7164):                     // :1497
            ret = ndhal_register_funcs_trn1()              // :1498  nd="v2" nc="v2" inst="Trn1" name="Trainium1"
        case INF2_DEVICE_ID0 (0x7264):                     // :1504
            ret = ndhal_register_funcs_inf2()              // :1505  nd="v3" nc="v2" inst="Inf2" name="Inferentia2"
        default:                                           // :1511
            pr_err("Unknown HW architecture. Can't init neuron_dhal.")
            return -EINVAL                                 // :1513

    // ── SANITY: the configured core bitmap must fit the core count ──────────────
    if (dev_nc_map >= (1 << nc_per_device)):               // :1516  note: >=, reject if it does NOT fit
        pr_err("Invalid nc map for device"); return -EINVAL // :1518
    return ret                                             // :1521
```

### Function Map

The complete V2 slot fill. Slot names are the `ndhal_<area>.<member>` the registrar assigns; `Body` is the implementation definition line, `Reg` the assignment line. Confidence is HIGH throughout — open GPL source, no decompilation.

| Slot (`ndhal_…`) | V2 impl fn | Body | Reg | Conf |
|---|---|---|---|---|
| `reset.nr_post_reset_config` | `nr_post_reset_config_v2` (sets `supports_hbm_7200=0`) | `:242` | `:1403` | HIGH |
| `reset.nr_initiate_reset` (real) | `nr_initiate_reset_v2` | `:143` | `:1489` | HIGH |
| `reset.nr_wait_for_reset_completion` (real) | `nr_wait_for_reset_completion_v2` | `:188` | `:1490` | HIGH |
| `reset.{initiate_max_wait_time,retry_count}` | `120000 ms` / `NR_RESET_RETRY_COUNT (5)` | `:28` | `:1401-1402` | HIGH |
| `topsp.ts_nq_{init,destroy_one,get_nqid,set_hwaddr}` | `ts_nq_*_v2` | `:308,:355,:258,:275` | `:1404-1407` | HIGH |
| `nc.{nc_get_semaphore_base,nc_get_event_addr}` | `nc_get_*_v2` | `:377,:393` | `:1408-1409` | HIGH |
| `nq.{nnq_get_nqid,nnq_set_hwaddr}` | `nnq_*_v2` | `:412,:427` | `:1410-1411` | HIGH |
| `mpset.mpset_set_dram_and_mpset_info` | `mpset_set_dram_and_mpset_info_v2` | `:456` | `:1414` | HIGH |
| `ndmar.{get_h2t_eng_id,…,quiesce_queues}` (6) | `ndmar_*_v2` | `:479-542` | `:1415-1420` | HIGH |
| `fw_io.{topology,…,post_metric}` (5) | `fw_io_*_v2` | `:631-737` | `:1421-1425` | HIGH |
| `mmap.{dm_mmap_special,mmap_get_bar4_offset}` | `dm_mmap_special_v2[]` / `mmap_get_bar4_offset_v2` | `:30,:752` | `:1426-1427` | HIGH |
| `sysfs_metrics.{ecc,hbm_error,tensor_engine}` (3) | `nsysfsmetric_*_v2` | `:784,:805,:826` | `:1430-1432` | HIGH |
| `pci.{axi_bar,dram_bar}` | `BAR_UNUSED` / `4` | — | `:1433-1434` | HIGH |
| `pci.{get_device_id,device_id_to_rid_map}` | `neuron_pci_*_v2` | `:867,:918` | `:1435-1436` | HIGH |
| `cdev.{compatible_version,get_default_tpbs_for_hbm}` | `ncdev_*_v2` (compat 5..7) | `:965,:971` | `:1439,:1441` | HIGH |
| `cdev.ncdev_logical_to_physical_nc_map` | `NULL` (V2 has no NC remap) | — | `:1440` | HIGH |
| `udma.{num_queues,num_beats}` | `DMA_MAX_Q_V4 (16)` / `1024` | — | `:1442-1443` | HIGH |
| `ndma.ndma_retry_memcpy` | `true` (**BUG-A**) | — | `:1444` | HIGH |
| `ndma.{wait_for_completion_time,validate_pa,init,…}` (6) | `ndma_*_v2` | `:989-1190` | `:1445-1450` | HIGH |
| `npe.*` (7, all single-node stubs) | `npe_*_v2` | `:1210-1320` | `:1451-1457` | HIGH |
| `perf.*` (4, all NOP/default) | `perf_*_v2` | `:1263-1285` | `:1458-1461` | HIGH |
| `tpb.pe_{xbus,row_grp,col_grp}_count` | `5 / 4 / 4` | — | `:1462-1464` | HIGH |
| `tpb.pe_*_cntr_offsets` (4 tables) | `ntpb_pe_*_cntr_offsets_v2[2]` | `:68-90` | `:1466-1469` | HIGH |
| `tpb.pe_get_aggregated_wl_cycle_cnt` | `ntpb_pe_get_aggregated_wl_cycle_cnt_v2` | `:1335` | `:1470` | HIGH |
| `ext_cleanup` | `ndhal_ext_cleanup_v2` (empty) | `:1356` | `:1471` | HIGH |
| `sysfs_metrics.arch_*_suffix` | `ndhal_register_funcs_{trn1,inf2}` | `:92,:104` | `:1498,:1505` | HIGH |

> **GOTCHA —** the `dev_nc_map` sanity check at `:1516` reads `if (dev_nc_map >= (1 << nc_per_device)) return -EINVAL` — it rejects a map that does **not** fit the core count. With `nc_per_device = 2` (`:1393`) the legal range is `0..3` and the registrar pre-sets `dev_nc_map = (1<<2)-1 = 0x3` (`:1394`), so the gate passes on real silicon; it can only trip under the emulator path, which overwrites both `nc_per_device` and `dev_nc_map` from module params (`:1485-1486`). A reimplementation that copies the check but forgets the emulator overrides will see a spuriously-passing gate. The comparison is `>=`, not `<` — read it as "reject when the bitmap has a bit above the highest core."

> **QUIRK —** Inf2's `arch_nd_type_suffix` is `"v3"`, not `"v2"` (`:109`), while its `arch_nc_type_suffix` stays `"v2"` (`:110`). Trn1 uses `"v2"` for both (`:97-98`). The "nd type" suffix is a *sysfs node-naming* token, decoupled from `enum neuron_arch` (which is `V2=2` for both parts, owned by [generations-enum](../arch/generations-enum.md)). A reimplementer who derives the sysfs nd-type string from the arch enum will mislabel Inf2 nodes. The four suffixes are pure presentation strings; only these, the FWIO neighbor tables, and the Trn1 routing-id renumber differ between the two parts.

---

## 2. Reset — Initiate and Wait-Poll

### Purpose

The reset slots are driven by the arch-neutral worker in [reset](reset.md): stage 1 calls `ndhal_reset.nr_initiate_reset`, stage 2 calls `ndhal_reset.nr_wait_for_reset_completion`. V2 supplies three variants of each — real, qemu, emulator — selected by the platform branch (§1). On real silicon, **initiate does no MMIO of its own**: it builds the reset bitmap and hands it to `nr_initiate_reset_via_fw` (owned by [reset](reset.md)), which pokes BAR0 and polls the FW. **Wait** is the V2-specific poll loop that reads the FW_STATUS register and tests the `DEVICE_READY` bit.

### Algorithm

The reset-map builder plus the real-path initiate and wait, modelling `nr_get_tpb_reset_map` / `nr_initiate_reset_v2` / `nr_wait_for_reset_completion_v2` (`:118-208`):

```c
function nr_get_tpb_reset_map(nc_map, *tpb_reset_map):    // v2/neuron_dhal_v2.c:118
    if (nc_map == NEURON_NC_MAP_DEVICE): return            // :123  whole-device: leave map 0, FW handles it
    for i in [0 .. MAX_NC_PER_DEVICE(8)):                  // :124  loops to 7 though V2 has 2 NCs
        if (!((1 << i) & nc_map)): continue
        *tpb_reset_map |= (1 << i)                          // :127  TPB i → bit i (bits 0..7)
        ts_per_nc = V2_TS_PER_DEVICE / V2_NC_PER_DEVICE     // :128  = 6/2 = 3 TopSP per NC
        for j in [i*3 .. (i+1)*3):
            *tpb_reset_map |= (1 << (8 + j))                // :131  3 TopSPs → bits 8.. ;
                                                            //       NC0→TopSP{0,1,2}=bits 8,9,10
                                                            //       NC1→TopSP{3,4,5}=bits 11,12,13

function nr_initiate_reset_v2(nd, nc_map):                 // v2/neuron_dhal_v2.c:143  (real path)
    if (no_reset): return 0                                 // :145
    tpb_reset_map = 0
    nr_get_tpb_reset_map(nc_map, &tpb_reset_map)            // :149
    // NO MMIO here on real HW — the trigger is the FW handshake in [reset]; hi-word = 0.
    return nr_initiate_reset_via_fw(nd, nc_map, tpb_reset_map, 0)   // :151  BOUNDARY neuron_reset.c

function nr_wait_for_reset_completion_v2(nd):              // v2/neuron_dhal_v2.c:188  (real path)
    if (no_reset): return 0                                 // :190
    // FW_STATUS register, host-BAR0 byte offset 0x30fa0808 :
    //   0x30000000 (APB) + 0xe00000 (IOFAB) + 0x1a0000 (MISC_RAM) + 0x808 (FW_STATUS)
    addr = bar0 + V2_PCIE_BAR0_APB_OFFSET                   // 0x30000000
                + V2_APB_IOFAB_RELBASE                      // 0xe00000
                + V2_APB_IOFAB_MISC_RAM_RELBASE             // 0x1a0000
                + V2_FW_IO_REG_FW_STATUS_OFFSET             // 0x808          → 0x30fa0808   (:194)
    for i in [0 .. retry_count(5)):                        // :196  retry_count = NR_RESET_RETRY_COUNT
        reset_in_progress = true
        if (fw_io_read_csr_array(&addr, &status, 1, false) == 0):       // :200  readless CSR read
            reset_in_progress = status & V2_FW_IO_REG_FW_STATUS_DEVICE_READY_MASK  // :201  mask 0x8
        if (!reset_in_progress): return 0                  // :202  success ⇔ read OK and READY-bit clear
        if (nr_msleep_stoppable(nd, NR_RESET_RETRY_SLEEP_MS * i)): return -1  // :204  100ms*i backoff; i=0 → 0ms
    return -1                                              // :207  5 tries exhausted → timeout
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `nr_get_tpb_reset_map` | `:118-136` | build TPB(bits 0..7) + TopSP(bits 8..) reset bitmap from `nc_map`; hi-word unused | HIGH |
| `nr_initiate_reset_v2` | `:143-157` | real path: build map, hand to FW (no local MMIO) | HIGH |
| `nr_initiate_reset_v2_qemu` | `:159-176` | qemu: `writel(1, bar0+0x30647010)` doorbell, then FW call | HIGH |
| `nr_initiate_reset_v2_emu` | `:178-181` | tail-call `nr_initiate_reset_v2` (identical to real) | HIGH |
| `nr_wait_for_reset_completion_v2` | `:188-208` | poll `bar0+0x30fa0808`, mask `0x8`, `100ms*i` backoff, 5 tries | HIGH |
| `nr_wait_for_reset_completion_v2_qemu` | `:210-230` | poll `bar0+0x30647010` via `readl`, `msleep(2000)`/iter | HIGH |
| `nr_post_reset_config_v2` | `:242-246` | set `nd->supports_hbm_7200 = 0`, return 0 | HIGH |
| `nr_initiate_reset_via_fw` | `neuron_reset.c:381` | BAR0 trigger + poll (boundary, [reset](reset.md)) | HIGH |
| `fw_io_read_csr_array_v2` | `:696` | readless CSR read used by the wait loop | HIGH |

### Considerations

The wait loop's backoff is `100 ms × i` with `i` running `0..4`, so the first iteration sleeps **zero** (`i=0`) and the worst case is `0+100+200+300+400 = 1000 ms` of sleep across 5 reads — far below the `initiate_max_wait_time` of 120 s, which bounds the *initiate* FW handshake in [reset](reset.md), not this wait. On the emulator the platform branch multiplies `retry_count` by 1000 (`:1480`) so the wait tolerates 5000 reads; the qemu wait variant replaces the FW_STATUS read with a `readl` of the qemu doorbell at `bar0+0x30647010` and a flat `msleep(2000)` per iteration.

> **CORRECTION —** an earlier draft of this page gave the qemu reset doorbell as `bar0+0x30657010`; the correct address is `bar0+0x30647010`. It is built as `V2_PCIE_BAR0_APB_OFFSET (0x30000000) + V2_APB_SENG_0_RESERVED1_RELBASE (0x647000) + 0x10` (`neuron_dhal_v2.c:164,216`; `address_map.h:85,188`) → `0x30647010`. The earlier value was high by `0x10000`.

> **GOTCHA —** the wait loop's local is named `reset_in_progress` but is assigned the `DEVICE_READY` masked bit, and the loop returns success on `if (!reset_in_progress)` (`:201-203`). Read literally: it returns 0 when the CSR read **succeeds** and the `DEVICE_READY (0x8)` bit is **clear**. The variable name inverts the FW-status semantic — "device ready" reads as the *reset-done* condition, expressed here as the ready bit being clear at this poll site. The behavior is unambiguous from the code; only the name is confusing. A reimplementation should preserve the literal logic (`success ⇔ read_ok && !(status & 0x8)`), not the variable's English. *(MEDIUM on the human interpretation only; the code path is HIGH.)*

> **NOTE —** the FW_STATUS register constant is `V2_FW_IO_REG_FW_STATUS_OFFSET (0x808)` / `..._DEVICE_READY_MASK (0x8)` (`neuron_reset.h:24-25`). V3 reuses the **same `V2_`-named macros** for its own poll (`v3/neuron_dhal_v3.c:364,371`, marked FIXME there) — the constant is V2-named but generation-shared. The value `0x8` for DEVICE_READY is identical across arches, so the cross-naming is cosmetic. The *address* differs (V2 `0x30fa0808` vs the V3 MISC_RAM base), but the mask does not.

---

## 3. DMA Engine Init — `ndma_init_v2`

### Purpose

`ndma_init_v2` (`:1061-1112`) brings up one DMA engine: it resolves the engine's UDMA and SDMA base addresses (from one of the two static base tables, or the top-level H2D/D2H bases), configures the SDMA broadcast group, initializes the UDMA engine, writes the three SDMA reorder/event-accel enable registers, and arms AXI-error abort. It is the slot the DMA-rings bring-up calls per engine (`ndhal_ndma.ndma_init`, `:1447`). The engine index space is 0..33: `0..15` = NC0 engines, `16..31` = NC1 engines, `32` = D2H top-level, `33` = H2D top-level (`address_map.h:96-97`).

### Algorithm

The base resolution plus the SDMA bring-up sequence, modelling `ndma_init_v2` (`:1061-1112`) and the two `sdma.h` helpers it calls:

```c
function ndma_init_v2(bar0, udma, eng_id):                // v2/neuron_dhal_v2.c:1061
    d2h = (eng_id == V2_D2H_IDX(32));  h2d = (eng_id == V2_H2D_IDX(33))   // :1065-1066

    if (h2d || d2h):                                       // :1072  top-level engines
        udma_rel = (h2d ? V2_APB_H2D_UDMA_BASE(0xffff0d00000)
                        : V2_APB_D2H_UDMA_BASE(0xffff0600000)) - V2_APB_BASE   // :1073
        sdma_rel = (h2d ? V2_APB_H2D_SDMA_BASE(0xffff0d40000)
                        : V2_APB_D2H_SDMA_BASE(0xffff0640000)) - V2_APB_BASE   // :1074
        udma_base = bar0 + V2_PCIE_BAR0_APB_OFFSET(0x30000000) + udma_rel      // :1076
        sdma_base = bar0 + V2_PCIE_BAR0_APB_OFFSET          + sdma_rel         // :1077
        // top-level engines SKIP sdma_configure_broadcast
    else:                                                  // :1078  per-NC engines
        nc_id = eng_id / V2_DMA_ENG_PER_NC(16)             // :1079
        eid   = eng_id % V2_DMA_ENG_PER_NC                 // :1080
        udma_rel = seng_udma_base[nc_id][eid] - V2_APB_BASE   // :1082  static table @:1025
        sdma_rel = seng_sdma_base[nc_id][eid] - V2_APB_BASE   // :1083  static table @:1039
        udma_base = bar0 + 0x30000000 + udma_rel           // :1085
        sdma_base = bar0 + 0x30000000 + sdma_rel           // :1086
        ret = sdma_configure_broadcast(sdma_base, eid)     // :1088  (sdma.h:85)
        if (ret): goto done                                // :1089-1092

    // ── UDMA engine init — the 65-descriptor BUG-B guard ────────────────────────
    snprintf(udma_name, "UDMA_ENG_%d", eng_id)             // :1095
    ret = udma_m2m_init_engine(udma, udma_base, DMA_MAX_Q_MAX, udma_name, 0,
                               V2_ALLOWED_DESC_PER_PACKET + 1 /* =65 */, true)  // :1096-1097
    if (ret): goto done                                    //         "+1 to allow for MD descriptor"

    // ── SDMA register writes — ROB / WOB / EVENT_ACCEL enables (sdma.h:56-71) ────
    ret = sdma_init_engine(sdma_base):                     // :1102
        if (sdma_base == NULL): return -EFAULT             // sdma.h:58
        reg_write32(sdma_base + REG_SDMA_ROB_CFG_OFFSET     /*0x1c*/, 1)  // sdma.h:62  enable Read Reorder Buffer
        reg_write32(sdma_base + REG_SDMA_WOB_CFG_OFFSET     /*0x20*/, 1)  // sdma.h:65  enable Write Reorder Buffer
        reg_write32(sdma_base + REG_SDMA_EVENT_ACCEL_OFFSET /*0x0*/, 1)   // sdma.h:68  enable event acceleration
    if (ret): goto done                                    // :1103-1106

    udma_m2m_set_axi_error_abort(udma)                     // :1108  arm DMA abort-on-AXI-error
done:
    return ret                                             // :1111
```

`sdma_configure_broadcast` (`sdma.h:85-105`) writes the broadcast-group mask and the last-node flag for the per-NC engines. It clears the top `eid+1` bits of a `0xFFFF` mask (`mask &= ~(1 << (16 - i))` for `i in 0..eid`, `sdma.h:94-97`), sets `last_node = 0xFFFF` only for `eid ∈ {0,3,6,9,15}` (`sdma.h:98-99`), and writes both to `BROADCAST_CFG_GROUP (+0x100)` and `BROADCAST_CFG_LAST_NODE (+0x104)` (`sdma.h:101-102`).

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `ndma_init_v2` | `:1061-1112` | per-engine DMA bring-up: base resolve → bcast → UDMA init → SDMA regs → AXI abort | HIGH |
| `seng_udma_base[2][16]` / `seng_sdma_base[2][16]` | `:1025` / `:1039` | static per-NC engine APB base tables | HIGH |
| `sdma_init_engine` | `sdma.h:56-71` | writes ROB←1 (`0x1c`), WOB←1 (`0x20`), EVENT_ACCEL←1 (`0x0`) | HIGH |
| `sdma_configure_broadcast` | `sdma.h:85-105` | broadcast group mask + last-node flag (per-NC engines only) | HIGH |
| `udma_m2m_init_engine` | boundary (`udma/`) | UDMA engine init; receives the `65` BUG-B threshold | HIGH |
| `udma_m2m_set_axi_error_abort` | boundary (`udma/`) | arm DMA abort on AXI error | HIGH |

### Considerations

The SDMA register block sits at the engine's `sdma_base`, distinct from the UDMA register block at `udma_base`; the two come from separate base tables (`seng_sdma_base` and `seng_udma_base`, `:1025/:1039`). The three SDMA writes are unconditional `reg_write32(..., 1)` enables — there is no read-modify-write, so a reimplementer must not assume the reorder-buffer config registers carry other live bits that need preserving (the `sdma.h:15-39` bit-field comments document `force_inorder`, `clear`, `rid_base`, etc., but V2 writes a flat `1`, enabling only bit 0). The top-level H2D/D2H engines skip `sdma_configure_broadcast` entirely (`:1072-1077` has no broadcast call) — broadcast grouping is a per-NC-engine concept only.

---

## 4. The Two Hardware-Bug Workarounds

### BUG-A — DMA hang in the reset window

V2 silicon can **hang an in-flight DMA if a NeuronCore reset overlaps the transfer**. The workaround is a single flag set in the registrar — `ndhal->ndhal_ndma.ndma_retry_memcpy = true` (`:1444`) — whose loop body lives in the DMA layer (`neuron_dma.c:378`, boundary, owned by [dma-rings](dma-rings.md)). When an H2T memcpy wait times out *and* the operation overlaps a reset window (`nr_op_in_reset_wnd`, `neuron_reset.c:366`), the retry path re-initializes the H2T ring (`ndmar_h2t_ring_init`) and re-issues every descriptor (`ndma_memcpy_chunks`), looping until the copy succeeds, the op leaves the reset window, or a sub-call errors.

> **QUIRK —** setting `ndma_retry_memcpy = true` has a second, non-obvious consequence: it **disables zerocopy on V2**. `ndma_zerocopy_supported` (`neuron_dma.c:1375`) returns `!ndma_retry_memcpy || zerocopy_trn1_override`, so with retry on, zerocopy is off unless the `zerocopy_trn1_override` module param is set. A reimplementer who treats `ndma_retry_memcpy` as a pure correctness flag will silently lose the zerocopy H2T fast path on V2 and mis-attribute the resulting copy overhead. The flag couples a HW-bug retry to a capability gate — they are not independent.

### BUG-B — aggressive single-descriptor prefetch

The V2 UDMA prefetcher mis-fires when its **prefetch threshold equals the 128-entry prefetch buffer size**, "aggressively" prefetching a single descriptor in a way that triggers the bug (`address_map.h:65-71`, the source's own comment). The fix is to keep the threshold strictly below 128 by capping descriptors-per-packet at 64.

> **GOTCHA —** the cap is `V2_ALLOWED_DESC_PER_PACKET = 64` (`address_map.h:71`), but `ndma_init_v2` passes `V2_ALLOWED_DESC_PER_PACKET + 1 = 65` to `udma_m2m_init_engine` (`:1097`) — the `+1` "allows for an MD descriptor" (metadata), and 65 is still well under the 128-entry buffer that triggers the bug. A reimplementation that passes the bare `64`, or that passes `128`, gets either an off-by-one against the real driver or a re-triggered hardware fault. The threshold consumer is `udma_m2s_max_desc_set` in the UDMA core (boundary, [udma-m2m](udma-m2m.md)); the `address_map.h` comment explicitly warns against changing the `64` without consulting the named hardware owners. Both the `64` constant and the `+1` at the call site are distinct decisions and must both be reproduced.

---

## Cross-References

- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the `struct neuron_dhal` container this registrar fills, the unconditional `vc` base, and the `vc → arch` compose that places V2 as a leaf
- [DHAL V3 (Trn2)](dhal-v3.md) — the peer leaf: lo/hi reset bitmap, 480 s deadline, and the `V2_FW_IO_REG_*` FW_STATUS macros it reuses with a FIXME
- [DHAL V4 (Trn3)](dhal-v4.md) — the override layer stacked on V3; contrast with V2's single self-contained registrar
- [Reset State Machine](reset.md) — the arch-neutral worker that drives `nr_initiate_reset` / `nr_wait_for_reset_completion`, and `nr_initiate_reset_via_fw` (the real reset trigger V2 hands its bitmap to)
- [PCI Probe and Device Detection](pci-probe.md) — `neuron_pci_get_device_id_v2`, the Trn1-only routing-id renumber, and the `apb_bar`/`dram_bar` indices this registrar sets per platform
