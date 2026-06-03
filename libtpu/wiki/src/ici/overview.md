# Inter-Chip Interconnect — Section Map

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`; `.text` VMA == file offset). All symbols below are present in the full-symbol binary; demangled names and addresses are cross-checked against the IDA decompile.

## Abstract

This page is the **map of the ICI (Inter-Chip Interconnect) subsystem** — the physical fabric that wires TPU chips into a 3-D torus inside a pod-slice, and the host/firmware machinery that brings it up, discovers its shape, and moves bytes across it. ICI sits one level below the collective stack: a collective decides "this core reduces with those cores," routing decides "out which link," and ICI is what carries the flits. Every chip exposes **four physical SerDes ports** (`LINK0..LINK3`, confirmed by the `MGT_USER_ICI_LINK[0-3]_STALLS_*` MMIO counter set); those four ports map onto up to **six logical torus directions** (`kIciXPlus, kIciXMinus, kIciYPlus, kIciYMinus, kIciZPlus, kIciZMinus`) by the per-chip routing table. A 2-D slice uses four directions (X±, Y±); a 3-D part uses all six.

The subsystem has a **two-level control plane** that a reimplementer must keep distinct. The slice-wide controller `accel_ssw::deepsea::slice_builder::Master` (one per pod-slice) owns the global ordering — discover, assign IDs, install routing tables, install GTC, enable ICI on every chip, wait for data-link-up, broadcast slice info, set coordinates. It drives this as a sequence of `ExecuteOnAllWorkers` gRPC fan-outs. Beneath it, the chip-local driver state machine `asic_sw::driver::deepsea::ici::SliceConfiguration` (one per owned chip) acts on per-worker commands and talks to the hardware via `IciControl`/`Ici`. The Cloud deployment inserts a `tpunetd` daemon between the two; the message shapes and phase order are identical, only the transport changes.

This page documents (1) the **ICI link model** — four SerDes ports, six logical directions, the firmware/host split for PHY and data-link bring-up; (2) the **bring-up → discovery → transfer flow** — the 16-step `Master::InitSlice` sequence, the seven-step topology-discovery graph inference, and where the ICI DMA descriptor and the all-reduce primitive plug in; and (3) the **per-generation link count and resource model** at a glance. Each topic — link bring-up, topology discovery, the DMA descriptor, the all-reduce primitive, failure recovery, and VC balance — has its own sibling **ici page**; collectives, routing, twist, and megascale are sibling **sections**. This page links them and does not duplicate their byte-level derivations.

For reimplementation, the contract of the ICI subsystem is:

- The **link model**: 4 SerDes ports per chip, ≤6 logical torus directions, PHY training is **firmware-owned** (host only sets `enable_ici_serdes_training` and polls a per-port `port_ready_state`), data-link layer is **host/driver-owned** (the `IciControl::WaitForLinksUp` poll loop).
- The **bring-up state machine**: `Master::InitSlice` @`0x1fbbaac0` runs 16 ordered steps, ~11 of them `ExecuteOnAllWorkers` gRPC fan-outs to `SliceBuilderWorkerService`; the others are local (locked) or sequential.
- The **discovery model**: topology is **pure graph inference** over firmware-supplied per-port connectivity — no active discovery-time probe; polarity is assigned from a square seed (2-D) or cable IDs (3-D), then Cartesian coordinates are propagated by BFS from chip (0,0,0).
- The **transfer model**: the all-reduce is a colored-ring reduce-scatter + all-gather; the reduction op is **never on the wire** (every link DMA is a plain `DMA_TYPE_REMOTE_WRITE_UNICAST` with a remote sync-flag bump), the reduction runs locally on the TensorCore VPU.

| | |
|---|---|
| **Slice controller** | `Master::InitSlice` @`0x1fbbaac0` (`accel_ssw::deepsea::slice_builder`) |
| **Chip-local driver** | `ici::SliceConfiguration` (modern) / `jxc::SliceConfiguration` (Jellyfish legacy) |
| **Physical ports per chip** | **4** SerDes (`LINK0..LINK3`) — confirmed by the MGT stall-counter set |
| **Logical directions** | ≤**6** (`kIciX/Y/Z {Plus,Minus}`); 2-D slice = 4, 3-D part = 6 |
| **Topology discovery** | `Master::DiscoverTopology` @`0x1fbbe4e0` → `TopologyDiscoverer::Discover` @`0x1fbff7e0` |
| **DL-up poll loop** | `IciControl::WaitForLinksUp` @`0xe7b1060` (1 ms quantum `0x3D0901`, 500 ms `0x77359401` fallback) |
| **All-reduce emitter** | `AllReduceEmitter::EmitAllReduce` @`0x13742200`; strategy picker `BaseStrategyND::SelectNDStrategy` @`0x137c78e0` |
| **Routing model** | Static, per-chip, dimension-order on a 3-D (twisted) torus; no runtime reroute |

---

## 1. The ICI link model

The hardware unit is a **4-port SerDes** per chip. Each port runs an NRZ/PAM-4 link (PAM-4 on Jellyfish JFC/DFC and newer; NRZ on older Pufferfish) and connects to one neighbor chip — intra-host (same tray) or inter-tray (longer copper/optical, flagged `is_high_latency`). The four ports carry the torus: a chip's `(direction → port)` assignment is established once, at bring-up, and never moves.

### 1.1 Four ports, six directions

The 4 physical ports map onto up to 6 logical torus directions. The 6-direction `Direction` enum is the union of an `Orientation` (axis: `X=1, Y=2, Z=3`) and a `Polarity` (`POSITIVE=1, NEGATIVE=2`):

```text
Direction   = Orientation × Polarity
kIciXPlus   = (X, POSITIVE)     coordinate offset (+1, 0, 0)
kIciXMinus  = (X, NEGATIVE)                       (-1, 0, 0)
kIciYPlus   = (Y, POSITIVE)                       ( 0,+1, 0)
kIciYMinus  = (Y, NEGATIVE)                       ( 0,-1, 0)
kIciZPlus   = (Z, POSITIVE)                       ( 0, 0,+1)
kIciZMinus  = (Z, NEGATIVE)                       ( 0, 0,-1)
```

A 2-D slice populates four of these (X±, Y±); a 3-D part populates all six. Whether a port carries `Z` at all is a function of the assembled cabling, which firmware exposes per-port via the `ChipConnectorInfo` register set. `Direction::Opposite` maps each value to its sign-flipped counterpart (X+ ↔ X-, etc.) — the invariant that every bidirectional link checks during discovery (§3). The per-direction coordinate offset is **not** hard-coded in the discoverer; it asks the topology object via `ToroidalTopologyInterface::GetCoordinateOffset`, so a twisted torus can inject a non-zero cross-axis delta at the wrap boundary (see [Twisted Torus](../twist/overview.md)).

> **QUIRK —** the 6-direction `Direction` enum is the logical model; the hardware has only 4 ports. A reimplementation that assumes 6 ports per chip is wrong: the routing table is what fans 4 physical ports out to ≤6 logical directions, and a 3-D torus on a 4-port part is realized by port aggregation, not six discrete cables.

### 1.2 The firmware/host split

ICI bring-up is split across two owners, and the seam matters:

- **PHY layer (firmware-owned).** SerDes calibration, adaptive equalization, lane lock, 64b/66b alignment all run on the chip's embedded core. The host has **no software hook** into the analog PHY. It only writes `enable_ici_serdes_training` (plus `ignore_external_ici_ports` and a `disabled_serdes_index` mask) through `ConfigureIci`/`EnableIciPorts` (`jfc::Ici::EnableIciPorts` @`0xe7accc0`, `dfc::Ici::EnableIciPorts` @`0xe76e980`), then observes progress through a single per-port 3-bit `cm_scratch_user_firmware::link_stack_ready_state::port_ready_state` field.
- **Data-link layer (host/driver-owned).** Once firmware reports a per-port ready code, the driver drives the DL state machine (`IciControl`, `Ici::ChangeStateLocked`) and the slice-wide `Master` orders it. The firmware's 8-valued `port_ready_state` is remapped to a 7-valued software `LinkStackReadyState` enum via an 8-entry table at `0xe7b6400` (confirmed in the decompile: the `WaitForLinksUp` body calls `proto2::internal::NameOfDenseEnum<&LinkStackReadyState_descriptor, 0, 7>`).

The full PHY-training detail, the 7-value enum, and the per-port DL-state array are in **[Link Bring-Up](link-bringup.md)**.

> **GOTCHA —** the string `"ICI Probe failed. local port: %d name: %s took %d us..."` is **not** a discovery-time probe. It is a *post-bring-up* health-check probe issued by the `LinkChecker` path (which also emits `"LinkChecker reports a physical ICI down at %s port %d."`). Discovery itself sends no active probe — see §3.

---

## 2. The bring-up → discovery → transfer flow

ICI goes from cold links to live collective traffic in three movements: bring-up (firmware PHY + host DL), discovery (graph inference of the torus shape and routing), and transfer (DMA descriptors driven by the collective emitters). The flow below is the spine of the whole section.

```text
                 ┌─────────────────────── SLICE CONTROLLER (Master::InitSlice @0x1fbbaac0) ───────────────────────┐
