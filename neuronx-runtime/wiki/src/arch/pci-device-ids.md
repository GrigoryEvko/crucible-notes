# PCI Device-ID → Arch Map

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The identity table is read verbatim from `neuron_device.h` (the `#define`s) and `neuron_pci.c` (the `pci_device_id` table); the device-ID → arch switch from `neuron_pci.c`; the BAR indices from the per-arch DHAL register tables. The source is read directly, not reverse-engineered; every claim is anchored to its `#define` or statement line. Other driver versions renumber lines and may add device IDs. Part — Architecture · [back to index](../index.md)*

## Abstract

This page is the authoritative PCI identity table for the Neuron driver: the single vendor ID, the five accelerator device IDs the driver claims, the silicon generation each maps to, the AWS cloud-instance family that ships it, and the BAR layout the probe maps for each. It is the reference a reimplementer matches a device by — pick a `(vendor, device)` pair, read its row, learn which architecture enum the probe latches (`V2`/`V3`/`V4`), which DHAL vtable that selects, and which of the three BARs (APB register window, AXI, DRAM/HBM window) the driver reserves and `iomap`s. The detection axis is purely the PCI device ID plus one config byte (`PCI_REVISION_ID`); there is no subsystem-ID or class-code discrimination.

The mapping is small and static. One `struct pci_device_id` table (`neuron_pci.c:30-39`) lists five entries, all under Amazon/Annapurna vendor `0x1D0F`, plus the `{0,}` sentinel. The device-ID → arch decision is a five-case `switch` in `neuron_pci_set_device_architecture` (`neuron_pci.c:212-226`): two IDs collapse to `V2`, one to `V3`, two to `V4`. The BAR indices are *not* hardcoded in the PCI layer — they come from `ndhal->ndhal_pci.{apb_bar, axi_bar, dram_bar}`, set by the per-arch DHAL register table the probe selects after latching the arch. This page tabulates the resulting identity cross-table and the per-arch BAR set; the probe *sequence* (enable, set-master, reserve/map ladder, routing-id resolution, registry publish, error unwind) is the property of [pci-probe](../kernel/pci-probe.md) and is linked, not re-derived here.

| | |
|---|---|
| **Vendor ID** | `AMZN_VENDOR_ID 0x1D0F` (Annapurna Labs / Amazon) (`neuron_device.h:35`) |
| **Device IDs** | `0x7164` `0x7264` `0x7364` `0x7564` `0x7565` (`neuron_device.h:36-40`) |
| **`id_table`** | `neuron_pci_dev_ids[]` — 5 entries + sentinel (`neuron_pci.c:30-39`) |
| **ID → arch decision** | `switch(device)` in `neuron_pci_set_device_architecture` (`neuron_pci.c:212-226`) |
| **Arch enum** | `NEURON_ARCH_V2=2` / `V3=3` / `V4=4` (`neuron_arch.h:12-18`) — no value 1 |
| **BAR-index source** | `ndhal->ndhal_pci.{apb_bar,axi_bar,dram_bar}` (`neuron_dhal.h:137-139`); `BAR_UNUSED=-1` (`neuron_pci.h:13`) |
| **Confidence** | HIGH — vendor/device IDs, the switch, and the `id_table` read verbatim from the shipped source |

---

## The Device-ID Table

Every device the driver claims is keyed solely by `(0x1D0F, device_id)`. The five IDs span three generations and four cloud-instance families; `TRN3` carries two device IDs that fold to the same arch. The arch column is the value `narch_init` caches (`neuron_pci.c:227` → `neuron_arch.c:33`); the BAR-set column names the DHAL BAR profile that arch selects (detailed in the next section).

| Device ID | `#define` | Arch / gen | Cloud instance | BAR set | Notes | Conf |
|---|---|---|---|---|---|---|
| `0x7164` | `TRN1_DEVICE_ID0` (`neuron_device.h:37`) | `NEURON_ARCH_V2` (=2) | Trainium1 (Trn1) | V2 | shares arch V2 with Inf2; `case` at `neuron_pci.c:213` | HIGH |
| `0x7264` | `INF2_DEVICE_ID0` (`neuron_device.h:36`) | `NEURON_ARCH_V2` (=2) | Inferentia2 (Inf2) | V2 | shares arch V2 with Trn1; `case` at `neuron_pci.c:214` | HIGH |
| `0x7364` | `TRN2_DEVICE_ID0` (`neuron_device.h:38`) | `NEURON_ARCH_V3` (=3) | Trainium2 (Trn2) | V3 | `case` at `neuron_pci.c:217` | HIGH |
| `0x7564` | `TRN3_DEVICE_ID0` (`neuron_device.h:39`) | `NEURON_ARCH_V4` (=4) | Trainium3 (Trn3) | V4 (= V3 indices) | first of two Trn3 IDs; `case` at `neuron_pci.c:220` | HIGH |
| `0x7565` | `TRN3_DEVICE_ID1` (`neuron_device.h:40`) | `NEURON_ARCH_V4` (=4) | Trainium3 (Trn3) | V4 (= V3 indices) | second Trn3 ID, same arch; `case` at `neuron_pci.c:221` | HIGH |

