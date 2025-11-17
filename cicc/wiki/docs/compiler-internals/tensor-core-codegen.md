# Tensor Core Code Generation

CICC tensor core instruction selection over SM generations (SM70-SM120).
Latency progression: 8 → 4 → 3 → 2 cycles.
Instruction variant counts: SM70 (67) → SM80 (40+) → SM90 (expanded FP8 paths) → SM100 (tcgen05 with FP4/sparsity).

---

## SM70 (Volta) - WMMA

Warp-level tensor core unit. 32 threads per warp. MMA latency: 8 cycles.

**Instruction format**: `wmma.{load|mma|store}.{a|b|d}.sync.{layout}.{type}.m{M}n{N}k{K}`

**67 instruction variants** across precision and matrix shape combinations.

**Load/Store operations**:
- `wmma.load.a.sync.row.f16.m16n16k16`: 1 cycle latency, 256 bytes → 8 registers per thread
- `wmma.load.b.sync.col.f16.m16n16k16`: 1 cycle latency, 256 bytes → 8 registers per thread
- `wmma.store.d.sync.row.f16.m16n16k16`: 1 cycle latency, 8 registers → 256 bytes

**Compute instructions** (8-cycle latency each):
- `wmma.mma.sync.m16n16k16.row.col.f16.f16.f16.f32`: Input FP16, accum FP32, output FP32. 256 operations.
- `wmma.mma.sync.m16n16k16.row.col.f16.f16.f16.f16`: Input FP16, output FP16. 256 operations.
- `wmma.mma.sync.m16n16k16.row.col.f32.f32.f32.f32`: Input FP32, output FP32. 64 operations.
- `wmma.mma.sync.m16n16k16.row.col.s8.s8.s32.s32`: Input INT8, output INT32. 256 operations.

**Accumulator operations**:
- `wmma.fill.f32 reg[0:7]`: Initialize 8x FP32 values per thread. 1 cycle.

**Matrix shapes**: 16x16x16 only.

**Cost model** (0x94cab0, 0x94dcb0):
```c
latency[wmma_load] = 1;
latency[wmma_mma_f16] = 8;
latency[wmma_mma_f32] = 8;
latency[wmma_store] = 1;
barrier_cost = 5;  // __shared__ coherence overhead
sync_cost = 10;    // inter-warp synchronization
```

**Instruction selection evidence**: 0x94cab0 (dword_3F147A0, dword_3F147E0, dword_3F14840 lookup tables)

---

## SM80 (Ampere) - MMA.SYNC

Warp-level MMA unit with async copy. 32 threads per warp. MMA latency: 4 cycles (50% vs SM70).

**Instruction format**: `mma.sync.aligned.m{M}n{N}k{K}.{layout_a}.{layout_b}.{dtype_d}.{dtype_a}.{dtype_b}.{dtype_c}`

**40+ instruction variants** by precision and matrix shape. Primary shapes: m16n8k16, m8n32k16.

**MMA compute** (4-cycle latency):
- `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32`: 256 FP16 ops, output FP32
- `mma.sync.aligned.m16n8k16.row.col.f32.tf32.tf32.f32`: 256 TF32 ops, output FP32
- `mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32`: 256 BF16 ops, output FP32
- `mma.sync.aligned.m16n8k16.row.col.s32.s8.s8.s32`: 256 INT8 ops, output INT32

**Memory operations**:
- `ldmatrix.sync.aligned.m8n8.x4.f16 [R0-R3], [shared_addr]`: 1 cycle, 128 bytes → 4 registers (transpose integrated)
- `cp.async.cg [shared_dst], [global_src], 16`: 10 cycle latency, 4.0 bytes/cycle throughput (2.0x vs SM70)

**2:4 Structured Sparsity** (SM80+):
- Pattern: exactly 2 non-zeros per 4-element block
- 6 valid patterns: C(4,2) = 6
- Metadata: 2 bits per 4-element block
- Instruction: `mma.sync.aligned.m16n8k16.sparse.*`
- Cost reduction: 0.5x (2x speedup)

Sparse metadata encoding (0x2F9DAC0):
```c
// For each 4-element block [a,b,c,d]:
uint8_t metadata = 0;
if (a != 0) metadata |= 0x1;
if (b != 0) metadata |= 0x2;
if (c != 0) metadata |= 0x4;
if (d != 0) metadata |= 0x8;
// Only valid if popcount(metadata) == 2
// 6 valid patterns map to metadata values 0-5
```

