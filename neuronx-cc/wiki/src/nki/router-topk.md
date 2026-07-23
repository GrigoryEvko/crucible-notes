# MoE Routing: router_topk

> *All source line numbers on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (the cp312 wheel; cp310/cp311 carry byte-identical kernel sources per their RECORD sha256). The router algorithm is shipped as readable NKI-DSL Python under `nkilib/core/router_topk/router_topk.py` — a binary-derived wheel artifact, citeable verbatim. The production-called copy is the Cython extension `neuronxcc/nki/_private_kernels/router_topk.cpython-312-x86_64-linux-gnu.so`; both are documented below.*

## Abstract

`router_topk` is the front of every Mixture-of-Experts (MoE) layer in the Neuron stack: given the per-token hidden states `x` and the gate weight `w`, it computes the router logits `x.T @ w + bias`, gates them through softmax or sigmoid, selects the top-K experts per token, and produces the three tensors the downstream MoE dispatch consumes — `router_logits[T,E]`, `expert_index[T,K]`, and `expert_affinities[T,E]`. It is the gate of the MoE FFN: a single sparse routing decision per token, materialized as a dense GEMM, a top-K, an activation, and a scatter. The dense FFN that executes the selected experts is the [MoE CTE/prefill](moe-cte-prefill.md) path and its decode sibling MoE TKG *(planned)*; this page is the router that feeds both.

