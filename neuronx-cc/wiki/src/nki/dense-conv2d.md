# private_nkl Dense Conv2D — NHWC-Replication & Column-Packing

> *All symbols, line numbers, and shapes on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, file `neuronxcc/private_nkl/conv.py` (4259 lines). The cp310 / cp311 / cp312 wheel copies are byte-identical; cited line numbers are the cp310 copy. `conv.py` ships as readable Python inside the wheel — it is a binary-derived artifact, and every claim below was re-verified against that source plus the `neuronxcc-stubs` `.pyi`.*

## Abstract

This is the **PE-array dense convolution path**: the `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` family lowers a 2D convolution to a sequence of `nisa.nc_matmul` calls on the 128×128 systolic array. It is the structural opposite of its sibling [`depthwise-conv`](depthwise-conv.md) (§6.8.2), which emits **zero** `nc_matmul` and does each per-channel multiply on the scalar/vector engines: the depthwise region of `conv.py` (L121–L1104) contains no `nc_matmul` token at all, while this family contains exactly three call sites — one per variant, at L1767, L2400, L3354. Where a dense conv has a real cross-channel `C_in` contraction, the PE array earns its keep; where depthwise has a `C×C` diagonal, it does not.

The defining trick is **`Pcinh`** — *Partition axis carries (C_in × H-replication)*. The 128 PE partition rows are the matmul's contraction dimension. A dense conv only needs `C_in` of them for the channel reduction, so the kernel stacks `H_REP` vertically-adjacent **filter rows** into the spare partition headroom: partition `p` decomposes as `p = h_rep·C_in + cin`, and one `nc_matmul` contracts over `C_in × H_REP` rows at once. The filter is the stationary operand; the image is staged into SBUF as an **im2col-by-DMA-scatter** (each `h_rep` copy lands in its own partition band) and read as the moving operand. The full conv reduction over `(C_in, H_f, W_f)` decomposes across three accumulation levels: `C_in × H_REP` folded into one matmul (partition contraction), `W_f` accumulated in PSUM (repeated matmuls, same bank), and the remaining `H_f / H_REP = H_OUTER` filter-row tiles accumulated in SBUF via `tensor_tensor` add.

Three variants share this engine. **`_default`** (L1104) is the stride-1 fast path: pre-allocate all buffers once, load image once per K0-DMA tile, 4D store. **`_stride`** (L1889) is the strided path: per-tile re-staging, a `use_3d` (N==1) collapse, and the stride encoded directly in the moving access-pattern strides. **`_column_packing`** (L2937) flattens the output tiles into a "combo table" bucketed by K0-DMA tile and fills up to 8 PSUM banks in parallel. The dispatcher `conv2d_dw_...Pcinh` (L2578) picks `_default` if `h_stride==1 and w_stride==1`, else `_stride`. Separately, the column-packing engine is the dense workhorse for a *depthwise-as-dense-diagonal* hybrid: the [§6.8 column-packing wrappers](conv-device-lowering.md) feed it a grouped conv as a full dense matmul producing a `BGC×BGC` block, then extract the diagonal with a single strided DMA.

For reimplementation, the contract is:

- The **`Pcinh` partition layout** — `filter_partition_dim = C_in·H_REP`, `H_REP = replication_factor(canonical_H_f, C_in, dil)`, partition `p = h_rep·C_in + cin`, and *why* this is the conv's `(C_in + H-row)` reduction folded into one PE pass.
- The **im2col-by-DMA-scatter** — `cin_dst_start = h_rep·C_in` lands each replicated image copy in its own partition band; the free axis carries `(K0_out_rows, K1_out_cols, N)`.
- The **dest-first `nc_matmul`** call convention (stationary=filter, moving=im2col), the three-level reduction, and the PSUM/`out_sb` two-stage accumulation.
- The **`_default` / `_stride` / `_column_packing`** differences: tiling, buffer lifetime, stride-in-AP, and the combo table.

