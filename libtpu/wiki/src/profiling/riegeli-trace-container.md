# riegeli Trace Container

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text` VMA equals file offset. Other versions will differ.*

## Abstract

The riegeli Trace Container is the **transport and timebase** layer that wraps the on-device profiler codec. Where [`TraceEntriesCoder`](trace-entries-coder.md) decodes one fixed 16-byte packet into a `TraceEntry` proto, this layer answers three questions that surround that codec: how the compressed bytes arrive off the device, which per-chip codec walks them, and how the raw hardware tick on each packet becomes a wall-clock picosecond offset on an `XEvent`. It is stage 2 (transport) and stage 5 (timebase) of the [device-trace pipeline](overview.md); the codec is stage 3.

The container is deliberately *thin*. The on-the-wire object is a proto2 `RepeatedPtrField<std::string>` — one element per per-core hardware trace ring drain — and **each element is an independent zlib stream**, not a riegeli record-framed chunk file. The only `riegeli` object in play is a `riegeli::ZlibReader<riegeli::StringReader>` wrapping each whole buffer; the riegeli name buys the pooled `z_stream` and the `Reader`/`Object` lifecycle, nothing more. Inflating one buffer yields a *flat concatenation* of fixed 16-byte packets — there is no per-packet framing inside the inflated stream, so the walk is a bare `for (cursor += 16)` loop that stops at the first packet whose `valid` framing bit reads 0. This is the single most important structural fact: a reimplementation that looks for riegeli chunk headers inside the inflated bytes will never find them.

Codec selection is a two-tier factory. The public `xprof::tpu::GetTraceCodec(DeviceIdentifiers, int)` @ `0xf5a2900` runs an ordered chain of `Is<Family>` predicates — each a direct compare of the 12-byte PCI-identification POD against one of **seventeen** baked PCI-tuple constants (only one — `kPuffyliteChipIdentifiers` @ `0xbdf3c4c` — carries an ELF symbol; the other sixteen, including the two Ghostfish/gfc tuples, are unnamed in the symbol table, each identified by the `Is<Family>` accessor that compares against it inline, e.g. `IsGfc`) — then dispatches into the matching family's `TypeFactoryBase::Create`, which keys a `util_registration::StaticMapBase` singleton `std::map<DeviceIdentifiers, factory>` (`std::less<void>`, field-wise compare) and invokes the registered `CreateTraceCodec`. The map key is the *PCI ID tuple*, not a chip-codename string. Finally, each packet's `TraceHeader.timestamp` is a Global-Time-Counter tick in ×16 fixed-point; `TpuXLineBuilder::AddEvent(GtcSpan)` @ `0xf1df1e0` converts it to picoseconds with a 128-bit `round(gtc · 1e9 / (gtc_freq_hz << 4))` divide, and `XLineBuilder::SetTimestampNsAndAdjustEventOffsets` @ `0x1cf4dcc0` applies the ns→ps `×1000` line-origin rescale.

For reimplementation, the contract is:

- **The container framing** — `RepeatedPtrField<std::string>` of N independently zlib-framed (window 15, zlib/gzip auto-detect) per-buffer blobs; each inflates via `riegeli::ZlibReader<StringReader>` + a whole-stream `ReadAllImpl` drain to a flat 16-byte-packet stream with no inner record framing.
- **The per-packet walk** — `TraceCodec::DecodeInternal` validates `len >= 16 && len % 16 == 0`, then iterates `DecodeEntry` at stride 16, breaking on the `valid==0` end-of-stream sentinel.
- **The DeviceIdentifiers-keyed factory** — the `Is<Family>` PCI-tuple predicate chain → `TypeFactoryBase::Create` → `StaticMapBase` `std::map` lookup → registered `CreateTraceCodec`, returning a `std::variant` of per-family `unique_ptr<TraceCodecInterface<…>>`.
- **The clock-domain conversion** — the 45/48-bit GTC tick (×16 fixed-point), the `GtcSpan {start,length}` span built by `GetEntriesGtcSpan`, the `round(gtc · 1e9 / (clk << 4))` 128-bit divide, and the runtime (not baked) `Task.gtc_freq_hz` divisor.

| | |
|---|---|
| **Transport driver** | `xprof::tpu::DecodeTraceBuffers<TraceEntry>` @ `0xf59ffa0` (pxc; 6 per-family instantiations) |
| **Per-packet walk** | `asic_sw::driver::deepsea::profiler::TraceCodec<TraceEntry>::Decode` @ `0xf5ad800` → `DecodeInternal` @ `0xf5ada40` |
| **Decompressor** | `riegeli::ZlibReaderBase::Initialize` @ `0xf69f9e0`; `rs_inflateInit2_(windowBits=0x2f=47)` @ `0x209f2e40` |
| **Whole-stream drain** | `riegeli::read_all_internal::ReadAllImpl` @ `0xf5acf40` (size cap `-1`) |
| **Factory entry** | `xprof::tpu::GetTraceCodec(asic_sw::DeviceIdentifiers, int)` @ `0xf5a2900` |
| **Factory map** | `util_registration::StaticMapBase<…>::GetValue` @ `0xf5a3020`; singleton `std::map` root @ `0x224c6000` |
| **Factory key** | 12-byte `asic_sw::DeviceIdentifiers` PCI tuple (vendor `0x1AE0` = Google); **17** baked PCI tuples @ `.rodata 0xbdf3c0c`–`0xbdf3cdc` (only `kPuffyliteChipIdentifiers` @ `0xbdf3c4c` is an ELF symbol; the rest are unnamed, identified by `Is<Family>` xref) |
| **Timebase** | `xprof::TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata)` @ `0xf1df1e0` — `round(gtc·1e9/(clk<<4))` 128-bit divide |
| **Span build** | `…collector::TpuTraceEntries<…>::GetEntriesGtcSpan` @ `0xf1bf5e0` (min/max over `timestamp − DurationCycles·16`) |
| **ns→ps rescale** | `tsl::profiler::XLineBuilder::SetTimestampNsAndAdjustEventOffsets` @ `0x1cf4dcc0` (`×1000` = `0x3e8`) |
| **Source paths (rodata)** | `third_party/riegeli/zlib/zlib_reader.cc`; `platforms/asic_sw/proto/device_identifiers.proto`; `.../trace_codec.h`; `.../tpu/<fam>/profiler/trace_codec_factory.cc` |

---

## The Container — `DecodeTraceBuffers`

### Purpose

`DecodeTraceBuffers<TraceEntry>` @ `0xf59ffa0` is the driver: it turns the proto2 `RepeatedPtrField<std::string>` of compressed per-core trace blobs into a `RepeatedPtrField<TraceEntry>`. It owns the riegeli/zlib inflate and the per-buffer iteration; the actual 16-byte packet decode is delegated to the selected codec's virtual `Decode`.

### Entry Point

```text
DecodeTraceBuffers<TraceEntry> @0xf59ffa0    ── transport driver (6 per-family instantiations)
  └─ per buffer (decompress==true):
       riegeli::StringReader<string_view>           ── InitializerBase::ConstructMethodFromObject @0xf59eac0
       riegeli::ZlibReaderBase::Initialize @0xf69f9e0
         └─ InitializeDecompressor @0xf69fa60 ── rs_inflateInit2_(windowBits=47) @0x209f2e40
       read_all_internal::ReadAllImpl @0xf5acf40      ── drain whole inflated stream (cap -1)
       riegeli::Object::Close @0x20d97dc0             ── status check; failure ⇒ skip buffer
  └─ codec->Decode(string_view) via *0x18(vtable)  ── TraceCodec<TraceEntry>::Decode @0xf5ad800
