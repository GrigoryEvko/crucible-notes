# Scan / Reduce / Top-K Primitives

> *All source line numbers on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 canonical; cp311/cp312 carry byte-identical kernel sources). The three kernels are shipped as readable, Apache-licensed NKI-DSL Python under `nkilib/core/{cumsum,max,topk}/` — binary-derived wheel artifacts, citeable verbatim. Each also has a production Cython twin under `neuronxcc/nki/_private_kernels/` (`cumsum.cpython-310-…so`, the `topk/` subpackage); the readable copies are the algorithm of record and are documented below.*

## Abstract

Three nkilib kernels build the entire scan / reduce / top-K surface of the Neuron stack, and all three reduce to a single hardware idiom each. **`cumsum`** is an inclusive prefix sum that lowers to exactly **one** `nisa.tensor_tensor_scan` per 2048-wide free tile — no software log-step tree. **`cascaded_max`** is a global argmax (top-1 value *and* index) over a large vocabulary `V`, implemented as a **2-level fold-then-combine** reduction, not a recursive 8-ary max tree. **`rotational_topk`** is the top-K generalization for **large K (K > 8)**: it runs the `max8 → match_replace8(-inf)` "strike-out" loop over a *folded* `V`, then circulates per-stage winners across partitions with a **circulant block-diagonal rotation matrix** so every fold-chunk's candidates eventually reach every reduction stage.

The shared substrate is the DVE 8-wide search family — `max8` (descending top-8, [encoded in Part 2](../isa/dve-search-encoding.md)), `nc_find_index8` (first-occurrence argmax), and `nc_match_replace8` (fused strike-and-write-index). The same family powers the K ≤ 8 [`router_topk`](router-topk.md), which issues a *single* `max8`+`find_index8` with no strike-out; this page is the K > 8 sibling. `cumsum` is the odd one out: it is a *scan*, not a reduction, and uses the `tensor_tensor_scan` running-accumulator op instead.

The page documents, in order: **`cumsum`** (the one-instruction scan, the partition-parallel / free-sequential structure, the carry across `H > 2048` tiles); **`cascaded_max`** (the V→partition fold, the leaf reduce, the matmul-transpose combine, the grouped mask-select argmax); **`rotational_topk`** (the strike-out core, the three execution modes, the rotation tournament, the cost-model tile search, the optional sort); and the **`topk` method dispatch** — including a correction to the long-standing claim that `CASCADED` is unwired.

For reimplementation, the contract is:

- The **`cumsum` recurrence**: `out[i] = op1(op0(ones[i], state), data[i])` with `op0=mul, op1=add, in0=1` ⇒ inclusive prefix sum; partition-parallel, free-sequential, fp32 accumulation, last-column carry across free tiles.
- The **`cascaded_max` two levels**: `predicated_folded_load` (V across `n_stages` partition rows) → per-row `tensor_reduce(max)`+`nc_find_index8` → matmul-transpose onto the free axis → `_grouped_reduce_max` mask-select argmax.
- The **`rotational_topk` mechanism**: `topk_core` strike-out (`max8`→`nc_match_replace8(-inf)`) per stage over the growing free window, `nc_n_gather` for local→global index, and the `kron(I, circulant(shift-1))` rotation matmul that makes the tournament complete.
- The **`topk` dispatch**: the `SupportedTopkMethods` enum, the readable 2-key registry vs. the production 3-key `.so` registry, and the trivial / scanning / rotational mode selection.

| | |
|---|---|
| **`cumsum`** | `nkilib/core/cumsum/cumsum.py` (148 lines, `def cumsum` `@nki.jit` at `:30`) |
| **`cascaded_max`** | `nkilib/core/max/cascaded_max.py` (352 lines, `def cascaded_max` at `:31`) |
| **`rotational_topk`** | `nkilib/core/topk/rotational_topk.py` (521 lines, entry `topk` at `:398`) |
| **the meat** | `nkilib/core/topk/rotational_topk_utils.py` (959 lines: `topk_core` `:816`, `RotationalConstants` `:216`) |
| **shared fold/unfold** | `nkilib/core/max/cascaded_max_utils.py` (`predicated_folded_load` `:58`, `unfolded_store` `:155`) |
| **production twins** | `_private_kernels/cumsum.so`; `_private_kernels/topk/{rotational_topk,naive_scanning_topk,cascaded_2_stage_topk,topk_method_mapping}.so` |
| **DVE primitives** | `max8`, `nc_find_index8`, `nc_match_replace8`, `tensor_tensor_scan`, `nc_n_gather` (all in `neuronxcc/nki/isa/neuron_isa.cpython-310-…so`) |
| **HW constants** | `P_MAX=128`, `F_TILE_SIZE=2048` (cumsum); `dve_max_alus=8`, `topk_per_stage=8`, `fixed_dve_inst_overhead=144`, `max_free_dim=2^14`, `index_dtype=uint32` (`TopkHardwareParams`, utils `:45-69`) |