The kernel is built around one hardware idiom that makes the top-K cheap: because `K <= 8` is asserted, the kernel never iterates a `max+strike-out` cascade. It issues exactly **one** `nisa.max8` (the DVE's native 8-wide descending top-8) and **one** `nisa.nc_find_index8` (the matching argmax-index search), then slices `[:k]`. The `K > 8` cascaded-max loop with `MatchReplace8` strike-out — documented for the general top-K primitive — is structurally forbidden here. The two ops are the same [DVE search primitives](../isa/dve-search-encoding.md) (Max8 / FindIndex8) encoded in Part 2.

The page documents, in pipeline order: the **router GEMM** (`x` stationary, `w` moving, H-tiled contraction, optional PE column-tiling); the **top-K** (single `max8` + `nc_find_index8`); the **gating** (streaming softmax vs. independent sigmoid, and the `router_pre_norm` ACT1-vs-ACT2 fork); the **norm/scatter keystone** (the `norm_topk_prob` L1 renorm and the one-hot-mask vs. indirect-DMA scatter, including the default case where the router emits *unmasked* affinities and a downstream block masks); and the **LNC token shard** (split `T` across two cores, `sendrecv` exchange).

For reimplementation, the contract is:

- The **5-stage pipeline** `ACT1 → topK → ACT2 → Norm → Scatter` and the three legal flag tuples derived from `router_pre_norm` / `norm_topk_prob` — the keystone that decides whether the router or the downstream block masks.
- The **router GEMM**: `x` as the stationary operand, `w` as moving, H tiled `/128` into PSUM, the four x/w SBUF layouts, the optional bias broadcast, and PE column-tiling for small `T`.
- The **K ≤ 8 top-K**: one `max8` (descending, earliest-index tie-break) + one `nc_find_index8` (first-occurrence argmax), sliced `[:k]` — *not* a cascade.
- The **gating**: numerically-stable streaming softmax (`negmax` → `exp+reduce` → `reciprocal` → multiply) and per-logit sigmoid; the ACT1/ACT2 fork and why sigmoid + post-norm needs indirect-DMA scatter.
- The **scatter**: the `iota + EQUAL` one-hot mask, and the flattened `[T*E]` indirect-DMA alternative; the L1 renorm; the LNC `sendrecv` exchange.

| | |
|---|---|
| **Algorithm of record** | `nkilib/core/router_topk/router_topk.py` (1839 lines, `def router_topk` at `:55`) |
| **Production-called** | `neuronxcc/nki/_private_kernels/router_topk.cpython-312-x86_64-linux-gnu.so` (Cython, 3.74 MB) |
| **`.so` entry points** | `router_topk_isa_kernel`, `router_topk_kernel_nki`, `compute_activation`, `router_topk_input_x_load`, `router_topk_input_w_load` |
| **Production caller** | `neuronxcc/nki/_pre_prod_kernels/moe_token_gen.py:18,159` (imports `router_topk_isa_kernel`) |
| **PyTorch reference** | `nkilib/core/router_topk/router_topk_torch.py` (`router_topk_torch_ref`, 135 lines) |
| **Activation enum** | `RouterActFnType` (`nkilib/core/utils/common_types.py:39`): `SIGMOID = 0`, `SOFTMAX = 1` |
| **Dims** | `T ≤ 2048` tokens, `H` mult of 128, `E ≤ 512` experts, `K ≤ 8` |
| **HW constants** | `P_MAX=128`, `F_MAX=512`, `ST_F_MAX=128`, PE column tiles `32/64/128` (`:36-41`) |
| **nisa primitives** | `nc_matmul`, `max8`, `nc_find_index8`, `activation`, `tensor_reduce`, `reciprocal`, `tensor_scalar`, `tensor_tensor`, `iota`, `dma_copy`, `sendrecv`, `core_barrier` |

> **NOTE —** the readable `nkilib/core/router_topk/router_topk.py` exposes a `router_topk(...)` function; the production MoE token-gen path does **not** import it. It imports `router_topk_isa_kernel` / `router_topk_kernel_nki` from the *compiled* `_private_kernels/router_topk` extension (`moe_token_gen.py:18,21`). The Cython `.so` is built from a same-named `_private_kernels/router_topk.py` (its `__pyx` symbol table names the same five functions and the same nisa primitive set — `max8`, `nc_find_index8`, `nc_matmul`, `iota`, `core_barrier`, `reciprocal`, `tensor_reduce`, `dma_copy`). The two trees share structure, primitives, and docstrings; the `.so` carries at least one assertion the readable copy lacks (see [The Pipeline](#the-pipeline-and-the-keystone)). This page describes the algorithm as it reads in the Apache-licensed library source, and flags every place the compiled copy is known to diverge.

> **GOTCHA — the `.so` is not just a compiled copy of the readable kernel.** It is the artifact the production MoE path actually calls, it exposes *different* public entry names (`router_topk_isa_kernel` / `router_topk_kernel_nki`, not `router_topk`), and its string table carries an assertion — `"ACT1/router_pre_norm requires use_indirect_dma_scatter=True"` — with no counterpart in the readable `router_topk.py`. Read the library source for the algorithm; read the `.so` for the production ABI.

---

## The Pipeline and the Keystone

### Purpose

After the GEMM produces `router_logits[T,E]`, the rest of the kernel is a 5-stage pipeline whose stages are individually enabled or disabled. This is the spine of `router_topk`, and the flag derivation is the single most consequential decision in the kernel — it determines whether the router emits *masked* or *unmasked* affinities, and therefore whether the downstream MoE block must mask.

### The five stages

```text
ACT1  -->  topK  -->  ACT2  -->  Norm  -->  Scatter
(act)   (always on)   (act)    (L1 ÷Σ)    (mask/DMA)
```

`ACT*` is an activation (softmax or sigmoid). `Norm` is an L1 renorm — divide each selected affinity by the sum of the top-K. `topK` is always on. The stage flags are **not** kernel arguments; they are derived from `router_pre_norm` and `norm_topk_prob` to keep the public signature backwards-compatible (`router_topk.py:528-531`):

```c
// router_topk.py:528-531
pipeline_enable_act1    = router_pre_norm
pipeline_enable_act2    = not router_pre_norm
pipeline_enable_norm    = pipeline_enable_act1 and norm_topk_prob
pipeline_enable_scatter = pipeline_enable_act2 or (pipeline_enable_act1 and pipeline_enable_norm)
```

Only three `(act1, act2, norm)` tuples are legal; any other combination is a hard `kernel_assert` (`:534-547`):

| `(act1, act2, norm)` | Stages run | Meaning | Trigger |
|---|---|---|---|
| `(False, True, False)` | topK, ACT2, Scatter | post-norm: activate the K selected only | `router_pre_norm=False` |
| `(True, False, False)` | ACT1, topK | pre-norm, no L1 — **default** | `router_pre_norm=True, norm_topk_prob=False` |
| `(True, False, True)` | ACT1, topK, Norm, Scatter | pre-norm + L1 renorm | `router_pre_norm=True, norm_topk_prob=True` |

### The keystone

> **GOTCHA —** in the **default** tuple `(True, False, False)`, `pipeline_enable_scatter` is `False`. The kernel writes the *full* `[T,E]` activated affinities (`router_topk.py:565-581`) and does **not** zero the non-selected experts. The selection still happened — `expert_index[T,K]` is correct — but `expert_affinities[T,E]` is a dense softmax/sigmoid over *all* `E`, not a K-hot vector. A reimplementer who assumes `router_topk` always returns pre-masked affinities will route every token to every expert. The masking is deferred to the downstream MoE block, which gates it on exactly this combination: `mask_unselected_experts = router_pre_norm and not norm_topk_prob`.

When `norm_topk_prob=True` *or* `router_pre_norm=False`, scatter is on and the router pre-masks (zeroes non-selected experts) and, for the norm case, renormalizes so each token's row sums to 1. The PyTorch reference encodes exactly this fork: `router_topk_torch.py:96-107` scatters and L1-normalizes only when `router_pre_norm and norm_topk_prob`, else returns the full `expert_affinities_full` (`:107`); the post-norm branch (`:108-129`) gathers, activates, and scatters the top-K.

> **NOTE —** the compiled `.so` adds a guard the readable source does not have. Its string table contains `"ACT1/router_pre_norm requires use_indirect_dma_scatter=True, got "`, implying the production kernel forbids at least one `router_pre_norm` combination unless indirect-DMA scatter is selected. The readable `router_topk.py` has no such assertion; its only activation/scatter cross-guard is the sigmoid one (`:237-240`). Treat the indirect-DMA requirement as broader in the production ABI than the library source suggests. The string is present in the `.so`; the exact triggering combination is [UNRESOLVED].

---

## The Router GEMM

### Purpose

Compute `router_logits[T,E] = x[H,T].T @ w[H,E] + w_bias[1,E]`. The contraction dimension is `H` — the PE-array partition dimension, tiled `/128` and accumulated in PSUM. `T` and `E` are free dimensions. The PyTorch reference is `x_work.T @ w (+ w_bias)` (`router_topk_torch.py:70-77`).

### Algorithm

```c
function router_gemm(x_sb, w_sb, bias_sb):           // router_topk.py:386-478
    num_h_tiles = H // 128                            // contraction tiles (:276)
    for t_tile in TiledRange(T_local, ST_F_MAX=128):  // :386
        psum = ndarray((128, E), fp32, buffer=psum)   // full P_MAX width (:395)
        for h_tile_idx in range(num_h_tiles):         // :397
            w_tile = w_sb[:, h_tile_idx, :]           // [128, E]   (:399)
            x_tile = slice_x(x_sb, h_tile_idx, t_tile) // [128, t_size], layout-dependent (:410-422)
            col = (h_tile_idx % num_pe_array_column_tiles) * pe_col_tile_size  // :425-427
            nc_matmul(dst       = psum[ds(col, t_size), :],   // x STATIONARY, w MOVING (:431-438)
                      stationary= x_tile,             // free = T
                      moving    = w_tile,             // free = E  -> result [T, E]
                      tile_position=(0, col),
                      tile_size =(128, pe_col_tile_size))
        if has_bias:                                  // :442-457
            tensor_tensor(router_logits_sb[t_tile], psum, bias_bc, op=add)  // fused PSUM->SBUF spill
        else:
            tensor_copy(router_logits_sb[t_tile], psum)        // plain spill (:461-465)
        for col_tile in range(1, min(num_h_tiles, num_pe_array_column_tiles)):  // merge col tiles (:471-478)
            tensor_tensor(router_logits_sb[t_tile], router_logits_sb[t_tile],
                          psum[ds(col_tile*sz, t_size), :], op=add)
```

> **QUIRK —** `x` is the **stationary** operand and `w` is **moving** (`:434-435`). Both are interpreted by `nc_matmul` as `[partition=contraction, free]`: stationary free = `T`, moving free = `E`, so the result is `[T, E]` directly — no post-transpose. The naive expectation (weights stationary, activations streamed) is inverted because here the *activation* tile is the small, reused operand and `E` is the wide moving dimension that the matmul streams. A reimplementer who swaps the operands gets `[E, T]` and a layout mismatch downstream.

### x and w SBUF layouts

`x` in HBM is `[H,T]` (`x_hbm_layout=0`) or `[T,H]` (`=1`); if `x.buffer == nl.sbuf` it is already resident and used as-is (`:184,343-344`). The HBM→SBUF load (`router_topk_input_x_load`, `:1231`) picks an internal layout (`:336-344`):

| `x_hbm_layout` | internal `sb_layout` | x_sb shape | p-dim stride |
|---|---|---|---|
| 0 (`[H,T]`) | 3 | `[128, H/128, T]` | consecutive H |
| 1 (`[T,H]`) | 0 | `[128, T, H/128]` | stride `H/128` |
| 1 | 1 | `[128, T, H/128]` | stride `H/256`, H interleaved in halves (LNC2-on-H amenable) |
| 1 | 2 | `[128, T, H/128]` | consecutive H |

Only `(hbm=0, sb=3)` and `(hbm=1, sb∈{0,1,2})` are supported (`:1299-1306`). `w` mirrors `x`'s H-stride so the matmul contraction lines up, returning `w_sb=[128, H/128, E]` (`router_topk_input_w_load`, `:1523`; the `sb_layout=1` path loads `[128,2,H/256,E]` then reshapes back to 3D, `:1688-1696`).

> **GOTCHA — the entry assert and the loader accept different layout sets, deliberately.** `router_topk.py:181` admits only `x_sb_layout in (0,1,2)`, while layout **3** is used internally for `hbm_layout=0` loads (`:339`) and `router_topk_input_x_load` accepts `sb_layout ∈ {0,1,2,3}` (`:1292`). Layout 3 is kernel-internal, chosen by the loader, and never user-facing.

### PE column tiling

For small `T`, the 128-wide PE array wastes columns. With `use_column_tiling=True` the column-tile width rounds `T` up to 32/64/128 (`:303-309`), and `num_pe_array_column_tiles = 128 // width` (`:315`). H-tiles are round-robined across the parallel column slots via `tile_position` (`:425-428`); the per-slot partial sums are then `tensor_tensor`-added back together (`:471-478`). Disabled ⇒ width 128, one slot, the merge loop does not execute. The merge loop guards on `min(num_h_tiles, num_pe_array_column_tiles)` (`:470`) so empty slots — when `num_h_tiles < num_column_tiles` — are never accumulated.

### Bias

`has_bias = w_bias != None` (`:268`), reshaped to `[1,E]` (`:271`). The `[1,E]` vector is `dma_copy`'d to SBUF then broadcast to `[t_tile_size, E]` by one of two paths (`:366-382`): with `use_PE_broadcast_w_bias=True`, a `nc_matmul` of `ones_mask[1,t] · bias[1,E]` with `is_stationary_onezero=True` (TensorE broadcast), then a scalar-engine copy; else `stream_shuffle_broadcast` (DVE/pool broadcast). The bias is applied **fused** with the PSUM→SBUF spill via `tensor_tensor(add)` (`:452-457`), which is why the plain `tensor_copy` spill (`:461-465`) is skipped when bias is present.

### Store

`router_logits[T,E]` is always stored to HBM (`:481-502`): `dma_copy` of the whole `[t_p_dim, tiles, E]` SBUF block via `_hbm_tiled_store_view` (slice → reshape → permute, `:1191`) plus a `_hbm_remainder_store_view` for the `T % 128` tail (`:1209`).

> **GOTCHA —** `skip_store_router_logits=True` is asserted **unsupported** (`:260-265`): `router_logits` is a `mutable_tensor` output, and the compiler limitation `NCC_IGCA090` requires every `mutable_tensor` to have at least one store. The argument exists in the signature for forward-compatibility but always fails the assert when set.

---

## The Top-K Selection

### Purpose

Find, per token, the K largest values and their expert indices. Because `K ≤ 8` is asserted (`:593-594`), this is **one** `max8` plus **one** `nc_find_index8`, sliced `[:k]` — never a cascade.

### Algorithm

```c
function top_k(topk_input_sb):                       // router_topk.py:604-633
    // topk_input = expert_affinities_full_sb if ACT1 else router_logits_sb   (:590)
    for t_tile in TiledRange(T_local, 128):
        top8 = ndarray((t_size, 8), dtype=topk_input.dtype)
        max8(dst=top8, src=topk_input_sb[:t_size, t_idx, :])   // top-8 DESCENDING (:609-611)
        tensor_copy(router_logits_topk_sb[:, t_idx, :], top8[:, :k])  // take K of 8 (:612-615)

        find_index8(dst=tmp[t_size, 8],               // argmax INDEX per max value (:620-624)
                    data=topk_input_sb[:t_size, t_idx, :],
                    vals=top8)                         // values DRIVE the index search
        tensor_copy(router_indexes_topk_sb[:, t_idx, :k], tmp[:, :k])    // uint32 expert ids (:625-627)
        tensor_copy(router_indexes_topk_truek_sb_fp32[:, t_idx, :],      // fp32 mirror for the
                    router_indexes_topk_sb[:, t_idx, :k])                // EQUAL mask later (:630-633)
```

`max8` returns the top-8 values sorted descending, with a strict-`>` insertion that yields an **earliest-index** tie-break (Part 2 `InstMax`/IT88). `nc_find_index8` then takes those 8 values and, for each, returns the **first-occurrence** data index (ascending scan, IEEE `==`) — `argmax` index per slot, with `-1` for absent slots (Part 2 `FindIndex8`/IT89). Output dtype is `uint32`. The fp32 mirror at `:630-633` exists only because the later one-hot scatter compares indices with `nl.equal` (a `tensor_scalar`), which needs a float operand.

> **NOTE —** in the default pre-norm path, top-K runs on the *activated* affinities (`topk_input_sb = expert_affinities_full_sb`, `:590`); in the post-norm path it runs on the *raw* logits (activation applied to the K selected later). The selection is identical either way: softmax and sigmoid are monotonic, so `argmax` over logits equals `argmax` over activated values. Running top-K on activated values in the ACT1 path is a scheduling choice (the activation is already materialized), not a correctness requirement.

> **QUIRK —** this is the K ≤ 8 special case of the general top-K primitive, not a degenerate use of it. The general primitive iterates `max8 + MatchReplace8` strike-out for `K > 8`; `router_topk` asserts `k <= 8` (`:594`) precisely to avoid the strike-out loop — one `max8` covers all of K in a single 8-wide hardware op. A reimplementer targeting `K > 8` must add the cascade; this kernel cannot.

### Store expert_index

`expert_index[T,K]` (uint32) is written to its auto-detected buffer (`:637-730`): SBUF output is a `tensor_copy` (`:710-714`); HBM output is `dma_copy` via the tiled + remainder views (`:721-730`); the `shard_on_tokens` path exchanges across cores (see [LNC Token Shard](#lnc-token-shard)). `skip_store_expert_index=True` suppresses the store (`:637,715`).

---

## The Gating

### Purpose

Apply the router activation. `compute_activation` (`router_topk.py:1753-1839`) is the single gating routine, dispatched on `RouterActFnType` — `SIGMOID = 0`, `SOFTMAX = 1` (`common_types.py:42-43`). The branch tests the enum *members*, so the numeric ordinals are behaviorally irrelevant.

### Algorithm

```c
function compute_activation(dst, input, t_tile, act_fn, ...):   // router_topk.py:1753
    if act_fn == SIGMOID:                                       // :1783-1786
        activation(dst[t_tile], op=sigmoid, data=input[t_tile]) // per-logit, NO sum
        return
    if act_fn == SOFTMAX:                                       // :1788-1834  numerically-stable streaming
        tensor_reduce(negmax,  op=maximum, data=input[t_tile], negate=True)  // -max(row) (:1799-1806)
        activation(result_exp, op=exp, data=input[t_tile],
                   bias=negmax, reduce_op=add, reduce_res=exp_sum,
                   reduce_cmd=reset_reduce)                      // exp(x - max), Σ in one pass (:1808-1821)
        reciprocal(exp_sum, exp_sum)                            // 1/Σ  (:1823-1825)
        if complete_activation:                                 // :1828-1834
            tensor_scalar(dst[t_tile], result_exp, op0=multiply, operand0=exp_sum)  // exp * (1/Σ)
```

Sigmoid is per-logit and independent — no normalization sum. Softmax is the standard numerically-stable form: subtract the row max (carried as `negmax`, computed with `negate=True` so it can be passed as the `exp` bias), exponentiate while accumulating the denominator in the *same* activation instruction (`reduce_op=add`, `reduce_res=exp_sum`, `reduce_cmd=reset_reduce`), reciprocate, multiply.

> **NOTE —** `complete_activation=False` returns only the intermediates `(negmax, exp_sum)` and skips the final multiply. The ACT2 one-hot scatter uses this to softmax the *full* `E` later using the top-K's `negmax` and denominator, so the masked output equals a softmax taken over the top-K only (see [Scatter](#the-scatter)).

### The ACT1 / ACT2 fork

`router_pre_norm` selects *where* gating runs:

- **ACT1** (`router_pre_norm=True`, `:549-563`): activate the full `[T,E]` logits **before** top-K into `expert_affinities_full_sb`; top-K then runs on the activated values. In the default (no scatter) case, this full tensor *is* the output (`:565-581`).
- **ACT2** (`router_pre_norm=False`, `:757-793`): top-K first on raw logits, then activate **only** the K selected values into `expert_affinities_topk_sb`.

> **GOTCHA —** sigmoid + `router_pre_norm=False` + not `use_indirect_dma_scatter` is asserted **illegal** in the readable source (`:237-240`): the one-hot ACT2 path completes the softmax over the full `E` using top-K `negmax`/denominator, a construction that has no sigmoid analogue, so sigmoid in that path must use indirect-DMA scatter. The production `.so` appears to extend this constraint (see the `"ACT1/router_pre_norm requires use_indirect_dma_scatter"` note above) — verify the exact legal matrix against the compiled kernel before relying on a `router_pre_norm` + one-hot combination.

### Norm — L1 renorm

When `norm_topk_prob=True` (only legal with ACT1, `:797-824`):

```c
// router_topk.py:804-824
tensor_reduce(sum_of_max, op=add, data=router_logits_topk_sb[t_tile], axis=1)  // Σ_k top-K values
reciprocal(sum_of_max, sum_of_max)                                             // 1/Σ
tensor_scalar(expert_affinities_topk_sb[t_tile],                               // L1 renorm: top-K ÷ Σ
              data=router_logits_topk_sb[t_tile], op0=multiply, operand0=sum_of_max)
```

In the scatter, the full affinities are *also* scaled by the same `1/Σ` before masking (`:922-929`), so the masked output rows sum to 1. This is the case where the router pre-masks and renormalizes, and the downstream mask is therefore disabled. The PyTorch reference matches: scatter the top-K values then divide by the per-token sum (`router_topk_torch.py:96-105`).

---

## The Scatter

### Purpose

Build `expert_affinities[T,E]` — the activated affinity at the K chosen experts, zero elsewhere. Two methods: a one-hot mask (default) and an indirect DMA. Scatter only runs when `pipeline_enable_scatter` is set (post-norm, or pre-norm + L1).

### One-hot scatter

```c
function scatter_one_hot(t_tile):                    // router_topk.py:835-956
    memset(mask_sbuf[t_size, E], 0.0)                 // :880
    iota(expert_num_idx_arr[t_size, E], pattern=[[1,E]], offset=0, channel_multiplier=0)  // 0..E-1 per row (:884)
    for expert_idx in range(k):                       // :892-917
        tensor_scalar(export_check, op0=EQUAL,        // 1.0 exactly at the chosen expert column (:903-910)
                      data=expert_num_idx_arr,
                      operand0=router_indexes_topk_truek_sb_fp32[:, t_tile, expert_idx])
        tensor_tensor(mask_sbuf, mask_sbuf, export_check, op=add)   // accumulate K-hot mask (:912-917)
    if pipeline_enable_act2:                           // finish softmax over FULL E with top-K negmax+denom (:849-866)
        activation(exp_full, op=exp, data=router_logits_sb[t_tile], bias=topk_negmax)
        tensor_scalar(softmax_full, exp_full, op0=multiply, operand0=topk_exp_sum)
        tensor_tensor(scattered[t_tile], mask_sbuf, softmax_full, op=multiply)   // :939-944
    else:  // ACT1
        if pipeline_enable_norm:
            tensor_scalar(affin_full[t_tile], affin_full[t_tile], op0=multiply, operand0=1/Σ)  // :924-929
        tensor_tensor(scattered[t_tile], mask_sbuf, affin_full[t_tile], op=multiply)  // :931-936
```

The mask is built by `iota`-ing a row of incrementing expert ids `0..E-1` (`channel_multiplier=0` makes every partition row identical), then for each of the K columns comparing it for equality against the token's expert index (broadcast across `E`). `nl.equal` is a `tensor_scalar` (vector vs. tensor), which is why the per-column index must be a `[t,1]` column vector — hence the loop over `k`. The K `export_check` results are summed into a K-hot mask. The final store is `dma_copy` SBUF→HBM (tiled + remainder, `:1060-1072`) followed by `core_barrier(expert_affinities, cores=[0,1])` for cross-core consistency (`:1074`).

> **QUIRK —** the ACT2 path softmaxes the *entire* `E` (`:849-866`), not just the top-K, but it does so with the `negmax` and denominator taken from the **top-K** values. After the K-hot mask multiply, the surviving entries equal a softmax computed over the top-K only — the full-`E` exponentials in the non-selected columns are zeroed by the mask before they matter. This avoids a separate gather/softmax of the K values and reuses the already-resident full-logit tensor.

### Indirect-DMA scatter

```c
function scatter_indirect_dma(t_tile):               // router_topk.py:958-1057
    assert not expert_affin_in_sb                     // affinities MUST be HBM here (:974)
    dma_copy(expert_affinities[token_slice], zeros)   // memset HBM region to 0 (:979-982)
    iota(index_offset[t_p_dim,1], channel_multiplier=E, offset=(T_offset+tile*t_p_dim)*E)  // 0,E,2E,... (:999-1005)
    tensor_scalar(flattened, data=router_indexes_topk_sb[:, t_tile, :], op0=add, operand0=index_offset)  // -> [T*E] idx (:1031-1036)
    affin_1d = expert_affinities.reshape((T*E,))      // :1040
    for k_idx in range(k):                            // :1042-1057
        dma_copy(dst=affin_1d.ap(..., vector_offset=flattened[:,k_idx], indirect_dim=0),  // indirect scatter
                 src=expert_affinities_topk_sb[..., k_idx])
```

Indirect DMA cannot use a 2-D index tensor, so the `[T,K]` expert indices are flattened into indices into a 1-D `[T*E]` view of `expert_affinities`. The flattening adds a per-token offset (`0, E, 2E, …`, built by `iota` with `channel_multiplier=E`) to each expert index, mapping `(token, expert)` → `token*E + expert`. Each of the K affinity columns is then scattered with one `dma_copy` using `vector_offset=index_column, indirect_dim=0`. Requires `T ≤ 128` or a multiple of 128 (`:243-247`), and is the mandatory path for the sigmoid + post-norm case.

---

## LNC Token Shard

### Purpose

`shard_on_tokens=True` splits `T` across `n_prgs` (= 2) NeuronCores on the **token** dimension, so each core computes its own token slice's logits / top-K / affinities, then exchanges results so every output tensor is globally complete (`:215-232, 637-709, 1076-1182`).

### Algorithm

```c
// router_topk.py:218-232
T_first_shard  = T // n_prgs
T_local        = T_first_shard       if prg_id==0 else T - T_first_shard
T_offset       = 0                   if prg_id==0 else T_first_shard
T_sendrecv_size= max(T_local, other_T_local)   // Beta-3 sendrecv needs matching partition dims (:225-227)
```

Each core processes its slice, then three `sendrecv` sites (all `peer = 1 - prg_id`, `pipe_id=0`) exchange and merge:

| Output | Path | Lines |
|---|---|---|
| `expert_index` | `T≤128`: send/recv `[T,k]`, then `cross_partition_copy` local+remote in offset order (`dst_start=0` first) | `:640-686` |
| `expert_index` | `T>128` (mult-128): tile-dim `sendrecv` directly into `expert_index` slices | `:687-709` |
| `expert_affinities` (one-hot, SBUF out) | send/recv `[T,E]`, `cross_partition_copy` | `:1077-1125` |
| eager affinities (`return_eager_affi`) | send/recv `[T,k]` → `expert_affinities_topk_full` | `:1131-1183` |

> **NOTE —** the `dst_start=0`-first ordering is deliberate: Core 0 writes its local slice first then the received remote slice; Core 1 writes the received slice first then its local slice. This keeps the partition writes monotonically increasing and avoids an out-of-order `cross_partition_copy` (`:651-686`). The non-shard path sets `T_local=T` and skips all `sendrecv`; the `core_barrier` after the HBM stores still runs for consistency.

---

## The Outputs

`router_topk` returns `[router_logits[T,E], expert_index[T,K], expert_affinities[T,E]]`, with `expert_affinities_topk[T,K]` appended when `return_eager_affi=True` (`:1129-1188`):

| Output | Content | Buffer |
|---|---|---|
| `router_logits[T,E]` | raw GEMM + bias, always stored | HBM |
| `expert_index[T,K]` | uint32 chosen expert ids per token | HBM or SBUF (auto) |
| `expert_affinities[T,E]` | gating weights — **full activated, unmasked** in the default tuple; K-hot masked/renormalized with `norm_topk_prob` or post-norm | HBM or SBUF (auto) |
| `expert_affinities_topk[T,K]` | eager top-K affinities (optional fast path) | SBUF |

Buffer types are auto-detected from each tensor's `.buffer` attribute (`x`, `expert_affinities`, `expert_index`, `:184-186`); SBUF outputs require `T ≤ 128` (`:639, 947-950`). The `[T,E]` affinity tensor is what the downstream MoE dispatch consumes — the CTE path flattens it; the TKG path reads it as `[T,E]` — and the masking decision propagates from this kernel's pipeline tuple.

> **NOTE —** there is **no histogram** in `router_topk`. No `Nonzero`/count op appears in the source. The expert-count histogram (how many tokens chose each expert) belongs to the downstream MoE dispatch, not the router.

---

## Related Components

| Name | Relationship |
|---|---|
| [MoE CTE / prefill](moe-cte-prefill.md) | Consumes `expert_affinities` / `expert_index`; the dense per-expert FFN behind the routing decision |
| MoE decode (TKG) *(planned)* | Decode-phase consumer; `moe_token_gen.py` is the production caller that imports `router_topk_isa_kernel` |
| [DVE Search & Datamove Encoding](../isa/dve-search-encoding.md) | Part 2 ISA encoding of `max8` (IT88) and `nc_find_index8` (IT89) — the top-K primitives |
| Top-K primitives *(planned)* | The general `K > 8` cascaded-max / `MatchReplace8` strike-out path that this kernel forbids |

## Cross-References

- [MoE Context/Prefill (CTE)](moe-cte-prefill.md) — the dispatch and `bwmm` shard variants this router feeds; same readable-library-vs-compiled-`.so` provenance split
- [DVE Search & Datamove Encoding](../isa/dve-search-encoding.md) — `Max8` / `FindIndex8` ISA encoding, descending-sort and first-occurrence semantics
- [TopK Legalization](../hlo-opt/topk-legalize.md) — the HLO-level top-K lowering that may target this primitive family
- [BirCodeGenLoop](bircodegenloop.md) — the beta-3 NKI lowering stack that compiles trace-mode kernels like `router_topk`