```

### Algorithm

```c
// DecodeTraceBuffers<TraceEntry> @0xf59ffa0
function DecodeTraceBuffers(codec, compressed_buffers, decompress, out):
    for buffer in compressed_buffers:                 // RepeatedPtrField<string>, r14 += 8 per elem
        if decompress:                                 // dl gate @0xf59ffeb: test dl,dl; je raw-path
            StringReader src(buffer);                  // @0xf59eac0
            ZlibReader  zin(&src);                      // window field = 47 (@0xf5a00aa: v37=47)
            //   BufferOptions {min 0x1000, max 0x10000} packed 0x1000000001000 (@0xf5a0075)
            zin.Initialize();                           // ZlibReaderBase::Initialize @0xf69f9e0
            view = ReadAllImpl(&zin, &scratch, /*max*/ -1);   // whole-stream drain @0xf5acf40
            if (!zin.Close())                           // @0x20d97dc0
                continue;                               // MakeErrorImpl<13>("Failed to decompress trace buffer.")
        else:
            view = buffer;                              // already-raw packet bytes, no zlib
        codec->Decode(view, out);                       // virtual *0x18(vtable) → TraceCodec::Decode @0xf5ad800
```

> **GOTCHA —** the `decompress` bool is a real flow gate, not a hint. With it clear, the buffer string is treated as *already-inflated* raw packet bytes and handed straight to the codec; with it set, the buffer is one zlib stream that must be inflated first. A reimplementation that always inflates (or never inflates) will mis-handle one of the two producer modes. The device-trace capture path sets it; the gate is `test dl,dl; je` at `0xf59ffeb`.

### The Decompressor

The inflate is *stock zlib*, configured for maximum window and automatic header detection, recycled through a riegeli `z_stream` pool:

| Anchor | Address | Detail |
|---|---|---|
| `ZlibReaderBase::Initialize` | `0xf69f9e0` | constructs the reader, then `InitializeDecompressor` |
| `InitializeDecompressor` | `0xf69fa60` | pulls a `z_stream` from a `RecyclingPool<void, ZStreamDeleter>` keyed by window options |
| new-stream lambda | `0xf6a1300` | `rs_inflateInit2_(z_stream, windowBits=[this+0x78], …)` @ `0x209f2e40` |
| `windowBits` | — | `0x2f = 47 = 15 (32 KiB window) + 32 (auto-detect zlib OR gzip header)` |
| pool reset / teardown | `0x209f2ec0` / `0x209f2e00` | `rs_inflateReset2` on reuse, `rs_inflateEnd` on drop; pool guard @ `0x224c6280`, storage @ `0x224c6240` |

The `rs_` prefix marks the statically-linked vendored zlib. There is **no custom dictionary** — a stock `inflate` with a 32 KiB window and zlib/gzip auto-detection reproduces the reader exactly. The companion compressor is `riegeli::ZlibWriterBase`, driven by `xprof::tpu::(anon)::MaybeCompressTraceBuffers` (lambda invokers @ `0xf5984c0`/`0xf598580`), which compresses each raw per-buffer packet stream in place.

> **NOTE —** the writer's compression *level* and *strategy* are not recoverable from the decode path: because the reader auto-detects the header (`+32`) and the window is the maximum, the level is a producer-side knob invisible to the inflater (LOW confidence on the exact level; the reader contract — window 15, zlib/gzip auto-detect — is CERTAIN).

---

## The Per-Packet Walk — `TraceCodec::DecodeInternal`

### Purpose

`TraceCodec<TraceEntry>::Decode` @ `0xf5ad800` (thin wrapper) → `DecodeInternal` @ `0xf5ada40` is the loop that turns one inflated buffer (a flat 16-byte-packet stream) into appended `TraceEntry` messages. It validates the stream length against the fixed packet size and iterates the codec's `DecodeEntry` until the end-of-stream sentinel.

### Algorithm

```c
// TraceCodec<TraceEntry>::DecodeInternal @0xf5ada40
function DecodeInternal(view, out_entries):
    pkt = GetEntryPacketSize();                       // virtual *0x18(vtable) → 0x10 = 16
    if (view.length < pkt):
        return MakeErrorImpl<3>("Entries must be at least %d bytes.");          // @0xf5adacc (line 117)
    if (view.length % pkt != 0):                       // (unsigned)len % (unsigned)pkt
        return MakeErrorImpl<3>("Entries must be a multiple of %d bytes.");  // @0xf5adb01 (line 121)
    cursor = view.begin;
    while (cursor < view.end):                          // stride pkt (16)
        TraceEntry* e = out_entries.Add();
        bool valid, started;
        codec->DecodeEntry({cursor, pkt}, e, &valid, &started);  // virtual *0x20(vtable) @0xf5af3a0
        if (!valid):                                    // valid framing bit == 0 ⇒ graceful EOS
            out_entries.RemoveLast();                   // discard the empty sentinel slot
            break;                                       // "… done with decode. Remaining bytes : "
        cursor += pkt;                                  // advance one 16-byte packet
