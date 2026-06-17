# Bananaphone IPC and the Proxy Driver

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present, and the source TU is `/opt/workspace/KaenaRuntime/enc/proxy_queue.cc`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Where a symbol belongs to the peer collective library it is tagged `nccom:0x…` and pins to `libnccom.so.2.31.24` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`). Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and disasm-anchored)** — every struct size/offset and both run-state enums are verbatim from the binary's own DWARF (`structures.json` / `enums.json`); the `process()` drain→step→recycle loop, the `step()` 5-state jump table (`movsxd`/`jmp rdx` @`0x1d0668`, base `0x858260`), the three seam call sites, and the device-trigger `-1` compare are read directly from the disassembly. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

A Neuron collective's network phase has no warp kernel and no self-driving thread inside the collective library: libnccom's proxy advances one tick only when something calls `neuronNetworkProxyProgress`, and otherwise does nothing ([proxy-engine](../nccom/proxy-engine.md) — *the proxy is the primitive*). **This page documents the something.** On the libnrt side the driver is one persistent worker thread — named `"proxy-<tid>"` — that drains a queue of proxy *tasks*, runs each task's virtual `step()` once per loop iteration, and publishes a status object a separate consumer thread polls. The network task's `step()` is a small jump-table state machine whose states map one-to-one onto the three seam calls into libnccom (`ncclStartNetworkProxy` / `ncclNetworkProxyProgress` / `ncclStopNetworkProxy`). The whole thing is gated by a host-visible **device-trigger handshake** so the proxy never posts network ops ahead of the on-device program. This is the producer/driver half of the seam whose consumer half ([proxy-engine](../nccom/proxy-engine.md) §4) is the libnccom op-FIFO.

The driver is **shared and refcounted**, which is the structural fact that shapes the lifecycle. There is not one worker per comm; there is one `enc_proxy_queue` (296 B) — and one thread — per **shared-proxy group**, keyed by `gid` in a process-global `std::map` (`g_shared_proxy_queues` @`0xc91320`) under one lock. Many global comms that fall in the same group (grouped by `NEURON_RT_RANKS_PER_NETWORK_PROXY`) bump a refcount and reuse the existing queue and thread; the thread's lifetime is the group's, not any comm's. The reference frame is upstream NVIDIA NCCL's `ncclProxyService` thread, with one move: NCCL's internal per-comm proxy thread has been **lifted out of the collective library and re-homed here**, and then *generalized* — it drives a polymorphic task list (a network task that bridges to libnccom plus a host-side barrier task), not just a raw op-FIFO.

The page is organized as five `##` units: the shared queue registry and worker lifecycle (`g_shared_proxy_queues`, `start`/`worker_thread`, refcounting); the producer/consumer plumbing (`enc_enq_proxy_tasks`, `append_task`, the status-poll API) and the two struct families; the `process()` driver loop (drain → step → recycle); the network task `step()` jump-table state machine (the producing side of the seam); and the device-trigger handshake. The two struct families — `enc_proxy_queue` and the `enc_proxy_task` hierarchy — are tabled up front from DWARF. The on-wire FIFO records (`net_ops_info`/`net_src_addr`/`net_dest_addr`) that the network task hands across the seam are owned by [enc-primitives](enc-primitives.md) and [send-recv-prims](../nccom/send-recv-prims.md) and only referenced here. The **Bananaphone** IPC — a memfd ring + futex bus used for the host local-leader rendezvous during global-comm setup — is the unrelated subsystem that shares the `proxy_queue` lifecycle owners (`enc_setup_global_comm_internal` / `enc_free_global_comm`); it is sketched in §1 and detailed elsewhere.

For reimplementation, the contract is:

- **One driver thread per shared-proxy group, refcounted.** Key a process-global map by `gid`; on the first comm in a group `new` the queue + spawn `"proxy-<tid>"`, on later comms `refcnt++` and reuse; destroy the queue (joining the worker) only when `refcnt` returns to 0. Put the thread on the wrong side and the collective never makes progress; key it per-comm and you spawn N threads where the binary spawns one.
- **The polymorphic task model.** Two task kinds (`enc_network_proxy_task`, `enc_barrier_proxy_task`) subclass an abstract `enc_proxy_task` with a pure-virtual `step()`. The worker calls `step()` through vtable slot 0; the network task crosses the seam, the barrier task is pure host-side.
- **The two nested state machines.** The *queue-level* loop (`process`: drain enqueued tasks into a per-rank active map → `step()` each → recycle DONE → `cv.wait` on quiescence or `sched_yield`/busy-spin on all-idle) and the *task-level* `proxy_task_run_state_t` machine (INIT → WAITING_DEVICE_TRIGGER → PROGRESSING ⇄ IDLE → DONE) dispatched through a 5-entry jump table.
- **The device-trigger gate.** A per-stream host-mem word the device writes when its program reaches the net stage; `can_start_proxy_task` spins until none reads `0xFFFFFFFF`, then `clear_start_proxy_task_flag` consumes it. The producer fills the pointers; the device writes the value.

