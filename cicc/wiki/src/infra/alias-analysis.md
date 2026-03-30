# Alias Analysis & NVVM AA

cicc ships a custom alias analysis pass (NVVM AA, registered as `nvptx-aa`) that exploits GPU address space disjointness to prove pointer pairs cannot alias. On a GPU, each hardware memory partition -- global DRAM, shared scratchpad, local stack, constant cache, kernel parameter window -- occupies a physically separate address range. Pointers into different address spaces can never reference the same byte, a property that does not hold on any mainstream CPU ISA. NVVM AA encodes this hardware invariant into the LLVM AA pipeline, returning `NoAlias` for any cross-address-space pointer pair. This single fact unlocks aggressive dead-store elimination, load-store motion, GVN load forwarding, and MemorySSA precision that would be impossible on a flat-memory machine. The pass is stateless, trivially cheap, and runs first in the AA chain so that more expensive analyses (BasicAA, TBAA) can skip pairs that NVVM AA already resolved.

Beyond pure address-space disjointness, cicc augments the standard LLVM AA infrastructure in three further ways: (1) a `process-restrict` pass that propagates `noalias` attributes from `__restrict__` kernel parameters, (2) `!noalias.addrspace` metadata (metadata kind 42) that tags pointers with the set of address spaces they provably do not alias with, and (3) NVIDIA-specific knobs controlling traversal depth, TBAA strictness, and fence relaxation.


## Key Facts

| Property | Value |
|---|---|
| Pass name (legacy PM) | `nvptx-aa` |
| Pass name (new PM) | Registered via `NVPTXTargetMachine::registerEarlyDefaultAliasAnalyses` |
| Legacy wrapper | `NVPTXAAWrapperPass` (ImmutablePass, char ID) |
| External wrapper | `NVPTXExternalAAWrapper` (hooks into `ExternalAAWrapperPass`, `RunEarly=true`) |
| Result class | `NVPTXAAResult : AAResultBase` |
| State | Stateless -- `invalidate()` always returns false |
| AA chain position | First (before BasicAA) |
| Address traversal depth | Controlled by `nvptx-traverse-address-aliasing-limit` (default 6) |
| AA evaluator pass | `aa-eval` at `sub_13549C0` (11,038 bytes) |
| AA query entry point | `sub_134CB50` -- `AAResults::alias(MemoryLocation, MemoryLocation)` |
| ModRef query (call, loc) | `sub_134F0E0` -- `AAResults::getModRefInfo(CallBase, MemoryLocation)` |
| ModRef query (call, call) | `sub_134F530` -- `AAResults::getModRefInfo(CallBase, CallBase)` |


## GPU Address Space Table

NVPTX defines six logically disjoint address spaces plus a generic (flat) umbrella:

| LLVM AS | Name | PTX qualifier | Hardware | Aliasing rule |
|---------|------|--------------|----------|---------------|
| 0 | generic (flat) | `.generic` | Virtual -- maps to any physical space at runtime | May alias with **all** other spaces |
| 1 | global | `.global` | Device DRAM, L2 cached | May alias with AS 1 and AS 101 (param window is within global) |
| 3 | shared | `.shared` | Per-CTA scratchpad SRAM | May alias with AS 3 and AS 7 (shared cluster) |
| 4 | constant | `.const` | Read-only constant cache | Cannot be written; `getModRefInfoMask` returns `NoModRef` |
| 5 | local | `.local` | Per-thread stack in DRAM | Only aliased by itself |
| 7 | shared cluster | `.shared::cluster` | Distributed shared memory (SM 90+) | May alias with AS 3 (regular shared) |
| 101 | param | `.param` | Kernel parameter window | Read-only; may alias with AS 1 on SM 70+ via `cvta.param` |

The critical insight: any `(AS_x, AS_y)` pair where `x != y` and neither is 0 (generic) and neither is the shared/shared-cluster pair returns **NoAlias**, unless `x` is global and `y` is param (or vice versa) once `cvta.param` support is added.

MemorySpaceOpt uses an internal bitmask encoding for dataflow analysis of address spaces:

| Bit | Value | Address space |
|-----|-------|--------------|
| 0 | `0x01` | global (AS 1) |
| 1 | `0x02` | shared (AS 3) |
| 2 | `0x04` | constant (AS 4) |
| 3 | `0x08` | local (AS 5) |
| 4 | `0x10` | param (AS 101) |
| 0--3 | `0x0F` | unknown (union of all non-param) |

