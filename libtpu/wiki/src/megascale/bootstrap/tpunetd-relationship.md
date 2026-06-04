# tpunetd Relationship

`tpunetd` is the per-host TPU control daemon. Its responsibilities
overlap with Megascale's in name only: tpunetd handles
**within-slice** chip and fabric setup, while Megascale's
`MegaScaleTransport` handles **across-slice** address-table
exchange. The two run sequentially and on different transports.

This page describes the data flow between them; for tpunetd's own
internals see [tpunetd Protocol](../tpunetd-protocol.md).

## Layering

```text
+----------------------------------------------------------------+
| XLA Megascale Runtime                                           |
| (xla::megascale::runtime::CommunicationBackend)                 |
|                                                                 |
|  DiscoverTopologyAndAddressBindings(slice_id, args,             |
|                                    host_id, num_slices)         |
|        │                                                        |
|        ▼                                                        |
|  GetMultiSliceTopology gRPC over MEGASCALE_PORT                 |
|  Coordinator (one process per job)                              |
+──────────────────────────────────────────────────────────────────+
        ▲ reads tpu_topology_args (TpuTopologyArgsProto) and the
        │ address_mapping (NetworkAddressMapping) derived from the
        │ chip coordinates that tpunetd published earlier
        │
+──────────────────────────────────────────────────────────────────+
| tpunetd_client (superpod::tpunetd_client::TpunetdClient)         |
|                                                                 |
|  TpunetdClient::Init                                            |
|    ConnectToTpunetd  →  Unix socket /var/google/services/...    |
|    SessionMaster::Create  →  spawns per-peer SessionWorker      |
|                              stubs over TCP within this slice   |
|                                                                 |
|  Issued RPCs (subset libtpu uses):                              |
|    StartSession / StopSession / StatSession                     |
|    CheckSessionHealth / GetChipCoordinates                      |
|    GrantSessionPermission / GetCoreDump                         |
+──────────────────────────────────────────────────────────────────+
        ▼ gRPC over Unix socket
+──────────────────────────────────────────────────────────────────+
| tpunetd daemon (out-of-process, owns the chips)                  |
|                                                                 |
|  ICI fabric setup: CreateNetwork, UpdateTopology,               |
|    ConfigureIci, EnableIciDataLink, WaitForDataLinkUp           |
|  Routing table install: SetRoutingTable                          |
|  Global time counter: SetGtcConfiguration, WaitForGtcReset       |
|  Chip coordinate assignment: SetChipCoordinates,                 |
|    SetGlobalChipId                                                |
+──────────────────────────────────────────────────────────────────+
```

## Sequence: full Megascale job bringup

The dependency runs strictly tpunetd → MegaScaleTransport. The
runtime enforces the order by sequencing the calls inside the
plugin's `do_init`:

1. **tpunetd_client connect.**
   `TpunetdClient::Create(topology, TpuType, Options)` followed by
   `TpunetdClient::Init(opts, retry_count)` opens the Unix socket
   to the local tpunetd daemon.
2. **Session bringup.** Either `SessionControl/StartSession`
   (when `--bypass_vbar_control_service=true`) or
   `VBARControl/StartSession` (default) registers this host's
   chips. The daemon assigns chip coordinates, programs ICI
   routing tables, configures the global time counter, brings up
   ICI data links, and signals back when the session is ready.
3. **In-slice rendezvous (`tpunetd_client::BroadcastBarrier`).**
   The `tpunetd_client::BroadcastBarrier` class
   (`BroadcastNotification` / `BroadcastWaitForReady`, gated by
   `SyncWithTimeout`) drives the
   `superpod.tpunetd_client.proto.TpuNetworkSessionBarrier/Notify`
   and `.../WaitForReady` RPCs across every host in the slice.
   This is a peer-to-peer barrier; tpunetd itself is not involved.
4. **TpuTopologyArgsProto extraction.** Once the in-slice
   rendezvous is complete, the runtime queries
   `SessionControl/GetChipCoordinates` to fetch the assigned
   coordinates, builds the per-slice
   `tpu::TpuTopologyArgsProto` (chip dimensions, host bounds,
   per-process bounds, twist factors), and stashes it.
5. **MegaScaleTransport bringup.** `CommunicationBackend::Create`
   constructs the backend; `transport_factory()` produces a
   `GrpcTransport`; `GrpcTransport::Init` binds the
   `MEGASCALE_PORT` server. The coordinator process additionally
   instantiates `TopologyCoordinator`.
