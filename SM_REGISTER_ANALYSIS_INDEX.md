# SM Register Constraints Analysis Index

## Generated Documents

### 1. SM_REGISTER_CONSTRAINTS_TECHNICAL_REPORT.md (Main Document)
**Size**: 29 KB, 840 lines
**Content**: Comprehensive technical analysis of SM70-SM120 register constraints
**Structure**:
- Executive summary
- Architecture timeline
- Detailed per-SM analysis (SM70, SM75, SM80, SM86/89, SM90, SM100, SM120)
- Register file configuration
- Tensor core constraints with evidence
- Special registers & calling convention
- Alignment constraints
- Bank conflict avoidance
- Graph coloring algorithm
- Cross-architecture comparison
- Constraint implementation details
- Evidence summary with decompiled code locations
- Validation status and recommendations

**Use Case**: Complete technical reference for register allocation implementation

---

### 2. SM_REGISTER_CONSTRAINTS_QUICK_REFERENCE.md
**Content**: Quick lookup tables and summary information
**Sections**:
- Key parameters (all SM versions)
- Register file size comparison
- Register classes table
- Tensor core accumulators by SM
- Alignment constraints
- Per-SM constraint summary
- Graph coloring algorithm overview
- Evidence locations (function addresses)
- Constraint edge weighting
- Cost model comparison
- Validation checklist
- Feature comparison matrix

**Use Case**: Quick reference during development, easy lookup

---

## Source Material Used

