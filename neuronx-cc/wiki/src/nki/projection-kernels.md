# Projection Kernels: Fused QKV & Output-Projection

> *All symbols, line numbers, and constants on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/cp311/cp312 — the kernel bodies are byte-identical across the three wheels, md5-verified: `qkv_cte.py`, `output_projection_tkg.py`, `output_projection_cte_quantization.py`). The sources are the readable `@nki.jit` NKI-Python kernels shipped under `nkilib/core/qkv/` and `nkilib/core/output_projection/` inside the wheel — binary-derived artifacts, not stripped. Every `nisa.*` primitive named here is a Cython intrinsic; cross-check against [nki.isa COMPUTE Intrinsics](isa-compute-intrinsics.md).*

## Abstract

The two transformer **projection** kernels bracket the attention block. `qkv` computes the fused input projection `qkv = norm(x) @ W_qkv + bias` and slices the result into Q, K, V; `output_projection` computes the mirror contraction `out = attn @ W_o + bias` that folds the per-head attention output back into the hidden dimension. Both are matmul kernels with a residual-add / norm front-end (QKV) or a head-feature contraction (o_proj), both fuse bias, both shard across LNC cores, and both come in a **CTE** (prefill / context-encoding, large `B·S`) and a **TKG** (decode / token-generation, `B·S ≤ 128`) variant. The QKV CTE variant additionally fuses RoPE.

The single most important fact about `qkv` is that the projection is **one fused matmul over the concatenated output dimension** `I = (n_q + 2·n_kv)·d_head`, never three separate Q/K/V matmuls. The hidden axis `H` is the contraction (it lives on the PE-array partition); `I` is the free axis. Q, K, V are recovered purely by **free-dim slicing** of that one `[S, I]` output: `Q = out[:, 0:q_dim]`, `K = out[:, q_dim:q_dim+kv_dim]`, `V = out[:, q_dim+kv_dim:q_dim+2·kv_dim]`. The single most important fact about `output_projection` is that its multi-core (LNC) parallelism shards the **output** dimension `H`, so each core writes a **disjoint HBM column range** — a gather realised by addressing, with **no in-kernel collective** (no `all_reduce`, `reduce_scatter`, or `sendrecv`). Any tensor-parallel all-reduce that the textbook Megatron placement would put at o_proj is a *graph-level* collective inserted around this leaf kernel by the HLO compiler, not emitted by the kernel itself.

The page is organised around the two kernels: the **fused QKV matmul and output slicing**, the layout enums, the fused-RoPE half-sin scheme, and the CTE-vs-TKG split for `qkv`; then the **o_proj mirror contraction**, the head-packing trick, the dtype dispatch, and the disjoint-HBM H-shard for `output_projection`. Each closes with the verified `nisa` primitive sequence.

For reimplementation, the contract is:

- **One fused matmul.** `nc_matmul(stat=input_T[h_sub, S], mov=W_qkv[h_sub, I_tile], dst=psum[S, I_tile])`, `H` on the partition (contraction), `I` tiled into 512-column PSUM banks. Q/K/V are slices, not separate products.
- **The slice geometry.** `q_dim = n_q·d_head`, `kv_dim = n_kv·d_head`, `I = q_dim + 2·kv_dim`; static-quant scale columns `w_scale[:,0/1/2] = Q/K/V`.
- **Fused RoPE (CTE only).** `RoPE([X1,X2]) = [X1,X2]·cos + [−X2,X1]·sin`, half of `sin` reused (`sin_cache_1 == sin_cache_2`), applied per-head to **Q and K only**; V copied as-is.
- **The o_proj mirror.** `nc_matmul(stat=attn[D, S], mov=W_o[D, H], dst=psum[S, H])`, head-feature `D` on the partition (contraction), accumulated over `N` heads into one PSUM bank; `H` is the output free axis.
- **The no-collective H-shard.** `h_sharded = H // LNC`; core `prg_id` writes `out[:, prg_id·h_sharded : (prg_id+1)·h_sharded]`. No `sendrecv`/`all_reduce` inside either o_proj body.

