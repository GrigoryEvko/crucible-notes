# Payload: jxc Legacy Trace (`PerformanceTraceEntry`)

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text`/`.rodata` VMA equals file offset. Other versions will differ.*

## Abstract

jxc — **Jellyfish**, the oldest profiled TPU generation — does not use the fixed-width [`TraceEntriesCoder`](trace-entries-coder.md) packet that every later family decodes. Its on-device trace ring is decoded into a **self-describing proto2 message**, `asic_sw::driver::deepsea::jxc::PerformanceTraceEntry`, by a different `DecodeTraceBuffers` instantiation. Where the deepsea gens (pxc/vfc/vlc/glc/gfc) read a constant **16-byte bit-packed packet** — 2-bit framing, a 59-bit `TraceHeader`, a `GetBits64` width sequence, and a per-event consumed-bit `CHECK` — jxc parses an ordinary proto2 record: `timestamp` (field 1, the GTC cycle stamp), `chip_id` (field 2), and a `oneof entry_data` (fields 3..19) that selects one of **17 band sub-messages**, each carrying its own `id` enum (the `TracePoint`) plus typed fields. There is no `GetBits` sequence and no consumed-bit `CHECK` on jxc; the wire is the proto2 message layout itself.

The single fact that unifies jxc with the modern path is the **routing key**. The deepsea codec dispatches decode on the 8-bit on-wire `trace_point_id` and encode on the dense oneof field. jxc has no on-wire id byte at all — so `TracePoint<jxc>::FromTraceEntry` *synthesizes* a 16-bit key by packing the oneof case into the high byte and the band's local `id` enum into the low byte: `key = (EntryDataCase << 8) | (id & 0xff)`. This is the key the [tracepoints registry](tracepoints-master-registry.md) records in the jxc `0x9xx`/`0xaxx` range. Above that key the subscriber layer is **uniform** with the modern gens: the cross-gen `TraceEntryWrapper<jxc>` exposes the same `CoreId()`/`MemoryCommand()`/`GetDmaId()`/`SyncFlagValue()` accessor surface, so `CoreDispatcher<jxc>::Dispatch` is a byte-for-byte structural twin of the deepsea dispatcher — only the wire format and the key composition differ.

This page owns the **jxc legacy `PerformanceTraceEntry` wire format** (the 17-band oneof, the per-band `TracePoint` enums and payload fields, the selector enum value tables, and the packed-key composition) and the **two jxc-unique trace subscribers** — `HbmMuxSubscriber` (the HBM read/write multiplexer occupancy band, on its own XLine) and `DmaSubscriber` (the Node-Fabric DMA command/data-end band, paired by a synthetic `dma_id`). Both are bands the newer gens fold into their ICI/intra-DMA payloads, so they have no analog on [UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) or [vfc/vlc/gfc](payload-vfc-vlc-gfc.md). The fixed-16-byte codec contrast is on [TraceEntriesCoder](trace-entries-coder.md).

For reimplementation, the contract is:

- **The proto2 record, not a bit packet** — `PerformanceTraceEntry` is parsed by the standard proto2 runtime; band fields are message-layout offsets, not a `GetBits64` width sequence. The codec is chosen as a distinct `std::variant` alternative inside `GetTraceCodec`.
- **The synthetic packed routing key** `key = (EntryDataCase << 8) | (band.id & 0xff)`, composed by `FromTraceEntry` from the per-band `id`-field offset — the jxc analog of the modern dual dispatch, collapsed onto one 16-bit value.
- **The 17 bands** — each band's oneof field number, its `TracePoint` enum (the local 16-bit id range), its named payload fields, and the selector enum value tables.
- **The two unique subscribers** — `HbmMux` (single key `0x728`, a 2-state switch tracker on XLine 56), and `Dma` (17 nf-band keys `0x603..0x617`, paired through a `FlatHashMap<dma_id, pending-begin>` keyed on `GetDmaId()`).

| | |
|---|---|
| **Decoded message** | `asic_sw::driver::deepsea::jxc::PerformanceTraceEntry` (proto2; **self-describing**, not bit-packed) |
| **Container layout** | field 1 `timestamp` (u64, the GTC cycle stamp); field 2 `chip_id` (u32); `oneof entry_data` fields **3..19** (the 17 band sub-messages) |
| **Codec selection** | `xprof::tpu::DecodeTrace(DeviceType, JfTrace*)` @ `0xf59dba0` → `GetTraceCodec` @ `0xf5a2900` (variant alternative `unique_ptr<TraceCodecInterface<…jxc::PerformanceTraceEntry>>`) |
| **Routing key** | `TracePoint<jxc>::FromTraceEntry` @ `0xf1bace0` — `key = (EntryDataCase << 8) \| (id & 0xff)` |
| **Dispatcher** | `CoreDispatcher<jxc>::Dispatch` @ `0xf1dcee0` — `FlatHashMap<u16 packed-key, vector<subscriber>>`, SwissTable probe, fan-out `call *0x10(vtable)` |
| **Name lookup** | `JxcTracePointName(EntryDataCase, u16)` @ `0xf69d800` (per-band `NameOfDenseEnum`; default `"Unknown"` @ `0x85dd1fd`) |
| **Subscriber setup** | jxc `ConvertTpuTraceToXPlaneV2<jxc::PerformanceTraceEntry>` lambda @ `0xf1da7c0` — **10** `RegisterSubscriber` sites |
| **Unique subscribers** | `HbmMuxSubscriber<jxc>` @ `0xf1def00` (vtable `0x21643ce0`); `DmaSubscriber<jxc>` @ `0xf1dfee0` (vtable `0x21643dc0`) |

---

## Why jxc Diverges — Proto2 vs the 16-Byte Packet

The cross-gen split is fundamental and decided at codec-selection time. `xprof::tpu::DecodeTrace(DeviceType, JfTrace*)` @ `0xf59dba0` calls `GetTraceCodec` @ `0xf5a2900` (with the device generation read from `*(int*)(a2 + 260)`), which returns an `std::variant` whose alternatives are:

```text
variant<
  monostate,
  unique_ptr<TraceCodecInterface<gxc::glc::profiler::TraceEntry>>,   // glc
  unique_ptr<TraceCodecInterface<gxc::gfc::profiler::TraceEntry>>,   // gfc
  unique_ptr<TraceCodecInterface<vxc::vlc::profiler::TraceEntry>>,   // vlc
  unique_ptr<TraceCodecInterface<vxc::vfc::profiler::TraceEntry>>,   // vfc
  unique_ptr<TraceCodecInterface<pxc::profiler::TraceEntry>>,        // pxc
  unique_ptr<TraceCodecInterface<jxc::PerformanceTraceEntry>>        // jxc — DISTINCT TYPE