**Cost model** (0xD788E0, 0xFDE760):
```c
latency[mma_sync] = 4;
latency[cp_async] = 10;
latency[ldmatrix] = 1;
async_copy_cost = 0.5;  // overlaps with compute
sparsity_cost = 0.5;    // 2x throughput from metadata
memory_barrier_cost = 3;
sync_cost = 8;
```

---

## SM90 (Hopper) - Warpgroup MMA + TMA

Warpgroup-level operations (128 threads = 4 warps). MMA latency: 3 cycles (25% vs SM80).

**Instruction format**: `warpgroup.mma.sync.m{M}n{N}k{K}.{layout_a}.{layout_b}.{dtype_d}.{dtype_a}.{dtype_b}.{dtype_c}`

**Warpgroup scope**: 128 threads (0-127) coordinate single MMA operation. Output dimensions larger than SM80 (m16n16k16 becomes single op vs multiple SM80 operations).

**MMA compute** (3-cycle latency):
- `warpgroup.mma.sync.m16n16k16.row.col.f32.f16.f16.f32`: 512 FP16 ops per instruction, 0.5 throughput/cycle (2 MMAs per 2 cycles)
- `warpgroup.mma.sync.m16n16k16.row.col.f32.f8.f8.f32`: 1024 FP8 ops per instruction, 1.0 throughput/cycle
- `warpgroup.mma.sync.m16n16k16.row.col.f32.tf32.tf32.f32`: 128 TF32 ops

**TMA operations** (Tensor Memory Accelerator):
- `cp.async.bulk.tensor.g2s [shared_dst], [global_src], descriptor`: 5 cycle latency (variablecount), 4.0 bytes/cycle throughput
- Format conversion hardware: FP32→FP8, FP32→FP4, transposition during transfer
- Implicit byte counting via `mbarrier.arrive.expect_tx [barrier], bytes`

**Barrier synchronization**:
- `mbarrier.arrive.expect_tx [barrier], bytes_to_transfer`: Atomic increment + expected byte encoding. 0 cycle latency.
- `mbarrier.wait [barrier]`: Blocking wait for producer. Atomic load + spin.
- Scope: CTA or cluster (8 CTAs)

**2:4 Sparsity** (SM90 variant):
- Pattern same as SM80: 2 non-zeros per 4-element block
- Instruction: `warpgroup.mma.sync.sparse.*`
- Metadata: 2 bits per 4-element block
- Cost reduction: 0.5x

**Cost model** (0xa8e250):
```c
latency[warpgroup_mma] = 3;
latency[tma_load] = 5;
latency[mbarrier_arrive] = 0;
latency[mbarrier_wait] = variable;  // depends on producer
load_cost = 0.25;  // TMA amortized
tma_cost = 0.1;    // high overlap
fp8_boost = 2.0;   // 1024 ops vs 512 for FP16
memory_barrier_cost = 2;
sync_cost = 5;
warpgroup_sync_cost = 3;
```

---

## SM100/SM120 (Blackwell) - TCGen05

Warpgroup-level operations (128 threads). MMA latency: 2 cycles (50% vs Hopper).

**Instruction format**: `tcgen05.mma.{m64n32k32|...}.{layout}.{dtype_d}.{dtype_a}.{dtype_b}.{dtype_c}`

**FP4 E2M1 Format** (Blackwell exclusive):

Bit layout (4 bits per value):
```
[bit3: sign] [bit2-1: exponent(2)] [bit0: mantissa(1)]
```

Exponent range: 0-3 (bias=1, effective exponent range: -1 to +2)
Mantissa: implicit leading 1 when exponent != 0
Representable values: 16 distinct (8 positive, 8 negative, including ±0)

```c
// FP4 quantization (scale-and-round)
float quantize_fp4(float x, float scale) {
    float normalized = x / scale;
    // Round to nearest of 8 FP4 positive values:
    // {0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0}
    fp4_t result = round_to_nearest_fp4(normalized);
    return result;
}

// FP4 dequantization (multiply)
float dequantize_fp4(fp4_t val, float scale) {
    return ((float)val) * scale;
}
```

Packing: 2 FP4 values per byte (E2M1x2 format)

**Block-Scaled Quantization**:

Format IDs: 10299, 10304 (decompiled 0x3036ab0)

