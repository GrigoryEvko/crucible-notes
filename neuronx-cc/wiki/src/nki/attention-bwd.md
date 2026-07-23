# Flash-Attention: Backward (training)

> *All symbols and line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The kernel is shipped as readable, Apache-2.0-licensed Python under `nkilib/core/attention/attention_bwd.py` (1617 lines) inside the cp310/311/312 wheels — the three are byte-identical. The golden math reference is `attention_bwd_torch.py` (323 lines) in the same directory. Both are binary-derived wheel artifacts; every claim below is anchored to a line in one of them or to the cross-checked reference twin `neuronxcc/nki/kernels/attention.py`. Other wheels differ — treat line numbers as version-pinned.*

## Abstract

This is the **training-phase backward pass** of flash-attention: given the upstream gradient `dO` and the small statistics the forward pass checkpointed, it produces `dQ`, `dK`, `dV` (and `dsinks` when attention sinks are present) without ever materialising the `seqlen_q × seqlen_k` probability matrix `P`. It is the gradient twin of the forward CONTEXT kernel documented in [Flash-Attention: Context (CTE)](attention-cte.md); the two share the negated-max / LSE convention, and this page recomputes `P` against exactly the statistic that kernel emits.

The algorithm is the **FlashAttention-2 backward recipe**, but two design choices dominate the implementation and a reimplementer must reproduce both byte-exactly. First, the softmax Jacobian rowsum is computed by the **`O∘dO` identity**: the textbook `dS = P∘(dP − rowsum(P∘dP))` needs a per-row sum of `P∘dP`, but because the forward output satisfies `O = P@V`, that sum equals `D_i = Σ_d O_id·dO_id = rowsum(O∘dO)` — a quantity over the *head* axis (cheap, `d_head` wide) instead of the *key* axis (expensive, `seqlen_k` wide). The kernel precomputes `D` **once per query row** (Step 1) and reuses it for every K-tile, never materialising the P-weighted rowsum of `dP`. Second, `P` is **recomputed from the checkpointed LSE in a single fused `exp`**: `P = exp(S − LSE)` where `LSE = max + log(sum)`, so both the numerical max-shift *and* the `1/sum` normalisation collapse into the one additive bias `−LSE`. No subtract-max, no divide-by-sum instruction is ever emitted.

