# Address Spaces

This page is the single source of truth for NVPTX address space numbering, hardware mapping, pointer widths, aliasing rules, and the internal bitmask encoding used by MemorySpaceOpt. It supersedes all inline address space tables elsewhere in the wiki -- those pages should cross-reference this one rather than maintaining their own copies.

NVPTX defines eight address spaces in cicc v13.0, six of which correspond to physically disjoint hardware memory partitions. The generic (flat) address space is a virtual overlay resolved at runtime by the GPU's address translation unit. The eighth, tensor memory (AS 6), is a Blackwell-era addition accessible only through TMA intrinsics. A ninth, AS 25, is used internally within NVVM IR for device-linkage annotations and never reaches PTX emission. A tenth, AS 53, appears in MemorySpaceOpt initialization as an internal annotation space for global variable tracking.


## Master Address Space Table

| LLVM AS | Name | PTX Qualifier | Hardware | Pointer Width | Typical Latency | CUDA Qualifier |
|---------|------|---------------|----------|---------------|-----------------|----------------|
| 0 | Generic (flat) | `.generic` | Virtual -- address translation unit maps to physical space at runtime | 64-bit | +4-8 cycles over resolved (translation overhead) | Default for unresolved pointers |
| 1 | Global | `.global` | Device DRAM, L2 cached, optionally L1 cached | 64-bit | 200-800 cycles (DRAM); 32-128 cycles (L2 hit) | `__device__`, `cudaMalloc` |
| 3 | Shared | `.shared` | Per-CTA on-chip scratchpad SRAM (48-228 KB per SM) | 32-bit (when `p3:32:32:32` active) or 64-bit | 20-30 cycles (bank-conflict-free) | `__shared__` |
| 4 | Constant | `.const` | Read-only constant cache (64 KB per SM) | 64-bit | 4-8 cycles (cache hit); DRAM latency on miss | `__constant__` |
| 5 | Local | `.local` | Per-thread private stack in DRAM, L1 cached | 32-bit (effective) or 64-bit | Same as global (backed by DRAM) | Stack allocations (`alloca`) |
| 6 | Tensor Memory | N/A (TMA intrinsics only) | Blackwell tensor memory (SM 100+) | 64-bit | Varies (TMA pipeline) | N/A -- accessed via `cp.async.bulk` intrinsics |
| 7 | Shared Cluster | `.shared::cluster` | Distributed shared memory across CTAs in a cluster (SM 90+) | 32-bit or 64-bit | ~30-50 cycles (cross-CTA penalty over AS 3) | `__shared__` with cluster scope |
| 25 | Internal device linkage | N/A | Not a physical memory -- NVVM IR annotation for `__device__` linkage | N/A | N/A | Used internally by module summary for extern device resolution |
| 53 | Internal annotation | N/A | Not a physical memory -- used by MemorySpaceOpt for global tracking | N/A | N/A | Internal to cicc pipeline |
| 101 | Param | `.param` | Kernel parameter window (mapped into constant bank or global memory) | 64-bit | 4-8 cycles (constant cache path) | Kernel parameters (`__global__` function args) |

Address space 2 is **not used** by NVPTX. The numbering gap between shared (3) and constant (4) is inherited from upstream LLVM NVPTX conventions. The NVVM verifier's valid-AS check uses the formula `(AS + ~2) & 0xFFFFFF) > 2`, which accepts AS values 0, 1, and 3 unconditionally; AS 2 is sometimes valid depending on context.


## Aliasing Rules

The core property exploited by NVVM AA is hardware address space disjointness: pointers in different non-generic address spaces can never reference the same byte. NVVM AA (`nvptx-aa`) encodes this as a NoAlias rule for every cross-space pointer pair, with the following exceptions.

| Pointer A | Pointer B | Alias Result | Reason |
|-----------|-----------|-------------|--------|
| AS 0 (generic) | Any | MayAlias | Generic can map to any physical space at runtime |
| AS X (same) | AS X (same) | MayAlias | Same space -- further analysis needed (BasicAA, TBAA) |
| AS 1 (global) | AS 101 (param) | MayAlias | `cvta.param` on SM 70+ makes param addressable as global |
| AS 3 (shared) | AS 7 (shared cluster) | MayAlias | Cluster shared memory overlaps with regular shared |
| Any other cross-space pair | | **NoAlias** | Physically disjoint hardware memory partitions |