```

The length validation is byte-confirmed: `len < 16` → `MakeErrorImpl<3>("Entries must be at least %d bytes.")` (`%d`-formatted via `absl::str_format_internal::FormatPack`, line 117, `0xf5adacc`), and `(unsigned)len % (unsigned)pkt` → `MakeErrorImpl<3>("Entries must be a multiple of %d bytes.")` (line 121, `0xf5adb01`). The `valid==0` break is the [codec's](trace-entries-coder.md#framing-semantics--valid-and-started) graceful end-of-stream sentinel — the same framing bit the codec reads at packet bit 0.

> **QUIRK —** the inflated stream carries **no length prefix and no record framing**. The stream is terminated *in-band* by the first packet whose `valid` bit is 0, exactly the way an over-allocated ring buffer is drained. `len % 16 == 0` therefore checks alignment of the whole buffer, not record boundaries — a riegeli `RecordReader` is never instantiated inside the inflated bytes. The only riegeli object in the entire path is the per-buffer `ZlibReader`.

> **NOTE —** `DecodeInternal` also carries three diagnostic-log paths — `"Found invalid packet, skipping over it. Remaining bytes: "`, `"Found invalid packet, done with decode. Remaining bytes : "`, and `"Skipping invalid packet when continuing partial entry."` — so a torn packet mid-stream is logged-and-skipped rather than fatal, and a partial entry can be continued. The skip/continue policy is HIGH confidence (strings byte-exact); the exact partial-entry state machine was not exhaustively traced.

---

## The DeviceIdentifiers-Keyed Codec Factory

### Purpose

`xprof::tpu::GetTraceCodec(DeviceIdentifiers, int)` @ `0xf5a2900` selects one per-chip-family codec for a device. The selection key is the **12-byte PCI identification tuple**, not a codename string; the result is a `std::variant` of per-family `unique_ptr<TraceCodecInterface<…>>` that `DecodeTraceBuffers` then drives polymorphically.

### The Key — `DeviceIdentifiers`

The runtime key is a 12-byte POD, the packed form of the `asic_sw.proto.DeviceIdentifiers` message (offsets confirmed from `FromProto` @ `0x20b3ce40`, `ToString` @ `0x20b3cee0` reading `vpmovzxwd [k]` for 4×u16 + `vpmovzxbd [k+8]` for 4×u8):

| Field | Offset | Type | Proto | Meaning |
|---|---|---|---|---|
| `vendor_id` | `+0x0` | u16 | f1 | PCI vendor — always `0x1AE0` (Google) |
| `device_id` | `+0x2` | u16 | f2 | PCI device id — the chip-family discriminator |
| `subsystem_vendor_id` | `+0x4` | u16 | f3 | always `0x1AE0` |
| `subsystem_device_id` | `+0x6` | u16 | f4 | board/SKU discriminator |
| `class_code` | `+0x8` | u8 | f5 | PCI class (`0xff` legacy / `0x12` processing accelerator) |
| `subclass` | `+0x9` | u8 | f6 | |
| `programming_interface` | `+0xa` | u8 | f7 | |
| `revision_id` | `+0xb` | u8 | f8 | silicon stepping (e.g. `0x10` for Pufferfish B0) |

### The Predicate Chain

```c
// GetTraceCodec @0xf5a2900 — Is<Family> ordered dispatch
function GetTraceCodec(out_variant, device_ids, core_or_mode):
    if (IsDfc(device_ids) || IsJfc(device_ids)):                 // @0xf699000 / @0xf698fc0
        return PerformanceTrace ctor @0xf5a5060;                  // variant idx 6 — jxc legacy
    if (IsPlc(device_ids) || IsPfc(device_ids)):                 // @0xf699040 / @0xf699080
        return TypeFactoryBase<…pxc::TraceEntry>::Create(...);    // variant idx 5 — DEFAULT below too
    if (IsVlc(device_ids)):                                       // @0xf699140
        return TypeFactoryBase<…vlc::TraceEntry>::Create(...);    // variant idx 3
    if (IsVfc(device_ids)):                                       // @0xf699220
        return TypeFactoryBase<…vfc::TraceEntry>::Create(...);    // variant idx 4
    if (IsGlc(device_ids)):                                       // @0xf6992a0
        return TypeFactoryBase<…glc::TraceEntry>::Create(...);    // variant idx 1
    if (IsGfc(device_ids)):                                       // @0xf699320 → GetTraceCodec<gfc> @0xf5a2b60
        return …gfc… (variant idx 2);
    return TypeFactoryBase<…pxc::TraceEntry>::Create(...);        // pxc as final fallthrough default
