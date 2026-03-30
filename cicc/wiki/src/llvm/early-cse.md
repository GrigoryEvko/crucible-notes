# EarlyCSE (Early Common Subexpression Elimination)

EarlyCSE is a fast dominator-tree-walk pass that eliminates redundant computations, loads, and calls within a function. Cicc's version is **not** stock LLVM 20.0.0 -- the binary contains four CUDA-specific extensions that handle GPU memory model semantics: barrier-aware memory versioning with hardcoded NVVM intrinsic ID checks, shared memory address space 7 protection against unsafe store-to-load forwarding, a dedicated NVVM intrinsic call CSE handler with a fast-path for thread-invariant special register reads, and a PHI operand limit of 5 for compile-time control. It also adds a fourth scoped hash table (store-forwarding) that upstream lacks.

## Key Facts

| Field | Value |
|---|---|
| **Pass name** | `"early-cse"` (standard), `"early-cse-memssa"` (MemorySSA variant) |
| **Pipeline parser params** | `memssa` (selects MemorySSA-backed variant) |
| **Pass entry (standard)** | `sub_2778270` |
| **Pass entry (MemorySSA)** | `sub_27783D0` |
| **Core function** | `sub_2780B00` (12,350 bytes) |
| **NVVM call CSE handler** | `sub_2780450` (1,142 bytes, ~263 decompiled lines) |
| **Pipeline positions** | 245, 291 (tier 1); 525, 593 (tier 2+); ~370 (late) |
| **Disable flag** | `NVVMPassOptions` offset `+1440` |
| **Pipeline assembler** | `sub_18E4A00` (MemorySSA variant), `sub_196A2B0` (standard) |
| **Upstream LLVM file** | `llvm/lib/Transforms/Scalar/EarlyCSE.cpp` |
| **NVIDIA modifications** | Barrier generation tracking, AS 7 handling, NVVM call CSE, PHI limit, store-fwd table |

## Algorithm Overview

The pass performs a stack-driven iterative DFS over the dominator tree. At each basic block it scans instructions linearly, attempting three forms of elimination:

1. **Expression CSE** -- arithmetic, casts, comparisons, GEPs with identical operands are looked up in a scoped hash table. If a matching canonical instruction exists, the redundant one is replaced via RAUW and erased.

2. **Load CSE and store-to-load forwarding** -- loads from the same address and type as a prior load (or a prior store) are replaced with the already-available value. This is gated by a `CurrentGeneration` counter that invalidates stale entries whenever a memory-writing instruction or barrier intrinsic is encountered.

3. **Call CSE** -- readonly/readnone calls with identical targets and arguments are deduplicated. The NVVM-specific handler `sub_2780450` provides a fast-path for thread-invariant NVVM intrinsics (`llvm.nvvm.read.ptx.sreg.*`).

The dominator tree walk is **not recursive**. It uses an explicit growable stack (initial capacity 8 entries, 64 bytes) with `DomTreeScope` nodes that record per-scope hash table insertions. On scope exit all insertions are tombstoned. This matters for deeply-nested GPU kernel CFGs where stack overflow from recursion is a real risk.

```
function EarlyCSE(ctx):
    root = ctx.Function.DomTree.root
    stack.push(DomTreeScope(root))

    while stack is not empty:
        scope = stack.top()
        ctx.CurrentGeneration = scope.generation_begin

        if not scope.visited:
            for inst in scope.bb.instructions:
                processNode(ctx, inst)       // CSE logic below
            scope.visited = true
            scope.generation_end = ctx.CurrentGeneration
        else:
            if scope has unvisited children:
                child = scope.children.pop_front()
                stack.push(DomTreeScope(child))
                continue
            else:
                unwindScope(ctx, scope)      // tombstone entries, free node
                stack.pop()
```

## DomTreeScope Structure

Each scope node is 160 bytes (`0xA0`), allocated via `sub_22077B0`:

| Offset | Type | Field |
|---|---|---|
| `+0x00` | `u32` | `generation_begin` -- snapshot of `CurrentGeneration` at scope entry |
| `+0x04` | `u32` | `generation_end` -- value at scope exit (after processing all instructions) |
| `+0x08` | `BasicBlock*` | The basic block for this domtree node |
| `+0x10` | `DomTreeNode**` | `children_begin` |
| `+0x18` | `DomTreeNode**` | `children_end` |
| `+0x20` | scope link | Expression ScopedHT chain -> `ctx+0x78` |
| `+0x38` | scope link | Load ScopedHT chain -> `ctx+0x108` |
| `+0x50` | scope link | Call ScopedHT chain -> `ctx+0x198` |
| `+0x68` | scope link | Call-values ScopedHT chain -> `ctx+0x228` |
| `+0x80` | scope link | Store-fwd ScopedHT chain -> `ctx+0x250` |
| `+0x98` | `u8` | `visited` flag (0 = not yet processed, 1 = instructions scanned) |

Each chain entry is a triplet `[link_fwd, link_back, insertion_list_head]` occupying 24 bytes. On scope exit, the pass walks each insertion list and tombstones the corresponding hash table entries, then frees the scope node.

## Four Scoped Hash Tables

Upstream LLVM EarlyCSE has three scoped hash tables (expression, load, call). Cicc adds a fourth dedicated to store-to-load forwarding.

| Table | Context offset | Hash function | Equality | Key | Value |
|---|---|---|---|---|---|
| Expression | `+0xE8` / `+0xF8` | `sub_277F590` | `sub_277AC50` | Opcode + operand value-numbers | Canonical instruction pointer |
| Load | `+0x178` / `+0x188` | `sub_277CF80` | `sub_27792F0` | Load address + type | Previously loaded value |
| Call | `+0x230` / `+0x240` | `sub_277CF80` | `sub_27792F0` | Call target + arguments | Return value |
| Store-fwd | `+0x2C0` / `+0x2D0` | `sub_277C800` | `sub_27781D0` | Store address + type | Stored value |

All four use open-addressing with linear probing. Sentinel values: `0xFFFFFFFFFFFFF000` = empty, `0xFFFFFFFFFFFFE000` = tombstone. Resize triggers at 75% load factor (`4 * (count + 1) >= 3 * bucket_count`) or when tombstones exceed 12.5% of capacity. Bucket counts are always a power of two.

The store-forwarding table is the NVIDIA addition. Upstream EarlyCSE performs store-to-load forwarding through the load table by inserting the stored value when a store is processed. Cicc separates this into a dedicated table, which enables more aggressive dead-store detection within the early pipeline -- two stores to the same address with no intervening load or barrier can be recognized without polluting the load table's namespace.

## CUDA Extension 1: Barrier-Aware Memory Versioning

The context structure holds a `CurrentGeneration` counter at offset `+0x2E0` (type `u32`). This counter acts as a memory version number. Every load and call CSE lookup checks whether the cached entry's generation matches the current generation -- a mismatch means an intervening memory-modifying operation invalidated the entry.

Generation is incremented when:
- A trivially dead instruction is skipped (minor bump at `0x2781950`)
- `sub_B46490` (`hasMemoryWriteSideEffects`) returns true for a call instruction
- Any of four hardcoded NVVM barrier intrinsic IDs is encountered

The barrier intrinsic checks are explicit `cmp dword ptr [rax+24h], IMM` instructions at specific addresses in the binary:

| Address | Encoding | Intrinsic ID | Decimal | Identity |
|---|---|---|---|---|
| `0x2781B30` | `cmp ..., 9Bh` | `0x9B` | 155 | `llvm.nvvm.barrier0` (`__syncthreads`) |
| `0x27812AF` | `cmp ..., CDh` | `0xCD` | 205 | `llvm.nvvm.membar.*` (device/system memory barrier) |
| `0x2781F4D` | `cmp ..., 123h` | `0x123` | 291 | `llvm.nvvm.bar.sync` (named barrier sync) |
| `0x2781F40` | `cmp ..., 144h` | `0x144` | 324 | NVVM cluster barrier (SM 90+ cluster-scope fence) |

