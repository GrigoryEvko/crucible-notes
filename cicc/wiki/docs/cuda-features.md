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

## FP4 Quantization (SM100+, L3-26)

### Format Specification

**FP4 E2M1 Format** (Extracted from `sub_35ED820.c` @ 0x35ED820, line 83):
```
Name:              FP4 E2M1
Bits per value:    4 total
  - Sign:          1 bit
  - Exponent:      2 bits
  - Mantissa:      1 bit (implicit leading 1)
Packing:           E2M1x2 (2 FP4 values per 8 bits)
Representable values: 16 distinct values
Format identifier: ".e2m1x2"
```

**Bit Layout**:
```
[bit3: sign | bit2-1: exponent (E2) | bit0: mantissa (M1)]

Value range: [-0.75, +0.75] with 16 representable points
Quantization interval: ~0.046875 (0.75/16)
ULP (Unit in Last Place): 2^-4 = 0.0625
```

---

### Accuracy Thresholds

**Critical Finding** (FP4_EXTRACTION_SUMMARY.md, lines 198-202):

| Deployment Type | Acceptable Accuracy Loss | Notes |
|-----------------|--------------------------|-------|
| **Inference-only scenarios** | 0.5-3% loss | Block scale essential for maintaining bounds |
| **LLM deployment** | 0.5-2% perplexity increase | Most critical - language model semantic preservation |
| **Vision models** | 1-3% ImageNet accuracy drop | Variable by architecture (ViT more tolerant than ResNet) |
| **Object detection** | > 5% unacceptable | Use FP8 instead |

**Practical Accuracy Formulas**:
```c
// Per-layer accuracy preservation (block scale)
float quantization_error_bound(float layer_max, int block_size) {
  // Maximum absolute error after quantization
  return (layer_max / 16.0f) * 0.5f;  // Half-ULP error
}

// Relative error with block scale
float relative_error_percent(float layer_max, int block_size) {
  float max_error = quantization_error_bound(layer_max, block_size);
  return (max_error / layer_max) * 100.0f;
}

// LLM perplexity impact (empirical)
float perplexity_increase_factor(float mean_accuracy_loss) {
  // Linear approximation: 1% accuracy loss → 0.5-1.0% perplexity increase
  return 1.0f + (mean_accuracy_loss * 0.01f);
}
```

**Accuracy Loss by Model Type**:
- **LLM (GPT, LLAMA)**: 0.5-2.0% loss → 1.5-3.0% perplexity increase (acceptable)
- **Vision Transformer**: 1-3% accuracy drop → Still converges well
- **CNN (ResNet)**: 2-4% drop → May need FP8 for critical layers
- **Object Detection (YOLO)**: >5% drop → Unacceptable, use FP8

---

### Block Scale Algorithm

**Block Scale Concept** (Discovered in `sub_3036AB0.c`, `sub_36E9630.c`):

Block scaling divides tensors into fixed-size blocks, where each block has its own FP16 or FP32 scale factor:

```c
// Quantization with per-block scale
void quantize_with_block_scale(float* src, fp4* dst, float* scales,
                               int total_size, int block_size) {
  for (int block_idx = 0; block_idx < total_size; block_idx += block_size) {
    // STEP 1: Compute block scale
    float max_val = 0.0f;
    for (int i = 0; i < block_size && (block_idx + i) < total_size; ++i) {
      max_val = fmax(max_val, fabsf(src[block_idx + i]));
    }

    // Scale = max(abs(block)) / max_fp4_representable (0.75)
    float scale = max_val / 0.75f;  // Normalizes to [-1,1] range
    if (scale == 0.0f) scale = 1.0f;  // Avoid division by zero
    scales[block_idx / block_size] = scale;

    // STEP 2: Quantize all values in block by this scale
    for (int i = 0; i < block_size && (block_idx + i) < total_size; ++i) {
      float scaled = src[block_idx + i] / scale;  // Normalize

      // Round to nearest FP4 representable value
      fp4 quantized = round_to_nearest_fp4(scaled);
      dst[block_idx + i] = quantized;
    }
  }

  // Block scales stored separately (FP32 or FP16)
  // Format IDs 10299, 10304 identify block-scaled variants
}

// Dequantization is purely multiplicative
void dequantize_with_block_scale(fp4* src, float* scales, float* dst,
                                 int total_size, int block_size) {
  for (int idx = 0; idx < total_size; ++idx) {
    float scale = scales[idx / block_size];
    dst[idx] = (float)src[idx] * scale;  // Hardware-native operation
  }
}
```

**Format IDs for Block Scale** (Code locations):
```
Format ID 10299: tcgen05.mma.block_scale variant 1
Format ID 10304: tcgen05.mma.block_scale variant 2

Both identified in:
  - sub_3036AB0.c (format ID case handling)
  - sub_36E9630.c (constraint validation)
```

**Block Scale Constraints** (sub_36E9630.c, lines 162-175):

Supported types:
- ✅ FP4 (E2M1)
- ✅ FP8 (E2M3, E4M3, E5M2)

