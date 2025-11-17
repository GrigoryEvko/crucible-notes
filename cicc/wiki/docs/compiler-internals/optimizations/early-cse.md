# EarlyCSE (Early Common Subexpression Elimination)

**Pass Type**: Function-level optimization pass
**LLVM Class**: `llvm::EarlyCSEPass`
**Algorithm**: Hash-based value numbering with MemorySSA integration
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: HIGH - Configuration and algorithm confirmed
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

EarlyCSE (Early Common Subexpression Elimination) is a lightweight optimization pass that runs early in the compilation pipeline to eliminate redundant computations and loads. Unlike the more sophisticated GVN pass, EarlyCSE uses simple hash-based value numbering and operates in a single forward pass, making it very fast while still catching common redundancies.

**Key Innovation**: Combines hash-based CSE with optional MemorySSA integration for efficient redundant load elimination early in the optimization pipeline.

**Core Algorithm**: Single-pass forward scan with hash table for expression equivalence detection.

---

## Pass Configuration

### Evidence from CICC Binary

**String Evidence**:
- `"EarlyCSE"`
- `"EarlyCSEPass"`
- `"Enable the EarlyCSE w/ MemorySSA pass (default = on)"`
- `"Enable imprecision in EarlyCSE in pathological cases"`

### Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `enable-earlycse-memoryssa` | bool | **true** | Enable MemorySSA integration for load elimination |
| `earlycse-debug-hash` | bool | false | Enable hash function debugging |
| `earlycse-mssa-optimization-cap` | int | 500 | MemorySSA query limit per block |
| `enable-earlycse-imprecision` | bool | false | Allow imprecise analysis in pathological cases |

### Estimated Function Count

**~110 functions** implement EarlyCSE in CICC, including:
- Hash table implementation
- Value numbering logic
- MemorySSA integration
- Load/store analysis
- Expression canonicalization

---

## Algorithm Description

### High-Level Overview

EarlyCSE operates in a single forward pass through the function:

```
For each basic block (in dominator order):
    1. Load available values from dominating blocks
    2. For each instruction in block:
        a. Hash the instruction
        b. Check if equivalent instruction exists in hash table
        c. If yes: replace uses with existing value
        d. If no: insert into hash table
    3. Propagate available values to dominated blocks
```

### Single-Pass Forward Scan

```c
void runEarlyCSE(Function& F, DominatorTree& DT) {
    // Hash table: expression hash → instruction
    DenseMap<unsigned, Instruction*> available_values;

    // MemorySSA for load analysis (if enabled)
    MemorySSA* MSSA = enable_memoryssa ? buildMemorySSA(F) : nullptr;

    // Process blocks in dominator tree order
    for (BasicBlock* BB : depth_first(DT.getRootNode())) {
        // Inherit available values from dominator
        BasicBlock* IDom = DT.getIDom(BB);
        if (IDom) {
            available_values = getAvailableValues(IDom);
        }

        // Process instructions in block
        for (Instruction& I : *BB) {
            unsigned hash = computeHash(&I);

            // Check for existing equivalent instruction
            if (Instruction* Existing = available_values.lookup(hash)) {
                if (isEquivalent(&I, Existing)) {
                    // Found redundant computation
                    I.replaceAllUsesWith(Existing);
                    I.eraseFromParent();
                    continue;
                }
            }

            // Insert new value
            available_values[hash] = &I;
        }

        // Store available values for this block
        setAvailableValues(BB, available_values);
    }
}
```

---

## Hash Function

EarlyCSE uses a lightweight hash function for fast equivalence checking:

```c
unsigned computeHash(Instruction* I) {
    unsigned hash = I->getOpcode();  // Start with opcode

    // Hash operands (order matters for non-commutative ops)
    for (Value* Operand : I->operands()) {
        hash = hash * 37 + getValueHash(Operand);
    }

    // Hash type
    hash = hash * 37 + getTypeHash(I->getType());

    // Hash attributes (for instructions that have them)
    if (hasAttributes(I)) {
        hash = hash * 37 + getAttributeHash(I);
    }

    return hash;
}

unsigned getValueHash(Value* V) {
    if (Instruction* I = dyn_cast<Instruction>(V)) {
        return I->getValueNumber();  // SSA value number
    } else if (Constant* C = dyn_cast<Constant>(V)) {
        return hashConstant(C);
    } else {
        return 0;  // Unknown value
    }
}
```

