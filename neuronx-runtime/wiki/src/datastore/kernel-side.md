# Kernel Side (Per-Process Slabs)

> *All `file:line` citations on this page are against the GPL-2.0 kernel source of `aws-neuronx-dkms 2.27.4.0` — `neuron_ds.c` (245 lines) and `neuron_ds.h` (119 lines), plus the cross-cell callers `neuron_pci.c`, `neuron_cdev.c`, `neuron_metrics.c`, `neuron_sysfs_metrics.c` in the same tree (`usr/src/aws-neuronx-2.27.4.0/`). The kernel↔userspace layout contract is the shared header `share/neuron_driver_shared.h`. Other driver versions will differ. · Part XIV — The Neuron DataStore (NDS) · [back to index](../index.md)*

## Abstract

The kernel half of the **Neuron DataStore (NDS)** owns the *backing store*, not the *contents*. Picture it as a per-device pool of pre-allocated, `mmap`-able shared-memory slabs: at device probe the driver carves out a fixed array of sixteen 256 KiB host-DRAM chunks (`NEURON_DATASTORE_SIZE = 262144`, `neuron_ds.h:17`; `NEURON_MAX_DATASTORE_ENTRIES_PER_DEVICE = NEURON_MAX_PROCESS_PER_DEVICE = 16`, `neuron_ds.h:18`), one slab per process that can be live on the device at once, and never grows or shrinks the pool thereafter. A process claims a slab through an ioctl, the driver hands back an `mmap` offset, and userspace maps the same physical pages and writes counters into them. The kernel's only writes are `memset(0)`; the kernel's only reads are counter words pulled back during metrics and sysfs aggregation. Everything *between* — the `"nds\0"` magic, the version word, the 67 200-byte region map, the per-record FNV hashes — is written and validated by userspace (see [The NDS Wire Format](wire-format.md)).

The closest familiar frame is a slab allocator fused with a `/proc`-style telemetry surface. The 16-slot array is the slab table; each slot is `{ pid, clear_tick, mem_chunk* }` (`neuron_ds.h:22-26`); a single per-device mutex (`nds->lock`, `neuron_ds.h:29`) serializes the whole table — there is no per-slot lock and, crucially, **no refcount**. Assignment is single-owner-by-PID: a slot is occupied iff `entry->pid != 0`, claimed by stamping the calling process's TGID, and released only by that exact TGID. Reuse is LRU by a monotonic "clear tick" stamped when a slot is freed-for-reuse: never-used slots win first, then the oldest-freed slot. The backing 256 KiB chunk lives for the lifetime of the *device* (`MC_LIFESPAN_DEVICE`, `neuron_ds.c:23`); releasing a slot zeroes its contents but keeps the chunk, so a re-acquire never re-allocates.

This page maps four kernel paths a reimplementer must reproduce: (1) slab-pool allocation once per device, (2) per-process slab assignment on `NEURON_IOCTL_ACQUIRE_NEURON_DS`, (3) the `mmap`-offset publish that hands the slab to userspace, and (4) the lock-free-to-userspace read/walk path that metrics and sysfs use to fold a dying or live process's counters into device-level telemetry. It does **not** re-derive the byte layout of the slab — that is owned by [The NDS Wire Format](wire-format.md) — beyond the 3-band counter dispatch the kernel itself implements in `get_neuroncore_counter_value` (`neuron_ds.c:207`).

For reimplementation, the kernel-side contract is:

- **The slab table** — a fixed `[16]` array of `{ pid_t pid; u64 clear_tick; struct mem_chunk *mc; }` per `neuron_device`, all 16 backing chunks allocated up front at probe (`4 MiB` host DRAM/device), guarded by one mutex, no refcount.
- **The assignment policy** — occupied ⇔ `pid != 0`; acquire stamps `task_tgid_nr(current)` (own slab) or attaches read-only to an existing PID's slab (monitor); release fires only for the owning TGID and immediately zeroes + LRU-stamps the slot.
- **The LRU reuse rule** — `neuron_ds_find_empty_slot` prefers a never-used slot (`clear_tick == 0`), else the smallest `clear_tick`, over the *unused* slots only; `ds_entry_clear_counter` is a module-global atomic tick source.
- **The publish/read split** — acquire returns `{ nmmap_offset(mc), size = 262144 }`; userspace owns every byte written; the kernel reads counters back only under `nds->lock` during aggregation, via the 3-section `get_neuroncore_counter_value` dispatch that mirrors the wire format's NC addressing.