[bring-up]       │  1  GetLocalTopology         (fanout)   per-worker links over gRPC                              │
                 │  2  DiscoverTopology         (local)    fold locals → global toroidal  ── §3                    │
                 │  3  SetGlobalChipId          (fanout)   Cartesian-ordered chip-id map                           │
                 │  4  Generate routing tables  (local)    RoutingTableGeneratorFactory  ── ../routing             │
                 │  5  DetectRoutingTableDeadlock (gated)   channel-dependency cycle check (if this+0x90)           │
                 │  6  SetRoutingTable          (fanout)    install per-link ICR tables                             │
                 │  7  Generate GTC tree        (local)     global-time-counter root/leaf                          │
                 │  8  SetGtcConfiguration      (fanout)                                                            │
                 │  9  ControlIciErrorReport    (fanout)    mask bring-up errors                                    │
                 │ 10  EnableIciDataLink        (fanout)    PHY + DL training kick-off                              │
                 │ 11  WaitForDataLinkUp        (sequential) per-chip DL-up poll  ── IciControl::WaitForLinksUp     │
                 │ 12  ClearGlobalGtc / 13 WaitForGtcReset  (sequential) GTC resync                                 │
                 │ 14  SetChipCoordinates       (fanout)    push (X,Y,Z) per chip                                   │
                 │ 15  BroadcastSliceInformation / 16 DisableIciInterrupts (sequential)                             │
                 └──────────────────────────────────────────────────────────────────────────────────────────────┘
                                                          │
