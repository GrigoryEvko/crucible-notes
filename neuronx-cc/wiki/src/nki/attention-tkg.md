# Flash-Attention: Decode (TKG)

> *All line numbers and symbols on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical — `attention_tkg.py` sha256 `d9e294d5…fc665`). The kernel ships as readable NKI Python under `neuronxcc/.../nkilib/core/attention/`, a binary-derived wheel artifact. Other wheels differ; treat every line number as version-pinned.*

## Abstract

`attention_tkg` is flash-attention specialized for **token generation** — the decode phase, where one (or a handful of) freshly-sampled token(s) attend the entire KV cache. It is the sibling of `attention_cte` (the context/prefill kernel, [6.7.6](attention-cte.md)) and the two split the attention workload along a single axis: prefill is **compute-bound** (a long query times a long key, a big GEMM), decode is **memory-bound** (a tiny query times a huge cache, a long DMA read with almost no arithmetic per byte). Every structural choice in this kernel follows from that inversion. The query is *not* tiled — the whole query-head group rides ≤128 PE partitions in a single matmul pass — and the cache read, not the matmul, is what the kernel works to make cheap.

Three things make the decode kernel its own algorithm rather than a degenerate prefill. First, the scores are kept **transposed** throughout: `[s_prior, s_active·q_head]` with the cache axis on the partition dimension, so the softmax reduces *along partitions*. Second, that reduction is done not by a reduce-engine pass but by a **matmul against a ones-vector** — the softmax denominator `Σ exp(·)` falls out of `nc_matmul(stationary=exp_scores, moving=ones)`, reusing the PE array that already holds the data. Third, decode has **no in-kernel causal triangle** at all (`grep "causal"` returns 0, against 65 in the prefill kernel): every cache position is in the past, so the only masking is a validity cut (`index < cache_len`), an optional sliding window, and — for speculative decode — a small causal sub-block among the active tokens.

