# DMA IOCTL Handlers

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The handler bodies are read from `neuron_cdev.c` (4043 lines), the arg structs and command macros from `neuron_ioctl.h` (876 lines), the queue-type / async-handle enums and the state structs returned to userspace from `share/neuron_driver_shared.h`, and the DMA constants (`MAX_DMA_DESC_SIZE`, `DMA_MAX_Q_MAX`, `union udma_desc`) from `udma/udma.h`. The source is read directly, not reverse-engineered; every constant and offset is a `#define`, a struct field, or a literal line. Other driver versions renumber lines and add/remove commands. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

The DMA `ioctl` family is the userspace control plane for the AnnapurnaLabs/Alpine UDMA micro-DMA engines: `libnrt` builds descriptor rings in device or host memory, then drives every stage of a transfer — engine state, queue lifecycle, descriptor staging, the *trigger*, and completion acknowledgement — through `unlocked_ioctl` on `/dev/neuronN`. Sixteen commands fall into **five functional groups**, all routed by `ncdev_ioctl` (`neuron_cdev.c:3188`): (A) **engine state** (`DMA_ENG_INIT`/`SET_STATE`/`GET_STATE`), (B) **queue lifecycle** (`DMA_QUEUE_INIT`, `DMA_QUEUE_INIT_BATCH`, `DMA_QUEUE_COPY_START`, `DMA_ACK_COMPLETED`, `DMA_QUEUE_GET_STATE`, `DMA_QUEUE_RELEASE`, and the removed `DMA_QUIESCE_QUEUES`), (C) **descriptor I/O** (`DMA_COPY_DESCRIPTORS`/`+64`, `DMA_DESCRIPTOR_COPYOUT`), (D) **H2T queue management** (`H2T_DMA_ALLOC_QUEUES`/`FREE_QUEUES`, `GET_ASYNC_H2T_DMA_COMPL_QUEUES`), and (E) the **`dmabuf` export** (`DMABUF_FD`, reachable only on the free-access misc lane).

The defining property of this family is that the handlers contain **no ring logic**. Each is a thin marshalling wrapper: `copy_from_user(arg)` → validate (`nc_id` range, descriptor-count bounds, `mem_handle → mem_chunk` resolution) → a single call into the ring layer (`ndmar_*`, [dma-rings](dma-rings.md)) or the DMA op layer (`ndma_*`, [dma-op-layer](dma-op-layer.md)) → `copy_to_user` for any out-fields. The descriptor wire format, the chunked submit, the host-marker completion poll, and the doorbell all live below this boundary; this page documents how each `ioctl` *marshals into* that layer and never re-derives the layer itself. The arg structs are the contract a reimplementer packs; the validation gates are the only thing standing between attacker-controlled `eng_id`/`qid`/`offset`/`num_descs` and the hardware.

The pivot a reimplementer must internalize is the **trigger bridge**: `DMA_QUEUE_COPY_START` (`nr 35`, `cmd 0x80084E23`). All the other group-B/C commands build and inspect a ring that userspace has already filled with descriptors via mmap and `DMA_COPY_DESCRIPTORS`; `COPY_START` is the one that *arms* the queue — `ncdev_dma_copy_start` (`:281`) thunks straight to `ndmar_queue_copy_start` (`neuron_ring.c:309`), which calls `udma_m2m_copy_start` to ring the per-queue doorbell (`drtp_inc` at MMIO `+0x38`, owned by [udma-main](udma-main.md)). It is the userspace-driven submission path — distinct from the kernel-internal `MEM_COPY*`/`ndma_memcpy*` path the [mem family](ioctl-mem.md) uses, which builds *and* triggers in one kernel call. A reimplementer wiring a runtime drives this command once per batch, after `DMA_COPY_DESCRIPTORS`, to hand the descriptors to silicon.

For reimplementation, the contract is:

- **The five groups and their boundary calls** — each handler's exactly-one delegation: engine-state → `ndmar_eng_*`, queue-lifecycle → `ndmar_queue_*`/`ndmar_ack_completed`, descriptor-I/O → `ndma_memcpy_dma_copy_descriptors`/`ndma_memcpy`, H2T-mgmt → `ndmar_h2t_ring_request`/`_release`, `dmabuf` → `ndmabuf_get_fd`.
- **The trigger bridge** — `DMA_QUEUE_COPY_START` is the userspace ring-arm/doorbell command; `tx_desc_count`/`rx_desc_count` are the submission-ring advance counts handed to `udma_m2m_copy_start`.
- **The per-handler arg/validate/dispatch shape** — each copies a fixed struct, resolves handles or range-checks counts, then issues one boundary call; the size-overloaded `DMA_COPY_DESCRIPTORS64` demuxes struct width first.
- **The two staging paths** — `DMA_COPY_DESCRIPTORS` bounces a user descriptor buffer through a `MC_LIFESPAN_LOCAL` host chunk in ≤64 KiB chunks (`:258-277`); `DMA_DESCRIPTOR_COPYOUT` reads HW-ring descriptors back, DMA-pulling device-resident rings through a `kmalloc` buffer (`:391-409`).

