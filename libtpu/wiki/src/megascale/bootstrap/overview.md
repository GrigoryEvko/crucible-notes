# Megascale Bootstrap Rendezvous — Overview

The Megascale bootstrap rendezvous is the cross-host handshake that
brings up an XLA Megascale multi-slice TPU job. Each worker process
on every TPU host participates exactly once at startup; when the
rendezvous completes, every process holds an identical
**multi-slice address table** that maps every `(slice_id, host_id)`
pair to the network addresses needed for steady-state collectives
and host transfers.

The rendezvous is **strictly centralised**. One process — the
coordinator — owns the rendezvous state. Every other process
contacts the coordinator over gRPC, registers its own bindings, and
blocks until the coordinator has assembled the full topology.

## What the rendezvous accomplishes

After bootstrap:

1. **Address table.** Every process knows the gRPC endpoint of every
   other process, indexed by `(slice_id, host_id)`. The XLA Megascale
   runtime uses this table to populate the per-peer
   `NetworkAddressMapping` array that drives all subsequent
   `MegaScaleTransport.Send` collective RPCs and DCN host transfers.
2. **Consistent topology view.** All processes agree on the
   `MultiSliceTopologyInfo`: total slice count, per-slice host count,
   per-slice `TpuTopologyArgsProto`, and the unified endpoint list.
   The agreement is enforced by the coordinator running
   `proto2::util::MessageDifferencer::Compare` over every incoming
   topology proto against the first one it saw.
3. **Single completion notification.** Every blocked worker is
   unblocked simultaneously when the coordinator's
   `TopologyCoordinator` reaches its quorum. Late arrivals receive
   a cached copy of the same response without forcing the
   coordinator to recompute.
4. **A shared `incarnation_id` per worker.** Each worker injects a
   per-process `NewGlobalID()` into its request; the coordinator
   remembers the latest one per `(slice_id, host_id)` slot so it can
   detect and warn about silent worker restarts.

## How the rendezvous fits between tpunetd and PJRT

The Megascale bootstrap is the **middle layer** of TPU cluster
bringup:

```
   PJRT distributed CoordinationService (xla.coordination)
      ↑   (Python-level rendezvous: shard assignment, run id, ...)
      │
   ┌──┴───────────────────────────────────────────────────┐
   │ xla::megascale::runtime::CommunicationBackend         │
   │   - GrpcTransport server on MEGASCALE_PORT            │
   │   - On the coordinator process:                       │
   │       TopologyCoordinator   (cross-slice rendezvous)  │
   │       BarrierCoordinator map (per-id rendezvous)      │
   │       ErrorReporter (P-3-45)                          │
   │   - On every process:                                 │
   │       address table, peer Send fan-out, HeartBeat     │
   └──┬────────────────────────────────────────────────────┘
      ↑   uses tpunetd's per-host chip-coordinate bindings as input
      │
   ┌──┴────────────────────────────────────────────────────┐
   │ superpod::tpunetd_client::TpunetdClient (P-3-11)      │
   │   - SessionMaster talks to tpunetd over UDS           │
   │   - Brings up ICI fabric WITHIN this slice            │
   │   - BroadcastBarrier rendezvous WITHIN this slice     │
   └──┬────────────────────────────────────────────────────┘
      ↑
   tpunetd daemon: SessionControl + ICI fabric controller
```

Where the layers cleave:

- **tpunetd handles intra-slice bringup**: ICI link configuration,
  routing tables, global-time-counter sync, per-host
  `SetChipCoordinates`. Its `SessionMaster::BroadcastBarrier` is the
  *within-slice* rendezvous shared by all hosts in one slice.
- **MegaScaleTransport handles inter-slice bringup**: aggregating
  every slice's per-host network endpoints into one cluster-wide
  address table. This is the rendezvous documented in this section.
