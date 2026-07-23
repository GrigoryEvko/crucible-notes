# MoE Context/Prefill (CTE): Dispatch and the bwmm Shard Variants

> *All source line numbers on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (the cp310 wheel; the cp311/cp312 wheels carry byte-identical kernel sources per their RECORD sha256). The kernels are shipped as readable NKI-DSL Python under `nkilib/core/moe/moe_cte/` — a binary-derived wheel artifact, citeable verbatim.*

## Abstract

`moe_cte` is the nkilib *context-encoding* (prefill) Mixture-of-Experts FFN. It is the megabatch token path: tokens are pre-routed by an upstream router into `N` fixed-size blocks of `B` tokens each, every token in a block goes to one expert, and the kernel runs a dense three-matmul expert FFN per block — `gate = hidden @ Wg`, `up = hidden @ Wu`, `inter = act(gate) ⊙ up`, `out = inter @ Wdown`, then an expert-affinity scale and a scatter-add back to the token rows. This is the *blockwise-matmul* (`bwmm`) formulation of MoE: a sparse router decision turned into a sequence of dense per-expert GEMMs by the block layout. The decode-phase sibling is [MoE decode (TKG)](moe-decode-tkg.md) *(planned)*; the router that builds the block layout is the MoE router page *(planned)*.

The page has two halves. The first is the **dispatch**: a `MoECTESpec` dataclass carries a `MoECTEImplementation` enum that selects one of seven declared variants — but the shipped `moe_cte()` body wires only **five** of them. The second is the **four shard kernels** that the dispatch (and direct importers) actually reach: `bwmm_shard_on_block` / `bwmm_shard_on_I` (non-MX, TRN2) and their MX twins `bwmm_shard_on_block_mx` / `bwmm_shard_on_I_mx` (MXFP4/MXFP8, TRN3). These factor along two orthogonal axes — *what is sharded across the two NeuronCores* (the block axis vs. the intermediate `I` axis) and *the weight numeric format* (bf16/fp8 vs. MX block-scaled). Rather than four near-duplicate walkthroughs, the variants are presented as one table plus shared-mechanism prose: the MoE/MLP control skeleton is the same across all four; the deltas are the cross-core reduce (mandatory and per-block for I-shard, absent-per-block for block-shard) and the matmul primitive (`nc_matmul_mx` with E8M0 scales riding inside vs. plain `nc_matmul` with a post-matmul dequant multiply).

> **NOTE —** the readable `nkilib/core/moe/moe_cte` package is the kernel-library convenience entry, not the production caller. Neuronx-Distributed (NxD) and the HLO→NKI lowerer (`LowerToNKIKernelCC`) reach the production MoE-blockwise path through the *compiled* `neuronxcc.nki._private_kernels.blockwise_mm` extension module — a `.so`, not this Python. No caller of `moe_cte(` or `MoECTESpec` exists anywhere in the extracted neuronx-cc tree; NxD imports the leaf kernels directly. This page documents the algorithm as it reads in the shipped library source.

For reimplementation, the contract is:

- The **`MoECTESpec` / `MoECTEImplementation` dispatch**: the 7-member enum, the two sub-config dataclasses, `__post_init__` default-filling, the `if/elif`-chain dispatch body, and the five live vs. two dead targets.
- The **block-sharded blockwise algorithm** (`bwmm_shard_on_block`): the PING_PONG even/odd block→core map, the `BLOCK_PARALLEL_FACTOR=3` phase-split "double-buffering", the per-block 3-matmul FFN, and the two-plane epilogue reduce for TopK>1.
- The **I-sharded algorithm** (`bwmm_shard_on_I`): why splitting the intermediate `I` (contraction) dimension forces a mandatory **per-block cross-core reduce** (`sendrecv` + `tensor_tensor(add)`), the output-B-tile co-sharding "layout twist", the real static+dynamic-while hybrid, and the "dropping" layout (`N == E`, no in-kernel drop logic).
- The **MX numeric scheme** (both `_mx` kernels): x4-packed FP4/FP8 weights view-cast from int alternates, E8M0 per-32-element block scales consumed *inside* `nc_matmul_mx`, online `quantize_mx` of activations to FP8, and the quadrant-hole indirect-DGE scale gather.

| | |
|---|---|
| **Package** | `nkilib/core/moe/moe_cte/` |
| **Dispatch entry** | `moe_cte()` — `moe_cte.py:252` (`@nki.jit(mode="trace")`) |
| **Spec / enum** | `MoECTESpec` `:181`, `MoECTEImplementation` `:35` (7 members, 0–6) |
| **Live dispatch targets** | enums 0–4 (`shard_on_block`, `shard_on_i`, `shard_on_i_hybrid`, `shard_on_i_dropping`, `shard_on_block_mx`) |
| **Dead enum members** | enum 5 `shard_on_i_mx`, enum 6 `shard_on_i_mx_hybrid` — declared, leaves exist, never wired |
| **Variant leaves** | `bwmm_shard_on_block.py:72`, `bwmm_shard_on_I.py:87/327/2014`, `bwmm_shard_on_block_mx.py:71`, `bwmm_shard_on_I_mx.py:96/401` |
| **Shard axis** | 2 NeuronCores (LNC); `shard_id = nl.program_id(axis=0) ∈ {0,1}` |
| **HW targets** | enums 0–3 → TRN2 (bf16/fp8); enums 4–6 → TRN3/CoreV4 (MX) |
| **HW constants** | `TILE_SIZE=128`, `PSUM_SIZE=512`, `N_PSUM_BANKS=8`, `SB_QUADRANT_SIZE=32` (`moe_cte_utils.py:57-62`) |

