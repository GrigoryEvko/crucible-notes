# WindowDescription Byte-Cost

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). The binary is **not** stripped — every symbol below is a demangled C++ name. Other versions differ. All addresses are virtual addresses; for this binary `.text` VMA == file offset (`0xe63c000`) and `.rodata` VMA == file offset (`0x84a0000`). `.data.rel.ro` VMA − `0x200000` == file offset.*

## Abstract

`xla::jellyfish::windowing_util::WindowDescription` is the cost model's **window object**: the per-axis dimensions of a convolution or pooling window — its chunk counts, strides, dilation, and padding — packaged so the memory-transfer pricer can turn the window into a byte count, and the byte count into HBM/VMEM bandwidth cycles. A reimplementer should picture it as the cost-model analogue of XLA's `Window` proto, but flattened into one heap object whose six `absl::inlined_vector<long,6>` dimension arrays each hold one `long` per logical axis. Every TensorCore operand DMA, every conv kernel transfer, and every reduce-window tile is priced by building a `WindowDescription` from the operand `Shape` plus the op's `Window`, computing the transferred bytes with `windowing_util::Size`, and depositing `bytes / bytes_per_cycle` (corrected by a DMA-fragmentation ratio) into the bandwidth lane of the [`ResourceVector`](resource-enum.md).

The familiar reference frame is LLVM's `TargetTransformInfo::getMemoryOpCost`: a load/store is priced by its access size divided by the unit's bytes-per-cycle, with a penalty for misalignment or scatter. `WindowCycles` is the same idea, specialised to a strided multi-dimensional DMA. The "access size" is `ElementSize · align_up(Product(window_strides), ChunkGranules)`, divided by an element-packing factor (bf16 packs 1:1; int8/PRED pack tighter) and a 2nd-minor-tile compaction ratio. The "scatter penalty" is a **DMA-fragment ratio**: the transfer is decomposed into contiguous descriptor runs, and a run count in `{1, 2-3, 4-7, 8-31, >31}` selects a multiplier in `{1.0, 1.6/1.3, 1.1, 1.05, 1.0}` — the `>31` bucket falls back to `1.0` (no penalty), as does a single-DMA-level transfer. The startup latency (`DefaultHbmInitLatency`) is `InitialDmaLatencyInNs · TC_GHz`, deposited once into a separate latency lane.

This page documents the byte-cost model end to end: the `WindowDescription` field layout (recovered byte-exact from the copy constructor and both builders), the `windowing_util::Size` byte primitive, the `WindowCyclesGenericTargetAgnostic` fragment-aware byte→cycle core, the `WindowCycles` bandwidth deposit with its newer-generation (`Target+0x398 >= 5`) two-direction blend, and the `RecordMemXferCyclesImpl` wiring that routes the latency and bandwidth into the resource vector. The matmul/matpush *throughput* integers this cost path multiplies in (the `VfCycleTable` CT values) are framed in [`vf-cycletable`](vf-cycletable.md); the conv-deposit arithmetic that consumes them is in [`convolution-cost-state`](convolution-cost-state.md) and [`reduce-window-pooling-cost`](reduce-window-pooling-cost.md).

For reimplementation, the contract is:

- The `WindowDescription` object layout: the six `inlined_vector<long,6>` dimension arrays (window-sizes `+0x70`, strides `+0xf8`, base-dilation `+0x130`, dilation `+0x168`, padding-low `+0x268`, padding-high `+0x2a0`), the operand-shape pointer `+0x20`, `ChunkBytes` `+0x2d8`, and `element_type` `+0x320`.
- The two builders: the from-`LloValue` `MakeWindowDescription` (chunk-count window) and the cost-model `MakeWindowDescription` (from a `Window` proto), and the rank-equality CHECKs they enforce.
- The byte primitive `Size = ElementSize · align_up(Product(span), granule)`, and the operand-shape corrections (`ElementPackingFactor`, `Compact2ndMinorRatio`) that divide it down to transferred bytes.
- The byte→cycle core: the DMA-fragment count (a product over contiguous stride runs), its `{1.0, 1.6, 1.3, 1.1, 1.05}` ratio table, the F16-class element divisor, and `transferred_bytes / bytes_per_cycle`.
- The `RecordMemXferCyclesImpl` routing: `DefaultHbmInitLatency` → R9/R11 latency lane, `WindowCycles` → R10/R12 bandwidth lane.

