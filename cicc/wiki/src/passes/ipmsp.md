# ipmsp — Inter-Procedural Memory Space Propagation

The IPMSP pass resolves generic (address space 0) pointer arguments to concrete NVIDIA address spaces by analyzing call sites across the entire module. When all callers of a function agree that a pointer argument points to a specific memory space (global, shared, local, constant), the pass either specializes the function in place or clones it with narrowed pointer types. This enables downstream passes to emit space-specific load/store instructions (e.g., `ld.shared` instead of generic `ld`) and eliminates `addrspacecast` overhead.

Disabling this pass (`-disable-MemorySpaceOptPass`) causes 2--20x performance regressions on real workloads. The pass is automatically disabled in OptiX IR mode (`--emit-optix-ir` routes `-do-ip-msp=0`).

| | |
|---|---|
| **Pass name** | `ipmsp` |
| **Class** | `llvm::IPMSPPass` |
| **Scope** | Module pass |
| **Registration** | New PM slot 125, line 1111 in `sub_2342890` |
| **Main function** | `sub_2CBBE90` (11 KB native) — memory-space cloning worklist driver |
| **LIBNVVM variant** | `sub_1C6A6C0` (11 KB native) |
| **Inference engine** | `sub_2CE96D0` -> `sub_2CE8530` |
| **Cloning engine** | `sub_F4BFF0` (CloneFunction) |
| **Callee matching** | `sub_2CE7410` |
| **Propagation** | `sub_2CF5840` -> `sub_2CF51E0` |
| **Pipeline control** | `do-ip-msp` NVVMPassOption (default: enabled) |

## NVPTX Address Spaces

The pass resolves generic (AS 0) pointers to specific address spaces: global (AS 1), shared (AS 3), constant (AS 4), local (AS 5), or param (AS 101). Generic pointers require a runtime address space check on every access; resolving them statically eliminates this overhead. See [Address Spaces](../reference/address-spaces.md) for the complete table with hardware mapping, pointer widths, aliasing rules, and the MemorySpaceOpt bitmask encoding.

## Algorithm Overview

The pass operates as a worklist-driven inter-procedural fixed-point analysis. The top-level loop:

```c
function IPMSP_Run(Module M):
    worklist = deque<Function*>{}
    argSpaceMap = map<Value*, int>{}        // formal arg -> resolved AS
    returnSpaceMap = map<Function*, int>{}  // function -> return AS
    calleeInfoMap = map<Function*, set<Function*>>{}  // reverse call graph

    // Phase 1: seed
    for each F in M.functions():
        if shouldProcess(F):
            worklist.push_back(F)
        for each caller of F:
            calleeInfoMap[F].insert(caller)

    debug("Initial work list size : %d", worklist.size())

    // Phase 2: fixed-point iteration
    while worklist not empty:
        F = worklist.pop_back()

        // Analyze and specialize F's callee arguments
        changed = analyzeAndSpecialize(F, argSpaceMap, calleeInfoMap)

        if changed:
            // Propagate to F's callees
            propagateSpacesToCallees(F, argSpaceMap)
            for each callee C of F in calleeInfoMap:
                if shouldProcess(C):
                    worklist.push_back(C)
            debug("%d callees are affected")

        // Check return space
        if resolveReturnSpace(F, returnSpaceMap):
            debug("%s : return memory space is resolved : %d")
            // propagate to callers and push them onto worklist
```

### Phase 1: Build Worklist

The pass iterates all functions in the module. A function enters the worklist if `sub_2CBA650` returns true, meaning:

- The function is not a declaration or `available_externally`
- Its linkage is not `extern_weak` or `common`
- It is not an intrinsic (`sub_B2DDD0` filter)
- It has at least one formal argument that is a generic pointer not yet in the resolved-space map

Specifically, `sub_2CBA650` checks:

```c
function shouldProcess(this, F):
    if F has no users (F[16] == 0): return false

    linkage = F.linkage & 0xF
    if (linkage + 14) & 0xF <= 3: return false   // available_externally, appending
    if (linkage + 7) & 0xF <= 1: return false     // common, extern_weak

    if isIntrinsic(F): return false

    retType = F.getReturnType()
    if retType is pointer with AS 0 and not in returnSpaceMap:
        return true

    return hasUnresolvedPointerArgs(this, F)
```

