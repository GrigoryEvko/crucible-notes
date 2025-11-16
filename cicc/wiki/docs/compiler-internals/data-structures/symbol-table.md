# Symbol Table

## Structure Size

```
Total: 128 bytes
Alignment: 8 bytes
Cache lines: 2 (64-byte cache lines)
```

## SymbolEntry Structure Layout

```c
struct SymbolEntry {
    // Offset 0x00 (8 bytes)
    SymbolEntry*      next_in_bucket;        // Collision chain pointer

    // Offset 0x08 (8 bytes)
    const char*       symbol_name;           // Null-terminated name

    // Offset 0x10 (8 bytes)
    const char*       full_qualified_name;   // Namespace/class prefix

    // Offset 0x18 (8 bytes)
    Type*             symbol_type;           // Type descriptor

    // Offset 0x20 (4 bytes)
    enum StorageClass storage_class;         // See enum below

    // Offset 0x24 (8 bytes)
    uint64_t          address_or_offset;     // Memory address/offset

    // Offset 0x2C (4 bytes)
    int               scope_level;           // Nesting depth (0=global)

    // Offset 0x30 (8 bytes)
    Scope*            parent_scope;          // Parent scope pointer

    // Offset 0x38 (8 bytes)
    Scope*            defining_scope;        // Definition location

    // Offset 0x40 (8 bytes)
    Expression*       initialization_value;  // Initial value expr

    // Offset 0x48 (4 bytes)
    uint32_t          attributes;            // Bitfield (see below)

    // Offset 0x4C (4 bytes)
    int               line_number;           // Source line

    // Offset 0x50 (4 bytes)
    int               file_index;            // File table index

    // Offset 0x54 (1 byte)
    enum CudaMemory   cuda_memory_space;     // GLOBAL/SHARED/LOCAL/CONSTANT/GENERIC

    // Offset 0x55 (1 byte)
    bool              is_cuda_kernel;        // __global__ flag

    // Offset 0x56 (1 byte)
    bool              is_cuda_device_func;   // __device__ flag

    // Offset 0x57 (1 byte)
    bool              forward_declared;      // Forward decl tracking

    // Offset 0x58 (8 bytes)
    const char*       mangled_name;          // C++ mangled linker name

    // Offset 0x60 (8 bytes)
    TemplateArgs*     template_args;         // Template instantiation

    // Offset 0x68 (8 bytes)
    SymbolEntry*      overload_chain;        // Overload linkage

    // Offset 0x70 (8 bytes)
    SymbolEntry*      prev_declaration;      // Declaration history

    // Offset 0x78 (8 bytes)
    uint64_t          reserved;              // Padding/future use
};
// Total: 128 bytes (0x00 - 0x7F)
```

## Enumerations

### StorageClass (4 bytes)

```c
enum StorageClass {
    EXTERN        = 0,
    STATIC        = 1,
    AUTO          = 2,
    REGISTER      = 3,
    TYPEDEF       = 4,
    PARAMETER     = 5,
    CUDA_SHARED   = 6,
    CUDA_CONSTANT = 7,
    CUDA_DEVICE   = 8,
    CUDA_GLOBAL   = 9
};
```

### CudaMemorySpace (1 byte)

```c
enum CudaMemorySpace {
    GLOBAL    = 0,
    SHARED    = 1,
    LOCAL     = 2,
    CONSTANT  = 3,
    GENERIC   = 4
};
```

### Attributes Bitfield (uint32_t at offset 0x48)

```
Bit 0:  used
Bit 1:  extern
Bit 2:  static
Bit 3:  inline
Bit 4:  const
Bit 5:  volatile
Bit 6:  restrict
Bit 7:  cuda_specific_flag_1
Bit 8:  cuda_specific_flag_2
Bits 9-31: Reserved
```

## Hash Table Parameters

