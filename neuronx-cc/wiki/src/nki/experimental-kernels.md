# Experimental & Pre-Prod NKI Kernels

> *All paths and line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the readable NKI library shipped inside the wheel under `nkilib/experimental/` (cp310; cp311/cp312 are byte-identical for every file cited). The kernel bodies are plain `.py` — binary-derived wheel artifacts, not stripped ELF — so every line is ground truth, but the `nisa.*` primitive bodies are compiled and their contracts are read from call-site semantics.*

## Abstract

This is a **clustered** page: four unrelated families of experimental NKI kernels that share exactly one property — **none has a production caller**. They live under `nkilib/experimental/`, each is referenced only by its own PyTorch numerical-reference oracle and (sometimes) its own `__init__.py`, and production NeuronxDistributed instead drives the *compiled* BIR twins of these kernels. They are staging / reference implementations: readable, traceable, sometimes shipped-but-broken. The page documents all four behind delimited section rules so a reimplementer can lift the algorithm without mistaking any of them for the shipped path.

The four sections are deliberately heterogeneous and must not be read as variants of one thing. **Section 1** is the MoE training **backward** pass (`blockwise_mm_bwd_dropless`) plus its H-sharded **forward** producer (`bwmm_shard_on_H`) — the gradient of the shipped blockwise-FFN expert kernel, with a *store-not-recompute* checkpoint strategy and per-expert read-modify-write accumulation. **Section 2** is three fine-grained **collectives** (`fgcc`, `fg_allgather`, `sb2sb_allgather`) — a double-buffered all-gather ring fused with matmul, its compute-free twin, and a small-tensor on-chip variant. **Section 3** is the dense full-transformer-block **decode composer** (`transformer_tkg`) with its genuinely-fused attention sub-block (`attention_block_tkg`) and the MoE-combine subkernel (`topk_reduce`). **Section 4** is experimental **conv1d / depthwise-conv1d** and the **cross-entropy** LSE loss pair.

For reimplementation, the contract per section is the *distinguishing mechanism*, proven against source: the MoE backward's H-shard / disjoint-byte combine and per-expert RMW; the collective ring's `collective_permute_implicit` double buffer; the transformer composer's trace-time multi-layer unroll; and the conv's im2col-by-scatter / K-replication and the CE's online log-sum-exp.

> **NOTE — every kernel here is unverified-in-production.** Section 0 proves the "no caller" claim by grep. Treat all four as designs you can study and rebuild, not as the code that runs when a real Llama-MoE compiles. The shipped MoE forward lives in [MoE CTE/Prefill](moe-cte-prefill.md) and [MoE Decode TKG](moe-decode-tkg.md); this page is its *backward* and its *experimental sharding axis*.

| | |
|---|---|
| **Tree** | `nkilib/experimental/` (GITIGNORED — `rg`/`fd` need `--no-ignore`) |
| **§1 MoE bwd** | `moe/bwd/blockwise_mm_backward.py` (175L) → `moe/bwd/bwmm_bwd_dropless.py` (2300L); fwd producer `moe/forward/bwmm_shard_on_H.py` (707L) |
| **§2 Collectives** | `collectives/{fgcc.py 1151L, fg_allgather.py 953L, sb2sb_allgather.py 167L}` |
| **§3 Transformer** | `transformer/{transformer_tkg.py 444L, attention_block_tkg.py 1811L}`; `subkernels/topk_reduce.py 146L` |
| **§4 Conv / Loss** | `conv/{conv1d.py 1347L, depthwise_conv1d.py 378L}`; `loss/cross_entropy.py 570L` |
| **Wheel parity** | every file md5/sha256-identical across cp310/cp311/cp312 |
| **Production status** | **none** — staging / reference only (see §0) |

---

## 0. No Production Callers — the cross-cutting fact

The single property all four families share is that **no kernel here is imported or called from outside its own file, its torch reference, or its `__init__.py`**. This is proven by grepping the entire `nkilib` tree (with `--no-ignore`, because `nkilib/` is in `.gitignore` and a plain `rg` silently returns nothing). The matrix below is the audit; each row's only references are the kernel's own definition site and its `*_torch.py` oracle.

| Symbol | Defined in | External caller? | Evidence |
|---|---|---|---|
| `blockwise_mm_bwd` (dispatcher) | `moe/bwd/blockwise_mm_backward.py` | none | only its own def + `bwmm_bwd_dropless` callee |
| `blockwise_mm_baseline_shard_hidden` | `moe/forward/bwmm_shard_on_H.py` | none | self only |
| `allgather_compute_matmul` | `collectives/fgcc.py` | none (twin is compiled `.so`) | self + compiled `_private_kernels/collective_matmul.so` symbols |
| `fine_grained_allgather` | `collectives/fg_allgather.py` | none | self; *imports* `fgcc` but no one imports it |
| `allgather_sb2sb[_tiled]` | `collectives/sb2sb_allgather.py` | none | self only |
| `conv1d` | `conv/conv1d.py` | torch ref only | `conv1d_torch.py` permutes filter (2,1,0) |
| `depthwise_conv1d_implicit_gemm` | `conv/depthwise_conv1d.py` | torch ref only | self + oracle |
| `cross_entropy_{forward,backward}` | `loss/cross_entropy.py` | torch ref only | self + oracle |
| `transformer_tkg` | `transformer/transformer_tkg.py` | torch ref only | self + `transformer_tkg_torch.py` |
| `attention_block_tkg` | `transformer/attention_block_tkg.py` | self + sharding/torch | `attention_block_tkg_sharding.py`, doc mentions |
| `topk_reduce` | `subkernels/topk_reduce.py` | **not even its own `__init__`** | `subkernels/__init__.py` exports only `find_nonzero_indices` / `indexed_flatten` |

> **GOTCHA — the gitignore trap.** `nkilib/` is gitignored. A reviewer who runs `rg blockwise_mm_bwd` from the repo root gets **zero hits** and may wrongly conclude the symbol does not exist. Every grep on this tree must pass `--no-ignore` (`rg`) or `--no-ignore` (`fd`). There are also two divergent `common_types.py` (the full `core/utils/` copy vs a truncated `_pre_prod` copy); always follow the actual `import` line, not the first match.

