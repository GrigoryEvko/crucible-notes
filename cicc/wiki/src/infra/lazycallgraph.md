# LazyCallGraph & CGSCC Pass Manager

The LazyCallGraph (LCG) is the data structure that represents which functions call or reference which other functions, built on demand rather than up front. It drives the CGSCC (Call Graph Strongly Connected Components) pass manager, which walks the call graph in bottom-up order so that interprocedural passes -- the inliner, argument promotion, devirtualization, function attribute inference -- process callees before callers. This ordering is essential: the inliner must have finished optimizing a callee's body before it decides whether to inline that callee into a caller. cicc v13.0 uses LLVM's stock LazyCallGraph implementation without NVIDIA-specific modifications to the graph itself. The GPU-specific behavior comes entirely from how the pipeline configures the CGSCC framework: kernels serve as call graph roots, device functions are internal nodes, recursion is rare, and the inline cost model is radically different from any CPU target.

The LCG cluster occupies approximately 220KB of code at `0xD230A0`--`0xD2F8A0`, containing the graph construction logic, Tarjan's SCC algorithm, incremental SCC mutation operations, and the DOT/text graph printers. A separate 69KB function at `sub_2613930` implements the New PM CGSCC inliner that runs inside this framework.


## Key Facts

| Property | Value |
|---|---|
| Binary cluster | `0xD230A0` -- `0xD2F8A0` (~220KB, ~25 functions) |
| LLVM source | `llvm/lib/Analysis/LazyCallGraph.cpp` |
| CGSCC pass manager | `sub_1A62BF0` (the `InlinerWrapper`/standard pipeline factory) |
| CGSCC pipeline parser | `sub_2377300` (103KB) |
| CGSCC-to-function adaptor | `sub_2362FB0` (6.7KB) |
| New PM CGSCC inliner | `sub_2613930` (69KB) |
| NVIDIA custom inliner | `sub_1864060` (75KB, the old CGSCC SCC-walk inliner) |
| Inliner core loop | `sub_186CA00` (61KB, `Inliner::inlineCallsImpl`) |
| DevirtSCCRepeatedPass | `sub_2284BC0` (16KB, "Max devirtualization iterations reached") |
| SCC object size | 136 bytes (`0x88`) |
| Edge encoding | Pointer with tag bits: bit 2 = call edge, bit 2 clear = ref edge |
| DenseMap hash | `hash(ptr) = (ptr >> 4) ^ (ptr >> 9)`, bucket size = 16 bytes |
| DenseMap sentinels | Empty = `0xFFFFFFFFFFFFF000`, Tombstone = `0xFFFFFFFFFFFFE000` |
| CGSCC invocations per O1/O2/O3 | 4 passes of `sub_1A62BF0(1,...)`, 1 iteration each |
| CGSCC invocations at tier 3 | `sub_1A62BF0(5,...)` -- 5 iterations |
| BumpPtrAllocator | `[LCG+0x150]` cursor, `[LCG+0x158]` slab end |


## Lazy Call Graph Construction

The graph is not built all at once. When the CGSCC pass manager begins, the LCG starts with just the module's externally visible functions and kernel entry points as root nodes. Each node's edges are populated only when first visited by the SCC traversal -- the `Node::populateSlow()` method (`sub_D23BF0` returns the edge iterator range) scans all instructions in the function, recording two kinds of edges:

**Call edges** (bit 2 set in pointer tag): direct `CallBase` instructions whose callee resolves to a defined function. These form the strong connectivity that defines SCCs.

**Ref edges** (bit 2 clear): any other reference to a defined function -- a function pointer stored in a global, passed as a callback argument, taken address of. These contribute to RefSCC grouping but do not create call-graph cycles.

```
Node layout (deduced from binary):
  +0x00: Function*          (LLVM IR function)
  +0x08: Edge array pointer  (populated lazily)
  +0x10: Edge count / DFSNumber (int32, -1 = completed)
  +0x14: LowLink             (int32, repurposed as SCC index after Tarjan)
  +0x18: Callee edge list    (second array for call edges)
  +0x20: Callee edge count

Edge encoding (single qword):
  Bits 63..3: pointer to target Node
  Bit 2:      1 = call edge, 0 = ref edge
  Bits 1..0:  reserved (alignment)
```

Population is the only lazy step. Once a node is populated, its edges are cached. Subsequent visits reuse the cached edge list at `[node+0x08]`. The scan checks `[rsi] != 0` to skip unresolvable edges (declaration-only functions with no body).

For a reimplementation: scan every instruction in the function. For each `CallBase`, if the callee is a defined function, add a call edge. Then walk all non-call operands recursively through constants (including `BlockAddress`, `GlobalAlias`, `ConstantExpr`) collecting any additional function references as ref edges. This matches upstream `populateSlow()` exactly.


## SCC and RefSCC: The Two-Level Hierarchy

The LCG maintains a two-level SCC decomposition:

1. **SCC** (Call SCC): a maximal set of functions connected by call edges such that every function is reachable from every other through calls. This is the unit of work for the CGSCC pass manager.

