# Memory Space Optimization

The Memory Space Optimization pass (`memory-space-opt`) is NVIDIA's inter-procedural address space resolution engine. Its job is to convert generic (flat) pointers into specific address spaces -- global, shared, local, constant, or parameter -- so that the backend can emit specialized memory instructions (`ld.shared`, `st.global`, etc.) instead of generic ones (`ld`, `st`) that require address translation hardware at runtime. On NVIDIA GPUs, generic memory accesses go through an address translation unit that adds latency; resolving pointer provenance at compile time eliminates this overhead entirely and is one of the most impactful optimizations in the CUDA compilation pipeline.

The pass is implemented as a multi-function cluster totaling roughly 250KB of decompiled code, with two cooperating systems: an intra-procedural address space resolver and an inter-procedural function cloning engine.

## Key Facts

| Property | Value |
|---|---|
| Pass name (pipeline) | `memory-space-opt` |
| Class | `MemorySpaceOptPass` |
| Pass type | Parameterized FunctionPass (NVIDIA-custom) |
| Registration | New PM #416, parameterized: `first-time;second-time;no-warnings;warnings` |
| Runtime positions | Tier 1/2/3 #65 (after DSE + DCE + LLVM standard pipeline); also runs early in "mid" path (see [Pipeline](../llvm/pipeline.md)) |
| Pass entry point | `sub_1C70910` (11 KB native; 2,427 lines decomp) |
| Pass factory | `sub_1C8E680` |
| NVVMPassOptions slot | Offset `+2680` (disable), offset `+3120` (mode parameter) |
| Binary size | ~250 KB total (multi-function cluster) |
| Upstream equivalent | None -- entirely NVIDIA-proprietary |

## NVPTX Address Space Numbering

The pass operates on the standard NVPTX address spaces (0=generic, 1=global, 3=shared, 4=constant, 5=local, 101=param). See [Address Spaces](../reference/address-spaces.md) for the complete table with hardware mapping, pointer widths, and aliasing rules.

Internally, the pass encodes address spaces as a single-bit bitmask for efficient dataflow computation (0x01=global, 0x02=shared, 0x04=constant, 0x08=local, 0x10=param, 0x0F=unknown). When multiple pointer sources contribute different spaces, the bitmask is OR'd together. A singleton bit (popcount == 1) means the space is fully resolved; multiple bits set means ambiguous. See the [MemorySpaceOpt Internal Bitmask](../reference/address-spaces.md#memoryspaceopt-internal-bitmask) section for the complete mapping and resolution algorithm.

## IR Before/After Example

The following illustrates the core transformation: generic-pointer loads/stores are resolved to specific address spaces, enabling specialized PTX memory instructions.

**Before** (generic pointers, AS 0):
```llvm
define void @kernel(ptr addrspace(0) %shared_buf, ptr addrspace(0) %global_out) {
  %val = load float, ptr addrspace(0) %shared_buf, align 4
  %add = fadd float %val, 1.0
  store float %add, ptr addrspace(0) %global_out, align 4
  %check = call i1 @llvm.nvvm.isspacep.shared(ptr %shared_buf)
  br i1 %check, label %fast, label %slow
fast:
  ret void
slow:
  ret void
}
```

**After** (resolved address spaces):
```llvm
define void @kernel(ptr addrspace(3) %shared_buf, ptr addrspace(1) %global_out) {
  %val = load float, ptr addrspace(3) %shared_buf, align 4    ; -> ld.shared.f32
  %add = fadd float %val, 1.0
  store float %add, ptr addrspace(1) %global_out, align 4     ; -> st.global.f32
  ; isspacep.shared folded to true (phase 2), branch simplified by later DCE
  br label %fast
fast:
  ret void
}
```

The `addrspacecast` instructions are inserted during resolution and consumed by downstream passes. The `isspacep` folding (phase 2 only) eliminates runtime address space checks when the space is statically known.

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

