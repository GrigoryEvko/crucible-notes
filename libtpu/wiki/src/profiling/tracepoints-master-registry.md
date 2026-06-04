# TracePoints Master Registry

> *All addresses, ids, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text` VMA equals file offset. Other versions will differ.*

## Abstract

This page is the **master trace-point id→name registry** for the libtpu on-device profiler: the table that maps every on-wire `trace_point_id` (the 8-bit `TraceHeader` field the [codec](trace-entries-coder.md) decodes) to a human event name, the hardware band it belongs to, and the subscriber(s) that consume it. It is the index that the decode → XSpace fan-out keys off: when `DecodeTraceBuffers` walks a 16-byte packet, the wire `trace_point_id` it reads is *only* a number; the meaning of that number — what event it names, which device line it lands on, which begin/end tracker pairs it — lives here.

The registry is not a static table in the binary. It is *built at runtime* by `ConvertTpuTraceToXPlaneV2<TraceEntry>` (204 symbol references), which, on first sight of each on-chip core, runs a per-family **CoreContext setup lambda** that registers a fixed set of `TraceEventSubscriber`s, each carrying an inline `TracePoints<TraceEntry>` int32 id-set. `CoreDispatcher::RegisterSubscriber` (one out-of-line symbol per chip family, 6 total) inserts each subscriber into a `FlatHashMap<trace_point_id, vector<subscriber>>`. The id→name mapping itself is the codec's per-event `Decode<EventName>()` jump-table — the master registry is the *union* of that name space with the subscriber routing the lambda installs over it. Reconstructing it byte-exact means reading both: the codec's 111-entry decode table for names, and each family's lambda for the id-set each subscriber selects on.

The id space is **banded**. The deepsea gens (pxc/vfc/vlc/glc/gfc) pack their `trace_point_id` into a gappy 0..255 space partitioned by hardware origin: UHI host-bridge, OCI on-chip interconnect, ICI inter-chip interconnect, DMA, the TensorCore sync/fence band at base 80, the SparseCore band at base 109, the throttle band at base 104/200, and the MGR/power band at base 160. The legacy jxc (jellyfish) gen uses a *different* 16-bit id namespace entirely. This page owns the per-band id→name/subscriber/tracker rollup; the per-band **payload field decode** is owned by the payload pages, and the **id→name codec dispatch** by [`TraceEntriesCoder`](trace-entries-coder.md).

For reimplementation, the contract is:

- **The band partition of the deepsea 0..255 `trace_point_id` space** — which contiguous id range each hardware origin owns, and its band base.
- **The id→name mapping per band** — the event name the codec's `Decode<Name>()` attaches to each id (the wire→human map every decode/translation step keys off).
- **The id→subscriber fan-out** — the `FlatHashMap` routing the CoreContext lambda installs; the structural fact that one id can drive *N* subscribers (id 85 → 4).
- **The begin/end tracker match keys** — which ids feed which stateful tracker (Step/Task/Overlay/Sync/Dma) and the field it pairs spans on.

| | |
|---|---|
| **Registry builder** | `xprof::tpu::ConvertTpuTraceToXPlaneV2<TraceEntry>` — pxc `@0xf1d4360` (+5 families) |
| **CoreContext setup lambda** | pxc `@0xf1eb2e0`, gfc `@0xf229100`, glc `@0xf214640`, vfc `@0xf201bc0`, vlc `@0xf1f5940`, jxc `@0xf1da7c0` |
| **Routing structure** | `CoreDispatcher` `FlatHashMap<trace_point_id (u16 @ TraceHeader+0x18), vector<shared_ptr<TraceEventSubscriber>>>` |
| **Register call** | `CoreDispatcher::RegisterSubscriber(this, const TracePoints&, shared_ptr<sub>)` — pxc `@0xf1ecee0` (+5) |
| **Dispatch (drain)** | `CoreDispatcher::Dispatch` `@0xf1ed280` — `movzwl 0x18(%rax),%edx` reads the id, `call *0x10(vtable)` fans out |
| **Id→name source** | per-event `Decode<Name>()` via the 111-entry `rel32` jump table `@0xab85bc0` ([codec](trace-entries-coder.md)) |
| **Subscriber count / family** | pxc 8 · gfc 19 · glc 19 · vfc 17 · vlc 11 · jxc 10 |

### At-a-glance — bands and their id ranges

| Band | Base / range (deepsea) | Origin | Subscribers | Payload page | Conf. |
|---|---|---|---|---|---|
| UHI | OCI sub-channel (`*UhiBridge`) | Host bridge | (raw path) | [uhi-oci-ici-dma](payload-uhi-oci-ici-dma.md) | HIGH |
| OCI | `OciMessage*` (20 events) | On-chip interconnect | (raw path) | [uhi-oci-ici-dma](payload-uhi-oci-ici-dma.md) | HIGH |
| ICI | `IciPacket*` | Inter-chip interconnect | (raw path) | [uhi-oci-ici-dma](payload-uhi-oci-ici-dma.md) | HIGH |
| DMA | CMQ/CMN/HDE req+data-end | DMA engines | DmaSubscriber (jxc) | [uhi-oci-ici-dma](payload-uhi-oci-ici-dma.md) | MEDIUM |
| Sync/Fence | base **80** (80..90) | TensorCore | Sync + ScalarFence ×2 | [sc-band](payload-sc-band.md) / [vfc-vlc-gfc](payload-vfc-vlc-gfc.md) | CERTAIN |
| Throttle | base **104** (vfc/vlc) / **200** (gfc/glc) | Power mgmt | PowerThrottle | [vfc-vlc-gfc](payload-vfc-vlc-gfc.md) | HIGH |
| SparseCore | base **109** (109..120) | SparseCore | 6 SC subscribers | [sc-band](payload-sc-band.md) | CERTAIN |
| MGR/Power | base **160** + 168/169 | MGR firmware | PState/Firmware/Spi | [vfc-vlc-gfc](payload-vfc-vlc-gfc.md) | HIGH |
| BarnaCore | pxc — no SC band | pxc-only | (TC fan-out only) | [jxc-legacy](payload-jxc-legacy.md) | HIGH |
| jxc legacy | 16-bit jellyfish ns | jellyfish gen | HbmMux + Dma + 8 | [jxc-legacy](payload-jxc-legacy.md) | MEDIUM |

> **NOTE —** "raw path" means the band has **no dedicated subscriber** in the deepsea CoreContext lambda. UHI/OCI/ICI ids fall through to the raw `TraceEventSubscriber` decimal-string XEvent path (the FastIntToBuffer name fallback) rather than a semantic subscriber. See [§ Unbound bands](#unbound-bands--uhi-oci-ici-dma-on-deepsea). The *names* below are still the authoritative codec decode names; the routing is what is absent.

---

## How an id Becomes an Event

### The registration mechanism

The registry is assembled lazily. `ConvertTpuTraceToXPlaneV2<TraceEntry>` spawns a worker thread and, the first time it sees a given `ChipCoreId`, runs the per-family CoreContext setup lambda (`{lambda(shared_ptr<TraceEntryWrapper>)}::operator()`). That lambda builds the per-core `TpuXPlaneBuilder` and then performs N `RegisterSubscriber` calls — N being the per-family subscriber count (8/19/19/17/11/10).

```c
// CoreContext setup lambda — schematic of one register call
// (pxc @0xf1eb2e0; one of 8 such blocks)
function RegisterOne(dispatcher, kind, id_set[], n, line):
    sub = operator_new(...)                  // allocate the subscriber
    sub.vtable = SubscriberVtable[kind]       // or call its out-of-line ctor
    // most subscribers are wrapped in a ThreadedSubscriber (vtable + ClosureThread
    // worker @0xf1ede60 at +0xa0); HLO/SparseCore/Dma/HbmMux are NOT wrapped.
    tp = TracePoints<TraceEntry>()            // std::vector<int32> on the stack
    buf = _Znwm(4 * n)                        // {ptr@+0x0, size@+0x8, cap@+0x10}
    write_ids(buf, id_set, n)                 // movl/movabs/vmovaps id stores
    tp.size = n                               // movq $n, slot+0x8
    CoreDispatcher::RegisterSubscriber(dispatcher, tp, sub)  // @0xf1ecee0
    free(buf)                                 // tp freed after each call