The `id_table` lists these in a different order than the `#define`s — `neuron_pci.c:31-35` enumerates `TRN1, INF2, TRN2, TRN3_ID0, TRN3_ID1` — but match order is irrelevant: each `PCI_DEVICE(...)` row is an exact `(vendor, device)` equality, and the kernel binds on the first exact hit. The `{0,}` row (`neuron_pci.c:36-38`) is the mandatory null terminator.

> **NOTE —** two facts a reimplementer must not over-read. (1) The arch is the *silicon generation*, not the instance brand: `V2` is genuinely two distinct products (Trn1 training, Inf2 inference) that share one chip family and therefore one DHAL vtable. (2) `V4` does **not** install its own BAR indices — it inherits the V3 BAR profile and overrides only the routing-id callback (see the next section). The full marketing-name correspondence and the deliberate absence of a `V1`/enum-value-1 are owned by [generations-enum](generations-enum.md).

> **QUIRK —** an unknown device ID is reachable *only* through manual `new_id`/`sysfs` binding, never through the `id_table`. If it does occur, the `switch` `default` (`neuron_pci.c:224-225`) `return`s **without** calling `narch_init`, leaving `arch_info.arch == NEURON_ARCH_INVALID`. The very next probe step, `neuron_dhal_init` (`neuron_pci.c:383`), then reads back `narch_get_arch()`, whose `BUG_ON(arch == INVALID)` (`neuron_arch.c:44`) panics. There is no graceful "unknown device" path; binding an unlisted ID by hand crashes the probe.

---

## The BAR Layout

The driver maps up to three BARs per device. Their *roles* are fixed (APB register/CSR window, an AXI window, a DRAM/HBM window), but their *PCI BAR indices* are supplied per-arch by the DHAL through `struct ndhal_pci` (`neuron_dhal.h:136-144`), not by the PCI layer. The descriptor that receives the mapping is `struct neuron_pci_device` (`neuron_device.h:45-55`), whose `bar0`/`bar2`/`bar4` fields are *names*, not indices — `bar0` ← `apb_bar`, `bar2` ← `axi_bar`, `bar4` ← `dram_bar`.

### Role → field → index

| BAR role | `npdev` field (`neuron_device.h`) | DHAL index field | Index (all shipping arches) | Purpose | Conf |
|---|---|---|---|---|---|
| APB / MMIO register window | `bar0_pa` / `bar0` / `bar0_size` (`:46-48`) | `apb_bar` (`neuron_dhal.h:137`) | `0` | CSR / MISC RAM register window; the primary FWIO target (`neuron_pci.c:425`) | HIGH |
| AXI window | `bar2_pa` / `bar2` / `bar2_size` (`:49-51`) | `axi_bar` (`neuron_dhal.h:138`) | `BAR_UNUSED` (`-1`) | unused on every shipping arch — reserve/map/release are no-ops, `bar2` stays `NULL` | HIGH |
| DRAM / HBM window | `bar4_pa` / `bar4` / `bar4_size` (`:52-54`) | `dram_bar` (`neuron_dhal.h:139`) | `4` | direct HBM window; WC-mapped when `wc_enable` (`neuron_pci.c:311-312`); failure-tolerated | HIGH |

### Per-arch index profile

The BAR indices come from the per-arch DHAL register tables (a boundary owned by the DHAL cell), latched into `ndhal->ndhal_pci` before the probe's reserve/map ladder runs:

| Arch | `apb_bar` | `axi_bar` | `dram_bar` | Source profile | Conf |
|---|---|---|---|---|---|
| V2 (Trn1, Inf2) | `0` | `BAR_UNUSED` (`-1`) | `4` | DHAL v2 register table | HIGH |
| V3 (Trn2) | `0` | `BAR_UNUSED` (`-1`) | `4` | DHAL v3 register table | HIGH |
| V4 (Trn3) | `0` | `BAR_UNUSED` (`-1`) | `4` | inherits V3 base; V4 overrides routing-id only | MEDIUM |

The DRAM BAR *size* is not a compile-time constant — it is read from the hardware (`pci_resource_len`) at probe time and cached into `ndhal->ndhal_pci.dram_bar_size` (`neuron_pci.c:307-308`) for the DHAL to consume. No per-device byte size is fixed in the PCI source; the only static size constant near the BAR path is the host scratch buffer `MEMSET_HOST_BUF_SIZE = MAX_DMA_DESC_SIZE` (`neuron_device.h:43`), which is host memory, not a BAR.

