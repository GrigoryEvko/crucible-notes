# Reset Engine

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. This page owns exactly two files — `neuron_reset.c` (437 lines) and `neuron_reset.h` (130 lines) — both read in full; every function, struct, enum value, and constant below is transcribed verbatim. The arch-specific reset bodies it dispatches through (`v2/neuron_dhal_v2.c`, `v3/neuron_dhal_v3.c`) belong to [DHAL V2](dhal-v2.md) / [DHAL V3](dhal-v3.md); they are cited here only at the vtable boundary. Other driver versions renumber lines.*
> *Evidence grade: **Confirmed (source-anchored)** — the queue model, coalescing, and state machine are direct C, not reverse-engineered. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`neuron_reset` is an **asynchronous, single-kthread-per-device** reset engine. The familiar frame is a classic worker-thread/work-queue: callers never reset hardware on their own stack — they *enqueue a request* and (optionally) block on its result, while one dedicated kernel thread per device drains the queue and drives the real hardware reset. Userspace (through the cdev ioctl layer) and the PCI probe/remove paths submit two shapes of request — a **per-core "TPB" reset** scoped to a bitmap of NeuronCores, or a **whole-device reset** (`nc_map == NEURON_NC_MAP_DEVICE`, `0xffffffff`) — by appending a `struct neuron_reset_request` to a pending singly-linked list and waking the worker (`nr_start_ncs`, `neuron_reset.c:252`). The thread is named `"nd%d reset"` after the device index (`nr_create_thread`, `:197`).

The worker's defining behavior is **coalescing**: when several partial-core requests are queued back-to-back, the thread folds the whole contiguous run into one hardware reset by OR-ing their `nc_map`s together, so the silicon is reset once for the union of cores rather than once per request (`nr_reset_thread_fn`, `:97-114`). It then drives a fixed four-stage pipeline through the arch vtable — **initiate → wait-for-completion → DMA re-init → post-reset config** (`:119-152`) — and finally either updates `nd->device_state` (for internal driver-wide resets keyed `NEURON_RESET_REQUEST_ALL`) or migrates each coalesced request onto a *completed* doubly-linked list whose `ret` field its waiters poll (`:171-194`). Per-request state walks `STARTED(1) → {COMPLETED(2) | FAILED(3)}` (`neuron_reset.h:28-32`).

The hardware handshake itself (`nr_initiate_reset_via_fw`, `:381`) lives in this file but is *called by* the per-arch `nr_initiate_reset` callbacks, not by the worker directly: it pokes a reset trigger into BAR0 via `fw_io_initiate_reset` and polls `fw_io_is_reset_initiated` with periodic re-issue until a per-arch deadline (V2 = 120 s, V3 = 480 s). Concurrency is one mutex (`nd->nr.nr_lock`) guarding both queues, with `volatile` fields for cross-thread visibility and a `schedule()`-based busy-poll in `nr_wait` that the source itself flags as crude (`:336-339`, in-tree TODO).

For reimplementation, the contract is:

- **The request object and its two queues** — a `neuron_reset_request {request_id, nc_map, volatile ret, *next, *prev}`; a singly-linked pending FIFO drained by the worker and a doubly-linked completed list polled by waiters, both under one mutex.
- **The coalesce-then-drive worker loop** — snapshot the pending head/tail, fold a contiguous run of *partial* requests into one `nc_map`, run the four-stage arch pipeline once, stamp every coalesced request with the shared result.
- **The four-stage pipeline through the `ndhal_reset` vtable** — initiate, wait, `ndmar_init_ncs`, post-config — and the precise failure-branch bookkeeping (which branches bump the failure metric and which one does not).
- **The two completion disciplines** — `request_id == ALL` updates `device_state` and frees the request inline; every other request is handed to the completed list for a waiter to reap.