| | |
|---|---|
| **Owning TU** | `neuron_ds.c` / `neuron_ds.h` (GPL-2.0, dkms 2.27.4.0) |
| **Per-device state** | `struct neuron_datastore` embedded in `neuron_device` as field `datastore` |
| **Slab size** | `NEURON_DATASTORE_SIZE = 256*1024 = 262144` (`neuron_ds.h:17`) |
| **Slab count** | `16` = `NEURON_MAX_PROCESS_PER_DEVICE` (`neuron_ds.h:18`; shared `:154`) |
| **Reserved DRAM/device** | `16 × 256 KiB = 4 MiB`, allocated once at probe, never resized |
| **Init / publish entry** | `neuron_ds_init` (`:14`) · `neuron_ds_acquire_pid` (`:126`) |
| **Slot occupancy predicate** | `entry->pid != 0` (`neuron_ds_entry_used`, `:45`) — no refcount |
| **Reuse policy** | LRU by `clear_tick`; `neuron_ds_find_empty_slot` (`:70`) |
| **Counter read** | `get_neuroncore_counter_value` 3-band dispatch (`:207`) |
| **ioctls** | `ACQUIRE_NEURON_DS` #71, `RELEASE_NEURON_DS` #72 (`neuron_ioctl.h:758-759`) |

---

## 1. The Slab Table

### Purpose

The slab table is the per-device control structure: a fixed array of 16 slots, each describing one process's claim on one pre-allocated 256 KiB host-DRAM chunk, plus the mutex that serializes the array and a back-pointer to the owning device (needed so the read path can route a dying process's counters into *that device's* metrics). It is the only mutable kernel state in the NDS cell — every function below either scans it, stamps a slot, or zeroes a slot.

### Layout

`struct neuron_datastore_entry` (`neuron_ds.h:22-26`) — one per process slot:

| Field | Type | Role | file:line |
|---|---|---|---|
| `pid` | `pid_t` (4 B) | owner TGID; `0` ⇔ free. Sole occupancy predicate. | `neuron_ds.h:23` |
| `clear_tick` | `u64` | LRU timestamp stamped on free-for-reuse; `0` ⇔ never used | `neuron_ds.h:24` |
| `mc` | `struct mem_chunk *` | backing 256 KiB host chunk; `mc->va` is the slab VA, `mc->size = 262144` | `neuron_ds.h:25` |

`struct neuron_datastore` (`neuron_ds.h:28-32`) — one per `neuron_device`:

| Field | Type | Role | file:line |
|---|---|---|---|
| `lock` | `struct mutex` | serializes the entire 16-slot array; no per-slot locking | `neuron_ds.h:29` |
| `parent` | `struct neuron_device *` | owning device; read path routes counters to `parent`'s metrics/sysfs | `neuron_ds.h:30` |
| `entries[16]` | `neuron_datastore_entry[]` | the slab table, `NEURON_MAX_DATASTORE_ENTRIES_PER_DEVICE` slots | `neuron_ds.h:31` |

> **NOTE —** the in-struct byte offsets of `lock`/`parent`/`entries` depend on `sizeof(struct mutex)` (kernel-config dependent), so only the *entry* layout is pinned: `pid` at `+0`, `clear_tick` at `+8` (the `pid_t` is padded to 8 before the `u64`), `mc` at `+16`, `sizeof == 24`. (HIGH on the 24-byte entry; MEDIUM on the container offsets.)

> **QUIRK —** the slot count is `16` because the shared header fixes `NEURON_MAX_PROCESS_PER_DEVICE = 16` — its own comment calls this "2 per core (arbitrary but needs to small number for fast lookup)" (`share/neuron_driver_shared.h:154`). The linear scans (`neuron_ds_find`, `neuron_ds_find_empty_slot`) are O(16) by design; a reimplementation that grows this to hundreds of slots should reconsider the linear-scan-under-one-mutex structure, which is only cheap because 16 is small.

> **CORRECTION (K-DS R1) —** the header doc comments describe acquire/release as "increases ref count" / "decreases ref count, deallocates if 0" (`neuron_ds.h:56,68`) and `neuron_ds_clear` as clearing "regardless of refcount" (`neuron_ds.h:85`). **The code has no refcount field.** `struct neuron_datastore_entry` is `{ pid, clear_tick, mc }` (`neuron_ds.h:22-26`) — no counter. Acquire merely stamps `entry->pid` (`neuron_ds.c:101`); release fires only for the owning TGID and immediately zeroes the slab (`neuron_ds.c:158-163`). The semantics are **single-owner-by-PID**, not refcounted; a reimplementation that builds a refcount here diverges from the binary. (CONFIDENCE HIGH — no refcount in any kernel `.c`.)

---

## 2. Slab Allocation (once per device)

### Purpose

`neuron_ds_init` builds the pool. It runs once per device, at PCI probe, and reserves all 4 MiB of NDS host DRAM up front so that a later `acquire` (which runs on the ioctl hot path, under the mutex) never has to allocate — assignment is pure slot selection plus a `pid` stamp.

### Entry Point

