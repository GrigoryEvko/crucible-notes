# Kernel Driver Overview

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. Every claim is line-anchored to a `.c`/`.h` that was read directly, not reverse-engineered. The driver also builds as a stripped `neuron.ko`, but the GPL C is authoritative — every offset on the Part III pages is a struct field or a `#define`, never a recovered binary offset. Other driver versions renumber lines and add/remove ioctl commands.*
> *Evidence grade: **Confirmed (source-anchored)** — module entry/exit, PCI probe/remove, the char-device fops, and the dispatch front door are all transcribed from the shipped source. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`aws-neuronx-dkms` is an out-of-tree, GPL-2.0 PCIe accelerator driver for Amazon's Neuron chips (Trainium 1/2/3, Inferentia 2). It is an ordinary Linux character driver in shape: it binds one `struct pci_driver` (`"neuron-driver"`, `neuron_pci.c:536`) to every matching PCI function, and for each bound device it publishes one `/dev/neuronN` node (`N == device_index`, 0..63; `neuron_cdev.c:3699`). Userspace — almost exclusively `libnrt.so` through its NDL layer — drives the silicon through that node, and **all** userspace↔device traffic flows through exactly two file-operation entry points: `.unlocked_ioctl` (a ~96-command dispatcher) and `.mmap`. There is no `.read`, `.write`, `.poll`, or `.compat_ioctl` (`ncdev_fops`, `neuron_cdev.c:3540-3547`); the driver is LP64-only and the ioctl ABI carries every argument as a user pointer the handler `copy_from_user`s itself.

The driver's job decomposes into five concerns, and the ~32 Part III pages are organized around them. **(1) Lifecycle:** module load registers the chrdev region and the PCI driver; PCI probe allocates a `struct neuron_device`, maps up to three BARs, brings up DMA/reset/mempool, and publishes the node; probe/remove unwind in mirror order. **(2) The ioctl/mmap surface:** one giant `if/else` dispatcher (`ncdev_ioctl`, `neuron_cdev.c:3188`) wrapped in a three-gate authorization model, plus an `mmap` path that hands BAR/DRAM windows and notification rings to userspace. **(3) DMA:** the Annapurna/Alpine UDMA engines, the descriptor-ring containers, the H2T memcpy queues, and a host-memory completion-marker model that replaces a hardware completion ring. **(4) The DHAL abstraction:** a vtable-of-vtables (`ndhal`) that hides every V2/V3/V4 hardware difference behind function pointers, so the arch-neutral `.c` files contain essentially zero `#ifdef`s for chip generation. **(5) State and telemetry:** reset state machine, pod election (UltraServer), notification queues, datastore, metrics, and the sysfs tree.

What is Neuron-specific against the generic-char-driver frame is concentrated in three places. The driver is **single-arch per load** — `narch_init` latches the first probed device's architecture and `neuron_dhal_init` is a one-time global init (`neuron_arch.c:36-37`, `neuron_dhal.c:14-16`), so mixed-generation hosts are unsupported by construction. The **authorization model has no Linux capability check** anywhere on the ioctl path — access control is the `/dev/neuronN` file mode plus a per-device PID-attach table (detail on [ioctl-dispatch](ioctl-dispatch.md)). And **completion is observed by host-memory marker words**, not a hardware CQ ([dma-rings](dma-rings.md)). This page is the map: it fixes the file layout, the request lifecycle, and the privilege model at a glance, then points each subsystem at its owning sibling page rather than re-deriving the byte layouts those pages own.

For reimplementation, the map-level contract is:

- **The TU layout and ownership** — which `.c` translation unit owns which subsystem, so a reimplementer knows where the reset state machine, the DMA ring container, or the DHAL vtable lives. The grouped module/file map below is the index.
- **The bring-up/tear-down lifecycle** — the exact call order `module_init → pci_probe → ncdev_open → ioctl/mmap → ncdev_flush/release → pci_remove → module_exit`, and which step allocates which resource. The lifecycle diagram pins it.
- **The three-gate authorization model** — the `O_WRONLY` free-access split, the `npid_is_attached` attach sub-gate, and the absence of any capability layer — summarized here, owned in full by [ioctl-dispatch](ioctl-dispatch.md).

