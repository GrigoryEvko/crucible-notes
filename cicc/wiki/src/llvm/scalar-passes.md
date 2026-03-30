# Scalar Passes: SROA, EarlyCSE & JumpThreading

Three LLVM scalar optimization passes play outsized roles in cicc's GPU pipeline. SROA (Scalar Replacement of Aggregates) eliminates `alloca` instructions by promoting them to SSA registers -- on a GPU this is existential, because every surviving alloca becomes a spill to local memory (address space 5), which is DRAM-backed per-thread storage with 200+ cycle latency. EarlyCSE performs dominator-tree-scoped common subexpression elimination with CUDA-specific extensions for barrier semantics, shared memory address spaces, and NVVM intrinsic recognition. JumpThreading duplicates basic blocks to resolve statically-determinable branches, but operates under strict constraints because PTX requires reducible control flow and block duplication directly increases register pressure.

All three passes are stock LLVM implementations with NVIDIA configuration overrides and, in the case of EarlyCSE, binary-level modifications. SROA and EarlyCSE each appear twice in the pipeline (once in the early function simplification sequence, once after loop optimization); JumpThreading appears three times at different tier levels. Each invocation can be independently disabled via `NVVMPassOptions` flags.

## SROA (Scalar Replacement of Aggregates)

| Property | Value |
|----------|-------|
| Pass name | `"sroa"` |
| Pipeline parser params | `preserve-cfg`, `modify-cfg` |
| Entry function | `sub_2935C30` (`runOnAlloca`) |
| Core function | `sub_2930B90` (`splitAlloca`) |
| Binary size | ~138 KB (80 KB + 58 KB) |
| Pipeline positions | Position 29 and 594 in tier-2/3 ordering |
| Disable flag | `NVVMPassOptions` offset `+1400` |
| Size threshold knob | `qword_50056C8` (max alloca size in bits) |
| Two-pass flag | `qword_50055E8` (enables pre-analysis for new PM) |
| NVIDIA modifications | None to core algorithm; custom threshold and pipeline integration |

### Why SROA is Critical on GPU

On a CPU, an `alloca` that the register allocator cannot promote simply lives on the stack -- a cached, low-latency memory region. On an NVIDIA GPU there is no hardware stack cache. Allocas that survive into code generation are lowered to `.local` memory (NVPTX address space 5), which is physically located in device DRAM and accessed through the L1/L2 cache hierarchy. Measured latencies are 200-400 cycles for an L1 miss, compared to zero cycles for a register read. A single un-promoted alloca in a hot loop can degrade kernel performance by 10-50x.

SROA runs early in the pipeline (position 29, immediately after InstCombine) to eliminate as many allocas as possible before any other transform sees them. It runs again late (position 594, after loop unrolling) to catch allocas created or exposed by loop transforms. The pass uses the `isAllocaPromotable` fast-path (`sub_B4CE70`): if the alloca is trivially promotable (all uses are simple loads and stores with no address-taken escapes), SROA marks it for `mem2reg` and returns without performing any splitting. This avoids unnecessary work on the common case of simple local variables.

### Algorithm Phases

The `runOnAlloca` entry (`sub_2935C30`) validates each alloca candidate against the type filter and size threshold. Accepted IR types are integers, floats, pointers, vectors, arrays, and structs; function types and labels are rejected. The size threshold at `qword_50056C8` gates the maximum alloca size in bits -- allocas larger than this value are left untouched for the backend to lower to `.local` memory.

The core algorithm in `splitAlloca` (`sub_2930B90`) operates in phases:

```
runOnAlloca(state, alloca):
    if alloca has no users: eraseFromParent; return
    if isAllocaPromotable(alloca): defer to mem2reg; return
    if typeSize(alloca) > qword_50056C8: return  // too large

    slices = buildSlices(alloca)           // sub_2927160
    partitions = buildPartitions(slices)   // sub_2924690

    // Merge contiguous slices sharing the same base type
    // Skip volatile operations (isVolatile check via sub_B46500)
    mergeContiguousSlices(slices)

    splitAlloca(state, alloca, partitions)
    // Recursively process newly created sub-allocas
```