| | |
|---|---|
| **Driver thread** | `enc_proxy_queue::process` `0x1cf7c0` — `"proxy-<tid>"`, one per shared-proxy group |
| **Queue registry** | `g_shared_proxy_queues` `0xc91320` (.bss, 0x30) — `std::map<int gid, shared_network_proxy_queue>`, **refcounted** |
| **Registry lock** | `g_shared_proxy_queues_lock` `0xc912e0` (.bss, 0x28 `pthread_mutex`) |
| **Queue object** | `enc_proxy_queue` — **296 B** (`0x128`); deque@+0, active_tasks@+80, cv@+128, mtx@+176, state_queue@+216, tid@+280, stop@+288 |
| **Producer** | `enc_enq_proxy_tasks` `0x105560` → `append_task` `0x1cf3c0` (cv-notify worker) |
| **Network task step** | `enc_network_proxy_task::step` `0x1d0620` (vtable `0xbf6b18` slot0); jump-table `0x858260` |
| **The seam call** | `proxy_progress` `0x1d03f0` → `ncclNetworkProxyProgress` `0x1c1aa0` (trampoline) → `neuronNetworkProxyProgress` `nccom:0x48930` |
| **Status poll API** | `enc_proxy_check_status` `0xfcf90` → `check_proxy_task_status` `0x1cf1c0` (reads `state_queue`) |
| **Task base / kinds** | `enc_proxy_task` (88 B, abstract) → `enc_network_proxy_task` (104 B) / `enc_barrier_proxy_task` (128 B) |
| **Task state var** | `proxy_task_run_state_t` @ `enc_proxy_task_state+20`: INIT=0, PROGRESSING=1, WAITING_DEVICE_TRIGGER=2, IDLE=3, DONE=4 |
| **Task kind discriminator** | `proxy_task_state_type_t`: UNKNOWN=0, NETWORK=1, BARRIER=2 |
| **Device-trigger gate** | `can_start_proxy_task` `0x1ce7d0` — spins until no `start_proxy_task_host_mem_addrs[i] == 0xFFFFFFFF` (`-1`) |
| **Lifecycle owners** | `enc_setup_global_comm_internal` `0x10b050` (create/share) · `enc_free_global_comm` `0x10a650` (release) |

> **QUIRK — the thread you must spawn is not where the op-FIFO lives.** A reimplementer porting NCCL looks for the proxy thread inside the collective library. It is not there: libnccom is passive ([proxy-engine](../nccom/proxy-engine.md)). The thread lives here, in libnrt's `enc_proxy_queue::process`, and it is **shared across comms** — one `pthread_create` per `gid`, refcounted in `g_shared_proxy_queues`, not one per communicator. The worker drives a *task list*, of which the libnccom-bridging network task is only one kind; the other (the barrier task) never crosses the seam at all. Spawn one thread per comm, or assume the thread only drives network ops, and you have rebuilt a different machine.

---

## 1. The Shared Queue Registry and Worker Lifecycle

### Purpose

The driver is not per-comm; it is per **shared-proxy group**. `enc_setup_global_comm_internal` (`0x10b050`) keys a process-global `std::map` (`g_shared_proxy_queues` @`0xc91320`) by `gid` — the group id derived from `NEURON_RT_RANKS_PER_NETWORK_PROXY` (`nrt_config.ranks_per_network_proxy`). The first comm in a group constructs an `enc_proxy_queue`, sets its CPU affinity, and spawns the worker; every later comm in the same group bumps the node's refcount and reuses the existing queue and thread. The queue and its worker outlive any single comm and die only when the last comm in the group releases it. This unit fixes that lifecycle, because getting the refcount discipline wrong is the difference between one driver thread and N.

### Entry Point

```text
enc_setup_global_comm_internal 0x10b050          ── CREATE / SHARE the queue
  under g_shared_proxy_queues_lock (0xc912e0):
    key = gid                                     ── = nec_dev grouped by NEURON_RT_RANKS_PER_NETWORK_PROXY
    g_shared_proxy_queues.find(gid)
      HIT  : node.refcnt++  (node+0x30)           ── reuse existing queue + worker thread
      MISS : node = map.emplace(gid)
             queue = new(nothrow) enc_proxy_queue ── ctor 0x13b9e0
             ncclSetAffinity + sched_setaffinity  ── place the worker (low-latency CPU mask)
             queue->start()                       ── 0x1ce4d0
               └─ pthread_create(&queue->tid, NULL, worker_thread, queue)
                    worker_thread 0x1d0120         ── gettid; setname "proxy-<tid>"; → process()
    comm[+0x89ca8] = queue                        ── stash the queue ptr on the comm
  unlock

enc_free_global_comm 0x10a650                     ── RELEASE the queue
  under lock: node.refcnt-- ; if (refcnt == 0):
    ~enc_proxy_queue  (0x1ceb50)                  ── stop=1; cv.notify_all; pthread_join
    map.erase(gid)
  else keep the shared queue alive for the other comms
```

The Bananaphone IPC shares these two owners: `enc_setup_global_comm_internal` also calls `bp_create`/`bp_connect` for the host local-leader rendezvous, and `enc_free_global_comm` calls `bp_hangup`/`bp_cleanup`. The setup nlog tag is the verbatim string `"enc_setup_global_comm_bananaphones"` (@`0x7e2e60`); "bananaphone" is the fork's nickname for the memfd-backed host bootstrap bus (a single-producer ring + futex + pidfd liveness, source `enc/bananaphone.c`) — the host all-gather/barrier that runs *before* any proxy task exists. It is structurally unrelated to the proxy driver and is detailed in the bananaphone IPC cell; the only coupling is that both are created and torn down by the same global-comm lifecycle functions.

### Algorithm — share-or-create under the lock