The NVVM AA algorithm (pseudocode from `NVPTXAAResult::alias` in cicc):

```
AliasResult alias(Loc1, Loc2):
    AS1 = getAddressSpace(Loc1.Ptr, TraverseLimit)  // walk through casts
    AS2 = getAddressSpace(Loc2.Ptr, TraverseLimit)

    if AS1 == 0 or AS2 == 0:         return MayAlias  // generic kills precision
    if (AS1==3 and AS2==7) or (AS1==7 and AS2==3): return MayAlias
    if AS1 == AS2:                    return MayAlias  // same space, need deeper AA
    return NoAlias                                     // different non-generic spaces
```

The `getAddressSpace` helper walks backward through `getUnderlyingObject` (stripping GEPs, bitcasts, PHIs) up to `nvptx-traverse-address-aliasing-limit` (default 6) levels deep, resolving generic pointers that were produced by `addrspacecast` from a specific space.

### ModRef Rules

| Address Space | ModRef Mask | Meaning |
|---------------|-------------|---------|
| AS 4 (constant) | `NoModRef` | Read-only -- never modified |
| AS 101 (param) | `NoModRef` | Kernel params are read-only from device code |
| All others | `ModRef` | May be both read and written |

These masks enable DSE to skip constant/param stores entirely, and LICM to hoist loads from constant memory without checking for intervening stores.


## MemorySpaceOpt Internal Bitmask

MemorySpaceOpt (`sub_1C70910`) encodes address spaces as single-bit positions in a byte-wide bitmask for efficient dataflow computation. The mapping is performed in `sub_1CA8CD0` via a switch on the LLVM address space ID:

| Bit | Value | LLVM AS | Name |
|-----|-------|---------|------|
| 0 | `0x01` | 1 | Global |
| 1 | `0x02` | 3 | Shared |
| 2 | `0x04` | 4 | Constant |
| 3 | `0x08` | 5 | Local |
| 4 | `0x10` | 101 | Param |
| 0-3 | `0x0F` | N/A | Unknown (union of global + shared + constant + local) |

```
// sub_1CA8CD0 — address space to bitmask
switch (addrspace) {
    case 1:   return 0x01;   // global
    case 3:   return 0x02;   // shared
    case 4:   return 0x04;   // constant
    case 5:   return 0x08;   // local
    case 101: return 0x10;   // param
    default:  return 0x0F;   // unknown = union of all non-param
}
```

When multiple pointer sources contribute different address spaces (e.g., through PHI nodes or function arguments receiving pointers from different call sites), the bitmask is OR'd. A singleton bit (`popcount == 1`) means the space is fully resolved; multiple bits set means the pointer is ambiguous and requires either runtime `isspacep` or a conservative default to global.

### Resolution Decision

Once the bitmask is computed for a pointer:

- **Single bit set**: Resolved. The pass inserts an `addrspacecast` from generic to the target space and replaces all uses.
- **Multiple bits set, param bit included**: If `param-always-point-to-global` is true (default), resolve to global. The rationale: kernel parameters always point into global device memory.
- **Multiple bits set, no param**: Ambiguous. Emit warning `"Cannot tell what pointer points to, assuming global memory space"` and default to global.
- **Zero bits**: Unreachable code or analysis error.

### Relationship to EDG Frontend Encoding

The EDG frontend uses a separate encoding in the symbol table entry at offset +156/+157:

| EDG Bit | Value | Memory Space |
|---------|-------|-------------|
| +156 bit 0 | `0x01` | `__device__` (any device placement) |
| +156 bit 1 | `0x02` | `__shared__` |
| +156 bit 2 | `0x04` | `__constant__` |
| +156 bit 4 | `0x10` | Read-only linkage flag |
| +157 bit 0 | `0x01` | `__managed__` |

The EDG memory_space_code at offset +136 maps to LLVM address spaces during IR generation: code 1 (`__device__`) maps to AS 1, code 2 (`__shared__`) maps to AS 3, code 3 (`__constant__`) maps to AS 4.