| | |
|---|---|
| **Struct** | `xla::jellyfish::windowing_util::WindowDescription` — heap object, fields to `+0x350` |
| **Copy ctor (layout source)** | `WindowDescription::WindowDescription(const&)` `@0xfaa9da0` |
| **Builder (from LloValue)** | `windowing_util::MakeWindowDescription(Target, LloValue*, Shape)` `@0x1c86c1c0` |
| **Builder (from Window)** | `cost_model_util::…::MakeWindowDescription(CycleTable, Window, Shape)` `@0x138456c0` |
| **Byte primitive** | `windowing_util::Size(Span<long>, MemUnit, long granule)` `@0x1c86f320` |
| **Byte→cycle core** | `fusion_util::WindowCyclesGenericTargetAgnostic` `@0x14552180` |
| **Bandwidth deposit** | `fusion_util::WindowCycles` `@0x14552660` |
| **Startup latency** | `fusion_util::DefaultHbmInitLatency` `@0x14552ca0` |
| **Consumer** | `cost_model_util::…::RecordMemXferCyclesImpl` `@0x13844e80` |
| **Dim-array element type** | `absl::inlined_vector<long,6>` (inline ≤6 longs, else heap) |

---

## The WindowDescription Object

### Purpose

A `WindowDescription` is the cost model's frozen snapshot of one windowed access: enough of the operand `Shape` and the op's `Window` to compute transferred bytes and to count how the transfer fragments into DMA descriptors. It is built fresh for each priced operand, read once by the byte/cycle path, and discarded. It is distinct from the SparseCore `windowing_util::WindowDescription` (a different, smaller type near `0x132113xx`); only the cost-model type — the one the copy ctor `@0xfaa9da0` constructs — is documented here.

### Structure

The layout below is recovered byte-exact from three independent sources that must agree: the copy ctor `@0xfaa9da0` (which `vmovups`/`InitFrom`-copies every field at a fixed offset), the from-`LloValue` builder `@0x1c86c1c0` (which writes each field), and the byte/cycle readers `@0x14552180`/`@0x13844e80` (which read them). Each dimension array is an `absl::inlined_vector<long,6>`: the first qword is a metadata word (bit 0 = is-heap-allocated; otherwise `size << 1`); if inline, the up-to-six `long`s start at `+0x8`; if heap, `+0x8` is the data pointer and `+0x10` the capacity. This Storage layout is confirmed by `inlined_vector<long,6>::Assign` `@0xf913da0`.

| Field | Offset | Type | Meaning (confirmed source) | Confidence |
|---|---|---|---|---|
| valid flag | `+0x00` | byte | "is-default"/valid flag; ctors set 0 | HIGH |
| name string | `+0x08` | `std::string` (libc++ SSO, 0x18 B) | debug/name string; `__init_copy_ctor_external` `@0xfaa9de5` | MEDIUM |
| string-present | `+0x18` | byte | flag: name string populated | MEDIUM |
| **operand pointer** | `+0x20` | `const void*` (`LloValue*` / element-type-bearing ptr) | written from the builder's `LloValue*` arg (`null` on the cost-model path); the byte/cycle readers treat it as an element-type-bearing pointer, reading the type as `WORD[ptr+0xb] >> 2 & 0x1f` (and gating the F16 divisor on `WORD[ptr+0xb] & 0x7c == 0x10`) | HIGH |
| index LloValues | `+0x28` | `inlined_vector<LloValue*,8>` | per-axis window-iteration index variables | HIGH |
| **window sizes** | `+0x70` | `inlined_vector<long,6>` | per-axis chunk count of the window = `ChunkCountsWithTmp(Shape)` | CERTAIN |
| layout order | `+0xa8` | `inlined_vector<long,6>` | the `Shape`'s minor-to-major physical dim order | HIGH |
| second array | `+0xe0` | heap `{ptr, size, cap}` (long) | heap-only long vector; `memcpy`'d by the copy ctor; iterated as the second array by the fragment loop | MEDIUM |
| **strides** | `+0xf8` | `inlined_vector<long,6>` | per-axis stride; `Resize(N, init=1)`. The array `Size`/the fragment loop product over | CERTAIN |
| base dilation | `+0x130` | `inlined_vector<long,6>` | per-axis base/window dilation; `Resize(N, init=1)` | HIGH |
| **dilation** | `+0x168` | `inlined_vector<long,6>` | per-axis dilation; `Resize(N, init=0)`. Fragment-run gate (must be 0) | CERTAIN |
| ElementalStrideInfo | `+0x1a0` | sub-struct | elemental stride descriptor; **read by the LLO window-iteration emitter, not the byte/cycle path** | MEDIUM |
| ESI-present | `+0x260` | byte | flag: `ElementalStrideInfo` constructed | HIGH |
| **padding-low** | `+0x268` | `inlined_vector<long,6>` | per-axis padding-low; `Resize(N, init=0)`. Fragment-run gate (must be 0) | CERTAIN |
| padding-high | `+0x2a0` | `inlined_vector<long,6>` | per-axis padding-high / aux | HIGH |
| **ChunkBytes** | `+0x2d8` | `long` | `= Target::ChunkBytes()` (`= topology[+0x1a8]·4`) | CERTAIN |
| index callback | `+0x310` | `std::function<…>` (16 B) | per-axis index-computation callback (`+0x310` fn-ptr, `+0x318` policy) | MEDIUM |
| **element_type** | `+0x320` | `PrimitiveType` (dword) | `= Shape::element_type()` | CERTAIN |
| config flags | `+0x324` | word=1, byte=0 | default config (`1`/`0`) | LOW |
| config scalars | `+0x328` | 3×long (`1`,`0`,`1`) | transfer-granularity defaults | LOW |
| offset LloValues | `+0x350` | `inlined_vector<LloValue*,?>` | offset index variables (`ComputeWindowWithIndicesOnly`) | LOW |

