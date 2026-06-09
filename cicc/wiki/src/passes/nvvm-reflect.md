# NVVMReflect

The NVVMReflect pass resolves calls to `__nvvm_reflect()` — a compile-time introspection mechanism that lets CUDA device code query compilation parameters such as the target GPU architecture, flush-to-zero mode, and precision settings. Each `__nvvm_reflect("__CUDA_ARCH")` call is replaced with an integer constant derived from the target SM version, and each `__nvvm_reflect("__CUDA_FTZ")` is replaced with `0` or `1` depending on the `-ftz` flag. After replacement, the constant result feeds into conditional branches that standard LLVM passes (SimplifyCFG, SCCP, ADCE) can fold away, eliminating dead architecture-specific code paths at compile time. This is NVIDIA's primary mechanism for producing architecture-specialized code from a single portable source: libdevice alone contains hundreds of `__nvvm_reflect` calls that select between FTZ and non-FTZ instruction variants.

The pass is relatively small in code size but architecturally critical — it runs multiple times at different pipeline positions because inlining, loop unrolling, and other transformations continuously expose new `__nvvm_reflect` calls that were previously hidden inside un-inlined function bodies.

## Key Facts

| Property | Value |
|---|---|
| Pass factory | `sub_1857160` |
| Pass level | Function pass (runs per-function) |
| Registration | Legacy PM only (not separately registered in New PM); post-processor `nvvm-reflect-pp` is New PM #381 at line 2237 |
| Runtime positions | Tier 0 #7; Tier 1/2/3 #9, #73 (see [Pipeline](../llvm/pipeline.md)) |
| Pipeline disable flag | NVVMPassOptions offset `+880` |
| Knob | `nvvm-reflect-enable` (boolean, default: `true`) |
| Global knob constructor | `ctor_271` |
| Vtable (likely) | `unk_3C2026C` |
| Post-processing pass | `nvvm-reflect-pp` = `SimplifyConstantConditionalsPass` |
| New PM registration | Not separately registered — NVVMReflect is a legacy-PM pass invoked from the pipeline assembler; `nvvm-reflect-pp` is the New PM companion at registration line 2237 of `sub_2342890` |
| Upstream equivalent | `NVVMReflect` in `llvm/lib/Target/NVPTX/NVVMReflect.cpp` |
| Occurrences in pipeline | ~8 invocations across all paths (see [Multi-Run Pattern](#multi-run-pattern)) |

## Reflect Query Names

The `__nvvm_reflect` mechanism supports a fixed set of query strings. These are embedded as global string constants in NVVM IR (typically from libdevice bitcode) and matched by the pass:

| Query String | Meaning | Value Source |
|---|---|---|
| `__CUDA_ARCH` | Target GPU compute capability | `-arch=compute_XX` flag, encoded as `major*100 + minor*10` |
| `__CUDA_FTZ` | Flush-to-zero mode for single-precision | `-ftz=1` sets to 1; default 0 |
| `__CUDA_PREC_DIV` | Precise division mode | `-prec-div=1` sets to 1; default 0 |
| `__CUDA_PREC_SQRT` | Precise square root mode | `-prec-sqrt=1` sets to 1; default 0 |

### `__CUDA_ARCH` Values

The `__CUDA_ARCH` value is an integer encoding `SM_major * 100 + SM_minor * 10`, propagated from the CLI through the EDG frontend as `-R __CUDA_ARCH=NNN`:

| Architecture | `__CUDA_ARCH` | SM Variants |
|---|---|---|
| Turing | 750 | sm_75 |
| Ampere | 800, 860, 870, 880 | sm_80, sm_86, sm_87, sm_88 |
| Ada Lovelace | 890 | sm_89 |
| Hopper | 900 | sm_90, sm_90a (both share 900) |
| Blackwell | 1000, 1030 | sm_100/100a/100f, sm_103/103a/103f |
| (SM 11.x) | 1100 | sm_110/110a/110f |
| (SM 12.x) | 1200, 1210 | sm_120/120a/120f, sm_121/121a/121f |

Note: Architecture variants with `a` (accelerated) and `f` (forward-compatible) suffixes share the same `__CUDA_ARCH` value as their base. They differ only in `-opt-arch` and `-mcpu` flags, which affect instruction selection and scheduling but not reflect queries.

## Algorithm

The NVVMReflect pass implements a straightforward pattern-matching replacement. In pseudocode:

```c
bool NVVMReflectPass::runOnFunction(Function &F) {
    bool changed = false;
    if (!nvvm_reflect_enable)  // controlled by 'nvvm-reflect-enable' knob
        return false;

    SmallVector<CallInst *, 8> reflect_calls;

    // Phase 1: Collect all __nvvm_reflect call sites
    for (BasicBlock &BB : F) {
        for (Instruction &I : BB) {
            if (auto *CI = dyn_cast<CallInst>(&I)) {
                Function *callee = CI->getCalledFunction();
                if (callee && callee->getName() == "__nvvm_reflect")
                    reflect_calls.push_back(CI);
            }
        }
    }

    // Phase 2: Resolve each call to a constant
    for (CallInst *CI : reflect_calls) {
        // Extract the query string from the first argument.
        // The argument is a pointer to a global constant string:
        //   @.str = private constant [12 x i8] c"__CUDA_ARCH\00"
        // The pass traces through the GEP/bitcast to find the
        // ConstantDataArray initializer.
        StringRef query = extractStringArgument(CI->getArgOperand(0));

        int result = 0;
        if (query == "__CUDA_ARCH")
            result = sm_version;          // e.g., 900 for sm_90
        else if (query == "__CUDA_FTZ")
            result = ftz_enabled ? 1 : 0;
        else if (query == "__CUDA_PREC_DIV")
            result = prec_div ? 1 : 0;
        else if (query == "__CUDA_PREC_SQRT")
            result = prec_sqrt ? 1 : 0;
        else
            result = 0;  // unknown query => 0

        // Replace the call with the constant integer
        CI->replaceAllUsesWith(ConstantInt::get(CI->getType(), result));
        CI->eraseFromParent();
        changed = true;
    }
    return changed;
}
```

The string extraction logic must handle the IR pattern produced by the CUDA frontend and libdevice linking:

```llvm
@.str = private unnamed_addr constant [12 x i8] c"__CUDA_ARCH\00", align 1

%1 = call i32 @__nvvm_reflect(ptr @.str)
```

The pass walks through the argument operand, stripping `ConstantExpr` GEPs and bitcasts, to reach the `ConstantDataArray` containing the query string. If the argument is not a resolvable constant string, the call is left unmodified (this is a no-op safety — in practice, all reflect calls use literal string arguments).

## Interaction with Constant Propagation and Dead Code Elimination

The reflect replacement produces a constant integer that feeds directly into an `icmp` and conditional branch. This is the canonical pattern in libdevice:

**Before NVVMReflect** (from `libdevice.10.ll`, function `__nv_floorf`):

```llvm
define float @__nv_floorf(float %f) {
  %1 = call i32 @__nvvm_reflect(ptr @.str)   ; @.str = "__CUDA_FTZ"
  %2 = icmp ne i32 %1, 0
  br i1 %2, label %ftz_path, label %precise_path

ftz_path:
  %3 = call float @llvm.nvvm.floor.ftz.f(float %f)
  br label %merge

precise_path:
  %4 = call float @llvm.nvvm.floor.f(float %f)
  br label %merge

merge:
  %.0 = phi float [ %3, %ftz_path ], [ %4, %precise_path ]
  ret float %.0
}
```

**After NVVMReflect** (with `-ftz=1`):

```llvm
define float @__nv_floorf(float %f) {
  %2 = icmp ne i32 1, 0            ; constant 1 replaces the call
  br i1 %2, label %ftz_path, label %precise_path

ftz_path:
  %3 = call float @llvm.nvvm.floor.ftz.f(float %f)
  br label %merge

precise_path:                       ; now unreachable
  %4 = call float @llvm.nvvm.floor.f(float %f)
  br label %merge

merge:
  %.0 = phi float [ %3, %ftz_path ], [ %4, %precise_path ]
  ret float %.0
}
```

**After SimplifyCFG / SCCP / ADCE** (subsequent passes):

```llvm
define float @__nv_floorf(float %f) {
  %1 = call float @llvm.nvvm.floor.ftz.f(float %f)
  ret float %1
}
```

The `icmp ne i32 1, 0` folds to `true`, SimplifyCFG eliminates the dead branch, and ADCE removes the unused `llvm.nvvm.floor.f` call. The function collapses from 4 basic blocks to 1.

This pattern repeats for every libdevice math function: `__nv_fabsf`, `__nv_fminf`, `__nv_fmaxf`, `__nv_rsqrtf`, `__nv_exp2f`, and dozens more all contain the same `__nvvm_reflect("__CUDA_FTZ")` branch. After reflect resolution, each function specializes to either FTZ or precise mode.

### `__CUDA_ARCH` branching pattern

For architecture-dependent code, the pattern uses inequality comparisons:

```llvm
%arch = call i32 @__nvvm_reflect(ptr @.str.1)  ; "__CUDA_ARCH"
%is_sm80_plus = icmp sge i32 %arch, 800
br i1 %is_sm80_plus, label %sm80_path, label %legacy_path

sm80_path:
  ; use SM 8.0+ specific intrinsics (e.g., async copy, cp.async)
  ...

legacy_path:
  ; fallback path for older architectures
  ...
```

After NVVMReflect replaces `%arch` with (e.g.) `900` for Hopper, the comparison `icmp sge i32 900, 800` folds to `true`, and the legacy path is eliminated.

## Multi-Run Pattern

NVVMReflect (`sub_1857160`) is invoked multiple times across the pipeline because optimization passes continuously expose new reflect calls. The key insight is that `__nvvm_reflect` calls originate primarily from **libdevice** functions, which are linked as bitcode and initially exist as un-inlined function calls. Each inlining pass expands these functions inline, exposing their internal `__nvvm_reflect` calls to the containing function.

### Tier 0 Pipeline (Full Optimization via `sub_12DE330`)

In the Tier 0 (O1/O2/O3) full optimization pipeline, NVVMReflect appears once:

| Position | Factory | Context |
|---|---|---|
| #7 | `sub_1857160()` | After CGSCC inliner (#2), GVN (#5-6). Catches reflect calls exposed by first-round inlining |

### "mid" Path Pipeline (Ofcmid/Ofcmin via `sub_12E54A0` PATH B)

In the "mid" fast-compile path, NVVMReflect appears at **three** distinct positions:

| Position | Factory | Guard | Context |
|---|---|---|---|
| After CGSCC pipeline #8 | `sub_1857160()` | `!opts[880]` | After aggressive CGSCC inlining (8 iterations). Catches reflect calls from freshly inlined libdevice bodies |
| After Sinking2 + EarlyCSE | `sub_1857160()` | `!opts[880]` | After loop transformations and code motion. Catches reflect calls in loop bodies after unrolling |
| (appears once more in late position) | `sub_1857160()` | `!opts[880]` | Final cleanup after late CGSCC pass and NVVMIntrinsicLowering |

### Default/General Path Pipeline (PATH C)

In the default path (external bitcode input), NVVMReflect appears at **three** positions:

| Position | Factory | Context |
|---|---|---|
| After CGSCC pipeline #4 | `sub_1857160()` | First resolution after initial inlining |
| After NVVMIntrinsicLowering | `sub_1857160()` | Intrinsic lowering may expose new reflect patterns |
| After LoopUnroll + InstCombine | `sub_1857160()` | Loop unrolling duplicates loop bodies containing reflect calls |

### Tiered Pipeline Insertions (`sub_12DE8F0`)

Within the tiered sub-pipeline, NVVMReflect appears with additional gating:

| Tier | Guard | Position |
|---|---|---|
| 1, 2, 3 | `opts[3200] && !opts[880]` | Mid-tier, after NVVMVerifier and IPConstPropagation |
| 3 only | `opts[3200] && tier==3 && !opts[880]` | Late-tier, after ADCE and LoopOpt/BarrierOpt. This extra run at O3 catches reflect calls exposed by the most aggressive transformations |

### Why Multiple Runs Are Necessary

Consider this scenario:

1. User code calls `__nv_sinf(x)` (a libdevice function).
2. Initially, `__nv_sinf` is an external function call — its body contains `__nvvm_reflect("__CUDA_FTZ")` but the reflect call is not visible to the optimizer.
3. **First NVVMReflect run**: No-op for this function (the reflect is inside `__nv_sinf`'s body, which has not been inlined yet).
4. **CGSCC Inliner runs**: Inlines `__nv_sinf` into the caller, expanding its body with the `__nvvm_reflect` call.
5. **Second NVVMReflect run**: Now sees the freshly-inlined `__nvvm_reflect` call and resolves it to a constant.
6. **Loop Unrolling runs**: If the `__nv_sinf` call was inside a loop, unrolling duplicates the call site. If the loop body was too complex to inline before unrolling simplified it, a third inlining opportunity may arise.
7. **Third NVVMReflect run**: Resolves any remaining reflect calls exposed by unrolling + re-inlining.

Without multiple runs, libdevice functions inlined late in the pipeline would retain their reflect-based branching, defeating the specialization mechanism and leaving dead code paths in the final binary.

## The `nvvm-reflect-pp` Post-Processing Pass

After NVVMReflect replaces calls with constants, the resulting IR contains trivially-foldable comparisons and dead branches. While standard LLVM passes (SimplifyCFG, ADCE) handle most of this, NVIDIA registers a dedicated post-processing pass under the misleading name `nvvm-reflect-pp`.

Despite its name, **`nvvm-reflect-pp` is `SimplifyConstantConditionalsPass`** (class `llvm::SimplifyConstantConditionalsPass`), not a reflection pass. It is a targeted dead-branch elimination pass that:

1. Finds conditional branches where the condition is a constant (`icmp` with both operands constant).
2. Replaces the branch with an unconditional branch to the taken target.
3. Marks the not-taken successor as potentially unreachable.
4. Cleans up resulting dead phi nodes and empty blocks.

This pass is registered in the New PM at `sub_2342890` line 2237 as a function-level pass. It runs immediately after NVVMReflect in some pipeline configurations to ensure that reflected constants are cleaned up before subsequent optimization passes see the IR.

## Configuration

| Knob | Type | Default | Effect |
|---|---|---|---|
| `nvvm-reflect-enable` | `bool` | `true` | Master enable for NVVMReflect. When `false`, all `__nvvm_reflect` calls are left unresolved (they default to 0 at link time, selecting the non-FTZ/non-precise/lowest-arch path). |

### Pipeline Disable Flag

NVVMPassOptions offset `+880` is the per-compilation disable flag for NVVMReflect. When set (e.g., by an internal debugging mechanism), all pipeline insertion points skip the pass via the `!opts[880]` guard. This flag is distinct from the `nvvm-reflect-enable` knob: the knob controls the pass's internal behavior, while the pipeline flag prevents the pass from being added to the pipeline at all.

### Reflect Value Propagation Path

The reflect query values flow from the CLI through three layers:

1. **CLI**: `-arch=compute_90` is parsed by `sub_95EB40` / `sub_12C8DD0`
2. **EDG frontend**: Receives `-R __CUDA_ARCH=900` and defines the preprocessor macro
3. **Optimizer**: Receives `-opt-arch=sm_90`. The NVVMReflect pass reads the SM version from the target machine configuration (not from `-R` flags — those are for the preprocessor)

For FTZ/precision flags, the path is:
1. `-ftz=1` maps to `-R __CUDA_FTZ=1` (EDG) and `-nvptx-f32ftz` (optimizer/backend)
2. The NVVMReflect pass reads the FTZ setting from the NVPTX subtarget or a global variable set during pipeline configuration

## Differences from Upstream LLVM

Upstream LLVM's `NVVMReflect` pass (in `llvm/lib/Target/NVPTX/NVVMReflect.cpp`) is functionally similar but differs in several respects in CICC v13.0:

| Aspect | Upstream LLVM | CICC v13.0 |
|---|---|---|
| Pipeline placement | Runs once, typically early | Runs ~8 times at strategic positions throughout the pipeline |
| Post-processing | Relies on standard SimplifyCFG | Has dedicated `nvvm-reflect-pp` (`SimplifyConstantConditionalsPass`) |
| Pipeline integration | New PM function pass | Legacy PM function pass invoked from the pipeline assembler (`sub_12E54A0`), with the pipeline disable flag at `NVVMPassOptions[880]` |
| Tier 3 extra run | Not applicable | Extra late-pipeline run gated by `tier==3` for O3-only cleanup |
| Query string set | `__CUDA_ARCH`, `__CUDA_FTZ` | Same set plus `__CUDA_PREC_DIV`, `__CUDA_PREC_SQRT` |

The multi-run strategy is the most significant difference. Upstream LLVM assumes that NVVMReflect runs once before optimization, resolving all reflect calls in the linked libdevice bitcode. CICC's pipeline accounts for the reality that aggressive inlining and loop transformations in a GPU-focused compiler expose reflect calls at many different pipeline stages.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| NVVMReflect pass factory | `sub_1857160` | — | Creates and returns a new NVVMReflect pass instance |
| NVVMReflect constructor knob | `ctor_271` | — | Registers `nvvm-reflect-enable` cl::opt |
| SimplifyConstantConditionalsPass (nvvm-reflect-pp) | registered at line 2237 of `sub_2342890` | — | Post-reflect dead branch cleanup |
| Pipeline assembler | `sub_12E54A0` | — | Inserts NVVMReflect at multiple positions |
| Tier 0 pipeline builder | `sub_12DE330` | — | Inserts NVVMReflect as pass #7 |
| Tiered sub-pipeline | `sub_12DE8F0` | — | Inserts NVVMReflect at tier-gated positions |
| Architecture detection table | `sub_95EB40` | — | Maps `-arch=compute_XX` to `__CUDA_ARCH` values |
| Architecture detection (libnvvm) | `sub_12C8DD0` | — | Parallel mapping table for the libnvvm path |

## Test This

The following kernel calls a libdevice math function whose implementation branches on `__CUDA_FTZ` and `__CUDA_ARCH`. Compile for two configurations and compare the PTX to see NVVMReflect in action.

```cuda
#include <math.h>

__global__ void reflect_test(float* out, const float* in, int n) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid < n) {
        out[tid] = sinf(in[tid]);
    }
}
```

Compile twice:
```bash
nvcc -ptx -arch=sm_90 -ftz=true  reflect_test.cu -o reflect_ftz.ptx
nvcc -ptx -arch=sm_90 -ftz=false reflect_test.cu -o reflect_noftz.ptx
```

**What to look for in PTX:**
- With `-ftz=true`: the PTX should contain flush-to-zero math instructions (e.g., `sin.approx.ftz.f32`). The NVVMReflect pass resolved `__nvvm_reflect("__CUDA_FTZ")` to `1`, SimplifyCFG folded the branch, and only the FTZ code path survived.
- With `-ftz=false`: the PTX should contain precise math instructions without the `.ftz` suffix. The reflect resolved to `0`, selecting the non-FTZ path.
- The key evidence is that the PTX contains only **one** code path — no conditional branch choosing between FTZ and non-FTZ variants. If both paths survive, NVVMReflect or its downstream cleanup passes failed.
- Comparing `-arch=sm_75` vs. `-arch=sm_90` exercises the `__CUDA_ARCH` reflect. Functions like `__nv_dsqrt_rn` use architecture comparisons (`icmp sge i32 %arch, 800`) to select between SM 8.0+ instruction sequences and legacy fallbacks.

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building an equivalent compile-time reflection mechanism.

**1. Returning the wrong `__CUDA_ARCH` encoding.** The `__CUDA_ARCH` value is `major * 100 + minor * 10`, not `major * 10 + minor`. For SM 9.0, the correct value is 900, not 90. For SM 10.0, the correct value is 1000, not 100. A reimplementation that uses the wrong encoding will select the wrong code paths in libdevice, potentially enabling instructions not supported by the target architecture (e.g., SM 7.0 paths on an SM 9.0 target) or disabling instructions that should be available. This encoding is also used by the CUDA preprocessor (`__CUDA_ARCH__`), so consistency between the frontend macro and the reflect value is critical.

**2. Running NVVMReflect only once in the pipeline.** The pass must run multiple times (approximately 8 invocations across the full pipeline) because `__nvvm_reflect` calls are hidden inside un-inlined libdevice function bodies. The first run resolves calls visible at the top level, but each subsequent inlining pass exposes new reflect calls from freshly inlined libdevice functions. A reimplementation with a single early invocation will leave reflected branches unresolved in all functions inlined after that point, resulting in both FTZ and non-FTZ code paths surviving to the final binary — doubling code size and defeating the entire specialization mechanism.

**3. Not running `SimplifyConstantConditionalsPass` (nvvm-reflect-pp) after reflect resolution.** After NVVMReflect replaces `__nvvm_reflect("__CUDA_FTZ")` with the constant `1`, the IR contains `icmp ne i32 1, 0` feeding a conditional branch. If no pass simplifies this to an unconditional branch, the dead code path survives through the rest of the pipeline, consuming compile time in every subsequent pass and inflating the final binary. While standard LLVM SimplifyCFG will eventually handle it, the dedicated `nvvm-reflect-pp` pass provides immediate cleanup at the point where it matters most.

**4. Returning 0 for unknown query strings instead of propagating a diagnostic.** The pass returns 0 for any unrecognized `__nvvm_reflect` query string. This is the correct behavior (documented default), but a reimplementation that raises an error or leaves the call unresolved will break forward compatibility: future CUDA toolkit versions may introduce new query strings that libdevice checks. The value 0 is the safe default because libdevice code always treats 0 as "feature not available" and falls back to the conservative code path.

**5. Reading the SM version from the wrong source.** The reflect query values flow through three layers: CLI (`-arch=compute_90`), EDG frontend (`-R __CUDA_ARCH=900`), and optimizer (`-opt-arch=sm_90`). The NVVMReflect pass must read the SM version from the target machine configuration (the optimizer-level value), not from the `-R` preprocessor flags. A reimplementation that reads from the wrong layer may get a stale or mismatched value, especially in LTO scenarios where the preprocessor flags were consumed during an earlier compilation phase.

## Cross-References

- [Optimizer Pipeline](../pipeline/optimizer.md) — NVVMReflect pipeline positions and the NVVMPassOptions system
- [NVIDIA Custom Passes](index.md) — registry of all NVIDIA-proprietary passes
- [NVVM Intrinsic Constant-Fold Eligibility (K02)](../pipeline/optimizer.md) — `sub_14D90D0`, the companion pass that checks whether an intrinsic can be constant-folded (NVVMReflect calls are resolved *before* K02 runs)
- [Architecture Detection](../pipeline/optimizer.md) — the `sub_95EB40` table that maps CLI flags to `__CUDA_ARCH` values
- [Optimization Levels](../pipeline/optimizer.md) — how NVVMReflect placement varies across O0/O1/O2/O3 and fast-compile tiers
