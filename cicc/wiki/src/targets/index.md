# GPU Target Architecture

45 SM variants across 6 generations. Processor table at `qword_502A920` (stride-2 layout: name + PTX version). Architecture gating throughout the binary controls feature availability.

| | |
|---|---|
| **SM table** | `qword_502A920` (45 entries, `ctor_605` at `0x584510`) |
| **Arch detection** | `sub_95EB40` (38KB, CLI → 3-column mapping) |
| **NVVM arch enum** | `sub_CD09E0` (14.5KB, `NVVM_ARCH_*` strings) |
| **EDG arch gates** | `sub_60E7C0` (~60 feature flags based on SM version) |
| **Backend subtarget** | NVPTXSubtarget (feature offsets at +2498, +2584, +2843) |
| **Target triples** | `nvptx64-nvidia-cuda`, `nvsass-nvidia-directx`, `nvsass-nvidia-spirv` |

## Complete SM Table

| SM | `__CUDA_ARCH` | PTX Ver | Generation | Suffix | Status |
|---|---|---|---|---|---|
| `sm_75` | 750 | 5 | Turing | — | Production |
| `sm_80` | 800 | 5 | Ampere | — | Production |
| `sm_82` | 820 | 5 | Ampere | — | **Undocumented** |
| `sm_86` | 860 | 5 | Ampere | — | Production |
| `sm_87` | 870 | 5 | Ampere | — | Production |
| `sm_88` | 880 | 5 | Ada | — | **Undocumented** |
| `sm_89` | 890 | 5 | Ada | — | Production |
| `sm_90` | 900 | 5 | Hopper | — | Production |
| `sm_90a` | 900 | 6 | Hopper | `a` | Production |
| `sm_100` | 1000 | 6 | Blackwell | — | Production |
| `sm_100a` | 1000 | 7 | Blackwell | `a` | Production |
| `sm_100f` | 1000 | 7 | Blackwell | `f` | Production |
| `sm_101` | 1010 | 6 | Blackwell | — | **Undocumented** |
| `sm_101a` | 1010 | 7 | Blackwell | `a` | **Undocumented** |
| `sm_101f` | 1010 | 7 | Blackwell | `f` | **Undocumented** |
| `sm_102` | 1020 | 6 | Blackwell | — | **Undocumented** |
| `sm_102a` | 1020 | 7 | Blackwell | `a` | **Undocumented** |
| `sm_102f` | 1020 | 7 | Blackwell | `f` | **Undocumented** |
| `sm_103` | 1030 | 6 | Blackwell | — | Production |
| `sm_103a` | 1030 | 7 | Blackwell | `a` | Production |
| `sm_103f` | 1030 | 7 | Blackwell | `f` | Production |
| `sm_110` | 1100 | 6 | Post-Blackwell | — | **Future** |
| `sm_110a` | 1100 | 7 | Post-Blackwell | `a` | **Future** |
| `sm_110f` | 1100 | 7 | Post-Blackwell | `f` | **Future** |
| `sm_120` | 1200 | 6 | Post-Blackwell | — | **Future** |
| `sm_120a` | 1200 | 7 | Post-Blackwell | `a` | **Future** |
| `sm_120f` | 1200 | 7 | Post-Blackwell | `f` | **Future** |
| `sm_121` | 1210 | 6 | Post-Blackwell | — | **Future** |
| `sm_121a` | 1210 | 7 | Post-Blackwell | `a` | **Future** |
| `sm_121f` | 1210 | 7 | Post-Blackwell | `f` | **Future** |

Legacy architectures also present in the table but not in the CLI mapping: `sm_20`, `sm_21`, `sm_30`, `sm_32`, `sm_35`, `sm_37`, `sm_50`, `sm_52`, `sm_53`, `sm_60`, `sm_61`, `sm_62`, `sm_70`, `sm_72`, `sm_73`.

### Suffix Meanings

| Suffix | Meaning | PTX Version |
|---|---|---|
| (none) | Base feature set | 5 (legacy) or 6 (sm_100+) |
| `a` | Accelerated / advanced features | 6 (sm_90a) or 7 (sm_100a+) |
| `f` | Forward-compatible feature set | 7 |

### PTX Version Mapping

| PTX Version | SM Range |
|---|---|
| 5 | sm_20 through sm_90 (legacy/base) |
| 6 | sm_90a, sm_100/101/102/103/110/120/121 (base) |
| 7 | sm_100a/f through sm_121a/f (extended features) |

## Architecture Gating

### EDG-Level Gates — `sub_60E7C0`

Sets ~60 `unk_4D04*` feature flags based on SM version thresholds:

| Threshold | SM Boundary | Features Enabled |
|---|---|---|
| 30399 | sm_75 (Turing) | Base CUDA features |
| 40000 | sm_80 (Ampere) | L2 cache hints, extended atomics |
| 89999 | sm_90 (Hopper) | Cluster ops, TMA, setmaxnreg |
| 109999 | sm_100 (Blackwell) | tcgen05, match instruction |
| 119999 | sm_120 | Post-Blackwell features |

Each flag is gated by a `byte_4CF8*` user-override check.

