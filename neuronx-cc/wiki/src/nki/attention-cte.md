# Flash-Attention: Context (CTE)

> *All symbols, line numbers, and constants on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/cp311/cp312 — the kernel body is byte-identical across the three wheels). The source is `nkilib/core/attention/attention_cte.py` (2920 lines), a readable `@nki.jit` NKI Python kernel shipped inside the wheel; it is a binary-derived artifact, not stripped. Every `nisa.*` primitive named here is defined in the `neuronxcc/nki/isa` Cython modules; cross-check against [nki.isa COMPUTE Intrinsics](isa-compute-intrinsics.md) and [REDUCE / SELECT / DVE Intrinsics](isa-reduce-dve-dma.md).*

## Abstract

`attention_cte` is the **prefill / context-encoding** flash-attention kernel for the Trainium/Inferentia TPB. It computes `O = softmax(scale · Q·Kᵀ) · V` for one `(batch·head)` attention problem with a large query sequence (`seqlen_q` ~256..36864) and head dimension `d ≤ 128`. It is the kernel a framework's fused-attention custom-call lowers to (via `DecomposeAttention`, `hlo-opt` pass #38) and the kernel the [0.8 worked example](../front/worked-example-flash-attention.md) traces end-to-end; **this page is the kernel-level reimplementation companion** to that walkthrough — where the worked example shows the *descent* (`@nki.jit` → trace → Penguin IR → BIR → NEFF), this page shows the *algorithm that descends*.

The algorithm is textbook flash-attention — online softmax with running per-row max and sum, the accumulator rescaled by `α = exp(m_old − m_new)` whenever a new KV section lowers the max — but the TPB implementation is shaped by two hardware facts that a reimplementer must reproduce exactly. First, the **row-max is stored negated** throughout: `mm1_running_max` holds `−max(S)`, never `+max(S)`. This is not a cosmetic choice — it lets the running-max update be a `minimum` (min of two negated maxes is the negated of their max) and, more importantly, lets the per-element `exp(S − max)` be a *single fused activation* whose additive bias operand **is** the stored negated max (`bias = mm1_running_max`). No subtract instruction is ever emitted for the softmax shift. Second, the partial output `O` is **round-tripped through HBM** between sections rather than kept resident in SBUF: across 8K-token sections the running max and sum stay in SBUF, but `O` is DMA-written, then re-read and rescaled by `α` each section — an SBUF-budget tradeoff, not an oversight.

The page is organised by subsystem: the **online-softmax recurrence** (the negated-max trick, the `α` rescale, the running sum, the reciprocal normalize); the **tiling** (Q-outer 128-groups, KV-inner 512/2048 tiles, the "Q resident, KV streams" residency, the 3-stage software pipeline); the **masking** (causal compute-skip, the `affine_select` diagonal iota mask, the SWA second affine-select, and the dynamic `range_select` family for CP / sequence-packing / prefix-caching); **GQA / multi-head**; and **scale**. Each subsystem closes with the exact `nisa` primitive sequence and SBUF/PSUM residency.

For reimplementation, the contract is:

- The **negated-max online-softmax recurrence**: `mm1_running_max = min(−max_old, −max_new) = −max_new`; `α = exp((+max_old) + (−max_new)) = exp(max_old − max_new)`; `l_new = α·l_old + l_section`; `O_acc = α·O_prev + O_section`; final `O = O_acc · (1/l)`. Get the negation byte-exact — every sign here is deliberate.
- The **fused exp**: `exp(S − max)` is emitted as `activation_reduce(op=exp, data=S, bias=−max, reduce_op=add → partial_sum)` — one HW pass does the shift, the exponential, and the running-sum accumulation.
- The **tiling and residency**: `Q_GRP=128` (= SBUF partition dim), `K_TILE=512`, `V_TILE=128`, `LARGE=2048`; Q transposed (`d` on partition) and resident; K/V re-loaded per section; PSUM banks 0–3 for MM1, 4–7 for MM2.
- The **masking dispatch**: static `affine_select` (scale-aware) for plain causal; dynamic `range_select` (requires `scale==1.0`) for CP / sequence-packing / prefix-prior / SWA≤128; causal compute-skip via `_has_any_compute_*` drops upper-triangle tiles entirely.

