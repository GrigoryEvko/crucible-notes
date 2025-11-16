# GPU Architectures (SM 20-120): Complete Specifications

15 compute capabilities, 8 architecture generations (Fermi 2010 → Blackwell 2024), coverage: SM 20, 21, 30, 32, 35, 37, 50, 52, 53, 60, 61, 62, 70, 71, 72, 75, 80, 81, 82, 86, 87, 89, 90, 90a, 100, 101, 102, 103, 120, 121.

## SM Architecture Summary Table

| Gen | Codename | SM | Tensor Unit | Latency | Variants | Tensor Peak (FP16) | Year |
|-----|----------|----|----|---------|----------|-------------------|------|
| Fermi | - | 20 | None | - | 0 | - | 2010 |
| Fermi | - | 21 | None | - | 0 | - | 2011 |
| Kepler | - | 30 | None | - | 0 | - | 2012 |
| Kepler | - | 32 | None | - | 0 | - | 2012 |
| Kepler | - | 35 | None | - | 0 | - | 2013 |
| Kepler | - | 37 | None | - | 0 | - | 2013 |
| Maxwell | - | 50 | None | - | 0 | - | 2014 |
| Maxwell | - | 52 | None | - | 0 | - | 2016 |
| Maxwell | - | 53 | None | - | 0 | - | 2016 |
| Pascal | - | 60 | None | - | 0 | - | 2016 |
| Pascal | - | 61 | None | - | 0 | - | 2016 |
| Pascal | - | 62 | None | - | 0 | - | 2016 |
| Volta | - | 70 | WMMA | 2-8 | 67 | 62.5 | 2017 |
| Volta | - | 71 | WMMA | 2-8 | 67 | 62.5 | 2017 |
| Volta | - | 72 | WMMA | 2-8 | 67 | 62.5 | 2017 |
| Turing | - | 75 | WMMA+ | 2-8 | 60+ | 62.5 | 2018 |
| Ampere | - | 80 | MMA.SYNC | 4 | 40+ | 62.5 | 2020 |
| Ampere | - | 81 | MMA.SYNC | 4 | 40+ | 62.5 | 2020 |
| Ampere | - | 82 | MMA.SYNC | 4 | 40+ | 62.5 | 2020 |
| Ampere | - | 86 | MMA.SYNC | 4 | 40+ | 62.5 | 2021 |
| Ampere | - | 87 | MMA.SYNC | 4 | 40+ | 62.5 | 2020 |
| Ampere | - | 89 | MMA.SYNC | 4 | 40+ | 62.5 | 2022 |
| Hopper | - | 90 | Warpgroup MMA | 3 | 67+ | 156 | 2022 |
| Hopper | - | 90a | Warpgroup MMA | 3 | 67+ | 156 | 2023 |
| Blackwell | - | 100 | TCGen05 | 2 | 50+ | 512 | 2024 |
| Blackwell | - | 101 | TCGen05 | 2 | 50+ | 512 | 2024 |
| Blackwell | - | 102 | TCGen05 | 2 | 50+ | 512 | 2024 |
| Blackwell | - | 103 | TCGen05 | 2 | 50+ | 512 | 2024 |
| Blackwell | - | 120 | TCGen05×2 | 2 | 50+ | 1024 | 2024 |
| Blackwell | - | 121 | TCGen05×2 | 2 | 50+ | 1024 | 2024 |

## Tensor Core Specifications (by Generation)

### SM 70 (Volta) - WMMA

**Instruction Variants**: 67

**Key Instructions**:
- wmma_load_a_fp16: latency 1 cycle, throughput 1/cycle, 256 ops
- wmma_load_b_fp16: latency 1 cycle, throughput 1/cycle, 256 ops
- wmma_mma_fp16_fp16_fp16: latency 8 cycles, throughput 1/cycle, 256 ops, accumulator FP32
- wmma_mma_fp16_fp32_fp32: latency 8 cycles, throughput 1/cycle, 256 ops, accumulator FP32
- wmma_store_d_fp16: latency 1 cycle, throughput 1/cycle, 256 ops
- wmma_fill: latency 1 cycle, throughput 1/cycle, 32 ops

