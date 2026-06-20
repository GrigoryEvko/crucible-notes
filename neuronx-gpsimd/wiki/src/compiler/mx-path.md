# The MX Microscaling Path (end-to-end)

This page traces the **OCP Microscaling (MX)** path of the NKI / `neuronx-cc`
compiler **end-to-end** — from the `nki.isa.quantize_mx` / `nki.isa.nc_matmul_mx`
host surface, through the SBUF scale-scatter layout, down to the **exact device
opcodes** on three different engines — and reconciles it field-for-field with the
firmware decode ([the PE MX path](../firmware/kernels/pe-matmul.md) and the
[FW `0x7b` dequant datapath](../firmware/kernels/mx-dequant.md)), the
[unified datatype model](../firmware/kernels/dtype-model.md) (E8M0), and the
[140-opcode ledger](../firmware/kernels/opcode-catalog-ledger.md).

It carries **one headline CORRECTION** the earlier compiler-seam analysis got
wrong: **the forward quantize-pack and the inverse dequant are two distinct device
opcodes on two distinct engines** — they were conflated as a single `0x7b`. §0 and
§6 settle which is which against the device decode.

Where the parent firmware pages decode the *device bodies*, this page is the
**compiler-side lowering map**: the value semantics are driven from the shipped
on-device value oracle — the nki simulator `backends/simulator/advanced.py`, a plain
binary-derived `.py` that ships byte-identical across the cp310/311/312 wheels
(`…/neuronx-misc/extracted/nki-0.3.0+…/nki/backends/simulator/advanced.py`). The host
emitter is the Cython `SundaISel` `QuantizeMXCodeGen` (not stripped — pyx symbols +
docstrings survive); the block geometry is hard-pinned from the production weight-prep
constants. Every device-opcode numeric value is cross-checked against the firmware
ledger; mismatches are flagged as **CORRECTION**, never silently reconciled.

