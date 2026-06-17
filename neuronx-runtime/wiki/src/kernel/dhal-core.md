# DHAL Core (ndhal Vtable-of-Vtables)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The DHAL **core** owns exactly two files — `neuron_dhal.c` (77 lines) and `neuron_dhal.h` (268 lines) — both read in full; every struct member and the arch switch below is transcribed verbatim. The per-arch implementation bodies live in `vc/`, `v2/`, `v3/`, `v4/` and are owned by their own pages. Other driver versions renumber lines and add/remove sub-vtables.*
> *Evidence grade: **Confirmed (source-anchored)** — composition, lifecycle, and the arch dispatch are direct C, not reverse-engineered. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

DHAL — the **D**evice **H**ardware **A**bstraction **L**ayer — is how the arch-neutral driver `.c` files run on three incompatible chip generations (Trainium 1/Inferentia 2, Trainium 2, Trainium 3) with **zero** `#ifdef` branching for chip generation. The mechanism is one global pointer, `struct neuron_dhal *ndhal` (`neuron_dhal.c:7`, initialized `NULL`), holding a single process-wide **vtable-of-vtables**: a container struct whose members are themselves 18 named sub-structs, each one a small vtable grouping the config scalars and function pointers for a single subsystem (reset, DMA rings, notification queues, fw-io mailbox, pod election, PCI BARs, sysfs metrics, and the rest). The arch-neutral code never branches on generation; it calls through `ndhal->ndhal_<subsystem>.<method>(...)`, and the *value* behind each pointer was chosen once at boot by the latched architecture.

The core itself is deliberately thin. It owns only **composition and lifecycle** — allocation, the one-time-init lock, the arch switch that selects which `register_funcs` to invoke, and the free/cleanup. It assigns **none** of the 56 callbacks; every function-pointer write lives in a boundary cell. `neuron_dhal_init` is structured as classic double-checked locking around a single `kmalloc`, then lays down a version-common base (`ndhal_register_funcs_vc`, always), then dispatches on `ndhal_arch.arch` to the per-generation registrar. For V4 specifically the registrar is *layered*: it runs the V3 registrar first as a base and then runs the V4 registrar to override a handful of slots, so anything V4 leaves untouched falls through to the V3 implementation.

The single most consequential design fact is that `ndhal` is **one global shared by every device in the host** (`neuron_dhal.h:231` comment: *"ndhal is a global structure shared by all available neuron devices"*). There is no per-device HAL instance. Combined with the first-wins arch latch in `narch_init` (owned by [arch/generations-enum](../arch/generations-enum.md)) and the `if (ndhal) return 0` fast-path guard, this makes the driver **single-architecture per load**: a host with two different chip generations binds both to one `ndhal` built for whichever probed first. Mixed-generation hosts are unsupported by construction, not by policy. A reimplementer who models the HAL as per-device state has already diverged from the shipped driver.

For reimplementation, the contract is:

- **The container layout** — `struct neuron_dhal` is one scalar plus 18 by-value sub-structs plus one top-level cleanup hook; the sub-structs are embedded, not pointed-to, so the whole HAL is one `kmalloc` and the entire vtable is reachable by member access off `ndhal`.
- **The init protocol** — fast-path guard → lock → double-check → single `kmalloc` → set arch/device-id → unconditional `vc` base → arch switch → delayed cdev class-attr init → unlock. The double-checked-locking shape and the unlock-on-every-exit discipline must be preserved exactly.
- **The arch dispatch and the V4-on-V3 layering** — V2 and V3 each call one registrar; V4 calls *two* (V3 base, then V4 overrides). The order is the contract: V4 must run after V3 or its overrides are clobbered.

