# StructurizeCFG

> **Prerequisites:** Familiarity with [GPU execution model](../gpu-execution-model.md) (warp divergence, reconvergence), LLVM dominator tree and post-dominator tree concepts, and the [PTX emission pipeline](../pipeline/emission.md). Understanding of reducible vs. irreducible control flow is assumed.

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Transforms/Scalar/StructurizeCFG.cpp` (LLVM 20.0.0). The upstream version was originally written for AMDGPU; cicc ships both the stock AMDGPU copy at `sub_1F0EBC0` and a separate NVPTX-customized copy at `sub_35CC920`.

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

The attribute IDs likely map to: 56 = `convergent`, 63 = `nodivergencesource`, 59 = `nounwind`, 64 = `alwaysinline`, 57 = `optnone`. `[MEDIUM confidence]` These numeric-to-name associations are inferred from LLVM attribute enumeration ordering in the upstream source and the semantic context of their usage (skip-structurize guard), not from string evidence in the binary. The attribute enum may differ in NVIDIA's fork. Functions carrying any of these are either already guaranteed to have uniform control flow or are explicitly marked as not-to-be-optimized.

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

## Flow Block Insertion Algorithm

The previous sections describe the pass at the function-dispatch level. This section provides the complete algorithmic detail of how Flow blocks are actually created, wired, and how PHI networks are maintained -- the core transformation that converts a reducible-but-unstructured CFG into a fully structured CFG suitable for PTX emission.

### Complexity

Let B = number of basic blocks, E = number of CFG edges, and D = depth of the dominator tree. The irreducibility detection (`sub_35CA2C0`) is O(B * E) -- for each block in reverse RPO, it probes successors against the dominator tree hash table (O(1) per probe). The per-block classification loop is O(B * (P_avg + S_avg)) where P_avg and S_avg are average predecessor and successor counts -- effectively O(B + E). The uniform branch classifier (`sub_35CB4A0`) is O(1) per block (a few flag checks and one DivergenceAnalysis query). The NCA computation (`sub_35C9ED0`) walks the domtree upward from two nodes until convergence: O(D) per call. Each Flow block insertion is O(D + PHI_count) where PHI_count is the number of PHI nodes at the original merge point (each needs entry copying). Recursive child splitting adds at most O(B) new blocks total across the entire function. The bitvector tracking is O(B / 64) per test/set operation. Overall: O(B * D + E + F * PHI_total) where F = number of Flow blocks created. Since F <= B (one Flow per divergent region) and D = O(B) in the worst case, the theoretical worst case is O(B^2 + E). In practice, CUDA CFGs are shallow (D < 20) and sparsely divergent, making the pass effectively O(B + E).

### Conceptual Model

A "Flow block" is a synthetic basic block that serves as an explicit thread reconvergence point. In an unstructured CFG, divergent branches may merge at a common successor without any indication of *which* predecessor each thread arrived from. The hardware's reconvergence mechanism needs a single merge point where it can resume lockstep execution. Flow blocks provide this by:

1. Interposing between the divergent region and its exit.
2. Carrying a PHI node whose value encodes the path taken by each thread.
3. Branching conditionally on that PHI to either enter the next region body or skip to the next Flow block.

The algorithm processes the function bottom-to-top (reverse RPO), which ensures that inner regions are structurized before outer ones. Each region is defined by a head (dominator) and tail (post-dominator). The output is a function where every conditional branch leads to at most one "then" block followed by a Flow block, guaranteeing single-entry single-exit regions.

### Top-Level Algorithm: sub_35CC920

This is the complete algorithm for the main pass body, including the Flow block insertion logic interleaved with the classification phases already described above.

```
sub_35CC920(pass, function):
    // ---- Phase 1: Analysis setup ----
    div_info    = getAnalysis<DivergenceAnalysis>(function) + 200
    loop_info   = getAnalysis<LoopInfo>(function) + 200
    dom_tree    = getAnalysis<DominatorTree>(function) + 200
    post_dom    = getAnalysis<PostDominatorTree>(function) + 200
    pass[65]    = div_info
    pass[66]    = loop_info
    pass[67]    = NULL          // region_head
    pass[68]    = NULL          // region_tail
    pass[69]    = dom_tree
    pass[70]    = post_dom

    // Compute RPO via sub_2EA7130 -> sub_2EA7B20
    rpo_list = computeRPO(function)

    // Cross-reference RPO with SCC decomposition (sub_357E170)
    scc_order = buildSCCOrdering(rpo_list)

    // ---- Phase 1b: Reject irreducible ----
    if sub_35CA2C0(scc_order, dom_tree) == 1:   // irreducible detected
        sub_35CA580(pass, "UnsupportedIrreducibleCFG",
                    "Irreducible CFGs are not supported yet.")
        return 0

    // ---- Phase 1c: Initialize bitvector ----
    bb_count = countBasicBlocks(function)
    word_count = (bb_count + 63) >> 6
    bitvector = allocate(word_count * 8)
    memset(bitvector, 0, word_count * 8)
    pass[91] = bitvector   // at offset +728

    // ---- Phase 2: Bottom-up region identification and Flow insertion ----
    modified = false
    order = reverse(scc_order)      // process bottom-to-top

    for each BB in order:
        // 2a. Reject EH funclets
        if *(BB + 235) != 0:       // isEHFunclet flag
            sub_35CA580(pass, "UnsupportedEHFunclets",
                        "EH Funclets are not supported yet.")
            resetBitvector(pass)
            return 0

        // 2b. Already marked for structurization (from prior inner-region pass)
        if *(BB + 216) != 0 or *(BB + 262) != 0:
            sub_35CBCD0(pass, BB, context)
            continue

        // 2c. Detect back-edges to already-visited blocks (loop detection)
        has_loop_backedge = false
        for each successor S of BB:
            if bitvectorTest(pass[91], S->ordinal):
                has_loop_backedge = true

        // 2d. Classify predecessors for divergence
        needs_structurize = false
        for each predecessor P of BB:
            if sub_35CB4A0(pass, P, ...) == 1:  // divergent branch
                needs_structurize = true
                break

        // 2e. Structurize the region rooted at BB
        if needs_structurize:
            sub_35CBCD0(pass, BB, context)      // collect region bounds

            // If region bounds are valid, insert Flow blocks
            head = pass[67]
            tail = pass[68]
            if head != NULL and tail != NULL:
                modified |= insertFlowBlocks(pass, head, tail, function)

        // 2f. Update bitvector
        if needs_structurize:
            bitvectorSet(pass[91], BB->ordinal)
        else:
            bitvectorClear(pass[91], BB->ordinal)

    // ---- Phase 3: Domtree-guided outer-region finalization ----
    if pass[67] != NULL and pass[68] != NULL:
        current = pass[67]    // split_point
        while current != NULL:
            if strategy->shouldSplit(current):            // vtable+312
                sub_35CBCD0(pass, current, context)
                modified |= insertFlowBlocks(pass, pass[67], pass[68], function)

            if strategy->shouldSplitChild(current):       // vtable+320
                // recurse into child regions
                modified |= insertFlowBlocksForChildren(pass, current, function)

            current = domtreeParent(dom_tree, current)

        // Store reconvergence metadata for PTX emission
        *(function + 672) = pass[67]    // reconvergence head
        *(function + 680) = pass[68]    // reconvergence tail

    // ---- Phase 4: Cleanup ----
    free(scc_order)
    free(bitvector)
    return modified ? 1 : 0
