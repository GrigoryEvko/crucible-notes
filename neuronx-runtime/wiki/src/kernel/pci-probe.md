# PCI Probe and Device Detection

> **Kernel:** `aws-neuronx-dkms 2.27.4.0` (GPL-2.0 source; build SHA `1c7ed9bd14936635773b5a01777882804ee8ea6e`, `neuron_module.c:23,27`). Every `file:line` cites into `extracted/aws-neuronx-dkms_2.27.4.0_all/usr/src/aws-neuronx-2.27.4.0/`, read directly — no reverse engineering. The driver also builds a stripped `neuron.ko`, but the GPL C is authoritative; every offset here is a struct field or `#define`, never a recovered binary offset. Other driver versions renumber these lines.
> **Status:** Reimplementation-grade · **Evidence grade:** `file:line`-anchored (source confirmed) · **Part III — Kernel Driver**, DEEP · [back to overview](overview.md)

## Abstract

`neuron_pci_probe` (`neuron_pci.c:352`) is an ordinary Linux `pci_driver` `.probe` callback in shape, and a reimplementer who has written one for any PCIe accelerator already owns the frame: the kernel binds the one `struct pci_driver` (`"neuron-driver"`, `neuron_pci.c:536-541`) to each matching PCI function, and for each bound function the probe allocates the per-device object, enables the device and bus-mastering, maps the BARs, brings up the device's subsystems, and publishes a control node — all behind an error-unwind ladder of `goto` rungs that releases exactly the resources acquired so far. `neuron_pci_remove` (`:497`) is the mirror. What makes this probe Neuron-specific is concentrated in four decisions layered onto that generic skeleton, and this page is the DEEP derivation of each.

First, the **architecture is latched here, once, before anything that depends on it**. The probe reads the PCI device-ID and revision byte, switch-maps the device-ID to `enum neuron_arch`, and calls `narch_init` — which is **first-wins**: the first probed device sets the global arch and every later device's arch is silently dropped (`neuron_arch.c:36-37`). The arch decides the BAR *indices* the rest of probe maps, so it must run before BAR mapping; the source enforces this by call order (`:381` then `:383`). Second, the **BAR names are not BAR indices**: probe maps a triple it labels `bar0`/`bar2`/`bar4` (APB register window, AXI, DRAM/HBM), but the actual PCI BAR number for each comes from the per-arch `ndhal->ndhal_pci.{apb_bar,axi_bar,dram_bar}` vtable — and on every shipping arch `axi_bar == BAR_UNUSED(-1)`, so the AXI map is a no-op and the DRAM map is failure-tolerated. Third, the **device's slot in the global registry is its routing id, not its enumeration order**: probe assigns a provisional `atomic_fetch_add` counter (`:421`) that is immediately overwritten by a firmware read inside the DHAL `neuron_pci_get_device_id` callback (`:433`), and that resolved index is both the `neuron_devices[64]` slot and the `/dev/neuronN` minor. Fourth, a **duplicate routing id triggers driver self-unload** via a `call_usermodehelper` `modprobe -r` (`:71-107`).

The page proceeds in the probe's own order: the entry-point tree, then the device-ID→arch resolution and first-wins latch, then the BAR-map trio (with the validate/reserve/iomap helpers and the DRAM-tolerance rule), then the registry publish (the provisional-then-routing-id index dance and the publish `BUG_ON`), then the full probe sequence and its error-unwind ladder as annotated pseudocode, then `remove`. The two probe-stage leak-on-error paths the audit flagged (`neuron_pci.c:132-133, :166-167`) and the stale `MODULE_ALIAS` device-ID `0x7064` are called out in place. The arch enum's *meaning* (V2/V3/V4, the marketing-name vocabulary, the EMU/QEMU revision axis) is owned by [generations-enum](../arch/generations-enum.md); this page owns the probe *mechanism* that produces and consumes it.

For reimplementation, the contract is:

- **The `pci_driver` and id_table** — five device-IDs under vendor `0x1D0F`, the bind handler, and the absence of MSI/MSI-X and `.suspend`/`.resume`/`.shutdown` in this driver.
- **The probe sequence and its exact error-unwind ladder** — every `goto`/return rung, what it releases, and the two rungs that leak.
- **The device-ID→arch resolution and single-arch first-wins latch** — run before BAR mapping, which the BAR indices depend on.
- **The BAR-map trio** — names vs PCI indices from `ndhal_pci`, the DRAM-tolerance and WC-mapping rules, AXI as a no-op on every arch.
- **The two-source `device_index`** — provisional counter then routing-id overwrite — and the publish `BUG_ON` that enforces one device per slot.