---

## The Dispatch

### Purpose

`moe_cte()` is the single public entry. It accepts the full MoE tensor set plus a `MoECTESpec`, unpacks the (separately-passed) quantization scales, and forwards to exactly one variant leaf chosen by `spec.implementation`. It is a trace-mode NKI kernel (`@nki.jit(mode="trace")`, `moe_cte.py:252`) — it emits an HLO/NKI trace rather than executing eagerly, consistent with the prefill/context-encoding role.

### The Spec and Its Enum

The spec is a small composition dataclass (`moe_cte.py:181`): a required `implementation` plus two optional sub-config objects, of which exactly one is relevant per variant.

```c
@dataclass
class MoECTESpec(nl.NKIObject):                  // moe_cte.py:181
    implementation : MoECTEImplementation        // REQUIRED, no default
    shard_on_block : Optional[ShardOnBlockConfig] = None  // block family (enums 0,4)
    shard_on_I     : Optional[ShardOnIConfig]     = None  // I family (enums 1,2,3)

    def __post_init__():                         // :235 — default-fill the relevant sub-config
        if implementation in (shard_on_block, shard_on_block_mx):   // enums 0,4
            self.shard_on_block ??= ShardOnBlockConfig()
        elif implementation in (shard_on_i, shard_on_i_hybrid, shard_on_i_dropping): // 1,2,3
            self.shard_on_I ??= ShardOnIConfig()
        // NOTE: enums 5,6 are covered by NEITHER clause — no auto-fill, consistent
        //       with their being undispatched (see "Five Live, Two Dead").
```

The enum declares seven members (`moe_cte.py:35-55`), read verbatim:

| Enum | Value | Variant | Sub-config | HW | Dispatch |
|---|---|---|---|---|---|
| `shard_on_block` | 0 | block-sharded blockwise | `ShardOnBlockConfig` | TRN2 | **live** (`:348`) |
| `shard_on_i` | 1 | I-sharded baseline | `ShardOnIConfig` | TRN2 | **live** (`:377`) |
| `shard_on_i_hybrid` | 2 | I-sharded + static/dynamic loop | `ShardOnIConfig` | TRN2 | **live** (`:405`) |
| `shard_on_i_dropping` | 3 | I-sharded, dropping layout | `ShardOnIConfig` | TRN2 | **live** (`:435`) |
| `shard_on_block_mx` | 4 | block-sharded + MX | `ShardOnBlockConfig` | TRN3 | **live** (`:461`) |
| `shard_on_i_mx` | 5 | I-sharded + MX (static) | (none auto-filled) | TRN3 | **DEAD** — no branch |
| `shard_on_i_mx_hybrid` | 6 | I-sharded + MX + hybrid | (none auto-filled) | TRN3 | **DEAD** — no branch |

The two sub-configs (both `nl.NKIObject` dataclasses):

```c
@dataclass class ShardOnBlockConfig:             // moe_cte.py:98 — enums 0 & 4
    n_block_per_iter        : int = 1            // forwarded to enum 0 (but UNUSED in its body)
    block_sharding_strategy : BlockShardStrategy = PING_PONG   // enum 0 only
    n_static_blocks         : int = -1           // enum 4: -1 => auto from N,E
    n_dynamic_blocks        : int = 55           // enum 4: max blocks in dynamic loop

@dataclass class ShardOnIConfig:                 // moe_cte.py:134 — enums 1,2,3
    checkpoint_activation         : bool = False // save gate/up acts for bwd pass
    expert_affinity_multiply_on_I : bool = False // True=affinity on I (post-act); False=on H (post-down)
    num_static_block              : Optional[int] = None  // enum 2: None => auto = (N-E)
```

`QuantizationConfig` (`moe_cte.py:63`) holds `gate_up_proj_scale` / `down_proj_scale` (both `None` ⇒ no quant). It is a **separate kwarg**, not a `MoECTESpec` field — an asymmetry with the sharding sub-configs that *do* live on the spec.

### Algorithm — the dispatch body