The `splitAlloca` function proceeds through the partition list. For each partition `[start, end)`:

1. **Find rewrite target.** Search for an existing SSA value covering the byte range (`sub_291A860`). If none exists, create a new sub-alloca with type derived from the partition's dominant load/store type.

2. **Size/alignment check.** Verify partition size is under 8 MB (`0x800000` sanity limit). Compute alignment via `_BitScanReverse64` (log2) and `getABITypeAlignment` / `getPrefTypeAlignment`.

3. **Create sub-alloca.** Allocate an 80-byte `AllocaInst` with the `.sroa.` name prefix. Copy debug metadata from the original. Insert before the original alloca.

4. **Rewrite uses.** The `visitUse` function (`sub_292A4F0`) rewrites each user instruction: GEPs are retargeted, loads/stores get type-cast if needed, memcpy/memset calls are split to cover the sub-alloca's range.

5. **Speculative loads through select.** When a load reaches its pointer through a `select(cond, ptrA, ptrB)`, SROA hoists the load into both branches:

```
// Before:
%p = select i1 %c, ptr %a, ptr %b
%v = load T, ptr %p

// After:
%vt = load T, ptr %a          ; .sroa.speculate.load.true
%vf = load T, ptr %b          ; .sroa.speculate.load.false
%v  = select i1 %c, T %vt, T %vf  ; .sroa.speculated
```

This is significant on GPU because it converts a control-dependent memory access into two independent loads with a predicated select, which maps naturally to the SIMT execution model.

### Interaction with .param Space

Function parameters passed by value in PTX use the `.param` address space (NVPTX address space 101). The frontend generates an alloca to hold a copy of each byval parameter, then loads fields from it. SROA is the primary mechanism for eliminating these allocas: it splits the aggregate parameter copy into individual scalar values, which the register allocator can then place in registers. Without SROA, every struct parameter would round-trip through `.local` memory. The `NVPTXTargetLowering::LowerCall` function (`sub_3040BF0`) emits `DeclareParam` (opcode 505) and `StoreV1/V2/V4` (opcodes 571-573) for the `.param` writes; SROA's job is to ensure the corresponding reads never touch memory.

### Data Structures

| Structure | Size | Layout |
|-----------|------|--------|
| Slice entry | 24 bytes | `[u64 start, u64 end, u64 flags]`; bit 2 = splittable marker, bits [63:3] = user instruction metadata pointer |
| Sub-alloca record | 56 bytes | `[alloca_ptr, slice_list, slice_list_cap, ...]` stored in SmallVector with 2-element inline buffer |
| Hash table | open-addressing | sentinel `0xFFFFF000` = empty, `0xFFFFE000` = tombstone; hash = `((key >> 9) ^ (key >> 4)) & mask`; resize at 4/3 load factor |

---

## EarlyCSE (Early Common Subexpression Elimination)

| Property | Value |
|----------|-------|
| Pass name | `"early-cse"` |
| Pipeline parser params | `memssa` (enables MemorySSA-backed variant) |
| Entry function | `sub_2780B00` |
| Binary size | 12,350 bytes |
| Pipeline positions | Positions 245/291 (tier 1), 525/593 (tier 2+), and late-pipeline (position ~370) |
| Disable flag | `NVVMPassOptions` offset `+1440` |
| NVIDIA modifications | Barrier-aware generation tracking, shared memory (AS 7) handling, NVVM intrinsic call CSE, PHI operand limit |

### CUDA-Specific Extensions

Cicc's EarlyCSE is **not** stock LLVM. The binary contains four distinct extensions that handle GPU-specific semantics.