| | |
|---|---|
| **PCI driver** | `neuron_pci_driver` `{.name="neuron-driver", .id_table, .probe, .remove}` (`neuron_pci.c:536-541`) — **no** MSI/MSI-X, no `.suspend`/`.resume`/`.shutdown`/`.err_handler` |
| **Bind / unbind** | `neuron_pci_probe` (`:352`) / `neuron_pci_remove` (`:497`) |
| **id_table** | `neuron_pci_dev_ids[]` (`:30-39`) — 5 IDs + `{0,}` sentinel: TRN1 `0x7164`, INF2 `0x7264`, TRN2 `0x7364`, TRN3 `0x7564`/`0x7565` under vendor `0x1D0F` |
| **Arch latch** | `neuron_pci_set_device_architecture` (`:205`) → `narch_init` (`neuron_arch.c:33`) — first-wins (`:36-37`), runs **before** BAR map (`:381` < `:383`) |
| **BAR triple** | `struct neuron_pci_device {bar0/bar2/bar4}` (`neuron_device.h:45-55`); PCI indices from `ndhal->ndhal_pci.{apb,axi,dram}_bar`; `axi_bar == BAR_UNUSED(-1)` on every arch |
| **DRAM BAR** | `BAR4`, WC-mapped when `wc_enable`(default 1, `:50-52`); map failure **tolerated** (`:262-267, :321-326`) |
| **Registry** | `neuron_devices[MAX_NEURON_DEVICE_COUNT(64)]` (`:57`) — routing-id-indexed; publish at `:466-467` (`BUG_ON` slot-not-empty) |
| **device_index** | provisional `atomic_fetch_add(&device_count)` (`:421`) → overwritten by routing-id read `neuron_pci_get_device_id` (`:433`) |
| **Dup-rid handler** | `neuron_pci_handle_dup_routing_id` (`:71`) — schedules `modprobe -r` self-unload on first dup |
| **Module params** | `dup_helper_enable` (`:46-48`), `wc_enable` (`:50-52`) — both default 1, perm `0660` |
| **Stale alias** | `MODULE_ALIAS("pci:…d00007064…")` (`neuron_module.c:24`) — `0x7064` is **not** an id_table ID (PCI-01) |

---

## The PCI Driver and id_table

### Purpose

The driver registers one `struct pci_driver` that the kernel matches against every PCIe function on the bus. `neuron_pci_module_init` (`:543`) calls `pci_register_driver(&neuron_pci_driver)` (`:547`); the kernel then invokes `neuron_pci_probe` synchronously for each already-enumerated function whose `(vendor, device)` is in the id_table, and again later for hot-plugged functions. There is no interrupt setup in this lane at all — despite the file header comment claiming "MSI interrupt setup" (`:6`), the probe never calls `pci_alloc_irq_vectors` or `pci_enable_msix`; the only PCI-config action beyond enable is `pci_set_master` (`:378`). The device is driven entirely by MMIO and host-memory completion markers, so the driver carries no `.suspend`/`.resume`/`.shutdown`/`.err_handler`.

### Encoding

The id_table and driver object, verbatim (`neuron_pci.c:30-39, 536-541`):

```c
static struct pci_device_id neuron_pci_dev_ids[] = {
    { PCI_DEVICE(AMZN_VENDOR_ID, TRN1_DEVICE_ID0) },   // 0x1D0F:0x7164  -> arch V2
    { PCI_DEVICE(AMZN_VENDOR_ID, INF2_DEVICE_ID0) },   // 0x1D0F:0x7264  -> arch V2
    { PCI_DEVICE(AMZN_VENDOR_ID, TRN2_DEVICE_ID0) },   // 0x1D0F:0x7364  -> arch V3
    { PCI_DEVICE(AMZN_VENDOR_ID, TRN3_DEVICE_ID0) },   // 0x1D0F:0x7564  -> arch V4
    { PCI_DEVICE(AMZN_VENDOR_ID, TRN3_DEVICE_ID1) },   // 0x1D0F:0x7565  -> arch V4
    { 0, },                                            // sentinel
};

static struct pci_driver neuron_pci_driver = {
    .name     = "neuron-driver",       // driver node /sys/bus/pci/drivers/neuron-driver
    .id_table = neuron_pci_dev_ids,
    .probe    = neuron_pci_probe,       // :352
    .remove   = neuron_pci_remove,      // :497
};
```

The five device-IDs and the vendor are `#define`s in `neuron_device.h:35-40`. Five IDs collapse to three arches and four products (Trn1+Inf2 share V2; `0x7564`/`0x7565` are both Trn3/V4) — the full device-ID→arch→product reconciliation is owned by [pci-device-ids](../arch/pci-device-ids.md).