`sub_2CBA520` (`hasUnresolvedPointerArgs`) walks the formal arg list (stride 40 bytes) and returns true if any arg has type byte 14 (pointer) and is not already in the arg-space map.

A reverse call graph is also constructed: for each callee, the pass records which callers invoke it.

Debug output (when `dump-ip-msp` is enabled): `"Initial work list size : N"`

### Phase 2: Per-Function Analysis

For each function popped from the worklist:

1. **Classify arguments**: allocate a per-arg array initialized to 1000 ("unresolved"). Non-pointer args and already-resolved args are marked 2000 ("skip").

2. **Walk call sites**: for each call instruction, examine each actual argument:
   - If the actual's address space is non-zero (already specific), record it.
   - If the actual is generic (AS 0), first check the callee-space map for a cached result. If not found, invoke the dataflow inference engine `sub_2CE96D0` to trace the pointer's provenance.
   - If this is the first call site for this arg, record the space. If a subsequent call site disagrees, mark 2000 ("conflicting — give up").

3. **Count resolved arguments**: any arg where all call sites agree on a single address space is a candidate for specialization.

```c
function analyzeArgSpaces(F, argSpaceMap, calleeSpaceMap):
    numArgs = F.arg_size()
    spaces[numArgs] = {1000, ...}     // 1000 = unresolved

    for i in 0..numArgs:
        arg = F.getArg(i)
        if arg.type != pointer:
            spaces[i] = 2000          // not a pointer, skip
        else if arg in argSpaceMap:
            spaces[i] = 2000          // already resolved

    for each CallInst CI using F:
        calledFn = CI.getCalledFunction()
        for i in 0..numArgs:
            if spaces[i] == 2000: continue
            actual = CI.getOperand(i)
            if actual == F.getArg(i): continue  // passthrough

            as = actual.type.addrspace
            if as == 0:
                // Check cache first
                if actual in calleeSpaceMap:
                    as = calleeSpaceMap[actual]
                else:
                    ok = inferAddressSpace(calledFn, actual, &as, ...)
                    if !ok:
                        spaces[i] = 2000
                        continue

            if spaces[i] == 1000:
                spaces[i] = as         // first call site
            else if spaces[i] != as:
                spaces[i] = 2000       // conflict

    return count(s for s in spaces if s != 1000 and s != 2000)
```

Debug output: `"funcname : changed in argument memory space (N arguments)"`

### Phase 3: Specialization Decision

The pass chooses between two strategies based on linkage:

| Linkage | Strategy | Mechanism |
|---|---|---|
| Internal / Private (7, 8) | **In-place specialization** | Modify the function's arg types directly. No clone needed since all callers are visible. |
| External / Linkonce / Weak | **Clone** | Create a new function with specialized arg types and internal linkage. Rewrite matching call sites to target the clone. Keep the original for external callers. |

The decision at line 1114 in `sub_2CBBE90`:

```c
if (F.linkage & 0xF) - 7 <= 1:
    // Internal/Private: specialize in place
    for each resolved arg:
        argSpaceMap[arg] = resolvedAS
else:
    // External: must clone
    if resultsTree is empty:
        debug("avoid cloning of %s")
    else:
        createClone(F, resolvedArgs)
```

The clone is created by `sub_F4BFF0` (CloneFunction):
- Builds a new `FunctionType` with specific-space pointer arg types
- Allocates a new Function object (136 bytes via `sub_BD2DA0`)
- Copies the body via a ValueMap-based cloner (`sub_F4BB00`)
- For each specialized arg, inserts an `addrspacecast` from specific back to generic at the clone's entry (these fold away in later optimization)
- Sets clone linkage to internal (`0x4007`)

Debug output: `"funcname is cloned"`

### Phase 4: Transitive Propagation

After specializing a function, the pass propagates resolved spaces to its callees via `sub_2CF5840`. This function:

1. Creates an analysis context similar to `sub_2CE96D0`
2. Calls `sub_2CF51E0` which walks F's body
3. For each call instruction in F that targets a known function, determines if the called function's args now have resolved spaces
4. Updates the arg-space map accordingly

Affected callees are pushed back onto the worklist. This enables bottom-up resolution through call chains: if `A -> B -> C`, specializing `A`'s args may resolve `B`'s args, which in turn resolves `C`'s args.

