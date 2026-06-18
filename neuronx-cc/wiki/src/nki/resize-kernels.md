# Resize Kernels

> *All symbols, addresses, and source line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/311/312 wheels; Cython modules under `neuronxcc/starfish/penguin/targets/codegen/`, NKI leaves under `neuronxcc/private_nkl/`). Other wheels differ; treat every address as version-pinned.*

## Abstract

Nearest-neighbour image resize on Trainium/Inferentia is **a pure indirect-DMA gather** — there is no interpolation arithmetic on the pixel data at all. The forward kernel, `resize_nearest_fixed_dma_kernel`, computes one source-pixel index per output pixel and issues a single scatter-gather DMA that copies each output pixel's channel vector straight from its nearest source pixel. The "fixed" in the name is the crucial property: the scale factors are **compile-time-constant Python floats** baked into the trace, so the entire output→input index map is a *static index graph* — built once, with constant operands, no runtime scale operand and no dynamic index recomputation.

The kernel is the `kernel_func` of the `"ResizeNearest"` entry in the internal-kernel registry (see [internal-kernel-registry](internal-kernel-registry.md)). At codegen the compiler re-traces it through the new NKI frontend, supplying one extra argument — the target output shape — via the registry's `additional_args_builder`, `_get_resize_args`. That extractor's entire body is `return {"out_shape": inst.results[0].access_shape}`. The kernel derives the scales internally from `data_tensor.shape` and `out_shape`; nothing else is plumbed in, because the coordinate-transform mode and `align_corners` flag are validated and fixed **upstream**, at HLO lowering, where only `align_corners=false` (the asymmetric, floor-rounding convention) is legal.

The page covers: the floor-rounding asymmetric index transform and *why* it forces an explicit index vector instead of a strided descriptor; the static-index-graph construction (iota → reciprocal-multiply → HW-floor → mod → mul/add); the two-DMA gather/store; the `_get_resize_args` registry binding; and the **separate** backward path. The gradient op, `ResizeNearestGrad`, never reaches this kernel — it is lowered to an average-pool (`reduce_window`+`divide`) far earlier in C++ MLIR; it shares the op *family* and conventions with the forward kernel but **none** of the codegen and **not** `_get_resize_args` (see [canonicalize-for-tensorizer](../hlo-opt/canonicalize-for-tensorizer.md)).

For reimplementation, the contract is:

- The **asymmetric/floor index transform**: `src = floor(dst * (in/out))`, with the nested-floor subtlety that makes the per-pixel offset non-affine.
- The **static index graph**: scales are compile-time constants, so `load_indices` is computed with constant operands per 128-pixel tile — no runtime scale, no interpolation.
- The **indirect-DMA gather** access-pattern (`vector_offset` + `indirect_dim=0`) and the plain strided store.
- The **registry plumbing**: `_get_resize_args(inst) = {"out_shape": inst.results[0].access_shape}`, and why the kernel needs only the target shape.

| | |
|---|---|
| **Forward kernel (lowered)** | `resize_nearest_fixed_dma_kernel` — `neuronxcc/private_nkl/resize.py:8` |
| **Forward kernel (public)** | same name — `neuronxcc/nki/kernels/vision.py:169` (semantically identical reference form) |
| **Floor primitive** | `floor_nisa_kernel` — `neuronxcc/private_nkl/utils/kernel_helpers.py:99` |
| **Arg extractor** | `_get_resize_args` — `BirCodeGenLoop.py:266`; decompile `@0x957e0` |
| **Registry entry** | `_INTERNAL_KERNEL_REGISTRY["ResizeNearest"]`; builder `@0xbd4b0` |
| **IR level** | Penguin → NKI re-trace → BIR (the `InstNKIKLIRKernel` re-trace sink) |
| **Coordinate transform** | asymmetric only (`align_corners=false`), nearest-mode = `floor` |
| **Backward op** | `ResizeNearestGrad` → average-pool, **different path** (`@0x207df40`) |

## The index transform

### Purpose

Map each output pixel `(oh, ow)` to the single nearest source pixel `(floor(oh·h_scale), floor(ow·w_scale))`, NHWC, with the ONNX/TensorFlow *asymmetric* (`align_corners=false`) transform and `floor` rounding. The whole kernel exists to materialise that map as an integer index vector; the actual pixel move is a copy.

### Algorithm

Scales are the **inverse** of the usual upsample factor — input-over-output — and are Python floats fixed at trace time (`resize.py:32-33`):

