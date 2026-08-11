# Experimental conv2d_pbp Kernels

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The two kernels live ONLY as compiled bodies in `neuronxcc/nki/_private_kernels/conv.cpython-310-x86_64-linux-gnu.so` (5,993,224 B; Cython-3, `-O3`); cp311/cp312 carry byte-identical sources and the same `__pyx` roster (not byte-diffed). There is no readable `.py` twin for the pbp functions — the readable `neuronxcc/private_nkl/conv.py` (4259 L, "New NKI FE, Migrated from old NKI FE") ships the depthwise / column-packing / `rep_nhwc_Pcinh` siblings with identical helpers and naming, but NOT pbp. Algorithm and op-flow are grounded on that twin and flagged where the Cython `mstate` string-indirection blocks static call-order recovery. Provenance: cross-checked against the binary string/symbol table and `private_nkl/conv.py`.*

## Abstract

`conv2d_pbp_0f1b_0i1o_01fb_experimental_1` and `conv2d_pbp_fb01_io01_01bf_kernel_experimental_1` are two **full cross-channel** (not depthwise) experimental 2-D convolution kernels that lower a `conv` op to the Tensor-Engine `nisa.nc_matmul` primitive by **implicit GEMM**. "pbp" is *partition-by-partition*: the kernel maps the convolution's contraction dimension onto the 128-wide SBUF/PE partition axis, packing `C_in × H_REP` replicated rows there so a single matmul contracts an entire filter-height window of the receptive field at once. This is the same `Pcinh` ("**P**artition = **c**_**in** × **h**") packing the production `conv2d_dw_..._rep_nhwc_Pcinh` sibling uses, lifted from depthwise to the cross-channel case.

The two kernels compute the *same* convolution; they differ on exactly two axes. First, **memory layout**: each kernel name hard-bakes a permutation of the operands' HBM axes — `0f1b_0i1o_01fb` is height-major, `fb01_io01_01bf` is feature(channel)-major — so the partition axis can be fed by a contiguous HBM stride without an extra transpose under that layout. Second, **prefetch strategy**: the `0f1b` variant owns a double-buffer that issues the next filter (and image) DMA a tile ahead to overlap load with matmul; the `fb01` variant is the synchronous baseline. Both emit a spatial-major (`01xx`) output so the downstream consumer sees a uniform layout regardless of which ran.

Neither kernel is ever auto-matched. The conv-op → NKI-kernel selector (`TransformConvOp`, documented in [Conv Device-Lowering](conv-device-lowering.md)) does not list either pbp name; the names appear only as explicit local bindings in the kernel-inlining pass and in the BIR code generator. They are reachable by **explicit kernel selection only** — first-iteration experimental cuts (`_experimental_1`, no `_2` successor) that were never carried into the new readable FE. This page documents the layout-code decode, the `C_in × H_REP` tiling, the implicit-GEMM op flow, the two prefetch variants, and the binary evidence that pins them as never-auto-matched.

For reimplementation, the contract is:

- **The layout-code decode** — how each four-char token group in the kernel name maps to an HBM axis order, and why the two variants pick different permutations.
- **The `filter_partition_dim = C_in × H_REP` partition packing** — `replication_factor`, the partition-axis budget, and the dimension → hardware-axis map.
- **The implicit-GEMM tile nest** — weight-stationary / ifmap-moving `nc_matmul`, im2col-by-access-pattern (no materialized im2col), PSUM accumulation over `f_W` taps and `H_OUTER` tiles, drain-and-accumulate.
- **The two prefetch variants** — what the `0f1b` double-buffer adds over the `fb01` baseline, and the SBUF/transpose trade-off.
- **The never-auto-matched status** — which registry lists the kernel and which does not.

