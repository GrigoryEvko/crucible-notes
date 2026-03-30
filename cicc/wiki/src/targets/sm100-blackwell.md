# Blackwell Datacenter (sm_100, sm_100a, sm_103, sm_103a)

The Blackwell datacenter family introduces the fifth-generation tensor core instruction set (tcgen05), new floating-point formats (FP4, FP6, MX formats), and a sophisticated arch-conditional versus family-conditional feature gating system. sm_100/sm_100a targets the NVIDIA B200, while sm_103/sm_103a targets Blackwell Ultra (GB300 system). Both share the tcgen05 ISA but differ in `__CUDA_ARCH` values and minor tensor core configuration.

## Architecture Identity

Six Blackwell arch constants are defined in `sub_CD09E0`:

| NVVM Enum | Numeric Value | Implied SM |
|---|---|---|
| `NVVM_ARCH_BLACKWELL_10_0` | 1000 | sm_100 |
| `NVVM_ARCH_BLACKWELL_10_1` | 1010 | sm_101 |
| `NVVM_ARCH_BLACKWELL_10_3` | 1030 | sm_103 |
| `NVVM_ARCH_BLACKWELL_11_0` | 1100 | sm_110 (Jetson Thor) |
| `NVVM_ARCH_BLACKWELL_12_0` | 1200 | sm_120 |
| `NVVM_ARCH_BLACKWELL_12_1` | 1210 | sm_121 |

Notable: sm_110 (Jetson Thor) was originally designated sm_101 before being renumbered to its own 11.x line. Despite the rename, both remain in the Blackwell family (`NVVM_ARCH_BLACKWELL_*`). The numeric encoding follows the standard `major*100 + minor*10` formula: 11*100 + 0*10 = 1100.

### SM Variant Table

Each Blackwell datacenter target has base, accelerated (`a`), and forward-compatible (`f`) sub-variants:

| Variant | `__CUDA_ARCH` | PTX Version | Product |
|---|---|---|---|
| `sm_100` | 1000 | 6 | B200 base |
| `sm_100a` | 1000 | 7 | B200 accelerated |
| `sm_100f` | 1000 | 7 | B200 forward-compatible |
| `sm_103` | 1030 | 6 | Blackwell Ultra / GB300 base |
| `sm_103a` | 1030 | 7 | Blackwell Ultra / GB300 accelerated |
| `sm_103f` | 1030 | 7 | Blackwell Ultra / GB300 forward-compatible |

The undocumented sm_101 and sm_102 targets also exist in the processor table (`ctor_605`) with their own `a`/`f` variants. sm_101 maps to `__CUDA_ARCH=1010` and sm_102 to `__CUDA_ARCH=1020`. No unique feature gates differentiate them from sm_100 in cicc.

### Suffix Semantics

The sub-variant flags are stored in EDG frontend globals:

- **`unk_4D045E8`** — Major SM number (100, 103)
- **`unk_4D045E4`** — Accelerated flag; set for both `a` and `f` variants
- **`unk_4D045E0`** — Forward-compatible flag; set only for `f` variants

The `f` suffix implies `a` — whenever the forward-compatible flag is set, the accelerated flag is also set. In cicc v13.0, the `f` flag is set during CLI parsing and reset in `sub_615CB0` but is never read by any compiler logic. It exists for future-proofing and potential ptxas-level differentiation.

## Arch-Conditional vs. Family-Conditional Gating

Blackwell introduces a two-tier feature gating system that distinguishes between "arch-conditional" and "family-conditional" access to instructions. This pattern repeats across every tcgen05 handler.

The gate check at `sub_30462A0`, `sub_304E6C0`, and `sub_36E9630` uses a complex encoding:

```
v = arch_version (offset +340 of arch struct)
if (v > 0x408) {           // 0x408 = 1032 = sm_103.2
    if (v - 1101 > 1)      // allows {1101, 1102} — sm_110a/sm_110f (Jetson Thor)
        goto ERROR;
} else if (v <= 0x3E8 || ((1LL << ((v & 0xFF) + 23)) & 0xC0000C03) == 0) {
    goto ERROR;             // 0x3E8 = 1000 = sm_100 base
}
```

The bitmask `0xC0000C03` selects specific sub-variants when shifted by `(v & 0xFF) + 23`. PTX version gates further refine access: family-conditional features require PTX >= 86, while arch-conditional features require PTX >= 88.

Features gated by **both** arch-conditional and family-conditional (broader access): tcgen05.fence, tcgen05.wait, tcgen05.relinquish.alloc, tcgen05.cp, tcgen05.commit, tcgen05.alloc, tcgen05.mma, and the `ue8m0x2` type in `cvt_packfloat`.

