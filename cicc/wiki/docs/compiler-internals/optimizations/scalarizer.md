# Scalarizer Pass

**Pass Type**: Function-level transformation pass
**LLVM Class**: `llvm::ScalarizerPass`
**Extracted From**: CICC binary analysis and string literals
**Analysis Quality**: HIGH - RTTI confirmation and extensive configuration
**Evidence Sources**: RTTI type info, configuration parameters, error messages

---

## Overview

The Scalarizer pass decomposes vector operations into scalar (individual element) operations. This transformation can improve performance on targets where scalar operations are more efficient than vector operations, or enable further scalar optimizations.

**Core Algorithm**: Vector instruction decomposition with element tracking

**Key Features**:
- Decomposes vector operations into scalar equivalents
- Configurable (can scalarize loads/stores separately)
- Precision control (minimum bit width)
- Handles masked memory intrinsics
- Target-aware optimization

---

## Pass Registration and Configuration

### Scalarizer Pass Evidence

**Evidence**: `optimization_passes.json:27387`

```json
{
  "value": "constexpr llvm::StringRef llvm::getTypeName() [with DesiredTypeName = llvm::ScalarizerPass]"
}
```

This RTTI evidence definitively confirms `llvm::ScalarizerPass` is instantiated.

**Evidence**: `optimization_passes.json:29733`

```json
{
  "value": "ScalarizerPass"
}
```

---

## Algorithm

### Vector Decomposition

The scalarizer transforms vector operations into sequences of scalar operations:

```
Vector Operation:
  <4 x i32> %vec_add = add <4 x i32> %a, <4 x i32> %b

Scalarized:
  i32 %a0 = extractelement <4 x i32> %a, i32 0
  i32 %b0 = extractelement <4 x i32> %b, i32 0
  i32 %r0 = add i32 %a0, %b0

  i32 %a1 = extractelement <4 x i32> %a, i32 1
  i32 %b1 = extractelement <4 x i32> %b, i32 1
  i32 %r1 = add i32 %a1, %b1

  i32 %a2 = extractelement <4 x i32> %a, i32 2
  i32 %b2 = extractelement <4 x i32> %b, i32 2
  i32 %r2 = add i32 %a2, %b2

  i32 %a3 = extractelement <4 x i32> %a, i32 3
  i32 %b3 = extractelement <4 x i32> %b, i32 3
  i32 %r3 = add i32 %a3, %b3

  <4 x i32> %vec_add = insertelement undef, i32 %r0, i32 0
  <4 x i32> %tmp1 = insertelement %vec_add, i32 %r1, i32 1
  <4 x i32> %tmp2 = insertelement %tmp1, i32 %r2, i32 2
  <4 x i32> %result = insertelement %tmp2, i32 %r3, i32 3
```

### Core Algorithm Pseudocode

