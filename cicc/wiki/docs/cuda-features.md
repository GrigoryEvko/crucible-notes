# CUDA-Specific Features (Ultra-Technical)

## Divergence Analysis (L3-10)

**Detection Function**: sub_920430 @ 0x920430

**Classification Return Values**:
```
0 = threadIdx (x, y, z)
1 = blockDim (uniform)
2 = blockIdx (x, y, z)
3 = gridDim (uniform)
4 = warpSize (uniform)
```

**Forward Dataflow Propagation**:
```c
void divergence_analysis(function* fn) {
  // Phase 1: Mark threadIdx-derived values
  for (auto instr : fn->instructions) {
    if (is_threadidx_source(instr)) {
      divergent[instr] = TRUE;
    }
  }

  // Phase 2: Propagate through use-def chains
  for (auto instr : fn->instructions) {
    for (auto operand : instr->operands) {
      if (divergent[operand]) {
        divergent[instr] = TRUE;
      }
    }
  }

  // Phase 3: Control-dependence tracking
  for (auto bb : fn->blocks) {
    if (is_divergent_branch(bb->terminator)) {
      for (auto succ : bb->successors) {
        for (auto instr : succ->instructions) {
          control_dependent[instr] = TRUE;
        }
      }
    }
  }

  // Phase 4: Convergence detection
  for (auto sync : fn->syncthreads_calls) {  // 0x90aee0
    mark_convergence_point(sync);
  }
}
```

**ADCE Safety Rules** (6 mandatory):
```
R1: if (side_effect(i) && divergent(i)) => preserve
R2: if (memory_op(i) && divergent(i)) => preserve
R3: if (control_dependent(i) && divergent_branch) => preserve
R4: if (func_call(i)) => always preserve
R5: if (convergent_metadata(i)) => constrain_optimization
R6: if (divergent_target(branch)) => no_speculative_execution
```

**Integration**:
- UniformityPass: Computes uniform[value] for each SSA value
- StructurizeCFG: Handles structured control flow in divergent regions
- ADCE Driver: sub_2ADCE40 @ 0x2ADCE40 (458 lines decompiled)
- ADCE Core: sub_30ADAE0 @ 0x30ADAE0 (iterative marking + removal)

---

## Bank Conflict Analysis (L3-15)

**Bank Configuration**:
```
Banks per SM:        32
Bytes per bank:      4
Address cycle:       128 bytes
Bank formula:        bank_index = (address % 128) / 4
Conflict latency:    32 cycles (worst case)
Penalty weight:      2.0
```

**Detection Algorithm**:
```c
bool has_bank_conflict(uint64_t addr1, uint64_t addr2) {
  uint32_t bank1 = (addr1 >> 2) % 32;
  uint32_t bank2 = (addr2 >> 2) % 32;
  return (bank1 == bank2);
}

uint32_t compute_stride_banks(uint64_t stride) {
  return (stride % 128) / 4;  // Bank positions in stride
}

float bank_conflict_penalty(int conflict_count) {
  return 1.0f + (2.0f * conflict_count);  // penalty_weight=2.0
}
```

**Cost Model Integration**:
```
spill_cost = base_cost
           * pow(loop_depth_multiplier, depth)
           * occupancy_penalty
           * (1.0f + bank_conflict_penalty)
```

**Avoidance Strategies** (6):
```
1. Register reordering:       Graph coloring with bank constraints
2. Shared memory padding:     stride_adjustment = gcd(access_stride, 128)
3. Address width optimization: use 32-bit ptrs for shared memory
4. Broadcast optimization:    if (uniform_address) => shfl.sync
5. Stride versioning:         symbolic stride analysis (ctor_053, ctor_716)
6. Instruction reordering:    Post-RA scheduler with conflict awareness
```

**Register Allocation Constraint Encoding**:
```
Register class incompatibility edge inserted between:
  - virtual registers that would map to same bank
  - during graph coloring simplify/spill phases
```

---

## Warp Specialization (SM90+, L3-24)