```

`RegisterSubscriber` walks every id in the `TracePoints` set and, for each, appends `sub` to `FlatHashMap[id]`. This is what makes the registry a *multimap*: registering two subscribers with overlapping id-sets means a single wire id resolves to a vector of subscribers, and at drain every one of them runs.

> **QUIRK —** the id-set is not a property of the codec. The codec knows how to *decode* id 85 into a `TraceEntry`; it has no idea that on a deepsea gen id 85 must drive four different consumers. The decode name space (111 entries) and the routing (8..19 subscribers over a handful of ids) are two independent structures, joined only here. A reimplementation that builds the routing off the decode table — assuming "one decoder, one consumer" — is wrong for every fan-out id.

### The id write encodings

The inline `TracePoints` int32 buffer is filled with one of three x86 idioms, depending on set size. Reading them back is how each id-set below was recovered byte-exact:

| Set size | Encoding | Example |
|---|---|---|
| 1 | `movl $id,(buf)` | `movl $0x54,…` → {84} (pxc reg3, `@0xf1ec331`) |
| 2 | `movabs $0xHI00000000LO,%rcx; mov %rcx,(buf)` | `movabs $0x5a00000059` → {89,90} (`@0xf1ec0be`) |
| 4 | `vmovaps rodata,%xmm0; vmovups %xmm0,(buf)` | `@0xa2d6b40` → {81,82,88,87} (pxc sync) |
| >4 | a 4-id `vmovaps` base + per-id `movl $id,off(newbuf)` growth copies | SC-Syncs {111..116}, size 6 (`movq $0x6` `@0xf22af00`) |

The 4-id sync vector lives in `.rodata @0xa2d6b40` and decodes to `{81,82,88,87}`; the remaining two sync ids `{86,80}` are appended via `movabs $0x5000000056`. The SpiSampler pair `{168,169}` is `movabs $0xa9000000a8`.

### The dispatch (drain) path

At drain `CoreDispatcher::Dispatch @0xf1ed280` reads the wire id with `movzwl 0x18(%rax),%edx @0xf1ed2c4`, probes the `FlatHashMap`, and for each registered subscriber calls `*0x10(vtable)` (the subscriber's `ProcessTraceEntry`) at `@0xf1ed422/43c/498/4de`. A miss — an id no subscriber registered — is where the unbound bands go.

---

## The Sync / Fence Band (base 80)

### Purpose

The TensorCore sync-flag and scalar-fence band: the 80..90 id range carrying every sync-flag update/read/wait and scalar-fence start/end. This is the only band with a *literal in-body selection mask* (`0x1c7`), and the only one whose pairing semantics (wait BEGIN → wait END) are decoded directly here.

### Registry

| id | event name (codec) | subscriber(s) | tracker / key | Conf. |
|---|---|---|---|---|
| 80 | `TCS_EXTERNAL_SYNC_FLAG_UPDATE_DMA_DONE` | SyncSubscriber | SyncTracker (END, DMA-done) | CERTAIN |
| 81 | `TCS_INTERNAL_SET_SYNC_FLAG` | SyncSubscriber | (instant) | CERTAIN |
| 82 | `TCS_INTERNAL_ADD_SYNC_FLAG` | SyncSubscriber | (instant) | CERTAIN |
| 84 | `TCS_INTERNAL_SET_TRACEMARK` | TensorCoreStepSubscriber | StepTracker (TraceMark id) | CERTAIN |
| 85 | `TCS_INTERNAL_TRACE_INSTRUCTION` | Hlo + Overlay + OnDeviceTraceMe + LloOp | OverlayTracker (overlay_id) | CERTAIN |
| 86 | `TCS_INTERNAL_UNSUCCESSFUL_SYNC_ATTEMPT` | SyncSubscriber | SyncTracker (wait BEGIN) | CERTAIN |
| 87 | `TCS_INTERNAL_SUCCESSFUL_SYNC_ATTEMPT` | SyncSubscriber | SyncTracker (wait END) | CERTAIN |
| 88 | `TCS_INTERNAL_READ_SYNC_FLAG` | SyncSubscriber | (instant) | CERTAIN |
| 89 | `TCS_INTERNAL_SCALAR_FENCE_START` | ScalarFenceSubscriber ×2 | fence span (lines 9 + 62) | CERTAIN |
| 90 | `TCS_INTERNAL_SCALAR_FENCE_END` | ScalarFenceSubscriber ×2 | fence span | CERTAIN |

Two further codec names exist in this neighborhood but are not bound by any deepsea lambda subscriber: `TCS_INTERNAL_CORE_INTERRUPT` and `TCS_INTERNAL_HOST_INTERRUPT` (decode names confirmed in `.rodata`; routing not installed — `(unbound, HIGH)`).

### The 0x1c7 selection mask

`SyncSubscriber::ProcessTraceEntry` (pxc `@0xf1eeee0`) loads `mov $0x1c7,%esi @0xf1eef3d` and tests `(1 << (id - 80))` against it. The band base is **80**, so:

```text
0x1c7 = 0b1_1100_0111 = bits {0,1,2,6,7,8} = ids {80,81,82,86,87,88}
```

This is the canonical band-base encoding: a sync `trace_point_id`'s "selection-mask bit" is `(1 << (id - 80))`. It excludes 84 (tracemark → StepTracker), 85 (trace-instruction → 4-way fan-out), and 89/90 (scalar fence → ScalarFence), which are routed by their own registered subscribers.

> **GOTCHA —** the `0x1c7` mask is *in addition to* the `FlatHashMap` routing, not instead of it. SyncSubscriber is registered for `{80,81,82,86,87,88}` (the mask set) and the dispatch already filters to those ids; the in-body mask is a second, redundant-looking filter. Only Sync was confirmed to carry such an in-body literal mask. Every other subscriber routes **purely** by its registered set — the `FlatHashMap` is the authoritative selection structure. Do not assume other bands have a hidden bitmask (LOW that they do).

---

## The SparseCore Band (base 109)

### Purpose

The SparseCore instruction/task/sync band: ids 109..120 carrying SC tracemarks, trace-instructions, sfence/sync/barrier start-stop pairs, and the task issue→commit span. Present on gfc/glc/vfc; **absent** on pxc and vlc.

> **NOTE —** the *subscriber-bound* SC ids are 109..120, but the full SC codec band starts one lower at **id 108** (`ScInstructionCoreInterrupt`, unbound — no deepsea lambda subscriber routes it) and extends through id 132/133 plus the gfc-only PMU samples at 134/135. "Base 109" here is the lowest *routed* id, not the band floor; the complete 108..133/135 on-wire SC sub-range (and the SC-issued OCI events at 124..130 that sit inside it) is owned by [`payload-sc-band.md`](payload-sc-band.md).

### Registry

| id | event name (codec) | subscriber(s) | tracker / key | Conf. |
|---|---|---|---|---|
| 109 | `ScInstructionSetTracemark` | SC-Hlo + SC-Task + SC-Step | StepTracker (SC TraceMark) | CERTAIN |
| 110 | `ScInstructionTraceInstruction` | SC-Hlo + SC-Task + SC-Overlay + SC-OnDeviceTraceMe | OverlayTracker (SC overlay_id) | CERTAIN |
| 111 | `ScInstructionSfenceStart` | SparseCoreSyncsSubscriber | sfence span | CERTAIN |
| 112 | `ScInstructionSfenceStop` | SparseCoreSyncsSubscriber | sfence span | CERTAIN |
| 113 | `ScInstructionSyncStart` | SparseCoreSyncsSubscriber | sync span | CERTAIN |
| 114 | `ScInstructionSyncStop` | SparseCoreSyncsSubscriber | sync span | CERTAIN |
| 115 | `ScInstructionBarrierStart` | SparseCoreSyncsSubscriber | barrier span | CERTAIN |
| 116 | `ScInstructionBarrierStop` | SparseCoreSyncsSubscriber | barrier span | CERTAIN |
| 119 | `ScTaskIssueFromScs` | SC-Hlo + SC-Task | TaskTracker (task tag, ISSUE) | CERTAIN |
| 120 | `ScTaskCommitOnSct` | SC-Hlo + SC-Task | TaskTracker (task tag, COMMIT) | CERTAIN |

Adjacent codec names not bound by the SC lambda subscribers: `ScInstructionSyncWatchStart` / `ScInstructionSyncWatchStop` and `ScInstructionCoreInterrupt` (decode names present; `(unbound, HIGH)`). The SC-Syncs subscriber's id-set is written `movabs $0x7200000071 {113,114}` then grown `+0x73{115}+0x74{116}+0x6f{111}+0x70{112}`, size 6 (`movq $0x6,-0x110 @0xf22af00`).

### Subscriber roster (gfc/glc/vfc)

| # | subscriber | TracePoints set | XLine | role |
|---|---|---|---|---|
| 5 | SparseCoreHloSubscriber | {109,110,119,120} | — | SC XLA-op name |
| 6 | SparseCoreTaskSubscriber | {109,110,119,120} | — | → TaskTracker |
| 7 | SparseCoreOverlaySubscriber | {110} | 142 SC Overlay | → OverlayTracker |
| 8 | SparseCoreOnDeviceTraceMeSubscriber | {110} | 100 SC TraceMe | TraceMe span |
| 9 | SparseCoreStepSubscriber | {109} | 117 SC Steps | → StepTracker |
| 10 | SparseCoreSyncsSubscriber | {111,112,113,114,115,116} | 67 SC Syncs | sfence/sync/barrier |

> **QUIRK —** ids 109 and 110 each fan out to a *different* set of subscribers. 109 (tracemark) hits Hlo+Task+Step; 110 (trace-instruction) hits Hlo+Task+Overlay+TraceMe. The Task subscriber listens on the full `{109,110,119,120}` but only the 119/120 pair actually pairs into a span — 109/110 reach it for completeness. This is the SC analog of the TensorCore 85 fan-out.

---

## The OCI / ICI / UHI Bands (interconnect)

### Purpose

The on-chip (OCI), inter-chip (ICI), and host-bridge (UHI) interconnect bands. These carry the bulk of the on-wire event volume — message issue/receive across engines, ICI link packet flow, and UHI host-bridge traffic — but have **no dedicated semantic subscriber** in the deepsea CoreContext lambda.

### Registry (representative — OCI message events)

The OCI band's decode names form a 20-event `OciMessage*` family naming each hop of an on-chip message:

| event name (codec) | meaning | Conf. |
|---|---|---|
| `OciMessageIssuedFromTcs` | message issued from TensorCore sequencer | HIGH |
| `OciMessageIssuedFromMgr` | message issued from MGR firmware | HIGH |
| `OciMessageMsgIssuedFromEngine` / `...FromQnm` | engine / QNM issue | HIGH |
| `OciMessageSentByBc` / `...BySc` / `...ByCmnde` / `...ByHde` | egress per source block | HIGH |
| `OciMessageSentByUhiBridge` / `OciMessageReceivedByUhiBridge` | **UHI band** — host-bridge hop | HIGH |
| `OciMessageReceivedByBc` / `...BySc` / `...ByMgr` | ingress per dest block | HIGH |
| `OciMessageGeneratedInIcrEgressDma` / `...IngressDma` | ICR DMA-bridge generation | HIGH |
| `OciMessagePacketReceivedInIcr` / `OciMessagePacketSentToOci` | OCI/ICR boundary | HIGH |
| `OciMessageSyncFlagUpdateFromDstEngine` | sync-flag update carried over OCI | HIGH |

ICI link flow uses the parallel `IciPacket*` decode family: `IciPacketControlPacketInjectedByIcrDmaBridge` / `...QueuedForLocalIngress` / `...ReceivedByIcrDmaBridge`, the matching `IciPacketDataPacket*` trio, and the link-side `IciPacketPacketQueuedForLinkTransmission` / `...ReceivedOnLinkInput` / `...TransmittedOnLinkOutput`.

The **UHI band** is not a separate id range — it is the subset of OCI/ICR events naming the UHI host bridge (`*UhiBridge`). The `UhiBridge` symbol is present; the UHI events ride the OCI codec.

> **NOTE —** the per-id numeric values for the OCI/ICI/UHI bands are owned by the [codec decode table](trace-entries-coder.md) (the gappy 0..255 jump-table index). This page asserts the *names* and the *band membership*, both byte-confirmed from `.rodata`. The exact id→name numeric pairing for the interconnect bands was read from the codec, not re-derived here — see the codec page for the jump-table indices.

### Unbound bands — UHI/OCI/ICI/DMA on deepsea

The CoreContext lambda registers only the ~8..19 *semantic* subscribers (Sync/Step/Hlo/Overlay/TraceMe/Llo/ScalarFence/SparseCore*/Firmware/Throttle/Spi). The dominant interconnect and DMA bands have no entry in the `FlatHashMap`, so on the deepsea gens they fall through to the raw `TraceEventSubscriber` path: the event name becomes a `FastIntToBuffer` decimal string of the id, emitted as a generic XEvent — *or* the id is dropped. Which one, and whether a separate `DmaSubscriber` is registered on the deepsea gens (it is present only for jxc), was **not resolved** `(LOW)`. This is the single largest completeness gap in the master registry: the bands with the most on-wire traffic are the least semantically bound.

---

## The DMA Band

### Purpose

The DMA started/completed band: CMQ/CMN/HDE descriptor request + data-end events, paired by `dma_id`. On jxc this is a first-class `DmaSubscriber`; on deepsea it is unbound (above).

### Registry

| element | value | Conf. |
|---|---|---|
| request decode names | `CmnDmaRequestEastSideLane` / `...WestSideLane` / `CmnDmaRequestSet` | HIGH |
| DMA engines | CMQ (queue), CMN/CMNDE (notify), HDE (host) | HIGH |
| pairing key | `dma_id = GetDmaId()` — first descriptor = BEGIN, data-end = END | HIGH |
| jxc subscriber | `DmaSubscriber` — routes on `MemoryCommand()`/`GetDmaId()`, **not** a static id-set | MEDIUM |
| jxc HBM mux | `HbmMuxSubscriber` — HBM-mux band, routes on `MemoryCommand` | MEDIUM |

> **GOTCHA —** the jxc `DmaSubscriber` and `HbmMuxSubscriber` do not register a `TracePoints` int32 set at all. They route dynamically on the decoded `MemoryCommand()` accessor, not on the wire `trace_point_id`. A reimplementation that expects every subscriber to carry an id-set will mis-model these two — they are the exception to the registration mechanism.

---

## The Throttle Band (base 104 / 200)

### Purpose

The power-management cycle-skip band: throttle events keyed by a `RunLengthTracker<unsigned int>` that coalesces consecutive cycle-skip samples into runs. The band base is gen-dependent — **104** on vfc/vlc, **200** on gfc/glc.

### Registry

| id | event | subscriber | Conf. |
|---|---|---|---|
| 104 | ThrottleCycleSkip band base (vfc/vlc) | PowerThrottleSubscriber | HIGH |
| 200 | ThrottleCycleSkip band base (gfc/glc) | PowerThrottleSubscriber | HIGH |

The deepsea throttle decode names form a deep `ThrottleCycleSkip*` taxonomy by brake source: `...ElectricalBrakeEventCycleSkipLdidtBrake`, `...ElectricalDroopEvent...LdidtDroop`, `...ExternalBrakeEvent...ExtBrake`, `...ExternalThrottleEvent...ExtThrottle`, and the PPM family (`...PpmBrakeEventDidtAggressive/Nominal`, `...OvershootAggressive/Nominal`, `...SustainedAggressive/Nominal`, plus the rising/falling-edge variants). The `RunLengthTracker` per-sample accumulation key (which thermal/power counter line each run targets) was **not** decoded `(LOW)`. The vfc base is written `movl $0x68 = 104 @0xf2027ae`; gfc `movl $0xc8 = 200 @0xf229cee`.

---

## The MGR / Power Band (base 160)

### Purpose

The MGR firmware power/p-state band: the 160 base plus the 168/169 SPI sampler pair. These subscribers wrap `RunLengthTracker<double>` (firmware) / `<...EventBuilder>` (SPI) accumulators rather than pairing begin/end spans.

### Registry

| id | event | subscriber(s) | Conf. |
|---|---|---|---|
| 160 | `MgrFwEvent` / MGR band base | PStateTrackerSubscriber + FirmwareSubscriber (+ 2nd component FW on gfc/glc) | HIGH |
| 168 | SPI sampler (gfc/glc) | SpiSamplerSubscriber | HIGH |
| 169 | SPI sampler (gfc/glc) | SpiSamplerSubscriber | HIGH |

The 2nd `FirmwareSubscriber` (gfc/glc only) wraps `RunLengthTracker<FirmwareComponentEventBuilder>` and emits per-component power lines (kComponents 120..130 / 134..138). The `SpiSamplerSubscriber` (`SpiSamplerSubscriber` / `PowerSpiSamplerEventBuilder` confirmed) wraps `RunLengthTracker<PowerSpiSamplerEventBuilder>`, lines 118/119, id-set `{168,169}` (`movabs $0xa9000000a8 @0xf22c3aa`). vfc/vlc have **neither** the 2nd Firmware nor the SPI sampler. The MGR base is `movl $0xa0 = 160`.

---

## The pxc (BarnaCore) Gen

### Purpose

pxc (pufferfish) is the BarnaCore gen — the simplest deepsea family, **8 subscribers**, with **no SparseCore band** and no power/throttle subscribers. Its high-id space holds only the TC sync/fence/instruction bands.

### Registry

| # | subscriber | TracePoints | XLine | tracker/key |
|---|---|---|---|---|
| 1 | SyncSubscriber (threaded) | {80,81,82,86,87,88} mask 0x1c7 | 17 | sync_flag_number |
| 2 | ScalarFenceSubscriber (threaded) | {89,90} | 9 Scalar Unit | fence span |
| 3 | TensorCoreStepSubscriber (threaded) | {84} | 1 Steps | TraceMark id |
| 4 | TensorCoreHloSubscriber | {85} | 3 XLA Ops | (HLO name) |
| 5 | TensorCoreOverlaySubscriber (threaded) | {85} | 7 TC Overlay | overlay_id |
| 6 | TensorCoreOnDeviceTraceMeSubscriber | {85} | 6 XLA TraceMe | TraceMe span |
| 7 | LloOpEventSubscriber (threaded) | {85} | 8 Tensor Core | (LLO op) |
| 8 | ScalarFenceSubscriber (threaded) | {89,90} | 62 Barna Core Fence | fence span |

The 4-way fan-out of id 85 (`TCS_INTERNAL_TRACE_INSTRUCTION`) is most visible here: subscribers 4/5/6/7 all register `{85}`. Their shared `-0xc0 {85}` stack buffer is allocated once and freed once (`@0xf1ecafb`). Subscribers 2 and 8 are both `ScalarFenceSubscriber` on `{89,90}` but emit to different device lines (9 Scalar Unit vs 62 Barna Core Fence) — distinguished by the `+0x18` line field (`movl $0x9,0x18` vs `movl $0x3e,0x18 = 62`).

---

## The jxc (jellyfish) Legacy Gen

### Purpose

jxc (jellyfish) is the legacy `PerformanceTraceEntry` gen — the only family on the old codec, with a **16-bit id namespace distinct from the deepsea 0..255 band**, and the only gen with `HbmMux` + `Dma` subscribers. **10 subscribers** (lambda `@0xf1da7c0`).

### Registry (jellyfish 16-bit ids)

| # | subscriber | TracePoints (jellyfish ids) | Conf. |
|---|---|---|---|
| 1 | HbmMuxSubscriber (threaded) | (HBM-mux band; routes on `MemoryCommand`) | MEDIUM |
| 2 | DmaSubscriber | (routes on `MemoryCommand()`/`GetDmaId()`) | MEDIUM |
| 3 | SyncSubscriber (threaded) | {0xa3d,0xa3e,0xa42,0xa43,0xa44,0x93c} | HIGH |
| 4 | ScalarFenceSubscriber (threaded) | {0xa45,0xa46} = {2629,2630} | HIGH |
| 5 | TensorCoreStepSubscriber (threaded) | (TC SetTracemark, jellyfish id) | MEDIUM |
| 6 | TensorCoreHloSubscriber | (TC TraceInstruction, jellyfish id) | MEDIUM |
| 7 | TensorCoreOverlaySubscriber (threaded) | (same) | MEDIUM |
| 8 | TensorCoreOnDeviceTraceMeSubscriber | (same) | MEDIUM |
| 9 | LloOpEventSubscriber (threaded) | (same) | MEDIUM |
| 10 | ScalarFenceSubscriber (threaded) | {0xa45,0xa46} | HIGH |

The sync id-set is packed `movabs $0xa430a440a3e0a3d @0xf1db880` (16-bit halves `0xa3d/0xa3e/0xa44/0xa43`) + `movl $0x93c0a42` (`{0xa42,0x93c}`). The ScalarFence set is `movl $0xa460a45` (`{0xa45,0xa46}`). The TC single-id sets (subscribers 5..9) and the HbmMux band were **not** individually enumerated `(LOW)`; the jellyfish-id → event-name cross-walk is owned by the [jxc legacy payload page](payload-jxc-legacy.md). `jellyfish::TraceOperand` and `PerformanceTraceEntry` symbols confirmed present.

> **QUIRK —** jxc subscribers 3/4/10 reuse the same C++ subscriber *types* as the deepsea gens (SyncSubscriber, ScalarFenceSubscriber) but feed them ids from a completely different namespace. The subscriber type is portable; the id-set is not. A cross-gen routing table that keys on the deepsea band bases (80/109/160) will silently mis-route every jxc event — the only common ground is the subscriber *taxonomy*, not the ids.

---

## The Five Stateful Trackers — Match Keys

The trackers are the begin/end pairing layer above the subscribers. Each consumes a fixed id subset and pairs spans on one field; this is the analog of `SyncTracker(sync_flag_number)` and `DmaSubscriber(dma_id)`.

| tracker | feeds (trace_point_ids) | begin event | end event | MATCH KEY | Conf. |
|---|---|---|---|---|---|
| SyncTracker | 80,86,87 (+81,82,88 instant) | 86 UNSUCCESSFUL_SYNC | 87 SUCCESSFUL / 80 DMA_DONE | `sync_flag_number` | CERTAIN |
| DmaSubscriber | CMQ/CMN/HDE req + data-end | `First()`/`MemoryCommand` | `Last()`/`MemoryDataEnd` | `dma_id = GetDmaId()` | HIGH |
| StepTracker | 84 (TC) / 109 (SC) | TraceMark `0x7fffffff` | TraceMark `0x7ffffffe` | TraceMark id (state+0x8) | CERTAIN |
| TaskTracker | 119,120 (SC only) | 119 `ScTaskIssueFromScs` | 120 `ScTaskCommitOnSct` | task `tag` (FlatHashMap) | CERTAIN |
| OverlayTracker | 85 (TC) / 110 (SC) | operand kind `0xd` (open) | operand kind `0x9` (close) | `overlay_id` (state+0x8) | CERTAIN |

### StepTracker — TraceMark id, sentinel-discriminated

`StepTracker::ProcessTraceEntry<gfc> @0xf231900` reads the id and dispatches by core type: TC (id `0x54`=84, marker from `TcsInternalSetTracemark_globals_+0x18`) or SC (id `0x6d`=109, from `ScInstructionSetTracemark_globals_+0x18`). It builds a `TraceMarkEntry{+0x0 step_id, +0x8 mark_type, +0x10 gtc, +0x18 flag}` and calls the shared `StepTracker::ProcessTraceEntry @0xf2c4480`. Mark-type sentinels (cmp `@0xf2c44bd/4f1/4513/4580/4589`):

```text
0x7fffffff  step-BEGIN marker  → close any open step, open a new one keyed by the marker id
0x7ffffffe  step-END marker     → close the open step, emit StepInfo{step_id, start, end}
0x7ffffff9  intra-step marker   → annotate the open step without closing it
```

State struct: `+0x8` = current step id (the match key), `+0x10` = start gtc, `+0x28` = flag, `+0x30` = present.

### TaskTracker — task tag, FlatHashMap-keyed

`TaskTracker::ProcessTraceEntry<gfc> @0xf2394e0`. id `0x77`=119 (`ScTaskIssueFromScs`, oneof 0x4d) reads `tag` from `ScTaskIssueFromScs_globals_+0x28` and records a pending task in a `FlatHashMap<tag, pending>` (probe `mulq 0x28(%r12)`, cap `cmp 0x38(%r12)`). id `0x78`=120 (`ScTaskCommitOnSct`, oneof 0x4e) reads the matching `tag` from `+0x20`, looks it up, and on match emits `TaskInfo{+0x8 tag, +0x50 present}` via `ProcessTaskCommit @0xf2c4ce0`. Begin = 119, end = 120, key = the task `tag`. SparseCore-only.

### OverlayTracker — overlay_id operand

`OverlayTracker::ProcessTraceEntry<gfc> @0xf2335c0` dispatches on the core-type selector (state+0x0: 2=TC, 5=SC), filters id + `block_id` (`+0x1c` against state+0x4), and extracts the operand: TC id `0x55`=85 (operand from `TcsInternalTraceInstruction_globals_+0x18`) / SC id `0x6e`=110 (from `ScInstructionTraceInstruction_globals_+0x18`). `OverlayTracker::ProcessTraceOperand @0xf2c3e40` branches on operand kind:

```text
0xd  overlay OPEN   → record overlay_id at state+0x8 (match key), active flag +0xc, start gtc +0x10
0x9  overlay CLOSE  → emit OverlayInfo{overlay_id, start, end=gtc}, clear +0x8/+0x10
```

Both ride on id 85 (TC) / 110 (SC); the open/close discriminant is the operand kind, not a separate id.

---

## Relevant Struct / Table Offsets

| Structure | Layout | Conf. |
|---|---|---|
| `TracePoints<TraceEntry>` | `std::vector<int32>` {ptr@+0x0, size@+0x8, cap@+0x10}; built on stack, freed after each register | CERTAIN |
| `CoreDispatcher` map | `FlatHashMap<u16 id (TraceHeader+0x18), vector<shared_ptr<TraceEventSubscriber>>>` | CERTAIN |
| `TraceEventSubscriber` base | +0x08/+0x0c core_id/chip_id filter; +0x10 `TpuXPlaneBuilder*`; +0x18 per-id `XEventMetadata*` cache / line field | HIGH |
| `ThreadedSubscriber` | vtable + `ClosureThread` worker (`ThreadLoop @0xf1ede60` pxc) at +0xa0; wrapped vtable set inline | HIGH |
| `StepTracker` state | +0x8 step id (key), +0x10 start gtc, +0x28 flag, +0x30 present | CERTAIN |
| `TaskTracker` state | `FlatHashMap<tag, pending>`; `TaskInfo{+0x8 tag, +0x50 present}` | HIGH |
| `OverlayTracker` state | +0x8 overlay_id (key), +0xc active flag, +0x10 start gtc | CERTAIN |
| sync id rodata | `@0xa2d6b40` = {81,82,88,87}; +{86,80} via `movabs $0x5000000056` | CERTAIN |

---

## Cross-References

- [TraceEntriesCoder](trace-entries-coder.md) — the 16-byte packet codec; owns the id→`Decode<Name>()` jump table and the dual id-space dispatch
- [Profiling overview](overview.md) — where the registry sits in the device-trace pipeline
- [Payload — UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) — per-id field decode for the interconnect and DMA bands
- [Payload — SparseCore band](payload-sc-band.md) — field decode for ids 109..120
- [Payload — vfc/vlc/gfc](payload-vfc-vlc-gfc.md) — field decode for the sync/throttle/MGR bands
- [Payload — jxc legacy](payload-jxc-legacy.md) — the jellyfish 16-bit id namespace and `PerformanceTraceEntry` field decode
- [XEvent metadata ids](xevent-metadata-ids.md) — the subscriber→XEvent metadata naming the registry feeds into
- [TraceEntry → XEvent](trace-entry-to-xevent.md) — the subscriber `ProcessTraceEntry` → XEvent shaping downstream of dispatch
