# Public C API: Async, SendRecv and Collectives Thunks

> *All addresses, offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`; build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce917b633a70eb3d0bc460f34ac3da620`). The ELF is **not** stripped and retains DWARF — function names, struct field names, and enum constants survive. `.text` VMA equals file offset, so every `0x…` is an analysis VMA verifiable against `readelf -sW` / `nm -n libnrt.so`. Export versions are split: the explicit-async `nrta_*` family is `@@NRT_3.0.0`; the sendrecv thunks and host-collective ops are `@@NRT_2.0.0`. Other versions will differ.*
> *Evidence grade: **Confirmed (export-, disasm-, and string-anchored)** — entry addresses and version nodes from `native_exports.json` / `readelf -sW` (the `@@NRT_3.0.0` vs `@@NRT_2.0.0` split), each forward target from the thunk disassembly and the IDA call graph, reconstructed prototypes from the typed argument marshalling in each thunk body. The `enc_async_sr_*` engine and the XU/exec implementations are boundaries (see Cross-References) and are described here only by the forward edge. · Part IV — Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **thin public ABI surface** for three families of operation that the runtime keeps *out* of the synchronous `nrt_execute` path: the explicit-async schedule family `nrta_*`, the point-to-point async send/recv thunks `nrt_async_sendrecv_*`, and the two host-driven collective ops `nrt_barrier` / `nrt_all_gather`. What unifies them is that the symbols documented here are **almost entirely forwarders**. A `nrta_*` entry marshals its typed arguments, runs one lifecycle/mutual-exclusion guard, and tail-calls into the XU work-queue producer or the exec manager; a `nrt_async_sendrecv_*` entry is a one-instruction jump into the collectives `enc_async_sr_*` engine; `nrt_barrier` and `nrt_all_gather` run the four-state init guard and then delegate to the collective/NEFF machinery. The implementations live elsewhere — the XU producer/consumer and the doorbell are owned by [exec/submit-path](../exec/submit-path.md), and the OFI rendezvous engine by [collectives/send-recv](../collectives/send-recv.md).

The reason this surface is worth its own page is a fact a reimplementer must not miss: **these thunks are the only place the typed prototypes survive.** The hot bodies they forward into are reached internally through function pointers, XU dispatch tables, and `dlsym`'d `ncclNet*` wrappers; the engine code is shaped around untyped request records (the 64-byte `io_req`, the 24-byte `cc_sched_req`, the opaque `nrt_async_sendrecv_comm*`). The C signature that a framework integrator links against — the argument count, order, types, and the out-parameter convention — is recoverable *only* by reading these forwarder bodies. Lose the thunk layer and you have an engine with no callable boundary. This page therefore treats the surface the way the binary does: it pins every public symbol to its address, its version node, and its single forward target, shows the one recurring thunk *shape* as annotated pseudocode, and reconstructs the prototypes — then stops at the forward edge.

The familiar reference frame is a hand-written stable C ABI laid over a versioned internal C++/queue core, the same delegation pattern the rest of the public surface uses (`nrt_execute` → `kmgr_exec`, the tensor entries → `tensor_*` workers). The novelty is the **two-axis version split**: the explicit-async producers were added under a *new* symbol-version node `NRT_3.0.0`, while everything else — the sendrecv thunks, the host collectives — remains on the original `NRT_2.0.0`. A loader that resolves `nrta_execute_schedule` against a `NRT_2.0.0`-only library will not find it; the `nrta_*` family is the ABI marker of a 3.x-capable runtime. A second cross-cutting rule governs the `nrta_*` family alone: every one of them **hard-rejects when the runtime is in implicit-async mode** (`gconf.implicit_async_mode`, set by `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS`), because the implicit and explicit async modes are mutually exclusive.

The page is organized as: §1 the **complete public ABI table** — every `nrta_*`, every sendrecv thunk, `nrt_barrier`, `nrt_all_gather`, each with its address, version node, and forward target; §2 the recurring **thunk shape** (arg-marshal → guard → tail-call) as annotated `c` pseudocode; §3 the **host-collective ops** (`nrt_barrier` / `nrt_all_gather`) as `c` pseudocode, since they carry one extra step beyond a pure thunk; §4 the **reconstructed ABI signatures**, with the standing note that these prototypes survive only here.

For reimplementation, the contract is:

- **The version split** — `nrta_*` is `@@NRT_3.0.0`, all sendrecv thunks and the host collectives are `@@NRT_2.0.0`; the `nrta_*` node is the marker of an explicit-async-capable build.
- **The explicit/implicit async interlock** — every `nrta_*` entry rejects with `NRT_FAILURE(1)` when `gconf.implicit_async_mode` is set; the two modes never coexist.
- **The thunk shape** — for the sendrecv family, a literal one-instruction forward; for the `nrta_*` family, arg-validate → lifecycle gate → implicit-async gate → core/queue resolve → tail-call the XU producer or exec scheduler, returning a 64-bit `nrta_seq_t` the caller later polls.
- **The host-collective extra step** — `nrt_barrier` lazily builds a per-LNC barrier NEFF on first call (sticky-disabled on failure); `nrt_all_gather` does a local slice `memcpy` before the host bootstrap-socket all-gather.
- **The forward-target map** — every public symbol's single internal callee, so the engine can be wired in once it exists, and the thunk layer regenerated mechanically over it.

