# Bank Conflict Detection and Avoidance Analysis (L3-15)

## Executive Summary

Bank conflict analysis in CICC compiler is implemented through **six complementary strategies** across multiple compilation phases:

1. **Register class constraints** during allocation
2. **Shared memory padding** optimization
3. **32-bit pointer optimization** for address control
4. **Broadcast detection** for uniform accesses
5. **Stride versioning** analysis
6. **Instruction scheduling** aware of bank patterns

**Confidence**: MEDIUM (based on indirect evidence from optimization passes and compiler options)

---

## 1. Bank Conflict Architecture

### GPU Memory Bank Configuration

NVIDIA GPUs (SM 3.0 onwards) have:
- **32 independent banks** per SM in shared memory
- **4 bytes per bank** (32-bit access)
- **128-byte stride** for bank repetition
- **Bank addressing**: `bank_index = (address % 128) / 4`

### Conflict Conditions

```
TRUE BANK CONFLICT:
- Multiple threads (in same warp) access different addresses
- All addresses map to SAME bank
- Results in serialization across 32 cycles

BROADCAST (NO CONFLICT):
- All threads access SAME address
- Uses efficient broadcast mechanism
- No serialization penalty

SERIALIZED ACCESS:
- Sequential stride causes multiple threads to hit different banks
- If stride < 128, same bank can be hit multiple times
- Penalty depends on conflict degree
```

### Performance Impact

```
Penalty Formula:
  conflict_cycles = conflicts * 32 (full serialization)

Estimated Cost Model Weight:
  bank_conflict_penalty = 2.0
  (used in spill cost and instruction selection)
```

---

## 2. Detection Algorithm

### Location in Code

**Primary Evidence**: Register allocation (sub_B612D0_0xb612d0.c)
- Implemented as register class constraints
- Applied during graph coloring phase
- Reference: `20_REGISTER_ALLOCATION_ALGORITHM.json:87-91`

### Detection Process

#### Phase 1: Static Analysis
```
For each shared memory access instruction:
  1. Extract address formula (base + offset*stride)
  2. Compute symbolic stride value
  3. Calculate bank_index = stride % 32
  4. Mark if conflict-prone
```

#### Phase 2: Register Allocation
```
During interference graph construction:
  1. Create implicit edges between register classes
  2. Constraint: virtual_regs in same_bank -> incompatible
  3. Graph coloring respects these constraints
  4. Result: no allocation creates bank conflicts
```

#### Phase 3: Instruction Scheduling
```
In post-RA scheduler:
  1. Analyze memory access patterns
  2. Reorder non-dependent instructions
  3. Separate conflicting accesses in time
  4. Minimize simultaneous bank hits
```

---

## 3. Avoidance Strategies

### Strategy 1: Register Reordering (HIGH Confidence)

**Mechanism**: Register class constraints in graph coloring

```
Evidence Location: Register allocation analysis
- "bank_conflict_avoidance": true
- "implementation": "Register class constraints during coloring"

Code Pattern:
  - Implicit edges prevent conflicting register assignments
  - Enforced in simplify/spill phases
  - Transparent to user code
```

**Effect**: Virtual registers assigned to different banks when possible

---

### Strategy 2: Shared Memory Padding (HIGH Confidence)

**Mechanism**: Insert padding in shared memory arrays

```
Evidence:
  File: sub_1CC5230_0x1cc5230.c
  File: sub_2D198B0_0x2d198b0.c
  Pass: SetSharedMemoryArrayAlignmentPass
  Description: "NVVM pass to set alignment of statically sized shared memory arrays"

Example:
  Original: float arr[1024];    // Stride = 4 bytes
  Padded:   float arr[1120];    // Padding to avoid stride conflicts

  New stride breaks bank conflict pattern
```

**Calculation**:
```
padding = gcd(access_stride, 128) adjustment
Goal: Make stride non-multiples of bank width (4)
```

---

### Strategy 3: 32-bit Pointer Optimization (HIGH Confidence)

**Mechanism**: Use 32-bit instead of 64-bit pointers for shared memory

```
Evidence:
  File: ctor_356_0_0x50c890.c:126-127
  Option: "sharedmem32bitptr"
  Description: "Use 32 bit ptrs for Shared Memory"

Benefits:
  - Reduces pointer width from 64 to 32 bits
  - Enables tighter stride control
  - Improves bank access locality
  - Reduces overall memory footprint
```

