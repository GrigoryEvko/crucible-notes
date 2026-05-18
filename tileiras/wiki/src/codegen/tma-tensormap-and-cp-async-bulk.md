# TMA + Tensormap + cp.async.bulk Emission

## Abstract

The Tensor Memory Accelerator (TMA) path used by Hopper and Blackwell
targets has three surfaces that must stay consistent: descriptor
construction, descriptor mutation, and instruction emission. They share
one 64-byte payload (the `CUtensorMap` / `DESC_TMA512` descriptor), one
set of operand classes (`l` for 64-bit pointers, `r` for i32 coordinates,
`h` for i16 im2col offsets), and one modifier order: `.im2col`, then
`.multicast::cluster`, then `.L2::cache_hint`.

Device-side in-place mutators can rebind a tiled descriptor without
calling `cuTensorMapEncodeTiled` again, but a cross-proxy handshake
threads through this layer. Device-side writes to a descriptor pass
through the **generic** PTX proxy; `cp.async.bulk.tensor.*` reads of the
same descriptor enter the **tensormap** proxy. Without an explicit
`fence.proxy.tensormap::generic` acquire/release pair or the fused
`nvvm.tensormap.cp_fenceproxy` operation, the two accesses are unordered.
Emitter, mutator, and fence intrinsic are therefore one feature.

## TMA Descriptor Shape

The descriptor payload is a 64-byte record, represented as eight 64-bit
slots. The device-mutator path writes through a 128-byte `.b1024`
operation, so every device-visible descriptor pointer must be 128-byte
aligned. The live 64-byte payload occupies the lower half of that aligned
slot; the upper half is reserved padding.

| Field | Offset | Meaning |
|---|---:|---|
| `tensor_base_ptr` | 0 | Base address of the logical tensor |
| `fmt_dim_stride_packed` | 8 | Format plus packed dimension lanes |
| `box_size_packed` | 16 | Tile box extents and paired-CTA layout bits |
| `elem_stride_packed` | 24 | Packed global-stride lanes |
| `load_mode_packed` | 32 | Tiled / im2col / multicast mode fields |
| `interleave_fill` | 40 | Interleave and out-of-bounds fill behavior |
| `l2_sector_promo` | 48 | L2 sector-promotion policy |
| `reserved_future` | 56 | Reserved payload slot |

Rank lives nowhere as an independent mutable field in the device rebind
path. The operation consuming or mutating the descriptor carries it,
selecting the lane to update inside the packed fields.

### Inner Bit Packing — Limits of Binary Visibility

Three slots in the eight-slot table multiplex multiple logical fields per
64-bit word. The binary observes only the lane-index argument the mutator
templates substitute into PTX text — not the bit-level placement the
hardware ultimately writes. Specifically:

| Slot | Mutator template | Lane width | Lane count | Bit packing |
|---|---|---|---|---|
| `tensor_base_ptr` (slot 0) | `tensormap.replace.tile.global_address.b1024.b64 [$0], $1` | b64 (full slot) | 1 | direct address — no inner packing |
| `fmt_dim_stride_packed` (slot 1) | `tensormap.replace.tile.global_dim.b1024.b32 [$0], {N}, $1` | b32 | `rank` (0..4) | format bits coexist with `rank` 32-bit dim lanes; per-lane bit layout is PTX-ISA-defined and not observable in the emitter |
| `box_size_packed` (slot 2) | none in emitted set — see PTX `tensormap.replace.tile.box_size` | n/a | n/a (host-born only) | hardware-internal |
| `elem_stride_packed` (slot 3) | `tensormap.replace.tile.global_stride.b1024.b64 [$0], {N-1}, $1` | b64 | `rank-1` (0..3) | strides occupy 64-bit lanes; dim-0 stride is implicit element size, never device-written |
| `load_mode_packed` (slot 4) | none | n/a | n/a | mode enum bits, multicast cardinality — set host-side |
| `interleave_fill` (slot 5) | none | n/a | n/a | interleave + OOB fill — set host-side |
| `l2_sector_promo` (slot 6) | none | n/a | n/a | promotion policy — set host-side |
| `reserved_future` (slot 7) | none | n/a | n/a | observed all-zero in seed templates the binary copies |

