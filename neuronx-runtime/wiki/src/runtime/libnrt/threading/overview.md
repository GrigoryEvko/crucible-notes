# libnrt Threading Model: Overview

`libnrt.so.2` (aws-neuronx-runtime-lib 2.31.24.0) is a multi-threaded
shared library. A typical Neuron framework process running on a Trn1 /
Trn2 host owns:

- One thread per visible virtual NeuronCore (vnc) executing the
  synchronous XU worker loop (`kmgr_xu_worker_do_work`)
- Optionally a second pthread per vnc for async-mode submission
  (`kmgr_exec_worker_do_work`), gated on
  `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS`
- One transient pthread per TPB during `nrt_load` (joined inline)
- Optional collective-comm threads when the process participates in
  NCCom rings (OFI service, KV-store trio, enc_proxy_queue dispatcher)
- A pool of one-shot per-device close threads during `nrt_close`

This page surveys the model and links into the detailed pages on TLS,
worker threads, locking, fork policy, signal handlers, IPC primitives,
and the four-state runtime FSM.

## Thread families at a glance

| # | Family                        | Trigger                  | Cardinality       | Cancellation                  |
|---|-------------------------------|--------------------------|-------------------|--------------------------------|
| 1 | XU sync worker                | `nrt_init`               | visible vncs      | stop-event eventfd            |
| 2 | Async-exec worker             | `nrt_init` (opt-in)      | visible vncs      | `kmgr_async_exec_destroy`     |
| 3 | Model-stage helper            | `nrt_load`               | sg_count          | inline join                   |
| 4 | OFI/RDMA async send-recv      | NCCom channel open       | per channel       | encd_destroy                  |
| 5 | KV-store TCP listener         | NCCom KV-server start    | 1                 | TCP close + join              |
| 6 | KV-store client handler       | per accepted peer        | per peer          | per-connection close          |
| 7 | KV-store completion worker    | KV-store init            | 2 (server+client) | close + join                  |
| 8 | enc_proxy_queue dispatcher    | `enc_proxy_queue::start` | per CC queue      | condvar broadcast + join      |
| 9 | Device close fan-out          | `nrt_close`               | used MLAs        | inline join                   |

Families 1–2 dominate the steady-state thread inventory; the
remainder are dormant unless collective communication is active.

## Process-wide runtime state

A single 32-bit `nrt_init_state` global drives every public-API guard:

```
0 → NRT_STATE_START   (pre-init / failed init)
1 → NRT_STATE_INIT    (bootstrap complete)
2 → NRT_STATE_CHILD   (set in atfork_child only — process poisoned)
3 → NRT_STATE_CLOSED  (set on successful nrt_close)
```

The transitions are linear (`START → INIT → CLOSED`) with a single
fork-side branch into `CHILD`. See [nrt-state-model.md](nrt-state-model.md)
for the full lifecycle.

## Fork policy in one paragraph

`nrt_init` registers three `pthread_atfork` handlers. Both
`atfork_prepare` and `atfork_parent` are empty `ret`s; only
`atfork_child` runs, and it does exactly one thing: set the state to
`NRT_STATE_CHILD`. Every subsequent libnrt entry point in the child
returns failure with `"Incompatible runtime state: CHILD"`. The
runtime does **not** quiesce locks before fork — it relies on the
poison-the-child contract to keep the parent's locked state irrelevant.
Frameworks must avoid `multiprocessing.fork`; use
`multiprocessing.spawn` (fork+exec) instead. See [atfork.md](atfork.md).

## Thread-local error context (`err_ctx`)

The runtime owns exactly one ELF `__thread` variable for its own
purposes: `err_ctx`, 16 400 bytes per thread, in `.tdata`. Public APIs
read fields at fixed offsets:

```
err_ctx + 0x0000 .. + 0x3FFE   per-thread rendered error string ring
err_ctx + 0x3FFF                init-marker byte
err_ctx + 0x4000                last-error cause code (4 bytes; sentinel = 0xFFFFFFFF)
err_ctx + 0x4008                log_coalescing_enabled cache
err_ctx + 0x400C                coalesce counter
```

`nlog_set_error_cause` writes `+0x4000` with first-set-wins semantics;
`nrt_get_last_error_str` (presumed) reads it. Importantly, there is
**no `pthread_key_create`** used by libnrt for runtime state — all
per-thread state is ELF TLS, allocated by `__tls_get_addr` on first
access. See [tls-fields.md](tls-fields.md).

## Locks at a glance

Roughly 25 distinct mutexes and 4 condvars across the binary. The
hot-path producer chain (`submit_work_lock → compute_req_lock →
efd_pool_lock`) is fully disjoint from the worker-side chain
(`mark_comp_lock → unstage_lock`). No nested locks cross the boundary,
which is why the design tolerates 9-event epoll batches without
contention. See [locking-hierarchy.md](locking-hierarchy.md).

## Signal handling — minimal by design

The only signal handler libnrt itself installs is `nrt_inspect_signal_handler`,
attached to a configurable set of fatal signals when device profiling
is armed. SIGTERM, SIGINT, SIGQUIT, SIGCHLD have no libnrt handlers.
Workers are killed where they stand; the kernel-side DKMS layer drops
per-process resources on the chrdev fd close. The bundled Rust stdlib
adds its own SIGPIPE-ignore and SEGV alt-stack at process bring-up.
See [signal-handlers.md](signal-handlers.md).

## Inter-thread comms

- **eventfd × 7 creation sites.** Two flavors: `EFD_SEMAPHORE`
  (per-submission has-work signal) and counter mode (stop events,
  completion notifications, pooled handles).
- **Condvars × 4.** All broadcast-only; one waits via the Abseil
  shim. Used for output completion fan-out, async completion,
  PMU sampling, and CC dispatch.
- **POSIX semaphores.** Only in the async-exec worker pool
  (`has_work_sema`, `inflight_limit_sema`).
- **Lock-free SPSC ring queues** (`xu_queue_t`). One per XU; producer
  uses a mutex above the ring (effectively MPSC).
- **Memory-mapped device NQ rings.** The completion bus from
  hardware; polled by family-1 workers via mmap'd CSR reads.

See [inter-thread-comms.md](inter-thread-comms.md).

## When you'll observe what

| Operation              | Thread families active                                            |
|------------------------|-------------------------------------------------------------------|
| `nrt_init`             | main thread spawns families 1, 2 (if async); registers atfork     |
| `nrt_load`             | main thread + family 3 (sg_count helpers, joined before return)   |
| `nrt_execute`          | main thread submits; family 1 (or family 2) consumes              |
| `nrt_execute` with CC  | + family 8 (enc_proxy_queue), + family 4 (OFI service)            |
| `nccom_init` for ranks | + families 4 (OFI), 5–7 (KV-store trio)                          |
| `nrt_close`            | family 9 fans out per-device close; then drains families 1, 2     |
| `os.fork()` in process | `atfork_child` flips state to 2; child cannot use libnrt          |

The remainder of this section explores each of these surfaces in
depth.