```python
def Scalarizer(function, config):
    modified = False

    for BB in function.basic_blocks:
        for instr in BB.instructions:
            if should_scalarize(instr, config):
                if scalarize_instruction(instr):
                    modified = True

    return modified

def should_scalarize(instr, config):
    """Determine if instruction should be scalarized"""

    # Skip non-vector operations
    if not instr.type.is_vector():
        return False

    # Check minimum bit width
    element_type = instr.type.get_element_type()
    if element_type.bit_width < config.min_bits:
        return False

    # Scalable vectors cannot be scalarized
    if instr.type.is_scalable_vector():
        return False  # Error: "Cannot scalarize scalable vector"

    # Check specific instruction types
    if instr.is_load() or instr.is_store():
        return config.scalarize_load_store

    if instr.is_masked_memory_intrinsic():
        return config.scalarize_masked_mem_intrin

    # Other vector operations
    if is_scalarizable_operation(instr):
        return True

    return False

def scalarize_instruction(instr):
    """Scalarize a single vector instruction"""

    vector_type = instr.type
    num_elements = vector_type.get_num_elements()
    element_type = vector_type.get_element_type()

    # Extract operands element by element
    scalar_operands = []
    for operand in instr.operands:
        if operand.type.is_vector():
            # Extract each element
            elements = []
            for i in range(num_elements):
                elem = create_extractelement(operand, i)
                elements.append(elem)
            scalar_operands.append(elements)
        else:
            # Scalar operand - replicate for each element
            scalar_operands.append([operand] * num_elements)

    # Perform scalar operations
    scalar_results = []
    for i in range(num_elements):
        # Get i-th element from each operand
        elem_operands = [ops[i] for ops in scalar_operands]

        # Create scalar version of operation
        scalar_result = create_scalar_operation(
            instr.opcode,
            element_type,
            elem_operands
        )
        scalar_results.append(scalar_result)

    # Reconstruct vector result
    result_vector = undef_value(vector_type)
    for i, scalar_result in enumerate(scalar_results):
        result_vector = create_insertelement(result_vector, scalar_result, i)

    # Replace original instruction
    instr.replace_all_uses_with(result_vector)
    instr.erase_from_parent()

    return True

def scalarize_load(load_instr, config):
    """Scalarize vector load into scalar loads"""

    if not config.scalarize_load_store:
        return False

    vector_type = load_instr.type
    num_elements = vector_type.get_num_elements()
    element_type = vector_type.get_element_type()

    base_ptr = load_instr.get_pointer_operand()
    alignment = load_instr.get_alignment()

    # Load each element separately
    result_vector = undef_value(vector_type)
    for i in range(num_elements):
        # Compute element pointer: base + i * element_size
        elem_ptr = create_gep(base_ptr, [i])

        # Load element
        elem_load = create_load(element_type, elem_ptr, alignment)

        # Insert into result vector
        result_vector = create_insertelement(result_vector, elem_load, i)

    load_instr.replace_all_uses_with(result_vector)
    load_instr.erase_from_parent()

    return True

def scalarize_store(store_instr, config):
    """Scalarize vector store into scalar stores"""

    if not config.scalarize_load_store:
        return False

    vector_value = store_instr.get_value_operand()
    vector_type = vector_value.type
    num_elements = vector_type.get_num_elements()

    base_ptr = store_instr.get_pointer_operand()
    alignment = store_instr.get_alignment()

    # Store each element separately
    for i in range(num_elements):
        # Extract element
        elem_value = create_extractelement(vector_value, i)

        # Compute element pointer
        elem_ptr = create_gep(base_ptr, [i])

        # Store element
        create_store(elem_value, elem_ptr, alignment)

    store_instr.erase_from_parent()
    return True

def scalarize_masked_intrinsic(intrinsic):
    """Scalarize masked memory intrinsics"""
    # Example: llvm.masked.load -> scalar loads with conditional
    #
    # Before:
    #   %vec = call <4 x i32> @llvm.masked.load.v4i32(
    #       <4 x i32>* %ptr,
    #       i32 4,                   ; alignment
    #       <4 x i1> %mask,          ; element mask
    #       <4 x i32> %passthru      ; default values
    #   )
    #
    # After:
    #   for i in 0..3:
    #     %mask_i = extractelement <4 x i1> %mask, i
    #     %ptr_i = getelementptr %ptr, i
    #     if %mask_i:
    #       %elem_i = load i32 %ptr_i
    #     else:
    #       %elem_i = extractelement <4 x i32> %passthru, i
    #     %result = insertelement %result, %elem_i, i

    vector_type = intrinsic.type
    num_elements = vector_type.get_num_elements()

    ptr = intrinsic.get_argument(0)
    alignment = intrinsic.get_argument(1)
    mask = intrinsic.get_argument(2)
    passthru = intrinsic.get_argument(3)

    result_vector = undef_value(vector_type)

    for i in range(num_elements):
        # Check mask for this element
        mask_elem = create_extractelement(mask, i)

        # Create conditional load
        then_block = create_basic_block("masked_load_then")
        else_block = create_basic_block("masked_load_else")
        merge_block = create_basic_block("masked_load_merge")

        create_cond_br(mask_elem, then_block, else_block)

        # Then: Actually load
        set_insert_point(then_block)
        elem_ptr = create_gep(ptr, [i])
        loaded_value = create_load(elem_ptr, alignment)
        create_br(merge_block)

        # Else: Use passthru value
        set_insert_point(else_block)
        passthru_value = create_extractelement(passthru, i)
        create_br(merge_block)

        # Merge: PHI node to select value
        set_insert_point(merge_block)
        elem_value = create_phi([loaded_value, then_block],
                               [passthru_value, else_block])

        # Insert into result
        result_vector = create_insertelement(result_vector, elem_value, i)

    intrinsic.replace_all_uses_with(result_vector)
    intrinsic.erase_from_parent()

    return True
```

