# Kernel Datastore

> *Map page for the kernel-driver lane (Part III). The kernel datastore is the device-driver half of the **Neuron DataStore (NDS)**; its mechanism is documented in full — slab table, allocation, per-process assignment, `mmap` publish, kernel read/walk, lifecycle — in [Part XIV → Kernel Side (Per-Process Slabs)](../datastore/kernel-side.md). This page orients a reader who arrives from the driver lane and routes them there without duplicating the algorithms. GPL-2.0 `aws-neuronx-dkms 2.27.4.0`, `neuron_ds.c` (245 lines) / `neuron_ds.h`. · [back to index](../index.md)*

## Abstract

`neuron_ds.c` is the driver TU that backs the NDS: at device probe it pre-allocates a fixed array of sixteen 256 KiB host-DRAM slabs — one per process that can be live on a device at once — and publishes them to userspace via `mmap`. The kernel never authors slab *contents*; it only `memset(0)`s a slab on (re)assignment and reads counter words back during metrics/sysfs aggregation. The byte layout inside a slab (`"nds\0"` magic, version word, region map, per-record FNV hashes) is written and validated entirely by userspace `libnds.a`.

Because the same TU sits in two organizing axes — the **kernel-driver lane** (this Part, grouped by driver TU) and the **NDS subsystem** (Part XIV, the cross-cutting shared-memory counter plane) — its deep treatment lives once, in Part XIV, and this lane entry points to it. See the NOTE below on why both nav slots exist.

## At a Glance

| | |
|---|---|
| **Owning TU** | `neuron_ds.c` / `neuron_ds.h` (GPL-2.0, dkms 2.27.4.0) |
| **Per-device state** | `struct neuron_datastore` embedded in `neuron_device` as field `datastore` |
| **Slab size** | `NEURON_DATASTORE_SIZE = 256 * 1024 = 262144` (`neuron_ds.h:17`) |
| **Slab count** | `16` = `NEURON_MAX_PROCESS_PER_DEVICE` (`neuron_ds.h:18`) — `4 MiB` host DRAM / device |
| **Slot record** | `{ pid_t pid; u64 clear_tick; struct mem_chunk *mc; }` (`neuron_ds.h:22-26`) |
| **Serialization** | one per-device mutex `nds->lock` (`neuron_ds.h:29`); **no per-slot lock, no refcount** |
| **Ownership** | single-owner-by-PID: occupied ⇔ `pid != 0`; release only by the owning TGID |
| **Reuse** | LRU by monotonic `clear_tick` over *unused* slots (never-used win first) |
| **Backing lifetime** | `MC_LIFESPAN_DEVICE` (`neuron_ds.c:23`) — release zeroes contents, keeps the chunk |
| **Full treatment** | [datastore/kernel-side.md](../datastore/kernel-side.md) (Part XIV) |

## The Four Kernel Paths

A reimplementer reproduces four driver paths; each is derived in full on the [Part XIV kernel-side page](../datastore/kernel-side.md):

1. **Slab-pool allocation, once per device** → all 16 backing chunks carved at probe. The pool never grows or shrinks thereafter.
2. **Per-process slab assignment** on `NEURON_IOCTL_ACQUIRE_NEURON_DS` → stamp `task_tgid_nr(current)` into a free slot (own slab), or attach read-only to an existing PID's slab (monitor).
3. **The `mmap`-offset publish** → acquire returns `{ nmmap_offset(mc), size = 262144 }`; userspace maps the same physical pages. The pgoff→PA cookie convention is owned by [cdev-mmap](cdev-mmap.md).
4. **The kernel read / walk path** → metrics and sysfs aggregation fold a live or dying process's counters into device-level telemetry under `nds->lock`, via the 3-section `get_neuroncore_counter_value` dispatch (`neuron_ds.c:207`) that mirrors the wire format's NC addressing.

> **NOTE — why two nav entries.** The wiki is organized along two axes that intersect at this TU: the kernel-driver lane (Part III) catalogues every driver TU, and the NDS subsystem (Part XIV) catalogues the cross-cutting counter plane spanning kernel + userspace + wire format. Rather than duplicate `neuron_ds.c`, the lane entry (this page) is a pointer and the subsystem page is canonical. The counter *contents*, the slab byte layout, and the userspace writer are owned by the Part XIV pages — see Cross-References.

## Cross-References

- [datastore/kernel-side.md](../datastore/kernel-side.md) — **canonical** deep treatment of `neuron_ds.c` (the four paths above, in full).
- [datastore/overview.md](../datastore/overview.md) — the NDS shared-memory counter plane, end to end.
- [datastore/wire-format.md](../datastore/wire-format.md) — the in-slab byte layout (magic, version, region map, FNV records) userspace owns.
- [kernel/cdev-mmap.md](cdev-mmap.md) — the `mmap` pgoff→PA cookie the publish path rides.
- [kernel/metrics.md](metrics.md) / [kernel/sysfs.md](sysfs.md) — the aggregation consumers of the kernel read/walk path.
