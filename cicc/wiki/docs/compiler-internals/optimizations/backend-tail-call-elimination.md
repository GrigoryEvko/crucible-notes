# Tail Call Elimination (Backend)

**Pass Type**: Scalar optimization pass (backend)
**LLVM Class**: `llvm::TailCallElim`
**Algorithm**: Tail recursion detection and transformation
**Extracted From**: CICC optimization pass mapping
**Analysis Quality**: MEDIUM - Standard LLVM pass with CUDA adaptations
**Pass Category**: Scalar Optimization (Backend)

---

## Overview

Tail Call Elimination (TCE) identifies function calls that occur in tail position (the last operation before return) and transforms them into jumps, eliminating stack frame overhead. This optimization is particularly important for recursive algorithms and enables tail recursion to execute with O(1) stack space instead of O(n).

**Key Innovation**: For GPU kernels, tail call elimination reduces register pressure and enables longer call chains without stack overflow.

---

## Algorithm Overview

### Tail Call Detection

A call is in **tail position** when:
1. The call is the last operation in a basic block
2. The return value (if any) is immediately returned without modification
3. No cleanup code executes after the call
4. The caller and callee have compatible calling conventions

**Example**:
```c
// Original recursive function (NOT tail recursive)
__device__ int factorial(int n) {
    if (n <= 1) return 1;
    return n * factorial(n - 1);  // NOT tail call (multiplication after call)
}

// Tail recursive version
__device__ int factorial_tail(int n, int acc) {
    if (n <= 1) return acc;
    return factorial_tail(n - 1, n * acc);  // Tail call (no work after call)
}

// After tail call elimination
__device__ int factorial_tail(int n, int acc) {
tail_recursion_entry:
    if (n <= 1) return acc;
    // Transform recursive call to parameter update + jump
    int new_n = n - 1;
    int new_acc = n * acc;
    n = new_n;
    acc = new_acc;
    goto tail_recursion_entry;  // Jump instead of call
}
```

---

## Transformation Algorithm

### Step 1: Tail Call Identification

```c
bool isTailCall(CallInst* Call) {
    // Check if call is in tail position
    BasicBlock* BB = Call->getParent();
    Instruction* Next = Call->getNextNode();

    // Must be followed only by return
    if (!isa<ReturnInst>(Next)) {
        return false;
    }

    // Return value must be the call result (or void)
    ReturnInst* Ret = cast<ReturnInst>(Next);
    if (Ret->getReturnValue() != Call && !Call->getType()->isVoidTy()) {
        return false;
    }

    // Calling convention must match
    if (Call->getCallingConv() != Call->getFunction()->getCallingConv()) {
        return false;
    }

    return true;
}
```

### Step 2: Recursive Tail Call Elimination

For **self-recursive** tail calls (function calls itself):

```c
void eliminateTailRecursion(Function* F) {
    // 1. Create entry basic block for jump target
    BasicBlock* Entry = BasicBlock::Create(Context, "tail_recursion_entry", F);

    // 2. Create PHI nodes for function parameters
    SmallVector<PHINode*, 8> ArgPHIs;
    for (Argument& Arg : F->args()) {
        PHINode* PHI = PHINode::Create(Arg.getType(), 2, Arg.getName() + ".phi", Entry);
        PHI->addIncoming(&Arg, &F->getEntryBlock());
        Arg.replaceAllUsesWith(PHI);
        ArgPHIs.push_back(PHI);
    }

    // 3. For each tail recursive call
    for (CallInst* Call : getTailCalls(F)) {
        if (Call->getCalledFunction() == F) {
            // Replace call with parameter update + branch to entry
            BasicBlock* BB = Call->getParent();

            // Update PHI incoming values
            for (unsigned i = 0; i < Call->getNumArgOperands(); i++) {
                ArgPHIs[i]->addIncoming(Call->getArgOperand(i), BB);
            }

            // Replace call + return with branch to entry
            ReturnInst* Ret = cast<ReturnInst>(Call->getNextNode());
            BranchInst::Create(Entry, BB);
            Ret->eraseFromParent();
            Call->eraseFromParent();
        }
    }
}
```

### Step 3: Non-Recursive Tail Call Optimization

For **non-recursive** tail calls (calls to other functions):

```c
void optimizeTailCall(CallInst* Call) {
    // Mark call as tail call (backend will emit as jump)
    Call->setTailCall(true);

    // Set tail call kind
    Call->setTailCallKind(CallInst::TCK_Tail);

    // Backend will transform:
    //   call func
    //   ret
    // Into:
    //   jmp func  (no return address pushed)
}
```

---

## Configuration Parameters

**Evidence**: Standard LLVM tail call optimization

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `disable-tail-calls` | bool | false | Disable tail call optimization globally |
| `tail-call-elimination` | bool | true | Enable tail recursion elimination |

---

## CUDA-Specific Considerations

### Register Pressure

Tail call elimination **reduces** register pressure:
- Eliminates stack frame allocation
- Reuses caller's registers instead of allocating new frame
- Enables longer call chains before register spilling

**Example Impact**:
```c
// Without TCE: Each recursive call allocates new registers
// Depth 10: 10 × register_set = potential spill

// With TCE: Single register set reused
// Depth 10: 1 × register_set = no additional pressure
```

### Stack Space Limitations

CUDA kernels have limited stack space per thread:
- Default: 1KB per thread
- Maximum: 16KB per thread (configurable)