> **NOTE —** these are *binary-derived wheel artifacts*: `nkilib/core/**.py` ships as readable Apache-licensed Python inside the wheel, and the op names cited here (`max8`, `nc_match_replace8`, `tensor_tensor_scan`, …) are confirmed as live binary strings in `neuron_isa.cpython-310-…so`. The production-called copies are Cython `.so` twins; where the readable and compiled trees are known to diverge (the `topk` registry), it is flagged with a correction.

---

## cumsum — The Prefix-Sum Scan

### Purpose

`cumsum(x, axis=-1)` computes the inclusive cumulative sum along the last dimension of an HBM tensor (`:30-147`). It is the only *scan* among the three kernels — the running accumulator flows along the free axis rather than collapsing it. The docstring (`:38-48`) fixes the envelope: batch `B ≤ 2048`, hidden `H ≤ 8192`, 3-D sequence `S ≤ 10`; the result is the same shape and dtype as the input, with fp32 accumulation internally. The PyTorch reference (`cumsum_torch.py:33`) is literally `torch.cumsum(x, dim=axis)` — the kernel matches it, so it is **inclusive** (`out[0] = data[0]`).

### Entry Point

```text
cumsum(x, axis=-1)                              ── @nki.jit, cumsum.py:30
  ├─ kernel_assert(axis == rank-1)              ── last-dim only, :82
  ├─ x_2d = x.reshape(outer_dim, last_dim)      ── outer_dim = prod(shape[:-1]), :86-90
  ├─ y = ndarray(..., buffer=shared_hbm)        ── output on HBM, :93
  └─ for p_tile in TiledRange(outer_dim, 128):  ── partition tiles, :99
       memset(init_sb=0); memset(ones_sb=1.0)   ── :103,107
       for f_tile in sequential_range(num_f_tiles):  ── free tiles, MUST be sequential, :110
         dma_copy(load) → tensor_tensor_scan → dma_copy(store) → tensor_copy(carry)
```

### Algorithm

The entire cumulative sum is **one** hardware instruction, repeated once per `F_TILE_SIZE=2048` free tile. There is no Hillis-Steele / log-step tree in software.

```c
function cumsum(x, axis):                          // cumsum.py:30
    assert axis == rank - 1                         // last dim only, :82
    x_2d = x.reshape(outer_dim, last_dim)           // outer_dim = prod(shape[:-1]), :86-90
    y_2d = alloc_hbm(shape_2d)                       // :93-94
    num_f_tiles = ceil(last_dim / 2048)             // F_TILE_SIZE = 2048, :96

    for p_tile in TiledRange(outer_dim, 128):       // up to 128 rows per tile, :99
        init_sb = memset((128, 1), 0.0, fp32)        // running carry, fp32, :102-103
        ones_sb = memset((128, 2048), 1.0, fp32)     // the in0 multiplicand, :106-107

        for f in sequential_range(num_f_tiles):      // sequential — carry dependence, :110
            f_start = f * 2048; f_size = min(2048, last_dim - f_start)
            data_sb = dma_load(x_2d[p_tile, f_start:f_end])        // :117-120

            // result[i] = (ones[i] * state) + data[i] = state + data[i]
            tensor_tensor_scan(                                     // ONE HW op, :125-132
                dst   = result_sb,
                data0 = ones_sb,        // in0 = 1
                data1 = data_sb,        // in1 = the V-tile
                initial = init_sb,      // scan seed / running state
                op0 = multiply, op1 = add)

            dma_store(y_2d[p_tile, f_start:f_end], result_sb)      // :135-138

            if f + 1 < num_f_tiles:                                 // carry last col forward, :141-145
                tensor_copy(init_sb, result_sb[:, f_size - 1])
    return y
```

