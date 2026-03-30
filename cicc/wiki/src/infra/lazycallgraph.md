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
| SCC object size | 136 bytes (`0x88`) |
| Edge encoding | Pointer with tag bits: bit 2 = call edge, bit 2 clear = ref edge |
| DenseMap hash | `hash(ptr) = (ptr >> 4) ^ (ptr >> 9)`, bucket size = 16 bytes |
| DenseMap sentinels | Empty = `0xFFFFFFFFFFFFF000`, Tombstone = `0xFFFFFFFFFFFFE000` |
| CGSCC invocations per O1/O2/O3 | 4 passes of `sub_1A62BF0(1,...)`, 1 iteration each |
| CGSCC invocations at tier 3 | `sub_1A62BF0(5,...)` -- 5 iterations |


## Lazy Call Graph Construction

The graph is not built all at once. When the CGSCC pass manager begins, the LCG starts with just the module's externally visible functions and kernel entry points as root nodes. Each node's edges are populated only when first visited by the SCC traversal -- the `Node::populateSlow()` method (`sub_D23BF0` returns the edge iterator range) scans all instructions in the function, recording two kinds of edges:

**Call edges** (bit 2 set in pointer tag): direct `CallBase` instructions whose callee resolves to a defined function. These form the strong connectivity that defines SCCs.

**Ref edges** (bit 2 clear): any other reference to a defined function -- a function pointer stored in a global, passed as a callback argument, taken address of. These contribute to RefSCC grouping but do not create call-graph cycles.

```
Node layout (deduced from binary):
  +0x00: Function*          (LLVM IR function)
  +0x08: Edge array pointer  (populated lazily)
  +0x10: Edge count
  +0x14: DFSNumber           (Tarjan state, -1 = completed)
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
  +0x40: Flags byte          (bit 0 = active map selector for double-buffering)
  +0x44: Tombstone count
  +0x48: DenseMap #2         (alternate map for lazy rehashing)
```

The bottom-up SCC ordering is computed using Tarjan's algorithm, implemented in `sub_D2C610`. The algorithm uses the standard DFS stack with 24-byte entries (`{Node*, EdgeIter, EdgeEnd}`) and the classic `DFSNumber` / `LowLink` fields at node offsets `+0x10` and `+0x14`. When `LowLink == DFSNumber`, the node is an SCC root -- all nodes above it on the DFS result stack are popped into a new SCC, their DFSNumber set to `-1` (completed), and the SCC index written into the LowLink field for reuse.

The Tarjan inner loop at `0xD2CD90`--`0xD2CEA4` and the SCC member popping at `0xD2CF61`--`0xD2CFD0` are both 4x unrolled, indicating these are hot paths in the CGSCC pipeline.


## CGSCC Pass Manager: Bottom-Up Interprocedural Optimization

The CGSCC pass manager (`sub_1A62BF0`) wraps the LCG traversal and runs a pipeline of CGSCC passes over each SCC in bottom-up (post-order) order. In the O1/O2/O3 pipeline, it is invoked four times at different points in the optimization sequence, each with 1 iteration:

```
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #2 (inliner framework, early)
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #17 (after DSE/GVN/MemCpyOpt)
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #21 (after ADCE/JumpThreading)
sub_1A62BF0(1,0,0,1,0,0,1)  -- pass #38 (late, after Sink)
```

At higher tier levels (tier 3 aggressive optimization), a 5-iteration variant appears: `sub_1A62BF0(5,0,0,1,0,0,1)`. The first parameter controls the maximum number of SCC re-visitation iterations when the call graph is mutated during optimization.

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

The CGSCC-to-function adaptor (`sub_2362FB0`) allows running function-level passes inside the CGSCC framework. This is how the inliner triggers function-level optimization of a newly inlined callee before proceeding to the caller.


## Incremental SCC Mutation

When a pass modifies the call graph, the SCC structure must be updated without recomputing the entire graph. Two functions handle this:

**`sub_D25FD0` -- `switchInternalEdgeToCall()`** (5,526 bytes): Called when the inliner or devirtualization pass reveals a new direct call (previously an indirect call or ref edge). If the new call edge creates a cycle between previously separate SCCs within the same RefSCC, those SCCs must merge. The function uses DFS-based reachability to identify affected SCCs, then merges them into a single SCC. The merge callback (`function_ref<void(ArrayRef<SCC*>)>`) notifies the CGSCC pass manager to re-queue the merged SCC for re-optimization.

