# Dense (non-MoE) MLP

> *All symbols, line numbers, and enum values on this page apply to `neuronx_cc` **2.24.5133.0+58f8de22**, the `cp310` wheel. The `cp310`/`cp311`/`cp312` copies of every dense-MLP file are byte-identical (`diff -q` over `mlp/` and `mlp_tkg/`), so `cp310` is the source of truth. Other releases will differ — see the version-mismatch correction in the Abstract.*

## Abstract

The dense MLP is the plain feed-forward block of a transformer layer: one gate projection, one up projection, a SwiGLU non-linearity, and one down projection, optionally preceded by a fused residual add and a fused RMSNorm/LayerNorm. There is **no expert dimension, no router, no token gather/scatter, and no affinity scaling** — the same weight triple `(gate_w[H,I], up_w[H,I], down_w[I,H])` runs over every token. It is the non-expert specialization of the MoE expert-MLP kernel ([MoE CTE](moe-cte-prefill.md), [MoE Decode](moe-decode-tkg.md)): structurally the same `gate/up → act → down` skeleton, minus all the expert plumbing.

The public entry is `fused_mlp_isa_kernel` in `_pre_prod_kernels/mlp/mlp.py`. It builds an immutable `MLPParameters` tuple, then dispatches on a single scalar threshold: `is_mlp_tkg(B, S)` returns `True` when `B·S ≤ 96`, routing decode-shaped work to the **TKG** path (`_pre_prod_kernels/mlp_tkg/mlp_tkg_isa.py`) and everything larger to the **CTE** path (`_pre_prod_kernels/mlp/mlp_cte.py`). The two paths share the parameter model, the activation-function map, and the SwiGLU algebra, but differ completely in tiling, sharding axis, and matmul operand orientation. CTE tiles the `B·S` rows and shards on the **intermediate** axis `I` (or on `B·S`); TKG keeps a tiny `T = B·S` and shards on the **hidden** axis `H` by default, falling back to `I` only when `I > 2048`. That single difference drives the cross-core asymmetry that is the centerpiece of this page: under H-sharding gate/up **reduce** (H is their contraction) while down **gathers** (H is its output).

> **CORRECTION (DENSE-MLP-1) —** the D-O18 backing report describes a *different, later* refactor (`nkilib/core/mlp/` with `mlp_cte_utils.py`, `mlp_tkg_mx.py`, `gate_up_projection_mx_shard_H.py`, `projection_mx_constants.py`, and a six-member `QuantizationType` including `MX`/`STATIC_MX`/`ROW_MX`). **None of that exists in 2.24.5133.0.** This wheel ships the dense MLP under `_pre_prod_kernels/mlp/` (17 files) + `_pre_prod_kernels/mlp_tkg/` (16 files); `QuantizationType` (`common_types.py:41-44`) has exactly `NONE=0, STATIC=1, ROW=2`; `build_mlp_quant_params` (`mlp_parameters.py:51-82`) `raise`s an `AssertionError` on anything else; and `rg -ni mx` over `mlp/` returns **zero hits**. The MX (microscaling / per-block-E8M0) primitives the report calls `*_mx_shard_H` are, in this build, the MoE-only `gate_up_projection_mx_shard_I.py` / `down_projection_mx_shard_I.py`, reached **solely** through `expert_mlp_tkg_all_expert_mx_impl.py`. Every claim below is re-grounded against the files that actually ship.

For reimplementation, the contract is:

- The `MLPParameters` model and the `B·S ≤ 96` CTE/TKG dispatch.
- The forward skeleton: `hidden += residual` (pre-norm) → norm → `gate = h@Wg; up = h@Wu; inter = act(gate)·up; out = inter@Wd`, with gate/up weights as **separate** 2-D tensors.
- The CTE 10-step per-tile pipeline and the TKG column-tiling-vs-`lhs_rhs_swap` operand orientations.
- The H-shard reduce-vs-gather asymmetry (gate/up add-reduce; down disjoint-half gather) and its I-shard mirror image.
- The FP8 quant story: `STATIC` (per-tensor, double-row matmul) and `ROW` (per-row dynamic absmax) — and the absence of any MX path.

