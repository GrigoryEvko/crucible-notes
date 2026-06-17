# Overview: the Hot Inference Path

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64 x86-64, **not** stripped, full C++ symbols + DWARF present). `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA verifiable against `nm -n libnrt.so`. The single kernel citation is an `ioctl` number into `aws-neuronx-dkms`. Source asserts name `/opt/workspace/KaenaRuntime/{kmgr,tdrv}/…`. Other versions will differ.*
> *Evidence grade: **Confirmed (decompiled + struct-anchored)** — the end-to-end chain (`nrt_execute` → fork → bind → stage → doorbell → harvest) was read from per-function decompilation and cross-checked against the IDA `structures.json`/`enums.json`/`callgraph.json`; the deep mechanics live in the five sibling pages this map points to. · Part VII — Execution Engine · [back to index](../index.md)*

## Abstract

A loaded model on a NeuronCore runs an inference the way a GPU command submitter runs a draw: **bind** the user's input/output buffers, **stage** a hardware-readable request into a ring that was allocated once at model load, **ring a doorbell**, then **harvest** the completion. This page is the map of that hot path — the steady-state `nrt_execute` loop — and it orients the five sibling pages that each own one stage in byte-level detail. The Neuron-specific shape is that almost nothing touches the kernel: the descriptor ring (`model_exec_desc_q`) and the request buffer are allocated at load, an execution only *patches* per-engine instruction-block addresses into a 52-byte `device_exec_request_t` and appends DMA trigger descriptors, the doorbell is a single `ioctl(SEMAPHORE_INCREMENT)` per core, and completion is learned by **polling** mmap'd notification rings — there is no completion interrupt.

The reference frame is a standard userspace submit/complete split with two twists. First, the path **forks once** on a process-global pointer, `async_exec_workers` (`@0xc5d8c8`): NULL (the default) is **synchronous** — the caller stages, doorbells, then blocks on `read(eventfd)` until a worker marks the sequence complete; non-NULL is **implicitly asynchronous** — the same stage+doorbell runs inline under a worker lock, the request is pushed onto a `std::deque`, and a handle returns immediately. Both forks share one bind→stage→doorbell core. Second, a model is statically bound at load to `sg_count` physical TPBs (`mod->vcore->tpbs[0..sg_count-1]`); the stage loop and the doorbell loop both iterate that array **lockstep** — arm every core, then fire every core — with one execution-unit (XU) per vcore, so the work item is enqueued exactly once even though `sg_count` cores execute it. There is no dynamic dispatch and no work-stealing.

The path is allocation-free on the steady-state hot path because the placement of every device variable was decided **at compile time**. The compiler emits a static memory plan (`kbin_mem_ref` records inside the NEFF/KELF); model-load instantiates that plan once, assigning each variable a fixed device physical address. At execute time, instruction translation resolves a variable to its address through `mem_ref_to_addr` (`@0x22da60`) — `addr = offset + mr->physical_address` — and the DMA-ring descriptors built during stage point straight at those fixed addresses. Nothing is allocated per op. This map documents (1) the entry-and-fork shape, (2) the bind→stage→doorbell→harvest pipeline with the entry symbol pinned at each stage, and (3) the sync/async split, the per-NC/LNC fan-out, and the zero-alloc resolution — then links the siblings rather than re-deriving their layouts.

For reimplementation, the map-level contract is:

- **The single fork** on `async_exec_workers`, and the *inverted* exec-mode it passes (`workers==NULL ? mode 1 : mode 0`) — sync clones host tensors to device; async **rejects** any host tensor with `NRT_INVALID`.
- **The five stages and their seams** — entry/validate (`nrt_execute`), bind (`kmgr_exec_pre`), stage (`hw_exec_queue_add_exec_request_impl`), doorbell (`dlr_kickoff_exec` → one `ioctl`), harvest (`exec_request_progress_one_step`) — each owned by a sibling page.
- **The lockstep fan-out** — one `compute_req_lock` on `tpbs[0]`, stage all `sg_count` cores, one `compute_idx_tail++`, then `sg_count` doorbell ioctls; one XU per vcore, enqueue once.
- **The zero-alloc invariant** — rings and request buffer allocated at load; execute only patches addresses and appends triggers; variables resolve to fixed PAs via the compiler's static plan.
- **The polled completion** — no interrupt; the host reads mmap'd NQ rings and the `mark_comp_efd` that unblocks the sync caller is written by *userspace*, not the kernel. Streaming outputs surface on a second per-tensor NQ channel before the whole inference is `DONE`.

| | |
|---|---|
| **API entry** | `nrt_execute` `@0x91de0` (export `T`, `NRT_2.0.0`) → `nrt_execute_repeat` `@0x91650` |
| **Dispatch fork** | `kmgr_exec` `@0xdfd50`, branch on global `async_exec_workers` `@0xc5d8c8` |
| **Bind** | `kmgr_exec_pre` `@0xdf820` → `kbl_compute_build_compute_resources` `@0x306790` |
| **Submit funnel** | `tpb_xu_schedule_request` `@0xe7540` (holds `submit_work_lock`) |
| **Stage core** | `hw_exec_queue_add_exec_request_impl` `@0x320810` → `device_exec_request_t` (**52 B**) |
| **Doorbell** | `dlr_kickoff_exec` `@0xdd890` → `ndl_nc_semaphore_increment` `@0xc3ba0` → `ioctl(0x80084E29)` |
| **Publish queue** | `xu_queue_t` (256 B, lock-free SPSC), `xuq_client_submit` `@0xe9f10` |
| **Sync boundary** | `read(mark_efd, …, 8)` inside `kmgr_sync_exec` `@0xdca70` |
| **Harvest** | `exec_request_progress_one_step` `@0x263330` — NQ poll, no interrupt |
| **Zero-alloc resolution** | `mem_ref_to_addr` `@0x22da60` — `addr = offset + physical_address` |
| **Fan-out** | per vcore: one XU, `sg_count` TPBs, `sg_count` ioctls, lockstep |

---

## 1. The Pipeline at a Glance

One inference flows through five stages. The diagram pins the entry symbol of each stage and the seam between them; the byte-level mechanics of every box are owned by the sibling page named in §4. The fork at `kmgr_exec` is the only branch in the topology — both arms re-converge on the shared `dlr_add_to_hw_exec_queue` → `dlr_kickoff_exec` core.

```text
  nrt_execute @0x91de0                          ── state guard; trace/profile arm
        │   nrt_execute_repeat @0x91650         ── host validate: repeat<=1, ifmap count
        ▼
  kmgr_exec @0xdfd50                            ── resolve handle; count host tensors
        │
        ├──────────────[ async_exec_workers == NULL ]──────────────┐  SYNC (default)
        │                                                          │
        │  kmgr_exec_pre @0xdf820   (mode 1)        ── BIND        │
        │  kmgr_sync_exec @0xdca70                  ── submit+block │
        │    tpb_xu_schedule_request @0xe7540       ── THE FUNNEL   │
        │                                                          │
        └──────────────[ async_exec_workers != NULL ]─────────────┤  ASYNC (implicit)
                                                                   │
           kmgr_exec_pre @0xdf820  (mode 0)         ── BIND        │
           kmgr_async_exec_add_work @0xe6d20                       │
             kaew_post_request @0xe5cd0   ── inline stage+doorbell │
                                                                   ▼
        ┌──────────────────────  SHARED bind→stage→doorbell  ──────────────────────┐
        │                                                                          │
   [BIND]    kmgr_exec_pre @0xdf820                                                │
        │      dlr_check_valid_io_sets @0xe5b30   ── tensor_io & placement         │
        │      kbl_compute_build_compute_resources @0x306790                       │
        │        ddrs_build_dma_rings + ioqs_build_swap_data  ── wire tensor PAs   │
        │                                                                          │
   [STAGE]   dlr_add_to_hw_exec_queue @0xdd820 → kbl_compute_setup @0x306fb0       │
        │      (per-TPB, under compute_req_lock)                                   │
        │      hw_exec_queue_add_exec_request_impl @0x320810                       │
        │        device_exec_request_t (52 B) → dmem_buf_copyin                    │
        │        3 trigger pairs: req-copy 52B · evt-poke 4B · tail-inc 4B×2       │
        │                                                                          │
   [DOORBELL] dlr_kickoff_exec @0xdd890  (loop sg_count TPBs)                      │
        │      kbl_infer_kickoff @0x307320 → exec_kickoff_infer @0x2632e0          │
        │        ndl_nc_semaphore_increment @0xc3ba0                               │
        │          ioctl(fd, 0x80084E29, {nc, INFERENCE_START, 1})  ── #41         │
        │                                                                          │
   [PUBLISH] xuq_client_submit @0xe9f10  ── seq_id=(tail&0xFFFF…)|(xu_id<<48)      │
        │      xuq_client_trigger_next @0xea090 ; write(has_work_efd,1,8)          │
        └──────────────────────────────────────────────────────────────────────  ┘
        ▼
   [HARVEST]  exec_request_progress_one_step @0x263330  ── INIT→WAIT_BARRIER_PROXY
        │       →WAIT_CORE (poll INFER_STATUS NQ rings, no IRQ) →DONE              
        │     sync : exec_wait_round_robin @0x2655c0   (usleep(1) busy-poll)       
        │     async: tpb_xu_step @0xe8940              (worker thread step)        
        │       tpb_xu_base_report_complete @0xe8800  ── signal mark_comp_efd      
        ▼
   [ERROR DECODE] exec_request_process_errors @0x2615b0
                    notification_consume_error_block @0x2ff250
                    → NRT_TIMEOUT(5) / SW_NQ_OVERFLOW(1204) /
                      COMPLETED_WITH_ERR(1004) / _NUM_ERR(1003) / HW_ERR_NC_UE(1202)