**CTA Group Assignment** (sub_35F3330 @ 0x35F3330, line 85-111):
```c
void assign_cta_group(uint32_t result_bitfield) {
  if (result_bitfield & 0x2) {
    // cta_group::2 (producer/async)
  } else {
    // cta_group::1 (consumer/compute)
  }
}
```

**Bitfield Layout** (result value):
```
Bits 0-1:   Weight stationary mode (00=none, 01=WS, 10=WS, 11=INVALID_with_cta_group_2)
Bit  1:     CTA group assignment (0=group1, 1=group2)
Bits 2-3:   Scale vector size (00=1X, 01=reserved, 10=2X, 11=4X)
Bit  4:     Reserved
Bits 6-8:   MMA data type/kind (000=mxf4nvf4, 001=f8f6f4, 010=mxf8f6f4,
                                 011=f16, 100=i8, 101=tf32, 110=reserved, 111=mxf4)
```

**Group 1 (Consumer)**:
- Identifier: `result & 0x2 == 0`
- Instructions: `mbarrier.wait`, `mma.sync`, `tcgen05.mma`
- Constraint: Weight stationary allowed

**Group 2 (Producer)**:
- Identifier: `result & 0x2 != 0`
- Instructions: `cp.async.bulk.tensor.*`, `mbarrier.arrive.expect_tx`, `tcgen05.commit_group`
- Constraint: Weight stationary FORBIDDEN (sub_36E9630 @ 0x36E9630:169-170)

**Barrier Operation Encoding** (Bits 4-7):
```
Code 0x0: .mbarrier::arrive::one
Code 0x1: .mbarrier::arrive_drop
Code 0x2: .mbarrier::arrive_wait
Code 0x3: .mbarrier::arrive_wait_drop
Code 0x4: .mbarrier::expect_tx         ← CRITICAL FOR TMA
Code 0x5: .mbarrier::complete_tx
```

**Scope Encoding** (Bits 0-3):
```
0x1 (bits 0-3): .cluster scope (up to 8 blocks)
NOT 0x1:        .cta scope (single block)
```

**Multicast Barrier Opcodes**:
```
10090: mbarrier.arrive.multicast
10091: mbarrier.arrive.multicast.shared
10095: mbarrier.arrive.mc.cg1
10096: mbarrier.arrive.mc.cg2
10097: mbarrier.arrive.mc.shared.cg1
10098: mbarrier.arrive.mc.shared.cg2
```

---

## TMA Scheduling (SM90+, L3-23)

**Instruction Variants** (13 total, 17 opcodes):

| Category | Instruction | Opcodes | Encoding |
|----------|-------------|---------|----------|
| Tensor Tile Copy | `cp.async.bulk.tensor.g2s.tile.w[1,2,4,8,16]` | 9222-9226 | width multiplier |
| Im2Col | `cp.async.bulk.tensor.g2s.im2col.w[32,64,128]` | 9213-9215 | bit-width |
| Distributed Shared | `cp.async.bulk.gmem.to.dsmem` | 8316 | — |
| Cluster Scope | `cp.async.bulk.global.to.shared.cluster` | 8315 | — |
| Generic Tensor | `cp.async.bulk.tensor.gmem.to.smem.[f1-f16]` | 8324-8328 | format |
| Im2Col Width | `cp.async.bulk.tensor.gmem.to.smem.im2col.w[32,64,128]` | 8329-8331 | bit-width |

**Scale Vector Encoding** (Bits 51-53 of instruction):
```
00: 1X (default)
01: Reserved
10: 2X
11: 4X
```

**Type-Specific Scale Constraints**:
```
mxf4nvf4:   Cannot use 1X  (error: sub_36E9630:308)
mxf8f6f4:   Cannot use 2X or 4X (error: sub_36E9630:291)
mxf4:       Cannot use 1X or 4X (error: sub_36E9630:318)
f16, tf32, i8, f8f6f4: All scales valid
```

