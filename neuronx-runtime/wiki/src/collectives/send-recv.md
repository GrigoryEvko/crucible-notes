# Async Send / Recv (Point-to-Point)

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present, and the source TUs are `/opt/workspace/KaenaRuntime/enc/async_sr/{async_sr.cc, async_sr_comm.cc, async_sr_context.cc, async_sr_ofi.cc, async_sr_request.cc, async_sr_service_thread.cc, kv_store.cc}`. `.text`/`.rodata`/`.data` VMA == file offset (libnrt is delta-zero: `.data` VMA `0xc07e00` == file offset `0xc07e00`, `readelf -SW`), so every `0x…` is both an analysis VMA and a file offset. Where a symbol crosses into the collective transport it is reached through libnrt-internal `ncclNet*` wrappers that `dlsym` into `libnccom`/`libnccom-net`; those are a boundary ([nccl-boundary](nccl-boundary.md), [transport-efa](../nccom/transport-efa.md)). Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF-, disasm-, and string-anchored)** — the request/connect/lnc enums and the three struct layouts are verbatim from the binary's DWARF; the post→progress→complete chain, the 8-state connect SM with its numeric `_InterlockedExchange` literals, the `status_to_string` jump table (`0x8578a4`), the two KV-callback copy targets (`+0x2500`/`+0x2580`), and the service-thread queue mechanics were read from the decompiled hot bodies (`enc_async_sr_post_op @0xec0c0`, `async_sr_service_thread_fn @0xef700`, `async_sr_progress_connect_comm @0xee7e0`, `kv_store_make_progress @0xf2730`) cross-checked against `objdump -d`/`nm -C`. The two `NEURON_RT_ASYNC_SENDRECV_*` env gates and every enum-name string were re-read from the binary. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

