# StructurizeCFG

CICC v13.0 ships two copies of the StructurizeCFG pass: an NVPTX-specific version at `sub_35CC920` (95 KB, 2,397 decompiled lines) and a stock LLVM/AMDGPU version at `sub_1F0EBC0`. Both exist because the binary links both the NVPTX backend and the generic LLVM Scalar library; only the NVPTX instance is scheduled in the CUDA compilation pipeline. This page documents the NVPTX version exclusively.

The pass is **mandatory** for PTX emission. It is registered as `"structurizecfg"` in the pipeline parser (`sub_2377300`, `sub_233F860`) and listed as a required late pass by `sub_29882C0` and `sub_1A6D600`.

## Why PTX Requires Structured Control Flow

PTX is a structured instruction set. Unlike x86 or ARM, where a branch can target any address and the hardware resolves control flow at retirement, the NVIDIA GPU execution model imposes three hard constraints:

1. **Reconvergence at post-dominators.** When a warp diverges (threads take different sides of a branch), the hardware needs a defined reconvergence point where all threads synchronize before continuing. This reconvergence point must be the immediate post-dominator of the branch. An unstructured CFG has no guarantee that such a point exists or is reachable from both sides.

2. **No multi-entry loops.** A loop header must dominate every block in the loop body. If two distinct blocks serve as loop entries (an irreducible cycle), the hardware has no single point to insert the loop counter logic and the warp-level loop exit barrier. PTX therefore requires all loops to be natural (single-entry, reducible).

3. **No exception handling funclets.** CUDA device code has no runtime support for stack unwinding, personality routines, or catch dispatch. The funclet-based EH model (Windows SEH, C++ landing pads) produces control flow patterns that cannot be expressed in PTX.

The StructurizeCFG pass converts reducible-but-unstructured flow into structured form by inserting "Flow" blocks that serve as explicit reconvergence points. It **rejects** irreducible flow and EH funclets with diagnostic remarks rather than attempting to restructure them.

## Binary Layout

| Function | Address | Size | Role |
|----------|---------|------|------|
| `sub_35CC920` | `0x35CC920` | 95 KB | Main pass body |
| `sub_35CF930` | `0x35CF930` | ~2 KB | Entry gate / dispatch wrapper |
| `sub_35CA2C0` | `0x35CA2C0` | ~4 KB | Irreducibility detector |
| `sub_35CB4A0` | `0x35CB4A0` | ~8 KB | Uniform branch classifier |
| `sub_35CBCD0` | `0x35CBCD0` | ~6 KB | Region structurizer core |
| `sub_35CA580` | `0x35CA580` | ~1 KB | Diagnostic emitter |
| `sub_35CA9C0` | `0x35CA9C0` | ~1 KB | Hash-set insert for BB tracking |
| `sub_35C9CD0` | `0x35C9CD0` | ~2 KB | Edge reroute through new block |
| `sub_35C9ED0` | `0x35C9ED0` | ~1 KB | Domtree NCA (nearest common ancestor) walk |
| `sub_35C9B40` | `0x35C9B40` | trivial | Successor array offset (`return a1 + 8*a3`) |

## Entry Gate: sub_35CF930

`sub_35CF930` is the `runOnFunction` entry. It implements a multi-stage filter before committing to the expensive structurization:

```
sub_35CF930(pass, function):
    // 1. Early-out for trivially uninteresting functions
    if sub_BB98D0(pass, function) fails:
        return 0

    // 2. Single-block functions need no structurization
    bb_list = function + 40
    if bb_list points to itself (single block):
        return 0

    // 3. Query target machine for a structurizer strategy object
    strategy = target_machine->vtable[136](...)

    // 4. Check enable-shrink-wrap override
    switch qword_50400C8:
        case 1:  goto force_structurize    // always run
        case 2:  return 0                  // always skip
        case 0:                            // ask strategy object
            if not strategy->vtable[72](function):
                return 0                   // strategy says skip

    // 5. Check function attributes for safe-to-skip markers
    for attr_id in [56, 63, 59, 64, 57]:
        if sub_B2D610(function, attr_id):
            return 0

    // 6. Run the actual structurizer
    force_structurize:
        return sub_35CC920(pass, function)
```

