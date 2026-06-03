# XEvent Metadata IDs

> *All strings, ids, and addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

An `XEvent` on a device or host timeline carries no name — it carries an `int64 metadata_id` that keys into its `XPlane`'s `event_metadata` map (`map<int64, XEventMetadata>`). This page is the catalog of the *names* that map resolves to: the interned `XEventMetadata.name` strings libtpu registers for every trace point and every host scope it can emit. It is a reference catalog, not an algorithm page — the builder that allocates the ids and the four-level object model are owned by [XPlane / XStat / TraceMe Emission](xplane-xstat-traceme.md), and the device trace-point → event-name translation is owned by [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md). The companion *stat* dictionary (`XStatMetadata`, the per-event annotations) is on [XStat Metadata IDs](xstat-metadata-ids.md). This page owns the event-name dictionary alone.

The single fact that governs the whole catalog: **an XEvent metadata id is not a global type number — it is a per-plane interning key.** The integer `7` on `/device:TPU:0` and the integer `7` on `/host:0` denote different events, because each `XPlane` builds its own `event_metadata` map at collection time. A consumer must read the plane's dictionary to resolve any id; there is no cross-plane id namespace. Consequently the catalog has two halves with two different id-assignment regimes. **Device-plane events** are seeded by a *static, wire-stable* hardware enum: each chip family's `TraceEntries.TracePointId` value (banded 0–255, with gaps) is stamped into the hardware ring buffer's `TraceHeader.trace_point_id`, and its enum-value *string* becomes the `XEventMetadata.name`. **Host-plane events** are *dynamically name-interned*: a `tsl::profiler::TraceMe` label flows through `XPlaneBuilder::GetOrCreateEventMetadata(string_view)`, which hashes the label and hands out the next free plane-local id on first sight.

The device half is therefore catalogable by the enum it derives from. There are five per-chip `TraceEntries.TracePointId` enums (one per silicon family), 99/122/78/135/144 values each, re-banded across generations rather than strictly additive. The host half is catalogable only by name — the integers are a `tsl` build detail (`HostEventType`), not a wire contract — so this page lists host events by their confirmed ASCII label. Both halves are grouped below by the cross-cutting category a profile consumer sees on the timeline: TensorCore-sequencer / compute, DMA & memory transfer, sync & fence, control & instrumentation, collective substrate, throttle & power, SparseCore, and the host-scope band.

The catalog this page reconstructs covers:

- **The two id regimes** — static `TracePointId`-enum-seeded device names versus dynamic name-interned host names, and why an id only means something relative to its plane.
- **The device event-name dictionary** — every `TraceEntries.TracePointId` band, the enum-value strings it contributes, and the family-by-family deltas (UHI→HDE, BarnaCore→SparseCore, the growing throttle band).
- **The host event-name dictionary** — the confirmed `TraceMe` labels (`TpuExecuteOp`, `InfeedEnqueueTuple`, `MegaScale:…`, `TpuCompile`, …) interned by name.
- **The category taxonomy** — how each band maps onto compute / memory / sync / control / collective / throttle / SparseCore, with the representative event names per category.

| | |
|---|---|
| **Event name source field** | `XEventMetadata.name` (xplane.proto field 2), keyed by `XEvent.metadata_id` |
| **Device id regime** | static — `TraceEntries.TracePointId` enum value string |
| **Host id regime** | dynamic — `GetOrCreateEventMetadata(string_view)` name intern |
| **Device chip families** | 5 — pxc, vfc, vlc, glc, gfc |
| **Device event counts** | 99 / 122 / 78 / 135 / 144 (pxc/vfc/vlc/glc/gfc) |
| **TracePointId value range** | banded 0–255 with reserved gaps; sentinel `255` (pxc only) |
| **Device builder** | `xprof::TpuXPlaneBuilder` / `TpuXLineBuilder::AddEvent` |
| **Host builder** | `tsl::profiler::XPlaneBuilder::GetOrCreateEventMetadata` |

> **NOTE —** the *ids* in this page's device tables are `TraceEntries.TracePointId` enum values (the hardware-stamped, wire-stable integers), **not** the plane-local `XEvent.metadata_id` a serialized `XSpace` actually carries. The profiler maps the enum value to a freshly-interned plane-local id at collection time; the enum value is the stable contract, the metadata id is derived per plane. When a row says "id 81", that is `TCS_INTERNAL_SET_SYNC_FLAG`'s enum value on that family, the thing the silicon writes — see [XPlane / XStat / TraceMe Emission §How a Device Trace-Entry Becomes an XEvent](xplane-xstat-traceme.md).