> **NOTE — the compiled twins are the production path.** `fgcc.py`'s nested-function names (`launch_collective_permutes`, `load_into_lhs_sbuf_use_sb_to_sb`, `run_matmul_sb_to_sb`, `write_to_output`) and its docstring "*Kernel is double buffered and supports multi channel cc permute*" appear verbatim in the compiled `neuronxcc/nki/_private_kernels/collective_matmul.so` symbol table. So `fgcc.py` is the *readable refactor* of a kernel that **does** ship — but only the `.so` is wired into production; this `.py` is the spec, not the binary on the critical path. [CONFIRMED via `strings` of the `.so`.]

---

## 1. MoE Backward — checkpoint-not-recompute, H-sharded

> Backing report: D-O25.

This section is the **gradient** of the shipped blockwise expert-FFN forward documented in [MoE CTE/Prefill §bwmm shard variants](moe-cte-prefill.md). It is structurally three distinct kernels, not three variants: a thin dispatcher, the 2300-line backward math, and a *forward* (`bwmm_shard_on_H`) that exists here only because it is the **producer of the two checkpoints** the backward consumes.

### Purpose

Compute the four (or six, with bias) gradients of a dropless MoE block — `dX`, `d(affinity)`, `dW_gate_up`, `dW_down`, optional `dB` — by replaying the saved forward activations rather than recomputing the matmuls. The forward (`bwmm_shard_on_H`) computes the same expert FFN as the shipped block-shard ([§O02-equivalent](moe-cte-prefill.md)) and I-shard variants, but on a **third sharding axis: the hidden dim H**, and it writes the activation checkpoints as a side product.

### Entry Point

```text
blockwise_mm_bwd(...)                       moe/bwd/blockwise_mm_backward.py:32  @nki.jit
  ├─ alloc 4(+2 bias) grad HBM tensors      :116-135   (same shape as primals)
  ├─ MOEBwdParameters(...).validate()       :137-162   moe_bwd_parameters.py
  └─ blockwise_mm_bwd_dropless(params)       :164       ── the whole backward
       └─ for block_idx in range(N):         bwmm_bwd_dropless.py:2183  ── SEQUENTIAL
            STEP 1  _compute_down_projection_output_grad     :480   dY, d(aff)
            STEP 2  _compute_gate_up_projection_output_grad  :763   SwiGLU bwd
            STEP 3  _compute_down_projection_weight_grad     :1271  dW_down  RMW
            STEP 4  _compute_hidden_states_grad              :1496  dX
            STEP 5  _compute_gate_up_projection_weight_grad  :1807  dW_gu    RMW
```

### Algorithm

The dispatcher is purely structural: it allocates grads, packs `MOEBwdParameters`, validates, and calls the *one* backend that ships.

```c
function blockwise_mm_bwd(...):                  // blockwise_mm_backward.py:32, @nki.jit
    // docstring :106 — "Currently only supports DROPLESS kernel type"
    hidden_states_grad        = alloc(shared_hbm, [T,H])         // :116
    expert_affinities_grad    = alloc(shared_hbm, [T*E,1])
    gate_up_proj_weight_grad  = alloc(shared_hbm, [E,H,2,I_TP])
    down_proj_weight_grad     = alloc(shared_hbm, [E,I_TP,H])
    if bias: alloc gate_and_up_proj_bias_grad[E,2,I_TP], down_proj_bias_grad[E,H]
    params = MOEBwdParameters(...)                               // :137-159
    params.validate(); params.validate_sharding(num_programs(0)) // :161-162
    blockwise_mm_bwd_dropless(params)                            // :164 — IN-PLACE
    return (h_grad, ea_grad, gup_w_grad, dp_w_grad [, bias grads])
```

The backward body is a **plain, sequential** `for block_idx in range(N)` loop (`bwmm_bwd_dropless.py:2183` — not a dynamic `while`). Each block resolves to one expert `e = block_to_expert[block_idx]`, and the five gradient functions run in order, each in its own SBUF scope. The SwiGLU backward (STEP 2) is the analytically-interesting one:

```c
function _compute_gate_up_projection_output_grad(...):   // bwmm_bwd_dropless.py:763
    // (a) REPLAY SwiGLU from the gate/up checkpoint — only the CHEAP ops re-run
    gate, up = dma_transpose(gate_up_proj_act_checkpoint_T[block])  // [N,2,I_TP,B]→[I,B]
    s = nisa.activation(op=silu, gate)                  // :952  re-apply SiLU forward
    m = s * up                                          // :976  SwiGLU product (= GUmult)
    dma_copy m -> gate_up_multipy_output_hbm            // :986  reused by STEP 3
    // (b) dM = dY · Wdownᵀ   (back through the down-proj matmul, K=H accumulation)
    dM = sum_over_H( nc_matmul(stationary=dYᵀ[H,B], moving=Wdownᵀ[H,I]) )   // :1105
    // (c) SwiGLU CHAIN RULE — the gradient split
    up_grad   = dM * s                                  // :1136  ∂m/∂u = s
    silu_grad = dM * up                                 // :1178
    s_prime   = nisa.activation(op=silu_dx, gate)       // :1186  HW activation DERIVATIVE
    gate_grad = silu_grad * s_prime                     // :1191  ∂m/∂g = dM·u·silu'(g)
    // (d) optional linear/non-linear clamp; (e) write dGU[:,0]=gate, dGU[:,1]=up
    core_barrier(dGU,(0,1))                             // RAW fence, not a reduce
```

> **QUIRK — the activation derivative is a hardware primitive.** `nl.silu_dx` (and `nl.gelu_apprx_sigmoid_dx`) is a *hardware* activation op that computes the SiLU/Swish derivative directly (`bwmm_bwd_dropless.py:1186`). The kernel does **not** hand-roll `sigmoid(g)·(1+g·(1−sigmoid(g)))`. These `_dx` ops appear *only* in this backward tree (grep-confirmed); `get_activation_ops` in `moe_bwd_parameters.py` maps `SiLU → (nl.silu, nl.silu_dx)` and `Swish → (gelu_apprx_sigmoid, gelu_apprx_sigmoid_dx)`. A reimplementation that lacks a HW `silu_dx` must substitute the analytic derivative.