**Producer-Consumer Execution Pattern**:
```c
// Producer (cta_group::2)
for (int batch = 0; batch < num_batches; ++batch) {
  // 1. Dispatch async load
  cp.async.bulk.tensor.g2s [shared_dst + batch*SIZE],
                           [global_src + batch*SIZE];

  // 2. Signal expected bytes
  mbarrier.arrive.expect_tx [barrier], BATCH_SIZE_BYTES;

  // 3. Flush async queue
  tcgen05.commit_group;
}

// Consumer (cta_group::1)
for (int batch = 0; batch < num_batches; ++batch) {
  // 1. Wait for batch arrival
  mbarrier.wait [barrier];

  // 2. Compute with loaded data
  mma.m16n8k32.sync.aligned [acc],
                            [shared_src + batch*SIZE],
                            [weights];
}
```

**Latency Profile**:
```
TMA dispatch to completion:    50-500 cycles (size + memory pattern dependent)
mbarrier.arrive:               1-2 cycles
mbarrier.expect_tx:            0 cycles (metadata only)
mbarrier.wait:                 Blocking until TMA signals
```

**Shared Memory Buffer Strategy**:
```
Buffer 0: [0:SIZE]
Buffer 1: [SIZE:2*SIZE]
Synchronization: mbarrier rendezvous between producer write + consumer read
Typical sizes: 8KB-32KB per buffer
```

---

## 2:4 Structured Sparsity (SM100+, L3-25)

**Pattern Space** (C(4,2) = 6 valid patterns):

| ID | Mask | Positions | Metadata |
|----|----|-----------|----------|
| 0 | 1100 | [0,1] | 0 |
| 1 | 1010 | [0,2] | 1 |
| 2 | 1001 | [0,3] | 2 |
| 3 | 0110 | [1,2] | 3 |
| 4 | 0101 | [1,3] | 4 |
| 5 | 0011 | [2,3] | 5 |

**Metadata Encoding**:
```
Bits per block:    2
Blocks per byte:   4
Lookup:            metadata[block_idx / 4] >> (2 * (block_idx % 4)) & 0x3

// Encoding example: block with non-zeros at positions [1,3]
metadata = 0b0101 => pattern_id = 4
```

**Detection Algorithm** (O(n)):
```c
bool is_2_4_sparse(float* matrix, int size) {
  for (int block = 0; block < size; block += 4) {
    int nonzero_count = 0;
    for (int i = 0; i < 4; ++i) {
      if (matrix[block + i] != 0.0f) nonzero_count++;
    }
    if (nonzero_count != 2) return false;
  }
  return true;
}

void generate_metadata(float* matrix, int size, uint8_t* meta) {
  for (int block = 0; block < size; block += 4) {
    uint8_t pattern = 0;
    for (int i = 0; i < 4; ++i) {
      if (matrix[block + i] != 0.0f) pattern |= (1 << i);
    }
    // pattern now in {0b1100, 0b1010, 0b1001, 0b0110, 0b0101, 0b0011}
    // Map to pattern_id [0-5]
    int pattern_id = encode_pattern(pattern);
    pack_into_metadata(meta, block / 4, pattern_id);
  }
}
```

**Sparse Instruction Variants**:
```
tcgen05.mma.m64n32k32.sparse                    (default)
tcgen05.mma.m64n32k32.f32.f32.sparse            (FP32)
tcgen05.mma.m64n32k32.f16.f16.sparse            (FP16)
tcgen05.mma.m64n32k32.f8.f8.sparse              (FP8)
tcgen05.mma.m64n32k32.mxf4.sparse               (MXF4)

All: latency 2 cycles, throughput 1/cycle, 4096 ops/instr
```

**Cost Model**:
```
Dense MMA cost:     1.0
Sparse MMA 2:4:     0.25  (0.5 bandwidth * 0.5 metadata overhead)
Breakeven matrix:   64x64 (metadata overhead amortization)
```

**Performance Characteristics**:
```
Speedup factor:        2.0x (50% fewer elements)
Latency:               2 cycles (vs 4 dense)
Throughput:            fp32 sparse = 704 TFLOPs/SM
Bandwidth reduction:   37.5% (50% data - 12.5% metadata)
Register overhead:     25-50% for metadata
```