| | |
|---|---|
| **Public entry** | `fused_mlp_isa_kernel` — `mlp/mlp.py:30` (`@nki.jit`) |
| **Dispatch predicate** | `is_mlp_tkg(B,S)` — `mlp/mlp_helpers.py:17` → `B·S ≤ TKG_BS_SEQLEN_THRESHOLD` (`=96`, `mlp_helpers.py:8`) |
| **CTE entry** | `mlp_cte_invoke_kernel` — `mlp/mlp_cte.py:424`; per-core `mlp_cte_single_core_kernel:57` |
| **TKG entry** | `nki_mlp_tkg_isa_kernel` — `mlp_tkg/mlp_tkg_isa.py:169` (via `mlp_tkg.py:12` → `mlp_tkg_isa.py`) |
| **Param model** | `MLPParameters` (NamedTuple) — `mlp/mlp_parameters.py:170`; builder `build_mlp_params:269` |
| **Quant types** | `QuantizationType {NONE=0, STATIC=1, ROW=2}` — the **truncated** `_pre_prod_kernels/common_types.py:41` (md5 `9721a6bb…`); **no MX**. The full 6-member enum lives in the separate `nkilib/core/utils/common_types.py:56`, which this dense path does **not** import |
| **Act-fn map** | `_act_fn_map` — `util/kernel_helpers.py:18`; default `SiLU` |
| **IR level** | NKI Python trace → penguin.ir → BIR ([3-layer stack](architecture-overview.md)) |

---

## The Parameter Model and Dispatch

### Purpose

`fused_mlp_isa_kernel` is a thin façade. It validates and packs every argument into an `MLPParameters` NamedTuple, then chooses CTE or TKG. The packing is where the *shape* of the kernel is decided: which projections exist, whether quantization is on, whether a residual is folded, and the derived `(B, S, H, I)` dimensions.

### Entry Point

```text
fused_mlp_isa_kernel (mlp.py:30)        ── @nki.jit public entry
  └─ build_mlp_params (mlp_parameters.py:269)     ── pack MLPParameters
  └─ validate_mlp_arguments_fused_mlp_isa (:458)  ── restriction asserts
  └─ is_mlp_tkg(B, S) (mlp_helpers.py:17)
       ├─ True  → mlp_tkg_invoke_kernel (mlp_tkg.py:12) → nki_mlp_tkg_isa_kernel (mlp_tkg_isa.py:169)
       └─ False → mlp_cte_invoke_kernel (mlp_cte.py:424)   [allocates out HBM tensors here]
```

There are three deprecated sibling entries (`mlp_isa_kernel:233`, `mlp_fused_add_isa_kernel:315`, `quant_mlp_isa_kernel:402`), each emitting `warnings.warn("Use 'fused_mlp_isa_kernel' instead")` and funneling into the same `build_mlp_params` → dispatch. A fourth route exists inside `fused_mlp_isa_kernel`: `use_bir_tkg_kernel=True` (`mlp.py:159`) forwards the TKG case to the BIR kernel `_private_kernels.mlp.fused_mlp_isa_kernel` instead of the NKI path.

### Algorithm

```c
function fused_mlp_isa_kernel(hidden, gate_w, up_w, down_w, ...):   // mlp.py:30
    gate_proj_w = skip_gate ? None : gate_w                          // :121
    params = build_mlp_params(hidden, gate_proj_w, up_w, down_w, ...)// :123
    validate_mlp_arguments_fused_mlp_isa(params)                     // :156

    if is_mlp_tkg(params.batch_size, params.sequence_len):           // :158  B*S <= 96
        return mlp_tkg_invoke_kernel(params)                         // :160  (NKI TKG)
    else:
        out = nl.ndarray((B, S, H), dtype, buffer=nl.shared_hbm)     // :207  HBM out
        if mlpp_store_fused_add(params):                             // :217
            add_out = nl.ndarray((B, S, H), ...)                     // spill (hidden+residual)
        mlp_cte_invoke_kernel(params, out, add_out)                  // :225
        return output_tensors
```

`build_mlp_params` (`mlp_parameters.py:269`) derives `(B,S,H) = hidden.shape` unless `B/S/H` are passed explicitly (the SBUF-input case, `:315-319`); `I` comes from `up_w.shape[1]` (`:321`). Two layout adjustments matter:

- **Quantized weights are re-viewed as FP8.** When `quantization_type != NONE`, `gate_w/up_w/down_w` are cast `tensor.view(nl.float8_e4m3)` (`:328-333`).
- **A row-quantized FP8 *input* carries its scale inline.** For `ROW` + FP8 hidden, `hidden_size -= 4` (`:323-325`): the per-row fp32 dequant scale is the trailing fp32 element of each hidden row (loaded later by `load_hidden_scales`).

### Related Knobs