| | |
|---|---|
| **Global** | `struct neuron_dhal *ndhal` (`neuron_dhal.c:7`, init `NULL`) — one per module, shared by all devices |
| **Init / cleanup / free** | `neuron_dhal_init` (`neuron_dhal.c:10`) · `neuron_dhal_cleanup` (`:61`) · `neuron_dhal_free` (`:72`) |
| **One-time guard** | `if (ndhal) return 0` (`neuron_dhal.c:14-16`) + double-check under lock (`:19`) |
| **Init lock** | `static DEFINE_MUTEX(ndhal_init_lock)` (`neuron_dhal.c:9`) |
| **Container** | `struct neuron_dhal` (`neuron_dhal.h:207-229`) — 1 scalar + 18 sub-vtables + `ndhal_ext_cleanup` |
| **Callback count** | **56** function pointers across 15 of the 18 sub-vtables (`neuron_dhal.h`, tally below) |
| **Registrars** | `ndhal_register_funcs_{vc,v2,v3,v4}` (decl `neuron_dhal.h:263-266`; bodies in boundary cells) |
| **Arch source** | `narch_get_arch()` (`neuron_arch.h:42`) → `enum neuron_arch {INVALID, V2=2, V3=3, V4=4}` (`neuron_arch.h:12-17`) |
| **Caller** | `neuron_pci_probe → neuron_dhal_init(dev->device)` (`neuron_pci.c:383`), one-time per host |

---

## 1. The Container — `struct neuron_dhal`

### Purpose

`struct neuron_dhal` (`neuron_dhal.h:207-229`) is the whole HAL in one allocation. It is **not** a table of pointers to sub-vtables; the 18 sub-structs are embedded **by value** (`struct ndhal_reset ndhal_reset;`, not `struct ndhal_reset *`). That single decision shapes everything downstream: `neuron_dhal_init` allocates the entire HAL with one `kmalloc(sizeof(struct neuron_dhal), GFP_KERNEL)` (`neuron_dhal.c:21`), every callback is reachable as `ndhal->ndhal_<area>.<fn>` with no second indirection, and a registrar populates its slots by plain assignment into the live global. The only true pointer member at the container level is the top-level cleanup hook `ndhal_ext_cleanup` (`:228`).

The grouping axis is **subsystem**, not call frequency or arch. Each sub-vtable bundles (a) the config scalars that generation needs — BAR indices, NC-per-device counts, DRAM channel base/end arrays, reset poll delays — and (b) the function pointers that read or act on that subsystem's hardware. Three sub-vtables (`ndhal_arch`, `ndhal_address_map`, `ndhal_udma`) carry **only** scalars and no callbacks; they are pure per-arch data tables that the registrars fill in alongside the function pointers.

### Layout

The container, in member order. Offsets are **member indices**, not byte offsets — the exact byte layout is ABI/compiler-determined and out of pure-source scope (compute with `pahole` on the built `.ko` if a byte map is ever needed; LOW confidence on bytes, CERTAIN on order).

| # | Member | Type | Callbacks | Role | Decl |
|---|---|---|---|---|---|
| 0 | `pci_device_id` | `unsigned int` | — | raw PCI device-id, set by core at `.c:33` | `:208` |
| 1 | `ndhal_arch` | `struct ndhal_arch` | 0 | arch enum + platform type + server id (scalars only) | `:210` |
| 2 | `ndhal_address_map` | `struct ndhal_address_map` | 0 | 18 BAR/sema/event offsets + NC/DMA/DRAM counts (scalars only) | `:211` |
| 3 | `ndhal_reset` | `struct ndhal_reset` | 3 | reset poll/retry scalars + initiate/wait/post-reset hooks | `:212` |
| 4 | `ndhal_topsp` | `struct ndhal_topsp` | 4 | TopSP notification-queue init/destroy/hwaddr | `:213` |
| 5 | `ndhal_nc` | `struct ndhal_nc` | 2 | per-NC semaphore-base / event-address resolvers | `:214` |
| 6 | `ndhal_nq` | `struct ndhal_nq` | 2 | notification-queue id + hwaddr writers | `:215` |
| 7 | `ndhal_mpset` | `struct ndhal_mpset` | 1 | mempool min-alloc + DRAM channel base/end arrays + set-info | `:216` |
| 8 | `ndhal_ndmar` | `struct ndhal_ndmar` | 6 | DMA-ring H2T engine/queue geometry + quiesce | `:217` |
| 9 | `ndhal_fw_io` | `struct ndhal_fw_io` | 5 | MiscRAM mailbox: topology, readless-read region, CSR array, request, metric | `:218` |
| 10 | `ndhal_mmap` | `struct ndhal_mmap` | 1 | special-mmap table + BAR4 offset resolver | `:219` |
| 11 | `ndhal_sysfs_metrics` | `struct ndhal_sysfs_metrics` | 3 | arch name suffixes + root attr table + ECC/HBM/tensor-engine node adders | `:220` |
| 12 | `ndhal_pci` | `struct ndhal_pci` | 2 | `apb_bar`/`axi_bar`/`dram_bar` indices + device-id/rid-map reads | `:221` |
| 13 | `ndhal_cdev` | `struct ndhal_cdev` | 3 | mem-region table + bar0 write-block list + compat-version/nc-map | `:222` |
| 14 | `ndhal_udma` | `struct ndhal_udma` | 0 | `num_queues` + `num_beats` FIFO depth (scalars only) | `:223` |
| 15 | `ndhal_ndma` | `struct ndhal_ndma` | 6 | retry-memcpy flag + wait-time/validate-pa/init/barrier/connectivity | `:224` |
| 16 | `ndhal_npe` | `struct ndhal_npe` | 8 | pod election: notify/info/status/ctrl + 4 sysfs class shows | `:225` |
| 17 | `ndhal_tpb` | `struct ndhal_tpb` | 5 | tensor-engine perf-counter offset arrays + 5 counter accessors | `:226` |
| 18 | `ndhal_perf` | `struct ndhal_perf` | 4 | performance-profile get/set/supported + HBM-7200 update | `:227` |
| 19 | `ndhal_ext_cleanup` | `void (*)(void)` | (1) | top-level per-arch teardown hook, invoked by `neuron_dhal_cleanup` | `:228` |

