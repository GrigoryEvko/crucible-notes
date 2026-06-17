# The Inspect / Profile API

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.1`, version namespace `NRT_2.0.0`, NTFF package `KaenaProfilerFormat-2.31.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata` VMA equals file offset, so every `0x9…`/`0xa…`/`0xb…` is both an analysis VMA and a file offset. The orchestration is first-party C++ from `/opt/workspace/KaenaRuntime/nrt/` — TUs `nrt_inspect.cpp`, `nrt_inspect_config.cpp`, `nrt_profile.cpp` (GNU C++17 14.2.1, `-O2 -fPIC`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every function is pinned to a `.symtab` symbol + `.text` address; struct offsets come from DWARF-derived `structures.json` ordinals cross-checked against `operator new` sizes and decompiled store sequences; every `.rodata` literal is byte-verified. The `nrt_inspect_options_t` internal field semantics are `[MED]` (offsets are byte-accurate, names assigned by analogy to the config setters). · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

This page owns the **host-side orchestration** of the Neuron runtime's profiler — the C++ control plane that configures a device profile, captures it, and emits NTFF. It is the umbrella that sits above the three trace producers documented in [overview](overview.md): the public `nrt_inspect_*` session API, the `nrt_inspect_config_*` options bag and its activity string↔enum mapping, the per-execute device-profile capture path (`nrt_profile_*`, both MODEL- and SESSION-keyed), and the continuous-profiler options. It does **not** own the on-disk wire format — that 128-byte header, the `ntff_info` proto, and the `notification_type → trace_type` map are owned by [ntff-format](ntff-format.md) and cross-checked by [ntff-wire-tables](ntff-wire-tables.md); this page links them and stops at the serializer call.

The familiar reference frame is a **session-scoped profiler controller with a fan-out arm and a fused harvest** — the shape of `perf record` opening a session, arming several event sources, then draining them all at teardown into one output tree. Here the controller is `g_inspect_context` (432 B, `operator new 0x1B0`, @`0xc5c8b8`, guarded by the rwlock `global_lock` @`0xc5c880`). `nrt_inspect_begin_with_options` @`0x99050` allocates it, installs crash signal handlers, resolves a per-instance output directory, then fans out by an activity bitmask: bit0 arms the Rust sys_trace ring, bit3 arms the host cpu/mem sampler, and `device_profile_mode == SESSION` arms a per-NeuronCore device profile. The mirror image is `nrt_inspect_stop` @`0x9f130` — a 10.5 KB / 410-basic-block **fused harvest+serialize** point (there is no separate "collect" step): per NeuronCore it stops and serializes the device-profile session, then dumps the system-profile snapshot (`cpu_util.pb` / `host_mem.pb` / `ntrace.pb` / `trace_info.pb`) into the output tree.

Underneath the session sits a second, independently usable surface: the **device-profile capture engine** (`nrt_profile.cpp`). It has two API families that share one collection engine — MODEL-keyed (`nrt_profile_start` / `..._stop`, one profile per `filename→model`) and SESSION-keyed (`nrt_profile_session_start` / `..._stop` / `..._serialize`, one profile per visible vNC). The SESSION family is the one the inspect session drives; the MODEL family is the per-execute path armed by `nrt_inspect_device_profile_start` @`0x99840` on each `nrt_execute`. Both terminate at `nrt_profile_serialize` @`0xb2670`, which writes the per-NC `.ntff` file. This page documents the orchestration of both — the begin/arm path, the per-execute MODEL capture, the SESSION harvest+stash, the config object lifecycle, and the four output-dir/continuous-option traps a reimplementer will hit.

For reimplementation, the contract is:

- **The session controller** — `inspect_context_t` (432 B), its activity bitmask (`nrt_inspect_activities_t`), the begin fan-out (sys_trace / system-monitor / per-NC device profile, with rollback), and the fused `nrt_inspect_stop` harvest+serialize.
- **The two device-profile families on one engine** — MODEL-keyed (`requested_model_profiles` map, per-execute arm/stop) vs SESSION-keyed (`pending_serialization` stash keyed by a returned `session_id`), and the deferred harvest→stash→serialize split that *is* the "collect" phase.
- **The config object** — `nrt_inspect_config_t` (624 B) embedding a `nrt_sys_trace_config_t` (312 B), the allocate/free/setter family, and the activity name↔enum hash map populated by the TU static initializer.
- **The output-dir resolution + NTFF record map** — `instance_output_dir` templating (`<base>/<instance_id>-<pid>/<session_init_time_ns>`), the per-model `ntff_record_map`, and the on-fail "./tmp" salvage rename.

| | |
|---|---|
| **Session controller** | `inspect_context_t` (432 B = `0x1B0`) — `g_inspect_context` @`0xc5c8b8`, rwlock `global_lock` @`0xc5c880` |
| **Begin / arm** | `nrt_inspect_begin` @`0x997e0` → `nrt_inspect_begin_with_options` @`0x99050` |
| **Stop (fused harvest+serialize)** | `nrt_inspect_stop` @`0x9f130` (10573 B, 410 BB) · crash flush `nrt_inspect_signal_handler` @`0xa1a80` · close `nrt_inspect_close` @`0xa1c00` |
| **Config bag** | `nrt_inspect_config_t` (624 B = `0x270`) embeds `nrt_sys_trace_config_t` (312 B) @+48; `nrt_inspect_config_allocate` @`0xa8050` |
| **Device profile (MODEL)** | `nrt_inspect_device_profile_start` @`0x99840` → `nrt_profile_start` @`0xaee40` |
| **Device profile (SESSION)** | `nrt_profile_session_start` @`0xb6800` → `nrt_profile_session_stop` @`0xb1dd0` → `nrt_profile_session_serialize` @`0xb61d0` |
| **NTFF emit (sink)** | `nrt_profile_serialize` @`0xb2670` (128-B header + proto + raw blobs) — **format owned by [ntff-format](ntff-format.md)** |
| **Output tree** | `profile_nc_<n>_session_<id>.ntff` · `cpu_util.pb` · `host_mem.pb` · `ntrace.pb` · `trace_info.pb` |
| **Activity bitmask** | `SYSTEM_PROFILE=1` `DEVICE_PROFILE=2` `HOST_MEMORY=4` `CPU_UTIL=8` `ALL=255` |
| **Globals (.bss)** | `g_device_profile_context` @`0xc5d140` (96 B) · `session_ctx` @`0xc5c900` (2112 B) · `activity_mappings` @`0xc5c8c0` · `previous_signal_handlers` @`0xc5c840` |

---

## 1. The Session Controller

### Purpose

`inspect_context_t` is the runtime-side session object — the live counterpart to the public `nrt_inspect_config_t` options bag. It is allocated once (`operator new 0x1B0`, lazily, on first `nrt_inspect_begin_with_options`), held at `g_inspect_context` @`0xc5c8b8`, and guarded by the process rwlock `global_lock` @`0xc5c880`. Everything the inspect API does reads or mutates this object under that lock. It carries the resolved output directory, the enabled-activity bitmask, the device-profile mode, a 256-entry per-NeuronCore capture bitmap, and the `ntff_record_map` that ties each in-flight MODEL-mode profile to its eventual `.ntff` filename.

The reference-frame analogy is a `perf_event` session descriptor: one object owns the enable mask, the per-CPU (here per-NC) gating, and the bookkeeping to drain every armed source at teardown. The divergence is that the activities are heterogeneous — a Rust ring, a C++ NQ drain, and a `/proc` sampler — so the controller does not own the rings; it owns the *decision* of which to arm and the *output tree* they all write into.

### Layout

```text
inspect_context_t   size 432 (0x1B0)   g_inspect_context @0xc5c8b8   (structures.json ord 14220)
  +0x000  1    std::atomic<bool>    inspect_enabled          ── master gate (read by every entry)
  +0x001  1    std::atomic<bool>    inspect_on_fail_enabled  ── NRT_INSPECT_ON_EXEC_FAIL; arms "./tmp" salvage
  +0x008  8    uint64               session_init_time_ns     ── output-dir leaf component (see §5)
  +0x010  8    uint64               session_close_time_ns
  +0x018  4    nrt_inspect_activities_t   enabled_activities ── bit0 sys_trace, bit1 device, bit2 host_mem, bit3 cpu_util
  +0x01c  4    nrt_inspect_device_profile_mode_t  device_profile_mode  ── DISABLED=0 MODEL=1 SESSION=2
  +0x020  32   std::string          output_dir               ── resolved base (SSO)
  +0x040  4    int                  session_id
  +0x048  56   unordered_map<string,ntff_record>  ntff_record_map  ── MODEL-mode: record_name → {name, complete}
  +0x080  40   pthread_mutex_t      ntff_record_lock
  +0x0a8  8    size_t               last_dump_num_events     ── snapshot delta bookkeeping
  +0x0b0  256  bool[256]            capture_enabled_for_nc   ── per-NC arm bitmap