**Precisions**: FP16, FP32, INT8, INT4

**Matrix Dimension**: 16×16×16

**Peak Performance**: 62.5 TFLOPs FP16 per SM

**Cost Model**: base=1, load=1, store=1, compute=1, memory_barrier=5, synchronization=10

**Register Pressure**: 64 KB per SM, 255 max per thread, occupancy target 80%

### SM 75 (Turing) - WMMA + RT Cores

**Variants**: 60+

**Identical to SM70 WMMA** specifications. RT cores added for ray tracing (not tensor-related).

### SM 80 (Ampere) - MMA.SYNC

**Instruction Variants**: 40+

**Latency**: 4 cycles (uniform across all variants)

**Key Instructions**:
- mma.sync.m16n8k16.f16.f16.f32: 256 ops, throughput 1/cycle
- mma.sync.m16n8k16.tf32.tf32.f32: 256 ops, throughput 1/cycle
- mma.sync.m16n8k16.bf16.bf16.f32: 256 ops, throughput 1/cycle
- mma.sync.m16n8k16.i8.i8.i32: 256 ops, throughput 1/cycle
- cp.async.cg: 16 bytes, latency 10 cycles, throughput 2/cycle
- ldmatrix: 128 ops, latency 1 cycle, throughput 1/cycle (shared memory load with transpose)

**Precisions**: FP32, TF32, FP16, BF16, INT8, INT4, FP64 (datacenter)

**Matrix Dimension**: 16×8×16 (warp-level)

**Peak Performance**: 62.5 TFLOPs FP16 per SM

**Cost Model**: base=1, load=1, store=1, async_copy=0.5, compute=1, memory_barrier=3, synchronization=8

**Sparsity**: 2:4 structured (cost reduction 0.5), 4-cycle latency

**Register Pressure**: 64 KB per SM, occupancy target 75%

### SM 90 (Hopper) - Warpgroup MMA + TMA

**Variants**: 67+

**Latency**: 3 cycles (25% faster than SM80)

**Warpgroup Size**: 128 threads (4 warps)

**Key Instructions**:
- warpgroup_mma.m64n64k32.f16: 512 ops, latency 3 cycles, throughput 0.5/cycle
- warpgroup_mma.m64n64k32.f32: 128 ops, latency 3 cycles, throughput 0.5/cycle
- warpgroup_mma.m64n64k32.f8: 1024 ops, latency 3 cycles, throughput 1.0/cycle
- warpgroup_mma.m64n64k32.bf16: 512 ops, latency 3 cycles, throughput 0.5/cycle
- tma_load_mxnk: 128 bytes, latency 5 cycles, throughput 4.0/cycle
- tma_store: 128 bytes, latency 5 cycles, throughput 4.0/cycle
- ldmatrix_im2col: 128 ops, latency 1 cycle, throughput 1/cycle

**TMA Instructions** (13 variants, opcodes 8315-8331, 9213-9226):
- cp.async.bulk.global.to.shared.cluster: opcode 8315
- cp.async.bulk.gmem.to.dsmem: opcode 8316
- cp.async.bulk.tensor.gmem.to.smem.f1: opcode 8324
- cp.async.bulk.tensor.gmem.to.smem.f2: opcode 8325
- cp.async.bulk.tensor.gmem.to.smem.f4: opcode 8326
- cp.async.bulk.tensor.gmem.to.smem.f8: opcode 8327
- cp.async.bulk.tensor.gmem.to.smem.f16: opcode 8328
- cp.async.bulk.tensor.gmem.to.smem.im2col.w32: opcode 8329
- cp.async.bulk.tensor.gmem.to.smem.im2col.w64: opcode 8330
- cp.async.bulk.tensor.gmem.to.smem.im2col.w128: opcode 8331
- cp.async.bulk.tensor.g2s.im2col.w32: opcode 9213
- cp.async.bulk.tensor.g2s.tile.w1: opcode 9222, w2: 9223, w4: 9224, w8: 9225, w16: 9226

