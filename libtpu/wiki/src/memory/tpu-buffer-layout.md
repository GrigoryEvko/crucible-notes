# TPU Buffer Layout

> *All addresses, struct offsets, and field tags on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions will differ.*

## Abstract

A PJRT device buffer on a TPU is a flat run of HBM bytes whose internal arrangement is *not* row-major. Between the logical `xla::Shape` a user hands to the runtime (a `(element_type, dimensions[], layout)` triple) and the bytes that actually land in HBM sits one translation, `xla::jellyfish::TransferSizeUtil::HostShapeToDeviceShape`, which (a) chooses a minor-to-major dimension order, (b) pads the two minor-most dimensions up to the chip's `(sublane, lane)` tile, (c) packs sub-byte and splits >32-bit element types, and (d) stamps an `xla::Tile` list onto the layout describing the physical tiling. The padded *device shape* is what the allocator sizes, what a DMA descriptor addresses, and what `ShapeSizeBytesRaw` measures. This page documents that translation: the shape↔layout data model, the tile-padding arithmetic, the element-packing rules, and the on-device buffer residency record (`xla::ShapedBuffer` → `XLA_ShapedBuffer`) that names which HBM addresses hold a buffer's leaves.

The reader who knows XLA on GPU should hold one analogy and immediately complicate it. On a GPU an `xla::Layout` is a `minor_to_major` permutation and little else; the bytes are dense row-major-after-permutation. On TPU the layout *additionally* carries a tile (`xla::Layout::tiles()`, a vector of `xla::Tile`), and the physical buffer is the logical array reshaped into `(sublane, lane)` tiles laid out tile-major. The lane dimension is 128 across every generation; the sublane dimension is `8` on this v5+/v6e/TPU7x build (an earlier generation used `16`) — the *same* `(SublaneCount, LaneCount)` tile [layout assignment](../compiler/layout-assignment.md) stamps onto every leaf shape. The hardware reads a tile as one contiguous chunk, so a `[3, 5]` f32 array does not occupy 60 bytes: it pads to `[8, 128]` (one tile) and occupies one granule, with the trailing lanes/sublanes filled (with `0xFF` on the pad path). Wasted padding is real HBM, and minimising it is exactly why the [layout-assignment chooser](../compiler/layout-assignment.md#per-tensor-chooser--findmemoryminimizinglayout) optimises for compact byte size.

This page owns the *on-device tiled buffer*: the shape→device-shape mapping (`HostShapeToDeviceShape` / `SetPaddedShape`), the tile-padding rules (`Pad2ndMinorCompact`, `GetCompactTiles`), the element-packing factors, the byte-size functions (`ShapeSizeBytesRaw` / `ShapeSizeCompact` / `ShapeWithMetadataSizeBytes`), and the residency record. It does **not** reproduce the compile-time *choice* of `minor_to_major` (that is [layout-assignment.md](../compiler/layout-assignment.md)), the HBM free-list allocator that hands out the offset (that is [hbm-allocator.md](hbm-allocator.md)), the DMA wire alignment (that is [hbm-dma-alignment.md](hbm-dma-alignment.md)), or the PJRT `PJRT_Buffer` ABI and external refcounting (that is [pjrt/buffer-and-memory.md](../pjrt/buffer-and-memory.md)).

For reimplementation, the contract is:

- **The shape↔layout data model** — the `xla::Shape` / `xla::Layout` fields the device layout reads, and the `XLA_Shape` / `XLA_Layout` C-ABI mirror with their exact offsets.
- **The shape→device-shape mapping** — `HostShapeToDeviceShape` → `SetPaddedShape`: unpack sub-byte types, recurse, pad the minor dims, stamp the tile.
- **The tile-padding rules** — minor dim → multiple of `LaneCount` (else next pow2); 2nd-minor via `Pad2ndMinorCompact`; the `granule_bytes % sublane_bytes == 0` invariant.
- **The element packing** — `ElementPackingFactor` (sub-byte pack via `tc_max_packing_factor`), and the 64/128-bit split (`ComponentShape<64>`/`<128>`).
- **The residency record** — `xla::ShapedBuffer` (on-device shape + per-leaf `DeviceAddressBase`), mirrored to `XLA_ShapedBuffer`.

| | |
|---|---|
| **Host→device shape** | `xla::jellyfish::TransferSizeUtil::HostShapeToDeviceShape` (via `HardwareLayout::HostShapeToDeviceShape` @ `0xeab0e20`) |
| **Padded-shape core** | `TransferSizeUtil::SetPaddedShape` @ `0x1d6ae0e0` |
| **Tile builder** | `TransferSizeUtil::GetCompactTiles` @ `0x1d6b11c0` (returns `inlined_vector<xla::Tile,3>`) |
| **2nd-minor pad** | `TransferSizeUtil::Pad2ndMinorCompact` @ `0x1d6af5c0` |
| **Element packing** | `TransferSizeUtil::ElementPackingFactor` @ `0x1d6b03e0` (table-driven, `kPackingFactors[35]`) |
| **Padded byte size** | `TransferSizeUtil::ShapeSizeBytesRaw` @ `0x1d6add40`; with metadata `ShapeWithMetadataSizeBytes` (via `HardwareLayout::ShapeSize` @ `0xeab0ec0`) |
| **Compact byte size** | `TransferSizeUtil::ShapeSizeCompact` @ `0x1d6ae8a0` (via `HardwareLayout::ShapeSizeCompact` @ `0xeab0f20`) |
| **Tile stamp** | `xla::jellyfish::HardwareLayout::PopulateShape`; per-leaf `TransferSizeUtil::UpdateLeafLayout` @ `0x1d6b08c0` |
| **Linearizer** | `TransferSizeUtil::LinearIndex` @ `0x1d6b0600` → `xla::LayoutUtil::LinearIndex` when tiled |
| **HBM repacker** | `RepackToHardwareLayout<sublane,128>` @ `0x1d5c3b40` (`<16,128>`), `0x1d5c2880` (`<2,128>`), `0x1d5c3f80` (`<32,128>`) |
| **Residency record** | `xla::ShapedBuffer` → `XLA_ShapedBuffer` (`ApiConverter::ToC` @ `0xfcb8580`) |
| **Default tile** | `(SublaneCount, LaneCount)` = `(8, 128)` this build; `(16, 128)` on an earlier generation |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The Shape / Layout Data Model

### Purpose

Everything on this page operates on two C++ objects: `xla::Shape` and the `xla::Layout` it contains. A reimplementer must reproduce these fields exactly, because the device-layout code reads them by raw offset and the runtime mirrors them across the C ABI into `XLA_Shape` / `XLA_Layout` for the PJRT/`stream_executor` boundary. The compile-time pass that *fills in* `minor_to_major` and the `memory_space` is [layout assignment](../compiler/layout-assignment.md); this page is the consumer.

### The `xla::Layout` fields the device path reads

`xla::Layout` is an `absl::InlinedVector`-rich struct; the device code touches only a handful of members. The C-ABI mirror `XLA_Layout` (built by `ApiConverter::ToC(const xla::Layout&, XLA_Layout*)` @ `0xfcb7ca0`) pins the offsets, and the on-device functions reach the same data through `xla::Shape::layout()` (returns a `Layout*`).

| Field | `xla::Layout` access | `XLA_Layout` offset | Meaning |
|---|---|---|---|
| `minor_to_major[]` | `layout()[2..3]` inlined vector (count at `[2]>>1`) | `+0` (ptr), `+48` (count) | Physical dim order, minor-first |
| `dim_level_types` / flags | `layout()` low bytes | `+400`, `+404` | sparse/dense level kinds |
| `index_primitive_type` / `pointer_primitive_type` | `*((qword*)layout+1)` | `+408` | sparse index/pointer types |
| `element_size_in_bits` | `layout()[1]` | `+416` | non-zero for packed/odd-width elements; `0` ⇒ use dtype byte size |
| `memory_space` | `*((char*)layout+2)` | `+424` | tier color (HBM/VMEM/…); see [overview.md](overview.md#2-the-memoryspace-enum) |
| `tiles[]` | `layout()[9]` is `tiles().size()` | `+56` (ptr), `+392` (count) | the physical tile list (each `xla::Tile` is a dim vector) |
| `tail_padding_alignment_in_elements` | `*((qword*)layout+24)` | `+432` | trailing-element pad quantum |

The single most consulted field is `layout()[9]` — **the tile count**. `LinearIndex` (`0x1d6b0600`), `ShapeSizeBytesRaw` (`0x1d6add40`), and `HasLinearLayout` (`0x1d6b0160`) all branch on whether `tiles().size() >= 2` (or `> 1`): a populated tile list means the buffer is tiled and addressed via `ShapeUtil::ArraySize` / `LayoutUtil::LinearIndex`; an empty tile list means a *linear* (untiled) buffer.

### The `xla::Shape` fields

`xla::Shape` (and its `XLA_Shape` mirror, `ApiConverter::ToC` @ `0xfcb7940`) carries the dims and the layout. `XLA_Shape` offsets, decoded directly from the converter:

```text
XLA_Shape (536 bytes per element in a tuple array):
  +0    element_type      (PrimitiveType, int32)
  +8    dimensions[]      (int64 array)          count @ +56
  +64   dynamic_dims[]    (bool array)           count @ +72
  +80   tuple_shapes[]    (XLA_Shape array, 536 B stride)  count @ +88
  +92   has_layout        (bool)
  +96   layout            (XLA_Layout, present iff has_layout)
```

> **NOTE —** the on-device functions special-case three `element_type` families up front, recovered from the validity bitmask `0x2FFF91FFE` tested with `_bittest64` (the searchable array types, ≤ 0x21), the mask-`0x20`-family token-and-opaque types, and the bitmask `0x400048000` for the 64/128-bit families. The same `0x2FFF91FFE` mask gates [the layout-assignment chooser](../compiler/layout-assignment.md#per-tensor-chooser--findmemoryminimizinglayout); it is the canonical "is this a tiled array element type" predicate.

### Function Map

| Function | Address | Role |
|---|---|---|
| `ApiConverter::ToC(const xla::Layout&, XLA_Layout*)` | `0xfcb7ca0` | `xla::Layout` → C ABI; pins layout offsets |
| `ApiConverter::ToC(const xla::Shape&, XLA_Shape*)` | `0xfcb7940` | `xla::Shape` → C ABI; recurses tuple shapes |
| `ApiConverter::FromC(XLA_Shape*)` | `0xfcb7400` | C ABI → `xla::Shape` for runtime calls |
| `xla::jellyfish::TransferSizeUtil::HasLinearLayout(const Layout&)` | `0x1d6b0160` | tile list empty ⇒ linear |

---

## 2. Host Shape → Device Shape

### Purpose

`HostShapeToDeviceShape` is the entry point that turns a logical host `xla::Shape` into the physical *device shape* the TPU stores. It is what the transfer manager, the allocator-sizing path, and the C shim `Tpu_OnDeviceShape` call. The work is delegated to `TransferSizeUtil::HostShapeToDeviceShape`, which fans out to `SetPaddedShape` per leaf and re-stamps the layout. The padded device shape differs from the host shape in three ways: padded minor dims, packed/split element types, and a populated `tiles()` list.

### Entry Point

```text
HardwareLayout::HostShapeToDeviceShape (0xeab0e20)        ── C-ABI wrapper
  ├─ ApiConverter::FromC(host XLA_Shape -> xla::Shape)
  ├─ GetRegisteredDeepseaPlatform / DeepseaPlatform::GetTopology   ── the tpu::TpuTopology*
  ├─ GlobalTpuCompEnv()                                    ── reads comp-env+4124, +4373 (pack flags)
  ├─ TransferSizeUtil::HostShapeToDeviceShape(topology, shape, ...)
  │     └─ per leaf: SetPaddedShape (0x1d6ae0e0) -> HardwareLayout::PopulateShape
  └─ ApiConverter::ToC(device xla::Shape -> XLA_Shape)
```

### Algorithm — SetPaddedShape

`SetPaddedShape` (`0x1d6ae0e0`) is the core of the mapping. It is recursive on packed element types and computes, per minor dimension, the padded extent before handing the dims to `HardwareLayout::PopulateShape` to stamp the tile.

```c
function SetPaddedShape(topology, shape /*buffer leaf*/, out_shape):   // 0x1d6ae0e0
    REQUIRE(LayoutUtil::HasLayout(shape))            // FATAL "Can't pad a shape without knowing its layout", line 1019

    // ---- Step 1: sub-byte / packed element types -> unpack, recurse, re-stamp ----
    pack = ShouldPackPREDAsSingleBit(topology, shape)         // PRED -> 1-bit packing
    if ElementPackingFactor(shape.element_type(), pack) >= 2:
        unpacked = GetUnpackedShape(topology, shape)          // widen the packed dtype
        SetPaddedShape(topology /*unpacked*/, shape, out_shape)   // recurse on unpacked
        out_shape.set_element_type(shape.element_type())      // restore original packed dtype
        ForEachMutableSubshape(out_shape, UpdateLayout)       // re-stamp tile per leaf
        return OK

    pbytes = ShapeUtil::ByteSizeOfPrimitiveType(shape.element_type())

    // ---- Step 2: rank<=1 scalar/vector fast path ----
    if shape.dimensions().size() <= 1:
        CHECK(scalar_layout.minor_to_major().size() == 0)     // line 1040
        chunk_bytes = 4 * topology.chunk_size_elems            // this[53]
        CHECK(chunk_bytes % pbytes == 0)                       // line 1043
        out_shape = PopulateShape(et, {chunk_bytes/pbytes}, tile=1, layout)
        return OK

    // ---- Step 3: only 4-byte (or PRED) element widths are implemented for tiling ----
    if shape.element_type() != PRED and pbytes != 4:
        return Unimplemented("Attempted to map shape ... to on-device TPU padded shape   // line 1050
                              but this is not implemented")

    // ---- Step 4: pad each minor dim up to its chunk bound ----
    padded_dims = copy(shape.dimensions())
    for d in [0 .. rank):                                       // walk in minor_to_major order
        dim_idx  = minor_to_major[rank-1 - d]
        bound    = ChunkBound(topology, d, rank, dim_idx)        // 0x1d6b22e0
        extent   = shape.dimensions(dim_idx)
        padded_dims[dim_idx] = round_up(extent, bound)           // ceil(extent/bound) * bound

    // ---- Step 5: stamp the tile + memory_space ----
    out_shape = HardwareLayout::PopulateShape(et, padded_dims, ntiles, layout)
    return OK
```

> **GOTCHA —** Step 3 is a hard wall: `SetPaddedShape` only implements tiling for **4-byte element widths** (and PRED). Anything wider takes the 64/128-bit *split* path in `ShapeSizeBytesRaw` (§4) before it ever reaches a 4-byte `SetPaddedShape`; anything narrower is *packed* up to a 4-byte-equivalent in Step 1. A reimplementation that tries to tile a raw bf16 or s8 buffer directly will hit the `Unimplemented` at line 1050 — the design forces every element width into the 4-byte tiling kernel via pack/split first.

> **QUIRK —** the rank≤1 fast path (Step 2) does **not** pad to the 2D `(sublane, lane)` tile. A scalar or 1-D buffer is reshaped to a single chunk of `chunk_bytes/pbytes` elements (`chunk_bytes = 4 * topology.chunk_size_elems`). Only rank≥2 buffers get the 2-minor tile. This is why a 1-D vector of N f32 elements rounds up to a chunk multiple, not to a `[1, 128]` lane tile.

### Function Map

| Function | Address | Role |
|---|---|---|
| `HardwareLayout::HostShapeToDeviceShape` | `0xeab0e20` | C-ABI host→device shape |
| `TransferSizeUtil::SetPaddedShape` | `0x1d6ae0e0` | Per-leaf pad + tile stamp |
| `TransferSizeUtil::GetUnpackedShape` | `0x1d6b2100` | Widen a packed dtype for the recursion |
| `TransferSizeUtil::ShouldPackPREDAsSingleBit` | `0x1d6b0080` | PRED 1-bit packing predicate |
| `TransferSizeUtil::ChunkBound` | `0x1d6b22e0` | Per-dim chunk bound used in the pad round-up |
| `HardwareLayout::PopulateShape` | `0x1d6da360` | Stamp dims + tile + `memory_space` |
| `XlaShapeToTpuPaddedShape` (C shim) | `0xeabf0e0` | `tensorflow::XlaTpuPaddedShapeFn` boundary |
| `XlaShapeToTpuShapeRepresentation` (C shim) | `0xeabefa0` | Shape-representation boundary |

---

## 3. The Tile and the Padding Rules

### Purpose

A device buffer's physical bytes are the logical array reshaped into `(sublane, lane)` tiles, laid out tile-major. This section gives the tile geometry, the exact 2nd-minor padding arithmetic, and the `granule % sublane` invariant a reimplementer must enforce. The tile values are read from the chip's `tpu::TpuTopology` at boot, never hard-coded.

### The tile geometry

The default leaf tile is `(SublaneCount, LaneCount)`, the same `xla::Tile` [layout assignment stamps onto every leaf](../compiler/layout-assignment.md#depth-aware-layout-cost-and-the-tile):

- **`LaneCount` = 128** across every generation (topology field `*((qword*)topology+52)`, used as the minor-dim bound).
- **`SublaneCount`** = topology field `*((qword*)topology+51)` — **8** on this v5+/v6e/TPU7x build, **16** on an earlier generation (the 2nd-minor bound).
- **`granule_bytes`** = `*(qword*)(topology->[1] + 200)` — the hardware DMA granule. `GetCompactTiles` and `Pad2ndMinorCompact` both `CHECK(granule_bytes % sublane_bytes == 0)` at line 77, where `sublane_bytes = 4 * topology->[51]` (4 bytes × sublane count).

A leaf's physical run is therefore: tiles tiled over the two minor dims, each tile `SublaneCount × LaneCount` 4-byte slots, padded out. For sub-byte dtypes an extra *subtile* is appended (§3.3).

### The 2nd-minor padding — Pad2ndMinorCompact

`Pad2ndMinorCompact` (`0x1d6af5c0`) computes the padded 2nd-minor extent and the per-tile element count. It is the precise rule a reimplementer must copy.

```c
function Pad2ndMinorCompact(topology, extent /*2nd-minor logical*/, element_type):  // 0x1d6af5c0
    lane = topology[52]                                  // LaneCount, 128
    if extent >= lane:
        padded = round_up(extent, lane)                  // ceil(extent/lane) * lane
    else:
        padded = next_pow2(extent)                       // 1 << ceil_log2(extent)

    granule_bytes  = *(topology->[1] + 200)
    sublane_bytes  = 4 * topology[51]                    // 4 * SublaneCount
    CHECK(granule_bytes % sublane_bytes == 0)            // line 77
    per_tile_rows  = (granule_bytes / sublane_bytes) * ElementPackingFactor(element_type, /*pack=*/0)

    return max(padded, per_tile_rows)
```

> **QUIRK —** the sub-`LaneCount` branch rounds to the **next power of two**, not to the lane count. A 2nd-minor extent of 5 pads to 8, not to 128; an extent of 100 pads to 128 (the lane multiple). The padded extent is then floored at `per_tile_rows` so a tile is never smaller than one granule's worth of sublanes. A reimplementation that always rounds the 2nd-minor to `LaneCount` will over-pad every small buffer and disagree with libtpu's HBM footprint.

### Element packing — ElementPackingFactor

`ElementPackingFactor` (`0x1d6b03e0`) returns how many logical elements share one 4-byte physical slot. It is fully table-driven:

```c
function ElementPackingFactor(topology, element_type, pack_pred_as_bit):   // 0x1d6b03e0
    tc_max = topology->[432]                              // tc_max_packing_factor, a power of two
    REQUIRE(popcount(tc_max) == 1)                        // else FATAL "Unsupported tc_max_packing_factor", line 1489
    k = tzcnt(tc_max)                                     // 0..5  (1,2,4,8,16,32)
    REQUIRE(element_type < 0x23)                          // 35 primitive types
    if k == 0: return 1                                   // no packing
    return kPackingFactors[2^k][pack_pred_as_bit][element_type]
```

The `kPackingFactors[35]` tables are per-`tc_max` (`<2>`, `<4>`, `<8>`, `<16>`, `<32>`) and per-pack-mode (the `pack_pred_as_bit` flag selects the `true` table for `tc_max >= 8`, packing PRED as a single bit). The factor for a given dtype is the number of logical elements that fit in 4 bytes at that `tc_max`. This is the same packing the [layout-assignment `ShapeSizeCompact`](../compiler/layout-assignment.md) folds into its byte count.

> **NOTE —** sub-byte packing adds *subtiles* to the tile list. In `GetCompactTiles`, after the main `(sublane, lane)` tile is pushed, if `ElementPackingFactor(...) >= 2` it appends a `GetSubtileForPacking` tile (and, when the minor dim has extent 1, a `GetSubtileForBreakingMinorDimensionForPacking` tile). So a packed buffer's `tiles()` list has length 2–3, and a reimplementer must emit the subtile or the bytes will not match.

### Function Map

| Function | Address | Role |
|---|---|---|
| `TransferSizeUtil::GetCompactTiles` | `0x1d6b11c0` | Build the `inlined_vector<xla::Tile,3>` for a leaf |
| `TransferSizeUtil::Pad2ndMinorCompact` | `0x1d6af5c0` | 2nd-minor pad + per-tile rows |
| `TransferSizeUtil::ElementPackingFactor` | `0x1d6b03e0` | Logical elements per 4-byte slot (table-driven) |
| `TransferSizeUtil::GetSubtileForPacking` | `0x1d6b1f20` | Sub-byte subtile |
| `TransferSizeUtil::GetSubtileForBreakingMinorDimensionForPacking` | `0x1d6b1fe0` (called in `0x1d6b11c0`) | Subtile when minor extent == 1 |
| `TransferSizeUtil::DoesShapeRequireMultiChunkPacking` | `0x1d6b0720` | Multi-chunk packing predicate |
| `Target::LaneCount` / `SublaneCount` | `0x1d60f400` / `0x1d60f300` | Tile dims from chip descriptor |

---

## 4. Byte Sizes — ShapeSizeBytesRaw and Friends

### Purpose

The allocator must know how many HBM bytes a device buffer occupies. Three size functions exist, all driven off the *padded device shape*, and a reimplementer must pick the right one: `ShapeSizeBytesRaw` (the padded data bytes), `ShapeSizeCompact` (the same with compact tiling), and `ShapeWithMetadataSizeBytes` (data + per-buffer metadata, used by the transfer manager). They are surfaced to the C ABI as `HardwareLayout::ShapeSize` (`0xeab0ec0`), `HardwareLayout::ShapeSizeCompact` (`0xeab0f20`), and `HardwareLayout::ShapeSizeCompactRaw` (`0xeab0f80`).

### Algorithm — ShapeSizeBytesRaw

`ShapeSizeBytesRaw` (`0x1d6add40`) is the dispatcher over element-type families. The branch order is the reimplementation contract.

```c
function ShapeSizeBytesRaw(topology, shape):                      // 0x1d6add40
    et = shape.element_type()

    if et == TUPLE (13):                                          // tuple = array of pointers
        n = ShapeUtil::TupleElementCount(shape)
        return round_up(4 * n, ptr_granule)                       // 4 B / element, topology->[1]+200

    if et == TOKEN (17) or IsZeroElementArray(shape) or MaxElementsInPerSplit == 0:
        return 0

    if ElementHasBitWidth(shape, 64):                             // s64/u64/f64/c64-half
        return 2 * ShapeSizeBytesRaw(topology, ComponentShape<64>(shape))   // hi/lo split
    if ElementHasBitWidth(shape, 128):                            // c128
        return 2 * ShapeSizeBytesRaw(topology, ComponentShape<128>(shape))

    if ColorToMemorySpace(layout.memory_space) == kSparseCoreSequencerSflag (12):
        return ExtentProduct(shape) * ByteSizeOfPrimitiveType(et)  // SparseCore: untiled, dense

    if shape.layout().tiles().size() >= 2:                        // already tiled
        return ShapeUtil::ArraySize(shape)

    // untiled 4-byte / packed leaf: pad it, then measure
    tmp = thread_local Shape
    CHECK(SetPaddedShape(topology, shape, &tmp) is OK)            // line 264
    max_elems = LayoutUtil::MaxElementsInPerSplit(tmp, shape)
    if layout.element_size_in_bits == 0:
        return max_elems * ByteSizeOfPrimitiveType(et)
    else:
        bits = element_size_in_bits * max_elems                   // packed: bit-accurate
        return bits/8 + (rounding correction)                     // ceil to byte
```

> **Note —** the literal `12` that `ShapeSizeBytesRaw` (`0x1d6add40`) tests on the untiled-dense branch is `sparse_core_sequencer_sflag` in the canonical LLO `MemorySpace` enum — *not* `sparse_core_sequencer_smem`, which is `14` (byte-confirmed by `MakeSparseCoreSequencerSmemConstant` @ `0x1d60bc60` = `mov $0xe,%esi`). `ColorToMemorySpace` (`0x1d6ffb00`) is a `byte_B5435CA[color]` remap gated on `color < 0xA`, so its output is already a canonical `MemorySpace` enum value, not a raw layout color. See [memory-space-table.md](../appendix/memory-space-table.md) for the 17-value owner table.

> **GOTCHA —** the 64- and 128-bit element split is **not** "8 bytes per element". A 64-bit buffer is decomposed by `ComponentShape<64>` into two 32-bit *component* buffers (high and low words), each tiled independently as a 4-byte buffer, and the sizes summed (`2 * ...`). So an `[N]` f64 buffer is physically two `[N]` f32 tiled buffers, not one `[N]` 8-byte tiled buffer. A reimplementation that treats f64 as an 8-byte element and tiles it directly will mis-size and mis-address every double-precision buffer. The same split governs how DMA reads the buffer.

> **NOTE —** the `element_size_in_bits != 0` branch is the packed-buffer size: it multiplies by the bit width and divides by 8, with a ceiling correction. This is how a bit-packed PRED buffer (1 bit/element) reports its byte size — the `0` value means "use the dtype's natural byte size", the non-zero value means "this many bits per element, round the total up to bytes".

### Function Map

| Function | Address | Role |
|---|---|---|
| `TransferSizeUtil::ShapeSizeBytesRaw` | `0x1d6add40` | Padded data bytes (family dispatch) |
| `TransferSizeUtil::ShapeSizeCompact` | `0x1d6ae8a0` | Compact-tiled byte size |
| `TransferSizeUtil::ShapeSizeCompactRaw` | `0x1d6aea60` | Compact bytes w/o metadata |
| `TransferSizeUtil::ShapeWithMetadataSizeBytes` | `0x1d6aea00` (via `0xeab0ec0`) | Data + per-buffer metadata |
| `HardwareLayout::ShapeSize` / `ShapeSizeCompact` | `0xeab0ec0` / `0xeab0f20` | C-ABI byte-size wrappers |
| `HardwareLayout::ComponentShape<64>` / `<128>` | `0x1d6d9cc0` / `0x1d6d9e40` | 64/128-bit element split |
| `xla::ShapeUtil::ArraySize` | (OSS) | Tiled-array byte size |

---

## 5. Linearization and the HBM Repacker

### Purpose

Two operations turn the tiled device shape into actual byte movement: linearization (`LinearIndex`, the tiled multi-index → flat offset) and the repack kernel (`RepackToHardwareLayout`, the SIMD routine that physically shuffles host bytes into tiled hardware order). A reimplementer needs both: the index math to address a single element, and the repack to stage a whole buffer for DMA.

### Algorithm — LinearIndex

```c
function LinearIndex(topology, shape, multi_index):              // 0x1d6b0600
    CHECK(!ElementHasBitWidth(shape, 64))                        // 64-bit handled by component split, line 646
    if shape.layout().tiles().size() > 1:                        // tiled
        return LayoutUtil::LinearIndex(shape, multi_index)       // OSS tiled index math
    // untiled: stamp a default device layout first, then linearize
    tmp = shape
    ForEachMutableSubshape(tmp, UpdateLayout)                    // 0x1d6b05a0 - fill linear layout
    return LayoutUtil::LinearIndex(tmp, multi_index)
```

> **QUIRK —** `LinearIndex` refuses 64-bit element widths outright (`CHECK(!ElementHasBitWidth(shape, 64))`, line 646). Addressing into an f64/s64 buffer is done on its 32-bit *component* shapes, never on the 64-bit logical shape — consistent with the `ShapeSizeBytesRaw` split. The branch on `tiles().size() > 1` is the same tiled/linear test as everywhere else: a populated tile list routes to the OSS tiled `LinearIndex`; an empty one stamps a default layout first.

### The repack kernel

`RepackToHardwareLayout<SUBLANE, 128>` (instantiated `<2,128>` @ `0x1d5c2880`, `<16,128>` @ `0x1d5c3b40`, `<32,128>` @ `0x1d5c3f80`) is the hand-written AVX routine that shuffles a contiguous host buffer into hardware tile order before DMA. It processes the buffer in **2048-byte (`0x800`) tiles** (`size >> 11` whole tiles + `size & 0x7FF` remainder):

```c
function RepackToHardwareLayout<S,128>(dst, size_bytes, src, pad_flag):  // e.g. 0x1d5c3b40
    whole_tiles = size_bytes >> 11          // 2048-byte tiles
    rem         = size_bytes & 0x7FF
    for t in [0 .. whole_tiles):
        // AVX interleave: vpunpcklbw/vpunpckhbw/vpunpcklwd transpose
        // 4-bit/2-bit field packs via vpsllw + vpand with masks
        //   dword_84A2F00 / dword_84A2580 / byte_84A2D4C / dword_84A2A98
        transpose_tile(dst + t*512, src + t*2048)
    if rem:
        copy rem bytes into a scratch tile
        if pad_flag: memset(scratch + rem, 0xFF, 2048 - rem)   // FILL TRAILING PAD WITH 0xFF
        transpose_tile(dst_tail, scratch)
```

> **NOTE —** the `pad_flag` argument is the on-device tile-padding made physical: when set, the trailing partial tile is filled with `0xFF` (`memset(..., 255, 2048 - rem)`) before the SIMD transpose. The `0xFF` fill is the byte pattern that lands in the padding lanes/sublanes of a buffer whose logical extent did not fill its last tile. A reimplementation that zero-fills instead will produce byte-different (though numerically equivalent for most reductions) HBM contents, and any checksum/SDC path that hashes raw HBM will disagree. The fixed `2048` tile (= `512 B` packed output × the 4-way unpack) is generation-independent across the `<2/16/32,128>` instantiations.

### Function Map

| Function | Address | Role |
|---|---|---|
| `TransferSizeUtil::LinearIndex` | `0x1d6b0600` | Tiled/linear multi-index → flat offset |
| `TransferSizeUtil::UpdateLayout` | `0x1d6b05a0` | Stamp default device layout (per-subshape) |
| `TransferSizeUtil::UpdateLeafLayout` | `0x1d6b08c0` | Stamp tile on a single leaf |
| `RepackToHardwareLayout<2,128>` | `0x1d5c2880` | SIMD host→tile repack, sublane 2 |
| `RepackToHardwareLayout<16,128>` | `0x1d5c3b40` | SIMD host→tile repack, sublane 16 |
| `RepackToHardwareLayout<32,128>` | `0x1d5c3f80` | SIMD host→tile repack, sublane 32 |

---

## 6. The Device-Buffer Residency Record

### Purpose

A logical buffer can be a tuple of many leaves, and each leaf lives at its own HBM address. The runtime tracks "which device addresses hold this buffer's leaves, and what is the on-device shape" in an `xla::ShapedBuffer`, mirrored across the C ABI as `XLA_ShapedBuffer`. This is the residency record the transfer manager, the executable, and PJRT pass around; the *allocator* hands out the addresses ([hbm-allocator.md](hbm-allocator.md)), and this record remembers them.

### The XLA_ShapedBuffer record

Recovered from `ApiConverter::ToC(const xla::ShapedBuffer&, XLA_ShapedBuffer*)` (`0xfcb8580`):

```text
xla::ShapedBuffer  (the on-device residency record)
  +336  on_device_shape   (xla::Shape; the PADDED device shape from §2)
  +656  device_ordinal    (int)
  +664  buffers           (ShapeTree<DeviceAddressBase>; one leaf per tuple element)

XLA_ShapedBuffer  (C-ABI mirror)
  +0..   on_device_shape  (XLA_Shape, via ApiConverter::ToC)
  +536   device_ordinal   (int)
  +544   addresses[]      (SE_DeviceAddressBase array, 24 B stride)   count @ +552
```

Each leaf is a `SE_DeviceAddressBase` (the `stream_executor::DeviceAddressBase` mirror), a 48-byte record in the source vector copied down to a 24-byte C-ABI entry of `{opaque_ptr, size, payload}`. The `(opaque_ptr, size)` pair is the HBM `(offset, byte-size)` the allocator returned; `size` is exactly the `ShapeSizeBytesRaw` of that leaf's padded device shape.

> **NOTE —** the record stores the *on-device* (padded, tiled) shape, not the host shape. Two leaves with the same logical host shape but different `memory_space` colors are distinct device shapes and occupy different tiers. The single `device_ordinal` pins the whole buffer to one chip; a buffer never straddles devices at this layer (cross-device is sharding, above this). The `MaybeOwningDeviceMemory` / `ScopedShapedBuffer` variants (seen in `XlaComputationLaunchContext::PopulateOutputs` @ `0xeadbb80`) wrap the same record with an ownership flag deciding whether dropping the record frees the HBM — see [buffer-donation-aliasing.md](buffer-donation-aliasing.md).

### The driver-side buffer handle

Below the XLA `ShapedBuffer`, the driver moves bytes through `MaybeOwningDmaBuffer` (seen in `JfHbmWriteQueue` and `TpuPxcDriver::WriteToMemoryHelper`), a `(ptr, size, optional<SyncFlag>)` tuple. The `optional<SyncFlag>` is the completion handshake; the DMA into the buffer signals it on completion. This is the wire end of the residency record: the `ShapedBuffer`'s per-leaf address becomes a `MaybeOwningDmaBuffer` at DMA-issue time. The SyncFlag protocol is [sflag-protocol.md](sflag-protocol.md); the DMA alignment floor that constrains the leaf offset is [hbm-dma-alignment.md](hbm-dma-alignment.md).

### Function Map

| Function | Address | Role |
|---|---|---|
| `ApiConverter::ToC(const xla::ShapedBuffer&, XLA_ShapedBuffer*)` | `0xfcb8580` | Residency record → C ABI |
| `ApiConverter::FromC(XLA_ShapedBuffer*)` | `0xfcb7000` | C ABI → `xla::ShapedBuffer` |
| `ApiConverter::ToC(const DeviceAddressBase&)` | `0xfcb78c0` | Per-leaf `(ptr,size)` → `SE_DeviceAddressBase` |
| `ApiConverter::ToC(const xla::ShapeIndex&)` | `0xfcb82e0` | Leaf index within the tuple tree |
| `XlaComputationLaunchContext::PopulateOutputs` | `0xeadbb80` | Build output `ScopedShapedBuffer`s post-execute |
| `TpuTransferManager::CanShapedBufferBeAccessedNow` | `0xeaba6e0` | Residency-readiness check |
| `TpuTransferManager::ReadDynamicShapes` | `0xe9735a0` | Read back dynamic dims into the shape |

---

## Related Components

| Component | Relationship |
|---|---|
| `xla::jellyfish::TransferSizeUtil` | Owns the whole shape→device-shape mapping, tile padding, and byte sizing on this page |
| `xla::jellyfish::HardwareLayout` (`PopulateShape` / `ComponentShape`) | Stamps the tile + `memory_space` and splits 64/128-bit elements |
| `TpuTransferManager` (`0xeab0…` family) | The C-ABI surface that calls `HostShapeToDeviceShape` and the size functions |
| `ApiConverter` (`0xfcb7…` family) | Marshals `Shape`/`Layout`/`ShapedBuffer` across the PJRT C ABI |
| `RepackToHardwareLayout<S,128>` | The SIMD kernel that physically realises the tiling for DMA |

## Cross-References

- [layout-assignment.md](../compiler/layout-assignment.md) — the compile-time pass that *chooses* `minor_to_major` and stamps the `(SublaneCount, LaneCount)` tile this page consumes
- [overview.md](overview.md) — the six-region memory taxonomy and the `memory_space` enum that colors each leaf's tier
- [hbm-allocator.md](hbm-allocator.md) — the `BestFitAllocator` that hands out the HBM offset stored in the residency record's `DeviceAddressBase`
- [hbm-dma-alignment.md](hbm-dma-alignment.md) — the 1024-B DMA floor vs. 16-KiB compile-time alignment that constrains each leaf's offset
- [vmem-allocator.md](vmem-allocator.md) — the on-chip `kAlternate` tier whose word size feeds the per-tier tile/granule
- [buffer-donation-aliasing.md](buffer-donation-aliasing.md) — `MaybeOwningDeviceMemory` ownership and input/output aliasing on the residency record
- [sflag-protocol.md](sflag-protocol.md) — the `optional<SyncFlag>` completion handshake on the driver-side `MaybeOwningDmaBuffer`
- [../compiler/tpu-program-serialization.md](../compiler/tpu-program-serialization.md) — how the `ComputationLayout` (input/output device shapes) is serialized into the executable
- [../pjrt/buffer-and-memory.md](../pjrt/buffer-and-memory.md) — the `PJRT_Buffer` ABI and external refcounting above this layer
- [back to index](../index.md) — Part X — On-Chip Memory & DMA
