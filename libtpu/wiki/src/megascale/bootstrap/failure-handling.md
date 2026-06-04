# Failure Handling

Megascale's bootstrap has three structural failure modes, each
producing a distinctive log signature and a distinct propagation
path. The behaviours below are the ones the binary actually
implements, recovered from disassembly and string anchors; there
is no retry policy on the rendezvous RPCs themselves.

## Failure 1: worker times out on `GetMultiSliceTopology`

A worker's gRPC deadline is set from
`FLAGS_megascale_topology_discovery_timeout` (Options field at
`+0x34..+0x40`, an `absl::Duration`). When the deadline fires before
the coordinator's `TopologyCoordinator` accumulates a quorum, the
worker's stub returns `DEADLINE_EXCEEDED` to
`DiscoverTopologyAndAddressBindings`
(`xla::megascale::runtime::CommunicationBackend::DiscoverTopologyAndAddressBindings`,
`0x1ccacb80`), which:

1. **CONFIRMED.** Returns the error to the XLA Megascale runtime via
   the `StatusOr<tuple<MultiSliceTopologyAndLocationProto,
   EndpointAddresses>>` constructed at `0x1ccacea4` (the
   `absl::StatusOr<...>::StatusOr` call inside
   `DiscoverTopologyAndAddressBindings`; arg setup at `0x1ccacea1`).
   The worker side does **not** emit the
   `"TopologyCoordinator: Unable to wait for all slices to
   connect..."` log; that string (`0x96df1d4`) is emitted by the
   **coordinator's** destructor — see Failure 2.
2. The runtime calls
   `CommunicationBackend::ReportError(slice_id, host_id,
   MegaScaleRuntimeError{...})`
   (`xla::megascale::runtime::CommunicationBackend::ReportError(int, int,
   MegaScaleRuntimeError const&)`, `0x1ccadbe0`) — this is the
   `MegaScaleTransport.ReportError` gRPC
   (`/xla.megascale.runtime.MegaScaleTransport/ReportError`) that fans
   into the coordinator's `ErrorReporter` (P-3-45). The
   `MegaScaleRuntimeError` carries the `error_message` field
   (`xla.megascale.runtime.MegaScaleRuntimeError.error_message`) and an
   `ErrorType` enum
   (`MegaScaleRuntimeError.ErrorType`); the `UNRECOVERABLE_ERROR`
   member is real, though the bootstrap-timeout message text is
   constructed at runtime, not a fixed rodata literal.
3. The runtime returns a non-OK Status to PJRT, which surfaces it
   to user code.

If `--megascale_use_inplace_restart_for_error` is set, the runtime
attempts to tear down and re-instantiate the
`CommunicationBackend` in-place rather than exiting. Otherwise the
process exits and the cluster scheduler restarts it (Borg/GKE
crash-loop), at which point bootstrap retries from scratch.

## Failure 2: coordinator never sees enough registrations

The coordinator's `TopologyCoordinator` does NOT have its own
timeout — the `Coordinator<>` base does not call
`WaitForNotificationWithTimeout` or any per-instance timer. The
bound on bootstrap time is imposed entirely by the per-RPC deadline
that each worker attaches. So what happens when registrations keep
arriving late:

1. The coordinator's pending-callback vector at `+0x88` grows.
2. **CONFIRMED.** The periodic `ReportStatus()`
   (`xla::megascale::runtime::TopologyCoordinator::ReportStatus`,
   `0x213b7ba0`) continues to emit
   `"MegaScale Topology Discovery in progress. Missing hosts
   (num_slices=<N>, num_hosts=<H>): [<list>]"` (`0x96df232`)
   with the list of slices/hosts that haven't checked in.
3. As individual workers hit their deadlines, the gRPC framework
   cancels their server-side requests. The pending callback in
   the coordinator's vector is still invoked when the response is
   eventually built — but the call now lands on a cancelled
   `ServerCallbackUnaryImpl`, which silently drops the response.
4. When the slowest worker eventually shows up (or a restarted
   worker that uses the same `(slice_id, host_id)`), the
   coordinator's `IsComplete()` transitions true,
   `CreateResponse()` runs, and every still-active pending
   callback receives the response. Cancelled ones get a no-op.