| | |
|---|---|
| **Owns** | `neuron_reset.c` (437 lines) · `neuron_reset.h` (130 lines), both read in full |
| **Engine** | one kthread per device, `"nd%d reset"` (`neuron_reset.c:202`), created by `nr_create_thread` (`:197`) |
| **Worker loop** | `nr_reset_thread_fn` (`:73`) — wait → snapshot → coalesce → drive → complete |
| **Submit** | `nr_start_ncs(nd, nc_map, request_id)` (`:252`) · `nr_start(nd)` (`:311`, full-device keyed by PID) |
| **Wait** | `nr_wait(nd, request_id, check)` (`:317`) — blocks until request leaves `STARTED` |
| **FW handshake** | `nr_initiate_reset_via_fw` (`:381`) — BAR0 trigger + poll, *called by* the arch vtable |
| **State machine** | `STARTED=1 → {COMPLETED=2, FAILED=3}` (`neuron_reset.h:28-32`) |
| **Drive vtable** | `ndhal->ndhal_reset.{nr_initiate_reset, nr_wait_for_reset_completion, nr_post_reset_config}` ([dhal-core](dhal-core.md)) |
| **Lock** | one `mutex nd->nr.nr_lock` guards **both** queues + ids (`neuron_reset.h:50`) |
| **Key constants** | retry-count 5 · poll 100 ms · device re-issue 30 s · TPB re-issue 10 s (`:32-34`, `reset.h:17`) |

---

## 1. The Request Object and the Two Queues

### Purpose

A reset is reified as a heap `struct neuron_reset_request` (`neuron_reset.h:34-40`) that lives from submit to completion. Submitters fill it and append it to a **pending** list; the worker dequeues it, drives the reset, then either frees it (internal driver-wide resets) or moves it to a **completed** list. The whole machine is five `volatile` pointers plus a mutex, embedded in `neuron_device` as `nd->nr` (`struct neuron_reset`, `neuron_reset.h:42-55`).

### Encoding

The request and the per-device control block, verbatim:

| Field | Type | Meaning | Decl |
|---|---|---|---|
| `request_id` | `uint32_t` | caller PID (`task_tgid_nr`) **or** `NEURON_RESET_REQUEST_ALL` (`0xffffffff`) for internal resets | `neuron_reset.h:35` |
| `nc_map` | `uint32_t` | core bitmap **or** `NEURON_NC_MAP_DEVICE` (`0xffffffff`) for whole-device | `:36` |
| `ret` | `volatile enum neuron_reset_state` | result/state, polled by waiters | `:37` |
| `next` | `volatile struct neuron_reset_request *` | pending = singly-linked FIFO; completed = forward link | `:38` |
| `prev` | `volatile struct neuron_reset_request *` | used **only** in the completed (doubly-linked) list | `:39` |

The control block `nd->nr` (`neuron_reset.h:42-55`) holds: `thread` (the kthread), `wait_queue` (worker sleeps here), `volatile bool stop` (cooperative-stop flag), the four queue pointers `req_pending_head/tail` + `req_cmpl_head/tail`, the `nr_lock` mutex, and two `uint64_t` jiffies stamps `reset_start_time` / `reset_end_time` (used by the reset-window heuristic, §6).

> **QUIRK —** `request_id` is overloaded as both an identity and a routing key. A normal request carries the submitting thread's PID (`nr_start`, `:311`), and a waiter finds *its* request by matching that PID (`nr_find_req`, `:234`). The sentinel `NEURON_RESET_REQUEST_ALL == NEURON_NC_MAP_DEVICE == 0xffffffff` (`reset.h:18`, `share/neuron_driver_shared.h`) marks an **internal** driver-wide reset that has no waiter — the worker frees it inline and writes `device_state` instead of enqueuing a completion (§4). A reimplementation must not assume every dequeued request will be reaped by a waiter; the `ALL` request is self-disposing.

### State machine

```text
   submit (nr_start_ncs)          worker drives pipeline
        │                                  │
        ▼                                  ▼
   STARTED(1) ──────────────► COMPLETED(2)   ← all four stages succeeded
        │                     FAILED(3)      ← any stage failed / -EINTR on unload
        └─ waiters spin on req->ret until it leaves STARTED (nr_wait)
```

`enum neuron_reset_state` (`neuron_reset.h:28-32`): `NEURON_RESET_STATE_STARTED = 1`, `COMPLETED = 2`, `FAILED = 3`. The enum has no terminal-cleanup state; the request's *memory* lifetime is orthogonal to its `ret` value and is governed by the completion discipline in §4.

### Considerations

The pending list is **singly** linked and drained FIFO; the completed list is **doubly** linked so a waiter can unlink its own node from the middle in O(1) (`nr_wait`, `:347-360`). Both share one mutex (`nr_lock`), so there is no lock ordering to get wrong — but it also means a long reset holds nothing: the worker drops the lock for the entire multi-second pipeline (§3) and only re-acquires it to dequeue/complete. `volatile` substitutes for memory barriers across the worker/submitter/waiter threads; the source comment treats this as a known-adequate-on-supported-arches shortcut, not a memory-model guarantee (MEDIUM — works in practice, not a portable contract).