**`sub_D2C610` -- `switchInternalEdgeToRef()`** (5,236 bytes): Called when an edge is demoted from call to ref (e.g., a direct call is deleted during optimization). This may break an SCC into multiple smaller SCCs. The function runs Tarjan's algorithm on just the affected SCC to recompute its internal SCC structure. New SCC objects (136 bytes each) are allocated from the LCG's BumpPtrAllocator at `[LCG+0x150]`.

Both functions use a double-buffered DenseMap scheme (the flags byte at `RefSCC+0x40` alternates between two maps) for incremental rehashing. When edges are moved between SCCs, old map entries are tombstoned and new entries inserted into the alternate map, avoiding full rehash.


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


## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_D230A0` | -- | LazyCallGraph cluster start |
| `sub_D23910` | -- | `std::rotate` / SCC array reorder |
| `sub_D23A60` | -- | SCC array splitting helper |
| `sub_D23BF0` | -- | `Node::populate()` / edge iterator (lazy population point) |
| `sub_D23C40` | -- | `LazyCallGraph::lookupSCC(Node&)` |
| `sub_D23CB0` | -- | `RefSCC::isAncestorOf()` connectivity check |
| `sub_D23D60` | -- | `LazyCallGraph::notifyRefSCCChange()` |
| `sub_D23E00` | -- | `Edge::setKind()` (flip call/ref tag bit) |
| `sub_D23F30` | -- | SCC constructor |
| `sub_D248B0` | -- | `LazyCallGraph::insertRefSCC()` |
| `sub_D24960` | -- | Node edge list cleanup |
| `sub_D24C50` | -- | DenseMap insert (Node-to-SCC) |
| `sub_D24D10` | -- | `RefSCC::isPartOfRefSCC()` check |
| `sub_D24EE0` | -- | DenseMap clear (SCC internals) |
| `sub_D25AF0` | -- | `RefSCC::find()` / updateSCCIndex |
| `sub_D25BD0` | -- | `RefSCC::SCCIndexMap::find()` |
| `sub_D25CB0` | -- | DenseMap grow/rehash |
| `sub_D25FD0` | 5,526 | `switchInternalEdgeToCall()` |
| `sub_D27750` | -- | `Node::setRefSCC()` |
| `sub_D27A10` | 29,179 | `switchOutgoingEdgeToCall/Ref()` |
| `sub_D29180` | 6,417 | Call graph verification |
| `sub_D29900` | 8,235 | DOT graph dumper |
| `sub_D2A080` | 15,253 | `insertInternalRefEdge()` |
| `sub_D2AD40` | 12,495 | `computeRefSCC()` |
| `sub_D2B640` | 12,287 | Call graph text printer |
| `sub_D2BEB0` | 9,782 | `buildSCCs()` / initial construction |
| `sub_D2C610` | 5,236 | `switchInternalEdgeToRef()` |
| `sub_D2DA90` | 17,930 | `mergeRefSCC()` |
| `sub_D2E510` | 6,890 | SCC iteration logic |
| `sub_D2F240` | 6,141 | `rebuildSCC()` |
| `sub_D2F8A0` | 10,451 | Post-order SCC traversal helper |
| `sub_D30800` | 7,796 | Post-order traversal |
| `sub_D301A0` | 5,148 | Edge management helper |
| `sub_D31270` | 7,696 | RefSCC-level operations |
| `sub_1A62BF0` | -- | CGSCC pass manager / InlinerWrapper factory |
| `sub_1864060` | 75,000 | NVIDIA custom inliner (old CGSCC) |
| `sub_186CA00` | -- | `Inliner::inlineCallsImpl()` (CGSCC core loop) |
| `sub_2342850` | -- | InlinerWrapper factory (nv-early-inliner, inliner-wrapper) |
| `sub_2362FB0` | 6,700 | CGSCC-to-function adaptor |
| `sub_2377300` | 103,000 | CGSCC pipeline text parser |
| `sub_2582AC0` | 39,000 | Attributor CGSCC pass |
| `sub_2613930` | 69,000 | New PM CGSCC inliner |


## Cross-References

- [Inliner Cost Model](../lto/inliner-cost.md) -- the cost computation that the CGSCC inliner uses to decide whether to inline each call site
- [ThinLTO Function Import](../lto/thinlto-import.md) -- how cross-module functions are imported into the call graph
- [Pipeline & Ordering](../llvm/pipeline.md) -- where the four CGSCC invocations sit in the overall pass sequence
- [Optimization Levels](../config/optimization-levels.md) -- how CGSCC iteration counts vary by O-level and tier