```text
neuron_pci.c:442  neuron_ds_init(&nd->datastore, nd)        ── device probe
  └─ mc_alloc_align(... NEURON_DATASTORE_SIZE ...) ×16       ── K-MEMPOOL boundary
       └─ memset(mc->va, 0, 262144) ×16                      ── kernel writes ONLY zeros
  (probe-failure rollback) neuron_pci.c:477  neuron_ds_destroy(&nd->datastore)
  (normal teardown)        neuron_pci.c:194  neuron_ds_destroy(&nd->datastore)
```

### Algorithm

```c
// neuron_ds_init(nds, parent)                              // neuron_ds.c:14
int neuron_ds_init(neuron_datastore *nds, neuron_device *parent):
    nds->parent = parent;                                   // :18  back-pointer for read path
    memset(nds->entries, 0, 16 * sizeof(neuron_datastore_entry)); // :19  all slots free, clear_tick=0
    mutex_init(&nds->lock);                                 // :21

    for (idx = 0; idx < 16; idx++):                         // :22  NEURON_MAX_DATASTORE_ENTRIES_PER_DEVICE
        ret = mc_alloc_align(parent, MC_LIFESPAN_DEVICE,    // :23  lives until device detach
                             NEURON_DATASTORE_SIZE, 0,      //      262144 bytes
                             MEM_LOC_HOST, 0, 0, 0,         // :24  host DRAM
                             NEURON_MEMALLOC_TYPE_NCDEV_HOST,
                             &nds->entries[idx].mc);
        if (ret):                                           // :26
            pr_err("nds allocation failure for nd[%d]", parent->device_index); // :27
            return ret;                                     // :28  partial-init left to caller (R3)
        memset(nds->entries[idx].mc->va, 0, NEURON_DATASTORE_SIZE); // :30  zero the slab — never the magic
    return 0;                                               // :32
```

### Function Map

| Function | Role | Confidence | file:line |
|---|---|---|---|
| `neuron_ds_init` | allocate + zero all 16 slabs, init mutex, set parent | HIGH | `neuron_ds.c:14` |
| `mc_alloc_align` | per-slab 256 KiB host chunk (K-MEMPOOL, boundary) | HIGH | `neuron_ds.c:23` |
| `neuron_ds_destroy` | free all 16 chunks; probe-rollback + teardown caller | HIGH | `neuron_ds.c:189` |

### Considerations

The allocation tags are fixed: `MC_LIFESPAN_DEVICE` (the chunk is freed only when the device detaches, never on slot release), `MEM_LOC_HOST` (host DRAM, so the pages are `mmap`-able into a userspace process), `NEURON_MEMALLOC_TYPE_NCDEV_HOST` (`neuron_ds.c:23-24`). Because the chunk outlives every slot occupancy, `acquire` reuses the *same* `mc` a prior owner used — only the *contents* are zeroed between owners (§4).

> **GOTCHA (K-DS R3) —** `neuron_ds_init` is **not** self-cleaning on partial failure. If `mc_alloc_align` fails at slot `idx`, the function returns the error with slots `0..idx-1` still holding allocated chunks (`neuron_ds.c:28`). Cleanup is the caller's job: `neuron_pci.c:477` invokes `neuron_ds_destroy` on the probe-failure path, and `neuron_ds_destroy` (`:189`) frees every slot whose `mc != NULL`, so the already-allocated chunks are reclaimed. A reimplementation that treats `neuron_ds_init` as transactional (self-rolling-back) will double-free; one that omits the caller-side `destroy` on probe failure will leak. (CONFIDENCE HIGH.)

> **NOTE —** the kernel writes **only zeros** here (`memset`, `:30`). It never stamps the `"nds\0"` signature or the version word — those live at slab `+0` and are written by userspace's `nds_commit_header` after `mmap`. A freshly initialized slab reads all-zero at `+0`; userspace's reader retry (`nds_check_header`, `usleep(500)`) exists precisely to tolerate the window before the producer commits. (K-DS R2, CONFIDENCE HIGH — no signature store in any kernel `.c`. See [The NDS Wire Format](wire-format.md) §2.)

---

## 3. Per-Process Slab Assignment

### Purpose

`neuron_ds_acquire_pid` is the public claim path. It serves two roles, distinguished by the `pid` argument: `pid == 0` means "the calling inference app wants *its own* slab" (create-or-find for `task_tgid_nr(current)`); `pid != 0` means "a monitoring tool wants to *attach* to another process's existing slab read-only" (find-only). It runs under `nds->lock` and never allocates — the pool was filled at probe (§2), so assignment is slot selection plus a `pid` stamp.

### Entry Point