**Complexity.** Let F = number of functions in the module, A = total number of pointer-typed arguments across all functions, E = total call-graph edges, and I = total instructions. The intra-procedural use-def chain walk is O(I) per function (bounded by visited-set to avoid cycles through PHI nodes). The IP-MSP worklist iterates until no argument's bitmask changes; since each of the A arguments has a 5-bit bitmask that can only grow (OR of incoming values), the worklist converges in at most O(A) rounds. Each round re-analyzes at most O(F) functions, and adding callers back to the worklist costs O(E) in total across all rounds. Worst-case: O(A * (F * I_avg + E)) where I_avg is average instructions per function. Function cloning adds at most O(F) clones (bounded by `do-clone-for-ip-msp`), each clone being O(I_f) to create. In practice, GPU modules have small call graphs (F < 200 after inlining) and the worklist converges in 2--4 rounds, making the pass effectively O(F * I_avg + E).

The IP-MSP driver in `sub_1C70910` implements a fixed-point worklist algorithm that propagates address space information across function boundaries:

1. Build a worklist of all functions in the module. Debug: `"Initial work list size: %d"`.
2. Pop a function from the worklist.
3. Run intra-procedural resolution (phase 1 or 2).
4. If argument memory spaces changed (`"changed in argument memory space"`), add all callers back to the worklist (`"callees are affected"`).
5. If the return memory space is resolved (`"return memory space is resolved"`), propagate to callers.
6. Repeat until the worklist is empty.

A second IP-MSP implementation exists at `sub_1C6A6C0` (11 KB native), which appears to be the LIBNVVM/module-pass variant. It uses DenseMap-style hash tables (sentinel -8 for empty, -16 for tombstone), has explicit loop-induction analysis (`sub_1BF8310`), and runs three sub-phases: call-site collection (level controlled by `dword_4FBD1E0`, default 4), address space resolution (level `dword_4FBD2C0`, default 2), and a WMMA-specific pass (`sub_1C5FDC0`).

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

```c
/* Backward use-def chain walk: sub_1CA5350, 8776 bytes (~1641 lines).
 * Returns a 5-bit address-space bitmask:
 *   0x01=global, 0x02=shared, 0x04=constant, 0x08=local, 0x10=param.
 * popcount(mask)==1 means fully resolved; >1 means ambiguous (caller
 * decides based on `param-always-point-to-global`). */
uint8_t resolveAddressSpace(Value *root, AnalysisCtx *ctx) {
    SmallVector<Value*, 32> worklist = { root };
    DenseSet<Value*> visited;
    uint8_t mask = 0;

    while (!worklist.empty()) {
        Value *v = worklist.pop_back_val();
        if (!visited.insert(v).second) continue;  // PHI cycle guard

        switch (getOpcodeChar(v)) {              /* MSP node tag */
        case 'H': worklist.push_back(getPtrOperand(v));    break; /* GEP */
        case 'G': worklist.push_back(getCastSource(v));    break; /* bitcast */
        case 'O':                                                 /* PHI */
            for (unsigned i = 0; i < getNumIncoming(v); ++i)
                worklist.push_back(getIncomingValue(v, i));
            break;
        case '8': mask |= 0x08; break;           /* alloca -> local (AS 5)   */
        case 'M': mask |= classifyCallReturn(v); break; /* call ret-AS table */
        case 32:  /* Load */
            if (ctx->knobs.track_indir_load)
                worklist.push_back(getPtrOperand(v));
            else mask |= 0x0F;                   /* unknown -> all-non-param */
            break;
        case 47:  /* inttoptr */
            if (ctx->knobs.track_int2ptr)
                worklist.push_back(getCastSource(v));
            else mask |= 0x0F;
            break;
        case 48:  worklist.push_back(getCastSource(v)); break; /* ptrtoint */
        default:  mask |= classifyByAttribute(v, ctx); break;  /* arg/global */
        }
    }
    return mask ? mask : 0x01;   /* default to global on empty (unreachable) */
}
```

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
| Memory-space cloning engine | `sub_2CBBE90` | 71KB | Inter-procedural function cloning |
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

## Optimization Level Behavior

