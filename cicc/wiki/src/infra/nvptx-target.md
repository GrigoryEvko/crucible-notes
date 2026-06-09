# NVPTX Target Infrastructure

The NVPTXTargetMachine, NVPTXSubtarget, and NVPTXTargetTransformInfo form the target description layer that the entire LLVM backend consults for every decision from type legality through instruction cost to vectorization factor selection. In upstream LLVM, these are three separate source files totaling roughly 1,500 lines; in cicc v13.0 they are spread across the 0xDF0000-0xE00000 address range (TTI hooks), the 0x330-0x35B range (NVPTXTargetLowering), the type legalization tables embedded in NVPTXSubtarget, and the pipeline assembler at 0x12EA000-0x12F0000 (TargetMachine construction). The NVIDIA delta relative to upstream is moderate — the TTI hooks return GPU-specific constants rather than CPU ones, the SubtargetFeatures carry NVIDIA-proprietary math precision flags, and the TargetMachine creation path has a dual-path design that handles both the cicc standalone pipeline and the LibNVVM API pipeline.

## Key Facts

| Property | Value |
|---|---|
| SM processor table | `qword_502A920` (45 entries, stride-2, `ctor_605` at `0x584510`) |
| Target lookup | `sub_12EA530` (1 KB native, calls `sub_16D3AC0` = `TargetRegistry::lookupTarget`) |
| TargetMachine creation | `sub_12F4060` (4 KB native, NVIDIA options) / `sub_12E54A0` (10 KB native, pipeline path) |
| TTI wrapper pass | `sub_1BFB520` (208-byte alloc, wraps `sub_1BFB9A0`) |
| Register bit width (Vector) | `sub_DFE640` — returns 32 (fixed) |
| Scalable vectors | `sub_DFE610` — returns `false` |
| Max interleave factor | `sub_DFB120` (at TTI+448), `sub_DFB730` (vectorized variant) |
| SubtargetFeatures | Offsets +2498, +2584, +2843, +2870, +2871 |
| Target triples | `nvptx64-nvidia-cuda`, `nvptx-nvidia-cuda`, `nvsass-nvidia-*` (6 total) |

## NVPTXTargetMachine

### Dual-Path Target Initialization

cicc constructs the TargetMachine through two independent code paths depending on whether compilation enters through the standalone cicc CLI or through the LibNVVM API. Both converge on `TargetRegistry::lookupTarget` (`sub_16D3AC0`) but assemble the target triple, feature string, and TargetOptions differently.

**Path 1 — cicc standalone** (`sub_12F7D90` -> `sub_12F4060`):

```c
sub_12F7D90 — CLI parser:
    parse "-arch=compute_XX" → SM version (multiplied by 10)
    parse "-opt=N"           → optimization level
    parse "-ftz=N"           → flush-to-zero mode
    parse "-fma=N"           → FMA contraction level
    parse "-prec-div=N"      → float division precision
    parse "-prec-sqrt=N"     → sqrt precision
    parse "--device-c"       → device compilation flag

sub_12F4060 — TargetMachine creation (4 KB native):
    triple = (pointerWidth == 64) ? "nvptx64" : "nvptx"
    features = ""
    if (sharedmem32bit):
        features += "+sharedmem32bitptr"
    features += ",+fma-level=N,+prec-divf32=N,+prec-sqrtf32=N"

    opts = TargetOptions {
        flags: 0,
        reloc: PIC (1),
        codeModel: 8,
        optLevel: from_cli,
        threadModel: 1
    }

    TM = TargetRegistry::lookupTarget(triple, cpu_string)
    if (!TM):
        error "Error: Cannot specify multiple -llcO#\n"
    return TM->createTargetMachine(triple, cpu, features, opts)
```

**Path 2 — pipeline assembler** (`sub_12E54A0`):

The master pipeline assembly function (50KB, called from both Phase I and Phase II) constructs the target independently:

