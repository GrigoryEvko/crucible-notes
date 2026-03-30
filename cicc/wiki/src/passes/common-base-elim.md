# Common Base Elimination

The Common Base Elimination pass hoists shared base address expressions to dominating points in the control flow graph, eliminating redundant recomputations of the same base pointer across multiple basic blocks. Where [Base Address Strength Reduction](./base-address-sr.md) targets intra-loop patterns driven by induction variables, Common Base Elimination operates at the inter-block level: it groups memory operations that share the same base pointer regardless of loop structure, finds their common dominator, and creates a single base computation at that dominator. Every original address is then rewritten as `(hoisted_base + relative_offset)`.

This is a strictly GPU-motivated optimization. NVIDIA GPUs have limited integer ALU throughput relative to their floating-point pipelines, so any reduction in address arithmetic directly translates to freed execution slots for other work. On a typical CUDA kernel performing strided accesses across multiple branches (e.g., different cases of a `switch` over tile indices), the pass can eliminate dozens of redundant GEP chains that independently recompute the same base address.

The two-pass approach -- Common Base Elimination first at the IR level for inter-block redundancies, then Base Address Strength Reduction for intra-loop induction-variable patterns -- ensures comprehensive coverage of GPU address computation overhead.

## Key Facts

| Field | Value |
|---|---|
| **Pass name (string)** | `"Common Base Elimination"` |
| **Entry point** | `sub_1C5DFC0` |
| **Binary offset** | `0x1C5DFC0` |
| **Size** | 38 KB (~850 decompiled lines) |
| **Scope** | Function-level |
| **IR level** | LLVM IR (pre-codegen) |
| **No upstream equivalent** | Entirely NVIDIA-proprietary |
| **Complementary pass** | Base Address Strength Reduction (`sub_1C67780`) |
| **Related SCEV-CGP knob** | `scev-cgp-cross-block-limit` -- limits common bases from a single block |
| **Required analysis** | Dominator tree (`a1[23]`), DataLayout |

## Algorithm

The pass has four major phases: address decomposition, base pointer grouping, dominator-based hoisting, and address rewriting.

### Phase 1 -- Address Expression Decomposition

For every memory operation (load, store, GEP-based address) in the function, the pass calls `sub_1C53170` to decompose the address into a structured form:

```
struct AddressExpr {
    Value *base_ptr;          // The root pointer (alloca, global, argument)
    Operand  operands[];      // List of (index, constant_offset) pairs
    unsigned operand_count;   // Number of index terms
};
```

The result is stored as a `(base_ptr, operand_list, operand_count)` tuple. The decomposition strips away GEP chains to expose the underlying base pointer and accumulates constant offset terms separately from variable index terms. This is the same decomposition helper used by BASR (`sub_1C67780`), ensuring both passes reason about addresses in a compatible representation.

### Phase 2 -- Base Pointer Grouping

The pass maintains two hash maps for grouping addresses:

**Non-pointer-type bases** (hash map at `v382`, keyed by base pointer value):
- Each memory operation whose decomposed base is not a pointer type (type_id != 15) is inserted via `sub_1C50900`.
- The hash entry accumulates a list of all instructions sharing that base.
- New bases are appended to worklist `v363`.

**Pointer-to-global bases** (hash map at `v378`, keyed by underlying global variable):
- For pointer-type bases, `sub_1CCDC20` extracts the underlying global variable by walking through bitcast and GEP chains.
- This allows grouping addresses to the _same global_ even when accessed through different local pointer variables.
- New globals are appended to worklist `v360`.

The hash maps use the standard DenseMap growth policy (75% load factor, 12.5% tombstone compaction) with NVVM-layer sentinels (-8 / -16). `sub_1C54050` handles both resize and in-place rehash. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the complete specification.

### Phase 3 -- Dominator Walk and Base Hoisting

For each base pointer group containing two or more uses, the pass:

1. **Finds the anchor.** Among all constant offsets in the group, the operand with the _minimum_ constant offset becomes the anchor. For offsets up to 64 bits, the constant is extracted directly from the GEP operand. For wider offsets (> 64 bits), the pass reads from extended-precision word arrays. Sign-extended comparisons determine the minimum.

2. **Computes the common dominator.** The pass reads the function's dominator tree from `a1[23]` and walks it to find the nearest block that dominates all use sites. This is the standard `findNearestCommonDominator` operation -- iteratively walk both paths toward the root until they meet.

3. **Inserts the hoisted base.** `sub_13A5B00` creates a new base address computation (a GEP or add instruction) at the terminator insertion point of the common dominator block. The hoisted instruction computes `base_ptr + min_offset`, which is the anchor's address.