| Knob (arg) | Type | Default | Description |
|---|---|---|---|
| `quantization_type` | `QuantizationType` | **`NONE`** | `NONE`/`STATIC`/`ROW` only; FP8 e4m3 weights |
| `norm_type` | `NormType` | **`RMS_NORM`** | `NO_NORM=0`/`RMS_NORM=1`/`LAYER_NORM=2`/`RMS_NORM_SKIP_GAMMA=3` (`common_types.py:10`) |
| `act_fn` | `ActFnType` | **`SiLU`** | `SiLU=0`/`GELU=1`/`GELU_Tanh_Approx=2`/`Swish=3` (`common_types.py:17`) |
| `attn_output` | tensor? | **`None`** | residual; added to `hidden` **before** norm (pre-norm fold) |
| `store_add` | bool | **`False`** | spill `(hidden+residual)` to a 2nd HBM tensor |
| `skip_gate` | bool | **`False`** | drop gate proj; activation then applies to `up` |
| `lower_bound` | float | **`0.0`** | symmetric clip `[-b,+b]` before intermediate FP8 quant |
| `gate_up_lhs_rhs_swap` | bool | **`False`** | TKG-only: swap gate/up matmul operands → `[I,T]` output |
| `lhs_rhs_swap` | bool | **`False`** | TKG-only: swap **down** matmul operands → `[H,T]` output |
| `output_in_sbuf` | bool | **`False`** | TKG-only: return SBUF tile (skip HBM store) for fusion |

> **GOTCHA —** projection bias and quantization are mutually exclusive: `_validate_mlp_arguments_restrictions_fused_mlp_isa` (`mlp_parameters.py:414-416`) asserts *"Projection bias is not supported when quantization is needed."* Likewise TKG forbids any projection bias and any LayerNorm bias (`:434-435`). A reimplementation that wires a quantized + biased path will be rejected at trace time, not silently mishandled.

> **NOTE —** the residual is **pre-norm**: `attn_output` is added to `hidden` *before* normalization (`hidden = hidden + attn_out; norm(hidden)`), not after the MLP. The MLP's own output is `down_proj_out` with no further residual — the post-MLP residual is the framework's job. This is the "fused-add" the docstring (`mlp.py:65-117`) refers to.

---

## The Forward Skeleton and SwiGLU Algebra

### Purpose

Both paths compute the identical math. The SwiGLU is `out = act(gate) · up`, where the activation is applied to the **gate** branch only; with `skip_gate=True` there is no gate tensor and the activation applies to `up`. The activation callable is resolved from the enum exactly once.

### Algorithm

```c
// _act_fn_map (util/kernel_helpers.py:18-23)
SiLU            -> nl.silu
GELU            -> nl.gelu
GELU_Tanh_Approx-> nl.gelu_apprx_tanh
Swish           -> nl.gelu_apprx_sigmoid      // NOT nl.silu — sigmoid-approx gelu

function mlp_forward(hidden, Wg, Wu, Wd):
    if fused_add: hidden = hidden + attn_out          // pre-norm residual
    hidden = norm(hidden)                             // RMS / Layer / none
    gate = hidden @ Wg                                // [.,I]
    act_gate = act_fn(gate)                           // gate branch only
    up   = hidden @ Wu                                // [.,I]
    inter = act_gate * up                             // SwiGLU  (tensor_tensor multiply)
    out  = inter @ Wd                                 // [.,H]
    return out
```

> **QUIRK —** `Swish` does **not** map to `nl.silu`. `_act_fn_map` sends `Swish → nl.gelu_apprx_sigmoid` and `SiLU → nl.silu` (`kernel_helpers.py:19,22`). The two are distinct intrinsics; a reimplementation that treats "swish" and "silu" as synonyms will emit the wrong activation. `RMS_NORM_SKIP_GAMMA` is similarly a real, separate mode — `normalization_uses_weights` (`kernel_helpers.py:65`) returns `True` only for `RMS_NORM`/`LAYER_NORM`, so skip-gamma carries no γ tensor.

---

## CTE (Prefill) — `mlp_cte.py`

### Purpose

CTE handles `B·S > 96`. It tiles the `B·S` rows into 128-partition subtiles and streams each `(batch, bxs_tile)` through a fixed 10-stage pipeline. Sharding (when launched SPMD) is along the **intermediate** dim `I` or along `B·S`; both differ from TKG's H-shard.

### Entry Point

```text
mlp_cte_invoke_kernel (mlp_cte.py:424)
  ├─ if SPMD: calculate_sharding (mlp_sharding.py:136) → loop shards → mlp_cte_single_core_kernel:443
  └─ else:    DimShard(0, B*S, params), ShardedDim.BATCH_X_SEQUENCE_LENGTH → mlp_cte_single_core_kernel:467
        └─ mlp_cte_single_core_kernel (mlp_cte.py:57)
             for batch_idx in range(batch_range):              // :190
               for bxs_tile_idx in range(bxs_dim_tile.tile_count):  // :192
```

### Algorithm — the per-`(batch, bxs_tile)` pipeline

Biases, norm γ/β, and quant scales are loaded **once above the tile loop** (`mlp_cte.py:76-178`). Inside the loop, the ordered stages are:

```c
function cte_pipeline(batch_idx, bxs_tile_idx):           // mlp_cte.py:190-416
  1.  load_hidden_tensor_tile_opt_fused_add(...)          // :231  DMA hidden (+residual, opt. spill)
  2.  apply_normalization_if_necessary(...)               // :243  RMS / Layer (mean/rsqrt only)
      with allocation_scope():                            // :248
  3.    transpose_source_tensor_tile(...)                 // :265  [BxS,H]->[H,BxS]; FOLD gamma/beta here
  4.  perform_gate_projection_if_necessary(...)           // :280  matmul + bias + act(gate)
  5.  perform_up_projection(...)                          // :295  matmul + bias + (act_gate * up)
  if mlpp_has_quantized_weights:                          // :310
  6.    perform_intermediate_quantization(...)            // :334  inter -> FP8 (STATIC or ROW)
  7.  transpose_intermediate_tensor_tile(...)             // :368  [BxS,I]->[I,BxS]
  8.  perform_down_projection(...)                        // :394  matmul + bias  (I = contraction)
  9.  if sharded_dim == INTERMEDIATE: store_half_hidden_tensor_tile(...)  // :412
      else:                          store_hidden_tensor_tile(...)        // :416
```

> **CORRECTION (DENSE-MLP-2) —** D-O18 §2 cites `SbufManager`, a `top_level_interleave_degree` knob, and a `STATIC_MX` inlined matmul for CTE. In 2.24.5133.0 **none of these exist**: `rg SbufManager` and `rg top_level_interleave_degree` over `mlp_cte.py` return zero hits (SBUF is allocated through `constants.alloc_info.*_alloc.get_allocator()` + `nki.compiler.allocation_scope()`), and there is no MX matmul anywhere in `mlp_cte_projection.py`. The only special matmul mode is `perf_mode='double_row_gen3'` for FP8 (`mlp_cte_projection.py:266,617`).

### Algorithm — gate/up and down matmul orientation

```c
// GATE/UP  (project_source_tensor_tile, mlp_cte_projection.py:42; quant variant :183)
//   H is the contraction. 128x128 hidden subtiles STATIONARY; 128x512 weight tiles MOVING.
psum[bank, p, f] += nc_matmul(                                  // :169-177
    stationary = st_tile,                  // transposed hidden  [128, BxS]
    moving     = weights_sbuf[...])         // weight slice       [128, 512]
// quantized: nc_matmul(stationary, moving, perf_mode='double_row_gen3')   // :265-267
// activation: nisa.activation(op=act_fn, data=gate_psum, bias=zero)        // mlp_cte_activation.py:89
// SwiGLU:     nisa.tensor_tensor(data1=gate_sbuf, data2=up_psum, op=multiply) // mlp_cte_mult.py:69

// DOWN  (perform_down_projection, mlp_cte_projection.py:275; quant :472)
//   I is the contraction. intermediate STATIONARY; down-weight MOVING.
psum_bank = (... terms WITHOUT intermediate_tile_idx ...) % bank_count   // :385-389
psum[bank, p_h, f_h] += nc_matmul(                              // :398-403
    stationary = source_tile_sbuf_view[..., intermediate_slice],   // inter [128, .]
    moving     = weights_sbuf_view[..., hidden_slice])             // down_w [128, .]
if intermediate_tile_idx == int_tiles-1:                        // :406  last accum tile
    out = bias ? tensor_tensor(psum + down_bias, add)           // :420-422
              : tensor_copy(psum)                               // :428-430
```

The down PSUM bank index deliberately **excludes** `intermediate_tile_idx` (`:385-389`) so the bank is invariant across the contraction axis and `+=` accumulates correctly. Bias is added only on the final intermediate tile.

> **NOTE — operand sides.** In CTE *both* gate/up and down put the **activation** (hidden / intermediate) on the **stationary** side and the **weight** on the moving side. There is no operand swap in the CTE non-quant path; the swaps (`gate_up_lhs_rhs_swap`, `lhs_rhs_swap`) are TKG-only knobs.

### CTE Sharding — `mlp_sharding.py`

`ShardedDim` (`mlp_sharding.py:33-37`) = `{NONE, SEQUENCE_LENGTH, INTERMEDIATE, BATCH_X_SEQUENCE_LENGTH}`. `calculate_sharding` (`:136`) chooses:

```c
shard_on_inter = (num_workers == 2)                         // :144-147
              and (B*S <= pmax*2)                           //   <= 256
              and not mlpp_has_projection_bias(params)
if H < 7168 or I < 1024: shard_on_inter = False             // :151-152  heuristic veto
sharded_dim = INTERMEDIATE if shard_on_inter else BATCH_X_SEQUENCE_LENGTH  // :154-160
// SEQUENCE_LENGTH path exists (:158-159) but is commented out — dead code
```