```c
function moe_cte(hidden_states, expert_affinities_masked,           // moe_cte.py:252
                 gate_up_proj_weight, down_proj_weight,
                 token_position_to_id, block_to_expert,
                 block_size, spec, conditions=None, ...):
    print(f"spec: {spec}")                                          // :341 — DEBUG print (stdout noise)

    quant_cfg = quantization_config or QuantizationConfig()         // :344
    gate_up_proj_scale = quant_cfg.gate_up_proj_scale               // :345 — unpacked into two flat
    down_proj_scale    = quant_cfg.down_proj_scale                  // :346   scale tensors

    if   spec.implementation == shard_on_block:                     // :348
        print("bwmm_shard_block_branch")                            // :349 — DEBUG print
        cfg = spec.shard_on_block or ShardOnBlockConfig()           // belt-and-braces re-default
        return bwmm_shard_on_block(..., n_block_per_iter=cfg.n_block_per_iter,
                                        block_sharding_strategy=cfg.block_sharding_strategy,
                                        down_activations=down_activations)   // NO conditions, NO gate_up_act_T
    elif spec.implementation == shard_on_i:                         // :377
        print("bwmm_shard_I_branch")                                // :378
        cfg = spec.shard_on_I or ShardOnIConfig()
        return blockwise_mm_baseline_shard_intermediate(..., checkpoint_activation=cfg.checkpoint_activation,
                                        expert_affinity_multiply_on_I=cfg.expert_affinity_multiply_on_I)
    elif spec.implementation == shard_on_i_hybrid:                  // :405
        print("bwmm_shard_I_hybrid_branch")                         // :406
        cfg = spec.shard_on_I or ShardOnIConfig()
        return blockwise_mm_baseline_shard_intermediate_hybrid(..., conditions=conditions,
                                        num_static_block=cfg.num_static_block,
                                        gate_up_activations_T=..., down_activations=...)
    elif spec.implementation == shard_on_i_dropping:               // :435
        cfg = spec.shard_on_I or ShardOnIConfig()
        return blockwise_mm_shard_intermediate_dropping(..., expert_affinity_multiply_on_I=cfg.expert_affinity_multiply_on_I)
    elif spec.implementation == shard_on_block_mx:                 // :461
        cfg = spec.shard_on_block or ShardOnBlockConfig()
        return bwmm_shard_on_block_mx(..., conditions=conditions,
                                        n_static_blocks=cfg.n_static_blocks,
                                        n_dynamic_blocks=cfg.n_dynamic_blocks,
                                        gate_up_activations_T=..., down_activations=...)
    // *** FILE ENDS AT LINE 489 — no elif shard_on_i_mx, no elif shard_on_i_mx_hybrid,
    //     NO final else, NO raise. spec.implementation in {5,6} falls off the end => returns None. ***
```

The common kwarg set threaded to **all five** live branches: `hidden_states`, `expert_affinities_masked`, `gate_up_proj_weight`, `down_proj_weight`, `block_size`, `token_position_to_id`, `block_to_expert`, the two optional biases, the two unpacked `*_proj_scale` tensors, `activation_function`, `skip_dma`, `compute_dtype`, `is_tensor_update_accumulating`, `expert_affinities_scaling_mode`, and the four gate/up clamp limits. Threaded **conditionally**: `conditions` and `gate_up_activations_T` go only to enum 2 (hybrid) and enum 4 (block_mx); `down_activations` goes to enums 0, 2, 4 (not 1, not 3).

### Five Live, Two Dead

> **GOTCHA —** the enum advertises seven implementations and the `moe_cte()` docstring "Pseudocode" block (`:325-339`) lists all seven, but the shipped dispatch body implements **only five** (enums 0–4). Calling `moe_cte()` with `spec.implementation ∈ {shard_on_i_mx, shard_on_i_mx_hybrid}` matches no `elif`, hits no `else`, and **silently returns `None`** — a footgun with no diagnostic. The two MX-I leaf kernels *do* exist with exactly the names the docstring cites (`blockwise_mm_shard_intermediate_mx` at `bwmm_shard_on_I_mx.py:96`, `..._mx_hybrid` at `:401`), but `moe_cte.py` never imports them (the live import at `:27-31` pulls only the three non-MX I-shard leaves), so no dispatch branch can reach them.

The death is exhaustive. Across all three wheels, the only textual occurrences of `blockwise_mm_shard_intermediate_mx` / `..._mx_hybrid` are (a) their own two `def` lines in `bwmm_shard_on_I_mx.py`, and (b) `moe_cte.py:337` / `:339`, which sit **inside the docstring Pseudocode block** (`:325-340`), not in code. There is zero real import, zero real call; no test, no `_private_kernels` `.so` source-reference, and (since NxD is a separate pip package not bundled inside the neuronx-cc wheel) no in-artifact caller. The two MX-I variants are reachable **only** by a direct deep import of the leaf function.

A second tell that the MX-I path is unproductionised: the static MX-I leaf `blockwise_mm_shard_intermediate_mx` defaults `expert_affinities_scaling_mode = PRE_SCALE` (`bwmm_shard_on_I_mx.py:113`), but `compute_one_block_mx` executes `kernel_assert(False, "PRE_SCALE mode not implemented")` whenever `scaling_mode == PRE_SCALE` (`:914-915`). So even if a caller imported enum 5 directly and used its own default, it would **assert-fail**. Only `POST_SCALE` works there; the hybrid (enum 6) correctly defaults `POST_SCALE` and is internally consistent.

