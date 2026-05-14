# Intrinsic ID Switch + Name Table

## Abstract

`tileiras` carries the LLVM constant-folder predicate that decides whether a `CallBase` can be evaluated at compile time. It is the upstream `llvm::canConstantFoldCallTo(const CallBase*, const Function*)` shape with NVIDIA extensions for NVPTX intrinsics and libdevice naming conventions. A positive result permits the APFloat/APInt folding body to replace the call with a constant.

The dispatcher decomposes into a primary 412-case switch on `Function::IntrinsicID`, a secondary 161-case switch for the `Intrinsic::nvvm_*` block, a sparse high-ID range tree, and a name-walking tail for non-intrinsic libdevice and finite-math aliases.

## 412-case `Intrinsic::ID` switch

The primary switch is indexed by `IntrinsicID ∈ [0, 411]`. Five successor buckets are reached:

| Target | Bucket | Cases | Semantic |
|---|---|---:|---|
| `T_FALSE` | A | 311 | `return false`; intrinsic carries side effects or is not foldable. |
| `T_ATTR` | B | 29 | `return !NoFold && !StrictFP`; floating-point arithmetic gated by attributes. |
| `T_TRUE` | C | 71 | `return true`; pure integer/bit-domain APInt-foldable. |
| `T_LIB` | D | 1 | `Intrinsic::not_intrinsic`; dispatch on `Function::getName()`. |
| `T_DEF` | — | — | default arm; range tree for IDs above the primary table. |

Bucket A (`T_FALSE`, 311 cases) collects the IDs that have observable side effects on memory, the debug-info family, EH/GC/sanitizer support, frame/return-address probes, the entire VP-intrinsic block, and the low-numbered NVPTX intrinsics whose lowering happens during NVPTX ISel pattern matching rather than at constant-fold time. The verbatim union of cases is `2..11, 13, 16..19, 22, 23, 27..62, 68..87, 91..96, 98..101, 110..113, 116..127, 129, 130, 134..139, 141..172, 174, 180, 181, 185..187, 189..208, 213..220, 224..230, 232..237, 241..248, 252, 254..287, 290..311, 318..328, 331, 338, 340, 341, 344..349, 351..358, 360..362, 365, 367, 368, 371, 372, 374, 377..380, 382..387, 391..396, 399..404`.

Bucket B (`T_ATTR`, 29 cases) is the floating-point arithmetic family: `llvm.{sin,cos,exp,exp2,exp10,log,log2,log10,pow,sqrt,fma,minnum,maxnum,copysign,fabs,floor,ceil,trunc,round,roundeven,nearbyint,rint}` and their f16/bf16/f32/f64/fp128/x86_fp80 type-overloaded variants. The folder can evaluate them via the APFloat-emulating tail, but only when the surrounding `Function` carries neither `NoFold` nor `StrictFP`. Cases: `12, 24, 25, 63, 64, 88..90, 176..179, 182, 212, 221..223, 238..240, 249..251, 288, 289, 329, 330, 332, 339`.

Bucket C (`T_TRUE`, 71 cases) is the bit-precise integer arithmetic surface: `llvm.abs`, `umax`/`umin`/`smax`/`smin`, the `vector_reduce_*` family (102..109), the saturating-arith block (209..211), the `bswap`/`ctlz`/`cttz`/`ctpop`/`bitreverse`/`fshl`/`fshr` bitfield block (312..317), and the matrix / masked-{load,store,gather,scatter} family at the upper end (405..411). Cases: `1, 14, 15, 20, 21, 26, 65..67, 97, 102..109, 114, 115, 128, 131..133, 140, 173, 175, 183, 184, 188, 209..211, 231, 253, 312..317, 333..337, 342, 343, 350, 359, 363, 364, 366, 369, 370, 373, 375, 376, 381, 388..390, 397, 398, 405..411`.

Bucket D is the single `case 0` (`Intrinsic::not_intrinsic`) path. Before reaching the name-walking sub-tree it checks that the function only reads memory, re-runs the `NoFold` and `StrictFP` gates, loads `Function::getName()`, and dispatches on the first character. The sum `311 + 29 + 71 + 1 = 412` exhausts every label in the primary table.

## 161-case secondary switch — 8851..9011 (NVPTX block)

When the default arm sees an ID in the NVPTX intrinsic range, it falls into a 161-case secondary switch. This block covers per-shape variants of `cp.async.bulk.tensor.{1..5}d`, `tcgen05.*` alloc/dealloc/commit, `wgmma.fence`, `fence.proxy.*`, `mbarrier.*`, `cluster.*`, `ldmatrix.*`, `stmatrix.*`, and block-scaled MMA dispatcher entries. All 161 IDs are explicitly classified between `T_FALSE` and `T_ATTR`; no NVPTX hardware-effect intrinsic is always foldable.

| ID | Bucket | Class | Notes |
|---|---|---|---|
| 8851 | T_ATTR | TMA-tensor metadata | First case in block; per-shape "no-op" variant |
| 8852 | T_ATTR | TMA prefetch | Foldable to no-op if not `StrictFP`-marked |
| 8853 | T_FALSE | TMA store | Side-effecting on shared/global |
| 8854 | T_ATTR | commit-group head | First of 5-stride boundary family |
| 8855..8916 | T_FALSE | `cp.async.bulk.tensor.*` body | 62-case contiguous block — all SM90+ TMA primitives |
| 8917 | T_ATTR | TMA fence variant | `+5` stride from 8852 |
| 8923 | T_ATTR | `tcgen05.alloc` head | 5th in the 5-step pattern |
| 8931, 8936, 8941, 8946, 8951 | T_ATTR | `tcgen05.commit` / `tcgen05.fence` | One per dimension |
| 8956, 8972, 8978 | T_ATTR | `wgmma.fence.{sync,async,wait}` | Hopper warpgroup-MMA fences |
| 8957..8971 | T_FALSE | `wgmma.mma_async.*` | Side-effecting matrix multiply |
| 8997..9010 | T_FALSE | `mbarrier.arrive.*` / `cluster.*` | Side-effecting sync primitives |
| 9011 | T_ATTR | last case | Final ID in block |

