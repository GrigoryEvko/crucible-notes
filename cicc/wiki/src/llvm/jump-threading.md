# JumpThreading

CICC v13.0 ships LLVM's `JumpThreadingPass` at `sub_2DC4260` (12,932 bytes, address range `0x2DC4260`--`0x2DC74E4`). The pass duplicates basic blocks so that predecessors whose branch conditions can be statically resolved jump directly to the correct successor, eliminating a conditional branch from the critical path. On a GPU, this directly reduces warp divergence: a branch that was previously data-dependent becomes unconditional along each incoming edge, so the warp scheduler never needs to serialize the two paths.

The pass is fundamentally at odds with PTX's requirement for reducible control flow. Block duplication can create multi-entry loops (irreducible cycles) when the duplicated block is a loop header or when the threading target sits inside a loop whose header is not the threading source. CICC addresses this through three layered mechanisms -- loop header protection, conservative duplication thresholds, and a late-pipeline StructurizeCFG safety net -- that collectively keep the CFG reducible without sacrificing the pass's optimization value.

| Property | Value |
|----------|-------|
| Pass name (pipeline parser) | `"jump-threading"` |
| Pass class | `llvm::JumpThreadingPass` |
| Entry function | `sub_2DC4260` |
| Binary size | 12,932 bytes |
| Stack frame | `0x748` (1,864) bytes |
| Block duplication helper | `sub_2DC22F0` (2,797 bytes) |
| CFG finalization | `sub_2DC30A0` (1,094 bytes) |
| Single-instruction threading | `sub_2DC37C0` (2,288 bytes) |
| Select unfolding | `sub_2DC40B0` (420 bytes) |
| Pipeline positions | Three invocations: ~position 234, ~278, and a late tier-3 position (~239) |
| `NVVMPassOptions` disable offset | `+320` |
| Upstream LLVM source | `lib/Transforms/Scalar/JumpThreading.cpp` |

## Why JumpThreading Matters on GPU

Consider a CUDA kernel containing:

```
if (threadIdx.x < threshold)
    val = computeA();
else
    val = computeB();

if (val > 0)
    result = pathX(val);
else
    result = pathY(val);
```

The second branch depends on `val`, which is a PHI of `computeA()` and `computeB()`. If JumpThreading can determine that `computeA()` always returns a positive value, it duplicates the second `if` block and wires the `computeA` predecessor directly to `pathX`. Threads that took the first branch path never execute the second conditional at all.

On a CPU this saves a branch misprediction. On a GPU the payoff is larger: eliminating the second branch prevents a second point of warp divergence. If both branches would diverge on different thread subsets, removing one cuts the total serialization overhead in half. The threads that took `computeA` proceed straight to `pathX` without waiting for the `computeB` threads to rejoin.

## Knob Inventory

Six `cl::opt` globals control the pass, registered in `ctor_456` at `0x544220`:

| Knob | Default | Global | Description |
|------|---------|--------|-------------|
| `jump-threading-threshold` | **6** | `qword_4FFDBA0` | Max instructions in a block eligible for duplication |
| `jump-threading-implication-search-threshold` | **3** | `qword_4FFDAC0` | Max predecessors to search for condition implications |
| `jump-threading-phi-threshold` | **76** (`0x4C`) | `qword_4FFD9E0` | Max PHI nodes in a block eligible for duplication |
| `jump-threading-across-loop-headers` | **false** | `qword_4FFD900` | Allow threading across loop headers (testing only) |
| `jump-threading-disable-select-unfolding` | **false** | `qword_4FFDC80` | Disable unfolding `select` instructions into branches |
| `print-lvi-after-jump-threading` | **false** | -- | Debug: print LazyValueInfo cache after pass completes |

The block-size threshold of 6 matches upstream LLVM. The PHI threshold of 76 is significantly higher than upstream's default (which is typically lower), reflecting GPU kernels' tendency toward wider PHI nodes due to predication and convergence patterns. The implication search depth of 3 is conservative, limiting compile-time cost from predecessor chain analysis in the typically shorter basic-block chains of GPU code.

## Two Disable Flags

CICC registers **two** independent `cl::opt` flags that suppress jump threading behavior. They live in different subsystems and control different things:

| Flag | Registration | Subsystem | Effect |
|------|-------------|-----------|--------|
| `"disable-JumpThreadingPass"` | `ctor_637` @ `0x5934A7` | JumpThreading pass itself | Disables the standalone `JumpThreadingPass` invocations in the pipeline |
| `"disable-jump-threading"` | `ctor_073` @ `0x49A91E` (also `ctor_243` @ `0x4ED0C0`) | SimplifyCFG | Disables jump threading logic *within SimplifyCFG* -- the per-block branch-through-PHI threading that SimplifyCFG performs as part of its CFG simplification |

The `"disable-jump-threading"` flag carries the annotation **"Disable jump threading for OCG experiments"**, where OCG is NVIDIA's Optimizing Code Generation research infrastructure. This is a SimplifyCFG option, not a JumpThreadingPass option -- SimplifyCFG has its own internal implementation of branch threading through PHI nodes that is separate from the standalone pass. NVIDIA engineers can disable either or both independently.

The `"fold-with-var-cond"` flag is registered alongside `"disable-jump-threading"` in the same SimplifyCFG constructor group, controlling a related NVIDIA-specific extension for folding branches with variance conditions.

## Interaction with StructurizeCFG

The fundamental tension: JumpThreading duplicates blocks to bypass conditionals, which can transform a reducible loop into an irreducible cycle. PTX requires all loops to be natural (single-entry, reducible). An irreducible CFG causes StructurizeCFG to emit `"UnsupportedIrreducibleCFG"` and bail out, leaving the function in a state that `ptxas` will likely reject.

CICC addresses this through three layered mechanisms:

### 1. Loop Header Protection via LoopInfo

The `jump-threading-across-loop-headers` flag defaults to `false`. Before threading any block, the pass queries LoopInfo through a red-black tree lookup at `0x2DC4781` using `dword_501D5A8` as the analysis key. If the target block is a loop header (the LoopInfo query returns a non-null loop containing the block as its header), the pass skips it entirely.

A parallel DominatorTree query at `0x2DC4839` (using `dword_501D4C8`) verifies loop membership and nesting depth. If the block is found within a loop, a threshold override is loaded from `qword_501D628`, replacing the standard duplication threshold with a loop-specific one. A second override from `qword_501D548` applies to blocks found via the DominatorTree-based lookup.

This double check -- LoopInfo for header identification, DominatorTree for membership -- prevents the most common source of irreducibility: duplicating a loop header creates a second entry into the loop body.

### 2. Conservative Duplication Thresholds

The three thresholds (6 instructions, 3 predecessors, 76 PHIs) restrict duplication to small, simple blocks where the CFG outcome is highly predictable and the duplication cost is bounded. A block must satisfy all three limits simultaneously. These thresholds interact multiplicatively: even a 6-instruction block with 4 predecessors would exceed the implication search depth and be rejected, while a 5-instruction block with 100 PHIs would exceed the PHI threshold.

### 3. StructurizeCFG Safety Net

StructurizeCFG (`sub_35CC920`) runs late in the pipeline, after all IR-level scalar and loop transforms. Its irreducibility detector (`sub_35CA2C0`) checks every back-edge: if the target does not dominate the source, the loop has multiple entries and is irreducible. If JumpThreading or any other pass creates an irreducible cycle that slipped past the loop header protection, StructurizeCFG will catch it.

This is defense-in-depth: the threading constraints prevent most irreducible cases, and structurization catches the rest. The design deliberately tolerates a small number of "false acceptances" at the JumpThreading level because the cost of occasionally running StructurizeCFG's rejection path is far lower than the cost of being too conservative and missing profitable threading opportunities.

## Cost Model

The pass enforces a multi-level cost model that bounds total code growth per function.

### Global Budget

At `0x2DC4887`, the pass initializes a global instruction budget:

```
mov ebx, 200h    ; 512 instructions total budget
```

Each block duplication charges the duplicated block's instruction count against this budget. The budget is tracked in `var_460` and checked before each duplication. Once exhausted, no further threading occurs in that invocation regardless of how profitable individual candidates might be.

### Per-Predecessor Cost Division

When threading involves multiple predecessors, the per-predecessor cost is the block instruction count divided by the number of predecessors being threaded, with ceiling rounding:

```
cost_per_pred = block_instr_count / num_predecessors
; ceiling via: sbb eax, -1 (adds 1 if remainder was nonzero)
```