| | |
|---|---|
| **Module object / driver node** | `neuron.ko` (`/sys/module/neuron`) — driver `"neuron-driver"` (`neuron_pci.c:536-537`) |
| **Module entry / exit** | `neuron_module_init` (`neuron_module.c:59`) / `neuron_module_exit` (`:90`) |
| **PCI bind / unbind** | `neuron_pci_probe` (`neuron_pci.c:352`) / `neuron_pci_remove` (`:497`) |
| **Char-device fops** | `ncdev_fops` (`neuron_cdev.c:3540-3547`) — `open/flush/release/unlocked_ioctl/mmap` only |
| **IOCTL front door** | `ncdev_ioctl` (`neuron_cdev.c:3188`) — ~96 command macros, base char `'N'` (`neuron_ioctl.h:656`) |
| **Device registry** | `neuron_devices[MAX_NEURON_DEVICE_COUNT]` (`neuron_pci.c:57`), 64 slots, routing-id-indexed |
| **Per-node control block** | `struct ncdev devnodes[64]` (`neuron_cdev.c:63`); `filep->private_data → &devnodes[minor]` |
| **HAL boundary** | `ndhal` vtable-of-vtables (`neuron_dhal.{c,h}`, `v{2,3,4}/`) — one-time per-arch init |
| **Authorization** | `O_WRONLY` free-access split (`:3206`) + `npid_is_attached` attach gate (`:3210-3225`); **no** `capable()` on ioctl |
| **Driver version string** | `"2.27.4.0"`, SHA `1c7ed9bd…` (`neuron_module.c:23,27`) |

---

## 1. The module / file map

The driver is one DKMS-built module compiled from a flat directory of arch-neutral translation units plus three per-generation subdirectories (`v2/`, `v3/`, `v4/`) and a `udma/` fork of the AnnapurnaLabs UDMA engine. There is no internal `#ifdef` arch branching in the arch-neutral files; generation differences are resolved at runtime through the `ndhal` vtable (§4). The map below groups every owned TU by subsystem and names the Part III page that documents it.