## The Generic Address Space Problem

The generic (flat, AS 0) address space is the fundamental obstacle to alias precision on GPUs. When the EDG frontend or NVVM IR generator cannot determine which physical memory a pointer targets, it emits the pointer in AS 0. The hardware resolves generic addresses at runtime by checking whether the address falls within the shared memory window, the local memory window, or defaults to global -- a process that adds 4-8 cycles of latency per access.

For NVVM AA, a generic pointer forces MayAlias against every other pointer, destroying the disjointness guarantee and blocking optimizations in DSE, LICM, GVN, and MemorySSA. Three mechanisms address this:

**1. MemorySpaceOpt (compile-time conversion).** The two-phase inter-procedural pass resolves generic pointers by tracing them back to their allocation sites through use-def chains. When a generic pointer always derives from a `__shared__` variable, the pass inserts `addrspacecast` to AS 3 and rewrites all uses. When different call sites disagree on the address space for the same argument, the pass clones the function into space-specialized versions. Every generic pointer resolved gives NVVM AA an additional NoAlias edge. Disabling this pass (`-disable-MemorySpaceOptPass`) causes 2-20x performance regressions.

**2. AA address-space traversal.** Even without MemorySpaceOpt, NVVM AA's `getAddressSpace` helper walks through `addrspacecast` chains. If `%p` was produced by `addrspacecast i8 addrspace(3)* %s to i8*`, the traversal discovers AS 3 despite `%p` being in AS 0 at the use site.

**3. `!noalias.addrspace` metadata (kind 42).** cicc attaches this metadata to instructions when address space information is known but the pointer itself remains generic. The AA evaluator detects this via opcode byte `0x4E` ('N') and sets bit 2 in a pointer-tagged value (OR with 4), propagating disambiguation information through to `AAResults::alias`. This is a cicc-specific extension not found in upstream LLVM.


## Data Layout Strings

The NVPTX data layout string encodes pointer widths and alignment for each address space. cicc produces three variants based on pointer width and shared memory pointer mode.

### 64-bit with shared memory specialization (most common production mode)

```
e-p:64:64:64-p3:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f16:16:16-f32:32:32-f64:64:64-v16:16:16-v32:32:32-n16:32:64
```

### 64-bit without shared memory specialization

```
e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f16:16:16-f32:32:32-f64:64:64-v16:16:16-v32:32:32-n16:32:64
```

### 32-bit mode

```
e-p:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f16:16:16-f32:32:32-f64:64:64-v16:16:16-v32:32:32-n16:32:64
```

### Field-by-Field Breakdown

| Field | Meaning | NVIDIA Note |
|-------|---------|-------------|
| `e` | Little-endian | All NVIDIA GPUs |
| `p:64:64:64` | Default pointer: 64-bit size, 64-bit ABI align, 64-bit preferred align | Applies to AS 0 (generic), AS 1 (global), AS 4 (constant), AS 101 (param) |
| `p3:32:32:32` | AS 3 pointer: 32-bit size, 32-bit ABI align, 32-bit preferred align | Shared memory is on-chip, addressable with 32 bits even in 64-bit mode |
| `i1:8:8` | Booleans stored as 8-bit | Standard |
| `i128:128:128` | 128-bit integers: 128-bit aligned | Used by cmpxchg on global/shared |
| `n16:32:64` | Native integer widths | PTX has 16-bit, 32-bit, and 64-bit register files |
| `v16:16:16` / `v32:32:32` | Vector alignment: natural | 16-bit vectors at 16-bit, 32-bit vectors at 32-bit |

### Shared Memory 32-bit Pointer Optimization

The `p3:32:32:32` entry is the most impactful NVIDIA delta in the data layout. Shared memory lives in 48-228 KB of on-chip SRAM per SM, addressable with 32-bit pointers even when the rest of the address space is 64-bit. Using 32-bit pointers for shared memory saves register pressure (one register instead of two for every shared pointer) and instruction count (32-bit arithmetic instead of 64-bit for every address calculation).

The optimization is controlled by three knobs that alias the same underlying global (`unk_4D0461C`):