```
Bucket Count (estimated):  1024
Bucket Count Range:        256 - 4096
Power-of-2:                Yes (for mask-based indexing)
Confidence:                MEDIUM (70%)

Load Factor (estimated):   0.75
Load Factor Range:         0.5 - 1.5
Confidence:                LOW-MEDIUM (60%)

Collision Method:          Separate chaining
Confidence:                HIGH (95%)

Initial Table Size:        BUCKET_COUNT * 8 bytes (8192 bytes for 1024 buckets)
Growth Strategy:           2x multiplication
Rehash Trigger:            symbol_count >= bucket_count * load_factor
```

## Hash Function

### Candidate 1: DJB2 (Probability: 45%)

```c
unsigned long hash_djb2(const char* str) {
    unsigned long hash = 5381;
    int c;
    while ((c = *str++))
        hash = ((hash << 5) + hash) + c;  // hash * 33 + c
    return hash;
}
```

**Constants**: `5381`, `33`
**Operations**: Left shift 5, addition
**Complexity**: O(n) where n = string length

### Candidate 2: Multiplicative Hash (Probability: 50%)

```c
unsigned long hash_mult(const char* str) {
    unsigned long hash = 0;
    while (*str)
        hash = hash * 31 + *str++;
    return hash;
}
```

**Constants**: `31` (or `33`, `37`)
**Operations**: Multiplication, addition
**Complexity**: O(n) where n = string length

### Candidate 3: FNV-1a (Probability: 20%)

```c
unsigned long hash_fnv1a(const char* str) {
    unsigned long hash = 2166136261u;  // FNV offset basis
    while (*str) {
        hash ^= (unsigned char)*str++;
        hash *= 16777619u;  // FNV prime
    }
    return hash;
}
```

**Constants**: `2166136261`, `16777619`
**Operations**: XOR, multiplication
**Complexity**: O(n) where n = string length

### Bucket Indexing

```c
unsigned int bucket_index = hash & (BUCKET_COUNT - 1);
// For 1024 buckets: hash & 0x3FF
// For 512 buckets:  hash & 0x1FF
// For 2048 buckets: hash & 0x7FF
```

## Operations

### Insert

```c
void symbol_table_insert(Scope* scope, SymbolEntry* entry) {
    // 1. Compute hash
    unsigned long hash = hash_function(entry->symbol_name);

    // 2. Compute bucket index
    unsigned int bucket_idx = hash & (BUCKET_COUNT - 1);

    // 3. Head insertion into collision chain
    entry->next_in_bucket = scope->symbol_table[bucket_idx];
    scope->symbol_table[bucket_idx] = entry;

    // 4. Increment symbol count
    scope->symbol_count++;

    // 5. Check load factor
    if (scope->symbol_count >= BUCKET_COUNT * LOAD_FACTOR) {
        rehash_table(scope);
    }
}
```

**Time Complexity**: O(1) average, O(n) for rehash
**Space Complexity**: O(1)

### Lookup (Unqualified)

```c
SymbolEntry* lookup_symbol(const char* name, Scope* current_scope) {
    // 1. Compute hash once
    unsigned long hash = hash_function(name);

    // 2. Traverse scope chain
    Scope* scope = current_scope;
    while (scope) {
        // 3. Compute bucket index
        unsigned int bucket_idx = hash & (BUCKET_COUNT - 1);

        // 4. Traverse collision chain
        SymbolEntry* entry = scope->symbol_table[bucket_idx];
        while (entry) {
            if (strcmp(entry->symbol_name, name) == 0) {
                return entry;  // Found
            }
            entry = entry->next_in_bucket;
        }

        // 5. Move to parent scope
        scope = scope->parent_scope;
    }

    return NULL;  // Not found
}
```

**Time Complexity**:
- Best case: O(1)
- Average case: O(1) per scope, O(d) total where d = scope depth
- Worst case: O(d * n) where n = symbols per bucket

**Space Complexity**: O(1)

### Lookup (Qualified)