>
```

The decode then `__visit`s that variant. The five deepsea alternatives are all `TraceEntry` codecs — the [16-byte fixed-width packet](trace-entries-coder.md). The jxc alternative is a *different proto type entirely*, `PerformanceTraceEntry`, parsed by the standard proto2 runtime. This is byte-confirmed in the `DecodeTrace` decompile: the codec is dispatched through a `__variant_detail::__visitation` `__fmatrix` whose last alternative names `jxc::PerformanceTraceEntry`. There is no `GetBits64`/`SkipBits` call anywhere in the jxc decode path.

The consequence for a reimplementer is concrete:

> **QUIRK —** the jxc trace is **self-delimiting proto2**, so there are no fixed field widths and no per-event total-bit `CHECK`. A reimplementation that tries to drive jxc off the modern codec's framing (2-bit `valid`/`started`, 59-bit header, `GetBits64` width sequence) will misparse every record. jxc has no framing bits, no `TraceHeader` sub-record, and no `TraceIdHeader`; the GTC timestamp is proto field 1, `chip_id` is field 2, and the band is the active oneof member. The proto wire-tag layout *is* the format.

### The container and the band offsets

The decoded `PerformanceTraceEntry` object exposes two offsets the routing key reads. The oneof discriminator (`EntryDataCase`) is at **`entry + 0x30`**, and the active oneof member pointer is at **`entry + 0x28`** (proto2 stores the case as an int and the variant member as a pointer). Both are confirmed in `FromTraceEntry` (`*(int*)(a1 + 48)` reads the case; `*(_QWORD*)(a1 + 40)` dereferences the member). Fields 1/2 (`timestamp`, `chip_id`) are scalar and are *not* part of the oneof, so they never index a band.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `EntryDataCase` | `+0x30` | int (oneof case) | which of the 17 bands is active (3..19); 0 = `DATA_NOT_SET` |
| active oneof member | `+0x28` | ptr | the band sub-message; its `id` field is read at a per-band offset |
| `timestamp` (field 1) | proto-layout | u64 | the GTC cycle stamp (→ `GtcSpanConverter`, cycle→ps downstream) |
| `chip_id` (field 2) | proto-layout | u32 | chip identifier |

---

## The Packed Routing Key

jxc has no on-wire `trace_point_id` byte — the band's identity lives in the proto oneof case, and the event's identity lives in the band sub-message's own `id` enum. `TracePoint<jxc>::FromTraceEntry` @ `0xf1bace0` folds the two into one 16-bit dispatch key:

```c
// TracePoint<jxc>::FromTraceEntry @0xf1bace0 — packed-key composer
function FromTraceEntry(entry):
    case = *(int*)(entry + 0x30);            // EntryDataCase (oneof discriminator)
    switch (case):
        case 0:  id = case;                   // DATA_NOT_SET — degenerate
        case 1, 2: __builtin_trap();          // timestamp/chip_id are scalar, never keyed
        case 3, 8:                  id = member->[+0x18];   // nf_descriptor / ici_packet
        case 4, 6, 10, 12, 15, 16:  id = member->[+0x30];   // nf_control / nf / cs_internal / brn_sync_wait / bcs_internal / hib_request
        case 5:                     id = member->[+0x34];   // nf_ici
        case 7:                     id = member->[+0x20];   // hbm_mux_switch
        case 9, 18:                 id = member->[+0x40];   // cs_external_sync / hib_sync_update
        case 11:                    id = member->[+0x24];   // brn_fabric_sync
        case 13, 14, 19:            id = member->[+0x38];   // brn_perf1 / brn_perf2 / hib_hbm_write
        case 17:                    id = member->[+0x4c];   // hib_interrupt
    return ((u16)case << 8) | (u8)id;          // <- the packed key
