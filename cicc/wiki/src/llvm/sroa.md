# SROA (Scalar Replacement of Aggregates)

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

SROA is the single most important early-pipeline optimization for NVIDIA GPU compilation. Every `alloca` instruction that survives into code generation is lowered to `.local` memory (NVPTX address space 5) -- physically backed by device DRAM and accessed through the L1/L2 cache hierarchy. A `.local` access that misses L1 costs 200-400 cycles; a register read costs zero. A single un-promoted alloca in a hot loop can degrade kernel throughput by 10-50x. SROA's job is to decompose aggregate allocas (structs, arrays, unions) into individual scalar SSA values that the register allocator can place in registers, eliminating the memory traffic entirely.

| Property | Value |
|----------|-------|
| Pass name | `"sroa"` |
| Pipeline parser params | `preserve-cfg`, `modify-cfg` |
| Entry function | `sub_2935C30` (`runOnAlloca`) |
| Core function | `sub_2930B90` (`splitAlloca`) |
| Binary footprint | ~138 KB primary (80 KB + 58 KB), ~200 KB secondary (legacy PM) |
| Binary address range | `0x2910000`-`0x293FFFF` (178 functions) |
| Pipeline positions | Position 4 (early, after NVVMReflect) and post-sinking (late) |
| Disable flag | `NVVMPassOptions` offset `+1400` |
| Size threshold knob | `qword_50056C8` (max alloca size in bits) |
| Two-pass flag | `qword_50055E8` (enables pre-analysis for new PM) |
| NVIDIA modifications | None to core algorithm |
| Upstream source | `llvm/lib/Transforms/Scalar/SROA.cpp` |

## Why SROA Is Existential on GPU