| | |
|---|---|
| **QKV sources** | `nkilib/core/qkv/{qkv.py (326), qkv_cte.py (3322), qkv_cte_utils.py (870), qkv_tkg.py (1551), qkv_tkg_mx_impl.py (714)}` |
| **o_proj sources** | `nkilib/core/output_projection/{output_projection_cte/* (6 files), output_projection_tkg.py (1075), output_projection_utils.py}` |
| **QKV dispatcher** | `qkv.py:44 qkv(...)` — `SEQLEN_THRESHOLD_FOR_QKV_CTE = 96` (`qkv.py:235`) |
| **o_proj entries** | two separate `@nki.jit`: `output_projection_cte` (`cte.py:40`), `output_projection_tkg` (`tkg.py:66`) — no internal size dispatcher |
| **QKV fused dim** | `I = (num_q_heads + 2·num_kv_heads)·d_head` (`cte_utils.py:454,496`) |
| **Enums** | `nkilib/core/utils/common_types.py` — `QKVOutputLayout`, `QKVWeightLayout`, `NormType`, `QuantizationType` |
| **Live callers** | `nkilib/experimental/transformer/attention_block_tkg.py` imports both; o_proj called at `:555` |
| **Matmul primitives** | `nisa.nc_matmul` (float/FP8), `nisa.nc_matmul_mx` (5-operand, E8M0) |

---

## The Fused QKV Matmul

### Purpose

`qkv` produces the three attention projections in one shot. Reusing one weight matrix and one PE-array pass for Q, K, and V is the entire point: the contraction dimension `H` is loaded once onto the 128-partition array and streamed against the full fused weight `W_qkv[H, I]`, so the cost is one matmul over `I = (n_q + 2·n_kv)·d_head` rather than three matmuls over `q_dim`, `kv_dim`, `kv_dim`. The kernel front-end optionally folds a residual add and a normalization (RMS / LayerNorm) before the matmul, and the CTE back-end fuses bias, RoPE, and an FP8 KV-cache store after it.

### Entry Point

```text
qkv.py:44  qkv(...)                              ── public dispatcher
  ├─ qkv.py:235  SEQLEN_THRESHOLD_FOR_QKV_CTE = 96
  ├─ qkv.py:240  fused_rope ⇒ force CTE
  ├─ qkv.py:245  HBM input & (S>96 or B*S>pmax) ⇒ force CTE
  ├─ qkv.py:261  qkv_cte(...)                     ── prefill / RoPE / FP8-KV
  └─ qkv.py:302  qkv_tkg(...)                     ── decode (B*S ≤ 128)
```

The dispatch is purely capability/size driven. `fused_rope=True` *always* routes to CTE — TKG's signature has no `cos_cache`/`sin_cache`/`kv_cache` params at all, so it can never fuse RoPE (`qkv_tkg.py:71-93`). HBM input additionally forces CTE when `S > 96` or `B·S > pmax` (128), because TKG requires `B·S ≤ pmax` to keep its output SBUF-resident (`qkv.py:245-248`).

### Algorithm — the fused matmul (CTE)

This is the inner matmul of `_qkv_cte_impl`, at `qkv_cte.py:1401-1411`. The input has already been transposed so that `H` is on the partition axis (the matmul contraction); `W_qkv` is prefetched into SBUF with `H` on the partition and `I` on the free axis. The `I` axis is tiled into 512-column PSUM banks (`psum_fmax`), and `H` is tiled into 128-row subtiles (`pmax`) that **accumulate** into the same bank.

```c
function _qkv_cte_matmul(input_sb, weights_sb, psum):       // qkv_cte.py:1401-1411
  for k_tile_I in range(num_512_tiles_per_I):               // 512-col I tiles → one PSUM bank each
    for i_weight_load, j_128_subtile in H-subtiles:          // 128-row H subtiles, accumulate
      h_subtile_offset = weight_load_block * i_weight_load
                       + pmax * j_128_subtile               // line 1390
      i_offset = psum_fmax * k_tile_I                        // line 1394
      // ONE matmul over the FULL fused I tile — not 3 separate Q/K/V products.
      nc_matmul(
        stationary = input_sb[h_subtile, ds(h_off, S)],      // H on partition (contraction)
        moving     = weights_sb[h_subtile, ds(i_off, I_tile)],// H on partition, I free
        dst        = psum[ S, 0:I_tile ])                     // → [S, ≤512] fused output, line 1410
      // ^ accumulates across H subtiles into psum_accumulation_bank_id
```

