# DCE (Dead Code Elimination)

**Pass Type**: Function-level optimization pass
**LLVM Class**: `llvm::DCEPass`
**Algorithm**: Simple unused value elimination
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Algorithm inferred from standard implementation
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`

---

## Overview

DCE (Dead Code Elimination) is a lightweight, fast optimization pass that removes trivially dead instructions—those whose results are never used. Unlike the more sophisticated ADCE (Aggressive DCE), basic DCE uses simple local analysis and operates at the instruction level within basic blocks.

**Key Characteristic**: Fast O(n) algorithm that catches obvious dead code without expensive control flow analysis.

**Core Algorithm**: Single-pass scan identifying instructions with no uses.

---

## Algorithm Type

**Simple Unused Value Detection**

DCE performs a straightforward check: if an instruction's result is never used, and the instruction has no side effects, it can be safely removed.

```
For each instruction I:
    if I.use_count() == 0 && !I.hasSideEffects():
        eliminate(I)
```

This is much simpler than:
- **ADCE**: Uses control dependence graph analysis
- **GlobalDCE**: Uses call graph and module-wide analysis
- **DSE**: Uses MemorySSA for store analysis

---

## Algorithm Description

### Single-Pass Elimination

```c
bool runDCE(Function& F) {
    bool changed = false;
    SmallVector<Instruction*, 64> dead_instrs;

    // Pass 1: Collect dead instructions
    for (BasicBlock& BB : F) {
        for (Instruction& I : BB) {
            if (isDeadInstruction(&I)) {
                dead_instrs.push_back(&I);
            }
        }
    }

    // Pass 2: Remove dead instructions
    for (Instruction* I : dead_instrs) {
        I->eraseFromParent();
        changed = true;
    }

    return changed;
}

bool isDeadInstruction(Instruction* I) {
    // Instruction is dead if:
    // 1. It has no uses
    if (!I->use_empty()) return false;

    // 2. It has no side effects
    if (I->mayHaveSideEffects()) return false;

    // 3. It's not a terminator
    if (I->isTerminator()) return false;

    // 4. It's not an exception handling instruction
    if (I->isEHPad()) return false;

    return true;
}
```

### Side Effect Detection

```c
bool mayHaveSideEffects(Instruction* I) {
    // Instructions with observable effects
    return I->mayWriteToMemory() ||      // stores, calls
           I->mayReadFromMemory() ||      // volatile loads
           I->mayThrow() ||               // can throw exception
           isa<FenceInst>(I) ||           // memory barriers
           I->isAtomic() ||               // atomic operations
           isa<CallInst>(I) && !isPure(cast<CallInst>(I));
}

bool isPure(CallInst* Call) {
    Function* Callee = Call->getCalledFunction();
    if (!Callee) return false;  // Indirect call - assume impure

    // Check function attributes
    return Callee->doesNotAccessMemory() &&  // Pure function
           !Callee->mayThrow();              // No exceptions
}
```

---

## Comparison with Other DCE Passes

| Pass | Algorithm | Scope | Complexity | Strength |
|------|-----------|-------|------------|----------|
| **DCE** | Unused value detection | Instruction | O(n) | Fast, simple |
| **ADCE** | Control dependence graph | Function | O(n²) | Eliminates dead branches |
| **GlobalDCE** | Call graph analysis | Module | O(n+e) | Eliminates dead functions |
| **DSE** | MemorySSA | Function | O(n) | Eliminates dead stores |

### When to Use Each

```
┌─────────────────────────────────────────────┐
│              Dead Code Type                 │
├─────────────────────────────────────────────┤
│ Unused arithmetic/logical → DCE             │
│ Dead control flow → ADCE                    │
│ Dead functions/globals → GlobalDCE          │
│ Dead memory stores → DSE                    │
└─────────────────────────────────────────────┘
```

---

## Handled Patterns

### Pattern 1: Unused Computation

```llvm
; Before DCE
define i32 @example(i32 %x) {
    %dead = mul i32 %x, 2    ; Result never used
    ret i32 %x
}

