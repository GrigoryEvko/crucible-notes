# Struct/Aggregate Splitting

GPU register files are typed and scalar. An SM has no concept of loading a struct, storing a struct, or passing a struct through a register -- every value that survives past IR lowering must reduce to a set of individually-named scalar registers. LLVM's standard SROA pass handles alloca-based aggregates by promoting them to scalars, but a large class of aggregate operations never touch an alloca: return values, call arguments, PHI nodes carrying struct types, and aggregate load/store patterns from memcpy lowering. NVIDIA's struct-splitting pass operates on these non-alloca aggregate operations at the NVVM IR level, decomposing every struct-typed value into its constituent scalar fields so that downstream register allocation sees only scalar types.

The pass exists in two binary instances. The primary implementation at `sub_1C86CA0` (72KB, ~1,200 lines, 500+ locals) lives in the aggregate-splitting cluster at `0x1C80000`--`0x1CBFFFF` and operates on NVVM IR using NVIDIA-proprietary type IDs. A second, closely related implementation at `sub_2CCF450` (58KB) handles the `lower-aggr-copies` pipeline pass and shares the same string constants (`"splitStruct"`, `"srcptr"`, `"dstptr"`, `"remsrc"`, `"remdst"`, `"split"`, `"vld"`). Both instances produce the same fundamental transformation: aggregate operations become sequences of scalar operations on individual struct elements.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_1C86CA0` |
| Size | 72KB (~1,200 lines decompiled), 500+ local variables |
| Binary cluster | `0x1C80000`--`0x1CBFFFF` (Aggregate Splitting + Memory Ops) |
| Second instance | `sub_2CCF450` (58KB, `lower-aggr-copies` pass) |
| Pipeline pass name | `lower-aggr-copies` (parameterized: `lower-aggr-func-args`) |
| Related pass | `lower-struct-args` (parameterized: `opt-byval`) |
| IR level | NVVM IR (NVIDIA-proprietary type IDs, not LLVM `Type::TypeID`) |
| Key opcode | 32 (`splitStruct` instruction) |
| Use replacement | `sub_164D160` (RAUW -- Replace All Uses With) |
| LLVM upstream | No equivalent -- this is entirely NVIDIA-proprietary |

## Algorithm

The pass walks every instruction in a function, looking for operations whose result type or operand type is an aggregate (struct or array). For each such operation, it decomposes the aggregate into its scalar elements, creates a `splitStruct` multi-output instruction, and rewires all uses to reference individual element extractions.

### Step 1: Type Decomposition

For each struct type encountered, the pass retrieves the struct layout from the DataLayout and enumerates its elements:

```
function decomposeStructType(struct_type, data_layout):
    layout = sub_1643350(data_layout, struct_type)  // GetStructLayout
    element_types = []
    for each element in struct_type.elements:
        scalar_ty = sub_159C470(element)            // getScalarType
        element_types.append(scalar_ty)
    return element_types
```

`sub_1643350` retrieves the `StructLayout` from the `DataLayout`, giving byte offsets and sizes for each field. `sub_159C470` maps each element to its scalar type -- for nested structs, this recurses; for arrays, it yields the element type; for scalars, it returns the type directly.

The element types accumulate in a local array `v505[]` with the count tracked in `v506`. This flattened type list drives all subsequent instruction creation.

### Step 2: `splitStruct` Instruction Creation (Opcode 32)

The pass creates a new multi-output instruction with NVVM opcode 32:

```
function createSplitStruct(original_inst, element_types, count):
    composite_ty = sub_15F9F50(element_types, count)     // ComputeCompositeType
    aligned_ty   = sub_1646BA0(composite_ty, data_layout) // SetAlignmentFromDL

    // If original was a vector type (type_id == 16), wrap in vector
    if getTypeId(original_inst.type) == 16:
        aligned_ty = sub_16463B0(aligned_ty)              // WrapInVectorType

    split_inst = sub_15F1EA0(aligned_ty, 32, parent, nops, flags)
                                                          // InitInstruction(opcode=32)
    // Store original type info at inst+56, composite at inst+64
    split_inst[+56] = original_type_info
    split_inst[+64] = sub_15F9F50(composite_ty)
    return split_inst