Debug output: `"N callees are affected"`

### Phase 5: Return Space Resolution

After argument processing, the pass checks return values:
- If the function returns a generic pointer, walk all `ret` instructions.
- Follow the def chain through GEPs to the base pointer.
- If all returns agree on a single address space, record it in the return-space map and propagate to callers.

Debug output: `"funcname : return memory space is resolved : N"`

## The Dataflow Inference Engine

The inference engine is the core analysis that determines what address space a generic pointer actually points to. It is invoked when a call-site argument has address space 0 (generic) and the pass needs to determine the concrete space.

### Entry Point: `sub_2CE96D0`

```c
function inferAddressSpace(calledFn, actualArg, &result, module, symtab, argSpaceMap):
    as = actualArg.type.addrspace
    if as != 0:
        *result = as
        return true                    // trivially resolved

    // Generic pointer: need full analysis
    context = alloca(608)              // 608-byte stack context
    // Initialize 6 tracking sets:
    //   [0]  visited set (bitset for cycle detection in PHI chains)
    //   [1]  user-list collector
    //   [2]  callee mapping
    //   [3]  load tracking (when track-indir-load)
    //   [4]  inttoptr tracking (when track-int2ptr)
    //   [5]  alloca tracking

    return coreDataflowWalker(context, calledFn, actualArg,
                              &loadsVec, &callsVec, result)
```

The 608-byte context is allocated on the stack and contains all working state for the backward dataflow walk.

### Core Backward Dataflow Walker: `sub_2CE8530`

The walker traces the pointer's provenance backward through the SSA def chain. It uses a worklist plus visited-set to handle cycles (primarily PHI nodes).

**IR nodes handled:**

| IR node | Action |
|---|---|
| `getelementptr` | Transparent: follow the base pointer operand |
| `bitcast` | Transparent: follow the source operand |
| `addrspacecast` | Extract target address space, record it |
| `phi` | Add all incoming values to the worklist |
| `select` | Add both arms to the worklist (result = OR of both) |
| `call` / `invoke` | Look up callee in return-space map; if found, use that |
| `load` | If `track-indir-load` enabled: follow the loaded pointer; otherwise opaque |
| `inttoptr` | If `track-int2ptr` enabled: follow the integer source; otherwise opaque |
| `alloca` | If `process-alloca-always`: immediately resolve to AS 5 (local) |
| `argument` | If in arg-space map: use the recorded space |

**Inference rules (lattice):**

The engine collects candidate address spaces from all reachable definitions. The resolution follows these rules:

```c
// All sources agree:     resolved to that space
// Sources disagree:      unresolvable (return false)
// param bit set + param-always-point-to-global:  resolve to global (AS 1)
// alloca found + process-alloca-always:  resolve to local (AS 5)
// __builtin_assume(__isGlobal(p)) + process-builtin-assume:  resolve to global
```

The walker collects three separate vectors during traversal:
- **loads**: pointers loaded from memory (indirect provenance)
- **GEPs**: `getelementptr` instructions encountered along the chain
- **calls**: function calls whose return values contribute to the pointer

### Per-Callee Space Propagation: `sub_2CE8CB0`

This function is the heavy-weight driver called from the worklist loop for each function. It processes a function's call graph entries and determines concrete address spaces for callees by examining actual arguments at all call sites.

**Architecture:**

1. A global limit at `qword_3CE3528` caps maximum analysis depth to prevent explosion on large call graphs.

2. The function iterates the BB instruction list (offset +328, linked list). For each callee encountered:
   - Check visited set. The set has two representations:
     - Small set: flat array at object offsets +32..+52 (checked when flag at +52 is set)
     - Large set: hash-based DenseSet at offset +24 (checked via `sub_18363E0`)
   - If callee has no body (`*(_DWORD *)(callee + 120) == 0`): collect it as a leaf and record its argument address spaces via `sub_2CE80A0`
   - Otherwise: skip (will be processed when popped from worklist)

3. For each collected callee, a DenseMap cache at offset +160 is checked:
   - Hash function: `(ptr >> 9) ^ (ptr >> 4)`, linear probing
   - Empty sentinel: `-4096` (`0xFFFFFFFFFFFFF000`)
   - If found in cache: skip re-analysis (use cached result)

