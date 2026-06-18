# MXFP Weight/Scale Load Infrastructure

> *All source line numbers and symbols on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (the cp310 wheel; the cp311/cp312 wheels carry byte-identical kernel sources — `fd --no-ignore` spot-checks the three trees match). The kernels are shipped as readable `@nki.jit` NKI-DSL Python under `nkilib/core/moe/moe_cte/` and `nkilib/core/qkv/` inside the wheel — a binary-derived wheel artifact, citeable verbatim. Every `nisa.*` primitive named here is a Cython intrinsic; cross-check against [nki.isa COMPUTE Intrinsics](isa-compute-intrinsics.md).*

## Abstract

This page documents the **shared load path** that feeds neuronx-cc's microscaling (MX) matmul kernels: how an MXFP4/MXFP8 weight checkpoint and its OCP-MX E8M0 per-block scales get from HBM onto the SBUF partitions in exactly the geometry `nc_matmul_mx` expects. It is the common substrate under the two shipped MX MoE kernels — [`bwmm_shard_on_block_mx` / `bwmm_shard_on_I_mx`](moe-cte-prefill.md) — and under the MX QKV projection ([projection kernels](projection-kernels.md)). Three mechanisms make up the infra, and each is counter-intuitive:

- **The weight "load" never unpacks anything.** Frameworks (`torch`/`xla`) cannot pass an MXFP tensor across the boundary, so x4-packed FP4/FP8 weights ride as same-width integer alternates (`uint16`↔`fp4_e2m1fn_x4`, `uint32`↔`fp8_e4m3fn_x4`). The kernel **view-casts** the alternate to the x4 dtype — a pure metadata relabel with no data copy — and DMA-copies the container bytes verbatim. The ×4 expansion of the packed K-extent is a **hardware event inside the matmul descriptor**, not a software unpack ([MXMEM_PATTERN1D §x4-unpack](../isa/mxmem-pattern1d.md), [PE matmul encoding](../isa/pe-matmul-encoding.md)).
- **The scale "load" is an indirect gather into a sparse quadrant layout.** A 512-element x4-packed contraction tile is covered by 16 E8M0 bytes; the matmul wants those 16 scattered as **4 valid rows at the base of each 32-partition quadrant**, the other 28 partitions left as holes. An expert-indexed `-1`-padded index vector drives one indirect-DGE `dma_copy` whose `-1` entries are silently skipped, leaving the holes as pre-zeroed SBUF.
- **Two weight layouts, one footprint.** `MX_CONTIGUOUS` packs 4 adjacent `H` rows per x4 container; `MX_INTERLEAVED` permutes the rows so the quads line up with the swizzle a DMA-transpose stamps on the activation. Both land at `[H//4, I]` x4 — same bytes, different occupants. The kernel never re-permutes at runtime; the caller must pre-pack to match its transpose mode.

The reference frame is OCP-MXFP (32-element blocks, one shared E8M0 exponent per block) and a weight-stationary systolic array. If you know that an MX operand is `(packed data, per-block exponent)` and that the PE array contracts on the partition axis, the endpoints are familiar; the Neuron-specific part is the **partition geometry** — the `8(p)×4(f)` scale grouping and the quadrant-hole packing that follow from it.

For reimplementation, the contract is:

- **The x4-pack alternate map and the no-copy view-cast** (`_ALTERNATIVE_DTYPE_TO_MXFP`, `convert_to_mxfp_dtype`, `TensorView.reinterpret_cast`): which dtypes alias, why same-width casts take the pure-relabel branch, and the `target_dtype` inheritance that forbids a mixed FP4/FP8 gate/down pair.
- **The HBM weight/scale shapes** (`[E, 128, 2, n_H512_tile, I]` weights, `[E, 16, …]` scales), the `E·16` scale fold, and the expert-selected, LNC-sharded indirect weight DMA (`select` + `slice`).
- **The quadrant-hole index builder** (`_generate_expert_index_vector`): `iota` + `memset(-1)` + `scalar_tensor_tensor` + `nc_transpose` → a `[128,1]` int32 index, 4 valid per 32, the rest `-1`.
- **The scale gather** (`dma_copy(vector_offset=idx, indirect_dim=0, oob_mode=skip)`) and the static 4-quadrant loop variant.
- **The `MX_CONTIGUOUS` / `MX_INTERLEAVED` weight-packing recipes** and the `nc_matmul_mx` 4-operand handoff.

