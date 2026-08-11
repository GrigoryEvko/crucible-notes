# Production Kernel Inventory: the Three-Tree Story

> *All file paths, symbols, and sizes on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel, canonical; cp311/cp312 are byte-parity for the `.so` roster). Other versions will differ. Every count and path below is read from the extracted wheel filesystem and the IDA strings corpus.*

## Abstract

The NKI kernel library does not ship as one tree. It ships as **three physical copies** that differ by *readability* and *role*, not by purpose. A reimplementer who greps for "the attention kernel" finds three files named `attention*`, in three directories, in three states of disclosure — and only one of them is the code that actually executes a forward pass in production. This page is the physical inventory: which kernels exist on disk, in which tree, in which state, and which import edges wire the readable orchestrators to the compiled compute leaves.

The three trees, in increasing order of source-protection:

- **`nkilib/`** — 172 readable `.py` files, Apache-2.0, the **canonical reference**. This is the documentation-grade "what the kernel computes" source. It has no compiled twin. The detailed math of these kernels is the subject of Part-6 sub-series 6.7.x.
- **`neuronxcc/nki/_pre_prod_kernels/`** — 103 readable `.py` files carrying an "Authors do not provide any warranty" banner. These are the **live orchestration layer**: real, imported-at-runtime modules that fuse norm → route → expert → projection and that, at the leaf, `import` the compiled `.so` from the third tree. They also host pre-production / experimental / training variants with no twin elsewhere (backward blockwise-MM, grouped-matmul, TRN1 attention).
- **`neuronxcc/nki/_private_kernels/`** — **34 compiled Cython `.so` files, zero `.py`**, source withheld. These are the **production compute leaves** — the bytes that run. Each began life as `<name>.py → <name>.c → <name>.so`; the `.c` and `.py` filenames survive as strings inside the binary, but only the `.so` ships.

The production path is an import edge: a `_pre_prod_kernels` orchestrator (readable) imports a `_private_kernels` leaf (compiled), and that compiled leaf is what executes. A second, deeper path bypasses the orchestrator entirely: when the Penguin middle-end lowers a registered op macro, `BirCodeGenLoop` looks the kernel up in a compiled-only `_INTERNAL_KERNEL_REGISTRY` and traces the `.so` directly. Both paths terminate in the same 34-file compiled tree; `nkilib` is never on either critical path for the registered families. This page enumerates all three trees, the exact 34-`.so` roster, the import edges, and — honestly — the leaves whose algorithm bodies are **not recoverable** from this corpus.

For reimplementation, the contract is:

- The three-tree topology and the source-protection gradient (`nkilib` open → `_pre_prod` readable-glue → `_private` compiled).
- The exact 34-`.so` roster, its 19 + 12 + 3 partition, and which entries are full implementations vs thin re-export shims vs package `__init__` stubs.
- The production import edges: which `_pre_prod` orchestrator imports which compiled leaf, named by module and symbol.
- The recoverability ledger: which leaves have a readable twin and which are compiled-only gaps (`mamba_prefix_scan`, `prefix_caching_attention` ISA leaf, `collective_matmul`, `fused_linear`, `hw_ubench`, the `conv2d_*` production family).

| | |
|---|---|
| **Tree A (reference)** | `nkilib/` — 172 `.py`, Apache-2.0, readable, no compiled twin |
| **Tree B (orchestration)** | `neuronxcc/nki/_pre_prod_kernels/` — 103 `.py`, readable, "no warranty", imports tree C |
| **Tree C (production)** | `neuronxcc/nki/_private_kernels/` — **34 `.so`, 0 `.py`**, compiled, source withheld |
| **`.so` partition** | 19 top-level + 12 `topk/` + 3 `legacy/` = 34 |
| **Wheel parity** | cp310 = cp311 = cp312 = 34 `.so` each |
| **Registry** | `_INTERNAL_KERNEL_REGISTRY` — global inside `BirCodeGenLoop.cpython-310-…so`, **not** in any `nki/*.py` |
| **Frontend selector** | `NKI_FRONTEND ∈ {beta2 (KLIR, default), beta3 (BIR)}` |
| **How it was read** | `fd -uu`, `rg -uu`, `strings`, `nm -D` over the extracted wheel |