The scan recurrence implemented by `tensor_tensor_scan` is `out[i] = op1(op0(in0[i], state), in1[i])` with `state := out[i]`. Substituting `op0=mul, op1=add, in0=1, in1=data` collapses to `out[i] = state + data[i]` — the textbook inclusive prefix sum. The `initial` seed is `0` on the first free tile (`memset`, `:103`) and carries forward thereafter.

### Parallelism Structure

`cumsum` exploits three axes differently — this is the part a naive log-step reimplementation gets wrong:

- **Free (scan) axis** — a *single* DVE sweep. The silicon runs the running accumulator across the free axis natively; the scan axis comes from the access pattern, lowering to `TSCMode = TensorScan`. There is no software tree.
- **Partition axis** — *nothing to combine*. `cumsum` is per-row: each of up to 128 partitions is an independent sequence, run in lockstep. "Parallel scan" here means **128 independent HW scans**, not a cross-partition prefix.
- **Free-tile axis** (only when `H > 2048`) — software-**sequential**. The loop is `nl.sequential_range` (`:110`), and the last column of tile `f` is copied into `init_sb` (`:143-145`) as the seed for tile `f+1`. The carry dependence is why this loop *must not* be parallelized.

> **QUIRK —** `ones_sb` and `init_sb` are `fp32` while `data_sb`/`result_sb` keep `x.dtype` (`:106,116,124`). The scan accumulates in fp32 for stability, but the *store* is in input precision. The docstring (`:57`) warns of ~`1e-2` absolute error for `H > 5K` purely from fp32 accumulation — not a bug, a documented precision floor.

> **NOTE —** `P_MAX = 128` is a hard-coded Python `int` (`:25`), with a comment that `nl.tile_size.pmax` "returns non-int, compiler requires Python int for shapes." A reimplementation that passes the symbolic `pmax` into a shape tuple will fail shape inference; force a literal.

---

## cascaded_max — The Cascaded Max Reduction

### Purpose

`cascaded_max(input_tensor)` returns the **global top-1** value and its `uint32` index over the last (vocabulary) dimension (`:31-93`). Input is `[B, S, V]` or `[BxS, V]`; output is `[B, S, 1]` × 2. It is the large-`V` argmax: when `V` exceeds what a single DVE free dimension covers, the reduction is recast as a partition-parallel fold plus two combine levels. LNC sharding is LNC2 for `BxS > 1`, LNC1 for `BxS == 1` (`:55`, config `:147-149`).

### Entry Point

```text
cascaded_max(input_tensor)                      ── @nki.jit, cascaded_max.py:31
  ├─ config = CascadedMaxConfig(shape, dtype)   ── computes n_stages, stage_free_size, :76
  │    └─ _calculate_cascading_constants        ── n_stages = min(128//BxS, V//128), :163-177
  ├─ val, idx = cascaded_max_core(inp2d, config) ── the 2-level reduction, :77
  └─ dma_copy(val, idx → HBM with LNC offset)    ── program_id * BxS_size, :84-91
```

### Algorithm

`cascaded_max_core` (`:186-285`) is a **flat, two-level** reduction. The leaf level folds `V` into partition rows and reduces each row in parallel; the combine levels transpose those per-fold maxima onto the free axis and pick the winner with a mask-select argmax.

```c
function cascaded_max_core(inp, config):                  // cascaded_max.py:186
    n_stages   = config.n_stages                           // min(128//BxS, V//128), :172
    BxS_size   = config.per_lnc_BxS
    total_pdim = n_stages * BxS_size

    // ---- Level 0: fold V across n_stages partition rows ----
    values = predicated_folded_load(inp, fold_factor=n_stages)   // [total_pdim, chunk_size], :246

    // ---- Level 1: per-fold leaf reduce + local argmax ----
    value = tensor_reduce(maximum, values, axis=1)               // one max per fold-row, :256
    ind_buf = nc_find_index8(values, vals=value_broadcast8)      // 8 first-occurrences, :258
    //          only ind_buf[:,0] (the per-fold argmax) is used downstream

    // ---- local index -> global index ----
    ind_offset = _repeat(n_stages, stage_free_size, BxS_size)    // s * stage_size, iota, :260,333
    identity = memset(1.0); affine_select(diagonal)              // one-hot selector, :263-273
    ind_t   = nc_matmul(ind_buf_float, identity, is_transpose)   // local idx -> free axis, :277
    ind_shifted = tensor_tensor(ind_t, ind_offset, op=add)       // = global index, :280

    // ---- Level 2: combine across folds ----
    value_psum = nc_matmul(value, identity, is_transpose)        // fold maxima -> free axis, :283
    final_max, global_index =                                    // :284
        _grouped_reduce_max(value_psum, ind_shifted, fold_factor=BxS_size)
    return final_max, global_index
```