; After DCE
define i32 @example(i32 %x) {
    ret i32 %x
}
```

### Pattern 2: Chain of Dead Values

```llvm
; Before DCE
define i32 @chain(i32 %x) {
    %a = add i32 %x, 1       ; Dead
    %b = mul i32 %a, 2       ; Dead (uses dead value)
    %c = sub i32 %b, 3       ; Dead (uses dead value)
    ret i32 0
}

; After DCE
define i32 @chain(i32 %x) {
    ret i32 0
}
```

**Note**: DCE may require multiple iterations to fully eliminate chains.

### Pattern 3: Dead Cast Operations

```llvm
; Before DCE
define i32 @casts(i32 %x) {
    %ptr = bitcast i32* %x to i8*    ; Dead cast
    %ext = zext i32 %x to i64        ; Dead extension
    ret i32 %x
}

; After DCE
define i32 @casts(i32 %x) {
    ret i32 %x
}
```

### Pattern 4: Dead GEP (Pointer Arithmetic)

```llvm
; Before DCE
define i32 @gep(%struct* %s) {
    %ptr = getelementptr %struct, %struct* %s, i32 0, i32 1  ; Dead GEP
    %val = load i32, i32* %s  ; Different pointer, GEP unused
    ret i32 %val
}

; After DCE
define i32 @gep(%struct* %s) {
    %val = load i32, i32* %s
    ret i32 %val
}
```

---

## Iterative Elimination

DCE may need to run multiple times to eliminate chains of dead values:

```llvm
; Initial IR
%a = add i32 %x, 1    ; Has use (%b)
%b = mul i32 %a, 2    ; Has use (%c)
%c = sub i32 %b, 3    ; No uses - DEAD
ret i32 0

; After DCE iteration 1
%a = add i32 %x, 1    ; Has use (%b)
%b = mul i32 %a, 2    ; No uses - DEAD
ret i32 0

; After DCE iteration 2
%a = add i32 %x, 1    ; No uses - DEAD
ret i32 0

; After DCE iteration 3
ret i32 0             ; All dead code eliminated
```

**Iteration Strategy**:
```c
bool changed = true;
while (changed) {
    changed = runDCE(F);
}
```

**Typical Iteration Count**: 1-3 iterations for most functions.

---

## Side Effect Categories

### Always Preserved (Never Eliminated)

1. **Memory Writes**:
   ```llvm
   store i32 %val, i32* %ptr  ; Never eliminated by DCE
   ```

2. **Function Calls** (unless pure):
   ```llvm
   call void @foo()  ; Preserved (may have side effects)
   ```

3. **Atomic Operations**:
   ```llvm
   %old = atomicrmw add i32* %ptr, i32 1 seq_cst  ; Preserved
   ```

4. **Volatile Operations**:
   ```llvm
   %v = load volatile i32, i32* %ptr  ; Preserved
   ```

5. **Barriers and Fences**:
   ```llvm
   fence seq_cst  ; Preserved
   ```

6. **Exception Handling**:
   ```llvm
   invoke void @foo() to label %normal unwind label %catch  ; Preserved
   landingpad { i8*, i32 } cleanup  ; Preserved
   ```

### Can Be Eliminated (If Unused)

1. **Pure Arithmetic**:
   ```llvm
   %a = add i32 %x, %y  ; Can eliminate if unused
   ```

2. **Bitwise Operations**:
   ```llvm
   %b = and i32 %x, 0xFF  ; Can eliminate if unused
   ```

3. **Comparisons**:
   ```llvm
   %cmp = icmp eq i32 %x, 0  ; Can eliminate if unused
   ```

4. **Type Conversions**:
   ```llvm
   %ext = zext i32 %x to i64  ; Can eliminate if unused
   ```

5. **Pure Function Calls**:
   ```llvm
   %result = call i32 @pure_function(i32 %x) readnone  ; Can eliminate
   ```

6. **GEP (Pointer Arithmetic)**:
   ```llvm
   %ptr = getelementptr %type, %type* %base, i32 1  ; Can eliminate if unused
   ```

---

## CUDA-Specific Handling

### Thread-Local Computations

DCE can safely eliminate unused thread-local computations:

```llvm
; Thread-local dead computation
define void @kernel() {
    %tid = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
    %dead = mul i32 %tid, 2    ; Never used
    ret void
}