| | |
|---|---|
| **Dispatcher** | `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` (L2578) |
| **Variants** | `_default` (L1104), `_stride` (L1889), `_column_packing` (L2937) |
| **PE intrinsic** | `nisa.nc_matmul` — call sites L1767, L2400, L3354 (one per variant) |
| **Contraction dim** | `filter_partition_dim = C_in · H_REP` (L1408, L2149, L3102) |
| **Rep factor** | `replication_factor(rep_from, rep_to, dilation)` (L55) — largest `i≤rep_from` with `i·rep_to ≤ 128` and `i%dil==0` |
| **Moving-free cap** | `K0_COMP·K1_TILE·N_COMP ≤ 512` (L1357 / L2132) |
| **PSUM banks** | `NUM_PSUM_BANKS = 8` (fp32) |
| **IR level** | NKI Python trace → `penguin.ir` → BIR (3-layer stack) |

---

## Family Geometry and the Layout-Name Decode

The family name `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` is a fully structured token string; decoding it is the fastest way to read the kernel.

```text
conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh
   │     │   │    │    │    │    │     └─ Partition = (C_in × H-replication)   ← the key contrast
   │     │   │    │    │    │    └─ data tensor native layout NHWC
   │     │   │    │    │    └─ REPLICATION strategy (depthwise ABANDONED this)
   │     │   │    │    └─ output perm token group  (0=H_out=K0, 1=W_out=K1, b=N, f=C_out)
   │     │   │    └─ filter perm token group  (i=C_in, o=C_out, 0=H_f, 1=W_f)
   │     │   └─ image perm token group  (f=feature/C, b=batch/N, 0=H, 1=W)
   │     └─ "depthwise-weight"-style family tag — NOT depthwise math here
   └─ 2D conv
```

> **GOTCHA —** the `dw` ("depthwise-weight") in the name is a family naming convention, **not** a statement that the math is depthwise. This family is dense (cross-channel) conv. The genuinely-depthwise kernels are `conv1d_depthwise_*` / `conv2d_depthwise_*` (L121–L764), documented in [§6.8.2](depthwise-conv.md). The two are distinguished not by the name but by the binary fact that the depthwise region emits no `nc_matmul`.

All variants massage the caller's physical tensors into three **canonical** layouts before any matmul (the perm-dispatch blocks at L1146–L1284 in `_default`, byte-identical in `_stride` at L1934–L2052):

| Tensor | Canonical shape | Token group |
|---|---|---|
| Filter | `(C_in, H_f, W_f, C_out)` | `io01` — i-major, o-last |
| Image | `(C_in, H, W, N)` | `fb01` — feature-major |
| Output | `(C_out, K0, K1, N)` — `K0=H_out`, `K1=W_out` | `01bf` |

`K0` and `K1` are the spatial output extents; they become the matmul **moving-free** axes. The closing transpose for `out_perm==(2,3,0,1)` (L1880) and the NCHW per-N store (`nchw_out`) are the only output-side branches that survive past the canonical form.

---

## The `Pcinh` Partition Layout — H-REP onto the Contraction Axis

### Purpose

The PE array contracts over its partition axis (up to 128 rows). A dense conv's natural contraction is over `C_in` input channels. When `C_in < 128`, the remaining partition rows are wasted — unless they carry something else that also reduces. `Pcinh` fills them with **filter rows**: stack `H_REP` vertically-adjacent rows of the filter onto the partition axis alongside `C_in`, so one matmul performs the `C_in`-channel reduction *and* an `H_REP`-row slice of the height reduction simultaneously.

### Algorithm

```c
// replication_factor — conv.py L55-77.  Models the partition-packing decision.
function replication_factor(rep_from /*=canonical_H_f*/, rep_to /*=C_in*/, dilation):
    max_rep = 0
    for i from rep_from down to 1:                 // L73 loop
        if i * rep_to <= 128 and i % dilation == 0:  // 128-partition cap + dilation divisibility
            max_rep = i
            break
    return max(max_rep, 1)                          // never below 1

// In each variant (L1291 / L2086 / L3013):
H_REP = replication_factor(canonical_H_f, C_in, rhs_dilation[0])
filter_partition_dim = C_in * H_REP                 // L1408 / L2149 / L3102  ← THE PE partition count

// H_REP tiling of the filter height (L1295-1304 _default):
if H_REP == 1:  H_REP_NUM_TILES = 1
else:           assert H_REP % rhs_dilation[0] == 0
                H_REP_NUM_TILES = H_REP // rhs_dilation[0]
// H_OUTER walks the remaining filter-row tiles: tile(canonical_H_f, H_REP)
```