```

Each `Is<Family>` is a direct compare of the packed 64-bit `DeviceIdentifiers` low word (plus the `>> 48` high half for the revision byte) against a baked constant. `IsPlc` @ `0xf699040` is byte-exact:

```c
// IsPlc @0xf699040
return (u32)*(u64*)device_ids == (u32)kPuffyliteChipIdentifiers          // vendor+device match
    && ((u64)*device_ids ^ kPuffyliteChipIdentifiers) >> 48 == 0;        // subsystem+rev match
```

> **QUIRK —** pxc is reached **two ways**: explicitly when `IsPlc||IsPfc`, and again as the *final fallthrough* when no predicate matches. A reimplementation must keep pxc as the default arm; an unknown-but-Google TPU decodes with the pxc/Puffylite codec rather than failing. jxc/dfc, by contrast, take a *separate* `PerformanceTraceEntry` codec (variant idx 6), not the fixed-16-byte `TraceEntry` path — see [jxc Legacy](payload-jxc-legacy.md).

### The Baked Chip Constants

**Seventeen** baked PCI-tuple constants live in `.rodata 0xbdf3c0c`–`0xbdf3cdc` (each 12 bytes; one 4-byte alignment gap after the two jxc tuples, before the Pufferfish block, so the seventeen records are *not* a contiguous 17×12 array). Only one carries an ELF symbol — `nm` reports exactly one `*ChipIdentifiers` symbol, `kPuffyliteChipIdentifiers` @ `0xbdf3c4c` (the plc tuple). The other sixteen tuples are unnamed in the symbol table; their per-family identity is recovered not from symbol names but from the `Is<Family>` accessor that `lea`s each tuple's address inline (jxc×2, pfc×3, plc×1, vlc×4, vfc×2, glc×3 across `0xbdf3c0c`..`0xbdf3cb8`, then the two Ghostfish/gfc tuples). The two gfc tuples at `0xbdf3cc4`/`0xbdf3cd0` (`device_id 0x0075`/`0x0076`, `subsystem_device_id 0x00f2`) are byte-confirmed real constants, referenced inline by `IsGfc` @ `0xf699320` (which `lea`s `0xbdf3cd0`/`0xbdf3cc4`). The `Is<Family>` predicates compare against all seventeen. All share `vendor=0x1AE0` and `subsystem_vendor=0x1AE0`. The discriminating axes are `device_id` (family) × `subsystem_device_id` (board) × `revision_id` (stepping):

| Family (predicate) | `device_id` | `class` | discriminating sub-id / rev | codec `TraceEntry` |
|---|---|---|---|---|
| Jellyfish/Dragonfish (`IsJfc`/`IsDfc`) | `0x0027` | `0xff` | sub `0x004e`/`0x004f` | `jxc::PerformanceTraceEntry` |
| Pufferfish B0 (`IsPfc`) | `0x005e` | `0xff` | sub `0x0050`/`51`/`52` (Mfg/Water/Air), rev `0x10` | `pxc::profiler::TraceEntry` |
| Puffylite (`IsPlc`) | `0x0056` | `0xff` | sub `0x007b` | `pxc::profiler::TraceEntry` |
| Viperlite (`IsVlc`) | `0x0063` | `0xff` | sub `0x00ae` (PF)/`0x00af` (VF), rev `0x00`/`0x01` | `vxc::vlc::profiler::TraceEntry` |
| Viperfish (`IsVfc`) | `0x0062` | `0xff` | sub `0x00ac` (PF)/`0x00ad` (VF) | `vxc::vfc::profiler::TraceEntry` |
| Ghostlite (`IsGlc`) | `0x006e`/`6f`/`70` | `0x12` | sub `0x00d1` (App PF/VF, Mgt PF) | `gxc::glc::profiler::TraceEntry` |
| Ghostfish-band (`IsGfc`) | `0x0075`/`76` | `0xff` | sub `0x00f2` | `gxc::gfc::profiler::TraceEntry` |

> **NOTE —** the family binding folds three orthogonal SKU axes. `IsPfc` matches three Pufferfish B0 sub-SKUs (Mfg/Water/Air) that differ only in `subsystem_device_id` while sharing `device_id=0x5e, rev=0x10`; PF vs VF variants differ in the last sub-id nibble; A0/A1 steppings differ in `revision_id`. Ghostlite "App" silicon switches PCI `class` to `0x12` (processing accelerator); the older families use `0xff`. The same tuple-compare is reused by `xprof::tpu::DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0` to fold a `DeviceIdentifiers` into a device-type ordinal. The PCI tuple↔codename binding is owned by the silicon map; this page owns only the factory keying.

### The Registry — `StaticMapBase`

`TypeFactoryBase<DeviceIdentifiers, &DeviceIdentifiersAsString, TraceCodecInterface<…>, false>::Create` (pxc @ `0xf5a2c20`) looks the factory up in a lazily-built singleton `std::map<DeviceIdentifiers, pair<const char*, std::function<StatusOr<unique_ptr<TraceCodecInterface>>()>>>` via `StaticMapBase::GetValue` @ `0xf5a3020`:

```c
// StaticMapBase::GetValue<DeviceIdentifiers> @0xf5a3020 — rb-tree walk, std::less<void> field-wise
function GetValue(map_singleton, key):                 // guard @0x224c6020, root @0x224c6000
    node = map.root;
    while (node):
        k = node[+0x20];                                 // node key = DeviceIdentifiers at node+0x20
        // ordered field-wise compare (the 4 u16 halves, then revision byte at +0x2b):
        if (key.vendor   != k.vendor)   { go left/right by < ; continue }   // si  vs WORD0
        if (key.device   != k.device)   { go left/right ; continue }        // WORD1
        if (key.subvend  != k.subvend)  { go left/right ; continue }        // WORD2 / HIDWORD
        if (key.subdev   != k.subdev)   { go left/right ; continue }        // WORD3 (HIWORD)
        if (key.rev      != node[+0x2b]) { go left/right ; continue }        // byte +0x2b = revision_id
        return &node[+0x38];                             // value (the registered std::function)
    return NULL;                                          // → "… is not registered" diagnostic