```

The final composition is byte-exact: `((unsigned __int16)case << 8) | (unsigned __int8)id`. The per-case `id`-field offset is the proto2 layout offset of that band's first field inside its sub-message, read through the `+0x28` member pointer.

> **GOTCHA —** the `id`-field offset is **not** constant across bands — it ranges from `+0x18` to `+0x4c` depending on how many fixed-layout fields precede `id` in each band's proto2 message. A reimplementation that reads the band id at a single hardcoded offset will mis-key every band but the ones that happen to share that offset. Drive the offset off the oneof case, exactly as `FromTraceEntry` does. (Cases 1 and 2 trap — they are the scalar `timestamp`/`chip_id`, which are never the active oneof member.)

This packed key directly decodes the namespace the [tracepoints registry](tracepoints-master-registry.md) reports for jxc. Worked, byte-confirmed examples:

| Packed key | `= (case << 8) \| id` | band | event |
|---|---|---|---|
| `0x728` | `(7<<8)\|0x28` | case 7 `hbm_mux_switch` | id 40 `EVENT` |
| `0x603..0x617` | `(6<<8)\|low` | case 6 `nf` | the 17 DMA command/data-end ids (see [DMA band](#the-dma-subscriber-the-node-fabric-dma-band)) |
| `0x93c` | `(9<<8)\|0x3c` | case 9 `cs_external_sync_flag_update` | id 60 `DMA_DONE` |
| `0xa3d` | `(10<<8)\|0x3d` | case 10 `cs_internal` | id 61 `SET_SYNC_FLAG` |
| `0xa40` | `(10<<8)\|0x40` | case 10 `cs_internal` | id 64 `SET_TRACEMARK` |
| `0xa41` | `(10<<8)\|0x41` | case 10 `cs_internal` | id 65 `TRACE_INSTRUCTION` |
| `0xa45`/`0xa46` | `(10<<8)\|0x45/0x46` | case 10 `cs_internal` | ids 69/70 `SCALAR_FENCE_{START,END}` |

### Dispatch

`CoreDispatcher<jxc>::Dispatch` @ `0xf1dcee0` is the structural twin of the deepsea dispatcher. It calls `FromTraceEntry` to obtain the u16 key, then probes a `FlatHashMap<u16 packed-key, vector<shared_ptr<TraceEventSubscriber>>>` and fans the entry out to each registered subscriber:

```c
// CoreDispatcher<jxc>::Dispatch @0xf1dcee0
function Dispatch(self, entry_wrapper):
    lock(self);
    key = FromTraceEntry(entry_wrapper->[+0x10], entry_wrapper);   // u16 packed key
    // SwissTable probe of FlatHashMap<u16, vector<subscriber>>:
    h    = crc32(seed, key);                       // _mm_crc32_u64
    grp  = vpshufb(h7);                             // 16-lane control byte broadcast
    for (slot in groups):
        if (vpcmpeqb(grp, ctrl) match && *(u16*)slot == key)   // cmp word, (slot)
            break;
    for (sub in bucket.vector):                    // fan-out
        sub->vtable[0x10](sub, entry_wrapper);     // call *0x10(vtable) = ProcessTraceEntry
