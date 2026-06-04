# ErrorAggregator

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781 691 048 bytes, x86-64). Other versions will differ.*

## Abstract

The Megascale `MegascaleErrorAggregator` is the fleet-wide root that turns a storm of per-host failure reports into a single classified root-cause verdict. When a multi-slice TPU job hangs or crashes, every host's libtpu posts one `MegaScaleRuntimeError` over the `MegaScaleTransport.ReportError` gRPC to the coordinator process. The aggregator fans those reports into one `RapidEyeErrorDigestProto` — a self-describing record that names the suspected culprit hosts, classifies the failure into one of nine `Cause` values, and embeds the temporally-first error verbatim so a human or a downstream batch job can read the verdict without re-deriving it.

The closest familiar analogue is a crash-reporting aggregator (Sentry/Crashpad) wired to a barrier: it is a centralized, single-instance collector keyed by participant identity, with a "did everyone report yet" early-fire and a deadline fallback — but unlike a generic crash sink it is one-shot. The aggregator lives only for the duration of a single error storm: it is lazily created on the first inbound `ReportError`, fans in until `size() == expected_workers` or a 300 ms idle deadline (rearmed on every report) elapses, emits its digest, and dies. There is no ring buffer, no TTL sweep, and no cross-restart persistence — the design assumes one storm followed by one drain.

This page owns the **wire format** (`RapidEyeErrorDigestProto` and the embedded `MegaScaleRuntimeError`), the **scope** (a single fleet-wide instance on the coordinator, fed by the flat DCN `(slice_id, host_id)` host identity), the **retention model** (in-memory `linked_hash_map`, opt-in on-disk RapidEye log, no eviction), and the **dedup / first-error-wins** logic (per-`(worker, task)` last-write-wins coalescing plus a sticky first-error slot). ICI-level failure *detection* — how a chip fault becomes a `MegaScaleRuntimeError` in the first place — belongs to [ICI failure recovery](../ici/failure-recovery.md), the layer below. The fleet-metadata view of how the aggregator consumes `NumHosts()` and `(slice_id, host_id)` belongs to [barrier-error usage](fleet-metadata/barrier-error-usage.md).

For reimplementation, the contract is:

- **The wire format** — `RapidEyeErrorDigestProto` (17 live fields, 13 nested messages, a 9-value `Cause` enum) and the embedded per-host `MegaScaleRuntimeError` (13 fields, two enums). The digest embeds the first error verbatim, so the record is self-describing.
- **The scope model** — a single fleet-wide aggregator instance on the coordinator, lazily allocated, keyed by the flat DCN host identity; workers run no aggregator of their own.
- **The dedup / retention rules** — the `slice<S>-task<H>/<task_id>` map key, last-write-wins coalescing, the sticky first-error slot, and the absence of any count bound, TTL, or persistence.
- **The early-fire / deadline drain** — `size() == GetClusterStatus().expected` fires immediately; otherwise a 300 ms idle deadline (rearmed on every inbound `ReportError`) drains a partial digest with a "missing workers" diff once 300 ms pass with no new report.

| | |
|---|---|
| **Class** | `xla::megascale::runtime::MegascaleErrorAggregator` |
| **Object size** | `0x2f0` = 752 bytes, `operator new(752, 16)` |
| **Owner / scope** | single instance, owned by the coordinator's `ErrorReporter` (`+0x38`, lazy); fleet-wide |
| **Wire format** | `xla.megascale.runtime.RapidEyeErrorDigestProto` (syntax `editions`) |
| **Descriptor** | `rapideye_logging.proto` FileDescriptorProto at `protodesc_cold` VA `0xc169340` (4 460 bytes) |
| **Source TU** | `platforms/xla/megascale/runtime/common/megascale_error_aggregator.cc` |
| **Ingestion** | `AddError` `0x1ccba940`; gRPC fan-in `ErrorReporter::ReportError` `0x1ccb6ea0` |
| **Drain** | `ProcessAndShutdown` `0x1ccbaba0` (idempotent, one-shot) |
| **Coordinator job_name** | `"McJax"` |
| **Map key** | `slice<S>-task<H>/<task_id>` (`linked_hash_map<string, MegaScaleRuntimeError>`) |
| **Persistence** | none by default; opt-in `RapidEyeLogger` sink (`--megascale_rapideye_error_digest_log_path`) |

> **CORRECTION (AGG-1) —** an earlier pass estimated the coordinator dedup key template as `"$0:$1"` and the aggregator `job_name` as `"error"`. Decompilation of `ErrorReporter::ReportError` (`0x1ccb6ea0`) shows the binary uses `SubstituteAndAppendArray("slice$0-task$1", …)` (line 92, template length 14) and constructs `MegascaleErrorAggregator(…, "McJax")` (line 47). The `slice<S>-task<H>` form and the `"McJax"` launcher identity are the values on the wire; the page uses them throughout.