```

### Flow Block Insertion Detail: insertFlowBlocks

This function (inlined within the Phase 2/Phase 3 loops of `sub_35CC920`, approximately decompiled lines 980--2027) performs the actual CFG transformation for a single region.

```
insertFlowBlocks(pass, head, tail, function):
    // Step 1: Validate region boundaries via dominator/post-dominator trees
    if not dominates(pass[69], head, tail):
        return false    // head does not dominate tail => not a valid region
    if not postDominates(pass[70], tail, head):
        return false    // tail does not post-dominate head => not a valid region

    // Step 2: Classify edges leaving the tail block
    external_edges = []     // edges pointing outside the region
    internal_edges = []     // edges pointing back inside the region

    for each successor S of tail:
        if not dominatedBy(S, head) or S == tail:
            external_edges.append((tail, S))
        else:
            internal_edges.append((tail, S))

    // Step 3: Query strategy object for each edge
    for each edge E in (external_edges + internal_edges):
        classification = strategy->classifyEdge(E)   // vtable+344
        if classification == SKIP:
            continue
        // else: edge needs restructuring

    // Step 4: Create the Flow block
    //   sub_2E7AAE0 = BasicBlock::Create(context, name_hint, function)
    flow_bb = sub_2E7AAE0(function->getContext(), "Flow", function)

    //   sub_2E33BD0 = insert into function's BB list after tail
    sub_2E33BD0(flow_bb, tail->getNextNode())

    // Step 5: Build PHI node in the Flow block
    //   The PHI encodes "which path did threads arrive from?"
    //   Convention: true (i1 1) = came from the "then" body
    //              false (i1 0) = skipped the body (fell through)
    phi = createPHINode(Type::i1, flow_bb)
    phi.addIncoming(ConstantInt::getTrue(),  body_block)    // threads that executed body
    phi.addIncoming(ConstantInt::getFalse(), head_block)    // threads that skipped body

    // Step 6: Create conditional branch in the Flow block
    //   Branch on PHI: true -> next_region_or_exit, false -> next_flow_or_exit
    createCondBranch(flow_bb, phi, next_target_true, next_target_false)

    // Step 7: Reroute original edges through the Flow block
    //   For each predecessor that previously branched to the original merge:
    for each edge (P, original_merge) that should go through flow_bb:
        // sub_2E337A0 = replaceAllUsesWith for the branch target
        P->getTerminator()->replaceSuccessor(original_merge, flow_bb)

    // Step 8: Copy PHI entries from original merge to Flow block
    //   If the original merge had PHI nodes, their incoming values from
    //   rerouted predecessors must be transferred.
    for each phi_node in original_merge->phis():
        value = phi_node->getIncomingValueForBlock(rerouted_pred)
        // sub_2E33140 = addIncoming to new PHI at flow_bb
        // sub_2E341F0 = removeIncomingValue from original PHI
        flow_bb_phi.addIncoming(value, rerouted_pred)
        phi_node.removeIncomingBlock(rerouted_pred)
        phi_node.addIncoming(flow_bb_phi, flow_bb)

    // Step 9: Update dominator tree
    //   The new Flow block is immediately dominated by head.
    //   It immediately dominates the original merge (if flow_bb is its
    //   only predecessor now).
    dom_tree->addNewBlock(flow_bb, head)

    // Step 10: Update divergence analysis
    //   sub_35C9CD0 = edge reroute handler
    for each rerouted_edge:
        sub_35C9CD0(pass, rerouted_edge)
        strategy->updateDivergence(rerouted_edge)   // vtable+368

    // Step 11: Recursive child-split (if needed)
    //   The strategy may determine that the Flow block itself needs
    //   further splitting (deeply nested divergent regions).
    if strategy->shouldSplitChild(flow_bb):         // vtable+320
        child_flow = sub_2E7AAE0(function->getContext(), "Flow", function)
        sub_2E33BD0(child_flow, flow_bb->getNextNode())
        // ... repeat Steps 5-10 for the child Flow block ...
        // This recursion terminates when shouldSplitChild returns false.

    // Step 12: Expand bitvector if function grew
    new_bb_count = countBasicBlocks(function)
    if new_bb_count > pass[bb_count_field]:
        sub_C8D5F0(pass[91], new_bb_count)    // SmallVector::grow
        // Initialize new words to 0xFF...FF (conservatively "visited")
        // Then clear trailing bits beyond actual block count

    return true
