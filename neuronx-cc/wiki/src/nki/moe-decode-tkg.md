# MoE Decode (TKG): the Fused Megakernel and the Strategy Leaves

> *All source line numbers on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (the cp310 wheel). The MoE-decode kernels are shipped as readable NKI-DSL Python under `nkilib/core/moe_block/` and `nkilib/core/moe/moe_tkg/` — a binary-derived wheel artifact, citeable verbatim. `moe_block_tkg.py` and `moe_tkg_affinity_masking.py` are byte-identical across cp310/cp311/cp312 (RECORD sha256), so the line numbers transfer to all three tags.*

## Abstract

`moe_block_tkg` is the nkilib *token-generation* (decode) Mixture-of-Experts block: a single `@nki.jit` kernel that **fuses** RMSNorm → RouterTopK → (shared expert) → expert-MLPs into one invocation. It is the latency-bound sibling of the prefill path [MoE Context/Prefill (CTE)](moe-cte-prefill.md): where CTE iterates *blocks* of pre-routed tokens through dense per-expert GEMMs and the stages are separate kernels, decode has only the few tokens of the active step (`T = B·S`, typically one per sequence). With no block of tokens to amortize a per-stage HBM round-trip over, decode wins by keeping the token's hidden vector **SBUF-resident** across norm, router, and MLP — read from HBM once as `inp`, normed once, and reused twice in place. That residency is the entire reason the megakernel exists (`moe_block_tkg.py:38`).