| | |
|---|---|
| **Kernel A (height-major, prefetch)** | `conv2d_pbp_0f1b_0i1o_01fb_experimental_1` — `__pyx_pw_…_15conv2d_pbp_0f1b…` @ `0x095340` (`0x2487e` B) |
| **Kernel B (feature-major, baseline)** | `conv2d_pbp_fb01_io01_01bf_kernel_experimental_1` — `__pyx_pw_…_13conv2d_pbp_fb01…` @ `0x0b9bc0` (`0x29d03` B) |
| **Docstrings** | `__pyx_doc_…_14conv2d_pbp_0f1b…` @ `0x1bdb20` (151 B) · `__pyx_doc_…_12conv2d_pbp_fb01…` @ `0x1bdbc0` (89 B) |
| **Bodies** | Inlined into the `__pyx_pw_*` wrappers (no separate `__pyx_pf_*`) — large straight-line tiling kernels |
| **Lowers to** | `nisa.nc_matmul` (weight-stationary implicit GEMM), `nisa.tensor_copy`, `nisa.tensor_tensor`, `nisa.dma_copy` |
| **Selector** | `TransformConvOp` (auto-match) — **does not list pbp**; reachable only via explicit inline (`InlineNKIKernels`) |
| **Readable twin** | `neuronxcc/private_nkl/conv.py` `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh_default` L1104 (helpers L24-111) |

---

## 1. Layout-Code Decode

### Purpose

Each kernel name carries three four-character token groups, in the fixed order
`<img-input-desc> _ <filter-desc> _ <output-desc>`. A token group is the physical dim **order** (outermost → innermost HBM axis) of one operand. This is the same information the generic conv kernel passes at runtime via `in_perm` / `kern_perm` / `out_perm` tuples; the pbp kernels **hard-bake one permutation into the function name and body** instead of branching on a runtime perm tuple. Decoding the name therefore tells a reimplementer the exact HBM stride pattern the kernel assumes.

### Encoding

The `.so` docstrings make the mapping explicit and verbatim (`.rodata` @ `0x1bdb20` / `0x1bdbc0`):

```text
conv2d_pbp_0f1b_0i1o_01fb_experimental_1:
  "kernel_img_input_desc='0f1b', kernel_filter_desc='0i1o', kernel_output_desc='01fb'"
  + "Prefetches filter"
conv2d_pbp_fb01_io01_01bf_kernel_experimental_1:
  "kernel_img_input_desc='fb01', kernel_filter_desc='io01', kernel_output_desc='01bf'"
  (no "Prefetches filter" line)
```

The token alphabet is per-operand — the same glyph means a different logical axis depending on which group it sits in. The kernel and module docstrings name the logical axes:

| Glyph | In IMG group (`N,C,H,W`) | In FILTER group (`i,o,f_H,f_W`) | In OUT group (`N,out_C,out_H,out_W`) |
|---|---|---|---|
| `f` | `C` (feature/channel) | — | `out_C` |
| `b` | `N` (batch) | — | `N` (batch) |
| `i` | — | `C_in` | — |
| `o` | — | `C_out` | — |
| `0` | `H` (spatial-0 / height) | `f_H` (filter height) | `out_H` |
| `1` | `W` (spatial-1 / width) | `f_W` (filter width) | `out_W` |

> **GOTCHA —** the glyph alphabet is *positional*, not global. `f` is the **channel** axis in the image/output groups but never appears in the filter group (which uses `i`/`o` for in/out channels). A decoder that treats `f` as one fixed axis across all three groups will mis-order the filter. Decode each group against its own logical-axis tuple.

### Decode — Kernel A: `0f1b_0i1o_01fb`

```text
img    "0f1b" -> (H, C, W, N)              height outermost, batch innermost
filter "0i1o" -> (f_H, C_in, f_W, C_out)   filter-height outermost, C_out innermost
out    "01fb" -> (out_H, out_W, out_C, N)  spatial-major, batch innermost
```

The leading `0` on all three groups puts the **filter-height / image-height axis as the outer loop** — consistent with the `H_OUTER_NUM_TILES` / `H_REP` tiling in the body (filter rows tiled outermost). The trailing `b` (N) on img and out makes batch the innermost free axis streamed through the matmul.

### Decode — Kernel B: `fb01_io01_01bf`

```text
img    "fb01" -> (C, N, H, W)              feature-major, batch 2nd
filter "io01" -> (C_in, C_out, f_H, f_W)
out    "01bf" -> (out_H, out_W, N, out_C)  spatial-major, out_C innermost
```

This is the NHWC-replication layout: the image is channel-partition-major, so a single C-tile lands one channel block on the 128 partitions; `N` sits next so a DMA streams the batch as the matmul moving free-dim; `out_C` innermost is the PSUM-natural order (partition = `out_C`). The `fb01_io01_01bf` token set is **byte-identical** to the readable twin `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh_default` (the auto-matched production sibling), which is what pins this decode rather than leaving it inferred.

