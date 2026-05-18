# Address-Space Vote Lattice

## Abstract

Interprocedural memory-space propagation classifies each generic pointer argument with a small flat lattice. Every call-site observation for one callee argument folds into one of three useful states: no decision yet, all observations agree on one concrete NVPTX address space, or conflicting observations make specialization unsafe.

The lattice is deliberately tiny — one bottom element, six concrete address spaces, one absorbing poison element. That suffices for the cloner to decide whether a callee can be cloned with a narrower pointer type, and the same partition is reused by the type converter and NVVM alias analysis.

NVIDIA GPUs expose seven memory spaces — global, shared, distributed shared, constant, local, tensor (Blackwell), and parameter — plus a generic encoding that names the union when provenance is unknown. [Memory Hierarchy and Data Flow](memory-hierarchy-and-dataflow.md) is the orientation page for those spaces: it tabulates the PTX state-space names, the LLVM address-space numbers, the MLIR encodings, and the data-flow patterns that move operands between them. This page focuses on the lattice machinery that infers which space a pointer names; the orientation page provides the why.

[Force-Inline and Specialize Callees](force-inline-and-specialize-callees.md) is the data-flow companion: it covers the worklist driver, the kernel/image/size thresholds that decide between inlining and specialization, and the call-site rewrite mechanics. This page covers only the lattice itself, the four red-black trees that hold its working state, and the `"nvvm.as"` attribute that publishes results across passes. NVVMAA uses the same six-way partition for `MayAlias`/`NoAlias`, so the lattice page doubles as the canonical reference for any consumer reasoning about per-pointer NVPTX address spaces.

## Worker Model

For each candidate function, the pass allocates one vote slot per argument. Pointer arguments start as `UNDET`. The classifier then walks the argument's uses at all relevant call sites and returns either a concrete NVPTX address space or a failure. Failure poisons the slot. Agreement keeps or adopts the concrete address space. Disagreement poisons the slot.

The outer algorithm is a fixed-point worklist. A successful clone or signature rewrite can make callers newly classifiable, so affected callers re-enqueue until the worklist drains or the clone budget is exhausted.

## Lattice

| Element | Value | Role | Lattice position |
| --- | ---:| --- | --- |
| `UNDET` | 1000 | no useful observation yet | bottom |
| `GLOBAL` | 1 | `addrspace(1)`, global memory | concrete address-space element |
| `SHARED` | 3 | `addrspace(3)`, shared memory | concrete address-space element |
| `CONST` | 4 | `addrspace(4)`, constant memory | concrete address-space element |
| `LOCAL` | 5 | `addrspace(5)`, local memory | concrete address-space element |
| `TMEM` | 6 | `addrspace(6)`, tensor memory | concrete address-space element |
| `DIST_SH` | 7 | `addrspace(7)`, distributed shared memory | concrete address-space element |
| `POISON` | 2000 | conflict or unclassifiable use | top, absorbing |
| `UNSET` | 256 | scratch state used inside one classifier invocation | outside vote slots |

The concrete address spaces form an antichain. None is more specific than another. The meet of
two distinct concrete address spaces is `POISON`.

```
                              TOP = POISON (2000)
                                   |
               +------+------+------+------+------+------+
               |      |      |      |      |      |      |
             GLOBAL SHARED CONST LOCAL  TMEM DIST_SH (1/3/4/5/6/7)
               |      |      |      |      |      |      |
               +------+------+------+------+------+------+
                                   |
                             UNDET (1000)
```

```c
AddressVote meet_address_vote(AddressVote old_vote, ClassifierResult observed) {
    if (!observed.valid) {
        return AS_POISON;
    }

    AddressVote new_vote = address_space_to_vote(observed.addrspace);
    if (old_vote == AS_UNDET) {
        return new_vote;
    }

    if (old_vote == new_vote) {
        return old_vote;
    }

    return AS_POISON;
}
```

`UNSET` must stay separate from the vote lattice. It is a scratch sentinel used while one use is
being classified; it should never be stored as the final vote for an argument.

## Clone-budget `DoCloneForIpMsp`

The clone budget is exposed as `DoCloneForIpMsp`, described as the maximal number of callees
specialized for a call base. It has three useful modes:

| Budget | Meaning |
| ---:| --- |
| `-1` | Unlimited cloning; this is the default. |
| `0` | No clone attempts; useful for inspecting candidates and diagnostics. |
| Positive `N` | Permit at most `N` clone attempts before suppressing further clones. |

The counter is attempt-based, not success-based. Charging attempts prevents recursive or ambiguous
call graphs from repeatedly trying the same specialization shape.

## Verbatim `--dump-ip-msp` transcript strings

When `DumpIpMsp` is enabled, the pass emits a compact transcript. The strings are intentionally
simple because they describe the fixed-point worklist:

```text
Initial work list size :
 arguments)
 is cloned
avoid cloning of
 callees are affected
 : changed in argument memory space (
 : return memory space is resolved :
```

The first line reports seed size. The argument-memory-space line reports resolved parameter slots.
`avoid cloning of` marks a budget-suppressed candidate. `is cloned` marks a successful clone.
`callees are affected` reports callers re-enqueued after a rewrite. The return-space line reports
the same lattice decision applied to return values.

## Pass Entry `sub_281D480`

