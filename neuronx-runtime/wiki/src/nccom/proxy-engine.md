# Proxy & Progress Engine (and the libnrt Bridge)

> *Cross-binary page. The **libnccom half** pins to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**). The **libnrt half** pins to `libnrt.so.2.31.24.0` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; SONAME `libnrt.so.1`; source unit `/opt/workspace/KaenaRuntime/enc/proxy_queue.cc`). Both ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text` VMA == file offset for the cited bands. Every `0x…` is tagged to the binary it belongs to (`nccom:0x…` vs `nrt:0x…` where ambiguity is possible).*
> *Evidence grade: **Confirmed (byte-anchored)** — all struct sizes/offsets and both run-state enums read from each binary's own DWARF (`nm`/`objdump`/`gdb ptype /o`); the seam call chain read from decompiled bodies on both sides; vtable step() slots read from `.data.rel.ro`. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

The proxy engine is the steady-state runtime of a Neuron collective, and it is **host-thread-driven**: there are no device-side warp kernels (see [overview](overview.md) — *the proxy is the primitive*), so a collective makes forward progress only because a host thread repeatedly calls a progress function until the operation reports done. This page documents that loop, and the fact that determines its whole shape: **the loop spans two binaries.** The driver thread lives in **libnrt** (`enc_proxy_queue::process`, `nrt:0x1cf7c0`); the op-FIFO, the per-op transport state machine, and the actual EFA/libfabric I/O live in **libnccom** (`ncclProxyProgressOps`, `nccom:0x40a80`). They meet at one exported symbol — libnccom's `neuronNetworkProxyProgress` (`nccom:0x48930`) — which libnrt reaches through a dlopen'd trampoline. **The seam is the point of this page.**

If you already hold a mental model of upstream NVIDIA NCCL's `proxy.cc`, you already know libnccom's half. Upstream has a per-comm persistent proxy thread (`ncclProxyService`) that owns an `ncclProxyArgs` op-FIFO and, in a `while` loop, walks the active op list calling each op's `progress()` callback, recycling completed ops. libnccom keeps the FIFO, the `ncclProxyArgs` (352 bytes), the active/pending/free three-list structure, and the `progress()` dispatch — but it **deletes the thread**. The only `pthread_create` sites in the whole `.so` are in bootstrap (`bootstrapInitWithHierarchy`, `bootstrapCreateRoot`), never a proxy thread; the `"[Proxy Thread]"` string (`nccom:0x8e7c8`) is a log tag, not a spawned worker. In its place, the progress loop is exported as three plain functions and **driven synchronously by libnrt**. So the familiar delta is exactly one move: NCCL's internal proxy thread has been lifted out of the library and re-homed in the runtime that loads it.

The libnrt half is the part that has no NCCL analogue. libnrt owns one **persistent worker thread per shared-proxy group** (`"proxy-<tid>"`), a process-global **refcounted** registry of those queues (`g_shared_proxy_queues`, `nrt:0xc91320`), and two polymorphic **proxy-task** kinds (a network task that drives libnccom, and a host-side barrier task). The worker's `process()` loop drains an enqueue deque into a per-rank active-task map, calls each task's virtual `step()`, and publishes a status object that a *separate* consumer thread (the exec/host-cc path) polls. The page is organized as five `##` units: the cross-binary loop as a whole; libnrt's **driver thread + op queue**; libnrt's **network task `step()` state machine** (the producing side of the seam); libnccom's **progress loop + op-FIFO** (the consuming side); and the **device-trigger handshake** that interlocks the host loop with the on-device collective program. The per-op send/recv transport state machines (`netNeuron{Send,Recv}Proxy`, the `requestTracker` rings, the RDMA-write-credit protocol) are owned by **[send-recv-prims](send-recv-prims.md)** and only referenced here.

For reimplementation, the contract of the proxy/progress layer is:

- **The host-thread-driven progress model** — no GPU kernel, no self-spawned proxy thread inside libnccom. Forward progress is `while (!done) progress(&idle, &done);` run by an *external* driver. A reimplementer who ports NCCL's `ncclProxyService` thread into the collective library has put the thread on the wrong side of the boundary.
- **The op-FIFO and three-list discipline** — libnccom's per-stream `ncclProxyState` (192 B, array of 4) with its `nextOps` (pending-enqueue) → `ops` (active) → `pool` (free) lists and the `basic_block_args_map` deferred-by-block queue; ops are *recycled*, not freed, until a basic block fully drains.
- **The seam ABI** — the three exported entry points (`neuronStartNetworkProxy` / `neuronNetworkProxyProgress` / `neuronStopNetworkProxy`) and their libnrt-side trampolines (`ncclStartNetworkProxy` / `ncclNetworkProxyProgress` / `ncclStopNetworkProxy`), which call *through* a `.bss` neuron-slot table populated at dlopen — the same load mechanism as the init ABI ([abi](abi.md)).
- **The two libnrt-side state machines** — the queue-level loop (`process`: drain → step → recycle DONE → wait/yield) and the task-level `proxy_task_run_state_t` machine (INIT → WAITING_DEVICE_TRIGGER → PROGRESSING ⇄ IDLE → DONE), plus the device-trigger host-mem handshake (`can_start_proxy_task` spins on `-1`) that gates a task's first tick.