> **QUIRK —** the *contraction* side is `H`, but at the NKI call site `input` is the **stationary** operand and `weights` is **moving**, the reverse of the textbook "weights stay, activations stream" intuition. The PE-array contract (`stationary`/`moving`, both loaded with the contraction on the 128-partition) is orthogonal to which logical tensor is "the weights". The static-FP8 path (`qkv_cte.py:1336-1383`) uses the same single-matmul structure with `perf_mode="double_row"` (256-H subtiles, two 128-tiles fused) for 2× throughput.

The TKG matmul (`qkv_tkg.py:1507-1513`) keeps the hidden as `[H0=128, B·S, H1]` (`H0` already on the partition, no transpose needed) and packs multiple small-`B·S` matmuls into the 128-wide array via `tile_position`. The array-tiling factor is a single integer, `array_tiling_dim`, taking the values `32` / `64` / `128` (`qkv_tkg.py:611-621`): `B·S ≤ 32 → 32` (4×), `≤ 64 → 64` (2×), else `128`, with `gen2` forced to `64`. The source comments name the two packed modes `"4x 128P*32F"` and `"2x 128P*64F"`.

### Output slicing — Q | K | V

The fused `[S, I]` output is sliced along its free axis into three contiguous blocks. With `q_dim = num_q_heads·d_head` and `kv_dim = num_kv_heads·d_head` (`cte_utils.py:493-494`), and the kernel asserting `I == q_dim + 2·kv_dim` (`cte_utils.py:496`):

```c
Q = out[:, 0                  : q_dim            ]   // heads 0 .. n_q-1
K = out[:, q_dim              : q_dim + kv_dim   ]   // heads n_q .. n_q+n_kv-1
V = out[:, q_dim + kv_dim     : q_dim + 2·kv_dim ]   // heads n_q+n_kv .. n_q+2n_kv-1
```

The non-RoPE static-quant eviction path slices exactly here (`qkv_cte.py:1458-1505`), and the static-quant **scale columns map 1:1**: `w_scale_tile[:,0]` is applied to Q (`:1461`/`:1514`), `[:,1]` to K (`:1474`/`:1525`), `[:,2]` to V (`:1497`/`:1544`). When RoPE is fused the same `Q→0, K→1, V→2` column mapping reappears in the per-head loop (`:3206-3209` for Q/K, `:3291` for V). The `QKVOutputLayout.BSD` HBM tensor *is* literally this concatenated `[B, S, I]` block.

### Layout enums

Four enums in `nkilib/core/utils/common_types.py` parameterise `qkv` (and o_proj). All values are read verbatim from the file `qkv_cte.py:32` imports.

| Enum | Values | Meaning |
|---|---|---|
| `QKVOutputLayout` | `BSD=0`, `NBSd=1`, `NBdS=2` | output arrangement: `BSD` = concat `[B,S,I]`; `NBSd` = `[num_heads,B,S,d]` per-head scatter; `NBdS` = reserved, no kernel path |
| `QKVWeightLayout` | `CONTIGUOUS=0`, `MX_CONTIGUOUS=1`, `MX_INTERLEAVED=2` | offline weight-packing contract (validation marker, not a matmul branch) |
| `NormType` | `NO_NORM=0`, `RMS_NORM=1`, `LAYER_NORM=2`, `RMS_NORM_SKIP_GAMMA=3` | front-end normalization (`SKIP_GAMMA` = γ pre-folded into `W` offline, CTE-only) |
| `QuantizationType` | `NONE=0`, `STATIC=1`, `ROW=2`, `MX=3`, `STATIC_MX=4`, `ROW_MX=5` | dtype path; `STATIC_MX`/`ROW_MX` are defined but unused by `qkv` (`ROW_MX` unused everywhere) |