```

The compare order — `vendor_id`, `device_id`, `subsystem_vendor_id`, `subsystem_device_id`, then the `revision_id` byte at node `+0x2b` — is byte-confirmed (`cmp si,…; di,…; r8w,…; rcx,…>>0x30; dl,[node+0x2b]`), with the value at node `+0x38`. On a miss, `Create` builds a `"Function for DeviceIdentifiers %s with … is not registered"` diagnostic via `util::Demangle` + `DeviceIdentifiers::ToString` + `StaticMapBase::GetKeys` + absl `FormatPack` (`0xf5a2c91`..).

### Registration and Construction

Each family's `CreateTraceCodec` is the static-init registrant, inserted under a `Mutex` into the same singleton map by `InsertValue` (pxc @ `0xf5ad460`). The default builder `plc::driver::profiler::CreateTraceCodec` @ `0xf5af2c0` is byte-confirmed:

```c
// plc::driver::profiler::CreateTraceCodec @0xf5af2c0
function CreateTraceCodec():
    coder = new TraceEntriesCoder(/*8 B*/);            // operator new(8); vptr off_21771038
    codec = new TraceCodec<pxc::TraceEntry>(/*0x50 B*/);  // operator new(0x50); vptr off_21770ef8
    codec[+0x38] = &EncodeEntry policy_func;            // std::function<bool(const TraceEntry&)> wrapper
    codec[+0x10] = operator new(coder->GetMaxEntrySize());  // 0x20-byte decode scratch
    return codec;                                       // owns the TraceEntriesCoder
