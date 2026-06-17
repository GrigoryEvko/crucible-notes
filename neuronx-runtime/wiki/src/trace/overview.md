# Profiling, Trace & Telemetry — Section Map

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.1`, NTFF package `KaenaProfilerFormat-2.31.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata` VMA equals file offset. The Rust producer is statically linked from `rustc 1.91.1` (sysroot tag `/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/`, confirmed in `.rodata`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every producer entry point is pinned to a `.symtab` symbol + `.text` address; the three-producer split, the convergence on NTFF, and the divergent metrics plane are cross-checked against the `nm`/`functions.json`/`callgraph.json` exports and the consolidated trace facets (`P1-F-TRACE`, `P1-L-PROF-01`, `P2-W2-RUST-SYSTRACE`). · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

> **CORRECTION —** an earlier revision gave the soname as `libnrt.so.2`. The ELF `DT_SONAME` is `libnrt.so.1` (`readelf -dW libnrt.so` → `0x…0e (SONAME) Library soname: [libnrt.so.1]`; the shared object is installed as `libnrt.so → libnrt.so.1 → libnrt.so.2.31.24.0`, so `.2.31.24.0` is the *version triple*, not the ABI soname).

## Abstract

This page is the **map of the Neuron runtime's trace and telemetry surface** as reconstructed from the (unstripped, full-DWARF) `libnrt.so`. The runtime is a pure **producer**: it never reads back any trace it writes (the consumers — `neuron-profile` in Go, `neuron-monitor`, CloudWatch — live in other binaries and are out of scope). What it produces splits cleanly into **three independent trace producers that converge on one schema**, plus **a fourth, divergent path** that feeds counters to the Neuron DataStore for downstream metric shipping.

The familiar reference frame is a system with **multiple instrumentation backends multiplexed behind one wire format** — the way OpenTelemetry routes spans, logs, and metrics through distinct SDK pipelines that all serialize to OTLP. Here the analogy is exact in shape but different in substance: three *capture engines* with three *different languages and ring structures* (a C++ device-notification drain, a Rust lock-free software-span ring, and a small C in-process event ring) all serialize into the **same AWS-authored `ntff::` protobuf family** — the Neuron Trace File Format — written by one of three container writers. The metrics path does **not** join this convergence: the `kmetric_*` family pushes aggregate counters into the Neuron DataStore (NDS), a shared-memory counter plane that `neuron-monitor` polls and ships to CloudWatch. NTFF is offline post-mortem profile data; NDS is live operational telemetry. They share neither schema nor writer.

This map orients the ten Part-XIII pages: it fixes the producer/consumer boundary, names every capture engine and container writer with a symbol and address, draws the data flow showing where the three trace streams converge and where the metrics plane diverges, and tabulates which sub-page owns each producer's byte-level derivation. It does **not** duplicate the NTFF schema (owned by [NTFF Trace File Format](ntff-format.md)) or the wire-parse tables (owned by [NTFF Wire Tables](ntff-wire-tables.md)) — it links them.

For reimplementation, the contract this section documents is:

- **The producer/consumer split** — `libnrt.so` writes; nothing here reads. The NTFF `.ntff`/`.pb` files and the NDS counter plane are the two distinct outputs; both their readers are separate processes.
- **The three trace producers and their independence** — a C++ device-profile NQ drain (`nrt_profile_*`), a Rust software-span ring (`neuron_rustime::sys_trace`), and a C in-process event ring (`ntrace`) — each with its own taxonomy, its own capture data structure, and its own lifecycle, all co-resident and separately armed.
- **The convergence point** — all three serialize into the `ntff::` protobuf family; the umbrella `nrt_inspect` session is what arms and harvests them together into one output-directory tree.
- **The divergent metrics plane** — the `kmetric_* → tdrv_nds_* → NDS` shared-memory counter path, schema-disjoint from NTFF, destined for CloudWatch via `neuron-monitor` (out of binary).

## At a glance

| | |
|---|---|
| **Role of `libnrt.so`** | Trace/metric **producer only** — no in-process consumer of either output |
| **Trace producers** | **3**, independent & co-resident — see the three-producer table below |
| **Convergence schema** | `ntff::` protobuf family (`KaenaProfilerFormat-2.31.0.0`), 40 message classes — [NTFF Format](ntff-format.md) |
| **Umbrella controller** | `nrt_inspect` session — `nrt_inspect_begin_with_options` @`0x99050`, `nrt_inspect_stop` @`0x9f130`, ctx `g_inspect_context` @`0xc5c8b8` (432 B) |
| **Container writers** | `nrt_profile_serialize` @`0xb2670` · `nrt_profile_session_serialize` @`0xb61d0` · `nrt_trace_serialize` (NTFF body) · serde_json `String` (JSON body) |
| **Divergent metrics plane** | `kmetric_*` @`0xe0cd0..0xe10a0` → `tdrv_nds_*` → Neuron DataStore (shared-memory counters) → CloudWatch (via `neuron-monitor`, out of binary) |
| **Output tree** | `profile_nc_<n>_session_<id>.ntff` · `cpu_util.pb` · `host_mem.pb` · `ntrace.pb` · `trace_info.pb` (under the inspect output dir) |
| **Rust toolchain** | `rustc 1.91.1` (`ed61e7d7e242494fb7057f2657300d9e77bb4fcb`); serde_json statically linked (`serde_json::*` @`0x6c610..`) |

---

## 1. The producer/consumer boundary

The single most important orienting fact: **`libnrt.so` is a write-only trace endpoint.** Every byte of trace data it emits is consumed by a *different* process. A reimplementer who looks for a deserializer, a parser, or a query path inside this binary will not find one — the `ntff::<msg>::_InternalParse` thunks exist only because protoc emits them for every message, and they are never reached on any host code path here (they are dead weight in the producer; their live use is in `neuron-profile`).

```text
                          ┌──────────────────────── libnrt.so (PRODUCER) ────────────────────────┐
   instrumented runtime → │  3 trace producers  ──serialize──►  ntff:: protobuf  ──writer──►  files│
                          │  kmetric_* counters ──push──────►  Neuron DataStore (shared mem)       │
                          └───────────────────────────────────────────────────────────────────────┘
                                        │ .ntff / *.pb files                 │ NDS counter slabs
                                        ▼                                     ▼
                              neuron-profile (Go)                   neuron-monitor → CloudWatch
                                 [out of binary]                       [out of binary]