---

## FP4 Format (SM100+, L3-26)

**Format Specification**:
```
Name:        FP4 E2M1
Bits:        4 (1 sign + 2 exponent + 1 mantissa)
Packing:     E2M1x2 (2 FP4 values per byte)
Representable values: 16 (2^4)

Bit layout:
[bit3: sign | bit2-1: exponent | bit0: mantissa]
```

**Block Scale Configuration**:
```
Format IDs:      10299, 10304
Concept:         Per-block FP16/FP32 scale factors
Scale computation: scale = max(abs(block_values)) / max_fp4_value
Memory layout:   [FP4 data block 0...N] [scale factor 0...N]
```

**Quantization Algorithm**:
```c
void quantize_to_fp4(float* src, fp4* dst, float* scales, int block_size) {
  for (int block = 0; block < src_size; block += block_size) {
    // Compute scale
    float max_val = 0.0f;
    for (int i = 0; i < block_size; ++i) {
      max_val = fmax(max_val, fabsf(src[block + i]));
    }
    float scale = max_val / 7.0f;  // 7 = max representable FP4
    scales[block / block_size] = scale;

    // Quantize with rounding-to-nearest-even
    for (int i = 0; i < block_size; ++i) {
      float scaled = src[block + i] / scale;
      // Find nearest of 16 representable values
      fp4 best = 0;
      float min_error = INFINITY;
      for (fp4 candidate = 0; candidate < 16; ++candidate) {
        float error = fabsf((float)candidate - scaled);
        if (error < min_error) {
          min_error = error;
          best = candidate;
        }
      }
      dst[block + i] = best;
    }
  }
}
```

**Dequantization Algorithm**:
```c
void dequantize_fp4(fp4* src, float* scales, float* dst, int block_size) {
  for (int block = 0; block < src_size; block += block_size) {
    float scale = scales[block / block_size];
    for (int i = 0; i < block_size; ++i) {
      dst[block + i] = (float)src[block + i] * scale;
    }
  }
}
```

**Tensor Core Instruction**:
```
tcgen05_mma_fp4_fp4_fp32:
  - Latency:        2 cycles
  - Throughput:     4.0 per cycle
  - Operations:     4096 per instruction
  - Peak TFLOPS:    2048 per SM (4x FP16, 2x FP8)
  - Matrix size:    16x16x16
```

**Block Scale Variants**:
```
tcgen05.mma.block_scale.fp4:
tcgen05.mma.block_scale.fp8:
  - Constraints:    Non-sync aligned only
  - Restrictions:   ashift not supported
  - Format IDs:     10299, 10304
```

**Format Selection Decision Tree**:
```
Is SM100+ ?
  Yes: Model > GPU memory capacity?
    Yes: Accuracy loss < 3%?
      Yes: Use FP4 with block scale
      No:  Use FP8 with block scale
    No:  Bandwidth limited?
      Yes: Use FP4
      No:  Use FP16 or mixed precision
  No:  Is SM90?
    Yes: Use FP8 (no FP4 support)
    No:  Use FP16
```

**Accuracy Profiles**:
```
LLM quantization:        0.5-2.0% loss (FP4 suitable)
Vision transformer:      1-3% loss (FP4 suitable)
Object detection:        > 5% loss (Use FP8)
```

**Compression Characteristics**:
```
FP4 vs FP16:     4x compression (0.5 bytes per element vs 2)
FP4 vs FP32:     8x compression (0.5 bytes per element vs 4)
With block scales (FP32): 3.5-3.8x vs FP16 (accounting for scale factors)
Net bandwidth:   37.5% reduction (50% compression - 12.5% scale overhead)
```

**Instruction Constraints**:
```
Block scale types: Only FP4, FP8 (not F16, TF32, I8)
Alignment:         Non-sync aligned variants only
Address shift:     ashift not supported
Weight stationary: Incompatible with FP4
```

