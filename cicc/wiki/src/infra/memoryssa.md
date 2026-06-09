# MemorySSA Builder for GPU

MemorySSA constructs a sparse SSA form over memory operations, giving every instruction that reads or writes memory a position in a use-def chain that tracks the flow of memory state through a function. In upstream LLVM, MemorySSA already delivers significant speedups over the older `MemoryDependenceResults` analysis by avoiding per-query linear scans. In cicc v13.0, the payoff is amplified because the underlying alias analysis pipeline includes NVVM AA, which returns `NoAlias` for any cross-address-space pointer pair. A store to shared memory (`addrspace(3)`) and a load from global memory (`addrspace(1)`) will never produce a dependency edge in the MemorySSA graph, yielding a dramatically sparser representation than would be possible on a flat-memory architecture. Every pass that consumes MemorySSA — LICM, EarlyCSE, DSE, GVN, SimpleLoopUnswitch — benefits from this precision without containing any GPU-specific logic itself.


## Key Facts

| Property | Value |
|---|---|
| Builder entry wrapper | `sub_1A6CAD0` (48 bytes — skipFunction guard + tail call) |
| Builder core function | `sub_1A6A260` (10,344 bytes) |
| MemoryAccess allocator | `sub_1A69110` (1,245 bytes) |
| Pass registration string | `"memoryssa"` (analysis #179 in pipeline parser) |
| Pipeline parser entry | `"print<memoryssa>"` -> `MemorySSAPrinterPass` |
| Required analyses | AliasAnalysis (tag `unk_4F9D3C0`), DominatorTree (tag `unk_4F9E06C`), LoopInfo (tag `unk_4F9A488`) |
| Stack frame size | 0x3F8 = 1,016 bytes |
| MemoryAccess node size | 0x40 = 64 bytes (bump-allocated) |
| Walker check limit | `memssa-check-limit` = 100 (max stores/phis to walk past) |
| Verification flag | `verify-memoryssa` (off by default, on under `EXPENSIVE_CHECKS`) |
| DOT graph output | `dot-cfg-mssa` (filename for CFG + MemorySSA visualization) |


## MemorySSA Node Types

MemorySSA represents memory state with three node types, all stored in 64-byte heap-allocated objects:

**MemoryDef** (kind=2) — Created for every instruction that may write memory: stores, calls with side effects, atomics, `memcpy`/`memmove` intrinsics. Each MemoryDef takes the previous memory state as its operand and produces a new version of memory state.

**MemoryUse** (kind=1) — Created for every instruction that reads memory but does not modify it: loads, calls to readonly/readnone functions. A MemoryUse points to the MemoryDef (or MemoryPhi) that represents the most recent memory state it depends on.

**MemoryPhi** (kind=3) — Inserted at control flow join points where predecessors have different reaching memory definitions, exactly like an SSA phi node for scalar values. A MemoryPhi merges the memory states from each predecessor into a single version.

All three types share a common layout:

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | vtable / next pointer (intrusive list) |
| +0x08 | 8 | prev pointer (intrusive list) |
| +0x10 | 4 | kind (1=MemoryUse, 2=MemoryDef, 3=MemoryPhi) |
| +0x14 | 4 | operand_count (bits 0-27) | flags (bits 28-31) |
| +0x17 | 1 | flags byte (bit 6 = 0x40 = "has inline operands") |
| +0x18 | 8 | defining instruction / accessed Value* |
| +0x20 | 8 | type/size descriptor (APInt or pointer to APInt) |
| +0x28 | 8 | operand/predecessor pointer |
| +0x30 | 8 | current reaching definition (MemoryAccess*) |
| +0x38 | 8 | associated BasicBlock* (or null) |

The sentinel value `1` stored in the reaching-definition field (`+0x30`) represents `LiveOnEntry` — the implicit MemoryDef that dominates the entire function and represents the initial state of memory at function entry.


## Construction Algorithm

The builder at `sub_1A6A260` follows the standard LLVM MemorySSA construction algorithm, implemented as a dominator-tree DFS rename pass. The implementation is split into eight phases.

### Phase 1 — Prerequisite Retrieval (0x1A6A260 - 0x1A6A3A0)

The builder queries the analysis manager for three required results via a vtable-tagged vector. Each registered analysis is identified by a unique tag pointer:

1. `unk_4F9D3C0` -> calls virtual method `[rax+0x68]` -> `sub_14A4050` — retrieves `AAResults`, stored at `[this+0xB8]`
2. `unk_4F9E06C` -> retrieves DominatorTree result, stored at `[this+0xA8]` (offset +0xA0 within the wrapper)
3. `unk_4F9A488` -> retrieves LoopInfo, stored at `[this+0xB0]`

If any tag is not found in the registered analysis vector, control jumps to terminal handlers at 0x1A6CAAF-0x1A6CABE (assertion / unreachable).

### Phase 2 — Worklist Initialization (0x1A6A3A0 - 0x1A6A6B0)

The builder allocates a 1,016-byte stack frame and initializes four layers of SmallVector-based renaming stacks:

- **Level 0**: DFS traversal order over the dominator tree (computed by `sub_13B8390`)
- **Level 1**: Per-block instruction iterator
- **Level 2**: Per-block incoming MemoryPhi operand buffer (SmallVector at `rbp-0x330`, inline capacity 8)
- **Level 3**: Memory state stack (current reaching definition per DFS depth)

Each layer is initialized by `sub_16CCEE0` (SmallVector move-assign). Temporary intermediate buffers are freed before the main walk begins.

### Phase 3 — Dominator Tree Walk (0x1A6A88C - 0x1A6B070)

The main loop visits each basic block in DFS order over the dominator tree. For every instruction, the builder reads the opcode byte at `[instruction-8]` and classifies it:

```c
opcode_tag = *(uint8_t*)(instr - 8);

switch (opcode_tag) {
    case 0x18..0x38:    // Memory instructions (load/store range)
        type_tag = *(uint8_t*)(*(instr - 0x18) + 8);
        if (type_tag == 0x10)  // PointerType result -> this is a Load
            createMemoryUse(instr);
        else
            createMemoryDef(instr);  // Store
        break;

    case 0x0B:          // CallInst
        classifyCall(instr);   // -> sub_1A69C30
        break;

    case 0x27:          // PHINode
        if (predecessors_disagree_on_memory_state())
            createMemoryPhi(block);
        break;
}
```

**Type-size computation.** For each memory access, a three-level nested switch computes the byte-size of the accessed region. The switch handles all LLVM Type IDs:

| Type ID | Type | Size computation |
|---------|------|-----------------|
| 1 | HalfTy | 16 bits |
| 2 | FloatTy | 32 bits |
| 3 | DoubleTy | 64 bits |
| 4 | FP80 | 80 bits |
| 5 | FP128 | 128 bits |
| 6 | PPC_FP128 | 128 bits |
| 7 | PointerTy | `getPointerSizeInBits()` via `sub_15A9520` |
| 11 | IntegerTy | `[type+8] >> 8` (raw bit width) |
| 14 | StructTy | `getStructLayout()` via `sub_15A9FE0` |
| 0, 8, 10, 12, 16 | Array/Vector | element_count * element_size |

When the computed access size differs from the store size (`[rax+8] >> 8`), the builder routes through `sub_1A69690` to create a partial-store MemoryDef, capturing the precise overlap region.

### Phase 4 — Call and Intrinsic Classification

Call instructions (opcode 0x0B) are dispatched through `sub_1A69C30` (call-instruction MemoryDef handler), which classifies intrinsics by ID:

- **ID 0x0F** (lifetime.start) and **ID 0x17** (lifetime.end) — no memory effect, skipped
- **ID 0x27** — `memcpy`/`memmove`-like intrinsics, create MemoryDef
- **ID 0x2F** — atomic intrinsics (checks `[rdx-0x30]` for ordering)
- **ID 0x33** — NVIDIA-specific intrinsics (surface/texture operations, NVVM builtins)

### Phase 5 — MemoryAccess Allocation (sub_1A69110)

The core allocator creates all three node types. Parameters:

| Register | Meaning |
|----------|---------|
| rdi | MemorySSA `this` |
| esi | kind: 1=MemoryUse, 2=MemoryDef, 3=MemoryPhi |
| rdx | defining value / access value |
| rcx | type descriptor (APInt holding access size) |
| r8 | instruction pointer |
| r9 | predecessor block (for MemoryPhi) |

Each allocation calls `sub_22077B0` (BumpPtrAllocator::Allocate) for 0x40 bytes, populates all fields, inserts the node into the intrusive list via `sub_2208C80`, and increments the node counter at `[this+0xD0]`.

For kind==1, `sub_16A57B0` (countLeadingZeros) determines whether the access is a full or partial def. For kind==3 (MemoryPhi), the operand list is populated by iterating predecessor blocks through `sub_146F1B0` (AA-driven reaching-definition lookup).

### Phase 6 — Trivial Phi Optimization (0x1A6B280 - 0x1A6B9BD)

After the DFS walk, the builder post-processes all MemoryPhi nodes. Any MemoryPhi whose operands all resolve to the same MemoryDef is trivial — it can be replaced with that single reaching definition. The loop at 0x1A6B9DE iterates the result vector `[this+0xD8..this+0xE0]`:

```cpp
for (auto *Phi : result_vector) {
    unsigned count = Phi->operand_count & 0x0FFFFFFF;
    if (all_operands_identical(Phi)) {
        Phi->replaceAllUsesWith(single_reaching_def);  // sub_164B780
        Phi->eraseFromParent();                         // sub_1AEB370
        destroy(Phi);                                   // sub_164BEC0
    }
}
```

This cleanup is critical for GPU code. Because NVVM AA proves so many memory operations are independent, many join points that would require MemoryPhis on a flat-memory machine will have all predecessors carrying the same memory state. The trivial-phi elimination pass removes these, reducing the graph to only the essential dependencies.


## GPU-Specific Precision Gains

The MemorySSA builder itself contains no explicit GPU logic. The GPU awareness comes entirely through the AA pipeline at `[this+0xB8]`, which chains BasicAA -> TBAA -> ScopedNoAliasAA -> NVVM AA. The critical interaction points are:

**Cross-address-space independence.** When `sub_146F1B0` queries the AA for a `(store to addrspace(3), load from addrspace(1))` pair, NVVM AA returns `NoAlias` before BasicAA or TBAA are even consulted. The MemorySSA builder then skips creating a dependency edge. This means a MemoryUse for a global load will not depend on a MemoryDef for a shared store — they exist in parallel chains.

**Partial-alias precision.** The builder at 0x1A6AFB3 creates MemoryDefs even for partial overlaps, then calls `sub_1A69690` to register the precise overlap region. Standard LLVM would conservatively treat partial alias as MayAlias and create a full dependency. cicc's more aggressive approach uses the partial overlap information downstream for finer-grained DSE and LICM decisions.

**Address-space check on volatile access.** The call to `sub_15FA300` at 0x1A6B88E performs what appears to be a volatile-access or address-space check specific to CUDA memory spaces. This gate prevents the builder from creating false dependencies between volatile shared memory operations (used for inter-warp communication) and non-volatile global operations.

**NVIDIA custom intrinsic handling.** Type ID 0x33 in `sub_1A69990` is not a standard LLVM type ID. It appears to be cicc's custom type for CUDA-specific memory operations (surface/texture references, NVVM-specific typed pointers). These are classified as memory-clobbering conservatively unless the AA can prove otherwise.

**Practical effect.** Consider a kernel that loads from global memory, operates on shared memory, and stores back to global memory:

```cuda
__global__ void kernel(float *out, float *in) {
    __shared__ float smem[256];
    smem[threadIdx.x] = in[threadIdx.x];        // global load + shared store
    __syncthreads();
    float val = smem[threadIdx.x] * 2.0f;       // shared load
    out[threadIdx.x] = val;                      // global store
}
```

On a flat-memory machine, the MemorySSA graph would have a single linear chain: every memory operation depends on the previous one. With NVVM AA feeding MemorySSA, the graph splits into two parallel chains — one for shared memory and one for global memory — connected only at the `__syncthreads()` barrier (which is modeled as a MemoryDef that clobbers all address spaces).


## The MemorySSA Walker

Passes do not directly traverse the MemorySSA def-use chains. Instead, they query the **CachingWalker**, which answers the fundamental question: "What is the nearest MemoryDef that actually clobbers this memory location?"

The walker performs an **optimized upward walk** along the def chain, testing each MemoryDef against the query location using the full AA pipeline. The walk terminates when:

1. A MemoryDef that clobbers the query location is found (`instructionClobbersQuery` returns true)
2. `LiveOnEntry` is reached (the location was never written in this function)
3. The walk budget (`memssa-check-limit` = 100 steps) is exhausted, in which case the current MemoryDef is returned conservatively as a clobber

When a MemoryPhi is encountered, the walker splits into multiple paths (one per predecessor) and tracks them using a `DefPath` worklist. Each path records a `(MemoryLocation, First, Last, Previous)` tuple, enabling the walker to reconstruct the full path from any clobber back to the query origin.

**Caching.** The CachingWalker memoizes results per `(MemoryAccess, MemoryLocation)` pair. Once a clobber query is resolved, subsequent queries for the same access return the cached result immediately. The **SkipSelfWalker** variant (used by DSE) additionally skips the MemoryDef that is the query origin itself, answering "what did this store overwrite?" rather than "what clobbers this store?"

On GPU, the walker's budget is rarely exhausted for shared-memory operations because NVVM AA prunes so many false dependencies that the def chain is short. For global memory operations in loops with many stores, the 100-step limit can be hit; increasing `memssa-check-limit` trades compilation time for precision in these cases.


## Consumer Passes

Five major passes consume MemorySSA in cicc:

| Pass | How it uses MemorySSA |
|------|----------------------|
| **LICM** | Queries the walker to determine whether a load inside a loop is clobbered by any store in the loop body. If no clobber is found, the load is hoisted. NVVM AA makes shared-memory loads trivially hoistable past global stores. |
| **EarlyCSE** (`early-cse-memssa` variant, `sub_27783D0`) | Uses MemorySSA to find redundant loads — two loads from the same location with no intervening clobber are CSE'd. The MemorySSA variant avoids the O(n^2) scanning of the non-MSSA EarlyCSE. |
| **DSE** | Walks the MemorySSA graph backwards from a store to find earlier stores to the same location with no intervening loads. Dead stores are eliminated. DSE has its own extensive set of MemorySSA walk limits (see knobs below). |
| **GVN** | Can optionally use MemorySSA instead of MemoryDependenceResults (controlled by `enable-gvn-memoryssa`). When enabled, GVN uses the walker for load-value forwarding and PRE. |
| **SimpleLoopUnswitch** | Queries MemorySSA to determine whether a condition inside a loop depends on memory modified in the loop. The `simple-loop-unswitch-memoryssa-threshold` knob controls the walk limit. |


## Knobs and Thresholds

### MemorySSA Core

| Knob | Default | Effect |
|------|---------|--------|
| `memssa-check-limit` | 100 | Maximum stores/phis the walker will walk past before giving up. Higher values improve precision at the cost of compilation time. |
| `verify-memoryssa` | false | Enables expensive verification of MemorySSA invariants after every modification. |
| `dot-cfg-mssa` | `""` | If set, dumps the CFG annotated with MemorySSA information to the named DOT file. |

### DSE MemorySSA Walk Limits

| Knob | Default | Effect |
|------|---------|--------|
| `dse-memoryssa` | true | Master switch enabling MemorySSA-based DSE. |
| `dse-memoryssa-scanlimit` | 150 | Max memory accesses DSE will scan for a redundant store. |
| `dse-memoryssa-walklimit` | 90 | Max MemorySSA walk steps per DSE query. |
| `dse-memoryssa-partial-store-limit` | 5 | Max partial stores DSE will try to merge. |
| `dse-memoryssa-defs-per-block-limit` | 5000 | Skip blocks with more defs than this limit. |
| `dse-memoryssa-samebb-cost` | 1 | Walk cost weight for same-block MemoryDefs. |
| `dse-memoryssa-otherbb-cost` | 5 | Walk cost weight for cross-block MemoryDefs. |
| `dse-memoryssa-path-check-limit` | 50 | Max paths DSE will check for nontrivial reachability. |
| `dse-optimize-memoryssa` | true | Enables DSE's own MemorySSA optimization (trivial phi removal during DSE). |

### GVN / MemoryDependence

| Knob | Default | Effect |
|------|---------|--------|
| `enable-gvn-memoryssa` | varies | Switches GVN from MemDep to MemorySSA. |
| `memdep-block-scan-limit` | 100 (legacy) | Legacy MemDep per-block scan limit. |
| `memdep-block-number-limit` | 200 (legacy) / 1000 (NewPM) | Max blocks MemDep will search. Note: the NewPM variant defaults to 1,000, a 5x increase. |


## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Pass entry wrapper (skipFunction guard + tail call to builder) | `sub_1A6CAD0` | 48 | — |
| MemorySSA builder core (DFS rename walk) | `sub_1A6A260` | 10,344 | — |
| MemoryAccess node allocator (Def/Use/Phi) | `sub_1A69110` | 1,245 | — |
| MemoryDef creation dispatcher (routes to `sub_1A69110`) | `sub_1A695F0` | — | — |
| Store-instruction MemoryDef handler (partial store support) | `sub_1A69690` | 754 | — |
| MemoryPhi operand insertion handler (bidirectional edge setup) | `sub_1A69990` | 664 | — |
| Call-instruction handler (intrinsic classification) | `sub_1A69C30` | — | — |
| `MemorySSA::getMemoryAccess` or walker lookup | `sub_1643330` | — | — |
| `MemoryAccess::getDefiningAccess` | `sub_1643D30` | — | — |
| `MemoryLocation::get` or `getForDest` | `sub_1644900` | — | — |
| `Value::replaceAllUsesWith` (def substitution during trivial phi removal) | `sub_164B780` | — | — |
| `MemoryAccess::~MemoryAccess` (destructor) | `sub_164BEC0` | — | — |
| `MemoryAccess::eraseFromParent` | `sub_1AEB370` | — | — |
| `BumpPtrAllocator::Allocate` (64-byte node allocation) | `sub_22077B0` | — | — |
| AA query: getModRefInfo / reaching-def resolution | `sub_146F1B0` | — | — |
| AA query: may-alias check (two-pointer comparison) | `sub_145CF80` | — | — |
| AA query: isNoAlias / clobber check | `sub_1487400` | — | — |
| DominatorTree DFS order computation | `sub_13B8390` | — | — |
| skipFunction guard (checks `isDeclaration`) | `sub_1636880` | — | — |


## Diagnostic Strings

Diagnostic strings recovered from `p2-J04-memoryssa.txt` and the pipeline parser (`p2c.1-01-pipeline-parser.txt`). MemorySSA itself emits no optimization remarks; its diagnostics are configuration knobs and the verification/dump infrastructure.

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"memoryssa"` | Pipeline parser analysis #179 | Registration | Analysis registration name in the pass pipeline |
| `"print<memoryssa>"` | Pipeline parser #406 | Registration | Printer pass registration; params: `no-ensure-optimized-uses` |
| `"memssa-check-limit"` | Knob (default 100) | Knob | Maximum stores/phis the CachingWalker will walk past before returning a conservative clobber |
| `"verify-memoryssa"` | Knob (default false) | Knob | Enables expensive verification of MemorySSA invariants after every modification; on under `EXPENSIVE_CHECKS` |
| `"dot-cfg-mssa"` | Knob (default `""`) | Knob | If set, dumps the CFG annotated with MemorySSA information to the named DOT file for visualization |
| `"dse-memoryssa"` | Knob (default true) | Knob | Master switch enabling MemorySSA-based DSE |
| `"dse-memoryssa-scanlimit"` | Knob (default 150) | Knob | Max memory accesses DSE will scan for a redundant store |
| `"dse-memoryssa-walklimit"` | Knob (default 90) | Knob | Max MemorySSA walk steps per DSE query |
| `"dse-memoryssa-partial-store-limit"` | Knob (default 5) | Knob | Max partial stores DSE will try to merge |
| `"dse-memoryssa-defs-per-block-limit"` | Knob (default 5000) | Knob | Skip blocks with more defs than this limit |
| `"dse-memoryssa-samebb-cost"` | Knob (default 1) | Knob | Walk cost weight for same-block MemoryDefs |
| `"dse-memoryssa-otherbb-cost"` | Knob (default 5) | Knob | Walk cost weight for cross-block MemoryDefs |
| `"dse-memoryssa-path-check-limit"` | Knob (default 50) | Knob | Max paths DSE will check for nontrivial reachability |
| `"dse-optimize-memoryssa"` | Knob (default true) | Knob | Enables DSE's own MemorySSA optimization (trivial phi removal during DSE) |
| `"enable-gvn-memoryssa"` | Knob (varies) | Knob | Switches GVN from MemDep to MemorySSA |
| `"memdep-block-scan-limit"` | Knob (default 100 legacy) | Knob | Legacy MemDep per-block scan limit |
| `"memdep-block-number-limit"` | Knob (default 200 legacy / 1000 NewPM) | Knob | Max blocks MemDep will search; NewPM variant defaults to 1,000 (5x increase) |
| `"print<memoryssa-walker>"` | Pipeline parser | Registration | MemorySSA walker printer pass |
| `"early-cse-memssa"` | Pipeline parser | Registration | EarlyCSE variant that uses MemorySSA |

## Cross-References

- [Alias Analysis & NVVM AA](./alias-analysis.md) — the AA pipeline that feeds MemorySSA with GPU-aware NoAlias results
- [LICM](../llvm/licm-real.md) — primary consumer; NVVM AA-enhanced MemorySSA enables aggressive hoisting of shared-memory loads past global stores
- [DSE](../llvm/dse.md) — walks MemorySSA backwards to find dead stores; extensive set of MemorySSA-specific knobs
- [GVN](../llvm/gvn.md) — optional MemorySSA backend via `enable-gvn-memoryssa`
- [EarlyCSE](../llvm/early-cse.md) — EarlyCSE's `memssa` variant uses MemorySSA for redundant load elimination