| Category | pxc | vfc | vlc | glc | gfc | id source |
|---|---:|---:|---:|---:|---:|---|
| Compute / TensorCore-seq (TCS, BarnaCore, SC tasks) | 11+29 | 11 | 11 | 12 | 12 | TracePointId |
| Memory / DMA (UHI/HDE, OCI, CMQ/VDQ, CMN/CMNUR, O2CUR) | 48 | 76 | 54 | 70 | 73 | TracePointId |
| Sync / fence (TCS sync flags, SC barriers) | 8 | 8 | 8 | 14 | 14 | TracePointId |
| Control / instrumentation (tracemark, interrupt, fence) | ~9 | ~6 | ~3 | ~9 | ~9 | TracePointId |
| Collective substrate (ICI packet, ICR DMA) | 9 | 9 | 9 | 9 | 9 | TracePointId |
| Throttle / power (+ FLL, SPI sampler) | 1 | 7 | 7 | 21 | 25 | TracePointId |
| SparseCore band (SC instruction/task/stream/message) | — | 18 | — | 18 | 18 | TracePointId |
| Perf-counter sampling (STATS_COUNTER) | — | — | — | — | 6 | TracePointId |
| Host scope band (TraceMe labels) | dynamic, name-interned (shared across host planes) | | | | | name intern |

---

## How an Event Name Is Resolved

### Purpose

Before the catalog, the resolution rule, because it is what makes the catalog usable. Every consumer that wants a human name for an `XEvent` performs the same two-step lookup, and a reimplementer who skips it will mis-label every event.

### The lookup

```text
XEvent.metadata_id  ──(key)──▶  XPlane.event_metadata[ id ]  ──▶  XEventMetadata.name
                                 (map<int64, XEventMetadata>          (the human string;
                                  on THIS plane only)                  for device events, the
                                                                       TracePointId enum string)
```

The map is per-plane. The same logical event interned on `/device:TPU:0` and `/device:TPU:1` gets an independently-allocated id in each plane's map, so the id is meaningless without the plane that owns it. This is why the device tables below are keyed on the *hardware* `TracePointId` enum value (which is stable) and not on a metadata id (which is not).

### The two regimes side by side

| | Device plane | Host plane |
|---|---|---|
| Plane name | `/device:TPU:<n>` | `/host:<n>`, `XLA Modules`, `XLA Ops`, `Steps` |
| Name source | `TraceEntries.TracePointId` enum-value string | `tsl::profiler::TraceMe` label |
| Id assignment | static enum → name → interned per-plane id | dynamic: hash label, next free plane-local id |
| Builder | `xprof::TpuXPlaneBuilder` / `TpuXLineBuilder::AddEvent` | `tsl::profiler::XPlaneBuilder::GetOrCreateEventMetadata` |
| Payload (`XEventMetadata.metadata`) | serialized `TraceEntry` variant blob | usually empty |
| Confidence | CERTAIN (enum strings byte-confirmed in `.rodata`) | CERTAIN (labels byte-confirmed) |

> **QUIRK —** the device `TraceEntry` oneof *field number* (2..N, dense and sequential) is a different id space from the `TracePointId` *enum value* (banded 0–255). Both are present in `trace_entries.proto` and they share declaration order, but only the enum value is what the hardware stamps into `TraceHeader.trace_point_id` and therefore what becomes the event name. A reimplementation that keys events off the protobuf field tag will mis-name every device event. The catalog tables below carry the enum value; the field tag is an implementation detail of the wire encoding documented on [TraceEntriesCoder](trace-entries-coder.md).

---

## TensorCore Sequencer & Compute Events

### Purpose

The hardware does not name a matmul or a convolution as a trace point — those are HLO ops that appear on the *XLA Ops* plane via dynamic name-interning, not via `TracePointId`. Compute *progress* on the device is observed indirectly through the TensorCore Sequencer (TCS) instruction-stream band, the BarnaCore FSM band (pufferfish only), and the SparseCore task/stream band (viperfish onward). This section catalogs the compute-adjacent TCS and BarnaCore names; the SparseCore band has its own section below.

### TensorCore Sequencer (TCS) band — ids 80–100

The TCS band is present on every family and is the most stable part of the catalog. Ids are nearly constant across generations; one name changes (`HOST_INTERRUPT` → `CORE_INTERRUPT` from viperfish on), and glc/gfc append OCI-completion and PPM ids at 97–100.