```text
ioctl NEURON_IOCTL_ACQUIRE_NEURON_DS (#71)                  // neuron_ioctl.h:758
  └─ neuron_cdev.c:3344  dispatch → ncdev_acquire_neuron_ds // cdev.c:2076
       └─ neuron_ds_acquire_pid(&nd->datastore, arg.pid, &mc)   // ds.c:126
            pid==0 → neuron_ds_create_and_acquire_pid(task_tgid_nr(current)) // :116
                       └─ neuron_ds_add_pid → neuron_ds_find_empty_slot (LRU) // :90,:70
            pid!=0 → neuron_ds_acquire_existing_pid          // :107  (-ENOENT if absent)
       └─ arg.mmap_offset = nmmap_offset(mc) ; arg.size = mc->size  // cdev.c:2092-2093  (§4)
```

### Algorithm

```c
// neuron_ds_acquire_pid(nds, pid, mc) — public entry        // neuron_ds.c:126
int neuron_ds_acquire_pid(neuron_datastore *nds, pid_t pid, mem_chunk **mc):
    *mc = NULL;                                              // :129  NULL on every failure
    mutex_lock(&nds->lock);                                  // :130  whole-table serialize
    if (pid != 0):
        ret = neuron_ds_acquire_existing_pid(nds, pid, mc);  // :132  monitor attaches (find-only)
    else:
        ret = neuron_ds_create_and_acquire_pid(nds,          // :134  app gets/creates own slab
                                               task_tgid_nr(current), mc);
    mutex_unlock(&nds->lock);                                // :135
    return ret;

// neuron_ds_acquire_existing_pid(nds, pid, mc)              // neuron_ds.c:107
//   monitor path: the target must already own a slot
int acquire_existing(nds, pid, mc):
    entry = neuron_ds_find(nds, pid);                        // :109  linear scan for pid match
    if (entry == NULL): return -ENOENT;                      // :111  no slab for that PID
    *mc = entry->mc;                                         // :112  hand back the same chunk (RO use)
    return 0;

// neuron_ds_create_and_acquire_pid(nds, pid, mc)            // neuron_ds.c:116
//   app path: idempotent — re-acquire returns the existing slot, no new slot
int create_and_acquire(nds, pid, mc):
    entry = neuron_ds_find(nds, pid);                        // :119
    if (entry == NULL): return neuron_ds_add_pid(nds, pid, mc); // :121  first time → claim a slot
    *mc = entry->mc;                                         // :122  already owned → same chunk
    return 0;

// neuron_ds_add_pid(nds, pid, mc) — claim an empty slot     // neuron_ds.c:90
int add_pid(nds, pid, mc):
    *mc = NULL;
    new_index = neuron_ds_find_empty_slot(nds);              // :96  LRU selection (below)
    if (new_index < 0): return -ENOMEM;                      // :98  all 16 slots occupied
    entry = &nds->entries[new_index];
    entry->pid = pid;                                        // :101  STAMP — this is the whole "claim"
    *mc = entry->mc;                                         // :102  reuse pre-allocated chunk (no alloc)
    return 0;

// neuron_ds_find_empty_slot(nds) — LRU over UNUSED slots    // neuron_ds.c:70
int find_empty_slot(nds):
    found_idx = -1; min_clear_tick = ~0ull;
    for (idx = 0; idx < 16; idx++):                          // :76
        entry = &nds->entries[idx];
        if (neuron_ds_entry_used(entry)): continue;          // :78  skip occupied (pid != 0)
        if (entry->clear_tick == 0): return idx;             // :80-81  never-used → take immediately
        if (entry->clear_tick < min_clear_tick):             // :82  else track oldest-freed
            min_clear_tick = entry->clear_tick; found_idx = idx;
    return found_idx;                                        // :87  -1 if no slot free

// neuron_ds_find(nds, pid) — linear pid lookup             // neuron_ds.c:57
neuron_datastore_entry *find(nds, pid):
    for (idx = 0; idx < 16; idx++):
        entry = &nds->entries[idx];
        BUG_ON(used(entry) && entry->mc == NULL);            // :63  invariant: occupied ⇒ has chunk
        if (entry->pid == pid): return entry;                // :64
    return NULL;                                             // :67
```

### Function Map

| Function | Role | Confidence | file:line |
|---|---|---|---|
| `neuron_ds_acquire_pid` | public entry; lock + route by `pid` 0/non-0 | HIGH | `neuron_ds.c:126` |
| `neuron_ds_acquire_existing_pid` | monitor attach (find-only, `-ENOENT`) | HIGH | `neuron_ds.c:107` |
| `neuron_ds_create_and_acquire_pid` | app get-or-create (idempotent) | HIGH | `neuron_ds.c:116` |
| `neuron_ds_add_pid` | claim empty slot; stamp `pid`; reuse `mc` | HIGH | `neuron_ds.c:90` |
| `neuron_ds_find_empty_slot` | LRU slot selection over unused slots | HIGH | `neuron_ds.c:70` |
| `neuron_ds_find` | linear `pid` lookup; occupancy invariant | HIGH | `neuron_ds.c:57` |
| `neuron_ds_entry_used` | `entry->pid != 0` — the occupancy predicate | HIGH | `neuron_ds.c:45` |

