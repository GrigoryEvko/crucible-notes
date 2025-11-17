# SM90 Warp Specialization - Technical Implementation

## Overview

SM90 (Hopper) introduces **explicit warp specialization** where warps within a warpgroup take different roles:
- **Producer warps (cta_group::2)**: Load data via TMA (Tensor Memory Accelerator)
- **Consumer warps (cta_group::1)**: Perform tensor core computations

This enables overlap of memory and compute operations, achieving 20-50% performance improvement depending on compute/memory ratio.

**Evidence**: WARP_SPECIALIZATION_SM90_ANALYSIS.md, warp_specialization_sm90.json, tma_scheduling_sm90.json, decompiled code at sub_35F3330, sub_35F4E30, sub_35F4080, sub_A8E250, sub_36E9630

---

## Warp Role Assignment

### Static Assignment Algorithm

**Location**: `decompiled/sub_35F3330_0x35f3330.c:85-111`

**Decision Logic**:
```c
if (!strcmp((const char *)a5, "cta_group")) {
    void *output_buffer = *(void **)(a4 + 32);
    unsigned __int64 space = *(_QWORD *)(a4 + 24) - (_QWORD)output_buffer;

    if ((result & 2) != 0) {
        // Assign to cta_group::2 (producer)
        output_string("cta_group::2");
    } else {
        // Assign to cta_group::1 (consumer)
        output_string("cta_group::1");
    }
}
```

**Key Finding**: Assignment is based on **Bit 1 (0x02)** of the `result` bitfield:
- If `(result & 0x2) != 0` → **cta_group::2 (Producer)**
- If `(result & 0x2) == 0` → **cta_group::1 (Consumer)**

### Bitfield Encoding Structure

The `result` value encodes all MMA attributes:

```
Bit  |  Field           |  Meaning / Encoding
-----|------------------|-------------------------------------
0    |  Weight Mode 0   |  Part of weight stationary bits
1    |  CTA Group       |  0: consumer, 1: producer ⭐ DECISION
2    |  Weight Mode 1   |  Part of weight stationary bits
3-4  |  Scale Vector    |  00: 1X, 10: 2X, 11: 4X
6-8  |  Data Kind       |  Tensor data type (mxf4, f8f6f4, etc)
9-10 |  Other Scaling   |  Register scaling
     |  Reserved        |  Other configuration
```

**Evidence**: bitfield_encoding in warp_specialization_sm90.json:406-447

### Compilation Decision Factors

The compiler uses heuristics to determine whether to enable warp specialization:

1. **Memory Bandwidth Requirements**: High-bandwidth kernels benefit more
2. **Compute Intensity**: Ratio of compute to memory operations
3. **MMA Instruction Type**: tcgen05.mma variants with specific capabilities
4. **Data Flow Dependencies**: Can data be pre-loaded while computing?
5. **Buffer Reuse Patterns**: Multi-iteration loops enable pipelining
6. **Async Copy Feasibility**: Whether TMA acceleration is applicable

**Evidence**: WARP_SPECIALIZATION_SM90_ANALYSIS.md:98-108, role_assignment_algorithm in warp_specialization_sm90.json

---

## Producer Warp Responsibilities (cta_group::2)

### TMA Operations Dispatched

Producer warps initiate various tensor memory accelerator operations:

**Primary Instruction Family**: `cp.async.bulk.tensor.g2s`

```c
// Evidence: sub_A8E250_0xa8e250.c:1019-1170
cp.async.bulk.tensor.g2s.<type> [dst_shared], [src_global]

Types supported:
├─ .mxf4nvf4    // Mixed int4/float4 precision
├─ .f8f6f4      // FP8/FP6/FP4 (ultra-low precision)
├─ .mxf8f6f4    // Mixed FP8/FP6/FP4
├─ .f16         // Half precision (standard)
├─ .i8          // 8-bit integer
├─ .tf32        // TensorFloat-32
└─ .mxf4        // Alternative mixed format
```

**Tile-Based Variants**:
```c
cp.async.bulk.tensor.g2s.tile.<dim> [dst], [src]
├─ .tile.w1     // Scale multiplier 1X (opcode 9222)
├─ .tile.w2     // Scale multiplier 2X (opcode 9223)
├─ .tile.w4     // Scale multiplier 4X (opcode 9224)
├─ .tile.w8     // Opcode 9225
└─ .tile.w16    // Opcode 9226
```

**Generic Variants**:
```c
cp.async.bulk.gmem.to.dsmem [dst], [src]
    // Global to distributed shared memory (opcode 8316)

cp.async.bulk.global.to.shared.cluster [dst], [src]
    // Cluster-scope load, multi-block coordination (opcode 8315)

cp.async.bulk.tensor.gmem.to.smem.<fmt> [dst], [src]
    // Format-specific tensor load
    ├─ .f1  (opcode 8324)
    ├─ .f2  (opcode 8325)
    ├─ .f4  (opcode 8326)
    ├─ .f8  (opcode 8327)
    └─ .f16 (opcode 8328)
```