### The distinguishing mechanism — proven

Two things make this kernel what it is, and both are confirmed verbatim against source.

**(1) Store-not-recompute checkpoint.** Unlike the attention backward ([§attention-bwd](attention-bwd.md)), which stores only the LSE and *recomputes* `P = exp(QKᵀ − LSE)`, this MoE backward **stores the full forward activations** and replays them. Two checkpoint tensors are saved by the forward: `gate_up_proj_act_checkpoint_T[N,2,I_TP,B]` (the gate/up *pre-activation* projections, transposed) and `down_proj_act_checkpoint[N,B,H]` (the down-proj *output* y, pre-affinity). In the backward only the **cheap** ops re-run — one `nisa.activation(silu)` and one multiply (STEP 2a) — while the expensive gate/up and down matmuls are *not* recomputed; their results flow through the checkpoints and the dgrad matmuls. This is classic gradient checkpointing in the *store* regime (memory↑, compute↓), the exact opposite of attention's *recompute* regime.

**(2) Per-expert RMW accumulation, race-free by disjoint sharding.** Because dropless puts *many* blocks on the same expert, the weight-grad functions accumulate into a shared per-expert HBM row via a **load-existing → add → store-back** triplet keyed on the expert index. The triplet is verbatim at `bwmm_bwd_dropless.py:1461-1490` (down-proj weight grad):

```c
// _compute_down_projection_weight_grad — the per-expert RMW (lines 1461-1490)
nisa.dma_copy(dst=existing_weight_grad,
              src=down_projection_weight_grad.ap(..., scalar_offset=expert_idx,
                                                 indirect_dim=0),     // :1466 GATHER row e
              dge_mode=hwdge)
nisa.tensor_tensor(dst=result, op=nl.add,
                   data1=existing_weight_grad, data2=result)          // accumulate
nisa.dma_copy(dst=down_projection_weight_grad.ap(..., scalar_offset=expert_idx,
                                                 indirect_dim=0),     // :1485 SCATTER back
              src=result, dge_mode=hwdge)
```