| TracePointId value | Name | Category | Families | Confidence |
|---|---|---|---|---|
| 80 | `TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE` | sync | all | CERTAIN |
| 81 | `TCS_INTERNAL_SET_SYNC_FLAG` | sync | all | CERTAIN |
| 82 | `TCS_INTERNAL_ADD_SYNC_FLAG` | sync | all | CERTAIN |
| 83 | `TCS_INTERNAL_HOST_INTERRUPT` (pxc) / `TCS_INTERNAL_CORE_INTERRUPT` (vfc+) | control | all | CERTAIN |
| 84 | `TCS_INTERNAL_SET_TRACEMARK` | control | all | CERTAIN |
| 85 | `TCS_INTERNAL_TRACE_INSTRUCTION` | control | all | CERTAIN |
| 86 | `TCS_INTERNAL_UNSUCCESSFUL_SYNC_ATTEMPT` | sync | all | CERTAIN |
| 87 | `TCS_INTERNAL_SUCCESSFUL_SYNC_ATTEMPT` | sync | all | CERTAIN |
| 88 | `TCS_INTERNAL_READ_SYNC_FLAG` | sync | all | CERTAIN |
| 89 | `TCS_INTERNAL_SCALAR_FENCE_START` | sync | all | CERTAIN |
| 90 | `TCS_INTERNAL_SCALAR_FENCE_END` | sync | all | CERTAIN |
| 91–96 | `OCI_DESCRIPTOR_*_ISSUED_FROM_TCS` / `OCI_MESSAGE_ISSUED_FROM_TCS` / `OCI_COMMON_*_COMPLETED_IN_TCS` | memory | all | HIGH |
| 99 | `TCS_PPM_ENTRY_PPM_UPDATE_EVENT` | throttle | glc/gfc | HIGH |
| 100 | `STATS_COUNTER_SAMPLE_ISSUED_FROM_TCS` | perf-sample | gfc | HIGH |

> **NOTE —** `TCS_INTERNAL_TRACE_INSTRUCTION` (85) and `TCS_INTERNAL_SET_TRACEMARK` (84) are the closest the device catalog comes to a "compute step" marker: they bracket instrumented instruction spans that a consumer aligns with HLO ops. The actual matmul/conv identity is supplied later by the HLO symbolizer (`TpuXPlaneSymbolizer::SetEventMetadataFromSymbol`), which fills `display_name` and HLO source stats — see [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md).

### BarnaCore FSM + sequencer band — ids 100–134 (pufferfish only)

Pufferfish carries a 29-event BarnaCore band that no later family has; it was replaced by the SparseCore band from viperfish on. The FSM channel-controller ids (100–115) are a 16-deep numbered family; the named compute and sequencer-control events are the interesting rows.

| TracePointId value | Name | Category | Confidence |
|---|---|---|---|
| 100–115 | `BC_FSM_CHANNEL_CONTROLLER0` … `CONTROLLER15` | compute FSM | HIGH |
| 116 | `BC_FSM_PROCESS_HOSTID` | compute | HIGH |
| 117 | `BC_FSM_SPARSE_REDUCE` | compute | CERTAIN |
| 118 | `BC_FSM_PROCESS_BCID` | compute | HIGH |
| 119 | `BC_FSM_CONCAT` | compute | HIGH |
| 120 | `BCS_TRACE_INSTRUCTION` | control | HIGH |
| 121 | `BCS_SET_TRACEMARK` | control | HIGH |
| 122 | `BCS_SYNC_START_STOP_TRACE` | sync | HIGH |
| 123 | `BCS_HOST_INTERRUPT` | control | HIGH |
| 124 | `BCS_FENCE` | sync | HIGH |
| 125–134 | `BC_OCI_{READ,WRITE}_{REQUEST,RESPONSE}` / `OCI_DESCRIPTOR_*_ISSUED_BY_BC` / `OCI_MESSAGE_{RECEIVED,SENT}_BY_BC` | memory | HIGH |

---

## DMA & Memory Transfer Events

### Purpose

The largest category by event count. Every host↔chip transfer, on-chip-interconnect descriptor lifecycle, VPU↔scratchpad DMA, and HBM-controller request is a trace point here. The band is the most re-architected across generations: the host-DMA front end migrates from UHI (pufferfish) to HDE (viperfish on), the VPU-DMA engine changes from CMQ (pufferfish) to VDQ (viperfish-lite) to nothing, and the memory-network controller hierarchy (CMN/CMNUR/CMNDE) and address-translation DMA (O2CUR) appear only on the later chips.

### Host-DMA front end

| Family | Band ids | Names (representative) | Confidence |
|---|---|---|---|
| pxc (UHI) | 0–10 | `UHI_HOST_DMA_TRANSACTION_STARTED_ADDRESS_TRANSLATION`, `UHI_HOST_PHYSICAL_{REQUEST,RESPONSE}_{READ,WRITE}`, `UHI_OCI_REQUEST_{READ,WRITE}`, `OCI_*_BY_UHI_*` | CERTAIN |
| vfc/vlc/glc/gfc (HDE) | 8–14 (glc) / 1–14 (gfc) | `HDE_HOST_REQUEST_WRITE`, `HDE_HOST_RESPONSE_WRITE`, `HDE_HOST_REQUEST_READ`, `HDE_HOST_RESPONSE_READ`, `OCI_COMMON_HDE_{READ,WRITE}_REQUEST`, `OCI_MESSAGE_SENT_BY_HDE` | CERTAIN |

