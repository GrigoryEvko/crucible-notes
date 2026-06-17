# The Completion Engine: NQ Harvest and Error Decode

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64 x86-64, not stripped, DWARF present). `.text` VMA == file offset, so every `0x…` is an analysis VMA verifiable against `nm -n libnrt.so`. Other versions will differ.*
> *Evidence grade: **Confirmed (decompiled + struct-anchored)** — the state machine, the 16-byte notification-entry decode, the timeout/overflow disambiguation, and the per-engine sub-state were read from per-function decompilation (`exec_request_progress_one_step` @`0x263330`, 8847 bytes / 1832 insns) and cross-checked against the IDA `structures.json`/`enums.json`/`callgraph.json`. · Part VII — Execution Engine · [back to index](../index.md)*

## Abstract

The [submit path](submit-path.md) ends at a blocking `read(mark_efd)`; everything after it — learning the inference finished, decoding when and how each engine completed, and surfacing hardware or numerical errors — is this page. The shape will be familiar to anyone who has polled a GPU completion ring instead of taking an interrupt: the device sequencer writes 16-byte completion records into per-engine **Notification Queue (NQ)** rings that are mmap'd into the runtime's address space, and the host **polls** those rings until every engine has reported both a *start* and an *end* record. There is **no interrupt** on the completion path. Completion is notification-driven, not kernel-signalled — the host learns of completion only by reading device memory.

The harvest is a single per-request state machine, `exec_request_progress_one_step` @`0x263330`, walking `INIT → WAIT_BARRIER_PROXY → WAIT_CORE → DONE`. It is a *one-step* function: each call advances the machine as far as it can without blocking and returns `1` ("not done, call me again"), `0` ("done, success"), or `2` ("done, error already written"). Two drivers spin it. The synchronous driver `exec_wait_round_robin` @`0x2655c0` busy-polls it with `usleep(1)` between calls — this is the inner half of `kmgr_exec_wait` that the submit-path's `read(mark_efd)` is ultimately waiting on. The asynchronous XU worker `tpb_xu_step` @`0xe8940` drives the *same* state machine from a worker thread, and on `DONE` writes the per-execution `mark_comp_efd` that unblocks the synchronous caller via `tpb_xu_base_report_complete` @`0xe8800`.

In `WAIT_CORE` the machine sweeps every `(pcore, engine)` pair — up to 2 NeuronCores × 5 TPB engines (PE/ACT/POOL/DVE/SP) — calling `notification_read_exec_queue` @`0x2ff170` to drain that engine's `INFER_STATUS` NQ ring, decoding each 16-byte `notification_entry_t`, and advancing a per-engine sub-state `START_PENDING → END_PENDING → COMPLETE`. When all engines reach `COMPLETE` the machine post-processes timestamps and transitions to `DONE`. If an engine is still pending when `elapsed_ms ≥ core.timeout_ms`, the machine probes INTC cause **bit 29** (`exec_check_intc_sw_notif_queue_overflow` @`0x2613c0`) to decide whether the stall was a genuine timeout (`NRT_TIMEOUT=5`) or a notification-queue overflow that ate the completion record (`NRT_EXEC_SW_NQ_OVERFLOW=1204`).

For reimplementation, the contract is:

- **The four-state machine** and its `{1,0,2}` return discipline — the harvest is poll-and-retry, never block-inside.
- **The 16-byte `notification_entry_t` decode** — which byte holds the NQ-id tag, the debug hint, the custom-notification sub-id, the START/END/CC subtype, and the device timestamp.
- **The per-engine `START_PENDING → END_PENDING → COMPLETE` matcher** — what a `START`/`END`/`CC` record does to the sub-state, and the assert that a state regression is an overflow.
- **The timeout vs overflow disambiguation** — `elapsed_ms ≥ timeout_ms` then INTC cause bit 29, yielding `5` or `1204`.
- **The error-classification map** — `NRT_TIMEOUT`, `NRT_EXEC_SW_NQ_OVERFLOW`, and the `notification_consume_error_block` subtypes (`TRANSIENT → 1004`, `NUMERICAL → 1003`, uncorrectable ECC → `1202`).

| | |
|---|---|
| **Harvest state machine** | `exec_request_progress_one_step` @`0x263330` (8847 B, 1832 insns) |
| **Sync driver** | `exec_wait_round_robin` @`0x2655c0` ← `kbl_infer_exec_wait` ← `kmgr_exec_wait` |
| **Async driver** | `tpb_xu_step` @`0xe8940` ← `kmgr_xu_worker_do_work` |
| **Request state** | `exec_request_state_t` — **472 bytes** (`0x1D8`), `[2]` cores |
| **NQ entry** | `notification_entry_t` — **16 bytes**, decoded positionally |
| **NQ read** | `notification_read_exec_queue` @`0x2ff170` → `aws_hal_notific_nq_read` |
| **Timeout probe** | `exec_check_intc_sw_notif_queue_overflow` @`0x2613c0` — INTC cause **bit 29** |
| **Error decode** | `exec_request_process_errors` @`0x2615b0` → `notification_consume_error_block` @`0x2ff250` |
| **Completion signal** | `tpb_xu_base_report_complete` @`0xe8800` — pops slot, signals `mark_comp_efd` |
| **INFER_STATUS NQ ids** | `{4,5,6,7,8}` (PE/ACT/POOL/DVE/SP); ERROR=NQ#2 |

> **QUIRK —** completion is **notification-driven, not interrupt-driven**. The device sequencer DMA-writes 16-byte records into the per-engine `INFER_STATUS` NQ rings, and the host *reads* them with `aws_hal_notific_nq_read`. A reimplementer must not wait on an IRQ, an eventfd from the kernel, or a `poll()` on the device fd for completion — there is none on this path. The only host-side blocking primitive is `usleep(1)` (sync) or the worker thread's own scheduling (async); the `mark_comp_efd` the submit path blocks on is written by *userspace* (the worker), not by the kernel.

---

## 1. The Harvest State Machine

### Purpose

`exec_request_progress_one_step` (`0x263330`) is the entire completion harvest. It owns one `exec_request_state_t` (472 B), advances it by one non-blocking step, and reports whether the caller should poll again. The first call (`state == INIT`) arms the barrier-proxy wait; subsequent calls drain the collectives barrier, then the per-engine NQ rings, then post-process and classify. It is deliberately re-entrant and idempotent within a state: calling it on a `WAIT_CORE` request that has new NQ records advances the matcher; calling it with no new records returns `1` and changes nothing.

