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
- **Arrival key** = `(slice_id, host_id)` tuple. The coordinator stores
  arrivals in a `flat_hash_set<tuple<int,int>>`; a duplicate arrival
  (e.g. a retry) is rejected so it cannot double-count.
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

- **Early-fire condition** = `aggregator.size() ==
  TopologyCoordinator::NumWorkers()`. `NumWorkers()` equals
  `NumHosts()` — the same fleet-wide host count.
- **Per-error worker key** = `StrCat("$0:$1", slice_id, host_id)`. The
  dedup inner key appends the in-host `task_id`, so the full key is
  `"<slice_id>:<host_id>/<task_id>"`.
- **Culprit naming.** Hosts blamed in the digest are named by:

  ```
  message WorkerAndCoreInfo {
    string   worker_id = 1;   // "<slice_id>:<host_id>"
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
  message FaultyNetworkLink {
    WorkerInfo src_worker = 1;
    WorkerInfo dst_worker = 2;
  }
  ```

  Each endpoint is a host (`WorkerInfo`), mapping straight back to a
  `NetworkAddressMapping` entry. There is no ICI-level link in the
  cross-host fault model.

## The shared dependency

Both consumers depend on the same two derived facts:

| Fact | Source | Used by |
|------|--------|---------|
| `NumHosts()` | sum of `product(host_bounds)` over all slices | barrier participant count, aggregator early-fire |
| `(slice_id, host_id)` | `NetworkAddressMapping` | barrier arrival key, aggregator worker key |

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