6. **Cross-slice rendezvous.**
   `DiscoverTopologyAndAddressBindings(local_slice_id,
   args /* from step 4 */, local_host_id, num_slices)` runs.
   This is the entry point documented elsewhere in this section.
7. **Steady state.** After Megascale's rendezvous completes,
   `StartHeartBeat()` runs on `MegaScaleTransport`. The runtime
   becomes ready to handle PJRT executable launches; tpunetd's
   `SessionMaster::CheckSessionHeartbeat` runs in parallel on the
   tpunetd transport.

There is no path where Megascale starts before tpunetd. The
`enable_megascale_topology=false` flag bypasses step 5/6 entirely
(single-slice mode); tpunetd still runs. The reverse —
Megascale without tpunetd — does not happen in the binary.

## Data flow through the boundary

The Megascale request (`GetMultiSliceTopologyRequest`, whose proto
descriptor lives at file offset `0xbf81634`) carries the following
state, two pieces of which are derived from tpunetd:

| Field of `GetMultiSliceTopologyRequest` | Derived from tpunetd RPC |
|---|---|
| `tpu_topology_args` (field 2, `tpu.TpuTopologyArgsProto`) | `SessionControl/GetChipCoordinates` plus the local `TpuTopologyArgs` construction in the runtime (uses chip layout fields populated by `SetChipCoordinates`). |
| `address_mapping` (field 1, `xla.megascale.runtime.NetworkAddressMapping`) — carries `slice_id`, `host_id`, and repeated `HostNetworkAddress` entries (each a single `address` string plus `interface_name`, `numa_node`, `host_name_for_debugging`). | Not directly from tpunetd: the `address` value comes from local resolution governed by the `megascale_port` / `megascale_port_name` flags, but the binding depends on tpunetd having assigned the `slice_id` / `host_id` chip coordinates that determine which network the host uses. |
| `incarnation_id` (field 3, int) | Process-local; independent of tpunetd. |

The coordinator validates the `tpu_topology_args` field by running
`proto2::util::MessageDifferencer::Compare` over every inbound
request (see [Topology Exchange](topology-exchange.md)). The
implication: two hosts in the same slice that completed tpunetd
bringup with different chip coordinates will be detected at this
boundary — even though tpunetd's own `StartSession` would have
been consistent. This is one of the failure modes that the drift
warnings at rodata `0x9b27486` are designed to surface.

## What tpunetd does NOT do

- **No cross-slice address exchange.** tpunetd is per-host;
  its peer fanout (`SessionMaster::ExecuteOnAllWorkers`) is bounded
  to peers within the same slice. Multi-slice address tables are
  Megascale's domain.
- **No cross-slice barrier.** `BroadcastBarrier` is per-slice;
  multi-slice synchronization uses `MegaScaleTransport.Barrier`.
- **No PJRT integration.** tpunetd is invisible to the JAX/TF
  client; PJRT talks to libtpu, libtpu talks to tpunetd. The
  client never sees a `tpunetd.*` proto.
- **No support for non-Borg / non-Cloud paths.** The
  `kTpunetdSupportedTpuVersions` whitelist limits tpunetd to
  certain TPU generations; older single-host setups fall back to
  the in-process `SliceBuilder` family, which has its own per-host
  rendezvous (different from both tpunetd and Megascale).

## Operational consequences

- **Operator-visible startup log on a multi-slice TPU pod:**
  1. tpunetd_client side: `"Running in Cloud, using TpunetdClient"`
     or `"Creating tpunetdclient for worker ..."`.
  2. tpunetd_client SessionMaster: peer connection logs.
  3. Per-slice barrier completes (no specific log; the
     `SessionMaster` proceeds silently).
  4. CommunicationBackend ctor side: per-process FLAGS log line.
  5. On the coordinator only:
     `"Megascale Topology Coordinator started for <N>"`.
  6. Per-process: periodic
     `"MegaScale Topology Discovery in progress. Missing hosts..."`
     until completion.
  7. Once complete: `"MegaScale Topology Discovery completed."`
- **Operator-visible failure on a stuck slice:** the per-slice
  tpunetd `SessionMaster::CheckSessionHeartbeat` is the first to
  notice a stalled host; it emits
  `"Session is failing due to the following chips having zero as
  chip id"` (P-3-11). If the slice never finishes tpunetd bringup,
  Megascale's per-host requests never arrive, and the coordinator
  reports the missing slice in its periodic status log.

The two systems are therefore complementary diagnostics: tpunetd
sees per-chip failures; Megascale sees per-slice / per-host
absences.
