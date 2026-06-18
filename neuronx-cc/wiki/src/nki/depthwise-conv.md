# private_nkl Depthwise Conv Kernels

> *All line numbers on this page apply to `neuronxcc/private_nkl/conv.py` in `neuronx_cc-2.24.5133.0+58f8de22` (cp310/cp311/cp312 wheels carry byte-identical sources). The file is a binary-derived wheel artifact: readable `.py`, no compiled twin. Other versions will differ.*

## Abstract

The depthwise convolution family in `private_nkl/conv.py` is the **no-PE-array** conv path. Where a dense `conv2d` lowers to `im2col`-as-matmul and drives the systolic PE array through `nisa.nc_matmul`, the four depthwise kernels documented here compute the entire convolution on the Vector/Pool engines with two intrinsics only: `nisa.tensor_tensor(op=nl.multiply)` followed by `nisa.tensor_reduce(op=nl.add, axis=2)`. Channels map 1:1 onto the 128 SBUF partitions, and the per-partition multiply/reduce is exactly what depthwise needs — each output channel depends on one input channel, so there is no cross-channel contraction for a systolic array to perform. The array is not merely unused; it is the *wrong tool*, because its strength is summing across the partition/contraction axis.

The "im2col" is **virtual**: it is never copied into a matrix. It is constructed from overlapping access patterns (`.ap()`) — a sliding window with unit outer stride over output positions and an inner walk over the `W_f` kernel taps — so the same SBUF bytes are read at offset-shifted strides. A stride-0 axis broadcasts each filter tap across every output position. The page covers four functions: two 1D workhorses (`conv1d_depthwise_default`, `conv1d_depthwise_f_packing`), the 1D dispatcher that picks between them (`conv1d_depthwise_bf01_oi01_bf01`), and the 2D kernel (`conv2d_depthwise_f01b_o01i_bf01`) that adds LHS (input) dilation via a zero-fill-and-scatter and an SBUF-budgeted H-tile sliding window.

These kernels are externally-named lowering targets — the C++/MLIR frontend (see [Conv Device Lowering](conv-device-lowering.md), 6.8.1) emits the call by name; there is no in-tree Python caller. The contrast page is dense `conv2d` (6.8.3), the PE-array path. The relevant compute substrate is the [Pool engine](../arch/pool-engine.md) (Part 1.09) and the [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) — the 128-partition layout is the reason channels saturate partitions with no replication.

For reimplementation, the contract is:

- **The no-PE-array compute core** — `tensor_tensor(multiply)` + `tensor_reduce(add)`, the exact access patterns that realize a virtual sliding-window im2col without a copy, and the proof that no `nc_matmul` exists in the path.
- **The 1D dispatch + f-packing strategy** — how output positions are packed so each vector op stays ~512 wide.
- **The 2D LHS-dilation realization** — `calc_dilated_dimension`, the zero-fill + strided scatter DMA, and the dilation-row bookkeeping in the H sliding window.
- **The name-encoding / layout decode** — `bf01`/`oi01`/`f01b`/`o01i` dim orders and the `f01b` channel-first/batch-last choice that makes a single C-tile DMA gather one batch element.

| | |
|---|---|
| **Source** | `neuronxcc/private_nkl/conv.py` (4259 lines; depthwise family L24–1094) |
| **Dispatcher (1D entry)** | `conv1d_depthwise_bf01_oi01_bf01` (L682–754) |
| **1D impls** | `conv1d_depthwise_default` (L121–410) · `conv1d_depthwise_f_packing` (L413–679) |
| **2D entry** | `conv2d_depthwise_f01b_o01i_bf01` (L764–1094) |
| **Compute intrinsics** | `nisa.tensor_tensor(op=nl.multiply)`, `nisa.tensor_reduce(op=nl.add, axis=2)` |
| **PE-array intrinsics in path** | **none** — `nc_matmul` first appears at L1767 (rep_nhwc sibling) |
| **Partition mapping** | `C_TILE_SIZE = 128`; one channel per partition, no height replication |
| **Imports** | `nki.language as nl`, `nki.isa as nisa` (L11–12); `tiled_dve_transpose_210_newfe` (L17) |