| | |
|---|---|
| **Loop shape** | host thread, no GPU kernel; `while (!done) progress()` spanning 2 binaries |
| **Driver thread (libnrt)** | `enc_proxy_queue::process` `nrt:0x1cf7c0` — `"proxy-<tid>"`, one per shared-proxy group |
| **Queue registry (libnrt)** | `g_shared_proxy_queues` `nrt:0xc91320` — `std::map<int gid, shared_network_proxy_queue>`, **refcounted** |
| **Producer (libnrt)** | `enc_enq_proxy_tasks` `nrt:0x105560` → `append_task` `nrt:0x1cf3c0` (cv-notify worker) |
| **Network task step (libnrt)** | `enc_network_proxy_task::step` `nrt:0x1d0620` (vtable `nrt:0xbf6b18` slot0); jump-table `nrt:0x858260` |
| **The seam call (libnrt → libnccom)** | `proxy_progress` `nrt:0x1d03f0` → `ncclNetworkProxyProgress` `nrt:0x1c1aa0` → **`neuronNetworkProxyProgress` `nccom:0x48930`** |
| **Progress entry (libnccom)** | `neuronNetworkProxyProgress` `nccom:0x48930` → `ncclProxyProgress` `nccom:0x40f40` → `ncclProxyProgressOps` `nccom:0x40a80` |
| **Op descriptor (libnccom)** | `ncclProxyArgs` — **352 B** (`0x160`); free-listed in `ncclProxyPool` blocks of `calloc(0xB008)` = 8 + 128×352 |
| **Per-stream state (libnccom)** | `ncclProxyState` — **192 B**, array[4] at `ncclComm`+18384; 4 streams (`stream_idx` 0..3) |
| **Enqueue / drain (libnccom)** | `neuronStartNetworkProxy` `nccom:0x483a0` · `neuronStopNetworkProxy` `nccom:0x487d0` |
| **Op-level state machine** | `args->state` (off 80): 0 = free, 1 = active; per-op transport state via `reqStage` enum |
| **Task-level state machine (libnrt)** | `proxy_task_run_state_t`: INIT=0, PROGRESSING=1, WAITING_DEVICE_TRIGGER=2, IDLE=3, DONE=4 |
| **Device-trigger gate (libnrt)** | `can_start_proxy_task` `nrt:0x1ce7d0` — spins until no `start_proxy_task_host_mem_addrs[i] == 0xFFFFFFFF` |

> **QUIRK — there is no proxy thread inside libnccom, and no GPU at all.** A reimplementer porting upstream NCCL will look for `ncclProxyService` / the proxy `pthread_create` and the device warp kernel that the proxy feeds. Neither exists here. `nm libnccom.so` finds zero device-primitive templates and `cudaLaunchKernel`; an exhaustive `pthread_create` xref finds **3 sites, all in bootstrap** (`bootstrapInitWithHierarchy` `nccom:0x3b695`, `bootstrapCreateRoot` `nccom:0x3cb2b`/`0x3cc09`) — none a proxy worker. The whole progress engine is **passive**: it advances exactly one tick per call to `neuronNetworkProxyProgress` and otherwise does nothing. The thread that calls it in a loop lives in **libnrt** (`enc_proxy_queue::process`). Get the thread on the right side of the seam, or the collective never makes progress.

---

## 1. The Cross-Binary Progress Loop

### Purpose

One collective's network phase is driven by one full pass through two binaries: libnrt's worker thread pops a task, the task's `step()` calls into libnccom's progress entry, libnccom walks its op-FIFO running each op's transport callback (which posts/polls EFA), and the boolean `done` propagates back up to libnrt, which recycles the task. This unit fixes the geography — which half owns which structure, and where the single function-pointer seam sits — before the later units detail each half.

### The seam, end to end