| Level | Phase 1 (first-time) | Phase 2 (second-time) | IP-MSP Cloning |
|-------|---------------------|----------------------|----------------|
| **O0** | Runs (mode 0) -- address space resolution is required for correct PTX emission | Not run | Not run |
| **Ofcmax** | Runs (mode 0); `LSA-Opt` forced to 0, limiting resolution depth | Not run | Not run |
| **Ofcmid** | Runs (mode 0) | Runs (mode 1) after IP-MSP propagation | Enabled (`do-clone-for-ip-msp=-1`) |
| **O1+** | Runs (mode 0) early in pipeline | Runs (mode 1) after IP-MSP propagation | Enabled; iterates to fixed point |

This pass is unusual in that it runs even at O0 -- address space resolution is a correctness requirement, not purely an optimization. Without it, all memory accesses would use generic (flat) addressing, which is functionally correct but significantly slower due to the address translation hardware penalty. At Ofcmax, the pass runs in a reduced mode with `LSA-Opt` disabled. See [Optimization Levels](../config/optimization-levels.md) for the complete pipeline structure.

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

## Multi-Pass Data Flow: MemorySpaceOpt / IP-MSP / Alias Analysis

The following diagram shows how three cooperating subsystems exchange data to resolve generic pointers into specific address spaces. The left column is MemorySpaceOpt (per-function), the center is IP-MSP (module-level), and the right is NVVM Alias Analysis (query service). Arrows show data produced (`-->`) and consumed (`<--`).

```
 MemorySpaceOpt (per-function)       IP-MSP (module-level)          NVVM Alias Analysis
 ==============================      ==========================      ======================

 1. EARLY RUN (mode 0)
 +----------------------------+
 | Use-def chain walker       |
 | (sub_1CA5350)              |
 | Walk: GEP, bitcast, PHI,  |
 | alloca, call returns       |
 |                            |
 | Produces:                  |
 |  - per-arg bitmask         |
 |    (0x01=global,0x02=shr,  |
 |     0x04=const,0x08=local, |
 |     0x10=param)            |
 |  - unresolved arg list     |
 +---+------------------------+
     |                                                              +----------------------+
     | per-arg bitmasks                                             | Address space         |
     | (singleton bit = resolved,                                   | disjointness table:  |
     |  multi-bit = ambiguous)                                      |                      |
     v                                                              | AS 1 vs AS 3: NoAlias|
 +---+------------------------+                                     | AS 1 vs AS 5: NoAlias|
 | addrspacecast insertion    |                                     | AS 3 vs AS 5: NoAlias|
 | (sub_1CA1B70)              |                                     | AS 0 vs any: MayAlias|
 | Rewrites loads/stores to   |                                     | (stateless, trivial) |
 | ld.shared / st.global etc. |                                     +----------+-----------+
 +---+------------------------+                                                |
     |                                                                         |
     | Resolved pointer types on                                               |
     | function args + return values                                           |
     v                                                                         |
 +---+-----------------------------+      +--------------------------+         |
 | Unresolved args remain generic  | ---> | IP-MSP worklist driver   |         |
 | Need cross-function evidence    |      | (sub_1C70910 / 2CBBE90)  |         |
 +---+-----------------------------+      |                          |         |
     ^                                    | For each function F:     |         |
     |                                    |  1. Collect all callers  |         |
     |                                    |  2. Intersect arg AS     |         |
     |                                    |     across call sites    |         |
     |                                    |  3. If unanimous:        |         |
     |                                    |     specialize or clone  |         |
     |  propagated arg spaces             |                          |         |
     |  (from callers)                    | Produces:                |         |
     +------------------------------------+  - cloned functions      |         |
                                          |    with AS-specific args |         |
                                          |  - updated call sites    |         |
                                          |  - "changed in argument  |         |
                                          |    memory space" events  |         |
                                          +---+----------------------+         |
                                              |                                |
 2. LATE RUN (mode 1)                         | Enriched module with           |
 +----------------------------+               | resolved pointer types          |
 | Hash-table resolver        |               v                                |
 | (sub_1CA9E90)              | <--- cloned functions re-enter worklist        |
 |                            |                                                |
 | Additional capabilities:   |      Each resolved addrspacecast               |
 |  - isspacep folding        |      feeds into...                             |
 |    (builtins 0xFD0-0xFD5) |                                                |
 |  - Dead cast elimination   |                                     +----------v-----------+
 |                            |                                     | NVVM AA (nvptx-aa)   |
 | Consumes:                  |                                     |                      |
 |  - IP-MSP propagated       |                                     | With resolved AS on  |
 |    address spaces          |                                     | pointers, queries    |
 |  - hash table of known     |                                     | return NoAlias for   |
 |    pointer->space mappings |                                     | cross-space pairs    |
 +---+------------------------+                                     |                      |
     |                                                              | Enables downstream:  |
     | Fully resolved IR                                            |  - GVN load forward  |
     | (minimal generic ptrs)                                       |  - DSE elimination   |
     v                                                              |  - LICM hoisting     |
 +---+------------------------+                                     |  - MemorySSA queries |
 | Downstream consumers:      |                                     +----------------------+
 |  - Instruction selection   |
 |    (ld.shared, st.global)  |
 |  - Backend PTX emission    |
 |  - Register allocation     |
 |    (no generic-ptr spills) |
 +----------------------------+
```