---

## The No-PE-Array Proof

The single highest-value claim on this page is that the depthwise family never touches the systolic array. It is directly checkable in the source.

```text
$ rg -n 'nc_matmul' private_nkl/conv.py
1767:                          nisa.nc_matmul(      # conv2d_dw_..._rep_nhwc_Pcinh_default (L1104+)
2400:                nisa.nc_matmul(                # rep_nhwc_Pcinh_stride (L1889+)
3354:              nisa.nc_matmul(                  # column_packing (L2937+)
```

Every `nc_matmul` site is at L1767 or later — past the close of `conv2d_depthwise_f01b_o01i_bf01` at L1094. Inside the depthwise range L24–1094 the only compute intrinsics are:

```text
251, 357, 587, 634, 1015 : nisa.tensor_tensor(..., op=nl.multiply)
276, 381, 594, 641, 1046 : nisa.tensor_reduce(..., op=nl.add, axis=2)
```

`nisa.transpose` (the PE-array transpose) is likewise absent; the only transpose used is the DVE-engine `tiled_dve_transpose_210_newfe` (L799), an SBUF-to-SBUF layout op, not a contraction. (CONFIRMED — both greps run against the shipped wheel source.)

> **QUIRK — depthwise abandons the wave-1 K-replication trick on purpose.** An earlier experimental depthwise (the wave-1 `replication_factor` strategy, L55–76) stacked `r` channel-copies down the 128 partitions so `r` taps could be pushed through the PE array in one pass. The production family here keeps **one** channel per partition and never calls `replication_factor` inside L121–1094 (CONFIRMED by absence). `replication_factor` and `tile_with_stride` survive in the file only because the dense/`rep_nhwc` siblings (L1104+) still consume them. For depthwise, the partitions are already saturated by channels — there is nothing to replicate, and the direct elementwise path avoids the transpose/replication overhead entirely.

The math the path computes is, for every channel `c`, output position `w`, and tap `k`:

```text
out[c, w] = sum_{k=0..K-1}  img_window[c, w, k] * filter[c, k]
```

realized as a `tensor_tensor` product into a `prod_buffer` then a `tensor_reduce` over the tap axis. Because `c` indexes SBUF partitions and the ops run per-partition independently, this is exactly depthwise (no cross-channel sum). The dense framing of im2col-as-matmul is replaced by **im2col-as-elementwise-multiply-then-axis-reduce**.

---

## Name Encoding & Layout Decode

The function-name suffixes encode the dim order of the (input ; filter ; output) tensors. The letters are decoded from the shape-unpack lines and the `.ap()` stride patterns. (STRONG — reconstructed from asserts + access patterns, not a docstring table.)

| Letter | Meaning |
|---|---|
| `b` | batch (N) |
| `f` | feature / channel (C) |
| `0` | spatial dim 0 (H) |
| `1` | spatial dim 1 (W) |
| `o` | filter out-channel (C_out) |
| `i` | filter in-channel-per-group (C_in/grp) |

| Kernel | Tensor | Suffix | Shape | Anchor |
|---|---|---|---|---|
| `conv1d_*_bf01_oi01_bf01` | img | `bf01` | `(N, C_in, H=1, W)` | L710 |
| | filter | `oi01` | `(C_out, C_in=1, H_f=1, W_f)` | L711 |
| | out | `bf01` | `(N, C_out, H=1, W_out)` | L712 |
| `conv2d_depthwise_f01b_o01i_bf01` | img | `f01b` | `(C, H, W, N)` | L802 |
| | filter | `o01i` | `(C_out, H_f, W_f, C_in=1)` | L803 |
| | out | `bf01` | `(N, C_out, H_out, W_out)` | L804 |

The **depthwise invariant** is asserted, not assumed. 1D: the filter `i` slot is 1 (`oi01`). 2D (L807–808):

```c
assert in_C == k_FO and _C_out == k_FO    // in_C == C_out == filter-out-channels
assert in_N == _N and 1 == k_FI           // exactly one in-channel per group
```

so each output channel depends on exactly one input channel — the textbook `feature_group_count == C_in` case.