```c
function resize_index_transform(p):           // resize.py:60-93, per output pixel p
    h_scale = 1.0 * in_h / out_h              // compile-time const float  (resize.py:32)
    w_scale = 1.0 * in_w / out_w              // compile-time const float  (resize.py:33)

    out_h_idx = floor(p / out_w)              // output row  — itself an integer
    out_w_idx = p - out_h_idx * out_w         // output col  (= p mod out_w)
    src_h_idx = floor(out_h_idx * h_scale)    // nearest source row
    src_w_idx = floor(out_w_idx * w_scale)    // nearest source col
    load_indices = src_h_idx * in_w + src_w_idx   // flat source pixel index over in_h*in_w
    return load_indices
```

This is `src = floor(dst · (in/out))`: **no `+0.5` half-pixel term, no `(out-1)/(in-1)` align-corners scale.** `scale < 1` upsamples (`out > in`); `scale > 1` downsamples. The kernel docstring's worked example is an upsample, `in_h=30→out_h=59` (`h_scale ≈ 0.508`), `in_w=20→out_w=38` (`w_scale ≈ 0.526`) — deliberately *fractional*, "designed to be used when the scaling factor is not an integer" (`resize.py:9-14`).

> **QUIRK — the nested floor is what forbids a strided descriptor.** `src_h_idx = floor(out_h_idx · h_scale)` where `out_h_idx = floor(p / out_w)` is *already* an integer. For fractional scale this is piecewise-constant in `p`, **non-affine** — there is no constant stride that walks `load_indices`. That is precisely why the kernel materialises an explicit per-pixel index vector and gathers, rather than encoding a strided DMA. A reimplementer who assumes a linear descriptor will be correct only for the integer-scale case the kernel never special-cases.

### Considerations

The transform is hard-coded to the asymmetric+floor convention because the alternatives are rejected **before** the kernel is ever traced. The HLO-lowering validator (in `hlo2penguin`) emits, verbatim in `.rodata`:

- `"ResizeNearest: align_corners=true is not supported in this implementation."`
- `"Set align_corners=false in your ResizeNearest configuration."`

So half-pixel / align-corners variants never reach codegen, and the NKI kernel carries no mode parameter — neither the transform mode nor `align_corners` is among the kwargs `_get_resize_args` supplies. (CONFIRMED — error strings; backing D-AA07 §d.)

---

## The static index graph

### Purpose

Realise the per-tile `load_indices` column on-chip, entirely from compile-time-constant operands. Because the scales are baked floats, the only runtime input to the graph is the output position `p` — produced by `iota`. There is no scale operand, no division by a runtime value, and no interpolation. The graph is *static* in the strong sense: its operands (`1.0/out_w`, `out_w`, `h_scale`, `w_scale`, `in_w`) are all constants known at trace time.

### Algorithm

The spatial `H·W` axis is flattened and tiled by 128 onto the SBUF **partition** dimension; channels are the free dimension (`resize.py:36-48`):

```c
function build_index_graph(tile_start, actual_tile_size):   // resize.py:50-93
    // output_pos[i] = tile_start + i, one flat output position per partition
    output_pos = iota(pattern=[[1,1]], offset=tile_start, channel_multiplier=1)  // resize.py:53

    // out_h_idx = floor(output_pos / out_w)   — divide-by-reciprocal, then HW-floor
    out_h_f32 = tensor_scalar(output_pos, op0=multiply, operand0=(1.0/out_w))   // resize.py:63
    out_h_idx = floor_nisa_kernel(out_h_f32, ..., actual_tile_size, 1)          // resize.py:65

    // out_w_idx = output_pos - out_h_idx*out_w   (= p mod out_w)
    temp_mul_w = tensor_scalar(out_h_idx, op0=multiply, operand0=float(out_w))  // resize.py:69
    out_w_idx  = tensor_tensor(output_pos, temp_mul_w, op=subtract)             // resize.py:70

    // src_{h,w}_idx = floor(out_{h,w}_idx * scale)
    src_h_f32 = tensor_scalar(out_h_idx, op0=multiply, operand0=h_scale)        // resize.py:79
    src_h_idx = floor_nisa_kernel(src_h_f32, ..., actual_tile_size, 1)          // resize.py:81
    src_w_f32 = tensor_scalar(out_w_idx, op0=multiply, operand0=w_scale)        // resize.py:84
    src_w_idx = floor_nisa_kernel(src_w_f32, ..., actual_tile_size, 1)          // resize.py:86

    // load_indices = src_h_idx*in_w + src_w_idx   (int32 column)
    temp_mul = tensor_scalar(src_h_idx, op0=multiply, operand0=in_w)            // resize.py:90
    load_indices = tensor_tensor(temp_mul, src_w_idx, op=add)                   // resize.py:93
    return load_indices    // shape (actual_tile_size, 1)
```

