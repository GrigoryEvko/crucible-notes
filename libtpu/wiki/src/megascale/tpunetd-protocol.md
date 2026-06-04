# tpunetd Protocol

> *All addresses, symbols, and wire strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (ELF 64-bit LSB, x86-64, ~745 MB, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

`tpunetd` is the host-resident control daemon that owns the physical TPU
chips on a Cloud TPU VM or superpod host. The user process never touches
the chips directly; instead `libtpu.so` links a full client stub —
`superpod::tpunetd_client` — that connects to the daemon over a local
gRPC channel and drives chip lifecycle through it. This page documents
the *client stub as observed inside `libtpu.so`*: the four gRPC services
it links, the request/response message shapes, the Unix-socket / TCP
endpoint discovery, and how the client connects to (or declines to
connect to) the daemon at job start. The daemon binary itself is **not**
present in this wheel; every claim here derives from the linked
client-side unit alone, which carries the gRPC stubs, the four proto
`FileDescriptorProto` blobs, the validators, and the `TpunetdControl` /
`SessionMaster` machinery.

If you have used gRPC's generated C++ stubs, the surface is familiar: a
`Stub` class per service with one `BlockingUnaryCall`-backed method per
RPC plus an `async`-inner-class `ClientUnaryReactor` variant, a `NewStub`
factory taking a `shared_ptr<grpc::ChannelInterface>`, and a thin
hand-written wrapper (`TpunetdControl`) that marshals an internal
`SessionData` struct into proto requests. The two surprises worth
internalizing up front: (1) by default libtpu talks not to the daemon's
own `SessionControl` service but to an 8-method **VBARControl** shim
under a *different* package (`libtpu.tpunetd`), and (2) two of the four
services — `TpuNetworkSessionWorker` and `TpuNetworkSessionBarrier` — are
peer-to-peer between client processes and are *served by `libtpu` itself*,
not by `tpunetd`.

The bootstrap rendezvous that *uses* this stub (the
`CreateTpuNetworkInterface` → `Init` → `MaybeInitSessionMaster` startup
sequence and its place relative to Megascale) is owned by
[`bootstrap/tpunetd-relationship.md`](bootstrap/tpunetd-relationship.md);
this page owns the *RPC surface, the message taxonomy, and endpoint
discovery* and does not re-derive the startup sequence.

For reimplementation, the contract is:

- **The four gRPC services and their wire prefixes** — exact service
  paths, the method set of each, and which packages they live in.
- **The request/response message shapes** — recovered from the linked
  `FileDescriptorProto` strings and from the mangled stub method
  signatures (`Request`/`Response` type pairs per RPC).
- **Endpoint discovery and channel construction** — the Unix-socket
  default path, the `--vbar_control_service_url` override, plaintext
  (`InsecureChannelCredentials`) gRPC, and the connect-retry loop.
- **The default-vs-bypass branch** — when libtpu picks `VBARControl` vs
  `SessionControl::Stub`, and when it installs a no-op stub instead.

| | |
|---|---|
| **Client namespace** | `superpod::tpunetd_client` (774 mangled-symbol hits, `nm \| rg -c '8superpod14tpunetd_client'`) |
| **Daemon namespace** | `superpod::tpunetd` (720 mangled-symbol hits, `nm \| rg -c '8superpod7tpunetd'`) |
| **Source-path prefix** | `platforms/deepsea/software/superpod/routing/` |
| **Local endpoint (default)** | `/var/google/services/tpunetd/user.socket` (UDS) |
| **Transport** | plaintext gRPC (`grpc::InsecureChannelCredentials`) |
| **Channel ctor** | `superpod::tpunetd_client::ConnectToGrpcEndpoint` (`0x1ffcef60`) |
| **Impl-select factory** | `superpod::routing::CreateTpuNetworkInterface` (`0x1fba1100`) |
| **Default client stub** | `libtpu::tpunetd::grpc::VBARControl::Stub` |
| **Bypass client stub** | `superpod::tpunetd::grpc_gen::SessionControl::Stub` (`NewStub` `0x1ffcdd20`) |
| **Services / methods** | 4 services / 21 RPCs |

---

## Service Surface at a Glance

Four gRPC services are linked into `libtpu.so`. They split cleanly by who
hosts the server and who issues the calls. Every wire prefix below was
extracted verbatim from the decompiled `.rodata` string table; the method
counts match the per-method stub functions present in the IDA database.

| Service | Wire prefix | Methods | Server | Issued by libtpu |
|---|---|---|---|---|
| `SessionControl` | `/superpod.tpunetd.SessionControl/` | 7 | `tpunetd` daemon | only when `--bypass_vbar_control_service=true` |
| `VBARControl` | `/libtpu.tpunetd.VBARControl/` | 8 | `tpunetd` daemon (shim) | **default path** |
| `TpuNetworkSessionWorker` | `/superpod.tpunetd_client.TpuNetworkSessionWorker/` | 4 | peer `libtpu` process | session master → workers |
| `TpuNetworkSessionBarrier` | `/superpod.tpunetd_client.proto.TpuNetworkSessionBarrier/` | 2 | peer `libtpu` process | barrier fan-out |

> **QUIRK —** `SessionControl` and `VBARControl` expose almost the same
> method names but live in **different proto packages** (`superpod.tpunetd`
> vs `libtpu.tpunetd`) and therefore have **different, non-interchangeable
> message types**. `superpod.tpunetd.StartSessionRequest` and
> `libtpu.tpunetd.StartSessionRequest` are distinct descriptors. A
> reimplementation that aliases them will serialize against the wrong
> `FileDescriptorProto`. The package prefix in the wire path is the
> discriminator.

> **NOTE —** the `tpunetd` daemon's `SessionControl` proto also declares
> ~20 ICI-fabric / routing-controller RPCs (`CreateNetwork`,
> `SetRoutingTable`, `SetGtcConfiguration`, `PerformReset`, …). Those are
> present in the descriptor but **never issued from `libtpu`** — they
> belong to the higher-privileged superpod-routing controller plane. They
> are catalogued under "Daemon-only RPC surface" below for completeness,
> not as part of the client contract.

---

## Endpoint Discovery and Channel Construction

### Purpose

Before any RPC can be issued the client must resolve an endpoint URL and
open a gRPC channel to it. The endpoint is either a Unix domain socket
(local daemon link) or a TCP address (peer fanout); the channel is always
plaintext.

### Entry Point

```text
TpunetdClient::Init (decl, throws "Must run TpunetdClient::Init first")
  └─ ConnectToTpunetd                          ── default-vs-bypass + NoOp branch
       └─ ConnectToGrpcEndpoint (0x1ffcef60)    ── CreateChannel + retry loop
            ├─ grpc::InsecureChannelCredentials
            ├─ grpc::CreateChannel(url, creds)
            └─ Channel::GetState(true) until READY or deadline
```

### Algorithm

```c
// superpod::tpunetd_client::ConnectToGrpcEndpoint  (0x1ffcef60)
// args: (string_view url, absl::Duration deadline)
function ConnectToGrpcEndpoint(url, deadline):
    creds   = grpc::InsecureChannelCredentials()   // line 67 — plaintext, no TLS
    channel = grpc::CreateChannel(url, creds)       // line 70
    end     = absl::Now() + deadline
    loop:
        state = channel.GetState(/*try_to_connect=*/true)
        if state == 2:                              // line 104 — GRPC_CHANNEL_READY
            return channel
        if absl::Now() >= end:
            return Error("Failed to connect to " + url)   // rodata "Failed to connect to "
        WaitForStateChange(state, short_deadline)   // spin until next transition
```

### Endpoint selection

The URL the channel is opened on is resolved in `ConnectToTpunetd`, in
this order:

1. **`Options` URL field**, if the embedding runtime supplied one.
2. **`FLAGS_vbar_control_service_url`** — the absl flag
   `--vbar_control_service_url=<url>`; the TPU-driver env-var token is
   `kVbarControlServiceUrl`.
3. **Fallback to the Unix-socket default**
   `/var/google/services/tpunetd/user.socket` when the flag is unset.
   This exact string is present once in `.rodata` (confirmed in the
   decompiled output).

The connect log line is `"Connecting to vbar control service at "` (with
the resolved URL appended), confirmed verbatim in the binary.

### The stub chosen on top of the channel

Once the channel is `READY`, exactly one of two daemon stubs is wrapped:

```c
// inside ConnectToTpunetd
if process_owns_chips == false:
    install NoOpControl                         // rodata "process_owns_chips is false, using NoOpControl"
    return OK                                   // never opens a channel at all
else:
    channel = ConnectToGrpcEndpoint(url, retry_timeout)
    if FLAGS_bypass_vbar_control_service:       // default = false
        stub = superpod::tpunetd::grpc_gen::SessionControl::NewStub(channel)   // 0x1ffcdd20
        wrap into TpunetdControl
    else:
        stub = libtpu::tpunetd::grpc::VBARControl::NewStub(channel)
        wrap into VbarControl proxy
```

> **GOTCHA —** when `Options.process_owns_chips == false`, no channel is
> ever opened. The client installs `NoOpControl`, whose every method logs
> `"NoOpControl::<Op> is called; skipping tpunetd call"` and returns OK.
> A reimplementation that always dials the socket will hang on
> chip-less / sidecar processes that legitimately own no chips.

### Activation gate (impl-select factory)

Whether `tpunetd` is used at all is decided one level up, in
`superpod::routing::CreateTpuNetworkInterface(const tpu::TpuTopology&,
bool)` at `0x1fba1100`. The branch:

```c
// superpod::routing::CreateTpuNetworkInterface  (0x1fba1100)
function CreateTpuNetworkInterface(topology, process_owns_chips):
    if FLAGS_enable_tpunetd_client                          // default false
       && binary_search(kTpunetdSupportedTpuVersions, topology.tpu_type):
        log("Running in Cloud, using TpunetdClient")
        return TpunetdClient::Create(topology, tpu_type, Options)   // kTpunetd impl
    else:
        log("tpunetd either not supported or disabled, falling back to Slice Builder")
        return SliceBuilder-backed impl                     // accel_ssw::deepsea::slice_builder::*
```

`kTpunetdSupportedTpuVersions` is a sorted `tpu::TpuType` array in
`.rodata` at `0xb8ec184` — a static-local of
`superpod::routing::CreateTpuNetworkFactory` but referenced here. It holds
exactly **two** entries, `{4, 5}` (bytes `04 00 00 00 05 00 00 00`,
end sentinel at `0xb8ec18c`); the lookup is a `lower_bound`-style binary
search over those 4-byte `TpuType` values. So `tpunetd` is
**strictly the Cloud / production-superpod path**; everything else falls
back to the in-process SliceBuilder family with its own
`/accel_ssw.deepsea.slice_builder.SliceBuilderWorkerService/*` RPCs. The
`BorglessTpunetd` token in `.rodata` is the local-host variant used on
Cloud TPU VMs where no Borg job manager is present. Both activation log
strings are confirmed verbatim. The full startup sequencing is in
[`bootstrap/tpunetd-relationship.md`](bootstrap/tpunetd-relationship.md).

---

## Service 1 — SessionControl (daemon, 7 methods)

### Purpose

The user-facing chip-session lifecycle on the full daemon. Used directly
only when `--bypass_vbar_control_service=true`. A "session" is the right
to exclusive (or shared) control of a set of chips identified by
`asic_sw::proto::ChipLocation`.

### Encoding

```text
Wire prefix : /superpod.tpunetd.SessionControl/
Package     : superpod.tpunetd
Stub class  : superpod::tpunetd::grpc_gen::SessionControl::Stub
NewStub     : 0x1ffcdd20   (takes shared_ptr<grpc::ChannelInterface>, StubOptions)
Ctor        : 0x1ffcdd60
```

All seven methods follow the standard generated-stub signature
`Stub::<Op>(grpc::ClientContext*, const <Op>Request&, <Op>Response*)`
plus an `async` inner-class variant with a `ClientUnaryReactor`. Request
and response type pairs are recovered from the mangled per-method symbols.

| RPC | Request | Response | Stub addr | Confidence |
|---|---|---|---|---|
| `StartSession` | `superpod.tpunetd.StartSessionRequest` | `…StartSessionResponse` | `0x1ffce0e0` | CERTAIN |
| `StopSession` | `…StopSessionRequest` | `…StopSessionResponse` | `0x1ffce2c0` | CERTAIN |
| `StatSession` | `…StatSessionRequest` | `…StatSessionResponse` | `0x1ffce4a0` | CERTAIN |
| `CheckSessionHealth` | `…CheckSessionHealthRequest` | `…CheckSessionHealthResponse` | `0x1ffce6a0` | CERTAIN |
| `GetChipCoordinates` | `…GetChipCoordinatesRequest` | `…GetChipCoordinatesResponse` | `0x1ffceaa0` | CERTAIN |
| `GetCoreDump` | `…GetCoreDumpRequest` | `…GetCoreDumpResponse` | `0x1ffce8a0` | CERTAIN |
| `GrantSessionPermission` | `…GrantSessionPermissionRequest` | `…GrantSessionPermissionResponse` | `0x1ffcdf00` | CERTAIN |

### Client wrapper

`superpod::tpunetd_client::TpunetdControl` is the hand-written class that
wraps the `SessionControl` stub and translates an internal `SessionData`
struct into proto requests. Confirmed entry points:

| Wrapper method | Addr | Confidence |
|---|---|---|
| `TpunetdControl::StartSession(const SessionData&)` | `0x1ffcb660` | CERTAIN |
| `TpunetdControl::StopSession(const SessionData&)` | `0x1ffcbc80` | CERTAIN |
| `TpunetdControl::StatSession` | `0x1ffcc2a0` | HIGH |
| `TpunetdControl::CheckSessionHealth` | `0x1ffcc9a0` | HIGH |
| `TpunetdControl::GetCoreDump` | `0x1ffcd040` | HIGH |
| `TpunetdControl::GetChipCoordinates` | `0x1ffcd7c0` | HIGH |

Each wrapper sets the `grpc::ClientContext` deadline from the global
constant `superpod::tpunetd_client::kSessionControlTimeout` (mangled
`_ZN8superpod14tpunetd_client22kSessionControlTimeoutE`, type
`absl::Duration`). The deadline computation is identical across all
wrappers: `now = absl::Now(); deadline = now + kSessionControlTimeout;`
then `absl::ToChronoTime` → `grpc::Timepoint2Timespec` →
`context.set_deadline`.

---

## Service 2 — VBARControl (libtpu↔daemon shim, 8 methods, DEFAULT)

### Purpose

The **default** path. When `--bypass_vbar_control_service` is false
(default), libtpu does not call `SessionControl` directly; it goes
through a Virtual-BAR shim that mediates BAR-register operations through
`tpunetd` rather than letting the user process touch the chip directly.
The method set mirrors `SessionControl`'s six chip-session calls plus two
extras: `GetHostMetrics` and `TPUBackendConnectionTest`. It does **not**
expose `GrantSessionPermission` (that grant is a daemon-internal /
controller concern).

### Encoding

```text
Wire prefix : /libtpu.tpunetd.VBARControl/
Package     : libtpu.tpunetd                         (note: NOT superpod.tpunetd)
Stub class  : libtpu::tpunetd::grpc::VBARControl::Stub
NewStub     : 0x1ffd2360   (Stub ctor at 0x1ffd23a0)
Source proto: learning/45eac/tfrc/tpunetd/proto/vbar_control.proto  (TFRC team)
```

| RPC | Request | Response | Stub addr | Confidence |
|---|---|---|---|---|
| `StartSession` | `libtpu.tpunetd.StartSessionRequest` | `…StartSessionResponse` | `0x1ffd2580` | CERTAIN |
| `StopSession` | `…StopSessionRequest` | `…StopSessionResponse` | `0x1ffd2760` | CERTAIN |
| `StatSession` | `…StatSessionRequest` | `…StatSessionResponse` | `0x1ffd2940` | CERTAIN |
| `CheckSessionHealth` | `…CheckSessionHealthRequest` | `…CheckSessionHealthResponse` | `0x1ffd2b20` | CERTAIN |
| `GetChipCoordinates` | `…GetChipCoordinatesRequest` | `…GetChipCoordinatesResponse` | `0x1ffd3120` | CERTAIN |
| `GetCoreDump` | `…GetCoreDumpRequest` | `…GetCoreDumpResponse` | `0x1ffd2d20` | CERTAIN |
| `GetHostMetrics` | `…GetHostMetricsRequest` | `…GetHostMetricsResponse` | `0x1ffd3320` | CERTAIN |
| `TPUBackendConnectionTest` | `…TPUBackendConnectionTestRequest` | `…TPUBackendConnectionTestResponse` | `0x1ffd2f20` | CERTAIN |

All eight stub functions and their `async` / `ClientUnaryReactor`
variants are present as distinct decompiled functions; the request and
response type names are read directly off the mangled signatures (e.g.
`…Stub::StartSession(grpc::ClientContext*, const StartSessionRequest&,
StartSessionResponse*)`).

### Extra messages in the `libtpu.tpunetd` package

Beyond the request/response pairs, the VBAR proto declares the envelope
machinery used to multiplex BAR pokes:

| Message / enum | Role | Confidence |
|---|---|---|
| `libtpu.tpunetd.Status` | status wrapper carrying nested `Status.StatusCode` enum | HIGH |
| `libtpu.tpunetd.VBARRequestType` | oneof-tag enum selecting which VBAR poke an envelope carries | HIGH |
| `libtpu.tpunetd.VBARRequestInput` | request envelope union over the poke types | HIGH |

> **NOTE —** `TPUBackendConnectionTest` is gated by the
> `TPU_BACKEND_CONNECTION_TEST` env var. The on-the-wire serialization of
> the `VBARRequestType` / `VBARRequestInput` oneof envelope is **not**
> recoverable from the client unit alone — reproducing it requires
> decoding the `protodesc_cold` field IDs or the daemon binary. Marked
> LOW for any reimplementer who needs byte-exact VBAR poke framing.

---

## Service 3 — TpuNetworkSessionWorker (peer-to-peer, 4 methods)

### Purpose

The inter-host fanout channel. One node in a slice runs the
**session-master** role; the rest run **session-worker** roles. The
master contacts every worker over **TCP gRPC** for heartbeat, session
info, and core-dump collection. This is the only piece of the stack where
client hosts talk *to each other*; `tpunetd` is unaware of it — it hands
the master the worker list and steps out.

### Encoding

```text
Wire prefix : /superpod.tpunetd_client.TpuNetworkSessionWorker/
Package     : superpod.tpunetd_client
Transport   : gRPC over TCP (peer hosts)  /  in-process loopback (local node)
```

| RPC | Request | Response | Confidence |
|---|---|---|---|
| `CheckHeartbeat` | `superpod.tpunetd_client.CheckHeartbeatRequest` | `…CheckHeartbeatResponse` | CERTAIN |
| `GetSessionInfo` | `…GetSessionInfoRequest` | `…GetSessionInfoResponse` | CERTAIN |
| `UpdateSessionInfo` | `…UpdateSessionInfoRequest` | `…UpdateSessionInfoResponse` | CERTAIN |
| `CollectCoreDump` | `…CollectCoreDumpRequest` | `…CollectCoreDumpResponse` | CERTAIN |

### Stub implementations

Two stub types implement the same surface:

- **`SessionWorkerStubRpc`** (`CheckHeartbeat` at `0x1ffcb1a0`) — real
  gRPC over an internal `TpuNetworkSessionWorker::StubInterface*`.
- **`SessionWorkerStubLocal`** (`CheckHeartbeat` at `0x1ff920e0`) — same
  surface but loops through a `FakeServerContext` so the master can call
  its own worker process without an actual TCP round-trip.

The peer list is **not discovered by the client itself**. It is supplied
by the caller as a `flat_hash_map<string worker_name,
SessionWorkerStubFactory>` to `SessionMaster::Create`; each entry's
factory closure yields a `unique_ptr<SessionWorkerStub>` that is either
the RPC or the local variant. The authoritative source of peer addresses
is the Megascale coordinator (`MEGASCALE_COORDINATOR_ADDRESS`). See
[`fleet-metadata/overview.md`](fleet-metadata/overview.md).

### UpdateSessionInfoRequest field schema

Recovered from validator error texts confirmed verbatim in the binary
(`session_worker_validation.cc`):

| Field | Type | Rule | Confidence |
|---|---|---|---|
| `in_session` | bool | required (`"in_session must be specified"`) | HIGH |
| `session_id` | string | required iff `in_session == true` (`"session_id must be present iff in_session is true"`) | HIGH |
| `failure_type` | enum | must be absent iff `in_session == true`; value `SLICE_FAILURE_UNKNOWN` rejected | HIGH |

The `failure_type` enum is shared with the SliceBuilder code path
(`accel_ssw::deepsea::slice_builder::SliceFailureType`).

### Heartbeat behavior

`SessionMaster::CheckSessionHeartbeat()` (`0x1ffa6180`) drives the sweep:

```c
// SessionMaster::CheckSessionHeartbeat  (0x1ffa6180)
function CheckSessionHeartbeat():
    lock(heartbeat_mutex)                         // SessionMaster offset +104
    ExecuteOnAllWorkers([](name, stub){            // fans CheckHeartbeat to all peers
        return stub->CheckHeartbeat(req, resp, deadline)
    })
    for worker in results:
        if any chip has chip_id == 0:              // vector<asic_sw::proto::ChipLocation>
            log("Session is failing due to the following chips having zero as chip id")
            transition SessionState -> kFailing (=3)   // HandleFailingSession on ThreadPool
            return
    reschedule self at now + heartbeat_interval    // ThreadPool::ScheduleAt; interval at offset 0
```

`heartbeat_interval` lives at `SessionMaster` offset 0 (set from
`Options.heartbeat_interval`); the per-call deadline for the heartbeat
sweep is the user-supplied absolute `absl::Time` rather than the
`kSessionControlTimeout` duration used by the unary daemon calls.

---

## Service 4 — TpuNetworkSessionBarrier (peer-to-peer, 2 methods)

### Purpose

A peer-to-peer rendezvous barrier used to synchronize the hosts of one
slice. Like `TpuNetworkSessionWorker`, the server side is **hosted by
each `libtpu` process itself** — the `WithCallbackMethod_Notify` /
`WithCallbackMethod_WaitForReady` template instantiations in the symbol
table are the in-process server. `tpunetd` is uninvolved.

### Encoding

```text
Wire prefix : /superpod.tpunetd_client.proto.TpuNetworkSessionBarrier/
Package     : superpod.tpunetd_client.proto      (note the extra .proto sub-namespace)
Client class: BroadcastBarrier  (tpunetd_client/lib/broadcast_barrier.cc)
```

| RPC | Request | Response | Confidence |
|---|---|---|---|
| `Notify` | `superpod.tpunetd_client.proto.NotifyRequest` (barrier_id string + chip_locations) | `…NotifyResponse` (status only) | CERTAIN |
| `WaitForReady` | `…WaitForReadyRequest` (barrier_id string) | `…WaitForReadyResponse` (status only) | CERTAIN |

### Algorithm

```c
// BroadcastBarrier  (Init 0x1ff9bac0, BroadcastNotification 0x1ff9c320)
function Init(absl::Duration):
    for each known peer worker:                 // peer count at this+104, peer array at this+96
        build one std::function<absl::Status()>  // 32 B closure each; vector sized 32 * num_workers

function BroadcastNotification(barrier_id, deadline):
    materialize vector<grpc::ClientContext>(num_peers)   // 400 B each; alloc = 400 * num_peers
    counter = absl::BlockingCounter(num_peers)
    for peer in peers:                          // parallel fan-out
        log("Notifying " + peer + " with barrier id " + barrier_id)
        async Notify(peer, {barrier_id, chip_locations}) -> counter.DecrementCount()
    counter.Wait()

function BroadcastWaitForReady(barrier_id, deadline):
    // symmetric parallel fan-out of WaitForReady

function SyncWithTimeout(barrier_id, timeout):
    BroadcastNotification(barrier_id, now + timeout)   // fire-and-forget per peer
    BroadcastWaitForReady(barrier_id, now + timeout)   // blocks until every peer notified AND waiting
```

`SyncWithTimeout` is the user-facing collective rendezvous point: `Notify`
is fire-and-forget per peer; `WaitForReady` blocks until every peer has
both notified the same `barrier_id` and is itself waiting on it. A
`NoopBarrier` (`lib/noop_barrier.h`) replaces this when
`process_owns_chips == false`.

---

## SessionData — what every daemon request carries

`superpod::tpunetd_client::SessionControlInterface::SessionData` is the
internal struct `TpunetdControl` marshals into every Start/Stop/Stat/
CheckHealth/GetCoreDump/GetChipCoordinates request. Layout recovered from
the decompiled call sites (offsets in bytes):

| Offset | Type | Meaning | Confidence |
|---|---|---|---|
| `+0` | `vector<DriverInterface*>::begin` | per-chip driver pointer array (start) | HIGH |
| `+8` | `vector<DriverInterface*>::end` | drivers vector end — used as the for-loop bound | HIGH |
| `+24` | `superpod::routing::proto::Topology` | topology proto attached to the session | HIGH |
| `+104` | `NetworkContextId` | network identity, copied into request iff flag at `+144 == 1` | HIGH |
| `+144` | bool | `has_network_context` — gates the `+104` copy | HIGH |

Each driver in the `[+0, +8)` array exposes a virtual function at vtable
offset `+56` returning an `asic_sw::ChipLocation`; libtpu calls it per
driver and appends the result to the request's `repeated ChipLocation
chip_locations`.

> **QUIRK —** `tpunetd` never discovers chips on its own. Every session
> RPC carries the **full local `ChipLocation` set** of the user process,
> announced by the client from its driver vector. A reimplementation that
> expects the daemon to enumerate chips will find an empty
> `chip_locations` and a session that owns nothing.

---

## CoreDump collection

Two distinct paths produce core dumps:

```text
direct (daemon):
  TpunetdControl::GetCoreDump(SessionData, CoreDumpType, Duration)
    -> /superpod.tpunetd.SessionControl/GetCoreDump
    CoreDumpType ∈ { CORE_DUMP_UNKNOWN, CORE_DUMP_CHIP_DUMP, CORE_DUMP_ICI_DUMP }

peer fanout (master collects from all workers):
  SessionMaster::ExecuteOnAllWorkers([](name, stub){
      return stub->CollectCoreDump(req, resp, deadline); })
    -> /superpod.tpunetd_client.TpuNetworkSessionWorker/CollectCoreDump
    writes through accel_ssw::deepsea::CoreDumpUploader (CoreDumpUploaderInterface)
    which can also reach logmanagerd at
      /var/google/services/logmanagerd/remote_coredump.socket
```

---

## Daemon-only RPC surface (declared, never issued by libtpu)

The `superpod.tpunetd` proto descriptor declares a large ICI-fabric /
routing-controller surface that is linked (as descriptor bytes) but never
called from the client. These belong to the superpod-routing controller
plane that also speaks to `tpunetd`. Listed by axis, not exhaustively, so
a reimplementer knows the daemon's full method space without a 30-row
dump:

| Axis | RPCs (declared in descriptor) |
|---|---|
| Network lifecycle | `CreateNetwork`, `DeleteNetwork`, `ListNetworks`, `UpdateTopology`, `GetLocalTopology` |
| ICI data links | `ConfigureIci`, `EnableIciDataLink`, `WaitForDataLinkUp`, `ResetIciNetwork`, `UpdateIciNetworkConfiguration` |
| Routing | `SetRoutingTable` (+ `RoutingTableMap`) |
| Coordinates / IDs | `SetChipCoordinates`, `SetGlobalChipId` |
| Global time counter (GTC) | `SetGtcConfiguration`, `ClearGlobalGtc`, `WaitForGtcReset` |
| Reset / driver | `PerformReset`, `RestartDriver` |

These confirm `tpunetd` owns ICI-link configuration, routing-table
install, and global-time synchronization on the host — but none are part
of the libtpu client contract. The chip-coordinate output they produce is
read back by libtpu via `GetChipCoordinates` and flows into the
[fleet-metadata](fleet-metadata/overview.md) and Megascale topology
exchange.

---

## Proto File Inventory

Four `FileDescriptorProto` blobs back the four services; three additional
routing protos supply shared message types. Filenames are visible as
length-prefixed strings in the proto pool (`protodesc_cold` ELF section).

| Proto path | Package | Service |
|---|---|---|
| `…/superpod/routing/tpunetd/proto/tpunetd.proto` | `superpod.tpunetd` | SessionControl |
| `…/superpod/routing/tpunetd/proto/common.proto` | `superpod.tpunetd` | (messages only) |
| `…/superpod/routing/tpunetd_client/proto/session_worker.proto` | `superpod.tpunetd_client` | TpuNetworkSessionWorker |
| `…/superpod/routing/tpunetd_client/proto/session_barrier.proto` | `superpod.tpunetd_client.proto` | TpuNetworkSessionBarrier |
| `…/superpod/routing/tpunetd_client/proto/common.proto` | `superpod.tpunetd_client` | (messages only) |
| `learning/45eac/tfrc/tpunetd/proto/vbar_control.proto` | `libtpu.tpunetd` | VBARControl |

Shared routing protos: `common/proto/topology.proto`
(`superpod.routing.proto`), `common/proto/chip_coordinate.proto`,
`common/proto/tpu_type.proto` (the `TpuType` enum, dense `0..10`, names
fetched at runtime via
`proto2::internal::NameOfDenseEnum<&TpuType_descriptor,0,10>`), and
`common/proto/ici_network_config.proto`.

> **GOTCHA —** the leading digit on each descriptor filename string is the
> protobuf-internal length-prefix byte, **not** a field number. The
> numeric **field IDs** for every request/response are not recovered here;
> they require decoding the compressed `protodesc_cold` entries or the
> daemon binary. Any byte-exact wire reimplementation must do that
> decode — the message *shapes* on this page are sufficient to name the
> fields but not to assign their tag numbers. (LOW confidence on field
> numbers; CERTAIN on message names and RPC surface.)

---

## Security and Observability Notes

- **No transport security on the local link.** The channel is built with
  `grpc::InsecureChannelCredentials()` (confirmed in
  `ConnectToGrpcEndpoint` at line 67). Authentication, if any, is
  daemon-side — likely `SO_PEERCRED` or filesystem permissions on the
  Unix socket — and is **not observable from the client unit**. A
  third-party client connecting to the socket would face whatever the
  daemon enforces, which this binary does not reveal (LOW).
- **Telemetry.** `IciSessionMonitorImpl` records session-health,
  state-transition latency, broadcast latency, notification latency, and
  missed-health-check counts into TF streamz counters under the `/tpu/…`
  tree via `tsl::monitoring`. `tpu_type` label values are resolved with
  `NameOfDenseEnum<&TpuType_descriptor,0,10>`.

---

## Cross-References

- [Megascale Overview](overview.md) — where tpunetd sits in the multi-host / DCN stack
- [bootstrap/tpunetd Relationship](bootstrap/tpunetd-relationship.md) — the startup sequence that *uses* this stub; tpunetd → MegaScaleTransport ordering
- [bootstrap/Overview](bootstrap/overview.md) — the job-bringup rendezvous as a whole
- [fleet-metadata/Overview](fleet-metadata/overview.md) — the peer-address / chip-coordinate metadata exchanged across this surface
- [Error Aggregator](error-aggregator.md) — where session-failure signals (kFailing, zero-chip-id) surface to the runtime
- [ICI Topology Discovery](../ici/topology-discovery.md) — the ICI-fabric configuration the daemon's superpod-controller surface drives