> **CORRECTION (PCI-01) —** the udev autoload `MODULE_ALIAS` is `pci:v00001d0fd00007064sv*sd*bc*sc*i*` (`neuron_module.c:24`) — device-ID `0x7064`, which is **not** any of the five IDs in `neuron_pci_dev_ids[]` (TRN1 `0x7164` / INF2 `0x7264` / TRN2 `0x7364` / TRN3 `0x7564`/`0x7565`). `0x7064` appears nowhere else in the tree (`rg` confirmed). Runtime binding is driven by the `id_table`, so a *loaded* driver still matches its devices; but the autoload alias points at a device-ID the driver never claims, so udev-based autoload by modalias is broken for the supported parts. A reimplementer must derive the `MODULE_ALIAS` from the id_table (e.g. via `MODULE_DEVICE_TABLE(pci, neuron_pci_dev_ids)`) rather than hand-writing it. **(HIGH** — direct source read; also flagged from [generations-enum](../arch/generations-enum.md#detection-and-latching--the-one-place-one-time-decision).**)**

### Considerations

- **Module object name ≠ driver name.** `Kbuild` builds `neuron.o` so the module is `/sys/module/neuron`, but `.name = "neuron-driver"` makes the sysfs driver node `/sys/bus/pci/drivers/neuron-driver`. Scripts binding/unbinding by sysfs must use the right path for the right object — see [overview](overview.md#1-the-module--file-map).
- **`total_neuron_devices` is sampled once.** `neuron_pci_module_init` caches `total_neuron_devices = atomic_read(&device_count)` (`:552`) immediately after `pci_register_driver` returns. Only probes that completed *synchronously* inside `pci_register_driver` are counted; a device probed later still registers into `neuron_devices[]` but does not bump `total_neuron_devices`. **(HIGH** — a real undercount on hot-plug or deferred-probe hosts.**)**

---

## Device-ID → Arch Resolution and the First-Wins Latch

### Purpose

Before any BAR can be mapped, the probe must know the architecture, because the BAR *indices* are arch-specific (next section). `neuron_pci_set_device_architecture` (`:205`) reads the PCI device-ID and the `PCI_REVISION_ID` byte, switch-maps the device-ID to `enum neuron_arch`, and latches it into a file-static singleton via `narch_init`. The latch is **first-wins**: the global arch is set by the first device to probe and never changed, encoding the driver's hard "one chip type per host" assumption (`neuron_arch.c:6-8`).

### Entry Point

```text
neuron_pci_probe (neuron_pci.c:352)                          ── PCI bind handler (per device)
  ├─ pci_enable_device / pci_set_master                      ── :372 / :378
  ├─ neuron_pci_set_device_architecture (:381 → :205)        ── device-ID switch
  │     ├─ pci_read_config_byte(PCI_REVISION_ID)             ── :210
  │     └─ narch_init(arch, revision)  (neuron_arch.c:33)    ── LATCH (first-wins, :36-37)
  └─ neuron_dhal_init(dev->device)     (:383 → neuron_dhal.c:10) ── reads arch back, installs ndhal vtable
```

### Algorithm

The resolution and latch, modeling `neuron_pci_set_device_architecture` (`:205-228`) and `narch_init` (`neuron_arch.c:33-40`):

```c
// neuron_pci_set_device_architecture @ neuron_pci.c:205
function set_device_architecture(nd):
    device   = nd->pdev->device                       // PCI device-ID    (:207)
    arch     = NEURON_ARCH_INVALID                     //                  (:208)
    revision = pci_read_config_byte(PCI_REVISION_ID)   // single byte      (:210)

    switch (device):                                   // neuron_device.h:36-40 constants
        case TRN1_DEVICE_ID0(0x7164):                  // Trainium1
        case INF2_DEVICE_ID0(0x7264):                  // Inferentia2 -> SAME arch
            arch = NEURON_ARCH_V2                       // (:213-216)
        case TRN2_DEVICE_ID0(0x7364):                  // Trainium2
            arch = NEURON_ARCH_V3                       // (:217-218)
        case TRN3_DEVICE_ID0(0x7564):                  // Trainium3
        case TRN3_DEVICE_ID1(0x7565):                  // Trn3, 2nd id -> SAME arch
            arch = NEURON_ARCH_V4                       // (:220-222)
        default:
            return                                     // unknown id: NO narch_init, arch stays INVALID (:224-225)

    narch_init(arch, revision)                         // (:227)

// narch_init @ neuron_arch.c:33  — the first-wins latch
function narch_init(arch, revision):
    if arch_info.arch != NEURON_ARCH_INVALID:          // already latched by an earlier device
        return                                         // FIRST-WINS: later devices ignored (:36-37)
    arch_info.arch     = arch                           // (:38)
    arch_info.revision = revision                       // (:39)
    // no lock: relies on PCI probe serialization
```

The probe then calls `neuron_dhal_init(dev->device)` (`:383`), which reads the latched arch back via `narch_get_arch()` and installs the per-arch `ndhal` ops vtable — a **one-time global** init guarded by `if (ndhal) return 0` plus a double-checked `mutex_lock(&ndhal_init_lock)` (`neuron_dhal.c:14-19`). From this point the probe's BAR indices come from that vtable.

### Considerations

- **`set_device_architecture` returns `void` and cannot fail the probe on an unknown ID.** An unrecognized device-ID falls through `default:` (`:224-225`) leaving `arch_info.arch == INVALID`; the probe proceeds to `neuron_dhal_init`, whose `narch_get_arch()` `BUG_ON`s on `INVALID` (`neuron_arch.c:44`) — so a vendor-matched but ID-unknown device crashes the driver rather than failing the probe gracefully. In practice unreachable because the `id_table` binds only the five known IDs, but the two lists must stay in sync: adding an `id_table` entry without a switch case is a latent panic. Derived in full on [generations-enum](../arch/generations-enum.md#detection-and-latching--the-one-place-one-time-decision).

> **QUIRK —** the driver is **single-architecture per host, first device wins.** `narch_init` early-returns once latched (`neuron_arch.c:36-37`) and `neuron_dhal_init` is a one-time global (`neuron_dhal.c:14-16`). A host with both a Trn2 (V3) and a Trn3 (V4) card latches *one* arch — whichever probed first — and builds *one* `ndhal`. The second device is still enabled, its BARs mapped (with the *wrong* arch's indices), and it is published into `neuron_devices[]`; the driver does **not** reject the mismatched second device. A reimplementer targeting heterogeneous hosts must replace the singleton with a per-device ident; until then, mixed-generation hosts are unsupported by construction, not policy.

---

## The BAR-Map Trio

### Purpose

The probe maps up to three BARs into the per-device `struct neuron_pci_device` triple (`neuron_device.h:45-55`): `bar0` (APB register/CSR window, feeds FWIO readless-read), `bar2` (AXI), and `bar4` (DRAM/HBM window). Each BAR goes through the same two-step `reserve` (mark the PCI region busy) then `set_npdev` (record phys addr / size and `iomap` it) pair, both routed through one `is_valid_bar` validator. The decisive subtlety: the *names* `bar0`/`bar2`/`bar4` are struct field names, not PCI BAR indices — the real index for each comes from `ndhal->ndhal_pci.{apb_bar, axi_bar, dram_bar}`, populated by the arch's DHAL register call.

### Encoding — names vs PCI indices

| Struct field | Role | PCI index source | V2 (Trn1/Inf2) | V3 (Trn2) | V4 (Trn3) |
|---|---|---|---|---|---|
| `bar0` | APB register/CSR window (FWIO) | `ndhal_pci.apb_bar` | 0 (2 on qemu) | 0 | 0 (inherits V3) |
| `bar2` | AXI | `ndhal_pci.axi_bar` | `BAR_UNUSED(-1)` | `BAR_UNUSED(-1)` | `BAR_UNUSED(-1)` |
| `bar4` | DRAM/HBM window | `ndhal_pci.dram_bar` | 4 | 4 | 4 (inherits V3) |

`BAR_UNUSED = -1` (`neuron_pci.h:13`). The arch BAR-index values live in the DHAL register functions (`v2/neuron_dhal_v2.c:1433-1434`, `v3/neuron_dhal_v3.c:1925-1927`; V4 inherits V3's BAR base and overrides only the device-id read) and are BOUNDARY to this cell — recorded, not owned (see [dhal-core](dhal-core.md)). **(BAR index values: MED** — read from DHAL boundary files.**)**

> **QUIRK —** the AXI BAR (`bar2`) is `BAR_UNUSED` on **every** shipping arch, so `neuron_pci_reserve_bar`/`set_npdev`/`release_bar` short-circuit to no-ops (`:249-251, :291-293, :344-346`) and `nd->npdev.bar2` stays `NULL`. `fw_io_setup` is still handed `bar2`/`bar2_size` (both zero/NULL, `:425-426`) — FWIO drives off the APB window (`bar0`) alone. A reimplementer wiring the FWIO context must not assume `bar2` is mapped.

### Algorithm — the validate / reserve / map helpers

The three helpers share `is_valid_bar` and the **DRAM-tolerance** rule: a mapping failure on the DRAM BAR is *swallowed* (the device binds without an HBM window), while a failure on any other BAR aborts the probe. Modeling `is_valid_bar` (`:230`), `neuron_pci_reserve_bar` (`:242`), and `neuron_pci_set_npdev` (`:281`):

```c
// is_valid_bar @ :230 — the bar must be one of the three named in the arch vtable
function is_valid_bar(bar):
    return bar == ndhal_pci.apb_bar
        || bar == ndhal_pci.axi_bar
        || bar == ndhal_pci.dram_bar          // (:231)

// neuron_pci_reserve_bar @ :242 — pci_request_region with DRAM tolerance
function reserve_bar(dev, bar, res_name):
    if !is_valid_bar(bar): goto err           // (:245-248)
    if bar == BAR_UNUSED:  return 0            // AXI no-op on every arch (:249-251)
    if pci_request_region(dev, bar, res_name) != 0:  goto err   // (:253-257)
    return 0
err:
    if bar == ndhal_pci.dram_bar: return 0    // DRAM failure tolerated (:262-264)
    else:                         return -ENODEV  // any other BAR is fatal (:265-266)

// neuron_pci_set_npdev @ :281 — record pa/size, iomap (WC for DRAM)
function set_npdev(dev, bar, res_name, *bar_pa, **bar_ioaddr, *bar_size):
    if !is_valid_bar(bar): return -ENODEV      // (:287-290)
    if bar == BAR_UNUSED:  return 0            // (:291-293)
    if pci_resource_len(dev, bar) == 0:  goto err          // (:295-298)
    *bar_pa = pci_resource_start(dev, bar)
    if *bar_pa == 0:                     goto err          // (:300-304)
    *bar_size = pci_resource_len(dev, bar)                 // (:305)
    if bar == ndhal_pci.dram_bar:
        ndhal_pci.dram_bar_size = *bar_size                // cache into DHAL (:307-309)
    if bar == ndhal_pci.dram_bar && wc_enable:
        *bar_ioaddr = pci_iomap_wc(dev, bar, len)          // write-combining (:311-313)
    else:
        *bar_ioaddr = pci_iomap(dev, bar, len)             // (:314-316)
    return 0
err:
    if bar == ndhal_pci.dram_bar:                          // DRAM tolerated: zero out, succeed
        *bar_pa = 0; *bar_size = 0; *bar_ioaddr = NULL; return 0   // (:321-326)
    else:
        return -ENODEV                                     // (:327-328)
```

### Considerations

- **DRAM write-combining is gated by `wc_enable` (default 1, `:50-52`).** When set, the HBM BAR is mapped `pci_iomap_wc` rather than `pci_iomap` (`:311-316`), which weakens ordering on that window — a reimplementer must respect the WC semantics on the DRAM mapping. The cached `dram_bar_size` (`:307-309`) is later consumed by the emulation-driven dynamic HBM sizing (`dram_bar_size/4`) in the per-arch DHAL — see [generations-enum](../arch/generations-enum.md#the-emuqemu-axis--revision-as-a-simulator-marker).
- **The reserve/map split is two `goto` labels per BAR**, so the unwind ladder (next) has a `*_map` rung (between reserve and set_npdev) and a `*_resource` rung (after set_npdev) for each — see §"The Probe Sequence".

---

## The Registry Publish — Two-Source Index and the BUG_ON

### Purpose

The per-device object's home is the global `neuron_devices[MAX_NEURON_DEVICE_COUNT(64)]` array (`:57`), the single registry every other subsystem reaches through `neuron_pci_get_device` (`:64`). The slot is the device's **routing id** — a firmware-assigned topology index, not the PCI enumeration order — so `/dev/neuronN`, `neuron_devices[N]`, and the char-node `devnodes[N]` all key on one consistent index.

### Algorithm — provisional then routing-id, then publish

`device_index` has two sources in sequence. A provisional value is taken so that `fw_io_setup` and the subsequent FWIO routing-id read have a working `nd`; the real index then overwrites it inside the DHAL callback:

```c
// neuron_pci_probe @ :420-433, :466-467
nd->device_index = atomic_fetch_add(1, &device_count);   // PROVISIONAL counter (:421)
                                                          //   (4.14-gated atomic_add_return-1 fallback :423)

nd->fw_io_ctx = fw_io_setup(bar0, bar0_size, bar2, bar2_size);   // readless-read ctx (:425)
if (nd->fw_io_ctx == NULL): { ret = -ENODEV; goto fail_bar4_resource; }   // (:427-431)

// DHAL callback reads the routing id over FWIO and OVERWRITES device_index:
ret = ndhal->ndhal_pci.neuron_pci_get_device_id(nd, dev);   // (:433)
if (ret): goto fail_bar4_resource;                          // (:434-435)
    //  _v2 (v2:867) / _v3 (v3:1139) / _v4 (v4:311): poll fw_io_device_id_read up to 20x w/ msleep(1000),
    //  reject 0xdeadbeef, bound routing_id < MAX_NEURON_DEVICE_COUNT(64);
    //  on collision -> neuron_pci_handle_dup_routing_id() (:71)
...
// after device_init / datastore / mc allocs / optional metrics:
BUG_ON(neuron_devices[nd->device_index] != NULL);   // slot MUST be empty (:466)
neuron_devices[nd->device_index] = nd;              // PUBLISH (:467)
return 0;
```

### Considerations

- **The provisional counter is never reclaimed.** `device_count` is a monotonically increasing `atomic_t` (`:55`); the fetch-add bump at `:421` is not undone on a probe failure after that point. It is only a *provisional* index (always overwritten at `:433` before publish), so a leaked count does not corrupt the registry — but `total_neuron_devices` (sampled from `device_count`, `:552`) is therefore an upper bound, not an exact device count, if any probe failed past `:421`. **(MED** — inference from the monotonic counter; no decrement path exists.**)**
- **The publish `BUG_ON` enforces one device per routing-id slot** (`:466`). Two devices resolving to the same routing id would otherwise overwrite a live registry entry; instead the second crashes the kernel. The dup-rid handler (below) is the *graceful* path that fires earlier, inside `neuron_pci_get_device_id`, before the publish is reached.

### The duplicate-routing-id self-unload

When a device's routing id collides with one already seen, the DHAL `get_device_id` callback calls `neuron_pci_handle_dup_routing_id` (`:71`). On the **first** dup encountered (`atomic_fetch_add(&dup_rid_cnt) == 0`) and if `dup_helper_enable` (default 1), it schedules the driver to unload itself:

```c
// neuron_pci_handle_dup_routing_id @ :71
function handle_dup_routing_id():
    dup_cnt = atomic_fetch_add(1, &dup_rid_cnt)            // (:77; 4.14-gated fallback :79)
    if dup_cnt == 0 && dup_helper_enable:                  // first dup only (:83)
        n = snprintf(cmd, 256, "sleep 10;/sbin/modprobe -r %s", module_name)  // (:86)
        if n >= sizeof(cmd): return -EINVAL                // buffer-overflow guard (:87-90)
        argv = {"/bin/sh", "-c", cmd, NULL}                // (:91-94)
        envp = {"HOME=/", "TERM=linux", "PATH=/sbin:/usr/sbin:/bin:/usr/bin", NULL}  // (:95-98)
        return call_usermodehelper(argv[0], argv, envp, UMH_WAIT_EXEC)   // (:100)
    return -ENODEV                                         // default (:72, :106)
```

> **NOTE —** the self-unload is fire-and-forget through a usermodehelper shell (`/bin/sh -c "sleep 10; modprobe -r neuron"`); it depends on `/sbin/modprobe` being present on the configured `PATH`. A reimplementer should treat a duplicate routing id as a fatal topology misconfiguration and surface it explicitly rather than relying on a 10-second-delayed shell to tear the module down.

---

## The Probe Sequence and Error-Unwind Ladder

### Purpose

The whole `neuron_pci_probe` body is a linear acquire sequence with a single descending `goto`-label unwind ladder: each acquisition has a label that releases it, and a failure jumps to the label *one rung below* the resource it failed to acquire, so the ladder releases exactly what was acquired. This is the canonical Linux probe-unwind idiom; the value of reproducing it precisely is that the rungs are non-obvious (the BAR reserve/map split doubles the rungs, and two paths skip the ladder entirely).

### Algorithm — the full probe with unwind

```c
// neuron_pci_probe @ neuron_pci.c:352
function neuron_pci_probe(dev, id):                       // id is unused; binding is by id_table
    nd = kvzalloc(sizeof(struct neuron_device))           // (:357)
    if nd == NULL: goto fail_alloc_nd_mem                 // (:358-361)

    nmetric_init_driver_metrics(nd)                        // (:363)
    if neuron_log_init(nd): pci_warn(...)                  // warn-only, not fatal (:365-367)
    nd->pdev = dev;  pci_set_drvdata(dev, nd)              // (:369-370)

    if pci_enable_device(dev):        goto fail_enable     // (:372-376)
    pci_set_master(dev)                                    // (:378)
    neuron_pci_set_device_architecture(nd)                 // LATCH arch (:381)
    if neuron_dhal_init(dev->device): goto fail_dhal_init  // install ndhal vtable (:383-387)

    // --- BAR map trio: each is reserve(*_map label) then set_npdev(*_resource label) ---
    if reserve_bar(dev, apb_bar, "APB"):  goto fail_bar0_map        // (:390-393)
    if set_npdev(dev, apb_bar, "APB", &bar0...): goto fail_bar0_resource  // (:394-397)
    if reserve_bar(dev, axi_bar, "AXI"):  goto fail_bar2_map        // no-op (:400-403)
    if set_npdev(dev, axi_bar, "AXI", &bar2...): goto fail_bar2_resource  // (:404-407)
    if reserve_bar(dev, dram_bar,"BAR4"): goto fail_bar4_map        // (:410-413)
    if set_npdev(dev, dram_bar,"BAR4", &bar4...): goto fail_bar4_resource // (:414-417)

    nd->device_index = atomic_fetch_add(1, &device_count)  // provisional (:421)
    nd->fw_io_ctx = fw_io_setup(bar0, bar0_size, bar2, bar2_size)         // (:425)
    if fw_io_ctx == NULL: { ret = -ENODEV; goto fail_bar4_resource }      // (:427-431)
    if ndhal_pci.neuron_pci_get_device_id(nd, dev): goto fail_bar4_resource  // routing id (:433-435)

    if neuron_pci_device_init(nd):     goto fail_bar4_resource   // DMA mask, reset thread, char node (:437-439)
    if neuron_ds_init(&nd->datastore,nd): goto fail_nds_resource // (:442-444)
    if mc_alloc_align(... &nd->ndma_q_dummy_mc): goto fail_nds_resource  // (:446-449)
    if mc_alloc_align(... &nd->memset_mc):       goto fail_memset_mc     // (:452-455)
    if nmetric_log_posts != 0 && nmetric_init(nd): goto fail_nmetric_resource  // (:457-462)
    mutex_init(&nd->memset_lock)                            // (:464)

    BUG_ON(neuron_devices[nd->device_index] != NULL)        // (:466)
    neuron_devices[nd->device_index] = nd                   // PUBLISH (:467)
    return 0

// ---- the unwind ladder (:471-494), each rung releases its resource then falls through ----
fail_nmetric_resource:  nmetric_stop_thread(nd); mc_free(&nd->memset_mc)   // (:472-473)
fail_memset_mc:         mc_free(&nd->ndma_q_dummy_mc)                       // (:475)
fail_nds_resource:      neuron_ds_destroy(&nd->datastore)                   // (:477)
fail_bar4_resource:     neuron_pci_release_bar(dev, dram_bar)               // (:479)
fail_bar4_map:          /* fallthrough */                                  // (:480)
fail_bar2_resource:     neuron_pci_release_bar(dev, axi_bar)               // (:482)
fail_bar2_map:          /* fallthrough */                                  // (:483)
fail_bar0_resource:     neuron_pci_release_bar(dev, apb_bar)               // (:485)
fail_bar0_map:          pci_disable_device(dev)                            // (:487)
fail_dhal_init:         /* fallthrough */                                  // (:488)
fail_enable:            neuron_log_destroy(nd); kvfree(nd)                  // (:490-491)
fail_alloc_nd_mem:      pci_set_drvdata(dev, NULL); return ret             // (:492-494)
```

The ladder structure is worth one observation a reimplementer needs: `fail_bar4_resource` (`:479`) is the catch-all for *every* failure from `fw_io_setup` (`:430`) through `device_init` (`:439`) — those steps acquire no BAR-level resource of their own and rely on `device_init`/`fw_io` having unwound themselves, so the ladder picks up at the BAR-release rung. The `*_map` labels (`:480, :483, :486`) are empty fallthroughs because a reserve failure leaves nothing of that BAR to release.

### Considerations

> **GOTCHA —** **two probe-stage error paths skip the unwind ladder entirely**, inside `neuron_pci_device_init` (called at `:437`). `nr_create_thread` failure does a bare `return ret` (`:132-133`) *before* any of the mch/mpset/chardev allocations, so nothing in `device_init` leaks on that path — but `nr_start_ncs` failure also does a bare `return ret` (`:166-167`) *after* `nmch_handle_init`, `mpset_constructor`, and `ncdev_create_device_node` have all succeeded, leaking the mc-handle map, the mempool set, and the char-device node. `device_init`'s own labels (`fail_chardev`/`fail_mpset`/`fail_mch`, `:171-179`) handle the *other* failures correctly; these two `return ret`s bypass them. The probe sees a non-zero `ret`, jumps to `fail_bar4_resource`, and releases the BARs — but never re-enters `device_init` to free what it allocated. A reimplementation must route both through `goto fail_chardev` (or the appropriate rung). **(MED** — the leak is an inference from the bypassed labels; the bare returns are direct reads.**)** Separately, `neuron_module_init` does **not** call `ncdev_module_exit` if `neuron_pci_module_init` fails after `ncdev_module_init` reserved the chrdev region (`neuron_module.c:79,83`) — a chrdev-region leak on that module-load error path.

---

## Remove — the Mirror Teardown

### Purpose

`neuron_pci_remove` (`:497`) is the unbind handler: it reverses the probe's acquisitions for one device. It is *not* a strict mirror of the probe ladder — BARs are released before `pci_disable_device` and the `iounmap`s run after `device_close` — but the orderings that differ (`release_region` vs `iounmap`) operate on independent kernel state, so the divergence is functionally safe.

### Algorithm

```c
// neuron_pci_remove @ :497
function neuron_pci_remove(dev):
    nd = pci_get_drvdata(dev)
    if nd == NULL: return                                  // never bound / already gone (:502-503)
    nr_stop_thread(nd)                                     // (:505)
    nmetric_stop_thread(nd)                                // (:507)
    ndhal->ndhal_ext_cleanup()                             // per-arch external cleanup (:509)
    neuron_pci_release_bar(dev, apb_bar)                   // (:511)
    neuron_pci_release_bar(dev, axi_bar)                   // no-op (:513)
    neuron_pci_release_bar(dev, dram_bar)                  // (:515)
    pci_disable_device(dev)                                // (:517)
    neuron_pci_device_close(nd)                            // nnq/ndmar/ds/mpset/mch/fw_io teardown (:519)
    if nd->npdev.bar0: pci_iounmap(dev, nd->npdev.bar0)    // (:521-523)
    if nd->npdev.bar2: pci_iounmap(dev, nd->npdev.bar2)    // bar2 always NULL -> skipped (:524-526)
    if nd->npdev.bar4: pci_iounmap(dev, nd->npdev.bar4)    // (:527-529)
    neuron_log_destroy(nd)                                 // (:531)
    kvfree(nd)                                             // (:533)
```

### Function Map

| Function | Line | Role | Confidence |
|---|---|---|---|
| `neuron_pci_probe` | `:352` | PCI bind handler; full acquire sequence + unwind ladder | HIGH |
| `neuron_pci_remove` | `:497` | PCI unbind handler; mirror teardown | HIGH |
| `neuron_pci_set_device_architecture` | `:205` | device-ID → `enum neuron_arch`, calls `narch_init` | HIGH |
| `neuron_pci_device_init` | `:109` | per-device bring-up (DMA mask, reset thread, char node) | HIGH |
| `neuron_pci_device_close` | `:182` | per-device teardown counterpart | HIGH |
| `is_valid_bar` | `:230` | BAR ∈ `{apb,axi,dram}_bar` predicate | HIGH |
| `neuron_pci_reserve_bar` | `:242` | `pci_request_region`, DRAM-tolerant | HIGH |
| `neuron_pci_set_npdev` | `:281` | record pa/size, `pci_iomap[_wc]`, DRAM-tolerant | HIGH |
| `neuron_pci_release_bar` | `:339` | `pci_release_region`, BAR_UNUSED no-op | HIGH |
| `neuron_pci_get_device` | `:64` | sole public accessor of `neuron_devices[]`; index `BUG_ON` | HIGH |
| `neuron_pci_handle_dup_routing_id` | `:71` | first-dup `modprobe -r` self-unload | HIGH |
| `neuron_pci_module_init` | `:543` | `pci_register_driver`; samples `total_neuron_devices` | HIGH |
| `neuron_pci_module_exit` | `:556` | `dhal_cleanup → unregister_driver → dhal_free` | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `neuron_dhal` vtable | supplies the BAR indices (`ndhal_pci`) and the routing-id `get_device_id` callback the probe consumes |
| `neuron_arch` singleton | the first-wins arch latch the probe fills before BAR mapping |
| `neuron_cdev` | `ncdev_create_device_node` (called from `device_init`) publishes `/dev/neuronN` keyed on `device_index` |
| `neuron_fw_io` | `fw_io_setup` builds the readless-read context off `bar0`; routing-id reads use it |
| `neuron_reset` | `nr_create_thread`/`nr_start_ncs` (in `device_init`) bring the NeuronCores out of reset |

## Cross-References

- [Kernel Driver Overview](overview.md) — the Part III map, the request lifecycle diagram, and the privilege model this probe sits under
- [Char Device, fops and mmap](cdev-mmap.md) — `ncdev_create_device_node` and the `/dev/neuronN` ⇔ `device_index` ⇔ `neuron_devices[N]` index identity
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](../arch/generations-enum.md) — the meaning of the latched arch, the EMU/QEMU revision axis, and the `0x7064` `MODULE_ALIAS` discrepancy (GEN-01 / PCI-01)
- [PCI Device-ID → Arch Map](../arch/pci-device-ids.md) — the full five-ID → three-arch → four-product reconciliation under vendor `0x1D0F`
- [Reset State Machine](reset.md) — `nr_create_thread`/`nr_start_ncs` (the two `device_init` steps whose bare returns leak), and the RESET→READY machine
- [back to index](../index.md)