---

## The Shard Kernels — Shared Skeleton

All four reached kernels share one MoE/MLP control skeleton; only two things differ across them. This section establishes the common machinery; the next is the four-variant table of deltas.

### The blockwise contract and the per-block FFN

Tokens are pre-routed into `N` blocks of `B` tokens, all routed to one expert `block_to_expert[n]`. Per block the math is a dense expert FFN over a `[B, H]` token tile:

```c
// per block n, expert e = block_to_expert[n]:
block_hidden = gather(hidden_states, token_position_to_id[n*B : (n+1)*B])  // indirect-DGE gather
gate = block_hidden @ Wg[e]                          // [B,I]  gate projection
up   = block_hidden @ Wu[e]                          // [B,I]  up   projection
inter = act(gate) ⊙ up                               // SiLU/Swish/GELU elementwise, then multiply
out  = inter @ Wdown[e]                              // [B,H]  down projection
out *= expert_affinity[token]                        // router weight scale
scatter_add(output, token_ids, out)                  // accumulate to token rows (TopK>1)
```

`gate_up_proj_weight[E,H,2,I_TP]` packs gate at index 0, up at index 1 on axis 2; `down_proj_weight[E,I_TP,H]`. `token_position_to_id[N*B]` is block-major with padding id = `T` (the `+1` sink row in `hidden_states[T+1,H]` and `expert_affinities_masked[(T+1)*E,1]`). The affinity address is `token_id*E + block_expert`, indirect-loaded from the flattened router weights.

### Two NeuronCores and `is_tensor_update_accumulating`

Every kernel runs SPMD across two LNC cores: `shard_id = nl.program_id(axis=0) ∈ {0,1}`, `num_shards = nl.num_programs(axis=0) = 2`. With TopK > 1, a single token can be hit by blocks routed to both cores, so `is_tensor_update_accumulating=True` (the default) turns the scatter into a read-modify-write accumulate and adds a cross-core combine. *How* that combine is structured is the central divergence between block-shard and I-shard (below). See [SPMD programming model](spmd-programming-model.md) for `program_id` / `num_programs` semantics and [ISA compute intrinsics](isa-compute-intrinsics.md) for `nc_matmul` / `nc_transpose` / `dma_compute` / `sendrecv`.

### The PE matmul mapping

`nisa.nc_matmul` computes `dst = stationaryᵀ @ moving`, contracting over the 128-row PE partition dimension (stationary = weights resident in the 128×128 systolic array, moving = streamed IFMAP). The gate/up projection sets stationary = the `H×I` weight, moving = `Hᵀ`-transposed tokens, output partition = `I`, free = `B`; the down projection swaps roles (stationary = the `[I,B]` activations, moving = the `[I,H]` down weight) so the result lands token-major `[B,H]` ready for the scatter. The K-loop issues many `nc_matmul` into one PSUM tile (K = `H` for gate/up, K = `I_TP` for down); the backend groups those into a hardware accumulate chain — see [ISA compute intrinsics](isa-compute-intrinsics.md). All four kernels emit hidden transposes via `nc_transpose` against a shared identity matrix into PSUM, then copy back to SBUF.

---

## The Four Shard Variants — One Table

The four reached kernels are the cross product of two axes. The MoE skeleton above is shared; the entire delta is captured below.