**Data flow summary:**

| Producer | Data | Consumer |
|----------|------|----------|
| MemorySpaceOpt phase 1 | Per-arg address space bitmask | IP-MSP worklist |
| IP-MSP worklist | Cloned functions with specialized arg types | MemorySpaceOpt phase 2 |
| IP-MSP worklist | Call-site rewriting (`addrspacecast` at boundaries) | All downstream passes |
| MemorySpaceOpt phase 2 | `isspacep` folded to `true`/`false` | Dead code elimination |
| Both phases | Resolved pointer address spaces on all IR values | NVVM AA (`nvptx-aa`) |
| NVVM AA | `NoAlias` for cross-space pointer pairs | GVN, DSE, LICM, MemorySSA |

The feedback loop between MemorySpaceOpt and IP-MSP is the critical insight: phase 1 resolves locally-obvious cases, IP-MSP propagates those resolutions across call boundaries (cloning when necessary), and phase 2 picks up the newly-available information to resolve cases that were previously ambiguous. The worklist iterates until no more argument spaces change, guaranteeing a fixed point. NVVM AA is the downstream beneficiary -- every resolved pointer pair that previously required a conservative `MayAlias` answer can now return `NoAlias`, enabling more aggressive optimization in GVN, DSE, LICM, and scheduling.

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building an equivalent address space resolution engine.

**1. Resolving ambiguous pointers to the wrong default space.** When the bitmask has multiple bits set (e.g., `0x03` = global OR shared), the pass defaults to global if `param-always-point-to-global` is true. A reimplementation that defaults to shared instead will silently produce `ld.shared` instructions for what is actually global memory, causing out-of-bounds accesses on the shared memory aperture. The correct behavior is: ambiguous always resolves to global (the safe conservative choice), never to a more restrictive space.

**2. Forgetting to re-run after inter-procedural propagation.** The pass must run twice: once before IP-MSP to resolve locally-obvious cases, and again after IP-MSP to consume propagated information. A single-pass reimplementation will miss every case where a callee's argument space is only known from the caller's context. The second run (mode 1) is not optional -- it catches the majority of inter-procedural resolutions and performs `isspacep` folding that the first run cannot do.

**3. Cloning functions with external linkage instead of specializing in-place.** The pass uses two strategies: in-place specialization for internal/private functions (all call sites visible) and clone-and-specialize for external/weak linkage. Reversing this logic -- cloning internal functions or modifying external ones -- either wastes compile time on unnecessary clones or breaks callers outside the module who still pass generic pointers. The linkage check (`0x4007` for internal) is the discriminator and must not be inverted.