```

Two consequences follow for the whole section:

- **NTFF is offline.** The `.ntff` / `*.pb` files are post-mortem artifacts opened later by `neuron-profile`. Nothing time-critical depends on them; the producers may overwrite oldest events under pressure (the Rust ring's `force_push` overwrite-oldest policy, §3) precisely because no live reader is waiting.
- **NDS is live.** The `kmetric_*` counters are read continuously by `neuron-monitor` while the workload runs, which is why that path is a lock-light shared-memory write, not a protobuf serialize. This is the architectural reason the metrics plane *cannot* ride the NTFF convergence — see §5.

> **NOTE —** the `KaenaProfilerFormat-2.31.0.0` package name and the `ntff::` message family are AWS first-party (`protoc`-generated, all `si: google::protobuf::Message`, RTTI band `0xbf6fa8..0xbf8708`). The vendored `google::protobuf` 26.1 runtime, Abseil, and the Rust crates (`serde_json`, `crossbeam`, `hashbrown`) are linked dependencies, not reimplementation targets — see [NTFF Format §1](ntff-format.md).

---

## 2. The three trace producers

The runtime carries three *physically distinct* capture engines. They are not three views of one ring — each has its own in-memory data structure, its own event taxonomy, its own enable gate, and its own drain function. The reason there are three is that they instrument three different *temporal grains*: on-device hardware notifications (what the silicon did), host software spans (what the runtime thread did), and coarse host milestones (a lightweight always-available timeline).

The three taxonomies are deliberately **disjoint, not nested.** A reimplementer's instinct is to assume one is a superset of the others — that the 46-variant sys_trace enum subsumes the 16-variant ntrace ring, or that the device-profile `notification_type` is a subset of sys_trace. None of that holds: the 11 device `notification_type` values (0..10), the 46 host `nrt_sys_trace_event_type` values (0..45), and the 16 host `ntrace_data_type` values (0..15) are three independent enumerations with overlapping *concepts* (each has a notion of "exec start/end", "collectives", "memory alloc/free") but distinct *discriminant spaces* and distinct payload structs. They reconcile only at the `ntff::` message level, never at the enum level — the device path maps `notification_type 0..10 → ntff::notification_trace_type 0..5 + block_type 0..2` through `nrt_profile_convert_trace_type_from_ntff_params` @`0xaaf40` (jump table @`0x8570c0`), while the two host paths both land in `ntff::ntrace_event`. This three-way reconciliation is the section's deepest subtlety; the per-taxonomy derivations live in [event-taxonomy](event-taxonomy.md) and [ntff-format](ntff-format.md).

### Producer comparison

| Dimension | (1) Device-profile | (2) sys_trace | (3) ntrace |
|-----------|--------------------|--------------|------------|
| **Source TU** | `nrt_profile.cpp` (C++) | `neuron_rustime::sys_trace` (Rust 1.91.1) | host `ntrace` ring (C) |
| **What it captures** | on-device Notification-Queue blobs per NeuronCore (per-TPB + per-TopSP) | host-side software spans (~46 event types across exec/tensor/dmem/collectives) | coarse host milestones (16 event types: exec start/end, CC, IO copy, alloc/free) |
| **Capture structure** | drained NQ rings → `ntff_data_t` (72 B) harvest | per-NC lock-free `crossbeam::ArrayQueue<Event>` (112-B records, overwrite-oldest) | in-process `ntrace_data_t[]` C ring (48-B entries) |
| **Taxonomy** | `notification_type` 0..10 → `ntff::notification_trace_type` 0..5 + `block_type` 0..2 | `nrt_sys_trace_event_type` 46 variants (0..45) | `ntrace_data_type` 0..15 |
| **Enable gate** | per-NC NQ subscribe (`nrt_profile_notification_subscribe_nc`) | `nrt_sys_trace_config_t` (312 B): per-NC + 46-entry per-event-type bitmap | implicit (NRT-state + TDRV guard); rejected in implicit-async mode |
| **Arm entry** | `nrt_profile_start` @`0xaee40` (MODEL) · `nrt_profile_session_start` @`0xb6800` (SESSION) | `nrt_sys_trace_start` @`0xb9800` → `capture::capture_start` @`0x5b0590` | `nrt_trace_start` @`0xad140` |
| **Drain / harvest** | `nrt_profile_notification_read_all_nc` @`0xb1850` | `capture::drain_events` @`0x5afea0` (proto) · `api::fetch_events` @`0x5aa3b0` (JSON) | `nrt_trace_stop` @`0xb5ad0` |
| **Serializer(s)** | `ntff::ntff_info` proto → `nrt_profile_serialize` @`0xb2670` | `ntff::ntrace_event` (via `event_to_proto` @`0x9aec0`) **or** serde_json `String` | `ntff::ntrace_info` proto → `nrt_trace_serialize` |
| **Output** | `profile_nc_<n>_session_<id>.ntff` (128-B header + proto + raw blobs) | `ntrace.pb` (proto path) / a JSON `String` (`{"events":[…],"data_version":2}`) | `ntrace.pb` (`ntff::ntrace_info`, bare stream) |

The three producers are byte-distinct but **schema-convergent**: producers (2) and (3) both terminate in `ntff::ntrace_event` records (producer (2) via the C++ `event_to_proto` mapper, producer (3) via the offset-mapping inside `nrt_trace_stop`), and producer (1) terminates in `ntff::ntff_info`. All three message families live in the same `ntff::` package — see [NTFF Format](ntff-format.md).

> **QUIRK —** sys_trace has **two terminal serializers, not one.** The same 112-byte `nrt_sys_trace_event` records can leave the runtime either as protobuf (`event_to_proto` → `ntff::ntrace_event`, the NTFF path taken by `nrt_inspect_stop`) or as a self-contained JSON `String` (`api::fetch_events`, taken by `nrt_sys_trace_fetch_events` @`0xb9880`). The JSON form is *not* NTFF — it is a separate `{"events":[…],"data_version":2}` object with snake_case keys (`event_type`, `phase`, `id`, `nc_idx`, `worker_gid`, …; literals `"events"` @`0xa30dac`, `"data_version"` @`0xa30db2`). A reimplementer must treat the JSON exporter as a *third* output sink, parallel to the two NTFF container writers. It is covered in [serde_json Serializer](rust-serde.md).

---

## 3. Three-producer data-flow

The diagram below shows the full surface: the three trace producers converging on the NTFF file tree (left/center), and the metrics plane diverging to NDS/CloudWatch (right). The `nrt_inspect` session is the umbrella that arms producers (1) and (2) together and fuses their harvest at `nrt_inspect_stop`.

```text
                       INSTRUMENTED RUNTIME  (exec / tensor / dmem / collectives / kmgr)
                         │                      │                      │                    │
   ──────────────────────┼──────────────────────┼──────────────────────┼────────────────────┼─────────
   PRODUCER (1)          │  PRODUCER (2)         │  PRODUCER (3)         │  METRICS PLANE      │
   device-profile (C++)  │  sys_trace (Rust)     │  ntrace (C)          │  kmetric (C++)      │
   ──────────────────────┼──────────────────────┼──────────────────────┼────────────────────┼─────────
                         │                       │                      │                     │
   on-device NQ rings    │  ~30 instrumented     │  nrt_trace_* call    │  exec/load/unload   │
   (per-TPB {0,3,1,2,4,5}│  call sites           │  sites               │  completion         │
    per-TopSP {6,8,7})   │       │               │       │              │       │             │
        │                │  new_event_with_seq   │       ▼              │       ▼             │
        ▼                │   @0x5b1410           │  ntrace_data_t[] ring │  kmetric_update_    │
   subscribe + drain     │       │               │  (48-B, C, in-proc)  │   nds_exec_stats    │
   read_all_nc @0xb1850  │       ▼               │       │              │   @0xe0d30 + family │
        │                │  crossbeam ArrayQueue │       ▼              │       │             │
        ▼                │  <Event> per-NC ring  │  nrt_trace_stop      │       ▼             │
   convert_trace_type_   │  (112-B, lock-free,   │   @0xb5ad0           │  tdrv_nds_register_*│
   from_ntff_params      │   overwrite-oldest)   │       │              │  → NDS shared-mem   │
   @0xaaf40              │       │               │       ▼              │     counter slabs   │
   (0..10 → 0..5 +block) │       ├──drain_events │  ntff::ntrace_event  │       │             │
        │                │       │  @0x5afea0    │  (offset map +72.. ) │       │             │
        ▼                │       ▼               │       │              │       │             │
   ntff::trace_info  ◄───┤  event_to_proto       │       ▼              │       │             │
   wraps NQ blob         │   @0x9aec0  ──────────┤  ntff::ntrace_info   │       │             │
        │                │  (46-variant switch → │       │              │       │             │
        ▼                │   ntff::ntrace_event +│       │              │       │             │
   ntff::ntff_info       │   Map<string,string>) │       │              │       │             │
        │                │       │               │       │              │       │             │
   ═════╪════════════════╪═══════╪═══════ NTFF CONVERGENCE ═════╪══════ │       │             │
        ▼                │       ▼               │              ▼       │       │             │
   nrt_profile_serialize │  (also) api::         │  nrt_trace_serialize │       │             │
   @0xb2670  / session_  │  fetch_events @0x5aa3b0│      │              │       │             │
   serialize @0xb61d0    │  → serde_json String  │      │              │       │             │
        │                │       │               │      │              │       │             │
        ▼                │       ▼               │      ▼              │       ▼             │
   profile_nc_<n>_       │  ntrace.pb            │  ntrace.pb          │  Neuron DataStore   │
   session_<id>.ntff     │  (or JSON String)     │  (ntff::ntrace_info)│  (NDS shared mem)   │
        │                │       │               │      │              │       │             │
        └────────────────┴───────┴───── inspect output dir ───┴────────┘       ▼             │
                  (nrt_inspect_stop @0x9f130 fuses harvest+serialize)     neuron-monitor      │
                       arms (1)+(2)+system_monitor; writes the tree       → CloudWatch        │
                                                                          [out of binary]     │
   ════════════════════════════════════════════════════════════ DIVERGENCE ═════════════════
