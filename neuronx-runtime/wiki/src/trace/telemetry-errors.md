# Telemetry, Metrics & Error Reporting

> *All `libnrt.so` addresses, symbols, and `.rodata`/`.data`/`.bss` offsets on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, soname `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`; unstripped, full DWARF v4; `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA). All `file:line` citations into the kernel are against the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0` (`usr/src/aws-neuronx-2.27.4.0/`), principally `neuron_metrics.c` (1247 lines) and `neuron_metrics.h` (218 lines). Other versions will differ.*
>
> *Part XIII — Profiling, Trace & Telemetry / DEEP. **Evidence grade:** **Confirmed (binary symbol+addr / GPL file:line anchored)** — the runtime posting path and the two counter-bridge functions are pinned to `.symtab` symbols + `.text` addresses and their `.rodata`/`.data` tables; the kernel aggregation engine is read `file:line` from `neuron_metrics.c`. The `NRT_STATUS` code table is **owned by** [Error and Status Codes](../runtime/error-codes.md) and the sysfs metric-id taxonomy by [Sysfs Metrics Tree](../kernel/sysfs.md) — both are cross-linked here, not re-derived. · [back to index](../index.md)*

## Abstract

This page owns the **telemetry-post and error-reporting half** of the metrics plane that [the section map](overview.md#5-the-divergent-metrics-plane) identifies as divergent from the NTFF trace convergence. There is no single end-to-end actor: a metric is *published* by `libnrt.so` into a Neuron DataStore (NDS) shared-memory counter slab, the kernel's per-device `ndN metrics` kthread *aggregates* every live slab on a slow tick, packs the differential result into a ≤128-byte CloudWatch (CW) byte-stream, and posts it to firmware over FW-IO. The runtime never serializes a metric and never talks to CloudWatch; the kernel never sees an `NRT_STATUS`. They meet only at the NDS counter slab — userspace writes counter words, the kernel reads them back under the datastore lock. The familiar frame is a **statsd-style two-process split**: an in-process client that increments shared counters and an out-of-band agent that drains, differentiates, and ships them; the difference is that the "shared memory" is a kernel-mediated datastore and the "agent" is a kernel thread posting to on-device firmware rather than a UDP socket.

The error path runs *alongside* this metrics path and feeds it. A hardware TPB error notification is decoded in userspace into an `INFER_ERROR_SUBTYPE`, collapsed into an execution-band `NRT_STATUS`, and persisted by two `libnrt` bridge functions — `kmetric_update_nds_generic_status @0xe0cd0` (the `0..15` core band, via the `CSWTCH.6` lookup) and `kmetric_update_nds_error_stats @0xe0f60` (the L2 software subtypes, via a `+0x1f` offset) — into the same per-NeuronCore NDS counters the kernel later aggregates. So an error becomes a CloudWatch metric by the identical machinery a success count does: increment an NDS counter, let the kthread differential-post it. The kernel registry that binds each counter to a CW id (`nmetric_defs[]`, `neuron_metrics.c:172-243`) carries an inline comment naming the exact `NRT_STATUS` each metric tracks — the single most explicit cross-layer crosswalk in the tree.

The page is structured as the flow it describes. §1 is the end-to-end diagram (runtime publish → NDS slab → kernel aggregation → FW-IO → CloudWatch, with the parallel error path). §2 traces the runtime-side PROF-05 session-finalization / telemetry-post path. §3 is the runtime→kernel counter bridge (`kmetric_update_nds_*`) as pseudocode. §4 is the error-reporting subsystem — the infer-error-subtype decode and the subtype→`NRT_STATUS` table. §5 is the kernel CloudWatch aggregation engine (`neuron_metrics.c`). The `NRT_STATUS` value space, the priority classifier, and the sysfs metric-id namespace math are owned by sibling pages and only referenced here.

For reimplementation, the contract is:

- **The publish/aggregate/post split** — `libnrt` writes counter words into an NDS slab; the kernel `ndN metrics` kthread reads them back under the datastore lock, differentiates against a per-session baseline, and posts; the runtime is not in the post path.
- **The two counter-bridge functions** — `kmetric_update_nds_generic_status` (status → NDS via `CSWTCH.6 @0x8576a0`) and `kmetric_update_nds_error_stats` (infer-error flags → EXT SW counters via `+0x1f`), the only runtime writers of the *error* counters.
- **The kernel aggregation engine** — the 41-row `nmetric_defs[]` registry, the 10 metric types each with a mirror aggregate/post path, the differential `(curr+freed−prev)` counter model, the two-tick 128-byte budget split, and the `nmetric_cw_metric {id:u8, len:u8, data[len]}` wire record.
- **The error→telemetry join** — the infer-error-subtype decode (TPB notification → `INFER_ERROR_SUBTYPE` → `NRT_STATUS`), and how each surfaced code lands in an NDS counter, a CW id (`200..253`), and a sysfs attribute.

| | |
|---|---|
| **Runtime finalize path** | `nrt_profile_stop_internal @0xb2cd0` (NTFF) — PROF-05 session-finalization; live counter writers `kmetric_*` `@0xe0cd0..0xe10a0` |
| **Counter bridge #1** | `kmetric_update_nds_generic_status @0xe0cd0` — `NRT_STATUS−1 → CSWTCH.6 @0x8576a0` → NDS NC counter |
| **Counter bridge #2** | `kmetric_update_nds_error_stats @0xe0f60` — `infer_error_flags[5..8] → counter (bx+0x1f) = NDS_EXT 36..39` |
| **Infer-error decode** | `v2_error_get_infer_error_subtype @0x321760` via `sunda_error_subtypes @0x9e0fa0` (12×u32) |
| **Kernel aggregation engine** | `neuron_metrics.c` — `ndN metrics` kthread `nmetric_thread_fn` (`:1047`); registry `nmetric_defs[]` (`:172-243`, 41 rows) |
| **Per-tick pipeline** | `nmetric_aggregate_and_post_tick` (`:1017`) → full_aggregate → cache → **release lock** → post → new_session |
| **Post target** | `ndhal->ndhal_fw_io.fw_io_post_metric(ctx, buf, size)` (`:900`, POST_TO_CW); buffer ≤128 B |
| **CW wire record** | `struct nmetric_cw_metric { u8 id; u8 len; u8 data[]; }` `__packed__` (`:254-258`), value is ASCII, no trailing NUL |
| **Periods** | post delay `150000 ms` (~2.5 min, module-param, `:23`); sample delay `50 ms` (`:24`); two ticks split the 128-B budget |
| **CW id space** | `enum nmetric_cw_id` (`:42-170`), ids `11..253`; error band `200..253` — taxonomy **owned by** [kernel/sysfs](../kernel/sysfs.md) |

---

## 1. End-to-End Telemetry & Error Flow

### Purpose

This is the central picture a reimplementer reproduces: a counter word written by `libnrt` into an NDS slab, walked by the kernel kthread, differentiated, packed, and posted to firmware; and the parallel error path that converts a silicon fault into one of those counter increments. The two converge at the NDS counter slab and share no other code.

### The flow

```text
  ┌──────────────────────── libnrt.so (PUBLISH — userspace) ────────────────────────┐
  │  exec / load / unload completion        TPB ERROR-ring entry (NQ_TYPE_ERROR)      │
  │        │                                       │ drained by userspace             │
  │        │ success/latency counters              ▼                                   │
  │        │                            v2_error_get_infer_error_subtype @0x321760     │
  │        │                            via sunda_error_subtypes @0x9e0fa0 (L3→L2)     │
  │        │                                       │                                   │
  │        ▼                                        ▼                                   │
  │  kmetric_update_nds_exec_stats          INFER_ERROR_SUBTYPE → NRT_STATUS            │
  │  kmetric_update_nds_exec_latencies      (1003 / 1004 / 1200-series)                 │
  │        │                                       │                                   │
  │        │                          ┌────────────┴───────────────┐                   │
  │        │                          ▼                            ▼                   │
  │        │       kmetric_update_nds_generic_status     kmetric_update_nds_error_stats │
  │        │       @0xe0cd0  (CSWTCH.6 @0x8576a0)         @0xe0f60  (flags[5..8]+0x1f)  │
  │        │       NRT_STATUS−1 → NDS NC counter          → NDS_EXT 36..39 SW counters  │
  │        └──────────────┬───────────────────────────────────────┘                   │
  │                       ▼                                                             │
  │             nds_increment_nc_counter(nds, nc, id, 1)  → counter word in NDS slab    │
  └───────────────────────┼─────────────────────────────────────────────────────────┘
                          │  shared-memory NDS counter slab  [datastore/kernel-side]
  ════════════════════════╪══════════════ kernel/userspace boundary ═══════════════════
                          ▼
  ┌──────────────────── aws-neuronx-dkms (AGGREGATE+POST — kernel) ───────────────────┐
  │  ndN metrics kthread  nmetric_thread_fn (:1047)  50 ms sample / ~2.5 min slow tick  │
  │        │                                                                            │
  │        ▼  nmetric_aggregate_and_post_tick (:1017)                                    │
  │   neuron_ds_acquire_lock ─► nmetric_full_aggregate (:400)                            │
  │        for each in-use slab: Σ get_neuroncore_counter_value over dev_nc_map          │
  │        curr[id] += counter ;  FW_IO_ERR: curr[17] = fw_io_get_err_count (:412)       │
  │   nmetric_cache_shared_bufs (:916)  snapshot+reset ds_freed_* (process-death capture)│
  │   neuron_ds_release_lock                                                             │
  │        ▼  nmetric_post_metrics (:838)   crt = curr + freed − prev  (skip crt<=prev)  │
  │        pack {id:u8,len:u8,data[len]} into posting_buffer[129]  (≤128 B/tick)         │
  │   nmetric_start_new_session (:973)   prev := curr  (differential baseline)           │
  │        ▼                                                                             │
  │   if log_posts&1: ndhal->ndhal_fw_io.fw_io_post_metric(ctx, buf, size)  (:900)       │
  │   if log_posts&2: trace_metrics_post(id,len,data)  (ftrace, :442)                    │
  └────────────────────────┼──────────────────────────┼───────────────────────────────┘
                           ▼ POST_TO_CW (FW-IO)        ▼ ftrace
                  on-device firmware                neuron-monitor / debug
                           │
                           ▼
                   CloudWatch  [out of binary, out of kernel]

  parallel persistence sink (read-only consumers of the same NDS counters):
   NDS counter id ──► sysfs attribute name  ("success" / "hw_error" / "execute_sw_…")  [kernel/sysfs]
                 └──► nmetric_defs[] cw_id  (200..253)  ──► CW post above
```

Three facts a reimplementer must internalize from this picture:

- **The runtime writes counters, not metrics.** `libnrt`'s entire contribution to telemetry is `nds_increment_nc_counter` into a shared slab. It performs no aggregation, no differencing, no packing, and no FW-IO. A reimplementer looking for a "post a metric" call in `libnrt` will not find one — it ends at the NDS write, exactly as the trace producers end at the NTFF file write.
- **The kernel posts differentials, not absolutes.** Every COUNTER/UTILIZATION metric is posted as `(curr + freed − prev)` and *skipped* when `crt <= prev` (`nmetric_post_counter`, `:617`); `prev[]` is rebased to `curr[]` at the end of every tick (`nmetric_start_new_session`, `:991`). The wire value is the delta since the last post, not the lifetime total.
- **The error path is just a counter increment.** There is no separate error-shipping channel. An error reaches CloudWatch because the two bridge functions increment an NDS *error* counter, and that counter is in `nmetric_defs[]` bound to a CW id. The error machinery ends where the success machinery does.

> **NOTE —** the NDS counter slab is the only shared surface. Its byte layout is owned by [datastore/kernel-side](../datastore/kernel-side.md); the kernel's 3-band counter reader `get_neuroncore_counter_value` (`neuron_ds.c:207`) is the read used at `neuron_metrics.c:376`. This page treats the slab as the publish target and the aggregation source, never re-deriving its layout.

---

## 2. The Runtime Telemetry-Post Path (PROF-05 Session Finalization)

### Purpose

The runtime's telemetry contribution has two distinct shapes, and a reimplementer must not conflate them. The **NTFF session-finalization** path (`nrt_profile_*`, TU `nrt_profile.cpp`) assembles a post-mortem device-profile protobuf and writes a `.ntff` file — it is *offline* trace, not live telemetry, and it does **not** publish CloudWatch counters. The **live metrics** path is the `kmetric_*` family (§3), invoked from the *exec / load / unload* completion paths, not from profile finalization. They share the word "telemetry" and the `nrt_profile.cpp` neighborhood but are schema-disjoint and code-disjoint, exactly as [the section map](overview.md#5-the-divergent-metrics-plane) describes.

### Entry Point

```text
nrt_profile_stop (0xb47c0)                    ── public wrapper → stop_internal(file, flush=1)
  └─ nrt_profile_stop_internal (0xb2cd0)      ── NTFF finalizer (6882 B, 259 BB, 65 callees)
       ├─ kmgr_get_nn_info / kmgr_set_profiling_status        [BOUNDARY KMGR]
       ├─ nrt_profile_notification_read_all_nc (0xb1850)      ── drain per-NC NQ blobs
       ├─ ntff::* DefaultConstruct / ArenaStringPtr::Set      [BOUNDARY protobuf/ntff.pb.cc]
       ├─ nrt_profile_sg_add_* / get_model_collectives_*_info ── field-by-field proto build
       ├─ nrt_profile_serialize (0xb2670)                     ── write .ntff file
       └─ nrt_profile_notification_unsubscribe_nc (0xab020)

  (the LIVE metrics path is NOT here — it is the exec/load/unload completion path:)
nrt_execute … completion ─► kmetric_update_nds_exec_stats (0xe0d30)
                           ► kmetric_update_nds_exec_latencies (0xe0fe0)
                           ► kmetric_update_nds_generic_status (0xe0cd0)   [§3]
                           ► kmetric_update_nds_error_stats (0xe0f60)      [§3, §4]
```

### Algorithm — the NTFF finalizer

`nrt_profile_stop_internal @0xb2cd0` is the central NTFF device-profile finalizer — the largest function in the PROF-05 cell (6882 B / 259 basic blocks / 65 callees). The skeleton below names the decision points and the boundary calls; the field-by-field `ntff::ntff_info` assembly is owned by the NTFF schema page.

```c
// Models nrt_profile_stop_internal @0xb2cd0  — nrt_profile.cpp.
// flush (a2) = 1 from public stop / continuous_save; 0 only unsubscribes. (LOW: arg not DWARF-named.)
NRT_STATUS nrt_profile_stop_internal(const char *filename, char flush) {
    nlog(TAG, "nrt_profile_stop_internal", "API:IN: (filename=%s)", filename);   // @0x83ebc6

    if (nrt_init_state == UNINIT)  return state_err("NRT uninitialized");         // @0x83cf4b
    if (nrt_init_state == CLOSED)  return state_err("NRT already closed");        // @0x83cf73

    // look the filename up in the requested-profiles map; reject if never started
    nrt_model *model = g_device_profile_context.requested_model_profiles[filename]; // .bss @0xc5d140
    if (model == NULL)
        return log_err("Profile stop request for %s not found…", filename);       // @0x7d50e8

    // drain on-device notification blobs for every NeuronCore of this model
    nrt_profile_notification_read_all_nc(model);                                   // 0xb1850

    // assemble the ntff::ntff_info protobuf message (BOUNDARY ntff.pb.cc):
    ntff_info *pb = arena_new<ntff_info>();
    add_version_info(pb, ndl_get_version, nec_get_version_info, nrt_get_version);  // x3 version_info
    add_ultraserver_info(pb);                  // pod / ultraserver topology
    for (sg : model->subgraphs) {
        subgraph_info *sgi = pb->add_subgraph_info();
        nrt_profile_sg_add_dma_info(sgi, sg);                                      // 0xabc30
        for (i = 0; i < 22; i++) sgi->nc_memory_usage[i] = dma_mem_usage(sg, i);   // nc_memory_usage[22]
        nrt_profile_get_instructions(sgi, sg);                                     // 0xb0bc0
        nrt_profile_get_model_collectives_ops_info(sgi, sg);                       // 0xae010
        add_execution_timeline(sgi);          // "Added execution timeline … UID=%lu …" @0x7d5150
    }

    nrt_profile_serialize(filename, pb, instr, traces);                            // 0xb2670 — write .ntff
    if (flush) nrt_profile_notification_unsubscribe_nc(model);                     // 0xab020

    nlog(TAG, "nrt_profile_stop_internal", "API:OUT: (filename=%s) ret=%u", filename, rc); // @0x83ebdc
    return rc;
}
```

### The session-queued variant

The session API (`nrt_profile_session_*`) defers serialization: `nrt_profile_session_start @0xb6800` arms per-NC capture and allocates an `active_ntff` payload; `nrt_profile_session_serialize @0xb61d0` pops one queued `ntff_data_t` (72 B) out of the per-NC `pending_serialization` map, releases the NC lock, then calls `nrt_profile_serialize` + `ntff_data_clear`; `nrt_profile_session_close @0xb6500` (called by `nrt_close`) walks all `nc_session[]` slots under `session_ctx.global_lock @0xc5c900`, clears every queued payload, marks each for deletion, and **spin-waits with `sched_yield` until `ref_count <= 1`** before release. The queued payload struct:

| Field | Offset | Type | Role |
|---|---|---|---|
| `proto` | `+0` | `ntff::ntff_info *` | the protobuf message being assembled |
| `instruction_data` | `+8` | `vector<pair<char*,uint32>>` (24 B) | per-instruction blobs |
| `trace_notifications` | `+32` | `vector<pair<char*,uint32>>` (24 B) | drained NQ notification blobs |
| `expl_notifs` | `+56` | `profile_session_nc_expl_notifications_t` (16 B) | `{num_topsp:u32, cc_notifications:buf*}` |

> **NOTE — the finalize path publishes NTFF, not CloudWatch.** `nrt_profile_stop_internal` writes a `.ntff` file and touches no NDS counter. The live CloudWatch metrics are published from the exec/load/unload completion paths by `kmetric_*` (§3). A reimplementer wiring CloudWatch telemetry must instrument the exec path, not the profile-stop path; the two are co-resident but independent, as [overview §5](overview.md#5-the-divergent-metrics-plane) establishes (`kmetric_* → tdrv_nds_* → NDS`).

> **QUIRK — the `cc_notifications[]` array carries a hidden count word.** `expl_notifs.cc_notifications` is allocated as `operator new[](3 * num_topsp * 8 + 8)`; the leading 8 bytes hold the element count (the `new[]` cookie pattern), and `nrt_profile_session_ntff_data_clear @0xb5ed0` reads that word back before `operator delete[]`. A reimplementer allocating a bare `num_topsp`-element array will under-allocate by the 8-byte cookie and corrupt the teardown. (LOW confidence on the exact count-word semantics; read from the `new[]` cookie pattern.)

---

## 3. The Runtime→Kernel Counter Bridge (`kmetric_update_nds_*`)

### Purpose

Two `libnrt` functions are the only writers of the *error* counters in the NDS slab. They translate an `NRT_STATUS` (or a decoded infer-error flag set) into an NDS NeuronCore counter id and increment it. They are the structural seam between the [error-code catalogue](../runtime/error-codes.md) (which owns the `NRT_STATUS` value space) and this page (which owns how those codes become telemetry). A reimplementer must reproduce both lookup tables byte-for-byte, because the kernel aggregation engine assumes the counter ids are exactly these.

### Entry Point

```text
exec completion ─► kmetric_update_nds_generic_status (0xe0cd0)   ── status → NDS NC counter (CSWTCH.6)
                ─► kmetric_update_nds_error_stats   (0xe0f60)   ── infer flags → NDS_EXT SW counters
                     └─ both ─► nds_increment_nc_counter(nds, nc, id, 1)   [datastore write]
```

### Algorithm — bridge #1: generic status

`kmetric_update_nds_generic_status @0xe0cd0` maps the `0..15` core `NRT_STATUS` band through a 10-entry signed lookup table `CSWTCH.6 @0x8576a0`, where `−1` means "this status has no NeuronCore counter and is skipped". Only six of the ten core codes land in a counter; the rest (`TIMEOUT`/`HW_ERROR`/`QUEUE_FULL`/gap-8) route through other paths or have no per-NC mirror.

```c
// Models kmetric_update_nds_generic_status @0xe0cd0.
// CSWTCH.6 @0x8576a0  (10 × i32):  {16, 19, -1, 17, -1, -1, -1, -1, 18, 20}
//   NRT_STATUS-1 indexes it; result is the NDS_NC_COUNTER id, or -1 to skip.
void kmetric_update_nds_generic_status(int nds_handle, int nc, NRT_STATUS s) {
    int edx = (int)s - 1;                          // NRT_FAILURE(1) -> 0, …
    if ((unsigned)edx > 9) return;                 // outside the 0..15 core band -> not this path
    int id = CSWTCH.6[edx];                         // @0x8576a0
    if (id < 0) return;                            // -1 sentinel: no per-NC counter for this status
    nds_increment_nc_counter(nds_handle, nc, id, 1);
}
```

The verified mapping (cross-checked index-by-index against the kernel `NDS_NC_COUNTER` enum):

| `NRT_STATUS` | `edx = s−1` | `CSWTCH.6[edx]` | NDS NC counter |
|---|---|---|---|
| `NRT_FAILURE` (1) | 0 | `16` | `NDS_NC_COUNTER_GENERIC_FAIL` |
| `NRT_INVALID` (2) | 1 | `19` | `NDS_NC_COUNTER_ERR_INVALID` |
| `NRT_INVALID_HANDLE` (3) | 2 | `−1` | *skip* |
| `NRT_RESOURCE` (4) | 3 | `17` | `NDS_NC_COUNTER_ERR_RESOURCE` |
| `NRT_TIMEOUT`..`NRT_QUEUE_FULL` (5..7) | 4..6 | `−1` | *skip (other paths)* |
| (gap 8) | 7 | `−1` | *skip* |
| `NRT_LOAD_NOT_ENOUGH_NC` (9) | 8 | `18` | `NDS_NC_COUNTER_ERR_RESOURCE_NC` |
| `NRT_UNSUPPORTED_NEFF_VERSION` (10) | 9 | `20` | `NDS_NC_COUNTER_ERR_UNSUPPORTED_NEFF_VERSION` |

### Algorithm — bridge #2: infer-error stats

`kmetric_update_nds_error_stats @0xe0f60` persists the decoded software error subtypes. It walks the 9-byte `kbl_infer_errors.infer_error_flags[]` array (one byte per `INFER_ERROR_SUBTYPE` index, §4) and, for the four *software* subtypes (indices `5..8`), increments the matching kernel extended counter at id `index + 0x1f` — i.e. `5..8 → NDS_EXT_NC_COUNTER 36..39`. The same function also writes the latency / collective-comm-time counters (`0xd/0xe/0xf/0x15`) from float duration fields.

```c
// Models kmetric_update_nds_error_stats @0xe0f60.
// kbl_infer_errors @<3f1ead> (9 B): infer_error_flags[9], one byte per INFER_ERROR_SUBTYPE index.
// SW subtypes 5..8 (SEMAPHORE/EVENT/PSUM/SEQUENCER) -> NDS_EXT_NC_COUNTER 36..39.
void kmetric_update_nds_error_stats(int nds_handle, int nc, const kbl_infer_errors *e) {
    for (int bx = 0; bx < 9; bx++) {
        if (!e->infer_error_flags[bx]) continue;
        unsigned tmp = (unsigned)(bx - 5);          // subtype index relative to SW_SEMAPHORE
        if (tmp <= 3) {                             // bx in 5..8  -> the four SW subtypes
            int id = bx + 0x1f;                     // 5+31=36 … 8+31=39
            nds_increment_nc_counter(nds_handle, nc, id, 1);
        }
    }
    // (+ latency / cc-time counters 0xd/0xe/0xf/0x15 from float durations — exec-stats overlap)
}
```

> **GOTCHA — the four SW subtypes have a counter but no distinct `NRT_STATUS`.** Subtypes `5..8` (`SW_SEMAPHORE/EVENT/PSUM/SEQUENCER`) each get a dedicated NDS extended counter (`36..39`) and a sysfs attribute (`execute_sw_semaphore_error`, …), but at the public API they all collapse into `NRT_EXEC_COMPLETED_WITH_ERR(1004)`. The resolution exists *only* in the kernel counter and the L2 enum — a reimplementer reading the return code alone cannot recover which SW subtype fired. The kernel header comment is explicit that the counter order is contractual: *"these must be in this specifc order … runtime assumes these are offset by error code"* (`share/neuron_driver_shared.h:310-312`). Preserve the ordering exactly.

> **NOTE — the bridge writes; it never posts.** Both functions terminate in `nds_increment_nc_counter`, a write into the NDS slab ([datastore/kernel-side](../datastore/kernel-side.md) owns the slab; the increment lands in a per-NeuronCore counter word). Neither function touches FW-IO or CloudWatch. The post is the kernel kthread's job (§5). The `kmetric` family is the entire runtime side of the metrics plane — there is nothing downstream of it in `libnrt`.

---

## 4. The Error-Reporting Subsystem

### Purpose

The error path is what feeds the *error* counters the bridge (§3) writes. A hardware TPB error notification is decoded, in three table lookups, into a public `NRT_STATUS`; that code is then either bridged into an NDS counter (bridge #1) or, for the software subtypes, persisted directly from the infer-error flags (bridge #2). This section owns the *decode* and the *subtype table*; the full `NRT_STATUS` value space, the `nrt_get_status_as_str` string map, and the `nrt_get_status_priority` classifier are owned by [Error and Status Codes](../runtime/error-codes.md) and are **not** re-derived here.

### Entry Point

```text
TPB ERROR-ring entry (NQ_TYPE_ERROR, NEURON_ISA_TPB_ERROR_TYPE)   [drained by userspace]
  └─ v2_error_get_infer_error_subtype (0x321760)        ── remap notif idx → INFER_ERROR_SUBTYPE
       via sunda_error_subtypes (0x9e0fa0, 12×u32)
  └─ error_get_infer_error_subtype_str (0x3217f0)       ── subtype → string (error_subtypes_str @0xc095a0)
  └─ v2_infer_error_get_sequencer_error_text (0x321720)  ── sequencer subtype → text (clamps idx>0x80)
  ─► flags packed into kbl_infer_errors.infer_error_flags[9]
  ─► exec path collapses flags → NRT_STATUS (1003 / 1004 / 1200-series)
  ─► kmetric_update_nds_{generic_status,error_stats}    [§3]
```

### Algorithm — the infer-error-subtype decode

The decode is a single table remap from the raw hardware notification index to the libnrt-internal `INFER_ERROR_SUBTYPE`. `v2_error_get_infer_error_subtype @0x321760` indexes `sunda_error_subtypes @0x9e0fa0` (12 × u32) with the notification index `0..0xb` and returns the subtype value.

```c
// Models v2_error_get_infer_error_subtype @0x321760  — the L3→L2 remap.
// sunda_error_subtypes @0x9e0fa0 (12 × u32):  {0,1,0,0,3,4,5,6,7,2,8,0}
//   index = NEURON_ISA_TPB_ERROR_TYPE notification index (0..11)
//   value = INFER_ERROR_SUBTYPE  (the 0..8 taxonomy below)
int v2_error_get_infer_error_subtype(unsigned notif_idx) {
    if (notif_idx > 0xb) notif_idx = 0;            // out-of-range clamps to NONE
    return sunda_error_subtypes[notif_idx];        // @0x9e0fa0  (mov (rax,rcx,4))
}

// Models error_get_infer_error_subtype_str @0x3217f0 — index → string.
const char *error_get_infer_error_subtype_str(unsigned subtype) {
    if (subtype > 8) subtype = 0;                  // clamp to NONE; table has 9 entries
    return error_subtypes_str[subtype];            // .data @0xc095a0  (mov (rax,rdi,8))
}
```

The exec path then collapses the packed flags into a public code: numerical subtypes → `NRT_EXEC_COMPLETED_WITH_NUM_ERR(1003)`; the memory/fake/semaphore/event/psum/sequencer subtypes → `NRT_EXEC_COMPLETED_WITH_ERR(1004)`; the specific HBM/SRAM-UE/DMA-abort/NQ-overflow faults → the `1200`-series. (The exact `mov $code,%eax` emitter site is not individually pinned; MED — the chain is established, the per-code emitter is owned by the error-taxonomy deep dive.)

### The error-subtype table

`INFER_ERROR_SUBTYPE` (DWARF enum `@<39f45>`, byte-size 4; string pool `error_subtypes_str @0xc095a0`). The subtype is the L2 join between the hardware TPB taxonomy and the public `NRT_STATUS`; the `NRT_STATUS` column is **referenced** from the [code catalogue](../runtime/error-codes.md), not owned here.

| Subtype | Index | Meaning | `NRT_STATUS` mapping | Origin (TPB notif) | Conf |
|---|---|---|---|---|---|
| `NONE` | 0 | no per-inference error | `NRT_SUCCESS(0)` | default / no notification | HIGH |
| `NUMERICAL` | 1 | NaN/Inf in output | `NRT_EXEC_COMPLETED_WITH_NUM_ERR(1003)` | `FP_NAN` (notif 1) → remap | HIGH |
| `TRANSIENT` | 2 | retryable seq-nonfatal | `1004` (collapsed) | `SEQUENCER_NONFATAL` (notif 9) | HIGH |
| `MEMORY_ERROR` | 3 | on-chip memory fault | `NRT_EXEC_COMPLETED_WITH_ERR(1004)` | `MEMORY_ERROR` (notif 4) | HIGH |
| `SW_FAKE_ERROR` | 4 | injected test fault | `1004` (collapsed) | `FAKE_ERROR` (notif 5) | HIGH |
| `SW_SEMAPHORE_ERROR` | 5 | semaphore protocol error | `1004` → **NDS_EXT 36** | `SEMAPHORE_ERROR` (notif 6) | HIGH |
| `SW_EVENT_ERROR` | 6 | event protocol error | `1004` → **NDS_EXT 37** | `EVENT_ERROR` (notif 7) | HIGH |
| `SW_PSUM_COLLISION_ERROR` | 7 | PSUM write collision | `1004` → **NDS_EXT 38** | `PSUM_COLLISION` (notif 8) | HIGH |
| `SW_SEQUENCER_ERROR` | 8 | sequencer fatal | `1004` → **NDS_EXT 39** | `SEQUENCER_FATAL` (notif 10) | HIGH |
| `MAX` | 9 | enum sentinel | — | — | HIGH |

> **QUIRK — the remap is not the identity.** `sunda_error_subtypes` is `{0,1,0,0,3,4,5,6,7,2,8,0}`, so the hardware notification index and the `INFER_ERROR_SUBTYPE` value do **not** coincide: notif `9` (`SEQUENCER_NONFATAL`) maps to subtype `2` (`TRANSIENT`), notif `10` (`SEQUENCER_FATAL`) maps to subtype `8` (`SW_SEQUENCER_ERROR`), and notifs `0/2/3/11` all map to `0` (`NONE`). A reimplementer that treats the TPB notification index as the subtype directly will mis-bin every sequencer and FP error. The per-arch remap table is `sunda` here; other arches have their own tables (owned by the ISA error deep dive).

> **NOTE — the kernel does not classify the ring.** The `NQ_TYPE_ERROR` ring is allocated and programmed by the kernel (the notification-queue engine), but its per-entry TPB error type is parsed *in userspace* by the decode above. The kernel's only error-side role is owning the counter ids these decoded errors increment into. The sysfs status-counter taxonomy that mirrors these (`"hw_error"`, `"execute_sw_semaphore_error"`, …) is **owned by** [kernel/sysfs](../kernel/sysfs.md) — cross-linked, not duplicated here.

---

## 5. The Kernel CloudWatch Aggregation Engine

### Purpose

`neuron_metrics.c` implements the per-device `ndN metrics` kthread that reads the NDS counter slabs the runtime wrote, differentiates them against a per-session baseline, packs the result into a ≤128-byte CloudWatch byte-stream, and posts it to firmware over FW-IO. It is the consumer of everything §2–§4 produced and the only actor that ever talks to CloudWatch. A reimplementer rebuilds the registry (`nmetric_defs[]`), the differential counter model, the two-tick budget split, and the wire record.

### The registry

The single source of truth is `nmetric_defs[]` (`neuron_metrics.c:172-243`, 41 rows). Each row is a `nmetric_def_t` (`neuron_metrics.h:90-98`, seven `u8` fields): `{index, type, count, tick, cw_id, ds_id, flags}` — binding a metric to its type-specific aggregate/post path, its CloudWatch id (`enum nmetric_cw_id`, `:42-170`), and its source NDS counter (`0xFF` = none / not in datastore). Rather than dump 41 rows, the table is described by its axes; the full per-row CW-id↔NDS-counter binding is owned by [kernel/sysfs](../kernel/sysfs.md).

| Axis | Values | Source |
|---|---|---|
| **type** (10) | `CONSTANT 0, VERSION 1, COUNTER 2, FW_IO_ERR 3, BITMAP 4, CONSTANT_U64 5, DRIVER_RESET 6, DRIVER_USERVER 7, UTILIZATION 8, ECC_ERR_COUNTER 9` | `neuron_metrics.h:23-32` |
| **tick** (3) | `POST_TIME_TICK_0 0, POST_TIME_TICK_1 1, POST_TIME_ALWAYS 0xF` | `neuron_metrics.h:18-21` |
| **cw_id** | `enum nmetric_cw_id` `11..253`; error band `200..253` (each row's comment names the `NRT_STATUS` it tracks) | `neuron_metrics.c:42-170` |
| **ds_id** | `NDS_NC_COUNTER_*` / `NDS_EXT_NC_COUNTER_*` / `NDS_ND_COUNTER_*`, or `0xFF` for non-datastore (driver-reset, FW-IO-err, ECC) | `share/neuron_driver_shared.h` |
| **flags** | `VERS_ALLOW_TYPE 1` (version also posts framework type on `cw_id+1`); `CONST_U64 {SKIP_ZERO 1, PREFER_FREED 2}` | `neuron_metrics.h:34-37` |

Each of the 10 types has a dedicated aggregate path (the switch in `nmetric_aggregate_nd_counter_entry`, `:364`) and a mirror-image post path (the switch in `nmetric_post_metrics`, `:856`).

### Algorithm — the per-tick pipeline

`nmetric_aggregate_and_post_tick @:1017` is the full pipeline for one slow tick. The critical property a reimplementer must preserve: **aggregation happens under the datastore lock; posting happens outside it.**

```c
// Models nmetric_aggregate_and_post_tick (neuron_metrics.c:1017).
void nmetric_aggregate_and_post_tick(struct neuron_device *nd, …, u8 tick) {
    u64 curr[30], feature_bm = 0, const_u64[2];     // thread-local accumulators

    neuron_ds_acquire_lock(&nd->datastore);                              // :1019  [datastore/kernel-side]
        nmetric_full_aggregate(nd, curr, &feature_bm, const_u64, tick);  // :1020
        //   for each in-use slab (NEURON_MAX_DATASTORE_ENTRIES_PER_DEVICE):  :406
        //     switch(type):
        //       COUNTER/UTILIZATION: curr[idx] += Σ get_neuroncore_counter_value(entry,nc,ds_id)
        //                            over every nc in dev_nc_map        :376  [datastore read]
        //       VERSION:  nmetric_aggregate_version_metrics (LFU over 8 slots)  :303
        //       BITMAP:   feature_bm |= NDS_ND_COUNTERS[ds_id]
        //       CONSTANT_U64: const_u64[idx] = NDS_ND_COUNTERS[ds_id]   (last writer wins)
        //   FW_IO_ERR special: curr[17] = fw_io_get_err_count(nd->fw_io_ctx)    :412  [FW-IO]
        nmetric_cache_shared_bufs(nd, freed, versions, …, tick);         // :1022
        //   snapshot+reset ds_freed_* (process-death capture) for THIS tick, so a freed
        //   counter is consumed exactly once; refresh perf-profile constant     :954
    neuron_ds_release_lock(&nd->datastore);                              // :1025  (POST is outside the lock)

    nmetric_post_metrics(nd, curr, prev, freed, …, tick);                // :838  (see below)
    nmetric_start_new_session(nd, curr, prev, freed, …, tick);           // :973  prev := curr (baseline)
}
```

### Algorithm — differential post and the wire record

`nmetric_post_metrics @:838` is the mirror switch that turns each accumulator into a packed CW record. The counter post is differential and skip-on-no-change; the packed buffer is capped at 128 bytes per tick.

```c
// Models nmetric_post_counter (neuron_metrics.c:608) and nmetric_post_metrics (:838).
// Wire record: struct nmetric_cw_metric { u8 id; u8 len; u8 data[]; } __packed (:254-258)
//   data is ASCII decimal/hex, NO trailing NUL on the wire.
int nmetric_post_counter(u64 curr, u64 prev, u64 freed,
                         const nmetric_def_t *m, u8 *dest, int avail) {
    u64 crt = curr + freed;                          // live + dying-process-captured counts
    if (crt <= prev) return 0;                       // :617  overflow / no-change -> skip entirely
    char ascii[…]; int n = sprintf(ascii, "%llu", crt - prev);  // delta since last post
    int sz = 2 + n;                                  // id:u8 + len:u8 + n data bytes
    if (avail < sz) return 0;                        // budget guard; later smaller metrics may still fit
    dest[0] = m->cw_id; dest[1] = (u8)n;
    memcpy(dest + 2, ascii, n);
    return sz;
}

// nmetric_post_metrics tail (:896-900):
//   if (log_posts & 2) nmetric_mock_fw_io_post_metric(buf, size);     // -> trace_metrics_post (ftrace)
//   if (data_size && (log_posts & 1))
//       ndhal->ndhal_fw_io.fw_io_post_metric(nd->fw_io_ctx, buf, data_size);  // :900  POST_TO_CW
```

### The two-tick budget and the periodic engine

`nmetric_thread_fn @:1047` is the kthread body. It samples power every 50 ms and posts metrics on a drift-free slow tick (~2.5 min). The 128-byte CW budget is split across **two** ticks (`POST_TICK_COUNT = 2`, `neuron_metrics.h:21`): each metric carries a `tick` field (`TICK_0`/`TICK_1`/`ALWAYS`), the thread alternates `tick = (tick+1) % 2`, and `ALWAYS` metrics post on every tick. This is why the error counters split across the two ticks — the `TICK_0` band carries the `0..15`-derived counters and the `TICK_1` band carries the extended HW/SW error counters.

```c
// Models nmetric_thread_fn (neuron_metrics.c:1047).
int nmetric_thread_fn(void *arg) {
    struct neuron_device *nd = arg;
    u64 sample_jiffies = msecs_to_jiffies(50);        // :1074  nmetric_metric_sample_delay
    u64 post_jiffies   = msecs_to_jiffies(150000);    // :1075  nmetric_metric_post_delay (~2.5 min)
    u8  tick = 0;

    while (!kthread_should_stop() && state != STOPPED) {
        wait_event_interruptible_timeout(wq, state==STOPPED||state==RESUMING, sample_jiffies);
        if (nd->device_state != NEURON_DEVICE_STATE_READY) continue;        // :1094 gate
        nmetric_sample_high_freq(nd);                  // :1099 -> npower_sample_utilization (every 50ms)

        u64 slow_tick = (jiffies - start_jiffies) / post_jiffies;            // :1104 drift-free count
        if (state == PAUSED) continue;                                       // :1107 skip slow post
        if (slow_tick != last_logged_slow_tick) {
            int budget = (state == RESUMING)                                 // :1115 catch-up
                       ? min(slow_tick - last_logged_slow_tick, POST_TICK_COUNT)
                       : 1;
            for (int i = 0; i < budget; i++) {
                nmetric_aggregate_and_post_tick(nd, …, tick);                // the per-tick pipeline
                tick = (tick + 1) % 2;                                        // :1126 alternate the band
            }
            last_logged_slow_tick = slow_tick;
        }
    }
    return 0;
}
```

### Out-of-band counter sources

Two metric families bypass the NDS slab entirely:

- **Process-death capture** — when a process's datastore slab is freed, `neuron_ds.c:161` calls `nmetric_partial_aggregate @:420`, rolling the dying process's counters into the device-level `ds_freed_*` buffers with `tick = POST_TIME_ALWAYS` so they are not lost before the next post. These are the `freed` term in `crt = curr + freed − prev`; `nmetric_cache_shared_bufs` (`:916`) snapshots and resets them per tick so each is consumed exactly once.
- **Driver-reset metrics** — max/avg device & TPB reset time and fail counts live only in `struct nmetric_driver_metrics` as `atomic64_t` arrays (NOT in the datastore), written lock-free by `neuron_reset.c` (`nmetric_set_reset_time_metrics @:1196`, `nmetric_increment_reset_failure_count @:1233`) and read+reset at post time via `atomic64_xchg(…, 0)` (`nmetric_post_and_reset_driver_metrics @:714`).

> **GOTCHA — the budget-exhaustion warning does not stop the loop.** When `available_size <= 0`, `nmetric_post_metrics` (`:852`) only `pr_err_once` and does **not** `break`/`continue`; the subsequent post helpers each re-check `avail < metric_size` and return `0`. So no overflow occurs, but the warning can fire while later, smaller metrics are still successfully written into the buffer. A reimplementer who treats the warning as "buffer full, stop" will drop metrics that would otherwise have fit. (Behavior is safe-by-construction but subtle.)

> **NOTE — ECC error counts are read live, not from the datastore.** `nmetric_post_driver_ecc_metrics @:787` reads HBM UE / repairable-UE counts via `ndhal->ndhal_sysfs_metrics.nsysfsmetric_get_hbm_error_count()` — the **same** helper the sysfs `mem_ecc_uncorrected` attribute uses — subtracts `ecc_prev[index]`, and guards underflow by logging warn-once and posting `0` (`:809-816`). The shared reader is deliberate: the CW metric and the sysfs attribute must report a consistent ECC count. The sysfs side is owned by [kernel/sysfs](../kernel/sysfs.md).

### Periods and module parameters

| Constant | Value | file:line | Role |
|---|---|---|---|
| `nmetric_metric_post_delay` | `150000` ms (~2.5 min) | `:23` (module_param `:27`) | slow-tick interval (`post_jiffies`, `:1075`) |
| `nmetric_metric_sample_delay` | `50` ms | `:24` (NOT a module_param) | loop wait / power-sample cadence (`:1074`) |
| `nmetric_log_posts` | `1` (bit0=CW, bit1=trace) | `:25` (module_param `:30`) | post sink select (`:896,899`) |
| `POST_TICK_COUNT` | `2` | `neuron_metrics.h:21` | budget split + modulo (`:1118,1125`) |
| `NEURON_METRICS_MAX_POSTING_BUF_SIZE` | `128` | `neuron_metrics.h:14` | per-tick CW budget; `posting_buffer[129]` (`h:143`) |

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_profile_stop_internal @0xb2cd0` | the NTFF finalizer (§2) — offline trace, *not* the CW metrics path |
| `kmetric_update_nds_*` `@0xe0cd0..0xe10a0` | the runtime counter bridges (§3) — the only `libnrt` writers of NDS error counters |
| `v2_error_get_infer_error_subtype @0x321760` | the L3→L2 remap (§4) producing the subtypes the bridge persists |
| `nmetric_thread_fn` (`neuron_metrics.c:1047`) | the kernel kthread (§5) that aggregates and posts; the only CloudWatch actor |
| `fw_io_post_metric` (`:900`) | the FW-IO POST_TO_CW sink; `fw_io_get_err_count` (`:412`) the FW_IO_ERR metric source |

## Cross-References

- [Kernel CloudWatch Metrics Engine](../kernel/metrics.md) — the full `neuron_metrics.c` aggregation engine, the 41-row `nmetric_defs[]` registry, the differential counter model and tick scheduling this page summarizes
- [Sysfs Metrics Tree](../kernel/sysfs.md) — the metric-id taxonomy (`nsysfsmetric` counter namespace, `MAX_METRIC_ID`) and the named status-counter attributes (`"success"`, `"hw_error"`, `"execute_sw_semaphore_error"`, …) that mirror the NDS counters this page's bridge writes — **owns** the metric_id table this page cross-links but does not re-derive
- [Error and Status Codes (NRT_STATUS)](../runtime/error-codes.md) — the full `NRT_STATUS` value space, the six-layer error model, the `nrt_get_status_as_str`/`nrt_get_status_priority` decoders, and the `CSWTCH.6`/`+0x1f` bridge tables — **owns** the code table referenced in §3/§4
- [Kernel Side (Per-Process Slabs)](../datastore/kernel-side.md) — the NDS slab the runtime publishes counters into and the kernel reads back (`get_neuroncore_counter_value`, `neuron_ds.c:207`) during aggregation
- [Overview: the Three Trace Producers](overview.md) — the section map that places this metrics plane as divergent from the NTFF trace convergence (`kmetric_* → tdrv_nds_* → NDS`)
- [The System Monitor and Debug Stream](system-monitor.md) — the host cpu/mem sampler and the status-string debug decoders sibling to this error path
- [back to index](../index.md)