ntff_record   size 40   (value type of ntff_record_map; ord 3335)
  +0x000  32   std::string          name      ── "<model_name>_vnc_<interned_model_id>.ntff" basename
  +0x020  1    bool                 complete  ── set by device_profile_stop_internal before serialize
```

### Entry Point

```text
nrt_inspect_begin (0x997e0)                              ── public, NRT_2.0.0
  └─ nrt_inspect_config_allocate (0xa8050)               ── alloc+default the options bag
       └─ nrt_config_parse_inspect_config (0x86760)      ── env-var parse [other cell]
  └─ nrt_inspect_begin_with_options (0x99050)            ── THE master arm
       ├─ operator new 0x1B0 → g_inspect_context         ── lazy, one-time
       ├─ install signal handlers (__cxa_guard, one-time) ── into previous_signal_handlers @0xc5c840
       ├─ get_output_dir (0x96e40) / instance_output_dir (0x97000)  ── resolve + mkdir -p
       ├─ [act & 1] nrt_sys_trace_start (0xb9800)        ── producer (2) [rust-capture]
       ├─ [act & 8] nrt_system_monitor_start(10000)      ── host cpu/mem sampler [system-monitor]
       └─ [mode==SESSION] per-NC nrt_profile_session_start (0xb6800)  ── producer (1)
            └─ on fail: nrt_profile_session_stop + nrt_profile_session_drop  ── rollback
  └─ nrt_inspect_config_free                             ── tail-cleanup