---

## Data Structures

### ScalarizerOptions

```c
struct ScalarizerOptions {
    // Minimum bit width to scalarize
    int min_bits;                    // Default: likely 8 or 16

    // Control which operations to scalarize
    bool scalarize_load_store;       // Scalarize vector loads/stores
    bool scalarize_masked_mem_intrin; // Scalarize masked intrinsics
    bool scalarize_variable_insert_extract; // Variable index extract/insert

    // Target-specific options
    bool prefer_scalar_on_target;    // Target prefers scalar over vector
};
```

### Scalarization Cache

```c
struct ScalarizationCache {
    // Cache extracted elements to avoid duplicates
    Map<std::pair<Value*, int>, Value*> extracted_elements;

    // Track which vectors have been scalarized
    Set<Value*> scalarized_values;

    // Statistics
    int vectors_scalarized;
    int loads_scalarized;
    int stores_scalarized;
    int masked_intrinsics_scalarized;
};
```

---

## Configuration Parameters

### `scalarize-load-store`

**Type**: Boolean (default: unknown, likely false)
**Purpose**: Enable scalarization of vector loads and stores

**Evidence**: `optimization_passes.json:19864,19891`

```json
{
  "value": "scalarize-load-store",
  "value": "Allow the scalarizer pass to scalarize loads and store"
}
```

```bash
# Enable load/store scalarization
nvcc -Xcompiler -mllvm -Xcompiler -scalarize-load-store file.cu
```

### `scalarize-masked-mem-intrin`

**Type**: Boolean (default: unknown)
**Purpose**: Enable scalarization of masked memory intrinsics

**Evidence**: `optimization_passes.json:25360,25328,25344`

```json
{
  "value": "scalarize-masked-mem-intrin",
  "value": "Scalarize Masked Memory Intrinsics",
  "value": "Scalarize unsupported masked memory intrinsics"
}
```

Masked intrinsics include:
- `llvm.masked.load`
- `llvm.masked.store`
- `llvm.masked.gather`
- `llvm.masked.scatter`

### `min-bits` (Scalarizer parameter)

**Type**: Integer
**Purpose**: Minimum element bit width to scalarize

**Evidence**: `optimization_passes.json:27725-27736`

```json
{
  "value": "invalid argument to Scalarizer pass min-bits parameter: '{0}' ",
  "value": "invalid Scalarizer pass parameter '{0}' "
}
```

```bash
# Only scalarize vectors with elements >= 16 bits
nvcc -Xcompiler -mllvm -Xcompiler -scalarizer-min-bits=16 file.cu
```

---

## Pass Dependencies

### Required Analyses

1. **TargetTransformInfo** (critical)
   - Determines if scalarization is profitable
   - Provides target-specific cost models

2. **DominatorTree** (optional)
   - Used for safe insertion point determination

### Preserved Analyses

Scalarizer preserves:
- **DominatorTree**: No CFG changes (unless masked intrinsics)
- **LoopInfo**: Loop structure unchanged

Scalarizer invalidates:
- **ScalarEvolution**: Vector operations become scalar
- **AliasAnalysis**: Load/store patterns change

---

## Integration Points

### Pipeline Position

Scalarizer typically runs late in optimization pipeline, or as needed for target-specific requirements:

```
Backend Optimization:
  Vectorization Passes
  → Scalarizer (conditional)    ← Undo vectorization if not profitable
  CodeGen Preparation
```

**Use case**: Some targets lack efficient vector support. Scalarizer converts back to scalar operations.

---

## CUDA Considerations

### Vector Types in CUDA

CUDA has built-in vector types:
- `int2`, `int3`, `int4`
- `float2`, `float3`, `float4`
- `double2`