```

The `splitStruct` instruction is the NVVM-specific multi-result node that represents the decomposition. It produces N outputs, one per struct element. The instruction stores both the original aggregate type (at offset +56) and the composite element type (at offset +64) for later phases that may need to reconstruct type information.

### Step 3: Element Pointer Extraction

For each element of the decomposed struct, the pass creates an indexed load from the `splitStruct` result:

```
for i in 0..count:
    ptr = sub_15FD590(split_inst, element_types[i],
                      operand=i, name="ptr", insertion_point)
    // Creates opcode 56 (extractvalue-like) with type=1
```

`sub_15FD590` creates an instruction with opcode 56 that extracts the i-th element from the multi-output `splitStruct` node. The `"ptr"` name prefix appears in debug output. Each extraction yields a scalar-typed value that downstream passes can assign to an individual PTX register.

### Step 4: Split Load with Alignment Preservation

For the actual memory access that feeds the `splitStruct`, the pass creates a split load instruction:

```
function createSplitLoad(original_load, element_types):
    alignment = computeAlignment(original_load)
    split_load = sub_15F90A0(element_types, alignment, ...)
    additional_align = sub_1CCB4A0(data_layout, element_types)
    final_align = alignment & (-additional_align)  // min power-of-2
    return split_load
```

The resulting instruction carries the `"split"` name prefix. The alignment computation is described in detail in the next section.

### Step 5: Use Replacement

After creating all scalar operations, `sub_164D160` (RAUW -- Replace All Uses With) replaces every use of the original aggregate operation with the corresponding scalar element extraction:

```
sub_164D160(original_aggregate_inst, split_inst)
```

This is the same RAUW infrastructure used across CICC (also called from GlobalOpt, DSE, the inliner, and other passes). After replacement, the original aggregate instruction has zero uses and is eligible for dead code elimination.

## Alignment Preservation

The pass must preserve memory alignment when splitting aggregate loads/stores into per-element accesses. GPU memory transactions have strict alignment requirements: a misaligned access can silently produce wrong results or trap, depending on the address space and SM architecture.

### The Alignment Formula

The decompiled alignment calculation is:

```
aligned_value = 1 << (alignment_field >> 1) >> 1
```

Breaking this down:

1. `alignment_field >> 1` -- the alignment is stored in a compressed encoding where the field value is approximately `2 * log2(alignment) + bias`.
2. `1 << (result)` -- converts back to a power-of-two alignment value.
3. `>> 1` -- adjusts for the encoding's off-by-one (the encoding stores `2*log2 + 1`, so the final shift corrects it).

For example, if `alignment_field = 9`, then `9 >> 1 = 4`, `1 << 4 = 16`, `16 >> 1 = 8`, yielding 8-byte alignment. This encoding is compact and used throughout NVVM's type system to store alignment in a single byte.

### Additional Alignment Computation

`sub_1CCB4A0` provides a DataLayout-aware alignment computation for the element type. The final alignment is the minimum of the original alignment and the element's natural alignment, computed via:

```
final_align = original_align & (-element_natural_align)
```

The bitwise AND with the negation of the element alignment selects the largest power-of-two that divides both values, ensuring the per-element access is always naturally aligned for its type without exceeding the original aggregate's alignment guarantee.

## NVVM Type ID System

The pass operates on NVVM's proprietary type ID system, not LLVM's `Type::TypeID`. The size classification logic (decompiled lines 997--1030) reveals the mapping:

| NVVM Type ID | Type | Bit Width |
|:---:|---|---|
| 1 | BFloat16 (i8 pair with padding) | 16 |
| 2 | Float | 32 |
| 3 | Double / i32 (context-dependent) | 64 |
| 4 | i64 | 80 (with padding to 10 bytes) |
| 5, 6 | FP128 / PPC FP128 | 128 |
| 7 | Pointer | `8 * DataLayout::getPointerSizeInBits(0)` |
| 9 | Float (alternate, possibly metadata) | 64 |
| 0xB (11) | Integer (arbitrary width) | `element_encoding >> 8` |
| 0xD (13) | Array | `8 * DataLayout::getStructLayout(type)` total size |
| 0xE (14) | Struct | Recursive sum of element sizes |
| 16 | Vector | Triggers vector-type wrapping via `sub_16463B0` |

For struct types (ID 0xE), the size computation is recursive: the pass sums the sizes of all elements, each resolved through the same type-ID dispatch table. Array types (ID 0xD) use `sub_15A9930` to look up the total allocation size from the DataLayout's `StructLayout` cache (which also handles arrays despite the name).

## Nested Struct and Array Handling

When a struct element is itself a struct or an array, the pass recurses. The `sub_159C470` (getScalarType) call during type decomposition flattens nested aggregates: a struct `{i32, {f32, f64}, i16}` decomposes not into three elements but into four scalars: `i32, f32, f64, i16`. The flattening continues until every element is a primitive scalar or a pointer.

Arrays within structs are handled differently depending on their size. Small arrays may be fully unrolled into individual element accesses. The size threshold is governed by the `max-aggr-copy-size` and `large-aggr-store-limit` knobs. Arrays that exceed the threshold are not decomposed into per-element loads but instead lowered to byte-copy loops (the `"remsrc"` / `"remdst"` / `"i8dst"` paths correspond to this remainder-byte handling when the aggregate cannot be evenly split into typed elements).

The remainder path:
1. Computes the number of whole elements that can be extracted as typed loads.
2. For any trailing bytes that do not fill a complete element, generates an `i8` byte loop: `"remsrc"` is the source pointer for the remainder, `"remdst"` is the destination, and `"i8dst"` is the byte-typed destination pointer.

## Relationship with SROA

LLVM's SROA (Scalar Replacement of Aggregates) and NVIDIA's struct splitting are complementary, not overlapping:

| Aspect | LLVM SROA | NVIDIA Struct Splitting |
|--------|-----------|------------------------|
| Target | `alloca` instructions in entry block | Non-alloca aggregate operations |
| Scope | Stack-allocated structs | Return values, call args, PHI nodes, memcpy results |
| IR level | LLVM IR (standard `Type::TypeID`) | NVVM IR (proprietary type IDs) |
| Pipeline position | Early scalar optimization passes | After LLVM optimization, NVVM lowering phase |
| Output | SSA scalars replacing alloca uses | `splitStruct` (opcode 32) multi-output nodes |
| Upstream | Standard LLVM pass | No upstream equivalent |

SROA runs during the standard LLVM optimization pipeline and eliminates alloca-based aggregates. By the time struct splitting runs, all remaining aggregate operations are those SROA could not handle: function return values carrying struct types, call sites passing or receiving struct-typed parameters, and aggregate-typed PHI nodes at control flow merges. Struct splitting is the final lowering step that ensures no aggregate-typed values survive into register allocation.

## PTX Register Mapping

After struct splitting, every value in the IR is scalar-typed. During instruction selection and register allocation, each scalar maps to a PTX virtual register of the corresponding type:

```
// Before struct splitting:
%result = load {i32, f32, i64}, ptr %p, align 8

