# Module Layout and Build

> **Kernel:** `aws-neuronx-dkms 2.27.4.0` (GPL-2.0 source; build SHA `1c7ed9bd14936635773b5a01777882804ee8ea6e`, `neuron_module.c:23,27`). Every `file:line` cites into `extracted/aws-neuronx-dkms_2.27.4.0_all/usr/src/aws-neuronx-2.27.4.0/`, read directly — no reverse engineering. The driver also builds a stripped `neuron.ko`, but the GPL C is authoritative; every offset here is a struct field or a `#define`, never a recovered binary offset. Other driver versions renumber these lines.
> **Status:** Reimplementation-grade · **Evidence grade:** `file:line`-anchored (source confirmed) · **Part III — Kernel Driver**, DEEP · [back to overview](overview.md)

## Abstract

`neuron_module.c` (100 lines) is the kernel-module skeleton: the `module_init`/`module_exit` pair, the `MODULE_*` metadata, the optional `CONFIG_FAULT_INJECTION` debugfs tree, and the wall-clock banner the driver prints on load. It is deliberately thin — it owns no device state. Its entire job is to bring up exactly two module-global services in a fixed order, `ncdev_module_init` (the chrdev region + sysfs class) then `neuron_pci_module_init` (the `pci_driver` registration), and to tear them down in reverse. Once `pci_register_driver` returns, the kernel drives everything else through `neuron_pci_probe` per matching PCI function (owned by [pci-probe](pci-probe.md)); this page owns the *skeleton around* that registration, not the probe body.

Wrapped around that skeleton is a cluster of small, cross-cutting support translation units the rest of the driver leans on but that no single subsystem owns. The most algorithmic is `neuron_log.c` — a **lock-free 1024-entry per-device event ring** that records the four char-device lifecycle events (`FOPEN`/`FFLUSH`/`IOCTL`/`MMAP`) and is dumpable to `dmesg` on demand. Its enqueue is one `atomic_fetch_add` on a monotonic `tail` followed by a publish-last store of the record `type`, with no spinlock anywhere on the producer path; the wrap and the publish ordering are the subtle parts a reimplementer must get exactly right, so they get the depth in §3. Alongside it sit `neuron_pid.c` (the per-device PID-attach table that is the ioctl privilege gate), `neuron_reg_access.c` (three MMIO wrappers routing reads through the DHAL readless path), `neuron_cinit.c` (the per-NeuronCore init-state handshake), and `neuron_core.c` (semaphore/event MMIO primitives) — each a tight paragraph and a Function Map row in §4.

The page proceeds in load order: the entry-point init/exit tree (§1), the `module_init`/`module_exit` bodies as annotated pseudocode with the exact registration order and reverse teardown (§2), the log ring's atomic enqueue and dump (§3), the support-TU roster (§4), and the `MODULE_*` metadata plus DKMS/Kbuild packaging — including the stale `0x7064` `MODULE_ALIAS` the audit flagged as PCI-01 (§5). The PCI probe sequence, the BAR map, and the device registry publish are the property of [pci-probe](pci-probe.md); the char-device fops and the `ncdev_module_init` body are the property of [cdev-mmap](cdev-mmap.md). This page cites both at their boundaries rather than re-deriving them.

For reimplementation, the contract is:

- **The module skeleton and its two-service init order** — `ncdev_module_init` (chrdev region) **before** `neuron_pci_module_init` (`pci_register_driver`), and the reverse-order `module_exit`; plus the missing-unwind error path between them.
- **The lock-free log ring** — the 1024-entry power-of-two buffer, the `atomic_fetch_add(&tail)` enqueue, the publish-last `type` store, the modulo-wrap index, and the backward-scan/forward-print dump with jiffies→wall-clock reconstruction.
- **The support-TU roster** — what `neuron_pid` / `neuron_reg_access` / `neuron_cinit` / `neuron_core` each provide, their entry constants, and which subsystem consumes them.
- **The `MODULE_*` metadata and DKMS packaging** — the license/version/description, the `0x7064` alias quirk, and the `Kbuild` `obj-m += neuron.o` object list that fixes the module name to `neuron` while the driver node is `neuron-driver`.