```

The hash is `crc32` over the 16-bit key with a `vpcmpeqb` group scan and a `cmp %ax,(slot)` final compare — the same absl SwissTable probe the modern `CoreDispatcher<TraceEntry>` uses. The only jxc-specific part is `FromTraceEntry`; everything above the key is gen-independent.

---

## The 17 Bands

The `oneof entry_data` holds 17 band sub-messages, oneof fields 3..19. For each band the packed routing key of an event is `(field << 8) | (band.id & 0xff)`. `JxcTracePointName(EntryDataCase, u16)` @ `0xf69d800` resolves a key to a name via the per-band `NameOfDenseEnum<descriptor, Lo, Hi>` table (`Lo`/`Hi` = the band's local id range; an out-of-range id resolves to `"Unknown"`, len 7, @ `0x85dd1fd`). Rather than dump every field, the table below gives each band's oneof field, its local id range and key range, and its role; the field lists that follow describe the bands a subscriber actually reads.

| Case | Band sub-message | Local ids | Key range | Role |
|---|---|---|---|---|
| 3 | `nf_descriptor` | 0..2 | `0x300..0x302` | Node-Fabric descriptor — the OCI-descriptor analog (full src/dst + 3 sync-flag-update channels) |
| 4 | `nf_control_message` | 28..29 | `0x31c..0x31d` | Node-Fabric control message |
| 5 | `nf_ici` | 24..26 | `0x518..0x51a` | Node-Fabric ICI receive/send framing |
| **6** | **`nf`** | **3..27** | **`0x603..0x61b`** | **Node-Fabric DMA band — the `DmaSubscriber` source** |
| **7** | **`hbm_mux_switch`** | **40** | **`0x728`** | **HBM read/write multiplexer switch — the `HbmMuxSubscriber` source** |
| 8 | `ici_packet` | 0..7 | `0x800..0x807` | Node-Fabric router flit band (jxc analog of deepsea ICI link) |
| 9 | `cs_external_sync_flag_update` | 60 | `0x93c` | TC cross-chip sync set-done (`DMA_DONE`) — `SyncSubscriber` |
| 10 | `cs_internal` | 61..70 | `0xa3d..0xa46` | TC sequencer internal sync/trace band — `Sync`/`Step`/`Hlo`/`Overlay`/`ScalarFence` |
| 11 | `brn_fabric_sync` | 112 | `0xb70` | BarnaCore fabric sync |
| 12 | `brn_sync_wait` | 113 | `0xc71` | BarnaCore sync wait |
| 13 | `brn_perf1` | 109..111 | `0xd6d..0xd6f` | BarnaCore FSM perf group 1 |
| 14 | `brn_perf2` | 100..108, 114..121 | `0xe64..0xe79` | BarnaCore FSM 16-channel controllers |
| 15 | `bcs_internal` | 122..127 | `0xf7a..0xf7f` | BarnaCore sequencer internal |
| 16 | `hib_request` | 80..83 | `0x1050..0x1053` | Host Interface Block DMA request — the UHI/HDE analog |
| 17 | `hib_interrupt` | 84..85 | `0x1154..0x1155` | HIB interrupt (queue-occupancy snapshot) |
| 18 | `hib_sync_update` | 86 | `0x1256` | HIB sync-flag update |
| 19 | `hib_hbm_write` | 87 | `0x1357` | HIB→HBM write |

> **NOTE —** the `brn_perf2` enum range `Lo..Hi = 100..121` *overlaps* the `brn_perf1`/`brn_fabric_sync`/`brn_sync_wait` id values, but the `EntryDataCase` disambiguates: case 14 is always the 16-channel-controller band regardless of the raw id. The id alone is ambiguous across BarnaCore bands; only the packed key (case + id) is unique. This is precisely why the routing key packs the case into the high byte.

### The bands the subscribers read

The `cs_internal` band (case 10) is the busiest — it fans to five different subscribers by id:

```text
case 10 cs_internal (ids 61..70)  fields: id, tensor_node, data_field,
                                          sync_flag_number, program_counter,
                                          sfence_end, sfence_start
  61 SET_SYNC_FLAG           ┐
  62 ADD_SYNC_FLAG           │
  66 UNSUCCESSFUL_SYNC_ATTEMPT├─ SyncSubscriber   (in-body mask 0xe3 over id-base 61)
  67 SUCCESSFUL_SYNC_ATTEMPT │
  68 READ_SYNC_FLAG          ┘
  64 SET_TRACEMARK           ── TensorCoreStep   (key 0xa40 → StepTracker)
  65 TRACE_INSTRUCTION       ── Hlo+Overlay+OnDevTraceMe+LloOp  (key 0xa41, 4-way fan-out)
  69 SCALAR_FENCE_START      ┐
  70 SCALAR_FENCE_END        ┴─ ScalarFence      (keys 0xa45/0xa46)
  63 HOST_INTERRUPT  · 65 (also) covered above
```

The `nf` band (case 6) is the DMA-started/completed band the `DmaSubscriber` consumes: each transfer through the Node Fabric emits a `*_COMMAND`/`RECEIVE` begin and a `*_DATA_END` completion per memory engine. Its fields are `id, tensor_node, trace_id, descriptor_source, node_id, chip_id, first, last`. The `hbm_mux_switch` band (case 7) carries only `id, tensor_node, fsm` — the `fsm` is the 2-state mux-switch direction the `HbmMuxSubscriber` tracks.

### Selector enum value tables

Several band fields are enums whose value tables are byte-exact from the descriptor pool:

```text
descriptor_source_value:  0=TENSOR_CORE 1=BARNA_CORE 2=HIB 3=HIB_HBM_QUEUE
nf_ici.vc_value:          0=CONTROL 1=DATA
ici_packet.router_port_id_value:  0..3=EXTERNAL_PORT_0..3  4=NODE_FABRIC_0  5=NODE_FABRIC_1
hib_sync_update.sf_rsrc_value:    0=TENSOR_CORE 1=BARNA_CORE
hib_hbm_write.queue_type_value:   0=TC_INFEED 1=BC_INFEED 2=HBM_WRITE
hib_request.requester_tag_value:  0=TC_INFQ 1=TC_OFQ 2=BC_INFQ 3=BC_OFQ 4=HBM_WRQ
                                  5=BC_FSMQ 6=NF_DESCRQ 7=CHIP_DEBUGQ 8=NF_OFQ