// After struct splitting:
%split = splitStruct {i32, f32, i64}   // opcode 32, multi-output
%r0 = extractelement %split, 0         // i32 -> %r1 (32-bit register)
%r1 = extractelement %split, 1         // f32 -> %f1 (32-bit FP register)
%r2 = extractelement %split, 2         // i64 -> %rd1 (64-bit register)
```

In PTX, register types are explicit:
- `%r` registers: 32-bit integers
- `%rd` registers: 64-bit integers
- `%f` registers: 32-bit floats
- `%fd` registers: 64-bit floats
- `%h` registers: 16-bit values (half/bfloat)
- `%p` registers: predicates (1-bit)

Without struct splitting, the register allocator would need to handle aggregate-typed live ranges, which is impossible on GPU hardware where the register file has no concept of a "struct register." The pass is therefore a hard prerequisite for correct register allocation.

## Pipeline Position

The pass runs as part of the NVVM lowering phase, after the main LLVM optimization pipeline has completed. It is registered as `lower-aggr-copies` in the New PM pipeline parser at index 417 (`sub_2342890`), with parameter `lower-aggr-func-args` controlling whether function argument aggregates are also lowered.

```
Pipeline position:
  LLVM Optimizer (SROA, GVN, DSE, etc.)
    -> NVIDIA NVVM Lowering Phase
      -> lower-struct-args (opt-byval)     [lower struct function args]
      -> lower-aggr-copies (lower-aggr-func-args)  [struct splitting]
      -> memory-space-opt                   [address space resolution]
      -> register allocation preparation
