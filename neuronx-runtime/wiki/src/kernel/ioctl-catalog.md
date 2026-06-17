# IOCTL Catalog

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The command macros are read verbatim from `neuron_ioctl.h` (876 lines); the handlers from `neuron_cdev.c` (4043 lines). The source is read directly, not reverse-engineered; every `_IOC` number is anchored to its `#define` line and every handler to its function line. Other driver versions renumber lines and add/remove commands. Part — Kernel Driver · [back to index](../index.md)*

## Abstract

This page is the authoritative cross-map of the Neuron driver's `ioctl` command set: the complete table of every command under magic char `'N'` (`NEURON_IOCTL_BASE`, `0x4E`, `neuron_ioctl.h:656`), its `_IOC` number and direction, its argument struct and size, the kernel handler it dispatches to, the `libnrt`/NDL `ndl_*` caller that issues it, and the gate tier that admits it. It is the reference a reimplementer drives the device from: pick a command, read its row, learn exactly which struct to pack, which fd lane to use, which line in `neuron_cdev.c` consumes it, and whether `DEVICE_INIT` ownership is required. The dispatch *mechanism* — the three-tier gate model, the `_IOC_NR`/`_IOC_SIZE`/`_IOC_DIR` overload-resolution discipline, the `*64` sub-gate slip — is owned by the [dispatch page](ioctl-dispatch.md) and is not re-derived here; this page links to it and tabulates the result.

The command space is 96 distinct macros over a sparse `_IOC_NR` set `{1..135}`, grouped into five families: **mem** (allocation, copy, memset, info, zerocopy — handlers traced by [the mem page](ioctl-mem.md)), **dma** (engine/queue lifecycle, descriptor I/O, H2T queue management — [the dma page](ioctl-dma.md)), **nq** (notification-queue init, plus semaphore/event/hw-counter — [the nq page](ioctl-nq.md)), **pod** (UltraServer membership query/control — [the pod/misc page](ioctl-pod.md)), and **misc** (device info, version, BDF, topology, PRINTK, the deprecated/stub set). Eleven `_IOC_NR` values are size-overloaded: two or three differently-sized structs share one `nr` and direction, demultiplexed kernel-side by exact `cmd ==` or `_IOC_SIZE(cmd)`. The gate tier is one of three: **PID** (the command is in the `npid_is_attached` attach allow-list, `:3210-3219`, so it requires the `DEVICE_INIT` owner on a non-free-access fd), **FA** (free-access: reachable on the `O_WRONLY` misc lane through `ncdev_misc_ioctl`, `:3147`, with no attach gate), or **none** (a non-free-access fd, no attach sub-gate). Every command present kernel-side has a `libnrt` caller except the deprecated/stub set (`DEVICE_INIT`/`RELEASE`/`RESET`, `DMA_ENG_INIT`, `NOTIFICATIONS_DESTROY_V1`/`QUEUE_INFO`, `DMA_QUIESCE_QUEUES`).

For this reference, the contract is:

- **The exact command constant** — `nr`, direction, and the 32-bit `cmd` the caller passes (`strace`-visible), recomputed from the `_IOC` macro and verified against the `#define` line.
- **The arg struct and its byte size** — what `libnrt` packs and the handler `copy_from_user`s; for size-overloaded families, every variant.
- **The handler and the caller** — the `ncdev_*` function (`file:line`) that consumes the command, and the `ndl_*` userspace caller (`@addr`, `libnrt.so`) that issues it.
- **The gate tier and variants** — PID / FA / none, plus the V1/V2/`*64` sibling set per `nr`, so a reimplementer knows which fd lane and which struct width each command demands.

| | |
|---|---|
| **Command base** | `'N' = 0x4E` (`neuron_ioctl.h:656`) |
| **Macro span** | `neuron_ioctl.h:659-874`; `_IOC_NR` set `{1..135}` sparse, 96 macros |
| **Dispatcher** | `ncdev_ioctl` (`neuron_cdev.c:3188`) · misc lane `ncdev_misc_ioctl` (`:3147`) |
| **Encoding** | `dir<<30 \| size<<16 \| 'N'<<8 \| nr`; ptr-form macros ⇒ `_IOC_SIZE = 8` (LP64) |
| **Gate tiers** | **PID** (attach allow-list `:3210-3219`) · **FA** (free-access misc lane) · **none** |
| **Size-overloaded `nr`** | 11 (`23`, `24`, `28`, `38`, `102`, `105`, `106`, `108`, `110`, `111`, `122`, `123`) |
| **Userspace caller band** | `libnrt.so` `ndl_*` `.text` `0xc1940..0xc5a30` (NDL) |
| **Confidence** | HIGH — every `_IOC` macro line and every handler line read verbatim from the shipped source |

---

## At a Glance — The Five Families and Their Lanes

The catalogue partitions by family; each family's gate profile is distinctive, and that profile is the first thing a reimplementer needs.

| Family | `nr` span | Handler file region | Dominant gate | Notes |
|---|---|---|---|---|
| **mem** | `21-29`, `102`, `105`, `107-112`, `116`, `124`, `129` | `neuron_cdev.c:416-1426`, `:2299`, `:2390` | PID (alloc/free/copy) + none | size-overloaded `23`/`24`/`28`/`102`/`105`/`108`/`111`; `*64` slips the attach gate |
| **dma** | `30-40`, `125-126`, `133`, `135` | `:103-414`, `:2920-3144` | PID (control) + none (queries) | `38` `*64` slips the attach gate; `DMA_QUIESCE` (`40`) removed |
| **nq / sem / event** | `41-46`, `51-54`, `58`, `61` | `:1428-1476`, `:1694`, `:1996-2074` | PID (NQ init only) + none | sem/event/hw-counter ungated; `DESTROY_V1`/`QUEUE_INFO` stubs |
| **pod** | `121-123` | `:2804-2918` | **FA** (all three) | V1/V2 size-overloaded `122`/`123`; state-changing `POD_CTRL` on the ungated lane |
| **misc / info** | `1-13`, `71-72`, `81-93`, `100-101`, `103-104`, `106-107`, `110`, `113-115`, `117-120`, `127-128`, `130-132`, `134` | scattered | mixed (FA info + none + PID) | deprecated stubs at `1`/`4`/`5`/`30`/`52`/`58`; `PRINTK`/`CRWL`/`DRIVER_INFO_GET` on the FA lane |

