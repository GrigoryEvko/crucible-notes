# NQ and Semaphore IOCTL Handlers

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The handler bodies are read from `neuron_cdev.c` (4043 lines), the arg structs and command macros from `neuron_ioctl.h` (876 lines), the `NQ_TYPE`/`NQ_DEVICE_TYPE` enums from `share/neuron_driver_shared.h`, and the engine prototypes (`nc_semaphore_*`, `nc_event_*`) from `neuron_core.h`, (`nnq_init`) from `neuron_nq.h`, (`fw_io_read_counters`) from `neuron_fw_io.h`. The source is read directly, not reverse-engineered; every constant and offset is a `#define`, a struct field, or a literal line. Other driver versions renumber lines and add/remove commands. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

This family is the userspace front door to three device subsystems that the [NQ engine](notification-queues.md), the [NeuronCore CSR layer](topsp.md), and the [FW-IO mailbox](fw-io.md) own below it: the **notification-queue init trio** (`NOTIFICATIONS_INIT_V1`/`_V2`/`_WITH_REALLOC_V2`), the **multiplexed semaphore** handler (`SEMAPHORE_READ`/`WRITE`/`INCREMENT`/`DECREMENT`), the **multiplexed event** handler (`EVENT_GET`/`SET`), and `READ_HW_COUNTERS`. All eleven live commands are routed only by the privileged dispatcher `ncdev_ioctl` (`neuron_cdev.c:3188`) — none appears in the free-access misc lane (`ncdev_misc_ioctl`, `:3147`), so an `O_WRONLY` opener cannot reach any of them. Two of the eleven are not handlers at all: `NOTIFICATIONS_DESTROY_V1` is an inline `return 0` (`:3338-3339`) and `NOTIFICATIONS_QUEUE_INFO` is an inline `return -1` (`:3340-3341`); both are called out below because a reimplementer will otherwise hunt for missing code.

The shape is the family's defining property: every handler is a thin marshalling wrapper around exactly one engine call. `copy_from_user` a fixed arg struct → (sometimes) validate `nc_id` → call the engine (`nnq_init`/`ts_nq_init`/`nc_semaphore_*`/`nc_event_*`/`fw_io_read_counters`) → `copy_to_user` on the read-back paths. There is no ring logic, no CSR programming, and no notification consumption inside `neuron_cdev.c` — the NQ ring lifecycle (power-of-two sizing, host-vs-device backing, the three-register `nnq_set_hwaddr` program, the halt/destroy sweep) is owned entirely by the [NQ engine page](notification-queues.md); this page documents how each `ioctl` *marshals into* it and never re-derives the engine. The arg structs are the contract a reimplementer packs; the `mmap_offset` return is the cookie `libnrt` feeds to `mmap(2)` to drain the ring.

The pivot a reimplementer must internalize is the **NQ-init return contract** and its V1→V2→realloc evolution. All three init handlers return an `mmap_offset` — the host ring's physical address (`nmmap_offset(mc) == mc->pa`), or `-1` for a device-DRAM ring — that userspace mmaps to reach the producer's ring. V2 adds a second out-field, a `mem_handle` minted lazily by `ncdev_mem_chunk_to_mem_handle` (`:65`, the same handle the [mem family](ioctl-mem.md) returns from `MEM_ALLOC`), and a `nq_dev_type` discriminator that picks the NeuronCore engine (`nnq_init`) or the TopSP engine (`ndhal->ndhal_topsp.ts_nq_init`). The realloc variant is byte-for-byte V2 plus one `__u32 force_alloc_mem` that, when set, makes the engine free the old ring and allocate a fresh one — the only path that can re-back an NQ in place. The deprecated V1 hard-codes host memory and never returns a `mem_handle`.

For reimplementation, the contract is:

- **The three NQ-init handlers and their return mechanism** — each copies a fixed struct, dispatches to `nnq_init` (NeuronCore) or `ts_nq_init` (TopSP, V2/realloc only), and returns `mmap_offset`; V2/realloc additionally mint and return a `mem_handle`. V1 forwards hard-coded `on_host_memory=true, dram_channel=0, dram_region=0, force_alloc_mem=false`; realloc forwards the caller's `force_alloc_mem`.
- **The semaphore multiplexer** — one handler (`ncdev_semaphore_ioctl`) demuxes four commands by exact `cmd ==` to `nc_semaphore_read/write/increment/decrement`; only `READ` copies a value back; it is the *only* handler in the family that range-checks `nc_id` (`ncdev_ncid_valid`).
- **The event multiplexer and its missing `nc_id` check** — `ncdev_events_ioctl` is the semaphore handler's near-twin, demuxing `GET`/`SET` to `nc_event_get/set`, but it **omits** the `ncdev_ncid_valid` call (the [attack-surface S2](../security/ioctl-attack-surface.md) finding).
- **`READ_HW_COUNTERS`** — `kmalloc` two arrays sized by the user `count`, copy the address array in, `fw_io_read_counters`, copy the data array out; the OOM path on the second `kmalloc` returns success-with-no-data.
- **The two stubs** — `DESTROY_V1` → `return 0` (teardown is implicit on reset/flush), `QUEUE_INFO` → `return -1` (= `-EPERM` to userspace, *not* `-ENOSYS`).