Every tensor is an `(actual_tile_size, 1)` column — one source offset per output pixel. The last tile is handled by **shrinking** it: `actual_tile_size = min(128, out_seqlen - tile_start)` (`resize.py:48`).

> **NOTE — "fixed_dma" names the static graph, not a single descriptor.** The scale is fixed at compile time, so the index-compute graph is fully static and `load_indices` is built once per tile from constant operands. It does **not** mean a single static linear DMA descriptor for the whole image — the gather is still an indirect, per-pixel vector offset (next section). The name describes the *constancy of the scale*, which is what makes the index graph static.

### floor_nisa_kernel — the HW-correct floor

A plain `float→int32` cast rounds to **nearest-even** on this hardware (ticket kaena-4592), not toward `-∞`, so it cannot implement `floor`. The helper builds a true floor out of the cast (`kernel_helpers.py:99-127`):

```c
function floor_nisa_kernel(a, out, p, f):     // kernel_helpers.py:99
    casted   = (int32) a                       // round-to-nearest-even cast   (tensor_copy)
    b        = (float) casted                  // back to float                (tensor_copy)
    cast_m1  = casted - 1                       // tensor_scalar subtract 1
    cond     = (b >  a)                         // int8; the cast rounded UP    (tensor_tensor greater)
    cond_not = cond XOR 1                       // (b <= a)                     (tensor_scalar logical_xor)
    smaller  = casted  * cond_not               // keep cast when it didn't round up
    larger   = cast_m1 * cond                   // else subtract 1
    out      = larger + smaller                 // floor(a)
```

The math is "subtract 1 iff the round went up." `resize.py` calls it three times — for `out_h_idx`, `src_h_idx`, `src_w_idx` — each as `floor_nisa_kernel(<f32 src>, <int32 dst>, actual_tile_size, 1)`.

> **GOTCHA — do not floor with a cast.** A reimplementation that uses a single `float→int` cast for any of these floors will silently mis-place pixels at exactly-integer products (where nearest-even and floor diverge) and at the boundary of every scale band. The cast-plus-correction is mandatory, not an optimisation.

---

## The DMA: gather then store

### Purpose

Move pixel data. Per `(batch b, tile)` the kernel issues exactly **two** DMAs: one *indirect* gather (HBM→SBUF) driven by `load_indices`, one *plain strided* store (SBUF→HBM). No compute touches the pixel values between them — the resize is a copy.

### Algorithm

```c
function resize_dma(b, tile_start, actual_tile_size, load_indices):  // resize.py:95-119
    // --- GATHER: indirect / vector-offset DMA, HBM -> SBUF ---
    loaded_tile_sbuf = sbuf(actual_tile_size, out_c)
    dma_copy(dst = loaded_tile_sbuf,
             src = data_view_flattened.ap(                       // (in_b*in_seqlen, in_c) row-major
                     pattern       = [[in_c, actual_tile_size],  // partition axis, nominal stride in_c
                                      [1, out_c]],                // free axis: out_c contiguous channels
                     offset        = b * in_seqlen * in_c,        // batch slice
                     vector_offset = load_indices,                // per-partition source-pixel index
                     indirect_dim  = 0))                          // dim-0 base is REPLACED by vector_offset

    // --- STORE: plain strided DMA, SBUF -> HBM (contiguous output run) ---
    dma_copy(dst = output_view_flattened.ap(
                     pattern = [[out_c, actual_tile_size], [1, out_c]],
                     offset  = b*out_seqlen*out_c + tile_start*out_c),
             src = loaded_tile_sbuf)
```

With `indirect_dim=0`, the partition axis's *nominal* stride (`in_c`) is ignored; partition `i`'s base offset becomes `vector_offset[i] · in_c`. The effective source address for output pixel `i`, channel `c` is:

```text
base + (b*in_seqlen + load_indices[i]) * in_c + c
```