### Entry Point

```text
kmgr_exec_wait                                   ── the sync completion-wait inner half
  └─ kbl_infer_exec_wait                          ── 0x307410
       └─ exec_wait_round_robin (0x2655c0)        ── busy-poll: while step()==1 usleep(1)
            ├─ exec_request_init_state (0x260c20)
            ├─ exec_request_progress_one_step (0x263330)   ── THE state machine
            └─ exec_request_cleanup_state (0x260bf0)

kmgr_xu_worker_do_work                            ── async worker thread loop
  └─ tpb_xu_step (0xe8940)                         ── drives the SAME state machine
       ├─ exec_request_init_state (first call only)
       ├─ exec_request_progress_one_step (0x263330)
       └─ tpb_xu_base_report_complete (0xe8800)    ── on DONE: signal mark_comp_efd
```

Both callers are the only two in-edges (`callgraph.json`: `to == exec_request_progress_one_step` ⇒ `{exec_wait_round_robin, tpb_xu_step}`). The synchronous driver is trivial:

```c
NRT_STATUS exec_wait_round_robin(h_model, vcore, tpb_count, inference_id,    // 0x2655c0
                                 fmap_output_set, out_info, sync_point_cpu_ts):
    exec_request_state_t st;
    memset(&st, 0, 472)                                    // st.state = INIT = 0
    exec_request_init_state(&st, vcore, h_model, tpb_count, …)   // 0x260c20: resolve mla/tpb, ref models
    exec_ret = NRT_SUCCESS
    while exec_request_progress_one_step(&st, &exec_ret) == 1:   // <== poll loop
        usleep(1)                                          // yield 1us, no kernel wait
    exec_request_cleanup_state(&st)                         // 0x260bf0: model_ref_decrement
    return exec_ret
```

### Algorithm

The machine branches on `state->state` (`exec_state_t`: `INIT=0, WAIT_BARRIER_PROXY=1, WAIT_CORE=2, DONE=3`, from `enums.json`). The `INIT` case is the decompiler's `else` (default) branch; the published source orders the cases `INIT → WAIT_BARRIER_PROXY → WAIT_CORE`.

```c
int exec_request_progress_one_step(exec_request_state_t *st, NRT_STATUS *out):   // 0x263330

    // ---- state INIT (default branch, decompile lines 1238-1244) ----
    if st->state == EXEC_STATE_INIT:
        st->state = EXEC_STATE_WAIT_BARRIER_PROXY
        st->barrier_proxy.timeout_thresh = 120              // seconds; doubles on each warn
        clock_gettime(CLOCK_REALTIME, &st->barrier_proxy.start_ts)
        return 1                                            // not done; poll again

    // ---- state WAIT_BARRIER_PROXY (lines 242-348) ----
    if st->state == EXEC_STATE_WAIT_BARRIER_PROXY:
        for pcore in 0 .. tpb_count-1:
            if !st->barrier_proxy.proxy_done[pcore]:
                rc = enc_check_proxy_barrier_task_status(&mods[pcore]->cc_ctx,    // collectives barrier
                                                         &st->barrier_proxy.proxy_done[pcore])
                if rc: st->exec_fatal_status[pcore][5] = 1; goto FINALIZE(rc)   // [5] = barrier-proxy
        if any proxy_done[pcore] still false:
            clock_gettime(CLOCK_REALTIME, &now)
            if get_timespec_delta(&now, &start_ts) >= timeout_thresh:           // seconds
                warn("Timeout exceeded: Waiting on barrier proxy task: %u sec")
                st->barrier_proxy.timeout_thresh *= 2                            // back off, keep waiting
            return 1                                                            // still waiting

        // all barriers cleared -> transition to WAIT_CORE
        expected_engines = al_hal_tpb_get_tpb_eng_count()                       // 5 on sunda
        assert st->state == WAIT_BARRIER_PROXY                                  // exec.c:0x599
        st->state = EXEC_STATE_WAIT_CORE
        notifications_get_nq_ids_for_type(INFER_STATUS, &core.infer_status_nqs, // -> {4,5,6,7,8}
                                          &core.infer_status_nq_cnt, &ens_type)
        assert core.infer_status_nq_cnt == expected_engines                     // exec.c:0x5A0
        for pcore in 0 .. tpb_count-1:
            for eng in 0 .. expected_engines-1:
                core.tpb_exec_state[pcore][eng] = TPB_ENG_STATE_START_PENDING   // 0
        *(u16*)core.sync_point_initialized = 0
        assert mods[0] != NULL                                                  // exec.c:0x5AC
        core.timeout_ms = 1000 * mods[0]->param.timeout                         // seconds -> ms
        clock_gettime(CLOCK_REALTIME, &core.start_ts)
        return 1

    // ---- state WAIT_CORE (lines 350-1234): the per-engine NQ harvest ----
    if st->state == EXEC_STATE_WAIT_CORE:
        for pcore in 0 .. tpb_count-1:
            if !core.network_proxy_done[pcore]:
                rc = enc_check_proxy_network_task_status(&mods[pcore]->cc_ctx,  // collectives net proxy
                                                         &core.network_proxy_done[pcore])
                if rc: st->exec_fatal_status[pcore][4] = 1; goto FINALIZE(rc)   // [4] = net-proxy
            consume_tpb_status_notifications_per_engine(st, pcore)              // see §2 / §3
            // ^ may FINALIZE on a matcher fault (overflow=1204, bad type, custom-notif error)

        // ---- end-of-sweep: completeness + timeout (lines 1178-1234) ----
        clock_gettime(CLOCK_REALTIME, &now)
        elapsed_ms = get_timespec_delta(&now, &core.start_ts) * 1000.0
        all_done = true; status = 0
        for pcore in 0 .. tpb_count-1:
            assert units_done[pcore] <= units_polled[pcore]                     // exec.c:0x5E8
            if units_done[pcore] < units_polled[pcore]:                         // an engine still pending
                all_done = false
                if elapsed_ms >= core.timeout_ms:
                    if exec_check_intc_sw_notif_queue_overflow(pcore):          // INTC bit 29
                        st->exec_fatal_status[pcore][2] = 1; status = 1204      // SW_NQ_OVERFLOW
                    else:
                        st->exec_fatal_status[pcore][1] = 1; status = 5         // NRT_TIMEOUT
        if status: goto FINALIZE(status)
        if all_done:
            assert st->state == WAIT_CORE                                       // exec.c:0x622
            st->state = EXEC_STATE_DONE
            goto FINALIZE(0)
        return 1                                                               // engines outstanding; poll again

    // ---- FINALIZE (postprocess + error sweep + status priority, lines 891-1119) ----
FINALIZE(status):
    exec_request_postprocess_exec(st)        // per-engine device->cpu timestamps, NDS metrics, GPSIMD stdio
    exec_request_process_errors(st)          // 0x2615b0 -> notification_consume_errors  (see §4)
    best = NRT_SUCCESS
    for pcore in 0 .. tpb_count-1:
        nds_increment_nc_counter(... exec_notif_duration, mac_count ...)
        if nrt_get_status_priority(exec_rets[pcore]) > priority(best):
            best = exec_rets[pcore]                                            // highest-priority error wins
        out_info->exec_rets[pcore]    = exec_rets[pcore]
        out_info->tpb_duration[pcore] = tpb_duration[pcore]
        out_info->err[pcore]          = infer_err[pcore]
    *out = best
    if status == 0: return 0                  // clean completion
    if best != 0:   return 2                  // done with error (already in *out)
    *out = NRT_FAILURE; return 2              // "failed but error not set" -> generic failure
```