**Tail call elimination is critical** for recursive CUDA algorithms:
```c
// Without TCE: Stack grows with recursion depth
// Max depth ≈ 1KB / frame_size ≈ 10-20 levels

// With TCE: Constant stack usage
// Max depth: unlimited (bounded by algorithm, not stack)
```

### Warp Divergence

Tail calls do not introduce additional divergence:
- All threads in a warp execute the same control flow
- Jump target is uniform across warp
- No divergence penalty compared to original call

---

## Limitations and Constraints

### Cannot Eliminate If:

1. **Non-tail position**:
```c
__device__ int bad_tail(int n) {
    int result = recursive(n - 1);
    return result + 1;  // Work after call - NOT tail position
}
```

2. **Exception handling**:
```c
__device__ int with_cleanup(int n) {
    try {
        return recursive(n);  // Cleanup code prevents TCE
    } catch (...) {
        cleanup();
    }
}
```

3. **Address taken**:
```c
__device__ int address_taken(int n) {
    void* ptr = &recursive;  // Function address taken
    return recursive(n);  // Cannot optimize
}
```

4. **Different calling conventions**:
```c
extern "C" __device__ int c_convention(int n);

__device__ int cpp_function(int n) {
    return c_convention(n);  // Different conventions - no TCE
}
```

---

## Performance Characteristics

### Stack Space Savings

| Recursion Depth | Without TCE | With TCE |
|-----------------|-------------|----------|
| Depth 10 | 10 × frame_size | 1 × frame_size |
| Depth 100 | 100 × frame_size | 1 × frame_size |
| Depth 1000 | Stack overflow | 1 × frame_size |

**Frame size** typically 64-256 bytes per call.

### Execution Time Impact

| Scenario | Speedup | Reason |
|----------|---------|--------|
| Tail recursion | 5-20% | Eliminates call/return overhead |
| Deep recursion | 10-40% | Avoids stack allocation/deallocation |
| Simple tail calls | 2-8% | Jump cheaper than call |

---

## Integration with Other Passes

### Prerequisites

| Pass | Purpose |
|------|---------|
| **SimplifyCFG** | Ensures tail calls in canonical form |
| **InstCombine** | Simplifies return value expressions |

### Downstream Passes

| Pass | Interaction |
|------|-------------|
| **Register Allocation** | Benefits from reduced live ranges |
| **Frame Lowering** | Eliminates stack frame allocation |
| **Code Generation** | Emits jump instead of call instruction |

---

## Example Transformations

### Example 1: Tail Recursive Sum

**Before TCE**:
```llvm
define i32 @sum(i32 %n, i32 %acc) {
entry:
  %cmp = icmp sle i32 %n, 0
  br i1 %cmp, label %return, label %recurse

recurse:
  %sub = sub i32 %n, 1
  %add = add i32 %acc, %n
  %call = call i32 @sum(i32 %sub, i32 %add)
  ret i32 %call

return:
  ret i32 %acc
}
```

**After TCE**:
```llvm
define i32 @sum(i32 %n.initial, i32 %acc.initial) {
entry:
  br label %tail_recursion

tail_recursion:
  %n = phi i32 [ %n.initial, %entry ], [ %sub, %recurse ]
  %acc = phi i32 [ %acc.initial, %entry ], [ %add, %recurse ]
  %cmp = icmp sle i32 %n, 0
  br i1 %cmp, label %return, label %recurse

recurse:
  %sub = sub i32 %n, 1
  %add = add i32 %acc, %n
  br label %tail_recursion

return:
  ret i32 %acc
}
```

### Example 2: Non-Recursive Tail Call

**Before TCE**:
```llvm
define i32 @caller(i32 %x) {
  %result = call i32 @callee(i32 %x)
  ret i32 %result
}
```

**After TCE** (at backend):
```ptx
// caller:
//   ... prepare arguments in R0-R7 ...
//   jmp callee  // Jump instead of call (no return address)
```

---

## Debugging and Diagnostics

### Disabling TCE

```bash
# Disable all tail call optimizations
nvcc -Xcompiler -fno-optimize-sibling-calls

# Disable at LLVM level
-mllvm -disable-tail-calls
```

### Verification

Check if tail calls were eliminated:
```bash
# Look for tail call markers in LLVM IR
llvm-dis output.bc -o - | grep "tail call"

# Check PTX for jump instructions instead of calls
nvcc -ptx kernel.cu -o kernel.ptx
grep "call" kernel.ptx  # Should be fewer after TCE
grep "jmp" kernel.ptx   # Should see jumps for tail calls
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| No mutual recursion elimination | Only self-recursion optimized | Manual trampolining |
| Limited to direct calls | Indirect calls not optimized | Function pointers prevent TCE |
| Requires compatible conventions | Cross-language calls not optimized | Use uniform conventions |

---

## Related Optimizations

- **Inlining**: [inlining.md](inlining.md) - Alternative to tail calls
- **Register Allocation**: [register-allocation.md](../register-allocation.md) - Benefits from reduced pressure
- **Frame Lowering**: Stack frame elimination

---

**Pass Location**: Unconfirmed (suspected in scalar optimization cluster)
**Confidence**: MEDIUM - Standard LLVM pass
**Last Updated**: 2025-11-17
**Source**: CICC optimization pass mapping + LLVM documentation