### Why the permutations differ

Both variants are the same convolution under two HBM layouts, chosen so the partition axis is fed by a contiguous stride without an extra transpose:

- **`fb01`/`io01`** — channel-major image+filter → `C_in` already adjacent to the partition axis → minimal pre-transpose. This is the production `rep_nhwc` path.
- **`0f1b`/`0i1o`** — height-major image+filter → the **filter-height window is contiguous**, which is exactly what the `H_REP` height-replication packing wants to stream — but `C_in` is *not* adjacent to partitions, so an ifmap/weight transpose may be needed to reach matmul layout. This variant pays that transpose and adds **filter prefetch** (§4) to hide the DMA of the height-major filter.

Both end at a `01xx` (spatial-major) output so the consumer's layout is uniform across variants.

---

## 2. Signature and Guards

### Signature

The `.so` interns exactly this parameter vocabulary (`__pyx_k_`), identical to the readable twin's `conv2d_dw_…Pcinh_default` signature (`conv.py` L1104-1119):

```c
def conv2d_pbp_<LAYOUT>_experimental_1(
        in_ref, filter_ref, out_ref, padding,
        stride=(1,1), rhs_dilation=None, lhs_dilation=None,
        feature_group_count=1, batch_group_count=1,
        srcs_shapes=None, dsts_shapes=None,
        in_perm=None, kern_perm=None, out_perm=None) -> out_ref
    // in_ref     : N,C,H,W            order set by in_perm
    // filter_ref : i(C_in),o(C_out),f_H,f_W  order set by kern_perm
    // out_ref    : N,out_C,out_H,out_W order set by out_perm
    // padding    : [[H_pad_l,H_pad_r],[W_pad_l,W_pad_r]]
```

> **NOTE —** `in_perm` / `kern_perm` / `out_perm` are accepted but redundant for these kernels: the layout is already fixed by the name. The generic conv kernel branches on these tuples at runtime; the pbp kernels exist precisely so it does not have to.

### Output shape

```c
H_out = (H_img + 2 * H_padding - H_filter) // H_stride + 1
W_out = (W_img + 2 * W_padding - W_filter) // W_stride + 1
```

(verbatim debug fragment: `"…(H_img + 2 * H_padding - H_filter) // H_stride + 1: …"`.)

### Guard set

| Guard message | Meaning | Confidence |
|---|---|---|
| `c_in_assert != C_in` | derived `C_in` must equal filter's `C_in` | CERTAIN |
| `c_out_assert != C_out` | derived `C_out` must equal filter's `C_out` | CERTAIN |
| `batch_assert != N_batch` | derived `N` must equal image `N` | CERTAIN |
| `H_filter > 128` | filter HEIGHT must fit in ≤128 partitions (the partition axis carries the filter-height window) | CERTAIN (string); HIGH (rationale) |
| `expect small conv window` | `f_H·f_W` / windowed `K` must stay small — the implicit GEMM packs the window onto free/part dims; a large window blows the 512 matmul-free limit | CERTAIN (string); HIGH (rationale) |
| `Only supports positive paddings` | negative padding (crop) is rejected here, unlike the dw kernels which trim | CERTAIN |
| `Only support stride of size 1` | the experimental pbp path restricts to stride (1,1) on these layouts | CERTAIN |
| `unsupported perm with strides` | strided conv only legal in feature-major layout | CERTAIN |
| `rhs_dilation is not supported when lhs_dilation is enabled` | input-dilation (`rhs`) and transpose-conv (`lhs`) dilation are mutually exclusive | CERTAIN |
| `wrong tilesize for striding` | from `tile_with_stride` — asserts `tile_size % stride == 0` (`conv.py` L104) | CERTAIN |

> **QUIRK —** the `H_filter > 128` guard is not a generic conv limit; it is a direct consequence of the pbp packing. Because the **filter-height window is replicated onto the partition axis** (§3), a filter taller than 128 rows cannot be laid onto the 128 SBUF/PE partitions at all. A reimplementer who lifts this kernel to taller filters must re-tile the height window across multiple matmuls (an `H_OUTER` loop), not just widen a buffer.

---

## 3. The Partition-by-Partition Tiling Algorithm

### Purpose

The kernel maps the convolution's contraction dimension onto the 128 SBUF/PE partitions and walks the problem partition-block by partition-block. The contraction a single `nc_matmul` performs is