```

### Algorithm

`nrt_inspect_begin_with_options` is the one function a reimplementer must get right: it is the single arm point, and its fan-out, default-directory policy, and rollback are all subtle. The annotated logic:

```c
// nrt_inspect_begin_with_options(const nrt_inspect_options_t *opts)  // 0x99050, NRT_2.0.0
int nrt_inspect_begin_with_options(opts) {
    if (opts == NULL)                                   // "called with null options."
        return NRT_INVALID;                             // (2)

    wrlock(global_lock);                                // 0xc5c880

    if (g_inspect_context == NULL) {                    // lazy one-time construct
        g_inspect_context = operator new(0x1B0);        // 432 B inspect_context_t
        reset_context(g_inspect_context);               // single-bucket ntff_record_map init
    }

    // One-time crash-handler install, guarded by __cxa_guard.
    // signal() over a fixed signal array; prior handler saved per-signum.
    install_signal_handlers();                          // -> previous_signal_handlers map @0xc5c840

    ctx = g_inspect_context;
    ctx->session_id            = opts->session_id;       // opts+0
    ctx->inspect_enabled       = opts->enable_inspect;   // opts+360  [MED naming]
    ctx->inspect_on_fail_enabled = opts->enable_inspect_on_fail;  // opts+361
    ctx->device_profile_mode   = opts->device_profile_mode;       // opts+364
    ctx->enabled_activities    = opts->enabled_activities;        // opts+40
    memcpy(ctx->capture_enabled_for_nc, opts+368, 256);          // per-NC bitmap
    ctx->session_init_time_ns  = time_utils_current_timestamp_ns();

    // --- Output-directory policy (see §5) ---
    dir = opts->output_dir;                              // opts+8 (std::string)
    if (dir == "/dev/null")                              // "Tmp is not acceptable for output directory name."
        goto reject;                                     //   (logged via reset path)
    if (dir.empty()) {                                   // "provided inspect output directory is empty. Defaulting to %s"
        dir = "./output";
    }
    instance_output_dir(dir, &ctx->output_dir);          // 0x97000  -> "<dir>/<inst_id>-<pid>/<init_ns>"
    utils_mkdir_recursive(ctx->output_dir);              // mkdir -p

    // --- Activity fan-out ---
    if (ctx->enabled_activities & SYSTEM_PROFILE)        // bit0 == 1
        nrt_sys_trace_start(&opts->sys_trace_config);    // 0xb9800   producer (2)
    if (ctx->enabled_activities & CPU_UTIL)              // bit3 == 8
        nrt_system_monitor_start(10000 /*ms*/);          // host sampler, 10s cadence

    if (ctx->device_profile_mode == SESSION) {           // ==2
        for (nc in visible_vncs)
            if (ctx->capture_enabled_for_nc[nc]) {
                rc = nrt_profile_session_start(nc);       // 0xb6800  producer (1)
                if (rc != OK) {                           // rollback EVERY NC armed so far
                    for (prev in armed_so_far) {          // "Failed to cleanup session on NC %d
                        nrt_profile_session_stop(prev);   //  after failure to start on NC %d"
                        nrt_profile_session_drop(prev);
                    }
                    unlock(global_lock); return rc;
                }
            }
    }
    unlock(global_lock);
    return NRT_SUCCESS;
}
```

> **QUIRK —** the begin path rejects `"/dev/null"` *and* `"./tmp"` as output directories but accepts an empty string by silently defaulting it to `"./output"`. The rejection is logged with the string `"Tmp is not acceptable for output directory name."` even for the `/dev/null` case — the same reset path handles both, so the log message names only the `tmp` arm. A reimplementer mirroring the diagnostics must not assume the message text identifies which directory was rejected.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrt_inspect_begin` | `0x997e0` | public umbrella: allocate+parse config, set `enable_inspect=1`, tail-call `_with_options` | HIGH |
| `nrt_inspect_begin_with_options` | `0x99050` | master arm: alloc ctx, install signals, resolve dir, activity fan-out + rollback | HIGH |
| `nrt_inspect_stop` | `0x9f130` | fused harvest+serialize: per-NC session stop/serialize + system-profile snapshot | HIGH |
| `nrt_inspect_close` | `0xa1c00` | `nrt_inspect_stop` then `remove_all("./tmp")` iff `inspect_on_fail_enabled` | HIGH |
| `nrt_inspect_signal_handler` | `0xa1a80` | crash flush: log, `nrt_inspect_stop`, chain saved handler or re-raise | HIGH |
| `nrt_inspect_get_instance_output_dir` | `0x97b10` | public buffer API: `strncpy` instance dir into caller buffer; "Buffer too small…Need %zu" | HIGH |
| `get_output_dir` | `0x96e40` | `"./tmp"` if on-fail enabled else `ctx->output_dir` | HIGH |
| `instance_output_dir` | `0x97000` | template `<base>/<inst_id>-<pid>/<session_init_time_ns>` via `tdrv_get_instance_info` | HIGH |
| `nrt_inspect_device_profile_is_enabled` | `0x96fd0` | `ctx && inspect_enabled && (activities & DEVICE_PROFILE)` | HIGH |
| `nrt_inspect_on_exec_fail_enabled` | `0x96e20` | `ctx ? inspect_on_fail_enabled : false` | HIGH |

---

## 2. Device Profile — MODEL vs SESSION