| | |
|---|---|
| **Explicit-async family** | `nrta_*` — 8 entries, `0x7bb00`–`0x7e440`, export `@@NRT_3.0.0`, src `nrt/nrt_async.cpp` |
| **SendRecv thunks** | `nrt_async_sendrecv_*` — 11 entries, `0x7ed80`–`0x7ee20`, export `@@NRT_2.0.0`, → `enc_async_sr_*` |
| **Host collectives** | `nrt_barrier` `@0x7ee30` · `nrt_all_gather` `@0x7f2d0`, export `@@NRT_2.0.0` |
| **State word** | `nrt_init_state` `@0xc5d1a0` (`.bss`, `NRT_INIT_STATE`) — the shared four-state guard |
| **Async-mode interlock** | `gconf.implicit_async_mode` (env `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS`) → `nrta_*` reject |
| **Sequence handle** | `nrta_seq_t` (`uint64`): `[63:56]=lnc/vtpb`, `[55:52]=xu_type\|(queue<<4)`, `[51:0]=counter` |
| **XU producer arrays** | `ccschedxu @0xc37c60` · `ccprepxu @0xc40460` · `tensoropxu @0xc48c60` (engine, [exec/submit-path](../exec/submit-path.md)) |
| **Source** | `nrt/{nrt_async,nrt_barrier,nrt_collectives}.cpp`, forwarding into `enc/async_sr/`, `tdrv/host_collectives.c` |

> **NOTE —** the implementation halves are deliberately out of scope. The XU work-queue producer/consumer model (`init_xu_common`, the `thr_io`/`thr_cc_*` workers, the `std::deque` request records, the `completed_seq` advance) is owned by [exec/submit-path](../exec/submit-path.md); the OFI rendezvous engine (`enc_async_sr_init`, the connect/accept state machine, the KV store) by [collectives/send-recv](../collectives/send-recv.md). This page documents the *callable boundary* — what the symbols are, what they take, and where they go — not the engines behind it.

---

## 1. The Public ABI Table

### Purpose

This is the surface a framework integrator links against, in one table. Every row is a public exported symbol; the columns pin it to its `.text` address, its symbol-version node (the `@@NRT_2.0.0` vs `@@NRT_3.0.0` split a loader resolves against), and its single forward target — the internal body it tail-calls or jumps to. The forward target is itself a boundary: its address is given for cross-checking, but its logic is owned by the page named in Cross-References. The Confidence column flags the one row whose forward-target address was not isolated to a single byte offset in the mined evidence.

### Explicit-async schedule family (`@@NRT_3.0.0`)

The `nrta_*` family is the **producer side** of three process-global per-vtpb "execution unit" (XU) work-queue arrays. Each entry resolves a virtual core, locks the addressed XU, lazily spawns its worker thread, assigns a monotonic `nrta_seq_t`, pushes a typed request record onto the XU's `std::deque`, and returns the sequence to the caller. Completion is later polled with `nrta_is_completed` / `nrta_get_sequence`. The queue mechanics are owned by [exec/submit-path](../exec/submit-path.md); the "forwards-to" column names the internal entry each producer drives.

| Public symbol | Addr | Version | Forwards to (symbol @ addr) | Confidence |
|---|---|---|---|---|
| `nrta_execute_schedule` | `0x7bb00` | `@@NRT_3.0.0` | compute-XU schedule → `kmgr_tpb_xu_schedule @0xe01d0` | HIGH |
| `nrta_cc_schedule` | `0x7bd80` | `@@NRT_3.0.0` | push `cc_sched_req` → `ccschedxu[vtpb] @0xc37c60` (worker `thr_cc_sched @0x7b7f0`) | HIGH |
| `nrta_get_sequence` | `0x7c450` | `@@NRT_3.0.0` | read `completed_seq` → `kmgr_xu_get_compl_seq @0xdd510` / XU array | HIGH |
| `nrta_is_completed` | `0x7c720` | `@@NRT_3.0.0` | decode seq bits → compare XU `completed_seq >= seq` (`vtpb_get_virtual_core_by_id @0x314050`) | HIGH |
| `nrta_cc_prepare` | `0x7c980` | `@@NRT_3.0.0` | push `cc_prep_req` → `ccprepxu[vtpb] @0xc40460` (worker `thr_cc_prep @0x7b6b0`) | HIGH |
| `nrta_tensor_copy` | `0x7d8c0` | `@@NRT_3.0.0` | push `io_req(COPY=2)` → `tensoropxu[vtpb][queue] @0xc48c60` (worker `thr_io @0x7b4f0`) | HIGH |
| `nrta_tensor_write` | `0x7df10` | `@@NRT_3.0.0` | push `io_req(WRITE=1)` → `tensoropxu[vtpb][queue]` | HIGH |
| `nrta_tensor_read` | `0x7e440` | `@@NRT_3.0.0` | push `io_req(READ=0)` → `tensoropxu[vtpb][queue]` | HIGH |