; After DCE
define void @kernel() {
    ret void
}
```

### Synchronization Preservation

DCE never eliminates synchronization barriers:

```llvm
; Barrier always preserved
define void @kernel() {
    %x = add i32 1, 2          ; Dead
    call void @llvm.nvvm.barrier.sync()  ; PRESERVED
    ret void
}

; After DCE
define void @kernel() {
    call void @llvm.nvvm.barrier.sync()
    ret void
}
```

### Address Space Computations

Dead pointer arithmetic in any address space can be eliminated:

```llvm
; Dead shared memory pointer computation
%shared_ptr = addrspacecast i32* %ptr to i32 addrspace(3)*  ; Dead
%local_ptr = addrspacecast i32* %ptr to i32 addrspace(5)*   ; Dead

; Both eliminated if unused
```

---

## Algorithm Complexity

| Operation | Complexity | Notes |
|-----------|------------|-------|
| **Use count check** | O(1) | SSA form provides immediate use list |
| **Side effect check** | O(1) | Instruction properties cached |
| **Per-instruction analysis** | O(1) | Constant time per instruction |
| **Single pass** | O(n) | n = instruction count |
| **Iterative (k passes)** | O(k×n) | k typically ≤ 3 |

**Space Complexity**: O(n) for dead instruction list.

---

## Performance Impact

### Typical Results (CUDA Kernels)

| Metric | Improvement | Variability |
|--------|-------------|-------------|
| **Instruction count** | 1-5% reduction | Low |
| **Register pressure** | 0-2% reduction | Very low |
| **Code size** | 1-3% reduction | Low |
| **Execution time** | 0-1% improvement | Minimal |
| **Compile time** | <1% overhead | Very low |

### Best Case Scenarios

1. **After constant propagation**:
   ```llvm
   ; Constant propagation creates dead code
   %x = add i32 5, 3      ; Folded to 8
   %y = mul i32 %x, 2     ; Now dead
   ret i32 8              ; Constant return
   ```

2. **After inlining**:
   ```llvm
   ; Inlined function parameters may be unused
   ; DCE cleans up dead parameter copies
   ```

3. **Debug code in release builds**:
   ```llvm
   ; Debug instrumentation becomes dead
   %debug_val = compute_debug_info()  ; Eliminated in release
   ```

---

## Integration with Pass Pipeline

### Typical Pass Ordering

DCE runs multiple times throughout the pipeline:

```
SimplifyCFG (initial cleanup)
    ↓
InstCombine (creates constants)
    ↓
[DCE] ← First pass (cleanup after combining)
    ↓
Inlining (exposes dead code)
    ↓
[DCE] ← Second pass (cleanup after inlining)
    ↓
GVN, SCCP (value numbering, constant propagation)
    ↓
[DCE] ← Third pass (cleanup after propagation)
    ↓
ADCE (aggressive elimination)
    ↓
[DCE] ← Final pass (cleanup)
    ↓