> **NOTE — why the 2D image is `f01b` (channel-first, batch-last).** The DMA load at L959–966 strides the image columns by `in_N` (batch is the *unit* HBM stride). With N innermost, one C-tile DMA gathers a single batch element across H and W by stepping `in_N` per pixel. At compute time the dilated buffer's unit-stride col walk then samples consecutive pixels of that one batch element. The layout is chosen to make the gather a single strided DMA. (STRONG — read from the L959–966 stride pattern.)

---

## conv1d_depthwise_default

### Purpose

1D depthwise where the **whole** `W_out` row plus all `W_f` taps fit in one `tensor_tensor`/`tensor_reduce` pair (the free axis is `W_out * W_f`). The dispatcher routes here when the entire output row packs into a single ~512-wide vector step.

### Algorithm

```c
function conv1d_depthwise_default(img, filter, out, padding, ...):   // L121
    W_padding_l, W_padding_r = padding[1]                 // 1D: only W padding
    N, C_in, _, W   = img.shape
    C_out, _, _, W_f = filter.shape
    _, _, _, W_out  = out.shape
    image_size = W_padding_l + W + W_padding_r             // padded row length
    needs_padding = W_padding_l > 0 or W_padding_r > 0
    C_TILE_SIZE = 128
    C_NUM_TILES = div_ceil(C_in, 128)                      // L24 ceil-div
    can_batch_all = (C_in % 128 == 0)
    prod_size = W_out * W_f                                // flat (W_out, W_f) product axis

    // --- SPMD channel-tile sharding across cores (L154-173) ---
    pid = nl.program_id(0); num_progs = nl.num_programs()  // see SPMD model page
    tiles_per_core = div_ceil(C_NUM_TILES, num_progs)
    start_tile = pid * tiles_per_core
    end_tile   = min(start_tile + tiles_per_core, C_NUM_TILES)
    if end_tile - start_tile <= 0: return out              // idle-core early-out
    // core owning the last tile peels off the partial (<128-channel) tail
    has_partial = (not can_batch_all) and (end_tile == C_NUM_TILES)
    c_start = start_tile * 128

    // --- PART 1: full 128-channel tiles, fully batched (L181-311) ---
    filter_full  = sbuf(128, tiles * W_f)                  // loaded once, one DMA
    dma_copy(filter_full, filter strided [W_f,128]/[128*W_f,tiles]/[1,W_f],
             offset = c_start * W_f)                       // L194-211
    for i_n in affine_range(N):
        if needs_padding: memset(img_full region) = 0      // L216-223
        dma_copy(img_full[:, Wpad_l : Wpad_l+W], img ...)  // ONE DMA, all tiles, L226-243
        memset(out_full) = 0                               // L246
        for c_tile in affine_range(my_num_full_tiles):
            // VIRTUAL im2col: sliding window via overlapping access patterns
            tensor_tensor(                                  // L251
              dst   = prod_buffer_full[(prod_size,128),(1,prod_size)],
              data1 = img_full   [(flat,128),(1,W_out),(1,W_f)]  off c_tile*image_size,
              data2 = filter_full[(flat,128),(0,W_out),(1,W_f)]  off c_tile*W_f,
              op    = nl.multiply)
              // outer (1,W_out): each output pos starts one element later -> the slide
              // inner (1,W_f):   walks the W_f taps
              // data2 (0,W_out): stride-0 broadcasts the same W_f taps to every output pos
              // => prod[c,w,k] = img[c, w+k] * filt[c, k]
            tensor_reduce(                                  // L276
              dst  = out_full[(flat,128),(1,W_out)]  off c_tile*W_out,
              data = prod_buffer_full[(prod_size,128),(W_f,W_out),(1,W_f)],
              op = nl.add, axis = 2)
              // => out[c,w] = sum_{k=0..W_f-1} prod[c,w,k]
        dma_copy(out, out_full strided, off i_n*C_out*W_out + c_start*W_out)  // L294-311

    // --- PART 2: partial (<128-channel) tail tile (L316-408) ---
    // same algorithm, C_LAST_TILE_SIZE partitions, NOT batched (single tile),
    // c_offset = (C_NUM_TILES-1)*128
```