On a CPU, an alloca that cannot be promoted to a register lives on the stack -- a cached, low-latency memory region with typical access times of 1-4 cycles. On an NVIDIA GPU there is no hardware stack cache: every surviving alloca becomes a `.local` allocation backed by DRAM with [200-800 cycle latency](../gpu-execution-model.md#memory-hierarchy) on cache miss versus zero for a register. See the [GPU Execution Model memory hierarchy table](../gpu-execution-model.md#latency-table) for per-tier latencies.

Every alloca that survives SROA becomes a `.local` allocation. The NVPTX backend emits these as frame objects in the `NVPTXFrameLowering::emitPrologue` path, and ptxas maps them to per-thread local memory. Because [occupancy](../gpu-execution-model.md#register-pressure-and-occupancy) is bounded by register count per SM, and `.local` spills effectively consume both registers (for the address) and memory bandwidth, the performance impact compounds.

The pipeline runs SROA twice: once early (position 4, immediately after NVVMReflect) to eliminate allocas before any other transform sees them, and once late (after NVVMCustomSinking2 and BreakCriticalEdges) to catch allocas created or exposed by loop unrolling, inlining, and other mid-pipeline transforms. The early invocation handles the common case (byval parameter copies, local struct variables); the late invocation cleans up whatever the loop optimizer and sinking passes left behind.

## The `isAllocaPromotable` Fast Path

Before performing any splitting, `runOnAlloca` checks whether the alloca is trivially promotable via `sub_B4CE70` (`isAllocaPromotable`). An alloca is promotable if every use is a simple load or store with no address-taken escape -- the same criterion as `mem2reg`. When this returns true, SROA marks the alloca for `mem2reg` and returns without performing any slice analysis or splitting. This fast path avoids the O(n) slice-building cost for the vast majority of CUDA local variables (scalar `int`, `float`, simple pointers), which are already simple enough for `mem2reg` to handle directly.

## Algorithm: `runOnAlloca` (`sub_2935C30`)

The top-level per-alloca entry point. Validates the alloca as a candidate, builds the partition/slice table, and delegates to `splitAlloca` for the actual transformation.

### Phase 1: Candidate Validation

```
runOnAlloca(state, alloca):
    if alloca has no users:
        eraseFromParent(alloca)
        return

    if isAllocaPromotable(alloca):
        defer to mem2reg
        return

    type = getAllocatedType(alloca)
    type_byte = getTypeID(type)

    // Accept: integers(3), half(4), bfloat(5), float(6),
    //         pointers(10), vectors(11), arrays(12), structs(15-18, 20)
    // Reject structs/composites unless isVectorType returns true
    if type_byte not in {3,4,5,6,10,11,12,15,16,17,18,20}:
        return
    if type_byte in {15,16,17,18,20} and not isVectorType(type):
        return  // function types, labels, etc.

    size = getTypeSizeInBits(type)   // sub_BDB740
    if size > qword_50056C8:         // SROA size threshold
        return  // alloca too large, leave for backend
```

The size threshold at `qword_50056C8` is a global tuning knob, likely controlled by the `sroa<preserve-cfg>` / `sroa<modify-cfg>` pipeline parameter. Allocas larger than this threshold are left untouched; the backend will lower them to `.local` memory. The exact default is not exposed in the binary's constructor initializers, but upstream LLVM uses a default of 128 bytes (1024 bits) for the `sroa-threshold` flag.

### Phase 2: Use Analysis and Slice Building

```
    metadata = buildMetadataTable(alloca)   // sub_D5F1F0

    if qword_50055E8:                       // two-pass mode
        buildSlices(state, alloca, 1)       // sub_2927160 — pre-analysis
        slices = buildPartitions(state)     // sub_2924690
    else:
        slices = buildPartitions(state)     // single-pass
```

`buildSlices` (`sub_2927160`) walks all users of the alloca, classifying each use as a "slice" -- a byte range `[start, end)` with associated flags. Each slice is a 24-byte entry:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | `start` (byte offset into alloca) |
| +8 | 8 | `end` (byte offset, exclusive) |
| +16 | 8 | `flags` -- bit 2 = splittable, bits [63:3] = user instruction metadata pointer |

`buildPartitions` (`sub_2924690`) groups non-overlapping slices into partitions. Each partition represents a contiguous byte range that can be replaced by a single sub-alloca. Overlapping slices are merged; slices that cross partition boundaries are marked as "unsplittable."

The two-pass flag (`qword_50055E8`) enables a pre-analysis pass that runs `buildSlices` first with a "dry-run" mode to count slices and pre-allocate arrays, then runs the actual partition builder. This is the new PM (PassManager) style -- the legacy PM code path at `0x1A10000` does a single pass.

### Phase 3: Contiguous Slice Merging

After building slices, `runOnAlloca` scans for contiguous ranges that share the same base type and can be merged:

```
    for each group of contiguous slices:
        if all loads/stores in group use the same type:
            if none are volatile (isVolatile check via sub_B46500):
                if all are in-bounds (byte +2, bit 0):
                    mergeSlices(group)   // sub_11D2BF0 + sub_11D3120 + sub_11D7E80
```

This optimizer/merger reduces redundant slices before the splitting phase. For example, if a 16-byte struct has four contiguous 4-byte `i32` loads, the merger can combine them into a single slice covering the full struct, which may then map to a single `<4 x i32>` register rather than four separate scalar registers.

### Phase 4: Dead Instruction Processing

```
    for each dead instruction found during analysis:
        for each operand:
            addToWorklist(operand)         // sub_29220F0
        replaceAllUsesWith(undef)          // sub_BD84D0 + sub_ACADE0
        eraseFromParent(instruction)       // sub_BD60C0
```

Dead instructions identified during slice building (stores to never-loaded ranges, loads of write-only ranges) are removed immediately, before the splitting phase begins.

### Phase 5: Recursive Splitting

```
    if slices is non-empty:
        splitAlloca(state, alloca, slices)  // sub_2930B90 — recursive
```

This is the key: `splitAlloca` may create new sub-allocas that are themselves candidates for further splitting. The newly created sub-allocas are added to the worklist and processed in stack order (LIFO).

### Phase 6-8: Post-Split Processing

After splitting, `runOnAlloca` processes newly created sub-allocas (56-byte records stored in a `SmallVector` with 2-element inline buffer), rewrites per-sub-alloca slice lists, and returns a two-byte result: byte 0 = changed flag, byte 1 = re-run needed flag.

## Algorithm: `splitAlloca` (`sub_2930B90`)

The core splitting function. Given a partitioned alloca and its use-slices, it creates new sub-allocas and rewrites all users.

### Phase 1: Pre-Filter Slices

Iterates the 24-byte slice array. For slices whose instruction is a load (opcode 61) or store (opcode 62) of a simple scalar type that fits entirely within the alloca boundary, clears the "splittable" bit (flag & 4). This prevents unnecessary splitting of trivial accesses -- a scalar `i32` load from an `i32` alloca does not need splitting. If any slices were de-flagged, calls `sortSlices` (`sub_2912200`) and `compactSlices` (`sub_2915A90` / `sub_2914CE0`) to remove the now-redundant entries.

### Phase 2: Partition Iteration

`buildPartitionTable` (`sub_2913C40`) produces a partition list from the sorted slices. Each partition is a local tuple `[start, end, first_slice_ptr, last_slice_ptr]`. The main loop advances through partitions via `sub_2912870` (`advancePartitionIterator`).

### Phase 3: Find Rewrite Target

For each partition `[start, end)`:

1. Get the `DataLayout` via `sub_B43CC0` (`getDL`).
2. If the partition contains only unsplittable slices, call `findExistingValue` (`sub_291A860`) to search for an existing SSA value that already covers `[start, end)`. If found, reuse it instead of creating a new alloca.
3. Otherwise, scan slices for a single dominating load or store. Dispatch on opcode:
   - **61 (load)**: extract the loaded type.
   - **62 (store)**: extract the stored value type from the store's value operand.
   - **85 (intrinsic)**: memcpy/memset/memmove -- follow the pointer chain to determine the affected type.
4. Compare type sizes via `getTypeSizeInBits` (`sub_BDB740`).
5. If no suitable existing value, create a new alloca via `CreateAlloca` (`sub_BCD420`) or `CreateBitCast` (`sub_BCD140`).

### Phase 4: Size and Alignment Check

```
    alloc_size = getTypeAllocSize(partition_type)    // sub_9208B0
    if alloc_size > 0x800000:                        // 8 MB sanity limit
        skip partition

    // Verify rewrite target matches partition size (8-byte aligned)
    if match:
        checkTypeCompatibility(both_directions)      // sub_29191E0
        validateUnsplittableSlices(partition)         // sub_291A4D0
```

The 8 MB sanity limit prevents SROA from creating absurdly large sub-allocas from pathological input.

### Phase 5: Slice Classification

For each slice in the partition, `classifySlice` (`sub_29280E0`) sorts it into one of two lists:

| List | Variable | Contents |
|------|----------|----------|
| splittable-inside | `v446` | Slices fully contained within `[start, end)` |
| splittable-outside | `v452` | Slices that reference bytes outside the partition (integer widening) |

The classification also tracks:
- `v413` (sameType flag): whether all slices in the partition use the same LLVM type.
- `v415` (common type): the shared type if `sameType` is true.
- `v412` (hasPointerType): whether any slice involves a pointer type.
- Integer types (type byte == 14) are routed to the outside list for special handling (widening/narrowing may be needed).

Then `rewritePartition` (`sub_29197E0`) is called twice: first for inside slices with callback `sub_2919EF0`, then for outside slices if the first call produced nothing.

### Phase 6: New Sub-Alloca Creation

```
    // Compute alignment
    align_log2 = _BitScanReverse64(alloca_alignment)
    abi_align = getABITypeAlignment(type)            // sub_AE5020
    pref_align = getPrefTypeAlignment(type)          // sub_AE5260

    // Build name: original_name + ".sroa." + index
    name = getName(alloca) + ".sroa."                // sub_BD5D20

    // Create the new alloca (80-byte AllocaInst object)
    new_alloca = AllocaInst::Create(type, size, alignment, name)
                                                     // sub_BD2C40 + sub_B4CCA0
    // Insert before the original alloca
    insertBefore(new_alloca, alloca)

    // Copy debug metadata
    copyDebugInfo(alloca, new_alloca)                // sub_B96E90 + sub_B976B0
```

Each sub-alloca is an 80-byte `AllocaInst` object with the `.sroa.` name prefix. The insertion point is always directly before the original alloca in the entry block, maintaining the invariant that all allocas are grouped at the function entry.

### Phase 7: Instruction Rewriting

The `visitUse` function (`sub_292A4F0`) rewrites each user of the original alloca to reference the appropriate sub-alloca:

- **GEP chains**: retargeted to the new sub-alloca with adjusted offsets (`sub_29348F0`).
- **Loads**: rewritten with type-casts if the sub-alloca type differs from the original load type (`sub_F38250`).
- **Stores**: same treatment as loads (`sub_F38250`).
- **Memcpy/memset**: split into smaller operations covering only the sub-alloca's byte range (`sub_F38330`).

Each rewritten instruction is validated via `sub_291F660` (`validateRewrite`).

### Phase 8: Worklist Management

Dead instructions are removed from the pass's open-addressing hash table (at pass state offset `+432`, mask at `+896`). New sub-allocas are added to the worklist (`sub_2928360`) for re-processing. Allocas that cannot be split are recorded via `sub_2916C30` (`recordNonSplitAlloca`).

### Phase 9: Result Recording

For each partition that produced a new alloca, the result is stored as a 24-byte entry `[new_alloca, bit_offset, bit_size]` in the output array. Hash table capacity is computed using the classic `4n/3 + 1` formula (next power of 2), and entries are stored via open-addressing with linear probing (`sub_29222D0` handles resizing).

### Phase 10: Post-Split Use Rewriting

The most complex phase. For every use of the original alloca:

1. `getOperandNo` (`sub_B59530`) determines which operand references the alloca.
2. `getAccessRange` (`sub_AF47B0`) computes the byte range `[begin, end)` within the alloca that this use touches.
3. For each new sub-alloca in the result array, `checkSubAllocaOverlap` (`sub_AF4D30`) tests whether the sub-alloca's range overlaps the use's range.
4. If overlap: `computeRewrittenValue` (`sub_2916270`) produces the replacement value by combining reads from multiple sub-allocas if the original use spans a partition boundary.
5. Dead uses are identified by `isDeadUse` (`sub_291D8F0`) and erased.

The use-list implementation uses a tagged-pointer scheme: bit 2 indicates "heap-allocated list" vs. "inline single element," bits [63:3] are the actual pointer. Lists are freed via `_libc_free` after extracting the data pointer.

### Phase 11-12: Lifetime and Debug Info

Lifetime markers (`llvm.lifetime.start` / `llvm.lifetime.end`) are rewritten via `sub_291E540` to cover only the sub-alloca's byte range. Debug declarations (`dbg.declare`, `dbg.value`) are similarly rewritten: each debug-info entry pointing to the original alloca is retargeted to the sub-alloca whose byte range covers the relevant fragment, using the debug expression's `DW_OP_LLVM_fragment` to indicate the piece.

## Speculative Loads Through Select

When a load reaches its pointer through a `select` instruction, SROA hoists the load into both branches:

```llvm
; Before SROA:
%p = select i1 %cond, ptr %a, ptr %b
%v = load float, ptr %p, align 4

; After SROA:
%vt = load float, ptr %a, align 4          ; .sroa.speculate.load.true
%vf = load float, ptr %b, align 4          ; .sroa.speculate.load.false
%v  = select i1 %cond, float %vt, float %vf ; .sroa.speculated
```

This is significant on GPU for two reasons:

1. **SIMT execution model.** A `select` on a GPU maps to a predicated move, which executes in a single cycle without divergence. The two speculative loads execute unconditionally and in parallel (both issue to the memory pipeline regardless of the predicate). This is cheaper than a control-dependent load that would require branch divergence handling.

2. **Alloca elimination.** The original pattern requires the `select` to produce a pointer, which means the alloca must remain in memory (the pointer must be materializable). After speculation, both pointers are consumed directly by loads, and if `%a` and `%b` are themselves sub-allocas that can be promoted to registers, the entire chain collapses to register-only operations.

The implementation (Kind 3, lines 1024-1235 of `splitAlloca`) creates:
- Two `BitCastInst` with names `.sroa.speculate.cast.true` and `.sroa.speculate.cast.false`.
- Two `LoadInst` with names `.sroa.speculate.load.true` and `.sroa.speculate.load.false`, preserving alignment from the original load.
- One `SelectInst` with name `.sroa.speculated` via `sub_B36550` (`SelectInst::Create`).
- Metadata copied from the original load via `sub_B91FC0` (`copyMetadata`).

## Interaction with `.param` Space

Function parameters passed by value in CUDA/PTX use the `.param` address space (NVPTX address space 101). The EDG frontend generates an alloca to hold a copy of each `byval` parameter, then loads fields from it. Consider:

```cuda
struct Vec3 { float x, y, z; };

__device__ float sum(Vec3 v) {
    return v.x + v.y + v.z;
}
```

The IR before SROA contains:

```llvm
define float @sum(%struct.Vec3* byval(%struct.Vec3) align 4 %v) {
  %v.addr = alloca %struct.Vec3, align 4           ; byval copy
  %x = getelementptr %struct.Vec3, ptr %v.addr, i32 0, i32 0
  %0 = load float, ptr %x, align 4
  %y = getelementptr %struct.Vec3, ptr %v.addr, i32 0, i32 1
  %1 = load float, ptr %y, align 4
  %z = getelementptr %struct.Vec3, ptr %v.addr, i32 0, i32 2
  %2 = load float, ptr %z, align 4
  %add = fadd float %0, %1
  %add1 = fadd float %add, %2
  ret float %add1
}
```

SROA splits `%v.addr` into three scalar allocas (`%v.addr.sroa.0`, `.sroa.1`, `.sroa.2`), each holding a single `float`. Because each sub-alloca has only simple loads and stores, `mem2reg` (which runs in the next pipeline iteration) promotes all three to SSA registers. The final IR has no allocas and no memory traffic -- the three `float` values live entirely in registers.

Without SROA, the `byval` copy would persist as a `.local` allocation, and every field access would be a `.local` load. For a kernel that calls `sum()` in a tight loop, this difference is the difference between register-speed and DRAM-speed execution.

The `NVPTXTargetLowering::LowerCall` function (`sub_3040BF0`) emits `DeclareParam` (opcode 505) and `StoreV1/V2/V4` (opcodes 571-573) for the `.param` writes on the caller side; SROA's job is to ensure the callee's reads never touch memory.

## Auxiliary SROA Functions (Secondary Instance)

The binary contains a second SROA instance at `0x1A10000`-`0x1A3FFFF` (~200 KB), corresponding to the legacy pass manager code path. This instance contains additional rewriting functions not visible in the primary (new PM) instance:

| Function | Size | Role | Key strings |
|----------|------|------|-------------|
| `sub_1A3B290` | 58 KB | `rewritePartition` (memcpy/memset) | `"memcpy.load.fca"`, `"memcpy.store.fca"`, `"memset.store.fca"`, `".fca"` |
| `sub_1A2D070` | 35 KB | `presplitLoadsAndStores` | `"select.gep.sroa"`, `"select.sroa"`, `"phi.sroa"`, `"phi.gep.sroa"` |
| `sub_1A2C2F0` | 9 KB | Select speculation | `".sroa.speculate.load.true"`, `".sroa.speculate.load.false"` |
| `sub_1A2FFA0` | 12 KB | Vector splat handling | `"vsplat"`, `".splatinsert"`, `".splat"` |
| `sub_1A30D10` | 16 KB | Load rewriting | `"copyload"`, `"oldload"` |
| `sub_1A31B60` | 9 KB | Extract/load patterns | `"extract"`, `"load.ext"`, `"endian_shift"`, `"load.trunc"` |
| `sub_1A23B30` | 11 KB | Type casting | `"sroa_raw_cast"`, `"sroa_raw_idx"`, `"sroa_cast"` |
| `sub_1A3A670` | 13 KB | Speculative load promotion | `".sroa.speculated"`, `".sroa.speculate.load."` |
| `sub_1A13B30` | 36 KB | Alloca analysis / slice building | -- |
| `sub_1A15E70` | 34 KB | Partition computation | -- |
| `sub_1A18770` | 38 KB | Use analysis | -- |
| `sub_1A3DCD0` | 15 KB | Cleanup | -- |

The `.fca` suffix stands for "first-class aggregate" -- LLVM's term for structs and arrays passed by value. The `presplitLoadsAndStores` function handles a special case where loads and stores of aggregates can be split before the main SROA algorithm runs, decomposing `load { i32, i32 }` into separate `load i32` instructions and `store { i32, i32 }` into separate `store i32` instructions. The `select.gep.sroa` and `phi.gep.sroa` strings indicate that this pre-split phase also handles GEP chains through PHI nodes and selects, a pattern common in CUDA code after inlining.

## Data Structures

### Slice Entry (24 bytes)

```
struct SROASlice {
    uint64_t start;     // +0:  byte offset into alloca (inclusive)
    uint64_t end;       // +8:  byte offset into alloca (exclusive)
    uint64_t flags;     // +16: bit 2 = splittable, bits [63:3] = user metadata ptr
};
```

The `splittable` bit indicates whether the slice can be split across partition boundaries. Loads and stores of simple scalars that fit entirely within the alloca have this bit cleared in Phase 1 of `splitAlloca`.

### Sub-Alloca Record (56 bytes)

```
struct SubAllocaRecord {
    void* alloca_ptr;       // +0:  pointer to the new AllocaInst
    void* slice_list;       // +8:  pointer to slice list for this sub-alloca
    uint64_t slice_list_cap; // +16: capacity of slice list
    // ... additional fields through +55
};
```

Stored in a `SmallVector<SubAllocaRecord, 2>` -- the inline buffer holds two elements (common case: a struct with two fields), spilling to heap for larger aggregates.

### Pass State Hash Table

The SROA pass state object (parameter `a1` to both main functions) contains an open-addressing hash table at offsets `+432` through `+896`. It uses LLVM-layer sentinels (-4096 / -8192) with instruction pointer keys. This table tracks which instructions have already been processed or are pending in the worklist. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing strategy, and growth policy.

### Tagged Pointer Scheme

Use-lists and debug-info lists use a tagged-pointer encoding for memory efficiency:

- **Bit 2 clear**: the "pointer" field directly contains a single element (inline storage for the common case of one use).
- **Bit 2 set**: bits [63:3] are a heap-allocated pointer to a variable-length list. Freed via `_libc_free` after masking off the tag bits.

This avoids heap allocation for the overwhelmingly common case where an alloca field has exactly one load or one store.

## IR Before/After Example

Consider a CUDA kernel that uses a local struct:

```cuda
__global__ void kernel(float* out, int n) {
    struct { float a; int b; float c; } local;
    local.a = 1.0f;
    local.b = n;
    local.c = 2.0f;
    out[0] = local.a + local.c;
    out[1] = (float)local.b;
}
```

**Before SROA:**

```llvm
define void @kernel(ptr %out, i32 %n) {
entry:
  %local = alloca { float, i32, float }, align 4
  %a = getelementptr { float, i32, float }, ptr %local, i32 0, i32 0
  store float 1.0, ptr %a, align 4
  %b = getelementptr { float, i32, float }, ptr %local, i32 0, i32 1
  store i32 %n, ptr %b, align 4
  %c = getelementptr { float, i32, float }, ptr %local, i32 0, i32 2
  store float 2.0, ptr %c, align 4
  %v0 = load float, ptr %a, align 4
  %v2 = load float, ptr %c, align 4
  %sum = fadd float %v0, %v2
  store float %sum, ptr %out, align 4
  %v1 = load i32, ptr %b, align 4
  %conv = sitofp i32 %v1 to float
  %idx = getelementptr float, ptr %out, i64 1
  store float %conv, ptr %idx, align 4
  ret void
}
```

**After SROA (three sub-allocas, then mem2reg promotes to registers):**

```llvm
define void @kernel(ptr %out, i32 %n) {
entry:
  ; No allocas remain -- all promoted to SSA values
  %sum = fadd float 1.0, 2.0          ; constant-folded later by InstCombine
  store float %sum, ptr %out, align 4
  %conv = sitofp i32 %n to float
  %idx = getelementptr float, ptr %out, i64 1
  store float %conv, ptr %idx, align 4
  ret void
}
```

SROA splits `%local` into `%local.sroa.0` (float), `%local.sroa.1` (i32), `%local.sroa.2` (float). Each sub-alloca has trivial load/store patterns, so `mem2reg` promotes all three. The stores and loads collapse, GEPs disappear, and the kernel runs entirely from registers.

## Name Suffixes Created During Splitting

| Suffix | Purpose |
|--------|---------|
| `.sroa.` | New sub-alloca name prefix |
| `.sroa.speculate.cast.true` | Bitcast for true branch of select |
| `.sroa.speculate.cast.false` | Bitcast for false branch of select |
| `.sroa.speculate.load.true` | Speculative load from true branch |
| `.sroa.speculate.load.false` | Speculative load from false branch |
| `.sroa.speculated` | Final select combining speculative loads |
| `.cont` | Continuation block (after branch splitting) |
| `.then` | Then-branch block |
| `.else` | Else-branch block |
| `.val` | Value extracted from split load/store |
| `.fca` | First-class aggregate decomposition |
| `select.gep.sroa` | GEP through select, pre-split |
| `select.sroa` | Select pointer, pre-split |
| `phi.sroa` | PHI pointer, pre-split |
| `phi.gep.sroa` | GEP through PHI, pre-split |
| `sroa_raw_cast` | Raw bitcast during type rewriting |
| `sroa_raw_idx` | Raw index computation during rewriting |
| `sroa_cast` | Generic SROA type cast |
| `vsplat` | Vector splat element |
| `.splatinsert` | Splat insert element |
| `.splat` | Splat shuffle |
| `copyload` | Copy of a load during rewriting |
| `oldload` | Original load being replaced |
| `extract` | Extracted sub-value |
| `load.ext` | Load with extension |
| `endian_shift` | Endianness-adjustment shift |
| `load.trunc` | Load with truncation |
| `memcpy.load.fca` | Memcpy load of first-class aggregate |
| `memcpy.store.fca` | Memcpy store of first-class aggregate |
| `memset.store.fca` | Memset store of first-class aggregate |

## Differences from Upstream LLVM

The core SROA algorithm in cicc v13.0 is stock LLVM SROA. No CUDA-specific modifications to the splitting logic, slice building, or partition computation were detected. The NVIDIA-specific elements are limited to:

1. **Pass state object layout.** The offsets within the pass state structure (worklist at `+432`, hash table at `+824`-`+864`, sub-alloca records at `+1080`-`+1096`) reflect NVIDIA's PassManager integration, not upstream's.

2. **IR node encoding.** Opcode numbers (61 = load, 62 = store, 85 = intrinsic, 55 = phi) and operand layout (32-byte basic blocks, tagged pointers) follow NVIDIA's modified IR format.

3. **Debug metadata system.** The metadata kind for debug info uses `MD_dbg = 38` (NVIDIA assignment), queried via `sub_B91C10`.

4. **Global threshold knob.** The value at `qword_50056C8` may have an NVIDIA-specific default different from upstream's 128-byte / 1024-bit default. The knob is likely settable via the pipeline text `sroa<preserve-cfg>` or `sroa<modify-cfg>`.

5. **Pipeline positioning.** The early-pipeline placement (position 4, before `NVVMLowerArgs` and `NVVMLowerAlloca`) is NVIDIA-specific. Upstream LLVM typically places SROA after `InstCombine` and `SimplifyCFG`; cicc places it before those passes to eliminate byval parameter copies as early as possible.

## Configuration

| Knob | Global | Description |
|------|--------|-------------|
| `qword_50056C8` | SROA size threshold | Maximum alloca size (in bits) that SROA will attempt to split. Allocas exceeding this are left for the backend. |
| `qword_50055E8` | Two-pass analysis flag | When set, enables a pre-analysis pass before slice building (new PM integration). |
| `NVVMPassOptions` offset `+1400` | Disable flag | Setting this byte disables SROA entirely. |
| Pipeline param `preserve-cfg` | -- | Runs SROA without modifying the CFG (no block splitting for speculative loads across PHIs). |
| Pipeline param `modify-cfg` | -- | Allows SROA to modify the CFG (enables full speculative load hoisting including PHI/select decomposition). |

## Function Map

| Address | Identity | Size |
|---------|----------|------|
| **Primary instance (new PM)** | | |
| `sub_2935C30` | `SROAPass::runOnAlloca` | 58 KB |
| `sub_2930B90` | `SROAPass::splitAlloca` | 80 KB |
| `sub_2927160` | `buildSlices` (use analysis) | -- |
| `sub_2924690` | `buildPartitions` (group slices) | -- |
| `sub_2913C40` | `buildPartitionTable` | -- |
| `sub_2912200` | `sortSlices` | -- |
| `sub_2915A90` | `compactSlices` (with filter) | -- |
| `sub_2914CE0` | `compactSlices` (simple) | -- |
| `sub_291A860` | `findExistingValue` | -- |
| `sub_29197E0` | `rewritePartition` | -- |
| `sub_2919EF0` | `rewriteCallback` | -- |
| `sub_292A4F0` | `visitUse` (rewrite one use) | 54 KB |
| `sub_291F660` | `validateRewrite` | -- |
| `sub_29150D0` | `analyzeSlice` | -- |
| `sub_2929FB0` | `addToNewAllocaWorklist` | -- |
| `sub_2928360` | `addToWorklist` | -- |
| `sub_29220F0` | `addOperandToWorklist` | -- |
| `sub_2921860` | `clearPendingQueue` | -- |
| `sub_29280E0` | `classifySlice` | -- |
| `sub_2916C30` | `recordNonSplitAlloca` | -- |
| `sub_2916270` | `computeRewrittenValue` | -- |
| `sub_2912870` | `advancePartitionIterator` | -- |
| `sub_29348F0` | `rewriteGEPChain` | -- |
| `sub_2914800` | `replaceAndErase` | -- |
| `sub_2914380` | `collectUsesForRewrite` (variant) | -- |
| `sub_2914550` | `collectUsesForRewrite` (original) | -- |
| `sub_29222D0` | Hash table resize | -- |
| `sub_292D810` | Alloca rewriting helper | 67 KB |
| `sub_2912100` | SROA pass metadata | -- |
| `sub_2912340` | SROA pass registration (`"Scalar Replacement Of Aggregates"`, `"sroa"`) | -- |
| **Secondary instance (legacy PM)** | | |
| `sub_1A33E80` | `SROAPass::runOnAlloca` (legacy) | 61 KB |
| `sub_1A37040` | `SROAPass::splitAlloca` (legacy) | 46 KB |
| `sub_1A3B290` | `rewritePartition` (memcpy/memset) | 58 KB |
| `sub_1A2D070` | `presplitLoadsAndStores` | 35 KB |
| `sub_1A2C2F0` | Select speculation | 9 KB |
| `sub_1A2FFA0` | Vector splat handling | 12 KB |
| `sub_1A30D10` | Load rewriting | 16 KB |
| `sub_1A31B60` | Extract/load patterns | 9 KB |
| `sub_1A23B30` | Type casting | 11 KB |
| `sub_1A3A670` | Speculative load promotion | 13 KB |
| `sub_1A13B30` | Alloca analysis / slice building | 36 KB |
| `sub_1A15E70` | Partition computation | 34 KB |
| `sub_1A18770` | Use analysis | 38 KB |
| `sub_1A3DCD0` | Cleanup | 15 KB |
| **Shared helpers** | | |
| `sub_B4CE70` | `isAllocaPromotable` | -- |
| `sub_B43CC0` | `getDL` (DataLayout) | -- |
| `sub_BDB740` | `getTypeSizeInBits` | -- |
| `sub_9208B0` | `getTypeAllocSize` | -- |
| `sub_BD5C60` | `getType` | -- |
| `sub_BD5D20` | `getName` | -- |
| `sub_BD2C40` | `AllocaInst::Create` | -- |
| `sub_BD2DA0` | `PHINode::Create` | -- |
| `sub_B4CCA0` | `AllocaInst` constructor | -- |
| `sub_BCD140` | `CreateBitCast` | -- |
| `sub_BCD420` | `CreateAlloca` | -- |
| `sub_BD84D0` | `replaceAllUsesWith` | -- |
| `sub_B43D60` | `eraseFromParent` | -- |
| `sub_B36550` | `SelectInst::Create` | -- |
| `sub_ACADE0` | `UndefValue::get` | -- |
| `sub_AE5020` | `getABITypeAlignment` | -- |
| `sub_AE5260` | `getPrefTypeAlignment` | -- |
| `sub_B91FC0` | `copyMetadata` | -- |
| `sub_B46500` | `isVolatile` | -- |
| `sub_BCEBA0` | `isVectorType` | -- |
| `sub_F38250` | `rewriteLoadStoreOfSlice` | -- |
| `sub_F38330` | `rewriteMemTransferOfSlice` | -- |
| `sub_AE74C0` | `collectAllUses` | -- |
| `sub_AF47B0` | `getAccessRange` | -- |
| `sub_AF4D30` | `checkSubAllocaOverlap` | -- |
| `sub_D5F1F0` | `buildMetadataTable` | -- |
| `sub_D6B260` | `addToErasedSet` | -- |
| `sub_11D2BF0` | Slice optimizer init | -- |
| `sub_11D3120` | Slice optimizer run | -- |
| `sub_11D7E80` | Slice optimizer finalize | -- |

## Cross-References

- [Scalar Passes Hub](./scalar-passes.md) -- hub page linking SROA, EarlyCSE, and JumpThreading with GPU-context summaries
- [Pipeline & Ordering](./pipeline.md) -- pipeline positions 4 and post-sinking
- [Register Allocation](./register-allocation.md) -- surviving allocas become `.local` spills, directly increasing register pressure
- [Rematerialization](../passes/rematerialization.md) -- recomputes cheap values to reduce register pressure; operates downstream of SROA
- [StructSplitting](../passes/struct-splitting.md) -- NVIDIA custom pass that splits struct arguments at the call boundary; complements SROA's intra-procedural splitting
- [MemorySpaceOpt](../passes/memory-space-opt.md) -- resolves generic pointers to specific address spaces; runs after SROA
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- the open-addressing hash table used by the SROA pass state
