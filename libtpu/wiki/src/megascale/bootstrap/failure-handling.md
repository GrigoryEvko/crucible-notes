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
`DiscoverTopologyAndAddressBindings`, which:

1. Logs `"TopologyCoordinator: Unable to wait for all slices to
   connect. The missing hosts (num_slices=<N>, num_hosts=<H>):
   [<list>]"` (rodata `0x96df1d4`). On the worker side the
   missing-hosts list is empty because the worker has no global
   state to report — only the coordinator emits a meaningful
   list.
2. Returns the error to the XLA Megascale runtime via the
   `StatusOr<tuple<MultiSliceTopologyAndLocationProto,
   EndpointAddresses>>` out-parameter at
   `0x1ccacea1`.
3. The runtime calls
   `CommunicationBackend::ReportError(slice_id, host_id,
   MegaScaleRuntimeError{error_type=UNRECOVERABLE_ERROR=2,
   error_message="topology discovery timed out"})` — this is the
   `MegaScaleTransport.ReportError` gRPC that fans into the
   coordinator's `ErrorReporter` (P-3-45).
4. The runtime returns a non-OK Status to PJRT, which surfaces it
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
2. The periodic `ReportStatus()` continues to emit
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

A coordinator-specific log is
`"Some workers didn't report an error after 5 minutes: ..."`
(rodata `0xa238672`) — this comes from the
`ErrorReporter::ProcessErrorDigest()` path (P-3-45), not from
TopologyCoordinator itself. It fires when the **error**
aggregation in turn times out; topology aggregation has no such
log.

## Failure 3: re-key / drift mismatch

When a worker re-registers with a different `topology_args`,
different `host_addresses`, or different `incarnation_id`, the
coordinator catches the drift in two places:

1. **At `ProcessRequest` time.** The
   `MessageDifferencer::Compare` calls at `0x1cf526a4` and
   `0x1cf5279e` log:
   - `"Received topology that differs from previously registered
     topology at same sliceID. SliceID: $0 Previous HostId: $1
     New HostId: $2 Addresses: $3 Diff: $4"` (`0x9b27486`) when
     `topology_args` differs.
   - `"Received host address mapping that differs from previous
     mapping SliceID: $0 HostId: $1 Prev Address: $2 New
     Addresses: $3"` (`0x9c14204`) when `host_addresses` differs.
   The drift is logged at WARNING but **not** treated as fatal.
   The coordinator retains the originally accepted state and
   serves the cached response.
2. **At response broadcast time.** `LogUniqueIds` at the response
   site walks the three static slots at `0x223717c0..0x223717c8`
   and emits:
   - The topology drift warning above (slot 0).
   - The address mapping drift warning above (slot 1).
   - `"Received incarnation ID that is different from previous
     incarnation ID. SliceID: $0 HostId: $1 Prev IncarnationId:
     $2 New IncarnationId: $3"` (`0x9c14456`) when only the
     incarnation id has changed (slot 2). This is the signal of a
     silent worker restart.

The drift warnings are informational. The coordinator does not
invalidate its cache; the restarted worker reuses the original
address table. This is the **intended** behaviour because most
restarts come back with the same network endpoints.

To force a real re-bootstrap the operator must kill the
coordinator process; on coordinator restart the new
`TopologyCoordinator` starts empty and every worker must re-issue
`GetMultiSliceTopology`.

## Barrier failure

`BarrierCoordinator` has the same timeout-on-the-RPC model. Each
`BarrierRequest` carries `barrier_timeout_in_ms` (field 4) which
acts as the per-call deadline. When a participant times out:

1. Worker side logs the local deadline-exceeded error.
2. Coordinator side, on the next `ReportStatus()`, emits
   `"BarrierCoordinator: Unable to wait for all slices to connect.
   Saw <K> of <NumW> expected participants. Seen hosts:
   <list>"` (`0xa1b93bb`).
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
the coordinator's `MegascaleErrorAggregator` (P-3-45). The error
type is `UNRECOVERABLE_ERROR=2`. The aggregator's classifier maps
this into `Cause::UNRECOVERABLE_ERROR` and emits the diagnostic
template:
`"Some workers have halted with an unrecoverable error: ..."`
(`0xa23e476`).

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