The page is organised by the five algorithmic steps in their emission order — `D = rowsum(O∘dO)`; the `P`-recompute; `dV = Pᵀ@dO`; the fused softmax-backward `dS = P∘(dP − D)`; and the `dQ`/`dK` matmuls — then the **transposed KV-outer tiling** (why backward inverts the forward's Q-outer loop), the masking-backward, the attention-sink gradient, and the recompute-vs-store tradeoff. Each step closes with the exact `nisa` primitive sequence.

For reimplementation, the contract is:

- **The five gradient equations** and their on-chip realisation: `dV = Pᵀ@dO`, `dP = dO@Vᵀ`, `dS = P∘(dP − D)`, `dK = Qᵀ@dS`, `dQ = (dS@K)·scale` — with `softmax_scale` folded into `Q` at load so it appears in both `dK`'s Q-factor and `dQ`'s explicit scale.
- **The `O∘dO` rowsum identity**: why `D_i = Σ_d O_id·dO_id` substitutes for `Σ_j P_ij·dP_ij`, and that it is computed once per Q-tile-group on the first K-section only.
- **The single-`exp` P-recompute**: `P = activation(exp, data=S, bias=−LSE)` and the LSE sign chain `−LSE = −max − log(sum)` consistent with the forward's `(−max, 1/sum)` checkpoint.
- **The transposed KV-outer tiling**: KV-section outer, Q inner; `dK`/`dV` accumulate in SBUF section buffers (GQA-reduced over q-heads), `dQ` is written-then-reloaded per section through HBM.
- **The attention-sink gradient**: `dS_sink = −p_sink·D`, accumulated with `reverse1=True`.

| | |
|---|---|
| **Public entry** | `attention_bwd` — `attention_bwd.py:43` (`@nki.jit`) |
| **Main driver** | `flash_attn_bwd` — `attention_bwd.py:1039` |
| **Per-tile core** | `_flash_attn_bwd_core` — `attention_bwd.py:1408` |
| **P-recompute** | `recompute_qk_softmax` — `attention_bwd.py:849` |
| **Softmax backward** | `compute_softmax_backward_dx` — `attention_bwd.py:973` |
| **Rowsum `D`** | `compute_rowsum_single_tile` — `attention_bwd.py:614` |
| **Golden math** | `attention_bwd_torch.py:26` (`attention_bwd_torch_ref`) |
| **NKI layer** | Python trace → `penguin.ir` → BIR (see [BirCodeGenLoop](bircodegenloop.md)) |
| **Dropout** | **none** in this kernel (`rg -ci dropout` = 0) — see [§ Masking & dropout](#masking-causalswasequence-packingand-the-absent-dropout) |

---

## Data layout and signature

### Purpose

The HBM tensor layout is the single most surprising fact for a reimplementer coming from a GPU flash-attention: **`head_dim` is the partition axis and `seq` is the free axis**, the transpose of the usual `[..., seq, d]` convention. This is what makes the score matmul `Qᵀ@K` a *stationary-transpose* matmul on the PE array with the contraction (`d_head`) already on the partition axis, and it is why the backward needs explicit transposes for `dK`/`dQ` where the contraction shifts to `seqlen`.

### Signature

```c
// attention_bwd.py:43-58
attention_bwd(q_ref, k_ref, v_ref, o_ref, dy_ref, lse_ref,
              sinks_ref=None, bound_min=None, bound_max=None,
              use_causal_mask=False, mixed_precision=False,
              softmax_scale=None, sliding_window=None)
```

| Tensor | HBM shape | Role | Line |
|---|---|---|---|
| `q_ref` | `[bs, nheads, d_head, seqlen_q]` | Query | 75 |
| `k_ref` | `[bs, nheads_kv, d_head, seqlen_k]` | Key (GQA: `nheads_kv ≤ nheads`) | 76 |
| `v_ref` | `[bs, nheads_kv, d_head, seqlen_k]` | Value | 77 |
| `o_ref` | `[bs, nheads, d_head, seqlen_q]` | **Forward output** — needed for `D = rowsum(O∘dO)` | 78 |
| `dy_ref` | `[bs, nheads, d_head, seqlen_q]` | Upstream gradient `dO` | 79 |
| `lse_ref` | `[bs, nheads, pmax=128, seqlen_q//128]` | **LSE checkpoint** from forward | 80 |
| `sinks_ref` | `[bs,nheads]` or `[bs,nheads,num_sinks]` | Optional attention sinks | 82 |
| `bound_min`/`bound_max` | `[seqlen_q]` f32 | Optional sequence-packing bounds | 83-92 |

Outputs are allocated into `nl.shared_hbm` (`attention_bwd.py:155-161`): `out_dq_ref [bs,nheads,d_head,seqlen_q]`, `out_dk_ref`/`out_dv_ref [bs,nheads_kv,d_head,seqlen_k]` (GQA-reduced over the q-heads), and `out_dsinks_ref` only when `sinks_ref` is present.

### Considerations

`validate_inputs` (`attention_bwd.py:188`) enforces the contract: GQA requires `nheads % nheads_kv == 0` (L226-229); the LSE shape must be exactly `(bs, nheads, pmax, seqlen_q // pmax)` (L234-243); **causal masking requires `seqlen_q == seqlen_k`** (L244-247); **sliding window is only valid with causal** (L258); all of `q/k/v/o/dy` must share one dtype (L248-251); and `bound_min`/`bound_max` are all-or-nothing, each `[seqlen_q]` and `float32` (L259-274). `softmax_scale` defaults to `1/sqrt(d_head)` (L152).

> **NOTE —** `o_ref` and `lse_ref` are *unused* in the torch golden's signature (`attention_bwd_torch.py:50,52` mark them "needed for kernel signature match") because the golden recomputes the forward from scratch via `compute_o_lse`. The *kernel* uses them for real: `o_ref` feeds the `O∘dO` rowsum, `lse_ref` feeds the `exp` bias. The golden's job is only to prove the arithmetic, not to mirror the recompute strategy.

---

## The FlashAttention-2 backward equations

### Purpose

The kernel's own inline pseudocode (`attention_bwd.py:112-141`) is the ground truth and maps one-to-one onto the standard FA-2 notation. Reproduced and annotated:

```c
// attention_bwd.py:113-139 (inline pseudocode, verbatim semantics)
D     = rowsum(dO * O)                 // Step 1  — head-axis reduction
scores = matmul(Q, K.T)               // Step 2.1
scores = scores * softmax_scale       // Step 2.2 (folded into Q at load)
scores = apply_mask(scores)           // Step 2.3 (causal/SWA/packing)
P     = softmax(scores)               // Step 2.4 — single exp(S - LSE)
dV    = matmul(P.T, dO)               // Step 3.1
dP    = matmul(dO, V.T)               // Step 3.2 ("softmax_dy")
dS    = P * (dP - D)                  // Step 4   — softmax Jacobian, fused
dQ    = matmul(dS, K) * softmax_scale // Step 5.1
dK    = matmul(Q.T, dS)               // Step 5.2 (Q already scaled)
return dQ, dK, dV
```

### The O∘dO rowsum identity

The FA-2 softmax-backward identity is `dS_ij = P_ij·(dP_ij − Σ_j P_ij·dP_ij)`. The naive implementation forms the bracketed per-row sum over the *key* axis. This kernel never does. Because the forward output is `O = P@V`, the chain rule gives, for each query row `i`:

```text
Σ_j P_ij · dP_ij  =  Σ_j P_ij · (Σ_d dO_id · V_jd)
                  =  Σ_d dO_id · (Σ_j P_ij · V_jd)
                  =  Σ_d dO_id · O_id
                  =  rowsum_d (O ∘ dO)_i   =   D_i
```

So the expensive `seqlen_k`-wide P-weighted rowsum of `dP` equals the cheap `d_head`-wide rowsum of the pointwise product `O∘dO`. The kernel computes `D` **once per Q-tile-group** and reuses it for every K-tile of every K-section.

> **QUIRK — the substitution is exact, not an approximation.** The golden reference does it the *textbook* way: `softmax_dx` (`attention_bwd_torch.py:299-323`) literally forms `prod = dy*y; reduce = prod.sum(dim=-1); return (dy - reduce)*y` — the P-weighted key-axis sum. The kernel substitutes `D = rowsum(O∘dO)`. Both compute the identical `dS`; the kernel's path is one head-axis reduction per Q-row instead of one key-axis reduction per (Q-row × K-tile). A reimplementation that "verifies" against the textbook rowsum will match to floating-point tolerance — and is paying for a wider, repeated reduction it does not need.

### Golden-reference cross-check

The torch golden assembles the same five outputs (`attention_bwd_torch.py:88-107`):

```c
softmax_dy      = matmul(dy.T, V)                  // → dP            (L88)
softmax_dx_gold = softmax_dx(softmax_dy, P)        // → dS            (L94/103)
dv = matmul(dy, P)                                 // → P.T@dO        (L105)
dq = matmul(K, dx.T) * softmax_scale               // → dS@K · scale  (L106)
dk = matmul(q_scaled, dx)                          // → Q.T@dS        (L107)
```

Note that `q_scaled = q * softmax_scale` (`attention_bwd_torch.py:73`) — the scale is pre-applied to `Q`, so it rides into `dK`'s Q-factor *and* appears explicitly on `dQ`. This exactly mirrors the kernel, where `load_q_dy` scales `Q` at DMA time (next section) and `dQ` carries a separate `·softmax_scale`. (Kernel L106/137-139, golden L73/106-107.)

---

## Step 1 — D = rowsum(O ∘ dO)

### Algorithm

```c
function compute_rowsum_single_tile(o_tile, dy_transposed, dy_o_partial, i_d_head_tile, ...):
    // attention_bwd.py:614-655 — one head-dim tile's contribution to D
    for tile_idx in range(num_tiles):
        tmp = nc_transpose(o_tile[:, tile_idx-slice])        // (d,q) -> (q,d) onto PSUM   (L645-646)
        dy_o_mul[tile_idx] = tensor_tensor(dy_transposed[:, d-slice],
                                           tmp, op=nl.multiply)  // elementwise (q,d)       (L647-652)
    for tile_idx in range(num_tiles):
        dy_o_partial[tile_idx][:, i_d_head_tile] =
            tensor_reduce(op=nl.add, data=dy_o_mul[tile_idx], axis=1)  // reduce over d_head (L654-655)
```

`D` is accumulated across head-dim tiles into the per-tile column `dy_o_partial[g][:, i_d_head_tile]`, then a final `tensor_reduce(add, axis=1)` in the driver (`flash_attn_bwd:1264-1270`) sums those per-tile columns into the live statistic `dy_o_sum`, shape `(q_seq_tile_size, q_seq_n_tiles)` per q-head, `float32` when `mixed_precision`.

> **GOTCHA — D is computed only on the FIRST K-section.** The driver guards the whole rowsum block with `if k_seq_start == 0:` (`attention_bwd.py:1233`). `D` is a per-Q-row quantity independent of which keys are in the section, so it is computed once when the first KV section is loaded and the resulting `dy_o_sum` persists in SBUF across all later sections. A reimplementation that recomputes `D` per section wastes a transpose + multiply + reduce per Q-tile per section.

---

## Step 2 — Recompute P from the checkpointed LSE

### Purpose

Flash-attention backward does not store the quadratic `P` matrix; it recomputes it from `{Q, K}` plus the **1-D LSE statistic** the forward checkpointed. `recompute_qk_softmax` (`attention_bwd.py:849`) is that recompute.

### Algorithm

```c
function recompute_qk_softmax(cfg, q_local, k_local, softmax_exp_bias, softmax_y, ...):
    // attention_bwd.py:849-970
    for g in range(q_tile_group_size):
        if not tile_required[g]: continue                          // causal/SWA early-exit
        qk_psum = psum(q_seq_tile_size, k_seq_tile_size)            // (q, k)
        for i_d_head_tile in range(d_head_n_tiles):
            nc_matmul(stationary = q_local[i_d_head_tile][:, g-slice],  // (d, q)
                      moving    = k_local[i_d_head_tile],               // (d, k)
                      dst       = qk_psum)                              // qk_psum = Q.T @ K  (L912-918)
        // ---- mask + PSUM->SBUF copy ----
        if use_sequence_packing:
            range_select(qk_res_buf[g], qk_psum, _FLOAT32_MIN,
                         greater_equal bound_min, less bound_max, ...)  // fused copy+mask (L924-933)
        else:
            tensor_copy(qk_res_buf[g], qk_psum)                         // plain copy     (L935)
    for g in range(q_tile_group_size):
        if not tile_required[g]: continue
        if (not packing) and use_causal_mask:
            affine_select(... k_idx > q_idx -> _FLOAT32_MIN ...)        // causal diagonal (L943-951)
            if sliding_window > 0:
                affine_select(... k_idx < q-w+1 -> _FLOAT32_MIN ...)    // SWA lower bound (L954-962)
        // ---- THE single fused softmax ----
        activation(dst=softmax_y[g], op=nl.exp,
                   data=qk_res_buf[g],
                   bias=softmax_exp_bias[:, q_tile], scale=1.0)         // P = exp(S - LSE) (L964-970)
```

The `nc_matmul` convention used throughout is `dst = stationaryᵀ @ moving`, with the contraction on the partition axis of both operands; here `stationary=Q (d,q)` and `moving=K (d,k)` give `qk_psum = Qᵀ@K` in `(q, k)` layout. The `softmax_scale` is **already folded into `Q`** at load (`load_q_dy → scale_first=softmax_scale`, `attention_bwd.py:751/790/698-699`), so there is no separate scale op in the recompute.

### The single-exp and the LSE sign chain

The whole softmax — max-shift and normalisation — is one `activation(op=nl.exp, bias=−LSE)`:

```text
LSE = m_i + log(l_i)                          (row-max + log row-sum)
exp(S - LSE) = exp(S - m_i - log l_i)
             = exp(S - m_i) / l_i             = the normalised softmax weight
```

The `−LSE` bias is loaded once per `(batch, head)` (`flash_attn_bwd:1149-1160`):

```c
// attention_bwd.py:1153-1160
dma_copy(softmax_exp_bias[i_q_head], lse_ref.ap(...))            // = LSE
tensor_scalar(softmax_exp_bias[i_q_head], ·, nl.multiply, -1.0)  // -> -LSE
```

The AP transposes the `(n_tiles, pmax)` packing of `lse_ref` into the SBUF layout `(q_seq_tile_size, q_seq_n_tiles)`.

> **QUIRK — the max-shift and the 1/sum normalise both live in one bias operand.** There is no subtract-max instruction and no divide-by-sum instruction anywhere in the recompute. Both are folded into `−LSE`. This is the entire reason the forward checkpoints LSE: it is a *linear*-size statistic (`O(seqlen_q)` per head, not `O(seqlen_q²)`) that carries both softmax normalisers in one number per row.

The sign chain is consistent with the forward. The [CTE kernel](attention-cte.md) stores the row-max **negated** (`mm1_running_max = −max`) and a reciprocal sum (`exp_sum_reciprocal = 1/l`), DMA'd out as the `(out_neg_max, out_sum_recip)` pair when `cache_softmax=True` (CTE kernel lines 799-808). The golden reconstructs `lse = −1·(neg_max + log(recip)) = max + log(sum)` (`attention_bwd_torch.py:230`), so `−LSE = −max − log(sum)` and `exp(S − max − log sum) = softmax`. The forward emits the pair; the host combines it into the single LSE this kernel ingests.

---

## Step 3.2 + Step 4 — softmax backward (dP, then dS)

### Algorithm

```c
function compute_softmax_backward_dx(cfg, dy_local, v_local, softmax_y, dy_o_sum, ...):
    // attention_bwd.py:973-1036
    for g in range(q_tile_group_size):
        if not tile_required[g]: continue
        // ---- Step 3.2: dP = dO @ V.T ----
        softmax_dy_psum = psum(q_seq_tile_size, k_seq_tile_size)
        for i_d_head_tile in range(d_head_n_tiles):
            nc_matmul(stationary = dy_local[i_d_head_tile][:, g-slice],  // (d, q)
                      moving    = v_local[i_d_head_tile],                // (d, k)
                      dst       = softmax_dy_psum)                       // (q, k) = dP  (L1019-1025)
        // ---- Step 4: dS = (dP - D) * P, ONE fused op ----
        scalar_tensor_tensor(dst=softmax_dx_local[g],
                             data=softmax_dy_psum,
                             op0=nl.subtract, operand0=dy_o_sum[:, g],    // (dP - D)
                             op1=nl.multiply, operand1=softmax_y[g])      // * P          (L1029-1036)
```

`softmax_dy_psum[q,k] = Σ_d dO[d,q]·V[d,k] = (dO @ Vᵀ)` in `(q,k)` layout = `dP`. The `scalar_tensor_tensor` primitive computes `dst = (data op0 operand0) op1 operand1 = (dP − D) ∘ P` in a single Pool/DVE instruction — the on-chip realisation of `softmax_dx`, with the `Σ(dy·y)` rowsum supplied by the precomputed `D`.

The reference twin uses the *identical* op with the *identical* operand order (`neuronxcc/nki/kernels/attention.py:1027-1033`: `scalar_tensor_tensor(data=softmax_dy, op0=np.subtract, operand0=dy_o_sum[...], op1=np.multiply, operand1=softmax_y)`), strong mutual corroboration that the fused `(dP − D)∘P` ordering is deliberate.

---

## Step 5 + Step 3.1 — the gradient matmuls

### Purpose

`_flash_attn_bwd_core` (`attention_bwd.py:1408`) emits, per `(q-tile-group, k-tile)`, the recompute (Step 2), the softmax-backward (Steps 3.2/4), then the three gradient matmuls in the order `dQ` (Step 5.2), `dV` (Step 3.1), `dK` (Step 5.1). The matmul convention forces transposes wherever the contraction axis is `seqlen_k` rather than `d_head`.

### Algorithm — dQ (Step 5.2)

`dQ = (dS @ K)·scale` contracts over `seqlen_k`, so both `K` and `dS` must be transposed to put `k_seq` on the partition axis:

```c
// attention_bwd.py:1499-1565
for i_d_head_tile:                                                       // transpose K to (k_b, d)
    transpose_tiles(k_local[i_d_head_tile], transposed_k_local[i_d_head_tile],
                    k_seq_tile_size_backward, engine=nisa.scalar_engine)  // L1504-1510
for kb in range(k_seq_fwd_bwd_multiplier):                               // transpose dS to (k_b, q)
    nc_transpose(transposed_softmax_dx_local_psum[:, g-slice],
                 softmax_dx_local[g][:, kb-slice])                        // L1527-1536
    tensor_copy(transposed_softmax_dx_local[kb], ...psum)                 // L1538
for i_d_head_tile:
    dq_psum = psum(d_head_tile_size, q_seq_tile_size * q_group); memset(dq_psum, 0)
    for kb, g:
        if not tile_required[g]: continue
        nc_matmul(stationary = transposed_k_local[i_d_head_tile][:, kb-slice],  // (k_b, d)
                  moving    = transposed_softmax_dx_local[kb][:, g-slice],      // (k_b, q)
                  dst       = dq_psum[:, g-slice])                             // (d, q)  L1548-1556
    // ---- scale + running accumulation, ONE fused op ----
    scalar_tensor_tensor(dst=dq_local[i_d_head_tile], data=dq_psum,
                         op0=nl.multiply, operand0=softmax_scale,
                         op1=nl.add,      operand1=dq_local[i_d_head_tile])     // L1558-1565
```

The final `scalar_tensor_tensor` computes `dq_local += dq_psum·softmax_scale` — the explicit `·scale` of `dQ`, fused with the running accumulation across K-tiles. `dq_local` is `(d_head_tile_size, q_seq_tile_size·q_tile_group_size)`. The K transpose is explicitly pinned to `scalar_engine` (L1509).

### Algorithm — dV (Step 3.1) and dK (Step 5.1)

Both contract over `seqlen_q`; `dV = Pᵀ@dO` reuses the already-transposed `dO`, and `dK = Qᵀ@dS` reuses the already-transposed scaled `Q`:

```c
// dV — attention_bwd.py:1568-1591
for i_d_head_tile:
    dv_psum = psum(d_head_tile_size, k_seq_tile_size)
    for g:
        if not tile_required[g]: continue
        nc_matmul(stationary = trans_dy[i_d_head_tile][:, g-slice],   // transposed dO (q, d)
                  moving    = softmax_y[g],                           // P (q, k)
                  dst       = dv_psum)                                // (d, k) = P.T @ dO
    tensor_tensor(dv_local_reduced[i_d_head_tile][:, k-slice], ..., dv_psum, op=nl.add)  // accumulate

// dK — attention_bwd.py:1594-1617
for i_d_head_tile:
    dk_psum = psum(d_head_tile_size, k_seq_tile_size)
    for g:
        if not tile_required[g]: continue
        nc_matmul(stationary = trans_q_local[i_d_head_tile][:, g-slice],  // transposed scaled-Q (q, d)
                  moving    = softmax_dx_local[g],                        // dS (q, k)
                  dst       = dk_psum)                                    // (d, k) = Q.T @ dS
    tensor_tensor(dk_local_reduced[i_d_head_tile][:, k-slice], ..., dk_psum, op=nl.add)  // accumulate
```

`dV` and `dK` accumulate via `tensor_tensor(add)` into **SBUF section accumulators** `dv_local_reduced` / `dk_local_reduced`, which are zero-initialised per K-section (`flash_attn_bwd:1166-1167`, `value=0.0`) and summed across **all q-heads and all q-tiles** of the section — this is the **GQA reduction**: one KV-head receives gradient from `nheads_per_kv_head` q-heads.

### nisa primitive census (one core invocation)

| Step | Primitive sequence | Engine |
|---|---|---|
| Recompute | `nc_matmul`×`d_tiles` → `range_select`\|`tensor_copy` → [`affine_select`×1-2] → `activation(exp)` | PE, DVE, Act |
| Softmax-bwd | `nc_matmul`×`d_tiles` → `scalar_tensor_tensor(sub,mul)` | PE, Pool/DVE |
| dQ | `transpose_tiles(K)` → `nc_transpose(dS)`+`tensor_copy` → `nc_matmul`×`(k_mult·q_grp)` → `scalar_tensor_tensor(mul,add)` | PE, Act, Pool/DVE |
| dV | `nc_matmul`×`q_grp` → `tensor_tensor(add)` | PE, Pool/DVE |
| dK | `nc_matmul`×`q_grp` → `tensor_tensor(add)` | PE, Pool/DVE |

Exactly **five `nc_matmul` call sites** exist in the file (QK, dP, dQ, dV, dK) and **two `op=nl.exp` activations** (P-recompute L966, sink-prob L1380).

---

## The transposed KV-outer tiling

### Purpose

The forward [CTE kernel](attention-cte.md) streams K/V tiles inside a **Q-outer** loop, accumulating `O` online. The backward **inverts** this: K/V become the **outer (section) axis** and Q the inner axis. The reason is the gradient dependency structure — `dK`/`dV` must accumulate over *all* Q (every query attends to a given key), while `dQ` accumulates over *all* K. The kernel splits the difference: `dK`/`dV` live in SBUF section accumulators (one whole K-section, reduced across all q in the section), and `dQ` is **written-then-reloaded** per K-section through HBM so it accumulates across sections.

### The loop nest

```c
// flash_attn_bwd — attention_bwd.py:1143-1405
for sample_idx in [start_idx, end_idx):                      // shard over bs*nheads_kv (L1134-1143)
    batch_id = sample_idx // nheads_kv;  head_id = sample_idx % nheads_kv
    load -LSE bias once per (batch,head)                      // L1149-1160
    for k_seq_start in range(0, seqlen_k, k_seq_section_len): // KV SECTIONS (<= 8K)        (L1162)
        zero dk/dv section accumulators; load_kv(section)     // L1166-1178
        for i_q_head in range(nheads_per_kv_head):            // GQA q-heads               (L1180)
            for i_q_seq_tile in range(0, q_seq_n_tiles, q_tile_group_size):  // Q-tile groups (L1184)
                dq_local = zero  if k_seq_start==0  else  reload from out_dq_ref  // L1188-1207
                load_q_dy (scaled Q, dO); transpose Q, dO                          // L1210-1230
                if k_seq_start == 0: compute D (rowsum O.dO)                       // L1233-1270
                for i_k_seq_tile in range(cur_k_seq_n_tiles): // K-tiles in section          (L1274)
                    tile_required = get_required_tiles_mask(...)                   // L1275
                    if any: _flash_attn_bwd_core(...)         // dQ,dV,dK for this tile
                write dq (accumulated over this section's K-tiles)                 // L1326-1335
        write dk, dv (section accumulators, GQA-reduced)                          // L1340-1353
        if num_sinks > 0: compute dsinks                                          // L1355-1405
```

> **QUIRK — dQ round-trips through HBM, dK/dV do not.** `dq_local` is zero-initialised only on the first section (`value=0.0 if k_seq_start == 0 else None`, `attention_bwd.py:1192`); on later sections it is **DMA-reloaded** from `out_dq_ref` (L1194-1207), the partial `dQ` accumulated into it, and written back (L1326-1335). `dK`/`dV` stay resident in SBUF for the life of one K-section. The asymmetry is the SBUF budget: a K-section accumulator is `d_head × k_seq_section_len` (bounded), but a full `dQ` would need `d_head × seqlen_q` resident across *all* sections — so `dQ` is the one paid through HBM. This mirrors the forward, where it is `O` that round-trips.

### Tile sizes

| Knob | Default | Source |
|---|---|---|
| `q_seq_tile_size` | `pmax` (128) | `setup_config:363` |
| `k_seq_tile_size` | `psum_fmax` (512), clamped to `seqlen_k` | `setup_config:364,405` |
| `k_seq_tile_size_backward` | `pmax` (128) | `setup_config:365` |
| `d_head_tile_size` | `pmax` (128), re-divided to equal tiles | `setup_config:366,403-404` |
| `q_tile_group_size` | `4` | `setup_config:367` |
| `k_seq_section_len` | `8192 // power_of_2(d_head_tiles)` | `setup_config:368,408` |
| `k_seq_fwd_bwd_multiplier` | `k_seq_tile_size // k_seq_tile_size_backward` | `setup_config:434` |

`k_seq_fwd_bwd_multiplier` is the number of 128-wide transposed sub-tiles inside one 512-wide K-tile — the inner loop count in the `dQ` transpose+matmul. `power_of_2(n)` rounds *up* to the smallest power of two `≥ n` (`attention_bwd.py:494-497`); dividing the 8K section budget by it keeps the K/V/dK/dV SBUF footprint bounded as `d_head` grows.

The driver is **SPMD-sharded** over `bs·nheads_kv` (`flash_attn_bwd:1134-1143`): `num_shards = nl.num_programs(0)`, `shard_id = nl.program_id(0)`, `shard_size = div_ceil(bs·nheads_kv, num_shards)`. See [SPMD programming model](spmd-programming-model.md).

---

## Masking (causal/SWA/sequence-packing) — and the absent dropout

### Two levels of mask

**(a) Tile-level early exit** — `get_required_tiles_mask` (`attention_bwd.py:794-846`) is pure compile-time Python. For causal, a `(q-tile, k-tile)` pair is skipped when `q_tile_max_pos < k_tile_min_pos`; SWA additionally skips when `k_tile_max_pos < q_tile_min_pos − sliding_window + 1`. Fully-skipped tiles never enter `_flash_attn_bwd_core`.

**(b) Element-level mask** — applied inside `recompute_qk_softmax` on the *recomputed scores* (so the backward mask is identical to the forward). Non-packed causal uses `affine_select` to set `S → _FLOAT32_MIN (−3.4e38)` where `k_idx > q_idx` (pattern `[[-1, k_seq]]`, `channel_multiplier=1`, `greater_equal`); a second `affine_select` enforces the SWA lower bound (`attention_bwd.py:943-962`). After `exp`, masked entries → 0 and contribute nothing to any gradient.

**(c) Sequence packing** — when `bound_min`/`bound_max` are present, the per-row bounds are DMA'd to SBUF once and clamped on-device: causal clamps `bound_max[q] = min(bound_max[q], q+1)` via `iota` + `tensor_tensor(minimum)`; SWA clamps `bound_min[q] = max(bound_min[q], q−w+1)` via `iota` + `tensor_tensor(maximum)` (`flash_attn_bwd:1120-1131`). Masking then folds into the PSUM→SBUF copy as **one `range_select`** (`greater_equal bound_min AND less bound_max → keep, else _FLOAT32_MIN`, `attention_bwd.py:924-933`), replacing the two `affine_select`s. See [mask-predicate algebra](mask-predicate-algebra.md) and [index-mask inference](index-mask-inference.md).

### No dropout in this kernel

An exhaustive search of `attention_bwd.py` for `dropout|rng|seed|philox|bernoulli|random_seed` returns **zero hits** (`rg -ci` = 0). This kernel re-applies no dropout mask, re-draws no Bernoulli mask, and applies no `1/(1−p)` rescale to `dP`. The golden reference likewise never exercises dropout: its `compute_o_lse` accepts a `dropout_mask` parameter (`attention_bwd_torch.py:136,214-215`) but `attention_bwd_torch_ref` calls it **without** that argument (L75-86).

**The reference twin's backward, by contrast, does handle dropout** — so the absence above is specific to `nkilib`, not a property of the algorithm. In `neuronxcc/nki/kernels/attention.py`, the dropout re-draw sits *inside* `_flash_attn_bwd_core` (the twin's backward core, L872-1080), guarded by `if dropout_p > 0.0`: `nl.random_seed(offset_seed)`, then `softmax_y = nl.dropout(softmax_y, rate=...)`, then `nl.multiply(softmax_y, 1/(1−dropout_p))`, at **lines 974-981**. That is the standard FA-2 dropout-backward recipe: re-draw the same-seeded mask against the recomputed `softmax_y` and re-apply the `1/(1−p)` rescale.

A reimplementer wiring dropout should follow that L974-981 pattern — re-seed per `(batch, head, q-tile, k-tile)` offset, redraw, then `·1/(1−p)` — which matches the `InstDropout` / MT19937-64 mechanism documented for the dropout op generally.

---

## Attention-sink gradient

### Purpose

When `sinks_ref` is present, each attention head has `num_sinks` extra logit columns that participate in the softmax denominator but produce no value output. Their gradient `dsinks` is computed after the dK/dV write of each section (`flash_attn_bwd:1355-1405`).

### Algorithm

```c
// attention_bwd.py:1355-1405
dsinks_local = zeros(q_seq_tile_size, nheads_per_kv_head, num_sinks)        // L1357-1358
load sink_sigma (the sink logits)                                          // L1361-1372
for i_q_head, i_q_seq_tile:
    p_sink = activation(op=nl.exp, data=sink_sigma[:, i_q_head],
                        bias=softmax_exp_bias[i_q_head][:, i_q_seq_tile], scale=1.0)  // L1378-1384
    scalar_tensor_tensor(dst=dsinks_local[:, i_q_head], data=p_sink,
                         op0=nl.multiply, operand0=dy_o_sum[i_q_head][:, i_q_seq_tile],
                         op1=nl.subtract, operand1=dsinks_local[:, i_q_head],
                         reverse1=True)                                     // L1386-1394
dsinks_reduced = tensor_partition_reduce(nl.add, dsinks_local)             // sum over q   L1397-1398
dma_copy(out_dsinks_ref, dsinks_reduced)                                   // L1399-1405
```

`p_sink = exp(sink_logit − LSE)` uses the **same `−LSE` bias** as `P` (sinks are part of the same softmax). With `reverse1=True`, the op computes `dst = operand1 − (data·operand0) = dsinks_local − p_sink·D`, accumulating `dsinks_local −= p_sink·D` over q-tiles.

The math is the sink case of the softmax-backward: a sink contributes no value output, so `dP_sink = 0`, and `dS_sink = p_sink·(dP_sink − D) = −p_sink·D`, summed over the query axis. This matches the golden, which pads `softmax_dy` with zeros for the sink columns and takes the sink slice of `softmax_dx_golden` summed over `dim=2` (`attention_bwd_torch.py:91-101`).

> **NOTE — `reverse1=True` is the sink op's only distinguishing flag.** Three `scalar_tensor_tensor` call sites exist in the file (dS, dQ scale-accumulate, dsinks); only the dsinks one (`attention_bwd.py:1393`) sets `reverse1=True`, swapping the second operand order so the accumulator can be *subtracted into* without a separate negate. The other two use the default `(data op0 operand0) op1 operand1` order.

---

## Saved-for-backward and the recompute-vs-store tradeoff

The forward checkpoints `{O, LSE}` and the host supplies the upstream `dO`. None of these is the quadratic `P` matrix:

| Saved tensor | Size | Why it is saved (not recomputed) |
|---|---|---|
| `O` (`o_ref`) | `O(seqlen_q · d_head)` | feeds `D = rowsum(O∘dO)` cheaply — `P@V` is **never** recomputed for `D`, thanks to the `O∘dO` identity |
| `LSE` (`lse_ref`) | `O(seqlen_q)` per head | the one-number-per-row softmax normaliser; recomputes `P` via one `exp` |
| `dO` (`dy_ref`) | `O(seqlen_q · d_head)` | the upstream gradient (always supplied) |

The tradeoff: instead of saving the `O(seqlen_q · seqlen_k)` probability matrix, the forward saves only the linear LSE, and the backward re-derives `P` on-chip with **one extra QK matmul + one `exp`** per K-tile, plus the `dO@Vᵀ` matmul for `dP`. It trades a constant factor of extra matmul FLOPs for eliminating the quadratic activation memory.

---

## Related Components

| Name | Relationship |
|---|---|
| [Flash-Attention: Context (CTE)](attention-cte.md) | the forward twin; emits the `(−max, 1/sum)` checkpoint that combines to the LSE this kernel ingests; the negated-max convention originates there |
| [Flash-Attention: Decode (TKG)](attention-tkg.md) | the decode-phase forward; no backward (inference only) |
| [BirCodeGenLoop](bircodegenloop.md) | lowers the `penguin.ir` this kernel's trace produces into BIR |
| [SPMD programming model](spmd-programming-model.md) | the `program_id`/`num_programs` sharding over `bs·nheads_kv` |
| [Boundary Markers & Layer-Cut](../hlo-opt/boundary-markers-layer-cut.md) | where the train-time forward/backward checkpoint boundary is cut |

## Cross-References

- [Flash-Attention: Context (CTE)](attention-cte.md) — the forward online-softmax this kernel recomputes against; negated-max / LSE provenance
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the PSUM tile budget that bounds `k_seq_section_len` and forces the `dQ` HBM round-trip
- [Worked Example B — flash-attention end-to-end](../front/worked-example-flash-attention.md) — the forward trace→BIR walkthrough whose backward this page completes
- [ISA compute intrinsics](isa-compute-intrinsics.md) — `nc_matmul`, `nc_transpose`, `activation`, `scalar_tensor_tensor` semantics
- [ISA reduce / DVE / DMA](isa-reduce-dve-dma.md) — `tensor_reduce`, `tensor_tensor`, `range_select`, `tensor_partition_reduce`
- [Mask-predicate algebra](mask-predicate-algebra.md) — the `affine_select` / `range_select` masking primitives reused for the backward causal/SWA/packing masks
