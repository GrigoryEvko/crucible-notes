# The XU Work-Queue and Worker Threads

> *All addresses, offsets, and struct ordinals on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64 x86-64, **not** stripped, full C++ symbols + DWARF present). `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA verifiable against `nm -n libnrt.so`; BSS globals are cited by their `_ZL` symbol. Source asserts name `/opt/workspace/KaenaRuntime/kmgr/xu/{tpb_xu.cc, xu_worker.cc, queue.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (decompiled + struct-anchored)** — the three-cursor SPSC ring, the producer/consumer fences, the per-vNC epoll worker loop, and the wait→dispatch→complete→kmetric step were read from per-function decompilation and cross-checked against the IDA `structures.json` (ordinals), `functions.json` (frame/size), `names.json` (BSS symbols), and `strings.json`. The `device_exec_request` byte ABI is **deferred** to [the xu_queue / device_exec_request ABI page](xu-queue-abi.md). · Part VII — Execution Engine · [back to index](../index.md)*

## Abstract

The **XU layer** ("Execution Unit") is the runtime's lock-free hand-off between the API thread that submits an inference and the per-NeuronCore thread that harvests its completion. Read it the way you read a GPU command-submitter's ring: a single-producer/single-consumer (SPSC) queue, one slot per in-flight execution, with the producer (the API/client thread) bumping a tail cursor and the consumer (a dedicated worker thread) bumping a head cursor. There is exactly **one XU per virtual NeuronCore (vNC)** — a 576-byte `tpb_execution_unit_t` in the process-global array `tpb_xus` (`@0xc64a40`, `MAX_VIRTUAL_TPB = 256` entries) — and exactly one worker thread per vNC, each `epoll`-waiting on its XU's `has_work_efd` eventfd. This page owns that work-queue + worker-thread model: the ring's three cursors and their memory ordering, the worker loop's wait→dispatch→complete→kmetric cycle, and how shutdown drains both. The submit-side staging (the 52-byte `device_exec_request_t`, the doorbell `ioctl`) is owned by [the submit path](submit-path.md); the NQ harvest that the worker drives is owned by [the completion engine](completion-engine.md); the canonical byte layout of the queue structs is owned by [the ABI page](xu-queue-abi.md).

The ring is a **256-byte `xu_queue_t`** with **three monotonic 48-bit cursors, each isolated on its own 64-byte cache line** to defeat false sharing: `exec_head` (`+0x40`, the consumer/completed cursor), `scheduled_head` (`+0x80`, the released-to-HW cursor), and `scheduled_tail` (`+0xC0`, the producer cursor). The invariant `exec_head ≤ scheduled_head ≤ scheduled_tail` holds at all times. A slot transitions producer→worker through three distinct cursor bumps owned by three distinct operations: `xuq_client_submit` (`@0xe9f10`) writes the slot and bumps `scheduled_tail`; `xuq_client_trigger_next` (`@0xea090`) bumps `scheduled_head` once the descriptors+doorbell for that slot have issued; `xuq_worker_pop_current_exec` (`@0xea1a0`) bumps `exec_head` once the worker observes the device completion. All three advances are `_InterlockedAdd64` (`LOCK XADD`) — a full barrier on x86-64 — which, combined with the cache-line padding and the strict single-producer/single-consumer discipline, is the entire lock-free machinery. No CAS loop, no spin, no per-slot lock.

The worker side is a per-vNC pthread running `kmgr_xu_worker_do_work` (`@0xe92c0`). It `epoll`-waits on a worker-private *stop* eventfd plus every owned XU's `has_work_efd`; on a `has_work` event it calls `tpb_xu_step` (`@0xe8940`), which peeks the head slot, drives the [completion state machine](completion-engine.md) one non-blocking step, and on `DONE` feeds three NDS counter updaters (`kmetric_update_nds_exec_latencies` / `_error_stats` / `_exec_stats`, all in-cell at `@0xe0fe0`/`@0xe0f60`/`@0xe0d30`), handles any deferred model unstage under a per-XU lock, frees the work item, and calls `tpb_xu_base_report_complete` (`@0xe8800`) to pop the slot and signal the submitter's completion eventfd. On a *stop* event the worker exits the thread. There is a **second, separate** worker model — the implicit-async pool (`kmgr_exec_worker_do_work` `@0xe6310`, a `std::deque` + condition-variable design) — documented here only by its delta from the XU model; the two are mutually exclusive (`tpb_xu_step` asserts `!implicit_async_mode`).

For reimplementation, the contract is:

- **The SPSC ring** — a 256-byte `xu_queue_t` with three 48-bit cursors at `+0x40`/`+0x80`/`+0xC0` (cache-line isolated), all starting at **1** (seq_num 0 reserved), each advanced by a single `LOCK XADD`; full predicate `scheduled_tail − exec_head ≥ capacity`, empty predicates `scheduled_head == scheduled_tail` (producer) and `exec_head == scheduled_head` (worker).
- **The seq_id encoding** — `(cursor & 0xFFFFFFFFFFFF) | (xu_id.raw << 48)`; low 48 bits = monotone seq_num (asserts `< 0xFFFFFFFFFFFE`, never wraps), high 16 = `xu_id {type:4, queue:4, vnc_id:8}`. "Last completed" = `exec_head − 1`.
- **The per-vNC worker** — one pthread per vNC, `epoll` over a stop eventfd (`EPOLLIN|EPOLLEXCLUSIVE`) plus each owned XU's `has_work_efd` (`EFD_NONBLOCK|EFD_SEMAPHORE`); dispatch tag `0` = exit, tag `1` = `tpb_xu_step`.
- **The step cycle** — peek → health/type dispatch → `exec_request_progress_one_step` → on `DONE` per-TPB kmetric update → deferred unstage under `unstage_lock` → free → `report_complete`.
- **The two-eventfd handshake** — submit `write(has_work_efd, 1)` wakes the worker; the worker `read(has_work_efd, 8)` consumes exactly one semaphore count, then `write(mark_comp_efd, 1)` wakes the blocked submitter. Both eventfds are userspace-to-userspace; the kernel is not in the completion loop.

| | |
|---|---|
| **XU array** | `tpb_xus` `@0xc64a40` — `tpb_execution_unit_t[256]`, stride **576 B** (`0x240`), span `0x24000` |
| **SPSC ring** | `xu_queue_t` — **256 B** (ord 13198), cursors `+0x40`/`+0x80`/`+0xC0` |
| **Ring slot** | `xuq_exec_info_t` — **32 B** (ord 13199) |
| **Worker array** | `_ZL7workers` `@0xc88ae0` — `kmgr_xu_worker_t[]`, **88 B** each (ord 13206); count `_ZL12worker_count` `@0xc88ac0` |
| **Worker entry** | `kmgr_xu_worker_do_work` `@0xe92c0` (1639 B) — per-vNC `epoll` loop |
| **Worker step** | `tpb_xu_step` `@0xe8940` (1781 B) — peek→step→kmetric→report |
| **Producer** | `xuq_client_submit` `@0xe9f10` (224 B) — bump `scheduled_tail` |
| **Consumer** | `xuq_worker_pop_current_exec` `@0xea1a0` (253 B) — bump `exec_head`, signal `mark_comp_efd` |
| **Completion signal** | `tpb_xu_base_report_complete` `@0xe8800` (308 B) |
| **Async-pool sibling** | `kmgr_exec_worker_do_work` `@0xe6310` (deque + condvar; *not* this ring) |

---

## 1. The Lock-Free SPSC Ring

### Purpose

`xu_queue_t` is the per-XU hand-off between the submitter and the worker. It is an SPSC ring of `capacity` slots, each a 32-byte `xuq_exec_info_t`, with three monotone cursors that together encode the lifecycle of a slot: *submitted* (slot written), *released to hardware* (descriptors + doorbell issued), and *completed* (device finished, slot reclaimed). The design choice that makes it lock-free is that each of the three cursors is mutated by exactly one of three distinct operations — and the producer and the consumer never write the same cursor — so a single atomic add per advance suffices with no compare-and-swap and no lock. This section pins the layout, the three predicates, and the seq_id encoding before any code that reads them.

### Struct: `xu_queue_t` (256 bytes, ord 13198)

The ring header. The deliberate gaps (`0x18..0x3F`, `0x48..0x7F`, `0x88..0xBF`, `0xC8..0xFF`) are padding that places each cursor on its own 64-byte cache line.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `xu_id` | `+0x00` | `xu_id_t` (16-bit) | `{type:4, queue:4, vnc_id:8}`; goes in the high 16 bits of every `seq_id` | HIGH |
| `buffer` | `+0x08` | `xuq_exec_info_t*` | `calloc(capacity, 0x20)` slot ring | HIGH |
| `capacity` | `+0x10` | `uint64_t` | slot count; modulus for `cursor % capacity` | HIGH |
| `exec_head` | `+0x40` | `uint64_t` | **consumer cursor** (own cache line); bumped by `xuq_worker_pop_current_exec` | HIGH |
| `scheduled_head` | `+0x80` | `uint64_t` | **released-to-HW cursor** (own cache line); bumped by `xuq_client_trigger_next` | HIGH |
| `scheduled_tail` | `+0xC0` | `uint64_t` | **producer cursor** (own cache line); bumped by `xuq_client_submit` | HIGH |

### Struct: `xuq_exec_info_t` (32 bytes, ord 13199)

One ring slot. The worker reads `mark_comp_efd`/`efd_from_pool` at completion; everything else the producer writes at submit.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `seq_id` | `+0x00` | `seq_id_t` | `(seq_num:48 \| xu_id:16)` | HIGH |
| `exec_status` | `+0x08` | `NRT_STATUS*` | caller's status out-pointer; the worker writes the result through it | HIGH |
| `mark_comp_efd` | `+0x10` | `int` | completion eventfd; **`-1` until a waiter installs one** | HIGH |
| `efd_from_pool` | `+0x14` | `bool` | `true` ⇒ pooled fd, **do not** `close()` on signal | HIGH |
| `exec_info` | `+0x18` | `void*` | opaque work item (`tpb_execution_info_t*`) | HIGH |

### The three cursors and their predicates

The three cursors carve the ring into three regions, and each region names a slot's lifecycle stage. With `exec_head ≤ scheduled_head ≤ scheduled_tail` as a standing invariant:

```text
 reclaimed        in-flight on device      submitted-not-triggered     free
◀──────────── exec_head ──────── scheduled_head ──────── scheduled_tail ────────▶
              [exec_head,        [scheduled_head,        [scheduled_tail, …)
               scheduled_head)    scheduled_tail)         unwritten
               = device busy      = staged, awaiting
                                    doorbell trigger
```

| Predicate | Test | Owner | Meaning |
|---|---|---|---|
| Full | `scheduled_tail − exec_head ≥ capacity` | producer | no free slot → `NRT_QUEUE_FULL` (7) |
| Trigger-empty | `scheduled_head == scheduled_tail` | producer (trigger) | nothing submitted-but-untriggered → `NRT_QUEUE_EMPTY` (15) |
| Worker-empty | `exec_head == scheduled_head` | worker | nothing in flight to harvest → `NRT_QUEUE_EMPTY` (15) |

> **QUIRK —** the in-flight *depth* is `scheduled_tail − exec_head` (used by the full check), not `scheduled_head − exec_head`. A slot counts against capacity from the moment it is *submitted* (tail bumped), even before it is *triggered* (head bumped) — so the queue can be full of submitted-but-untriggered work. A reimplementation that gates fullness on the released-to-HW cursor will over-admit and clobber a slot the producer has already written but not yet doorbelled.

### The start-at-1 convention

`xu_queue_init` (`@0xe9d00`) sets `exec_head = scheduled_head = scheduled_tail = 1`, not 0:

```c
NRT_STATUS xu_queue_init(xu_queue_t *xq, xu_id_t xu_id, uint64_t capacity):   // 0xe9d00
    assert xq != NULL                                  // queue.c:0x15
    if capacity == 0: return NRT_INVALID               // "XU Queue size must be greater than 0"
    xq->buffer = calloc(capacity, 0x20)                // 32-byte slots
    if !xq->buffer: return NRT_RESOURCE                // "Failed to allocate memory for XU Queue"
    xq->capacity       = capacity
    xq->xu_id          = xu_id
    xq->exec_head      = 1                              // <-- all THREE start at 1, not 0
    xq->scheduled_head = 1
    xq->scheduled_tail = 1
    return NRT_SUCCESS
```

> **QUIRK —** seq_num **0 is never a valid in-queue sequence**. Because all three cursors start at 1 and `xuq_get_last_completed` returns `exec_head − 1`, a freshly-initialized queue reports last-completed seq_num `0` — i.e. "nothing done yet." A reimplementation that initializes cursors to 0 will report a phantom completion of seq_num `0xFFFFFFFFFFFF` (the `(0 − 1) & 0xFFFFFFFFFFFF` underflow) on the first `nrta_is_completed` poll, and will index `buffer[0]` for the first submit instead of `buffer[1 % capacity]`.

### The seq_id encoding

A slot's `seq_id` is a self-describing 64-bit value that lets `nrta_is_completed` test completion with no side table — the high 16 bits identify the owning XU, the low 48 the per-XU monotone counter. Both submit and "last completed" compute it identically:

```c
seq_id.raw = (cursor & 0xFFFFFFFFFFFF) | ((uint64_t)xu_id.raw << 48)
//            └──────── seq_num:48 ────────┘  └──── xu_id:16 ────┘
//            cursor = scheduled_tail (submit)  {type:4, queue:4, vnc_id:8}
//                   = exec_head − 1 (last completed)
```

Every queue op asserts the seq_num it indexed actually matches the cursor it expected (`xuq_*_peek_*` assert `slot.seq_id.seq_num == cursor`), so a stale or corrupt slot is caught at the read, not propagated. The monotonicity guard `scheduled_tail < NRTA_SEQ_NUM_MAX = (1<<48)−1 = 0xFFFFFFFFFFFE` is asserted on every submit; the 48-bit seq space is treated as **non-wrapping for the process lifetime** (≈ 2.8×10¹⁴ executions — unreachable in practice).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `xu_queue_init` | `0xe9d00` | 235 | `calloc` slots; cursors = 1; reject `capacity == 0` | HIGH |
| `xu_queue_destroy` | `0xe9df0` | 73 | `free(buffer)`; `memset` the 256-byte header to 0 | HIGH |
| `xuq_get_last_completed` | `0xe9e40` | 32 | pure: `(exec_head − 1) & mask \| (xu_id<<48)` | HIGH |
| `xu_queue_get_exec` | `0xe9e60` | 174 | range-check `seq_num ∈ [exec_head, scheduled_tail)`; return `&buffer[seq_num % capacity]` | HIGH |

---

## 2. Producer, Trigger, and Consumer — the Fences

### Purpose

Three operations advance the three cursors, and the order in which a slot's *body* is written relative to the *cursor bump* is what makes the ring safe without a lock. The producer writes the slot **then** bumps `scheduled_tail`; the worker reads the slot **then** bumps `exec_head`. On x86-64 the `_InterlockedAdd64` (`LOCK XADD`) that advances every cursor is a full memory barrier, so the store of the slot body cannot be reordered past the cursor publish, and the consumer's load of the slot cannot be reordered before its observation of the cursor. This is the one place a reimplementer must get the ordering exactly right.

### Algorithm — producer (`xuq_client_submit`)

```c
NRT_STATUS xuq_client_submit(xu_queue_t *xq, void *item,             // 0xe9f10
                             NRT_STATUS *exec_status, seq_id_t *out_seq_id):
    assert xq != NULL                                                // queue.c:0x4D
    if (scheduled_tail − exec_head) >= capacity: return NRT_QUEUE_FULL   // depth = tail − head

    seq = (scheduled_tail & 0xFFFFFFFFFFFF) | (xu_id.raw << 48)
    slot = &buffer[scheduled_tail % capacity]
    slot->seq_id        = seq                                        // ┐
    slot->exec_status   = exec_status                               // │ STORE the slot body
    slot->exec_info     = item                                      // │ BEFORE publishing
    slot->mark_comp_efd = -1            // a waiter installs one later │ the new tail
    if out_seq_id: out_seq_id->raw = seq                            // ┘

    _InterlockedAdd64(&scheduled_tail, 1)         // <== PUBLISH (LOCK XADD = full barrier)
    assert scheduled_tail < NRTA_SEQ_NUM_MAX      // 0xFFFFFFFFFFFE — never wraps  (queue.c:0x5C)
    return NRT_SUCCESS
```

> **GOTCHA —** the slot body is written **before** `_InterlockedAdd64(&scheduled_tail, 1)`, never after. The worker decides a slot is readable by observing `exec_head != scheduled_head` — but `scheduled_head` only moves past this slot in `xuq_client_trigger_next`, which itself runs *after* submit on the same producer thread and bumps via the same `LOCK XADD`. A reimplementation that publishes the tail (or the head) before finishing the slot stores exposes a torn slot to the worker. On x86 the `LOCK`-prefixed add is the release fence; on a weaker ISA the reimplementer must insert an explicit store-release on the cursor and a load-acquire on the consumer side.

### Algorithm — trigger (`xuq_client_trigger_next`)

`scheduled_head` is bumped *separately* from `scheduled_tail`, by `xuq_client_trigger_next`, only after the submit path has staged the descriptors and rung the doorbell for that slot. This two-cursor split is what lets the producer hold a slot in the "submitted-but-not-yet-triggered" region while it builds hardware descriptors:

```c
NRT_STATUS xuq_client_trigger_next(xu_queue_t *xq, xuq_exec_info_t *out):   // 0xea090
    assert xq != NULL                                               // queue.c:0x71
    rc = xuq_client_peek_next_trigger(xq, &info)                    // empty if sched_head==sched_tail
    if rc == NRT_SUCCESS:
        if out:                                                     // copy the 32-byte slot out
            out[0..15]  = _mm_loadu_si128(info)                     // seq_id + exec_status
            out[16..31] = _mm_loadu_si128(&info->mark_comp_efd)     // efd + exec_info
        _InterlockedAdd64(&scheduled_head, 1)                       // release the slot to HW
    return rc

// xuq_client_peek_next_trigger (0xe9ff0): NRT_QUEUE_EMPTY if scheduled_head == scheduled_tail;
//   else assert buffer[scheduled_head % capacity].seq_id.seq_num == scheduled_head  (queue.c:0x6A)
```

### Algorithm — consumer (`xuq_worker_pop_current_exec`)

The worker reads the head slot's `mark_comp_efd`/`efd_from_pool`, bumps `exec_head`, then signals the waiter. The pop is the **completion-signal primitive**: it is the only place `mark_comp_efd` is written.

```c
NRT_STATUS xuq_worker_pop_current_exec(xu_queue_t *xq):              // 0xea1a0
    assert xq != NULL                                               // queue.c:0x8E
    rc = xuq_worker_peek_current_exec(xq, &info)                    // empty if exec_head==scheduled_head
    if rc != NRT_SUCCESS: return rc                                 // NRT_QUEUE_EMPTY

    seq           = info->seq_id           // ┐ CAPTURE slot fields
    mark_comp_efd = info->mark_comp_efd    // │ BEFORE bumping exec_head — once exec_head
    efd_from_pool = info->efd_from_pool    // ┘ moves, the producer may overwrite this slot

    _InterlockedAdd64(&exec_head, 1)        // <== reclaim the slot (LOCK XADD = full barrier)

    if mark_comp_efd != -1:                 // a waiter is blocked on read(mark_comp_efd)
        if write(mark_comp_efd, &one, 8) == -1:
            log("Failed to signal completion event for request 0x%lx. errno: %d", seq, errno)
            if !efd_from_pool: close(mark_comp_efd)
            return NRT_FAILURE
        if !efd_from_pool: close(mark_comp_efd)   // fresh fd: signal-and-close; pooled: reuse
    return NRT_SUCCESS
```

> **GOTCHA —** `mark_comp_efd` and `efd_from_pool` are captured **before** `_InterlockedAdd64(&exec_head, 1)`, not after. The instant `exec_head` advances, that slot re-enters the producer's free region and a concurrent `xuq_client_submit` may overwrite `mark_comp_efd` with `-1` for the next execution. Reading the fd after the bump is a data race that would either signal the wrong waiter or signal `-1` (no-op). Capture-then-publish is mandatory.

> **QUIRK —** `xuq_worker_peek_current_exec` and `xuq_client_peek_next_trigger` both index `buffer[cursor % capacity]` and then **assert** the slot's stored seq_num equals the cursor (`queue.c:0x87` / `0x6A`). This is a torn-write / wraparound tripwire: under correct SPSC discipline the assert is always true, so a failure is a hard signal that the cache-line padding or the fence ordering is wrong. A reimplementation should keep the assert in debug builds — it is the cheapest detector of a broken ring.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `xuq_client_submit` | `0xe9f10` | 224 | write slot; `LOCK XADD scheduled_tail`; full check | HIGH |
| `xuq_client_peek_next_trigger` | `0xe9ff0` | 160 | empty check + seq_num consistency assert | HIGH |
| `xuq_client_trigger_next` | `0xea090` | 105 | copy slot out; `LOCK XADD scheduled_head` | HIGH |
| `xuq_worker_peek_current_exec` | `0xea100` | 152 | head slot or `NRT_QUEUE_EMPTY`; seq_num assert | HIGH |
| `xuq_worker_pop_current_exec` | `0xea1a0` | 253 | capture efd; `LOCK XADD exec_head`; signal `mark_comp_efd`; conditional `close` | HIGH |

---

## 3. The XU and its Container

### Purpose

The ring lives inside a larger per-vNC object, the 576-byte `tpb_execution_unit_t`, which adds the wakeup eventfd, the two locks the submit/complete paths take, and the per-unit execution state the worker threads through. There is one such unit per vNC in the global `tpb_xus` array, indexed by the vNC's `vtpb`. This section pins the container layout and the two lookup functions, because every later algorithm resolves an XU through one of them.

### Struct: `tpb_xu_base_t` (448 bytes, ord 13216)

The base embedded at offset 0 of every XU. It holds the SPSC ring plus the eventfd and locks.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `xu_id` | `+0x00` | `xu_id_t` | `{type, queue, vnc_id}`; `type = COMPUTE`, `vnc_id = vcore->vtpb` | HIGH |
| `initialized` | `+0x02` | `bool` | gates both lookups (return NULL if false) | HIGH |
| `vcore` | `+0x08` | `const virtual_core_t*` | bound logical core | HIGH |
| `has_work_efd` | `+0x10` | `int` | wakeup eventfd, `EFD_NONBLOCK\|EFD_SEMAPHORE` | HIGH |
| `health_status` | `+0x14` | `xu_health_status_t` | `HEALTHY=0` / `SHUTDOWN=1` / `FATAL_SHUTDOWN=2` | HIGH |
| `work_queue` | `+0x40` | `xu_queue_t` (256 B) | the §1 SPSC ring | HIGH |
| `submit_work_lock` | `+0x140` | `pthread_mutex_t` | held by the submit funnel ([submit path §4](submit-path.md)) | HIGH |
| `mark_comp_lock` | `+0x168` | `pthread_mutex_t` | held around `xuq_worker_pop_current_exec` in `report_complete` | HIGH |

### Struct: `tpb_execution_unit_t` (576 bytes, ord 13214)

The full per-vNC unit. Stride `0x240`; `tpb_xus` spans `256 × 576 = 0x24000` bytes (the decompiler's `stru_24000` size constant).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `base` | `+0x000` | `tpb_xu_base_t` (448 B) | the SPSC ring + eventfd + locks | HIGH |
| `current_exec_initialized` | `+0x1C0` | `bool` | gates one-time `exec_request_init_state` per request | HIGH |
| `current_exec_state` | `+0x1C8` | `exec_request_state_t*` | the 472-byte harvest state ([completion engine §1](completion-engine.md)); `calloc`'d at init | HIGH |
| `last_posted_model` | `+0x1D0` | `H_NN` | last model the submit funnel posted; the model-switch discriminant | HIGH |
| `deferred_unstage_ref` | `+0x1D8` | `dlr_model*[2]` | per sub-core deferred unstage; asserted NULL on destroy | HIGH |
| `last_exec_model` | `+0x1E8` | `H_NN[2]` | per sub-core last-executed model (drives unstage deferral) | HIGH |
| `unstage_lock` | `+0x1F0` | `pthread_mutex_t` | guards deferred-unstage refs (the init log calls it the "trigger lock") | HIGH |

### The two lookups

`tpb_xu_get_by_vcore` resolves an XU from a `virtual_core_t*` (the hot path); `tpb_xu_get` resolves one from a raw `xu_id_t` (used by the explicit-async completion API). Both return NULL on an uninitialized unit.

```c
tpb_execution_unit_t* tpb_xu_get_by_vcore(const virtual_core_t *vcore):   // 0xe7b10
    assert vcore->vtpb < MAX_VIRTUAL_TPB (256)         // tpb_xu.cc:0x43
    xu = (tpb_execution_unit_t*)((char*)&tpb_xus + 576 * vcore->vtpb)
    return xu->base.initialized ? xu : NULL

tpb_execution_unit_t* tpb_xu_get(xu_id_t xu_id):       // 0xe7b60
    assert (xu_id.raw & 0xF) == NRTA_XU_COMPUTE (1)    // tpb_xu.cc:0x4B
    idx = HIBYTE(xu_id.raw)                             // vnc_id is the high byte
    xu  = (tpb_execution_unit_t*)((char*)&tpb_xus + 576 * idx)
    return xu->base.initialized ? xu : NULL
```

> **NOTE —** the index into `tpb_xus` is the vNC id (`vcore->vtpb` ≡ the high byte of `xu_id`), not a physical TPB index. A single vNC may bind `sg_count` physical TPBs ([submit path §5](submit-path.md#5-the-doorbell-and-multi-nc-fan-out)), but there is **one XU and one worker per vNC** — the multi-TPB fan-out happens below the XU, in the stage/doorbell loops, not in the queue. The work item is enqueued exactly once even though `sg_count` cores execute it.

### Init and teardown

`tpb_xu_init` (`@0xe7e50`, called from `kmgr_init`) `memset`s the whole `tpb_xus` array, then for each vNC in `[0, vnc_count)`: resolves the vcore, calls `tpb_xu_base_init` (`@0xe7cf0`), inits `unstage_lock`, and `calloc`s `current_exec_state` via `exec_request_malloc_state`. `tpb_xu_base_init` creates the queue, the eventfd, and the two mutexes:

```c
NRT_STATUS tpb_xu_base_init(tpb_xu_base_t *xu, const virtual_core_t *vcore,
                            nrta_xu_t xu_type, uint64_t capacity):   // 0xe7cf0
    assert !xu->initialized                            // tpb_xu.cc:0x79
    xu_id = { .type = xu_type & 0xF, .vnc_id = vcore->vtpb }
    rc = xu_queue_init(&xu->work_queue, xu_id, capacity)   // §1
    if rc: return rc
    xu->vcore         = vcore
    xu->health_status = XU_STATUS_HEALTHY
    xu->has_work_efd  = eventfd(0, 2049)               // 2049 = EFD_NONBLOCK(0x800) | EFD_SEMAPHORE(0x1)
    if xu->has_work_efd == -1: { base_destroy_one(xu); return NRT_FAILURE }
    if pthread_mutex_init(&xu->submit_work_lock, 0): { log; base_destroy_one(xu); return NRT_FAILURE }
    if pthread_mutex_init(&xu->mark_comp_lock, 0):   { log; base_destroy_one(xu); return NRT_FAILURE }
    xu->initialized = 1
    return NRT_SUCCESS
```

> **QUIRK —** `has_work_efd` is created with `EFD_SEMAPHORE` (the `2049` flag = `0x801`). That is what makes the wakeup *counted*: each `write(has_work_efd, 1)` from a submit adds one to the counter, and each `read(has_work_efd, 8)` in `tpb_xu_base_report_complete` subtracts exactly one. Without `EFD_SEMAPHORE`, a single read would drain the whole counter and the worker would process one event per *batch* of submits instead of one per submit. The submit-increment and complete-decrement must stay exactly paired, one per scheduled request — an underflowing read is treated as fatal (`abort()`, §5).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `tpb_xu_init` | `0xe7e50` | 486 | per-vNC: `base_init` + `unstage_lock` + `calloc` exec state; rollback on fail | HIGH |
| `tpb_xu_base_init` | `0xe7cf0` | 341 | `xu_queue_init`; `eventfd(0,2049)`; 2 mutexes | HIGH |
| `tpb_xu_get_by_vcore` | `0xe7b10` | 67 | `&tpb_xus[vcore->vtpb]`; NULL if `!initialized` | HIGH |
| `tpb_xu_get` | `0xe7b60` | 91 | `xu_id` → `&tpb_xus[HIBYTE]`; assert `type == COMPUTE` | HIGH |
| `tpb_xu_base_destroy_one` | `0xe7bc0` | — | `xu_queue_destroy`; destroy mutexes; `close(has_work_efd)`; zero | HIGH |

---

## 4. The Per-vNC Worker Thread

### Purpose

`kmgr_xu_worker_do_work` (`@0xe92c0`) is the body of every per-vNC worker pthread. Its job is to sleep until *some* owned XU has work, wake, run one completion step on the signalled XU, and loop — until a stop event tells it to exit. It owns no descriptor ring and fires no `ioctl`; it is purely the consumer side of the SPSC ring plus the driver of the [completion harvest](completion-engine.md). The wakeup mechanism is `epoll` over two kinds of fd: a worker-private *stop* eventfd and each owned XU's `has_work_efd`.

### Entry Point

```text
kmgr_init
  └─ kmgr_xu_workers_init (0xe9a60)              ── one pthread per vNC
       └─ pthread_create(kmgr_xu_worker_do_work, &workers[v])
            └─ epoll_create1(0)
            └─ epoll_ctl(ADD, stop_thread_efd,    EPOLLIN|EPOLLEXCLUSIVE)   ── tag 0
            └─ [per owned XU] epoll_ctl(ADD, has_work_efd, EPOLLIN)         ── tag 1
            └─ loop: epoll_wait → tag 0 ? exit : tpb_xu_step(xu)
```

### Algorithm — the worker loop

The `epoll` registration uses a small 16-byte record per fd, `{u32 tag; XU* xu}`, stored in `event.data.ptr`. The stop fd carries `tag = 0` (its `xu` slot is the dummy `v29`); each XU's `has_work_efd` carries `tag = 1` and the XU pointer. On wakeup the loop dispatches on the tag.

```c
void* kmgr_xu_worker_do_work(kmgr_xu_worker_t *worker):              // 0xe92c0
    epfd = epoll_create1(0)
    if epfd == -1: { log("Failed to initalize epoll fd"); abort() }   // sic: source typo "initalize"

    // ---- register the STOP fd, EXCLUSIVE so a shared stop fd wakes one worker ----
    stop_rec = { .tag = 0, .xu = NULL }                              // tag 0 = exit
    assert worker->stop_thread_efd != -1                            // xu_worker.cc:0x60
    event = { .events = EPOLLIN|EPOLLEXCLUSIVE (0x80000001),
              .data.ptr = &stop_rec }
    if epoll_ctl(epfd, EPOLL_CTL_ADD, worker->stop_thread_efd, &event): { log; abort() }

    // ---- register every owned XU's has_work_efd ----
    for i in 0 .. worker->owned_tpb_xu_count - 1:
        xu = worker->owned_tpb_xus[i]
        assert xu != NULL                                           // xu_worker.cc:0x6B
        xu_rec[i] = { .tag = 1, .xu = xu }                          // tag 1 = step this XU
        ev = { .events = EPOLLIN, .data.ptr = &xu_rec[i] }
        if epoll_ctl(epfd, EPOLL_CTL_ADD, xu->base.has_work_efd, &ev):
            log("Failed to add TPB XU 0x%x to worker epoll"); abort()

    // ---- the wait/dispatch loop ----
    while true:
        n = epoll_wait(epfd, events, 9, -1)                        // up to 9, infinite timeout
        if n == -1: { log_warn("Epoll returned invalid status"); continue }   // EINTR-tolerant
        for e in events[0 .. n-1]:
            nlog_coalescing_init_thread()                          // per-iteration nlog setup
            rec = e.data.ptr
            if rec->tag == 0:                                      // STOP event
                goto exit_thread
            // rec->tag == 1 (the only other produced value; see CORRECTION)
            xu = rec->xu
            if tpb_xu_step(xu):                                    // <== drive completion (§5)
                log("Failed to step TPB Execution Unit on core %u", xu->base.vcore->vtpb)

exit_thread:
    close(epfd)
    return 0
```

> **CORRECTION (XU-1) —** the decompiler renders the dispatch as `if (rec->tag != 1) kmgr_xu_worker_do_work();` — an apparent self-recursion. This is a misrender of a `jne abort` (the unreachable default arm of a two-value switch): the `tag` field is only ever written `0` (stop) or `1` (XU) at registration time, so the `!= 1` branch is dead. There is no recursion. Verified against the disasm at the dispatch site: the only two tags produced are `0` and `1`, and the "recursive call" target is the cold abort path, not the function entry.

> **QUIRK —** the stop fd is registered `EPOLLIN|EPOLLEXCLUSIVE` (`0x80000001`), but each XU's `has_work_efd` is registered plain `EPOLLIN`. `EPOLLEXCLUSIVE` matters only if multiple workers share one fd; here each worker has its own `epoll` instance and its own stop fd, so the flag is defensive. The functional contrast that *does* matter is `has_work_efd`'s `EFD_SEMAPHORE` (§3): the worker's `read` in `report_complete` consumes one count, so `epoll` re-arms and the worker processes exactly one queued execution per wakeup, never starving or batching.

> **NOTE —** the worker is **not** detached. `kmgr_xu_workers_init` creates joinable threads (`pthread_create` with default attrs), and `kmgr_xu_workers_destroy` `pthread_join`s each one after writing the stop fd. A reimplementer must keep the threads joinable so shutdown can deterministically reclaim them; a detached design would race teardown against in-flight `tpb_xu_step` calls.

### Worker container and lifecycle

The workers live in a file-local static array, one element per vNC. `kmgr_xu_workers_init` (`@0xe9a60`) builds them; `kmgr_xu_workers_destroy` (`@0xe9930`) tears them down.

#### Struct: `kmgr_xu_worker_t` (88 bytes, ord 13206)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `thread` | `+0x00` | `pthread_t` | the worker pthread handle (joined at destroy) | HIGH |
| `stop_thread_efd` | `+0x08` | `int` | `eventfd(0, 0)` — **blocking, non-semaphore**; written to stop the thread | HIGH |
| `owned_tpb_xus` | `+0x10` | `tpb_execution_unit_t*[8]` | XUs this worker drives; init binds slot `[0]` only | HIGH |
| `owned_tpb_xu_count` | `+0x50` | `size_t` | length of the owned array (`1` in this build) | HIGH |

File-local statics: `_ZL12worker_count` (`@0xc88ac0`, `size_t`) and `_ZL7workers` (`@0xc88ae0`, `kmgr_xu_worker_t[]`, stride 88).

```c
NRT_STATUS kmgr_xu_workers_init(uint32_t vnc_count):                // 0xe9a60
    worker_count = 0
    for v in 0 .. vnc_count - 1:
        w = &workers[v]
        vcore = vtpb_get_virtual_core(v); assert vcore != NULL       // xu_worker.cc:0xC3
        w->stop_thread_efd = eventfd(0, 0)                           // flags 0 = blocking, NOT semaphore
        if w->stop_thread_efd == -1: { close; zero; kmgr_xu_workers_destroy(); return NRT_FAILURE }
        w->owned_tpb_xus[0] = tpb_xu_get_by_vcore(vcore)
        assert w->owned_tpb_xus[0] != NULL                           // xu_worker.cc:0xB0
        w->owned_tpb_xu_count++
        if pthread_create(&w->thread, 0, kmgr_xu_worker_do_work, w): // joinable
            { close stop fd; zero slot; kmgr_xu_workers_destroy(); return NRT_FAILURE }
        worker_count++
    return NRT_SUCCESS

void kmgr_xu_workers_destroy():                                     // 0xe9930
    for i in 0 .. worker_count - 1:                                 // phase 1: signal STOP
        if write(workers[i].stop_thread_efd, &one, 8) == -1:
            log("Failed to post stop to XU worker %lu. errno: %u"); abort()   // fatal
    for i in 0 .. worker_count - 1:                                 // phase 2: join + close
        pthread_join(workers[i].thread, 0)
        close(workers[i].stop_thread_efd); workers[i] = {0}
    worker_count = 0
```

> **GOTCHA —** the stop fd is `eventfd(0, 0)` — **no** `EFD_SEMAPHORE`, **no** `EFD_NONBLOCK` — whereas `has_work_efd` is `eventfd(0, 2049)`. The two eventfds in this layer are deliberately asymmetric: `has_work_efd` is a *counted* wakeup (one increment per submit, consumed one at a time), while `stop_thread_efd` is a *latch* (one write, the worker exits and never reads it). A reimplementation that makes the stop fd a semaphore is still correct (the worker exits on the first event regardless), but one that makes `has_work_efd` a non-semaphore latch will collapse N submits into a single step and lose completions.

> **CORRECTION (XU-2) —** an earlier scan described the XU workers as "detached pthreads." They are **joinable**: `kmgr_xu_workers_destroy` `pthread_join`s each worker after writing its stop fd, and a failed stop-fd `write` is fatal (`abort()`). The detached characterization was incorrect; the deterministic stop→join→close sequence is the design.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `kmgr_xu_worker_do_work` | `0xe92c0` | 1639 | per-vNC `epoll` loop; tag dispatch → `tpb_xu_step` | HIGH |
| `kmgr_xu_workers_init` | `0xe9a60` | 664 | one joinable pthread per vNC; bind XU; stop eventfd | HIGH |
| `kmgr_xu_workers_destroy` | `0xe9930` | 295 | write stop fd (abort on fail); `pthread_join`; close | HIGH |

---

## 5. The Worker Step: Wait → Dispatch → Complete → kmetric

### Purpose

`tpb_xu_step` (`@0xe8940`) is what the worker runs per `has_work` event. It is the consumer half of the entire XU lane: peek the head slot, dispatch on the XU's health and the work item's type, drive the [completion state machine](completion-engine.md) one non-blocking step, and on `DONE` fold per-TPB latency/error/status into the NDS counters, release any deferred model staging, free the work item, and report completion (which pops the slot and wakes the submitter). It runs entirely on the worker thread; nothing in it blocks on the kernel.

### Algorithm

```c
NRT_STATUS tpb_xu_step(tpb_execution_unit_t *xu):                   // 0xe8940
    // ---- WAIT side already done by epoll; PEEK the head slot ----
    rc = xuq_worker_peek_current_exec(&xu->base.work_queue, &exec_info)
    if rc == NRT_QUEUE_EMPTY:
        log("Execution Unit[%x] nothing to step"); return NRT_SUCCESS    // spurious wake
    assert rc == NRT_SUCCESS                                        // tpb_xu.cc:0x281 ("false")
    info = exec_info->exec_info                                     // tpb_execution_info_t*

    // ---- DISPATCH on health, then on request type ----
    if xu->base.health_status == XU_STATUS_FATAL_SHUTDOWN:
        log("…in fatal shutdown, stepping past [%lx]")
        exec_status = NRT_EXEC_UNIT_UNRECOVERABLE                   // 101 — drain, do not run
    else if info->type == STOP (1):
        log("…received shutdown request")
        rc = dlr_wait_model_stop_request(xu->base.vcore)
        if rc: { xu->base.health_status = FATAL_SHUTDOWN; exec_status = rc }
    else:   // info->type == EXECUTE (0)
        assert !nrt_gconf()->implicit_async_mode                   // tpb_xu.cc:0x294 — XU vs pool exclusive
        if !xu->current_exec_initialized:                          // first visit to this request
            assert info->type == EXECUTE                           // tpb_xu.cc:0x248 (tpb_xu_init_current_exec)
            exec_request_init_state(xu->current_exec_state, …)     // resolve mla/tpb, ref models
            xu->current_exec_initialized = 1
            nrt_sys_trace_new_event(open, 19, …)                   // execute span open
        v = exec_request_progress_one_step(xu->current_exec_state) // <== ONE non-blocking step
        if v == 2: { log("degrading…"); xu->base.health_status = FATAL_SHUTDOWN }   // error → poison
        else if v != 0: return NRT_SUCCESS                         // v == 1: in flight, epoll re-arms

    // ---- DONE: per-TPB NDS metrics (EXECUTE only) ----
    if info->type == EXECUTE:
        clock_gettime(MONOTONIC, &done_ts)
        e2e = get_timespec_delta(&done_ts, &info->submission_ts)   // info + 7 qword (+0x38)
        for i in 0 .. vcore->num_tpbs - 1:
            dev_lat = tdrv_timestamp_to_microseconds(info->resp[i].device_ts)
            nc_lat  = tdrv_timestamp_to_microseconds(info->resp[i].nc_ts)
            tpb_idx = vcore->tpbs[i].device_tpb_idx
            kmetric_update_nds_exec_latencies(info->device_id, tpb_idx, e2e, e2e, dev_lat, nc_lat, …)  // 0xe0fe0
            kmetric_update_nds_error_stats   (info->device_id, tpb_idx, &info->err[i])                 // 0xe0f60
            kmetric_update_nds_exec_stats    (info->device_id, tpb_idx, info->status[i])               // 0xe0d30
        nrt_sys_trace_new_event(close, 19, …)                      // execute span close

    // ---- deferred model unstage under unstage_lock ----
    pthread_mutex_lock(&xu->unstage_lock)
    assert tpb_count ∈ [1, MAX_VIRTUAL_TPB_CORE_SIZE]              // tpb_xu.cc:0x2EF
    if info->type == STOP:  for i: { defer[i] = xu->deferred_unstage_ref[i]; clear last_exec_model[i] }
    else (EXECUTE):         for i: { xu->last_exec_model[i] = mod->h_nn; defer[i] = swap(deferred_unstage_ref[i], NULL) }
    pthread_mutex_unlock(&xu->unstage_lock)
    if defer[0]: kmgr_unstage_kelf_model(defer[0])                 // release deferred model staging
    if tpb_count == 2 && defer[1]: kmgr_unstage_kelf_model(defer[1])

    // ---- cleanup + report ----
    if xu->current_exec_initialized: { exec_request_cleanup_state(xu->current_exec_state); xu->current_exec_initialized = 0 }
    if info->type == EXECUTE:
        if info->resources: kmgr_exec_resources_free(&info->resources)
        if info->mod_ref:   nn_ref_decrement(info->mod_ref)
    free(info)
    tpb_xu_base_report_complete(&xu->base, exec_info->seq_id, exec_info->exec_status, exec_status)
    return NRT_SUCCESS
```

### The completion signal

`tpb_xu_base_report_complete` (`@0xe8800`) is the seam where the worker hands the result back to the blocked submitter. It consumes one `has_work_efd` count, publishes the status through the caller's out-pointer, then pops the slot (which signals `mark_comp_efd`) under `mark_comp_lock`:

```c
void tpb_xu_base_report_complete(tpb_xu_base_t *xu, seq_id_t seq,
                                 NRT_STATUS *status_output, NRT_STATUS exec_status):   // 0xe8800
    if read(xu->has_work_efd, &one, 8) == -1:          // consume EXACTLY ONE semaphore count
        log("Failed to pop work from has work efd on Execution Unit[%x]"); abort()     // fatal
    if status_output: *status_output = exec_status     // publish the result the caller reads
    pthread_mutex_lock(&xu->mark_comp_lock)
    rc = xuq_worker_pop_current_exec(&xu->work_queue)  // exec_head++; write(mark_comp_efd,1) → wake submitter
    pthread_mutex_unlock(&xu->mark_comp_lock)
    if rc: { log("[XU %x] Failed to complete request 0x%lx"); xu->health_status = FATAL_SHUTDOWN }
    log_debug("Completed with status %s, Execution Unit[%x]", nrt_get_status_as_str(exec_status))
```

> **QUIRK —** there are **two** eventfds and they flow in opposite directions: the submit path `write(has_work_efd, 1)` wakes the worker's `epoll`; the worker `read(has_work_efd, 8)` here consumes that wakeup; then `xuq_worker_pop_current_exec` `write(mark_comp_efd, 1)` wakes the submitter blocked in `read(mark_comp_efd, 8)` ([submit path §6](submit-path.md#the-submit-boundary-sync)). Both are userspace-to-userspace — the kernel never signals completion on this path. A `read(has_work_efd)` returning `-1` is **fatal** (`abort()`): it means the semaphore counter underflowed, i.e. the submit-increment / complete-decrement pairing is broken, and the work-queue accounting can no longer be trusted.

> **QUIRK —** the `mark_comp_efd` write happens under `mark_comp_lock`, but the `exec_head` bump inside `xuq_worker_pop_current_exec` is *also* under that same lock. The lock does not protect the SPSC cursor (that is single-consumer by construction) — it serializes the pop against a concurrent `tpb_xu_base_get_comp_efd` (`@0xe8430`), which under the same lock installs a `mark_comp_efd` into a not-yet-completed slot. Without it, a waiter could install its fd into a slot the instant after the worker checked it was `-1`, and the completion signal would be lost. The lock closes that install-vs-signal race; it is not a ring lock.

> **GOTCHA —** on `EXECUTE`, the per-TPB loop runs `vcore->num_tpbs` iterations (1 or 2 for a 2-core LNC), feeding all three kmetric updaters per TPB. On `STOP`, the metric loop is skipped entirely — a stop request carries no inference timings. A reimplementation that runs the latency loop on a STOP item will read uninitialized `info->resp[]` and corrupt the NDS counters. The type check (`info->type == EXECUTE`) gates the whole metric + resource-free block.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `tpb_xu_step` | `0xe8940` | 1781 | peek; health/type dispatch; drive harvest; kmetric; unstage; report | HIGH |
| `tpb_xu_base_report_complete` | `0xe8800` | 308 | `read(has_work_efd)`; pop slot; signal `mark_comp_efd`; publish status | HIGH |
| `tpb_xu_base_get_comp_efd` | `0xe8430` | — | install/dup a waiter's `mark_comp_efd` under `mark_comp_lock`; pooled-only-for-sync | HIGH |
| `kmetric_update_nds_exec_latencies` | `0xe0fe0` | 129 | 4 latency counters via `nds_set_nc_counter` (ords `0xE`,`0xD`,`0xF`,`0x15`) | HIGH |
| `kmetric_update_nds_error_stats` | `0xe0f60` | 118 | per `kbl_infer_errors` flag, indices 5..8 → `nds_increment_nc_counter(ord = flag+31)` | HIGH |
| `kmetric_update_nds_exec_stats` | `0xe0d30` | 552 | `NRT_STATUS` → NDS ordinal switch → `nds_increment_nc_counter` | HIGH |
| `exec_request_progress_one_step` | `0x263330` | — | the 4-state NQ harvest ([completion engine §1](completion-engine.md)) | HIGH |

> **NOTE —** the three `kmetric_*` updaters are *leaf shims* with no embedded source paths — they map an `NRT_STATUS` / `kbl_infer_errors` bitset / latency tuple onto NDS (Neuron DataStore) counter mutations via `tdrv_get_nds_instance(nd)`. `exec_stats` switches the status code onto an ordinal (`SUCCESS→1`, `COMPLETED_WITH_ERR→2`, `TIMEOUT→4`, `QUEUE_FULL→6`, `HW_ERROR→11`, `FAILURE→16`, `INVALID→19`, `OOB→30`, `HBM_UE→32`, `NC_UE→33`); `error_stats` counts only `infer_error_flags` indices 5..8 (ordinal `flag+31`); `latencies` writes four `nds_set_nc_counter` slots. The numeric ordinals are HIGH (read from code), but their human counter *names* are not string-resolvable in this binary — they are reported as raw indices into the NDS counter table.

---

## 6. Submit and Shutdown Producers

### Purpose

The producer side of the ring is owned by [the submit path](submit-path.md); this section pins only the two in-cell producers — the work-item allocators `tpb_xu_schedule_exec` / `tpb_xu_schedule_stop` — and the shutdown drain (`tpb_xu_shutdown`) that quiesces the ring before teardown. The funnel `tpb_xu_schedule_request` (which holds `submit_work_lock`, stages, doorbells, and calls `xuq_client_submit` + `write(has_work_efd)`) is documented in [submit path §4](submit-path.md#4-the-submit-funnel-and-the-per-tpb-loop).

### The work item: `tpb_execution_info_t` (208 bytes, `0xD0`)

The opaque payload pointed to by a ring slot's `exec_info`. `tpb_xu_schedule_exec` `calloc`s it with `type = EXECUTE`; `tpb_xu_schedule_stop` with `type = STOP`. The byte-level interior (the `infer_resp_t` at `+0x28`, the per-TPB `kbl_infer_errors` stride) is owned by [the ABI page](xu-queue-abi.md); the fields the worker reads in §5 are the type (`+0x00`), the model ref (`+0x08`), the resources (`+0x10`), the submission timestamp (`+0x38`), `device_id` (`+0x90`), `device_tpb_idx` (`+0x92`), and `sync_exec` (`+0xC8`).

### Shutdown drain

`tpb_xu_shutdown` (`@0xe9140`) quiesces every XU before destroy. It flags every initialized unit, drains the in-flight queue, schedules a STOP on each, then drains again — using `wait_exec_queue_empty` to spin on the worker cursor:

```c
void tpb_xu_shutdown():                                            // 0xe9140
    for each initialized XU:  lock submit_work_lock; set shutdown flag; unlock
    t0 = time_utils_current_timestamp_ns()
    for each initialized XU:  wait_exec_queue_empty(&xu->base, t0)  // drain in-flight
    for each initialized XU:  if tpb_xu_schedule_stop(xu): log("Failed to schedule model stop request")
    t1 = time_utils_current_timestamp_ns()
    for each initialized XU:  wait_exec_queue_empty(&xu->base, t1)  // drain the STOPs

void wait_exec_queue_empty(tpb_xu_base_t *xu, uint64_t start_ts):  // 0xe7a60
    n = 1
    while xu->work_queue.scheduled_head != xu->work_queue.exec_head:   // in-flight on device
        if 120e9 * n < now() - start_ts: { n++; log("Waiting for queue to empty…") }   // warn every 120s
        usleep(100000)                                             // 100 ms
```

> **QUIRK —** `wait_exec_queue_empty` polls `scheduled_head != exec_head` — the *released-to-HW* region, not `scheduled_tail != exec_head`. It waits for everything **triggered** to drain, but does not block on slots that were submitted-but-never-triggered. That is correct for shutdown because the submit funnel always triggers immediately after submit under one lock; the gap between the two cursors is transient. The warn fires every 120 s but the drain **never times out** — like the unload ref-drain ([kmgr facade §4](kmgr-facade.md#4-the-unload-state-machine)), a wedged execution wedges shutdown rather than risk tearing down a ring the device still writes.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `tpb_xu_schedule_exec` | `0xe8040` | — | `calloc(0xD0)` EXECUTE item; fill; → funnel ([submit path §4](submit-path.md)) | HIGH |
| `tpb_xu_schedule_stop` | `0xe8320` | — | `calloc(0xD0)` STOP item; → funnel | HIGH |
| `tpb_xu_shutdown` | `0xe9140` | 372 | flag workers; drain; STOP every XU; drain again | HIGH |
| `wait_exec_queue_empty` | `0xe7a60` | 175 | spin on `scheduled_head != exec_head`; `usleep(100ms)`; warn every 120s | HIGH |
| `tpb_xu_model_unstage` | `0xe9040` | 248 | defer model unstage to next exec finish, or unstage now on FATAL | HIGH |

---

## 7. The Implicit-Async Pool — by Delta

### Purpose

There is a *second* worker model in the runtime: the **implicit-async pool** (`kmgr_exec_worker_do_work` `@0xe6310`, TU `kmgr/dlr.cpp`), selected when the process-global `async_exec_workers` (`@0xc5d8c8`) is non-NULL. It does **not** use the SPSC ring of this page. A reimplementer must not confuse the two; this section pins the deltas so the boundary is unambiguous. The pool is owned by the [kmgr facade](kmgr-facade.md) (which routes to it) and is documented in full there; only its contrast with the XU model belongs here.

| Axis | XU model (this page) | Implicit-async pool |
|---|---|---|
| Worker entry | `kmgr_xu_worker_do_work` `@0xe92c0` | `kmgr_exec_worker_do_work` `@0xe6310` |
| Wakeup | `epoll` over `has_work_efd` (eventfd, `EFD_SEMAPHORE`) + stop fd | `std::condition_variable` + `sem_post`/`sem_wait` |
| Queue | lock-free SPSC `xu_queue_t` (256 B, 3 cursors) | `std::deque` under `work_queue_lock` |
| Submit fn | `tpb_xu_schedule_request` `@0xe7540` (sync funnel) | `kaew_post_request` `@0xe5cd0` (inline stage under worker lock) |
| Init | `kmgr_xu_workers_init` `@0xe9a60` | `kmgr_async_exec_init` `@0xe7020` |
| Completion | `mark_comp_efd` wakes the blocked sync `read` | callback (`nrt_register_async_exec_callback`) |
| Backpressure | ring `capacity` (`NRT_QUEUE_FULL`) | `sem_wait(inflight_limit_sema)` |
| Concurrency | one XU + one worker per vNC | one worker per vNC; deque of `kmgr_async_exec_req` (56 B) |

> **GOTCHA —** the two models are **mutually exclusive at runtime**: `tpb_xu_step` (§5) asserts `!nrt_gconf()->implicit_async_mode` before it runs a request, and `async_exec_workers` is the single global that selects the mode for the whole process at `kmgr_init` ([kmgr facade §2](kmgr-facade.md#2-the-execute-fork)). A process is *either* XU-mode *or* pool-mode for all models — never both. A reimplementation that wires the SPSC ring and the deque to the same submitter, or that lets a request reach `tpb_xu_step` in implicit-async mode, trips the assert. The explicit-async API (`NRT_3.0.0`, `nrta_execute_schedule`) is a *third* surface that reuses the XU ring (it goes through `tpb_xu_schedule_request`, just never blocks) — so the XU ring serves both the plain synchronous path and the explicit-async API, while the implicit pool is the only consumer that bypasses it.

---

## 8. Reimplementation Checklist

An XU work-queue + worker reimplementation is correct when it reproduces, in order:

1. **The ring** — 256-byte header, 32-byte slots, three cursors at `+0x40`/`+0x80`/`+0xC0` each on its own 64-byte cache line; all three start at **1**; full = `scheduled_tail − exec_head ≥ capacity`.
2. **The fences** — producer writes the slot body **then** `LOCK XADD scheduled_tail`; trigger `LOCK XADD scheduled_head` after stage+doorbell; worker captures `mark_comp_efd`/`efd_from_pool` **then** `LOCK XADD exec_head`. Keep the peek-time `seq_num == cursor` asserts.
3. **The seq_id** — `(cursor & 0xFFFFFFFFFFFF) | (xu_id.raw << 48)`; assert `scheduled_tail < 0xFFFFFFFFFFFE`; last-completed = `exec_head − 1`.
4. **The container** — one 576-byte XU per vNC in a 256-entry array indexed by `vcore->vtpb`; `has_work_efd = eventfd(0, EFD_NONBLOCK|EFD_SEMAPHORE)`; `submit_work_lock` + `mark_comp_lock`.
5. **The worker** — one **joinable** pthread per vNC; `epoll` over stop fd (`EPOLLIN|EPOLLEXCLUSIVE`, `eventfd(0,0)`) + each owned `has_work_efd` (`EPOLLIN`); 16-byte `{tag, xu}` record; tag `0` exit, tag `1` step.
6. **The step** — peek; dispatch on `health_status` then `info->type`; `EXECUTE` drives `exec_request_progress_one_step` (skip on FATAL/STOP); on `DONE` run the per-TPB kmetric loop (`num_tpbs` iterations, EXECUTE only); deferred unstage under `unstage_lock`; free; `report_complete`.
7. **The handshake** — `report_complete` `read(has_work_efd, 8)` (abort on `-1`) → publish status → pop slot under `mark_comp_lock` → `write(mark_comp_efd, 1)` (close fresh fds, keep pooled).
8. **Shutdown** — `tpb_xu_shutdown` flags, drains (`scheduled_head == exec_head`, 100 ms poll, never times out), STOPs every XU, drains again; `kmgr_xu_workers_destroy` writes every stop fd (abort on fail), `pthread_join`s, closes.

> **GOTCHA —** the XU ring and the implicit-async deque are two distinct execution models gated by `async_exec_workers`, mutually exclusive at runtime (`tpb_xu_step` asserts `!implicit_async_mode`). A reimplementer must build *one* of them per process, not both, and must route the explicit-async `NRT_3.0.0` API through the XU ring (not the pool). Folding the two into a single queue trips the exclusivity assert and corrupts the completion accounting.

---

## Related Components

| Component | Relationship |
|---|---|
| [The Submit Path](submit-path.md) | the producer side: `tpb_xu_schedule_request` fills the ring this page consumes; the doorbell + `device_exec_request_t` |
| [The Completion Engine](completion-engine.md) | the harvest `tpb_xu_step` drives; the `exec_request_progress_one_step` state machine and the NQ decode |
| [The xu_queue / device_exec_request ABI](xu-queue-abi.md) | the canonical byte layout of `xu_queue_t`, `xuq_exec_info_t`, and the `tpb_execution_info_t` work item |
| [KMGR Facade and Model DB](kmgr-facade.md) | routes a `kmgr_exec` to this XU ring (sync) or the implicit-async pool; owns `async_exec_workers` |

## Cross-References

- [The Submit Path (Bind → Stage → Doorbell)](submit-path.md) — the producer that calls `xuq_client_submit` + `write(has_work_efd)`; the funnel holding `submit_work_lock`
- [The Completion Engine (NQ Harvest & Error Decode)](completion-engine.md) — the `exec_request_progress_one_step` machine `tpb_xu_step` advances, and the `mark_comp_efd` write that unblocks the submitter
- [The xu_queue and device_exec_request ABI](xu-queue-abi.md) — the canonical wire layout of the ring header, the 32-byte slot, and the 208-byte work item this page references but does not re-derive
- [Exec Manager (KMGR): Facade and Model DB](kmgr-facade.md) — the `async_exec_workers` fork and the implicit-async pool this page contrasts in §7
- [Overview: the Hot Inference Path](overview.md) — where this XU lane sits in the end-to-end `nrt_execute` flow