| Mechanism | `bwmm_shard_on_block` (enum 0) | `bwmm_shard_on_I` (enums 1/2/3) | `bwmm_shard_on_block_mx` (enum 4) | `bwmm_shard_on_I_mx` (enums 5/6, **dead**) |
|---|---|---|---|---|
| **File / leaf** | `bwmm_shard_on_block.py:72` | `bwmm_shard_on_I.py:87/327/2014` | `bwmm_shard_on_block_mx.py:71` | `bwmm_shard_on_I_mx.py:96/401` |
| **Shard axis** | N-block dim | `I_TP` intermediate dim (`I_TP_sharded = I_TP//2`) | N-block dim | `I_TP` intermediate dim |
| **What each core owns** | whole expert blocks | same block, its `I/2` slice + half the B-tiles | whole expert blocks | same block, its `I/2` slice + half the B-tiles |
| **Block→core map** | PING_PONG: `block_idx = 2*linear_idx + shard_id` (even/odd) | both cores co-process the *same* block | PING_PONG (same as enum 0) | both cores co-process the *same* block |
| **Hidden load** | per-core blocks | **unsharded** (each core loads whole `[B,H]`) | per-core blocks | **unsharded** |
| **Gate/up weight DMA** | whole `I_TP` | I-sliced `[…I_TP_offset : +I/2]` | whole `I_TP` (MX-packed) | I-sliced (MX-packed) |
| **Cross-core reduce** | none per block; **one epilogue** plane sum | **mandatory per block**: `sendrecv` + `tensor_tensor(add)` | none per block; one epilogue plane sum | **mandatory per block**: `sendrecv` + `tensor_tensor(add)` |
| **Output tensor** | `[T,2,H]` (per-core plane), reduced in epilogue | `[T+1,H]` single plane (reduce already done) | `[2,T,H]` (per-core plane) | `[T,H]` single plane |
| **"Double-buffer"** | `BLOCK_PARALLEL_FACTOR=3` phase split (load/compute) | straight `sequential_range`, no 3-deep | static + dynamic-while padded loop | static + dynamic-while |
| **Dynamic-while** | absent (docstring promises it; body is static-only) | **real** in hybrid (`nl.dynamic_range`) | **real** | **real** in hybrid |
| **Matmul primitive** | `nc_matmul` (plain), then post-matmul scalar dequant multiply | `nc_matmul` (plain) + post-matmul dequant | `nc_matmul_mx` (E8M0 scales inside) | `nc_matmul_mx` |
| **Weight dtype** | bf16 / fp8 | bf16 / fp8 | x4-packed FP4/FP8 (`float4_e2m1fn_x4` / `float8_e4m3fn_x4` / `e5m2_x4`) | x4-packed FP4/FP8 |
| **Dequant scales** | per-column `[E,1,2*I_TP]` / `[E,1,H]` scalar | per-channel scalar | E8M0 `[E,16,…]` uint8 block scales | E8M0 block scales |
| **Variants in file** | 1 | 3 (baseline / hybrid / dropping) | 1 | 2 (static / hybrid) — **no dropping** |
| **Default scaling mode** | `POST_SCALE` | baseline `PRE_SCALE`, hybrid `POST_SCALE`, dropping `PRE_SCALE` | `POST_SCALE` | static `PRE_SCALE` (latent bug), hybrid `POST_SCALE` |
| **Best when** | many blocks (load-balance by count) | few/large blocks or large `I` (split FFN width) | many blocks, TRN3 | few/large blocks, TRN3 |

The rest of the page explains each distinguishing mechanism once, in depth.

---

## Mechanism: Block Sharding and PING_PONG (enums 0, 4)

### The even/odd block→core map

Block-shard distributes whole blocks across the two cores. The shipped strategy is `PING_PONG` (`HI_LO` is coded in helpers but blocked by `kernel_assert`, so it is dead at runtime). The map is interleaved:

```c
// bwmm_shard_on_block.py — both the load phase (:333) and compute phase (:516):
block_idx = 2 * linear_idx + shard_id      // linear_idx = outer_block_iter*3 + inner_block_iter
// helpers shard_strat2blk_idx (:1782) / shard_strat2new_blk_idx_offset (:1804):
//   PING_PONG: shared = 2*(outer*3 + inner);  offset = shard_id
//   => shard 0 owns blocks {0,2,4,...}, shard 1 owns {1,3,5,...}
if block_idx < N:  ...                      // tail guard (last group may be partial)
```

Each core contracts the **full** `I_TP` for its blocks — an expert FFN is computed whole on one core — so there is no per-block cross-core dependency. The only combine is the TopK overlap (a token hit by blocks on both cores), handled once in the epilogue. `n_blocks_per_shard = ceil(N/2)`; the outer loop trips `n_shard_tile_count = ceil(n_blocks_per_shard / 3)` times.

### `BLOCK_PARALLEL_FACTOR=3` — the real "double-buffering"

> **QUIRK —** the "PING_PONG double-buffering" is **not** an `i%2` two-buffer SBUF swap. `BLOCK_PARALLEL_FACTOR=3` (`bwmm_shard_on_block.py:47`) processes blocks in groups of three with the load fully separated from the compute: PHASE-1 issues *all* token-gathers and hidden-transposes for the three blocks `{3o, 3o+1, 3o+2}` (`:331-511`), then PHASE-2 issues *all* weight-loads and matmuls for the same three (`:513-1038`). Three independent SBUF buffer sets (`block_hidden_states_lst[0..2]`) let block g+1/g+2's loads retire while block g computes — a three-stage software pipeline expressed structurally.

This is the frontend producer of the load/compute split that the backend's `SeparateLoadAndCompute` pass software-pipelines: the frontend pre-separates and triple-buffers; the backend assigns load vs. compute stages and drops the inter-stage barrier, overlapping `DMA(load g+1)` with `PE(compute g)`. The innermost gate/up matmul also drains its PSUM one I-tile behind the matmul (`:615-666`) — the same "compute N while draining N−1" idea at the finest grain. See [dynamic for-loop](../front/dynamic-for-loop.md) and the [matmul worked example](../front/worked-example-matmul.md) for the loop-structuring background.

> **GOTCHA — `bwmm_shard_on_block`'s loop is static only, whatever its docstring says.** The docstring (`:15`, `:101-102`) promises a "static + dynamic / hybrid early-exit" loop. The shipped body sets `use_dynamic_while=False` and `NUM_STATIC_BLOCKS=N`, carries a single `# STATIC LOOP`, and has no `conditions` argument in its signature at all. The hybrid path exists only in the `_I` and `_mx` siblings. Two related vestiges live in the same leaf: `n_block_per_iter` is forwarded by the dispatch but never read, and `down_activations` is reset to `None` mid-body, which makes activation-checkpointing here a no-op.