```text
 ┌─────────────────────────────── libnrt.so (the DRIVER) ───────────────────────────────┐
 │                                                                                       │
 │  PRODUCER (exec/prepare thread)                                                       │
 │    nrt_cc_prepare / kbl_exec_cc_enq_proxy_tasks_internal                              │
 │      └─ enc_enq_proxy_tasks            nrt:0x105560                                    │
 │           ├─ new enc_network_proxy_task ─┐                                            │
 │           └─ new enc_barrier_proxy_task ─┤ append_task nrt:0x1cf3c0 → cv.notify_all   │
 │                                          ▼                                            │
 │  enc_proxy_queue  (one per shared-proxy GROUP gid; refcounted in g_shared_proxy_queues)│
 │    +0x000  queue        (deque)  ── pending in-list                                   │
 │    +0x050  active_tasks  (map<rank, list>) ── per-rank running tasks                  │
 │    +0x0d8  state_queue  (list)   ── published status (consumer reads)                 │
 │                                          │                                            │
 │  WORKER THREAD "proxy-<tid>"   enc_proxy_queue::process  nrt:0x1cf7c0                  │
 │    loop while !stop:                                                                  │
 │      drain queue → active_tasks[task->get_rank_id()]                                  │
 │      for task in active_tasks:                                                        │
 │        task->step()  ── vtbl slot0 ──┐                                                │
 │           enc_network_proxy_task::step nrt:0x1d0620                                   │
 │             PROGRESSING/IDLE:                                                         │
 │               proxy_progress(&idle,&done) nrt:0x1d03f0                                │
 │                 ncclNetworkProxyProgress  nrt:0x1c1aa0  (trampoline)                  │
 │                       │                                                               │
 └───────────────────────┼───────────────────────────────────────────────────────────────┘
                         │  call *neuron_slot_table[..]  (.bss 0xc967b8.., dlopen-populated)
                         ▼   *** THE SEAM ***  (one exported fn-ptr)
 ┌─────────────────────────────── libnccom.so (the FIFO + TRANSPORT) ─────────────────────┐
 │  neuronNetworkProxyProgress  nccom:0x48930   (thin: AND idle/done; map err → FAILURE)   │
 │    └─ ncclProxyProgress      nccom:0x40f40   (reload pending bb | drain)                │
 │         └─ ncclProxyProgressOps  nccom:0x40a80   *** THE OP-FIFO LOOP ***               │
 │              for op in state->ops:                                                      │
 │                op->progress(op,…)  == netNeuronSendProxy nccom:0x4e510                  │
 │                                    /  netNeuronRecvProxy nccom:0x4fe20                  │
 │                   ├─ ncclNet->{isend,irecv,iread,iflush,test}  ──► EFA / libfabric      │
 │                   └─ nec_inc_semaphore(...)  ──► back into libnrt (device completion)   │
 │                if op->state==0: recycleOp (re-file under basic_block_args_map[bb])      │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

The asymmetry is the headline: **libnrt owns the thread and the enqueue/status plumbing; libnccom owns the op-FIFO and the network I/O.** They share exactly four exported entry points (the seam ABI, §1 Function Map), and libnccom calls *back* into libnrt through 16 `nec_*` thunks (the device-completion reverse edge, [abi](abi.md)). Neither side has a thread the other can see: libnrt's worker is invisible to libnccom (which is purely passive), and libnccom's transport callbacks are invisible to libnrt (which only sees the `done` boolean).

### The seam ABI (libnrt trampoline → libnccom export)

libnrt never calls libnccom by name after init. `ncclInit`'s `dlopen("libnccom.so")` resolves the `neuron*` exports into a `.bss` slot table (`nrt:0xc967b8..0xc968d8`); the three proxy entry points are reached through libnrt-internal trampolines that index that table.

| libnrt trampoline | Addr | → libnccom export | Addr | Seam role | Confidence |
|---|---|---|---|---|---|
| `ncclStartNetworkProxy` | `nrt:0x1c16f0` | `neuronStartNetworkProxy` | `nccom:0x483a0` | enqueue one collective's net ops (per basic block) | HIGH |
| `ncclNetworkProxyProgress` | `nrt:0x1c1aa0` | `neuronNetworkProxyProgress` | `nccom:0x48930` | **advance one tick** — the hot seam | HIGH |
| `ncclStopNetworkProxy` | `nrt:0x1c18e0` | `neuronStopNetworkProxy` | `nccom:0x487d0` | drain/abort a stream on basic-block switch | HIGH |
| (MR helper) | — | `neuronNetGetMrKey` | `nccom:0x49500` | MR-key accessor (transport, not progress) | HIGH |

> **NOTE — `neuronNetworkProxyProgress` is a thin wrapper, by design.** The exported function (`nccom:0x48930`) does almost nothing itself: it calls `ncclProxyProgress`, **ANDs** the returned `idle`/`done` ints into the caller's `*idle`/`*done` (so a multi-call accumulation across streams converges to "all idle / all done"), and maps any nonzero internal result to `NRT_NETWORK_PROXY_FAILURE`. The actual loop is one frame deeper, in `ncclProxyProgressOps`. The thinness is what makes the call cheap enough to spin on.

---

## 2. The Driver — libnrt Worker Thread and Op Queue

### Purpose

This is the half with no NCCL analogue: the thread that turns libnccom's passive progress function into a running collective. libnrt holds one `enc_proxy_queue` (296 B) per **shared-proxy group**, each owning a persistent `"proxy-<tid>"` worker. Many global comms that fall in the same group (grouped by `NEURON_RT_RANKS_PER_NETWORK_PROXY`) **share** one queue and one thread, refcounted in the process-global `g_shared_proxy_queues` map. The worker's `process()` loop is the outer driver: drain enqueued tasks into a per-rank active map, `step()` each, recycle the DONE, and block or spin when idle.

### Entry Point

```text
enc_setup_global_comm_internal nrt:0x10b050   ── CREATE/SHARE queue
  under g_shared_proxy_queues_lock (nrt:0xc912e0):
    find g_shared_proxy_queues[gid]
      HIT  : node.refcnt++           ── reuse existing queue + thread
      MISS : new enc_proxy_queue; ncclSetAffinity + sched_setaffinity;
             enc_proxy_queue::start nrt:0x1ce4d0
               └─ pthread_create(&tid, worker_thread)
                    worker_thread nrt:0x1d0120  ── name "proxy-<tid>"; → process()