---

## 2. Submit — `nr_start_ncs` / `nr_start`

### Purpose

`nr_start_ncs(nd, nc_map, request_id)` (`neuron_reset.c:252`) is the single enqueue path; `nr_start(nd)` (`:311`) is a one-line convenience that keys a whole-device reset by the caller's PID (`request_id = task_tgid_nr(current)`, `nc_map = NEURON_NC_MAP_DEVICE`). Both run on the *submitter's* stack, do the per-core teardown bookkeeping under lock, append a request, and wake the worker — they never touch hardware.

### Entry Point

```text
userspace ioctl (neuron_cdev.c:1737/1747/3562)   PCI probe (neuron_pci.c:165)
        │                                                │
        ▼                                                ▼
   nr_start_ncs(nd, nc_map, request_id)   ◄── nr_start(nd) keys request_id = PID (:311)
        ├─ [no_reset]  ndmar_init_ncs(DEVICE) + post_reset_config(true) + state=READY → return 0
        └─ [normal]    lock → dup-check → per-core teardown → kmalloc req → append tail → wake worker
```

### Algorithm

Annotated pseudocode modelling `nr_start_ncs` (`neuron_reset.c:252-309`):

```c
function nr_start_ncs(nd, nc_map, request_id):           // neuron_reset.c:252
    // SHORT-CIRCUIT: module param disables real HW reset but still re-inits the driver side.
    if (no_reset):                                       // :255
        ndmar_init_ncs(nd, NEURON_NC_MAP_DEVICE)         // :257  re-init all DMA rings
        ndhal->ndhal_reset.nr_post_reset_config(nd, true, no_reset)  // :259 post-config as success
        nd->device_state = NEURON_DEVICE_STATE_READY     // :261
        return 0

    mutex_lock(&nd->nr.nr_lock)                           // :266

    // DUP GUARD: a request already in flight (pending OR completed) for this id is a no-op.
    if (nr_find_req(nd, request_id) != NULL):             // :269  linear scan both queues
        mutex_unlock(&nd->nr.nr_lock)
        return 1                                          // :271  duplicate — caller treats as benign

    // WHOLE-DEVICE: flip device_state to RESET up front so concurrent ops bail.
    if (request_id == NEURON_RESET_REQUEST_ALL):          // :274
        nd->device_state = NEURON_DEVICE_STATE_RESET      // :275

    // PER-CORE TEARDOWN: for every core selected by nc_map, scrub its software state now,
    // so the post-reset re-init starts from a clean slate.
    for nc_idx in cores_set_in(nc_map):                   // :277
        nci_reset_state_nc(nd, nc_idx)                    // :278  scrub core control state
        nd->nc_model_started_count[nc_idx] = 0            // :280  no model is "running" post-reset
        nnq_destroy_nc(nd, nc_idx)                        // :281  tear down notification queues
        nsysfsmetric_inc_reset_req_count(nd, nc_idx)      // :283  bump per-core sysfs counter

    // ENQUEUE: heap the request and append to the pending FIFO tail.
    req = kmalloc(sizeof(struct neuron_reset_request), GFP_KERNEL)   // :286
    if (req == NULL):
        mutex_unlock(&nd->nr.nr_lock)
        return 1                                          // ENOMEM folded into the "1" return
    req->request_id = request_id                          // :291
    req->nc_map     = nc_map
    req->ret        = NEURON_RESET_STATE_STARTED          // :293
    req->next = req->prev = NULL
    append_tail(&nd->nr.req_pending_head, &nd->nr.req_pending_tail, req)  // :296-303

    mutex_unlock(&nd->nr.nr_lock)                          // :305
    wake_up_interruptible_sync(&nd->nr.wait_queue)         // :307  kick the worker
    return 0
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `nr_start_ncs` | `:252-309` | the enqueue path; per-core teardown + append + wake | HIGH |
| `nr_start` | `:311-315` | whole-device convenience, `request_id = task_tgid_nr(current)` | HIGH |
| `nr_find_req` | `:234-250` | "expects `nr_lock` held"; linear scan pending **then** completed for `request_id` | HIGH |
| `nci_reset_state_nc` | boundary (`neuron_core`) | scrub per-core control state | HIGH |
| `nnq_destroy_nc` | boundary (`neuron_nq.h:83`) | tear down per-core notification queues | HIGH |
| `nsysfsmetric_inc_reset_req_count` | boundary (sysfs) | per-core reset-request counter | HIGH |

### Considerations

The dup guard returns `1`, not an error code, and so does `kmalloc` failure — both are folded into the same non-zero return that the cdev caller treats as "nothing to wait on" (so `nr_wait(..., check=true)` later returns success for a request that was never enqueued). The per-core teardown is done **at submit time on the submitter's stack**, before the hardware is touched, so a model is marked stopped and its NQs are gone the instant a reset is *requested*, not when it *completes*. For a whole-device reset, `nc_map == 0xffffffff` makes the `cores_set_in` loop iterate every bit; `device_state = RESET` (`:275`) is the gate that makes concurrent DMA/exec paths abort early.

---

## 3. The Worker Loop — `nr_reset_thread_fn`

### Purpose

One kthread per device runs `nr_reset_thread_fn` (`neuron_reset.c:73`). It is the only consumer of the pending list and the only driver of hardware reset. Each iteration: sleep until work or stop; snapshot the pending run under lock; coalesce a contiguous run of partial requests into one `nc_map`; drive the four-stage arch pipeline once; stamp the shared result onto every coalesced request; then dequeue and complete under lock.

### Entry Point

```text
nr_create_thread (neuron_reset.c:197)
  └─ kthread_run(nr_reset_thread_fn, nd, "nd%d reset", device_index)   ── :202
       └─ loop forever:
            wait_event_interruptible(req_pending_head != NULL || nr.stop)   ── :80
            ├─ stop?  → break out, thread exits
            ├─ snapshot + COALESCE pending run               ── :88-114
            ├─ drive  initiate → wait → dma-reinit → post    ── :117-152
            └─ complete: device_state update OR completed-list append ── :171-194
