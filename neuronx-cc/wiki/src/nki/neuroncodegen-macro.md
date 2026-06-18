# NeuronCodegen Macro-Kernel Emitters

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The EMIT side lives in `KernelBuilder.cpython-310-x86_64-linux-gnu.so` (class `GeneratedNeuronCodegen`, under `neuronxcc/nki/compiler/backends/neuron/`); the LOWER side lives in `BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (`neuronxcc/starfish/penguin/targets/codegen/`). Cython-compiled, `-O3 -fwrapv -fPIC -g`. Addresses are version-pinned; cp311/cp312 share the `__pyx` method roster but were not byte-diffed.*

## Abstract

The NKI front end does not lower a flash-attention or a fused-MLP kernel by re-emitting its inner GEMM/softmax/normalize math. It **names** them. About twenty-five high-level emitter methods on `GeneratedNeuronCodegen` — `attention_kernel`, `mlp_kernel`, `qkv_kernel`, `rmsnorm_quant_kernel`, `router_topk_kernel`, `expert_mlps_kernel`, `packed_cayman_pe_tp_kernel`, and friends — each produce a **single Penguin macro-op node** carrying a string NAME (`AttentionMMSoftmaxMM`, `MLP`, `QKV`, `RMSNormQuant`, `RouterTopK`, `ExpertMLPs`, `CaymanPackedPETranspose`, …) plus a marshalled bag of tensor operands and a kernel-config dict. The compute is withheld in a precompiled leaf: `attention.so`, `mlp.so`, `qkv.so`, `rmsnorm.so`, `router_topk.so`, `expert_mlps.so`, `hw_ubench.so` — all shipped under `neuronxcc/nki/_private_kernels/`.

The macro-op NAME is the key. On the LOWER side, `BirCodeGenLoop` carries a bespoke `codegen<Name>` twin for the common families (`codegenAttentionMMSoftmaxMM` @ `0xb8c10`, `codegenMLPKernel` @ `0xf6a70`, `codegenNormQKV` @ `0xfb050`, `codegenRMSNormQuantKernel` @ `0x9a9c0`, `codegenBackwardsAttention` @ `0x1e02c0`, plus four `codegenTiledNativeKernel*`), and a generic `codegenBIRKernel` @ `0xa8360` for everything else. Every twin funnels into `codegenInternalNativeNkiKernel` @ `0x8d630` → `_resolve_kernel_config` @ `0xa0ca0` → `_trace_internal_kernel_to_new_nki_frontend` @ `0x5cd10`, which looks the NAME up in `_INTERNAL_KERNEL_REGISTRY` and traces the matching `_private_kernels` leaf. The macro emitter is therefore a packaging layer: it picks the NAME, classifies tiled-vs-untiled, folds residual-add and quantization into the operand list, and hands off (cross-ref Part 6.6.1 *the three-sink kernel-node model*, Part 6.7 *the kernel algorithms these macros invoke*, and [`hlo-opt/hlo-to-native-kernel-lowering`](../hlo-opt/hlo-to-native-kernel-lowering.md)).

Two decisions inside the emitter are easy to get wrong and are reconstructed here in full. First, **tiled vs untiled is not a tile-size threshold** — it is an IO-type test (`_is_all_io_type_memref_tile`): all operands already SBUF `MemrefTile`s → the `Tiled*` name; all whole HBM tensors → the base name; a mix is a hard assert. Second, **fused-add and quant do not change the NAME** — they add a residual operand and scale operands to the same macro and set epilogue flags consumed inside the leaf.

| | |
|---|---|
| **Emit class** | `GeneratedNeuronCodegen` (KernelBuilder.so) — ~190 emit methods, `…_379finalize_kernel` is the last |
| **Macro band** | methods #313–#377 (the highest band, after ~150 per-instruction `nl`/`nisa` emitters) |
| **Tiled/untiled selector** | `_is_all_io_type_memref_tile` (#313) — all-tiles vs all-tensors, mixed = assert |
| **Lower twins** | `codegen<Name>` in BirCodeGenLoop.so (Attention/MLP/NormQKV/RMSNormQuant/Backwards + 4× TiledNative); generic `codegenBIRKernel` @ `0xa8360` for the rest |
| **Registry spine** | `codegenInternalNativeNkiKernel` @ `0x8d630` → `_resolve_kernel_config` @ `0xa0ca0` → `_trace_internal_kernel_to_new_nki_frontend` @ `0x5cd10` → `_trace_kernel_beta2`/`beta3` |
| **Registry** | `_INTERNAL_KERNEL_REGISTRY`, built by `_build_internal_kernel_registry` ("Build the registry of all internal NKI kernels that can be traced to new NKI frontend.") |
| **Leaves** | `_private_kernels/{attention,mlp,qkv,rmsnorm,router_topk,expert_mlps,hw_ubench,prefix_caching_attention,blockwise_mm,conv}.cpython-310…so` |
| **Keystone proof** | BirCodeGenLoop string `"Expected to get an AttentionMMSoftmaxMM or TiledNativeKernelAttention but got "` + the type hint `Union[AttentionMMSoftmaxMM, TiledNativeKernelAttention]` |

---

## 1. The two-sided picture: EMIT names, LOWER resolves

A macro kernel crosses two compiled modules. The boundary between them is a **string** — the macro-op NAME.

```text
  KernelBuilder.so                          BirCodeGenLoop.so
  GeneratedNeuronCodegen.<family>_kernel    codegen<Name>  (bespoke twin, or
        │  classify tiled/untiled                          generic codegenBIRKernel)
        │  marshal tensor operands                  │
        │  set fused-add / quant flags              ▼
        ▼                                    codegenInternalNativeNkiKernel  @0x8d630
  emit ONE macro-op node ───── NAME ─────►  _resolve_kernel_config          @0xa0ca0
       "<MacroName>"                               │  lookup NAME in
       + operand bag                               │  _INTERNAL_KERNEL_REGISTRY
       + kernel-config kwargs                      ▼
                                            _trace_internal_kernel_to_new_nki_frontend @0x5cd10
                                                   │  → _trace_kernel_beta2 (KLIR, default)
                                                   │  → _trace_kernel_beta3 (BIR)
                                                   ▼
                                            compiled _private_kernels.<leaf>.so
                                                   (the actual GEMM/softmax/norm math)