The `_grouped_reduce_max` combine (`:288-330`) is a **mask-select argmax**, not a second find-index call:

```c
function _grouped_reduce_max(input, index, fold_factor):   // cascaded_max.py:288
    reshape input -> [b, fold_factor, elts_per_fold]        // :316-317
    reduced_max  = tensor_reduce(maximum, input, axis=2)    // the global max, :324
    mask         = tensor_tensor(input == reduced_max_bcast) // uint8, which elt wins, :325-327
    masked_index = tensor_tensor(mask * index)              // keep only winner's index, :328
    final_index  = tensor_reduce(maximum, masked_index, axis=2) // pull surviving index, :329
    return reduced_max, final_index
```

### The Fold (Level 0)

`predicated_folded_load` (`cascaded_max_utils.py:58-152`) is the V→partition fold shared with `rotational_topk`. It lays a `[BxS, V]` HBM tensor into SBUF `[BxS_local * fold_factor, n_folded]` where `n_folded = ceil(V / fold_factor)`:

- **Fast path** (`:119-130`) — when `V == fold_factor * n_folded` (exact), a pure `reshape` + one `dma_copy`.
- **Predicated path** (`:132-152`) — when `V` is not divisible, a per-row two-segment DMA: the first `remainder` columns get `fold_factor` rows, the rest get `fold_factor - 1`. The buffer is pre-filled with `fill_value = -9948.0` (`:63`), the bf16 sentinel, so padding never wins a max.
- The asserted invariant (`:106-109`) is `batch_size_sharded * fold_factor <= 128` — the fold cannot exceed the partition dimension.

> **CORRECTION (O21-1) —** `cascaded_max` is **not** a recursive `log_8(N)`-deep 8-ary max tree, despite the "cascaded" name suggesting one. It is a **2-level fold+combine**: Level 0 folds `V` across `n_stages` partition rows (a DMA, fully parallel), Level 1 does one `tensor_reduce`+`nc_find_index8` per fold-row, and Level 2 transposes onto the free axis and runs one grouped mask-select argmax. The 8-wide `max8` silicon appears *only* at the leaf, via `nc_find_index8`. "Cascaded" names the fold-into-partitions plus the two combine levels — there is no recursion. A reimplementation built as a deep 8-ary tree will not match this code.

> **NOTE —** the older `neuronxcc/nki/_pre_prod_kernels/max/cascaded_max.py` (222 lines) is the same algorithm — the same fold → `tensor_reduce` → `find_index8` → matmul-transpose → grouped reduce — in its earlier value-returning form. The nkilib version refactors it behind `CascadedMaxConfig` and explicit access-pattern plumbing. Algorithmically equal; no correction needed.

---

## rotational_topk — The Rotational Top-K (K > 8)

### Purpose

`rotational_topk(inp, config)` finds the K largest elements (values *and* `uint32` global indices) along the last dimension for **large K** (`:54-222`). Its tested envelope (docstring `:86`) is `V` up to 151,936, `k` up to 2,048, batch up to 1,024. Where [`router_topk`](router-topk.md) handles `K ≤ 8` with a single `max8`, `rotational_topk` is the generalization: it runs a `max8 → match_replace8` strike-out loop, and — crucially — amortizes the cost of re-scanning a huge `V` by folding `V` across partitions and *rotating* per-stage winners between them.

### The Strike-Out Core

`topk_core` (`rotational_topk_utils.py:816-867`) is the heart shared by both the scanning and the rotational paths. It pulls the top-K of a `[BxS, V]` SBUF block by repeated 8-wide max + strike-out:

```c
function topk_core(data, k):                            // utils:816, data modified in-place
    n_fold = ceil(k / 8)                                 // topk_per_stage = 8, :833
    out_vals = alloc[BxS, k]; out_inds = alloc[BxS, k]
    for fold_idx in static_range(n_fold):                // :838
        if k % 8 != 0 and fold_idx == n_fold - 1:        // ragged last fold, :839
            max8(val_buf, data)                           // top-8 desc, :843
            nc_find_index8(ind_buf, data, val_buf)        // their indices, :844
            copy out_vals[:, k - rem :] <- val_buf[:rem]  // keep only k%8 cols, :847-852
            copy out_inds[:, k - rem :] <- ind_buf[:rem]
        else:                                             // full 8-wide fold, :854
            max8(out_vals[:, fold*8 : +8], data)          // next 8 largest, :857
            nc_match_replace8(                            // FUSED strike + write index, :859-865
                dst = data,                               //   overwrite winners with -inf
                dst_idx = out_inds[:, fold*8 : +8],       //   write their data positions
                data = data, vals = out_vals[:, fold*8:+8],
                imm = -inf)                               //   the strike-out sentinel
    return out_vals, out_inds
```