### Considerations

The LRU rule is selective: `find_empty_slot` only ever returns an **unused** slot (it `continue`s past any `pid != 0` slot). A never-used slot (`clear_tick == 0`) is taken on sight; otherwise the slot with the smallest `clear_tick` — the one freed longest ago — wins. `clear_tick` is set from a module-global atomic counter (§4), so the ordering is total across the device's whole lifetime.

> **QUIRK —** the `pid == 0` argument is *not* a "system" PID — it is the sentinel that means "use my own TGID." `neuron_ds_acquire_pid` rewrites `pid == 0` to `task_tgid_nr(current)` (`neuron_ds.c:134`), so an inference app always passes `0` and a monitor always passes the *target's* real PID. Because `0` is also the free-slot sentinel (`pid == 0` ⇔ unused), no real slot can ever be stamped with `pid == 0` — the rewrite happens before the stamp. A reimplementation that lets a literal `pid == 0` reach `add_pid` would create a slot indistinguishable from a free one. (CONFIDENCE HIGH.)

> **GOTCHA —** the monitor path (`pid != 0`) is **find-only**: `acquire_existing_pid` returns `-ENOENT` if the target owns no slab (`neuron_ds.c:111`). A monitor never gets a slot of its own and never creates one for a target. This is the sharing model: a target must `acquire(0)` (publish) *before* any monitor can `acquire(target_pid)` (observe). The kernel does not block or queue the monitor; the contract is "`-ENOENT` means not-yet-publishing, retry later." (CONFIDENCE HIGH.)

---

## 4. The mmap Publish

### Purpose

Assignment returns a `mem_chunk*`; the ioctl handler turns that into an `mmap` offset and a size, copies them to userspace, and userspace `mmap`s the offset to obtain the slab VA. This is the entire kernel→userspace handoff: after it, the kernel is passive on the slab until aggregation reads it back (§5) or the owner releases it.

### Entry Point

```text
ncdev_acquire_neuron_ds(nd, param)                          // neuron_cdev.c:2076
  └─ neuron_ds_acquire_pid(&nd->datastore, arg.pid, &mc)    // cdev.c:2088 → ds.c:126
  └─ arg.mmap_offset = nmmap_offset(mc)                      // cdev.c:2092  K-MMAP boundary
  └─ arg.size       = mc->size   (= 262144)                 // cdev.c:2093
  └─ copy_to_user(param, &arg)
       userspace: mmap(mmap_offset) → slab VA → nds_commit_header writes "nds\0"+version
```

### Algorithm

```c
// ncdev_acquire_neuron_ds(nd, param) — ioctl #71 handler   // neuron_cdev.c:2076 (K-CDEV boundary)
long ncdev_acquire_neuron_ds(neuron_device *nd, void *param):
    copy_from_user(&arg, param);                             // {pid, mmap_offset, size}
    ret = neuron_ds_acquire_pid(&nd->datastore, arg.pid, &mc); // cdev.c:2088 → §3
    if (ret): return ret;                                   // -ENOENT / -ENOMEM propagate
    arg.mmap_offset = nmmap_offset(mc);                     // cdev.c:2092  remap_pfn_range token
    arg.size        = mc->size;                             // cdev.c:2093  = NEURON_DATASTORE_SIZE
    copy_to_user(param, &arg);
    return 0;
    // userspace then mmap()s mmap_offset and owns every byte it writes
```

The release counterpart simply routes back into the slab table:

```c
// ncdev_release_neuron_ds(nd, param) — ioctl #72 handler   // neuron_cdev.c:2098
long ncdev_release_neuron_ds(neuron_device *nd, void *param):
    copy_from_user(&arg, param);
    neuron_ds_release_pid(&nd->datastore, arg.pid);         // cdev.c:2105 → §5
    return 0;

// neuron_ds_release_pid(nds, pid)                          // neuron_ds.c:166
void release_pid(nds, pid):
    mutex_lock(&nds->lock);                                 // :169
    if (pid == 0): pid = task_tgid_nr(current);             // :170-171  sentinel → own TGID
    entry = neuron_ds_find(nds, pid);                       // :172
    if (entry): neuron_ds_release_entry(nds, entry);        // :173-174  owner-guarded (§5)
    mutex_unlock(&nds->lock);                               // :175

// neuron_ds_clear_entry(entry) — free-for-reuse, keep chunk // neuron_ds.c:148
void clear_entry(entry):
    entry->pid = 0;                                         // :150  slot now free
    entry->clear_tick = __sync_fetch_and_add(&ds_entry_clear_counter, 1); // :151  LRU stamp
    memset(entry->mc->va, 0, NEURON_DATASTORE_SIZE);        // :152  zero contents, keep mc
```