| | |
|---|---|
| **Module entry / exit** | `neuron_module_init` (`neuron_module.c:59`, `__init`) / `neuron_module_exit` (`:90`, `__exit`); `module_init`/`module_exit` macros at `:99-100` |
| **Init order** | `nmetric_init_constants_metrics` (`:73`) → `[FI]` debugfs (`:76`) → `ncdev_module_init` (`:79`) → `neuron_pci_module_init` (`:83`) |
| **Exit order (reverse)** | `[FI]` free debugfs (`:93`) → `neuron_pci_module_exit` (`:95`) → `ncdev_module_exit` (`:96`) |
| **PCI register / unregister** | `neuron_pci_module_init` (`neuron_pci.c:543`) → `pci_register_driver` (`:547`) / `neuron_pci_module_exit` (`:556`) → `pci_unregister_driver` (`:559`) |
| **Log ring** | `struct neuron_log_obj {atomic_t tail; neuron_log_rec *log}` (`neuron_log.h:36-39`); `NEURON_LOG_NUM_ENTRIES = 1024` (`neuron_log.c:18`); enqueue `neuron_log_rec_add` (`:59`); dump `neuron_log_dump` (`:83`) |
| **Module metadata** | `MODULE_LICENSE("GPL")` (`:22`), `MODULE_VERSION("2.27.4.0")` (`:23`), `MODULE_DESCRIPTION(…SHA…)` (`:21`) |
| **Stale autoload alias** | `MODULE_ALIAS("pci:…d00007064…")` (`neuron_module.c:24`) — `0x7064` is **not** an id_table ID (PCI-01) |
| **Banner** | `printk(KERN_ERR "Neuron Driver Started with Version:…")` (`:70-71`) — `KERN_ERR` so it reaches the serial console |
| **Build** | `Kbuild` `obj-m += neuron.o` (`Kbuild:1`); `dkms.conf` `BUILT_MODULE_NAME[0]="neuron"`, `PACKAGE_VERSION=2.27.4.0` (`dkms.conf:1-3`); `ccflags-y += -O3 -Wall -Werror` (`Kbuild:20`) |
| **Confidence** | HIGH — `neuron_module.c`, `neuron_log.{c,h}`, the four support TUs, `Kbuild`, and `dkms.conf` all read in full; ordering, ring algorithm, metadata, and packaging are verbatim |

---

## 1. Entry-Point Tree — Init and Exit

The module skeleton is a strict two-phase bring-up with a reverse-order teardown. Nothing here is device-specific: both `ncdev_module_init` and `neuron_pci_module_init` are module-global, called once at `insmod` and undone once at `rmmod`. The per-device work all happens *inside* `pci_register_driver`'s synchronous probe callbacks (and later, on hot-plug). The tree below pins the call order; the `[FI]` rungs exist only when the kernel is built with `CONFIG_FAULT_INJECTION`.

```text
insmod neuron.ko
  │
  ▼  module_init → neuron_module_init        (neuron_module.c:59, __init)
     ├─ ktime_get_real_ts64 + time64_to_tm                       :65-67
     ├─ printk(KERN_ERR "Neuron Driver Started with Version:…")  :70-71   (serial-console banner)
     ├─ nmetric_init_constants_metrics()                         :73      → metrics.md
     ├─ [FI] neuron_module_init_debugfs()                        :76      (5 fault-attr knobs)
     ├─ ncdev_module_init()                                      :79      → cdev-mmap.md
     │     └─ alloc_chrdev_region("neuron", 0, 64) + class_create("neuron_device")
     └─ neuron_pci_module_init()                                 :83      → pci-probe.md
           └─ pci_register_driver(&neuron_pci_driver)   (neuron_pci.c:547)
                │  (kernel now calls neuron_pci_probe per matching PCI function — synchronous)
                └─ total_neuron_devices = atomic_read(&device_count)  (neuron_pci.c:552)

rmmod neuron.ko
  │
  ▼  module_exit → neuron_module_exit        (neuron_module.c:90, __exit)
     ├─ [FI] neuron_module_free_debugfs()                        :93      (debugfs_remove_recursive)
     ├─ neuron_pci_module_exit()                                 :95      → pci-probe.md
     │     ├─ neuron_dhal_cleanup()                  (neuron_pci.c:558)
     │     ├─ pci_unregister_driver(&neuron_pci_driver)          :559     (kernel calls neuron_pci_remove per device)
     │     └─ neuron_dhal_free()                                 :560
     └─ ncdev_module_exit()                                      :96      (class_destroy + unregister_chrdev_region)
```

> **NOTE —** the banner is logged at `KERN_ERR`, not `KERN_INFO` (`neuron_module.c:69-71`), and the source comment says why: the elevated level forces the driver version + build SHA onto the serial console even on a host configured to suppress info-level boot spew. A reimplementer keeping a quiet log should preserve this one deliberate level inversion — it is the line that tells field triage which driver build is loaded.

---

## 2. `module_init` / `module_exit` — Registration Order and Reverse Teardown

### Purpose

`neuron_module_init` registers two module-global services and returns the first error verbatim; `neuron_module_exit` unwinds them in the reverse order. The single decision that matters for reimplementation is the **order**: the chrdev region and sysfs class (`ncdev_module_init`) must exist before `pci_register_driver` runs, because `pci_register_driver` can call `neuron_pci_probe` synchronously, and probe's `ncdev_create_device_node` needs the class and region already reserved. The teardown reverses it: unregister the PCI driver (which removes every device) before destroying the chrdev region the devices' nodes lived in.

### Algorithm