**Barrier Operations** (6 types):
- arrive (0x0): Increment counter, 1 cycle overhead
- arrive_drop (0x1): Fast signaling without wait
- arrive_wait (0x2): Atomic arrive+wait, 2 cycle overhead
- arrive_wait_drop (0x3): Full synchronization with cleanup
- expect_tx (0x4): Mark expected async data (bytes), critical for TMA coordination
- complete_tx (0x5): Signal async transmission complete

**Barrier Multicast Variants** (opcodes):
- mbarrier.arrive.multicast: opcode 10090
- mbarrier.arrive.multicast.shared: opcode 10091
- mbarrier.arrive.mc.cg1: opcode 10095
- mbarrier.arrive.mc.cg2: opcode 10096
- mbarrier.arrive.mc.shared.cg1: opcode 10097
- mbarrier.arrive.mc.shared.cg2: opcode 10098

**Scale Vector Configuration** (bits 51-53):
- 1X (encoding 00): default, 1× multiplier
- 2X (encoding 01): 2× multiplier, constraints: invalid for mxf8f6f4
- 4X (encoding 11): 4× multiplier, constraints: invalid for mxf8f6f4, mxf4

**Warp Specialization**:
- cta_group::1 (result & 0x2 == 0): Consumer (3 warps) - compute via MMA, weight stationary supported
- cta_group::2 (result & 0x2 != 0): Producer (1 warp) - TMA dispatch, weight stationary NOT supported

**Precisions**: FP32, TF32, FP16, BF16, INT8, FP8, INT4

**FP8 Variants**:
- E4M3: 1 sign, 4 exp, 3 mantissa, bias=7
- E5M2: 1 sign, 5 exp, 2 mantissa, bias=15

**Peak Performance**: 156 TFLOPs FP16, 312 TFLOPs FP8 per SM

**Cost Model**: base=1, load=0.25, store=0.25, tma=0.1, compute=1, memory_barrier=2, synchronization=5, warpgroup_sync=3

**Register Pressure**: 64 KB per SM (partitioned by cta_group), occupancy target 50%

### SM 100 (Blackwell) - TCGen05 + FP4

**Variants**: 50+

**Latency**: 2 cycles (50% faster than SM90, 2× faster than SM80)

**Warpgroup Size**: 128 threads (4 warps)

**Key Instructions**:
- tcgen05.mma.f8.f8.f32: 2048 ops, latency 2 cycles, throughput 2.0/cycle
- tcgen05.mma.f4.f4.f32: 4096 ops, latency 2 cycles, throughput 4.0/cycle
- tcgen05.mma.block_scale_fp8: 2048 ops, latency 2 cycles, throughput 2.0/cycle
- tcgen05.mma.block_scale_fp4: 4096 ops, latency 2 cycles, throughput 4.0/cycle
- tcgen05.mma.f16.f16.f32: 512 ops, latency 2 cycles, throughput 1.0/cycle
- tcgen05.mma.bf16.bf16.f32: 512 ops, latency 2 cycles, throughput 1.0/cycle
- tcgen05.mma.tf32.tf32.f32: 512 ops, latency 2 cycles, throughput 1.0/cycle
- tcgen05.mma.i8.i8.i32: 2048 ops, latency 2 cycles, throughput 2.0/cycle
- tcgen05.mma.i4.i4.i32: 4096 ops, latency 2 cycles, throughput 4.0/cycle
- tcgen05.mma.sparse (variable): variable ops, latency 2 cycles, throughput varies
- tcgen05.cp.async: 16 bytes, latency 10 cycles, throughput 4.0/cycle
- tcgen05.commit: multicast sync, latency 0
- tcgen05.fence: memory fence, latency 0
- tcgen05.wait: MMA sync, latency 0
- tcgen05.alloc: descriptor alloc, latency 1 cycle
- tcgen05.dealloc: descriptor dealloc, latency 1 cycle
- tcgen05.relinquish_alloc: descriptor relinquish, latency 1 cycle

**Precisions**: FP32, TF32, FP16, BF16, INT8, FP8, FP4, INT4, block_scale formats

**FP4 Format** (E2M1):
- Bit 3: sign
- Bits 2-1: exponent (2 bits)
- Bit 0: mantissa (1 bit)
- 4 bits total, 16 representable values
- Packed: 2 FP4 values per byte
- Representable values: ±{0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0}