```text
partition axis  =  C_in × H_REP        (the Pcinh packing)
```

where `H_REP = replication_factor(canonical_H_f, C_in, h_dilation)` is the largest filter-height window count `r` such that `r·C_in ≤ 128` and `r % h_dilation == 0`. One matmul thus reduces over a whole filter-height window of the receptive field.

### The replication factor

```c
function replication_factor(rep_from, rep_to, dilation=1):   // conv.py L55
    // rep_from = canonical_H_f (dilated filter height)
    // rep_to   = C_in
    max_rep = 0
    for i in range(rep_from, 0, -1):                          // largest first
        if i * rep_to <= 128 and i % dilation == 0:           // partition + dilation fit
            max_rep = i; break
    return max(max_rep, 1)                                    // never < 1

// caller, twin L1291 / L1408:
H_REP = replication_factor(canonical_H_f, C_in, rhs_dilation[0])
filter_partition_dim = C_in * H_REP
```

The name `filter_partition_dim` is the twin's, not the binary's. The pbp `.so` interned-string table (`__pyx_k_` / `__pyx_n_s_`) contains `H_REP`, `H_REP_NUM_TILES`, `C_NUM_TILES`, `COUT_NUM_TILES`, `K0`/`K1`/`WF`/`MM_REDUCE_NUM_TILES`, and `replication_factor` — but zero occurrences of `filter_partition_dim`. The quantity `C_in × H_REP` is solid, resting on the twin plus the `H_REP*` tile vars that *are* interned; only the identifier that holds it inside the pbp bodies is [INFERRED], carried over from `conv.py` L1408.

### Dimension → hardware-axis map

| Logical conv dim | Hardware axis | Tile var |
|---|---|---|
| `C_in` | PARTITION (stacked; tiled if `C_in>128`) | `C_NUM_TILES` |
| `f_H` window | PARTITION (replicated `H_REP` copies, interleaved with `C_in`) | `H_REP_NUM_TILES` |
| `f_W` (W_f) | LOOP — PSUM `+=` per `f_W` tap | `WF_NUM_TILES` |
| `H_f` outer | LOOP — filter-row tiles beyond what `H_REP` packs | `H_OUTER_NUM_TILES` |
| `out_C` | matmul STATIONARY free → PSUM PARTITION | `COUT_NUM_TILES` |
| `out_H` (K0) | matmul MOVING free (sliding rows) | `K0_NUM_TILES` |
| `out_W` (K1) | matmul MOVING free | `K1_NUM_TILES` |
| `N` (batch) | matmul MOVING free, innermost | `N_OUTER/N_COMP/N_DMA_*_TILES` |

The matmul free dim `K0·K1·N` must stay ≤ 512 (the PE moving-free limit; twin: `K0_TILE*K1_TILE*N_TILE <= 512 (matmul free dim limit)`). This is the real constraint behind the `expect small conv window` guard.

> **NOTE —** every tile-var name in the table is interned in the pbp `.so` string pool (`C_NUM_TILES`, `COUT_NUM_TILES`, `H_REP_NUM_TILES`, `H_OUTER_NUM_TILES`, `K0_NUM_TILES`, `K1_NUM_TILES`, `WF_NUM_TILES`, `MM_REDUCE_TILES`). Their precise tile-size *values* are computed at runtime, not string constants — not statically recoverable.

### The tile nest

```c
for bgc_tile in range(BGC_TILES):              // feature/batch-group-count blocks
  for c_tile in range(C_NUM_TILES):            // C_in>128 -> partition-tile C_in
    for n_outer in range(N_OUTER_NUM_TILES):   // batch outer
      for h_outer in range(H_OUTER_NUM_TILES): // filter-row outer tiles
        // --- LOAD: build im2col window in SBUF by strided DMA scatter ---
        for h_rep in range(H_REP):             // stack height window onto partitions
            dma_copy ifmap rows -> img_buffer[partition = h_rep*C_in + cin, free=K0*W*N]
        memset filter_buffer
        for wf_tile, h_rep:
            dma_copy filter -> filter_buffer   // partition = h_rep*C_in*dil
        // --- COMPUTE: implicit GEMM, PSUM accumulates over f_W ---
        for k0_tile / k1_tile / c_out_tile:
          for wf in range(W_f):                // one PSUM += per filter-col tap
            nc_matmul(out_psum[:c_out_count, :free],
                      stationary = filter_buffer.ap(...),   // weight-stationary
                      moving     = img_buffer.ap(strided window))   // ifmap streamed
        // --- DRAIN: PSUM -> SBUF -> accumulate -> store ---
        tensor_copy   psum -> temp_sbuf (vector_engine)
        tensor_tensor add: results_accumulation_sbuf += temp   // over H_OUTER tiles
      store results_accumulation_sbuf -> out_ref (01fb / 01bf layout)
```