---

## Scope — One Fleet-Wide Instance

### Purpose

The aggregator's scope is the entire multi-slice job, collected at a single point. There is exactly one `MegascaleErrorAggregator` for a running job, and it lives on the coordinator process. Worker hosts do **not** run an aggregator; they run only the host-local `DebugManager` collector (a separate process singleton) and post their errors up to the coordinator. The model is two flat tiers — host collector → coordinator root — with no per-slice intermediate aggregator. A superpod with eight slices still has one coordinator aggregator covering all of them.

### Entry Point

```text
ErrorReporter::ReportError (0x1ccb6ea0)        ── coordinator-side gRPC fan-in handler
  ├─ (first hit) operator new(752, 16)         ── lazy-allocate the one aggregator
  │    └─ MegascaleErrorAggregator(_, "McJax")  ── 1-arg ctor 0x1ccba4c0 (delegates to 4-arg 0x1ccba600)
  ├─ thread::AddCancellableAt(.., this+64)     ── arm 300 ms deadline → ProcessErrorDigest
  ├─ SubstituteAndAppendArray("slice$0-task$1") ── build the (slice_id, host_id) worker key
  ├─ MegascaleErrorAggregator::AddError        ── 0x1ccba940 — upsert into the map
  └─ if size() == GetClusterStatus().expected  ── early-fire
       └─ ErrorReporter::ProcessErrorDigest    ── 0x1ccb7140 — drain + classify + log
```

### Algorithm

```c
function ErrorReporter_ReportError(req, resp):       // sub_1ccb6ea0
    TracedMutexLock guard(this->mu_)                  // ErrorReporter+0x48, kind=14
    if this->aggregator_ == null:                     // +0x38, lazy
        this->aggregator_ = new(752,16)
            MegascaleErrorAggregator("McJax")         // line 47
    deadline = clock_.Now() + 300ms                   // line 55 — rearmed each call
    AddCancellableAt(threadpool_, deadline,
                     [this]{ ProcessErrorDigest(); },
                     &this->deadline_handle_)          // line 59, &(this+64)
    invoke this->unicast_callback_(slice_id, host_id, *err)  // ErrorReporter+0x10, per-error
    worker_id = SubstituteAndAppendArray(
                  "slice$0-task$1", {slice_id, host_id}) // line 92 — DCN host identity
    err = req.error  (proto field 3); if null -> global empty MegaScaleRuntimeError
    aggregator_->AddError(worker_id, *err)             // line 102
    status = coordinator_->GetClusterStatus()          // vtable+64; expected-worker count
    if status.ok() and aggregator_->size() == status.expected:  // line 117 early-fire
        ProcessErrorDigest()                           // line 120 — bypass the deadline
    resp = ReportErrorResponse{}                       // empty ack
```

> **NOTE —** the early-fire count is the *cluster-status* expected count read through `GetClusterStatus()` (coordinator vtable+64), not a raw constant. Per the [fleet-metadata](fleet-metadata/barrier-error-usage.md) view that expected count equals `MultiSliceTopologyAndLocation::NumHosts()` — the same fleet-wide host count the [cross-host barrier](cross-host-barrier.md) waits on. If the topology is not yet discovered (`!status.ok()`), the digest is dropped with `"Unexpected error digest before topology discovery is completed:"` rather than emitted against an unknown population.

### The PJRT parallel surface

A third, independent scope exists: the PJRT C-API exposes the same class to higher-level XLA/JAX clients without any networking. Eight shims under `pjrt::(anonymous)` (`Create` `0xe6bab80`, `Delete` `0xe6bac00`, `AddError` `0xe6bad60`, `ProcessAndShutdown` `0xe6bae40`, `LogErrorDigest` `0xe6baec0`, `Size` `0xe6baf20`, `Active` `0xe6baf80`, plus `ErrorDigestDelete` `0xe6bace0`) let a client `Create` its own aggregator with a caller-supplied `job_name`, drive `AddError` with a caller-supplied `worker_id` and a serialized `MegaScaleRuntimeError` (`ParseFromString`), and pull the digest directly in-process. Each shim is gated by an `ActualStructSizeIsGreaterOrEqual` ABI check against a documented args-struct size (`Create` `0x20`, `AddError` `0x30`, `ProcessAndShutdown`/`Size`/`LogErrorDigest` `0x18`, `Active` `0x11`, `Delete`/`ErrorDigestDelete` `0x10`). The scope of a PJRT-path aggregator is whatever the client feeds it; no RPC and no coordinator are involved.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `ErrorReporter::ReportError` | `0x1ccb6ea0` | gRPC fan-in; lazy alloc, key build, deadline, AddError, early-fire | CERTAIN |
| `ErrorReporter::ProcessErrorDigest` | `0x1ccb7140` | drain + missing-worker diff + LogErrorDigest + optional abort | HIGH |
| `MegascaleErrorAggregator` ctor (4-arg) | `0x1ccba600` | class init; stores `job_name`/`log_dir` strings + precomputed `global_id`/`xid`; reads `FLAGS_megascale_error_aggregation_enabled` → `shutdown_` | HIGH |
| `MegascaleErrorAggregator` ctor (1-arg) | `0x1ccba4c0` | `job_name`-only delegator: reads `FLAGS_megascale_rapideye_error_digest_log_path`, `NewGlobalID()`, `getenv("XM_XID")`, then calls the 4-arg ctor | HIGH |
| `PJRT ErrorAggregatorCreate` | `0xe6bab80` | in-process aggregator (`operator new(752,16)`) | CERTAIN |
| `PJRT ErrorAggregatorAddError` | `0xe6bad60` | `ParseFromString` + AddError; `megascale_extension.cc:321` | HIGH |
| `PJRT ErrorAggregatorProcessAndShutdown` | `0xe6bae40` | returns a 448-byte (`0x1C0`) `ErrorDigest` | HIGH |