Partition `p` decomposes as `p = h_rep·C_in + cin` with `cin ∈ [0,C_in)`, `h_rep ∈ [0,H_REP)`. Equivalently: `H_REP` filter rows are stacked on the partition axis beside the `C_in` channels, and one `nc_matmul` contracts over `(C_in × H_REP)` rows.

### Why this is the conv reduction

The conv output element is `out[c_out, k0, k1, n] = Σ_{cin, h, w} filter[cin,h,w,c_out] · image[cin, k0·s+h, k1·s+w, n]`. The `Σ_cin` and `H_REP` rows of `Σ_h` are exactly the partition dimension of one matmul. The full height reduction is split: `H_REP` rows ride inside one matmul, and the remaining `H_f / H_REP` row-tiles (`H_OUTER`) are summed afterward in SBUF. This is the "rep" that the depthwise family **abandoned**: with depthwise each `C_out` depends on a single `C_in`, so there is no cross-channel contraction to feed the PE array and stacking channels on the partition buys nothing.

> **NOTE —** `replication_factor` is monotone-greedy: it walks *down* from `canonical_H_f`, taking the largest legal `i`. So for `C_in=32`, `H_f=5`: the loop tries `5·32=160 > 128` (reject), then `4·32=128 ≤ 128` (accept) ⇒ `H_REP=4`, two `H_OUTER` tiles (rows {0..3}, {4}). For `C_in=128`, any `H_REP>1` exceeds 128 ⇒ `H_REP=1`, the partition is pure `C_in`, and the whole height reduction falls to `H_OUTER`.

### Dilation alignment

For `rhs_dilation[0] > 1` the filter band stride is multiplied by the dilation: the filter loader uses `dst_partition_start = h_rep·C_in·rhs_dilation[0]` (L1666), while the image loader uses `cin_dst_start = h_rep·C_in` (L1589). The `·rhs_dilation[0]` spacing places each dilated filter row onto exactly the partition slot the image's dilation-spaced row occupies, so the partition contraction pairs the correct `(filter row, image row)`. This is how dilated conv stays correct under `Pcinh`; `H_REP_NUM_TILES = H_REP // rhs_dilation[0]` keeps the count consistent.

---

## The `nc_matmul` Emit — Stationary Filter, Moving im2col

### Purpose

This is the PE-array GEMM at the heart of every variant. The filter is the **stationary** operand (held in the array), the im2col image window is the **moving** operand streamed through it, and the PSUM bank is the accumulator.

### The dest-first call convention

```c
// _default compute nest (L1767). Identical shape at _stride L2400, _column_packing L3354.
nisa.nc_matmul(
    psum_banks[h_idx][c_out_tile][:c_out_count, :free_dim_size],   // OUT = fp32 PSUM accumulator
    stationary = filter_buffer.ap(
        pattern=[[filter_free_dim, filter_partition_dim], [1, c_out_count]],
        offset=filter_offset),                  // (C_in·H_REP) × c_out_count
    moving = img_buffer.ap(
        pattern=img_pattern, offset=img_offset)) // (C_in·H_REP) × (k0·k1·n)
```

> **CORRECTION / signature note (STRONG) —** the public stub `neuronxcc-stubs/nki/isa/__init__.pyi` L915 declares `nc_matmul(stationary, moving, *, ...)` — `stationary` is the *first positional*, computing `stationary.T @ moving` (stationary free ≤ 128, moving free ≤ 512, partition ≤ 128). But `conv.py` calls it **dest-first**: the leading unnamed positional is the OUTPUT PSUM, then `stationary=` / `moving=` are passed by keyword. The front-end binds a dest-taking form whose real implementation is compiled into the backend `.so`; the call sites + stub together fix the semantics as `PSUM += stationary.T @ moving`. Do not assume the stub's positional order at a `conv.py` call site.

### Operand layouts

Both operands share the **same** leading partition stride pair `[<free_stride>, filter_partition_dim]`, which is what proves the contraction axis is identical:

- **Stationary (filter)** `[[filter_free_dim, filter_partition_dim], [1, c_out_count]]` → shape `(C_in·H_REP, c_out_count)`: partition = contraction, free = output channels (≤ 128 PE columns). `filter_offset` selects filter column `wf` and the `c_out` tile (`wf·C_out + w·C_out + c_out_start`).
- **Moving (im2col)** feature-major pattern (L1756): `[[img_tile_free, filter_partition_dim], [h_stride·img_buffer_W·N_DMA, k0_count], [w_stride·N_DMA, k1_count], [1, n_count]]` → shape `(C_in·H_REP, k0·k1·n)`: same partition contraction, free = staged output positions. `img_offset` slides the window by filter column via `w_offset = wf·rhs_dilation[1]`. The `nchw_in` form (L1747) drops the N stride and broadcasts `[0, n_count]`.

### Result semantics and the three-level reduction

```text
PSUM[c_out, (k0,k1,n)] += Σ_{cin,h_rep} filter[(cin,h_rep), c_out] · img[(cin,h_rep), (k0,k1,n)]

  C_in  + H_REP filter rows  ─→ folded into ONE matmul (partition contraction)
  W_f                        ─→ accumulated in PSUM   (repeated matmuls, same bank — L1741 w/wf loop)
  H_f / H_REP  (= H_OUTER)   ─→ accumulated in SBUF    out_sb += temp  (tensor_tensor add, L1798)
```

One `nc_matmul` does the full `(C_in × H_REP)` reduction for one filter column `wf`. The inner `w/wf` loop re-issues into the **same** PSUM bank (the PE array accumulates in place across the `W_f` taps). After all `wf` columns, the bank is copied to SBUF (`tensor_copy` PSUM→`temp_sbufs`, L1789) and `tensor_tensor`-added into `out_sb` across all `h_idx` (L1798–L1807), folding the `H_OUTER` filter-row tiles. `out_sb` is memset to 0 only at `psum_batch_idx==0` (L1712), so it persists across PSUM batches.

---

## The im2col-by-DMA-Scatter — Image Staging

### Purpose

The moving operand is an im2col reformat of the image, but it is never materialized as an explicit `[C_in·H_REP × patch]` matrix. Instead a DMA-scatter copies image rows into partition bands, and the matmul's moving access-pattern reads the patches by stride.

### Algorithm

```c
// _default image loader (L1571-1652). One H_OUTER tile per h_idx.
memset(img_buffers[h_idx], 0)                       // L1571 — halo/padding partitions read as zero
for h_rep in range(H_REP):                          // L1577
    h            = h_outer*H_OUTER_TILE_SIZES + h_rep
    k0_base      = k0_dma_tile*K0_TILE_SIZES*h_stride_intra_tile
    h_coord_base = h + k0_base - H_padding_l         // top input row, padded
    i_k0_min = max(0, -h_coord_base)                 // clamp window to [0,H) — skip halo rows
    i_k0_max = min(K0_TILE_SIZES, H - h_coord_base)
    cin_dst_start   = h_rep * C_in                   // L1589 — THE REPLICATION-BAND SCATTER
    dst_free_offset = i_k0_min*(W_total*N_DMA) + W_l_pos_padding*N_DMA   // past left zero-pad

    if nchw_in:                                      // L1591 — per-image i_n loop, 4D dst AP
        for i_n in range(n_dma_count): dma_copy(...) // src NCHW [H*W; i_k0; W; 1]
    else:                                            // L1609 — single feature-major DMA
        dma_copy(dst=img_buffers[h_idx][cin band, free], src=(C_in,H,W,N) window)
```

The crux is `cin_dst_start = h_rep·C_in` (L1589): the same image is DMA-copied `H_REP` times, each copy shifted by one filter row and landing in its own partition band `[h_rep·C_in : h_rep·C_in + C_in)`. So partition `p=(h_rep,cin)` sees the input row aligned to filter row `h_rep`. The `C_in` channels land on consecutive **partitions** (partition stride = full row stride `img_tile_free_dim`); the `(i_k0, W, N)` triple lands on the **free** axis. That is the im2col: free = staged spatial window × batch, partition = the contraction.