— exactly "copy the `in_c`-wide channel vector of nearest source pixel `load_indices[i]`." The **output** side is a dense sweep (tile pixels are contiguous `tile_start..tile_start+actual_tile_size-1`), so the store is a pure affine descriptor — stride `out_c` on dim-0, unit on dim-1, no indirection. Only the input is gathered.

> **QUIRK — integer scale is not special-cased.** When `in/out` divides evenly, `src = floor(dst·k) = dst·k` is affine and the gather could collapse to a constant-stride DMA (partition stride `k·in_c`). This kernel never does that — it always builds the explicit `load_indices` and uses the indirect path (no special-case in the source). The integer-ratio fast path lives only on the **backward** side, as a `reduce_window`. (INFERRED that a strided collapse is possible; CONFIRMED that the kernel does not implement it — backing D-AA07 §c.)

### Considerations

Cost: the vector-engine index work is `O(out_h·out_w)` scalars per tile (iota + ~5 tensor ops + 3 floors); the DMA moves `out_h·out_w·out_c` elements. The design "bottoms out" at the indirect DMA: for fractional scale there is no closed-form strided descriptor, so paying for an on-chip index vector + gather is the only correct encoding. There is **no interpolation arithmetic on pixel data** — `loaded_tile_sbuf` is stored unmodified.

---

## Registry binding — `_get_resize_args`

### Purpose

`resize_nearest_fixed_dma_kernel` takes two positional args — `data_tensor` and `out_shape` — but the IR op carries only the input operand. The registry's `additional_args_builder` for `"ResizeNearest"`, `_get_resize_args`, supplies the second: the desired output shape, pulled from the op's single result tensor.

### Entry Point

```text
codegenInternalNativeNkiKernel @0x8d630          ── meets an InternalNativeNkiKernel macro
  └─ _resolve_kernel_config @0xa0ca0             ── looks "ResizeNearest" up in the registry
       ├─ kfn      = config.kernel_func          ── resize_nearest_fixed_dma_kernel
       ├─ extra_kw = config.additional_args_builder(inst)   ── _get_resize_args(inst) @0x957e0
       └─ traces   kfn(*operands[:opcount], **extra_kw)
                 = resize_nearest_fixed_dma_kernel(data_tensor, out_shape=inst.results[0].access_shape)
```

### Algorithm

The extractor's entire body, recovered from the Cython decompile `@0x957e0` (`BirCodeGenLoop.py:266-267`):

```c
function _get_resize_args(inst):              // @0x957e0  — CONFIRMED, decompile + strings
    d = PyDict_New()                          // decompile line 282
    r = getattr(inst, "results")              // __pyx_n_s_results, line 287
    r0 = r[0]                                 // PyObject_GetItem(r, 0), line 319
    shp = getattr(r0, "access_shape")         // __pyx_n_s_access_shape, line 367
    d["out_shape"] = shp                      // PyDict_SetItem, line 387
    return d
```

The registry construction (builder `@0xbd4b0`) imports the kernel by exact name and binds the config:

```c
from neuronxcc.private_nkl.resize import resize_nearest_fixed_dma_kernel
registry["ResizeNearest"] = InternalKernelConfig(
    kernel_func             = resize_nearest_fixed_dma_kernel,
    operand_count           = 1,                  // data_tensor is the only operand
    additional_args_builder = _get_resize_args)
```

### Considerations

**Why a shape and not a scale.** NN-resize output is an explicit target size, not derivable from the input plus a stored scalar. The kernel reconstructs `h_scale, w_scale = in/out` internally from `data_tensor.shape` and `out_shape`, so the single extra argument it needs is exactly the output shape — which `_get_resize_args` reads from `inst.results[0].access_shape`, a quantity the compiler already knows from the HLO op's result type. The mode and `align_corners` are *not* passed; they were fixed upstream.

> **NOTE — `operand_count=1` is explicit.** The registry-builder disassembly constructs this config with `operand_count=1` (it is the single input operand, `data_tensor`). The backing report treats it as a default; the registry page ([internal-kernel-registry](internal-kernel-registry.md)) shows it spelled out. Either way the traced call marshals one operand. (STRONG — registry page cross-confirms the explicit kwarg.)

---

## Forward vs backward — the tie that is not shared code

The forward op (`mhlo.resize_nearest`, custom-call target `"ResizeNearest"`) and the gradient op (`ResizeNearestGrad`) share the op family and the asymmetric/integer-ratio conventions, but **no codegen and not `_get_resize_args`**.