| Knob | Source |
|------|--------|
| `nvptx-short-ptr` | Backend option (`ctor_609_0` at `0x585D30`) |
| `nvptx-32-bit-smem` | Backend option (same constructor) |
| `+sharedmem32bitptr` | Target feature string (passed via `-arch` processing) |

When any of these is active, the data layout gains the `p3:32:32:32` entry, and LLVM's type system treats all `addrspace(3)*` pointers as 32-bit. This is transparent to the rest of the compiler -- DataLayout queries like `getPointerSizeInBits(3)` return 32 automatically, and all pointer arithmetic in shared memory is lowered to 32-bit operations.

The same 32-bit treatment applies to local memory (AS 5) in practice: local stack addresses are within the per-thread frame and always fit in 32 bits. However, the data layout does not carry an explicit `p5:32:32:32` entry -- the 32-bit treatment is enforced by the SelectionDAG lowering which uses AS 7 for stack operations.

### Known-Bits Implications

The 32-bit address spaces have direct implications for the known-bits analysis (`sub_BD5420`):

| Address Space | Pointer Width | Known Bits Effect |
|---|---|---|
| AS 0 (generic) | 64-bit | Pointer alignment only |
| AS 1 (global) | 64-bit | Low 4 bits often known-zero (16-byte alignment typical) |
| AS 3 (shared) | 32-bit | Low 2 bits known-zero (4-byte minimum), bits [32,63] irrelevant |
| AS 4 (constant) | 64-bit | Low 2 bits known-zero (4-byte alignment) |
| AS 5 (local) | 32-bit effective | Low 2 bits known-zero (stack alignment), bits [32,63] irrelevant |

DemandedBits exploits the 32-bit address spaces to eliminate zero-extensions and truncations around shared/local address calculations, keeping all pointer arithmetic in 32-bit ALU operations. This interacts with IV Demotion (`sub_18B1DE0`), which narrows 64-bit induction variables to 32-bit where shared memory address calculations permit.

### Data Layout Validation

The NVVM verifier (`sub_2C80C90`) validates the data layout string at multiple pipeline points:

- If empty: `"Empty target data layout, must exist"`
- If invalid: prints `"Example valid data layout:"` with reference strings from `off_4C5D0A0` (32-bit) and `off_4C5D0A8` (64-bit)
- A shortened compatibility form `e-i64:64-v16:16-v32:32-n16:32:64` is used in the IR linker (`sub_106AB30`) to verify that two modules being linked share the same NVPTX target data layout.


## Address Space Casts

NVPTX has strict rules for `addrspacecast` instructions, enforced by the NVVM verifier:

1. **At least one side must be generic (AS 0).** Casting between two non-generic address spaces is prohibited: `"Cannot cast non-generic pointer to different non-generic pointer"`. You must go through generic: `addrspace(3) -> addrspace(0) -> addrspace(1)`.

2. **Source and target must be valid.** The verifier rejects invalid address space IDs with `"Invalid target address space"` / `"Invalid source address space"`.

3. **Alloca must be in generic.** `"Allocas are not supported on address spaces except Generic"` -- alloca produces AS 0 pointers; MemorySpaceOpt later promotes them to AS 5.

4. **Tensor memory (AS 6) rejects load/store.** `"Tensor Memory loads/stores are not supported"` -- AS 6 memory must be accessed through TMA intrinsics (`cp.async.bulk.*`), not regular load/store instructions.

5. **cmpxchg is restricted.** `"cmpxchg pointer operand must point to generic, global, or shared address space"` -- atomic compare-exchange only supports AS 0, AS 1, and AS 3, with i32/i64/i128 operand types.

### cvta Intrinsic Mapping

The PTX `cvta` (Convert Virtual Address) instructions are lowered through intrinsic IDs in the EDG frontend (`sub_94A030`):

| Intrinsic ID Range | Direction | Address Space |
|---------------------|-----------|---------------|
| 0xC1 (193) | Generic -> Specific | Shared (AS 3) |
| 0xC2 (194) | Generic -> Specific | Constant (AS 4) |
| 0xC3 (195) | Generic -> Specific | Local (AS 5) |
| 0xC4 (196) | Generic -> Specific | Global (AS 1) |
| 0xC5 (197) | Specific -> Generic | Shared (AS 3) |
| 0xC6 (198) | Specific -> Generic | Constant (AS 4) |
| 0xC7 (199) | Specific -> Generic | Local (AS 5) |
| 0xC8 (200) | Specific -> Generic | Global (AS 1) |