The dispatch cell (`ncdev_ioctl`, `:3188`) routes by exact `cmd ==` for single-variant commands and by `_IOC_NR(cmd) ==` for the size-overloaded families; the [dispatch page §3](ioctl-dispatch.md#3-overload-resolution--_ioc_nr-_ioc_size-_ioc_dir) owns that logic. The columns below name the dispatch line so a row can be cross-checked, but the resolution rule itself is not repeated per row.

### Reading a row

- **cmd name** — the `NEURON_IOCTL_*` macro suffix.
- **`_IOC` nr** — the `_IOC_NR`, exactly as the `#define` assigns it (header line in the citation).
- **dir** — `_IO` (NONE) · `_IOR` (R) · `_IOW` (W) · `_IOWR` (RW), from the macro. *Note the driver ignores `_IOC_DIR` for dispatch except in `ncdev_driver_info`; several read-back handlers are encoded `_IOR` yet `copy_to_user` results — see the [dispatch page](ioctl-dispatch.md#scheme-b--struct-version-overloads-size-resolved-inside-the-handler).*
- **arg struct / size** — the `struct neuron_ioctl_*` packed by the caller; **size** is the C `sizeof` (LP64, natural packing). Pointer-form macros encode `_IOC_SIZE = 8` regardless of the pointed-to struct; struct-form macros encode the struct's `sizeof` (which is what makes a `*64` sibling's `cmd` differ).
- **kernel handler (file:line)** — the `ncdev_*` function in `neuron_cdev.c`, plus the dispatch line in parentheses.
- **`ndl_*` caller** — the `libnrt` NDL function (`@addr` in `libnrt.so`) that issues this command, from the DWARF-attributed NDL disassembly.
- **gate** — **PID** (attach allow-list `:3210-3219`) · **FA** (free-access misc lane) · **none**.
- **variants** — sibling commands sharing the `nr` (`/64`, `/V2`, `/V2MT*`, `_EXT0`).

> **QUIRK —** the byte `size` and the encoded `_IOC_SIZE` are not always equal. A *pointer-form* macro — `_IOR('N', 21, struct neuron_ioctl_mem_alloc *)` — encodes `_IOC_SIZE = sizeof(void*) = 8`, so its `cmd` is `0x80084E15` even though the struct it points at is 32 bytes. A *struct-form* macro — `_IOR('N', 23, struct neuron_ioctl_mem_copy64)` (no `*`) — encodes `_IOC_SIZE = sizeof(struct) = 40`, giving `0x80284E17`. This is exactly how the two siblings of an overloaded `nr` get distinct `cmd` constants: `MEM_COPY` is pointer-form (8) and `MEM_COPY64` is struct-form (40). The **size** column below reports the *struct* size; the **cmd-hex** in prose reports the encoded value.

---

## Family: mem

Allocation, copy, memset, info queries, and the zero-copy fast path. Handlers traced in [the memory page](ioctl-mem.md); the security boundary for every copy/memset is `mc_access_is_within_bounds` (`neuron_mempool.h:225`). The `npid_is_attached` attach gate covers `MEM_ALLOC`, `MEM_FREE`, `MEM_COPY` (base), `MEM_GET_PA`, `MEM_COPY_ASYNC` (base), `MEM_COPY_ASYNC_WAIT`, `MEM_GET_INFO` (`:3213-3216`); the `*64` siblings and the buf-copy/zerocopy/memset families are **not** in the attach list (they still require a non-free-access fd).

| cmd name | `_IOC` nr | dir | arg struct / size | kernel handler (file:line) | `ndl_*` caller | gate | variants | Conf |
|---|---|---|---|---|---|---|---|---|
| `MEM_ALLOC` | 21 (`:681`) | R | `mem_alloc` * / 32 | `ncdev_mem_alloc` `:416` (`:3282`) | `ndl_memory_alloc_cm2` `@0xc1a00` (legacy) | PID | — | HIGH |
| `MEM_FREE` | 22 (`:684`) | R | `mem_free` * / 8 | `ncdev_mem_free` `:691` (`:3292`) | `ndl_memory_free` `@0xc2ba0` | PID | — | HIGH |
| `MEM_COPY` | 23 (`:686`) | R | `mem_copy` * / 28 | `ncdev_mem_copy` `:761` (`_IOC_NR` `:3294`) | `ndl_memory_copy_cm` `@0xc1c30` | PID | `MEM_COPY64` | HIGH |
| `MEM_COPY64` | 23 (`:687`) | R | `mem_copy64` / 40 | `ncdev_mem_copy` `:761` (`_IOC_NR` `:3294`) | `ndl_memory_copy` `@0xc2dc0` | **none** × | `MEM_COPY` | HIGH |
| `MEM_BUF_COPY` | 24 (`:690`) | RW | `mem_buf_copy` * / 8 | `ncdev_mem_buf_copy` `:1045` (`_IOC_NR` `:3300`) | `ndl_memory_buf_copy_cm` `@0xc1b50` | none | `MEM_BUF_COPY64` | HIGH |
| `MEM_BUF_COPY64` | 24 (`:691`) | RW | `mem_buf_copy64` / 40 | `ncdev_mem_buf_copy` `:1045` (`_IOC_NR` `:3300`) | `ndl_memory_buf_copy` `@0xc24d0` | none | `MEM_BUF_COPY` | HIGH |
| `MEM_GET_PA` | 25 (`:694`) | R | `mem_get_pa` * / 8 | `ncdev_mem_get_pa_deprecated` `:551` (`:3290`) | `ndl_memory_get_pa` `@0xc2a00` | PID | — | HIGH |
| `MEM_GET_INFO` | 26 (`:696`) | R | `mem_get_info` * / 8 | `ncdev_mem_get_info_deprecated` `:568` (`:3288`) | `ndl_memory_map` `@0xc2a40`; `ndl_memory_map_contiguous_scratchpad` `@0xc54d0` | PID | — | HIGH |
| `PROGRAM_ENGINE` | 27 (`:697`) | RW | `program_engine` * / 8 | `ncdev_program_engine` `:949` (`:3306`) | `ndl_program_engine_cm` `@0xc2040` | none | — | HIGH |
| `MEMSET` | 28 (`:699`) | R | `memset` * / 8 | `ncdev_memset` `:709` (`_IOC_NR` `:3310`) | `ndl_memset_cm` `@0xc1bc0` | none | `MEMSET64` | HIGH |
| `MEMSET64` | 28 (`:700`) | R | `memset64` / 32 | `ncdev_memset` `:709` (`_IOC_NR` `:3310`) | `ndl_memset` `@0xc2d00` | none | `MEMSET` | HIGH |
| `MEM_GET_EXTENDED_INFO` | 29 (`:705`) | R | `mem_get_extended_info` * / 8 | `ncdev_mem_get_extended_info` `:594` (`:3286`) | (mem layer) | none | — | HIGH |
| `MEM_ALLOC_V2` | 102 (`:785`) | R | `mem_alloc_v2` * / 8 | `ncdev_mem_alloc_libnrt` `:456` (`_IOC_NR` `:3284`) | `ndl_memory_alloc_cm2` `@0xc1a00` | PID | `V2MT`, `V2MT64` | HIGH |
| `MEM_ALLOC_V2MT` | 102 (`:786`) | R | `mem_alloc_v2_mem_type` / 48 | `ncdev_mem_alloc_libnrt` `:456` (`_IOC_NR` `:3284`) | `ndl_memory_alloc` `@0xc2820` | PID | `V2`, `V2MT64` | HIGH |
| `MEM_ALLOC_V2MT64` | 102 (`:787`) | R | `mem_alloc_v2_mem_type64` / 56 | `ncdev_mem_alloc_libnrt` `:456` (`_IOC_NR` `:3284`) | `ndl_alloc_contiguous_scratchpad` `@0xc53c0` | PID | `V2`, `V2MT` | HIGH |
| `PROGRAM_ENGINE_NC` | 105 (`:796`) | RW | `program_engine_nc` * / 8 | `ncdev_program_engine_nc` `:984` (`_IOC_NR` `:3308`) | `ndl_program_engine` `@0xc2c00` | none | `PROGRAM_ENGINE_NC64` | HIGH |
| `PROGRAM_ENGINE_NC64` | 105 (`:797`) | RW | `program_engine_nc64` / 40 | `ncdev_program_engine_nc` `:984` (`_IOC_NR` `:3308`) | `ndl_program_engine` `@0xc2c00` (size-selected) | none | `PROGRAM_ENGINE_NC` | HIGH |
| `DMABUF_FD` | 107 (`:806`) | R | `dmabuf_fd` * / 24 | `ncdev_get_dmabuf_fd` `:674` (misc `:3158`) | `ndl_get_dmabuf_fd` `@0xc4ad0` | **FA** | — | HIGH |
| `MEM_COPY_ASYNC` | 108 (`:809`) | RW | `mem_copy_async` / 48 | `ncdev_mem_copy_async` `:823` (`_IOC_NR` `:3296`) | `ndl_memory_copy_as_cm2` `@0xc2e90` | PID | `MEM_COPY_ASYNC64` | HIGH |
| `MEM_COPY_ASYNC64` | 108 (`:810`) | RW | `mem_copy_async64` / 56 | `ncdev_mem_copy_async` `:823` (`_IOC_NR` `:3296`) | `ndl_memory_copy_as` `@0xc2ee0` | **none** × | `MEM_COPY_ASYNC` | HIGH |
| `MEM_COPY_ASYNC_WAIT` | 109 (`:813`) | W | `mem_copy_async_wait` / 24 | `ncdev_mem_copy_async_wait` `:903` (`:3298`) | `ndl_memory_copy_as_wait` `@0xc3020` | PID | — | HIGH |
| `MEM_MC_GET_INFO` | 111 (`:820`) | RW | `mem_get_mc_mmap_info` / 24 | `ncdev_mem_get_mc_mmap_info` `:628` (`_IOC_NR` `:3358`) | `ndl_mem_get_mc_mmap_info_v1` `@0xc1fe0` | none | `MEM_MC_GET_INFO_V2` | HIGH |
| `MEM_MC_GET_INFO_V2` | 111 (`:821`) | RW | `mem_get_mc_mmap_info_v2` / 32 | `ncdev_mem_get_mc_mmap_info` `:628` (`_IOC_SIZE` `:3358`) | `ndl_mem_get_mc_mmap_info` `@0xc4b70` | none | `MEM_MC_GET_INFO` | HIGH |
| `RESOURCE_MMAP_INFO` | 112 (`:824`) | RW | `resource_mmap_info` / 32 | `ncdev_resource_mmap_info` `:2299` (`:3360`) | `ndl_mmap_bar_region_` `@0xc1df0` | none | — | HIGH |
| `DUMP_MEM_CHUNKS` | 116 (`:835`) | R | `dump_mem_chunks` * / 24 | `ncdev_dump_mem_chunks` `:2390` (`:3364`) | `ndl_dump_device_allocation_info` `@0xc4e00` | none | — | HIGH |
| `MEM_BUF_ZEROCOPY64` | 124 (`:852`) | RW | `mem_buf_copy64zc` / 48 | `ncdev_mem_buf_zerocopy64` `:1150` (`_IOC_NR` `:3302`) | `ndl_memory_buf_zerocopy` `@0xc47c0` | none | — | HIGH |
| `MEM_BUF_ZEROCOPY64_BATCHES` | 129 (`:861`) | RW | `mem_buf_copy64zc_batches` / 32 | `ncdev_mem_buf_zerocopy64_batch` `:1242` (`:3304`) | `ndl_memory_buf_batch_copy` `@0xc5a30` | none | — | HIGH |

> **GOTCHA —** the rows marked `×` (`MEM_COPY64`, `MEM_COPY_ASYNC64`) carry gate **none** while their 32-bit base siblings carry **PID**. This is the `*64` sub-gate slip: the attach allow-list tests *exact* `cmd` values (`cmd == NEURON_IOCTL_MEM_COPY`, `:3214`; `cmd == NEURON_IOCTL_MEM_COPY_ASYNC`, `:3215`) but the dispatcher reaches the family by `_IOC_NR` (`:3294`, `:3296`), which is true for both widths. The `*64` `cmd` differs in `_IOC_SIZE`, so the exact compare is false and `npid_is_attached` is skipped — a non-`DEVICE_INIT` opener of a non-free-access fd can issue them. Blast radius is bounded by handle ownership and bounds-checking; full analysis is finding S3 on [the attack-surface page](../security/ioctl-attack-surface.md). `DMA_COPY_DESCRIPTORS64` (dma family) shares the slip.

> **QUIRK —** the `V2MT64` allocation struct (`mem_alloc_v2_mem_type64`, 56 B) is byte-identical to `V2MT` (48 B) plus a trailing `__u32 pad` (`neuron_ioctl.h:52-62`). The pad is never read by `ncdev_mem_alloc_libnrt`; it exists *solely* to bump `_IOC_SIZE` so `libnrt` can feature-probe whether the driver accepts the wider command — i.e. whether it has `>=4GB` allocation/copy support. The size column reflects this manufactured difference (8 → 48 → 56 across `V2`/`V2MT`/`V2MT64`). The same idea drives the `DRIVER_INFO` `feature_flags1` capability bitmap (misc family).

---

## Family: dma

Engine and queue lifecycle, descriptor staging, and H2T (host-to-tensor) queue management. Handlers traced in [the dma page](ioctl-dma.md); they are thin marshalling wrappers that delegate to the ring layer (`ndmar_*`) and the DMA layer (`ndma_*`). The attach gate covers `DMA_ENG_INIT`, `DMA_ENG_SET_STATE`, `DMA_QUEUE_INIT`, `DMA_ACK_COMPLETED`, `DMA_QUEUE_RELEASE`, `DMA_COPY_DESCRIPTORS` (base) (`:3211-3212`); state queries and the H2T family are ungated (still non-free-access).

| cmd name | `_IOC` nr | dir | arg struct / size | kernel handler (file:line) | `ndl_*` caller | gate | variants | Conf |
|---|---|---|---|---|---|---|---|---|
| `DMA_ENG_INIT` | 30 (`:709`) | R | `dma_eng_init` * / 8 | inline `return 0` (`:3257`, deprecated) | (none — deprecated) | PID | — | HIGH |
| `DMA_ENG_SET_STATE` | 31 (`:712`) | R | `dma_eng_set_state` * / 8 | `ncdev_dma_engine_set_state` `:103` (`:3259`) | `ndl_dma_eng_set_state` `@0xc3670` | PID | — | HIGH |
| `DMA_ENG_GET_STATE` | 32 (`:714`) | RW | `dma_eng_get_state` * / 8 | `ncdev_dma_engine_get_state` `:113` (`:3273`) | `ndl_dma_eng_get_state` `@0xc3690` | none | — | HIGH |
| `DMA_QUEUE_INIT` | 33 (`:716`) | R | `dma_queue_init` * / 8 | `ncdev_dma_queue_init` `:127` (`:3261`) | `ndl_dma_queue_init` `@0xc3860`; `ndl_dma_queue_init_batch_cm` `@0xc1ea0` | PID | — | HIGH |
| `DMA_QUEUE_RELEASE` | 34 (`:719`) | R | `dma_queue_release` * / 8 | `ncdev_dma_queue_release` `:321` (`:3269`) | `ndl_dma_queue_release` `@0xc3960` | PID | — | HIGH |
| `DMA_QUEUE_COPY_START` | 35 (`:721`) | R | `dma_queue_copy_start` * / 8 | `ncdev_dma_copy_start` `:281` (`:3265`) | `ndl_dma_queue_copy_start` `@0xc3930` | none | — | HIGH |
| `DMA_ACK_COMPLETED` | 36 (`:723`) | R | `dma_ack_completed` * / 8 | `ncdev_dma_ack_completed` `:293` (`:3267`) | `ndl_dma_ack_completed_desc` `@0xc3900` | PID | — | HIGH |
| `DMA_QUEUE_GET_STATE` | 37 (`:725`) | RW | `dma_queue_get_state` * / 8 | `ncdev_dma_queue_get_state` `:304` (`:3275`) | `ndl_dma_queue_get_state` `@0xc36c0` | none | — | HIGH |
| `DMA_COPY_DESCRIPTORS` | 38 (`:727`) | R | `dma_copy_descriptors` * / 8 | `ncdev_dma_copy_descriptors` `:211` (`_IOC_NR` `:3271`) | `ndl_dma_copy_descriptors_cm` `@0xc1cb0` | PID | `DMA_COPY_DESCRIPTORS64` | HIGH |
| `DMA_COPY_DESCRIPTORS64` | 38 (`:728`) | R | `dma_copy_descriptors64` / 40 | `ncdev_dma_copy_descriptors` `:211` (`_IOC_NR` `:3271`) | `ndl_dma_copy_descriptors` `@0xc3980` | **none** × | `DMA_COPY_DESCRIPTORS` | HIGH |
| `DMA_DESCRIPTOR_COPYOUT` | 39 (`:731`) | RW | `dma_descriptor_copyout` * / 8 | `ncdev_dma_descriptor_copyout` `:331` (`:3277`) | `ndl_dma_descriptor_copyout` `@0xc36f0` | none | — | HIGH |
| `DMA_QUIESCE_QUEUES` | 40 (`:733`) | R | `dma_quiesce_all` * / 8 | removed → `-ENOIOCTLCMD` (`:3279`) | (none) | PID | — | HIGH |
| `H2T_DMA_ALLOC_QUEUES` | 125 (`:854`) | RW | `h2t_dma_alloc_queues` / 24 | `ncdev_h2t_dma_alloc_queues` `:2920` (`:3370`) | `ndl_h2t_dma_queue_alloc` `@0xc5600` | none | — | HIGH |
| `H2T_DMA_FREE_QUEUES` | 126 (`:855`) | RW | `h2t_dma_free_queues` / 8 | `ncdev_h2t_dma_free_queues` `:2978` (`:3372`) | `ndl_h2t_dma_queue_free` `@0xc5680` | none | — | HIGH |
| `DMA_QUEUE_INIT_BATCH` | 133 (`:870`) | R | `dma_queue_init_batch` / 12296 | `ncdev_dma_queue_init_batch` `:180` (`:3263`) | `ndl_dma_queue_init_batch` `@0xc3740` | none | — | MED † |
| `GET_ASYNC_H2T_DMA_COMPL_QUEUES` | 135 (`:874`) | RW | `get_async_h2t_dma_compl_queues` / 264 | `ncdev_get_async_h2d_dma_compl_queues` `:3096` (`:3382`) | `ndl_get_async_h2t_dma_compl_queues` `@0xc5770` | none | — | HIGH |

> **GOTCHA —** `DMA_QUIESCE_QUEUES` (`nr 40`) is **listed in the attach allow-list region** but the handler is gone: `:3279` returns `-ENOIOCTLCMD` with a "no longer supported" log. A reimplementer hunting for the queue-quiesce semantics will not find a handler — it was removed. `DMA_ENG_INIT` (`nr 30`) is likewise a deprecated inline `return 0` (`:3257`) yet stays in the gate list; both are dead arms preserved for ABI stability.

> **CORRECTION (XMAP-batch) —** `DMA_QUEUE_INIT_BATCH`'s real struct is `8 + 256×48 = 12296` bytes (`MAX_DMA_QUEUE_INIT_BATCH = 256`, `neuron_ioctl.h:271`), which exceeds the 14-bit `_IOC_SIZE` field (max `0x3FFF`); the encoded size therefore *wraps*. NDL was observed issuing `0xB0084E85` and falling back to the `_cm` path on `-EINVAL` (`P1-L-NDL-03`). The kernel dispatch matches this command by exact `cmd ==` (`:3263`), so userspace and kernel agree on the same wrapped constant — the wrap self-cancels. The 12296-byte figure is the *true* `sizeof`; the *encoded* `_IOC_SIZE` is the wrapped value, hence Conf MED on the exact 32-bit hex (HIGH on the US↔K pairing).

---

## Family: nq / semaphore / event / hw-counter

Notification-queue init (V1 deprecated, V2, V2-with-realloc) plus the multiplexed semaphore, event, and hardware-counter handlers. Traced in [the nq page](ioctl-nq.md). Only the three NQ-init commands are in the attach gate (`:3217-3219`); semaphore, event, and `READ_HW_COUNTERS` are reachable on any non-free-access fd with no attach check.

| cmd name | `_IOC` nr | dir | arg struct / size | kernel handler (file:line) | `ndl_*` caller | gate | variants | Conf |
|---|---|---|---|---|---|---|---|---|
| `SEMAPHORE_INCREMENT` | 41 (`:738`) | R | `semaphore` * / 12 | `ncdev_semaphore_ioctl` `:1428` (`:3316`) | `ndl_nc_semaphore_increment` `@0xc3ba0` | none | shares handler | HIGH |
| `SEMAPHORE_DECREMENT` | 42 (`:739`) | R | `semaphore` * / 12 | `ncdev_semaphore_ioctl` `:1428` (`:3318`) | `ndl_nc_semaphore_decrement` `@0xc3bd0` | none | shares handler | HIGH |
| `SEMAPHORE_READ` | 43 (`:740`) | RW | `semaphore` * / 12 | `ncdev_semaphore_ioctl` `:1428` (`:3312`) | `ndl_nc_semaphore_read` `@0xc3c00` | none | shares handler | HIGH |
| `SEMAPHORE_WRITE` | 44 (`:741`) | R | `semaphore` * / 12 | `ncdev_semaphore_ioctl` `:1428` (`:3314`) | `ndl_nc_semaphore_write` `@0xc3c40` | none | shares handler | HIGH |
| `EVENT_SET` | 45 (`:742`) | R | `event` * / 12 | `ncdev_events_ioctl` `:1457` (`:3322`) | `ndl_nc_event_set` `@0xc3cb0` | none | shares handler | HIGH |
| `EVENT_GET` | 46 (`:743`) | RW | `event` * / 12 | `ncdev_events_ioctl` `:1457` (`:3320`) | `ndl_nc_event_get` `@0xc3c70` | none | shares handler | HIGH |
| `NOTIFICATIONS_INIT_V1` | 51 (`:747`) | R | `notifications_init_v1` * / 24 | `ncdev_nc_nq_init_deprecated` `:1996` (`:3332`) | (legacy) | PID | `_V2`, `_REALLOC_V2` | HIGH |
| `NOTIFICATIONS_DESTROY_V1` | 52 (`:748`) | R | `notifications_destroy` * / 8 | inline `return 0` (`:3338`) | `ndl_notification_destroy` `@0xc40d0` (munmap) | none | — | HIGH |
| `NOTIFICATIONS_INIT_V2` | 53 (`:749`) | R | `notifications_init_v2` * / 48 | `ncdev_nc_nq_init_libnrt` `:2014` (`:3334`) | `ndl_notification_init` `@0xc3ce0` | PID | `_V1`, `_REALLOC_V2` | HIGH |
| `NOTIFICATIONS_INIT_WITH_REALLOC_V2` | 54 (`:750`) | R | `notifications_init_with_realloc_v2` * / 52 | `ncdev_nc_nq_init_with_realloc_libnrt` `:2045` (`:3336`) | `ndl_notification_init_with_realloc` `@0xc3ea0` | PID | `_V1`, `_V2` | HIGH |
| `NOTIFICATIONS_QUEUE_INFO` | 58 (`:752`) | R | `notifications_queue_info` * / 12 | inline `return -1` (`:3340`, stub) | (none) | none | — | HIGH |
| `READ_HW_COUNTERS` | 61 (`:755`) | R | `read_hw_counters` * / 20 | `ncdev_read_hw_counters` `:1694` (`:3342`) | `ndl_read_hw_counters` `@0xc3630` | none | — | HIGH |

> **NOTE —** the `EVENT_SET`/`EVENT_GET` macros are encoded against `struct neuron_ioctl_semaphore *` (`:742-743`) but the handler `copy_from_user`s into `struct neuron_ioctl_event` (`:1462`). The two are layout-identical (`3 × __u32`), so the wire form matches; the handler simply uses a clearer struct name. `SEMAPHORE_READ`/`EVENT_GET` are `_IOWR` (they read a value back); `INCREMENT`/`DECREMENT`/`WRITE`/`SET` and the NQ-init + `READ_HW_COUNTERS` are encoded `_IOR` despite writing back via explicit `copy_to_user` — the driver dispatches on the full `cmd`, not `_IOC_DIR`.

> **GOTCHA —** `NOTIFICATIONS_QUEUE_INFO` (`nr 58`) returns a raw `-1` (`:3340`), which surfaces to userspace as `-EPERM`, not `-ENOSYS` — a reimplementer probing for "unsupported" on this command sees a permission error, not an unimplemented one. `NOTIFICATIONS_DESTROY_V1` (`nr 52`) is an inline `return 0`: NQ teardown is implicit on reset/flush, and `libnrt`'s `ndl_notification_destroy` does the userspace `munmap` of the ring rather than relying on a kernel destroy.

---

## Family: pod

UltraServer / pod membership query and control. All three commands dispatch on the **free-access** misc lane (`:3173-3178`), so they are reachable on an `O_WRONLY` fd with no attach gate. Handlers traced in [the pod/misc page](ioctl-pod.md); they thunk to the pod-election engine `ndhal->ndhal_npe.npe_*`. `POD_STATUS` and `POD_CTRL` are V1/V2 size-overloaded.

| cmd name | `_IOC` nr | dir | arg struct / size | kernel handler (file:line) | `ndl_*` caller | gate | variants | Conf |
|---|---|---|---|---|---|---|---|---|
| `POD_INFO` | 121 (`:844`) | RW | `pod_info` / 260 | `ncdev_pod_info` `:2804` (`_IOC_NR` misc `:3173`) | `ndl_pod_info` `@0xc4f50` | **FA** | — | HIGH |
| `POD_STATUS` | 122 (`:846`) | RW | `pod_status` / 268 | `ncdev_pod_status` `:2832` (`_IOC_NR` misc `:3175`) | `ndl_pod_mapping_info` `@0xc5080` | **FA** | `POD_STATUS_V2` | HIGH |
| `POD_STATUS_V2` | 122 (`:847`) | RW | `pod_status_v2` / 276 | `ncdev_pod_status` `:2832` (`cmd ==` misc `:3175`) | `ndl_pod_election_state` `@0xc4ff0`; `ndl_pod_status` `@0xc51b0` | **FA** | `POD_STATUS` | HIGH |
| `POD_CTRL` | 123 (`:849`) | RW | `pod_ctrl` / 16 | `ncdev_pod_ctrl` `:2873` (`_IOC_NR` misc `:3177`) | `ndl_pod_ctrl` `@0xc5110` (v1) | **FA** | `POD_CTRL_V2` | HIGH |
| `POD_CTRL_V2` | 123 (`:850`) | RW | `pod_ctrl_v2` / 20 | `ncdev_pod_ctrl` `:2873` (`cmd ==` misc `:3177`) | `ndl_pod_ctrl` `@0xc5110` (v2) | **FA** | `POD_CTRL` | HIGH |

> **GOTCHA —** `POD_CTRL` is **state-changing on the ungated free-access lane**. It thunks to `npe_pod_ctrl` (`:2902`), which can trigger a pod-election state transition (`ctrl ∈ {REQ_POD, REQ_SINGLE_NODE, REQ_KILL, SET_MODE}`, `neuron_driver_shared.h:38`). An `O_WRONLY` opener that never attached can drive pod control. The V1 path forces `mode = NEURON_ULTRASERVER_MODE_UNSET` (`:2886`); V2 carries an explicit `mode`. This is finding S5/S8-adjacent on [the attack-surface page](../security/ioctl-attack-surface.md). The V1/V2 split is resolved by `_IOC_SIZE` (`static_assert(POD_CTRL != POD_CTRL_V2)`, `:2875`), and the handlers `memset` the arg after `copy_from_user` then only honour `.sz` — all other `[in]` fields of `POD_INFO`/`POD_STATUS` are discarded by design.

---

## Family: misc / info / lifecycle

Device info, version negotiation, BDF, topology discovery, CRWL core reservation, PRINTK, datastore acquire/release, the metric/power/HBM commands, and the deprecated/stub set. This is the largest and most heterogeneous family; gate tier varies per command. The free-access (`FA`) members are the topology/discovery/diagnostic subset the misc lane was built for; the rest run on the main path (`none`), with a handful PID-gated.

| cmd name | `_IOC` nr | dir | arg struct / size | kernel handler (file:line) | `ndl_*` caller | gate | variants | Conf |
|---|---|---|---|---|---|---|---|---|
| `DEVICE_RESET` | 1 (`:659`) | NONE | — | `ncdev_device_reset_deprecated` `:1744` (`:3227`) | (none — deprecated) | none | — | HIGH |
| `DEVICE_READY` | 2 (`:660`) | R | `__u8` / 1 | `ncdev_device_ready_deprecated` `:1772` (`:3229`) | (legacy) | none | order vs `RESET_STATUS` | HIGH |
| `DEVICE_INFO` | 3 (`:663`) | R | `device_info` * / 80 | `ncdev_device_info` `:1845` (`:3247`) | `ndl_get_board_info` fallback `@0xc4980` | none | — | HIGH |
| `DEVICE_INIT` | 4 (`:666`) | R | `device_init` * / 8 | inline `return 0` (`:3249`, deprecated) | (none) | none | — | HIGH |
| `DEVICE_RELEASE` | 5 (`:667`) | NONE | — | inline `return 0` (`:3251`, deprecated) | (none) | none | — | HIGH |
| `DEVICE_APP_PID` | 6 (`:670`) | R | `__s32` / 4 | `ncdev_app_pid_deprecated` `:1926` (`:3253`) | (none observed) | none | — | HIGH |
| `DEVICE_GET_ALL_APPS_INFO` | 7 (`:671`) | R | `get_apps_info` * / 8 | `ncdev_get_all_apps_info` `:1931` (`:3255`) | `ndl_get_all_apps_info` `@0xc3ad0` | none | — | HIGH |
| `BAR_READ` | 11 (`:674`) | R | `bar_rw` * / 32 | `ncdev_bar_rw(.., true)` `:1622` (`:3324`) | `ndl_bar_read` `@0xc3590` | none | — | HIGH |
| `BAR_WRITE` | 12 (`:676`) | W | `bar_rw` * / 32 | `ncdev_bar_rw(.., false)` (`:3326`) | `ndl_bar_write` `@0xc35e0` | PID | — | HIGH |
| `POST_METRIC` | 13 (`:678`) | W | `post_metric` * / 8 | `ncdev_post_metric` `:1657` (`:3328`) | (metrics layer) | PID | — | HIGH |
| `ACQUIRE_NEURON_DS` | 71 (`:758`) | R | `neuron_ds_info` * / 8 | `ncdev_acquire_neuron_ds` `:2076` (`:3344`) | `ndl_nds_open` `@0xc4110` | none | — | HIGH |
| `RELEASE_NEURON_DS` | 72 (`:759`) | R | `neuron_ds_info` * / 8 | `ncdev_release_neuron_ds` `:2098` (`:3346`) | `ndl_nds_close` `@0xc41b0` | none | — | HIGH |
| `CRWL_READER_ENTER` | 81 (`:762`) | W | `crwl` * / 8 | `ncdev_crwl_reader_enter` `:2109` (`:3348`) | `ndl_crwl_reader_enter` `@0xc4200` | none | — | HIGH |
| `CRWL_READER_EXIT` | 82 (`:763`) | W | `crwl` * / 8 | `ncdev_crwl_reader_exit` `:2121` (`:3350`) | `ndl_crwl_reader_exit` `@0xc4240` | none | — | HIGH |
| `CRWL_WRITER_ENTER` | 83 (`:764`) | W | `crwl` * / 8 | `ncdev_crwl_writer_enter` `:2133` (`:3352`) | `ndl_crwl_writer_enter` `@0xc4280` | none | — | HIGH |
| `CRWL_WRITER_DOWNGRADE` | 84 (`:765`) | W | `crwl` * / 8 | `ncdev_crwl_writer_downgrade` `:2145` (`:3354`) | `ndl_crwl_writer_downgrade` `@0xc42c0` | none | — | HIGH |
| `CRWL_NC_RANGE_MARK` | 85 (`:766`) | W | `crwl_nc_map` * / 8 | `ncdev_crwl_nc_range_mark` `:2163` (misc `:3148`) | `ndl_crwl_nc_range_mark` `@0xc4300` | **FA** | `_EXT0` | HIGH |
| `CRWL_NC_RANGE_MARK_EXT0` | 85 (`:767`) | RW | `crwl_nc_map_ext` / 80 | `ncdev_crwl_nc_range_mark` `:2163` (misc `:3148`) | `ndl_crwl_nc_range_mark` `@0xc4300` | **FA** | `MARK` | HIGH |
| `CRWL_NC_RANGE_UNMARK` | 86 (`:768`) | W | `crwl_nc_map` * / 8 | `ncdev_crwl_nc_range_unmark` `:2220` (misc `:3150`) | `ndl_crwl_nc_range_unmark` `@0xc4420` | **FA** | `_EXT0` | HIGH |
| `CRWL_NC_RANGE_UNMARK_EXT0` | 86 (`:769`) | W | `crwl_nc_map_ext` / 80 | `ncdev_crwl_nc_range_unmark` `:2220` (misc `:3150`) | `ndl_crwl_nc_range_unmark` `@0xc4420` | **FA** | `UNMARK` | HIGH |
| `CINIT_SET_STATE` | 91 (`:772`) | W | `cinit_set` * / 8 | `ncdev_cinit_set_state` `:2256` (`:3243`) | `ndl_nc_init_set_state` `@0xc4900` | none | — | HIGH |
| `NC_MODEL_STARTED_COUNT` | 92 (`:773`) | W | `nc_model_started_count` * / 8 | `ncdev_nc_model_started_count` `:2273` (`:3245`) | `ndl_nc_model_started_count` `@0xc4940` | none | — | HIGH |
| `COMPATIBLE_VERSION` | 93 (`:776`) | W | `compatible_version` * / 8 | `ncdev_compatible_version` `:2321` (misc `:3152`) | `ndl_get_compatible_version` `@0xc2770` | **FA** | — | HIGH |
| `DEVICE_BASIC_INFO` | 100 (`:779`) | W | `device_basic_info` * / 8 | `ncdev_device_basic_info` `:1790` (misc `:3154`) | `ndl_get_board_info` `@0xc4980` | **FA** | — | HIGH |
| `DEVICE_BDF` | 101 (`:782`) | R | `device_bdf` * / 8 | `ncdev_device_bdf` `:1797` (`:3356`) | `ndl_get_device_bdf` `@0xc4a80` | none | — | HIGH |
| `NC_RESET` | 103 (`:790`) | R | `device_reset` * / 8 | `ncdev_nc_reset` `:1726` (`:3241`) | `ndl_reset_ncs` `@0xc3a50` | none | — | HIGH |
| `NC_RESET_READY` | 104 (`:793`) | R | `device_ready` * / 8 | `ncdev_nc_reset_ready` `:1757` (`:3237`) | `ndl_ready_ncs` `@0xc3a90` | none | — | HIGH |
| `DEVICE_RESET_STATUS` | 106 (`:800`) | R | `__u8` / 1 | `ncdev_reset_status_deprecated` `:1751` (`:3239`) | (none — deprecated) | none | `BDF_EXT` (NR-collision) | HIGH |
| `DEVICE_BDF_EXT` | 106 (`:803`) | R | `device_bdf_ext` * / 8 | `ncdev_device_bdf_ext` `:1806` (misc `:3156`) | `ndl_get_device_bdf_ext` `@0xc4b00` | **FA** | `RESET_STATUS` (NR-collision) | HIGH |
| `DRIVER_INFO_GET` | 110 (`:816`) | R | `device_driver_info` / 24 | `ncdev_driver_info` `:1896` (`_IOC_NR` misc `:3163`) | `ndl_feature_supported` `@0xc22d0` | **FA** | `SET` (`_IOC_DIR`) | HIGH |
| `DRIVER_INFO_SET` | 110 (`:817`) | W | `device_driver_info` / 24 | `ncdev_driver_info` `:1896` (→ `-ENOTSUPP`) | (none observed) | PID † | `GET` (`_IOC_DIR`) | HIGH |
| `PRINTK` | 113 (`:827`) | W | `printk` / 16 | `ncdev_printk` `:2333` (misc `:3165`) | `ndl_printk` `@0xc4ca0`; `ndl_printk_cm` `@0xc1960` | **FA** | — | HIGH |
| `HOST_DEVICE_ID` | 114 (`:830`) | R | `host_device_id` / 4 | `ncdev_get_host_device_id` `:2362` (`:3362`) | `ndl_get_host_device_id` `@0xc4d20` | none | — | HIGH |
| `HOST_DEVICE_ID_TO_RID_MAP` | 115 (`:833`) | RW | `host_device_id_to_rid_map` / 260 | `ncdev_host_device_id_to_rid_map` `:2376` (`_IOC_NR` misc `:3167`) | `ndl_get_host_device_id_to_rid_map` `@0xc4d50` | **FA** | — | HIGH |
| `NC_PID_STATE_DUMP` | 117 (`:837`) | RW | `nc_pid_state_dump` / 16 | `ncdev_nc_pid_state_dump` `:2454` (misc `:3169`) | `ndl_dump_nc_pid_info` `@0xc4e50` | **FA** | — | HIGH |
| `HBM_SCRUB_START` | 118 (`:839`) | RW | `hbm_scrub_start` / 16 | `ncdev_hbm_scrub_start` `:2498` (`:3366`) | `ndl_hbm_scrub_start` `@0xc4e90` | none | — | HIGH |
| `HBM_SCRUB_WAIT` | 119 (`:840`) | RW | `hbm_scrub_wait` / 8 | `ncdev_hbm_scrub_wait_for_cmpl` `:2681` (`:3368`) | `ndl_hbm_scrub_wait` `@0xc4f10` | none | — | HIGH |
| `GET_LOGICAL_TO_PHYSICAL_NC_MAP` | 120 (`:842`) | RW | `get_logical_to_physical_nc_map` / 16 | `ncdev_logical_to_physical_nc_map` `:2753` (misc `:3171`) | `ndl_get_logical_to_physical_nc_map` `@0xc27b0` | **FA** | — | HIGH |
| `POWER_PROFILE` | 127 (`:857`) | W | `power_profile` / 8 | `ncdev_power_profile_set` `:3005` (`:3374`) | `ndl_set_performance_profile` `@0xc58c0` | none | — | HIGH |
| `METRICS_CTRL` | 128 (`:859`) | W | `metrics_ctrl` / 4 | `ncdev_metric_ctrl` `:1682` (`:3330`) | `ndl_metrics_ctrl` `@0xc5a00` | none | — | HIGH |
| `THROTTLING_NOTIFICATIONS` | 130 (`:863`) | W | `throttling_notifications` / 4 | `ncdev_throttling_notifications_set` `:3063` (`:3378`) | `ndl_enable_throttling_notifications` `@0xc5950` | none | — | HIGH |
| `GET_VA_PLACEMENT` | 131 (`:865`) | W | `get_va_placement` / 16 | `ncdev_get_va_placement` `:3075` (misc `:3179`) | `ndl_get_va_placement` `@0xc5af0` | **FA** | — | HIGH |
| `GET_PERFORMANCE_PROFILE` | 132 (`:867`) | RW | `power_profile` / 8 | `ncdev_power_profile_get` `:3023` (`:3376`) | `ndl_get_performance_profile` `@0xc5900` | none | — | HIGH |
| `AVAILABLE_PERF_PROFILES` | 134 (`:872`) | RW | `available_perf_profiles` / 36 | `ncdev_available_perf_profiles` `:3046` (`:3380`) | `ndl_get_supported_profiles` `@0xc5980` | none | — | HIGH |

> **QUIRK —** `nr 106` is a genuine `_IOC_NR` collision: `DEVICE_RESET_STATUS` (`__u8`, size 1, `:800`) and `DEVICE_BDF_EXT` (ptr, size 8, `:803`) share it. They are disambiguated by **both** `_IOC_SIZE` (different `cmd` constants) **and** access-mode lane: `RESET_STATUS` is matched by exact `cmd` on the main path (`:3239`), `BDF_EXT` by exact `cmd` on the misc lane (`:3156`). Separately, `DEVICE_READY` (`nr 2`) must be tested *before* `RESET_STATUS` (`:3229` ahead of `:3239`) because older drivers assigned both to `ioctl 2`; the dispatch *order* preserves the legacy contract. Neither collision is ambiguous as shipped — see [dispatch §3](ioctl-dispatch.md#scheme-a--_ioc_nr-collisions-order--and-lane-resolved).

> **NOTE —** `DRIVER_INFO` (`nr 110`) is the lone command keyed on `_IOC_DIR`. `GET` (`_IOR`, `:816`) is dispatched in the misc lane by `_IOC_NR` (`:3163`) and so is reachable free-access; `SET` (`_IOW`, `:817`) is in the attach allow-list (`:3218`) on the main path, but `ncdev_driver_info` returns `-ENOTSUPP` for the write direction (`:1902-1903`) — so `SET` is effectively unimplemented regardless of lane. The `†` on the gate marks this dir-demux: a `SET`-form `cmd` arriving on a free-access fd reaches `ncdev_driver_info` by `_IOC_NR` and is rejected by `_IOC_DIR`. The `GET` response advertises `feature_flags1 = 0x1FF` (all nine capability bits, `:1912-1916`) with `version = 0`.

> **GOTCHA —** the `FA` rows in this family are the free-access lane's full membership: `CRWL_NC_RANGE_MARK`/`UNMARK` (+`_EXT0`), `COMPATIBLE_VERSION`, `DEVICE_BASIC_INFO`, `DEVICE_BDF_EXT`, `DMABUF_FD` (mem family), `DRIVER_INFO_GET`, `PRINTK`, `HOST_DEVICE_ID_TO_RID_MAP`, `NC_PID_STATE_DUMP`, `GET_LOGICAL_TO_PHYSICAL_NC_MAP`, `POD_INFO`/`STATUS`/`CTRL` (pod family), `GET_VA_PLACEMENT`. Most are read-only topology/info, but `CRWL_NC_RANGE_MARK`/`UNMARK` mutate the device-spanning core-reservation map and `PRINTK` carries the size-0 OOB stack-read bug (finding S1). A reimplementation that treats "`O_WRONLY` ⇒ harmless query" is wrong — see [attack-surface §1](../security/ioctl-attack-surface.md#1-at-a-glance--the-gate-lanes-that-make-findings-reachable).

---

## Cross-References

- [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) — the dispatch mechanism this catalogue tabulates: the three-tier gate model, `_IOC_NR`/`_IOC_SIZE`/`_IOC_DIR` overload resolution, and the `*64` sub-gate slip (not re-derived here)
- [Memory IOCTL Handlers](ioctl-mem.md) — per-handler detail for the mem family: `mem_chunk` lifecycle, the 2-level handle table, `mc_access_is_within_bounds`, and the BAR4 zero-copy path
- [DMA IOCTL Handlers](ioctl-dma.md) — per-handler detail for the dma family: ring/queue lifecycle, descriptor staging, and the H2T queue-management commands
- [NQ and Semaphore IOCTL Handlers](ioctl-nq.md) — per-handler detail for NQ init (V1/V2/realloc), the multiplexed semaphore/event handlers, and `READ_HW_COUNTERS`
- [Pod and Misc IOCTL Handlers](ioctl-pod.md) — per-handler detail for the pod family and the misc info/version/BDF/topology commands
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security projection of this catalogue: which gate tier admits each finding (the `*64` slip S3, the ungated free-access state-changers S5/S8, the `PRINTK` OOB read S1)