> **GOTCHA —** there are **two** `common_types.py` files in the wheel and they disagree. `neuronxcc/private_nkl/utils/common_types.py` and `neuronxcc/nki/_pre_prod_kernels/common_types.py` truncate `QuantizationType` at `ROW=2` and define **no** `QKVWeightLayout` class at all. The production `qkv`/o_proj kernels import the *full* version, `nkilib/core/utils/common_types.py` (`QuantizationType` to `ROW_MX=5`, plus `QKVWeightLayout`). A reimplementer (or a grep) that lands on the `private_nkl` / `_pre_prod` copy will wrongly conclude that MX quant and the weight-layout enum do not exist. Follow the *import line* (`qkv_cte.py:32 from ..utils.common_types import …`), not a filename match.

`QKVWeightLayout` selects the offline packing, not a kernel code path — the kernel *asserts* the layout matching the path it takes and does not branch the matmul on it (`cte_utils.py:564-583`). `CONTIGUOUS` is the non-MX `[H,I]` checkpoint as-is; `MX_CONTIGUOUS` packs every 4 consecutive `H` rows into `float8_e4m3fn_x4` (`w.reshape(H/4,4,I).transpose(0,2,1).reshape(H/4,I*4)` → x4); `MX_INTERLEAVED` additionally reorders rows (`h_idx = arange(H).reshape(2,H/4,2).transpose(1,0,2).reshape(H)`) to match the quads the DMA-transpose produces on the input. The docstring at `common_types.py:66-87` gives the exact numpy reshapes.

`NBSd` scatters the single fused output head-by-head into the leading head dim via an `.ap()` pattern, `for i_head in range(num_heads)` at `cte_utils`-driven `qkv_cte.py:1652-1667` (MX: `:2395-2410`), and requires `d_head == 128` (`cte_utils.py:368-373`). `NBdS` is a reserved enum slot: `qkv_tkg.py:504-507` asserts it unsupported, and CTE has no path for it.

---

## Fused RoPE (CTE)

### Purpose

When `fused_rope=True`, the rotary embedding is applied to Q and K **inside** the QKV kernel, interleaved with the PSUM→SBUF eviction of the fused matmul output, so the rotated Q/K never round-trip to HBM unrotated. This is CTE-exclusive; TKG defers RoPE to a downstream decode op. RoPE is documented in the sibling page [§6.7.11 RoPE Kernels](rope-kernels.md) — this section documents only the fused-in-CTE realisation.

### Algorithm — the half-sin rotation

`_copy_psum_to_sbuf_apply_rope_and_bias` (`qkv_cte.py:3141`). The formula, verbatim from `qkv_cte.py:3196`, is `RoPE([X1, X2]) = [X1, X2]·cos + [−X2·sin, X1·sin]`. Because `sin_cache_1 == sin_cache_2`, only `d_head/2` of the sin cache is loaded (`qkv_cte.py:817-821`, half-sin reuse). `cos`/`sin` are DMA'd per S-tile from `cos_cache_hbm`/`sin_cache_hbm[B,S,d]`.

```c
function _copy_psum_to_sbuf_apply_rope_and_bias(psum, out, ...):  // qkv_cte.py:3141
  // --- Q and K heads: rotate ---
  for i_head in sequential_range(num_q_heads + num_kv_heads):     // line 3197
    head_offset = i_head * d_head
    w_scale_col = (i_head < num_q_heads) ? 0 : 1                  // Q→col0, K→col1, lines 3206-3209
    X = copy head from psum (optionally * w_scale + bias)
    neg_X2_sin = -(X2) * sin                                      // lines 3245-3257 (half-sin)
    X1_sin     =  (X1) * sin                                      // lines 3260-3265
    X_cos      =   X   * cos                                      // lines 3268-3273
    out[head]  =  X_cos + [neg_X2_sin, X1_sin]                    // lines 3276-3281
  // --- V heads: copied AS-IS, no rotation ---
  for i_head in range(num_q_heads + num_kv_heads,
                      num_q_heads + 2*num_kv_heads):              // line 3284
    w_scale_col = 2                                               // V→col2, line 3291
    out[head]  =  copy head from psum (optionally * w_scale + bias) // tensor_copy, lines 3319-3322
```

> **NOTE —** RoPE rotates **Q and K only**; the V heads `[n_q+n_kv .. n_q+2n_kv)` are a plain `tensor_copy` (with optional dequant/bias) and are never rotated. The literal source comment writes the sin term as `[−X2·sin, X1·sin]` (the half-sin already distributed) rather than the factored `[−X2, X1]·sin`; the arithmetic is identical.