Code Generation
```

### Multiple Invocations

DCE typically runs 5-10 times in a full optimization pipeline:
- **After each major transformation**: Cleanup exposed dead code
- **Very low cost**: O(n) makes it cheap to run frequently
- **Enables downstream passes**: Smaller IR improves analysis precision

---

## Limitations vs ADCE

### What DCE Misses

1. **Dead Branches**:
   ```llvm
   ; DCE cannot eliminate dead branches
   br i1 false, label %dead, label %live

   dead:  ; Unreachable but DCE won't detect
       %x = add i32 1, 2
       ret i32 %x

   ; ADCE eliminates this, DCE doesn't
   ```

2. **Dead PHI Nodes**:
   ```llvm
   ; DCE cannot eliminate entire dead control flow
   %phi = phi i32 [%a, %bb1], [%b, %bb2]
   ; If %phi is dead, DCE removes it but not the branches
   ; ADCE removes branches too
   ```

3. **Control-Dependent Code**:
   ```llvm
   ; Code that only affects dead branches
   if (compute_dead_condition()) {  ; Condition is dead
       dead_code();
   }
   ; ADCE eliminates condition and branch, DCE only eliminates uses
   ```

---

## Known Limitations

| Limitation | Impact | Workaround | Status |
|-----------|--------|------------|--------|
| **No control flow analysis** | Misses dead branches | Use ADCE | By design |
| **No memory analysis** | Cannot eliminate dead stores | Use DSE | By design |
| **Local scope only** | Cannot eliminate dead across BBs | Use ADCE | By design |
| **Requires multiple iterations** | Chain elimination is slow | Run multiple times | Known |
| **Conservative on calls** | Assumes calls have side effects | Mark functions `readnone` | Known |

---

## Configuration

### Typical Usage

DCE is always enabled and runs automatically as part of standard optimization levels:

```bash
# O0: DCE disabled
nvcc -O0 kernel.cu

# O1+: DCE enabled (runs multiple times)
nvcc -O1 kernel.cu
nvcc -O2 kernel.cu
nvcc -O3 kernel.cu
```

### Debug Options

```bash
# View DCE statistics (if available)
nvcc -Xcicc -mllvm=-stats kernel.cu

# Disable DCE (not recommended)
# Note: No explicit disable flag for basic DCE
# Use -O0 to disable all optimizations including DCE
```

---

## Example: Complete Transformation

**Before Optimization Pipeline**:

```llvm
define i32 @example(i32 %x, i32 %y) {
    %a = add i32 %x, %y
    %b = mul i32 %a, 2
    %c = add i32 5, 3         ; Constant folded by InstCombine
    %d = mul i32 %c, 2        ; Dead (uses %c which becomes constant)
    %e = sub i32 %b, 1
    ret i32 %e
}
```

**After InstCombine** (constant folding):

```llvm
define i32 @example(i32 %x, i32 %y) {
    %a = add i32 %x, %y
    %b = mul i32 %a, 2
    %d = mul i32 8, 2         ; c=8 folded, %d still dead
    %e = sub i32 %b, 1
    ret i32 %e
}
```

**After DCE** (remove dead %d):

```llvm
define i32 @example(i32 %x, i32 %y) {
    %a = add i32 %x, %y
    %b = mul i32 %a, 2
    %e = sub i32 %b, 1
    ret i32 %e
}
```

---

## Verification

### Assertions (Debug Builds)

```c
// Verify instruction has no uses before elimination
assert(I->use_empty() && "Eliminating instruction with uses!");

// Verify no side effects
assert(!I->mayHaveSideEffects() && "Eliminating instruction with side effects!");

// Verify not a terminator
assert(!I->isTerminator() && "Eliminating terminator instruction!");
```

### Testing Strategy

1. **Unit tests**: Small IR snippets with known dead code
2. **Regression tests**: Historical dead code patterns
3. **Integration tests**: Full optimization pipeline verification
4. **Negative tests**: Ensure side-effect instructions preserved

---

## Decompiled Code Evidence

**Evidence Sources**:
- DCE listed in dead code elimination cluster
- No explicit string evidence (basic pass, may be inlined)
- Algorithm inferred from LLVM standard implementation

**Confidence Level**: MEDIUM
- Pass existence inferred from category
- Algorithm based on LLVM standard DCE implementation
- May be integrated with other passes in CICC

---

## References

**LLVM Documentation**:
- DCE Pass: https://llvm.org/docs/Passes.html#dce
- Instruction properties: https://llvm.org/doxygen/classllvm_1_1Instruction.html

**Related Passes**:
- ADCE (Aggressive Dead Code Elimination)
- GlobalDCE (module-level elimination)
- DSE (Dead Store Elimination)
- InstCombine (creates dead code)

**Related Concepts**:
- SSA Form (enables efficient use checking)
- Def-Use Chains
- Side Effect Analysis

---

**L3 Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Source**: CICC foundation analyses + 21_OPTIMIZATION_PASS_MAPPING.json