The three device-side mutators emitted by tileiras (`global_address`,
`global_dim`, `global_stride`) touch slots 0, 1, and 3. Slots 2, 4, 5, 6,
and 7 are immutable on the device path. Anything that would require
writing them — box-shape changes, swizzle, fill-mode, element-type,
interleave, paired-CTA layout — has to round-trip through host-side
`cuTensorMapEncode*` driver entries, which is why im2col descriptors and
SM100 paired-CTA descriptors are host-born only.

> ⚡ **QUIRK — eight-slot logical view, not eight-slot byte layout**
> The "eight 64-bit slots" framing is the *device-mutator-visible* logical view. The PTX `b1024` operand class declares the operand is a 1024-bit aligned region; only the lower 64 bytes are live in current tensormap formats. Lane indices in the mutators are *logical* (dim index, stride index), not raw byte offsets — the hardware translates each lane index to the corresponding bit window inside the relevant packed slot. The exact bit-window mapping is not derivable from the binary; the emitter just hands `{N}` to PTX and the assembler/hardware handles placement.

Confidence: HIGH on slot names and per-slot mutator coverage (direct
evidence: emitted PTX strings at `0x4ce3b40`, `0x4ce3b80`, `0x4ce3bc0` in
the rodata string table; debug-dump format `"DESC_TMA512: 0x%016lx
%016lx %016lx %016lx"` at `0x4603ba8` corroborates the 4-of-8 active
slots). MED on the named "logical roles" for slots 4-7 — derived from
host-side `cuTensorMapEncodeTiled` parameter ordering and the
`SeparateHostTMA` pass's host-encoder call sites, not from device-side
mutators. LOW on inner bit-position claims — the binary does not contain
the bit-packing logic; consult the PTX ISA `tensormap.replace.*` section
for the authoritative byte-level layout.

## Tensormap Init / Update Algorithm

Descriptor birth follows one of two paths.

The **host-born** path is emitted by the `SeparateHostTMA` pass. It
materialises a 64 B stack-aligned blob, calls `cuTensorMapEncodeTiled`
(or `cuTensorMapEncodeIm2col` for im2col-mode operations), then hands the
blob to the kernel-argument attachment step, which appends a kernel
parameter tagged `cute_nvgpu.grid_constant`. The descriptor passes
by-value into the kernel as a `__grid_constant__ CUtensorMap` and never
gets written by the device. This is the only legal path for im2col
descriptors and for SM100 `TWO_CTA` paired-CTA descriptors, because
neither the box-size field nor the im2col-offsets field has a device-side
`tensormap.replace.*` mutator template.

The **device-born** path is the rebind sequence. A zeroed 128 B aligned
slot is allocated in global or `shared::cta`, optionally seeded from a
host descriptor via the fused `tensormap.cp_fenceproxy` op, then patched
in a fixed order — address → dim[0..rank−1] → stride[1..rank−1] — for a
total of `1 + rank + (rank-1) = 2*rank` inline-asm ops per rebind. The
ordering invariant is structural: dim-extent writes re-pack the
`fmt_dim_stride_packed` slot through a hardware-internal bit-interleave
that relies on the format field already being valid (set by
`cuTensorMapEncodeTiled` at birth). Write strides before all dims and a
short window opens where the slot is coherent but the stride lanes are
stale. Per-kernel counters `nv_tileas.num-device-tmas` and
`nv_tileas.num-host-tmas` tally the two populations separately; device-TMA
slots sit before host-TMA slots in the appended block so the kernel can
locate its working buffer at a fixed parameter-list offset.

```c
void rebind_tiled_tma_descriptor(TmaDescriptor *desc, const TmaRebind *rebind) {
    require_aligned(desc, 128);
    require(1 <= rebind->rank && rebind->rank <= 5);

    fence_proxy_tensormap_from_generic_acquire(rebind->scope);

    tensormap_replace_global_address(desc, rebind->address);

    for (int dim = 0; dim < rebind->rank; ++dim) {
        tensormap_replace_global_dim(desc, dim, rebind->extent[dim]);
    }

    for (int dim = 1; dim < rebind->rank; ++dim) {
        tensormap_replace_global_stride(desc, dim - 1, rebind->stride[dim]);
    }

    fence_proxy_tensormap_from_generic_release(rebind->scope);
}
```