### SendRecv thunks (`@@NRT_2.0.0`)

All eleven `nrt_async_sendrecv_*` are pure forwarders into the collectives `enc_async_sr_*` engine ([collectives/send-recv](../collectives/send-recv.md)). They carry the public ABI signatures and nothing else — six are literal one-instruction `jmp`s (the `init`/`close`/`connect`/`accept`/`get_max_*` cluster `0x7ed80`–`0x7edd0`); the rest follow the same shape. The "forwards-to" address is the hot body's entry; those bodies are documented on the send-recv page.

| Public symbol | Addr | Version | Forwards to (symbol @ addr) | Confidence |
|---|---|---|---|---|
| `nrt_async_sendrecv_get_max_num_communicators_per_lnc` | `0x7ed80` | `@@NRT_2.0.0` | `enc_async_sr_get_max_num_communicators_per_lnc @0xea7f0` | HIGH |
| `nrt_async_sendrecv_get_max_num_pending_request` | `0x7ed90` | `@@NRT_2.0.0` | `enc_async_sr_get_max_num_pending_request @0xeadd0` | HIGH |
| `nrt_async_sendrecv_init` | `0x7eda0` | `@@NRT_2.0.0` | `enc_async_sr_init @0xeade0` | HIGH |
| `nrt_async_sendrecv_close` | `0x7edb0` | `@@NRT_2.0.0` | `enc_async_sr_close @0xea860` | HIGH |
| `nrt_async_sendrecv_connect` | `0x7edc0` | `@@NRT_2.0.0` | `enc_async_sr_connect @0xebed0` | HIGH |
| `nrt_async_sendrecv_accept` | `0x7edd0` | `@@NRT_2.0.0` | `enc_async_sr_accept @0xebf30` | HIGH |
| `nrt_async_sendrecv_test_comm` | `0x7ede0` | `@@NRT_2.0.0` | `enc_async_sr_test_comm` (addr not isolated) | MEDIUM |
| `nrt_async_sendrecv_send_tensor` | `0x7edf0` | `@@NRT_2.0.0` | `enc_async_sr_send_tensor @0xec840` | HIGH |
| `nrt_async_sendrecv_recv_tensor` | `0x7ee00` | `@@NRT_2.0.0` | `enc_async_sr_recv_tensor @0xec820` | HIGH |
| `nrt_async_sendrecv_test_request` | `0x7ee10` | `@@NRT_2.0.0` | `enc_async_sr_test_request @0xec860` | HIGH |
| `nrt_async_sendrecv_flush` | `0x7ee20` | `@@NRT_2.0.0` | `enc_async_sr_flush @0xebd10` | HIGH |

### Host-driven collective ops (`@@NRT_2.0.0`)

`nrt_barrier` and `nrt_all_gather` are not pure thunks — they carry one collective-specific step before delegating — but they are part of the same thin public surface, init-state-guarded like every other entry. §3 documents them.

| Public symbol | Addr | Version | Forwards to (symbol @ addr) | Confidence |
|---|---|---|---|---|
| `nrt_barrier` | `0x7ee30` | `@@NRT_2.0.0` | lazy barrier-NEFF build → `nrt_execute` (model) → `nrt_tensor_read` | HIGH |
| `nrt_all_gather` | `0x7f2d0` | `@@NRT_2.0.0` | local slice `memcpy` → `enc_all_gather(nec_dev, ctx0, out, size)` | HIGH |

> **QUIRK —** the version node is not cosmetic. `readelf -sW libnrt.so` shows the `nrta_*` eight as `…@@NRT_3.0.0` and the entire sendrecv/barrier/all-gather surface as `…@@NRT_2.0.0`. A consumer compiled and linked against a `NRT_2.0.0`-only `.so` resolves the sendrecv and host-collective symbols but gets an *unresolved symbol* on any `nrta_*`. The presence of the `NRT_3.0.0` node — equivalently, the existence of `nrta_execute_schedule @0x7bb00` — is the runtime ABI's own marker that explicit-async scheduling is available. Do not assign the `nrta_*` family to `NRT_2.0.0`; a reimplementation that exports them under the wrong node breaks version-script-based loaders.