---

## Ingestion and Dedup — `AddError`

### Purpose

`AddError` is the single ingestion point. It serializes concurrent callers under the aggregator's lone mutex, captures the first non-cancellation error in a sticky slot for classification, and upserts each report into a `linked_hash_map` keyed by host-and-task identity. Two errors are "the same" iff their key matches; a late arrival with a matching key overwrites the earlier value (last write wins), but insertion order is preserved so the *first* reporter is still recoverable without a timestamp.

### Algorithm

```c
function AddError(worker_id, err):                    // sub_1ccba940
    mu_.lock()                                         // mutex at this+744 == +0x2e8
    if shutdown_:                                       // +0x30 — already drained / poisoned
        mu_.unlock(); return                            // no-op
    if !has_first_recorded_error_:                      // +0xe0
        if err.error_type == CANCELLED (3):             // line 33 — *((DWORD*)err + 18) == 3
            shutdown_ = true                            // teardown signal, store NOTHING
            mu_.unlock(); return
        first_recorded_error_.CopyFrom(err)             // +0x70, arena=null
        has_first_recorded_error_ = true
    key = StrCat(worker_id, "/", err.task_id)           // line 81; task_id = field 9, +0x58
    slot = errors_by_worker_launch_.LazyEmplaceInternal(key)  // line 91 — +0xe8
    slot.value.CopyFrom(err)                            // line 95 — last write wins
    mu_.unlock()
```

> **QUIRK —** a `CANCELLED` first error is a *teardown signal, not data*. If the very first `AddError` carries `error_type == CANCELLED (3)` while the sticky slot is still empty, `AddError` flips `shutdown_ = true` and returns without storing anything (lines 33-36). This poisons the aggregator: a subsequent `ProcessAndShutdown` returns the empty cached digest. The semantics are "the job is tearing down on purpose — do not build a hang digest." A reimplementation that treats cancellation as just another error will emit spurious `UNKNOWN_CAUSE` digests on every clean shutdown.

### The key, in two segments

The map key is `slice<S>-task<H>/<task_id>`, built in two places:

- **Outer (host identity)** — `ErrorReporter::ReportError` builds `worker_id = "slice<slice_id>-task<host_id>"` from `ReportErrorRequest.{slice_id, host_id}`, the flat DCN host identity ([fleet metadata](fleet-metadata/host-identity.md)).
- **Inner (in-host task)** — `AddError` appends `"/" + err.task_id`, where `task_id` is `MegaScaleRuntimeError` field 9 (offset `+0x58`), the Borg task / process index inside that host.

Because the early-fire check caps the population at `expected` and the key collapses duplicates, the map naturally holds exactly one entry per expected reporter; a host that retries its `ReportError` (e.g. a transport retransmit) coalesces onto its existing entry rather than inflating `size()`.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `MegascaleErrorAggregator::AddError` | `0x1ccba940` | mutex, CANCELLED short-circuit, key build, upsert | CERTAIN |
| `MegascaleErrorAggregator::size` | `0x1ccba900` | `linked_hash_map.size()` under mutex (+744) | CERTAIN |
| `MegascaleErrorAggregator::active` | `0x1ccba8c0` | `!shutdown_` under mutex (+744) | CERTAIN |

---

## Drain and Classification — `ProcessAndShutdown`

### Purpose

`ProcessAndShutdown` is the one-shot drain that converts the accumulated map into the final classified `ErrorDigest`. It walks every stored error, builds the five `btree_set` indices (culprit workers, faulty network links, sequencer tag/pc state, worker-by-tag/pc inverted index, program info), runs the post-hoc `Cause` classifier over the full cross-host set, caches the result, sets `shutdown_`, and returns the digest by value. It is **idempotent**: a second call returns the cached copy. After it runs, the aggregator is dead.