**Barrier-aware memory versioning.** The pass maintains a `CurrentGeneration` counter (offset `+0x2E0` in the context structure) that serves as a memory version number. Every time a memory-writing instruction is encountered, the generation is incremented, invalidating all stale entries in the load and call CSE tables. Critically, four specific NVVM intrinsic IDs force generation bumps regardless of their declared memory effects:

| Intrinsic ID | Value | Likely identity |
|-------------|-------|-----------------|
| `0x9B` (155) | `llvm.nvvm.barrier0` | `__syncthreads()` |
| `0xCD` (205) | `llvm.nvvm.membar.*` | Memory barrier (device/system) |
| `0x123` (291) | NVVM barrier/fence | `bar.sync` or similar |
| `0x144` (324) | NVVM cluster barrier | Cluster-level fence (SM 90+) |

When any of these barrier intrinsics appears between two memory operations, EarlyCSE will not forward the earlier value to the later operation. This prevents the optimizer from CSE-ing loads across synchronization points where another thread may have modified the location. Upstream LLVM would rely on the intrinsic's memory effect attributes; cicc adds explicit ID checks as a safety net.

**Shared memory address space awareness.** Stores to address space 7 (NVPTX's internal representation for `__shared__` memory) receive special treatment. At address `0x2781BB6`, the pass checks `byte [rdx+8] == 7` and routes shared-memory stores through `sub_B49E20` (`isSharedMemoryStore`). This prevents unsafe store-to-load forwarding in shared memory, where the stored value may be consumed by a different thread after a barrier -- forwarding would bypass the barrier's visibility guarantee.

**NVVM intrinsic call CSE.** The dedicated function `sub_2780450` (263 lines) handles CSE for calls to NVVM builtins. Many NVVM intrinsics (`llvm.nvvm.read.ptx.sreg.tid.x`, `llvm.nvvm.read.ptx.sreg.ntid.x`, etc.) are thread-invariant within a kernel invocation: `threadIdx.x` never changes for a given thread. Upstream LLVM would model these as `readnone` and CSE them through the generic call table. The NVVM-specific handler adds a fast-path that recognizes these intrinsic IDs directly, avoiding the overhead of the general readonly-call analysis for the most common GPU builtins.

**PHI operand limit.** At address `0x2781BED`, the pass skips CSE analysis for PHI nodes with more than 5 incoming values. This is a compile-time heuristic: GPU kernel code after loop unrolling and predication can produce PHI nodes with dozens of operands. Comparing all incoming values for CSE equivalence becomes quadratic, and the benefit is marginal for wide PHIs. Upstream LLVM has no such limit.

### Scoped Hash Tables

The pass uses four separate scoped hash tables, all using open-addressing with linear probing:

| Table | Context offset | Key | Value | Purpose |
|-------|---------------|-----|-------|---------|
| Expression | `+0xE8` | Opcode + operands (value number) | Canonical instruction | Arithmetic, casts, GEPs, comparisons |
| Load | `+0x178` | Load address + type | Previously loaded value | Load CSE and load-after-load elimination |
| Call | `+0x230` | Call target + arguments | Return value | Readonly/readnone call deduplication |
| Store-fwd | `+0x2C0` | Store address + type | Stored value | Store-to-load forwarding |

Upstream LLVM EarlyCSE has three tables (expression, load, call). The dedicated store-forwarding table is an NVIDIA addition that enables more aggressive dead-store detection within the early pipeline.

All tables share the same hash infrastructure: power-of-2 bucket counts, 75% load factor threshold, tombstone cleanup when tombstones exceed 12.5% of capacity. Sentinel values are `0xFFFFFFFFFFFFF000` (empty) and `0xFFFFFFFFFFFFE000` (tombstone).

The scoping mechanism uses the dominator tree walk. Each DomTreeScope node (160 bytes, allocated via `sub_22077B0`) records which hash table entries were inserted during that scope. On scope exit, all entries are tombstoned and counters are adjusted. The walk itself is iterative (explicit stack), not recursive -- important for deeply-nested GPU kernels.

### Knobs

No dedicated `cl::opt` flags were found for the NVIDIA EarlyCSE extensions. The PHI operand limit of 5 and the barrier intrinsic ID list are hardcoded. The `memssa` pipeline parameter selects between the standard and MemorySSA-backed variants; the pipeline assembler calls `sub_18E4A00` for the MemorySSA variant and `sub_196A2B0` for the standard variant.

---

## JumpThreading

| Property | Value |
|----------|-------|
| Pass name | `"jump-threading"` |
| Entry function | `sub_2DC4260` |
| Binary size | 12,932 bytes |
| Block duplication helper | `sub_2DC22F0` (2,797 bytes) |
| CFG finalization | `sub_2DC30A0` (1,094 bytes) |
| Pipeline positions | Positions 234, 278, and late-pipeline (~239 in tier-3) |
| Disable flag | `NVVMPassOptions` offset `+320` |
| NVIDIA override flag | `"disable-jump-threading"` (separate from LLVM's `"disable-JumpThreadingPass"`) |
| OCG experiment note | `"Disable jump threading for OCG experiments"` |

### LLVM Knobs

| Knob | Default | Global address range | Description |
|------|---------|---------------------|-------------|
| `jump-threading-threshold` | **6** | `qword_4FFDBA0` | Max instructions in a block eligible for duplication |
| `jump-threading-implication-search-threshold` | **3** | `qword_4FFDAC0` | Max predecessors to search for condition implications |
| `jump-threading-phi-threshold` | **76** | `qword_4FFD9E0` | Max PHI nodes in a block eligible for duplication |
| `jump-threading-across-loop-headers` | **false** | `qword_4FFD900` | Allow threading across loop headers (testing only) |
| `jump-threading-disable-select-unfolding` | **false** | `qword_4FFDC80` | Disable unfolding select instructions into branches |
| `print-lvi-after-jump-threading` | **false** | -- | Debug: print LazyValueInfo cache after pass |

### Interaction with StructurizeCFG

JumpThreading is fundamentally at odds with the GPU requirement for reducible control flow. The pass works by duplicating a basic block so that different predecessors jump directly to the appropriate successor, bypassing a conditional branch. This can create multi-entry loops (an irreducible cycle) when the duplicated block is a loop header or when the threading target is inside a loop whose header is not the threading source.

Cicc addresses this tension through three mechanisms:

1. **Loop header protection.** The `jump-threading-across-loop-headers` flag defaults to `false`. The pass queries LoopInfo (via a red-black tree lookup at `0x2DC4781` using `dword_501D5A8` as the analysis key) to determine whether a block is a loop header. If it is, the block is skipped entirely. A parallel DominatorTree query at `0x2DC4839` verifies loop membership and nesting depth. This prevents the most common source of irreducibility.

2. **Conservative duplication thresholds.** The block-size threshold of 6 instructions and the implication search depth of 3 predecessors limit the scope of threading. The PHI threshold of 76, while higher than upstream LLVM's default, still prevents unbounded block growth. These thresholds are tuned to produce threading only in cases where the CFG outcome is highly predictable and the duplication cost is small.

3. **StructurizeCFG as a safety net.** The StructurizeCFG pass (`sub_35CC920`) runs late in the pipeline, after all LLVM scalar and loop transforms. If JumpThreading (or any other pass) creates an irreducible cycle, StructurizeCFG's irreducibility detector (`sub_35CA2C0`) will identify it and either restructure it or reject it with a diagnostic. This is a defense-in-depth strategy: the threading constraints prevent most cases, and structurization catches any that slip through.

The separate NVIDIA disable flag `"disable-jump-threading"` (registered in `ctor_073` at `0x49A91E`, distinct from LLVM's `"disable-JumpThreadingPass"` at `ctor_637`) provides a global kill switch. The OCG experiment annotation suggests NVIDIA engineers have empirically measured cases where JumpThreading's CFG disruption outweighs its branch-elimination benefit on specific GPU architectures.

### Cost Model

The pass uses a multi-level cost model. At `0x2DC4887`, a global budget of 512 instructions is initialized per invocation. Each block duplication charges the block's instruction count against this budget. Blocks with one or fewer instructions are always eligible. For larger blocks, the pass checks both the number of unique predecessors being threaded and the accumulated cost.

When threading involves multiple predecessors, the per-predecessor cost is computed by dividing the block instruction count by the predecessor count (with ceiling via `sbb eax, -1`). The pass also employs LazyValueInfo (`sub_11F3070`, `sub_DFABC0`) for range-based condition evaluation: if LVI can prove a branch condition's value along a specific incoming edge, the threading is profitable regardless of block size.

### Created Block Names

| Name | Purpose |
|------|---------|
| `"endblock"` | Terminal block of the threaded path |
| `"phi.res"` | PHI resolution node for merged values |
| `"res_block"` | Result block for threaded path |
| `"loadbb"` | Load basic block (for load-bearing threading) |
| `"phi.src1"` / `"phi.src2"` | PHI source blocks |

---

## Function Map

| Address | Identity |
|---------|----------|
| **SROA** | |
| `sub_2935C30` | `SROAPass::runOnAlloca` |
| `sub_2930B90` | `SROAPass::splitAlloca` |
| `sub_2927160` | `buildSlices` (use analysis) |
| `sub_2924690` | `buildPartitions` (group slices) |
| `sub_291A860` | `findExistingValue` |
| `sub_292A4F0` | `visitUse` (rewrite one use) |
| `sub_29197E0` | `rewritePartition` |
| `sub_2916270` | `computeRewrittenValue` |
| `sub_29348F0` | `rewriteGEPChain` |
| `sub_F38250`  | `rewriteLoadStoreOfSlice` |
| `sub_F38330`  | `rewriteMemTransferOfSlice` |
| `sub_B4CE70`  | `isAllocaPromotable` |
| **EarlyCSE** | |
| `sub_2780B00` | `EarlyCSEPass::run` (main pass body) |
| `sub_2780450` | NVVM intrinsic call CSE handler |
| `sub_277F590` | Expression hash function |
| `sub_277AC50` | Expression equality check |
| `sub_277CF80` | Load/call key hash |
| `sub_27792F0` | Load/call key equality |
| `sub_277C800` | Store key hash |
| `sub_27781D0` | Store key equality |
| `sub_D222C0`  | `isSimpleExpression` |
| `sub_B49E20`  | `isSharedMemoryStore` |
| `sub_1020E10` | `getCallCSEValue` |
| **JumpThreading** | |
| `sub_2DC4260` | `JumpThreadingPass::run` |
| `sub_2DC22F0` | Block cloning engine (`duplicateBlock`) |
| `sub_2DC30A0` | CFG finalization after threading |
| `sub_2DC37C0` | Single-instruction threading |
| `sub_2DC40B0` | `tryToUnfoldSelect` |
| `sub_11F3070` | `LVI::getPredicateAt` |
| `sub_DFABC0`  | `evaluateConditionOnEdge` |

## Cross-References

- [StructurizeCFG](./structurizecfg.md) -- the safety net that catches irreducible CFG created by JumpThreading or other passes
- [Register Allocation](./register-allocation.md) -- surviving allocas after SROA become register pressure; failed promotion leads to `.local` memory spills
- [Pipeline & Ordering](./pipeline.md) -- the tier-dependent scheduling of all three passes
- [GVN](./gvn.md) -- GVN performs load CSE and redundancy elimination complementary to EarlyCSE, running later in the pipeline with more expensive analysis
- [MemorySpaceOpt](../passes/memory-space-opt.md) -- resolves generic pointers to specific address spaces; interacts with EarlyCSE's address-space-aware load forwarding
- [DSE](./dse.md) -- Dead Store Elimination complements EarlyCSE's within-block store-to-load forwarding with cross-block dead store detection