```

### Algorithm

The coalesce-and-drive core, modelling `nr_reset_thread_fn` one iteration (`neuron_reset.c:73-194`):

```c
function nr_reset_thread_fn(nd):                          // neuron_reset.c:73
  while (!kthread_should_stop()):
    // SLEEP until a request lands or teardown requests stop.
    wait_event_interruptible(nd->nr.wait_queue,
                             nd->nr.req_pending_head != NULL || nd->nr.stop)   // :80
    if (nd->nr.stop): break                               // :83  cooperative exit

    // SNAPSHOT the current pending run under the lock, then release it for the
    // entire (multi-second) drive — submitters keep appending to the tail meanwhile.
    mutex_lock(&nd->nr.nr_lock)                            // :88
    first = nd->nr.req_pending_head                        // :90
    last  = nd->nr.req_pending_tail                        // :91
    mutex_unlock(&nd->nr.nr_lock)                          // :93

    nc_map   = first->nc_map                               // :97  base map = head request
    coal_cnt = 1

    // COALESCE: fold a contiguous run of PARTIAL requests into one reset.
    // A whole-device request (ALL) is never coalesced — it already covers everything.
    if (first->request_id != NEURON_RESET_REQUEST_ALL && first->next != NULL):  // :99
        for iter in ITER_COAL_REQS(first .. last):        // :46 macro; loop :99-114
            nc_map  |= iter->nc_map                        // :109  union the core bitmaps
            coal_cnt += 1
    else:
        last = first                                       // :116  no coalesce — drive head alone

    // DRIVE the four-stage pipeline ONCE for the coalesced nc_map.
    state = NEURON_RESET_STATE_STARTED
    nd->nr.reset_start_time = get_jiffies_64()             // :118  stamp window start

    // ── stage 1: INITIATE (arch vtable → fw handshake) ──────────────────────────
    ret = ndhal->ndhal_reset.nr_initiate_reset(nd, nc_map) // :119  BOUNDARY [dhal-v2/v3]
    if (ret):
        state = NEURON_RESET_STATE_FAILED                  // :122
        // -EINTR here == "interrupted by driver unload"
        nr_call_post_reset_config(nd, nc_map, false)       // :124
        nsysfsmetric_inc_reset_fail_count(nd)              // :126
    else:
        // ── stage 2: WAIT FOR COMPLETION (arch vtable → poll FW_STATUS) ──────────
        ret = ndhal->ndhal_reset.nr_wait_for_reset_completion(nd)  // :130 BOUNDARY
        if (ret):
            state = NEURON_RESET_STATE_FAILED
            nr_call_post_reset_config(nd, nc_map, false)   // :133
            nsysfsmetric_inc_reset_fail_count(nd)          // :136
        else:
            // ── stage 3: DMA RE-INIT for the reset cores ────────────────────────
            ret = ndmar_init_ncs(nd, nc_map)               // :138  BOUNDARY [dma-rings]
            if (ret):
                state = NEURON_RESET_STATE_FAILED
                nr_call_post_reset_config(nd, nc_map, false)   // :142
                nsysfsmetric_inc_reset_fail_count(nd)      // :144
            else:
                // ── stage 4: POST-RESET CONFIG (full-device only; §5) ───────────
                ret = nr_call_post_reset_config(nd, nc_map, true)  // :147
                if (ret):
                    state = NEURON_RESET_STATE_FAILED      // :148
                    // NOTE: this branch does NOT bump the fail metric — see CORRECTION
                else:
                    state = NEURON_RESET_STATE_COMPLETED   // :151

    nd->nr.reset_end_time = get_jiffies_64()               // :154  stamp window end

    // COMPLETE under lock — dequeue the coalesced run from pending.
    mutex_lock(&nd->nr.nr_lock)                            // :171
    nd->nr.req_pending_head = last->next                   // :174  unlink run [first..last]
    fixup_head_prev_and_tail(nd)                           // :175-180

    if (first->request_id == NEURON_RESET_REQUEST_ALL):    // :183  internal driver-wide reset
        nd->device_state = (state == NEURON_RESET_STATE_COMPLETED)
                           ? NEURON_DEVICE_STATE_READY
                           : NEURON_DEVICE_STATE_INVALID   // :184-186
        kfree(first)                                       // :188  self-dispose, no waiter
    else:                                                  // :190  normal: hand to waiters
        append_run_to_completed(nd, first, last)           // doubly-linked
        for iter in ITER_COAL_REQS(first .. last):         // :190
            iter->ret = state                              // every coalesced request shares the result
    mutex_unlock(&nd->nr.nr_lock)                          // :194
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `nr_reset_thread_fn` | `:73-195` | the worker: wait → snapshot → coalesce → drive → complete | HIGH |
| `nr_create_thread` | `:197-207` | init lock + wait_queue, `kthread_run(..., "nd%d reset", idx)` | HIGH |
| `nr_call_post_reset_config` | `:65-72` | dispatch `nr_post_reset_config` **only** for `nc_map == DEVICE`; else return 0 | HIGH |
| `nr_stop_thread` | `:219-232` | idempotent teardown: `state=INVALID`, `stop=true`, wake, `kthread_stop`, free both queues | HIGH |
| `nr_free_req_queue` | `:209-217` | walk a singly-linked list, `kfree` each node (used for both queues at stop) | HIGH |
| `ITER_COAL_REQS` | `:46` (macro) | iterate the coalesced run `[first..last]` inclusive | HIGH |
| `ndmar_init_ncs` | boundary (`dma-rings`) | re-init DMA rings for the reset cores (stage 3) | HIGH |