> **QUIRK —** the "FIXED VERSION" docstring (L1121, "Restructured loops to load data once per K0_DMA tile") records a real restructure: the loop nest is `n_outer → k0_dma_tile → psum_batch → (per h_idx) load`, so each K0-DMA strip of the image is DMA'd once and reused across the `psum_batch`/`c_out` matmuls instead of reloaded per matmul. This is the central residency-vs-traffic trade that `_stride` deliberately does *not* take (see below).

A second DMA (L1628–L1652) handles **negative W-padding**: the valid `W_trimmed` columns are copied out of `img_buffers` into `img_trimmed_buffers`, dropping over-padded columns. When there is no negative W-pad, `img_trimmed_buffers == img_buffers` and the copy is skipped.

### Filter staging (mirror layout)

```c
// _default filter loader (L1658-1678) — lays out partition=(h_rep,cin), free=(wf,c_out)
memset(filter_buffers[h_idx], 0)                            // L1658
for wf_tile, h_rep in [0, H_REP_NUM_TILES):
    h = h_outer*H_REP_NUM_TILES + h_rep
    if h < H_f:
        dst_partition_start = h_rep * C_in * rhs_dilation[0] // L1666 — dilation-spaced band
        dst_free_offset     = wf_tile*WF_TILE*C_out
        dma_copy(_weight[row h, cols wf_start..] -> filter_buffers band)
```

The filter band layout `partition=(h_rep,cin), free=(wf,c_out)` exactly matches the image's partition layout, so the matmul's partition contraction `Σ_{cin,h_rep}` *is* the conv's combined `C_in + H-row` reduction.

---

## `_default` Variant — the Stride-1 Fast Path

### Tiling and budget