Modeling `neuron_module_init` (`:59-88`) and `neuron_module_exit` (`:90-97`), with `neuron_pci_module_init`/`exit` (`neuron_pci.c:543-561`) inlined at their call edges:

```c
// neuron_module_init @ neuron_module.c:59  (__init — discarded after load)
function neuron_module_init():
    ktime_get_real_ts64(&tv); time64_to_tm(tv.tv_sec, 0, &tm)     // wall clock (:65-66)
    tm.tm_mon = (tm.tm_mon > 11) ? 0 : tm.tm_mon                  // month2name[] bound guard (:67)
    printk(KERN_ERR "Neuron Driver Started with Version:%s-%s …",
           driver_version, driver_revision, …)                    // banner to serial (:70-71)

    nmetric_init_constants_metrics()                              // boot-time metric constants (:73)

    #ifdef CONFIG_FAULT_INJECTION
        neuron_module_init_debugfs()                              // debugfs "neuron" + 5 attrs (:76)
    #endif

    ret = ncdev_module_init()                                     // alloc_chrdev_region + class (:79)
    if ret: return ret                                           // (:80-81)  — NOTE: no debugfs teardown

    ret = neuron_pci_module_init()                                // pci_register_driver (:83)
    if ret: return ret                                           // (:84-85)  — GOTCHA: no ncdev_module_exit
    return 0                                                      // (:87)

// neuron_pci_module_init @ neuron_pci.c:543
function neuron_pci_module_init():
    ret = pci_register_driver(&neuron_pci_driver)                 // (:547) kernel probes matching devices here
    if ret != 0: pr_err("Failed to register neuron inf driver %d", ret); return ret   // (:548-551)
    total_neuron_devices = atomic_read(&device_count)            // sampled ONCE (:552) — see pci-probe.md
    return 0

// neuron_module_exit @ neuron_module.c:90  (__exit)  — strict reverse order
function neuron_module_exit():
    #ifdef CONFIG_FAULT_INJECTION
        neuron_module_free_debugfs()                             // debugfs_remove_recursive (:93)
    #endif
    neuron_pci_module_exit()                                     // (:95)
    ncdev_module_exit()                                          // (:96) — chrdev region + class last

// neuron_pci_module_exit @ neuron_pci.c:556
function neuron_pci_module_exit():
    neuron_dhal_cleanup()                                        // per-arch ndhal teardown (:558)
    pci_unregister_driver(&neuron_pci_driver)                    // kernel calls neuron_pci_remove per device (:559)
    neuron_dhal_free()                                           // free the ndhal vtable (:560)
```

### Considerations

> **GOTCHA —** `neuron_module_init` has **no unwind between its two registrations**. If `neuron_pci_module_init` fails (`:84`), the function returns the error *without* calling `ncdev_module_exit`, so the chrdev region and sysfs class reserved at `:79` leak for the lifetime of the failed `insmod` attempt (and the `[FI]` debugfs dir, if created at `:76`, leaks too). The kernel will not retry `module_init`, so the region is unreserved only on a subsequent successful load + `rmmod`. A reimplementation must route the `:84` failure through a `goto`-unwind that calls `ncdev_module_exit` (and `neuron_module_free_debugfs`). **(MED** — the missing call is a direct read; "leak" is the inference.**)** This is the module-load twin of the two probe-stage leaks owned by [pci-probe](pci-probe.md).

> **QUIRK —** the DHAL is **not** brought up in `module_init`. `neuron_dhal_init` runs inside `neuron_pci_probe` (`neuron_pci.c:383`), once, after the first device latches its architecture — so the `ndhal` vtable does not exist until at least one device probes. `neuron_pci_module_exit` nonetheless calls `neuron_dhal_cleanup`/`neuron_dhal_free` (`:558,:560`) unconditionally; both are written to tolerate a never-initialized `ndhal` (the module can be loaded with no matching device present, then unloaded). The per-arch `ndhal` bring-up overrides a substantial set of slots — V4's `ndhal_register_funcs_v4` alone installs seven base callbacks plus platform-conditional ones (`v4/neuron_dhal_v4.c:439-445`, then ultraserver/PDS hooks at `:456-464`) before chaining into the V3 base — it is **not** a single device-id override. The vtable structure is owned by [dhal-core](dhal-core.md).

---

## 3. The Lock-Free Log Ring (`neuron_log.c`)

### Purpose

`neuron_log.c` is a per-device circular event log: a fixed 1024-entry ring (`NEURON_LOG_NUM_ENTRIES`, `neuron_log.c:18`) of `struct neuron_log_rec`, one `struct neuron_log_obj` embedded in each `struct neuron_device` (`neuron_device.h:118`). It records the four char-device lifecycle events — `FILE_OPEN`, `FILE_FLUSH`, `FILE_IOCTL`, `FILE_MMAP` (`neuron_log.h:21-27`) — fired from `ncdev_open`/`flush`/`ioctl`/`mmap`, and is dumped to `dmesg` on the `NC_PID_STATE_DUMP` ioctl. It is allocated in probe (`neuron_pci.c:365`, warn-only on failure) and freed in remove (`:490` on the error path, `:531` on the happy path). The whole point of the design is a **producer path with no lock** — `ncdev_ioctl` is on the hot path and must not contend a spinlock just to journal one event — so the ring is built around a single monotonic atomic counter.