The mapping is implemented in `sub_1CA8CD0` via a switch on the LLVM address space ID. When multiple sources contribute different spaces, bitmasks are OR'd. A singleton bit means the pointer's space is fully resolved; multiple bits set means the pointer is ambiguous (requires runtime `isspacep` or conservative default to global).


## The NVVM AA Algorithm

The core alias function follows upstream `NVPTXAliasAnalysis.cpp` in structure, enhanced with cicc-specific extensions. The pseudocode:

```cpp
// NVPTXAAResult::alias -- the heart of NVVM AA
AliasResult alias(const MemoryLocation &Loc1,
                  const MemoryLocation &Loc2,
                  AAQueryInfo &AAQI) {
    unsigned AS1 = getAddressSpace(Loc1.Ptr, TraverseLimit);
    unsigned AS2 = getAddressSpace(Loc2.Ptr, TraverseLimit);

    // If either pointer is in generic (flat) space, we cannot disambiguate.
    // Generic pointers can point to any physical memory at runtime.
    if (AS1 == ADDRESS_SPACE_GENERIC || AS2 == ADDRESS_SPACE_GENERIC)
        return AliasResult::MayAlias;

    // Distributed shared memory (AS 7) overlaps with regular shared (AS 3).
    if ((AS1 == 3 && AS2 == 7) || (AS1 == 7 && AS2 == 3))
        return AliasResult::MayAlias;

    // Same address space: cannot determine from space alone.
    // Fall through to BasicAA / TBAA for further analysis.
    if (AS1 == AS2)
        return AliasResult::MayAlias;

    // Different non-generic, non-overlapping spaces: provably disjoint.
    return AliasResult::NoAlias;
}

// getAddressSpace -- walk through casts to find the underlying space.
// Traverses up to MaxLookup levels of getUnderlyingObject().
unsigned getAddressSpace(const Value *V, unsigned MaxLookup) {
    while (MaxLookup-- > 0) {
        unsigned AS = V->getType()->getPointerAddressSpace();
        if (AS != ADDRESS_SPACE_GENERIC)
            return AS;
        const Value *Next = getUnderlyingObject(V, /*MaxLookup=*/1);
        if (Next == V)
            break;  // Reached a root (alloca, argument, global)
        V = Next;
    }
    return V->getType()->getPointerAddressSpace();
}
```

The `getAddressSpace` helper is the key difference from a naive check. A pointer may be in generic address space (AS 0) at its use site but was produced by an `addrspacecast` from a specific space. The traversal walks backward through `getUnderlyingObject` (which strips GEPs, bitcasts, PHIs) to find the original non-generic space. The depth limit (`nvptx-traverse-address-aliasing-limit`, default 6) prevents exponential blowup on deeply nested pointer chains.

The `getModRefInfoMask` method adds a further optimization: pointers into constant memory (AS 4) or parameter memory (AS 101) are read-only, so it returns `NoModRef` -- the pointer's memory is never modified. This allows DSE to skip analysis of stores that might alias with const/param loads, and lets LICM hoist loads from constant memory without checking for intervening stores.

The `getMemoryEffects` method handles inline assembly: PTX inline asm without side-effects or `{memory}` clobbers is treated as having no memory effects, which prevents it from blocking optimizations.


## The Generic Address Space Problem

The generic (flat, AS 0) address space is the fundamental obstacle to alias precision on GPUs. When the frontend cannot determine which physical memory a pointer targets, it emits the pointer in AS 0. The hardware resolves generic addresses at runtime using address range checks -- a pointer into the shared memory window maps to shared, otherwise it maps to global.

For NVVM AA, a generic pointer forces `MayAlias` against every other pointer, destroying the disjointness guarantee. This is why MemorySpaceOpt is so critical: it runs before the main optimization pipeline and converts generic pointers to specific address spaces wherever possible, feeding precise AS information into NVVM AA.

Three mechanisms address the generic pointer problem:

**1. MemorySpaceOpt (pre-optimization conversion).** The two-phase interprocedural pass at `sub_1C70910` resolves generic pointers by tracing them back to their allocation sites. If a generic pointer is always derived from a `__shared__` variable, the pass inserts an `addrspacecast` to AS 3 and rewrites all uses. When different call sites pass different address spaces for the same argument, the pass clones the function into space-specialized versions. This is the most impactful optimization: every generic pointer that MemorySpaceOpt resolves gives NVVM AA an additional `NoAlias` edge.

**2. Address space traversal in AA.** Even without MemorySpaceOpt, the `getAddressSpace` helper in NVVM AA walks through `addrspacecast` chains. If a generic pointer `%p` was produced by `addrspacecast i8 addrspace(3)* %s to i8*`, the traversal discovers AS 3. The traversal depth limit (default 6) controls how far back the walk goes.