The attribute IDs likely map to: 56 = `convergent`, 63 = `nodivergencesource`, 59 = `nounwind`, 64 = `alwaysinline`, 57 = `optnone`. Functions carrying any of these are either already guaranteed to have uniform control flow or are explicitly marked as not-to-be-optimized.

## CLI Knobs

| Knob | Registration | Type | Default | Effect |
|------|-------------|------|---------|--------|
| `structurizecfg-skip-uniform-regions` | `ctor_227` @ `0x4E9E40`, `ctor_489` @ `0x553F30` | bool | false | When true, regions with only uniform (warp-coherent) branches are left unstructured, avoiding unnecessary code bloat |
| `structurizecfg-relaxed-uniform-regions` | `ctor_489` @ `0x553F30` | bool | true | Allows treating a region as uniform even if sub-regions contain non-uniform branches, provided there is at most one conditional direct child |
| `enable-shrink-wrap` (`qword_50400C8`) | `ctor_688` @ `0x5A6520` | int (0/1/2) | 0 | **0** = ask `TargetRegisterInfo` (vtable+72) whether to structurize; **1** = force structurize unconditionally; **2** = skip structurize entirely |

The `enable-shrink-wrap` knob is stored as a global at `qword_50400C8`. Despite its name (borrowed from the generic LLVM shrink-wrapping pass infrastructure), it serves as a master override for the structurization decision. Mode 2 effectively disables the pass, which would produce miscompilation for any function with divergent branches -- it exists purely as a debugging/override mechanism.

## Irreducibility Detection: sub_35CA2C0

Called early in `sub_35CC920` (line ~743 of the decompiled output), this function determines whether the CFG contains irreducible cycles. It **detects** irreducibility but does **not** restructure it.

### Algorithm

The function receives the RPO-ordered basic block list from the SCC decomposition phase and iterates backwards:

```
sub_35CA2C0(result, domtree_data, bb_list, bb_count):
    for each BB in reverse(bb_list):
        for each successor S of BB:
            // Probe dominator tree hash table
            // Hash: ((ptr >> 9) ^ (ptr >> 4)) & (bucket_count - 1)
            dom_node = lookup(domtree_data, S)

            // If S does NOT dominate BB, but there is a back-edge
            // from BB to S, this is an irreducible cycle
            if back_edge(BB, S) and not dominates(S, BB):
                return 1  // irreducible

    return 0  // reducible
```

The core invariant: in a reducible CFG, every back-edge target dominates its source. If a back-edge exists where the target does *not* dominate the source, the loop has multiple entries and is irreducible.

### Rejection behavior

When `sub_35CA2C0` returns 1 (irreducible detected), the main pass emits:

```
remark: UnsupportedIrreducibleCFG
        "Irreducible CFGs are not supported yet."
```

via `sub_35CA580` and returns without modifying the function. The return value is forced to 0 (no modification made).

This is a critical design choice. LLVM upstream provides a separate `FixIrreduciblePass` (`sub_29D33E0`, registered as `"fix-irreducible"`) that performs node-splitting to convert irreducible cycles into reducible ones. However, the NVPTX pipeline in CICC v13.0 does **not** schedule `FixIrreduciblePass` before `StructurizeCFG`. The assumption is that well-formed CUDA C++ source never produces irreducible flow. If it does (extreme `goto` abuse, or a prior optimization pass introducing an irreducible pattern), the compilation emits the diagnostic and the resulting PTX will likely be rejected by `ptxas`.

## EH Funclet Rejection

During the per-block iteration in the main loop, each basic block is checked for funclet status at offset `BB+235` (a boolean flag indicating the block is a `catchpad`, `cleanuppad`, or `catchret` target):

```
if BB->isEHFunclet():   // *(BB + 235) != 0
    emit_diagnostic("UnsupportedEHFunclets",
                     "EH Funclets are not supported yet.")
    clear visited bitvector
    bail out
```