> **GOTCHA —** the explicit-async producers and the *implicit*-async exec path are mutually exclusive at runtime, not just conceptually. Every `nrta_*` body re-reads `gconf.implicit_async_mode` (the flag `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS` sets, consumed by [exec/submit-path](../exec/submit-path.md)) and bails with the verbatim error *"Explicit async API cannot be used in implicit async mode (where NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS is set)."* before touching any queue. A reimplementation that lets a caller mix `nrta_tensor_copy` with an implicit-async `nrt_execute` will corrupt the shared XU/`async_exec_workers` state; the interlock exists precisely to forbid that, and it lives in the thunk, not the engine.

---

## 2. The Thunk Shape

### Purpose

Two shapes cover the entire sendrecv + explicit-async surface, and naming them once lets the rest of the table be read mechanically. The sendrecv family is a **degenerate thunk** — a single tail-`jmp` that preserves the C calling convention untouched, so the public symbol and the engine body share one ABI with zero marshalling. The `nrta_*` family is a **guarded producer thunk** — it does real shell work (lifecycle gate, implicit-async interlock, argument validation, virtual-core resolution) but no operation logic; the operation is a request record pushed onto an XU `std::deque`, and the body returns a `nrta_seq_t` the caller polls later. A reimplementer writes each shape once and instantiates it per row.

### Algorithm — degenerate sendrecv thunk

```c
// THE SENDRECV THUNK SHAPE — modeled from the one-instruction forward shared by
// nrt_async_sendrecv_* (0x7ed80..0x7ee20). Shown once; §1's table names the per-row
// enc_async_sr_* target. Six of the eleven (0x7ed80..0x7edd0) are literal `jmp`s.
NRT_STATUS nrt_async_sendrecv_<op>(/* api args, unchanged */):
    // No marshalling, no guard, no frame: the public symbol's only body is a tail-call.
    // The calling convention is identical, so args pass through registers verbatim.
    jmp enc_async_sr_<op>            // tail-call into the collectives engine (boundary)
    //   ↑ the engine, not the thunk, runs the per-LNC init/connected/bounds guards
    //     (see collectives/send-recv.md §1–§5). The thunk's sole contribution is the
    //     *typed* public symbol name + prototype (see §4).
```

> **NOTE —** because the forward is a tail-call with an unchanged frame, the engine body is what runs every guard — the per-LNC `async_sr_init_mutex[lnc]`, the `CONNECTED`-state check, the request-freelist bound. The thunk adds *no* lifecycle gate of its own; a SEND/RECV op on an uninitialized runtime is rejected inside `enc_async_sr_*`, not at `nrt_async_sendrecv_*`. This is the one place the public surface does *not* run the four-state `nrt_init_state` guard at the boundary — it is pushed one frame down into the engine.

### Algorithm — guarded `nrta_*` producer thunk

```c
// THE EXPLICIT-ASYNC PRODUCER SHAPE — modeled from the shared body of the nrta_*
// schedule family (nrt_async.cpp). Shown once; §1's table names the per-row XU array
// and request record. State word nrt_init_state @0xc5d1a0. Returns NRT_STATUS; the
// 64-bit nrta_seq_t is written to the caller's *req_sequence out-parameter.
NRT_STATUS nrta_<op>(/* typed args */, NRT_STATUS* exec_ret, nrta_seq_t* req_sequence):
    NlogErrorContextManager ctx("nrta_<op>")            // RAII name tag → "Generic API Failure" on exit
    nlog_coalescing_init_thread()

    // (1) LIFECYCLE GATE — the shared four-state guard (api-lifecycle.md §3)
    switch nrt_init_state:                              // @0xc5d1a0
      case NRT_STATE_INIT:   break                      // 1 — the only working state
      case NRT_STATE_CLOSED: log_warn("NRT already closed");  return NRT_CLOSED        // 14
      case NRT_STATE_START:  log_err("NRT uninitialized");    return NRT_UNINITIALIZED // 13
      default:               log_err("Incompatible runtime state: %s",
                                     nrt_state_get_string()); return NRT_FAILURE       // 1

    // (2) EXPLICIT/IMPLICIT ASYNC INTERLOCK — the family-wide reject (see §1 GOTCHA)
    if gconf.implicit_async_mode:
        log_err("Explicit async API cannot be used in implicit async mode "
                "(where NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS is set). ")
        return NRT_FAILURE                              // 1

    // (3) ARG VALIDATION — per-op; emits "%s is NULL" / the per-op type & queue checks
    if any required pointer == NULL: log_err("%s is NULL", arg_name); return NRT_INVALID  // 2
    if queue >= xu_queue_count(xu_type):               // Compute=1, Collectives=3, TensorOp=2
        log_err("Invalid queue parameter for %s XU. XU only supports %lu queues. (queue=%d)")
        return NRT_INVALID
    //  tensor ops additionally: both DMA(type 2), same device, same HBM index (nrta_tensor_copy)

    // (4) RESOLVE the target XU and (lazily) its worker
    vcore = vtpb_get_virtual_core(lnc)                 // 0x313fb0 — "Failed to find logical core (lnc_idx=%u)…"
    xu    = &<xu_array>[vcore.vtpb_idx][queue]          // ccschedxu / ccprepxu / tensoropxu (§1)
    lock(xu.common.mutex)
    if !xu.common.inited: init_xu_common(&xu.common)    // 0x7b100 — sem_init + pthread_create(thr_*)

    // (5) ASSIGN sequence, PUSH the typed request record, WAKE the worker
    seq = ++xu.common.next_seq                          // nrta_seq_t: lnc<<56 | xu_type<<52 | counter
    req = { .common = { .seq = seq, .ret = exec_ret }, /* per-op union: io_req / cc_*_req */ }
    xu.req_deque.push_back(req)                         // engine: exec/submit-path.md
    unlock(xu.common.mutex)
    sem_post(&xu.common.sem)                            // the worker drains, runs the op, bumps completed_seq
    *req_sequence = seq                                 // caller later polls via nrta_is_completed(seq)
    return NRT_SUCCESS
```