enc_proxy_queue::process nrt:0x1cf7c0   ── THE DRIVER LOOP (runs until stop)
```

### Algorithm — the queue loop

```c
// enc_proxy_queue::process — nrt:0x1cf7c0  [HIGH: drain/step/recycle read from disasm]
void enc_proxy_queue::process():                      // worker thread body
    while (!this->stop):                              // +0x120, set by ~enc_proxy_queue
        lock(this->mtx);                              // +0xb0 — guards queue + active_tasks

        // (1) DRAIN: move newly enqueued tasks into the per-rank active map
        while (!this->queue.empty()):                 // +0x000 deque, filled by append_task
            task = this->queue.pop_front();
            rank = task->get_rank_id();               // nrt:0x1ce540 — encd_context glb_comm rank
            this->active_tasks[rank].push_back(task); // +0x050 map<int rank, list<task>>

        // (2) STEP: advance every active task one tick
        idle_count = 0;
        for (rank, list) in this->active_tasks:
            for task in list:
                task->step();                          // VIRTUAL slot0 — net=0x1d0620 / barrier=0x1ce830
                // step() publishes task->proxy_state->{progress_status, run_state}
                rs = task->proxy_state->run_state;     // proxy_task_state +0x14
                if (rs == PROXY_TASK_STATE_IDLE)   ++idle_count;        // ==3
                if (rs == PROXY_TASK_STATE_DONE):                       // ==4
                    list.erase(task);                  // RECYCLE: unhook the finished task
        prune empty rank buckets;

        unlock(this->mtx);

        // (3) BLOCK or SPIN when there is no forward progress to make
        if (this->active_tasks.empty() && this->queue.empty()):
            this->cv.wait(this->mtx);                  // +0x080 — sleep until append_task notifies
        else if (idle_count == active_task_count):     // all tasks idle (waiting on net/device)
            if (nrt_config.one_thread_per_core)         // +0x9c
                /* busy-spin */ ;                      // dedicated core → keep polling hot
            else
                sched_yield();                         // shared core → yield between ticks
```

> **GOTCHA — IDLE does not mean "stop calling".** A task reporting `run_state == IDLE` (3) has made no progress *this tick* (the network has nothing to complete yet), but it is **not** done. The loop keeps it in `active_tasks` and keeps calling `step()` — it only changes its *waiting strategy* (busy-spin on a dedicated core, `sched_yield` otherwise). Only `DONE` (4) removes a task. A reimplementer who treats IDLE as a terminal state will drop in-flight collectives on the floor. The `cv.wait` is reached only when there is **nothing** active and **nothing** queued — genuine quiescence, not a single idle tick.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `enc_proxy_queue::process` | `nrt:0x1cf7c0` | the driver loop: drain → step → recycle → wait/yield | HIGH |
| `enc_proxy_queue::start` | `nrt:0x1ce4d0` | `pthread_create(&tid, worker_thread)`; `NRT_RESOURCE` on fail | HIGH |
| `enc_proxy_queue::worker_thread` | `nrt:0x1d0120` | `gettid`; `pthread_setname_np "proxy-<tid>"`; → `process()` | HIGH |
| `enc_proxy_queue::append_task` | `nrt:0x1cf3c0` | hook status into `state_queue`; push task to `queue`; `cv.notify_all` | HIGH |
| `enc_proxy_queue::check_proxy_task_status` | `nrt:0x1cf1c0` | consumer view: walk `state_queue`, match ctx+type, return status/done | HIGH |
| `enc_proxy_queue::~enc_proxy_queue` | `nrt:0x1ceb50` | `stop=1`; `cv.notify_all`; `pthread_join`; teardown | HIGH |
| `enc_enq_proxy_tasks` | `nrt:0x105560` | PRODUCER: `new` network + barrier task, `append_task` each | HIGH |
| `enc_setup_global_comm_internal` | `nrt:0x10b050` | create/share queue under lock; `refcnt++` on hit | HIGH |
| `enc_free_global_comm` | `nrt:0x10a650` | `refcnt--`; destroy queue (joins worker) at 0 | HIGH |

### Considerations

The queue is **shared and refcounted**, which is the subtle part of the lifecycle. `enc_setup_global_comm_internal` keys `g_shared_proxy_queues` by `gid` (the shared-proxy group id); a second comm in the same group does **not** create a thread — it bumps `refcnt` (node+0x30) and reuses the existing queue and worker. `enc_free_global_comm` decrements; only the transition to `refcnt == 0` runs `~enc_proxy_queue`, which sets `stop`, notifies the cv, and `pthread_join`s. A reimplementer must honor this: the worker thread's lifetime is the *group's* lifetime, not any single comm's. The two-mutex split inside the queue (`mtx` @+0xb0 guards `queue`/`active_tasks`; `state_mtx` @+0xf0 guards `state_queue`) lets the worker advance tasks while a consumer polls status without contending on one lock — the published status object (`enc_proxy_task_state`, 24 B) is the only shared surface between them.

---

## 3. The Producing Side of the Seam — Network Task `step()`

### Purpose

`enc_network_proxy_task::step()` (`nrt:0x1d0620`) is the libnrt object that owns the libnccom call. It is the per-task state machine the worker invokes through vtable slot 0; its states map one-to-one onto the three seam calls — INIT issues `ncclStartNetworkProxy` (one per basic block), PROGRESSING issues `ncclNetworkProxyProgress`, and a basic-block transition issues `ncclStopNetworkProxy`. Its state variable is `proxy_task_run_state_t` at `proxy_state+0x14`, dispatched through a 5-entry jump table at `nrt:0x858260`.

### Entry Point

```text
enc_network_proxy_task::step nrt:0x1d0620   (vtable nrt:0xbf6b18 slot0; jumptable nrt:0x858260)
  INIT(0)                       → init_network_proxy nrt:0x1ce5b0
                                    build_fifo_block_ids → per-bb ncclStartNetworkProxy (== B1)
  PROGRESSING(1) / IDLE(3)      → proxy_progress nrt:0x1d03f0
                                    ncclNetworkProxyProgress (== B2 SEAM) [ ncclStopNetworkProxy on bb switch (== B3) ]
  WAITING_DEVICE_TRIGGER(2)     → can_start_proxy_task? → clear flag → PROGRESSING(1)
  DONE(4)                       → nlog "Task shouldn't be polled after it reached a done state"