```

### PHI Network Construction for Nested Regions

When multiple Flow blocks are created for a chain of if-then-else regions, the PHI networks form a cascade. Each Flow block's PHI determines whether threads should enter the next body or skip to the subsequent Flow block.

Consider a three-way branch (implemented as nested if-then-else):

```
Before:                          After:
    Entry                            Entry
    / | \                            |
   A  B  C                          cond_A?
    \ | /                           / T   F
    Merge                          A      |
                                   |      |
                                  Flow1   |
                                  / F  T  |
                                 |   cond_B?
                                 |   / T   F
                                 |  B      |
                                 |  |      |
                                 | Flow2   |
                                 | / F  T  |
                                 ||   C    |
                                 ||   |    |
                                 || Flow3  |
                                 | \ | /   |
                                  Merge----+
```

The PHI cascade at each Flow block:

```
Flow1:
    %path_A = phi i1 [ true, %A ], [ false, %Entry ]
    br i1 %path_A, <continue to cond_B>, <skip to Merge via Flow3>

Flow2:
    %path_B = phi i1 [ true, %B ], [ false, %Flow1 ]
    br i1 %path_B, <continue to C>, <skip to Merge via Flow3>

Flow3:
    %path_C = phi i1 [ true, %C ], [ false, %Flow2 ]
    br i1 %path_C, <Merge>, <Merge>
    // Flow3's branch is unconditional to Merge (both sides converge)
    // but the PHI values propagated through the chain ensure each
    // thread sees the correct value at Merge's PHI nodes.