Each full fold does one `max8` (the next 8 largest) and one `nc_match_replace8`, which **fuses** writing the 8 winners' data positions into `dst_idx` with overwriting those 8 data slots with `-inf`, so the *next* `max8` yields the next 8. The ragged final fold (`k % 8 != 0`) cannot use the fused form — it falls back to a separate `max8` + `nc_find_index8` into temporaries and copies only `k % 8` columns. This is the strike-out idiom; the contrast with `router_topk` (one `max8`, no loop) is the K ≤ 8 / K > 8 boundary.

### Three Execution Modes

`rotational_topk` (`:111-222`) and `log_strategy` (`utils:415-438`) pick one of three modes:

| Mode | Trigger | Path | Source |
|---|---|---|---|
| **trivial** | `orig_k == vocab_size` | return `inp` + `iota` indices, no reduction | `:134-151` |
| **scanning** | `n_stages == 1` | `naive_scanning_topk` — `topk_core` over the whole `V`, no rotation | `:154-172`, `utils:762` |
| **rotational** | `n_stages > 1` | `_topk_rotated_core` per BxS-tile | `:174-222`, `:225` |

> **GOTCHA —** `naive_scanning_topk` (`utils:762-813`) is the scanning mode but is **not** a distinct algorithm — it tiles `BxS` by `P_MAX`, loads each tile, calls the *same* `topk_core`, and stores. The only difference from rotational is that scanning runs the strike-out over the entire `V` instead of a folded `V/n_stages`. `cost_estimate` (`utils:145-160`) puts the scanning cost at `ceil(k/8) * 2 * (V + 144)` DVE cycles — dominated by re-scanning all `V` on each of the `k/8` passes. That cost is exactly what rotation amortizes.

### The Rotation Mechanism

`_topk_rotated_core` (`:225-362`) folds `V` across `n_stages` partition rows (so each `max8` scans only `V/n_stages`), then circulates per-stage local winners between partitions so every stage eventually sees every chunk's candidates:

```c
function _topk_rotated_core(inp, config, batch_start, batch_end):   // :225
    total_pdim    = n_stages * BxS_size
    concat_free   = stage_free_size + n_stages * local_top_k_per_stage

    // values: folded V in first stage_free_size cols; grows by local_top_k per stage
    values  = predicated_folded_load(inp, fold_factor=n_stages, into=values)  // :315-323
    // indices: precomputed GLOBAL-index map (np.arange reshaped) — local->global is a gather
    indices[:, :stage_free_size] = shared_constant(global_index_map)          // :305-310
    rotation   = shared_constant(permutation_matrix)        // circulant (X) I, :287-288,333
    rotation_f32 = tensor_copy(rotation)                    // fp32 copy for index precision, :337-338

    for stage in static_range(n_stages):                    // :345
        offset = stage_free_size + local_top_k * stage      // growing free window, :346
        value, local_index = topk_core(values[:, :offset], k=local_top_k)   // strike-out, :348
        global_index = nc_n_gather(indices, local_index)     // local -> true V position, :351
        rotated      = rotate(value,        rotation)        // matmul permutation (PSUM), :357
        rotated_index= rotate(global_index, rotation_f32)    // :356
        insert(values,  rotated,       offset)               // append winners as new free cols, :359
        insert(indices, rotated_index, offset)               // tensor_copy on scalar_engine, :360
    return value, global_index                               // last stage's winners
```

Each stage (1) runs the strike-out top-K over the current free window `[:offset]` — which **grows** by `local_top_k` columns each stage as rotated winners are appended; (2) gathers true global indices via `nc_n_gather` against the precomputed index map (so local→global is a memory gather, not arithmetic); (3) **rotates** the winners one partition-step so the next stage's `max8` — which reads a *different* fold-row's chunk — also sees them. After `n_stages` rotations, every chunk's candidates have circulated through every stage, and the union of per-stage winners contains the global top-K.