> **QUIRK —** the `nrta_*` producer returns `NRT_SUCCESS` the instant the request is *enqueued*, not when the operation completes — `*exec_ret` is the slot the worker will later write the real status into, and `*req_sequence` is the handle to poll. This is the explicit-async contract: the public call is a queue push, and completion is a *separate* `nrta_is_completed(seq, &done)` / `nrta_get_sequence(...)` call against the same XU's `completed_seq`. A reimplementer who treats the `NRT_SUCCESS` return as "the copy/schedule finished" has collapsed the async boundary; the only thing that finished is the enqueue.

> **GOTCHA —** the lazy `init_xu_common` inside the producer means the *first* `nrta_*` call on a given `(vtpb, xu_type, queue)` slot pays the worker-thread spawn cost (`sem_init` + `pthread_create`) under the XU mutex, and every later call does not. A reimplementation that pre-spawns all `128 × xu_type × queue` workers at init wastes threads on slots never used; one that forgets the lazy-init guard double-spawns. The `inited` flag at `xu_common_t +80` is the one bit that gates this — check it under the lock, set it after the spawn.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nrta_execute_schedule` | `0x7bb00` | Compute-XU schedule producer (`queue==0` only) | HIGH |
| `nrta_cc_schedule` | `0x7bd80` | Collectives-schedule producer; clears `*cc_ctx` on success | HIGH |
| `nrta_cc_prepare` | `0x7c980` | Collectives-prepare producer; allocates `nrt_cc_context_t(0x58)` | HIGH |
| `nrta_tensor_copy` / `_write` / `_read` | `0x7d8c0` / `0x7df10` / `0x7e440` | Tensor-op producers (`io_req` COPY/WRITE/READ) | HIGH |
| `nrta_get_sequence` / `nrta_is_completed` | `0x7c450` / `0x7c720` | Completion pollers over XU `completed_seq` | HIGH |
| `init_xu_common` | `0x7b100` | Lazy per-XU worker spawn (boundary, [exec/submit-path](../exec/submit-path.md)) | HIGH |
| `vtpb_get_virtual_core` | `0x313fb0` | LNC → virtual-core resolve (boundary) | HIGH |

### Considerations

`nrta_execute_schedule` is the one `nrta_*` entry that does *not* push onto a shared XU `std::deque` — its `queue` argument must be `0` (the Compute XU has a single queue) and it forwards straight to `kmgr_tpb_xu_schedule @0xe01d0` on the model's compute XU, after an optional `nrt_profile_session_add_node_info`. `nrta_cc_schedule` clears the caller's `*cc_ctx` to `0` on a successful push, transferring ownership of the prepared context into the queued `cc_sched_req`; a reimplementation that leaves the caller's pointer live invites a double-consume. The `nrta_seq_t` bit-layout (`[63:56]` lnc/vtpb, `[55:52]` `xu_type | (queue<<4)`, `[51:0]` monotonic counter) is what lets `nrta_is_completed` decode *which* XU a bare 64-bit handle belongs to without a side table — `nrta_is_completed` reads the high bits, finds the core via `vtpb_get_virtual_core_by_id @0x314050`, then compares that XU's `completed_seq >= seq`. The three XU arrays are torn down at unload by three `__cxa_atexit`-registered static destructors (`__tcf_0/1/2 @0x7d770/0x7d7e0/0x7d850`), each running `~_Deque_base<req>` plus the per-XU sequence→index `_Rb_tree` erase.

---

## 3. The Host-Collective Ops

### Purpose

`nrt_barrier` and `nrt_all_gather` are the two `@@NRT_2.0.0` collective entries that run **on the host**, not as an on-device `cc_op` stream. They share the thin-surface shape — the four-state lifecycle guard at the boundary — but each carries exactly one collective-specific step before delegating, which is why they are pseudocode here rather than table rows. `nrt_barrier` materializes a per-LNC "barrier model" (a tensor-set-backed NEFF collective) lazily on first call and executes it; `nrt_all_gather` copies this rank's slice into the output buffer locally and then runs a host bootstrap-socket all-gather. Both read the root-comm id from the parsed env config and log it on the way out.

### Algorithm — `nrt_barrier`

```c
// Models nrt_barrier @0x7ee30 (export @@NRT_2.0.0; src nrt/nrt_barrier.cpp).
// bs[] is a global per-LNC barrier_state_t array, indexed by vnc (MAX_VIRTUAL_TPB=256).
NRT_STATUS nrt_barrier(int32 vnc, uint32 g_device_id, uint32 g_device_count):
    log_dbg("API:IN: (lnc=%u, gid=%u, count=%u)", vnc, g_device_id, g_device_count)
    GUARD nrt_init_state == NRT_STATE_INIT             // else UNINITIALIZED(13)/CLOSED(14)/FAILURE(1)
    assert(vnc < MAX_VIRTUAL_TPB)                      // nrt_barrier.cpp:0x14E

    if bs[vnc].failed_to_init:                         // sticky disable from a prior failed build
        return error                                   // "The barrier initialization has failed…"

    if bs[vnc].model == NULL:                          // FIRST CALL — lazily build the barrier NEFF
        log_info("Loading barrier model for LNC: %d", vnc)
        nrt_load_collectives(...)                      // materialize the barrier model
        nrt_allocate_tensor_set(&bs[vnc].in_set,  2733)   // NEFF tensor count 2733 = 0xAAD
        nrt_allocate_tensor_set(&bs[vnc].out_set, 2733)
        nrt_tensor_allocate(input)                     // 128-byte payload
        buf = calloc(128); nrt_tensor_write(input, buf, 0, 128); free(buf)
        nrt_tensor_allocate(&bs[vnc].output)
        nrt_add_tensor_to_tensor_set(bs[vnc].in_set,  "input")
        nrt_add_tensor_to_tensor_set(bs[vnc].out_set, "output")
        if any step failed:
            bs[vnc].failed_to_init = true              // disable LNC's barrier permanently
            log_err("The barrier initialization has failed. … LNC:%d, worker: %u/%u")
            return error

    if nrt_execute(bs[vnc].model, bs[vnc].in_set, bs[vnc].out_set) != SUCCESS:
        log_err("The barrier execution has failed on LNC: %d, worker: %u/%u"); return error
    if nrt_tensor_read(bs[vnc].output, out1, 0, ...) != SUCCESS:
        log_err("Failed to read barrier output on LNC: %d, worker: %u/%u"); return error

    log_dbg("API:OUT: (lnc=%u) ret=%u root_comm_id=%s", vnc, ret, nrt_config_0.cc_root_comm_id)
    return NRT_SUCCESS
