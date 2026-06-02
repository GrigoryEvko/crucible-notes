# ICI Handoff

ICI (Inter-Chip Interconnect) is the dedicated copper / optical
fabric that connects TPU chips within a pod. ICI bringup is
tpunetd's responsibility; Megascale's bootstrap rendezvous
consumes the **result** of that bringup but never touches ICI
itself. This page documents the explicit data-flow handoff and
what each side reads / writes.

## Three pieces of state that flow tpunetd → Megascale

1. **Chip coordinates.** Each chip in the pod is assigned a
   coordinate tuple (x, y, z, …) that the ICI fabric uses for
   routing. Coordinates are set by tpunetd's
   `SessionControl/SetChipCoordinates` (or
   `SetGlobalChipId` for unique pod-wide identification).
   Workers read their assigned coordinates via
   `SessionControl/GetChipCoordinates` and incorporate them into
   the `TpuTopologyArgsProto` that goes into the Megascale
   `GetMultiSliceTopologyRequest.topology_args` field.
2. **Routing tables.** tpunetd's `SetRoutingTable` writes per-link
   next-hop tables for every chip; the daemon programs them into
   the ICI link hardware. Megascale's bootstrap does **not**
   transmit routing tables — they are entirely a within-slice
   concern. Megascale only cares that ICI is up so the resulting
   `TpuTopologyArgsProto` is consistent across hosts of the same
   slice.
3. **Host network bindings.** The `HostNetworkAddress` entries in
   the Megascale request carry the host's `MEGASCALE_PORT` (or
   `MEGASCALE_PORT_NAME` resolved over the host's DCN interface).
   These are NOT ICI; they are the DCN-side bindings used for
   cross-slice traffic over normal TCP/gRPC. The ICI fabric
   serves the in-slice chip-to-chip traffic; DCN serves the
   slice-to-slice host-to-host traffic.

## Sequencing within a single host bringup

```
host bringup
  │
  ▼
tpunetd_client::TpunetdClient::Init
  │  ConnectToTpunetd over /var/google/services/tpunetd/user.socket
  │
  ▼
tpunetd_client::TpunetdControl::StartSession
  │  passes per-host DriverInterface ChipLocation vector
  │
  ▼
tpunetd daemon:
  │  CreateNetwork (allocate NetworkContextId for this slice)
  │  ConfigureIci (program per-chip ICI link config)
  │  EnableIciDataLink (turn on the high-speed links)
  │  WaitForDataLinkUp (block until every link reaches L1 sync)
  │  SetChipCoordinates / SetGlobalChipId (per-chip assignment)
  │  SetRoutingTable (per-chip per-link next-hop tables)
  │  SetGtcConfiguration / WaitForGtcReset (global time counter sync)
  │
  ▼
tpunetd_client::SessionMaster::BroadcastBarrier
  │  Notify + WaitForReady across all hosts of this slice
  │  Ensures every host completed StartSession before any
  │  host's runtime continues
  │
  ▼
Local runtime: build TpuTopologyArgsProto from
  - chip coordinates (GetChipCoordinates result)
  - ICI dimensions (TPU_PROCESS_BOUNDS, TPU_HOST_BOUNDS)
  - per-host stride (TPU_CHIPS_PER_HOST_BOUNDS)
  - twist factors (TPU_TOPOLOGY_ALT, TPU_TOPOLOGY_WRAP)
  │
  ▼
xla::megascale::runtime::CommunicationBackend::DiscoverTopologyAnd
  AddressBindings(slice_id, *that TpuTopologyArgsProto*, host_id,
                  num_slices)
```

Only at the bottom of this sequence does Megascale's bootstrap
run. By then ICI has been brought up, the chip coordinates are
final, and `TpuTopologyArgsProto` faithfully describes the local
slice.

## How the coordinator uses the handoff

`TopologyCoordinator::ProcessRequest` (`0x1cf524c0`) compares
every inbound `topology_args` against the first one it saw for
the same `slice_id`:

```
MessageDifferencer diff;
diff.set_message_field_comparison(EQUIVALENT);
if (!diff.Compare(req.topology_args,
                  *cached_topology_args_for_slice[slice_id])) {
  LOG(ERROR) << "Received topology that differs from previously
                 registered topology at same sliceID. ...";
}
```