> **NOTE —** the decompiler renders these offsets in decimal. The copy ctor's `vmovups [rbx+0x78]`/`[rbx+0x88]` pair (qword index `this+14`) is the **inline payload** of the window-sizes vector at metadata-word `+0x70`; likewise `+0x100`/`+0x110` is the strides payload behind metadata `+0xf8`. When the metadata word's bit 0 is set the array spilled to the heap and the ctor takes the `Storage<long,6>::InitFrom` path instead of the inline `vmovups` copy. A reimplementation must read the metadata word first, never assume the data lives inline.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `windowing_util::WindowDescription::WindowDescription(const&)` | `0xfaa9da0` | copy ctor — the byte-exact layout witness | CERTAIN |
| `windowing_util::MakeWindowDescription(Target, LloValue*, Shape)` | `0x1c86c1c0` | from-LloValue builder (chunk-count window) | CERTAIN |
| `cost_model_util::…::MakeWindowDescription(CycleTable, Window, Shape)` | `0x138456c0` | from-`Window` builder (cost-model path) | CERTAIN |
| `inlined_vector<long,6>::Assign` | `0xf913da0` | Storage metadata layout witness | CERTAIN |
| `Target::ChunkBytes` | — | `+0x2d8` source (`topology[+0x1a8]·4`) | HIGH |
| `Target::ChunkCountsWithTmp` | — | `+0x70` window-sizes source | HIGH |

---

## The Two Builders

### Purpose

A `WindowDescription` is never default-constructed for pricing; it is built by one of two `MakeWindowDescription` overloads depending on whether the caller has an `LloValue` (LLO lowering path) or a `Window` proto (cost-model path). Both produce the same layout; they differ only in where the per-axis dimension arrays come from.

### From an LloValue — `@0x1c86c1c0`

The from-`LloValue` builder fills the window-sizes array with chunk counts and defaults every other dimension array:

```c
function MakeWindowDescription(Target, LloValue* shape_val, Shape):   // @0x1c86c1c0
    wd.window_sizes  = Target.ChunkCountsWithTmp(Shape)     // +0x70 = per-axis chunk count
    wd.strides       = Resize(rank, init = 1)               // +0xf8 = all-ones
    wd.base_dilation = Resize(rank, init = 1)               // +0x130 = all-ones
    wd.dilation      = Resize(rank, init = 0)               // +0x168 = all-zero
    wd.padding_low   = Resize(rank, init = 0)               // +0x268 = all-zero
    wd.layout_order  = Assign(Shape.layout.minor_to_major)  // +0xa8
    wd.element_type  = Shape.element_type()                 // +0x320 (DWORD this+200)
    wd.chunk_bytes   = Target.ChunkBytes()                  // +0x2d8 (qword this+91)
```

The all-ones strides and all-zero dilation/padding are the "dense, unfragmented" default: a window built this way produces exactly one DMA descriptor (fragment count 1, ratio 1.0) unless a later pass overwrites the strides.

### From a Window proto — `@0x138456c0`

The cost-model builder first delegates to the from-`LloValue` builder to lay down the defaults, then overwrites the window-sizes and strides arrays from the `Window`'s dimension arrays:

```c
function MakeWindowDescription(CycleTable, Window, Shape):   // @0x138456c0
    MakeWindowDescription(Target, /*shape_val=*/null, Shape)         // @0x1c86c1c0 — defaults
    CHECK(window.base_bounds.size()   == shape.dimensions().size())  // cost_model_util.cc:287
    CHECK(window.window_bounds.size() == shape.dimensions().size())  // cost_model_util.cc:288
    tag      = BYTE[Shape + 0x138]                                   // shape kind ∈ {3=array, 5=tuple/boxed}
    dim_vec  = (tag == 3) ? &Shape.dimensions : *(Shape + 8) + 8     // shape rank source
    rank     = dim_vec.size                                          // shape.dimensions().size()
    wd.window_sizes = Assign(window.window_bounds, rank)   // +0x70  (Storage this+14)
    wd.strides      = Assign(window.base_bounds,   rank)   // +0xf8  (Storage this+31)
```

> **GOTCHA —** the rank used by the two `Assign`s and by both CHECKs is read from the `Shape`, gated by a shape-kind tag byte at `Shape+0x138` (the same `BYTE[a4+312]` the from-`LloValue` builder reads): when it is `3` the dimensions vector is inline in the `Shape`; when it is `5` the `Shape` is a boxed array and the rank source is chased through `*(Shape+8)+8`. (A mismatched tag aborts with `CHECK(buffer != nullptr)` from `shape.h:843`.) A reimplementation that hardcodes the inline read will dereference garbage on a boxed shape. The two CHECK strings (`"window.base_bounds.size() == shape.dimensions().size()"`, `"window.window_bounds.size() == shape.dimensions().size()"`) are the rank-equality guards — the window must have exactly one entry per shape dimension.

---

## The Byte Primitive — windowing_util::Size

### Purpose

`Size` is the lowest layer: it turns a span of per-axis counts (the window strides) into a byte count, rounded up to the transfer granule. It is the cost model's `align_up(elements, granule) · element_size`.

### Algorithm

```c
function Size(span, MemUnit memunit, long granule):   // @0x1c86f320
    cnt     = xla::Product(span)            // @0x20cf5200 — Π span[i]; empty span → 1
    rounded = ceil_div(cnt, granule)        // q = cnt/granule; if cnt > q·granule: q += 1
    bytes   = ElementSizeBytes(memunit) · granule · rounded
            = ElementSizeBytes · align_up(cnt, granule)
    return MemUnit{ byte_count = bytes, mem_tag = memunit }
```

The `Product` is over the per-axis stride array (not the window-size array): each stride entry is the number of granule-sized chunks that axis sweeps, and their product is the total chunk count of the transfer. The ceil-div-then-multiply is the `align_up`: a transfer that does not fill an integer number of granules is billed for the next whole granule. The element-size factor (`granule · rounded` is in granules; multiplied by `ElementSizeBytes`) converts to bytes. The result is a `{byte_count, mem_tag}` pair so the caller knows which memory space (HBM vs VMEM) the bytes belong to.

The granule comes from `Target::ChunkGranules` `@0x1d61a440` = `(topology.ChunkCellCount · 4) / vtable[+0x5c0]()` — the chunk-cell count scaled to bytes, divided by a per-gen element-per-chunk factor.

---

## Bytes to Transferred Bytes — the operand corrections

`Size` gives the raw window byte count; `RecordMemXferCyclesImpl` `@0x13844e80` then divides it by two operand-shape corrections before handing it to the cycle core:

```c
function bytes_to_transfer_bytes(raw_bytes, Target, shape):   // inside @0x13844e80
    granule        = Target.ChunkGranules(MemUnit)                          // @0x1d61a440
    raw_bytes      = windowing_util::Size(wd.strides, MemUnit, granule)     // r14 = byte_count
    pred_as_1bit   = TransferSizeUtil.ShouldPackPREDAsSingleBit(shape)
    packing_factor = TransferSizeUtil.ElementPackingFactor(elem_type, pred_as_1bit)  // bf16=1
    compaction     = Target.Compact2ndMinorRatio(shape)                     // 2nd-minor tiling
    divisor        = compaction · packing_factor                            // v99
    transfer_bytes = raw_bytes / divisor                                    // v37
```

`ElementPackingFactor` is 1 for bf16/fp32 (one byte-stream element per logical element) and larger for sub-byte packed formats (int8/int4, and PRED packed to single bits when `ShouldPackPREDAsSingleBit` holds). `Compact2ndMinorRatio` accounts for the 2nd-minor-dimension tiling compaction the layout applies. Dividing by both yields the actual number of bytes that cross the bus.