The async point-to-point send/recv path is the one collective edge in libnrt that does **not** compile into an on-device program. Where an all-reduce or all-gather lowers to a `cc_op_entry` stream the NeuronCore sync core executes ([overview](overview.md)), an async `SEND`/`RECV` is a **host-driven OFI rendezvous**: a tensor is registered as an OFI memory region and shipped over libfabric (via the `dlsym`'d `ncclNet*` plugin) with no hardware semaphore and no device program in the loop. Completion is detected by **polling** the OFI request handle (`ncclNetTest`), never by a semaphore wait. The subsystem is feature-gated behind `NEURON_RT_ASYNC_SENDRECV_EXPERIMENTAL_ENABLED=1` and a mandatory bootstrap port `NEURON_RT_ASYNC_SENDRECV_BOOTSTRAP_PORT`; absent either, `enc_async_sr_init @0xeade0` refuses to bring the lnc up.

The engine is a **single-producer / single-consumer pipeline, one per host-LNC** (`0..127`). Application threads that call `nrt_async_sendrecv_*` are producers; one dedicated service thread per LNC (`async_sr_service_thread_fn @0xef700`, spawned at init, guarded by `async_sr_init_mutex[lnc]`) is the sole consumer that runs the connect/accept rendezvous state machine and lazily registers OFI memory regions. Three bounded queues plus two atomics carry every hand-off: a 128-slot connect-task ring (context `+0x8`), a 128-slot stashed-request ring per comm (comm `+0x2068`), and a 128-entry request freelist per comm (comm `+0x1c38`), with a work counter `task_n` (context `+0x520`) and a condition variable `task_cv` (`+0x4f0`) that wakes the consumer. A request walks a four-state machine `FREE → STASHED → OFI_POSTED → (recycled to FREE)`, with an `ERROR` sink; a comm walks an eight-state connect machine whose SEND and RECV halves are asymmetric (SEND `0→2→4→6`; RECV `0→1→5→3→4→6`). The rendezvous that matches a send to its recv is an embedded **non-blocking TCP key-value store** (`kv_store_*`) over which the two sides exchange an OFI net handle keyed by `(send_lnc, recv_lnc, hostname)`.

This page documents the parts a reimplementer must rebuild: §1 the request record and its four-state machine; §2 the post / progress / complete chain (`enc_async_sr_post_op`, the service thread, `enc_async_sr_test_request`) as annotated pseudocode; §3 the eight-state connect/accept machine (`async_sr_progress_connect_comm`) as a diagram plus pseudocode; §4 the service-thread model and the KV-store rendezvous client (`kv_store_make_progress`); and §5 the thin OFI transport leaf (the `async_sr_ofi_*_locked` wrappers → `ncclNet*` → `dlsym`'d `neuron*`). The EFA/libfabric transport *internals* are a boundary owned by [transport-efa](../nccom/transport-efa.md); this page documents the hand-off, not the wire protocol.

For reimplementation, the contract is:

- **One service thread per LNC, polling completion — no semaphore.** Spawn exactly one consumer per host-LNC, guarded by a per-LNC init mutex; it is the only thread that runs the connect machine and registers MRs. P2P completion is `ncclNetTest` polling through `enc_async_sr_test_request`; the collective DMA semaphore edge (`nec_inc_semaphore @0x1bfd40`) is **not** on this path. Drive completion off a semaphore and you have rebuilt a different machine.
- **The fast/slow post gate preserves per-comm order.** A post takes the fast path (post immediately, `status = OFI_POSTED`) only if the MR is already cached **and** the comm's stashed ring is empty. The "stash ring empty" precondition is the ordering invariant: once anything is stashed, every later post on that comm also stashes, so the service thread cannot reorder a registration-pending request ahead of an already-posted one.
- **The rendezvous is keyed by `(send_lnc, recv_lnc, hostname)`, asymmetrically.** The RECV side listens, then `kv_store_set`s key `"handle-"+recv_lnc+send_lnc+peer_hostname → net_handle`; the SEND side `kv_store_get`s `"handle-"+send_lnc+recv_lnc+hostname`. A well-known `"hostname"` key, seeded at init against `127.0.0.1`, bootstraps the RECV side's view of the remote host. Get the key ordering wrong and sends never match their recvs.

## At a glance

| | |
|---|---|
| **Feature gate** | `NEURON_RT_ASYNC_SENDRECV_EXPERIMENTAL_ENABLED=1` + `NEURON_RT_ASYNC_SENDRECV_BOOTSTRAP_PORT` (both required at `enc_async_sr_init @0xeade0`) |
| **Per-LNC model** | 128 LNCs (`0..0x7F`); `async_sr_ctxs[128]`, `async_sr_status[128]`, init/close guard `async_sr_init_mutex[128]` |
| **Producer (connect)** | `async_sr_queue_connect_task @0xea300` ← `enc_async_sr_connect @0xebed0` / `enc_async_sr_accept @0xebf30` |
| **Producer (post)** | `enc_async_sr_post_op @0xec0c0` ← `send_tensor @0xec840` / `recv_tensor @0xec820` |
| **Consumer (service thread)** | `async_sr_service_thread_fn @0xef700` (one per LNC) ← `async_sr_service_thread_fn_pthread @0xf0fb0` |
| **Connect SM** | `async_sr_progress_connect_comm @0xee7e0` — 8 states; SEND `0→2→4→6`, RECV `0→1→5→3→4→6` |
| **Connect driver** | `async_sr_connect_comms @0xef490 [.isra.0]` — one SM step per comm per tick |
| **Completion / poll** | `enc_async_sr_test_request @0xec860` → `async_sr_ofi_test_locked @0xedcb0` → `ncclNetTest` |
| **Rendezvous client** | `kv_store_make_progress @0xf2730` — non-blocking TCP FSM (INIT→CONNECTING→SENDING→RECEIVING→CLOSING) |
| **comm record** | `nrt_async_sendrecv_comm` — **9760 B** (`0x2620`); `status` @+0x2610, freelist @+0x1c38, stashed ring @+0x2068 |
| **context record** | `async_sr_context` — **1424 B** (`0x590`); handover ring @+0x8, `task_n` @+0x520, `task_cv` @+0x4f0 |
| **request record** | `nrt_async_sendrecv_request` — **56 B** (`0x38`); `status` @+0x30, `ofi_req` @+0x20 |
| **OFI data-path lock** | `async_sr_ofi_mutex[lnc]` (`.bss 0xc8fdc0`) — data-path ops only; control-path ops unlocked |
| **Transport leaf** | `async_sr_ofi_{isend,irecv,iflush,test}_locked @0xeddc0/eded0/edfe0/edcb0` → `ncclNet*` → `dlsym neuron*` |

---

## 1. The Request Record and Its State Machine

### Purpose

One `nrt_async_sendrecv_request` is one in-flight P2P operation — a `{tensor, offset, length}` slice bound to a comm and a direction, with an OFI request handle and an atomic status. A comm owns a fixed pool of **128** of them, constructed in place by the comm constructor (`nrt_async_sendrecv_comm::nrt_async_sendrecv_comm @0xecd10`) and recycled forever; there is no per-op allocation on the data path. The request's status field is the synchronization point between the application producer, the service-thread consumer, and the completion poller.

### Layout

The record is 56 bytes, DWARF-confirmed (`async_sr_request.h`):

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `tensor` | `+0x00` | `nrt_tensor_t*` | source/destination tensor; VA resolved at post time |
| `offset` | `+0x08` | `size_t` | byte offset into the tensor |
| `length` | `+0x10` | `size_t` | transfer length (bounds-checked against tensor size) |
| `type` | `+0x18` | `async_sr_request_type` | `NOTYPE=0` / `ISEND=1` / `IRECV=2` |
| `ofi_req` | `+0x20` | `async_sr_ofi_req_handle` | 1-pointer wrapper; set non-NULL by `isend`/`irecv`, consumed by `test` |
| `comm` | `+0x28` | `nrt_async_sendrecv_comm*` | owning comm (asserted `== comm` on the slow path) |
| `status` | `+0x30` | `atomic<async_sr_request_status>` | the state-machine variable |

The constructor (`nrt_async_sendrecv_request @0xee3d0`) zeroes all seven fields, so a fresh request is `status = FREE(0)`, `type = NOTYPE(0)`, every handle NULL.

### The four states

`async_sr_request_status` is a 4-value enum (DWARF-confirmed; the names are verbatim in `async_sr_request_status_to_string @0xee430`): `FREE=0`, `STASHED=1`, `OFI_POSTED=2`, `ERROR=3`.

```text
                       enc_async_sr_post_op @0xec0c0
                       pop from comm->req_freelist (data[--top])
                                    │
            ┌───────── FAST path ───┴─── SLOW path ─────────┐
            │  (MR cached AND          (MR miss OR stash    │
            │   stash ring empty AND    ring non-empty)     │
            │   ofi_req set)                                │
            ▼                                               ▼
   ┌───────────────┐                              ┌───────────────┐
   │  OFI_POSTED 2 │◀──── service thread ─────────│   STASHED 1   │
   │               │   registers MR + posts;       │ (push to      │
   │ posted; ofi_  │   task_n--; status:=OFI_POSTED │  stashed_reqs │
   │ req live      │                                │  ring; task_  │
   └──────┬────────┘                                │  n++; notify) │
          │                                         └──────┬────────┘
          │  enc_async_sr_test_request @0xec860            │  VA lookup fails,
          │  ncclNetTest:                                  │  post fails, or
          │   done && rc==0  ──► reset fields, push        │  test rc!=0
          │                     back to freelist           ▼
          │   !done          ──► stay OFI_POSTED      ┌───────────────┐
          ▼                                           │    ERROR 3    │
   ┌───────────────┐   reset + push to freelist       │ (logged,      │
   │    FREE 0     │◀─────────────────────────────────│  recycled to  │
   │ (recyclable)  │                                   │  freelist)    │
   └───────────────┘                                   └───────────────┘
```

The freelist is a `locked_stack<request*,128>` (data base `+0x1c38`, top `+0x2078`, mutex `+0x2080`): `pop = data[--top]`, `push = data[top++]`. A `FREE` request is on the stack; a `STASHED`/`OFI_POSTED`/`ERROR` request is off it, owned by one of the producer/consumer/poller until it returns to `FREE` and is pushed back.

> **GOTCHA —** `STASHED` is not a transient. A request can sit in `STASHED` across many `enc_async_sr_test_request` polls — that function returns `(*done = 0, NRT_SUCCESS)` for a `STASHED` request without touching OFI. The poller must treat "stashed" as "not yet posted, keep polling," not as an error or a completion. The state only advances to `OFI_POSTED` when the *service thread* drains the stash ring, registers the MR, and posts the op — which happens on the consumer's schedule, not the poller's.

---

## 2. Post / Progress / Complete

The data path is three functions on two thread classes: the application producer posts (`enc_async_sr_post_op`), the per-LNC consumer progresses stashed posts (inside `async_sr_service_thread_fn`), and the application poller completes (`enc_async_sr_test_request`). All three touch the same request record and the same per-comm queues.

### Algorithm — POST (`enc_async_sr_post_op @0xec0c0`)

```c
// enc_async_sr_post_op — application (producer) thread, TU enc/async_sr/async_sr.cc
// returns 0 success | 1 invalid/post-fail | 3 unconnected | 4 freelist exhausted
function enc_async_sr_post_op(tensor, off, len, comm, out_req, req_type, comm_type):  // 0xec0c0
    // --- validation battery (each failure logs + returns) ---
    if (!tensor || !comm)                        return 1;   // "Expected valid tensor/communicator"
    if (comm->type != comm_type)                 return 1;   // SEND vs RECV mismatch  (comm+0x28)
    ctx = async_sr_ctxs[comm->lnc];                          // comm+0x24
    if (!ctx)                                    return 1;
    if (tensor is DMA && ctx->vtpb_idx != tensor->sto->vtpb_idx) return 1;
    if (comm->status != CONNECTED)               return 3;   // comm+0x2610 ; "post on unconnected communicator"
    if (off + len > nrt_tensor_get_size(tensor)) return 1;   // "transfer ... out of bounds"
    va = nrt_tensor_get_va(tensor);
    if (!va)                                     return 1;

    // --- pop a request slot from the per-comm freelist (locked stack) ---
    lock(comm->req_freelist.mutex);               // comm+0x2080
    if (comm->req_freelist.top == 0):             // comm+0x2078 == 0  → none free
        unlock; return 4;                         // "Maximum number of pending requests 128 reached"
    req = comm->req_freelist.data[--top];         // comm+0x1c38[--top]
    unlock;
    assert(req != nullptr);                        // async_sr.cc:0x36E

    // --- FAST-PATH GATE: MR already cached AND stash ring empty ---
    if (comm->stashed_reqs.head == comm->stashed_reqs.tail):       // comm+0x2068 ring empty?
        lock(comm->mhandle_cache_mutex);          // comm+0x24d8
        mhandle = mhandle_cache.find( key(va, comm) );  // hash = va ^ (2*comm)
        unlock;
        if (mhandle != nullptr):
            rc = comm->post_operation(&comm->ofi_comm, va + off, len,   // isend_locked / irecv_locked
                                      mhandle, &req->ofi_req, comm->lnc);
            if (rc == 0 && req->ofi_req.is_set()):
                req->{tensor,offset,length} = ...;
                req->type   = comm->req_type;
                req->status = OFI_POSTED;          // 2 — posted immediately, no service thread
                *out_req = req; return 0;
            if (rc != 0):                          // OFI post failed outright
                push req back to req_freelist;     // recycle the slot
                return 1;

    // --- SLOW PATH: stash for the service thread (async_sr_queue_sendrecv_task, inlined) ---
    req->{tensor,offset,length} = ...;
    req->type = req_type;
    assert(req->comm == comm);                     // async_sr.cc:0x295
    req->status = STASHED;                          // 1
    lock(ctx->task_mutex);  ctx->task_n += 1;  ctx->task_cv.notify_one();  unlock;   // wake consumer
    lock(comm->stashed_producer_mutex);            // comm+0x2478
        assert((tail - head) <= 0x7F);             // async_sr.cc:0x29A — ring not full
        comm->stashed_reqs.buffer[tail & 0x7F] = req;  tail += 1;
    unlock;
    *out_req = req; return 0;
```

> **QUIRK —** the fast path requires the stash ring to be **empty**, not merely the MR to be cached. That precondition is the entire per-comm ordering guarantee. The MR cache (`mhandle_cache`, comm `+0x24a0`) is keyed by `(tensor_va, comm)` with hash `va ^ (2·comm)`, so a hot tensor stays registered and the fast path is the steady state. But the moment any post stashes (a cold tensor, or a registration in flight), every subsequent post on that comm is forced to stash too — even ones whose MR is cached — so the service thread drains them in FIFO order and a registration-pending request can never be overtaken by a later already-registered one. A reimplementer who gates the fast path on the MR cache alone reorders posts within a comm.

### Algorithm — PROGRESS (the service thread's post drain, `async_sr_service_thread_fn @0xef700`)

The consumer's inner post-drain (the source-level `async_sr_post_sendrecv_task`, inlined) walks every CONNECTED comm and empties its stash ring, lazily registering the MR before posting:

```c
// inside async_sr_service_thread_fn — consumer thread, one per LNC
// for each comm in ctx->comms (CONNECTED list):
while (comm->stashed_reqs.head != comm->stashed_reqs.tail):   // SPSC ring drain
    req = comm->stashed_reqs.buffer[head & 0x7F];
    assert(req && req->status == STASHED && req->comm && req->tensor);  // sthread.cc:0x1CC/0x1F/0x20/0x21
    va = nrt_tensor_get_va(req->tensor);
    if (!va):
        log("Failed to obtain virtual address of tensor");
        req->status = ERROR;  ctx->task_n--;  advance head;  continue;

    // --- lazy MR registration into the per-comm cache (consumer side) ---
    lock(comm->mhandle_cache_mutex);
        node = mhandle_cache.find( key(va, comm) );
        if (!node):                                    // MISS — register now
            size    = nrt_tensor_get_size(req->tensor);
            mhandle = ncclNetRegMrOG(comm->ofi_comm.ptr, va, size, /*type=*/4, &mhandle);
            assert(mhandle != nullptr);                // sthread.cc:0x3D
            mhandle_cache.insert( {va, comm} -> mhandle );   // 0x28-byte node
    unlock;

    assert(req->type != NOTYPE);                       // sthread.cc:0x45
    rc = comm->post_operation(&comm->ofi_comm, va + req->offset, req->length,
                              mhandle, &req->ofi_req, comm->lnc);
    if (rc != 0):
        log("Failed to post async sendrecv request %p type %s");
        req->status = ERROR;  ctx->task_n--;  advance head;  continue;
    if (rc == 0 && req->ofi_req.is_set()):
        ctx->task_n--;                                  // _InterlockedSub
        advance head;                                   // _InterlockedAdd64
        req->status = OFI_POSTED;                        // 2
```

The MR registration type constant `4` is the `ncclNetRegMrOG` "type" argument used uniformly by this subsystem (init's flush buffer and every data-path tensor register with it); its meaning is owned by the transport ([transport-efa](../nccom/transport-efa.md)). The cache is never evicted on the data path — entries are deregistered (`async_sr_ofi_dereg_mr_og @0xee3b0`) only at comm close.

### Algorithm — COMPLETE (`enc_async_sr_test_request @0xec860`)

```c
// enc_async_sr_test_request — application poller; out: *done, *size
// returns 0 (poll ok / in-flight / completed) | 1 (error, recycled) | 3 (bad/unused request)
function enc_async_sr_test_request(req, done, size):           // 0xec860
    if (!req || !req->comm)  return 3;                          // "Expected request/communicator"
    switch (req->status):                                       // comm+0x30
      case OFI_POSTED:                                          // 2
        *done = 0; *size = 0;
        assert(req->type != NOTYPE && req->ofi_req.is_set() && req->comm);  // 0x3C7/0x3C8/0x3C9
        rc = async_sr_ofi_test_locked(&req->ofi_req, done, size, comm->lnc);  // → ncclNetTest
        if (rc != 0):
            req->status = ERROR;                                // _InterlockedExchange
            reset(req);  push to req_freelist;  return 1;
        if (*done):                                             // completed
            log("Successfully tested request");
            reset(req);                                         // zero tensor/off/len/type/ofi_req/status → FREE
            lock(comm->req_freelist.mutex);
                assert(top <= 0x7F);  data[top++] = req;        // 0x3E4 — back to FREE pool
            unlock;
            return 0;
        return 0;                                               // !done — still in flight
      case FREE:     return 3;                                   // "Request not in use"
      case STASHED:  *done = 0; return 0;                        // not yet posted by service thread
      case ERROR:    log("Request state %s"); reset+recycle; return 1;
```

> **NOTE —** there is **no semaphore** anywhere in this chain. Completion is `async_sr_ofi_test_locked → ncclNetTest`, a poll of the OFI request handle under `async_sr_ofi_mutex[lnc]`. The collective DMA path's MMIO semaphore edge (`nec_inc_semaphore @0x1bfd40`, [nccl-boundary](nccl-boundary.md) §4) is a separate, exported callback consumed by `libnccom`'s collectives signaling; the async P2P path holds no internal reference to it. A reimplementer who wires P2P completion to a hardware semaphore is modeling the wrong subsystem.

---

## 3. The Connect / Accept State Machine

### Purpose

Before any tensor can move, the two endpoints must establish an OFI connection, and they cannot do so directly — neither knows the other's libfabric net handle. The connect machine (`async_sr_progress_connect_comm @0xee7e0`) drives one comm through an eight-state rendezvous in which the RECV side publishes its listen handle into the embedded key-value store and the SEND side fetches it. The machine is **non-blocking and resumable**: each service-thread tick advances a comm by at most one step (the KV client makes a little socket progress, the SM checks `done`, advances `status` via `_InterlockedExchange`), then yields so other comms and other LNCs are not starved.

### State diagram

`async_sr_comm_status` is an 8-value enum (DWARF-confirmed; names verbatim from the `status_to_string` jump table at `0x8578a4`). The two halves are asymmetric — a SEND comm never visits states 1/3/5; a RECV comm never visits state 2 (and reaches 6/7 only terminally):

```text
                       async_sr_queue_connect_task @0xea300
                       new comm, status = START_INIT(0), pushed to handover ring
                                          │
            ┌──────── SEND_COMM (type 0) ─┴── RECV_COMM (type 1) ───────────┐
            ▼                                                               ▼
   ┌──────────────────┐                                       ┌──────────────────────┐
   │ 0 START_INIT     │                                       │ 0 START_INIT         │
   │  create key      │                                       │  kv_store_get        │
   │  kv_store_get    │                                       │   "hostname"         │
   │   (net_handle)   │                                       │  → callback fills    │
   │  → callback fills│                                       │    peer_hostname     │
   │    net_handle    │                                       │    (comm+0x2580)     │
   └────────┬─────────┘                                       └──────────┬───────────┘
            ▼ status:=2                                                   ▼ status:=1
   ┌──────────────────┐                                       ┌──────────────────────┐
   │ 2 WAIT_FOR_NET_  │                                       │ 1 WAIT_FOR_HOSTNAME  │
   │   HANDLE_GET     │                                       │  kv_store_make_      │
   │  kv_store_make_  │                                       │   progress; on done  │
   │   progress; done │                                       │   free handle        │
   └────────┬─────────┘                                       └──────────┬───────────┘
            ▼ status:=4                                                   ▼ status:=5
            │                                                 ┌──────────────────────┐
            │                                                 │ 5 LISTEN             │
            │                                                 │  async_sr_ofi_listen │
            │                                                 │   → net_handle = my  │
            │                                                 │     listen handle    │
            │                                                 │  kv_store_set        │
            │                                                 │   (key, net_handle)  │
            │                                                 └──────────┬───────────┘
            │                                                            ▼ status:=3
            │                                                 ┌──────────────────────┐
            │                                                 │ 3 WAIT_FOR_NET_      │
            │                                                 │   HANDLE_SET         │
            │                                                 │  kv_store_make_      │
            │                                                 │   progress; free hdl │
            │                                                 └──────────┬───────────┘
            ▼                                                            ▼ status:=4
   ┌──────────────────┐                                       ┌──────────────────────┐
   │ 4 NET_CONNECT    │                                       │ 4 NET_CONNECT        │
   │  async_sr_ofi_   │                                       │  async_sr_ofi_accept │
   │   connect        │                                       │   (l_comm → ofi_comm)│
   │   (net_handle →  │                                       │                      │
   │    ofi_comm)     │                                       │                      │
   └────────┬─────────┘                                       └──────────┬───────────┘
            ▼ status:=6                                                   ▼ status:=6 (if ofi_comm set)
            └────────────────────► 6 CONNECTED ◀───────────────────────────┘
                                    (terminal OK — comm → ctx->comms)

   ANY error on either path:  nlog_write + status := 7 CONNECTION_ERROR
                              (kv_store_req freed if present; comm → conn_error_comms)
```

### Algorithm — `async_sr_progress_connect_comm @0xee7e0`

```c
// async_sr_progress_connect_comm — runs on the service thread, one step per call
// branches on comm->type into the (inlined) _send_comm / _recv_comm bodies
function async_sr_progress_connect_comm(ctx, comm):            // 0xee7e0
    assert(comm->status != CONNECTED);                          // sthread.cc:0x149
    loop until status==CONNECTED(6) || status==CONNECTION_ERROR(7) || a make_progress stalls:

      if (comm->type == SEND_COMM):                             // states 1,3,5 illegal → assert 0xBB
        switch (comm->status):
          case START_INIT:                                      // 0
            key = async_sr_create_handle_req_key(comm->lnc, comm->peer_lnc, ctx->hostname);
            assert(key.size() < KV_STORE_KEY_LENGTH /*0x90*/);  // send 0x7F
            kv_store_get(comm->peer_ip, boot_port, /*retry=*/1, &comm->net_handle,
                         /*type=*/1, &send_callback, &comm->kv_store_req);  // 0xee6b0 fills net_handle
            assert(comm->kv_store_req);                          // 0x8F
            status := WAIT_FOR_NET_HANDLE_GET;                   // 2
          case WAIT_FOR_NET_HANDLE_GET:                          // 2
            kv_store_make_progress(comm->kv_store_req, &done, &made);
            if (done):  status := NET_CONNECT;                   // 4 (callback already filled net_handle)
          case NET_CONNECT:                                      // 4
            async_sr_ofi_connect(ctx->nic, &comm->net_handle, &comm->ofi_comm);  // → ncclNetConnect
            status := CONNECTED;                                 // 6

      else: /* RECV_COMM */                                     // states 2,6,7 illegal → assert 0x142
        switch (comm->status):
          case START_INIT:                                      // 0
            kv_store_get("hostname", boot_port, /*retry=*/1, &comm->peer_hostname,
                         /*type=*/0, &recv_callback, &comm->kv_store_req);  // 0xee620 fills peer_hostname
            assert(comm->kv_store_req);                          // 0xE6
            status := WAIT_FOR_HOSTNAME;                         // 1
          case WAIT_FOR_HOSTNAME:                                // 1
            kv_store_make_progress(comm->kv_store_req, &done, &made);
            if (done):  kv_store_free_handle(comm->kv_store_req); comm->kv_store_req = null;
                        status := LISTEN;                        // 5
          case LISTEN:                                           // 5
            async_sr_ofi_listen(ctx->nic, &comm->net_handle, &comm->l_comm);  // net_handle = my listen handle
            key = async_sr_create_handle_req_key(comm->peer_lnc, comm->lnc, comm->peer_hostname);
            kv_store_set("127.0.0.1", boot_port, key, comm->net_handle, 128, &comm->kv_store_req);
            assert(comm->kv_store_req);                          // 0x118
            status := WAIT_FOR_NET_HANDLE_SET;                   // 3
          case WAIT_FOR_NET_HANDLE_SET:                          // 3
            kv_store_make_progress(comm->kv_store_req, &done, &made);
            if (done):  kv_store_free_handle(...);  status := NET_CONNECT;   // 4
          case NET_CONNECT:                                      // 4
            async_sr_ofi_accept(&comm->l_comm, &comm->ofi_comm);  // → ncclNetAccept
            if (comm->ofi_comm.ptr):  status := CONNECTED;       // 6

      on ANY error:  nlog_write(...);  status := CONNECTION_ERROR;  // 7 ; kv_store_req freed if present
```

> **QUIRK —** the rendezvous key encodes direction, and the SEND/RECV halves build it with the LNCs in **opposite order** so they collide on the same KV entry. `async_sr_create_handle_req_key @0xee4a0` lays out a 143-byte string: `"handle-"` (7 B) + `send_lnc` (4 B LE) + `recv_lnc` (4 B LE) + 128-byte hostname. The RECV side calls it `(peer_lnc, lnc, peer_hostname)` and `kv_store_set`s the result; the SEND side calls it `(lnc, peer_lnc, hostname)` and `kv_store_get`s it. For the two strings to match, the SEND side's `(lnc, peer_lnc, hostname)` must equal the RECV side's `(peer_lnc, lnc, peer_hostname)` — i.e. the send's local LNC is the recv's peer LNC and vice versa, and both name the same host. The separate well-known `"hostname"` key (seeded once at init to `"127.0.0.1"`) is what lets the RECV side learn the remote hostname before it can build the directional key at all. Build the key with the LNCs in the same order on both sides and no send will ever find its recv's handle.

> **NOTE —** the two KV callbacks are `std::function` lambdas registered on the `kv_store_get` request, not inline copies. The SEND callback (`_M_invoke @0xee6b0`) asserts the reply length equals `comm->net_handle.size()` (128) and `memcpy`s 128 bytes into `comm+0x2500` (`net_handle`); the RECV callback (`_M_invoke @0xee620`) asserts length `== comm->peer_hostname.size()` (128) and copies 128 bytes into `comm+0x2580` (`peer_hostname`). The KV client (`kv_store_make_progress`) invokes the callback when it parses a GET response, so by the time `status` advances past the wait state the destination array is already filled — the SM never reads the handle until the callback has run.

---

## 4. The Service-Thread Model and the KV Rendezvous

### Purpose

One service thread per LNC is the single consumer of both producer queues. It is spawned by `enc_async_sr_init @0xeade0` (via the trampoline `async_sr_service_thread_fn_pthread @0xf0fb0`) and lives until `enc_async_sr_close` signals shutdown and joins it. The thread is the *only* place the connect machine runs and the *only* place MRs are registered; the per-LNC slot is brought up and torn down under `async_sr_init_mutex[lnc]`, so init/close of an LNC and its service thread never race.

### Entry Point

```text
enc_async_sr_init 0xeade0  (under async_sr_init_mutex[lnc])
  └─ pthread_create(&ctx->service_thread_id, async_sr_service_thread_fn_pthread, ctx)
       └─ async_sr_service_thread_fn_pthread 0xf0fb0          ── trampoline
            └─ async_sr_service_thread_fn 0xef700             ── *** the engine, one per LNC ***
                 async_sr_thread_n++  (0xc911c0)
                 LOOP (LABEL_2):
                   assert(task_n >= 0)                        ── sthread.cc:0x1AE
                   if (task_n == 0):  task_cv.wait()          ── sleep until a producer notifies
                   if (service_thread_shutdown_signal): break
                   ├─ pickup_connection_task (inlined)        ── drain handover ring into 2 ordered maps
                   ├─ async_sr_connect_comms(SEND map) 0xef490 ── one SM step per comm
                   ├─ async_sr_connect_comms(RECV map) 0xef490
                   └─ post_sendrecv_task (inlined)            ── drain each CONNECTED comm's stash ring (§2)
                 SHUTDOWN: abort in-flight comms → conn_error_comms; async_sr_thread_n--
```

### The three queues and the wakeup token

The whole pipeline is three bounded queues plus two atomics, all in the per-LNC `async_sr_context` (1424 B, `0x590`) and the per-comm `nrt_async_sendrecv_comm` (9760 B, `0x2620`):

| Queue / atomic | Location | Kind | Producer → Consumer |
|---|---|---|---|
| `conn_task_handover_queue` | ctx `+0x8` | SPSC ring `<comm*,128>` (`& 0x7F`) | `queue_connect_task` (under `+0x418` mutex) → pickup, inlined |
| `comm->stashed_reqs` | comm `+0x2068` | SPSC ring `<request*,128>` (`& 0x7F`) | post slow path (under `+0x2478` mutex) → post drain, inlined |
| `comm->req_freelist` | comm `+0x1c38` | `locked_stack<request*,128>` (top `+0x2078`, mutex `+0x2080`) | post pop / complete push |
| `task_n` | ctx `+0x520` | `atomic<int>` | outstanding-work counter / wakeup token (asserted `>= 0`) |
| `task_cv` / `task_mutex` | ctx `+0x4f0` / `+0x4c8` | cond var / mutex | producer `notify_one` → consumer `wait` |
| `service_thread_shutdown_signal` | ctx `+0x524` | `atomic<bool>` | close → consumer break |

The handover ring carries *comms* (connect tasks); the stash ring carries *requests* (data posts). The pickup step partitions the handover ring into two `std::map<pair<string,int>, queue<comm*>>` keyed by `(peer_ip, peer_lnc)` (the `str_int_pair_comparator`-ordered map confirmed in the symbol table) — one map per direction — so `async_sr_connect_comms` can advance same-peer comms in arrival order. `task_n` is bumped by both producers and decremented by the consumer as each unit of work retires; when it hits 0 the thread sleeps on `task_cv` rather than spinning.

> **QUIRK —** `async_sr_connect_comms @0xef490` advances each pending comm by **at most one SM step per tick**, then returns. A comm mid-handshake is left in its direction's map and revisited next wakeup; a comm that reaches `CONNECTED(6)` moves to `ctx->comms`, one that errors moves to `conn_error_comms`. This is what makes the connect machine non-blocking: the service thread never blocks on a TCP connect or an OFI accept — it pumps `kv_store_make_progress` (a non-blocking socket FSM) one notch, checks `done`, and yields. A reimplementer who blocks the consumer inside a single comm's rendezvous stalls every other comm and every stashed post on that LNC.

### The KV rendezvous client — `kv_store_make_progress @0xf2730`

The rendezvous transport is an embedded non-blocking TCP key-value store. Each `kv_store_get`/`kv_store_set` builds a `kv_store_client_handle` (2416 B, `0x970`) carrying a fixed 1156-byte frame (`KV_STORE_BUFFER_LENGTH`), and the connect SM pumps it one socket operation at a time:

```c
// kv_store_make_progress — non-blocking rendezvous client FSM (TU kv_store.cc)
// out: *done (advances the connect SM), *made_progress
function kv_store_make_progress(h, done, made):                // 0xf2730
    if (h->is_completed):  *done = 1;  return h->is_failed;
    switch (h->state):                                          // h+0x958
      INIT:       socket(AF_INET,SOCK_STREAM); set O_NONBLOCK; inet_pton; connect();
                  if (errno == EINPROGRESS):  state = CONNECTING;  else fail;
      CONNECTING: poll(POLLOUT, 0); getsockopt(SO_ERROR);
                  if (0):            state = SENDING; *made = 1;
                  if (ECONNREFUSED): state = INIT;            // server not up yet — retry
                  else fail;
      SENDING:    send(request_buffer[bytes_sent..], 1156 - bytes_sent, MSG_NOSIGNAL);
                  if (bytes_sent > 1155):  state = RECEIVING; bytes_received = 0;
                  if (errno == EAGAIN):    return success;     // try again next tick
      RECEIVING:  recv(buffer[bytes_received..], 1156 - bytes_received);
                  if (bytes_received > 1155):  opcode = buffer[0]:
                      2,3 (GET response): parse + invoke callback (fills net_handle/hostname); state = CLOSING;
                      1   (SET ack):      state = CLOSING;
                      5   (NOT_FOUND):    if (retry_on_not_found) re-SEND else CLOSING;
                      6   (server error): fail;
                  if (recv == 0):  fail ("Connection closed by server");
      CLOSING:    send the end-request; on full send → is_completed = 1; *done = 1; *made = 1;
      FAILED:     *done = 1;  return FAILURE;
```

`*done` is exactly the signal the connect SM (§3) waits on in each `WAIT_FOR_*` state. The `retry_on_not_found` flag (passed as the `retry=1` argument by both SEND and RECV `kv_store_get` calls) is what lets the SEND side poll for a handle the RECV side has not yet published — `NOT_FOUND` re-sends the request rather than failing, so a send posted before its recv listens will spin in `WAIT_FOR_NET_HANDLE_GET` until the recv's `kv_store_set` lands.

> **NOTE —** the KV *server* side (`kv_store_handle_client_fn @0xf4a50`, the global `kv_store_map`, the file-lock leader election on `/tmp/nrt_kv_store_server_lock_uid_<uid>`, and the exact 1156-byte frame layout / opcodes `1..6`) is a boundary owned by the kv_store cell — only one process per UID wins the file lock and runs the server; every LNC's clients talk to it over loopback on the bootstrap port. This page documents the *client* FSM the connect machine drives, not the server protocol. The numeric ordering of `kv_store_client_op_state` (INIT first … through CLOSING/FAILED) is inferred from the switch layout rather than a labelled enum dump (MED confidence); the state *transitions* above are HIGH.

---

## 5. The OFI Transport Leaf

### Purpose

The transport leaf is the thinnest possible C++ adapter between the async_sr engine and the collective network plugin. Eleven `async_sr_ofi_*` wrappers each take a per-LNC lock (data-path ops only), call the matching libnrt-internal `ncclNet*` wrapper, and move the opaque OFI comm/req pointer into the caller's 1-pointer handle struct. The `ncclNet*` wrappers are themselves trampolines into a `.bss` function-pointer table that `ncclInit` filled by `dlsym`'ing `neuron*` symbols out of `libnccom` — so the true cross-binary boundary is one hop further out ([nccl-boundary](nccl-boundary.md)).

### The two op classes and the lock discipline

| Class | Wrappers (addr) | Lock? | Forwards to |
|---|---|---|---|
| data-path | `test_locked 0xedcb0`, `isend_locked 0xeddc0`, `irecv_locked 0xeded0`, `iflush_locked 0xedfe0` | **yes** — `async_sr_ofi_mutex[lnc]` | `ncclNetTest/Isend/Irecv/Iflush` |
| control-path | `connect 0xee0f0`, `listen 0xee1a0`, `accept 0xee270`, `close_{listen,send,recv} 0xee320/350/380`, `dereg_mr_og 0xee3b0` | no | `ncclNetConnect/Listen/Accept/Close*/DeregMrOG` |

```c
// representative data-path wrapper — async_sr_ofi_isend_locked @0xeddc0
// bound as comm->post_operation (comm+0x30) by enc_async_sr_connect (SEND) / _accept (RECV)
function async_sr_ofi_isend_locked(comm /*&ofi_comm*/, data, size, mhandle, req /*&ofi_req*/, lnc):  // 0xeddc0
    lock(async_sr_ofi_mutex[lnc]);                  // .bss 0xc8fdc0[lnc]
    rc = ncclNetIsend(comm.ptr, data, size, mhandle, &req_ptr);   // 0x1c3da0 → *neuronNetIsend
    unlock(async_sr_ofi_mutex[lnc]);
    if (rc != 0):  nlog("ENC", ..., "Failed to post ofi send request");  return rc;
    assert(req.empty());                            // ofi.cc:0x29 — handle must be fresh
    req.set(req_ptr);                               // store new OFI request into the 1-ptr handle
    return NRT_SUCCESS;
```

The `post_operation` function pointer (comm `+0x30`) is bound at connect time: `enc_async_sr_connect` passes `async_sr_ofi_isend_locked` and `enc_async_sr_accept` passes `async_sr_ofi_irecv_locked` into `async_sr_queue_connect_task`, which stores it in the comm constructor. Neither `*_locked` op has a direct caller — they are reached only through this fn-ptr (and `enc_async_sr_flush`'s direct `iflush` call), which is why a SEND comm always posts isends and a RECV comm always posts irecvs without a per-op branch.

> **NOTE —** the `ncclNet*` targets are libnrt-internal wrappers (band `0x1c3xxx`), not the cross-binary symbols. Each forwards through a `.bss` slot to a `dlsym`'d `neuron*` entry point — e.g. `ncclNetTest @0x1c4310 → *neuronNetTest`. The all-or-nothing `dlopen`/`dlsym` handoff, the `nccl_init_error` short-circuit, and the EFA/libfabric data path beneath are documented in [nccl-boundary](nccl-boundary.md) and [transport-efa](../nccom/transport-efa.md). This page hands off at the `ncclNet*` call; it does not re-derive the immediate-data word, the rail striping, or the CQ harvest.

### The flush fence

`enc_async_sr_flush @0xebd10` is the read-after-recv fence and the one OFI op that is *not* a tensor transfer. It posts `async_sr_ofi_iflush_locked` against a 4-byte HBM loopback buffer (set up at init, registered with the same `type=4` MR) until the OFI request is set, then polls `async_sr_ofi_test_locked` until done. The flush makes prior IRECV DMA writes to HBM visible to the host; like the data path, it uses an OFI iflush + poll, never a semaphore.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_async_sr_init` (`@0xeade0`) | brings up the per-LNC context, seeds the KV `"hostname"` key, registers the flush MR, spawns the service thread |
| `async_sr_service_thread_fn` (`@0xef700`) | the sole per-LNC consumer: connect SM + MR registration + stash drain |
| `async_sr_progress_connect_comm` (`@0xee7e0`) | the 8-state connect/accept machine, one step per tick |
| `kv_store_make_progress` (`@0xf2730`) | the non-blocking TCP rendezvous client the connect SM pumps |
| `async_sr_ofi_isend_locked` (`@0xeddc0`) | the SEND `post_operation` fn-ptr; the data-path hand-off into `ncclNetIsend` |
| `nec_inc_semaphore` (`@0x1bfd40`) | the collective DMA semaphore edge — **not** on this P2P path (completion is OFI poll) |

## Cross-References

- [Public C API: Async, SendRecv and Collectives Thunks](../runtime/api-async-collectives.md) — the `nrt_async_sendrecv_*` public thunks that front `enc_async_sr_*`
- [Overview: the Collective-Compute Architecture](overview.md) — where the `SEND`/`RECV` op-types sit in the 13-case `enc_op_type` dispatch and why P2P is host-driven, not device-compiled
- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](nccl-boundary.md) — the `dlopen`-forward / `dlsym`'d `neuron*` seam every `ncclNet*` wrapper in §5 rides
- [Inter-Node Transport: EFA / libfabric](../nccom/transport-efa.md) — the EFA/libfabric data path beneath `ncclNetIsend`/`Irecv`/`Test`: rails, immediate-data demux, CQ harvest
- [Bananaphone IPC and the Proxy Driver](proxy-driver.md) — the proxy-driver model for multi-node ring net edges; contrast to the dedicated per-LNC service thread here
- [back to index](../index.md)