```

### Algorithm — `nrt_all_gather`

```c
// Models nrt_all_gather @0x7f2d0 (export @@NRT_2.0.0; src nrt/nrt_collectives.cpp).
NRT_STATUS nrt_all_gather(int32 vnc, uint32 gid, uint32 g_count,
                          uint32 rank_input_size, void* in, void* out):
    log_dbg("API:IN: (vnc=%u, gid=%u, g_count=%u)", vnc, gid, g_count)
    GUARD nrt_init_state == NRT_STATE_INIT             // four-state guard

    vcore = vtpb_get_virtual_core(vnc)                 // else "Failed to find core %u"
    if nrt_build_global_comm(vnc, gid, g_count) != SUCCESS:
        log_err("Failed to build global comm (nec_dev %u, gid=%u, g_count=%u)…"); return error

    memset(out, 0, rank_input_size * g_count)          // zero the whole gather buffer
    memcpy(out + rank_input_size * gid, in, rank_input_size)   // place THIS rank's slice locally

    if enc_all_gather(vcore.nec_dev_id, /*ctx_id=*/0, out, rank_input_size) != SUCCESS:
        log_err("Call to enc_all_gather failed (nec_dev %u, gid=%u, g_count=%u)"); return error

    log_dbg("API:OUT: (vnc=%u) ret=%u root_comm_id=%s", vnc, ret, nrt_config_0.cc_root_comm_id)
    return NRT_SUCCESS