| | |
|---|---|
| **Handler region** | `neuron_cdev.c:1428-1476` (sem/event), `:1694-1724` (hw-counters), `:1996-2074` (NQ-init trio) |
| **Dispatcher** | `ncdev_ioctl` (`:3188`); NQ/sem/event/counter branches `:3312-3343`; **no misc-lane member** |
| **`nc_id` gate** | `ncdev_ncid_valid` (`:95`) → `nc_id < ndhal_address_map.nc_per_device` else `-E2BIG`; called by sem (`:1437`), **not** by event |
| **Attach gate** | only the three NQ-init cmds in the `npid_is_attached` allow-list (`:3217-3219`); sem/event/hw-counters **ungated** (still non-free-access) |
| **mmap cookie** | `mmap_offset = nmmap_offset(mc) = mc->pa` (host ring) or `-1` (device ring) — minted by the NQ engine, [notification-queues](notification-queues.md) |
| **V2 mem_handle** | `ncdev_mem_chunk_to_mem_handle` (`:65`) → lazy `nmch_handle_alloc` — same handle table as [ioctl-mem](ioctl-mem.md) |
| **Engine boundary** | `nnq_init` (`neuron_nq.h:72`), `ts_nq_init` (DHAL ptr), `nc_semaphore_*`/`nc_event_*` (`neuron_core.h:22-82`), `fw_io_read_counters` (`neuron_fw_io.h:416`) |
| **Stubs** | `DESTROY_V1` → `return 0` (`:3338`); `QUEUE_INFO` → `return -1` (`:3340`) |
| **Confidence** | HIGH — every handler body, arg struct, command macro, dispatch line, and engine prototype read verbatim from the shipped source |

---

## 1. The Marshalling Shape and the NQ-Init Return Contract

Every handler in this family is the same three-beat the [mem](ioctl-mem.md) and [dma](ioctl-dma.md) families use: copy the fixed arg struct, validate, issue one engine call. The NQ subsystem has no in-kernel consumer — the kernel allocates the ring, programs three BAR0 registers, and hands back an `mmap` offset; userspace `mmap`s the ring and drains it by reading the hardware-advanced HEAD register the kernel never touches ([notification-queues §1](notification-queues.md)). So the *only* thing these init handlers own is the userspace ABI: the arg struct in, the `mmap_offset` (+ `mem_handle` for V2) out.

The three init commands form a versioned evolution, not three distinct features. They reach the *same* `nnq_init` (or `ts_nq_init`) engine entry; what differs is which fields the handler forwards and what it returns:

```text
NOTIFICATIONS_INIT_V1        nc_id-addressed, NeuronCore only, host memory forced.
  │  (deprecated)            returns mmap_offset.  No mem_handle, no device/realloc.
  ▼
NOTIFICATIONS_INIT_V2        nq_dev_id + nq_dev_type discriminator (NEURON_CORE | TOPSP).
  │                          honours on_host_memory / dram_channel / dram_region.
  │                          returns mmap_offset AND a lazily-minted mem_handle.
  ▼
NOTIFICATIONS_INIT_WITH_REALLOC_V2   = V2 + one __u32 force_alloc_mem.
                             force_alloc_mem=true → engine frees old ring, allocs fresh.
```

> **NOTE —** the `mmap_offset` returned by every init handler is **not** a generic offset — for a host ring it is the ring's *physical address* (`nmmap_offset(mc)` is a one-line `return mc->pa`, [notification-queues §5](notification-queues.md)), and for a device-DRAM ring it is `-1` (the ring is not host-mappable; the consumer drains it by device DMA). Userspace `mmap`s with `pgoff = mmap_offset >> PAGE_SHIFT`, and `ncdev_mmap` reverses the shift to find the chunk in the PA-keyed rbtree — the same cookie convention the [mem family](ioctl-mem.md) uses for `MEM_GET_INFO`. The handlers here never compute the cookie; the NQ engine writes it into `arg.mmap_offset` and the handler `copy_to_user`s the whole struct back.

> **QUIRK —** there is **no per-NQ destroy** ioctl in this family. `NOTIFICATIONS_DESTROY_V1` is an inline `return 0` (`:3338-3339`); the `struct neuron_ioctl_notifications_destroy` it nominally consumes (`neuron_ioctl.h:386-388`, a single `__u64 mmap_offset`) is read by no kernel code. NQ teardown is bulk-only, driven by core reset (`nnq_destroy_nc`) or device removal (`nnq_destroy_all`), owned by [notification-queues §5.1](notification-queues.md). `libnrt`'s `ndl_notification_destroy` does the userspace `munmap` of the ring and relies on the kernel reclaiming the backing memory at reset/exit. A reimplementer must not pair every `INIT` with a `DESTROY` ioctl — the destroy command is a no-op.