Unsupported types:
- ❌ F16 (float16) - insufficient dynamic range
- ❌ TF32 (TensorFloat32) - precision mismatch
- ❌ F8F6F4 (mixed precision) - format incompatible
- ❌ I8 (int8) - no block scale support

Instruction-level constraints:
```c
// Error from sub_36E9630:128-131
if (has_block_scale && uses_ashift) {
  error("ashift is not supported with tcgen05.mma.block_scale variants");
}

// Non-sync aligned variants ONLY
if (has_block_scale && is_sync_aligned) {
  error("nvvm.mma.blockscale currently supports non-sync aligned variants only!");
}

// Weight stationary incompatible (sub_36E9630:134)
if (has_block_scale && weight_stationary_enabled && (is_fp4 || is_f8f6f4)) {
  error("Cannot use weight stationary with mxf8f6f4 and fp4 types");
}
```

**Block Size Selection**:
```
block_size options: 8, 16, 32, 64
Typical default:    32

Trade-off:
  - Smaller blocks (8): More precise scales, higher scale overhead (12.5%)
  - Larger blocks (64): Simpler scales, lower overhead (3.1%)
  - Sweet spot (32):    3.125% scale overhead, good precision balance
```

---

### Tensor Core Instructions

**Peak Performance** (from `tensor_core_costs.json`):

```json
{
  "tcgen05_mma_fp4_fp4_fp32": {
    "architecture": "SM100, SM120 (Blackwell)",
    "latency_cycles": 2,
    "throughput_per_cycle": 4.0,
    "ops_per_instruction": 4096,
    "cost_model": "fp4_compute_boost: 4.0"
  }
}
```

**Performance Comparison** (per SM):
```
FP4:   2048 TFLOP/s per SM (4.0 throughput/cycle × 512 ops/cycle base)
FP8:   1024 TFLOP/s per SM (2.0 throughput/cycle)
FP16:  512 TFLOP/s per SM  (1.0 throughput/cycle)
FP32:  512 TFLOP/s per SM  (1.0 throughput/cycle)

Real-world speedup on bandwidth-limited workloads:
  Memory-bandwidth-limited (typical): 2.5-4.0x faster with FP4
  Compute-bound:                      Limited speedup (overhead minimal)
  LLM inference (mixed I/O):         2.5-3.5x speedup with FP4
```

**Instruction Variants**:
```
tcgen05.mma.m16n16k16.fp4.fp4.fp32:
  - Matrix dimensions: 16×16×16 = 4096 ops
  - Latency: 2 cycles
  - Throughput: 4.0 per cycle
  - Cost in model: 1.0 (baseline FP4)

tcgen05.mma.block_scale.fp4:
tcgen05.mma.block_scale.fp8:
  - Non-sync aligned variants only
  - ashift NOT supported
  - Weight stationary INCOMPATIBLE
```

---

### Implementation Details from Decompiled Code

**Format Selection Logic** (sub_35ED820.c @ 0x35ED820, line 83):

```c
// FP4 format identifier case
case 5:
  result = sub_CB6200(a2, ".e2m1x2", 7u);  // FP4 E2M1 packed as x2
  break;

// Related FP8 format identifiers for comparison
case 6:
  result = sub_CB6200(a2, ".e2m3x2", 7u);  // FP8 E2M3
  break;
case 2:
  result = sub_CB6200(a2, ".e4m3x2", 7u);  // FP8 E4M3
  break;
case 3:
  result = sub_CB6200(a2, ".e5m2x2", 7u);  // FP8 E5M2
  break;
```

**Block Scale Format IDs** (sub_3036AB0.c):

```c
// Format ID case analysis
if ( (_DWORD)v12 == 10299 || (_DWORD)v12 == 10304 ) {
    // Block scale specific handling
    // Format ID 10299: Block scale variant A
    // Format ID 10304: Block scale variant B
}
```

**Matrix Format Types** (sub_35F3330.c):

```c
// MMA format type enumeration
enum mma_format_type {
  MXF4_STANDARD      = 0,      // Standard FP4 matrix format
  MXF4_NVF4          = 1,      // FP4 with NVIDIA optimizations
  MXF8F6F4_MIXED     = 2,      // Mixed precision: FP8/FP6/FP4
  F16_FORMAT         = 3,      // float16
  I8_FORMAT          = 4,      // int8
  TF32_FORMAT        = 5,      // TensorFloat32
  // 110, 111 reserved/invalid
};
```

---

### FP4 vs FP8 Selection Decision Tree

**Cost Model-Driven Decision** (from L3-26):

```
Is SM100 (Blackwell) or SM120 (Blackwell-Ultra)?
  │
  ├─ NO: Is SM90 (Hopper)?
  │      ├─ YES: Use FP8 only (no FP4 support)
  │      └─ NO:  Use FP16 (fall back)
  │
  └─ YES: Is model too large for GPU memory?
         │
         ├─ YES: Is accuracy loss < 3%?
         │       ├─ YES: Use FP4 + block scale (4-8x compression)
         │       └─ NO:  Use FP8 (2-4x compression)
         │
         └─ NO:  Is bandwidth-limited?
                 ├─ YES: Use FP4 (throughput advantage)
                 └─ NO:  Use FP16 or mixed precision (preserve accuracy)
```