> **QUIRK —** the cost model bills strides, not window sizes, for the byte count. `Size` products over the `+0xf8` strides array, *not* the `+0x70` window-sizes array. The window-sizes array drives the fragment count (below) and the LLO iteration; the strides array drives the byte volume. A reimplementation that products over window-sizes will get the wrong byte count for any non-unit-stride window.

---

## The Byte→Cycle Core — WindowCyclesGenericTargetAgnostic

### Purpose

This function converts a byte count into a fragment-corrected cycle value: it counts how many contiguous DMA descriptor runs the window decomposes into, looks up an efficiency multiplier for that fragment count, applies an element-type divisor, and returns `chunk_count · ratio / divisor + residual`. It is "target-agnostic" because it takes the byte count as a `double` argument rather than reading bandwidth from the `Target`.

### Algorithm

```c
function WindowCyclesGenericTargetAgnostic(MemUnit, wd, count_descriptors, bytes):  // @0x14552180
    // 1. Element-type divisor. The element-type-bearing pointer is at wd+0x20.
    elem_ptr = *(wd + 0x20)
    if elem_ptr != null and (WORD[elem_ptr + 0xb] & 0x7c) == 0x10:   // F16-class element type
        div = 2003.0                          // @0xa2df1b0 — fixed F16 unpack penalty
    else:
        div = (bytes != 0) ? bytes : 1.0      // @0xa2df230 — default, no extra penalty

    ComputeDmaLevels(&dma_levels, wd)         // @0x1c86a9e0 — populates the descriptor levels

    // 2. DMA-fragment count: product over contiguous stride runs.
    frag = 1
    for i in reverse(rank):                                // loop @0x14552250
        if wd.strides[i] == wd.window_sizes[i]             // stride matches window span
           and wd.dilation[i]    == 0                      // wd+0x168
           and wd.padding_low[i] == 0:                     // wd+0x268
            frag *= wd.strides[i]                          // run continues — contiguous
        else:
            frag *= wd.strides[i]; break                   // run broken — descriptor boundary

    // 3. Fragment-efficiency ratio by descriptor count.
    if dma_levels <= 1:  ratio = 1.0                       // single descriptor, no penalty
    elif frag > 31:      ratio = 1.0                       // (very large — falls through)
    elif frag <= 3:      ratio = table[frag >= 2]          // @0xa2d71a0 = {1.6, 1.3}
    elif frag <= 7:      ratio = 1.1                       // @0xa2df4c8
    else:                ratio = 1.05                      // @0xa2df2a8  (frag in 8..31)

    // 4. Return (NOT yet divided by bytes_per_cycle — that is WindowCycles' job).
    //    div is `bytes` itself on the non-F16 path, so the ratio term is normalised by
    //    the byte count; the trailing addend is a carried-in count term (UNVERIFIED label).
    return contiguous_chunk_count · ratio / div + addend
```

The fragment count is the cost model's scatter estimate: a window whose stride equals its span with no dilation and no padding is one contiguous run (`frag` small, ratio near 1.0); a window that strides past gaps, dilates, or pads fragments into many descriptors, and the ratio climbs. The VLOG-6 trace inside this function names the intermediates — `"stride_levels.size"`, `"contiguous_chunk_count"`, `"access multiplier"` — confirming `frag` is the contiguous-chunk count and `ratio` the access multiplier.

> **GOTCHA —** the fragment ratio is keyed by two near-synonyms that are *not* the same. The early-out `dma_levels <= 1` (from `ComputeDmaLevels`) returns ratio 1.0; the table lookup is keyed by `frag` (the contiguous-chunk product). They usually agree but the table is only consulted when there is more than one DMA level. A reimplementation that keys the `{1.6, 1.3, 1.1, 1.05}` table off the wrong counter will misprice every multi-level transfer. The `{1.6, 1.3}` pair at `@0xa2d71a0` is selected by `frag >= 2` (a 2-element table indexed by the boolean), so a 2-fragment transfer takes `1.3` and a 1-fragment-but-multi-level transfer takes `1.6`.

### The element-type divisor