> **GOTCHA —** the DRAM BAR is the *only* failure-tolerated BAR. Both `neuron_pci_reserve_bar` (`neuron_pci.c:262-267`) and `neuron_pci_set_npdev` (`neuron_pci.c:321-329`) treat a reserve/map failure as fatal (`-ENODEV`) for any BAR **except** `dram_bar`, where they zero `bar4_pa`/`bar4_size`/`bar4` and `return 0`. A reimplementer who fails the whole probe on a missing HBM window diverges from the driver: some hosts legitimately do not expose BAR4, and the driver runs without it. APB (`bar0`) failure, by contrast, aborts the probe.

> **NOTE —** because `axi_bar == BAR_UNUSED` on every shipping arch, the `is_valid_bar` guard (`neuron_pci.c:230-231`) admits `-1`, and the early `bar == BAR_UNUSED → return 0` short-circuit (`neuron_pci.c:249-251`, `:291-293`) makes the AXI reserve/map a silent no-op. `bar2`/`bar2_size` therefore stay zero, yet `fw_io_setup` (`neuron_pci.c:425-426`) is still handed them — FWIO drives the readless-read path off `bar0` and tolerates a zero AXI window.

---

## Resolving an ID to an Arch

Detection is the device ID plus one config byte, decided in `neuron_pci_set_device_architecture` and committed by `narch_init`'s first-wins cache. The annotated form:

```c
function neuron_pci_set_device_architecture(nd):       // neuron_pci.c:205
    device   = nd->pdev->device                         // :207  the 16-bit PCI device ID
    arch     = NEURON_ARCH_INVALID                       // :208
    pci_read_config_byte(nd->pdev, PCI_REVISION_ID, &revision)  // :210  one byte; also EMU/QEMU marker

    switch (device):                                     // :212
        case TRN1_DEVICE_ID0 (0x7164):                   // :213
        case INF2_DEVICE_ID0 (0x7264):                   // :214
            arch = NEURON_ARCH_V2; break                 // :215-216
        case TRN2_DEVICE_ID0 (0x7364):                   // :217
            arch = NEURON_ARCH_V3; break                 // :218
        case TRN3_DEVICE_ID0 (0x7564):                   // :220
        case TRN3_DEVICE_ID1 (0x7565):                   // :221
            arch = NEURON_ARCH_V4; break                 // :222
        default:
            return                                       // :224-225  unknown ID: arch stays INVALID, no latch

    narch_init(arch, revision)                           // :227  first-wins cache; ignores later devices
```

`narch_init` (`neuron_arch.c:33`) caches `arch`+`revision` into a file-static singleton **on the first call only** — if `arch_info.arch != NEURON_ARCH_INVALID` it returns immediately (`neuron_arch.c:36-37`). The whole driver is therefore single-arch per load: the first probed device's generation wins, and a mixed-silicon host is unsupported by construction. The cached arch is read back by `neuron_dhal_init` (`neuron_pci.c:383`) to select the V2/V3/V4 ops vtable — which is where the BAR indices above originate. The `revision` byte doubles as the simulator discriminator (`REVID_EMU=240`, `REVID_QEMU=255`); that axis is owned by [generations-enum](generations-enum.md) and [arch overview](overview.md).

> **CORRECTION (PCI-01) —** the udev autoload `MODULE_ALIAS` is `pci:v00001d0fd00007064sv*sd*bc*sc*i*` (`neuron_module.c:24`) — device-ID **`0x7064`**, which is **not** any of the five IDs in `neuron_pci_dev_ids[]`. `0x7064` appears nowhere else in the tree. Runtime binding is driven by the `id_table`, so a *loaded* driver still matches its devices, but udev keys autoload on the modalias, so by-modalias autoload is effectively broken for every supported part. A reimplementer must generate the alias from the table via `MODULE_DEVICE_TABLE(pci, neuron_pci_dev_ids)` rather than hand-write this stale line. This quirk is the property of [module-layout](../kernel/module-layout.md#5-module-metadata-and-packaging) (tracked there as PCI-01); it is recorded here because it is a PCI identity fact. **(HIGH** — direct source read.**)**

---

## Cross-References

- [PCI Probe and Device Detection](../kernel/pci-probe.md) — the probe sequence, the reserve/map BAR ladder, routing-id resolution, the registry publish, and the error-unwind ladder this page deliberately does not re-derive
- [Silicon & Architecture Model — Runtime Lens](overview.md) — how the latched arch fans out across the runtime; the EMU/QEMU revision axis
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](generations-enum.md) — the arch enum (`V2=2`/`V3=3`/`V4=4`, no value 1), the marketing-name mapping, and platform-type (STD/ULTRASERVER/PDS)
- [Module Layout and Build](../kernel/module-layout.md) — the `MODULE_*` metadata and the stale `0x7064` `MODULE_ALIAS` (PCI-01) that breaks modalias autoload
