# Memory Space Optimization

The Memory Space Optimization pass (`memory-space-opt`) is NVIDIA's inter-procedural address space resolution engine. Its job is to convert generic (flat) pointers into specific address spaces -- global, shared, local, constant, or parameter -- so that the backend can emit specialized memory instructions (`ld.shared`, `st.global`, etc.) instead of generic ones (`ld`, `st`) that require address translation hardware at runtime. On NVIDIA GPUs, generic memory accesses go through an address translation unit that adds latency; resolving pointer provenance at compile time eliminates this overhead entirely and is one of the most impactful optimizations in the CUDA compilation pipeline.

The pass is implemented as a multi-function cluster totaling roughly 250KB of decompiled code, with two cooperating systems: an intra-procedural address space resolver and an inter-procedural function cloning engine.

## NVPTX Address Space Numbering

CICC uses the standard NVPTX/LLVM address space convention:

| LLVM AS | Name | Description |
|---------|------|-------------|
| 0 | Generic (flat) | Unresolved -- the pointer could target any space |
| 1 | Global | Device DRAM, accessible by all threads |
| 3 | Shared | Per-block on-chip SRAM (`__shared__`) |
| 4 | Constant | Read-only memory in the constant cache |
| 5 | Local | Per-thread stack-private memory |
| 101 | Param | Kernel parameter window |

Internally, the pass encodes these as a bitmask for efficient dataflow computation:

| Bit | Value | Maps to AS |
|-----|-------|------------|
| 0 | 0x01 | Global (AS 1) |
| 1 | 0x02 | Shared (AS 3) |
| 2 | 0x04 | Constant (AS 4) |
| 3 | 0x08 | Local (AS 5) |
| 4 | 0x10 | Param (AS 101) |
| 0-3 | 0x0F | Unknown (union of all non-param spaces) |

When multiple pointer sources contribute different spaces, the bitmask is OR'd together. A singleton bit (popcount == 1) means the space is fully resolved; multiple bits set means ambiguous.

## Two-Phase Architecture

The pass entry point (`sub_1C70910`) accepts a mode parameter controlling execution:

| Mode | Name | Behavior |
|------|------|----------|
| 0 | First-time | Conservative resolution via `sub_1CA2920`. Called early in the pipeline. |
| 1 | Second-time | Hash-table-based resolution via `sub_1CA9E90`. Called after IP-MSP propagation. |
| 2 | First-time, no warnings | Same as mode 0 but suppresses "Cannot tell what pointer points to" messages. |
| 3 | Second-time, no warnings | Same as mode 1 but silent. Used on re-runs where repeated warnings would be noise. |

Both phases share the same instruction dispatch structure, handling loads (opcode `0x36`), stores (`0x37`), calls (`0x4E`), atomic loads (`0x3A`), and atomic stores (`0x3B`).

**Phase 1 (first-time)** resolves obvious cases where pointer origin is statically known. It uses `sub_1C9F820` for dataflow analysis and `sub_1C98370` for annotation-based resolution.

**Phase 2 (second-time)** runs after inter-procedural propagation has enriched the analysis context. It uses hash-table lookups (`sub_1CA8350`) and can fold `isspacep` intrinsics (builtins `0xFD0`-`0xFD5`) to constants when the address space is already known, eliminating runtime space checks.

## Inter-Procedural Memory Space Propagation (IP-MSP)

The IP-MSP driver in `sub_1C70910` implements a fixed-point worklist algorithm that propagates address space information across function boundaries:

1. Build a worklist of all functions in the module. Debug: `"Initial work list size: %d"`.
2. Pop a function from the worklist.
3. Run intra-procedural resolution (phase 1 or 2).
4. If argument memory spaces changed (`"changed in argument memory space"`), add all callers back to the worklist (`"callees are affected"`).
5. If the return memory space is resolved (`"return memory space is resolved"`), propagate to callers.
6. Repeat until the worklist is empty.

A second IP-MSP implementation exists at `sub_1C6A6C0` (54KB), which appears to be the LIBNVVM/module-pass variant. It uses DenseMap-style hash tables (sentinel -8 for empty, -16 for tombstone), has explicit loop-induction analysis (`sub_1BF8310`), and runs three sub-phases: call-site collection (level controlled by `dword_4FBD1E0`, default 4), address space resolution (level `dword_4FBD2C0`, default 2), and a WMMA-specific pass (`sub_1C5FDC0`).

## Function Cloning for Specialization

When different call sites pass pointers from different address spaces to the same function argument, the pass clones the function so that each clone can be specialized for a single address space. This is the key mechanism that eliminates generic pointers at call boundaries.

The cloning engine (`sub_2CBBE90`, 71KB) uses two distinct strategies based on function linkage:

**Strategy 1 -- In-place specialization** (internal/private linkage): All call sites are visible within the module, so the function is modified directly. Pointer argument types are changed from generic (AS 0) to the resolved specific space. No clone is created. This is the cheaper path.

**Strategy 2 -- Clone and specialize** (external/linkonce/weak linkage): The function might have callers outside the module, so the original must be preserved. A clone is created with internal linkage (`0x4007`), its argument types are specialized, and internal call sites are rewritten to target the clone. The original remains for any remaining generic-pointer callers.

The cloning process (`sub_F4BFF0`):
1. Iterate all formal args of the original function.
2. For each arg whose address space was resolved, create a new function type with the specific address space.
3. Allocate a new `Function` object via `sub_BD2DA0(136)`.
4. Copy linkage, attributes, and calling convention.
5. Clone the body via `sub_F4BB00` (ValueMap-based cloner).
6. For specialized args, insert `addrspacecast` instructions at the clone's entry.
7. Rewrite matching call sites via `sub_BD84D0`.

After cloning, the clone is pushed back onto the worklist, enabling recursive specialization through call chains: if A calls B calls C, each level's arguments resolve bottom-up as the worklist iterates.

## Intra-Procedural Resolution Algorithm

### Use-Def Chain Walking (`sub_1CA5350`)

The core resolver walks backward through use-def chains to find the original allocation a pointer derives from:

| IR Node | Behavior |
|---------|----------|
| GEP (`H`) | Transparent -- follow pointer operand |
| Bitcast (`G`) | Transparent -- follow source operand |
| PHI (`O`) | Follow all incoming values (adds all to worklist) |
| Call (`M`) | Check if returns a known-space pointer |
| Load (subcode 32) | Tracked if `track-indir-load` is enabled |
| inttoptr (subcode 47) | Tracked if `track-int2ptr` is enabled |
| ptrtoint (subcode 48) | Transparent |
| Alloca (`8`) | Resolves to local (AS 5) |

The walker uses a worklist with a visited bitset to handle cycles through phi nodes. It collects three separate vectors: loads (indirect pointers), GEPs, and calls returning pointers.

### Resolution Decision

Once the bitmask is computed:
- **Single bit set**: resolved. Insert `addrspacecast` to the target space.
- **Multiple bits set**: ambiguous. If `param-always-point-to-global` is true and the param bit is set, resolve to global. Otherwise emit a warning and default to global.
- **Zero bits**: unreachable or error.

### Address Space Inference Engine (`sub_2CE96D0`)

For generic-pointer arguments at call sites, the inference engine creates a 608-byte analysis context on the stack, sets up six independent tracking sets, and calls `sub_2CE8530` for deep dataflow analysis tracing pointer provenance through GEPs, bitcasts, PHI nodes, and loads from known-space pointers.

## Post-Resolution Optimizations

After resolving a pointer's address space, the pass performs several follow-up transformations:

- **addrspacecast insertion**: `sub_1CA1B70` (first-time) / `sub_1CA28F0` (second-time) inserts a cast from generic to the resolved space and replaces all uses of the generic pointer.
- **Instruction rewriting**: Loads and stores on generic pointers are rewritten to use the specific space, enabling the backend to emit `ld.shared`, `st.global`, etc.
- **isspacep folding** (second-time only): If a pointer's space is known, `isspacep.shared(%p)` folds to `true` or `false`.
- **Dead cast elimination**: Redundant `addrspacecast` chains (e.g., generic-to-shared followed by shared-to-generic) are simplified.
- **Call site specialization**: After cloning, call sites are rewritten to call the specialized version with casted arguments.

## Error Handling for Illegal Operations

The pass detects and reports illegal address-space/operation combinations as soft warnings (compilation continues):

| Operation | Illegal Space | Warning Message |
|-----------|---------------|-----------------|
| Atomic load/store | Constant | `"Cannot do atomic operation on const memory"` |
| Atomic load/store | Local | `"Cannot do atomic on local memory"` |
| WMMA | Constant | `"Cannot do WMMA on constant memory"` |
| WMMA | Local | `"Cannot do WMMA on local memory"` |
| Vector atomic | Shared | `"Cannot to vector atomic on shared memory"` |
| Vector atomic | Local | `"Cannot to vector atomic on local memory"` |
| Vector atomic | Constant | `"Cannot to vector atomic on const memory"` |

Note: The vector atomic messages contain a typo in NVIDIA's source -- `"Cannot to"` should read `"Cannot do"`. This typo is present in all three vector atomic warning strings.

## Key Functions