> **GOTCHA —** the exact intra-body **call order** above is INFERRED by parallel to the twin, not read from the pbp `.so`. Cython-3 interns every name through the runtime-populated `__pyx_mstate_global` struct, so the pbp bodies reference op names only through mstate slots — no direct `lea` to a name appears in the body (all direct k-refs live in module-init). The op *set*, the data flow, and the tile *structure* are grounded; the per-instruction *sequence* is the twin's `Pcinh_default` flow and is flagged INFERRED throughout §3–§4.

### Weight-stationary vs moving

```c
nc_matmul(
    psum_banks[h_idx][c_out_tile][:c_out_count, :free_dim_size],
    stationary = filter_buffer.ap(                                 // FILTER is stationary
        pattern=[[filter_free_dim, filter_partition_dim], [1, c_out_count]],
        offset=filter_offset),
    moving     = img_buffer.ap(                                    // IFMAP is moving
        pattern=[[img_tile_free, filter_partition_dim],            // partition = C_in*H_REP
                 [h_stride*img_buffer_W*N_DMA_TILE_SIZES, k0_count],  // sliding rows
                 [w_stride*N_DMA_TILE_SIZES, k1_count],               // sliding cols
                 [1, n_count]],                                       // batch innermost
        offset=img_offset))
// output = stationaryᵀ @ moving = [C_out, K0*K1*N] in PSUM, fp32-accumulated
```

The FILTER is the **stationary** operand (resident in the 128×128 PE array, partition = `C_in·H_REP`, free = `C_out`); the IMAGE/ifmap is **moving** (streamed, partition = `C_in·H_REP`, free = `K0·K1·N` sliding window). This is weight-stationary conv — the `nc_matmul` contracts over the partition dim. Repeated `nc_matmul` into the **same** PSUM tile accumulates across `f_W` taps and `H_OUTER` tiles (the contraction exceeds 128).

### Implicit GEMM / im2col structure

There is **no materialized im2col matrix**. The sliding window is built two ways, both present in the pbp vocabulary:

1. **LOAD-side scatter** — `dma_copy` with a 4-axis access pattern lays ifmap rows at `partition = h_rep*C_in + cin` and `free = (K0 sliding rows)·W·N`, applying `H`-padding by clamping the row coordinate and `W`-padding by offset. (twin L1591-1652; pbp interns `img_buffers` / `img_local` / `sub_img_sbuf`, `flattend_input_img_*`, `W_l` / `W_total` padding bookkeeping.)
2. **COMPUTE-side access pattern** — the MOVING `.ap()` itself encodes the strided window (`[h_stride·W·N, k0], [w_stride·N, k1], [1, n]`), so the matmul reads the overlapping window directly from contiguous SBUF. **im2col is free** — pure addressing, not a copy (twin L1748-1776, shown above).

### Dilation / padding / stride

- `calc_dilated_dimension` / `calc_inverse_dilated_dimension` / `canonicalize_filter_shape` (all three interned in the pbp pool; bodies in twin L29-52) expand `f_H,f_W` by `rhs_dilation` **before** tiling: `canonical_H_f = (H_f-1)·dil_h + 1`. Dilated taps spread across partitions via `dst_partition_start = h_rep*C_in*rhs_dilation[0]`; the body interns `dilated_image` / `write_dilated_tensor` / `is_current_row_a_dilation_row` / `dilation_offset` to skip the zero rows of a dilated kernel.
- `lhs_dilation` = transpose-conv (input dilation), mutually exclusive with `rhs_dilation` (the guard above).
- Negative padding: `is_negative_padding` present, but pbp **guards it out** (`Only supports positive paddings`) rather than trimming (the dw kernels trim via `img_trimmed`).
- Stride: the experimental pbp restricts to stride (1,1) on these layouts; `h_stride_intra_tile` + `tile_with_stride` machinery exists but is gated off in the experimental cut.