### OCI engine descriptor / message lifecycle — the dominant band

The on-chip-interconnect (OCI) descriptor and message lifecycle is the single largest contributor to the memory category — 32 events on pxc, up to 54 on glc. Names follow a `OCI_<phase>_<verb>_<location>` grammar (`OCI_DESCRIPTOR_DESC_AT_QNM`, `OCI_COMMON_READ_CMD_ISSUED_FROM_ENGINE`, `OCI_MESSAGE_MSG_ISSUED_FROM_QNM`). Because the band is generated mechanically from the descriptor/message FSM rather than hand-named, it is better described by its grammar than dumped row by row.

| Axis | Values | Source |
|---|---|---|
| phase prefix | `OCI_DESCRIPTOR`, `OCI_GENERIC`, `OCI_COMMON`, `OCI_MESSAGE`, `OCI_WRITE_REQ` | enum string prefix |
| verb | `ISSUED_FROM`, `ENQUEUED_AT`, `RECEIVED_BY`, `SENT_BY`, `ACCEPTED_AT`, `COMPLETED_IN`, `GENERATED_IN`, `DESC_AT` | enum string body |
| engine/location | `ENGINE`, `QNM`, `MN`, `TCS`, `BC`, `SC`, `MGR`, `CMNDE`, `ICR_{EGRESS,INGRESS}_DMA`, `UHI/HDE_BRIDGE` | enum string suffix |
| representative ids (pxc) | 20–27 (engine), 49–55 (ICR/command), 91–96 (TCS-issued), 129–134 (BC-issued) | TracePointId |
| representative ids (glc) | 20–27 (engine), 91–96 (TCS), 124–130 (SC), 161–167 (MGR) | TracePointId |

A reimplementer reconstructs the full per-family list from `trace_entries.proto`'s nested enum; the rows that carry distinct semantics (a true command issue, a completion, a cross-engine handoff) are the ones above. The named anchors confirmed verbatim: `OCI_COMMON_OCI_READ_COMMAND` (pxc 55), `OCI_MESSAGE_PACKET_SENT_TO_OCI` (pxc 52), `OCI_DESCRIPTOR_DESC_AT_QNM` (pxc 20).

### VPU / scratchpad DMA — CMQ (pxc) and VDQ (vlc)

| Family | Band ids | Name family | Confidence |
|---|---|---|---|
| pxc (CMQ) | 140–149 | `CMQ_VPU_DMA_DESC`, `OCI_MESSAGE_CMQ_VPU_DMA_MSG`, `CMQ_VPU_DMA_REQ_{VMEM0,VMEM1,CMEM}_TO_{CMEM,VMEM0,VMEM1}_{READ,WRITE}` (8 directions) | CERTAIN |
| vlc (VDQ) | 142–149 | `VDQ_TRANSACTION_{READ,WRITE}_{REQ,RESP}_CHAN0/1` (8 events) | HIGH |

> **QUIRK —** the CMQ band names *both endpoints and direction* in the enum string (`VMEM0_TO_CMEM_READ` vs `CMEM_TO_VMEM1_WRITE`), so the eight ids 142–149 fully enumerate the {VMEM0,VMEM1}×{read,write}×{to-CMEM,from-CMEM} cross product. A reimplementation that collapses these to a single "VPU DMA" event loses the source/destination scratchpad identity that the timeline renders.

### Memory-network controller & HBM (glc/gfc) and address translation (gfc)

| Family | Band ids | Name family | Category | Confidence |
|---|---|---|---|---|
| vfc/glc/gfc | 70–79 | `OCI_DESCRIPTOR_COMMON_RECEIVED_BY_CMNDE`, `OCI_MESSAGE_SENT_BY_CMNDE`, `CMN_DMA_REQUEST_{EAST,WEST}_SIDE_LANE0..3` | memory | HIGH |
| glc/gfc | 170–173 | `CMNUR_HBMC_{RD_REQ,RD_RSP,WR_REQ,WR_RSP}` (HBM controller) | memory | CERTAIN |
| glc/gfc | 174–185 | `CMNDE_CMNUR_{SRC,DST}_{REQ,RSP}`, `OCI_CMNUR_{RD,WR}_{REQ,RSP}`, `CMNUR_CMNUCB_CONTROL_*` | memory | HIGH |
| gfc | 183–188 | `O2CUR_L2P_{WR_REQ_FIRST,WR_RSP_LAST,RD_REQ,RD_RSP}`, `CMNDE_UR_L2P_DMA_{REQ,RSP}` (logical→physical addr translation) | memory | HIGH |