| | |
|---|---|
| **Handler region** | `neuron_cdev.c:103-414` (groups A/B/C), `:674` (`dmabuf`), `:2920-3144` (group D) |
| **Dispatcher** | `ncdev_ioctl` (`:3188`); DMA branches `:3257-3383`; `dmabuf` on misc lane `ncdev_misc_ioctl` (`:3158-3162`) |
| **Trigger bridge** | `DMA_QUEUE_COPY_START` `nr 35` (`cmd 0x80084E23`) → `ncdev_dma_copy_start` `:281` → `ndmar_queue_copy_start` (`neuron_ring.c:309`) → `udma_m2m_copy_start` (doorbell) |
| **Handle → mc** | `ncdev_mem_handle_to_mem_chunk` `:75` (+ `MEMCHUNK_MAGIC 0xE1C2D3F4` check `:79`) — [ioctl-mem](ioctl-mem.md) |
| **Bounds gate** | `mc_access_is_within_bounds` (`neuron_mempool.h:225`, overflow-safe) — descriptor staging only |
| **`nc_id` gate** | `arg.nc_id >= ndhal_address_map.nc_per_device → -E2BIG` (`:2931`, `:2988`, `:3113`) |
| **Stage chunk** | `MAX_DMA_DESC_SIZE = 65536` (`udma/udma.h:479`); `sizeof(union udma_desc) = 16` (`:337`) |
| **Batch cap** | `MAX_DMA_QUEUE_INIT_BATCH = 256` (`neuron_ioctl.h:271`); arg struct = `4 + 256×48 = 12296` B |
| **H2T queue cap** | `DMA_MAX_Q_MAX = DMA_MAX_Q_V4 = 16` (`udma/udma.h:12-13`) |
| **Boundary owners** | ring lifecycle → [dma-rings](dma-rings.md); op layer → [dma-op-layer](dma-op-layer.md); doorbell → [udma-main](udma-main.md) |
| **Confidence** | HIGH — every handler body, arg struct, command macro, and dispatch line read verbatim from the shipped source |

---

## 1. The Marshalling Shape and the Trigger Bridge

Every handler in this family is the same three-beat: copy the fixed arg struct, validate, issue one boundary call. There is no descriptor construction, no ring-pointer arithmetic, no doorbell write inside `neuron_cdev.c` — those are all below the `ndmar_*`/`ndma_*`/`udma_*` boundary. What the handlers *own* is the userspace ABI (the arg structs), the validation gates, and the demux for the size-overloaded commands.

The group-B/C commands form a small state machine over one UDMA queue, driven from userspace:

```text
DMA_QUEUE_INIT        bind tx/rx/rxc mem_chunks into ring eng_id:qid   (ndmar_queue_init)
  │                   (or DMA_QUEUE_INIT_BATCH — up to 255 binds in one call)
  ▼
DMA_COPY_DESCRIPTORS  stage user-built descriptors into the ring MC    (ndma_memcpy_dma_copy_descriptors)
  │
  ▼
DMA_QUEUE_COPY_START  ── arm the queue, ring the doorbell ──           (ndmar_queue_copy_start → udma_m2m_copy_start)   ◄── THE TRIGGER
  │
  ▼
DMA_QUEUE_GET_STATE   poll hw/sw status, head/tail pointers            (ndmar_queue_get_state)
  │  DMA_DESCRIPTOR_COPYOUT  read the HW ring descriptors back         (ndmar_queue_get_descriptor_info + ndma_memcpy)
  ▼
DMA_ACK_COMPLETED     advance the completion counter, recycle descs    (ndmar_ack_completed)
  ▼
DMA_QUEUE_RELEASE     drop the queue                                   (ndmar_queue_release — a near no-op, see GOTCHA)
```

> **QUIRK —** `DMA_QUEUE_COPY_START` is the *only* command in this family that hands work to silicon. Everything else builds, binds, inspects, or tears down. `ncdev_dma_copy_start` (`:281`) is nine lines: `copy_from_user` a 16-byte `{eng_id, qid, tx_desc_count, rx_desc_count}`, then `ndmar_queue_copy_start(nd, eng_id, qid, tx_desc_count, rx_desc_count)` (`:289`). The ring layer acquires the engine, calls `udma_m2m_copy_start(&eng->udma, qid, tx_desc_count, rx_desc_count)` (`neuron_ring.c:321`) — which is the doorbell — and releases. A reimplementer's runtime mmaps the ring, writes 16-byte descriptors, issues `DMA_COPY_DESCRIPTORS` (or writes them directly through the mmap), then issues `COPY_START` to advance the submission-ring tail and fire the engine. There is no separate "doorbell" ioctl; `COPY_START` *is* the doorbell from userspace's perspective.

> **GOTCHA —** `DMA_QUEUE_RELEASE` (`nr 34`) looks like a teardown counterpart to `DMA_QUEUE_INIT`, but `ndmar_queue_release` (`neuron_ring.c:338`) is a trace-and-`return 0` stub — it frees nothing. Queue resources are reclaimed at process-exit/reset, not on this ioctl. A reimplementer must not assume `RELEASE` returns a queue to a free pool; the queue stays bound until the owning process tears the device down ([dma-rings](dma-rings.md) owns the actual lifecycle). The command exists for ABI symmetry, and it is *PID-gated* (in the attach allow-list, `:3212`) despite doing nothing.