### Algorithm

```c
function ProcessAndShutdown() -> ErrorDigest:          // sub_1ccbaba0
    mu_.lock()
    if shutdown_:                                       // line ~817 fast path
        out = cached_digest_; mu_.unlock(); return out  // idempotent / poisoned-by-CANCELLED
    LOG(INFO) "Processing $0 errors", size()            // line 1134
    digest.local = WorkerInfo(job_name, global_id, xid) // +0x00/+0x60/+0x68 — identify the coordinator
    for (key, err) in errors_by_worker_launch_:          // arrival order preserved
        st = err.runtime_state.rapid_eye_info            // optional<RapidEyeInfo>
        note chip_config_name == "default"               // 8-byte compare @ 1ccbaf56
        populate workers_set, hlo_set, faulty_link_set,
                 sequencer-state set, workers-by-(tag,pc) index
        build OffloadedSparseCoreTagPc entries by HLO
    digest.cause = ClassifyCause(...)                    // see decision order below
    cached_digest_ = digest                              // +0x128
    shutdown_ = true
    mu_.unlock()
    return digest
```

### Cause decision order

The classifier emits exactly one `Cause`. The decision order observed in the 5320-byte body (the precise tie-break scoring between adjacent causes is not fully data-flow-extracted — MEDIUM):

```text
any error_type == UNRECOVERABLE_ERROR (2)   -> UNRECOVERABLE_ERROR
any worker chip_id == -1                     -> PROGRAM_NOT_QUEUED
FaultyNetworkLink set non-empty              -> NETWORKING_ISSUE
"default" config DMA-stage stall pattern     -> DATA_INPUT_STALL
module fingerprints differ across workers    -> DIFFERENT_MODULE
fingerprints match but tag/pc layouts differ -> FINGERPRINT_MISMATCH
TC stall pattern present                     -> BAD_TPU_CHIP
SC stall pattern present                     -> BAD_SC_CHIP
otherwise                                    -> UNKNOWN_CAUSE
```

> **NOTE —** every `Cause` is a *fleet verdict* computed by the coordinator over the full set; there is no host-local cause. `DIFFERENT_MODULE` and `FINGERPRINT_MISMATCH` are the only genuinely fleet-scope causes — they cannot be decided without comparing multiple hosts' reports, which is the whole reason the aggregator's scope is the entire job. `NETWORKING_ISSUE` is host-pair scope (it names a faulty `src→dst` link); the chip/stall causes name a single culprit host but are still emitted only after the full set arrives so the "following hosts" lists can be built. A chip-level ICI fatal escalated per [ICI failure recovery](../ici/failure-recovery.md) surfaces here as `NETWORKING_ISSUE` once its `FaultyNetworkLink` lands in the set.

### Consumer side — `LogErrorDigest`

`LogErrorDigest` (`0x213b42c0`, in `.text.unlikely`) is a jump-table on `digest.cause`: each cause has a baked LOG(ERROR) template plus a `FormatWorkers(...)` host list. Examples verified present in the binary: `BAD_TPU_CHIP` → "Megascale detects a hang that is likely caused by bad TPU tensor core chips on the following hosts. Please remove the hosts from the fleet and restart the workload." (`0xa05862f`) followed by "  The host that have bad TPUs are: " (`0xa273611`); `NETWORKING_ISSUE` → "…likely caused by a networking issue. Please examine the underlying networking stack for the following hosts." (`0x9ffc2f2`); `UNKNOWN_CAUSE` → "Megascale detects a hang but cannot determine the root cause. Please inspect the full digest below." (`0x9fe02ae`) plus the full textual proto. The coordinator's LOG(ERROR) trail is the primary *human* consumer of the digest.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `MegascaleErrorAggregator::ProcessAndShutdown` | `0x1ccbaba0` | build/cache digest, classify, set `shutdown_` (idempotent) | HIGH |
| `MegascaleErrorAggregator::LogErrorDigest` | `0x213b42c0` | cause-switched LOG(ERROR) emitter + `FormatWorkers` | HIGH |
| `ErrorDigest::ToRapidEyeErrorDigestProto` | `0x1ccb8560` | `ErrorDigest` → wire proto | HIGH |
| `(anon)::FormatWorkers` | `0x1ccba060` | render a `btree_set<WorkerAndCoreInfo>` host list | HIGH |
| `(anon)::ToWorkerAndCoreInfoProto` | `0x1ccc4f80` | culprit-worker → proto message | HIGH |

---

## Wire Format — `RapidEyeErrorDigestProto`

### Purpose