`_default` (L1104) carries the full tiling machinery. `COUT` is tiled by 128 (L1309, PE stationary-free / output-partition). The matmul moving-free budget starts at 512; `K0` is DMA-tiled with `tile_with_stride(K0·h_stride, tile_size, h_stride)` capped to `MAX_K0_DMA_TILES = 8` (L1338–L1340). The free-dim product `K0_COMP·K1_TILE·N_COMP` must be ≤ 512 (the PSUM/moving-free cap), and L1357–L1372 shrinks `K0_COMP`, then `K1_TILE`, then `N_COMP` in that order until it fits. An SBUF-byte heuristic (L1420–L1448) estimates per-H-tile bytes against ~20 MB and combines with `NUM_PSUM_BANKS=8 / COUT_NUM_TILES` to choose `PSUM_H_OUTER_BATCH` (how many `H_OUTER` tiles are resident and matmul'd before draining PSUM).

### Buffer lifetime

All buffers are **pre-allocated once outside the loops** (L1456–L1530): `out_sb[c_out_tile]`, `img_buffers[h_idx]`, `filter_buffers[h_idx]`, `psum_banks[h_idx][c_out_tile]` (fp32 PSUM), `temp_sbufs[h_idx][c_out_tile]`. This trades higher SBUF residency for fewer DMA reloads — the "load once per K0-DMA tile" promise.

### Store

On the last `psum_batch`: `nchw_out` does a per-N extract via `tensor_copy` into `output_slice_3d` then DMA to `(N,C_out,K0,K1)` (L1819); the canonical `(C_out,K0,K1,N)` target is a single DMA (3D if `K0_COMP==1`, else 4D, L1839). `out_perm==(2,3,0,1)` appends a closing `transpose_to_last_dim` (L1880).

---

## `_stride` Variant — Stride in the Moving Access-Pattern

`_stride` (L1889) is the `h_stride>1` / `w_stride>1` path. The conv math is identical (`Pcinh`, im2col, `nc_matmul` GEMM, two-level reduction); what differs is window addressing, tiling, and buffer lifetime.

> **GOTCHA —** the dispatcher (L2600) asserts `stride==(1,1)` for NCHW (`in_perm==(0,1,2,3)`) and NHWC (`in_perm==(0,3,1,2)`) image layouts — "unsupported perm with strides". So `_stride` in practice only ever runs with feature-major (already-transposed) image layouts. A reimplementer wiring strided NHWC directly will hit this assert.

| Facet | `_default` | `_stride` |
|---|---|---|
| Tiling | SBUF-byte budget + cascading shrink + `PSUM_BATCH` | straight-line pick (L2106): `MAX_FREE_DIM=512`, `N_TILE=1` if `use_3d` else `min(N,32)`, then `K1_TILE`, then `K0_TILE` |
| `N==1` collapse | none (always carries N axis) | `use_3d = (N==1)` (L2078) — drops one AP dim everywhere |
| Image buffer span | `K0_TILE·W_total` (contiguous out-rows) | `img_h_span = (K0_TILE-1)·h_stride + canonical_H_f` (L2155) |
| Buffer lifetime | pre-alloc once outside loops | alloc fresh **inside** `(k0,k1,n)×h_outer` loops, memset each (L2199) |
| Stride encoding | `h_stride`/`w_stride` factors vanish (==1) | strides live in the moving AP: `[h_stride·W_padded, k0_count]`, `[w_stride, k1_count]` (L2384) |

### The strided staging + read

The strided image buffer must hold every input row touched by `K0_TILE` output rows whose receptive fields are `h_stride` apart, plus `canonical_H_f-1` filter-window rows — hence `img_h_span = (K0_TILE-1)·h_stride + canonical_H_f`. The DMA loads a **contiguous** vertical window (it is *not* strided in the DMA itself); the stride is applied later by the matmul moving AP:

```c
// _stride matmul moving AP, use_3d (L2384)
img_pattern = [[img_f_dim,         filter_p_dim],   // partition = C_in·H_REP (contraction)
               [h_stride*W_padded,  k0_count],       // K0 moving-step = h_stride rows  <<<
               [w_stride,           k1_count]]       // K1 moving-step = w_stride cols  <<<
// (!use_3d adds [1,n_count] and carries *N_TILE on the K0/K1 strides)
```

The PE reads only every `h_stride`-th row / `w_stride`-th col of the densely-staged window. This is the entire reason `_stride` exists as a separate function: `_default`'s moving AP carries the same `h_stride`/`w_stride` factors (L1750/L1758) but with value 1 they collapse; here they are >1 and the image buffer was sized (`img_h_span`) to keep the strided reads in-bounds.

---

## `_column_packing` Variant — the Combo Table and 8-Bank Packing

`_column_packing` (L2937) is the NHWC-rep family's column-packed inner kernel. Its conv math is the identical `Pcinh` PE-array GEMM; only the tile bookkeeping and store layout differ. It is **layout-fixed** — the docstring (L2954) and asserts (L2967) require `stride==(1,1)`, `in_perm==(3,0,1,2)` (f01b), `kern_perm==(3,0,1,2)` (i01o), and `out_perm ∈ {(0,1,2,3) NCHW, (3,0,1,2) f01b}`. With those fixed, there is **no transpose stage**: `_weight = filter_ref`, `_ifmap = img_ref` (L2997).

### The combo table

```c
// L3110-3180. Flat list of work items instead of nested compute loops.
for each (k0c, k1t, nt, ct) output sub-tile:
    combo = { k0_start, k0_count, k1_start, k1_count, n_start_local, n_count,
              c_out_start, c_out_count, k0_dma_tile, k0_offset_in_dma,
              actual_free = k0_count*k1_count*n_count,
              slot = (k0c*K1_NUM_TILES + k1t)*N_COMP_NUM_TILES + nt }   // L3144
    combos_per_k0_dma[combo.k0_dma_tile].append(combo)   // bucket by image DMA tile
COMBOS_PER_BATCH = min(NUM_PSUM_BANKS=8, max_combos_per_k0)
```

`slot` is the packing index: it packs every `(k0c,k1t,nt)` output column-group into a distinct region of **one wide** `out_sb` free axis (`OUT_SB_FREE_DIM = K0_COMP_NUM·K1_NUM·N_COMP_NUM·PSUM_F_DIM`, L3186), with `C_out` on the partition. Combos are bucketed by which K0-DMA image tile they need, so the image is DMA'd once per K0 tile and *all* its combos compute together (the docstring "FIX: process one K0 DMA tile at a time", L2961).

### Compute and store

```c
// L3234-3398
for n_outer: memset(out_sb, 0)
  for h_outer:
      load filter once (Pcinh band scatter, dst_partition = h_rep*C_in*rhs_dilation[0])
      for k0t in K0 DMA tiles:
          load image once (im2col scatter, cin_dst_start = h_rep*C_in, halo-clamped)
          for batch_idx (≤8 combos):
              for bank in batch: memset(psum_banks[bank][:c_out,:free], 0)
              for wf in affine_range(W_f):          // accumulate width taps in PSUM
                  for bank in batch:
                      img_w_offset = (neg_pad_w_offset + wf*rhs_dilation[1])*N_DMA  // neg W-pad in offset
                      nc_matmul(psum_banks[bank], stationary=filter slice, moving=img im2col)
              for bank in batch:                     // drain — fold H_OUTER reduction
                  tensor_tensor(out_sb[slot region], reshape(bank), out_sb[slot region], nl.add)
store: for each combo, DMA out_sb[slot*PSUM_F_DIM] -> HBM (per-N nchw, or f01b 3D/4D)
```

Up to 8 combos matmul into 8 PSUM banks in parallel per batch (one bank per combo) — that is the "packing": many output columns share one filter load and one image DMA, get matmul'd into 8 banks at once, then folded into the single packed `out_sb`. Negative W-pad is folded directly into the moving offset (`neg_pad_w_offset`, L3349) — **no trim buffer**, unlike `_default`.

> **QUIRK — "column packing" carries two distinct senses.** (a) The *combo/column packing inside the engine*: the matmul free dim `(K0_comp·K1·N)` is packed into ≤512-wide PSUM columns and assigned to banks. (b) The *diagonal packing* added by the §6.8 wrappers: a depthwise conv is run as a **dense** `C_out×C_out` matmul so all channels' columns are produced at once, sitting on the diagonal of the `BGC×BGC` block, then unpacked by a single strided DMA with partition stride `(BGC+1)·K0·K1` (L2911) or `K0·K1·BGC+1` (L3824/L4235). Sense (a) is this engine; sense (b) is the wrapper layer.

---

## Column-Packing = Dense `Pcinh` Run for a Diagonal

The wrappers `conv2d_column_packing_1` (L2661), `conv2d_column_packing` (L3495), and `conv2d_column_packing_io10` (L3879) — covered in [§6.8.1](conv-device-lowering.md) as the selector layer — call into this same dense engine to solve a *depthwise* problem. The insight: a grouped conv with group size 1 (`feature_group_count == C_in == C_out`, or `batch_group_count == N == C_out`) is computed as a **full dense matmul** producing a `BGC×BGC` block, of which only the diagonal `[i,i,:,:]` is the wanted per-channel result.

```c
// conv2d_column_packing_1 diagonal extract (L2899-2911) — the "+1 stride" trick
// Element [i,i,:,:] of an (BGC,BGC,K0,K1) NCHW block is at offset i*(BGC+1)*K0*K1.
dma_copy(dst = reduced.ap([[K0*K1, bgc_actual], [1, K0*K1]], offset=0),         // (BGC, K0*K1)
         src = sparse_out_ref.ap([[(BGC_TILE_SIZE+1)*K0*K1, bgc_actual],         // partition stride
                                  [1, K0*K1]], offset=0))                        //   = (BGC+1)*K0*K1
```

`_column_packing_1` actually routes through the **dispatcher** `conv2d_dw_...Pcinh` (L2876) (so it picks `_default`/`_stride` for its dense block), whereas `conv2d_column_packing` (L3792) and `_io10` (L4203) call the `_column_packing` engine directly. The dense `BGC×BGC` matmul is wasteful by construction, but for these layouts it is faster/simpler than scattering a true grouped conv, and the diagonal extract is one strided DMA.

> **NOTE (INFERRED) —** the source never literally states "we discard the off-diagonal". The conclusion is forced by the `dsts_shapes = [[BGC, BGC, K0, K1]]` dense-block allocation (L2710) combined with the diagonal-only `(BGC+1)`-stride extraction (L2911). The math + the `+1` stride make it certain; the *rationale* (why pay for the dense block) is reconstructed, not transcribed.

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **"H_REP is stacked onto the *contraction* (partition) axis."** — CONFIRMED. `filter_partition_dim = C_in·H_REP` (L1408) is the **leading** stride pair element for *both* operands: `stationary` pattern `[[filter_free_dim, filter_partition_dim], …]` (L1770) and `moving` pattern `[[img_tile_free, filter_partition_dim], …]` (L1756). A shared leading partition stride is the matmul contraction by definition. Not the free axis (that is `c_out_count` / `k0·k1·n`).

2. **"This is the PE-array path; depthwise is not."** — CONFIRMED. `rg nc_matmul` over the depthwise region L121–L1104 returns **zero** hits; this family has exactly three (`L1767/L2400/L3354`). The intrinsic is `nisa.nc_matmul` (the PE-array op), not `nl.*`. Contrast with [§6.8.2](depthwise-conv.md) is structural, not stylistic.

3. **"`nc_matmul` is called dest-first."** — CONFIRMED at the call site (L1767: leading positional is `psum_banks[...]`, then `stationary=`/`moving=` keywords) but the *signature* is STRONG-not-CERTAIN: the stub (`.pyi` L915) declares `(stationary, moving, *, …)` keyword-only-after-two, the real impl is compiled `.so`. The dest-taking form is a front-end binding inferred from call-site + stub. Flagged in-text.

4. **"Column-packing = dense `Pcinh` for a diagonal."** — engine reuse CONFIRMED (`_column_packing` is called by L3792/L4203; `_1` routes the dispatcher at L2876); the *diagonal discard* is INFERRED from `dsts_shapes` + the `(BGC+1)` stride, marked so above. The `+1` diagonal-of-a-square stride is itself CONFIRMED (L2911/L3824/L4235).

5. **"Stride lives in the moving AP, not the DMA."** — CONFIRMED. `_stride` stages a contiguous `img_h_span` window (L2155) and the moving AP carries `[h_stride·W_padded, k0_count]` / `[w_stride, k1_count]` (L2384). The DMA load (L2288) reads a contiguous run; the PE does the subsampling.

**Re-verification ceiling.** Everything traceable in `conv.py` and the `.pyi` is CONFIRMED to the line. The two genuine ceilings: (a) the `nc_matmul` *internal* PSUM-accumulate semantics live in the backend `.so` and are inferred from call structure + stub doc; (b) the depthwise-as-dense *rationale* is reconstructed, though its mechanism (dense block + diagonal stride) is verbatim.

---

## Related Components

| Function | Lines | Relationship |
|---|---|---|
| `conv2d_dw_..._Pcinh` (dispatcher) | L2578 | Picks `_default`/`_stride` by stride; allocates out HBM |
| `conv2d_dw_..._Pcinh_default` | L1104 | Stride-1 fast path (this page) |
| `conv2d_dw_..._Pcinh_stride` | L1889 | Strided path (this page) |
| `conv2d_dw_..._Pcinh_column_packing` | L2937 | Combo-table 8-bank engine (this page) |
| `conv2d_column_packing{,_1,_io10}` | L2661/L3495/L3879 | Depthwise-as-dense diagonal wrappers ([§6.8.1](conv-device-lowering.md)) |
| `conv2d_depthwise_*` / `conv1d_depthwise_*` | L121–L764 | The NON-PE path — zero `nc_matmul` ([§6.8.2](depthwise-conv.md)) |
| `replication_factor` | L55 | The `Pcinh` partition-packing decision |

## Cross-References

- [Conv Device-Lowering and Variant Selection](conv-device-lowering.md) — §6.8.1, the selector that routes into this family and owns the column-packing wrappers.
- [private_nkl Depthwise Conv Kernels](depthwise-conv.md) — §6.8.2, the contrast case: zero `nc_matmul`, scalar/vector per-channel multiply.
- [PE Engine — the Systolic Matmul Array](../arch/pe-engine.md) — the 128×128 array `nc_matmul` targets; contraction = partition.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the 8 fp32 PSUM banks and SBUF residency budget this family schedules into.
- [PE Matmul Encoding — Dense / Sparse / MX & Quantize](../isa/pe-matmul-encoding.md) — the BIR-level encoding of the `nc_matmul` this kernel emits.
- [Worked Example A — a matmul end-to-end](../front/worked-example-matmul.md) — the bare `nc_matmul` (stationary/moving/PSUM) this conv builds on.
- [Conv Canonicalization](../hlo-opt/conv-canonicalization.md) — the HLO-level conv normalization upstream of NKI lowering.