**Image-to-Column Format**:
```c
cp.async.bulk.tensor.g2s.im2col.<width> [dst], [src]
    ├─ .w32  // 32-bit width (opcode 9213)
    ├─ .w64  // 64-bit width (opcode 9214)
    └─ .w128 // 128-bit width (opcode 9215)

cp.async.bulk.tensor.gmem.to.smem.im2col.<width> [dst], [src]
    ├─ .w32  (opcode 8329)
    ├─ .w64  (opcode 8330)
    └─ .w128 (opcode 8331)
```

**Evidence**: tma_instruction_set in tma_scheduling_sm90.json:28-151

### Synchronization Points

Producer must signal expected data arrival before dispatch:

```c
// Evidence: sub_35F4080_0x35f4080.c:138-144, expect_tx operation (case 4)

// Step 1: Signal expected byte count
mbarrier.arrive.expect_tx [barrier_address], expected_bytes;
    // Tells barrier how many bytes TMA will transfer
    // Critical: must match actual transfer size exactly
    
// Step 2: Dispatch TMA operations
cp.async.bulk.tensor.g2s.mxf4 [shared_dst], [global_src];
cp.async.bulk.tensor.g2s.mxf4 [shared_dst2], [global_src2];
    // Queue multiple TMA loads
    
// Step 3: Flush pending operations
cp.async.bulk.commit_group;  // or tcgen05.commit_group (SM100)
    // Ensures all queued transfers are committed to memory hierarchy
    
// Step 4: Synchronize if needed
__syncthreads();  // Block-level sync for SM90
cluster.sync();   // Cluster-wide sync for multi-block
```

### Typical Producer Code Pattern

```c
// Evidence: WARP_SPECIALIZATION_SM90_ANALYSIS.md:158-170, 220-280

void producer_warp_async_copy() {
    // Typical configuration: 1 warp per block
    
    for (int tile = 0; tile < num_tiles; tile++) {
        // Signal expected data
        mbarrier.arrive.expect_tx [barrier], TILE_BYTES;
        // E.g., TILE_BYTES = 4096 for 64x64 FP16 tiles
        
        // Initiate async TMA load
        cp.async.bulk.tensor.g2s [shared_buffer0], [global_A];
        cp.async.bulk.tensor.g2s [shared_buffer1], [global_B];
        
        // Flush pending operations
        cp.async.bulk.commit_group;
        
        // Producer can immediately start next iteration
        // (overlap with consumer computation)
    }
}
```

**Evidence**: producer_pattern in warp_specialization_sm90.json:84-122, tma_scheduling_model in tma_scheduling_sm90.json:347-410

### Synchronization Cost

- **expect_tx overhead**: 0 cycles (metadata operation, no latency)
- **arrive operation**: 1-2 cycles
- **commit_group**: Minimal (ensures dispatch, non-blocking)

**Evidence**: synchronization_cost in tma_scheduling_sm90.json:552-555

---

## Consumer Warp Responsibilities (cta_group::1)

### Waiting for Data

Consumer warps block until producer signals data ready:

```c
// Evidence: sub_35F4E30_0x35f4e30.c:46-61, barrier 'arrive' operation handling

mbarrier.wait [barrier_address];
    // Blocking operation
    // Returns only when expected byte count arrives from TMA
    // Enforces acquire semantics for shared memory
```

### Tensor Core Computation

Once data arrives in shared memory, consumer performs MMA operations:

```c
// Typical instruction sequence
for (int tile = 0; tile < num_tiles; tile++) {
    // Wait for producer to load data
    mbarrier.wait [barrier];
    
    // Now perform matrix multiply-accumulate
    // SM90 tcgen05.mma instructions
    mma.m16n8k32.sync.aligned [accum], [smem_A], [smem_B];
    mma.m32n8k16.sync.aligned [accum], [smem_A], [smem_B];
    mma.m8n32k16.sync.aligned [accum], [smem_A], [smem_B];
    
    // Or SM90-specific variants
    tcgen05.mma [accum], [smem_A], [smem_B];
    tcgen05.mma.block_scale [accum], [smem_A], [smem_B], scale_factor;
    tcgen05.mma.tile.<type> [accum], [smem_A], [smem_B];
}
```

### Pipeline Strategy

```
Typical 3-iteration pipeline overlap:

Iteration 0:
  Producer: Load batch[0] → shared[0:SIZE]
  Consumer: Idle (waiting for data)

Iteration 1:
  Producer: Load batch[1] → shared[SIZE:2*SIZE]
  Consumer: Wait resolved, compute batch[0]
            (producer's next load starts immediately)

Iteration 2:
  Producer: Load batch[2] → shared[0:SIZE]
  Consumer: Compute batch[1]
            (can start wait for batch[2])

Iteration 3:
  Producer: Idle or help with computation
  Consumer: Compute batch[2]

Result: Compute and load overlap for 2-3 iterations
Effective throughput: Limited by max(TMA_latency, MMA_latency)
```

**Evidence**: pipeline_strategy in warp_specialization_sm90.json:152-157, execution_phases in tma_scheduling_sm90.json:353-387

### Consumer Capabilities

Consumer warps have optimization advantages unavailable to producers:

- **Can use weight-stationary MMA**: Optimization for dense matrix operations
- **Can use all data types**: No type restrictions
- **Guaranteed data safety**: After mbarrier.wait returns, shared memory data is valid

