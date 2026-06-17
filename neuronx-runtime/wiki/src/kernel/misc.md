# Misc: Log Ring, PID, Trace, Reg, Core

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The seven support translation units catalogued here — `neuron_log.{c,h}`, `neuron_pid.{c,h}`, `neuron_trace.h`, `neuron_reg_access.{c,h}`, `neuron_cinit.{c,h}`, `neuron_core.{c,h}` — were read in full; every constant, enum, and accessor below is a `#define`, a struct field, or a literal line, not a recovered binary offset. The driver also ships as a stripped `neuron.ko`, but the GPL C is authoritative. Other driver versions renumber these lines.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. · Part III — Kernel Driver, REFERENCE · [back to overview](overview.md)*

## Abstract

This page is the reference catalogue for the small, cross-cutting support translation units the rest of the Neuron driver builds on but that no single subsystem owns: the per-device **event log ring** (`neuron_log.c`), the per-device **PID-attach table** that doubles as the ioctl privilege gate (`neuron_pid.c`), the header-only **ftrace event schema** (`neuron_trace.h`), the three **MMIO register-access wrappers** (`neuron_reg_access.c`), the per-NeuronCore **core-init handshake** (`neuron_cinit.c`), and the NeuronCore **semaphore/event MMIO engine** (`neuron_core.c`). Each is a tight TU — a few functions, one or two structs — that surfaces a primitive consumed across the cdev, DMA, DHAL, and reset cells. The intent of this page is that a reimplementer can find any one of them by name, learn its role, its key structs, and its full function map with confidence labels, in one place.

Two of these TUs are documented in algorithmic depth elsewhere and are summarized-plus-linked here to avoid duplication: the lock-free log ring's enqueue/dump algorithm is owned by [module-layout §3](module-layout.md#3-the-lock-free-log-ring-neuron_logc), and the semaphore/event *consumer* side — how the `SEMAPHORE_*`/`EVENT_*` ioctls marshal into the engine — is owned by [ioctl-nq](ioctl-nq.md). The remaining material is genuinely owned here: the register-access primitives (§4) and the `nc_semaphore_*`/`nc_event_*` engine internals (§6) get real C pseudocode, because no other page derives them. Every claim is `file:line`-anchored; the handful of bugs and smells observed during the source read are flagged in place as text-marker callouts.