```

Each Flow block carries exactly one `i1` PHI and one conditional branch. The chain length equals the number of divergent exits from the region minus one. The final Flow block has an unconditional branch (or a branch where both targets are the same) because all paths must converge at the region exit.

### Loop Flow Block Insertion

For divergent loops, Flow blocks serve double duty: they both gate the loop body and control the back-edge. The algorithm handles loops specially:

```
insertLoopFlowBlock(pass, header, latch, exit, function):
    // The loop has structure: header -> body -> latch -> {header, exit}
    // After structurization:
    //   header -> body -> FlowLoop -> {header (back-edge), exit}

    // Step 1: Create FlowLoop block between latch and exit
    flow_loop = sub_2E7AAE0(context, "Flow", function)
    sub_2E33BD0(flow_loop, latch->getNextNode())

    // Step 2: PHI in FlowLoop encodes continue/break decision
    //   Convention: true = exit the loop, false = take back-edge
    //   This is INVERTED from what you might expect.
    //   Rationale: the "default" path (false) continues the loop,
    //   and the "exception" path (true) exits. This matches
    //   upstream LLVM's structurization invariant and simplifies
    //   the PHI lowering in CSSA.
    phi_loop = createPHINode(Type::i1, flow_loop)
    phi_loop.addIncoming(ConstantInt::getTrue(),  exit_pred)   // threads exiting
    phi_loop.addIncoming(ConstantInt::getFalse(), body_block)  // threads continuing

    // Step 3: Conditional branch
    createCondBranch(flow_loop, phi_loop, exit, header)
    // true -> exit, false -> header (back-edge)

    // Step 4: Reroute latch
    latch->getTerminator()->replaceSuccessor(header, flow_loop)
    latch->getTerminator()->replaceSuccessor(exit, flow_loop)

    // Step 5: Update loop info
    //   FlowLoop is inside the loop (it has the back-edge to header).
    //   LoopInfo must be updated so that FlowLoop is recognized as
    //   a loop block, otherwise subsequent passes (LICM, LSR) may
    //   misclassify it.
    loop_info->addBlockToLoop(flow_loop, loop)

    // Step 6: Domtree update
    //   FlowLoop is dominated by latch (or by header if the latch
    //   was the only block between header and exit).
    dom_tree->addNewBlock(flow_loop, latch)