The F16-class test masks the element-type bits of the `wd+0x20` pointer (`WORD[ptr+0xb] & 0x7c == 0x10`) and, when set, replaces the divisor with a fixed `2003.0` (`@0xa2df1b0`) instead of the incoming `bytes_per_cycle`. On every other (non-F16) type the divisor stays the `bytes_per_cycle` argument the caller passed (`vblendvpd` keeps `xmm0` when it is non-zero, falling back to `1.0` at `@0xa2df230` only when `bytes_per_cycle == 0`). So F16 is the one element type whose access cost is *not* normalised by the per-cycle byte budget but by the constant `2003.0`. The full `PrimitiveType → divisor` enumeration beyond the F16-class cell was not swept — only the `2003.0` case is pinned (LOW confidence that no other type takes a special cell).

---

## The Bandwidth Deposit — WindowCycles

### Purpose

`WindowCycles` is the public entry: it adds the startup latency, calls the target-agnostic core, divides by `bytes_per_cycle`, and — when the `Target` generation field `+0x398` is `>= 5` — blends the two transfer directions (HBM and VMEM) by TC frequency. Its return is the bandwidth-cycle value deposited into the R10/R12 lane.

### Algorithm

```c
function WindowCycles(MemUnit, wd, Target, bytes_per_cycle, a, b, c):  // @0x14552660
    if MemUnit == Target.MemUnitFromKiB(0): return 0.0      // sentinel — no transfer

    // startup-latency term
    if c (7th arg) != -1:
        init = Target.TensorCoreFrequencyInMegaHertz() / 1000.0 · c   // freq-scaled startup
    else:
        init = DefaultHbmInitLatency(MemUnit, wd, Target)             // @0x14552ca0

    count_desc = (Target.vtable[+0x590]() == 1)             // per-gen count-descriptors predicate
    base = WindowCyclesGenericTargetAgnostic(MemUnit, wd, count_desc, bytes_per_cycle) + init

    if Target[+0x398] >= 5:                                 // v5p+ (a4[230] >= 5)
        // two-direction TC-freq blend: read the {HBM,VMEM} TC-freq pair, divide it by
        // 1000.0 (the xmmword pair @0xa2ce650), multiply the per-direction byte rates by it,
        // take the per-direction MAX, then add the bandwidth + init terms
        return max(dir0_cycles, dir1_cycles) + (hbm_term + init)
    else:                                                   // < v5p
        return max(dir0_cycles, dir1_cycles) + (hbm_term + init)   // simple add path
```

The core divide is the `vdivsd` at the tail of `WindowCyclesGenericTargetAgnostic`: `WindowCycles` forwards the incoming `bytes_per_cycle` (the `xmm0` it received from `RecordMemXferCyclesImpl`) straight into the core as its `bytes` argument, so on the non-F16 path the core's `div` *is* `bytes_per_cycle`, and the `contiguous_chunk_count · ratio / div` term is exactly `chunks · ratio / bytes_per_cycle`. `bytes_per_cycle` itself is `HbmFullChipBytesPerSecond / (TC_freq_MHz · 1e6) / CoresPerChip` — the per-core, per-cycle byte budget — computed by `GetBytesPerCycle` and cross-checked against the same geometry in [`memory-bandwidth-latency-model`](memory-bandwidth-latency-model.md).

On the newer-generation path (`Target+0x398 >= 5`) the function prices both transfer directions and takes the slower one: it splits the byte volume into an `{HBM, VMEM}` percentage pair, reads the `{HBM, VMEM}` TC-frequency pair and divides *it* by `1000.0` (the `xmmword_A2CE650` MHz→GHz pair), multiplies the per-direction byte rates by that frequency, and takes the per-direction maximum. The VLOG-6 trace names the directions explicitly — `"hbm_percentage"`, `"vmem_percentage"`, `"hbm_bytes"`, `"vmem_bytes"`, `"hbmbw"`, `"vmembw"`, `"hbmlatency"`, `"vmemlatency"`, `"next_gen"` — confirming the newer-generation model is a two-lane (HBM vs VMEM) max, not a single transfer.

### Startup latency — DefaultHbmInitLatency

```c
function DefaultHbmInitLatency(MemUnit, wd, Target):   // @0x14552ca0
    elem      = wd.element_type via wd+0x20             // reads operand shape element type
    init_ns   = Target.vtable[+0x20].InitialDmaLatencyInNs(MemUnit, window_bytes)
    return init_ns · (TC_freq_MHz / 1000.0)             // ns · GHz = cycles
```