**Evidence**: consumer_pattern in warp_specialization_sm90.json:124-162, constraint_validation in warp_specialization_sm90.json:530-535

---

## Async Barrier Protocol (mbarrier)

### Barrier Structure and Initialization

**Primary Primitive**: `mbarrier` (multicast barrier)

```c
// Evidence: WARP_SPECIALIZATION_SM90_ANALYSIS.md:113-125, 145-152

// Barrier initialization
mbarrier.init.shared.b64 [barrier_ptr], expected_count;
    // Allocates barrier at shared memory address
    // Sets up expected arrival count
    // Typically one barrier per tile iteration
```

### Scope Options

Barriers can coordinate at different granularities:

```c
| Scope                | Size                 | Use Case                    |
|---------------------|----------------------|---------------------------|
| .cluster            | Up to 8 blocks       | Cluster-wide coordination  |
| .cta                | Single block (256+)  | Block-level only          |
| .shared::cluster    | Cluster + shared mem | Cluster memory ops        |
| .shared::cta        | Block + shared mem   | Block-local sync          |

Evidence: scope_options in tma_scheduling_sm90.json:204-230
Evidence: barrier_scope_extensions in WARP_SPECIALIZATION_SM90_ANALYSIS.md:117-125
```

### Operation Types with Opcodes

```c
// Evidence: sub_35F4080_0x35f4080.c:99-158 (switch statement on operation type)
// Evidence: barrier_operation_opcodes in WARP_SPECIALIZATION_SM90_ANALYSIS.md:323-332

Operation Type          Code    Instruction                    Purpose
─────────────────────────────────────────────────────────────────────────
arrive                  0x0     .mbarrier::arrive::one         Single thread arrives
arrive_drop             0x1     .mbarrier::arrive_drop         Arrive, no wait
arrive_wait             0x2     .mbarrier::arrive_wait         Arrive and wait
arrive_wait_drop        0x3     .mbarrier::arrive_wait_drop    Arrive, wait, cleanup
expect_tx               0x4     .mbarrier::expect_tx           🔑 CRITICAL FOR TMA
complete_tx             0x5     .mbarrier::complete_tx         Transmission done
```

### Producer Signal Mechanism (expect_tx)

**Critical Operation for TMA Coordination**:

```c
// Evidence: WARP_SPECIALIZATION_SM90_ANALYSIS.md:145-152
// Evidence: expect_tx operation in warp_specialization_sm90.json:213-220
// Evidence: sub_35F4080_0x35f4080.c:138-144

mbarrier.arrive.expect_tx [barrier_address], expected_bytes;
    
    Operands:
    ├─ barrier_address: Shared memory location of mbarrier structure
    └─ expected_bytes: Number of bytes TMA will transfer (must be exact)
    
    Effect:
    ├─ Signals to barrier that TMA data is incoming
    ├─ Marks expected byte count in barrier state
    └─ Enables consumer to know transfer is complete by byte count
    
    Critical Property:
    ├─ If expected_bytes does not match actual TMA transfer:
    │  └─ Consumer will deadlock or get wrong data
    └─ Must be called BEFORE TMA dispatch
```

### Consumer Wait Mechanism

```c
// Evidence: WARP_SPECIALIZATION_SM90_ANALYSIS.md:157-170
// Evidence: sub_35F4E30_0x35f4e30.c:46-61

mbarrier.wait [barrier_address];
    
    Semantics:
    ├─ Blocking operation (consumer stalls)
    ├─ Returns when actual bytes arrive == expected_bytes
    └─ Provides acquire semantics for shared memory
    
    Timing:
    ├─ If data already arrived: Returns immediately
    └─ If not arrived: Blocks until TMA completes
    
    Typical latency: 1-5 cycles (post-TMA)
    Expected latency: 50-500 cycles (including TMA transit)
```

### Complete Protocol Sequence

```c
// Evidence: producer_consumer_flow in warp_specialization_sm90.json:228-239
// Evidence: WARP_SPECIALIZATION_SM90_ANALYSIS.md:158-170

Timeline for one tile iteration:

T0:  Producer: mbarrier.arrive.expect_tx [barrier], 4096
T0:  Producer: cp.async.bulk.tensor.g2s [smem_A], [gmem_A]
T0:  Producer: cp.async.bulk.tensor.g2s [smem_B], [gmem_B]
T0:  Producer: cp.async.bulk.commit_group  // Flush pending
T0:  Consumer: mbarrier.wait [barrier]  // Block
     
...  (TMA in flight: 50-500 cycles)

T100: TMA hardware: auto-signals barrier (complete_tx)
      Consumer: mbarrier.wait returns

T100: Consumer: Now safe to read smem_A, smem_B
      Consumer: mma.m16n8k32 [acc], [smem_A], [smem_B]  (200 cycles)

T100: Producer: Can immediately start next iteration
      Producer: mbarrier.arrive.expect_tx [barrier], 4096  (next tile)
      Producer: cp.async.bulk.tensor.g2s [smem_A], [gmem_A + offset]
```

### Phase Management for Pipelined Execution