### Considerations

The `data1` access pattern `[(flat,128),(1,W_out),(1,W_f)]` is the entire trick: the outer index strides by 1 per output position and the inner index covers `0..W_f-1`, so two overlapping reads of the **same** SBUF row produce a `(W_out × W_f)` im2col view with no copy. The `data2` filter pattern is identical except the `W_out` axis has stride 0, broadcasting each tap.

> **NOTE — stride and dilation are accepted but unused in 1D `_default`.** `stride`, `rhs_dilation`, and `lhs_dilation` are in the signature but the body's W slide is unit-stride and the dispatcher applies no dilation either. (CONFIRMED in body; INFERRED that dilated 1D is routed to a different upstream kernel, since neither the body nor the dispatcher handles it.)

---

## conv1d_depthwise_f_packing

### Purpose

1D depthwise for **larger** outputs. A single `tensor_tensor` over all of `W_out` would make the free axis `W_out * W_f` wide and blow past the ~512-wide vector-engine sweet spot. `_f_packing` instead processes `W_out` in chunks of `flattend_input_img_f` output positions, so each vector op's free axis stays ~512.

### Algorithm

```c
function conv1d_depthwise_f_packing(img, filter, out, ...):   // L413
    flattend_input_img_f = div_ceil(512, W_f)              // L468: positions packed per step
    // free-axis width per step = flattend_input_img_f * W_f ~= 512
    // small W_f (e.g. 3) packs ~171 positions; large W_f packs fewer.

    // same SPMD sharding + full/partial split as _default (L446-465)
    filter_full = sbuf(128, tiles, W_f)                    // 3D-shaped buffers now
    img_full    = sbuf(128, tiles, image_size)
    out_full    = sbuf(128, tiles, W_out)
    prod_buffer = sbuf(128, flattend_input_img_f, W_f)     // L489: hoisted, reused per step

    dma_copy(filter_full, ...)                             // PHASE 1: one strided DMA, L494-520
    for i_n in affine_range(N):                            // PHASE 2: per-batch compute
        // memset-pad + img DMA into img_full[:,:,Wpad_l:Wpad_l+W]; memset out_full=0
        for c_tile in affine_range(my_num_full_tiles):
            for base_i_out in range(0, W_out, flattend_input_img_f):  // PYTHON range -> unrolled
                num_valid = min(flattend_input_img_f, W_out - base_i_out)  // tail clamp
                tensor_tensor(                             // L587
                  dst   = prod_buffer[:, :num_valid, :W_f],
                  data1 = img_full[(flat,128),(1,num_valid),(1,W_f)]  off c_tile*image_size + base_i_out,
                  data2 = filter_full[(flat,128),(0,num_valid),(1,W_f)] off c_tile*W_f,
                  op    = nl.multiply)
                tensor_reduce(                             // L594
                  dst  = out_full[(flat,128),(1,num_valid)] off c_tile*W_out + base_i_out,
                  data = prod_buffer[:, :num_valid, :W_f],
                  op = nl.add, axis = 2)
        // partial-tile chunked loop, writes out_last (L610-646)
    dma_copy(out, out_full ...)                            // PHASE 3 store, L651-677
```

### Considerations

The math identity is identical to `_default`: `prod[c, base+w', k] = img[c, base+w'+k] * filt[c, k]`, then `out[c, base+w'] = sum_k prod`. The **only** difference is the outer Python `base_i_out` chunk loop that caps each vector op at `flattend_input_img_f` output positions.

> **GOTCHA — `prod_buffer_last` is re-allocated per batch element.** In the partial-tile path the per-step product buffer is allocated **inside** the `i_n` loop (L611), whereas the full-tile `prod_buffer` is hoisted out (L489). Harmless under NKI SBUF allocation but inconsistent — a minor smell, not a correctness bug. (CONFIRMED.)

---

## conv1d_depthwise_bf01_oi01_bf01 (Dispatcher)

### Purpose

The externally-named 1D entry point. It allocates the output HBM tensor, asserts 1D-ness, computes the f-packing tile count, and dispatches to `_default` (single pack) or `_f_packing` (multiple packs).