```c
// enc_setup_global_comm_internal (queue path) — 0x10b050  [HIGH: refcnt++ @0x10be72, share/create read]
enc_proxy_queue *get_or_create_shared_queue(int gid, ncclComm *comm):
    lock(g_shared_proxy_queues_lock);                  // 0xc912e0
    node = g_shared_proxy_queues.find(gid);            // map<int gid, shared_network_proxy_queue>
    if (node != end):
        node->value.refcnt++;                          // node+0x30 — SHARE: reuse queue + thread
        queue = node->value.queue;                     // node+0x28
    else:
        queue = (enc_proxy_queue*) operator new(296, nothrow);   // 0x128
        construct enc_proxy_queue(queue);              // ctor 0x13b9e0: deque/list sentinels, cv, stop=0, tid=0
        ncclSetAffinity(...); sched_setaffinity(...);  // place the worker on the low-latency CPU mask
        if (queue->start() != NRT_SUCCESS):            // 0x1ce4d0 — spawn "proxy-<tid>"
            destroy + operator delete(queue); unlock; return NULL;
        node = g_shared_proxy_queues.emplace(gid, {queue, refcnt=1});
    comm[0x89ca8] = queue;                             // the comm now points at its shared queue
    unlock(g_shared_proxy_queues_lock);
    return queue;

// enc_proxy_queue::start — 0x1ce4d0  [HIGH]
NRT_STATUS enc_proxy_queue::start():
    if (pthread_create(&this->tid /*+280*/, NULL, worker_thread, this) != 0):  // worker_thread 0x1d0120
        nlog("Failed to create worker thread: %d", errno);   // str @0x7f7cf8
        return NRT_RESOURCE;                                 // (4)
    return NRT_SUCCESS;

// enc_proxy_queue::worker_thread — 0x1d0120  [HIGH: "proxy-%d" @0x8437b6]
void *enc_proxy_queue::worker_thread(void *arg):              // [static]
    enc_proxy_queue *q = arg;
    int tid = syscall(SYS_gettid /*0xba*/);
    char name[16]; snprintf(name, 16, "proxy-%d", tid);
    if (pthread_setname_np(pthread_self(), name) != 0):
        nlog("Cannot set thread name: %d", errno);           // str @0x8437bf
    q->process();                                            // 0x1cf7c0 — THE DRIVER LOOP
    return NULL;

// ~enc_proxy_queue — 0x1ceb50  [HIGH: stop/notify/join read]
enc_proxy_queue::~enc_proxy_queue():
    this->stop = true;                                       // +288
    this->cv.notify_all();                                   // +128 — wake a sleeping worker so it sees stop
    if (this->tid != 0):
        pthread_join(this->tid, NULL);                       // join site 0x1cef10
    /* teardown active_tasks Rb-tree, cv, deque, state_queue */
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `enc_setup_global_comm_internal` | `0x10b050` | create/share queue under lock; `refcnt++` on hit (`@0x10be72`); store on `comm[+0x89ca8]` | HIGH |
| `enc_free_global_comm` | `0x10a650` | `refcnt--`; destroy queue (joins worker) at 0; erase map node | HIGH |
| `enc_proxy_queue::enc_proxy_queue` | `0x13b9e0` | ctor: deque/list sentinels, `condition_variable`, `stop=0` (+288), `tid=0` (+280) | HIGH |
| `enc_proxy_queue::start` | `0x1ce4d0` | `pthread_create(&tid, worker_thread)`; `NRT_RESOURCE` on fail | HIGH |
| `enc_proxy_queue::worker_thread` | `0x1d0120` | `gettid`; `pthread_setname_np "proxy-<tid>"`; → `process()` | HIGH |
| `enc_proxy_queue::~enc_proxy_queue` | `0x1ceb50` | `stop=1`; `cv.notify_all`; `pthread_join` (`@0x1cef10`); teardown | HIGH |

### Considerations

The two-step share check is the subtle correctness point: the refcount lives on the **map node's value** (`shared_network_proxy_queue.refcnt` at node+0x30), not on the queue object, so a comm that shares a queue never touches the queue's internal state — it only adjusts the registry node. The affinity calls (`ncclSetAffinity` + `sched_setaffinity`) run only on the MISS path, i.e. once per group, because the worker is spawned once per group; a reimplementer who re-applies affinity per comm is operating on a thread that already exists and is already placed. The destructor's ordering matters for a clean shutdown: `stop` is set *before* `cv.notify_all`, so a worker blocked in `cv.wait` re-checks `stop` immediately on wake and exits the loop, after which `pthread_join` returns without racing the worker's last iteration. The `tid != 0` guard around the join means a queue whose `start()` failed (no thread) is still safely destructible.

---

## 2. Producer, Status Poll, and the Two Struct Families

### Purpose

Two channels connect the worker to the rest of the runtime. The **producer** path (exec/prepare thread) builds tasks and hands them to the queue; the **consumer** path (exec/host-cc thread) polls a published status object. They share the queue object but use *different* internal lists guarded by *different* mutexes, so the worker can advance tasks while a consumer reads status without lock contention. This unit fixes both channels and tables the two struct families — the queue and the task hierarchy — from DWARF, because every offset the later algorithm units cite resolves here.

### The structs (DWARF — authoritative)

`enc_proxy_queue` (296 B / `0x128`) — the shared queue + driver state. The split is deliberate: `mtx` (+176) guards the work lists (`queue` + `active_tasks`); `state_mtx` (+240) guards the published-status list (`state_queue`). The worker holds `mtx`; the consumer holds `state_mtx`; they meet only on the 24-byte status object.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `queue` | +0 | `std::queue<shared_ptr<enc_proxy_task>>` (deque, 80 B) | PENDING in-list; `append_task` pushes, `process` drains | HIGH |
| `active_tasks` | +80 | `std::map<int, std::list<shared_ptr<enc_proxy_task>>>` (48 B) | per-**rank** running task lists (key = `get_rank_id`) | HIGH |
| `cv` | +128 | `std::condition_variable` (48 B) | worker wakeup; `append_task` notifies, `process` waits | HIGH |
| `mtx` | +176 | `std::mutex` (40 B) | guards `queue` + `active_tasks` | HIGH |
| `state_queue` | +216 | `std::list<shared_ptr<enc_proxy_task_state>>` (24 B) | published status ring (consumer view) | HIGH |
| `state_mtx` | +240 | `std::mutex` (40 B) | guards `state_queue` | HIGH |
| `tid` | +280 | `pthread_t` | worker thread id; `0` if none spawned | HIGH |
| `stop` | +288 | `bool` | shutdown flag; set by `~enc_proxy_queue` | HIGH |

`enc_proxy_task` (88 B / `0x58`, abstract base; pure-virtual `step()` at vtable `0xbf6980` slot 0).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `_vptr` | +0 | vtable ptr | net = `0xbf6b18`, barrier = `0xbf6b30` | HIGH |
| `wq` | +8 | `enc_proxy_queue*` | owning queue (back-pointer) | HIGH |
| `ctx` | +16 | `enc_context*` | the per-NC engine context this task drives | HIGH |
| `exec_id` | +24 | `uint64_t` | exec instance id (flow control) | HIGH |
| `proxy_state` | +32 | `shared_ptr<enc_proxy_task_state>` (16 B) | the published status object (ptr@+32, ctrl@+40) | HIGH |
| `start_proxy_task_host_mem_addrs` | +48 | `volatile uint32_t*[4]` | **device-trigger** host-mem words, one per stream | HIGH |
| `streams_n` | +80 | `int` | count of valid trigger addrs (loop bound for the gate) | HIGH |

`enc_network_proxy_task` (104 B) and `enc_barrier_proxy_task` (128 B) — the two concrete kinds. Both embed `enc_proxy_task` at +0.

| Struct | Extra field | Offset | Type | Role | Confidence |
|---|---|---|---|---|---|
| `enc_network_proxy_task` | `num_net_ops` | +84 | `int[4]` | per-stream net-op counts (from FIFO vector sizes) | HIGH |
| `enc_barrier_proxy_task` | `exec_id` | +88 | `uint64_t` | second exec id (barrier scope) | HIGH |
| `enc_barrier_proxy_task` | `barrier_done_semaphore_inc_addrs` | +96 | `volatile uint32_t*[4]` | leader SPS semaphore words the barrier increments | HIGH |

`enc_proxy_task_state` (24 B) — the status object the worker **publishes** and the consumer **reads**. This is the only shared surface between the two threads.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `progress_status` | +0 | `NRT_STATUS` | worker writes the last `step()` result; consumer reads | HIGH |
| `enc_ctx` | +8 | `enc_context*` | match key for `check_proxy_task_status` | HIGH |
| `task_type` | +16 | `proxy_task_state_type_t` | NETWORK(1) / BARRIER(2) — second match key | HIGH |
| `run_state` | +20 | `proxy_task_run_state_t` | **the per-task state-machine variable** (`0x14`) | HIGH |

`shared_network_proxy_queue` (16 B) — the map's value type: `queue` (`enc_proxy_queue*`) @+0, `refcnt` (`uint32_t`) @+8. `enc_fifo_set` (72 B) — the per-basic-block FIFO triple `init_network_proxy` builds (`src_addr_fifo`@+0 / `dest_addr_fifo`@+24 / `net_ops_info_fifo`@+48), whose `(base,count)` vectors are the arrays handed across the seam (owned by [enc-primitives](enc-primitives.md)).

### The two enums (DWARF — exact)

```text
proxy_task_state_type_t {            // task kind / discriminator (enc_proxy_task_state+16)
    PROXY_TASK_UNKNOWN = 0,
    PROXY_TASK_NETWORK = 1,          // enc_check_proxy_network_task_status arg
    PROXY_TASK_BARRIER = 2 }         // enc_check_proxy_barrier_task_status arg