The funclet model (Windows x64, ARM64) structures exception handling into mini-functions that require personality routines and unwind tables. None of this exists in the GPU runtime. If a funclet block appears, it means the frontend erroneously lowered exception handling into device code.

After emitting the diagnostic, the pass checks `qword_503FFE8` (a global flag, possibly a debug override). If nonzero, it attempts to find a single-entry point and process the rest of the function; if zero, it bails out entirely.

## Uniform Branch Classification: sub_35CB4A0

This function (~500 decompiled lines) classifies whether a branch instruction is warp-uniform (all threads in the warp take the same direction) or divergent. The classification determines whether the region under that branch needs structurization.

### Classification logic

```
sub_35CB4A0(pass_state, BB, ...):
    terminator_opcode = BB->opcode_category   // BB + 68, unsigned short

    // Non-conditional terminators (ret, unreachable, switch) skip analysis
    if (terminator_opcode - 1) > 1:
        return 0  // not a conditional branch, no structurization needed

    // Check function-level flags
    func_flags = BB->parent->flags   // BB + 32 + 64
    // bit 3 (0x08) = hasConvergentCalls
    // bit 4 (0x10) = hasDivergentBranches

    // Check block-level properties
    block_flags = BB->properties   // BB + 44
    // bit 2 (0x04) = already classified
    // bit 3 (0x08) = uses profile data

    // Query DivergenceAnalysis
    uniformity = sub_2E88A90(divergence_info, BB, mask_bits)
    // mask_bits: 0x80000 = uniform, 0x100000 = divergent, 0x80 = other

    // Additional uniformity check
    is_uniform = sub_2E8B090(divergence_info, BB)

    if is_uniform and skip_uniform_regions_enabled:
        return 0  // uniform, can skip structurization

    return 1  // divergent, needs structurization
```

When the `structurizecfg-skip-uniform-regions` knob is active, regions with all-uniform branches are left unmodified. This is sound because uniform branches do not cause warp divergence and therefore do not require explicit reconvergence points. Skipping these regions reduces code bloat from the insertion of unnecessary Flow blocks.

The `structurizecfg-relaxed-uniform-regions` knob relaxes the uniformity check for sub-regions. In upstream LLVM, `hasOnlyUniformBranches` refuses to treat a region as uniform if any sub-region contains a non-uniform branch. The relaxed mode allows this if there is at most one conditional direct child, under the reasoning that a single divergent sub-region can be handled by an inner structurization pass invocation.

## Region Structurizer Core: sub_35CBCD0

This is the heart of the transformation. When a non-uniform, non-EH block is identified, `sub_35CBCD0` processes its region:

```
sub_35CBCD0(pass_state, BB, context):
    // 1. Manage region boundaries
    head = pass_state[67]   // current region head
    tail = pass_state[68]   // current region tail

    // 2. Iterate successors
    for each successor S of BB (via sub_2E313E0):

        // 3. Check uniformity of successor edge
        if sub_35CB4A0(pass_state, S, ...) returns 0:
            continue  // uniform edge, skip

        // 4. Compute reconvergence point via NCA
        nca = sub_35C9ED0(domtree, BB, S)
        // NCA = nearest common ancestor in dominator tree
        // This is where threads from both sides of the branch
        // must reconverge

        // 5. Update region boundaries
        pass_state[67] = update_head(head, nca)
        pass_state[68] = update_tail(tail, nca)

    // 6. Update visited-BB bitvector
    set_bit(pass_state[91], BB->ordinal)
```

The NCA computation (`sub_35C9ED0`) walks the dominator tree upward from both the current block and its successor until finding their nearest common ancestor. This NCA becomes the reconvergence point: the block where the hardware must synchronize all threads before continuing.

## Main Structurization Loop: sub_35CC920

The main pass body executes in four phases.

### Phase 1: Initialization (lines 433-648)

