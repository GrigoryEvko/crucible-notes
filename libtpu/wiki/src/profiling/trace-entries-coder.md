# TraceEntriesCoder

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TraceEntriesCoder` is the on-device profiler trace-entry codec: the fixed-width, LSB-first bit format that every TPU hardware trace event packs into, and the per-chip-family decoder/encoder that translates between that wire packet and a proto2 `TraceEntry` message. It is the bottom-most layer of the [xprof device-trace pipeline](overview.md) — the stage between the compressed [riegeli container](riegeli-trace-container.md) and the [`TraceEntry → XEvent/XStat`](trace-entry-to-xevent.md) shaping. Where a route-cache record is a self-delimiting varint stream, a profiler trace event is the opposite: a **constant 16-byte (128-bit) packet** with a 2-bit framing prefix, a fixed 59-bit header, an optional 36-bit transaction-identity sub-record, and a per-event fixed-width payload, all read with the shared `GetBits64`/`SkipBits` bit-codec primitives.

The codec has a deliberately asymmetric two-id-space dispatch, and getting it right is the whole reimplementation. **Decode** peeks the 2 framing bits and the 8-bit `trace_point_id` — the *banded hardware enum value*, gappy, max `0x6e` for pxc — and indexes a 111-entry `rel32` jump table to reach an anonymous-namespace `Decode<EventName>()`. **Encode** dispatches on the *dense proto oneof field number* stored at `TraceEntry+0x28` through a parallel jump table. The two id spaces are not interchangeable; the registry that pairs them is owned by [TracePoints Master Registry](tracepoints-master-registry.md), and a reimplementation that drives encode off the wire id (or decode off the oneof field) mis-keys every event.

This page owns the **codec format and dispatch**: the 16-byte packet, the framing/header/TraceIdHeader bit layout, the per-event total-bit `CHECK` validation, the per-family `CreateTraceCodec`/`GetTraceCodec` factory wiring, and how the header split shifts across silicon generations. It does **not** own the per-band payload field maps ([UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md), [SparseCore band](payload-sc-band.md), [vfc/vlc/gfc](payload-vfc-vlc-gfc.md), [jxc legacy](payload-jxc-legacy.md)), the trace-point id registry ([master registry](tracepoints-master-registry.md)), the compressed container ([riegeli](riegeli-trace-container.md)), or the XEvent translation ([trace-entry-to-xevent](trace-entry-to-xevent.md)).

For reimplementation, the contract is:

- **The fixed 16-byte packet and the bit-stream reader** — LSB-first, `GetBits64NoInline(n)` for an n-bit field, `SkipBitsNoInline(n)` to advance, masked by `mask_[k] = (1<<k)-1`.
- **The 2-bit framing prefix + 59-bit `TraceHeader` + optional 36-bit `TraceIdHeader`** — the universal envelope, with the per-gen `block_id`/`timestamp` split.
- **The dual dispatch** — decode by 8-bit wire `trace_point_id` (`DecodeEntry` jump table), encode by dense oneof field number (`EncodeEntry` jump table); the `valid`/`started` framing semantics; the per-event total-bit `CHECK`.
- **The per-family factory** — `GetTraceCodec(DeviceIdentifiers, int)` selecting one of pxc/vfc/vlc/glc/gfc fixed-width codecs (or the jxc legacy `PerformanceTraceEntry` path) from a static type-factory keyed by chip codename.

| | |
|---|---|
| **Codec interface** | `asic_sw::driver::deepsea::profiler::TraceCodecInterface<TraceEntry>` (abstract; vtable: `DecodeEntry`/`EncodeEntry`/`GetMaxEntrySize`/`GetEntryPacketSize`) |
| **Packet size** | fixed **16 bytes (128 bits)** — `GetEntryPacketSize()==0x10`, `GetMaxEntrySize()==0x20` (decoded-proto upper bound), all 5 families |
| **Bit order** | LSB-first; read via `BitDecoder::GetBits64NoInline` @ `0x21073760`, `SkipBitsNoInline` @ `0x21073580`, mask table `mask_` @ `0xbe79440` |
| **Header layout** | `valid:1 · started:1 · trace_point_id:8 · block_id:3│6 · timestamp:48│45` — framing+header = **61 bits**; payload begins at **bit 61** |
| **Decode entry** | `pxc TraceEntriesCoder::DecodeEntry` @ `0xf5af3a0` → 111-entry `rel32` jump table @ `0xab85bc0` |
| **Encode entry** | `pxc TraceEntriesCoder::EncodeEntry` @ `0xf5c5e60` → parallel jump table @ `0xab85fc0` |
| **Decode driver** | `xprof::tpu::DecodeTraceBuffers<TraceEntry>` @ `0xf59ffa0` (pxc) — inflates + walks 16-byte packets |
| **Codec selector** | `xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` @ `0xf5a2900` |
| **Factory** | `pxc::driver::profiler::CreateTraceCodec` (`plc` symbol @ `0xf5af2c0`); `vfc` @ `0xf5f5da0`, `vlc` @ `0xf5d5180`, `glc` @ `0xf6282e0`, `gfc` @ `0xf65ed00` |
| **Source paths (rodata)** | `platforms/asic_sw/driver/deepsea/<fam>/profiler/trace_entries.proto`, `…/trace_codec_factory.cc`; `third_party/gloop/util/coding/bitcoding.cc` |

---

## The Fixed 16-Byte Packet

Every on-device profiler trace event is a constant-size **16-byte (128-bit) packet** — not a varint, gamma, or any self-delimiting record. This is the single most important divergence from the rest of the [gloop bit-codec](riegeli-trace-container.md) toolkit, which elsewhere uses varint-framed records: the profiler path is pure fixed-width.

The constant size is proven three ways in the binary:

- `GetEntryPacketSize()` returns `0x10` (16) in **all five families** — each is a single `mov $0x10,%eax; ret` at pxc `0xf5d4ec0`, vfc `0xf628020`, vlc `0xf5f5ae0`, glc `0xf65ea40`, gfc `0xf697aa0`.
- `GetMaxEntrySize()` returns `0x20` (32) at the matching `-0x20` addresses — this is the decoded *proto's* in-RAM upper bound, **not** the wire size. A reimplementer must not confuse the two: 16 bytes on the wire, ≤32 bytes as a deserialized `TraceEntry`.
- Each per-event decoder gates on input length before reading: `cmp $0xf,%rdx; ja …` requires the `string_view` to be longer than 15 bytes (i.e. at least one whole 16-byte packet), and on success records bytes-consumed = `0x10` (`movq $0x10,0x8(%rbx)`).

The packet is an LSB-first bit-stream read over the shared `BitDecoder` window (`{cursor@+0x8, end@+0x10, buffer@+0x18, bits_avail@+0x20}`, a 0x28-byte object). In the decompiled `DecodeEntry`, the window is stack-constructed inline from the input `string_view`:

```c
// DecodeEntry @0xf5af3a0 — window init (decompiled v14[] = the BitDecoder)
v14[1] = data_ptr;          // +0x8  cursor   = start of the 16-byte packet
v14[0] = data_ptr;          // +0x0/+0x18 buffer base (LSB-first source)
v14[2] = data_ptr + length; // +0x10 end
v15    = 0;                  // +0x20 bits_avail
```

Fields are extracted with two primitives from `bitcoding.cc`:

| Primitive | Address | Effect |
|---|---|---|
| `BitDecoder::GetBits64NoInline(dec, n, out)` | `0x21073760` | read the next `n` bits (LSB-first) into `*out`, masked by `mask_[n]`; advance the cursor `n` bits |
| `BitDecoder::SkipBitsNoInline(dec, n)` | `0x21073580` | advance the cursor `n` bits without materializing them |
| `mask_[k] = (1<<k)-1` | `0xbe79440` | 65-qword `.rodata` mask table; `+0x8`→`mask_[1]`, `+0x18`→`mask_[3]`, `+0x40`→`mask_[8]`, `+0xa8`→`mask_[21]`, `+0x100`→`mask_[32]`, `+0x180`→`mask_[48]` (offsets verified against the field widths) |

> **NOTE —** `GetBits64NoInline` faults (`ud1`) on `n > 64` and handles the within-buffer straddle internally; the codec never asks for more than 48 bits in a single call. The `NoInline` variants are the out-of-line copies; the same logic is inlined at thousands of call sites across the per-event decoders.

---

## The Framing Prefix and the 59-Bit TraceHeader

Every packet opens with a 2-bit framing prefix and a 59-bit `TraceHeader`, together a fixed **61-bit envelope**, after which the typed payload always begins at **bit 61**.

```text
 bit  field                 width                    proto
 ───  ────────────────────  ───────────────────────  ──────────────────────────────
  0   valid                 1                        (framing — 0 ⇒ end of buffer)
  1   started               1                        (framing — valid&&!started ⇒ FATAL)
 2-9  trace_point_id        8                        TraceHeader.trace_point_id  (f1)
