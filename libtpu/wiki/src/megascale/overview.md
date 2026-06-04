# Megascale — Section Map

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`; `.text` VMA == file offset). All symbols below are present in the full-symbol binary; demangled names and addresses are cross-checked against the IDA decompile.

## Abstract

**Megascale** is the data-center-network (DCN) scale-out layer of the TPU runtime. Where [ICI](../ici/overview.md) is the in-pod optical torus that wires the chips of one *slice* into a single SerDes-connected `(X,Y,Z)` mesh, Megascale federates many such slices — each its own ICI island — into one logical TPU job that spans racks and rows of a data center, communicating over Ethernet via gRPC. ICI moves operands chip-to-chip at link speed inside a slice; Megascale moves the cross-slice fraction of a collective (the `AllReduceDcnFusionData` / `LowerCollectivePermuteFullDCN` paths the compiler emits in `xla::megascale::compiler::CrossSliceRewrites`) host-to-host between slices, and it owns the control plane that lets those hosts find one another at all. The split is visible in the binary's own vocabulary: the cross-slice rewriter names its primitives `ICIDCN…` and `…FullDCN`, treating "ICI" and "DCN" as the two transport tiers of one collective.

Everything in this section lives under the C++ namespace `xla::megascale::runtime` and is hosted by one facade object, the `CommunicationBackend` (object size `0x370`, built by `Create()`). Each TPU host runs one backend; the backend owns a gRPC server — the 6-method `MegaScaleTransport` service — bound to `MEGASCALE_PORT`, plus, on exactly one elected process per job, a `TopologyCoordinator`, a map of `BarrierCoordinator`s, and an `ErrorReporter`. Beneath the backend, the host-local **`tpunetd`** daemon (namespace `superpod::tpunetd_client`) brings up the ICI fabric *within* the slice before Megascale's cross-slice rendezvous ever runs; the two are sequenced one-way, tpunetd → Megascale.

This page is a map. It orients the reader on the ICI↔DCN boundary, the host-side daemon model, and the four-phase job lifecycle, then hands off to the already-written subsections for every byte-level derivation. It does not duplicate their internals.

For the section as a whole, the reimplementation contract is:

- **The two transport tiers and where they cleave** — ICI (in-slice torus, owned by tpunetd) vs DCN (cross-slice Ethernet/gRPC, owned by Megascale), and the single datum (`TpuTopologyArgsProto`) that crosses from one to the other.
- **The host-side daemon model** — one `CommunicationBackend` per host, one elected coordinator per job, the `MegaScaleTransport` gRPC surface, and the host-local `tpunetd` beneath it.
- **The job lifecycle** — rendezvous → fleet-metadata distribution → cross-host barrier → steady-state heartbeat, with error aggregation as the failure spine running orthogonal to all four.

| | |
|---|---|
| **C++ namespace** | `xla::megascale::runtime` |
| **Per-host facade** | `CommunicationBackend` (`0x370` bytes; `Create()` @ `0x1ccafe60`) |
| **Bootstrap entry** | `CommunicationBackend::DiscoverTopologyAndAddressBindings(int, tpu::TpuTopologyArgsProto, int, int)` @ `0x1ccacb80` |
| **gRPC service** | `MegaScaleTransport` — 6 unary RPCs, prefix `/xla.megascale.runtime.MegaScaleTransport/` |
| **Coordinator (1/job)** | `TopologyCoordinator` (`0x108` bytes) + `BarrierCoordinator` map + `ErrorReporter` |
| **Below (in-slice)** | `tpunetd` daemon via `superpod::tpunetd_client` (ICI fabric, UDS) |
| **Election knob** | `MEGASCALE_COORDINATOR_ADDRESS` / `--megascale_coordinator_address` |
| **Source root** | `platforms/xla/megascale/runtime/communication/` |

---

## The ICI↔DCN Boundary

This is the central distinction the section turns on, and the binary draws it sharply.

### Two coordinate systems, joined at one point

A TPU job is a hierarchy of two networks stacked on each other:

```text
   ┌──────────────────────────────────────────────────────────────┐
   │  DCN tier — Megascale (xla.megascale.runtime)                  │
   │  Ethernet + gRPC between hosts of different slices.            │
   │  Names a chip by (slice_id, host_id) + per-slice device id.    │
   │  Owns: CommunicationBackend, MegaScaleTransport, coordinators. │
   └──────────────────────────────┬─────────────────────────────────┘
                                  │  TpuTopologyArgsProto (the only
                                  │  datum that crosses the boundary)
   ┌──────────────────────────────┴─────────────────────────────────┐
   │  ICI tier — one optical torus per slice (superpod.routing.*)    │
   │  SerDes links between chips of ONE slice.                       │
   │  Names a chip by ChipCoordinate (X,Y,Z) in a ToroidalTopology.  │
   │  Owns: tpunetd link bring-up, routing tables, GTC sync.         │
   └──────────────────────────────────────────────────────────────────┘