hib_request.requester_id_value:   0=TC_OF 1=BC_OF 2=NF_OF 3=CHIP_DEBUG 4=STATUS_BLOCK_WRITE
                                  5=TC_INF 6=BC_INF 7=HBM_WR 8=BC_FSM 9=QUEUE_FETCH 10=PAGE_TABLE_REQ
```

> **NOTE —** the `hib_request.virt_addr` (u64) and `hib_hbm_write.virt_addr` (u64) split, and the exact proto2 wire-tag offset of each sub-message field, are the standard proto2 message layout, not a hand bit-codec — so they are recovered from the descriptor, not a `GetBits` width sequence. (LOW confidence on the *exact in-RAM offset* of these two u64s; the field *presence and type* are CERTAIN from the descriptor pool.)

---

## The Ten jxc Subscribers

The jxc `ConvertTpuTraceToXPlaneV2<jxc::PerformanceTraceEntry>` setup lambda @ `0xf1da7c0` makes exactly **10** `RegisterSubscriber` calls (confirmed by an E8-rel32 caller scan). Eight reuse the deepsea begin/end-pairing trackers; two — `HbmMux` and `Dma` — are jxc-unique.

| # | Subscriber (vtable) | Packed key(s) | jellyfish event(s) | Notes | Confidence |
|---|---|---|---|---|---|
| 1 | `HbmMux` (`0x21643ce0`, threaded) | `{0x728}` | `hbm_mux_switch` `EVENT`(40) | XLine 56 "HBM Mux"; 2-state fsm tracker | CERTAIN |
| 2 | `Dma` (`0x21643dc0`, threaded) | `{0x603..0x617}` (17) | `nf` DMA cmd/data-end | `dma_id` pairing (FlatHashMap); XStat 0x38 | CERTAIN |
| 3 | `Sync` (`0x21643e88`, threaded) | `{0x93c,0xa3d,0xa3e,0xa42,0xa43,0xa44}` | `cs_external` `DMA_DONE` + `cs_internal` sync set | in-body mask `0xe3` over id-base 61 | CERTAIN |
| 4 | `ScalarFence` (`0x21643ed8`, threaded) | `{0xa45,0xa46}` | `cs_internal` `SCALAR_FENCE_{START,END}` | XLine 9 (Scalar Unit) | HIGH |
| 5 | `TensorCoreStep` (`0x21643f28`) | `{0xa40}` | `cs_internal` `SET_TRACEMARK`(64) | → StepTracker (TraceMark id) | HIGH |
| 6 | `TensorCoreHlo` (ctor `0xf1e27c0`) | `{0xa41}` | `cs_internal` `TRACE_INSTRUCTION`(65) | XLA Ops line | HIGH |
| 7 | `TensorCoreOverlay` (`0x21644178`) | `{0xa41}` | `cs_internal` `TRACE_INSTRUCTION`(65) | → OverlayTracker | HIGH |
| 8 | `TensorCoreOnDeviceTraceMe` (`0x216441c8`) | `{0xa41}` | `cs_internal` `TRACE_INSTRUCTION`(65) | XLA TraceMe | HIGH |
| 9 | `LloOpEvent` (`0x21644218`, threaded) | `{0xa41}` | `cs_internal` `TRACE_INSTRUCTION`(65) | Tensor Core line | HIGH |
| 10 | `ScalarFence` (`0x21643ed8`, threaded) | `{0xa45,0xa46}` | `cs_internal` `SCALAR_FENCE_{START,END}` | XLine 62 (Barna Core Fence) | HIGH |

> **QUIRK —** key `0xa41` (`TRACE_INSTRUCTION`) is a **4-way fan-out** point: `Hlo`, `Overlay`, `OnDeviceTraceMe`, and `LloOp` all register on the *same* packed key and share one `TracePoints` buffer in the setup lambda. This mirrors the deepsea id-85 4-way fan-out exactly — the dispatcher delivers one entry to all four subscribers, each of which projects it onto a different XPlane line. A reimplementation that assumes one subscriber per key will drop three of the four TRACE_INSTRUCTION consumers.

The Step/Overlay/Sync trackers reuse the deepsea begin/end-pairing model: `StepTracker` keys on the TraceMark id, `OverlayTracker` on the overlay operand, `SyncTracker` on `sync_flag_number`. The two jxc-unique trackers — `DmaSubscriber` on `dma_id` and `HbmMux` on the fsm direction — are detailed below.

---

## The DMA Subscriber — the Node-Fabric DMA Band

The `DmaSubscriber<jxc>` (vtable `0x21643dc0`) is the second subscriber registered. It is wrapped in a `0x240`-byte `ThreadedSubscriber` (`ThreadLoop` @ `0xf1ddd60`); its inner object holds an XStat `StatMetadata` at `+0x18` (StatType `0x38`). It consumes the `nf` band (case 6) — the jxc DMA band that the newer gens fold into the [ICI/intra-DMA payloads](payload-uhi-oci-ici-dma.md).

> **CORRECTION (P-3-421) —** an earlier reading listed the `Dma` subscriber as non-threaded. The byte-level read of the setup lambda shows **both** `HbmMux` and `Dma` are `ThreadedSubscriber`-wrapped (the `0x240`-byte wrapper with the `ClosureThread` worker at `+0xa0`). Treat both as threaded.

### Registered keys — the 17 nf DMA bands

The lambda builds the `Dma` `TracePoints` buffer by looping over 17 dword entries in a `.rodata` table @ `0xab53940`: for each it reads the low byte (an `nf` `TracePoint` id) and OR-s `0x600` (case 6 in the high byte), producing 17 packed keys:

```text
 key  nf id  event                       key  nf id  event
