# Topology Discovery

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`; `.text` VMA == file offset). Symbols are present in the full-symbol binary; demangled names and addresses are cross-checked against the IDA decompile of `libtpu.so` v0.0.40.

## Abstract

This page documents how the slice controller learns the **physical torus shape** of a TPU pod-slice: how the chip-local connectivity that every worker reported in phase 1 is folded into a single global topology, how each chip is assigned a Cartesian `(X,Y,Z)` coordinate, and how that coordinate map is turned into a slice-wide integer chip-id pushed back to every chip. The entry point is `accel_ssw::deepsea::slice_builder::Master::DiscoverTopology` @`0x1fbbe4e0`, which drives the composite `TopologyDiscoverer::Discover` @`0x1fbff7e0` and then installs a `ResilientToroidalTopology` on the `Master`. Coordinates and the per-direction link map become the input to chip-id assignment (`Master::SetGlobalChipId` @`0x1fbbe7e0`) and to route-table generation.

The single most important fact a reimplementer must internalise: **discovery sends no active probe.** By the time `DiscoverTopology` runs, every chip's firmware has already negotiated, at PHY/data-link-up time, *who is on the other end of each SerDes port* (remote `ChipLocation`, remote port name, axis orientation). That per-port "who's there" field — shipped to the `Master` in the `LocalTopology` proto during phase 1 — **is** the probe response. Discovery is therefore a pure graph-construction-and-validation pass over already-gathered data, not a network exchange. The `"ICI Probe failed..."` `.rodata` string belongs to the *post*-bring-up `LinkChecker` health probe, not to this path.

This page owns three things: (1) the **discovery probe** — what the `LocalTopology` graph contains and the seven-step inference (`TopologyDiscoverer::Discover`); (2) the **coordinate↔chip map** — the BFS that propagates `(X,Y,Z)` from the origin chip and the conflict checks that validate it; and (3) the **global chip-id assignment** — how the validated coordinate map is converted to a dense `0..N-1` id and pushed to every chip. Link bring-up (the firmware PHY / host DL split and the 16-step `Master::InitSlice`) is on **[Link Bring-Up](link-bringup.md)**; the route-table generation that consumes this output is on **[Route-Table Generation](../routing/route-table-generation.md)**; the section map is **[ICI Overview](overview.md)**.

For reimplementation, the contract is:

- **Input.** A `vector<LocalTopology>` (one entry per chip, collected by the phase-1 `GetLocalTopology` fan-out) plus a target `ToroidalTopologyInterface` describing the *intended* slice shape. The locals span is passed as `Master+776`/`Master+784` (begin/end).
- **Probe model.** No active discovery probe. Each `LocalTopology` carries, per port, the firmware-resolved `remote_chip_location`, `remote_port`, `orientation` (axis), `is_data_layer_connected`, and (on 3-D parts) `polarity`. Discovery infers the missing sign on 2-D parts, builds the global link map, and validates it.
- **Output.** A `ResilientToroidalTopology` installed at `Master+152`, backed by a `flat_hash_map<ChipLocation, Coordinates>` (the coordinate↔chip map, held at `Master+832`) and a per-chip `map<Direction, PhysicalIciLink>` (the direction↔port map that seeds routing).
- **Id assignment.** `Master::SetGlobalChipId` reads each owned chip's coordinate and asks the topology object for that chip's dense integer id (Cartesian ordering, X fastest), then ships a `repeated ChipLocationToId` list to the worker.
- **Idempotence.** Re-discovery is rejected (`already_discovered_` guard); `SetGlobalChipId` requires the chip map to be populated first.

| | |
|---|---|
| **Discovery entry** | `Master::DiscoverTopology` @`0x1fbbe4e0` → inner lambda `$_0::operator()` @`0x1fbc1ae0` |
| **Composite discoverer** | `TopologyDiscoverer::Discover` @`0x1fbff7e0` (ctor @`0x1fbff680`); legacy fallback `LegacyTopologyDiscoverer::Discover` @`0x213dcfe0` |
| **Link discovery** | `IciDiscoverer::Init` @`0x1fc09d40`, `IciDiscoverer::Discover` @`0x1fc0b720` |
| **Coordinate assignment** | `ChipCoordinatesAssigner::Assign` @`0x1fc00420`, `::BreadthFirstWalk` @`0x1fc02040` |
| **Polarity assignment** | `superpod::routing::IciLinkPolarityAssigner::Assign` @`0x1fc0d880`, `::ChooseSeed` @`0x1fc10cc0` |
| **Coordinate↔chip map** | `flat_hash_map<ChipLocation, Coordinates>` @`Master+832`; topology installed @`Master+152` |
| **Global chip-id push** | `Master::SetGlobalChipId(string_view, Stub*)` @`0x1fbbe7e0` |
| **Coordinate push** | `Master::SetChipCoordinates` @`0x1fbc4640` → `WorkerService::SetChipCoordinates` @`0x1fc3db80` |
| **New-module gate** | flag `tpu_slice_builder_topology_discovery_new_module` (else legacy path) |

---

## 1. The "probe" is phase 1 — the `LocalTopology` graph

`DiscoverTopology` runs *after* the `Master` has already called `GetLocalTopology` on every worker in the slice (step 1 of `Master::InitSlice`; see **[Link Bring-Up](link-bringup.md)** for the full phase table). Each worker's `WorkerService::GetLocalTopology` @`0x1fc3c740` returns a list of `LocalTopology` protos assembled by the chip-local driver from the firmware-readable `ChipConnectorInfo` register set. The `Master` folds them into its owned `vector<LocalTopology>` at `Master+776`/`+784`/`+792` and caches the per-worker chip set in a `flat_hash_map<string, flat_hash_set<ChipLocation>>` at `Master+800`.

That vector is the whole input to discovery — which is why the inner lambda passes `*(_QWORD*)(this+776)` (begin) and `*(_QWORD*)(this+784)` (end) of the locals vector straight into `TopologyDiscoverer::Discover` as `absl::Span<const LocalTopology>`.

### 1.1 `LocalTopology` wire format

The `accel_ssw::deepsea::proto::LocalTopology` proto carries one chip's connectivity snapshot. The C++ wrapper (`LocalTopology::LocalTopology(proto&)` @`0x1ffdb620`) materialises a **2560-byte** record (confirmed: `operator new(2560 * count)` in the vector emplace path): a ~40-byte header (`ChipLocation` + `hostname`) followed by **12 inline 208-byte `PortEntry` slots** (12 × 208 = 2496), with the `num_ports` count stored as an `int32` at byte offset **2552** (`*((int*)this + 638)`) — i.e. *after* the port array, not in the header. `kMaxPortsPerChip = 12` in the proto, even though current SerDes hardware uses 4 ports per chip — see [ICI Overview](overview.md) §1.

```text
message LocalTopology {
  optional asic_sw.proto.ChipLocation chip_location = 1;  // tray/slot identity, e.g. "tray12-3"
  optional string                     hostname      = 2;  // chip's host machine FQDN
  optional int32                      num_ports     = 3;  // 0..12 actual SerDes count (4 on JFC/DFC)
  repeated PortEntry                  ports         = 4;  // num_ports entries
}