---

## Sync & Fence Events

### Purpose

Semaphore, barrier, and fence operations. On all families this is the TCS sync-flag sub-band (already listed under TensorCore Sequencer above, ids 80–90); from viperfish on, the SparseCore instruction band adds an explicit, richer set of barrier/sync/sfence start-stop pairs that the older silicon did not expose.

### The sync vocabulary

| Layer | Names | Ids | Families | Confidence |
|---|---|---|---|---|
| TCS sync flags | `TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE`, `TCS_INTERNAL_{SET,ADD,READ}_SYNC_FLAG`, `TCS_INTERNAL_{SUCCESSFUL,UNSUCCESSFUL}_SYNC_ATTEMPT`, `TCS_INTERNAL_SCALAR_FENCE_{START,END}` | 80–90 | all | CERTAIN |
| SparseCore fences/barriers | `SC_INSTRUCTION_SFENCE_{START,STOP}` (111/112), `SC_INSTRUCTION_SYNC_{START,STOP}` (113/114), `SC_INSTRUCTION_BARRIER_{START,STOP}` (115/116), `SC_INSTRUCTION_SYNC_WATCH_{START,STOP}` (117/118) | 111–118 | vfc/glc/gfc | CERTAIN |
| BarnaCore (pxc) | `BCS_SYNC_START_STOP_TRACE` (122), `BCS_FENCE` (124) | 122/124 | pxc | HIGH |

> **NOTE —** the sync events come in `_START`/`_STOP` (or `_SUCCESSFUL`/`_UNSUCCESSFUL`) pairs because the timeline renders a sync attempt as a duration span, not a point. A consumer pairs the start id with the next matching stop id on the same line, using the `sync_flag_id`/`sync_flag_number` stat ([XStat Metadata IDs](xstat-metadata-ids.md)) to disambiguate concurrent flags. The unsuccessful/successful split lets the UI color a stalled sync differently from one that completed immediately.

---

## Control & Instrumentation Events

### Purpose

The events that mark the instrumentation stream itself — tracemark insertion, the trace-instruction span, and host/core interrupts. These are not workload events; they are the scaffolding the profiler uses to align hardware time with software intent.

| Name | Id (pxc) | Id (glc/gfc) | Subsystem | Confidence |
|---|---|---|---|---|
| `TCS_INTERNAL_SET_TRACEMARK` | 84 | 84 | TCS | CERTAIN |
| `TCS_INTERNAL_TRACE_INSTRUCTION` | 85 | 85 | TCS | CERTAIN |
| `TCS_INTERNAL_HOST_INTERRUPT` / `..._CORE_INTERRUPT` | 83 | 83 | TCS | CERTAIN |
| `SC_INSTRUCTION_CORE_INTERRUPT` | — | 108 | SparseCore | CERTAIN |
| `SC_INSTRUCTION_SET_TRACEMARK` | — | 109 | SparseCore | CERTAIN |
| `SC_INSTRUCTION_TRACE_INSTRUCTION` | — | 110 | SparseCore | CERTAIN |
| `BCS_TRACE_INSTRUCTION` / `BCS_SET_TRACEMARK` / `BCS_HOST_INTERRUPT` | 120/121/123 | — | BarnaCore | HIGH |

> **QUIRK —** the interrupt event renames across generations (`HOST_INTERRUPT` on pufferfish, `CORE_INTERRUPT` from viperfish on) but keeps the *same enum value* (83). The id is the stable contract; the string is not. A consumer that switches on the enum value sees one event; one that switches on the name string must handle both spellings. This is the general rule for the device catalog — ids are wire-stable, names can be re-spelled.

---

## Collective Substrate Events (ICI)

### Purpose

Collectives have two views. The *semantic* view (`AllReduce`, `AllGather`, `ReduceScatter`, …) is a host `TraceMe` label, catalogued in the host section below. The *physical* view is the inter-chip-interconnect (ICI) packet band — the actual link-level packet rx/tx/queue/inject events the silicon stamps. The ICI band is identical in shape across all five families (9 events, ids 40–48, plus the ICR-DMA bridge events 43–53), because the link layer did not change.