---

## The Source-Protection Gradient

### Purpose

Before enumerating files, fix the mental model: the three trees are not three libraries. They are one library photographed at three disclosure levels. The rule that predicts which tree a kernel lives in is its production-criticality: **the more a kernel is HW-tuned and on the hot path, the more compiled and hidden it is.**

```text
nkilib/ (172 .py, Apache-2.0)          ── readable REFERENCE — "what it computes"
   │   no compiled twin; documentation-grade math
   ▼  (same kernels, refactored / older-API split)
_pre_prod_kernels/ (103 .py, "no warranty") ── readable LIVE GLUE — orchestration
   │   real imported modules; fuse norm→route→expert→proj
   │   import edge ↓
   ▼
_private_kernels/ (34 .so, source withheld) ── compiled PRODUCTION COMPUTE — "what runs"
       full Cython impls (MB-scale) + 2 thin re-export shims + 2 __init__ stubs
```

### What Lives Where, and Why

The same kernel family appears in two or three trees with different roles. `moe_tkg`, for example, has a readable reference body under `nkilib/core/moe/moe_tkg/`, a readable orchestrator under `_pre_prod_kernels/mlp_tkg/`, and a compiled compute leaf in `_private_kernels/expert_mlps.cpython-310-…so`. The three are not redundant copies of identical bytes: `nkilib` is the documentation-canonical (often refactored) source, `_pre_prod` is the newer-API "klir_*"-flavored live glue, and `_private` is the HW-tuned compiled production leaf.

> **QUIRK —** the three `attention*` files mislead a naïve grep. `nkilib/.../attention.py` is reference math; `_pre_prod_kernels/attn_fwd.py` (91 KB) is a readable legacy CTE orchestrator; `_private_kernels/attention.cpython-310-…so` (4.9 MB) is the master compiled attention leaf hosting `attention_isa_kernel`, `attention_tkg_fwd_isa_kernel`, `backward_attention_isa_kernel`, and more. Only the `.so` is on the production path for the registered attention family. A reimplementer who reads only the readable twin reimplements the *reference*, not the *shipping* kernel.

### The `_pre_prod` Enum Fork

The orchestration tree carries its own reduced copy of the shared enums. `_pre_prod_kernels/__init__.py` re-exports six enums from `_pre_prod_kernels.common_types`: `NormType`, `QKVOutputLayout`, `ActFnType`, `RouterActFnType`, `ExpertAffinityScaleMode`, `QuantizationType`. This `QuantizationType` is a **reduced fork** — `NONE / STATIC / ROW` only, with no MX members — distinct from the **six-member** `nkilib` enum (the two `common_types` modules differ by md5). The full `nkilib/core/utils/common_types.py` (96 lines, md5 `0c2cff02…`) defines `NONE=0, STATIC=1, ROW=2, MX=3, STATIC_MX=4, ROW_MX=5` plus `QKVWeightLayout`; the truncated `_pre_prod_kernels`/`private_nkl` twin (44 lines, md5 `9721a6bb…`) stops at `ROW=2`. A reimplementer copying enum integer values across trees will silently mis-map MX quantization modes.

> **NOTE —** "no warranty" is a literal banner string in the `_pre_prod` files, not editorial colour. It marks these as live-but-experimental glue: imported at runtime, but not the documented stable surface. Treat them as the *current* orchestration, subject to change between releases.

---

## Tree C — the 34 Compiled `.so` (the production roster)

### Purpose

This is the inventory that matters most, because these are the bytes that run. Every entry is a Cython `.py → .c → .so` compile; the `.c`/`.py` source filenames survive as strings (e.g. `blockwise_mm.c`, `blockwise_mm.py` inside `blockwise_mm.cpython-310-…so`), but no source ships. Entry-point symbols below were mined from `PyInit_*` and `__pyx_mdef_*` symbols and from the `_strings.json` IDA exports.

### The 19 + 12 + 3 Partition

Direct enumeration over `_private_kernels/` gives exactly **34 files = 19 top-level + 12 in `topk/` + 3 in `legacy/`**, identical across cp310/cp311/cp312.