The single serialized artifact the aggregator persists is `xla.megascale.runtime.RapidEyeErrorDigestProto`. It is a 17-live-field root carrying 13 nested message types and a 9-value `Cause` enum, recovered from the `rapideye_logging.proto` FileDescriptorProto at `protodesc_cold` VA `0xc169340` (4 460 bytes serialized). Its syntax is `editions` (Google's proto2/3 successor). Crucially the digest **embeds the inbound `MegaScaleRuntimeError` verbatim** (field 11, `first_recorded_error`), so the record is self-describing — a downstream reader needs no separate inventory of the inbound errors to interpret the verdict.

### Root message

| Field # | Name | Label | Type | Confidence |
|--------:|------|-------|------|---|
| 1 | `potential_cause` | optional | enum `Cause` | HIGH |
| 2 | `potential_culprit_workers` | repeated | `WorkerAndCoreInfo` | HIGH |
| 4 | `workers_by_tpu_states` | repeated | `WorkersByTpuState` | HIGH |
| 5 | `all_workers` | repeated | `WorkerInfo` | HIGH |
| 7 | `timestamp_ns` | optional | int64 | HIGH |
| 11 | `first_recorded_error` | optional | `MegaScaleRuntimeError` (embedded verbatim) | CERTAIN |
| 12 | `error_messages` | repeated | `ErrorMessage` | HIGH |
| 13 | `graph_consolidater_output` | optional | `GraphConsolidaterOutput` | HIGH |
| 14 | `event_id` | optional | fixed64 | HIGH |
| 15 | `xid` | optional | int64 (from `getenv("XM_XID")`) | HIGH |
| 16 | `executable_by_modules` | repeated | `ExecutableByModules` | HIGH |
| 17 | `app_type` | optional | string | HIGH |
| 18 | `faulty_network_links` | repeated | `FaultyNetworkLink` | HIGH |
| 19 | `offloaded_sparse_core_tag_pc` | repeated | `OffloadedSparseCoreTagPc` | HIGH |
| 20 | `build_info` | optional | `BuildInfo` | HIGH |
| 21 | `launches` | repeated | `Launch` | HIGH |
| 22 | `runtime_error_ids` | repeated | fixed64 | HIGH |

Field numbers 3, 6, 8, 9, 10 are gaps — retired fields. The dependency `megascale_status.proto` supplies `MegaScaleRuntimeError`; `actions.proto` supplies the `MegaScaleAction` embedded in `BottleneckNode.action`.

### The `Cause` enum

Nine values, dense `0..8`; names are present in the `protodesc_cold` table (`FINGERPRINT_MISMATCH`, `PROGRAM_NOT_QUEUED`, etc. verified by string scan of `libtpu.so`):

| Value | Name | Meaning | Locality of evidence |
|------:|------|---------|---|
| 0 | `UNKNOWN_CAUSE` | classifier exhausted, no root cause pinned | fleet (exhaustion) |
| 1 | `BAD_TPU_CHIP` | hang likely from a faulty tensor-core chip | per-host chip (named host) |
| 2 | `FINGERPRINT_MISMATCH` | inconsistent HLO compile output across workers | **fleet-wide** (needs comparison) |
| 3 | `DATA_INPUT_STALL` | data pipeline starvation | per-host (data pipeline) |
| 4 | `UNRECOVERABLE_ERROR` | at least one report was `UNRECOVERABLE` | per-host (one host suffices) |
| 5 | `DIFFERENT_MODULE` | workers running mismatched HLO modules | **fleet-wide** (needs comparison) |
| 6 | `NETWORKING_ISSUE` | ICI/DCN link failure detected | host-pair (`src→dst`) |
| 7 | `BAD_SC_CHIP` | hang likely from a faulty sparse-core chip | per-host chip (named host) |
| 8 | `PROGRAM_NOT_QUEUED` | a worker never reached the launch queue (`chip_id == -1`) | per-host |

### Nested message tree

The 13 nested messages are described by their shape rather than dumped exhaustively; the diagnostic-critical ones:

```text
RapidEyeErrorDigestProto
├─ WorkerAndCoreInfo {1 worker_id, 2 host_name, 3 CoreInfo?}     ← culprit naming
│    └─ CoreInfo {1 chip_id, 2 core_idx, 3 physical_location}    ← only physical chip name in the layer
├─ WorkerInfo {1 worker_id, 2 host_name, 3 cloud_instance_id}
├─ WorkersByTpuState {1 TpuState, 2 [WorkerAndCoreInfo]}         ← sequencer-state grouping
│    └─ TpuState {1 tag, 2 pc, 3 hlo_name, 4 computation_name, 5 [ProgramCompletion]}
├─ Launch {1 launch_id, 2 [WorkersByTpuState], 3 [Executable]}
├─ Executable {1 fingerprint, 2 [Launch], 3 module_name, 4 sample_worker}
├─ ExecutableByModules {1 module_name, 2 [Executable]}
├─ FaultyNetworkLink {1 src_worker:WorkerInfo, 2 dst_worker:WorkerInfo}  ← host-granular fault
├─ GraphConsolidaterOutput {1 problematic_workers, 4 workers_with_missing_graphs,
│                            5 [BottleneckNode], 6 [FaultyNetworkLink]}
├─ ErrorMessage {1 worker:WorkerAndCoreInfo, 2 error_message}
├─ OffloadedSparseCoreTagPc {1 hlo, 2 [SparseCoreTagPcToWorkers]}
└─ BuildInfo {1 changelist, 2 baseline_changelist, 3 build_timestamp_secs,
              4 build_label, 5 build_target, 6 build_id}
```

> **NOTE —** `CoreInfo {chip_id, core_idx, physical_location}` inside `WorkerAndCoreInfo` is the *only* place in the cross-host layer where a chip is named physically — and it is diagnostic output, not part of the bootstrap inventory. A `FaultyNetworkLink` names *hosts* (`WorkerInfo`), not ICI links. This is why a `BAD_TPU_CHIP`/`BAD_SC_CHIP` verdict points at hosts and the operator must follow up with [`tpunetd`](tpunetd-protocol.md)'s `GetCoreDump(CORE_DUMP_CHIP_DUMP)` to pull chip-level register state — the digest knows *which host*, tpunetd knows *which chip*. See [barrier-error usage](fleet-metadata/barrier-error-usage.md).

### Embedded `MegaScaleRuntimeError` (field 11 / `ReportErrorRequest` field 3)

Per-host report, source `megascale_status.proto`, syntax `editions`:

| Field # | Name | Type | Note |
|--------:|------|------|------|
| 1 | `error_type` | enum `ErrorType` | drives CANCELLED short-circuit & HANG abort |
| 2 | `error_message` | string | |
| 3 | `estimated_start_time_ms` | uint64 | |
| 4 | `runtime_state` | `RuntimeStateSummary` | carries `RapidEyeInfo` for the classifier |
| 5 | `triggering_launch_id` | int32 | |
| 6 | `hostname` | string | |
| 7 | `user` | string | |
| 8 | `job_name` | string | |
| 9 | `task_id` | int32 | the inner dedup-key segment (`+0x58`) |
| 10 | `task_uid` | int64 | |
| 11 | `event_id` | fixed64 | correlation id, not a dedup key |
| 12 | `unrecoverable_error_type` | enum `UnrecoverableErrorType` | |
| 13 | `runtime_error_id` | fixed64 | correlation id → root field 22 |

```text
enum ErrorType { NO_ERROR=0; HANG_DETECTED=1; UNRECOVERABLE_ERROR=2; CANCELLED=3; }
enum UnrecoverableErrorType { UNCLASSIFIED=0; HOST_TO_DEVICE_ERROR=1; DEVICE_TO_HOST_ERROR=2; }
```

`error_type` is the *per-report* category; the root `Cause` is the *cross-host verdict*. They are different axes — a fleet of `HANG_DETECTED` reports can classify to any of the nine causes depending on the per-host `RapidEyeInfo` state. `event_id` and `runtime_error_id` are unique-per-collection identifiers carried for cross-fleet correlation (OutKast / RapidEye), explicitly **not** the dedup key.

---

## Retention

### Purpose

Retention is deliberately minimal: hold the errors only long enough to drain one storm. The primary store is an in-memory `linked_hash_map`; an opt-in `RapidEyeLogger` sink writes the serialized digest to disk; nothing survives a process restart.

### The model

```text
default (no flags):   in-memory linked_hash_map  →  LOG(ERROR) on drain  →  freed
                       (NullRapidEyeLogger drops all WriteRecord calls)

--megascale_rapideye_error_digest_log_path=<p>:
                       in-memory map  →  ProcessAndShutdown  →
                       CloudRapidEyeLogger.WriteRecord(RapidEyeErrorDigestProto)  →  <p>
                       (scheme picks backend: gs:// → GCS, /cns/ → CNS, local → Posix)
```

| Aspect | Behavior | Evidence |
|---|---|---|
| TTL | none — lives until `ProcessAndShutdown` | `ProcessAndShutdown` `0x1ccbaba0` sets `shutdown_` then frees |
| Count cap | none — `linked_hash_map` grows to one entry per unique key | `+0xe8`, no bound; retired `num_workers_to_log_errors` flag |
| Byte cap | per-error only, at the host tier (truncate, never drop) | `--max_rapid_eye_runtime_error_size_bytes`; `is_truncated` set |
| Ring / LRU | none | no eviction site in `AddError` |
| Cross-restart | none — no load on ctor; the log is write-only | `RapidEyeLogger` has no `Read` on its vtable |
| Drain | one-shot; `ProcessAndShutdown` empties and dies | idempotent fast path at `0x1ccbaba0` line ~817 |

> **QUIRK —** the `linked_hash_map` is *unbounded by design*. There is no LRU, no ring buffer, no eviction sweep — the formerly-present `num_workers_to_log_errors` flag that capped logged-error count was retired (visible via `absl::flags_internal::Retire` in `_GLOBAL__sub_I_megascale_error_aggregator.cc` `0x213560a0`). The map is bounded *only* by the dedup key collapsing per-host duplicates plus the `size() == expected` early-fire that drains it the instant every expected worker has reported. A reimplementation that omits the early-fire and relies on the 300 ms idle deadline alone will hold the storm in memory until 300 ms pass with no new report; with one ~112-byte `MegaScaleRuntimeError` plus a 24-byte string per worker, a 1 000-chip pod's worst case is ~150 kB plus message payload — bounded, but only because the map is one-entry-per-worker, not because anything evicts.

### The sticky first-error slot

`first_recorded_error_` at `+0x70` is set exactly once, on the first non-`CANCELLED` `AddError`, and is never overwritten. It becomes `RapidEyeErrorDigestProto.first_recorded_error` (field 11) and is the basis for the abort decision (`abort_on_hang` keys off `first_recorded_error.error_type == HANG_DETECTED`). The aggregator thus preserves **both** the root cause (first error, sticky) and the complete inventory (the full map, last-write-wins) — first-error-wins for the verdict, keep-everything for the forensic record.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `MegascaleErrorAggregator::WriteErrorDigestToStorage` | `0x1ccb83e0` | empty-path skip; else `RapidEyeLogger::Create` + `WriteRecord` | HIGH |
| `RapidEyeLogger::Create` | `0x20511aa0` | `StatusOr<unique_ptr<RapidEyeLogger>>` from options | HIGH |
| `NullRapidEyeLogger::WriteRecord` | `0x20511d40` | default sink — drops all writes | HIGH |
| `CloudRapidEyeLogger::WriteRecord` | `0x20511e00` | `tsl::FileSystem`-backed write | HIGH |

---

## Class Layout — `MegascaleErrorAggregator` (752 bytes)

The 752-byte (`0x2f0`) heap object, allocated 16-aligned. Scope-relevant offsets:

| Offset | Type | Field | Confidence |
|---|---|---|---|
| `+0x00` | `std::string` | `job_name` (`"McJax"` on the coordinator) | CERTAIN |
| `+0x18` | `std::string` | `rapideye_log_dir` (FLAG copy) | HIGH |
| `+0x30` | `bool` | `shutdown_` (`active()` = `!shutdown_`) | CERTAIN |
| `+0x50` | `AnyInvocable<void(int,int,MegaScaleRuntimeError const&)>` | per-error unicast callback | MEDIUM |
| `+0x60` | `int64_t` | `global_id_` (`util::random::NewGlobalID()`) | HIGH |
| `+0x68` | `int64_t` | `xid_` (`getenv("XM_XID")`) | HIGH |
| `+0x70` | `MegaScaleRuntimeError` (`0x70`) | `first_recorded_error_` (sticky) | HIGH |
| `+0xe0` | `bool` | `has_first_recorded_error_` | HIGH |
| `+0xe8` | `linked_hash_map<string, MegaScaleRuntimeError>` | `errors_by_worker_launch_` (the store) | CERTAIN |
| `+0x128`..`+0x2e7` | `ErrorDigest` (`0x1c0` = 448 B) | `cached_digest_` (filled on drain); spans up to `mu_`. Standalone `ErrorDigest` confirmed `operator new(0x1C0)` in PJRT `ProcessAndShutdown` `0xe6bae40` | HIGH |
| `+0x1a8`..`+0x2b0` | 5 `btree_set` / `btree_map` | indices built during `ProcessAndShutdown`; sub-fields *inside* `cached_digest_` (not separate aggregator members) | MEDIUM |
| `+0x2e8` | `absl::Mutex` | `mu_` — the *only* lock (`this+744`) | CERTAIN |

> **NOTE —** one `absl::Mutex` at `+0x2e8` guards every public method — `AddError`, `ProcessAndShutdown`, `size`, `active` all lock `this+744`. There is no lock-free path. The coordinator's `ErrorReporter` adds a *separate* `TracedMutex` (kind=14) so the lazy-alloc, deadline arm, AddError, and early-fire check are atomic with respect to a concurrent deadline fire; the deadline fiber re-takes that mutex and the `if (!aggregator) return` idempotence guard at `ProcessErrorDigest` (`0x1ccb7140`) handles a late report racing the drain.

---

## Alerts and Counters

Two coordinator-tier flags gate a process abort after the digest is emitted (the abort feeds the normal Borg/GKE crash-loop restart pipeline — neither flag pages directly):

| Flag | Default | Effect |
|---|---|---|
| `--megascale_error_reporter_abort_on_hang` | **false** | `LOG(FATAL)` if `first_recorded_error.error_type == HANG_DETECTED` ("Aborting the coordinator after collecting errors from all workers…", `0xa045d37`) |
| `--megascale_error_reporter_abort_on_error` | **false** | `LOG(FATAL)` on any reported error after the digest |
| `--megascale_error_aggregation_enabled` | **true** | master switch; false → ctor sets `shutdown_` so every `AddError` is a no-op |
| `--megascale_rapideye_outkast_culprit_hosts` | **false** | forward the culprit host list to OutKast for cross-fleet correlation |

The streamz counter `/platforms/xla/megascale/runtime/error_count` (also `/platforms/xla/megascale/error_count` and `platforms.xla.megascale.error_count`) increments per `AddError`; `/platforms/xla/megascale/runtime/aggregated_error` increments once per `ProcessAndShutdown` and is keyed by `(error_type, cause)` (metadata string carries the verbatim typo "The number of error reported by the MXLA errore aggregator."). Two distinct anonymous-namespace BSS slots back the per-instance `error_count` at `0x2257b080` (gRPC path) and `0x22589a40` (host/PJRT path).

---

## How the Aggregated Error Surfaces Back to PJRT

The digest reaches a PJRT client by four routes, each a distinct consumer:

1. **Coordinator LOG(ERROR) trail** — `LogErrorDigest` emits the cause verdict and per-cause diagnostic text; the primary human consumer reading coordinator logs.
2. **In-process unicast callback** — the `AnyInvocable<void(int slice, int host, MegaScaleRuntimeError const&)>` passed to the `ErrorReporter` ctor fires *per individual error* (not per digest); the typical consumer is the runtime error handler that propagates the failure back to the PJRT executor (JAX/TF/XLA), so user code sees a failed computation rather than a silent hang.
3. **`MegascaleDebugService.ConsumeError` gRPC** — `ConsumeErrorRequest{}` (empty) → `ConsumeErrorResponse{error: MegaScaleRuntimeError}`; the "give me the next error" inspection path used by debuggers. Backed by `DebugManager::ConsumeRuntimeError` (`0x204d8f20`), which is *drain-once*: reading the pending slot resets it.
4. **RapidEye on-disk archive** (opt-in) — `CloudRapidEyeLogger` writes the serialized proto for downstream batch jobs (incident analysis, sawmill ingestion, OutKast scoring).

The PJRT C-API shims (`PJRT_Megascale_ErrorAggregator_*`) are the *fifth* surface: a JAX/XLA process drives `AddError`/`ProcessAndShutdown` directly and reads the 448-byte `ErrorDigest` in-process, with no coordinator involved.

---

## Not Traced / Open

- The exact tie-break scoring inside `ProcessAndShutdown` when two adjacent causes both fire (e.g. `BAD_TPU_CHIP` vs `FINGERPRINT_MISMATCH`) — the 5320-byte body is recovered but a full data-flow extraction is pending (MEDIUM).
- The numeric default of `--max_rapid_eye_runtime_error_size_bytes` — an int flag whose default lives in `.data` with no generator fn; needs a `.data` cross-reference (LOW).
- `RuntimeStateSummary` / `RapidEyeInfo.chip_config_name` schema — the classifier keys off the `"default"` string (8-byte compare at `0x1ccbaf56`) but the upstream `RapidEyeInfo` schema is only partially decoded (`{1 telemetry, 2 tpu_core_hlos, 3 is_truncated, 6 slice_id, 7 task_id, 10 chip_config_name, …}`).
- The `megascale_xmon_gateway_address` consumer (a potential third push path alongside LOG/RapidEye/callback) — the flag is registered in this TU but its read site is not traced (LOW).
- `MegaScaleAction` wire format inside `BottleneckNode.action` (a proto oneof of 7+ alternatives) — only the top-level message graph is catalogued.

---

## Cross-References

- [Megascale Overview](overview.md) — the section opener; where the aggregator sits in the multi-host/DCN runtime.
- [ICI Failure Recovery](../ici/failure-recovery.md) — the layer below; how a chip/link fault becomes a `MegaScaleRuntimeError` before it ever reaches `AddError`.
- [Barrier and Error-Aggregator Fleet-Metadata Usage](fleet-metadata/barrier-error-usage.md) — how the aggregator consumes only the flat DCN layer (`NumHosts()`, `(slice_id, host_id)`); the host-vs-chip forensic handoff.
- [Cross-Host Barrier](cross-host-barrier.md) — the sibling fleet coordinator; shares the `(slice_id, host_id)` identity and `NumHosts()` expected-count source.
- [tpunetd Protocol](tpunetd-protocol.md) — `GetCoreDump(CORE_DUMP_CHIP_DUMP)`, the chip-level follow-up after a `BAD_TPU_CHIP`/`BAD_SC_CHIP` verdict names a host.
- [Host Identity](fleet-metadata/host-identity.md) — the `(slice_id, host_id)` DCN identity that becomes the outer dedup-key segment.