These checks are a **safety net on top of** the intrinsics' declared memory-effect attributes. Upstream LLVM relies solely on the memory-effect modeling to determine whether a call clobbers memory. Cicc adds the explicit ID checks because the barrier intrinsics' memory effects, as declared in the NVVM tablegen files, may not fully capture the GPU-specific semantics: a `bar.sync` does not just write memory from the perspective of one thread -- it makes writes from *other* threads visible. The LLVM memory model has no native concept of inter-thread visibility guarantees at the IR level, so the explicit ID checks are the correctness backstop.

When any of these four intrinsics appears between two memory operations, EarlyCSE refuses to forward the earlier value. This prevents optimizations like:

```llvm
;; INCORRECT optimization that barriers prevent:
%v1 = load i32, ptr addrspace(3) %p          ;; load from shared memory
call void @llvm.nvvm.barrier0()               ;; __syncthreads()
%v2 = load i32, ptr addrspace(3) %p          ;; CANNOT be replaced with %v1
;; Another thread may have written to %p between the barrier and this load
```

## CUDA Extension 2: Shared Memory Address Space 7 Handling

Stores targeting NVPTX address space 7 (the internal representation for `__shared__` memory) receive special treatment that prevents unsafe store-to-load forwarding.

At address `0x2781BB6`, the pass checks `byte [rdx+8] == 7` on the store's pointer operand type. When this matches, the store is routed through `sub_B49E20` (`isSharedMemoryStore`), which calls `sub_B43CB0` (`getCalledFunction`) and `sub_B2D610` (`hasIntrinsicID`) to confirm the target is a shared memory variable (string ID `0x31` = `"shared"`).

The motivation: shared memory is written by one thread and potentially read by a *different* thread after a barrier. Forwarding a stored value to a subsequent load in the same thread is only safe if no barrier intervenes -- but even then, a reimplementor must be careful because the CUDA memory model permits a thread to read its own store without a barrier, while other threads cannot. The shared-memory path in EarlyCSE conservatively disables forwarding for shared-memory stores to avoid the case where a load is CSE'd to the stored value, but the actual runtime value has been modified by another thread's post-barrier store to the same location.

```
processStore(ctx, store_inst):
    ptr_type = store_inst.pointer_operand.type
    if ptr_type.address_space == 7:                 // NVPTX shared memory
        if isSharedMemoryStore(store_inst):         // sub_B49E20
            ctx.CurrentGeneration++                 // invalidate load/call tables
            return                                  // do NOT insert into store-fwd table
    // Normal path: insert stored value into store-fwd table for later forwarding
    insertStoreForwarding(ctx, store_inst)
```

## CUDA Extension 3: NVVM Intrinsic Call CSE (`sub_2780450`)

The dedicated function `sub_2780450` (1,142 bytes, ~263 decompiled lines) handles CSE for calls to NVVM builtin intrinsics. It is entered when the main instruction loop detects a single-use-by-call pattern: the instruction's result has exactly one user, that user is a `CallInst` (opcode `0x1F`), and the operand index is 3.

The function provides a **fast-path** for thread-invariant special register reads. Many NVVM intrinsics return values that are constant for the lifetime of a kernel invocation from a given thread's perspective:

- `llvm.nvvm.read.ptx.sreg.tid.x/y/z` -- `threadIdx.x/y/z`
- `llvm.nvvm.read.ptx.sreg.ntid.x/y/z` -- `blockDim.x/y/z`
- `llvm.nvvm.read.ptx.sreg.ctaid.x/y/z` -- `blockIdx.x/y/z`
- `llvm.nvvm.read.ptx.sreg.nctaid.x/y/z` -- `gridDim.x/y/z`
- `llvm.nvvm.read.ptx.sreg.warpsize`
- `llvm.nvvm.read.ptx.sreg.laneid`

