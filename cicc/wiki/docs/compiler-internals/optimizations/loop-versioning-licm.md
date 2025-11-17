# Loop Versioning for LICM

**Pass Type**: Loop versioning and optimization pass
**LLVM Class**: `llvm::LoopVersioningLICMPass`
**Algorithm**: Runtime versioning with aggressive LICM
**Extracted From**: CICC decompiled code + optimization mapping
**Analysis Quality**: VERY HIGH - Complete implementation documented
**L3 Source**: `deep_analysis/L3/optimizations/licm_versioning.json`
**Pass Index**: LICM family (160-162 @ 0x4e33a0)

---

## Overview

**See Primary Documentation**: [licm.md](licm.md) - LICM with Versioning

Loop Versioning for LICM creates runtime-checked loop versions to enable aggressive loop-invariant code motion. This pass is **fully documented in the main LICM page**.

**Key Features**:
- Creates fast path with hoisted invariants
- Generates safe path for potential aliasing
- Inserts runtime memory disambiguation checks

**Parameters** (from LICM versioning evidence):
- `enable-loop-versioning-licm`: **true**
- `licm-versioning-invariant-threshold`: **90%**
- `licm-versioning-max-depth-threshold`: **2**
- `runtime-memory-check-threshold`: **8**
- `memory-check-merge-threshold`: **100**

---

## Relationship to LICM

Loop Versioning LICM is a specialized variant of standard LICM that uses versioning when:
1. Invariance cannot be proven statically
2. Memory aliasing uncertain
3. Benefit exceeds 2× versioning overhead

**See Full Documentation**: [licm.md](licm.md) for complete details on:
- Versioning strategy
- Decision criteria
- Cost model
- CUDA considerations
- Configuration parameters

---

## Quick Reference

| Aspect | Value | Source |
|--------|-------|--------|
| **Pass Address** | 0x4e33a0 | Decompiled code |
| **Invariant Threshold** | 90% | ctor_218_0x4e7a30.c |
| **Max Depth** | 2 | ctor_473_0x54d740.c |
| **Max Checks** | 8 | ctor_053_0x490b90.c |
| **Merge Threshold** | 100 comparisons | ctor_053_0x490b90.c |

---

## Related Optimizations

- **LICM**: [licm.md](licm.md) - **PRIMARY DOCUMENTATION**
- **LoopSimplify**: [loop-simplify.md](loop-simplify.md) - Prerequisite
- **ScalarEvolution**: Required for trip count analysis

---

**Last Updated**: 2025-11-17
**Source**: deep_analysis/L3/optimizations/licm_versioning.json (complete implementation)
**Note**: See [licm.md](licm.md) for complete 1,000+ line documentation