> **GOTCHA — 34 is the file count, not the kernel count.** Two of the 34 are package `__init__.cpython-310-…so` stubs (one in `topk/`, one in `legacy/`) that carry no kernel at all — they are Cython-compiled `__init__.py`. The count of *kernel-bearing* modules is therefore **32**. Say which number you mean.

```text
_private_kernels/                      19 top-level .so
├── attention.so          4.9 MB       master attention (full impl)
├── attention_cte.so      3.2 MB       llama3 CTE attention block (full impl)
├── blockwise_mm.so        80 KB       ⚠ THIN SHIM → re-exports _pre_prod_kernels.blockwise_mm
├── collective_matmul.so  1.9 MB       in-kernel TP collective GEMM (UNIQUE, full impl)
├── conv.so               6.0 MB       conv2d_* production family (UNIQUE, full impl)
├── cumsum.so             427 KB       prefix-sum scan (full impl)
├── expert_mlps.so        1.0 MB       production decode-MoE compute (full impl)
├── fused_linear.so       1.2 MB       fused RMSNorm@wQKV (UNIQUE, full impl)
├── hw_ubench.so          407 KB       PE-array TP microbenchmarks (UNIQUE, full impl)
├── _internal.so          474 KB       resize_nearest_* (full impl)
├── llama3_transformer.so  80 KB       ⚠ THIN SHIM → re-exports _pre_prod_kernels.llama3_transformer
├── mlp.so                2.5 MB       dense MLP family (full impl)
├── prefix_caching_attention.so 858 KB vLLM-style prefix-cache flash-attn (UNIQUE, full impl)
├── qkv.so                1.4 MB       rmsnorm+QKV projection (full impl)
├── rmsnorm.so            411 KB       rmsnorm_quant (full impl)
├── RoPE.so               676 KB       RoPE, RoPE_sbuf (full impl)
├── router_topk.so        2.8 MB       MoE router + top-k (full impl)
├── shard_common.so       220 KB       get_seqlen_tile_size (UNIQUE infra, full impl)
└── transpose.so          2.2 MB       layout transposes (full impl)
├── topk/                              12 .so
│   ├── __init__.so                    package stub (NO kernel)
│   ├── topk.so  topk_core.so  topk_config.so  topk_helpers.so  kernel_helpers.so
│   ├── naive_scanning_topk.so  cascaded_2_stage_topk.so  cascaded_2_stage_topk_helpers.so
│   ├── rotational_topk.so  rotational_topk_helpers.so
│   └── topk_method_mapping.so         topk auto-dispatch registry (UNIQUE)
└── legacy/                            3 .so
    ├── __init__.so                    package stub (NO kernel)
    ├── allocated_fused_linear.so      _allocated_fused_rms_norm_qkv (kernels/__init__ legacy export)
    └── tutorial.so                    add_kernel_nx8x128x512
```

### Full Implementation vs Thin Shim

Two of the 19 top-level entries are **not** full compiled kernels — they are Cython re-export shims that pull symbols back from the *readable* `_pre_prod` source of the same name. This is verifiable by size (80 KB vs MB-scale) and by the presence of the `__pyx_import_star` symbol.

```text
$ strings blockwise_mm.cpython-310-x86_64-linux-gnu.so | rg 'import_star|_pre_prod'
neuronxcc.nki._pre_prod_kernels.blockwise_mm
__pyx_import_star
__pyx_import_star_set

$ strings llama3_transformer.cpython-310-…so | rg 'import_star|_pre_prod'
neuronxcc.nki._pre_prod_kernels.llama3_transformer
__pyx_import_star
__pyx_import_star_set

$ strings expert_mlps.cpython-310-…so | rg -c 'isa_kernel'      # full impl, no shim
42
$ strings expert_mlps.cpython-310-…so | rg 'import_star'        # (none)
```

> **NOTE —** the consequence for recoverability is sharp. For `blockwise_mm` and `llama3_transformer`, the "production" copy *is* the readable `_pre_prod` `.py`, merely Cython-compiled and re-exported — so those algorithm bodies **are** recoverable from the wheel. For `attention`, `qkv`, `mlp`, `expert_mlps`, `router_topk`, `conv`, `collective_matmul`, `fused_linear`, `prefix_caching_attention`, and `hw_ubench`, the `.so` is a full MB-scale compiled implementation with **no readable twin** — source withheld.