10-K  trace_point_block_id  3 (pxc/vlc) │ 6 (vfc/glc/gfc)   TraceHeader.f2
K+1   timestamp             48 (pxc/vlc) │ 45 (vfc/glc/gfc)  TraceHeader.f3
 …60  (header ends at bit 60; total framing+header = 61)
 61-  per-event payload     variable (fixed per id)
```

The **header sub-budget (id + block_id + timestamp) is invariably 59 bits**. Newer silicon widens `block_id` from 3 to 6 bits and narrows the `timestamp` from 48 to 45 bits to keep that 59-bit envelope constant — so the payload origin never moves off bit 61, regardless of generation. This is the key invariant a cross-gen reimplementation must encode: do not hardcode the timestamp width; derive it from `59 - 8 - block_w`.

### Framing semantics — valid and started

The two framing bits are not part of the proto; they are wire-level flow control:

- **`valid`** (bit 0) is the empty-slot sentinel. A `valid == 0` packet means *graceful end of stream* — the buffer can be drained to its capacity without an explicit entry count; the decoder stops and reports success with `*started_out = 0`. This is why a ring buffer can be over-allocated and read until the first cleared slot.
- **`started`** (bit 1) catches torn/partial hardware writes. `valid && !started` is a fatal error: the decoder builds `MakeErrorImpl<3>("Found a valid but not started packet.")` (string @ `0x9ff8a9c`, status code `0x2e02`) and aborts.

The decompiled head of `DecodeEntry` shows the exact read order and branch logic:

```c
// pxc TraceEntriesCoder::DecodeEntry @0xf5af3a0
GetBits64NoInline(dec, 1, &valid);     // slot -0x58 (bit 0)
GetBits64NoInline(dec, 1, &started);   // slot -0x50 (bit 1)
GetBits64NoInline(dec, 8, &id);        // slot -0x48 (bits 2..9) — peek, then re-decode in handler