```text
neuron-driver (neuron.ko)  — aws-neuronx-dkms 2.27.4.0, GPL-2.0
│
├─ ENTRY / BUS GLUE ─────────────────────────────────────── module-layout.md, pci-probe.md
│   neuron_module.c   module_init/exit, MODULE_* metadata, [FI] debugfs
│   neuron_pci.c      pci_driver, probe/remove, BAR map, device registry, dup-rid helper
│   neuron_pci.h      neuron_devices[], neuron_pci_get_device(), init/exit hooks
│
├─ CHAR DEVICE SURFACE ──────────────────────────────────── cdev-mmap.md, ioctl-dispatch.md
│   neuron_cdev.c     /dev/neuronN: fops, open/flush/release/mmap, node create/delete,
│                     sysfs attrs, the ~96-cmd ioctl dispatcher + ~90 handler bodies
│   neuron_cdev.h     ncdev public API, ncdev_mem_region[]
│   neuron_ioctl.h    the 'N'-based ioctl command macros + arg structs (876 lines)
│   neuron_mmap.c     nmmap_mem: BAR/DRAM/NQ window mapping, CAP_SYS_RAWIO dm-root gate
│
├─ MEMORY ───────────────────────────────────────────────── mempool-handles.md, ioctl-mem.md
│   neuron_mempool.c  mem_chunk allocator, lifespan classes, dump
│   neuron_mc_handle.c  opaque mem-handle ↔ mem_chunk table (MEMCHUNK_MAGIC)
│
├─ DMA ──────────────────────────────────────────────────── dma-op-layer.md, dma-rings.md
│   neuron_dma.c      ndmar op layer, completion-marker model, process-exit reset
│   neuron_ring.c     ndma_eng → ndma_queue → ndma_ring container nest, H2T queues
│   udma/udma.c       AnnapurnaLabs/Alpine UDMA engine fork          udma-main.md
│   udma/udma_m2m.c   memory-to-memory descriptor builder           udma-m2m.md
│   udma/iofic.c      IOFIC interrupt-controller registers           udma-iofic.md
│
├─ DHAL (HW abstraction) ────────────────────────────────── dhal-core.md, dhal-v{2,3,4}.md
│   neuron_dhal.c     ndhal global, one-time per-arch register_funcs dispatch
│   neuron_arch.c     narch_init first-wins arch latch
│   v2/neuron_dhal_v2.c   Trn1 / Inf2 register maps + callbacks
│   v3/neuron_dhal_v3.c   Trn2
│   v4/neuron_dhal_v4.c   Trn3 (inherits v3 BAR base, overrides ~8+1 slots: device-id, platform, NPE/mpset/mmap/cdev/perf)
│
├─ NOTIFICATION / SYNC ──────────────────────────────────── notification-queues.md, topsp.md
│   neuron_nq.c       arch-neutral NQ ring engine (nq_mc[][] store)
│   v{2,3}/notific.c  per-gen NQ/TopSP BAR0 offset geometry
│
├─ STATE PLANE ──────────────────────────────────────────── reset.md, crwl.md, pod-election.md
│   neuron_reset.c    reset thread + RESET→READY state machine
│   neuron_crwl.c     cooperative reader/writer lock + NC-range bitmap reservation
│   neuron_cinit.c    NeuronCore init-state tracking
│   (pod election)    ndhal_npe vtable (UltraServer)                 pod-election.md
│
├─ PLATFORM I/O ─────────────────────────────────────────── fw-io.md, dmabuf-p2p.md
│   neuron_fw_io.c    MiscRAM mailbox, readless-read, device-id/topology reads
│   (dma-buf export)  ndmabuf_get_fd                                 dmabuf-p2p.md
│
└─ TELEMETRY / MISC ─────────────────────────────────────── datastore.md, metrics.md, sysfs.md,
    neuron_ds.c       per-pid datastore                                power.md, misc.md
    neuron_metrics.c  metric aggregation + posting thread
    neuron_sysfs_metrics.c  sysfs metric tree under each node kobj
    neuron_log.c      in-kernel log ring (FILE_OPEN/IOCTL/FLUSH/MMAP records)
    neuron_pid.c      per-device PID attach table (the ioctl attach gate)
    neuron_trace.h    CREATE_TRACE_POINTS owner (neuron_module.c)
```

> **NOTE —** the module *object* name and the *driver* name differ: `Kbuild` builds `neuron.o` so the module is `/sys/module/neuron`, but `struct pci_driver.name = "neuron-driver"` (`neuron_pci.c:537`, struct opens `:536`), so the sysfs driver node is `/sys/bus/pci/drivers/neuron-driver`. A reimplementer wiring udev or sysfs scripts must use the right one for the right path.

### 1.1 Device identity and the registry

The driver claims five PCI device-ids under Amazon's vendor id `0x1D0F` (`neuron_device.h:35-40`): `TRN1 0x7164` / `INF2 0x7264` → arch **V2**, `TRN2 0x7364` → **V3**, `TRN3 0x7564`/`0x7565` → **V4**, all listed in `neuron_pci_dev_ids[]` (`neuron_pci.c:30-39`). Each bound function gets a `struct neuron_device` (`neuron_device.h:70`), and the global registry `neuron_devices[64]` (`neuron_pci.c:57`) is indexed not by PCI enumeration order but by the chip's **routing id**: probe assigns a provisional `atomic_fetch_add` counter (`:421`) that is immediately overwritten by the DHAL `neuron_pci_get_device_id` read (`:433`), which resolves the real slot from a firmware register. That same slot number becomes the char-device minor, so `/dev/neuronN` ⇔ `neuron_devices[N]` ⇔ `devnodes[N]` is one consistent index across the registry, the node array, and the userspace device path.

