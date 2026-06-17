# Peer-to-Peer Export: register_va and the dma-buf Exporter

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. Both peer-export paths are read directly, not reverse-engineered: the legacy symbol API from `neuron_p2p.c` (179 lines) + `neuron_p2p.h` (42 lines), the dma-buf exporter from `neuron_dmabuf.c` (378 lines) + `neuron_dmabuf.h` (27 lines) — all four read in full. Every function signature, the `dma_buf_ops` vtable, the struct field order, and the version guards are transcribed from the shipped `.c`/`.h`. Struct byte offsets past `is_attached` depend on LP64 alignment and are MED (declaration order only); everything else is HIGH. Other driver versions renumber lines. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

A Neuron device-memory buffer that a user process `mmap`'d (the pgoff-is-the-physical-address cookie owned by [cdev-mmap](cdev-mmap.md)) must, for RDMA, expose its **host physical addresses** to a *third-party* PCI device — in practice the AWS EFA NIC behind libfabric, doing peer-direct DMA straight into HBM with no CPU bounce buffer ([transport-efa](../nccom/transport-efa.md)). This cell is the two parallel mechanisms that hand those PAs across the device boundary. Neither is a new allocation: both resolve an existing user VA back to the `mem_chunk` it maps and publish that chunk's physical address to the peer.

The first mechanism — `neuron_p2p.c` — is the **legacy `register_va` path**: a pair of `EXPORT_SYMBOL_GPL` in-kernel functions (`neuron_p2p_register_va` / `neuron_p2p_unregister_va`) that another GPL kernel driver calls directly. Given a `(virtual_address, length)`, it walks every Neuron device's per-PID mmap rbtree, finds the matching `nmmap_node`, stashes a `free_callback` into that node (so the peer is notified when the buffer is torn down), and returns a heap-allocated `struct neuron_p2p_va_info` carrying the single contiguous PA, a page-size shift, and a page count. There is no fd and no dma-buf — it is a symbol-level peer contract that mirrors NVIDIA's `nvidia_p2p_*` peer-memory client model. The second mechanism — `neuron_dmabuf.c` — is the **modern Linux dma-buf exporter**: a user ioctl (`DMABUF_FD`) mints an anonymous `dma_buf` backed by the `ndmabuf_ops` vtable and installs an `O_CLOEXEC` fd; the importer (ibcore/EFA) then drives the standard `attach → map_dma_buf → [DMA] → unmap_dma_buf → detach → release` sequence, where `map_dma_buf` builds an `sg_table` of `PAGE_SIZE`-granular entries each carrying `sg_dma_address = device PA`.

