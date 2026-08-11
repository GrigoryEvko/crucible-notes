# GPU Target Architecture

45 SM variants across 6 generations. Processor table at `qword_502A920` (stride-2 layout: name + PTX version). Architecture gating throughout the binary controls feature availability.

| | |
|---|---|
| **SM table** | `qword_502A920` (45 entries, `ctor_605` at `0x584510`) |
| **Arch detection** | `sub_95EB40` (14 KB native, CLI -> 3-column mapping) |
| **NVVM arch enum** | `sub_CD09E0` (2.3 KB native, `NVVM_ARCH_*` strings) |
| **EDG arch gates** | `sub_60E7C0` (~60 feature flags based on SM version) |
| **Backend subtarget** | NVPTXSubtarget (feature offsets at +2498, +2584, +2843) |
| **Target triples** | `nvptx64-nvidia-cuda`, `nvsass-nvidia-directx`, `nvsass-nvidia-spirv` |

**Per-SM Deep Dives:**

- [SM 70-89 (Volta through Ada Lovelace)](sm70-89.md) — Feature configuration call order, complete `sub_60E7C0` flag table, atomic lowering, cumulative flag profiles
- [SM 90 — Hopper](sm90-hopper.md) — Thread block clusters, TMA descriptor format and lowering, WGMMA, setmaxnreg, distributed shared memory
- [SM 100 — Blackwell Datacenter](sm100-blackwell.md) — tcgen05 tensor core ISA, arch-conditional vs. family-conditional gating, cvt_packfloat FP4/FP6/MX formats
- [SM 120 — Blackwell Consumer](sm120.md) — No tcgen05, .offset.bindless texture intrinsics, f16 texture support, per-warp `mma.sync.block_scale`; retains the Hopper async stack (TMA, clusters, `setmaxnreg`)

## Complete SM Table