`sub_281D480` is `IPMSPPass::run`, the function-level entry point of the Intra-Procedural Memory-Space Propagation pass. It reads `DoCloneForIpMsp`, `DumpIpMsp`, and the small handful of threshold options that tune the lattice, allocates the four per-function red-black trees described below, then dispatches the seeded worklist into the driver core `sub_2858070`. On terminate it hands the converged lattice state to the I08 type converter `sub_2882E00`, where the inferred address spaces become a real `"nvvm.as"` attribute on the function.

The entry function is the only place that knows the pass-option layout. Everything below it works on raw lattice values and tree handles, which keeps the driver core small enough to inline its inner update step.

## Driver Core `sub_2858070`

`sub_2858070` is the driver body of the pass: 3 784 bytes of code, 234 basic blocks. It walks each function's basic blocks, lifts every pointer value to its address-space lattice element, and propagates joins across SSA def-use edges. Case analysis on MLIR operation kinds plus the four-tree update fast paths dominate the 234-block fan-out; the actual control flow is a single worklist loop guarded by a per-block budget cap.

The driver reads pointer address spaces directly out of `PointerType` rather than through the public MLIR accessor. Pointer types in tileiras carry their AS in `PointerType::getSubclassData()`, reached via a mask-less `>> 8` of the `kindAndSubclass` u32 at `Type+0x10`. The call form inlines poorly, so the binary keeps a hand-rolled read in the inner loop, preventing the address-space read from showing up as a non-trivial call in the propagation hot path.

## The Four RB-Trees

The driver keeps four red-black trees of per-block lattice state. Each tree is a libc++
`std::_Rb_tree` with the canonical `_Rb_tree_increment` / `_Rb_tree_decrement` callees; they are
the entire mutable working set of the pass.

| Tree | Keyed by | Value |
|---|---|---|
| argmask | `BlockArgument *` | AS bitmask propagated through PHI/select |
| clone-count | `Block *` | how many times this block has been re-visited (for budget cap) |
| vote | `Value *` | lattice vote (one of the 10 elements) |
| return-space | `Function *` | inferred AS for the function's return-value pointer (if any) |

The argmask tree carries a bitmask, not a single lattice element, because PHI and select merges must remember every concrete AS that reached the block argument before the lattice meet collapses them. The clone-count tree governs convergence: every visit increments the count for the value's parent block, and the driver bails out of the worklist as soon as any block exceeds `kBudgetCap`. The vote tree holds the lattice value that the cross-link section consumes. The return-space tree is populated only for functions whose return type is a pointer — the analogue of the per-argument vote slot for the result.

## Driver Pseudocode

```c
LogicalResult IPMSPPass::run(Function *F) {
    RBTrees state = allocRBTrees();
    WorkList wl = seedFromArguments(F);
    while (!wl.empty()) {
        Value *v = wl.pop();
        ASLattice newAS = visit(v, state);
        if (newAS != getOldAS(state, v)) {
            updateLattice(state, v, newAS);
            wl.pushUses(v);
        }
        ++state.cloneCount[parentBlock(v)];
        if (state.cloneCount[parentBlock(v)] > kBudgetCap)
            return failure();   // give up
    }
    typeConverter.install(F, state);    // sub_2882E00
    return success();
}
```

`kBudgetCap` defaults to 1024 visits per block. Exceeding it means the lattice failed to converge in a reasonable time, and the pass leaves the function unchanged rather than guess. The fall-back is deliberate: a poisoned slot is correct (the cross-link section already defines `POISON` as absorbing), but a non-converged function should not be specialized at all.

## I08 Type Converter `sub_2882E00`

`sub_2882E00` is the I08 type converter that runs after the lattice has converged. It is the only
component that turns the lattice's in-memory state into observable IR. For each function it:

1. Reads the inferred AS for each pointer argument from the vote tree.
2. Installs a `"nvvm.as"` function attribute on the function, with the inferred AS values
   serialized as a `DenseI32ArrayAttr`.
3. Leaves the actual pointer types alone; rewriting argument types is the cloner's job, not the
   converter's.

The downstream NVPTX backend reads `"nvvm.as"` via the standard
`mlir::nvvm::isPointerInAddressSpace()` API and emits the corresponding PTX AS modifier
(`global`, `shared`, `local`, `const`, `tmem`, `dist_shared`) on every load/store that touches the
argument. The attribute is therefore the public contract between the lattice and the rest of the
toolchain: everything upstream of `sub_2882E00` is internal lattice state, and everything
downstream sees only the array attribute.

## Cross-link

The same six-way partition is consumed by other NVVM components. The type converter writes the
resolved address space into specialized function signatures, so the backend sees
`ptr addrspace(N)` instead of a generic `ptr`. `TMEM` and `DIST_SH` are first-class participants;
excluding them would downgrade Blackwell tensor-memory and distributed-shared kernels back to
generic address conversions.

NVVMAA uses the same partition for aliasing:

```c
AliasResult nvvm_alias(AddressSpace a, AddressSpace b) {
    if (a == ADDRSPACE_GENERIC || b == ADDRSPACE_GENERIC) {
        return MayAlias;
    }

    if (a != b) {
        return NoAlias;
    }

    return MayAlias;
}
```

That mirrors the lattice. A poisoned vote means specialization cannot prove one concrete space, so alias analysis falls back to `MayAlias`. Distinct concrete address spaces are disjoint.