### Entry Point

```text
neuron_pci_probe (neuron_pci.c:352)
  └─ neuron_log_init(nd)            (neuron_log.c:40)   ── atomic_set(tail,0); kzalloc 1024 recs

ncdev_open / flush / ioctl / mmap  (neuron_cdev.c)
  └─ neuron_log_rec_add(nd, type, data)  (neuron_log.c:59)   ── the lock-free enqueue (PRODUCER)

ioctl NC_PID_STATE_DUMP
  └─ neuron_log_dump(nd, pid, limit) (neuron_log.c:83)        ── snapshot + backward-scan + forward-print

neuron_pci_remove (neuron_pci.c:497)
  └─ neuron_log_destroy(nd)         (neuron_log.c:52)   ── kfree(log) if non-NULL
```

### Data Layout

| Field | Type | Source | Meaning |
|---|---|---|---|
| `neuron_log_obj.tail` | `atomic_t` | `neuron_log.h:37` | monotonic enqueue counter; the live index is `tail % 1024`, never reset after init |
| `neuron_log_obj.log` | `neuron_log_rec *` | `neuron_log.h:38` | `kzalloc`'d 1024-entry buffer; `NULL` ⇒ ring disabled (alloc failed), every op early-returns |
| `neuron_log_rec.type` | `enum neuron_log_type` | `neuron_log.h:30` | `INVALID(0)`/`FOPEN(1)`/`FFLUSH(2)`/`IOCTL(3)`/`MMAP(4)`; **published last**, gates record validity |
| `neuron_log_rec.pid` | `pid_t` | `neuron_log.h:31` | `task_tgid_nr(current)` of the producer |
| `neuron_log_rec.data` | `uint64_t` | `neuron_log.h:32` | type-specific: `file*` for open/flush/mmap, the ioctl cmd for IOCTL |
| `neuron_log_rec.jiffies` | `uint64_t` | `neuron_log.h:33` | `get_jiffies_64()` stamp, used to reconstruct wall-clock at dump time |

`NEURON_LOG_NUM_ENTRIES = 1024` is a power of two, so `i % 1024` is a single AND-mask — the modulo never costs a divide. The ring keeps **no head**: it never tracks how many entries are live, because the consumer (`neuron_log_dump`) always snapshots the whole buffer and scans, so over-writes of un-read entries are by design (this is a debug journal, not a queue with backpressure).

### Algorithm — the atomic enqueue

The producer is `neuron_log_rec_add` (`neuron_log.c:59-79`). It is the algorithmic heart of the TU: one fetch-add reserves a slot, then the record is filled and **published by storing `type` last**.

```c
// neuron_log_rec_add @ neuron_log.c:59  — single-shot, lock-free producer
function neuron_log_rec_add(nd, type, data):
    if nd->log_obj.log == NULL:                              // ring disabled (alloc failed in init)
        return                                               // (:64-66)

    // Reserve a monotonically increasing slot index with one atomic RMW.
    // 4.14+ has atomic_fetch_add; older kernels emulate via add_return-1.
    #if LINUX_VERSION_CODE >= KERNEL_VERSION(4,14,0)
        i = atomic_fetch_add(1, &nd->log_obj.tail)           // returns PRE-increment value (:68)
    #else
        i = atomic_add_return(1, &nd->log_obj.tail) - 1      // returns same PRE value (:70)
    #endif

    log_rec = &nd->log_obj.log[i % NEURON_LOG_NUM_ENTRIES]   // wrap into the 1024-slot ring (:72)

    // PUBLISH-LAST protocol: stamp INVALID first so a concurrent dump that
    // races this slot sees a clearly-skippable record, fill the body, then
    // store the real type last to "commit" the record.
    log_rec->type    = NEURON_LOG_TYPE_INVALID               // (:74)  pre-mark
    log_rec->pid     = task_tgid_nr(current)                 // (:75)
    log_rec->data    = data                                  // (:76)
    log_rec->jiffies = get_jiffies_64()                      // (:77)
    log_rec->type    = type                                  // (:78)  COMMIT — last store
```

The correctness argument a reimplementer needs: the only synchronization is the `atomic_fetch_add`, which guarantees every producer gets a **distinct** `i` and therefore a distinct slot until 1024 producers later the index wraps onto the same slot. Two producers separated by exactly 1024 increments collide on one slot; on a debug journal that is acceptable — the older record is simply over-written. The `type`-last store is the publish: the consumer treats `type == INVALID` as "not yet written / skip", so a record is never read with a meaningful `type` while half-filled. The store ordering is, however, only a compiler-ordering on most builds, not a memory barrier — see the GOTCHA.