### Backend Subtarget Feature Offsets (NVPTXSubtarget)

| Offset | Purpose | Stride |
|---|---|---|
| +2498 | Type legality flags (per MVT) | 259 bytes |
| +2584 | Float legality flags (per MVT) | 259 bytes |
| +2843 | Integer type support flag | 1 byte |
| +2870 | Branch distance flag | 1 byte |
| +2871 | Jump table eligibility flag | 1 byte |

### Intrinsic Verifier Architecture Gates — `sub_2C7B6A0`

The NVVMIntrinsicVerifier (143KB) gates intrinsics by SM version:

| Gate | SM | Intrinsics |
|---|---|---|
| sm_72 (Volta) | Convergent branch intrinsics, some atomic ops |
| sm_75 (Turing) | Conversion type intrinsics |
| sm_89 (Ada) | Specific intrinsics |
| sm_90 (Hopper) | Cluster dimensions, TMA, WGMMA |
| sm_100+ (Blackwell) | `.offset.bindless` intrinsics, tcgen05 |

## Generation-Specific Features

### Turing (sm_75)
- Base tensor core (HMMA m16n16k16)
- Conversion intrinsics
- Baseline for cicc v13.0 (default architecture)

### Ampere (sm_80–sm_89)
- `L2::cache_hint` on atomic operations (`sub_21E6420`)
- Extended tensor core shapes (tf32, bf16)
- Async copy (`cp.async`)

### Hopper (sm_90/90a)
- **Cluster operations**: `barrier.cluster.arrive/wait`, `fence.sc.cluster`
- **Cluster registers**: `%cluster_ctarank`, `%clusterid.x/y/z`, `%is_explicit_cluster`
- **Kernel attributes**: `.blocksareclusters`, `.maxclusterrank`, `.reqnctapercluster`, `.cluster_dim`
- **setmaxnreg**: Dynamic register allocation limit (`sub_21EA5F0`)
- **TMA**: Tensor Memory Access with Im2Col, dimension validation, 2CTA mode
- **WGMMA**: Warpgroup MMA async (f16, bf16, tf32, f8)

### Blackwell (sm_100–sm_103)
- **tcgen05**: Next-gen tensor core instruction (`scaleD`, `transA`, `negA`, `negB` at `sub_21E8CD0`)
- **match instruction**: Architecture-gated (`sub_21DF500`, `"match instruction not supported on this architecture!"`)
- **Extended MMA shapes**: m16n8k256
- **`.offset.bindless`** intrinsics

### Post-Blackwell (sm_110, sm_120, sm_121)
- Present in binary but feature details not yet exercised in the codebase

## NVVM Container Architecture Enum — `sub_CD09E0`

The NVVM container format uses an architecture enumeration:

| Enum String | Implied SM |
|---|---|
| `NVVM_ARCH_BLACKWELL_10_0` | sm_100 |
| `NVVM_ARCH_BLACKWELL_10_1` | sm_101 |
| `NVVM_ARCH_BLACKWELL_10_3` | sm_103 |
| `NVVM_ARCH_BLACKWELL_11_0` | sm_110 |
| `NVVM_ARCH_BLACKWELL_12_0` | sm_120 |
| `NVVM_ARCH_BLACKWELL_12_1` | sm_121 |
| `NVVM_ARCH_HOPPER_9_0` | sm_90 |
| `NVVM_ARCH_ADA_8_9` | sm_89 |
| `NVVM_ARCH_AMPERE_8_0` through `8_8` | sm_80–sm_88 |
| `NVVM_ARCH_HW_SM_5_0` through `10_4` | sm_50–sm_104 |

Notable: `NVVM_ARCH_HW_SM_10_4` (sm_104) and `NVVM_ARCH_BLACKWELL_11_0` are not publicly documented.

## Target Triples

| Triple | Purpose |
|---|---|
| `nvptx64-nvidia-cuda` | Standard 64-bit CUDA compilation |
| `nvptx-nvidia-cuda` | 32-bit CUDA compilation |
| `nvptx64-nvidia-nvcl` | OpenCL target |
| `nvsass-nvidia-cuda` | SASS backend (native assembly) |
| `nvsass-nvidia-directx` | DirectX SASS backend |
| `nvsass-nvidia-spirv` | SPIR-V SASS backend |

The `nvsass-nvidia-directx` and `nvsass-nvidia-spirv` triples (discovered in `sub_2C80C90`) reveal that NVIDIA's SASS-level backend supports DirectX and SPIR-V targets alongside traditional CUDA and OpenCL.

## Data Layout Strings

| Mode | Layout | Notes |
|---|---|---|
| 64-bit + shared | `e-p:64:64:64-p3:32:32:32-i1:8:8-...-n16:32:64` | `p3:32:32:32` = 32-bit shared mem pointers |
| 64-bit | `e-p:64:64:64-i1:8:8-...-n16:32:64` | No shared memory specialization |
| 32-bit | `e-p:32:32:32-i1:8:8-...-n16:32:64` | 32-bit mode |

Address space 3 (shared memory) uses 32-bit pointers even in 64-bit mode, controlled by `nvptx-short-ptr` and `nvptx-32-bit-smem` flags.