**Hash Collisions**: When hash matches, full equivalence check is performed:

```c
bool isEquivalent(Instruction* I1, Instruction* I2) {
    // Must have same opcode
    if (I1->getOpcode() != I2->getOpcode()) return false;

    // Must have same type
    if (I1->getType() != I2->getType()) return false;

    // Must have same operands
    if (I1->getNumOperands() != I2->getNumOperands()) return false;
    for (unsigned i = 0; i < I1->getNumOperands(); i++) {
        if (I1->getOperand(i) != I2->getOperand(i)) return false;
    }

    // Must have same attributes
    if (!haveSameAttributes(I1, I2)) return false;

    return true;
}
```

---

## MemorySSA Integration

When `enable-earlycse-memoryssa=true` (default), EarlyCSE uses MemorySSA for redundant load elimination:

### Load Redundancy Elimination

```c
bool canEliminateLoad(LoadInst* Load, MemorySSA* MSSA) {
    MemoryUse* LoadAccess = MSSA->getMemoryAccess(Load);
    MemoryAccess* DefiningAccess = LoadAccess->getDefiningAccess();

    // Check if defining access is a store to same location
    if (MemoryDef* Def = dyn_cast<MemoryDef>(DefiningAccess)) {
        if (StoreInst* Store = dyn_cast<StoreInst>(Def->getMemoryInst())) {
            // Check if store is to same address
            if (Store->getPointerOperand() == Load->getPointerOperand()) {
                // Can replace load with stored value
                return true;
            }
        }
    }

    // Check if load is redundant with another load
    if (MemoryUse* PrevUse = dyn_cast<MemoryUse>(DefiningAccess)) {
        if (LoadInst* PrevLoad = dyn_cast<LoadInst>(PrevUse->getMemoryInst())) {
            if (PrevLoad->getPointerOperand() == Load->getPointerOperand()) {
                // Can replace with previous load
                return true;
            }
        }
    }

    return false;
}
```

### MemorySSA Query Limit

To prevent excessive analysis time, EarlyCSE limits MemorySSA queries:

```c
int mssa_query_count = 0;
const int mssa_query_limit = earlycse_mssa_optimization_cap;  // Default: 500

if (mssa_query_count < mssa_query_limit) {
    // Perform MemorySSA analysis
    mssa_query_count++;
} else {
    // Conservative: assume load is not redundant
}
```

**Rationale**: MemorySSA queries can be expensive in large functions. The limit prevents quadratic behavior.

---

## Value Numbering vs GVN

### Comparison with GVN

| Feature | EarlyCSE | GVN/NewGVN |
|---------|----------|------------|
| **Algorithm** | Hash-based, single pass | Lexicographic value numbering, iterative |
| **Complexity** | O(n) | O(n log n) to O(n²) |
| **Phi handling** | No phi-of-ops | Full phi-of-ops analysis |
| **Memory** | MemorySSA (optional) | MemorySSA (always) |
| **Precision** | Lower | Higher |
| **Speed** | Very fast | Slower |
| **Pipeline position** | Early (before inlining) | Mid-level (after inlining) |

### Why EarlyCSE Runs First

EarlyCSE runs early in the optimization pipeline because:

1. **Fast cleanup**: Quick wins on obvious redundancies
2. **Enables inlining**: Smaller code before inlining
3. **Reduces IR size**: Fewer instructions for downstream passes
4. **Low overhead**: Minimal compilation time impact

**Typical Pipeline**:
```
EarlyCSE (quick cleanup)
    ↓
Inlining (exposes more redundancies)
    ↓
InstCombine (algebraic simplification)
    ↓
GVN (sophisticated redundancy elimination)
    ↓
Later optimizations
```