> **GOTCHA —** the publish-last store has **no `smp_wmb()` / `WRITE_ONCE`** between the body fills (`:75-77`) and the `type` commit (`:78`). On a weakly-ordered architecture (the driver runs on arm64 Graviton hosts) a concurrent `neuron_log_dump` reader can observe the committed `type` while still seeing a *stale* `pid`/`data`/`jiffies` from the slot's previous occupant, because nothing forces the body stores to be globally visible before the `type` store. The window is one ring-wrap wide and the log is best-effort debug output, so it is benign in practice — but a reimplementer who reuses this ring for anything that must be exact must insert a `smp_wmb()` before the `type` commit and a paired `smp_rmb()` (or `smp_load_acquire` on `type`) in the reader. **(MED** — the missing barrier is a direct read; the torn-read consequence is inference from the arm64 memory model.**)**

### Algorithm — the dump

`neuron_log_dump` (`neuron_log.c:83-155`) is the consumer, invoked from the `NC_PID_STATE_DUMP` ioctl. It snapshots the whole ring under no lock, walks backward from the newest entry to find the start of the window (bounded by `log_dump_limit`, default 128, `:81/:103-105`), then prints forward so `dmesg` reads oldest-to-newest, reconstructing each record's wall-clock time from its jiffies delta.

```c
// neuron_log_dump @ neuron_log.c:83
function neuron_log_dump(nd, pid, log_dump_limit):
    if nd->log_obj.log == NULL: return -ENOMEM                // (:93-95)
    snapshot = kzalloc(1024 * sizeof(neuron_log_rec))         // private copy (:97)
    if snapshot == NULL: return -ENOMEM                       // (:98-101)
    if log_dump_limit == 0: log_dump_limit = NEURON_LOG_DUMP_LIMIT  // default 128 (:103-105)

    // Snapshot: tail-1 is the newest committed slot; copy the whole ring in one memcpy.
    tail_index = (atomic_read(&nd->log_obj.tail) - 1) % 1024  // (:108)
    memcpy(snapshot, nd->log_obj.log, 1024 * sizeof(rec))     // racy-but-tolerated bulk copy (:109)

    // Pass 1 — scan BACKWARD from newest, counting non-skipped records up to the limit,
    //          to locate how far back the print window starts.
    for (i = 1, j = 0; i < 1024 - 1; i++):                    // (:113)
        rec = &snapshot[(tail_index - i) % 1024]              // (:114)
        if rec.type == INVALID || (pid && rec.pid != pid):    // skip empty / foreign-pid (:116)
            continue
        if j++ > log_dump_limit: break                        // window found (:119-121)

    jiffies_now = get_jiffies_64(); ktime_get_real_ts64(&tv_now)   // (:126-127)

    // Pass 2 — print FORWARD from the located window start to the newest entry.
    i = (tail_index - i) % 1024                               // window start (:131)
    while i != tail_index:                                    // (:132)
        rec = &snapshot[i]; i = (i + 1) % 1024                // (:133-135)
        if rec.type == INVALID || (pid && rec.pid != pid): continue   // (:136)
        // jiffies delta -> wall clock: now - rec.jiffies, subtracted from current realtime
        jiffies_to_timespec64(jiffies_now - rec.jiffies, &tv)    // (:142)
        tv = timespec64_sub(tv_now, tv); time64_to_tm(tv.tv_sec, 0, &tm)  // (:143-144)
        pr_info("neuron_log_dump: nd%02d: type: %s pid: %8u, data: %16llx, %4ld-…",
                nd->device_index, type_to_str(rec.type), rec.pid, rec.data, …)   // (:146-148)

    kfree(snapshot)                                           // (:151-153)
    return 0
```

> **NOTE —** the backward scan is bounded twice and slightly off from the documented "limit": the loop guard `i < NEURON_LOG_NUM_ENTRIES - 1` (`:113`) visits at most 1022 entries (never the full 1024), and the count test `j++ > log_dump_limit` (`:119`, post-increment with `>` rather than `>=`) admits `limit + 1` records before breaking. The effect is a print window of `limit + 1` records over a ring that can never be fully traversed — intentional slack for a debug dumper, but a reimplementer mirroring the "last N events" contract should use `>=` and a full `< 1024` (or head-tracked) bound. The dump also takes **no lock** against concurrent `neuron_log_rec_add`, so the snapshot can straddle a producer's publish; the per-record `INVALID` skip is the only consistency the dumper relies on. **(MED** — the bounds are direct reads; the "off-by-one vs documented limit" is the inference.**)**

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `neuron_log_rec_type_to_str` | `neuron_log.c:20` | enum → fixed-width label (`"FOPEN,  "`…) for the dump line | HIGH |
| `neuron_log_init` | `neuron_log.c:40` | `atomic_set(tail,0)` + `kzalloc` 1024 recs; `-ENOMEM` on fail | HIGH |
| `neuron_log_destroy` | `neuron_log.c:52` | `kfree(log)` if non-`NULL` | HIGH |
| `neuron_log_rec_add` | `neuron_log.c:59` | the lock-free `atomic_fetch_add` enqueue + publish-last `type` | HIGH |
| `neuron_log_dump` | `neuron_log.c:83` | snapshot → backward-scan window → forward-print with jiffies→wall-clock | HIGH |