### The Permutation Matrix

`RotationalConstants._get_permutation_matrix` (`utils:221-249`) builds the rotation as a **circulant block-diagonal shift-by-1**:

```c
function _get_permutation_matrix(block_size, num_blocks, dtype):  // utils:221
    base_perm = zeros(block_size)
    base_perm[1 % block_size] = 1                  // shift = 1, :237-240
    P_block = circulant(base_perm)                 // cyclic shift-by-1 on block_size rows, :241
    B = kron(eye(num_blocks), P_block)             // block-diagonal: num_blocks copies, :243-244
    save B.astype(dtype) -> NamedTemporaryFile.npy // shared_constant cache, :247-249
```

Here `block_size = BxS_size` (tile rows) and `num_blocks = n_stages`. The effect: `rotation @ tensor` cyclically rotates the `BxS` rows **by one within each stage-block** — partition `p`'s winners move to partition `p+1 (mod BxS)` inside the same stage. Across `n_stages` iterations the data makes a full circuit. The constant is materialized as a `np.float32` `.npy` (`prepare_rotational_constants`, `:503-504`) pulled in via `nl.shared_constant`; a bf16/input-dtype copy is used for values and the fp32 copy for indices (`:333-338`). `rotate` itself (`utils:699-716`) tiles the free dim by `nl.tile_size.gemm_moving_fmax` and issues one `nc_matmul` per tile — **the rotation is a permutation matmul on the PE array, landing in PSUM**, not a DVE op.

> **NOTE —** the global-index map (`_get_global_indices`, `utils:251-277`) is `np.tile(np.arange(padded_vocab).reshape(n_stages, stage_free_size), (BxS_size, 1))` — a static `[total_pdim, stage_free_size]` array recording the true V-position of every folded element. Both constants are written to `NamedTemporaryFile` `.npy` files cached in `RotationalConstants._shared_const_cache` and deleted by `cleanup_rotational_constants` (`:518-520`) after the kernel runs. A reimplementation must reproduce this map exactly, or the gathered global indices will be wrong.

> **QUIRK —** when `block_size == 1` (i.e. `BxS_tile == 1`), `base_perm[1 % 1] = base_perm[0] = 1` ⇒ `P_block` is the `1×1` identity and the rotation is a no-op within each block. A single-row block cannot rotate; coverage in that degenerate case relies on the `n_stages` partition rows being distinct fold-chunks rather than on intra-block rotation. The math is sound but the edge is easy to misread. *(INFERRED from the `shift % block_size` expression; the degenerate path is not separately asserted.)*

### The Cost Model and Tile Search

`_calculate_rotational_constants` (`utils:441-490`) derives the stage count and the HW-constrained tile size:

```c
max_n_stages        = floor(pmax // BxS_tile)               // partitions available, :458
ideal_n_stages      = ceil(min(k, V) / 8)                   // one fold per 8 outputs, :459
min_n_stages_for_hw = ceil(V / 2^14)                        // DVE free-dim limit, :462
n_stages            = clamp(ideal, min_hw, max_n_stages)    // :464-465
local_top_k_per_stage = align_up(ceil(k / n_stages), 8)     // :473
stage_free_size     = ceil(V / n_stages)                    // :476
```

Two HW constraints are asserted (`:480-488`): `stage_free_size <= 2^14` **and** `stage_free_size + n_stages * local_top_k_per_stage <= 2^14` (the concatenated free dim, the actual `max8` operand width). `validate_topk_input` (`utils:732-745`) re-asserts the same bound at kernel entry. For `BxS > 128`, `_find_optimal_tile_size` (`utils:542-616`) sweeps `tile_size ∈ [1, 128]` and minimizes `n_tiles × _estimate_dve_cost`, where the per-tile cost (`utils:493-539`) is:

```c
per_stage_cost = ceil(k_per_stage / 8) * 2 * (stage_free_size + 144)   // 144 = fixed_dve_inst_overhead
unsorted_cost  = n_stages * per_stage_cost
if needs_sort:                                                          // sorted or padded_k != orig_k
    sorted_cost = base_sort_cost / (BxS_tile / pmax)                    // penalize < 128 channels
```

The `TopkHardwareParams` (`utils:45-69`) fix the constants: `dve_max_alus = 8`, `topk_per_stage = 8`, `num_sbuf_quadrants = 4`, `fixed_dve_inst_overhead = 144`, `max_free_dim = 2^14`, `index_dtype = uint32`. The sort penalty divides by `BxS_tile / 128` because the sort wants the full 128 partition channels.