Both paths reach the VA→PA resolver `nmmap_search_va()` (owned by [mempool-handles](mempool-handles.md) / the mmap cell) under the per-device write-lock `nd->mpset.rbmmaplock`, and both reach the device table via `neuron_pci_get_device()`. The entire dma-buf file is gated `#if LINUX_VERSION_CODE >= KERNEL_VERSION(5,15,0)`; below that the public entry is a stub returning `-EPROTONOSUPPORT`. Two correctness concerns straddle the boundary but their trigger code is in-scope: the warn-only node teardown that can free a node mid-DMA (§5, [the attack-surface page's S6](../security/ioctl-attack-surface.md#8-s6--dma-buf-node-teardown-use-after-free-window)), and the legacy `u64`-return error-as-PA smell that is self-mitigated by an alignment guard ([S13](../security/ioctl-attack-surface.md#13-s13--p2p-register_va-error-as-pa-correction-self-mitigated)).

For reimplementation, the contract is:

- **The shared VA→PA model** — both paths take a user VA, write-lock `rbmmaplock`, `nmmap_search_va` the per-PID rbtree on each of up to 64 devices, validate `offset + size <= mmap->size`, and compute `pa = mmap->pa + (va - mmap->va)` for a single *contiguous* span. A reimplementer must reproduce the single-chunk-contiguous assumption — the buffer is one `mem_chunk`, not a scatter list of independent allocations.
- **The `register_va` symbol API** — the two `EXPORT_SYMBOL_GPL` functions, the `neuron_p2p_va_info` heap struct with its `{PA, shift_page_size, entries}` payload, the `free_callback` stash into the node, the huge-page (2 MiB) vs `PAGE_SIZE` decision, and the `device_index` security re-check on unregister.
- **The dma-buf exporter** — `ndmabuf_get_fd` (VA-validate → `dma_buf_export` → `dma_buf_fd`), the five-slot `ndmabuf_ops` vtable, the single-attach CAS guard, the `sg_table` build (one `PAGE_SIZE` entry per page, `sg_dma_address = pa`), the `dmabuf_ref_cnt` bump/drop, and the unusual FD-recycle in `detach`.
- **The version-compat envelope** — the 5.15 gate, the 5.16 `MODULE_IMPORT_NS` gate, the 6.13/RHEL10 string-vs-token `MODULE_IMPORT_NS("DMA_BUF")` churn, and the `-EPROTONOSUPPORT` stub below 5.15.

| | |
|---|---|
| **Legacy entry** | `neuron_p2p_register_va` (`neuron_p2p.c:62`) / `_unregister_va` (`:135`) — both `EXPORT_SYMBOL_GPL` (`:133`/`:179`) |
| **dma-buf entry** | `ndmabuf_get_fd` (`neuron_dmabuf.c:290`) ← ioctl `DMABUF_FD` (`NR 107`) via `ncdev_get_dmabuf_fd` (`neuron_cdev.c:674`) |
| **Shared resolver** | `nmmap_search_va(nd, va)` under `write_lock(&nd->mpset.rbmmaplock)` — owned by [cdev-mmap](cdev-mmap.md)/[mempool-handles](mempool-handles.md) |
| **PA formula** | `pa = mmap->pa + (va - mmap->va)` (`neuron_p2p.c:54`, `neuron_dmabuf.c:189`) — single contiguous span |
| **Legacy payload** | `struct neuron_p2p_va_info {virtual_address, size, shift_page_size, device_index, entries, page_info[]}` (`neuron_p2p.h:14`) |
| **Page model** | legacy: huge 2 MiB **or** `PAGE_SIZE`, shift = `fls(page_size)-1` (`:128`) · dma-buf: **always** `PAGE_SIZE` per sg entry |
| **dma-buf ops** | `ndmabuf_ops` (`neuron_dmabuf.c:281`) — `attach`/`detach`/`map_dma_buf`/`unmap_dma_buf`/`release` only; **no** `mmap`/`vmap`/`begin_cpu_access` |
| **Export flags** | `DEFINE_DMA_BUF_EXPORT_INFO(exp_info)`; `exp_info.flags = O_CLOEXEC` (`:336`); `dma_buf_fd(.., O_CLOEXEC)` (`:346`) |
| **Version gate** | whole exporter `#if LINUX_VERSION_CODE >= KERNEL_VERSION(5,15,0)` (`neuron_dmabuf.c:28`); else stub `-EPROTONOSUPPORT` (`:375`) |
| **Confidence** | HIGH — all four files read in full; signatures, vtable, call chains, constants, guards verbatim. Struct byte offsets MED (declaration order only) |

---

## 1. The Shared VA→PA Model

### Purpose

Both peer-export paths answer one question: *given a user virtual address into a previously-`mmap`'d Neuron buffer, what host physical address(es) does a third-party DMA engine target?* The answer is identical machinery in both files — the divergence is only in *how the answer is packaged* (a heap struct vs an `sg_table`) and *who consumes it* (a GPL symbol caller vs a dma-buf importer). Fixing the shared model first means §2 and §3 need only describe the packaging.

The buffer the peer wants is one the user already minted through the memory ioctls: a `mem_chunk` allocated by [mempool-handles](mempool-handles.md), then `mmap`'d through the pgoff-is-the-PA cookie owned by [cdev-mmap](cdev-mmap.md). That `mmap` left an `nmmap_node` in the per-PID rbtree (`mpset.mmap_root[16]`, declared in `neuron_mempool.h` but managed by `neuron_mmap.c`) recording the mapping's `{va, pa, size, device_index}`. The peer path does **not** allocate — it reverses the user VA back to that node.

### The resolver and the lock

`nmmap_search_va(nd, va)` (the lookup, owned by the mmap cell) walks one device's per-PID rbtree and returns the `nmmap_node` whose VA range contains `va`, or `NULL`. Because the *caller* of `register_va` (and the dma-buf importer) does not know *which* of the up-to-64 Neuron devices owns the buffer, both paths loop `neuron_pci_get_device(i)` for `i` in `0 .. MAX_NEURON_DEVICE_COUNT-1` (`= 64`, `neuron_device.h:10`) and search each device's tree until one matches. The search is performed under the per-device write-lock `write_lock(&nd->mpset.rbmmaplock)` (`neuron_p2p.c:37`) — the same `rbmmaplock` that guards `mmap_root` for create/delete.

```c
// the shared inner step, identical in spirit across both files
function resolve_va_to_pa(va, size, *out_nd, *out_mmap):
    for i in 0 .. MAX_NEURON_DEVICE_COUNT-1:           // 64; neuron_p2p.c:33 / neuron_dmabuf.c:309
        nd = neuron_pci_get_device(i)                  // [EDGE K-PCI] may be NULL — skip
        if nd == NULL: continue
        write_lock(&nd->mpset.rbmmaplock)              // neuron_p2p.c:37
        mmap = nmmap_search_va(nd, va)                 // [EDGE mmap] per-PID rbtree lookup
        if mmap != NULL:
            offset = va - mmap->va
            if offset + size > mmap->size:             // neuron_p2p.c:44  span must fit the chunk
                <error>                                 // (see CORRECTION below)
            *out_nd = nd; *out_mmap = mmap
            return mmap->pa + offset                   // neuron_p2p.c:54  THE contiguous PA
        write_unlock(&nd->mpset.rbmmaplock)
    return 0                                            // no device matched (not-found sentinel)
```

> **QUIRK —** the model assumes a **single contiguous physical span**. `pa = mmap->pa + (va - mmap->va)` is valid only because the underlying `mem_chunk` is one contiguous HBM/host allocation (the genpool hands out contiguous spans — [mempool-handles §2](mempool-handles.md#2-the-device-pool--grid-small-pool-and-the-bit-63-convention)). The `neuron_p2p_va_info.entries` field exists for "future expansion" to multiple discontiguous segments, but is hard-coded to `1` today (`neuron_p2p.c:100-102`). A reimplementer must not assume a scatter-gather source: there is exactly one PA and one length. The dma-buf path *does* emit many `sg` entries (§3), but they are `PAGE_SIZE` slices of that one contiguous span, not independent allocations.

> **CORRECTION (P2P NOTE-1) —** an earlier reading flagged the `offset + size > mmap->size` failure (`neuron_p2p.c:44-48`) as a latent type-confusion: `neuron_p2p_register_and_get_pa` returns `(u64)-EINVAL` on that failure, and `neuron_p2p_register_va` checks only `if (!pa)` (`:83`), so a "negative" `u64` is not caught as the not-found `0`. Re-reading the shipped source overturns the *exploitability*: the immediately-following alignment guard `if (pa % PAGE_SIZE != 0) return -EINVAL` (`:93-97`) rejects the error-as-PA case, because `(u64)-EINVAL == 0xFFFFFFFFFFFFFFEA` is not page-aligned (`% 4096 == 0xFEA`), and no small errno is page-aligned. The smell remains — a `u64` return cannot structurally separate an error from a PA — but it is self-mitigated as shipped. The clean form returns the PA via an out-parameter and an `int` error. Tracked as [S13](../security/ioctl-attack-surface.md#13-s13--p2p-register_va-error-as-pa-correction-self-mitigated).

> **NOTE —** the boundary is precise: this cell **reads** `mmap->{pa, va, size, device_index}` and **writes** `mmap->{free_callback, data}` (legacy) and `mmap->dmabuf_ref_cnt` (dma-buf). The rbtree, the node lifetime, and the `rbmmaplock` itself are owned by the mmap cell ([cdev-mmap](cdev-mmap.md)); the `mem_chunk` the node points at is owned by [mempool-handles](mempool-handles.md). This page does not re-derive the rbtree walk or the node free — only the fields it touches.

---

## 2. The `register_va` Path

### Purpose

`neuron_p2p_register_va` / `_unregister_va` are the **in-kernel symbol API**: a peer GPL driver (EFA's peer-memory client, in the same shape as `nvidia_p2p_register_va`) links against the exported symbols and calls them directly — no fd, no ioctl. `register_va` resolves a VA to a contiguous PA, installs a `free_callback` so the peer is told when the user tears the buffer down, and returns a heap-allocated descriptor. `unregister_va` reverses it: re-finds the node, clears the callback, and frees the descriptor.

### Entry Point

```text
[external GPL peer driver — EFA peer-memory client]
  neuron_p2p_register_va(va, len, &vainfo, free_callback, data)   p2p.c:62   EXPORT_SYMBOL_GPL :133
    └─ neuron_p2p_register_and_get_pa(va, len, cb, data, &idx)    p2p.c:26
         ├─ loop neuron_pci_get_device(i)        [EDGE K-PCI]     p2p.c:33
         ├─ write_lock(&nd->mpset.rbmmaplock)                     p2p.c:37
         ├─ nmmap_search_va(nd, va)              [EDGE mmap]      p2p.c:38
         ├─ validate offset+size <= mmap->size                   p2p.c:44
         ├─ mmap->free_callback = cb ; mmap->data = data         p2p.c:50-51
         └─ return mmap->pa + (va - mmap->va)                     p2p.c:54
    └─ reject pa==0 / pa not PAGE_SIZE-aligned                    p2p.c:83/93
    └─ kzalloc va_info(+1 page_info) ; huge-vs-PAGE_SIZE ; fill   p2p.c:100-128

  ...user munmaps the buffer →  [EDGE mmap]
    nmmap_delete_node / nmmap_delete_all_nodes  invoke mmap->free_callback(data)

  neuron_p2p_unregister_va(vainfo)                               p2p.c:135   EXPORT_SYMBOL_GPL :179
    └─ neuron_pci_get_device(idx) ; write_lock ; nmmap_search_va
    └─ device_index match check (-EPERM) ; clear callback ; kfree(vainfo)
```

### The `neuron_p2p_va_info` descriptor

The returned heap struct (`neuron_p2p.h:14`) is `kzalloc`'d with exactly one trailing `page_info` entry and carries the contiguous PA, the byte length, the page-size *shift*, the owning device index, and the entry count.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `virtual_address` | +0x00 | `void *` | the user VA the peer registered (echoed back) | HIGH |
| `size` | +0x08 | `u64` | actual byte length of the region | HIGH |
| `shift_page_size` | +0x10 | `u32` | `log2(page_size)`; `fls(page_size)-1` (`:128`) — `12` for 4 KiB, `21` for 2 MiB | HIGH |
| `device_index` | +0x14 | `u32` | owning Neuron device index; set to `-1` on unregister (`:174`) | HIGH |
| `entries` | +0x18 | `u32` | number of `page_info[]` entries — **always 1** today (`:101`) | HIGH |
| `page_info[]` | +0x1c | `neuron_p2p_page_info[]` | flexible array; `{physical_address @+0, page_count @+8}` (`neuron_p2p.h:9`) | HIGH |

Allocation is `kzalloc(sizeof(va_info) + sizeof(page_info) * entries)` with `entries = 1` (`neuron_p2p.c:100-102`) — the flexible-array tail holds the single `{PA, page_count}` pair.

### Algorithm — register

```c
// neuron_p2p_register_va — neuron_p2p.c:62   [EXPORT_SYMBOL_GPL :133]
function neuron_p2p_register_va(virtual_address, length, **va_info, free_callback, data):
    if va_info == NULL || virtual_address == 0: return -EINVAL    // :70  null guards
    device_index = -1

    pa = neuron_p2p_register_and_get_pa(virtual_address, length,  // :81  → §1 resolver + callback stash
                                        free_callback, data, &device_index)
    if pa == 0:        return -EINVAL                             // :83  not-found sentinel (no device matched)
    if pa % PAGE_SIZE != 0:                                       // :93  align guard — ALSO rejects error-as-PA
        pr_err("physical address is not %ld aligned", PAGE_SIZE)  // :94
        return -EINVAL

    // ---- choose the page granularity ----
    if length >= NEURON_P2P_HUGE_PAGE_SZ_USAGE_THRESHOLD         // :112  >= 256 MiB (0x10000000)
       && length % NEURON_P2P_HUGE_PAGE_SZ == 0                  // :113  multiple of 2 MiB
       && virtual_address % NEURON_P2P_HUGE_PAGE_SZ == 0         // :114  VA 2 MiB-aligned
       && pa % NEURON_P2P_HUGE_PAGE_SZ == 0:                     // :115  PA 2 MiB-aligned
        page_size = NEURON_P2P_HUGE_PAGE_SZ                      //       0x200000 = 2 MiB
    else:
        page_size = PAGE_SIZE                                    // :119  4 KiB

    // ---- build the descriptor (single contiguous span) ----
    vainfo = kzalloc(sizeof(*vainfo) + sizeof(page_info) * 1)    // :100  entries = 1
    if vainfo == NULL: return -ENOMEM
    vainfo->virtual_address = virtual_address                    // :124
    vainfo->size            = length
    vainfo->device_index    = device_index
    vainfo->entries         = 1                                  // :101
    vainfo->shift_page_size = fls(page_size) - 1                 // :128  log2(page_size)
    vainfo->page_info[0].physical_address = pa                   // contiguous base PA
    vainfo->page_info[0].page_count       = length / page_size   // # pages of (1<<shift) each
    *va_info = vainfo
    return 0
```

```c
// neuron_p2p_register_and_get_pa — neuron_p2p.c:26   (static helper)
function neuron_p2p_register_and_get_pa(va, size, free_callback, data, *device_index):
    for i in 0 .. MAX_NEURON_DEVICE_COUNT-1:                     // :33  64 devices
        nd = neuron_pci_get_device(i)                            // [EDGE K-PCI]
        if nd == NULL: continue
        write_lock(&nd->mpset.rbmmaplock)                        // :37
        mmap = nmmap_search_va(nd, va)                           // :38  [EDGE mmap]
        if mmap != NULL:
            if (va - mmap->va) + size > mmap->size:              // :44  span overflow → (u64)-EINVAL
                write_unlock(&nd->mpset.rbmmaplock); return -EINVAL
            mmap->free_callback = free_callback                  // :50  peer teardown hook stashed in NODE
            mmap->data          = data                           // :51
            *device_index       = mmap->device_index
            pa = mmap->pa + (va - mmap->va)                      // :54  contiguous PA
            write_unlock(&nd->mpset.rbmmaplock)
            return pa
        write_unlock(&nd->mpset.rbmmaplock)
    return 0                                                     // :59  no device matched
```

### Algorithm — unregister

```c
// neuron_p2p_unregister_va — neuron_p2p.c:135   [EXPORT_SYMBOL_GPL :179]
function neuron_p2p_unregister_va(vainfo):
    if vainfo == NULL: return -EINVAL
    nd = neuron_pci_get_device(vainfo->device_index)             // re-fetch device by stored index
    if nd == NULL: return -ENODEV
    write_lock(&nd->mpset.rbmmaplock)
    if vainfo->device_index >= MAX_NEURON_DEVICE_COUNT:          // re-bound the index defensively
        write_unlock; return -EINVAL
    mmap = nmmap_search_va(nd, vainfo->virtual_address)          // re-find the node
    if mmap == NULL: { write_unlock; return -ENOENT }
    if mmap->device_index != vainfo->device_index:              // :164  SECURITY re-check
        pr_err("Device index mismatch during unregister")
        write_unlock; return -EPERM
    mmap->free_callback = NULL ; mmap->data = NULL              // clear the peer hook
    write_unlock(&nd->mpset.rbmmaplock)
    vainfo->device_index = -1                                    // :174  poison the descriptor
    kfree(vainfo)
    return 0
```

### Considerations

The huge-page decision is the one piece of policy that distinguishes `register_va` from the dma-buf path. The legacy path can advertise a **2 MiB** page granularity (`shift_page_size = 21`) when the region is `>= 256 MiB` and the length, VA, *and* PA are all 2 MiB-aligned (`neuron_p2p.c:112-115`); otherwise it falls back to `PAGE_SIZE` (4 KiB, `shift = 12`). A larger page size means the peer's IOMMU/page-table walk costs fewer entries for a big contiguous tensor. The four-way conjunction is conservative: any single mis-alignment downgrades the whole region to 4 KiB pages. Note the asymmetry with §3 — the dma-buf exporter **never** uses huge pages; every `sg` entry is exactly `PAGE_SIZE`, so the two peer paths describe the *same* physical bytes with different page granularities.

The `free_callback` is the legacy path's only teardown signal. It is stashed into the *node* (not the descriptor), so when the user `munmap`s the buffer — `nmmap_delete_node` / `nmmap_delete_all_nodes`, owned by [cdev-mmap](cdev-mmap.md) — the node's `free_callback(data)` fires, telling the peer "this PA is gone, stop DMA." This is the same `free_callback` field the dma-buf path leaves untouched; the two mechanisms share the node but use disjoint fields of it. The header doc states the callback is invoked "with a lock held," but the mmap-cell free site calls it *after* `write_unlock` — a doc/impl note flagged at the boundary, not resolved here (the free site is in the mmap cell).

---

## 3. The dma-buf Exporter

### Purpose

`neuron_dmabuf.c` is the Linux dma-buf exporter — the modern, fd-based peer path that ibcore/EFA prefers because it composes with the standard importer machinery and the `FI_MR_DMABUF` registration libfabric uses ([transport-efa §4](../nccom/transport-efa.md#4-the-connection-lifecycle-and-memory-registration)). A user ioctl mints an anonymous `dma_buf` whose `priv` is a small `ndmabuf_private_data` recording `{va, size, fd, device_idx, is_attached}`; the importer then drives the five-op vtable to obtain an `sg_table` of device PAs.

### Entry Point

```text
user ioctl NEURON_IOCTL_DMABUF_FD  (NR 107)                     neuron_cdev.c:3158
  └─ ncdev_get_dmabuf_fd(param)            [EDGE K-IOCTL]        neuron_cdev.c:674
       └─ ndmabuf_get_fd(va, size, &fd)                         neuron_dmabuf.c:290
            ├─ kzalloc(ndmabuf_private_data)                    :298
            ├─ for-all-devices nmmap_search_va to validate VA   :309-322  [EDGE mmap]
            ├─ DEFINE_DMA_BUF_EXPORT_INFO(exp_info)             :333
            ├─ dma_buf_export(&exp_info{ndmabuf_ops, O_CLOEXEC, priv})  :339
            └─ dma_buf_fd(dmabuf, O_CLOEXEC)                    :346
  └─ copy_to_user(arg.fd, &dmabuf_fd)                           neuron_cdev.c:688

[importer — ibcore / EFA peer-direct] drives the vtable:
    .attach        ndmabuf_attach   CAS is_attached 0→1                 :58
    .map_dma_buf   ndmabuf_map      build sg_table ; mmap->dmabuf_ref_cnt++   :126
       ... peer device DMAs into/out of the device PAs ...
    .unmap_dma_buf ndmabuf_unmap    ref_cnt-- ; sg_free_table + kfree   :223
    .detach        ndmabuf_detach   CAS 1→0 ; FD RECYCLE put_unused_fd+fput   :79
    .release       ndmabuf_release  kfree(private_data)  on last dma_buf_put  :271
```

### The private-data struct

`struct ndmabuf_private_data` (`neuron_dmabuf.c:40`, the `dmabuf->priv`) is the exporter's per-fd state. Byte offsets past `is_attached` assume natural LP64 layout (MED); field order is verbatim (HIGH).

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `va` | +0x00 | `void *` | userspace VA of the exported buffer | HIGH |
| `size` | +0x08 | `u64` | buffer size in bytes | HIGH |
| `is_attached` | +0x10 | `bool` | single-attach guard, CAS'd 0/1 via `__sync_bool_compare_and_swap` | HIGH |
| `fd` | ~+0x14 | `int` | the installed dma-buf fd; recycled in `detach` | MED (alignment) |
| `device_idx` | ~+0x18 | `int` | Neuron device index owning the buffer | MED (alignment) |

Lifetime: `kzalloc` in `ndmabuf_get_fd` (`:298`), `kfree` in `ndmabuf_release` (`:278`) or the error paths (`:367`).

### Algorithm — export

```c
// ndmabuf_get_fd — neuron_dmabuf.c:290   [public; neuron_dmabuf.h:25]   (5.15+ branch)
function ndmabuf_get_fd(va, size, *dmabuf_fd):
    priv = kzalloc(sizeof(*priv))                               // :298
    if priv == NULL: return -ENOMEM
    priv->va = va ; priv->size = size ; priv->is_attached = false

    // validate the VA exists on SOME device (mirror of §1 resolver, search-only)
    found = false
    for i in 0 .. MAX_NEURON_DEVICE_COUNT-1:                    // :309
        nd = neuron_pci_get_device(i)
        if nd == NULL: continue
        write_lock(&nd->mpset.rbmmaplock)
        if nmmap_search_va(nd, va) != NULL:                     // :316  [EDGE mmap]
            priv->device_idx = i ; found = true
            write_unlock; break
        write_unlock(&nd->mpset.rbmmaplock)
    if !found:
        pr_err("No matching memory was found with va=0x%llx "   // :326
               "after searching all neuron devices", va)
        kfree(priv); return -EINVAL

    DEFINE_DMA_BUF_EXPORT_INFO(exp_info)                        // :333
    exp_info.ops   = &ndmabuf_ops                              // :334
    exp_info.size  = size                                       // :335
    exp_info.flags = O_CLOEXEC                                  // :336
    exp_info.priv  = priv                                       // :337
    dmabuf = dma_buf_export(&exp_info)                          // :339  anon dma_buf, refcount 1
    if IS_ERR(dmabuf): { kfree(priv); return PTR_ERR(dmabuf) }

    fd = dma_buf_fd(dmabuf, O_CLOEXEC)                          // :346  install fd
    if fd < 0: { dma_buf_put(dmabuf); return fd }              //       release drops the dma_buf
    priv->fd   = fd                                             // store for the detach FD-recycle
    *dmabuf_fd = fd
    return 0
```

### Algorithm — the map/unmap hot path

`map_dma_buf` is the only path that actually emits PAs. It re-validates the single-attach CAS, re-resolves the VA (the node may have moved), allocates an `sg_table` sized at one `PAGE_SIZE` entry per page of the buffer, and fills each entry with the running device PA.

```c
// ndmabuf_map — neuron_dmabuf.c:126   (.map_dma_buf)
function ndmabuf_map(attachment, direction):
    priv = attachment->dmabuf->priv
    if !__sync_bool_compare_and_swap(&priv->is_attached, 1, 1): // :148  must be attached
        pr_err("Must attach() before map()") ; return ERR_PTR(-EINVAL)

    nd = neuron_pci_get_device(priv->device_idx)
    write_lock(&nd->mpset.rbmmaplock)
    mmap = nmmap_search_va(nd, priv->va)                        // re-resolve (node may have changed)
    if mmap == NULL: { write_unlock; return ERR_PTR(-EINVAL) }

    pa = mmap->pa + (priv->va - mmap->va)                       // :189  contiguous base PA
    if pa % PAGE_SIZE != 0:                                     // :194
        pr_err("physical address is not %ld aligned", PAGE_SIZE)
        write_unlock; return ERR_PTR(-EINVAL)

    sgt = kzalloc(sizeof(*sgt))                                 // sg_table header
    n   = ALIGN(priv->size, PAGE_SIZE) / PAGE_SIZE              // :183  one entry per page
    sg_alloc_table(sgt, n, GFP_KERNEL)                          // :184

    for_each_sgtable_dma_sg(sgt, sg, k):                        // :200
        sg_dma_address(sg) = pa                                 // :202  device PA for this page
        sg_dma_len(sg)     = PAGE_SIZE                          // :204  ALWAYS PAGE_SIZE (no huge pages)
        pa += PAGE_SIZE                                         // :205  next contiguous page

    mmap->dmabuf_ref_cnt++                                      // :208  in-flight peer-DMA refcount
    write_unlock(&nd->mpset.rbmmaplock)
    return sgt

// ndmabuf_unmap — neuron_dmabuf.c:223   (.unmap_dma_buf)
function ndmabuf_unmap(attachment, sgt, direction):
    priv = attachment->dmabuf->priv
    BUG_ON(!__sync_bool_compare_and_swap(&priv->is_attached, 1, 1))  // :240  must be attached
    nd = neuron_pci_get_device(priv->device_idx) ; BUG_ON(nd == NULL) // :247
    write_lock(&nd->mpset.rbmmaplock)
    mmap = nmmap_search_va(nd, priv->va)                        // may be NULL — TOLERATED (node freed)
    if mmap != NULL:
        BUG_ON(mmap->dmabuf_ref_cnt == 0)                      // :260  underflow tripwire
        mmap->dmabuf_ref_cnt--                                  // drop the in-flight ref
    write_unlock(&nd->mpset.rbmmaplock)
    sg_free_table(sgt) ; kfree(sgt)
```

### Algorithm — attach, detach, and the FD recycle

```c
// ndmabuf_attach — neuron_dmabuf.c:58   (.attach)   single-attach enforced
function ndmabuf_attach(dmabuf, attachment):
    priv = dmabuf->priv
    if priv == NULL: return -EINVAL
    if !__sync_bool_compare_and_swap(&priv->is_attached, 0, 1): // CAS 0→1
        pr_err("Only one device is allowed to be attached "    // :71
               "to a Neuron dmabuf object")
        return -EPERM
    return 0

// ndmabuf_detach — neuron_dmabuf.c:79   (.detach)   the FD recycle
function ndmabuf_detach(dmabuf, attachment):
    priv = dmabuf->priv
    if !__sync_bool_compare_and_swap(&priv->is_attached, 1, 0): // CAS 1→0
        pr_err("multiple detach calls are not allowed for the same dmabuf object")  // :92
        return
    nd = neuron_pci_get_device(priv->device_idx) ; BUG_ON(nd == NULL)  // :101
    if npid_is_attached(nd):                                    // :116  [EDGE K-PID] process still alive
        put_unused_fd(priv->fd)                                 // :118  RECYCLE: return fd to the table
        fput(file_of(priv->fd))                                 //        drop the file ref

// ndmabuf_release — neuron_dmabuf.c:271   (.release)   last dma_buf_put
function ndmabuf_release(dmabuf):
    kfree(dmabuf->priv)                                         // :278
```

### Considerations

The **FD recycle** in `detach` is the exporter's most surprising mechanic. A normal dma-buf exporter holds the installed fd until the process exits — the fd *is* the userspace handle to the buffer. Neuron instead reclaims the fd in `detach` (`put_unused_fd(priv->fd) + fput`, `:118`) when the owning process is still attached (`npid_is_attached(nd)`, `:116`). The rationale is FD-exhaustion avoidance: a single process that repeatedly loads and unloads NEFFs would mint a new dma-buf fd per buffer per load, and without recycling would leak fds across the model's lifetime. By recycling on `detach`, the exporter caps the fd cost at the *concurrently-mapped* set, not the *cumulative* set. The `npid_is_attached` gate guards against recycling into a dead process's fd table — if the process already exited, the fd table is gone and the recycle is skipped (the file refs unwind through normal exit cleanup).

The `dmabuf_ref_cnt` on the *node* (bumped in `map` `:208`, dropped in `unmap`, `BUG_ON` on underflow `:260`) is the in-flight-DMA counter the teardown race in §5 turns on. It is a node field, so it is visible to the mmap cell's node-free path — which is exactly the interlock that §5 shows to be advisory rather than blocking. Note that `unmap` *tolerates* a `NULL` node (`:170`): if the user already munmap'd the buffer, the node is gone, and `unmap` simply frees its `sg_table` without touching a refcount — the warning already fired at node-free time (§5).

---

## 4. The `dma_buf_ops` Vtable

The exporter registers exactly five of the dma-buf operations (`ndmabuf_ops`, `neuron_dmabuf.c:281`). The absent ops are as informative as the present ones: there is **no** `.mmap` (the peer DMAs by PA, it does not CPU-map the buffer), no `.vmap` (no kernel-VA view), and no `.begin_cpu_access`/`.end_cpu_access` (no CPU-side cache coherency dance — the buffer is device memory the peer reads/writes directly).

| Op | Handler | `file:line` | Role | Confidence |
|---|---|---|---|---|
| `.attach` | `ndmabuf_attach` | `:282` → `:58` | CAS `is_attached` 0→1; reject second attach (`-EPERM`) | HIGH |
| `.detach` | `ndmabuf_detach` | `:283` → `:79` | CAS 1→0; FD recycle (`put_unused_fd`+`fput`) iff process attached | HIGH |
| `.map_dma_buf` | `ndmabuf_map` | `:284` → `:126` | build `sg_table` of `PAGE_SIZE` entries, `sg_dma_address = PA`; `ref_cnt++` | HIGH |
| `.unmap_dma_buf` | `ndmabuf_unmap` | `:285` → `:223` | `ref_cnt--`; `sg_free_table`+`kfree`; tolerate freed node | HIGH |
| `.release` | `ndmabuf_release` | `:286` → `:271` | `kfree(priv)` on last `dma_buf_put` | HIGH |
| `.mmap` | — | absent | peer DMAs by PA; no CPU userspace mapping of the dma-buf | HIGH |
| `.vmap` / `.vunmap` | — | absent | no kernel-VA view of device memory | HIGH |
| `.begin_cpu_access` / `.end_cpu_access` | — | absent | no CPU cache-coherency hooks (device-memory-only) | HIGH |

> **NOTE —** `DEFINE_DMA_BUF_EXPORT_INFO(exp_info)` seeds the export descriptor; the exporter then sets `ops = &ndmabuf_ops`, `size`, `flags = O_CLOEXEC`, `priv` (`:334-337`). The `O_CLOEXEC` appears twice — on `exp_info.flags` (`:336`) and on `dma_buf_fd` (`:346`) — so the fd is close-on-exec from the moment it is installed.

### Version-compat envelope

The whole exporter is conditionally compiled; below 5.15 the public entry is a stub. The `MODULE_IMPORT_NS` form changed twice across the supported kernel range — a reimplementer targeting multiple kernels must reproduce all three gates.

| Guard | `file:line` | Effect |
|---|---|---|
| `#if LINUX_VERSION_CODE >= KERNEL_VERSION(5,15,0)` | `neuron_dmabuf.c:28` | the entire exporter; else the `-EPROTONOSUPPORT` stub (`:375`) |
| `#if >= KERNEL_VERSION(5,16,0)` | `:30` | gate `MODULE_IMPORT_NS` at all (the namespace-import machinery is 5.16+) |
| `#if >= 6.13.0 \|\| RHEL_RELEASE >= RHEL10` | `:31-36` | `MODULE_IMPORT_NS("DMA_BUF")` (string literal) **else** `MODULE_IMPORT_NS(DMA_BUF)` (bare token) |
| `int ndmabuf_get_fd(...) { return -EPROTONOSUPPORT; }` | `:373-375` | the `<5.15` fallback — the only symbol the rest of the driver sees on old kernels |

> **QUIRK —** the `MODULE_IMPORT_NS` churn (`:31-36`) is pure API-form drift, not a behavior change: kernel 6.13 (and RHEL 10) changed the namespace-import macro from a bare token (`DMA_BUF`) to a string literal (`"DMA_BUF"`). Both forms import the same `DMA_BUF` symbol namespace so the driver may call `dma_buf_export` / `dma_buf_fd` / `sg_alloc_table` etc. A reimplementation that hard-codes one form fails to build on the other half of the supported kernel range.

---

## 5. The Warn-Only Teardown GOTCHA

Both peer paths share one structural hazard: the buffer's backing `mem_chunk` and its tracking `nmmap_node` can be torn down **while a peer is mid-DMA**, and the node-free path only *warns* — it does not block or defer.

### The window

A user process: (1) exports the buffer (`DMABUF_FD` → `ndmabuf_get_fd`, or `register_va`); (2) the importer (EFA) attaches and maps, bumping `mmap->dmabuf_ref_cnt` (`:208`) and beginning DMA into the device PAs; (3) the *same* process then `munmap`s the buffer — or dies — invoking `nmmap_delete_node` / `nmmap_delete_all_nodes` in the mmap cell. That free path (`nmmap_remove_node_rbtree`, owned by [cdev-mmap](cdev-mmap.md)) only `pr_warn`s when `dmabuf_ref_cnt > 0`, then `rb_erase`s and the caller `kfree`s the node — *regardless* of the in-flight ref. The peer still holds an `sg_table` whose `sg_dma_address` points at PAs whose tracking node has been freed and whose memory may be re-handed-out by the genpool.

```text
exporter side (in-scope)            mmap cell (boundary)               peer (EFA)
─────────────────────────           ──────────────────────            ──────────────
ndmabuf_map: ref_cnt++  ────────────────────────────────────────────► DMA in flight
                                    munmap → nmmap_delete_node
                                      ref_cnt > 0 ?  pr_warn ONLY  ◄─── still mapping!
                                      rb_erase ; kfree(node)  ×
ndmabuf_unmap: node NULL — tolerated                                    DMA into freed PA
```

> **GOTCHA —** the `dmabuf_ref_cnt` is an *advisory* counter, not a teardown interlock. `ndmabuf_map` bumps it (`:208`) and `ndmabuf_unmap` drops it, but the node-free path on the mmap side `pr_warn`s and frees anyway — there is no path that *blocks* the free while `ref_cnt > 0`. The exporter's own `unmap` is written to tolerate a `NULL` node (the node may already be gone, `:170`), which keeps the *driver* from crashing, but does nothing for the *peer*, which is DMAing into physical addresses whose backing chunk may have been recycled. This is the "application terminated midway between register and deregister" race the authors explicitly acknowledge (mmap-cell comment). It is a genuine use-after-free window for device DMA, bounded only by importer timing — not a deterministic primitive. The consolidated security framing, severity, and proposed fix (defer/block `rb_erase`+`kfree` while `ref_cnt > 0`, or force-invalidate the peer mapping before free) are on [the attack-surface page as S6](../security/ioctl-attack-surface.md#8-s6--dma-buf-node-teardown-use-after-free-window).

> **NOTE —** the `register_va` path has the analogous-but-milder version: its teardown signal is the `free_callback` fired from the *same* node-free path. The callback is the peer's "stop now" notification, but it is invoked synchronously from the free site with no acknowledgement — the peer is *told* the PA is gone, but the node is freed whether or not the peer has quiesced its DMA. The dma-buf path's `ref_cnt` at least *records* in-flight DMA; the legacy path relies entirely on the peer reacting to `free_callback` in time.

---

## Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `neuron_p2p_register_va` | `neuron_p2p.c:62` | `[GPL]` VA→PA, stash callback, build `va_info`; huge-vs-PAGE_SIZE | HIGH |
| `neuron_p2p_register_and_get_pa` | `:26` | per-device rbtree walk; validate span; stash callback; return contiguous PA | HIGH |
| `neuron_p2p_unregister_va` | `:135` | `[GPL]` re-find node; `device_index` `-EPERM` check; clear callback; `kfree` | HIGH |
| `ndmabuf_get_fd` | `neuron_dmabuf.c:290` | `[PUBLIC]` validate VA; `dma_buf_export`; `dma_buf_fd(O_CLOEXEC)` | HIGH |
| `ndmabuf_get_fd` (stub) | `:373` | `<5.15` fallback: `return -EPROTONOSUPPORT` | HIGH |
| `ndmabuf_attach` | `:58` | CAS `is_attached` 0→1; single-attach `-EPERM` | HIGH |
| `ndmabuf_detach` | `:79` | CAS 1→0; FD recycle `put_unused_fd`+`fput` iff `npid_is_attached` | HIGH |
| `ndmabuf_map` | `:126` | build `sg_table` (`PAGE_SIZE` entries, `sg_dma_address=PA`); `ref_cnt++` | HIGH |
| `ndmabuf_unmap` | `:223` | `ref_cnt--`; `sg_free_table`+`kfree`; tolerate freed node | HIGH |
| `ndmabuf_release` | `:271` | `kfree(priv)` on last `dma_buf_put` | HIGH |
| `ndmabuf_ops` | `:281` | the 5-slot `dma_buf_ops` vtable | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `nmmap_search_va` / `nmmap_node` (`neuron_mmap.c`) | the per-PID rbtree VA→node lookup and the node whose `{pa,va,size}` both paths read and whose `free_callback`/`dmabuf_ref_cnt` they write — owned by [cdev-mmap](cdev-mmap.md) |
| `neuron_pci_get_device` (`neuron_pci.c:64`) | the device-table accessor both paths loop over (`BUG_ON(idx>=64)`, may return NULL) |
| `npid_is_attached` (`neuron_pid.c:60`) | the gate for the `detach` FD-recycle — recycle only into a live process |
| `ncdev_get_dmabuf_fd` (`neuron_cdev.c:674`) | the sole userspace ioctl entry into `ndmabuf_get_fd` (`DMABUF_FD`, `NR 107`) |
| Linux dma-buf core | `dma_buf_export` / `dma_buf_fd` / `dma_buf_put` / `sg_alloc_table` / `put_unused_fd` / `fput` — imported via `MODULE_IMPORT_NS(DMA_BUF)` |

## Cross-References

- [Char Device, fops and mmap](cdev-mmap.md) — the pgoff-is-the-PA cookie that mints the user mapping, the per-PID `mmap_root` rbtree this page walks, and the `nmmap_delete_node` free path whose warn-only teardown is the §5 hazard
- [Memory Pool and MC Handle Table](mempool-handles.md) — the `mem_chunk` the exported buffer lives in: the genpool that guarantees the single-contiguous-span assumption both peer paths rely on, and the PA-keyed rbtree
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security framing of the warn-only teardown UAF (S6) and the self-mitigated `register_va` error-as-PA smell (S13)
- [Inter-Node Transport: EFA / libfabric](../nccom/transport-efa.md) — the real consumer: the EFA peer-direct importer that registers device memory via `FI_MR_DMABUF`, the libfabric `reg_mr` path that drives this exporter, and the RDMA fast path that DMAs into the PAs published here
- [back to index](../index.md)