### Handle resolution and the bounds gate

The descriptor-staging and engine-binding handlers resolve a userspace `u64` mem handle to a kernel `mem_chunk *` through the same helper the mem family uses (`ncdev_mem_handle_to_mem_chunk`, `:75`), which layers a `MEMCHUNK_MAGIC` check (`:79`) on the 2-level handle-table lookup — the handle table and its free-list/poisoning internals are owned by [ioctl-mem](ioctl-mem.md). `DMA_QUEUE_INIT` resolves *three* optional handles (tx/rx/rxc), passing `NULL` for any zero handle (`:139-150`); the lifecycle commands (`COPY_START`, `ACK_COMPLETED`, `GET_STATE`, `RELEASE`) carry no handle at all — they address the queue by `(eng_id, qid)` and never touch memory directly. The `mc_access_is_within_bounds` gate (overflow-safe, `neuron_mempool.h:225`) appears only on the descriptor-staging path (`DMA_COPY_DESCRIPTORS`, `:253`), where a user-supplied `offset`/`num_descs` indexes into a ring `mem_chunk`.

---

## 2. Group A — Engine State

### Purpose

Set and read the per-engine TX/RX run state and capability metadata. `DMA_ENG_INIT` is a deprecated no-op (`return 0` inline at dispatch, `:3257`); `SET_STATE`/`GET_STATE` thunk to the ring layer's engine-state accessors.

### Algorithm

```c
// neuron_cdev.c:103 — set one engine's run state
function ncdev_dma_engine_set_state(nd, param):
    copy_from_user(&arg, param, sizeof(dma_eng_set_state))   // :107  { u32 eng_id, u32 state }
    return ndmar_eng_set_state(nd, arg.eng_id, arg.state)    // :110  → dma-rings

// neuron_cdev.c:113 — read engine state back to userspace
function ncdev_dma_engine_get_state(nd, param):
    copy_from_user(&arg, param, sizeof(dma_eng_get_state))   // :118  { u32 eng_id, struct *state }
    ndmar_eng_get_state(nd, arg.eng_id, &state)              // :121  fills neuron_dma_eng_state
    return copy_to_user(arg.state, &state, sizeof(state))    // :124  out via user pointer
```

Neither handler range-checks `eng_id` in `neuron_cdev.c`; validation against `NUM_DMA_ENG_PER_DEVICE` (132, `neuron_ring.h:13`) is the ring layer's responsibility ([dma-rings](dma-rings.md)). The state returned by `GET_STATE` is `struct neuron_dma_eng_state` (`share/neuron_driver_shared.h:73`): `{revision_id, max_queues, num_queues, tx_state, rx_state}` — five `__u32`, 20 bytes.

---

## 3. Group B — Queue Lifecycle

### Purpose

Bind `mem_chunk` rings into a UDMA queue, trigger the transfer, acknowledge completion, read queue status, and (nominally) release. This group carries the trigger bridge (`COPY_START`) and the batch-init fast path.

### Algorithm — the representative handler

`DMA_QUEUE_INIT_BATCH` is the most instructive handler in the family: it is the only one that `kmalloc`s its arg (because the struct is 12296 bytes — far too large for the stack), it loops, and it shows the bind path that `DMA_QUEUE_INIT` runs once:

```c
// neuron_cdev.c:180 — bind up to 255 queues in one ioctl
function ncdev_dma_queue_init_batch(nd, param):
    arg = kmalloc(sizeof(dma_queue_init_batch))             // :182  12296 B — heap, not stack
    if !arg: return -ENOMEM                                 // :185
    copy_from_user(arg, param, sizeof(*arg))               // :189  whole 12 KiB batch in one copy
    if (copy failed): { ret = -EACCES; goto done }         // :190-193  note: -EACCES, not -EFAULT
    if arg->count >= MAX_DMA_QUEUE_INIT_BATCH:             // :195  count must be < 256 (strict)
        { ret = -E2BIG; goto done }
    for i in 0 .. arg->count-1:                            // :201
        ret = ncdev_dma_queue_init_batch_entry(nd, &arg->entries[i])   // :202  per-entry bind
        if ret: goto done                                  // :203  first failure aborts the batch
done:
    kfree(arg)                                             // :207
    return ret

// neuron_cdev.c:156 — one entry: resolve 3 handles, bind the ring (no copy_from_user — arg in kernel)
function ncdev_dma_queue_init_batch_entry(nd, arg):
    rx_mc  = arg->rx_handle  ? ncdev_mem_handle_to_mem_chunk(nd, arg->rx_handle)  : NULL   // :163-166
    tx_mc  = arg->tx_handle  ? ncdev_mem_handle_to_mem_chunk(nd, arg->tx_handle)  : NULL   // :167-170
    rxc_mc = arg->rxc_handle ? ncdev_mem_handle_to_mem_chunk(nd, arg->rxc_handle) : NULL   // :171-174
    return ndmar_queue_init(nd, arg->eng_id, arg->qid,                                     // :175  → dma-rings
                            arg->tx_desc_count, arg->rx_desc_count,
                            tx_mc, rx_mc, rxc_mc, arg->axi_port, false)
```