```

### Algorithm — the task state machine

```c
// enc_network_proxy_task::step — nrt:0x1d0620  [HIGH: jumptable + call sites read from disasm]
NRT_STATUS enc_network_proxy_task::step():
    switch (this->proxy_state->run_state):              // +0x14
      case INIT:                                        // 0
        if (ctx->skip_proxy /* ctx+0x108 */):           // debug/early-completion NOP path (MED)
            proxy_state->progress_status = NRT_SUCCESS;
            proxy_state->run_state = DONE;              // 4 — skip the whole net stage
            break;
        // build the per-basic-block FIFO triple and enqueue each block into libnccom
        init_network_proxy();                            // nrt:0x1ce5b0 — see below
        if (ctx->wait_for_device /* ctx+0x10c && ctx+0x380 */)
            proxy_state->run_state = WAITING_DEVICE_TRIGGER;   // 2 — gate on device write
        else
            proxy_state->run_state = PROGRESSING;        // 1
        break;

      case PROGRESSING:                                  // 1
      case IDLE:                                         // 3  (re-entered every tick while in flight)
        bool idle = false, done = false;
        proxy_state->progress_status =
            proxy_progress(&idle, &done);                // nrt:0x1d03f0 — THE SEAM CALL
        if (done)      proxy_state->run_state = DONE;     // 4
        else if (idle) proxy_state->run_state = IDLE;     // 3 (loop keeps ticking it)
        break;

      case WAITING_DEVICE_TRIGGER:                       // 2
        if (can_start_proxy_task()):                     // nrt:0x1ce7d0 — host-mem != -1 ?
            clear_start_proxy_task_flag();               // nrt:0x1ce810 — consume the trigger (write 0)
            proxy_state->run_state = PROGRESSING;        // 1
        break;                                           // else stay WAITING; worker re-ticks

      case DONE:                                         // 4
        nlog("Task shouldn't be polled after it reached a done state");  // nrt:0x7f81b8
        break;
    return proxy_state->progress_status;

// init_network_proxy — nrt:0x1ce5b0  [HIGH: ncclStartNetworkProxy call site read]
void enc_network_proxy_task::init_network_proxy():
    comm = encd_get_global_comm();
    build_fifo_block_ids();                              // nrt:0x124f20 — pack enc_fifo_set per basic block
    for (bb in basic_blocks):
        enc_fifo_set *fs = &bb_map[bb];                  // unordered_map<bb_id, enc_fifo_set>
        // hand each vector's (base,count) straight across the seam — B1
        ncclStartNetworkProxy(comm, stream, bb,
            fs->net_ops_info_fifo.data(),  fs->net_ops_info_fifo.size(),   // == B1 / nccom:0x483a0
            fs->src_addr_fifo.data(),      fs->src_addr_fifo.size(),
            fs->dest_addr_fifo.data(),     fs->dest_addr_fifo.size());
    setup_first_basic_block_switch();                    // nrt:0x1ce560

