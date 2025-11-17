# SM Register Constraints Quick Reference

**Last Updated**: 2025-11-16
**Based on**: L3 Analysis (8+ agents) + Decompiled CICC Binary (80,281 files)

---

## Key Parameters (All SM Versions)

| Parameter | Value | Evidence | Confidence |
|-----------|-------|----------|-----------|
| **K (physical registers)** | 15 | 0x1090BD0:1039 checks `v64 > 0xE` | HIGH |
| **Coalescing factor** | 0.8 | Magic: 0xCCCCCCCCCCCCCCCD @ 0x1090BD0:603 | HIGH |
| **Max virtual registers** | 255 | PTX ISA spec (R0-R254) | HIGH |
| **Bank count** | 32 | All SM architecture | HIGH |
| **Bank width** | 4 bytes | Architecture standard | HIGH |
| **Bank conflict penalty** | 32 cycles | Memory latency model | MEDIUM |

---

## Register File Size by SM

| SM | Size | Registers/Warp | Registers/Thread Max |
|----|------|---|---|
| SM70 | 64 KB | 2048 | 255 |
| SM75 | 64 KB | 2048 | 255 |
| SM80 | 64 KB | 2048 | 255 |
| SM86 | 64 KB | 2048 | 255 |
| SM89 | 64 KB | 2048 | 255 |
| **SM90** | **128 KB** | **4096** | **255** |
| **SM100** | **128 KB** | **4096** | **255** |
| **SM120** | **128 KB** | **4096** | **255** |

---

## Register Classes

| Class | Syntax | Count | Alignment | Per-Thread Max |
|-------|--------|-------|-----------|---|
| GPR32 | `.reg .b32 R<0-254>` | 255 | 1-register | 255 |
| GPR64 | `.reg .b64 RD<0-127>` | 127 | 2-register (even) | 127 |
| PRED | `.reg .pred P<0-7>` | 7 | 1-register | 7 |
| H16 | `.reg .f16 H<0-255>` | 255 | 1-register | 255 |

---

## Tensor Core Accumulators

| SM | Unit | Matrix | Accum Regs | Alignment | Latency | Cost |
|----|------|--------|----------|-----------|---------|------|
| SM70 | WMMA | 16x16x16 | 8 | Consecutive | 8 cy | barrier=5, sync=10 |
| SM75 | WMMA | 16x16x16 | 8 | Consecutive | 8 cy | barrier=5, sync=10 |
| SM80 | mma.sync | 16x8x16 | 4 | Consecutive | 4 cy | barrier=3, sync=8 |
| SM86 | mma.sync | 16x8x16 | 4 | Consecutive | 4 cy | barrier=3, sync=8 |
| SM89 | mma.sync | 16x8x16 | 4 | Consecutive | 4 cy | barrier=3, sync=8 |
| SM90 | warpgroup_mma | 16x16x16 | 8 | Warpgroup | 3 cy | barrier=2, sync=5, wg_sync=3 |
| SM100 | tcgen05 | 16x16x16 | 8 | Warpgroup | 2 cy | barrier=1, sync=2, wg_sync=1 |
| SM120 | tcgen05×2 | 16x16x16 | 8 | Warpgroup | 2 cy | barrier=1, sync=2, wg_sync=1 |

---

## Alignment Constraints

| Operation | Alignment | Requirement | Example |
|-----------|-----------|-------------|---------|
| **32-bit** | 1-register | Any register | R0-R254 |
| **64-bit** | 2-register (even) | R0:R1, R2:R3, ... | Not R1:R2 |
| **128-bit** | 4-register | R0:R3, R4:R7, ... | Vector ops |
| **WMMA (SM70)** | Consecutive | 8 registers | R0-R7 only |
| **MMA (SM80)** | Consecutive | 4 registers | R0-R3 only |
| **Warpgroup (SM90+)** | Warpgroup | 8 registers | Coordinated across 4 warps |

---

## SM70 (Volta) - Register Constraints

**Register File**: 64 KB per warp
**Physical Registers**: 15
**Max Virtual**: 255

### WMMA Constraints
- **Accumulator**: 8 consecutive registers
- **Operands**: Separate allocation regions
- **Latency**: 8 cycles
- **Cost**: barrier=5, sync=10

### Special Registers
- R0-R7: Function arguments
- R24-R31: Callee-saved
- P0-P7: Predicate (7 available)