**3. `!noalias.addrspace` metadata (kind 42).** cicc attaches this metadata to instructions when address space information is known but the pointer itself remains generic. The AA evaluator (`sub_13549C0`) detects this metadata via opcode byte `0x4E` ('N') and sets bit 2 in a pointer-tagged value (OR with 4), propagating the address-space disambiguation information through to `AAResults::alias`. This is a cicc-specific extension not found in upstream LLVM.


## AA Pipeline Ordering

cicc configures the AA chain with NVVM AA running first, as confirmed by the `NVPTXExternalAAWrapper` which passes `RunEarly=true` to `ExternalAAWrapperPass`. The full chain:

```
NVVM AA  -->  BasicAA  -->  TBAA  -->  ScopedNoAliasAA  -->  GlobalsAA
  |              |            |              |                    |
  |              |            |              |                    +-- Module-level: which globals
  |              |            |              |                        escape? (enable-unsafe-
  |              |            |              |                        globalsmodref-alias-results)
  |              |            |              |
  |              |            |              +-- !noalias / !alias.scope metadata
  |              |            |                  (enable-scoped-noalias, default true)
  |              |            |
  |              |            +-- Type-based: !tbaa metadata tree
  |              |                (enable-tbaa, default true)
  |              |
  |              +-- Stateless: GEP decomposition, alloca vs argument,
  |                  capture analysis (basic-aa-recphi, default true;
  |                  basic-aa-separate-storage, default true)
  |
  +-- Address space disjointness (stateless, O(depth) per query)
```

The chain is queried through `AAResults::alias()` (`sub_134CB50`), which dispatches through the registered AA providers in order. Each provider returns `NoAlias`, `MayAlias`, `PartialAlias`, or `MustAlias`. If any provider returns `NoAlias`, the chain short-circuits -- subsequent providers are not consulted. This is why NVVM AA runs first: cross-address-space pairs are resolved in O(1) without invoking the more expensive BasicAA GEP decomposition.

The `AAResults` object consumed by MemorySSA, GVN, DSE, and LICM is the same chained result. All memory-aware passes benefit transparently from NVVM AA without any code changes.


## Integration with Memory Optimization Passes

NVVM AA's impact flows through every pass that queries alias information:

**MemorySSA** (`sub_1A6A260`) builds its memory SSA graph using `AAResults` at `[this+0xB8]` (retrieved via tag `unk_4F9D3C0`). When NVVM AA proves that a store to shared memory and a load from global memory are `NoAlias`, MemorySSA does not create a dependency edge between them, resulting in a sparser -- and more precise -- memory graph. This precision propagates to every consumer of MemorySSA.

**GVN** (`sub_1900BB0`) uses AA for load elimination and store forwarding. With NVVM AA, a load from `%p_global` can be forwarded past a store to `%q_shared` because they provably do not alias. Without NVVM AA, GVN would conservatively assume they might alias and abandon the forwarding. The GVN implementation queries `sub_134CB50` indirectly through `MemoryDependenceResults`, which itself consults `AAResults`.

**DSE** (`sub_19DD1D0` and related functions) eliminates dead stores by proving that no subsequent load reads the stored value. DSE requires `AAResults` at `unk_4F9D3C0`. The DSE report confirms: "The alias analysis that DSE consumes already handles address-space separation. CUDA address spaces (shared=3, global=1, local=5, constant=4) are handled by the underlying NVVM alias analysis which knows that different address spaces cannot alias." DSE does NOT implement its own address-space checks -- it relies entirely on NVVM AA.

**LICM** uses AA to determine whether a load inside a loop can be hoisted out. If NVVM AA proves a loop-invariant load from constant memory (AS 4, `getModRefInfoMask` returns `NoModRef`) cannot be modified by any store in the loop, LICM hoists it. This is especially impactful for `__constant__` kernel arguments accessed repeatedly in hot loops.


## `noalias` Metadata and `__restrict__` Handling

cicc provides two mechanisms for marking kernel pointer parameters as non-aliasing:

**1. `-restrict` / `--kernel-params-are-restrict` (frontend flag, ID 73).** When the user passes `-restrict` to nvcc (or `--kernel-params-are-restrict` to cicc), it routes to the LLVM knob `-nvptx-kernel-params-restrict`. This causes cicc to add the `noalias` attribute to all pointer-typed kernel parameters, asserting that the programmer guarantees no two kernel pointer arguments alias. The `process-restrict` pass (`ProcessRestrictPass`, registered as a function pass in the new PM at position 419 in the pipeline parser) then propagates this attribute through the call graph. The `propagate-only` mode restricts the pass to propagation without inserting new restrict annotations.