```
// Store analysis results in pass object fields
pass[65] = DivergenceAnalysis + 200
pass[66] = LoopInfo + 200
pass[67] = 0              // current head
pass[68] = 0              // current tail
pass[69] = DomTree + 200
pass[70] = PostDomTree + 200
pass[71] = loop_depth_info

// Compute RPO (reverse post-order)
rpo = sub_2EA7130() -> sub_2EA7B20()

// Build SCC ordering (cross-references RPO with SCC decomposition)
scc_order = sub_357E170(rpo)

// Check for irreducible cycles
if sub_35CA2C0(scc_order, domtree, ...):
    emit "UnsupportedIrreducibleCFG"
    return 0
```

### Phase 2: Per-block classification (lines 816-2253)

Iterates blocks in reverse RPO order (bottom-to-top):

```
for each BB in reverse_rpo(scc_order):

    // (a) Reject EH funclets
    if BB->isEHFunclet:
        emit "UnsupportedEHFunclets"
        clear bitvector, bail out

    // (b) Already marked for structurization
    if BB->structurize_flag (BB+216) or BB->flag_262 (BB+262):
        sub_35CBCD0(pass, BB, ...)  // structurize this region
        continue

    // (c) Check successors for back-edges to visited blocks
    has_loop = false
    for each successor S of BB:
        if bitvector_test(S->ordinal):
            has_loop = true   // back-edge detected = loop header

    // (d) Classify uniformity of predecessors
    needs_structurize = false
    for each predecessor P of BB:
        if sub_35CB4A0(pass, P, ...):
            needs_structurize = true
            break

    // (e) Apply structurization
    if needs_structurize:
        sub_35CBCD0(pass, BB, ...)

    // (f) Update bitvector
    bitvector_set_or_clear(BB->ordinal, needs_structurize)
```

### Phase 3: Domtree-guided reconvergence (lines 2255-2396)

After the per-block loop, if a split point was identified (`pass[67] != 0` and `pass[68] != 0`):

```
// Walk domtree from split point upward
current = split_point
while current != null:
    // Query strategy object for split decisions
    if strategy->shouldSplit(current):       // vtable+312
        sub_35CBCD0(pass, current, ...)

    if strategy->shouldSplitChild(current):  // vtable+320
        // second round for child regions
        ...

    current = domtree_parent(current)

// Store results in function metadata for PTX emission
function_obj[672] = head    // reconvergence head
function_obj[680] = tail    // reconvergence tail
```

These stored head/tail values are read by subsequent PTX emission passes to emit the correct convergence/reconvergence annotations in the output PTX.

### Phase 4: Cleanup (lines 2383-2396)

Frees the helper object allocated at line 771 (0xA8 bytes), the SCC ordering buffer, and returns the modification flag (0 = no changes, 1 = modified).

## Reconvergence Insertion Path

When a non-uniform divergent region is identified between a head block and a tail block, the pass performs the actual CFG transformation:

### Step 1: Dominance validation

```
// Head must dominate tail
if not sub_2E6D360(domtree, head, tail):
    skip  // invalid region, cannot structurize

// Tail must post-dominate head
if not sub_2EB3EB0(postdomtree, tail, head):
    skip
```

### Step 2: Edge classification

Collect successors of the tail into two sets:
- **External edges**: successors pointing outside the region (into `v395/v396`)
- **Internal edges**: successors pointing back inside the region (into `v404/v405`)

The strategy object (`vtable+344`) classifies each edge to determine if restructuring is needed.

### Step 3: Flow block creation

```
// Create new "Flow" basic block
new_block = sub_2E7AAE0(function, 0, ...)  // BasicBlock::Create
sub_2E33BD0(new_block, insert_point)       // insert into BB list

// Copy phi-node entries from original target
for each phi in original_target:
    sub_2E33140(phi, ...)   // copy incoming value
    sub_2E341F0(phi, ...)   // update predecessor
```

### Step 4: Edge rerouting

```
// Reroute edges from old target to new Flow block
sub_2E337A0(old_target, new_block)         // replaceAllUsesWith
sub_2E33F80(new_block)                     // finalize successors

// For each stale edge, update divergence info
for each stale_edge:
    sub_35C9CD0(stale_edge, ...)
    strategy->updateDivergence(...)        // vtable+368
```