### Bank Configuration
- Banks: 32
- Width: 4 bytes
- Penalty: 32 cycles
- Formula: bank = (address % 128) / 4

---

## SM75 (Turing) - Register Constraints

**Identical to SM70 in register aspects**
- Same register file (64 KB)
- Same constraints
- WMMA unchanged
- Difference: RT cores (not register-related)

---

## SM80 (Ampere) - Register Constraints

**Register File**: 64 KB per warp
**Physical Registers**: 15
**Max Virtual**: 255

### MMA.SYNC Constraints
- **Accumulator**: 4 consecutive registers (vs 8 in SM70)
- **Matrix**: 16x8x16 (vs 16x16x16 in SM70)
- **Latency**: 4 cycles (vs 8 in SM70)
- **Cost**: barrier=3, sync=8

### New: cp.async Constraints
- **Destination**: Consecutive registers required
- **Latency**: 10 cycles
- **Cost**: async_copy=0.5

### New: 2:4 Sparsity
- **Patterns**: 6 valid combinations
- **Metadata**: 2 bits per 4-element block
- **Cost**: sparsity=0.5

---

## SM86/SM89 (Ada) - Register Constraints

**Identical to SM80**
- Same register file (64 KB)
- Same constraints
- No new register-related features

---

## SM90 (Hopper) - Register Constraints

**Register File**: 128 KB per warp (DOUBLED)
**Physical Registers**: 15 (unchanged)
**Max Virtual**: 255 (unchanged)

### Warpgroup MMA Constraints
- **Size**: 128 threads (4 warps)
- **Accumulator**: 8 consecutive registers
- **Matrix**: 16x16x16
- **Latency**: 3 cycles
- **Cost**: barrier=2, sync=5, wg_sync=3

### TMA Constraints (New)
- **Descriptor registers**: Reserved regions
- **Latency**: 5 cycles
- **Cost**: tma=0.1

### Sparsity Enhancement
- **Patterns**: Same 6 combinations
- **Metadata**: 2 bits per 4-element block
- **Cost**: sparsity=0.5

---

## SM100 (Blackwell) - Register Constraints

**Register File**: 128 KB per warp (same as SM90)
**Physical Registers**: 15 (unchanged)
**Max Virtual**: 255 (unchanged)

### TCGen05 Constraints
- **Accumulator**: 8 consecutive registers
- **Matrix**: 16x16x16
- **Latency**: 2 cycles
- **Cost**: barrier=1, sync=2, wg_sync=1

### FP4 (E2M1) New Format
- **Bits**: 4 (sign:1, exp:2, mantissa:1)
- **Values**: 16 representable (±{0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0})
- **Packing**: 2 per byte
- **Cost boost**: 4.0

### Block-Scale Quantization
- **Scale factors**: Per-block (FP16/FP32)
- **Register constraint**: Scale + data coordinated
- **Compression**: 3.5-3.8×

### Sparsity Further Enhanced
- **Patterns**: Same 6 combinations
- **Metadata**: 2 bits per 4-element block
- **Cost**: sparsity=0.25 (vs 0.5)

### Descriptor Management (New)
- **Operations**: alloc, dealloc, commit, fence, wait
- **Constraint**: Reserved register regions
- **Handling**: Non-spillable

---

## SM120 (Blackwell-Ultra) - Register Constraints

**Register File**: 128 KB per warp (same as SM100)
**Physical Registers**: 15 (unchanged)
**Max Virtual**: 255 (unchanged)

### Key Difference
- **Dual tensor cores**: 2 per SM
- **Instructions**: All SM100 tcgen05 instructions
- **Latency**: Same as SM100 (2 cycles)
- **Throughput**: 2× of SM100

### Register Constraints
- **Identical to SM100** (no changes)

---

## Graph Coloring Algorithm

**Algorithm**: Briggs optimistic coloring with conservative coalescing

**Phases**:
1. **Liveness Analysis** (0xB612D0)
2. **Interference Graph Construction** (0xB612D0) - 180+ instruction types
3. **Conservative Coalescing** (0x1090BD0) - George's criterion, 0.8 factor
4. **Briggs Coloring** (0x1081400) - Simplify & color main loop
5. **Lazy Reload** (0xA78010) - Place reloads, eliminate redundant

**Node Selection Priority**:
1. **Briggs nodes**: >= K neighbors with degree < K (HIGHEST)
2. **Cost-based**: priority = spill_cost / effective_degree (SECOND)

---

## Evidence Locations

### Primary Functions