### Algorithm

```c
function conv1d_depthwise_bf01_oi01_bf01(img, filter, padding, ..., lnc=1, out_shape):  // L682
    assert out_shape is not None                          // L708
    N, C_in, H, W   = img.shape
    C_out, _, H_f, W_f = filter.shape
    _, _, K0, K1    = out_shape                            // K1 == W_out
    out = nl.ndarray((N,C_out,K0,K1), dtype=img.dtype, buffer=nl.shared_hbm)  // L713
    assert H == 1 and H_f == 1 and K0 == 1                 // L716: true 1D
    assert W_f < 512                                       // L717: small window
    flattend_input_img_f    = div_ceil(512, W_f)
    flattend_input_img_size = div_ceil(K1, flattend_input_img_f)
    if flattend_input_img_size == 1:                       // L723: whole row fits one pack
        return conv1d_depthwise_default(img, filter, out, ...)
    else:
        return conv1d_depthwise_f_packing(img, filter, out, ...)
```

> **NOTE — `lnc` is accepted for API compatibility only.** The docstring (L705–706) states LNC sharding is handled at the outer kernel level, not in these helpers. The `lnc` parameter is threaded into neither dispatch branch. (CONFIRMED.) No in-tree Python caller exists; the lowering selects this entry by name (STRONG — searched `neuronxcc/`, none found; the C++/MLIR frontend emits the call).

---

## conv2d_depthwise_f01b_o01i_bf01

### Purpose

2D depthwise with full **LHS (input/transposed) dilation**, H/W padding, and an optional input transpose. The compute core is the same per-partition multiply+reduce, but now wrapped in (1) an explicit dilated-image *scatter* that bakes the à-trous spacing into the buffer, and (2) an H-tile sliding window bounded by a 16 KB SBUF budget.

### Preconditions

```c
assert W_padding_l >= 0 and W_padding_r >= 0 and H_padding_l >= 0 and H_padding_r >= 0  // L792
assert stride == (1,1) or stride is None                  // L795
assert rhs_dilation == (1,1) or rhs_dilation is None       // L796: "rhs_dilation not supported
                                                           //        with lhs_dilation"
if in_perm == (1, 0, 3, 2):                                // L798: optional DVE transpose
    img = tiled_dve_transpose_210_newfe(img, is_intermediate=True)   // SBUF->SBUF, not PE
in_C, in_H, in_W, in_N = img.shape                         // f01b
k_FO, k_H, k_W, k_FI   = filter.shape                      // o01i
assert in_C == k_FO and _C_out == k_FO                     // L807 depthwise
assert in_N == _N and 1 == k_FI                            // L808 depthwise
```

This kernel handles **LHS dilation only** — RHS (kernel/à-trous) dilation is explicitly rejected at L796.

### Dilation & Tiling Setup

```c
dil_H = calc_dilated_dimension(in_H, dil_H_lhs) + H_padding_l + H_padding_r   // L821
dil_W = calc_dilated_dimension(in_W, dil_W_lhs) + W_padding_l + W_padding_r   // L822
// calc_dilated_dimension(d, dil) = (d-1)*(dil-1) + d   (L29-31): inserts dil-1 zeros
//   between adjacent samples. d=5,dil=2 -> 9  (X.X.X.X.X). The FULL padded+dilated extent.

C_TILE_SIZE = min(in_C, 128); C_NUM_TILES = div_ceil(in_C, C_TILE_SIZE)       // L830-831
N_TILE_SIZE = 1; N_NUM_TILES = in_N                        // one batch element at a time
dtype_size = 4                                             // L839 -- HARDCODED (see GOTCHA)
max_sbuf_bytes = 16 * 1024
h_tile_count = div_ceil(16384, dil_W * N_TILE_SIZE * dtype_size)              // L841
max_h_tile_size = max(min(in_H,
                          calc_inverse_dilated_dimension(h_tile_count, dil_H_lhs) - 1),
                      k_H)                                  // L842: never below kernel height
```

