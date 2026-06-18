# Normalization Kernels — the RMSNorm & LayerNorm Family

> *All file:line citations on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310; the cp311 subkernel tree is byte-identical, `diff -q` empty; cp312 ships the same un-minified source). The evidence base is the shipped, decompressed nkilib wheel under `nkilib/core/` — readable Apache-2.0 NKI Python, **not** a stripped binary, so an nkilib `.py` line quoted here is itself a binary-derived wheel artifact. Provenance is drawn from RE reports **D-O11** (RMSNorm trio) and **D-O12** (LayerNorm + NormType taxonomy).*

## Abstract

The Neuron transformer pre-norm is not one kernel. It is a **four-member type vocabulary** (`NormType`, [§ The NormType family](#the-normtype-family-4-members-exact-semantics)) backed by **two implementation families** that split on execution phase. The **decode** (token-generation, "TKG") family normalizes one token at a time with the hidden dim `H` laid out on the **partition** axis and computes its statistics with a *software* reduction — `activation(square)` → free-dim `tensor_reduce` → a `nc_matmul` against an all-ones / all-`1/H` constant matrix that does the cross-partition sum and the `÷H` mean divisor in a single PE pass. The **prefill** (context-encode, "CTE") family lays `H` on the **free** axis (rows = tokens) and computes mean+variance with the silicon **BNStats** Pool/BN-engine path (`nisa.bn_stats` → `nisa.bn_aggr`, Welford in hardware). These are **two genuinely different LayerNorm implementations**, selected by data layout, not a config flag.

The single highest-value, most-misquoted fact on this page is **epsilon placement**. Every variant — all three RMSNorm kernels, decode LayerNorm, CTE LayerNorm, and the golden torch reference — adds eps **inside the sqrt, to the mean-of-squares (RMS) or to the variance (LN)**, never outside. The RMS form is `x·γ · rsqrt(mean(x²) + eps)` = `x·γ / sqrt(mean(x²)+eps)`, and it is implemented on-device by passing `eps` as the **bias** of an `nl.rsqrt` activation whose **scale** carries the `1/H`. The wrong form `1/(sqrt(mean(x²))+eps)` does **not** appear anywhere. The proof is byte-exact and lives in [§ Eps-inside-sqrt — the proof](#eps-inside-sqrt-the-proof).

The page is organized: the `NormType` family and dispatch, the eps proof, the three RMSNorm variants (plain / int8-fp8 quant / MX-quant), the decode LayerNorm software-reduce, the CTE BNStats Welford path, then `SKIP_GAMMA` and the adversarial self-check. A reader who finishes it can reimplement the exact arithmetic of all six algorithms and route a `NormType` to the correct kernel.

For reimplementation, the contract is:

- **`NormType` = 4 members** — `NO_NORM=0`, `RMS_NORM=1`, `LAYER_NORM=2`, `RMS_NORM_SKIP_GAMMA=3`; classifier predicates `is_rms_normalization` / `normalization_uses_weights`.
- **Eps = `1e-6` default, inside the sqrt** — passed as the `bias` of `nl.rsqrt`; `scale` carries the `1/H` (RMS-TKG path) or the `1/H` lives in the matmul constant (LN-TKG path).
- **Two stats engines** — decode = software reduce (`activation`+`tensor_reduce`+`nc_matmul`); prefill = BNStats Welford (`bn_stats` DST=6 / `bn_aggr` =2).
- **RMSNorm = 1 moment** (`mean(x²)` only, no centering, no β); **LayerNorm = 2 moments** (`Var = E[X²] − E[X]²`, centering, optional β).
- **Three RMSNorm output flavors** — plain bf16/fp16, fused int8/fp8 (240-max e4m3, ROW/STATIC), fused MX/E8M0 (448-max e4m3fn, 32-element block scale).

| | |
|---|---|
| **Type vocabulary** | `NormType` (4 members) — `common_types.py:25-29` |
| **Decode RMSNorm (plain)** | `rmsnorm_tkg` — `subkernels/rmsnorm_tkg.py` (560 ln) |
| **Decode RMSNorm + int8/fp8** | `rmsnorm_quant_kernel` — `rmsnorm/rmsnorm_quant.py` (868 ln) |
| **Decode RMSNorm + MX/E8M0** | `rmsnorm_mx_quantize_tkg` — `subkernels/rmsnorm_mx_quantize_tkg.py` (383 ln) |
| **Decode LayerNorm** | `layernorm_tkg` — `subkernels/layernorm_tkg.py` (636 ln) |
| **Prefill LayerNorm (BNStats)** | `_compute_layer_norm_stats` — `qkv/qkv_cte.py:3083` ; `mlp/mlp_cte/mlp_cte_norm.py:164` |
| **Golden refs** | `rmsnorm_torch.py` / `layernorm_torch.py` (`subkernels/`) |
| **Eps default** | `1e-6` (all variants) |

---

## The NormType family (4 members, exact semantics)

The enum is exactly four members, transcribed verbatim (**CONFIRMED**, `common_types.py:25-29`):

```python
class NormType(Enum):
    NO_NORM = 0
    RMS_NORM = 1
    LAYER_NORM = 2
    RMS_NORM_SKIP_GAMMA = 3
```

Two canonical classifier helpers key every dispatch decision (**CONFIRMED**, `kernel_helpers.py:229-268`):

```python
def is_rms_normalization(t):       # kernel_helpers.py:251
    return t == RMS_NORM or t == RMS_NORM_SKIP_GAMMA
def normalization_uses_weights(t): # kernel_helpers.py:254-268
    return t == RMS_NORM or t == LAYER_NORM   # ⭐ SKIP_GAMMA uses NO weights
```

The MLP layer wraps these into predicate accessors (`mlp_parameters.py`): `mlpp_has_normalization = (t != NO_NORM)`, `mlpp_has_rms_normalization = is_rms_normalization(t)`, `mlpp_has_layer_normalization = (t == LAYER_NORM)`, `mlpp_has_normalization_weights = has_norm and uses_weights(t)`.

| Member | Value | Math | Centers? | β? | Weights (γ)? | Decode kernel |
|---|---|---|---|---|---|---|
| `NO_NORM` | 0 | identity load → `[H0,BxS,H1]` | no | no | no | direct `_input_load` |
| `RMS_NORM` | 1 | `x·γ · rsqrt(mean(x²)+eps)` | no | no | **yes** | `rmsnorm_tkg` |
| `LAYER_NORM` | 2 | `γ·(x−μ)·rsqrt(var+eps)+β` | **yes** | optional | **yes** | `layernorm_tkg` |
| `RMS_NORM_SKIP_GAMMA` | 3 | `x · rsqrt(mean(x²)+eps)` (γ pre-folded) | no | no | **no** | CTE-only (§ SKIP_GAMMA) |

> **NOTE — `is_rms_normalization` covers SKIP_GAMMA.** Because the predicate returns `True` for both `RMS_NORM` and `RMS_NORM_SKIP_GAMMA`, the *defining* property of member 3 is read off the **second** predicate: `normalization_uses_weights` returns `False`. SKIP_GAMMA is "an RMS denominator that uses no γ tensor." Anyone who classifies it by the first predicate alone will incorrectly route it through the γ-multiply (`kernel_helpers.py:254`).

### Dispatch — inlined per megakernel, never a single module

There is **no** central norm-dispatch function; each megakernel inlines a `NormType` switch on the `mlpp_has_*` predicates. **Decode** sites — `mlp_tkg_utils.py:387-405` and the QKV megakernel `qkv_tkg.py:805-847` — branch only `{NO_NORM, RMS_NORM, LAYER_NORM}`; **SKIP_GAMMA never reaches decode** (no decode caller constructs it, so the SKIP_GAMMA arm is unreachable there — **INFERRED-STRONG** from grep: zero SKIP_GAMMA branches in the TKG files). **Prefill** sites — `qkv_cte.py` / `mlp_cte_norm.py` — handle all four. The public surface `qkv.py:154-161` / `qkv_cte.py:182-183` documents `fused_norm_type ∈` all four members; `transformer_tkg.py:296` imports `NormType` and sets `RMS_NORM` for the whole block's pre-norm.

---

## Eps-inside-sqrt — the proof

This is the precision-critical claim. It is proven three ways: against the torch golden ref, against the on-device `rsqrt` activation in each TKG kernel, and against the CTE rsqrt.

**(1) Golden reference (`rmsnorm_torch.py:44-52`, CONFIRMED verbatim):**

```python
if hidden_actual is not None:
    sum_squares = hidden.square().sum(dim=-1, keepdim=True)
    rms = (sum_squares / hidden_actual + eps).sqrt()   # ⭐ eps INSIDE sqrt, on the MEAN
else:
    rms = (hidden.square().mean(dim=-1, keepdim=True) + eps).sqrt()  # ⭐ same
norm = hidden * rms.reciprocal()                       # x / sqrt(mean(x²)+eps)
if gamma is not None: norm *= gamma                    # ⭐ γ AFTER normalization
```

The parenthesization is unambiguous: `(mean + eps).sqrt()`, then `.reciprocal()`. There is no `(sqrt(...) + eps)` anywhere. `hidden_actual` decouples the divisor from a padded `H` — when `H` is padded to a 128/512 multiple, the mean still divides by the *real* hidden size; default `hidden_actual = H`.

**(2) On-device rsqrt (decode RMSNorm, `rmsnorm_tkg.py:420-426`, CONFIRMED):**

```python
hidden_scale = 1.0 / hidden_actual           # rmsnorm_tkg.py:420
nisa.activation(
    dst=rms_recip, op=nl.rsqrt,
    data=final_reduced_sum_of_squares,        # = Σx²  (cross-partition sum in PSUM)
    scale=hidden_scale,                        # ⭐ scale = 1/H
    bias=eps_view.get_view(),                  # ⭐ bias = eps
)
```

The `activation` primitive computes `op(scale·data + bias)`. Substituting: `rsqrt((1/H)·Σx² + eps)` = `rsqrt(mean(x²) + eps)` = `1/sqrt(mean(x²)+eps)`. **Byte-exact match to the torch ref.** The `eps` lands inside the sqrt because it is the *bias* added *before* the `rsqrt` op evaluates — proven by `eps_view` being memset to `eps` (`rmsnorm_tkg.py:528`, `nisa.memset(eps_sb, value=eps)`) and passed straight into the activation bias.

> **QUIRK — where the `÷H` lives differs between RMS-TKG and LN-TKG.** In `rmsnorm_tkg` the cross-partition reduction matrix is `memset(value=1.0)` (`rmsnorm_tkg.py:535`) — a *plain* ones matrix that only sums; the `÷H` is deferred into the rsqrt `scale=1/hidden_actual`. In `layernorm_tkg` the same matrix is `memset(value=(1.0/hidden_actual))` (`layernorm_tkg.py:604`) — the `÷H` is folded into the **matmul constant** instead, so the matmul outputs the *mean* directly and the rsqrt's `scale` is implicit `1.0`. Same net math, different fusion point. Do not conflate the two constants. (**CONFIRMED** both sites.)

**(3) CTE / prefill rsqrt (`qkv_cte.py:3133-3138`, CONFIRMED):** `nisa.activation(data=variance, bias=norm_eps, op=nl.rsqrt)` → `1/sqrt(var + eps)`. Same `bias=eps`, same inside-the-sqrt placement, on the BNStats-produced variance.

> **CORRECTION (supersedes any "eps-outside" or "rsqrt-at-HLO" claim).** The HLO-side fusion `fuseMulRedSqrt` (Part 4, [`rmsnorm-fusion-cluster-codegen.md`](../hlo-opt/rmsnorm-fusion-cluster-codegen.md)) captures only the `mul(x,x) → reduce(+) → sqrt` *denominator core* (3 ops; it matches `mhlo::SqrtOp`, **not** rsqrt). The `+eps`, the reciprocal (the `rsqrt` fuses sqrt+recip), the γ broadcast-multiply, and the output normalize all live in **these NKI kernels**, outside the HLO cluster. So `MulRedSqrt`(HLO) ⊂ `rmsnorm_*`(NKI). Treating the HLO fusion as the whole RMSNorm — or putting eps at HLO — is wrong.

**Eps default is `1e-6` across every variant** (CONFIRMED): `RmsNormQuantKernelArgs.eps:56`, `rmsnorm_tkg(eps=1e-6)`, `rmsnorm_mx_quantize_tkg(eps=1e-6)`, `layernorm_tkg(eps=1e-6)` (`layernorm_tkg.py:47`), `qkv.norm_eps`, `MLPNormParameters.eps`. The quant kernel asserts `eps >= 0` (`rmsnorm_quant.py:71`).

---

## RMSNorm variant 1 — `rmsnorm_tkg` (plain decode, bf16/fp16 out)

`rmsnorm_tkg(input, gamma, output, eps=1e-6, hidden_actual=None, hidden_dim_tp=False, single_core_forced=False, shard_on_h=False, …)`. Output is the normalized tensor laid out `[128, BxS, H/128]` (transposed so the downstream sharded router/MLP matmul reads contiguous H-halves). The per-tile core (`_process_rmsnorm_tile`, `rmsnorm_tkg.py:306-442`, CONFIRMED) is:

```python
# all intermediates inter_dtype = nl.float32
square   = nisa.activation(op=nl.square, data=input)                 #  x²            :357
reduced  = nisa.tensor_reduce(nl.add, square, axis=2)                #  Σx² over H1   :367
# [shard_on_h only: sendrecv the [H0,BxS] partial + tensor_tensor(add) → full Σx² across H0]
gmul     = nisa.tensor_tensor(input, gamma, nl.multiply)            #  x·γ  (eager)
final_Σ  = nisa.nc_matmul(stationary=ones[H0,H0], moving=reduced)    #  cross-partition Σ → PSUM :413
rms_rcp  = nisa.activation(op=nl.rsqrt, data=final_Σ,
                           scale=1/hidden_actual, bias=eps)          #  1/sqrt(mean(x²)+eps) :421
out      = nisa.tensor_tensor(gmul, rms_rcp_bcast, nl.multiply)      #  (x·γ)/rms
```

BxS is tiled by `BxS_FULL_TILE_SIZE=512`. The `nc_matmul(ones)` does the cross-partition (H0) reduction *and* nothing else (ones, not 1/H — see the QUIRK above). Sharding has two modes:

- **shard-on-BxS** (default, `_rmsnorm_tkg_shard_on_bxs:138`): splits tokens across the two cores iff `LNC==2 ∧ BxS > SHARDING_THRESHOLD(=18) ∧ BxS%lnc==0` (`rmsnorm_tkg.py:31,182`). Each token's full `H` is local, so **no cross-core reduce** is needed; a final `sendrecv` swaps the two output halves if the output is SBUF.
- **shard-on-H** (`_rmsnorm_tkg_shard_on_h:232`): splits `H`. Each core's partial `Σx²` is **sendrecv'd and `tensor_tensor(add)`-combined before the rsqrt** (`rmsnorm_tkg.py:379-393`) — the variance needs the full `H`. Mutually exclusive with `hidden_dim_tp`/`single_core_forced`.

---

## RMSNorm variant 2 — `rmsnorm_quant_kernel` (CTE-style, fused int8/fp8 quant)

`rmsnorm_quant_kernel(hidden[B,S,H], ln_w[H], kargs, input_dequant_scale=None)` is the **context-encode-style** fused quantize kernel: it collapses `[B,S,H]→[OD,PD]`, tiles the outer dim into `pmax(=128)`-row tiles, and per tile does **load → (optional) RMSNorm → quantize → store** entirely in SBUF with no HBM round-trip (`_rmsnorm_quant_single_core_kernel:703`). It accepts only `NormType ∈ {NO_NORM, RMS_NORM}` × `QuantizationType ∈ {ROW, STATIC}` and asserts away everything else (`:60-66`).

**Fused RMSNorm tile math** (`_rms_normalize_tile`, `rmsnorm_quant.py:446-516`, CONFIRMED), here each *row* is a partition, so a single reduce suffices:

```python
nisa.activation_reduce(op=nl.square, reduce_op=nl.add, data=in_tile,
                       reduce_res=inverse_rms_scale, scale=1.0)        # Σx² per row  :446
nisa.activation(op=nl.rsqrt, data=Σx², bias=rmsn_eps_bias,
                scale=1/constants.proc_dim_size)                       # 1/sqrt(mean+eps) :458-461
nisa.nc_matmul(stationary=pe_broadcast_ones, moving=gamma)            # bcast γ[1,H] → PSUM :478
nisa.scalar_tensor_tensor(data=in_tile, op0=multiply, operand0=1/rms,
                          op1=multiply, operand1=γ_bcast)             # x·(1/rms)·γ in place :487
```

The proc dim is tiled into `gemm_moving_fmax`-wide free tiles iterated in **batches of 8** (`:468`, manual unroll for the 8 PSUM banks) with a remainder loop. RMS reduce/rsqrt run in FP32; only the matmul/γ path uses bf16 `compute_data_type`.

> **NOTE — divisor here is `proc_dim_size`, not `hidden_actual`.** The rsqrt scale at `rmsnorm_quant.py:461` is `1/constants.proc_dim_size` — the *padded/tiled* proc dimension — whereas the TKG kernels divide by `hidden_actual`. For an un-padded `H` these coincide; for a padded `H` the quant kernel divides by the proc-dim. (**CONFIRMED** `:461`; contrast `rmsnorm_tkg.py:420`.)

**ROW quantize** (`_row_quantize_tile:526`, per-row dynamic fp8): `tensor_scalar_reduce(abs, maximum)` → `max|x|`; optional clip to `lower_bound` (active iff `> 1e-6`); `dequant_scale = max|x| / range` with `range = 240`; clamp `scale ≥ min_dequant_scale_value(=1e-6)` against reciprocal blow-up; `quant_scale = 1/dequant_scale` (`nisa.reciprocal`, `:308`); `tensor_scalar(multiply)` → cast to `float8_e4m3` on store. **STATIC quantize** (`_static_quantize_tile:623`) uses a caller-supplied `input_dequant_scale`, inverted once on entry (`:308`), then per tile `multiply` and **clamp to `[-240, 240]`**.

> **CORRECTION — two distinct fp8 targets, two ranges.** This kernel quantizes to **240-max `float8_e4m3`** (the **non-fn** e4m3 whose max finite is `2^7·(1+2^7/2^8)=240`, derived verbatim in `rmsnorm_quant_constants.py:86-87`). The MX path (variant 3) targets **448-max `float8_e4m3fn`** (the OCP "fn" variant, `mx_torch_common.py:194,198`). They are *not* the same dtype; anyone citing 448 for the ROW/STATIC path is wrong, and vice-versa.

**ROW output shape = `[B,S,H+4]`** (`rmsnorm_quant.py:152`): the extra 4 fp8 elements per row hold the fp32 dequant scale reinterpreted as 4×fp8 bytes (`dequant_scale_size = 4/1 = 4`, `constants.py:92-95`), stored via an access-pattern reinterpret cast. STATIC output is `[B,S,H]` (scale is global, no tail).

---

## RMSNorm variant 3 — `rmsnorm_mx_quantize_tkg` (decode, fused MX/E8M0)

`rmsnorm_mx_quantize_tkg(input, gamma, output, output_quant, output_scale, residual=None, output_residual=None, eps=1e-6, hidden_actual=None, hidden_dim_tp=True)`. A **decode** kernel that fuses **(optional residual-add) → RMSNorm → MXFP8 quantize** in one pass, emitting **both** an un-quantized bf16/fp16 `output` (the router GEMM input) **and** a quantized `output_quant` + `output_scale` (the expert-MLP input). The file docstring states the fusion verbatim: *"Fused optional residual add + RMSNorm + MX quantization … for token generation (decoding) phase"* (`rmsnorm_mx_quantize_tkg.py:15,42`).

The per-tile pipeline (`:140-258`, CONFIRMED): `dma_transpose` load `(BxS·H1,H0)→(H0,BxS_tile,H1)`; **residual add `hidden = input + residual`** (iff `is_residual_add`, `:74,85`) — note the summed tensor is what gets normalized **and** spilled, i.e. the **pre-norm residual pattern `RMSNorm(x + residual)`**; `activation(square)`; `tensor_tensor(·γ_bcast)`; `tensor_reduce(add, axis=2)` then `nc_matmul(ones[H0,H0])` to finish the cross-partition `Σx²`; `activation(rsqrt, scale=1/hidden_actual, bias=eps)` (identical to §1); `tensor_tensor(·(1/rms)_bcast)` → `output`; swizzle to `[H0, n_H512, BxS_tile, _q_width]` then `nisa.quantize_mx` (`:254`).

**The MX / E8M0 block scheme** (`mx_torch_common.py:211-215` = hardware golden, CONFIRMED). The block geometry is `_q_height=8 × _q_width=4 = 32-element OCP block` (`projection_mx_constants.py:28-29`):

```python
exp_field     = (data_f32.view(uint32) >> 23) & 0xFF                  # IEEE-754 biased exp per elem :211
block_max_exp = exp_field.reshape(P//8, 8, F//4, 4).max(axis=(1,3))   # max exp over the 8×4 block   :212
scale_uint8   = (block_max_exp - max_exp).clip(0, 255)                # ⭐ E8M0 scale = exp delta, 1 byte :213
scale_factor  = 2**(block_max_exp - max_exp - 127)                    # :215
clipped       = clip(x / scale_factor, -max_val, max_val)            # → static_cast → fp8_e4m3fn_x4 :217
```

For `float8_e4m3fn_x4`: `(max_exp, max_val) = (8, 448.0)` (`mx_torch_common.py:198`). So the **scale is E8M0** (8-bit exponent, zero mantissa) — a raw power-of-two exponent, **one `uint8` per 32-element block**, *not* a float scale — and the data element is **e4m3fn (max 448)**. The HW scale layout packs the 4 valid scale rows into the **leading 4 partitions of each 32-partition quadrant** (28 zero-padded holes per quadrant; `quantize_mx_golden:266-278`).

**Outputs** (`validate_shapes_quantize_mx`, `norm_tkg_utils.py:427`): `output[H0,BxS,H1]` fp16/bf16, `output_quant[H0, H/512, BxS]` e4m3fn_x4, `output_scale[H0, H/512, BxS]` uint8, `output_residual[BxS,H]` (only if residual). Requires `H%512==0`, `LNC==2`, `BxS%4==0`. After compute, **three `nisa.sendrecv` exchanges** (pipes 0/1/2) gather the other core's halves of `output`/`output_quant`/`output_scale` so both cores hold the complete tensors for `router_topk`. The summed `hidden` is PE-transposed and DMA'd to `output_residual` (HBM) as the pre-norm residual stream carried to the next layer.

> **CROSS-REF — RMSNorm output = router input.** The MoE decode megakernel ([6.7.3, in flight]) Step-1 dispatch selects `rmsnorm_mx_quantize_tkg` in MXFP all-expert mode and `rmsnorm_tkg` otherwise; the **un-quantized** RMSNorm `output` is fed straight into the router GEMM, while the quantized twin goes to the experts — one fused norm pass, two consumers. The MoE megakernel **fuses** an RMSNorm internally; the kernels on this page are the **standalone** family. See also the CTE/prefill MoE norm in [`moe-cte-prefill.md`](moe-cte-prefill.md), and the nkilib allocator/tiling these all sit on in [`nkilib-infrastructure.md`](nkilib-infrastructure.md).

---

## Decode LayerNorm — `layernorm_tkg` (software reduce, NO BNStats)

`layernorm_tkg(input, gamma, output, beta=None, eps=1e-6, shard_on_h=False, …)`. Output `[H0, BxS, H1]`; `H` divisible by 128; β optional (γ-only LayerNorm is legal). The per-tile core (`_process_layernorm_tile`, `layernorm_tkg.py:303-498`, docstring `:320-321`: *"output = gamma * (input - mean)/sqrt(var+eps)+beta where var = E[X^2] - E[X]^2"*, CONFIRMED) computes **two moments** via a *software* reduce — no hardware BNStats:

```python
# inter_dtype = nl.float32 for ALL intermediates
sq        = nisa.activation(op=nl.square, data=input)                        # x²       :362
Σsq       = nisa.tensor_reduce(nl.add, sq,    axis=2)  # → packed[:, 0:BxS]   :380
Σx        = nisa.tensor_reduce(nl.add, input, axis=2)  # → packed[:, BxS:2BxS]:384
# Σx² and Σx PACKED into one [H0,2·BxS] buffer → a SINGLE sendrecv in shard_on_h
# [shard_on_h only: sendrecv packed[H0,2BxS] + tensor_tensor(add) to combine partials :386-410]
E_x2      = nisa.nc_matmul(stationary=const_1_over_H[H0,H0], moving=Σsq)      # mean(x²) :426
E_x       = nisa.nc_matmul(stationary=const_1_over_H[H0,H0], moving=Σx)       # mean(x)  :433
centered  = nisa.tensor_tensor(input, E_x_bcast, nl.subtract)                 # x − μ    :441
E_x_sq    = nisa.activation(op=nl.square, data=E_x)                           # E[X]²    :450
var       = nisa.tensor_tensor(E_x2, E_x_sq, nl.subtract)                     # E[X²]−E[X]² :456
rsqrt     = nisa.activation(op=nl.rsqrt, data=var, bias=eps)                  # 1/sqrt(var+eps) :466
out       = nisa.tensor_tensor(centered, gamma, nl.multiply)                  # (x−μ)·γ  :469
out       = nisa.tensor_tensor(out, rsqrt_bcast, nl.multiply)                 # ·(1/σ)   :478
if beta:  out = nisa.tensor_tensor(out, beta, nl.add)                         # +β       :486
```

⇒ `output = ((x − μ)·γ)·rsqrt(var+eps) + β`, algebraically `γ(x−μ)/σ + β`. The variance is **population** variance (`÷H`, not `÷(H−1)`) — the `1/H` lives in the matmul constant (`memset(1.0/hidden_actual)`, `:604`). The two `nc_matmul`s (vs RMSNorm's one) do both the cross-partition sum and the mean divisor. Sharding mirrors RMSNorm but with `SHARDING_THRESHOLD=10` (`:36`) — **lower** than RMSNorm's 18, because LayerNorm's higher per-token cost amortizes the collective overhead at smaller `BxS` (INFERRED-STRONG).

> **CORRECTION (supersedes the brief's "decode LayerNorm uses the BNStats HW path" hypothesis).** It does **not**. The decode/TKG LayerNorm is the software reduce above; `grep` finds **zero** `bn_stats`/`bn_aggr` in the entire `subkernels/` directory (verified: `rg -c` empty). BNStats is the **prefill/CTE** path only (next section). Decode and prefill LayerNorm are **two different implementations**, selected by data layout.

---

## Prefill LayerNorm — the BNStats (Welford) hardware path

The CTE LayerNorm uses the silicon Pool/BN engine via `nisa.bn_stats` → `nisa.bn_aggr`. Transcribed from `_compute_layer_norm_stats` (`qkv_cte.py:3100-3138`, CONFIRMED verbatim):

```python
BN_STATS_TILE_SIZE = 512 ; BN_STATS_DST_SIZE = 6        # qkv_cte.py:3101-3102
for i_bn_tile in range(ceil(H / 512)):                  # one 512-col H-tile at a time
    nisa.bn_stats(dst=bn_stats_result[:, i*6 : i*6+6],  # ⭐ 6 metrics/tile  :3120-3123
                  data=input_row[:, off:off+512])        #    (Welford count,mean,M2 ×2-lane)
NUM_AGGR_STATS = 2                                       # :3126
nisa.bn_aggr(dst=bn_aggr_result[:, 0:2],                # ⭐ → [mean, variance] :3127-3130
             data=bn_stats_result[:, 0 : 6·Ntiles])
nisa.activation(dst=bn_aggr_result[:, 1:2], op=nl.rsqrt,
                data=bn_aggr_result[:, 1:2], bias=norm_eps)  # var → 1/sqrt(var+eps) :3133-3138
```

The result tile then holds `col0 = mean`, `col1 = rvar = 1/sqrt(var+eps)`, applied as `(x − mean)·rvar` with γ fused (`qkv_cte.py:951-959`) and optional β. The same `bn_stats(6)/bn_aggr(2)/rsqrt` construct recurs in `mlp_cte_norm.py:260-271`. RMSNorm in CTE uses **no** BNStats — it uses `activation_reduce(square)` + rsqrt (`qkv_cte.py:3055-3071`), the row-partition analogue of the decode path.

The `DST_SIZE=6` / `AGGR=2` are the Python-side witnesses of the binary BNStats encoding:

- `nisa.bn_stats` → `codegenBatchNormStats` → `InstBNStats` (IT34): output AP = **6 elements/partition** = two interleaved Welford `(count, mean, M2)` triples (2-way even/odd lane split). The shipped `BN_STATS_DST_SIZE=6` is the witness.
- `nisa.bn_aggr` → `codegenBatchNormAggregate` → `InstBNStatsAggregate` (IT35): input `%3==0` (Welford triples), output `==2` = `(mean, population variance Σdev²/N)` merged via Chan's parallel-variance `M2 = M2A + M2B + nA·nB·(μA−μB)²/N`. The shipped `NUM_AGGR_STATS=2` is the witness.

Both norms therefore produce **population variance** — the decode path's `E[X²]−E[X]²` and the prefill path's Welford-`÷N` agree (consistent). See the ISA-level encoding of these instructions in [`batchnorm-encoding.md`](../isa/batchnorm-encoding.md).

> **NOTE — why decode ≠ prefill.** Decode lays `H` on the **partition** axis (layout `[H0,BxS,H1]`), making a cross-*partition* reduction the bottleneck → the PE matmul-by-`1/H` trick reduces partitions for free and reuses PSUM. Prefill lays `H` on the **free** axis (rows = tokens) → the Pool/BN engine's free-dim Welford in one HW pass is ideal. Different layouts ⇒ different optimal stats engine. (INFERRED-STRONG; layouts explicit in both files.)

---

## `RMS_NORM_SKIP_GAMMA` — the gamma-pre-folded variant

`RMS_NORM_SKIP_GAMMA=3` computes the RMS denominator `x · rsqrt(mean(x²)+eps)` **without** the γ-multiply. It is used when γ has been algebraically folded into the next matmul's weight (`W' = diag(γ)·W`), so re-applying γ would double-count. It is **CTE-exclusive** (zero occurrences in any TKG decode file). In the CTE path the γ-load is guarded by `(RMS_NORM or LAYER_NORM)`, and the SKIP_GAMMA / NO_NORM arms just copy the transposed PSUM tile — comment verbatim: *"In NO_NORM, RMS_NORM_n, we just copy the transposed PSUM tile to SBUF"* (`qkv_cte.py`). The rsqrt/stats still run (it is still an RMS denominator); only the γ-mul is dropped.

> **GOTCHA — the validator REJECTS γ alongside SKIP_GAMMA.** `qkv_cte_utils.py:439-442` errors with *"gamma_norm_weights are provided, but fused_norm_type is RMS_NORM_n"* — supplying a γ tensor with SKIP_GAMMA is a hard error, because γ is supposed to already be inside the weight. (**CONFIRMED**.) Note the comment strings render the enum suffix as the mangled `RMS_NORM_n`; the authoritative member name is `RMS_NORM_SKIP_GAMMA` per `common_types.py:29`.

---

## Adversarial self-verification

The five strongest claims, re-challenged against source:

1. **Eps inside the sqrt, on the mean-of-squares.** Challenged hardest. Triple-proven: torch ref `(sum_squares/hidden_actual + eps).sqrt()` (`rmsnorm_torch.py:46`), device `rsqrt(scale=1/H, bias=eps)` = `rsqrt(mean(x²)+eps)` (`rmsnorm_tkg.py:421`, `rmsnorm_quant.py:458`, `rmsnorm_mx_quantize_tkg.py`), CTE `rsqrt(data=var, bias=eps)` (`qkv_cte.py:3133`). The `(sqrt(...)+eps)` form appears **nowhere**. **CONFIRMED.**
2. **Welford-vs-software-reduce is a phase split, not a flag.** Challenged hardest. `grep -c bn_stats` over `subkernels/` = **empty** (decode has none); BNStats appears only in `qkv_cte.py:3120` / `mlp_cte_norm.py:260`. Decode LayerNorm computes `E[X²]−E[X]²` in software (`layernorm_tkg.py:450-456`). Both yield **population** variance. **CONFIRMED** — corrects the brief's "decode uses BNStats" premise.
3. **`NormType` = exactly 4 members.** `common_types.py:25-29` shows `NO_NORM=0, RMS_NORM=1, LAYER_NORM=2, RMS_NORM_SKIP_GAMMA=3`. Not 3, not 5. **CONFIRMED.**
4. **Two distinct fp8 quant targets.** ROW/STATIC = `float8_e4m3` range **240** (`rmsnorm_quant_constants.py:87`, derivation in `:86`); MX = `float8_e4m3fn_x4` `max_val` **448** (`mx_torch_common.py:198`). Two ranges, two dtypes. **CONFIRMED.**
5. **The `÷H` fusion-point divergence (RMS const=1.0 vs LN const=1/H).** `rmsnorm_tkg.py:535` `memset(value=1.0)` vs `layernorm_tkg.py:604` `memset(value=(1.0/hidden_actual))`. RMS defers `÷H` into the rsqrt scale (`:420`); LN folds it into the matmul. **CONFIRMED.**

**Re-verification ceiling.** All Python-level math, enum, eps, dtype, and stats-path claims are **CONFIRMED** byte-exact against shipped wheel source (cp310; cp311 byte-identical). The `InstBNStats` IT34/IT35 opcode mapping and Welford lane-interleave semantics are **STRONG** (cross-referenced from the BNStats ISA encoding, not re-derived here). The SHARDING_THRESHOLD rationale (18 vs 10) and the decode-≠-prefill layout argument are **INFERRED-STRONG** — supported by the explicit constants/layouts but not stated as cause in any single comment. SKIP_GAMMA's unreachability in decode is **INFERRED** from the absence of a decode caller, not a positive assertion.