4. **Rewrites all uses.** For each original memory operation in the group, `sub_14806B0` rewrites the address as `(hoisted_base + (original_offset - min_offset))`. Since the anchor's own relative offset is zero, it becomes a direct use of the hoisted base.

In pseudocode:

```
fn run_common_base_elimination(F: &Function):
    let dom_tree = F.dominator_tree    // a1[23]
    let data_layout = F.module.data_layout

    // Phase 1+2: decompose and group
    let base_groups: HashMap<Value*, Vec<(Instruction*, ConstantOffset)>> = {}
    let global_groups: HashMap<GlobalVariable*, Vec<(Instruction*, ConstantOffset)>> = {}

    for bb in F.basic_blocks():
        for inst in bb.instructions():
            if !is_memory_op(inst): continue
            let (base, offsets, count) = sub_1C53170(inst)

            if base.type_id != POINTER_TYPE:
                base_groups[base].push((inst, offsets))
            else:
                let gv = sub_1CCDC20(base)   // extract global
                global_groups[gv].push((inst, offsets))

    // Phase 3+4: hoist and rewrite
    for (base, uses) in chain(base_groups, global_groups):
        if uses.len() < 2: continue

        let min_offset = uses.iter().map(|u| u.offset).min()
        let anchor_inst = uses.find(|u| u.offset == min_offset).inst

        // Find common dominator of all use blocks
        let dom_block = uses[0].inst.parent
        for use in uses[1..]:
            dom_block = dom_tree.find_nearest_common_dominator(
                            dom_block, use.inst.parent)

        // Hoist: create base+min_offset at dominator
        let hoisted = sub_13A5B00(dom_block, base, min_offset)

        // Rewrite all uses
        for (inst, offset) in uses:
            let relative = offset - min_offset
            sub_14806B0(inst, hoisted, relative)
```

### Phase 4 -- Pointer-to-Global Grouping

The global-variable grouping deserves special attention. Consider two local pointers `p` and `q` that both derive from the same global array `g`:

```llvm
%p = getelementptr [1024 x float], ptr @g, i64 0, i64 %tid
%q = getelementptr [1024 x float], ptr @g, i64 0, i64 %tid2
```

Without the global extraction step, these would be in different groups (keyed by `%p` vs `%q`). The `sub_1CCDC20` helper walks through the pointer chain to find the underlying `@g`, allowing the pass to recognize that both addresses target the same global and can share a hoisted base.

## Cost-Benefit Analysis

The pass trades register pressure at the dominator for reduced address computation at use sites. This trade-off is particularly favorable on GPUs for two reasons:

**Benefit -- Reduced integer ALU pressure.** Each eliminated GEP chain frees integer ALU slots. On SM architectures, integer instructions compete for the same warp scheduler slots as floating-point instructions. A kernel with N memory operations sharing the same base saves up to (N-1) complete base address recomputations. For a kernel doing 8 loads from the same struct through different control-flow paths, this eliminates 7 redundant address computations.

**Cost -- Extended live range at the dominator.** The hoisted base must remain live from the dominator block down to every use site. On GPUs, each additional live register reduces occupancy (the number of concurrent warps per SM). The pass implicitly relies on the subsequent rematerialization pass (`sub_1CE7DD0`) to undo any hoisting decisions that prove too costly for register pressure -- if the hoisted value's live range crosses too many basic blocks, rematerialization will re-derive it closer to the use point.

The SCEV-CGP knob `scev-cgp-cross-block-limit` provides an explicit limit on how many common bases can be created from a single block, acting as a safety valve against excessive register pressure growth. The related `scev-cgp-idom-level-limit` constrains how far up the dominator tree the pass is willing to hoist.

## Relationship with Base Address Strength Reduction

The two passes operate at different granularities and are intentionally complementary:

| Aspect | Common Base Elimination | Base Address Strength Reduction |
|---|---|---|
| **Scope** | Inter-block (dominator-based) | Intra-loop (induction-variable-based) |
| **Target pattern** | Multiple BBs accessing the same base | Loop body with `base + stride * iv` |
| **Mechanism** | Hoist to common dominator | Factor out common base, use incremented pointer |
| **Key helper** | `sub_1C53170` (address decomposition) | `sub_1C53170` (same decomposition) |
| **Offset handling** | Minimum-offset anchor | Minimum-offset anchor (same strategy) |
| **Pipeline order** | Runs first | Runs after CBE |