```

The sync arm's `read(mark_efd, …, 8)` inside `kmgr_sync_exec` is the seam between submit and completion: it is the last instruction the [submit path](submit-path.md) owns, and it blocks until the [completion engine](completion-engine.md) — driven from a worker thread — writes the per-execution `mark_comp_efd`. The async arm returns a request handle the moment `kaew_post_request` pushes onto the deque; its completion is harvested entirely by the worker and surfaced through `nrt_register_async_exec_callback` (`@0x94490`).

### Stage entry points and seams

Each pipeline stage is one function descent with a named entry symbol; the seams between them are the natural cut-lines along which Part VII is split into sibling pages. The table is the map index: it pins the entry symbol of each stage, the seam it ends at, and the page that owns its byte-level mechanics. A reimplementer walks this table top-to-bottom and follows each link for the layout it summarizes.

| Stage | Entry symbol @addr | Ends at seam | Owned by |
|---|---|---|---|
| Entry / validate | `nrt_execute` `@0x91de0` → `nrt_execute_repeat` `@0x91650` | `kmgr_exec` handle resolve | [submit-path §1](submit-path.md) / [api-lifecycle](../runtime/api-lifecycle.md) |
| Dispatch fork | `kmgr_exec` `@0xdfd50` | branch on `async_exec_workers` `@0xc5d8c8` | [kmgr-facade §2](kmgr-facade.md) |
| Bind | `kmgr_exec_pre` `@0xdf820` → `kbl_compute_build_compute_resources` `@0x306790` | per-TPB rings wired (tensor PAs) | [submit-path §2](submit-path.md) |
| Submit funnel | `tpb_xu_schedule_request` `@0xe7540` | holds `submit_work_lock` | [submit-path §4](submit-path.md) |
| Stage | `hw_exec_queue_add_exec_request_impl` `@0x320810` | `device_exec_request_t` (52 B) + 3 trigger pairs in ring | [submit-path §3](submit-path.md) / [xu-queue-abi](xu-queue-abi.md) |
| Doorbell | `dlr_kickoff_exec` `@0xdd890` → `ndl_nc_semaphore_increment` `@0xc3ba0` | `ioctl(0x80084E29)` ×`sg_count` | [submit-path §5](submit-path.md) |
| Publish | `xuq_client_submit` `@0xe9f10` | SPSC slot + `write(has_work_efd)` | [submit-path §6](submit-path.md) / [xu-queue-abi](xu-queue-abi.md) |
| Boundary | `read(mark_efd, …, 8)` in `kmgr_sync_exec` `@0xdca70` | the submit↔complete cut | [submit-path §6](submit-path.md) ↔ [completion-engine](completion-engine.md) |
| Harvest | `exec_request_progress_one_step` `@0x263330` | `DONE` → `mark_comp_efd` signal | [completion-engine §1–§3](completion-engine.md) |
| Error decode | `exec_request_process_errors` `@0x2615b0` → `notification_consume_error_block` `@0x2ff250` | `NRT_STATUS` set | [completion-engine §4](completion-engine.md) |

> **NOTE —** the two drivers of the harvest stage (`exec_wait_round_robin` `@0x2655c0` sync, `tpb_xu_step` `@0xe8940` async) run the *identical* `exec_request_progress_one_step` machine; the seam between sync and async is *which thread* spins it, not which code. A reimplementer factors the harvest as one re-entrant step function with two driver loops, never as two copies.

---

## 2. The Fork, the Fan-Out, and the Zero-Alloc Invariant

### The sync/async fork

`kmgr_exec` (`@0xdfd50`) resolves the handle (`db_get_nn_ref_count` `@0xdc0c0`), counts host-resident (CPU-malloc) tensors in the input and output sets (`kbl_tensor_set_get_malloc_tensor_count`), and branches on `async_exec_workers`. The two arms differ in their **input contract**, not merely in latency:

- **Sync** (`async_exec_workers == NULL`): host tensors are *cloned and copied* to device DRAM (`kbl_tensor_set_clone_to_physical_mem` + `kbl_tensor_set_copy`), the bind runs in mode `1`, `kmgr_sync_exec` (`@0xdca70`) submits and **blocks**, and outputs are copied back on success.
- **Async** (`async_exec_workers != NULL`): any host tensor is **rejected** with `NRT_INVALID` (string *"Async exec mode only supports device allocated tensors…"*), the bind runs in mode `0`, and `kmgr_async_exec_add_work` (`@0xe6d20`) hands a `kmgr_async_exec_req` (56 B) to the worker pool and returns a handle.

> **QUIRK —** the exec-mode argument is computed as `(async_exec_workers == NULL)`, so the **sync** path passes mode `1` and the **async** path passes mode `0`. The naming inverts the obvious guess: mode `0` is the *asynchronous* mode. A reimplementer who assumes `0 == default == sync` will mis-route the host-tensor clone and the fmap-set selection. The full enum (`kmgr_exec_mode_t`, `exec_mode` at `+0x20` of `kmgr_exec_resources_t`) and both contracts are pinned in [the submit path §1](submit-path.md) and [the KMGR facade §2](kmgr-facade.md).

### The per-NC/LNC lockstep fan-out

A model is bound at load to `sg_count` physical TPBs. Both the stage loop (`kbl_compute_setup` `@0x306fb0`) and the doorbell loop (`dlr_kickoff_exec` `@0xdd890`) iterate `mod->vcore->tpbs[0..sg_count-1]` in order. The pattern is **arm-all-then-fire-all**: stage descriptors into *every* TPB's ring first, then doorbell *every* TPB. Staging is serialized through a single `compute_req_lock` taken on `tpbs[0]` only — there is no per-TPB lock and no parallel staging — and the execution's `compute_idx` is one monotonic counter (`compute_idx_tail++`) on the main TPB, identifying the whole multi-NC execution rather than a per-NC slot. There is exactly one XU per vcore (`tpb_xu_get_by_vcore` `@0xe7b10`), so the work item is published **once**; a single `sg_count`-wide execution costs `sg_count` doorbell ioctls and one queue publish. The lockstep mechanics are owned by [the submit path §4–§5](submit-path.md); the XU and worker model by [the XU work-queue page](xu-workers.md).

### The zero-alloc resolution

The steady-state path allocates nothing on device for variable placement, because the compiler decided every placement and the runtime instantiated it once at load. The static plan (`kbin_mem_ref` records) is staged at model load into per-subgraph runtime `mem_ref_t` (208 B) records, each carrying a fixed `physical_address`. At execute time, instruction translation resolves a variable through `mem_ref_to_addr` (`@0x22da60`): it bounds-checks `offset + size ≤ mr->size`, rejects an unstaged variable (`physical_address == -1` → *"Variable %s is not staged in HBM"*), and returns `addr = offset + physical_address`. The DMA-ring descriptors built during stage point at those fixed addresses; the per-engine instruction-block addresses patched into `device_exec_request_t` are the same kind of pre-resolved PAs. The only per-execution heap traffic is the `0xD0` sync work item or the `0x38` async request, plus the one-time, cached collectives model-switch descriptor chain. The plan model — `MR_SB`/`MR_VIRTUAL_TMP_BUF`/`MR_BUFFER`/`MR_REMOTE` and the scratchpad arenas — is owned by [the completion engine page §2](completion-engine.md) (which co-documents the static planner) and [static memory planning](../neff/memory-planning.md).

> **NOTE —** the rings and the request buffer are allocated **at model load**, not per execute. A reimplementation that allocates a ring per `nrt_execute` call is functionally correct but defeats the design: submit is meant to be allocation-free, and the *first* execution after a model switch pays a one-time config reload (`reload_seq_config`) while steady-state same-model execution does not. See [the submit path §3](submit-path.md) for the skip-config optimization.

### The steady-state cost budget

The design goal — submit cheap, completion poll-only — is visible as a per-execution resource budget. For one steady-state same-model inference on a vcore of `sg_count` cores, a reimplementer should expect exactly:

| Resource | Steady-state count | Note |
|---|---|---|
| Syscalls on submit | `sg_count` `ioctl(0x80084E29)` | the doorbell; the *only* submit syscalls — everything else is device-mapped DRAM stores |
| Syscalls on complete (sync) | one `read(mark_efd)` + busy-poll `usleep(1)`s | no kernel completion wait; the efd is userspace-written |
| Device allocations | **0** | rings + request buffer allocated at load; variables resolve to fixed PAs (`mem_ref_to_addr`) |
| Host heap allocations | 1 (`0xD0` sync work item *or* `0x38` async request) | plus the one-time, cached CC model-switch desc chain on first switch |
| DMA trigger pairs appended | 3 (req-copy 52 B · evt-poke 4 B · tail-inc 4 B×2) | per stage; built by `al_udma_m2m_build_copy_descriptor` |
| Queue publishes | 1 | one XU per vcore; the work item is enqueued once for all `sg_count` cores |
| Locks held on submit | `submit_work_lock` (funnel) + `compute_req_lock` on `tpbs[0]` (stage) | no per-TPB lock; staging is lockstep-serialized |

> **GOTCHA —** the budget changes on a **model switch** and on the **first execution** of any model: a switch pays one config reload (`reload_seq_config = 1`, table reload if the ACT/DVE hashes differ) and, if the model carries a collectives context, builds-and-caches the CC model-switch descriptor chain on `tpbs[0]` only. A reimplementer profiling "average" submit latency must separate the steady-state case (above) from the first-after-switch case; conflating them mis-attributes the one-time reload to the hot path. The skip-config diff and the CC-chain caching are owned by [the submit path §3](submit-path.md).

---

## 3. Completion is Polled, not Interrupted

After the doorbell ioctl fires the inference, the runtime does **not** wait on an IRQ or a kernel eventfd. The device sequencer DMA-writes 16-byte records into per-engine **Notification-Queue (NQ)** `INFER_STATUS` rings that are mmap'd into the runtime's address space, and the host **reads** them (`notification_read_exec_queue` `@0x2ff170` → `aws_hal_notific_nq_read`). The harvest is the one-step state machine `exec_request_progress_one_step` (`@0x263330`): `INIT → WAIT_BARRIER_PROXY → WAIT_CORE → DONE`. In `WAIT_CORE` it sweeps up to 2 cores × 5 TPB engines (PE/ACT/POOL/DVE/SP), advancing a per-engine sub-state `START_PENDING → END_PENDING → COMPLETE` as `START`/`END` records arrive; when all engines reach `COMPLETE` it transitions to `DONE`.

Two drivers spin the same machine: the synchronous `exec_wait_round_robin` (`@0x2655c0`, `usleep(1)` between steps) — the inner half of `kmgr_exec_wait` that the sync `read(mark_efd)` is ultimately waiting on — and the async XU worker `tpb_xu_step` (`@0xe8940`), which on `DONE` calls `tpb_xu_base_report_complete` (`@0xe8800`) to pop the SPSC slot and signal the `mark_comp_efd` that unblocks the sync caller.

> **QUIRK —** the `mark_comp_efd` the [submit path](submit-path.md) blocks on is written by **userspace** (the worker thread), not by the kernel. Completion is notification-driven, not interrupt-driven: the only host-side blocking primitives are `usleep(1)` (sync busy-poll) and the worker's own scheduling (async). A reimplementer must not wait on a device-fd `poll()` or a kernel signal for completion — there is none on this path. The four-state machine, the 16-byte entry decode, and the timeout-vs-overflow disambiguation (INTC cause bit 29 → `NRT_TIMEOUT=5` vs `SW_NQ_OVERFLOW=1204`) are owned by [the completion engine](completion-engine.md).

The error lane forks out of `WAIT_CORE` on a sub-state regression or a sweep timeout: `exec_request_process_errors` (`@0x2615b0`) → `notification_consume_error_block` (`@0x2ff250`) reads the ERROR ring (NQ#2), classifies the infer-error subtype, and maps it to a status code — `TRANSIENT → NRT_EXEC_COMPLETED_WITH_ERR (1004)`, `NUMERICAL/NaN → _WITH_NUM_ERR (1003)`, uncorrectable ECC → `HW_ERR_NC_UE (1202)`. The full subtype map and the NQ-plane layout are in [the completion engine](completion-engine.md).

There are in fact **two** completion channels layered on the same NQ rings. The whole-inference channel above (`START`/`END` records per engine → `DONE`) is the one the boundary `read(mark_efd)` waits on. A second, *per-tensor* channel rides custom-notification records (subtype `6`, custom sub-id `7`): the device writes a record naming a finished output tensor by `var_id`, the runtime resolves it through `io_mr_to_name_map`, bumps that tensor's completion count, and `pthread_cond_broadcast`s — waking a host thread blocked in `nrt_get_tensor` on that output *before* the whole inference is `DONE`. This is how streaming outputs surface mid-execution; the custom sub-id table is owned by [the completion engine §3.1](completion-engine.md).

---

## 4. Two Async Surfaces: Implicit vs Explicit

The fork in §2 is the **implicit** async surface: a process-global mode flag (`async_exec_workers`) silently re-routes every `nrt_execute` call to a worker pool, transparent to the caller, which still uses the `NRT_2.0.0` synchronous API. There is a second, **explicit** async surface — the `NRT_3.0.0` API (`nrta_execute_schedule` → `nrta_is_completed`) — that a reimplementer must not confuse with it. The two share the same bind→stage→doorbell core but differ in who owns completion.

| Axis | Implicit async (this page's fork) | Explicit async (`NRT_3.0.0`) |
|---|---|---|
| API | `nrt_execute` (`@0x91de0`), same as sync | `nrta_execute_schedule` (`@0x7bb00`) / `nrta_is_completed` (`@0x7c720`) |
| Selector | global `async_exec_workers` `@0xc5d8c8` set at `kmgr_init` | per-call; always explicit-async mode |
| Submit fn | `kmgr_async_exec_add_work` `@0xe6d20` → `kaew_post_request` `@0xe5cd0` | `kmgr_tpb_xu_schedule` `@0xe01d0` → `tpb_xu_schedule_exec` (sync funnel) |
| Exec-mode arg | `0` (`KMGR_EXEC_MODE_ASYNC`) | `1` (`KMGR_EXEC_MODE_EXPLICIT_ASYNC`) |
| Completion | worker thread + `nrt_register_async_exec_callback` `@0x94490` | caller polls a `seq_id` (`kmgr_xu_get_compl_seq` / `kmgr_xu_get_comp_efd`) |
| Host tensors | rejected (`NRT_INVALID`) | rejected (`NRT_INVALID`) |

Both async surfaces reject host-resident tensors — only the synchronous fork clones-and-copies them. The explicit-async submit reuses the *same* `tpb_xu_schedule_request` funnel as sync (it just never blocks on the boundary read), whereas the implicit-async submit takes the separate `kaew_post_request` inline-under-worker-lock path. The explicit surface is owned by [the KMGR facade §6](kmgr-facade.md) and [the submit path §6](submit-path.md); the implicit worker pool by [the XU work-queue page](xu-workers.md).

> **QUIRK —** both async surfaces and the sync path drive the *same* exec-mode field (`kmgr_exec_resources_t.exec_mode` `@+0x20`), but the synchronous `nrt_execute` path stores the `EXPLICIT_ASYNC` enum value (`1`) — because the mode argument is computed as `(async_exec_workers == NULL)`, and a sync process has `workers == NULL`. The enum label `EXPLICIT_ASYNC` therefore tags both the explicit `NRT_3.0.0` API *and* the ordinary synchronous path. Do not read the stored mode value as the caller's intent; it is a fork-internal discriminator, decoded in [submit-path §1](submit-path.md) and [kmgr-facade §2](kmgr-facade.md).

---

## 5. The Five Sub-Pages

This map orients all six Part-VII pages. The table pins which stage of the hot path each owns and the one anchor that page is built around; follow the link for byte-level depth.

| Page | Owns | Primary anchor |
|---|---|---|
| [Exec Manager (KMGR): Facade and Model DB](kmgr-facade.md) | The orchestration facade: model registry, refcount protocol, the `kmgr_exec` fork, unload state machine, and the `dlr_kelf_stage` seam | `dlr_model_set` `@0xc5c720`; `kmgr_exec` `@0xdfd50` |
| [The XU Work-Queue and Worker Threads](xu-workers.md) | The execution-unit model: one XU per vcore, the worker-thread loop, the implicit-async worker pool and its deque/semaphores | `tpb_xu_*` / `kmgr_async_exec_*`; `kmgr_async_exec_init` `@0xe7020` |
| [The Submit Path (Bind → Stage → Doorbell)](submit-path.md) | The submit half end-to-end: bind, the 52-byte request, the three trigger pairs, the doorbell ioctl, publish, and the `read(mark_efd)` boundary | `tpb_xu_schedule_request` `@0xe7540`; `ioctl(0x80084E29)` |
| [The Completion Engine (NQ Harvest & Error Decode)](completion-engine.md) | Everything past the boundary: the four-state harvest, the 16-byte NQ decode, timeout/overflow, error classification — plus the static memory planner | `exec_request_progress_one_step` `@0x263330` |
| [The xu_queue and device_exec_request ABI](xu-queue-abi.md) | The wire layout of the structs the submit/complete pages fill: `xu_queue_t` (256 B), `device_exec_request_t` (52 B), the work-item and request-slot structs | `xu_queue_t`; `device_exec_request_t` (`0x34`) |
| *(this page)* | The map: entry+fork, the bind→stage→doorbell→harvest pipeline, the fan-out and zero-alloc invariant | `nrt_execute` `@0x91de0` |

> **GOTCHA —** the submit and completion pages are split exactly at `read(mark_efd, …, 8)`. The `device_exec_request_t` and `xu_queue_t` byte layouts appear in *both* the submit page (as it fills them) and the ABI page (as the canonical layout). When the two disagree, the ABI page is canonical; the submit page is the usage. A reimplementer building the wire structs reads the ABI page; one building the control flow reads the submit page.

---

## 6. Verification Notes

> The end-to-end chain, the single fork, the lockstep fan-out, and the polled-completion shape were cross-checked against the IDA artifacts of `libnrt.so` 2.31.24.0:
> - Entry/fork: `nrt_execute @0x91de0`, `nrt_execute_repeat @0x91650`, `kmgr_exec @0xdfd50` decompiled; the `async_exec_workers @0xc5d8c8` branch and the inverted exec-mode (`workers==NULL ? 1 : 0`) read directly; host-tensor reject string and `db_get_nn_ref_count @0xdc0c0` confirmed.
> - Bind→stage→doorbell: `kmgr_exec_pre @0xdf820`, `kbl_compute_build_compute_resources @0x306790`, `tpb_xu_schedule_request @0xe7540`, `hw_exec_queue_add_exec_request_impl @0x320810`, `dlr_kickoff_exec @0xdd890`, `ndl_nc_semaphore_increment @0xc3ba0` all nm-verified; `device_exec_request_t` size `0x34` proven by the two `dmem_buf_copyin` lengths; `ioctl 0x80084E29` (`NEURON_IOCTL_SEMAPHORE_INCREMENT`, #41) the only submit-path syscall.
> - Fan-out: `kbl_compute_setup @0x306fb0` single `compute_req_lock` on `tpbs[0]`; `dlr_kickoff_exec` loops `sg_count`; `tpb_xu_get_by_vcore @0xe7b10` one XU per vcore — read from decompilation.
> - Harvest: `exec_request_progress_one_step @0x263330` (8847 B) state machine and the `{1,0,2}` return discipline; `exec_wait_round_robin @0x2655c0` / `tpb_xu_step @0xe8940` drivers; `tpb_xu_base_report_complete @0xe8800` `mark_comp_efd` signal; INTC cause bit 29 disambiguation — all confirmed.
> - Zero-alloc: `mem_ref_to_addr @0x22da60` (`addr = offset + physical_address`; `-1` ⇒ unstaged) confirmed; `seq_id = (tail & 0xFFFFFFFFFFFF) | (xu_id<<48)`, `NRTA_SEQ_NUM_MAX = 0xFFFFFFFFFFFE` from `xuq_client_submit @0xe9f10`.
> - Async surfaces: implicit `kmgr_async_exec_add_work @0xe6d20` → `kaew_post_request @0xe5cd0`, worker pool `kmgr_async_exec_init @0xe7020`, callback `nrt_register_async_exec_callback @0x94490`; explicit `nrta_execute_schedule @0x7bb00` / `nrta_is_completed @0x7c720` → `kmgr_tpb_xu_schedule @0xe01d0` — all nm-verified addresses; the implicit-vs-explicit split is read from the `kmgr_exec` fork plus the explicit-API callers.
>
> **[MED]** The exact `sg_count` ceiling (1 or 2 cores per vcore on this arch) is treated as `[2]` from the harvest state struct's `[2]`-core arrays; LNC topologies with wider fan-out are arch-dependent and owned by the vcore/topology cell, not pinned here.
> **[NOTE]** This map deliberately does not re-derive any struct's byte layout, the skip-config diff, the NQ-entry bit fields, or the static-plan staging math — each is owned and pinned by the sibling page named in §5.

---

## Cross-References

- [The Submit Path (Bind → Stage → Doorbell)](submit-path.md) — the submit half end-to-end: the 52-byte request, three trigger pairs, doorbell ioctl, publish, and the `read(mark_efd)` boundary
- [The Completion Engine (NQ Harvest & Error Decode)](completion-engine.md) — everything past the boundary: the polled NQ harvest, error classification, `mark_comp_efd` write, and the static memory planner
- [Exec Manager (KMGR): Facade and Model DB](kmgr-facade.md) — the orchestration facade, the model registry, and the `kmgr_exec` fork this map opens with
- [The XU Work-Queue and Worker Threads](xu-workers.md) — one XU per vcore, the worker-thread loop, and the implicit-async worker pool
- [The xu_queue and device_exec_request ABI](xu-queue-abi.md) — the canonical wire layout of the queue and request structs the submit/complete pages fill
- [Public C API: Lifecycle and Init/Teardown](../runtime/api-lifecycle.md) — the `nrt_init`/`nrt_close` lifecycle whose `NRT_STATE_INIT` guard `nrt_execute` checks first
- [back to index](../index.md)
