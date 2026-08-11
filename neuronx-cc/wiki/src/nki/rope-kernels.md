# RoPE Kernels

> *All symbols, line numbers, and formulas on this page apply to `neuronx_cc` **2.24.5133.0+58f8de22** (cp310; the cp310/311/312 wheels are byte-identical for these files). Sources are the readable NKI Python under `nkilib/core/embeddings/` and `nkilib/core/attention/`, each a binary-derived wheel artifact, cross-checked against the compiled `neuronxcc/nki/_private_kernels/RoPE.cpython-310-…-.so` twin.*

## Abstract

Rotary Position Embedding (RoPE) rotates query and key vectors by a position-dependent angle so that attention scores depend only on relative position. neuronx-cc ships RoPE in four places, and the single most important fact about all of them is that **they compute the same rotation**: the HuggingFace / GPT-NeoX *split-half* convention `rotate_half([x1, x2]) = [-x2, x1]`, `y = x·cos + rotate_half(x)·sin`. There is **no** GPT-J interleaved-adjacent-pair rotation kernel anywhere in the shipped tree. The word "interleaved" appears only as a *physical memory layout* of the head dimension (`contiguous_layout=False` in `rope.py`), decoupled from the math and de-interleaved before the rotation runs.

The four sites differ by tensor layout, I/O, and how cos/sin arrive — not by rotation math. `rope.py` (`RoPE`) is the NeuronCore-native standalone, head_dim on the partition axis, half-width cos/sin loaded from HBM. `rope_hf.py` (`rope_hf`) is the HuggingFace-layout public kernel, head_dim on the free axis, full-width or packed `[cos|sin]` cache, and the *only* site with a backward pass. The CTE/prefill path fuses RoPE inline in the QKV projection's PSUM→SBUF copy (`qkv_cte.py`). The TKG/decode path (`attention_tkg.py`) fuses RoPE and **generates cos/sin on-device** from `inv_freq` and position IDs.