### Purpose

The device-profile engine (`nrt_profile.cpp`) is the producer (1) capture path: it subscribes the per-NeuronCore device Notification Queues (NQs), lets the silicon fill them during execution, drains them at stop, wraps each drained blob in an `ntff::trace_info`, and serializes an `ntff_info` proto into a per-NC `.ntff` file. It exposes **two API families over one collection engine**:

- **MODEL-keyed** — one profile per `(filename → model)`. Armed per-execute by `nrt_inspect_device_profile_start` @`0x99840` (when `device_profile_mode == MODEL` and continuous profiling is *not* enabled). State lives in `g_device_profile_context` @`0xc5d140` (the `requested_model_profiles` map). Public knobs: `nrt_profile_start` @`0xaee40`, `nrt_profile_stop` (sibling cell @`0xb47c0`).
- **SESSION-keyed** — one profile per visible vNC, with a *deferred* harvest. `nrt_profile_session_start` @`0xb6800` marks a per-NC slot active; `nrt_profile_session_stop` @`0xb1dd0` harvests the NQ rings into an in-memory `ntff_data_t`, **stashes** it (keyed by a freshly-minted `session_id`) into the per-NC `pending_serialization` map, and returns that `session_id`; `nrt_profile_session_serialize` @`0xb61d0` later pops the stash and writes the file. This stash→pop split *is* the runtime's "collect" phase — there is no `nrt_profile_collect` symbol.

The SESSION family is the one the inspect umbrella drives (begin arms `session_start`, stop calls `session_stop`+`session_serialize`). The MODEL family is the older direct API, kept alive for the per-execute path.

> **QUIRK —** the two families are not a class hierarchy — they are two key spaces over **disjoint global state**. MODEL profiles live in `g_device_profile_context.requested_model_profiles` (a `std::map<string,nrt_model*>` @`0xc5d140+0`); SESSION profiles live in `session_ctx.nc_session[256]` (an array of `profile_session_nc_data_t*` @`0xc5c900+48`). A reimplementer cannot collapse them into one map: a `filename` keys a MODEL profile, a `(vnc, session_id)` pair keys a SESSION profile, and they harvest the same NQ rings through different bookkeeping.

### Per-NC SESSION state

```text
profile_session_context_t   size 2112   session_ctx @0xc5c900
  +0    pthread_mutex_t      global_lock
  +40   size_t               num_valid_vncs
  +48   std::atomic<profile_session_nc_data_t*>  nc_session[256]   ── one slot per vnc

profile_session_nc_data_t   size 256   operator new(0x100, align 64)   (ord 13778)
  +0x00  int32   vnc_idx
  +0x08  pthread_mutex_t  nc_lock
  +0x30  uint32  next_nc_session_id        ── monotonic; the stash KEY minted at stop
  +0x34  bool    session_active
  +0x38  atomic<int>   ref_count           ── init 1; release_nc_data does interlocked sub
  +0x3c  atomic<bool>  marked_for_deletion ── close() spins (sched_yield) until ref_count<=1
  +0x40  std::set<string>  seen_uuids      ── node-info uuid dedup
  +0x70  ntff_data_t  active_ntff          ── current capture (72 B)
  +0xb8  unordered_map<uint64,ntff_data_t>  pending_serialization  ── session_id → harvested data

ntff_data_t   size 72   (ord 3104)
  +0x00  ntff::ntff_info*  proto           ── operator new 0xE8; the NTFF message [ntff-format]
  +0x08  vector<pair<char*,uint>>  instruction_data     ── raw KBL instruction-stream blobs
  +0x20  vector<pair<char*,uint>>  trace_notifications  ── raw NQ blobs (byte offsets into .ntff)
  +0x38  profile_session_nc_expl_notifications_t  expl_notifs  ── per-TopSP CC status (16 B)
```

### Algorithm — SESSION harvest (the "collect" phase)

`nrt_profile_session_stop` is where capture becomes a serializable artifact. It drains every subscribed NQ ring, folds the explicit collectives notifications in, stashes the result, and hands back a `session_id` the caller serializes later.

```c
// nrt_profile_session_stop(int vnc, uint64 *session_id)  // 0xb1dd0, NRT_2.0.0
int nrt_profile_session_stop(vnc, out_session_id) {
    nc = nrt_profile_session_get_nc_data(vnc);          // 0xab3a0; ref_count++
    if (nc == NULL || !nc->session_active) {            // "No active profile session for NC %d"
        release_nc_data(nc); return NRT_INVALID;
    }
    lock(nc->nc_lock);
    nc->session_active = 0;

    // (1) Drain ALL device NQ rings into the active proto.
    rc = nrt_profile_notification_read_all_nc(vnc, nc->active_ntff.proto,
                                              &nc->active_ntff.trace_notifications,
                                              &offset);  // 0xb1850
    if (rc != OK)                                        // "Failed to read notifications from device for NC %d"
        log_warn(...);                                   //   (non-fatal: partial profile still stashed)

    // (2) Fold explicit per-TopSP CC status notifications into ntff::trace_info.
    for (topsp in nc->active_ntff.expl_notifs)           // tag nrt_profile_session_add_explicit_notifs
        add_explicit_trace_info(nc->active_ntff.proto, topsp);  // "unable to collect notifications for CC core %u…"

    // (3) Mint a session id and STASH the harvested data (move, not copy).
    sid = nc->next_nc_session_id++;                       // +0x30 monotonic
    nc->pending_serialization[sid] = move(nc->active_ntff);  // +0xb8 unordered_map
    *out_session_id = sid;

    nrt_profile_notification_unsubscribe_nc(vnc);        // 0xab020; release the NQ rings
    unlock(nc->nc_lock);
    release_nc_data(nc);                                  // 0xaf560: ref_count--; free if 0 && marked_for_deletion
    return NRT_SUCCESS;
}
```