Under `INTERMEDIATE` (`_calculate_intermediate_sharding:99`), each of 2 cores owns `I//2` of the intermediate dim, so its down projection (contraction over its `I`-slice) yields a **partial** `[B·S, H]` sum. The partner half is exchanged through `shard_i_other_core_alloc` (`mlp_cte_constants.py:527-534`) and **added** (`store_half_hidden_tensor_tile:177` writes each core's half after the cross-core add). Under `BATCH_X_SEQUENCE_LENGTH` each core owns disjoint rows — **no reduce**.

---

## TKG (Decode) — `mlp_tkg_isa.py`

### Purpose

TKG handles `B·S ≤ 96`. With so few tokens (`T = B·S`), the win is keeping the tiny activation resident and overlapping the LNC-2 cross-core exchange with compute. The sharding axis is **H by default**, which makes gate/up a contraction-reduce and down an output-gather.

### Entry Point

```text
nki_mlp_tkg_isa_kernel (mlp_tkg_isa.py:169)
  ├─ get_verified_program_sharding_info(...)            // :207  (num_shards, shard_id)
  ├─ lnc_shard_dim = ShardDim.I if I > 2048 else ShardDim.H   // :118
  ├─ input_fused_add(...) (utils.py:256)               // :265  pre-norm residual
  ├─ input_norm_load(...) (utils.py:296)               // :341  rmsnorm_tkg / layernorm_tkg / plain
  ├─ process_gate_up_projection(...) (gate_up_projection.py:845)  // :345
  ├─ [if not gate_up_lhs_rhs_swap] transpose hidden via nc_matmul(identity)   // :360-377
  ├─ process_down_projection(...) (down_projection.py:757)        // :380
  ├─ [if ShardDim.I] sendrecv + tensor_tensor(add)      // :392-394  down reduce
  └─ nl.store(output_hbm[:, shard_slice], ...)          // :416 / :422
```

### Algorithm — the two matmul orientations

Each projection has a column-tiling form (activation stationary) and an `lhs_rhs_swap` form (weight stationary). The dense entry selects them via the `gate_up_lhs_rhs_swap` / `lhs_rhs_swap` (= `down_lhs_rhs_swap`) flags.

```c
// GATE/UP column-tiling  (gate_up_projection.py:26)
//   Hidden[T, HTile] STATIONARY @ Weight[HTile, I] MOVING -> [T, I]
psum[i_moving, ds(arrayTilingDim*factor, T), :512] += nc_matmul(   // :106-111
    hidden[0:H0, 0:T, h_off+array_tile_offset],            // stationary
    weight_tile[..., :512],                                // moving
    tile_position=(0, arrayTilingDim*factor),              // up-to-4x parallel matmul
    tile_size=(H0, arrayTilingDim))

// GATE/UP lhs_rhs_swap  (gate_up_projection_lhs_rhs_swap:252)
//   Weight[HTile, I] STATIONARY @ Hidden[T, HTile] MOVING -> [I, T]   (ready for down)
psum[i_stationary, 0:I0, 0:T] += nc_matmul(weight_tile[...], hidden[...])   // :424-426

// DOWN column-tiling (down_projection.py:23): Hidden[T,I] STAT @ Weight[I,HTile] MOV -> [T,H]
// DOWN lhs_rhs_swap  (down_projection_lhs_rhs_swap:402): Weight STAT @ Hidden MOV -> [H,T]
```

### Algorithm — clamp, activation, SwiGLU (dense path)

```c
// process_gate_up_projection (gate_up_projection.py:845) — the DENSE entry (NON-fused)
gate_psum = nc_matmul(...)            // gate proj
if num_shards > 1:                    // H-shard: gate is a PARTIAL sum
    sendrecv(src=gate, dst=recv, send=1-shard_id, recv=1-shard_id, pipe=0)  // :1065
    gate = tensor_tensor(gate, recv, op=nl.add)                            // :1072  REDUCE
gate = nisa.activation(op=act_fn, data=gate, scale=1.0, bias=zero)         // :1079-1081
up_psum = nc_matmul(...)
if num_shards > 1:
    sendrecv(src=up, dst=recv, ...)                                        // :1085
    up = tensor_tensor(up, recv, op=nl.add)                                // :1088  REDUCE
out = nl.multiply(gate, up)                                                // :1091  SwiGLU
```

> **CORRECTION (DENSE-MLP-3) —** D-O18 §3/§7 describes a gate/up **clamp** (`tensor_scalar(min upper, max lower)`) on the dense decode path. In 2.24.5133.0 the clamp lives **only** in `process_fused_gate_up_projection` (`gate_up_projection.py:576`, the *fused* MoE-shaped path, clamps at `:746-766`/`:806-827`). The dense entry `nki_mlp_tkg_isa_kernel` calls the **non-fused** `process_gate_up_projection` (`mlp_tkg_isa.py:345`), which has **no clamp** — its activation and multiply are unconditional. The clamp limits (`gate_clamp_*`, `up_clamp_*`) default to `None` (`constants.py:185-188`); the dense kernel never sets them. Treat clamp as a fused-path / MoE feature, not a dense-MLP feature.

### Algorithm — the H-shard reduce-vs-gather asymmetry

This is the defining behavior. The shard axis is chosen once (`mlp_tkg_isa.py:118`):

```c
I_SHARDING_THRESHOLD = 2048                                   // :27
lnc_shard_dim = ShardDim.I if I > 2048 else ShardDim.H        // :118  default H
// H-shard: assert H1 % num_shards == 0   (:124-126)
// I-shard: assert H  % num_shards == 0   (:120-122)
```

```text
                  contraction axis      cross-core op                where
  GATE/UP  under H-shard:  H (split)  →  sendrecv + tensor_tensor(ADD)   gate_up_projection.py:1065/1072, 1085/1088
  DOWN     under H-shard:  I (whole)  →  NONE — disjoint H halves stored to HBM[:, shard_slice]  (GATHER)
                                          mlp_tkg_isa.py:423  (i_shard_id = shard_id)
  DOWN     under I-shard:  I (split)  →  sendrecv + tensor_tensor(ADD)   mlp_tkg_isa.py:392-394 (then i_shard_id = 0)
```

Under **H-sharding** (the `I ≤ 2048` default) gate/up split their contraction axis `H` across the two cores, so each core holds a *partial* `[T,I]` that must be **summed** — `gate_up_projection.py` does the `sendrecv` + `nl.add` reduce (gate and up exchanged separately, `:1063-1072` and `:1084-1088`, so the gate SiLU overlaps the up exchange). Down, by contrast, contracts over the *whole* `I` and writes a **disjoint** H-half per core; there is **no `sendrecv` anywhere in `down_projection.py`** — the two halves are simply stored to non-overlapping HBM column slices (`mlp_tkg_isa.py:423`, `i_shard_id = shard_id`). That concatenation *is* the gather.

The I-shard branch is the mirror image: when `I > 2048` the kernel shards `I` instead, down now holds a partial sum, and the *only* TKG `tensor_tensor(add)` over `down_sb` fires (`mlp_tkg_isa.py:392-394`), after which both cores store the full reduced H (`i_shard_id = 0`).

> **QUIRK —** the asymmetry is *not* "gate/up always reduce, down always gather." It is "the projection whose **contraction** axis is sharded reduces; the projection whose **output** axis is sharded gathers." Under H-shard that makes gate/up reduce and down gather; under I-shard down reduces and gate/up are not split. A reimplementation that hard-codes "down = gather" will silently drop the partial-sum add for large-`I` models.

---

## Normalization Fusion (RMSNorm / LayerNorm)

### Purpose

The pre-MLP norm is fused into the kernel. CTE computes only the mean/rsqrt scaling inside the norm function and **defers γ (scale) and β (bias) to the transpose**, folding them into the same `tensor_scalar` that moves data PSUM→SBUF. TKG delegates to the shared `rmsnorm_tkg`/`layernorm_tkg` subkernels.

### Algorithm — CTE

```c
// RMSNorm  apply_rms_norm_to_source_tensor_tile (mlp_cte_norm.py:25)
sumsq = activation_reduce(op=square, data=x, reduce_op=add, bias=0)   // :69-76  Σx²
rms_inv = activation(op=rsqrt, data=sumsq, scale=1/H, bias=eps)       // :77-83  rsqrt(Σx²/H + ε)
x_n     = activation(op=copy,  data=x, scale=rms_inv, bias=0)         // :84-90  ε INSIDE sqrt

// LayerNorm apply_layer_norm_to_source_tensor_tile (mlp_cte_norm.py:98)
stats = bn_stats(x)                                                   // :149-157  (6 elems/tile)
agg   = bn_aggr(stats)                                                // :159-162  (mean, var)
std_inv = activation(op=rsqrt, data=agg[var_slot], bias=eps)          // :166-171  rsqrt(var + ε)
x_n   = tensor_scalar(data=x, op0=subtract(mean), op1=multiply(std_inv))  // :173-181

// gamma/beta FOLD at transpose  transpose_source_tensor_tile (mlp_cte_transpose.py:32)
xT = nc_matmul(stationary=x_n, moving=identity, is_transpose=...)     // :134-139  [BxS,H]->[H,BxS]
if apply_scale or apply_bias:                                         // :72
    op0 = multiply(gamma) ; op1 = add(beta) if both                  // :78-81
    xT  = tensor_scalar(data=psum, op0=op0, op1=op1)                  // :154-168  fused γ·x+β
```

> **NOTE — where ε lands.** RMSNorm adds ε to the **mean of squares** (`rsqrt(Σx²/H + ε)`, `scale=1/H`); LayerNorm adds ε to the **variance** (`rsqrt(var + ε)`, no scale, because `bn_aggr` already produced the true variance). Two different fusions; do not copy one into the other.

### Algorithm — TKG

`input_norm_load` (`utils.py:296`, called at `mlp_tkg_isa.py:341`) branches on `norm_type`: `RMS_NORM` → `rmsnorm_tkg` (`:350`), `LAYER_NORM` → `layernorm_tkg` (`:373`), `NO_NORM` → plain DMA. Both pass `force_lnc1 = (num_shards == 1)`; under LNC-2 the norm output is sliced per shard afterward (`utils.py:388-390`). See [Normalization Kernels](normalization-kernels.md) for the shared `rmsnorm_tkg`/`layernorm_tkg` internals.

---

## Quantization (FP8: STATIC / ROW — no MX)

### Purpose

Two FP8 modes ship. `STATIC` uses a precomputed per-tensor scale and a double-row matmul for 2× PE throughput; `ROW` computes a per-row dynamic absmax scale online for the intermediate tensor. Both produce `float8_e4m3` operands; there is no microscaling/MX path in the dense MLP.

### Algorithm — CTE intermediate quant

```c
// ROW: per-row dynamic absmax  perform_intermediate_row_quantization (mlp_cte_quantization.py:94)
if lower_bound != 0:                                                  // :157-165
    x = tensor_scalar(x, op0=minimum(+b), op1=maximum(-b))           // symmetric clip
absmax = tensor_scalar_reduce(x, op0=abs, reduce_op=maximum)         // :168-175  per-row max|x|
deq    = tensor_scalar(absmax, op0=multiply(1/max_pos), op1=maximum(1e-6))  // :176-183
q      = reciprocal(deq)                                              // :184-186
x_fp8  = activation(op=copy, data=x, scale=q)                         // :189-195

// STATIC: precomputed per-tensor scale  perform_intermediate_static_quantization (:31)
x_fp8 = activation(op=copy, data=x, scale=inv_input_scale)           // :74-80
x_fp8 = tensor_scalar(x_fp8, op0=minimum(+max_pos), op1=maximum(-max_pos))  // :81-88  clip
```

`max_pos` per dtype comes from `_max_pos_range_map` (`kernel_helpers.py:13`): `float8_e4m3 → 240.0`, `float8_e5m2 → 57344.0`. The dequant scales fold back at down-projection: STATIC via `activation(copy, scale=static_weight_scale, bias=down_bias)` (`mlp_cte_projection.py:637-643`); ROW via `tensor_tensor(multiply, weight_scales)` + `activation(copy, scale=source_scales, bias)` (`:649-657, :664-670`).

> **GOTCHA —** `mlp_cte_quantization.py` quantizes the **intermediate** tensor (the down-proj input), not the hidden input. A *quantized hidden input* is never absmax-quantized inline; it arrives pre-quantized and its per-row fp32 scale is the trailing fp32 column of each row (`build_mlp_params:323-325`, `load_hidden_scales` at `mlp_cte_tensor_io.py:367,377-379`). `_validate_row_quant_params` (`mlp_parameters.py:20-24`) enforces "weight scales present, input scales absent."

---

## Dense vs MoE Expert MLP

The dense MLP is the non-expert specialization of the MoE expert kernels. The shared substrate is the `MLPTKGConstants` attribute model (`mlp_tkg/constants.py:64`), whose `Attributes` dataclass carries *both* dense fields and MoE fields (`local_experts_num`, `all_experts`, `expert_affinities_scale_mode`, `is_mxfp4_kernel`, `is_gate_up_fused`, the clamp limits). The dense entry hard-sets the MoE knobs off:

```c
MLPTKGConstants.set_attributes(                       // mlp_tkg_isa.py:129
    is_mxfp4_kernel = False,   # "Not supported"      // :135
    is_gate_up_fused = False,  # "Not supported"      // :136
    ...)
```

| | Dense MLP (this page) | MoE expert MLP ([decode](moe-decode-tkg.md) / [prefill](moe-cte-prefill.md)) |
|---|---|---|
| Tokens | one MLP for all tokens; no expert dim | per-expert weights; loop / gather over experts |
| Weights | `gate_w[H,I]`, `up_w[H,I]` **separate**; `down_w[I,H]` | fused gate/up planes; per-expert `[E,…]` |
| Routing | none | RouterTopK, affinity scale, scatter-accumulate |
| MX / E8M0 | **none** (`is_mxfp4_kernel=False`) | `*_mx_shard_I.py` via `expert_mlp_tkg_all_expert_mx_impl.py` |
| Fused gate/up | **none** (`is_gate_up_fused=False`); separate matmuls | `process_fused_gate_up_projection` with clamp |
| TKG shard | H (default) or I (`I>2048`) | per the expert kernel |
| Output | single `[B,S,H]` to HBM (or SBUF tile) | per-expert planes / token-scatter |

> **CORRECTION (DENSE-MLP-4) —** D-O18 §8 states the dense MLP and selective-MoE **share** the same `mx_shard_H` projection primitives. In 2.24.5133.0 the dense `nki_mlp_tkg_isa_kernel` imports **only** `process_gate_up_projection` and `process_down_projection` from the non-MX `gate_up_projection.py`/`down_projection.py` (`mlp_tkg_isa.py:14-15`). The MX `fused_gate_up_projection_mx_shard_I` / `fused_down_projection_mx_shard_I` are imported **only** by `expert_mlp_tkg_all_expert_mx_impl.py` (`:18-24`). The two paths share the *attribute model and the SwiGLU algebra*, but the dense path does **not** call the MX shard kernels — those are MoE-only. The shared-subkernel claim holds for the non-MX projection *shape*, not for the MX primitives.

---

## Adversarial Self-Verification

Five strongest claims, re-challenged against the binary-derived `.py`:

1. **"`B·S ≤ 96` picks TKG."** `is_mlp_tkg` (`mlp_helpers.py:17`) = `batch*seq_len <= TKG_BS_SEQLEN_THRESHOLD`; `TKG_BS_SEQLEN_THRESHOLD = 96` (`:8`). CONFIRMED.
2. **"No MX in the dense MLP."** `QuantizationType` has only `NONE/STATIC/ROW` (`common_types.py:41-44`); `build_mlp_quant_params` raises otherwise (`mlp_parameters.py:77`); `rg -ni mx mlp/` = 0 hits; TKG sets `is_mxfp4_kernel=False` (`mlp_tkg_isa.py:135`). The MX `*_mx_shard_I` files are imported only by `expert_mlp_tkg_all_expert_mx_impl.py`. CONFIRMED — and DENSE-MLP-1/4 correct the report.
3. **"Gate/up reduce (add), down gather (no add), under H-shard."** Gate/up `sendrecv` + `tensor_tensor(...,op=nl.add)` at `gate_up_projection.py:1065/1072, 1085/1088`; `down_projection.py` has **no** `sendrecv` (grep = 0); the only down `add` is the `ShardDim.I` branch at `mlp_tkg_isa.py:392-394`. CONFIRMED — and the axis-keyed framing (QUIRK above) refines the report's "gate/up reduce / down gather" into the threshold-driven H/I rule.
4. **"CTE folds γ/β at transpose, ε inside sqrt."** Transpose fold `tensor_scalar(op0=multiply(gamma), op1=add(beta))` at `mlp_cte_transpose.py:154-168`; RMSNorm `rsqrt(Σx²/H + ε)` at `mlp_cte_norm.py:77-83`; LayerNorm `rsqrt(var + ε)` at `:166-171`. CONFIRMED.
5. **"Dense clamp is fused-path-only."** Dense calls `process_gate_up_projection` (`mlp_tkg_isa.py:345`), which has no clamp; clamp is in `process_fused_gate_up_projection` (`gate_up_projection.py:576`), and limits default `None` (`constants.py:185-188`). CONFIRMED — DENSE-MLP-3 corrects the report.

**Re-verification ceiling.** All evidence is readable, binary-derived Python from the wheel (`mlp/` + `mlp_tkg/`), cross-checked across `cp310`/`cp311`/`cp312` byte-identity and triangulated by three independent reads. Not traced to the BIR/penguin/BIR-kernel level: the `use_bir_tkg_kernel=True` fork into `_private_kernels.mlp` (an alternate TKG backend) was identified but its internals are out of scope. The `tile_position`/`tile_size` "up-to-4× parallel matmul" claim is taken from the call-site arguments and the [PE matmul encoding](../isa/pe-matmul-encoding.md); the exact PE-array parallelism factor was not measured against emitted BIR.

---

## Cross-References

- [MoE Context/Prefill (CTE)](moe-cte-prefill.md) — the expert-MLP prefill kernel the dense CTE path specializes from
- [MoE Decode (TKG)](moe-decode-tkg.md) — the expert-MLP decode kernel; source of the MX `*_mx_shard_I` and fused gate/up paths
- [NeuronCodegen Macro-Kernel Emitters](neuroncodegen-macro.md) — the MLP macro emitter that names this kernel
- [Normalization Kernels: RMSNorm & LayerNorm](normalization-kernels.md) — the shared `rmsnorm_tkg`/`layernorm_tkg` subkernels TKG delegates to
- [PE Matmul Encoding](../isa/pe-matmul-encoding.md) — `nc_matmul` operand orientation, `double_row_gen3`, `tile_position`/`tile_size`
- [NKI Architecture Overview](architecture-overview.md) — the trace → penguin.ir → BIR lowering stack
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition tiling and PSUM bank model the pipeline allocates against