```c
SymbolEntry* lookup_qualified(const char* qualified_name, Scope* global_scope) {
    // Parse qualified name: "namespace::class::symbol"
    // Navigate scope hierarchy directly
    // No scope chain traversal

    unsigned long hash = hash_function(qualified_name);
    unsigned int bucket_idx = hash & (BUCKET_COUNT - 1);

    SymbolEntry* entry = global_scope->symbol_table[bucket_idx];
    while (entry) {
        if (strcmp(entry->full_qualified_name, qualified_name) == 0) {
            return entry;
        }
        entry = entry->next_in_bucket;
    }

    return NULL;
}
```

**Time Complexity**: O(1) average, O(n) worst case
**Space Complexity**: O(1)

### Update

```c
void symbol_table_update(SymbolEntry* entry, Type* new_type) {
    // Direct field modification - no rehashing required
    entry->symbol_type = new_type;
    entry->attributes |= ATTR_MODIFIED;
}
```

**Time Complexity**: O(1)
**Space Complexity**: O(1)

### Delete (Logical)

```c
void symbol_table_delete(Scope* scope, const char* name) {
    unsigned long hash = hash_function(name);
    unsigned int bucket_idx = hash & (BUCKET_COUNT - 1);

    SymbolEntry** indirect = &scope->symbol_table[bucket_idx];
    while (*indirect) {
        if (strcmp((*indirect)->symbol_name, name) == 0) {
            // Unlink from collision chain
            SymbolEntry* to_delete = *indirect;
            *indirect = to_delete->next_in_bucket;

            // Free entry
            free(to_delete);
            scope->symbol_count--;
            return;
        }
        indirect = &(*indirect)->next_in_bucket;
    }
}
```

**Time Complexity**: O(1) average, O(n) worst case
**Space Complexity**: O(1)

### Rehash

```c
void rehash_table(Scope* scope) {
    // 1. Allocate new table (2x size)
    unsigned int old_bucket_count = BUCKET_COUNT;
    unsigned int new_bucket_count = old_bucket_count * 2;
    SymbolEntry** new_table = calloc(new_bucket_count, sizeof(SymbolEntry*));

    // 2. Rehash all entries
    for (unsigned int i = 0; i < old_bucket_count; i++) {
        SymbolEntry* entry = scope->symbol_table[i];
        while (entry) {
            SymbolEntry* next = entry->next_in_bucket;

            // Recompute bucket index with new size
            unsigned long hash = hash_function(entry->symbol_name);
            unsigned int new_idx = hash & (new_bucket_count - 1);

            // Insert into new table (head insertion)
            entry->next_in_bucket = new_table[new_idx];
            new_table[new_idx] = entry;

            entry = next;
        }
    }

    // 3. Replace old table
    free(scope->symbol_table);
    scope->symbol_table = new_table;
    BUCKET_COUNT = new_bucket_count;
}
```

**Time Complexity**: O(n) where n = total symbols
**Space Complexity**: O(2 * BUCKET_COUNT) during rehash

## Performance Characteristics

### Lookup Time

```
Best case:     O(1)     - Symbol in current scope, no collisions
Average case:  O(1)     - Good hash function, load factor < 0.75
Worst case:    O(d*n)   - d = scope depth, n = symbols in bucket
```

### Insertion Time

```
Best case:     O(1)     - Direct head insertion
Average case:  O(1)     - No rehashing
Worst case:    O(n)     - Rehashing triggered
```

### Memory Overhead

```
Per Entry:     128 bytes fixed
Per Bucket:    8 bytes (pointer)
Total Table:   BUCKET_COUNT * 8 + (symbol_count * 128)
Example:       1024 * 8 + (1000 * 128) = 136,192 bytes for 1000 symbols
```

### Cache Line Utilization