`nrt_profile_session_serialize` @`0xb61d0` is the consumer of that `session_id`: it pops `pending_serialization[session_id]`, passes the `ntff_info` proto plus the raw instruction/notification vectors to `nrt_profile_serialize`, and writes the file. A missing id is reported as `"Unable to find profile session id %lu"`.

### Algorithm — `nrt_profile_notification_read_all_nc`

The drain that turns device rings into proto records. It walks the two notification-type lists (per-TPB then per-TopSP), reads each ring, and emits one `ntff::trace_info` per `(engine, type)`:

```c
// nrt_profile_notification_read_all_nc(vcore, ntff_info*, vec&, off&)  // 0xb1850
void read_all_nc(vcore, proto, blob_vec, offset) {
    // collected_tpb_notifications  @0x857140 : {TRACE=0, INFER_STATUS=3, EVENT=1, ERROR=2, DMA=4, THROTTLE=5}
    // collected_topsp_notifications@0x857128 : {TOPSP_TRACE=6, TOPSP_ERROR=8, TOPSP_EVENT=7}
    for (type in collected_tpb_notifications) {
        blob = nrt_profile_notification_read(vcore, type);          // 0xab400 [sibling cell]
        tt   = nrt_profile_convert_trace_type_from_ntff_params(type); // 0xaaf40 — map 0..10 → ntff 0..5 [ntff-format]
        if (tt == UNKNOWN) {                                        // "Unexpected notification type! Dropping…"
            continue;                                              //   silently drops unmapped types
        }
        ti = AddMessageLite<ntff::trace_info>(proto);              // protobuf RepeatedPtrField
        ti->set_trace_type(tt);
        blob_vec.push_back({blob.ptr, blob.len});                  // raw bytes; referenced by byte-offset from proto
    }
    for (type in collected_topsp_notifications) {
        idx = encd_arch_get_topsp_idx_for_worker(...);             // per-TopSP routing
        ... // same shape, per-TopSP engine
    }
}
```

> **GOTCHA —** the device→NTFF type mapping is **not** done here — `read_all_nc` delegates it to `nrt_profile_convert_trace_type_from_ntff_params` @`0xaaf40` (a sibling-cell jump table @`0x8570c0`), and the eleven device `notification_type` values (0..10) collapse to six `ntff::notification_trace_type` values (0..5) plus a `block_type` (0..2). Unknown types are **silently dropped** with only a debug-level log. A reimplementer who passes blobs through unmapped will produce `.ntff` files `neuron-profile` cannot parse. The full map is owned by [ntff-format](ntff-format.md).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrt_inspect_device_profile_start` | `0x99840` | MODEL per-execute arm; guards on continuous, records `ntff_record` | HIGH |
| `nrt_inspect_device_profile_stop` | `0x99040` | one-liner → `_stop_internal(model, save=1)` | HIGH |
| `nrt_inspect_device_profile_stop_internal` | `0x98540` | MODEL harvest: set `complete=1`, build path, `nrt_profile_stop_internal`; on-fail rename | HIGH |
| `nrt_ntff_get_record_name` | `0x97d00` | build `"<model>_vnc_<interned_id>.ntff"` basename via stringstream | HIGH |
| `nrt_ntff_get_record` | `0x983d0` | look up `ntff_record*` in `ntff_record_map` under `ntff_record_lock` | HIGH |
| `nrt_profile_start` | `0xaee40` | MODEL arm: `kmgr_set_profiling_status`, register filename→model, subscribe NQs | HIGH |
| `nrt_profile_stop_internal` | `0xb2cd0` | fused harvest+build+serialize; find+erase filename→model; `nrt_profile_serialize` | HIGH |
| `nrt_profile_session_start` | `0xb6800` | SESSION arm: reset per-NC state, alloc `ntff_info`, subscribe NQs | HIGH |
| `nrt_profile_session_stop` | `0xb1dd0` | SESSION harvest+stash; returns `session_id` | HIGH |
| `nrt_profile_session_serialize` | `0xb61d0` | pop stash by `session_id` → `nrt_profile_serialize` → write file | HIGH |
| `nrt_profile_notification_read_all_nc` | `0xb1850` | drain all NQ rings → `ntff::trace_info` per `(engine,type)` | HIGH |
| `nrt_profile_serialize` | `0xb2670` | write `.ntff`: 128-B header + proto + raw blob vectors — **[ntff-format]** | HIGH |
| `nrt_profile_session_add_node_info` | `0xb14f0` | per-execute append model node info (dedup via `seen_uuids`) | HIGH |
| `nrt_profile_session_append_cc_notifications` | `0xaf700` | per-execute append CC status bytes to `expl_notifs` | HIGH |

---

## 3. The Config Object

### Purpose

`nrt_inspect_config_t` is the public opaque options bag — the thing a caller allocates, mutates through ~17 setters, and hands to `nrt_inspect_begin_with_options`. It is 624 bytes (`operator new 0x270`) and **embeds** a 312-byte `nrt_sys_trace_config_t` at +48, so the inspect-config layer is largely a thin forwarder onto the host sys_trace capture config: the per-NC and per-event-type capture setters write *through* into the embedded struct. The config TU (`nrt_inspect_config.cpp`) also owns the activity name↔enum hash map.

### Layout

```text
nrt_inspect_config_t   size 624 (0x270)   operator new 0x270   (structures.json)
  +0     4    int                                session_id
  +8     32   std::string                        output_dir            ── SSO
  +40    4    nrt_inspect_activities_t           enabled_activities    ── bitmask
  +48    312  nrt_sys_trace_config_t             sys_trace_config      ── EMBEDDED (forwarders write here)
  +360   1    bool                               enable_inspect
  +361   1    bool                               enable_inspect_on_fail
  +364   4    nrt_inspect_device_profile_mode_t  device_profile_mode
  +368   256  bool[256]                          capture_enabled_for_nc