Barriers support phase bits for multiple outstanding requests:

```c
// Implicit in mbarrier implementation
// Evidence: async_coordination_details in tma_scheduling_sm90.json:389-395

Phase flipping allows:
├─ Multiple outstanding TMA operations in different buffers
├─ Producer: Load into buffer[phase ^ 1] while consumer reads buffer[phase]
├─ Typical: 2-4 phases for deep pipelining
└─ Critical: Prevents producer from overwriting data consumer is using
```

**Evidence**: barrier_synchronization_framework in tma_scheduling_sm90.json:153-270

---

## TMA (Tensor Memory Accelerator) Integration

### TMA Descriptor Setup

Producers initialize TMA descriptors (compiler-generated):

```c
// Evidence: descriptor_handling in tma_scheduling_sm90.json:413-449
// Evidence: tma_integration in warp_specialization_sm90.json:242-308

TMA Descriptor Components:
├─ Tensor Layout: Multi-dimensional shape [N, H, W, C] or [M, K]
├─ Stride Configuration: Byte offsets between elements in each dimension
├─ Data Type: mxf4, f8f6f4, mxf8f6f4, f16, i8, tf32
├─ Scale Vector: Multiplier for tile dimensions (1X, 2X, 4X)
└─ Memory Footprint: 64-128 bytes typically

Note: Descriptor initialization not fully exposed in binary (compiler IR level)
      Descriptor is created during early compilation phases
```

### Memory Transfer Initiation

```c
// Producer dispatches TMA:

for (int k = 0; k < K; k += TILE_K) {
    // Indicate TMA will transfer BATCH_SIZE bytes
    mbarrier.arrive.expect_tx [barrier], BATCH_SIZE;  // E.g., 4096 bytes
    
    // Dispatch TMA load (using implicit descriptor)
    cp.async.bulk.tensor.g2s [shared_A], [global_A + k*stride];
    cp.async.bulk.tensor.g2s [shared_B], [global_B + k*stride];
    
    // Commit ensures dispatch
    cp.async.bulk.commit_group;
    
    // Producer continues immediately (no wait!)
}

Timing:
├─ mbarrier.arrive.expect_tx: 0 cycles (metadata)
├─ cp.async.bulk.tensor: 0 cycles (queued, not blocking)
├─ cp.async.bulk.commit_group: ~1-2 cycles (flush)
└─ Total: ~2 cycles to initiate next iteration
    while TMA hardware executes 50-500 cycle transfer concurrently
```

### Completion Detection

```c
// Evidence: barrier_pre_condition in tma_scheduling_sm90.json:390

Mechanism:
├─ TMA hardware automatically signals mbarrier upon completion
├─ Increments arrival count by expected_bytes
├─ Consumer's mbarrier.wait unblocks when count matches
├─ No explicit TMA completion check needed by producer

Example (assume expected_bytes = 4096):
├─ T0: Producer: expect_tx(..., 4096)  // Signal expectation
├─ T0: TMA hardware: starts transfer
├─ T100: TMA hardware: completes 4096 bytes
├─ T100: TMA auto-signal: barrier.complete_tx(4096 bytes)
├─ T100: Consumer: mbarrier.wait(...) returns
└─ All automatic, no producer intervention needed
```

### Overlap Analysis

```c
// Evidence: overlap_benefit in tma_scheduling_sm90.json:541-546

Scenario: Producer loads 100 cycles, Consumer computes 150 cycles

WITHOUT overlap:
├─ Load: 100 cycles
├─ Compute: 150 cycles
└─ Total: 250 cycles per iteration

WITH overlap (warp specialization):
├─ Iteration 0: Producer loads (100), Consumer waits
├─ Iteration 1: Producer loads next (100), Consumer computes prev (150)
│              Overlap: min(100, 150) = 100 cycles
├─ Iteration 2+: Sustained overlap at compute rate
└─ Total: max(100, 150) = 150 cycles per iteration
└─ **Speedup: 250/150 = 1.67x**

Critical Path: max(TMA_latency - MMA_latency, 0)
├─ If load faster than compute: Compute-bound
├─ If load slower than compute: Memory-bound
└─ Warp specialization addresses memory-bound cases
```

**Evidence**: tma_latency in tma_scheduling_sm90.json:525-533, overlap_benefit in tma_scheduling_sm90.json:541-546

---

## Performance Characteristics

### Memory Bandwidth Overlap

```
Measured Improvement: 20-50% depending on workload

Factors:
├─ TMA latency: 50-500 cycles (hidden by concurrent compute)
├─ MMA latency: 4-8 cycles (pipelined with load)
├─ Best case: Producer stalls <10% of iteration time
├─ Typical case: 30% improvement in throughput
└─ Worst case: No improvement if memory not bottleneck

Evidence: memory_bandwidth_overlap in tma_scheduling_sm90.json:546-551
Evidence: performance_characteristics in WARP_SPECIALIZATION_SM90_ANALYSIS.md:336-366
```

### Synchronization Overhead