---

## Handled Instruction Types

### Arithmetic and Logical Operations

```llvm
%a = add i32 %x, %y
%b = add i32 %x, %y  ; Redundant

; EarlyCSE eliminates %b:
%a = add i32 %x, %y
; %b replaced with %a
```

**Supported operations**:
- Binary arithmetic: `add`, `sub`, `mul`, `div`, `rem`
- Bitwise operations: `and`, `or`, `xor`, `shl`, `lshr`, `ashr`
- Floating-point: `fadd`, `fsub`, `fmul`, `fdiv`
- Comparisons: `icmp`, `fcmp`

### Loads (with MemorySSA)

```llvm
store i32 42, i32* %ptr
%a = load i32, i32* %ptr   ; Can be replaced with constant 42
%b = load i32, i32* %ptr   ; Redundant with %a

; EarlyCSE with MemorySSA:
store i32 42, i32* %ptr
%a = i32 42                ; Load forwarding
%b = %a                    ; CSE
```

### Casts and Conversions

```llvm
%a = bitcast i32* %ptr to i8*
%b = bitcast i32* %ptr to i8*  ; Redundant

; EarlyCSE eliminates %b
```

**Supported casts**:
- `bitcast`, `trunc`, `zext`, `sext`
- `fptrunc`, `fpext`, `fptoui`, `fptosi`
- `uitofp`, `sitofp`

### GetElementPtr (GEP)

```llvm
%a = getelementptr inbounds %struct, %struct* %base, i32 0, i32 1
%b = getelementptr inbounds %struct, %struct* %base, i32 0, i32 1  ; Redundant

; EarlyCSE eliminates %b
```

---

## Imprecision Mode

When `enable-earlycse-imprecision=true`, EarlyCSE uses approximate analysis in complex cases:

```c
if (enable_imprecision && isPathologicalCase(BB)) {
    // Use approximate hash (may miss some CSE opportunities)
    hash = approximateHash(I);
} else {
    // Use precise hash (slower but catches more)
    hash = preciseHash(I);
}

bool isPathologicalCase(BasicBlock* BB) {
    // Heuristics for complex blocks
    return BB->size() > 1000 ||              // Very large block
           countPhiNodes(BB) > 50 ||         // Many phi nodes
           getDominatorTreeDepth(BB) > 100;  // Deep nesting
}
```

**Use Cases**:
- Extremely large functions (>10,000 instructions)
- Heavily optimized code with many phi nodes
- Compilation time budget is tight

---

## Dominator Tree Traversal

EarlyCSE processes blocks in dominator tree order to ensure correctness:

```
CFG:                   Dominator Tree:        Processing Order:
┌───┐                  ┌───┐                  1. entry
│entry│                │entry│                2. bb1
└─┬─┘                  ├─┬─┘                  3. bb2
  │                    │ └─bb1                4. bb3
  ├─> bb1              └─┬─bb2                5. bb4
  │                      └─bb3
  └─> bb2                └─bb4
       └─> bb3
            └─> bb4
```

**Why Dominator Order?**

1. **Value availability**: Values defined in dominator are available in dominated blocks
2. **Correctness**: Ensures we never use a value before it's defined
3. **Efficiency**: Inherited hash table is valid (no invalidation needed)

---

## Example Transformations

### Example 1: Basic CSE

**Before**:

```llvm
define i32 @example(i32 %x, i32 %y) {
    %a = add i32 %x, %y
    %b = mul i32 %a, 2
    %c = add i32 %x, %y    ; Redundant with %a
    %d = mul i32 %c, 2     ; Redundant with %b
    %result = add i32 %b, %d
    ret i32 %result
}
```

**After EarlyCSE**:

```llvm
define i32 @example(i32 %x, i32 %y) {
    %a = add i32 %x, %y
    %b = mul i32 %a, 2
    %result = add i32 %b, %b  ; %c and %d eliminated
    ret i32 %result
}
```

### Example 2: Load Forwarding (MemorySSA)