```

The inverted convention (true = break) is critical. It ensures that the "natural" loop iteration (the common case) follows the fall-through path, which maps to the hardware's predicted branch direction. The PTX assembler uses this hint to generate the `@p bra` instruction with the back-edge as the taken path, minimizing branch misprediction overhead on the GPU.

## Irreducible CFG Rejection: Why FixIrreducible is Not Scheduled

The pass rejects irreducible CFGs rather than attempting to restructure them. This section documents the design rationale and the consequences.

### What Makes a CFG Irreducible

A CFG is irreducible if it contains a cycle with multiple entry points -- that is, there exist two blocks A and B in the cycle such that neither dominates the other, yet both can be reached from outside the cycle. The classic example is a `goto` into the middle of a loop:

```
Irreducible:
    Entry
    / \
   v   v
   A -> B
   ^   /
    \ v
     C

Both A and B are reachable from Entry, and both are in the cycle A->B->C->A.
Neither A dominates B nor B dominates A.
```

In a reducible CFG, every back-edge target dominates its source. This is the invariant that `sub_35CA2C0` checks: it iterates blocks in reverse RPO and, for each back-edge (successor that was already visited), verifies that the target dominates the source via the dominator tree hash table.

### The FixIrreducible Pass Exists But Is Not Used

CICC v13.0 links `FixIrreduciblePass` at `sub_29D33E0` (registered as `"fix-irreducible"` at pipeline-parser index 239). Its core implementation at `sub_29D3E80` (60KB) performs controlled node splitting: it duplicates blocks to create a single-entry version of each irreducible cycle. This is the standard compiler technique (T1-T2 node splitting from Hecht and Ullman).

However, the NVPTX pipeline in CICC v13.0 does **not** schedule `FixIrreduciblePass` before `StructurizeCFG`. The pipeline ordering is:

```
... -> SimplifyCFG -> Sink -> StructurizeCFG -> CSSA -> ISel -> ...
                              ^
                              |
                     fix-irreducible is NOT here
```

### Design Rationale

Three factors explain this decision:

1. **CUDA source language guarantee.** Well-formed CUDA C++ does not produce irreducible control flow. The language has no `goto` across loop boundaries (the EDG frontend rejects it), and structured constructs (`if`/`for`/`while`/`do`/`switch`) always produce reducible CFGs. The only way to get irreducible flow is through extreme `goto` abuse in C mode or through a buggy optimization pass that introduces one.

2. **Code size explosion.** Node splitting can exponentially increase code size in pathological cases. For a cycle with N entry points, splitting may duplicate up to 2^N blocks. On a GPU where register pressure is the primary performance limiter, this expansion would be catastrophic -- more blocks means more live ranges, more register pressure, and lower occupancy.

3. **Correctness risk.** `FixIrreduciblePass` transforms the CFG before divergence analysis has finalized. If the splitting creates new blocks with divergent branches, those branches would need re-analysis. The interaction between `FixIrreducible`, `DivergenceAnalysis`, and `StructurizeCFG` is not validated in the NVPTX pipeline.

### Consequence: Silent Miscompilation Risk

When `sub_35CA2C0` detects irreducibility, it emits a diagnostic remark:

```
remark: UnsupportedIrreducibleCFG
        "Irreducible CFGs are not supported yet."