### Optional Sort

If `sorted` (forced `True` whenever `padded_k != orig_k`, `:667-670`), the per-tile winners `[n_stages*BxS, local_top_k]` are flattened back to `[BxS, padded_k]` through a `private_hbm` round-trip (`reshape_with_dma`, `utils:924-944`), then `sort` (`utils:870-921`) — itself a strike-out: repeated `max8` + `match_replace8(-inf)` in passes of 8, with `nc_n_gather` pulling global indices — and sliced `[:true_k]` to HBM. The unsorted path uses `unfolded_store` to reverse the fold directly (`:207-220`).

> **GOTCHA —** the `sort` helper has a generation branch (`utils:900-912`). On `nisa.get_nc_version() <= gen2` it uses a **separate** `nc_find_index8` + `nc_match_replace8`; on gen3+ it uses the **fused** `nc_match_replace8(dst_idx=...)`. A reimplementation targeting only the fused form will produce wrong indices on gen2 silicon. `topk_core` (`:843-865`) always uses the fused form in the full-fold branch — the split is specific to `sort`.

---

## The topk Method Dispatch

### The Enum and the Two Registries

`SupportedTopkMethods` (`rotational_topk.py:45-50`) enumerates three methods: `SCANNING = 0`, `CASCADED = 1`, `ROTATIONAL = 2`. The `@_kernel`-wrapped `topk(inp, k, sorted_flag=True, method=ROTATIONAL, lnc=None)` entry (`:397-486`) builds a `TopkConfig` + `RotationalTopkConfig`, looks the method up in `SUPPORTED_TOPK_METHOD_MAPPING`, and dispatches via grid syntax `selected_method[grid](...)` (`:480`).

```c
SUPPORTED_TOPK_METHOD_MAPPING = {                  // rotational_topk.py:365
    SupportedTopkMethods.SCANNING:   naive_scanning_topk,
    SupportedTopkMethods.ROTATIONAL: rotational_topk,
}                                                  // CASCADED enumerated but NOT a key
```