### Callback Tally

The header declares the function-pointer surface as follows. Counting `(*` matches in `neuron_dhal.h` yields **57**, but one of those is not a callback:

```text
reset 3 · topsp 4 · nc 2 · nq 2 · mpset 1 · ndmar 6 · fw_io 5 · mmap 1
sysfs_metrics 3 · pci 2 · cdev 3 · ndma 6 · npe 8 · tpb 5 · perf 4      = 55
+ ndhal_ext_cleanup (top-level)                                         =  1
                                                            true total  = 56
```

> **GOTCHA —** the 57th `(*` match is `ndhal_npe.npe_neighbor_eng_ids` (`neuron_dhal.h:180`), declared `u32 (*npe_neighbor_eng_ids)[2]` — a **pointer-to-array-of-2-u32**, *not* a function pointer. A reimplementer (or an audit script) that counts callbacks by grepping `(*` over the header will overcount by one and look for a 57th registrar assignment that does not exist. The honest count is 56 callbacks, of which `vc` sets 4 (§3) and the per-arch registrars set the rest. The three sub-vtables with **zero** callbacks — `ndhal_arch`, `ndhal_address_map`, `ndhal_udma` — are pure scalar tables.

### Considerations

The embed-by-value design means the HAL has **no internal pointers to free**: `neuron_dhal_free` is a single `kfree(ndhal)` (`neuron_dhal.c:75`), and the per-arch cleanup is delegated entirely to the `ndhal_ext_cleanup` hook (§4). It also means a registrar that fails partway leaves a **partially-populated** global with stale-but-valid scalars and a mix of set and NULL callbacks — there is no transactional rollback. Whether that partial vtable is ever used depends on the init return-code path, which is the subject of the V4 quirk in §2.

---

## 2. Init and Arch Dispatch — `neuron_dhal_init`

### Purpose

`neuron_dhal_init(unsigned int pci_device_id)` (`neuron_dhal.c:10-59`) is the only function that allocates and populates `ndhal`. It is **idempotent and global**: called once per matching PCI function during probe (`neuron_pci.c:383`), but it does real work only on the *first* call across the whole host. Every later call hits the fast-path guard and returns 0 without touching the global. It returns `0` on success (including the already-initialized fast path), `-ENOMEM` on allocation failure, and `-EINVAL` for an unknown architecture.

### Entry Point

```text
neuron_pci_probe (neuron_pci.c:352)
  └─ neuron_pci_set_device_architecture (:381)   ── device-id switch → narch_init(arch, rev)
  └─ neuron_dhal_init(dev->device)      (:383)    ── THIS function (pci_device_id = raw PCI did)
       └─ narch_get_arch()              (.c:32)   ── reads the value narch_init latched
       └─ ndhal_register_funcs_vc()     (.c:34)   ── version-common base (always)
       └─ ndhal_register_funcs_v{2,3,4} (.c:38-45)── arch-selected registrar(s)
       └─ ncdev_class_attr_init()       (.c:54)   ── delayed: needs platform data from dhal
  └─ (probe immediately reads ndhal->ndhal_pci.apb_bar, neuron_pci.c:390) ── vtable must be live
```