0x603   3   HBM_READ_COMMAND           0x60a  10  VMEM_ICI_WRITE_COMMAND
0x604   4   HBM_WRITE_COMMAND          0x60b  11  VMEM_ICI_WRITE_DATA_END
0x605   5   HBM_WRITE_DATA_END         0x60c  12  SMEM_READ_COMMAND
0x606   6   VMEM_HBM_READ_COMMAND      0x60d  13  SMEM_WRITE_COMMAND
0x607   7   VMEM_HBM_WRITE_COMMAND     0x60e  14  SMEM_WRITE_DATA_END
0x608   8   VMEM_HBM_WRITE_DATA_END    0x60f  15  IMEM_WRITE_COMMAND
0x609   9   VMEM_ICI_READ_COMMAND      0x610  16  IMEM_WRITE_DATA_END
0x614  20   HIB_WRITE_RECEIVE          0x616  22  HIB_WRITE_COMMAND
                                       0x617  23  HIB_WRITE_DATA_END
```

> **NOTE —** the `nf` ids 17/18/19 (BMEM) and 27 (`ICI_SEND_END`) are present in the `nf` band but are **not** registered by the `Dma` subscriber — only the six memory-engine command/data-end families (HBM, VMEM-HBM, VMEM-ICI, SMEM, IMEM, HIB) are paired. A reimplementation that registers all of band 6 will pick up BMEM and ICI-send events the subscriber never pairs.

### The match key — `dma_id` pairing

`DmaSubscriber<jxc>::ProcessTraceEntry` @ `0xf1dfee0` is a begin/end pairer keyed on a synthetic `dma_id`:

```c
// DmaSubscriber<jxc>::ProcessTraceEntry @0xf1dfee0
function ProcessTraceEntry(self, entry):
    if (!CoreId_matches(self, entry)) return;          // chip/core filter (+0x08/+0x0c)
    if (!(MemoryCommand(entry) || MemoryDataEnd(entry)))  return;  // gate: begin or end only
    if (entry.EntryDataCase != 6) return;              // confirm nf band (member+0x30 == 6)
    switch (nf.id - 3):                                // 0..0x14 → per-engine XStat selector
        // picks a TypeInfo label (typeinfo @0x21643e20/e30),
        // a kind (4=command, 5=data-end), a StatType (0x12/0x13/0x14/0x34/0x39)
    dma_id = GetDmaId(entry);                          // @0xf698180 — composite pairing key
    bucket = pending.find_or_prepare_insert_large(dma_id);   // FlatHashMap<dma_id, vector<begin>>
    if (MemoryCommand(entry) && First(entry)):         // @0xf698620
        bucket.vector = { entry };                     // open a pending begin
    else if (MemoryDataEnd(entry)):
        emit_duration_span(bucket.vector[0], entry);   // pair, emit, clear the slot
