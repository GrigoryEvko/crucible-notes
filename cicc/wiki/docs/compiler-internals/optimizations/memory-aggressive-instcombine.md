# AggressiveInstCombine - Aggressive Instruction Combining

**Pass Type**: Function-level aggressive pattern matching
**LLVM Class**: `llvm::AggressiveInstCombinerPass`
**Algorithm**: Extended pattern library beyond standard InstCombine
**Extracted From**: CICC pass mapping
**Analysis Quality**: MEDIUM - Limited direct evidence
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

AggressiveInstCombine extends standard InstCombine with more expensive pattern transformations that may increase code size or analysis cost but improve performance. It targets specific high-value patterns not covered by regular InstCombine.

**Key Innovation**: Willing to increase code size for performance gain, unlike standard InstCombine.

---

## Algorithm Complexity

| Metric | InstCombine | AggressiveInstCombine |
|--------|-------------|----------------------|
| **Pattern library** | 10,000+ patterns | ~200 aggressive patterns |
| **Analysis cost** | Low-medium | Medium-high |
| **Code size impact** | Always reduces | May increase |
| **Compile time** | 15-30% | +2-5% additional |

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `-disable-AggressiveInstCombinePass` | flag | - | Disable pass |
| `aggressive-instcombine-max-scan` | int | **100** | Maximum instruction scan |

---

## Core Patterns

### 1. Expensive Expression Expansion

```llvm
; Pattern: Expand for better parallelism (GPU-friendly)
%t1 = add i32 %a, %b
%t2 = add i32 %t1, %c
%r = add i32 %t2, %d

; May expand to:
%t1 = add i32 %a, %b
%t2 = add i32 %c, %d
%r = add i32 %t1, %t2
; Better instruction-level parallelism, but more registers
```

### 2. Fused Operations

```llvm
; Recognize multiply-shift patterns
%mul = mul i32 %x, 3
%shl = shl i32 %mul, 2

; Fuse to:
%r = mul i32 %x, 12  ; Single fused operation
```

### 3. Bit-Field Operations

```llvm
; Extract-modify-insert sequences
%ext = and i32 %val, 0xFF
%mod = add i32 %ext, 1
%ins = or i32 (and i32 %val, 0xFFFFFF00), %mod

; Optimize to specialized bitfield instruction (if available)
```

---

## CUDA-Specific Patterns

### Warp-Level Optimization

```llvm
; Recognize warp-uniform patterns
%tid = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
%uniform = shl i32 %tid, 0  ; Always 0 (uniform across warp)

; Optimize for warp-uniform execution
```

### Texture/Surface Operations

```llvm
; Combine texture coordinate computations
%u = fmul float %x, %scale
%v = fmul float %y, %scale
call @llvm.nvvm.tex.2d(float %u, float %v, ...)

; May fuse into single texture operation
```

---

## Performance Impact

| Metric | Improvement | Notes |
|--------|-------------|-------|
| **Execution time** | 2-8% | Pattern-dependent |
| **Code size** | -5% to +10% | May increase |
| **Compile time** | +2-5% | Additional overhead |

---

**L3 Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC pass mapping + LLVM reference