The specific-to-generic direction emits `addrspacecast` (opcode `0x30`). The generic-to-specific direction uses a store-to-temp followed by a load with the target address space annotation.


## SelectionDAG Address Space Encoding

The SelectionDAG backend uses a secondary address space encoding for the `.param` passing convention. In `sub_33B0210` (intrinsic lowering within the SelectionDAG), pointer arguments use this mapping:

| SelectionDAG Code | LLVM AS | PTX Space |
|--------------------|---------|-----------|
| 1 | 1 (global) | `.global` |
| 2 | 3 (shared) | `.shared` |
| 3 | 4 (constant) | `.const` |
| 4 | 5 (local) | `.local` |
| 5 | -- | `.param` (not a real AS, lowered to param window) |
| 7 | 7 (shared cluster) | `.shared::cluster` |

Stack operations (SelectionDAG opcode 16, `StackAlloc`) explicitly use AS 7 for the `.param`-like space when lowering stack frames via `sub_33FF780(dag, ..., 7, 0, 1, 0)`.


## Internal Address Spaces (Non-Physical)

### AS 25 -- Device Linkage Annotation

Address space 25 is used by the module summary pass (`sub_1C28690` in `p2-H01-nvmodule-summary.txt`) to tag functions and variables with `__device__` linkage during inter-module resolution. When a function's type resolves to AS 25, it indicates the symbol has device-side linkage and requires device-side extern resolution. This address space never appears in emitted PTX -- it is consumed during linking and stripped before codegen.

### AS 53 -- MemorySpaceOpt Global Annotation

During pass initialization (`sub_1CAB590`), MemorySpaceOpt filters module globals that carry address space 53 and registers them into internal tracking structures. This appears to be an annotation mechanism for marking globals that require special address space analysis. Like AS 25, this address space is internal and does not survive to PTX emission.


## Shared Memory Specializations by SM Generation

| SM | Shared Memory Size | Cluster Support | AS 7 Available | Shared Memory Pointer |
|----|--------------------|-----------------|----------------|----------------------|
| SM 70 (Volta) | 96 KB configurable with L1 | No | No | 32-bit (when `+sharedmem32bitptr`) |
| SM 80 (Ampere) | 164 KB configurable | No | No | 32-bit |
| SM 86 (Ampere GA10x) | 100 KB configurable | No | No | 32-bit |
| SM 89 (Ada) | 100 KB configurable | No | No | 32-bit |
| SM 90 (Hopper) | 228 KB configurable | Yes | Yes | 32-bit |
| SM 100 (Blackwell) | 228 KB configurable | Yes | Yes | 32-bit |

With SM 90+, `__shared__` variables accessed with cluster scope use `.shared::cluster` (AS 7), which provides cross-CTA access within a cooperative thread array cluster. Regular intra-CTA shared access remains on AS 3 (`.shared`). The EarlyCSE pass (`sub_2781BB6`) detects AS 7 stores and applies conservative aliasing to prevent CSE across shared cluster barriers.


## isspacep Intrinsics

The PTX `isspacep` instruction tests at runtime whether a generic pointer points to a specific address space. cicc represents these as intrinsics with builtin IDs `0xFD0`-`0xFD5`:

| Builtin ID | PTX | Tests for |
|------------|-----|-----------|
| `0xFD0` | `isspacep.global` | Global (AS 1) |
| `0xFD1` | `isspacep.shared` | Shared (AS 3) |
| `0xFD2` | `isspacep.local` | Local (AS 5) |
| `0xFD3` | `isspacep.const` | Constant (AS 4) |
| `0xFD4` | `isspacep.shared::cta` | Shared CTA-local (AS 3, SM 90+) |
| `0xFD5` | `isspacep.shared::cluster` | Shared cluster (AS 7, SM 90+) |

