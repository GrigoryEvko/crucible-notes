# DHAL V4 (Trn3)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. This page owns the V4 arch-ops cell — `v4/neuron_dhal_v4.c` (482 lines) and `v4/address_map.h` (229 lines), both read in full; every override, constant, and table row below is transcribed verbatim. The full V3 base it layers over is owned by [dhal-v3](dhal-v3.md); the vtable container is owned by [dhal-core](dhal-core.md); the reset worker by [reset](reset.md); the pod-election callees by [pod-election](pod-election.md). Other driver versions renumber these lines.*
> *Evidence grade: **Confirmed (source-anchored)** — the override registrar and every replaced body are direct C, not reverse-engineered. · Part III — Kernel Driver, DEEP · [back to overview](overview.md)*

## Abstract

V4 is the DHAL leaf for **Trainium 3** (PCI device `0x7564` / `0x7565`), and it is the thinnest leaf in the HAL — a **delta over V3, not a full vtable**. [dhal-core](dhal-core.md)'s `NEURON_ARCH_V4` case (`neuron_dhal.c:43-45`) calls `ndhal_register_funcs_v3()` first, which fills **every** scalar and function-pointer slot of the global `ndhal` with Trainium 2 (V3) implementations, and *then* calls `ndhal_register_funcs_v4()` (`v4/neuron_dhal_v4.c:430`), which replaces exactly **8 unconditional slots + 1 PDS-only slot + 4 sysfs suffix strings**. Everything the V4 registrar does not touch — the entire DMA data-plane, reset, notification queues, H2T engine sharing, TopSP, fw_io, the TPB perf counters, BAR layout, address-map counts — is the V3 body verbatim. The driver comment states the contract directly: *"This function only overrides the functions and constants that are different from v3 in v4"* (`:427-428`). A reimplementer should read this page as a patch set against [dhal-v3](dhal-v3.md), not as a standalone arch.

The single substantive **hardware** delta is HBM capacity: `V4_HBM_ACTIVE_SIZE = 0x900000000` (36 GiB per stack, 4 stacks) against V3's `0x600000000` (24 GiB) (`v4/address_map.h:92` vs `v3/address_map.h:92`). A literal diff of the two `address_map.h` files shows this is the **only value-level difference** — every other diff line is just the `V3_`→`V4_` macro-name prefix; the engine, NeuronCore, SENG, DMA, and TopSP counts are byte-identical (8 NC, 132 DMA engines, 16 TopSP, 4 HBM channels, 5 TPB eng/NC). Four of the 8 overrides exist solely to carry this 36 GiB number into the slots that touch HBM size — `mpset_set_dram_and_mpset_info`, `dm_mmap_special`, `mmap_get_bar4_offset`, and `ncdev_mem_regions`. The other four overrides are device-identity and platform-naming differences: the Trn3 platform-type classifier, the Trn3 device-id discovery (with a 1-device `3xl` shortcut), the relaid-out pod-election neighbor engine ids, and a hardcoded `supports_hbm_7200 = 0`.