The single-shot `DMA_QUEUE_INIT` (`:127`) is `ncdev_dma_queue_init_batch_entry` plus a `copy_from_user` of one 48-byte entry — identical bind, same trailing `false` argument to `ndmar_queue_init`. That `false` selects the non-H2T binding path in the ring layer (the H2T memcpy rings the driver owns are built by a different path, [dma-rings](dma-rings.md)); its exact meaning is owned there and not re-derived here.

The trigger and the completion-ack are both small thunks:

```c
// neuron_cdev.c:281 — THE TRIGGER: arm queue eng_id:qid, ring the doorbell
function ncdev_dma_copy_start(nd, param):
    copy_from_user(&arg, param, sizeof(dma_queue_copy_start))   // :285  { eng_id, qid, tx_desc_count, rx_desc_count }
    return ndmar_queue_copy_start(nd, arg.eng_id, arg.qid,      // :289  → ndmar_queue_copy_start
                                  arg.tx_desc_count, arg.rx_desc_count)   //       → udma_m2m_copy_start (doorbell +0x38)

// neuron_cdev.c:293 — recycle spent descriptors after completion is observed
function ncdev_dma_ack_completed(nd, param):
    copy_from_user(&arg, param, sizeof(dma_ack_completed))      // :297  { eng_id, qid, count }
    return ndmar_ack_completed(nd, arg.eng_id, arg.qid, arg.count)   // :301  advances the UDMA completion counter
```

`DMA_QUEUE_GET_STATE` (`:304`) fills *two* `struct neuron_dma_queue_state` (TX and RX) via `ndmar_queue_get_state` and `copy_to_user`s each through its own out-pointer (`:315`,`:318`). The state struct (`share/neuron_driver_shared.h:81`) is `{hw_status, sw_status, base_addr(u64), length, head_pointer, tail_pointer, completion_base_addr(u64), completion_head}` — the ring-cursor snapshot a runtime polls.

### Function Map — Group B

| Handler | file:line | Arg struct (`neuron_ioctl.h`) | Boundary call | Gate | Conf |
|---|---|---|---|---|---|
| `ncdev_dma_queue_init` | `:127` | `dma_queue_init` `:260` (48 B) | `ndmar_queue_init(...,false)` `:151` | PID `:3211` | HIGH |
| `ncdev_dma_queue_init_batch` | `:180` | `dma_queue_init_batch` `:272` (12296 B) | per-entry `ndmar_queue_init` `:175` | none | HIGH |
| `ncdev_dma_copy_start` | `:281` | `dma_queue_copy_start` `:296` (16 B) | `ndmar_queue_copy_start` `:289` | none | HIGH |
| `ncdev_dma_ack_completed` | `:293` | `dma_ack_completed` `:290` (12 B) | `ndmar_ack_completed` `:301` | PID `:3211` | HIGH |
| `ncdev_dma_queue_get_state` | `:304` | `dma_queue_get_state` `:317` (24 B) | `ndmar_queue_get_state` `:312` | none | HIGH |
| `ncdev_dma_queue_release` | `:321` | `dma_queue_release` `:277` (8 B) | `ndmar_queue_release` `:328` (stub) | PID `:3212` | HIGH |
| `DMA_QUIESCE_QUEUES` | — | `dma_quiesce_queues` `:283` | removed → `-ENOIOCTLCMD` `:3279-3281` | PID `:3212`† | HIGH |

> **GOTCHA —** `DMA_QUEUE_INIT_BATCH`'s arg struct is `4 (count) + 256 × 48 (entries) = 12296` bytes (`neuron_ioctl.h:271-275`), which exceeds the 14-bit `_IOC_SIZE` field (max `0x3FFF`), so the encoded `_IOC_SIZE` *wraps*. The kernel dispatches it by exact `cmd ==` (`:3263`) and `libnrt` issues the same wrapped constant, so the two agree and the wrap self-cancels — but a reimplementer computing the command with a standards-correct `_IOC` macro that asserts `size <= 0x3FFF` gets a different value and `-ENOTTY`. The guard `arg->count >= MAX_DMA_QUEUE_INIT_BATCH` (`:195`) is *strict* `>=`, so the usable maximum is **255** entries, not 256 — the 256th slot exists in the struct but can never be filled. The `†` on `QUIESCE` marks a dead arm: it stays in the attach allow-list region but the handler returns `-ENOIOCTLCMD` with a "no longer supported" log (`:3280`).

---

## 4. Group C — Descriptor I/O

### Purpose

Move 16-byte UDMA descriptors between userspace and a ring `mem_chunk`: `DMA_COPY_DESCRIPTORS` stages user-built descriptors *into* a ring (the program path), `DMA_DESCRIPTOR_COPYOUT` reads HW-ring descriptors *back out* (the inspect/debug path). Both branch on whether the ring lives in host or device memory.

### Algorithm — descriptor staging (the bounce loop)