### The lazy `mem_handle` mint (V2 / realloc)

V2 and realloc return a second out-field beyond `mmap_offset`: a `u64 mem_handle`, minted through the same helper the [mem family](ioctl-mem.md) uses, so userspace can later name the ring `mem_chunk` by handle:

```c
// neuron_cdev.c:65 — mc → handle, allocate on first publication (shared with the mem family)
function ncdev_mem_chunk_to_mem_handle(nd, mc, *mh):
    if mc->mc_handle == NMCH_INVALID_HANDLE:        // :68  not yet published
        nmch_handle_alloc(nd, mc, &mc->mc_handle)   // :69  pop free-list head, lazily kzalloc next L2 tbl
    *mh = (u64)mc->mc_handle                          // :71
    return ret                                       // :72  (alloc failure propagates up)
```

The handler calls this *after* the engine returns the ring `mc` and *before* the final `copy_to_user`, so the returned struct carries both keys. V1 never calls it — its struct has no `mem_handle` field. The handle-table internals (the 2-level table, the free-list, `MEMCHUNK_MAGIC`) are owned by [mempool-handles](mempool-handles.md).

---

## 2. NQ-Init Trio

### Purpose

Create one notification-queue ring on a NeuronCore or TopSP, program its hardware base/size registers (via the engine), and return the `mmap` offset (and, for V2/realloc, the `mem_handle`). The three handlers share an identical body shape and differ only in their forwarded arguments and their return fields.

### Entry Point

```text
ioctl NOTIFICATIONS_INIT_V1               → ncdev_ioctl :3332  [attach-gated :3217]
  └─ ncdev_nc_nq_init_deprecated  (:1996)  → nnq_init(... true,0,0, false ...)
ioctl NOTIFICATIONS_INIT_V2               → ncdev_ioctl :3334  [attach-gated :3218]
  └─ ncdev_nc_nq_init_libnrt      (:2014)  → NEURON_CORE: nnq_init ; TOPSP: ts_nq_init   (force_alloc=false)
ioctl NOTIFICATIONS_INIT_WITH_REALLOC_V2  → ncdev_ioctl :3336  [attach-gated :3219]
  └─ ncdev_nc_nq_init_with_realloc_libnrt (:2045) → same, forwarding arg.force_alloc_mem
```

### Algorithm — the V2 handler (the libnrt path)

V2 is the most instructive: it carries the `nq_dev_type` branch, the host/device fields, and the `mem_handle` mint. V1 is this body with the branch removed and the arguments hard-coded; realloc is this body with `false` replaced by `arg.force_alloc_mem`.

```c
// neuron_cdev.c:2014 — create one NQ, return mmap_offset + mem_handle
function ncdev_nc_nq_init_libnrt(nd, param):
    copy_from_user(&arg, param, sizeof(notifications_init_v2))   // :2020  48 bytes

    if arg.nq_dev_type == NQ_DEVICE_TYPE_NEURON_CORE:            // :2025  (= 0)
        ret = nnq_init(nd, arg.nq_dev_id, arg.engine_index, arg.nq_type, arg.size,
                       arg.on_host_memory, arg.dram_channel, arg.dram_region,
                       false, &mc, &arg.mmap_offset)            // :2026  → NQ engine (NeuronCore)
    elif arg.nq_dev_type == NQ_DEVICE_TYPE_TOPSP:               // :2029  (= 1)
        ret = ndhal->ndhal_topsp.ts_nq_init(nd, arg.nq_dev_id, arg.engine_index,
                       arg.nq_type, arg.size, arg.on_host_memory,
                       arg.dram_channel, arg.dram_region,
                       false, &mc, &arg.mmap_offset)            // :2030  → DHAL TopSP NQ init
    else:
        return -ENOSYS                                          // :2033  unknown device type
    if ret: return ret                                         // :2036  engine failure propagates

    ret = ncdev_mem_chunk_to_mem_handle(nd, mc, &arg.mem_handle) // :2038  lazy handle mint
    if ret: return ret                                         // :2040

    return copy_to_user(param, &arg, sizeof(arg))              // :2042  mmap_offset + mem_handle out
```

Three facts a reimplementer must preserve. First, the `nq_dev_type` discriminator is **ABI-frozen** at `NEURON_CORE=0`/`TOPSP=1` (`share/neuron_driver_shared.h:107-115`): the header comment (`:105-110`) records that runtime v5 passed this as a `bool` (true=TopSP), and the enum values are pinned to that bool until the minimum compat version reaches ≥6. Second, the handler passes `arg.nq_type` straight through to the engine with **no** range-check against `NQ_TYPE_MAX` (6, `share/neuron_driver_shared.h:124`) — any validation is the engine's job ([notification-queues §2](notification-queues.md)). Third, an unknown `nq_dev_type` is `-ENOSYS` (`:2033`), distinct from the `-E2BIG`/`-EINVAL` the engine returns for a bad core/size.