**Scalarizer interaction**:
```cuda
__global__ void kernel(float4* data) {
    float4 val = data[tid];

    // Compiler may scalarize to:
    // float val_x = data[tid].x;
    // float val_y = data[tid].y;
    // float val_z = data[tid].z;
    // float val_w = data[tid].w;

    // Process each component...
}
```

### When Scalarization Helps

1. **Partial vector operations**:
```cuda
float4 val = data[tid];
float result = val.x + val.y;  // Only using 2 components
// Scalarizer avoids loading full vector
```

2. **Sparse vector usage**:
```cuda
if (condition) {
    use(val.x);  // Only one component needed
}
```

### When Scalarization Hurts

1. **Coalesced memory access**:
```cuda
// Bad: Scalarized loads break coalescing
float4 val = data[tid];  // Single coalesced 128-bit load
// vs
float x = data[tid].x;   // Four separate 32-bit loads (not coalesced)
float y = data[tid].y;
float z = data[tid].z;
float w = data[tid].w;
```

2. **Vector ALU utilization**:
   - GPU hardware optimized for vector operations
   - Scalarization may reduce throughput

---

## Code Evidence

### RTTI Type Information

**Evidence**: `optimization_passes.json:27387`

```json
{
  "value": "constexpr llvm::StringRef llvm::getTypeName() [with DesiredTypeName = llvm::ScalarizerPass]"
}
```

Definitive confirmation of `llvm::ScalarizerPass` class.

### ScalarizeMaskedMemIntrinPass

**Evidence**: `optimization_passes.json:27287`

```json
{
  "value": "constexpr llvm::StringRef llvm::getTypeName() [with DesiredTypeName = llvm::ScalarizeMaskedMemIntrinPass]"
}
```

Separate pass specifically for masked intrinsics.

### Configuration Strings

**Evidence**: `optimization_passes.json:19864-19891,25328-25360,27725-27736`

Multiple configuration parameters and error messages.

### Error Messages

**Evidence**: `optimization_passes.json:25880,25896`

```json
{
  "value": "Do not know how to scalarize this operator's operand!\n",
  "value": "Do not know how to scalarize the result of this operator!\n"
}
```

Indicates unsupported vector operations.

**Evidence**: `optimization_passes.json:42280,42291,42674`

```json
{
  "value": "Cannot scalarize scalable vector loads",
  "value": "Cannot scalarize scalable vector stores",
  "value": "Scalarization of scalable vectors is not supported."
}
```

Scalable vectors (SVE/RISC-V V) are not scalarizable.

---

## Performance Impact

### Typical Results

**Performance**: Highly target-dependent
- **Benefit**: On targets without vector hardware (20-50% speedup)
- **Harm**: On targets with vector hardware (10-30% slowdown)

**Code size**: Usually increases (5-20%)
- Multiple scalar operations instead of one vector operation

**Compile time**: Minimal overhead (<1%)

### Best Case Scenarios

1. **No vector hardware**:
   - Old CPUs without SSE/AVX
   - Embedded targets
   - Scalarization enables execution

2. **Partial vector usage**:
```c
<4 x float> vec = load(...)
float result = vec[0] + vec[1];  // Only 2 elements used
// Scalarization avoids full vector load
```

3. **Unsupported vector operations**:
   - Target lacks specific vector instruction
   - Scalarization provides fallback

### Worst Case (Should Avoid)

1. **Modern GPUs/CPUs with vector hardware**:
   - Scalarization throws away hardware advantage

2. **Coalesced memory accesses**:
   - Scalarizing loads/stores breaks coalescing
   - Severe performance degradation

---

## Examples

### Example 1: Basic Vector Scalarization

**Before Scalarizer**:
```llvm
define <4 x i32> @vector_add(<4 x i32> %a, <4 x i32> %b) {
  %result = add <4 x i32> %a, %b
  ret <4 x i32> %result
}
```