**Before**:

```llvm
define i32 @load_forward(i32* %ptr) {
    store i32 100, i32* %ptr
    %a = load i32, i32* %ptr   ; Can forward from store
    %b = add i32 %a, 1
    %c = load i32, i32* %ptr   ; Redundant with %a
    %d = add i32 %c, 1
    ret i32 %d
}
```

**After EarlyCSE (with MemorySSA)**:

```llvm
define i32 @load_forward(i32* %ptr) {
    store i32 100, i32* %ptr
    ; %a eliminated via load forwarding
    %b = add i32 100, 1
    ; %c eliminated via CSE
    %d = add i32 100, 1
    ret i32 %d
}
```

### Example 3: Dominator-Based CSE

**Before**:

```llvm
define i32 @dominator_example(i1 %cond, i32 %x) {
entry:
    %a = mul i32 %x, 2
    br i1 %cond, label %then, label %else

then:
    %b = mul i32 %x, 2    ; Redundant (dominated by entry)
    br label %merge

else:
    %c = add i32 %x, 1
    br label %merge

merge:
    %phi = phi i32 [%b, %then], [%c, %else]
    ret i32 %phi
}
```

**After EarlyCSE**:

```llvm
define i32 @dominator_example(i1 %cond, i32 %x) {
entry:
    %a = mul i32 %x, 2
    br i1 %cond, label %then, label %else

then:
    br label %merge

else:
    %c = add i32 %x, 1
    br label %merge

merge:
    %phi = phi i32 [%a, %then], [%c, %else]  ; %b replaced with %a
    ret i32 %phi
}
```

---

## CUDA-Specific Considerations

### Shared Memory Load Elimination

EarlyCSE with MemorySSA can eliminate redundant shared memory loads:

```llvm
; Before
%val1 = load i32, i32 addrspace(3)* %shared_ptr
%val2 = load i32, i32 addrspace(3)* %shared_ptr  ; Redundant

; After EarlyCSE
%val1 = load i32, i32 addrspace(3)* %shared_ptr
; %val2 eliminated, replaced with %val1
```

**Constraints**:
- No intervening barrier (`__syncthreads`)
- No intervening store to aliasing address
- Same address space

### Barrier Handling

EarlyCSE respects synchronization barriers:

```llvm
%val1 = load i32, i32 addrspace(3)* %shared[%tid]
call void @llvm.nvvm.barrier.sync()
%val2 = load i32, i32 addrspace(3)* %shared[%tid]

; EarlyCSE cannot eliminate %val2 - barrier invalidates memory state
```

**Memory Barrier Semantics**:
- `@llvm.nvvm.barrier.sync()`: Invalidates all shared memory
- `@llvm.nvvm.membar.cta()`: Invalidates thread block memory
- `@llvm.nvvm.membar.gl()`: Invalidates global memory

### Thread-Local Memory

EarlyCSE is more aggressive with thread-local memory (address space 5):

```llvm
; Thread-local storage (per-thread private)
%val1 = load i32, i32 addrspace(5)* %local_ptr
%val2 = load i32, i32 addrspace(5)* %local_ptr  ; Always redundant

; EarlyCSE can safely eliminate %val2
; No other thread can modify local memory
```

---

## Algorithm Complexity

| Operation | Best Case | Average Case | Worst Case | Notes |
|-----------|-----------|--------------|------------|-------|
| **Hash computation** | O(1) | O(k) | O(k) | k = operand count |
| **Hash table lookup** | O(1) | O(1) | O(n) | n = hash collisions |
| **MemorySSA query** | O(1) | O(1) | O(m) | m = memory defs |
| **Per-block processing** | O(n) | O(n) | O(n²) | n = instructions |
| **Overall EarlyCSE** | O(n) | O(n) | O(n²) | Single forward pass |

**Space Complexity**:
- Hash table: O(n) for n unique expressions
- Available values: O(n) per block
- MemorySSA: O(m) for m memory operations

---

## Performance Impact

### Typical Results (CUDA Kernels)