### Step 5: Recursive child splitting

If the strategy's `shouldSplitChild` (vtable+320) returns true, the newly created Flow block itself may need further splitting. This creates another block, reroutes edges again, and recurses. This handles deeply nested divergent regions where a single Flow block is insufficient.

## Before/After CFG Example

Consider a function with a divergent if-then-else:

**Before structurization:**

```
    Entry
    /    \
  Then   Else
    \    /
    Merge
      |
    Exit
```

If the branch at `Entry` is divergent (some threads go to `Then`, others to `Else`), the hardware needs an explicit reconvergence point. After structurization:

**After structurization:**

```
    Entry
    / T
   |    \
   |   Then
   |    /
  Flow1         <- new block: reconvergence for Then
   | F  \
   |   Else
   |    /
  Flow2         <- new block: reconvergence for Else
    |
   Merge
    |
   Exit
```

The `Flow1` and `Flow2` blocks are inserted with conditional branches controlled by PHI networks. `Flow1` has a branch: if the thread came from `Then`, continue to `Flow2`; if the thread skipped `Then`, also continue to `Flow2` (the "false" exit). `Flow2` similarly gates the `Else` path.

For a divergent loop:

**Before:**

```
    Entry
      |
    Header <--+
    /    \     |
  Body    |   |
    \    /    |
   Latch -----+
      |
    Exit
```

**After:**

```
    Entry
      |
    Header <------+
      |            |
    Body           |
      |            |
    FlowLoop       |
    / (back) \     |
   |          +----+
   | (exit)
   Exit
```

`FlowLoop` is a new block whose branch condition is a PHI: `true` incoming from `Body` means exit the loop, `false` means take the back-edge. This inverted convention (true = break, false = continue) matches upstream LLVM's structurization invariant.

## Bitvector Tracking for Region Membership

The pass tracks which basic blocks have been visited using a dynamically sized bitvector stored in the pass object:

| Field | Offset | Meaning |
|-------|--------|---------|
| `uint64_t *array` | `pass + 728` | Pointer to the word array |
| `uint64_t word_count` | `pass + 736` | Current number of 64-bit words |
| `uint64_t capacity` | `pass + 740` | Allocated capacity in words |
| `uint64_t bb_count` | `pass + 792` | Total number of basic blocks |

Index computation for a block with ordinal `idx`:

```c
word_offset = idx >> 6;          // idx / 64
bit_mask    = 1ULL << (idx & 63); // idx % 64

// Test
is_visited = (array[word_offset] & bit_mask) != 0;

// Set
array[word_offset] |= bit_mask;

// Clear
array[word_offset] &= ~bit_mask;
```

When new basic blocks are created during structurization (the function grows), the bitvector is expanded via `sub_C8D5F0` (the `SmallVector::grow` equivalent). New words are initialized to `0xFFFFFFFFFFFFFFFF` (all bits set = "visited"), then trailing bits beyond the actual block count are cleared. This ensures newly created blocks are conservatively marked as visited until explicitly processed.

## Hash Table Implementation

The pass uses LLVM DenseSet-style open-addressing hash tables for BB tracking:

| Property | Value |
|----------|-------|
| Hash function | `((ptr >> 9) ^ (ptr >> 4)) & (size - 1)` |
| Empty sentinel | `-4096` (`0xFFFFFFFFFFFFF000`) |
| Tombstone sentinel | `-8192` (`0xFFFFFFFFFFFFE000`) |
| Resize threshold | `4 * (count + 1) >= 3 * bucket_count` (75% load) |
| Shrink threshold | Tombstones exceed 1/8 of capacity |
| Resize function | `sub_2E61F50` |

Two hash tables are used: `v394` tracks BBs already processed during the BFS expansion, and `v417` serves as a scratch set for child-split deduplication.

## Comparison with Upstream LLVM StructurizeCFG

The NVIDIA version and upstream LLVM share the same fundamental algorithm. Both are derived from the same codebase (confirmed by identical diagnostic strings and strategy-object vtable layouts). The differences are:

### Architectural differences

| Aspect | NVIDIA (`sub_35CC920`) | Upstream LLVM |
|--------|----------------------|---------------|
| **Granularity** | Operates on entire function, iterating blocks in SCC/RPO order | Operates on individual `Region` objects, one region per invocation |
| **Region discovery** | Inline SCC decomposition + domtree walk | Relies on `RegionInfo` analysis pass |
| **Object layout** | Pass fields at `a1[65..91]`; BB flags at `+216`, `+235`, `+262` | Different offsets reflecting different `BasicBlock` subclass |
| **SCC ordering** | `sub_357E170` computes RPO/SCC cross-product | Uses `scc_iterator` from `llvm/ADT/SCCIterator.h` |
| **Strategy object** | Queried via vtable+312/320/344/368 | Uses `TargetTransformInfo` for cost decisions |

### Functional differences

1. **Irreducibility handling.** Both reject irreducible CFGs with the same diagnostic. Neither performs restructuring. Upstream LLVM relies on `FixIrreduciblePass` being scheduled separately (AMDGPU does this). NVIDIA does not schedule it.

2. **EH funclet handling.** Both reject funclets. The NVIDIA version checks `BB+235` (a wider BasicBlock struct with CUDA-specific fields). Upstream checks via `isa<FuncletPadInst>`.

3. **Uniform region skipping.** Both support `structurizecfg-skip-uniform-regions`. The NVIDIA version integrates DivergenceAnalysis queries inline (`sub_2E88A90`, `sub_2E8B090`). Upstream uses `UniformityInfo::isUniform(BranchInst*)`.

4. **Metadata tagging.** Both use the `"structurizecfg.uniform"` metadata kind to mark branches that have been classified as uniform, preventing re-analysis in nested region processing.

5. **Zero-cost hoisting.** Upstream LLVM (recent versions) includes `hoistZeroCostElseBlockPhiValues` to reduce VGPR pressure from structurization-induced phi nodes. The NVIDIA version may or may not include this optimization; the decompiled code at the corresponding offset shows similar phi-manipulation logic but uses different register-pressure heuristics.

6. **Reconvergence metadata.** The NVIDIA version writes reconvergence head/tail to function metadata at offsets `+672` and `+680`. This is consumed by downstream PTX emission passes (`AsmPrinter`, convergence barrier insertion). Upstream LLVM has no equivalent because AMDGPU uses `SIAnnotateControlFlow` instead.

### What NVIDIA did NOT change

The core structurization algorithm is identical: topological ordering of region nodes, iterative flow-block insertion, PHI-node reconstruction via SSAUpdater, and domtree maintenance. The strategy-object interface (shouldSplit, shouldSplitChild, classifyEdge, updateDivergence) has the same vtable layout in both versions. The FlowBlock naming convention (`"Flow"`) is preserved.

## Pipeline Position

StructurizeCFG runs late in the NVPTX backend pipeline, after most IR-level optimizations and before machine code generation:

```
... -> SimplifyCFG -> Sink -> StructurizeCFG -> CSSA -> ISel -> ...
```

It must run **after** divergence analysis (so it can query which branches are uniform) and **before** instruction selection (which assumes structured control flow). The CSSA (Convergent SSA) pass that follows converts phi nodes to respect warp divergence semantics at the reconvergence points that StructurizeCFG inserted.

## Summary of Pass Decisions

| Input condition | Action | Diagnostic |
|----------------|--------|------------|
| Single-block function | Skip | None |
| Function with convergent/optnone attributes | Skip | None |
| `enable-shrink-wrap` = 2 | Skip | None |
| Strategy object declines | Skip | None |
| All-uniform branches (with skip-uniform knob) | Skip | None |
| Irreducible CFG detected | **Reject** | `"UnsupportedIrreducibleCFG"` |
| EH funclet block detected | **Reject** | `"UnsupportedEHFunclets"` |
| Reducible, divergent regions | **Restructure** | None (new Flow blocks inserted, edges rerouted) |