The 23 `T_ATTR` IDs `{8851, 8852, 8854, 8917, 8919, 8923, 8926, 8931, 8936, 8941, 8946, 8951, 8956, 8972, 8974, 8978, 8981, 8986, 8991, 8996, 9001, 9006, 9011}` cluster suspiciously on +5 strides — they correspond to the metadata-only / prefetch / commit-group variants of each TMA-tensor dimension. The remaining 138 IDs go to `T_FALSE`.

## Default-case binary tree for high IDs

When `ID > 9011` the default arm executes a hand-coded binary search over the sparse high-ID space [3184, 15923]. Membership for tight ranges is tested with 64-bit bitmasks rather than nested compares — a classic clang sparse-switch pattern. The decision tree splits at `0x2628` (9768), `0x3AA3` (15011), `0x2628`, `0x255F` (9567), `0x254B` (9547), and `0x21FF` (8703); each leaf is a `goto T_TRUE/T_ATTR/T_FALSE`. The bit-mask leaves are:

| Range base | Selected IDs | Target |
|---|---|---|
| 8740 | 8740..8755, 8770..8786 | `T_ATTR` |
| 9548 | 9548, 9553..9567 | `T_ATTR` |
| 9695 | 9695, 9696, 9697, 9699, 9704, 9708 | `T_ATTR` |
| 9723 | 9723..9726, 9762, 9764, 9766 | `T_ATTR` |
| 9830 | 9830, 9832, 9833, 9839..9842 | `T_ATTR` |
| 15889 | 15889, 15890, 15921, 15922, 15923 | `T_ATTR` |

Isolated `T_TRUE` IDs from the same tree: 1352, 3184, 3260, 3278, 3299, 3422..3424, 3600..3604, 8294 (`cvt.packfloat` head), 9211, and 14542..14543. Isolated `T_ATTR` IDs: 2191, 2192..2196, 2315, 2318..2319, 3312, 8625, 8638..8653, 8698..8699, 8703, 9178, 15006..15011, and 15486..15493. Every other ID outside the enumerated leaves falls through to `T_FALSE`.

## LLVM 17/18 fingerprint analysis

Three independent fingerprints converge on the LLVM 17/18 family. The generic `Intrinsic::ID` space contains exactly 412 entries, which sits between upstream LLVM 17 and 18 counts. The `Function::IntrinsicID` field position rules out older layouts, and the attribute gate uses the slot occupied by `NoFold` and `StrictFP` in the LLVM 17 family. The combined evidence favors an LLVM 17-era generic table with NVIDIA NVPTX additions, though LLVM 18 with selected legacy removals remains close enough that the public documentation should treat this as a 17/18-family implementation detail.

## libdevice suffix name table

The `case 0` tail walks `Function::getName()` byte-by-byte and dispatches into nested switches for generic libm names, Itanium-mangled names, and CUDA-C suffix overloads such as `*d`, `*ff`, and `*dd`.

| String | Class |
|---|---|
| `remainderf` | libdevice helper |
| `powff`, `powdd` | CUDA-C type-suffix helpers |
| `acosd`, `asind`, `atand`, `ceild`, `coshd`, `exp2d`, `fabsd` | double-precision suffix helpers |
| `sinhd`, `sqrtd`, `tanhd`, `floord`, `log10d` | double-precision suffix helpers |
| `__acos_finite`, `__acosf_finite`, `__asin_finite`, `__asinf_finite` | finite-math aliases |
| `__atan2_finite`, `__atan2f_finite`, `__cosh_finite`, `__coshf_finite` | finite-math aliases |
| `__sinh_finite`, `__sinhf_finite` | finite-math aliases |

The suffix names are CUDA-C overload helpers that disambiguate float and double arguments where C++ ABI mangling is unavailable: `f` means float scalar, `d` means double scalar, `ff` means `(float, float)`, and `dd` means `(double, double)`. These symbols are recognition keys; libdevice itself exposes canonical `__nv_*` names. When the walker matches a suffix helper, lowering rewrites the call to the canonical symbol pair, for example `acosd` to `__nv_acos` and `powff` to `__nv_powf`. The `__<name>_finite` entries are GCC/Clang finite-math call targets and fold identically to their non-finite siblings for constant operands.

A separate mini-table holds the Itanium-mangled binary-argument helpers consumed by the constant-fold rewriter:

| String | Demangled |
|---|---|
| `_Z4fmodff` | `fmod(float, float)` |
| `_Z4fmoddd` | `fmod(double, double)` |
| `_Z5atan2ff` | `atan2(float, float)` |
| `_Z5atan2dd` | `atan2(double, double)` |

Together the suffix table, mangled helper table, and finite-math aliases form the NVIDIA extension to LLVM's `TargetLibraryInfo` recognition set.

## Reimplementation Notes

```text
can_constant_fold(call):
    if call.callee.is_intrinsic:
        return classify_intrinsic(call.callee.intrinsic_id, call.function_attrs)

    if not call.callee.only_reads_memory:
        return false
    if call.function_attrs.has("NoFold") or call.function_attrs.has("StrictFP"):
        return false

    return classify_libdevice_name(call.callee.name)
```

Keep the side-effecting NVPTX intrinsics out of the always-foldable bucket. Metadata-only and prefetch-like intrinsics may be attribute-gated, but barriers, async copies, tensor-memory operations, and cluster synchronization must remain non-foldable.