```c
// neuron_cdev.c:211 — stage user descriptors into ring MC, ≤64 KiB at a time
function ncdev_dma_copy_descriptors(nd, cmd, param):
    static_assert(DMA_COPY_DESCRIPTORS != DMA_COPY_DESCRIPTORS64)        // :213
    if cmd == DMA_COPY_DESCRIPTORS:    copy_from_user(&arg32); widen     // :225-234  u32 offset
    elif cmd == DMA_COPY_DESCRIPTORS64: copy_from_user(&arg64); widen    // :235-244  u64 offset
    else: return -EINVAL                                                 // :245-246

    mc = ncdev_mem_handle_to_mem_chunk(nd, mem_handle)                   // :249  ring chunk + magic-check
    if !mc: return -EINVAL                                              // :250-251
    if !mc_access_is_within_bounds(mc, offset, num_descs * 16):         // :253  THE OOB gate (sizeof udma_desc)
        return -EINVAL                                                  // :254
    remaining = num_descs * sizeof(union udma_desc)                     // :257  = num_descs * 16
    mc_alloc_align(nd, MC_LIFESPAN_LOCAL, MAX_DMA_DESC_SIZE, 0,         // :258  64 KiB host bounce buffer
                   MEM_LOC_HOST, .., mc->nc_id, NCDEV_HOST, &src_mc)
    while remaining:                                                    // :262
        copy_size = min(remaining, MAX_DMA_DESC_SIZE)                   // :263  ≤64 KiB BYTES (not descriptors)
        copy_from_user(src_mc->va, buffer + copy_offset, copy_size)    // :264  user → bounce
        ndma_memcpy_dma_copy_descriptors(nd, src_mc->va, 0, mc,        // :268  bounce → ring (→ dma-op-layer,
                                         offset + copy_offset,          //       which re-validates every desc PA)
                                         copy_size, queue_type)
        remaining -= copy_size; copy_offset += copy_size               // :273-274
    mc_free(&src_mc)                                                    // :277  release the LOCAL bounce buffer
    return ret
```

> **NOTE —** the staging loop chunks at `MAX_DMA_DESC_SIZE = 65536` **bytes** (`:263`), while the bounds check is `num_descs × sizeof(union udma_desc)` = `num_descs × 16` **bytes** (`:253`) — the units agree because both are byte counts, but a reimplementer must not read the chunk bound as "65536 descriptors." `ndma_memcpy_dma_copy_descriptors` re-walks the copied buffer and validates *each* descriptor's payload PA through the DHAL (`ndma_validate_pa`) before binding it ([dma-op-layer](dma-op-layer.md) Chain F) — so the `mc_access_is_within_bounds` gate here guards the *ring offset*, and the op-layer guards the *descriptor contents*; both are required.

### Algorithm — descriptor copyout (the device-pull path)

```c
// neuron_cdev.c:331 — read HW-ring descriptors back to userspace
function ncdev_dma_descriptor_copyout(nd, param):
    copy_from_user(&arg, param, sizeof(dma_descriptor_copyout))        // :342
    if arg.count == 0: return -EINVAL                                  // :346
    total_size = arg.count * 16; offset = arg.start_index * 16         // :349-350  desc_size = sizeof(udma_desc)
    ndmar_queue_get_descriptor_info(nd, eng_id, qid, &tx,&rx,          // :354  ring PAs + per-dir sizes
                                    &tx_pa,&rx_pa,&tx_size,&rx_size)
    eng = ndmar_acquire_engine(nd, eng_id)                             // :360  engine lock held to :412
    if arg.type == TX:  if count+start_index > tx_size: -EFBIG         // :361-366  bound vs descriptor count
        mc = tx; pa = tx_pa
    elif arg.type == RX: if count+start_index > rx_size: -EFBIG        // :369-376
        mc = rx; pa = rx_pa
    if mc == NULL: -EINVAL                                             // :381-384  (COMPLETION type → mc stays NULL)
    if mc->magic != MEMCHUNK_MAGIC: -EINVAL                            // :385-389
    if mc->mem_location == MEM_LOC_DEVICE:                             // :391  device ring → DMA-pull through kmalloc
        addr = kmalloc(total_size)                                     // :392
        ndma_memcpy(nd, mc->nc_id, pa,                                 // :397  device PA → host buffer PA
                    virt_to_phys(addr) | pci_host_base, total_size)    //       (host-PA encoding, dma-op-layer)
    else:
        addr = mc->va + offset                                        // :404  host ring → direct VA
    copy_to_user(arg.buffer, addr, total_size)                        // :407
    if device: kfree(addr)                                            // :408-409
    ndmar_release_engine(eng)                                         // :412
```

> **GOTCHA —** the copyout bound `count + start_index > tx_size` (`:362`) compares **descriptor counts** (`tx_size`/`rx_size` come back from `ndmar_queue_get_descriptor_info` as descriptor counts, not byte sizes), but the actual copy is `total_size = count × 16` bytes from `pa + start_index × 16`. The arithmetic is consistent only because every term scales by 16; a reimplementer who substitutes a byte size for `tx_size`, or drops the `× 16`, opens a ring-descriptor over-read. The `COMPLETION` queue type (`NEURON_DMA_QUEUE_TYPE_COMPLETION = 2`) is not handled — neither `if` branch matches, `mc` stays `NULL`, and the `:381` guard rejects it with `-EINVAL`. Only TX (0) and RX (1) are copyable.

### Function Map — Group C