if (valid) {
    if (started) {
        *valid_out   = 1;
        *started_out = 0;              // (sic — set then overwritten by handler success path)
        switch (id) {                  // 8-bit banded wire id → 111-entry jump table @0xab85bc0
            case 0:  DecodeUhiHostDmaTransactionStartedAddressTranslation(...); break;
            case 1:  DecodeUhiHostPhysicalRequestRead(...);                     break;
            // … ids 0-10 UHI, 20-27 OCI, 40-55 ICI, 80-97 TCS, 100-110 CMQ …
            default: /* common error label @0xf5b032f */                        break;
        }
    } else {
        return MakeErrorImpl<3>("Found a valid but not started packet.");  // @0x9ff8a9c
    }
} else {
    *valid_out = 0;                    // EOS — graceful, status OK
    return OK;
}
```

> **GOTCHA —** `DecodeEntry` reads the 8-bit id only to *index the jump table*. Each `Decode<Name>()` handler then re-decodes the packet from bit 0 — `SkipBits(2)` past the framing, a full `DecodeTraceHeader`, then the typed payload — because the handler needs the header fields materialized into the proto, not just the id peeked. A reimplementation that tries to share one header decode between the dispatcher and the handler will double-consume or mis-position the cursor.

### Per-gen header widths

`DecodeTraceHeader` is an anonymous-namespace helper, one per family, that reads exactly three `GetBits64` calls in order and stamps the `TraceHeader` proto. The pxc version is byte-confirmed in the decompile:

```c
// pxc DecodeTraceHeader @0xf5d4f20
GetBits64NoInline(dec, 8,  &id);    th = Arena::DefaultConstruct<TraceHeader>();  th[+0x18]=id;    th[+0x10] |= 1;
GetBits64NoInline(dec, 3,  &block); th[+0x1c]=block;                              th[+0x10] |= 2;
GetBits64NoInline(dec, 48, &ts);    th[+0x20]=ts;                                 th[+0x10] |= 4;
```

The `|= 1 / 2 / 4` are the proto2 *has-bits* (presence byte at `TraceHeader+0x10`, bit0=id, bit1=block_id, bit2=timestamp). The widths per family:

| Family | `DecodeTraceHeader` | `id` | `block_id` | `timestamp` | header bits | payload start | Confidence |
|---|---|---|---|---|---|---|---|
| pxc | `0xf5d4f20` | 8 | 3 | 48 | 59 | 61 | CERTAIN |
| vfc | `0xf628080` | 8 | 6 | 45 | 59 | 61 | CERTAIN |
| vlc | `0xf5f5b40` | 8 | 3 | 48 | 59 | 61 | CERTAIN |
| glc | `0xf65eaa0` | 8 | 6 | 45 | 59 | 61 | CERTAIN |
| gfc | `0xf697b00` | 8 | 6 | 45 | 59 | 61 | CERTAIN |

> **QUIRK —** the `timestamp` is the **raw device cycle counter** (48 or 45 bits), not picoseconds. The cycle→ps conversion (per-gen device clock rate) and the per-line `timestamp_ns` origin are applied *downstream* in `TpuXLineBuilder`, never in the codec. A reimplementation that treats the on-wire timestamp as picoseconds is off by the clock period and will mis-compute the counter wrap interval (≈`2^48` cycles at 48 bits). See [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md).

---

## The TraceIdHeader Sub-Record

Most variant messages carry a 36-bit `TraceIdHeader` immediately after the 61-bit header — the per-transaction identity that lets a multi-packet DMA be stitched back together. It is decoded *inline* (there is no standalone `DecodeTraceIdHeader` symbol) into a nested proto2 `TraceIdHeader` submessage.

```text
 rel bit  field            width   proto
 ───────  ───────────────  ──────  ─────────────────────────────────────────────
  0-20    transaction_id   21      TraceIdHeader.transaction_id (f1)
 21-23    core_id          3       f2 — enum {TC0,TC1,BC0..BC3,NONCORE,…} (8 values ⇒ 3 bits)
 24-35    chip_id          12      f3
 = 36 bits, immediately after the 61-bit header when present