| | |
|---|---|
| **Shared helpers** | `nkilib/core/moe/moe_cte/moe_cte_mx_utils.py` (1007 lines) |
| **MoE load callers** | `bwmm_shard_on_I_mx.py` (2040), `bwmm_shard_on_block_mx.py` (1628) |
| **QKV MX path** | `nkilib/core/qkv/{qkv_cte.py, qkv_cte_utils.py, qkv_tkg_mx_impl.py}` |
| **Layout enum** | `QKVWeightLayout` — `nkilib/core/utils/common_types.py:65` (`CONTIGUOUS=0`, `MX_CONTIGUOUS=1`, `MX_INTERLEAVED=2`) |
| **View-cast** | `TensorView.reinterpret_cast` — `nkilib/core/utils/tensor_view.py:120` |
| **Index builder** | `_generate_expert_index_vector` — `moe_cte_mx_utils.py:948` |
| **x4 dtype sizes** | `fp4_e2m1fn_x4 = 2 B`, `fp8_e4m3fn_x4 = fp8_e5m2_x4 = 4 B` (`allocator.py:50-53`) |
| **HW constants** | `SBUF_QUADRANT_SIZE = 32`, `_q_height = 8`, `_q_width = 4` (`moe_cte_mx_utils.py:52-56`) |
| **Matmul handoff** | `nisa.nc_matmul_mx(dst, stationary, moving, stationary_scale, moving_scale)` |

> **NOTE — what this page does *not* own.** The 64-byte MX matmul/load bundle encoding (`LdWeightMx 0x1009`, `MatmultMx 0x100A`) is [PE matmul encoding](../isa/pe-matmul-encoding.md); the data+scale descriptor that pairs the two addresses and performs the `n <<= 2` x4-unpack is [MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md); the activation-side `quantize_mx` and the MX numeric format are [MX-FP8 microscaling legalization](../hlo-opt/mx-fp8-legalization.md). This page is the **NKI-source load path** that produces the operands those pages consume.

---

## The x4-Packed Weight Load

### Purpose

A weight checkpoint that is logically MXFP4 or MXFP8 cannot cross the framework boundary as an MX tensor — `torch`/`xla` have no MXFP dtype. So the producer stores it as a **same-width integer alternate** and the kernel relabels it back. The "load" is therefore two distinct steps that a reimplementer must not conflate: a **metadata-only dtype reinterpret** (no bytes move) and a **plain DMA** of the already-packed container bytes from HBM into SBUF. There is no software unpack anywhere in the NKI source.

### The alternate-dtype map

`_ALTERNATIVE_DTYPE_TO_MXFP` (`moe_cte_mx_utils.py:61`) is the entire dtype contract:

```python
# moe_cte_mx_utils.py:61-66
_ALTERNATIVE_DTYPE_TO_MXFP = {
    nl.uint16: nl.float4_e2m1fn_x4,   nl.int16: nl.float4_e2m1fn_x4,   # MXFP4
    nl.uint32: nl.float8_e4m3fn_x4,   nl.int32: nl.float8_e4m3fn_x4,   # MXFP8
}
```

The `_x4` suffix is the **hardware x4-pack geometry**: 4 logical contraction elements packed into one container element along the `K`/`H`/`I` contraction axis. `fp4_e2m1fn_x4` is `4 × FP4` (e2m1: 1-sign / 2-exp / 1-mantissa nibble) → 16 bits → a **2-byte** container; `fp8_e4m3fn_x4` and `fp8_e5m2_x4` are `4 × FP8` → 32 bits → a **4-byte** container. CONFIRMED against `allocator.py:50-53`:

```python
# allocator.py:50-53 (sizeinbytes)
elif str(dtype) == str(nl.float4_e2m1fn_x4):                                  return 2
elif str(dtype) == str(nl.float8_e4m3fn_x4) or str(dtype) == str(nl.float8_e5m2_x4): return 4
```

The container byte-width equals the alternate's: `uint16 ↔ fp4_e2m1fn_x4` are both 2 B, `uint32 ↔ fp8_e4m3fn_x4` both 4 B. That equality is what makes the view-cast free.

> **GOTCHA — only the four *integer* alternates convert.** The `convert_to_mxfp_dtype` docstring (`:79`) mentions "`float16` for MXFP4, `float32` for MXFP8", but `float16`/`float32` are **not keys** in `_ALTERNATIVE_DTYPE_TO_MXFP`. A `float16`/`float32` input falls through to `mxfp_target = None` and keeps its own dtype — it is *not* auto-reinterpreted as x4. A reimplementer who passes BF16/FP32 weights expecting an implicit MX relabel gets a plain float tensor. CONFIRMED — the dict has exactly four keys, all `int`/`uint` (`:61-66`).

> **QUIRK — the descriptor's K-extent counts *logical* elements, the HW re-expands ×4.** The matmul/load descriptor's K-extent at `+0x8` ([MXMEM_PATTERN1D §3](../isa/mxmem-pattern1d.md)) counts x4-**un**packed elements, and the silicon multiplies it back by four (`n <<= 2` for `Dtype ∈ {2,8,9}`) when it streams the container into the PE. The NKI source only ever sees the packed 2-byte/4-byte container; the "x4-unpack" the brief asks about happens *inside* `nc_matmul_mx`, not here. CONFIRMED — three-way agreement: this source view-casts and DMAs verbatim, the descriptor carries the logical extent, the encoder rejects/accepts the x4 tags ([PE matmul encoding §dtype legality](../isa/pe-matmul-encoding.md)).

### Algorithm — the no-copy view-cast