```c
sub_12E54A0:
    ptrSize = Module::getDataLayout().getPointerSizeInBits(0)
    if (8 * ptrSize == 64):
        triple = "nvptx64"                          // 7 chars
    else:
        triple = "nvptx"                            // 5 chars

    target = sub_16D3AC0(&triple, &cpu_string)      // TargetRegistry::lookupTarget
    if (!target):
        error "Failed to locate nvptx target\n"     // sub_1C3EFD0

    // TargetOptions setup:
    opts[0] = 0                                     // no flags
    opts[1] = 1                                     // PIC relocation
    opts[2] = 8                                     // code model
    opts[3] = 1                                     // opt level indicator
    opts[4] = 1                                     // thread model
    opts[5] = 0                                     // reserved

    sub_167F890(subtargetInfo)                       // initialize SubtargetInfo
    TLI = sub_14A04B0(targetLibInfo, moduleName)     // TargetLibraryInfo
    sub_149CBC0(TLI)                                 // finalize TLI
    TTI = sub_1BFB9A0(DataLayout, a2, a3, v269)     // TargetTransformInfo

    optLevel = read qword_4FBB430                    // cl::opt<int> value
    PassManagerBuilder = sub_1611EE0(PM)
```

The pipeline assembler path also checks for an extension hook: if the target has a `createExtendedTargetMachine` vtable entry at offset +88, it calls that instead, enabling custom target backends. The returned TargetMachine pointer feeds into the 150+ pass registrations that follow.

### TargetOptions

The `TargetOptions` struct passed to both paths uses LLVM's standard layout. The key NVIDIA-specific values:

| Field | Value | Meaning |
|-------|-------|---------|
| Relocation model | 1 (PIC) | Position-independent code, always |
| Code model | 8 | Large code model (matches PTX's flat addressing) |
| Thread model | 1 | POSIX-style threading assumed |
| Optimization level | From CLI | Stored in `qword_4FBB430`, default from `qword_4FBB430[2]` |

### NVIDIA-Specific Target Features

The feature string passed to `createTargetMachine` encodes math precision and shared memory configuration as subtarget features. These are not upstream LLVM features — they are NVIDIA extensions:

| Feature | CLI Source | Subtarget Effect |
|---------|-----------|------------------|
| `+sharedmem32bitptr` | `nvptx-short-ptr` / `nvptx-32-bit-smem` | Enables 32-bit pointers for address space 3 (shared memory); adds `p3:32:32:32` to data layout |
| `+fma-level=N` | `-fma=N` | 0=off, 1=on, 2=aggressive FMA contraction |
| `+prec-divf32=N` | `-prec-div=N` | 0=approx, 1=full, 2=IEEE+ftz, 3=IEEE compliant |
| `+prec-sqrtf32=N` | `-prec-sqrt=N` | 0=approx (`rsqrt.approx`), 1=rn (`sqrt.rn`) |

Registered in `ctor_607` (`0x584B60`, 14KB):

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `nvptx-sched4reg` | bool | — | Schedule for register pressure |
| `nvptx-fma-level` | int | — | FMA contraction level |
| `nvptx-prec-divf32` | int | — | F32 division precision |
| `nvptx-prec-sqrtf32` | int | — | Sqrt precision |
| `nvptx-approx-log2f32` | bool | — | Use `lg2.approx` for log2 |
| `nvptx-force-min-byval-param-align` | bool | — | Force 4-byte byval alignment |
| `nvptx-normalize-select` | bool | — | Override `shouldNormalizeToSelectSequence` |
| `enable-bfi64` | bool | — | Enable 64-bit BFI instructions |

## NVPTXSubtarget Feature Flags

The NVPTXSubtarget object carries the type legalization tables and architecture-specific feature flags that the SelectionDAG, register allocator, and type legalizer consult at every step. These are populated during target construction and indexed by the SM processor table.

### Feature Flag Offsets

| Offset | Size | Purpose | Stride |
|--------|------|---------|--------|
| +120 | ptr | Register class array (8-byte stride entries) | — |
| +2498 | 259 | Type legality flags (indexed per MVT) | 259 bytes per type action |
| +2584 | 259 | Float legality flags (indexed per MVT) | 259 bytes per type action |
| +2843 | 1 | Integer type support flag | — |
| +2870 | 1 | Branch distance flag | — |
| +2871 | 1 | Jump table eligibility flag | — |

The type legality arrays at +2498 and +2584 are the backbone of SelectionDAG's `getTypeAction()` and `isTypeLegal()` queries. Each entry covers one MVT (Machine Value Type) and stores the action: Legal, Promote, Expand, Scalarize, or SplitVector. For NVPTX, i32 and f32 are always Legal; i64 and f64 are Legal on all supported SM versions but with expanded arithmetic costs; vectors wider than 128 bits are always Split or Scalarized.

The function `sub_201BB90` reads these offsets during type legalization to determine expansion strategy. The branch distance flags at +2870/+2871 control `sub_20650A0`, which decides jump table eligibility beyond the standard `no-jump-tables` flag.

### Initialization Flow

The SubtargetFeatures initialization follows this path:

1. `ctor_605` (`0x584510`, 2.6KB) populates `qword_502A920` with the 45-entry SM processor table at static init time.
2. `sub_167F890` initializes the SubtargetInfo during pipeline setup.
3. `sub_982C80` initializes the 224-byte NVPTX feature flag table based on SM version and OS/ABI info.
4. `sub_97DEE0` performs initial population of the feature bitfield.
5. `sub_982B20` applies SM-version-specific refinements from the global table at `qword_4F7FCC8`.

The 224-byte feature table (`sub_982C80`) initializes bytes 0-127 to all-1s (0xFF), then selectively clears bits based on the target configuration. This "default-enabled, selectively-disabled" pattern means that features are assumed present unless explicitly turned off for a given target.

## NVPTXTargetTransformInfo Hook Table

The TTI is the interface through which all LLVM optimization passes query target-specific costs and capabilities. For NVPTX, every hook returns a value calibrated for a scalar-register GPU architecture rather than a SIMD-register CPU.

| TTI Hook | Address | Return Value | Upstream Equivalent |
|----------|---------|-------------|---------------------|
| `getRegisterBitWidth(Vector)` | `sub_DFE640` | `TypeSize::getFixed(32)` | AVX2 returns 256, AVX-512 returns 512 |
| `supportsScalableVectors()` | `sub_DFE610` | `false` | AArch64 SVE returns `true` |
| `getMaxInterleaveFactor()` | `sub_DFB120` | Register-pressure-bounded | CPU returns 2-4 based on uarch |
| `getMaxInterleaveFactor(vectorized)` | `sub_DFB730` | Separate limit for vectorized loops | — |
| `getRegisterBitWidth(Scalar)` | `sub_DFB1B0` | 32 | Matches PTX 32-bit register file |
| `getInstructionCost()` | `sub_20E14F0` (6.4 KB native) | Per-opcode latency from sched model | — |
| `hasAttribute(30)` | `sub_B2D610` | Checks `noimplicitfloat` | Standard LLVM |
| `hasAttribute(47)` | `sub_B2D610` | Checks `alwaysvectorize` | Standard LLVM |
| `hasAttribute(18)` | `sub_B2D610` | Checks `optnone` | Standard LLVM |

### Impact on Loop Vectorization

The 32-bit register width return from `sub_DFE640` is the single most consequential TTI hook for GPU compilation. The standard LLVM VF formula is:

```text
VF = registerBitWidth / elementBitWidth
```

With `registerBitWidth = 32`:
- `float` (32-bit): VF = 1 — no vectorization from the register-width formula alone
- `half` (16-bit): VF = 2
- `i8` (8-bit): VF = 4

This means that profitable vectorization of 32-bit types (the dominant case in CUDA) must come entirely from the cost model determining that `ld.v2.f32` or `ld.v4.f32` is cheaper than multiple scalar loads, not from the register-width heuristic. The LoopVectorize pass (`sub_2AF1970`) has an explicit override: when the VF formula produces VF <= 1 and the `byte_500D208` knob is set, it forces VF = 4 for outer loops.

### Impact on SLP Vectorization

The SLP vectorizer (`sub_2BD1C50`) receives the target vector register width as parameter `a3` and uses it to determine maximum bundle width. With 32 bits, SLP bundles are limited to:
- 2x i16 (32 bits total)
- 4x i8 (32 bits total)
- 1x i32 or f32 (degenerate — no SLP benefit)

In practice, the SLP vectorizer's profitability model can override this limit when paired loads/stores demonstrate memory coalescing benefit, but the register width serves as the initial upper bound.

### Impact on Interleave Count

The `getMaxInterleaveFactor` hook (`sub_DFB120`, queried at `TTI+448`) caps the interleave count (IC) for loop unroll-and-jam. The interleave selection algorithm in `sub_2AED330` reads this value and combines it with scheduling info at `TTI+56`:

```c
maxIC    = TTI.getMaxInterleaveFactor(VF)
issueWidth = *(TTI + 56 + 32)              // scheduling model: issue width
latency    = *(TTI + 56 + 36)              // scheduling model: latency
IC         = IC / max(issueWidth, latency)  // cap by pipeline throughput
```

This models the SM's instruction issue pipeline: even if register pressure allows IC=8, the warp scheduler may saturate at lower IC values, making additional interleaving waste register budget without throughput gain.

### Arithmetic Cost for i64

NVPTX GPUs have 32-bit ALUs. All 64-bit integer arithmetic is emulated through pairs of 32-bit operations with carry propagation. The TTI `getArithmeticInstrCost` hook reflects this by returning approximately 2x the base cost for i64 operations:

| Operation | i32 Cost | i64 Cost | Ratio |
|-----------|----------|----------|-------|
| ADD/SUB | 1 | 2 | 2x (add.cc + addc) |
| MUL | 1 | ~4 | 4x (mul.lo + mul.hi + add chain) |
| DIV/REM | high | very high | Library call on both |
| Shift | 1 | 2-3 | funnel shift pair |

This cost differential causes LLVM optimization passes (InstCombine, SCEV-based transformations, IV widening) to prefer i32 operations, which NVIDIA's custom IV Demotion pass (`sub_18B1DE0`) further exploits by narrowing 64-bit induction variables to 32-bit where the trip count permits.

## SM Processor Table

The processor table at `qword_502A920` is a flat array of 90 entries (45 SM variants x 2 fields per entry) with stride-2 layout: even indices hold the SM name string pointer, odd indices hold the PTX version code.

Populated by `ctor_605` at `0x584510` (2.6KB), called during static initialization before `main`. The table is read-only after construction.

```c
qword_502A920[2*i + 0] = const char* sm_name    // e.g., "sm_100"
qword_502A920[2*i + 1] = uint64_t   ptx_version // 5, 6, or 7
```

### PTX Version Codes

| Code | Meaning | SM Range |
|------|---------|----------|
| 5 | Legacy PTX | sm_20 through sm_90 (all base variants) |
| 6 | Modern PTX | sm_90a, sm_100-sm_121 (base variants only) |
| 7 | Extended PTX | sm_100a/f through sm_121a/f (accelerated/forward-compatible) |

Notable observations:
- `sm_90a` is the only pre-Blackwell SM with PTX version 6.
- The `f` (forward-compatible) suffix uses the same PTX version as `a` (accelerated).
- No entries exist for sm_84, sm_85 (Ada Lovelace numbering gap).
- `sm_73` (Volta sub-variant) and `sm_88` (Ada sub-variant) are present but not publicly documented.
- The table contains 15 legacy architectures (sm_20 through sm_75) that are no longer accessible through the CLI mapping but remain in the backend's processor table.

## Data Layout String

The NVPTX data layout string follows LLVM's standard format with three variants selected based on pointer width and shared memory pointer mode:

### 64-bit with shared memory specialization (most common)

```text
e-p:64:64:64-p3:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f16:16:16-f32:32:32-f64:64:64-v16:16:16-v32:32:32-n16:32:64
```

### 64-bit without shared memory specialization

```text
e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f16:16:16-f32:32:32-f64:64:64-v16:16:16-v32:32:32-n16:32:64
```

### 32-bit mode

```text
e-p:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f16:16:16-f32:32:32-f64:64:64-v16:16:16-v32:32:32-n16:32:64
```

### Key fields

| Field | Meaning | NVIDIA Note |
|-------|---------|-------------|
| `e` | Little-endian | All NVIDIA GPUs |
| `p:64:64:64` | Generic pointers: 64-bit, 64-bit aligned | Default for 64-bit compilation |
| `p3:32:32:32` | Address space 3 (shared memory): 32-bit pointers | Controlled by `nvptx-short-ptr` / `nvptx-32-bit-smem` / `unk_4D0461C` |
| `n16:32:64` | Native integer widths: 16, 32, 64 | Tells LLVM that i16/i32/i64 are all hardware-supported |
| `v16:16:16` / `v32:32:32` | Vector alignment: natural | 16-bit and 32-bit vectors aligned to their width |

The `p3:32:32:32` entry is the NVIDIA delta: shared memory lives in a 48KB-228KB on-chip SRAM per SM, addressable with 32-bit pointers even in 64-bit mode. Using 32-bit pointers for shared memory saves register pressure and instruction count for every shared memory access.

A separate data layout string `e-i64:64-v16:16-v32:32-n16:32:64` appears in the IR linker (`sub_106AB30`) as a compatibility check during module linking. This shortened form is used to validate that two modules being linked share the same NVPTX target data layout.

Data layout validation is performed at multiple points:
- `sub_2C74F70` in the NVVM verifier checks the layout string on every module
- If empty: `"Empty target data layout, must exist"`
- If invalid: prints `"Example valid data layout:"` with reference 32-bit and 64-bit strings from `off_4C5D0A0` / `off_4C5D0A8`

## Target Triple Construction

The target triple is constructed at module creation time by checking the pointer width:

```c
if (unk_4F06A68 == 8)                    // 64-bit data model
    triple = "nvptx64-nvidia-cuda"       // 19 chars
else
    triple = "nvptx-nvidia-cuda"         // 17 chars
```

Eight triples are valid in UnifiedNVVMIR mode:

| Triple | Width | Runtime |
|--------|-------|---------|
| `nvptx-nvidia-cuda` | 32-bit | CUDA |
| `nvptx64-nvidia-cuda` | 64-bit | CUDA |
| `nvptx-nvidia-nvcl` | 32-bit | OpenCL |
| `nvptx64-nvidia-nvcl` | 64-bit | OpenCL |
| `nvsass-nvidia-cuda` | SASS | CUDA native assembly |
| `nvsass-nvidia-nvcl` | SASS | OpenCL native assembly |
| `nvsass-nvidia-directx` | SASS | DirectX backend |
| `nvsass-nvidia-spirv` | SASS | SPIR-V backend |

In non-UnifiedNVVMIR mode, validation is looser: the triple must start with `nvptx-` or `nvptx64-` and contain `-cuda`. The `nvsass-nvidia-directx` and `nvsass-nvidia-spirv` triples (discovered in `sub_2C80C90`) are notable evidence that NVIDIA's SASS-level backend supports DirectX and SPIR-V shader compilation alongside traditional CUDA/OpenCL.

## Configuration Knobs

### Backend Options (ctor_609_0, 0x585D30, 37KB)

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `nvptx-short-ptr` | bool | — | 32-bit pointers for const/local/shared |
| `nvptx-32-bit-smem` | bool | — | 32-bit shared memory pointers |
| `nvptx-enable-machine-sink` | bool | — | Enable Machine Sinking |
| `enable-new-nvvm-remat` | bool | true | Enable new rematerialization |
| `nv-disable-remat` | bool | false | Disable all remat passes |
| `nv-disable-mem2reg` | bool | false | Disable MI Mem2Reg pass |
| `nv-disable-scev-cgp` | bool | false | Disable SCEV address mode opt |
| `disable-nvptx-load-store-vectorizer` | bool | false | Disable load/store vectorizer |
| `disable-nvptx-require-structured-cfg` | bool | false | Turn off structured CFG requirement |
| `nvptx-exit-on-unreachable` | bool | true | Lower unreachable as exit |
| `nvptx-early-byval-copy` | bool | — | Copy byval args early |
| `enable-nvvm-peephole` | bool | true | Enable NVVM Peephole Optimizer |
| `lower-func-args` | bool | true | Lower large aggregate params |
| `enable-sink` | bool | true | Enable Sinking |
| `disable-post-opt` | bool | false | Disable LLVM IR opts post-opt |
| `usedessa` | int | 2 | Select deSSA method |
| `ldg` | bool | true | Load Global Constant Transform |
| `print-isel-input` | bool | false | Print LLVM IR input to isel |
| `no-reg-target-nvptxremat` | bool | false | Only old remat without reg targets |
| `disable-set-array-alignment` | bool | false | Disable alignment enhancements |
| `nvptx-lower-global-ctor-dtor` | bool | — | Lower GPU ctor/dtors to globals |

### Register Pressure & FCA Options (ctor_074, 0x49AAB0)

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `fca-size` | int | 8 | Max size of first-class aggregates (bytes) |
| `reg-target-adjust` | int | 0 (range -10..+10) | Register pressure target adjustment |
| `pred-target-adjust` | int | 0 (range -10..+10) | Predicate register target adjustment |
| `remat-load-param` | bool | — | Support remating const `ld.param` not in NVVM IR |
| `cta-reconfig-aware-rpa` | bool | — | CTA reconfiguration-aware register pressure analysis |

### Extension Options (ctor_610, 0x5888A0)

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `unroll-assumed-size` | int | 4 | Assumed size for unknown local array types |
| `enable-loop-peeling` | bool | — | Enable loop peeling |
| `enable-256-bit-load-store` | bool | — | Enable 256-bit vector loads/stores |
| `ias-param-always-point-to-global` | bool | — | Parameters always point to global memory |
| `ias-strong-global-assumptions` | bool | — | Strong global memory assumptions |
| `ias-wmma-memory-space-opt` | bool | — | Memory Space Optimization for WMMA |

### TTI Cost Model Options (ctor_061, 0x494D20)

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `costmodel-reduxcost` | bool | — | Recognize reduction patterns |
| `cache-line-size` | int | — | Cache line size for cost model |
| `min-page-size` | int | — | Minimum page size |
| `predictable-branch-threshold` | float | — | Threshold for predictable branch cost |

## Differences from Upstream LLVM

1. **Dual-path TargetMachine construction.** Upstream LLVM has a single target creation path through `LLVMTargetMachine::createPassConfig`. NVIDIA has two independent paths (CLI and pipeline assembler) that converge at `TargetRegistry::lookupTarget`.

2. **NVIDIA-proprietary target features.** The `+sharedmem32bitptr`, `+fma-level=N`, `+prec-divf32=N`, `+prec-sqrtf32=N` features do not exist in upstream NVPTX. Upstream NVPTX has `+ptx75`, `+sm_90` style features. NVIDIA's math precision features are passed through the target feature string to avoid adding new cl::opt for each.

3. **224-byte feature table.** The `sub_982C80` feature table with its "default all-1s then selectively clear" initialization pattern is unique to cicc. Upstream NVPTXSubtarget uses a much simpler feature set derived from `+sm_XX` and `+ptx_YY` features.

4. **Scheduling info at TTI+56.** The issue-width and latency values stored in the TTI sub-structure at offset +56 are used by the interleave count selection algorithm. Upstream LLVM's NVPTX backend does not populate these scheduling parameters — it relies on the default "no scheduling model" behavior.

5. **Extension hook at vtable+88.** The pipeline assembler checks for a `createExtendedTargetMachine` entry, enabling loadable target backend extensions. This is not present in upstream LLVM.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| NVPTX Target Lookup and Creation | `sub_12EA530` | 4 KB | — |
| TargetMachine Creation with NVIDIA Options | `sub_12F4060` | 16 KB | — |
| Master Pipeline Assembly (includes TM setup) | `sub_12E54A0` | 50 KB | — |
| CICC CLI Argument Parser | `sub_12F7D90` | 14 KB | — |
| `TargetRegistry::lookupTarget()` | `sub_16D3AC0` | — | — |
| SubtargetInfo initialization | `sub_167F890` | — | — |
| TTIWrapperPass allocation (208 bytes) | `sub_1BFB520` | — | — |
| TargetTransformInfo / DataLayout creation | `sub_1BFB9A0` | — | — |
| TargetLibraryInfo creation | `sub_14A04B0` | — | — |
| TargetLibraryInfo finalization | `sub_149CBC0` | — | — |
| `TTI::getRegisterBitWidth(Vector)` — returns 32 | `sub_DFE640` | — | — |
| `TTI::supportsScalableVectors()` — returns false | `sub_DFE610` | — | — |
| `TTI::getMaxInterleaveFactor()` (at TTI+448) | `sub_DFB120` | — | — |
| `TTI::getMaxInterleaveFactor(vectorized)` | `sub_DFB730` | — | — |
| `TTI::getRegisterBitWidth(Scalar)` or cache-line query | `sub_DFB1B0` | — | — |
| `TTI::getInstructionCost()` / scheduling cost model | `sub_20E14F0` | 33 KB | — |
| `TTI::hasAttribute(N)` — function attribute query | `sub_B2D610` | — | — |
| `TTI::getInstructionCost()` (IR-level variant) | `sub_B91420` | — | — |
| NVPTX feature flag table initializer (224 bytes) | `sub_982C80` | — | — |
| Feature bitfield initial population | `sub_97DEE0` | — | — |
| SM-version-specific feature refinements | `sub_982B20` | — | — |
| SubtargetFeature reads at +2843, +2584, +2498 | `sub_201BB90` | — | — |
| Branch distance / jump table checks at +2870, +2871 | `sub_20650A0` | — | — |
| EDG SM architecture feature gating (38KB, ~60 flags) | `sub_60E7C0` | — | — |
| Module initialization with triple and data layout | `sub_908850` | — | — |
| SM processor table population (`0x584510`, 2.6KB) | `ctor_605` | — | — |
| NVPTX backend math options (`0x584B60`, 14KB) | `ctor_607` | — | — |
| NVPTX backend options (`0x585D30`, 37KB) | `ctor_609_0` | — | — |

## Cross-References

- [GPU Target Architecture](../targets/index.md) — Full SM table, architecture gating thresholds, NVVM container arch enum
- [LoopVectorize & VPlan](../llvm/loop-vectorize.md) — TTI hook usage in VF selection and interleave count
- [SLP Vectorizer](../llvm/slp-vectorizer.md) — TTI register width as SLP bundle width limit
- [SelectionDAG](../llvm/selectiondag.md) — NVPTXTargetLowering, type legality from SubtargetFeatures
- [Memory Space Optimization](../passes/memory-space-opt.md) — Address space numbering convention
- [IV Demotion](../passes/iv-demotion.md) — Exploits i64 cost differential reported by TTI
- [Register Allocation](../llvm/register-allocation.md) — Register pressure budgets bounded by TTI
- [Instruction Scheduling](../llvm/scheduling.md) — Scheduling model data at TTI+56
- [CLI Flags](../config/cli-flags.md) — `-arch`, `-ftz`, `-fma`, `-prec-div`, `-prec-sqrt` routing
- [Optimization Levels](../config/optimization-levels.md) — `qword_4FBB430` optimization level storage
- [Pipeline & Ordering](../llvm/pipeline.md) — Where TTI is registered in the pass pipeline