```
mbarrier Operation Costs:

├─ arrive: 1-2 cycles
├─ expect_tx: 0 cycles (metadata)
├─ wait: 1-5 cycles (post-TMA)
├─ arrive_drop: 1 cycle (fast path)
└─ complete_tx: Automatic (TMA triggers)

Total per iteration: ~2-5 cycles synchronization overhead
Amortized: Negligible for 100+ cycle TMA operations

Evidence: barrier_latency in tma_scheduling_sm90.json:535-539
```

### Register Allocation Strategy

```
Producer (cta_group::2):
├─ Minimal register usage: ~10-20 registers
├─ Mostly: Address calculations, loop counters
├─ TMA hardware handles indexing (no descriptor computation needed)
└─ Frees registers for consumer

Consumer (cta_group::1):
├─ High register usage: 200+ registers
├─ Accumulators: Large matrix (64×64 = 4096 values)
├─ Source operands: Input tensors in registers
├─ Working set: Algorithm-specific intermediate values
└─ Register pressure primary constraint

Partition Strategy:
├─ Compiler reserves specific registers per cta_group
├─ No overlapping register allocation between groups
├─ Enables full utilization without conflicts

Evidence: register_allocation in warp_specialization_sm90.json:406-410
Evidence: producer_registers and consumer_registers in tma_scheduling_sm90.json:407-410
```

---

## Code Generation Patterns in CICC

### Warp Specialization Decision in Compiler

**Decision Point**: `sub_35F3330_0x35f3330.c:85-111`

```c
// Evidence: code_generation_details in warp_specialization_sm90.json:405-459

bool should_specialize_warps(KernelConfig *cfg) {
    // Check for SM90+ architecture
    if (cfg->sm_version < 90) 
        return false;
    
    // Check workload characteristics for cost-benefit
    if (has_large_data_movement(cfg) && 
        has_tensor_ops(cfg) &&
        compute_intensity(cfg) > THRESHOLD) {
        return true;
    }
    
    return false;
}

// Heuristic factors (from WARP_SPECIALIZATION_SM90_ANALYSIS.md:98-108):
├─ Memory Bandwidth Requirements
├─ Compute Intensity (computation / memory bytes)
├─ MMA Instruction Type (tcgen05 variants)
├─ Data Flow Dependencies
├─ Buffer Reuse Patterns
└─ Async Copy Feasibility
```

### Warp Divergence Handling

```c
// Evidence: DIVERGENCE_ANALYSIS_GUIDE.md (from ANALYSIS_SUMMARY.txt:107-116)

// Consumer vs Producer code divergence:
int warp_id = get_warp_id_in_warpgroup();  // 0-3

if (warp_id == 0) {
    // Warp 0: Producer (cta_group::2)
    // Execute async copy, barrier signaling
    producer_code();
} else {
    // Warps 1-3: Consumer (cta_group::1)
    // Execute compute
    consumer_code();
}

Divergence handling:
├─ Static: Compiler knows which warp is which at compile time
├─ No runtime overhead for role decision
├─ Both paths compiled, selected based on warp_id
├─ CUDA divergence analysis (L3-10) tracks through CFG
└─ Safe because roles are uniform (all warp 0 → producer)
```

### MMA Attribute Encoding

```c
// Evidence: code_generation_details in warp_specialization_sm90.json:406-447
// Evidence: bitfield_encoding in warp_specialization_sm90.json:405-447

// Compiler encodes all MMA properties in single result value:
unsigned int result = 0;

// Encode CTA group
if (is_producer_warp) {
    result |= 0x2;  // Set bit 1 → cta_group::2
} else {
    result &= ~0x2; // Clear bit 1 → cta_group::1
}

// Encode weight stationary (bits 0, 2)
if (use_weight_stationary && !is_producer_warp) {
    result |= 0x01;  // Only allowed for consumer
}

// Encode scale vector (bits 3-4)
switch (scale_multiplier) {
    case 1: result |= 0x0; break;    // 1X
    case 2: result |= 0x8; break;    // 2X
    case 4: result |= 0xC; break;    // 4X
}

// Encode data kind (bits 6-8)
switch (data_type) {
    case mxf4nvf4: result |= 0x00; break;
    case f8f6f4:   result |= 0x40; break;
    case mxf8f6f4: result |= 0x80; break;
    ...
}

// Result passed to assignment function
assign_warp_group_from_bitfield(result);
```

---

## Example: GEMM with Warp Specialization

### Simplified Kernel Structure