```c
// convert_to_mxfp_dtype  — moe_cte_mx_utils.py:74-107
function convert_to_mxfp_dtype(tensor, weight_dtype = None):
    mxfp_target = _ALTERNATIVE_DTYPE_TO_MXFP.get(tensor.dtype)   // :93 — None unless int alt
    if weight_dtype is None:                                     // :95
        target_dtype = mxfp_target if mxfp_target is not None else tensor.dtype
    else:
        target_dtype = weight_dtype                              // caller pins it (see inheritance below)
    if mxfp_target is not None:                                  // :100 — int alt → relabel
        tensor = TensorView(tensor).reinterpret_cast(target_dtype)
    else:                                                        // :104 — already x4 / float: just wrap
        tensor = TensorView(tensor)
    return tensor, target_dtype
```

The relabel itself is `TensorView.reinterpret_cast` (`tensor_view.py:120`). Its docstring states the contract verbatim — *"Similar to NumPy's `ndarray.view(dtype)` or C++ `reinterpret_cast`. No data is copied; only the dtype, shape, and strides are adjusted"* (`:123-124`). The crucial branch for MX is the **same-width** one:

```c
// TensorView.reinterpret_cast — tensor_view.py:143-200
old_size = sizeinbytes(self.dtype)       // :143
new_size = sizeinbytes(new_dtype)        // :144
if old_size == new_size:                 // :146 — uint16↔fp4x4 (2≡2), uint32↔fp8x4 (4≡4)
    return self._copy(dtype=new_dtype)   // :147 — PURE RELABEL: no shape/stride/offset change
// ── below here is the cross-size path; MX never reaches it ──
kernel_assert(self.indirect_dim is None, ...)              // :152 indirect+resize unsupported
if new_size > old_size:  ratio = new_size//old_size; ...   // :160 last dim shrinks, strides /ratio
else:                    ratio = old_size//new_size; ...    // :186 last dim grows,  strides *ratio
return self._copy(shape=new_shape, strides=new_strides, offset=new_offset, dtype=new_dtype)
```

> **CORRECTION (D-O29 §1.2) — the view-cast is even simpler than "no-copy bitcast".** Because the alternates are always *byte-identical in width* to their x4 target, the cast takes the `old_size == new_size` branch (`:146-147`) — `self._copy(dtype=new_dtype)`, a pure dtype **relabel** with **zero** shape/stride/offset change. The general `reinterpret_cast` *does* rescale strides and resize the last dim (the docstring example `(128,512) f32 → (128,2048) u8` at `:137`), but the MX path never hits that arm. The HBM bytes are already in x4-packed order; there is no deinterleave, no repack, no reshape. (D-O29 reached this; the source confirms the exact branch.)

> **QUIRK — the in-SBUF re-relabel uses a *different* method.** `convert_to_mxfp_dtype` is the **HBM-side** relabel and uses `TensorView.reinterpret_cast`. The **block-shard** kernel additionally re-relabels the *already-loaded SBUF* buffer with `gup_weights_qtz_sb = gup_weights_qtz_sb.view(inps.gate_up_proj_weight.dtype)` (`bwmm_shard_on_block_mx.py:708`) — `TensorView.view`, not `reinterpret_cast`. Both are same-width relabels with no data motion, but they are distinct methods; the I-shard variant does not need the second one because it allocates the SBUF buffer with `dtype=inps.gate_up_proj_weight.dtype` directly (`bwmm_shard_on_I_mx.py:1124`).

### The weight DMA — expert-selected, LNC-sharded, indirect

The HBM weight tensor is laid out per-expert with the x4-packed `H` contraction already on the leading partition axis. Gate/Up is `[E, 128, 2, n_H512_tile, I]` — `dim1 = 128` is the x4-packed `H` contraction on the partition axis (a 512-element `H` tile = 128 partitions × 4 x4-lanes), `dim2 = 2` is the gate/up pair. Down is `[E, p_I, n_total_I512_tile, H]`. The expert pick and LNC shard ride entirely in the access pattern; there is **no software gather**:

```c
// load_gup_weights_scales_shard_on_intermediate_mx — bwmm_shard_on_I_mx.py:1123-1138
gup_weights_qtz_sb = ndarray((128, 2, n_H512_tile, I_lnc_sharded),
                             dtype = gate_up_proj_weight.dtype, buffer = sbuf)   // :1123
gup_weight_view = gate_up_proj_weight.select(dim=0, index=block_expert)         // :1130 expert pick
                  .slice(dim=3, start=I_lnc_sharded*shard_id, end=…)            // :1131 LNC I-shard
dma_copy(dst=gup_weights_qtz_sb, src=gup_weight_view.get_view(),               // :1133
         oob_mode = skip if skip_weight else error,                            // :1136
         dge_mode = hwdge)                                                      // :1137
```

`select(dim=0, index=block_expert)` is the expert selector. When `index` is not an `int` it routes through `TensorView._dynamic_select` (`tensor_view.py:677-678`), which sets `indirect_dim` and a `scalar_offset` on the leading `E` axis — so the HBM base address is offset by `block_expert` **at descriptor time**, on a hardware DGE (`dge_mode.hwdge`), not by reading and re-storing data. The down path is the mirror: `select(dim=0, index=block_expert).slice(dim=1, …)` (`bwmm_shard_on_I_mx.py:1316-1320`).

