# Alias Analysis Infrastructure and Passes

**Pass Category**: Analysis passes for pointer aliasing
**LLVM Infrastructure**: AAManager with multiple AA implementations
**Algorithm**: Various (flow-sensitive, type-based, etc.)
**Extracted From**: CICC analysis infrastructure
**Analysis Quality**: HIGH - Core compiler infrastructure
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

Alias Analysis (AA) determines whether two pointers may refer to the same memory location. This is fundamental for all memory optimizations. CICC uses a layered AA infrastructure with multiple specialized analyses.

**Key Innovation**: Composable AA framework—multiple analyses provide results, aggregated by AAManager.

---

## Alias Analysis Results

### Query Types

```c
enum AliasResult {
    NoAlias,        // Pointers definitely don't alias
    MayAlias,       // Pointers might alias (conservative)
    PartialAlias,   // Pointers partially overlap
    MustAlias       // Pointers definitely alias (same location)
};

// Query interface
AliasResult alias(const MemoryLocation& A, const MemoryLocation& B);
ModRefInfo getModRefInfo(const Instruction* I, const MemoryLocation& Loc);
```

### Conservative to Precise Spectrum

```
Most Conservative                                Most Precise
      │                                                │
      ▼                                                ▼
BasicAA → TBAA → ScopedNoAliasAA → GlobalsAA → CFL-Anders/Steens
   (Always runs)     (Type info)    (restrict)    (Globals)    (Points-to)
```

---

## Individual AA Passes

## 1. TBAA - Type-Based Alias Analysis

**Algorithm**: Uses type information to prove non-aliasing

```c
// Different types don't alias (in C/C++ strict aliasing rules)
float* f_ptr;
int* i_ptr;
// TBAA: f_ptr and i_ptr cannot alias (different types)

struct A { int x; };
struct B { int y; };
struct A* a_ptr;
struct B* b_ptr;
// TBAA: a_ptr and b_ptr cannot alias (different struct types)
```

**LLVM IR Representation**:

```llvm
!0 = !{!"int"}
!1 = !{!"float"}

%v1 = load i32, i32* %p1, !tbaa !0
store float %val, float* %p2, !tbaa !1
; TBAA: p1 and p2 don't alias (different types)
```

**Configuration**:
- `enable-tbaa`: Enable/disable TBAA
- `tbaa-strict`: Strict C/C++ aliasing rules

**Limitations**:
- Type punning breaks TBAA
- Union types conservative
- Casts may invalidate

---

## 2. ScopedNoAliasAA - Restrict Qualifier Analysis

**Algorithm**: Analyzes `restrict` keyword and scope-based non-aliasing

```c
// C restrict keyword
void process(float* restrict a, float* restrict b, int n) {
    // Compiler guarantees: a and b don't alias
    for (int i = 0; i < n; i++) {
        a[i] = b[i] * 2.0f;
    }
}
```

**LLVM IR Representation**:

```llvm
!0 = !{!0}  ; Scope: function-level
!1 = !{!1}  ; Scope: different restrict

%v = load float, float* %a, !alias.scope !0, !noalias !1
store float %r, float* %b, !alias.scope !1, !noalias !0
; ScopedNoAliasAA: a and b don't alias
```

**CUDA Usage**:
```c
__global__ void kernel(float* __restrict__ input,
                       float* __restrict__ output) {
    // __restrict__ tells compiler input and output don't overlap
    output[tid] = input[tid] * 2.0f;
}
```

---

## 3. GlobalsAA - Global Variable Analysis

**Algorithm**: Analyzes global variables and function side effects

```c
int global_var;
static int file_static;

void func(int* ptr) {
    // GlobalsAA: ptr might alias &global_var
    // GlobalsAA: ptr might alias &file_static
    *ptr = 10;
}

void func2(int* ptr) {
    // If func2 doesn't have address of global_var taken,
    // GlobalsAA: ptr doesn't alias &global_var
}
```

**Analysis**:
- Tracks which globals have address taken
- Tracks which functions can access which globals
- Proves non-aliasing for globals not exposed to function

**Key Benefit**: Enables optimization of global variables in GPU kernels

---

## 4. CFLAndersAA - CFL-Style Andersen's Analysis

**Algorithm**: Flow-insensitive, context-insensitive points-to analysis