The three BARs each device maps carry **names that are not PCI indices**. The `struct neuron_pci_device` triple (`neuron_device.h:45-55`) labels them `bar0`/`bar2`/`bar4` (APB register window, AXI, DRAM/HBM), but the actual PCI BAR index comes from `ndhal_pci.{apb_bar,axi_bar,dram_bar}` — and on every shipping arch `axi_bar == BAR_UNUSED (-1)`, so the AXI iomap is effectively never populated and FWIO drives off the APB window alone. The DRAM BAR is WC-mapped when the `wc_enable` module param is set (default 1) and its mapping failure is *tolerated* (the device still binds without an HBM window). The per-arch BAR-index matrix is owned by [pci-probe](pci-probe.md).

> **QUIRK —** the driver is **single-architecture per load**. `narch_init` latches the first probed device's arch and early-returns on every later device (`neuron_arch.c:36-37`); `neuron_dhal_init` is a one-time global guarded by `ndhal != NULL` (`neuron_dhal.c:14-16`). A host with both a Trn2 and a Trn3 card would bind both to *one* `ndhal` built for whichever probed first — mixed-generation hosts are unsupported by construction, not by policy. A reimplementer must not assume per-device HAL state.

---

## 2. The request lifecycle

The driver's entire dynamic behavior is one nested lifecycle: a module-global stage (chrdev region + PCI driver registration), a per-device stage (probe → publish, remove → free), and a per-open stage (open → ioctl/mmap → flush/release) that repeats for every userspace fd. The diagram pins the call order and the resource each step owns; the per-step `file:line` cites let a reimplementer follow each arrow into the owning sibling page.

```text
insmod neuron.ko
  │
  ▼  module_init → neuron_module_init        (neuron_module.c:59)
     ├─ nmetric_init_constants_metrics                          :73
     ├─ ncdev_module_init   alloc_chrdev_region("neuron",64) +
     │                      class_create("neuron_device")       :79  → module-layout.md
     └─ neuron_pci_module_init → pci_register_driver            :83
            │   (kernel now calls neuron_pci_probe per matching PCI function)
            ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │ PER DEVICE — neuron_pci_probe(dev,id)              (neuron_pci.c:352)     │
   │   nd = kvzalloc(struct neuron_device)                            :357     │
   │   pci_enable_device + pci_set_master                            :372/378  │
   │   neuron_pci_set_device_architecture → narch_init  (latch arch) :381      │
   │   neuron_dhal_init                  (one-time per-arch ndhal)    :383      │  → dhal-core.md
   │   reserve+map APB / AXI / DRAM BARs (indices from ndhal_pci)    :390-417  │  → pci-probe.md
   │   fw_io_setup                       (readless-read context)      :425     │  → fw-io.md
   │   ndhal_pci.neuron_pci_get_device_id (routing-id → device_index) :433     │
   │   neuron_pci_device_init:                                        :437     │
   │      dma_set_mask · nr_create_thread · nci_reset_state ·                  │  → reset.md
   │      ndmar_preinit · mpset_constructor · crwl mutex_init ·                │  → dma-rings.md
   │      ncdev_create_device_node  → /dev/neuronN published          :159     │  → cdev-mmap.md
   │      nr_start_ncs(NEURON_NC_MAP_DEVICE, RESET_REQUEST_ALL)       :165     │
   │   neuron_ds_init · memset_mc/ndma_q_dummy_mc alloc · nmetric_init :442-462│
   │   BUG_ON(neuron_devices[idx]); neuron_devices[idx] = nd ◀ PUBLISH :466-467│
   └─────────────────────────────────────────────────────────────────────────┘
            │
            ▼   (userspace: NDL opens /dev/neuronN — tdrv-lifecycle.md)
   ┌─────────────────────────────────────────────────────────────────────────┐
   │ PER OPEN — ncdev_open(inode,filep)                (neuron_cdev.c:3390)    │
   │   dev = &devnodes[iminor(inode)]; nd = dev->ndev                          │
   │   O_WRONLY free-access fd → private_data=dev; return  (NO attach) :3400   │
   │   normal fd → open_count++; wait while device_state==RESET → READY :3405  │
   │             → npid_attach(nd)  (else -EBUSY)                     :3429    │  → ioctl-dispatch.md
   │      │                                                                    │
   │      ▼  ncdev_ioctl(filep,cmd,param)  ── ~96 cmds, 3-gate model  :3188    │  → ioctl-dispatch.md
   │      ▼  ncdev_mmap(filep,vma) → nmmap_mem (BAR/DRAM/NQ windows)  :3522    │  → cdev-mmap.md
   │      │                                                                    │
   │   ncdev_flush(filep,id)  per close(2); on last attach:          :3452    │
   │      nr_wait · ndmar_handle_process_exit · ncrwl_release ·                │
   │      neuron_ds_release_pid · mpset_free(CUR_PROCESS) · npid_detach        │
   │   ncdev_release(inode,filep)  on open_count==0:                 :3499    │
   │      neuron_ds_clear · mpset_free(ALL_PROCESS) · nmmap_delete_all_nodes   │
   └─────────────────────────────────────────────────────────────────────────┘
            │
            ▼   neuron_pci_remove(dev)                  (neuron_pci.c:497)
            │      nr_stop_thread · nmetric_stop_thread · ndhal_ext_cleanup
            │      release BARs · pci_disable_device · neuron_pci_device_close
            │        └─ ncdev_delete_device_node (node death)           :186
            │      pci_iounmap · neuron_log_destroy · kvfree(nd)
            ▼
rmmod neuron.ko
       module_exit → neuron_module_exit            (neuron_module.c:90)
         ├─ neuron_pci_module_exit → ndhal_cleanup → pci_unregister_driver :95
         └─ ncdev_module_exit  (class_destroy + unregister_chrdev_region)  :96
```