> **NOTE — `p_I` is not padded when `I < 512`.** Down's `p_I = 128 if I > 512 else I // 4` (`moe_cte_mx_utils.py:403`); when the intermediate dimension is small the last `I512` tile is left unfilled in HBM (to save bytes) and the unused SBUF partitions are `memset 0` before the gather, so the hole rows read as neutral.

---

## The E8M0 Per-Block Scale Load

### Purpose

Each 32-element OCP-MX block shares one 8-bit E8M0 exponent. The matmul does not want those exponents densely packed — it wants them in the **same quadrant geometry** the PE array reads weight scales from: one E8M0 byte per `8(partition) × 4(free)` PE sub-tile, which means **4 valid scale rows at the base of each 32-partition quadrant** and 28 holes. The scale load's whole job is to gather the right expert's scales from HBM into that sparse layout. It is an indirect DGE whose index vector encodes both the expert offset and the quadrant holes.

### The scale shape and the E·16 fold

The HBM scale tensors carry a `16` fold per expert (`InputTensors` docstring, `moe_cte_mx_utils.py:131-132`):

```text
gate_up_proj_scale  [E, 16, 2, n_H512_tile, I]   uint8  (E8M0 exponent)
down_proj_scale     [E, 16, n_I512_tile, H]      uint8  (E8M0 exponent)
```

The `16` is the per-expert scale fold: a 512-element x4-packed `H` tile (128 partitions × 4 lanes) is covered by `512 / 32 = 16` E8M0 bytes (OCP-MX `block_size = 32`, [MXMEM_PATTERN1D §block size](../isa/mxmem-pattern1d.md)). The loaders **fold `E` and `16` together** so the partition axis (`E·16`) can be hit by one vector index:

```c
// bwmm_shard_on_I_mx.py:1148-1150
gup_scale_view  = gate_up_proj_scale.reshape((E*16, 2, n_H512_tile, I))      // :1148
// bwmm_shard_on_I_mx.py:1344
down_scale_view = down_proj_scale.reshape((E*16, n_I512_tile, H))            // :1344
```

### Algorithm — the quadrant-hole index vector

`_generate_expert_index_vector` (`moe_cte_mx_utils.py:948`) builds a `[128,1]` int32 index whose entries are the *folded* `E·16` row numbers for the chosen expert, scattered 4-per-quadrant with `-1` holes:

```c
// _generate_expert_index_vector — moe_cte_mx_utils.py:948-1007
function _generate_expert_index_vector(expert_index, dst_idx_vector,
                                       scale_factor, n_quadrants_needed, n_remaining_partition=0):
    n_quadrants = n_quadrants_needed + (n_remaining_partition > 0)        // :977
    assert n_quadrants <= 4 and n_remaining_partition < 4                 // :978-979

    arange_f = iota([[1, n_quadrants*4]])            // :981-982 — [0,1,2,3,…] int32, 4 per quadrant
    padded_index = memset(-1.0, shape [1, 128])      // :984-985 — all 128 partitions start as HOLES

    scalar_tensor_tensor(                            // :987 — scatter valid rows into quadrant bases
        dst   = padded_index.ap([[128,1],[SBUF_QUADRANT_SIZE, n_quadrants],[1, 4]]),  // quad-stride 32, inner span 4
        data  = expert_index.ap([[1,1],[0, n_quadrants*4]]),  // broadcast the expert scalar
        op0   = multiply, operand0 = float(scale_factor),     // expert * 16
        op1   = add,      operand1 = arange_f)                // + [0,1,2,3]
    // expert 0 → [0,1,2,3, -1×28, 4,5,6,7, -1×28, 8,9,10,11, -1×28, 12..15, -1×28]
    // expert 3 → [48,49,50,51,-1×28, 52..55, -1×28, 56..59, -1×28, 60..63, -1×28]

    if n_remaining_partition > 0:                    // :996 — down path, p_I < 128
        memset(-1.0, padded_index.ap([[128,1],[1, 4 - n_remaining_partition]],
               offset=(n_quadrants-1)*32 + n_remaining_partition))   // :999 — extra holes in last quad

    padded_index_psum = nc_transpose(padded_index)   // :1002 — [1,128] → [128,1], onto partition axis
    tensor_copy(dst_idx_vector, padded_index_psum)   // :1003
    return tensor_copy(... , engine=scalar_engine)   // :1005-1006 — land as int32 on partitions
```

`scale_factor = scale_shape[1] = 16` (the per-expert fold, passed at the call site, `bwmm_shard_on_I_mx.py:1166`). So `expert * 16 + [0..3]` is the folded `E·16` row for the four valid scale rows of one quadrant; the `nc_transpose` puts the index **on the partition axis** so it drives a partition-dim vector DGE. This is the only place the quadrant-hole pattern is *computed*; the static-loop variant below stamps the same geometry without an index.