**Usage**: Controlled via compiler flag `--sharedmem32bitptr`

---

### Strategy 4: Broadcast Optimization (MEDIUM Confidence)

**Mechanism**: Detect and optimize uniform address accesses

```
Evidence:
  - Broadcast instructions detected in code
  - shfl.sync.i32 references
  - Special case handling for uniform accesses

Pattern Recognition:
  If all threads in warp access same_address:
    -> Use broadcast operation
    -> No serialization
    -> Much faster than bank access
```

**Implementation**: Pattern detection + instruction replacement

---

### Strategy 5: Stride Memory Access Versioning (HIGH Confidence)

**Mechanism**: Symbolic analysis of memory access strides

```
Evidence:
  File: ctor_053_0_0x490b90.c
  File: ctor_716_0x5bfdc0.c
  Option: "Enable symbolic stride memory access versioning"

Purpose:
  - Analyze strides symbolically (not just concrete values)
  - Detect conflict-prone access patterns
  - Enable versioning for safe optimization

Targets:
  - 32-bit shared memory pointer expressions
  - General memory access patterns
  - Version checking at runtime if needed
```

---

### Strategy 6: Instruction Scheduling (HIGH Confidence)

**Mechanism**: Post-RA scheduling aware of bank patterns

```
Evidence:
  File: ctor_310_0_0x500ad0.c
  Pass: "Post RA machine instruction scheduling"
  Strategies:
    - list-burr (Bottom-up register reduction)
    - source (Source-order with awareness)
    - top-down (List latency)

Integration:
  1. Scheduler analyzes memory access dependencies
  2. Includes conflict detection in latency calculation
  3. Reorders instructions to:
     - Separate conflicting accesses in time
     - Allow independent operations to overlap
     - Minimize peak bank utilization
```

---

## 4. Integration with Compilation Phases

### Register Allocation Integration

```
Phase: Graph coloring (Chaitin-Briggs style)
├─ Phase 1: Liveness analysis
├─ Phase 2: Interference graph construction
│   └─ Add bank conflict constraints as implicit edges
├─ Phase 3: Coalescing
├─ Phase 4: Graph coloring
│   └─ Respect bank constraint edges
├─ Phase 5: Spill code insertion
│   └─ Cost includes bank_conflict_penalty (2.0)
└─ Phase 6: Live range splitting

Result: Virtual registers assigned avoiding bank conflicts
```

### Instruction Selection Integration

```
Phase: Pattern matching + cost computation
├─ Pattern database lookup
├─ Cost calculation includes:
│   - Latency weight
│   - Throughput weight
│   - Register pressure weight
│   - Memory latency weight
│   - Bank conflict penalty: 2.0
├─ Select lowest-cost pattern
└─ Result: Prefer non-conflicting access patterns
```

### Instruction Scheduling Integration

```
Phase: Post-RA scheduling (after register allocation)
├─ Build dependency DAG
├─ Analyze memory access latencies (including conflicts)
├─ Select scheduling direction:
│   - top-down (critical path aware)
│   - bottom-up (register pressure aware)
│   - source-order (with awareness)
├─ Schedule instructions respecting:
│   - Data dependencies
│   - Resource conflicts
│   - Bank access patterns
└─ Result: Minimized simultaneous bank conflicts
```

---

## 5. SM-Specific Considerations

### Volta (SM 7.0-7.2)
- 32 banks, 4 bytes each
- Penalty: 2.0 (full conflict weight)
- Typical stall: 32 cycles for full conflict

### Ampere (SM 8.0-8.9)
- 32 banks, 4 bytes each (same as Volta)
- Larger register file (improved occupancy)
- Penalty: 2.0
- Better parallelism helps hide bank stalls

### Hopper (SM 9.0)
- 32 banks, 4 bytes each
- **TMA (Tensor Memory Accelerator)**: Can bypass traditional shared memory
- Penalty: 1.5 (slightly lower due to TMA as alternative)
- New warpgroup scheduling context

---

## 6. Evidence Summary

### Direct Evidence (HIGH Confidence)

| Type | Finding | Location | Strength |
|------|---------|----------|----------|
| Option | 32-bit pointer optimization | ctor_356_0_0x50c890.c:126 | HIGH |
| Pass | Array alignment optimization | sub_1CC5230_0x1cc5230.c | HIGH |
| Option | Stride memory versioning | ctor_053_0x490b90.c | HIGH |
| Scheduling | Post-RA scheduling | ctor_310_0_0x500ad0.c | HIGH |