4. After collecting all callees: invoke `sub_2CE88B0` for merge/commit.

5. For single-entry results (exactly 1 callee entry in the vector): special fast path via `sub_2CE2F10` that commits directly through a vtable dispatch.

```c
function perCalleePropagate(this, F):
    if this.firstVisit:
        // Reset tracking vectors
        clearUserVectors()

    // Walk BB instruction list
    for each BB in F.body():
        if BB in visitedSet: continue
        if BB.isDeclaration(): continue

        collectCalleeInfo(BB)       // -> sub_2CE80A0
        addToVisitedSet(BB)

    // Check depth limit
    if userVector.size() > depthLimit:
        return false

    // Merge phase
    if userVector.size() > 1:
        return mergeAndCommit(this, F)    // sub_2CE88B0
    elif userVector.size() == 1:
        commitSingleResult(this)          // fast path
    return false
```

### Callee Matching Engine: `sub_2CE7410`

When multiple call instructions target the same callee, this function determines the best pair to use for space inference. This is critical for correctness — the pass must ensure that the inferred space is valid for all uses.

**Algorithm:**

1. **Parallel operand walk**: for each pair of call instructions to the same callee, walk their operand use-chains in parallel. Compare the instructions at each position via the instruction equivalence DenseMap at offset +80.

2. **Coverage scoring**: count the number of matching operands (variable `v95`). Higher coverage means more confidence in the match.

3. **Dominance check**: call `sub_2403DE0(A, B)` to test if BB A dominates BB B. Both directions are checked:
   - If A dominates B and B dominates A (same BB or trivial loop): strong match.
   - If only one direction: check if the non-dominating one is the entry BB's first instruction.

4. **Loop membership gate**: `sub_24B89F0` checks whether both call instructions are in the same loop. If both are in the same loop and the coverage score > 1, the match is accepted even without strict dominance (loops create natural fixed-point convergence).

5. **Attribute check**: for each matched pair, `sub_245A9B0` verifies metadata flags (at instruction offset +44) to ensure the transformation is legal.

6. **Output**: the best-scoring pair is written into the results vector for subsequent instruction rewriting.

### Post-Inference Merge: `sub_2CE88B0`

After the per-callee analysis produces a list of `(instruction, resolved_space)` entries:

```c
function mergeAndCommit(this, F):
    entries = this.resultVector
    if entries.size() > 1:
        qsort(entries, comparator=sub_2CE2BD0)  // sort by callee ID

    changed = false
    while entries.size() > 1:
        entry = entries.back()
        calleeId = entry.calleeId

        // Find best match for this callee
        matchScore = sub_2CE7410(this, calleeId, ...)

        if matchScore > 0:
            // Commit via instruction specialization
            sub_2CE4830(this, matchedCallee)     // edge weight
            sub_2CE3B60(this, bestMatchIdx)      // commit space

            // Propagate to other entries sharing this callee
            for each other entry with same callee:
                if other != bestMatch:
                    sub_2CE3780(this, other.users, matchedCallee)

            // Compact the entries vector
            changed = true
        else:
            // No match: fallback propagation
            sub_2CE3A70(this, calleeId, ...)

    return changed
```

### Instruction Specialization: `sub_2CE8120`

Once a callee's address space is determined, this function creates a specialized copy of the instruction:

1. **Legality check**: vtable dispatch at offset +408 (`sub_25AE460` default). Returns false if the instruction cannot be legally specialized (e.g., volatile operations, intrinsics with fixed types).

2. **Create specialized instruction**: `sub_244CA00` creates a new instruction with the modified pointer type (generic -> specific address space).

3. **Insert into BB**: `sub_24056C0` places the new instruction in the basic block's instruction list.

4. **Rewrite use chain**: all uses of the old instruction are updated to reference the new specialized version.

5. **Update DenseMap caches**:
   - Instruction-to-space map at offset +80: insert mapping from new instruction to resolved space
   - Edge count at offset +72: update via `sub_24D8EE0`
   - If nested clone tracking (offset +131 flag): update debug info via `sub_2D2DBE0`

## Handling Recursion and Clone Limits