[discovery]   Master::DiscoverTopology @0x1fbbe4e0 → TopologyDiscoverer::Discover @0x1fbff7e0 (7-step graph inference)
                  polarity (square seed / cable IDs) → BFS coordinates from (0,0,0) → reverse-counterpart validation
                  → ResilientToroidalTopology installed on Master+152
                                                          │
[transfer]    HLO collective op → collective strategy (../collectives) → route table/schedule (../routing)
                  → ICI DMA descriptor (per-family DmaDescriptorState) → 4-SerDes-port flits → remote sync-flag bump
```

Steps 1–16 are the bring-up sequence reconstructed from `Master::InitSlice` (the decompile shows 11 `ExecuteOnAllWorkers` fan-out sites plus the `DiscoverTopology`, `SetGlobalChipId`, `SetRoutingTable`, `SetGtcConfiguration`, `ControlIciErrorReport`, `EnableIciDataLink`, and `DetectRoutingTableDeadlock` sub-calls). The full per-phase RPC table, deadlines, and exit conditions live in **[Link Bring-Up](link-bringup.md)**. Routing-table generation (step 4) and installation (step 6) are owned by the **[Routing](../routing/overview.md)** section.

> **NOTE —** discovery (step 2) runs *before* the data-link is enabled on the slice-wide path (step 10), because phase 1 already collected each chip's firmware-resolved neighbor info at the chip's own PHY/DL-up time. Phase 1 **is** the "probe exchange": the firmware's per-port "who is on the other end" field, shipped to the Master in the `LocalTopology` proto, is the probe response. There is no separate active probe at slice-discovery time.

### 2.1 Where each ici page plugs in

| Stage | Owner page | Section |
|---|---|---|
| Firmware PHY + host DL bring-up, the 16-step `InitSlice` sequence | [Link Bring-Up](link-bringup.md) | this section |
| Polarity + coordinate inference, `LocalTopology` wire format | [Topology Discovery](topology-discovery.md) | this section |
| Per-family DMA descriptor word layout, remote sync-flag encoding | [DMA Descriptor](dma-descriptor.md) | this section |
| Colored-ring reduce-scatter + all-gather, strategy families | [All-Reduce Primitive](all-reduce-primitive.md) | this section |
| `SliceFailureType`, `LinksDownReset`, `FailDevice` cascade | [Failure Recovery](failure-recovery.md) | this section |
| `IciResource → ResourceVector` slot mapping, VC merge | [VC Balance / Allocation](vc-balance-allocation.md) | this section |
| Replica groups → per-color ring schedule | [Collectives](../collectives/overview.md) | sibling |
| `(src,dst) → link path`, route table vs net_router schedule | [Routing](../routing/overview.md) | sibling |
| Twisted-torus geometry, coordinate-offset twist | [Twisted Torus](../twist/overview.md) | sibling |
| Cross-slice topology stitching | [Megascale](../megascale/overview.md) | sibling |

---

## 3. Topology discovery — graph inference, not probing

Once data-link is up on every port, `Master::DiscoverTopology` @`0x1fbbe4e0` folds each worker's `LocalTopology` (collected in phase 1) into a global toroidal topology. The modern path runs the composite `TopologyDiscoverer` (ctor @`0x1fbff680`, `Discover` @`0x1fbff7e0`) holding five sub-objects — `IciLinkPolarityAssigner`, `ChipCoordinatesAssigner`, `IciDiscoverer`, `TopologyFaultVerifier`, `TrayShapeChecker` — gated by `--tpu_slice_builder_topology_discovery_new_module`; the `LegacyTopologyDiscoverer` @`0x213dcfe0` is the fallback and adds a `SliceReshaper` step but produces the same `ResilientToroidalTopology`.

Discovery adds four things on top of the firmware's per-port info, in this order:

```c
function TopologyDiscoverer_Discover(locals, target_topology):   // 0x1fbff7e0
    if already_discovered_: return error                          // re-discover guard
    // [polarity] orientation is symmetric (axis only, no sign on 2-D parts)
    if IciLinkPolarityAssigner::IsPolarizationNeeded(tpu_type):   // 0x1fc0d7a0 — binary search kTpusWith2dSlices
        seed = ChooseSeed()                                       // first chip that forms a 4-link square
        BreadthFirstWalk(seed):                                   // propagate +/- signs across bidirectional pairs
            AssignOrVerifyPolarity(chip)                          // opposite signs on the two endpoints of every link
            UpdatePolarizedLocalConnectivity(chip)                // rewrite Orientation → signed Direction
    // [link discovery] build map<Chip, map<Direction, PhysicalIciLink>>
    IciDiscoverer::Init(); IciDiscoverer::Discover()              // 0x1fc09d40 / 0x1fc0b720
        // reject loopback / unconnected ports; for each link verify the remote chip
        // carries Direction::Opposite back to us, else: "...does not have a reverse counterpart..."
        // verify node count == target_topology.GetTopologySize()
    // [coordinates] BFS from origin chip = (0,0,0)
    ChipCoordinatesAssigner::BreadthFirstWalk():                  // 0x1fc02040
        for each Direction d from cur: neighbor_coord = cur_coord + offset_for_direction(d)
        // re-visit from a different path → VerifyCoordinateConsistency (modulo torus size)
        //   mismatch: "Discovered conflicting cartesian coordinates assignment ..."
        NormalizeChipPositions()                                  // shift origin to (0,0,0)
    // [validation] fault pattern + tray shape
    TopologyFaultVerifier::Verify(); TrayShapeChecker::Check()
    install ResilientToroidalTopology on Master+152