### Considerations

**Coalescing is a contiguous-prefix fold, not a set merge.** The worker snapshots `first = head` and `last = tail` once (`:90-91`), then walks `[first..last]` OR-ing `nc_map`s (`:109`). Any request appended *after* the snapshot is left for the next iteration — it is not pulled into this reset even though it is now in the list, because `last` was fixed before the lock was dropped. This is correct: the worker is the sole consumer of the head, and submitters only append to the tail under the same lock, so the snapshot's `[first..last]` run is stable for the duration of the drive even though the tail advances behind it (HIGH — reasoning, no bug).

**A whole-device request short-circuits coalescing.** `if (first->request_id != ALL && first->next != NULL)` (`:99`) means an `ALL` request at the head is driven alone with `nc_map = 0xffffffff`; there is nothing to gain by folding partial requests into a reset that already covers every core.

> **GOTCHA —** the four failure branches are **not** symmetric in metric accounting. Stages 1, 2, and 3 each call `nsysfsmetric_inc_reset_fail_count` on failure (`:126`, `:136`, `:144`), but the stage-4 (`nr_call_post_reset_config`) failure branch sets `state = FAILED` *without* bumping the counter (`:147-149`). A reimplementation that wants uniform failure telemetry must add the missing increment; a verifier comparing the failure count against the failed-reset count will see an off-by-one whenever post-config is the failing stage. The source carries no comment justifying the asymmetry.