```

The companion pass `lower-struct-args` (pass index 418) handles byval-attributed function parameters specifically, converting struct-typed byval parameters into explicit copy + scalar access patterns. It runs before `lower-aggr-copies` to ensure that byval struct arguments are already decomposed when the main splitting pass encounters them.

## Configuration

### Knobs (ctor_265 at `0x4F48E0`)

| Knob | Default | Description |
|------|---------|-------------|
| `devicefn-param-always-local` | -- | Treat parameter space as local in device functions |
| `skiploweraggcopysafechk` | false | Skip safety check in aggregate copy lowering |
| `large-aggr-store-limit` | -- | Threshold for large aggregate store unrolling |
| `max-aggr-copy-size` | -- | Maximum aggregate size for full decomposition |
| `lower-aggr-unrolled-stores-limit` | -- | Limit on unrolled stores per aggregate copy |

### InstCombine Aggregate Knobs (ctor_086 at `0x49E670`)

| Knob | Default | Description |
|------|---------|-------------|
| `max-aggr-lower-size` | 128 | Size threshold (bytes) below which InstCombine lowers aggregates |
| `aggressive-max-aggr-lower-size` | 256 | Aggressive threshold for aggregate lowering |
| `instcombine-merge-stores-from-aggr` | true | Merge stores originating from aggregate decomposition |

### Related Passes

| Knob | Scope | Description |
|------|-------|-------------|
| `lsa-opt` | `lower-struct-args` | Controls struct argument lowering |
| `lower-read-only-devicefn-byval` | `lower-struct-args` | Lower read-only device function byval params |
| `hoist-load-param` | `lower-struct-args` | Hoist parameter loads |
| `nvptx-force-min-byval-param-align` | backend | Force 4-byte minimum alignment for byval params |
| `nvptx-early-byval-copy` | backend | Copy byval arguments early in the pipeline |

## Diagnostic Strings

```
"splitStruct"     -- Name prefix for the opcode-32 multi-output node
"srcptr"          -- Source pointer in aggregate copy lowering
"dstptr"          -- Destination pointer in aggregate copy lowering
"remsrc"          -- Remainder source pointer (byte-copy tail loop)
"remdst"          -- Remainder destination pointer (byte-copy tail loop)
"i8dst"           -- Byte-typed destination for remainder copies
"split"           -- Name prefix for the per-element split load
"ptr"             -- Name prefix for element pointer extractions
"vld"             -- Vector load variant in the second instance
```

## Function Map

### Primary Instance (`sub_1C86CA0`, 72KB)

| Function | Address | Role |
|----------|---------|------|
| **Main driver** | `sub_1C86CA0` | Top-level struct splitting pass |
| StructLayout query | `sub_1643350` | `DataLayout::getStructLayout` |
| Scalar type query | `sub_159C470` | Get scalar element type (recursive for nested structs) |
| Composite type creation | `sub_15F9F50` | Build composite type from element array |
| Alignment from DL | `sub_1646BA0` | Set type alignment from DataLayout |
| Vector type wrapping | `sub_16463B0` | Wrap in vector type if original was vector |
| Instruction creation | `sub_15F1EA0` | `InitInstruction(type, opcode=32, parent, nops, flags)` |
| Element extraction | `sub_15FD590` | Create indexed load from multi-output node |
| Split load creation | `sub_15F90A0` | Create load with alignment preservation |
| Alignment computation | `sub_1CCB4A0` | DataLayout-aware alignment for element type |
| Use replacement | `sub_164D160` | RAUW (Replace All Uses With) |
| Pointer size query | `sub_15A9520` | `DataLayout::getPointerSizeInBits(AS)` |
| Struct size query | `sub_15A9930` | `DataLayout::getStructLayout` for size lookup |

### Second Instance (`sub_2CCF450`, 58KB)

| Function | Address | Role |
|----------|---------|------|
| **Aggregate lowering** | `sub_2CCF450` | `lower-aggr-copies` pass implementation |

### Pipeline Registration

| Function | Address | Role |
|----------|---------|------|
| New PM registration | `sub_2342890` | Pass index 417 (`lower-aggr-copies`) |
| Parameter parser | `sub_233A3B0` | Parses `lower-aggr-func-args` parameter |
| `lower-struct-args` parser | `sub_233A370` | Parses `opt-byval` parameter |

## Test This

The following kernel returns a struct from a device function. Struct splitting should decompose the aggregate return value into individual scalar registers.

```cuda
struct Result {
    float value;
    int   index;
    float confidence;
};