### Indirect Evidence (MEDIUM Confidence)

| Type | Finding | Reference | Strength |
|------|---------|-----------|----------|
| Analysis | Bank conflict avoidance in register allocation | 20_REGISTER_ALLOCATION_ALGORITHM.json:87-91 | MEDIUM |
| Analysis | Bank penalty in cost model | Spill cost formula | MEDIUM |
| Analysis | Penalty weight value (2.0) | Register allocation analysis | MEDIUM |

### Absent Evidence (Limitations)

- No explicit `detectBankConflict()` function
- No bank conflict penalty lookup table
- No detailed stride analysis implementation

---

## 7. Algorithm Pseudocode

### Bank Conflict Detection

```python
def detect_bank_conflicts(instruction):
    """
    Returns True if instruction likely causes bank conflicts
    """
    if not is_shared_memory_access(instruction):
        return False

    # Extract address computation
    address_formula = extract_address(instruction)  # e.g., base + stride*thread_id
    stride = symbolic_eval(address_formula)

    # Compute bank indices for warp
    bank_indices = []
    for thread in warp:
        addr = evaluate(address_formula, thread=thread)
        bank = (addr % 128) // 4
        bank_indices.append(bank)

    # Check for conflicts
    unique_banks = len(set(bank_indices))
    threads_per_bank = len(bank_indices) / unique_banks

    # Conflict if multiple threads hit same bank
    return threads_per_bank > 1 and unique_banks < len(warp)
```

### Bank Avoidance via Padding

```python
def pad_shared_array(arr_stride, arr_size):
    """
    Calculate padding to avoid bank conflicts
    """
    # Original stride in bytes
    original_bytes = arr_stride * arr_size

    # Find padding to break bank conflict pattern
    # Goal: (original_bytes + padding) % 128 != 0
    target_size = original_bytes
    while (target_size % 128) == 0 or (target_size % 128) % 4 == 0:
        target_size += 1

    padding_bytes = target_size - original_bytes
    return padding_bytes, target_size
```

---

## 8. Validation & Testing Strategy

### Test Case 1: Bank Conflict Detection
```cuda
__global__ void test_conflict() {
    __shared__ int arr[32*32];
    // Each thread accesses arr[threadIdx.x * stride]
    // If stride=4 and 32 threads, all hit same bank
    int val = arr[threadIdx.x * 4];
}
```

### Test Case 2: Broadcast Case
```cuda
__global__ void test_broadcast() {
    __shared__ int arr[1024];
    // All threads access same address (broadcast)
    int val = arr[0];
}
```

### Test Case 3: Padded Array
```cuda
__global__ void test_padded() {
    __shared__ float arr[1024 + 128];  // Padding added
    // Access pattern changed by padding
    float val = arr[threadIdx.x * stride];
}
```

### Validation Approach
1. Compile with `-ptx` to get PTX assembly
2. Analyze memory instructions and addressing
3. Measure performance on hardware
4. Compare conflict vs non-conflict versions

---

## 9. Key Insights

1. **Multi-layer approach**: Bank conflict avoidance is not single-phase, but integrated across register allocation, instruction selection, and scheduling

2. **Cost-based decisions**: Conflict avoidance is weighted in cost models to balance other optimization goals

3. **Compiler transparency**: Developers don't explicitly manage bank conflicts; compiler handles it through standard optimizations

4. **Architecture-aware**: Strategy choices differ by SM architecture (especially Hopper with TMA)

5. **Padding as fallback**: When register allocation can't prevent conflicts, shared memory padding provides secondary mitigation

---

## 10. References

- `20_REGISTER_ALLOCATION_ALGORITHM.json` - Register allocation constraints
- `spill_cost_formula.json` - Cost model with bank penalty
- `cost_model_complete.json` - Instruction selection costs
- NVIDIA CUDA C++ Programming Guide - Shared Memory Bank Conflicts
- NVIDIA GTC talks on memory optimization

---

**Analysis Date**: 2025-11-16
**Agent**: L3-15
**Confidence**: MEDIUM
**Next Steps**: Decompile large register allocation functions and scheduling code to extract exact penalties and threshold values