> **CORRECTION (K-RESET) —** an intuitive reading expects all four pipeline failure branches to behave identically. They do not: the post-config branch (`neuron_reset.c:147-150`) is the lone exception that omits `nsysfsmetric_inc_reset_fail_count`. Confidence MEDIUM on whether this is intentional (post-config failure may be considered a soft/non-reset failure) vs. a bug; flagged here verbatim rather than silently "corrected" in the pseudocode.

---

## 4. Wait and Complete — `nr_wait`

### Purpose

`nr_wait(nd, request_id, check)` (`neuron_reset.c:317`) is the waiter side, run on a *different* stack than the worker. It blocks until the matching request leaves `STARTED`, unlinks it from the completed list, reads its result, frees it, and returns `0` for `COMPLETED` / `1` for `FAILED`. The `check` flag controls whether a missing request is an error.

### Algorithm

Modelling `nr_wait` (`neuron_reset.c:317-364`):

```c
function nr_wait(nd, request_id, check):                  // neuron_reset.c:317
    if (no_reset): return 0                                // :320  nothing was enqueued

    mutex_lock(&nd->nr.nr_lock)
    req = nr_find_req(nd, request_id)                      // :234  scan pending + completed
    mutex_unlock(&nd->nr.nr_lock)

    if (req == NULL):                                      // request absent
        if (check): return 0                               // :325  idempotent "already done" OK
        pr_err("Invalid reset request id")                // :328
        return 1

    // BUSY-POLL the volatile ret until the worker stamps a terminal state.
    while (req->ret == NEURON_RESET_STATE_STARTED):        // :336
        schedule()                                         // :337  yield, then re-read
        if (signal_pending: SIGTERM/SIGKILL):              // :338
            return -EINTR                                  // abort the wait on fatal signal

    // REAP: unlink from the completed doubly-linked list, read result, free.
    mutex_lock(&nd->nr.nr_lock)
    unlink_from_completed(nd, req)                         // :347-358  prev/next fixups
    mutex_unlock(&nd->nr.nr_lock)

    ret = req->ret                                         // :361
    kfree(req)                                             // :362
    return (ret == NEURON_RESET_STATE_COMPLETED) ? 0 : 1   // :363
```

### Considerations

The completion discipline forks on `request_id`. An **internal** reset (`ALL`) is reaped by the *worker* itself — it never reaches the completed list; the worker `kfree`s it inline after writing `device_state` (`:188`). Every **other** request is left on the completed list for *its waiter* to reap in `nr_wait` (`:362`). So the request's memory is freed by exactly one of two parties, never both, selected by whether anyone is waiting.

> **GOTCHA —** `nr_wait` busy-polls `req->ret` via `schedule()` (`:336-339`) rather than blocking on a wait-queue or semaphore — the in-tree source carries a TODO admitting this should be replaced. Under a stuck reset the waiting thread will spin (yielding) until the worker terminally stamps the request or a fatal signal arrives. A faithful reimplementation can substitute a per-request completion (`struct completion` / `complete()` from the worker), but must preserve the `SIGTERM`/`SIGKILL` early-out (`:338`) so a killed userspace process is not wedged on a hung device.

---

## 5. The Four-Stage Drive Through the Arch Vtable

### Purpose

The worker is arch-neutral; the actual register pokes are reached through three `ndhal->ndhal_reset` callbacks (the vtable lives in [dhal-core](dhal-core.md), slot group `ndhal_reset`, `neuron_dhal.h:212`). This page owns the *fourth* helper they all funnel into — `nr_initiate_reset_via_fw` — which the arch `nr_initiate_reset` bodies call to do the BAR0 handshake.

### Entry Point

```text
nr_reset_thread_fn (:119)
  └─ ndhal_reset.nr_initiate_reset(nd, nc_map)        ── arch body [dhal-v2/v3]
       └─ nr_get_tpb_reset_map(nc_map, &lo[,&hi])     ── build FW reset bitmap (arch)
       └─ nr_initiate_reset_via_fw(nd, nc_map, lo, hi)── THIS file (:381)  BAR0 trigger + poll
            └─ fw_io_initiate_reset(bar0, dev, lo, hi) ── [fw-io]
            └─ fw_io_is_reset_initiated(bar0)          ── [fw-io]  poll
nr_reset_thread_fn (:130)
  └─ ndhal_reset.nr_wait_for_reset_completion(nd)     ── arch body: poll FW_STATUS DEVICE_READY
```

### Algorithm

The FW handshake hosted here, modelling `nr_initiate_reset_via_fw` (`neuron_reset.c:381-437`):