Confidence per [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/MED/LOW]` × `OBSERVED` (read-from-byte / shipped `.py` this pass) /
`INFERRED` (reasoned over OBSERVED) / `CARRIED` (re-used from a cited sibling page at
its confidence). The `extracted/` trees are gitignored — reach them with
`fd --no-ignore` or an absolute path.

> **NOTE — two unrelated "MX" mechanisms, do not conflate.** There are *two*
> microscaling surfaces in this hardware and they share only the marketing word.
> **(1)** The POOL `TensorDequantize 0x7b` *in-band block-of-8* dequant (NC-v3+) —
> covered by [the MX dequant page](../firmware/kernels/mx-dequant.md); its scale
> rides **inside** a group of 8 source codes and it is **not** the OCP block-of-32.
> **(2)** The OCP-MX surface this page traces — `quantize_mx` (forward pack),
> `nc_matmul_mx` (MX matmul) — uses an **out-of-band E8M0** shared-exponent scale
> over a **32-element block** (8 partitions × 4 elements). When this page says "MX"
> unqualified it means surface (2). §6.4 reconciles the two. `[HIGH/OBSERVED both
> contracts.]`

---

## 0. TL;DR — the model in eight facts

1. **The forward pack and the inverse dequant are different opcodes on different
   engines.** Forward `nki.isa.quantize_mx` lowers to **`0xE3 QUANTIZE_MX` on the
   DVE (Vector) engine**; the standalone inverse dequant is **`0x7B
   TENSOR_DEQUANTIZE` on the POOL (GpSimd) engine**. The earlier compiler-seam pass
   bound `quantize_mx → 0x7b`; that is the **headline CORRECTION** (§6.2).
   `[HIGH — forward engine OBSERVED in the nki binding; the `0xE3` numeric
   triangulated INFERRED-HIGH, §2.3.]`
2. **The MX matmul runs on the PE (Tensor) engine with integrated dequant.** On
   **v4 it is a separate op pair** — `0x09 LDWEIGHTS_MX` (`SMX1_LW_STRUCT`) +
   `0x0A MATMUL_MX` (`SMX1D3_MM_STRUCT`); on **v5 it folds into `0x01 LDWEIGHTS` /
   `0x02 MATMUL`** via the `MEM_PATTERN3D` `MXTENSOR_V2` union member. `[HIGH/CARRIED
   — pe-matmul §2/§7 + ledger §2.1.]`
3. **The shared exponent is E8M0** — `SFP8_E8 = 0x13 = FP8_S0E8M0`, a `uint8`,
   bias 127, no sign bit, scale-only (never a compute element). `[HIGH/OBSERVED —
   dtype-model §4 + the shipped `ScaleDtype.F8E8M0` / `bias 127`.]`
4. **The 32-element MX block = 8 partitions × 4 elements.** `Q_WIDTH = 4`,
   `Q_HEIGHT = 8`, `mx_block_size = Q_WIDTH × Q_HEIGHT = 32`; one E8M0 scale per
   32-element group. `[HIGH/OBSERVED — hard constants in the shipped layout
   transform.]`
5. **The shared-exponent extract is a MAX-over-block of the raw biased fp32
   exponent.** The forward op reads `(src.view(uint32) >> 23) & 0xFF` (the 8-bit
   biased exponent — a direct bit-field read, no `frexp`), takes the max over the
   8×4 block, subtracts `max_exp`, and stores `uint8` E8M0. `[HIGH/OBSERVED — the
   shipped sim value model, §4.]`
6. **The data leg divides, clamps, and ×4-packs.** Divide by `2^(scale−127)`, clamp
   to `±max_val` (`448.0` for `e4m3fn`, `57344.0` for `e5m2`), cast to FP8, view as
   `uint32` (4 FP8 bytes per word). `[HIGH/OBSERVED — §4.]`
7. **The SBUF scale is scattered ×32 into the quadrants** — 16 scales at partitions
   `0-3, 32-35, 64-67, 96-99`; the forward scatter and the dequant gather are exact
   inverses. `[HIGH/OBSERVED — the `SundaISel` emitter docstring + both sim legs,
   §5.]`
8. **The E8M0 multiply is bit-exact across four altitudes** — the host torch
   `torch.ldexp(sub, scale−127)`, the sim `2.0**(scale−127)×`, the FW-75 scale-MAC
   (`ivp_mulus4tan16xr16` / `ivp_dmulqa2n8xr8`), and the ISS `xdref_addexp` (fp
   exponent-add = `ldexp`). `[HIGH — host/sim OBSERVED; FW + ISS CARRIED, §7.]`

---

## 1. The MX surface — who calls what

Three host call sites reach the same two device primitives — a production driver, a
torch/numpy reference oracle, and the MoE wiring. All confirmed from the shipped
`.py`. `[HIGH/OBSERVED]`

| caller | reaches | role |
|---|---|---|
| `moe_fused_tkg_mx.py` (`@nki.jit` MoE token-gen) | `quantize_mx` on activations + `nc_matmul_mx` on (MXFP8 act × MXFP4 weight) | the production driver; weights arrive **pre-packed** MXFP4 + pre-scaled |
| `mx_torch.quantize_mxfp8` / `matmul_mx` + the nki sim `quantize_mx` / `nc_matmul_mx` | the bit-exact value oracle | the reference the device must match (§4) |
| `expert_mlps_mx.all_expert_mlps_act_mxfp8_w_mxfp4` | `swizzle(.T) → quantize_mxfp8 → matmul_mx → swiglu → quantize_mxfp8 → matmul_mx` | the MoE wiring that **manufactures** the 8×4 block geometry before the quantize |

The MoE wiring is where the block geometry is *produced*: `swizzle` maps
`[M,N] → [M/4, N*4]` (`reshaped.transpose(1,2).reshape(M//4, N*4)`), and its shipped
docstring is explicit — *"Takes contiguous blocks of shape [32, 1] and converts to
contiguous blocks of [8, 4]."* That is the 8-partition × 4-element MX group the
device expects, manufactured **before** the quantize. `[HIGH/OBSERVED — `swizzle.py`
docstring + body.]`

```
norm_out[T,H]
   │  swizzle(norm_out.T)            # [H//4, T*4] — [32,1] blocks -> [8,4] blocks
   ▼
quantize_mxfp8  ──► (act_x4 [H//4, T], act_scale)        ┐ FORWARD pack
   │                                                      │   = 0xE3 QUANTIZE_MX (DVE)
   ▼                                                      ┘
nc_matmul_mx(gate/up weight_x4 [MXFP4], weight_scale) ──► gate/up   ┐ MX matmul
   │  swiglu(α=1.702, limit=7.0)                                     │   = PE: v4 0x09+0x0A
   ▼                                                                 │        v5 0x01/0x02
quantize_mxfp8(swizzle(act.T)) ; nc_matmul_mx(down weight) ─────────┘
   │  expert-affinity weighted sum
   ▼  out[T,H]
```

> **GOTCHA — `quantize_mx` cannot produce MXFP4.** The forward op outputs **only**
> `float8_e5m2_x4` or `float8_e4m3fn_x4` (§2.2). `nc_matmul_mx` *accepts*
> `float4_e2m1fn_x4` as an input (the pre-packed weight leg), but there is **no**
> on-device op that quantizes a tensor *to* MXFP4 — MXFP4 weights arrive pre-packed
> from the host weight-prep (`transform_weights.py`). A reimplementer must not expect
> a symmetric "quantize to MXFP4" primitive. `[HIGH/OBSERVED — `validate_quantize_mx_shapes`
> dst set vs the `nc_matmul_mx` input set.]`

---

## 2. The forward op — `quantize_mx → 0xE3 QUANTIZE_MX` (DVE)

### 2.1 The binding chain

The frontend lowers `quantize_mx` through a fixed-engine path; there is **no engine
kwarg** — the engine is pinned to **Vector** at the irbuilder/backend. The nki
binding states the engine **verbatim**: the irbuilder binding
(`compiler/kernel_builder/nisa_apis.py:1996`) reads *"Apply on-the-fly quantization
from source tile to destination tile in OCP Microscaling (MX) formats using Vector
Engine. **Engine: Vector**"*, and the public `isa/advanced.py:288` API reads
*"Quantize FP16/BF16 data to MXFP8 tensors (both data and scales) using Vector
Engine."* `[HIGH/OBSERVED — shipped `.py` binding strings.]`

```text
nki.isa.quantize_mx(dst, src, dst_scale)        # isa/advanced.py
  → validate_quantize_mx_shapes                 # isa/validation.py:905
  → get_backend().quantize_mx(...)              # → emit_quantize_mx (mlir_tracer/isa_emit.py)
  → _nki_irbuilder.quantize_mx(...)             # nisa_apis.py:1996 "Engine: Vector"
  → SundaISel.transformTQuantizeMXOperator → QuantizeMXCodeGen   (§5)
  → device 0xE3 QUANTIZE_MX  (DVE / Vector)     # ledger §2.3: 0xE3 | DVE | --YYY
```

### 2.2 The shape / dtype contract (`validate_quantize_mx_shapes`)

Read byte-exact from the shipped `isa/validation.py:905-938`. `[HIGH/OBSERVED]`

| field | constraint | why |
|---|---|---|
| `src.dtype` | `∈ {float16, bfloat16}` | **not fp32** — the host surface restricts input; the fp32 upcast + biased-exponent read happen **inside** the op (§4) |
| `dst.dtype` | `∈ {float8_e5m2_x4, float8_e4m3fn_x4}` | the ×4-packed FP8 — a 32-bit word holding 4 FP8 bytes (note the `e4m3**fn**` variant) |
| `dst_scale.dtype` | `== uint8` | the E8M0 shared exponent |
| `src.shape[0] % 32 == 0` | partition dim multiple of 32 (≤128) | the quadrant constraint (§5) |
| `prod(src.shape[1:]) % 4 == 0` | free dim multiple of 4 | the ×4 pack |

`dst` free dim `= src` free dim `/ 4` (the ×4 pack); `dst` partition dim `== src`
partition dim.

### 2.3 Why `0xE3`, not `0x7b` — the adjudication

Three independent OBSERVED facts converge on `0xE3` and **contradict** the earlier
`0x7b` binding on all three axes:

| axis | `quantize_mx` is… | `0xE3 QUANTIZE_MX` | `0x7B TENSOR_DEQUANTIZE` |
|---|---|---|---|
| **engine** | "Vector" (verbatim, both bindings) | **DVE** ✔ | POOL ✘ |
| **direction / shape** | a **pack** (extract + divide + FP8-cast) | QUANTIZE\_MX (pack) ✔ | TENSOR\_DEQUANTIZE (unpack) ✘ |
| **gen availability** | ".pyi: *Available only on NeuronCore-v4 and newer*" | `--YYY` (v4+) ✔ | `YYYYY` (all gens) ✘ |

An op the frontend explicitly calls **Vector** cannot lower to the POOL `0x7b`
opcode; a forward **pack** is not the inverse **unpack**; and a v4-only op is not the
all-gen `0x7b`. The three constraints triangulate uniquely to `0xE3`. `[HIGH/INFERRED
— the strongest triangulation; the residual is below.]`

> **GOTCHA — the `SundaISel → 0xE3` numeric edge is forced, not byte-read.** The
> `QuantizeMXCodeGen.so` does **not** spell the numeric opcode in any string read
> this pass; the opcode is assigned downstream at the `isa_tpb` `get_isa_opcode`
> step (the same engine-poly mechanism the other compiler ops use). The `0xE3`
> binding is therefore **INFERRED-HIGH** — engine (Vector) + op-name (QUANTIZE pack)
> + gen-availability (v4-only) force it, and the firmware ledger independently lists
> `0xE3 QUANTIZE_MX | DVE` as a real, dedicated opcode separate from `0x7b`. The
> *forward* DVE FLIX body is not byte-decoded this pass (§7). `[flagged honestly.]`

---

## 3. The MX matmul — `nc_matmul_mx → PE` (v4: `0x09`+`0x0A`; v5: `0x01`+`0x02`)

### 3.1 The binding chain

`nc_matmul_mx` is single-engine — the kernel-builder pops the engine kwarg
(`compiler/kernel_builder/isa.py:191` *"matmul_mx is single-engine, Layer 2 doesn't
accept engine"*) — and accumulates the MAC count, so it is a true PE matmul. The
binding names the engine **verbatim**: *"Compute matrix multiplication between two
quantized matrices using Tensor Engine. … **Engine: Tensor.**"* (`isa.py:167-176`);
the public `isa/advanced.py:182` corroborates *"…with integrated dequantization using
Tensor Engine"* on a "systolic array with 128 rows and 128 columns of PEs". `[HIGH/OBSERVED]`

```text
nki.isa.nc_matmul_mx(dst, stationary, moving, stationary_scale, moving_scale,
                     tile_position, tile_size, accumulate)   # isa/advanced.py
  → get_backend().nc_matmul_mx(...)  → emit_matmul_mx        # mlir_tracer/isa_emit.py
  → _nki_irbuilder.matmul_mx(...); accumulate_mac_count       # isa.py "Engine: Tensor"
  → device: v4  0x09 LDWEIGHTS_MX + 0x0A MATMUL_MX
            v5  0x01 LDWEIGHTS / 0x02 MATMUL  + MXTENSOR_V2
```

### 3.2 The layout / tile contract (`isa/advanced.py` docstring)

`[HIGH/OBSERVED]`

- **Contraction dim** = the **partition** dim of `stationary` & `moving` **AND** the
  ×4 dim within each packed element. A 32-element MX block therefore spans **8
  partitions × 4 elements/partition** — the micro-scaling group = 32 = 8 × 4.
- `stationary`/`moving` partition dim: **multiple of 32, ≤128** (same quadrant rule
  as `quantize_mx`).
- `stationary` free dim: **even, ≤128** (→ dst partition dim). `moving` free dim:
  **≤512** (dst fp32) or **≤1024** (dst bf16) (→ dst free dim).
- inputs `∈ {float8_e5m2_x4, float8_e4m3fn_x4, float4_e2m1fn_x4}`; scales `uint8`;
  dst `∈ {float32, bfloat16}` in **PSUM** (the sim asserts *"nc_matmul_mx dst dtype
  must be float32 or bfloat16"*). All four operands (stat / mov / stat_scale /
  mov_scale) are **SBUF**; dst is **PSUM**.
- row-tiling: 128 PE rows sliced `2×64` or `4×32`
  (`tile_size=(row_size,128)`, `tile_position=(start_row,0)`).

### 3.3 The PE-side opcode truth (CARRIED — pe-matmul §2/§7)

| gen | ops | structs | operand carrying the scale |
|---|---|---|---|
| **v4 (MARIANA / M+)** | `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` | `SMX1_LW_STRUCT` / `SMX1D3_MM_STRUCT` (both `sizeof==64`) | `MXMEM_PATTERN1D` `{t: MXTENSOR1D \| i: MXINDIRECT16B}` with an **out-of-band** `scale_addr` (E8M0 per-block) + `data_addr` |
| **v5 (MAVERICK)** | folds into `0x01 LDWEIGHTS` / `0x02 MATMUL` | `MEM_PATTERN3D` union | the `MXTENSOR_V2` member (selected by the `start_addr` `ADDR4MARKER_MXTENSOR_V2 = 0x01` LSB), with its own `scale_addr` |

v4 inputs `∈ {FP8_E4M3, FP8_E5M2, FP4_E2M1}`, scale dtype `SFP8_E8 = 0x13`, 128-row
array (`num_active_rows ∈ {128,64,32}`), dst → PSUM with FP32-internal accumulation.
On v5 the `MX_PERF_MODE {QUAD_ROW=0x1 / OCT_ROW=0x4}` pumps 4×/8× rows per cycle. The
firmware header is explicit that v5 *"replaces the separate LdWeightMX/MatmulMX
instruction from previous chips."* `[HIGH/CARRIED — pe-matmul §7 struct + validity
selector; ledger §2.1 `0x09`/`0x0A` `--YYY`.]`

> **CORRECTION (sharpening) — the v4 device truth is the dedicated `0x09`/`0x0A`
> pair, not a unified `0x02 (mx)`.** The earlier compiler-seam analysis described the
> MX matmul as "`MATMUL 0x02 (mx)`". That is **correct for v5 / the host-abstract
> view** but **incomplete for v4**: on MARIANA the MX matmul is the separate `0x09
> LDWEIGHTS_MX` + `0x0A MATMUL_MX` ops with the dedicated `SMX1_LW` / `SMX1D3_MM`
> structs. A reimplementer targeting v4 must emit the separate pair; only v5 folds MX
> into `0x01`/`0x02` via `MXTENSOR_V2`. `[HIGH — pe-matmul §2 dispatch chain
> byte-decodes `bnei a2,9` / `bnei a2,10` → distinct stubs `0x2baf` / `0x2bb7`.]`

---

## 4. The forward value model — the shared-exponent extract + pack

The shipped on-device-op sim (`backends/simulator/advanced.py:136-192`, the value
oracle for `0xE3`) is the bit-exact reference. Reproduced as annotated C pseudocode
naming the real sim operations, per 8×4 block:

```c
/*
 * quantize_mx  —  the value model of device 0xE3 QUANTIZE_MX (DVE).
 * Source of truth: nki backends/simulator/advanced.py:136-192 (shipped, binary-derived).
 * src in {fp16, bf16}; dst in {e4m3fn_x4, e5m2_x4}; dst_scale uint8 (E8M0).
 * Per-(8 partition x 4 element)-block. num_active lanes run in parallel on the DVE.
 */
void quantize_mx(const f16 *src, u32 *dst, u8 *dst_scale, int P, int F,
                 mx_fmt_t fmt /* e4m3fn_x4 | e5m2_x4 */)
{
    const f32  src_data[P][F];                 /* src upcast to fp32 INSIDE the op           */
    const int  SP = P / 8, SF = F / 4;         /* the 8x4 block grid                         */
    const int  max_exp = (fmt == e4m3fn_x4) ?  8 : 15;       /* e4m3fn: 8/448 ; e5m2: 15/57344 */
    const f32  max_val = (fmt == e4m3fn_x4) ? 448.0f : 57344.0f;
    const int  float32_exp_bias = 127;

    /* ---- SHARED-EXPONENT EXTRACT (the scale-extraction op) ---- */
    u32 exp[P][F];
    for (p, f) exp[p][f] = (bitcast_u32(src_data[p][f]) >> 23) & 0xFF;  /* raw 8-bit BIASED fp32 exp */

    u8 mx_scale[SP][SF];                       /* one E8M0 per 8x4 block */
    for (int bp = 0; bp < SP; ++bp)
      for (int bf = 0; bf < SF; ++bf) {
        u32 m = 0;                             /* MAX over the 8x4 block of the biased exp */
        for (int i = 0; i < 8; ++i)
          for (int j = 0; j < 4; ++j)
            m = max(m, exp[bp*8 + i][bf*4 + j]);
        mx_scale[bp][bf] = (u8)(m) - max_exp;  /* cast uint8 THEN subtract max_exp -> E8M0 */
      }

    /* ---- DATA LEG: divide by scale, clamp, x4-pack ---- */
    for (p, f) {
        i32 scale_exp = (i32)mx_scale[p/8][f/4] - float32_exp_bias;   /* E8M0 -> exponent */
        f32 s = exp2f((f32)scale_exp);                               /* 2 ** scale_exp   */
        f32 q = clampf(src_data[p][f] / s, -max_val, +max_val);      /* np.clip          */
        fp8  packed = cast_fp8(q, fmt);                             /* RNE              */
        store_x4(&dst[p][f/4], packed);                            /* 4 FP8 bytes -> one u32 word */
    }

    /* ---- SCALE SCATTER: x32 quadrant placement (the SBUF layout, §5) ---- */
    if (dst_scale_shape0 == P)
      for (int q = 0; q < SP/4; ++q)                      /* the x32 quadrant stride */
        copy_rows(&dst_scale[32*q], &mx_scale[4*q], 4);   /* land at 0-3, 32-35, 64-67, 96-99 */
    else
      write_compact(dst_scale, mx_scale);                 /* shape0 != P -> dense, no scatter */
}
```

`[HIGH/OBSERVED — the extract (`(src_data.view(uint32) >> 23) & 0xFF` →
`np.max(..., axis=(1,3))` → `.astype(uint8) − max_exp`), the divide
(`2.0**(mx_scale.astype(int32) − 127)`), the `np.clip(±max_val)` + `.astype(target) →
.view(uint32)` pack, the constants `8/448.0` and `15/57344.0`, and the scatter loop
are all read byte-exact from the shipped sim.]`

> **QUIRK — the cast-to-`uint8` happens *before* the `−max_exp` subtract.** The sim
> writes `mx_scale = np.max(exp_reshaped, axis=(1,3)).astype(np.uint8) - max_exp` —
> the max-over-block is narrowed to `uint8` and *then* `max_exp` (8 or 15) is
> subtracted, so the subtraction is in `uint8` arithmetic (the implicit numpy
> promote-then-store). The downstream `mx_scale.astype(np.int32) - 127` re-widens for
> the divide. A reimplementer must preserve this order (narrow-then-subtract), not
> subtract in wider precision first. `[HIGH/OBSERVED — sim line order.]`

> **NOTE — two `max_exp` conventions, reconciled.** The sim subtracts `max_exp` from
> the **raw biased** max-exponent; the torch reference `mx_torch` adds the bias then
> subtracts (`max_exp_per_block + 127 − max_exp`). Both yield the same E8M0 — the
> sim's exponent is already biased (+127) while the torch `frexp` exponent is
> unbiased, and the `+127` reconciles them. `use_unbiased_scale` toggles `max_exp`
> between `8` (unbiased) and the torch value — a precision/bias tradeoff, **not** a
> layout difference. `[HIGH/OBSERVED both; reconciliation INFERRED-HIGH.]`

**Direction verdict.** `0xE3 quantize_mx` is unambiguously the **forward pack**:
scale-EXTRACT (max-over-block exponent) → DIVIDE-by-scale → FP8-PACK. The inverse
(multiply-by-scale) lives in (a) `nc_matmul_mx`'s integrated PE dequant (§3) and
(b) the standalone `0x7B TENSOR_DEQUANTIZE` POOL reference path (§6). `[HIGH/OBSERVED
— both sim legs.]`

---

## 5. The SBUF scale layout — forward scatter / inverse gather (the ×32)

### 5.1 The host emitter — `SundaISel` `QuantizeMXCodeGen`

The Cython `QuantizeMXCodeGen` is the host emitter of the scatter; its
`_create_scale_access_pattern` docstring is read byte-exact (the `.so` is **not**
stripped — pyx symbols + docstrings survive):

> *"Create strided access pattern for scale tensor on SBUF. The scale tensor needs a
> strided layout to place scales in the correct hardware quadrants. SBUF has 32
> partitions per quadrant, and scales must be placed in the quadrant from which their
> source data originated. For `partition_axes = [scale_in_quadrant_idx,
> quadrant_idx]`:*
> - *Standard linear:  `scale_in_quadrant*1 + quadrant*4`*
> - *Strided for SBUF: `scale_in_quadrant*1 + quadrant*32`*
>
> *This places 16 scales at positions: 0-3, 32-35, 64-67, 96-99."*

The companion `codegenQuantizeMXOperator` docstring pins the two outputs and the axis
roles: *"Output[0]: Quantized data (FP8_x4); Output[1]: Scale (uint8); `par_indices[0]
= pack axis` (inner reduction, skipped for the scale partition); `par_indices[1:] =
scale block axis` (e.g. tripcount=16); `free_indices[0] = elem axis` (packed into
FP8_x4, skipped for output free dims)."* `[HIGH/OBSERVED — pyx docstrings.]`

### 5.2 The forward scatter ↔ inverse gather (sim, byte-exact)

The forward op and the matmul's `_extract_scales` helper
(`backends/simulator/advanced.py:12-25`) are **exact inverses**:

```c
/* FORWARD scatter  (quantize_mx, when dst_scale.shape[0] == P): */
for (q = 0; q < SP/4; ++q)                          /* the x32 stride */
    scale_out[32*q .. 32*q+3][:] = mx_scale[4*q .. 4*q+3][:];
/*   -> the q-th quadrant's 4 scales land at SBUF partitions 32q .. 32q+3.            */
/*   When dst_scale.shape[0] != P the scales are written COMPACT (no scatter).        */

/* INVERSE gather  (_extract_scales, when scale.shape[0] == K): */
num_scale_rows = K / 8;
for (q = 0; q < num_scale_rows/4; ++q)
    compact[4*q .. 4*q+3][:F] = scale[32*q .. 32*q+3][:F];    /* the inverse pull */
/*   then np.repeat(., 8, axis=0)[:K] ; mult = 2.0**(scale_expanded - 127) ;          */
/*        einsum("kiq,kjq->ij", stat_scaled, mov_scaled).                             */
```

The docstring's *"scale tensors have partition dimensions that depend on whether the
data tensors span multiple quadrants"* branch is exactly this **`shape[0]==P`
(scattered) vs `!=P` (compact)** dichotomy — in the sim, `dst_scale.shape[0] == P`
forward and `scale.shape[0] == K` inverse. `[HIGH/OBSERVED — both sim legs + emitter
docstring.]`

### 5.3 The block geometry, hard-pinned

Grounded in **hard constants**, not inference — every one independently re-read this
pass from the shipped production weight-prep:

| constant | value | source (shipped `.py`) |
|---|---|---|
| `Q_WIDTH` | `4` | `gpt_oss/mx_layout_transform.py:14` |
| `Q_HEIGHT` | `8` | `mx_layout_transform.py:15` |
| `mx_block_size = Q_WIDTH × Q_HEIGHT` | `32` | `mx_layout_transform.py:145` |
| `q_blocks_per_H_tile = 512 // (Q_WIDTH × Q_HEIGHT)` | `16` | `mx_layout_transform.py:113` |
| `assert q_blocks_per_H_tile × Q_HEIGHT == 128` | `16 × 8 = 128` partitions | `mx_layout_transform.py:176` |
| `ScaleDtype.F8E8M0` | `torch.uint8`, bias `127` | `quantization_config.py:134,137-138` |
| `F8E8M0_MAX / _MIN` | `2.0**(255−127)` / `2.0**(−127)` | `quantization_config.py:40-41` |

So a full SBUF partition column = 16 quad-blocks × 8 = 128 partitions; the on-disk
weight is `[E, H//4, 2I[x4]]` and the scale is `[E, H//32, 2I]` — one E8M0 per
32-element group. The ×4-packed dtype byte-counts: `fp8_x4 = uint32` (4×8b),
`fp4_x4 = uint16` (4×4b). This ties the `[8,4]=32` block, the 16-scale quadrant, and
the 128-partition tile **field-for-field**. `[HIGH/OBSERVED — constants re-read this
pass.]`

---

## 6. The opcode ledger — the MX cluster, engine-pinned

The MX cluster spans **three engines** and **two directions**. Every numeric value
is cross-checked against the [140-opcode ledger](../firmware/kernels/opcode-catalog-ledger.md)
(`§2.1` PE, `§2.3` DVE, `§2.4` POOL):

| host op (nki) | device opcode | engine | gen (ledger) | FW kernel / struct |
|---|---|---|---|---|
| `quantize_mx` (FWD pack) | **`0xE3 QUANTIZE_MX`** | **DVE** (Vector) | `--YYY` | `S3DMX1_QUANT` (forward extract + ÷) |
| *(standalone dequant)* | `0x7B TENSOR_DEQUANTIZE` | POOL (GpSimd) | `YYYYY` | `S3D3_TENS_DEQUANT` · `proc_4bit_mx_8` (unpack + ×scale) |
| `nc_matmul_mx` (v4) | `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` | PE (Tensor) | `--YYY` | `SMX1_LW` / `SMX1D3_MM` |
| `nc_matmul_mx` (v5) | `0x01 LDWEIGHTS` / `0x02 MATMUL` + `MXTENSOR_V2` | PE (Tensor) | (v5) | `MXTENSOR_V2` in `MEM_PATTERN3D` |
| *(E8M0 scale dtype)* | `SFP8_E8 = 0x13 = FP8_S0E8M0` | — | — | scale-only, `uint8`, bias 127 |

`[opcode numerics + engine + gen-column CARRIED from the ledger §2.1/§2.3 and
[pe-matmul](../firmware/kernels/pe-matmul.md) §2/§7; host engine tags OBSERVED in the
nki bindings this pass; the `0xE3`-fwd binding INFERRED-HIGH per §2.3.]`

### 6.1 Forward vs inverse — the direction split

| | FORWARD pack | INVERSE dequant |
|---|---|---|
| host op | `quantize_mx` | (standalone) / folded into `nc_matmul_mx` |
| device op | `0xE3 QUANTIZE_MX` | `0x7B TENSOR_DEQUANTIZE` |
| engine | DVE (Vector) | POOL (GpSimd) |
| scale op | EXTRACT max-over-block, then **÷** `2^(s−127)` | **×** `2^(s−127)` |
| arithmetic | bit-field exp read → max → divide → clamp → FP8-cast | nibble-unpack → ufloat → scale-MAC → clamp → FP8 |
| gen | v4+ | all gens (NC-v3+ for the MX `grp8` body) |

### 6.2 The CORRECTION, explicit

> **CORRECTION — `quantize_mx` lowers to `0xE3 QUANTIZE_MX` (DVE), NOT `0x7B
> TENSOR_DEQUANTIZE` (POOL).** The earlier compiler-seam analysis bound (or
> co-listed) the **forward** `quantize_mx` to the POOL `0x7b` dequant opcode,
> reasoning "same MX-block machinery, opposite direction." That binding is **wrong
> for the on-device op**:
> - The forward pack is its own dedicated opcode **`0xE3 QUANTIZE_MX` on the DVE
>   (Vector) engine** — the very engine the nki frontend names verbatim, and a
>   separate ledger row from `0x7b`.
> - `0x7B TENSOR_DEQUANTIZE` on **POOL** is the **inverse** (the standalone dequant
>   datapath, `proc_4bit_mx_8`) **only** — the multiply-by-scale direction, never the
>   forward pack.
>
> The adjudication is forced on three axes (§2.3): engine (Vector ≠ POOL), op-name
> (QUANTIZE pack ≠ TENSOR\_DEQUANTIZE unpack), and gen-availability (`0xE3` is `--YYY`
> v4+, `0x7b` is `YYYYY` all-gens, while `quantize_mx`'s `.pyi` says v4+). A
> reimplementer must emit **`0xE3` on the DVE** for the forward quantize and reserve
> `0x7b` on POOL for the inverse. `[HIGH — engine OBSERVED; numeric INFERRED-HIGH.]`

### 6.3 The `0xE3` gen-availability — RESOLVED (floor = NC-v4 / MARIANA)

The shipped sources *appeared* to disagree on **which generation first ships `0xE3`**;
the divergence is now **RESOLVED** (floor = NC-v4 per
[mx-device-bodies §6.1](mx-device-bodies.md) / [dtype-engine-fanin-synthesis §A.5.1](dtype-engine-fanin-synthesis.md)).
The three rows below are reconciled by the per-arch header read in the CORRECTION beneath
the table — each `nc == Vn` gate is gen-local to its own header, not a global floor:

| source | `0xE3 QUANTIZE_MX` available on… |
|---|---|
| [opcode ledger](../firmware/kernels/opcode-catalog-ledger.md) §2.3 | `--YYY` = **MARIANA (v4)** / M+ / MAVERICK |
| this page §2.3 (the `.pyi` gating) | "v4 and newer" |
| [mx-dequant](../firmware/kernels/mx-dequant.md) §6.2 / [dtype-model](../firmware/kernels/dtype-model.md) §2.4 | **NC-v5 only** (`s3dmx1_quant_valid_nc: nc == V5`) |

> **CORRECTION — RESOLVED: the `0xE3` gen floor is NC-v4 (MARIANA); `nc == V5` is the
> maverick-header-LOCAL gate, not a global floor.** This page originally flagged the floor
> as a MED divergence. [mx-device-bodies §6.1](mx-device-bodies.md) and
> [dtype-engine-fanin-synthesis §A.5.1](dtype-engine-fanin-synthesis.md) **resolve** it by
> reading both per-arch `s3dmx1_quant.h` headers directly: the header is present in
> **mariana(v4) + maverick(v5), absent in sunda + cayman(v3)**, and each present header
> gates `0xE3` on **its own** core version — `mariana/.../s3dmx1_quant.h:142` reads
> `nc == V4`, `maverick/.../s3dmx1_quant.h:103` reads `nc == V5`. The `nc == V5` that
> [mx-dequant §6.2](../firmware/kernels/mx-dequant.md) and
> [dtype-model §2.4](../firmware/kernels/dtype-model.md) quote is the **MAVERICK** header's
> own gate (gen-local), not a claim that `0xE3` first ships on v5. So there is **no
> contradiction with the ledger** (`--YYY`) or the host `.pyi` ("v4 and newer"): the
> forward `QUANTIZE_MX` gen floor is **NC-v4 (MARIANA)**. A reimplementer should target the
> **v4** floor and use the per-target header's own `s3dmx1_quant_valid_nc` as the validity
> gate (`V4` on MARIANA, `V5` on MAVERICK). The **opcode number `0xE3` and the engine (DVE)
> were never in dispute**. `[HIGH/OBSERVED — both per-arch headers byte-read, per
> mx-device-bodies §6.1.]`

### 6.4 Reconciling the two "MX" surfaces

| | DEQUANT-side MX (`0x7b`) | OCP-MX (`0xe3` / `0x09` / `0x0A`) |
|---|---|---|
| direction | MX → FP8 (dequant) | data → MX (quantize) / MX matmul |
| block | **8 elements** (in-band) | **32 elements** (8 part × 4 elem), out-of-band |
| scale location | IN-BAND (group of 8) | OUT-OF-BAND (`scale_addr` tensor) |
| scale bit-format | not named (implicit) | **E8M0** (`SFP8_E8`) explicit |
| scale applied as | vector MULTIPLY (`pr<N>` MAC) | reciprocal-÷ (forward) / × (matmul dequant) |
| engine | POOL | DVE / PE |
| gen | NC-v3+ | v4+ (DVE/PE) |

They share only the word "microscaling." The OCP block-of-32 + E8M0 + out-of-band
`scale_addr` mechanism is the surface this page traces; the POOL block-of-8 in-band
dequant is a separate, older mechanism. `[HIGH/OBSERVED both contracts.]`

---

## 7. The firmware / value-model tie — the E8M0 multiply is real

The **inverse-direction** block-scale multiply is grounded at four altitudes, all
consistent — `2^(scale−127) × value`:

| altitude | the multiply | status |
|---|---|---|
| **host torch** | `transform_weights.get_mxfp8_tensor_from_uint32`: `scales = scales.to(int32) − 127` then `torch.ldexp(sub, exp)` | OBSERVED (`:278`, `:312`) |
| **device sim** | `nc_matmul_mx`: `scale_mult = 2.0**(scale_expanded − 127)`; `stat_scaled = stat × scale_mult`; `einsum("kiq,kjq→ij")` | OBSERVED (`:118-126`) |
| **firmware** | FW-75 dequant datapath: nibble-unpack → ufloat → scale-MAC (`ivp_mulus4tan16xr16` / `ivp_dmulqa2n8xr8`) → clamp → `cvtg48` extract | CARRIED ([mx-dequant](../firmware/kernels/mx-dequant.md) §5) |
| **ISS value model** | `xdref_addexp` / `addexpm` = "fp exponent-add (`ldexp`)" — the soft-float primitive realizing `2^(s−127) × value` as an exponent add | CARRIED (DX-ISS) |

`torch.ldexp(scale−127) == sim 2.0**(scale−127)× == FW-75 scale-MAC == ISS addexp` —
**bit-exact across all four**. The same `−127` bias appears in **five** shipped torch
dequant variants re-read this pass (`get_mxfp4_tensor` `:54`/`:84`,
`get_mxfp4_tensor_from_uint16` `:208`/`:243`, `get_mxfp8_tensor_from_uint32`
`:278`/`:312`, `dequant_byte_4bit_tensor` `:123`/`:133`, and the LUT path), each
ending in `torch.ldexp(sub, exp)`. `FP4_VALUES` (`transform_weights.py:28`) is the
16-entry e2m1 codebook the inverse uses. `[HIGH — host/sim OBSERVED; FW + ISS CARRIED;
the exact MAC-input-vs-PSUM-drain TAP of the PE-side scale is MED per pe-matmul §7.]`

> **NOTE — the FORWARD divide leg has no single firmware-decode this pass.** The
> ledger pins `0xE3 QUANTIZE_MX | DVE` (the `0xe0`/`0xe1`/`0xe3` DVE band:
> `SPARSITY_COMPRESS` / `RAND2` / `QUANTIZE_MX`), but the forward extract
> (max-over-block biased exponent) + divide + FP8-pack is the **mirror** of the FW-75
> dequant legs — a DVE shift/max + divide + saturating-pack — and is **not**
> byte-decoded here. The forward FLIX body is **INFERRED-HIGH** to be the inverse of
> §4's dequant. `[HIGH ledger pin; forward body INFERRED-HIGH.]`

---

## 8. Cross-check verdict

| axis | finding | status |
|---|---|---|
| fwd opcode/engine | `0xE3 QUANTIZE_MX` / DVE (Vector) | **CORRECTED** ✔ (was `0x7b`/POOL) |
| inv opcode/engine | `0x7B TENSOR_DEQUANTIZE` / POOL (GpSimd) | ✔ (the standalone inverse) |
| matmul opcode (v4) | `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` / PE | **SHARPENED** ✔ (was "`0x02` mx") |
| matmul opcode (v5) | `0x01`/`0x02` + `MXTENSOR_V2` / PE | ✔ (the unified path) |
| shared-exp op | max-over-[8,4]-block biased fp32 exp − `max_exp` → E8M0 | ✔ |
| E8M0 scale dtype | `SFP8_E8 = 0x13 = FP8_S0E8M0`, `uint8`, bias 127 | ✔ |
| block geometry | 32 = 8 part × 4 elem ; 16 blocks × 8 = 128 | ✔ (hard consts) |
| SBUF scale scatter | ×32 quadrant stride (0-3 / 32-35 / 64-67 / 96-99) | ✔ (fwd & inv inverses) |
| scale-multiply tie | `ldexp == 2.0**(s−127) == FW-75 MAC == addexp` | ✔ bit-exact |
| `0xE3` gen floor | NC-v4 (MARIANA); `nc==V5` is the maverick-header-local gate | **RESOLVED** (§6.3) |

**Verdict:** one **CORRECTION** (the forward `0xE3`-not-`0x7b` engine/opcode), one
**SHARPENING** (the v4 separate `0x09`/`0x0A` PE pair), one **resolved divergence**
(the `0xE3` gen floor = NC-v4 / MARIANA, §6.3), zero residual opcode-numeric
disagreements. Every engine the nki frontend names — Vector for `quantize_mx`, Tensor
for `nc_matmul_mx` — matches the ledger engine of the corrected opcode; the E8M0
multiply is bit-exact across host/sim/FW/ISS.

---

## 9. Reimplementation checklist

To rebuild a Vision-Q7-compatible OCP-MX path:

1. **Geometry first.** Manufacture the 8-partition × 4-element MX block (the
   `swizzle [M,N] → [M/4,N*4]`); enforce `mx_block_size == 32`, partition dim
   `% 32 == 0` (≤128), free dim `% 4 == 0`.
2. **Forward quantize (`0xE3`, DVE).** Upcast `src` (fp16/bf16) to fp32 *inside* the
   op; extract `(bits>>23)&0xFF`; MAX over each 8×4 block; cast `uint8` **then**
   subtract `max_exp` (`8` for `e4m3fn`, `15` for `e5m2`) → E8M0. Divide by
   `2^(scale−127)`, clamp `±max_val` (`448` / `57344`), cast FP8 (RNE), ×4-pack to
   `uint32`. Scatter the scale ×32 into the SBUF quadrants
   (`0-3, 32-35, 64-67, 96-99`). **Output is FP8 only — never MXFP4.**
3. **MX matmul (PE).** v4: emit `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` with the
   `MXMEM_PATTERN1D` out-of-band `scale_addr`; v5: emit `0x01`/`0x02` with the
   `MEM_PATTERN3D` `MXTENSOR_V2` member (`ADDR4MARKER = 0x01`). Gather the
   ×32-scattered scale to compact, `repeat ×8`, multiply `2^(scale−127)`, MAC into
   FP32 PSUM; dst FP32 or BF16. Inputs may be `e5m2_x4` / `e4m3fn_x4` / `e2m1fn_x4`.
4. **Standalone dequant (`0x7b`, POOL).** Reserve `0x7b` for the **inverse only** —
   the POOL in-band-block-of-8 dequant, **not** the forward quantize.
5. **Do not conflate** the POOL block-of-8 in-band dequant with the OCP block-of-32
   out-of-band E8M0 surface (§6.4).

---

## 10. Honest limitations / desync flags

- **The `0xE3` numeric edge is INFERRED-HIGH.** The `SundaISel` `QuantizeMXCodeGen`
  does not spell the opcode byte; `0xE3` is forced by engine + op-name + gen
  availability (§2.3) and corroborated by the ledger's dedicated `0xE3 QUANTIZE_MX |
  DVE` row, but the host-emitter → `0xE3` numeric step is not byte-read.
- **The forward `0xE3` DVE FLIX body is not byte-decoded** this pass (§7); it is
  INFERRED-HIGH to be the mirror of the FW-75 dequant legs.
- **The `0xE3` gen floor is RESOLVED** (no longer a desync flag): the floor is
  **NC-v4 (MARIANA)**, and the `nc == V5` predicate is the maverick header's gen-local
  gate, not a global floor — see §6.3 and [mx-device-bodies §6.1](mx-device-bodies.md)
  (both per-arch `s3dmx1_quant.h` headers byte-read).
- **The PE-side MX scale TAP** (multiplier-input vs PSUM-drain) is MED/INFERRED — the
  PE array RTL is out of corpus ([pe-matmul](../firmware/kernels/pe-matmul.md) §7).
- **v5/MAVERICK MX interiors are header-OBSERVED only.** The `MXTENSOR_V2` struct +
  `ADDR4MARKER` selector are header-compile-verified; the v5 firmware handler
  interiors are FLIX-desynced → INFERRED.
- **The dequant-side in-band block-of-8 scale's bit-format** is not pinned (whether
  it is itself E8M0) — E8M0 is OBSERVED only for the separate OCP `MXTensorV2`
  `scale_dtype` ([mx-dequant](../firmware/kernels/mx-dequant.md) §6.3).

---

## See also

- [Byte-Decode of the MX Device Bodies](mx-device-bodies.md) — the `QuantizeMX 0xe3`
  (`S3DMX1_QUANT`) forward DVE body and the MX matmul interiors.
- [MX (Microscaling) Dequant Compute Paths](../firmware/kernels/mx-dequant.md) — the
  POOL `0x7b` in-band block-of-8 dequant (`proc_4bit_mx_8`); the inverse direction.
- [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) — `LdweightsMX 0x09` /
  `MatmulMX 0x0A` (v4) and the unified `MXTENSOR_V2` matmul (v5).
- [The unified datatype model](../firmware/kernels/dtype-model.md) — `SFP8_E8 = 0x13
  = FP8_S0E8M0` (E8M0), the scale-only codes, the FP32 convert hub.
- [The Opcode Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the
  `0xE3` / `0x7B` / `0x09` / `0x0A` engine + per-gen presence rows.