```

Byte-confirmed in `DecodeUhiHostPhysicalRequestRead` @ `0xf5b0f20`, where the three opening `GetBits64` calls are `21 / 3 / 12`:

```c
// DecodeUhiHostPhysicalRequestRead @0xf5b0f20 — TraceIdHeader, then payload
GetBits64NoInline(dec, 21, &transaction_id);   // TraceIdHeader f1
GetBits64NoInline(dec,  3, &core_id);          // f2 (3-bit enum)
GetBits64NoInline(dec, 12, &chip_id);          // f3
// then the typed payload: 30, 1, 1, 29, 26, 8, 20, 20 …
```

The 3-bit `core_id` exactly fits the 8-value `CORE_ID` enum (`RESERVED/NONCORE/TC0/TC1/BC0..BC3`). Some events carry **multiple** `TraceIdHeader`s — OCI read/write commands embed three (`cmd0/cmd1/cmd2`), i.e. `3 × 36 = 108` bits of identity before any other payload. The per-band detail of which events carry one, three, or zero is owned by the [payload pages](payload-uhi-oci-ici-dma.md).

---

## The Dual Dispatch

The codec is deliberately asymmetric: decode and encode index *different* jump tables keyed by *different* id spaces.

### Decode — by 8-bit wire trace_point_id

`DecodeEntry` dispatches on the 8-bit on-wire `trace_point_id` — the **banded hardware enum**, gappy, max `0x6e = 110` for pxc — through a 111-entry `rel32` jump table at `0xab85bc0`. The table is read byte-exact; its band structure mirrors the [trace-point registry](tracepoints-master-registry.md):

| Band | `trace_point_id` (pxc) | Subsystem |
|---|---|---|
| UHI | 0–10 | host-DMA / address translation |
| OCI | 20–27 | on-chip interconnect engine |
| ICI | 40–55 | inter-chip interconnect / collective fabric |
| TCS | 80–97 | TensorCore sequencer sync/control + throttle |
| CMQ | 100–110 | command queue |
| (reserved) | 11–19, 28–39, 56–79, 98–99 | all → common error label `0xf5b032f` |

> **GOTCHA —** the reserved id ranges do **not** fall through to a neighbour handler — every reserved `rel32` slot points at the *same* common error label (`0xf5b032f`). This directly confirms the band gaps are deliberate reserved space, not a decode bug. Drive band detection off the per-family jump table contents, never off a hardcoded pxc range; the band boundaries shift per generation as trace-point cardinality grows (≈99 → ≈144 events).

### Encode — by dense oneof field number

`EncodeEntry` @ `0xf5c5e60` dispatches on the **dense proto oneof field number** held at `TraceEntry+0x28` (the proto2 oneof *case*), through a parallel `rel32` table at `0xab85fc0`:

```c
// EncodeEntry @0xf5c5e60 — dispatch + inlined header pack
field = *(int*)(entry + 0x28);          // proto oneof case (dense)
idx   = field - 2;                       // table indexed by field-2
if (idx > 0x62 /*pxc bound*/) default;
goto *jumptable_0xab85fc0[idx];          // → Encode<Name>()