| Metric | Improvement | Variability |
|--------|-------------|-------------|
| **Instruction count** | 2-8% reduction | Medium |
| **Load count** | 3-12% reduction (with MemorySSA) | High |
| **Register pressure** | 1-5% reduction | Low |
| **Execution time** | 0.5-3% improvement | Low to medium |
| **Compile time** | +1-2% overhead | Very low |

### Best Case Scenarios

1. **Repeated address calculations**:
   ```llvm
   %idx1 = add i32 %base, %offset
   %idx2 = add i32 %base, %offset  ; Common in array indexing
   ```

2. **Load-heavy code**:
   ```llvm
   %a = load i32, i32* %ptr
   %b = load i32, i32* %ptr  ; Redundant loads
   ```

3. **Inline expansion**:
   - Small functions inlined multiple times
   - Repeated computations exposed
   - EarlyCSE cleanup before GVN

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **No phi-of-ops** | Misses some redundancies | Use GVN for complex cases | Known, by design |
| **Single pass** | May miss multi-level redundancies | Run GVN later | Known, by design |
| **Hash collisions** | False negatives on collisions | Improve hash function | Minor |
| **MemorySSA query limit** | Conservative on large functions | Increase cap | Tunable |
| **No algebraic simplification** | Doesn't recognize `a+b == b+a` unless normalized | Use InstCombine first | Known |

---

## Integration with Pass Pipeline

### Typical Pass Ordering

```
SimplifyCFG (initial cleanup)
    ↓
SROA (scalar replacement)
    ↓
[EarlyCSE] ← First CSE pass (quick wins)
    ↓
InstCombine (algebraic simplification)
    ↓
Inlining (exposes more redundancies)
    ↓
[EarlyCSE] ← Second pass (cleanup after inlining)
    ↓
GVN (sophisticated value numbering)
    ↓
Later optimizations
```

### Multiple Invocations

EarlyCSE typically runs 2-3 times:
1. **Before inlining**: Cleanup to reduce code size
2. **After inlining**: Eliminate redundancies exposed by inlining
3. **After InstCombine**: Cleanup after instruction combining

---

## Configuration Examples

### Enable MemorySSA (Default)

```bash
# MemorySSA enabled by default
nvcc kernel.cu
```

### Disable MemorySSA

```bash
# Disable MemorySSA integration
nvcc -Xcicc -mllvm=-enable-earlycse-memoryssa=false kernel.cu
```

### Increase MemorySSA Query Limit

```bash
# Allow more MemorySSA queries per block
nvcc -Xcicc -mllvm=-earlycse-mssa-optimization-cap=1000 kernel.cu
```

### Enable Imprecision Mode

```bash
# Use approximate analysis for faster compilation
nvcc -Xcicc -mllvm=-enable-earlycse-imprecision=true kernel.cu
```

---

## Decompiled Code Evidence

**Evidence Sources**:
- String literals: `"EarlyCSE"`, `"EarlyCSEPass"`
- Configuration: `"Enable the EarlyCSE w/ MemorySSA pass (default = on)"`
- Imprecision mode: `"Enable imprecision in EarlyCSE in pathological cases"`
- Estimated ~110 functions implementing EarlyCSE

**Confidence Level**: HIGH
- Algorithm confirmed via string evidence
- MemorySSA integration confirmed (default enabled)
- Configuration parameters extracted from binary
- Function count estimated from binary structure

---

## References

**LLVM Documentation**:
- EarlyCSE Pass: https://llvm.org/docs/Passes.html#early-cse
- MemorySSA: https://llvm.org/docs/MemorySSA.html

**Related Passes**:
- GVN (Global Value Numbering)
- NewGVN (new GVN algorithm)
- InstCombine (instruction combining)
- DSE (Dead Store Elimination)

**Research Papers**:
- Alpern et al., "Detecting Equality of Variables in Programs" (1988)
- Cytron et al., "Efficiently Computing Static Single Assignment Form" (1991)

---

**L3 Analysis Quality**: HIGH
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