The two nestings to keep distinct are **node lifetime** and **fd lifetime**. The `/dev/neuronN` node is created once per device in probe (`ncdev_create_device_node`, `neuron_pci.c:159`) and destroyed once in remove (`:186`); within that window any number of userspace fds open and close. `open_count` (incremented in `ncdev_open` for non-free-access fds only) tracks the fd nesting and gates the last-close teardown in `ncdev_release`; the per-process PID attach (`npid_attach`/`npid_detach`) tracks which *process* owns the device and gates the privileged ioctl subset. `ncdev_flush` runs on every `close(2)` and does per-process cleanup only on the last attached reference; `ncdev_release` runs on the final `fput` and does the last-fd cleanup. The full open/flush/release accounting — including the free-access divergence and the `open_count++`-before-RESET-wait race window — is owned by [cdev-mmap](cdev-mmap.md).

> **GOTCHA —** two probe-stage error paths in `neuron_pci_device_init` return without unwinding earlier allocations (`nr_create_thread` and `nr_start_ncs` failure, `neuron_pci.c:132-133, :166-167`), and `neuron_module_init` does not call `ncdev_module_exit` if `neuron_pci_module_init` fails after the chrdev region was already reserved. These are leak-on-error-path defects in the shipped source, not part of the happy-path lifecycle; a reimplementation should add the missing `goto`-unwind rungs. Recorded on [pci-probe](pci-probe.md).

### 2.1 Two accounting axes: fd count and process attach

The per-open stage tracks two *independent* counts, and a reimplementer who collapses them gets the teardown wrong. The first is `devnode->open_count` (`struct ncdev`, `neuron_cdev.c:53-60`), incremented in `ncdev_open` for normal fds only and decremented in `ncdev_release`; it counts *open file descriptions* of the node and gates the **last-fd** teardown (`neuron_ds_clear`, `mpset_free(ALL_PROCESS)`, `nmmap_delete_all_nodes`) when it reaches zero. The second is the per-device PID attach table (`neuron_pid.c`, the `npid_*` family): `npid_attach` in `ncdev_open` registers the calling `tgid` into one of the device's process slots, and `ncdev_flush` runs the **per-process** teardown (`ndmar_handle_process_exit`, `ncrwl_release_current_process`, `neuron_ds_release_pid`, `mpset_free(CUR_PROCESS)`) only when the *last attached reference* for that process closes (`attach_cnt == 1`). All of this is serialized by the per-node `devnode->ncdev_lock`.