proxy_task_run_state_t {             // per-task state machine (enc_proxy_task_state+20)
    PROXY_TASK_STATE_INIT                   = 0,
    PROXY_TASK_STATE_PROGRESSING            = 1,
    PROXY_TASK_STATE_WAITING_DEVICE_TRIGGER = 2,
    PROXY_TASK_STATE_IDLE                   = 3,
    PROXY_TASK_STATE_DONE                   = 4 }
```

### Algorithm — produce and poll

```c
// enc_enq_proxy_tasks — 0x105560  [HIGH: two `new` + two append_task read]  *** PRODUCER ***
// callers: nrt_cc_prepare (+0x250) 0x7f860 ; kbl_exec_cc_enq_proxy_tasks_internal (+0x8b) 0x306edb
void enc_enq_proxy_tasks(encd_context *dctx, uint64_t exec_id):
    comm  = encd_get_global_comm();
    queue = comm[0x89ca8];                                  // the shared enc_proxy_queue
    // one network task + one barrier task per CC op
    auto net = make_shared<enc_network_proxy_task>(ctx, exec_id);   // ctor 0x1cf140, vtbl 0xbf6b18
    queue->append_task(net);
    auto bar = make_shared<enc_barrier_proxy_task>(ctx, exec_id);   // ctor 0x1cf170, vtbl 0xbf6b30
    queue->append_task(bar);

// enc_proxy_queue::append_task — 0x1cf3c0  [HIGH: state_queue hook + deque push + notify read]
void enc_proxy_queue::append_task(shared_ptr<enc_proxy_task> task):
    {   lock(this->state_mtx);                              // +240
        this->state_queue.push_back(task->proxy_state); }   // +216 — publish status BEFORE running
    {   lock(this->mtx);                                    // +176
        this->queue.push_back(task); }                      // +0 — enqueue for the worker
    this->cv.notify_all();                                  // +128 — wake "proxy-<tid>"

// enc_proxy_check_status — 0xfcf90  [HIGH]  *** CONSUMER bridge ***
// callers: exec_request_progress_one_step 0x264c7a/0x264d6b ; hostcc_wait_completion 0x267304/0x267318
NRT_STATUS enc_proxy_check_status(const encd_context *dctx, proxy_task_state_type type,
                                  bool *done, NRT_STATUS *status):
    comm  = encd_get_global_comm();
    queue = comm[0x89ca8];
    return queue->check_proxy_task_status(ctx, type, done, status);   // 0x1cf1c0