### L3 Analysis Files
1. **register_class_constraints.json** (Unknown #22)
   - Complete register class definitions for 8 SM architectures
   - Register counts: 255 (GPR32), 127 (GPR64), 7 (PRED), 255 (H16)
   - K=15 physical registers confirmed

2. **REGISTER_CONSTRAINTS_SUMMARY.md** (Unknown #22)
   - Executive summary of constraint definitions
   - Register class constraints table
   - SM-specific constraint summary

3. **tensor_core_costs.json** (Unknown #14)
   - Latency, throughput, and cost tables for all SM versions
   - WMMA costs (SM70)
   - mma.sync costs (SM80)
   - warpgroup_mma costs (SM90)
   - tcgen05 costs (SM100, SM120)

4. **graph_coloring_priority.json** (Unknown #4)
   - K=15 physical register confirmation
   - Briggs criterion threshold (14 = K-1)
   - Coalescing factor 0.8 (magic constant 0xCCCCCCCCCCCCCCCD)
   - Evidence locations in decompiled code

5. **spill_cost_formula.json** (Unknown #1)
   - Register allocation formula structure
   - Evidence locations

### Decompiled Code Evidence
- **0xB612D0** (102 KB) - BuildInterferenceGraph
- **0x1081400** (69 KB) - SimplifyAndColor
- **0x1090BD0** (61 KB) - SelectNodeForRemoval (K=15 evidence @ offset 1039)
- **0x94CAB0** - WMMA instruction selection (SM70)
- **0xA8E250** - TCGen05 instruction parsing (SM90+)
- **0x35F5090** - SM100+ specific operations

### Wiki Documentation
- /cicc/wiki/docs/architectures/index.md (SM architecture specifications)
- /cicc/wiki/docs/compiler-internals/tensor-core-codegen.md (Tensor core details)
- /cicc/wiki/docs/compiler-internals/register-allocation.md (Register allocation algorithms)

---

## Key Findings Summary

### Universal Parameters
- **K (Physical Registers)**: 15 (confirmed by 0x1090BD0:1039)
- **Coalescing Factor**: 0.8 (magic constant 0xCCCCCCCCCCCCCCCD)
- **Max Virtual Registers**: 255 (R0-R254)
- **Bank Count**: 32
- **Bank Width**: 4 bytes
- **Bank Conflict Penalty**: 32 cycles

### Register File Size Progression
- **SM70-89**: 64 KB per warp
- **SM90+**: 128 KB per warp (2× increase)

### Tensor Core Evolution

| Generation | SM | Unit | Accum Regs | Matrix | Latency |
|------------|-----|------|----------|--------|---------|
| Volta | 70/75 | WMMA | 8 | 16×16×16 | 8 cy |
| Ampere | 80/86/89 | mma.sync | 4 | 16×8×16 | 4 cy |
| Hopper | 90 | warpgroup_mma | 8 | 16×16×16 | 3 cy |
| Blackwell | 100/120 | tcgen05 | 8 | 16×16×16 | 2 cy |

### New Features per SM

**SM80 (Ampere)**:
- cp.async (async copy, 10 cycle latency)
- 2:4 structured sparsity support
- Reduced MMA accumulator size (4 vs 8 registers)

**SM90 (Hopper)**:
- Doubled register file (64KB → 128KB)
- Warpgroup MMA (128 threads, 4 warps)
- TMA (Tensor Memory Accelerator)
- Warpgroup barrier operations

**SM100 (Blackwell)**:
- TCGen05 tensor core (2 cycle latency, 50% vs Hopper)
- FP4 (E2M1) support (4-bit format)
- Block-scale quantization
- Enhanced sparsity (cost reduction 0.25 vs 0.5)
- Descriptor management operations

**SM120 (Blackwell-Ultra)**:
- Dual tensor cores (2× throughput)
- All SM100 instructions unchanged

---

## Constraint Mechanism

**Implementation**: Implicit edges in Chaitin-Briggs interference graph coloring

**Phases**:
1. Liveness analysis (backward dataflow)
2. Interference graph construction (180+ instruction types)
3. Conservative coalescing (George's criterion, 0.8 factor)
4. Briggs simplification & coloring (K=15 threshold)
5. Lazy reload optimization

**Constraint Types**:
- **Alignment**: 64-bit even, 128-bit 4-aligned, tensor core consecutive
- **Aliasing**: 32-bit vs 64-bit register pair conflicts
- **Bank conflict**: 32 banks, 4-byte width (formula: bank = (addr % 128) / 4)
- **Tensor core**: Accumulator consecutiveness
- **Warpgroup** (SM90+): Cross-warp coordination
- **Descriptor** (SM100+): Reserved register regions

---

## Confidence Levels

### HIGH Confidence (90%+)
- K=15 physical register count
- Register class counts (255, 127, 7)
- Alignment requirements (even for 64-bit)
- Register file sizes (64KB vs 128KB)
- Coalescing factor 0.8
- Graph coloring mechanism

### MEDIUM Confidence (60-80%)
- Exact SM-specific constraint multipliers
- Bank conflict penalty formula (2.0 weight)
- Warpgroup coordination specifics
- Descriptor management (SM100+)
- TMA constraint details
- FP4 allocation impact

### MEDIUM-LOW Confidence (40-60%)
- Exact register class constraint table format
- Constraint enforcement performance overhead
- Spill cost coefficient adjustments
- Loop depth multiplier exact value (suspected 1.5)

---

## Validation Status

### Validated
- K=15 threshold in decompiled code
- Coalescing factor via magic constant
- Register class counts from PTX specification
- Alignment requirements from architecture docs

### Recommended for Validation
- PTX output analysis against documented constraints
- Hardware profiling of bank conflict penalty
- Tensor core register utilization patterns
- Warpgroup coordination verification
- Descriptor management in SM100+ code

---

## Usage Guide

### For Quick Lookup
→ Use **SM_REGISTER_CONSTRAINTS_QUICK_REFERENCE.md**

### For Detailed Technical Reference
→ Use **SM_REGISTER_CONSTRAINTS_TECHNICAL_REPORT.md**

### For Evidence Verification
→ Reference decompiled code locations in both documents

### For Implementation
→ Refer to graph coloring algorithm section + constraint implementation details

---

## File Locations

```
/home/user/nvopen-tools/SM_REGISTER_CONSTRAINTS_TECHNICAL_REPORT.md
/home/user/nvopen-tools/SM_REGISTER_CONSTRAINTS_QUICK_REFERENCE.md
/home/user/nvopen-tools/SM_REGISTER_ANALYSIS_INDEX.md (this file)
```

---

## Related Analysis Files (in codebase)

```
cicc/deep_analysis/L3/register_allocation/register_class_constraints.json
cicc/deep_analysis/L3/register_allocation/REGISTER_CONSTRAINTS_SUMMARY.md
cicc/deep_analysis/L3/register_allocation/INDEX.md
cicc/deep_analysis/L3/instruction_selection/tensor_core_costs.json
cicc/deep_analysis/L3/register_allocation/graph_coloring_priority.json
cicc/deep_analysis/L3/register_allocation/spill_cost_formula.json
```

---

**Document Generated**: 2025-11-16
**Analysis Basis**: 8+ L3 analysis agents, 80,281 decompiled CICC files
**Total Documentation**: ~1,500 lines across all generated files