- **Transitive**: clones are pushed back onto the worklist, so chains `A->B->C` are handled iteratively.
- **Mutual recursion**: already-resolved args are detected via the map (marked 2000), preventing infinite re-processing.
- **Self-recursion**: after the first pass resolves args, re-processing finds agreement and applies specialization.
- **Clone limit**: `do-clone-for-ip-msp` (default -1 = unlimited) caps the total number of clones. Each clone increments a counter at `this[200]`. When the limit is exceeded, cloning stops but in-place specialization continues for internal functions.
- **Analysis depth limit**: `qword_3CE3528` limits the per-function callee analysis depth to prevent explosion on large modules.

## The LIBNVVM Variant

A second implementation at `sub_1C6A6C0` (11 KB native) serves the LIBNVVM/module-pass path. Key differences:

- Uses DenseMap-style hash tables (empty sentinel = -8, tombstone = -16, 16-byte entries)
- Includes loop-induction analysis via `sub_1BF8310` with `maxLoopInd` tracking (debug: `"phi maxLoopInd = N: Function name"`)
- Three processing phases controlled by globals:
  - Phase A (`dword_4FBD1E0`, default=4): call-site collection, threshold `dword_4FBC300` = 500
  - Phase B (`dword_4FBD2C0`, default=2): address space resolution. If `dword_4FBCAE0` (special mode), picks the callee with the smallest constant value (minimum address space ID).
  - Phase C (`dword_4FBCD80`, default=2): WMMA-specific sub-pass via `sub_1C5FDC0`, called with `wmma_mode=1` first (WMMA-specific), then `wmma_mode=0`
- Threshold: `v302 > 5` triggers `sub_1C67780` for deeper analysis
- Pre/post analysis toggle: `byte_4FBC840` controls calls to `sub_1C5A4D0`

## Interaction with memory-space-opt

The `ipmsp` and `memory-space-opt` passes are complementary:

- **`ipmsp`** is inter-procedural: it analyzes call graphs, infers address spaces across function boundaries, and specializes function signatures via cloning.
- **`memory-space-opt`** is intra-procedural: it resolves generic pointers within a single function body using backward dataflow analysis and bitmask accumulation.

The typical pipeline flow:

1. `ipmsp` runs first (module pass) to propagate address spaces across function boundaries
2. `memory-space-opt` runs with `first-time` mode to resolve obvious intra-procedural cases
3. Further optimization passes run (may create new generic pointers via inlining, SROA, etc.)
4. `memory-space-opt` runs with `second-time` mode to clean up remaining generic pointers, fold `isspacep` intrinsics to constants

Both passes share the same set of knobs (with `ias-` prefixed mirrors for the IAS variant). The inference engine `sub_2CE96D0` is shared between IPMSP and the alternate algorithm selected by `mem-space-alg`.

## Knobs

### IPMSP-Specific Knobs

| Knob | Default | Storage | Description |
|---|---|---|---|
| `dump-ip-msp` | 0 | `qword_5013548` | Enable debug tracing |
| `do-clone-for-ip-msp` | -1 (unlimited) | `qword_5013468` | Max clones allowed |
| `do-ip-msp` | 1 (enabled) | NVVMPassOption | Enable/disable the entire pass |

### Shared Inference Knobs (MemorySpaceOpt variant)

| Knob | Default | Storage | Description |
|---|---|---|---|
| `param-always-point-to-global` | true | `unk_4FBE1ED` | Parameter pointers always resolve to global (AS 1) |
| `strong-global-assumptions` | true | (adjacent) | Assume constant buffer pointers always point to globals |
| `process-alloca-always` | true | `unk_4FBE4A0` | Treat alloca-derived pointers as local (AS 5) unconditionally |
| `wmma-memory-space-opt` | true | `unk_4FBE3C0` | Specialize WMMA call args to shared memory (AS 3) |
| `track-indir-load` | true | `byte_4FBDE40` | Track indirect loads during inference |
| `track-int2ptr` | true | `byte_4FBDC80` | Track `inttoptr` in inference |
| `mem-space-alg` | 2 | `dword_4FBDD60` | Algorithm selection for address space optimization |
| `process-builtin-assume` | — | (ctor_531_0) | Process `__builtin_assume(__is*(p))` for space deduction |

### IAS Variant Knobs (IPMSPPass path, ctor_610)