Features gated by **arch-conditional only** (stricter): `{fp6/fp4}x2` types in `cvt_packfloat`, INT8 type in tcgen05.mma, MXF4/MXF4NVF4 with sparsity, and explicit scale vector size.

## tcgen05 — Tensor Core Generation 5

The tcgen05 instruction family is the primary new ISA extension for Blackwell datacenter. All tcgen05 instructions are handled in `sub_30462A0` and `sub_304E6C0`.

### Lifecycle Instructions

| Instruction | Opcode | ISD | Operands | Purpose |
|---|---|---|---|---|
| `tcgen05.alloc` | 10080 | 4765 | Basic allocation | Allocate tensor core accumulator memory |
| `tcgen05.alloc` (multicast) | 10083 | 4770/4771 | 32-bit flag variant | Multicast allocation |
| `tcgen05.dealloc` | 10140 | 4827 | 4 operands | Deallocate tensor core memory |
| `tcgen05.commit` | 10090/10091 | 4772–4777 | Mask variants | Commit pending operations |
| `tcgen05.fence` | 10143 | 4830 | 2 operands | Memory fence for tensor ops |
| `tcgen05.wait` | 10351 | 5020 | 2 operands | Wait for tensor ops to complete |
| `tcgen05.relinquish.alloc` | 10311 | 4941 | 2 operands | Relinquish allocated tensor memory |
| `tcgen05.cp.*` | 10101 | 4790 | 4 operands | Copy operations for tensor data |

The commit instruction has multiple variants based on multicast mask size. Only 16-bit and 32-bit masks are valid; other sizes produce an error.

### tcgen05.mma — Matrix Multiply-Accumulate

The main MMA instruction is handled in `sub_304E6C0` (opcodes 10299–10309) and validated in `sub_36E9630`. The operand encoding packs configuration into bitfields:

**Data types** (bits 8–6 of operand):

| Value | Kind | Notes |
|---|---|---|
| 0 | `kind::mxf4nvf4` | MX FP4 with NV FP4 |
| 1 | `kind::f8f6f4` | Standard FP8/FP6/FP4 |
| 2 | `kind::mxf8f6f4` | MX variant of f8f6f4 |
| 3 | `kind::f16` | Half precision |
| 4 | `kind::i8` | 8-bit integer (arch-conditional only) |
| 5 | `kind::tf32` | TensorFloat-32 |
| 7 | `kind::mxf4` | MX FP4 |

**Scale vector sizes** (bits 3–2):

| Value | Modifier | Constraints |
|---|---|---|
| default | `.scale_vec::1X` | Not for mxf4nvf4 or mxf4 |
| 2 | `.scale_vec::2X` | Not for mxf8f6f4 |
| 3 | `.scale_vec::4X` | Not for mxf8f6f4 or mxf4 |

**Block scale** (bits 10–9): `.block16` (16-element block scaling) or `.block32` (32-element block scaling). Not supported for f16, tf32, f8f6f4, or i8.

**Weight stationary** (bit 0): `.ws` flag. Incompatible with `cta_group::2`, `mxf8f6f4`, and FP4 types.

**Sparsity** (bit 5): Restricted for MXF4 and MXF4NVF4 types on arch-conditional variants only.

**Scale input accumulator** (bit 4): Scales the accumulator input. Only usable with f16 and tf32 types. Notably, this is NOT supported on the `a` sub-variants (sm_100a at v=1001, sm_103a at v=1033) but IS supported on base variants (sm_100 at v=1000, sm_103 at v=1030) and sm_120+.

**CTA group** (bit 1): `cta_group::1` (clear) or `cta_group::2` (set).

**Collector modes** (from `sub_35F38B0`): `.collector::a::fill`, `.collector::a::use`, `.collector::a::lastuse`, and `.collector::b` with `::ws` sub-variants. Constraint: cannot use `collector::a::use` or `collector::a::fill` with the `ashift` modifier.

### tcgen05.cp Copy Shapes

The copy instruction shape emission at `sub_35F5090` supports:

| Shape | Bits 3–1 Value |
|---|---|
| `.128x256b` | 0 |
| `.4x256b` | 1 |
| `.128x128b` | 2 |
| `.64x128b` | 3 |
| `.32x128b` | 4 |

Destination format modifiers: `.b8x16` (base), `.b6x16_p32` (6-bit with 32-bit padding), `.b4x16_p64` (4-bit with 64-bit padding).

Multicast modes: `.warpx2::02_13` (warp pairs 0,2 and 1,3), `.warpx2::01_23` (warp pairs 0,1 and 2,3), `.warpx4` (all 4 warps).