// enc_proxy_queue::check_proxy_task_status — 0x1cf1c0  [HIGH]
NRT_STATUS enc_proxy_queue::check_proxy_task_status(enc_context *ctx, proxy_task_state_type type,
                                                    bool *done, NRT_STATUS *status):
    lock(this->state_mtx);                                  // +240
    for (state in this->state_queue):                       // +216
        if (state->enc_ctx == ctx && state->task_type == type):   // +8 / +16 match
            *status = state->progress_status;               // +0 — what the worker last published
            *done   = (state->run_state == PROXY_TASK_STATE_DONE);   // +20 == 4
            if (*done) this->state_queue.erase(state);       // unhook + delete the finished node
            return NRT_SUCCESS;
    return NRT_FAILURE;                                     // no matching task
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `enc_enq_proxy_tasks` | `0x105560` | PRODUCER: `new` network + barrier task, `append_task` each | HIGH |
| `enc_proxy_queue::append_task` | `0x1cf3c0` | publish status to `state_queue`; push task to `queue`; `cv.notify_all` | HIGH |
| `enc_proxy_check_status` | `0xfcf90` | bridge `encd_context` → shared queue → `check_proxy_task_status` | HIGH |
| `enc_proxy_queue::check_proxy_task_status` | `0x1cf1c0` | match ctx+type in `state_queue`; return status/done; erase on DONE | HIGH |
| `enc_check_proxy_network_task_status` | `0xfd350` | thin wrapper → `enc_proxy_check_status(type=NETWORK=1)` | HIGH |
| `enc_check_proxy_barrier_task_status` | `0xfd380` | thin wrapper → `enc_proxy_check_status(type=BARRIER=2)` | HIGH |
| `enc_consume_proxy_task_status` | `0xfd0d0` | BLOCKING consumer: poll + `usleep` + timeout WARN until done | HIGH |
| `enc_proxy_task::get_rank_id` | `0x1ce540` | `ctx[+0x10][+0x670d30 glb_comm][+0][+8]` → rank (active_tasks key) | HIGH |

### Considerations

The producer publishes status to `state_queue` **before** pushing the task to the work `queue`, and under a *different* lock — so the consumer can observe a task's initial status the instant `append_task` returns, even before the worker has run a single `step()`. The two-list/two-lock split (`mtx` for `queue`+`active_tasks`, `state_mtx` for `state_queue`) is what lets the worker hold `mtx` for the entire drain/step pass while a consumer poll holds only `state_mtx`; the 24-byte `enc_proxy_task_state`, shared via `shared_ptr` between the task and the `state_queue` node, is the only memory both threads touch. The blocking consumer (`enc_consume_proxy_task_status` `0xfd0d0`) is the host-cc completion path: it loops `enc_proxy_check_status` + `usleep`, and on a stall emits the verbatim watchdog `"[nec_dev %u gid %u] Timeout exceeded: Waiting on proxy task type %d: %u sec"` (@`0x7e0550`), surfacing `"[nec_dev %u gid %u] Proxy failed to execute task, status = %d"` (@`0x7e0510`) on error. Note that `get_rank_id` walks `ctx → glb_comm (ctx+0x670d30) → [+0] → [+8]`; the displacement is exact but the full `encd_context` layout is owned by [engine-core](engine-core.md) (LOW for the intermediate types, HIGH for the chain).

---

## 3. The Driver Loop — `process()`

### Purpose

`enc_proxy_queue::process` (`0x1cf7c0`) is the body of the `"proxy-<tid>"` thread and the outer state machine: drain newly enqueued tasks into the per-rank active map, `step()` every active task once, recycle the tasks that report DONE, and then either block (genuine quiescence) or spin/yield (all tasks idle, waiting on net/device). It is the analogue of NCCL's `ncclProxyService` `while` loop, generalized to a polymorphic task list and a per-rank bucketing.

### Entry Point

```text
worker_thread 0x1d0120  ── "proxy-<tid>"
  └─ enc_proxy_queue::process 0x1cf7c0   ── runs until this->stop (loop body below)
       per task: task->step()  == call qword ptr [rax]  (vtbl slot0 @0x1cfb09)
         enc_network_proxy_task::step  0x1d0620   (→ seam, §4)
         enc_barrier_proxy_task::step  0x1ce830   (host barrier, no seam)
```

### Algorithm — drain → step → recycle → wait/yield