```

Three structural facts a reimplementer must internalize from this picture:

- **The convergence is at the `ntff::` schema, not at a single function.** Producer (1) builds `ntff_info`; producers (2) and (3) build `ntff_event`/`ntrace_info`. They never share a serializer call — they share the *message definitions*. That is why the NTFF schema is its own page and is referenced by all three producers.
- **`event_to_proto` @`0x9aec0` is the join seam for producer (2)→NTFF.** It is the largest function in the lane (~10 KB / 374 basic blocks) and switches on the 46-variant `nrt_sys_trace_event_type` to fill an `ntff::ntrace_event` plus a `Map<string,string>` of named custom fields. The exhaustive per-variant field selection is owned by [SysTraceEventType Taxonomy](event-taxonomy.md).
- **The metrics plane never crosses the `═══` convergence line.** It has no `ntff::` message, no container writer, and no `.ntff`/`.pb` file. Its output is the NDS shared-memory counter slab, read live by an external monitor. This is the divergence the section is named for.

> **GOTCHA —** the sys_trace ring is **overwrite-oldest, not blocking** (`crossbeam_queue::ArrayQueue::force_push` @`0x5b4b20`). Under capture pressure the ring silently drops the oldest event and keeps the newest; there is no backpressure to the instrumented caller. A reimplementer who assumes a lossless span buffer will mis-size the ring and lose head-of-trace events. The ring size is `max_events_per_nc` (default `0x100000`); the memory cost is logged ("Allocating memory for `<n>` events (`<x>` GB) for system trace ring buffer", `.rodata` @`0xa31928`). Full ring mechanics in [sys_trace Capture Engine](rust-capture.md).

---

## 4. The umbrella: the `nrt_inspect` session

Producers (1) and (2) are rarely armed directly. The public entry is the **inspect session**, an umbrella controller (`nrt_inspect.cpp`, `g_inspect_context` @`0xc5c8b8`, 432 B) that fans out by an activity bitmask and fuses the harvest. It is the only place all the streams are written into one coherent output-directory tree.

```text
nrt_inspect_begin_with_options (0x99050)            ── arm by activity bitmask
  activity bit0  SYSTEM_PROFILE=1  ─► nrt_sys_trace_start (0xb9800)        [producer 2]
  activity bit3  CPU_UTIL=8        ─► nrt_system_monitor_start(10000ms)    [host cpu/mem sampler]
  device_profile_mode == SESSION   ─► per-NC nrt_profile_session_start (0xb6800)  [producer 1]
  (+ install crash signal handlers; reject "/tmp" & "/dev/null"; default "./output")