nrt_sys_trace_config_t   size 312   (embedded @+48; also standalone)
  +0     8    uint64                             max_events_per_nc            ── ring sizing
  +8     256  bool[256]                          capture_enabled_for_nc       ── mirror of inspect's @+368
  +264   46   bool[46]                           capture_enabled_for_event_type  ── the 46-variant taxonomy

nrt_profile_continuous_options_t   size 32 (0x20)   operator new 0x20
  +0     32   std::string                        output_dir            ── default "./output" (.rodata 0x83ea1a)
```

### Activity string ↔ enum mapping

The activity bitmask is exposed to callers as *names*, not bits. The TU static initializer `_GLOBAL__sub_I_nrt_inspect_config.cpp` @`0x74b10` interns the four activity names into the global hash map `activity_mappings` @`0xc5c8c0` (registered via `__cxa_atexit`); `nrt_inspect_config_activity_string_to_enum` @`0xa8340` resolves a name to its bit, and `nrt_inspect_config_set_activity` @`0xa85f0` OR-s the bit in (enable) or AND-NOT-s it out (disable).

| Activity name (`.rodata`) | Bit value | Enum | Arms (begin fan-out) | Confidence |
|---|---|---|---|---|
| `"system_profile"` @`0x83e578` | `1` | `SYSTEM_PROFILE` | `nrt_sys_trace_start` (producer 2) | HIGH |
| `"device_profile"` @`0x83e587` | `2` | `DEVICE_PROFILE` | per-NC `nrt_profile_session_start` (producer 1) | HIGH |
| `"host_memory"` @`0x83e596` | `4` | `HOST_MEMORY` | host mem sampling into `ntff::host_stats` | HIGH |
| `"cpu_util"` @`0x83e5a2` | `8` | `CPU_UTIL` | `nrt_system_monitor_start(10000)` (cpu/mem sampler) | HIGH |
| (composite) | `255` | `ALL` | — | HIGH |

> **NOTE —** the four interned activity names are **not** the 46-variant sys_trace event taxonomy. They are the coarse top-level *activities* (which producer to arm); the 46 event types are a finer per-event filter inside `nrt_sys_trace_config_t.capture_enabled_for_event_type[46]`, set by name through `nrt_sys_trace_config_set_capture_enabled_for_event_type` @`0xba0d0` (which builds a transient 46-entry name→enum map, hash seed `0xC70F6907`). The two name spaces are independent — see [event-taxonomy](event-taxonomy.md).

### Config Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrt_inspect_config_allocate` | `0xa8050` | `operator new(0x270, nothrow)`, zero, SSO-init `output_dir`, `set_defaults` | HIGH |
| `nrt_inspect_config_set_defaults` | `0xa7f60` | `session_id=1`, `output_dir=""`, activities=0, mode=DISABLED, per-NC bitmap all-1 | HIGH |
| `nrt_inspect_config_free` | `0xa80a0` | free heap `output_dir` (if not SSO), `operator delete(opts, 0x270)`; NULL-safe | HIGH |
| `nrt_inspect_config_set_output_dir` | `0xa8100` | `_M_replace output_dir`; NULL/empty → warn + clear | HIGH |
| `nrt_inspect_config_set_activity` | `0xa85f0` | name→enum then OR-in / AND-NOT the bit | HIGH |
| `nrt_inspect_config_activity_string_to_enum` | `0xa8340` | hash-lookup name in `activity_mappings` @`0xc5c8c0` | HIGH |
| `nrt_inspect_config_get_all_activity_types` | `0xa8650` | `calloc` array of `strdup`'d names of all map entries | HIGH |
| `nrt_inspect_config_get_enabled_activity_types` | `0xa8730` | like `_all`, filtered by `enabled_activities` | HIGH |
| `nrt_inspect_config_free_activity_types` | `0xa8870` | free each `strdup`'d name then the array | HIGH |
| `nrt_inspect_config_set_capture_enabled_for_nc` | `0xa8200` | write `capture_enabled_for_nc[idx]` **and** forward to sys_trace_config | HIGH |
| `nrt_inspect_config_set_capture_enabled_for_event_type_string` | `0xa8280` | forward name → `nrt_sys_trace_config_set_capture_enabled_for_event_type` | HIGH |
| `_GLOBAL__sub_I_nrt_inspect_config.cpp` | `0x74b10` | TU static ctor: intern the 4 activity names into `activity_mappings` | HIGH |