**2. `-allow-restrict-in-struct` (flag at offset +1128).** Extends `__restrict__` handling to pointer fields inside struct arguments. When enabled, the `process-restrict` pass annotates struct-member pointers with `noalias` scope metadata, enabling AA to disambiguate pointers extracted from different struct fields.

Supporting knobs:
- `apply-multi-level-restrict` -- apply `__restrict__` to all pointer indirection levels (not just the outermost pointer)
- `dump-process-restrict` -- debug dump during restrict processing

The `noalias` attribute interacts with the AA chain through `ScopedNoAliasAA`, which reads `!noalias` and `!alias.scope` metadata attached to instructions. cicc's frontend emits these metadata nodes when `__restrict__` qualifiers are present in the CUDA source.

The `!noalias.addrspace` metadata (kind 42, registered in `sub_B6EEA0`) is a separate mechanism specific to address-space disambiguation. It is attached by MemorySpaceOpt or IR generation when a pointer is known to not alias with pointers in specific address spaces, even if the pointer itself remains in generic AS 0. The AA evaluator detects this metadata and tags the pointer with bit 2 (OR with 4) for disambiguation during alias queries.


## Comparison with Upstream LLVM NVPTX

Upstream LLVM (as of LLVM 19/20) includes `NVPTXAliasAnalysis.cpp` in `llvm/lib/Target/NVPTX/`, which implements the same core address-space disjointness logic. cicc's version is functionally equivalent to upstream for the basic alias query but differs in several ways:

| Aspect | Upstream LLVM | cicc v13.0 |
|--------|--------------|------------|
| Core alias check | Same: cross-AS = NoAlias, generic = MayAlias | Same |
| Shared cluster handling | AS 3 vs AS 7 = MayAlias | Present (SM 90+ targets) |
| Param aliasing with global | Commented TODO: "cvta.param not yet supported" | Same conservative treatment |
| getModRefInfoMask | Const/param = NoModRef | Same |
| Inline asm analysis | Checks side-effects + `{memory}` clobber | Same |
| Traversal depth knob | `nvptx-traverse-address-aliasing-limit` (default 6) | Same knob present |
| `!noalias.addrspace` metadata | Not used upstream | cicc-specific extension (metadata kind 42) |
| `strict-aliasing` knob | Not in upstream NVPTX | cicc adds "Datatype based strict alias" |
| `nvptxaa-relax-fences` | Not in upstream | cicc-specific: ordering relaxation for fences |
| `process-restrict` pass | Not in upstream NVPTX backend | cicc-specific interprocedural restrict propagation |
| Integration with MemorySpaceOpt | No upstream equivalent | cicc's address space inference feeds NVVM AA |

The most significant delta is the ecosystem: upstream NVPTX has the AA pass but lacks the interprocedural MemorySpaceOpt pipeline that resolves generic pointers, the `process-restrict` pass that propagates `noalias`, and the `!noalias.addrspace` metadata that bridges partial address-space knowledge into the AA chain. These three components working together give cicc far more `NoAlias` results than upstream LLVM achieves on the same IR.


## Configuration Knobs

### NVVM AA Knobs

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `nvptx-traverse-address-aliasing-limit` | unsigned | 6 | Maximum depth for `getAddressSpace` traversal through `getUnderlyingObject` |
| `nvptxaa-relax-fences` | bool | (unknown) | Enable ordering relaxation for fence instructions in AA |
| `strict-aliasing` | bool | (unknown) | "Datatype based strict alias" -- NVIDIA extension for type-based disambiguation |
| `traverse-address-aliasing` | bool | (unknown) | "Find address space through traversal" -- master enable for the traversal in `getAddressSpace` |
| `assume-default-is-flat-addrspace` | bool | false | Treat default address space (0) as flat/generic (testing knob) |

### Standard LLVM AA Knobs (present in cicc)

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `disable-basic-aa` / `disable-basicaa` | bool | false | Disable BasicAA entirely |
| `basic-aa-recphi` | bool | true | Enable recursive PHI analysis in BasicAA |
| `basic-aa-separate-storage` | bool | true | Enable separate-storage analysis in BasicAA |
| `enable-tbaa` | bool | true | Enable Type-Based Alias Analysis |
| `enable-scoped-noalias` | bool | true | Enable ScopedNoAlias AA (processes `!noalias` / `!alias.scope`) |
| `enable-unsafe-globalsmodref-alias-results` | bool | false | Enable GlobalsModRef (requires unsafe assumption about global escapes) |
| `alias-set-saturation-threshold` | int | (default) | Maximum pointers in an AliasSet before it saturates |
| `aa-pipeline` | string | (default) | Override the AA pipeline configuration |