nrt_close ─► nrt_inspect_close (0xa1c00) ─► nrt_inspect_stop (0x9f130)     ── fused harvest+serialize
  per NC:  nrt_profile_session_stop ─► nrt_profile_session_serialize ─► nrt_profile_serialize
  dump_system_profile_snapshot:
     datafile_init (0x9ac30) ×3 ─► dump_trace_info (0x9d610) ─► event_to_proto (0x9aec0) per event
                                 ─► add_interned_data (0x9a8c0) ─► write cpu_util.pb / host_mem.pb
                                                                          / ntrace.pb / trace_info.pb
```

The activity bitmask (`nrt_inspect_activities_t`: `SYSTEM_PROFILE=1`, `DEVICE_PROFILE=2`, `HOST_MEMORY=4`, `CPU_UTIL=8`, `ALL=255`) is what decides which producers run; producer (3) (`ntrace`) is armed independently via `nrt_trace_start` and is *not* on the inspect bitmask. The full lifecycle, the output-directory naming, the signal-handler flush, and the on-fail cleanup are owned by [Inspect / Profile API](inspect-profile-api.md); the host cpu/mem sampler (activity bit3) that feeds `ntff::host_stats` is owned by [System Monitor](system-monitor.md).

> **NOTE —** producer (3) (`ntrace`) **rejects implicit-async execution mode** — `nrt_trace_stop` bails with "Trace is not supported in implicit async execution mode…" (`.rodata` @`0x7d4838`). This is a producer-availability constraint, not a schema one: the same `ntff::ntrace_info` output is fine, but the in-process C ring is only coherent when execution is synchronous. The three producers therefore are not always all available simultaneously.

---

## 5. The divergent metrics plane

The fourth path is deliberately *not* a trace producer. The `kmetric_*` family does not capture events, does not build `ntff::` messages, and does not write a file. It maintains **aggregate counters** — execution counts, error counts, exec latencies, per-NEFF aggregates — and pushes them into the Neuron DataStore, a shared-memory counter plane.

| `kmetric` entry | Address | What it updates (into NDS) |
|-----------------|---------|----------------------------|
| `kmetric_update_nds_generic_status` | `0xe0cd0` | per-NC generic status code |
| `kmetric_update_nds_exec_stats` | `0xe0d30` | per-NC execution stats (count/status) |
| `kmetric_update_nds_error_stats` | `0xe0f60` | per-NC error stats (from `kbl_infer_errors`) |
| `kmetric_update_nds_exec_latencies` | `0xe0fe0` | per-NC exec latency quad (4 × double) |
| `kmetric_load_model_update_agg_neff_id` | `0xe1070` | aggregate per-NEFF-id load counter |
| `kmetric_unload_model_update_agg_neff_id` | `0xe10a0` | aggregate per-NEFF-id unload counter |

These write through the `tdrv_nds_*` layer (`tdrv_nds_register_mem_alloc`, `tdrv_nds_register_mem_free`, `tdrv_nds_save_process_info`, `tdrv_init_nds_for_device`) into the per-process NDS slabs. `neuron-monitor` (a separate process) polls those slabs and ships the counters to CloudWatch. The CloudWatch leg is entirely outside `libnrt.so` — there is **no** `cloudwatch`, `otlp`, `prometheus`, or `statsd` string anywhere in this binary (verified absent); the runtime's contract ends at the NDS shared-memory write.

> **CORRECTION (TRACE-MAP-01) —** the section brief frames the metrics path as "kernel metrics → CloudWatch". Grounded on the binary, the producer-side reality is **`kmetric_* → tdrv_nds_* → Neuron DataStore (shared memory)`**; CloudWatch is the *downstream* destination reached by the external `neuron-monitor`, not by `libnrt.so`. The label "→ CloudWatch" is retained on the diagram as the eventual sink, but the in-binary boundary is the NDS write. The NDS counter plane itself is documented in [The Neuron DataStore (NDS)](../datastore/overview.md); the `kmetric` callers and error-reporting surface in [Telemetry, Metrics & Error Reporting](telemetry-errors.md).

This is why the metrics plane diverges: NTFF is a *serialize-once, read-later* file format; NDS is a *write-continuously, poll-live* counter plane. A schema that suits one is wrong for the other, so the runtime keeps them disjoint by construction.

---

## 6. Pointer table: the ten Part-XIII pages

| Page | Owns | Primary anchors |
|------|------|-----------------|
| **This page** — Section Map | the three-producer split, the NTFF convergence, the metrics divergence | producer entry points; `nrt_inspect_stop` @`0x9f130` |
| [Inspect / Profile API](inspect-profile-api.md) | the `nrt_inspect` umbrella lifecycle, output-dir tree, device-profile MODEL/SESSION harvest | `nrt_inspect_begin_with_options` @`0x99050`, `g_inspect_context` @`0xc5c8b8`, `nrt_profile_*` family |
| [System Monitor & Debug Stream](system-monitor.md) | the host cpu/mem sampler feeding `ntff::host_stats`; `cpu_util.pb`/`host_mem.pb` | `nrt_system_monitor_start` (10000 ms), `dump_trace_info` @`0x9d610` |
| [NTFF Trace File Format](ntff-format.md) | the 40-message `ntff::` schema, the 128-B `.ntff` container header, the `notification_type` map | `descriptor_table_protodef_ntff_2eproto` @`0xad4200`; RTTI band `0xbf6fa8..0xbf8708` |
| [NTFF Wire Tables](ntff-wire-tables.md) | the table-driven `TcParser` decode, `type_card` encoding, 207 `FieldEntry` records | `TcParser::ParseLoop` @`0x6a4b00`; 38 `_table_` statics `0xc09680..0xc0bf20` |
| [SysTraceEventType Taxonomy](event-taxonomy.md) | the 46-variant enum, the 40-B `data` union, the `event_to_proto` per-variant field map | `event_to_proto` @`0x9aec0`; `nrt_sys_trace_event` (112 B) |
| [sys_trace Capture Engine](rust-capture.md) | the Rust lock-free ring, `NcContext`/`ArrayQueue`/`InternedDataShard`, the lock protocol | `capture::capture_start` @`0x5b0590`, `new_event_with_seq` @`0x5b1410`, `CONTEXTS` @`0xc0d300` |
| [serde_json Serializer](rust-serde.md) | the JSON export path, the `EventWrapper`/`EventsWrapper` serde impls, snake_case keys | `api::fetch_events` @`0x5aa3b0`; `EventWrapper::serialize` @`0x5ab320` |
| [cbindgen FFI Boundary](rust-ffi.md) | the C-ABI shims into the Rust crate; the `nrt_sys_trace_config_t`/`fetch_options` marshaling | `nrt_sys_trace_capture_*` @`0x509680..0x509980`; `Config::from` @`0x5b2110` |
| [Telemetry, Metrics & Error Reporting](telemetry-errors.md) | the `kmetric_* → NDS` metrics plane, error/status reporting | `kmetric_*` @`0xe0cd0..0xe10a0`; `tdrv_nds_*` |

---

## 7. Verification notes

> The three-producer split, the NTFF convergence, and the metrics divergence were cross-checked against the `libnrt.so` exports and the consolidated trace facets:
> - **Producer entry points** — `nrt_profile_start` @`0xaee40`, `nrt_profile_session_start` @`0xb6800`, `nrt_profile_notification_read_all_nc` @`0xb1850`, `nrt_profile_convert_trace_type_from_ntff_params` @`0xaaf40` (producer 1); `nrt_sys_trace_start` @`0xb9800`, `capture::capture_start` @`0x5b0590`, `new_event_with_seq` @`0x5b1410`, `api::fetch_events` @`0x5aa3b0` (producer 2); `nrt_trace_start` @`0xad140`, `nrt_trace_stop` @`0xb5ad0` (producer 3) — all present in `functions.json`, addresses match `nm` exactly.
> - **Convergence seam** — `event_to_proto` @`0x9aec0` and `nrt_profile_serialize` @`0xb2670` / `nrt_profile_session_serialize` @`0xb61d0` resolved to `nrt_inspect.cpp` / `nrt_profile.cpp` via DWARF `addr2line`; the 40-class `ntff::` RTTI band `0xbf6fa8..0xbf8708` matches the schema page.
> - **Metrics plane** — `kmetric_update_nds_*` family @`0xe0cd0..0xe10a0` and `tdrv_nds_*` resolved by symbol; **no** `cloudwatch`/`otlp`/`prometheus`/`statsd` literal exists in `.rodata` (grep-verified absent), confirming the in-binary boundary is the NDS write.
> - **Rust toolchain** — sysroot tag `/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/` @`.rodata`; `serde_json::*` symbols @`0x6c610..` confirm the JSON serializer is statically linked.
>
> **[MED]** The exact per-field byte layout *within* the 112-byte sys_trace `Event` and the exhaustive per-variant mapping in `event_to_proto` are derived from decompiled store sequences, not a DWARF struct dump; they are owned (and confidence-graded) by [event-taxonomy](event-taxonomy.md) and [rust-capture](rust-capture.md). The NTFF wire field *numbers* (as opposed to struct offsets) are recovered independently by [ntff-wire-tables](ntff-wire-tables.md) from the embedded `FileDescriptorProto`.

---

## Cross-References

**The NTFF convergence schema**
- [NTFF Trace File Format](ntff-format.md) — the 40-message `ntff::` schema, the 128-B `.ntff` container, the `notification_type → trace_type + block_type` map
- [NTFF Wire Tables](ntff-wire-tables.md) — the table-driven `TcParser` decode and `type_card` encoding (independent schema cross-check)

**The three producers**
- [Inspect / Profile API](inspect-profile-api.md) — the umbrella session, device-profile MODEL/SESSION harvest, output-dir tree (producer 1 + controller)
- [SysTraceEventType Taxonomy](event-taxonomy.md) — the 46-variant enum, the `data` union, the `event_to_proto` mapper (producer 2 schema seam)
- [sys_trace Capture Engine](rust-capture.md) — the Rust lock-free ring and capture lifecycle (producer 2 engine)
- [serde_json Serializer](rust-serde.md) — the JSON export sink, parallel to the NTFF writers (producer 2 second terminal)
- [cbindgen FFI Boundary](rust-ffi.md) — the C-ABI shims and config/fetch-option marshaling into the Rust crate

**The metrics divergence**
- [Telemetry, Metrics & Error Reporting](telemetry-errors.md) — the `kmetric_* → NDS` counter plane and error reporting
- [System Monitor & Debug Stream](system-monitor.md) — the host cpu/mem sampler feeding `ntff::host_stats`
- [The Neuron DataStore (NDS)](../datastore/overview.md) — the shared-memory counter plane the metrics path writes into
- [back to index](../index.md)