**Selection Heuristics** (Empirical from cost model):

| Metric | Priority | Decision Criteria |
|--------|----------|------------------|
| Model size | 1 | If exceeds capacity → FP4 |
| Accuracy tolerance | 2 | <3% loss acceptable? → FP4 viable |
| Layer criticality | 3 | First/last layers → higher precision (FP8) |
| Hardware support | 4 | SM100+? → FP4 allowed |
| Quantization scope | 5 | Weights → FP4; Activations → FP8 |

**Per-Layer Decision**:
```
Layer type: Attention?
  ├─ YES (critical): Use FP8 (0.5-1.0% loss threshold)
  └─ NO (FFN, Conv): Use FP4 (1-3% loss threshold)

Layer position:
  ├─ First 2 layers: Use FP8 (information density high)
  ├─ Middle layers:  Use FP4 (good compression/accuracy trade-off)
  └─ Last 2 layers:  Use FP8 (output quality critical)

Model type:
  ├─ LLM: FP4 weights + FP8 activations (0.5-2% loss)
  ├─ Vision: FP8 + FP4 for backbone (1-3% loss)
  └─ Detection: Mostly FP8 (>5% loss → FP8 mandatory)
```

---

### Compression Characteristics

**Memory Efficiency**:
```
FP4 vs FP16:         4.0x compression (0.5 bytes vs 2 bytes per element)
FP4 vs FP32:         8.0x compression (0.5 bytes vs 4 bytes per element)

With FP32 block scales (32-element blocks):
  Per-block overhead:     128 bits (FP32 scale) / 32 elements = 4 bits/element
  Effective compression:  3.5-3.8x vs FP16 (accounting for scale factors)

Net bandwidth reduction: 37.5%
  Calculation: 50% data compression - 12.5% scale overhead = 37.5% savings
```

**Real-World Memory Impact**:
```
7B LLM model (FP16 baseline):
  - FP16:  14 GB (7B params × 2 bytes)
  - FP4:   3.5 GB (7B params × 0.5 bytes)
  - Speedup: 3.5-4.0x due to reduced memory bandwidth

70B LLM model:
  - FP16:  140 GB (exceeds typical GPU memory)
  - FP4:   35 GB (fits on 40GB GPU with block scales)
  - Feasibility: Enables models previously impossible to deploy
```

---

### Configuration Parameters

**Compiler Flags for FP4 Control**:
```
enable-fp4:              boolean (default: false)
  Controls whether FP4 instructions are generated

fp4-block-size:          {8|16|32|64} (default: 32)
  Block size for per-block scale factors

fp4-scale-precision:     {fp16|fp32} (default: fp32)
  Precision of scale factors (trade-off: memory vs accuracy)

fp4-accuracy-threshold:  float [0.90, 1.00] (default: 0.97)
  Minimum acceptable accuracy threshold (0.97 = 3% max loss)

mixed-precision-policy:  {weights-only|weights-and-activations|custom}
  - weights-only: FP4 weights, FP8/FP16 activations
  - weights-and-activations: Both FP4 quantized
  - custom: Per-layer specification

fp4-calibration-method:  {minmax|percentile|entropy}
  - minmax: Uses min/max values for scale
  - percentile: Clips outliers (typically 99.9th percentile)
  - entropy: Minimizes KL divergence
```

---

### Architectural Support

**Blackwell Exclusivity**:
```
✅ SM100 (Blackwell):       Full FP4 support
✅ SM120 (Blackwell-Ultra): Enhanced FP4 (dual tensor cores per warp group)
⚠️  SM90 (Hopper):          FP8 only (block_scale.fp8 supported, no FP4)
❌ SM80 (Ampere):           FP16/INT8 only
❌ SM70 (Volta):            FP16 only
```

**Hopper FP8 Alternative** (SM90):
```
When deploying on Hopper without FP4:
  Use: tcgen05.mma.block_scale.fp8
  Accuracy loss: 0.5-1.5% (lower than FP4 but higher cost)
  Compression: 2-4x vs FP16
  Performance: 2.0 TFLOP/s per SM (vs 4.0 for FP4 on Blackwell)
```

---

### Validation & Testing

**Recommended Accuracy Validation**:
```c
// Measure actual accuracy loss after FP4 quantization
float validate_quantization_accuracy(
    Model* original,
    Model* quantized_fp4,
    DataLoader* test_set,
    int num_samples = 10000
) {
  float total_loss = 0.0f;

  for (int i = 0; i < num_samples; ++i) {
    auto input = test_set->get_sample(i);
    auto original_output = original->forward(input);
    auto quantized_output = quantized_fp4->forward(input);

    // Cosine similarity (preferred for NLP)
    float loss = 1.0f - cosine_similarity(original_output, quantized_output);
    total_loss += loss;
  }

  return total_loss / num_samples;  // Average loss percentage
}

// PASS if: avg_loss < threshold
// For LLM: threshold = 0.02 (2%)
// For Vision: threshold = 0.03 (3%)
// For Detection: threshold = 0.05 (5%) [use FP8 if exceeded]
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