```

> **QUIRK —** `nrt_barrier` builds its model **lazily and per-LNC**, and a build failure is *sticky*: `bs[vnc].failed_to_init` is latched true so the LNC's barrier is permanently disabled for the process lifetime — every subsequent `nrt_barrier(vnc, …)` returns the init-failure error without retrying. A reimplementation that retries the model build on each call will hammer a failing path; the binary decides once. The barrier NEFF is a fixed 2733-tensor (`0xAAD`) construction with a 128-byte payload — the meaning of those tensor sets is the collective NEFF itself (MEDIUM: the constant is verified, the NEFF's internal semantics are owned by the load pipeline, not this surface).

> **NOTE —** `nrt_all_gather` writes this rank's contribution into `out` with a **local `memcpy` before** the network all-gather, and `enc_all_gather` is called with `ctx_id = 0` (the NEFF-loaded-comm context, distinct from the `ctx_id = 1` standalone-user-comm path that `nrt_cc_global_comm_init` uses). The host all-gather runs over the bootstrap socket, not the device DMA fabric; a reimplementer must place this rank's slice at `out + rank_input_size * gid` *first*, because the transport fills only the *other* ranks' slots.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nrt_barrier` | `0x7ee30` | Lazy per-LNC barrier-NEFF build + execute + read | HIGH |
| `nrt_all_gather` | `0x7f2d0` | Local slice `memcpy` + host bootstrap all-gather | HIGH |
| `nrt_build_global_comm` | — | Build/cache the global communicator (boundary, collectives) | HIGH |
| `enc_all_gather` | — | Host bootstrap-socket all-gather (boundary, collectives) | HIGH |
| `nrt_load_collectives` | — | Materialize the barrier model NEFF (boundary, load pipeline) | MEDIUM |

---

## 4. Reconstructed ABI Signatures

### Purpose

These are the typed C prototypes recovered from the thunk and producer bodies — argument count, order, types, and the out-parameter convention. They are the page's deliverable: the engine bodies these forward into are reached internally through function pointers, XU dispatch, and `dlsym`'d wrappers and operate on untyped request records, so **the public prototype survives only at the thunk.** A reimplementer who wants the engine's behavior reads the boundary pages; one who wants the *callable contract* reads here.

```c
// ── EXPLICIT-ASYNC SCHEDULE FAMILY (@@NRT_3.0.0; nrt/nrt_async.cpp) ──
// Every entry returns NRT_STATUS on enqueue and writes a pollable nrta_seq_t.
NRT_STATUS nrta_execute_schedule(nrt_model_t* model, nrt_tensor_set_t* in,
                                 nrt_tensor_set_t* out, uint32_t queue /* ==0 */,
                                 NRT_STATUS* exec_ret, nrta_seq_t* req_seq);            // 0x7bb00
NRT_STATUS nrta_cc_schedule(nrt_cc_context_t** cc_ctx, uint32_t queue /* <=2 */,
                            void* _reserved, nrta_seq_t* req_seq);                      // 0x7bd80
NRT_STATUS nrta_cc_prepare(nrt_cc_comm_t* comm, nrt_tensor_list_t* in_list,
                           nrt_tensor_list_t* out_list, nrt_dtype_t dtype,
                           nrt_op_type_t op, nrt_cc_op_type_t cc_op,
                           nrt_cc_context_t** out_ctx);                                 // 0x7c980
NRT_STATUS nrta_get_sequence(uint32_t lnc, nrta_xu_t xu_type, uint32_t queue,
                             nrta_seq_t* seq);                                          // 0x7c450
NRT_STATUS nrta_is_completed(nrta_seq_t seq, bool* completed);                         // 0x7c720
NRT_STATUS nrta_tensor_copy(nrt_tensor_t* src, uint64_t src_off,
                            nrt_tensor_t* dst, uint64_t dst_off, uint64_t size,
                            uint32_t lnc, uint32_t queue /* <=1 */,
                            NRT_STATUS* exec_ret, nrta_seq_t* req_seq);                 // 0x7d8c0
NRT_STATUS nrta_tensor_write(nrt_tensor_t* tensor, void* buf, uint64_t off, uint64_t size,
                             uint32_t lnc, uint32_t queue, NRT_STATUS* exec_ret,
                             nrta_seq_t* req_seq);                                      // 0x7df10
NRT_STATUS nrta_tensor_read(void* buf, nrt_tensor_t* tensor, uint64_t off, uint64_t size,
                            uint32_t lnc, uint32_t queue, NRT_STATUS* exec_ret,
                            nrta_seq_t* req_seq);                                       // 0x7e440

// ── POINT-TO-POINT SENDRECV THUNKS (@@NRT_2.0.0; → enc_async_sr_*) ──
NRT_STATUS nrt_async_sendrecv_get_max_num_communicators_per_lnc(int* out);             // 0x7ed80
NRT_STATUS nrt_async_sendrecv_get_max_num_pending_request(int* out);                   // 0x7ed90
NRT_STATUS nrt_async_sendrecv_init(int local_host_lnc);                                // 0x7eda0
NRT_STATUS nrt_async_sendrecv_close(int local_host_lnc);                               // 0x7edb0
NRT_STATUS nrt_async_sendrecv_connect(const char* peer_ip, int peer_lnc, int local_lnc,
                                      nrt_async_sendrecv_comm** send_comm);            // 0x7edc0
NRT_STATUS nrt_async_sendrecv_accept(const char* peer_ip, int peer_lnc, int local_lnc,
                                     nrt_async_sendrecv_comm** recv_comm);             // 0x7edd0
NRT_STATUS nrt_async_sendrecv_test_comm(nrt_async_sendrecv_comm* comm, bool* done);    // 0x7ede0
NRT_STATUS nrt_async_sendrecv_send_tensor(nrt_tensor_t* tensor, size_t off, size_t len,
                                          nrt_async_sendrecv_comm* send_comm,
                                          nrt_async_sendrecv_request** req);            // 0x7edf0
NRT_STATUS nrt_async_sendrecv_recv_tensor(nrt_tensor_t* tensor, size_t off, size_t len,
                                          nrt_async_sendrecv_comm* recv_comm,
                                          nrt_async_sendrecv_request** req);            // 0x7ee00
NRT_STATUS nrt_async_sendrecv_test_request(nrt_async_sendrecv_request* req, bool* done,
                                           size_t* size);                               // 0x7ee10
NRT_STATUS nrt_async_sendrecv_flush(int lnc);                                          // 0x7ee20

// ── HOST-DRIVEN COLLECTIVE OPS (@@NRT_2.0.0) ──
NRT_STATUS nrt_barrier(int32_t vnc, uint32_t g_device_id, uint32_t g_device_count);     // 0x7ee30
NRT_STATUS nrt_all_gather(int32_t vnc, uint32_t gid, uint32_t g_count,
                          uint32_t rank_input_size, void* in, void* out);              // 0x7f2d0
```