```

The emitter never re-emits the inner math. It does three things: (a) choose the macro NAME (which encodes the family and, via suffixes, the variant); (b) pack operands into the macro node's argument list; (c) attach a kernel-config kwarg bag. The NAME is then the registry key the lower side resolves.

> **NOTE — Why the dispatch is read off wrappers, not bodies.** The KernelBuilder.so DWARF compile string is `GNU C17 … -O3 -fwrapv -fPIC -g`. At `-O3`, most `__pyx_pf_*` emitter bodies are inlined into their `__pyx_pw_*` wrappers; only 56 named `__pyx_pf` survive (mostly `__defaults__` thunks). The macro-band emit methods (#313–#377) therefore appear as their wrappers plus the interned name pool, not as standalone decompilable bodies. The NAME pairings below are grounded on the LOWER side (`codegen<Name>` symbols + assertion strings in BirCodeGenLoop.so, which **are** present and decompiled) and on the readable `_pre_prod_kernels` source, not on KernelBuilder body decompiles.

### Bespoke twins vs the generic path

The LOWER side does *not* carry a `codegen<Name>` for every macro. The split is sharp and is provable by **string absence**: a family with a bespoke twin has its NAME and `codegen<Name>` interned in BirCodeGenLoop.so; a generic-routed family has neither (only the leaf module string survives). The confirmed bespoke set, with VA addresses from `BirCodeGenLoop` function-address table:

| Bespoke `codegen<Name>` | VA | Macro NAME it consumes |
|---|---|---|
| `codegenAttentionMMSoftmaxMM` | `0xb8c10` | `AttentionMMSoftmaxMM` (+ variants §3) |
| `codegenTiledNativeKernelAttention` | `0x5aaf0` | `TiledNativeKernelAttention` |
| `codegenBackwardsAttention` | `0x1e02c0` | `BackwardsAttention` |
| `codegenMLPKernel` | `0xf6a70` | `MLP` / `MLPKernel` |
| `codegenTiledNativeKernelMLP` | `0x19b0d0` | `TiledNativeKernelMLP` |
| `codegenNormQKV` | `0xfb050` | `QKV` (RMSNorm fused into the proj) |
| `codegenTiledNativeKernelQKV` | `0xf8fe0` | `TiledNativeKernelQKV` |
| `codegenRMSNormQuantKernel` | `0x9a9c0` | `RMSNormQuant` / `RMSNormQuantKernel` |
| `codegenTiledNativeKernelRMSNormQuant` | `0x94240` | `TiledNativeKernelRMSNormQuant` |
| `codegenNeuronReduceMacro` | `0x13d2b0` | `NeuronReduceMacro` |
| `codegenBIRKernel` (+`codegenBIRKernelAccess`) | `0xa8360` | `BIRKernel` and every generic-routed macro |
| `codegenInternalNativeNkiKernel` | `0x8d630` | (the shared trace spine) |

Everything else — `RouterTopK`, `ExpertMLPs`, `RowTiledMM`, `ColumnTiledMM`, `CaymanPackedPETranspose`, `AttentionTkgFwd` — has **zero** bespoke-string occurrences in BirCodeGenLoop.so and lowers through generic `codegenBIRKernel` + the registry. The decode-vs-prefill distinction for the TKG path is carried as an `is_tkg` flag (string confirmed in BirCodeGenLoop.so) on the config bag, not as a separate NAME. `[CONFIRMED by name-presence/absence]`

> **QUIRK — `MatMulMX`/`QuantizeMX` are *not* macro twins.** The report's "MX path → codegenMatMulMX" is loose. The real lower symbols are `codegenMatMulMXOp` and `codegenQuantizeMXOp` (@ `0x1c0000`) — per-*instruction* op codegens, not family-macro codegens. MX block-scaled quant is an inline op used *inside* the leaves, not a macro-kernel family. See §5. `[CORRECTION]`

---

## 2. The macro-emitter → macro-op → leaf table

The complete catalog. The **emit method** is on `GeneratedNeuronCodegen` (mdef index in parentheses); the **macro NAME** is the string the node carries; the **lower** column is the `codegen<Name>` twin (or `generic` = `codegenBIRKernel`); the **leaf** is the resolved `_private_kernels` module, all confirmed present as `.so` files in the wheel.

| Emit method (mdef #) | Macro-op NAME | Lower codegen | Leaf `.so` |
|---|---|---|---|
| `attention_kernel` (#349), untiled | `AttentionMMSoftmaxMM` (+ `WithoutSwap` / `Causal*` / `V2*`, §3) | `codegenAttentionMMSoftmaxMM` | `attention` |
| `attention_kernel` (#349), tiled | `TiledNativeKernelAttention` | `codegenTiledNativeKernelAttention` | `attention` |
| `attention_tkg_fwd_kernel` (#343) | `AttentionTkgFwd` | generic (`is_tkg` flag) | `attention` (tkg) |
| `attention_prefix_caching_fwd_kernel` (#371) | (name-keyed prefix-cache macro) | generic + registry | `prefix_caching_attention` |
| `backwards_attention_kernel` (#375) | `BackwardsAttention` | `codegenBackwardsAttention` | `attention` (bwd) |
| `mlp_kernel` (#359), untiled | `MLP` / `MLPKernel` | `codegenMLPKernel` | `mlp` |
| `mlp_kernel`, tiled | `TiledNativeKernelMLP` | `codegenTiledNativeKernelMLP` | `mlp` |
| `mlp_fused_add_kernel` (#357) | `MLP` (+ residual operand, §4) | `codegenMLPKernel` | `mlp` (fused_add) |
| `quant_mlp_kernel` (#363) | `MLP` + `QuantOnly` fold (§5) | `codegenMLPKernel` | `mlp` (quant) |
| `quant_mlp_fused_add_kernel` (#361) | `MLP` + `QuantOnly` + residual | `codegenMLPKernel` | `mlp` (quant+add) |
| `qkv_kernel` (#331), untiled | `QKV` | `codegenNormQKV` | `qkv` |
| `qkv_kernel`, tiled | `TiledNativeKernelQKV` | `codegenTiledNativeKernelQKV` | `qkv` |
| `qkv_fused_add_kernel` (#333) | `QKV` (+ residual operand) | `codegenNormQKV` | `qkv` (fused_add) |
| `rmsnorm_quant_kernel` (#347), untiled | `RMSNormQuant` / `RMSNormQuantKernel` | `codegenRMSNormQuantKernel` | `rmsnorm` |
| `rmsnorm_quant_kernel`, tiled | `TiledNativeKernelRMSNormQuant` | `codegenTiledNativeKernelRMSNormQuant` | `rmsnorm` |
| `router_topk_kernel` (#345) | `RouterTopK` | generic | `router_topk` |
| `expert_mlps_kernel` (#365) | `ExpertMLPs` | generic | `expert_mlps` |
| `row_tiled_mm_kernel` (#321) | `RowTiledMM` | generic | `hw_ubench` |
| `column_tiled_mm_kernel` (#323) | `ColumnTiledMM` | generic | `hw_ubench` |
| `packed_cayman_pe_tp_kernel` (#319) | `CaymanPackedPETranspose` | generic + `cayman_matmul_double_row_ap` | `hw_ubench` |
| (norm/reduce macro) | `NeuronReduceMacro` | `codegenNeuronReduceMacro` | — |
| (generic / fallback) | `BIRKernel` | `codegenBIRKernel` | registry leaf |

> **NOTE — mdef indices.** The emit-side indices (#319–#377) are the Cython method ordinals reported from the KernelBuilder `__pyx_mdef` roster (the macro band runs from `attention_kernel` family up to `…_379finalize_kernel`). The lower-side ordinals are read directly from `__pyx_pw_…BirCodeGenLoop_<NNN>codegen<Name>` symbol names — e.g. `codegenAttentionMMSoftmaxMM` = method #219, `codegenBIRKernel` = #221, `codegenBackwardsAttention` = #223, `codegenNormQKV` = #227, `_resolve_kernel_config` = #235, `_trace_internal_kernel_to_new_nki_frontend` = #241, `codegenInternalNativeNkiKernel` = #243, `codegenMLPKernel` = #245, `codegenRMSNormQuantKernel` = #247, `codegenTiledNativeKernelAttention` = #253, `cayman_matmul_double_row_ap` = #287. The emit-side ordinals are `STRONG` (roster-derived); the lower-side ordinals are `CONFIRMED` (in the symbol).

---

## 3. Tiled vs untiled: the IO-type test (the real heuristic)

> **CORRECTION — It is not a tile-size threshold.** A natural guess is that `*_tiled` vs `*_untiled` is chosen by a tile-size or tile-shape threshold, or by the K15 `SBSizeLegalization` decision. Both are wrong. The selector is a **type classification of the kernel's IOs**: are the operands already SBUF-resident `MemrefTile`s (NDTiles), or are they whole HBM tensors that the kernel must itself tile? `[CONFIRMED]`

The decision lives in helper `_is_all_io_type_memref_tile` (mdef #313). Its rodata docstring is verbatim:

> *"The function returns true iff all kernel IOs are MemrefTile. The function asserts if only some of IOs are MemrefTile, while the rest are not."*

Each public emitter calls it once, caches the boolean as the `is_tiled` instance attribute, and branches:

```c
// GeneratedNeuronCodegen.<family>_kernel  (e.g. _mlp_kernel @0x1ef3e0 wrapper)
// Reconstructed from the GetAttr/FastCall cascade in the __pyx_pw body
// + the rodata name roster (is_tiled, _tiled_/_untiled_<family>_kernel_impl).
PyObject *family_kernel(self, ios, **cfg) {
    // _is_all_io_type_memref_tile asserts on a tile/tensor MIX (hard reject).
    bool is_tiled = self->_is_all_io_type_memref_tile(ios);   // #313
    self->is_tiled = is_tiled;                                 // cached attr

    if (is_tiled)
        // all IOs are MemrefTile / NDTile (already SBUF sub-tensors)
        return self->_tiled_<family>_kernel_impl(ios, cfg);
        //   → emits macro-op  "TiledNativeKernel<Family>"
    else
        // all IOs are whole KernelHBMTensors; kernel owns HBM→SBUF tiling
        return self->_untiled_<family>_kernel_impl(ios, cfg);
        //   → emits macro-op  "<Family>"  (AttentionMMSoftmaxMM / MLP / QKV / RMSNormQuant)
}
```

The three outcomes:

- **all `MemrefTile`** → `is_tiled = true` → `_tiled_<family>_kernel_impl` → NAME `TiledNativeKernel<Family>`.
- **all whole HBM tensors** → `_untiled_<family>_kernel_impl` → NAME `<Family>` (the base name).
- **mixed** → hard assert. The user-facing sibling message is verbatim *"Inputs/outputs to quant_mlp_fused_add_kernel must be all tensors or all tiles, not a mix of both"* (and the generic *" must be all tensors or all tiles, not a mix of both"*). `[CONFIRMED]`

The two NAMEs are not just decorative — the LOWER side asserts on exactly this pair. The keystone, verbatim from BirCodeGenLoop.so:

> *"Expected to get an AttentionMMSoftmaxMM or TiledNativeKernelAttention but got "*

with the companion type annotation `Union[AttentionMMSoftmaxMM, TiledNativeKernelAttention]`. That `Union` is the untiled/tiled pair, named explicitly, proving the split is a binary NAME choice on the IO type. `[CONFIRMED]`

A second corroboration ties a *feature* to the untiled path: *"Softmax caching not supported yet for tiled attention_kernel. Pass in tensors instead of tiles."* — `cache_softmax` is legal only when operands are tensors (the untiled path), confirming `is_tiled` is purely "are the operands tiles." `[CONFIRMED]`

> **NOTE — Relation to K15 `SBSizeLegalization`.** K15 sizes and legalizes SBUF tiles upstream. The tiled emitter *presupposes* legalized `MemrefTile`s — the caller (e.g. a `TiledNative` codegen loop in BirCodeGenLoop) already produced them. So K15 is upstream of the tiled branch; it is not the branch *condition*. The tiled emitter consumes K15's output but does not call `SBSizeLegalization` itself. `[INFERRED]`

> **GOTCHA — Disassembly limit.** In the `_mlp_kernel` wrapper @ `0x1ef3e0`, after the ~14 `__Pyx_GetKwValue_FASTCALL` kwarg-parse prologue, the body shows a `GetAttrStr` + `FastCallDict`-on-`self` cascade — the visible shape of `if self._is_all_io_type_memref_tile(ios): self._tiled_…(…) else: self._untiled_…(…)`. The interned-name operands were *not* slot-resolved to literal method strings from the mstate table, so the branch is `STRONG` (structurally certain, the two `codegen*` twins + rodata names make it unambiguous), not byte-exact per call.

---

## 4. Fused-add residual variants

Three emitters add a residual: `mlp_fused_add_kernel` (#357), `qkv_fused_add_kernel` (#333), `quant_mlp_fused_add_kernel` (#361). The mechanism is uniform and minimal.

The fused-add variant **does not change the macro NAME**. `mlp_fused_add` still emits `MLP` → `codegenMLPKernel`; `qkv_fused_add` still emits `QKV` → `codegenNormQKV`. It does two things: (a) appends **one extra residual tensor operand** to the macro's operand list, and (b) sets a fused-add flag in the config bag so the leaf does `out = kernel(x) + residual` in its epilogue — no separate `add` op appears in the graph.

```c
// _mlp_fused_add_kernel (#357) — same macro NAME as plain mlp_kernel
PyObject *mlp_fused_add_kernel(self, x, weights, mlp_out, attn_out, **cfg) {
    // pairing contract: residual operands are (mlp_out, attn_out) — both or neither
    require(mlp_out_present == attn_out_present,
            "Fused add requires both mlp_out and attn_out to be provided or "
            "neither of them");
    operands = pack(x, weights);
    operands.append(attn_out);          // <-- the ONE extra residual operand
    cfg["fused_add"] = true;            // epilogue-add flag consumed inside mlp.so
    return emit_macro("MLP", operands, cfg);   // NAME unchanged
}
```

The residual operands are `(mlp_out, attn_out)`, and the **pairing contract** is verbatim: *"Fused add requires both mlp_out and attn_out to be provided or neither of them"* — an exclusive-or is rejected. Semantically: the MLP block's residual *is* the attention block's output (`attn_out`), so the transformer's `x + MLP(norm(x))` residual stream becomes a single macro op rather than a kernel followed by a graph-level add. `[CONFIRMED]`

`quant_mlp_fused_add_kernel` (#361) combines the residual-add *and* the quant fold (§5) in one macro; its all-tensors-or-all-tiles guard (§3) is the verbatim `quant_mlp_fused_add_kernel` message quoted above.

---

## 5. Quant variants: scale folding, and where MX is not

Two emitters fold quantization into an existing macro rather than introducing a new family:

**`quant_mlp_kernel` (#363) / `quant_mlp_fused_add_kernel` (#361)** emit the same `MLP` macro (`codegenMLPKernel` → `mlp.so`), but with quantized weights. The operand set gains per-projection scale tensors `gate_up_w_scale` and `down_w_scale`, and (for fp8) an `is_fp8_kernel` gate. The quant mode is carried as `QuantOnly` (an interned NAME in the macro's config), and the matmul folds the scale rather than running a separate dequant. The leaf is `quant_mlp_isa_kernel` / `quant_mlp_fused_add_isa_kernel`. `[CONFIRMED operand names + leaf]`

**`rmsnorm_quant_kernel` (#347)** is RMSNorm with a fused output quantize — `norm-then-quantize` as a single macro, no separate quantize op. Operands: `hidden_in`, `eps` (float-typed — guard *"Expecting eps input to have type float"*), `weight`, `dst_scale` / `scales` (the output quant scale), and `dst_dtype` (gated *"dst_dtype must be float32 or bfloat16."*). NAME `RMSNormQuant`(`Kernel`) → `codegenRMSNormQuantKernel` @ `0x9a9c0` → `rmsnorm.so`. `[CONFIRMED]`

> **QUIRK — MX block-scaled quant is a different layer.** Don't conflate the `quant_mlp`/`rmsnorm_quant` scale *folds* with MX. MX (block-scaled, `block_size 32`, x4-packed) is a **per-instruction** path: `GeneratedNeuronCodegen.quantize_mx` (#161) emits a `QuantizeMXOp`; `matmult_mx` (#107) emits a `MatMulMXOp` with `stationary_scale`/`moving_scale`. Their lower twins are `codegenQuantizeMXOp` (@ `0x1c0000`) and `codegenMatMulMXOp` — instruction codegens, not family-macro codegens. MX ops live *inside* the compiled leaves; they are not a macro-kernel emitter. `[CONFIRMED]`

---

## 6. Attention family variants and the special cases

### Attention NAME variant set

`attention_kernel` (#349) is the public dispatcher. Its operand kwargs span `q, k, v, mask / active_mask / prefix_mask, out / attn_out, scale / softmax_scale, is_causal, cache_softmax, sink, q_head, k_active, v_active, k_prior, rope_pos_ids, batch_size, num_stages, num_tiles, fuse_batches`. It classifies IO type (§3) and dispatches to `_tiled_attention_kernel_impl` (#315, with nested `fuse_batches`) or `_untiled_attention_kernel_impl` (#317), which emit the macro node.

The untiled attention macro has **five** NAME forms — the chosen form *is* the variant the lower `codegenAttentionMMSoftmaxMM` dispatches on:

| Macro NAME | Meaning |
|---|---|
| `AttentionMMSoftmaxMM` | base: MM1·softmax·MM2, with operand swap |
| `AttentionMMSoftmaxMMWithoutSwap` | no LHS/RHS swap on the 2nd matmul |
| `CausalAttentionMMSoftmaxMMWithoutSwap` | causal-masked, no swap |
| `V2AttentionMMSoftmaxMMWithoutSwap` | gen2 ("V2") PE-array variant |
| `V2CausalAttentionMMSoftmaxMMWithoutSwap` | gen2 causal, no swap |

Three of these (`AttentionMMSoftmaxMM`, `…WithoutSwap`, `CausalAttention…WithoutSwap`) appear directly in the readable `_pre_prod_kernels/attn_fwd.py` source; the two `V2*` forms are present in the compiled leaf / KernelBuilder rodata pool. The `WithoutSwap` suffix records whether MM2 keeps stationary/moving as-given vs swaps to legalize the PE-array contraction axis (cross-ref Part 6.7, mm1→softmax→mm2). The selector — which depends on `is_causal`, MM2 operand-layout swap legality, and arch/"V2" — was not disassembled to the comparand level; the NAMEs are `CONFIRMED`, the selector logic is `STRONG/INFERRED`.

> **GOTCHA — the causal-no-swap form hardcodes scale 1.0.** Verbatim contract: *"CausalAttentionMMSoftmaxMMWithoutSwap only supports scale equal to 1.0"*. The causal-no-swap kernel pre-folds the softmax scale into Q upstream, so it rejects any `scale != 1.0`. `[CONFIRMED]`

### Decode (TKG) and prefix-caching

`attention_tkg_fwd_kernel` (#343) is the decode / token-generation emitter (`_tiled_`#337 / `_untiled_`#335 impls) → macro `AttentionTkgFwd` → `attention.so::attention_tkg_fwd_isa_kernel`. Its operand set adds the KV-cache split (`cache_softmax`, `k_prior`/`k_active`/`v_active` = prior vs current KV). It has **no** bespoke `codegen<Name>`; it lowers via generic `codegenBIRKernel` with the `is_tkg` flag on the config bag. `[CONFIRMED name + leaf; flow STRONG]`

`attention_prefix_caching_fwd_kernel` (#371) is the vLLM-style prefix-cache flash-attention emitter (`_tiled_`#367 / `_untiled_`#369) → the **unique** `prefix_caching_attention.so` leaf (a distinct `.so`, confirmed present in the wheel). It is **name-keyed**: the verbatim error *"Prefix caching is not implemented for this kernel name "* is a dict lookup on `kernel_name` → the prefix-caching leaf; an unknown name raises. `prefix_mask` is the extra operand. `[CONFIRMED]`

### Backwards (training)

`backwards_attention_kernel` (#375) → macro `BackwardsAttention` → `codegenBackwardsAttention` @ `0x1e02c0` → the `attention.so` backward leaf. Two verbatim contracts gate it: *"Backwards Attention BIR kernel only supports softmax scale = 1.0"* and *"Backwards Attention BIR kernel does not currently support non zero dropout prob"* — `scale != 1` and `dropout > 0` are both forbidden. `[CONFIRMED]`

### The MM / hw_ubench probes and Cayman

Three emitters produce hardware-microbench tiled-GEMM probes that all resolve to `hw_ubench.so` and carry `tile_position`/`tile_size`/`tile_shape` + `stationary`/`moving`/`lhs`/`rhs` + `transpose`/`psum` operands; all lower via generic `codegenBIRKernel`:

- `row_tiled_mm_kernel` (#321) → `RowTiledMM` (type guard *"Must be RowTiledMM but got "*) → `row_tiled_matmul_isa_kernel`.
- `column_tiled_mm_kernel` (#323) → `ColumnTiledMM` (guard *"Must be ColumnTiledMM but got "*) → `column_tiled_matmul_isa_kernel`.
- `packed_cayman_pe_tp_kernel` (#319) → `CaymanPackedPETranspose` (guard *"Must be CaymanPackedPETranspose but got "*) → `packed_cayman_pe_tp_isa_kernel`.

`packed_cayman_pe_tp_kernel` is the gen3 "Cayman" packed PE-array tensor-parallel matmul probe: the PE array runs a **double-row** packed GEMM (two output rows per pass) with a fused transpose. The lower side builds the double-row access pattern in `cayman_matmul_double_row_ap` @ `0xecf30`. Its contracts (verbatim): *"first F dim of LHS and RHS of the double_row matmult must be 2"* (pack factor = 2 = two rows), *"perf_mode=`double_row_gen3` is not supported on "* (gen3-gated; `double_row_gen3` is a confirmed string), and the partition-stride rule *" must have indexing i \* 32 + j at partition dimension for src tensor with more than 32 partitions but got "* (the 32-lane packed-transpose stride). Operands: `stationary`/`moving`, `tile_position`/`tile_size`, `transpose`, `psum`, `num_channels`, `perf_mode`, `double_row_indices`, `lhs_free_and_double_row_shape`. Additional Cayman contracts: *"Unexpected double row index size"* and the tile-combine helper `combine_trn2_double_row_matmult_tiles` (the 2-row PSUM pack/merge). `[CONFIRMED]`

> **NOTE — "Cayman" is a uarch codename.** `targets.cayman.Cayman` is imported by BirCodeGenLoop. The packed PE-TP family is unique to the private `hw_ubench` microbench — it is not a model kernel; it is a hardware probe for the gen3 double-row matmul.

---

## 7. The registry spine and the leaves

Every NAME — bespoke or generic — converges on one trace spine inside BirCodeGenLoop:

```text
codegen<Name>  ──┐
codegenBIRKernel ─┴─►  codegenInternalNativeNkiKernel  @0x8d630
                            │
                            ▼
                       _resolve_kernel_config           @0xa0ca0
                            │   lookup NAME in _INTERNAL_KERNEL_REGISTRY
                            ▼
                       _trace_internal_kernel_to_new_nki_frontend  @0x5cd10
                            │   ├─► _trace_kernel_beta2   (KLIR — default)
                            │   └─► _trace_kernel_beta3   (BIR)
                            ▼
                       _private_kernels.<leaf>.so   (the compiled compute)