```
Entry Size:           128 bytes
Cache Line Size:      64 bytes (typical x86-64)
Cache Lines per Entry: 2
Hot Fields (0-63):    next_in_bucket, symbol_name, full_qualified_name,
                      symbol_type, storage_class, address_or_offset,
                      scope_level, parent_scope, defining_scope
Cold Fields (64-127): initialization_value, attributes, line_number,
                      file_index, cuda_memory_space, mangled_name,
                      template_args, overload_chain, prev_declaration
```

### Collision Statistics (Estimated)

```
Load Factor:         0.75
Avg Chain Length:    1.0 (with good hash function)
Max Chain Length:    ~5 (worst case in practice)
Empty Buckets:       ~25% (at load factor 0.75)
```

## Scope Structure

```c
struct Scope {
    int               scope_id;          // Unique ID
    enum ScopeType    scope_type;        // GLOBAL/FUNCTION/BLOCK/CLASS/etc
    Scope*            parent_scope;      // Enclosing scope
    SymbolEntry**     symbol_table;      // Hash table [BUCKET_COUNT]
    int               symbol_count;      // Symbols in this scope
    int               scope_depth;       // Nesting level (0=global)
    SymbolEntry*      owning_function;   // For nested scopes
    SymbolEntry*      owning_class;      // For class members
    struct {
        bool kernel;
        bool device;
        bool shared;
    } cuda_attrs;
};
// Estimated size: 256 bytes
```

### ScopeType Enumeration

```c
enum ScopeType {
    GLOBAL       = 0,
    NAMESPACE    = 1,
    CLASS        = 2,
    FUNCTION     = 3,
    BLOCK        = 4,
    FOR_INIT     = 5,
    CUDA_KERNEL  = 6,
    CUDA_DEVICE  = 7,
    CUDA_SHARED  = 8
};
```

## Scope Operations

### Enter Scope

```c
Scope* enter_scope(ScopeType type, Scope* current_scope) {
    // 1. Allocate new scope
    Scope* new_scope = malloc(sizeof(Scope));

    // 2. Initialize fields
    new_scope->scope_id = next_scope_id++;
    new_scope->scope_type = type;
    new_scope->parent_scope = current_scope;
    new_scope->symbol_count = 0;
    new_scope->scope_depth = current_scope ? current_scope->scope_depth + 1 : 0;

    // 3. Allocate hash table
    new_scope->symbol_table = calloc(BUCKET_COUNT, sizeof(SymbolEntry*));

    // 4. Return new scope (caller updates current_scope)
    return new_scope;
}
```

**Time Complexity**: O(BUCKET_COUNT)
**Space Complexity**: O(BUCKET_COUNT)

### Exit Scope

```c
Scope* exit_scope(Scope* current_scope) {
    Scope* parent = current_scope->parent_scope;

    // 1. Deallocate symbol table (symbols persist in AST)
    free(current_scope->symbol_table);

    // 2. Free scope structure
    free(current_scope);

    // 3. Return parent scope
    return parent;
}
```

**Time Complexity**: O(1)
**Space Complexity**: O(1)

## Binary Evidence

### Target Functions (Estimated)

```
Hash Function:      Unknown address - Pattern search required
Insert Function:    0x672A20 region (parser with symbol creation, 25.8KB)
Lookup Function:    0x1608300 region (semantic analysis, 17.9KB)
Rehash Function:    Unknown address
Scope Management:   0x1608300 region (semantic analysis)
```

### Decompilation Requirements

#### Hash Function Identification

**Search Patterns**:
```asm
; DJB2 pattern
mov     eax, 5381
.loop:
shl     eax, 5
add     eax, edx
add     eax, ecx
; ...

; Multiplicative pattern
imul    eax, 31
add     eax, ecx
; ...

; Mask-based indexing
and     eax, 0x3FF    ; 1024 buckets
and     eax, 0x1FF    ; 512 buckets
```

#### Bucket Count Identification

**Search Patterns**:
```c
calloc(1024, 8)       // 1024 buckets * 8 bytes per pointer
calloc(512, 8)        // 512 buckets
calloc(2048, 8)       // 2048 buckets

hash & 0x3FF          // Mask for 1024 buckets
hash & 0x1FF          // Mask for 512 buckets
hash & 0x7FF          // Mask for 2048 buckets
```