| Function | Address | Size | Role |
|----------|---------|------|------|
| Pass entry / IP-MSP driver | `sub_1C70910` | 2427 lines | Main entry point, worklist iteration, mode dispatch |
| First-time resolver | `sub_1CA2920` | 1119 lines | Conservative address space resolution |
| Second-time resolver | `sub_1CA9E90` | 933 lines | Hash-table-based resolution with isspacep folding |
| Use-def chain walker | `sub_1CA5350` | 1641 lines | Backward pointer origin tracking |
| Per-BB scanner | `sub_1CA8CD0` | 898 lines | Instruction scan, bitmask builder |
| Pass initialization | `sub_1CAB590` | 1040 lines | Global registration, data structure setup |
| MemorySpaceCloning engine | `sub_2CBBE90` | 71KB | Inter-procedural function cloning |
| IPMSPPass variant | `sub_1C6A6C0` | 54KB | LIBNVVM module-pass variant |
| Address space inference | `sub_2CE96D0` | -- | Dataflow analysis for single argument |
| CloneFunction | `sub_F4BFF0` | -- | Full function clone with type rewriting |
| shouldProcessFunction | `sub_2CBA650` | -- | Multi-condition filter for worklist eligibility |
| hasUnresolvedPointerArgs | `sub_2CBA520` | -- | Checks if any arg is an unresolved generic pointer |
| replaceAllUsesWith | `sub_BD84D0` | -- | Rewrites call sites to target the clone |
| propagateSpacesToCallees | `sub_2CF5840` | -- | Propagates resolved spaces through call graph |

## Alternate Algorithm

A parallel implementation exists at `sub_2CBBE90` / `sub_2CEAC10` / `sub_2CF2C20`, selected when `mem-space-alg != 2`. The default algorithm (value 2) is the one documented above; the alternate may be a simpler or older version optimized for different patterns.

## Configuration Knobs

### Primary Knobs (ctor_264 / ctor_267_0)

| Knob | Global | Type | Default | Description |
|------|--------|------|---------|-------------|
| `dump-ip-msp` | `dword_4FBD480` | bool | false | Dump inter-procedural memory space propagation debug info |
| `do-clone-for-ip-msp` | `dword_4FBD3A0` | int | -1 | Max number of clones (-1 = unlimited). Set to 0 to disable cloning. |
| `param-always-point-to-global` | `unk_4FBE1ED` | bool | true | Assume kernel parameters always point to global memory |
| `dump-ir-before-memory-space-opt` | `byte_4FBE000` | bool | false | Dump IR before the pass runs |
| `dump-ir-after-memory-space-opt` | `byte_4FBDF20` | bool | false | Dump IR after the pass completes |
| `track-indir-load` | `byte_4FBDE40` | bool | true | Track pointers loaded from memory during use-def walking |
| `mem-space-alg` | `dword_4FBDD60` | int | 2 | Algorithm selection for address space optimization |
| `track-int2ptr` | `byte_4FBDC80` | bool | true | Track `inttoptr` casts during analysis |

### Additional Knobs (ctor_267_0 / ctor_531_0)

| Knob | Default | Description |
|------|---------|-------------|
| `process-alloca-always` | true | Treat `alloca` instructions as definite local (AS 5) regardless of context |
| `wmma-memory-space-opt` | true | Enable memory space optimization for WMMA operations |
| `strong-global-assumptions` | true | Assume const buffer pointers always point to globals |
| `process-builtin-assume` | -- | Process `__builtin_assume(__is*(p))` assertions for space deduction |

### IP-MSP Pass Knobs (ctor_528)

| Knob | Global | Default | Description |
|------|--------|---------|-------------|
| `dump-ip-msp` | `qword_5013548` | 0 | Debug tracing for IPMSP variant |
| `do-clone-for-ip-msp` | `qword_5013468` | -1 | Clone limit for IPMSP variant |

## Diagnostic Strings

```
"Initial work list size: %d"
"changed in argument memory space"
"is cloned"
"avoid cloning of"
"callees are affected"
"return memory space is resolved"
"Cannot tell what pointer points to, assuming global memory space"
"Cannot do atomic operation on const memory"
"Cannot do atomic on local memory"
"Cannot do WMMA on constant memory"
"Cannot do WMMA on local memory"
"Cannot to vector atomic on shared memory"
"Cannot to vector atomic on local memory"
"Cannot to vector atomic on const memory"
```

## Pipeline Interaction

The pass runs at two points in the CICC pipeline: once early (first-time, mode 0) to resolve obvious cases before optimization, and again after inter-procedural propagation (second-time, mode 1) to catch cases that became resolvable after inlining and constant propagation. The no-warnings variants (modes 2/3) suppress repeated diagnostics on re-runs. The pass feeds directly into instruction selection, where resolved address spaces determine which PTX memory instructions are emitted. It also interacts with the `ipmsp` module pass, which drives the inter-procedural cloning engine separately from the per-function resolver.
