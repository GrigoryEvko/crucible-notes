# ipmsp -- Inter-Procedural Memory Space Propagation

The IPMSP pass resolves generic (address space 0) pointer arguments to concrete NVIDIA address spaces by analyzing call sites across the entire module. When all callers of a function agree that a pointer argument points to a specific memory space (global, shared, local, constant), the pass either specializes the function in place or clones it with narrowed pointer types. This enables downstream passes to emit space-specific load/store instructions (e.g., `ld.shared` instead of generic `ld`) and eliminates `addrspacecast` overhead.

| | |
|---|---|
| **Pass name** | `ipmsp` |
| **Class** | `llvm::IPMSPPass` |
| **Scope** | Module pass |
| **Registration** | New PM slot 125, line 1111 in `sub_2342890` |
| **Main function** | `sub_1C6A6C0` (54 KB) |
| **Cloning engine** | `sub_2CBBE90` (71 KB) |
| **Address space inference** | `sub_2CE96D0` |

## NVPTX Address Spaces

The NVPTX target uses five address spaces:

| AS | Name | PTX memory | Typical source |
|---|---|---|---|
| 0 | Generic | generic | Unresolved pointers (default) |
| 1 | Global | `.global` | `__device__`, `cudaMalloc` |
| 3 | Shared | `.shared` | `__shared__` |
| 4 | Constant | `.const` | `__constant__` |
| 5 | Local | `.local` | Stack allocations (`alloca`) |

Generic pointers require a runtime address space check on every access. Resolving them statically eliminates this overhead.

## Algorithm

The pass operates as a worklist-driven inter-procedural analysis with three strategies.

### Phase 1: Build Worklist

The pass iterates all functions in the module. A function enters the worklist if `sub_2CBA650` returns true, meaning:

- The function is not a declaration or `available_externally`
- Its linkage is not `extern_weak` or `common`
- It is not an intrinsic (`sub_B2DDD0` filter)
- It has at least one formal argument that is a generic pointer not yet in the resolved-space map

A reverse call graph is also constructed: for each callee, the pass records which callers invoke it.

Debug output (when `dump-ip-msp` is enabled): `"Initial work list size : N"`

### Phase 2: Per-Function Analysis

For each function popped from the worklist:

1. **Classify arguments**: allocate a per-arg array initialized to 1000 ("unresolved"). Non-pointer args and already-resolved args are marked 2000 ("skip").

2. **Walk call sites**: for each call instruction, examine each actual argument:
   - If the actual's address space is non-zero (already specific), record it.
   - If the actual is generic (AS 0), invoke the dataflow inference engine `sub_2CE96D0` to trace the pointer's provenance through GEPs, bitcasts, PHI nodes, and loads.
   - If this is the first call site for this arg, record the space. If a subsequent call site disagrees, mark 2000 ("conflicting -- give up").

3. **Count resolved arguments**: any arg where all call sites agree on a single address space is a candidate for specialization.

Debug output: `"funcname : changed in argument memory space (N arguments)"`

### Phase 3: Specialization Decision

The pass chooses between two strategies based on linkage:

| Linkage | Strategy | Mechanism |
|---|---|---|
| Internal / Private (7, 8) | **In-place specialization** | Modify the function's arg types directly. No clone needed since all callers are visible. |
| External / Linkonce / Weak | **Clone** | Create a new function with specialized arg types and internal linkage. Rewrite matching call sites to target the clone. Keep the original for external callers. |

The clone is created by `sub_F4BFF0` (CloneFunction):
- Builds a new `FunctionType` with specific-space pointer arg types
- Allocates a new Function object (136 bytes via `sub_BD2DA0`)
- Copies the body via a ValueMap-based cloner (`sub_F4BB00`)
- For each specialized arg, inserts an `addrspacecast` from specific back to generic at the clone's entry (these fold away in later optimization)
- Sets clone linkage to internal (`0x4007`)

Debug output: `"funcname is cloned"`

### Phase 4: Transitive Propagation