| | |
|---|---|
| **Source** | `nkilib/core/attention/attention_cte.py` (2920 lines) |
| **Entry point** | `@nki.jit attention_cte(...)` — line 168 |
| **Math** | `O = softmax(scale · Q·Kᵀ) · V`, prefill/causal |
| **Head dim** | `d ≤ 128` (`_MAX_HEAD_DIM`, line 143) — one PE pass, no head-dim tiling |
| **Tiling** | `_Q_GRP_SZ=128`, `_K_TILE_SZ=512`, `_V_TILE_SZ=128`, `_LARGE_TILE_SZ=2048` (lines 156–160) |
| **Sectioning** | KV in 8K sections above `_FLASH_ATTENTION_THRESHOLD=10·1024` (lines 161–162) |
| **Mask sentinel** | `_FLOAT32_MIN = -3.4028235e38` (line 135) → masked logit → `exp → 0` |
| **MM1 / MM2** | `nc_matmul(stat=Q[d,q], mov=K[d,k])` → `nc_matmul(stat=Pᵀ[kv,q], mov=V[kv,d])` |
| **Lowered from** | `DecomposeAttention` (`hlo-opt` #38) recognises matmul→masked-softmax→matmul |

---

## The online-softmax recurrence

### Purpose

Flash-attention never materialises the full `seqlen_q × seqlen_kv` score matrix. The KV axis is processed in **sections** (outer flash-attention blocks, 8K tokens each above the threshold). For each Q row, the kernel carries a running maximum `mᵢ` and a running denominator `lᵢ` across sections; when a later section produces a higher score than the running max, the partial output and the running sum accumulated so far are rescaled by `α = exp(m_old − m_new) ≤ 1`. The single most important reimplementation detail is that **every per-row max is stored negated** (`−max`), which the next three subsections justify mechanically.

### Algorithm — the per-section max update

This is `_update_max_impl` (lines 2113–2171). The section-max reduce is computed with `negate=True`, so `mm1_section_max` already holds `−max(S_section)`.

```c
function _update_max_impl(grp, ...):                       // lines 2113-2171
    if causal and tile fully masked: return                // _has_any_compute_causal guard, line 2122

    // Step 1: section max, NEGATED. mm1_partial_max holds per-512-tile maxes.
    if sink and section_idx == 0:                          // line 2132
        copy sink logit into mm1_partial_max[:, num_k_tiles] // append sink column
    tensor_reduce(mm1_section_max[grp], op=maximum,
                  mm1_partial_max[grp], axis=1, negate=True) // line 2135  -> mm1_section_max = -max(S_section)

    // Step 2: running max + correction factor alpha
    if num_sections != 1:
        if section_idx == 0:                               // first section: seed
            tensor_copy(mm1_running_max[:,grp], mm1_section_max[grp]) // line 2146  running = -max_0
            memset(flash_attn_correction_factor[grp], 0.0)           // line 2147  (unused for sect 0)
        if section_idx > 0:
            // de-negate the OLD running max:  -1 * (-max_old) = +max_old
            activation(prev_mm1_running_max[grp], op=copy,
                       mm1_running_max[:,grp], scale=-1.0,
                       bias=zero_bias_tensor)                        // line 2149  prev = +max_old
            // running-max update via MIN of two NEGATED maxes:
            //   min(-max_old, -max_new) = -max(max_old, max_new) = -max_new
            tensor_tensor(mm1_running_max[:,grp], mm1_running_max[:,grp],
                          mm1_section_max[grp], op=minimum)          // line 2156  running = -max_new
            // alpha = exp( (+max_old) + (-max_new) ) = exp(max_old - max_new)
            activation(flash_attn_correction_factor[grp][:,0], op=exp,
                       prev_mm1_running_max[grp],
                       bias=mm1_running_max[:,grp], scale=1.0)       // line 2162  alpha = exp(m_old - m_new)
    else:
        tensor_copy(mm1_running_max[:,grp], mm1_section_max[grp])    // single section: running = -max
```

The arithmetic, made explicit because the signs are the whole point:

| Quantity | Stored value | Computed by |
|---|---|---|
| `mm1_section_max` | `−max(S_section)` | `tensor_reduce(maximum, negate=True)` (line 2135) |
| `mm1_running_max` | `−max(all sections so far)` | `minimum` of two negated maxes (line 2156) |
| `prev_mm1_running_max` | `+max_old` | `activation(copy, scale=−1)` (line 2149) |
| `flash_attn_correction_factor` (α) | `exp(max_old − max_new)` | `exp(prev + running)` = `exp((+max_old)+(−max_new))` (line 2162) |

> **QUIRK — the running-max update is a `minimum`, not a `maximum`.** Because the maxes are stored negated, `min(−a, −b) = −max(a, b)`. A reimplementation that stores `+max` and uses `maximum` here is arithmetically equivalent for the max itself but then needs a *separate* subtract for the exp shift and a *separate* negate for the correction-factor exponent. The negated representation collapses both into existing operands: the correction factor's `exp(max_old − max_new)` is literally `exp(prev_running_max + running_max)` with no extra negate, because `running_max` is already `−max_new`.

### Algorithm — fused exp and the running sum

`_exp_impl` (lines 2173–2309). The exponential of the masked, scaled scores, the per-512-column partial sum, and the score shift `S − max` are **all one `activation_reduce` instruction** per 512-wide sub-tile:

```c
function _exp_impl(grp, ...):                              // lines 2173-2309
    if causal and tile fully masked: return                // line 2182
    memset(exp_partial_sum[grp], 0.0)                       // line 2191
    for large_tile in range(num_large_tiles_per_section):   // 2048-tiles
      assert exp_inst_elems == 512                          // line 2194
      for exp_tile in range(num_exp_insts_per_large_tile):  // 512-cols each
        if causal compute-skip says this tile empty: continue // _has_any_compute_causal, line 2214
        if use_swa and skip: continue                        // _has_any_compute_swa, line 2221
        if seqlen_k > k_start_pos:
            // ONE op: exp( S - max ) AND sum over the 512 columns
            activation_reduce(
                out        = exp_sb[grp][large_tile][:num_p, exp_tile*512 : +num_f],
                op         = exp,
                data       = mm1_masked[grp][large_tile][...],   // scaled+masked scores S
                reduce_op  = add,
                reduce_res = exp_partial_sum[grp][:, large_tile*N + exp_tile],  // Σ over 512 cols
                bias       = mm1_running_max[:num_p, grp])       // bias = -max  =>  exp(S - max)
                                                                 // lines 2225-2237
            dma_transpose(exp_sb -> exp_tp_sb[128,4,128])        // KV onto partition for MM2, lines 2258/2280
    if sink and section_idx == 0:
        exp_partial_sum[grp][:, last] = exp(sink, bias=-max)     // append sink to sums, lines 2302-2309
```

> **GOTCHA — `bias = mm1_running_max` IS the softmax shift.** The activation primitive computes `out = op(scale·data + bias)`. With `scale=1`, `data=S`, `bias=−max`, this is exactly `exp(S − max)`. There is no separate "subtract the row max" instruction anywhere in the kernel. A reimplementer who emits `S − max` as its own op and then `exp` of the result wastes a full SBUF pass and a buffer; the hardware folds the subtraction into the activation's bias adder for free. The same fold appears in the correction-factor `exp` (line 2162) and the sink `exp` (line 2304).

### Algorithm — running-sum update, reciprocal, output accumulate

`_write_back_impl` (lines 2391–2523). The section's partial sum is reduced to a per-row scalar, combined with the running sum via `l_new = α·l_old + l_section`, and the reciprocal is taken once on the last section. The partial output is accumulated `O_acc = α·O_prev + O_section`, where `O_prev` is **re-loaded from HBM**.

```c
function _write_back_impl(grp, ...):                       // lines 2391-2523
    // ---- running sum ----
    tensor_reduce(exp_section_sum[grp], add, exp_partial_sum[grp], axis=1) // line 2393  l_section
    if num_sections != 1:
        if section_idx == 0:
            tensor_copy(exp_running_sum[:,grp], exp_section_sum[grp])      // line 2396  l = l_section
        if section_idx > 0:
            tensor_copy(prev_exp_running_sum[grp], exp_running_sum[:,grp]) // line 2398
            // l_new = prev * alpha + l_section   (op0 multiply, op1 add)
            tensor_scalar(exp_running_sum[:,grp], prev_exp_running_sum[grp],
                          op0=multiply, operand0=flash_attn_correction_factor[grp],
                          op1=add,      operand1=exp_section_sum[grp])      // lines 2402-2409
        if last section:
            reciprocal(exp_sum_reciprocal[:,grp], exp_running_sum[:,grp])  // line 2411  1/l
    else:
        reciprocal(exp_sum_reciprocal[:,grp], exp_section_sum[grp])        // line 2416 (single section)

    // ---- output accumulate (non-tp_out path) ----
    if num_sections != 1:
        if section_idx == 0 and not last:
            _write_back_o_impl(mm2_sb[grp], ...)            // line 2441  write UNNORMALIZED O to HBM
        if section_idx > 0:
            dma_copy(mm2_prev_output[grp] <- o[batch, grp_slice])         // line 2487  RE-LOAD O from HBM
            // O_acc = alpha * O_prev + O_section
            scalar_tensor_tensor(mm2_accum_flash_attn[grp],
                data=mm2_prev_output[grp], op0=multiply, operand0=flash_attn_correction_factor[grp],
                op1=add,                   operand1=mm2_sb[grp])           // lines 2488-2495
            if last: _scale_reciprocal_write_back_impl(mm2_accum_flash_attn[grp], ...) // normalize+write
            else:    _write_back_o_impl(mm2_accum_flash_attn[grp], ...)               // write unnormalized
    else:
        _scale_reciprocal_write_back_impl(mm2_sb[grp], ...) // single section: normalize and write
```

The final normalize multiplies `O_acc` by `1/l` and DMAs to HBM (`_scale_reciprocal_write_back_impl`, lines 2526–2565):

```c
function _scale_reciprocal_write_back_impl(src, grp, ...): // lines 2526-2565
    // non-tp_out: O_final = O_acc * (1/l), fused into an activation copy with scale=recip
    activation(mm2_final[grp][:num_p, :d], op=copy, src[:num_p, :d],
               scale=exp_sum_reciprocal[:num_p, grp],       // multiply by 1/l
               bias=zero_bias_tensor)                        // lines 2557-2563
    _write_back_o_impl(mm2_final[grp], ...)                  // DMA to HBM o
```

> **GOTCHA — the partial output is not SBUF-resident across sections, unlike the max and sum.** Lines 2487 and 2454 DMA-*load* `mm2_prev_output` back from HBM `o` on every section after the first, rescale it by `α`, add the new section's `O`, and write it out again; only `mm1_running_max`, `exp_running_sum` and `exp_sum_reciprocal` live in SBUF across sections (allocated outside the section loop, lines 690–692). The HBM round-trip is the deliberate SBUF-budget tradeoff that makes 8K-token sections fit.

### Considerations

The `tp_out` (transposed-output) path (lines 2445–2477, 2540–2555) does the same `α·O_prev + O_section` and `·(1/l)` math but transposes the correction factor and the reciprocal via `nc_transpose` because in that layout `O` is `(d, seqlen)` rather than `(seqlen, d)`, so the per-row scalars must be broadcast along the partition (`d`) axis instead of the free axis. The math is identical; only the broadcast geometry differs.

Optional training outputs: when `cache_softmax=True`, the kernel DMAs `mm1_running_max` to `out_neg_max` (the **negated** max — caller must negate) and `exp_sum_reciprocal` to `out_sum_recip` (lines 799–808) for the backward pass.

---

## The tiling

### Purpose

The tiling makes the partition-dimension constraints of the PE array (≤128 contraction lanes) and the SBUF budget work out. Q is grouped into **128-token groups** (one group fills the SBUF partition dimension, `pmax=128`, asserted equal to `_Q_GRP_SZ` at line 996). The KV axis streams in **512-column tiles** for MM1 and masking, **128-row tiles** for MM2's contraction, allocated and pipelined in **2048-element large tiles** (4×512). Above `_FLASH_ATTENTION_THRESHOLD = 10·1024` total KV length the KV axis is additionally split into **8K-token sections** (`_FLASH_ATTENTION_SECTION_LENGTH`), and KV is re-loaded per section.

### Tile shapes and residency

`_allocate_attention_buffers` (lines 1256–1539). The defining layout choice: **Q and K both carry head-dim `d` on the partition axis** (because `d` is the MM1 contraction dimension, ≤128 = one PE pass); V carries KV-seq on partition and `d` on free (because `d` is MM2's free/output dim).

| Buffer | Shape | dtype | Role |
|---|---|---|---|
| `q_sb` | `(d, 128·num_q_grps_per_load)` | bf16 | Q **transposed** (d on partition). `num_q_grps_per_load = 8` (bf16) / `4` (fp32), packed per DMA (line 1008–1009). Q stays resident across all KV tiles of a section. |
| `k_sb` | `(d, 512)` × `num_k_tiles` | bf16 | K, d on partition (MM1 contraction). Re-loaded per section. |
| `v_sb` | `(128, d)` × `num_v_tiles` | bf16 | V, KV-seq on partition (MM2 contraction). Straight DMA, no transpose. |
| `mm1_masked` | `(128, 2048)` | fp32 | scaled + masked scores `S`; partition=Q(128), free=KV(2048). |
| `exp_sb` | `(128, 2048)` | bf16 | `exp(S − max)`. |
| `exp_tp_sb` | `(128, 4, 128)` | bf16 | **transposed** exp: 4 blocks of `128(KV-part)×128(Q-free)` (`num_tps_in_mm2_grp = 512//128 = 4`, line 1066). |
| `mm2_sb` / `mm2_accum_flash_attn` / `mm2_final` | `(128, d)` | fp32 | output accumulators. |
| `mm1_running_max` / `exp_running_sum` / `exp_sum_reciprocal` | `(128, num_grps)` | fp32 | **persistent across sections** (allocated outside the section loop, lines 690–692). |

Allocation uses the `nkilib` `ModularAllocator` (see [nkilib Infrastructure](nkilib-infrastructure.md)) with `num_free_tiles` multi-buffering; the entire inner buffer set is reset to the section base address each section (lines 695, 718). PSUM banks are hand-assigned: **MM1 → banks 0–3, MM2 → banks 4–7** (lines 1481, 1528, 1535) — see [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md).

> **QUIRK — Q resident, KV streams.** The whole tiling is built so that one Q group's `q_sb` is loaded once and stays in SBUF while every KV 512-tile of the section is streamed past it. This is the opposite of the naive "tile both operands" loop and is why Q is the outer loop and KV the inner one. `num_q_grps_per_load` packs 8 Q groups (bf16) into one DMA for efficiency, but each group's scores are still computed against the full streamed KV.

### The loop nest

```text
L1  LNC2 shard (Trn2+): split batch across 2 NeuronCores              lines 457-551
     odd-batch remainder shards on seqlen_q with split factor
       _SEQLEN_SHARDING_SPLIT_FACTOR_CAUSAL = 0.65  (causal -> 65/35)  lines 153-155
       _SEQLEN_SHARDING_SPLIT_FACTOR_DEFAULT = 0.5  (else   -> 50/50)  line  152
     falls back to 1 core if seqlen_q < _MIN_SEQLEN_FOR_LNC2_SHARDING (1024)
L2  batch loop  (per core; GQA batch-id remap, see "GQA")
L3  section loop  range(num_sections), section_len ≤ 8K              K/V re-loaded per section
L4  group loop    over Q groups of 128 (num_grps = ceil(seqlen_q/128))
      L5  large-tile (2048) loop
            L6  k-tile (512) loop  -> MM1 / mask / max ; MM2 / accumulate
```

### Software pipelining

Lines 757–789. When a shard has more than one Q group, three Q groups `i, i+1, i+2` are overlapped so the PE array stays busy:

```text
grp i    : PV (MM2) + write_back
grp i+1  : EXP (+ running sum)
grp i+2  : load Q + QK (MM1) + max
```

MM2(i) and MM1(i+2) are fused into `_fused_qkmax_and_pv_impl` (line 2332/780) so the PE array interleaves the two matmuls per large tile. The prologue primes groups `i, i+1` (lines 760–767); the steady-state loop runs `i_start .. i_end−2` (line 770); the epilogue drains the last two groups (lines 784–789: `pv(end−2)`, `write_back(end−2)`, then `exp(end−1)`, `pv(end−1)`, `write_back(end−1)`).

> **NOTE — the single-section fast path (lines 748–755) skips the pipeline entirely.** When `shard_seqlen_q_length <= 1` (one Q group) the kernel runs the un-pipelined straight-line sequence `load_q → qk_and_max → update_max → exp → pv → write_back`. The pipelined and un-pipelined paths emit the same per-stage primitives; only the scheduling differs.

---

## The masking

### Purpose

Causal attention forbids query position `q` from attending to key position `k > q`. The kernel realises this in two layers: a **coarse compute-skip** that drops whole KV tiles that are entirely above the diagonal (no MM1, no exp, no MM2 — the real prefill speedup, ~½ the work), and a **fine partial mask** on the single diagonal-straddling tile that sets above-diagonal elements to `_FLOAT32_MIN` so their `exp` is `0`. Two masking *mechanisms* implement the fine layer, selected by `atp.dynamic_sel_mask`: a compile-time-static `affine_select` for plain causal, and a runtime-dynamic `range_select` for CP / sequence-packing / prefix-prior / small-window SWA.

### Algorithm — compute-skip (the causal optimization)

`_has_any_compute_causal` (lines 2884–2900) and `_has_any_compute_swa` (lines 2903–2920). A KV tile is skipped iff even the **largest** Q index in the group is below the **smallest** K index in the tile.

```c
function _has_any_compute_causal(q_grp, k_start_pos, ac, num_grps=1): // lines 2884-2900
    max_q_in_grp = q_grp*128 + 128*num_grps - 1
    if ac.cp_strided_q_slicing:                                      // line 2895
        max_q_in_grp = max_q_in_grp * ac.global_cp_deg + ac.global_cp_deg - 1  // worst-case rank
    return max_q_in_grp >= k_start_pos        // tile has compute only if largest q reaches smallest k

function _has_any_compute_swa(q_grp, k_start_pos, k_tile_size, ac):  // lines 2903-2920
    min_q_in_grp = q_grp*128 * (cp stride if strided)
    max_k_in_tile = k_start_pos + k_tile_size - 1
    return min_q_in_grp < max_k_in_tile + ac.sliding_window          // skip when q is past the window
```

This guard is consulted before MM1 (`matmul_selection`, line 2639), before exp (`exp_sel_mask`, line 2214), and before MM2 (`mm2_sel_mask`, line 2805). The strictly-upper-triangle tiles are never computed.

### Algorithm — the diagonal `affine_select` mask

Lines 2668–2721. A tile straddles the diagonal when `qkmax_grp·128 < k_start_pos + 512` (line 2668–2669). `affine_select` evaluates an affine iota predicate per element and selects `on_true` (the score) or `on_false` (`_FLOAT32_MIN`):

```c
// only on the diagonal tile (diagonal_sel_mask true), static causal, not dynamic:
tensor_copy(mm1_copy_sb, mm1_psum)                       // line 2687  affine_select needs SBUF input
affine_select(
    out               = mm1_affine_select_output,        // line 2691
    pattern           = [[-1, num_f]],                   // free (k) coordinate, slope -1
    offset            = qkmax_grp*128 - k_start_pos,      // grp*128 - k_start
    channel_multiplier= 1,                                // partition (q) coordinate, slope +1
    cmp_op            = greater_equal,
    on_true_tile      = mm1_copy_sb,                      // keep the score
    on_false_value    = _FLOAT32_MIN)                     // -3.4e38 -> exp = 0
```

The predicate evaluated per element `(partition p, free f)` is `channel_multiplier·p + pattern_slope·f + offset ≥ 0`, i.e.

```text
1·p + (-1)·f + (grp·128 - k_start_pos) ≥ 0
⇔ (grp·128 + p) - (k_start_pos + f) ≥ 0
⇔ q_pos - k_pos ≥ 0
⇔ q_pos ≥ k_pos                            (the causal predicate)
```

with `q_pos = grp·128 + p`, `k_pos = k_start_pos + f`. After masking, the **scale and the section-max reduce are folded in by one `tensor_scalar_reduce`** (lines 2714–2721): `op0=multiply by ac.scale`, `reduce_op=maximum into mm1_partial_max`. This is where `scale` is applied to the scores (see "Scale").

> **GOTCHA — `affine_select` cannot AND two triangular masks, so SWA emits a SECOND one.** Sliding-window attention also needs the lower-diagonal predicate `q_pos < k_pos + sliding_window`. One `affine_select` expresses one half-plane, so SWA runs a second `affine_select` (lines 2704–2712) with `pattern=[[1,num_f]]` (slope +1), `channel_multiplier=-1`, and `offset = k_start_pos + sliding_window − 1 − grp·128`, chaining its output from the first (`on_true_tile = mm1_affine_select_output`). The two selects compose the AND of the upper-causal and lower-window triangles.

### Algorithm — the dynamic `range_select` family

Lines 2723–2750. When bounds are unknown at compile time (CP offset, sequence-packing bounds, dynamic prefix length, small-window SWA), `atp.dynamic_sel_mask` is set and `range_select` keeps columns whose absolute KV index falls in `[bound0, bound1]`:

```c
// dynamic mask path (atp.dynamic_sel_mask or prior tile):
assert ac.scale == 1.0   // range_select has no scalar-multiply slot     line 2737
range_select(
    out          = mm1_masked[...],                                       // line 2738
    on_true_tile = mm1_psum,
    on_false_value = _FLOAT32_MIN,
    comp_op0     = greater_equal,         // keep if  range_start+col >= bound0
    comp_op1     = <varies>,              // keep if  range_start+col (op1) bound1
    bound0       = lower, bound1 = upper, // per-row fp32 bounds
    reduce_op    = maximum,               // fold the section-max reduce in
    reduce_cmd   = reset_reduce,
    range_start  = k_start_pos)           // absolute index of column 0 of this tile
```

`comp_op1` and the bounds are selected per mode (lines 2723–2735):

| Mode | `bound0` (lower) | `bound1` (upper) | `comp_op1` | Source |
|---|---|---|---|---|
| Prefix-prior tile | `range_sel_lbs_prior` (SWA) / `0` | `range_sel_ubs_prior` = `prior_used_len` | `less` (k < used_len) | lines 2724–2727 |
| Sequence-packing | `range_sel_lbs` = `bound_min` | `min(iota, bound_max)` | `less_equal` if causal else `less` | lines 2728–2731 |
| CP | `range_sel_lbs` (SWA) / `0` | `range_sel_ubs` = `iota + cp_offset` | `less_equal` (k ≤ q+offset) | lines 2732–2735 |

The bounds are built in `_setup_range_select_bounds` (lines 1072–1147) from `nisa.iota` (with `channel_multiplier` supplying the per-partition q-coordinate) plus `cp_offset` / `bound_min` / `bound_max`, in **fp32** (`range_select` supports fp32 bounds only, line 1091). For CP, `nisa.iota` fills q-positions `0..num_grps·128` and `tensor_scalar` adds the broadcast `cp_offset` (lines 1118–1131); for strided CP the iota stride is `global_cp_deg` (lines 1111–1115).

> **QUIRK — CP turns OFF static causal compute-skip.** When `use_cp` and not `cp_strided_q_slicing`, the kernel sets `atp.is_causal = False` (line 960) specifically to *disable* the `_has_any_compute_causal` tile-skip, because under context-parallel slicing the mask is dynamic and the compile-time triangle no longer matches the real attended region. Strided slicing keeps causal on (line 953–954) because the masked region is identical across ranks. A reimplementation that keeps the compile-time skip under non-strided CP will silently drop tiles that the dynamic mask would have kept — wrong results, not a crash.

### Considerations

The mask sentinel is `_FLOAT32_MIN = -3.4028235e38` (line 135); after the `bias = −max` shift, `exp(−3.4e38 − max) = 0`, so masked columns contribute nothing to the softmax numerator or the running sum. For `sliding_window ≤ _SWA_ALLOCATION_STRATEGY_THRESHOLD` the kernel switches SWA to `range_select` and enables `use_swa_optimized_allocation` (more Q groups, fewer K tiles, lines 981–989) to overlap more groups when each group touches only 1–2 KV tiles. SWA+CP loads only the required KV slice: `seqlen_k_active = min(seqlen_k, seqlen_q + sw − 1)` aligned to 512 (lines 977–979).

The HLO→NKI bridge that selects this kernel is `DecomposeAttention` (`hlo-opt` pass #38), which recognises the matmul→masked-softmax→matmul root and lowers `CausalAttentionMMSoftmaxMM*` to this native family; its "masking select" + "divide" correspond to this kernel's range/affine select and the final reciprocal multiply. See [Softmax Legalization](../hlo-opt/softmax-legalize.md) for the softmax decomposition this kernel's exp/sum/reciprocal mirrors.

---

## GQA and multi-head

Heads are **folded into the batch dimension by the caller**: `q` is `(batch_size, seqlen_q, d)` where `batch_size = nbatch · nheads`. The kernel never sees a head axis — one `(batch·head)` item is one independent attention problem.

Grouped-query attention is native and replication-free. `bs_kv = k.shape[0]` may be `< bs`, requiring `bs % bs_kv == 0` (assert line 384). `_q_to_kv_batch_id(batch_id, bs, bs_kv)` (line 1541) maps each Q `(batch·head)` to its shared KV group as `batch_id // (bs // bs_kv)` (line 1551) — e.g. `bs=6, bs_kv=2` yields `{0,1,2 → 0; 3,4,5 → 1}` — equivalent to a `torch.repeat_interleave` on K/V but **without physically replicating** the KV tensors. Head-dim `d ≤ 128` (`_MAX_HEAD_DIM`, line 143) occupies the partition axis of `q_sb`/`k_sb` for MM1 and the free axis of `v_sb`/output for MM2; since `d ≤ 128 = pmax`, no head-dim tiling is needed — MM1's contraction is a single PE pass.

---

## Scale

`scale` is a kernel argument (default `1.0`, line 173). It is applied **to the scores, fused into the mask/copy step** as the `op0=multiply, operand0=ac.scale` of the `tensor_scalar_reduce` that also does the row-max reduce — in the no-mask path (lines 2753–2760) and the diagonal-mask path (lines 2714–2721). So `S_scaled = scale · (Q·Kᵀ)` is computed in the same instruction as the section-max reduce; no separate scale op.

> **GOTCHA — `scale` MUST be `1.0` for SWA / prefix-caching / CP.** Those modes use `range_select`, which has no scalar-multiply slot, so the kernel asserts `ac.scale == 1.0` at line 946 (mode entry) and line 2737 (the range_select call). In those modes the caller must **pre-scale Q** before invoking the kernel (docstring lines 208–210). The kernel itself never divides by `√d`. That the caller passes `scale = 1/√d` is [INFERRED]: to the kernel `scale` is an opaque float, and `1/√d` is simply the conventional softmax temperature.

---

## nisa primitive sequence

Per Q-group `g`, per section (single-section path); KV streamed in 512/2048 tiles. SBUF/PSUM residency in brackets.

```text
LOAD Q   : dma_transpose / dma_copy   HBM q -> q_sb (d,128)            [SBUF]   line 1635
LOAD K   : dma_transpose / nc_transpose HBM k -> k_sb (d,512)*N        [SBUF]   line 1754
LOAD V   : dma_copy                    HBM v -> v_sb (128,d)*M         [SBUF]   line 1974
 -- per 512 K-tile (_qk_and_max_large_tile_impl) --
MM1      : nc_matmul(mm1_psum[q,k], stat=q_sb[d,q], mov=k_sb[d,k])     [PSUM]   line 2655
MASK     : affine_select (causal) | range_select (dynamic) -> mm1_masked [SBUF] line 2691/2738
SCALE+MAX: tensor_scalar_reduce(*scale, reduce=maximum) -> mm1_partial_max [SBUF] line 2714/2753
SECT MAX : tensor_reduce(maximum, negate=True) -> mm1_section_max (= -max) [SBUF] line 2135
RUN MAX  : minimum(running, section) ; exp(prev+running)=alpha          [SBUF]  lines 2156-2168
EXP+SUM  : activation_reduce(exp, data=mm1_masked, bias=-runmax,
                             reduce=add -> exp_partial_sum) -> exp_sb    [SBUF]  line 2225
TRANSPOSE: dma_transpose exp_sb -> exp_tp_sb [128,4,128] (KV->partition) [SBUF]  line 2258
MM2      : nc_matmul(mm2_psum, stat=exp_tp_sb[kv,q], mov=v_sb[kv,d])     [PSUM]  line 2823
ACCUM    : tensor_copy / tensor_tensor(add) mm2_psum -> mm2_sb           [SBUF]  line 2861
RUN SUM  : tensor_reduce(add) ; tensor_scalar(alpha*prev + section)      [SBUF]  line 2393/2402
RECIP    : reciprocal(exp_running_sum) -> exp_sum_reciprocal (last sect) [SBUF]  line 2411
X-SECTION: scalar_tensor_tensor(alpha*O_prev + O_section)               [SBUF]  line 2488
NORMALIZE: activation(copy, scale=recip) -> mm2_final                    [SBUF]  line 2557
WRITE    : dma_copy mm2_final -> HBM o                                   [HBM]   line 2597
```

> **NOTE — MM1 and MM2 swap the stationary/moving roles.** In MM1 the stationary (PE-resident) operand is Q (`stat=q_sb[d,q]`, contraction `d` on partitions) and the moving operand is K. In MM2 the stationary operand is the transposed probabilities `Pᵀ` (`stat=exp_tp_sb[kv,q]`, contraction KV on partitions) and the moving operand is V; the output is `(q, d)`. The `tp_out` path swaps these roles again (line 2817). This is why `exp_sb` must be transposed (`exp_tp_sb`) between the two matmuls — MM1 produces scores with KV on the free axis, MM2 needs KV on the partition axis.

---

## Evidence summary

The signs in the recurrence are the part most worth pinning, and each lands on a specific line:

- **The running-max update is a `minimum`.** Line 2156 is `tensor_tensor(..., op=nl.minimum)` over `mm1_running_max` (`= −max_old`) and `mm1_section_max` (`= −max_new`, from the `negate=True` at line 2135), and `min(−a, −b) = −max(a, b)`.
- **`α = exp(max_old − max_new)` falls out of `exp(prev + running)`.** Line 2149 sets `prev = −1·(−max_old) = +max_old`; line 2162 is `activation(op=exp, prev_mm1_running_max, bias=mm1_running_max)`, so the `+running` term lands in the bias adder and both operands are buffers that already exist.
- **The exp is fused, with no separate subtract.** Line 2225 reads `activation_reduce(op=nl.exp, data=mm1_masked, bias=mm1_running_max[:num_p, g], reduce_op=nl.add, reduce_res=exp_partial_sum)` — one op performs the shift, the exponential and the sum. No subtract instruction appears anywhere in `_exp_impl`.
- **The diagonal predicate reduces to `q_pos ≥ k_pos`.** From `channel_multiplier=1` (line 2695), `pattern=[[-1, num_f]]` (line 2693), `offset = qkmax_grp·128 − k_start_pos` (line 2694) and `cmp_op=greater_equal` (line 2696): `1·p − 1·f + (grp·128 − k_start) ≥ 0`. The source comment at lines 2681–2685 states the same predicate independently.
- **`O` round-trips through HBM.** `dma_copy(dst=mm2_prev_output, src=o[...])` sits at lines 2454 (tp_out) and 2487 (non-tp_out), inside the `section_idx > 0` branch and *before* the `α·O_prev + O_section` accumulate; the comment at line 2443 states the intent.

Corroboration comes from the legacy twin `_pre_prod_kernels/attn_fwd.py`, which carries the same running-max/min plus `correction = exp` recurrence — its exp bias argument is verbatim `bias=running_max[ip_reduce, grp_i]` at line 1579. The `nisa.*` signatures check out against the wheel's type stubs (`neuronxcc-stubs/nki/isa/__init__.pyi`): `range_select` (`:1133`), `affine_select` (`:268`), `activation_reduce` (`:206`, with `bias`/`scale`/`reduce_res` keywords present), `scalar_tensor_tensor` (`:1255`), `tensor_scalar_reduce` (`:1848`).

## Limits of this reading

Every algorithmic claim rests on the readable `attention_cte.py` wheel artifact at named line numbers. Two things sit outside that.

The concrete *bodies* of the `nisa.*` primitives live only in compiled modules — the runtime `neuronxcc/nki/isa/` directory holds stubs and `.so` files, nothing readable — so the micro-op encoding each one lowers to (whether `activation_reduce` is a single TPB instruction or a two-op macro, for instance) is taken from the stub signature and the primitive's name rather than disassembled. This does not affect the algorithm. Separately, the `affine_select` coordinate convention that `channel_multiplier` addresses the partition axis and `pattern` the free axis is the documented semantics of the primitive; the affine arithmetic built on top of it is exact.

> **GOTCHA — a stale `attention_cte` `.so` exists and will mislead a cross-check.** The Cython `_private_kernels/attention_cte.cpython-3XX.so` has `attention_cte` and `affine_select` in its string table but lacks `range_select` and the modern helper names entirely. It is an older build than the Python source analysed here, so treating it as the same kernel produces spurious disagreements about the masking family.

---

## Related Components

| Name | Relationship |
|---|---|
| `attention_tkg` (decode attention) | Sister kernel — single-query decode/generation phase; this is the prefill twin *(planned page)* |
| `_pre_prod_kernels/attn_fwd.py` | Legacy same-algorithm twin (running-max/min, `correction = exp`); corroborates the recurrence |
| `DecomposeAttention` (`hlo-opt` #38) | Recognises matmul→masked-softmax→matmul and lowers `CausalAttentionMMSoftmaxMM*` to this kernel family |
| `ModularAllocator` (nkilib) | SBUF/PSUM tile allocation used by `_allocate_attention_buffers` |

## Cross-References

- [Worked Example B — flash-attention end-to-end](../front/worked-example-flash-attention.md) — the same `attention_cte` traced `@nki.jit` → Penguin IR → BIR → NEFF; this page is its kernel-level companion
- [nkilib Infrastructure: Allocator, Tiling & Common Types](nkilib-infrastructure.md) — the `ModularAllocator`, tiling helpers, and `AttnConfig`/`AttnTileParams` machinery this kernel builds on
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the partition/free axes and the PSUM bank 0–3 (MM1) / 4–7 (MM2) split
- [nki.isa COMPUTE Intrinsics & Validators](isa-compute-intrinsics.md) — `nc_matmul`, `activation`, `tensor_tensor`, `scalar_tensor_tensor`, `tensor_scalar` definitions
- [nki.isa REDUCE / SELECT / DVE / MEMORY / DMA Intrinsics](isa-reduce-dve-dma.md) — `affine_select`, `range_select`, `tensor_reduce`, `activation_reduce`, `tensor_scalar_reduce`, `dma_transpose`, `iota`, `reciprocal`
- [Softmax Legalization](../hlo-opt/softmax-legalize.md) — the HLO softmax decomposition this kernel's exp / sum / reciprocal mirrors
- [MoE Context/Prefill (CTE)](moe-cte-prefill.md) — sibling CTE-phase kernel family using the same sectioned-tile and allocator infrastructure
