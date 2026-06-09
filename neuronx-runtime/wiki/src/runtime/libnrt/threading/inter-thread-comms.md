# Inter-Thread Communication Primitives

`libnrt.so.2` uses five distinct IPC fabrics to coordinate work
across its thread families. This page enumerates each, with binary-
confirmed creation sites, flags, and intended consumers.

## 1. eventfds — the dominant wake mechanism

Seven `eventfd@plt` call sites in the binary. Two flavors are used:

| Flavor             | Flags value | Read semantics                                  |
|--------------------|-------------|-------------------------------------------------|
| Counter            | `0`         | `read` returns and decrements the accumulated count to 0 |
| Semaphore          | `EFD_SEMAPHORE` (`0x1`) | `read` returns `1` per call, decrements by 1 |
| Semaphore non-block| `EFD_SEMAPHORE \| EFD_NONBLOCK` (`0x801`) | Same as Semaphore but `read` returns -1/EAGAIN when 0 |

### Creation sites

| Address    | Owner                                    | Flags  | Read mode    |
|------------|------------------------------------------|--------|--------------|
| `0x7ce8d`  | `nrta_cc_prepare` (NRT_3.0.0 async API)  | `0x800` (EFD_SEMAPHORE) | semaphore |
| `0xe7d44`  | `tpb_xu_base_init` — `has_work_efd`      | `0x801` (EFD_SEMAPHORE \| EFD_NONBLOCK) | semaphore |
| `0xe846e`  | `tpb_xu_base_get_comp_efd` (fallback)    | `0`    | counter      |
| `0xe85ec`  | `tpb_xu_base_get_comp_efd` (alt path)    | `0`    | counter      |
| `0xe8731`  | `tpb_xu_base_get_comp_efd` (third path)  | `0`    | counter      |
| `0xe9aea`  | `kmgr_xu_workers_init` — `stop_thread_efd` | `0`  | counter      |
| `0x449718` | `ndebug_stream_init` — debug stream      | `0x801` (semaphore) | semaphore |

### Roles

#### `has_work_efd` (per XU)

- **Flags:** `EFD_SEMAPHORE | EFD_NONBLOCK`
- **Producer:** `tpb_xu_schedule_request` writes `1` after publishing
  exec_info into `xu_queue.work_queue`
- **Consumer:** `kmgr_xu_worker_do_work` registers it in epoll with
  `EPOLLIN` (level-triggered)
- **Semantics:** semaphore mode means each `write(_, 1)` increments
  by 1 and each `read` decrements by 1. The worker's `read` happens
  inside `tpb_xu_base_report_complete` only when the exec is fully
  done — guarantees one wake per submitted exec.
- **EFD_NONBLOCK:** defensive — a misbehaving double-read returns
  `EAGAIN` instead of blocking.

#### `stop_thread_efd` (per worker)

- **Flags:** `0` (counter mode)
- **Producer:** `kmgr_xu_workers_destroy` writes `1` to signal
  shutdown to one worker
- **Consumer:** the worker's epoll_wait sees it ready, sees the
  `*tag == 0` dispatch, closes epoll, returns NULL
- **Semantics:** edge-triggered (the worker registered it with
  `EPOLLIN | EPOLLET`); fires exactly once at shutdown

#### Pooled `mark_comp_efd`

- **Flags:** `0` (counter mode)
- **Producer:** `tpb_xu_base_report_complete` writes `1` to signal
  the original submitter that their exec is done
- **Consumer:** `kmgr_sync_exec` reads inside `read(efd, &val, 8)`
  (blocks)
- **Pool:** 16 efds per vnc, LIFO cached in `comp_handle_pool_t`
  at `0xc5d980`. Released on completion via `xu_release_pooled_efd`
  which drains stale counts before push-back.
- **Fallback:** if pool is exhausted, allocate a fresh efd via
  `eventfd(0,0)`; if even that fails, busy-poll
  `tpb_xu_get_last_completed(xu)` with `usleep(1)`.

### CLOEXEC concern

**None of the eventfds set `EFD_CLOEXEC`.** This means:

- `execve()`ed children inherit the fds (cosmetic only — the new
  image won't use them, but `lsof` shows them dangling).
- `fork()`ed children inherit the fds AND they reference the same
  kernel objects as the parent. A write from the child would be
  visible to the parent's epoll_wait. Combined with the
  state-CHILD poison, the child cannot legitimately call code that
  writes to these fds — but a buggy child could.

A defensive framework could `fcntl(fd, F_SETFD, FD_CLOEXEC)` post-init
by walking `/proc/self/fd`. This is not done by libnrt.

## 2. Condition variables

Four condvars are observed in the binary:

| CondVar location                          | Companion lock                | Producer (broadcast)                | Consumer (wait)               |
|-------------------------------------------|-------------------------------|-------------------------------------|-------------------------------|
| `output_completion_cond` at `0xca72c0`    | `output_completion_lock` at `0xca7280` | `progress_one_step` on CUSTOM type-0 tensor-completion at `0x264384` | tensor wait functions          |
| `nr_completion_cond` (per-vnc offset +0x80) | `nr_completion_lock` (per-vnc) | `progress_one_step` END notif at `0x30fe18` | `nrta_wait_completion`        |
| `pmu_completion_cond` (per-pmu)           | `pmu_completion_lock`         | PMU sweep broadcast at `0xe6522`    | PMU stats query               |
| Anonymous (caller-supplied)               | Abseil Mutex internals        | (Abseil shim)                       | `pthread_cond_wait` at `0xc0ea9` |

All libnrt-owned condvar use is **broadcast-only**. No
`pthread_cond_signal` (narrow wake) is present in libnrt code. The
rationale: a single notification can release multiple distinct
waiters (e.g., several tensors get flagged complete by one NQ
entry), and broadcast is the safer default.

### Critical-section sketch

The output completion broadcast (from `exec_request_progress_one_step`
at `0x264354..0x264394`):

```c
pthread_mutex_t *lock = tensor_get_output_completion_lock(tensor);  // 0x310410
pthread_cond_t *cond = tensor_get_output_completion_cond(tensor);   // 0x310400
pthread_mutex_lock(lock);
tensor->completion_count++;                                          // +0x80 of tensor struct
pthread_cond_broadcast(cond);
pthread_mutex_unlock(lock);
```

Waiters do the inverse:

```c
pthread_mutex_lock(lock);
while (tensor->completion_count < expected) {
    pthread_cond_wait(cond, lock);
}
pthread_mutex_unlock(lock);
```

The wait is unbounded — there is no timeout passed to
`pthread_cond_timedwait`. If a producer fails to broadcast (e.g.,
because the inference timed out and the NQ never fired), the waiter
blocks forever. Frameworks that need bounded waits must use the
synchronous `nrt_execute` instead, which has its own timeout.

## 3. POSIX semaphores

POSIX `sem_t` is used exclusively by the async-exec worker pool
(family 2). Per-pool semaphores:

- `has_work_sema` — producer posts on each submission; worker waits
- `inflight_limit_sema` — initialized to
  `async_exec_max_inflight_requests`; producer waits (acquire) before
  submitting, worker posts (release) after completion

Initialization (in `kmgr_async_exec_init`):

```c
sem_init(&worker->has_work_sema, 0, 0);
sem_init(&worker->inflight_limit_sema, 0, max_inflight);
pthread_create(&worker->tid, NULL, kmgr_exec_worker_do_work, worker);
```

The worker's main loop:

```c
while (1) {
    sem_wait(&worker->has_work_sema);                  // block until work
    pthread_mutex_lock(&worker->work_queue_lock);
    if (worker->shutdown_requested) {
        pthread_mutex_unlock(&worker->work_queue_lock);
        break;
    }
    kmgr_async_exec_req *req = worker->work_queue.front();
    worker->work_queue.pop();
    pthread_mutex_unlock(&worker->work_queue_lock);

    tpb_xu_step_async(req);                            // call same step engine
    req->mark_done();
    sem_post(&worker->inflight_limit_sema);            // release inflight slot
}
```

Cancellation is via `worker->shutdown_requested = true; sem_post(&has_work_sema);`
followed by `pthread_join`.

## 4. Lock-free SPSC ring queues (`xu_queue_t`)

The single most central IPC structure is `xu_queue_t`, the
single-producer single-consumer ring at the heart of each XU. Layout:

```c
struct xu_queue_t {
    uint64_t   capacity;            // power of 2
    uint64_t   scheduled_tail;      // producer cursor
    uint64_t   scheduled_head;      // staged-to-worker cursor
    uint64_t   exec_head;           // consumer cursor
    xu_id_t    xu_id;               // packed into seq_id high bits
    xuq_exec_info_t *buffer;        // ring of `capacity` entries
};
```

Producer protocol (under `submit_work_lock`):

```c
buffer[scheduled_tail % capacity] = exec_info;
_InterlockedAdd64(&scheduled_tail, 1);
_InterlockedAdd64(&scheduled_head, 1);
seq_id = (xu_id << 48) | scheduled_tail_old;
return seq_id;
```

Consumer protocol (worker thread, no lock):

```c
exec_info_t *info = buffer[exec_head % capacity];     // peek
// ... process ...
_InterlockedAdd64(&exec_head, 1);                     // pop
```

Queue full: `scheduled_tail - exec_head >= capacity`
Queue empty: `scheduled_head == exec_head`

The 48-bit sequence space is shared with `xu_id` in the high 16 bits,
giving `(1 << 48) - 2 = 2.8e14` max submissions before the assert
fires. A process executing 1 inference per microsecond would hit
the assert in ~8.9 years.

## 5. Memory-mapped device NQ rings

Not strictly inter-thread but worth listing for completeness: the
hardware-to-host notification queues are mmap'd at `nrt_init` time
via IOCTL `0x80084E36` (NOTIFICATIONS_INIT_WITH_REALLOC_V2 #54). The
worker thread reads them via direct CSR access in
`aws_hal_notific_nq_available_count_mariana` (and similar per-arch
variants). The producer is the device's NQ writer; the consumer is
the libnrt worker.

This is the only "outside the kernel" producer in the system.
Everything else (eventfds, condvars, semaphores, queues) is purely
userspace.

## When to use what

The runtime makes deliberate choices about which primitive to use
where. A summary table:

| Use case                            | Primitive                | Rationale                              |
|-------------------------------------|--------------------------|----------------------------------------|
| Producer signals "work available"   | eventfd (semaphore)      | One wake per submission                |
| Worker signals "shutdown"           | eventfd (counter)        | One wake suffices                      |
| Worker signals "exec complete"      | eventfd (counter, pooled)| Amortise syscall cost                  |
| Async: producer signals submission  | POSIX semaphore          | Same as eventfd but with fewer syscalls in some libc paths |
| Async: producer throttles           | POSIX semaphore          | Natural fit for counted limit          |
| Tensor completion fan-out           | condvar (broadcast)      | Multiple waiters per event             |
| Process state                       | atomic int               | Single writer, multiple readers        |
| Producer→consumer queue             | SPSC ring (lock-free)    | Avoid lock contention at queue level   |
| Hardware → host notification        | mmap'd ring + MMIO       | Avoid IOCTL on every notification      |

## Race-free invariants

The combination of these primitives produces strong invariants:

1. **At most one exec is in-flight per XU at any time** as observed
   by the worker. Producers are serialized by `submit_work_lock`,
   the SPSC queue ensures FIFO order, and the worker pops one entry
   per `tpb_xu_step` call.

2. **Every `write(has_work_efd, 1)` is matched by exactly one
   `read(has_work_efd)`** in `tpb_xu_base_report_complete`. The
   semaphore mode counts wakes; no wakes are lost.

3. **Every pooled `mark_comp_efd` is drained before reuse** via
   the `xu_release_pooled_efd` fcntl/read loop. Stale completions
   from prior execs cannot wake a fresh waiter.

4. **The output completion lock+cond pair prevents lost wakeups**
   via the standard "lock-check-wait-unlock" pattern with broadcast
   on the producer side.

These invariants survive concurrent submission from multiple
producer threads (the `submit_work_lock` serializes them) and
concurrent draining of multiple XUs by multiple workers (each
worker owns its XUs exclusively).
