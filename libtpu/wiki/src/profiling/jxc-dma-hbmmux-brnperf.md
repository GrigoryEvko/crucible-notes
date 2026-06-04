# jxc DMA / HbmMux / brn_perf Bands

> *All addresses on this page apply to `libtpu.so` v0.0.40 (libtpu-0.0.40-cp314, build-id `89edbbe81c5b328a958fe628a9f2207d`). VMAs equal file offsets in `.text`/`.rodata`; `.data.rel.ro` sits at VMA −0x200000 from file offset. Other versions will differ.*

## Abstract

This page decodes the three oldest-generation (Jellyfish, codec family `jxc`) device-trace bands that profile data movement: the **Dma** band (per-engine DMA transfer spans linked by a flow id), the **HbmMux** band (the HBM-multiplexer direction FSM), and the **brn_perf1 / brn_perf2** bands (BarnaCore performance counters). All three are consumed by template specializations of the XProf subscribers over `asic_sw::driver::deepsea::jxc::PerformanceTraceEntry` — the proto2, self-describing trace record that predates the bit-packed packet codecs of the deepsea (`pxc`) and SparseCore generations. The proto2 path that classifies a `PerformanceTraceEntry` into these bands is documented separately on [jxc Legacy Payload](payload-jxc-legacy.md); this page owns the DMA XStat emission, the HbmMux state machine, and the BarnaCore perf payloads.

Three subsystems share one shape. The **Dma** subscriber (`DmaSubscriber<jxc>::ProcessTraceEntry` @ `0xf1dfee0`) pairs a `*_COMMAND` begin trace entry with its matching `*_DATA_END` completion through a `FlatHashMap` keyed by a synthetic `dma_id`, then draws an XEvent on a per-engine XLine carrying an XProf **flow** stat so the profiler renders an arrow from begin to end. The **HbmMux** subscriber (`HbmMuxSubscriber<jxc>::ProcessTraceEntry` @ `0xf1def00`) is a four-symbol open/close FSM that times how long the HBM read/write multiplexer pointed at the BFIFO versus the Node Fabric. The **brn_perf** bands are reflection-driven perf records for the three fixed BarnaCore reduce operators (`brn_perf1`) and the sixteen DMA channel controllers (`brn_perf2`).

The page is the runtime-observability fingerprint of BarnaCore as a live engine: distinct profiler bands for an HBM mux, three fused reduce operators, and sixteen stream channel controllers are exactly the microarchitecture that the SparseCore generations replaced (the TpuComponent enum keeps the ordinals; no v3+ trace populates them). For reimplementation, the contract is:

- The **Dma flow protocol**: the COMMAND/DATA_END gate masks, the `(nf.id − 3)` engine switch, the per-engine XLine and Read/Write/Receive display name, and the `flow` XStat value formula.
- The **`GetDmaId` composite key**: the exact OR/shift bit layout that pairs a begin to its end, and which proto2 fields feed each bit window.
- The **HbmMux FSM**: the four `fsm` symbols, the open/close pairing, and the two metadata-selected event names.
- The **nf_descriptor 3-channel sync-flag payload** and the **brn_perf1/brn_perf2 field tables** with their per-id XLine assignment.

| | |
|---|---|
| **Dma subscriber** | `xprof::tpu::DmaSubscriber<jxc::PerformanceTraceEntry>::ProcessTraceEntry` @ `0xf1dfee0` |
| **HbmMux subscriber** | `xprof::tpu::HbmMuxSubscriber<jxc::PerformanceTraceEntry>::ProcessTraceEntry` @ `0xf1def00` |
| **DMA-id composer** | `TraceEntryWrapper<jxc>::GetDmaId` @ `0xf698180` |
| **COMMAND / DATA_END gates** | `MemoryCommand` @ `0xf698560` (mask `0x56B6D8`) / `MemoryDataEnd` @ `0xf6985a0` (mask `0x894920`) |
| **Flow stat** | `StatType` 56 = `"flow"`; value `((dma_id & 0x00FFFFFFFFFFFFFF) << 2) \| 3` |
| **HbmMux XLine** | `TpuComponent` 56 = `"HBM Mux"` (`TpuComponentName` @ `0x1c8ebb60`) |
| **Record discriminator** | `EntryDataCase` at submsg-ptr `+0x30`; nf band sub-id at submsg `+0x30` (alias) |

---

## Dma Band — Per-Engine Transfer Spans