```c
// enc_proxy_queue::process — 0x1cf7c0  [HIGH: drain/step/recycle + cv.wait@0x1cffee / sched_yield@0x1cff93]
void enc_proxy_queue::process():                         // worker thread body
    while (!this->stop):                                 // +288
        lock(this->mtx);                                 // +176 — guards queue + active_tasks

        // (1) DRAIN: move newly enqueued tasks into the per-rank active map
        while (!this->queue.empty()):                    // +0 deque, filled by append_task
            task = this->queue.pop_front();
            rank = task->get_rank_id();                  // 0x1ce540 — glb_comm rank
            this->active_tasks[rank].push_back(task);    // +80 map<int rank, list<task>>

        // (2) STEP: advance every active task exactly one tick
        idle_count = 0; active_count = 0;
        for (rank, list) in this->active_tasks:
            for task in list:
                active_count++;
                task->step();                            // VIRTUAL slot0 — call [rax] @0x1cfb09
                //   step() has already published proxy_state->{progress_status, run_state}
                rs = task->proxy_state->run_state;       // enc_proxy_task_state+20
                if (rs == PROXY_TASK_STATE_IDLE):  idle_count++;          // ==3
                if (rs == PROXY_TASK_STATE_DONE):                        // ==4
                    list.erase(task);                    // RECYCLE: unhook the finished task
            if (list.empty()): prune rank bucket;

        unlock(this->mtx);

        // (3) BLOCK or SPIN — there is no forward progress to make this instant
        if (this->active_tasks.empty() && this->queue.empty()):
            relock(this->mtx);
            this->cv.wait(unique_lock(this->mtx));       // +128 @0x1cffee — sleep until append_task notifies
        else if (idle_count == active_count):            // every active task idle (net/device pending)
            if (nrt_config.one_thread_per_core /*+0x9c*/):
                /* busy-spin: dedicated core → keep the seam call hot */ ;
            else:
                sched_yield();                           // @0x1cff93 — shared core → yield between ticks
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `enc_proxy_queue::process` | `0x1cf7c0` | the driver loop: drain → step → recycle DONE → wait/yield | HIGH |
| `enc_network_proxy_task::step` | `0x1d0620` | network task state machine (§4) — dispatched via slot0 | HIGH |
| `enc_barrier_proxy_task::step` | `0x1ce830` | host barrier task: `enc_barrier` send/recv + leader SPS sema | HIGH |
| `enc_barrier_proxy_task::inc_leader_sps_barrier_done_semas` | `0x1ce7b0` | leader-only increment of the 4 `barrier_done_semaphore_inc_addrs` | HIGH |

> **GOTCHA — IDLE is not terminal; only DONE recycles a task.** A task reporting `run_state == IDLE` (3) made no progress *this tick* — the network had nothing to complete — but it is still in flight. The loop keeps it in `active_tasks` and keeps calling `step()`; it only changes its *waiting strategy* (busy-spin on a dedicated core when `nrt_config.one_thread_per_core` @+0x9c is set, `sched_yield` otherwise). Only `DONE` (4) erases the task from the active list. A reimplementer who treats IDLE as completion drops in-flight collectives. And `cv.wait` is reached **only** when there is nothing active *and* nothing queued — real quiescence, not a single idle tick; reaching `cv.wait` while a task is still IDLE would deadlock the collective, because nothing but `append_task` notifies the cv.

### Considerations

The bucketing by rank (`active_tasks` is `map<int rank, list<task>>`, not a flat list) matters when a single shared queue drives multiple ranks' tasks — each rank's tasks advance in their own list, and the rank key comes from `get_rank_id`'s `glb_comm` walk. The barrier task is the sibling kind the worker also drives but which never crosses the seam: its `step()` (`0x1ce830`) runs the host-side execution barrier — `can_start_proxy_task` (usleep poll) + clear, `enc_barrier` send (`enc_barrier` @`0x12f660`), `inc_leader_sps_barrier_done_semas` (`0x1ce7b0`, leader-only SPS semaphore broadcast over the 4 `barrier_done_semaphore_inc_addrs`), `enc_barrier` recv → `run_state = DONE` — and logs `"[nec_dev %u] run enc_barrier_proxy_task for enc_context %p"` (@`0x7f7df8`). It is a pure host-side collective barrier; the seam-crossing logic is entirely in the network task (§4). The `one_thread_per_core` busy-spin vs `sched_yield` choice is the only latency/CPU trade-off in the loop: a dedicated core keeps the (cheap, syscall-free) `neuronNetworkProxyProgress` call hot at the cost of a busy CPU; a shared core yields between ticks.

---

## 4. The Producing Side of the Seam — Network Task `step()`

### Purpose

`enc_network_proxy_task::step()` (`0x1d0620`) is the libnrt object that owns the call into libnccom. The worker invokes it through vtable slot 0; its state variable is `proxy_task_run_state_t` at `proxy_state+0x14`, dispatched through a 5-entry jump table at `0x858260` (the `movsxd ...[rsi+rdx*4]; jmp rdx` at `0x1d0668`/`0x1d066f`). Each state maps onto a seam call: INIT issues `ncclStartNetworkProxy` per basic block (== seam B1), PROGRESSING/IDLE issues `ncclNetworkProxyProgress` (== B2, the hot seam), and a basic-block transition issues `ncclStopNetworkProxy` (== B3). This is the producing half of the seam whose consumer half is libnccom's op-FIFO loop ([proxy-engine](../nccom/proxy-engine.md) §4).

### Entry Point

```text
enc_network_proxy_task::step 0x1d0620   (vtable 0xbf6b18 slot0; jumptable 0x858260)
  INIT(0)                    @0x1d0728  → init_network_proxy 0x1ce5b0
                                           build_fifo_block_ids → per-bb ncclStartNetworkProxy (== B1)
  PROGRESSING(1) / IDLE(3)   @0x1d06b0  → proxy_progress 0x1d03f0
                                           ncclNetworkProxyProgress (== B2 SEAM) 0x1c1aa0
                                           [ ncclStopNetworkProxy on bb switch (== B3) 0x1c18e0 ]
  WAITING_DEVICE_TRIGGER(2)  @0x1d0700  → can_start_proxy_task? → clear flag → PROGRESSING(1)
  DONE(4)                    @0x1d0678  → nlog "Task shouldn't be polled after it reached a done state"