---

## 4. Op Sequence and the matmul-Reduction Fast Path

### The op alphabet interned by the pbp bodies

```text
nl.*    : affine_range, arange, add, astype, all/any, dtype helpers
nisa.*  : nc_matmul, nc_transpose, memset, dma_copy, tensor_copy,
          tensor_tensor (add-accumulate), tensor_reduce
helpers : transpose_to_last_dim, tiled_dve_transpose_10, tiled_dve_transpose_210,
          tiled_pf_transpose
module  : div_ceil, tile, tile_with_stride, calc_dilated_dimension,
          calc_inverse_dilated_dimension, canonicalize_filter_shape,
          replication_factor, is_negative_padding, reshape_all, create_indices
```

### nc_matmul contract and the onezero reduction

```c
nc_matmul(stationary, moving, *, is_stationary_onezero=False, is_moving_onezero=False,
          is_transpose=False, tile_position=(), tile_size=(), mask=None) -> PSUM
// stationaryᵀ @ moving ; reads SBUF, writes PSUM, fp32 accumulate
// partition (contraction) <= 128 ; stationary free <= 128 ; moving free <= 512
```

The pbp bodies intern `is_stationary_onezero` **and** `mm_reduce_weight` together. The pairing is the **matmul-as-reduction** fast path: `mm_reduce_weight` is an all-ones stationary tile that **sum-reduces** across a tiled dimension via matmul; `MM_REDUCE_TILES` / `MM_REDUCE_TILE_SIZE` tile that reduction and `reduced` / `reduce_tile` hold the result. With `is_stationary_onezero=True` (which hints the stationary operand is ones/zeros only) the reduction runs at up to 2× when the stationary operand is float32. This is the same `onezero` flag family as `MatMulSparse`'s `is_lhs_onezero` / `is_weight_onezero`.

> **NOTE —** `is_stationary_onezero` is a *performance hint*, not a correctness flag: it tells the engine the stationary tile is binary so it can skip multiplies. A reimplementer may set it `False` for a correct (slower) result; setting it `True` on a non-binary tile is silently wrong.

### Transpose sub-ops

The bodies intern `weight_transposed` / `intermediate_weight` / `intermediate_weight_transposed` / `ifmap_transposed` / `transposed_sub_filter` / `transposed_sub_img` — both the filter and the ifmap may be pre-transposed into the `(partition = C_in·H_REP)`-major SBUF layout `nc_matmul` demands, via `nc_transpose` (PE-array transpose, lowers to `nc_matmul` with `is_transpose=True`) or the DVE/PF software transposes (`tiled_dve_transpose_10` = swap last-2, `tiled_dve_transpose_210` = reverse-3, `tiled_pf_transpose` = partition-fold). **The `0f1b`/`fb01` layout choice (§1) exists precisely to minimise how many of these transposes are needed.**

### Reconstructed per-kernel op trace

The op *set* below is read off the binary; the *sequence* is [INFERRED] from the twin's `Pcinh_default` flow, for the reason given in §3.

```c
// PROLOGUE (both kernels)
1. unpack padding/stride/rhs_dilation; canonicalize_filter_shape (dilate f_H,f_W)
2. compute H_out,W_out; assert c_in/c_out/N  (c_in_assert/c_out_assert/batch_assert)
3. H_REP = replication_factor(canonical_H_f, C_in, dil_h)
   filter_partition_dim = C_in * H_REP ; assert H_filter <= 128 ; tile every dim
4. allocate SBUF (image_sbuf/filter_sbuf/results_accumulation_sbuf, PSUM banks)
   memset filter buffer to 0
// MAIN NEST (per §3 bgc/c_tile/n_outer/h_outer)
5. LOAD ifmap : dma_copy strided scatter -> img_buffer (im2col-by-DMA), h_coord clamp
6. LOAD filter: memset + dma_copy -> filter_buffer at partition h_rep*C_in*dil
               [0f1b ONLY: this load is PREFETCHED a tile ahead — §5]
7. (optional) nc_transpose / tiled_pf_transpose ifmap+filter into partition-major
8. COMPUTE  : for wf in W_f, for tiles:
                nc_matmul(out_psum, stationary=filter_buffer.ap, moving=img_buffer.ap)
                accumulating into the same PSUM bank across wf taps & H_OUTER
9. (optional) MM-REDUCE: nc_matmul(reduced, stationary=mm_reduce_weight(all-ones),
                moving=partials, is_stationary_onezero=True)  // sum partial windows
10. DRAIN     : tensor_copy(temp_sbuf <- out_psum, engine=vector_engine)
11. ACCUMULATE: tensor_tensor(results_accumulation_sbuf += temp, nl.add)
// EPILOGUE
12. (optional) transpose results to the named output layout (01fb / 01bf)
13. dma_copy results_accumulation_sbuf -> out_ref ; return out_ref
```