message PortEntry {
  optional asic_sw.deepsea.PortName        local_port             = 1;  // e.g. "ici_n0" — letter encodes lane + axis
  optional asic_sw.proto.ChipLocation      remote_chip_location   = 2;  // who is on the other end (set iff DL up)
  optional asic_sw.deepsea.PortName        remote_port            = 3;  // which remote port our cable plugs into
  optional int32                           port_index             = 4;  // hardware port index (0..3 on jfc)
  optional bool                            is_data_layer_connected = 5; // DL came up; false = missing cable / PHY fail
  optional accel_ssw.deepsea.proto.Orientation orientation        = 6;  // X / Y / Z axis (from SerDes connector ID)
  optional accel_ssw.deepsea.proto.Polarity    polarity           = 7;  // POSITIVE/NEGATIVE/UNKNOWN (UNKNOWN on 2-D)
  optional bool                            is_high_latency        = 8;  // inter-tray cable; affects latency model
}
```

> **NOTE —** field *names* are inferred from C++ accessor order and `ByteSizeLong` serialization order; the numeric protobuf tags above are nominal (HIGH confidence on names/order, LOW on the exact tag numbers, which would require decoding the linked `FileDescriptorProto` blob).

The enum value mappings recovered from `.rodata` are:

| Enum | Values | Confidence |
|---|---|---|
| `Polarity` | `UNKNOWN_POLARITY=0`, `POSITIVE=1`, `NEGATIVE=2` | HIGH |
| `Orientation` | `UNKNOWN_ORIENTATION=0`, `X=1`, `Y=2`, `Z=3` | HIGH |
| `Direction` (axis × sign) | `kIciXPlus/XMinus/YPlus/YMinus/ZPlus/ZMinus` (string anchors present; numeric order LOW) | HIGH names / LOW numeric |

`orientation` is **physical** — it is baked into the cabling and exposed identically on both ends of a link by firmware. What discovery adds is the *sign* (polarity), the Cartesian coordinates, the global validation, and the direction→port map that routing consumes. `Direction::Opposite` (referenced inline in `IciDiscoverer::Init`) maps each `Direction` to its sign-flipped counterpart — the invariant every bidirectional link is checked against (§2.3).

---

## 2. `TopologyDiscoverer::Discover` — the seven-step inference

`Master::DiscoverTopology` @`0x1fbbe4e0` builds the inner-lambda capture `{this, &target_size, origin_coord}` and invokes `$_0::operator()` @`0x1fbc1ae0`, which drives the composite. The composite `TopologyDiscoverer` (ctor @`0x1fbff680`) holds five sub-objects at fixed pointer offsets +8..+40 — `IciLinkPolarityAssigner`, `ChipCoordinatesAssigner`, `IciDiscoverer`, `TopologyFaultVerifier`, `TrayShapeChecker` — and is gated by `tpu_slice_builder_topology_discovery_new_module`. The `LegacyTopologyDiscoverer` @`0x213dcfe0` is the fallback (it adds a `SliceReshaper` step and a monolithic walk but produces the same `ResilientToroidalTopology`).

> **[CONFIRMED]** `Master::DiscoverTopology` @`0x1fbbe4e0`: builds a 3-dim `Coordinates` from a `.rodata` constant array (`{4,4,4}`, not a `Master` field), captures `{this, &(*(int*)(this+304)), coord}` — where `this+304` (`*((int*)this + 76)`) is the target-size int — and calls `$_0::operator()` twice (once normally, once after optional fault injection). The `$_0 != 1` (not-OK) path returns `CreateStatusAndConditionallyLog(668, "…/master.cc", …)` — i.e. the wrapping error `"Failed to discover ICI network topology"` is emitted at **`master.cc:668`** (the raw-anchor estimate of `:655` is superseded by the decompile).

```c
// TopologyDiscoverer::Discover(target_topology, locals_span, option)   // 0x1fbff7e0
if (already_discovered_)                                                // +48 guard
    return FAILED_PRECONDITION                                          // "Topology graph has already been discovered ..."