---

## 4. The Support-TU Roster

Four further translation units are cross-cutting kernel-driver support: small, owned by no subsystem, consumed everywhere. Each is summarized here with its entry constants; the algorithmically-rich ones (the PID gate's full lifecycle, the semaphore/event addressing) are owned by their downstream pages and cited at the boundary.

**`neuron_pid.c` — the per-device PID-attach table.** A fixed 16-slot `attached_processes[]` array per device (`neuron_device.h:76`) of `{pid, open_count, memory_used[2], task*}`. `npid_attach` (`:81`) claims the first free slot for `task_tgid_nr(current)` on a normal `open(2)`, ref-counting `open_count`; `npid_detach` (`:115`) releases it. The function that matters to the security model is `npid_is_attached` (`:60`) — it returns the caller's `open_count` (0 if not attached) and is read as the **ioctl privilege gate** by `ncdev_ioctl`'s mutating-command dispatch ([ioctl-dispatch](ioctl-dispatch.md)). The per-process `memory_used[location-1]` accounting (`npid_add/dec_allocated_memory`, `:129/:135`) feeds sysfs/datastore. The slot lookup carries a tgid-reuse defence: `npid_find_process_slot_by_task` (`:29`) matches by `task_struct*` so a recycled tgid does not alias a dead process's slot. The full attach/gate/detach lifecycle is owned by [cdev-mmap](cdev-mmap.md).

**`neuron_reg_access.c` — three MMIO wrappers.** `reg_read32` (`:8`) does **not** issue a raw `readl`: it routes through `ndhal->ndhal_fw_io.fw_io_read_csr_array(&addr, value, 1, true)` — the DHAL "readless-read" path the Neuron CSR fabric requires (a plain MMIO read can hang the link, so reads are serviced via the firmware mailbox). `reg_write32` (`:13`) is a bare `writel`. `reg_write32_masked` (`:21`) is the read-modify-write: `reg_read32` then `writel(MASK_VAL(mask, data, temp))`, where `MASK_VAL(mask,data,bg) = (mask & data) | (~mask & bg)` (`:19`). These three are called pervasively across DHAL/ring/fw_io; the readless-read mechanism is owned by [fw-io](fw-io.md).

**`neuron_cinit.c` — the per-NeuronCore init-state handshake.** A small `INVALID → STARTED → COMPLETED` state machine (one `struct neuron_cinit {mutex; volatile state}` per NC, `nci[MAX_NC_PER_DEVICE]`, `neuron_device.h:112`) that coordinates user-space core init so two processes don't both initialize one core. `nci_set_state` (`:37`) under `nci_lock` promotes `INVALID→STARTED` (first initializer wins) or, if a peer is already `STARTED`, calls `nci_wait` (`:25`) — a poll of up to `NCI_RETRY_COUNT(10) × NCI_RETRY_SLEEP_MS(500ms) ≈ 5 s` (`:17-18`) for `COMPLETED`, returning regardless on timeout (no error). `nci_reset_state` (`:78`) slams every NC back to `INVALID`; it is called from probe's `neuron_pci_device_init` (`neuron_pci.c:136`) and from each reset (`nr_start_ncs`, [reset](reset.md)). It is driven by the `CINIT_SET_STATE` ioctl.

> **GOTCHA —** `nci_set_state` writes its `*new_state = nci->state` output **outside** `nci_lock` (`neuron_cinit.c:62`). A concurrent `nci_set_state`/`nci_reset_state` on another NC-init thread can change the state between the unlock and the read, so the value returned to userspace can be stale relative to the transition that just ran under the lock. The `volatile` qualifier prevents a torn read of the aligned `u32` but provides no ordering. **(MED** — direct read of the unlocked store; the staleness is inference.**)**

**`neuron_core.c` — semaphore/event MMIO primitives.** Six functions implementing the HW-sync primitives on each NeuronCore: `nc_semaphore_read/write/increment/decrement` (`:50/:68/:87/:106`) and `nc_event_get/set` (`:125/:142`), driven by the `SEMAPHORE_*`/`EVENT_*` ioctls. Each bound-checks the index, resolves a base address through a DHAL address-map callback (`nc_get_semaphore_base` / `nc_get_event_addr`), adds `index × NC_SEMAPHORE_SIZE(4)` (`neuron_core.h:9`), and issues a `writel` (mutators) or a readless `fw_io_read_csr_array` (readers). The semaphore stride and addressing are owned by the per-arch DHAL address map ([dhal-core](dhal-core.md)).

> **CORRECTION (CORE-01) —** the two event primitives bound-check with `event_index > ndhal->ndhal_address_map.event_count` (`neuron_core.c:130, :147`), whereas the four semaphore primitives correctly use `>=` (`:55, :73, :92, :111`). The `>` admits `event_index == event_count`, one slot past the valid `[0, event_count)` range, yielding an out-of-bounds MMIO address fed to the DHAL `nc_get_event_addr` callback. The asymmetry with the sibling semaphore checks makes this a clear off-by-one rather than an intentional inclusive bound; a reimplementation must use `>=` on both event paths. **(HIGH** — direct read; the four-vs-two asymmetry is unambiguous.**)**

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `npid_attach` | `neuron_pid.c:81` | claim a process slot; ref-count `open_count` | HIGH |
| `npid_detach` | `neuron_pid.c:115` | release a slot; clear `pid`/`task` at `open_count==0` | HIGH |
| `npid_is_attached` | `neuron_pid.c:60` | the ioctl privilege gate — `open_count` for `current` tgid | HIGH |
| `npid_find_process_slot_by_task` | `neuron_pid.c:29` | tgid-reuse-safe slot lookup by `task_struct*` | HIGH |
| `reg_read32` | `neuron_reg_access.c:8` | CSR read via DHAL readless-read (`fw_io_read_csr_array`) | HIGH |
| `reg_write32` / `reg_write32_masked` | `neuron_reg_access.c:13/:21` | raw `writel` / read-modify-write with `MASK_VAL` | HIGH |
| `nci_set_state` | `neuron_cinit.c:37` | `INVALID→STARTED→COMPLETED` handshake under `nci_lock` | HIGH |
| `nci_reset_state` | `neuron_cinit.c:78` | reset all NCs to `INVALID` (probe + reset) | HIGH |
| `nc_semaphore_read/write/increment/decrement` | `neuron_core.c:50/68/87/106` | NC semaphore MMIO, 4-byte stride, DHAL base | HIGH |
| `nc_event_get/set` | `neuron_core.c:125/142` | NC event MMIO — note the `>` off-by-one (CORE-01) | HIGH |

> **NOTE —** `neuron_core.h` declares `nc_get_nq_mem_handle` (twice) and `nc_nq_device_init` but **no `.c` defines them** anywhere in the tree — they are dead declarations; the real notification-queue handle code lives in `neuron_nq.c` ([notification-queues](notification-queues.md)). A reimplementer should not reproduce these stale prototypes. **(HIGH** — `rg` over the tree finds no definition.**)**

---

## 5. Module Metadata and DKMS Packaging

### Metadata

The `MODULE_*` macros, verbatim (`neuron_module.c:21-27`):

```c
MODULE_DESCRIPTION("Neuron Driver, built from SHA: 1c7ed9bd14936635773b5a01777882804ee8ea6e");  // :21
MODULE_LICENSE("GPL");                                                                            // :22
MODULE_VERSION("2.27.4.0");                                                                       // :23
MODULE_ALIAS("pci:v00001d0fd00007064sv*sd*bc*sc*i*");                                             // :24
const char driver_version[]  = "2.27.4.0";                                                        // :26
const char driver_revision[] = "1c7ed9bd14936635773b5a01777882804ee8ea6e";                        // :27
```

`MODULE_LICENSE("GPL")` (`:22`) is what makes the GPL-only kernel symbols this driver uses (`debugfs`, `pci_iomap`, the timekeeping helpers) linkable. `driver_version`/`driver_revision` are the strings the boot banner (§1) and the metrics constants print; they duplicate the `MODULE_VERSION`/`MODULE_DESCRIPTION` SHA, so a reimplementer changing the version must update both the macro and the C string.

> **CORRECTION (PCI-01) —** the udev autoload `MODULE_ALIAS` is `pci:v00001d0fd00007064sv*sd*bc*sc*i*` (`neuron_module.c:24`) — device-ID **`0x7064`**, which is **not** any of the five IDs the driver actually claims in `neuron_pci_dev_ids[]` (TRN1 `0x7164` / INF2 `0x7264` / TRN2 `0x7364` / TRN3 `0x7564`/`0x7565`, `neuron_pci.c:30-39`). `0x7064` appears nowhere else in the tree (`rg` confirmed). Runtime binding is driven by the `id_table`, so a *loaded* driver still matches its devices; but udev-based autoload keys on the modalias, so the alias points at a device-ID the driver never binds — autoload-by-modalias is effectively broken for every supported part. A reimplementer must generate the alias from the id_table (`MODULE_DEVICE_TABLE(pci, neuron_pci_dev_ids)`) instead of hand-writing this single static line. **(HIGH** — direct source read; also tracked from [pci-probe](pci-probe.md#the-pci-driver-and-id_table).**)**

### Fault-injection debugfs (optional)

When the kernel is built `CONFIG_FAULT_INJECTION`, `neuron_module_init_debugfs` (`:39-48`) creates a `debugfs` directory `"neuron"` (`:41`) and five fault-attr knobs that let a test harness force specific failures: `fail_nc_mmap`, `fail_dma_wait`, `fail_mc_alloc`, `fail_fwio_read`, `fail_fwio_post_metric` (`:42-47`). Each binds a `struct fault_attr` defined in its owning subsystem (extern'd at `:31-35`). `neuron_module_free_debugfs` (`:50-54`) does `debugfs_remove_recursive(dbgfs_root)` and NULLs the root. The whole block compiles out when the config is off; the knobs are inert on a production kernel.

### DKMS and Kbuild

The module is built out-of-tree by DKMS. `dkms.conf` fixes the package identity and module name:

```text
PACKAGE_NAME=aws-neuronx              # dkms.conf:1
PACKAGE_VERSION=2.27.4.0              # dkms.conf:2  — matches MODULE_VERSION
BUILT_MODULE_NAME[0]="neuron"         # dkms.conf:3  — the built object is neuron.ko
DEST_MODULE_LOCATION[0]=/kernel/drivers/neuron/   # dkms.conf:6
AUTOINSTALL=yes ; NO_WEAK_MODULES=yes ; REMAKE_INITRD=no            # dkms.conf:7-9
PRE_INSTALL=./preinstall ; POST_INSTALL=./postinstall ; POST_REMOVE=./postremove  # dkms.conf:10-12
```

`Kbuild` declares the single module object and its component list:

```text
obj-m += neuron.o                                       # Kbuild:1  — module object name = "neuron"
neuron-objs := neuron_arch.o neuron_dhal.o              # Kbuild:3
neuron-objs += neuron_module.o neuron_pci.o …           # Kbuild:5-19  (incl. v2/ v3/ v4/ vc/ udma/ subdirs)
ccflags-y += -O3 -Wall -Werror -Wno-declaration-after-statement -Wunused-macros …   # Kbuild:20
ccflags-y += -I$(src)/                                  # Kbuild:21
```

> **NOTE —** the module **object** name and the **driver** name differ, and both appear in sysfs at different paths. `Kbuild:1` `obj-m += neuron.o` plus `dkms.conf:3` `BUILT_MODULE_NAME[0]="neuron"` fix the module to `/sys/module/neuron` (and `KBUILD_MODNAME == "neuron"`, which feeds every `pr_fmt`); but `struct pci_driver.name = "neuron-driver"` (`neuron_pci.c:537`; the struct opens at `:536`) makes the bus driver node `/sys/bus/pci/drivers/neuron-driver`. A reimplementer wiring udev rules, the `modprobe -r` dup-rid self-unload (which uses the *module* name, [pci-probe](pci-probe.md#the-duplicate-routing-id-self-unload)), or `bind`/`unbind` sysfs scripts must use the right name for the right path. The whole driver compiles `-Werror` at `-O3` (`Kbuild:20`), so any new warning is a hard build failure — relevant when porting to a newer kernel whose headers tighten a prototype.

---

## Related Components

| Name | Relationship |
|---|---|
| `neuron_pci.c` | `neuron_pci_module_init`/`exit` are the second service `module_init`/`exit` registers; owns `pci_register_driver` and the probe body |
| `neuron_cdev.c` | `ncdev_module_init`/`exit` are the first service registered (chrdev region + class); fires every `neuron_log_rec_add` event |
| `neuron_device.h` | hosts `log_obj`, `attached_processes[16]`, `nci[]`, the `MAX_*` limits this page's TUs index |
| `neuron_dhal.{c,h}` | the `ndhal` vtable `neuron_pci_module_exit` cleans up; supplies the readless-read and address-map callbacks `reg_access`/`core` consume |
| `neuron_trace.h` | the `CREATE_TRACE_POINTS` instantiation lives in `neuron_module.c:16-17` — the module TU is the sole trace-point owner |

## Cross-References

- [Kernel Driver Overview](overview.md) — the Part III file map, the full request-lifecycle diagram, and where this skeleton sits in it
- [PCI Probe and Device Detection](pci-probe.md) — the `pci_driver`, the probe sequence and error-unwind ladder, the routing-id registry, and the `0x7064` alias (PCI-01) that `module_init` registers
- [Reset State Machine](reset.md) — `nci_reset_state` is called from probe and from each `nr_start_ncs` reset; the RESET→READY machine the cinit handshake coordinates against
- [Char Device, fops and mmap](cdev-mmap.md) — `ncdev_module_init` (the chrdev region this page orders first), the fops that fire the log-ring events, and the full PID-attach lifecycle
- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the per-arch `ndhal` bring-up (V4 overrides 8+ slots, not just device-id) and the address-map/readless callbacks `reg_access`/`core` drive through
- [back to index](../index.md)