- **PJRT CoordinationService is a separate channel**: a different
  proto namespace (`xla.coordination`), different gRPC port,
  different abstraction (Python-level run identity vs C++-level
  chip identity). The two never share Barrier IDs.

The dependency graph is one-way: tpunetd must finish before
Megascale starts; Megascale must finish before steady-state
collectives or HeartBeat begin.

## The single round trip

The rendezvous is exactly **one gRPC round trip per worker**:

```
worker N (slice_id=S, host_id=H)
    │
    │   GetMultiSliceTopologyRequest {
    │     slice_id      = S
    │     host_id       = H
    │     host_addresses = [HostNetworkAddress { ... }]
    │     topology_args  = TpuTopologyArgsProto (from tpunetd)
    │     incarnation_id = NewGlobalID()
    │   }
    ▼
gRPC /xla.megascale.runtime.MegaScaleTransport/GetMultiSliceTopology
    │
    ▼
coordinator process:
  CommunicationBackend::OnTopologyRequestReceived(req, reply_cb)
      ├─ TracedMutexLock backend.mu (kind=6)
      ├─ topology_coordinator_ = backend->+0x1a0
      ├─ if null → "TopologyCoordinator not initialized." error
      └─ topology_coordinator_->AddRequest(req, reply_cb)
                │
                ├─ stash reply_cb into pending vector
                ├─ ProcessRequest(req)
                │     ├─ verify topology_args matches first seen
                │     │   (MessageDifferencer::Compare)
                │     ├─ flat_hash_map<int, SliceState>[slice_id]
                │     │   .num_hosts_seen++
                │     └─ append host_addresses to slice's host list
                ├─ if IsComplete():
                │     ├─ response = CreateResponse()
                │     │     - assemble MultiSliceTopologyInfo
                │     │     - sort SliceInfo* and
                │     │       NetworkAddressMapping*
                │     │       (byte-stable)
                │     ├─ LogUniqueIds(slice_id, host_id, response)
                │     │     - re-key drift warnings
                │     ├─ state_ = 2 (completed)
                │     ├─ Notification.Notify()
                │     └─ for every pending callback: cb(response)
                └─ else if state_ == 0 → ScheduleStatusReport()
worker N receives the response, installs endpoint addresses
into backend.+0x170..+0x1e0 and the runtime returns from
CommunicationBackend::DiscoverTopologyAndAddressBindings.
```

The same template-based `Coordinator<Req, Resp, Callback>`
machinery drives the `BarrierCoordinator` flow: one
`BarrierCoordinator` per `barrier_id`, indexed by a
`flat_hash_map<string, unique_ptr<BarrierCoordinator>>` at offset
`+0x228` of `CommunicationBackend`.

## How this section is organised

- [Coordinator Election](coordinator-election.md) — env-var-driven
  selection of the single coordinator process.
- [Worker Registration](worker-registration.md) — the
  `GetMultiSliceTopologyRequest` schema and the gRPC server-side
  callback chain.
- [Topology Exchange](topology-exchange.md) — internals of
  `TopologyCoordinator`: SliceState map, `ProcessRequest`,
  `CreateResponse`, the `MessageDifferencer` validation, and the
  byte-stable sort used by the response.
- [Convergence](convergence.md) — the generic `Coordinator<>`
  template's state machine, pending-callback vector,
  `absl::Notification`, and `ReportStatus` periodic logger.
- [Failure Handling](failure-handling.md) — timeouts, partial
  registrations, re-key warnings, and the path from a failed
  bootstrap to `ErrorReporter` and `RapidEye`.
- [tpunetd Relationship](tpunetd-relationship.md) — why tpunetd must
  complete first; how its `TpuTopologyArgsProto` and chip-coordinate
  state feed Megascale.
- [ICI Handoff](ici-handoff.md) — the explicit data-dependency from
  tpunetd's `SetChipCoordinates`/`SetGlobalChipId`/`SetRoutingTable`
  results into the `HostNetworkAddress` fields of the rendezvous
  request.