clone(locals_span) -> owned vector<LocalTopology>                       // stages mutate the per-port Direction in place

// STEP 2-4: polarity (only on 2-D parts)
if (IciLinkPolarityAssigner::IsPolarizationNeeded(tpu_type)):           // 0x1fc0d7a0 — binary search kTpusWith2dSlices
    IciLinkPolarityAssigner::Assign(target, locals):                    // 0x1fc0d880
        Init()                                                          // bucket links by (Chip, Orientation)
        seed = ChooseSeed()                                             // first chip forming a 2x2 square
        BreadthFirstWalk(seed):                                         // propagate +/- across bidirectional pairs
            AssignOrVerifyPolarity(chip)                                // opposite signs on the two endpoints of a link
            UpdatePolarizedLocalConnectivity(chip)                      // rewrite Orientation -> signed Direction
    locals = GetUpdatedLocals()                                         // fresh vector, every port now signed

// STEP 5: link discovery + validation
IciDiscoverer::Init(target, locals); IciDiscoverer::Discover(target, &chip_to_coord, locals)  // 0x1fc09d40 / 0x1fc0b720
// STEP 6: coordinate BFS
ChipCoordinatesAssigner::Init(target, locals); ::Assign(); ::BreadthFirstWalk()               // 0x1fc00580 / 0x1fc02040
// STEP 7: validation
TopologyFaultVerifier::Verify(); TrayShapeChecker::Check()
install ResilientToroidalTopology on Master+152
```

### 2.1 Polarity gate — `IsPolarizationNeeded`

`IciLinkPolarityAssigner::IsPolarizationNeeded(TpuType)` @`0x1fc0d7a0` does a binary search over a sorted `.rodata` array `kTpusWith2dSlices`. It returns true only for TPU generations whose slice is a 2-D torus, where the firmware reports each port's **axis but not its sign**. On 3-D parts the cable IDs carry the sign explicitly and the entire polarity pass is skipped.

> **WHY —** orientation is symmetric: the firmware knows a port is on the X axis but cannot say which end is `X+`. The only structure that pins the sign uniquely is a *closed loop*. A 2×2 square in the orientation graph has exactly one consistent sign assignment (X+ on one edge, X− on the opposite; likewise Y), so the assigner searches for a square seed and propagates.

### 2.2 Square-seed + BFS polarity (`ChooseSeed` / `BreadthFirstWalk`)

`ChooseSeed` @`0x1fc10cc0` scans the slice's chips; for each it calls `FindCorners(loc)` @`0x1fc11fa0`, which uses `FindLinksByOrientation` @`0x1fc15140` to pull the chip's X-axis and Y-axis links and looks for a pair `(x_link, y_link)` whose two remotes converge on a single fourth chip — a 2×2 square. The first chip that forms a square is the seed; if none exists:

```text
"2D slice's ICI link polarity assignment fails because no seed chip is found that forms a square"
    -> FAILED_PRECONDITION   (ici_link_polarity_assigner.cc:258)
