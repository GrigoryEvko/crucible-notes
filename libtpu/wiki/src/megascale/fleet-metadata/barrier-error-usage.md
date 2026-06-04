# How the Barrier and Error Aggregator Use Fleet Metadata

Two cross-host subsystems consume the fleet metadata, and both read
only the *flat DCN layer*: the total host count and the
`(slice_id, host_id)` host identity. Neither touches chip coordinates,
slice shapes, or the DCN reduction tree.

## The cross-host barrier

The Megascale cross-host barrier is a centralised counter keyed by host
identity:

- **Default participant count** = `MultiSliceTopologyAndLocation::NumHosts()`
  — the total number of hosts across all slices. An un-parameterised
  barrier (the collective-rendezvous flavour) completes only when every
  host in the fleet has arrived.
- **Arrival key** = `(slice_id, host_id)` tuple. The coordinator
  (`BarrierCoordinator::ProcessRequest @0x1cf54e60`) stores arrivals in a
  `flat_hash_set<tuple<int,int>>` at `BarrierCoordinator+0xD8`; a
  duplicate arrival (e.g. a retry) is rejected so it cannot
  double-count. Separately, the caller side
  (`Communicator::Barrier @0x1cca8ee0`) keeps a `flat_hash_set<string>`
  of already-used barrier IDs and rejects a reused ID with
  "Barrier ID: $0 has already been used."
- **Nothing else.** The barrier never reads slice shape, chip
  coordinates, or `DCNTopology`. It needs exactly two facts: how many
  hosts to wait for (`NumHosts`) and how to identify each one
  (`(slice_id, host_id)`).

`NumHosts()` is computed by the `MultiSliceTopologyAndLocation` class
from the assembled fleet view: it sums `product(host_bounds)` over every
slice's [`TpuTopologyArgsProto`](slice-shape.md). So the barrier's
participant count is ultimately derived from the per-slice shapes
gathered at bootstrap.

## The error aggregator

The [error aggregator](../bootstrap/failure-handling.md) fans per-host
error reports into one digest, again keyed by host identity:

- **Early-fire condition** = `MegascaleErrorAggregator::size() ==`
  *expected-worker-count*, checked in
  `ErrorReporter::ReportError @0x1ccb6ea0`. The expected count is read
  through a virtual accessor on the `ErrorReporter` (constructed with a
  `TopologyCoordinator const*`), and equals the fleet-wide host count
  (`TopologyCoordinator::GetNumHosts() @0x1cf51de0` /
  `MultiSliceTopologyAndLocation::NumHosts() @0x20802c20`). When the
  count is reached the reporter calls `ErrorReporter::ProcessErrorDigest`
  immediately; otherwise it falls back to the 300 ms timeout armed at the
  same site (`Duration += 300`). There is no `TopologyCoordinator::NumWorkers()`
  method in the binary.
- **Per-error worker key** = `Substitute("slice$0-task$1", slice, task)`
  (built in `ReportError`, from the in-memory `MegaScaleRuntimeError`'s
  slice and task indices). The dedup `linked_hash_map<string, ...>` key
  (`AddError @0x1ccba940`) appends a `/` separator and a trailing integer,
  so the full key is `"slice<slice>-task<task>/<n>"`.
- **Culprit naming.** Hosts blamed in the digest are named by the
  `RapidEyeErrorDigestProto.WorkerAndCoreInfo` message (emitted from the
  in-memory `MegascaleErrorAggregator::WorkerAndCoreInfo` struct via
  `ToWorkerAndCoreInfoProto @0x1ccc4f80`):

  ```
  message WorkerAndCoreInfo {            // RapidEyeErrorDigestProto.WorkerAndCoreInfo
    string   worker_id = 1;              // "slice<slice>-task<task>"
    string   host_name = 2;
    CoreInfo core_info = 3;
    message CoreInfo {
      int32  chip_id           = 1;
      int32  core_idx          = 2;
      string physical_location = 3;
    }
  }
  ```

This `CoreInfo {chip_id, core_idx, physical_location}` triple is the
**only** place in the cross-host layer where a chip is named physically
— and it is diagnostic output, not part of the bootstrap inventory.

- **Fault links** are host-granular:

  ```
  message FaultyNetworkLink {           // RapidEyeErrorDigestProto.FaultyNetworkLink
    WorkerInfo src_worker = 1;          // RapidEyeErrorDigestProto.WorkerInfo
    WorkerInfo dst_worker = 2;
  }
  ```

  Each endpoint is a host (`WorkerInfo` = `{worker_id, host_name,
  cloud_instance_id}`), mapping straight back to a
  `NetworkAddressMapping` entry. There is no ICI-level link in the
  cross-host fault model.

## The shared dependency

Both consumers depend on the same two derived facts:

| Fact | Source | Used by |
|------|--------|---------|
| `NumHosts()` | sum of `product(host_bounds)` over all slices | barrier participant count, aggregator early-fire |
| `(slice_id, host_id)` | `NetworkAddressMapping` | barrier arrival key (`flat_hash_set<tuple<int,int>>`), aggregator worker key (`"slice<slice>-task<task>"`) |

Everything richer — slice shape, chip coordinates, `DCNTopology` — is the
collectives and compiler layer's concern. The barrier and error
aggregator treat the fleet as a flat set of `NumHosts` hosts, each named
`(slice_id, host_id)`.

## Why this matters for forensics

Because the cross-host layer is host-granular, a digest that classifies
a hang as `BAD_TPU_CHIP` or `BAD_SC_CHIP` can only point at *hosts*
(via `WorkerAndCoreInfo`). To get chip-level state for those hosts, the
operator follows up with tpunetd's `GetCoreDump(CORE_DUMP_CHIP_DUMP)` —
crossing back into the ICI layer. The fleet metadata's two-layer split
is exactly why this handoff is needed: the cross-host digest knows
*which host*, and tpunetd knows *which chip*. See
[ICI vs DCN](ici-vs-dcn.md) and the
[tpunetd relationship](../bootstrap/tpunetd-relationship.md).