The defining V4 quirk is in the **conditional** override. On a PDS (Phoenix-Discovery-Server) platform, V4 installs a *static, pre-baked* logical→physical NeuronCore swap table (`nc_mapping_v0_seng_swap_pds[128]`, `:375`) with **no runtime die-flip** — it never calls `ndhal_die_flipped` and never XORs the core index. This is the sharp contrast with V3, whose [die-flip](dhal-v3.md#5-the-die-flip-a-software-nc-remap-not-a-register) applies a runtime `XOR 0x6` to one base table for UltraServer node 1/3. On PDS the flip is *baked in per device* rather than computed; on non-PDS V4 the V3 dynamic-XOR map is inherited unchanged. This page proceeds: the piggyback init order and the override registrar as annotated pseudocode; the complete override table; two representative override bodies (the neighbor engine ids and the PDS static NC-swap); then the thin-delta and no-die-flip callouts.

For reimplementation, the contract is:

- **The piggyback init order** — `ndhal_register_funcs_v3()` must run first and fill the whole vtable; `ndhal_register_funcs_v4()` then overwrites a fixed, small set of slots. Swap the order and every V4 override is clobbered by the V3 base ([dhal-core §2](dhal-core.md#2-init-and-arch-dispatch--neuron_dhal_init)).
- **The exact override set** — 8 unconditional slots (`:438-445`), 1 PDS-only slot (`:464`), and the 4 sysfs suffixes (`:143-146`, dispatched on device-id at `:467-475`). Nothing else is touched; everything else resolves to the V3 body.
- **The 36 GiB HBM constant and its four carrier slots** — `V4_HBM_ACTIVE_SIZE = 0x900000000` is the sole hardware-value change, threaded through the four HBM-size-touching overrides; the bases are unchanged.
- **The PDS static NC-swap, contrasted with V3's runtime die-flip** — V4-PDS bakes the swap into a per-device table with no XOR; V4-non-PDS inherits V3's dynamic-XOR map. A reimplementer must keep both behaviors behind the one `ncdev_logical_to_physical_nc_map` slot.

| | |
|---|---|
| **Owns** | `v4/neuron_dhal_v4.c` (482 ln) · `v4/address_map.h` (229 ln), both read in full |
| **Override registrar** | `ndhal_register_funcs_v4` (`:430-482`) — runs **after** `ndhal_register_funcs_v3` (`neuron_dhal.c:44-45`); replaces 8 + 1 slots + 4 suffixes |
| **Device ids** | `TRN3_DEVICE_ID0 (0x7564)`, `TRN3_DEVICE_ID1 (0x7565)` (`neuron_device.h:39-40`); both → `arch = NEURON_ARCH_V4` |
| **Sole HW delta** | `V4_HBM_ACTIVE_SIZE = 0x900000000` (36 GiB/stack) vs V3 `0x600000000` (24 GiB) (`address_map.h:92`) — *only* value-level diff vs V3 map |
| **Hardware shape** | 2 die, 4 SENG, 8 NeuronCores, 4 HBM (36 GiB each = 144 GiB usable), 16 TopSP, 5 TPB eng/NC, 132 DMA eng/dev (`address_map.h:45-92`) — counts == V3 |
| **8 unconditional overrides** | `arch.platform_type` · `pci.get_device_id` · `npe.neighbor_eng_ids` · `mpset.set_dram_and_mpset_info` · `mmap.dm_mmap_special` · `mmap.get_bar4_offset` · `cdev.mem_regions` · `perf.update_hbm_7200_supported` (`:438-445`) |
| **PDS-only override** | `cdev.ncdev_logical_to_physical_nc_map` ← `..._v4` static table, **no die-flip XOR** (`:464`, gated `platform == PDS`) |
| **sysfs suffixes** | `arch_nd_type="v4"` · `arch_nc_type="v4"` · `arch_instance="Trn3"` · `arch_device_name="Trainium3"` (`:143-146`) |
| **Platform classifier** | `trn3s.48xlarge` / `trn3-dev0.48xlarge` → PDS · `trn3p.48xlarge` → ULTRASERVER · else STD (`:152-170`); `trn3pd98.3xlarge` → 3xl shortcut (`:179`) |
| **Neighbor eng ids** | `{{40,72} L, {8,104} R}` (`:132-136`) vs V3 `{{36,68},{4,100}}` (`v3:214-218`) |
| **Emu side-effect** | `narch_is_emu()` → `no_reset = 1` (`:447-451`), disable resets on emulation |

---

## 1. The Piggyback Registrar

### Purpose

`ndhal_register_funcs_v4` (`v4/neuron_dhal_v4.c:430-482`) is the only exported function of the cell, and unlike the V2/V3 registrars it is **not** a full vtable fill. By the time it runs, [dhal-core](dhal-core.md) has already executed `ndhal_register_funcs_v3()` against the same global (`neuron_dhal.c:44`), so every slot already holds a working Trn2 implementation. The V4 registrar's job is to *selectively re-point* the slots whose behavior differs on Trn3, leaving the rest as V3. It returns `-EINVAL` only when `ndhal` is NULL (`:433-436`), when pod-election init fails on an UltraServer (`:457-461`), or when the `pci_device_id` is not one of the two Trn3 ids (`:476-478`).

The function has four phases in fixed order: **(1)** install the 8 unconditional overrides by plain assignment (`:438-445`); **(2)** an *emulation* side-effect that sets the global `no_reset` flag (`:447-451`); **(3)** a *platform* branch — `npe_init()` for UltraServer, or the PDS-only NC-swap slot for PDS (`:456-465`); and **(4)** a *device-id* switch that installs the four sysfs suffix strings for the two Trn3 ids, rejecting anything else (`:467-479`). Because the V3 base already ran, the platform-type the branch reads at `:456`/`:462` is the **V4** value just installed at `:438`, not V3's.

> **QUIRK —** V4 is a **thin delta, not an arch.** Reading `ndhal_register_funcs_v4` in isolation is misleading: it assigns only ~13 slots, but the live vtable has 56 callbacks plus scalars — the other ~43 callbacks and all the address-map scalars are V3 bodies installed moments earlier by `ndhal_register_funcs_v3()`. A claim like "V4's reset path" or "V4's H2T sharing" is shorthand for "the V3 reset / H2T body, unchanged." The only slots this page documents as *new code* are the ones in the override table (§2). Everything else is owned by [dhal-v3](dhal-v3.md), and a reimplementer must build the V3 vtable in full before applying this patch.

### Entry Point

```text
neuron_dhal_init (neuron_dhal.c:10)              [dhal-core]
  └─ ndhal_register_funcs_vc()        (:34)      ── version-common base (4 tpb slots)
  └─ case NEURON_ARCH_V4:             (:43)
       ├─ ndhal_register_funcs_v3()   (:44)      ── V3 BASE — fills ALL slots with Trn2 ops  (ret discarded, see GOTCHA)
       └─ ndhal_register_funcs_v4()   (:45)      ── THIS cell (v4/neuron_dhal_v4.c:430) — patches 8 + 1 slots
            ├─ platform_type = ndhal_platform_type_v4()  ── :438   (Trn3 SKU strncmp → PDS|ULTRASERVER|STD)
            ├─ install 8 unconditional overrides          ── :439-445
            ├─ if narch_is_emu() → no_reset = 1            ── :447-451
            ├─ if ULTRASERVER → npe_init()                 ── :456-461  BOUNDARY [pod-election]
            ├─ else if PDS → install nc_map_v4 (no flip)   ── :462-465  PDS-only slot
            └─ switch pci_device_id                        ── :467-479
                 ├─ 0x7564 / 0x7565 → ndhal_register_funcs_trn3 (:138)  ── sysfs = "Trn3"/"Trainium3"
                 └─ default → -EINVAL                      ── :476-478
  └─ ncdev_class_attr_init()          (:54)       ── needs platform data the registrars installed
```

### Algorithm

The override registrar, modelling `ndhal_register_funcs_v4` (`:430-482`):

```c
function ndhal_register_funcs_v4():                          // v4/neuron_dhal_v4.c:430
    if (ndhal == NULL): return -EINVAL                       // :433-436

    // ── PHASE 1: 8 UNCONDITIONAL OVERRIDES — plain re-assignment over the V3 base ──
    ndhal_arch.platform_type            = ndhal_platform_type_v4()        // :438  VALUE, not fn-ptr (Trn3 SKUs)
    ndhal_pci.neuron_pci_get_device_id  = neuron_pci_get_device_id_v4     // :439  +3xl shortcut
    ndhal_npe.npe_neighbor_eng_ids      = npe_neighbor_eng_ids_v4         // :440  {{40,72},{8,104}}
    ndhal_mpset.mpset_set_dram_and_mpset_info = mpset_set_dram_and_mpset_info_v4  // :441  36 GiB
    ndhal_mmap.dm_mmap_special          = dm_mmap_special_v4              // :442  HBM rows = 36 GiB
    ndhal_mmap.mmap_get_bar4_offset     = mmap_get_bar4_offset_v4         // :443  36 GiB bounds
    ndhal_cdev.ncdev_mem_regions        = ncdev_mem_regions_v4            // :444  HBM rows = 36 GiB
    ndhal_perf.perf_update_hbm_7200_supported = perf_update_hbm_7200_supported_v4 // :445  hardcode 0

    // ── PHASE 2: EMU SIDE-EFFECT — not a slot ─────────────────────────────────────
    if (narch_is_emu()):                                     // :447
        extern int no_reset; no_reset = 1                    // :449-450  disable resets on emulation

    // ── PHASE 3: PLATFORM BRANCH — reads the platform_type JUST set at :438 ────────
    // TODO (driver): "V4 is piggybacking on V3 which risks double calling any hal init" (:453-454)
    if (platform_type == NEURON_PLATFORM_TYPE_ULTRASERVER):  // :456
        ret = npe_init()                                     // :457  BOUNDARY [pod-election]
        if (ret): pr_err("failed ... pod election on V4"); return ret    // :458-461
    else if (platform_type == NEURON_PLATFORM_TYPE_PDS):     // :462
        // PDS-ONLY +1 OVERRIDE: static swap table, NO runtime die-flip
        ndhal_cdev.ncdev_logical_to_physical_nc_map = ncdev_logical_to_physical_nc_map_v4  // :464

    // ── PHASE 4: DEVICE-ID — install only the four sysfs suffix strings ───────────
    switch (ndhal->pci_device_id):                           // :467
        case TRN3_DEVICE_ID0 (0x7564):
        case TRN3_DEVICE_ID1 (0x7565):                       // :468-469
            ret = ndhal_register_funcs_trn3()                // :470  "v4"/"v4"/"Trn3"/"Trainium3"
            if (ret): pr_err("...trn3"); return ret          // :471-474
        default:                                             // :476
            pr_err("Unknown HW architecture. Can't init neuron_dhal."); return -EINVAL  // :477-478

    return ret                                               // :481
```

`ndhal_register_funcs_trn3` (`:138-148`) is a 4-line suffix setter: it null-checks `ndhal`, then writes the four sysfs name strings (`arch_nd_type_suffix`, `arch_nc_type_suffix`, `arch_instance_suffix`, `arch_device_name_suffix`) directly into `ndhal_sysfs_metrics`. These replace V3's `"v3"/"v3"/"Trn2"/"Trainium2"` strings that the base registrar set.

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `ndhal_register_funcs_v4` | `:430-482` | override installer: 8 slots → emu flag → platform branch (+1 PDS slot) → device-id suffixes | HIGH |
| `ndhal_register_funcs_trn3` | `:138-148` | set 4 sysfs suffix strings (`"v4"/"v4"/"Trn3"/"Trainium3"`) | HIGH |
| `ndhal_platform_type_v4` | `:156-174` | Trn3 SKU `strncmp` → PDS / ULTRASERVER / STD | HIGH |
| `ndhal_instance_type_3xl` | `:176-188` | detect `trn3pd98.3xlarge` (1-device SKU); helper, not a slot | HIGH |
| `neuron_pci_get_device_id_v4` | `:311-372` | routing-id poll + PDS server-id + dedup; 3xl shortcut rid=0 | HIGH |
| `neuron_pci_routing_id_to_user_id` | `:296-302` | routing-id → user-id via torus / PDS-identity table | HIGH |
| `mpset_set_dram_and_mpset_info_v4` | `:201-239` | program 4×HBM base + 36 GiB size; qemu dyn-size path | HIGH |
| `mmap_get_bar4_offset_v4` | `:251-266` | HBM addr → BAR4 offset, 4 stacks, 36 GiB bounds | HIGH |
| `ncdev_logical_to_physical_nc_map_v4` | `:397-417` | PDS static swap table, **no die-flip XOR** | HIGH |
| `perf_update_hbm_7200_supported_v4` | `:419-422` | hardcode `nd->supports_hbm_7200 = 0`, no fw_io query | HIGH |

> **GOTCHA —** the V4 path swallows the V3 registrar's return code. [dhal-core](dhal-core.md#2-init-and-arch-dispatch--neuron_dhal_init) writes `ret = ndhal_register_funcs_v3(); ret = ndhal_register_funcs_v4();` (`neuron_dhal.c:44-45`) — the second assignment overwrites the first, so a V3-base failure is silently lost and `neuron_dhal_init` can return success with a partially-built V3 floor under the V4 patch. The exposure is bounded because both registrars are straight-line pointer assignments with no allocation, but a reimplementation that adds fallible work to a registrar must check the V3 return before applying V4. Severity MEDIUM (correctness, not observed harmful); owned by [dhal-core](dhal-core.md), re-verified here at `neuron_dhal.c:44-45`.

---

## 2. The Override Table

V4 replaces exactly **8 unconditional slots + 1 PDS-only slot + 4 sysfs suffix strings**. Every other slot in the 56-callback vtable resolves to the V3 body documented in [dhal-v3](dhal-v3.md). The table below is the complete delta: each row names the slot, the V3 base behavior it had after `ndhal_register_funcs_v3()` ran, the V4 body that replaces it, and *why*. Confidence is HIGH throughout — open GPL source, no decompilation.

| Slot (`ndhal_…`) | V3 base behavior (inherited until overridden) | V4 override | Reg / body line | Conf |
|---|---|---|---|---|
| `arch.platform_type` (value) | Trn2 SKU `strncmp` → STD/ULTRASERVER/PDS (`v3:240`) | `ndhal_platform_type_v4()` — Trn3 SKU strings; no `force_userver`, no 3xl branch | `:438` / `:156-174` | HIGH |
| `pci.neuron_pci_get_device_id` | poll fw_io rid (20×@1s), PDS server-id ×2 range, dedup (`v3:1139`) | `neuron_pci_get_device_id_v4` — same logic **+ 3xl shortcut** (`rid=0`, skip poll) | `:439` / `:311-372` | HIGH |
| `npe.npe_neighbor_eng_ids` | `{{36,68},{4,100}}` (`v3:214-218`) | `npe_neighbor_eng_ids_v4 = {{40,72},{8,104}}` — Trn3 inter-die links relaid | `:440` / `:132-136` | HIGH |
| `mpset.mpset_set_dram_and_mpset_info` | 4×24 GiB (`V3_HBM_ACTIVE_SIZE`, `v3:639`) | `..._v4` — identical body, size = `V4_HBM_ACTIVE_SIZE` (36 GiB) | `:441` / `:201-239` | HIGH |
| `mmap.dm_mmap_special` | `dm_mmap_special_v3[]`, HBM rows 24 GiB (`v3:51`) | `dm_mmap_special_v4[]` — same 57-entry layout, HBM rows 36 GiB | `:442` / `:28-89` | HIGH |
| `mmap.mmap_get_bar4_offset` | 4-stack bounds, 24 GiB (`v3:966`) | `mmap_get_bar4_offset_v4` — same logic, 36 GiB bounds | `:443` / `:251-266` | HIGH |
| `cdev.ncdev_mem_regions` | whitelist, HBM rows 24 GiB (`v3:114`) | `ncdev_mem_regions_v4[]` — same regions, HBM rows 36 GiB | `:444` / `:91-129` | HIGH |
| `perf.perf_update_hbm_7200_supported` | fw_io query `FW_IO_AVAILABLE_PERF_PROFILES_HBM_7200`, set 1 if any bit (`v3:1729`) | `..._v4` — hardcodes `supports_hbm_7200 = 0`, **no query** | `:445` / `:419-422` | HIGH |
| `cdev.ncdev_logical_to_physical_nc_map` **(PDS only)** | base seng-swap table + runtime `XOR 0x6` die-flip (`v3:1560`) | `..._v4` — **static** `nc_mapping_v0_seng_swap_pds[128]`, **no XOR** | `:464` / `:397-417` | HIGH |
| `sysfs_metrics.arch_nd_type_suffix` | `"v3"` (`v3:225`) | `"v4"` | `:143` | HIGH |
| `sysfs_metrics.arch_nc_type_suffix` | `"v3"` | `"v4"` | `:144` | HIGH |
| `sysfs_metrics.arch_instance_suffix` | `"Trn2"` | `"Trn3"` | `:145` | HIGH |
| `sysfs_metrics.arch_device_name_suffix` | `"Trainium2"` | `"Trainium3"` | `:146` | HIGH |
| *— every other slot —* | reset · ndma/ndmar data-plane · topsp · nq · nc · fw_io · tpb (+vc PE counters) · udma scalars · BAR indices · address-map counts | **inherited from V3 verbatim** | — | HIGH |

> **NOTE —** four of the eight unconditional overrides (`mpset`, `dm_mmap_special`, `mmap_get_bar4_offset`, `ncdev_mem_regions`) carry **the same** 36 GiB constant `V4_HBM_ACTIVE_SIZE = 0x900000000` and differ from their V3 bodies *only* in that constant and the `V3_`→`V4_` macro prefixes. The HBM bases (`V4_HBM_0..3_BASE`) and the per-stack window `V4_HBM_SIZE = 0x1000000000` (64 GiB) are identical to V3. A reimplementer can produce all four V4 bodies mechanically from the V3 sources by substituting the active-size macro; only the four *non-HBM* overrides (platform-type, device-id, neighbor ids, hbm-7200) carry genuinely new logic.

---

## 3. Representative Override Bodies

The 36 GiB-carrier overrides are mechanical (§2 NOTE). The two overrides worth reading in full are the **neighbor engine ids** (a data-table swap that re-routes pod-election neighbor discovery) and the **PDS static NC-swap** (the conditional override and the V3-die-flip contrast).

### Neighbor Engine IDs

`npe_neighbor_eng_ids_v4` (`:132-136`) is a `u32[2][2]` consumed by the pod-election layer ([pod-election](pod-election.md)) to find the DMA engines that link a device to its left/right neighbor across the inter-die link. Trn3 relaid out those link engines, so the ids differ from V3:

```c
u32 npe_neighbor_eng_ids_v4[2][2] =          // v4/neuron_dhal_v4.c:132-136
{
    { 40,  72 },   // Left   (V3 was {36, 68}, v3:216)
    {  8, 104 }    // Right  (V3 was { 4,100}, v3:217)
};
```

The slot assignment is a single pointer write (`:440`): `ndhal->ndhal_npe.npe_neighbor_eng_ids = npe_neighbor_eng_ids_v4`. Note this member is declared `u32 (*)[2]` — a pointer-to-array-of-2, not a function pointer (the off-by-one in the [dhal-core callback tally](dhal-core.md#callback-tally)). Each engine id shifts by `+4` from V3 (`36→40`, `68→72`, `4→8`, `100→104`), consistent with Trn3 inserting four engines ahead of the neighbor-link block; that shift is the entire content of the override.

### The PDS Static NC-Swap — No Die-Flip

The conditional `+1` override (`:464`, installed only when `platform_type == PDS`) is the most consequential V4 delta. Where V3's `ncdev_logical_to_physical_nc_map_v3` reads **one** base seng-swap table and applies a runtime `XOR 0x6` to the core index for UltraServer node 1/3 ([dhal-v3 §5](dhal-v3.md#5-the-die-flip-a-software-nc-remap-not-a-register)), the V4 PDS body reads a **pre-baked** table and never flips:

```c
function ncdev_logical_to_physical_nc_map_v4(map, max_num_entries, version):  // v4/neuron_dhal_v4.c:397
    if (version != NEURON_IOCTL_NC_MAPPING_TYPE_V0): return -EINVAL            // :403-406  only V0
    entries_to_copy = min(max_num_entries, NC_MAPPING_MAX_CORE_COUNT_V4(128))  // :400
    mapping = nc_mapping_v0_seng_swap_pds                                      // :407  STATIC table — NO die-flip
    for entry_idx in [0 .. entries_to_copy):                                   // :409
        core_idx = entry_idx                                                   // :410  no XOR 0x6 (contrast v3:1576)
        WARN_ONCE(core_idx >= 128, ...)                                        // :411
        map->mappings[entry_idx] = mapping[core_idx]                           // :412  straight index, no flip
    map->num_entries = entries_to_copy                                         // :414
    return 0                                                                   // :416
```

The table `nc_mapping_v0_seng_swap_pds[128]` (`:375-392`) is 16 devices × 8 cores, pinned to 128 by a `static_assert` (`:394-395`). Its rows alternate by device parity: even devices use `{4,5,6,7,2,3,0,1}` and odd devices `{6,7,4,5,0,1,2,3}` — the per-device swap is **already encoded** in the table content, so no runtime flip is needed. This differs from V3's base table content too: V3's ND1 row is `{2,3,0,1,4,5,6,7}` against V4-PDS ND1's `{6,7,4,5,0,1,2,3}` (`v3:1520` vs `v4:377`).

> **QUIRK —** on PDS the die-flip is **baked into the table, not computed at runtime.** A reimplementer porting V3's nc-map logic to V4 will look for the `XOR 0x6` and the `ndhal_die_flipped()` call — there is none in `ncdev_logical_to_physical_nc_map_v4`. The flip is folded into the alternating row layout of `nc_mapping_v0_seng_swap_pds`. Crucially this override is installed **only** on PDS (`:462-464`); on non-PDS V4 the slot retains the V3 body with its dynamic XOR. So the one `ncdev_logical_to_physical_nc_map` slot resolves to *two different mechanisms* depending on platform — static-baked on PDS, runtime-XOR everywhere else — and a reimplementation must preserve both behind the single slot.

### Considerations

The `perf_update_hbm_7200_supported_v4` override (`:419-422`) is a two-line body — `nd->supports_hbm_7200 = 0; return;` — with no firmware query at all. The V3 body it replaces (`v3:1729`) calls `fw_io_get_available_profiles(FW_IO_AVAILABLE_PERF_PROFILES_HBM_7200, …)` and scans the returned bitmap. The slot is reached from the V3-inherited post-reset path (`nr_post_reset_config_v3`, `v3:413`), which lazily calls it once on the `-1` sentinel; on Trn3 that one call resolves to the hardcoded 0 because the HBM-7200 high-bandwidth profile is not advertised on Trn3. A reimplementer must keep the V3 firmware probe and the V4 hardcode as distinct bodies behind the one slot.

The device-id override `neuron_pci_get_device_id_v4` (`:311-372`) is the V3 discovery logic with one front branch added: if `ndhal_instance_type_3xl()` recognizes `trn3pd98.3xlarge` (`:318`), it skips the firmware routing-id poll entirely and force-assigns `routing_id = 0` (`:321`) — that SKU has a single device, so discovery is trivial. The rest is V3-identical: poll `fw_io_device_id_read` up to 20×@1s until the value is not the `0xdeadbeef` not-ready sentinel (`:324-331`), read the PDS server-id and double `routing_id_max` (`:338-352`), map routing-id → user-id (`:360`), and reject duplicates (`:365-369`). The source carries the same latent dead comparison as V3 — `if (routing_id < 0 …)` on an unsigned `u32` is always false (`:355`, the comment at `:354` flags it as a placeholder "valid routing_id check for TRN3"); this is inherited from V3's `v3:1177`, not a V4 regression.

> **CORRECTION (K-DHAL-V4) —** an earlier cell pin placed the four sysfs suffix assignments at `v4:138`. The body of `ndhal_register_funcs_trn3` begins at `:138`, but the four `arch_*_suffix` string writes are at **`:143-146`**; the registrar dispatches to it on device-id at `:467-475`, not `:138`. Use `:143-146` for the suffix strings and `:138` for the function header. Direct re-read of the shipped source confirms this.

> **NOTE —** `v4/address_map.h:6` opens `#ifndef __V4_ADDR_MAP_H__` with **no matching `#define __V4_ADDR_MAP_H__`** anywhere in the file (only a bare `#endif` at `:229`), so the include guard never arms. It is harmless today because the header has a single include site, but it is a latent multiple-definition trap. This is **not** a V4 regression — `v2/address_map.h:6` and `v3/address_map.h:6` carry the identical dangling `#ifndef`; it is family-wide inherited boilerplate (LOW severity, code-evident).

---

## Cross-References

- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the `struct neuron_dhal` container this registrar patches, and the `vc → v3 → v4` three-layer compose: V4 is the override layer that runs *after* the V3 base, the only three-layer arch in the HAL
- [DHAL V3 (Trn2)](dhal-v3.md) — the base layer V4 inherits in full; owns every slot V4 does not override (reset, H2T sharing, SDMA, the runtime-XOR die-flip V4-PDS replaces with a static table, and the fw_io HBM-7200 query V4 hardcodes to 0)
- [DHAL V2 (Trn1 / Inf2)](dhal-v2.md) — the peer leaf with no layer above it; contrast V2's self-contained registrar with V4's thin piggyback delta
- [Reset Engine](reset.md) — the arch-neutral worker whose `nr_post_reset_config_v3` (V3-inherited on V4) calls `perf_update_hbm_7200_supported`, and the `no_reset` flag V4 forces to 1 on emulation
- [Pod Election (UltraServer)](pod-election.md) — `npe_init` (called from V4 for UltraServer, the double-call risk the driver TODO flags) and the consumer of `npe_neighbor_eng_ids_v4`