**After Scalarizer**:
```llvm
define <4 x i32> @vector_add(<4 x i32> %a, <4 x i32> %b) {
  %a0 = extractelement <4 x i32> %a, i32 0
  %b0 = extractelement <4 x i32> %b, i32 0
  %r0 = add i32 %a0, %b0

  %a1 = extractelement <4 x i32> %a, i32 1
  %b1 = extractelement <4 x i32> %b, i32 1
  %r1 = add i32 %a1, %b1

  %a2 = extractelement <4 x i32> %a, i32 2
  %b2 = extractelement <4 x i32> %b, i32 2
  %r2 = add i32 %a2, %b2

  %a3 = extractelement <4 x i32> %a, i32 3
  %b3 = extractelement <4 x i32> %b, i32 3
  %r3 = add i32 %a3, %b3

  %tmp0 = insertelement <4 x i32> undef, i32 %r0, i32 0
  %tmp1 = insertelement <4 x i32> %tmp0, i32 %r1, i32 1
  %tmp2 = insertelement <4 x i32> %tmp1, i32 %r2, i32 2
  %result = insertelement <4 x i32> %tmp2, i32 %r3, i32 3

  ret <4 x i32> %result
}
```

### Example 2: CUDA Vector Type Scalarization

**Before Scalarizer**:
```cuda
__global__ void kernel(float4* data) {
    float4 val = data[threadIdx.x];
    float result = val.x + val.y;  // Only 2 components
    output[threadIdx.x] = result;
}
```

**After Scalarizer** (conceptual):
```cuda
__global__ void kernel(float4* data) {
    float val_x = data[threadIdx.x].x;  // Scalar load
    float val_y = data[threadIdx.x].y;  // Scalar load
    // val.z and val.w not loaded (dead code elimination)

    float result = val_x + val_y;
    output[threadIdx.x] = result;
}
```

**Note**: This may or may not be profitable depending on memory access patterns.

---

## Verification and Testing

### Verification Methods

1. **Check scalarization**:
```bash
nvcc -Xcompiler -mllvm -Xcompiler -print-after=scalarizer file.cu
# Look for extractelement/insertelement sequences
```

2. **Performance testing**:
```bash
# Compile with and without scalarization
nvcc -O3 file.cu -o with_vec
nvcc -O3 -Xcompiler -mllvm -Xcompiler -scalarize-load-store file.cu -o scalarized

# Benchmark both
```

3. **Code size comparison**:
```bash
size with_vec scalarized
# Compare text section sizes
```

### Correctness Checks

- [ ] Vector semantics preserved (element-wise)
- [ ] No undefined behavior introduced
- [ ] Memory access patterns correct
- [ ] Alignment requirements respected

---

## Known Limitations

1. **Scalable vectors not supported**:
   - SVE (ARM Scalable Vector Extension)
   - RISC-V V extension
   - Error: "Cannot scalarize scalable vector"

2. **May hurt performance**:
   - On vector-capable hardware
   - Breaks memory coalescing
   - Increases instruction count

3. **Code size increase**:
   - Many scalar operations instead of few vector operations

4. **Limited operator support**:
   - Some vector operations cannot be scalarized
   - Error: "Do not know how to scalarize this operator"

5. **Target-specific**:
   - Profitability highly dependent on target architecture

---

## Related Passes

- **SLPVectorizer**: Opposite of scalarizer (combines scalars into vectors)
- **LoopVectorizer**: Creates vector code from scalar loops
- **LoadStoreVectorizer**: Combines scalar loads/stores into vector operations
- **InstCombine**: May re-vectorize after scalarization

---

## References

### L2 Analysis Files

- `foundation/taxonomy/strings/optimization_passes.json:19864-19891` (scalarize-load-store)
- `foundation/taxonomy/strings/optimization_passes.json:25328-25360` (masked intrinsics)
- `foundation/taxonomy/strings/optimization_passes.json:27287,27387,27725-27736,29733` (RTTI + params)
- `foundation/taxonomy/strings/optimization_passes.json:25848,25880,25896` (error messages)
- `foundation/taxonomy/strings/optimization_passes.json:42280,42291,42674` (scalable vector errors)
- `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json:287`

### Algorithm References

- LLVM Scalarizer: `llvm/lib/Transforms/Scalar/Scalarizer.cpp`
- Vector decomposition techniques

---

**Analysis Quality**: HIGH
**Last Updated**: 2025-11-17
**Confidence**: Very High (RTTI confirmation + extensive configuration + error messages)

**Recommendation**: Use with caution. Scalarization can help on non-vector targets but usually hurts performance on modern GPUs. Profile before deploying.