```c
void quantize_block_scale_fp4(float* block, int block_size,
                              fp4_t* output, float* scales) {
    // Step 1: Compute per-block scale factor
    float max_val = max_abs_in_block(block, block_size);
    float scale = max_val / max_representable_fp4;  // ~6.0

    // Step 2: Quantize all values in block
    for (int i = 0; i < block_size; i++) {
        output[i] = quantize_fp4(block[i], scale);
    }

    // Step 3: Store scale (FP16 or FP32)
    *scales = scale;
}
```

Compression: FP4 (0.5 bytes/value) + block scale (4 bytes per ~32-64 values) = ~3.5-3.8x vs FP32

**2:4 Structured Sparsity** (SM100+):

Pattern specification (6 valid combinations):
```
Block size: 4 elements
Valid patterns (exactly 2 non-zeros):
  [0,1] → metadata = 0, mask = 0b1100
  [0,2] → metadata = 1, mask = 0b1010
  [0,3] → metadata = 2, mask = 0b1001
  [1,2] → metadata = 3, mask = 0b0110
  [1,3] → metadata = 4, mask = 0b0101
  [2,3] → metadata = 5, mask = 0b0011
```

Metadata encoding (2 bits per 4-element block):
```c
uint8_t encode_24_pattern(float* block) {
    int nz_positions[2] = {-1, -1};
    int count = 0;
    for (int i = 0; i < 4; i++) {
        if (block[i] != 0.0f) {
            nz_positions[count++] = i;
        }
    }
    if (count != 2) return INVALID;
    // Map pair of indices to metadata (0-5)
    int pairs[6] = {0b00, 0b01, 0b10, 0b11, 0b100, 0b101};
    return pairs[index_from_positions(nz_positions)];
}
```

Sparsity overhead: 0.5 bits per element (2 bits per 4 elements)
Cost reduction: 0.25x (dense cost vs sparse cost with metadata overhead)
Speedup: 2x (50% fewer computations)

**Instruction variants** (50+ tcgen05 variants):

MMA compute (2-cycle latency):
- `tcgen05.mma.m64n32k32.f32.f8.f8.f32`: 2048 FP8 ops, 2.0 throughput/cycle
- `tcgen05.mma.m64n32k32.f32.fp4.fp4.f32`: 4096 FP4 ops, 4.0 throughput/cycle (4x vs FP16)
- `tcgen05.mma.m64n32k32.s32.s8.s8.s32`: 2048 INT8 ops, 2.0 throughput/cycle
- `tcgen05.mma.m64n32k32.s32.s4.s4.s32`: 4096 INT4 ops, 4.0 throughput/cycle
- `tcgen05.mma.sparse.m64n32k32.f32.fp4.fp4.f32`: Sparse FP4 with metadata (0xa88888, 10.5KB function)
- `tcgen05.mma.block_scale.m64n32k32.f32.fp8.fp8.f32`: Block-scaled FP8

Descriptor operations (SM100+ only):
- `tcgen05.alloc [descriptor_id]`: 1 cycle, allocate matrix descriptor
- `tcgen05.dealloc [descriptor_id]`: 1 cycle, deallocate descriptor
- `tcgen05.commit [descriptor_id]`: 0 cycle latency, multi-cast commit (16/32-bit mask)
- `tcgen05.fence`: 0 cycle, memory fence for tensor operations
- `tcgen05.wait [descriptor_id]`: 0 cycle, wait for prior MMA completion

Async copy (TMA):
- `tcgen05.cp.async [shared_dst], [global_src], descriptor`: 10 cycle latency, 4.0 bytes/cycle

**Cost model** (0x35f5090, 0x4ac770):
```c
latency[tcgen05_mma] = 2;
latency[tcgen05_mma_fp8] = 2;
latency[tcgen05_mma_fp4] = 2;
latency[tcgen05_cp_async] = 10;

throughput[fp8] = 2.0;  // ops per cycle
throughput[fp4] = 4.0;  // ops per cycle
throughput[int8] = 2.0;
throughput[int4] = 4.0;

compute_boost[fp8] = 2.0;  // vs FP16
compute_boost[fp4] = 4.0;  // vs FP16
compute_boost[int4] = 4.0;

load_cost = 0.125;   // TMA amortized
store_cost = 0.125;
tma_cost = 0.05;     // extreme overlap
sparsity_cost = 0.25;  // 2x speedup - 25% metadata overhead
memory_barrier_cost = 1;
sync_cost = 2;
```