> **GOTCHA — `dtype_size` is hardcoded to 4 (fp32) regardless of `img.dtype`.** L839 pins `dtype_size = 4` even though `dtype = img.dtype` is captured at L810 and `sizeinbytes` is imported (L15, used only by the rep_nhwc siblings). For bf16/fp16 inputs this **over**-estimates bytes — the H-tiles are smaller than they need to be, so it is *conservative and safe but suboptimal*. A hypothetical >4-byte dtype would **under**-budget and could overflow the 16 KB SBUF window. Flagged as a latent perf/correctness smell. (CONFIRMED hardcode; INFERRED impact.)

### Algorithm — H Sliding Window

```c
for c_tile in range(C_NUM_TILES):                          // L847, Python-unrolled
    c_current = C_TILE_SIZE (or C_LAST on last); c_start = c_tile*C_TILE_SIZE
    dma_copy(filter_sbuf, filter strided [k_H*k_W*k_FI, c_current]/[1, kernel_size],
             offset = c_start*kernel_size)                 // L855-864: taps contiguous per channel
    for n_idx in range(N_NUM_TILES):                       // one batch element
        outer_row = 0
        while outer_row < out_H:                           // L876: H sliding window
            // --- dilation-row bookkeeping (L876-916) ---
            is_last_row = outer_row > out_H - H_padding_r
            h_tile_size = max_h_tile_size if not is_last_row else out_H - outer_row
            org_image_index = max(0, calc_inverse_dilated_dimension(
                                       outer_row+1-H_padding_l, dil_H_lhs) - 1)
            // detect rows that land on an INSERTED-ZERO position (not a real sample):
            is_dilation_row = ((outer_row+1+dil_H_lhs-1-H_padding_l) % dil_H_lhs != 0)
                              and outer_row >= H_padding_l
            dilation_offset = 0; load_h_size = h_tile_size
            if is_dilation_row:
                next_non_dilated = calc_dilated_dimension(org_image_index+2, dil_H_lhs)-1 + H_padding_l
                dilation_offset  = next_non_dilated - outer_row    // shift to next real row
                org_image_index += 1
                load_h_size = max(1, h_tile_size - 1)
            if org_image_index + h_tile_size >= in_H:              // clamp at image bottom
                load_h_size = h_tile_size - (org_image_index + h_tile_size - in_H)
            // top-pad / bottom-pad placement, skip_copy when a whole tile is padding
            i_padding = (top-pad rows) + dilation_offset
            window_size = max(0, (dilated_h_size - k_H) + 1)       // #output rows this tile yields
            // clamp window_size to out_H; if <= 0 advance outer_row by 1 and continue

            // --- load + scatter into dilated buffer (L949-995) ---
            image_sbuf   = sbuf(C_TILE, max_h_tile_size*in_W)
            dilated_image = sbuf(C_TILE, max_dilated_h*dil_W)
            if load_h_size > 0 and c_current > 0:
                dma_copy(image_sbuf, img f01b strided                       // L949-968
                         [[in_H*in_W*in_N, c_current],   // channel -> partition
                          [in_W*in_N, load_h_size],      // row stride (skips other N)
                          [in_N, in_W]],                 // col stride = in_N  (f01b!)
                         offset = c_start*in_H*in_W*in_N + org_image_index*in_W*in_N + n_start)
            memset(dilated_image[:c_current, :dilated_h_size*dil_W]) = 0     // L973
            if not skip_copy and load_h_size > 0:
                dma_copy(dilated_image, image_sbuf,                          // L977-995 SBUF->SBUF
                         dst strided [[max_dilated_h*dil_W, c_current],
                                      [dil_H_lhs*dil_W, load_h_size],  // leave dil-1 zero rows
                                      [dil_W_lhs, in_W]],              // leave dil-1 zero cols
                         dst_offset = i_padding*dil_W + W_padding_l)
                // => writes dense image onto the dilation grid; zeros untouched.
                //    LHS dilation realized by a STRIDED COPY, not arithmetic.

            // --- compute: per-tap multiply + batched reduce (L1000-1065) ---
            memset(results_sbuf[:c_current, :window_size*out_W]) = 0
            for h_offset in affine_range(window_size):     // each output row in this tile
                memset(product_sbuf[:c_current, :out_W*kernel_size]) = 0
                for kh in range(k_H):
                  for kw in range(k_W):                    // all taps, Python-unrolled
                    kernel_idx = kh*k_W + kw; img_h = h_offset+kh; img_w_base = kw
                    tensor_tensor(                          // L1015
                      dst   = product_sbuf[(out_W*kernel_size,c_current),(kernel_size,out_W)] off kernel_idx,
                      data1 = filter_sbuf[(kernel_size,c_current),(0,out_W)] off kernel_idx,  // stride-0 broadcast
                      data2 = dilated_image[(max_dilated_h*dil_W,c_current),(1,out_W)] off img_h*dil_W + img_w_base,
                      op    = nl.multiply)
                tensor_reduce(                              // L1046: ONE reduce, ALL k_H*k_W taps
                  dst  = results_sbuf[(window_size*out_W,c_current),(1,out_W)] off h_offset*out_W,
                  data = product_sbuf[(out_W*kernel_size,c_current),(kernel_size,out_W),(1,kernel_size)],
                  op = nl.add, axis = 2)
            dma_copy(out, results_sbuf, off n_idx*_C_out*out_H*out_W + c_start*out_H*out_W + outer_row*out_W)  // L1072-1090
            outer_row += window_size                        // L1092: advance H window
```