**4. Failing to handle the `addrspacecast` chain correctly.** After resolving a pointer's space, the pass inserts `addrspacecast` from generic to the specific space and replaces all uses. A reimplementation that replaces the pointer type directly (without the cast) will break LLVM's type system invariants, causing assertion failures in downstream passes. The cast must exist in the IR even though it is semantically a no-op -- LLVM's type-based alias analysis and GEP arithmetic depend on it.

**5. Not iterating the IP-MSP worklist to a fixed point.** The worklist must iterate until no argument bitmask changes. A reimplementation that runs one pass over all functions and stops will miss transitive resolutions through call chains (A calls B calls C). The bitmask OR is monotone (can only grow), so convergence is guaranteed, but early termination produces incomplete resolutions that leave generic pointers in the IR and forfeit the performance benefit of specialized memory instructions.

## Test This

The following minimal kernel exercises address space resolution. Compile with `nvcc -ptx -arch=sm_90` and inspect the PTX output.

```cuda
__global__ void memspace_test(float *global_out, int n) {
    __shared__ float smem[64];
    smem[threadIdx.x] = (float)threadIdx.x;
    __syncthreads();
    float val = smem[threadIdx.x];
    global_out[threadIdx.x] = val + 1.0f;
}
```

**What to look for in PTX:**
- `ld.shared.f32` for the read from `smem` -- confirms the pass resolved the shared pointer from generic (AS 0) to shared (AS 3). If you see a plain `ld.f32` without the `.shared` qualifier, the access goes through the generic address translation unit at runtime.
- `st.global.f32` for the write to `global_out` -- confirms global pointer resolution (AS 1).
- Absence of `cvta.to.shared` / `cvta.to.global` instructions. These `cvta` (convert address) instructions indicate the backend is converting generic pointers at runtime instead of using resolved address spaces at compile time. Their absence means the pass succeeded fully.
- Compare with `-O0` to see the unresolved version where generic `ld`/`st` instructions dominate.

## Reimplementation Checklist

1. **Address space bitmask dataflow engine.** Implement the per-value bitmask lattice (0x01=global, 0x02=shared, 0x04=constant, 0x08=local, 0x10=param) with OR-based meet, use-def chain walking through GEP/bitcast/PHI/alloca/inttoptr, and a visited-set to handle cycles through PHI nodes.
2. **Two-phase resolution with mode dispatch.** Build a mode-parameterized entry point: mode 0 (conservative first-time), mode 1 (hash-table-based second-time with `isspacep` folding), and warning-suppression variants (modes 2/3).
3. **Inter-procedural fixed-point worklist (IP-MSP).** Implement the module-level worklist that propagates per-argument address space bitmasks across call boundaries, re-adding callers when an argument's bitmask changes, iterating until no bitmask grows.
4. **Function cloning for specialization.** Implement two strategies: in-place specialization for internal-linkage functions (modify arg types directly) and clone-and-specialize for external-linkage functions (create internal clone, rewrite call sites, insert `addrspacecast` at clone entry).
5. **`isspacep` intrinsic folding (phase 2).** When a pointer's address space is resolved, fold `isspacep.shared`/`.global`/etc. builtins (IDs 0xFD0--0xFD5) to `true` or `false` constants.
6. **Post-resolution cleanup.** Insert `addrspacecast` instructions, rewrite loads/stores to specific address spaces, eliminate dead cast chains (generic-to-shared followed by shared-to-generic), and rewrite call sites to target specialized clones.
7. **Illegal operation detection.** Check and warn on illegal address-space/operation combinations (atomics on constant/local, WMMA on constant/local, vector atomics on shared/local/constant) without aborting compilation.

## Pipeline Interaction

The pass runs at two points in the CICC pipeline: once early (first-time, mode 0) to resolve obvious cases before optimization, and again after inter-procedural propagation (second-time, mode 1) to catch cases that became resolvable after inlining and constant propagation. The no-warnings variants (modes 2/3) suppress repeated diagnostics on re-runs. The pass feeds directly into instruction selection, where resolved address spaces determine which PTX memory instructions are emitted. It also interacts with the `ipmsp` module pass, which drives the inter-procedural cloning engine separately from the per-function resolver.