The ordering constraint is hard: probe dereferences `ndhal->ndhal_pci.{apb_bar,axi_bar,dram_bar}` the instant `neuron_dhal_init` returns (`neuron_pci.c:390` onward), so the entire vtable — at minimum every scalar and callback probe consumes — must be fully populated before the function returns success.

### Algorithm

Annotated pseudocode modelling `neuron_dhal_init` (`neuron_dhal.c:10-59`). The shape is textbook double-checked locking around a single allocation, then a base-plus-arch registration compose:

```c
function neuron_dhal_init(pci_device_id):              // neuron_dhal.c:10
    ret = 0

    // FAST PATH — global already built by an earlier probe; no lock taken.
    if (ndhal != NULL):                                // :14-16  "init must be done only once"
        return 0

    mutex_lock(&ndhal_init_lock)                       // :18

    // DOUBLE CHECK — close the race: another CPU may have allocated between
    // the fast-path read and us taking the lock.
    if (ndhal == NULL):                                // :19  "double check ... prevent race"
        ndhal = kmalloc(sizeof(struct neuron_dhal), GFP_KERNEL)   // :21  ONE alloc, whole HAL
        if (ndhal == NULL):                            // :22
            pr_err("Can't allocate memory for neuron_dhal.\n")    // :23
            mutex_unlock(&ndhal_init_lock)             // :24  unlock on EVERY exit
            return -ENOMEM                             // :25
    else:                                              // :27  someone else won the race
        mutex_unlock(&ndhal_init_lock)                 // :28
        return 0                                       // :29

    // We hold the lock and own a fresh, zero-content allocation.
    ndhal->ndhal_arch.arch = narch_get_arch()          // :32  latched by narch_init in probe
    ndhal->pci_device_id   = pci_device_id             // :33

    ndhal_register_funcs_vc()                           // :34  VERSION-COMMON base — UNCONDITIONAL
                                                        //       (sets only 4 ndhal_tpb fn-ptrs, §3)

    switch (ndhal->ndhal_arch.arch):                    // :36
        case NEURON_ARCH_V2:                            // :37   Trn1 0x7164 / Inf2 0x7264
            ret = ndhal_register_funcs_v2()             // :38
            break
        case NEURON_ARCH_V3:                            // :40   Trn2 0x7364
            ret = ndhal_register_funcs_v3()             // :41
            break
        case NEURON_ARCH_V4:                            // :43   Trn3 0x7564 / 0x7565
            ret = ndhal_register_funcs_v3()             // :44   "use v3 as base"
            ret = ndhal_register_funcs_v4()             // :45   "apply v4 overrides"  ◀ clobbers ret
            break
        default:                                        // :47   INVALID / unrecognized arch
            pr_err("Unknown HW architecture: %d. Can't init neuron_dhal.\n", arch)  // :48
            mutex_unlock(&ndhal_init_lock)              // :49
            return -EINVAL                              // :50

    // Class attributes need platform data the registrars just installed.
    ncdev_class_attr_init()                             // :54  delayed cdev class-attr setup

    mutex_unlock(&ndhal_init_lock)                      // :56
    return ret                                          // :58
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `neuron_dhal_init` | `.c:10-59` | idempotent global allocator + arch dispatcher | CERTAIN |
| `narch_get_arch` | `neuron_arch.h:42` | returns the host-latched `enum neuron_arch` (boundary) | CERTAIN |
| `ndhal_register_funcs_vc` | `vc/neuron_dhal_vc.c:139` | version-common base; sets 4 `ndhal_tpb` fn-ptrs (§3) | HIGH |
| `ndhal_register_funcs_v2` | `v2/neuron_dhal_v2.c:1377` | full V2 (Trn1/Inf2) vtable fill | HIGH |
| `ndhal_register_funcs_v3` | `v3/neuron_dhal_v3.c` | full V3 (Trn2) vtable fill; also V4 base | HIGH |
| `ndhal_register_funcs_v4` | `v4/neuron_dhal_v4.c:430` | V4 (Trn3) overrides on top of V3 base | HIGH |
| `ncdev_class_attr_init` | `neuron_cdev.*` | delayed sysfs class-attr init (boundary) | HIGH |

### Considerations

**The arch dispatch table is the whole policy.** Five PCI device-ids map to three arches, and three arches map to four registration sequences:

| `arch` | Constant | Device-ids | Registration sequence (in order) |
|---|---|---|---|
| 2 | `NEURON_ARCH_V2` | `0x7164` Trn1, `0x7264` Inf2 | `vc` → `v2` |
| 3 | `NEURON_ARCH_V3` | `0x7364` Trn2 | `vc` → `v3` |
| 4 | `NEURON_ARCH_V4` | `0x7564`, `0x7565` Trn3 | `vc` → `v3` → `v4` |
| else | `INVALID`/other | — | `vc` → `pr_err` → `-EINVAL` |

Device-id → arch is resolved one step earlier by `neuron_pci_set_device_architecture` (`neuron_pci.c:205-228`) via `narch_init`; the device-id macros are `neuron_device.h:36-40` and the full map is owned by [arch/generations-enum](../arch/generations-enum.md). Note `narch_init` is itself first-wins — only the first probed device sets the host arch — which is what couples this dispatch to the single-arch-per-host invariant (§3 of [overview](overview.md)).

> **QUIRK —** the V4 path runs **two** registrars, not one: `ndhal_register_funcs_v3()` first ("use v3 as base", `neuron_dhal.c:44`), then `ndhal_register_funcs_v4()` ("apply v4 overrides", `:45`). V4 is *not* a self-contained vtable — it is a thin override layer that depends on V3 having run first to fill the slots it does not touch (V4 overrides on the order of 8+1 slots; the rest fall through to V3). The order is decisive for correctness: swap the two lines and every V4 override is immediately clobbered by the V3 base. A reimplementation of the V4 registrar must be written assuming a fully-populated V3 vtable underneath it, and the dispatcher must run V3 before V4. This is the only three-layer compose in the HAL (`vc` → `v3` → `v4`); V2 and V3 are two-layer (`vc` → arch).

> **CORRECTION (K-DHAL-CORE) —** an earlier audit pin had the one-time-init guard at `neuron_dhal.c:13-15`. The guard `if (ndhal) { return 0; }` is at **`:14-16`**; line `:13` is the comment *"ndhal is a global struct so its init must be done only once."* Use `:14-16` for the guard. Direct re-read of the shipped source confirms this.

> **GOTCHA —** the V4 path swallows the V3 registrar's return code. `ret = ndhal_register_funcs_v3(); ret = ndhal_register_funcs_v4();` (`neuron_dhal.c:44-45`) — the second assignment overwrites the first, so if the V3 base registration failed but the V4 override succeeded, `neuron_dhal_init` returns success with a **partially-built V3 base** under the V4 overrides. The exposure is bounded only because the V3/V4 registrars are straight-line pointer assignments unlikely to fail in practice (no allocation in the registrar bodies), but a reimplementation that adds fallible work to a registrar must `ret = v3(); if (ret) goto unlock_fail;` before applying V4. Severity MEDIUM (correctness, not observed harmful). A second, lower-severity lifecycle note: `neuron_dhal_free` does `kfree(ndhal)` without setting `ndhal = NULL` afterward (`.c:72-77`), so the global is left dangling; whether a re-probe after free can re-enter `neuron_dhal_init` (and trip the fast-path guard on freed memory) depends on the PCI hot-plug lifecycle, which is out of this cell's scope to confirm (MEDIUM).

---

## 3. The Version-Common Base — `vc`

### Purpose

`ndhal_register_funcs_vc` (`vc/neuron_dhal_vc.c:139`) is called **unconditionally** before the arch switch (`neuron_dhal.c:34`), for every architecture including the about-to-fail unknown-arch path. Despite the "register funcs" name, it is the smallest registrar in the HAL: it installs exactly **four** function pointers, all into `ndhal_tpb` — the version-common tensor-engine performance-counter helpers that are identical across V2/V3/V4 because the counter-read arithmetic does not change between generations:

```c
ndhal->ndhal_tpb.pe_format_activity_stats               = ntpb_pe_format_activity_stats_vc;
ndhal->ndhal_tpb.pe_get_counter_val                     = ntpb_pe_get_counter_val_vc;
ndhal->ndhal_tpb.pe_get_row_grp_activity_counter_offset = ntpb_pe_get_row_grp_activity_counter_offset_vc;
ndhal->ndhal_tpb.pe_get_fast_wl_cycle_cnt               = ntpb_pe_get_fast_wl_cycle_cnt_vc;
                                                          // vc/neuron_dhal_vc.c:146-149