Each shared knob has an `ias-` prefixed mirror that controls the InferAddressSpaces-based code path (`sub_2CBBE90`):

| Knob | Mirrors |
|---|---|
| `ias-param-always-point-to-global` | `param-always-point-to-global` |
| `ias-strong-global-assumptions` | `strong-global-assumptions` |
| `ias-wmma-memory-space-opt` | `wmma-memory-space-opt` |
| `ias-track-indir-load` | `track-indir-load` |
| `ias-track-int2ptr` | `track-int2ptr` |

The unprefixed versions control the LIBNVVM variant (`sub_1C6A6C0`). The `ias-` prefixed versions control the New PM / IAS variant (`sub_2CBBE90`).

### LIBNVVM Variant Globals

| Global | Default | Description |
|---|---|---|
| `dword_4FBD1E0` | 4 | Phase A call-site collection level |
| `dword_4FBD2C0` | 2 | Phase B resolution level |
| `dword_4FBCD80` | 2 | Phase C WMMA sub-pass level |
| `dword_4FBC300` | 500 | Max analysis depth threshold |
| `dword_4FBCAE0` | — | Special minimum-selection mode |
| `byte_4FBC840` | — | Pre/post analysis toggle |
| `dword_4FBD020` | — | Debug: maxLoopInd dump |

### Debug Dump Knobs

| Knob | Description |
|---|---|
| `dump-ir-before-memory-space-opt` | Dump IR before MemorySpaceOpt runs |
| `dump-ir-after-memory-space-opt` | Dump IR after MemorySpaceOpt completes |
| `dump-process-builtin-assume` | Dump __builtin_assume processing |
| `msp-for-wmma` | Enable Memory Space Optimization for WMMA (tensor core) |

## Data Structures

### Worklist

The worklist is a `std::deque<Function*>` with 512-byte pages (64 pointers per page). Push-back via `sub_2CBB610` (extends the deque when the current page is full). Pop-back from the last page.

### Red-Black Tree Maps

The cloning engine uses red-black trees (`std::map`) for four separate maps:

| Map | Key | Value | Purpose |
|---|---|---|---|
| Return-space | `Function*` | Resolved AS | Return value address space |
| Arg-space | `Value*` | Resolved AS | Per-argument address space |
| Callee-space | `Value*` | Resolved AS | Callee pointer spaces (cached inference results) |
| Callee-info | `Function*` | Sub-tree | Reverse call graph (which callers invoke this callee) |

Red-black tree nodes are 0x58 bytes with the standard `{left, right, parent, color, key}` layout at offsets 16, 24, 8, 0, 32.

### DenseMap Caches

The inference engine and per-callee propagation use DenseMap hash tables with LLVM-layer sentinels (-4096 / -8192) and 16-byte entries (key + value). Growth is handled by `sub_240C8E0`. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing, and growth policy.

Three independent DenseMaps are used:
1. Offset +80: instruction -> resolved space (per-function analysis cache)
2. Offset +160: callee -> inference result (cross-function cache)
3. Offset +232: edge weight tracking (call graph weights for profitability)

### Visited Sets

Two representations depending on set size:

- **Small set** (flag at offset +52): flat array at offsets +32..+44, capacity at +40, count at +44. Linear scan for membership test.
- **Large set** (default): hash-based DenseSet at offset +24 via `sub_18363E0` for insert and `sub_18363E0` for membership test.

### Inference Context

The 608-byte stack-allocated context for `sub_2CE8530` contains:

| Offset range | Content |
|---|---|
| 0--23 | Result vector (pointer, size, capacity) |
| 24--47 | Loads vector (indirect pointer sources) |
| 48--71 | GEPs vector (getelementptr chains) |
| 72--95 | Calls vector (call instructions returning pointers) |
| 96--127 | Worklist for PHI traversal |
| 128--607 | Visited bitset, callee tracking, metadata |

## Sentinel Values

| Value | Meaning | Used in |
|---|---|---|
| 1000 | Unresolved pointer argument (not yet seen at any call site) | Per-arg analysis array |
| 2000 | Non-pointer, already resolved, or conflicting (skip) | Per-arg analysis array |
| -4096 | DenseMap empty slot | All DenseMap caches |
| -8192 | DenseMap tombstone (deleted entry) | All DenseMap caches |