| Handler | file:line | Arg struct | Validate gate | Boundary call | Conf |
|---|---|---|---|---|---|
| `ncdev_dma_copy_descriptors` | `:211` | `dma_copy_descriptors` `:244` / `64` `:252` | `mc_access_is_within_bounds` `:253` | `ndma_memcpy_dma_copy_descriptors` `:268` | HIGH |
| `ncdev_dma_descriptor_copyout` | `:331` | `dma_descriptor_copyout` `:324` (32 B) | `count+start_index ≤ {tx,rx}_size` `:362/:370` + magic `:385` | `ndma_memcpy` `:397` (device) | HIGH |

---

## 5. Group D — H2T Queue Management

### Purpose

Allocate and free the host-to-tensor "copy" and "service" queues a NeuronCore uses for its asynchronous H2D DMA, and report the mmap geometry of the per-queue async completion-queue (CQE) rings. These commands address queues by `nc_id`, not by `(eng_id, qid)` — the engine that hosts H2T for a given NeuronCore is resolved through the DHAL (`ndmar_get_h2t_eng_id`).

### Algorithm — H2T alloc with rollback

```c
// neuron_cdev.c:2920 — allocate copy + service H2T queues, build bitmaps
function ncdev_h2t_dma_alloc_queues(nd, cmd, param):
    copy_from_user(&arg, param, sizeof(h2t_dma_alloc_queues))          // :2927  24 B
    if arg.nc_id >= nc_per_device: -E2BIG                              // :2931  "invalid nc"
    if arg.copy_queue_cnt + arg.service_queue_cnt >= num_queues:       // :2936  total < 16 (strict)
        return -E2BIG                                                  // :2938  "invalid total queue count"
    arg.copy_queue_bmap = arg.service_queue_bmap = 0                   // :2941-2942
    for i in 0 .. copy_queue_cnt-1:                                    // :2944
        ndmar_h2t_ring_request(nd, arg.nc_id, true, &qid)              // :2945  request a COPY ring
        arg.copy_queue_bmap |= (1 << qid)                             // :2949
    for i in 0 .. service_queue_cnt-1:                                 // :2952
        ndmar_h2t_ring_request(nd, arg.nc_id, false, &qid)            // :2953  request a SERVICE ring
        arg.service_queue_bmap |= (1 << qid)                         // :2957
    arg.copy_default_queue = ndmar_get_h2t_def_qid(arg.nc_id)         // :2960  DHAL default qid
    ret = copy_to_user(param, &arg, sizeof(arg))                      // :2962  return bitmaps
done:
    if ret:                                                           // :2964  rollback: release every set bit
        combined = copy_queue_bmap | service_queue_bmap
        for i in 0 .. num_queues-1:
            if (1<<i) & combined: ndmar_h2t_ring_release(nd, nc_id, i) // :2969
        arg.copy_queue_bmap = arg.service_queue_bmap = 0
    return ret
```

`H2T_DMA_FREE_QUEUES` (`:2978`) is the mirror: validate `nc_id`, then loop `0..num_queues-1` and `ndmar_h2t_ring_release` every bit set in `arg.queue_bmap` (`:2993-3001`), accumulating the last error. `GET_ASYNC_H2T_DMA_COMPL_QUEUES` (`:3096`) reads, per requested qid, the per-queue completion-queue `mem_chunk` and returns its mmap offset and size:

```c
// neuron_cdev.c:3096 — report mmap geometry of the async H2D completion queues
function ncdev_get_async_h2d_dma_compl_queues(nd, param):
    copy_from_user(&arg, param, sizeof(get_async_h2t_dma_compl_queues))   // :3103  264 B
    if arg.nc_id >= nc_per_device: -EINVAL                                // :3113
    memset(arg.compl_queue_info, 0, ...)                                  // :3118  zero the 16-entry out array
    eng_id = ndmar_get_h2t_eng_id(nd, arg.nc_id)                         // :3120  DHAL: which engine hosts H2T
    for qid in 0 .. DMA_MAX_Q_MAX-1:                                     // :3122  16 queues
        if !(arg.qid_bitmap & (1u<<qid)): continue                       // :3123
        ring = &nd->ndma_engine[eng_id].queues[qid].ring_info            // :3127  read ring struct (dma-rings)
        mc = ring->dma_compl_queue.mc                                    // :3128-3129
        if !mc: -EINVAL  "compl queue not initialized"                   // :3131-3135
        arg.compl_queue_info[qid].mmap_offset = nmmap_offset(mc)         // :3137  = mc->pa (mmap cookie)
        arg.compl_queue_info[qid].mmap_size =                           // :3138  header + capacity CQEs
            sizeof(neuron_h2d_dma_compl_queue_t) +
            (compl_queue->capacity_mask + 1) * sizeof(..._entry_t)
    return copy_to_user(param, &arg, sizeof(arg))                        // :3142
```

> **NOTE —** both count gates in this group are **strict** `>=`: `nc_id >= nc_per_device` (`:2931`) and `copy_cnt + service_cnt >= num_queues` (`:2936`). With `num_queues = 16`, the largest total a caller can request is **15** queues, not 16 — one queue is structurally unreachable through `ALLOC_QUEUES`, mirroring the off-by-one in the batch-init cap. The mmap cookie returned in `mmap_offset` is `mc->pa` verbatim (`nmmap_offset`, owned by [ioctl-mem](ioctl-mem.md)); userspace mmaps the CQE ring with `pgoff = mmap_offset >> PAGE_SHIFT` and then reads completions lock-free (the SPMC CQE producer is `ndma_h2d_compl_queue_put`, [dma-op-layer](dma-op-layer.md)).