```

The fifth `ndhal_tpb` callback, `pe_get_aggregated_wl_cycle_cnt` (`neuron_dhal.h:195`), is **not** set by `vc` — it is left to the per-arch registrar, because aggregation crosses the per-generation row-group geometry. The remaining 51 of the 56 callbacks are all installed by `v2`/`v3`/`v4`.

### Considerations

`vc` running first, then the arch registrar writing on top, is the base-layering discipline applied to every arch — `vc` is the universal floor, the arch impl is the body, and (for V4 only) the V4 override is a second body on top of V3. Because `vc` only touches 4 `ndhal_tpb` slots and never the scalars, a registration failure in `vc` is structurally impossible from the core's point of view (`vc` returns `int` but the core does not check it at `.c:34`). The composed "who sets which of the 56 callbacks" matrix — V2 vs V3 vs V4-overrides — is owned by the per-arch pages ([dhal-v2](dhal-v2.md), [dhal-v3](dhal-v3.md), [dhal-v4](dhal-v4.md)); the core only fixes that `vc` lays down these 4 and the arch switch lays down the rest.

---

## 4. Cleanup and Free

### Purpose

Teardown is split across two core functions, mirroring the probe/remove split in the PCI layer (`neuron_pci.c:558-560`). `neuron_dhal_cleanup` (`neuron_dhal.c:61-70`) runs the *logical* teardown — it tears down the cdev class attributes and invokes the per-arch external cleanup hook, but does **not** free the allocation. `neuron_dhal_free` (`neuron_dhal.c:72-77`) runs the *physical* teardown — a single `kfree`. Splitting them lets the PCI remove path run device-specific cleanup (which may touch hardware through the still-live vtable) before the vtable memory disappears.

### Algorithm

```c
function neuron_dhal_cleanup():                  // neuron_dhal.c:61
    if (ndhal != NULL):                          // :63
        ncdev_class_attr_cleanup()               // :64  undo the delayed class-attr init (.c:54)
        if (ndhal->ndhal_ext_cleanup != NULL):   // :66  per-arch hook, wired by the registrar
            ndhal->ndhal_ext_cleanup()           // :67  V2→ext_cleanup_v2, V3→ext_cleanup_v3
    // NOTE: does NOT free ndhal — that is neuron_dhal_free's job.