**Complexity**: O(N³) worst case, typically O(N²)

```c
// Points-to graph construction
int x, y, z;
int *p, *q;

p = &x;      // p → {x}
q = &y;      // q → {y}
q = p;       // q → {x, y}  (union of p's targets)

*q = 1;      // May modify x or y
```

**Analysis**:
- Builds constraint system
- Solves using fixed-point iteration
- Produces points-to sets

**Precision**: Medium (flow-insensitive loses precision in complex flows)

---

## 5. CFLSteensAA - CFL-Style Steensgaard's Analysis

**Algorithm**: Fast, unification-based points-to analysis

**Complexity**: O(N × α(N)) - near-linear

```c
// Steensgaard's: more conservative than Andersen's
int *p, *q, *r;

p = &x;      // p → {x}
q = &y;      // q → {y}
r = p;       // Unification: p ≡ r → {x}
r = q;       // Unification: p ≡ q ≡ r → {x, y}

// All three point to {x, y} (conservative)
```

**Trade-off**:
- Faster than Andersen's (near-linear)
- Less precise (unification-based)
- Good for large codebases

---

## 6. AAEvaluator - Alias Analysis Testing

**Purpose**: Evaluate precision and performance of AA implementations

**Algorithm**: Runs AA queries and reports statistics

```c
// AAEvaluator tests all pointer pairs
for (Value* V1 : Pointers) {
    for (Value* V2 : Pointers) {
        AliasResult R = AA.alias(V1, V2);
        Stats[R]++;  // Count NoAlias, MayAlias, etc.
    }
}

// Reports:
// - Total queries
// - NoAlias %
// - MayAlias %
// - PartialAlias %
// - MustAlias %
```

**Usage**: Compiler development and debugging

---

## 7. AliasSetTracker - Alias Set Construction

**Purpose**: Groups memory locations into alias sets

**Algorithm**: Union-find based partitioning

```c
struct AliasSet {
    SmallPtrSet<Value*, 4> Pointers;  // All pointers in this set
    ModRefInfo AccessType;            // Read/Write/ReadWrite
    bool isVolatile;
};

class AliasSetTracker {
    SmallVector<AliasSet*> AliasSets;

    AliasSet* findAliasSet(Value* Ptr) {
        // Returns alias set containing Ptr
        // Uses AA to check aliases
    }

    void add(Value* Ptr) {
        // Add pointer to appropriate alias set
        // Merge sets if aliases found
    }
};
```

**Usage**: LICM, loop optimizations (track memory dependencies)

---

## 8. MemoryDependenceAnalysis - Dependency Tracking

**Purpose**: Find dependencies between memory operations (now mostly replaced by MemorySSA)

**Algorithm**: Flow-sensitive memory dependency analysis

```c
struct MemDepResult {
    enum DepType {
        Def,         // Definite dependency
        Clobber,     // May clobber
        NonLocal,    // Depends on multiple blocks
        Unknown      // Conservative
    };

    DepType Type;
    Instruction* Inst;  // Dependent instruction
};

MemDepResult getDependency(LoadInst* LI) {
    // Find the instruction that defines the loaded value
    // Or the clobbering store
}
```

**Status**: Legacy - being replaced by MemorySSA in modern LLVM

---

## 9. MemoryLocation - Location Abstraction

**Purpose**: Represent memory location with size and AA metadata

```c
struct MemoryLocation {
    const Value* Ptr;        // Pointer to memory
    LocationSize Size;       // Size in bytes (may be unknown)
    AAMDNodes AATags;        // TBAA, scope metadata
};

// Usage
MemoryLocation Loc1 = MemoryLocation::get(LoadInst);
MemoryLocation Loc2 = MemoryLocation::get(StoreInst);
AliasResult R = AA.alias(Loc1, Loc2);
```

---

## 10. MemoryBuiltins - Builtin Function Analysis

**Purpose**: Identify and analyze memory allocation/deallocation functions

**Recognized Functions**:
- `malloc`, `calloc`, `realloc`, `free`
- `new`, `delete`
- `alloca`
- CUDA: `cudaMalloc`, `cudaFree`

**Usage**:
```c
bool isMallocCall(const CallInst* CI) {
    // Recognizes malloc-like functions
    // Returns true if allocates memory
}

bool isFreeCall(const CallInst* CI) {
    // Recognizes free-like functions
}

Optional<uint64_t> getAllocSize(const CallInst* CI) {
    // Returns allocated size if statically known
}
```

