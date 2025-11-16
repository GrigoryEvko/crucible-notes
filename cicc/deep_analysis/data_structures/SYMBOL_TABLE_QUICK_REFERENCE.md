# Symbol Table & Scope Management - Quick Reference

## Symbol Entry Structure (128 bytes)

```
Offset  Size  Field Name                Type        Description
------  ----  ----------------------    ----------  ----------------------------------
0       8     next_in_bucket            SymbolEntry* Hash collision chain
8       8     symbol_name               char*       Symbol name string
16      8     full_qualified_name       char*       C++ qualified name
24      8     symbol_type               Type*       Type information pointer
32      4     storage_class             enum        EXTERN/STATIC/AUTO/REGISTER/...
36      8     address_or_offset         uint64_t    Code/data address
44      4     scope_level               int         0=global, 1+=nested
48      8     parent_scope              Scope*      Parent scope pointer
56      8     defining_scope            Scope*      Definition scope
64      8     initialization_value      Expression* Initial value/constant
72      4     attributes                uint32_t    Bitfield (const/volatile/inline/...)
76      4     line_number               int         Source line
80      4     file_index                int         Source file ID
84      1     cuda_memory_space         enum        GLOBAL/SHARED/LOCAL/CONSTANT
85      1     is_cuda_kernel            bool        __global__ flag
86      1     is_cuda_device_func       bool        __device__ flag
87      1     forward_declared          bool        Forward declaration flag
88      8     mangled_name              char*       C++ mangled name (Itanium ABI)
96      8     template_args             TemplateArgs* Template parameters
104     8     overload_chain            SymbolEntry* Next overload variant
112     8     prev_declaration          SymbolEntry* Previous declaration link
120     8     reserved                  uint64_t    Reserved padding
```

## Hash Table Parameters (Estimated)

| Parameter | Estimated Value | Basis |
|-----------|-----------------|-------|
| Bucket Count | 256-4096 | Typical compiler design |
| Load Factor | 0.75-1.0 | Memory patterns |
| Hash Function | djb2 or FNV | Common compiler practice |
| Collision Method | Separate chaining | Pointer chasing patterns |

## Scope Types & Depths

| Scope Type | Depth | Contains |
|-----------|-------|----------|
| Global | 0 | Functions, globals, types |
| Namespace | 1 | Namespace members (C++) |
| Class | 2 | Members, methods (C++) |
| Function | 1-3 | Parameters, locals |
| Block | 2+ | Variables declared in { } |
| For-Init | 2+ | Variables in for(int x; ...) |
| CUDA Kernel | Special | threadIdx, blockIdx, locals |
| CUDA Device | Special | Device function context |
| CUDA Shared | Special | __shared__ variables |

## Symbol Operations Complexity

```
Operation          Complexity    Notes
-----------        ----------    -----------------------------------
create_symbol()    O(1)         Allocate structure, init fields
insert_symbol()    O(1) amort   Hash, insert at bucket head
lookup_symbol()    O(1) exp     Hash lookup, traverse parent chain
scope_enter()      O(1)         Create scope, push stack
scope_exit()       O(n)         n = hash bucket count
```

## CUDA Implicit Parameters (in Kernel Scope)

```
Name        Type    Value                  Range
--------    ----    -----                  -------------------
threadIdx   dim3    Thread in block        (0 to blockDim-1)
blockIdx    dim3    Block in grid          (0 to gridDim-1)
blockDim    dim3    Threads per block      (1 to 1024)
gridDim     dim3    Blocks in grid         (1 to SM_COUNT)
warpSize    int     Threads per warp       32 (current NVIDIA)
```

## CUDA Storage Classes & Memory Spaces

```
Storage Class       Memory Space           Scope         Visibility
--------------      ----------------       -----------   ---------------------
CUDA_SHARED         CUDA_SHARED_MEMORY    Per-block      All threads in block
CUDA_CONSTANT       CUDA_CONSTANT_MEMORY  Global         All threads (read-only)
CUDA_DEVICE         CUDA_LOCAL_MEMORY     Per-thread     Single thread
CUDA_GLOBAL         CUDA_GLOBAL_MEMORY    Global         All threads
```

## Name Resolution Algorithm

```
lookup(name, current_scope):
  scope = current_scope
  while scope != NULL:
    hash = hash_function(name)
    bucket = scope->symbol_table[hash % BUCKET_COUNT]
    for entry in bucket:
      if entry.symbol_name == name:
        return entry
    scope = scope->parent_scope
  return NOT_FOUND
```

## Scope Enter/Exit

```
enter_scope(scope_type):
  new_scope = allocate(Scope)
  new_scope.parent_scope = current_scope
  new_scope.symbol_table = allocate_hash_table(BUCKET_COUNT)
  new_scope.scope_type = scope_type
  push(scope_stack, new_scope)
  current_scope = new_scope

exit_scope():
  deallocate(current_scope.symbol_table)
  pop(scope_stack)
  current_scope = scope_stack.top()
```

## Key Function Addresses (From Decompilation)

```
Function                          Address     Size    Purpose
----------------------------------  ----------  ------  -------------------------
Frontend Parser                    0x672A20    25.8KB  Parse .cu → AST
Semantic Analysis                  0x1608300   17.9KB  AST → Symbol table
Pass Manager                       0x12D6300   27.4KB  Optimize IR
Register Allocator                 0xB612D0    39KB    Graph coloring
PTX Emitter                        0x9F2A40    45.6KB  IR → PTX code
```

## Evidence Quality Summary

```
Component               Confidence   Evidence
-----------------------  -----------  -----------------------------------------
Symbol table structure   MEDIUM-HIGH  Allocation patterns, lookup detection
Symbol entry size       MEDIUM        Memory distribution analysis
Scope chain lookup      HIGH          Error messages, scope operations
CUDA extensions         MEDIUM-HIGH   CUDA string references
Name mangling           MEDIUM        Mangled name error messages
Memory layout           MEDIUM        Pointer chasing, allocation sizes
```

## Critical Sections in JSON Files

**symbol_table.json**:
- `symbol_entry_structure.fields` - 22 fields with offsets
- `cuda_symbol_handling` - __global__, __device__, __shared__
- `function_mapping` - 0x672A20, 0x1608300 addresses
- `validation_hypotheses` - Testable claims

**scope_management.json**:
- `scope_types` - 9 scope type definitions
- `scope_management_structure` - Scope entry layout
- `cuda_scope_extensions` - Kernel implicit parameters
- `scope_chain_examples` - Code examples with diagrams

## For Further Analysis

### Memory Dump Analysis
- Find symbol table allocation pattern in heap
- Measure actual symbol entry sizes
- Verify hash bucket organization
- Track scope creation/destruction

### Decompilation Focus
- 0x672A20 - Frontend parser symbol operations
- 0x1608300 - Scope stack management
- Hash function implementation
- Symbol lookup hot paths

### Test Programs
- Compile simple CUDA kernels with tracing
- Vary nesting depth to observe scope behavior
- Use __shared__, __device__, __global__ qualifiers
- Trigger name shadowing and redeclaration errors

### Validation Tests
- Symbol lookup performance (should be O(1))
- Scope enter/exit overhead
- Mangled name generation correctness
- CUDA implicit parameter availability

---

**Last Updated**: 2025-11-16
**Confidence Level**: MEDIUM-HIGH (70-75%)
**Status**: Ready for L2 synthesis and external validation