```

The gate and the `EntryDataCase == 6` confirm are byte-exact (`MemoryCommand() || MemoryDataEnd()`, then `*(int*)(member + 0x30) == 6`). The pairing store is a `FlatHashMap<unsigned long, vector<shared_ptr<TraceEntryWrapper>>>` reached via `find_or_prepare_insert_large<unsigned long>` @ `0xf1e05e0`. A `*_COMMAND`/`RECEIVE` with `First()` opens a begin under the `dma_id`; the matching `*_DATA_END` emits the duration span and clears the slot. The read/write display name is selected by a `"Writ"` magic compare at `0xf1e0152`.

### `GetDmaId` — the composite key

`GetDmaId` @ `0xf698180` is the synthetic-key composer. It switches on the `nf` oneof case and folds per-direction fields of the `nf` sub-message into one `unsigned long` so that a command and its data-end produce the **same** key (and distinct concurrent DMAs differ). The HBM/VMEM-HBM/ICI-packet arms are byte-confirmed:

```c
// GetDmaId @0xf698180 — composite-key composition (HBM/VMEM-HBM arm)
key = (trace_id & 0x1f00)                  // bits 8..12 of trace_id
    | ((resource & 3)   << 13)             // 2-bit src/dst resource
    | ((node_id  & 1)   << 15)             // node bit
    | ((chip_id  << 16) & 0x7ff0000)       // 11-bit chip
    | (id & 0xff);                          // low byte = the nf id
// simple arms (VMEM-ICI / SMEM / IMEM / BMEM / HIB): key = 0 (degenerate — no composite, low byte never set)
```

> **GOTCHA —** the simple-engine arms (`case 7,9,0xA..0x11` in the switch) fall to `LABEL_12` and return **`0`** — those cases never assign the composite fields (`v3` stays `0` and the low byte stays `0`), so the engine families without a rich command/data-end identity collapse to a single degenerate key. Only the composite arms (`case 3` `nf_descriptor`, `case 4`/`6` `nf_control`/`nf`, `case 5` `nf_ici`, `case 8` `ici_packet`, and the two HIB arms `case 0x12`/`0x13`) build the full key. The *value* is deterministic and pairs correctly per engine, but the **exact LSB bit layout per arm is CONFIRMED-PARTIAL** — the field selection per direction is byte-read from the arm table @ `0xab88674`, the OR/shift composition for the composite family is read, but a fully tabulated per-arm bit map was not enumerated. A reimplementer must reproduce the arm-by-arm field selection, not a single global formula.

The per-engine XStat assignment (which engine/direction lands on StatType `0x12`/`0x13`/`0x14`/`0x34`/`0x39`) and the read-vs-write XEvent display-name table (the `"Writ"` branch) were **not fully tabulated** (LOW confidence on the integer→XStat-name mapping; the *mechanism* is CERTAIN).

---

## The HBM-Mux Subscriber — the HBM Multiplexer Band

The `HbmMuxSubscriber<jxc>` (vtable `0x21643ce0`) is the first subscriber registered; it is `ThreadedSubscriber`-wrapped (`0x240` bytes). It is jxc-unique: the HBM read/write multiplexer occupancy band has **no analog on the deepsea gens** (they fold it into the intra-DMA/HBM bands). At construction it pre-creates two `XEventMetadata` for the two switch directions:

```text
+0x18 = "BFIFO to Node Fabric"   (rodata @0x8732bba, len 20)
+0x20 = "Node Fabric to BFIFO"   (rodata @0x929bc3b, len 20)
```

### Registered key and the 2-state tracker

It registers a single packed key `0x728` (built by `movw $0x728,(buf)` @ `0xf1db361`): `0x728 = (7<<8)|0x28 = case 7 (hbm_mux_switch) id 40 = EVENT`. `HbmMuxSubscriber<jxc>::ProcessTraceEntry` @ `0xf1def00` is a 2-state switch tracker:

```c
// HbmMuxSubscriber<jxc>::ProcessTraceEntry @0xf1def00
function ProcessTraceEntry(self, entry):
    if (!CoreId_matches(self, entry)) return;          // +0x08/+0x0c
    state = HbmMuxSwitchState(entry);                  // @0xf6986e0
    // HbmMuxSwitchState: if EntryDataCase==7 -> return (fsm | 0x100000000); else 0
    if ((state >> 32) == 0) return;                    // not an hbm_mux_switch entry
    fsm = state & 0xffffffff;                          // 0=drop, 1/2=directions, 3=other
    switch (fsm):
        case 1, 2:                                     // a switch: close the open direction's span
            dur   = GtcSpan(entry) - (DurationCycles(prev) << 4);
            line  = GetOrCreateLine(56);               // "HBM Mux" XLine, @0xf1df120
            meta  = (open_dir == BFIFO) ? self[+0x18] : self[+0x20];
            AddEvent(line, meta, self[+0x28] /*start*/, dur);   // @0xf1df1e0
            self[+0x28] = GtcSpan(entry);              // open the next direction
            self[+0x38] = fsm;                          // current direction
            self[+0x30] = entry;                        // prev-entry shared_ptr
        case 3:  ... // third path — see gap note
        case 0:  return;                                // dropped