### The two-plane epilogue reduce

Because each core writes its own H-plane `output[:, shard_id, :]` of an `[T,2,H]` tensor (enum 0; `[2,T,H]` for enum 4), the TopK overlap is resolved once, after the block loop, by summing the two planes:

```c
core_barrier(output, (0,1))                                   // bwmm_shard_on_block.py:1057 — both cores rendezvous
// shard 0 reduces tiles [0:nc0), shard 1 reduces [nc0:reduce_tiles) — split the work
function reduce_outputs(output, zeros, num_tiles, ...):        // :1682
    for tile in num_tiles:
        nisa.dma_compute(srcs=[output[...,0,:], output[...,1,:]],  // :1702 — sum the two shard planes
                         dst=tmp, scales=[1.0,1.0], reduce_op=nl.add)
        nisa.dma_copy(output[...,0,:], tmp)                   // plane 0 := Σ_shards
        nisa.dma_copy(output[...,1,:], zeros)                // zero plane 1
// final result lives in output[:,0,:]
```

---

## Mechanism: Intermediate (I) Sharding and the Cross-Core Reduce (enums 1/2/3)

### Why I-shard needs a per-block reduce

I-shard splits the **contraction dimension itself**: each core owns `I_TP_sharded = I_TP // 2` columns of the intermediate. Both cores load the *whole* `[B,H]` hidden (hidden is unsharded — only the I-projection is sharded), each computes the gate/up and down projections over only its `I/2` slice, so each core's `[B,H]` down-proj result is a **partial sum over half the intermediate**. Unlike block-shard (where a block's expert FFN is whole on one core), every block's down-proj here *must* be reduced across cores before it is a valid `[B,H]`. The kernel additionally co-shards the **output B-tiles** (`NUM_B_TILES_SHARDED = NUM_B_TILES // 2`): each core owns half the output rows.

### The sendrecv + add reduce, and "dropping-as-layout"

