# Coordinator Election

Megascale does not run a vote. The coordinator is the process whose
`MEGASCALE_COORDINATOR_ADDRESS` resolves to a local interface, has
`MEGASCALE_PORT` set, and successfully binds the gRPC server. Every
other process treats that endpoint as a remote target and acts as a
worker. The decision is therefore entirely deterministic: it falls
out of the way the launcher (Borg, GKE, or a hand-rolled job script)
sets the environment variables on each node.

## The election inputs

The relevant CLI flags / env vars are read by
`CommunicationBackend::PopulateOptionsFromFlags()` and copied into
the per-process `Options` struct:

| Source | Field copied into Options | Effect |
|---|---|---|
| `FLAGS_megascale_coordinator_address` (`MEGASCALE_COORDINATOR_ADDRESS`) | `Options.coordinator_address` (string at `+0x00`) | The well-known endpoint of the coordinator. Every process resolves this address; the one whose local interface matches becomes coordinator. |
| `FLAGS_megascale_port` (`MEGASCALE_PORT`) | `Options.port` (int32 at `+0x30`) | Bind port for the local `MegaScaleTransport` gRPC server. Required — when unset libtpu logs `"megascale_port is not set."` and aborts. |
| `FLAGS_megascale_port_name` | `Options.port_name` (string at `+0x18`) | Alternative named port resolved by the host's port-allocation service. When set takes priority over numeric port. |
| `FLAGS_megascale_num_slices` (`MEGASCALE_NUM_SLICES`) | passed through `xla_megascale` → `InitializeCoordinator(num_slices)` | Total slice count of this job. The coordinator's `TopologyCoordinator` waits for exactly this many slice registrations. |
| `FLAGS_megascale_slice_id` (`MEGASCALE_SLICE_ID`) | `Options.slice_id` (int32 at `+0x58`) | This process's slice id. Workers put this into every request; coordinator uses it as the key in `flat_hash_map<int, SliceState>`. |
| `FLAGS_megascale_topology_discovery_timeout` | `Options.topology_discovery_timeout` (absl::Duration at `+0x34..+0x40`) | Per-RPC deadline applied to `GetMultiSliceTopology`. Acts as the upper bound on total rendezvous time. |
| `FLAGS_tf_tpu_preexecution_barrier_timeout` | `Options.barrier_timeout` (absl::Duration at `+0x44..+0x50`) | Per-RPC deadline applied to subsequent `Barrier` calls. |
| `FLAGS_megascale_transport_type` (`MEGASCALE_TRANSPORT_TYPE`) | `Options.transport_type` | `"grpc"` (default) selects `GrpcTransport`; `"chaotic_good_legacy"` selects the experimental Chaotic-Good transport. |
| `FLAGS_megascale_transport_numa_node` | `Options.transport_numa_node` | Optional NUMA affinity hint for the transport thread. |
| `FLAGS_megascale_authentication` (`MEGASCALE_AUTHENTICATION`) | `Options.auth_method` | Selects gRPC `ServerCredentials`. Insecure by default for non-Google internal builds. |

The runtime emits a single instrumentation log line at startup that
echoes the election inputs:

```
megascale_coordinator_address:<addr> megascale_port: <port>
  megascale_port_name: <port_name>
  topology_discovery_timeout: <duration>
  barrier_timeout: <duration>
```

(string template at rodata 0x9b27325). Combined with `Instrumentations:
megascale_num_slices: <N>` and `Instrumentations: megascale_slice_id:
<S>` (0xa23ae2a, 0xa282bf7), these lines uniquely identify the role
each process is playing.

## Election sequence

```
process startup
  │
  ▼
xla_megascale runtime construction
  │
  ▼
CommunicationBackend::Create(options, alloc_cb, free_cb,
                              transport_factory, ...)
  │
  ▼
new CommunicationBackend(options, alloc_cb, free_cb)
  │  TracedMutex (kind=6) at +0xe0
  │  RandenPool seed_seq
  │
  ▼
transport_factory(callbacks)  // pluggable; default: GrpcTransport
  │
  ▼
GrpcTransport::Init({port = Options.port, ...})
  │  attempts to bind Options.port
  │  on success: registers WithCallbackMethod_* handlers
  │  on failure: returns NOT_OK; CommunicationBackend stores it at +0x148
  │
  ▼
runtime calls DiscoverTopologyAndAddressBindings(
    local_slice_id, topology_args, local_host_id, num_slices)
  │
  ▼
InitializeCoordinator(num_slices)
  │  if (this->coordinator_address_ != local_address)
  │      return InternalError(line 0x5c8);   // worker path — not the coordinator
  │  TopologyCoordinator* tc = new TopologyCoordinator(num_slices);
  │  this->+0x1a0 = tc;                     // coordinator path — TC installed
  │  this->+0x1a8 = new ErrorReporter(tc, threadpool_, cb, clock);
  │
  ▼
[coordinator path] Log:
  "Megascale Topology Coordinator started for <num_slices>"
```

## What "matches a local interface" means

`CommunicationBackend::InitializeCoordinator` checks whether the
`Options.coordinator_address` string maps to the bound endpoint of
the local `GrpcTransport`. The check happens at the start of
`InitializeCoordinator` (instructions at `0x1ccad600..0x1ccad62a`):
`cmpq $0x0, 0x1a0(%rdi)` ensures the coordinator was not already
installed; a separate path at `+0x5c8` returns
`InternalError("Coordinator address mismatch ...")` when the local
process is not the coordinator.

The implication is that a job manager that points two processes at
the same `MEGASCALE_COORDINATOR_ADDRESS` will produce two failed
elections: only the process that wins the port bind initialises a
TopologyCoordinator; the other loses with a port-in-use error before
ever reaching the InitializeCoordinator call.

## Election failure modes

1. **`MEGASCALE_COORDINATOR_ADDRESS` empty.** `Options.coordinator_
   address` empty triggers a `StatusBuilder::CreateStatusAndConditional
   lyLog` of `"megascale_coordinator_address is empty"` (line 0x3fc /
   0x3fe inside `DiscoverTopologyAndAddressBindings`) and aborts the
   bootstrap.
2. **`MEGASCALE_PORT` unset.** Logs `"megascale_port is not set."` at
   rodata `0x9ff6b6d`. The runtime treats this as a fatal config
   error.
3. **Port bind fails** (port already in use). `GrpcTransport::Init`
   returns a non-OK Status which `CommunicationBackend` stores in
   `+0x148`. `DiscoverTopology...` reads that and returns
   `"Transport not initialized"` to the caller. The next time the
   runtime tries to talk to the coordinator the bootstrap fails with
   `UNAVAILABLE`.
4. **Two processes both believe they are coordinator.** Either both
   try to bind the same port and one fails as above, or both bind
   different local ports but the address advertised to workers is
   the same — workers race-connect to whichever process they reach
   first. The `LogUniqueIds` re-key detection (see
   [Failure Handling](failure-handling.md)) catches the resulting
   topology drift and warns at runtime.

## What is not done

There is no Raft, no Paxos, no lease, no health-checked leader
election. Megascale assumes the launcher delivers a unique
`(coordinator_address, num_slices)` setup; if it doesn't, the
rendezvous degrades into one of the failure modes above.

In particular, **coordinator restart loses bootstrap state**. The
new coordinator process has an empty `TopologyCoordinator`; workers
that already received the original response continue running, but
any new worker registration must wait for the entire fleet to
re-register. The current binary has no on-disk cache of the
assembled `MultiSliceTopologyInfo` and no on-startup
rehydration path.