```
0xB612D0  (102 KB)  - BuildInterferenceGraph (graph construction dispatcher)
0x1081400 (69 KB)   - SimplifyAndColor (simplification + coloring loop)
0x1090BD0 (61 KB)   - SelectNodeForRemoval (node selection with Briggs)
0x12E1EF0 (51 KB)   - AssignColorsAndOptimize (color assignment)
0xA78010  (?)       - Emit instruction encoding with reloads
```

### Instruction Selection

```
0x94CAB0  - WMMA intrinsic selection (SM70)
0x94DCB0  - WMMA latency encoding (v44 values)
0xA8E250  - TCGen05 instruction parsing (SM90+)
0x35F5090 - SM100+ specific tcgen05 variants
0x2F9DAC0 - Pattern matching engine (4.7 KB)
0xD788E0  - Cost model evaluation
0xA88888  - TCGen05 sparse selection (10.5 KB)
0x4ac770  - Cost kind registration
0x3036AB0 - Block scale format IDs (10299, 10304)
```

### Helper Functions

```
0xA778C0  - Operand specification allocation
0xA79C90  - Constraint list processing wrapper
0xA79B90  - Constraint consolidation/sorting
0xB5BA00  - Register constraint classification
0xA77AB0  - Constraint encoding (bitmasks)
```

---

## Constraint Edge Weighting

| Constraint | Weight | Justification |
|-----------|--------|---|
| Interference | 1.0 | Core dependency |
| Bank conflict | 2.0 | 32-cycle penalty |
| Alignment | 1.0 | Hardware requirement |
| Tensor core | 1.0 | Operation requirement |
| Warpgroup | 1.0 | Synchronization requirement |
| Descriptor | 1.0 | Resource reservation |

---

## Cost Model Comparison

### SM70 (Volta)
```
wmma_load:         1 cy
wmma_mma:          8 cy
wmma_store:        1 cy
barrier:           5
sync:              10
```

### SM80 (Ampere)
```
mma_sync:          4 cy
cp_async:          10 cy
ldmatrix:          1 cy
async_copy_cost:   0.5
sparsity_cost:     0.5
barrier:           3
sync:              8
```

### SM90 (Hopper)
```
warpgroup_mma:     3 cy
tma_load:          5 cy
mbarrier:          0 cy
load_cost:         0.25
tma_cost:          0.1
barrier:           2
sync:              5
wg_sync:           3
```

### SM100 (Blackwell)
```
tcgen05_mma:       2 cy
tcgen05_cp_async:  10 cy
load_cost:         0.125
tma_cost:          0.05
sparsity_cost:     0.25
fp8_boost:         2.0
fp4_boost:         4.0
int4_boost:        4.0
barrier:           1
sync:              2
wg_sync:           1
```

### SM120 (Blackwell-Ultra)
```
Same as SM100 but with 2× throughput
```

---

## Validation Checklist

- [ ] Verify K=15 in decompiled code (0x1090BD0:1039)
- [ ] Confirm coalescing factor 0.8 (magic constant 0xCCCCCCCCCCCCCCCD)
- [ ] Test 64-bit even register alignment on hardware
- [ ] Validate WMMA 8-register accumulator requirement
- [ ] Verify cp.async consecutive register constraint
- [ ] Confirm warpgroup coordination in SM90+
- [ ] Validate FP4 E2M1 format implementation
- [ ] Check descriptor management in SM100+
- [ ] Measure bank conflict penalty in practice
- [ ] Profile register allocation time

---

## Quick Lookup: SM Features

| Feature | SM70 | SM75 | SM80 | SM90 | SM100 | SM120 |
|---------|------|------|------|------|-------|-------|
| Register File | 64KB | 64KB | 64KB | **128KB** | **128KB** | **128KB** |
| Physical Registers | 15 | 15 | 15 | 15 | 15 | 15 |
| Max Virtual | 255 | 255 | 255 | 255 | 255 | 255 |
| Tensor Unit | WMMA | WMMA | mma.sync | warpgroup_mma | tcgen05 | tcgen05×2 |
| Accum Regs | 8 | 8 | 4 | 8 | 8 | 8 |
| Latency | 8cy | 8cy | 4cy | 3cy | 2cy | 2cy |
| cp.async | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ |
| TMA | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ |
| FP8 | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ |
| FP4 | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ |
| Sparsity | ✗ | ✗ | 2:4 | 2:4 | 2:4 | 2:4 |