```

### Algorithm — the task state machine

```c
// enc_network_proxy_task::step — 0x1d0620  [HIGH: jumptable 0x858260 + seam call sites read from disasm]
NRT_STATUS enc_network_proxy_task::step():
    switch (this->proxy_state->run_state):                 // +0x14, jumptable @0x858260
      case PROXY_TASK_STATE_INIT:                          // 0  @0x1d0728
        if (ctx[+0x108] /* skip / early-completion NOP */): // (MED: owning struct not fully dumped)
            proxy_state->progress_status = NRT_SUCCESS;
            proxy_state->run_state = PROXY_TASK_STATE_DONE; // 4 — skip the whole net stage
            break;
        init_network_proxy();                              // 0x1ce5b0 — build FIFOs + per-bb B1
        if (ctx[+0x10c] && ctx[+0x380] /* device-wait policy */):
            proxy_state->run_state = PROXY_TASK_STATE_WAITING_DEVICE_TRIGGER;  // 2
        else:
            proxy_state->run_state = PROXY_TASK_STATE_PROGRESSING;            // 1
        break;

      case PROXY_TASK_STATE_PROGRESSING:                   // 1  @0x1d06b0
      case PROXY_TASK_STATE_IDLE:                          // 3  (re-entered each tick while in flight)
        bool idle = false, done = false;
        proxy_state->progress_status = proxy_progress(&idle, &done);   // 0x1d03f0 — THE SEAM CALL
        if (done)      proxy_state->run_state = PROXY_TASK_STATE_DONE;  // 4
        else if (idle) proxy_state->run_state = PROXY_TASK_STATE_IDLE;  // 3 (loop keeps ticking it)
        break;

      case PROXY_TASK_STATE_WAITING_DEVICE_TRIGGER:        // 2  @0x1d0700
        if (can_start_proxy_task()):                       // 0x1ce7d0 — every stream's host-mem != -1 ?
            clear_start_proxy_task_flag();                 // 0x1ce810 — consume the trigger (write 0)
            proxy_state->run_state = PROXY_TASK_STATE_PROGRESSING;       // 1
        break;                                             // else stay WAITING; worker re-ticks

      case PROXY_TASK_STATE_DONE:                          // 4  @0x1d0678
        nlog("Task shouldn't be polled after it reached a done state");  // str @0x7f81b8
        break;
    return proxy_state->progress_status;

// init_network_proxy — 0x1ce5b0  [HIGH: encd_get_global_comm@0x1ce601, build_fifo@0x1ce61b,
//                                       ncclStartNetworkProxy@0x1ce6d6, setup_first@0x1ce763]
void enc_network_proxy_task::init_network_proxy():
    comm = encd_get_global_comm();
    ctx->build_fifo_block_ids();                           // 0x124f20 — pack enc_fifo_set per basic block
    for (bb in basic_blocks):
        enc_fifo_set *fs = &bb_map[bb];                    // unordered_map<bb_id, enc_fifo_set>
        // hand each vector's (base,count) straight across the seam — B1
        if (ncclStartNetworkProxy(comm, stream, bb,
                fs->net_ops_info_fifo.data(), fs->net_ops_info_fifo.size(),   // ops  (== B1)
                fs->src_addr_fifo.data(),     fs->src_addr_fifo.size(),       // src
                fs->dest_addr_fifo.data(),    fs->dest_addr_fifo.size())      // dst
            != NRT_SUCCESS):
            nlog("[nec_dev %u] failed to start network proxy ret:%d", ...);   // str @0x7f7d88
    setup_first_basic_block_switch();                      // 0x1ce560

// proxy_progress — 0x1d03f0  [HIGH: ncclNetworkProxyProgress@0x1d04f4, ncclStopNetworkProxy@0x1d055d]
NRT_STATUS enc_network_proxy_task::proxy_progress(bool *idle, bool *done):
    comm = encd_get_global_comm();                         // @0x1d0446
    bool switched = false;
    check_basic_block_switch(cur_bb, &switched);           // 0x1d01c0 @0x1d04df — advance bb when old drains
    NRT_STATUS rc = ncclNetworkProxyProgress(comm, stream, bb, idle, done);  // == B2 / nccom:0x48930
    // logs "[nec_dev %u] ncclNetworkProxyProgress returns %d" @0x1d0523 on the error path
    if (switched)
        ncclStopNetworkProxy(comm, stream);                // == B3 / nccom:0x487d0 — drain old block
    return rc;
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `enc_network_proxy_task::step` | `0x1d0620` | task state machine (vtbl slot0); 5-state jump table `0x858260` | HIGH |
| `enc_network_proxy_task::init_network_proxy` | `0x1ce5b0` | build per-bb FIFOs + `ncclStartNetworkProxy` (B1) | HIGH |
| `enc_network_proxy_task::proxy_progress` | `0x1d03f0` | seam call `ncclNetworkProxyProgress` (B2) + `ncclStopNetworkProxy` (B3) | HIGH |
| `enc_network_proxy_task::check_basic_block_switch` | `0x1d01c0` | decide when to drain old bb / start new (`unordered_map<bb,enc_fifo_set>`) | HIGH |
| `enc_network_proxy_task::setup_first_basic_block_switch` | `0x1ce560` | arm the first basic block after init | HIGH |
| `ncclStartNetworkProxy` (trampoline) | `0x1c16f0` | → `neuronStartNetworkProxy` `nccom:0x483a0` (B1) | HIGH |
| `ncclNetworkProxyProgress` (trampoline) | `0x1c1aa0` | → `neuronNetworkProxyProgress` `nccom:0x48930` (B2) | HIGH |
| `ncclStopNetworkProxy` (trampoline) | `0x1c18e0` | → `neuronStopNetworkProxy` `nccom:0x487d0` (B3) | HIGH |

### Considerations

The five jump-table states are the full set — there is no sixth path; `INIT` either skips straight to `DONE` (the `ctx[+0x108]` early-completion NOP) or arms the FIFOs and chooses `WAITING_DEVICE_TRIGGER` vs `PROGRESSING` by the device-wait policy flags (`ctx[+0x10c] && ctx[+0x380]`). The `PROGRESSING` and `IDLE` cases share a body (the seam call) because IDLE is just PROGRESSING that made no progress last tick — the worker re-enters the same arm every iteration until `done`. The `ncclStart`/`Progress`/`Stop` calls are libnrt-internal trampolines that index a `.bss` neuron-slot table (`0xc967b8..0xc968d8`) populated by `ncclInit`'s `dlopen("libnccom.so")` — the same load mechanism as the rest of the libnccom ABI ([nccl-boundary](nccl-boundary.md)); libnrt never calls the `neuron*` exports by name. The three INIT-gate fields (`ctx+0x108`, `ctx+0x10c`, `ctx+0x380`) are per-context debug/early-completion policy bytes consistent with the RT's `dbg_proxy_early_completion` / `dbg_proxy_early_posting` config; their exact owning struct was not fully dumped (MED). The `num_net_ops[4]` field (network task +84) holds the per-stream op counts derived from the FIFO vector sizes during `init_network_proxy`; the per-element semantics were read in form but not exhaustively (MED).