```

`BreadthFirstWalk(seed)` @`0x1fc11040` enqueues the seed, calls `AssignOrVerifyPolarity(seed, is_seed=true)` @`0x1fc129a0` (fixing the four seed polarities by convention), then for each subsequent chip calls `FindMinimalSetOfLinksToPolarize` @`0x1fc15620` (infer this chip's signs from already-polarized neighbours — opposite sign on the two endpoints of every link), `AssignOrVerifyPolarity(chip, false)`, and `UpdatePolarizedLocalConnectivity(chip)` @`0x1fc13be0` (rewrite each port's record from raw `Orientation` to signed `Direction`). A revisit from a second path that disagrees emits:

```text
"ICI link <%s, %s> has assigned polarity which is invalid pre-condition for assigning polarities"
    -> INTERNAL
```

After the walk every port has a fully resolved `Direction` (axis + sign), every bidirectional pair has opposite signs at its two ends, and no port has `UNKNOWN` polarity.

### 2.3 Link discovery + reverse-counterpart check (`IciDiscoverer`)

> **[CONFIRMED]** `IciDiscoverer::Discover` @`0x1fc0b720` signature takes `(ToroidalTopologyInterface&, flat_hash_map<asic_sw::ChipLocation, superpod::routing::Coordinates>*, Span<LocalTopology>)` — i.e. its second argument is the **coordinate↔chip map**, confirming the discoverer writes directly into the map later read by id-assignment.

`IciDiscoverer::Init` @`0x1fc09d40` walks every chip × port. For each port it resolves the remote name (`LocalTopology::GetRemoteName` @`0x1ffdd1a0`) and:

- **Loopback / unconnected** — if no remote: when `is_data_layer_connected == 0` it logs `VLog(1) "ICI port %s does not have a remote port connected. Ignoring this link for coordinate assignment."` (silently dropped); otherwise `"ICI port %s is incorrectly left in loopback mode. Ignoring this link for ICI links discovery."` (also dropped).
- **Unknown axis/sign** — `Orientation == UNKNOWN` → `"ICI link <%s, %s> has unknown orientation which is invalid"` (`ici_discoverer.cc:76`); `Polarity == UNKNOWN` → `"...has unknown polarity which is invalid"` (`ici_discoverer.cc:82`). Both INVALID_ARGUMENT.
- **Good link** — build a `PhysicalIciLink{local_chip, local_port, remote_chip, remote_port, polarity, orientation, is_high_latency}` and insert into a `map<ChipLocation, map<Direction, PhysicalIciLink>>` keyed first on local chip then on signed `Direction`. A duplicate `ChipLocation` is rejected: `"Chip %s is not unique during ICI links discovery for topology %s."` (`ici_discoverer.cc:39`, INVALID_ARGUMENT).

A second pass validates every `(chip, direction)` against its reverse: look up the remote chip, compute `Direction::Opposite(direction)`, and confirm the remote carries a link back with `remote_chip == us` and `remote_port == our local_port`. On mismatch:

```text
"Bidirectional ICI link <%s, %s> with direction %c does not have a reverse counterpart during discovery."
    -> INTERNAL   (ici_discoverer.cc:121)