> **CORRECTION (O21-2) —** the claim "`CASCADED` is enumerated but not dispatch-wired" is true **only of the readable `nkilib/core/topk` registry**, where `SUPPORTED_TOPK_METHOD_MAPPING` (`:365`) carries exactly two keys (`SCANNING`, `ROTATIONAL`). The **production** registry is a separate compiled module, `neuronxcc/nki/_private_kernels/topk/topk_method_mapping.cpython-310-…so`, whose `__pyx` string table imports and wires **all three**: `naive_scanning_topk`, `rotational_topk`, **and** `cascaded_2_stage_topk` — a distinct kernel (`_private_kernels/topk/cascaded_2_stage_topk.so`, with its own `_helpers.so`) that has no readable nkilib twin. So `CASCADED` *is* dispatchable in the shipped compiler; it is the readable convenience copy that drops it. Treat the readable 2-key map as the library surface, the 3-key `.so` as the production ABI. This divergence is documented from the [internal-kernel-registry](internal-kernel-registry.md#the-second-registry--supported_topk_method_mapping) "second registry" analysis.

### Dispatch Flow

```text
topk(inp, k, sorted_flag, method, lnc)            ── rotational_topk.py:397
  ├─ if method not in SupportedTopkMethods: raise  ── :458
  ├─ topk_config = create_topk_config(...)         ── derives BxS, vocab, n_prgs, :461
  ├─ inp = inp.reshape(BxS, vocab_size)            ── :473
  ├─ config = create_rotational_topk_config(...)    ── n_stages, tile_size, padded_k, :476
  ├─ prepare_rotational_constants(config)          ── build circulant + index .npy, :477
  ├─ config.log_strategy()                          ── print the strategy box, :478
  ├─ selected = SUPPORTED_TOPK_METHOD_MAPPING[method]
  ├─ vals, inds = selected[grid](inp=inp, config=config)  ── :480
  └─ cleanup_rotational_constants()                 ── delete temp .npy files, :484
```

> **NOTE —** shard info (`prg_id`, `n_prgs`) is queried **inside** the kernel via `get_verified_program_sharding_info("topk", (0,1), 2)` (`:117`), not at config-construction time. The config carries only the user-requested `lnc`; the runtime corrects `prg_id`/`n_prgs` (`:118-125`). The same pattern appears in `cascaded_max` (`:144`). A reimplementation that bakes the shard id into the config will mis-shard.

---

## nisa Primitive → BIR → Engine Map

Every op these kernels emit and its lowering, confirmed via the cited DVE-search / TensorScalar encodings. All op names are live binary strings in `neuronxcc/nki/isa/neuron_isa.cpython-310-…so`.

| nisa op | Lowers to | TSCMode / note | Engine | Confidence |
|---|---|---|---|---|
| `tensor_tensor_scan` | `InstTensorScalarPtr` (IT29) | `TSCMode = TensorScan(2)`; `is_tensor_tensor_scan` flag, `op0`/`op1` fields | DVE/Pool | HIGH |
| `max8` | `InstMax` (IT88) | 8-wide, descending sort, earliest-index tie-break | DVE | HIGH |
| `nc_find_index8` | `InstMaxIndex` (IT89) | first-occurrence (ascending scan, IEEE `==`) | DVE | HIGH |
| `nc_match_replace8` | `InstMatchReplace` (IT90 / IT91 fused) | `imm = -inf` strike-out sentinel; IT91 writes `dst_idx` | DVE | HIGH |
| `tensor_reduce(maximum)` | reduce form | `TSCMode = Reduce(0)` | DVE/Pool | HIGH |
| `nc_matmul(is_transpose)` | matmul-transpose | rotation / index transpose | PE array | HIGH |
| `nc_n_gather` | gather | local→global index gather | DVE | MEDIUM |
| `iota` / `affine_select` / `memset` / `tensor_copy` | datamove / select | offset vectors, one-hot diagonal, carry | Act/Pool | HIGH |

The DVE-search ops (`max8` / `nc_find_index8` / `nc_match_replace8`) share the tie-break semantics documented in [DVE Search Encoding](../isa/dve-search-encoding.md): `max8` keeps an 8-slot buffer fully sorted descending (insertion sort, earliest data index wins on equal values); `nc_find_index8` returns the *first* occurrence of each max value.

---

## Related Components

| Name | Relationship |
|---|---|
| [`router_topk`](router-topk.md) | K ≤ 8 sibling — one `max8` + `find_index8`, no strike-out; `rotational_topk` is the K > 8 generalization |
| [_INTERNAL_KERNEL_REGISTRY](internal-kernel-registry.md) | hosts the second registry `SUPPORTED_TOPK_METHOD_MAPPING`; the production 3-key topk map (incl. `cascaded_2_stage_topk`) |
| [Production Kernel Inventory](production-kernel-inventory.md) | `cumsum.so` + the 12-file `topk/` `.so` partition; which kernels have readable twins |
| [DVE Search Encoding](../isa/dve-search-encoding.md) | Part 2 — `max8` / `FindIndex8` / `MatchReplace8` bit-level encoding and tie-break |
| [TensorScalar Encoding](../isa/tensorscalar-encoding.md) | Part 2 — `tensor_tensor_scan` lowers here (TSCMode TensorScan) |
| [nki.isa REDUCE / DVE Intrinsics](isa-reduce-dve-dma.md) | the `tensor_reduce` / `max8` / `nc_match_replace8` validator surface |
| [TopK Legalization](../hlo-opt/topk-legalize.md) | Part 4 — how a framework `TopK` op reaches these NKI kernels |

## Cross-References

- [router_topk](router-topk.md) — the K ≤ 8 single-`max8` top-K; this page is its K > 8 sibling
- [The _INTERNAL_KERNEL_REGISTRY Mechanism](internal-kernel-registry.md) — the production 3-key `SUPPORTED_TOPK_METHOD_MAPPING` that wires `CASCADED → cascaded_2_stage_topk`
- [Production Kernel Inventory](production-kernel-inventory.md) — the `cumsum` / `topk/` `.so` partition and readable-twin map
- [DVE Search Encoding](../isa/dve-search-encoding.md) — `max8` / `FindIndex8` / `MatchReplace8` encoding (Part 2)
- [TensorScalar Encoding](../isa/tensorscalar-encoding.md) — `tensor_tensor_scan` / TSCMode lowering (Part 2)
- [nki.isa REDUCE / SELECT / DVE Intrinsics](isa-reduce-dve-dma.md) — the validator surface for these primitives
- [TopK Legalization](../hlo-opt/topk-legalize.md) — framework `TopK` → NKI top-K path (Part 4)