### Function Map

| Function | Role | Confidence | file:line |
|---|---|---|---|
| `ncdev_acquire_neuron_ds` | ioctl #71: acquire + return `{offset, size}` | HIGH | `neuron_cdev.c:2076` |
| `nmmap_offset` | `mem_chunk` → `mmap` offset (K-MMAP, boundary) | HIGH | `neuron_cdev.c:2092` |
| `ncdev_release_neuron_ds` | ioctl #72: release by PID | HIGH | `neuron_cdev.c:2098` |
| `neuron_ds_release_pid` | lock + find + owner-guarded release | HIGH | `neuron_ds.c:166` |
| `neuron_ds_clear_entry` | zero contents, bump LRU, keep chunk | HIGH | `neuron_ds.c:148` |

### Considerations

`mc->size` is `NEURON_DATASTORE_SIZE = 262144`, so the published `size` is always the full slab (`cdev.c:2093`). Userspace gates on `size > 0x1067f` before laying out its 67 200-byte region map, so the kernel's 256 KiB grant is comfortably oversized — the slab carries ~195 KiB of headroom the current layout never touches (see [The NDS Wire Format](wire-format.md)).

> **QUIRK —** releasing a slab does **not** free DRAM. `neuron_ds_clear_entry` `memset`s the slab contents and bumps the LRU tick but keeps `entry->mc` intact (`neuron_ds.c:148-152`) — the chunk was allocated `MC_LIFESPAN_DEVICE` and only `neuron_ds_destroy` (`:189`, at device teardown) ever calls `mc_free`. So a re-acquire reuses a *zeroed* prior slab with no allocation on the ioctl path. The two reclamation paths are distinct: `clear_entry` (free-for-reuse, keep DRAM) vs `free_entry` (`:139`, `mc_free` + zero the *entry*, releasing DRAM at teardown). Conflating them — freeing DRAM on release — turns every acquire into an allocation and breaks the lock-held no-alloc invariant. (CONFIDENCE HIGH.)

> **NOTE —** `ds_entry_clear_counter` is a module-global `static u64`, initialized to `1` (`neuron_ds.c:146`) and incremented atomically via `__sync_fetch_and_add` (`:151`). It is shared across *all* devices' datastores, not per-device — the LRU ordering is monotone module-wide. Because it starts at `1`, a freed slot's `clear_tick` is always `≥ 1`, never colliding with the `clear_tick == 0` "never-used" sentinel. (CONFIDENCE HIGH.)

---

## 5. The Kernel Read / Walk Path

### Purpose

The kernel reads the slab back in exactly two situations, both folding a process's NDS counters into device-level telemetry: **on release** (a dying process's final counters are saved before its slab is zeroed) and **on the periodic aggregation tick** (every in-use slab is summed into the current metrics buffer). Both run under `nds->lock`; both read counters through the same 3-band `get_neuroncore_counter_value` dispatch. The kernel never parses the opaque process-info/model region of the slab — only the counter bands.

### Entry Point

```text
RELEASE (per-process, on explicit ioctl #72 or process detach):
  neuron_ds_release_pid → neuron_ds_release_entry          // ds.c:166 → :155
    OWNER GUARD: entry->pid == task_tgid_nr(current)        // :158-160
    └─ nmetric_partial_aggregate(parent, entry)             // :161  K-METRICS boundary
    └─ nsysfsmetric_nds_aggregate(parent, entry)            // :162  K-SYSFS boundary
    └─ neuron_ds_clear_entry(entry)                          // :163  then zero + LRU-stamp
  (process detach) neuron_cdev.c:3477 → release_pid(task_tgid_nr(current))
  (last close)     neuron_cdev.c:3513 → neuron_ds_clear (ALL slots, no owner guard)

PERIODIC TICK (device-wide aggregation, K-METRICS):
  neuron_metrics.c:1019  neuron_ds_acquire_lock(&nd->datastore)
    └─ nmetric_full_aggregate                                // metrics.c:400
         for i in 0..15: if neuron_ds_check_entry_in_use(i)  // metrics.c:406-407 → ds.c:50
              nmetric_aggregate_nd_counter_entry(entry)      // metrics.c:376
                   get_neuroncore_counter_value(entry, nc, ds_id) // ds.c:207
  neuron_metrics.c:1025  neuron_ds_release_lock
```

### Algorithm