Bias is fused in both variants. CTE loads `bias[1,I]`, broadcasts it to 128 rows via `stream_shuffle` (`_load_and_broadcast_bias`, `qkv_cte.py:2594`), and adds it during eviction — a `tensor_tensor` add on the plain path, or a single `scalar_tensor_tensor = (psum·w_scale) + bias` on the static-quant path. TKG pre-loads bias as the matmul *initializer* when `quant==NONE`, or adds it post-dequant for STATIC/ROW, and only `shard_id==0` adds it to avoid a double-add under LNC2.

---

## QKV CTE vs TKG — norm fusion

### Purpose

The two `qkv` variants differ in input residency, norm implementation, and cross-core reduction. CTE handles prefill blocks (`B·S ≳ 128`); TKG handles decode (`B·S ≤ 128`).

### The split

| Facet | CTE (`qkv_cte.py`) | TKG (`qkv_tkg.py`) |
|---|---|---|
| Regime | prefill, `B·S ≳ 128` | decode, `B·S ≤ 128` |
| Input | transposed so `H` on partition (DMA-transpose gen3+ no-norm, else PE-transpose vs identity, `:997`) | kept `[H0=128, B·S, H1]`, `H0` on partition, no transpose |
| Norm | **hardware BNStats** for LayerNorm (`bn_stats` DST=6 Welford triples + `bn_aggr`, `:3083-3138`); RMS via `activation_reduce(square,+)` + `rsqrt` (`:3028`) | **software reduce** (`_rmsnorm_tkg` `:828` / `_layernorm_tkg` `:839`), not BNStats |
| `SKIP_GAMMA` | supported (γ pre-folded in `W`) | rejected (TKG validate `:563` allows only `{NO_NORM,RMS_NORM,LAYER_NORM}`) |
| Cross-core | — (single output, `core_barrier`) | shards `W` on `H` over LNC, then `nisa.sendrecv + add` cross-core **sum** (`:1108-1121`) |
| RoPE / FP8-KV | fused (`:3141` / `:456`) | none |
| `fused_add` | residual add before norm | HBM→HBM residual before norm (`:660`) |