This division at `0x2DC4D78`--`0x2DC4D8E` means a 6-instruction block being threaded for 3 predecessors costs only 2 instructions per predecessor against the global budget. The logic recognizes that multi-predecessor threading amortizes the code growth across more eliminated branches.

### Special Cases

- **Single-instruction blocks** (checked at `0x2DC4D94`): Always eligible, regardless of budget. A block containing only a terminator instruction costs nothing to duplicate.
- **Empty blocks** (checked at `0x2DC4D70`): Skipped entirely.
- **Blocks with `<=1` effective instructions** (`0x2DC4BF1`): The comparison `cmp edx, 1; jbe` gates a fast path where the pass bypasses the full cost analysis.

## LazyValueInfo Integration

The pass accepts a `LazyValueInfo` pointer as its third parameter (`rdx`). When non-null (checked at `0x2DC42BD`), LVI provides range-based condition evaluation that enables threading even when the branch condition is not a simple constant comparison.

### LVI State

The LVI cache occupies approximately 600 bytes (`0x258`) of local state:

| Field | Offset | Purpose |
|-------|--------|---------|
| Cache structure | `var_2F0` through `var_98` | LVI range cache local state |
| Valid flag | `var_C0` | Set to 1 when LVI is initialized |
| Cached ranges | `var_B0` | SmallVector-like structure |
| Initial capacity | `var_A8` | 8 entries |

### Range-Based Threading

For `ICMP_NE` conditions (opcode `0xBA` = 186), the pass calls `sub_11F3070` (`LVI::getPredicateAt`) with the ICmp operand and a comparison predicate of 2, followed by `sub_DFABC0` (`evaluateConditionOnEdge`) to resolve the branch direction along a specific incoming edge.

For alternate opcode paths (opcode `0x165` = 357), the pass uses `sub_988330` (`getConstantOnEdge`) instead, which returns a concrete constant value if LVI can prove the condition evaluates to a known value along that edge.

The virtual dispatch at `0x2DC67D6` (`call qword ptr [rax+78h]`) invokes `LVI::getPredicateOnEdge`. If the vtable matches `sub_920130` (the default implementation), a fallback path calls `sub_AC4810` (`isImpliedCondition`) with predicate `0x27` (39), and if that also fails, `sub_AA93C0` (`SimplifyICmpInst`).

### Cleanup

On exit, if LVI was used, three cleanup calls occur:
- `sub_FFCE90` -- `LVI::eraseBlock` (invalidation)
- `sub_FFD870` -- `LVI::clear`
- `sub_FFBC40` -- `LVI::releaseMemory`

## Main Algorithm

### Outer Loop

The pass iterates over the function's basic block list via a linked-list traversal (`BB->next` chain at `[BB+8]`):

```
run(result_ptr, function, lvi_ptr, tli, ...):
    if lvi_ptr != null:
        initialize_lvi_cache(lvi_ptr)

    budget = 512
    changed = false

    loop:
        current_bb = function.entry_block    // sub_B2BEC0
        end = function + 0x48               // end sentinel

        while current_bb != end:
            if try_thread_block(current_bb, budget):
                changed = true
            current_bb = current_bb.next     // [current_bb + 8]

        if changed:
            changed = false
            goto loop    // restart: threading may expose new opportunities

    cleanup_lvi()
    return results
```

The restart-on-change behavior means threading is iterative: eliminating one branch can expose a new statically-determinable branch downstream.

### Per-Block Classification

For each basic block, the pass examines the terminator instruction:

1. **Opcode check** (`0x2DC443E`): The instruction opcode byte is compared against `0x55` (85), which is LLVM's `BranchInst` opcode. Only conditional branches are considered.

2. **Metadata check** (`0x2DC4449`--`0x2DC446E`): Two calls to `sub_A73ED0` check for metadata kinds `0x17` (23, `"prof"` branch weights) and `0x04` (debug). Then `sub_B49560` (`hasMetadataOtherThanDebugLoc`) is called on the branch instruction.

3. **Condition extraction** (`0x2DC45F8`--`0x2DC4636`): `sub_981210` (`getBranchCondition`) returns a success flag and a condition code. Two condition codes are handled:
   - `0x165` (357): likely `CmpInst::ICMP_EQ` or a switch opcode
   - `0x0BA` (186): likely `CmpInst::ICMP_NE`

   Other condition codes cause the block to be skipped.