**Benefits**:
- Optimization of allocation patterns
- Escape analysis
- Memory leak detection

---

## 11. MemDerefPrinter - Dereferenceable Analysis

**Purpose**: Determine how many bytes can be safely dereferenced from a pointer

```c
// Dereferenceable attribute
void process(int* ptr) __attribute__((dereferenceable(16))) {
    // Guaranteed: can access ptr[0..15]
}

// LLVM IR
define void @process(i32* dereferenceable(16) %ptr) {
    %v = load i32, i32* %ptr  ; Safe
    %p2 = getelementptr i32, i32* %ptr, i32 3
    %v2 = load i32, i32* %p2  ; Safe (within 16 bytes)
}
```

**Analysis**: Tracks dereferenceable metadata through transformations

---

## 12. MemorySSAPrinter - MemorySSA Visualization

**Purpose**: Debug and visualize MemorySSA representation

**Output Example**:
```
Function: @kernel
  BasicBlock: entry
    MemDef(LiveOnEntry)
      store i32 1, i32* %ptr
  BasicBlock: left
    MemDef(1)
      store i32 2, i32* %ptr
  BasicBlock: right
    MemDef(1)
      store i32 3, i32* %ptr
  BasicBlock: merge
    MemoryPhi(2, 3)
    MemUse(MemoryPhi)
      %v = load i32, i32* %ptr
```

**Usage**: Debugging memory optimizations

---

## AA Integration in CICC

### AAManager Architecture

```c
class AAManager {
    // Registered AA passes
    SmallVector<std::unique_ptr<AAResult>> AAResults;

    // Query aggregation
    AliasResult alias(MemoryLocation A, MemoryLocation B) {
        AliasResult Best = MayAlias;
        for (auto& AA : AAResults) {
            AliasResult R = AA->alias(A, B);
            if (R == NoAlias) return NoAlias;  // Definitive
            if (R < Best) Best = R;  // More precise
        }
        return Best;
    }
};
```

### Typical AA Stack in CICC

```
AAManager
  ├── BasicAA (always enabled)
  ├── TBAA (type-based)
  ├── ScopedNoAliasAA (restrict)
  ├── GlobalsAA (globals)
  └── CFLSteensAA (points-to)
```

---

## CUDA-Specific AA Considerations

### Address Space Awareness

```c
// Different address spaces don't alias
float addrspace(1)* global_ptr;   // Global memory
float addrspace(3)* shared_ptr;   // Shared memory
// AA: NoAlias (different address spaces)
```

### Thread-Local Analysis

```c
__device__ void kernel() {
    int local_var;  // Thread-private
    // AA: local_var doesn't alias with any other thread's locals
}
```

### Coalescing-Aware Analysis

```llvm
; Consecutive threads access consecutive addresses
%tid = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
%ptr = getelementptr float, float addrspace(1)* %base, i32 %tid

; AA: Different threads' pointers don't alias
; Enables optimization assuming no conflicts
```

---

## Performance Impact

| AA Pass | Precision | Cost | Typical Usage |
|---------|-----------|------|---------------|
| BasicAA | Low | Low | Always enabled |
| TBAA | Medium | Low | Type-based opts |
| ScopedNoAliasAA | Medium | Low | With restrict |
| GlobalsAA | Medium | Medium | Global vars |
| CFLSteensAA | Medium | Low | General |
| CFLAndersAA | High | High | Rarely (expensive) |

---

## Configuration

```bash
# Disable specific AA passes
-mllvm -disable-tbaa
-mllvm -disable-scoped-noalias

# Enable expensive AA
-mllvm -enable-cfl-anders-aa

# AA evaluation
-mllvm -aa-eval
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|-----------|--------|----------|
| **Conservative by default** | Missed optimizations | Use restrict |
| **Type punning** | Breaks TBAA | Avoid or use union |
| **Indirect calls** | Conservative | Devirtualize |
| **Complex pointer arithmetic** | Conservative | Simplify |

---

**L3 Analysis Quality**: HIGH
**Last Updated**: 2025-11-17
**Source**: CICC analysis infrastructure + LLVM AA documentation
**Criticality**: **CRITICAL** - Foundation for all memory optimizations