### Considerations

The dilation handling is the heart of this kernel. The H window walks output rows and distinguishes **real-sample** rows from **inserted-zero** rows (`is_dilation_row`), computes how many original image rows to DMA (`load_h_size`), where to place them inside the zeroed dilated buffer (`i_padding` = top-pad + `dilation_offset`), and how many valid output rows (`window_size`) the current dilated tile yields. (STRONG/CONFIRMED — read directly L876–916.)

> **QUIRK — dilation is baked into the buffer, so the compute needs no per-tap stride math.** Because `dilated_image` already carries the à-trous spacing (the scatter left `dil-1` zeros between samples), the `data2` access pattern at L1015 uses a **unit** col stride `[1, out_W]` and still naturally samples every `dil_W`-th original pixel for the dilated kernel footprint. The dilation cost is paid once, in the scatter DMA; the inner multiply loop is dilation-oblivious. This is why LHS dilation is a strided *copy*, not arithmetic at compute time.

The product layout places `kernel_size` taps contiguously per output position W (element `(c,w,k)` at `w*kernel_size + k`), so the final `tensor_reduce(axis=2)` collapses **all** `k_H*k_W` taps in one op — unlike the per-tap multiply loop, the reduce is a single intrinsic per output row. (CONFIRMED L1046–1065.)

---

## nisa/nl Call-Sequence Summary

The complete intrinsic vocabulary used anywhere in L24–1094 (all from `nki.isa as nisa` / `nki.language as nl`, L11–12):

| Intrinsic | Role | Kernels |
|---|---|---|
| `nl.program_id` / `nl.num_programs` | SPMD core identity | 1D only |
| `nl.ndarray(buffer=nl.sbuf\|nl.shared_hbm)` | buffer alloc | all |
| `nl.affine_range` / Python `range` | loop nests | all |
| `nisa.dma_copy` | HBM↔SBUF and SBUF→SBUF moves | all |
| `nisa.memset` | padding / accumulator init | all |
| `nisa.tensor_tensor(op=nl.multiply)` | elementwise product (virtual im2col) | all |
| `nisa.tensor_reduce(op=nl.add, axis=2)` | tap-axis sum | all |
| `tiled_dve_transpose_210_newfe` | optional input transpose (DVE engine) | conv2d only |
| **`nisa.nc_matmul`** | **NOT USED** — PE array | — (first at L1767) |
| **`nisa.transpose`** | **NOT USED** — PE-array transpose | — |

Canonical emit order per kernel:

```text
_default       : [filter DMA]x1; per-N: { memset-pad?, img DMA, memset-out,
                 per c_tile:(tensor_tensor, tensor_reduce), out DMA } (+ partial tail)
_f_packing     : [filter DMA]x1; per-N: { pad/img/out setup,
                 per c_tile per base_i_out chunk:(tensor_tensor, tensor_reduce), out DMA } (+ tail)
bf01_oi01_bf01 : alloc out-hbm; assert; dispatch -> one of the above
conv2d_f01b... : [optional DVE transpose]; per c_tile:{ filter DMA; per N:
                 H-window while-loop { img DMA, memset+scatter-DMA dilate,
                 per h_offset:(k_H*k_W tensor_tensor, 1 tensor_reduce), out DMA } }
```

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the wheel source.

1. **No PE array / no `nc_matmul` in the depthwise path.** Re-greped: `nc_matmul` appears only at L1767, L2400, L3354 — all past the depthwise close at L1094. `nisa.transpose` absent. **Holds — CONFIRMED.** This is the highest-risk claim and it is the most directly checkable; the depthwise functions end at L1094 (next `def` is `conv2d_dw_..._rep_nhwc` at L1104) and contain zero matmul sites.

2. **The compute is `tensor_tensor(multiply)` then `tensor_reduce(add, axis=2)`.** Verified all five `tensor_tensor` sites (L251/357/587/634/1015) carry `op=nl.multiply` and all five `tensor_reduce` sites (L276/381/594/641/1046) carry `op=nl.add, axis=2`. **Holds — CONFIRMED.**

3. **Virtual im2col via overlapping access patterns, no copy.** The `data1` patterns (`[(flat,128),(1,W_out),(1,W_f)]` 1D; `[(...),(1,out_W)]` 2D) and stride-0 `data2` filter broadcast are read verbatim from L256–271, L569–585, L1023–1036. No im2col matrix is materialized. **Holds — CONFIRMED.**

4. **`dtype_size = 4` is hardcoded.** L839 reads `dtype_size = 4` literally, with `dtype = img.dtype` captured but unused for the budget and `sizeinbytes` imported but unused here. **Holds — CONFIRMED.** The over/under-budget *consequence* is marked INFERRED.

5. **2D handles LHS dilation only, via zero-fill + strided scatter.** L796 rejects RHS dilation; L821–822 build the dilated extents with `calc_dilated_dimension`; L973 zeros the buffer; L977–995 scatters with strides `dil_H_lhs*dil_W` (rows) / `dil_W_lhs` (cols). **Holds — CONFIRMED.**

Re-verification ceiling: the page is grounded entirely in the shipped `.py`. What is **not** independently verified is the upstream selector's exact dispatch into these named entry points (owned by 6.8.1, the C++/MLIR frontend) — that the frontend emits `conv1d_depthwise_bf01_oi01_bf01` vs. a sibling is STRONG (no Python caller found) but the selection rule lives outside this file. The INFERRED claims (dilated-1D routing elsewhere; `dtype_size` over/under-budget consequences) are marked in place.

---

## Related Components

| Name | Relationship |
|---|---|
| `conv2d_dw_..._rep_nhwc_Pcinh_*` (L1104+) | The PE-array siblings — *do* call `nc_matmul`; consume `replication_factor`/`tile_with_stride` |
| `conv2d_column_packing*` (L2661+) | Column-packed dense conv variants (PE-array) |
| `tiled_dve_transpose_210_newfe` | DVE-engine transpose imported from `private_nkl/transpose.py`, used for the 2D `in_perm` rotate |

## Cross-References

- [Conv Device Lowering](conv-device-lowering.md) — 6.8.1, the selector that picks this kernel by name
- [Pool Engine](../arch/pool-engine.md) — Part 1.09, the Vector/Pool substrate the multiply+reduce runs on
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition layout that maps channels 1:1 and the 16 KB H-tile budget
- [SPMD Programming Model](spmd-programming-model.md) — `program_id`/`num_programs` channel-tile sharding used by the 1D kernels
- [nki.isa COMPUTE Intrinsics & Validators](isa-compute-intrinsics.md) — `tensor_tensor` / `tensor_reduce` semantics and access-pattern validation
- [BirCodeGenLoop Compute Codegens](bircodegen-compute.md) — how `tensor_tensor`/`tensor_reduce` lower to BIR on the beta3 path
- [Production Kernel Inventory](production-kernel-inventory.md) — where `private_nkl/conv.py` sits in the three-tree kernel story