Where the comparison matters:

- **Chip count per slice.** Two hosts of the same slice reporting
  different `TpuDimensionsProto` (rows × cols × depth) indicates
  tpunetd assigned different chip coordinates on those hosts —
  typically a sign that tpunetd's `WaitForDataLinkUp` timed out
  and the daemon proceeded with a partial slice.
- **Host bounds.** If two hosts disagree on per-host chip count,
  the runtime cannot correctly schedule collectives that
  partition across hosts. The drift warning at `0x9b27486` fires.
- **Twist factors / topology mode.** When
  `MEGASCALE_TOPOLOGY` or `FLAGS_megascale_force_use_dcn_topology_
  from_flags` injects a synthesised topology, it must match the
  one tpunetd actually programmed.

## Why ICI is silent here

The Megascale rendezvous **never** transports ICI link state.
The `MultiSliceTopologyInfo` response carries:
- `SliceInfo.topology_args` (chip layout) — agreed upon, not the
  link state itself.
- `SliceInfo.host_addresses` (DCN endpoints).
- `NetworkAddressMapping.addresses` (one per `(slice_id, host_id)`
  pair, DCN endpoints).

No ICI link tables, no routing tables, no link health bits travel
through Megascale. ICI's health is monitored by tpunetd's
`SessionMaster::CheckSessionHeartbeat`, which detects link
failures and transitions the SessionState to `kFailing` (P-3-11).
Such failures propagate up to PJRT through the tpunetd path, NOT
the Megascale path.

The architectural separation means:

- A single bad ICI link makes the **slice** fail; tpunetd detects
  it and surfaces it as a session failure.
- A bad DCN link between two slices makes the
  **multi-slice job** fail; Megascale's HeartBeat detects it and
  surfaces it as a heartbeat failure.

The two detection systems run independently — both can fire,
neither blocks the other.

## What about `TpuTopologyArgsProto`?

This proto is the only structured artefact that crosses the
tpunetd → Megascale boundary. Its fields (from
`platforms/deepsea/software/superpod/routing/common/proto/`):

- `TpuDimensionsProto dimensions` — chip count per ICI axis,
  effectively reflects tpunetd's `CreateNetwork` topology
  assignment.
- `host_count` / `host_bounds` — used to derive
  `expected_num_hosts` in `SliceState`.
- `process_bounds` — process-level partitioning of a host.
- `twist`, `wrap`, `alt` — twisted-torus parameters that the
  compiler uses to route collective routes correctly.

When `--megascale_force_use_dcn_topology_from_flags=true` is set,
the runtime constructs `TpuTopologyArgsProto` entirely from the
`MEGASCALE_TOPOLOGY` env-var override, bypassing tpunetd's
contribution. This is a testing / simulation path; production
deployments leave it off and let tpunetd drive the proto.

## When the handoff goes wrong

Three concrete scenarios:

1. **tpunetd partially failed `WaitForDataLinkUp`.** The daemon
   continued with a degraded `TpuTopologyArgsProto` reflecting
   the smaller working chip set. The Megascale coordinator
   sees consistent (but wrong) topology across all hosts of the
   slice. Bootstrap succeeds but downstream compiles will fail to
   schedule the missing chips.
2. **Two hosts of the same slice raced and got different chip
   coordinates.** Megascale's `MessageDifferencer` catches the
   mismatch and emits the `Received topology that differs...`
   warning. Bootstrap continues serving the first-seen topology
   (so the slice runs with the wrong chip set on half its hosts);
   collectives that hit the misaligned chips will fail at compile
   time with shape errors.
3. **A host restarted after the slice was already in steady
   state and reconnected with a different `incarnation_id`.** The
   `LogUniqueIds` slot-2 warning at `0x9c14456` fires. tpunetd's
   `SessionMaster` independently detects the restart via
   heartbeat and may transition the session to `kFailing`. In
   either case the operator sees both signals and can correlate.

The combined diagnostic surface is therefore: tpunetd answers
"is ICI healthy on this host?", Megascale answers "do all hosts
of all slices agree on the chip layout?", and the
ErrorReporter / RapidEye chain (P-3-45) answers "what was the
worst-case combination of failures across the fleet?".
