# Char Device, fops and mmap

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The char-device surface is read directly from `neuron_cdev.c` (4043 lines) and `neuron_cdev.h` (69 lines); the `.mmap` delegate from `neuron_mmap.c` (512 lines). The source is read, not reverse-engineered — every `file_operations` slot, struct field, global, and constant is transcribed from the shipped `.c`/`.h`. Struct byte offsets past `open_count` depend on `struct cdev`/`struct mutex` sizes (kernel-version specific), so the layout table gives declaration order with a Confidence column, never a hard `+N`. Other driver versions renumber lines. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`neuron_cdev.c` is the `/dev/neuronN` character-device front end of the Neuron kernel driver: it defines the one `file_operations` table every accelerator node is wired to, the per-fd state that backs `private_data`, the dynamic-major chrdev region that covers all 64 possible nodes, the per-node and per-platform sysfs schema, and the `.mmap` handler that turns a `vm_pgoff` cookie back into a device or host physical mapping. The driver exposes a deliberately **minimal** fops — `open`, `flush`, `release`, `unlocked_ioctl`, `mmap` (`ncdev_fops`, `:3540-3547`) — with **no** `.read`, `.write`, `.poll`, `.llseek`, `.compat_ioctl`, or `.fasync`. Userspace (`libnrt.so`) drives the device entirely through the `ioctl` and `mmap` slots; everything a user would normally `read()`/`write()` is an `ioctl` instead, and the LP64-only command set (no `compat_ioctl`) is owned by [ioctl-dispatch](ioctl-dispatch.md), which this page does not re-derive.

The node geometry is rigid and worth fixing in the reader's mind first: exactly one `struct ncdev` slot per minor in the file-static `devnodes[64]` array (`:63`), and **minor == `device_index`** (`ncdev_create_device_node`, `:3697`). `ncdev_module_init` (`:4007`) reserves a *dynamic* major over a 64-minor region (`alloc_chrdev_region(.., 0, NEURON_MAX_DEV_NODES=64, "neuron")`, `:4015`) and creates the `"neuron_device"` sysfs class once at module load; each node — `cdev` + `device_create` + sysfs attribute group + metrics tree — is built during PCI probe (`ncdev_create_device_node` → `ncdev_init_device_node`, `:3641`) and torn down during PCI remove (`ncdev_remove_device_node`, `:3709`). All node-local mutation (`open_count`, attach, the sysfs reset store) is serialized by the per-node `ncdev_lock`.

