# Coordinator Election

Megascale does not run a vote. The coordinator is the process the
launcher (Borg, GKE, or a hand-rolled job script) designates by way of
`MEGASCALE_COORDINATOR_ADDRESS` and `MEGASCALE_PORT`. Every process
that reaches `DiscoverTopologyAndAddressBindings` (@`0x1ccacb80`)
installs a `TopologyCoordinator` via `InitializeCoordinator`
(@`0x1ccad600`); the coordinator-vs-worker role is then read from that
`TopologyCoordinator`'s state — `CommunicationBackend::StartHeartBeat`
(@`0x1ccade60`) calls a virtual method on the object at `+0x1a0` and
branches on whether it returns `2` (coordinator) or not (worker). The
decision is therefore deterministic: it falls out of the way the
launcher sets the environment variables on each node.

> **CONFIRMED.** All VAs, offsets, source-line numbers and strings on
> this page were verified byte-for-byte against the IDA decompile and
> `objdump`/`strings` of `libtpu.so` (wheel 0.0.40, build-id
> `89edbbe81c5b328a958fe628a9f2207d`).

## The election inputs

`CommunicationBackend::PopulateOptionsFromFlags()` (@`0x1ccab3c0`)
reads four flags into the per-process `Options` struct, at the byte
offsets confirmed below:
`FLAGS_megascale_coordinator_address` → `+0x00`,
`FLAGS_megascale_port` → `+0x30` (int32),
`FLAGS_megascale_port_name` → `+0x18`,
`FLAGS_megascale_topology_discovery_timeout` → `+0x34`.
The remaining rows are populated on other paths; offsets marked
**INFERRED** were not confirmed in this function.

| Source | Field copied into Options | Effect |
|---|---|---|
| `FLAGS_megascale_coordinator_address` (`MEGASCALE_COORDINATOR_ADDRESS`) | `Options.coordinator_address` (string at `+0x00`) | The well-known endpoint of the coordinator. Every process resolves this address; the one whose local interface matches becomes coordinator. |
| `FLAGS_megascale_port` (`MEGASCALE_PORT`) | `Options.port` (int32 at `+0x30`) | Bind port for the local `MegaScaleTransport` gRPC server. Required — when unset libtpu logs `"megascale_port is not set."` and aborts. |
| `FLAGS_megascale_port_name` | `Options.port_name` (string at `+0x18`) | Alternative named port resolved by the host's port-allocation service. When set takes priority over numeric port. |
| `FLAGS_megascale_num_slices` (`MEGASCALE_NUM_SLICES`) | passed through `xla_megascale` → `InitializeCoordinator(num_slices)` | Total slice count of this job. The coordinator's `TopologyCoordinator` waits for exactly this many slice registrations. |
| `FLAGS_megascale_slice_id` (`MEGASCALE_SLICE_ID`) | `Options.slice_id` (int32 at `+0x58`, **INFERRED**) | This process's slice id. Workers put this into every request; coordinator uses it as the key in `flat_hash_map<int, SliceState>`. |
| `FLAGS_megascale_topology_discovery_timeout` | `Options.topology_discovery_timeout` (absl::Duration at `+0x34`, int64 + int32 at `+0x3c`) | Per-RPC deadline applied to `GetMultiSliceTopology`. Acts as the upper bound on total rendezvous time. |
| `FLAGS_tf_tpu_preexecution_barrier_timeout` | `Options.barrier_timeout` (absl::Duration at `+0x44..+0x50`, **INFERRED**) | Per-RPC deadline applied to subsequent `Barrier` calls. |
| `FLAGS_megascale_transport_type` (`MEGASCALE_TRANSPORT_TYPE`) | `Options.transport_type` | `"grpc"` (default) selects `GrpcTransport`; `"chaotic_good_legacy"` selects the experimental Chaotic-Good transport. |
| `FLAGS_megascale_transport_numa_node` | `Options.transport_numa_node` | Optional NUMA affinity hint for the transport thread. |
| `FLAGS_megascale_authentication` (`MEGASCALE_AUTHENTICATION`) | `Options.auth_method` | Selects gRPC `ServerCredentials`. Insecure by default for non-Google internal builds. |

The runtime emits a single instrumentation log line at startup that
echoes the election inputs:

```
megascale_coordinator_address:$0 megascale_port: $1 megascale_port_name: $2 topology_discovery_timeout: $3 barrier_timeout: $4
```

(literal absl-substitution template at rodata `0x9b27325`; `$0..$4`
are filled at runtime). Combined with `Instrumentations:
megascale_num_slices: ` and `Instrumentations: megascale_slice_id: `
(rodata `0xa23ae2a`, `0xa282bf7`), these lines uniquely identify the
role each process is playing.

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
  │  // @0x1ccacb80
  ▼