This is the fixed cost of starting a DMA, paid once per transfer regardless of size. `InitialDmaLatencyInNs · TC_GHz` converts nanoseconds to cycles: the `InitialDmaLatencyInNs` virtual call returns a per-generation, per-element-type, per-byte-count nanosecond figure, and `TC_freq_MHz / 1000.0` is the GHz conversion the binary applies (`@0xa2e0430 = 1000.0`). The concrete latency depends entirely on the target generation's `InitialDmaLatencyInNs` table, which was not swept here.

---

## The Consumer — RecordMemXferCyclesImpl

### Purpose

`RecordMemXferCyclesImpl` `@0x13844e80` is where the whole chain runs and where the two cycle values land in the [`ResourceVector`](resource-enum.md). It is called once per priced operand (input or output); the latency goes into one resource lane and the bandwidth into another.

### Algorithm

```c
function RecordMemXferCyclesImpl(label, latency_res, bw_res, hlo, wd, CycleTable, rv, do_check, fn):
    if not is_priced_memory_space(wd.shape):  return       // element-type gate (bittest)

    granule        = Target.ChunkGranules(MemUnit)         // @0x1d61a440
    raw_bytes      = windowing_util::Size(wd.strides, MemUnit, granule)
    transfer_bytes = raw_bytes / (Compact2ndMinorRatio · ElementPackingFactor)

    if rv[latency_res] == 0.0:                             // latency paid once per lane
        latency = DefaultHbmInitLatency(transfer_bytes, MemUnit, wd, Target)
        rv.Acc(latency_res, latency)                       // R9 (input) / R11 (output)

    bpc       = GetBytesPerCycle(hlo, Target)              // @0x1454dd00
    bandwidth = WindowCycles(transfer_bytes, MemUnit, wd, Target, bpc, 0, 0, -1)
    rv.Acc(bw_res, bandwidth)                              // R10 (input) / R12 (output)
```

The latency deposit is guarded by `rv[latency_res] == 0.0`: the DMA startup is billed only the first time a lane is touched, so a sequence of transfers into the same lane pays one startup, not N. The bandwidth deposit always accumulates. `RecordInputMemXferCycles` `@0x13845580` passes `(R9, R10)`; `RecordOutputMemXferCycles` `@0x13845860` passes `(R11, R12)` — the input/output split of [`memory-bandwidth-latency-model`](memory-bandwidth-latency-model.md). The VLOG-1 trace names the deposits: `"window_size.bytes"` (raw `Size` output), `"packing_factor"` (the divisor), `"window_xfer_size.bytes"` (transferred bytes), `"window_xfer_latency_cycles"` (R9/R11), `"window_xfer_bandwidth_cycles"` (R10/R12). There is also a consistency self-check (`"…are inconsistent. estimated_cycles_for_bytes:… error threshold: 0.01"`) comparing the deposited cycles against bytes·rate within 1%.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `RecordMemXferCyclesImpl` | `0x13844e80` | the chain — Size → corrections → latency + bandwidth → ResourceVector | CERTAIN |
| `RecordInputMemXferCycles` | `0x13845580` | input wrapper — R9/R10 lanes | HIGH |
| `RecordOutputMemXferCycles` | `0x13845860` | output wrapper — R11/R12 lanes | HIGH |
| `GetBytesPerCycle` | `0x1454dd00` | per-op bytes-per-cycle budget | HIGH |
| `Target::ChunkGranules` | `0x1d61a440` | `Size` granule = `(ChunkCellCount·4)/vtable[+0x5c0]` | CERTAIN |
| `ComputeDmaLevels` | `0x1c86a9e0` | DMA-level vector feeding the fragment early-out | MEDIUM |

---

## Per-Dimension Iteration Cost

The window's per-axis arrays are what make the cost *per-dimension*. Two distinct per-axis products are taken:

- **Byte volume** — `windowing_util::Size` products the `+0xf8` strides array. Each axis contributes its stride (chunk-sweep count); a wider or longer-strided axis multiplies the byte count, and the whole product is rounded up to one transfer granule. This is the term that scales with the window's spatial extent.
- **Fragment count** — `WindowCyclesGenericTargetAgnostic` products a *prefix* of the strides array: it walks axes from minor to major and multiplies while each axis is contiguous (`stride == window_size && dilation == 0 && padding_low == 0`), breaking the product at the first axis that is dilated, padded, or strided past its span. The break point is the DMA descriptor boundary; the resulting product is the fragment count that keys the efficiency ratio.