### Function Map — Group D + E

| Handler | file:line | Arg struct | Validate gate | Boundary call | Conf |
|---|---|---|---|---|---|
| `ncdev_h2t_dma_alloc_queues` | `:2920` | `h2t_dma_alloc_queues` `:604` (24 B) | `nc_id` `:2931` + total-count `:2936` | `ndmar_h2t_ring_request`/`_release` `:2945/:2969` | HIGH |
| `ncdev_h2t_dma_free_queues` | `:2978` | `h2t_dma_free_queues` `:614` (8 B) | `nc_id` `:2988` | `ndmar_h2t_ring_release` `:2996` | HIGH |
| `ncdev_get_async_h2d_dma_compl_queues` | `:3096` | `get_async_h2t_dma_compl_queues` `:646` (264 B) | `nc_id` `:3113` + `mc != NULL` `:3131` | reads `ring_info.dma_compl_queue` `:3127` | HIGH |
| `ncdev_get_dmabuf_fd` | `:674` | `dmabuf_fd` `:488` (24 B) | (none — delegates) | `ndmabuf_get_fd` `:684` | HIGH |

> **NOTE —** `DMABUF_FD` (group E) is the only DMA-family command on the **free-access** misc lane: `ncdev_misc_ioctl` routes it (`:3158-3162`) "to avoid iterating over all devices in the user space" (source comment). `ncdev_get_dmabuf_fd` (`:674`) takes no `nd` — it `copy_from_user`s `{va, size}`, calls `ndmabuf_get_fd(va, size, &fd)` (`:684`, [K-DMABUF boundary](dma-op-layer.md)), and `copy_to_user`s the new fd. An `O_WRONLY` opener reaches it without `DEVICE_INIT`; it exports a host VA range as a dma-buf fd for cross-driver sharing.

---

## 6. Per-Handler Arg Structs

The `[in]`/`[out]` field set per handler. Offsets are nominal LP64 (declaration order; the field *set* is HIGH, exact tail padding for pointer/enum-containing structs is MED — not `pahole`-verified).

| Handler | Arg struct (`neuron_ioctl.h`) | Key `[in]` fields | Key `[out]` fields |
|---|---|---|---|
| `ncdev_dma_engine_set_state` | `dma_eng_set_state` `:307` | `eng_id`, `state` | — |
| `ncdev_dma_engine_get_state` | `dma_eng_get_state` `:312` | `eng_id` | `*state` (`neuron_dma_eng_state`, 20 B) |
| `ncdev_dma_queue_init` | `dma_queue_init` `:260` | `eng_id`, `qid`, `tx/rx_desc_count`, `tx/rx/rxc_handle`, `axi_port` | — |
| `ncdev_dma_queue_init_batch` | `dma_queue_init_batch` `:272` | `count`, `entries[256]` | — |
| `ncdev_dma_copy_start` | `dma_queue_copy_start` `:296` | `eng_id`, `qid`, `tx_desc_count`, `rx_desc_count` | — |
| `ncdev_dma_ack_completed` | `dma_ack_completed` `:290` | `eng_id`, `qid`, `count` | — |
| `ncdev_dma_queue_get_state` | `dma_queue_get_state` `:317` | `eng_id`, `qid` | `*tx`, `*rx` (`neuron_dma_queue_state` ×2) |
| `ncdev_dma_queue_release` | `dma_queue_release` `:277` | `eng_id`, `qid` | — |
| `ncdev_dma_copy_descriptors` | `dma_copy_descriptors` `:244` / `64` `:252` | `mem_handle`, `buffer`, `num_descs`, `offset`, `queue_type` | — (`64` widens `offset` to `u64`) |
| `ncdev_dma_descriptor_copyout` | `dma_descriptor_copyout` `:324` | `eng_id`, `qid`, `type`, `start_index`, `count` | `*buffer` |
| `ncdev_h2t_dma_alloc_queues` | `h2t_dma_alloc_queues` `:604` | `sz`, `nc_id`, `copy/service_queue_cnt` | `copy/service_queue_bmap`, `copy_default_queue` |
| `ncdev_h2t_dma_free_queues` | `h2t_dma_free_queues` `:614` | `sz`, `nc_id`, `queue_bmap` | — |
| `ncdev_get_async_h2d_dma_compl_queues` | `get_async_h2t_dma_compl_queues` `:646` | `nc_id`, `qid_bitmap` | `compl_queue_info[16]` (`mmap_offset`, `mmap_size`) |
| `ncdev_get_dmabuf_fd` | `dmabuf_fd` `:488` | `va`, `size` | `*fd` |

### Enums and constants (verbatim)