> **NOTE —** these prototypes are reconstructed from the thunk/producer bodies, and that is the **only** place they exist in typed form. Internally the engine handles requests as untyped records — the `io_req` (64-byte) and `cc_*_req` for the `nrta_*` path, the opaque `nrt_async_sendrecv_comm*` / `nrt_async_sendrecv_request*` for the sendrecv path — reached through XU dispatch and function pointers, with no per-call C signature to recover downstream. If the thunk layer is regenerated or stripped, the public ABI must be reconstructed from *this page*, not from the engine. The `nrta_*` out-parameter convention is uniform: `exec_ret` receives the worker's eventual status, `req_seq` receives the pollable `nrta_seq_t` — both written *after* a successful enqueue, neither valid on a non-`SUCCESS` return.

> **CORRECTION (L-API-03) —** an earlier `SCAN-01` seed placed the dmabuf-export gate fields at `gconf.enable_p2p@+112` / `funtime@+176`; the disassembly of the surrounding tensor-surface bodies reads them at `+0x74` / `+0xB8`. Those offsets belong to the tensor page, not this surface, but the same `gconf` global is read here for the `implicit_async_mode` interlock — trust the disassembly-grounded offsets when reconciling `nrt_global_config_t`. (The full layout is owned by [config-structs](config-structs.md).)

> **GOTCHA —** `nrta_cc_schedule`'s third argument is a reserved/unused `void*` in this build, and the second argument `queue` is bounded `<= 2` (the Collectives XU has three queues), while `nrta_tensor_copy/write/read` bound `queue <= 1` (the Tensor-Op XU has two) and `nrta_execute_schedule` requires `queue == 0` (the Compute XU has one). A single reimplementation that hard-codes one queue cap across the family rejects valid collective schedules or accepts invalid tensor-op ones; the cap is per-XU-type — `Compute=1`, `Tensor Op=2`, `Collectives=3` — and the error string names which.

---

## Cross-References

- [Async Send / Recv (Point-to-Point)](../collectives/send-recv.md) — the `enc_async_sr_*` engine every sendrecv thunk forwards into: the OFI rendezvous, the connect/accept state machine, the KV store, the per-LNC service thread
- [The Submit Path: Bind → Stage → Doorbell](../exec/submit-path.md) — the XU work-queue producer/consumer, `async_exec_workers`, and the implicit-async path the `nrta_*` interlock excludes
- [Collectives Overview](../collectives/overview.md) — where host-driven `nrt_barrier`/`nrt_all_gather` and the host-CC path sit among the on-device `cc_op` collectives
- [Public C API: Lifecycle and Init/Teardown](api-lifecycle.md) — the four-state `nrt_init_state` guard every entry here runs, and the `NRT_UNINITIALIZED(13)`/`NRT_CLOSED(14)`/`NRT_FAILURE(1)` returns it emits
- [Public C API: Tensor Surface and I/O](api-tensors.md) — the synchronous tensor entries the `nrta_tensor_*` producers are the explicit-async mirror of, and the `nrt_tensor_t` object model both operate on
- [Configuration: nrt_config and nrt_global_config](config-structs.md) — `gconf.implicit_async_mode`, `nrt_config_0.cc_root_comm_id`, and the `nrt_global_config_t` fields this surface reads
