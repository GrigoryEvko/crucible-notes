# NVVM tcgen05 Ops

## Abstract

`nvvm.tcgen05.*` covers the Blackwell (sm_100+) tensor-memory family. Tensor memory (TMEM) is a per-SM scratchpad allocated and freed through the dialect's alloc / dealloc ops, accessed through `ld` / `st` and the long-K MMA path, and torn down before the kernel exits. The roster below is the only path to TMEM from MLIR; Hopper's WGMMA family ([`nvvm.wgmma.*`](wgmma-ops.md)) does not reach Blackwell tensor cores. See [tcgen05 Tensor Memory Model](../../topics/tcgen05-tensor-memory-model.md) for the TMEM allocation discipline and the variant taxonomy, and [tcgen05 Machine Validation](../../codegen/tcgen05-wgmma-mbarrier-cluster.md#tcgen05-machine-validation) for the codegen-side verifier rules.

`tcgen05.mma` carries a control-word modifier table that selects element-type interpretation, sparsity, block-scaling, and collector behaviour. Block-scaled UMMA exposes scale-vector size and scale-format enums; the cross-product produces several thousand legal PTX forms from a single dialect op.

## Op Roster

The "Properties slots used" column tracks where each op stores its attribute payload in the inline Properties record; see [Properties Blob — Per-op-family slot maps](properties-blob-and-attr-parsers.md#per-op-family-properties-slot-maps) for the exact byte offsets.

| Op | Role | Properties slots used |
|---|---|---|
| `nvvm.tcgen05.alloc` / `.shared` | request a TMEM range | `cta_group` |
| `nvvm.tcgen05.dealloc` | release a TMEM range | `cta_group` |
| `nvvm.tcgen05.relinquish_alloc_permit` | drop the alloc-permit token | `cta_group` |
| `nvvm.tcgen05.ld` | load from TMEM to registers | (none — operand-typed) |
| `nvvm.tcgen05.st` | store from registers to TMEM | (none — operand-typed) |
| `nvvm.tcgen05.cp` | copy TMEM tile across CTAs | `multicast`, `shape`, `src_fmt` |
| `nvvm.tcgen05.mma` | MMA into TMEM accumulator | `typeA/cType`, `collectorA`, `scale_d`, layout-bits |
| `nvvm.tcgen05.mma.sp` | sparse-input variant of above | same + sparse metadata operand |
| `nvvm.tcgen05.mma.block_scale` | block-scaled variant | `cType`, `collectorA`, `scale_d`, `layout`, `kindA`, `kindB` |
| `nvvm.tcgen05.mma.sp.block_scale` | sparse + block-scaled | merge of sparse and block-scaled fields |
| `nvvm.tcgen05.mma.ws` | weight-stationary variant | operand-only |
| `nvvm.tcgen05.commit` / `.commit.arrive` | close a group; optionally signal a barrier | `cta_group` |
| `nvvm.tcgen05.wait` | wait on `load` or `store` group | `wait_kind` |
| `nvvm.tcgen05.shift` | shift register fragment across TMEM | operand-only |
| `nvvm.tcgen05.fence` | producer / consumer fence | `tcgen05_fence` (before / after) |

## Operand Tables

### `nvvm.tcgen05.alloc[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `ptr addrspace(3)` (or generic) | output slot for the allocated TMEM base |
| operand 1 | `n` | `i32` | column count to allocate (must be a multiple of 32) |
| attribute | `cta_group` | enum `tcgen05_group` | `cta_1` or `cta_2` for 1-CTA or 2-CTA cooperative allocation |

### `nvvm.tcgen05.dealloc` / `.relinquish_alloc_permit`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tmem_base` | `i32` | TMEM column index returned by `alloc` |
| operand 1 (`dealloc`) | `n` | `i32` | column count being released |
| attribute | `cta_group` | enum `tcgen05_group` | matches the `alloc`'s `cta_group` |

### `nvvm.tcgen05.ld` / `nvvm.tcgen05.st`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tmem_addr` | `i32` | TMEM column address |
| operand 1 (`st`) | `frag` | `!llvm.struct<(i32, ...)>` | register fragment to store |
| result 0 (`ld`) | `frag` | `!llvm.struct<(i32, ...)>` | register fragment loaded |
| attribute (encoded into mnemonic) | shape | `m32n8` / `m32n16` / `m32n32` / ... | tile shape that fixes the fragment width |
| attribute (encoded into mnemonic) | `num` | `x1` / `x2` / `x4` / ... | replication factor |
| attribute (encoded into mnemonic) | `pack` | `pack` / `unpack` | per-thread packing mode |

### `nvvm.tcgen05.cp`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tmem_dst` | `i32` | destination TMEM column |
| operand 1 | `tmem_src` | `i32` | source TMEM column |
| attribute | `shape` | enum `tcgen05_cp_shape` | tile shape selector |
| attribute | `multicast` | enum `tcgen05_cp_multicast` | `none` / `warp_x2` / `warp_x4` |
| attribute | `src_fmt` | enum `tcgen05_cp_src_fmt` | source element format |

### `nvvm.tcgen05.mma` (dense)

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tmem_d` | `i32` | TMEM accumulator column |
| operand 1 | `desc_a` | `i64` | [SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for A, or TMEM column for `a_in_tmem` form |
| operand 2 | `desc_b` | `i64` | SMEM descriptor for B |
| operand 3 | `scale` | `i32` | accumulator-update scale (compile-time `0` or `1`) |
| attribute | `kind` | enum `tcgen05_mma_kind` | `f8f6f4` / `mxf4` / `mxf4nvf4` / `f16` / `tf32` / `i8` |
| attribute | `cta_group` | enum | `cta_1` / `cta_2` |
| attribute | `collectorA` | enum `tcgen05_mma_collectorop` | `discard` / `fill` / `use` / `last_use` |
| attribute | `scale_d` | enum | controls how `scale` selects between init and accumulate |
| attribute | `layout` | enum `TmemLayout` | TMEM tile layout |

### `nvvm.tcgen05.mma.sp` (sparse)

Adds one operand:

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 4 | `sparse_metadata` | `i32` | TMEM column holding the sparse selectors |

### `nvvm.tcgen05.mma.block_scale`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0..3 | same as `mma` | — | same accumulator + descriptors + scale |
| operand 4 | `scale_a_vec` | `i32` | TMEM column for A scale vector |
| operand 5 | `scale_b_vec` | `i32` | TMEM column for B scale vector |
| attribute | `kindA` / `kindB` | enum `block_scale_format` | `E8M0` / `E4M3FN` |
| attribute | `scale_vec_size` | enum `scale_vec_size` | `16` or `32` |

The `(atom_K, vecSize)` triples accepted by the verifier are documented on the [cute_nvgpu MMA atoms page (SM100 UMMA block-scaled)](../cute_nvgpu/mma-atoms-sm70-120.md#sm100-umma-block-scaled-atom_k-vecsize-atoms).

### `nvvm.tcgen05.commit` / `.commit.arrive`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 (`commit.arrive`) | `barrier` | `ptr addrspace(3)` | mbarrier slot to signal |
| attribute | `cta_group` | enum | matches the in-flight MMA's `cta_group` |

### `nvvm.tcgen05.wait`

| Position | Name | Type | Notes |
|---|---|---|---|
| attribute | `wait_kind` | enum `tcgen05_wait` | `load` (drain TMEM loads) or `store` (drain TMEM stores) |

### `nvvm.tcgen05.fence`

| Position | Name | Type | Notes |
|---|---|---|---|
| attribute | `tcgen05_fence` | enum | `before` (producer) or `after` (consumer) |

## Control-Word Modifier Table

The PTX form `tcgen05.mma.sync.aligned.{kind}.cta_group::{1,2}.{layout}.{collector}` packs several modifiers into the mnemonic. See [tcgen05 Tensor Memory Model — Control Word Layout](../../topics/tcgen05-tensor-memory-model.md#control-word-layout) for the bit-level encoding and [tcgen05 Machine Validation — Control-Word Bit Layout](../../codegen/tcgen05-wgmma-mbarrier-cluster.md#control-word-bit-layout) for the codegen-side checks. The table below pairs each modifier with its NVVM attribute and the legal value range.

| PTX modifier | NVVM attribute | Values |
|---|---|---|
| `{kind}` | `kind` | `f8f6f4` / `mxf4` / `mxf4nvf4` / `f16` / `tf32` / `i8` |
| `cta_group::{1,2}` | `cta_group` | `cta_1` (single-CTA) / `cta_2` (cluster-coop 2-CTA) |
| `{layout}` | `layout` | `mn` (row-major) / `kn` (canonical K-major) |
| `{collector}` | `collectorA` | `discard` / `fill` / `use` / `last_use` |
| `.sp` | (op mnemonic carries `.sp`) | sparse (`.sp`) vs dense |
| `.block_scale` | (op mnemonic carries `.block_scale`) | block-scaled vs unscaled |
| `.scale::vec::{16,32}` | `scale_vec_size` | `16` / `32` |
| `.{sfA}.{sfB}` | `kindA` / `kindB` | scale-factor element format |

The collector modifier controls how the MMA pipeline reuses register-file data across iterations: `discard` evicts on commit, `fill` accumulates without evicting, `use` consumes a previously-filled buffer, `last_use` consumes and then evicts.

## LLVM Intrinsic Mapping

| Op | LLVM intrinsic |
|---|---|
| `nvvm.tcgen05.alloc` (addrspace=3, shared SMEM dest) | `llvm.nvvm.tcgen05.alloc.cta_group.{1,2}.shared` |
| `nvvm.tcgen05.alloc` (addrspace=0/1, generic/global dest) | `llvm.nvvm.tcgen05.alloc.cta_group.{1,2}` |
| `nvvm.tcgen05.dealloc` | `llvm.nvvm.tcgen05.dealloc.cta_group.{1,2}` |
| `nvvm.tcgen05.ld` | `llvm.nvvm.tcgen05.ld.{shape}.{num}` |
| `nvvm.tcgen05.st` | `llvm.nvvm.tcgen05.st.{shape}.{num}` |
| `nvvm.tcgen05.mma` | `llvm.nvvm.tcgen05.mma.{kind}.cta_group.{1,2}.{collector}` |
| `nvvm.tcgen05.mma.sp` | `llvm.nvvm.tcgen05.mma.sp.{kind}.cta_group.{1,2}.{collector}` |
| `nvvm.tcgen05.mma.block_scale` | `llvm.nvvm.tcgen05.mma.block_scale.{kind}.{scale_vec}.cta_group.{1,2}.{collector}` |
| `nvvm.tcgen05.cp` | `llvm.nvvm.tcgen05.cp.{shape}.{multicast}.{src_fmt}` |
| `nvvm.tcgen05.commit` | `llvm.nvvm.tcgen05.commit.cta_group.{1,2}` |
| `nvvm.tcgen05.commit.arrive` | `llvm.nvvm.tcgen05.commit.arrive.cta_group.{1,2}` |
| `nvvm.tcgen05.wait` | `llvm.nvvm.tcgen05.wait.{load,store}` |
| `nvvm.tcgen05.fence` | `llvm.nvvm.tcgen05.fence.{before,after}.thread` |

## PTX Templates

```text
tcgen05.alloc.cta_group::{1,2}.shared::cta.b32 [%tmem], %n;
tcgen05.dealloc.cta_group::{1,2}.b32 [%tmem], %n;
tcgen05.relinquish_alloc_permit.cta_group::{1,2};

tcgen05.ld.sync.aligned.{shape}.{num}.b32 {%r0, %r1, ...}, [%tmem];
tcgen05.st.sync.aligned.{shape}.{num}.b32 [%tmem], {%r0, %r1, ...};

tcgen05.mma.sync.aligned.{kind}.cta_group::{1,2}.{layout}.{collector}
    [%tmem_d], %desc_a, %desc_b, %scale;

tcgen05.mma.sp.sync.aligned.{kind}.cta_group::{1,2}.{layout}.{collector}
    [%tmem_d], %desc_a, %desc_b, [%sparse_meta], %scale;

tcgen05.mma.block_scale.sync.aligned.{kind}.scale::vec::{16,32}.cta_group::{1,2}.{layout}.{collector}.{sfA}.{sfB}
    [%tmem_d], %desc_a, %desc_b, [%sf_a], [%sf_b], %scale;

tcgen05.cp.{shape}.{multicast}.{src_fmt} [%tmem_dst], [%tmem_src];
tcgen05.commit.cta_group::{1,2};
tcgen05.commit.arrive.cta_group::{1,2}.b64 [%mbar];
tcgen05.wait::{load,store}.sync.aligned;
tcgen05.fence::{before,after}.thread;
```

The descriptor operands `%desc_a` and `%desc_b` are 64-bit SMEM descriptors when the operand is SMEM-resident, or TMEM column indices when the operand is TMEM-resident.

## Inline-PTX Variants

`nvvm.tcgen05.cp` reaches PTX through `llvm.inline_asm` when the multicast / src_fmt combination has no matching LLVM intrinsic at the snapshot revision Tileiras tracks:

```text
asm template: "tcgen05.cp.{shape}.{multicast}.{src_fmt} [%dst], [%src];"
constraints : "r,r"
```

The two `r` slots are the destination and source TMEM column indices. The shape, multicast, and src_fmt tokens are baked into the template literal at lowering time; the constraint string never changes.

## Per-Arch Availability

| Op family | SM floor | `ptx_min` |
|---|---|---|
| `alloc` / `dealloc` / `relinquish_alloc_permit` | sm_100a | 8.6 |
| `ld` / `st` | sm_100a | 8.6 |
| `cp` | sm_100a (+ `sm_100f` for the `f`-suffixed variants) | 8.6 |
| `mma` / `mma.sp` | sm_100a | 8.6 |
| `mma.block_scale` / `mma.sp.block_scale` | sm_100a | 8.6 |
| `commit` / `commit.arrive` / `wait` / `fence` | sm_100a | 8.6 |

`sm_100a` is the architecture-qualified Blackwell target; the family is also legal on `sm_100f` for the few `f`-suffixed copy variants. Datacenter Blackwell (sm_100) is the only sub-arch the dialect exposes; Blackwell Ultra (sm_103) and Jetson Thor (sm_110) reuse the same op surface. See [Per-SM Emission Templates — SM100 / SM103](../../codegen/per-sm-emission-templates.md#sm100-sm103) for the codegen-side templates and [NVPTX Subtarget Feature Matrix](../../codegen/nvptx-subtarget-and-feature-matrix.md) for the feature gating.

## Verifier Invariants

- TMEM column counts are multiples of 32.
- `cta_group` agrees between matched `alloc` / `dealloc` and between the in-flight MMA and its `commit` / `wait`.
- `scale` is a compile-time immediate.
- Block-scaled `(atom_K, vecSize)` matches one of `(32, 32)`, `(64, 16)`, `(64, 32)`; other combinations are rejected by the per-combo expectation diagnostics listed under [nv_tileas Verifiers — Block-Scaled MMA Verification](../nv_tileas/verifiers.md#block-scaled-mma-verification) (e.g. `"expects A/B element types to be Float4E2M1FNType and sfa/sfb element types to be Float8E8M0FNUType when (atom_K=64 && vecSize=32)"`).
- Sparse metadata column must be valid TMEM and non-zero stride.
- Accumulator element type is `f32` for every block-scaled variant.
- `kindA` and `kindB` agree (no mixed scale-factor formats).