```

| Family | `CreateTraceCodec` | `Create` | variant idx | `DecodeTraceBuffers` instantiation |
|---|---|---|---|---|
| pxc (Puffylite/Pufferfish) | `0xf5af2c0` (`plc`), `0xf5ad5c0` | `0xf5a2c20` | 5 | `<pxc::…::TraceEntry>` @ `0xf59ffa0` |
| vlc (Viperlite) | `0xf5d5180` | `0xf5a3360` | 3 | `<vxc::vlc::…::TraceEntry>` @ `0xf59f560` |
| vfc (Viperfish) | `0xf5f5da0` | `0xf5a3aa0` | 4 | `<vxc::vfc::…::TraceEntry>` @ `0xf59fa80` |
| glc (Ghostlite) | `0xf6282e0` | `0xf5a41e0` | 1 | `<gxc::glc::…::TraceEntry>` @ `0xf59e540` |
| gfc (Ghostfish-band) | `0xf65ed00` | (anon wrapper `0xf5a2b60`) | 2 | `<gxc::gfc::…::TraceEntry>` @ `0xf59f040` |
| jxc (Jellyfish/Dragonfish) | (legacy) | ctor `0xf5a5060` | 6 | `<jxc::PerformanceTraceEntry>` @ `0xf5a04c0` |

> **GOTCHA —** the variant *index* is not the predicate order. The `std::variant<monostate, glc, gfc, vlc, vfc, pxc, jxc::PerformanceTrace>` payload (8-byte ptr + 1-byte index at `+0x8`) numbers glc=1, gfc=2, vlc=3, vfc=4, pxc=5, jxc=6, monostate=0 — distinct from the `Is*` evaluation order. A reimplementation that conflates "the third predicate" with "variant index 3" mis-tags every codec.

---

## The GTC-Tick → Picosecond Timebase

### The Clock Domain

The `TraceHeader.timestamp` field — 48 bits for pxc/vlc, 45 bits for vfc/glc/gfc (the [codec page](trace-entries-coder.md#per-gen-header-widths)) — is **not** a free-running per-core cycle counter. It is a **Global Time Counter (GTC) tick**: the chip-wide, cross-core, cross-chip synchronized counter programmed by `SetGtcConfiguration` (slice_builder gRPC @ `0x1fc4f300`). The low **4 bits are a fractional sub-tick**; the integer tick is bits `[4..]`. A single per-line GTC frequency therefore converts every device-plane event to one common wall-clock — which is precisely why the codec must *not* be the place the conversion happens.

### The Span — `GetEntriesGtcSpan`

`…collector::TpuTraceEntries<…>::GetEntriesGtcSpan` @ `0xf1bf5e0` collects the GTC range over the drained entries:

```c
// GetEntriesGtcSpan @0xf1bf5e0 — span over the ×16 fixed-point GTC domain
function GetEntriesGtcSpan(entries) -> GtcSpan {start, length}:
    min = +inf; max = 0;
    for e in entries:
        t = e.TraceHeader.timestamp;                    // +0x20
        t -= 16 * DurationCycles(e);                     // <<4 to match the GTC ×16 fixed-point
        min = (t < min) ? t : min;                       // cmovb @0xf1bf6a4
        max = (t > max) ? t : max;                       // cmovbe @0xf1bf6e9
    return { start: min, length: max - min };
```

`DurationCycles` @ `0xf699900` reads the per-event cycle count, returning non-zero only for the BC band's CmqVpuDma duration events (trace_point_id `0x64`..`0x77` = 100..119; the body gate is `(unsigned)(id − 100) > 0x13`) and `0` otherwise; the `×16` shift aligns it with the GTC fixed-point. The `GtcSpan` value occupies bits `[4..44]` — a 41-bit field, masked by `0x1ffffffffff0`.

### The Conversion — `AddEvent(GtcSpan)`

```c
// TpuXLineBuilder::AddEvent(GtcSpan, XEventMetadata) @0xf1df1e0 — byte-confirmed
//   ABI: rdi = this (builder); rdx:r8 = GtcSpan {start,length} by value; rsi = metadata ref `m`
function AddEvent(this, span, m):
    gtc   = span.start & 0xFFFFFFFFFFFFFFF0;             // clear the 4 fractional bits (@0xf1df247)
    clk16 = 16 * *(*(m + 0x10));                         // [m+0x10] = per-line GTC-clock; clk << 4 (@0xf1df256 shl 4)
    num   = 0x3B9ACA00 * (u128)gtc;                      // × 1e9, 128-bit mul (@0xf1df24b mov 0x3b9aca00)
    value = __udivti3(num + (clk16 >> 1), clk16);        // round-to-nearest 128-bit divide
    //  XStat "device_offset_ps" = value (start);  XStatMetadata id from [m+0x38]  (@0xf1df205)
    //  length side via GtcSpanConverter::TimespanFromGtcSpan((GtcSpan) on *(m+0x10), span)
    //    → XStat "device_duration_ps";  XStatMetadata id from [m+0x40]