### Entry Points of the Full-Impl Leaves

The following are the mined top-level kernel symbols. This is a *roster*, not a body — the algorithms behind these symbols are documented (where a readable twin exists) in 6.7.x / 6.8.x, and flagged as gaps below where none exists.

| Module (`.so`) | Size | Top-level kernel symbols (mined) | Readable twin? | Conf |
|---|---|---|---|---|
| `attention` | 4.9 MB | `attention_isa_kernel`, `attention_isa_kernel_context_parallel`, `attention_tkg_fwd_isa_kernel`, `attention_isa_kernel_cache`, `backward_attention_isa_kernel`, `fused_self_attn{,_fwd_cache_softmax,_bwd}`, **`mamba_prefix_scan_kernel`**, `get_global_ring_order`, `_sharded_nisa_attention_impl{,_context_parallel}` | nkilib + `_pre_prod` (partial) | CERTAIN |
| `attention_cte` | 3.2 MB | `llama3_nki_attention_block_cte_kernel` (+ nested rmsnorm/qkv/mm1/softmax/mm2/out_proj/store_and_exchange_v) | `_pre_prod/attn_fwd.py` | CERTAIN |
| `qkv` | 1.4 MB | `rmsnorm_qkv_isa_kernel`, `rmsnorm_qkv_isa_fused_add_kernel`, `qkv_projection_isa_kernel`, `_shard_nisa_qkv_impl` | nkilib `qkv/` | CERTAIN |
| `mlp` | 2.5 MB | `mlp_isa_kernel`, `mlp_fused_add_isa_kernel`, `quant_mlp_isa_kernel`, `fused_mlp_isa_kernel`, `shared_expert_isa_kernel`, `_shard_nisa_mlp_impl`; exports const `TKG_BS_SEQLEN_THRESHOLD` | nkilib `mlp/` | CERTAIN |
| `expert_mlps` | 1.0 MB | `expert_mlps_isa_inline_kernel`, `all_expert_mlps_isa_inline_kernel`, `expert_mlps_isa_kernel`, `all_expert_mlps_isa_kernel`, `_sharded_nisa_expert_mlp_impl` | nkilib `moe` | CERTAIN |
| `router_topk` | 2.8 MB | `router_topk_isa_kernel`, `router_topk_kernel_nki`, `compute_activation`, `_sharded_nisa_router_topk_impl` | nkilib `router_topk` | CERTAIN |
| `rmsnorm` | 411 KB | `rmsnorm_quant_isa_kernel`, `_shard_nisa_rmsnorm_quant_impl` | nkilib | CERTAIN |
| `RoPE` | 676 KB | `RoPE`, `RoPE_sbuf` | nkilib | CERTAIN |
| `cumsum` | 427 KB | `cumsum` | nkilib | CERTAIN |
| `transpose` | 2.2 MB | `transpose_to_last_dim{,_kernel}`, `tiled_dve_transpose_{10,210}`, `tiled_pf_transpose`, `_perform_pf_transpose` | nkilib utils (partial) | CERTAIN |
| `conv` | 6.0 MB | `conv2d`, `conv1d_depthwise_*`, `conv2d_depthwise_f01b_o01i_bf01`, `conv2d_pbp_*_experimental_1`, `conv2d_column_packing{,_io10,_1}` | **none** (nkilib has only exp conv1d ref) | CERTAIN |
| `_internal` | 474 KB | `resize_nearest_kernel`, `resize_nearest_fixed_dma_kernel` (registered, §registry) | **none** | CERTAIN |
| `collective_matmul` | 1.9 MB | `collective_matmul`, `run_matmul{,_sb_to_sb,_hbm_to_hbm}`, `launch_collective_permutes`, `generate_replica_groups` | **none** | CERTAIN |
| `fused_linear` | 1.2 MB | `fused_rms_norm_qkv`, `allocated_fused_rms_norm_qkv` | **none** | CERTAIN |
| `hw_ubench` | 407 KB | `packed_cayman_pe_tp_isa_kernel`, `row_tiled_matmul_isa_kernel`, `column_tiled_matmul_isa_kernel` | **none** | CERTAIN |
| `shard_common` | 220 KB | `get_seqlen_tile_size` | **none** (infra) | CERTAIN |