```

The polarity stage is the subtle one. On a 2-D slice the firmware tags each port's **axis** but not its **sign**; the assigner finds a chip whose links form a 2×2 square (the only closed loop with a unique consistent sign assignment), fixes its four polarities by convention, and BFS-propagates. On a 3-D part the cable IDs carry the sign explicitly and the polarity pass is skipped. After discovery the slice has a Cartesian `(X,Y,Z)` per chip, a `ChipLocationToCoordinate` map, and the per-chip `Direction → port` map that seeds routing-table generation. The full square-seed heuristic, the `LocalTopology`/`PortEntry` proto layout, the complete failure catalog, and the megascale coordinate handoff are in **[Topology Discovery](topology-discovery.md)**.

> **GOTCHA —** the coordinate origin is *not* necessarily a corner. The BFS seeds at the user's `--xla_jf_ici_origin_chip_location` (or the first chip), which can sit in the middle of the slice, producing negative coordinates mid-walk. `NormalizeChipPositions` shifts the whole map by the component-wise minimum afterwards. A reimplementer who assumes the origin is the lower corner will mis-assign chip IDs (which are derived from the normalized coordinates, X fastest, then Y, then Z).

---

## 4. The transfer layer — DMA descriptor and all-reduce

With the torus discovered and routing installed, collectives move bytes. The unit of transfer is a **remote-DMA descriptor**: the local TensorCore issues a `DMA_TYPE_REMOTE_WRITE_UNICAST` whose source is a local VMEM offset, whose destination is a VMEM offset on the neighbor core, and whose payload carries a **remote sync-flag handle**. When the chunk lands, the receiving NodeFabric Ingress Unit (NIU) auto-increments that remote sync flag — the wire-level `atomic_remote_add_set_done` mechanism. The descriptor word layout is per-generation (`JellyfishDmaDescriptorState` @`0x1d4c9f40`, `PufferfishDmaDescriptorState` @`0x1d5ab540`, plus Ghostlite/Viperfish encoders); granule size is target-dependent (32 B Jellyfish, 64 B newer). Full byte layout in **[DMA Descriptor](dma-descriptor.md)**.

> **QUIRK —** there is **no reduce-on-wire**. Every ICI DMA is a plain unicast write; the reduction op (`SUM/PRODUCT/MIN/MAX`, plus bitwise AND/OR on PRED/U32) is known only to the local accumulator, which runs a VPU `vadd/vmul/vmin/vmax` lambda on its local copy + the just-arrived chunk. The reduction kind is never transmitted. A reimplementer who looks for a reduction-op field in the descriptor will not find one.

### 4.1 The all-reduce primitive at a glance

The all-reduce is a **colored-ring reduce-scatter + all-gather**, not one algorithm. `AllReduceEmitter::EmitAllReduce` @`0x13742200` and `BaseStrategyND::SelectNDStrategy` @`0x137c78e0` pick one of five strategy families on tensor size, color count, topology, cross-module-ness, and prefer-flags. The conceptual decomposition is shared: `ring_size − 1` reduce-scatter steps (each core sends one shard CW, receives one from CCW, accumulates), then `ring_size − 1` all-gather steps. The **3-D torus exploit** is `BaseStrategyND::ComputeColorDimensions` @`0x137c3ba0` (signature confirms a `bitset<3>` axis-usability mask and a `long[6][3]` per-color/per-dimension result): on a 3-D part it runs up to three orthogonal rings concurrently, one per axis, so the rings never share a SerDes port.

| Strategy family | Algorithm | When chosen | Confidence |
|---|---|---|---|
| `BinomialSinglePhaseRingSumEmitter` @`0x13769be0` | binomial tree, log₂(ring) steps | small / latency-bound | HIGH |
| `UniDirection1DRingStrategy` @`0x137d4a20` | 1-D ring, single direction, 2-phase | generic 1-D torus axis | HIGH |
| `UniDirectionNDRingStrategy` @`0x137d4700` | N concurrent per-axis color rings | 2-D/3-D torus decomposition | HIGH |
| `StrategySubgroupND` @`0x137d4c00` | per-subgroup ring then over-rings | hierarchical / cross-module ARS | HIGH |
| rotated- / async-pincer family | bidirectional pincer, overlapped send/recv | mid/large, bandwidth-bound | HIGH |

Supported element types are exactly five — `kSupportedTypes` @`.rodata 0x0ae5a56c` = `{F32=11, S32=4, U32=8, BF16=16, PRED=1}`; anything else is promoted upstream. The quantized-pincer path additionally accepts `{S8, F8E5M2, F8E4M3B11FNUZ}` on the wire. The strategy decision tree, the per-family pincer overlap, dtype/BF16-accumulation gates, the tree-barrier scopes, and VMEM scratch sizing are all in **[All-Reduce Primitive](all-reduce-primitive.md)**. The `EmitAllReduce` decompile cross-confirms the family set (`Pincer`, `UniDirection`, `Binomial`, and `GetRingLocation` all referenced).

---

## 5. Resource model — links, credits, and VC balance

ICI flow control is **not** software-managed on the data path. The compiler emits only sync-flag bookkeeping; per-flit credit handling lives in the NIU and the on-chip switch fabric. The host observes link state only through a fixed MMIO counter set: per port (×4) nine `MGT_USER_ICI_LINK[n]_STALLS_*` categories (the `LST/NOLST × DATA/NODATA × CREDIT/NOCREDIT` cube that distinguishes egress-starved from receiver-back-pressured from idle), eight per-client `MGT_USER_ICI_LINK_ARB_DELAY_*` arbitration counters, and five `MGT_USER_ICI_LINK_XMIT_STALL_THRESHOLD_CNT_NF_CLIENT[n]` NodeFabric thresholds. These four-times-nine stall categories are the binary evidence pinning the **4-port** count.

What the compiler *does* control is the mapping of torus dimensions to scheduler resource slots, so the latency-hiding scheduler can model bidirectional ring contention. `GetResourceFromIciResource` @`0x1c894c00` maps `IciResource ∈ [1..6]` to `ResourceVector` slots `{0xd, 0xe|0xf, 0x10|0x11, 0x12}` = 3 torus dimensions × 2 ring directions; `CalculateBisectionBandwidth` @`0x133ef4c0` walks a `vector<IciResource>` to size the cross-section. On Jellyfish-DF the routing table is augmented by `SetChannelMergeBehavior` @`0xe76c680`, which configures the per-port VC-merge semantics; the optional `DetectRoutingTableDeadlock` pass (step 5) walks the channel-dependency graph for cycles. The full `IciResource` enum, the degraded-axis remap, and the VC-merge/deadlock model are in **[VC Balance / Allocation](vc-balance-allocation.md)**.

---

## 6. Failure recovery at a glance

Bring-up and runtime faults flow through a five-value `SliceFailureType` enum and a `LinksDownReset` recovery RPC. Driver-side, an uncorrectable link IRQ (`Ici::HandleIciLinkInterrupt`) escalates `Ici::SignalDeferredFailure → FailDevice`, which cascades `Driver → TensorNode → BarnaCore → Queue` and surfaces as `SLICE_FAILURE_CHIP_DRIVER_ERROR`. Slice-side, `Master::FailSlice` @`0x1fbc1760` transitions the 4-value `MasterState` toward failing and calls into the `SliceBuilderHelper` handlers. Recovery is `Master::LinksDownReset` @`0x1fbc4c40` → per-worker `SliceConfiguration::LinksDownReset` (turn every non-down link down via firmware, re-collect DL state, clear the enabled-port list). Bring-up errors are masked (`MaskIciErrors`) during PHY training and unmasked after DL-up. The full `SliceFailureType` table, the FailDevice cascade, and the reset state transitions are in **[Failure Recovery](failure-recovery.md)**.

---

## 7. Verification notes

> **[CONFIRMED]** The link model, bring-up entry, discovery entry, link count, and the all-reduce family were cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - `Master::InitSlice` @`0x1fbbaac0`: 11 `ExecuteOnAllWorkers` fan-out sites plus the `DiscoverTopology`, `SetGlobalChipId`, `SetRoutingTable`, `SetGtcConfiguration`, `ControlIciErrorReport`, `EnableIciDataLink`, `DetectRoutingTableDeadlock` sub-calls — the 16-step sequence is consistent.
> - `IciControl::WaitForLinksUp` @`0xe7b1060`: the `0x3D0901` comparand (1 ms quantum), `AbslInternalSleepFor`, `IsLinkUp` + `GetLinkStackReadyState` per link, and `NameOfDenseEnum<&LinkStackReadyState_descriptor, 0, 7>` (7-value enum) — exact.
> - `Master::DiscoverTopology` @`0x1fbbe4e0` and `TopologyDiscoverer::Discover` @`0x1fbff7e0` present; the composite sub-objects and `ResilientToroidalTopology` install are reconstructed from the discovery chain.
> - 4-SerDes-port count: confirmed by the `MGT_USER_ICI_LINK[0-3]_STALLS_*` counter set (4 links × 9 stall categories) in `.rodata`.
> - `AllReduceEmitter::EmitAllReduce` @`0x13742200`: references `Pincer`, `UniDirection`, `Binomial`, `RingLocation`; `BaseStrategyND::ComputeColorDimensions` @`0x137c3ba0` signature carries `bitset<3>` and returns `long[6][3]` (`PA6_A3_l`); `GetResourceFromIciResource` @`0x1c894c00` and `CalculateBisectionBandwidth` @`0x133ef4c0` over `vector<IciResource>` present — exact.
>
> **[LOW]** Per-port `LinkStackReadyState` value names (the 7 enum strings) are emitted at runtime via `NameOfDenseEnum` and not present as `.rodata` literals; the 8→7 firmware-to-software remap table at `0xe7b6400` is recovered numerically but the string names require the `link_stack.proto` descriptor. Per-chip-family physical port counts for the newest GFC/VFC generations are confirmed 4 only for the JXC family (the MGT counter set); GFC/VFC are inferred to match (marked LOW).

---

## Related Components

| Component | Relationship |
|---|---|
| [Collectives](../collectives/overview.md) | The primary consumer — lowers HLO collectives to ICI ring traffic over this fabric |
| [Routing](../routing/overview.md) | Sits between collectives and ICI; turns `(src,dst)` into the per-link path the DMA descriptor rides |
| [Twisted Torus](../twist/overview.md) | Supplies the per-direction coordinate offset (with twist delta) that discovery and routing query |
| [Megascale](../megascale/overview.md) | Stitches per-slice ICI topologies into a cross-rack cluster; consumes ICI's per-slice `(X,Y,Z)` bounds |

## Cross-References

**ICI section pages**
- [Link Bring-Up](link-bringup.md) — the 16-step `Master::InitSlice` sequence, per-phase RPCs, firmware PHY / host DL split, poll loop and deadlines
- [Topology Discovery](topology-discovery.md) — square-seed polarity, BFS coordinates, `LocalTopology` wire format, failure catalog, megascale handoff
- [DMA Descriptor](dma-descriptor.md) — per-family descriptor word layout, remote sync-flag encoding, granule sizing
- [All-Reduce Primitive](all-reduce-primitive.md) — colored-ring reduce-scatter + all-gather, five strategy families, dtype/quantization gates, tree barriers
- [Failure Recovery](failure-recovery.md) — `SliceFailureType`, `FailDevice` cascade, `LinksDownReset`, error masking
- [VC Balance / Allocation](vc-balance-allocation.md) — `IciResource → ResourceVector` slot mapping, channel-merge, deadlock detection

**Sibling sections**
- [Collectives](../collectives/overview.md) · [Routing](../routing/overview.md) · [Twisted Torus](../twist/overview.md) · [Megascale](../megascale/overview.md)
- [back to index](../index.md)