> **GOTCHA —** the three return codes are a contract the drivers depend on. `1` means "I made progress (or none) but the request is still in flight — sleep and call me again." `0` means "complete, `*out == NRT_SUCCESS`." `2` means "complete with error, `*out` holds the highest-priority `NRT_STATUS`." A driver that treats any non-zero return as failure will abort a perfectly healthy in-flight inference, because the *normal* in-flight return is `1`. `exec_wait_round_robin` gets this right by testing `== 1` specifically; `tpb_xu_step` tests `== 2` for the degrade-to-`FATAL_SHUTDOWN` path and treats any other non-zero as "keep going."

> **QUIRK —** the `WAIT_BARRIER_PROXY` timeout never fails the request — it *doubles* `timeout_thresh` (120s → 240s → …) and keeps polling, only emitting a `WARN`. Collective barriers can legitimately wait minutes for peers across a pod. Only the `WAIT_CORE` timeout (`core.timeout_ms`, derived from the model's `param.timeout` in seconds) is fatal. A reimplementer who applies a single hard wall-clock to the whole harvest will spuriously time out multi-node collectives during their barrier phase.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `exec_request_progress_one_step` | `0x263330` | The 4-state harvest machine; `{1,0,2}` returns | HIGH |
| `exec_wait_round_robin` | `0x2655c0` | Sync busy-poll driver (`usleep(1)` loop) | HIGH |
| `exec_request_init_state` | `0x260c20` | Zero state; resolve `mla`/`tpb`; ref-bump models | HIGH |
| `exec_request_cleanup_state` | `0x260bf0` | `model_ref_decrement` on `mods[0..1]` | HIGH |
| `tpb_xu_step` | `0xe8940` | Async XU worker step; drives the machine; signals on DONE | HIGH |
| `tpb_xu_base_report_complete` | `0xe8800` | Pop SPSC slot; signal `mark_comp_efd` | HIGH |
| `notifications_get_nq_ids_for_type` | `0x2fd160` | `INFER_STATUS → {4,5,6,7,8}` NQ-id table | HIGH |
| `al_hal_tpb_get_tpb_eng_count` | (HAL) | Expected engine count (5 on sunda) | HIGH |
| `enc_check_proxy_barrier_task_status` | (enc) | Collectives barrier gate in `WAIT_BARRIER_PROXY` | HIGH |
| `enc_check_proxy_network_task_status` | (enc) | Collectives net-proxy gate in `WAIT_CORE` | HIGH |

> **NOTE —** `exec_request_init_state` (`0x260c20`) is called *once* per request, by whichever driver owns it. In the sync path `exec_wait_round_robin` allocates the 472-byte `exec_request_state_t` on its own stack and inits it inline; in the async path the worker inits `xu->current_exec_state` on the first `tpb_xu_step` for that request (guarded by `xu->current_exec_initialized`). Both then funnel through the identical `progress_one_step`.

---

## 2. The Per-Engine NQ Harvest

### Purpose

`WAIT_CORE` is where the device's completion records are actually consumed. For each bound `(pcore, engine)` the machine reads that engine's `INFER_STATUS` NQ ring, decodes every 16-byte entry it finds, and uses the entry's subtype to drive that engine's `START_PENDING → END_PENDING → COMPLETE` sub-state. The five TPB engines are PE, ACT, POOL, DVE, SP, ordered by the engine index `eng`; the `INFER_STATUS` notification type maps to NQ ids `{4,5,6,7,8}` — one ring per engine.

### Entry Point

```text
exec_request_progress_one_step (WAIT_CORE)
  └─ [per pcore] [per engine eng]
       └─ notification_read_exec_queue (0x2ff170)         ── direct NQ read, type=INFER_STATUS
            └─ aws_hal_notific_nq_read(&notif->ens_nq[nq.nq], 0, buf, 16, &written)   [HAL]
       └─ decode 16B notification_entry_t  (§3)
       └─ advance core.tpb_exec_state[pcore][eng]  START_PENDING -> END_PENDING -> COMPLETE
       └─ notification_drain (0x300170)                   ── on engine done, drain subscriber rings
```

`notification_read_exec_queue` is a 212-byte gate that only permits the four exec-status notification types (`INFER_STATUS=3` is read as the device-side `2..3` / `8..9` lane), then forwards to the HAL ring read:

```c
NRT_STATUS notification_read_exec_queue(notif, type, nq, buf, buf_size, *written):   // 0x2ff170
    *written = 0
    if (type - 2) > 1 && (type - 8) > 1:                  // only types 2,3,8,9 allowed
        error("Only exec status types support direct read. type=%u")
        return NRT_INVALID
    rc = aws_hal_notific_nq_read(&notif->ens_nq[nq.nq], 0, buf, buf_size, written)
    if rc: error("Failed to read ens %d"); return NRT_FAILURE
    return NRT_SUCCESS
```

### Algorithm

The per-engine consumer (the published source calls it `exec_consume_tpb_status_notifications_per_engine`, inlined into `0x263330`; recovered only via its `__assert_fail` function-name args) is a read-decode-match loop:

```c
// inlined: exec_consume_tpb_status_notifications_per_engine(st, pcore, eng)   // exec.c:0x302..0x30F asserts
for eng in 0 .. infer_status_nq_cnt-1:                    // 5 engines
    sub = &core.tpb_exec_state[pcore][eng]                // START_PENDING|END_PENDING|COMPLETE
    if *sub == TPB_ENG_STATE_COMPLETE:                    // engine already done; count and skip
        units_done[pcore]++; continue
    assert st->state == WAIT_CORE && *sub != COMPLETE     // exec.c:0x302
    nq = core.infer_status_nqs[eng]                       // NQ id from {4,5,6,7,8}
    notif = &tpbs[pcore]->notification
    assert mla && tpb && mod                              // exec.c:0x309
    assert core.infer_status_nqs != NULL && eng < infer_status_nq_cnt   // exec.c:0x30F

    for read_idx in 1 .. 16:                              // up to 16 entries per engine per sweep
        u8 data[16]; u32 written = 0
        rc = notification_read_exec_queue(notif, INFER_STATUS, nq, data, 16, &written)
        if rc:   FINALIZE_ENGINE_FAULT(rc, "Failed to check for infer_status notifications.")
        if written == 0: break                            // ring empty for this engine, this sweep

        decode_notification_entry(data)                   // §3: subtype, NQ-id tag, debug-hint, ts
        match_entry_to_substate(sub, data)                // below
        if *sub == COMPLETE: notification_drain(notif); break

// match_entry_to_substate: the START/END/CC state transitions  (switch on *(u16*)&data[6])
switch *(u16*)&data[6]:                                   // notification subtype
    case 2:  // START
        if *sub != START_PENDING:                         // regression -> overflow probe
            if exec_check_intc_sw_notif_queue_overflow(pcore):
                error("Expected state to be start pending but got %u [overflow detected]"); FAULT(1204)
            else error("Expected state to be start pending but got %u");  FAULT(generic)
        if vcore_notif_ts[pcore][eng_start] - 1 >= ts: vcore_notif_ts[pcore][eng_start] = ts   // earliest start
        ntrace_record_event(NC_TIMESTAMP_SYNC, ts, device_id, device_tpb_idx, 0)
        *sub = TPB_ENG_STATE_END_PENDING                  // START_PENDING(0) -> END_PENDING(1)
    case 3:  // END
        if *sub != END_PENDING:                           // missing START -> overflow probe
            if exec_check_intc_sw_notif_queue_overflow(pcore):
                error("Expected state to be end pending but got %u [overflow detected]")
                st->exec_fatal_status[pcore][3] = 1; FAULT(1204)
            else error("Expected state to be end pending but got %u");  FAULT(generic)
        if vcore_notif_ts[pcore][eng_end] < ts || !vcore_notif_ts[pcore][eng_end]:
            vcore_notif_ts[pcore][eng_end] = ts           // latest end
        *sub = TPB_ENG_STATE_COMPLETE                     // END_PENDING(1) -> COMPLETE(2)
    case 7:  // CC start (collectives sync)
        if vcore_notif_ts[pcore][cc_start] - 1 >= ts: vcore_notif_ts[pcore][cc_start] = ts
    case 8:  // CC end
        if vcore_notif_ts[pcore][cc_end] < ts || !vcore_notif_ts[pcore][cc_end]:
            vcore_notif_ts[pcore][cc_end] = ts
    case 6:  // CUSTOM (explicit): re-tag NQ-id on arch>2, then fall into custom-notif decode (§3.1)
    case 0,1,4,5: // unexpected on an infer-status ring
        error("Recieved unexpected notification on engine %u. Debug Hint: %u, Notification Type: %u"); FAULT
    default (>8): same "unexpected notification" fault
```

The `START`/`END` transitions are the matcher's core. An engine sub-state is monotone: `START_PENDING(0) → END_PENDING(1) → COMPLETE(2)`, and a record arriving out of order (an `END` before a `START`, or a second `START`) is treated as evidence that the NQ ring **overflowed** and dropped an entry — which is exactly why the matcher probes INTC bit 29 before deciding whether to fail with `1204` (overflow) or a generic error.

> **GOTCHA —** the matcher reads **up to 16 entries per engine per sweep** (the `v168 != 16` loop bound). If a ring has more than 16 pending records in one pass, the remainder is left for the next `progress_one_step` call. A reimplementation that drains the whole ring in one pass is *also* correct, but one that reads exactly one entry per sweep will be correct yet pathologically slow under bursty completions — the 16-entry batch is a throughput choice, not a correctness one.

> **QUIRK —** on arch > 2 (cayman), a `CUSTOM` (subtype 6) record on DVE (`eng == 2`) has its NQ-id tag forcibly rewritten to `0xE` (`data[3] = (data[3] & 0xE0) | 0xE`). This is a hardware-quirk fixup: the cayman DVE engine emits explicit notifications with a stale NQ-id field, and the runtime patches it before downstream decode. On sunda (arch == 2) the field is trusted as-is. A reimplementation targeting cayman that trusts `data[3] & 0x1F` verbatim on DVE custom records will mis-route them.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `notification_read_exec_queue` | `0x2ff170` | Direct NQ ring read, exec-status types only | HIGH |
| `aws_hal_notific_nq_read` | (HAL) | Dequeue raw 16-byte entries from one NQ ring | HIGH |
| `notification_drain` | `0x300170` | Drain subscribed NQ rings into subscriber buffers | HIGH |
| `exec_check_intc_sw_notif_queue_overflow` | `0x2613c0` | INTC cause bit-29 overflow probe (§5) | HIGH |
| `ntrace_record_event` | (trace) | Emit `NC_TIMESTAMP_SYNC` on first START | HIGH |
| `sync_point_update` | (tdrv) | Anchor device-ts↔cpu-ts on the sync point | HIGH |
| *(inlined)* `consume_tpb_status_notifications_per_engine` | — | Per-engine read-decode-match loop | HIGH |
| *(inlined)* `parse_infer_status_notification` | — | Per-engine device→cpu timestamp rescale | MEDIUM |

---

## 3. The 16-Byte Notification Entry

### Purpose

Every NQ ring entry is a fixed **16-byte** `notification_entry_t`. The completion path decodes it **positionally** — the runtime reads specific byte offsets rather than a named struct, because the full bit-field union (`sunda.tpb_error` / `sequencer_generic_error_metadata_hi` / `dma_*`) is owned by the error-decode lane. The positions below are read directly from the `WAIT_CORE` decompile (`data[2] >> 4`, `data[3] & 0x1F`, `*(u16*)&data[4]`, `*(u16*)&data[6]`, `*(u64*)&data[8]`).

### Encoding

```text
notification_entry_t — 16 bytes (one NQ ring slot)

 byte:  0   1   2   3   4   5   6   7   8   9  10  11  12  13  14  15
       └──────┬──────┘ └─┬─┘ └──┬──┘ └──┬──┘ └────────────┬────────────┘
       u16 isa-line-lo   │   custom-sub  subtype          device timestamp (u64)
                         │   *(u16)@4    *(u16)@6          *(u64)@8
              data[2]>>4 = debug-hint
              data[3]&0x1F = NQ-id tag (engine)
```

| Offset | Width | Field | Meaning | Confidence |
|---|---|---|---|---|
| `+0` | 2 | `isa_line_lo` | low 16 bits of the ISA instruction line (OOB-error custom notifs combine with `data[2]<<16`) | HIGH |
| `+2` (`>>4`) | nibble | `debug_hint` | `data[2] >> 4`; must be `0` (silent) or `1` (drain to subscriber). Any other value → "unexpected debug hint" fault | HIGH |
| `+3` (`&0x1F`) | 5 bits | `nq_id_tag` | engine NQ-id; on arch>2 DVE custom records forced to `0xE` | HIGH |
| `+4` | 2 | `custom_sub_id` | custom-notification subtype (the 11-way `switch` of §3.1), only read when `subtype` selects a custom record | HIGH |
| `+6` | 2 | `subtype` | the START/END/CC discriminator: **2=START, 3=END, 6=CUSTOM, 7=CC_START, 8=CC_END** | HIGH |
| `+8` | 8 | `device_ts` | raw device timestamp; rescaled to CPU ns via the per-engine `init/actual/expected_timestamp_inc` then `/1000` (`0x3E8`) | HIGH |

The `subtype` field at `+6` is the matcher's discriminant. Its values are a reduction of the hardware `NEURON_ISA_NOTIFICATION_TYPE` enum (from `enums.json`): the per-engine `*_INST_START` / `*_INST_END` hardware codes (PE=4/5, ACT=8/9, POOL=12/13, DVE=16/17, SP=20/21) collapse — through the `(subtype + 26) & 0x1F` engine-select switch in the timestamp rescaler — onto the runtime's uniform `{2=START, 3=END}` so the matcher is engine-agnostic.

#### 3.1 Custom-notification sub-ids (`*(u16*)&data[4]`)

When `subtype == 6` (CUSTOM), `data[4]` selects one of an 11-way table. These are not engine completions — they are runtime-generated diagnostics and the tensor-completion / debug-tensor hooks:

| `custom_sub_id` | Meaning | Effect |
|---|---|---|
| `0` | OOB embedding-table update (text record) | sets fault status, formats ISA-line message |
| `1` | embedding-table-update OOB (with ISA line) | `"failed to run embedding table update… isa instruction line number = %d"` |
| `2`,`3` | scatter/gather (indirect copy) OOB | `"failed to run scatter/gather (indirect memory copy)…"` |
| `4`,`5` | scatter/gather via scalar/vector DGE OOB | DGE-variant OOB text |
| `6` | custom **debug** notification | `"Received custom debug notification with value %d"` — logged, non-fatal |
| `7` | **tensor-completion** | `ht_find` var_id in `io_mr_to_name_map`; bump `output_completion_count`; `pthread_cond_broadcast` |
| `8` | **debug-tensor-read** | `exec_process_debug_tensor_read` — copy tensor out, enqueue debug stream |
| `9`,`0xA` | scatter/gather transpose via scalar/vector DGE OOB | transpose-variant OOB text |
| default | unknown | `"unknown custom notification type: %d"`, status 6 |

> **NOTE —** the tensor-completion path (sub-id 7) is how *streaming* outputs are signalled mid-execution: the device writes a custom NQ record naming a finished output tensor by `var_id`, the runtime resolves it through `io_mr_to_name_map`, bumps that tensor's `output_completion_count`, and `pthread_cond_broadcast`s — waking any host thread blocked in `nrt_get_tensor` on that output. This is a second completion channel layered on the same NQ rings, distinct from the whole-inference `DONE`.

### Per-engine timestamp decode

The `device_ts` at `+8` is in device clock units and must be rescaled to CPU nanoseconds before it is meaningful. On sunda (arch == 2) the rescaler selects a per-engine triple `{init_timestamp, actual_timestamp_inc, expected_timestamp_inc}` from `notif->init_timestamp.tpb.{pe,act,pool,dve,sp}` using `(subtype + 26) & 0x1F`, then computes:

```c
delta  = device_ts - init_timestamp[eng]
cpu_ns = (double)expected_timestamp_inc[eng] / (double)actual_timestamp_inc[eng] * delta
```

i.e. the device delta is scaled by the ratio of *expected* to *actual* clock increment (correcting for the device clock running fast or slow relative to its nominal rate), and the START/END timestamps stored into `vcore_notif_ts[pcore][eng]` are then divided by `0x3E8` (1000) and anchored to a CPU timestamp via `sync_point_dev_ts_to_cpu_ts`. The engine→triple selection switch (`case 0→PE, 4→ACT, 8→POOL, 0xC→DVE, 0x10→SP`) is the `(subtype+26)&0x1F` mapping; the scaling math is read directly from the decompile but the *intent* (clock-drift correction) is interpretive (MEDIUM).

| Decoded value | `exec_request_state_t` slot | Subtype that writes it |
|---|---|---|
| earliest engine START | `vcore_notif_ts[pcore][2]` (PE-relative base) | 2 (START) |
| latest engine END | `vcore_notif_ts[pcore][3]` | 3 (END) |
| CC sync START | `vcore_notif_ts[pcore][7]` | 7 (CC_START) |
| CC sync END | `vcore_notif_ts[pcore][8]` | 8 (CC_END) |
| `tpb_duration[pcore]` | END − START (max across engines) | derived at FINALIZE |

> **QUIRK —** the 16-byte entry is decoded **positionally, never as a typed struct**, on the completion path. The same 16 bytes carry an entirely different bit-field layout on the *error* ring (NQ#2) — `sunda.tpb_error`, `sequencer_generic_error_metadata_hi`, ECC metadata — decoded by `notification_consume_error_block` (§4). A reimplementer must not assume one C struct covers both rings; the byte at `+6` is a completion subtype on `INFER_STATUS` rings and an error discriminant on the ERROR ring. The lane is selected by *which ring you read*, not by a tag inside the entry.

---

## 4. Error Decode

### Purpose

When the harvest finalizes — on clean completion *and* on every fault path — `exec_request_process_errors` (`0x2615b0`) runs a per-core error sweep. On a clean `DONE` it still sweeps the ERROR ring (NQ#2) because an inference can complete *with* errors (numerical NaN, transient, correctable ECC) that did not stall the engines. The heavy lifting is in `notification_consume_error_block` (`0x2ff250`, 3719 B), which dequeues 16-byte error entries, classifies the infer-error subtype, and maps it onto an `NRT_STATUS`.

### Entry Point

```text
exec_request_progress_one_step (FINALIZE)
  └─ exec_request_process_errors (0x2615b0)               ── per-core classifier
       └─ notification_consume_errors (0x300350)           ── top sweep: assert mod!=NULL
            ├─ nrt_sys_trace_new_event(0, 38, …)            ── MODEL stop event
            ├─ notification_consume_error_block (0x2ff250)  ── TPB error block (tpb=1, NQ#2)
            └─ [per TopSP] notification_consume_error_block (tpb=0)
                 ├─ aws_hal_notific_nq_available_count / aws_hal_notific_nq_read
                 ├─ v2_error_get_infer_error_subtype / _sequencer_error_text
                 ├─ sync_point_dev_ts_to_cpu_ts
                 └─ nrt_sys_trace_new_event(2, 13, …)
```

### Algorithm

```c
NRT_STATUS notification_consume_error_block(notif, tpb, ...):   // 0x2ff250
    n = aws_hal_notific_nq_available_count(&notif->ens_nq[2])   // NQ#2 = ERROR ring
    if n > LARGE: warn("Received an unusually large number of error notifications (count:%u)…")
    status = NRT_SUCCESS
    for each 16-byte error entry e in NQ#2:
        ring_buffer_enqueue(exec_error_ring, e)                 // 4MB backing, for subscribers
        subtype = v2_error_get_infer_error_subtype(e)           // infer_error_subtype enum
        ts_cpu  = sync_point_dev_ts_to_cpu_ts(&sync_point, e.device_ts / 1000)
        if already_logged[subtype]: continue                    // de-dup via already_logged[26]
        switch classify(subtype, e):
            INFER_ERROR_SUBTYPE_TRANSIENT(2):     status = NRT_EXEC_COMPLETED_WITH_ERR        // 1004
            INFER_ERROR_SUBTYPE_NUMERICAL(1):     status = NRT_EXEC_COMPLETED_WITH_NUM_ERR    // 1003 (NaN)
            INFER_ERROR_SUBTYPE_SW_SEQUENCER(8):  status = NRT_EXEC_COMPLETED_WITH_ERR; + sequencer text
            ECC uncorrectable:                    status = NRT_EXEC_HW_ERR_NC_UE              // 1202
                                                  fatal("(FATAL-RT-UNDEFINED-STATE) … Uncorrectable memory error…")
            ECC correctable:                      log("Correctable memory error…"); status unchanged
        nrt_sys_trace_new_event(2, 13, …)                       // error trace event
    return status
```

### Status-code map

A reimplementer must reproduce these exact `NRT_STATUS` values at these exact gates (all from `enums.json`). The completion engine itself sets `5`/`1204` (timeout/overflow); the error sweep sets the rest; `exec_request_process_errors` additionally classifies HBM-UE, DMA-abort and collectives faults by priority.

| Code | Value | Where set | Trigger |
|---|---|---|---|
| `NRT_SUCCESS` | 0 | FINALIZE | all engines `COMPLETE`, no errors |
| `NRT_TIMEOUT` | 5 | WAIT_CORE end-of-sweep | `elapsed_ms ≥ timeout_ms` ∧ INTC bit-29 clear |
| `NRT_EXEC_COMPLETED_WITH_NUM_ERR` | 1003 | `consume_error_block` | numerical / NaN error subtype |
| `NRT_EXEC_COMPLETED_WITH_ERR` | 1004 | `consume_error_block` | transient / sequencer error subtype |
| `NRT_EXEC_OOB` | 1006 | custom-notif decode | out-of-bounds embedding / scatter-gather |
| `NRT_EXEC_HW_ERR_COLLECTIVES` | 1200 | `process_errors` | collectives hang / barrier failure |
| `NRT_EXEC_HW_ERR_HBM_UE` | 1201 | `process_errors` | unrepairable HBM ECC (sysfs) at timeout |
| `NRT_EXEC_HW_ERR_NC_UE` | 1202 | `consume_error_block` | uncorrectable NC memory error (ECC) |
| `NRT_EXEC_HW_ERR_DMA_ABORT` | 1203 | `process_errors` | DMA engine abort |
| `NRT_EXEC_SW_NQ_OVERFLOW` | 1204 | WAIT_CORE / matcher | INTC bit-29 set (NQ ring overflowed) |
| `NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE` | 1205 | `process_errors` | repairable HBM ECC |
| `NRT_NETWORK_PROXY_FAILURE` | 1206 | WAIT_CORE net-proxy gate | `enc_check_proxy_network_task_status` fault |
| `NRT_EXEC_UNIT_UNRECOVERABLE` | 101 | `tpb_xu_step` | XU `health_status == FATAL_SHUTDOWN` |

> **CORRECTION (COMPLETE-1) —** the cross-cell seed (`P1-L-TDRV-CORE-06` §8 D2) flags a *possible* defect: the `process_errors` timeout branch tests `(_DWORD)hbm_ret == 1201` (`HW_ERR_HBM_UE`) but `exec_check_hbm_uncorrectable` can also return `1205` (`REPAIRABLE_HBM_UE`), so a *repairable* HBM UE detected exactly at timeout would fall through to DMA/collectives/`HANG_ON_COMPUTE` classification instead of being propagated as `1205`. The dedicated `printk` in `exec_check_hbm_uncorrectable` still fires, so this is a status-*priority* nuance (the operator still sees the HBM diagnostic), not a silent loss. It is recorded here as **evidence-only, not asserted as a confirmed bug** — confirm against a newer `libnrt` before relying on `1205` propagation through the timeout path.

> **NOTE —** `nrt_get_status_priority` resolves multi-core disagreement: when the two NeuronCores of an LNC return different `NRT_STATUS` values, the *highest-priority* (most severe) one becomes the request's result. A `NRT_SUCCESS` on core 0 and `NRT_EXEC_COMPLETED_WITH_NUM_ERR` on core 1 yields `1003` for the whole inference.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `exec_request_process_errors` | `0x2615b0` | Per-core priority classifier (`.isra.0`) | HIGH |
| `notification_consume_errors` | `0x300350` | Top error sweep; MODEL-stop trace; TPB + per-TopSP blocks | HIGH |
| `notification_consume_error_block` | `0x2ff250` | 16-byte error-entry decode; ECC/numerical/transient → status | HIGH |
| `exec_check_hbm_uncorrectable` | `0x2614c0` | sysfs ECC counters → `1201`/`1205` | HIGH |
| `nrt_get_status_priority` | (runtime) | Multi-core status reconciliation | HIGH |
| `v2_error_get_infer_error_subtype` | (error lane) | Map 16-byte error entry → `infer_error_subtype` | MEDIUM |

---

## 5. The Timeout / Overflow Disambiguation

### Purpose

A stalled engine has two distinct causes that look identical from the NQ ring: either the device genuinely never finished (a real timeout), or it *did* finish but the completion record was dropped because the NQ ring overflowed. These demand different operator actions, so the runtime disambiguates them by reading a hardware interrupt-cause register.

### Algorithm

`exec_check_intc_sw_notif_queue_overflow` (`0x2613c0`) is a 114-byte probe:

```c
bool exec_check_intc_sw_notif_queue_overflow(pcore):              // 0x2613c0
    db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)
    block = aws_hal_get_user_errtrig_block_from_tpb_idx(pcore->device_tpb_idx)
    cause = aws_hal_intc_read_cause(&mla->intc, block, 61)        // INTC cause, line 61
    debug("aws_hal_intc_read_cause = 0x%x", cause)
    return (cause >> 29) & 1                                       // bit 29 = SW NQ overflow
```

Bit **29** of the INTC error-trigger cause (block line 61) is the hardware's "software notification queue overflowed" flag. The end-of-sweep logic calls this only when an engine is incomplete *and* the wall-clock budget is exhausted:

```c
if elapsed_ms >= core.timeout_ms:                                 // budget exhausted
    if exec_check_intc_sw_notif_queue_overflow(pcore):            // bit 29 set?
        exec_fatal_status[pcore][2] = 1; status = 1204            // SW_NQ_OVERFLOW: record was dropped
    else:
        exec_fatal_status[pcore][1] = 1; status = 5               // TIMEOUT: device never finished
```

The matcher (§2) calls the *same* probe on an out-of-order record (`START` when not `START_PENDING`, `END` when not `END_PENDING`) — because an out-of-order record is itself a symptom of a dropped (overflowed) entry — and again returns `1204` vs a generic error depending on bit 29.

> **GOTCHA —** the timeout budget is `core.timeout_ms = 1000 * mods[0]->param.timeout` — the model's `param.timeout` is in **seconds**, scaled to milliseconds. A reimplementation that reads `param.timeout` as already-milliseconds will time out 1000× too early. The budget is measured from `core.start_ts`, captured at the `WAIT_BARRIER_PROXY → WAIT_CORE` transition — *after* the collectives barrier clears — so barrier wait time does not count against the core timeout.

> **QUIRK —** the overflow probe is read **lazily**, only at the timeout boundary or on an out-of-order record — never on the happy path. INTC cause registers are MMIO reads with latency; probing them every sweep would tax the poll loop. A reimplementer should treat the overflow check as a *disambiguation step* taken once the harvest has already decided something is wrong, not as a per-iteration health check.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `exec_check_intc_sw_notif_queue_overflow` | `0x2613c0` | Read INTC cause; return bit-29 | HIGH |
| `aws_hal_intc_read_cause` | (HAL) | MMIO read of INTC cause register (line 61) | HIGH |
| `aws_hal_get_user_errtrig_block_from_tpb_idx` | (HAL) | TPB idx → error-trigger block | HIGH |
| `get_timespec_delta` | (tdrv) | Elapsed seconds between two `timespec` | HIGH |

---

## 6. The Async Completion Signal

### Purpose

In the synchronous path the harvest loop *is* the caller's thread; when `exec_wait_round_robin` returns, the result is already in hand. In the async path the harvest runs on a worker thread (`tpb_xu_step`), so the worker must hand the result back to whoever submitted: the submit path's `read(mark_efd)`. That handoff is `tpb_xu_base_report_complete`.

### Algorithm

`tpb_xu_step` (`0xe8940`) drives the state machine and, on `DONE` (return `0`) or error (return `2`), tears down the request and reports:

```c
NRT_STATUS tpb_xu_step(xu):                                       // 0xe8940
    rc = xuq_worker_peek_current_exec(&xu->work_queue, &exec_info)
    if rc == NRT_QUEUE_EMPTY: return NRT_SUCCESS                   // nothing to step
    if xu->health_status == FATAL_SHUTDOWN:
        exec_status = NRT_EXEC_UNIT_UNRECOVERABLE                  // 101
    else:
        if !xu->current_exec_initialized:
            exec_request_init_state(xu->current_exec_state, …)     // first step only
            xu->current_exec_initialized = 1
        v = exec_request_progress_one_step(xu->current_exec_state) // SAME machine
        if v == 2: xu->health_status = FATAL_SHUTDOWN              // degrade XU
        else if v != 0: return NRT_SUCCESS                         // still in flight (v==1)
    // ---- DONE: metrics, trace, unstage, cleanup ----
    kmetric_update_nds_exec_latencies / _error_stats / _exec_stats
    nrt_sys_trace_new_event(1, 19, …)                              // close execute span
    kmgr_unstage_kelf_model(deferred_unstage_ref)                  // release model staging
    exec_request_cleanup_state(xu->current_exec_state); free(exec_info)
    tpb_xu_base_report_complete(&xu->base, seq_id, exec_status_out, exec_status)
    return NRT_SUCCESS

void tpb_xu_base_report_complete(xu, seq_id, *status_output, exec_status):   // 0xe8800
    read(xu->has_work_efd, &one, 8)                                // pop one work count; abort() on -1
    if status_output: *status_output = exec_status                 // publish result
    pthread_mutex_lock(&xu->mark_comp_lock)
    rc = xuq_worker_pop_current_exec(&xu->work_queue)              // signals the slot's mark_comp_efd
    pthread_mutex_unlock(&xu->mark_comp_lock)
    if rc: xu->health_status = FATAL_SHUTDOWN                      // "[XU %x] Failed to complete request"
    debug("Completed with status %s, Execution Unit[%x]")
```

`xuq_worker_pop_current_exec` is what writes the per-execution `mark_comp_efd` (the `+0x10` field of the `xuq_exec_info_t` slot the [submit path](submit-path.md#the-submit-boundary-sync) blocked on). That write is the only thing that unblocks the synchronous submitter's `read(mark_efd, …, 8)`. Note the symmetry: the worker first **reads** `has_work_efd` (consuming the wakeup the submit path posted) and then **signals** `mark_comp_efd` (waking the submit path's completion wait). Both eventfds are userspace-to-userspace; the kernel is not in this loop.

> **QUIRK —** `read(has_work_efd)` returning `-1` is treated as fatal (`abort()`), not a recoverable error. The eventfd counter must be readable here — it was incremented by the submit path's `write(has_work_efd, 1, 8)`. If it under-flows, the work-queue accounting is corrupt and the worker cannot safely continue. A reimplementer must keep the `has_work_efd` increment (submit) and decrement (complete) exactly paired, one per scheduled request.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tpb_xu_step` | `0xe8940` | Worker step: drive machine, on DONE unstage + report | HIGH |
| `tpb_xu_base_report_complete` | `0xe8800` | Pop `has_work_efd`; signal `mark_comp_efd`; publish status | HIGH |
| `xuq_worker_peek_current_exec` | (xu) | Peek the in-flight SPSC slot | HIGH |
| `xuq_worker_pop_current_exec` | (xu) | Pop slot; signal per-exec `mark_comp_efd` | HIGH |
| `kmgr_unstage_kelf_model` | (kmgr) | Release deferred model staging post-execution | HIGH |

---

## 7. Reimplementation Checklist

A completion-engine reimplementation is correct when it reproduces, in order:

1. **The one-step contract** — a non-blocking `progress` function returning `1` (in flight), `0` (done OK), `2` (done with error in `*out`). Never block inside; the driver polls with `usleep(1)` (sync) or worker scheduling (async).
2. **State machine** — `INIT` arms `barrier_proxy` (timeout 120s, doubling); `WAIT_BARRIER_PROXY` gates on `enc_check_proxy_barrier_task_status`; `WAIT_CORE` harvests the rings; `DONE` finalizes.
3. **Engine init** — on `WAIT_BARRIER_PROXY → WAIT_CORE`: fetch `INFER_STATUS` NQ ids `{4,5,6,7,8}`, set all `tpb_exec_state` to `START_PENDING`, set `timeout_ms = 1000 * model.param.timeout`, capture `core.start_ts`.
4. **The matcher** — per `(pcore, engine)`, read up to 16 entries via `notification_read_exec_queue`; on subtype `2` (START) `START_PENDING→END_PENDING`, on `3` (END) `END_PENDING→COMPLETE`, on `7`/`8` record CC timestamps; an out-of-order record → probe INTC bit-29 → `1204` or generic error.
5. **16-byte decode** — `data[2]>>4`=debug-hint, `data[3]&0x1F`=NQ-id (forced `0xE` for cayman DVE custom), `*(u16)@4`=custom sub-id, `*(u16)@6`=subtype, `*(u64)@8`=device timestamp.
6. **Timeout** — at end-of-sweep, if any engine incomplete and `elapsed_ms ≥ timeout_ms`: `exec_check_intc_sw_notif_queue_overflow` → `1204` (bit 29 set) or `5` (clear).
7. **Error sweep** — at FINALIZE, `exec_request_process_errors` → `notification_consume_error_block` on NQ#2: `TRANSIENT→1004`, `NUMERICAL→1003`, uncorrectable ECC→`1202`; reconcile multi-core via `nrt_get_status_priority`.
8. **Signal** — async only: pop `has_work_efd`, signal the slot's `mark_comp_efd` to unblock the submit path's `read(mark_efd)`.

> **GOTCHA —** the 472-byte `exec_request_state_t` carries *both* union substates (`barrier_proxy` 24 B and `core` 80 B) overlaid at `+0x188`, selected by `state`. A reimplementation that allocates them as separate fields is functionally fine but will read the wrong offsets against this binary; the overlay is why `barrier_proxy.start_ts` and `core.start_ts` share storage. The per-engine `tpb_exec_state[2][5]` (2 cores × 5 engines) and `vcore_notif_ts[2][13]` (per-engine timestamp slots, stride 13) are the matcher's working set — size them for the maximum 2-core LNC even on single-core models.

---

## Related Components

| Component | Relationship |
|---|---|
| [The Submit Path](submit-path.md) | the other half: ends at `read(mark_efd)` that *this* page's `tpb_xu_base_report_complete` unblocks |
| [Notification Queue Engine (kernel)](../kernel/notification-queues.md) | the kernel side that allocates and programs the NQ rings this page polls |
| [Overview: the Hot Inference Path](overview.md) | where submit and completion meet in the end-to-end exec flow |
| [The XU Work-Queue and Worker Threads](xu-workers.md) | the worker thread that calls `tpb_xu_step` and owns the SPSC slot |
| [Exec Manager (KMGR): Facade and Model DB](kmgr-facade.md) | resolves the model handle and owns `async_exec_workers` |

## Cross-References

- [The Submit Path (Bind → Stage → Doorbell)](submit-path.md) — the sibling half; writes the request this page harvests, blocks on the `mark_comp_efd` this page signals
- [Overview: the Hot Inference Path](overview.md) — the end-to-end exec flow this page closes
- [Exec Manager (KMGR): Facade and Model DB](kmgr-facade.md) — `kmgr_exec_wait`, the sync completion-wait that drives `exec_wait_round_robin`
- [The XU Work-Queue and Worker Threads](xu-workers.md) — `tpb_xu_step` and the SPSC slot whose `mark_comp_efd` gates completion
- [The xu_queue and device_exec_request ABI](xu-queue-abi.md) — byte layout of the `xuq_exec_info_t` slot and `exec_request_state_t`
- [Notification Queue Engine](../kernel/notification-queues.md) — the kernel NQ plane that backs the `INFER_STATUS` and ERROR rings