**Block Scale** (format IDs 10299, 10304):
- Per-block FP16/FP32 scale factor
- Handles dynamic range limitations of FP4
- ~2.5% memory overhead for scale factors
- 3.5-3.8× effective compression vs FP16 + 2:4 sparsity

**2:4 Structured Sparsity** (SM100 enhanced):
- 6 valid patterns (C(4,2) combinations)
- Pattern encoding (0-5):
  - Pattern 0: positions [0,1], mask 1100
  - Pattern 1: positions [0,2], mask 1010
  - Pattern 2: positions [0,3], mask 1001
  - Pattern 3: positions [1,2], mask 0110
  - Pattern 4: positions [1,3], mask 0101
  - Pattern 5: positions [2,3], mask 0011
- Metadata: 2 bits per 4-element block (0.5 bits/element)
- Dynamic sparsity discovery supported
- Cost reduction: 0.25 (vs 0.5 for SM80/90)

**Peak Performance**: 512 TFLOPs FP16, 1024 TFLOPs FP8, 2048 TFLOPs FP4 per SM

**Cost Model**: base=1, load=0.125, store=0.125, tma=0.05, compute=1, fp8_boost=2.0, fp4_boost=4.0, int4_boost=4.0, memory_barrier=1, synchronization=2, warpgroup_sync=1

**Compression**: 3.5-3.8× with FP4 + block scale + 2:4 sparsity

**Register Pressure**: 64 KB per SM (partitioned by cta_group), occupancy target 50%

### SM 120 (Blackwell-Ultra) - Dual TCGen05

**Architecture**: Dual tensor cores per SM

**Variants**: 50+ (extended support)

**Latency**: 2 cycles (identical to SM100)

**Key Difference**: 2× throughput for most operations (transparent to user)

**Peak Performance**: 1024 TFLOPs FP16, 2048 TFLOPs FP8, 4096 TFLOPs FP4 per SM

**All instructions identical to SM100**, automatic dual-core scheduling

**SM 121**: Refinements with improved frequency potential

## Precision Format Specifications

### IEEE 754 FP16 (binary16)
- Bits: [sign:1][exp:5][mantissa:10]
- Exponent bias: 15
- Range: ±65504 max, ±6.1×10⁻⁵ min normal

### BF16 (Brain Float 16, SM80+)
- Bits: [sign:1][exp:8][mantissa:7]
- Exponent bias: 127
- Range: same as FP32

### TF32 (TensorFloat-32, SM80+)
- Bits: [sign:1][exp:8][mantissa:10, implicit 1]
- Exponent bias: 127
- Internal format only, equivalent FP16 precision

### FP8 E4M3 (SM90+)
- Bits: [sign:1][exp:4][mantissa:3]
- Exponent bias: 7

### FP8 E5M2 (SM90+)
- Bits: [sign:1][exp:5][mantissa:2]
- Exponent bias: 15

### FP4 E2M1 (SM100+)
- Bits: [sign:1][exp:2][mantissa:1]
- Exponent bias: 1
- 16 representable values
- 4× compression vs FP16

## Feature Comparison Matrix

| Feature | SM70 | SM80 | SM90 | SM100 | SM120 |
|---------|------|------|------|-------|-------|
| Tensor latency (cycles) | 2-8 | 4 | 3 | 2 | 2 |
| Tensor variants | 67 | 40+ | 67+ | 50+ | 50+ |
| Warpgroup size | 32 | 32 | 128 | 128 | 128 |
| Max threads/block | 1024 | 1024 | 2048 | 2048 | 2048 |
| TMA | ✗ | ✗ | ✓ | ✓ | ✓ |
| Warp specialization | ✗ | ✗ | ✓ | ✓ | ✓ |
| FP16 | ✓ | ✓ | ✓ | ✓ | ✓ |
| BF16 | ✗ | ✓ | ✓ | ✓ | ✓ |
| TF32 | ✗ | ✓ | ✓ | ✓ | ✓ |
| FP8 | ✗ | ✗ | ✓ | ✓ | ✓ |
| FP4 | ✗ | ✗ | ✗ | ✓ | ✓ |
| Block scale | ✗ | ✗ | ✗ | ✓ | ✓ |
| Sparsity (2:4) | ✗ | ✓ | ✓ | ✓ | ✓ |
| Shared memory | 96 KB | 96 KB | 96 KB | 96 KB | 96 KB |
| Register file | 64 KB | 64 KB | 64 KB | 64 KB | 64 KB |
| Registers/thread | 255 | 255 | 255 | 255 | 255 |
| Cluster scope | ✗ | ✗ | 8 blocks | 8 blocks | 8 blocks |
| Cost model: barrier | 5 | 3 | 2 | 1 | 1 |
| Cost model: sync | 10 | 8 | 5 | 2 | 2 |