// inlined header pack (Encode<Name>, word0):
word0  =  (mask_[1] & flag) * 3;         // flag*3 ⇒ bits 0 and 1 both set (valid=1, started=1)
word0 |=  id    << 2;                     // trace_point_id
word0 |=  block << 10;                    // block_id
word0 |=  ts    << 13;                    // timestamp (pxc) — vfc shifts <<16 (6-bit block)
word0 |=  payload_lo << 61;               // payload begins at bit 61
// word1 = remaining payload; two qwords (16 B) written to the encoder SSO buffer
```

The encode shifts are the byte-exact inverse of the decode `GetBits` widths: `<<2` (after the 2 framing bits), `<<10` (after id), `<<13` for pxc (after 3-bit block) or `<<16` for vfc (after 6-bit block), and `<<61` for the payload origin. The framing is written as `flag*3` — `mask_[1] & 1` then `lea(r8,r8,2)` — which sets **both** bit 0 and bit 1, i.e. `valid=1, started=1` for every real entry.

### The two id spaces, paired

The wire id and the oneof field are distinct namespaces; the handler stamps the oneof field via `movl $field,0x28(%entry)`. Worked, byte-confirmed pairs:

| `trace_point_id` (wire) | event | oneof field (proto) | encode bound `cmp` |
|---|---|---|---|
| 0 | `UhiHostDmaTransactionStartedAddressTranslation` | 2 | — |
| 1 | `UhiHostPhysicalRequestRead` | 3 | — |
| 40 | `IciPacketPacketReceivedOnLinkInput` | 21 (`0x15`) | — |
| 81 | `TcsInternalSetSyncFlag` | 38 (`0x26`) | — |
| 97 | `ThrottleStateThermalAndElectrical` | 54 (`0x36`) | — |

Per-family oneof-field encode bounds (the `cmp $imm` before the jump): pxc `0x62`, vlc `0x4d`, vfc `0x79`, glc `0x7f`, gfc `0x7f`. The full id↔field registry is owned by [TracePoints Master Registry](tracepoints-master-registry.md).

> **QUIRK —** decode max wire id (`0x6e` pxc) and encode max oneof field (`0x62` pxc) differ because the wire id is *banded with gaps* while the oneof field is *dense*. Both count the same set of events; only the indexing differs. A reimplementation that sizes one table to the other's bound will overrun or truncate.

---

## The Per-Event Decode Contract and the Total-Bit CHECK

Each `Decode<Name>(string_view, bool* started_out, TraceEntry* out)` follows the same shape:

```c
// generic Decode<EventName> contract (e.g. DecodeUhiHostPhysicalRequestRead @0xf5b0f20)
function Decode<Name>(view, started_out, entry):
    if view.length <= 0xf: return error           // need a full 16-byte packet
    BitDecoder dec(view);
    SkipBits(dec, 2);                              // skip the 2 framing bits
    DecodeTraceHeader(entry, dec);                 // id/block/timestamp into TraceHeader
    [ DecodeTraceIdHeader inline: 21/3/12 ]        // when the event carries identity
    variant = entry.mutable_<Name>();              // construct the oneof submessage
    entry[+0x28] = <oneof field number>;           // stamp the dense proto case
    GetBits64(dec, w0, &variant.f0);               // typed fixed-width payload …
    GetBits64(dec, w1, &variant.f1);
    …
    consumed = (view.length * 8) - dec.bits_remaining();   // BitsDecoded()
    CHECK(consumed == <hardcoded constant>);       // MakeCheckOpString on mismatch ⇒ FATAL
    *bytes_consumed = 0x10;                         // 16 bytes, on success