The shared address decomposition helper (`sub_1C53170`) and the shared rewriting infrastructure (`sub_13A5B00` for creating new base computations, `sub_14806B0` for rewriting addresses) confirm that these passes were designed as a coordinated pair. Common Base Elimination runs first to eliminate inter-block redundancies, leaving BASR to focus on the remaining intra-loop stride patterns. Without CBE running first, BASR would encounter more diverse base expressions in loop bodies, reducing its grouping effectiveness.

Both passes share the same `0x1C50000`-`0x1CCFFFF` address range in the binary, and BASR's helper functions (e.g., `sub_1C637F0` -- base address bitcast helper, strings `"baseValue"`, `"bitCastEnd"`) are directly adjacent to CBE's entry point.

## Configuration

### Direct Knobs

No CBE-specific enable/disable knob has been identified in the binary. The pass appears to be unconditionally enabled when the SCEV-CGP subsystem is active.

### Related SCEV-CGP Knobs

| Knob | Type | Description |
|---|---|---|
| `scev-cgp-cross-block-limit` | int | Maximum number of common bases that can be created from a single block. Limits the register pressure increase from hoisting. |
| `scev-cgp-idom-level-limit` | int | Maximum dominator tree depth for hoisting. Prevents hoisting too far from use sites. |
| `do-scev-cgp` | bool | Master enable for the SCEV-CGP subsystem. Disabling this may also disable CBE. |
| `do-base-address-strength-reduce` | int | Two levels: 1 = basic, 2 = with conditions. Controls the companion BASR pass. |
| `do-base-address-strength-reduce-chain` | bool | Enables chained strength reduction in BASR. |
| `base-address-strength-reduce-iv-limit` | int | IV limit for BASR. |
| `base-address-strength-reduce-max-iv` | int | Maximum IVs considered by BASR. |

### BASR Aggressiveness Knob

The global `dword_4FBCAE0` controls aggressiveness for negative-offset handling in the BASR companion pass. When `dword_4FBCAE0 > 1`, BASR also considers base groups where the maximum offset has a negative sign bit, checking via `sub_1C51340` whether the base is loop-invariant before creating a separate common base via `sub_1C55CE0`. This knob does not directly affect CBE but influences how much address redundancy remains for CBE to handle.

## Diagnostic Strings

```
"Common Base Elimination"
```

The pass registers a single diagnostic string (its name). No additional debug/dump strings have been identified. The pass does not appear to have a dedicated dump knob analogous to `dump-base-address-strength-reduce` for BASR.

## Function Map

| Address | Size | Identity | Role |
|---------|------|----------|------|
| `sub_1C5DFC0` | 38 KB | `CommonBaseElimination::run` | Main entry point -- orchestrates all four phases |
| `sub_1C53170` | -- | `decomposeAddress` | Decomposes a memory address into `(base, offset_list, count)` tuple. Shared with BASR. |
| `sub_1C54050` | -- | `hashMapGrowOrRehash` | Hash map resize/rehash with load-factor policy |
| `sub_1C50900` | -- | `hashMapInsertOrLookup` | Insert into or look up in the base-pointer hash map |
| `sub_1CCDC20` | -- | `extractGlobalFromPointerChain` | Walks bitcast/GEP chains to find the underlying `GlobalVariable` |
| `sub_1C55CE0` | -- | `createCommonBaseForNegativeOffsets` | Creates a separate common base when the max offset is negative. Used by BASR, available to CBE. |
| `sub_1C51340` | -- | `isBaseLoopInvariant` | Checks whether a base address is loop-invariant |
| `sub_1C57390` | -- | `classifyAddressExpression` | Classifies an instruction's address expression type |
| `sub_13A5B00` | -- | `createNewBaseInstruction` | Creates a new base address computation at the insertion point |
| `sub_14806B0` | -- | `rewriteAddressAsBaseOffset` | Rewrites an address as `(new_base + relative_offset)` |
| `sub_1456040` | -- | `extractBasePointer` (SCEV helper) | Extracts the base pointer from an address expression (SCEV `getStart`/`getOperand(0)`) |

## Cross-References

- [Base Address Strength Reduction](./base-address-sr.md) -- the companion intra-loop pass
- [SCEV-CGP knobs](../llvm/scev.md#scev-cgp-knobs-address-mode-optimization) -- knobs controlling cross-block limits and IDOM depth
- [NVIDIA Custom Passes Overview](./index.md) -- pass inventory and registration
- [Rematerialization](./rematerialization.md) -- downstream pass that can undo costly hoisting by re-deriving values closer to use sites
- [Other NVIDIA Passes](./other.md) -- summary entries for CBE and BASR
- [LLVM Optimizer](../pipeline/optimizer.md) -- two-phase pipeline where CBE runs