```c
function nr_initiate_reset_via_fw(nd, nc_map, tpb_lo, tpb_hi):   // neuron_reset.c:381
    is_device_reset = (nc_map == NEURON_NC_MAP_DEVICE)           // :388
    // re-issue cadence differs: a whole-device reset is rarer/heavier than a TPB reset.
    retry_interval  = is_device_reset ? NR_DEVICE_RESET_RETRY_INTERVAL   // 30000 ms (:32)
                                      : NR_TPB_RESET_RETRY_INTERVAL      // 10000 ms (:33)

    fw_io_initiate_reset(bar0, is_device_reset, tpb_lo, tpb_hi)  // :398  poke BAR0 trigger
    start = now()
    next_reissue = start + retry_interval

    // POLL until FW reports the reset took, re-issuing the trigger every retry_interval.
    while (now() < start + ndhal->ndhal_reset.initiate_max_wait_time):  // :432 deadline (V2 120s/V3 480s)
        if (nr_msleep_stoppable(nd, NR_RESET_POLL_INTERVAL)):    // :406  100 ms; nonzero ⇒ stop
            return -EINTR                                        // driver unload mid-poll
        if (fw_io_is_reset_initiated(bar0)):                     // :411  reset accepted
            nmetric_set_reset_time_metrics(nd, now() - start)    // :415  record latency
            return 0
        if (now() >= next_reissue):                              // :393/428  past re-issue point
            fw_io_initiate_reset(bar0, is_device_reset, tpb_lo, tpb_hi)  // :428 re-poke
            next_reissue = now() + retry_interval

    nmetric_increment_reset_failure_count(nd)                    // :435  deadline hit
    return -ETIMEDOUT
```

`nr_msleep_stoppable` (`:55-63`) is the interruptible sleep both the poll loop and the arch wait loops use: it sleeps up to `msec` but returns early (with remaining ms, treated as stop/`-EINTR`) if `nd->nr.stop` is set — this is how `nr_stop_thread` (`:219`) yanks the worker out of a multi-second reset during driver unload.

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `nr_initiate_reset_via_fw` | `:381-437` | BAR0 reset-trigger + poll/re-issue handshake; hosted here, called by arch vtable | HIGH |
| `nr_call_post_reset_config` | `:65-72` | stage-4 wrapper; dispatches `nr_post_reset_config` only for full-device resets | HIGH |
| `nr_msleep_stoppable` | `:55-63` | interruptible sleep, aborts early on `nd->nr.stop` | HIGH |
| `fw_io_initiate_reset` | boundary (`neuron_fw_io.h:394`) | write reset trigger to BAR0 | HIGH |
| `fw_io_is_reset_initiated` | boundary (`neuron_fw_io.h:403`) | poll whether FW accepted the reset | HIGH |
| `ndhal_reset.nr_{initiate,wait_for_reset_completion,post_reset_config}` | boundary ([dhal-core](dhal-core.md)) | the three arch hooks the worker calls | HIGH |

### Considerations

The deadline `initiate_max_wait_time` is an arch scalar, not a constant in this file: V2 sets it to `1000 * 120 = 120000 ms` (`v2/neuron_dhal_v2.c:28`, registered `:1401`), V3 to `1000 * 480 = 480000 ms` (`v3/neuron_dhal_v3.c:31`, registered `:1893`). The `retry_count` scalar (5, `NR_RESET_RETRY_COUNT`, `reset.h:17`) governs the arch *wait* loop's iteration count, multiplied by 1000 on qemu/emulator (`v2:1480`, `v3:1971/1981`). The split — `nr_initiate_reset_via_fw` lives in the arch-neutral reset file but is *invoked from* the arch `nr_initiate_reset` body — exists because the BAR0 trigger protocol is identical across generations; only the bitmap construction (`nr_get_tpb_reset_map`) and the FW_STATUS poll address differ.

---

## 6. Per-Arch Reset-Register Sequence Delta (V2 vs V3/V4)

The arch-neutral worker is identical across generations; the *bodies* of the three `ndhal_reset` callbacks differ. The table below is the delta along the dimensions a reimplementer must vary per arch — full register-write bodies live in [dhal-v2](dhal-v2.md) (Trn1/Inf2) and [dhal-v3](dhal-v3.md) (Trn2; V4/Trn3 inherits all reset slots unchanged, §V4-overrides in dhal-v3).