The coordinator does NOT abort. The process keeps running with the
TopologyCoordinator still in state=1, ready to accept the missing
registrations. If the operator decides the rendezvous is doomed,
they must kill the coordinator process externally.

**CONFIRMED.** The `"TopologyCoordinator: Unable to wait for all
slices to connect. The missing hosts (num_slices=<N>,
num_hosts=<H>): [<list>]"` log (`0x96df1d4`) is emitted only when the
`TopologyCoordinator` is **destroyed** without converging — by
`xla::megascale::runtime::TopologyCoordinator::~TopologyCoordinator`
(`0x1cf511a0`), which calls `GetMissingHosts()` and formats the
remaining slices/hosts. It is not emitted by `DiscoverTopology...` on
the worker, nor by `ReportStatus()` during the wait.

A coordinator-specific log is
`"Some workers didn't report an error after 5 minutes: ..."`
(rodata `0xa238672`) — this comes from the
`ErrorReporter::ProcessErrorDigest()` path
(`xla::megascale::runtime::ErrorReporter::ProcessErrorDigest`,
`0x1ccb7140`, P-3-45), not from
TopologyCoordinator itself. It fires when the **error**
aggregation in turn times out; topology aggregation has no such
log.

## Failure 3: re-key / drift mismatch

When a worker re-registers with a different `topology_args`,
different `host_addresses`, or different `incarnation_id`, the
coordinator catches the drift in two places:

**CONFIRMED.** All three drift checks live in one function —
`xla::megascale::runtime::TopologyCoordinator::ProcessRequest`
(`0x1cf524c0`) — not split across a separate broadcast-time path.
Two `proto2::util::MessageDifferencer::Compare` calls (`0x1cf5269d`
and `0x1cf527a4`, each preceded by
`set_message_field_comparison(1)` + `ReportDifferencesToString`)
drive the topology and host-address comparisons; the incarnation id
is compared inline. The three warnings are:

- `"Received topology that differs from previously registered
  topology at same sliceID. SliceID: $0 Previous HostId: $1
  New HostId: $2 Addresses: $3 Diff: $4"` (`0x9b27486`) when
  `topology_args` differs (after the second `Compare` at
  `0x1cf527a4`).
- `"Received host address mapping that differs from previous
  mapping SliceID: $0 HostId: $1 Prev Address: $2 New
  Addresses: $3"` (`0x9c14204`) when `host_addresses` differs
  (after the first `Compare` at `0x1cf5269d`).
- `"Received incarnation ID that is different from previous
  incarnation ID. SliceID: $0 HostId: $1 Prev IncarnationId:
  $2 New IncarnationId: $3"` (`0x9c14456`) when only the
  incarnation id has changed. This is the signal of a silent
  worker restart.

All three use `absl::SubstituteAndAppendArray` (literal lengths
153 / 122 / 139 respectively in the decompile). The drift is logged
at WARNING but **not** treated as fatal: the coordinator retains the
originally accepted state and serves the cached response.

The drift warnings are informational. The coordinator does not
invalidate its cache; the restarted worker reuses the original
address table. This is the **intended** behaviour because most
restarts come back with the same network endpoints.

A separate once-per-`(slice,host)` de-duplication exists for
logging unique ids — `LogUniqueIds` (inlined into
`xla::megascale::runtime::Communicator::Create`, `0x1cca9aa0`) holds
three `int` slots `last_ids.0/.1/.2` at `0x223717c0`, `0x223717c4`,
`0x223717c8` guarded by a `unique_id_mutex`. This machinery throttles
unique-id logging on the response path; it does **not** emit the
three drift warnings above.

To force a real re-bootstrap the operator must kill the
coordinator process; on coordinator restart the new
`TopologyCoordinator` starts empty and every worker must re-issue
`GetMultiSliceTopology`.

## Barrier failure

`BarrierCoordinator` has the same timeout-on-the-RPC model. Each
`BarrierRequest` carries `barrier_timeout_in_ms` (field 4) which
acts as the per-call deadline. When a participant times out:

1. Worker side logs the local deadline-exceeded error.
2. **CONFIRMED.** When the `BarrierCoordinator` is destroyed without
   all participants having checked in, its destructor
   (`xla::megascale::runtime::BarrierCoordinator::~BarrierCoordinator`,
   `0x1cf55760`) emits
   `"BarrierCoordinator: Unable to wait for all slices to connect.
   Saw "` (`0xa1b93bb`) `+ "<K>" + " of " + "<NumW>"
   + " expected participants. Seen hosts: " + GetSeenHosts()`.
   The three rodata fragments (`Saw `, ` of `, ` expected
   participants. Seen hosts: `) are concatenated via
   `LogMessage::CopyToEncodedBuffer`, and the host list comes from
   `BarrierCoordinator::GetSeenHosts()`. This is the same
   destructor-on-non-convergence pattern as the topology coordinator,
   not a `ReportStatus()` emission.
3. The runtime treats a failed barrier as an
   `UNRECOVERABLE_ERROR` via `ReportError`.

If `--xla_tpu_enable_megascale_barrier=false` the entire barrier
mechanism is bypassed — `BarrierCoordinator` is never instantiated
and no `Barrier` RPCs are issued. This is a debug/testing knob;
production deployments always have it on.

## HeartBeat failure (post-bootstrap)

After bootstrap completes the runtime arms `StartHeartBeat()`
(`0x1ccade60`) which periodically pings every peer. Heartbeat
failures cascade through:

- `"Failed to send heartbeat to <peer>"` (`0xa1ec1d8`)
- `"Failed to perform heartbeat check"` (`0x863656b`)
- `"Stop heartbeat check watchdog"` (`0x8655783`)

Three abort policies, gated by flags:

| Flag | Effect on heartbeat failure |
|------|----------------------------|
| `--megascale_use_inplace_restart_for_failed_heartbeat` | `"Aborting (w/ restart) due to failed heartbeat."` (`0x9ffa7d9`) — runtime tears down and recreates the CommunicationBackend in place |
| `--megascale_restart_on_failed_heartbeat` (without inplace) | `"Aborting (w/o restart) due to failed heartbeat."` (`0x9ffa7a9`) — process exits, scheduler restarts |
| Neither flag | `"Terminating the process due to failed heartbeat."` (`0x9ffa778`) — process exits, no restart |

Heartbeat is logically **not** part of bootstrap — it runs only
after `DiscoverTopologyAndAddressBindings` returns OK — but it
inherits the address table built during bootstrap, so its failure
modes implicitly verify that table.

## Propagation into `ErrorReporter` / `RapidEye`

Every bootstrap failure mode that calls `ReportError` ends up in
the coordinator's `MegascaleErrorAggregator` (P-3-45). The
`MegaScaleRuntimeError.ErrorType` carries the `UNRECOVERABLE_ERROR`
member (the numeric ordinal is not independently confirmed here). The
aggregator's classifier maps this into the
`RapidEyeErrorDigestProto.Cause` enum member `UNRECOVERABLE_ERROR`
(a real `Cause` value alongside `BAD_TPU_CHIP`, `NETWORKING_ISSUE`,
`DATA_INPUT_STALL`) and emits the diagnostic template
`"Some workers have halted with an unrecoverable error: ..."`
(`0xa23e476`) from
`xla::megascale::runtime::MegascaleErrorAggregator::LogErrorDigest`
(`0x213b42c0`).

If `--megascale_rapideye_error_digest_log_path` is set, the digest
including the failed-host list is written to the path via
`CloudRapidEyeLogger`. Otherwise the failure is captured only in
the LOG(ERROR) trail on the coordinator. See P-3-45 for the full
retention and consumer chain.

## What does NOT exist

- No bootstrap-side retry. Workers do not internally retry
  `GetMultiSliceTopology` on transient failures.
- No coordinator-side timeout on the rendezvous itself. The
  rendezvous can wait forever as long as workers keep arriving
  before their per-RPC deadline.
- No on-disk persistence of the assembled topology. Coordinator
  restart loses the cache.
- No alternate channel (UDS, shared memory) for the rendezvous
  payload. Everything goes through gRPC over TCP.
- No bounded queue for pending callbacks. A coordinator process
  holding tens of thousands of pending workers will keep them all
  in memory.