## cvt_packfloat — Extended Numeric Formats

The `cvt_packfloat` intrinsic (`sub_304FBD0` for validation, `sub_35ED820` for emission) has a base requirement of SM >= 90 and PTX >= 78. Blackwell adds four new types:

| Case | Type | Generation |
|---|---|---|
| 0 | `.f32` | sm_90+ |
| 1 | `.f16x2` | sm_90+ |
| 2 | `.e4m3x2` (FP8 E4M3) | sm_90+ |
| 3 | `.e5m2x2` (FP8 E5M2) | sm_90+ |
| 4 | `.bf16x2` (BFloat16) | sm_90+ |
| 5 | `.e2m1x2` (FP4 E2M1) | sm_100+ |
| 6 | `.e2m3x2` (FP6 E2M3) | sm_100+ |
| 7 | `.e3m2x2` (FP6 E3M2) | sm_100+ |
| 8 | `.ue8m0x2` (UE8M0 scale) | sm_100+ |

The `ue8m0x2` type is gated by both arch-conditional and family-conditional paths, while `{fp6/fp4}x2` types (`e2m1x2`, `e2m3x2`, `e3m2x2`) are arch-conditional only.

## tcgen05 Commit with Mbarrier

The commit modifier emission at `sub_35F4E30` combines tensor core commit with mbarrier synchronization:

- `.cta_group::1` / `.cta_group::2` — Group selection
- `.mbarrier::arrive::one` — Mbarrier arrive modifier
- `.shared::cluster` — Shared memory cluster scope
- `.multicast::cluster` — Multicast cluster scope

## sm_100 vs. sm_103 Differences

Both families share the full tcgen05 ISA. Observable differences in cicc:

- **`__CUDA_ARCH`**: 1000 vs. 1030
- **Tensor core operand range**: sm_103 may handle wider operand loops (offset 760 vs. 600 for simpler variants in cases 10303/10308)
- **Scale input accumulator**: Not available on `a` sub-variants of either family

No sm_103-specific feature gates exist beyond the `__CUDA_ARCH` value. Hardware differences between B200 and GB300 are resolved at the ptxas level.

## Feature Flag Configuration

At the sm_100+ threshold (`qword_4F077A8 > 109999`), the master configurator `sub_60E7C0` enables:

| Flag | Condition |
|---|---|
| `unk_4D04184` | Unconditional |
| `unk_4D04800` | Requires CUDA mode + C++20 |
| `dword_4D041AC` | Guarded by `byte_4CF8172` |

## Key Binary Locations

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_CD09E0` | `0xCD09E0` | NVVM arch enum (all Blackwell constants) | NVVM arch enum (all Blackwell constants) |
| `sub_1C1B150` | `0x1C1B150` | Second arch enum copy (LLVM module metadata) | Second arch enum copy (LLVM module metadata) |
| `sub_30462A0` | `0x30462A0` | tcgen05 intrinsic handler (alloc/dealloc/commit/fence/wait/cp) | tcgen05 intrinsic handler (alloc/dealloc/commit/fence/wait/cp) |
| `sub_304E6C0` | `0x304E6C0` | tcgen05.mma intrinsic handler + SelectionDAG lowering | tcgen05.mma intrinsic handler + SelectionDAG lowering |
| `sub_36E9630` | `0x36E9630` | tcgen05.mma validation + ISD opcode selection | tcgen05.mma validation + ISD opcode selection |
| `sub_304FBD0` | `0x304FBD0` | cvt_packfloat intrinsic handler | cvt_packfloat intrinsic handler |
| `sub_35ED820` | `0x35ED820` | cvt_packfloat type string emission | cvt_packfloat type string emission |
| `sub_35F3330` | `0x35F3330` | tcgen05.mma modifier emission (kind, scale, cta_group) | tcgen05.mma modifier emission (kind, scale, cta_group) |
| `sub_35F38B0` | `0x35F38B0` | tcgen05.mma modifier emission (ashift, collector) | tcgen05.mma modifier emission (ashift, collector) |
| `sub_35F4E30` | `0x35F4E30` | tcgen05 commit modifier emission | tcgen05 commit modifier emission |
| `sub_35F5090` | `0x35F5090` | tcgen05.cp shape/format emission | tcgen05.cp shape/format emission |
| `sub_95EB40` | `0x95EB40` | CLI arch string mapping | CLI arch string mapping |
| `sub_617BD0` | `0x617BD0` | `compute_NNN` string parsing | `compute_NNN` string parsing |
| `ctor_605` | `0x584510` | Processor variant string table | Processor variant string table |
| `ctor_356` | `0x50C890` | LLVM processor description table | LLVM processor description table |