That on-device generation is the second headline. The Activation engine has a hardware *Sine* unit but **no hardware Cosine**, so `_rope()` synthesizes cos through sine: `cos(θ) = sin(θ + π/2)`, realized as a constant added to the angle before the same `nl.sin` activation runs. Both sin and cos first reduce the angle into `[-π, π]` (the Sine unit's legal range) with an explicit `round`-based modulo. Everywhere in the library, the *angle base* — theta (conventionally 10000), `inv_freq = 1/theta^(2i/d)`, and every rope-scaling variant (NTK / YaRN / LongRoPE / linear / dynamic) — is computed **framework-side** (NxD host) and handed to the kernel as a ready cos/sin table or an `inv_freq` vector. None of it lives in these kernels.

For reimplementation, the contract is:

- The **one rotation convention** (split-half / `rotate_half`) and the per-half algebra for forward and backward.
- The **layout vs math** distinction: `rope.py`'s interleaved↔contiguous relayout (strided DMA and the permutation-matmul) is a memory transform that leaves the rotation untouched.
- The **cos-via-sin trick** byte-for-byte: the `+π/2` cos offset, the `((θ + c) % 2π) − π` range reduction, and the `round(x/y − 0.5)` modulo.
- The **cos/sin provenance boundary**: precomputed-and-DMA'd vs on-device-generated, and that all theta/scaling is framework-side.
- The **nisa primitive sequences** (`tensor_copy` / `tensor_tensor` / `tensor_scalar` / `activation` / `nc_matmul`) each site emits.

| | |
|---|---|
| **Standalone kernel (NeuronCore layout)** | `RoPE` — `nkilib/core/embeddings/rope.py:27` (`@nki.jit`) |
| **SBUF compute helper (fusion entry)** | `RoPE_sbuf` — `rope.py:173` |
| **HuggingFace-layout kernel (+ backward)** | `rope_hf` — `nkilib/core/embeddings/rope_hf.py:29` (`@nki.jit`) |
| **HF per-head rotation** | `_apply_rope_single` — `rope_hf.py:269` |
| **On-device cos/sin generator** | `_rope` — `nkilib/core/attention/attention_tkg.py:3175` |
| **cos-via-sin** | `cos(θ)=sin(θ+π/2)`; `nl.sin` for both — `attention_tkg.py:3222,3238` |
| **Range-reduction modulo** | `_modulo` — `attention_tkg.py:3248`, `q=round(x/y−0.5)`, `res=x−q·y` |
| **TKG fused-RoPE apply** | `_apply_rope` / `_perform_rope` — `attention_tkg.py:3089` / `1506` |
| **CTE fused RoPE** | `_copy_psum_to_sbuf_apply_rope_and_bias` — `nkilib/core/qkv/qkv_cte.py` |
| **Rotation convention** | HF split-half `rotate_half([x1,x2])=[-x2,x1]` everywhere — no GPT-J |
| **Rotated operands** | Q and K only; V copied unrotated |
| **Compiled twin** | `neuronxcc/nki/_private_kernels/RoPE.cpython-310-…-.so` |

---

## The Single Rotation Convention

### Purpose

Establish that every shipped RoPE site computes the identical rotation, so a reimplementer writes the math once. The convention is HuggingFace's `rotate_half`, which is the GPT-NeoX block-split form, not the GPT-J adjacent-pair form.

### Algorithm

Let `x` be a head vector of dimension `d`, split into halves `x1 = x[:d/2]`, `x2 = x[d/2:]`. RoPE is:

```c
// rotate_half(x) = cat(-x2, x1)            // rope_hf_torch.py:22-25
// y = x*cos + rotate_half(x)*sin
// Written per-half (this is what every kernel emits):
out[:d/2] = x1*cos[:d/2] - x2*sin[:d/2];    // rope.py:185 "out[even]=x[even]*cos - x[odd]*sin"
out[d/2:] = x2*cos[d/2:] + x1*sin[d/2:];    // rope.py:186 "out[odd]=x[odd]*cos + x[even]*sin"
```

cos and sin satisfy `cos = cat(freqs, freqs)` and `sin = cat(freqs, freqs)` — the two halves are **identical** (the per-position frequency vector, repeated). Only half of cos/sin is therefore unique, which several sites exploit to carry half-width tables (see [cos/sin Provenance](#cossin-provenance--precomputed-vs-on-device)).

> **GOTCHA —** the `rope.py` docstring (`rope.py:185-186`) names the halves "even" and "odd". This is *not* the GPT-J even/odd-index interleaving. "even" means the **first half** `x[:d/2]`, "odd" the **second half** `x[d/2:]`; the code rotates `x_in_sb[:half_d]` against `x_in_sb[half_d:]` (`rope.py:226-252`). Reading "even/odd" as element interleaving is the single easiest way to reimplement the wrong rotation.

### Function Map

Verbatim formula evidence across all four sites — all the same rotation:

| Site | Source | Confidence |
|---|---|---|
| `RoPE_sbuf` docstring + ops | `rope.py:185-186` / `:251-252` | CERTAIN |
| `_apply_rope_single` (HF) | `rope_hf.py:288` "y=[x1,x2]·[cos1,cos2]+[-x2,x1]·[sin1,sin2]" | CERTAIN |
| `_apply_rope` (TKG) | `attention_tkg.py:3100-3106` `_rotate_half(x)=cat((-x2,x1)); x*cos+_rotate_half(x)*sin` | CERTAIN |
| `_gen_rope_coeff`/`_apply_rope` torch ref | `attention_tkg_torch.py:289-303` same `_rotate_half` | CERTAIN |
| CTE fused | `qkv.py` / `qkv_cte.py` "RoPE([X1,X2])=[X1,X2]·cos+[-X2·sin, X1·sin]" | CERTAIN |

> **GOTCHA — `rope.py` and `rope_hf.py` are not two rotation conventions.** Both compute split-half, verbatim, across all five sites above. `rope.py`'s `contiguous_layout` flag selects a memory **layout**, not a rotation. There is no interleaved-adjacent-pair (GPT-J) rotation kernel anywhere in the shipped tree — the literal `rotate_half([x0,x1],[x2,x3])`-style pair rotation appears in no path.

---

## `RoPE` — NeuronCore-Layout Standalone

### Purpose

`RoPE` (`rope.py:27`) is the standalone HBM-in/HBM-out kernel for the NeuronCore-native layout `[d_head, B, n_heads, S]`, where **head_dim sits on the SBUF partition axis**. It is invoked by the legacy `_pre_prod_kernels/llama3_transformer.py` full-transformer path; its SBUF helper `RoPE_sbuf` is the entry the experimental TKG megakernel (`attention_block_tkg.py:905`) calls when it defers RoPE to a standalone step.

### Entry Point

```text
RoPE (@nki.jit, rope.py:27)                       ── HBM I/O + optional LNC shard
  ├─ _validate_rope_inputs (rope.py:374)          ── shape/dtype guards
  ├─ [interleaved] nisa.dma_copy strided          ── de-interleave d_head (rope.py:120-135)
  ├─ nisa.dma_copy cos_sb / sin_sb                ── half-width tables (rope.py:140-143)
  ├─ RoPE_sbuf (rope.py:173)                      ── the rotation, in SBUF
  │    └─ [relayout_in_sbuf] _convert_from/_to_interleaved + _compute_convert_to_interleaved_mat
  └─ [interleaved] nisa.dma_copy strided scatter  ── re-interleave on store (rope.py:151-166)
```

### Algorithm

```c
function RoPE(x_in, cos, sin, lnc_shard, contiguous_layout=True, relayout_in_sbuf=False):  // rope.py:27
    _validate_rope_inputs(x_in, cos, sin, 'RoPE')        // d_head in {64,128}; cos/sin == (d_head//2, B, S)
    (d_head, B, n_heads, S) = x_in.shape;  half_d = d_head // 2

    (n_prgs, prg_id) = (1, 0)
    if lnc_shard: n_prgs, prg_id = program_sharding_info()  // tile S across LNC cores (rope.py:105)
    assert S % n_prgs == 0                                  // rope.py:108
    tile_size = S // n_prgs;  tile_start = tile_size * prg_id

    // Pick layout-conversion strategy (rope.py:114-116)
    sbuf_ok       = B*n_heads*S <= gemm_moving_fmax         // ~512
    is_dma_relay  = !contiguous_layout && (!relayout_in_sbuf || !sbuf_ok)
    is_sbuf_relay = !contiguous_layout &&  relayout_in_sbuf &&  sbuf_ok

    // Load x into SBUF (de-interleaving via stride-2 DMA when interleaved+DMA)  rope.py:119-137
    if is_dma_relay:
        dma_copy(x_in_sb[:half_d], src=View(x_in).slice(dim0, start=0, step=2)...)  // even d_head indices
        dma_copy(x_in_sb[half_d:], src=View(x_in).slice(dim0, start=1, step=2)...)  // odd  d_head indices
    else:
        dma_copy(x_in_sb, x_in[..., tile_start:tile_start+tile_size])               // contiguous, no relayout

    dma_copy(cos_sb, cos[..., tile])  ;  dma_copy(sin_sb, sin[..., tile])           // half-width (rope.py:140-143)

    RoPE_sbuf(x_in_sb, cos_sb, sin_sb, x_out_sb, convert_from_interleaved=is_sbuf_relay)  // rope.py:147

    // Store (re-interleaving via stride-2 scatter when interleaved+DMA)  rope.py:151-168
    if is_dma_relay: dma_copy(View(x_out).slice(start=0,step=2), x_out_sb[:half_d]); ...slice(start=1,step=2)...
    else:            dma_copy(x_out[..., tile], x_out_sb)
    return x_out  // @ shared_hbm
```

### `RoPE_sbuf` — the rotation in SBUF

`RoPE_sbuf` (`rope.py:173`) is the reusable SBUF-only kernel a megakernel fuses in. It emits exactly the per-half algebra:

```c
function RoPE_sbuf(x_in_sb, cos_sb, sin_sb, x_out_sb, convert_from_interleaved=False):  // rope.py:173
    (d_head, B, n_heads, S) = x_out_sb.shape;  half_d = d_head // 2

    if convert_from_interleaved:                                    // rope.py:210-212
        P = _compute_convert_to_interleaved_mat(x_in_sb)            // d_head×d_head permutation
        x_in_sb = _convert_from_interleaved(x_in_sb, P)             // P^T @ X  (interleaved -> contiguous)

    // Second half copied to its own buffer for tensor_tensor base-partition alignment (rope.py:214-216)
    tensor_copy(sb_odd, x_in_sb[half_d:])

    // 4 broadcast multiplies: cos/sin broadcast over n_heads via expand_dim(2).broadcast(2, n_heads)
    tensor_tensor(even_cos, x_in_sb[:half_d], bcast(cos_sb), nl.multiply)   // rope.py:226
    tensor_tensor(odd_cos,  sb_odd[:half_d],  bcast(cos_sb), nl.multiply)   // rope.py:232
    tensor_tensor(even_sin, x_in_sb[:half_d], bcast(sin_sb), nl.multiply)   // rope.py:238
    tensor_tensor(odd_sin,  sb_odd[:half_d],  bcast(sin_sb), nl.multiply)   // rope.py:244

    tensor_tensor(x_out_sb[:half_d], even_cos, odd_sin,  nl.subtract)       // out1 = x1*cos - x2*sin  (rope.py:251)
    tensor_tensor(x_out_sb[half_d:], odd_cos,  even_sin, nl.add)            // out2 = x2*cos + x1*sin  (rope.py:252)

    if convert_from_interleaved:
        x_out_sb = _convert_to_interleaved(x_out_sb, P)            // P @ X  (contiguous -> interleaved), rope.py:256
    return x_out_sb
```

### Considerations

- **Validation** (`_validate_rope_inputs`, `rope.py:374-410`): `d_head ∈ {64,128}`; `B>0`, `S>0`, `n_heads>0`; `cos.shape == sin.shape == (d_head//2, B, S)`; matching cos/sin dtype. Docstring soft limits (`rope.py:387-392`): `B∈(0,64]`, `S∈(0,512]`, `n_heads∈(0,16]`. SBUF budget (bf16): `B·n_heads·S ≤ 73728`; SBUF-relayout path: `B·n_heads·S ≤ gemm_moving_fmax (~512)`.
- **Half-width cos/sin** (`rope.py:51-52`): this kernel carries the *compressed* `[d_head//2, B, S]` table and reconstructs the full rotation from the two halves — the opposite of `rope_hf`, which carries full head_dim cos/sin.

---

## Interleaved vs Contiguous Memory Layout

### Purpose

`rope.py` is the only site that handles a non-native head_dim memory layout. "Interleaved" here is a property of how `d_head` is stored, **not** a rotation variant. The kernel always de-interleaves into the split-half compute layout, rotates, and (optionally) re-interleaves.

### Algorithm

```text
contiguous_layout=True  (default):  d_head = [ x1[0..half_d-1] | x2[0..half_d-1] ]   (first half | second half)
                                    -> RoPE_sbuf rotates x[:half_d] against x[half_d:] directly. No relayout.
contiguous_layout=False (interleaved): d_head = [ e0, o0, e1, o1, ... ]   (pairs adjacent)
                                    -> must be de-interleaved to [e0,e1,...,o0,o1,...] before rotation.
```

Two de-interleave strategies, chosen by tensor size (`rope.py:114-116`):

```c
// (A) Strided DMA relayout — default for large tensors
dma_copy(x_in_sb[:half_d], View(x_in).slice(dim=0, start=0, end=d_head, step=2))  // even indices  (rope.py:122-128)
dma_copy(x_in_sb[half_d:], View(x_in).slice(dim=0, start=1, end=d_head, step=2))  // odd  indices  (rope.py:129-135)
// mirror stride-2 scatter on store (rope.py:153-166)

// (B) SBUF permutation-matmul — only when B*n_heads*S <= gemm_moving_fmax (~512)
P = _compute_convert_to_interleaved_mat(x)        // rope.py:261  build d_head×d_head permutation from identity
//   strided AP [[d_head,d_head],[1,2],[2,half_d]] on shared_identity_matrix, reshaped (d_head,2,half_d)
//   => P@X maps [e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...]
_convert_from_interleaved(x, P): for each fmax tile  x_psum = nc_matmul(P,  x_flat); copy to SBUF   // P^T@X, rope.py:305
_convert_to_interleaved(x, P):   Pt = nc_transpose(P); for each fmax tile  nc_matmul(Pt, x_flat)    // P@X,   rope.py:337
```

> **QUIRK —** `_convert_to_interleaved` (`rope.py:337-371`) pre-transposes `P` with `nc_transpose` (`rope.py:360`) before the `nc_matmul`. This **cancels the matmul's own implicit transpose**, so the net effect is `P@X`, not `Pᵀ@X`. A reimplementer who feeds `P` straight into `nc_matmul` gets the inverse permutation. (`rope.py:357-361`.)

> **NOTE —** the torch reference `rope_torch.py` (`_rope_single_head`, `:47-72`) re-expresses the same result through interleaving: under `contiguous_layout` it permutes split→interleaved (`new_x[:,::2]=x[:,:d/2]`), rotates as adjacent pairs via `xri.reshape(...,-1,2)` (`:61-67`), then permutes back (`cat(x_out[:,0::2], x_out[:,1::2])`). The round-trip cancels to exactly the split-half rotation on the contiguous tensor. The torch ref is a pair-wise re-expression of the same answer — the kernel itself never interleaves on the default path.

---

## `rope_hf` — HuggingFace Layout and Backward Pass

### Purpose

`rope_hf` (`rope_hf.py:29`) accepts the native HuggingFace `[batch, heads, seq, head_dim]` layout (head_dim innermost/free), rotates **both q and k in one call**, accepts either separate full-width cos/sin or a packed `[cos|sin]` `rope_cache`, and is the **only RoPE site with a backward pass**. It has no in-tree caller — it is a public NxD-facing library kernel (and the readable mirror of the compiled `RoPE.so`).

### Entry Point

```text
rope_hf (@nki.jit, rope_hf.py:29)                 ── HF layout, q+k together, optional backward
  ├─ _validate_apply_rope_inputs (rope_hf.py:154) ── 4-D, head_dim match, cache-XOR-cos/sin, seq % (128*shards)==0
  ├─ per (batch, seq_tile group of NUM_COALESCE_TILES=8):
  │    ├─ dma_copy cos_tile / sin_tile            ── full head_dim width; packed-cache slices cos=[:hd], sin=[hd:]
  │    ├─ _apply_rope_all_heads(q, q_out, ...)    ── rope_hf.py:148
  │    └─ _apply_rope_all_heads(k, k_out, ...)    ── rope_hf.py:149
  └─ each head: _apply_rope_single (rope_hf.py:269)
```

### Algorithm — forward and backward in `_apply_rope_single`

`_apply_rope_single` (`rope_hf.py:269`) slices the **free** axis (head_dim) rather than partitions, and forward/backward differ only by two flipped signs:

```c
function _apply_rope_single(x, cos, sin, backward):           // rope_hf.py:269; x,cos,sin: [seq_tile, num_tiles, head_dim]
    h = head_dim // 2
    result = tensor_tensor(x, cos, nl.multiply)               // result = x*cos, full width  (rope_hf.py:301 / 325)

    if not backward:   // Forward: y = [x1,x2]*[cos1,cos2] + [-x2,x1]*[sin1,sin2]   (rope_hf.py:299)
        temp1  = tensor_tensor(x[..., h:], sin[..., :h], nl.multiply)   // x2 * sin1
        result[..., :h] = tensor_tensor(result[..., :h], temp1, nl.subtract)   //  - x2*sin   (rope_hf.py:309)
        temp2  = tensor_tensor(x[..., :h], sin[..., h:], nl.multiply)   // x1 * sin2
        result[..., h:] = tensor_tensor(result[..., h:], temp2, nl.add)        //  + x1*sin   (rope_hf.py:319)
    else:              // Backward: dx = dy*cos + rotate_half_backward(dy)*sin, rotate_half_backward([a1,a2])=[a2,-a1]
        temp1  = tensor_tensor(x[..., h:], sin[..., h:], nl.multiply)
        result[..., :h] = tensor_tensor(result[..., :h], temp1, nl.add)        //  + (sign flipped vs fwd)  (rope_hf.py:333)
        temp2  = tensor_tensor(x[..., :h], sin[..., :h], nl.multiply)
        result[..., h:] = tensor_tensor(result[..., h:], temp2, nl.subtract)   //  - (sign flipped vs fwd)  (rope_hf.py:343)
    return result
```

The gradient is the transpose of an orthogonal rotation — rotation by `−θ`. The torch ref (`rope_hf_torch.py:28-31, 70-72`) folds sin into the operand: `dx = q*cos + rotate_half_backward(q*sin)` with `rotate_half_backward([a1,a2]) = cat(a2, -a1)`. The kernel's two sign flips relative to forward realize exactly this.

> **QUIRK —** forward and backward read **different sin halves**. Forward subtracts `x2·sin[:h]` and adds `x1·sin[h:]`; backward adds `x2·sin[h:]` and subtracts `x1·sin[:h]`. Because `sin = cat(freqs, freqs)` the two halves are numerically equal, so the swap is observationally a no-op on a correct cache — but a reimplementer who hardcodes one half will silently break on any cache where the halves are *not* duplicated. (`rope_hf.py:303-345`.)

### Considerations

- **Validation** (`_validate_apply_rope_inputs`, `rope_hf.py:154-214`): q is 4-D; `q.head_dim == k.head_dim`; **exactly one** of `rope_cache` or `(cos, sin)` (`rope_hf.py:191,195` — both is rejected, neither is rejected); packed `rope_cache.last == head_dim*2`, separate `cos.last == head_dim`; `rope_tensor.seq ≥ q.seq`; **`q.seq % (pmax · num_shards) == 0`** i.e. divisible by `128·LNC` (`rope_hf.py:211-213`).
- **Coalescing** (`rope_hf.py:26,104-106`): `NUM_COALESCE_TILES = 8` sequence tiles are batched per group; tile size is `min(pmax=128, seq_per_shard)`.
- **Packed cache slicing** (`rope_hf.py:124-132`): cos is the first `head_dim` of the last axis (`offset += 0`), sin the second (`offset += head_dim`).

---

## cos/sin Provenance — Precomputed vs On-Device

### Purpose

Whether the kernel *receives* cos/sin or *computes* them on-chip is the cleanest way to partition the four sites. This section also fixes the framework/kernel boundary: theta and all rope-scaling live host-side, full stop.

### Two regimes

```text
(a) PRECOMPUTED, DMA'd in  ── rope.py, rope_hf.py, qkv_cte fused RoPE
    Framework (NxD) computes cos/sin (or packed rope_cache) host-side and DMAs them.
    The kernels never compute angle / inv_freq / theta in this regime.
      rope.py:140-143       dma_copy cos_sb / sin_sb       (half-width)
      rope_hf.py:145-146    dma_copy cos_tile / sin_tile   (full head_dim)
      qkv_cte: cos_cache_hbm / sin_cache_hbm DMA'd (HBM caches full d_head;
               SBUF sin buffer halved since sin = cat(freqs, freqs))

(b) ON-DEVICE GENERATION  ── attention_tkg.py fused RoPE (decode)
    Kernel is handed inv_freqs [d_head//2, 1] and rope_pos_ids [B, s_active]
    (attention_tkg.py:114-115) and builds cos/sin in _rope() (attention_tkg.py:3175).
```

### The on-device generator `_rope`

```c
function _rope(inv_freqs, pos_ids, bs, s_a, d_head, sbm):   // attention_tkg.py:3175
    assert d_head % 64 == 0                                 // half must be a multiple of 32 (attention_tkg.py:3197)
    h = d_head // 2

    // freqs = inv_freq · pos_id  (the matmul collapses to an elementwise multiply)  attention_tkg.py:3206
    emb = tensor_scalar(pos_ids[0:h, :], op0=nl.multiply, operand0=inv_freqs)

    // ---- sin path: reduce angle to [-pi, pi], then nl.sin ----
    // ((emb + pi) % 2pi) - pi      since sin(t) == sin((t+pi) % 2pi - pi)   attention_tkg.py:3208-3218
    emb4sin[0:h] = tensor_scalar(emb, nl.add, +math.pi)
    _modulo(emb4sin, 2*pi, emb4sin)
    emb4sin[0:h] = tensor_scalar(emb4sin[0:h], nl.add, -math.pi)
    tensor_copy(emb4sin[h:2h], emb4sin[0:h])               // cat(freqs, freqs)  attention_tkg.py:3221
    activation(sin, op=nl.sin, data=emb4sin)               // attention_tkg.py:3222

    // ---- cos path: cos(t) == sin(t + pi/2); offset is +pi/2 + pi = 1.5*pi before the SAME nl.sin ----
    // ((emb + pi/2 + pi) % 2pi) - pi                       attention_tkg.py:3224-3234
    emb4cos[0:h] = tensor_scalar(emb, nl.add, 1.5*math.pi) // 1.5*pi  (NOT 3*pi/2 applied separately)
    _modulo(emb4cos, 2*pi, emb4cos)
    emb4cos[0:h] = tensor_scalar(emb4cos[0:h], nl.add, -math.pi)
    tensor_copy(emb4cos[h:2h], emb4cos[0:h])               // cat(freqs, freqs)  attention_tkg.py:3237
    activation(cos, op=nl.sin, data=emb4cos)               // *** nl.sin, NOT nl.cos *** attention_tkg.py:3238

    return cos, sin
```

> **QUIRK —** the **cos output is produced by the Sine activation** (`nl.sin`, `attention_tkg.py:3238`), not a cosine op. The Activation engine has no hardware Cosine, so the kernel adds `1.5·π` (= `π/2 + π`) to the angle and runs `nl.sin`: the `π/2` is the `cos(θ)=sin(θ+π/2)` identity, and the extra `+π` (then `−π` after modulo) is the range-reduction wrapper shared with the sin path. The in-source comment confirms it verbatim: *"This is to reduce emb to [-π, π] which is the legal restriction for Act Sine (and we dont have Act Cosine)."* (`attention_tkg.py:3225`).

> **NOTE —** the comment at `attention_tkg.py:3224` writes the cos offset as `(emb + π/2 + π)`, and the code adds the single fused constant `1.5*math.pi` (`attention_tkg.py:3227`). These are the same value; the offset is applied as one `tensor_scalar` add, not two. The sin and cos paths are otherwise byte-identical except for this constant.

### The range-reduction `_modulo`

```c
function _modulo(x, y, out, sbm):    // attention_tkg.py:3248 ; computes x mod y, x,y must be positive
    // q = round(x/y - 0.5)  realized as float multiply-add then truncating cast to int32
    q_f32 = tensor_scalar(x, nl.multiply, 1.0/y, nl.add, -0.5)   // attention_tkg.py:3270
    q_i32 = tensor_copy(q_f32 -> int32)                          // truncating cast = floor for the +(-0.5) form, :3271
    qy    = tensor_scalar(q_i32, nl.multiply, y)                 // attention_tkg.py:3275
    out   = tensor_tensor(x, qy, nl.subtract)                    // res = x - q*y  (attention_tkg.py:3279)
    return out
```

> **GOTCHA —** `_modulo` accumulates float32 error as the angle grows. `_perform_rope` guards it: `cfg.curr_sprior <= _MAX_S_PRIOR_ACCURATE_ROPE` where `_MAX_S_PRIOR_ACCURATE_ROPE = 2**17` (131072) (`attention_tkg.py:51,1518-1521`). Above that prior-sequence length the on-device modulo is declared *inaccurate due to float32 error build-up* and the kernel asserts out. A reimplementer must either keep the prior length under 2^17 or compute cos/sin in higher precision.

### Where theta and rope-scaling live

```text
inv_freq  : passed IN as a kernel arg  [d_head//2, 1]   (attention_tkg.py:114, torch ref :76,326)
theta     : the 10000 base and inv_freq = 1/theta^(2i/d) are computed in the NxD FRAMEWORK, not here.
rope_scaling (NTK / YaRN / LongRoPE / linear / dynamic / mscale): ABSENT from every RoPE/attention kernel.
            Any scaling variant is absorbed into the precomputed cos/sin table or the supplied inv_freq.
```

> **NOTE —** a word-boundary search of the readable RoPE and attention kernels returns **zero** `rope_scaling` / NTK / YaRN / LongRoPE / linear / dynamic / `mscale` logic, and the literal base `10000` appears in **no** kernel. The only "LLaMA3" token in the tree is an MLP-tiling heuristic, unrelated to RoPE. This is a negative finding, deliberately stated: the kernels are scaling-agnostic by construction.

---

## Fused RoPE in the QKV / Attention Paths

### Purpose

Standalone `RoPE`/`rope_hf` are the explicit kernels; in production, prefill and decode **fuse** RoPE into the QKV/attention kernel so Q and K never round-trip to HBM between projection and `QKᵀ`. RoPE applies to Q and K only — V is copied unrotated — and always runs after the QKV projection/norm and before `QKᵀ`.

### CTE / prefill — fused in the PSUM→SBUF copy

The CTE path applies RoPE inline in `_copy_psum_to_sbuf_apply_rope_and_bias` (`qkv_cte.py:3140-3322`), gated by `cfg.fused_rope`. cos/sin are **precomputed and DMA'd** (`cos_cache_hbm` / `sin_cache_hbm`, `qkv_cte.py:3183-3194`):

```c
// per Q/K head (loop range(num_q_heads + num_kv_heads), qkv_cte.py:3197):
copy PSUM -> sbuf   (optionally * dequant-scale and/or + bias via scalar_tensor_tensor / tensor_tensor)
rope_buf[d:]      = X2 * sin   (tensor_tensor mul) ; then * -1 (tensor_scalar)   //  -X2*sin  (qkv_cte.py:3245-3257)
rope_buf[d+d/2:]  = X1 * sin   (tensor_tensor mul)                               //   X1*sin   (qkv_cte.py:3259-3265)
rope_buf[:d]      = X  * cos   (tensor_tensor mul)                               //           (qkv_cte.py:3267-3273)
out = X*cos + [-X2*sin, X1*sin]  (tensor_tensor add)                            // split-half  (qkv_cte.py:3275-3281)
// V heads (i_head in [num_q+num_kv, num_q+2*num_kv)): COPIED AS-IS, no rotation.  (qkv_cte.py:3284-3322)
```

`cfg.fused_rope` requires `cos_cache`, `sin_cache`, `num_q_heads`, `num_kv_heads`, and asserts the passed caches are `(B, S, d_head)` (`qkv_cte_utils.py:458-469`).

> **GOTCHA —** the half-sin economy here is **on-chip only**. The HBM input caches `cos_cache`/`sin_cache` are both full `(B, S, d_head)` (`qkv_cte_utils.py:467-469`); the kernel allocates a **full-width** `cos_buffer_sb` (`d_head`, `qkv_cte.py:826`) but a **half-width** `sin_buffer_sb` (`d_head//2`, `qkv_cte.py:836`) and DMAs only half the sin into SBUF — because `sin = cat(freqs, freqs)` makes the two halves equal (`qkv_cte.py:821`). A reimplementer must keep full cos in the buffer (cos multiplies both halves) but may halve the sin SBUF footprint.

See [Projection Kernels](projection-kernels.md) *(planned, 6.7.9)* for the full CTE QKV fusion that hosts this copy, and [CTE Attention](attention-cte.md) for the `QKᵀ` consumer.

### TKG / decode — fused with on-device cos/sin

`attention_tkg.py` has two decode sub-paths:

- **Fused** (`cfg.fuse_rope`): `_perform_rope` (`attention_tkg.py:1506`) calls `_rope` to generate cos/sin on-device (the [cos-via-sin](#the-on-device-generator-_rope) path), then `_apply_rope` (`attention_tkg.py:3089`) loads+transposes x (`BNSd→BNdS`, `nc_transpose`), multiplies by cos, builds `rotate_half(x)` via `tensor_copy` + `tensor_scalar(×−1)`, multiplies by sin, and adds. **Only the last NeuronCore rotates K_active** (`attention_tkg.py:1554`); K is written to `k_out` post-RoPE. Q is additionally scaled by `1/sqrt(d_head)` — folded directly into this kernel via `activation(..., scale=1/sqrt(d_head))` (`attention_tkg.py:1567`). See [TKG Attention](attention-tkg.md).
- **Deferred** (experimental `attention_block_tkg.py:905`): calls the standalone `RoPE_sbuf` with precomputed cos/sin, sandwiched between pre- and post-RoPE Q/K RMSNorm; `cos=None` ⇒ skip RoPE.

> **NOTE —** the Q-side `1/sqrt(d_head)` scaling is applied **only** when `cfg.fuse_rope` is set (`attention_tkg.py:91` docstring: *"Q is scaled with 1/sqrt(d_head) iff. cfg.fuse_rope"*). A reimplementer wiring the deferred path must apply that scale elsewhere.

### Function Map

| Symbol | Source | Role | Confidence |
|---|---|---|---|
| `_copy_psum_to_sbuf_apply_rope_and_bias` | `qkv_cte.py` | CTE fused RoPE+bias, Q/K rotated, V copied | CERTAIN |
| `_perform_rope` | `attention_tkg.py:1506` | decode RoPE driver, generates+applies, modulo guard | CERTAIN |
| `_rope` | `attention_tkg.py:3175` | on-device cos/sin (cos via `nl.sin`) | CERTAIN |
| `_apply_rope` | `attention_tkg.py:3089` | decode rotation apply (transpose, rotate_half, add) | CERTAIN |
| `_modulo` | `attention_tkg.py:3248` | `round`-based range reduction to `[-π,π]` | CERTAIN |
| `RoPE_sbuf` (deferred) | `rope.py:173` | standalone fusion entry for `attention_block_tkg` | CERTAIN |

---

## Evidence summary

| Claim | Grounding | Confidence |
|---|---|---|
| All sites use split-half; no GPT-J interleaved rotation | the verbatim formula at `rope.py:185-186`, `rope_hf.py:288`, `attention_tkg.py:3100-3106`, `attention_tkg_torch.py:289-303`, and the qkv docstrings (`qkv_cte.py:160-161,818-820,3196`; `qkv.py:123-125`) is `cat(-x2, x1)` block-split in every case. The only `reshape(...,-1,2)` pair rotation is in the *torch reference* `rope_torch.py`, where the surrounding permute-in/permute-out cancels to split-half. The compiled `RoPE.so` corroborates independently: its docstring names "even and odd embeddings" and states it implements them *"without interleaving"* as `result[:N/2] = X[:N/2]*cos - X[N/2:]*sin` / `result[N/2:] = X[N/2:]*cos + X[:N/2]*sin` | CERTAIN |
| cos is computed by the Sine engine; there is no hardware cosine | `attention_tkg.py:3236-3238` reads `nisa.activation(cos, op=nl.sin, data=emb4cos)` with `emb4cos = emb + 1.5π` reduced mod 2π; the comment at `:3225` says *"we dont have Act Cosine"*. The code applies the shift as one fused `+1.5*math.pi` add; the underlying identity is `cos(θ)=sin(θ+π/2)`, with the extra `+π` being the shared range-reduction wrapper | CERTAIN |
| Interleaving is a layout, undone before rotation | `rope.py:114-137` (strategy selection + stride-2 DMA) and `_compute_convert_to_interleaved_mat` / `_convert_from_interleaved` (`rope.py:261-334`): the compute layout fed to `RoPE_sbuf` is always `[first_half \| second_half]`, and interleaving is a pure memory permutation on `d_head` | CERTAIN |
| `rope_hf` is the only backward site, and backward is two sign flips | `rope_hf.py:322-345` flips exactly the two `subtract`/`add` ops relative to forward (`:308-321`), matching `rotate_half_backward([a1,a2])=[a2,-a1]` in the torch ref; no `backward` parameter exists in `rope.py`, `qkv_cte`, or `attention_tkg` RoPE | CERTAIN |
| No theta / RoPE-scaling logic in any kernel | negative search over `nkilib/core/embeddings/`, `attention_tkg.py`, `qkv_cte.py`: zero `rope_scaling` / NTK / YaRN / LongRoPE / `mscale` / `10000` / `theta` hits in the rotation kernels; `inv_freq` is always an input arg | CERTAIN |

Two limits on the above. The compiled `RoPE.so` (676,376 bytes; no readable `RoPE.py` ships alongside it) was read via `strings` only — it embeds the same split-half algebra in a differently-worded even/odd docstring, making it a sibling implementation rather than a copy, and it was not instruction-traced. And the `nl.sin`→hardware-Sine-engine mapping is taken from the op name and the in-source "Act Sine"/"Act Cosine" comments; the mapping is **[INFERRED]** at the ISA level, though certain at the NKI-op level.

## Related Components

| Name | Relationship |
|---|---|
| CTE QKV projection | Hosts the fused-RoPE PSUM→SBUF copy (`_copy_psum_to_sbuf_apply_rope_and_bias`) |
| TKG attention | Fuses on-device RoPE generation + apply in decode |
| RMSNorm-Q/K | May wrap RoPE pre- and post- in the experimental TKG megakernel |

## Cross-References

- [CTE Attention](attention-cte.md) — consumes the rotated Q/K that feed `QKᵀ` in prefill
- [TKG Attention](attention-tkg.md) — hosts `_rope`/`_apply_rope`/`_modulo`; the on-device cos/sin decode path
- [Projection Kernels](projection-kernels.md) — *(planned, 6.7.9)* CTE QKV path that fuses RoPE inline
- [nkilib Infrastructure](nkilib-infrastructure.md) — `@nki.jit`, the readable-`.py` ↔ compiled-`.so` mirror, `TensorView`/`kernel_assert`
- [ISA Compute Intrinsics](isa-compute-intrinsics.md) — `tensor_tensor` / `tensor_scalar` / `activation` / `nc_matmul` / `nc_transpose` primitives
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — partition-vs-free axis placement that distinguishes `rope` from `rope_hf`
- [Compiler Pipeline](../front/pipeline.md) — where NKI kernels enter the neuronx-cc flow