__device__ Result compute(const float* data, int tid) {
    Result r;
    r.value      = data[tid] * 2.0f;
    r.index      = tid;
    r.confidence = 0.95f;
    return r;
}

__global__ void struct_split_test(const float* in, float* out_val,
                                   int* out_idx, float* out_conf, int n) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= n) return;

    Result r = compute(in, tid);
    out_val[tid]  = r.value;
    out_idx[tid]  = r.index;
    out_conf[tid] = r.confidence;
}
```

**What to look for in PTX:**
- The `compute` function should be inlined, but even if it is not, the struct return should be decomposed. Look for the absence of `.local` memory for the `Result` struct -- all three fields (`value`, `index`, `confidence`) should live in individual PTX registers (`%f` for floats, `%r` for int).
- No `ld.local`/`st.local` pairs for passing the struct between `compute` and the kernel. If the struct survives unsplit, the caller allocates local memory for the return value, the callee stores into it, and the caller loads from it -- a 200+ cycle penalty per field.
- In the PTX, the three stores to `out_val`, `out_idx`, `out_conf` should use values directly from registers without any intermediate local memory traffic. Look for `st.global.f32` and `st.global.u32` with register operands, not loaded-from-local operands.
- To see the unsplit case, make `compute` a `__noinline__` function and compile at `-O0`. The struct will be passed through `.param` space with explicit `st.param`/`ld.param` sequences, showing the overhead that struct splitting eliminates.

## Cross-References

- [SROA](../llvm/sroa.md) -- upstream SROA handles alloca-based aggregates; complements StructSplitting's inter-procedural splitting
- [Rematerialization](./rematerialization.md) -- struct splitting reduces aggregate live ranges before remat
- [Memmove Unrolling](./other.md) -- companion pass at `sub_1C82A50` that unrolls memmove/memcpy loops
- [FP128/I128 Emulation](./other.md) -- companion pass at `sub_1C8C170` in the same binary cluster
- [Pipeline & Ordering](../llvm/pipeline.md) -- pass ordering in the New PM pipeline
- [NVIDIA Custom Passes Overview](./index.md) -- master inventory of all NVIDIA passes
- [Code Generation](../pipeline/codegen.md) -- register allocation that consumes split scalars