> **QUIRK —** the QKV TKG cross-core op is a **true reduce** (`sendrecv + add`), because TKG shards the *contraction* `H` across LNC cores: each core holds a partial sum over its `H`-slice and the partials must be summed. This is the opposite of o_proj, which shards the *output* `H` and needs no reduce at all (see [§o_proj H-shard](#the-no-collective-h-shard)). The two kernels sit at opposite ends of the LNC shard-axis spectrum.

### MX (E8M0 per-block) — both variants

Both `qkv` variants have an MX path using the 5-operand `nisa.nc_matmul_mx(dst, stationary, moving, stationary_scale, moving_scale)` where E8M0 `uint8` per-32-element-block scales ride **inside** the matmul (no post-dequant multiply). CTE MX is `_qkv_cte_mx_impl` (`qkv_cte.py:1678`); decode MX is `qkv_tkg_mx_impl.py`. Weights are x4-packed `float8_e4m3fn_x4` (`[H/4, I]`); activations are online-quantized via `nisa.quantize_mx`. The neutral scale is `MX_NEUTRAL_SCALE = 127` (`= 2^(127−127) = 1.0`, `qkv_cte.py:50`), used when a per-block scale is absent (FP8 stationary or `w_scale None`). The E8M0 scale HBM layout is `[H/32, I]`, loaded with the **quad-hole** spread (16 scale rows per H512 tile → 4 SBUF quadrants × 4 rows at partition offsets 0/32/64/96).

---

## The o_proj Mirror Contraction

### Purpose

`output_projection` computes `out = attn @ W_o + bias`, folding the per-head attention output back into the hidden dimension. It is the **mirror** of `qkv`: where `qkv` contracts `H` and outputs `I`, o_proj contracts the head-feature axis `N·D` and outputs `H`. The per-head attention output is the input, laid out with the head-dim `D` on the partition axis. There is **no in-kernel collective and no residual add** — o_proj emits only the raw projected output plus bias; the post-attention residual is the consumer's pre-norm fused-add.

### Entry Point

```text
output_projection_cte.py:40  output_projection_cte(...)   ── prefill, @nki.jit, attn [B,N,D,S]
output_projection_tkg.py:66  output_projection_tkg(...)   ── decode,  @nki.jit, attn [D,B,N,S]
```

> **NOTE —** unlike `qkv.py`/`mlp.py`, o_proj has **no internal size dispatcher**. CTE and TKG are two independent `@nki.jit` entries; the *caller* chooses (`attention_block_tkg.py:555` calls `output_projection_tkg` for decode). The CTE kernel is the prefill counterpart.

### Algorithm — the head-feature contraction (CTE float)

`perform_float_projection` (`output_projection_cte_float.py`). The contraction is the head-dim `D` (on the partition); the matmul **accumulates across the `N` heads** into one PSUM bank; `H` is the output free axis.

```c
function perform_float_projection(attention_sb, w_sbuf, bias, out_hbm, cfg):  // cte_float.py
  for h_block in H-blocks (≤10MB weights, SBUF-budget tiled):           // line 74
    h_start = cfg.h_sharded_size * prg_id + h_block_idx * tile_size      // LNC H-shard offset, :75
    for batch, for s_block(512):
      for s_subtile(128), for h_subtile(512):
        res_psum[S, H] = 0                                              // PSUM, fp32
        for head_idx in range(n_size):                                   // ACCUMULATE over N heads, :218
          nc_matmul(
            res_psum[:S, :H],
            stationary = attention_sb[head_idx][:d_size, S-slice],       // D on partition (contraction)
            moving     = w_sbuf[head_idx][:d_size, H-slice])             // D on partition, H free, :227-231
        res_sb = (bias) ? tensor_tensor(res_psum + bias, add)            // :236
                        : tensor_copy(res_psum)   // engine alternates by parity, :243
      // each LNC core writes its DISJOINT H columns — no reduction:
      dma_copy(out_hbm[b, s_offset:, h_start:h_start+h_block], res_sb)   // :278
```

The golden reference confirms the math: `attn.permute(0,3,1,2).reshape(B,S,N·D) @ weight[N·D,H] → [B,S,H]` (`output_projection_cte_torch.py:208`).

> **QUIRK —** the orientation differs from `qkv`. Both kernels make `input` the **stationary** operand, but `qkv` contracts `H` while o_proj contracts `D`. In o_proj `stationary=attention[D, S]` has free axis `S` (which becomes the *partition* of the output) and `moving=weight[D, H]` has free axis `H` (which becomes the *free* of the output) → `res_psum[S, H]`.

### Head packing

When `D < P_MAX`, o_proj folds multiple heads into the partition to fill the 128-wide contraction. `_calculate_head_packing` (`cte_parameters.py:193-196`) and `calculate_head_packing` (`output_projection_utils.py:36-43`) find `group_size` = the largest divisor of `N` with `group_size·D ≤ 128`, then `new_N = N//group_size`, `new_D = D·group_size`. For `D=128`, `group_size=1` (no packing). MX/STATIC_MX force `group_size=1` (`cte_parameters.py:322-323`); the TKG variant gates head-packing on `D % 32 == 0`. The two listed definitions are near-identical duplicates of one routine, not two behaviours.

### Dtype dispatch

The dtype coverage is **asymmetric** between CTE and TKG, and asymmetric vs `qkv` (where MX existed in both variants):

| dtype | CTE | TKG | Notes |
|---|---|---|---|
| `NONE` (bf16/fp16/fp32) | ✓ | ✓ | PSUM fp32 (non-MX) / bf16 (MX); cast attn→weight dtype to avoid mixed-precision matmul |
| `STATIC` (FP8 per-tensor) | ✓ | ✓ | double-row 2× (N-even); `combined_scale = weight_scale·input_scale` |
| `ROW` (FP8 per-channel) | — | ✓ | TKG-only; activations **not** quantized, dequant is post-matmul per-`H` multiply |
| `MX` (FP4 `float4_e2m1fn_x4`) | ✓ | — | CTE-only; true per-block E8M0 via `nc_matmul_mx` |
| `STATIC_MX` (FP8 `float8_e4m3fn_x4`) | ✓ | — | CTE-only; FP8 on MX engine, **neutral** E8M0, scalar dequant |
| `ROW_MX` | — | — | dead enum, unimplemented (as in `qkv`) |

CTE dispatches `STATIC`/`STATIC_MX`/`MX`/float at `output_projection_cte.py:155-199`; TKG asserts only `{NONE, STATIC, ROW}` at `output_projection_tkg.py:388-393`.

> **GOTCHA —** `ROW` is **silently ignored** in CTE. `build_quantization_config` (`cte_parameters.py:420-455`) handles only `NONE/STATIC/MX/STATIC_MX` and falls through to `is_enabled=False` for `ROW`, and the validate fn does not reject it — so a `ROW` request to the *CTE* kernel runs the **float** path with no dequant, producing silently wrong results. The reimplementer must reject `ROW` for CTE explicitly.

`STATIC_MX` runs per-tensor FP8 on the MX engine with **constant** E8M0 scales for 4× throughput — not true per-block MX. The constant is `127` (`= 2^(127−127) = 1.0`): `create_constant_mx_scales(..., scale_value: int = 127)` at `cte_tensor_io.py:575`, and the real dequant is a post-matmul `tensor_scalar(·combined_weight_scale)` (`cte_quantization.py:1136`), not the E8M0.

> **GOTCHA — the docstrings say `126`, the code says `127`.** Two docstrings (`cte_quantization.py:818` and `cte_tensor_io.py:579`) give the constant MX scale as `126`. The executed default is `127`, i.e. an E8M0 of exactly `1.0`. The docstrings are stale; trust `create_constant_mx_scales`.

---

## The No-Collective H-Shard

### Purpose

o_proj's multi-core (LNC) mode shards the **output** dimension `H` across cores. Because each core owns a disjoint slice of the output, the cores never need to exchange or sum partials — the "gather" is realised entirely by *where each core writes*. This is the purest gather of the projection family and is the defining structural fact of the kernel.

### The mechanism

The design intent is stated verbatim in the kernel docstring (`output_projection_tkg.py:34-37`):

```text
This kernel is designed with LNC support. When LNC>1, the H dimension is sharded
across the cores. We choose to shard on H as this avoids the need for any
inter-core collective operations, as each core produces part of the output tensor.
```

Each core computes `h_sharded = H // n_prgs` (`tkg.py:464`; CTE `h_sharded = H // LNC` at `cte.py:86`) and writes its disjoint column range:

```c
// TKG store — output_projection_tkg.py:806-811
dma_copy(
  dst = out_hbm[ ds(bxs_block.start, bxs_block.size),
                 ds(prg_id * h_sharded, h_sharded) ],   // disjoint H columns
  src = out_sb)

// CTE store — output_projection_cte_float.py:75,96-100
h_start = h_sharded_size * prg_id + h_block_idx * tile_size
output_view = TensorView(out_hbm).select(dim=0, index=batch_idx)
                                 .slice(dim=1, start=h_start, end=h_start+h_block)
```

`get_program_sharding_info()` supplies `(_, n_prgs, prg_id)`, defaulting to `n_prgs=1, prg_id=0` outside an SPMD context (`output_projection_cte.py:104-109`).

> **GOTCHA —** an `rg` for `all_reduce|reduce_scatter|sendrecv|all_gather|collective` over the entire o_proj tree returns exactly **one** hit — and it is inside the docstring above (`tkg.py:36`), not a kernel body. There is **zero** collective machinery in either o_proj. The contraction axis `N·D` is **fully present on every core** and accumulated locally (the `for head_idx` loop); only the output `H` is split. Do not insert a reduce here.

### Why this is not the TP all-reduce

In Megatron-style tensor parallelism, attention heads are column-sharded so o_proj's *input* (`N·D` contraction) is row-sharded across ranks, forcing an all-reduce over the contraction. The shipped NKI kernel does the opposite: it receives the **full, un-TP-sharded** `N·D` contraction per core (weight `[N·D, H]` is complete) and shards the **output** `H` across the LNC *logical* cores. So o_proj's own LNC parallelism is intra-layer hidden-dim partitioning that yields a partial-`H` output needing only concatenation (gather) — not a contraction reduce.

Any cross-*rank* TP all-reduce (distinct from the LNC logical-core shard) is a **separate HLO collective** — `AllReduce` (SUM, for TP) or `ReduceScatter` (for sequence-parallel TP) — lowered by the graph compiler and inserted *around* this leaf kernel, never inside it. See [AllReduce/ReduceScatter Combiners](../hlo-opt/collective-combiners.md) for the graph-level placement and threshold model.

> **GOTCHA — o_proj is not "the TP all-reduce point".** It is commonly described that way, which leads readers to look inside the kernel for a `sendrecv`-reduce. There is none. The kernel's LNC mode is the **gather** half — disjoint HBM writes — and any TP reduce is a graph-level collective wrapped *around* this leaf.

---

## NISA Primitive Sequences

### QKV CTE (non-MX, full feature)

```text
dma_compute(input + mlp_prev + attn_prev)            [fused_add]      cte:873
activation_reduce(square,+) / rsqrt + tensor_scalar  [RMS]           cte:3028
  | bn_stats ×⌈H/512⌉ + bn_aggr + rsqrt              [LayerNorm]     cte:3083
nc_transpose(input ⊥ identity → PSUM)                [PE-transpose]  cte:997
nc_matmul(stat=input_T, mov=W) → PSUM[S,≤512]/bank   [fused QKV]     cte:1401
  | double_row variant (STATIC fp8)                                  cte:1365
[PSUM→SBUF] tensor_copy | scalar_tensor_tensor(·w_scale + bias)      cte:1450
  | RoPE: dma cos/sin → (−X2·sin, X1·sin, X·cos, add) per Q/K head   cte:3245
  | V heads tensor_copy as-is                                        cte:3283
[opt FP8 KV] tensor_scalar(/scale, clamp) + dma_copy (indirect)      cte:487
dma_copy SBUF→HBM (BSD direct / NBSd per-head .ap) + core_barrier    cte:1634
```

### o_proj CTE (float)

```text
dma_copy(weight per head → w_sbuf)                                   cte_io:255
dma_copy(bias[1,h] → broadcast[128,h])                              cte_io:139
dma_copy(attention per head → attn_sb[D,S])                         cte_io:171
nc_matmul(res_psum[S,H], stat=attn[D,S], mov=w[D,H]) × N heads      cte_float:227
tensor_tensor(res_sb = res_psum + bias) | tensor_copy(alt eng)     cte_float:236/243
dma_copy(out_hbm[b, S, h_start:] ← res_sb)   [DISJOINT H cols]      cte_float:278
```

MX paths interpose `quantize_mx(→fp8_x4 + uint8 E8M0)` and swap `nc_matmul` for `nc_matmul_mx(±E8M0 scales)`; `STATIC_MX` uses const-127 scales and a post-matmul `tensor_scalar(·combined_scale)` dequant.

---

## Related Components

| Name | Relationship |
|---|---|
| `qkv` | input projection; produces the `[B,S,I]` / `[N,B,S,d]` Q\|K\|V consumed by attention |
| `output_projection` | output projection; the mirror contraction folding attention output back to `H` |
| `attention_block_tkg` | the decode caller that imports and wires both kernels |
| `mlp` (gate/up/down) | the same LNC shard-axis triad: gate/up reduce (contraction shard), down gathers (output shard) |

---

## Cross-References

- [Flash-Attention: Context (CTE)](attention-cte.md) — the attention kernel that consumes the QKV output and feeds o_proj
- [NKI Architecture Overview & the 3-Layer Lowering Stack](architecture-overview.md) — how these `@nki.jit` kernels trace to penguin.ir then BIR
- [nki.isa COMPUTE Intrinsics & Validators](isa-compute-intrinsics.md) — `nc_matmul`, `nc_matmul_mx`, `bn_stats`/`bn_aggr`, `quantize_mx`
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition PE array and 512-column PSUM banks the fused matmul tiles into
- [Worked Example — Matmul Lowering](../front/worked-example-matmul.md) — the matmul descent these projections specialise
- [AllReduce/ReduceScatter Combiners & Threshold Model](../hlo-opt/collective-combiners.md) — the *graph-level* TP collective placed around o_proj (the reduce o_proj itself does **not** emit)
- RoPE *(planned)* — the standalone rotary-embedding kernel; the QKV CTE variant fuses this scheme in `_copy_psum_to_sbuf_apply_rope_and_bias`