### The V1 and realloc deltas

```c
// neuron_cdev.c:1996 — V1: NeuronCore only, host memory forced, no mem_handle
function ncdev_nc_nq_init_deprecated(nd, param):
    copy_from_user(&arg, param, sizeof(notifications_init_v1))   // :2002  24 bytes
    ret = nnq_init(nd, arg.nc_id, arg.engine_index, arg.nq_type, arg.size,
                   true, 0, 0, false, &mc, &arg.mmap_offset)    // :2006  host=true, ch=0, rgn=0, no realloc
    if ret: return ret
    return copy_to_user(param, &arg, sizeof(arg))              // :2011  mmap_offset only

// neuron_cdev.c:2045 — realloc: V2 body, but forwards arg.force_alloc_mem
function ncdev_nc_nq_init_with_realloc_libnrt(nd, param):
    copy_from_user(&arg, param, sizeof(notifications_init_with_realloc_v2))  // :2051  56 bytes
    ... identical NEURON_CORE / TOPSP branch as V2 ...                       // :2056-2065
    //   but the 9th nnq_init/ts_nq_init argument is arg.force_alloc_mem, not false
    ncdev_mem_chunk_to_mem_handle(nd, mc, &arg.mem_handle)                   // :2069
    return copy_to_user(param, &arg, sizeof(arg))                           // :2073
```

> **QUIRK —** V1 hard-codes `on_host_memory=true, dram_channel=0, dram_region=0` (`:2006-2007`): a V1 caller can only ever get a *host* NQ ring on a NeuronCore. Only V2 and realloc honour the request's host/device and channel/region fields, and only the realloc handler can pass `force_alloc_mem=true` — the engine branch that frees the existing ring and allocates a fresh one ([notification-queues §5](notification-queues.md), `nnq_init :98-100`). A reimplementer wanting a device-DRAM NQ, or wanting to re-back an NQ in place, must use V2/realloc; V1 cannot express either.

### Function Map — NQ-init trio

| Handler | file:line | Arg struct (`neuron_ioctl.h`) | Gate | Boundary call | Returns | Confidence |
|---|---|---|---|---|---|---|
| `ncdev_nc_nq_init_deprecated` | `:1996` | `notifications_init_v1` `:345` (24 B) | PID `:3217` | `nnq_init(...,true,0,0,false,...)` `:2006` | `mmap_offset` | HIGH |
| `ncdev_nc_nq_init_libnrt` | `:2014` | `notifications_init_v2` `:353` (48 B) | PID `:3218` | `nnq_init`/`ts_nq_init(...,false,...)` `:2026/:2030` | `mmap_offset` + `mem_handle` | HIGH |
| `ncdev_nc_nq_init_with_realloc_libnrt` | `:2045` | `notifications_init_with_realloc_v2` `:366` (56 B) | PID `:3219` | `nnq_init`/`ts_nq_init(...,arg.force_alloc_mem,...)` `:2057/:2061` | `mmap_offset` + `mem_handle` | HIGH |