```

The pass then returns 0 (no modification). The function proceeds through the rest of the pipeline with its irreducible CFG intact. Downstream, one of two things happens:

1. **ptxas rejects the PTX.** If the irreducible pattern produces a branch target that violates PTX's structured control flow rules, `ptxas` will emit an error. This is the safe outcome.

2. **ptxas silently accepts malformed PTX.** If the irreducible pattern happens to look like valid PTX (perhaps it only involves uniform branches), the resulting code may execute with undefined reconvergence behavior. Threads may reconverge at the wrong point, producing silent data corruption. This is the dangerous outcome.

### The Stock LLVM Version Has the Same Limitation

The stock LLVM `StructurizeCFG` at `sub_1F0EBC0` (linked from `llvm/lib/Transforms/Scalar/StructurizeCFG.cpp`) contains identical rejection logic. The AMDGPU backend, which also requires structured control flow, schedules `FixIrreduciblePass` explicitly before `StructurizeCFG`. NVIDIA chose not to do this.

| Instance | Address | Size | Irreducible handling |
|----------|---------|------|---------------------|
| NVPTX custom | `sub_35CC920` | 95 KB | Reject with diagnostic |
| Stock LLVM | `sub_1F0EBC0` | ~58 KB | Reject with diagnostic |
| FixIrreducible | `sub_29D33E0` / `sub_29D3E80` | 60 KB | Node splitting (not scheduled) |

### The Stock StructurizeCFG Entry Block Handling

The stock LLVM version also includes explicit entry block handling at `sub_1A74020` (13KB). When the function's entry block has predecessors (which can happen if the function is a loop body extracted by a prior pass), this function creates a new entry block named `"entry"` and renames the original to `"entry.orig"`. The NVPTX version at `sub_35CC920` handles this inline in Phase 1.

## PTX Structured Control Flow Contract

This section documents the precise contract that StructurizeCFG must satisfy for downstream passes to emit correct PTX.

### What "Structured" Means for PTX

After StructurizeCFG completes, the function's CFG must satisfy these five invariants:

1. **Single-entry regions.** Every natural loop has exactly one entry (the loop header dominates all loop blocks). No irreducible cycles exist.

2. **Post-dominator reconvergence.** For every divergent conditional branch at block B, there exists a block P that post-dominates B and dominates all merge points of the two branch targets. A Flow block is inserted at P if one does not already exist.

3. **Linear Flow chain.** Between any divergent branch and its reconvergence point, the CFG forms a chain of Flow blocks with single-entry single-exit semantics. Each Flow block has exactly two predecessors (the "then" body exit and the "skip" path) and two successors (the next body entry or the final merge).

4. **PHI-encodable path selection.** Every Flow block contains an `i1` PHI that encodes which path was taken. This PHI is the sole branch condition of the Flow block's terminator. No other computation occurs in Flow blocks.

5. **Metadata tagging.** Uniform branches are tagged with `!structurizecfg.uniform` metadata (metadata kind registered at `sub_298D780`). This prevents CSSA from inserting unnecessary copies at reconvergence points for branches where all threads agree.

### Downstream Consumer: CSSA

The CSSA pass (`sub_3720740`) consumes the structured CFG and inserts explicit copy instructions at every reconvergence point. It relies on:

- The Flow block chain to identify where reconvergence happens.
- The `i1` PHI in each Flow block to determine which threads took which path.
- The `!structurizecfg.uniform` metadata to skip copy insertion for uniform regions.

Without StructurizeCFG, CSSA would not know where to insert copies, and the resulting register allocation would be unsound under warp divergence.

### Downstream Consumer: Convergence Control in AsmPrinter

The reconvergence head/tail stored at function offsets `+672` and `+680` are consumed by the AsmPrinter's convergence control framework (see [AsmPrinter](../infra/asmprinter.md)). The AsmPrinter emits `CONVERGENCECTRL_ENTRY` (opcode 24) and `CONVERGENCECTRL_LOOP` (opcode 33) pseudo-instructions at the boundaries defined by these metadata values. The hardware uses these to program the convergence barrier stack.

### Interaction with `SIAnnotateControlFlow` (AMDGPU Comparison)

AMDGPU uses a different approach: `SIAnnotateControlFlow` inserts explicit `if`/`else`/`end_cf` intrinsics after StructurizeCFG. NVPTX does not use this -- instead, the convergence information flows through:

1. StructurizeCFG (Flow blocks + function metadata)
2. CSSA (copy insertion at reconvergence)
3. SelectionDAG / ISel (structured branch patterns)
4. AsmPrinter (convergence pseudo-instructions)

This four-stage pipeline is NVIDIA-specific. Upstream LLVM for AMDGPU collapses stages 1-2 into `StructurizeCFG + SIAnnotateControlFlow` and has no equivalent of stage 4.

## The Two Binary Instances

CICC v13.0 contains two complete copies of the StructurizeCFG pass because the binary links both the NVPTX backend (custom) and the generic LLVM Scalar library (stock). Only the NVPTX version is scheduled in the pipeline.

| | NVPTX Custom | Stock LLVM |
|---|---|---|
| **Main body** | `sub_35CC920` (95 KB) | `sub_1F0EBC0` (~58 KB) |
| **Entry gate** | `sub_35CF930` | (inlined) |
| **Region processing** | `sub_35CBCD0` | `sub_1A761E0` (28 KB) |
| **Entry block handler** | (inlined in Phase 1) | `sub_1A74020` (13 KB, strings `"entry.orig"`, `"entry"`) |
| **Region-based** | Operates on entire function | Operates on individual `Region` objects |
| **Uniform metadata** | `sub_298D780` (`"structurizecfg.uniform"`) | Same string, different address |
| **Registration** | `sub_29882C0` (`"Structurize the CFG"`) | `sub_2988270` (`"Structurize control flow"`) |
| **Pipeline parser** | Index 413: `"structurizecfg"` with `skip-uniform-regions` param | Same index, same params |

The NVPTX version is 37 KB larger because it inlines the entry-block handler and region-processing logic (avoiding virtual dispatch overhead) and adds the CUDA-specific attribute checks (IDs 56, 63, 59, 64, 57) and the convergence metadata writes at offsets `+672`/`+680`.

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

The pass uses two DenseSet-style hash tables with LLVM-layer sentinels (-4096 / -8192); see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing, and growth policy. The resize function for this pass is `sub_2E61F50`. Table `v394` tracks BBs already processed during the BFS expansion, and `v417` serves as a scratch set for child-split deduplication.

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

## Cross-References

- [CSSA](../passes/cssa.md) -- the Conventional SSA pass that consumes Flow blocks to insert warp-safe copies
- [AsmPrinter](../infra/asmprinter.md) -- convergence control pseudo-instruction emission consuming the `+672`/`+680` metadata
- [GPU Execution Model](../gpu-execution-model.md) -- warp divergence and reconvergence fundamentals
- [Branch Folding](branch-folding.md) -- may eliminate redundant Flow blocks after code generation
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- details on the DenseSet implementation used by the BB tracking tables
- [Pipeline](pipeline.md) -- exact position of `structurizecfg` in the pass ordering
- [Knobs](../config/knobs.md) -- `structurizecfg-skip-uniform-regions`, `structurizecfg-relaxed-uniform-regions`, `enable-shrink-wrap`
- Upstream LLVM source: `llvm/lib/Transforms/Scalar/StructurizeCFG.cpp`

## Differences from Upstream LLVM

| Aspect | Upstream LLVM (AMDGPU) | CICC v13.0 (NVPTX) |
|--------|------------------------|---------------------|
| **Binary copies** | Single StructurizeCFG in LLVM Scalar library | Two copies: NVPTX-specific at `sub_35CC920` (95 KB) and stock LLVM/AMDGPU at `sub_1F0EBC0`; only NVPTX instance scheduled |
| **Divergence query** | Queries AMDGPU divergence analysis | Queries NVPTX warp divergence analysis; uniform branch skip via `structurizecfg-skip-uniform-regions` knob |
| **Flow block metadata** | Flow blocks inserted without convergence metadata | Inserts convergence control metadata at offsets `+672`/`+680` on Flow blocks, consumed by AsmPrinter for warp reconvergence pseudo-instructions |
| **Relaxed uniform regions** | Not present | `structurizecfg-relaxed-uniform-regions` knob allows less aggressive structurization when all branches in a region are provably uniform |
| **Irreducible CFG handling** | Attempts T1/T2 node-folding reduction | Same approach, but rejection diagnostic `"UnsupportedIrreducibleCFG"` is NVPTX-specific; GPU code with irreducible CFG is a hard error |
| **Skip conditions** | Skip for single-block functions | Extended skip: single-block, `convergent`/`optnone` attributes, `enable-shrink-wrap` = 2, and strategy object decline |
| **Mandatory status** | Required for AMDGPU but can be skipped via flag | Mandatory for PTX emission: registered as required late pass by both `sub_29882C0` and `sub_1A6D600` |