A reimplementer should picture the cost of one window axis as: it always multiplies the byte volume (so the transfer is bigger), and it multiplies the fragment count only until the first non-contiguous axis (so dilated/padded windows fragment and lose bandwidth efficiency). A dense 3×3 stride-1 conv window with no dilation/padding stays one contiguous fragment (ratio 1.0); the same window with `dilation=2` breaks the run at the dilated axis, raising the fragment count and the ratio. This is the cost-model encoding of "scattered DMAs are slower than contiguous ones."

> **QUIRK —** dilation and padding never change the byte *volume* in this model — `Size` ignores the `+0x168` dilation and `+0x268` padding arrays entirely; they enter only through the fragment-count gate. A reimplementation that scales the byte count by the dilation factor will double-count: dilation costs bandwidth *efficiency* (a higher ratio), not bandwidth *volume*.

---

## How It Feeds Conv and Reduce-Window

The byte-cost path prices the *operand DMA* half of a windowed op. The compute half — the matmul/matpush pushes for a convolution, or the XLU reductions for a pooling op — is priced separately and added into the same `ResourceVector`. The bridge is the `VfCycleTable` throughput integer `thru(CT)`, which is the per-format MXU reservation cycle for a matmul or matpush push:

```c
// RecordConvKernelCycles (see convolution-cost-state) deposits, per the conv kernel:
R[0] Matpush = matpush_count · thru(CT_matpush)       // bf16 = 2, int8 = 8  (VF array[0])
R[1] Matmul  = op_count · thru(CT0) · 0.5 / derate    // bf16 = 8, int8 = 32 (VF array[15])
R[2] Xlu     = thru(CT 0x1c) · chunks_per_tile · rem  // VfPerf(0x11f, res 0xe) + 1
// and the operand window DMA, priced here:
R[9/10]  input  MemXfer  = DefaultHbmInitLatency + WindowCycles(input window)
R[11/12] output MemXfer  = DefaultHbmInitLatency + WindowCycles(output window)
```

The final op cost is the bundle-MAX over these lanes: a conv that is bandwidth-bound is gated by R10/R12 (the `WindowCycles` deposit this page prices); a conv that is MXU-bound is gated by R0/R1 (the matmul/matpush throughput of [`vf-cycletable`](vf-cycletable.md)). Reduce-window/pooling reuses the identical operand-window pricing and substitutes the XLU reduction CTs (`0x16`, `0x1b`) for the matmul deposits — see [`reduce-window-pooling-cost`](reduce-window-pooling-cost.md). This is why both pages bridge to the same `WindowDescription`: it is the single byte model both the conv DMA and the pooling tile transfer share.

---

## Related Components

| Name | Relationship |
|---|---|
| `vf-cycletable` | the `thru(CT)` matmul/matpush throughput integers the conv compute deposit multiplies |
| `convolution-cost-state` | `RecordConvKernelCycles` — consumes this byte model for the operand DMA + the conv compute |
| `reduce-window-pooling-cost` | reuses this operand-window pricing for pooling tiles |
| `memory-bandwidth-latency-model` | the R9/R10/R11/R12 lane routing and `bytes_per_cycle` geometry |
| `mxu-latency-overview` | the `MxuLatencyTable` reservation arrays the `thru(CT)` integers index |
| `resource-enum` | the `ResourceVector` lanes `WindowCycles`/`DefaultHbmInitLatency` deposit into |

---

## Cross-References

- [VfCycleTable](vf-cycletable.md) — the 32-entry CT throughput table whose matmul/matpush integers price the conv compute half
- [ConvolutionCostState](convolution-cost-state.md) — `RecordConvKernelCycles`: the conv R0/R1/R2 deposits plus this operand window DMA
- [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) — pooling reuses this window byte model with XLU reduction CTs
- [Memory Bandwidth / Latency Model](memory-bandwidth-latency-model.md) — the R9/R10/R11/R12 lane split and `bytes_per_cycle = HBM B/s / (TC_MHz·1e6) / cores`
- [Local DMA Bandwidth](local-dma-bandwidth.md) — the VMEM/local transfer side of the same MemXfer pricer
- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuLatencyTable::GetResourceUsage` arrays the `VfCycleTable` CTs read
- [Resource Enum (23-slot)](resource-enum.md) — the `ResourceVector` the latency and bandwidth deposits accumulate into
- [CycleTable Family](cycletable-family.md) — the per-gen cost-model class family that owns the `VfCycleTable`/`GlcCycleTable`/`GfcCycleTable`
- [MXU Slot](../isa/slot-mxu.md) — the LLO matmul/matpush ops the conv compute deposit prices
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the latch ops behind the matpush throughput CTs
