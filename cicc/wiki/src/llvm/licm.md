# LICM (Loop-Invariant Code Motion)

> **Note on sweep misidentification.** The P2C.3 analysis sweep labeled `sub_19B73C0` as "LICM constructor." Deeper analysis reveals this is actually the **LoopUnroll** pass factory. The vtable at `unk_4FB224C`, the 7-parameter constructor signature with partial/runtime/upper-bound/profile-based toggles, and diagnostic function strings (`sub_19B78B0`, `sub_19B7B10`, `sub_19B7D80`) all confirm LoopUnroll identity. The two passes share loop-pass infrastructure and are adjacent in the binary, which caused the confusion. The actual LICM pass resides elsewhere, likely involving `sub_1560180` and loop-invariant-motion-specific logic. This page documents what was recovered under the LICM label; most content pertains to LoopUnroll as shipped in CICC v13.0.

## LoopUnroll Pass Factory

The pass factory at `sub_19B73C0` allocates a 184-byte pass object and accepts seven parameters that control unroll behavior. When a parameter is -1, the pass uses its compiled-in default.

### Constructor Parameters

| Parameter | Offset | Enable Flag | Semantics |
|-----------|--------|-------------|-----------|
| `a1` (optimization level) | +156 | -- | 2 = standard, 3 = aggressive |
| `a2` (unroll threshold) | +168 | +172 | Trip count threshold; -1 = use default |
| `a3` (unroll count) | +160 | +164 | Explicit unroll factor; -1 = use default |
| `a4` (allow partial) | +176 | +177 | 0 = disable partial unroll, 1 = enable |
| `a5` (runtime unroll) | +178 | +179 | 0 = disable runtime unroll, 1 = enable |
| `a6` (upper bound) | +180 | +181 | 0 = disable upper-bound unroll, 1 = enable |
| `a7` (profile-based) | +182 | +183 | 0 = disable profile-guided unroll, 1 = enable |

### Object Construction

The factory allocates 184 bytes via `sub_22077B0`, sets the vtable to `off_49F45F0` (loop-unroll pass vtable), stores pass ID `unk_4FB224C` at offset +16, initializes self-referential linked-list pointers at offsets +80/+88 and +128/+136, sets pass type 2 (FunctionPass) at offset +24, and calls `sub_163A1D0` / `sub_19B71A0` for pass registration.

## Pipeline Configurations

CICC invokes LoopUnroll with five distinct configurations at different pipeline stages, reflecting NVIDIA's careful tuning of unroll aggressiveness per compilation phase.

### Configuration A: Standard Pipeline (O1/O2)

Call site: `sub_12DE330`

```
LoopUnroll(2, -1, -1, -1, -1, -1, -1)
```

All parameters at defaults. Standard unrolling with default thresholds at optimization level 2.

### Configuration B: Code-Size Mode

Call site: `sub_12DE8F0`, when `*(a3+4480) < 0` (NVIDIA code-size flag set)

```
LoopUnroll(a2, -1, -1, 0, 0, 0, 0)
```

All unrolling features disabled: partial, runtime, upper-bound, and profile-based are all zeroed. The pass only unrolls when the trip count is statically known and the benefit is certain. This reflects the constraint that GPU register pressure makes speculative unrolling expensive when code size matters.

### Configuration C: Normal Optimizer

Call site: `sub_12DE8F0`, when `*(a3+4480) >= 0` (normal mode)

```
LoopUnroll(a2, -1, -1, -1, -1, -1, -1)
```

Fully aggressive unrolling with all defaults. The optimization level is passed through from the caller.

### Configuration D: Late Pipeline (Conservative)

Call site: `sub_12DE8F0`, late pipeline position

```
LoopUnroll(a2, -1, -1, 0, 0, -1, -1)
```

Partial and runtime unrolling disabled, but upper-bound and profile-based unrolling retain their defaults. This conservative late-pipeline configuration avoids creating new runtime overhead in code that has already been substantially optimized.

### Configuration E: Aggressive Pipeline (O3)

Call site: `sub_12E54A0`

```
LoopUnroll(3, -1, -1, 0, 0, -1, 0)
```

Optimization level 3 with aggressive thresholds, but partial, runtime, and profile-based unrolling are disabled. Only upper-bound unrolling retains its default. The rationale is that at O3, the higher thresholds already capture most profitable unrolling opportunities without needing speculative runtime checks.

### Configuration F: User-Configured

Call site: `sub_12EA3A0`

```
LoopUnroll(a1[4], a1[5], a1[6], a1[7], a1[8], a1[9], a1[10])
```

All seven parameters are read from a stored configuration object, enabling user-specified unroll behavior via command-line flags or pragmas.

## Threshold Initialization

The function `sub_19B6690` (17 KB) configures unroll thresholds based on optimization level and LLVM knobs. Default threshold values:

| Offset | Field | Default (O2+) | Default (O1) |
|--------|-------|---------------|--------------|
| +0 | OptThreshold | 405 | 150 |
| +4 | Threshold | 400 | 400 |
| +12 | SmallTripCountThreshold | 150 | 150 |
| +56 | MaxIterationsCountToAnalyze | 60 | 60 |

### Function-Attribute-Aware Override

The threshold initializer queries function attributes via `sub_1560180`:

- **Attribute ID 34** (`minsize`): Reduces `OptThreshold` to `SmallTripCountThreshold` (150).
- **Attribute ID 17** (`optsize`): Same reduction.

This means kernels annotated with size constraints get conservative unroll thresholds regardless of the global optimization level.