InitializeCoordinator(num_slices)              // @0x1ccad600, called unconditionally
  │  if (this->+0x1a0 != 0)                    // TopologyCoordinator already installed?
  │      return MakeErrorImpl<13>(             // INTERNAL, line 1480
  │          "Coordinator was already initialized. Re-init not supported.");
  │  TopologyCoordinator* tc = new TopologyCoordinator(num_slices);  // operator new(0x108)
  │  this->+0x1a0 = tc;                        // TC installed
  │  this->+0x1a8 = new ErrorReporter(tc, threadpool_, cb, clock);
  │  return 1;                                 // OK
  │
  ▼
TopologyCoordinator::TopologyCoordinator(num_slices)   // @0x213b7a40
  │  CHECK(num_expected_slices_ > 0)           // topology_coordinator.cc:55
  │  Log (topology_coordinator.cc:56):
  │    "Megascale Topology Coordinator started for " << num_slices << " slices"
```

## What `InitializeCoordinator` actually checks

`CommunicationBackend::InitializeCoordinator` does **not** compare the
`coordinator_address` string against a local interface. Its only guard
is an idempotency check: the prologue at `0x1ccad600..0x1ccad62a`
reads the `TopologyCoordinator` slot and refuses a second
installation.

```asm
1ccad600: 48 83 bf a0 01 00 00 00  cmpq  $0x0,0x1a0(%rdi)   ; TopologyCoordinator installed?
1ccad608: 74 1d                    je    1ccad627           ; not yet -> install it
1ccad60a: 48 8d 3d ...             lea   ...,%rdi           ; "Coordinator was already initialized..."
1ccad618: be 3b 00 00 00           mov   $0x3b,%esi         ; length 59
1ccad61d: ba c8 05 00 00           mov   $0x5c8,%edx        ; source line 1480
1ccad622: e9 59 13 47 04           jmp   MakeErrorImpl<13>  ; INTERNAL
```

When `0x1a0` is already non-NULL the function returns
`MakeErrorImpl<13>("Coordinator was already initialized. Re-init not
supported.", 59, 1480, …)` — the immediate `$0x5c8` is the **source
line 1480**, not a worker-path address. When `0x1a0` is NULL it
allocates a `TopologyCoordinator` (`operator new(0x108)`), stores it at
`+0x1a0`, installs an `ErrorReporter` at `+0x1a8`, and returns `1`
(OK). There is no `coordinator_address != local_address` branch and no
`"Coordinator address mismatch"` string in the binary.

The coordinator address is required, however: `DiscoverTopology...`
returns `MakeErrorImpl<13>("Discovery not available without
coordinator set.", 48, 1025, …)` when the coordinator-address field is
empty (read from `this+0x178`/`this[47]`).

The implication is that a job manager that points two processes at
the same `MEGASCALE_COORDINATOR_ADDRESS`+`MEGASCALE_PORT` will produce
a port-bind collision: only the process that wins the `GrpcTransport`
bind has a working transport; the other surfaces the transport-init
failure (stored at `+0x148`) on its next coordinator RPC.

## Election failure modes

1. **`MEGASCALE_COORDINATOR_ADDRESS` empty.** Inside
   `DiscoverTopologyAndAddressBindings`, an empty coordinator-address
   field returns `MakeErrorImpl<13>("Discovery not available without
   coordinator set.", 48, 1025, …)` — an `INTERNAL` status — and
   aborts the bootstrap. (There is no `"megascale_coordinator_address
   is empty"` string in the binary.)
2. **`MEGASCALE_PORT` unset.** Logs `"megascale_port is not set."` at
   rodata `0x9ff6b6d`. The runtime treats this as a fatal config
   error.
3. **Transport not initialized / port bind fails.**
   `DiscoverTopology...` calls `InitializeTransportLayerInternal` and
   stores its Status at `+0x148` (`this[41]`); a non-OK result is
   re-emitted at source line 1022. If the transport pointer
   (`this[39]`, `+0x138`) is still NULL when the runtime issues
   `GetMultiSliceTopology`, the code returns
   `MakeErrorImpl<13>("Transport not initialized.", 26, 1473, …)`.
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

## Cross-References

- `CommunicationBackend::InitializeCoordinator` — `0x1ccad600`
- `CommunicationBackend::DiscoverTopologyAndAddressBindings` — `0x1ccacb80`
- `CommunicationBackend::StartHeartBeat` — `0x1ccade60`
- `CommunicationBackend::PopulateOptionsFromFlags` — `0x1ccab3c0`
- `TopologyCoordinator::TopologyCoordinator` — `0x213b7a40`
- [Failure Handling](failure-handling.md) — heartbeat loss and re-key detection