```

The masks (`0x1ffffffffff0` value window, `0xfffffffffffffff0` low-4 clear), the `0x3B9ACA00` (=1e9) multiply, the `clk << 4`, the `+clk16/2` round, and the `__udivti3` 128-bit divide are all byte-confirmed at `0xf1df1e0`. Algebraically, with `gtc` carrying the ×16 scale and `clk16 = freq × 16`:

```text
value_ps = gtc · 1e9 / (gtc_freq_hz · 16)
```

> **QUIRK —** the divisor is `gtc_freq_hz << 4`, *not* `gtc_freq_hz`, because the GTC tick itself is in ×16 fixed-point. The two ×16 factors (numerator tick and denominator clock) cancel; getting only one of them right yields a result off by 16×. The exact unit of `*(*(m+0x10))` (Hz vs kHz vs ticks/ps) is inferred — the end-to-end picosecond result is byte-exact, the intermediate clock-object unit is LOW confidence. (The divisor and the two XStatMetadata ids are read off the metadata-reference argument `m` in `rsi`, not off the builder `this` in `rdi`.)

> **NOTE —** the length (duration) side routes through `xprof::tpu::GtcSpanConverter::TimespanFromGtcSpan(GtcSpan) const` @ `0xf2cb7e0` (called on the converter object `*(m+0x10)`) rather than inlining the same divide; the result feeds the `device_duration_ps` XStat. The start side inlines the divide directly. Both ultimately compute `gtc · 1e9 / (freq · 16)`; the start path is the canonical, fully byte-traced form.

### The Line Origin — `SetTimestampNsAndAdjustEventOffsets`

`tsl::profiler::XLineBuilder::SetTimestampNsAndAdjustEventOffsets` @ `0x1cf4dcc0` stamps the `XLine` origin and rescales the device-relative offsets into the same picosecond domain:

```c
// SetTimestampNsAndAdjustEventOffsets @0x1cf4dcc0
function SetTimestampNsAndAdjustEventOffsets(this, new_timestamp_ns):
    delta_ps = 1000 * (XLine.timestamp_ns - new_timestamp_ns);   // ×1000 = 0x3e8, ns→ps (@line 16)
    XLine.timestamp_ns = new_timestamp_ns;                        // XLine+0x40 (@line 17)
    for event in XLine.events:
        event.offset_ps += delta_ps;                              // shift every event into the new origin
```

The `×1000` (`0x3e8`) is the nanosecond→picosecond conversion of the line origin; `XLine.timestamp_ns` lives at `XLine+0x40`. Net result: every device-plane `XEvent.offset_ps`/`duration_ps` is picoseconds, derived from the GTC tick divided by the GTC frequency and re-anchored to the line's ns origin. The downstream XEvent shaping is owned by [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md).

### The Clock Constants — Runtime, Not Baked

The frequencies feeding the divisor are **device-info captured at profile time**, serialized into the xprof `Task` proto (`task.proto`), not compile-time constants in libtpu:

| Field | Proto # | Type | rodata descriptor | Role |
|---|---|---|---|---|
| `tensor_core_freq_hz` | f11 | uint64 | `0xbe99b16` | TensorCore cycle clock (per-engine duration cycle→time) |
| `sparse_core_freq_hz` | f12 | uint64 | `0xbe99b33` | SparseCore cycle clock |
| `gtc_freq_hz` | f13 | uint64 | `0xbe99b50` | **the GTC tick rate** — the `AddEvent` divisor |

> **GOTCHA —** the conversion *algebra* is byte-exact, but the numeric Hz values cannot be read from the binary — they are runtime inputs in the `Task` proto. A reimplementation must read `gtc_freq_hz` from the captured profile (or the on-device `GtcConfiguration` the driver programs), never assume a constant. This also fixes the trace wrap period: `2^48 / gtc_freq_hz` seconds for pxc/vlc, `2^45 / gtc_freq_hz` for vfc/glc/gfc. The `task.proto` schema is owned by [Task Proto](task-proto.md). Which freq applies to which *payload* duration field per subsystem band (TensorCore vs SparseCore) is inferred from the field names, not traced through every subscriber (LOW confidence).

---

## The Transport + Timebase Pipeline

```text
per-core HW trace ring drain
   │  raw bytes = flat concat of 16-byte LSB-first packets (codec page), one per buffer
   ▼
MaybeCompressTraceBuffers  →  riegeli ZlibWriter per buffer  (window 15, zlib/gzip)
   │  RepeatedPtrField<std::string>   (N independently zlib-framed buffers)
   ▼
DecodeTraceBuffers<TraceEntry> @0xf59ffa0
   │  per buffer: StringReader → ZlibReader (inflateInit2 windowBits=47) → ReadAllImpl(whole)
   ▼  one std::string of flat 16-byte packets
TraceCodec::Decode/DecodeInternal @0xf5ad800 / @0xf5ada40
   │  CHECK len>=16 && len%16==0 ; loop stride 16 → DecodeEntry per packet (valid==0 ⇒ EOS)
   ▼  RepeatedPtrField<TraceEntry>