| TracePointId value | Name | Confidence |
|---|---|---|
| 40 | `ICI_PACKET_PACKET_RECEIVED_ON_LINK_INPUT` | CERTAIN |
| 41 | `ICI_PACKET_PACKET_TRANSMITTED_ON_LINK_OUTPUT` | CERTAIN |
| 42 | `ICI_PACKET_PACKET_QUEUED_FOR_LINK_TRANSMISSION` | CERTAIN |
| 43–46 | `ICI_PACKET_{CONTROL,DATA}_PACKET_{INJECTED,RECEIVED}_BY_ICR_DMA_BRIDGE` | CERTAIN |
| 47–48 | `ICI_PACKET_{CONTROL,DATA}_PACKET_QUEUED_FOR_LOCAL_INGRESS` | HIGH |
| 49–53 | `OCI_DESCRIPTOR_ENQUEUED_IN_ICR_EGRESS_DMA`, `OCI_MESSAGE_GENERATED_IN_ICR_{EGRESS,INGRESS}_DMA`, `OCI_MESSAGE_PACKET_{SENT_TO_OCI,RECEIVED_IN_ICR}` | HIGH |

> **NOTE —** the link layer events carry `router_link_port_id ∈ {LINK0..LINK5}` and `virtual_channel` stats, so a consumer reconstructs per-link, per-VC bandwidth from this band even though the band itself names no collective. The mapping from these physical packets to a semantic `AllReduce` span is done host-side by correlating the `MegaScale:` TraceMe scope's time window with the ICI traffic in it — the two views are joined by time, not by a shared id.

---

## SparseCore Events

### Purpose

From viperfish on, sparse/embedding compute moved off the pufferfish BarnaCore FSM onto a dedicated SparseCore (SC) with its own sequencer (SCS), tile (SCT), and crossbar (XBAR). The SC instruction/task/stream/message band (18 events, ids 108–135) is its trace surface. The control and sync sub-bands were already listed above; this section catalogs the task/stream/message names that represent SparseCore *progress*.

| TracePointId value | Name | Category | Confidence |
|---|---|---|---|
| 119 | `SC_TASK_ISSUE_FROM_SCS` | compute | CERTAIN |
| 120 | `SC_TASK_COMMIT_ON_SCT` | compute | CERTAIN |
| 121 | `SC_STREAM_ISSUE_FROM_CORE` | compute | CERTAIN |
| 122 | `SC_STREAM_PROGRESS_XBAR` | compute | CERTAIN |
| 123 | `SC_STREAM_PROGRESS_CMN` | compute | CERTAIN |
| 131 | `SC_MESSAGE_OUTBOUND_INTERNAL_MESSAGE` | memory/msg | HIGH |
| 132 | `SC_MESSAGE_INBOUND_INTERNAL_MESSAGE` | memory/msg | HIGH |
| 124–130 | `OCI_DESCRIPTOR_*_ISSUED_BY_SC`, `OCI_MESSAGE_{RECEIVED,SENT}_BY_SC` | memory | HIGH |
| 129/134/135 | `STATS_COUNTER_SAMPLE_ISSUED_FROM_{SCS,SCTD,SCTC}` | perf-sample | HIGH (gfc) |

> **NOTE —** `SC_TASK_ISSUE_FROM_SCS` → `SC_TASK_COMMIT_ON_SCT` brackets a SparseCore task's lifetime across the issue/commit boundary, and the three `SC_STREAM_PROGRESS_*` ids mark its movement through the crossbar (XBAR) and memory network (CMN). These are how a profile shows a SparseCore op's pipeline occupancy; they are the SparseCore analogue of the TCS instruction-stream events.

---

## Throttle & Power-Management Events

### Purpose

The fastest-growing band in the catalog and a first-class trace category. Pufferfish has exactly one throttle event; gfc has twenty-five (20 throttle + 2 SPI samplers + 3 FLL). The growth tracks the power-delivery complexity of newer silicon — cycle-skip arbitration, PPM (peak-power-management) brake edges, LDIDT voltage tracking, and frequency-locked-loop lock/select all became observable.

| Family | Count | Band ids | Name families | Confidence |
|---|---:|---|---|---|
| pxc | 1 | 97 | `THROTTLE_STATE_THERMAL_AND_ELECTRICAL_THROTTLE_STATE` | CERTAIN |
| vfc/vlc | 7 | 98–104 | `THROTTLE_CYCLE_SKIP_*` (7-event family, lower band than glc) | HIGH |
| glc | 19+2 | 200–218, 168–169 | `THROTTLE_CYCLE_SKIP_{THERMAL,EXT_BRAKE,EXT_THROTTLE,LDIDT_*,ARBITRATION}`, `THROTTLE_CYCLE_SKIP_PPM_{SUSTAINED,DIDT,OVERSHOOT}_{AGGRESSIVE,NOMINAL}_BRAKE_{RISING,FALLING}_EDGE`, `THROTTLE_LDIDT_RUNNING_MEAN_VOLTAGE`, `SPI_SAMPLER_{VDD_CORE,HBM}_FRAME_EXEC` | CERTAIN |
| gfc | 20+2+3 | 200–222, 168–169 | glc set restructured + `THROTTLE_MAXIMUM_TEMPERATURE_*`, `THROTTLE_MAX_VALUE_THROTTLE_MAX_{FAST,SLOW}`, `THROTTLE_LDIDT_VOLTAGE_THROTTLE_{MAX,MIN}_VOLTAGE_*`, `FLL_LOCK_FLL_{0,1}_LOCK` (220/221), `FLL_SELECT_FLL_SELECT` (222) | HIGH |