### Restrict Processing Knobs

| Knob | Type | Default | Description |
|------|------|---------|-------------|
| `nvptx-kernel-params-restrict` | bool | false | Mark all kernel pointer params as `noalias` (activated by `-restrict` flag) |
| `allow-restrict-in-struct` | bool | false | Propagate `__restrict__` into struct pointer members |
| `apply-multi-level-restrict` | bool | (unknown) | Apply `__restrict__` through all pointer indirection levels |
| `dump-process-restrict` | bool | false | Debug dump during restrict processing |

### AA Evaluator Debug Flags

The `aa-eval` diagnostic pass (`sub_13549C0`) uses 14 independent boolean flags for selective output:

| Address | Flag | Controls |
|---------|------|----------|
| `byte_4F97AA0` | `print-all-alias-modref-info` | Master enable for all AA debug output |
| `byte_4F979C0` | `print-all-alias-no` | Print NoAlias pointer pairs |
| `byte_4F978E0` | `print-all-alias-may` | Print MayAlias pointer pairs |
| `byte_4F97800` | `print-all-alias-partial` | Print PartialAlias pointer pairs |
| `byte_4F97720` | `print-all-alias-mustalias` | Print MustAlias pointer pairs |
| `byte_4F97640` | `print-all-modref-none` | Print NoModRef results |
| `byte_4F97560` | `print-all-modref-ref` | Print JustRef results |
| `byte_4F97480` | `print-all-modref-mod` | Print JustMod results |
| `byte_4F973A0` | `print-all-modref-both` | Print BothModRef results |
| `byte_4F96F40` | `aa-eval-callsite-modref` | Enable call-site ModRef evaluation (Phase 5) |


## Function Map

| Address | Size | Identity |
|---------|------|----------|
| `sub_134CB50` | -- | `AAResults::alias(MemoryLocation, MemoryLocation)` -- main alias query entry |
| `sub_134F0E0` | -- | `AAResults::getModRefInfo(CallBase, MemoryLocation)` |
| `sub_134F530` | -- | `AAResults::getModRefInfo(CallBase, CallBase)` |
| `sub_13549C0` | 11,038 B | `AAEvaluator::runOnFunction` -- the `aa-eval` diagnostic pass |
| `sub_13540B0` | -- | `SmallPtrSet::insert` (pointer collection in aa-eval) |
| `sub_1352080` | -- | Pointer-pair result printer (aa-eval) |
| `sub_1351E00` | -- | Call-site pair result printer (aa-eval) |
| `sub_13523B0` | -- | Formatted alias result printer (aa-eval) |
| `sub_13C7380` | 35.7 KB | `GlobalsAA` main analysis function |
| `sub_13C5530` | 21 KB | `GlobalsAA` helper (per-function analysis) |
| `sub_13C4410` | 6.7 KB | `GlobalsAA` call-site analysis |
| `sub_13C34D0` | 12.6 KB | `GlobalsAA` alias query |
| `sub_FD1250` | 23.4 KB | AA iteration / chaining logic |
| `sub_14A4050` | -- | Dominator-tree-based AA query setup (used by MemorySSA) |
| `sub_B6EEA0` | 9 KB | Metadata kind registration (including `noalias.addrspace` = kind 42) |
| `sub_1C70910` | ~2,427 lines | MemorySpaceOpt pass entry (IP-MSP worklist driver) |
| `sub_1CA8CD0` | ~898 lines | MemorySpaceOpt per-BB scanner + address-space bitmask builder |


## Cross-References

- [MemorySpaceOpt](../passes/memory-space-opt.md) -- the interprocedural pass that resolves generic pointers to specific address spaces, directly feeding NVVM AA
- [IP Memory Space Propagation](../passes/ipmsp.md) -- the interprocedural wrapper around MemorySpaceOpt
- [GVN](../llvm/gvn.md) -- consumes AA for load elimination and store forwarding
- [DSE](../llvm/dse.md) -- relies on AA for dead store detection; confirmed to have no internal address-space checks
- [LICM](../llvm/licm.md) -- uses AA to hoist/sink memory operations across loops
- [Pipeline & Ordering](../llvm/pipeline.md) -- where NVVM AA fits in the overall pass schedule
- [LLVM Knobs](../config/knobs.md) -- complete knob inventory including AA-related knobs
- [Optimization Levels](../config/optimization-levels.md) -- how `NVVMAliasAnalysis` appears in the tier 2+ pipeline