Two access classes share the one fops through an `O_WRONLY` flag trick. A fd opened **exactly `O_WRONLY`** is a "free-access" / misc fd: `IS_NEURON_DEVICE_FREE_ACCESS(filep)` (`:52`) short-circuits `open` to set `private_data` and return with **no pid attach and no `open_count`** (`:3400-3403`), routes every `ioctl` straight into the ungated `ncdev_misc_ioctl` lane (`:3206-3207`), and makes `flush`/`release` near-no-ops. A normal (`O_RDONLY`/`O_RDWR`) fd instead waits for device reset to complete, attaches the calling pid, and reaches the full privileged dispatch. That split is the same Gate 1 the [ioctl-dispatch](ioctl-dispatch.md) page anchors its security model on; this page owns the *file-lifecycle* half of it. The headline reimplementation subject is the `.mmap` path: an `ioctl` (`MEM_MC_GET_INFO`) hands userspace `mmap_offset = mc->pa` (the chunk's physical address), userspace passes it back as `mmap(2)`'s `offset`, and `nmmap_mem` decodes `vm_pgoff << PAGE_SHIFT` back to a PA and resolves it against the per-device rbtree of `mem_chunk`s — the **pgoff-is-the-physical-address cookie** protocol.

For reimplementation, the contract is:

- **The minimal fops and the absent slots** — `open`/`flush`/`release`/`unlocked_ioctl`/`mmap` only; no `read`/`write`/`poll`/`llseek`/`compat_ioctl`. The ABI is `ioctl`+`mmap`, LP64-only.
- **The 64-minor dynamic chrdev** — `alloc_chrdev_region` over `NEURON_MAX_DEV_NODES = 64`, `"neuron"` region, `"neuron_device"` class, `"neuron%d"` node name, and the invariant **minor == device_index** that every sysfs `show` relies on to index `devnodes[]`.
- **The per-fd state and the `O_WRONLY` split** — `private_data == &devnodes[minor]`, the free-access early-return in `open`, and how `flush`/`release`/`ioctl` all branch on the same macro.
- **The node birth/death ordering** — `cdev_init`/`cdev_add` → `device_create` → `sysfs_create_group` → `nsysfsmetric_register`, with the failure-unwind at each stage, and the `memset(0)` on remove that resets `ndev` to NULL.
- **The mmap pgoff=PA cookie** — how `MEM_MC_GET_INFO` mints `mmap_offset = mc->pa`, how `nmmap_get_mc` reverses it (`pa = vm_pgoff << PAGE_SHIFT`; `mpset_search_mc(pa)`), and the device / host / special-resource / `CAP_SYS_RAWIO`-root mapping branches.

| | |
|---|---|
| **fops** | `ncdev_fops` — `neuron_cdev.c:3540-3547`; `open`/`flush`/`release`/`unlocked_ioctl`/`mmap`; **no** `read`/`write`/`poll`/`llseek`/`compat_ioctl`/`fasync` |
| **Per-fd state** | `filep->private_data == &devnodes[minor]` (`struct ncdev`, `:53-60`) |
| **Node array** | `static struct ncdev devnodes[NEURON_MAX_DEV_NODES]` (`:63`); `NEURON_MAX_DEV_NODES == MAX_NEURON_DEVICE_COUNT == 64` (`:51`) |
| **Chrdev region** | `alloc_chrdev_region(&neuron_dev, 0, 64, "neuron")` (`:4015`); dynamic `major = MAJOR(neuron_dev)` (`:4021`) |
| **Sysfs class** | `class_create("neuron_device")` (`:4024`, 6.4+/RHEL9.2+; two-arg `THIS_MODULE` else, `:4026`) |
| **Node name** | `"neuron%d"` (`:3699`) → `/dev/neuronN`; **minor == `device_index`** (`:3697`) |
| **Free-access macro** | `IS_NEURON_DEVICE_FREE_ACCESS(filep)` `((filep->f_flags & O_WRONLY) == 1)` (`:52`) |
| **mmap entry** | `ncdev_mmap` (`:3522`) → `nmmap_mem(nd, vma)` (`neuron_mmap.c:429`) |
| **mmap cookie** | `pa = vma->vm_pgoff << PAGE_SHIFT`; `mpset_search_mc(&nd->mpset, pa)` (`neuron_mmap.c:254-259`) |
| **Cookie mint** | `MEM_MC_GET_INFO`: `arg.mmap_offset = mc->pa` (`neuron_cdev.c:647/661`); `nmmap_offset(mc) == mc->pa` (`neuron_mmap.c:237`) |
| **Confidence** | HIGH — both files read in full; fops, struct, globals, the chrdev/class constants, and the mmap chain are verbatim. Byte offsets MED (declaration order only) |

---

## 1. The `file_operations` Surface

### Purpose

`ncdev_fops` is the entire kernel-visible behavior of `/dev/neuronN`. Every bound accelerator points the same `cdev` at this one table (`cdev_init(cdev, &ncdev_fops)`, `:3650`/`:3701`). The table is intentionally austere — the driver implements a device API, not a stream, so it registers only the four lifecycle hooks plus the two it actually uses.

```c
// neuron_cdev.c:3540
static struct file_operations ncdev_fops = {
    .owner          = THIS_MODULE,
    .open           = ncdev_open,       // :3390
    .flush          = ncdev_flush,      // :3452  (runs on every close(2) of a reference)
    .release        = ncdev_release,    // :3499  (final fput only)
    .unlocked_ioctl = ncdev_ioctl,      // :3188  (owned by ioctl-dispatch.md)
    .mmap           = ncdev_mmap,       // :3522
};
```

### The fops table

| Slot | Handler | `file:line` | Role | Confidence |
|---|---|---|---|---|
| `.owner` | `THIS_MODULE` | `:3541` | refcounts the module while a fd is open | HIGH |
| `.open` | `ncdev_open` | `:3390` | resolve `devnodes[minor]`, free-access split, wait-for-reset, `npid_attach` | HIGH |
| `.flush` | `ncdev_flush` | `:3452` | per-process teardown on the **last** attach (`attach_cnt == 1`) | HIGH |
| `.release` | `ncdev_release` | `:3499` | `open_count--`; last-close datastore/MC/mmap cleanup | HIGH |
| `.unlocked_ioctl` | `ncdev_ioctl` | `:3188` | the gated `ioctl` dispatcher — **see [ioctl-dispatch](ioctl-dispatch.md)** | HIGH |
| `.mmap` | `ncdev_mmap` | `:3522` | validate `private_data`/`ndev`, log, delegate to `nmmap_mem` | HIGH |
| `.read` | — | absent | no stream-read interface | HIGH |
| `.write` | — | absent | no stream-write interface | HIGH |
| `.poll` | — | absent | no readiness/`select` interface | HIGH |
| `.llseek` | — | absent | non-seekable | HIGH |
| `.compat_ioctl` | — | absent | **no 32-bit ABI**; pointer-form macros are `_IOC_SIZE == 8` | HIGH |
| `.fasync` | — | absent | no async-signal delivery | HIGH |

> **NOTE —** the absence of `.compat_ioctl` is a hard ABI fact, not an omission: a 32-bit process issuing a pointer-bearing command sends an `_IOC_SIZE` of 4, which matches no handler, and there is no thunk to widen it. A reimplementation that wants 32-bit userspace must add both the slot and per-command struct translation. The [ioctl-dispatch](ioctl-dispatch.md) page treats this as the source of the LP64-only contract.

---

## 2. Per-fd State and the Node Array

### `struct ncdev` — the per-node control block

Every `/dev/neuronN` is backed by one `struct ncdev` slot, and a fd's `private_data` points at that slot once `open` succeeds. The struct is small; its semantic field order is HIGH, but the numeric offsets past `open_count` depend on `struct cdev`/`struct mutex` sizing and are MED.

| Field | Type | Meaning | Confidence |
|---|---|---|---|
| `minor` | `int` | minor number; equals `device_index` (`:3671`, `:3697`) | HIGH |
| `open_count` | `int` | times this node is opened by **normal** fds (free-access fds never touch it) | HIGH |
| `cdev` | `struct cdev` | embedded char-device kobject; `cdev_init`/`cdev_add` target | HIGH |
| `ncdev_lock` | `struct mutex` | serializes `open_count`, attach/detach, the sysfs reset store | HIGH |
| `ndev` | `struct neuron_device *` | owning device back-pointer; **NULL when the slot is free** | HIGH |
| `device` | `struct device *` | the `device_create()` handle; sysfs parent kobj | HIGH |

The array itself is file-static and zero-initialized at module load:

```c
// neuron_cdev.c:46-63
static dev_t  neuron_dev;                 // :46  first dev_t of the reserved region
static int    major;                      // :47  MAJOR(neuron_dev)
static struct class *neuron_dev_class;    // :48  "neuron_device"
...
#define NEURON_MAX_DEV_NODES MAX_NEURON_DEVICE_COUNT   // :51  == 64
static struct ncdev devnodes[NEURON_MAX_DEV_NODES];    // :63  one slot per minor
```

> **GOTCHA —** the **minor == device_index** convention is not decorative — it is the indexing key for *every* sysfs `show` and for `open`. `ncdev_open` resolves its node by `dev = &devnodes[iminor(inode)]` (`:3395`); `device_reset_show`, `connected_devices_show`, and `fw_api_version_show` all index `devnodes[MINOR(dev->devt)].ndev` and then dereference `nd->...` **without a NULL check** (`:3552`, `:3586-3590`, `:3618-3620`). That is safe *only* because node lifetime brackets sysfs lifetime: `device_create` runs after `ndev` is set (`:3672` before `:3674`), and `device_destroy` + `memset(devnode, 0)` (`:3719-3721`) run before the slot's `ndev` could be observed NULL by a racing reader. A reimplementer who decouples sysfs registration from `ndev` assignment, or who reuses a minor for a different `device_index`, breaks both the dereference safety and every consumer that maps a minor to a device.

---

## 3. The Free-Access (`O_WRONLY`) Split in the File Lifecycle

The single macro `IS_NEURON_DEVICE_FREE_ACCESS(filep)` (`:52`) bifurcates the *entire* lifecycle. Each of `open`, `flush`, `release`, and `ioctl` tests it and takes a degenerate path for an `O_WRONLY`-only fd. The [ioctl-dispatch](ioctl-dispatch.md) page owns what the misc lane *does*; this page owns what the *file* does.

### Algorithm — `ncdev_open` and the split

```c
// neuron_cdev.c:3390
function ncdev_open(inode, filep):
    dev = &devnodes[iminor(inode)]                       // :3395  minor == device_index
    nd  = dev->ndev
    neuron_log_rec_add(nd, FILE_OPEN, filep)             // :3398  [boundary: log cell]

    // ---- FREE-ACCESS (O_WRONLY) lane: no attach, no open_count ----
    if IS_NEURON_DEVICE_FREE_ACCESS(filep):              // :3400  (f_flags & O_WRONLY) == 1
        filep->private_data = dev                        // :3401
        return 0                                         // :3402  early-out; misc lane only hereafter

    // ---- NORMAL lane ----
    mutex_lock(&dev->ncdev_lock)
    dev->open_count++                                    // :3406  counted BEFORE the wait
    mutex_unlock(&dev->ncdev_lock)

    // busy-wait until reset completes (acknowledged TODO :3410)
    while nd->device_state == NEURON_DEVICE_STATE_RESET: // :3411
        schedule()                                       // :3412
        if SIGTERM or SIGKILL pending:                   // :3413
            lock; dev->open_count--; unlock              // :3414-3416  decrement is paired
            return -EINTR                                // :3418

    if nd->device_state == NEURON_DEVICE_STATE_INVALID:  // :3421
        lock; dev->open_count--; unlock                  // :3422-3424
        return -EINVAL                                   // :3426

    mutex_lock(&dev->ncdev_lock)
    if !npid_attach(nd):                                 // :3430  [boundary: pid cell] — claim a PID slot
        dev->open_count--                                // :3431
        npid_print_usage(nd); unlock                     // :3433-3434
        return -EBUSY                                    // :3435
    mutex_unlock(&dev->ncdev_lock)
    filep->private_data = dev                            // :3438
    return 0                                             // :3439
```

Two facts a reimplementer must preserve. First, `open_count` is incremented **before** the RESET busy-wait (`:3406` vs `:3411`), and every early-exit path re-decrements it (`:3415`, `:3423`, `:3431`) — a process killed mid-wait does not leak a count. Second, the free-access fd never reaches `npid_attach` and never touches `open_count`, so it is invisible to the reset gating and to last-close accounting.

### `flush` and `release` — the two-phase close

`.flush` fires on **every** `close(2)` of a file reference (including each `dup`'d copy), while `.release` fires only on the final `fput`. The driver puts *per-process* teardown in `flush` (keyed on the attach count dropping to its last holder) and *per-node* teardown in `release` (keyed on `open_count` hitting zero).

```c
// neuron_cdev.c:3452
function ncdev_flush(filep, id):
    dev = filep->private_data; nd = dev->ndev
    neuron_log_rec_add(nd, FILE_FLUSH, filep)            // :3461
    if IS_NEURON_DEVICE_FREE_ACCESS(filep):              // :3463
        return ncdev_misc_flush(filep)                   // :3464  unmark ALL this fd's CRWL cores (bitmap=0xff)

    mutex_lock(&dev->ncdev_lock)
    attach_cnt = npid_is_attached(nd)                    // :3469
    if attach_cnt == 1:                                  // :3470  last attached reference of this pid
        nr_wait(nd, tgid, true)                          // :3472  [reset]  finish any in-flight reset
        ndmar_handle_process_exit(nd, tgid)              // :3474  [dma]    drain queues
        msleep(10)                                       // :3475
        ncrwl_release_current_process(nd)                // :3476  [crwl]
        neuron_ds_release_pid(&nd->datastore, tgid)      // :3477  [datastore]
        mpset_free_expired_mc(&nd->mpset, MC_LIFESPAN_CUR_PROCESS)   // :3478  [mempool]
        nmmap_delete_all_nodes(nd)                       // :3479  [mmap]   drop this pid's mmap rbnodes
    else if attach_cnt == 0:
        npid_is_attached_task(nd)                        // :3490  stale-pid probe (future-cleanup TODO)
    npid_detach(nd)                                      // :3492
    mutex_unlock(&dev->ncdev_lock)
    return 0

// neuron_cdev.c:3499
function ncdev_release(inode, filep):
    if IS_NEURON_DEVICE_FREE_ACCESS(filep): return 0     // :3504  free-access has nothing to release
    dev = filep->private_data; nd = dev->ndev
    mutex_lock(&dev->ncdev_lock)
    dev->open_count--                                    // :3511
    if dev->open_count == 0:                             // :3512  last normal close of the node
        neuron_ds_clear(&nd->datastore)                  // :3513  [datastore]
        mpset_free_expired_mc(&nd->mpset, MC_LIFESPAN_ALL_PROCESS)   // :3514  [mempool]
        nmmap_delete_all_nodes(nd)                       // :3515  [mmap]
    mutex_unlock(&dev->ncdev_lock)
    return 0
```

The lifespan tiers freed here (`CUR_PROCESS` in `flush`, `ALL_PROCESS` in `release`) are the reclaim ladder owned by [mempool-handles](mempool-handles.md); the cdev layer is only the trigger.

> **GOTCHA —** the free-access split is security-relevant, and the file-lifecycle half compounds the dispatch half. Because `ncdev_open` early-returns for an `O_WRONLY` fd (`:3400-3403`), such a fd is a fully-functional handle that **never attached a pid and never incremented `open_count`** — yet `ncdev_flush` still routes it to `ncdev_misc_flush` (`:3464`), which `memset`s a bitmap to `0xff` and calls `ncrwl_nc_range_unmark` to clear **all** NeuronCores. An `O_WRONLY` opener can therefore both *mark* cores (via the ungated `CRWL_NC_RANGE_MARK` misc command) and *unmark every core on the device* simply by closing the fd, with no attach and no `open_count` bookkeeping anywhere. The dispatch-side exposure of the same lane is [ioctl-dispatch §2](ioctl-dispatch.md#gate-1--the-free-access-o_wronly-split); the consolidated exploitability (S5/S8) is on [the attack-surface page](../security/ioctl-attack-surface.md). Reimplementers must not assume "writable fd ⇒ stateless info handle."

> **QUIRK —** the macro is `((f_flags & O_WRONLY) == 1)`, **not** `((f_flags & O_ACCMODE) == O_WRONLY)`. Since `O_RDONLY == 0`, `O_WRONLY == 1`, `O_RDWR == 2`, this is true *only* for an exact `O_WRONLY` open; an `O_RDWR` fd takes the full normal path. Behaviorally correct under current `libnrt` usage (it opens one process-global `O_WRONLY` discovery fd), but a foot-gun for anyone reasoning "writable ⇒ free-access." The clean form is the `O_ACCMODE` mask.

---

## 4. The `.mmap` Path — pgoff-is-the-physical-address

This is the page's reimplementation centerpiece. `ncdev_mmap` itself is a thin guard-and-delegate; all the resolution lives in `nmmap_mem` (`neuron_mmap.c:429`). The protocol is a two-trip handshake: an `ioctl` returns a `mem_chunk`'s physical address as an opaque `mmap_offset`, and a later `mmap(2)` uses that offset (shifted to a page-frame number in `vm_pgoff`) as the lookup key that recovers the same `mem_chunk`.

### Step 1 — minting the cookie (`ioctl` side)

The cookie is *the physical address itself*. `nmmap_offset(mc)` is the identity `return mc->pa` (`neuron_mmap.c:237-239`), and the `MEM_MC_GET_INFO` handler publishes it directly:

```c
// neuron_cdev.c:628 — ncdev_mem_get_mc_mmap_info (size-overloaded v1/v2; see ioctl-dispatch §3)
function mem_get_mc_mmap_info(nd, cmd, param):
    copy_from_user(&arg, param, ...)                     // arg.pa = the chunk's PA, from a prior MEM_ALLOC
    mc = nmmap_get_mc_from_pa(nd, arg.pa)                // :642/656  PA -> mem_chunk via the rbtree
    if mc == NULL: return -ENOMEM
    arg.mmap_offset = mc->pa                             // :647/661  COOKIE == physical address
    arg.size        = mc->size
    // v2 also publishes the opaque handle:
    if v2: ncdev_mem_chunk_to_mem_handle(nd, mc, &arg.mem_handle)   // :664  [mempool-handles]
    return copy_to_user(param, &arg, ...)
```

Userspace then calls `mmap(NULL, size, prot, MAP_SHARED, fd, mmap_offset)`. The kernel stores `vm_pgoff = mmap_offset >> PAGE_SHIFT` before `.mmap` runs — so the page-frame number in `vm_pgoff` *is the chunk's PA, divided by the page size*.

### Step 2 — `ncdev_mmap` guard-and-delegate

```c
// neuron_cdev.c:3522
function ncdev_mmap(filep, vma):
    ncd = filep->private_data; if ncd == NULL: return -EINVAL    // :3527-3529
    nd  = ncd->ndev;          if nd  == NULL: return -EINVAL    // :3531-3533
    neuron_log_rec_add(nd, FILE_MMAP, filep)                     // :3535
    return nmmap_mem(nd, vma)                                    // :3537  [boundary: mmap cell]
```

### Step 3 — `nmmap_mem` resolution

`nmmap_mem` reverses `vm_pgoff` to a PA, looks the PA up in the per-device `mem_chunk` rbtree (`mpset.root`, keyed on `pa` — owned by [mempool-handles](mempool-handles.md)), and dispatches to one of four mapping strategies by where the chunk lives. The whole decode is the `<<PAGE_SHIFT` / `mpset_search_mc` pair:

```c
// neuron_mmap.c:245 — nmmap_get_mc: cookie -> mem_chunk
function nmmap_get_mc(nd, vma):
    size   = vma->vm_end - vma->vm_start
    offset = vma->vm_pgoff << PAGE_SHIFT                  // :254  undo the PAGE_SHIFT from mmap()
    pa     = offset                                       // :256  the cookie IS the physical address
    read_lock(&nd->mpset.rblock)
    mc = mpset_search_mc(&nd->mpset, pa)                  // :259  PA-keyed rbtree lookup
    read_unlock(&nd->mpset.rblock)
    if mc == NULL: return NULL                            // :261  -> special-resource / root fallback
    if !IS_ALIGNED(mc->size, PAGE_SIZE): return NULL      // :267
    if !IS_ALIGNED(mc->pa,   PAGE_SIZE): return NULL      // :271
    // exact-size match required, EXCEPT contiguous-scratchpad may span chunks:
    if mc->size != size && mc->alloc_type != CONTIGUOUS_SCRATCHPAD_DEVICE:  // :281
        return NULL                                       //      no partial mmap of a normal chunk
    if scratchpad && mc->pa + size > mc->mp->main_pool_end_addr: return NULL // :286
    return mc

// neuron_mmap.c:429 — nmmap_mem: the four mapping strategies
function nmmap_mem(nd, vma):
    mc = nmmap_get_mc(nd, vma)
    if mc == NULL:                                        // :435  cookie matched no mem_chunk
        return nmap_dm_special(nd, vma)                   // :437  special-resource table, else root-only BAR map

    if mc->mem_location == MEM_LOC_DEVICE:                // :440
        return nmmap_dm_mc(nd, vma, mc)                   //       device HBM -> BAR4 window (io_remap)

    // ---- host DMA-coherent chunk ----
    if mc->pid != current_tgid && mc->lifespan != MC_LIFESPAN_DEVICE:   // :444  cross-process => RO
        clear VM_WRITE                                    // :445-446
    ret = remap_pfn_range(vma, vma->vm_start,
                          PHYS_PFN(mc->pa & ~ndhal.pci_host_base),       // :453  strip host-window tag
                          mc->size, vma->vm_page_prot)
    if ret: return ret
    set VM_DONTEXPAND | VM_DONTDUMP | VM_DONTCOPY         // :458
    nmmap_create_node(nd, vma->vm_start, tgid, size, mc->pa, -1)        // :461  track VA for unmap cleanup
    vma->vm_ops = &nmmap_dm_vm_ops                        // :465  .close -> nmmap_delete_node
    return 0
```

### The four mapping strategies

| Strategy | Trigger | Mechanism | `file:line` |
|---|---|---|---|
| **Host chunk** | `mc->mem_location == MEM_LOC_HOST` | `remap_pfn_range` of `mc->pa & ~pci_host_base` (clean DRAM PA) | `neuron_mmap.c:453` |
| **Device chunk** | `mc->mem_location == MEM_LOC_DEVICE` | `nmmap_dm_mc`: HBM PA → BAR4 offset via `mmap_get_bar4_offset`, then `io_remap_pfn_range` of `offset + bar4_pa` | `:329`, `:320`, `:325` |
| **Special resource** | cookie matched no chunk, but is in the special-resource table | `nmap_dm_special`: register space, `pgprot_noncached`, `io_remap` of `offset + bar0/bar4_pa` | `:368`, `:390-398` |
| **Root arbitrary BAR** | cookie matched no chunk and no special entry | `nmmap_dm_root`: `capable(CAP_SYS_RAWIO)` else `-EPERM`, then raw BAR4 map | `:360-365` |

Two cross-cutting rules apply across the chunk strategies. **Cross-process read-only:** if the mapping process is not the chunk's owner (`mc->pid != tgid`) and the chunk is not device-lifespan, `VM_WRITE` is cleared so a non-owner gets a read-only view (`:336-339` device, `:444-447` host). **Device-PA vs BAR offset:** a device chunk's PA is *not* directly mappable from the CPU — `nmmap_dm_mc` rewrites `vm_pgoff` to the chunk PA (`:342`) and then translates HBM-PA → BAR4 aperture offset via the DHAL (`mmap_get_bar4_offset`, `:320`) before `io_remap_pfn_range`, because the BAR4 layout squashes the inter-channel gaps the raw HBM address space has (comment `:333`).

> **GOTCHA —** the only authorization on the `.mmap` path is `CAP_SYS_RAWIO`, and it guards **only** the root-arbitrary-BAR fallback (`nmmap_dm_root`, `:362`) — the path taken when the cookie matches *neither* a `mem_chunk` *nor* a special-resource entry. A normal chunk mmap (the common case) has **no capability check**; its access boundary is entirely (a) holding an fd to the node, and (b) the PA-keyed rbtree only containing *this* `nd`'s chunks, so a cookie can only resolve to a chunk this device owns. The cross-process write-protect (`:444`) is the only intra-device isolation. This is the sole `capable()` in the whole ioctl/mmap tree; [ioctl-dispatch](ioctl-dispatch.md) notes there is none on the ioctl side at all.

> **QUIRK —** the cookie is the *physical* address, not a handle or an opaque token, so the cookie space and the rbtree key space are identical by construction. That is what makes the decode a bare `<<PAGE_SHIFT` with no lookup table of its own — `mpset_search_mc` *is* the lookup. The price is that `mmap` cannot validate intent beyond "is this PA one of my chunks": there is no per-mapping handle check on this path (the v2 `mem_handle` minted at `:664` is for the ioctl consumer, not for `mmap`). A reimplementer who instead keys mmap on the opaque handle would diverge from the wire protocol `libnrt` expects.

---

## 5. Node Lifecycle — Birth, Death, and the Chrdev Region

### Module init/exit — the 64-minor region and the class

`ncdev_module_init` (`:4007`, called from `neuron_module.c`) reserves the chrdev region and creates the sysfs class exactly once. `ncdev_class_attr_init` (`:3931`) is called *separately* (from `neuron_dhal.c`) after the platform type is latched, because the per-class attribute table depends on it (§6).

```c
// neuron_cdev.c:4007
function ncdev_module_init():
    memset(devnodes, 0, sizeof(devnodes))                // :4011  all slots free (ndev == NULL)
    for i in 0..63: mutex_init(&devnodes[i].ncdev_lock)  // :4012-4013
    ret = alloc_chrdev_region(&neuron_dev, 0,
                              NEURON_MAX_DEV_NODES=64, "neuron")   // :4015  DYNAMIC major, 64 minors
    if ret < 0: return ret
    major = MAJOR(neuron_dev)                            // :4021
    #if kernel >= 6.4 or RHEL >= 9.2:
        neuron_dev_class = class_create("neuron_device") // :4024  one-arg form
    #else:
        neuron_dev_class = class_create(THIS_MODULE, "neuron_device")  // :4026  legacy two-arg
    if IS_ERR(neuron_dev_class): goto fail               // :4028
    return ret
fail:
    ncdev_cleanup(); return ret                          // :4036

// neuron_cdev.c:3992
function ncdev_cleanup():
    for i in 0..63:
        if devnodes[i].ndev != NULL: pr_err("Error! ncdev is not NULL")  // :3996  leak warning
    if neuron_dev_class: class_destroy(neuron_dev_class) // :4001
    unregister_chrdev_region(MKDEV(major, 0), 64)        // :4004
```

### Node birth (PCI probe) and death (PCI remove)

Each node is built during PCI probe by `ncdev_create_device_node` → `ncdev_init_device_node` (`:3641`), which registers the four kernel objects in a strict order and unwinds at each failure. Crucially `devnode->ndev` is set (`:3672`) *before* `sysfs_create_group` (`:3674`), so the sysfs `show` callbacks (which deref `devnodes[minor].ndev`) never see a NULL.

```c
// neuron_cdev.c:3641 — build one node; unwinds on each failure
function ncdev_init_device_node(devnode, dev_name, minor, fops, ndev):
    devno = MKDEV(major, minor)                          // :3649  minor == device_index
    cdev_init(&devnode->cdev, fops); cdev->owner = THIS_MODULE        // :3650-3651
    if cdev_add(&devnode->cdev, devno, 1) < 0: return ret             // :3654  -> /dev/neuronN live
    device = device_create(neuron_dev_class, NULL, devno, NULL, "%s", dev_name)  // :3660
    if IS_ERR(device): { device_destroy; cdev_del; return ret }       // :3663-3668
    devnode->device = device; devnode->minor = minor; devnode->ndev = ndev      // :3670-3672
    if sysfs_create_group(&device->kobj, &attr_group):   // :3674  per-node attrs (§6)
        { sysfs_remove_group; device_destroy; cdev_del; return ret }  // :3677-3680
    if nsysfsmetric_register(ndev, &device->kobj):       // :3683  [boundary: sysfs metrics]
        { device_destroy; cdev_del; return -1 }          // :3686-3688
    return 0

// neuron_cdev.c:3695
function ncdev_create_device_node(ndev):
    minor = ndev->device_index                           // :3697  THE invariant
    snprintf(dev_name, 32, "neuron%d", minor)            // :3699  -> /dev/neuronN
    ncdev_init_device_node(&devnodes[minor], dev_name, minor, &ncdev_fops, ndev)
    ndev->ncdev = &devnodes[minor]                       // :3705  back-link
    return 0

// neuron_cdev.c:3709 — teardown, reverse order
function ncdev_remove_device_node(devnode):
    sysfs_remove_group(&devnode->device->kobj, &attr_group)          // :3714
    nsysfsmetric_destroy(devnode->ndev)                  // :3715
    device_destroy(neuron_dev_class, MKDEV(major, devnode->minor))   // :3719
    cdev_del(&devnode->cdev)                             // :3720
    memset(devnode, 0, sizeof(struct ncdev))             // :3721  ndev -> NULL: slot is free again
```

> **NOTE —** the `sysfs_create_group` failure path at `:3677` calls `sysfs_remove_group` on a group that did not fully create. This is benign under the kernel sysfs API (removing a partially-created group is tolerated) but is the kind of asymmetry a reimplementer should not copy blindly. (LOW concern — flagged for accuracy, not a live defect.)

---

## 6. The Sysfs Schema

The cdev layer owns two tiers of sysfs: a **per-node** attribute group attached to each `/dev/neuronN`'s `device` kobj, and a **per-platform class** attribute set on the `neuron_device` class. The metrics subtree under each node is registered here (`nsysfsmetric_register`, `:3683`) but owned by [sysfs](sysfs.md).

### Per-node attribute group

```c
// neuron_cdev.c:3629 — attr_group attached at :3674
attrs[] = { reset, core_count, connected_devices, fw_api_version, NULL };
```

| Attribute | Perms | Direction | Backing | `file:line` |
|---|---|---|---|---|
| `reset` | `S_IWUSR\|S_IRUSR` | RW | show → `nd->device_state`; store → `nr_start_ncs` **iff** `open_count == 0` | `:3549`/`:3555`/`:3569` |
| `core_count` | `S_IRUSR` | RO | `ndhal->...nc_per_device` | `:3571`/`:3578` |
| `connected_devices` | `S_IRUSR` | RO | `fw_io_topology` list (cap `CONNECTED_DEVICES_MAX_LEN = 20`) | `:3581`/`:3613` |
| `fw_api_version` | `S_IRUGO` | RO | `fw_io_api_version_read`; `0xdeadbeef` ⇒ `"busy\n"` (reg unreadable during reset) | `:3615`/`:3627` |

The `reset` store is the only writable per-node attribute and it self-gates: `driver_reset_store` (`:3555`) triggers `nr_start_ncs` only when no app holds the node (`open_count == 0`, `:3561`), under `ncdev_lock`. The `fw_api_version` `0xdeadbeef` sentinel (`:3621`) is the firmware register's documented "not readable mid-reset" value, surfaced to userspace as the literal string `"busy"`.

### Per-platform class attributes

`ncdev_class_attr_init` (`:3931`) selects one of three attribute tables by the latched `enum neuron_platform_type` and registers each via `class_create_file`. All class attrs are `S_IRUGO` (the `NCDEV_CLASS_ATTR` macro hardcodes it, `:3883`).

| Platform | Table | Attributes | `file:line` |
|---|---|---|---|
| `STD` | `ncdev_class_attrs[]` | `hbm_7200_capable`, `current_perf_profile` | `:3886` |
| `ULTRASERVER` | `ncdev_class_attrs_us[]` | `node_id_2/4`, `server_id_2/4`, `ultraserver_mode`, + the two STD | `:3891` |
| `PDS` | `ncdev_class_attrs_pds[]` | `node_id`, `node_cnt`, `reservation_id`, `ultraserver_mode`, + the two STD | `:3901` |

The `hbm_7200_capable` / `current_perf_profile` shows are AND-reductions across **all** devices: they emit `"busy"` if any device's value is still `-1` (unread) and `"-1"` if devices disagree (`:3849`, `:3877`) — a class attribute reflects a device-wide consensus, not a single node.

> **CORRECTION (CDEV-resv) —** the PDS `reservation_id` attribute is wired to `ncdev_class_reservation_id_show` (`:3807`), but that callback forwards to `npe_class_server_id_show_data` — the **server-id** formatter, not a reservation-specific one (`:3818-3821`). So `reservation_id` currently emits the server-id value. This is likely intentional callback reuse or a copy/paste; recorded here for wiki accuracy (LOW). A reimplementer should not assume a distinct reservation-id data source exists behind this attribute in 2.27.4.0.

---

## Function Map

| fop / function | `file:line` | Role | Confidence |
|---|---|---|---|
| `ncdev_fops` | `:3540` | the `file_operations` table; 5 live slots, 6 absent | HIGH |
| `ncdev_open` | `:3390` | free-access split, RESET busy-wait, `npid_attach`, set `private_data` | HIGH |
| `ncdev_flush` | `:3452` | per-process teardown on `attach_cnt == 1`; free-access → unmark-all CRWL | HIGH |
| `ncdev_misc_flush` | `:3442` | free-access flush: `memset(bitmap, 0xff)` → `ncrwl_nc_range_unmark` | HIGH |
| `ncdev_release` | `:3499` | `open_count--`; last-close datastore/MC/mmap cleanup; free-access no-op | HIGH |
| `ncdev_mmap` | `:3522` | guard `private_data`/`ndev`, log, delegate to `nmmap_mem` | HIGH |
| `nmmap_mem` | `neuron_mmap.c:429` | cookie→chunk resolve + 4-way map (host/device/special/root) | HIGH |
| `nmmap_get_mc` | `neuron_mmap.c:245` | `pgoff<<PAGE_SHIFT → pa → mpset_search_mc`; align + size checks | HIGH |
| `nmmap_dm_mc` | `neuron_mmap.c:329` | device HBM-PA → BAR4 offset → `io_remap_pfn_range` | HIGH |
| `nmmap_dm_root` | `neuron_mmap.c:360` | `CAP_SYS_RAWIO`-gated arbitrary BAR4 map | HIGH |
| `nmmap_offset` | `neuron_mmap.c:237` | the cookie mint: identity `return mc->pa` | HIGH |
| `ncdev_mem_get_mc_mmap_info` | `:628` | ioctl that publishes `mmap_offset = mc->pa` (v1/v2 size-overload) | HIGH |
| `ncdev_init_device_node` | `:3641` | core node builder: `cdev`/`device_create`/sysfs/metrics, with unwind | HIGH |
| `ncdev_create_device_node` | `:3695` | `[PUBLIC]` minor = `device_index`; `"neuron%d"`; set `ndev->ncdev` | HIGH |
| `ncdev_remove_device_node` | `:3709` | reverse teardown + `memset(0)` frees the slot | HIGH |
| `ncdev_module_init` | `:4007` | `alloc_chrdev_region(64, "neuron")` + `class_create("neuron_device")` | HIGH |
| `ncdev_cleanup` | `:3992` | leak-warn, `class_destroy`, `unregister_chrdev_region` | HIGH |
| `ncdev_class_attr_init` | `:3931` | register the platform-matched class attribute table | HIGH |
| `device_reset_show` / `driver_reset_store` | `:3549` / `:3555` | per-node `reset` attr; store gated on `open_count == 0` | HIGH |
| `fw_api_version_show` | `:3615` | per-node FW version; `0xdeadbeef` → `"busy"` | HIGH |

---

## Related Components

| Component | Relationship |
|---|---|
| `ncdev_ioctl` / `ncdev_misc_ioctl` (`:3188` / `:3147`) | the `.unlocked_ioctl` slot and its gated dispatch — owned by [ioctl-dispatch](ioctl-dispatch.md) |
| `nmmap_*` (`neuron_mmap.c`) | the `.mmap` delegate: cookie decode, the 4 mapping strategies, VA-rbtree tracking |
| `mpset_search_mc` (`neuron_mempool.c:532`) | the PA-keyed rbtree this page's mmap decode queries — owned by [mempool-handles](mempool-handles.md) |
| `npid_*` (`neuron_pid.c`) | the 16-slot attach table `open`/`flush` populate and drain |
| `nsysfsmetric_*` / `fw_io_*` / `nr_start_ncs` | the per-node metrics tree, FW-version read, and sysfs reset trigger |

## Cross-References

- [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) — the `.unlocked_ioctl` body, the three-gate model, and the same `O_WRONLY` free-access split seen here from the file-lifecycle side
- [Memory Pool and MC Handle Table](mempool-handles.md) — the `mem_chunk` whose `pa` becomes the mmap cookie, and the `mpset.root` rbtree that `nmmap_get_mc` searches
- [Memory IOCTL Handlers](ioctl-mem.md) — `MEM_ALLOC*` that creates the chunk and `MEM_MC_GET_INFO` that mints `mmap_offset = mc->pa` for the mmap handshake
- [Sysfs Metrics Tree](sysfs.md) — the per-node metrics subtree registered by `nsysfsmetric_register` at node birth (`:3683`)
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security framing of the `O_WRONLY` free-access split (S5/S8: ungated CRWL mark/unmark via this lane and `ncdev_misc_flush`) and the lone `CAP_SYS_RAWIO` mmap gate (S4 context)