## Latency Progression

| Gen | SM | Latency (cycles) | vs SM70 | vs Predecessor |
|-----|-----|------------------|---------|----------------|
| Volta | 70 | 2-8 (avg 4) | - | - |
| Turing | 75 | 2-8 (avg 4) | 0% | 0% |
| Ampere | 80 | 4 | 0% | 0% |
| Ada | 89 | 4 | 0% | 0% |
| Hopper | 90 | 3 | 25% | 25% |
| Blackwell | 100 | 2 | 50% | 33% |
| Blackwell-U | 120 | 2 | 50% | 0% |

## Instruction Selection Cost Models

### SM70 (Volta)
Base: 1, Load: 1, Store: 1, Compute: 1
Memory Barrier: 5, Synchronization: 10

### SM80 (Ampere)
Base: 1, Load: 1, Store: 1, Async Copy: 0.5
Memory Barrier: 3, Synchronization: 8

### SM90 (Hopper)
Base: 1, Load: 0.25, Store: 0.25, TMA: 0.1
Memory Barrier: 2, Synchronization: 5, Warpgroup Sync: 3

### SM100 (Blackwell)
Base: 1, Load: 0.125, Store: 0.125, TMA: 0.05
FP8 Boost: 2.0, FP4 Boost: 4.0, INT4 Boost: 4.0
Memory Barrier: 1, Synchronization: 2, Warpgroup Sync: 1

## Tensor Core Throughput Comparison

| Precision | SM70 | SM80 | SM90 | SM100 | SM120 |
|-----------|------|------|------|-------|-------|
| FP16 | 62.5 | 62.5 | 156 | 512 | 1024 |
| BF16 | - | 62.5 | 156 | 512 | 1024 |
| TF32 | - | 62.5 | 156 | 512 | 1024 |
| FP8 | - | - | 312 | 1024 | 2048 |
| FP4 | - | - | - | 2048 | 4096 |
| INT8 | 62.5 | 62.5 | 312 | 1024 | 2048 |
| INT4 | - | - | - | 2048 | 4096 |

(TFLOPs per SM, peak)

## Architecture Entry Points

**CICC compiles PTX to**:
- SM 20, 21 (Fermi, legacy)
- SM 30, 32, 35, 37 (Kepler)
- SM 50, 52, 53 (Maxwell)
- SM 60, 61, 62 (Pascal)
- SM 70, 71, 72 (Volta)
- SM 75 (Turing)
- SM 80, 81, 82, 86, 87, 89 (Ampere)
- SM 90, 90a (Hopper)
- SM 100, 101, 102, 103, 120, 121 (Blackwell)

**Compiler flags**: `-arch=sm_XYZ` (cubin), `-arch=compute_XYZ` (PTX)

## References

- **tensor_core_costs.json**: Complete latency/throughput/cost tables (SM70, SM80, SM90, SM100, SM120)
- **tma_scheduling_sm90.json**: TMA opcodes (8315-8331, 9213-9226), barrier operations (expect_tx opcode 0x4)
- **warp_specialization_sm90.json**: cta_group assignment (bit 1 of result), producer/consumer patterns
- **sparsity_support_sm100.json**: 2:4 patterns (6 variants, 2-bit metadata per block), dynamic detection
- **fp4_format_selection.json**: FP4 E2M1 specification, block scaling (format IDs 10299/10304), quantization