> **CORRECTION (NQ-realloc-size) —** the [catalog](ioctl-catalog.md#family-nq--semaphore--event--hw-counter) lists `notifications_init_with_realloc_v2` as 52 bytes and `read_hw_counters` (§5) as 20 bytes; those are the *unpadded field sums*. The true `sizeof` on LP64 is **56** and **24** respectively: the realloc struct is 9 × `__u32` (36) + 4 pad + 2 × `__u64` (16) = 56, because the trailing `__u64 mmap_offset` forces 8-byte alignment after the odd ninth `__u32 force_alloc_mem`. The handler `copy_from_user`s `sizeof(arg)` (the padded value), so the *kernel* reads 56/24 bytes; `libnrt` packs the same C struct, so the two agree. A reimplementer computing the wire size from the field list alone, without the alignment pad, under-reads by 4 bytes.

---

## 2.5 — Arg Structs and Command Numbers

Every command's `_IOC_NR`, encoded size, struct, and field set, grounded in `neuron_ioctl.h`. `NEURON_IOCTL_BASE = 'N'` (`:656`). All eleven are **pointer-form** macros (`_IO*(BASE, nr, struct *)`), so the *encoded* `_IOC_SIZE` is `sizeof(void*) = 8` regardless of the pointed-to struct — the **struct size** column is the C `sizeof` of the pointed-to struct (what the handler actually copies).

| cmd | `_IOC` nr | dir macro | struct (`neuron_ioctl.h`) | struct `sizeof` | handler |
|---|---|---|---|---|---|
| `SEMAPHORE_INCREMENT` | 41 (`:738`) | `_IOR` | `semaphore` `:333` | 12 | `ncdev_semaphore_ioctl` `:1428` |
| `SEMAPHORE_DECREMENT` | 42 (`:739`) | `_IOR` | `semaphore` `:333` | 12 | `ncdev_semaphore_ioctl` `:1428` |
| `SEMAPHORE_READ` | 43 (`:740`) | `_IOWR` | `semaphore` `:333` | 12 | `ncdev_semaphore_ioctl` `:1428` |
| `SEMAPHORE_WRITE` | 44 (`:741`) | `_IOR` | `semaphore` `:333` | 12 | `ncdev_semaphore_ioctl` `:1428` |
| `EVENT_SET` | 45 (`:742`) | `_IOR` | `semaphore` `:333` * | 12 | `ncdev_events_ioctl` `:1457` |
| `EVENT_GET` | 46 (`:743`) | `_IOWR` | `semaphore` `:333` * | 12 | `ncdev_events_ioctl` `:1457` |
| `NOTIFICATIONS_INIT_V1` | 51 (`:747`) | `_IOR` | `notifications_init_v1` `:345` | 24 | `ncdev_nc_nq_init_deprecated` `:1996` |
| `NOTIFICATIONS_DESTROY_V1` | 52 (`:748`) | `_IOR` | `notifications_destroy` `:386` | 8 | inline `return 0` `:3338` |
| `NOTIFICATIONS_INIT_V2` | 53 (`:749`) | `_IOR` | `notifications_init_v2` `:353` | 48 | `ncdev_nc_nq_init_libnrt` `:2014` |
| `NOTIFICATIONS_INIT_WITH_REALLOC_V2` | 54 (`:750`) | `_IOR` | `notifications_init_with_realloc_v2` `:366` | 56 | `ncdev_nc_nq_init_with_realloc_libnrt` `:2045` |
| `NOTIFICATIONS_QUEUE_INFO` | 58 (`:752`) | `_IOR` | `notifications_queue_info` `:396` | 12 | inline `return -1` `:3340` |
| `READ_HW_COUNTERS` | 61 (`:755`) | `_IOR` | `read_hw_counters` `:405` | 24 | `ncdev_read_hw_counters` `:1694` |

The `[in]`/`[out]` field sets:

| struct | `[in]` fields | `[out]` fields |
|---|---|---|
| `semaphore` (`:333`) | `nc_id`, `semaphore_index`, `value` (`value` is `[in]` for WRITE/INC/DEC) | `value` (READ only) |
| `event` (`:339`) | `nc_id`, `event_index`, `value` (`value` is `[in]` for SET) | `value` (GET only) |
| `notifications_init_v1` (`:345`) | `nc_id`, `nq_type`, `engine_index`, `size` | `mmap_offset` |
| `notifications_init_v2` (`:353`) | `nq_dev_id`, `nq_dev_type`, `nq_type`, `engine_index`, `size`, `on_host_memory`, `dram_channel`, `dram_region` | `mmap_offset`, `mem_handle` |
| `notifications_init_with_realloc_v2` (`:366`) | + `force_alloc_mem` | `mmap_offset`, `mem_handle` |
| `read_hw_counters` (`:405`) | `address` (user `__u64 *`), `count` | `data` (user `__u32 *`) |

> **NOTE —** the `EVENT_SET`/`EVENT_GET` macros (`*` above) are declared against `struct neuron_ioctl_semaphore *` (`:742-743`), but the handler `copy_from_user`s into a `struct neuron_ioctl_event` (`:1462`). The two are layout-identical — three `__u32`, 12 bytes — so the wire form matches and the only effect is a clearer field name (`event_index` vs `semaphore_index`); the `neuron_ioctl_event` comment even mislabels `event_index` as "Semaphore Index" (`:341`). The four `SEMAPHORE_*` macros and the two `EVENT_*` macros all point at the *same* `neuron_ioctl_semaphore` struct.

> **QUIRK —** the `_IOC_DIR` of these macros does **not** match what the handlers actually do with memory. `SEMAPHORE_READ`/`EVENT_GET` are `_IOWR` (they `copy_to_user` a value back), but `INCREMENT`/`DECREMENT`/`WRITE`/`SET`, all three NQ-init commands, and `READ_HW_COUNTERS` are encoded `_IOR` *despite* writing back via explicit `copy_to_user` (the NQ-init `mmap_offset`/`mem_handle`, the counter `data` array). The driver dispatches on the **full `cmd`** value, never on `_IOC_DIR`, so the mismatch is inert — but a reimplementer driving the device must not infer "writes back" from `_IOR`/`_IOWR`; the read-back is in the handler body, not the macro direction.

---

## 3. The Semaphore Multiplexer

### Purpose

Serve all four semaphore commands from one handler, demuxing by exact `cmd ==` to the matching `nc_semaphore_*` engine helper. `READ` copies the resulting value back; `WRITE`/`INCREMENT`/`DECREMENT` are write-only and return the engine's status directly. This is the *only* handler in the family that range-checks `nc_id`.

### Algorithm

```c
// neuron_cdev.c:1428 — one handler, four commands
function ncdev_semaphore_ioctl(nd, cmd, param):
    copy_from_user(&arg, param, sizeof(neuron_ioctl_semaphore))   // :1433  12 bytes
    if ret: return ret                                            // :1435

    ret = ncdev_ncid_valid(arg.nc_id)                             // :1437  THE nc_id gate
    if ret: return ret                                            // :1439  → -E2BIG if nc_id >= nc_per_device

    if cmd == SEMAPHORE_READ:                                     // :1442
        ret = nc_semaphore_read(nd, nc_id, semaphore_index, &arg.value)  // :1443  → neuron_core
        if ret: return ret                                       // :1444
        return copy_to_user(param, &arg, sizeof(arg))            // :1446  value out
    elif cmd == SEMAPHORE_WRITE:                                  // :1447
        return nc_semaphore_write(nd, nc_id, semaphore_index, arg.value)      // :1448
    elif cmd == SEMAPHORE_INCREMENT:                             // :1449
        return nc_semaphore_increment(nd, nc_id, semaphore_index, arg.value) // :1450
    elif cmd == SEMAPHORE_DECREMENT:                             // :1451
        return nc_semaphore_decrement(nd, nc_id, semaphore_index, arg.value) // :1452
    return -1                                                    // :1454  unreachable fallthrough
```

The structure is a guard-clause cascade: copy, validate `nc_id`, then a four-way exact-`cmd` switch where only `READ` takes the extra `copy_to_user`. The trailing `return -1` (`:1454`) is unreachable from `ncdev_ioctl`, which only routes the four `SEMAPHORE_*` cmds here (`:3312-3319`), but it guarantees a defined return if the handler is ever called with an out-of-set `cmd`.

> **GOTCHA —** the ioctl struct declares `semaphore_index` as `__u32` (`:335`), but the engine prototype takes `u16` (`nc_semaphore_read/write/increment/decrement(nd, u8 nc_id, u16 semaphore_index, u32 value)`, `neuron_core.h:22/34/46/58`). The top 16 bits of `semaphore_index` are **silently truncated** at the call. Likewise `nc_id` is `__u32` in the struct but `u8` in the prototype — so the engine sees `nc_id & 0xFF`, after `ncdev_ncid_valid` has already rejected anything `>= nc_per_device` (typically 1–8). The truncation is cosmetic only if no arch exposes `> 65535` semaphores per core; a reimplementer must reproduce the `u16`/`u8` narrowing exactly to match the engine ABI, and must not assume the full 32-bit index reaches hardware.

### Function Map — semaphore

| Command | Handler | file:line | Boundary call (`neuron_core.h`) | Read-back | Confidence |
|---|---|---|---|---|---|
| `SEMAPHORE_READ` | `ncdev_semaphore_ioctl` | `:1428` (`:3312`) | `nc_semaphore_read` `:22` | `copy_to_user` `:1446` | HIGH |
| `SEMAPHORE_WRITE` | `ncdev_semaphore_ioctl` | `:1428` (`:3314`) | `nc_semaphore_write` `:34` | — | HIGH |
| `SEMAPHORE_INCREMENT` | `ncdev_semaphore_ioctl` | `:1428` (`:3316`) | `nc_semaphore_increment` `:46` | — | HIGH |
| `SEMAPHORE_DECREMENT` | `ncdev_semaphore_ioctl` | `:1428` (`:3318`) | `nc_semaphore_decrement` `:58` | — | HIGH |

---

## 4. The Event Multiplexer

### Purpose

The semaphore handler's near-twin: serve `EVENT_GET`/`SET` from one handler, demuxing by exact `cmd ==` to `nc_event_get`/`nc_event_set`. `GET` copies the value back. The divergence from the semaphore handler is exactly one missing line.

### Algorithm

```c
// neuron_cdev.c:1457 — event GET/SET — note the absent nc_id check
function ncdev_events_ioctl(nd, cmd, param):
    copy_from_user(&arg, param, sizeof(struct neuron_ioctl_event))   // :1462  12 bytes
    if ret: return ret                                               // :1464

    // ── NO ncdev_ncid_valid(arg.nc_id) here — present in the semaphore handler at :1437 ──

    if cmd == EVENT_GET:                                             // :1467
        ret = nc_event_get(nd, arg.nc_id, arg.event_index, &arg.value)  // :1468  → neuron_core
        if ret: return ret                                          // :1469
        return copy_to_user(param, &arg, sizeof(arg))               // :1471  value out
    elif cmd == EVENT_SET:                                           // :1472
        return nc_event_set(nd, arg.nc_id, arg.event_index, arg.value)  // :1473
    return -1                                                        // :1475
```

> **GOTCHA —** `ncdev_events_ioctl` **omits** the `ncdev_ncid_valid(arg.nc_id)` call that `ncdev_semaphore_ioctl` runs at `:1437`. The two handlers are otherwise structurally identical — same copy, same exact-`cmd` switch, same read-back-on-get — and they sit a few lines apart, but the events path forwards an unvalidated `nc_id` straight into `nc_event_get`/`set`, where the per-arch DHAL (`nc_get_event_addr_v2`, `v2/neuron_dhal_v2.c:393-397`) multiplies it into a BAR0 MMIO offset (`bar0 + TPB_0_OFFSET + TPB_0_SIZE * nc_id + event_index * NC_EVENT_SIZE`) and `nc_event_set` issues a `writel` to it. `nc_id` is `u8`-narrowed at the prototype (`nc_event_get/set(nd, u8 nc_id, u16 event_index, ...)`, `neuron_core.h:70/82`), so the unchecked value is bounded `0..255` — the scaled offset is bounded but can still exceed the intended per-core window when `nc_per_device` is 1–8. This is **finding S2** on [the attack-surface page](../security/ioctl-attack-surface.md); the one-line fix is to mirror the semaphore handler's `:1437` check. Both commands are also un-attach-gated (§6), so any non-free-access opener can reach the unchecked path. The companion off-by-one in `nc_event_get/set` (`event_index > event_count` should be `>=`, finding S7) compounds it by +1 slot.

### Function Map — event

| Command | Handler | file:line | Boundary call (`neuron_core.h`) | Read-back | Confidence |
|---|---|---|---|---|---|
| `EVENT_GET` | `ncdev_events_ioctl` | `:1457` (`:3320`) | `nc_event_get` `:70` | `copy_to_user` `:1471` | HIGH |
| `EVENT_SET` | `ncdev_events_ioctl` | `:1457` (`:3322`) | `nc_event_set` `:82` | — | HIGH |

---

## 5. READ_HW_COUNTERS

### Purpose

Read an array of hardware register counters through the FW-IO mailbox. The handler heap-allocates two arrays sized by the user-supplied `count`, copies the address array in, forwards to `fw_io_read_counters`, and copies the resulting value array out.

### Algorithm

```c
// neuron_cdev.c:1694 — read `count` HW counters via the FW-IO mailbox
function ncdev_read_hw_counters(nd, param):
    reg_addresses = NULL; data = NULL
    copy_from_user(&arg, param, sizeof(neuron_ioctl_read_hw_counters))   // :1701  24 bytes
    if ret: return ret                                                  // :1703

    reg_addresses = kmalloc(arg.count * sizeof(u64), GFP_KERNEL)        // :1705  no upper bound on count
    if reg_addresses == NULL: return -ENOMEM                           // :1707
    ret = copy_from_user(reg_addresses, arg.address, arg.count * 8)    // :1708  user addr array → kernel
    if ret != 0: goto done                                            // :1710

    data = kmalloc(arg.count * sizeof(u32), GFP_KERNEL)               // :1712  ── see GOTCHA
    if data == NULL: goto done                                        // :1713  goto with ret STILL 0

    ret = fw_io_read_counters(nd->fw_io_ctx, reg_addresses, data, arg.count)  // :1716  → fw-io
    if ret: goto done                                                // :1718
    ret = copy_to_user(arg.data, data, arg.count * 4)                // :1719  value array out
done:
    kfree(reg_addresses)                                             // :1721
    kfree(data)                                                      // :1722
    return ret                                                       // :1723
```

The `done:` label is the uniform cleanup path: both `kmalloc`s are unconditionally `kfree`d (a `NULL` `kfree` is a no-op), and the function returns whatever `ret` holds. `fw_io_read_counters(ctx, addr_in[], val_out[], count)` (`neuron_fw_io.h:416`) walks the address array through the MiscRAM mailbox and fills `val_out`; its own per-read cap and protocol are owned by [fw-io](fw-io.md).

> **GOTCHA —** the second `kmalloc` failure (`data == NULL`, `:1713`) does `goto done` while `ret` is **still 0** from the successful `copy_from_user` at `:1708`. The cleanup `kfree`s both buffers and returns **0 (success)** — without ever calling `fw_io_read_counters` or `copy_to_user`. So an OOM on the `data` allocation silently returns success with the user's `data` array untouched (stale/uninitialized), where every other failure in the function returns a real error code. The fix is `goto done` with `ret = -ENOMEM` set first (mirroring `:1707`). CONFIDENCE HIGH — direct source fact. Separately, `arg.count` has **no upper bound** before either `kmalloc` (`:1705`/`:1712`), so a large `count` drives a large allocation; on LP64 `u32 × sizeof` cannot 64-bit-wrap (max ≈ 32 GB), so it fails cleanly to `-ENOMEM` rather than under-allocating, but a reimplementer should cap `count` defensively.

### Function Map — hw-counters

| Handler | file:line | Arg struct | Gate | Boundary call | Confidence |
|---|---|---|---|---|---|
| `ncdev_read_hw_counters` | `:1694` (`:3342`) | `read_hw_counters` `:405` (24 B) | none (non-free-access) | `fw_io_read_counters` `:1716` ([fw-io](fw-io.md)) | HIGH |

---

## 6. The Two Stubs and the Gate Asymmetry

### The stubs

Two NQ-family commands have no handler function — they are inline returns in `ncdev_ioctl`:

```c
// neuron_cdev.c:3338-3341 — the two NQ-family stubs
} else if (cmd == NEURON_IOCTL_NOTIFICATIONS_DESTROY_V1) {
    return 0;                          // :3339  no-op: NQ teardown is implicit on reset/flush
} else if (cmd == NEURON_IOCTL_NOTIFICATIONS_QUEUE_INFO) {
    return -1;                         // :3341  unsupported stub
```

> **GOTCHA —** `NOTIFICATIONS_QUEUE_INFO` returns a **raw `-1`** (`:3341`), which surfaces to userspace as `errno = EPERM` (`-1` is `-EPERM`), **not** `-ENOSYS` or `-ENOTTY`. A reimplementer probing whether the driver supports `QUEUE_INFO` sees a *permission* error, not an *unimplemented* one — and must not interpret the `EPERM` as an access-control failure. The `struct neuron_ioctl_notifications_queue_info` (`neuron_ioctl.h:396-403`, four `__u8` + two `__u32` out-fields `head`/`phase_bit`) is consumed by no kernel code; the head/phase the struct would report are read by userspace directly from the mmapped HEAD register ([notification-queues §7](notification-queues.md)). `NOTIFICATIONS_DESTROY_V1` (`return 0`) is the no-op destroy already covered in §1.

### The attach-gate asymmetry

Of the eleven live commands, only the **three NQ-init** commands sit in the `npid_is_attached` attach allow-list (`neuron_cdev.c:3217-3219`, verbatim):

```c
... cmd == NEURON_IOCTL_POST_METRIC || cmd == NEURON_IOCTL_NOTIFICATIONS_INIT_V1 ||
    cmd == NEURON_IOCTL_NOTIFICATIONS_INIT_V2 || cmd == NEURON_IOCTL_DRIVER_INFO_SET ||
    cmd == NEURON_IOCTL_NOTIFICATIONS_INIT_WITH_REALLOC_V2) {
    if (!npid_is_attached(nd)) { ...; return -EACCES; }   // :3220-3224
}
```

The four `SEMAPHORE_*`, the two `EVENT_*`, and `READ_HW_COUNTERS` are **absent** from the list, so they run with **no attach check** on any non-free-access fd — any process that opened `/dev/neuronN` (`O_RDONLY`/`O_RDWR`) can drive another app's semaphores/events and read HW counters without being the `DEVICE_INIT` owner. None of the eleven is on the free-access misc lane, so an `O_WRONLY` opener reaches none of them. The privilege model is owned by [ioctl-dispatch §2](ioctl-dispatch.md); the security projection (the ungated `EVENT_*` reaching an unvalidated `nc_id`) is [finding S2](../security/ioctl-attack-surface.md).

> **NOTE —** the gate is the *only* thing distinguishing the NQ-init commands from the rest of the family at the dispatch layer: they mutate device state (allocate rings, program BAR0 registers) and so are reserved to the attached owner, whereas the semaphore/event/counter commands are treated as low-stakes per-core register pokes — a treatment the S2/S7 findings argue is too permissive for `EVENT_*`, which reaches a raw BAR0 `writel`.

---

## Cross-References

- [IOCTL Catalog](ioctl-catalog.md) — the per-command `nr`/direction/arg-struct/`ndl_*`-caller/gate table for the nq/sem/event/counter family; this page is the handler-body deep-dive the catalog's nq rows cross-reference (and the source of the realloc/counter `sizeof` correction)
- [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) — the three-tier gate model and the attach allow-list that gates the three NQ-init commands but not the semaphore/event/counter commands
- [Notification Queue Engine](notification-queues.md) — `nnq_init`/`ts_nq_init`, the ring lifecycle, the `mmap_offset = mc->pa` cookie, the three-register `nnq_set_hwaddr` program, and the halt/destroy sweep that backs `DESTROY_V1`; this page is the ioctl front door to that engine
- [TopSP Notification Path](topsp.md) — the `ts_nq_init` TopSP branch that V2/realloc take when `nq_dev_type == NQ_DEVICE_TYPE_TOPSP`, and the `ts_nq_mc` handle store it populates
- [Memory IOCTL Handlers](ioctl-mem.md) — `ncdev_mem_chunk_to_mem_handle` and the 2-level handle table that mints the V2/realloc `mem_handle`, shared with the mem family's `MEM_ALLOC` path
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security projection: the unvalidated `EVENT_*` `nc_id` (S2), the `nc_event_get/set` off-by-one (S7), and the ungated reachability of the semaphore/event/counter commands