> **QUIRK —** the throttle band sits at a *high* id range (200–222) on glc/gfc but at a *low* range (97–104) on the earlier families — the band was relocated, not extended in place. A reimplementation that assumes throttle events live near id 97 on all chips will mis-classify every glc/gfc throttle event as memory or SparseCore. Key on the name prefix (`THROTTLE_`, `FLL_`, `SPI_SAMPLER_`), not on the numeric band.

### Perf-counter sampling — STATS_COUNTER (gfc only)

The newest family adds an in-band hardware perf-counter sampling band absent everywhere else: six `STATS_COUNTER_SAMPLE_ISSUED_FROM_{TCS,SCS,SCTD,SCTC,ICR_DATA,CMNUR}` events (ids 56, 100, 129, 134, 135, 182). Each carries `num_counters`, `payload_low/high` (uint64), and a `sample_id` — the on-chip counters are sampled and emitted as trace events rather than read out separately.

---

## Host Scope Events (TraceMe Labels)

### Purpose

Every event on a `/host:<n>`, `XLA Modules`, `XLA Ops`, or `Steps` plane comes from a `tsl::profiler::TraceMe` RAII scope on a runtime thread, not from hardware. Its name is the TraceMe label string, interned dynamically by `XPlaneBuilder::GetOrCreateEventMetadata(string_view)`. There is no fixed integer for these — the `tsl` `HostEventType` enum that supplies the by-id fast path is a build detail and was not recovered. The catalog is therefore by *name*. All names below are confirmed verbatim as ASCII in `libtpu.so`.

### Runtime execution & transfer

| Label | Role | Confidence |
|---|---|---|
| `TpuExecuteOp` | top-level device-execute scope | CERTAIN |
| `DoEnqueueProgram` / `DoEnqueueContinuationProgram` | program enqueue onto the device queue | CERTAIN |
| `EnqueueRequestLocked` / `EnqueueProgram` / `ExecuteProgram` / `LoadProgram` / `RunHlo` | queue submission & execution stages | CERTAIN |
| `InfeedEnqueueTuple` / `InfeedEnqueue` / `WaitForInfeed` | infeed path | CERTAIN |
| `OutfeedDequeueTuple` / `OutfeedDequeue` / `WaitForOutfeed` | outfeed path | CERTAIN |
| `TransferBufferToDevice` / `TransferBufferFromDevice` / `TransferToDevice` / `TransferFromDevice` / `HostToDevice` / `DeviceToHost` / `Memcpy` | host↔device buffer transfer | CERTAIN |
| `InitializeTpu` / `StepInfo` / `SessionRun` / `ExecutorState::Process` | session/step framing | HIGH |

### Compiler

| Label | Role | Confidence |
|---|---|---|
| `TpuCompile` | TPU backend compile scope | CERTAIN |
| `XlaCompile` | XLA compile scope | CERTAIN |
| `CompileOp` / `JitCompile` | op/JIT compile scopes | CERTAIN |

### Collective (semantic view)

| Label | Role | Confidence |
|---|---|---|
| `MegaScale:` (prefix) | megascale transport scope prefix | CERTAIN |
| `AllReduce` / `AllGather` / `ReduceScatter` / `SendRecv` / `collective-permute` | semantic collective op names | CERTAIN |
| `MegaScaleAction` / `MegaScaleActionGraph` | megascale action-trace scopes | CERTAIN |

> **GOTCHA —** these labels are interned *by name*, so two TraceMe scopes with the same label on the same plane share one `XEventMetadata` (and one id), while the same label on a different plane gets a fresh id. A reimplementation must not assume `AllReduce` has a fixed id — it has whatever plane-local id the first `AllReduce` scope on that plane was assigned. The names are stable; the ids are emphatically not. This is the dual of the device rule (device ids are stable, names can be re-spelled).