> **NOTE — `_generate_expert_index_vector` lives in the shared util, but the gather DMA does not.** The index builder is `moe_cte_mx_utils.py:948`, imported by both `bwmm_shard_on_I_mx.py:60` and `bwmm_shard_on_block_mx.py:41`. The actual scale-gather `dma_copy` bodies live in the two `bwmm_shard_on_*_mx.py` callers, not in the shared util — the util provides the index, the caller drives the DGE.

### The scale gather DMA — `-1` OOB-skip into pre-zeroed SBUF

```c
// gate/up scale gather — bwmm_shard_on_I_mx.py:1174-1191
dma_copy(
    src = gup_scale_view.ap(
        pattern = [[stride_dim0, 128],                       // :1177 dim0 stride = 2*full_n_H512_tile*I
                   [full_n_H512_tile_scale * I, 2],          // :1178 gate/up
                   [I, n_H512_tile],                         // :1179 H512 tile (load sharded count)
                   [1, I // 2]],                             // :1180 I dim
        offset = (I // 2) * shard_id,                        // :1182 LNC I-shard offset
        vector_offset = token_indices_on_p.ap([[1,128],[1,1]]),  // :1183 — THE GATHER (quadrant-hole index)
        indirect_dim = 0),                                   // :1187 — indirect on the folded E*16 axis
    dst = gup_scales_sb[:128, :2, :n_H512_tile, :I//2],      // :1189
    oob_mode = oob_mode.skip)                                // :1190 — -1 rows silently skipped → SBUF holes
```

The `vector_offset` is the transposed index from `_generate_expert_index_vector`; `indirect_dim=0` makes the DMA index the folded `E·16` partition axis per-row; `oob_mode.skip` means every `-1` index row is **not written**, so the 28 holes per quadrant keep whatever the destination held. The destination is pre-`memset 0` (`gup_scales_sb` zeroed at alloc), so the holes read as neutral E8M0 zero. The down gather is the same shape with a 3-entry pattern (`bwmm_shard_on_I_mx.py:1374-1386`).

> **GOTCHA — the scale gather carries no `dge_mode` kwarg; the weight DMA does.** The weight and bias DMAs are explicitly `dge_mode=dge_mode.hwdge`. The scale-gather `dma_copy` calls (`:1174`, `:1374`, and the block-shard `:743`, `:943`) pass **no `dge_mode`** — their indirect nature comes entirely from `vector_offset` + `indirect_dim=0`, and the DGE mode defaults. Do not assume `hwdge` on the scale gathers from the weight-DMA precedent; only `oob_mode=oob_mode.skip` is set. CONFIRMED across all four scale-gather sites.

### The static 4-quadrant loop variant — why 4-valid-per-32

`gate_up_projection_mx_tp_shard_I` shows the same geometry without an index vector, as a static 4-iteration loop over the **non-folded** weight scale `[128//8, n_H512_tile, I] = [16, …]`:

```c
// gate_up_projection_mx_tp_shard_I — bwmm_shard_on_I_mx.py:1660-1676
// "we have 1 scale per 8(p)x4(f) tile, but still span across full pdim with gaps"   // :1660
assert weight_scale.shape == (128 // _q_height, n_H512_tile, I)   // :1662 — (16, …); _q_height = 8
weight_scale_sb = ndarray(weight_qtz_sb.shape, dtype=uint8, sbuf) // :1665
n_quadrants_needed = H0 // SBUF_QUADRANT_SIZE                      // :1667 — H0=128 → 4
for quad_idx in range(n_quadrants_needed):                        // :1668
    dma_copy(src = weight_scale[quad_idx*4 : (quad_idx+1)*4, :, :],          // :1670 4 source rows
             dst = weight_scale_sb[quad_idx*32 : quad_idx*32 + 4, :, :])     // :1675 → quadrant base
// → 4 scale partitions at each quadrant base: [0:4], [32:36], [64:68], [96:100]
```

This is the **same** quadrant-hole layout as the vector-index path, expressed as a 4-quadrant copy loop. The reason is the `8(p)×4(f)` PE scale grouping: `_q_height = 8` (one E8M0 scale covers an 8-partition × 4-free PE sub-tile, `moe_cte_mx_utils.py:52-53`), so `32 partitions / 8 = 4` valid scale rows per quadrant. CONFIRMED three ways: this source (`128 // _q_height = 16` non-folded rows, 4 per quadrant), the host-side `mx_torch_common.py` round-trip (*"4 valid scale rows per 32-partition quadrant, rest zero"*, `_QUADRANT_SIZE = 32`, `_SCALE_PER_QUADRANT = 4`), and the QKV variant below.

> **NOTE — "1 scale per 8(p)×4(f) tile" is the `[P/8, F/4]` E8M0 operand `nc_matmul_mx` expects.** The PE matmul consumes weight/ifmap scales as `[P/8, F/4]` E8M0 `uint8` ([PE matmul encoding](../isa/pe-matmul-encoding.md)). The `qkv_tkg_mx_impl.py:148` docstring states it verbatim: *"weight scales are 8× times smaller, not 32×"* — i.e. one scale per 8 partitions (and per 4 free), exactly the grouping this loop materializes. The quadrant-hole `[128]` partition buffer with 4 valid per 32 *is* that `[P/8]`-effective operand spread across the full partition dimension.

