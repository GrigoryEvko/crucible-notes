# libnrt Worker Threads

`libnrt.so.2` spawns persistent pthreads in eight distinct families
plus one one-shot family during shutdown. Each family has a single
thread function, a well-defined spawn point, and a fixed cardinality
rule. There are 13 `pthread_create@plt` call sites in the binary
(one for the Rust stdlib glue is dead code, leaving 12 active sites).

All persistent worker threads are spawned during `nrt_init` (after
`kmgr_init` succeeds) or during NCCom initialization. None are
spawned lazily — the catalogue is known at the end of `nrt_init`
plus optionally at the first collective.

## Family 1 — Synchronous XU worker

**Thread function:** `kmgr_xu_worker_do_work` at `0xe92c0`
**Spawn site:** `kmgr_xu_workers_init` at `0xe9b20`, called from
`kmgr_init` (nrt_init step 72)
**Cardinality:** one per visible vnc (default = number of vncs the
process owns per `NEURON_RT_VISIBLE_CORES` / `NEURON_RT_NUM_CORES`)
**Storage:** `workers[]` array at `0xc88ae0` (BSS), stride 88 bytes
(`0x58`). Count at `0xc88ac0` (`worker_count`, `uint64_t`).
**Per-worker struct (88 bytes):**

| Offset | Field                                    | Size  |
|--------|------------------------------------------|-------|
| 0x00   | `pthread_t worker_thread`                | 8     |
| 0x08   | `int stop_thread_efd`                    | 4 + 4 |
| 0x10   | `tpb_execution_unit_t *owned_tpb_xus[8]` | 64    |
| 0x50   | `uint64_t xu_count`                      | 8     |

**Loop sketch:**

```c
void *kmgr_xu_worker_do_work(void *arg) {
    kmgr_xu_worker_t *self = (kmgr_xu_worker_t *)arg;
    int epfd = epoll_create1(0);                    // no EPOLL_CLOEXEC
    epoll_ctl(epfd, ADD, self->stop_thread_efd, &stop_ev);   // ET
    for (size_t i = 0; i < self->xu_count; i++)
        epoll_ctl(epfd, ADD, self->owned_tpb_xus[i]->base.has_work_efd, &xu_ev);

    struct epoll_event ready[9];
    for (;;) {
        int n = epoll_wait(epfd, ready, 9, -1);
        nlog_coalescing_init_thread();              // reset TLS
        for (int j = 0; j < n; j++) {
            int *tag = ready[j].data.ptr;
            if (*tag == 0) {                        // stop
                close(epfd);
                return NULL;
            }
            tpb_xu_step(((struct xu_entry *)tag)->xu);
        }
    }
}
```

**Cancellation:** `kmgr_xu_workers_destroy` writes 1 to every
`stop_thread_efd` in phase 1, then `pthread_join`s each thread in
phase 2. After join, each `stop_thread_efd` is closed and the
worker struct zeroed.

**Naming:** never named via `pthread_setname_np` — inherits the
process name in `/proc/PID/task/<tid>/comm`.

## Family 2 — Async-exec worker

**Thread function:** `kmgr_exec_worker_do_work` at `0xe6310`
**Spawn site:** `kmgr_async_exec_init` at `0xe7152`, called from
`kmgr_init` only if `nrt_gconf()->implicit_async_mode == true && nrt_gconf()->async_exec_max_inflight_requests > 0`
**Cardinality:** one per vnc
**Storage:** `async_exec_workers` pointer at `0xc5d8c8` (array of
`kmgr_async_exec_worker_threads_t *`, one per vnc).

This family operates on the same `_tpb_xus[]` array as Family 1
(at `0xc64a40`, stride 0x240) but uses POSIX semaphores instead of
eventfds:

- `has_work_sema` — producer posts on each submission
- `inflight_limit_sema` — capped to throttle in-flight count

The worker calls `sem_wait` to block; no epoll, no eventfd, no busy
poll. The trade-off: lower wake latency for a single XU, but no
multi-XU multiplexing — the async path covers one XU per worker.
Both families ultimately call `tpb_xu_step` at `0xe8940`, so all
NQ-polling state machine code is shared.