### Purpose

The Dma band turns a stream of DMA trace entries into per-engine timelines where each transfer appears as one duration XEvent, and a begin/end pair is visually linked by an XProf flow arrow. There is no byte-count in the span itself — the engine and direction are encoded by *which XLine* the span lands on and by the event's *display name* ("Read" / "Write" / "Receive"); the begin↔end pairing is carried by the `flow` stat.

### Entry Point

```text
DmaSubscriber<jxc>::ProcessTraceEntry  (0xf1dfee0)
  ├─ CoreId(entry)                      (chip/core filter: a1+8 vs a1+12)
  ├─ MemoryCommand() | MemoryDataEnd()  (0xf698560 / 0xf6985a0 — gate)
  ├─ switch (nf.id - 3)                 (jt @ 0xab531a4, arms 0..0x14)
  │     └─ sets XLine r15d + display-name blob + kind r14
  ├─ GetDmaId()                         (0xf698180 — FlatHashMap key)
  ├─ find_or_prepare_insert_large       (0xf1e05e0 — pending-begin map)
  ├─ GetOrCreateLine(XLine)             (0xf1df120)
  ├─ AddEvent                           (0xf1df1e0)
  └─ AddStatValue (flow)                (XStatsBuilder; metadata at a1+0x18)
```

### Algorithm

```c
function DmaSubscriber_ProcessTraceEntry(self, entry):       // 0xf1dfee0
    if CoreId(entry) != {self+8, self+12}: return            // chip/core filter
    if !MemoryCommand(entry) && !MemoryDataEnd(entry): return // not a DMA edge
    if EntryDataCase(entry) != 6: return                     // must be the nf band
    arm = nf.id - 3                                          // nf.id at nf-submsg+0x30
    switch arm:                                              // jt @ 0xab531a4
        case 0:           xline=57; name="Read";    kind=4   // HBM_READ_COMMAND
        case 1,2:         xline=57; name="Write";   kind=5   // HBM_WRITE cmd/data-end
        case 3,6:         xline=19; name="Read";    kind=4   // VMEM read cmds
        case 4,5,7,8:     xline=19; name="Write";   kind=5   // VMEM write cmd/data-end
        case 9:           xline=20; name="Read";    kind=4   // SMEM_READ_COMMAND
        case 10,11:       xline=20; name="Write";   kind=5   // SMEM write cmd/data-end
        case 12,13:       xline=18; name="Write";   kind=5   // IMEM write cmd/data-end
        case 17:          xline=51; name="Receive"; kind=7   // HIB_WRITE_RECEIVE
        case 19,20:       xline=52; name="Write";   kind=5   // HIB write cmd/data-end
        default: return                                      // BMEM/ICI_SEND_END dropped
    dma_id = GetDmaId(entry)                                 // 0xf698180
    if presence_byte != 1: return                            // no id -> drop

    slot = map[dma_id]                                       // FlatHashMap<dma_id, vector<entry>>
    if MemoryCommand(entry) && First(entry):                 // 0xf698620
        slot = [entry]                                       // open a pending begin
    else:
        slot.push_back(entry)
    if kind != 5: return                                     // only the DATA_END family closes
    is_not_write = (load32(name) ^ 0x74697257)               // "Writ" LE
                 | (load8(name+4) ^ 0x65) != 0               // 'e' -> "Write" compare @ 0xf1e0152
    // close only on a Write-labelled DATA_END with a pending begin
    if MemoryDataEnd(entry) ... :
        if Last(entry) && slot.nonempty():                   // 0xf698660
            begin = slot[0]
            start = begin.gtc                                // *(begin+16)+24
            dur   = entry.gtc - start
            line  = GetOrCreateLine(self.builder, xline)     // 0xf1df120
            ev    = AddEvent(line, start, dur, name)         // 0xf1df1e0
            flow  = ((dma_id & 0x00FFFFFFFFFFFFFF) << 2) | 3 // lea 0x3(,rax,4)
            AddStatValue(ev, self.flow_metadata /*+0x18*/, flow)
            map.erase(dma_id)                                // 0xf1e05a0
```

> **GOTCHA —** the close path is gated on a *string compare against `"Write"`*, not on the trace-point kind alone. At `0xf1e0152` the subscriber XORs the first four display-name bytes with `0x74697257` ("Writ") and the fifth with `0x65` ('e'). A reimplementation that closes spans purely on the `*_DATA_END` trace-point id will mis-handle the read paths (which never carry a "Write" name) and the `"Receive"` HIB path (kind 7). The discriminator is the *label*, set per-arm.