## cp.async.bulk Template Catalog

The complete cp.async.bulk template inventory follows. Three emission
strategies coexist: fixed inline-assembly templates for the 2D
gather4/scatter4 forms and tensormap-replace mutators, runtime-assembled
strings for rank-1 through rank-5 tensor loads and stores, and
TableGen-registered NVVM ops for the generic intrinsic surface. The
descriptor pointer always threads as an i64 GPR with LLVM inline-asm
constraint class `"l"` regardless of address space; multicast masks use
`"h"` (i16), L2 cache hints use `"l"` (i64), coordinate operands use `"r"`
(i32), im2col offsets use `"h"` (i16). The descriptor slot sits at PTX
operand position `%1` on G2S loads and `%0` on S2G stores.

| Variant                                                  | Emitter path | Mode      | Dim | Multicast / mask | L2 hint | Im2col offsets | Descriptor operand |
| -------------------------------------------------------- | ------------ | --------- | --- | ---------------- | ------- | -------------- | ------------------ |
| `cp.async.bulk.tensor.{1..5}d.shared::cluster.global.mbarrier::complete_tx::bytes` | runtime builder | tile | 1–5 | opt, i16 `"h"` | opt, i64 `"l"` | n/a | `%1` `"l"` |
| `cp.async.bulk.tensor.{3..5}d.shared::cluster.global.mbarrier::complete_tx::bytes.im2col` | runtime builder | im2col | 3–5 | opt, i16 `"h"` | opt, i64 `"l"` | K offsets, `"h"` | `%1` `"l"` |
| `cp.async.bulk.tensor.2d.tile::gather4.shared::cta.global.mbarrier::complete_tx::bytes` | fixed asm | gather4 | 2 | n/a | n/a | n/a | `$1` `"l"` |
| `cp.async.bulk.tensor.{1..5}d.global.shared::cta.bulk_group` | runtime builder | tile | 1–5 | n/a | n/a | n/a | `%0` `"l"` |
| `cp.async.bulk.tensor.2d.tile::scatter4.global.shared::cta.bulk_group` | fixed asm | scatter4 | 2 | n/a | n/a | n/a | `$0` `"l"` |
| `cp.async.bulk.tensor.s2g.im2col.{3..5}d` | LLVM intrinsic | im2col | 3–5 | n/a | per-intrinsic | per-intrinsic | LLVM-managed |
| `cp.async.bulk.tensor.s2g.tile.{1..5}d` | LLVM intrinsic | tile | 1–5 | n/a | per-intrinsic | n/a | LLVM-managed |
| `cp.async.bulk.tensor.reduce` (`mode` + `redKind`)       | LLVM intrinsic  | tile/im2col + 8-way redKind | 1–5 | n/a | n/a | per-mode | LLVM-managed |
| `cp.async.bulk.tensor.prefetch`                          | LLVM intrinsic  | tile/im2col | 1–5 | n/a              | per-intrinsic | per-mode      | LLVM-managed |
| `cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes` | LLVM intrinsic | n/a | scalar | n/a | n/a | n/a | n/a (byte count) |
| `cp.async.bulk.global.shared::cta.bulk_group` (non-tensor) | LLVM intrinsic | n/a       | scalar | n/a            | n/a     | n/a            | n/a (raw byte-count) |
| `cp.async.bulk.shared::cluster.shared::cta.mbarrier::complete_tx::bytes` | LLVM intrinsic | n/a | scalar | n/a | n/a | n/a | n/a |