**Cancellation:** `kmgr_async_exec_destroy` posts a "shutdown"
sentinel to `has_work_sema` and joins each worker.

## Family 3 — Model-stage helper

**Thread function:** `dlr_kelf_stage_model_add_thread` at `0xe07f0`
(symbol `_ZL31dlr_kelf_stage_model_add_threadPv`)
**Spawn site:** `dlr_kelf_stage_multi_tpb_model_add` at `0xe0656`,
inside `nrt_load`
**Cardinality:** `sg_count` (TPB count of the model being loaded)
**Lifetime:** spawned, runs, joined inline before `nrt_load` returns

These are short-lived helpers used to parallelize per-TPB model
staging during loading. They build per-TPB DMA programming, upload
IRAM contents, install ucode, etc. Each thread operates on one TPB
in isolation, so the only cross-thread interaction is the
`dmem_allocator` shared across the staging set (lock-free).

**Hazard:** if one of these threads aborts (e.g., ucode-init failure),
`pthread_join` will block on the others indefinitely. There is no
timeout. `nrt_load` would hang.

## Family 4 — OFI/RDMA async send-recv service

**Thread function:** `async_sr_service_thread_fn_pthread` at `0xf0fb0`
(symbol `_Z34async_sr_service_thread_fn_pthreadPv`)
**Spawn site:** `enc_async_sr_init` at `0xeb9ed`
**Cardinality:** one per `enc_comm_info` (typically one per CC channel)

Loops on libfabric's `fi_cq_read` polling for OFI completion events.
This is the userspace half of the NCCom RDMA path; it processes
send/recv completions out-of-band from the inference path so the
main worker isn't blocked by the network.

**Cancellation:** signaled via `enc_async_sr_destroy`, which sets a
"shutdown" flag the service thread checks each loop iteration.

## Families 5/6/7 — KV-store trio

The KV store is the runtime's intra-cluster control plane. Three
threads per node:

### KV server listener

**Thread function:** `kv_server_service_thread_fn_pthread` at `0xf1c10`
**Spawn site:** `0xf3db5`
**Cardinality:** 1 per process (only on the rank-0 / KV-server host)
**Lifecycle:** TCP `accept()` loop; spawns a Family 6 thread per
accepted peer.

### KV client connection handler

**Thread function:** `kv_store_handle_client_fn_pthread` at `0xf53d0`
**Spawn site:** `0xf1ccc`
**Cardinality:** one per active peer connection (on server side)
**Lifecycle:** reads requests on its socket, services GET/PUT/CAS,
exits on socket close.

### KV completion worker

**Thread function:** `kv_store_completion_thread_fn_pthread` at `0xf4430`
**Spawn sites:** `0xf4236` (server side) and `0xf438e` (client side)
**Cardinality:** 1 per side (server + client)
**Lifecycle:** drains a completion queue.

All three are dormant in a single-rank job; they activate when
`NEURON_RT_ROOT_COMM_ID` is set or when `nccom_init` runs.

## Family 8 — enc_proxy_queue dispatcher

**Thread function:** `enc_proxy_queue::worker_thread` at `0x1d0120`
(symbol `_ZN15enc_proxy_queue13worker_threadEPv`)
**Spawn site:** `enc_proxy_queue::start` at `0x1ce4e7`
**Cardinality:** one per `enc_proxy_queue` instance (typically one
per vnc when CC is active)

This is the **only** libnrt thread that self-names. The function
opens with:

```c
char name[16] = {0};
int tid = syscall(SYS_gettid);
snprintf(name, 16, "enc_proxy_%d", tid);
pthread_setname_np(pthread_self(), name);
```

So in `/proc/PID/task/<tid>/comm` you'll see `enc_proxy_<tid>` for
this family.

The body is `enc_proxy_queue::process()` (`0x1cf7c0`), a condvar-
driven dispatch loop that pops `enc_proxy_task` objects from a
priority queue and runs them. This decouples collective-comm
barriers from the XU worker: the XU worker enqueues a barrier task
on this thread and continues processing other notifications while
the proxy thread handles the barrier itself.

## Family 9 — Device close fan-out