```

The final `CHECK` validates that the handler consumed exactly the bit count the format demands — `absl::log_internal::MakeCheckOpString<…>(consumed, K, "decoder.BitsDecoded() == K")` fires a fatal log on mismatch. This is the codec's self-consistency guard: it pins each event's total wire width.

Representative byte-confirmed total-bit `CHECK` constants (one per subsystem band) and their payload-width sequences (bit widths *within* the payload, after bit 61):

| Event (pxc) | id | oneof | decoder | total bits | payload widths (after bit 61) |
|---|---|---|---|---|---|
| `UhiHostDmaTransactionStartedAddressTranslation` | 0 | 2 | `0xf5b0b80` | 128 | full packet (widest UHI event) |
| `UhiHostPhysicalRequestRead` | 1 | 3 | `0xf5b0f20` | 118 | `[21/3/12]` + `30,1,1,29,26,8,20,20` |
| `TcsInternalSetSyncFlag` | 81 | 38 | `0xf5b97a0` | 121 | (no id-header) `32,1,9,16,1,1` |
| `IciPacketPacketReceivedOnLinkInput` | 40 | 21 | `0xf5b56c0` | 125 | `[21/3/12]` + `3,3,6,1,1,12,1,1` |
| `ThrottleStateThermalAndElectrical` | 97 | 54 | `0xf5bc620` | 128 | discriminated A/B (oneof `0x36`/`0x37`) |

> **CORRECTION (TEC-1) —** the per-event `CHECK` constant is **not always a single value**. `DecodeUhiHostPhysicalRequestRead` contains two `MakeCheckOpString` sites — `BitsDecoded() == 128` and `== 233` — on two branches of a conditional payload; the `118`-bit figure is the minimal no-optional-field path. The *mechanism* (a hardcoded total-bit `CHECK` per consumed path) is byte-confirmed CERTAIN; the precise constant is branch-dependent for events with optional/repeated payload fields. Treat the table constants as the representative path, and recover the exact set per branch from the event's `Decode<Name>`/`Encode<Name>` pair (HIGH confidence on the listed primary-path values).

The per-band semantic field maps — what each width *means* — are owned by the payload pages: [UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md), [SparseCore band](payload-sc-band.md), [vfc/vlc/gfc](payload-vfc-vlc-gfc.md). The exhaustive (offset, width, semantic) tuple for all ~99–144 events per family is mechanically dumpable from the `Decode<Name>`/`Encode<Name>` pairs but is **not** tabulated here (LOW confidence on completeness — same gap as the payload pages note).

---

## The Decode Driver and Per-Family Factory

### DecodeTraceBuffers — the walk loop

`xprof::tpu::DecodeTraceBuffers<TraceEntry>` @ `0xf59ffa0` (pxc instantiation) is the driver that turns a compressed device-trace blob into a `RepeatedPtrField<TraceEntry>`:

```c
// DecodeTraceBuffers<TraceEntry> @0xf59ffa0
function DecodeTraceBuffers(codec, out_entries, ..., scratch):
    StringReader src(blob);                         // @0xf59eac0
    ZlibReader  zin(&src);  zin.Initialize();       // ZlibReaderBase::Initialize @0xf69f9e0
    ReadAllImpl(&zin, &decompressed);               // read_all_internal::ReadAllImpl @0xf5acf40
    view = decompressed;
    while (view.length >= 0x10) {                   // one 16-byte packet at a time
        bool valid, started;
        TraceEntry* e = out_entries.Add();
        codec->vtable.DecodeEntry(view, e, &valid, &started);  // call *0x18(vtable)
        if (!valid) break;                          // graceful EOS
        view.remove_prefix(0x10);                   // advance 16 bytes
    }
