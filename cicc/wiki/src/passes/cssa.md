# CSSA -- Conventional SSA for GPU Divergence

Standard SSA form assumes that a PHI node selects its incoming value based solely on the control flow edge along which execution arrived. On a scalar CPU, exactly one predecessor edge is taken per dynamic execution of the PHI, so this assumption holds trivially. On an NVIDIA GPU, it does not. A warp of 32 threads executes in lockstep, and when control flow diverges -- different threads take different branches -- all paths are eventually serialized and the warp reconverges. At the reconvergence point, a standard PHI node cannot correctly select a single incoming value because the warp carries live values from *multiple* predecessors simultaneously. The wrong thread could see the wrong value.

CSSA (Conventional SSA) is NVIDIA's transformation that rewrites the IR so that every PHI node is safe under warp-divergent execution. It does this by inserting explicit copy instructions at points where threads reconverge, ensuring that each thread's value is materialized into its own copy before the PHI merges anything. The name "Conventional SSA" comes from the SSA literature: a program is in CSSA form when every PHI node's operands can be simultaneously live without interfering -- the PHI web has no overlapping live ranges. This property is exactly what GPU divergence demands.

| | |
|---|---|
| **Pass location** | `sub_3720740` (22KB, ~800 lines decompiled) |
| **Address range** | `0x3720740`--`0x3721501` (3521 bytes) |
| **Gate knob** | `do-cssa` (NVVMPassOptions boolean toggle) |
| **Coalesce knob** | `cssa-coalesce` (controls copy coalescing aggressiveness) |
| **Debug knobs** | `cssa-verbosity`, `dump-before-cssa` |
| **Container knob** | `CSSACoalescing` (NVVM container format, parsed at `sub_CD9990`) |
| **Debug string** | `"IR Module before CSSA:\n"` |
| **Helper cluster** | `sub_371F790` (27KB), `sub_371F160`, `sub_371EDF0`, `sub_371CDC0` |
| **Pass-option slot** | One of the 221 NVVMPassOptions slots (boolean do/don't pair) |
| **Pipeline position** | Late IR, after optimization, before SelectionDAG lowering |
| **Upstream equivalent** | None. LLVM has no concept of warp-divergent PHI semantics. |

## GPU Divergence Background

### The Warp Execution Model

NVIDIA GPUs execute threads in warps of 32 under the [SIMT model](../gpu-execution-model.md#simt-warp-execution): all threads share a program counter, and [divergent branches](../gpu-execution-model.md#divergence) serialize both paths before the warp reconverges. The full warp execution model and its implications for cicc are documented in the [GPU Execution Model](../gpu-execution-model.md).

### Why Standard SSA Breaks

Consider a diamond CFG:

```
         entry
        /     \
     then     else
        \     /
         join        <-- PHI(%x = [then: %a], [else: %b])
```

On a CPU, the PHI at `join` works correctly: execution came from exactly one predecessor, so the PHI selects the corresponding value. On a GPU warp where threads 0-15 took `then` and threads 16-31 took `else`, both paths executed sequentially. When the warp reconverges at `join`, the PHI must produce `%a` for threads 0-15 and `%b` for threads 16-31 *simultaneously in the same register*. A naive lowering of the PHI to a simple register copy is incorrect -- whichever path executed last would overwrite the value from the first path.

### The CSSA Solution

CSSA transforms the IR so that the PHI web has non-interfering live ranges. Concretely, it inserts copy instructions at the end of each predecessor block so that each thread's value is written into a dedicated copy *before* the warp reconverges:

```
         entry
        /     \
     then     else
     %a_copy  %b_copy     <-- inserted copies (one per predecessor)
        \     /
         join
     %x = PHI [then: %a_copy], [else: %b_copy]
```

Now the PHI's operands occupy distinct virtual registers. During later register allocation, the allocator can assign them the same physical register only when their live ranges truly do not overlap -- which is the correct condition for divergent execution. The copies give the allocator the freedom to keep the values separate when divergence requires it.

## Algorithm

The `sub_3720740` function implements CSSA in several phases:

### Phase 1: Basic Block Ordering and Numbering

The function begins by iterating over all basic blocks in the LLVM function (accessed via `[r15]`, the LLVM Module/Function pointer) and assigning sequential numbering. Each basic block receives an ordinal stored at offset `+0x48` (preorder index) and `+0x4C` (reverse postorder index). These indices are used later for dominance and reconvergence queries. The block list is walked via the standard LLVM doubly-linked list at function offsets `+0x48`/`+0x50` (begin/end sentinels), with a secondary worklist stored in a dynamic array at `[rbp-0x240]` that grows via the standard SmallVector growth function `sub_C8D5F0`.

After ordering, the function sets byte `[r8+0x70] = 1` and dword `[r8+0x74] = 0` on the pass state object (at `[r15+8]`), marking the ordering phase as complete. If the ordering was already done (byte `[r8+0x70]` is non-zero on entry), the function skips directly to phase 2.

### Phase 2: PHI Node Scanning and Hash Map Population

The function iterates over every basic block (outer loop at `0x37208C0`) and within each block walks the instruction use-list (inner loop at `0x3720930`). Instructions are identified by checking byte `[rbx-0x18]` (the LLVM Value tag / opcode byte) against `0x54` (decimal 84), which is the LLVM PHI node opcode. Non-PHI instructions are skipped.

For each PHI node found, the function:

1. Increments a monotonic counter at `[r15+0x78]` to assign a unique PHI ID.
2. Computes a hash of the PHI's pointer value using the standard NVIDIA hash: `h = (ptr >> 4) ^ (ptr >> 9)`, masked by `(table_size - 1)`. This is the same hash function used across CICC's DenseMap infrastructure.
3. Inserts the PHI (or looks it up) in the hash map at `[r15+0x60]` with metadata fields: key at `[slot+0]`, PHI ID at `[slot+8]`. The hash table uses LLVM-layer sentinels (-4096 / -8192); see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the probing and growth policy.
4. Calls `sub_A41E30` to resize the hash table when the load factor exceeds the 75% threshold.

### Phase 3: Copy Insertion at Reconvergence Points

After populating the PHI map, the function enters the copy-insertion phase. For each basic block that contains PHI nodes, it:

1. Walks the PHI's incoming values (the use-list at offset `+0x18` through the instruction's operand chain at 32-byte stride).
2. For each incoming value, calls `sub_371F160` with `r8d=1` (the "insert copy" flag). This helper creates a copy instruction at the end of the predecessor block, before the terminator. The copy is named with the `"pcp"` (PHI copy propagation) prefix string, as evidenced by the `lea rax, aPcp` instruction at `0x3720D34`.
3. Calls `sub_ACA8A0` to set the name on the newly created copy instruction.
4. Calls `sub_371CDC0` with an instruction builder struct to create the actual copy/move IR instruction. The call passes opcode `0x22D7` (8919 decimal) as the first argument via `edi` -- this is likely an NVVM-internal opcode for a divergence-safe copy.
5. Calls `sub_371EDF0` to insert the new copy instruction into the predecessor block's instruction list. This is followed by `sub_BD84D0` (the standard LLVM `insertBefore`/`insertAfter`) to splice the instruction into position.
6. Updates the PHI node's use chain: the operand that previously pointed to the original value now points to the copy. This rewiring is done at `0x3720C87`--`0x3720CDD` by manipulating the 32-byte use-def chain entries (pointer at `[use+0]`, predecessor at `[use+8]`, backlink at `[use+10]`).

### Phase 4: Instruction-Level Copy Propagation

After copy insertion, the function iterates over all basic blocks a second time (`0x3720A2F`--`0x3720A62`). For each instruction in each block, it calls `sub_371F790` (27KB, the "NVPTX intrinsic operand builder" / copy propagation helper). This function propagates the `"pcp"` copies through the instruction graph, replacing uses of the original values with uses of the copies where appropriate, and eliminating redundant copies where the original value and the copy provably carry the same value for all threads.

### Phase 5: Dead Copy Cleanup

The final phase walks a linked list at `[r15+0x28]` (a cleanup worklist). For each entry, it checks whether the instruction at `[entry+8]` has zero remaining uses (`[rdi+0x10] == 0`). If so, it calls `sub_B43D60` to erase the dead instruction. This removes copies that were rendered unnecessary by the propagation phase.

## Copy Coalescing

The `cssa-coalesce` knob controls how aggressively the pass coalesces the inserted copies back together. Without coalescing, CSSA inserts one copy per PHI operand per predecessor -- potentially a large number of copies in control flow with many branches. Coalescing identifies cases where two or more copies carry the same value and can share a single register, reducing the copy overhead.

The `CSSACoalescing` knob in the NVVM container format (parsed by `sub_CD9990` from the finalizer knobs structure) provides a separate control path for the same behavior. The container knob is categorized alongside register allocation and scheduling controls (`AdvancedRemat`, `DisablePredication`, `DisableXBlockSched`, `ReorderCSE`), confirming that CSSA coalescing is considered part of the register allocation subsystem.

## deSSA Alternative

The `usedessa` knob (default value 2, registered at `ctor_358_0` at `0x50E8D0`, stored in `dword_4FD26A0`) selects an alternative path for PHI elimination during the transition from SSA to machine code. Despite its name suggesting "de-Static Single Assignment", analysis of the dispatch functions shows it controls the scheduling and PHI elimination pipeline:

| Mode | Pre-RA Scheduling | Post-RA Scheduling | Behavior |
|------|---|---|---|
| 1 | Skipped | Minimal (single pass) | Simple mode -- no pre-RA scheduling |
| 2 (default) | Full (`&unk_4FC8A0C`) | Three passes + StackSlotColoring | Full mode -- complete scheduling pipeline |

The deSSA mode and CSSA transformation are complementary. CSSA operates at the LLVM IR level, converting PHI nodes into a form safe for GPU divergence *before* instruction selection. The `usedessa` mode controls how PHI nodes are ultimately eliminated *during* the MachineIR lowering, after SelectionDAG has already consumed the CSSA-transformed IR. When `usedessa=2` (default), the full scheduling pipeline runs, giving the register allocator maximum flexibility to handle the extra copies that CSSA introduced. When `usedessa=1`, the minimal scheduling mode may be appropriate for debugging or for kernels where scheduling causes regressions.

## Configuration Knobs

### NVVMPassOptions Knob

| Knob | Type | Description |
|------|------|-------------|
| `do-cssa` | bool | Master enable/disable for the CSSA pass |

Set via `-opt "-do-cssa=0"` to disable the pass entirely.

### cl::opt Knobs (ctor_705 at `0x5BD430`)

| Knob | Type | Default | Global | Description |
|------|------|---------|--------|-------------|
| `cssa-coalesce` | int | (unknown) | (ctor_705 data) | Controls PHI operand coalescing aggressiveness. Higher values = more aggressive coalescing = fewer copies but higher risk of incorrect merging under divergence. |
| `cssa-verbosity` | int | 0 | (ctor_705 data) | Verbosity level for diagnostic output during the CSSA transformation. |
| `dump-before-cssa` | bool | false | `qword_5050A28` | When non-zero, dumps the entire IR module before CSSA runs. Triggers the `"IR Module before CSSA:\n"` output followed by `sub_A69980` (Module::print). |

### Container-Format Knob

| Knob | Parsed At | Category | Description |
|------|-----------|----------|-------------|
| `CSSACoalescing` | `sub_CD9990` | Register allocation / scheduling | Controls CSSA coalescing from the NVVM container format. Parsed alongside `AdvancedRemat`, `DisablePredication`, `DisableXBlockSched`. |

### Related Knob

| Knob | Type | Default | Global | Description |
|------|------|---------|--------|-------------|
| `usedessa` | int | 2 | `dword_4FD26A0` | Selects deSSA method / scheduling pipeline mode. Mode 1 = simple (no pre-RA scheduling), mode 2 = full. |

## Diagnostic Strings

```
"IR Module before CSSA:\n"          -- Module dump header (dump-before-cssa)
"pcp"                               -- PHI copy propagation instruction name prefix
```

The `"pcp"` prefix is assigned to all copy instructions created by the CSSA pass. These copies can be identified in IR dumps by their `%pcp` naming. After register allocation, these copies may be eliminated (coalesced into the same physical register) or materialized as actual move instructions in the final PTX.

## Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| CSSA main | `sub_3720740` | 22KB | BB ordering, PHI scanning, copy insertion, cleanup |
| PCP builder | `sub_371F790` | 27KB | PHI copy propagation / intrinsic operand builder |
| Copy insertion helper | `sub_371F160` | -- | Creates copy instruction in predecessor block |
| Copy instruction creator | `sub_371EDF0` | -- | Inserts copy into instruction list |
| Copy IR builder | `sub_371CDC0` | -- | Builds the copy instruction IR node |
| Hash table grow | `sub_A41E30` | -- | DenseMap resize for PHI hash table |
| Module printer | `sub_A69980` | -- | Module::print (for dump-before-cssa) |
| raw_ostream::write | `sub_CB6200` | -- | String output for debug dump |
| Debug stream getter | `sub_C5F790` | -- | Returns current debug output stream |
| Instruction eraser | `sub_B43D60` | -- | Erases dead instruction from parent block |
| Instruction insert | `sub_BD84D0` | -- | BasicBlock::insert (instruction splice) |
| Name setter | `sub_ACA8A0` | -- | Value::setName for `"pcp"` prefix |
| Use chain rewrite | `sub_B96E90` | -- | replaceAllUsesWith on operand |
| Use helper | `sub_B91220` | -- | Use-list manipulation |
| DenseMap grow helper | `sub_C8D5F0` | -- | SmallVector/DenseMap capacity growth |
| Knob registration | `ctor_705` (`0x5BD430`) | 5.4KB | Registers `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa` |
| Container knob parser | `sub_CD9990` | 31KB | Parses `CSSACoalescing` from NVVM container |
| deSSA dispatch (post-RA) | `sub_21668D0` | -- | Scheduling pipeline mode selector |
| deSSA dispatch (pre-RA) | `sub_2165850` | -- | Pre-RA scheduling mode selector |

## Differences from Upstream LLVM

LLVM's standard PHI elimination pass (`llvm::PHIEliminationPass`, registered as `"phi-node-elimination"` at pipeline slot 493 in CICC's pass parser) lowers PHI nodes to machine copies during the SelectionDAG-to-MachineIR transition. It operates under the assumption that PHI semantics follow scalar control flow -- exactly one predecessor contributes a value at each dynamic execution.