Upstream LLVM would model these as `readnone` and CSE them through the generic call table. The NVVM-specific handler recognizes these intrinsic IDs directly via `sub_987FE0` (`getIntrinsicID`), avoiding the overhead of the general readonly-call analysis. For a kernel that references `threadIdx.x` twenty times, the fast-path eliminates nineteen redundant intrinsic calls in a single pass.

The function also handles two additional NVVM intrinsic IDs:

| ID | Decimal | Identity | CSE behavior |
|---|---|---|---|
| `0xE4` | 228 | NVVM load intrinsic | CSE-able if same address and no intervening clobber |
| `0xE6` | 230 | NVVM store intrinsic | Blocks CSE (generation bump) |

The check at `0x2783890` tests for intrinsic ID 228 and at `0x27839BC` for intrinsic ID 230. The store intrinsic (230) triggers a generation bump, while the load intrinsic (228) is treated as a CSE candidate.

## CUDA Extension 4: PHI Operand Limit

At address `0x2781BED`, the pass checks:

```
if PHINode.getNumIncomingValues() > 5:
    skip CSE analysis for this PHI
```

This is a compile-time heuristic absent from upstream LLVM. GPU kernel code after loop unrolling and predication commonly produces PHI nodes with dozens of operands. Comparing all incoming values for CSE equivalence becomes quadratic in the operand count (each pair of values must be checked for dominance and equivalence), and the benefit for wide PHIs is marginal -- they rarely represent true common subexpressions.

The threshold of 5 is hardcoded with no `cl::opt` override.

## Instruction Classification

The inner processing loop at `0x2780EB5`--`0x2781110` classifies each instruction by its opcode byte at `[instr-0x18]`:

| Opcode | Hex | Instruction | EarlyCSE action |
|---|---|---|---|
| `0x55` | Store | `StoreInst` | Store-to-load forwarding path; shared memory check |
| `0x3D` | Call | `CallInst` | Call CSE or generation bump (if memory effects) |
| `0x3E` | Invoke | `InvokeInst` | Same as CallInst |
| `0x3F` | Select | `SelectInst` | Expression CSE with type-size check |
| `0x40` | PHI | `PHINode` | Expression CSE if operand count <= 5 |
| `<= 0x1C` | -- | Constants/args | Skip (not instructions) |
| `0x29` | Return | `ReturnInst` | Skip |
| `0x43`--`0x4F` | Casts | Cast instructions | Expression CSE |

The classification dispatches to these helper predicates:

| Helper | Address | Purpose |
|---|---|---|
| `sub_AA54C0` | `0x2780EC6` | `isTriviallyDead` -- if true, bump generation and skip |
| `sub_D222C0` | `0x2780F97` | `isSimpleExpression` -- arithmetic, casts, comparisons, GEPs |
| `sub_F50EE0` | `0x2780F7A` | `canCSE` / `doesNotAccessMemory` |
| `sub_1020E10` | `0x2781967` | `getCallCSEValue` -- readonly/readnone call check |
| `sub_B46420` | `0x2781B95` | `isLoadCSECandidate` |
| `sub_B46490` | `0x2781CC6` | `hasMemoryWriteSideEffects` -- triggers generation bump |

## Load-Store Forwarding Detailed Flow

The most complex code path (`0x2781B48`--`0x2781F32`) handles load CSE and store-to-load forwarding:

```
processLoad(ctx, load_inst):
    key = computeLoadCSEKey(load_inst, ctx.DataLayout)    // sub_2779A20
    if key.status != 0:
        // Cannot form clean key -- check if call/invoke returns equivalent value
        if load_inst is CallInst (0x3D) or InvokeInst (0x3E):
            tryCallValueForwarding(ctx, load_inst)
        return

    // Check for preceding store to same address
    store_entry = lookupStoreTable(ctx, key)
    if store_entry and store_entry.generation == ctx.CurrentGeneration:
        // Forward stored value to this load
        salvageDebugInfo(load_inst, store_entry.value)    // sub_BD84D0
        replaceAllUsesWith(load_inst, store_entry.value)  // sub_11C4E30
        eraseInstruction(load_inst)                       // sub_B43D60
        return CHANGED

    // Check for preceding load from same address
    load_entry = lookupLoadTable(ctx, key)
    if load_entry and load_entry.generation == ctx.CurrentGeneration:
        // Replace with previously loaded value
        replaceAllUsesWith(load_inst, load_entry.value)
        eraseInstruction(load_inst)
        return CHANGED

    // Not found -- insert into load table for future lookups
    insertLoadTable(ctx, key, load_inst, ctx.CurrentGeneration)
```

For stores, the pass also performs dead-store detection within the same scope: if two stores target the same address with no intervening load or barrier, the earlier store is dead. The barrier check uses the same four intrinsic ID comparisons described above.

## Type Compatibility and Bitwidth Handling

At `0x27829C3`--`0x2782B87`, for expression CSE of `SelectInst` and `PHINode`:

- `sub_AE43F0` computes type size in bits via the `DataLayout`
- If size <= 64 bits: use a `u64` bitmask as the CSE key
- If size > 64 bits: allocate a `BitVector` via `sub_C43690` and use bit-level comparison

At `0x2782F72`--`0x2782FD5`, integer constant range analysis computes leading zeros/ones to determine effective bit-width. If the value fits in fewer bits, EarlyCSE allows CSE across different integer types (e.g., `i32 zext i64` vs `i64`). This is an NVIDIA extension that upstream LLVM does not perform -- upstream requires exact type matches for expression CSE.

## Context Structure Layout

The `EarlyCSEContext` structure passed to `sub_2780B00` in `rdi`:

| Offset | Field | Size |
|---|---|---|
| `+0x00` | Current instruction pointer | 8 |
| `+0x08` | `DataLayout*` / `TargetData*` | 8 |
| `+0x10` | `Function*` (-> `[+0x60]` = DomTree root) | 8 |
| `+0x18` | `TargetLibraryInfo*` | 8 |
| `+0x20` | `AssumptionCache*` | 8 |
| `+0x68` | MemDep result tracking | 8 |
| `+0x70` | MemDep analysis reference | 8 |
| `+0xE8`--`+0x110` | Expression hash table (buckets, count, ScopedHT, free list, allocator) | 40 |
| `+0x170`--`+0x198` | Load hash table + ScopedHT | 40 |
| `+0x200`--`+0x258` | Call hash table + ScopedHT | 88 |
| `+0x2B8`--`+0x2D8` | Store-fwd hash table + ScopedHT | 32 |
| `+0x2E0` | `CurrentGeneration` (`u32`) | 4 |

Stack frame: `0x1D0` bytes (`sub rsp, 0x1A8` + 5 callee-saved pushes).

## Scope Page Management

The scoped hash tables use 512-byte (`0x200`) scope pages chained together. When a page fills:

1. At `0x2781328`: fetch previous page via `[stack.end - 8]`, advance by `0x200` to the next chained page.
2. At `0x2782260`: when reclaiming, free the current page and pop from the page pointer array.

The initial worklist stack is 64 bytes (8 entries of 8 bytes each). The scope-page-pointer array is 8-byte aligned via `lea rbx, [rdx*4 - 4]; and rbx, ~7; add rbx, rax`.

## `memssa` Pipeline Parameter