```

The transport — the riegeli record framing around the `ZlibReader`, the zlib window/dictionary, and whether multiple per-core ring drains are separate riegeli records — is owned by [riegeli Trace Container](riegeli-trace-container.md). `DecodeTraceBuffers` itself only sees one inflated `StringReader` stream.

### TraceCodecInterface and the factory

The per-chip-family codec is a concrete `TraceCodecInterface<TraceEntry>` (abstract base; the four vtable slots are `DecodeEntry`/`EncodeEntry`/`GetMaxEntrySize`/`GetEntryPacketSize`). It is constructed by `CreateTraceCodec` per family and registered into a static type-factory keyed by `asic_sw::DeviceIdentifiers` (the chip codename), via `DeviceIdentifiersAsString`:

| Family | `CreateTraceCodec` | `DecodeEntry` | `DecodeTraceHeader` | block/ts | `DecodeTraceBuffers` template | Confidence |
|---|---|---|---|---|---|---|
| pxc | `0xf5af2c0` (`plc` symbol) | `0xf5af3a0` | `0xf5d4f20` | 3/48 | `<pxc::…::TraceEntry>` | CERTAIN |
| vfc | `0xf5f5da0` | (per family) | `0xf628080` | 6/45 | `<vxc::vfc::…::TraceEntry>` | CERTAIN |
| vlc | `0xf5d5180` | (per family) | `0xf5f5b40` | 3/48 | `<vxc::vlc::…::TraceEntry>` | CERTAIN |
| glc | `0xf6282e0` | (per family) | `0xf65eaa0` | 6/45 | `<gxc::glc::…::TraceEntry>` | CERTAIN |
| gfc | `0xf65ed00` | (per family) | `0xf697b00` | 6/45 | `<gxc::gfc::…::TraceEntry>` | CERTAIN |
| jxc | (legacy path) | — | — | — | `<jxc::PerformanceTraceEntry>` | HIGH |

### GetTraceCodec — the selector

`xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` @ `0xf5a2900` is the runtime selector. The decompile shows it walking a chain of `asic_sw::internal::TypeFactoryBase<DeviceIdentifiers, &DeviceIdentifiersAsString, TraceCodecInterface<…::TraceEntry>, false>::Create<>` attempts — one per family (vlc, vfc, glc) — and falling through to the **pxc** factory as the default:

```c
// GetTraceCodec @0xf5a2900 (decompiled skeleton)
function GetTraceCodec(out, device_ids, gen):
    if (try TypeFactoryBase<…vlc::TraceEntry>::Create(out, device_ids)) return out;
    if (try TypeFactoryBase<…vfc::TraceEntry>::Create(out, device_ids)) return out;
    if (try TypeFactoryBase<…glc::TraceEntry>::Create(out, device_ids)) return out;
    // … gfc via its own GetTraceCodec<gfc::TraceEntry> @0xf5a2b60 …
    TypeFactoryBase<…pxc::TraceEntry>::Create(out, device_ids);     // default
    return out;