```

Finally the **node count** must match the intent: `target.GetTopologySize() == locals.size()`, else `"Topology intent %s with %d nodes does not match the slice's chip-local ICI connectivity input which has %d nodes"` (`ici_discoverer.cc:139`, FAILED_PRECONDITION).

---

## 3. The coordinate↔chip map — BFS from the origin

`ChipCoordinatesAssigner::Init` @`0x1fc00580` re-validates the link set and additionally tracks a `flat_hash_set<string>` of seen names to catch hostname collisions distinct from the `ChipLocation` check (`"Chip %s is not unique during node coordinate assignment for topology %s."`, `chip_coordinates_assigner.cc:91`). The heart is `BreadthFirstWalk` @`0x1fc02040`.

> **[CONFIRMED]** `ChipCoordinatesAssigner::BreadthFirstWalk` @`0x1fc02040`: uses a `std::deque<asic_sw::ChipLocation>` at `this+80` — `emplace_back` the origin, `pop_front` the current chip, then for each direction call `FindNeighbor` @`0x1fc03ba0` and `AssignOrVerifyCoordinates` @`0x1fc03da0`. The deque-driven BFS is exactly as reconstructed below.

```c
// ChipCoordinatesAssigner::BreadthFirstWalk()   // 0x1fc02040
dirs = target.GetAllDirections()                    // 4 entries on 2-D, 6 on 3-D
deque.emplace_back(origin)                          // origin seeded from assigner+56 (the first chip of the discovered link set)
chip_to_coord_[origin] = Coordinates(0,0,0)         // chip_to_coord_ at assigner+24
while (!deque.empty()):
    cur = deque.pop_front()
    for d in dirs:
        neighbor = FindNeighbor(cur, d)             // 0x1fc03ba0 — no neighbor => NOT_FOUND (cc:293)
        AssignOrVerifyCoordinates(cur, neighbor, d) // 0x1fc03da0
            if neighbor in chip_to_coord_: VerifyCoordinateConsistency(...)        // 0x1fc04ba0
            else:
                neighbor_coord = cur_coord + target.GetCoordinateOffset(d)         // topology supplies the offset
                chip_to_coord_[neighbor] = neighbor_coord                          // VLog(3) "Assigned %s with coordinate %s."
                deque.emplace_back(neighbor)
NormalizeChipPositions()                            // 0x1fc02ca0 — shift origin to (0,0,0)
(optional) RotateToroidalTopology()                 // 0x1fc040a0 — axis_swap from TopologyDiscoveryOption
```

**Coordinate type.** `superpod::routing::Coordinates` (ctor `Coordinates(array<int,6>&, int dims)` @`0x20c0b3c0`) is `struct{ int32 dimensions_; int32 values_[6]; }`, supporting 1..6 dimensions (`kMaxDimensionSize = 6`), defaulting to 3 for the `kIciXMinus..kIciZPlus` enum.

**Per-direction offset.** The discoverer never hard-codes `X+ = (+1,0,0)`; it asks `ToroidalTopologyInterface::GetCoordinateOffset(d)`. The natural unit offsets are:

```text
kIciXPlus  -> (+1, 0, 0)     kIciYPlus  -> ( 0,+1, 0)     kIciZPlus  -> ( 0, 0,+1)
kIciXMinus -> (-1, 0, 0)     kIciYMinus -> ( 0,-1, 0)     kIciZMinus -> ( 0, 0,-1)
```

…but for a twisted torus the topology object can inject a cross-axis delta at the wrap boundary (e.g. `Z+` wrapping to `(-1,0,+1)` for `twist=1`). Discovery stays twist-agnostic by delegating to the topology — see **[Twisted Torus](../twist/overview.md)**.

### 3.1 Conflict detection — `VerifyCoordinateConsistency`

`VerifyCoordinateConsistency` @`0x1fc04ba0` is what makes the map well-formed. When BFS reaches an already-coordinated chip from a new direction, it computes `(claimed_neighbor_coord − cur_coord)`, reduces it modulo the topology's per-axis size (`GetTopologySizeByDim(i)`), and demands it equal the unit offset for `d`. Mismatch:

```text
"Discovered conflicting cartesian coordinates assignment viewing from different paths in the slice's
 toroidal ICI network: chip %s (coordinate %s) tries to assign chip %s with coordinate %s along
 direction %c, whereas it has existing assigned coordinate %s."
    -> INVALID_ARGUMENT   (chip_coordinates_assigner.cc:325)
```

This is the classic discovery-time failure — a miscabled torus where two paths disagree on a chip's position. A missing required direction instead surfaces from `FindNeighbor`:

```text
"ICI link direction %c is not eligible for topology discovery from chip %s"
    -> NOT_FOUND   (chip_coordinates_assigner.cc:293)