function neuron_dhal_free():                     // neuron_dhal.c:72
    if (ndhal != NULL):                          // :74
        kfree(ndhal)                             // :75  one free for the whole HAL
    // NOTE: does NOT set ndhal = NULL (see §2 GOTCHA)
```

### Considerations

`ndhal_ext_cleanup` is the only per-arch behavior the core reaches at teardown, and it is wired by the registrars, not the core: V2 sets it to `ndhal_ext_cleanup_v2` (`v2/neuron_dhal_v2.c:1356`, assigned `:1471`); V3 sets it to `ndhal_ext_cleanup_v3` (`v3/neuron_dhal_v3.c:1844`, assigned `:1967`); V4 installs no override, so it inherits V3's hook through the base-layering (consistent with the V4-on-V3 compose in §2). The `kfree`-without-`NULL` is the lifecycle hazard already flagged in §2 — benign under the normal once-per-boot load/unload cycle, a latent double-init/use-after-free risk only if a re-probe can occur after `neuron_dhal_free` without a module reload.

---

## Cross-References

- [DHAL V2 (Trn1 / Inf2)](dhal-v2.md) — `ndhal_register_funcs_v2`, the full V2 vtable fill, and `ndhal_ext_cleanup_v2`
- [DHAL V3 (Trn2)](dhal-v3.md) — `ndhal_register_funcs_v3`, the V4 base layer, the `force_die_flip` module param, and `ndhal_ext_cleanup_v3`
- [DHAL V4 (Trn3)](dhal-v4.md) — `ndhal_register_funcs_v4`, the 8+1 override slots that piggyback the V3 base
- [Kernel Driver Overview](overview.md) — the §4 DHAL boundary, the single-arch-per-host invariant, and where every sub-vtable's consumers live
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](../arch/generations-enum.md) — `enum neuron_arch`, the device-id → arch map, and the `narch_init` first-wins latch this dispatch reads