> **NOTE —** a TraceMe label may carry key/value metadata encoded into the name string as `name#k1=v1,k2=v2#` (the `TraceMeEncode` wire format, [XPlane / XStat / TraceMe Emission §Encode](xplane-xstat-traceme.md)). The bare `name` before the first `#` is what becomes the `XEventMetadata.name`; the `k=v` pairs become `XStat`s on the event, not part of the event name. So `TpuExecuteOp#program_id=42#` interns the event as `TpuExecuteOp` and adds a `program_id` stat — the catalog name is always the un-suffixed base.

---

## Per-Generation Catalog Deltas

The device catalog grows monotonically in count but is re-banded, not strictly additive, across silicon generations. The table summarizes what each family adds or drops; a reimplementer targeting a specific chip uses it to know which bands exist.

| Dimension | pufferfish (pxc) | viperfish (vfc) | viperfish-lite (vlc) | ghostlite (glc) | 6acc60406 (gfc) |
|---|---|---|---|---|---|
| total events | 99 | 122 | 78 | 135 | 144 |
| host-DMA band | UHI (7) | HDE (4) | HDE (4) | HDE (4) | HDE (4) |
| sparse compute | BarnaCore `BC/BCS/B7b2m` (29) | SparseCore `SC_*` (18) | none | `SC_*` (18) | `SC_*` (18) |
| VPU DMA | CMQ (9) | — | VDQ (8) | — | — |
| mem-net controller | — | CMN/CMNUR/CMNDE (20) | — | CMN/CMNUR/CMNDE (16) | + O2CUR (4) |
| throttle/power | 1 | 7 | 7 | 19 + SPI(2) | 20 + SPI(2) + FLL(3) |
| perf-counter sampling | — | — | — | — | `STATS_COUNTER` (6) |
| addr translation | — | — | — | — | `O2CUR_L2P` (4) |
| TCS interrupt name | `HOST_INTERRUPT` | `CORE_INTERRUPT` | `CORE_INTERRUPT` | `CORE_INTERRUPT` | `CORE_INTERRUPT` |
| dummy sentinel (id 255) | yes | no | no | no | no |

The trend: the host interface unifies to HDE; sparse compute migrates from BarnaCore to SparseCore with explicit barrier/sync-watch semantics; the memory hierarchy gains an explicit network controller (CMNUR) and, on gfc, address-translation (O2CUR) and in-band perf-counter sampling; power management dominates the growth (1 → 20+ throttle events). The five enums live in the binary's bundled descriptor pool at `0xbef0d50` (pxc), `0xbf06830` (vfc), `0xbf28fd0` (vlc), `0xbf41210` (glc), `0xbf64c80` (gfc), each as the `TraceEntries.TracePointId` nested enum of its `trace_entries.proto`.

> **NOTE —** the per-event *payload* fields (the `TraceEntry` variant message that rides in `XEventMetadata.metadata`) are not catalogued here — that is the byte layout of each event, owned by the payload pages ([uhi/oci/ici DMA](payload-uhi-oci-ici-dma.md), [SC band](payload-sc-band.md), [vfc/vlc/gfc](payload-vfc-vlc-gfc.md)). This page is the name→category dictionary only.

---

## What Is Not Recoverable Here

- **Host event integer ids.** The `tsl` `HostEventType`/`StatType` static enum values (the `GetOrCreateEventMetadata(int64)` by-id fast path) are assigned at `tsl` build time and were not recovered from strings — they are an implementation detail, not a wire contract. Host events are documented by name only. (LOW confidence on any specific host integer.)
- **The live `XEventMetadata.metadata` embedding.** The field is proven to carry the serialized per-family `TraceEntry`, but whether it stores the full `TraceEntry`, just the `TraceHeader`, or a re-encoded subset was not confirmed against a captured `XSpace`. (MEDIUM.)
- **The plane-local metadata-id allocation order.** Because ids are interned in first-seen order at collection time, the specific integer any event receives in a given `XSpace` is not predictable from static analysis — only the name and (for device events) the source `TracePointId` enum value are stable.

---

## Cross-References

- [XPlane / XStat / TraceMe Emission](xplane-xstat-traceme.md) — the builder API that interns these names (`GetOrCreateEventMetadata`), the four-level object model, and the host `TraceMe` capture path
- [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) — the device-side translation that maps a hardware `TracePointId` into the event name this page catalogs
- [XStat Metadata IDs](xstat-metadata-ids.md) — the companion *stat* (annotation) dictionary that types the per-event attributes; events here, stats there
- [Tracepoints Master Registry](tracepoints-master-registry.md) — the full per-family `TraceEntries.TracePointId` enumeration backing the bands summarized here
- [TraceEntriesCoder](trace-entries-coder.md) — the device codec and the oneof field-tag id space distinct from the enum-value id space
- [Profiling and Telemetry Overview](overview.md) — the five-stage capture pipeline and the two-source (`/host` + `/device`) `XSpace` these events populate