The two axes diverge because of the free-access lane. An `O_WRONLY` free-access fd takes **neither** axis: `ncdev_open` early-returns without `open_count++` and without `npid_attach` (`:3400`), and its `ncdev_flush` is the trivial `ncdev_misc_flush` (unmark-all). So a process can hold a free-access fd for topology/info queries while a *different* fd (or process) owns the device through DEVICE_INIT. The full state machine — including the acknowledged TODO that `ncdev_open` busy-waits on `device_state == RESET` with a bare `schedule()` after `open_count++`, so a signal mid-wait correctly decrements — is owned by [cdev-mmap](cdev-mmap.md).

---

## 3. The privilege model, at a glance

There is **no Linux capability check on any ioctl path** — confirmed by grep over the driver: the sole `capable(CAP_SYS_RAWIO)` in the tree is in an `mmap` path (`neuron_mmap.c:362`, the device-DRAM-root window). Userspace authorization is therefore entirely a two-gate model layered on top of the `/dev/neuronN` file permission (Gate 0, default `root:neuron 0660`, out of the driver's hands):

| Gate | Mechanism | Where | What it controls |
|---|---|---|---|
| 0 | `/dev/neuronN` file mode | (out of driver) | who can `open(2)` the node at all |
| 1 | `O_WRONLY` free-access split | `IS_NEURON_DEVICE_FREE_ACCESS` (`neuron_cdev.c:52`), applied at `:3206` | an `O_WRONLY` fd is routed *unconditionally* to the restricted, **ungated** `ncdev_misc_ioctl` lane (topology/info + a few state-changing misc cmds), with no PID attach |
| 2 | `npid_is_attached` attach sub-gate | `neuron_cdev.c:3210-3225` | on a normal fd, ~19 mutating commands (`MEM_ALLOC`, `MEM_COPY`, `DMA_*`, `BAR_WRITE`, `NOTIFICATIONS_INIT_*`, …) require the caller be the `DEVICE_INIT` owner, else `-EACCES`; everything else runs un-PID-gated |

The model has one headline correctness slip a reimplementer must know about: the attach sub-gate tests **exact full `cmd` values**, but the dispatcher reaches the size-overloaded `*64`/`V2` siblings by `_IOC_NR`, so the `*64` variants (`MEM_COPY64`, `MEM_COPY_ASYNC64`, `DMA_COPY_DESCRIPTORS64`) **slip the attach gate** while their 32-bit base commands are gated. They still require a non-free-access fd, and the handlers re-validate mem-handles against the calling `nd`'s pool, so the exposure is bounded — but the gate is inconsistent as written. This and the `O_WRONLY`-only macro fragility (`(f_flags & O_WRONLY) == 1` matches `O_WRONLY` exactly, not `O_RDWR`) are fully derived on [ioctl-dispatch](ioctl-dispatch.md). The full ~96-command map with per-command gate, direction, and arg-struct columns is the [ioctl-catalog](ioctl-catalog.md).

---

## 4. The DHAL boundary

Every hardware-generation difference — BAR indices, register offsets, NQ/TopSP geometry, device-id reads, reset sequencing, pod election — is hidden behind one global vtable-of-vtables, `ndhal` (`neuron_dhal.{c,h}`). The arch-neutral `.c` files never branch on chip generation; they call through `ndhal->ndhal_<subsystem>.<method>(...)`. `neuron_dhal_init` runs **once** for the whole module (guarded by `ndhal != NULL`, `neuron_dhal.c:14-16`), populating the vtable from the latched architecture's `register_funcs` (V2 = Trn1/Inf2, V3 = Trn2, V4 = Trn3, which inherits V3's BAR base and overrides ~8+1 slots (device-id read, platform type, NPE/mpset/mmap/cdev/perf hooks), `v4/neuron_dhal_v4.c:438-445`). This is why mixed-architecture hosts are unsupported: the first probed device's `narch_init` wins (`neuron_arch.c:36-37`) and the single global `ndhal` is shared by every later device.