```

Each family also has a templated `GetTraceCodec<…::TraceEntry>` instantiation (e.g. gfc @ `0xf5a2b60`) that wraps the family-specific `unique_ptr<TraceCodecInterface<TraceEntry>>` construction. The selector returns a `unique_ptr` the `DecodeTraceBuffers` driver then drives polymorphically.

> **QUIRK —** jxc does **not** share this codec. It uses a different proto type — `asic_sw::driver::deepsea::jxc::PerformanceTraceEntry` — decoded by its own `DecodeTraceBuffers<PerformanceTraceEntry>` instantiation, not the fixed-16-byte `TraceEntry` path. A reimplementation that assumes one packet schema across all generations will misparse jxc traces. The jxc specifics are on [Payload: jxc Legacy](payload-jxc-legacy.md).

---

## Relevant Struct and Table Offsets

| Symbol | Address / offset | Role |
|---|---|---|
| `TraceEntry` (proto2) | `+0x18` trace_header ptr; `+0x20` active oneof variant ptr; `+0x28` oneof **case** (proto field number — the encode dispatch key) | the decoded message |
| `TraceHeader` (proto2) | `+0x10` presence (bit0=id, bit1=block, bit2=ts); `+0x18` trace_point_id; `+0x1c` block_id; `+0x20` timestamp | the universal header |
| `TraceIdHeader` (proto2) | `+0x10` presence; `+0x18` transaction_id; `+0x1c` core_id; `+0x20` chip_id | the 36-bit identity sub-record |
| `BitDecoder` | `+0x8` cursor, `+0x10` end, `+0x18` buffer (LSB-first), `+0x20` bits_avail (0x28 total) | the bit-stream window |
| `mask_` | `0xbe79440` | 65-qword mask table, `mask_[k]=(1<<k)-1` |
| Decode jump table | `0xab85bc0` | 111 `rel32` entries, indexed by 8-bit `trace_point_id` (pxc) |
| Encode jump table | `0xab85fc0` | `rel32` entries, indexed by `oneof_field - 2` (pxc) |
| `MakeErrorImpl<3>` string | `0x9ff8a9c` | `"Found a valid but not started packet."` (status `0x2e02`) |

---

## Related Components

| Component | Relationship |
|---|---|
| [riegeli Trace Container](riegeli-trace-container.md) | the compressed transport that `DecodeTraceBuffers` inflates before this codec walks 16-byte packets |
| [TracePoints Master Registry](tracepoints-master-registry.md) | the wire-id ↔ oneof-field two-id-space table the dual dispatch realizes |
| [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) | the per-band payload field maps for the host-DMA/interconnect/fabric bands |
| [Payload: SparseCore Band](payload-sc-band.md) | the TCS/SparseCore band payload field maps |
| [Payload: vfc/vlc/gfc](payload-vfc-vlc-gfc.md) | the newer-family payload deltas (6-bit block_id, 45-bit timestamp) |
| [Payload: jxc Legacy](payload-jxc-legacy.md) | the separate `PerformanceTraceEntry` schema and its own codec |
| [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) | the downstream shaping of a decoded `TraceEntry` into a device-plane XEvent + XStats |

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the five-stage capture→encode→decode→xplane pipeline this codec is stage 3 of
- [riegeli Trace Container](riegeli-trace-container.md) — stage 2, the zlib/riegeli compressed transport feeding `DecodeTraceBuffers`
- [TracePoints Master Registry](tracepoints-master-registry.md) — the banded wire id and dense oneof field id spaces this page's dual dispatch keys on
- [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) — stage 5, where the decoded proto becomes an XEvent and the raw cycle timestamp is converted to picoseconds
- [Payload: UHI/OCI/ICI/DMA](payload-uhi-oci-ici-dma.md) · [SparseCore Band](payload-sc-band.md) · [vfc/vlc/gfc](payload-vfc-vlc-gfc.md) · [jxc Legacy](payload-jxc-legacy.md) — the per-band payload field maps this codec page deliberately does not duplicate
