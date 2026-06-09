# libnrt Locking Hierarchy

The binary holds approximately 25 distinct `pthread_mutex_t`
instances, 4 `pthread_cond_t` instances, and 2 POSIX semaphores. Lock
counts from the binary:

| Primitive                     | Call sites |
|-------------------------------|------------|
| `pthread_mutex_init`          | 25         |
| `pthread_mutex_destroy`       | 29         |
| `pthread_mutex_lock`          | 149        |
| `pthread_mutex_unlock`        | 218        |
| `pthread_mutex_trylock`       | 0          |
| `pthread_mutex_timedlock`     | 0          |
| `pthread_cond_wait`           | 1 + 1 absl |
| `pthread_cond_broadcast`      | 3          |
| `pthread_cond_signal`         | 0          |

No `pthread_mutex_trylock` and no `pthread_mutex_timedlock` is a
strong design hint: the runtime accepts blocking for lock acquisition
and relies on lock-free queue protocols (`xu_queue_t`) plus careful
hierarchy to keep critical sections short.

## Lock catalogue

### Process-global locks (`.bss` resident)

| Address    | Lock                            | Purpose                                              |
|------------|---------------------------------|------------------------------------------------------|
| `0xc96960` | `error_log_mutex`               | Serializes global `error_log` ringbuffer flushes     |
| `0xca7280` | `output_completion_lock`        | Pairs with `output_completion_cond` (tensor done)    |

The error log mutex is the most-contended global lock; every
`nlog_write` call site acquires it briefly to append to the in-memory
ringbuffer. Critical sections are bounded by `memcpy(buf, msg, n)` +
counter advance — typically tens of nanoseconds.

The output-completion lock pairs with the corresponding condvar to
broadcast tensor-ready events to waiters. Held only across the
`pthread_cond_broadcast` call.

### Per-vnc locks

| Lock                       | Role                                                    |
|----------------------------|---------------------------------------------------------|
| `model_db_lock`            | Per-vnc model database (`ht_t`, 256 slots)              |
| `nr_completion_lock`       | Pairs with `nr_completion_cond` (async API)             |
| `efd_pool_lock`            | Guards 16-slot LIFO efd cache in `comp_handle_pool_t`   |

`model_db_lock` is held during `nrt_load`, `nrt_unload`, and
`db_get_nn_ref_count`. The hash table is 256 slots with simple
chaining, so lookups are O(1).

`efd_pool_lock` is held only across LIFO push/pop — three machine
instructions inside the critical section.

### Per-TPB locks (physical NeuronCore)

| Lock                       | Role                                                    |
|----------------------------|---------------------------------------------------------|
| `compute_req_lock`         | Per-TPB `hw_exec_queue` mutator lock                    |
| `ulib_staging_lock`        | Per-NC malloc-tensor → DMA-tensor cloning               |

`compute_req_lock` protects the per-NC `hw_exec_queue` data structure
when appending descriptors during `kbl_compute_setup`. Held for the
duration of the descriptor build phase.

`ulib_staging_lock` is acquired only when the sync API receives
CPU-malloc tensors and needs to clone them into HBM-resident shadow
tensors. Async mode rejects malloc tensors outright, so this lock is
held only on the sync path.

### Per-XU locks (per virtual NeuronCore execution unit)

| Offset | Lock                | Role                                                     |
|--------|---------------------|----------------------------------------------------------|
| +0x140 | `mark_comp_lock`    | Serializes `tpb_xu_base_get_comp_efd` and `_report_complete` |
| +0x168 | `unstage_lock`      | Per-XU deferred-unstage bookkeeping                       |
| +0x1F0 | `trigger_lock`      | First-submitter-wins doorbell race                        |
| (struct head) | `submit_work_lock` | Funnels all sync producers through one critical path |

`submit_work_lock` is the largest critical section in the runtime: it
covers input validation, CC barrier proxy enqueue, descriptor build,
doorbell IOCTL, and SPSC queue publish. Held for ~50–500 µs per
submission depending on model complexity.

`mark_comp_lock` is held only while updating the completion record
and writing the completion eventfd — sub-microsecond.

`unstage_lock` is held only during the `tpb_xu_step` post-step
bookkeeping — sub-microsecond.

`trigger_lock` exists for a "first submitter triggers the next batch"
race that occurs only at queue head transitions; init_only in the
binary, never observed contended in normal operation.

### Per-pmu / per-CC locks

| Lock                       | Role                                                    |
|----------------------------|---------------------------------------------------------|
| `pmu_completion_lock`      | Pairs with `pmu_completion_cond` (PMU stats query)      |
| `enc_proxy_queue::mutex_`  | Pairs with the proxy dispatch condvar                   |

### Anonymous async-pool locks

`kmgr_async_exec_init` initializes one mutex per worker for the
work queue. These are anonymous in the binary (no symbol) but visible
in the per-worker struct as `work_queue_lock`.

## Lock acquisition order

The runtime maintains a partial order that prevents deadlock:

```
            atomic counters (no lock taken)
                          |
                          v
                  submit_work_lock          ← held by producer threads only
                          |
                          v
                 compute_req_lock           ← descriptor build phase
                          |
                          v
                  efd_pool_lock             ← efd acquire
                          |
                          v
                 mark_comp_lock             ← worker side, after queue pop
                          |
                          v
                  unstage_lock              ← worker side, after dispatch
```

Disjoint chains (never nested with the above):

- `error_log_mutex` — always shortest-held; covers ringbuffer copy only
- `output_completion_lock` / `_cond` — held during broadcast only
- `nr_completion_lock` / `_cond` — held during broadcast only
- `pmu_completion_lock` / `_cond` — held during broadcast only
- `model_db_lock` — per-vnc; held during `nrt_load` and `nrt_unload`
- `ulib_staging_lock` — per-NC; held during `clone_to_physical_mem`
- `enc_proxy_queue::mutex_` — held during proxy task enqueue/dispatch

The producer-side chain (`submit_work_lock → compute_req_lock →
efd_pool_lock`) and the worker-side chain (`efd_pool_lock →
mark_comp_lock → unstage_lock`) overlap only at `efd_pool_lock` — the
producer acquires it briefly during submission, the worker briefly
during release. They are never held together.

## Why no `trylock` / `timedlock`?

The runtime accepts blocking on locks because:

1. The locks are designed to be short-held. The longest critical
   section is `submit_work_lock` at ~500 µs; all others are
   sub-microsecond.
2. The hot path through `tpb_xu_step` (worker side) avoids the
   producer-side locks entirely — workers never wait on
   `submit_work_lock` or `compute_req_lock`.
3. Multiple producers submitting to different vncs touch different
   `tpb_execution_unit_t` instances, so their `submit_work_lock`s
   don't contend.

Adding `trylock` retry loops would complicate the call-site code
without measurable benefit; the binary opts for simplicity.

## Cross-thread ordering guarantees

For a single inference submitted on vnc V:

1. Producer thread T1 acquires `submit_work_lock[V]`.
2. T1 builds descriptors under `compute_req_lock[V's TPB]` (released
   before exiting `kbl_compute_setup`).
3. T1 publishes the exec_info into `xu_queue.work_queue` via
   `xuq_client_submit` (lock-free).
4. T1 acquires `efd_pool_lock[V]` to fetch a pooled completion efd.
5. T1 issues the SEMAPHORE_INCREMENT IOCTL (the doorbell).
6. T1 writes 1 to `has_work_efd[V]`.
7. T1 releases `submit_work_lock[V]`.
8. T1 blocks on `read(mark_comp_efd)`.

Concurrently, the worker thread W1 (one per vnc):

1. W1's `epoll_wait` returns with `has_work_efd[V]` ready.
2. W1 calls `tpb_xu_step` which calls `xuq_worker_peek_current_exec`
   (lock-free), then `progress_one_step` (NQ poll).
3. On DONE, W1 acquires `mark_comp_lock[V]`.
4. W1 calls `xuq_worker_pop_current_exec` and writes 1 to
   `mark_comp_efd` (releases T1).
5. W1 releases `mark_comp_lock[V]`.
6. W1 acquires `unstage_lock[V]` for deferred-unstage bookkeeping,
   then releases it.

T1's blocked `read` returns. T1 releases the pooled efd to
`efd_pool_lock[V]` (briefly), reads the exec result, and returns.

The complete sequence has **two distinct critical sections per
inference** (T1's `submit_work_lock`, W1's `mark_comp_lock`); the
work queue itself is lock-free.

## Special case: deadlock-free CC barriers

The collective-comm barrier (`enc_check_proxy_barrier_task_status`)
runs under no libnrt lock. It is enqueued onto `enc_proxy_queue` by
the worker, which is a different lock domain. The proxy queue thread
processes it independently; its only interaction with the XU worker
is via the NQ ring (mmap'd), so no lock is contended across.

## Implications for fork()

The `atfork_prepare` handler is empty, meaning all 25 locks above are
left in whatever state they were in at fork-time. The child receives:

- Locks that may be held by a thread that no longer exists in the
  child (since `fork()` copies only the calling thread).
- Condvars whose waiters no longer exist.
- A SPSC queue in unknown state (the consumer may have been mid-pop).

This is why the child is poisoned: even if the application wanted to
"continue" in the child, the data structures are in an inconsistent
state that no recovery routine can untangle. The state-2 (CHILD)
guard is the only sane response.

See [atfork.md](atfork.md) for the full fork policy discussion.

## Performance counters

Approximate lock acquisition rates under sustained inference load
(based on the binary's expected behavior, not measured):

- `error_log_mutex` — once per log line; ~10⁴/s on a busy inference
  loop with `LOG_LEVEL=INFO`.
- `submit_work_lock[V]` — once per `nrt_execute` call on vnc V;
  ~10³/s for typical models, up to ~10⁴/s for sub-millisecond models.
- `mark_comp_lock[V]` — once per completion; matches submit rate.
- `efd_pool_lock[V]` — twice per inference (acquire + release).
- `output_completion_lock` — per CUSTOM type-0 notification (rare).
- All other locks — sub-1/s except during load/close.

Total locking overhead is well below 1 µs per inference at all rates.