> **GOTCHA —** `hw_ubench.so` is *not* a model kernel. Its symbols (`packed_cayman_pe_tp_isa_kernel`, the row/column-tiled matmul probes) are HW microbenchmarks that exercise the PE-array tensor-processing path. "Cayman" is a uarch/HW codename, not a layer name. A reimplementer who treats these as production compute will reimplement a benchmark harness.

---

## Tree B — `_pre_prod_kernels` (the live orchestrators)

### Purpose

The 103-`.py` orchestration tree is what frameworks actually `import`. Its files fuse multi-stage transformer blocks and, at the leaf, call into the compiled tree-C `.so`. It also holds variants that exist **nowhere else**: the backward/training kernels and the experimental families.

### The Production Import Edges

The single most important fact for a reimplementer is the import edge from a readable orchestrator to a compiled leaf. These are verbatim from the wheel (`rg -uu` — the tree is `.gitignore`'d, so a plain `rg` returns nothing):

```python
# neuronxcc/nki/_pre_prod_kernels/moe_token_gen.py  (the LIVE decode-MoE orchestrator)
# lines 18-21 — imports the COMPILED tree-C leaves as compute kernels:
from neuronxcc.nki._private_kernels.router_topk import (
    router_topk_isa_kernel, router_topk_kernel_nki)          # → router_topk.so
from neuronxcc.nki._private_kernels.expert_mlps import (
    expert_mlps_isa_inline_kernel, all_expert_mlps_isa_inline_kernel)  # → expert_mlps.so
from neuronxcc.nki._private_kernels.mlp import shared_expert_isa_kernel # → mlp.so

# neuronxcc/nki/_pre_prod_kernels/blockwise_matmul.py  line 25 (MoE prefill)
from neuronxcc.nki._private_kernels.blockwise_mm import (   # → blockwise_mm.so (shim)
    compute_gate_and_up_projections, compute_intermediate_states,
    load_block_expert, load_gate_up_proj_weights, store_block_output, ... )  # ~16 helpers

# neuronxcc/nki/_pre_prod_kernels/attention_token_gen.py  (decode attention)
from neuronxcc.nki._private_kernels.RoPE import RoPE_sbuf                  # → RoPE.so
from neuronxcc.nki._private_kernels.qkv  import rmsnorm_qkv_isa_kernel     # → qkv.so
from neuronxcc.nki._private_kernels.attention import attention_tkg_fwd_isa_kernel  # → attention.so

# misc infra edges
# qkv_cte_impl.py:    from _private_kernels.shard_common import get_seqlen_tile_size
#                     from _private_kernels.mlp import TKG_BS_SEQLEN_THRESHOLD
# max/cascaded_max.py:from _private_kernels.topk.rotational_topk_helpers import predicated_folded_load
# llama3_transformer.py: from _private_kernels.RoPE import RoPE
```

Twelve `_pre_prod` modules import from `_private_kernels` (`rg -uu -l '_private_kernels' | wc -l` = 12). The `blockwise_matmul.py` ↔ `blockwise_mm.so` pair is the circular case: the `.so` is a Cython compile of the `_pre_prod` `blockwise_mm.py` (via `__pyx_import_star`), and `blockwise_matmul.py` then imports the compiled symbols back. The direction is benign; the exact compiled-only-vs-readable-only symbol split was not byte-diffed (gap).

### Top-Level Families and the Experimental Subtree

```text
_pre_prod_kernels/ (103 .py)
├── MoE decode:   moe_token_gen.py (47 KB, 3 @nki.jit kernels) + mlp_tkg/ (15 files,
│                 the O07 2×2 leaf matrix: is_all_expert × is_mxfp4_kernel)
├── MoE prefill:  blockwise_matmul.py (126 KB) + blockwise_mm.py (106 KB) +
│                 blockwise_mm_shard_on_I.py + bwmm_mxfp4.py
├── Attention:    attn_fwd.py (91 KB), attn_fwd_software_pipeline.py (69 KB),
│                 attn_fwd_trn1.py (36 KB, TRN1), attention_token_gen{,_cascaded}.py
├── QKV/proj:     qkv.py, qkv_cte_impl.py (106 KB), qkv_tkg_impl.py, output_proj.py
├── Norm:         rmsnorm_tkg.py, layernorm_tkg.py, rms_norm/rmsnorm_quant*.py
├── MLP (dense):  mlp/ (18 files)
├── Megakernel:   llama3_transformer.py (57 KB)
├── Primitives:   max/cascaded_max.py, topk/topk.py, tp_broadcast.py, util/*.py
└── experimental/   (UNIQUE — no nkilib or _private twin)
    ├── blockwise_mm/   BACKWARD/training blockwise-MM (6 shard×affinity×drop combos)
    ├── gmm/gmm_2d_2d.py grouped matmul (Megablocks-style segmented-T GEMM)
    └── misc/, mlp/      klir_gather, klir_scatter_add, 2nd readable mlp_cte_* copy
```

> **QUIRK —** the training MoE is `_pre_prod`-exclusive and **readable**. `experimental/blockwise_mm/` holds the backward/dropless blockwise-MM; `grep` for "bwd"/"backward" in `blockwise_mm.so` is clean — there is no compiled backward blockwise leaf. This inverts the usual gradient (production = compiled): here the training kernel is the *readable* one and only the forward inference path is compiled into `_private`.

---

## The `_INTERNAL_KERNEL_REGISTRY` — the deeper production path

### Purpose

The import-edge path (tree B → tree C) is the explicit one. There is a second, implicit path that a reimplementer will miss: when the Penguin middle-end lowers a *registered* op macro, `BirCodeGenLoop` resolves the kernel from a compiled-only registry and traces the `.so` directly, without any `_pre_prod` orchestrator in the loop. This is the mechanism by which NxD ends up running the compiled `_private_kernels.blockwise_mm` rather than the readable `moe_cte`, and it generalizes to the whole registered family set.

> **GOTCHA — the registry is not a Python literal; grepping the `nki/` tree finds nothing.** `_INTERNAL_KERNEL_REGISTRY` appears **zero** times across `neuronxcc/nki/*.py`. It is a module-level global compiled into `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-…so` and must be mined from that binary.

The full registry mechanism is the subject of [the internal kernel registry page](internal-kernel-registry.md) (6.6.2); this page grounds only its *inventory* half — which compiled leaves it points at.

### Verbatim Registry Symbols

Mined from `BirCodeGenLoop.cpython-310-…so_strings.json`:

```text
_INTERNAL_KERNEL_REGISTRY            ── the global dict
_build_internal_kernel_registry      ── builder ("Build the registry of all internal
                                         NKI kernels that can be traced to new NKI frontend")
get_internal_kernel_registry         ── getter
_resolve_kernel_config               ── "Look up kernel config from registry, prepare args"
_trace_internal_kernel_to_new_nki_frontend
_trace_kernel_beta2 / _trace_kernel_beta3
NKI_FRONTEND                         ── env selector

Registered module → compiled leaf (verbatim module strings in BirCodeGenLoop.so):
  neuronxcc.nki._private_kernels.blockwise_mm  → blockwise_mm          (MoE prefill)
  neuronxcc.nki._private_kernels.mlp           → fused_mlp_isa_kernel  (dense MLP)
  neuronxcc.nki._private_kernels.conv          → conv2d_* family       (8 variants)
  neuronxcc.nki._private_kernels._internal     → resize_nearest_fixed_dma_kernel
```

### Selection Logic

```c
// BirCodeGenLoop, when the middle-end emits a registered macro kernel
// (codegenBIRKernel / codegenMLPKernel / codegenNormQKV / codegenConv / ...)
function lower_registered_kernel(macro_name, operands):
    reg = get_internal_kernel_registry()              // the compiled global dict
    entry = reg[macro_name]                            // → _private_kernels.<leaf>
    config = _resolve_kernel_config(entry, operands)   // per-kernel attr lambda maps
                                                       //   macro operands → kernel args
                                                       //   (_get_attrs / _get_conv_attrs /
                                                       //    _get_conv_attrs_with_out_shape /
                                                       //    _get_resize_args)
    frontend = getenv("NKI_FRONTEND", "beta2")         // beta2 = KLIR, beta3 = BIR
    if frontend == "beta2":
        binary = _trace_kernel_beta2(entry.so, config) // KLIR tracing → KlirToBirCodegen
    else:
        binary = _trace_kernel_beta3(entry.so, config) // BIR compilation
    cache(binary)                                       // _cache_new_nki_frontend_binary
                                                        //   metric sym "NEW_NKI_FE"
    return binary
```

This is why the compiled `_private_kernels` `.so` is the production path for the registered families and the readable `nkilib` `.py` is not. The readable source runs only when a kernel is **not** registered, or when it is invoked directly via `@nki.jit` from a `_pre_prod` orchestrator (which itself imports the compiled leaves). For the NKI 3-layer trace context (Python trace → `penguin.ir` → {beta3 `BirCodeGenLoop` | beta2 `klr`} → BIR), see [the three-sink kernel model](three-sink-kernel-model.md) (6.6.1) and [the frontend bridge cache](frontend-bridge-cache.md) (6.6.3).

### The Second, Smaller Registry

`_private_kernels/topk/topk_method_mapping.so` is a *kernel-local* dispatch registry — docstring "Mapping of topk method name to implementation for autodispatching topk kernel". It keys `NAIVE_SCANNING / CASCADED / ROTATIONAL` → `naive_scanning_topk / cascaded_2_stage_topk / rotational_topk`, picked by K-size / cost. It is parallel in spirit to the macro registry but local to the top-k family.

---

## Recoverability Ledger — what this corpus does and does not yield

### Recoverable

- **Reference math** for every `nkilib` family (172 readable `.py`) — full source. (6.7.x.)
- **Orchestration logic** for every `_pre_prod` family (103 readable `.py`) — including the production decode-MoE fusion (`moe_token_gen.py`), the megakernel (`llama3_transformer.py`), the `mlp_tkg` dispatcher, and the **training** kernels (`experimental/blockwise_mm/`, `gmm/`).
- **`blockwise_mm` and `llama3_transformer` compiled bodies** — these `.so` are `__pyx_import_star` shims over the readable `_pre_prod` `.py`, so their algorithm IS recoverable.
- **The registry inventory** — module→leaf map and the `NKI_FRONTEND` selector, mined verbatim from `BirCodeGenLoop.so`.

### Compiled-Only Gaps (algorithm bodies NOT recoverable from this corpus)

These leaves are full compiled implementations with **no readable twin in `nkilib` or `_pre_prod`**. Their entry-point symbols are recoverable; their algorithm bodies are not, short of decompiling the Cython `.so`.

| Compiled-only leaf | What it is | Readable twin |
|---|---|---|
| `mamba_prefix_scan_kernel` (in `attention.so`) | SSM / Mamba selective-scan support | **none** — no readable Mamba kernel anywhere |
| `prefix_caching_attention.so` (ISA leaf) | vLLM-style prefix-cache flash-attention compute (`attention_prefix_caching_fwd_kernel`, `prefix_caching_attention_fwd_isa_kernel`) | **partial** (see note below) |
| `collective_matmul.so` | in-kernel TP collective GEMM (all-gather/permute fused into the matmul) | **none** |
| `fused_linear.so` | fused RMSNorm(hidden) @ wQKV ("Allocated kernel: RMSNorm @ wQKV") | **none** |
| `hw_ubench.so` | PE-array TP microbenchmarks ("Cayman" probes) | **none** |
| `conv.so` `conv2d_*` family | production 2D conv (8 layout-tagged variants) | **none** (nkilib has only an experimental conv1d ref) |
| `shard_common.so` | `get_seqlen_tile_size` sharding infra | **none** |
| `topk_method_mapping.so` | top-k auto-dispatch registry | **none** |

> **NOTE — prefix caching is only *partly* compiled-only.** `_pre_prod_kernels/attn_fwd.py` carries a readable prefix-caching path: an `is_prefix_caching` flag threads through the whole CTE attention body (lines 155–1844, e.g. `is_prefix_caching = k_prior is not None`, branching the K/V load and the softmax tiling). So the concept and the CTE-side implementation are readable. What is compiled-only is the **dedicated `prefix_caching_attention.so` ISA leaf** with its own `attention_prefix_caching_fwd_isa_kernel` / `_sharded_nisa_prefix_caching_attention_impl` symbols — a separate, HW-tuned kernel from the `attn_fwd.py` path. The gap is that ISA leaf, not the family.

> **GOTCHA —** the `mamba_prefix_scan_kernel` lives *inside* `attention.so` (entry strings `mamba_prefix_scan_kernel`, `mamba_prefix_scan_kernel_scan_op`), not in its own module. A reimplementer enumerating modules by filename will miss the only SSM/Mamba support in the wheel — it is a symbol, not a file.

The full recoverability gaps ledger for the compiler lives in [the Confidence Ledger appendix](../appendix/confidence-ledger.md) (Part 14). The `conv2d_*` layout-tag taxonomy (`f01b`/`o01i`/`fb01`/`pbp`/`column_packing`) and the Cayman PE-TP ubench semantics are `.so`-internal and flagged there for a future conv/ubench deep dive; the production conv leaves are catalogued in 6.8.x.

---

## Production-vs-Reference — which bytes actually run

```text
RUNS IN PRODUCTION (compiled .so):
  MoE decode    → expert_mlps.so + router_topk.so   (imported by moe_token_gen.py)
  MoE prefill   → blockwise_mm   (registered; NxD uses compiled, not readable moe_cte)
  Attention     → attention.so / attention_cte.so / prefix_caching_attention.so
  QKV/MLP/Norm  → qkv.so / mlp.so / rmsnorm.so       (registered or .so-imported)
  RoPE/cumsum   → RoPE.so / cumsum.so
  conv/resize   → conv.so / _internal.so             (registered → traced by BirCodeGenLoop)

REFERENCE / READABLE (nkilib .py): same MATH, documentation-grade, NOT the running bytes
                                   for registered families. → 6.7.x.

PRE-PROD READABLE GLUE (live, "no warranty"): the orchestrators that fuse the stages
                                   and call the .so leaves (moe_token_gen, llama3_transformer,
                                   the mlp_tkg dispatcher).

TRAINING (pre_prod-exclusive, readable): bwd blockwise-MM, backward_attention (a symbol
                                   inside attention.so), fused_self_attn_bwd.
```

---

## Related Components

| Name | Relationship |
|---|---|
| [Three-sink kernel model](three-sink-kernel-model.md) (6.6.1) | The kernel-node sinks; this page is the physical inventory feeding them |
| [Internal kernel registry](internal-kernel-registry.md) (6.6.2) | Full `_INTERNAL_KERNEL_REGISTRY` mechanism; this page grounds its leaf-inventory half |
| [Frontend bridge cache](frontend-bridge-cache.md) (6.6.3) | The re-trace bridge + `NEW_NKI_FE` cache that materializes the compiled leaves |
| [NeuronCodegen macro](neuroncodegen-macro.md) (6.5.7) | Enumerated the `_private_kernels` leaf set and the 3 `attn_fwd.py` variant names |

## Cross-References

- [Three-sink kernel model](three-sink-kernel-model.md) — 6.6.1, the kernel-node sinks this inventory populates
- [Internal kernel registry](internal-kernel-registry.md) — 6.6.2, the macro→leaf registry mechanism in `BirCodeGenLoop.so`
- [Frontend bridge cache](frontend-bridge-cache.md) — 6.6.3, beta2/beta3 trace + cache of the compiled leaves
- [NeuronCodegen macro](neuroncodegen-macro.md) — 6.5.7, the forward `nl→penguin.ir` builder and the leaf-`.so` enumeration
- [Recoverability gaps](../appendix/confidence-ledger.md) — Part 14, the compiler-wide ledger of compiled-only / non-recoverable subjects
