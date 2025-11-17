# NVPTX Atomic Lowering

**Pass Type**: Atomic operation lowering pass
**LLVM Class**: `llvm::NVPTXAtomicLower`
**Category**: Atomic Operation / Memory Synchronization
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Inferred from atomic patterns
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

NVPTXAtomicLower transforms generic LLVM atomic operations into NVIDIA PTX-specific atomic instructions, handling different address spaces, data types, and SM architecture capabilities. This pass must account for:
- Address space differences (global, shared, generic)
- SM architecture support (different atomic operations per SM version)
- Data type limitations (not all types supported atomically)
- Memory ordering semantics

**Key Purpose**: Convert abstract atomic operations into hardware-supported PTX atomic instructions.

---

## LLVM Atomics vs PTX Atomics

### LLVM Atomic Operations

**Generic LLVM IR**:
```llvm
%old = atomicrmw add i32* %ptr, i32 1 seq_cst
%old = atomicrmw xchg i64* %ptr, i64 %val acq_rel
%success = cmpxchg i32* %ptr, i32 %expected, i32 %new seq_cst
```

### PTX Atomic Instructions

**PTX Syntax**:
```ptx
atom.global.add.u32 %old, [%addr], 1;
atom.shared.exch.u64 %old, [%addr], %val;
atom.global.cas.b32 %old, [%addr], %expected, %new;
```

**Key Differences**:
- Explicit address space (`.global`, `.shared`, `.generic`)
- Explicit scope (`.cta`, `.gpu`, `.sys`)
- Limited type support
- SM version dependencies

---

## Atomic Operation Types

### Supported Operations (SM 2.0+)

| LLVM Operation | PTX Instruction | Types | Address Spaces |
|----------------|-----------------|-------|----------------|
| `atomicrmw add` | `atom.add` | i32, u32, u64 | global, shared |
| `atomicrmw sub` | `atom.add` (negative) | i32, u32 | global, shared |
| `atomicrmw xchg` | `atom.exch` | i32, u32, u64 | global, shared |
| `atomicrmw and` | `atom.and` | i32, u32, u64 | global, shared |
| `atomicrmw or` | `atom.or` | i32, u32, u64 | global, shared |
| `atomicrmw xor` | `atom.xor` | i32, u32, u64 | global, shared |
| `atomicrmw max` | `atom.max` | i32, u32, i64 | global, shared |
| `atomicrmw min` | `atom.min` | i32, u32, i64 | global, shared |
| `cmpxchg` | `atom.cas` | i32, u32, u64 | global, shared |

### Floating-Point Atomics (SM 6.0+)

| Operation | PTX Instruction | Types | SM Version |
|-----------|-----------------|-------|------------|
| `atomicrmw fadd` | `atom.add.f32` | f32 | SM 6.0+ |
| `atomicrmw fadd` | `atom.add.f64` | f64 | SM 6.0+ |

**Note**: Earlier SM versions must emulate using `atom.cas`.

---

## Lowering Algorithm

### Phase 1: Classify Atomic Operations

```
FOR each AtomicInst AI in Function:
    Operation = AI.getOperation()  // add, sub, xchg, etc.
    Type = AI.getType()           // i32, i64, f32, etc.
    AddrSpace = AI.getPointerAddressSpace()
    Ordering = AI.getOrdering()   // seq_cst, acquire, etc.

    LoweringStrategy = selectStrategy(Operation, Type, AddrSpace, SMVersion)
    LoweringQueue.add({AI, LoweringStrategy})
```

### Phase 2: Select PTX Instruction

```
selectStrategy(Op, Type, AS, SM):
    IF hasNativeSupport(Op, Type, AS, SM):
        RETURN NATIVE_ATOMIC

    ELSE IF canEmulatWithCAS(Op, Type):
        RETURN CAS_EMULATION

    ELSE:
        RETURN LOCK_EMULATION  // Last resort
```

### Phase 3: Perform Lowering

**Native Atomic**:
```
FOR each (AI, NATIVE_ATOMIC) in LoweringQueue:
    PTXIntrinsic = getNativePTXIntrinsic(AI)
    NewCall = createIntrinsicCall(PTXIntrinsic, AI.getArgs())
    AI.replaceAllUsesWith(NewCall)
```

**CAS Emulation**:
```
FOR each (AI, CAS_EMULATION) in LoweringQueue:
    EmulatedLoop = generateCASLoop(AI)
    AI.replaceWith(EmulatedLoop)
```

---

## Transformation Examples

### Example 1: Simple Integer Atomic Add

**CUDA Source**:
```cuda
__global__ void kernel(int* counter) {
    atomicAdd(counter, 1);
}
```

**LLVM IR**:
```llvm
define void @kernel(i32* %counter) {
  %old = atomicrmw add i32* %counter, i32 1 seq_cst
  ret void
}
```

**After Lowering**:
```llvm
define void @kernel(i32 addrspace(1)* %counter) {
  %old = call i32 @llvm.nvvm.atomic.add.gen.i.i32.p1i32(
      i32 addrspace(1)* %counter, i32 1
  )
  ret void
}
```

**PTX**:
```ptx
.func kernel(.param .u64 counter_param) {
    .reg .u64 %rd_counter;
    .reg .u32 %r_old;

    ld.param.u64 %rd_counter, [counter_param];
    atom.global.add.u32 %r_old, [%rd_counter], 1;
    ret;
}
```