The mask is where decode and prefill diverge most sharply, and it is the single fact most easily gotten wrong. `attention_cte` writes additive `−inf` *directly into the scores* via `affine_select`/`range_select`. `attention_tkg` never materializes `−inf` in its mask: `gen_mask_tkg` emits a **multiplicative 0/1 mask** (1 = attend, 0 = masked), and the consumer converts it to `−inf` *lazily* by a predicated copy into a `−inf`-prefilled buffer. Two kernels, two representations — documented in §[gen_mask_tkg](#gen_mask_tkg--the-01-multiplicative-mask) and called out as a [CORRECTION](#correction--mask-representation).

For reimplementation, the contract is:

- **The single-query online softmax**, kept transposed — `K`-stationary/`Q`-moving QK, the **ones-vector-matmul sum reduction**, `V`-stationary/`exp`-moving PV, and the multiply-by-reciprocal normalization.
- **The two KV-cache layouts** — flat contiguous (`dma_copy`/`dma_transpose`) and paged/block (`active_blocks_table` uint32 page table + **indirect-DMA gather**, vLLM-style) — and the in-SBUF append of the current token's K/V at the cache tail.
- **The GQA free broadcast** — `kv_heads=1`, the whole `q_head` group flattened into the matmul's moving operand against the one stationary K/V tile, so KV reuse costs nothing.
- **`gen_mask_tkg`'s 0/1 multiplicative mask** — `iota < pos_ids` for causal/padding, a branchless `[start,end)` band for SWA, a `tril` active sub-block — and how the consumer turns 0/1 into `−inf`.

| | |
|---|---|
| **Kernel entry** | `attention_tkg(q, k_active, v_active, k_prior, v_prior, mask, out, cfg, sbm, …)` (`attention_tkg.py:56-73`) |
| **Reference semantics** | `_attention_tkg_fwd_ref` (`attention_tkg_torch.py:306-418`) |
| **Target regime** | decode: `s_active ≤ 7`, `d_head ≤ 128` (docstring `:15-18`) |
| **`nc_matmul` call-sites** | **3** — QK (`:1974`), ones-sum (`:2589`), PV (`:2952`) — vs 65 in `attention_cte` |
| **Score orientation** | TRANSPOSED `[s_prior(part), s_active·q_head(free)]` — softmax reduces along partitions |
| **Mask representation** | MULTIPLICATIVE 0/1 (fp32 in HBM, **uint8** in SBUF, `:1045`); `−inf` injected lazily |
| **Mask builder** | `gen_mask_tkg` / `gen_mask_tkg_hbm` (`gen_mask_tkg.py`, 1117 lines) |
| **Cache layouts** | FLAT `[B+,1,s_prior,d]` · PAGED `[blocks,block_len,d]` + `active_blocks_table:uint32` |
| **`causal` occurrences** | **0** (decode is mask-only) vs 65 in `attention_cte` |

---

## The single-query decode attention

### Purpose

Compute `O = softmax(scale · Q·Kᵀ) · V` for `s_active` new query tokens against `s_prior` cached keys/values, with the cache axis on the PE partition dimension so the softmax reduces along partitions. The torch reference (`attention_tkg_torch.py:306-418`) is the ground truth and is worth reading as the spec the kernel implements:

```python
# _attention_tkg_fwd_ref, attention_tkg_torch.py:306-418  [CONFIRMED]
k_prior[..., -s_active:]    = k_active     # :376  APPEND new K at cache tail
v_prior[..., -s_active:, :] = v_active     # :377  APPEND new V at cache tail
score = k_prior.permute(0,1,3,2) @ q       # :380  K·Q → [b,n,s_prior,s_active]  (TRANSPOSED)
score[mask == 0] = -inf                    # :382  validity / SWA / spec mask (NOT a triangle)
score_max = max(score, dim=s_prior)        # :392  reduce over the CACHE axis
score = exp(score - score_max)             # :397-398
score_sum = sum(score, dim=s_prior)        # :403  reduce over the CACHE axis
score = score / score_sum                  # :408
out = score.permute(0,1,3,2) @ v_prior     # :415  P·V → [b,n,s_active,d]
```

The decisive fact is the reduction axis: the softmax reduces over `s_prior` (the cache), and there is **no self-causal triangle** among cache positions because every cache slot is strictly in the past. The only masking is (a) the validity boundary — cache slots beyond `cache_len` are padding — and (b) the `s_active × s_active` sub-block for speculative decode plus optional sliding-window. This is why `attention_tkg.py` contains zero occurrences of `"causal"`.

### Algorithm

The kernel implements the same math in five PSUM-staged steps, holding the scores transposed so `s_prior` sits on the partition axis. The main loop (`:357-394`) iterates `batch_tile → FA_tile`; each iteration fires Steps 1–5.

```c
// attention_tkg() main step sequence, per (batch_tile, FA_tile)   :357-394
function decode_attention_tile(i_b, fa_tile):
    // ---- Step 0: optional RoPE + Q scale (fused) -------------------------
    if cfg.fuse_rope:
        q_sb = apply_rope(q, inv_freqs, rope_pos_ids)          // _perform_rope
        q_sb = activation(q_sb, op=copy, scale = 1/sqrt(d_head))   // :1567  scale fused here

    // ---- Step 1: QK  (K stationary, Q moving) ---------------------------
    k_tile = load_k_tile(fa_tile)                              // dma_copy / dma_transpose / gather
    if is_last_nc_shard and is_last_fa_tile:                   // :1800
        k_sb[:, tail-s_active : tail] = k_active               // :1891  APPEND current K
    qk_psum = nc_matmul(stationary=k_tile, moving=q_sb_view)   // :1974  = KᵀQ → [s_prior, s_active_qh]
    tensor_copy_predicated(dst=qk_sb, src=qk_psum, predicate=mask)  // :2005  MASK applied here

    // ---- Step 2: online max (over s_prior partitions) ------------------
    qk_max = cascaded_max_reduce(qk_sb)                        // :2060 tensor_reduce(maximum)+transpose
    qk_max = tensor_scalar(qk_max, _MIN_FLOAT32, clamp)        // :2305  clamp all-(-inf) → finite

    // ---- Step 3: exp ----------------------------------------------------
    qk_io = activation(qk_sb - qk_max, op=exp)                 // :2318/:2359  → io dtype (e.g. bf16)

    // ---- Step 4: sum  (ONES-VECTOR MATMUL — the denominator trick) ------
    for tile in n_sprior_tiles:                                // _tile_sum_reduction :2563
        sum_psum[:,tile] = nc_matmul(stationary=qk_io[tile],  // :2589  contract over s_prior
                                     moving=one_vec)           //        → [s_active_qh, 1]
    exp_sum = tensor_reduce(sum_psum, add)                     // :2598  across tiles
    exp_sum_recip = reciprocal(exp_sum)                        // :2555

    // ---- Step 5: PV  (V stationary, exp moving) + normalize ------------
    v_sb = load_v_tile(fa_tile)                                // dma_copy / gather
    if is_last_nc_shard and is_last_fa_tile:                   // :2782
        v_sb[bottom-right s_active,d] = v_active               // :2929  APPEND current V
    exp_v_psum = nc_matmul(stationary=v_sb, moving=qk_io)      // :2952  = (P·V)ᵀ → [d_head, s_active_qh]
    exp_v = tensor_tensor(exp_v_psum, exp_sum_recip, multiply) // :2973  normalize (non-FA path)
    store_output(exp_v)                                        // _gather_and_store_output :3036
```

> **QUIRK — the softmax sum is a matmul, not a reduce.** The denominator `Σ exp(score)` over the `s_prior` cache axis is computed by `nc_matmul(stationary=exp_scores, moving=one_vec)` where `one_vec` is a `[p_max,1]` column memset to `1.0` (`bufs.one_vec`, alloc `:315`, `memset(value=1.0)` `:316`, doc `:599-600`). Because `s_prior` lives on the partition (contraction) axis and the moving operand is a single ones-column, the matmul contracts `exp_scores[s_prior, s_active_qh]` against `ones[s_prior, 1]` to yield `[s_active_qh, 1]` — exactly the column sum. The PE array already holds the exp'd scores from Step 3's layout, so the reduction reuses the systolic array instead of issuing a separate reduce-engine pass. The same shape is why MM1 keeps scores transposed in the first place: it puts `s_prior` on the partition axis for *both* the max-reduce and this sum-matmul.

### The matmul operand contract

All three matmuls follow the `nc_matmul(out_psum, stationary=…, moving=…)` contract (the QK/PV stationary=weights, moving=ifmap convention; CONFIRMED at all three TKG sites):

| Matmul | `stationary` | `moving` | Contracts over | PSUM output | Line |
|---|---|---|---|---|---|
| MM1 (QK) | `k_tile` `[d_head, s_prior_tile]` | `q_sb` `[d_head, s_active_qh]` | `d_head` (partition) | `[s_prior_tile, s_active_qh]` = KᵀQ | `:1974` |
| MM-sum | `qk_io` `[s_prior_tile, s_active_qh]` | `one_vec` `[s_prior, 1]` | `s_prior` (partition) | `[s_active_qh, 1]` = Σexp | `:2589` |
| MM2 (PV) | `v_sb` `[s_prior_tile, d_head]` | `qk_io` `[s_prior_tile, s_active_qh]` | `s_prior` (partition) | `[d_head, s_active_qh]` = (P·V)ᵀ | `:2952` |

Normalization is **multiply-by-reciprocal**, applied at the PSUM→SBUF flush of MM2 (`tensor_tensor(exp_v_psum × exp_sum_recip)`, `:2973`) on the non-flash path. On the flash-attention path (`s_prior` per-shard > 8192) the PV output is left **unnormalized** and the `× recip(running_sum)` is deferred to `_fa_finalize_and_store` (`:2966-2969`, finalize `:2246`).

### Considerations

The max-reduce has a numerical guard worth reproducing exactly. If a query row's whole tile is masked, every score is `−inf`, and `score − score_max = −inf − (−inf) = NaN`. The kernel clamps the per-row max to `_MIN_FLOAT32 = float(np.finfo(np.float32).min) = −3.4028235e38` (`:53`, applied `:2305`) so the subtraction stays finite; `exp(−inf − finite) = 0` then drops the masked positions cleanly. The running max may be stored **negated** (`atp.max_negated`, `:549`) so the flash running-max update is a `minimum` instead of a `maximum`, saving an op in the sink-exp path (`:2149-2169`, gate logic `:1147-1148`).

---

## KV-cache layouts and access

### Purpose

The KV cache is what makes decode memory-bound, so the kernel supports two physical layouts selected purely by **which inputs are present** (there is no cfg enum). Flat is the simple contiguous cache; paged/block is the vLLM-style page-table cache that lets unrelated requests share physical blocks.

### Flat (contiguous) cache

```text
k_prior : [B+, 1, s_prior, d]   (cfg.tp_k_prior=True  → kernel transposes on load)
       or [B+, 1, d, s_prior]   (cfg.tp_k_prior=False → already transposed, direct load)
v_prior : [B+, 1, s_prior, d]
```

(`B+` is a possible extra garbage-buffer batch; the kernel uses the first `B` only, `:77-79`.) The K read picks one of three paths by dtype and transpose flag:

```c
// load_k_tile (flat), _compute_qk_matmul region        :1738-1797
read_offset = sprior_prg_id * s_prior + fa_tile_offset      // :1738  LNC2 shard + FA-tile offset
if not cfg.tp_k_prior:
    k_tile = dma_copy(k_prior[b, :, read_offset:…])         // :1745  direct, already transposed
elif atp.use_dma_transpose:                                  // :821  d_head==128 ∧ 2-byte ∧ ¬fp8
    k_tile = dma_transpose(k_prior[b, …])                    // :1797  HW DMA transpose
else:  // FP8 cache: "Can't do DMA transpose for FP8"
    k_bf16 = dma_copy(k_prior, as=bf16)                      // :1757  load as bf16
    k_tile = nc_transpose(k_bf16) ; cast → fp8               // :1757-1784  PE transpose via PSUM
```

The V read is strided or sequential depending on `cfg.strided_mm1` (default `True`, `utils:71`): `strided_mm1` reads **K** strided in MM1 so the MM2 **V** reads stay sequential for better DMA throughput (`:2749-2779`, docstring `:184-187`).

### Paged / block cache — the indirect-DMA gather

The paged path activates when `active_blocks_table is not None` (`atp.is_block_kv`, `:745`). It requires `qk_in_sb=True`, `tp_k_prior=True`, and `¬strided_mm1` (asserts `:893-894`, `:908`; block-KV is always non-strided).

```text
k_prior/v_prior   : [B+ · block_count, block_len, d]   → reshaped [num_blocks·resize, block_len·d]  (:950)
active_blocks_table : [B, num_blocks_per_batch]  dtype uint32   (THE PAGE TABLE, :888)
```

The cache read is an **indirect (gather) DMA** keyed by the page table — this is the vLLM-style paged-attention lookup. The block indices for the current fold (`cur_blks`) are loaded from `active_blocks_table` and handed to the DMA as a `vector_offset`:

```c
// block-KV K gather, _compute_qk_matmul          :1674-1688  [CONFIRMED verbatim]
dma_copy(dst=k_loaded,
         src=k_prior_reshaped.ap([[block_len*d_head, P_MAX],
                                  [1, block_len*d_head]],
                                 offset=0,
                                 vector_offset=cur_blks,   // per-fold column of block indices
                                 indirect_dim=0),          // → indirect/gather on the block axis
         oob_mode=oob_mode.error)
// then PE-transpose per fold (transpose_grp_size=min(8,block_len), :1692)
// or single indirect dma_transpose per fold if use_dma_transpose (:1639)
```

> **GOTCHA — `oob_mode.skip` is defined but dead on this path.** `OOB_MODE_SKIP = nisa.oob_mode.skip` carries a `"FIXME: needs to be instantiated externally from kernel"` comment (`:409`), but every block-KV gather uses `oob_mode.error` (`:1686`, `:2745`). A reimplementer wiring out-of-bounds page indices to silent-skip semantics is reproducing dead intent, not shipped behavior. The page table must contain only in-range block indices, or the gather faults.

The active-block table is itself loaded and reshaped per `(FA-tile, batch-tile)` by `_load_and_reshape_active_blk_table` (`:3369`), spreading 128 consecutive blocks across partitions; `resize_factor > 1` expands each index to `blk_idx·resize_factor + arange(resize_factor)` (`:3411-3427`). `resize_cache_block_len` (`utils:241`) *reduces* `block_len` when blocks-per-batch falls below `sprior_n_prgs·p_max`, raising the block count so `p_max` blocks land on the 128 partitions in parallel (it warns when the reduced length drops below 8 → poor DMA bandwidth). The torch reference makes the gather explicit: `block_cache[active_blocks_table[b]]` (`_gather_block_kv_to_flat`, `attention_tkg_torch.py:252-259`).

### New-token K/V append

The kernel does **not** write back to the HBM cache. It appends `k_active`/`v_active` into the in-SBUF working tile at the cache **tail**, only on the last NC shard and last FA tile (`atp.sprior_prg_id == sprior_n_prgs-1 ∧ is_last_fa_tile`):

```c
// flat append                                              :1891-1898 / :2929-2934
k_sb[:, fa_tile_s_prior - s_active : fa_tile_s_prior] = k_active   // :1891  tail columns
v_sb[bottom-right s_active, d]                          = v_active   // :2929
```

So the matmul sees `[cached_prior ‖ current_active]` as one fused `s_prior` axis — the kernel realization of the reference's `k_prior[...,-s_active:] = k_active`. The persistent RoPE'd-K write to the HBM cache (`k_out`) is a *separate* store (`:1557-1565`), emitted only when `fuse_rope` is set. The block-KV append (`:1801-1889` / `:2783-2873`) is the same idea but places the active rows into the last fold's partitions, accounting for block-boundary `extra_covered` padding.

`cfg.curr_sprior` is the actual cache content this step; `cfg.full_sprior` is the allocated bucket (`utils:51-55`). Flat loads slice the active window; the mask zeroes positions `≥ curr_sprior` (the `iota < pos_id` cut, §[gen_mask_tkg](#gen_mask_tkg--the-01-multiplicative-mask)).

---

## GQA / MQA grouped-query broadcast

### Purpose

The kernel is MQA-shaped: one shared KV head per batch, `H` query heads sharing it. The win is that the shared-KV "broadcast" across the query-head group costs nothing — it is the matmul's *stationary* operand, reused for every moving column.

### Algorithm

```c
// GQA layout + the free-broadcast matmul                   :585, :827, :1974
s_active_qh = cfg.s_active * cfg.q_head                      // :827  flatten [q_head, s_active]
q_sb : [d_head(part), B · s_active_qh]                       // :585  whole q-head group laid flat
// MM1: ONE shared K (stationary) × the ENTIRE q-head group (moving) in one pass
qk_psum = nc_matmul(stationary=k_tile,        // one shared K tile per batch
                    moving=q_sb_view)         // width = s_active_qh  → all H heads at once
// MM2 reuses the one V tile (stationary) against the whole group of exp scores (moving)  :2952
```

All `H` query heads of a batch multiply against the one shared K in a single `nc_matmul` — no explicit K replication. The caller `attention_block_tkg` hard-codes `kv_heads=1` ("Supports grouped-query attention (GQA) with a single key/value head"), and the KV tensors carry head dim = 1 (`k_prior/v_prior [B+,1,s_prior,d]`, `:102-105`).

> **QUIRK — the GQA broadcast is free because it is the stationary operand.** A naive implementation replicates K across the `q_head` group and pays the replication in SBUF and DMA. Here the group is flattened into `s_active_qh = s_active · q_head` (`:827`) and ridden as the *moving* operand against the single stationary K/V tile. The stationary operand is loaded once into the PE array and reused for every moving column, so KV reuse across heads is intrinsic to the matmul — zero extra memory, zero extra DMA. This is the decode kernel's main arithmetic-density lever, and it works only because `s_active · q_head` fits on ≤128 partitions (no Q-tiling, unlike prefill).

RoPE applies per-q-head to Q but with `ignore_heads=True` for `k_active` (`:1554-1555`) — one rotation for the shared K head. The output layout encodes the grouping: SBUF `res[d_head, s_active · n_qhead_per_kvhead]` → DRAM `out[B,H,d,s_active]` via permute (`_gather_and_store_output`, `:3036-3045`; the comment literally names `n_qhead_per_kvhead`). Sink and mask are per-q-head (`sink [1,H]` bcast over batch `:3300`; mask 5D `…,q_head,s_active`).

---

## gen_mask_tkg — the 0/1 multiplicative mask

### Purpose

`gen_mask_tkg` builds one mask per `(batch, q_head, query)` over the prior KV cache, in the exact SBUF/HBM layout `attention_tkg` consumes. It generates three logically distinct masks selected at **trace time** by which optional args are present, and it emits a **multiplicative boolean** — `1 = attend, 0 = masked` — never `−inf`.

### The three mask types

```c
// (A) CAUSAL / PADDING  (default; start_pos is None)        :401-413  [CONFIRMED]
cur_mask = tensor_scalar(data=mask_iota, op0=nl.less,
                         operand0=pos_ids[:, batch*s_active])     // = (iota < pos_ids[b])
// pos_ids[b] = cache length / next-write position. Because the active query SITS at
// position pos_ids[b], "index < pos_ids" is simultaneously the causal cutoff (no future)
// AND the padding cutoff (no unwritten slots). Golden: (k_indices < cache_lens), torch :186.

// (B) SLIDING WINDOW  (start_pos provided)                  :467-549  [CONFIRMED]
ge   = (iota >= start)                  // greater_equal
lt   = (iota <  end)                    // less,  end = pos_ids
norm = ge * lt                          // AND via multiply
wrap = max(ge, lt)                      // OR  via maximum  (circular cache, window straddles end)
final = norm + (start>end) * (wrap - norm)   // = select(start>end, wrap, norm)   :525-549
// Built BRANCHLESS — start/end are runtime data; no Python `if` on them.

// (C) ACTIVE  (active_mask provided)                        :552-681  [CONFIRMED]
// The last s_active KV slots are the current query block. Mask is NOT computed — it is a
// caller-supplied tensor DMA-loaded onto the tail of mask_out. For s_active>1 (spec decode)
// the golden supplies tril(ones(s_active,s_active)): the active sub-block is itself causal.
```

So the only true lower-triangular causal structure in decode lives in the `s_active × s_active` active sub-block (case C); the prior block is the flat `< cache_len` cut (case A).

### The iota → compare construction

The index tensor comes from `_generate_iota_tensor` (`:294-365`). The per-element value is `offset + Σ(step_d · idx_d)` along the free access-pattern ramps **plus** `channel_multiplier · partition_index` on the partition axis — the standard iota value formula. The layout differs by cache type:

| Layout | `iota_pattern` | `channel_mult` | `iota[p,f]` | maps linear `k` to | Line |
|---|---|---|---|---|---|
| Flat, strided (default) | `[[1, n_sprior_tile]]` | `n_sprior_tile` | `off + f + p·n_sprior_tile` | `(p=k//nst, f=k%nst)` | `:350-365` |
| Flat, non-strided | `[[P_MAX, n_sprior_tile]]` | `1` | `off + f·P_MAX + p` | `(p=k%P_MAX, f=k//P_MAX)` | `:357-358` |
| Block / paged | `[[1, block_len]]` per fold | `block_len` | `fold_base + p·block_len + f` | shuffled to block layout | `:327-348` |

The iota is replicated `s_active_qh = q_head · s_active` times (`:235-249`) so one `tensor_scalar` compare covers all heads and queries. `tensor_scalar` requires fp32 operands (HW constraint, `:980-982`), so integer `pos_ids` are cast to fp32 in the HBM wrapper before broadcast.

### How the consumer applies it — the lazy −inf

```c
// attention_tkg.py: 0/1 mask → -inf masking, NO add
memset(bufs.qk, value=-np.inf)                       // :1040  prefill scores buffer with -inf
// ... MM1 lands QKᵀ in qk_psum ...
tensor_copy_predicated(src=qk_psum, dst=qk_sb,       // :2005
                       predicate=mask_sb)            // mask_sb dtype = uint8  (:1045)
//   dst[i] = src[i] where predicate[i]!=0, else dst[i] LEFT UNCHANGED.
//   masked positions (mask==0) KEEP the prefilled -inf; valid positions get the real score.
// → online softmax: exp(-inf - max) = 0 → masked positions contribute 0 to sum and to P·V.
```

So decode realizes masking as `select(mask, score, −inf)` feeding an ordinary additive-free softmax. The `−inf` is injected once by the prefill + predicated copy, never re-added per tile. The mask is consumed two ways (`:1430-1503`): `cfg.use_pos_id=True` builds it **in-kernel** by calling `gen_mask_tkg()` straight into `bufs.mask_sb` (`:1485-1502`, saving HBM bandwidth); `cfg.use_pos_id=False` DMA-loads a **pre-generated** HBM mask (output of `gen_mask_tkg_hbm`, `:1430-1468`).

> **CORRECTION — mask representation.** Any prior claim that "the attention mask is additive `−inf` everywhere" is wrong. Only the **prefill** kernel (`attention_cte`) emits the `−inf` value: it writes `_FLOAT32_MIN = −3.4028235e38` (`attention_cte.py:135`) directly into the scores via `nisa.affine_select` (static causal/SWA, `cmp_op=nl.greater_equal`, `on_false_value=_FLOAT32_MIN`, `:2691`/`:2704`) and `nisa.range_select` (dynamic, `comp_op0`/`comp_op1` + `bound0`/`bound1` + `on_false_value=_FLOAT32_MIN`, `:2738`). Strictly it is a *select/overwrite* — the masked branch replaces the score with `_FLOAT32_MIN`, not a literal `score + (−inf)` add — but it is semantically the additive-`−inf` bias and produces the masked value *in the same instruction that masks*. The **decode** kernel (`attention_tkg`) instead keeps a 0/1 multiplicative mask (fp32 in HBM, uint8 in SBUF) and converts it to `−inf` lazily via `tensor_copy_predicated` into a separately `−inf`-prefilled buffer. `attention_cte` uses **no** `tensor_copy_predicated` and **no** `−np.inf` memset (it reuses `_FLOAT32_MIN` even for the running-max init, `:2107`/`:2353`); `gen_mask_tkg` never materializes `−inf`, never adds to scores, and contains no `exp`/`softmax` — it is purely an index/compare/copy builder. Two kernels, two representations. (CONFIRMED both sides; D-O16 §7.)

> **NOTE — SWA appears in two forms.** The two-sided band `[start,end)` is open-coded here with `tensor_scalar`/`tensor_tensor` (multiply for AND, maximum for OR), whereas `attention_cte` expresses the same band with the `nisa.range_select` primitive. A reader cross-referencing the range-select primitive should expect the band-select in both a primitive form (CTE) and a hand-rolled tensor-op form (TKG).

### Sharding and engine balancing

`gen_mask_tkg_hbm` (`:860-1117`) makes the LNC2 shard decision (`is_s_prior_sharded` / `is_batch_sharded`), tiles SBUF over `s_prior` and batch, broadcasts `pos_ids` across all `P_MAX` partitions (so the per-partition compare sees the same scalar), loops into `gen_mask_tkg`, and DMA-stores to `[n_sprior_tile, P_MAX, bs, q_head, s_active]` HBM. Batch/query copies alternate `scalar_engine`/`vector_engine` by index parity (`:420-423`, `:546-549`) — pure throughput balancing, no semantic effect.

> **GOTCHA — a live layout TODO on the `use_pos_id=False` strided path.** `attention_tkg.py:1449-1453` carries a shipped TODO: the `strided_mm1` flat-KV mask *load* reshapes with `reshape_dim(0, [P_MAX, n_sprior_tile])` (P_MAX-major) which the comment flags as "inconsistent with the n_sprior_tile-major HBM layout", "kept as-is pending end-to-end validation". A reimplementer following this path should validate mask alignment end-to-end rather than trusting the strided-load reshape.

---

## Decode (TKG) vs context (CTE) — the diff

The two kernels are the same softmax math fitted to opposite bottlenecks. The contrast is the fastest way to understand why decode is shaped as it is ([6.7.6](attention-cte.md) owns the prefill side).

| Axis | `attention_tkg` (DECODE) | `attention_cte` (CONTEXT/PREFILL) |
|---|---|---|
| Query | 1..few tokens (`s_active ≤ 7`); **no Q-tiling** — whole q-head group on ≤128 partitions | full prompt; Q tiled in groups (`_load_q_tile`, `q_grp`) |
| Causal masking | **none** in-kernel (`grep causal`=0); validity/SWA/spec-block only | heavy: `_has_any_compute_causal`, range-select; `causal`×65 |
| Mask representation | **multiplicative 0/1**, lazy `−inf` via predicated copy | **additive `−inf`** via `affine_select`/`range_select`, `_FLOAT32_MIN` |
| KV append | current K/V appended to cache tail in SBUF only | prior + active both span the prompt; no append |
| Bottleneck | **memory-bound** — KV-cache DMA dominates (small Q, big `s_prior`) | **compute-bound** — big QKᵀ/PV GEMMs over long seqlen |
| Score orientation | kept **transposed** `[s_prior, s_active_qh]` | standard `[s_q, s_k]` |
| Sum reduction | **ones-vector matmul** | tile reduce / running sum |
| `nc_matmul` sites | **3** (QK, ones-sum, PV) | 65 (tiled GEMMs) |

Both share: stable `exp(x−max)/Σ` softmax, PSUM accumulation, LNC2 sharding (batch- or `s_prior`-sharded via `sendrecv`/gpsimd), 8K flash-attention tiling with running max/sum and `exp(prev_max − curr_max)` correction, FP8-e4m3 KV, attention sink, and SWA. TKG is the decode counterpart that strips Q-tiling and causal logic and adds the cache-append plus paged-KV gather.

> **NOTE — prefix caching is a separate, compiled kernel.** There is no `attention_prefix_caching.py` in nkilib. The shared-prefix path ships as a compiled Cython module `neuronxcc/nki/_private_kernels/prefix_caching_attention.cpython-310-…so` (symbols `attention_prefix_caching_fwd_kernel`, backend variant `V2CausalAttentionMMSoftmaxMM`). That is the BIR/native-CC attention lowering, distinct from this NKI nkilib kernel. `attention_tkg`'s own block-KV path plus the validity mask already provide shared-prefix reuse at the KV-cache level.

---

## NISA primitive histogram

The authoritative per-op count for `attention_tkg` (use it to ground any inflated count claims; CONFIRMED, D-O14 §6):

```text
dma_copy ×32   tensor_copy ×24   tensor_tensor ×18   tensor_scalar ×12
nc_transpose ×9   memset ×9   activation ×8   sendrecv ×6   tensor_reduce ×5
nc_matmul ×3   reciprocal ×2   dma_transpose ×2   tensor_copy_predicated ×1
scalar_tensor_tensor ×1   iota ×1
```

The shape tells the story: `dma_copy ×32` (the memory-bound cache read) dominates, `nc_matmul ×3` (the tiny decode arithmetic), and `tensor_copy_predicated ×1` (the single mask application).

## Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `attention_tkg` | `:56-394` | kernel entry + main `batch_tile → FA_tile` loop | CONFIRMED |
| `_compute_qk_matmul` | `:~1600-2010` | K load (flat/transpose/gather), MM1, mask predicated copy | CONFIRMED |
| `_cascaded_max_reduce` | `:2060` | online max over `s_prior` tiles + transpose/clamp | CONFIRMED |
| `_compute_exp_qk` | `:2318` | `exp(qk ± qk_max)` → io dtype | CONFIRMED |
| `_tile_sum_reduction` | `:2563` | **ones-vector matmul** per tile → Σexp | CONFIRMED |
| `_cascaded_sum_reduction` | `:2395` | sum across tiles + reciprocal + broadcast | CONFIRMED |
| `_compute_pv_matmul_and_store` | `:2650` | V load/append, MM2, normalize, store | CONFIRMED |
| `_setup_block_kv_cache` | `:867` | paged-cache setup, `uint32` table assert | CONFIRMED |
| `_load_and_reshape_active_blk_table` | `:3369` | page-table load + resize_factor expand | CONFIRMED |
| `_perform_rope` / `_apply_rope` | `:~1500-1567` | RoPE + fused `1/sqrt(d_head)` Q scale | CONFIRMED |
| `_prep_sink` | `:3285` | attention-sink load + replicate `[1,H]→[BHS,1]` | CONFIRMED |
| `gen_mask_tkg` | `gen_mask_tkg.py:53-286` | in-SBUF mask entry (iota → compare → active) | CONFIRMED |
| `_create_batch_masks` | `gen_mask_tkg.py:368-423` | standard `iota < pos_ids` | CONFIRMED |
| `_create_batch_masks_swa` | `gen_mask_tkg.py:426-549` | branchless `[start,end)` band | CONFIRMED |
| `gen_mask_tkg_hbm` | `gen_mask_tkg.py:860-1117` | HBM entry: shard decision + tiling + store | CONFIRMED |

## Related Components

| Name | Relationship |
|---|---|
| `attention_cte` ([6.7.6](attention-cte.md)) | context/prefill sibling — additive `−inf`, Q-tiling, causal |
| `prefix_caching_attention.*.so` | separate compiled native attention for shared prefix |
| `TensorCopyDynamic` ([5.24](../penguin/tensorcopy-dynamic-generators.md)) | the gather/scatter dynamic-DMA generator family behind paged-KV |
| `nkilib` infra ([6.7.1](nkilib-infrastructure.md)) | `SbufManager`, `TileConstants`, `TensorView` used throughout |

## Cross-References

- [nkilib Infrastructure: Allocator, Tiling & Common Types](nkilib-infrastructure.md) — `SbufManager`/`alloc_stack`, `TileConstants.p_max`, `TensorView` AP model this kernel builds on
- [Flash-Attention: Context (CTE)](attention-cte.md) — the prefill sibling; the additive-`−inf` half of the masking contrast
- [TensorCopyDynamic Generators](../penguin/tensorcopy-dynamic-generators.md) — gather/scatter dynamic tensor-copy; the backend mechanism for the paged-KV `vector_offset`/`indirect_dim` DMA
- [Worked Example B — flash-attention end-to-end](../front/worked-example-flash-attention.md) — how an `@nki.jit` attention kernel descends trace → Penguin → BIR → NEFF
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition SBUF and 2 KiB PSUM banks the tiling and ones-vector matmul target