| | Forward — `ResizeNearest` (this page) | Backward — `ResizeNearestGrad` |
|---|---|---|
| **Where lowered** | Penguin codegen, Python registry re-trace | C++ MLIR, *before* tensorization |
| **Lowered by** | `_INTERNAL_KERNEL_REGISTRY["ResizeNearest"]` → NKI kernel | `CanonicalizeForTensorizer::lowerResizeNearestGrad` `@0x207df40` |
| **Becomes** | `resize_nearest_fixed_dma_kernel` (indirect-DMA gather) | `reduce_window(add)` + `broadcast` + `divide` (average-pool of grad) |
| **Arg plumbing** | `_get_resize_args` → `out_shape` | none — ratio carried in result-vs-operand shapes |
| **Scale** | fractional or integer | integer ratio only ("output dim N integral multiple of input dim N") |
| **Reaches this kernel?** | yes | **never** |

The backward path is fully documented at [canonicalize-for-tensorizer](../hlo-opt/canonicalize-for-tensorizer.md#lowerresizenearestgrad--average-pool-lowering-of-the-resize-gradient): the forward NN-upsample replicates one source pixel into a `k×k` output block, so the gradient w.r.t. that source pixel is the **sum** of the upstream gradients of all output pixels that copied it — a non-overlapping `k×k` window-sum (`window=stride=k`), i.e. `reduce_window` with an add body, then a `divide` for normalisation. The integer-ratio constraint is the integer-`k` requirement of this pooling.

> **CORRECTION (D-AA07/D-C09) —** forward and backward do **not** share `_get_resize_args`. An assumption that the gradient reuses the forward arg-extractor is wrong: the gradient is gone before the NKI frontend ever runs, and it carries its own ratio in its shapes. The only shared artefact is the op-family/config schema, not the extraction function. (CONFIRMED — separate addresses, separate IR stages; backing D-AA07 §d.)

---

## Two source variants — which one is real

There are two byte-identical-*logic* copies of `resize_nearest_fixed_dma_kernel`. The registry imports the first.

| Variant | File | Form | Ragged last tile | Floor |
|---|---|---|---|---|
| **Lowered (real)** | `private_nkl/resize.py:8` | low-level `nisa.*` + explicit `dma_copy(..., vector_offset, indirect_dim=0)` | shrinks tile (`actual_tile_size`) | `floor_nisa_kernel` (HW-correct) |
| **Public reference** | `nki/kernels/vision.py:169` | high-level `nl.*` (`nl.floor`/`nl.mod`/advanced-index gather) | masks lanes (`output_pos < out_seqlen`) | `nl.floor` |

The lowered variant is the one the codegen imports — confirmed by the registry-builder import `from neuronxcc.private_nkl.resize import resize_nearest_fixed_dma_kernel` and the module string `"neuronxcc.private_nkl.resize"`. The public form is the readable reference: its `data_view_flattened[b, load_indices, i_f_channel]` advanced indexing (`vision.py:233`) lowers to the same indirect DMA. Both compute the identical `load_indices = floor(floor(p/out_w)·h_scale)·in_w + floor((p mod out_w)·w_scale)`.

> **GOTCHA — verify against `private_nkl/resize.py`, not `vision.py`.** A reimplementer reading the public `vision.py` form will see `nl.floor` and a masked full-128 tile. The *lowered* kernel that actually runs uses the cast-plus-correction `floor_nisa_kernel` and a shrunk last tile. The semantics match, but the HW-floor detail (kaena-4592) is only visible in the `private_nkl` copy.

## Cross-References

- [internal-kernel-registry](internal-kernel-registry.md) — 6.6.2, the `"ResizeNearest"` entry, `InternalKernelConfig` schema, and `_get_resize_args` as the arg-builder
- [canonicalize-for-tensorizer](../hlo-opt/canonicalize-for-tensorizer.md) — 4.39, the backward `lowerResizeNearestGrad` average-pool lowering (`reduce_window`+`divide`)
- [three-sink-kernel-model](three-sink-kernel-model.md) — the `InstNKIKLIRKernel` re-trace sink this kernel dispatches through
- [bircodegenloop](bircodegenloop.md) — the codegen loop that re-traces internal NKI kernels
- [bircodegen-dma](bircodegen-dma.md) — DMA access-pattern construction, `vector_offset` / `indirect_dim` indirect-gather descriptors