### Example 2: Floating-Point Atomic Add (SM 6.0+)

**CUDA Source**:
```cuda
__global__ void kernel(float* sum) {
    atomicAdd(sum, 3.14f);
}
```

**LLVM IR**:
```llvm
define void @kernel(float* %sum) {
  %old = atomicrmw fadd float* %sum, float 3.14 seq_cst
  ret void
}
```

**After Lowering (SM 6.0+)**:
```llvm
define void @kernel(float addrspace(1)* %sum) {
  %old = call float @llvm.nvvm.atomic.add.gen.f.f32.p1f32(
      float addrspace(1)* %sum, float 3.14
  )
  ret void
}
```

**PTX (SM 6.0+)**:
```ptx
atom.global.add.f32 %f_old, [%rd_sum], 0f40490fdb;  // 3.14
```

### Example 3: Atomic Add Emulation (SM < 6.0)

**For SM < 6.0, float atomics must be emulated with CAS**:

**After Lowering (SM < 6.0)**:
```llvm
define void @kernel(float addrspace(1)* %sum) {
entry:
  br label %cas_loop

cas_loop:
  %old_bits = load i32, i32 addrspace(1)* %sum_as_int, atomic seq_cst
  %old_float = bitcast i32 %old_bits to float
  %new_float = fadd float %old_float, 3.14
  %new_bits = bitcast float %new_float to i32

  %cas_result = cmpxchg i32 addrspace(1)* %sum_as_int,
                         i32 %old_bits, i32 %new_bits seq_cst seq_cst
  %success = extractvalue { i32, i1 } %cas_result, 1
  br i1 %success, label %done, label %cas_loop

done:
  ret void
}
```

**PTX (SM < 6.0)**:
```ptx
CAS_LOOP:
    ld.global.u32 %r_old, [%rd_sum];      // Load current value
    mov.b32 %f_old, %r_old;               // Reinterpret as float
    add.f32 %f_new, %f_old, 0f40490fdb;   // Add 3.14
    mov.b32 %r_new, %f_new;               // Reinterpret as int

    atom.global.cas.b32 %r_cas, [%rd_sum], %r_old, %r_new;
    setp.eq.u32 %p, %r_cas, %r_old;       // Check if CAS succeeded
    @!%p bra CAS_LOOP;                    // Retry if failed
```

### Example 4: Shared Memory Atomic

**CUDA Source**:
```cuda
__global__ void kernel() {
    __shared__ int shared_counter;
    atomicAdd(&shared_counter, 1);
}
```

**After Lowering**:
```llvm
%old = call i32 @llvm.nvvm.atomic.add.shared.i.i32.p3i32(
    i32 addrspace(3)* %shared_counter, i32 1
)
```

**PTX**:
```ptx
atom.shared.add.u32 %r_old, [shared_counter], 1;
```

---

## Address Space Handling

### Address Space-Specific Intrinsics

| Address Space | PTX Space | NVVM Intrinsic Suffix |
|---------------|-----------|----------------------|
| 0 (generic) | `.generic` | `.gen` |
| 1 (global) | `.global` | `.global` |
| 3 (shared) | `.shared` | `.shared` |

**Example**:
```llvm
; Global atomic
call i32 @llvm.nvvm.atomic.add.global.i.i32.p1i32(...)

; Shared atomic
call i32 @llvm.nvvm.atomic.add.shared.i.i32.p3i32(...)

; Generic atomic (runtime address space resolution)
call i32 @llvm.nvvm.atomic.add.gen.i.i32.p0i32(...)
```

---

## Memory Ordering

### LLVM Memory Orderings

| LLVM Ordering | PTX Scope | Use Case |
|---------------|-----------|----------|
| `monotonic` | `.relaxed` | No synchronization |
| `acquire` | `.cta` | Thread block sync |
| `release` | `.cta` | Thread block sync |
| `acq_rel` | `.cta` | Thread block sync |
| `seq_cst` | `.gpu` | Device-wide sync |

**PTX Syntax**:
```ptx
atom.global.add.u32 %old, [%addr], 1;        // Monotonic
atom.global.acq_rel.add.u32 %old, [%addr], 1; // Acq/Rel
atom.global.sys.add.u32 %old, [%addr], 1;    // System-wide
```

---

## Performance Considerations

### Atomic Contention

**High contention** (many threads updating same location):
- Serialization overhead
- Performance degrades linearly with contention

**Mitigation**:
- Use shared memory for thread-block local reductions
- Tree-based reduction patterns

### Warp-Level Atomics

**SM 7.0+**: Warp-level reduction intrinsics
```cuda
int val = __reduce_add_sync(0xffffffff, local_val);
if (lane_id == 0) {
    atomicAdd(global_counter, val);  // Only one atomic per warp
}
```

---

## Related Passes

1. **NVPTXGenericToNVVM**: Creates initial atomic intrinsics
2. **MemorySpaceOptimization**: Determines address spaces
3. **Instruction Selection**: Maps to PTX instructions
4. **NVPTXFavorNonGenericAddrSpaces**: Converts generic → specific atomics

---

## Summary

NVPTXAtomicLower converts LLVM atomic operations into PTX atomic instructions, handling address spaces, types, and SM capabilities.

**Critical for**: Synchronization, concurrent data structures
**Performance Impact**: Correct lowering critical for performance
**Reliability**: Essential for correctness