```

### 3.2 Disconnection, normalization, rotation

After BFS, if `|chip_to_coord_| < target node count`, the unvisited names are collected and reported: `"Coordinate assignment failed ... because there are chips disconnected from the rest of the slice: %s."` (`chip_coordinates_assigner.cc:262`, FAILED_PRECONDITION).

> **GOTCHA —** the origin is **not** necessarily a corner. The BFS seeds at the first chip of the discovered link set (read from `assigner+56`, sourced from the locals during `Init`), which may sit in the middle of the slice, so coordinates can go negative mid-walk. `NormalizeChipPositions` @`0x1fc02ca0` computes the component-wise minimum across the whole map and shifts every coordinate so the lower corner is `(0,0,0)`. A reimplementer who assumes the origin is the lower corner will mis-derive chip ids (§4), which are ordered over the *normalized* coordinates.

`RotateToroidalTopology` @`0x1fc040a0` optionally rotates the whole coordinate set (X→Y→Z) if an `axis_swap` was passed via `TopologyDiscoveryOption`.

### 3.3 Step 7 — fault and tray validation

`TopologyFaultVerifier::Verify` @`0x1fc0c6c0` compares the links the discoverer marked `is_fault=true` (ports the target shape expects but observed-disconnected) against the accepted fault pattern in `TopologyDiscoveryOption`. `TrayShapeChecker::Check` @`0x1fc0d000` then validates the discovered `ChipLocation` set against the host's expected tray layout (e.g. a per-host 2×2 / 4×4 mini-mesh) via `CheckMultiTrayShape` @`0x1fc0d320`. Both pass before the `ResilientToroidalTopology` is installed at `Master+152`.

> **NOTE — fault injection.** `Master::DiscoverTopology` reads the string flag `tpu_slice_builder_topology_discovery_fault_injection`, parses it through `Orientation_descriptor` + `proto2::internal::ParseNamedEnum`, and on a valid value calls `InjectIciResilientFaults(...)` with the topology, the locals slot (`Master+776`), the coordinate/id slots (`Master+832`/`+840`), the parsed orientation, and a sentinel `Coordinates`, then re-runs the discovery lambda over the injected topology. This is the only fault knob on the production path; an invalid string yields `"Invalid input fault injection dimension %s"` (`master.cc:680`, INVALID_ARGUMENT). Everything else in discovery is deterministic.

---

## 4. Global chip-id assignment — coordinate map → dense id → chip

Once the coordinate↔chip map is installed, the slice still needs a dense integer id `0..N-1` per chip (this is what collectives, routing, and firmware index by). The `Master` derives the id from the validated coordinates with a Cartesian ordering — **X varies fastest, then Y, then Z** — and pushes it per worker via `Master::SetGlobalChipId(string_view name, Stub*)` @`0x1fbbe7e0`.

> **[CONFIRMED]** `Master::SetGlobalChipId` @`0x1fbbe7e0` decompile: `lock_shared(Master+760)` → look up the worker's chip set in the `flat_hash_map<string, flat_hash_set<ChipLocation>>` at `Master+800` → for each owned chip, `find<ChipLocation>` in the `flat_hash_map<ChipLocation, Coordinates>` at **`Master+832`** (the coordinate↔chip map) → call **vtable slot +144** on the `ResilientToroidalTopology` at `Master+152` to obtain that chip's id (returned as a `StatusOr<int>`) → emit a `SetGlobalChipIdRequest_ChipLocationToId{ChipLocation (via ToProto), int id}` → issue the gRPC call via the stub vtable. Confirmed exactly against the binary.

```c
// Master::SetGlobalChipId(worker_name, stub)   // 0x1fbbe7e0
lock_shared(Master+760)
chips = (*flat_hash_map<string, set<ChipLocation>>)(Master+800)[worker_name]   // worker's owned chips
req = SetGlobalChipIdRequest{}
for chip in chips:
    if !chip_to_coord_.contains(chip):   continue                              // chip_to_coord_ at Master+832
    id_or = topology_at(Master+152)->GetGlobalChipId(chip)                     // vtable slot +144 -> StatusOr<int>
    if (!id_or.ok()):
        return "Failed to get global ID for chip <name> at <coordinate>"       // builds coordinate via ToString
    entry = req.add_entries()                                                  // ChipLocationToId
    entry.chip_location = chip.ToProto()
    entry.chip_id       = id_or.value()
if (no entry added):
    return NOT_FOUND "Failed to find any global ID for chips on worker <name>" // master.cc:1065, MakeErrorImpl<5>