#### Load Factor Threshold

**Search Patterns**:
```c
if (symbol_count >= bucket_count * 3 / 4)  // 0.75 load factor
if (symbol_count * 4 >= bucket_count * 3)  // Same as above
cmp     eax, ebx
imul    ebx, 3
shr     ebx, 2
```

## Memory Layout Example

### Initial State (1024 buckets, 0 symbols)

```
Symbol Table:        8192 bytes (1024 * 8)
Total:               8192 bytes
```

### After 100 symbols

```
Symbol Table:        8192 bytes
Symbol Entries:      12800 bytes (100 * 128)
Total:               20992 bytes
Load Factor:         100 / 1024 = 0.098
```

### After 750 symbols (approaching threshold)

```
Symbol Table:        8192 bytes
Symbol Entries:      96000 bytes (750 * 128)
Total:               104192 bytes
Load Factor:         750 / 1024 = 0.732
```

### After 769 symbols (triggers rehash at 0.75)

```
Before rehash:
  Symbol Table:      8192 bytes
  Symbol Entries:    98432 bytes (769 * 128)
  Total:             106624 bytes
  Load Factor:       769 / 1024 = 0.751

After rehash:
  Symbol Table:      16384 bytes (2048 * 8)
  Symbol Entries:    98432 bytes (769 * 128)
  Total:             114816 bytes
  Load Factor:       769 / 2048 = 0.375
```

## CUDA-Specific Features

### Implicit Kernel Parameters

```c
// Automatically inserted into __global__ function scope
struct dim3 {
    unsigned int x, y, z;
};

struct dim3 threadIdx;   // Thread ID within block
struct dim3 blockIdx;    // Block ID within grid
struct dim3 blockDim;    // Block dimensions
struct dim3 gridDim;     // Grid dimensions
int warpSize;            // 32 on current NVIDIA GPUs
```

### Storage Class Extensions

```
CUDA_SHARED (6):    __shared__ memory
CUDA_CONSTANT (7):  __constant__ memory
CUDA_DEVICE (8):    __device__ variable/function
CUDA_GLOBAL (9):    __global__ kernel function
```

### Memory Space Tracking

```
GLOBAL (0):      Global device memory
SHARED (1):      Shared memory (per-block)
LOCAL (2):       Local/register memory (per-thread)
CONSTANT (3):    Constant memory (read-only, cached)
GENERIC (4):     Generic address space
```

## Confidence Levels

```
Symbol Entry Size:        HIGH (95%)     - Allocation pattern analysis
Collision Resolution:     HIGH (95%)     - Pointer chasing patterns
Scope Hierarchy:          HIGH (90%)     - Error messages, traversal code
Bucket Count:             MEDIUM (70%)   - Compiler design patterns
Load Factor:              LOW-MED (60%)  - Hash table theory
Hash Function Algorithm:  LOW (40%)      - Requires decompilation
Exact Binary Addresses:   PENDING        - Requires targeted analysis
```

## Validation Requirements

### Phase 2: Decompilation (10-15 hours)

```
1. Decompile hash function implementation    (3-4 hours)
   - Target: 0x672A20 region
   - Extract: Constants, loop structure, formula

2. Confirm bucket count constant             (2-3 hours)
   - Search: calloc/malloc allocations
   - Extract: Exact value (256/512/1024/2048/4096)

3. Locate scope stack implementation         (3-4 hours)
   - Target: 0x1608300 region
   - Confirm: vector vs linked list

4. Extract load factor threshold             (2-3 hours)
   - Search: Comparison operations
   - Extract: Exact threshold value
```

### Phase 3: Runtime Validation (8-10 hours)

```
1. GDB breakpoint analysis
2. Memory dump inspection
3. Hash collision profiling
4. Scope traversal tracing
```