4. **Operand analysis** (`0x2DC465F`--`0x2DC467C`): The operand count is extracted (AND with `0x7FFFFFF` mask -- the use-count field in LLVM's `Value` layout). If the branch condition is an ICmp with a constant operand (type byte `0x11` = 17 = `ConstantInt`), threading is potentially profitable.

### Condition-Specific Threading Paths

The pass contains four specialized threading strategies:

**Constant-value threading** (`0x2DC66B7`): When a predecessor can determine the branch outcome via a constant PHI incoming value, the simplest path. Creates a direct unconditional branch.

**Single-instruction threading** (`sub_2DC37C0`, 2,288 bytes): For blocks containing exactly one instruction (the terminator), called at `0x2DC6704`. Creates a direct branch bypass.

**Switch threading** (`0x2DC6A76`--`0x2DC6B0C`): When the terminator is a `SwitchInst` (opcode byte `0x37` = 55), calls `sub_2DC40B0` (`tryToUnfoldSelect`). This checks for `SelectInst` (opcode `0x52` = 82) and unfolds the select into explicit branches that can be individually threaded.

**Implication-based threading** (`0x2DC6E71`--`0x2DC6EB3`): For `ICmpInst` variants (opcode `0x28` = 40), the pass checks whether the predicate implies the branch condition via `sub_B532B0`, creates the threaded edge via `sub_B52EF0`, and wires the new block via `sub_92B530`.

### All-Ones Constant Detection

Four sites (`0x2DC71B0`, `0x2DC71CA`, `0x2DC7380`, `0x2DC74DA`) check for all-ones constants as PHI incoming values:

```
or rax, -1          ; create all-ones mask
shr rax, cl         ; cl = 64 - bitwidth, shift to match width
cmp [rdx+18h], rax  ; compare against actual constant value
setz al             ; true if constant is all-ones
```

For an `i1` type, all-ones means `true`. This handles the common pattern where a PHI incoming value from one predecessor is the constant `true` (all bits set), allowing the pass to resolve the branch direction for that predecessor.

### PHI Operand Iteration

Two nearly identical loops at `0x2DC7206`--`0x2DC726E` and `0x2DC7456`--`0x2DC74CD` iterate PHI operands to determine if all incoming values from relevant predecessors resolve to the same constant:

```
for pred_idx in range(phi.num_operands):    // var_668
    incoming = phi.getIncomingValueForBlock(pred)  // sub_AD69F0
    type_tag = incoming.type_byte

    if type_tag == 0x0D:     // ConstantInt::getTrue()
        continue
    if type_tag == 0x11:     // ConstantInt with bitwidth check
        if bitwidth <= 64:
            if value == all_ones_for_width:
                continue     // resolves to true
        else:
            skip             // wide integers, bail out

    // If any incoming value is non-constant, threading is unprofitable
    bail_out()
```

If every relevant predecessor provides the same constant value, the branch direction is fully determined and threading proceeds.

## Created Block Names

When threading occurs, the pass creates new basic blocks with diagnostic names:

| Name | String address | Purpose |
|------|---------------|---------|
| `"endblock"` | `0x42E9094` | Terminal block of the threaded path; created via `sub_F36990` (`SplitBlockAndInsertIfThen`) |
| `"phi.res"` | `0x42E90C0` | PHI resolution node for merged values; created via `sub_D5C860` (`PHINode::Create`) |
| `"res_block"` | `0x42E909D` | Result block for the threaded path; allocated as 0x50-byte BasicBlock via `sub_22077B0` |
| `"loadbb"` | `0x42E90B9` | Load basic block for load-bearing threading; created in a loop at `0x2DC4F05`--`0x2DC4FFB` |
| `"phi.src1"` | `0x42E90A7` | First PHI source block |
| `"phi.src2"` | `0x42E90B0` | Second PHI source block |

The `"loadbb"` blocks are created in a dynamic loop for multi-way threading, where each iteration allocates a 0x50-byte (`sizeof(BasicBlock)`) object and wires it into the CFG via `sub_AA4D50` (`BasicBlock::insertInto`).

## Block Duplication Engine: sub_2DC22F0

The 2,797-byte helper performs actual block cloning. Parameters:

| Register | Role |
|----------|------|
| `rdi` | Duplication context structure (at `var_490`) |
| `rsi` | Source block's value table |
| `rdx` | Destination hash table |
| `rcx` | PHI operand map |
| `r8d` | Instruction count for the source block |

The cloning process:
1. Clone each instruction from the source block
2. Insert cloned instructions into use-def chains (`0x2DC59A1`--`0x2DC59E7`: linked-list surgery on LLVM's Value use-list)
3. Update PHI operands to reference the new predecessor (`0x2DC5E1E` onward)
4. Update branch targets in the predecessor blocks

## CFG Finalization: sub_2DC30A0

The 1,094-byte helper, called at `0x2DC5015` and `0x2DC6408` after threading completes for a block, performs:
- Successor edge updates
- Dead block elimination for blocks made unreachable by the threading
- DominatorTree updates if available (via `sub_FFB3D0`, `DominatorTree::changeImmediateDominator`)

## Pipeline Positions

JumpThreading appears three times in the CICC pipeline, at different stages with different surrounding context:

| Position | Pipeline context | Parameter | Purpose |
|----------|-----------------|-----------|---------|
| ~234 | After ADCE, within the main function simplification loop | `sub_198DF00(-1)` | First opportunity: thread branches exposed by dead code elimination |
| ~278 | After NVVMPeephole2 and optionally GVN, in the NVIDIA-specific tier-2 sequence | `sub_198DF00(-1)` | Second opportunity: thread branches exposed by value numbering and peephole |
| Late tier-3 | Within the ADCE/MemCpyOpt/DSE sequence | `sub_198DF00(t)` | Final opportunity: catch any remaining threadable branches before StructurizeCFG |

The `sub_198DF00` function is the combined CorrelatedValuePropagation/JumpThreading registration wrapper. The `-1` parameter likely selects the default mode; the `t` parameter in the third position may be an optimization-level-dependent configuration.

All three positions are conditional on `NVVMPassOptions` offset `+320` not being set to disable. Each invocation resets the 512-instruction global budget, so the total code growth across all three invocations can reach up to 1,536 instructions per function.

## DFA JumpThreading

A separate DFA-based JumpThreading variant exists at `sub_276AF50`, registered as `"dfa-jump-threading"` (`llvm::DFAJumpThreadingPass`). This pass is controlled by:

| Knob | Registration | Description |
|------|-------------|-------------|
| `enable-dfa-jump-thread` | `ctor_445` @ `0x53F5C0` | Enable/disable the DFA variant |
| `dfa-jump-view-cfg-before` | `ctor_445` | Debug: dump CFG before DFA threading |
| `dfa-early-exit-heuristic` | `ctor_445` | Early-exit heuristic for compile time |

DFA JumpThreading handles state-machine patterns (switch statements in loops with predictable transitions between cases) that the standard JumpThreading cannot resolve. It is a separate pass with its own pipeline registration and does not share the budget or thresholds of the standard JumpThreading pass.

## Before/After IR Example

Consider a kernel with a two-branch diamond:

**Before JumpThreading:**

```llvm
entry:
  %cond1 = icmp sgt i32 %x, 0
  br i1 %cond1, label %positive, label %negative

positive:
  %a = call i32 @computeA()
  br label %merge

negative:
  %b = call i32 @computeB()
  br label %merge

merge:
  %val = phi i32 [ %a, %positive ], [ %b, %negative ]
  %cond2 = icmp eq i32 %val, 42
  br i1 %cond2, label %match, label %nomatch

match:
  ...
nomatch:
  ...
```

If LVI can prove that `computeA()` always returns 42 (e.g., it is a known constant), JumpThreading duplicates the `merge` block for the `%positive` predecessor:

**After JumpThreading:**

```llvm
entry:
  %cond1 = icmp sgt i32 %x, 0
  br i1 %cond1, label %positive, label %negative

positive:
  %a = call i32 @computeA()
  br label %match              ; threaded: skip %merge entirely

negative:
  %b = call i32 @computeB()
  br label %merge

merge:                          ; now has only one predecessor
  %val = phi i32 [ %b, %negative ]
  %cond2 = icmp eq i32 %val, 42
  br i1 %cond2, label %match, label %nomatch

match:
  ...
nomatch:
  ...
```

The `%positive` path no longer passes through `merge`. The second branch is eliminated for threads that took the first path.

## Differences from Upstream LLVM

| Aspect | CICC v13.0 | Upstream LLVM 20 |
|--------|-----------|------------------|
| PHI threshold default | **76** | Lower (typically ~32 or similar) |
| `disable-jump-threading` in SimplifyCFG | Present, annotated for OCG experiments | Present (standard LLVM flag) |
| Annotation | "Disable jump threading for OCG experiments" | No OCG reference |
| Pipeline invocations | **Three** positions, combined with CVP via `sub_198DF00` | Typically two (early and late in the function simplification pipeline) |
| `NVVMPassOptions` disable | Offset `+320` | N/A |
| Loop header override thresholds | `qword_501D628`, `qword_501D548` | Standard LoopInfo check only |
| `fold-with-var-cond` | NVIDIA-specific SimplifyCFG companion flag | Not present |

The core algorithm is unmodified from upstream. NVIDIA's changes are configuration-level: adjusted thresholds, additional pipeline positions, the OCG disable flag, and integration with the `NVVMPassOptions` system.

## Function Map

| Address | Size | Identity |
|---------|------|----------|
| `sub_2DC4260` | 12,932 bytes | `JumpThreadingPass::run` (main pass body) |
| `sub_2DC22F0` | 2,797 bytes | Block cloning engine (`duplicateBlock`) |
| `sub_2DC30A0` | 1,094 bytes | CFG finalization after threading |
| `sub_2DC37C0` | 2,288 bytes | Single-instruction threading |
| `sub_2DC40B0` | 420 bytes | `tryToUnfoldSelect` |
| `sub_2DC1F40` | 349 bytes | SmallVector append/copy for instruction map |
| `sub_11F3070` | -- | `LVI::getPredicateAt` |
| `sub_DFABC0` | -- | `evaluateConditionOnEdge` |
| `sub_988330` | -- | `getConstantOnEdge` |
| `sub_AC4810` | -- | `isImpliedCondition` |
| `sub_AA93C0` | -- | `SimplifyICmpInst` |
| `sub_981210` | -- | `getBranchCondition` |
| `sub_B43CB0` | -- | `BranchInst::getCondition` |
| `sub_B4C9A0` | -- | `BranchInst::Create` (conditional) |
| `sub_B4C8F0` | -- | `BranchInst::Create` (unconditional) |
| `sub_B99FD0` | -- | `PHINode::addIncoming` |
| `sub_D5C860` | -- | `PHINode::Create` |
| `sub_F36990` | -- | `SplitBlockAndInsertIfThen` |
| `sub_BD5C60` | -- | `BasicBlock::getContext` |
| `sub_22077B0` | -- | `operator new(0x50)` (allocate BasicBlock) |
| `sub_AA4D50` | -- | `BasicBlock::insertInto` |
| `sub_BD84D0` | -- | `Value::replaceAllUsesWith` |
| `sub_B43D60` | -- | `Instruction::eraseFromParent` |
| `sub_FFB3D0` | -- | `DominatorTree::changeImmediateDominator` |
| `sub_AD69F0` | -- | `PHINode::getIncomingValueForBlock` |
| `sub_C959E0` | -- | LoopInfo pass lookup |
| `sub_B532B0` | -- | Predicate implies branch check |
| `sub_B52EF0` | -- | `ConstantExpr::getICmp` or create threaded edge |
| `sub_92B530` | -- | `CloneBasicBlock` or wire new block |
| `sub_929DE0` | -- | `CloneBasicBlock` (alternate path) |

## Cross-References

- [StructurizeCFG](./structurizecfg.md) -- the late-pipeline safety net that catches irreducible CFG created by threading or other passes
- [Scalar Passes Hub](./scalar-passes.md) -- hub page linking SROA, EarlyCSE, and JumpThreading with GPU-context summaries
- [GVN](./gvn.md) -- runs between JumpThreading invocations in the tier-2 sequence; can expose new threadable branches
- [Pipeline & Ordering](./pipeline.md) -- tier-dependent scheduling of all three invocations
- [Knobs](../config/knobs.md) -- master knob inventory including all six JumpThreading knobs