| SM | `__CUDA_ARCH` | PTX Ver | Generation | Suffix | Status | Deep Dive |
|---|---|---|---|---|---|---|
| `sm_75` | 750 | 5 | Turing | — | Production | [sm70-89](sm70-89.md) |
| `sm_80` | 800 | 5 | Ampere | — | Production | [sm70-89](sm70-89.md) |
| `sm_82` | 820 | 5 | Ampere | — | **Undocumented** | [sm70-89](sm70-89.md) |
| `sm_86` | 860 | 5 | Ampere | — | Production | [sm70-89](sm70-89.md) |
| `sm_87` | 870 | 5 | Ampere | — | Production | [sm70-89](sm70-89.md) |
| `sm_88` | 880 | 5 | Ada | — | **Undocumented** | [sm70-89](sm70-89.md) |
| `sm_89` | 890 | 5 | Ada | — | Production | [sm70-89](sm70-89.md) |
| `sm_90` | 900 | 5 | Hopper | — | Production | [sm90](sm90-hopper.md) |
| `sm_90a` | 900 | 6 | Hopper | `a` | Production | [sm90](sm90-hopper.md) |
| `sm_100` | 1000 | 6 | Blackwell | — | Production | [sm100](sm100-blackwell.md) |
| `sm_100a` | 1000 | 7 | Blackwell | `a` | Production | [sm100](sm100-blackwell.md) |
| `sm_100f` | 1000 | 7 | Blackwell | `f` | Production | [sm100](sm100-blackwell.md) |
| `sm_101` | 1010 | 6 | Jetson Thor (pre-rename) | — | **Undocumented** | [sm100](sm100-blackwell.md) |
| `sm_101a` | 1010 | 7 | Jetson Thor (pre-rename) | `a` | **Undocumented** | [sm100](sm100-blackwell.md) |
| `sm_101f` | 1010 | 7 | Jetson Thor (pre-rename) | `f` | **Undocumented** | [sm100](sm100-blackwell.md) |
| `sm_102` | 1020 | 6 | Blackwell | — | **Undocumented** | [sm100](sm100-blackwell.md) |
| `sm_102a` | 1020 | 7 | Blackwell | `a` | **Undocumented** | [sm100](sm100-blackwell.md) |
| `sm_102f` | 1020 | 7 | Blackwell | `f` | **Undocumented** | [sm100](sm100-blackwell.md) |
| `sm_103` | 1030 | 6 | Blackwell | — | Production | [sm100](sm100-blackwell.md) |
| `sm_103a` | 1030 | 7 | Blackwell | `a` | Production | [sm100](sm100-blackwell.md) |
| `sm_103f` | 1030 | 7 | Blackwell | `f` | Production | [sm100](sm100-blackwell.md) |
| `sm_110` | 1100 | 6 | Jetson Thor | — | Production | [sm120](sm120.md#sm_110--jetson-thor) |
| `sm_110a` | 1100 | 7 | Jetson Thor | `a` | Production | [sm120](sm120.md#sm_110--jetson-thor) |
| `sm_110f` | 1100 | 7 | Jetson Thor | `f` | Production | [sm120](sm120.md#sm_110--jetson-thor) |
| `sm_120` | 1200 | 6 | Blackwell (sm120) | — | Production | [sm120](sm120.md) |
| `sm_120a` | 1200 | 7 | Blackwell (sm120) | `a` | Production | [sm120](sm120.md) |
| `sm_120f` | 1200 | 7 | Blackwell (sm120) | `f` | Production | [sm120](sm120.md) |
| `sm_121` | 1210 | 6 | Blackwell (sm120) | — | Production | [sm120](sm120.md) |
| `sm_121a` | 1210 | 7 | Blackwell (sm120) | `a` | Production | [sm120](sm120.md) |
| `sm_121f` | 1210 | 7 | Blackwell (sm120) | `f` | Production | [sm120](sm120.md) |

Legacy architectures also present in the table but not in the CLI mapping: `sm_20`, `sm_21`, `sm_30`, `sm_32`, `sm_35`, `sm_37`, `sm_50`, `sm_52`, `sm_53`, `sm_60`, `sm_61`, `sm_62`, `sm_70`, `sm_72`, `sm_73`.

### Suffix Meanings

| Suffix | Meaning | PTX Version | Detail |
|---|---|---|---|
| (none) | Base feature set | 5 (legacy) or 6 (sm_100+) | All architectures; [sm70-89](sm70-89.md#suffix-behavior) has no suffix-gated logic |
| `a` | Accelerated / advanced features | 6 (sm_90a) or 7 (sm_100a+) | sm_90a enables one EDG gate ([sm90](sm90-hopper.md#architecture-identity)); sm_100a+ enables tcgen05 arch-conditional path ([sm100](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating)) |
| `f` | Forward-compatible feature set | 7 | Implies `a`; never read by cicc logic ([sm120](sm120.md#suffix-behavior)); reserved for ptxas |

### PTX Version Mapping

| PTX Version | SM Range | Notes |
|---|---|---|
| 5 | sm_20 through sm_90 (legacy/base) | All pre-Blackwell base variants |
| 6 | sm_90a, sm_100/101/102/103/110/120/121 (base) | sm_90a is the sole pre-Blackwell PTX 6 target ([sm90](sm90-hopper.md#architecture-identity)) |
| 7 | sm_100a/f through sm_121a/f (extended features) | Required for tcgen05 arch-conditional intrinsics ([sm100](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating)) |

## Architecture Gating

Four subsystems cooperate to configure feature flags from the SM version. The master configurator `sub_60E7C0` runs last and has the highest non-CLI priority. For the complete flag table per tier, see [SM 70-89 Complete sub_60E7C0 Flag Table](sm70-89.md#complete-sub_60e7c0-flag-table).

### Feature Configuration Pipeline

```text
CLI parser (sub_617BD0)              Sets byte_4CF8* override flags
    |
sub_60DFC0 (secondary)               Sets unk_4D041B8 at sm_80+ (__VA_OPT__)
    |
sub_60D650 (optimization level)      ~109 flags from -O level
    |
sub_60E7C0 (master SM configurator)  ~60 flags via SM threshold comparisons
    |--- sub_60E530 (tertiary)        Supplementary progressive unlocks
    |
sub_982C80 (NVPTX subtarget)         224-byte bitfield for LLVM backend
```

Override priority: CLI flag > SM version > Optimization level > C++ standard version > CUDA mode > Virtual arch flag. See [CLI Flag Inventory](../config/cli-flags.md) for the complete CLI flag-to-pipeline routing and [Optimization Levels](../config/optimization-levels.md) for per-level flag differences.

### EDG-Level Gates — `sub_60E7C0`

Sets ~60 `unk_4D04*` feature flags based on SM version thresholds. Each flag is gated by a `byte_4CF8*` user-override check.

| Threshold | SM Boundary | Features Enabled | Detail |
|---|---|---|---|
| > 30399 | sm_75 (Turing) | Base CUDA features, dynamic parallelism | [sm70-89 Turing](sm70-89.md#turing-sm_75--default-architecture) |
| > 40000 | sm_80 (Ampere) | C++20 `__VA_OPT__`, L2 cache hints, extended atomics | [sm70-89 Ampere](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999) |
| > 89999 | sm_90 (Hopper) | Cluster ops, TMA, setmaxnreg, WGMMA fence | [sm90 Feature Flags](sm90-hopper.md#feature-flag-configuration) |
| > 109999 | sm_100 (Blackwell) | tcgen05, match instruction, `dword_4D041AC` | [sm100 Feature Flags](sm100-blackwell.md#feature-flag-configuration) |
| > 119999 | sm_120 | `unk_4D047BC` disabled, `unk_4D0428C` | [sm120 Feature Flags](sm120.md#feature-flag-configuration) |

### Backend Subtarget Feature Offsets (NVPTXSubtarget)

| Offset | Purpose | Stride | Detail |
|---|---|---|---|
| +2498 | Type legality flags (per MVT) | 259 bytes | See [Type Legalization](../llvm/type-legalization.md) |
| +2584 | Float legality flags (per MVT) | 259 bytes | See [Type Legalization](../llvm/type-legalization.md) |
| +2843 | Integer type support flag | 1 byte | — |
| +2870 | Branch distance flag | 1 byte | See [Block Placement](../llvm/block-placement.md) |
| +2871 | Jump table eligibility flag | 1 byte | See [BranchFolding](../llvm/branch-folding.md) |

For the complete NVPTXSubtarget analysis, see [NVPTX Target Infrastructure](../infra/nvptx-target.md).

### Intrinsic Verifier Architecture Gates — `sub_2C7B6A0`

The NVVMIntrinsicVerifier (143KB) gates intrinsics by SM version. For the complete three-layer verification architecture, see [NVVM IR Verifier](../passes/nvvm-verify-deep.md).

| SM gate | Intrinsics | Detail |
|---|---|---|
| sm_72 (Volta) | Convergent branch intrinsics, some atomic ops | [sm70-89 Volta](sm70-89.md#volta-sm_70--threshold-qword_4f077a8--69999) |
| sm_75 (Turing) | Conversion type intrinsics | [sm70-89 Turing](sm70-89.md#turing-sm_75--default-architecture) |
| sm_89 (Ada) | Specific intrinsics | [sm70-89 Ada](sm70-89.md#ada-lovelace-and-ampere-variants-sm_86--sm_89) |
| sm_90 (Hopper) | Cluster dimensions, TMA, WGMMA | [sm90 TMA](sm90-hopper.md#tma--tensor-memory-access), [sm90 WGMMA](sm90-hopper.md#wgmma--warpgroup-matrix-multiply-accumulate) |
| sm_100+ (Blackwell) | `.offset.bindless` intrinsics, tcgen05 | [sm100 tcgen05](sm100-blackwell.md#tcgen05--tensor-core-generation-5), [sm120 .offset.bindless](sm120.md#feature-1-offsetbindless-texture-intrinsics) |

## Feature Gate Matrix

This matrix shows which major compiler features are available at each SM tier. Each cell links to the detailed discussion in the per-SM deep-dive page.

### Tensor Core / MMA Instructions

| Feature | sm_70-75 | sm_80-89 | sm_90/90a | sm_100/103 | sm_110 | sm_120/121 |
|---|---|---|---|---|---|---|
| HMMA m16n16k16 (f16) | [Yes](sm70-89.md#volta-sm_70--threshold-qword_4f077a8--69999) | Yes | Yes | Yes | Yes | Yes |
| IMMA int8/int4, BMMA | sm_75+ | Yes | Yes | Yes | Yes | Yes |
| DMMA fp64, TF32, BF16 | — | [sm_80+](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999) | Yes | Yes | Yes | Yes |
| WGMMA async (f16/bf16/tf32/f8) | — | — | [Yes](sm90-hopper.md#wgmma--warpgroup-matrix-multiply-accumulate) | Yes | Yes | — |
| tcgen05.mma (MX formats) | — | — | — | [a/f only](sm100-blackwell.md#tcgen05--tensor-core-generation-5) | [a/f only](sm120.md#sm_110--jetson-thor) | [No](sm120.md#what-sm_120-does-not-have) |
| mma.sync.block_scale | — | — | — | — | — | [Future](sm120.md#what-sm_120-has-instead) |

See [Tensor / MMA Builtins](../builtins/tensor-mma.md) for the per-builtin ID reference and [Tensor / MMA Codegen](../llvm/mma-codegen.md) for the code generation pipeline.

### Memory and Synchronization

| Feature | sm_70-75 | sm_80-89 | sm_90/90a | sm_100/103 | sm_110 | sm_120/121 |
|---|---|---|---|---|---|---|
| Full atomic memory ordering | [sm_70+](sm70-89.md#atomic-lowering-detail) | Yes | Yes | Yes | Yes | Yes |
| 128-bit atomics | [sm_70+](sm70-89.md#volta-sm_70--threshold-qword_4f077a8--69999) | Yes | Yes | Yes | Yes | Yes |
| L2 cache hint atomics | — | [sm_80+](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999) | Yes | Yes | Yes | Yes |
| Cluster scope atomics | — | — | [Yes](sm90-hopper.md#atomic-cluster-scope) | Yes | Yes | Yes |
| cp.async | — | [sm_80+](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999) | Yes | Yes | Yes | Yes |
| TMA (tensor memory access) | — | — | [Yes](sm90-hopper.md#tma--tensor-memory-access) | Yes | Yes | Yes |
| TMA 2CTA mode, Im2Col_W | — | — | — | [sm_100+](sm90-hopper.md#cpasyncbulktensor-g2s-lowering-sub_36ec510) | sm_100+ | sm_100+ |
| TMA `tile::gather4`/`scatter4` (`a`/`f` target) | — | — | — | Yes | Yes | **—** |

The `tile::gather4`/`tile::scatter4` split is a ptxas-side gate: ptxas v13.1.115
accepts them for `sm_100a`/`f`, `sm_103a`/`f` and `sm_110a`/`f`, and rejects them
for `sm_120a`/`f` and `sm_121a`/`f`. All base (suffix-less) targets reject them.
| setmaxnreg | — | — | [Yes](sm90-hopper.md#setmaxnreg--dynamic-register-count) | Yes | Yes | Yes |
| fence.sc.cluster | — | — | [Yes](sm90-hopper.md#fencesccluster-instruction) | Yes | Yes | Yes |

See [Atomics Builtins](../builtins/atomics.md) for atomic PTX generation detail and [Barriers & Sync](../builtins/barriers.md) for barrier builtins.

### Thread Block Clusters

| Feature | sm_70-89 | sm_90/90a | sm_100+ |
|---|---|---|---|
| `__cluster_dims__` attribute | [Diagnostic 3687](sm70-89.md#ada-lovelace-and-ampere-variants-sm_86--sm_89) | [Yes](sm90-hopper.md#frontend-attributes) | Yes |
| `__launch_bounds__` 3rd param | [Diagnostic 3704](sm70-89.md#ada-lovelace-and-ampere-variants-sm_86--sm_89) | [Yes](sm90-hopper.md#frontend-attributes) | Yes |
| `__block_size__` 5th arg | [Diagnostic 3790](sm70-89.md#ada-lovelace-and-ampere-variants-sm_86--sm_89) | [Yes](sm90-hopper.md#frontend-attributes) | Yes |
| Cluster special registers (15) | — | [Yes](sm90-hopper.md#cluster-special-registers) | Yes |
| barrier.cluster.arrive/wait | — | [Yes](sm90-hopper.md#cluster-barrier-operations) | Yes |
| Cluster query builtins (9) | — | [Yes](sm90-hopper.md#cluster-query-builtins) | Yes |
| Distributed shared memory | — | [Yes](sm90-hopper.md#distributed-shared-memory) | Yes |
| `.blocksareclusters` directive | — | [Yes](sm90-hopper.md#ptx-directives) | Yes |

### Numeric Formats

| Format | First Available | Gate Location | Detail |
|---|---|---|---|
| f16, f32, f64 | All | — | Standard types |
| bf16 (bfloat16) | sm_80+ | [Ampere tensor core](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999) | Tensor core and `cvt` |
| tf32 (TensorFloat-32) | sm_80+ | [Ampere tensor core](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999) | Tensor core only |
| fp8 e4m3, e5m2 | sm_90+ | [WGMMA](sm90-hopper.md#wgmma--warpgroup-matrix-multiply-accumulate) | `cvt_packfloat` cases 2-3 |
| fp6 e2m3, e3m2 | sm_100+ | [cvt_packfloat](sm100-blackwell.md#cvt_packfloat--extended-numeric-formats) | Arch-conditional only |
| fp4 e2m1 | sm_100+ | [cvt_packfloat](sm100-blackwell.md#cvt_packfloat--extended-numeric-formats) | Arch-conditional only |
| ue8m0 (scale factor) | sm_100+ | [cvt_packfloat](sm100-blackwell.md#cvt_packfloat--extended-numeric-formats) | Both arch and family-conditional |
| MX formats (mxf4, mxf8f6f4, mxf4nvf4) | sm_100+ | [tcgen05.mma](sm100-blackwell.md#tcgen05mma--matrix-multiply-accumulate) | tcgen05 a/f sub-variants only |

### Texture and Surface

| Feature | sm_70-89 | sm_90 | sm_100/103 | sm_120/121 |
|---|---|---|---|---|
| Standard texture intrinsics | Yes | Yes | Yes | Yes |
| `.offset.bindless` intrinsics (68 variants) | — | — | — | [sm_120+](sm120.md#feature-1-offsetbindless-texture-intrinsics) |
| f16 texture element types | Limited (builtin 3811 only) | Limited | Limited | [Full support](sm120.md#feature-2-16-bit-texture-element-types) |

See [Surface & Texture Builtins](../builtins/surface-texture.md) for the tex_surf_handler dispatch algorithm.

### EDG Frontend Feature Flags

| Feature | Threshold | Flag | Detail |
|---|---|---|---|
| C++17 feature gates (EDG) | sm_70+ | `unk_4D041DC`, `unk_4D04858`, `unk_4D041EC` | [sm70-89 Flag Table](sm70-89.md#feature-escalation-by-sm-version) |
| C++20 `__VA_OPT__` | sm_80+ | `unk_4D041B8` | [sm70-89 sub_60DFC0](sm70-89.md#sub_60dfc0-sm-gated-flags) |
| C++23 extended float suffixes | sm_70+ | `unk_4D0428C` | [sm70-89 Tertiary Cascade](sm70-89.md#sub_60e530-tertiary-cascade) |
| C++20 feature gates | sm_90+ | `unk_4D043D0`, `unk_4D041B0`, `unk_4D04814` | [sm90 Feature Flags](sm90-hopper.md#feature-flag-configuration) |
| Blackwell extended features | sm_100+ | `unk_4D04184`, `dword_4D041AC` | [sm100 Feature Flags](sm100-blackwell.md#feature-flag-configuration) |

See [EDG 6.6 Frontend](../pipeline/edg.md) for the 737-define configuration system.

### tcgen05 Sub-Variant Access Table

The tcgen05 instruction family uses a two-tier gating system unique to Blackwell. Base variants (`sm_100`, `sm_103`, `sm_110`) are excluded; only `a` and `f` sub-variants pass the bitmask check.

| SmVersion | Target | tcgen05 | Detail |
|---|---|---|---|
| 1001 | sm_100a | Allowed | [sm100 Arch-Conditional Gate](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating) |
| 1002 | sm_100f | Allowed | [sm100 Arch-Conditional Gate](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating) |
| 1031 | sm_103a | Allowed | [sm100 Arch-Conditional Gate](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating) |
| 1032 | sm_103f | Allowed | [sm100 Arch-Conditional Gate](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating) |
| 1101 | sm_110a | Allowed | [sm120: Jetson Thor](sm120.md#sm_110--jetson-thor) |
| 1102 | sm_110f | Allowed | [sm120: Jetson Thor](sm120.md#sm_110--jetson-thor) |
| 1000, 1030, 1100 | base variants | **Blocked** | Bitmask `0xC0000C03` rejects; see [sm100](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating) |
| 1200-1212 | all sm_120/121 | **Blocked** | `v-1101 > 1`; see [sm120 No tcgen05](sm120.md) |

## Generation-Specific Features

### Turing (sm_75)

sm_75 is the default architecture for cicc v13.0, hardcoded as `"compute_75"` in `sub_900130` and `sub_125FB30`.

- Base tensor core (HMMA m16n16k16) — see [Tensor / MMA Builtins](../builtins/tensor-mma.md#hmma----half-precision-ids-678707-sm-70)
- Conversion intrinsics
- Baseline for cicc v13.0 (default architecture) — see [CLI Flag Inventory](../config/cli-flags.md)

Full detail: [SM 70-89 (Volta through Ada)](sm70-89.md)

### Ampere (sm_80-sm_89)

- `L2::cache_hint` on atomic operations (`sub_21E6420`) — see [Atomics Builtins](../builtins/atomics.md)
- Extended tensor core shapes (tf32, bf16) — see [Tensor / MMA Builtins](../builtins/tensor-mma.md)
- Async copy (`cp.async`) — see [SM 70-89: Ampere](sm70-89.md#ampere-sm_80--threshold-qword_4f077a8--79999)
- C++20 `__VA_OPT__` support — the sole differentiator between sm_75 and sm_80+ in `sub_60E7C0`/`sub_60DFC0`

Full detail: [SM 70-89 (Volta through Ada)](sm70-89.md)

### Hopper (sm_90/90a)

- **Cluster operations**: `barrier.cluster.arrive/wait`, `fence.sc.cluster` — see [Cluster Barriers](sm90-hopper.md#cluster-barrier-operations)
- **Cluster registers**: `%cluster_ctarank`, `%clusterid.x/y/z`, `%is_explicit_cluster` — see [Cluster Special Registers](sm90-hopper.md#cluster-special-registers)
- **Kernel attributes**: `.blocksareclusters`, `.maxclusterrank`, `.reqnctapercluster`, `.cluster_dim` — see [PTX Directives](sm90-hopper.md#ptx-directives)
- **setmaxnreg**: Dynamic register allocation limit (`sub_21EA5F0`) — see [setmaxnreg](sm90-hopper.md#setmaxnreg--dynamic-register-count)
- **TMA**: Tensor Memory Access with Im2Col, dimension validation, 2CTA mode — see [TMA](sm90-hopper.md#tma--tensor-memory-access)
- **WGMMA**: Warpgroup MMA async (f16, bf16, tf32, f8) — see [WGMMA](sm90-hopper.md#wgmma--warpgroup-matrix-multiply-accumulate)
- **Distributed shared memory**: `.shared::cluster` qualifier for cross-CTA access — see [DSMEM](sm90-hopper.md#distributed-shared-memory)
- **Mbarrier extensions**: DMA fence/arrive/wait for TMA coordination — see [Mbarrier](sm90-hopper.md#mbarrier-extensions--dma-fencearrivewait)

Full detail: [SM 90 — Hopper](sm90-hopper.md)

### Blackwell Datacenter (sm_100-sm_103)

- **tcgen05**: Next-gen tensor core instruction set (`scaleD`, `transA`, `negA`, `negB` at `sub_21E8CD0`) — see [tcgen05](sm100-blackwell.md#tcgen05--tensor-core-generation-5)
- **Arch-conditional vs. family-conditional gating**: Two-tier feature system for tcgen05 sub-instructions — see [Gating](sm100-blackwell.md#arch-conditional-vs-family-conditional-gating)
- **match instruction**: Architecture-gated (`"match instruction not supported on this architecture!"`) — see [sm100](sm100-blackwell.md)
- **Extended MMA shapes**: m16n8k256 with MX format support
- **`.offset.bindless`** intrinsics — gated at sm_120+, NOT sm_100 (see [sm120 .offset.bindless](sm120.md#feature-1-offsetbindless-texture-intrinsics))
- **cvt_packfloat extended types**: FP4, FP6, MX formats — see [cvt_packfloat](sm100-blackwell.md#cvt_packfloat--extended-numeric-formats)

Full detail: [SM 100 — Blackwell Datacenter](sm100-blackwell.md)

### Jetson Thor (sm_110)

sm_110 is architecturally a datacenter Blackwell derivative (originally sm_101 before rename). It retains full tcgen05/TMEM hardware on `a`/`f` sub-variants. The sm_110 section is documented on the sm_120 page because the two are often compared.

Full detail: [SM 120 — Jetson Thor section](sm120.md#sm_110--jetson-thor)

### Blackwell Consumer (sm_120, sm_121)

- **No tcgen05**: The entire tcgen05 ISA is rejected by cicc for all sm_120/121 variants — see [No tcgen05](sm120.md#what-sm_120-does-not-have)
- **`.offset.bindless`** texture intrinsics (68 variants) — see [.offset.bindless](sm120.md#feature-1-offsetbindless-texture-intrinsics)
- **16-bit texture element types** — see [f16 Texture](sm120.md#feature-2-16-bit-texture-element-types)
- **mma.sync.block_scale**: Present in upstream LLVM 22 but NOT emitted by cicc v13.0 — see [block_scale](sm120.md#what-sm_120-has-instead)
- Tensor core falls back to HMMA/IMMA inherited from sm_70-sm_90 path

Full detail: [SM 120 — Blackwell Consumer](sm120.md)

## NVVM Container Architecture Enum — `sub_CD09E0`

The NVVM container format uses an architecture enumeration. See [NVVM Container](../structs/nvvm-container.md) for the complete tag inventory.

| Enum String | Implied SM | Detail |
|---|---|---|
| `NVVM_ARCH_BLACKWELL_10_0` | sm_100 | [sm100](sm100-blackwell.md#architecture-identity) |
| `NVVM_ARCH_BLACKWELL_10_1` | sm_101 | Undocumented |
| `NVVM_ARCH_BLACKWELL_10_3` | sm_103 | [sm100](sm100-blackwell.md#architecture-identity) |
| `NVVM_ARCH_BLACKWELL_11_0` | sm_110 | [sm120: Jetson Thor](sm120.md#sm_110--jetson-thor) |
| `NVVM_ARCH_BLACKWELL_12_0` | sm_120 | [sm120](sm120.md#architecture-identity) |
| `NVVM_ARCH_BLACKWELL_12_1` | sm_121 | [sm120](sm120.md#architecture-identity) |
| `NVVM_ARCH_HOPPER_9_0` | sm_90 | [sm90](sm90-hopper.md#architecture-identity) |
| `NVVM_ARCH_ADA_8_9` | sm_89 | [sm70-89](sm70-89.md) |
| `NVVM_ARCH_AMPERE_8_0` through `8_8` | sm_80-sm_88 | [sm70-89](sm70-89.md) |
| `NVVM_ARCH_HW_SM_5_0` through `10_4` | sm_50-sm_104 | Hardware SM enum |

Notable: `NVVM_ARCH_HW_SM_10_4` (sm_104) and `NVVM_ARCH_BLACKWELL_11_0` are not publicly documented. NVIDIA's internal naming uses "BLACKWELL" for all sm_100-sm_121 variants, even though sm_110 is marketed as Jetson Thor and sm_120/121 are a distinct consumer microarchitecture (RTX 50xx). See [SM 120: Architecture Identity](sm120.md#architecture-identity) for the "SM 10.4" internal designation.

## Target Triples

| Triple | Purpose | Detail |
|---|---|---|
| `nvptx64-nvidia-cuda` | Standard 64-bit CUDA compilation | Default; see [NVPTX Target Infrastructure](../infra/nvptx-target.md) |
| `nvptx-nvidia-cuda` | 32-bit CUDA compilation | Legacy |
| `nvptx64-nvidia-nvcl` | OpenCL target | — |
| `nvsass-nvidia-cuda` | SASS backend (native assembly) | — |
| `nvsass-nvidia-directx` | DirectX SASS backend | Discovered in `sub_2C80C90`; see [NVVM IR Verifier](../passes/nvvm-verify-deep.md) |
| `nvsass-nvidia-spirv` | SPIR-V SASS backend | Discovered in `sub_2C80C90` |

The `nvsass-nvidia-directx` and `nvsass-nvidia-spirv` triples (discovered in `sub_2C80C90`) reveal that NVIDIA's SASS-level backend supports DirectX and SPIR-V targets alongside traditional CUDA and OpenCL.

## Data Layout Strings

| Mode | Layout | Notes |
|---|---|---|
| 64-bit + shared | `e-p:64:64:64-p3:32:32:32-i1:8:8-...-n16:32:64` | `p3:32:32:32` = 32-bit shared mem pointers |
| 64-bit | `e-p:64:64:64-i1:8:8-...-n16:32:64` | No shared memory specialization |
| 32-bit | `e-p:32:32:32-i1:8:8-...-n16:32:64` | 32-bit mode |

Address space 3 (shared memory) uses 32-bit pointers even in 64-bit mode, controlled by `nvptx-short-ptr` and `nvptx-32-bit-smem` flags. See [Address Spaces](../reference/address-spaces.md) for the complete address space reference.

## SM Version Encoding

Two parallel version tracking systems coexist in the binary:

- **`qword_4F077A8`** — Encodes `SM_MAJOR * 10000 + SM_MINOR * 100`. Used in approximately 309 decompiled files, primarily in the NVVM frontend and optimizer. Boundary thresholds use the `XX99` pattern (e.g., 69999 for pre-Volta, 89999 for pre-Hopper). See [SM 70-89: SM Version Encoding](sm70-89.md#sm-version-encoding) for full detail.

- **`unk_4D045E8`** — Stores the raw SM number as a decimal (e.g., 75 for sm_75, 89 for sm_89). Used in approximately 12 decompiled files, primarily in the builtin checker and atomic lowering logic. See [SM 70-89: unk_4D045E8 Frontend Gates](sm70-89.md#unk_4d045e8-frontend-gates) for the complete gate table.

## Cross-References

- [NVPTX Target Infrastructure](../infra/nvptx-target.md) — NVPTXTargetMachine, NVPTXSubtarget, TTI hooks
- [Tensor / MMA Builtins](../builtins/tensor-mma.md) — Per-builtin-ID reference for all MMA generations
- [Tensor / MMA Codegen](../llvm/mma-codegen.md) — Code generation pipeline for tensor core operations
- [Atomics Builtins](../builtins/atomics.md) — Atomic PTX generation and scope validation
- [Surface & Texture Builtins](../builtins/surface-texture.md) — Texture intrinsic dispatch algorithm
- [NVVM IR Verifier](../passes/nvvm-verify-deep.md) — SM-gated intrinsic verification
- [NVVM Container](../structs/nvvm-container.md) — Architecture enum and tag inventory
- [CLI Flag Inventory](../config/cli-flags.md) — `-arch=compute_XX` parsing and flag routing
- [Optimization Levels](../config/optimization-levels.md) — Per-level flag differences that interact with SM gates
- [EDG 6.6 Frontend](../pipeline/edg.md) — 737-define configuration, CUDA keyword handling
- [Address Spaces](../reference/address-spaces.md) — Address space 3 shared memory and data layout strings
- [GPU Execution Model](../gpu-execution-model.md) — CTA, warp, and cluster execution model context