// proxy_progress — nrt:0x1d03f0  [HIGH: both seam calls read]
NRT_STATUS enc_network_proxy_task::proxy_progress(bool *idle, bool *done):
    comm = encd_get_global_comm();
    bool switched = false;
    check_basic_block_switch(cur_bb, &switched);         // nrt:0x1d01c0 — advance bb when old drains
    NRT_STATUS rc = ncclNetworkProxyProgress(comm, stream, bb, idle, done);  // == B2 / nccom:0x48930
    if (switched)
        ncclStopNetworkProxy(comm, stream);              // == B3 / nccom:0x487d0 — drain old block
    return rc;
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `enc_network_proxy_task::step` | `nrt:0x1d0620` | task state machine (vtbl slot0); 5-state jumptable | HIGH |
| `enc_network_proxy_task::init_network_proxy` | `nrt:0x1ce5b0` | build FIFOs + per-bb `ncclStartNetworkProxy` (B1) | HIGH |
| `enc_network_proxy_task::proxy_progress` | `nrt:0x1d03f0` | the seam call `ncclNetworkProxyProgress` (B2) + `ncclStopNetworkProxy` (B3) | HIGH |
| `enc_network_proxy_task::check_basic_block_switch` | `nrt:0x1d01c0` | decide when to drain old bb / start new | HIGH |
| `enc_barrier_proxy_task::step` | `nrt:0x1ce830` | sibling task: host barrier (send/recv + leader SPS sema) | HIGH |
| `ncclStartNetworkProxy` (trampoline) | `nrt:0x1c16f0` | → `neuronStartNetworkProxy` `nccom:0x483a0` | HIGH |
| `ncclNetworkProxyProgress` (trampoline) | `nrt:0x1c1aa0` | → `neuronNetworkProxyProgress` `nccom:0x48930` | HIGH |
| `ncclStopNetworkProxy` (trampoline) | `nrt:0x1c18e0` | → `neuronStopNetworkProxy` `nccom:0x487d0` | HIGH |

### Considerations

Two tasks are enqueued per CC op — one `enc_network_proxy_task` and one `enc_barrier_proxy_task` — but only the network task crosses the seam; the barrier task is a pure host-side collective barrier (`enc_barrier` send/recv plus a leader-only SPS semaphore broadcast, `inc_leader_sps_barrier_done_semas` `nrt:0x1ce7b0`) and never touches libnccom. The basic-block machinery is the multi-block pipelining knob: `init_network_proxy` enqueues *every* block's ops up front into libnccom's deferred `basic_block_args_map`, and `check_basic_block_switch` advances the active block as each drains, pairing with libnccom's `proxyReloadProxyArgs` (§4). The three INIT-gate fields (`ctx+0x108` skip, `ctx+0x10c`/`ctx+0x380` device-wait) are per-context debug/early-completion flags consistent with the RT's `dbg_proxy_early_completion` / `dbg_proxy_early_posting` config; their exact owning struct was not fully dumped (MED).

---

## 4. The Consuming Side of the Seam — libnccom Op-FIFO and Progress Loop

### Purpose

This is the half a reimplementer can lift mostly from upstream NCCL `proxy.cc`. libnccom keeps the op-FIFO (`ncclProxyArgs`, 352 B), the per-stream three-list structure (`ncclProxyState`, 192 B × 4), and the generic progress loop that calls each op's `progress()` callback and recycles completed ops. The fork's one structural addition is the **per-basic-block deferred queue** (`basic_block_args_map`): enqueue parks ops by block, and the loop pulls one block's ops into the active list at a time — the libnccom counterpart of libnrt's `check_basic_block_switch`.

### Entry Point

```text
neuronNetworkProxyProgress nccom:0x48930   (thin wrapper: AND idle/done; err → NRT_NETWORK_PROXY_FAILURE)
  └─ ncclProxyProgress      nccom:0x40f40
       ├─ if state->ops empty: proxyReloadProxyArgs nccom:0x40e70  (basic_block_args_map[bb] → state->ops)
       └─ ncclProxyProgressOps nccom:0x40a80   *** THE LOOP ***
            for op in state->ops:
              op->progress(op,…)   ── netNeuronSendProxy nccom:0x4e510 / netNeuronRecvProxy nccom:0x4fe20
              if op->state == 0: recycleOp (inlined)
```

### Algorithm — the op-FIFO loop and recycle