The page has two trees and one matrix. The first tree is the **orchestrator** `moe_block_tkg` (`moe_block_tkg.py:38-349`): a 35-parameter kernel that parses dimensions, runs the four fused stages, and forwards a strategy bool plus the weight dtype to a dispatcher. It makes *no* leaf choice itself. The second tree is the **dispatcher** `moe_tkg` (`moe/moe_tkg/moe_tkg.py:43`), a pure 2×2 leaf selector over `is_all_expert × is_mx_kernel` — four compiled-Python leaves, all four wired, no `else/raise` footgun. The matrix is those four leaves: all-expert vs. selective (the report-brief "selective" maps to iterating *tokens* and gathering each token's K routed experts) crossed with MX vs. bf16/fp8. Rather than four near-duplicate walkthroughs, the leaves are one table plus shared-mechanism prose; the MX projections (`gate_up`/`down`) and the expert-affinity scale-masking are documented once because all relevant leaves share them.

> **NOTE —** the readable `moe_block_tkg`/`moe_tkg` Python is the kernel-library convenience surface, not the production caller. No `moe_block_tkg(`/`moe_tkg(` call site exists anywhere in the extracted neuronx-cc tree; production NxD reaches the decode MoE through the *compiled* `neuronxcc.nki._private_kernels.{expert_mlps,router_topk,mlp}.cpython-310-x86_64-linux-gnu.so` extensions (verified present on disk). This page documents the algorithm as it reads in the shipped library source.

For reimplementation, the contract is:

- The **fused four-stage pipeline** and its SBUF/HBM residency contract: which intermediates stay in SBUF (the normed hidden, expert_index, eager affinities, MX-quant hidden + scales) and which three classes of tensor touch HBM (kernel I/O, the all-expert affinity hand-off, the optional residual).
- The **2×2 leaf dispatch** in `moe_tkg`: the `is_all_expert` caller bool × the runtime `is_mx_kernel` dtype check, the four leaf functions, and the affinity-mask gate that sits in front of them.
- The **expert-affinity scale-masking** (`mask_expert_affinities`): the rank-slice of the global `[T,E]` affinities to this rank's `[T,E_L]` locals via indirect DMA, and the optional `_apply_expert_index_mask` top-K zeroing — gated by the `router_pre_norm ∧ ¬norm_topk_prob` keystone.
- The **MX gate_up/down projections**: `nc_matmul_mx` with E8M0 scales riding inside the matmul, the SwiGLU `act(clamp(gate)) · clamp(up)` with online `quantize_mx` to FP8 for the down matmul, and the I-shard cross-core reduce.

| | |
|---|---|
| **Orchestrator** | `moe_block_tkg()` — `moe_block/moe_block_tkg.py:38` (`@nki.jit`) |
| **Dispatcher** | `moe_tkg()` — `moe/moe_tkg/moe_tkg.py:43` |
| **Affinity mask** | `mask_expert_affinities()` — `moe/moe_tkg/moe_tkg_affinity_masking.py:31` |
| **MX projections** | `gate_up_projection_mx.py` / `down_projection_mx.py` (refactored twin, with `_clamp_tensor`/`_matmul_mx_accumulate`) |
| **Leaf matrix** | `is_all_expert × is_mx_kernel` → 4 leaves (`moe_tkg.py:266-277`) |
| **Live stages** | RMSNorm (live) · RouterTopK (live) · SharedExpert (DEAD) · ExpertMLPs (live) |
| **MX dtype set** | `_SUPPORTED_MX_DTYPES = (float4_e2m1fn_x4, float8_e4m3fn_x4)` (`moe_tkg.py:39`) |
| **Sharding** | LNC-2 only: `kernel_assert(n_prgs == 2)` (`moe_block_tkg_utils.py:237`) |
| **Production path** | compiled `_private_kernels/{expert_mlps,router_topk,mlp}.so` |
| **HW constants** | `_pmax = 128`, `_q_width = 4` (`moe_block_tkg_utils.py:26-27`) |

---

## The Orchestrator

### Purpose

`moe_block_tkg` is the single fused decode kernel. It takes the raw `inp[B,S,H]`, the RMSNorm `gamma`, the router weights, the per-expert gate/up and down weights (+ optional MX/FP8 scales and biases), a sheaf of mode flags, and produces 1–3 HBM outputs: the `result[T,H]`, the optional `router_logits[T,E]`, and (MX-all-expert + residual only) the `residual_out[T,H]`. One plain `@nki.jit` decorates it (`:38`) — no `mode='trace'`, no `debug_kernel`, no `@force_auto_alloc`; the heavy debug decorators live on the separate `_pre_prod_kernels` staging copies, not here.

### Entry Point

```text
moe_block_tkg()                              moe_block_tkg.py:38   ── fused decode megakernel
  ├─ parse_moe_block_config()                _utils.py:106         ── B,S,H,T,E,H_free,n_prgs,is_moe_weight_mx
  ├─ validate_moe_block_inputs()             _utils.py:160         ── the hard gates (§ below)
  ├─ Stage 1: _rmsnorm_mx_quantize_tkg()  |  ../subkernels/         ── MX all-expert (fused norm+MXquant)
  │           _rmsnorm_tkg()                 ../subkernels/         ── everything else
  ├─ Stage 2: _router_topk()                 ../router_topk/        ── GEMM + act + top-K + scatter
  ├─ Stage 3: if has_shared_expert: pass     :296-298              ── DEAD (TODO + asserted-out)
  └─ Stage 4: _moe_tkg()                     ../moe/moe_tkg/moe_tkg.py:43  ── the 2×2 leaf dispatcher
```

### Algorithm

The body is a straight-line fusion. The single decision the orchestrator makes is the convenience pivot `is_mxfp_all_expert`, which splits the kernel into its two top-level code shapes; everything else is plumbing.

```c
function moe_block_tkg(inp, gamma, router_weights, gate_up_w, down_w, ..., is_all_expert=False):  // :38
    dims, quant_config, expert_config = parse_moe_block_config(...)        // :172
    validate_moe_block_inputs(...)                                        // :175  — the hard gates

    is_mxfp_all_expert = quant_config.is_moe_weight_mx and expert_config.is_all_expert  // :190 — the pivot

    // ---- Stage 1: RMSNorm, allocated ONCE into SBUF, reused by router AND experts ----
    rmsnorm_out = ndarray((128, T, H_free), inp.dtype, buffer=sbuf)        // :193 — the spine
    if is_mxfp_all_expert:
        num_H512_tiles = H // (128 * 4)                                   // :200 — 512-wide MX blocks
        rmsnorm_out_quant = ndarray((128, num_H512_tiles, T), float8_e4m3fn_x4, sbuf)  // :202
        rmsnorm_out_scale = ndarray((128, num_H512_tiles, T), uint8, sbuf)             // :203 — E8M0
        residual_out = ndarray((T,H), inp.dtype, shared_hbm) if residual else None     // :204 — only HBM spill here
        _rmsnorm_mx_quantize_tkg(inp, gamma, rmsnorm_out, rmsnorm_out_quant,
                                 rmsnorm_out_scale, residual, residual_out, ...)        // :206 — fused norm+MXquant
    else:
        rmsnorm_out = _rmsnorm_tkg(inp, gamma, rmsnorm_out, eps,
                       hidden_dim_tp = quant_config.is_moe_weight_mx,                    // :226 — H-view orientation
                       single_core_forced = (not MX and not all_expert and T > 1))       // :227 — selective T>1 owns LNC
        // ^ selective multi-token forces single-core norm: the token-sharded router that follows
        //   owns the LNC parallelism; sharding the norm too would double-shard.

    router_in = rmsnorm_out
    if rmsnorm_out.dtype != router_mm_dtype:                              // :233 — router runs at router_mm_dtype (bf16)
        router_in = ndarray((128, T, H_free), router_mm_dtype, sbuf); tensor_copy(router_in, rmsnorm_out)  // :234

    // ---- Stage 2: Router + TopK (residency decisions) ----
    router_logits = None if skip_router_logits else ndarray((T,E), inp.dtype, shared_hbm)   // :238 — OUTPUT
    skip_store_expert_index = is_all_expert and not router_pre_norm            // :241
    expert_index = ndarray(get_sbuf_tensor_shape(T,K,is_sbuf=True), uint32, sbuf)  // :242 — ALWAYS SBUF
    affinities_in_sbuf = not is_all_expert                                     // :250
    expert_affinities = ndarray(get_sbuf_tensor_shape(T,E,is_sbuf=affinities_in_sbuf), float32,
                                sbuf if affinities_in_sbuf else shared_hbm)     // :251 — sel→SBUF, all-expert→HBM
    expert_affinities_eager = ndarray((128,ceil(T/128),K), float32, sbuf) if not all_expert else None  // :257
    router_x_sb_layout = XSBLayout_tp201__2  if is_moe_weight_mx              // :263-268 — the transpose orientation
                      else XSBLayout_tp102__0 if (not all_expert and T>1)
                      else XSBLayout_tp2013__1
    router_outputs = _router_topk(x=router_in, w=router_weights, act_fn=router_act_fn, k=K,
                       router_pre_norm=router_pre_norm, norm_topk_prob=norm_topk_prob,
                       return_eager_affi = (not all_expert and is_moe_weight_mx),         // :284 — sel-MX needs [T,K]
                       use_PE_broadcast_w_bias = is_mxfp_all_expert,
                       shard_on_tokens = is_mxfp_all_expert or (not all_expert and T>1),  // :287
                       skip_store_expert_index, skip_store_router_logits=skip_router_logits)  // :270-290
    router_logits, expert_index, expert_affinities = router_outputs[0:3]       // :291
    if not all_expert and is_moe_weight_mx:
        expert_affinities_eager = router_outputs[3].reshape((T, K))            // :293

    // ---- Stage 3: Shared Expert — DEAD ----
    if expert_config.has_shared_expert:                                       // :296
        pass                                                                  // :298 — TODO; also asserted-out at validate

    // ---- Stage 4: pick the expert-MLP input (the residency hand-off), then dispatch ----
    expert_mlp_in_scale = rmsnorm_out_scale if is_mxfp_all_expert else None    // :301
    if is_mxfp_all_expert:                expert_mlp_in = rmsnorm_out_quant     // :305 — the MX-quant SBUF hidden
    elif is_moe_weight_mx or (not all_expert and T>1):
                                          expert_mlp_in = rmsnorm_out          // :308 — full [128,T,H_free] SBUF
    else:                                 expert_mlp_in = ndarray((128,T,H_free_shard), inp.dtype, sbuf)  // :311
                                          tensor_copy(expert_mlp_in, rmsnorm_out[:,:, this_core_H_shard])  // :312 — H1-slice
    result = _moe_tkg(hidden_input=expert_mlp_in, expert_gate_up_weights=gate_up_w,
              expert_down_weights=down_w, expert_affinities=expert_affinities, expert_index=expert_index,
              is_all_expert=is_all_expert, rank_id=rank_id, hidden_input_scale=expert_mlp_in_scale,
              mask_unselected_experts = router_pre_norm and not norm_topk_prob,    // :331 — THE KEYSTONE
              expert_affinities_eager = eager if not all_expert else None,
              expert_affinities_scaling_mode, activation_fn=hidden_act_fn, output_dtype=inp.dtype, clamps...)  // :316-340

    outputs = [result]
    if not skip_router_logits: outputs.append(router_logits)                   // :344
    if residual_out != None:   outputs.append(residual_out)                    // :346
    return tuple(outputs)                                                      // :349
```

### Residency Contract

The fusion's whole payoff is that the normed hidden state is produced once into SBUF (`rmsnorm_out`, `:193`) and consumed twice — by the router GEMM (`router_in`, `:271`) and again by the expert MLP (`expert_mlp_in`, `:317`) — never round-tripped to HBM between stages. The decode token's `H`-vector is read from HBM once (as `inp`), normed once, and reused in place; that is the single largest HBM saving of the megakernel.

```text
inp[B,S,H]@HBM ─►[Stage1 RMSNorm]─► rmsnorm_out[128,T,H_free]@SBUF ───────────────────┐
                                     (+MX: _quant[128,H/512,T] fp8 & _scale uint8 @SBUF)│
                                              │                                         │
   router_weights@HBM ─►[Stage2 GEMM+act+topK+scatter] ◄──────── router_in@SBUF ────────┘
                            ├─► router_logits[T,E] ───────────────► @HBM  (OUTPUT)
                            ├─► expert_index[T,K] ────────────────► @SBUF (resident)
                            ├─► expert_affinities[T,E] ───────────► @SBUF (sel) / @HBM (all-expert)
                            └─► (sel) eager_affinities[T,K] ──────► @SBUF (resident)
                                              │
   [Stage3 Shared Expert: NO-OP]              ▼
   expert_mlp_in (=rmsnorm_out / _quant / H1-slice, all @SBUF) ─►[Stage4 _moe_tkg → 2×2 leaf]
                            → gate/up matmul → act⊙ → down matmul → POST_SCALE affinity → accumulate
                            → result[T,H] @HBM  (OUTPUT)
```

| Tensor | Buffer | Source line | Why |
|---|---|---|---|
| `rmsnorm_out[128,T,H_free]` | SBUF | `:193` | the normed-hidden spine, reused by router + MLP |
| `rmsnorm_out_quant[128,H/512,T]` fp8 | SBUF | `:202` | MX-quantized hidden (MX all-expert) |
| `rmsnorm_out_scale[128,H/512,T]` uint8 | SBUF | `:203` | E8M0 block scales (MX all-expert) |
| `expert_index[T,K]` uint32 | SBUF | `:242` | top-K indices — always resident (gather / mask need it) |
| `expert_affinities[T,E]` (selective) | SBUF | `:251` | full scattered affinities, gathered by index later |
| `expert_affinities[T,E]` (all-expert) | **HBM** | `:251` | streamed back by `mask_expert_affinities` rank-slice |
| `expert_affinities_eager[T,K]` (sel) | SBUF | `:257` | pre-scatter top-K affinities for the selective-MX leaf |
| `router_logits[T,E]` | **HBM** | `:238` | requested kernel OUTPUT |
| `result[T,H]` | **HBM** | `moe_tkg.py:263` | final OUTPUT (`output_in_sbuf=False` default) |
| `residual_out[T,H]` | **HBM** | `:204` | OUTPUT, MX-all-expert + residual only |

> **QUIRK —** the all-expert path puts `expert_affinities[T,E]` in **HBM** while selective keeps it in SBUF (`affinities_in_sbuf = not is_all_expert`, `:250`). This is deliberate budget management, not an oversight: in all-expert mode `E` is the *global* expert count (large), so the full `[T,E]` would be expensive to hold resident; `moe_tkg` streams just this rank's `[T,E_L]` slice from HBM via the indirect-DMA rank-slice. Selective keeps `[T,E]` resident because it then gathers only `K` columns by index in-SBUF.

### The Hard Gates

`validate_moe_block_inputs` (`moe_block_tkg_utils.py:160`) enforces the kernel's domain. The token-size gate is asymmetric and easy to misread:

```c
function validate_moe_block_inputs(dims, quant_config, expert_config, ...):   // _utils.py:160
    kernel_assert(dims.H % 128 == 0)                                          // :195
    if expert_config.is_all_expert:
        if quant_config.is_moe_weight_mx:
            kernel_assert(dims.T % 4 == 0)        // :200 — MX all-expert: T divisible by 4 (T>128 allowed)
        // NOTE: non-MX all-expert gets NO T<=128 gate here (capped downstream in moe_tkg.py:376)
    else:
        kernel_assert(dims.T <= 128)              // :202 — selective: T <= 128
    kernel_assert(not expert_config.has_shared_expert,
                  "shared_expert has not been supported in moe_block_tkg kernel yet")   // :205
    if expert_config.is_all_expert:
        kernel_assert(rank_id != None)            // :230 — EP locality needs the rank
        if residual != None:
            kernel_assert(quant_config.is_moe_weight_mx)   // :233 — fused residual is MX-all-expert only
    kernel_assert(dims.n_prgs == 2)               // :237 — LNC-2 ONLY
    kernel_assert(dims.H % (128 * 2) == 0)        // :238 — H splits evenly across the two cores
    kernel_assert(hidden_act_scale_factor == None)  // :240 — placeholder alpha, not wired
```

> **GOTCHA —** the block-level `T <= 128` assert (`:202`) fires only in the *selective* (`else`) branch. A reimplementer who hoists it to a top-level guard will wrongly reject non-MX all-expert with `T` larger than 128 at the block level — that case is instead bounded downstream by `moe_tkg.py:376` (`T <= 128 or is_all_expert`). The only firm token gate in all-expert mode is the MX `T % 4 == 0` at `:200`.

### Decode-Specific Optimizations

- **Token-partition fold.** `get_sbuf_tensor_shape(T, free, is_sbuf=True)` returns flat `[T, free]` when `T ≤ 128` and folds to `[128, ceil(T/128), free]` when `T > 128` (`_utils.py:50-54`), so the token count never overflows the 128-partition SBUF limit.
- **H-sharding.** `H_free = H // 128`; `H_free_shard = H_free // n_prgs` (`_utils.py:132,:144`). In the non-MX no-shard path the expert-MLP input holds only this core's `H`-slice (`:311-313`), halving the resident hidden footprint per core under LNC-2.
- **MX-block tiling.** 512-wide MX blocks → `num_H512_tiles = H // (128·4)`; the quant/scale tensors are `[128, H/512, T]` (`:200-203`).
- **Fused residual add.** Honored *only* for MX all-expert (`validate :231-233`); the `_rmsnorm_mx_quantize_tkg` subkernel folds residual-add + RMSNorm + MX-quantize into one pass, and writes `residual_out` to HBM as a kernel output.

---

## The Dispatcher and the 2×2 Leaf Matrix

### Purpose

`moe_tkg` (`moe/moe_tkg/moe_tkg.py:43`) is the strategy selector that Stage 4 calls. It owns one decision — *which of four expert-MLP leaves runs* — plus the affinity-mask gate that precedes them and the entry validation that rejects the unsupported scale modes. The orchestrator makes no leaf choice; it hands `is_all_expert` (a caller bool, default `False`) and the weight dtype to this function.

### Algorithm

```c
function moe_tkg(hidden_input, gate_up_w, down_w, expert_affinities, expert_index,
                 is_all_expert, rank_id=None, mask_unselected_experts=False,
                 expert_affinities_scaling_mode=NO_SCALE, ..., is_all_expert_dynamic=False):  // :43
    quant_type, is_mx_kernel = _extract_quantization_type(gate_up_w, scales...)   // :169
    //   is_mx_kernel = (gate_up_w.dtype in (float4_e2m1fn_x4, float8_e4m3fn_x4))  // :306
    //   else STATIC if both input scales present (:309), else ROW if both weight scales (:311)

    // ---- affinity-mask gate (only all-expert + scaling reaches the mask module) ----
    if is_all_expert and expert_affinities_scaling_mode != NO_SCALE:              // :178
        kernel_assert(rank_id != None, "rank_id is required ...")                 // :179
        E_L = gate_up_w.shape[0];  T = get_T_from_hidden_input(hidden_input, hidden_input_scale);  K = expert_index.shape[-1]
        masked_expert_affinities = mask_expert_affinities(expert_affinities, expert_index, rank_id,
                                     E_L, T, K, io_dtype=expert_affinities.dtype,
                                     mask_unselected_experts = mask_unselected_experts,
                                     output_in_sbuf = not is_all_expert_dynamic)   // :188 — see "Affinity Masking"
    else:
        masked_expert_affinities = expert_affinities                              // :200 — selective gathers by index

    _validate_moe_tkg_inputs(T, is_all_expert, is_all_expert_dynamic, block_size, is_mx_kernel, ...)  // :244
    //   kernel_assert(T <= 128 or is_all_expert)                                  // :376
    //   kernel_assert(mode != PRE_SCALE_DELAYED, "only applicable in CTE ...")    // :382 — CTE-only
    //   kernel_assert(mode != PRE_SCALE,        "Kernel does not support pre-scale mode")  // :387
    //   kernel_assert(gate_up_input_scale == None and down_input_scale == None)   // :393 — static quant off

    output = ndarray(hidden_input.shape, sbuf) if output_in_sbuf else ndarray((T,H), shared_hbm)  // :260-263

    // ---- THE 2×2 DISPATCH ----                                                  // :266-277
    if is_all_expert:
        if is_mx_kernel:  _all_expert_moe_tkg_mx(mlp_params, output, is_all_expert_dynamic, block_size)  // :268
        else:             _all_expert_moe_tkg(mlp_params, output)                  // :272
    else:
        if is_mx_kernel:  _selective_expert_moe_tkg_mxfp4(mlp_params, output)      // :275
        else:             _selective_expert_moe_tkg(mlp_params, output)            // :277
    return output
```

### The Leaf Matrix

The two axes are orthogonal and resolved at different times. **Level 1** (all-expert vs. selective) is the caller's `is_all_expert` bool — `moe_block_tkg` defaults it `False`; there is *no* runtime `num_experts` threshold in this readable code, so the strategy is chosen by the framework's kernel-selection policy upstream. **Level 2** (MX vs. bf16/fp8) is the deterministic runtime dtype check `is_mx_kernel` (`:306`); FP8 is handled *inside* the bf16 family by a weight `.view()` plus threaded scales, not as a separate leaf. All four cells are wired — there is no `else/raise`.

| `is_all_expert` | `is_mx_kernel` | Leaf (`moe_tkg.py`) | Iteration | LNC shard axis | Affinity application |
|---|---|---|---|---|---|
| **True** | False (bf16/fp8) | `_all_expert_moe_tkg` `:272` | `for e in range(E_L)` (dense, masked) | H1 (hidden-free) | broadcast masked column × down, then accumulate |
| **True** | True (MX) | `_all_expert_moe_tkg_mx` `:268` | `for e in SEQUENTIAL_RANGE(E_L)` | **I** (intermediate); sendrecv+add reduce | fused in `down_projection_mx` epilogue |
| **False** | False (bf16/fp8) | `_selective_expert_moe_tkg` `:277` | `for t in T: for k in K` (gather `expert_index[t,k]`) | H1 | gather affinity by index, × down |
| **False** | True (MXFP4) | `_selective_expert_moe_tkg_mxfp4` `:275` | `for t: for k` (gather) | K (selected experts) | `scalar_tensor_tensor` fused multiply-add |

The shared skeleton is identical: every leaf computes `out[t] = Σ_e A[t,e] · downproj(act(gate(x_t)) ⊙ up(x_t))` over its experts, weighting by the (masked or gathered) affinity `A` after the down projection. The deltas are exactly the four table columns: *what is iterated* (every local expert vs. each token's K routed experts), *which axis the two NeuronCores split*, and *how the per-expert affinity reaches the multiply*. All-expert is **dense compute, sparse result** — every token is run through every local expert, and the masked affinity (`0` for non-routed experts) collapses the sum to the K routed contributions. Selective is **sparse compute** — only the K routed experts are touched, the gather *is* the selection.

> **NOTE —** `_all_expert_moe_tkg_mx` declares a static-block / dynamic-while (DLoC, "Dynamic Loop on Chip") scaffold (`is_all_expert_dynamic`, `block_size`), but `moe_block_tkg` never sets either argument, so both default `False`/`None` and the path is **static-only**. The orchestrator does not even expose the dynamic-while knob. (The dispatcher *would* accept it: `_validate_moe_tkg_inputs` checks `is_all_expert_dynamic ⇒ is_all_expert ∧ block_size != None` at `:358-366`.)

### The Scale-Mode Validators

`ExpertAffinityScaleMode` is a 4-member enum (`utils/common_types.py`): `NO_SCALE=0`, `POST_SCALE=1`, `PRE_SCALE=2`, `PRE_SCALE_DELAYED=3`. In the decode path only `{NO_SCALE, POST_SCALE}` are live. `PRE_SCALE` is rejected at the entry (`:387`, "Kernel does not support pre-scale mode") and `PRE_SCALE_DELAYED` is rejected as CTE-only (`:382`). The decode default is `NO_SCALE` (`:60`), so — unlike the CTE MX kernel whose static entry *defaults* to `PRE_SCALE` and asserts deep — TKG fails fast at the entry with no latent default-vs-assert footgun.

---

## Affinity Masking

### Purpose

`mask_expert_affinities` (`moe_tkg_affinity_masking.py:31`) turns the global per-token-per-expert affinity matrix `expert_affinities[T,E]` into a per-*local*-expert masked tensor `[T,E_L]`, zeroing non-selected experts. It is reached only when `is_all_expert ∧ scaling_mode ≠ NO_SCALE` (`moe_tkg.py:178`); selective mode never calls it (it gathers affinities by index instead, so the mask is implicit in the gather).

### Algorithm

The module is two steps: a rank-slice that is itself the first level of masking (experts outside this rank's range are never loaded), and an optional index-mask that zeros experts a token did not route to.

```c
function mask_expert_affinities(expert_affinities[T,E], expert_index[T,K], rank_id, E_L, T, K,
                                io_dtype, mask_unselected_experts, output_in_sbuf):   // :31
    kernel_assert(output_in_sbuf or not mask_unselected_experts)   // :67 — can't index-mask an HBM-only slice
    rank_id_sbuf = dma_copy(rank_id)                               // :74
    expert_offset_sbuf = tensor_scalar(rank_id_sbuf * E_L)         // :78  — offset = rank_id · E_L

    // ---- STEP A: rank-slice global [T,E] → local [T,E_L] via INDIRECT DMA ----
    if output_in_sbuf:                                             // :87 → _load_slice_affinities_sbuf
        if T <= 128:                                              // :124 — 2D [T,E_L], one DMA
            dst[T,E_L] = dma_copy( expert_affinities.ap(pattern=[[E,T],[1,E_L]], offset=0,
                                     scalar_offset=expert_offset_sbuf, indirect_dim=1),
                                   dge_mode = unknown if T % 16 == 0 else swdge )   // :127-136
        else:                                                    // :137 — 3D tiled [128, n_T128_tiles, E_L]
            // n_full_tiles full-128 DMAs + one partial-tile DMA for T % 128
    else:                                                         // :97 → _slice_affinities_hbm
        T_shard = T // 2; T_offset = T_shard · prg_id            // :181 — LNC-sharded on T
        dma_copy(...) ; core_barrier((0,1))                       // :187,:197 — used for is_all_expert_dynamic

    // ---- STEP B: index-mask (zero non-selected experts) — only if mask_unselected_experts ----
    if mask_unselected_experts:  _apply_expert_index_mask(...)     // :106
```

`_apply_expert_index_mask` (`:202`) is the arithmetic heart. It walks the `E_L` local experts, and for each builds a per-token 0/1 indicator of "was this expert in the token's top-K" and multiplies the affinity column by it:

```c
function _apply_expert_index_mask(expert_affinities_masked[T,E_L], expert_index[T,K],
                                  expert_offset_sbuf, E_L, T, K, io_dtype):       // :202
    T_32s = 32 * ceil(T / 32)                                                    // :230 — partition-align
    expert_index_sbuf = (dma_copy(expert_index) if in HBM else expert_index)     // :233
    expert_offset_broadcast[T_32s,1] = stream_shuffle_broadcast(expert_offset_sbuf)  // :241
    expert_offset_f[T_32s,K] = broadcast expert_offset to all K columns, fp32    // :243-256
                              // alternating vector/scalar engine per k_idx parity

    for expert_idx in affine_range(E_L):                                         // :259  — checks E_L experts in turn
        expert_check[T,K] = tensor_tensor(expert_index_sbuf == expert_offset_f, op=nl.equal)   // :262
        expert_match[T,1] = tensor_reduce(expert_check, op=nl.add, axis=1)        // :271  — 1 if expert in top-K else 0
        expert_affinities_masked[:, expert_idx] *= expert_match                   // :279  — tensor_tensor nl.multiply
        expert_offset_f += 1                                                      // :287  — advance to next local expert id
```

So for each local expert `e = rank·E_L + expert_idx`, the token's affinity column is kept iff `e` appears in that token's `expert_index[t,:K]`, else zeroed — exactly the sparse mask that makes all-expert dense-compute / sparse-result. The check is `equal(expert_index, expert_offset_f)` reduced over `K`, where `expert_offset_f` starts at the rank's first local expert id (`rank·E_L`) and increments by 1 each loop iteration (`:287`), so it sweeps the absolute expert ids `[rank·E_L, rank·E_L + E_L)`.

### The Keystone

The orchestrator's single decision about *who* masks the affinities is one line (`moe_block_tkg.py:331`), passed straight into `_moe_tkg` as `mask_unselected_experts`:

```c
// Only mask when router_topk doesn't perform masking (router_pre_norm=True, norm_topk_prob=False).   // :329
// Otherwise, expert_affinities are already masked by router_topk's scatter operation.                // :330
mask_unselected_experts = router_pre_norm and not norm_topk_prob                                       // :331
```

Two booleans, both plumbed from the signature into both the router (Stage 2) and this mask decision (Stage 4). `router_pre_norm` (default `True`) selects ACT1 (activation *before* top-K, leaving all `E` affinities populated) vs. ACT2 (top-K on raw logits, activation on the K only). `norm_topk_prob` (default `False`) L1-normalizes the top-K affinities. The truth table:

| `router_pre_norm` | `norm_topk_prob` | Who masks the non-top-K affinities? |
|---|---|---|
| `True` | `False` | **`mask_unselected_experts=True`** → the expert-MLP re-masks via `_apply_expert_index_mask`. ACT1 left all `E` affinities populated; the scatter wrote the full softmax/sigmoid, no zeroing. |
| `True` | `True` | `False` → RouterTopK's normalize+scatter already produced a top-K-only renormalized vector. |
| `False` | any | `False` → ACT2 path; only the K experts get an activation, scatter writes only those columns. |

> **QUIRK —** `mask_unselected_experts` is computed unconditionally at `:331` but only *bites* in all-expert mode — the mask gate inside `moe_tkg` requires `is_all_expert` (`:178`), and selective never calls `mask_expert_affinities` at all. In selective mode the flag's value is computed and ignored. The `router_pre_norm ∧ ¬norm_topk_prob` corner is the *only* combination where the router's scatter leaves a dense affinity row, so it is the only case where the all-expert expert-MLP must re-do the top-K masking itself.

The torch ground truth mirrors the semantics (`moe_block_tkg_torch.py:107-111`): `expert_offset = rid·E_L`; slice affinities to `[:, expert_offset:expert_offset+E_L]`; then `if router_pre_norm: for e in range(E_L): mask = (expert_index == expert_offset + e).any(dim=1); affinity[:,e] *= mask`. The reference maps `mask_unselected_experts → router_pre_norm` (it handles `norm_topk_prob` in the router ref separately), confirming both the rank-slice offset and the per-expert `equal`-`any`-`multiply` mask.

---

## The MX Projections

### Purpose

The MX leaves reach two fused projection sub-kernels — `gate_up_projection_mx.py` (`gate_up_projection_mx_shard_I`) and `down_projection_mx.py` (`down_projection_mx`) — that implement the SwiGLU expert FFN over MX-quantized weights and activations. Both run on `nisa.nc_matmul_mx`, the five-operand PE-array matmul `{dst, stationary, moving, stationary_scale, moving_scale}` where the E8M0 (uint8) per-32-element block scales ride *inside* the matmul — there is no post-matmul dequant multiply. The gate*up SwiGLU result is itself online-`quantize_mx`'d to FP8 so the down matmul gets ready MX activations.

### The Gate/Up Projection and SwiGLU

The SwiGLU formula is `out = act(clamp(gate_proj(hidden))) · clamp(up_proj(hidden))` — clamp on *both* gate and up before the multiply, activation on the gate branch only. The sequence is verified verbatim:

```c
function fused gate_up (gate_up_projection_mx.py):
    // Step 3 — GATE branch
    out_psum = _matmul_mx_accumulate(out_psum, gate_weight_sb, gate_weight_scale_sb,
                                     input_quant_sb, input_scale_sb, ...)        // :161-178 calls matmul helper
    out_sb   = tensor_tensor(out_psum + gate_bias_sb) | tensor_copy             // :184-198 — bias during PSUM evict
    _clamp_tensor(out_sb, gate_clamp_upper_limit, gate_clamp_lower_limit)        // :201
    if hidden_act_fn != None:
        nisa.activation(out_sb, op=get_nl_act_fn_from_type(hidden_act_fn))       // :209 — GATE only

    // Step 4 — UP branch + SwiGLU + online QMX
    out_psum = _matmul_mx_accumulate(out_psum, up_weight_sb, up_weight_scale_sb, input_quant_sb, input_scale_sb, ...)  // :230
    intermediate_tile_sb = tensor_tensor(out_psum + up_bias_sb) | tensor_copy    // :251-264
    _clamp_tensor(intermediate_tile_sb, up_clamp_upper_limit, up_clamp_lower_limit)  // :267
    out_sb = tensor_tensor(out_sb, op=nl.multiply, intermediate_tile_sb)         // :274 — SwiGLU: act(clamp(gate)) · clamp(up)
    quantize_mx(src=out_sb, dst=out_quant_sb[fp8_e4m3_x4], dst_scale=out_scale_sb[uint8])  // :285 — online → FP8 for down
```

The two helpers the SwiGLU rests on:

```c
function _clamp_tensor(tensor, upper, lower):                                     // :427
    if upper != None or lower != None:
        nisa.tensor_scalar(dst=tensor, data=tensor,
            op0 = nl.minimum if upper != None else None, operand0 = upper,        // :444-445
            op1 = nl.maximum if lower != None else None, operand1 = lower)        // :446-447
    // ONE fused tensor_scalar does both bounds = clamp(x, lower, upper); either bound may be None.

function _matmul_mx_accumulate(out_psum, weight_sb, weight_scale_sb, input_quant_sb, input_scale_sb, ...):  // :452
    for q_width_I_idx in sequential_range(_q_width=4):                            // :481 — the 4 packed-I sub-tiles
        weight_I_offset = tile_i*MAX_MATMULT_MX_UNPACKED_CONTRACT_DIM + q_width_I_idx*cur_I128_tile_sz   // :482
        for tile_h in sequential_range(n_H512_tiles):
            nisa.nc_matmul_mx(dst=out_psum[:cur_I128_tile_sz, q_width_I_idx, :tile_T_actual],
                stationary       = weight_sb[:, tile_h, weight_I_slice],          // :487 — W stationary
                moving           = input_quant_sb[:, tile_h, tile_T_slice],       // :488 — hidden moving
                stationary_scale = weight_scale_sb[:, tile_h, weight_I_slice],    // :489 — E8M0 weight scale
                moving_scale     = input_scale_sb[:, tile_h, tile_T_slice])       // :490 — E8M0 input scale
```

> **NOTE —** `_clamp_tensor` and `_matmul_mx_accumulate` are the refactored extractions in the `nkilib/core/moe/moe_tkg/` tree. The parallel `_pre_prod_kernels/mlp_tkg/*_shard_I.py` copy inlines the same logic with a self-documented "move duplicate matmul and bias add logic into sub-functions" TODO; the two are byte-equivalent in algorithm. The "2" axis of `gate_up_weights[E,128,2,H/512,I]` is the gate/up selector — `GATE_FUSED_IDX=0`, `UP_FUSED_IDX=1`; gate and up are loaded as separate SBUF tiles and matmul'd separately, so the "fused" in the name means fused *with* act/clamp/QMX into one sub-kernel, not a single matmul.

What the kernel ships is the clamp *mechanism*, not a clamp *policy*. The gate and up `clamp_upper_limit` / `clamp_lower_limit` parameters default to `None` at every level — orchestrator `:65-68`, `moe_tkg.py:63-66`, and the projection signatures — and a tree-wide search finds no numeric clamp default anywhere. Any model-specific bound (a gpt-oss-style SwiGLU limit, say) arrives as a caller or NxD-config float.

> **GOTCHA — there is no baked-in SwiGLU clamp constant to recover.** Every clamp limit defaults to `None`, and `_clamp_tensor` degenerates to a no-op when both bounds are absent, so the numeric bound a given model uses is not extractable from the kernel source.

### The Down Projection, POST_SCALE, and the I-Shard Reduce

The down projection swaps operands relative to gate/up — the activation (intermediate) is *stationary* and the weight is *moving*, each still carrying its own E8M0 scale. Its epilogue applies the POST_SCALE affinity and accumulates, then (last expert) reduces the I-shard partials across the two cores.

```c
function down_projection_mx (down_projection_mx.py):
    kernel_assert(expert_affinities_scaling_mode == POST_SCALE)                   // :302 — only POST_SCALE supported
    out_psum = nc_matmul_mx(stationary=act_sb, moving=weight_sb, act_scale, weight_scale)  // down matmul (operand swap)
    expert_out_tile_sb = tensor_tensor(out_psum + bias_sb) | tensor_copy          // :358-369

    // affinity scale + expert-add (affinity = the masked [T,E_L] from mask_expert_affinities)
    if is_first_expert or is_blockwise:
        out_sb = tensor_scalar(expert_out * affinity, engine=scalar)              // :373 — direct write
    else:
        expert_out = tensor_scalar(expert_out * affinity, engine=scalar)          // :382
        out_sb     = tensor_tensor(out_sb + expert_out, op=nl.add)                // :390 — accumulate

    if is_last_expert and n_prgs > 1:                                            // :399-401 — LNC-2 I-shard reduce
        H_local = H // 2;  send/recv = 1 - prg_id                                 // :402-404
        sendrecv(swap the other core's H-half) ; tensor_tensor(add) ; dma_copy(out_hbm[T, H_half])   // :427+
```

Each NeuronCore computed `I//2` of the contraction (the gate/up weights are I-sliced, `I_local = I//2`), so the two cores hold partial `[T,H]` sums that must add. The `sendrecv` swaps each core's complementary `H`-half and a `tensor_tensor(add)` reduces, then the result spills to `out_hbm`. This is the decode delta from prefill/CTE, which shards the *contraction* `H` axis instead: same `nc_matmul_mx` contract, same E8M0-in-matmul, same SwiGLU, same `quantize_mx` — the entire difference is *which axis the two cores split* (intermediate `I` for decode, contraction `H` for CTE) plus block-of-tokens vs. few-decode-tokens tiling.

> **NOTE —** the projection signatures default `hidden_act_fn = ActFnType.Swish`, but the `moe_block_tkg` orchestrator passes `hidden_act_fn = SiLU` (`:62`) through to the leaf, and the entry default wins for the production call. SiLU and Swish are mathematically the same `x·σ(x)`, but the activation map routes them to *different* `nl` ops (`SiLU → nl.silu`, `Swish → nl.gelu_apprx_sigmoid`), a real implementation distinction.

---

## Audit and Smells

- **Shared Expert is a dead façade.** The signature carries six shared-expert params and the docstring pseudocode shows `shared_out = shared_expert_mlp(rmsnorm_out)`, but Stage 3 is literally `if has_shared_expert: pass` (`:296-298`) and `validate_moe_block_inputs` hard-asserts `not has_shared_expert` (`:205`). A non-`None` `shared_expert_gate_w` fails at validation — the path cannot run. The torch reference omits it too.
- **`skip_router_logits` is a flag the router rejects.** It is threaded into `_router_topk` (`:289`) but the router asserts `skip_store_router_logits == False` ("not currently supported due to a compiler limitation"). Setting `skip_router_logits=True` would fail inside the router. Latent; only bites if a caller sets it.
- **`hidden_act_scale_factor` / `hidden_act_bias`** are placeholder alpha/beta for the activation, asserted `== None` (`_utils.py:240`); the mechanism is not wired.
- **Selective-MX applies affinity unconditionally** (no `if POST_SCALE` guard), relying on the entry asserting POST_SCALE — a guard asymmetry versus the guarded bf16 selective leaf. Benign given the dispatch, but a latent smell.
- **No SbufManager threading at the megakernel level** — the top-level SBUF tensors use `@nki.jit` auto-allocation; the fine-grained budgeting (expert-weight double-buffering, no-overlap down-weight prefetch) lives inside the leaves.
- **Two parallel MX-projection implementations** — the inlined `_pre_prod_kernels/mlp_tkg/*_shard_I.py` twin and the refactored `nkilib/core/moe/moe_tkg/*` twin with helpers — a duplicate-code smell; the shipped leaf imports one, the refactor is the cleaner next-gen path.

---

## Related Components

| Name | Relationship |
|---|---|
| [MoE Context/Prefill (CTE)](moe-cte-prefill.md) | the prefill sibling: blockwise dense GEMMs, separate stages, contraction-axis shard; this page is the fused decode path |
| MoE Router (RouterTopK) *(planned)* | Stage 2: the GEMM + activation + top-K + scatter that produces `expert_index`/`expert_affinities`; owns the ACT1/ACT2 pre-norm split the keystone reads |
| RMSNorm (decode) *(planned)* | Stage 1: `_rmsnorm_tkg` / `_rmsnorm_mx_quantize_tkg`, the SBUF-resident normed hidden |
| MX numerics *(planned, appendix)* | the E8M0 / x4-packing / `quantize_mx` block-scale scheme the projections rest on |
| [nkilib Infrastructure](nkilib-infrastructure.md) | the BufferManager / ModularAllocator / TiledRange primitives the leaves build on |

## Cross-References

- [MoE Context/Prefill (CTE)](moe-cte-prefill.md) — the prefill MoE FFN; same dispatch shape (`is_all_expert × is_mx`), different token model and shard axis
- [nkilib Infrastructure](nkilib-infrastructure.md) — the allocator / tiling layer underneath every leaf
- [ISA Compute Intrinsics](isa-compute-intrinsics.md) — `nc_matmul_mx`, `quantize_mx`, `tensor_scalar`, `tensor_tensor` operand contracts
- [SPMD Programming Model](spmd-programming-model.md) — the LNC-2 program grid (`n_prgs == 2`, `prg_id`) the sharding rides on