| TU | Role | Key entry points | Owned-elsewhere? | Confidence |
|---|---|---|---|---|
| `neuron_log.c` | per-device 1024-entry lock-free event ring; 4 cdev lifecycle events; IOCTL-117 dmesg dump | `neuron_log_rec_add` (`:59`), `neuron_log_dump` (`:83`) | ring **algorithm** → [module-layout §3](module-layout.md#3-the-lock-free-log-ring-neuron_logc) | HIGH |
| `neuron_pid.c` | per-device 16-slot PID-attach table; the ioctl privilege gate; per-process mem accounting | `npid_attach` (`:81`), `npid_is_attached` (`:60`), `npid_detach` (`:115`) | attach/gate **lifecycle** → [cdev-mmap](cdev-mmap.md) | HIGH |
| `neuron_trace.h` | header-only ftrace schema, 16 `TRACE_EVENT`s on `TRACE_SYSTEM=neuron` | `CREATE_TRACE_POINTS` site `neuron_module.c:16-17` | fire sites in ring/dma/cdev/metrics (boundary) | HIGH |
| `neuron_reg_access.c` | 3 MMIO wrappers: readless read, raw write, masked RMW | `reg_read32` (`:8`), `reg_write32` (`:13`), `reg_write32_masked` (`:21`) | **owned here** (readless mechanism → [fw-io](fw-io.md)) | HIGH |
| `neuron_cinit.c` | per-NC `INVALID→STARTED→COMPLETED` init handshake; ~5 s wait | `nci_set_state` (`:37`), `nci_reset_state` (`:78`) | partial summary in [module-layout §4](module-layout.md#4-the-support-tu-roster) | HIGH |
| `neuron_core.c` | NeuronCore semaphore/event MMIO primitives, 4-byte stride, DHAL base | `nc_semaphore_*` (`:50/68/87/106`), `nc_event_*` (`:125/142`) | **owned here**; ioctl marshalling → [ioctl-nq](ioctl-nq.md) | HIGH |

---

## 1. `neuron_log.c` — Per-Device Event Log Ring

### Purpose

`neuron_log.c` is a per-device circular event log: a fixed 1024-entry ring (`NEURON_LOG_NUM_ENTRIES`, `neuron_log.c:18`) of `struct neuron_log_rec`, one `struct neuron_log_obj` embedded in each `struct neuron_device` (`neuron_device.h:118`). It records the four char-device lifecycle events — `FILE_OPEN`, `FILE_FLUSH`, `FILE_IOCTL`, `FILE_MMAP` (`neuron_log.h:21-27`) — fired from `ncdev_open`/`flush`/`ioctl`/`mmap`, and is dumped to `dmesg` on demand by the `NC_PID_STATE_DUMP` ioctl (cmd 117, [ioctl-catalog](ioctl-catalog.md)). It is allocated at probe (`neuron_pci.c:365`, warn-only on failure) and freed at remove (`:490` error path, `:531` happy path).

### Algorithm

The producer is a single `atomic_fetch_add` on a monotonic `tail` (the live index is `tail % 1024`, a power-of-two AND-mask) followed by a publish-last store of the record `type`; the consumer snapshots the whole ring under no lock, backward-scans to the window start (bounded by `log_dump_limit`, default 128), then prints forward with a jiffies→wall-clock reconstruction. The full enqueue and dump pseudocode, the publish-last protocol, the missing `smp_wmb()` torn-read window, and the off-by-one in the dump's scan bounds are owned by [module-layout §3](module-layout.md#3-the-lock-free-log-ring-neuron_logc) and are **not** re-derived here.

> **NOTE —** `neuron_log_rec_type_to_str` (`neuron_log.c:20-37`) maps the enum to *fixed-width* labels (`"FOPEN,  "`, `"FFLUSH, "`, `"IOCTL,  "`, `"MMAP,   "`, `"INVALID,"`, `"UNKNOWN,"`) so the dmesg dump columns align; the `data` field is type-overloaded — a `file*` for open/flush/mmap, the ioctl `cmd` for the IOCTL record (`neuron_log.h:23-26`). The `FILE_MMAP` header comment still reads "ioctl" (`neuron_log.h:26`); it is a stale comment, the value passed is the `file*`.

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `neuron_log_rec_type_to_str` | `neuron_log.c:20` | enum → fixed-width label for the dump line | HIGH |
| `neuron_log_init` | `neuron_log.c:40` | `atomic_set(tail,0)` + `kzalloc` 1024 recs; `-ENOMEM` on fail | HIGH |
| `neuron_log_destroy` | `neuron_log.c:52` | `kfree(log)` if non-`NULL` | HIGH |
| `neuron_log_rec_add` | `neuron_log.c:59` | lock-free `atomic_fetch_add` enqueue + publish-last `type` (algo → module-layout §3) | HIGH |
| `neuron_log_dump` | `neuron_log.c:83` | snapshot → backward-scan window → forward-print; dmesg line at `:146` | HIGH |

---

## 2. `neuron_pid.c` — Per-Device PID-Attach Table

### Purpose

A fixed 16-slot `attached_processes[NEURON_MAX_PROCESS_PER_DEVICE]` table per device (`neuron_device.h:76`; `NEURON_MAX_PROCESS_PER_DEVICE = 16`, `share/neuron_driver_shared.h:154`), each slot a `struct neuron_attached_process {pid_t pid; int open_count; size_t memory_used[2]; void *task;}` (`neuron_pid.h:18-23`). It is the per-device attach gate: a process must hold a slot to issue mutating ioctls. `npid_is_attached` (`:60`) is the function read by `ncdev_ioctl`'s privileged-command dispatch as the **ioctl privilege gate** ([ioctl-dispatch](ioctl-dispatch.md), [ioctl-nq](ioctl-nq.md)); the full open→attach→ioctl-gate→flush→detach lifecycle is owned by [cdev-mmap](cdev-mmap.md).

### Key Structs and Lookup

A slot is claimed by `task_tgid_nr(current)` and freed (`pid=0`) at `open_count==0`. Two lookups exist: `npid_find_process_slot` (`:19`) keys on the tgid, while `npid_find_process_slot_by_task` (`:29`) keys on the `task_struct*` — the latter is a **tgid-reuse defence** so a recycled tgid does not alias a dead process's slot. The per-process `memory_used[location-1]` accounting (`mem_location` is `MEM_LOC_HOST=1`/`MEM_LOC_DEVICE=2`, `neuron_mempool.h:30-34`, so host→`[0]`, device→`[1]`) feeds sysfs/datastore aggregation; it is updated from the mem alloc/free path (boundary mempool).

> **GOTCHA —** `npid_get_allocated_memory(nd, pid, *host, *device)` (`neuron_pid.c:143`) **ignores its `pid` argument**. The body uses `NPID_GET_SLOT()` (`:108`), which keys on `task_tgid_nr(current)`, not the passed `pid` — so it always returns the *calling* process's memory, never the requested one, despite the header doc saying "for the given pid" (`neuron_pid.h:108-116`). A privileged caller querying another pid's usage gets the wrong (or empty) result. **(HIGH** — direct read of the macro vs the parameter.**)**

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `npid_find_process_slot` | `neuron_pid.c:19` | linear scan for `pid == current tgid`; `-1` if none | HIGH |
| `npid_find_process_slot_by_task` | `neuron_pid.c:29` | tgid-reuse-safe lookup by `task_struct*` | HIGH |
| `npid_is_attached_task` | `neuron_pid.c:39` | `open_count` by task; logs new-vs-old pid mismatch | HIGH |
| `npid_attached_process_count` | `neuron_pid.c:50` | count of slots with `pid != 0` | HIGH |
| `npid_is_attached` | `neuron_pid.c:60` | **the privilege gate** — `open_count` for `current` tgid (0 = not attached) | HIGH |
| `npid_print_usage` | `neuron_pid.c:69` | `pr_info` dump of every `{pid, open_count}` slot | HIGH |
| `npid_attach` | `neuron_pid.c:81` | claim/ref-count slot; `BUG_ON(open_count<=0)` on re-attach; `false` if full (16) | HIGH |
| `npid_detach` | `neuron_pid.c:115` | `--open_count`; clear `pid`/`task` at 0; returns remaining; `BUG_ON(open_count==0)` | HIGH |
| `npid_add_allocated_memory` | `neuron_pid.c:129` | `memory_used[location-1] += amount`; `-1` if no slot | HIGH |
| `npid_dec_allocated_memory` | `neuron_pid.c:135` | saturating subtract (clamps at 0); `-1` if no slot | HIGH |
| `npid_get_allocated_memory` | `neuron_pid.c:143` | outputs host/device usage — **ignores `pid` arg** (see GOTCHA) | HIGH |

---

## 3. `neuron_trace.h` — Ftrace Event Schema (Header-Only)

### Purpose

`neuron_trace.h` defines the `TRACE_EVENT` set for the `neuron` ftrace subsystem (`TRACE_SYSTEM neuron`, `neuron_trace.h:14`). It is header-only — there is no `neuron_trace.c`; the single `CREATE_TRACE_POINTS` instantiation lives in `neuron_module.c:16-17`, making the module TU the sole trace-point owner. Including this header in a consumer TU compiles in the trace hooks; the fire sites are scattered across the ring, DMA, cdev, and metrics cells (boundary).

### The 16 Events

The schema is 16 events across five concern groups: DMA engine/queue lifecycle, DMA descriptor/ack/copy, ioctl mem operations, register `bar_read`/`bar_write`, and `metrics_post`. Rather than dump every `TP_printk`, the table gives each event's primary fire site (verified by `rg` across the tree); four events have no in-tree fire site (dead or userspace-only probes).

| `TRACE_EVENT` | `:line` | Primary fire site | Confidence |
|---|---|---|---|
| `dma_engine_init` | `:16` | `neuron_ring.c:681` | HIGH |
| `dma_queue_init` | `:33` | `neuron_ring.c:174` | HIGH |
| `dma_queue_release` | `:72` | `neuron_ring.c:340` | HIGH |
| `dma_desc_copy` | `:91` | (no in-tree fire site) | HIGH |
| `dma_queue_copy_start` | `:127` | `neuron_ring.c:319` | HIGH |
| `dma_ack_completed` | `:153` | `neuron_ring.c:282` | HIGH |
| `ioctl_mem_alloc` | `:176` | `neuron_cdev.c:442,537,704` | HIGH |
| `ioctl_mem_free` | `:198` | (no in-tree fire site) | HIGH |
| `ioctl_mem_copy` | `:218` | `neuron_cdev.c:819,899,1091,1093` | HIGH |
| `ioctl_mem_copyin` | `:242` | (no in-tree fire site) | HIGH |
| `ioctl_mem_copyout` | `:268` | (no in-tree fire site) | HIGH |
| `dma_memcpy` | `:294` | `neuron_dma.c:355` | HIGH |
| `bar_write` | `:323` | `neuron_cdev.c:1573` | HIGH |
| `bar_read` | `:345` | `neuron_cdev.c:1511,1539` | HIGH |
| `metrics_post` | `:369` | `neuron_metrics.c:442` (`METRICS_MAX_VALUE_LENGTH = 128`, `:367`) | HIGH |

> **CORRECTION (TRACE-rxtx) —** the `dma_queue_init` event's `TP_printk` **swaps rx and tx**: it prints `rx_mc->pa` under the `"tx %llx"` label and `tx_mc->pa` under the `"rx %llx"` label (`neuron_trace.h:60,66-67`). The recorded fields are correct; only the human-readable formatting is mislabeled, so anyone reading raw ftrace output for this event sees the two memory-chunk addresses under the wrong labels. **(HIGH** — direct read of the format string vs the `__entry` assignments; cosmetic.**)**

---

## 4. `neuron_reg_access.c` — MMIO Register-Access Wrappers

### Purpose

Three thin MMIO primitives consumed pervasively across the DHAL, ring, and fw_io cells. The non-obvious one is `reg_read32`: it does **not** issue a raw `readl`. A plain MMIO read against the Neuron CSR fabric can hang the link, so reads are serviced through the DHAL "readless-read" path — the firmware mailbox `fw_io_read_csr_array` (the mechanism is owned by [fw-io](fw-io.md)). Writes are bare `writel`; the masked variant is a read-modify-write built on the other two.

### Algorithm

This TU is genuinely owned here, so the three wrappers are given in full (`neuron_reg_access.c:8-25`, `MASK_VAL` at `:19`):

```c
// reg_read32 @ neuron_reg_access.c:8  — readless read via DHAL firmware mailbox
function reg_read32(addr, *value):
    // NOT a raw readl: route through the DHAL readless-read path.
    // args: (&addr, value, count=1, readless=true)
    return ndhal->ndhal_fw_io.fw_io_read_csr_array(&addr, value, 1, true)   // (:9)

// reg_write32 @ neuron_reg_access.c:13  — raw MMIO write
function reg_write32(addr, value):
    writel(value, addr)                                                     // (:14)

// MASK_VAL(mask, data, background) @ neuron_reg_access.c:19
//   = (mask & data) | (~mask & background)   — keep masked bits of data, rest of background

// reg_write32_masked @ neuron_reg_access.c:21  — read-modify-write
function reg_write32_masked(addr, mask, data):
    ret = reg_read32(addr, &temp)              // readless read of current value (:23)
    if ret == 0:                               // only write on a clean read
        writel(MASK_VAL(mask, data, temp), addr)   // merge masked bits into background (:24)
    return ret                                 // propagate read error to caller (:25)
```

> **QUIRK —** the read/write asymmetry is deliberate, not an oversight. Reads go through the firmware mailbox (`fw_io_read_csr_array`) because a direct `readl` on the CSR fabric can stall the PCIe link; writes are posted and use a plain `writel`. A reimplementer who "simplifies" `reg_read32` to a `readl` to match `reg_write32` reintroduces exactly the hang the readless path exists to avoid. `reg_write32_masked` only writes when the read returned 0, so a failed readless read aborts the RMW cleanly rather than writing a stale-background merge.

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `reg_read32` | `neuron_reg_access.c:8` | CSR read via DHAL readless-read (`fw_io_read_csr_array`) | HIGH |
| `reg_write32` | `neuron_reg_access.c:13` | raw posted `writel` | HIGH |
| `reg_write32_masked` | `neuron_reg_access.c:21` | read-modify-write with `MASK_VAL`; aborts on read error | HIGH |

---

## 5. `neuron_cinit.c` — Per-NeuronCore Init-State Handshake

### Purpose

A small `INVALID → STARTED → COMPLETED` state machine that coordinates user-space core init so two processes do not both initialize one NeuronCore. State is one `struct neuron_cinit {struct mutex nci_lock; volatile enum neuron_cinit_state state;}` per NC (`neuron_cinit.h:15-18`), held as `nci[MAX_NC_PER_DEVICE]` in the device (`neuron_device.h:112`, `MAX_NC_PER_DEVICE = 8`). The state enum values are `STARTED=1, COMPLETED=2, INVALID=3` (`share/neuron_driver_shared.h:66-70`). It is driven by the `CINIT_SET_STATE` ioctl (cmd 91); probe (`neuron_pci.c:136`) and each reset (`nr_start_ncs`, [reset](reset.md)) slam every NC back to `INVALID`.

### Algorithm

`nci_set_state` (`:37`) runs the transition under `nci_lock`. The first initializer to find `INVALID` promotes to `STARTED` (`nci_start`, `:20`); a second process arriving while `STARTED` calls `nci_wait` (`:25`) — a poll of up to `NCI_RETRY_COUNT(10) × NCI_RETRY_SLEEP_MS(500ms) ≈ 5 s` (`:17-18`) for `COMPLETED`, returning regardless on timeout (no error is raised). The `COMPLETED` request promotes `STARTED→COMPLETED` under the lock, else logs `pr_err`.

> **GOTCHA —** `nci_set_state` writes its `*new_state = nci->state` output **outside** `nci_lock` (`neuron_cinit.c:62`). A concurrent `nci_set_state`/`nci_reset_state` on another NC-init thread can change the state between the unlock and that read, so the value returned to userspace can be stale relative to the transition that just ran under the lock. The `volatile` qualifier prevents a torn read of the aligned `u32` but provides no ordering. The `current_state` captured at `:44`/`:69` is also unused on the `COMPLETED`/reset paths. **(MED** — direct read of the unlocked store; the staleness is inference.**)**

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `nci_start` | `neuron_cinit.c:20` | set `state = STARTED` (inline helper) | HIGH |
| `nci_wait` | `neuron_cinit.c:25` | poll ≤ 10 × 500 ms for `COMPLETED`; no error on timeout | HIGH |
| `nci_set_state` | `neuron_cinit.c:37` | `INVALID→STARTED→COMPLETED` under `nci_lock`; unlocked `*new_state` write at `:62` | HIGH |
| `nci_reset_state_nc` | `neuron_cinit.c:66` | one NC → `INVALID` under lock | HIGH |
| `nci_reset_state` | `neuron_cinit.c:78` | loop `nci_reset_state_nc` over `MAX_NC_PER_DEVICE` | HIGH |

---

## 6. `neuron_core.c` — NeuronCore Semaphore/Event MMIO Engine

### Purpose

Six MMIO primitives implementing the hardware-sync hardware on each NeuronCore: `nc_semaphore_read/write/increment/decrement` (`:50/68/87/106`) and `nc_event_get/set` (`:125/142`). They are driven by the `SEMAPHORE_*`/`EVENT_*` ioctls (cmds 41-46); how those ioctls marshal into these functions — the `ncdev_semaphore_ioctl`/`ncdev_events_ioctl` multiplexers and the `nc_id` range check — is owned by [ioctl-nq](ioctl-nq.md). The TopSP shares this sync-primitive model (signed-32-bit semaphores, 1-bit events; see [topsp](topsp.md)). This engine is genuinely owned here, so the addressing and the accessor set are given in full.

### Addressing Model

Each accessor (a) bound-checks the index against the DHAL address-map count, (b) resolves a per-NC base address through a DHAL callback (`nc_get_semaphore_base` for semaphores, `nc_get_event_addr` for events), (c) adds `index × stride` where `NC_SEMAPHORE_SIZE = NC_EVENT_SIZE = 4` (`neuron_core.h:9-10`) plus the per-operation mmap offset from the address map, and (d) issues a readless `fw_io_read_csr_array` (readers) or a `writel` (mutators). The four semaphore operations each use a *different* mmap offset, so read/set/incr/decr address four distinct register windows for the same semaphore index.

```c
// nc_semaphore engine @ neuron_core.c:50-123  — four ops, four address-map offsets
const NC_SEMAPHORE_SIZE = 4              // byte stride per semaphore (neuron_core.h:9)
am = &ndhal->ndhal_address_map           // DHAL per-arch address map (neuron_dhal.h)

function nc_semaphore_read(nd, nc_id, index, *result):       // :50
    if index >= am->semaphore_count: return -EINVAL          // :55  (>= — correct)
    if nc_get_semaphore_base(nd, nc_id, &addr) != 0:         // DHAL base (:58)
        pr_err("failed to retrieve semaphore base"); return err   // :60
    addr += am->mmap_nc_sema_read_offset + index * NC_SEMAPHORE_SIZE
    return fw_io_read_csr_array(&addr, result, 1, true)      // readless read

function nc_semaphore_write(nd, nc_id, index, value):        // :68
    if index >= am->semaphore_count: return -EINVAL          // :73
    nc_get_semaphore_base(nd, nc_id, &addr)                  // (err → :78 pr_err)
    addr += am->mmap_nc_sema_set_offset  + index * NC_SEMAPHORE_SIZE
    writel(value, addr)

function nc_semaphore_increment(nd, nc_id, index, value):    // :87
    if index >= am->semaphore_count: return -EINVAL          // :92
    nc_get_semaphore_base(nd, nc_id, &addr)                  // (err → :97 pr_err)
    addr += am->mmap_nc_sema_incr_offset + index * NC_SEMAPHORE_SIZE
    writel(value, addr)

function nc_semaphore_decrement(nd, nc_id, index, value):    // :106
    if index >= am->semaphore_count: return -EINVAL          // :111
    nc_get_semaphore_base(nd, nc_id, &addr)                  // (err → :116 pr_err)
    addr += am->mmap_nc_sema_decr_offset + index * NC_SEMAPHORE_SIZE
    writel(value, addr)

// nc_event engine @ neuron_core.c:125-160  — two ops, DHAL-resolved per-event addr
function nc_event_get(nd, nc_id, index, *result):            // :125
    if index > am->event_count: return -EINVAL              // :130  (> — OFF-BY-ONE, see CORRECTION)
    if nc_get_event_addr(nd, nc_id, index, &addr) != 0:     // DHAL resolves the full event addr
        pr_err("failed to retrieve event %u addr", index); return err   // :135
    return fw_io_read_csr_array(&addr, result, 1, true)     // readless read

function nc_event_set(nd, nc_id, index, value):             // :142
    if index > am->event_count: return -EINVAL             // :147  (> — same off-by-one)
    nc_get_event_addr(nd, nc_id, index, &addr)             // (err → :152 pr_err)
    writel(value, addr)
```

> **CORRECTION (CORE-01) —** the two event primitives bound-check with `event_index > am->event_count` (`neuron_core.c:130, :147`), whereas the four semaphore primitives correctly use `>=` (`:55, :73, :92, :111`). The `>` admits `event_index == event_count`, one slot past the valid `[0, event_count)` range, yielding an out-of-bounds event address fed to the DHAL `nc_get_event_addr` callback. The asymmetry with the sibling semaphore checks makes this a clear off-by-one rather than an intentional inclusive bound; a reimplementation must use `>=` on both event paths. **(HIGH** — direct read; the four-vs-two asymmetry is unambiguous.**)**

> **NOTE —** `neuron_core.h` declares `nc_get_nq_mem_handle` (twice, `:102/:115`) and `nc_nq_device_init` (`:123`) but **no `.c` defines them** anywhere in the tree — they are dead declarations; the real notification-queue handle code lives in `neuron_nq.c` ([notification-queues](notification-queues.md)). A reimplementer should not reproduce these stale prototypes. The header also pins `MAX_NQ_TYPE = 6`, `MAX_NQ_ENGINE = 16`, `MAX_NQ_SUPPORTED = 96` (`:86-89`), consumed by the NQ engine, not by this TU. **(HIGH** — `rg` over the tree finds no definition.**)**

### Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `nc_semaphore_read` | `neuron_core.c:50` | readless read; `mmap_nc_sema_read_offset + index*4`; `>=` bound | HIGH |
| `nc_semaphore_write` | `neuron_core.c:68` | `writel`; `mmap_nc_sema_set_offset + index*4` | HIGH |
| `nc_semaphore_increment` | `neuron_core.c:87` | `writel`; `mmap_nc_sema_incr_offset + index*4` | HIGH |
| `nc_semaphore_decrement` | `neuron_core.c:106` | `writel`; `mmap_nc_sema_decr_offset + index*4` | HIGH |
| `nc_event_get` | `neuron_core.c:125` | readless read; DHAL `nc_get_event_addr` — `>` off-by-one (CORE-01) | HIGH |
| `nc_event_set` | `neuron_core.c:142` | `writel`; DHAL `nc_get_event_addr` — same off-by-one | HIGH |

---

## Cross-References

- [Module Layout and Build](module-layout.md) — owns the lock-free log-ring **algorithm** (§3: `atomic_fetch_add` enqueue, publish-last `type`, dump backward-scan/forward-print) and the support-TU roster summaries (§4) this page catalogues in full
- [Notification Queue Engine](notification-queues.md) — the NQ ring this page's semaphore/event engine sits beside; owner of the dead `nc_get_nq_mem_handle`/`nc_nq_device_init` declarations' real code (`neuron_nq.c`)
- [TopSP Notification Path](topsp.md) — shares the semaphore/event sync model (signed-32-bit semaphores, 1-bit events) and the CORE-01 event off-by-one
- [NQ and Semaphore IOCTL Handlers](ioctl-nq.md) — the `SEMAPHORE_*`/`EVENT_*` ioctl front door that marshals into `nc_semaphore_*`/`nc_event_*`; the `nc_id` range check the engine relies on
- [Kernel Driver Overview](overview.md) — the Part III file map and where these support TUs sit in the request lifecycle