| Dimension | V2 (Trn1/Inf2) | V3 (Trn2) / V4 (Trn3) | Source |
|---|---|---|---|
| Reset-map builder | `nr_get_tpb_reset_map` (`v2:118`) — TPB bit `i`, TopSP bits `8 + i*3 .. +3` (3 TopSP/NC); `hi` unused (passed 0) | `nr_get_tpb_reset_map` (`v3:283`) — TPB `i`, SDMA `i+8`, TopSP `i+16`; `hi` word set per-SENG only if `reset_top_dma` | `v2:118-136` · `v3:283-308` |
| Bitmap words | lo only (hi = 0) | lo + hi (hi gates top-level DMA reset) | `v2:151` · `v3:330` |
| FW_STATUS poll addr | `bar0 + 0x30fa0808` (`IOFAB_MISC_RAM + FW_STATUS 0x808`) | `bar0 + V3_MMAP_BAR0_APB_IO_0_MISC_RAM_OFFSET + 0x808` (FIXME: reuses `V2_FW_IO_REG_*` macro) | `v2:194` · `v3:364` |
| Ready test | `status & V2_FW_IO_REG_FW_STATUS_DEVICE_READY_MASK (0x8)` | same mask (shared `V2_*` constant) | `reset.h:24-25` · `v3:371` |
| Wait backoff | `nr_msleep_stoppable(100 * i)` per iter, `i = 0..retry_count` | `nr_msleep_stoppable(100 * i)` — growing, `i = 0` sleeps 0 | `v2:204` · `v3:373` |
| Initiate deadline | `120000 ms` (`1000*120`) | `480000 ms` (`1000*480`) | `v2:28,1401` · `v3:31,1893` |
| Retry count | `5` (×1000 on emu) | `5` (×1000 on qemu/emu) | `v2:1402,1480` · `v3:1894,1971` |
| Post-reset config | `nr_post_reset_config_v2` → `supports_hbm_7200 = 0` | `nr_post_reset_config_v3` → lazy HBM-7200 detect + pod election (`npe_election_exec_on_rst`) if not STD platform | `v2:242` · `v3:413-428` |
| qemu reset doorbell | `writel(1, bar0 + 0x30657010)` (`RESERVED1 + 0x10`) before FW call | `writel(lo, bar0 + ... RESERVED2 + 0x10)` (lo word only; hi ignored) | `v2:164` · `v3:342` |

> **NOTE —** the V3 FW_STATUS poll still references the `V2_FW_IO_REG_FW_STATUS_*` macros (`v3:364,371`) — the constant is V2-named but generation-shared; the source marks this with a FIXME. The mask value (`0x8` for DEVICE_READY) is identical across arches, so the cross-naming is cosmetic, not a behavioral delta.

> **QUIRK —** V3's `nr_get_tpb_reset_map` only sets the `hi` (top-level DMA) word when the `reset_top_dma` module param is set, and on qemu only the `lo` word is `writel`'d to the doorbell (`v3:342`) — so the top-level-DMA reset bits are constructed but, on qemu, never delivered. On bare metal the full lo/hi pair reaches FW through `nr_initiate_reset_via_fw`. A reimplementer targeting V3 must thread both words to the FW path, not just `lo`.

---

## Cross-References

- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the `ndhal_reset` sub-vtable (`neuron_dhal.h:212`) this engine drives through: `nr_initiate_reset`, `nr_wait_for_reset_completion`, `nr_post_reset_config`, and the `initiate_max_wait_time`/`retry_count` scalars
- [DHAL V2 (Trn1 / Inf2)](dhal-v2.md) — the V2 reset bodies: `nr_get_tpb_reset_map` (TopSP triplet layout), the `bar0+0x30fa0808` FW_STATUS poll, 120 s deadline, qemu doorbell at `0x30657010`
- [DHAL V3 (Trn2)](dhal-v3.md) — the V3 reset bodies: lo/hi `tpb_reset_map`, 480 s deadline, lazy HBM-7200 detect + `npe_election_exec_on_rst` post-config; V4/Trn3 inherits these reset slots unchanged
- [Cooperative RW Lock (CRWL)](crwl.md) — the core-claim interlock; `nnq_destroy_nc` and per-core teardown at submit time interact with claim state
- [Pod Election (UltraServer)](pod-election.md) — `npe_election_exec_on_rst`, invoked from V3 post-reset config on non-STD platforms after a successful device reset
- [DMA Rings](dma-rings.md) — `ndmar_init_ncs` (stage 3 of the drive) and the V2 DMA-hang-in-reset-window workaround that consults `nr_op_in_reset_wnd`