After specializing a function, the pass propagates resolved spaces to its callees via `sub_2CF5840`. Affected callees are pushed back onto the worklist. This enables bottom-up resolution through call chains: if `A -> B -> C`, specializing `A`'s args may resolve `B`'s args, which in turn resolves `C`'s args.

Debug output: `"N callees are affected"`

### Phase 5: Return Space Resolution

After argument processing, the pass checks return values:
- If the function returns a generic pointer, walk all `ret` instructions.
- Follow the def chain through GEPs to the base pointer.
- If all returns agree on a single address space, record it in the return-space map and propagate to callers.

Debug output: `"funcname : return memory space is resolved : N"`

## Handling Recursion and Clone Limits

- **Transitive**: clones are pushed back onto the worklist, so chains `A->B->C` are handled iteratively.
- **Mutual recursion**: already-resolved args are detected via the map (marked 2000), preventing infinite re-processing.
- **Self-recursion**: after the first pass resolves args, re-processing finds agreement and applies specialization.
- **Clone limit**: `do-clone-for-ip-msp` (default -1 = unlimited) caps the total number of clones. Each clone increments a counter at `this[200]`.

## The LIBNVVM Variant

A second implementation at `sub_1C6A6C0` (54 KB) serves the LIBNVVM/module-pass path. Key differences:

- Uses DenseMap-style hash tables (empty sentinel = -8, tombstone = -16)
- Includes loop-induction analysis via `sub_1BF8310` with `maxLoopInd` tracking
- Three processing phases controlled by globals:
  - Phase A (`dword_4FBD1E0`, default=4): call-site collection, threshold `dword_4FBC300` = 500
  - Phase B (`dword_4FBD2C0`, default=2): address space resolution
  - Phase C (`dword_4FBCD80`, default=2): WMMA-specific sub-pass via `sub_1C5FDC0`

## Knobs

| Knob | Default | Description |
|---|---|---|
| `dump-ip-msp` | 0 | Enable debug tracing (stored at `qword_5013548`) |
| `do-clone-for-ip-msp` | -1 (unlimited) | Max clones allowed (`qword_5013468`) |
| `process-alloca-always` | true | Treat alloca-derived pointers as local (AS 5) unconditionally |
| `wmma-memory-space-opt` | true | Specialize WMMA call args to shared memory (AS 3) |
| `strong-global-assumptions` | true | Assume constant buffer pointers always point to globals |
| `param-always-point-to-global` | true | Parameter pointers always point to globals (`unk_4FBE1ED`) |
| `track-indir-load` | true | Track indirect loads during inference |
| `track-int2ptr` | true | Track `inttoptr` in inference |
| `mem-space-alg` | 2 | Algorithm selection for address space optimization |
| `process-builtin-assume` | -- | Process `__builtin_assume(__is*(p))` for space deduction |

## Relationship to memory-space-opt

The `ipmsp` and `memory-space-opt` passes are complementary:

- **`ipmsp`** is inter-procedural: it analyzes call graphs and specializes function signatures.
- **`memory-space-opt`** is intra-procedural: it resolves generic pointers within a single function body using dataflow analysis.

The typical flow is: `ipmsp` runs first to propagate address spaces across function boundaries, then `memory-space-opt` runs (parameterized with `first-time` / `second-time`) to resolve remaining generic pointers within each function.

## Data Structures

The cloning engine uses red-black trees (`std::map`) for four separate maps:

| Map | Key | Value | Purpose |
|---|---|---|---|
| Return-space | `Function*` | Resolved AS | Return value address space |
| Arg-space | `Value*` | Resolved AS | Per-argument address space |
| Callee-space | `Value*` | Resolved AS | Callee pointer spaces |
| Callee-info | `Function*` | Sub-tree | Reverse call graph (which callers invoke this callee) |

The worklist is a `std::deque<Function*>` with 512-byte pages (64 pointers per page).

Red-black tree nodes are 0x58 bytes with the standard `{left, right, parent, color, key}` layout at offsets 16, 24, 8, 0, 32.