## Diagnostic Messages

| Message | Source | Condition |
|---|---|---|
| `"Initial work list size : %d"` | `sub_2CBBE90` | Always (when `dump-ip-msp`) |
| `"funcname : changed in argument memory space (N arguments)"` | `sub_2CBBE90` | Args resolved |
| `"funcname is cloned"` | `sub_2CBBE90` | Clone created |
| `"avoid cloning of funcname"` | `sub_2CBBE90` | External linkage, empty results |
| `"N callees are affected"` | `sub_2CBBE90` | After propagation |
| `"funcname : return memory space is resolved : N"` | `sub_2CBBE90` | Return space resolved |
| `"phi maxLoopInd = N: Function name"` | `sub_1C6A6C0` | LIBNVVM loop-ind analysis |

## Function Map

Role labels below are descriptive (not symbol strings); only `sub_<HEX>` addresses are binary-confirmed.

| Address | Size | Role |
|---|---|---|
| `sub_2CBBE90` | 71 KB | Memory-space cloning worklist driver (New PM variant) |
| `sub_1C6A6C0` | 54 KB | IPMSPPass (LIBNVVM variant) |
| `sub_2CE96D0` | — | Address-space inference entry point |
| `sub_2CE8530` | — | Core dataflow walker (backward analysis) |
| `sub_2CE8CB0` | — | Per-callee space propagation |
| `sub_2CE88B0` | — | Post-inference merge (qsort) |
| `sub_2CE85D0` | — | Instruction rewriting for matched callee pairs |
| `sub_2CE7410` | — | Callee matching engine: dominance + coverage scoring |
| `sub_2CE80A0` | — | Append inference result to vector |
| `sub_2CE7E60` | — | Grow inference result vector |
| `sub_2CE4830` | — | Call graph edge weight computation |
| `sub_2CE3B60` | — | Commit resolved space to callee |
| `sub_2CE3A70` | — | Fallback propagation for unmatched entries |
| `sub_2CE3780` | — | Propagate to alternate callee users |
| `sub_2CE2F10` | — | Single-callee commit via vtable |
| `sub_2CE2DE0` | — | Single-predecessor property check |
| `sub_2CE2BD0` | — | qsort comparator: callee entry comparison |
| `sub_2CE2A70` | — | Merge small-vector pairs |
| `sub_2CE27A0` | — | Extract address space from Value's type |
| `sub_2CE8120` | — | Clone instruction + DenseMap update |
| `sub_2CE97F0` | — | Build per-arg user list |
| `sub_2CF5840` | — | Post-specialization space propagation |
| `sub_2CF51E0` | — | Body walker for propagation |
| `sub_2CBA650` | — | Worklist eligibility predicate |
| `sub_2CBA520` | — | Check for unresolved generic pointer args |
| `sub_F4BFF0` | — | Full function clone with arg rewriting |
| `sub_F4BB00` | — | ValueMap-based body cloner |
| `sub_BD84D0` | — | Redirect call sites to clone (RAUW) |
| `sub_2CBB230` | — | Red-black tree insert |
| `sub_2CBB490` | — | Red-black tree search |
| `sub_2CBB610` | — | Worklist deque push_back / grow |
| `sub_245A9B0` | — | Attribute flag membership test |
| `sub_245AA10` | — | Test instruction equivalence |
| `sub_2403DE0` | — | BasicBlock dominance test |
| `sub_24B89F0` | — | Check if two instructions share a loop |
| `sub_244CA00` | — | Create instruction with modified types |
| `sub_24056C0` | — | Insert instruction into BasicBlock |
| `sub_2D2DBE0` | — | Debug info update for cloned instruction |

## Cross-References

- [memory-space-opt](./memory-space-opt.md) — intra-procedural complement
- [reference/address-spaces](../reference/address-spaces.md) — consolidated AS reference
- [config/knobs](../config/knobs.md) — complete knob inventory
- [pipeline/optimizer](../pipeline/optimizer.md) — pipeline position and `do-ip-msp` option
- [pipeline/optix-ir](../pipeline/optix-ir.md) — OptiX disables IPMSP
- [infra/alias-analysis](../infra/alias-analysis.md) — cross-space NoAlias rules