```c
// PER-PROCESS, on release: neuron_ds_release_entry(nds, entry)  // neuron_ds.c:155
void release_entry(nds, entry):
    if (entry->pid != task_tgid_nr(current)): return;       // :158-160  OWNER GUARD — only owner clears
    nmetric_partial_aggregate(nds->parent, entry);          // :161  save dying proc's counters → metrics
    nsysfsmetric_nds_aggregate(nds->parent, entry);         // :162  → sysfs counters
    neuron_ds_clear_entry(entry);                           // :163  zero slab, bump LRU (§4)

// DEVICE-WIDE, on tick: held under nds->lock (metrics.c:1019..1025)
// nmetric_full_aggregate(nd, curr, ...)                    // neuron_metrics.c:400
void full_aggregate(nd, curr_metrics, ...):
    for (i = 0; i < 16; i++):                               // :406  every slot
        if (neuron_ds_check_entry_in_use(&nd->datastore, i)): // :407  pid != 0 (lock asserted)
            aggregate_entry(nd, &nd->datastore.entries[i], curr_metrics, ...); // :408 → :376

// neuron_ds_check_entry_in_use(nds, index)                 // neuron_ds.c:50
bool check_entry_in_use(nds, index):
    BUG_ON(!mutex_is_locked(&nds->lock));                   // :52  caller MUST hold the lock
    BUG_ON(index >= 16);                                    // :53
    return nds->entries[index].pid != 0;                    // :54  occupancy

// COUNTER READ: get_neuroncore_counter_value(entry, nc, ctr)  // neuron_ds.c:207
//   3-band dispatch — mirrors the wire format's NC addressing (wire-format §4)
uint64_t get_nc_counter(entry, nc_index, counter_index):
    base = entry->mc->va;                                   // :210  the slab VA
    if (nc_index < NDS_MAX_NEURONCORE_COUNT):               // :212  cores 0-3
        if (counter_index < NDS_NC_COUNTER_COUNT):          // :213  ctr 0-30 → original band @0x100
            return NDS_NEURONCORE_COUNTERS(base, nc_index)[counter_index];
        else:                                               // :215  ctr ≥ 31 → overflow band @0xda80
            counter_index -= NDS_NC_COUNTER_COUNT;          // :216
            if (counter_index >= NDS_EXT_NC_COUNTER_COUNT): goto invalid_counter; // :217
            return NDS_EXT_NEURONCORE_COUNTERS(base, nc_index)[counter_index]; // :220
    nc_index -= NDS_MAX_NEURONCORE_COUNT;                   // :224  cores 4-15 → m = nc-4
    if (nc_index >= NDS_EXT_MAX_NEURONCORE_COUNT): goto invalid_nc_index;  // :226
    if (counter_index >= NDS_TOTAL_NC_COUNTER_COUNT): goto invalid_counter; // :230  ctr 0-94
    return NDS_EXT_NEURONCORE_NC_DATA(base, nc_index)[counter_index]; // :234  full band @0xe280
invalid_counter:                                            // :236
    WARN_ONCE(1, "nds: invalid NeuronCore counter requested: %d", counter_index); // :237
    return 0;                                               // :238  invalid → 0, not counted
invalid_nc_index:                                           // :240
    WARN_ONCE(1, "nds: invalid NeuronCore index requested: %d", nc_index + 4); // :241
    return 0;                                               // :243
```

### Function Map

| Function | Role | Confidence | file:line |
|---|---|---|---|
| `neuron_ds_release_entry` | owner-guard → aggregate → clear (per-process) | HIGH | `neuron_ds.c:155` |
| `neuron_ds_check_entry_in_use` | lock-asserted occupancy test for the walk | HIGH | `neuron_ds.c:50` |
| `get_neuroncore_counter_value` | 3-band NC counter read from the slab | HIGH | `neuron_ds.c:207` |
| `neuron_ds_acquire_lock` / `_release_lock` | mutex wrappers held across full aggregation | HIGH | `neuron_ds.c:35,40` |
| `neuron_ds_clear` | clear ALL slots unconditionally on last close | HIGH | `neuron_ds.c:194` |
| `nmetric_full_aggregate` | walk all in-use slots under lock (K-METRICS) | HIGH | `neuron_metrics.c:400` |
| `nmetric_partial_aggregate` | save one dying process's counters (K-METRICS) | HIGH | `neuron_metrics.c:420` |
| `nsysfsmetric_nds_aggregate` | fold counters into sysfs tree (K-SYSFS) | HIGH | `neuron_sysfs_metrics.c:1127` |

### Considerations

The 3-band dispatch is the kernel's copy of the slab's asymmetric NC-counter geometry: cores 0-3 store counters 0-30 in the original band and counters 31+ in an overflow band, while cores 4-15 store the full 95-counter set contiguously in a third band (`neuron_ds.c:199-205` documents this verbatim, and the byte offsets are derived in [The NDS Wire Format](wire-format.md) §4). The kernel reads counters via the `NDS_*` accessor macros from the shared header, so the kernel and userspace resolve `(nc_index, counter_index)` to the identical byte address. An invalid index returns `0` with a `WARN_ONCE`, so a bad read is silently dropped from the aggregate rather than poisoning it (`neuron_ds.c:236-243`).