---

## MX_CONTIGUOUS vs MX_INTERLEAVED

### Purpose

The MoE/BWMM load above is implicitly **`MX_CONTIGUOUS`** — it never transposes the weight. The QKV kernels expose the choice explicitly through `QKVWeightLayout` (`nkilib/core/utils/common_types.py:65`), because QKV can run the activation through a **DMA-transpose** path, and a transposed activation needs its x4 weight quads permuted to match.

> **NOTE — `common_types.py` exists in three copies; only one carries the enum.** `fd --no-ignore common_types.py` finds three files: the truncated 44-line copies in `neuronxcc/nki/_pre_prod_kernels/` and `neuronxcc/private_nkl/utils/` (**no** `QKVWeightLayout`), and the full 96-line `nkilib/core/utils/common_types.py` (`:65`, has it). Production QKV imports the `nkilib/core/utils/` one — follow the actual import line, not the first `fd` hit.

### The enum — three layouts, one footprint

The enum docstring (`common_types.py:66-87`) *is* the packing spec — the recipes are host-side, applied to a `[H, I]` checkpoint before the kernel runs:

| Value | Name | Packing (from `[H, I]` checkpoint) | Result |
|---|---|---|---|
| `0` | `CONTIGUOUS` | non-MX: checkpoint as-is | `[H, I]` |
| `1` | `MX_CONTIGUOUS` | `w.reshape(H//4, 4, I).transpose(0,2,1).reshape(H//4, I*4)` → cast `float8_e4m3fn_x4` | `[H//4, I]` x4 — **4 adjacent `H` rows per quad** |
| `2` | `MX_INTERLEAVED` | from `MX_CONTIGUOUS`, unpack to `[H,I]`, reorder `h_idx = arange(H).reshape(2, H//4, 2).transpose(1,0,2).reshape(H)`, `w[h_idx,:]`, re-pack | `[H//4, I]` x4 — **`(2, H//4, 2)`-permuted quads** |

Both MX layouts land at `[H//4, I]` x4 — **same footprint**; they differ only in *which* 4 original `H` rows occupy each x4 container. `MX_CONTIGUOUS` puts 4 contiguous `H` rows in each quad. `MX_INTERLEAVED` applies the `(2, H//4, 2)` permutation so the quad lanes line up with the 2×2 interleave a `dma_transpose` stamps onto the swizzled activation — so stationary weight quads and moving activation quads dequantize against matching E8M0 blocks.

> **QUIRK — the kernel never re-permutes weights at runtime; the caller must pre-pack.** Across `nkilib/core/qkv/`, the only `MX_CONTIGUOUS` / `MX_INTERLEAVED` references are the **enum-comparison guards** in `qkv_cte_utils.py` (load-path selection) — the packing transforms themselves are not open-coded in any kernel. The kernel DMAs already-packed `float8_e4m3fn_x4` weights; the `(2,H//4,2)` reorder is done host-side per the docstring recipe. A reimplementer must pre-pack to the layout matching the transpose mode — the kernel will not fix a mismatch, it will assert.

### Load-path selection — the runtime assert pins the layout

```c
// qkv_cte_utils.py:564-583 — weight layout validation
if quantization_type == QuantizationType.MX:
    _is_fp8_input = input.dtype in [float8_e4m3, float8_e4m3fn]                  // :566
    _has_dequant  = qkv_in_scale != None and weight_layout == MX_INTERLEAVED      // :567
    _use_dma_xpose = (_is_fp8_input or (not _is_fp8_input and _has_dequant))
                     and load_input_with_DMA_transpose                            // :568
    if _use_dma_xpose:
        assert weight_layout == MX_INTERLEAVED   // :571-572 — DMA-transpose path DEMANDS interleaved
    else:
        assert weight_layout == MX_CONTIGUOUS    // :576-577 — plain PE-transpose path DEMANDS contiguous
else:
    assert weight_layout == CONTIGUOUS           // :581-582 — non-MX
```

The DMA-transpose activation path **demands** `MX_INTERLEAVED` weights; the plain path **demands** `MX_CONTIGUOUS`. The actual QKV x4-weight DMA + quadrant-scale DMA are at `qkv_cte.py:1867-1898`: a static 4-quadrant scale DMA into `mx_weight_scale_sb` (`SCALE_P_PER_QUAD = H_pack = 4`, `qkv_cte.py:1806`; `dst = …[ds(quad_idx*32, 4), …]`), then the `[128, num_128_tiles_per_H//4, I]` `float8_e4m3fn_x4` weight DMA — both `dge_mode=dge_mode.swdge`.

> **CORRECTION (D-O29 §3) — QKV scale/weight DMAs are `swdge`, MoE weight DMAs are `hwdge`.** The QKV MX DMAs (`qkv_cte.py:1880`, `:1896`) use `dge_mode.swdge`; the MoE weight/bias DMAs use `dge_mode.hwdge` (`bwmm_shard_on_I_mx.py:1137`). The MoE *scale gathers* carry no `dge_mode` at all (default). The QKV scale load is a **static 4-quadrant loop** (`nl.affine_range(4)`, `qkv_cte.py:1871`), not the `-1` vector index — the vector-DGE quadrant-hole gather is the MoE-specific mechanism.