```c
// ncclProxyProgressOps — nccom:0x40a80  [HIGH: progressOps/recycleOp asserts + loop read from disasm]
ncclResult_t ncclProxyProgressOps(ncclComm *comm, uint32 stream_id, uint32 bb_id,
                                  int *idle, int *done):
    state = &comm->proxyStateStream[stream_id];        // +18384 + 192*stream_id
    op = state->ops;                                   // +8 — active list head
    prev = &state->ops;
    while (op != NULL):
        assert(op->opCount < comm->lastOpCount);        // sequencing guard (comm+17696)
        assert(op->basic_block_id == state->current_basic_block_id);   // progressOps assert

        op->progress(op, …);                            // == netNeuron{Send,Recv}Proxy — see send-recv-prims
        *idle &= op->idle;                              // op->idle @ +192 — ANDed across all ops

        if (op->state != 0):                            // still active → keep it, advance
            prev = &op->next; op = op->next;            // +320
        else:                                           // op fully drained → RECYCLE (not free)
            *prev = op->next;                           // unlink from active list
            // re-file the op under its basic block's deferred queue
            assert(state->basic_block_args_map.count(op->basic_block_id));   // recycleOp assert
            chain op back into basic_block_args_map[op->basic_block_id];
            op->transportComm->freeRequest(op);         // netNeuronFreeRequest — drain requestTracker
            op->posted = 0; op->pre_post_fifo_idx = 0;  // reset ring cursors
            op->transportComm->allocRequest(op);        // netNeuronAllocRequest — fresh requestTracker
            op->state = 1;                              // re-arm for the next basic block
            op = *prev;
    return ncclSuccess;
  on error: comm->fatalError = rc;                       // comm+18184

// ncclProxyProgress — nccom:0x40f40  [HIGH]
ncclResult_t ncclProxyProgress(ncclComm *comm, uint32 stream_id, uint32 bb_id,
                               int *idle, int *done):
    state = &comm->proxyStateStream[stream_id];
    if (state->ops == NULL):                            // no active block → pull one in
        proxyReloadProxyArgs(state, bb_id);             // nccom:0x40e70: map[bb] → state->ops
    else:
        state->pending_basic_block_id = bb_id;          // +176 — stash the next block
    if (state->current_basic_block_id == (uint32)-1):    // nothing to run
        *idle = 1; *done = 1; return ncclSuccess;
    rc = ncclProxyProgressOps(comm, stream_id, bb_id, idle, done);
    if (*done && state->pending_basic_block_id != -1):    // block finished but another is queued
        *idle = 0; *done = 0;                           // force a reload next tick, don't report done yet
    return rc;
```

### Op-FIFO structure (the three lists + the deferred map)

```text
ncclProxyState  (192 B, one per stream, array[4] @ ncclComm+18384)
  +8   ops        ──► active op list      (walked by ncclProxyProgressOps)
  +16  nextOps    ──► pending-enqueue head (filled by ncclProxySaveNeuron, flushed by ncclProxyStart)
  +32  pool       ──► free ncclProxyArgs   (popped on Save, replenished on Stop/Destroy)
  +40  pools      ──► ncclProxyPool blocks  (calloc 0xB008 = 8 hdr + 128×352)
  +120 basic_block_args_map  ──► unordered_map<uint32 bb_id, ncclProxyArgs*>  DEFERRED-BY-BLOCK queue

 ENQUEUE   nextOps ──ncclProxyStart──► basic_block_args_map[bb]
 ACTIVATE  basic_block_args_map[bb] ──proxyReloadProxyArgs──► ops
 RECYCLE   ops (drained op) ──recycleOp──► basic_block_args_map[bb]   (loops until block fully done)
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `neuronNetworkProxyProgress` | `nccom:0x48930` | exported wrapper; AND idle/done; map err → `NRT_NETWORK_PROXY_FAILURE` | HIGH |
| `ncclProxyProgress` | `nccom:0x40f40` | one tick: reload pending bb or drain; `bb_id == -1` short-circuit | HIGH |
| `ncclProxyProgressOps` | `nccom:0x40a80` | the op-FIFO loop + inlined `recycleOp` | HIGH |
| `proxyReloadProxyArgs` | `nccom:0x40e70` | `basic_block_args_map[bb]` → `state->ops`; set `current_basic_block_id` | HIGH |
| `ncclProxySaveNeuron` | `nccom:0x405f0` | pop free op, copy args, `allocRequest`, append to `nextOps` | HIGH |
| `ncclProxyStart` | `nccom:0x41230` | flush `nextOps` chain → `basic_block_args_map[bb]` | HIGH |
| `neuronStartNetworkProxy` | `nccom:0x483a0` | B1 export: validate FIFOs, `Save` send+recv, `ProxyStart` | HIGH |
| `neuronStopNetworkProxy` | `nccom:0x487d0` | B3 export: free all ops + map lists; return to pool; drop hashtable | HIGH |
| `ncclProxyDestroy` | `nccom:0x40a30` | comm teardown: free all 4 `ncclProxyPool` chains | HIGH |

### Considerations

The single most counter-intuitive invariant is that **ops are recycled, not freed, until a basic block fully drains.** When `netNeuron{Send,Recv}Proxy` reports an op complete (`op->state == 0`), the loop does not return the op to the free pool — it re-files the op under `basic_block_args_map[bb_id]`, tears down its `requestTracker` (`freeRequest`), resets the ring cursors, allocates a *fresh* `requestTracker` (`allocRequest`), and sets `state = 1` again. The op cycles back through the same basic block until the block is exhausted; only `ncclProxyStart`'s deferred-queue interaction and the `pending_basic_block_id` reload logic terminate it. This is why `ncclProxyProgress` deliberately clears `*done` when a block finished but another is pending (`pending_basic_block_id != -1`): reporting `done` prematurely would let libnrt recycle the task before the next block runs. The `ncclProxyArgs.requests[8]` field (64 B at off 128) is NCCL-legacy: only `requests[0]` (the `requestTracker*`) is used on the Neuron path; the remaining 56 B are inherited `NCCL_STEPS` slots, unused here (MED).

> **CORRECTION (NCCOM-PROXY-1) —** an earlier reading treated a drained op as returning to `state->pool` immediately, mirroring upstream NCCL's per-op free. The decompiled `recycleOp` (inlined in `ncclProxyProgressOps` `nccom:0x40a80`) instead re-files the op into `basic_block_args_map[op->basic_block_id]` and re-arms it (`freeRequest` → reset cursors → `allocRequest` → `state = 1`). Ops only reach `state->pool` on stream teardown (`neuronStopNetworkProxy` `nccom:0x487d0`) or comm destroy. The per-op lifetime is **block-scoped**, not tick-scoped.

---

## 5. The Device-Trigger Handshake

### Purpose

The host progress loop and the on-device collective program are interlocked by a host-visible memory handshake, so the proxy does not start posting network ops before the device has reached the network stage of the collective. Each task holds up to four `start_proxy_task_host_mem_addrs[]` (one per stream); the **device** writes these words when its program reaches the net stage, and the network task gates its transition out of `WAITING_DEVICE_TRIGGER` on them. This is the libnrt-side complement of libnccom's `nec_inc_semaphore` completion edge: the device signals *start* via host-mem words read here, and the proxy signals *completion* back via `nec_inc_semaphore` from inside `netNeuron{Send,Recv}Proxy`.

### Algorithm — the trigger gate

```c
// can_start_proxy_task — nrt:0x1ce7d0  [HIGH: volatile read + -1 compare read]
bool enc_proxy_task::can_start_proxy_task():
    for (i = 0; i < this->streams_n; i++):              // +0x50 valid trigger count
        volatile uint32 *trig = this->start_proxy_task_host_mem_addrs[i];   // +0x30 + 8*i
        if (*trig == 0xFFFFFFFF)                         // -1 sentinel = device has NOT triggered
            return false;
    return true;                                        // every stream's word written → go