MemorySpaceOpt's second-time resolver (`sub_1CA9E90`) folds these to compile-time constants when the pointer's address space is already known: `isspacep.shared(%p)` where `%p` is proven to be AS 3 folds to `true`. This eliminates runtime address space checks from conditional code patterns like:

```cuda
if (__isShared(p))
    atomicAdd_shared(p, val);
else
    atomicAdd(p, val);
```


## Configuration Knobs Affecting Address Spaces

| Knob | Default | Effect |
|------|---------|--------|
| `nvptx-short-ptr` | -- | Enable 32-bit pointers for shared/const/local |
| `nvptx-32-bit-smem` | -- | Same effect as above (alias) |
| `param-always-point-to-global` | true | Resolve ambiguous param pointers to global |
| `mem-space-alg` | 2 | Algorithm selection for MemorySpaceOpt (2 = default, others select alternate impl at `sub_2CBBE90`) |
| `track-indir-load` | true | Track pointers loaded from memory during address space analysis |
| `track-int2ptr` | true | Track `inttoptr` casts during analysis |
| `nvptx-traverse-address-aliasing-limit` | 6 | Max depth for NVVM AA `getAddressSpace` traversal |
| `do-clone-for-ip-msp` | -1 (unlimited) | Max function clones for inter-procedural specialization |
| `process-alloca-always` | true | Treat alloca as definite local (AS 5) |


## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| MemorySpaceOpt pass entry | `sub_1C70910` | -- | Mode dispatch, IP-MSP worklist driver |
| Per-BB instruction scanner | `sub_1CA8CD0` | -- | AS-to-bitmask mapping switch |
| Use-def chain walker | `sub_1CA5350` | -- | Backward pointer origin tracking |
| First-time resolver | `sub_1CA2920` | -- | Conservative address space resolution |
| Second-time resolver | `sub_1CA9E90` | -- | Hash-table-based resolution, isspacep folding |
| MemorySpaceCloning engine | `sub_2CBBE90` | -- | Inter-procedural function cloning (11.0 KB binary) |
| IPMSP module pass variant | `sub_1C6A6C0` | -- | LIBNVVM path (10.5 KB binary) |
| EDG cvta lowering | `sub_94A030` | -- | Address space cast intrinsic generation |
| EDG decl-side memspace processing | `sub_6582F0` | -- | CUDA attribute to memory space code resolution |
| EDG def-side memspace processing | `sub_65F400` | -- | Definition validation and initializer handling |
| NVVMModuleVerifier | `sub_2C80C90` | -- | Data layout and address space validation |
| NVVMIntrinsicVerifier | `sub_2C7B6A0` | -- | Per-intrinsic address space constraint checking |
| SelectionDAG intrinsic lowering | `sub_33B0210` | -- | Backend AS mapping for param passing |
| getPointerAlignmentBits | `sub_BD5420` | -- | Known-bits for address space pointer widths |
| NVIDIA intrinsic known-bits oracle | `sub_F0C4B0` | -- | Special register ranges |


## Cross-References

- [Memory Space Optimization](../passes/memory-space-opt.md) -- Two-phase address space resolver, bitmask dataflow, function cloning
- [IPMSP](../passes/ipmsp.md) -- Inter-procedural memory space propagation, worklist algorithm
- [Alias Analysis & NVVM AA](../infra/alias-analysis.md) -- Address space disjointness, AA chain, `!noalias.addrspace`
- [NVPTX Target Infrastructure](../infra/nvptx-target.md) -- Data layout strings, `+sharedmem32bitptr` feature, TTI hooks
- [KnownBits & DemandedBits](../llvm/known-bits.md) -- Address space pointer width in known-bits, DemandedBits narrowing
- [NVVM Verifier](../passes/nvvm-verify-deep.md) -- addrspacecast rules, tensor memory restriction, cmpxchg constraints
- [EDG Frontend](../pipeline/edg.md) -- CUDA memory space attributes (`__shared__`, `__constant__`, `__device__`)
- [SelectionDAG](../llvm/selectiondag.md) -- Backend address space encoding for param passing
- [IV Demotion](../passes/iv-demotion.md) -- Exploits 32-bit shared memory pointers for induction variable narrowing
- [EarlyCSE](../llvm/early-cse.md) -- Shared cluster (AS 7) store handling