The pipeline parser registers `"early-cse"` at slot 394 with the parameter keyword `memssa`. When `memssa` is specified, the pass uses the MemorySSA-backed variant (`sub_27783D0`, pass name `"Early CSE w/ MemorySSA"`) instead of the standard variant (`sub_2778270`, pass name `"Early CSE"`). Both variants call the same core function `sub_2780B00`; the difference is that the MemorySSA variant receives a pre-built MemorySSA graph in the context structure and uses it for more precise clobber queries, avoiding the O(n^2) scanning that the non-MSSA path falls back to for load CSE.

## Knobs

| Knob | Default | Description |
|---|---|---|
| `enable-earlycse-memoryssa` | `true` | Master switch for MemorySSA integration |
| `earlycse-debug-hash` | `false` | Debug: log hash function inputs/outputs |
| `earlycse-mssa-optimization-cap` | `500` | Max MemorySSA queries per block before falling back to conservative |
| `enable-earlycse-imprecision` | `false` | Allow approximate analysis in pathological cases (huge blocks, deep PHI nests) |

No dedicated `cl::opt` flags exist for any of the four NVIDIA extensions. The PHI operand limit of 5, the four barrier intrinsic IDs, and the shared-memory address space 7 check are all hardcoded in the binary.

## Pipeline Positions and Tier Gating

| Tier | Position(s) | Notes |
|---|---|---|
| **Tier 1 (O1)** | Skipped | `sub_12DE8F0` explicitly gates EarlyCSE with `tier != 1` |
| **Tier 2 (O2)** | 525, 593 | Two invocations: early function simplification and post-loop-optimization |
| **Tier 3 (O3)** | 245, 291, ~370 | Three invocations; additional late-pipeline run |
| **Ofcmid** | After Sinking2 | Single invocation in the moderate-optimization path |

The pass is independently disableable via `NVVMPassOptions` at offset `+1440`. The same offset gates the standard and MemorySSA variants identically.

## Key Constants

| Value | Hex | Meaning |
|---|---|---|
| 160 | `0xA0` | `DomTreeScope` node size |
| 512 | `0x200` | Scope page size |
| 64 | `0x40` | Initial stack capacity (8 entries) |
| 48 | `0x30` | Hash table entry node size |
| 40 | `0x28` | Insertion record size |
| `0xFFFFFFFFFFFFF000` | -- | Hash table EMPTY sentinel |
| `0xFFFFFFFFFFFFE000` | -- | Hash table TOMBSTONE sentinel |
| 155 | `0x9B` | `llvm.nvvm.barrier0` intrinsic ID |
| 205 | `0xCD` | `llvm.nvvm.membar.*` intrinsic ID |
| 291 | `0x123` | NVVM `bar.sync` intrinsic ID |
| 324 | `0x144` | NVVM cluster barrier intrinsic ID |
| 228 | `0xE4` | NVVM load intrinsic ID |
| 230 | `0xE6` | NVVM store intrinsic ID |
| 5 | -- | PHI operand limit for CSE |

## Differences from Upstream LLVM 20.0.0