2. **RefSCC** (Reference SCC): a maximal set of SCCs connected by ref edges. A RefSCC contains one or more SCCs. SCCs within a RefSCC can reference each other (e.g., mutually store each other's function pointers) but do not necessarily call each other.

```
RefSCC layout (from [r15] in sub_D25FD0):
  +0x00: LazyCallGraph*     (parent graph)
  +0x08: SCC array pointer   (SmallVector data)
  +0x10: SCC array size
  +0x14: SCC array capacity
  +0x38: DenseMap #1         (SCC* -> index)
         +0x38: qword - bucket base pointer (or inline start)
         +0x40: byte  - flags (bit 0 = active map selector)
         +0x44: dword - tombstone count / generation
  +0x48: DenseMap #2         (alternate map for lazy rehashing)
         +0x48: qword - bucket base pointer
         +0x50: dword - bucket count

SCC layout (136 bytes = 0x88):
  +0x00: qword - parent pointer / metadata
  +0x08: qword - node member array pointer
  +0x10: dword - member count
  +0x14: dword - capacity
  +0x18: Edge list / callee info
  +0x38: DenseMap - node-to-index or similar
```

The bottom-up SCC ordering is computed using Tarjan's algorithm, implemented in `sub_D2C610`. The algorithm uses the standard DFS stack with 24-byte entries (`{Node*, EdgeIter, EdgeEnd}`) and the classic `DFSNumber` / `LowLink` fields at node offsets `+0x10` and `+0x14`. When `LowLink == DFSNumber`, the node is an SCC root -- all nodes above it on the DFS result stack are popped into a new SCC, their DFSNumber set to `-1` (completed), and the SCC index written into the LowLink field for reuse.

The Tarjan inner loop at `0xD2CD90`--`0xD2CEA4` and the SCC member popping at `0xD2CF61`--`0xD2CFD0` are both 4x unrolled, indicating these are hot paths in the CGSCC pipeline.


## Tarjan's SCC Algorithm: Binary-Level Pseudocode

**Complexity.** Tarjan's SCC algorithm is O(V + E) where V = number of nodes (functions) and E = number of call edges among those nodes. The 4x-unrolled inner loop is a constant-factor optimization, not an algorithmic change. The initial `buildSCCs` (`sub_D2BEB0`) runs Tarjan once over the entire call graph: O(V_total + E_total). The incremental `switchInternalEdgeToRef` runs Tarjan only over the affected SCC's members, giving O(V_scc + E_scc) which is typically O(1) since most GPU SCCs contain a single function. `switchInternalEdgeToCall` is O(V_scc + E_scc) for the same-SCC fast path (bit flip only), or O(M * V_scc) for the slow merge path where M = number of SCCs being merged. `switchOutgoingEdgeToCall/Ref` (`sub_D27A10`, 29KB) is O(R * S) where R = number of RefSCCs involved and S = total SCCs in those RefSCCs. The DenseMap operations throughout use `(ptr >> 4) ^ (ptr >> 9)` hashing with O(1) amortized insert/lookup. Graph verification (`sub_D29180`) is O(V + E) for the entire graph. The CGSCC pass manager's outer loop processes each SCC once in post-order, re-visiting at most `max_devirt_iterations` times (default 1, tier 3: 5), giving O(max_iter * V) passes over the SCCs.

The Tarjan implementation lives inside `sub_D2C610` (`switchInternalEdgeToRef`) at address range `0xD2CC66`--`0xD2D0BC`. It recomputes SCCs within a single RefSCC after a call edge is demoted to a ref edge, which may split the original SCC into multiple smaller SCCs.

The following pseudocode is reconstructed directly from the binary. Every variable name corresponds to a register or stack slot; every offset corresponds to a binary address.

```
// Address: 0xD2CC66 -- 0xD2D0BC (inside sub_D2C610)
// Input:  RefSCC containing one SCC whose internal call-edge structure changed
// Output: zero or more new SCCs replacing the original

struct StackEntry {           // 24 bytes (0x18)
    Node*       node;         // +0x00
    Edge*       edge_iter;    // +0x08
    Edge*       edge_end;     // +0x10
};

fn tarjan_recompute_scc(old_scc: &SCC, allocator: &BumpPtrAllocator) -> Vec<SCC> {
    // --- Phase 0: Initialize ---
    let mut dfs_counter: i32 = 1;                       // r13d, starts at 1
    let mut worklist: SmallVector<StackEntry, 4>;        // [rbp-0xA0], 24-byte entries
    let mut result_stack: SmallVector<*Node, 8>;         // [rbp-0x120]
    let mut new_scc_count: i32 = 0;                      // r14d, incremented per SCC found

    // --- Phase 1: Push all nodes of old_scc as unvisited roots ---
    for node in old_scc.members() {
        node.DFSNumber = 0;     // [node+0x10] = 0  (unvisited marker)
        node.LowLink   = 0;     // [node+0x14] = 0
    }

    // --- Phase 2: Outer loop -- pick next unvisited root (0xD2CCF7) ---
    for root in old_scc.members() {
        if root.DFSNumber != 0 { continue; }            // already visited

        // Assign DFS number and LowLink to root
        root.DFSNumber = dfs_counter;                    // [rbx+0x10] = r12d
        root.LowLink   = dfs_counter;                    // [rbx+0x14] = r12d
        dfs_counter += 1;                                // r13d++

        // Lazy-populate edges if not yet done
        let (edge_begin, edge_end) = sub_D23BF0(&root.edge_list);  // 0xD2CD0E
        worklist.push(StackEntry { node: root, edge_iter: edge_begin, edge_end });

        // --- Phase 3: DFS inner loop (0xD2CD90 -- 0xD2CEA4, 4x unrolled) ---
        while let Some(top) = worklist.last_mut() {
            if top.edge_iter == top.edge_end {
                // All edges of current node exhausted -- backtrack
                let finished = top.node;
                worklist.pop();                          // 0xD2CE80

                // LowLink propagation to parent
                if let Some(parent) = worklist.last_mut() {
                    // 0xD2CDF5: min(parent.LowLink, finished.LowLink)
                    let child_low = finished.LowLink;    // [rbx+0x14]
                    if child_low >= 0 && child_low < parent.node.LowLink {
                        parent.node.LowLink = child_low; // [r15+0x14] = edx
                    }
                }

                // --- Phase 4: SCC root detection (0xD2CF01) ---
                if finished.DFSNumber == finished.LowLink {
                    // This node is an SCC root. Pop members from result_stack.
                    // (0xD2CF30 -- 0xD2CFD2, 4x unrolled)
                    let scc_dfs = finished.DFSNumber;    // [r15+0x10]
                    loop {
                        // Unrolled: processes 4 nodes per iteration
                        let member = result_stack.pop();
                        if member.DFSNumber < scc_dfs { break; }  // 0xD2CF61

                        member.DFSNumber = -1;           // 0xFFFFFFFF = completed
                        member.LowLink = new_scc_count;  // assign SCC index
                    }
                    // The root itself
                    finished.DFSNumber = -1;
                    finished.LowLink = new_scc_count;
                    new_scc_count += 1;                  // r14d++
                } else {
                    // Not a root -- push onto result stack for later popping
                    result_stack.push(finished);
                }
                continue;
            }

            // Advance to next edge
            let edge_raw = *top.edge_iter;               // load qword
            top.edge_iter += 1;                          // advance by 8

            let target_node = edge_raw & 0xFFFFFFFFFFFFFFF8;  // mask off tag bits
            let is_call     = (edge_raw & 0x4) != 0;          // bit 2 = call edge

            // Only follow CALL edges for SCC computation (ref edges ignored)
            if !is_call { continue; }
            if target_node == 0 { continue; }            // skip null targets

            let target_dfs = target_node.DFSNumber;      // [target+0x10]

            if target_dfs == 0 {
                // Unvisited: assign DFS number, push onto worklist
                target_node.DFSNumber = dfs_counter;     // 0xD2CD78
                target_node.LowLink   = dfs_counter;
                dfs_counter += 1;

                let (eb, ee) = sub_D23BF0(&target_node.edge_list);
                worklist.push(StackEntry { node: target_node, edge_iter: eb, edge_end: ee });

            } else if target_dfs == -1 {
                // Already in a completed SCC -- skip entirely
                continue;

            } else {
                // On the stack (tree/back edge): update LowLink
                // 0xD2CDF5: min(current.LowLink, target.DFSNumber)
                if target_dfs < top.node.LowLink {
                    top.node.LowLink = target_dfs;
                }
            }
        }
    }
}
```

**Key binary details:**

- The DFS counter is split between `r12d` and `r13d`, alternating roles. In practice `r13d` holds the next available DFS number, starting at 2 (the root gets 1 via the `0x100000001` packed initialization at `0xD2CD0E`).
- The 4x-unrolled inner loop at `0xD2CD90` processes four edge entries per iteration before branching back, reducing loop overhead on this hot path.
- The SCC member popping at `0xD2CF61`--`0xD2CFD0` is likewise 4x unrolled: it pops at offsets `-8`, `-0x10`, `-0x18`, `-0x20` relative to the result stack top, then subtracts `0x20` from the stack pointer per iteration.
- The completed marker `-1` (`0xFFFFFFFF`) is written to `[node+0x10]` (DFSNumber), and the SCC identifier (the `r14d` counter) is written to `[node+0x14]` (LowLink). After Tarjan completes, the LowLink field holds the SCC index for every node -- the DFSNumber/LowLink fields are repurposed, not preserved.
- Only call edges (bit 2 set) are followed during Tarjan. Ref edges (bit 2 clear) are skipped. This is what makes the SCC decomposition "call-SCC" rather than "reference-SCC."

**Complexity:** O(V + E) where V = nodes in the old SCC and E = call edges among those nodes. The 4x unrolling is a constant-factor optimization, not an algorithmic change.


## Incremental SCC Mutation Operations

When a pass modifies the call graph, the SCC structure must be updated without recomputing the entire graph. The LCG provides six mutation operations, each handling a specific kind of edge change. The two most complex are `switchInternalEdgeToCall` and `switchInternalEdgeToRef`; the others handle cross-RefSCC edges and bulk operations.


### switchInternalEdgeToCall -- `sub_D25FD0` (5,526 bytes)

Called when a ref edge within the same RefSCC becomes a call edge (the inliner or devirtualization resolves an indirect call to a direct call). This may merge previously separate SCCs into one.

```
// Address: 0xD25FD0 -- 0xD27566
// Signature (deduced):
//   RefSCC::switchInternalEdgeToCall(
//       Node& SourceN,             // rsi
//       Node& TargetN,             // rdx
//       function_ref<void(ArrayRef<SCC*>)> MergeCB  // rcx (nullable), r8 (data)
//   ) -> bool

fn switchInternalEdgeToCall(source: &Node, target: &Node, merge_cb: Option<Fn>) -> bool {
    let source_scc = sub_D23C40(lcg, source);   // lookupSCC at 0xD26025
    let target_scc = sub_D23C40(lcg, target);   // lookupSCC at 0xD2604E

    // FAST PATH 1: Same SCC -- edge type flip only, no structural change
    if source_scc == target_scc {                // 0xD26B5B
        // Mark the edge as a call edge (flip bit 2) via sub_D23E00
        return false;  // no SCC change
    }

    // Look up SCC indices within the RefSCC's ordered list
    let source_idx = sub_D25BD0(refscc.map, source_scc);  // 0xD26055
    let target_idx = sub_D25BD0(refscc.map, target_scc);  // 0xD260A0

    // FAST PATH 2: Source already appears after target in post-order
    // (the new call edge doesn't create a cycle in the SCC DAG)
    if source_idx > target_idx {                 // 0xD260B4
        // Mark edge as call, no SCC restructuring needed
        return false;
    }

    // SLOW PATH: The new call edge creates a cycle between SCCs.
    // Must merge all SCCs in the range [target_idx .. source_idx].

    // Phase A: DFS reachability within the RefSCC (0xD26C92 -- 0xD26DAB)
    // Walk call edges from target, collecting all SCCs reachable
    // back to source. Uses SmallVector worklist (cap 4) and
    // DenseMap visited set at [r15+0x48].
    let mut merge_set: SmallVector<SCC*, 4>;
    let mut visited: DenseSet<SCC*>;
    // ... DFS marks all SCCs on the cycle ...

    // Phase B: Merge SCCs (0xD26335 -- 0xD263E1)
    let merge_range = &refscc.scc_array[target_idx..=source_idx];
    let merge_count = merge_range.len();

    // Allocate temp buffer for std::rotate
    let tmp = sub_2207800(merge_count * 8);      // operator new
    // sub_D23910 rotates the SCC array to consolidate merged entries
    sub_D23910(refscc.scc_array, target_idx, source_idx);

    // Move all nodes from secondary SCCs into the primary SCC
    for scc in &merge_range[1..] {
        primary_scc.members.extend(scc.members);
        scc.members.clear();
    }

    // Update the SCC-to-index DenseMap with double-buffered rehashing
    // Toggle flags byte at [RefSCC+0x40], tombstone old entries,
    // insert new entries into the alternate map via sub_D24C50

    // Phase C: Invoke merge callback (0xD26480)
    if let Some(cb) = merge_cb {
        cb(ArrayRef { ptr: merge_range.as_ptr(), len: merge_count });
    }

    // Phase D: Reindex remaining SCCs (0xD267A2)
    for scc in &refscc.scc_array[target_idx + 1..] {
        scc_index_map[scc] -= merge_count - 1;  // "sub [rax], ebx" at 0xD267B9
    }

    // Notify the graph of structural change
    sub_D23D60(lcg, 1);                          // notifyRefSCCChange

    return true;  // SCC structure changed
}
```

**Allocation fallback:** The temporary buffer allocation at `0xD27447` has a halving fallback (`sar rbx, 1`): if `operator new` fails for the full size, it retries with half the size. This handles the case where the merge set is unexpectedly large.

**DenseMap double-buffering:** The RefSCC maintains two DenseMaps at offsets `+0x38` and `+0x48`. The flags byte at `+0x40` (bit 0) selects which map is "current." When entries are migrated during SCC merging, old entries are tombstoned (`0xFFFFFFFFFFFFE000`) in the departing map and inserted fresh into the other map via `sub_D24C50`. This avoids a full rehash on every merge -- the tombstone count at `+0x44` is incremented, and the map is only rehashed (via `sub_D25CB0`) when the tombstone ratio crosses a threshold.


### switchInternalEdgeToRef -- `sub_D2C610` (5,236 bytes)

Called when a call edge within a RefSCC is demoted to a ref edge (a direct call is deleted or replaced with an indirect reference). This may split a single SCC into multiple smaller SCCs.

```
// Address: 0xD2C610 -- 0xD2DA84
// Signature (deduced):
//   RefSCC::switchInternalEdgeToRef(
//       RefSCC& Result,                   // rdi (output -- new RefSCC or self)
//       ArrayRef<pair<Node*, Node*>> Pairs // rdx (edge mutations), rcx (byte count)
//   ) -> RefSCC&

fn switchInternalEdgeToRef(pairs: &[(Node, Node)]) -> Vec<SCC> {
    // Phase 0: Flip all edge types from call to ref (0xD2C6A2)
    for (source, target) in pairs {
        sub_D23E00(&source.edge_list, target);   // clear bit 2 in edge pointer
    }

    // Phase 1: Check which pairs actually cross SCC boundaries (0xD2C6A2 -- 0xD2CA2B)
    // Processes pairs 4 at a time (4x unrolled loop).
    // For each pair: DenseMap lookup of source's SCC and target's SCC.
    // If same SCC: the call-to-ref demotion might break the SCC.
    // If different SCCs: no structural impact (they were already separated).
    let mut needs_recompute = false;
    for (source, target) in pairs {     // 4x unrolled at 0xD2C6D0
        let src_scc = densemap_lookup(source);
        let tgt_scc = densemap_lookup(target);
        if src_scc == tgt_scc {
            needs_recompute = true;
        }
    }

    if !needs_recompute { return vec![old_scc]; }

    // Phase 2: Run Tarjan's algorithm on the affected SCC (0xD2CC66 -- 0xD2D0BC)
    // (See "Tarjan's SCC Algorithm" section above for full pseudocode.)
    let new_sccs = tarjan_recompute_scc(old_scc, &lcg.allocator);

    if new_sccs.len() == 1 {
        // The SCC survived intact -- no split occurred
        return vec![old_scc];
    }

    // Phase 3: Allocate new SCC objects (0xD2D0BC -- 0xD2D12E)
    for i in 1..new_sccs.len() {
        // BumpPtrAllocator at [LCG+0x150]:
        let cursor = lcg.alloc_cursor;           // [r12+0x150]
        let aligned = (cursor + 7) & !7;         // align to 8
        let new_end = aligned + 0x88;            // 0x88 = 136 bytes per SCC
        if new_end > lcg.alloc_slab_end {        // [r12+0x158]
            sub_9D1E70(allocator, 0x88, 8);      // slow path: allocate new slab
        }
        lcg.alloc_cursor = new_end;
        let scc = aligned as *mut SCC;
        sub_D23F30(scc, lcg);                    // SCC constructor
    }

    // Phase 4: Distribute nodes among new SCCs (0xD2D1F2 -- 0xD2D309)
    // Each node's LowLink field (set by Tarjan to its SCC index) determines
    // which new SCC it belongs to.
    for node in old_scc.members() {
        let scc_idx = node.LowLink;              // [node+0x14]
        new_sccs[scc_idx].members.push(node);
    }

    // Phase 5: Update ownership maps (0xD2D168 -- 0xD2D1DC)
    // Register new SCCs in the RefSCC's SCC list via sub_D248B0
    for scc in &new_sccs[1..] {
        sub_D248B0(lcg, refscc, scc);            // insertRefSCC
    }
    // Update Node -> SCC DenseMap entries
    // Update SCC -> RefSCC back-pointers via sub_D27750

    // Phase 6: Clean up old SCC (0xD2D3D6 -- 0xD2D49A)
    // Reset all DFS/LowLink fields to -1 (completed state)
    // Zero out old SCC's member list
    // Clear old SCC's internal DenseMap via sub_D24EE0

    return new_sccs;
}
```

**Batch processing optimization:** The pair-processing loop at `0xD2C6A2` is 4x unrolled: it processes four `(Node*, Node*)` pairs per iteration, with explicit remainder handling (1, 2, or 3 leftover pairs) at `0xD2CA2B`. Each pair occupies 16 bytes (`0x10`), so the loop advances by 64 bytes per iteration.

**SCC object allocation:** New SCC objects (136 bytes each) are allocated from the LCG's `BumpPtrAllocator` at `[LCG+0x150]`. The allocator maintains a cursor/end pair for the current slab. When the slab is exhausted, `sub_9D1E70` allocates a new slab (the slow path). The alignment requirement is 8 bytes, enforced by the `(cursor + 7) & ~7` round-up at `0xD2D0F0`.


### switchOutgoingEdgeToCall / switchOutgoingEdgeToRef -- `sub_D27A10` (29,179 bytes)

Handles edges that cross RefSCC boundaries. When a ref edge from one RefSCC to another becomes a call edge (or vice versa), the RefSCC structure may need updating. If the new call edge creates a cycle between previously separate RefSCCs, they merge into one. This is the RefSCC-level analog of `switchInternalEdgeToCall`. The function at `sub_D27A10` is 29KB -- the largest single function in the LCG cluster -- because it must handle both directions (to-call and to-ref) and the full RefSCC merge/split logic.


### insertInternalRefEdge -- `sub_D2A080` (15,253 bytes)

Adds a new ref edge within a RefSCC. Called when optimization introduces a new reference between functions that are already in the same RefSCC (e.g., a new constant expression referencing a sibling function). This does not affect SCC structure (only call edges define SCCs), but it updates the RefSCC's internal edge tracking.


### computeRefSCC -- `sub_D2AD40` (12,495 bytes)

Computes the RefSCC decomposition from scratch for a set of nodes. Used during initial graph construction (`sub_D2BEB0`) and when incremental updates are insufficient (e.g., after bulk edge insertion). This runs a second level of Tarjan's algorithm over the ref-edge graph, grouping SCCs into RefSCCs.


### mergeRefSCC -- `sub_D2DA90` (17,930 bytes)

Merges two or more RefSCCs into one. Called when a new ref edge or promoted call edge connects previously separate RefSCCs that are now mutually reachable. This involves relocating all SCCs from the source RefSCC into the target, updating the graph's RefSCC list at `[LCG+0x240]`, and fixing all back-pointers.


## CGSCC Pass Manager: Bottom-Up Interprocedural Optimization

The CGSCC pass manager (`sub_1A62BF0`) wraps the LCG traversal and runs a pipeline of CGSCC passes over each SCC in bottom-up (post-order) order. The pass manager is invoked multiple times at different points in the optimization pipeline, controlled by a `pipelineID` parameter.

In the O1/O2/O3 pipeline, it is invoked four times, each with 1 devirtualization iteration:

```
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #2  (inliner framework, early)
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #17 (after DSE/GVN/MemCpyOpt)
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #21 (after ADCE/JumpThreading)
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #38 (late, after Sink)
```

At higher tier levels (tier 3 aggressive optimization), a 5-iteration variant appears: `sub_1A62BF0(5,0,0,1,0,0,1)`. The first parameter controls the maximum number of SCC re-visitation iterations when the call graph is mutated during optimization.

The pipeline IDs observed across all optimization levels are 1, 2, 4, 5, 7, and 8, likely corresponding to LLVM's `PassBuilder` extension points:

| Pipeline ID | Extension Point | Notes |
|---|---|---|
| 1 | `EP_EarlyAsPossible` / basic cleanup | Most common, 4x per O2 |
| 2 | `EP_LoopOptimizerEnd` | |
| 4 | `EP_ScalarOptimizerLate` | Sometimes with `optFlag=1` |
| 5 | `EP_VectorizerStart` | Used at tier 3 (5 iterations) |
| 7 | `EP_OptimizerLast` | |
| 8 | `EP_CGSCCOptimizerLate` | With `optFlag=1` for inlining |


### The CGSCC Pass Manager Run Loop

The pass manager's run loop implements the `DevirtSCCRepeatedPass` pattern. For each SCC in post-order:

```
fn run_cgscc_pipeline(module: &Module, lcg: &mut LazyCallGraph, max_devirt_iterations: u32) {
    // Build initial SCC post-order via sub_D2BEB0 (buildSCCs)
    let post_order = lcg.build_sccs();           // sub_D2BEB0, 10KB

    for refscc in post_order.bottom_up() {       // sub_D2F8A0 / sub_D30800
        for scc in refscc.sccs() {               // sub_D2E510, 7KB
            let mut iteration = 0;
            let mut changed = true;

            while changed && iteration < max_devirt_iterations {
                changed = false;
                iteration += 1;

                // Run each registered CGSCC pass on this SCC
                for pass in &cgscc_pipeline {
                    let result = pass.run(scc, lcg);

                    if result.invalidated_call_graph {
                        // The pass mutated the call graph.
                        // Update SCC structure via switchInternal* operations.
                        // If SCCs were merged or split, re-queue affected SCCs.
                        changed = true;
                    }

                    // Run the CGSCC-to-function adaptor (sub_2362FB0)
                    // to apply function-level passes to newly modified functions
                    if result.invalidated_functions {
                        for func in scc.functions() {
                            run_function_pipeline(func);
                        }
                    }
                }
            }

            if iteration >= max_devirt_iterations && changed {
                // sub_2284BC0: "Max devirtualization iterations reached"
                // Controlled by abort-on-max-devirt-iterations-reached knob
            }
        }
    }
}
```

**Iteration semantics:** The `max_devirt_iterations` parameter (argument 1 to `sub_1A62BF0`) controls how many times the pass manager will re-run the CGSCC pipeline on an SCC after the call graph mutates. At O1/O2/O3, this is 1 (single pass, no re-visitation). At tier 3, this is 5 (up to 5 re-runs if devirtualization keeps revealing new direct calls). The devirt iteration check at `sub_2284BC0` emits "Max devirtualization iterations reached" when the limit is hit and the graph is still changing.


### CGSCC-to-Function Adaptor -- `sub_2362FB0` (6,700 bytes)

The adaptor at `sub_2362FB0` wraps a function-level pass for execution inside the CGSCC framework. When the inliner inlines a callee, the callee's body is absorbed into the caller. The caller must then be re-optimized with function-level passes (SimplifyCFG, InstCombine, etc.) before the next CGSCC pass runs. The adaptor handles this by running the function pipeline on each function in the current SCC after each CGSCC pass that reports a change.

The adaptor constructor at `sub_230AC20` (5.4KB) creates the module-to-function or CGSCC-to-function wrappers. The adaptor itself stores the inner pass pipeline as a nested `FunctionPassManager` and forwards `run()` calls to each function in the SCC.


### Registered CGSCC Passes

The registered CGSCC passes (from the pipeline parser at `sub_2377300`):

| Pass name | Address/factory | Purpose |
|---|---|---|
| `inline` | `sub_2613930` | New PM CGSCC inliner (69KB) |
| `argpromotion` | `sub_2500970` | Promote pointer args to by-value |
| `attributor-cgscc` | `sub_2582AC0` | CGSCC attribute deduction (39KB) |
| `attributor-light-cgscc` | -- | Lightweight variant |
| `function-attrs` | `sub_1841180` | Infer `readonly`, `nounwind`, etc. |
| `openmp-opt-cgscc` | -- | OpenMP kernel optimization |
| `coro-annotation-elide` | -- | Coroutine elision |
| `coro-split` | -- | Coroutine splitting |
| `nv-early-inliner` | via `sub_2342850` | NVIDIA early inliner (wraps InlinerWrapper) |

CGSCC analyses (3 registered):

| Analysis name | Purpose |
|---|---|
| `no-op-cgscc` | No-op analysis (placeholder) |
| `fam-proxy` | `FunctionAnalysisManagerCGSCCProxy` -- bridges function-level analyses into CGSCC |
| `pass-instrumentation` | Pass instrumentation callbacks (via `sub_2342830`) |


## How the CGSCC Inliner Uses the Call Graph

The inliner is the most important consumer of the LazyCallGraph. The New PM inliner at `sub_2613930` (69KB) and the NVIDIA custom inliner at `sub_1864060` (75KB) both interact with the LCG through a specific protocol.

The core inlining loop (implemented at `sub_186CA00`, 61KB, `Inliner::inlineCallsImpl`) runs within the CGSCC framework:

```
fn inline_calls_in_scc(scc: &mut SCC, lcg: &mut LazyCallGraph) {
    // Collect all call sites in the SCC
    let mut worklist: Vec<CallSite> = collect_call_sites(scc);

    for callsite in &worklist {
        let callee = callsite.callee();
        let caller = callsite.caller();

        // Compute inline cost
        let cost = compute_inline_cost(callee, caller);  // sub_1864060

        // Decision: inline if cost < threshold
        // (emits optimization remarks: "Inlined", "NotInlined", "AlwaysInline",
        //  "NeverInline", "TooBig", etc.)
        if should_inline(cost) {
            // Perform inlining transformation
            inline_function(callsite);

            // CRITICAL: Update the call graph after inlining.
            // The callee's body is now in the caller. New call edges
            // may have appeared (callee's callees are now caller's callees).
            // Old edges may have disappeared (the call to callee is gone).

            // For each new direct call discovered in the inlined body:
            //   lcg.switchInternalEdgeToCall(caller_node, new_callee_node)
            //     -> may merge SCCs, triggering re-visitation

            // For the removed call edge (caller -> callee):
            //   lcg.switchInternalEdgeToRef(caller_node, callee_node)
            //     -> may split SCCs, triggering re-visitation
            //   (or removeEdge entirely if callee has no other references)

            // Run function-level cleanup on the caller
            // via CGSCC-to-function adaptor (sub_2362FB0)
        }
    }
}
```

**Call graph update protocol:** After each inline transformation, the inliner must report all edge changes to the LazyCallGraph. The CGSCC pass manager provides an `UpdateResult` structure that the inliner fills in:

1. **New call edges:** The inlined function body may contain direct calls that the caller did not previously have. Each creates a `switchInternalEdgeToCall` if target is in the same RefSCC, or `switchOutgoingEdgeToCall` (`sub_D27A10`) if target is in a different RefSCC.

2. **Removed call edges:** The direct call from caller to callee is replaced by the inlined body. If the caller no longer references the callee at all, the edge is removed. If it still references the callee (e.g., another call site remains), the edge type may change.

3. **SCC merging:** If the inlined body creates a new call cycle (e.g., A calls B, B's body contains a call to A), the affected SCCs merge. The merge callback re-queues the merged SCC for another pass of the CGSCC pipeline.

4. **SCC splitting:** If removing the call edge from caller to callee breaks the only call-path cycle, the SCC splits. New SCCs are created and inserted into the post-order traversal at the correct position.


## Initial Graph Construction: buildSCCs -- `sub_D2BEB0` (9,782 bytes)

The initial call graph is built by `sub_D2BEB0` when the CGSCC pass manager first runs. This function:

1. Collects all module-level root functions (kernels, externally visible functions).
2. For each root, lazily populates edges via `sub_D23BF0`.
3. Runs Tarjan's algorithm to decompose the call graph into SCCs.
4. Runs a second pass (`sub_D2AD40`, `computeRefSCC`) to group SCCs into RefSCCs based on ref edges.
5. Stores the resulting post-order in the LCG's RefSCC list at `[LCG+0x240]`.

The post-order traversal helpers (`sub_D2F8A0` at 10KB, `sub_D30800` at 8KB) implement the iterator that the CGSCC pass manager uses to walk RefSCCs and SCCs in bottom-up order. The SCC iteration logic at `sub_D2E510` (7KB) handles advancing through SCCs within each RefSCC.


## Graph Verification -- `sub_D29180` (6,417 bytes)

The verifier at `sub_D29180` checks the consistency of the entire LazyCallGraph after mutations. It validates:

- Every node's SCC assignment is correct (no node belongs to the wrong SCC).
- Every SCC's RefSCC assignment is correct.
- Call edges connect nodes that are reachable via calls (SCC invariant).
- Ref edges connect nodes within the same RefSCC.
- The post-order is valid: for every call edge A -> B, B's SCC appears before A's SCC in the traversal order.
- No dangling pointers (all edge targets are live nodes in the graph).

This verifier is expensive (O(V + E) for the whole graph) and is only enabled in debug builds or when explicitly requested.


## LazyCallGraph Data Structure Layout

```
LazyCallGraph (pointed to by [RefSCC+0]):
  +0x000: ...
  +0x130: DenseMap<Node*, SCC*>  (NodeToSCCMap)
           +0x130: qword - bucket count tracking
           +0x138: qword - bucket array pointer
           +0x140: dword - num entries
           +0x144: dword - num tombstones
           +0x148: dword - num buckets
  +0x150: BumpPtrAllocator
           +0x150: qword - current slab cursor
           +0x158: qword - current slab end
  +0x1A0: qword - total allocated bytes
  +0x1B0: SmallVector<SCC*> - SCC ownership list
           +0x1B0: qword - data pointer
           +0x1B8: dword - size
           +0x1BC: dword - capacity
  +0x240: SmallVector<RefSCC*> - RefSCC list (post-order)
```


## GPU-Specific Call Graph Properties

The LCG implementation itself is GPU-agnostic, but the call graph shape on GPU differs fundamentally from CPU:

**Kernels are roots.** Functions annotated with `nvvm.annotations` kernel metadata are externally visible entry points. They are the roots of the call graph -- nothing calls a kernel (launches are host-side). In CGSCC ordering, kernels are processed last (they are the top of the bottom-up traversal).

**Device functions are internal.** Non-kernel `__device__` functions are typically `internal` linkage. They appear in the call graph only as callees. This produces a characteristic tree-like (or DAG-like) call graph with very few cycles, meaning most SCCs contain a single function.

**Recursion is rare.** CUDA hardware historically did not support recursion (stack depth is bounded, and the compiler must statically allocate the call stack). Although modern architectures permit limited recursion, real-world CUDA code almost never uses it. This means SCC merging (`switchInternalEdgeToCall`) is rarely triggered -- most CGSCC processing is trivially single-function SCCs in a DAG.

**Aggressive inlining collapses the graph.** The NVIDIA inline budget (default 20,000, vs LLVM's 225) causes most device functions to be inlined into their callers. After the early inliner pass, the remaining call graph is typically flat: a handful of kernels with large bodies and very few un-inlined callees. Later CGSCC invocations mostly iterate over single-function SCCs.


## ThinLTO Interaction

When ThinLTO imports functions from other modules, they appear in the call graph as `available_externally` definitions. The LCG treats them like any other defined function -- they get nodes, their edges are lazily populated, and they participate in SCC computation. The NVModuleSummary builder (`sub_12E06D0`) records call graph edges in the module summary, which the ThinLTO import pass uses to decide which cross-module functions to import. Once imported, those functions become candidates for inlining during the CGSCC traversal.

The `function-inline-cost-multiplier` knob (visible in `sub_2613930`'s string table) penalizes recursive functions during ThinLTO inlining, since recursive inlining can explode code size without bound.


## Knobs and Thresholds

| Knob | Default | Effect |
|---|---|---|
| `inline-budget` | 20,000 | Per-caller NVIDIA inline cost budget (89x LLVM default) |
| `inline-threshold` | 225 | LLVM default cost threshold (used by New PM inliner) |
| `nv-inline-all` | off | Bypass cost analysis, force-inline everything |
| `-aggressive-inline` | -- | CLI flag, routes to `inline-budget=40000` |
| `intra-scc-cost-multiplier` | -- | Cost multiplier for inlining within the same SCC |
| `function-inline-cost-multiplier` | -- | Cost multiplier for recursive functions |
| `abort-on-max-devirt-iterations-reached` | false | Abort if devirt iteration limit is hit |
| `cgscc-inline-replay` | -- | Replay file for inline decisions (debugging) |
| `cgscc-inline-replay-scope` | `Function` | Replay scope: Function or Module |
| `cgscc-inline-replay-fallback` | `Original` | Fallback: Original, AlwaysInline, NeverInline |
| `cgscc-inline-replay-format` | `Line` | Replay format: Line, LineColumn, LineDiscriminator |
| CGSCC iteration count (arg 1 to `sub_1A62BF0`) | 1 (O1-O3), 5 (tier 3) | Max SCC re-visitation iterations after graph mutation |


## Sentinel Values and Constants

| Value | Meaning |
|---|---|
| `0xFFFFFFFFFFFFF000` | DenseMap empty bucket sentinel |
| `0xFFFFFFFFFFFFE000` | DenseMap tombstone sentinel |
| `0x100000000` | Packed `{size=0, cap=1}` for SmallVector initialization |
| `0x100000001` | Packed `{DFSNumber=1, LowLink=1}` for Tarjan root init |
| `0x400000000` | Packed `{size=0, cap=4}` for SmallVector initialization |
| `0x800000000` | Packed `{size=0, cap=8}` for SmallVector initialization |
| `0x88` (136) | SCC object size in bytes |
| `0x18` (24) | Tarjan StackEntry size (Node*, EdgeIter, EdgeEnd) |
| `0x10` (16) | Edge mutation pair size (Node*, Node*) |
| `0xFFFFFFFF` (-1) | DFSNumber value indicating "completed" / assigned to an SCC |


## Diagnostic Strings

The call graph printer at `sub_D2B640` (12,287 bytes) emits these strings for debugging:

```
"Printing the call graph for module:"
"RefSCC with"
"SCC with"
"Edges in function:"
"call SCCs:"
"call"
"ref"
" -> "
```

The DOT dumper at `sub_D29900` emits GraphViz format with `"digraph"`, `"[style=dashed"` (for ref edges), and standard `";\n"`, `"}\n"` terminators.

The New PM inliner at `sub_2613930` emits: `"function-inline-cost-multiplier"`, `"recursive"`, `"recursive SCC split"`, `"unavailable definition"`.

The devirtualization pass at `sub_2284BC0` emits: `"Max devirtualization iterations reached"`.

The old CGSCC inliner at `sub_186CA00` emits: `"inline"`, `"NoDefinition"`, `"NotInlined"`, `"AlwaysInline"`, `"Inlined"`, `"Callee"`, `"Caller"`, `"cost=always"`, `"cost="`, `"threshold="`.

The call graph DOT writer cluster at `0x2280000`--`0x228A000` emits: `"view-callgraph"`, `"View call graph"`, `"dot-callgraph"`, `"Print call graph to 'dot' file"`, `"Call graph: "`, `"external caller"`, `"external callee"`, `"external node"`, `"Writing '"`, `"error opening file for writing!"`.


## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| LazyCallGraph cluster start | `sub_D230A0` | -- | -- |
| `std::rotate` / SCC array reorder | `sub_D23910` | -- | -- |
| SCC array splitting helper | `sub_D23A60` | -- | -- |
| `Node::populate()` / edge iterator (lazy population point) | `sub_D23BF0` | -- | -- |
| `LazyCallGraph::lookupSCC(Node&)` | `sub_D23C40` | -- | -- |
| `RefSCC::isAncestorOf()` connectivity check | `sub_D23CB0` | -- | -- |
| `LazyCallGraph::notifyRefSCCChange()` | `sub_D23D60` | -- | -- |
| `Edge::setKind()` (flip call/ref tag bit) | `sub_D23E00` | -- | -- |
| SCC constructor | `sub_D23F30` | -- | -- |
| `LazyCallGraph::insertRefSCC()` | `sub_D248B0` | -- | -- |
| Node edge list cleanup | `sub_D24960` | -- | -- |
| DenseMap insert (Node-to-SCC) | `sub_D24C50` | -- | -- |
| `RefSCC::isPartOfRefSCC()` check | `sub_D24D10` | -- | -- |
| DenseMap clear (SCC internals) | `sub_D24EE0` | -- | -- |
| `RefSCC::find()` / updateSCCIndex | `sub_D25AF0` | -- | -- |
| `RefSCC::SCCIndexMap::find()` | `sub_D25BD0` | -- | -- |
| DenseMap grow/rehash | `sub_D25CB0` | -- | -- |
| `switchInternalEdgeToCall()` | `sub_D25FD0` | 5,526 | -- |
| `Node::setRefSCC()` | `sub_D27750` | -- | -- |
| `switchOutgoingEdgeToCall/Ref()` | `sub_D27A10` | 29,179 | -- |
| Call graph verification | `sub_D29180` | 6,417 | -- |
| DOT graph dumper | `sub_D29900` | 8,235 | -- |
| `insertInternalRefEdge()` | `sub_D2A080` | 15,253 | -- |
| `computeRefSCC()` | `sub_D2AD40` | 12,495 | -- |
| Call graph text printer | `sub_D2B640` | 12,287 | -- |
| `buildSCCs()` / initial construction | `sub_D2BEB0` | 9,782 | -- |
| `switchInternalEdgeToRef()` | `sub_D2C610` | 5,236 | -- |
| `mergeRefSCC()` | `sub_D2DA90` | 17,930 | -- |
| SCC iteration logic | `sub_D2E510` | 6,890 | -- |
| `rebuildSCC()` | `sub_D2F240` | 6,141 | -- |
| Post-order SCC traversal helper | `sub_D2F8A0` | 10,451 | -- |
| Post-order traversal | `sub_D30800` | 7,796 | -- |
| Edge management helper | `sub_D301A0` | 5,148 | -- |
| RefSCC-level operations | `sub_D31270` | 7,696 | -- |
| CGSCC pass manager / InlinerWrapper factory | `sub_1A62BF0` | -- | -- |
| NVIDIA custom inliner (old CGSCC) | `sub_1864060` | 75,000 | -- |
| `Inliner::inlineCallsImpl()` (CGSCC core loop) | `sub_186CA00` | 61,117 | -- |
| Call graph node visitor | `sub_2280510` | 24,000 | -- |
| Call graph builder | `sub_2282680` | 33,000 | -- |
| DevirtSCCRepeatedPass ("Max devirtualization iterations reached") | `sub_2284BC0` | 16,000 | -- |
| InlinerWrapper factory (nv-early-inliner, inliner-wrapper) | `sub_2342850` | -- | -- |
| CGSCC-to-function adaptor | `sub_2362FB0` | 6,700 | -- |
| CGSCC pipeline text parser | `sub_2377300` | 103,000 | -- |
| Attributor CGSCC pass | `sub_2582AC0` | 39,000 | -- |
| New PM CGSCC inliner | `sub_2613930` | 69,000 | -- |


## Cross-References

- [Inliner Cost Model](../lto/inliner-cost.md) -- the cost computation that the CGSCC inliner uses to decide whether to inline each call site
- [ThinLTO Function Import](../lto/thinlto-import.md) -- how cross-module functions are imported into the call graph
- [Pipeline & Ordering](../llvm/pipeline.md) -- where the four CGSCC invocations sit in the overall pass sequence
- [Optimization Levels](../config/optimization-levels.md) -- how CGSCC iteration counts vary by O-level and tier
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- DenseMap internals, sentinel values, and probing strategy used throughout the LCG