NVIDIA's CSSA pass runs *before* instruction selection, at the LLVM IR level, and transforms the IR into a form where PHI elimination can proceed safely even when the underlying execution model is SIMT. The two passes are not alternatives -- CSSA runs first to prepare the IR, then standard PHI elimination runs later to lower the CSSA-safe PHI nodes to machine copies.

This is one of the fundamental semantic gaps between LLVM's CPU-centric IR model and GPU reality. LLVM assumes sequential scalar semantics; NVIDIA's CSSA pass bridges that gap by making the implicit thread-level parallelism explicit in the copy structure of the IR.

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building an equivalent CSSA transformation for GPU targets.

**1. Inserting copies only at the merge block instead of at the end of each predecessor.** The entire point of CSSA is that copies must be placed *before* the warp reconverges, not *at* the reconvergence point. If you insert the copy instruction at the beginning of the merge block (after the PHI), the warp has already reconverged and whichever path executed last has overwritten the register value for all threads. Copies must be at the terminator position of each predecessor block, before control leaves that block. This is the fundamental GPU-vs-CPU distinction: on a CPU, only one predecessor executes so placement does not matter; on a GPU, all predecessors may execute sequentially within the same warp.

**2. Coalescing copies that have divergent live ranges.** The `cssa-coalesce` knob controls how aggressively copies are merged back together. Over-aggressive coalescing can assign two copies to the same physical register when their live ranges overlap under divergence -- threads from different predecessor paths would see each other's values. The coalescer must verify that live ranges are truly non-interfering under the SIMT execution model, not just under the sequential CFG model. A reimplementation that reuses a standard LLVM register coalescer without divergence-aware interference checking will produce silent miscompilation on any kernel with divergent control flow.