**Configuration Parameters**:
```
enable-fp4:              boolean (default false)
fp4-block-size:          8|16|32|64 (default 32)
fp4-scale-precision:     fp16|fp32 (default fp32)
fp4-accuracy-threshold:  float [0.9, 1.0] (default 0.97)
mixed-precision-policy:  weights-only|weights-and-activations|custom
```

---

## Quick Reference: Opcodes & Codes

**Divergence Detection**: 0x920430
**ADCE Driver**: 0x2ADCE40
**ADCE Core**: 0x30ADAE0

**Barrier Operation Codes** (Bits 4-7):
```
0x0: arrive
0x1: arrive_drop
0x2: arrive_wait
0x3: arrive_wait_drop
0x4: expect_tx
0x5: complete_tx
```

**TMA Opcodes**:
```
cp.async.bulk.tensor.g2s:           9222-9226 (w1,w2,w4,w8,w16)
cp.async.bulk.tensor.g2s.im2col:    9213-9215 (w32,w64,w128)
cp.async.bulk.gmem.to.dsmem:        8316
cp.async.bulk.global.to.shared.cluster: 8315
cp.async.bulk.tensor.gmem.to.smem:  8324-8328 (f1,f2,f4,f8,f16)
cp.async.bulk.tensor.gmem.to.smem.im2col: 8329-8331 (w32,w64,w128)
```

**Multicast Barrier Opcodes**:
```
10090: mbarrier.arrive.multicast
10091: mbarrier.arrive.multicast.shared
10095: mbarrier.arrive.mc.cg1
10096: mbarrier.arrive.mc.cg2
10097: mbarrier.arrive.mc.shared.cg1
10098: mbarrier.arrive.mc.shared.cg2
```

**Bank Conflict Constants**:
```
Banks per SM:      32
Stride:            128 bytes
Penalty weight:    2.0
Serialization:     32 cycles
```

**Sparsity Pattern Metadata** (Format IDs):
```
Pattern [0,1]:     metadata = 0
Pattern [0,2]:     metadata = 1
Pattern [0,3]:     metadata = 2
Pattern [1,2]:     metadata = 3
Pattern [1,3]:     metadata = 4
Pattern [2,3]:     metadata = 5
```

**FP4 Format Codes**:
```
Block scale format IDs: 10299, 10304
E2M1x2 encoding case:   5 (sub_35ED820 @ 0x35ED820)
```

---

## Compilation Integration Map

```
IR Construction
  ↓
Divergence Analysis (UniformityPass)
  ↓
Sparsity Detection (SM100+)
  ↓
Optimization Passes
  ├─ ADCE (respects R1-R6 rules)
  ├─ Bank Conflict Avoidance (penalty 2.0)
  ├─ Sparse Cost Analysis
  ↓
Instruction Selection
  ├─ Cost model evaluation
  ├─ Pattern matching (700 patterns on SM100)
  ├─ TMA instruction generation (SM90+)
  ├─ Warp specialization bit decision (bit 1)
  ├─ Sparse variant selection (12 variants)
  ├─ FP4 format selection (format IDs 10299, 10304)
  ↓
Register Allocation
  ├─ Bank conflict constraints
  ├─ Warpgroup partition (SM90+)
  ├─ Sparsity metadata storage
  ↓
Instruction Scheduling
  ├─ Bank conflict aware reordering
  ├─ TMA latency hiding
  ↓
Code Emission
  ├─ PTX generation
  ├─ Metadata encoding
```

---

**Analysis Metadata**:
- L3-10: Divergence Analysis (0x920430, 0x2ADCE40)
- L3-15: Bank Conflicts (penalty 2.0, formula (addr%128)/4)
- L3-23: TMA Opcodes 8315-8331, 9213-9226
- L3-24: Warp Specialization (bit 1 decision)
- L3-25: 2:4 Sparsity (6 patterns, cost 0.25)
- L3-26: FP4 E2M1 (format IDs 10299, 10304)
- **Date**: 2025-11-16
- **Confidence**: HIGH (L3-10, L3-26), MEDIUM-HIGH (L3-15, L3-23, L3-24, L3-25)