### Per-Engine Map

The seventeen DMA edges resolve to six XLines. `XLine` is a `TpuComponent` ordinal, decoded from `TpuComponentName` @ `0x1c8ebb60`; the display-name blobs live at `off_21643E10/E20/E30` (`R_X86_64_RELATIVE` addends → `"Receive"`/`"Read"`/`"Write"`).

| arm (`nf.id`) | Event | XLine | Display name | kind | Confidence |
|---|---|---|---|---|---|
| 0 (3) | HBM_READ_COMMAND | 57 `"HBM"` | Read | 4 | High |
| 1 (4) / 2 (5) | HBM_WRITE cmd / data-end | 57 `"HBM"` | Write | 5 | High |
| 3 (6) / 4 (7) / 5 (8) | VMEM↔HBM read/write cmd / data-end | 19 `"Tensor Core VMEM"` | Read/Write | 4/5 | High |
| 6 (9) / 7 (10) / 8 (11) | VMEM↔ICI read/write cmd / data-end | 19 `"Tensor Core VMEM"` | Read/Write | 4/5 | High |
| 9 (12) / 10 (13) / 11 (14) | SMEM read/write cmd / data-end | 20 `"Tensor Core SMEM"` | Read/Write | 4/5 | High |
| 12 (15) / 13 (16) | IMEM write cmd / data-end | 18 `"Tensor Core IMEM"` | Write | 5 | High |
| 17 (20) | HIB_WRITE_RECEIVE | 51 `"From Host Interface"` | Receive | 7 | High |
| 19 (22) / 20 (23) | HIB write cmd / data-end | 52 `"To Host Interface"` | Write | 5 | High |

> **NOTE —** `nf.id` 17/18/19 (BMEM) and 27 (ICI_SEND_END) have switch arms that route to the drop exit — BMEM has a `GetDmaId` composer but the Dma subscriber never registers it. Reads (`First`) open a pending begin; the matching write/receive `DATA_END` (`Last`) closes it. The byte count of a transfer is *not* in the flow stat; it is available separately via `GetDmaSize` @ `0xf6982a0` (`length << 10`, 1 KiB units — `EntryDataCase` 3 reads `source_offset+0x48`, case 19 reads `+0x2c`).

### The Flow Stat