**3. Failing to insert copies for uniform PHI nodes that become divergent after later transformations.** CSSA runs before instruction selection, but divergence analysis at that point may be imprecise. A PHI node classified as uniform (all threads agree on the incoming edge) may become effectively divergent after subsequent loop transformations or predication changes the control flow. The safe approach is to insert copies for all PHI nodes and let the coalescing phase remove unnecessary ones. A reimplementation that skips "uniform" PHI nodes based on divergence analysis risks correctness if that analysis is later invalidated.

**4. Using a standard LLVM `PHIElimination` pass without the CSSA preprocessing step.** LLVM's built-in PHI elimination assumes scalar control flow semantics (exactly one predecessor contributes at runtime). Running it directly on GPU IR without first converting to CSSA form will produce incorrect register assignments whenever a warp diverges at a branch leading to a PHI merge point. CSSA is not a replacement for PHI elimination -- it is a prerequisite that transforms PHI semantics into a form safe for the standard lowering.

**5. Not propagating the `"pcp"` copy through the instruction graph after insertion.** Phase 4 of the algorithm (copy propagation via `sub_371F790`) replaces uses of original values with uses of the inserted copies. A reimplementation that inserts copies but skips this propagation step will leave the PHI node still referencing the original value, making the copies dead. The subsequent dead-copy cleanup (Phase 5) will then erase them, and the transformation has no effect -- the original divergence-unsafe PHI remains.