---

## 4. The Continuous Profiler Options

### Purpose

Continuous profiling is a toggle that makes every `nrt_execute` auto-capture a MODEL-keyed profile, saved on demand via `nrt_profile_continuous_save`. Its options object is a deliberately minimal 32-byte bag — just an output directory — and the arm/disarm toggles a single global flag.

```text
nrt_profile_context_t   size 96   g_device_profile_context @0xc5d140
  +0    std::map<string,nrt_model*>  requested_model_profiles   ── filename → model (MODEL key space)
  +48   pthread_mutex_t              lock
  +88   std::atomic<bool>            is_continuous_enabled
  +89   std::atomic<bool>            has_requested_profiling
```

### Algorithm — continuous save

```c
// nrt_profile_continuous_save(nrt_profile_continuous_options_t *options)  // 0xb47d0, NRT_2.0.0
int nrt_profile_continuous_save(options) {
    if (!g_device_profile_context.is_continuous_enabled)        // +88 atomic load
        return error("To use the nrt_profile_continuous_save() API you must first "
                     "call nrt_profile_continuous_start().");
    if (options == NULL || options->output_dir.empty())
        return NRT_INVALID;
    // Timestamped path, NOT the instance_output_dir template.
    path = options->output_dir + "/model_" + id + "_pid_" + pid
         + "_vnc_" + vnc + "_inst_" + inst
         + strftime("%Y%m%d_%H%M%S") + ".ntff";                 // .rodata fragments
    return nrt_profile_stop_internal(path, /*save=*/1);          // 0xb2cd0
}
```