The single XStat the Dma subscriber emits is `StatType` 56, name `"flow"` (resolved through `GetStatTypeMap` @ `0x1cf8c660`; the subscriber's `StatMetadata` is cached at object offset `+0x18` and passed to `AddStatValue` as `*(self+0x18)` via the builder at `self+0x18`). The value is built inline:

```text
flow_value = ((dma_id & 0x00FFFFFFFFFFFFFF) << 2) | 3      // lea 0x3(,rax,4) over 56-bit mask
```

The low 56 bits of `dma_id` are the flow identity; the low two-bit tag `3` marks a both-ends flow link. Because the begin and end XEvents of one transfer share the same `dma_id`, they share the same flow id, and XProf draws the arrow. `dma_id` is the `FlatHashMap` key, so the pairing and the flow rendering use one composite key.

---

## GetDmaId — The Begin/End Pairing Key

### Purpose

`GetDmaId` @ `0xf698180` derives the synthetic 27-bit key that pairs a `*_COMMAND` with its `*_DATA_END`. It is the `jxc` proto2-field analog of the deepsea bit-packed composer `TraceEntryWrapper<pxc>::GetDmaId(int)` @ `0xf699ca0`: where the deepsea path slices bit windows out of a 16-byte packet, the `jxc` path folds proto2 message fields.

> **CORRECTION (JXC-DMA-1) —** the composer's switch dispatches on the **`EntryDataCase`** discriminator (`*(submsg_ptr + 0x30)`, the proto2 oneof tag), **not** on `(nf.id − 3)`. The `(nf.id − 3)` switch is the *Dma subscriber's* engine selector (`0xf1dfee0`); `GetDmaId` is a separate function whose `case 3` reads the `nf_descriptor` layout, `case 4/5/6/8` read the cmd/data-end layouts, and `case 0x12/0x13` read two further oneof arms. Cases `7,9..0x11` jump straight to the composite-merge label (`0xf69824e`) without ever loading a field — at entry the function zeroes `eax`/`edx` (`xor eax,eax; xor edx,edx`), so the merge (`movzbl al; or ecx`) folds `0 | 0` and these arms return `0`, *not* `id & 0xff`. Treat the two switches as distinct dispatch keys.

### Algorithm

```c
function GetDmaId(self):                                    // 0xf698180
    msg = *(self + 16)
    a = 0; node = 0                                         // xor eax,eax; xor edx,edx at entry
    switch EntryDataCase(msg):                              // *(msg + 0x30)
        case 3:   // nf_descriptor
            a    = field[8]   // trace_id   (+0x20)
            node = field[9]   // node_id    (+0x24)
            rsrc = field[32]  // descriptor_source (+0x80)
            chip = field[10]  // chip_id    (+0x28)
            goto TAIL_A
        case 4: case 6:       // command arms
            a    = field[7]; node = field[8]; rsrc = field[13]
            chip = field[9];  goto TAIL_A
        case 5:               // data-end arm
            a    = field[7]; node = field[8]; rsrc = field[14]
            chip = field[9];  goto TAIL_A
        case 8:               // vmem-hbm data-end arm
            a = field[7]; chip = field[8]; rsrc = field[21]; node = field[9]
            goto TAIL_A
        case 0x12: a=field[8]; node=field[6]; rsrc=field[17]; chip=*(msg+32); goto TAIL_A
        case 0x13: a=field[7]; node=field[6]; rsrc=field[15]; chip=*(msg+32); goto TAIL_A
        case 7,9,10,11,12,13,14,15,16,17:                   // simple
            return 0                                        // a/edx never reassigned; eax=0 at entry
        default: return 0
    TAIL_A:
        mid  = (a & 0x1F00) | ((rsrc & 3) << 13) | ((node << 15) & 0xFFFF)
        full = mid | ((chip << 16) & 0x7FF0000)
        return full | (a & 0xff)
```

### Bit Layout

A `TAIL_A` `dma_id` packs four proto2 fields into 27 bits:

```text
  bits  0.. 7 : trace_id[0:8]     (per-transfer tag low byte)
  bits  8..12 : trace_id[8:13]    (a & 0x1F00)
  bits 13..14 : resource[0:2]     (source/descriptor/dest resource, (rsrc & 3) << 13)
  bit  15     : node_id[0]        (tensor-node selector, (node << 15) & 0x8000)
  bits 16..26 : chip_id[0:11]     (chip in pod, (chip << 16) & 0x7FF0000)
```

| Bit window | Field (`EntryDataCase` 3) | Source field | Confidence |
|---|---|---|---|
| 0..12 | `trace_id` | `field[8]` (+0x20) | High |
| 13..14 | `resource` | `descriptor_source` `field[32]` (+0x80) | High |
| 15 | `node_id` | `field[9]` (+0x24) | High |
| 16..26 | `chip_id` | `field[10]` (+0x28) | High |

> **QUIRK —** a command and its data-end read the *resource* slot from different proto fields (e.g. a command's `descriptor_source` vs a data-end's `destination_*` field) yet still collide, because the dominant bits (`trace_id` + `node` + `chip`) are identical across the pair. The pairing invariant — same `trace_id` ⇒ same `dma_id` — is decoded here, but a *proof* that the 2-bit resource slot is always equal across a pair is a property of the firmware emitter, not the decoder (it is observable only on a captured trace). Treat begin/end collision as a firmware contract, not a decoder guarantee.

---

## HbmMux Band — The HBM-Multiplexer FSM

### Purpose

The HbmMux band times the HBM read/write multiplexer: how long it stays pointed at the BFIFO versus the Node Fabric. It is the on-device observability counterpart of the `EnableBarnaCoreHbmMuxWorkaround` / `SetBarnaCoreHbmMux*ModeTimer` TpuCore configuration — the BarnaCore↔HBM mux that SparseCore deleted, surfaced here as a single XLine (`TpuComponent` 56, `"HBM Mux"`).

### Entry Point

```text
HbmMuxSubscriber<jxc>::ProcessTraceEntry  (0xf1def00)
  ├─ CoreId filter                         (a1+8)
  ├─ HbmMuxSwitchState()                   (0xf6986e0 — returns 0x100000000 | fsm)
  ├─ fsm in {1,2}  -> open marker          (store prev entry, set dir)
  ├─ fsm == 3      -> close BFIFO->NF span  (emit, metadata a1+0x20)
  └─ fsm == 0      -> close NF->BFIFO span  (emit, metadata a1+0x18)
```

### Algorithm

The fsm symbol is `hbm_mux_switch_trace_entry.fsm` (proto field 3, submsg `+0x1c`). `HbmMuxSwitchState` @ `0xf6986e0` returns `(0x100000000 | fsm)` for `EntryDataCase == 7`, else 0 — bit 32 is the present flag. The subscriber object holds `+0x28` = prev-entry pointer, `+0x30` = its refcount handle, `+0x38` = the currently-open direction (0 = none, 1 = BFIFO→NF, 2 = NF→BFIFO).

```c
function HbmMuxSubscriber_ProcessTraceEntry(self, entry):   // 0xf1def00
    if CoreId(entry) != self+8: return
    s = HbmMuxSwitchState(entry)                            // 0xf6986e0
    if (s & 0x100000000) == 0: return                       // not an HbmMux entry
    fsm = (uint32)s
    if (fsm - 1) < 2:                                        // fsm in {1,2} -> OPEN
        self.prev      = entry        // +0x28
        self.refcount  = entry.rc     // +0x30
        self.open_dir  = fsm          // +0x38 (1 or 2)
        return                        // open marker does NOT emit
    if fsm == 3:                                            // CLOSE BFIFO->NF
        if self.open_dir != 1: { clear(); return }
        start = self.prev.gtc - (DurationCycles(self.prev) << 4)
        dur   = entry.gtc - start
        line  = GetOrCreateLine(self.builder, 56)           // "HBM Mux"
        AddEvent(line, start, dur, self.meta_nf_to_bfifo /*+0x20*/)
        clear()                                             // zero +0x28/+0x30, reset +0x38
    else if fsm == 0:                                       // CLOSE NF->BFIFO
        if self.open_dir != 2: { clear(); return }
        start = self.prev.gtc - (DurationCycles(self.prev) << 4)
        dur   = entry.gtc - start
        AddEvent(GetOrCreateLine(self.builder, 56), start, dur,
                 self.meta_bfifo_to_nf /*+0x18*/)
        clear()
```

> **CORRECTION (JXC-DMA-2) —** the FSM is a **four-symbol open/close machine**, not a two-state toggle. `{1,2}` open a direction span; `{0,3}` close it. State 3 is *not* a third mux mode — it is the close marker for the direction that `fsm==1` opened, exactly as `fsm==0` closes what `fsm==2` opened. The two pairs:
>
> ```text
> fsm 1 = open(BFIFO->NF)  ...  fsm 3 = close  -> emit "Node Fabric to BFIFO" (meta +0x20)
> fsm 2 = open(NF->BFIFO)  ...  fsm 0 = close  -> emit "BFIFO to Node Fabric" (meta +0x18)
> ```

The duration math subtracts `DurationCycles(prev) << 4` (cycle→subtick scale, ×16) from the prior entry's GTC to recover the span start. `DurationCycles` @ `0xf698720` reads `length+0x20` for `EntryDataCase` 13/14 and `+0x24` for case 12. Both close arms emit on XLine 56. The event-name metadata is pre-built and cached: `self+0x18` = `"BFIFO to Node Fabric"`, `self+0x20` = `"Node Fabric to BFIFO"` (strings present in `.rodata`).

### nf_descriptor — The 3-Channel Sync-Flag Payload

The richer cousin of the nf band, `nf_descriptor_trace_entry` (`EntryDataCase` 3, 27 fields), carries a full src/dst endpoint plus three independent sync-flag-update channels and multicast/segmented flags — the on-wire view of a staged Node-Fabric DMA descriptor. Three accessors surface the sync-flag targets, each gated on its channel's enable field:

| Accessor | Address | Gate field | Packs | Confidence |
|---|---|---|---|---|
| `SourceSyncFlagTarget` | `0xf6982e0` | `source_update` (+0x60) | `{node_id (+0x24), source_update_sync_flag (+0x64)}` via OCI fold | High |
| `DestinationSyncFlagTarget` | `0xf698340` | `destination_update` (+0x54) | explicit pack (below) | High |
| `AckSyncFlagTarget` | `0xf6983a0` | `ack_update` (+0x6c) | `{node_id (+0x24), ack_update_sync_flag (+0x70)}` via OCI fold | High |

`SourceSyncFlagTarget` and `AckSyncFlagTarget` load two 32-bit fields into the low quadwords of an XMM register and fold them with the OCI SyncFlag-target packer (`vpmulld` against `xmmword_A2C2560`, `vpand` mask `xmmword_A2D5E00`, then a horizontal OR reduction) — the same packer the OCI bands use. `DestinationSyncFlagTarget` packs explicitly:

```text
target = (dest_update_sync_flag (+0x58) & 0x3FF)
       | ((dest_update_resource (+0x5c) & 1) << 10)
       | ((destination_node_id  (+0x40) & 1) << 11)
       | ((destination_chip_id  (+0x44) << 12) & 0x7FF000)
```

The destination raises a "data arrived" flag, the source raises a "buffer free" flag, the ack raises a "completion" flag — the three-way sync handshake of a cross-chip Node-Fabric DMA, the `jxc` analog of the deepsea OCI descriptor.

---

## brn_perf Bands — BarnaCore Performance Counters

### Purpose

`brn_perf1` and `brn_perf2` are reflection-driven perf records: one per BarnaCore FSM operation. `brn_perf1` profiles the three fixed-function reduce operators; `brn_perf2` profiles the sixteen DMA channel controllers. The field names are not `StatType` enum entries — they are inline `GetOrCreateStatMetadata(string_view)` names taken from the embedded proto field names — and the builder walks them by reflection in the V1 converter (`ConvertTpuTraceToXPlane<jxc>` @ `0xf23f8c0` region).

### brn_perf1 — Three Reduce Operators

`brn_perf1_trace_entry` (`EntryDataCase` 13, `id` at `+0x38`). TracePoint ids and field names are byte-exact from the embedded `FileDescriptorProto`; all field name strings (`cycles_of_execution`, `input0_stall_cycles`, `input1_stall_cycles`, `output_stall_cycles`, `sync_flag_location`, `is_sync_update`) are present in `.rodata`.

| TracePoint | id | XLine (`TpuComponent`) | Confidence |
|---|---|---|---|
| `CONCAT` | 109 (0x6d) | 24 `"Barna Core Concat"` | High |
| `PROCESS_HOSTID` | 110 (0x6e) | 25 `"Barna Core Process Host ID"` | High |
| `SPARSE_REDUCE` | 111 (0x6f) | 26 `"Barna Core Sparse Reduce"` | High |

The shape is **2-input / 1-output** (`input0_stall_cycles`, `input1_stall_cycles`, `output_stall_cycles`) — the embedding-gather reduce topology: two gathered streams in, one reduced stream out. `cycles_of_execution` is the total run time; the stall fields count cycles blocked on each stream; `sync_flag_location` + `is_sync_update` name the flag the op raises on completion.

### brn_perf2 — Sixteen Channel Controllers

`brn_perf2_trace_entry` (`EntryDataCase` 14, `id` at `+0x38`). Same C++ field shape as `brn_perf1` but with an **inverted stall topology**: one input stall (`input_stall_cycles`) and two output stalls (`output0_stall_cycles`, `output1_stall_cycles`) — a channel controller pulls one descriptor stream in and fans it to two output queues.

| TracePoint | id range | XLine | Confidence |
|---|---|---|---|
| `CHANNEL0..7` | 100..107 (0x64..0x6b) | 28..35 `"Barna Core Channel 0..7"` | High |
| `PROCESS_BRNID` | 108 (0x6c) | 27 `"Barna Core Process BRN ID"` | High |
| `CHANNEL8..15` | 114..121 (0x72..0x79) | 36..43 `"Barna Core Channel 8..15"` | High |

Channel `n` → XLine `28 + n` (`TpuComponentName` cases 28..43 are contiguous `"Barna Core Channel 0".."15"`). The id range has a gap (107 → 114) bridged by `PROCESS_BRNID` (108) on its own XLine 27 — the chip-in-pod routing step that bookends a channel burst (exact role not isolated, Inferred).

> **NOTE —** the existence of these bands is the profiler-layer fingerprint of BarnaCore as a *live* engine on Jellyfish: a small fixed set of fused reduce operators, sixteen stream channel controllers, and an HBM mux, all driven by a hardwired sync FSM. SparseCore replaced exactly this — the three reduce ops became the TEC programmable reduce, the sixteen channel controllers became the TAC/TEC stream-gather/scatter engines, the HBM mux became the random-access MMU. The `TpuComponent` enum preserves the ordinals on v3+, but no v3+ trace populates them — the same "preserve the enum, delete the implementation" vestige pattern seen across the codec layers.

---

## Band → XLine Map

The complete `jxc` DMA/HbmMux/BarnaCore band-to-XLine assignment (`TpuComponent` decoded from `TpuComponentName` @ `0x1c8ebb60`):

| `TpuComponent` | XLine name | Band / source | Confidence |
|---|---|---|---|
| 18 | Tensor Core IMEM | nf IMEM cmd/data-end (Dma) | High |
| 19 | Tensor Core VMEM | nf VMEM↔HBM / VMEM↔ICI (Dma) | High |
| 20 | Tensor Core SMEM | nf SMEM cmd/data-end (Dma) | High |
| 24 | Barna Core Concat | brn_perf1 CONCAT(109) | High |
| 25 | Barna Core Process Host ID | brn_perf1 PROCESS_HOSTID(110) | High |
| 26 | Barna Core Sparse Reduce | brn_perf1 SPARSE_REDUCE(111) | High |
| 27 | Barna Core Process BRN ID | brn_perf2 PROCESS_BRNID(108) | High |
| 28..43 | Barna Core Channel 0..15 | brn_perf2 CHANNEL0..15 | High |
| 51 | From Host Interface | nf HIB_WRITE_RECEIVE (Dma) | High |
| 52 | To Host Interface | nf HIB write cmd/data-end (Dma) | High |
| 56 | HBM Mux | hbm_mux_switch EVENT (HbmMux FSM) | High |
| 57 | HBM | nf HBM read/write cmd/data-end (Dma) | High |

---

## Infrastructure Functions

| Function | Address | Role | Confidence |
|---|---|---|---|
| `DmaSubscriber<jxc>::ProcessTraceEntry` | `0xf1dfee0` | Dma band span builder + flow stat | High |
| `HbmMuxSubscriber<jxc>::ProcessTraceEntry` | `0xf1def00` | HbmMux open/close FSM span builder | High |
| `GetDmaId` | `0xf698180` | Composite begin/end pairing key | High |
| `MemoryCommand` | `0xf698560` | COMMAND gate (mask `0x56B6D8`, `nf.id ≤ 0x16`) | High |
| `MemoryDataEnd` | `0xf6985a0` | DATA_END gate (mask `0x894920`, `nf.id ≤ 0x17`) | High |
| `HbmMuxSwitchState` | `0xf6986e0` | fsm read (`+0x1c`), returns `0x100000000 \| fsm` | High |
| `DurationCycles` | `0xf698720` | cycle gap (cases 13/14 → `+0x20`, 12 → `+0x24`) | High |
| `GetDmaSize` | `0xf6982a0` | transfer size `length << 10` (cases 3/19) | High |
| `First` / `Last` | `0xf698620` / `0xf698660` | begin/end markers (cases 5/6/8) | High |
| `SourceSyncFlagTarget` | `0xf6982e0` | nf_descriptor source sync flag | High |
| `DestinationSyncFlagTarget` | `0xf698340` | nf_descriptor destination sync flag | High |
| `AckSyncFlagTarget` | `0xf6983a0` | nf_descriptor ack sync flag | High |
| `TpuComponentName` | `0x1c8ebb60` | `TpuComponent` ordinal → XLine name | High |
| `GetStatTypeMap` | `0x1cf8c660` | `StatType` 56 = `"flow"` | High |

---

## Cross-References

- [jxc Legacy Payload](payload-jxc-legacy.md) — the proto2 `PerformanceTraceEntry` classifier (`FromTraceEntry`) that produces the `EntryDataCase`/`nf.id` discriminators this page consumes
- [SparseCore Band](payload-sc-band.md) — the SparseCore-era replacement for the BarnaCore reduce/channel-controller profiler bands
- [UHI / OCI / ICI DMA Payloads](payload-uhi-oci-ici-dma.md) — the deepsea/modern DMA bands; the `GetDmaId` and OCI sync-flag-fold cross-gen analogs
- [DMA Endpoint Rendering](dma-endpoint-rendering.md) — how DMA spans and endpoints surface on the timeline
- [ICR DMA Timeline Band](icr-dma-timeline-band.md) — sibling DMA timeline band on a newer generation
- [Profiling Overview](overview.md) — the subscriber/XLine architecture these bands plug into