Modifier cascade order is fixed by both the emitter and PTX ISA 8.4:
`.im2col` → `.multicast::cluster` → `.L2::cache_hint`. Trailing operands
emit with the ordinary comma-space separator NVPTX assembly expects. The
multicast mask is an `OptionalAttr<I16Attr>` at adaptor slot 7; bit `i`
set in the mask selects CTA `i` (cluster maximum is 16, hence the 16-bit
width). The L2 cache hint is an `OptionalAttr<I64Attr>` at adaptor slot 8
and threads through as an opaque cookie the PTX assembler decodes
(eviction policy plus priority). Trailing operand indices for the optional
tails compute as `mcast_opnum = rank + 3 + im2col_count` and
`ch_opnum = mcast_opnum + (mcast_present ? 1 : 0)`. The TMA-load mode
enum (NO_MULTICAST=0, TWO_CTA=1, W_MULTICAST=2, W128_MULTICAST=3) gates
the `.multicast::cluster` modifier; the TMA-store mode enum (TILED=0,
IM2COL=1, IM2COL_W=2, IM2COL_W128=3) selects between the two
runtime-assembled emitters; the reduce `redKind` enum is the 8-valued
{ADD, MIN, MAX, INC, DEC, AND, OR, XOR} family but carries no PTX-text
emitter inside `tileiras` — every reduce variant lowers through
`int_nvvm_cp_async_bulk_tensor_reduce_*` NVPTX intrinsics. The
gather4 / scatter4 forms are 2D-only and Blackwell-specific (SM100+).

## TMA Descriptor Mutators

Of the nine `tensormap.replace.tile.*` field-mutator templates that
PTX ISA 8.3 defines, Tileiras emits only three device-callable mutators:

| Field             | Template (verbatim, `{0}` = address-space token, `{1}` = decimal index) | Width   | Constraint                                  | Emitted | Writes to                  |
| ----------------- | ----------------------------------------------------------------------- | ------- | ------------------------------------------- | ------- | -------------------------- |
| `global_address`  | `tensormap.replace.tile.global_address.{0}.b1024.b64 [$0], $1;`         | b1024.b64 | `"l,l"` (global) / `"r,l"` (`shared::cta`) | once    | `DESC_TMA512+0x00` (tensor_base_ptr) |
| `global_dim`      | `tensormap.replace.tile.global_dim.{0}.b1024.b32 [$0], {1}, $1;`        | b1024.b32 | `"l,r"`                                     | `rank` times, `i ∈ [0, rank)` | i-th 32-bit lane inside `fmt_dim_stride_packed` |
| `global_stride`   | `tensormap.replace.tile.global_stride.{0}.b1024.b64 [$0], {1}, $1;`     | b1024.b64 | `"l,l"`                                     | `rank-1` times, `i ∈ [1, rank)`, `{1}` = `i-1` | `(i-1)`-th 64-bit lane inside `elem_stride_packed` |

The other PTX ISA 8.3 mutators — `box_size`, `element_stride`,
`swizzle`, `fill_mode`, `elemtype`, `interleave`, `rank`, and the entire
`tensormap.replace.im2col.*` family — never appear in this path. The
structural consequence is sharp: any rebind that would change format,
box-shape, swizzle, fill-mode, element-type, or interleave layout must
round-trip through the host-side `cuTensorMapEncode*` driver entry; the
in-place device-side mutator handles only the
**tiled-rebind-with-new-base-and-extents** subset. Equivalently, im2col
descriptors are always host-born via `cuTensorMapEncodeIm2col`, and SM100
paired-CTA `TWO_CTA` descriptors are always host-born because the CTA
V-map folds into `box_size_packed` — which has no mutator. The dim-0
stride is implicit 1 (= element size) and never written by the device
path; the host encoder bakes it in at birth time.

The full device-side rebind sequence per descriptor is therefore:
optional `tensormap.cp_fenceproxy.global.shared::cta.tensormap::generic.release.cta.sync.aligned`
seed-copy from a host-prepared template, one
`fence.proxy.tensormap::generic.acquire.{cta|gpu|sys}`, one
`global_address` write, `rank` `global_dim` writes, `rank-1`
`global_stride` writes, one
`fence.proxy.tensormap::generic.release.{cta|gpu|sys}`, then the consumer
thread's matching acquire fence before the `cp.async.bulk.tensor.*` read.
Within a single CTA the `.cta` fence scope suffices; across CTAs in a
cluster the `.gpu` scope is mandatory — or the descriptor must be
re-staged into each CTA's own `shared::cta` slot via `cp_fenceproxy`.
Every replace mutator has side effects and must remain ordered with
respect to the surrounding proxy fences.