## Reimplementation Checklist

1. **Basic block ordering and numbering.** Assign preorder and reverse-postorder indices to every basic block (stored at block offsets +0x48/+0x4C), used later for dominance and reconvergence queries.
2. **PHI node scanning and hash map population.** Walk all instructions across all basic blocks, identify PHI nodes (opcode 0x54), assign monotonic IDs, and insert into a DenseMap using the hash `(ptr >> 4) ^ (ptr >> 9)` with LLVM-layer sentinels (-4096/-8192) and 75% load-factor growth.
3. **Copy insertion at reconvergence points.** For each PHI node's incoming value, insert a `"pcp"`-prefixed copy instruction at the end of the predecessor block (before the terminator) using opcode 0x22D7 (divergence-safe copy), then rewire the PHI's use chain so the operand points to the copy instead of the original value.
4. **Copy propagation.** Iterate all blocks a second time, invoking the PCP builder on each instruction to propagate inserted copies through the instruction graph, replacing uses of original values with uses of copies where appropriate and eliminating redundant copies where original and copy provably carry the same value for all threads.
5. **Dead copy cleanup.** Walk the cleanup worklist, check each entry for zero remaining uses, and erase dead copy instructions via `eraseFromParent`.
6. **Copy coalescing (cssa-coalesce).** Implement configurable coalescing that identifies cases where multiple `"pcp"` copies carry the same value and can share a single register, reducing copy overhead while preserving correctness under warp divergence.

## Cross-References

- [NVIDIA Custom Passes](./index.md) -- CSSA listed as `sub_3720740` with knobs `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa`
- [Register Allocation](../llvm/register-allocation.md) -- greedy RA consumes CSSA-prepared IR
- [Scheduling](../llvm/scheduling.md) -- `usedessa` knob controls pre-RA/post-RA scheduling mode
- [Code Generation Pipeline](../pipeline/codegen.md) -- CSSA's position in the overall compilation flow
- [StructurizeCFG](../llvm/structurizecfg.md) -- related pass that ensures structured control flow for PTX
- [Rematerialization](./rematerialization.md) -- CSSA copies may interact with remat decisions
- [Configuration Knobs](../config/knobs.md) -- full knob inventory