```cuda
// Evidence: Example from WARP_SPECIALIZATION_SM90_ANALYSIS.md:220-280

__global__ void gemm_specialized(
    float *A, float *B, float *C,
    int M, int N, int K) {
    
    // Shared memory buffers (double buffered)
    __shared__ float smem_A[2][TILE_M][TILE_K];
    __shared__ float smem_B[2][TILE_K][TILE_N];
    __shared__ mbarrier_t barrier[2];
    
    // Warp specialization
    int warp_id = threadIdx.x / 32;
    int lane_id = threadIdx.x % 32;
    
    // Initialize barriers (done once)
    if (threadIdx.x == 0) {
        mbarrier.init.shared.b64 [&barrier[0]], 4;
        mbarrier.init.shared.b64 [&barrier[1]], 4;
    }
    __syncthreads();
    
    if (warp_id == 0) {
        // ═════════════════════════════════════════════
        // PRODUCER WARP (cta_group::2)
        // ═════════════════════════════════════════════
        
        for (int k = 0; k < K; k += TILE_K) {
            int buf = (k / TILE_K) % 2;  // Double buffer index
            
            // Signal expected data (4 tiles × 4KB each = 16KB)
            // 🔑 CRITICAL: expect_tx BEFORE dispatch
            mbarrier.arrive.expect_tx [&barrier[buf]], 16384;
            
            // Dispatch TMA loads (non-blocking)
            cp.async.bulk.tensor.g2s [&smem_A[buf][0][0]], 
                                      [&A[k]];
            cp.async.bulk.tensor.g2s [&smem_B[buf][0][0]], 
                                      [&B[k*N]];
            
            // Flush pending operations
            cp.async.bulk.commit_group;
            
            // Producer immediately proceeds to next iteration
            // TMA executes in background (50-500 cycles)
        }
        
    } else {
        // ═════════════════════════════════════════════
        // CONSUMER WARPS (cta_group::1)
        // ═════════════════════════════════════════════
        
        float accum[16] = {0};  // Result accumulator
        
        for (int k = 0; k < K; k += TILE_K) {
            int buf = (k / TILE_K) % 2;
            
            // 🔑 CRITICAL: Wait for data
            // Blocks until expected bytes arrive
            mbarrier.wait [&barrier[buf]];
            
            // Data now guaranteed in shared memory
            // Can safely read smem_A[buf] and smem_B[buf]
            
            // Perform matrix multiply
            // (Typical: 200-500 cycle MMA operation)
            #pragma unroll
            for (int i = 0; i < TILE_M; i++) {
                #pragma unroll
                for (int j = 0; j < TILE_N; j++) {
                    float a_val = smem_A[buf][i][lane_id];
                    float b_val = smem_B[buf][lane_id][j];
                    
                    // Or use warpgroup.mma for SM90
                    // tcgen05.mma.m16n8k32.sync [accum], 
                    //    [smem_A[buf]], [smem_B[buf]];
                    
                    accum[i*TILE_N + j] += a_val * b_val;
                }
            }
        }
        
        // Write results to global memory
        for (int i = 0; i < TILE_M; i++) {
            for (int j = 0; j < TILE_N; j++) {
                int out_idx = (blockIdx.y*TILE_M + i)*N + 
                              (blockIdx.x*TILE_N + j);
                C[out_idx] = accum[i*TILE_N + j];
            }
        }
    }
}
```

### Execution Timeline

```
Timeline: One iteration of K loop

T0-T5:     Producer: mbarrier.arrive.expect_tx, cp.async dispatch
           Consumer: mbarrier.wait [blocked]

T0-T100:   Producer: TMA in flight (50-500 cycles)
           Consumer: Blocked waiting for data

T100:      TMA completes, auto-signals barrier
           Consumer: mbarrier.wait returns

T100-T300: Producer: Loads next batch (TMA 100 cycles)
           Consumer: Computes on current batch (200 cycles)
           ✅ OVERLAP: Load and compute happen concurrently

T300:      Producer finishes current batch, can start next
           Consumer finishes compute, waits for next data

T300-T400: Producer: Loads batch 3
           Consumer: Waits for batch 2 data (just arriving)

Result:
├─ Without specialization: 100 (load) + 200 (compute) = 300 cycles
├─ With specialization: max(100, 200) = 200 cycles per batch
└─ Speedup: 300/200 = **1.5x improvement**
```

**Evidence**: pipeline_example in tma_scheduling_sm90.json:294-307, execution_phases in tma_scheduling_sm90.json:353-387

---

## Weight Stationary Constraint

### Critical Restriction

**CANNOT use weight-stationary MMA with cta_group::2 (producer)**

```c
// Evidence: sub_36E9630_0x36e9630.c:169-170
// Evidence: mma_configuration_impact in warp_specialization_sm90.json:330-337

if (((unsigned __int8)v8 & 3) == 3) {  // weight stationary encoding
    if (this_is_producer_warp) {
        ERROR("cta_group::2 is not supported with weight stationary");
    }
}
```

### Why This Restriction?

**Weight stationary** is an optimization where matrix B weights stay in registers across iterations:

```c
// Weight stationary pattern (allowed in consumer only):
for (int k = 0; k < K; k += TILE_K) {
    // Load B once, reuse across multiple A tiles
    B_regs = load_B_weights();  // 🔑 Stays in registers
    
    for (int m = 0; m < TILE_M; m += 16) {
        A = load_A_tile();
        MMA(C, A, B_regs);  // Use same B repeatedly
    }
}
```

Producer warps **cannot** use this pattern because:
1. Producer is busy dispatching TMA loads (not computing)
2. Can't maintain register state across different operations
3. Optimization incompatible with async copy dispatch requirements

**Implication**: Producer warps use general compute, not dense matrix operations

**Evidence**: weight_stationary_constraint in warp_specialization_sm90.json:330-337, error_message in warp_specialization_sm90.json:335

### Type-Specific Constraints

Certain data types cannot use weight-stationary at all:

```c
// Evidence: type_compatibility in warp_specialization_sm90.json:338-345

Unsupported combinations:
├─ mxf8f6f4 + weight stationary → ERROR
├─ fp4 + weight stationary → ERROR
└─ Code location: sub_36E9630_0x36e9630.c:175
```

---

## Scale Vector Constraints

### Type-Specific Restrictions

```c
// Evidence: scale_vector_constraints in tma_scheduling_sm90.json:272-344
// Evidence: mma_configuration_impact in warp_specialization_sm90.json:346-350

Data Type    | 1X | 2X | 4X | Notes
─────────────|----|----|----|-----------------------------------------
mxf4nvf4     | ❌ | ✅ | ✅ | Cannot use 1X (requires 2X or 4X)
mxf8f6f4     | ✅ | ❌ | ❌ | Cannot use 2X or 4X (only 1X)
mxf4         | ❌ | ✅ | ❌ | Cannot use 1X or 4X (only 2X)
f16          | ✅ | ✅ | ✅ | Most flexible
f8f6f4       | ✅ | ✅ | ✅ | Flexible
tf32         | ✅ | ✅ | ✅ | Flexible
i8           | ✅ | ✅ | ✅ | Flexible
```

### Scale Vector Encoding

```c
// Evidence: scale_vector_configuration in tma_scheduling_sm90.json:272-344

// Scale multiplier encoding (bits 51-53)
enum ScaleVec {
    SCALE_1X = 0x00,  // Default, multiplier 1
    SCALE_2X = 0x01,  // Multiplier 2
    SCALE_4X = 0x03   // Multiplier 4
};

// Used in TMA instruction encoding:
cp.async.bulk.tensor.g2s.scale_vec::1X [dst], [src];
cp.async.bulk.tensor.g2s.scale_vec::2X [dst], [src];
cp.async.bulk.tensor.g2s.scale_vec::4X [dst], [src];

Semantic: Affects tile dimensions for TMA loads
├─ 1X: Base tile size (e.g., 64×64 for FP16)
├─ 2X: 2× dimensions (e.g., 128×128 for FP16)
└─ 4X: 4× dimensions (e.g., 256×256 for FP16)
```

---

## Instruction Opcode Reference

### TMA Operation Opcodes

```c
// Evidence: instruction_opcodes_reference in tma_scheduling_sm90.json:492-522
// Evidence: decompiled/sub_A8E250_0xa8e250.c:1019-1230

Opcode | Instruction                              | Purpose
-------|------------------------------------------|--------------------------------
8315   | cp.async.bulk.global.to.shared.cluster   | Multi-block load
8316   | cp.async.bulk.gmem.to.dsmem              | Distributed shared load
8324   | cp.async.bulk.tensor.gmem.to.smem.f1     | Generic tensor load (1 element)
8325   | cp.async.bulk.tensor.gmem.to.smem.f2     | Generic tensor load (2 elements)
8326   | cp.async.bulk.tensor.gmem.to.smem.f4     | Generic tensor load (4 elements)
8327   | cp.async.bulk.tensor.gmem.to.smem.f8     | Generic tensor load (8 elements)
8328   | cp.async.bulk.tensor.gmem.to.smem.f16    | Generic tensor load (16 elements)
8329   | cp.async.bulk.tensor.gmem.to.smem.im2col.w32  | Image-to-col (32-bit)
8330   | cp.async.bulk.tensor.gmem.to.smem.im2col.w64  | Image-to-col (64-bit)
8331   | cp.async.bulk.tensor.gmem.to.smem.im2col.w128 | Image-to-col (128-bit)
9213   | cp.async.bulk.tensor.g2s.im2col.w32      | TMA im2col (32-bit)
9214   | cp.async.bulk.tensor.g2s.im2col.w64      | TMA im2col (64-bit)
9215   | cp.async.bulk.tensor.g2s.im2col.w128     | TMA im2col (128-bit)
9222   | cp.async.bulk.tensor.g2s.tile.w1         | TMA tile (1X scale)
9223   | cp.async.bulk.tensor.g2s.tile.w2         | TMA tile (2X scale)
9224   | cp.async.bulk.tensor.g2s.tile.w4         | TMA tile (4X scale)
9225   | cp.async.bulk.tensor.g2s.tile.w8         | TMA tile (8X scale)
9226   | cp.async.bulk.tensor.g2s.tile.w16        | TMA tile (16X scale)
```

### Barrier Multicast Opcodes

```c
Opcode | Instruction                        | Scope
-------|-----------------------------------|------------------
10090  | mbarrier.arrive.multicast          | General cluster
10091  | mbarrier.arrive.multicast.shared   | Cluster + shared mem
10095  | mbarrier.arrive.mc.cg1             | Cooperative Group 1
10096  | mbarrier.arrive.mc.cg2             | Cooperative Group 2
10097  | mbarrier.arrive.mc.shared.cg1      | Shared + CG1
10098  | mbarrier.arrive.mc.shared.cg2      | Shared + CG2
```

---

## Cluster-Scope Extensions

### Multi-Block Coordination

SM90 extends warp specialization to cluster-level (up to 8 blocks):