---

## The `nc_matmul_mx` Handoff

### Purpose

The loaded x4 data and its E8M0 scale ride into `nc_matmul_mx` as **paired operands**: the descriptor packs a DATA ADDR4 and a SCALE ADDR4 back-to-back ([MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md)), and the NKI call surfaces that pairing as the kwargs `stationary`/`stationary_scale` and `moving`/`moving_scale`. The x4-unpack and the per-32-block dequant happen *inside* the descriptor; the kernel just hands over the two SBUF buffers it built.

### Algorithm — the 4-operand call

```c
// gate_up_projection_mx_tp_shard_I — bwmm_shard_on_I_mx.py:1695-1715
for h_tile_idx in range(n_H512_tile):                 // contraction tiled H
  for i_tile_idx in range(n_total_I512_tile_lnc_sharded):  // then I
    for i_mm_tile_idx in range(_q_width):             // then _q_width(4) — the 4 I128 sub-tiles of one I512
      nc_matmul_mx(
        dst              = out_psum_lst[i_tile_idx][:cur_I128, i_mm_tile_idx, :BxS],
        stationary       = weight_qtz_sb[:, h_tile, wI : wI+cur_I128],     // x4 weights      :1705
        moving           = hidden_qtz_sb[:, h_tile, bxs : bxs+BxS],        // fp8 activations  :1706
        stationary_scale = weight_scale_sb[:, h_tile, wI : wI+cur_I128],   // E8M0 weight scale:1709
        moving_scale     = hidden_scale_sb[:, h_tile, bxs : bxs+BxS])      // E8M0 act scale   :1712
```

Down swaps the operand roles — activations are stationary, weights are moving — with the scales swapped to match:

```c
// down_projection_mx_shard_I — bwmm_shard_on_I_mx.py:1837-1843
nc_matmul_mx(dst = psum_bank,
             stationary       = inter_qtz[:, i_tile, BxS_offset : …],       // quantized INTER acts
             moving           = weight_qtz[:, i_tile, H_offset : …],        // x4 weights
             stationary_scale = inter_qtz_scale[:, i_tile, …],
             moving_scale     = weight_qtz_scale[:, i_tile, …])
```

> **NOTE — activations are always FP8, even with FP4 weights.** Only the *weights* may be FP4 (`fp4_e2m1fn_x4`); the moving/stationary activation is always `fp8_e4m3fn_x4`, quantized online by `quantize_mx` (`_down_proj_prep_inter_and_weights`, the activation-side path). `nc_matmul_mx` requires FP8 activations regardless of weight precision. The kwarg names (`stationary_scale`/`moving_scale`) map straight onto the descriptor's stationary/moving E8M0 scale operands ([PE matmul encoding](../isa/pe-matmul-encoding.md)).

> **NOTE — `target_dtype` inheritance forbids a mixed gate/down pair.** At the load entry, `down_proj_weight` inherits gate/up's resolved `target_dtype`: `gate_up_proj_weight, target_dtype = convert_to_mxfp_dtype(gate_up_proj_weight, weight_dtype)` then `down_proj_weight, _ = convert_to_mxfp_dtype(down_proj_weight, target_dtype)` (`bwmm_shard_on_I_mx.py:244-245`, `bwmm_shard_on_block_mx.py:254-255`). Passing the gate/up target into the down conversion makes a mixed MXFP4-gate / MXFP8-down pair structurally impossible in one kernel instance.

---

## End-to-End Primitive Sequence (one expert block)

```text
view-cast    TensorView(w_alt).reinterpret_cast(fp4x4 / fp8x4)        # metadata relabel, no copy
weight DMA   dma_copy(select(E=block_expert).slice(LNC), hwdge)       # indirect DGE, no sw gather
scale index  iota + memset(-1) + scalar_tensor_tensor + nc_transpose + tensor_copy
                → [128,1] int32 quadrant-hole index (4 valid / 32, rest -1)
scale gather dma_copy(scale_view.ap(vector_offset=idx, indirect_dim=0), oob_mode=skip)
                → -1 rows skipped, dst pre-memset 0 → SBUF holes
bias DMA     dma_copy(select(E=block_expert), indirect_dim=0, hwdge)
act quantize quantize_mx(bf16 acts → fp8_e4m3fn_x4 data + uint8 E8M0 scale)   # activation side
matmul       nc_matmul_mx(stationary, moving, stationary_scale, moving_scale)
   (QKV path adds dma_transpose for the MX_INTERLEAVED activation swizzle)
```

The HW x4-unpack (×4 expansion of the packed K-extent) and the per-32-block E8M0 dequant happen **inside `nc_matmul_mx`'s descriptor** ([MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md), [PE matmul encoding](../isa/pe-matmul-encoding.md)) — not in NKI source.

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the wheel source:

| Claim | Challenge | Resolution |
|---|---|---|
| The weight view-cast is a no-copy bitcast | Could `reinterpret_cast` silently reshape? | **CONFIRMED, refined.** Same-width alternates (2≡2, 4≡4) hit `tensor_view.py:146-147` `if old_size==new_size: return self._copy(dtype=new_dtype)` — pure relabel, no shape/stride/offset change. The resize arms (`:160`/`:186`) are unreachable for MX. |
| The scale gather is a `-1` quadrant-hole indirect DGE | Is the `-1` skip real, or is the index dense? | **CONFIRMED.** `_generate_expert_index_vector:984-985` memsets the whole `[1,128]` to `-1`, `scalar_tensor_tensor:987` scatters only 4 per quadrant; the gather (`:1190`) is `oob_mode=oob_mode.skip` — `-1` rows are not written. Holes are pre-zeroed. |
| `MX_CONTIGUOUS` and `MX_INTERLEAVED` differ only in row occupancy | Same footprint, or different sizes? | **CONFIRMED.** Both end `[H//4, I]` x4 per the enum docstring (`common_types.py:77`, `:86`); `MX_INTERLEAVED` is `MX_CONTIGUOUS` + the `(2,H//4,2)` row permutation, re-packed to the same shape. |
| `_generate_expert_index_vector` is the shared helper | D-O29 said it is *in* `moe_cte_mx_utils.py` | **CONFIRMED.** Defined `moe_cte_mx_utils.py:948`, imported by both bwmm callers (`bwmm_shard_on_I_mx.py:60`, `bwmm_shard_on_block_mx.py:41`). The gather *DMA* bodies, however, live in the callers, not the util. |
| All four int alternates and only those convert | Do `float16`/`float32` auto-convert? | **CORRECTED.** The docstring mentions float alternates, but `_ALTERNATIVE_DTYPE_TO_MXFP:61-66` has only `uint16/int16/uint32/int32` keys. Float inputs fall through to no-relabel. Flagged in §1 GOTCHA. |

**Re-verification ceiling.** Everything on this page is grounded in the readable NKI-DSL Python (`fd --no-ignore` against the cp310 tree; cp311/cp312 byte-identical). The `nisa.*` primitives (`nc_matmul_mx`, `dma_copy` with `vector_offset`/`indirect_dim`, `scalar_tensor_tensor`, `nc_transpose`) are Cython intrinsics whose *internal* lowering to the MXMEM_PATTERN1D descriptor and the HW x4-unpack is **not** traced here — that boundary is owned by [MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md) and [PE matmul encoding](../isa/pe-matmul-encoding.md), and those pages confirm the descriptor side independently (block_size 32, dtype tags 2/8/9, `n <<= 2`). The handoff between this source layer and that descriptor layer is INFERRED from the matching operand shapes (`[P/8, F/4]` E8M0), not from a byte-traced call into the Cython intrinsic.

---

## Related Components

| Name | Relationship |
|---|---|
| [MoE Context/Prefill (CTE)](moe-cte-prefill.md) | The two MX MoE kernels (`bwmm_shard_on_*_mx`) that call this load path per expert block |
| [Projection Kernels](projection-kernels.md) | The QKV MX kernels that select `MX_CONTIGUOUS` / `MX_INTERLEAVED` and drive the static 4-quadrant scale DMA |
| [MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md) | The descriptor that pairs the loaded data + E8M0 scale addresses and performs the HW x4-unpack |
| [PE Matmul Encoding](../isa/pe-matmul-encoding.md) | The `LdWeightMx` / `MatmultMx` bundles `nc_matmul_mx` lowers to; the `[P/8, F/4]` scale operand and x4 dtype legality |
| [MX-FP8 Microscaling Legalization](../hlo-opt/mx-fp8-legalization.md) | The activation-side `quantize_mx` and the MX numeric format these scales encode |

## Cross-References

- [MoE Context/Prefill (CTE)](moe-cte-prefill.md) — the `bwmm_shard_on_I_mx` / `bwmm_shard_on_block_mx` kernels whose per-block load is documented here.
- [Projection Kernels: Fused QKV & Output-Projection](projection-kernels.md) — the QKV MX layout enum and the static-loop scale DMA twin of this gather.
- [MXMEM_PATTERN1D — MX Data + E8M0 Scale](../isa/mxmem-pattern1d.md) — the on-chip descriptor that consumes the data+scale operands this page produces; owns the x4-unpack `n <<= 2` and block_size 32.
- [PE Matmul Encoding — Dense / Sparse / MX & Quantize](../isa/pe-matmul-encoding.md) — the `nc_matmul_mx` bundle encoding, x4 dtype wire tags (2/8/9), and the `[P/8, F/4]` E8M0 scale operand layout.
- [MX-FP8 Microscaling Legalization](../hlo-opt/mx-fp8-legalization.md) — the graph-level MX legalization and the `quantize_mx` that produces the activation-side FP8 + E8M0 these matmuls consume.
- [nki.isa COMPUTE Intrinsics](isa-compute-intrinsics.md) — the `nisa.*` primitives (`nc_matmul_mx`, `dma_copy`, `scalar_tensor_tensor`, `nc_transpose`, `iota`, `memset`) used throughout.