> **QUIRK —** the continuous-save path builds a **different** filename scheme than the inspect session. The inspect session uses `instance_output_dir` (`<base>/<inst_id>-<pid>/<init_ns>/profile_nc_<n>_session_<id>.ntff`); continuous-save uses a flat `model_<id>_pid_<pid>_vnc_<vnc>_inst_<…>_%Y%m%d_%H%M%S.ntff` directly under the options output dir. A reimplementer who assumes one naming convention will mis-locate continuous profiles. Continuous profiling is also **mutually exclusive** with inspect device-profiling: `nrt_profile_continuous_start` @`0xacee0` rejects if `NEURON_RT_INSPECT_DEVICE_PROFILE` is set ("Please disable inspect device profiling…"), and `nrt_inspect_device_profile_start` @`0x99840` conversely refuses to arm if continuous is already on ("Continuous profiling is already enabled…").

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrt_profile_continuous_options_allocate` | `0xacda0` | `operator new(0x20, nothrow)`, init `output_dir="./output"` | HIGH |
| `nrt_profile_continuous_options_free` | `0xace00` | free heap `output_dir`, `operator delete(opts, 0x20)`; NULL-safe | HIGH |
| `nrt_profile_continuous_options_set_output_dir` | `0xace40` | `_M_replace`; NULL/empty → "Cannot set output dir to NULL or empty string." | HIGH |
| `nrt_profile_continuous_start` | `0xacee0` | set `is_continuous_enabled=1`; reject if inspect-profile / implicit-async / NULL opts | HIGH |
| `nrt_profile_continuous_stop` | `0xacf90` | clear flag, unsubscribe NQs per model, free `requested_model_profiles` | HIGH |
| `nrt_profile_continuous_save` | `0xb47d0` | timestamped path → `nrt_profile_stop_internal(path, 1)` | HIGH |
| `nrt_profile_continuous_is_enabled` | `0xacec0` | atomic load of `is_continuous_enabled` | HIGH |

---

## 5. Output-Directory Resolution and the NTFF Record Map

### Purpose

The output directory is not a literal: it is templated per process instance so concurrent runs on the same host do not collide. `instance_output_dir` @`0x97000` builds the leaf from the device instance id, the PID, and the session-init timestamp; `get_output_dir` @`0x96e40` switches the *base* to a salvage directory when on-fail mode is armed.

```c
// instance_output_dir(const std::string &base, std::string &out)  // 0x97000
NRT_STATUS instance_output_dir(base, out) {
    rc = tdrv_get_instance_info(&info);                  // instance_id, pid
    if (rc != OK) return rc;                             // "Failed to get instance info"
    out = base + "/" + info.instance_id + "-" + to_string(getpid());
    out += "/" + to_string(g_inspect_context->session_init_time_ns);  // +0x008
    return NRT_SUCCESS;
}
```

### The MODEL record map and the on-fail salvage

MODEL-mode capture records each in-flight profile's eventual `.ntff` basename in `ctx->ntff_record_map` (keyed by the record name, value `{name, complete}`). At arm (`nrt_inspect_device_profile_start`) the record is inserted with `complete=0`; at stop (`nrt_inspect_device_profile_stop_internal` @`0x98540`) it is set `complete=1`, the path is built, and `nrt_profile_stop_internal` writes it. The `save` flag controls the disposition:

```c
// nrt_inspect_device_profile_stop_internal(model, uint8 save)  // 0x98540
int stop_internal(model, save) {
    rec = nrt_ntff_get_record(model);                    // 0x983d0; under ntff_record_lock
    if (rec == NULL || rec->complete) {                  // "Trying to stop already completed profile"
        return ...;                                      //   (skip double-stop)
    }
    rec->complete = 1;
    path = ctx->output_dir + rec->name;                  // "<out>/<model>_vnc_<id>.ntff"
    rc = nrt_profile_stop_internal(path, save);          // 0xb2cd0

    if (save == 0) {                                     // discard: per-execute success, no profile wanted
        ntff_record_map.erase(rec->name);
        remove(path);                                    // delete the temp .ntff
    } else if (inspect_on_fail_enabled) {                // salvage: move produced files into "./tmp"
        mkdir(fail_dir);
        for (f in directory_iterator(ctx->output_dir))   // "Move %s to %s"
            std::filesystem::rename(f, fail_dir / f.name);
    }
    return rc;
}
```

> **GOTCHA —** the on-fail salvage uses `std::filesystem::rename` across a `directory_iterator` of the *whole* output directory, not just the one profile file. If `inspect_on_fail_enabled` is set and the output dir holds files from other NCs or prior runs, they are swept into `./tmp` too. The salvage base `"./tmp"` is *also* one of the two directory names `nrt_inspect_begin_with_options` *rejects* as an explicit output dir — so a reimplementer cannot configure the output dir to be the salvage dir; the runtime reserves `./tmp` for on-fail rescue only. `nrt_inspect_close` @`0xa1c00` is the counterpart: it calls `nrt_inspect_stop`, then `remove_all("./tmp")` iff on-fail was enabled, cleaning the scratch tree.

### `nrt_inspect_stop` — the fused harvest+serialize

The teardown is one function (no separate collect step). Per visible NC it stops and serializes the device-profile session; then, unless the output is `/dev/null`, it dumps the system-profile snapshot:

```c
// nrt_inspect_stop(...)  // 0x9f130 — 10573 B, 410 BB
int nrt_inspect_stop() {
    for (nc in visible_vncs) {
        rc = nrt_profile_session_stop(nc, &sid);          // 0xb1dd0  harvest+stash
        if (rc) log("Failed to stop device profile session on NC %d");
        out = get_output_dir();                           // 0x96e40
        path = out + "/profile_nc_" + nc + "_session_" + sid + ".ntff";
        nrt_profile_session_serialize(sid, path);         // 0xb61d0 → nrt_profile_serialize
    }
    nrt_sys_trace_get_gid_map();                          // 0xb9b40 — worker_gid resolution
    if (out != "/dev/null") {                             // "Not dumping … output location is /dev/null"
        // dump_system_profile_snapshot (timed)
        datafile_init(ctx, "cpu_util.pb");                // 0x9ac30 ×3
        datafile_init(ctx, "host_mem.pb");
        datafile_init(ctx, "ntrace.pb");
        dump_trace_info(file, ctx);                       // 0x9d610 — drains sys_trace ring
                                                          //   → event_to_proto (0x9aec0) per event
                                                          //   → ntff::ntrace_event / host_stats
        write("trace_info.pb");
        log("Dumped system profile snapshot in %.2f seconds");
    }
    return NRT_SUCCESS;
}
```

> **NOTE —** `nrt_inspect_stop` is reached three ways: normally via `nrt_close → nrt_inspect_close`; on crash via `nrt_inspect_signal_handler` @`0xa1a80` (installed into `previous_signal_handlers` @`0xc5c840`, which logs "Interrupt signal (%d) received. Finishing saving profiling data…", flushes, then chains the saved prior handler or re-raises); and at process exit via the `nrt_inspect_init_cleanup_handler` atexit destructor. All three converge on the same fused harvest — there is no path that collects without serializing. The system-trace ring drain (`dump_trace_info`) and the `event_to_proto` per-event mapper are the producer-(2)→NTFF seam, owned by [event-taxonomy](event-taxonomy.md), and the snapshot file derivation by [system-monitor](system-monitor.md).

---

## Cross-References

**The on-disk format this API emits (do not re-derive — link)**
- [NTFF Trace File Format (ntff.proto)](ntff-format.md) — the 40-message `ntff::` schema, the 128-byte `.ntff` container header, and the `notification_type → notification_trace_type + block_type` map that `read_all_nc` delegates to
- [NTFF Wire Tables (TcParseTable Decode)](ntff-wire-tables.md) — the table-driven wire encoding `nrt_profile_serialize` produces (independent schema cross-check)

**The producers this API arms and harvests**
- [Overview: the Three Trace Producers](overview.md) — the producer/consumer split, the NTFF convergence, and where the inspect session sits as the umbrella controller
- [The System Monitor and Debug Stream](system-monitor.md) — the host cpu/mem sampler (activity bit3) that `dump_trace_info` folds into `ntff::host_stats` → `cpu_util.pb` / `host_mem.pb`
- [SysTraceEventType Taxonomy](event-taxonomy.md) — the 46-variant event filter inside `nrt_sys_trace_config_t` and the `event_to_proto` mapper called from `nrt_inspect_stop`
- [sys_trace Capture Engine](rust-capture.md) — the Rust ring `nrt_sys_trace_start` arms (producer 2 engine)
- [back to index](../index.md)