[ codec selected once up front: ]
GetTraceCodec(DeviceIdentifiers, …) @0xf5a2900
   │  Is<Family>(PCI tuple) → TypeFactoryBase::Create → StaticMapBase::GetValue(std::map)
   │     → registered CreateTraceCodec() → TraceEntriesCoder
   ▼
…Subscriber::ProcessTraceEntry  →  GetEntriesGtcSpan @0xf1bf5e0
   │  GtcSpan = min/max over (TraceHeader.timestamp − DurationCycles·16)   [×16 GTC fixed-point]
   ▼
TpuXLineBuilder::AddEvent(GtcSpan) @0xf1df1e0
   │  device_*_ps = round(gtc · 1e9 / (gtc_freq_hz << 4))   (128-bit udiv)
   │  XLine timestamp_ns origin + offset ×1000 (ns→ps) in SetTimestampNsAndAdjustEventOffsets
   ▼
device-plane XEvent (offset_ps / duration_ps in picoseconds)
```

---

## Relevant Struct and Table Offsets

| Symbol | Address / offset | Role |
|---|---|---|
| `DeviceIdentifiers` (runtime POD, 12 B) | `+0x0` vendor_id, `+0x2` device_id, `+0x4` subsys_vendor, `+0x6` subsys_device, `+0x8` class, `+0x9` subclass, `+0xa` prog_iface, `+0xb` revision | the factory key (PCI tuple) |
| `StaticMapBase` singleton (pxc) | guard `0x224c6020`; map root `0x224c6000`; node key `+0x20` (+ rev byte `+0x2b`); value `+0x38` | the codec registry `std::map` |
| `kXxxChipIdentifiers` (+ 2 unnamed gfc) | `.rodata 0xbdf3c0c`–`0xbdf3cdc` | **17** baked PCI-tuple constants (only `kPuffyliteChipIdentifiers` an ELF symbol; rest identified by `Is<Family>` xref), vendor `0x1AE0` |
| `GtcSpan` | `{u64 start; u64 length}`, value bits `[4..44]` | the ×16 GTC fixed-point min/max span; passed by value (`rdx`=start, `r8`=length) |
| `AddEvent` metadata arg (`rsi`) | `+0x10` per-line GTC-clock object (`[0]` = divisor base); `+0x38`/`+0x40` offset/duration XStatMetadata ptrs | read by `AddEvent` off the 2nd pointer arg, not the builder `this` |
| `XLine` | `+0x40` timestamp_ns origin | the line-origin field rescaled `×1000` |
| `ZlibReaderBase` | `+0x78` windowBits (`0x2f=47`); pool guard `0x224c6280`, storage `0x224c6240` | the decompressor state |
| `TraceEntriesCoder` vtable (pxc) | obj vptr `off_21771038`; `TraceCodec<pxc>` vptr `off_21770ef8`; `GetEntryPacketSize`→`0x10`, `GetMaxEntrySize`→`0x20` | the codec object the factory builds |

---

## Related Components

| Component | Relationship |
|---|---|
| [TraceEntriesCoder](trace-entries-coder.md) | the per-packet codec this container inflates the bytes for and the factory builds; `DecodeInternal` calls its `DecodeEntry` per 16-byte packet |
| [TracePoints Master Registry](tracepoints-master-registry.md) | the wire-id ↔ oneof-field id spaces the codec keys on; the `DurationCycles` BC-band duration ids come from there |
| [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) | the downstream subscriber that calls `AddEvent(GtcSpan)` and turns the converted ps offsets into device-plane XEvents/XStats |
| [Task Proto](task-proto.md) | the `Task` message carrying `gtc_freq_hz`/`tensor_core_freq_hz`/`sparse_core_freq_hz`, the runtime clock divisors |
| [Payload: jxc Legacy](payload-jxc-legacy.md) | the separate `PerformanceTraceEntry` codec (variant idx 6) jxc/dfc take instead of the fixed-16-byte path |
| [Profiling and Telemetry Overview](overview.md) | the five-stage capture→encode→decode→xplane pipeline this page is stages 2 and 5 of |

## Cross-References

- [Profiling and Telemetry Overview](overview.md) — the device-trace pipeline; this page owns the transport (stage 2) and timebase (stage 5)
- [TraceEntriesCoder](trace-entries-coder.md) — stage 3, the fixed 16-byte packet codec this container frames and the factory constructs
- [TraceEntry → XEvent/XStat](trace-entry-to-xevent.md) — stage 5 consumer, where the GTC→ps conversion lands as XStat `device_offset_ps`/`device_duration_ps`
- [TracePoints Master Registry](tracepoints-master-registry.md) — the trace-point id space; the BC-band duration ids `0x64`–`0x77` (100–119) that `DurationCycles` reads
- [Task Proto](task-proto.md) — the runtime `gtc_freq_hz`/`*_core_freq_hz` clock sources the timebase divides by
- [Payload: jxc Legacy](payload-jxc-legacy.md) — the legacy `PerformanceTraceEntry` codec the factory selects for jxc/dfc
