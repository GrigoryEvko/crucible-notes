# Generations, the V2/V3/V4 Enum, and Cloud Naming

> **Kernel:** `aws-neuronx-dkms 2.27.4.0` (GPL-2.0 source; build SHA `1c7ed9bd14936635773b5a01777882804ee8ea6e`, `neuron_module.c:21,27`). Cited `file:line` into `extracted/aws-neuronx-dkms_2.27.4.0_all/usr/src/aws-neuronx-2.27.4.0/`.
> **Runtime lib:** `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` · **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64, unstripped, DWARF present; `.text`/`.rodata` VMA == file offset).
> **Status:** Reimplementation-grade · **Evidence grade:** `file:line` (kernel) / symbol+addr (runtime) anchored · **Part I — Silicon & Architecture Model**, DEEP · [back to overview](overview.md)

## Abstract

There is exactly **one generation number** threaded through the entire Neuron driver, and it is decided in **one place**, **once**. The kernel calls it `enum neuron_arch` and gives it three live values — `NEURON_ARCH_V2 = 2`, `V3 = 3`, `V4 = 4` (`neuron_arch.h:12-18`). Every later question the driver asks about "what silicon is this" — which BAR map, which register-offset math, which reset sequence, whether to expect a pod — bottoms out in a read of this one cached integer. The [overview](overview.md#the-generation-model--two-vocabularies-one-enum-value) established the cross-layer invariant (kernel V2/V3/V4 == runtime `al_hal_tpb_arch_type` SUNDA/CAYMAN/MARIANA, same integers, deliberate gap at 0/1); this page is the **DEEP derivation of how that integer is produced, latched, and dressed in marketing names**, plus the two orthogonal axes layered on top of it: the EMU/QEMU revision discriminator and the STD/ULTRASERVER/PDS platform type.

The mental frame is a familiar one: a device driver that supports several ASIC revisions usually keeps a small "chip ident" struct, fills it on `probe()` from the PCI device-ID, and hangs an ops vtable off it. The Neuron driver is exactly this, with one aggressive simplification — it assumes **a host contains exactly one chip type** (`neuron_arch.c:6-8`). So the ident is not per-device; it is a **single file-static singleton**, `arch_info`, latched **first-wins** by the first device to probe and served unchanged to every subsequent query (`neuron_arch.c:25-39`). A second, differently-typed device on the same host has its arch silently dropped. This is the central design decision of the whole arch layer, and the rest of the page is the consequences of it: the device-ID→enum switch that feeds the latch, the `BUG_ON`-guarded accessors that enforce "no query before probe", the V4-on-V3 DHAL piggyback that the latch ordering makes safe, and the two non-PCI axes (revision-as-EMU-marker, DMI-product-name-as-platform-type) that ride alongside the same singleton.

The naming is split three ways and a reimplementer must keep them separate. (1) The **silicon enum** — the integer above, the only thing dispatch keys on. (2) The **PCI device-IDs** — five 16-bit values under vendor `0x1D0F` that the switch maps onto the enum (two of them collapse to one arch). (3) The **cloud / marketing names** — Trn1, Inf2, Trn2, Trn3, plus the UltraServer/PDS *pod* names — which appear only in comments, sysfs suffixes, and DMI product-name string matches, and which are **never** keyed on by dispatch. The trap is to route logic on a name; the contract is to route on the integer.

For reimplementation, the contract this page fixes is:

- **The enum and its exact values** — `{INVALID=0, V2=2, V3=3, V4=4, NUM=5}`, the intentional absence of value 1, and the `neuron_platform_type {STD=0, ULTRASERVER=1, PDS=2, INVALID=3}` second axis.
- **The detection/latching algorithm** — the device-ID→arch switch (`neuron_pci.c:205-228`), the first-wins `narch_init` latch (`neuron_arch.c:33`), the `BUG_ON`-on-`INVALID` accessor contract, and the strict ordering (arch cached **before** DHAL init reads it back).
- **The V4-on-V3 DHAL piggyback** — V4 registers the V3 ops vtable first then patches V4 deltas (`neuron_dhal.c:44-45`), which is correct *only because* the arch is already latched as `V4`, not `V3`.
- **The two orthogonal axes** — EMU/QEMU gating from the PCI revision byte (`REVID_EMU=0xF0`/`REVID_QEMU=0xFF`, `neuron_arch.c:30-31`), and STD/ULTRASERVER/PDS platform-type from the DMI `product_name` string (V3/V4 only).

## At a glance

| | |
|---|---|
| **The enum** | `enum neuron_arch` `{INVALID=0, V2=2, V3=3, V4=4, NUM=5}` (`neuron_arch.h:12-18`) — **no value 1** |
| **The singleton** | `static struct neuron_arch_info arch_info = {INVALID, 0}` (`neuron_arch.c:21-28`) — first-wins latch |
| **Latch fn** | `narch_init(arch, revision)` (`neuron_arch.c:33`) — early-return if already latched (`:36-37`) |
| **Detection fn** | `neuron_pci_set_device_architecture` (`neuron_pci.c:205`) — device-ID switch → `narch_init` (`:227`) |
| **Accessors** | `narch_get_arch`/`_revision`/`_is_emu`/`_is_qemu` (`neuron_arch.c:42/48/54/60`) — `BUG_ON` if still `INVALID` |
| **Device-IDs** | `0x7164`Trn1/`0x7264`Inf2→V2 · `0x7364`Trn2→V3 · `0x7564`/`0x7565`Trn3→V4 (vendor `0x1D0F`, `neuron_device.h:35-40`) |
| **EMU/QEMU markers** | PCI revision `REVID_EMU=240(0xF0)`/`REVID_QEMU=255(0xFF)` (`neuron_arch.c:30-31`) |
| **Platform axis** | `enum neuron_platform_type` `{STD=0, ULTRASERVER=1, PDS=2, INVALID=3}` (`neuron_arch.h:20-25`) — V3/V4 only, DMI-matched |
| **DHAL dispatch** | `neuron_dhal_init` switch (`neuron_dhal.c:36-51`); V4 = `register_funcs_v3()` + `register_funcs_v4()` |
| **Single-chip assumption** | host holds one chip type only (`neuron_arch.c:6-8`); mixed-arch hosts unsupported by construction |

---

## The Enum — three live values, two intentional gaps

### Purpose

`enum neuron_arch` is the silicon-generation taxonomy: the integer the whole driver dispatches on. It maps AWS silicon *generations* — not user-facing instance names — onto a small dense-ish space.

### Encoding

The verbatim enum (`neuron_arch.h:12-18`):

```c
enum neuron_arch {
    NEURON_ARCH_INVALID = 0,   // sentinel; also the latch's "not yet set" state
    // -- no value 1 --        // intentional gap (matches DHAL dirs v2/ v3/ v4/)
    NEURON_ARCH_V2      = 2,    // Trainium1 (Trn1)  + Inferentia2 (Inf2)
    NEURON_ARCH_V3      = 3,    // Trainium2 (Trn2)
    NEURON_ARCH_V4      = 4,    // Trainium3 (Trn3)
    NEURON_ARCH_NUM     = 5,    // array-bound terminator (one past V4)
};
```

Two facts a reimplementer must encode exactly, not "clean up":

- **There is no value `1`.** The gap between `INVALID(0)` and `V2(2)` is deliberate — the numbering matches the DHAL source directory names `v2/`, `v3/`, `v4/`, so an earlier (V1) generation is simply not represented in this driver. A reimplementer who "tidies" the enum to `{INVALID=0, V2=1, V3=2, V4=3}` will mis-index every arch-keyed table the runtime shares with the kernel — the runtime's own `al_hal_tpb_arch_type` keeps `INVALID_1=1` as a reserved slot for exactly this reason ([overview](overview.md#the-generation-model--two-vocabularies-one-enum-value)).
- **`INVALID=0` is dual-purpose.** It is both the enum sentinel and the latch's "not yet latched" state — and it is initializer-safe because `arch_info` is statically initialized to `{INVALID, 0}` (`neuron_arch.c:25-28`), so the first-wins guard works before any probe.

> **GOTCHA —** the enum value is the contract; the marketing name is decoration. `V2 = 2` is a *single* arch shared by two distinct products (Trn1 and Inf2) and two distinct PCI device-IDs (`0x7164`, `0x7264`). Any logic that branches on "is this Trn1 vs Inf2" by reading `neuron_arch` is wrong — at the arch layer they are indistinguishable. The only place that distinction survives is the device-ID itself (read again in V2's `get_device_id` callback for the TRN1 routing-id remap, `v2/neuron_dhal_v2.c:867`).

### Function Map

| Function | Addr / line | Role | Confidence |
|---|---|---|---|
| `narch_init(arch, rev)` | `neuron_arch.c:33` | First-wins latch of `arch_info` | HIGH |
| `narch_get_arch()` | `neuron_arch.c:42` | Return cached arch; `BUG_ON` if `INVALID` (`:44`) | HIGH |
| `narch_get_revision()` | `neuron_arch.c:48` | Return cached PCI revision byte; `BUG_ON` if `INVALID` (`:50`) | HIGH |
| `narch_is_emu()` | `neuron_arch.c:60` | `revision == REVID_EMU(240)` | HIGH |
| `narch_is_qemu()` | `neuron_arch.c:54` | `revision == REVID_QEMU(255)` | HIGH |
| `narch_get_instance_type_name(buf, sz)` | `neuron_arch.c:66` | DMI `product_name` reader (platform-type source) | HIGH |

---

## Detection and Latching — the one-place, one-time decision

### Purpose

The arch is decided by reading the PCI device-ID of the *first* Neuron function the kernel binds, mapping it through a switch onto `enum neuron_arch`, and caching the result (plus the PCI revision byte) into the `arch_info` singleton. Everything downstream reads the singleton; nothing else re-derives the arch.

### Entry Point

The detection is wired into the PCI probe path. `neuron_pci_probe` calls the architecture setter *before* DHAL init, and that ordering is the central invariant:

```text
neuron_pci_probe (neuron_pci.c:352)                       ── PCI bind handler (per device)
  ├─ pci_enable_device / pci_set_master                   ── (:372 / :378)
  ├─ neuron_pci_set_device_architecture (neuron_pci.c:205)── device-ID switch → narch_init
  │     └─ narch_init(arch, revision) (neuron_arch.c:33)  ── LATCH (first-wins)
  └─ neuron_dhal_init(dev->device) (neuron_dhal.c:10)     ── reads arch back, installs ops vtable
        └─ narch_get_arch() (neuron_arch.c:42)            ── MUST see the latched value
```

The probe enforces the order by call sequence (`neuron_pci.c:381-383`): `set_device_architecture` at `:381`, `neuron_dhal_init` at `:383`. If a reimplementer reverses these, `neuron_dhal_init` reads `arch_info.arch` while it is still `INVALID`, trips the `BUG_ON` in `narch_get_arch` (`:44`), and panics the kernel at module load.

### Algorithm

The full detection-and-latch logic, modeling `neuron_pci_set_device_architecture` (`neuron_pci.c:205-228`), `narch_init` (`neuron_arch.c:33-39`), and `neuron_dhal_init`'s arch switch (`neuron_dhal.c:32-51`):

```c
// ---- STEP 1: device-ID -> arch, then latch ----
// neuron_pci_set_device_architecture @ neuron_pci.c:205
function set_device_architecture(nd):
    device   = nd->pdev->device                         // PCI device-ID  (:207)
    revision = pci_read_config_byte(PCI_REVISION_ID)    // single byte     (:210)

    switch (device):                                    // neuron_device.h:36-40 constants
        case TRN1_DEVICE_ID0(0x7164):                   // Trainium1
        case INF2_DEVICE_ID0(0x7264):                   // Inferentia2  -> SAME arch
            arch = NEURON_ARCH_V2                        // (:213-216)
        case TRN2_DEVICE_ID0(0x7364):                   // Trainium2
            arch = NEURON_ARCH_V3                        // (:217-218)
        case TRN3_DEVICE_ID0(0x7564):                   // Trainium3
        case TRN3_DEVICE_ID1(0x7565):                   // Trn3, 2nd id -> SAME arch
            arch = NEURON_ARCH_V4                        // (:220-222)
        default:
            return                                      // unknown id: NO latch, arch stays INVALID (:224-225)

    narch_init(arch, revision)                          // (:227)

// ---- STEP 2: the first-wins latch ----
// narch_init @ neuron_arch.c:33
function narch_init(arch, revision):
    if arch_info.arch != NEURON_ARCH_INVALID:           // already latched by an earlier device
        return                                          // FIRST-WINS: later devices ignored (:36-37)
    arch_info.arch     = arch                            // (:38)
    arch_info.revision = revision                        // (:39)
    // no lock: relies on PCI probe serialization (single-threaded per pci_register_driver)

// ---- STEP 3: read it back, install the per-arch ops vtable ----
// neuron_dhal_init @ neuron_dhal.c:10
function neuron_dhal_init(pci_device_id):
    if ndhal != NULL: return 0                          // one-time global init (:13-15)
    ndhal = kmalloc(sizeof(struct neuron_dhal))         // NOTE: kmalloc, NOT kzalloc (:21)
    ndhal->ndhal_arch.arch = narch_get_arch()           // BUG_ON here if STEP 1 didn't run (:32)
    ndhal_register_funcs_vc()                            // common ops, always first (:34)

    switch (ndhal->ndhal_arch.arch):
        case NEURON_ARCH_V2: ndhal_register_funcs_v2()  // (:37-38)
        case NEURON_ARCH_V3: ndhal_register_funcs_v3()  // (:40-41)
        case NEURON_ARCH_V4:
            ndhal_register_funcs_v3()                   // V4 = V3 BASE ...   (:44)
            ndhal_register_funcs_v4()                   // ... + V4 OVERRIDES (:45)
        default:
            pr_err("Unknown HW architecture")           // (:47-50)
            return -EINVAL
    return 0
```

### Considerations

- **The single-chip-type assumption is a hard constraint, not a soft default.** Because `narch_init` is first-wins and `neuron_dhal_init` is a one-time global guarded by `ndhal != NULL` (`:13-15`), a host with a Trn2 (V3) card and a Trn3 (V4) card has *one* arch — whichever probed first — and *one* ops vtable. The second device is bound, its BARs mapped, and it is published into `neuron_devices[]`, but it will be driven through the *wrong* arch's register math. The source states the assumption explicitly (`neuron_arch.c:6-8`); the driver does **not** reject the mismatched second device. A reimplementer targeting heterogeneous hosts must replace the singleton with a per-device ident.

- **`default:` in the switch is silent, not fatal — yet still crashes.** An unknown device-ID returns from `set_device_architecture` *without* calling `narch_init` (`:224-225`), leaving `arch_info.arch == INVALID`. The probe then proceeds to `neuron_dhal_init`, where `narch_get_arch`'s `BUG_ON` fires — so an unrecognized but vendor-matched device crashes the driver rather than failing the probe gracefully. In practice this is unreachable because the PCI `id_table` (`neuron_pci.c:30-39`) only binds the five known IDs, so the switch is total over what can actually probe. The two lists must stay in sync: adding a device-ID to the `id_table` without adding a switch case is a latent panic.

  > **CORRECTION (GEN-01) —** the `MODULE_ALIAS` for udev autoload is `pci:v00001d0fd00007064sv*…` — device-ID `0x7064` (`neuron_module.c:24`), which is **not** any of the five IDs in the `id_table` (TRN1 `0x7164` / INF2 `0x7264` / TRN2 `0x7364` / TRN3 `0x7564`/`0x7565`). `0x7064` appears nowhere else in the source tree. Runtime binding is driven by `id_table`, so a loaded driver still matches; but the autoload *alias* points at a device-ID the driver does not claim. This is a stale/wrong alias for udev-based autoload, not a detection bug — flagged for [PCI Probe and Device Detection](../kernel/pci-probe.md). (HIGH)

- **The PCI `id_table` order is TRN1, INF2, TRN2, TRN3_ID0, TRN3_ID1** (`neuron_pci.c:30-39`), each via `PCI_DEVICE(AMZN_VENDOR_ID, …)` + a `{0,}` sentinel. Order does not affect arch resolution (the switch is keyed on the matched device's own ID, not table position).

- **No locking on the latch.** `narch_init` takes no lock and relies on `pci_register_driver` calling `neuron_pci_probe` serially. This holds for the in-tree probe model; a reimplementer that probes devices concurrently must add a `cmpxchg` or a lock around the first-wins check.

---

## The V4-on-V3 DHAL Piggyback

### Purpose

V4 (Trn3) silicon is, at the driver level, *V3 plus deltas*. Rather than duplicate the entire V3 ops vtable, `neuron_dhal_init` installs the **full V3 register set first, then overwrites only the V4-specific slots** (`neuron_dhal.c:44-45`). This is why the kernel ships `v3/` and `v4/` DHAL directories where `v4/` is small — it carries only overrides.

### Algorithm

```c
// neuron_dhal.c:44-45 (the V4 case of the arch switch)
case NEURON_ARCH_V4:
    ndhal_register_funcs_v3()   // installs the complete V3 ops vtable as the BASE
    ndhal_register_funcs_v4()   // patches only the slots that differ on Trn3
```

The correctness of this rests entirely on the latch from STEP 2 above: `arch_info.arch` is `V4`, **not** `V3`. So although V4 *executes V3's register function*, every consumer that later calls `narch_get_arch()` sees `V4`, and any code that needs the true generation (e.g. the V4 platform-type matcher, below) keys correctly. If the latch instead stored `V3` for a Trn3 device, the V4 overrides in their slots would be installed but `narch_get_arch()` would report the wrong generation to userspace (`narch_fill_device_basic_info`, `neuron_cdev.c:1786`) and to any arch-conditional path that does not go through the vtable.

### Considerations

- **The V4 platform-type registration is itself a V4 override**, installed by `ndhal_register_funcs_v4()` (which sets `ndhal_arch.platform_type = ndhal_platform_type_v4()`, `v4:438`). V3 installs the analogous `ndhal_platform_type_v3()` (`v3:1876`). So the platform axis is set *during* the same register call — see the next section.

- **The V4 case swallows the V3 return value.** `neuron_dhal.c:44-45` overwrites `ret` from `ndhal_register_funcs_v3()` with the V4 call's return — a V3-base init failure under V4 is masked. **(MEDIUM** — a real but benign-in-practice ordering quirk; flagged for [DHAL Core](../kernel/dhal-core.md).**)**

- **`ndhal` is `kmalloc`'d, not `kzalloc`'d** (`neuron_dhal.c:21`). V2's register path never writes `ndhal_arch.platform_type` (V2 has no pod concept), so on Trn1/Inf2 that field holds indeterminate heap garbage. A grep of `v2/neuron_dhal_v2.c` finds **zero** reads of `platform_type`, so the garbage is never consumed on V2 today — but a future cross-cell V2 reader would read uninitialized memory. **(MEDIUM** — "never read" verified only within the V2 file.**)**

---

## The EMU/QEMU Axis — revision as a simulator marker

### Purpose

The PCI **revision byte** — latched alongside the arch in `arch_info.revision` — doubles as the **simulator discriminator**. Two "impossible" real-silicon revision values mark emulation and QEMU; any other revision means real hardware. This is orthogonal to the arch: a V2, V3, or V4 device can all be emulated, and the same two markers apply.

### Encoding

```c
// neuron_arch.c:30-31
#define REVID_EMU   240    // 0xF0  -> narch_is_emu()  true
#define REVID_QEMU  255    // 0xFF  -> narch_is_qemu() true
// any other revision byte  -> real silicon
```

Both accessors are pure equality compares against the cached byte (`narch_is_emu` `:60`, `narch_is_qemu` `:54`), each `BUG_ON`-guarded against an unlatched `arch_info` (`:62`/`:56`) — so, like the arch accessors, they may not be called before a device has probed.

### Considerations

The markers gate **HW-bypass** paths across the data plane and DHAL. The consumers and what each one skips (all consumers are `BOUNDARY` to this cell, cited only to close the call chain):

| Consumer | Site | Simulator-only behavior |
|---|---|---|
| BAR4 write path | `neuron_cdev.c:1195,1340` | gate device-DRAM writes on `!narch_is_qemu` |
| DMA completion wait | `neuron_dma.c:240` | skip HW completion-timing wait |
| Power telemetry | `neuron_power.c:266` | power readings N/A on a simulator |
| DRAM sizing (V3/V4) | `v3:968`, `v4:253` | dynamic HBM window = `dram_bar_size/4` instead of PCI-config size |
| Relaxed checks (V2/V3/V4) | `v2:702,1473,1479`; `v3:648,916,1970,1980`; `v4:210,447` | relax BAR / completion checks |

The pattern is uniform: a simulator gets **dynamic HBM sizing** and **skips hardware-timing waits**. A reimplementer's BAR-probe and DMA-completion paths must therefore branch on these two revision values, *not* assume real-silicon sizes and timings. The kernel's emulation-driven `dram_bar_size/4` sizing is cross-referenced from the [overview](overview.md#the-memory-planes-bar-layout) BAR section.

> **NOTE —** `REVID_EMU`/`REVID_QEMU` are chosen precisely because no shipping silicon revision is `0xF0` or `0xFF`. A reimplementer assigning revision IDs to real parts must keep those two values reserved, or the driver will mistake a real device for a simulator and take every bypass path.

---

## The Platform-Type Axis — STD / ULTRASERVER / PDS

### Purpose

`enum neuron_platform_type` is a **second, orthogonal axis**: it answers "is this device part of a multi-node *pod*?" — independent of the silicon generation. It is **not** derived from PCI; it is read from the host's DMI `product_name` (the EC2 instance type) and string-matched in the per-arch DHAL. It exists only on V3/V4 (V2 has no pod concept).

### Encoding

```c
// enum neuron_platform_type  (neuron_arch.h:20-25)
NEURON_PLATFORM_TYPE_STD         = 0,   // not a pod; election thunks short-circuit
NEURON_PLATFORM_TYPE_ULTRASERVER = 1,   // UltraServer: PCIe B-link ring election
NEURON_PLATFORM_TYPE_PDS         = 2,   // PDS server: LOCATION-based, no link election
NEURON_PLATFORM_TYPE_INVALID     = 3,   // loop/default sentinel
```

### Entry Point

The source of the string is the one OS-touching function in the whole arch cell:

```text
ndhal_register_funcs_v{3,4}()                            ── per-arch ops install (DHAL)
  └─ ndhal->ndhal_arch.platform_type = ndhal_platform_type_v{3,4}()
        └─ narch_get_instance_type_name(buf, 128)        ── neuron_arch.c:66
              └─ kernel_read_file_from_path("/sys/class/dmi/id/product_name", …)  (:77-78)
```

`narch_get_instance_type_name` (`neuron_arch.c:66`) `kzalloc`s a `PAGE_SIZE` buffer, reads up to **64 bytes** of `/sys/class/dmi/id/product_name`, `snprintf`s into the caller buffer, and `kfree`s. It returns `0` on success, `-ENOMEM` on alloc failure (`:72-75`), `-EIO` if the read returns zero length (`:79-83`), or `-ENOSYS` on kernels older than 5.10.0 — the whole body is `#if LINUX_VERSION_CODE >= KERNEL_VERSION(5,10,0)` (`:67`, `:89-91`).

### Algorithm

The per-arch string matching (modeling `ndhal_platform_type_v3` `v3:240-263` and `ndhal_platform_type_v4` `v4:156-174`):

```c
// V3 (Trn2) platform-type — v3/neuron_dhal_v3.c:240
function ndhal_platform_type_v3():
    narch_get_instance_type_name(buf, 128)               // (:245)
    if buf matches "trn2p.48xlarge"  or "trn2eu.48xlarge"
                 or "trn2u.48xlarge" or "trn2u-ac.24xlarge":
        plat = ULTRASERVER                               // (:246-250)
    else if buf matches "trn2es.48xlarge":
        plat = PDS                                       // (:251-252)
    else:
        plat = STD                                       // (:254)
    if force_userver:  plat = ULTRASERVER                // module-param override (v3:33-35, 258-260)
    return plat

// V4 (Trn3) platform-type — v4/neuron_dhal_v4.c:156
function ndhal_platform_type_v4():
    narch_get_instance_type_name(buf, 128)               // (:161)
    if buf matches "trn3s.48xlarge" or "trn3-dev0.48xlarge":
        return PDS                                       // (:162-165)
    if buf matches "trn3p.48xlarge":
        return ULTRASERVER                               // (:166-167)
    return STD                                           // (:169)
```

### Considerations

The instance-type → platform-type → pod-election cabling. The platform type is what `neuron_pelect.c` (the [Pod Election engine](../kernel/pod-election.md)) keys on for its three flows:

| Platform | EC2 instance strings (DMI `product_name`) | Pod behavior |
|---|---|---|
| **STD** | anything not matched below | not a pod; election thunks short-circuit (V3 `dhal_v3:1635`) |
| **ULTRASERVER** | V3: `trn2p`/`trn2eu`/`trn2u`/`trn2u-ac` · V4: `trn3p` (`.48xlarge`/`.24xlarge`) | full PCIe **B-link ring election** — 16 devices/node, up to 4 nodes in a ring, serial-number leader election (`npe_get_node_id`, `pelect.c:768`) |
| **PDS** | V3: `trn2es.48xlarge` · V4: `trn3s`/`trn3-dev0` (`.48xlarge`) | **location-based** node assignment, *no* link election; node_id/server_id from FW-IO + a hard-coded serial→node table (`npe_pds_config_init`, `pelect.c:1863`) |

A few reimplementation notes the matcher hides:

- **A node is 16 devices.** Both pod modes assume exactly 16 Neuron devices per node, device 0 = PRIMARY, 1..15 = SECONDARIES (`pelect.c:51`, the `pnd[16]` table at `:249`). The pod's `pod_type` (NONE / SWITCH=PDS / P2P=ULTRASERVER) is what the DHAL `npe_pod_info_v3` thunk dispatches on (`dhal_v3:1633`).
- **V4 shares V3's election engine.** `v4/neuron_dhal_v4.c:457` also calls `npe_init` — Trn3 reuses the Trn2 pod-election code wholesale, with only the platform-string table differing.
- **`force_userver` is a debug escape hatch** (V3 module param, `v3:33-35`): set it to force `ULTRASERVER` regardless of the DMI string (`v3:258-260`). There is no V4 equivalent in the V4 matcher.

> **GOTCHA —** the V4 matcher has a likely length-mismatch bug. `v4/neuron_dhal_v4.c:164` bounds a `strncmp` of `NEURON_TRN3PDS0_INSTANCE_NAME` (`"trn3-dev0.48xlarge"`) with `sizeof(NEURON_TRN3PDS_INSTANCE_NAME)` (`"trn3s.48xlarge"`) — two different-length macros. The shorter bound means `"trn3-dev0.…"` is compared only on its first ~15 chars; verify whether `trn3-dev0` is correctly classified as PDS before relying on it. **(MEDIUM** — flagged, not fixed.**)**

> **NOTE —** `narch_get_instance_type_name` caps the read at 64 bytes (`neuron_arch.c:78`) but every caller passes a `buf[128]` (`v3:243`, `v4:159`). Longer product names truncate silently. Separately, its error check tests `!len` (`:79`) — `len` is the read's return value, `file_size` is a separate `ssize_t` out-param (`:69`) — so only a *zero-length* read is an error, and a *negative* return from `kernel_read_file_from_path` is not caught here. Both are edge-case-robustness gaps, not detection bugs.

---

## Cloud / Marketing Naming — the third vocabulary

The marketing names never drive dispatch; they appear in exactly four kinds of site, and a reimplementer should know which is authoritative (the device-ID; everything else is derived or cosmetic):

| Name | Where it appears | Authoritative? |
|---|---|---|
| **Trn1 / Inf2** | device-ID comments (`neuron_pci.c:213-216`); both → `V2` | No — the device-ID is, the names are comments |
| **Trn2** | device-ID comment (`neuron_pci.c:217`); → `V3` | No |
| **Trn3 / "Trainium3"** | device-ID comment (`neuron_pci.c:220`); V4 sysfs `arch_*_suffix` (`v4:145-146`) | No — sysfs string only |
| **trn2*/trn3* `.48xlarge`** | DMI `product_name` matches → platform type (above) | Yes for *platform* axis (not the silicon axis) |
| **UltraServer / PDS** | pod-type names; platform-type enum + pelect (`pelect.c`) | Yes for *platform* axis |

The clean mapping a reimplementer should build their naming table from:

```text
device-ID      arch enum   silicon      cloud product   runtime codename   DHAL dir
─────────────────────────────────────────────────────────────────────────────────
0x7164         V2 (=2)     Trainium1    Trn1            sunda              v2/
0x7264         V2 (=2)     Inferentia2  Inf2            sunda              v2/
0x7364         V3 (=3)     Trainium2    Trn2            cayman             v3/
0x7564         V4 (=4)     Trainium3    Trn3            mariana            v4/  (= v3-base + v4)
0x7565         V4 (=4)     Trainium3    Trn3 (2nd id)   mariana            v4/  (= v3-base + v4)
```

> **QUIRK —** two device-IDs collapse into V2 (Trn1 + Inf2 share one arch) *and* two device-IDs collapse into V4 (`0x7564`/`0x7565` are both Trn3). So "number of device-IDs" (5) ≠ "number of archs" (3) ≠ "number of products" (4: Trn1, Inf2, Trn2, Trn3). A reimplementer building a product table off the three-value arch enum will be missing two products; one building it off the five device-IDs will double-count Trn3. The full device-ID derivation is the subject of [PCI Device-ID → Arch Map](pci-device-ids.md); the runtime-codename side (sunda/cayman/mariana) and the `mariana_plus` forward-declaration are in the [overview](overview.md#the-generation-model--two-vocabularies-one-enum-value).

> **NOTE —** the marketing-name → silicon mapping (V2=Trn1+Inf2, V3=Trn2, V4=Trn3) is asserted by the device-ID switch comments plus the V4 sysfs suffix `"Trainium3"` (`v4:145-146`). The exact silicon-codename correspondence beyond these in-source comments is not independently provable from the kernel arch cell alone — it is corroborated by the runtime's DWARF source paths `tdrv/encd/archs/{sunda,cayman,mariana}.c`, where `mariana.c` is DWARF-attributed to "Trn3 / NeuronCore-V4" and `cayman.c` to "Trn2 / NeuronCore-V3". **(HIGH** for the V→codename pairing via DWARF; **LOW** for any finer silicon-tapeout name.**)**

---

## Cross-References

**Part I siblings (canonical detail)**
- [Overview (Runtime Lens)](overview.md) — the two-vocabulary model, the `mariana_plus` forward-decl, the runtime `al_hal_tpb_arch_type` side of this enum
- [PCI Device-ID → Arch Map](pci-device-ids.md) — the full device-ID switch, vendor `0x1D0F`, and the PCI-revision EMU/QEMU markers in device-ID context
- [Per-Generation Hardware Geometry](hw-geometry.md) — what each latched arch *means* downstream: the per-arch geometry constants the ops vtable installs
- [Coretype Numbering Reconciliation](coretype-numbering.md) — the engine/coretype numbering that rides on the same generation enum

**Kernel consumers**
- [PCI Probe and Device Detection](../kernel/pci-probe.md) — `neuron_pci_set_device_architecture`, `narch_init`, the probe ordering, the `0x7064` `MODULE_ALIAS` discrepancy (GEN-01)
- [DHAL Core (ndhal Vtable-of-Vtables)](../kernel/dhal-core.md) — the per-generation ops install, V4 = V3-base + V4-overrides, the `kmalloc`-vs-`kzalloc` `platform_type` footgun
- [Pod Election (UltraServer)](../kernel/pod-election.md) — the STD/ULTRASERVER/PDS platform axis in full: B-link ring election, PDS location mapping, the 16-device node model
- [back to index](../index.md)