The sub-vtables a reimplementer meets across the Part III pages: `ndhal_pci` (BAR indices + routing-id reads, [pci-probe](pci-probe.md)), `ndhal_address_map` (`nc_per_device` and BAR/NQ offset arithmetic), `ndhal_nq` / `ndhal_topsp` (NQ register writers, [notification-queues](notification-queues.md), [topsp](topsp.md)), `ndhal_ndmar` (DMA ring geometry, [dma-rings](dma-rings.md)), `ndhal_npe` (pod election + sysfs class shows, [pod-election](pod-election.md)), and the reset/cleanup hooks (`ndhal_ext_cleanup`, [reset](reset.md)). The vtable structure, the one-time-init guard, and the per-arch `register_funcs` dispatch are owned by [dhal-core](dhal-core.md); the three concrete implementations are [dhal-v2](dhal-v2.md) / [dhal-v3](dhal-v3.md) / [dhal-v4](dhal-v4.md).

---

## 5. Part III page index

The ~32 Part III pages, grouped by the subsystem each owns. Start at the lifecycle/surface pages, descend into DMA and DHAL for the data path, and use the state/telemetry group for the auxiliary planes.

| Subsystem | Pages |
|---|---|
| **Entry & lifecycle** | [Module Layout and Build](module-layout.md) · [PCI Probe and Device Detection](pci-probe.md) |
| **Char-device surface** | [Char Device, fops and mmap](cdev-mmap.md) · [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) · [IOCTL Catalog](ioctl-catalog.md) |
| **IOCTL handlers** | [Memory IOCTL Handlers](ioctl-mem.md) · [DMA IOCTL Handlers](ioctl-dma.md) · [NQ and Semaphore IOCTL Handlers](ioctl-nq.md) · [Pod and Misc IOCTL Handlers](ioctl-pod.md) |
| **Memory** | [Memory Pool and MC Handle Table](mempool-handles.md) |
| **DMA / UDMA** | [DMA Op Layer and the Completion-Marker Model](dma-op-layer.md) · [DMA Rings and H2T Queues](dma-rings.md) · [UDMA Core (Annapurna/Alpine Fork)](udma-main.md) · [UDMA Memory-to-Memory Builder](udma-m2m.md) · [UDMA IOFIC Interrupt Controller](udma-iofic.md) |
| **DHAL** | [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) · [DHAL V2 (Trn1 / Inf2)](dhal-v2.md) · [DHAL V3 (Trn2)](dhal-v3.md) · [DHAL V4 (Trn3)](dhal-v4.md) |
| **NQ / TopSP** | [Notification Queue Engine](notification-queues.md) · [TopSP Notification Path](topsp.md) |
| **Reset / pod / lock** | [Reset State Machine](reset.md) · [Cooperative RW Lock (CRWL)](crwl.md) · [Pod Election (UltraServer)](pod-election.md) |
| **Platform I/O** | [FW-IO MiscRAM Mailbox Protocol](fw-io.md) · [DMA-buf Export and P2P](dmabuf-p2p.md) |
| **Datastore / metrics / sysfs** | [Kernel Datastore](datastore.md) · [Metrics Aggregation](metrics.md) · [Sysfs Metrics Tree](sysfs.md) · [Power Telemetry](power.md) · [Misc: Log Ring, PID, Trace, Reg, Core](misc.md) |

---

## Cross-References

- [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) — the three-gate authorization model, the overload-resolution discipline, and the `*64` sub-gate slip summarized in §3
- [PCI Probe and Device Detection](pci-probe.md) — the probe/remove call ladder, BAR mapping, routing-id resolution, and the probe-stage leak-on-error paths flagged in §2
- [Char Device, fops and mmap](cdev-mmap.md) — `ncdev_open`/`flush`/`release` accounting, the free-access divergence, and the `nmmap_mem` window protocol
- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the one-time per-arch vtable init and `register_funcs` dispatch that §4 maps
- [TDRV: Device Bring-Up and Lifecycle](../runtime/tdrv-lifecycle.md) — the runtime NDL consumer: how `libnrt` opens every `/dev/neuronN` and issues the ioctls this driver serves
- [back to index](../index.md)