| Feature | Upstream | Cicc |
|---|---|---|
| Scoped hash tables | 3 (expression, load, call) | 4 (+ store-forwarding) |
| Barrier intrinsic checks | Relies on memory-effect attributes only | Explicit ID checks for IDs 155, 205, 291, 324 |
| Shared memory handling | No address-space-specific logic | AS 7 stores skip store-fwd insertion, bump generation |
| NVVM intrinsic call CSE | Generic readonly-call path | Dedicated `sub_2780450` with fast-path for `sreg.*` reads |
| PHI operand limit | None | Skip CSE for PHI nodes with >5 incoming values |
| Cross-type expression CSE | Exact type match required | Allows CSE across integer widths when value range fits |
| Dominator tree walk | Recursive in many LLVM builds | Always iterative (explicit stack) |

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_2778270` | -- | `EarlyCSEPass::run` (standard variant entry) |
| `sub_27783D0` | -- | `EarlyCSEPass::run` (MemorySSA variant entry) |
| `sub_2780B00` | 12,350 | Core pass body (domtree walk + instruction processing) |
| `sub_2780450` | 1,142 | `handleNVVMCallCSE` (NVVM intrinsic call CSE) |
| `sub_277F590` | -- | Expression hash function |
| `sub_277AC50` | -- | Expression equality check |
| `sub_277CF80` | -- | Load/call key hash |
| `sub_27792F0` | -- | Load/call key equality |
| `sub_277C800` | -- | Store key hash |
| `sub_27781D0` | -- | Store key equality |
| `sub_D222C0` | -- | `isSimpleExpression` |
| `sub_F50EE0` | -- | `canCSE` / `doesNotAccessMemory` |
| `sub_B49E20` | -- | `isSharedMemoryStore` (AS 7 check) |
| `sub_B49E00` | -- | `isSharedMemoryAccess` |
| `sub_1020E10` | -- | `getCallCSEValue` (readonly/readnone check) |
| `sub_B46420` | -- | `isLoadCSECandidate` |
| `sub_B46490` | -- | `hasMemoryWriteSideEffects` |
| `sub_B46500` | -- | `computeCSEHash` / `isVolatile` |
| `sub_987FE0` | -- | `getIntrinsicID` (NVVM intrinsic ID from call) |
| `sub_AA54C0` | -- | `isTriviallyDead` |
| `sub_11C4E30` | -- | `replaceAllUsesWith` (RAUW) |
| `sub_BD84D0` | -- | `salvageDebugInfo` |
| `sub_B43D60` | -- | `eraseInstruction` |
| `sub_27793B0` | -- | `removeFromParent` |
| `sub_2779A20` | -- | `computeLoadCSEKey` |
| `sub_27808D0` | -- | `insertStoreForwarding` |
| `sub_27801B0` | -- | `insertExprIntoScopedHT` |
| `sub_277D510` | -- | `lookupScope` (find value by generation) |
| `sub_277D3C0` | -- | `lookupCallTable` |
| `sub_2778110` | -- | `lookupInScopedHT` |
| `sub_27785B0` | -- | `shouldInsertIntoTable` |
| `sub_277C980` | -- | `growTable` (double hash table size) |
| `sub_277C8A0` | -- | `insertIntoTable` (post-grow insert) |
| `sub_277FFC0` | -- | `cleanupLoadTable` (compact after scope exit) |
| `sub_277A110` | -- | `cleanupCallTable` (compact after scope exit) |
| `sub_277A9A0` | -- | `compareLoadTypes` (type compatibility) |
| `sub_AE43F0` | -- | `TargetData::getTypeSizeInBits` |
| `sub_B43CB0` | -- | `getCalledFunction` |
| `sub_B2D610` | -- | `hasIntrinsicID` |

## Cross-References

- [Scalar Passes Hub](./scalar-passes.md) -- hub page linking SROA, EarlyCSE, and JumpThreading with GPU-context summaries
- [MemorySSA Builder for GPU](../infra/memoryssa.md) -- the MemorySSA infrastructure consumed by the `early-cse-memssa` variant
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- the universal DenseMap mechanics shared by all four hash tables
- [Barriers & Sync](../builtins/barriers.md) -- the barrier builtins whose intrinsic IDs trigger generation bumps
- [Dead Synchronization Elimination](../passes/dead-sync-elimination.md) -- the 96KB pass that removes dead barriers; interacts with EarlyCSE's barrier-aware generation tracking
- [GVN](./gvn.md) -- the more expensive redundancy elimination pass that complements EarlyCSE later in the pipeline
- [DSE](./dse.md) -- Dead Store Elimination, which complements EarlyCSE's within-scope store-to-load forwarding with cross-block analysis
- [Pipeline & Ordering](./pipeline.md) -- tier-dependent scheduling and `NVVMPassOptions` gating
- [Alias Analysis & NVVM AA](../infra/alias-analysis.md) -- address-space-aware alias analysis that feeds into MemorySSA clobber queries