---

## 5. The Device-Trigger Handshake

### Purpose

The host driver and the on-device collective program are interlocked by a host-visible memory handshake, so the proxy does not begin posting network ops before the device has reached the network stage of the collective. Each task holds up to four `start_proxy_task_host_mem_addrs[]` (one per stream, `enc_proxy_task+48`); the **device** writes these words when its program reaches the net stage, and the network task gates its transition out of `WAITING_DEVICE_TRIGGER` on them. This is the libnrt-side complement of libnccom's `nec_inc_semaphore` completion edge: the device signals *start* via the host-mem words read here, and the proxy signals *completion* back via `nec_inc_semaphore` from inside the transport callbacks ([send-recv-prims](../nccom/send-recv-prims.md)).

### Algorithm — the trigger gate

```c
// can_start_proxy_task — 0x1ce7d0  [HIGH: volatile read + `cmp eax,0FFFFFFFFh; jz` @0x1ce7ef read]
bool enc_proxy_task::can_start_proxy_task():
    for (i = 0; i < this->streams_n; i++):                 // +80 — valid trigger count
        volatile uint32_t *trig = this->start_proxy_task_host_mem_addrs[i];   // +48 + 8*i
        if (*trig == 0xFFFFFFFF)                            // -1 sentinel = device has NOT triggered
            return false;
    return true;                                           // every stream's word written → go

// clear_start_proxy_task_flag — 0x1ce810  [HIGH]
void enc_proxy_task::clear_start_proxy_task_flag():
    for (i = 0; i < this->streams_n; i++)
        *(this->start_proxy_task_host_mem_addrs[i]) = 0;   // consume the trigger (zero each word)
```

The network task spins this only when it entered `WAITING_DEVICE_TRIGGER` (set by `step()` after `init_network_proxy` when the context's device-wait flags are set); each subsequent `step()` re-checks `can_start_proxy_task`, and on success consumes the trigger (`clear_start_proxy_task_flag`) and advances to `PROGRESSING`. The barrier task uses the same gate at its INIT, but with a `usleep` poll inside `step()` rather than relying on the worker's re-tick. The sentinel is `0xFFFFFFFF` (`-1`) written at device-side init; any other value means "this stream's collective has reached the net stage."

> **QUIRK — the handshake is one volatile load, not a syscall, and that is what makes the busy-spin affordable.** `can_start_proxy_task` reads a host-visible word the device writes; there is no futex, no lock, no kernel transition. The worker can re-tick a `WAITING_DEVICE_TRIGGER` task as fast as the loop runs (busy-spin on a dedicated core via `nrt_config.one_thread_per_core`) until the device flips the word — and only then does it start calling across the seam. A reimplementer who gates the start on a blocking primitive throws away the whole point: the device-to-host start signal is meant to be observed on the next spin, not waited on. The reverse direction — telling the device a net op *completed* — never appears in libnrt; it is `nec_inc_semaphore`, issued by libnccom from inside the transport callbacks, so the two completion directions are split across the seam exactly as the two halves of the loop are.

### Considerations

The handshake keeps the two binaries' clocks aligned without a shared lock. Missing the trigger gate races the network ahead of the device program (the proxy posts `isend`/`irecv` against buffers the device has not yet produced); missing the reverse `nec_inc_semaphore` edge leaves the device waiting forever for a slice the network already delivered. A reimplementer must wire **both**: a host-mem trigger word per stream (device→host *start*, read by `can_start_proxy_task`, consumed by `clear_start_proxy_task_flag`) and a `nec_inc_semaphore` increment per net-op/exec completion (host→device *done*, issued from the libnccom transport callbacks). The `streams_n` bound (`enc_proxy_task+80`) is the number of valid trigger pointers — a task with fewer than four active streams reads only its valid prefix, so the trigger array is not always fully populated; iterating past `streams_n` would read uninitialized pointers.

---

## Cross-References

- [Proxy & Progress Engine (and the libnrt Bridge)](../nccom/proxy-engine.md) — the consuming half of the seam: libnccom's op-FIFO loop (`ncclProxyProgressOps`) this page's `proxy_progress` ticks; the four-entry seam ABI and the cross-binary loop diagram
- [Send / Recv Primitives and Protocols](../nccom/send-recv-prims.md) — `netNeuron{Send,Recv}Proxy`, the `requestTracker` 128-rings, and the `net_ops_info`/`net_src_addr`/`net_dest_addr` wire records that `init_network_proxy` packs into `enc_fifo_set`
- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — the libnrt producer that *builds* the `enc_fifo_set` FIFO triple (`net_ops_info`/`net_src_addr`/`net_dest_addr`) and the `proxy_end_operation` `EXEC_COMPLETE` terminator the seam asserts
- [Engine Core (enc_context and Accessors)](engine-core.md) — the `enc_context` / `encd_context` object the tasks carry (`ctx+0x670d30` glb_comm walked by `get_rank_id`; the INIT-gate policy bytes at `ctx+0x108`/`+0x10c`/`+0x380`)
- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](nccl-boundary.md) — the `dlopen`-populated `.bss` neuron-slot table the `ncclStart/Progress/Stop` trampolines call through, and the 16 `nec_*` reverse callbacks incl. `nec_inc_semaphore`
- [Inter-Node Transport: EFA / libfabric](../nccom/transport-efa.md) — the OFI/EFA verb plugin the op-FIFO drives once `proxy_progress` reaches it across the seam
- [back to index](../index.md)
