# Tensor Register Alignment Analysis - Complete Documentation

This directory contains a comprehensive extraction and analysis of tensor core register alignment requirements across all NVIDIA GPU SM generations supported by CICC.

## Files Generated

### 1. TENSOR_ALIGNMENT_SPECIFICATION.md (1105 lines, 41KB)
**Primary Technical Reference Document**

Complete technical specification covering:
- SM70 (Volta) WMMA alignment rules (4-register boundaries, 8-register accumulators)
- SM80 (Ampere) mma.sync alignment (consecutive registers mandatory, 4-register accumulators)
- SM90 (Hopper) warpgroup MMA alignment (8-register boundaries, cross-warp coordination)
- SM100/120 (Blackwell) tcgen05 alignment (enhanced efficiency, FP4/INT4 support)
- Alignment by precision (FP16, BF16, TF32, FP32, FP64, INT8, INT4, FP8, FP4)
- Alignment by matrix size (16x16x16, 16x8x16, etc. with register counts)
- Accumulator alignment (why stricter constraints, penalty for violations)
- Register allocator enforcement (4-phase algorithm with code examples)
- Cross-warp alignment for SM90+ (warpgroup structure, synchronization, 128-thread requirements)
- Cost analysis (spill formulas, register pressure impact)

**Use this document for:**
- Deep technical understanding of alignment rules
- Register allocator implementation details
- Understanding hardware constraints
- Performance impact analysis
- Code citation and evidence tracking

### 2. TENSOR_ALIGNMENT_SUMMARY.md (345 lines, 14KB)
**Executive Summary and Quick Reference**

High-level overview including:
- Key findings for each SM generation
- Alignment requirements by operation (WMMA vs mma.sync vs warpgroup vs tcgen05)
- Alignment by precision with fragment sizes
- Alignment by matrix size (typical configurations)
- Accumulator alignment rules and penalties
- Register allocator enforcement (4-phase algorithm)
- Cross-warp alignment structure and synchronization
- Cost model and register pressure impact
- Evidence sources (JSON documents and decompiled code)
- Validation checklist
- Summary tables and comparison matrices
- Confidence assessment for each finding

**Use this document for:**
- Quick lookup of alignment requirements
- Sharing findings with team members
- Understanding key differences between SM generations
- Validating your implementation
- Assessing analysis confidence levels

## Key Findings Summary

### Alignment Requirements (One-Line Summary)

| SM | Operation | Accumulator | Matrix Operands | Why |
|----|-----------|-------------|-----------------|-----|
| 70 | WMMA | 2-register (even) | 4-register | 4-wide HW pipelines, bank conflicts |
| 80 | mma.sync | **Consecutive** | **Consecutive** | Single 4-wide execution unit |
| 90 | warpgroup_mma | **8-reg warpgroup** | **8-reg/warp** | Hardware broadcast, 128-thread sync |
| 100| tcgen05 | **8-reg warpgroup** | **8-reg/warp** | Enhanced efficiency, new precisions |

### Critical Differences

1. **SM70**: Preferred but not mandatory 4-register boundaries (hardware loads 4/cycle)
2. **SM80**: Strict consecutive requirement (enables tighter scheduling, 4-cycle latency)
3. **SM90/100**: 8-register per-warp alignment with warpgroup-wide coordination (128KB register file)

### Penalty for Misalignment

- **SM70 misalignment**: 50-100 spill cycles per iteration
- **SM80 misalignment**: 100-150 spill cycles per iteration
- **SM90 misalignment**: 200-500 cycle stall (warpgroup barrier failure)
- **SM100 misalignment**: 200-500 cycle stall (similar to SM90)

## Evidence Sources

### Primary L3 Analysis Documents

Located in `/home/user/nvopen-tools/cicc/deep_analysis/L3/`:

1. **register_allocation/register_class_constraints.json** (912 lines)
   - SM-specific constraints for SM70-SM120
   - Register classes and alignments
   - Tensor core requirements

2. **instruction_selection/tensor_core_costs.json** (500+ lines)
   - Latency, throughput, cost models
   - All tensor core instructions

3. **cuda_specific/bank_conflict_analysis.json** (230+ lines)
   - 32-bank structure analysis
   - Conflict detection and avoidance

4. **cuda_specific/warp_specialization_sm90.json** (400+ lines)
   - Warpgroup coordination patterns
   - Producer/consumer model

5. **cuda_specific/tma_scheduling_sm90.json** (300+ lines)
   - TMA instruction set
   - Register alignment for async operations

### Decompiled Code References

- `sub_B612D0_0xb612d0.c` @ 0xb612d0 - Register allocation constraint insertion
- `sub_1090BD0_0x1090bd0.c` @ 0x1090bd0 - K=15 physical register threshold
- `sub_1081400_0x1081400.c` @ 0x1081400 - Graph coloring with alignment
- `sub_A78010_0xa78010.c` @ 0xa78010 - Spill code generation

## Register Allocator Implementation

### 4-Phase Algorithm

```
Phase 1: Constraint Propagation
├─ Identify tensor core operations
├─ Add implicit constraint edges to interference graph
└─ Mark accumulator/matrix/metadata registers

Phase 2: Conservative Coalescing
├─ Merge virtual registers with factor 0.8
├─ Check alignment compatibility
└─ Prevent aggressive constraint violations

Phase 3: Graph Coloring with Alignment
├─ Color interference graph (K=15 physical registers)
├─ Filter allowed registers by alignment constraint
└─ Mark registers for spilling if no valid coloring

Phase 4: Spill Code Generation
├─ Lazy reload at use points (not at spill site)
├─ Respect alignment for temporary registers
└─ Insert load instructions with aligned destinations
```