---

## 5. The Two Variants and "experimental_1"

### What separates them

Same convolution, same `nc_matmul` core. The split is on three axes.

**(A) Memory layout (§1).** `0f1b/0i1o/01fb` is height-major (filter-height window contiguous; ideal to stream onto the `H_REP` packing, but `C_in` not adjacent to partitions → a transpose may be needed). `fb01/io01/01bf` is feature(C)-major (the `rep_nhwc` layout; `C_in` already at the partition axis → minimal transpose; the layout the production `Pcinh` sibling uses). Both emit a `01xx` output.

**(B) Prefetch strategy.** Only the `0f1b` docstring carries the line "Prefetches filter", and only the `0f1b` body interns the double-buffer vocabulary — every token below is **absent** from `fb01`:

```text
a0_filter_local / a0_filter_local_prefetch
a0_img_local / a0_img_local_prefetch / a0_img_local_prefetch_neg
filter_local_prefetch
img_local_prefetch / img_local_prefetch_raw / img_local_pref_hacknoinit
prefetch_load_if / prefetch_load_ip
PREFETCH_TILE_SIZE
W_filter_prefetch_inner / W_filter_prefetch_outer
```

It issues the next filter (and image) DMA one loop iteration ahead into a second SBUF buffer (`a0_*` = axis-0 prefetch buffer) so the matmul of tile `t` overlaps the load of tile `t+1`. `img_local_pref_hacknoinit` is a buffer reused without re-zeroing — an experimental micro-opt. The `fb01` variant has none of these tokens → it is the **synchronous baseline** (load, then matmul, no overlap).

The trade-off: `0f1b` spends extra SBUF (double buffer) + a transpose to **buy** load/compute overlap on the height-major layout; `fb01` spends no extra SBUF and no transpose but stalls the PE array on each filter/ifmap DMA.

**(C) Debug instrumentation.** Only `0f1b` emits the runtime print (verbatim, `__pyx_k_Running_conv2d_pbp_0f1b…` @ `0x1be000`):

```text
Running conv2d_pbp_0f1b_0i1o_01fb_experimental_1, img_ref.shape: {..},
  filter_ref.shape: {..}, out_ref.shape: {..}
```

plus the `H_out`/`W_out` sanity f-strings — a hallmark of a hand-tuned experimental path still under bring-up.

### Why "experimental_1"

- The `_experimental_1` suffix + the standalone `Experimental kernel for convolutions` docstring mark these as the first iteration of a partition-replication + prefetch conv that was not promoted to production. No `_2` successor string exists in the `.so`.
- The readable migration module `private_nkl/conv.py` is banner-tagged `MIGRATION STATUS: Migrated from old NKI FE (_private_kernels/conv.py)` / `KNOWN PERFORMANCE REGRESSIONS.` and ships the depthwise + `rep_nhwc_Pcinh` + column-packing siblings — but **not** pbp. So pbp was left behind in the old compiled FE (this `.so`), never carried into the new readable FE: an abandoned / pre-production cut.
- `img_local_pref_hacknoinit` ("hack","noinit") is a self-documented rough edge: prefetch tuning was in progress when the kernel was frozen.

### Never auto-matched — explicit-select only

The conv-op → NKI-kernel auto-selector is `TransformConvOp` (class `TransformConvOp`, pass "Match certain convolutions and lower them to NKI kernels"; see [Conv Device-Lowering](conv-device-lowering.md)). A grep of the four package binaries that reference any conv kernel name resolves the status:

| Binary | Role | pbp present? | Sibling `rep_nhwc_Pcinh` present? |
|---|---|---|---|
| `TransformConvOp.…so` | auto-match selector | **NO** | yes (`conv2d_dw…`, `conv2d_column_packing`, `conv2d_depthwise_f01b_o01i_bf01`, `conv2d_co*` listed) |
| `InlineNKIKernels.…so` | explicit-named-kernel inline | yes — as `__pyx_v_conv2d_pbp_*` local bindings | yes |
| `BirCodeGenLoop.…so` | BIR code generation | yes (both names) | yes |
| `conv.…so` (`_private_kernels`) | the kernel bodies | yes | yes |

> **NOTE —** the discriminating evidence: `conv2d_pbp` is **absent from `TransformConvOp`'s string table** while the auto-matched siblings (`conv2d_column_packing`, `conv2d_depthwise_f01b_o01i_bf01`, `conv2d_co*`) are present there. The pbp names surface only as `__pyx_v_*` **local variable** bindings in `InlineNKIKernels` (the explicit-kernel inline path) and in `BirCodeGenLoop`. A kernel the selector never names cannot be auto-selected; it is reachable only when the caller asks for it by name. This is consistent with `_experimental_1` / "Experimental kernel for convolutions".

---

## 6. Confidence Ledger

| Claim | Confidence | Evidence |
|---|---|---|
| Both kernel names + symbol addrs (`0x95340`/`0x2487e`, `0xb9bc0`/`0x29d03`); bodies inlined into `__pyx_pw` | CERTAIN | `readelf -sW` |
| All three layout token-groups per kernel | CERTAIN | docstring `kernel_*_desc` strings @ `0x1bdb20`/`0x1bdbc0` |
| Layout-glyph → axis decode | CERTAIN (glyph map) / HIGH (per-kernel shape) | docstrings + `fb01` token-identity with twin |
| Full signature + module docstring + `H_out`/`W_out` formula | CERTAIN | `__pyx_k_` kwarg set + `.rodata` f-string |
| Guard/assertion message set | CERTAIN | verbatim `.rodata` (all 10 grepped) |
| Tiling vars (`H_REP`, `H_REP_NUM_TILES`, `C/COUT/K0/K1/WF/MM_REDUCE_NUM_TILES`) | CERTAIN | interned in pbp `.so` pool |
| `H_REP` packing = `C_in × H_REP`; tile nest; weight-stationary; im2col-by-AP; `mm_reduce_weight` reduction | HIGH | `.so` vocabulary + twin `Pcinh_default` algorithm |
| `nc_matmul` partition≤128 / moving-free≤512; `is_stationary_onezero` 2× | HIGH | stub + twin call sites |
| `0f1b` "Prefetches filter" + runtime print; `fb01` has neither | CERTAIN | only `0f1b` interns the prefetch tokens + print |
| Never auto-matched (absent from `TransformConvOp`) | HIGH | grep: pbp absent from selector, present only as inline locals |
| Literal name `filter_partition_dim` inside pbp bodies | MEDIUM | string count 0 in pbp `.so`; name lives in twin `conv.py` L1408 |
| Exact intra-body **call order**; which tensors get transposed per layout; exact tile-size constants | NOT statically recoverable | Cython-3 `mstate` string-indirection; runtime-computed values |

---

## Related Components

| Name | Relationship |
|---|---|
| `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh_default` | the readable twin (`conv.py` L1104) — same `Pcinh` packing, depthwise, **is** auto-matched |
| `TransformConvOp` | the conv-op auto-selector that does **not** list pbp |
| `InlineNKIKernels` | the explicit-named-kernel inline path where pbp surfaces as a local |
| `MatMulSparse` (`is_lhs_onezero`/`is_weight_onezero`) | same `onezero` matmul-reduction flag family |

## Cross-References

- [Conv Device-Lowering and Variant Selection](conv-device-lowering.md) — the `TransformConvOp` selector; confirms pbp is never auto-matched
- [private_nkl Depthwise Conv Kernels](depthwise-conv.md) — the readable `Pcinh` / dw / column-packing siblings and the shared helpers
- [Experimental & Pre-Prod NKI Kernels](experimental-kernels.md) — the family of `_experimental_*` kernels and their pre-production status
- [NeuronCodegen Tiling Internals](neuroncodegen-tiling.md) — the tile-nest / SBUF-PSUM mechanics these kernels echo
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition / 512-free / PSUM-bank model the `nc_matmul` contract assumes