```c
// Evidence: cluster_scope_extensions in warp_specialization_sm90.json:353-402

// Block 0 (Producer):
cp.async.bulk.global.to.shared.cluster [block0_smem], [global_data];
mbarrier.arrive.multicast.shared [barrier], blocks_mask;

// Blocks 1-7 (Consumers):
mbarrier.wait.shared [barrier];  // Block on barrier
// Now data from Block 0 visible in distributed shared memory

Scope options:
├─ .cluster: All 8 blocks in cluster
├─ .shared::cluster: Cluster with shared memory visibility
├─ .shared::cta: Block-local (traditional __syncthreads)
└─ Various cooperative group scopes (CG1, CG2)

Cluster query:
├─ cluster.get.rank() → Which block am I in cluster? (0-7)
├─ cluster.sync() → Synchronize all blocks in cluster
└─ __nvvm_read_cluster_info_32() → Cluster dimension queries
```

**Evidence**: cluster_scope_extensions in warp_specialization_sm90.json:353-402, cluster_instructions in WARP_SPECIALIZATION_SM90_ANALYSIS.md:282-290

---

## Known Limitations and Unknowns

### Hard Constraints (Compile Errors)

```c
// Evidence: known_limitations_and_constraints in warp_specialization_sm90.json:587-616

1. cta_group::2 + Weight Stationary = ERROR
   Code: sub_36E9630:170
   Message: "cta_group::2 is not supported with weight stationary"

2. mxf8f6f4/fp4 + Weight Stationary = ERROR
   Code: sub_36E9630:175
   Cannot use weight stationary with these types

3. Scale Vector Type Incompatibilities
   mxf4nvf4: cannot use 1X
   mxf8f6f4: cannot use 2X or 4X
   mxf4: cannot use 1X or 4X
```

### Algorithmic Unknowns

```c
// Evidence: algorithmic_unknowns in warp_specialization_sm90.json:609-615
// Evidence: confidence_gaps in warp_specialization_sm90.json:536-542

- Exact heuristics for when compiler enables warp specialization
- Threshold metrics for cost-benefit analysis
- Register pressure estimation method
- Shared memory allocation policy for multiple buffers
- Barrier count per kernel determination
- Pipeline depth selection algorithm
- Descriptor initialization and optimization
- Dynamic load balancing between producer and consumer
```

---

## Validation and SM90 Specificity

### SM90-Exclusive Features

```c
// Evidence: sm90_exclusive_features in warp_specialization_sm90.json:517-524
// Evidence: validation_and_sm90_specificity in warp_specialization_sm90.json:516-543

Features only in SM90 (Hopper):
├─ Explicit .cta_group::1 and .cta_group::2 partitioning
├─ expect_tx barrier operations for async coordination
├─ cp.async.bulk.tensor family instructions with TMA
├─ Cluster-scope synchronization primitives
├─ tcgen05 (Tensor Core Generation 05) instructions
├─ Weight stationary mode constraints
└─ Explicit warp specialization compiler support

NOT in earlier SM:
├─ SM80 (Ampere): Manual sync patterns, no cta_group, no TMA
├─ SM75 (Turing): No tensor acceleration, no async support
└─ SM70 (Volta): Pre-dates structured warp specialization
```

### Verification Evidence

```c
Extracted artifacts confirm SM90 specificity:

✓ All string constants found:
  ├─ ".cta_group::1"
  ├─ ".cta_group::2"
  ├─ ".mbarrier::expect_tx"
  ├─ "cp.async.bulk.tensor.g2s"
  └─ "cluster.get.rank"

✓ Decompiled code locations verified:
  ├─ sub_35F3330: Warp group assignment
  ├─ sub_35F4080: Barrier operation codegen
  ├─ sub_A8E250: TMA instruction patterns
  └─ sub_36E9630: Constraint validation

✓ Bitfield logic verified (bit 1 for cta_group)
✓ Operation codes extracted and mapped
✓ Barrier primitives and scopes documented

Confidence: MEDIUM-HIGH for extracted mechanisms
           MEDIUM for compiler heuristics
```

---

## Summary of Technical Implementation

| Component | Evidence Location | Key Details |
|-----------|------------------|-------------|
| **Warp Assignment** | sub_35F3330:85-111 | Bit 1 of result → cta_group decision |
| **TMA Operations** | sub_A8E250:1019-1230 | 13 distinct instruction variants |
| **Barrier expect_tx** | sub_35F4080:138-144 | Operation code 0x4, critical for async |
| **Producer Flow** | JSON warp_specialization | Load → expect_tx → commit → next |
| **Consumer Flow** | JSON warp_specialization | wait → compute → repeat |
| **Performance** | tma_scheduling_sm90.json | 20-50% improvement, 1.5-2x speedup possible |
| **Constraints** | sub_36E9630:165-180 | Weight stationary forbidden for producer |
| **Cluster Scope** | JSON + ANALYSIS.md | Multi-block support, 8-block clusters |

---

**Document Status**: Complete technical implementation specification
**Last Updated**: November 17, 2025
**Confidence Level**: MEDIUM-HIGH (mechanisms), MEDIUM (heuristics)
**Sources**: 5 decompiled files, 2 analysis documents, 2 JSON specifications