The two aggregation modes differ only in *which buffer* they target and *whether they walk*: `nmetric_partial_aggregate` saves one entry (the dying process) into a "freed" buffer so its counters are not lost when the slab is zeroed (`neuron_metrics.c:418-423`); `nmetric_full_aggregate` walks all 16 slots into the "current" buffer on the periodic tick (`:406-408`). The owner guard in `release_entry` and the `BUG_ON(!mutex_is_locked)` in `check_entry_in_use` together fix the locking discipline: the per-process save runs from inside `release_pid` (which holds the lock), and the full walk holds the lock across the entire `nmetric_full_aggregate` (`metrics.c:1019-1025`).

> **QUIRK —** the read path is **lock-free from userspace's side**. The producer writes counters into the slab with `LOCK`-prefixed RMW (userspace); the kernel reads them with plain 8-byte loads under `nds->lock`. The mutex serializes *kernel readers against each other and against slot churn* (acquire/release/clear), **not** against the userspace producer — there is no kernel/userspace lock. Consistency of a single counter word rests on natural 64-bit load/store atomicity, exactly as for the counter arrays in [The NDS Wire Format](wire-format.md) §3. A reimplementation that tries to lock the userspace producer out during a kernel read has no mechanism to do so and does not need one. (CONFIDENCE HIGH.)

> **GOTCHA —** `neuron_ds_clear` (last close, `neuron_cdev.c:3513`) clears **every** slot with **no owner guard** — it applies `neuron_ds_clear_entry` to all 16 entries via `neuron_ds_for_each_entry` (`neuron_ds.c:194,178-187`), zeroing slabs that belong to *other* PIDs. This is safe only because it fires when the device file's `open_count` hits 0 (the last close of the whole device), at which point no process has a live mapping. Contrast `neuron_ds_release_entry`, which *does* guard by owner. A reimplementation must keep the distinction: per-PID release is owner-guarded; whole-device last-close clear is not. (CONFIDENCE HIGH.)

---

## 6. Lifecycle Summary

The four kernel transitions, in the order a single slot experiences them:

| Phase | Trigger | Function | DRAM effect | Slot effect | file:line |
|---|---|---|---|---|---|
| **Allocate** | device probe | `neuron_ds_init` | alloc 16 × 256 KiB (4 MiB) | all free, `clear_tick=0` | `neuron_ds.c:14` |
| **Assign** | ioctl #71, `pid==0` | `neuron_ds_add_pid` | none (reuse chunk) | stamp `pid`, reuse `mc` | `neuron_ds.c:90` |
| **Publish** | ioctl #71 return | `nmmap_offset` | none | — (userspace maps + writes) | `neuron_cdev.c:2092` |
| **Read** | tick / release | `get_neuroncore_counter_value` | none | counters → metrics/sysfs | `neuron_ds.c:207` |
| **Release** | ioctl #72 / detach | `neuron_ds_release_entry` | none (keep chunk) | owner-guard, zero contents, LRU-stamp | `neuron_ds.c:155` |
| **Clear-all** | last close (`open_count==0`) | `neuron_ds_clear` | none (keep chunks) | all slots zeroed, no owner guard | `neuron_ds.c:194` |
| **Destroy** | device teardown / probe-fail | `neuron_ds_destroy` | `mc_free` 16 chunks (−4 MiB) | all entries zeroed | `neuron_ds.c:189` |

The asymmetry worth internalizing: DRAM is allocated exactly once (probe) and freed exactly once (teardown); everything in between — assign, publish, read, release, clear-all — touches only `pid`, `clear_tick`, and slab *contents*, never the chunk itself. That is what makes acquire allocation-free under the mutex.

---

## Cross-References

**NDS sub-pages (Part XIV)**
- [Overview: the Shared-Memory Counter Plane](overview.md) — the kernel/userspace split and the torn-read safety model this page's backing store sits under
- [The NDS Wire Format](wire-format.md) — the byte-exact 67 200-B region map, the NC-counter addressing math that `get_neuroncore_counter_value`'s 3 bands resolve into, and the `"nds\0"`+version header userspace stamps
- [Userspace Side (libnds.a)](userspace-libnds.md) — the producer/reader accessors that write and snapshot the slab the kernel hands out

**Kernel driver (Part III)**
- [Kernel Datastore](../kernel/datastore.md) — the broader `neuron_ds.c` cell map this page deep-dives
- [Sysfs Metrics Tree](../kernel/sysfs.md) — `nsysfsmetric_nds_aggregate`, the sysfs surface fed by the slab counter read path (§5)