**SM120 (Blackwell-Ultra)**: Dual tensor cores per SM. All SM100 instructions with 2x throughput option.

---

## Instruction Selection Algorithm

**Pattern database** (SM100): 700 patterns total, 50+ tcgen05 variants

**Selection priority** (tensor_core_costs.json):
1. Check SM version → select unit (WMMA | MMASync | WarpgroupMMA | TCGen05)
2. Check sparsity: is matrix 2:4 sparse? Cost model: sparse_cost = 0.5 (SM80/90) or 0.25 (SM100)
3. Select precision: cost[FP4] < cost[FP8] < cost[INT8] < cost[FP16] ?
4. Final instruction selection via hash-table lookup in pattern database

```c
// Instruction selection pseudocode (0xa8e250)
PTXInstruction* select_tensor_instruction(TensorOp* op, int sm_version) {
    // Step 1: Unit selection
    if (sm_version >= 100) {
        return select_tcgen05_instruction(op);
    } else if (sm_version >= 90) {
        return select_warpgroup_mma_instruction(op);
    } else if (sm_version >= 80) {
        return select_mma_sync_instruction(op);
    } else {
        return select_wmma_instruction(op);
    }
}

// Step 2: Sparsity detection (SM80+)
bool is_2_4_sparse(float* matrix, int size) {
    for (int block = 0; block < size; block += 4) {
        int nonzero_count = 0;
        for (int i = 0; i < 4; i++) {
            if (matrix[block + i] != 0.0f) nonzero_count++;
        }
        if (nonzero_count != 2) return false;
    }
    return true;
}

// Step 3: Cost-driven precision selection
float evaluate_cost(TensorOp* op, int precision) {
    float compute_cost = latency[op_type][precision] * ops_count[precision];
    float mem_cost = bandwidth_bytes[precision] / memory_bandwidth;
    return compute_cost + mem_cost;
}

int select_precision(TensorOp* op) {
    int best_precision = FP32;
    float best_cost = evaluate_cost(op, FP32);

    // Try lower precisions in order
    int precisions[] = {FP4, FP8, INT8, FP16, TF32, BF16};
    for (int p : precisions) {
        float cost = evaluate_cost(op, p);
        if (cost < best_cost) {
            best_cost = cost;
            best_precision = p;
        }
    }
    return best_precision;
}
```

---

## Latency Comparison Table

| SM Gen | Unit | MMA Latency | Load Latency | Variants | Peak (FP16) |
|--------|------|-------------|--------------|----------|------------|
| 70 | WMMA | 8 cycles | 1 cycle | 67 | 62.5 TF |
| 80 | MMASync | 4 cycles | 1 cycle | 40+ | 312 TF |
| 90 | WarpgroupMMA | 3 cycles | 5 cycles (TMA) | expanded | 989 TF |
| 100 | TCGen05 | 2 cycles | 10 cycles | 50+ tcgen05 | 2 PF |
| 120 | TCGen05 dual | 2 cycles | 10 cycles | 50+ tcgen05 | 4 PF |

---

## Format Summary

**FP4 E2M1 representable values** (16 values):
```
±[0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]
```

**2:4 Sparsity patterns** (6 valid):
```
Pattern metadata encoding (2 bits):
  00 → positions [0,1]
  01 → positions [0,2]
  10 → positions [0,3]
  11 → positions [1,2]
  100 → positions [1,3]
  101 → positions [2,3]
```

**FP8 variants**:
- E4M3: 4-bit exponent, 3-bit mantissa (SM90+)
- E5M2: 5-bit exponent, 2-bit mantissa (SM90+)

---

## Evidence Locations

- 0x94cab0: WMMA intrinsic selection (SM70 path)
- 0x94dcb0: WMMA latency encoding (v44 values: 2,4,8)
- 0xa8e250: TCGen05 instruction parsing
- 0x35f5090: SM100+ specific tcgen05 variants
- 0x2F9DAC0: Pattern matching engine (4.7KB)
- 0xD788E0: Cost model evaluation (231 calls)
- 0xA88888: TCGen05 sparse selection (10.5KB)
- 0x4ac770: Cost kind registration
- 0x3036ab0: Block scale format IDs (10299, 10304)
- 0x36E9630: Validation constraints (FP4 restrictions)