```

`HbmMuxSwitchState` @ `0xf6986e0` is byte-confirmed: it checks `EntryDataCase == 7`, reads the `fsm` field at `member + 0x1c`, and returns `fsm | 0x100000000` (bit-32 = present marker, low 32 bits = state); otherwise 0.

The HBM-mux event payload is therefore `{fsm switch-direction (2-state), tensor_node}`. The begin/end pairing key is the **fsm direction** itself: each `EVENT` closes the previously open direction's span and opens the next, so the duration is the time the mux spent in one direction. Spans land on XLine `TpuComponent 56 = "HBM Mux"` (name @ `0x84c06ed`).

> **NOTE —** the `fsm == 3` arm (@ `0xf1df044`) takes a slightly different branch (it uses the `+0x18` and `+0x20` metadata differently). States 1/2 are the two named switch directions and state 0 is dropped; the precise semantic of state 3 — a third mux mode versus an init/flush event — was **not isolated** (LOW confidence on the state-3 path; the 2-state switch behaviour is CERTAIN).

---

## Relevant Struct and Table Offsets

| Symbol | Address / offset | Role | Confidence |
|---|---|---|---|
| `PerformanceTraceEntry` (proto2) | `+0x30` `EntryDataCase`; `+0x28` active oneof member ptr; field 1 `timestamp` (GTC); field 2 `chip_id` | the decoded jxc message | CERTAIN |
| `TracePoint<jxc>::FromTraceEntry` | `0xf1bace0` | `key = (case<<8)\|(member->[per-case off] & 0xff)` | CERTAIN |
| `CoreDispatcher<jxc>::Dispatch` | `0xf1dcee0` | `FlatHashMap<u16, vector<subscriber>>`, crc32/vpcmpeqb probe, `call *0x10` | CERTAIN |
| `JxcTracePointName` | `0xf69d800` | key → name (per-band `NameOfDenseEnum`; default `"Unknown"` @ `0x85dd1fd`) | CERTAIN |
| `DecodeTrace(DeviceType, JfTrace*)` | `0xf59dba0` | picks codec via `GetTraceCodec` @ `0xf5a2900`; proto path, no `GetBits` | CERTAIN |
| setup lambda | `0xf1da7c0` | 10 `RegisterSubscriber` sites; `RegisterSubscriber` @ `0xf1dca40` | CERTAIN |
| `HbmMuxSubscriber<jxc>` | vtable `0x21643ce0`; `ProcessTraceEntry` `0xf1def00` | `+0x18`/`+0x20` direction XEvents; `+0x28` start gtc; `+0x38` current dir | CERTAIN |
| `HbmMuxSwitchState` | `0xf6986e0` | `fsm` @ member+0x1c; returns `fsm \| 0x100000000` | CERTAIN |
| `DmaSubscriber<jxc>` | vtable `0x21643dc0`; `ProcessTraceEntry` `0xf1dfee0` | `+0x18` XStat (StatType 0x38); `+0x20` `FlatHashMap<dma_id, vector<begin>>` | CERTAIN |
| `GetDmaId` | `0xf698180` (arm jt `0xab88674`) | composite pairing key; HBM/VMEM-HBM arms full, simple arms `id & 0xff` | HIGH |
| `nf` key table | `0xab53940` | 17 dwords (low byte = nf id, OR `0x600`) | CERTAIN |
| `ThreadedSubscriber<jxc>` | `0x240` B; `+0x20` inner vtable; `+0x38` inner ptr; `+0xa0` worker | wraps both HbmMux and Dma | CERTAIN |

---

## Related Components

| Component | Relationship |
|---|---|
| [TraceEntriesCoder](trace-entries-coder.md) | the modern fixed-16-byte codec jxc does **not** use; jxc is a distinct `PerformanceTraceEntry` proto2 variant in the same `GetTraceCodec` selector |
| [TracePoints Master Registry](tracepoints-master-registry.md) | records the jxc `0x9xx`/`0xaxx` packed keys this page decodes as `(case<<8)\|id` |
| [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | the modern bands; the jxc `nf` DMA band and `hib_request` band are the legacy analogs the newer gens fold into ICI/intra-DMA |
| [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | the newer-family payload deltas; none carry the jxc-unique HBM-mux band |
| [Profiling and Telemetry Overview](overview.md) | the capture→decode→xplane pipeline this legacy decode is the jxc-specific stage of |

## Cross-References

- [TraceEntriesCoder](trace-entries-coder.md) — the modern fixed-width codec; jxc uses the legacy proto2 `PerformanceTraceEntry` path instead, selected as a separate `std::variant` alternative in `GetTraceCodec`
- [TracePoints Master Registry](tracepoints-master-registry.md) — the wire-id namespace; the jxc ids are the packed `(EntryDataCase<<8)|id` keys decoded on this page
- [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) — the modern interconnect/DMA bands; contrast with the jxc-unique `nf` DMA band and HBM-mux band
- [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) — the newer-family payload maps; the jxc HBM-mux band has no successor there
- [Profiling and Telemetry Overview](overview.md) — the device-trace pipeline this legacy-gen decode sits inside