```

The two layers keep their addressing schemes deliberately apart. The ICI layer addresses a chip by a Cartesian `ChipCoordinate` produced by [topology discovery](../ici/topology-discovery.md)'s square-seed polarity inference; the DCN layer addresses a *host* by `(slice_id, host_id)` and never re-derives chip coordinates. The single datum that crosses from ICI up to DCN is the **per-slice shape**, serialized as a `tpu::TpuTopologyArgsProto` and carried verbatim inside every `GetMultiSliceTopologyRequest` (field 4) and every `SliceInfo`. The DCN layer treats that shape as an opaque, validated blob — the coordinator only ever runs `proto2::util::MessageDifferencer::Compare` on it to confirm all slices agree, never to decode chip geometry. (Validation calls are in `TopologyCoordinator::ProcessRequest` @ `0x1cf524c0`.)

> **NOTE —** the decompile makes the two-tier model concrete at the *collective* level too. The compiler pass `CrossSliceRewrites::LowerAllReduce` (@ `0x111d6f40`) takes an `optional<AllReduceDcnFusionData>` and `ConstructAllGatherICIDCNextReceiverConstant` (@ `0x14b96340`) names both tiers in one symbol: an all-reduce that spans slices is fused into an *in-slice ICI phase* and a *cross-slice DCN phase*. Megascale owns the DCN phase; ICI owns the rest.

### Why centralize the DCN control plane

ICI discovery is decentralized — every chip in a slice infers its own coordinates by BFS over its neighbors' link signatures (see [Topology Discovery](../ici/topology-discovery.md)). DCN rendezvous is the opposite: **strictly centralized**, one coordinator process per job. The reason is reachability. ICI links are physically cabled and self-describing; a chip can discover its torus neighbors with no out-of-band channel. DCN peers are Ethernet hosts scattered across a data center with no a-priori knowledge of one another's addresses. Megascale solves this the way a classic parameter server does: one process binds a well-known endpoint (`MEGASCALE_COORDINATOR_ADDRESS`), every other process connects to it, and the coordinator assembles and broadcasts the single cluster-wide address table. There is no peer discovery and no vote — election is purely "whose `MEGASCALE_COORDINATOR_ADDRESS` resolves to a local interface."

---

## The Host-Side Daemon Model

Every TPU host in the job runs the same two software layers; only the coordinator role differs.

### CommunicationBackend — one per host

The `CommunicationBackend` (`0x370` bytes, allocated by `Create()` @ `0x1ccafe60` via `operator new(880, 16)`) is the per-process facade for all DCN activity. It owns:

- the `MegaScaleTransport` gRPC server, bound to `MEGASCALE_PORT` and wired through a `GrpcTransport` (default) by `InitializeTransportLayerInternal()` @ `0x1ccaeb40`;
- the per-(slice,host) **address table** (member at `+0x170`, below the two pointer slots at `+0x1a0`/`+0x1a8`) that steady-state `Send` RPCs index;
- a `TopologyCoordinator*` at `+0x1a0` — **null on every process except the coordinator**;
- a `flat_hash_map<string, unique_ptr<BarrierCoordinator>>` at `+0x1b0`, keyed by `barrier_id` and guarded by the `TracedMutex` at `+0xe0`;
- an `ErrorReporter*` at `+0x1a8`;
- the `HeartBeat` scheduler started by `StartHeartBeat()` @ `0x1ccade60`.

The asymmetry is the whole design: a worker's `+0x1a0` slot stays null, so when a stray Topology RPC reaches it, `OnTopologyRequestReceived` @ `0x1ccac380` rejects it with `"TopologyCoordinator not initialized."` (INTERNAL, category 13). Only the elected process ever runs `InitializeCoordinator(num_slices)` @ `0x1ccad600`, which `new`s the `TopologyCoordinator` and constructs the per-process `ErrorReporter`.

### MegaScaleTransport — the 6-method gRPC surface

Every cross-host control message — bootstrap and steady-state — rides one unary gRPC service. The decompile confirms the server-side nesting through the `WithCallbackMethod_*` template chain (Send → GetMultiSliceTopology → Barrier → ReportError → TriggerError → … visible in symbols at `0x1ce80200`+):

| RPC | Request → Response | Phase | Owner page |
|---|---|---|---|
| `GetMultiSliceTopology` | `GetMultiSliceTopologyRequest` → `…Response` | bootstrap | [Bootstrap](bootstrap/overview.md) |
| `Barrier` | `BarrierRequest` → `BarrierResponse` | barrier | [Cross-Host Barrier](cross-host-barrier.md) |
| `ReportError` | `ReportErrorRequest` → `…Response` | failure | [Error Aggregator](error-aggregator.md) |
| `TriggerError` | `TriggerErrorRequest` → `…Response` | failure | [Error Aggregator](error-aggregator.md) |
| `Send` | `SendRequest` → `TransferBufferResponse` | steady-state | — (DCN collective transfer) |
| `SendHeartBeat` | `HeartBeatRequest` → `HeartBeatResponse` | steady-state | — (liveness) |

The wire protocol for this service — message taxonomy, transport selection (`MEGASCALE_TRANSPORT_TYPE` = `grpc` default vs `chaotic_good_legacy`), authentication, and the `GrpcTransport` server wiring — is owned by [tpunetd Protocol](tpunetd-protocol.md) and [Bootstrap › Worker Registration](bootstrap/worker-registration.md).

### tpunetd — the in-slice daemon beneath

`tpunetd` is a host-local daemon, not part of `libtpu.so` itself; `libtpu` talks to it through `superpod::tpunetd_client` over a Unix domain socket. It owns *everything inside one slice*: ICI link configuration, routing-table generation, global-time-counter (GTC) sync, per-host `SetChipCoordinates`, and the in-slice `SessionMaster::BroadcastBarrier` rendezvous. Megascale runs strictly *above* it and consumes its output: the `HostNetworkAddress` and `TpuTopologyArgsProto` that every worker puts in its `GetMultiSliceTopologyRequest` are derived from tpunetd's chip-coordinate state, so tpunetd must finish before `DiscoverTopologyAndAddressBindings` can run. The dependency is one-way — tpunetd never blocks on `MegaScaleTransport`, and when `enable_megascale_topology=false` (single-slice mode), the `MegaScaleTransport` server is never started but tpunetd still runs. The full handoff is in [Bootstrap › ICI Handoff](bootstrap/ici-handoff.md) and [Bootstrap › tpunetd Relationship](bootstrap/tpunetd-relationship.md).

> **GOTCHA —** the PJRT distributed `CoordinationService` (package `xla.coordination`) is a *separate* control plane co-resident in `libtpu`, not part of Megascale. It coordinates Python-level state (shard assignment, run id) for JAX/TF; `MegaScaleTransport` coordinates TPU-internal chip-fabric state. They use different proto namespaces, different ports, and — critically — **never share barrier IDs**. A reimplementer who conflates the two will deadlock waiting on the wrong rendezvous. `xla.coordination.BarrierRequest` and `xla.megascale.runtime.BarrierRequest` are distinct protobuf types.

---

## The Job Lifecycle

A full Megascale job start is four ordered phases, with error aggregation as a fifth, orthogonal spine that any phase can fall into.

```text
  [tpunetd: ICI fabric bring-up within each slice — see ../ici]
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 1. RENDEZVOUS   DiscoverTopologyAndAddressBindings @0x1ccacb80 │
  │    one gRPC round trip/worker: GetMultiSliceTopology →        │
  │    coordinator accumulates num_slices·num_hosts regs →        │
  │    broadcasts one MultiSliceTopologyAndLocation               │
  └─────────────────────────────────┬───────────────────────────┘
                                    ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 2. FLEET METADATA   every process holds the same address      │
  │    table: (slice_id,host_id)→endpoints, per-slice shapes      │
  └─────────────────────────────────┬───────────────────────────┘
                                    ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 3. BARRIER   xla_tpu_enable_megascale_barrier: every host     │
  │    calls Barrier(id) → BarrierCoordinator releases at quorum  │
  └─────────────────────────────────┬───────────────────────────┘
                                    ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 4. STEADY STATE   Send (DCN collectives) + SendHeartBeat      │
  └─────────────────────────────────────────────────────────────┘

  ════ error spine (any phase) ════
  ReportError → ErrorReporter @0x1ccb6ea0 → MegascaleErrorAggregator
     → RapidEyeErrorDigestProto + LogErrorDigest (cause classifier)