**Thread function:** `device_close_fn` at `0x2691a0`
**Spawn site:** `tdrv_destroy` at `0x269b58`
**Cardinality:** one per used MLA (Neuron device) during shutdown
**Lifecycle:** spawned, runs cleanup, joined inline by `tdrv_destroy`

Each thread closes its assigned device's chrdev fd, unmaps the BAR
regions, frees DMA buffers. Parallelism is purely to amortise the
per-device close cost (which involves a synchronous IOCTL to the
DKMS layer).

## Spawn-site index

| Address    | Family | Function                                       |
|------------|--------|------------------------------------------------|
| `0xbc1e4`  | dead   | libstdc++ `_M_thread_deps_never_run`           |
| `0x7b14f`  | xu init helper | `init_xu_common` callback                |
| `0xe0656`  | 3      | `dlr_kelf_stage_model_add_thread`              |
| `0xe7152`  | 2      | `kmgr_exec_worker_do_work`                     |
| `0xe9b20`  | 1      | `kmgr_xu_worker_do_work`                       |
| `0xeb9ed`  | 4      | `async_sr_service_thread_fn_pthread`           |
| `0xf1ccc`  | 6      | `kv_store_handle_client_fn_pthread`            |
| `0xf3db5`  | 5      | `kv_server_service_thread_fn_pthread`          |
| `0xf4236`  | 7      | `kv_store_completion_thread_fn_pthread`(server)|
| `0xf438e`  | 7      | `kv_store_completion_thread_fn_pthread`(client)|
| `0x1ce4e7` | 8      | `enc_proxy_queue::worker_thread`               |
| `0x269b58` | 9      | `device_close_fn`                              |
| `0x528f01` | Rust   | `std::sys::thread::unix::Thread::new`          |

## Resource lifecycle summary

| Resource                           | Created at                | Destroyed at              |
|------------------------------------|---------------------------|---------------------------|
| `workers[]` family-1 array         | `kmgr_xu_workers_init`    | `kmgr_xu_workers_destroy` |
| `stop_thread_efd` per worker       | `kmgr_xu_workers_init`    | `kmgr_xu_workers_destroy` |
| `has_work_efd` per XU              | `tpb_xu_base_init`        | `tpb_xu_base_destroy_one` |
| `mark_comp_efd` per in-flight req  | `tpb_xu_base_get_comp_efd`| `tpb_xu_release_pooled_eventfd` (or close) |
| pooled-efd LIFO (16 slots/vnc)     | `xu_comp_handle_pool_init`| pool-close path (TBD)     |
| `_tpb_xus[]` global slot array     | `tpb_xu_init`             | `tpb_xu_destroy`          |
| `async_exec_workers[]`             | `kmgr_async_exec_init`    | `kmgr_async_exec_destroy` |
| `enc_proxy_queue::worker_thread`   | `enc_proxy_queue::start`  | `enc_proxy_queue::stop`   |
| `device_close_fn` threads          | `tdrv_destroy`            | inline join               |

## Performance characteristics

- **Family 1 (XU sync worker)** spends >95% of its CPU time inside
  `progress_one_step` (NQ ring polling). PCIe MMIO read latency
  dominates; CPU utilization for a busy worker is ~30–60% of one core.
- **Family 2 (async-exec worker)** has higher wake latency (sem_wait
  goes through futex) but lower per-call overhead than family 1
  (no epoll setup, no level-triggered re-fire). Suitable for
  bursty workloads where many requests arrive nearly together.
- **Family 8 (enc_proxy)** is condvar-driven, so it's essentially
  idle when no collectives are pending. Wake latency ~5 µs typical.

## Why one thread per vnc?

`tpb_xu_step` is non-reentrant per XU (it owns the `work_queue`,
`exec_state`, etc.), so each vnc needs at least one consumer thread.
Multiplexing two vncs onto one thread would force serialization at
the worker level, defeating the point of having two vncs. Conversely,
multiplexing one vnc onto two threads requires a lock-free MPMC queue
(more expensive) without measurable benefit. The 1:1 mapping is the
sweet spot.

The fact that `kmgr_xu_worker_t::owned_tpb_xus[8]` is sized for up to
8 XUs per worker suggests the design contemplates a future where
multiple cheap XUs (perhaps one per stream or per QoS class) share a
single worker. The current build exercises exactly one XU per worker.