// clear_start_proxy_task_flag — nrt:0x1ce810  [HIGH]
void enc_proxy_task::clear_start_proxy_task_flag():
    for (i = 0; i < this->streams_n; i++)
        *(this->start_proxy_task_host_mem_addrs[i]) = 0;   // consume the trigger (zero it)
```

The network task spins this only when it entered `WAITING_DEVICE_TRIGGER` (set by `init_network_proxy` when the context's device-wait flags are set); each `step()` re-checks `can_start_proxy_task`, and on success consumes the trigger (`clear_start_proxy_task_flag`) and advances to `PROGRESSING`. The barrier task uses the same gate at its INIT (with a `usleep` poll). The sentinel is `0xFFFFFFFF` written by the device-side init; a value other than `-1` means "this stream's collective has reached the net stage."

### Considerations

The handshake is what keeps the two binaries' *clocks* aligned without a shared lock. libnrt's worker can spin freely on `can_start_proxy_task` (it is a plain volatile load, no syscall) until the device-side program flips the host-mem word; only then does it begin calling across the seam into libnccom. The reverse direction — telling the device a network op *completed* — never appears in libnrt: it is issued by libnccom from inside the transport callbacks (`nec_inc_semaphore`, an libnccom→libnrt reverse import), so the two completion directions are split across the seam exactly as the two halves of the loop are. A reimplementer must wire both: a host-mem trigger word per stream (device→host start) and a `nec_inc_semaphore` device increment per net-op/exec completion (host→device done). Missing the trigger gate races the network ahead of the device; missing the semaphore leaves the device waiting forever.

---

## Cross-References

- [Overview: the NCCL Fork (2.31.24+nrt2.0)](overview.md) — *the proxy is the primitive*; the 8-stage host-orchestration pipeline whose stages 7–8 this page details
- [Communicator Init and Bootstrap](comm-init.md) — the dlopen/dlsym load that populates the `.bss` neuron-slot table this page's seam trampolines call through
- [Send / Recv Primitives and Protocols](send-recv-prims.md) — `netNeuron{Send,Recv}Proxy`, the `requestTracker` 128-rings, the RDMA-write-credit protocol that runs *inside* `op->progress()`
- [Inter-Node Transport: EFA / libfabric](transport-efa.md) — the `ncclNet->{isend,irecv,iread,iflush,test}` plugin calls the per-op state machine drives
- [Bananaphone IPC and the Proxy Driver](../collectives/proxy-driver.md) — the libnrt-side `enc_proxy_queue` / `g_shared_proxy_queues` / `enc_fifo_set` producer in its on-device-collectives context
- [The libnrt ↔ libnccom ABI](abi.md) — the 16 `nec_*` reverse callbacks (incl. `nec_inc_semaphore`) and the dlopen slot-table mechanics
- [back to index](../index.md)