```

### Phase 1 — Bootstrap rendezvous

The runtime calls `DiscoverTopologyAndAddressBindings` once per worker right after tpunetd brings up the slice. Each worker sends exactly one `GetMultiSliceTopology` RPC carrying its `(slice_id, host_id, host_addresses, topology_args, incarnation_id)` and blocks. The coordinator's `TopologyCoordinator` (a specialization of the generic `Coordinator<Req,Resp,Callback>` template) accumulates registrations into a `flat_hash_map<int, SliceState>`, and when `IsComplete()` reports `num_slices · num_hosts_per_slice` matching entries it builds one `GetMultiSliceTopologyResponse`, fans it out to every pending callback, and notifies. There is no retry loop — a single per-RPC deadline (`--megascale_topology_discovery_timeout`) bounds the whole rendezvous. Internals — election, the request schema, the coordinator state machine, the byte-stable response sort, and the failure paths — are owned by the **[Bootstrap section](bootstrap/overview.md)** (8 pages).

### Phase 2 — Fleet metadata

The response deserializes into the live `xla::megascale::runtime::MultiSliceTopologyAndLocation` C++ class (note: no `Proto` suffix; the wire form is `MultiSliceTopologyAndLocationProto`). This is the authoritative fleet model every downstream consumer holds by `const&` — the `Communicator`, the jellyfish scheduler (`ScheduleSendRecvs` @ `0x1d6b6520` takes a `const MultiSliceTopologyAndLocation*`), and the barrier. It answers what is in the fleet, who am I, how do I reach a peer, and how a chip is named fleet-wide. The schema — topology model, host identity (`incarnation_id`), global addressing, the slice shape, and the message-by-message wire decode — is owned by the **[Fleet Metadata section](fleet-metadata/overview.md)** (9 pages).

### Phase 3 — Cross-host barrier

Gated by `--xla_tpu_enable_megascale_barrier`, this is the pre-execution barrier. It reuses the same `Coordinator<>` template: one `BarrierCoordinator` per `barrier_id` (lazily inserted into the backend's `+0x1b0` map by `OnBarrierRequestReceived` @ `0x1ccac5c0`), releasing all waiters when the seen-host set reaches `num_workers`. The per-call deadline is the gRPC client deadline (`FLAGS_tf_tpu_preexecution_barrier_timeout`, default 30 s) — `BarrierRequest` field 4 is `num_participants`, not a timeout. Owned by [Cross-Host Barrier](cross-host-barrier.md).

### Phase 4 — Steady state

Once the barrier clears, the address table feeds `StartTransfer` for DCN collective `Send` RPCs, and `StartHeartBeat()` @ `0x1ccade60` begins per-peer `SendHeartBeat` liveness pings (gated by `--megascale_use_heartbeat`). The coordinator alone logs `"This task is the coordinator. Starting heartbeat with peers."`; workers heartbeat the coordinator. A failed heartbeat can terminate or in-place-restart the process depending on `--megascale_*_restart_*` flags.

### The error spine

Orthogonal to all four phases: any host that detects a fault posts a `ReportError` RPC. On the coordinator, `ErrorReporter::ReportError` @ `0x1ccb6ea0` lazily allocates one `MegascaleErrorAggregator`, dedups reports by `(slice_id:host_id, task_id)`, and fires `ProcessAndShutdown()` either at a 300 ms idle deadline or immediately once `size() == NumWorkers()`. The aggregator runs a cross-host cause classifier (9-value `Cause` enum: `BAD_TPU_CHIP`, `NETWORKING_ISSUE`, `DATA_INPUT_STALL`, …) and emits one `RapidEyeErrorDigestProto`. Owned by [Error Aggregator](error-aggregator.md).

---

## Section Contents

| Subsystem | Page | Owns |
|---|---|---|
| Bootstrap rendezvous (8 pages) | [Bootstrap › Overview](bootstrap/overview.md) | coordinator election, worker registration, topology exchange, convergence, failure handling, tpunetd relationship, ICI handoff |
| Fleet metadata schema (9 pages) | [Fleet Metadata › Overview](fleet-metadata/overview.md) | topology model, host identity, global addressing, ICI-vs-DCN, slice shape, bootstrap exchange, barrier/error usage, field decode |
| tpunetd wire protocol | [tpunetd Protocol](tpunetd-protocol.md) | the `MegaScaleTransport` / tpunetd daemon wire surface |
| Cross-host barrier | [Cross-Host Barrier](cross-host-barrier.md) | `BarrierCoordinator`, the `Coordinator<>` template, quorum and timeout |
| Error aggregation | [Error Aggregator](error-aggregator.md) | `MegascaleErrorAggregator`, the RapidEye digest, cause classifier, retention |
| The tier below | [ICI › Overview](../ici/overview.md) | the in-slice optical torus that Megascale federates |

---

## Provenance

Every claim on this page is anchored to a demangled symbol or address present in the full-symbol `libtpu.so` and cross-checked against its disassembly and decompilation. The top-level entry points (`DiscoverTopologyAndAddressBindings` @ `0x1ccacb80`, `InitializeCoordinator` @ `0x1ccad600`, `TopologyCoordinator::ProcessRequest` @ `0x1cf524c0` / `CreateResponse` @ `0x1cf54460`, `BarrierCoordinator` ctor @ `0x1ccb3fa0`, `ErrorReporter` ctor @ `0x1ccb6d80` / `ReportError` @ `0x1ccb6ea0`), the `MegaScaleTransport` `WithCallbackMethod_*` server chain, and the ICI/DCN tier split (`CrossSliceRewrites::LowerAllReduce`, `ConstructAllGatherICIDCNextReceiverConstant`, `AllReduceDcnFusionData`) are all confirmed by name in the decompiled function set. Byte-level layouts, wire formats, and decision logic are derived in the linked subsections, not restated here.

## Cross-References

- [Bootstrap › Overview](bootstrap/overview.md) — the cross-host rendezvous that opens phase 1 of the lifecycle
- [Fleet Metadata › Overview](fleet-metadata/overview.md) — the fleet model the rendezvous broadcasts (phase 2)
- [Cross-Host Barrier](cross-host-barrier.md) — phase 3, the pre-execution barrier on the same `Coordinator<>` template
- [Error Aggregator](error-aggregator.md) — the failure spine: `ReportError` → `MegascaleErrorAggregator` → RapidEye digest
- [tpunetd Protocol](tpunetd-protocol.md) — the wire surface of the daemon model
- [ICI › Overview](../ici/overview.md) — the in-slice optical torus tier that Megascale federates over DCN
- [ICI › Topology Discovery](../ici/topology-discovery.md) — where the `TpuTopologyArgsProto` that crosses the ICI↔DCN boundary is produced