```

`_INTERNAL_KERNEL_REGISTRY` is built once by `_build_internal_kernel_registry`, whose docstring is verbatim *"Build the registry of all internal NKI kernels that can be traced to new NKI frontend."* The registered leaf modules visible as strings in BirCodeGenLoop.so include `neuronxcc.nki._private_kernels.{blockwise_mm, conv, mlp}` (the registry references more; these are the ones interned in the decompiled band). The full leaf set is confirmed on disk under `neuronxcc/nki/_private_kernels/`: `attention`, `attention_cte`, `blockwise_mm`, `collective_matmul`, `conv`, `cumsum`, `expert_mlps`, `fused_linear`, `hw_ubench`, `_internal`, `llama3_transformer`, `mlp`, `prefix_caching_attention`, `qkv`, `rmsnorm`, `RoPE`, `router_topk`, `shard_common`, `transpose` (all `cpython-310-x86_64-linux-gnu.so`). `[CONFIRMED]`

The per-`codegen<Name>` bodies differ only in **how** they marshal a macro's operands into the kernel's argument bag; a shared nested helper (`codegenBIRKernelAccess` / `addBIRKernelTileAccess` / `addBIRKernelNDimSubTensorAccess`) builds the tile/sub-tensor access patterns. The marshalling bodies themselves are not traced here — that is the lower-side codegen page's scope; this page grounds the EMIT→NAME→codegen→registry→leaf spine.

---

## 8. Reimplementation checklist

A reimplementer of the macro-kernel emit layer needs, per family:

1. **A NAME constant** — the macro-op string is the registry key. Untiled families use the base name; the tiled path uses `TiledNativeKernel<Family>`. Attention additionally selects among five swap/causal/V2 variants.
2. **An IO-type classifier** — `_is_all_io_type_memref_tile`: returns true iff *all* IOs are `MemrefTile`, asserts on a mix. This single boolean drives the tiled/untiled NAME and is cached as `is_tiled`.
3. **An operand marshaller** — pack the family's tensors; for fused-add, append the residual operand (`attn_out`) and require the `(mlp_out, attn_out)` both-or-neither pairing; for quant, append `*_scale` operands and set the `QuantOnly` mode.
4. **A config bag** — flags like `fused_add`, `is_tkg`, `is_fp8_kernel`, `perf_mode`, plus the family's scalar kwargs (`scale`, `eps`, `is_causal`, …). Enforce the gates: causal-no-swap and backwards both require `scale == 1.0`; `dst_dtype ∈ {float32, bfloat16}`; backwards forbids non-zero dropout; tiled attention forbids `cache_softmax`.
5. **No inner math** — emit one macro node and stop. The compute is the registry-resolved `_private_kernels` leaf.

> **What is not pinned here.** The KernelBuilder-side emit *bodies* are inlined at `-O3` and were not decompiled; the macro NAMEs and contracts come from the rodata string pool and the readable `_pre_prod_kernels` source, and the LOWER-side `codegen<Name>` symbols + assertions. The variant-selector comparands (which of the 5 attention NAMEs) and the per-`codegen<Name>` operand-marshalling bodies are deliberately out of scope — handoff to the BirCodeGenLoop macro-codegen page.