> **QUIRK — the layout twist.** Each core computes *full-B* partials over its `I/2`, but *owns* only half the output B-tiles. So for the B-tiles it does **not** own, it ships its partial to the peer; for the B-tiles it **does** own, it receives the peer's `I/2` partial and adds. Net: every output B-tile is reduced exactly once, on its owner core, and `out[b,h] = (core0's Σ_{I/2}) + (core1's Σ_{I/2})`.

```c
// compute_down_proj_shard_on_intermediate — bwmm_shard_on_I.py:1681
N_B_TILES_OFFSET = NUM_B_TILES_SHARDED * shard_id           // this core owns B-tiles [OFFSET : OFFSET+sharded)
for b in range(NUM_B_TILES_SHARDED):
    sendrecv(                                               // :1684 — nki.isa.sendrecv (import :22)
        src = block_new_lst[b + NUM_B_TILES_SHARDED*(1-shard_id)],  // MY copy of the PEER's B-tile partial
        dst = recv_sbuf[b],
        send_to_rank   = 1 - shard_id,
        recv_from_rank = 1 - shard_id,
        pipe_id = 0)
    nisa.tensor_tensor(                                     // :1692 — bf16/io_dtype ADD
        data1 = block_new_lst[b + N_B_TILES_OFFSET],        // MY B-tile, MY I-half partial
        data2 = recv_sbuf[b],                               // peer's I-half partial of MY B-tile
        op = nl.add, dst = recv_sbuf[b])
// then: optional down_activations checkpoint (BEFORE affinity), then affinity + block_old accumulate
```

> **NOTE —** the reduce is a **pure bf16 partial-sum exchange**; any dequant (per-channel fp8 scale, or — in the MX twin — the E8M0 block scale) was already folded into `block_new_lst` *before* the `sendrecv`. Nothing quantized crosses the core boundary. The docstring advertises an `all_reduce_sum`; the shipped form is that all-reduce specialised to two ranks — accurate in spirit, but the literal `all_reduce` primitive is not used.

Because the reduce already combines the two cores per block, I-shard writes a **single** `[T+1,H]` output plane (no per-core planes), with a per-block `core_barrier` serialising the accumulating read-modify-write. The reference baseline (enum 1, `:87`) is a fully static `for block_idx in sequential_range(N)` over all blocks.

The **"dropping"** variant (enum 3, `bwmm_shard_on_I.py:2014`) is the same compute wrapped in a different memory layout — it is **not** an in-kernel capacity/drop/mask:

> **GOTCHA — "dropping" names a layout, not a runtime drop.** The dropping leaf contains no capacity factor, no overflow test, and no drop mask. Token dropping — capacity-overflow discard — happens upstream, in the router that builds `token_position_to_id` and `expert_affinities_masked`; this kernel only consumes the already-capacity-bounded layout. What the name marks in-kernel is the case `N == E`, one block per expert, so `block_idx == expert_idx`: the kernel never even reads `block_to_expert`, it memsets `block_expert = block_idx` (`:1906-1907`). Each block is one full per-expert capacity (`B >= 1024`), which is why it B-tiles into `MAX_BLOCK_TILE_SIZE=1024` chunks. Activation-checkpointing is always on, this being a training layer.

### The real hybrid (enum 2)

Unlike block-shard's doc-only static body, the I-shard hybrid (`bwmm_shard_on_I.py:327`) emits a genuine data-dependent dynamic while-loop:

```c
// after a static prefix of NUM_STATIC_BLOCKS = (N-E) rounded to a multiple of num_shards:
configs.use_dynamic_while = True
cond = nisa.tensor_reduce(add, conditions[N+1]) -> register_load(cond_reg)  // runtime live-block count
block_idx_sbuf = memset(NUM_STATIC_BLOCKS)                                  // live SBUF int32
for _ in nl.dynamic_range(NUM_STATIC_BLOCKS, cond_reg):                     // bwmm_shard_on_I.py:573
    compute_one_block(block_idx_sbuf, dynamic_loaders=True)                  // scalar-offset DMA loaders
    core_barrier(output, (0,1))
    block_idx_sbuf = tensor_scalar(block_idx_sbuf + 1)
    core_barrier(block_idx_sbuf, (0,1))
```

This optimizes padded variable-length sequences: guaranteed-present blocks run unrolled in the static prefix, padded/maybe-empty blocks in a runtime-gated dynamic loop. See [dynamic for-loop](../front/dynamic-for-loop.md) for `nl.dynamic_range` semantics.

> **NOTE — the static-only finding is block-shard's alone.** `bwmm_shard_on_block`'s docstring is the one that overstates. The hybrids in `bwmm_shard_on_I` (enum 2) and `bwmm_shard_on_I_mx` (enum 6) really do emit dynamic-while loops, so the family as a whole is not static-only.

---

## Mechanism: The MX Numeric Scheme (enums 4, 5/6)

The MX kernels are the MX twins of the non-MX ones: **the MoE/MLP skeleton is identical** (block loop, ping-pong or I-shard, dynamic-while, indirect-DGE gather, affinity scale, accumulate-scatter). The entire delta is in the projection inner loops and weight/scale handling. The format is OCP MX (MXFP4/MXFP8): per-32-element blocks carry an E8M0 (8-bit exponent, zero mantissa) scale plus FP4/FP8 mantissas. MX lowers to CoreV4-only ISA, so these kernels are TRN3/gen4 — enforced both by the lowering and by the dtypes the readable kernel asks for (`_q_height=8 × _q_width=4 = 32` OCP block; `float4_e2m1fn_x4`; uint8 E8M0 scale). See [MX microscaling numerics](../numerics/mx-microscaling.md) for the format details.

### x4-packed weights, view-cast from int

torch/xla can't carry MX tensors, so weights arrive as alternative int dtypes and the kernel **view-casts** (reinterpret, no data copy) to the MX dtype:

```c
// moe_cte_mx_utils.py:61 — _ALTERNATIVE_DTYPE_TO_MXFP
{ uint16/int16 -> float4_e2m1fn_x4 (MXFP4),   uint32/int32 -> float8_e4m3fn_x4 (MXFP8) }
// convert_to_mxfp_dtype (:74): gate/up viewed first, down FORCED to the same MX dtype.
```

The `_x4` is the HW x4-unpack geometry: each MX element holds 4 packed FP4/FP8 values along the contraction axis, so the PE H-contraction tile is `_pmax * _q_width = 128 * 4 = 512` per H512 tile.

### E8M0 scales ride inside `nc_matmul_mx` — no post-matmul dequant

> **QUIRK —** the non-MX kernels do a plain `nc_matmul` then a per-output-channel scalar dequant multiply (`tensor_tensor(op=multiply, data2=down_scale[:,idx,:])`). The MX kernels do **neither** post-multiply — the E8M0 per-32-element block scales are bound *as operands of the matmul itself*:

```c
// gate_up_projection_mx_shard_H.py:160 — the 5-operand MX matmul
nisa.nc_matmul_mx(
    dst              = out_psum,                 // bf16 PSUM (already dequantized)
    stationary       = weight_qtz_tv[H512 tile, I-slice],   // MX-x4 weights
    moving           = hidden_qtz_sb[H512 tile, BxS-slice], // fp8 activations
    stationary_scale = weight_scale_tv[same slice],         // uint8 E8M0
    moving_scale     = hidden_scale_sb[same slice])         // uint8 E8M0
// down side (down_projection_mx_shard_H.py:182/325) swaps which operand is the
// (online-quantized) activation: inter stationary, weight moving — but BOTH carry E8M0 scales.
```

The PE dequantizes internally and emits bf16. This is precisely why the I-shard MX cross-core reduce is still pure bf16: by the time `block_new` reaches the `sendrecv`, the E8M0 scales were consumed two ops earlier — they are intra-core and never cross the boundary.

### Online activation quantize and the quadrant-hole scale gather

MX matmul cannot take a raw bf16 IFMAP, so activations are online-quantized to FP8 via `nisa.quantize_mx` (3 operands: `src` bf16, `dst` fp8-x4, `dst_scale` E8M0; one E8M0 per 8×4=32-element block):

```c
// moe_cte_mx_utils.py:767 — quantize hidden states (and likewise the gate*up intermediate before down-proj)
nisa.quantize_mx(src=block_hidden_states_T[…,:128] (bf16),
                 dst=hidden_qtz_sb[…,:32] (float8_e4m3fn_x4),     // dst free dim is 1/4 of src (4:1 x4 pack)
                 dst_scale=hidden_scale_sb[…,:32] (uint8 E8M0))
```

The weight scales are folded `E*16` on the partition dim (the `16 = 128 // 8` scale-partitions per expert), so a non-contiguous indirect-DGE index vector is built per block-expert by `_generate_expert_index_vector` (`moe_cte_mx_utils.py:948`): four valid scale rows per 32-partition quadrant with `-1` (OOB→skip) in the holes, offset by `16*block_expert`. That index becomes the `vector_offset` of an `oob_mode.skip` `dma_copy` — the "central MX trick" for gathering the right E8M0 block per expert.

> **NOTE —** the online `quantize_mx` only ever targets `float8_e4m3fn_x4` (it never online-quantizes to FP4). FP4 appears **only** as a pre-quantized *weight* dtype loaded from HBM. The two MX kernels delegate their projections to `gate_up_projection_mx_*` / `down_projection_mx_*` (under `mlp/mlp_tkg/`), which the non-MX kernels inline.

---

## Audit / Smells

These are truthful code-quality observations, all read directly in the shipped source:

- **`None`-returning footgun** — `moe_cte()` with `spec.implementation ∈ {5,6}` returns `None` silently (no `else`/`raise`). (`moe_cte.py` ends at `:489`.)
- **Stdout noise** — bare `print()` at `moe_cte.py:341,349,378,406` fire on every CTE invocation; a commented-out `kernel_assert` + bare `print` warning in the I-shard hybrid (`bwmm_shard_on_I.py:551-553`).
- **MX-I static latent bug** — `bwmm_shard_on_I_mx.py:113` defaults `PRE_SCALE`, which `:914-915` asserts unimplemented (enum 5 would assert-fail on its own default).
- **block-shard checkpoint no-op** — `down_activations` is nulled mid-body in `bwmm_shard_on_block.py`, so its checkpoint path never fires; doc/code mismatch on `checkpoint_activation` for the dropping dispatch branch.
- **Dead `HI_LO`** — fully coded in `shard_strat2*` helpers but blocked by `kernel_assert`; only `PING_PONG` runs.
- **Doubled `kernel_assert`** — the PING_PONG-only check is written twice (`bwmm_shard_on_block.py:184-189`), identical message.
- **API asymmetry** — `QuantizationConfig` is a separate kwarg, not a `MoECTESpec` field, unlike the sharding sub-configs.

---

## Related Components

| Name | Relationship |
|---|---|
| `moe_cte_utils.py` | `BlockShardStrategy`, `SkipMode`, `InputTensors`/`Configs`, HW constants, shared `compute_intermediate_states` / `calculate_expert_affinities` |
| `moe_cte_mx_utils.py` | MX helpers: `convert_to_mxfp_dtype`, `quantize_block_hidden_state_T`, `_generate_expert_index_vector` (the D-O29 "mxfp_load_utils" — no such file exists; helpers live here) |
| `mlp/mlp_tkg/gate_up_projection_mx_shard_H.py`, `down_projection_mx_shard_H.py` | the `nc_matmul_mx` projection inner loops the MX kernels delegate to |
| `_private_kernels.blockwise_mm` (`.so`) | the **production** compiled MoE-blockwise path NxD actually calls — *not* this readable library entry |
| `moe_cte_torch.py` | implementation-agnostic PyTorch reference (no `MoECTESpec` arg) for numerical golden checks |

## Cross-References

- [MoE decode (TKG)](moe-decode-tkg.md) *(planned)* — the decode-phase sibling; this page is the prefill/context (CTE) path
- [nkilib infrastructure](nkilib-infrastructure.md) *(planned)* — BufferManager / ModularAllocator / TiledRange / TensorView these kernels build on
- [Production kernel inventory](production-kernel-inventory.md) — where MoE-CTE sits in the nkilib kernel catalog
- [ISA compute intrinsics](isa-compute-intrinsics.md) — `nc_matmul`, `nc_matmul_mx`, `nc_transpose`, `quantize_mx`, `dma_compute`, `sendrecv`, the accumulate-group semantics
- [SPMD programming model](spmd-programming-model.md) — `program_id` / `num_programs`, the 2-core LNC shard model, `core_barrier`
- [Dynamic for-loop](../front/dynamic-for-loop.md) — `nl.dynamic_range` and the static+dynamic hybrid loop structure
- [Matmul worked example](../front/worked-example-matmul.md) — the PE stationary/moving mapping and PSUM accumulation in a full trace
- [MX microscaling numerics](../numerics/mx-microscaling.md) — OCP MXFP4/MXFP8, E8M0, x4-packing details