unlock_shared(Master+760)
stub->SetGlobalChipId(ctx, req, &reply)                                        // master.cc:1077 wraps gRPC failure
```

The worker-side handler `Worker::SetGlobalChipId` ultimately drives the chip-local driver `SliceConfiguration::SetSliceChipId(int)`, which writes the id into chip-local firmware (the driver layer also exposes `QueueBasedGlobalConfig::SetGlobalChipId(int)` / `pxc::GlobalConfig::SetGlobalChipId(int)`).

> **QUIRK — the id is not stored in a separate map.** A natural design would keep a `flat_hash_map<ChipLocation, int> chip_id_map_`. The binary shows otherwise: `Master+832` holds the **coordinate** map (`ChipLocation → Coordinates`), and the integer id is produced on demand by the topology object's vtable slot +144 from the chip's coordinate. There is no standalone id table on the `Master`; the topology is the single source of truth for both coordinate and id.

### 4.1 Companion — `SetChipCoordinates`

Separately, the `Master` pushes the raw `(X,Y,Z)` coordinate to each chip via `Master::SetChipCoordinates` @`0x1fbc4640` → `WorkerService::SetChipCoordinates` @`0x1fc3db80`. It guards on the chip map being initialised first:

```text
"chip_map_ has not been initialized.Did DiscoverTopology execute correctly?"   (master.cc:1367)
```

— the explicit ordering contract: discovery must populate the coordinate map before either ids or coordinates can be shipped.

> **NOTE — ordering.** `DiscoverTopology` (coordinates) → `SetGlobalChipId` (dense ids) → routing-table generation/`SetRoutingTable` → `SetChipCoordinates`. Coordinates feed ids; both feed routing. The full step-by-step `Master::InitSlice` sequence is on **[Link Bring-Up](link-bringup.md)**.

---

## 5. Failure catalog

All discovery-time diagnostics, their status codes, and source lines, collected from `.rodata`. Routing-time and recovery semantics are owned by **[Route-Table Generation](../routing/route-table-generation.md)** and **[Failure Recovery](failure-recovery.md)**.

| Stage | Message (abridged) | Status | Source |
|---|---|---|---|
| Discovery wrapper | re-discover: `"Topology graph has already been discovered ..."` | FAILED_PRECONDITION | `topology_discoverer.cc:69` |
| Discovery wrapper | `"Failed to discover ICI network topology"` (wraps inner) | (propagated) | `master.cc:668` |
| Fault inject | `"Invalid input fault injection dimension %s"` | INVALID_ARGUMENT | `master.cc:680` |
| Polarity | `"2D slice's ICI link polarity assignment fails because no seed chip ... forms a square"` | FAILED_PRECONDITION | `ici_link_polarity_assigner.cc:258` |
| Polarity | `"ICI link <%s, %s> has assigned polarity which is invalid pre-condition ..."` | INTERNAL | `ici_link_polarity_assigner.cc` |
| Polarity | `"Expected topology size %d does not match observed number of local ICI connectivity metadata %d."` | FAILED_PRECONDITION | `ici_link_polarity_assigner.cc:64` |
| Link discovery | `"Chip %s is not unique during ICI links discovery for topology %s."` | INVALID_ARGUMENT | `ici_discoverer.cc:39` |
| Link discovery | `"ICI link <%s, %s> has unknown orientation which is invalid"` | INVALID_ARGUMENT | `ici_discoverer.cc:76` |
| Link discovery | `"ICI link <%s, %s> has unknown polarity which is invalid"` | INVALID_ARGUMENT | `ici_discoverer.cc:82` |
| Link discovery | `"Bidirectional ICI link ... does not have a reverse counterpart during discovery."` | INTERNAL | `ici_discoverer.cc:121` |
| Link discovery | `"Topology intent %s with %d nodes does not match ... which has %d nodes"` | FAILED_PRECONDITION | `ici_discoverer.cc:139` |
| Coordinates | `"Chip %s is not unique during node coordinate assignment for topology %s."` | INVALID_ARGUMENT | `chip_coordinates_assigner.cc:91` |
| Coordinates | `"ICI link direction %c is not eligible for topology discovery from chip %s"` | NOT_FOUND | `chip_coordinates_assigner.cc:293` |
| Coordinates | `"Discovered conflicting cartesian coordinates assignment ..."` | INVALID_ARGUMENT | `chip_coordinates_assigner.cc:325` |
| Coordinates | `"Coordinate assignment failed ... chips disconnected from the rest of the slice: %s."` | FAILED_PRECONDITION | `chip_coordinates_assigner.cc:262` |
| Id push | `"Failed to get global ID for chip <name> at <coord>"` | (propagated) | `master.cc` |
| Id push | `"Failed to find any global ID for chips on worker <name>"` | NOT_FOUND | `master.cc:1065` |
| Coord push | `"chip_map_ has not been initialized.Did DiscoverTopology execute correctly?"` | FAILED_PRECONDITION | `master.cc:1367` |

VLog-only (silently dropped, not errors): `"ICI port %s is incorrectly left in loopback mode. ..."`, `"ICI port %s does not have a remote port connected. ..."`, `"ICI link <%s ,%s> is ignored by discovery because it is not connected to another chip in this slice."`.

---

## 6. Verification notes

> **[CONFIRMED]** Cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - `Master::DiscoverTopology` @`0x1fbbe4e0`: inner `$_0` lambda invocation (passing `Master+152` topology and `Master+776`/`+784` locals begin/end into the discoverer at `Master+224`), the 3-dim `Coordinates` built from a `.rodata` constant `{4,4,4}`, the `!= 1` error at `master.cc:668`, the `tpu_slice_builder_topology_discovery_fault_injection` flag read, `Orientation_descriptor` + `ParseNamedEnum`, `InjectIciResilientFaults(...)`, and the `master.cc:680` invalid-dimension error — all present and matched.
> - `TopologyDiscoverer::Discover` @`0x1fbff7e0` and `LegacyTopologyDiscoverer::Discover` @`0x213dcfe0`: both present with the exact `(ToroidalTopologyInterface&, Span<LocalTopology>, TopologyDiscoveryOption&)` signature.
> - `IciDiscoverer::Discover` @`0x1fc0b720`: second argument confirmed as `flat_hash_map<asic_sw::ChipLocation, superpod::routing::Coordinates>*` (the coordinate↔chip map).
> - `ChipCoordinatesAssigner::BreadthFirstWalk` @`0x1fc02040`: `std::deque<ChipLocation>` at `this+80`, `emplace_back`/`pop_front`/`FindNeighbor`/`AssignOrVerifyCoordinates` — the deque-driven BFS confirmed.
> - `Master::SetGlobalChipId` @`0x1fbbe7e0`: `lock_shared(+760)`, worker-chip-set map at `+800`, coordinate map at `+832`, topology vtable slot +144 for the id, `ChipLocationToId` request build, the `master.cc:1065` NOT_FOUND, and the gRPC dispatch at `master.cc:1077` — confirmed exactly.
> - `superpod::routing::IciLinkPolarityAssigner` confirmed under the `superpod::routing` namespace (`ChooseSeed` @`0x1fc10cc0`, `Init` @`0x1fc0db40`), as the raw source path implied.
>
> **[LOW]** Numeric protobuf field tags for `LocalTopology`/`PortEntry` (names + serialization order are HIGH; tags require decoding the linked `FileDescriptorProto`). The numeric value assignment of the `Direction` enum (`kIciXPlus = ?`) — string anchors confirm the value→name set, but the integer ordering is produced at runtime by `NameOfDenseEnum<&Direction_descriptor>` and is not a `.rodata` literal. The BFS origin `ChipLocation` (read from `assigner+56`) derives from the first chip of the discovered link set; the exact selection rule the assigner's `Init` uses is not fully decompiled in this pass. The exact bodies of `FindMinimalSetOfLinksToPolarize` @`0x1fc15620` and `GetCoordinateOffset` per topology shape (`TwistedTorus` etc.) were not individually decompiled in this pass.

---

## Cross-References

**ICI section pages**
- [ICI Overview](overview.md) — section map; where discovery sits in the 16-step `Master::InitSlice` bring-up
- [Link Bring-Up](link-bringup.md) — firmware PHY / host DL split, the per-phase `InitSlice` RPC table, `GetLocalTopology` collection (phase 1, the "probe exchange")
- [DMA Descriptor](dma-descriptor.md) — the remote-write descriptor the discovered/ routed torus carries
- [Failure Recovery](failure-recovery.md) — `SliceFailureType`, `FailDevice` cascade, `LinksDownReset` (where discovery errors land)

**Sibling sections**
- [Route-Table Generation](../routing/route-table-generation.md) — consumes the coordinate↔chip map + direction↔port map produced here
- [Routing Overview](../routing/overview.md) — `(src,dst) → link path`, static dimension-order routing on the discovered torus
- [Twisted Torus](../twist/overview.md) — supplies `GetCoordinateOffset` (with twist delta) that the coordinate BFS queries
- [back to index](../index.md)