There is **no atomic and no lock**. Correctness rests on two facts: the block loop is *sequential* (`range(N)`, so the same expert's blocks never collide in time), and the two LNC cores own **disjoint H/I/B slices** so they never RMW the same bytes. The consequence is that the backward has **zero `sendrecv`** anywhere (grep-confirmed: `sendrecv` count = 0 in `bwmm_bwd_dropless.py`); the cross-core "combine" is just *disjoint HBM column writes*, fenced by `core_barrier` × 3 as read-after-write barriers — never as a collective reduce.

The shard axes per gradient (each core owns `H//num_shards` or `I_TP//num_shards`):

| Step | Gradient | Math | Shard axis |
|---|---|---|---|
| 1 | `dY`, `d(aff)` | `dY = dL/dy ⊙ aff`; `d(aff) = Σ_H(dL/dy ⊙ y_ckpt)` | **B-tile** (needs full H to reduce) |
| 2 | `dGU` (SwiGLU bwd) | `dM = dY·Wdownᵀ`; `d_up = dM·s`; `d_gate = dM·u·silu'(g)` | **I_TP** |
| 3 | `dW_down` | `dW_down[e] += mᵀ·dY` | **H** (RMW per expert) |
| 4 | `dX` | `dX = Σ_{gate,up}(dGU·Wguᵀ)` | **H** (scatter-accum per token) |
| 5 | `dW_gu` | `dW_gu[e] += xᵀ·dGU` | **H** (RMW per expert) |

> **GOTCHA — the `down_proj_act_checkpoint` is the down *output*, not the SwiGLU product.** The name suggests the SwiGLU product `m = silu(g)·u`, but STEP 1 uses it to form `d(affinity) = Σ_H(dL/dy ⊙ ckpt)`, and `d(affinity) = Σ(dy·y)` requires the *pre-affinity down output* `y`. That is exactly what `bwmm_shard_on_H`'s `compute_block_output_shard` writes (`:503-514`). The SwiGLU product `m` is **re-derived** in STEP 2a from the gate/up checkpoint, not stored. A reimplementer who feeds `m` here computes the affinity gradient wrong.

### The H-shard forward producer (`bwmm_shard_on_H`)

`blockwise_mm_baseline_shard_hidden` (`bwmm_shard_on_H.py:536`, `@nki.jit mode="trace"`) is the **third sharding axis** for the blockwise MoE forward, complementary to the shipped block-shard and I-shard variants:

| Variant | Shards | Cross-core combine | sendrecv |
|---|---|---|---|
| block-shard ([shipped](moe-cte-prefill.md)) | N blocks (ping-pong) | epilogue `dma_compute` reduce of the `[T,2,H]` plane | none |
| I-shard ([shipped](moe-cte-prefill.md)) | `I_TP` | per-block `sendrecv`+add of the down partial | **per block** |
| **H-shard** (this kernel) | hidden H | gate/up = `sendrecv`+add REDUCE; down = disjoint H-col GATHER | **1 call** |

The defining asymmetry: when H is the **contraction** dim (gate/up projection) each core holds a *partial sum*, so the projection must be **all-reduced** — and `bwmm_shard_on_H` does so with exactly **one** real `sendrecv` call site:

```c
function compute_gate_and_up_projections_shard(...):  // bwmm_shard_on_H.py:285
    partial = nc_matmul(stationary=gup_weight[H,I], moving=hiddenᵀ[H,B])  // partial over my H
    tensor_copy(partial -> sbuf)
    recv = sendrecv(send_to = recv_from = 1 - shard_id, pipe_id=0)        // :351  THE call
    full = tensor_tensor(op=add, local, recv)                            // sendrecv+add reduce
    dma_copy(full -> gate_up_activations_T[block, gate_or_up, i_tile, b]) // :365 CHECKPOINT
```

When H is the **output** dim (down projection) each core writes its *own* disjoint H columns — a pure layout gather, **no collective at all**. (A naive `rg sendrecv` returns 7 hits in this file; six are the `import`, docstrings, and `# LNC sendrecv` comments — only line 351 is a call.)

> **QUIRK — the `ShardOption` / `KernelTypeOption` enums are vestigial.** `moe_bwd_parameters.py` defines `KernelTypeOption {DROPPING=0, DROPLESS=1}` (`:121-122`) and `ShardOption {AUTO, SHARD_ON_HIDDEN=1, SHARD_ON_INTERMEDIATE=2, BASELINE_LNC1}`, and the dispatcher accepts both — but **neither is read** in the dispatch body. `blockwise_mm_bwd` *always* calls `blockwise_mm_bwd_dropless`, and the H-shard is hardcoded inside it. `affinity_option` and `block_tile_size` are likewise accepted and unused. They are extension points, dead today. [CONFIRMED — same dead-knob smell as the shipped forward's `HI_LO` / `n_block_per_iter`.]

> **NOTE — "Beta 3: dma_copy cannot read from psum directly."** Both checkpoint writers stage the PSUM result through SBUF before the DMA (`bwmm_shard_on_H.py:506`). This is a HW/SW limitation of the beta3 [BirCodeGenLoop](bircodegenloop.md) path, worth reproducing: a PSUM→SBUF `tensor_copy` hop precedes any `dma_copy` of a matmul result to HBM.

---

## 2. Fine-Grained Collectives — the double-buffered all-gather ring

> Backing report: D-O26. Relates to the planned collective lowering in [Part 13]; the BIR-level codegen is in [BirCodeGenLoop Collectives](bircodegen-collective.md).

Three files, three distinct things — and despite the naming, `fg_allgather` *depends on* `fgcc`, not the reverse (`fg_allgather.py:24` is `from . import fgcc`).

### Purpose

Implement tensor-parallel all-gather (and the gather-then-GEMM idiom) **inside one NKI kernel** as a `RANK_N`-step ring, so the gather is *pipelined chunk-by-chunk* and overlaps with the consuming compute, instead of being one monolithic `AllGather` HLO collective. `fgcc` is the framework (AG fused with matmul); `fg_allgather` is `fgcc` with the matmul removed; `sb2sb_allgather` is the odd one out — a single high-level `ncc.all_gather` for small tensors.

### Entry Points

```text
allgather_compute_matmul(lhs[m,K], rhs[K,N], tp_degree, num_groups, force_hbm_cc)
                                            fgcc.py:27  @nki.jit  →  out = AllGather(lhs) @ rhs
fine_grained_allgather(lhs[m,K], tp_degree, num_groups, force_hbm_cc)
                                            fg_allgather.py:27  @nki.jit  →  result[RANK_N,…,local_M,K]
allgather_sb2sb(inp[H,W], replica_groups, tp_degree)
                                            sb2sb_allgather.py:25  @nki.jit  →  out[H, W*tp_degree]
```

### Algorithm — the ring (shared by `fgcc` and `fg_allgather`)

The all-gather is `RANK_N` steps of a unidirectional ring "collective permute" (each rank passes its current tile to its neighbour), **double-buffered** so step k+1's permute overlaps step k's consuming work. The canonical shape is the `fg_allgather` docstring (`:66-77`):

```c
// fine_grained_allgather ring  (RANK_N total iters, 2 permutes per loop step)
numerator0 = collective_permute_implicit_current_processing_rank_id(0, channel, rg)  // runtime resolver
result[numerator0] = buf0                              // iter 0 = LOCAL data (no wait)
collective_permute_implicit(buf0 -> buf1, rg, channel_ids)
result[numerator1] = buf1                              // iter 1
for step in sequential_range(1, RANK_N // 2):          // remaining RANK_N-2 ranks
    permute buf1 -> buf0;  num = ...(2*step);   result[num] = buf0   // even
    permute buf0 -> buf1;  num = ...(2*step+1); result[num] = buf1   // odd
```

Two facts make this fine-grained and overlapping. First, `collective_permute_implicit` (BIR `CollectiveKind = 9 PermuteImplicit`) is the ring step; a *runtime resolver* `collective_permute_implicit_current_processing_rank_id(iteration_id, channel_id, replica_group)` returns which rank's data is currently in the buffer ("numerator"), used as an indirect `scalar_offset` to scatter the partial into the correct rank slot of the result tensor. Second, `buf0` / `buf1` are **disjoint** SBUF (or HBM) regions, so the backend can run the next permute concurrently with the current tile's matmul (`fgcc`) or transpose+scatter (`fg_allgather`).

```c
function allgather_compute_matmul(lhs, rhs, tp_degree, num_groups, force_hbm_cc):  // fgcc.py:27
    RANK_N = tp_degree                                      // assert even, :78
    CHANNEL_N = channel_table(tp_degree, get_nc_version())  // :96-115
    // tp 4/8/16 → 2 ; tp 32 → (2 if nc_version>=3 else 4) ; tp 64/128 → 4 ; else 1
    replica_group = ReplicaGroup(_generate_replica_groups(tp_degree, num_groups))
    // SBUF-BUDGET DECISION (the path selector)
    rhs_in_sbuf = (2*SIZE_RHS <= _MAX_RHS_SBUF_BYTES)        // 16<<20, :144,149
    lhs_in_sbuf = (not force_hbm_cc) and (2*SIZE_LHS <= remaining of _TOTAL_SBUF_BUDGET_BYTES)
                                                            // 24<<20, :146,159
    if lhs_in_sbuf: run_sbuf_ring(...)   // permute = SBUF↔SBUF, buffers on-chip
    else:           run_hbm_ring(...)    // permute over shared_hbm buffers
```

> **NOTE — transport path is chosen by a 24 MiB SBUF budget.** Each ring kernel has an SBUF path and an HBM path. The selector is whether the double-buffered operand footprint fits `_TOTAL_SBUF_BUDGET_BYTES = 24 << 20` (`fgcc.py:146`, `fg_allgather.py` similar), with a separate `_MAX_RHS_SBUF_BYTES = 16 << 20` (`:144`) for the matmul RHS. `force_hbm_cc=True` forces HBM. The result-tensor axis order even *swaps* between paths — `(RANK_N, LNC_N, CHANNEL_N, …)` in SBUF vs `(RANK_N, CHANNEL_N, LNC_N, …)` in HBM — matching the `lnc_id*CHANNEL_N+channel` vs `channel*LNC_N+lnc_id` offset formulas.

`fg_allgather` reuses `fgcc`'s helpers directly — `fgcc._generate_replica_groups` (`fg_allgather.py:118`), `fgcc._launch_collective_permutes_sbuf` (`:513/571/627`), `fgcc._launch_collective_permutes_hbm` (`:783/814/843`) — and replaces each ring step's matmul with a transpose-and-scatter copy. The "fine-grained" win is identical: compute starts on rank 0's data at iter 0 and never waits for the full gather.

### The small-tensor variant (`sb2sb_allgather`)

`allgather_sb2sb` is **not** the ring engine. It loads the input to SBUF, calls `ncc.all_gather` once, and stores back:

```c
function allgather_sb2sb(inp[H,W], replica_groups, tp_degree):   // sb2sb_allgather.py:25
    kernel_assert(H <= 128)                       // H is the SBUF partition dim
    in_buf  = sbuf(H, W);  dma_copy(in_buf <- inp)              // HBM→SBUF
    out_buf = sbuf(H, W*tp_degree)
    ncc.all_gather(dsts=[out_buf], srcs=[in_buf],
                   replica_group=replica_groups, collective_dim=1)  // SBUF→SBUF, :82-90
    dma_copy(out <- out_buf)                                    // SBUF→HBM
```

The "SB2SB" is that both the collective's source and destination are **SBUF** (`in_buf → out_buf`), which is what *enables* the compiler to lower the cross-core exchange onto the GPSIMD on-die SB2SB copy and skip an HBM round-trip. `allgather_sb2sb_tiled` (`:94-167`) adds M-tiling (`TILE_M = min(M,128)`) and LNC sharding over `affine_range` per-core tiles.

> **CORRECTION (O26-H7) — `sb2sb`'s `all_gather` is NOT directly the GPSIMD SB2SB op.** The BIR lowering pass `LowerLocalCollectives` only decomposes `CollectiveKind` 0/1/2 (`SendRecv` / `SendRecvCCE` / `AllReduce`) locally; `AllGather` is **kind 4 ≥ 3 → fence-only / remote-ICI**, *not* auto-rewritten to `GPSIMDSB2SB`. The on-chip fast path is reached only if the `ncc.all_gather` front-end first decomposes the gather into per-neighbour `SendRecv` (kind 0) micro-ops, which *can* lower to `GPSIMDSB2SB` (when `isCompatible` ∧ ≤ 1024 B/partition). The SBUF-resident buffers **enable** the on-chip path; they do not pin it. The actual transport-kind is decided downstream, not in this `.py`. [STRONG / INFERRED — boundary not visible in source.]

---

## 3. Transformer Decode Composer — `transformer_tkg`, `attention_block_tkg`, `topk_reduce`

> Backing report: D-O28. The dense full-block analog of the MoE megakernel in [MoE Decode TKG](moe-decode-tkg.md).

### Purpose

`transformer_tkg` composes the **entire** dense (non-MoE) decode model — all `num_layers` of `(RMSNorm→QKV→RoPE→Attn→o_proj→residual→RMSNorm→MLP→residual)` — into one BIR graph by **tracing** the leaf `@nki.jit` kernels in a host-side layer loop. `attention_block_tkg` is the genuinely-fused attention sub-block (one real `@nki.jit`). `topk_reduce` is an unrelated MoE-combine subkernel that the cluster includes because it sits in `experimental/subkernels/`.

### Algorithm — the multi-layer composer

```c
// transformer_tkg.py — @nki.jit COMMENTED OUT (:90), a trace-time composer
function transformer_tkg(X, W_qkvs[], W_outs[], ..., num_layers, replica_groups,
                         sbuf_residual_and_cc=False):
    n_prgs = get_verified_program_sharding_info(..., 2)   // LNC-2 forced
    current = X
    for layer_idx in range(num_layers):                   // UNROLLED at trace time
        quant_type = ROW if W_gate_scales[layer] else NONE   // per-layer FP8
        attn = attention_block_tkg(current, ..., fused RMSNorm-X in qkv)   // one @nki.jit
        attn = all_reduce(attn)                           // TP all-reduce (nccl OR SB2SB)
        current = current + attn                          // residual #1
        mlp  = mlp(current, ..., RMS_NORM, SiLU, quant_type)   // one @nki.jit
        mlp  = all_reduce(mlp)                             // TP all-reduce
        current = current + mlp                           // residual #2
    return current
```

So "megakernel" here means the **whole layer stack traced into one BIR graph** (the loop is unrolled at trace time), *not* one jit'd leaf. Two residuals and two all-reduces per layer; the pre-attn RMSNorm is fused inside `qkv` (`fused_norm_type=RMS_NORM`) and the pre-MLP RMSNorm inside `mlp` — there is no standalone norm call. This contrasts with the MoE megakernel ([moe-decode-tkg](moe-decode-tkg.md)), which *is* one `@nki.jit` fusing one block's stages.

> **CORRECTION (O28-F1) — `transformer_tkg` is not a `@nki.jit` kernel.** Its decorator is commented out: `transformer_tkg.py:90` reads `# @nki.jit  # Commented out - use nki.jit() at call site to avoid double-jit stack overflow`. It is a plain Python trace-time composer; a caller must wrap `nki.jit()` at the call site. A reimplementer who decorates it directly hits the double-jit stack overflow it was un-decorated to avoid.

The default **HBM path** (`sbuf_residual_and_cc=False`) round-trips `current` through HBM each layer; the **SBUF-residual path** keeps the residual and all-reduce in SBUF (`_sb2sb_all_reduce_gather`: `nccl.all_reduce` on the H1-shard slice, then `nisa.sendrecv` to swap the other core's H-shard, then re-layout), so a layer touches HBM only for its single output. The SBUF path is the latency-optimal decode route — but it is broken as shipped:

> **GOTCHA — real `NameError` bug in the SBUF-residual path.** At `transformer_tkg.py:257`, the attention call passes `attention_mask=mask` — but `mask` is **undefined** in scope. The signature has `mask_cache` and `mask_active` (verified: lines 104-105), no `mask`. The HBM branch (`:355`) correctly passes `mask_cache`. This branch raises `NameError` at trace time, confirming the SBUF-residual path is untested in this shipped copy. The `mask_active` parameter is *never consumed* anywhere — a dead signature param. Both facts corroborate §0's "no caller". [CONFIRMED — independently verified against the file.]

### `attention_block_tkg` — the genuinely-fused sub-block

`attention_block_tkg` (`:82`, real `@nki.jit`, ~40 kw-only params) fuses, all SBUF-resident with no inter-stage HBM round-trip:

```text
RMSNorm-X (inside qkv) → QKV projection → split/transpose Q,K → [pre-RoPE RMSNorm-QK]
  → RoPE (RoPE_sbuf, in-SBUF) → [post-RoPE RMSNorm-QK] → [FP8 K/V quant] → [KVDP gather]
  → attention_tkg (flash cascaded softmax over prior+active) → [KVDP scatter]
  → KV-cache update (block/flat, vector/scalar indirect DMA) → [o_proj]
```

The fusion win: `QKV_out`, `Q_sb`, `K_sb`, the per-head transpose PSUM, RoPE buffers, RMSNorm scratch are **all SBUF**. The only HBM touches are X-in (if HBM), the weights, the V round-trip (`attention_tkg` loads V tile-by-tile in the P·V matmul), the KV-cache scatter (the caches *are* HBM state), and the final output. Q/K never leave SBUF until the cache scatter. It ships its own `_rms_norm_inplace` (`:1093` — `x²` summed via `nc_matmul` with an all-ones operand, the matmul-as-reduction trick, then `activation(rsqrt, bias=eps)`), distinct from the `qkv`/`mlp` fused norms. FP8 KV-cache quant clips to ±240 (`_quantize_to_fp8` at `:1694`; the ±240 bound is the E4M3 max, applied via the symbolic `get_max_positive_value_for_dtype(float8_e4m3)` constant — saturating, lossy). RoPE is a fused site here (`RoPE_sbuf` inside `_process_head_group`), with `fuse_rope=False` passed to `attention_tkg` precisely *because* RoPE is already applied upstream. See [RoPE Kernels](rope-kernels.md), [Flash-Attention Decode](attention-tkg.md).

### `topk_reduce` — a misnomer (gather + K-way reduce, not top-K select)

```c
function topk_reduce(input[TK_padded, H+2], T, K, ...):   // subkernels/topk_reduce.py
    // input = sparse all_to_all_v() output: 1 row per (token,expert), global token
    // index packed into the last 2 bf16 cols as one int32. T<=128, K<=8. LNC shards H.
    idx_col = dma_transpose(input last-2-bf16 → [1,TK_padded] int32)   // extract packed idx
    idx_bcast = stream_shuffle_broadcast(idx_col → [T, TK_padded])     // row 0 to all T parts
    arange = iota([T,8], channel_multiplier=1)               // partition t holds value t (:106)
    nisa.nc_find_index8(data=idx_bcast, vals=arange,
                        dst=gather_token_indices[T,8])        // :112  INDEX-MATCH (==), not max
    for k in range(K):                                       // K-WAY REDUCE
        src = input.ap(..., vector_offset=gather_token_indices[:,k], indirect_dim=0)  // gather row
        if k == 0: dma_copy(reduced_sb <- src)               // :131
        else:      dma_compute(reduced_sb, srcs=[src, reduced_sb],
                               reduce_op=nl.add, unique_indices=True)   // :136  RMW add
    dma_copy(output_hbm[:, H_local_slice] <- reduced_sb)     // :144  each core its H shard
```

> **CORRECTION (O28-F4) — `topk_reduce` does NO top-K selection.** It is the MoE **combine** step: for each token it gathers the (up to K) already-routed expert-output rows from a sparse `all_to_all_v` buffer and *sums* them. The "topK" is the K experts a token was *already routed to* upstream by [router_topk](router-topk.md); no top-K is computed here. Crucially it reuses `nc_find_index8` as an **index-equality match** (find the first-occurrence rows where `packed_index == t`) — the *same* DVE silicon op that [scan-reduce-topk](scan-reduce-topk.md) / `router_topk` drive on *values* to get the largest-K. One op, two idioms; `topk_reduce` is a MoE-dispatch sibling, **not** a member of the value-top-K family. There is no `max8` and no `match_replace8` strike-out anywhere in the file (grep-confirmed), and `subkernels/__init__.py` does not even export `topk_reduce`.

### Why fuse the decode block

Decode is **latency-bound** — `attention_block_tkg` asserts `B*S_tkg*q_heads <= pmax` (≈ 1 token/seq, B≤16, S_tkg≤8). With so few tokens there is no block to amortize per-stage HBM round-trips over, so per-stage HBM traffic would dominate. Fusing norm→QKV→RoPE→attn→o_proj into one SBUF-resident kernel reads the token's H-vector from HBM once; the SBUF-residual `transformer_tkg` extends this across the attn/MLP boundary and the residual/all-reduce (SB2SB), so a layer touches HBM only for its output. Both force LNC-2 with H split across cores, `core_barrier` at each cross-core hand-off, and `sendrecv` for the SB2SB exchange. The same `BufferManager(0, 200*1024)` is threaded through attention and MLP, with `while sbm.heap: sbm.pop_heap()` between them to free attention's allocations for the MLP. Identical rationale to the MoE decode megakernel.

---

## 4. Experimental Conv & Cross-Entropy Loss

> Backing report: D-O27. Conv frontend canonicalization is a *separate layer* — see [Cross-References].

### Purpose

`conv1d` is a standard cross-channel 1D conv lowered to `nc_matmul`; `depthwise_conv1d_implicit_gemm` is a per-channel conv with no cross-channel contraction; the `cross_entropy` pair is a streaming, numerically-stable CE loss over a huge vocab. All are self-contained device TKG kernels, independent of the host-side conv/softmax legalization.

### `conv1d` — im2col-by-scatter + K-replication

```c
function conv1d(x_in[B,C_in,L], filters[K,C_in,C_out], bias, stride,
                padding=(pad_l,pad_r), dilation, activation_fn, lnc_shard):  // conv1d.py:1160
    L_out = (L + pad_l + pad_r - dilation*(K-1) - 1) // stride + 1
    // y[c_out,l] = Σ_cin Σ_k filters[k,c_in,c_out] · x[c_in, l*stride + k*dil - pad_l]
    // realised as a matmul whose CONTRACTION (partition) dim packs BOTH c_in AND stacked taps
```

The core trick is **K-replication**: to fill the 128-partition systolic array when `C_in` is small, multiple kernel taps are stacked along the partition dim (`_get_k_replication_params` at `:421`; the decision table is at `:438-439`, documented `:269-273`):

| `c_in_tile` | K_REP | partition_stride | rationale |
|---|---|---|---|
| ≤ 32 | `min(K, 4)` | 32 | 4 taps × 32 = 128 |
| 33–64 | `min(K, 2)` | 64 | 2 taps × 64 = 128 |
| > 64 | 1 | `c_in` | no stacking |

The "im2col" is a **scatter, not a materialized matrix**: `_scatter_input_to_stacked` (`:521`) produces the overlapping sliding windows by strided `tensor_copy` into partition sub-blocks (row-block k holds the input shifted by `k·dilation`), with gaps zero-padded via `memset` so the contraction picks up only real channels. The filter layout is **`[K, C_in, C_out]`** (kernel-tap major, `:209` — *not* torch's `[C_out, C_in, K]`; the oracle permutes `(2,1,0)`). Bias-add and activation are fused into the PSUM→SBUF copy (`tensor_scalar(+bias)` then `activation(fn)`); bias is always loaded fp32. `lnc_shard` splits `C_out` across cores — output-channel parallel, no cross-core reduce (the `C_in` contraction is wholly local).

### `depthwise_conv1d_implicit_gemm` — true depthwise, implicit GEMM

```c
function depthwise_conv1d_implicit_gemm(img[N,C,1,W], filter[C,1,1,S], padding,
                                        stride, rhs_dilation, lhs_dilation,
                                        feature_group_count):   // depthwise_conv1d.py:25
    kernel_assert(feature_group_count == C)        // TRUE depthwise → no Σ over channels
    kernel_assert(stride_h == 1 and rhs_dilation == (1,1) and lhs_dilation == (1,1))
    // out[c,q] = Σ_{s} filter[c,s] · input[c, q*stride_w + s - Wp_l]   (per channel, independent)
    for channel:
        // implicit GEMM = a degenerate (1×S)·(S×Q) matmul per channel
        nc_matmul(dst=psum[1, q_tile],
                  stationary=filter_tile[s_tile, channel:channel+1],   // (S,1)
                  moving    =input_tile [s_tile, q_start:q_end])       // (S,Q)
```

The contraction is over the **kernel taps S only**, done independently per channel (`feature_group_count == C` ⇒ output channel `c` depends only on input channel `c` — no channel sum). im2col is **implicit**: `input_tile[s,q] = input[s_start + s + q·stride_w]` is built without an unfold matrix by loading with an access pattern that encodes the slide (`:319` — for `stride_w==1` a single bulk DMA where consecutive partitions read consecutive elements; for `stride_w>1` a bulk load + on-chip strided `tensor_copy`). Filters are preloaded once per channel-tile and transposed via `nc_transpose`. Supports any `stride_w` but **no dilation and `stride_h==1` only** — contrast `conv1d`, which supports dilation + asymmetric pad.

### `cross_entropy_forward` — online log-sum-exp

```c
function cross_entropy_forward(logits[num_pos,V], targets[num_pos] int32, ...):  // cross_entropy.py:27
    m = -inf;  d = 0                                  // running max, running denom
    for chunk in vocab_chunks(V):                     // STREAMING — never materialize softmax(V)
        load chunk (pad tail with -inf so exp→0)
        chunk_max = reduce_max(chunk)                 // tensor_reduce
        m_new     = max(m, chunk_max)
        correction = exp(m - m_new)                   // activation(exp)
        d_corr    = d * correction
        exp_chunk = exp(chunk - m_new)                // every exp argument <= 0  (stable)
        d_new     = d_corr + reduce_add(exp_chunk)
        m, d = m_new, d_new
    lse = m + log(d)                                  // activation(log) + add
    // loss = lse - target_logit  (the GATHER)
    target_logit = logits.ap(..., scalar_offset=targets[pos], indirect_dim=1)  // indirect gather
    loss = lse - target_logit                         // = -log_softmax[target]
```

This is the flash/Milakov running-`(m,d)` recurrence — it never materializes a softmax over the whole vocab. The target-class logit is **gathered** via an indirect DMA (`scalar_offset = target[pos]`), and `loss = lse − target_logit = −log_softmax(logits)[target]`. The forward does **no reduction** (returns `reduction='none'` per-position loss) and saves `lse_state` for the backward.

### `cross_entropy_backward` — `softmax − onehot`, no scatter

```c
function cross_entropy_backward(logits, targets, lse_state, reduction="mean",
                                inplace=True, ...):   // cross_entropy.py:307
    grad_scale = (1/num_positions) if reduction=="mean" else 1.0     // :411
    for chunk in vocab_chunks(V):
        softmax_chunk = exp(logits_chunk - lse)       // RECONSTRUCT from saved LSE (no re-derive)
        grad_chunk    = grad_scale * softmax_chunk
        indices       = iota(offset=chunk_start, step 1)             // [chunk_start … +chunk]
        mask          = (indices == batch_targets)    // tensor_scalar(equal), broadcast targets
        grad_chunk   -= mask * grad_scale             // dense onehot subtraction — NO scatter
        store grad_chunk -> grad_logits
```

`grad_logits[i,j] = grad_scale·(softmax(logits[i,j]) − 1{j == target[i]})`, with softmax **reconstructed** from the saved LSE (`exp(logit − lse)`, no re-derivation of max/sum). The onehot is built **densely** with `iota + equal` (cheaper than a scatter for large P; the mask buffer is fp32 to avoid bf16 index-precision loss for vocab > 256). `inplace=True` (default, `:316`) overwrites `logits_hbm` with the gradient, saving `num_pos·V·dtype_bytes`. Reduction lives *here* via `grad_scale`, not in the forward.

> **GOTCHA — neither CE kernel implements `ignore_index`.** Grep for `ignore_index` across `loss/` returns **zero** hits. PyTorch's `ignore_index` semantics are unsupported, and the `mean` reduction divides by the *full* `num_positions` (no ignored-token exclusion). A reimplementer porting PyTorch CE must not assume ignore-index masking. [CONFIRMED.]

> **NOTE — these conv kernels are not the lowering target of `mhlo.convolution`.** The host-side `CanonicalizeConv` pass only normalizes the HLO conv into zero-pad/unit-stride form; it performs **no** conv→matmul or im2col rewrite. The im2col-by-scatter / implicit-GEMM lowering here is *this device kernel's own logic*, a different layer. Likewise the CE kernels reimplement log-softmax on-device from primitives and emit no `AwsNeuronSoftmax` custom-call. Do not conflate the conv/softmax *frontend legalization* with these *device kernels*.

---

## Adversarial Self-Verification

Five strongest claims, re-challenged against the binary-derived source:

1. **"No production caller for any kernel"** — re-grepped each symbol with `rg --no-ignore` across all of `nkilib`. Confirmed: every reference is the kernel's own def, its `*_torch.py` oracle, or (for `attention_block_tkg`) its sharding/torch siblings. `topk_reduce` is not even in its own `__init__.py` (exports only `find_nonzero_indices` / `indexed_flatten`). **HOLDS.**
2. **"MoE backward has zero `sendrecv`; combine is disjoint HBM writes"** — `rg --no-ignore -c sendrecv bwmm_bwd_dropless.py` = 0. The per-expert RMW triplet (`scalar_offset=expert_idx` load → `tensor_tensor add` → store) is verbatim at `:1461-1490`. **HOLDS.**
3. **"`bwmm_shard_on_H` has exactly one real `sendrecv` call"** — a naive grep gives 7 hits; six are the `import` (`:22`), docstrings (`:292/301`), and comments (`:349/607/608`). The single call is `:351`. **HOLDS — the over-count was the trap; corrected.**
4. **"`transformer_tkg.py:257` is a live `NameError`"** — read both the line (`attention_mask=mask`) and the signature (`mask_cache`/`mask_active` at `:104-105`, no `mask`). The HBM branch (`:355`) uses `mask_cache`. **HOLDS — independently verified.**
5. **"`topk_reduce` does index-match, not value top-K"** — `nc_find_index8(data=token-index-list, vals=arange)` at `:112` feeds *target token ids* as `vals`, not values; the result drives a `dma_compute(reduce_op=add)` gather-sum at `:136`. No `max8`/`match_replace8` in the file. **HOLDS.**

**Re-verification ceiling.** Three boundaries are not pinned by this source and are marked accordingly: (a) whether `sb2sb_allgather` *actually* reaches `GPSIMDSB2SB` for a given `(H,W,tp_degree)` depends on the compiled `ncc.all_gather` front-end + the BIR `LowerLocalCollectives` pass, not this `.py` (§2 CORRECTION, STRONG/INFERRED). (b) The exact numeric values of `gemm_stationary_fmax`/`gemm_moving_fmax` (assumed 128/512 TRN2) are not dumped from the ISA `.so` here (STRONG). (c) `collective_permute_implicit_current_processing_rank_id`'s rank-rotation formula lives in compiled `nki.nccl`, treated as an opaque runtime resolver. Everything in the `.py` bodies is CONFIRMED verbatim; the compiled-boundary claims are honestly downgraded.

---

## Related Components

| Name | Relationship |
|---|---|
| [MoE CTE/Prefill](moe-cte-prefill.md) | the SHIPPED blockwise forward; §1 is its backward + a third (H) shard axis |
| [MoE Decode TKG](moe-decode-tkg.md) | the MoE megakernel; §3 `transformer_tkg` is the dense analog |
| [Flash-Attention Backward](attention-bwd.md) | recompute checkpoint policy — the OPPOSITE of §1's store policy |
| [Flash-Attention Decode](attention-tkg.md) | the Stage-3 attention `attention_block_tkg` wraps |
| [MoE Routing: router_topk](router-topk.md) | value-top-K user of `nc_find_index8`; §3 `topk_reduce` is the index-match sibling |
| [Scan / Reduce / Top-K](scan-reduce-topk.md) | the value-top-K family `topk_reduce` is NOT a member of |

## Cross-References

- [BirCodeGenLoop Collectives](bircodegen-collective.md) — how `collective_permute_implicit` (kind 9) / `all_gather` (kind 4) lower to BIR
- [RoPE Kernels](rope-kernels.md) — `RoPE_sbuf`, the fused RoPE site inside `attention_block_tkg`
- [Normalization Kernels](normalization-kernels.md) — the fused RMSNorm; `attention_block_tkg` ships a second `_rms_norm_inplace`
- [Dense MLP](dense-mlp.md) — `transformer_tkg`'s per-layer MLP leaf (`mlp_tkg` / `mlp_tkg_mx`)
- [BirCodeGenLoop (beta3)](bircodegenloop.md) — the "Beta 3: dma_copy cannot read psum directly" PSUM→SBUF hop
- [nkilib Infrastructure](nkilib-infrastructure.md) — `SbufManager` / `BufferManager`, the gitignored two-`common_types` trap