| Name | Value | Source |
|---|---|---|
| `NEURON_DMA_QUEUE_TYPE_TX / _RX / _COMPLETION` | `0 / 1 / 2` | `share/neuron_driver_shared.h:61-63` |
| `NEURON_DMA_H2T_CTX_HANDLE_{NONE,SYNC,ASYNC1,ASYNC2,CNT}` | `-1, 0, 1, 2, 3` | `share/neuron_driver_shared.h:93-97` |
| `MAX_DMA_QUEUE_INIT_BATCH` | `256` | `neuron_ioctl.h:271` |
| `MAX_DMA_DESC_SIZE` | `65536` | `udma/udma.h:479` |
| `DMA_MAX_Q_V4` / `DMA_MAX_Q_MAX` | `16` | `udma/udma.h:12-13` |
| `sizeof(union udma_desc)` | `16` | `neuron_cdev.c:337` (`desc_size = sizeof(union udma_desc)`) |
| `MEMCHUNK_MAGIC` | `0xE1C2D3F4` | `neuron_mempool.h:114` |

---

## 7. Dispatch and the Attach Gate

Routing is owned by [ioctl-dispatch](ioctl-dispatch.md) and tabulated by [ioctl-catalog](ioctl-catalog.md#family-dma); this page records only how the DMA family lands. Single-variant commands dispatch by exact `cmd ==` (`:3259-3277`); `DMA_COPY_DESCRIPTORS`/`64` dispatch by `_IOC_NR` (`:3271`) into one handler that demuxes width by exact `cmd ==`. The `dmabuf` command is the lone DMA-family member on the free-access misc lane (`ncdev_misc_ioctl`, `:3158`), so an `O_WRONLY` opener can reach it without attaching.

The attach gate (`:3210-3225`) tests **exact** `cmd` values. Among DMA commands it lists `DMA_ENG_INIT`, `DMA_ENG_SET_STATE`, `DMA_QUEUE_INIT`, `DMA_ACK_COMPLETED`, `DMA_QUEUE_RELEASE`, and `DMA_COPY_DESCRIPTORS` (the 32-bit form) — these require the `DEVICE_INIT` owner (`npid_is_attached`, `:3220`) on a non-free-access fd. Everything else in the family — `GET_STATE` queries, `COPY_START`, `DESCRIPTOR_COPYOUT`, `QUEUE_INIT_BATCH`, the whole H2T group — runs on any non-free-access fd with no attach check.

> **GOTCHA —** `DMA_COPY_DESCRIPTORS64` reaches `ncdev_dma_copy_descriptors` via `_IOC_NR` dispatch (`:3271`) but **slips the attach gate**: the gate tests `cmd == NEURON_IOCTL_DMA_COPY_DESCRIPTORS` (`:3212`), the 32-bit constant, which the `64` width fails (its `_IOC_SIZE` differs). So a non-`DEVICE_INIT` opener of a non-free-access fd can stage descriptors through the 64-bit command path while the 32-bit path is gated. The blast radius is bounded by handle ownership (`mc_handle_find` resolves only *this* `nd`'s handles), the `mc_access_is_within_bounds` ring-offset gate (`:253`), and the per-descriptor PA validation in the op layer — but the gate asymmetry is real and shared with `MEM_COPY64`/`MEM_COPY_ASYNC64` (the mem family's S3). Full analysis is finding S3 on [ioctl-attack-surface](../security/ioctl-attack-surface.md).

> **QUIRK —** the `COPY_START` trigger is **not** in the attach allow-list (`:3210-3219` omits it), so a non-`DEVICE_INIT` process holding a non-free-access fd can arm a queue and ring the doorbell — provided it already has a bound queue. Binding (`DMA_QUEUE_INIT`) *is* PID-gated (`:3211`), so the trigger is reachable but the queue it triggers must have been bound by the attached owner. A reimplementer modelling the privilege surface must treat "trigger" and "bind" as separately gated steps, not one atomic privileged operation.

---

## Cross-References

- [IOCTL Catalog](ioctl-catalog.md) — the per-command `nr`/direction/arg-struct/`ndl_*`-caller/gate table for the dma family; this page is the handler-body deep-dive the catalog's dma rows cross-reference
- [Memory IOCTL Handlers](ioctl-mem.md) — the sibling mem-handler deep-dive: the `u64` mem-handle ↔ `mem_chunk` resolution, the 2-level handle table, `mc_access_is_within_bounds`, and the `nmmap_offset` cookie that this page's handlers reuse
- [DMA Op Layer and the Completion-Marker Model](dma-op-layer.md) — `ndma_memcpy`/`ndma_memcpy_dma_copy_descriptors` (descriptor staging + per-PA validation), the host-PA `| pci_host_base` encoding, and the SPMC async completion-queue producer behind `GET_ASYNC_H2T_DMA_COMPL_QUEUES`
- [DMA Rings and H2T Queues](dma-rings.md) — `ndmar_queue_init`/`_copy_start`/`_get_state`/`ack_completed`/`h2t_ring_request`, the queue lifecycle these handlers marshal into, and the meaning of the trailing `false` bind argument
- [UDMA Core (Annapurna/Alpine Fork)](udma-main.md) — `udma_m2m_copy_start`, the doorbell (`drtp_inc` at `+0x38`) that `DMA_QUEUE_COPY_START` ultimately rings, and the 16-byte `union udma_desc` the descriptor commands move
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security projection: the `DMA_COPY_DESCRIPTORS64` attach-gate slip (S3), the descriptor-count bound on `DESCRIPTOR_COPYOUT`, and the ungated `COPY_START` trigger