### Constraint Enforcement Method

**Implicit Graph Coloring Edges**: CICC uses Chaitin-Briggs graph coloring enhanced with implicit constraint edges. Rather than special coloring rules, incompatible virtual registers are prevented from using the same physical register by adding interference graph edges.

**Conservative Coalescing**: Magic constant 0xCCCCCCCCCCCCCCCD = 4/5 (fixed-point). Coalescing factor of 0.8 prevents aggressive merging that might violate alignment.

## Hardware Architecture Summary

### Register File Organization

| SM | KB/Warp | Total Registers | Physical (K) | Register Classes |
|----|---------|-----------------|--------------|-----------------|
| 70 | 64 | 255 virtual | 15 | GPR32, GPR64, PRED, H16 |
| 80 | 64 | 255 virtual | 15 | Same + TMA awareness |
| 90 | 128 | 255 virtual | 15 | Same + warpgroup constraints |
| 100| 128 | 255 virtual | 15 | Same + descriptor management |

### Bank Conflict Structure

**All SM versions**:
- Banks per SM: 32
- Bank width: 4 bytes per bank
- Cache line: 128 bytes (covers 32 banks)
- Conflict penalty: 32-cycle serialization (2.0x weight in cost model)
- Addressing: bank_index = (address % 128) / 4

## Validation Checklist

Before implementing or debugging tensor core register allocation:

- [ ] Verify fragment sizes match target SM generation
- [ ] Check accumulator alignment (even/consecutive/8-reg)
- [ ] Validate matrix operand boundaries
- [ ] For SM90+: confirm warpgroup coordination across 4 warps
- [ ] Measure spill cost impact if alignment fails
- [ ] Profile bank conflict penalties
- [ ] Test with high-pressure tensor kernels (occupancy < 50%)
- [ ] Compare results across SM generations

## Confidence Assessment

### HIGH CONFIDENCE (90%+)
- Alignment rule structure and boundaries
- K=15 physical register count
- Basic SM-specific patterns
- Graph coloring implementation
- Spill cost integration

### MEDIUM CONFIDENCE (60-80%)
- Exact constraint multipliers by SM
- Bank conflict penalty coefficients
- Warpgroup coordination details
- Tensor core utilization patterns

### MEDIUM-LOW CONFIDENCE (40-60%)
- Exact constraint table formats
- TMA descriptor management
- Performance overhead metrics

## Related Resources

### CICC Analysis Structure

```
cicc/deep_analysis/L3/
├── register_allocation/          # Register constraint analysis
│   ├── register_class_constraints.json
│   ├── register_constraints_validation.json
│   ├── graph_coloring_priority.json
│   ├── spill_cost_formula.json
│   └── lazy_reload_algorithm.json
├── instruction_selection/        # Tensor core costs
│   ├── tensor_core_costs.json
│   ├── cost_model_complete.json
│   └── pattern_database.json
├── cuda_specific/                # CUDA-specific optimizations
│   ├── bank_conflict_analysis.json
│   ├── warp_specialization_sm90.json
│   ├── tma_scheduling_sm90.json
│   └── sparsity_support_sm100.json
└── [other modules...]
```

### Reading Order

1. Start with **TENSOR_ALIGNMENT_SUMMARY.md** (this file)
2. Reference specific SM sections in **TENSOR_ALIGNMENT_SPECIFICATION.md**
3. Check evidence documents in `cicc/deep_analysis/L3/` for details
4. Consult decompiled code for implementation specifics

## Quick Start

### To understand SM70 alignment:
See TENSOR_ALIGNMENT_SPECIFICATION.md → "SM70 (Volta) WMMA Alignment"
Key: 4-register boundaries for matrix operands, even-register accumulators

### To understand SM80 alignment:
See TENSOR_ALIGNMENT_SPECIFICATION.md → "SM80 (Ampere) mma.sync Alignment"
Key: Consecutive registers mandatory (strict requirement)

### To understand SM90+ warpgroup alignment:
See TENSOR_ALIGNMENT_SPECIFICATION.md → "SM90 (Hopper) Warpgroup Alignment"
Key: 8-register per-warp, coordinated across 4 warps, 128-thread synchronization

### To validate your implementation:
See TENSOR_ALIGNMENT_SUMMARY.md → "Validation Checklist"
Use the provided checklist to verify your register allocator

## Questions and Further Research

### Answered Questions
- How do alignment constraints vary by SM generation?
- What is the hardware reason for each alignment requirement?
- How does the register allocator enforce alignment?
- What is the performance cost of misalignment?
- How do warpgroups coordinate in SM90+?

### Open Questions for Future Work
- How do descriptor operations affect register allocation in SM100?
- What is the exact TMA descriptor register management algorithm?
- How do dual tensor cores coordinate in SM120?
- What are the edge cases for unusual matrix sizes?

## Contact and Attribution

**Analysis Date**: 2025-11-17
**Analysis Source**: CICC decompiled binary + L3 deep analysis
**Total Documentation**: 1450 lines, 55KB
**Research Quality**: HIGH for structural understanding, MEDIUM for performance predictions

Generated from comprehensive reverse engineering of NVIDIA CICC compiler for CUDA kernels.

---

**File locations:**
- Main Specification: `/home/user/nvopen-tools/TENSOR_ALIGNMENT_SPECIFICATION.md`
- Executive Summary: `/home/user/nvopen-tools/TENSOR_ALIGNMENT_SUMMARY.md`
- This README: `/home/user/nvopen-tools/README_TENSOR_ALIGNMENT.md`