### Knob-Driven Override

The function queries the LLVM option registry (`dword_4FA0208` BST) ten times, each time looking up a different knob address. For each knob, it searches the BST rooted at `dword_4FA0208[2]`, compares the current function hash (`sub_16D5D50`) against node ranges, and applies the override if the knob value meets the threshold. The knob-to-field mapping:

| Knob Address | Override Address | Field |
|---|---|---|
| `dword_4FB3228` | `dword_4FB32C0` | OptThreshold (+0) |
| `dword_4FB3148` | `dword_4FB31E0` | SmallTripCountThreshold (+12) |
| `dword_4FB3068` | `dword_4FB3100` | Threshold (+4) |
| `dword_4FB2DC8` | `dword_4FB2E60` | field +32 |
| `dword_4FB2CE8` | `dword_4FB2D80` | field +36 |
| `dword_4FB2C08` | `dword_4FB2CA0` | field +24 |
| `dword_4FB2B28` | (next value) | field +40 |

The per-function BST lookup keyed by function hash enables fine-grained tuning of unroll behavior per kernel, a capability not present in upstream LLVM.

## Diagnostic Functions

Three diagnostic emission functions produce optimization remarks:

| Function | Address | Diagnostic |
|----------|---------|-----------|
| `emitPragmaCountDiag` | `sub_19B78B0` | Reports when pragma unroll count conflicts with trip multiple |
| `emitThresholdDiag` | `sub_19B7B10` | Reports when unrolled size exceeds threshold |
| `emitLoopSizeDiag` | `sub_19B7D80` | Reports when loop body is too large to unroll |

## Main Loop Processing

The primary analysis function `sub_19B7FA0` (11 KB) analyzes each candidate loop. The pass also uses hash table infrastructure:

| Function | Address | Size | Role |
|----------|---------|------|------|
| `rehashSmallTable` | `sub_19B60B0` | 5 KB | Small hash table resize |
| `rehashTable` | `sub_19B8820` | 4 KB | Key-value hash table resize |
| `rehashSet` | `sub_19B89E0` | 7 KB | Set hash table resize |
| `insertIntoSet` | `sub_19B8DA0` | -- | Set insert with growth |

All hash tables use the same `(value >> 9) ^ (value >> 4)` hash function and linear probing strategy found throughout CICC's LLVM passes.

## GPU-Specific Considerations

Loop unrolling in CICC differs from CPU-targeted LLVM in several important ways:

1. **Register pressure sensitivity.** GPU kernels have a fixed register budget per thread (typically 32-255 registers). Aggressive unrolling increases register pressure, which reduces occupancy (threads per SM). The multi-configuration pipeline reflects this tradeoff: early passes unroll aggressively to expose optimization opportunities, while late passes are conservative to avoid register spills after allocation decisions are partially committed.

2. **Code-size mode.** The `*(a3+4480) < 0` flag gates a code-size-sensitive mode that disables all speculative unrolling. This is relevant for GPU binaries where instruction cache pressure affects performance, particularly on older architectures with smaller I-caches.

3. **Per-kernel tuning.** The BST-based per-function knob override system allows different kernels in the same compilation unit to receive different unroll thresholds. This is significant because GPU workloads often contain kernels with vastly different register and memory access characteristics.

4. **Interaction with other passes.** LoopUnroll runs at multiple pipeline stages with different configurations. The early aggressive configuration creates opportunities for subsequent GVN, DSE, and InstCombine passes. The late conservative configuration runs after those passes have cleaned up redundancies, avoiding re-introduction of code bloat.

## Key Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| `LoopUnroll::create` | `0x19B73C0` | 2.3 KB | 7-parameter pass factory |
| `LoopUnroll::initThresholds` | `0x19B6690` | 17 KB | Threshold configurator |
| `LoopUnroll::processLoop` | `0x19B7FA0` | 11 KB | Main loop analysis |
| `LoopUnroll::registerPass` | `0x19B71A0` | -- | Pass registration |
| `LoopUnroll::initWithName` | `0x19B75E0` | -- | Pass init with string name |
| `LoopUnroll::emitPragmaCountDiag` | `0x19B78B0` | -- | Pragma vs. trip multiple diagnostic |
| `LoopUnroll::emitThresholdDiag` | `0x19B7B10` | -- | Size exceeds threshold diagnostic |
| `LoopUnroll::emitLoopSizeDiag` | `0x19B7D80` | -- | Body too large diagnostic |
| `LoopUnroll::rehashSmallTable` | `0x19B60B0` | 5 KB | Small hash table resize |
| `LoopUnroll::rehashTable` | `0x19B8820` | 4 KB | Key-value hash table resize |
| `LoopUnroll::rehashSet` | `0x19B89E0` | 7 KB | Set hash table resize |

## Differences from Upstream LLVM

1. **Multi-stage pipeline invocation.** Upstream LLVM typically invokes LoopUnroll once. CICC invokes it at 5+ pipeline positions with different feature configurations tailored to the compilation phase.
2. **Per-function threshold override via BST.** Upstream LLVM uses global knobs. NVIDIA adds a BST keyed by function hash that allows per-kernel threshold customization.
3. **Code-size gating.** The `*(a3+4480)` flag provides a binary-wide code-size mode that disables all speculative unrolling features, reflecting GPU-specific I-cache and register pressure constraints.
4. **Function-attribute integration.** The `minsize` and `optsize` attribute checks reduce thresholds for size-constrained kernels, enabling mixed compilation strategies within a single translation unit.
