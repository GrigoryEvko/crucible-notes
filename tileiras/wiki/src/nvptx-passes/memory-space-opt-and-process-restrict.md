# Memory Space Optimization and Restrict Processing

## Abstract

This cluster prepares pointer provenance for NVPTX codegen. It specializes generic-pointer callees whose callers consistently pass concrete address spaces, rewrites provable generic pointers inside each function, and translates `__restrict__` into alias metadata. The payoff is both correctness and quality — the backend gets to emit direct global, shared, constant, local, tensor-memory, or distributed-shared operations instead of dragging generic conversions through the pipeline.

Ordering is deliberate. Inter-procedural specialization runs first, function-local memory-space optimization second, and restrict processing last, once pointer forms have become more concrete.

## Address-Space Lattice

The shared lattice is flat and conservative.

| Element | Meaning |
| --- | --- |
| Unknown | No useful evidence yet. |
| Global | Device global memory. |
| Shared | CTA-local shared memory. |
| Constant | Constant memory or grid-constant parameters. |
| Local | Per-thread local memory. |
| Tensor memory | Blackwell tensor-memory space. |
| Distributed shared | Cluster-wide shared memory. |
| Generic | Conflicting or unknown-at-boundary provenance. |

```c
AddressSpace vote(AddressSpace current, AddressSpace observed) {
    if (observed == AS_UNKNOWN) {
        return current;
    }
    if (current == AS_UNKNOWN) {
        return observed;
    }
    if (current == observed) {
        return current;
    }
    return AS_GENERIC;
}
```

Tensor-memory and distributed-shared spaces are first-class elements of the lattice. Folding them into ordinary generic memory would keep unnecessary `cvta` conversions in precisely the code that needs the most accurate state-space lowering.

## Callee Specialization

The inter-procedural pass hunts for helper functions with generic pointer parameters. When every use of one parameter agrees on a concrete address space, it clones the helper with a specialized signature and retargets matching call sites.

```c
void specialize_generic_pointer_callees(Module module, int clone_budget) {
    WorkList work = collect_candidate_helpers(module);

    while (!work.empty()) {
        Function fn = work.pop();
        VoteVector votes = collect_argument_votes(fn);

        if (!has_specializable_vote(votes)) {
            continue;
        }
        if (clone_budget_exceeded(fn, clone_budget)) {
            continue;
        }

        Function clone = clone_function(fn);
        rewrite_pointer_argument_spaces(clone, votes);
        mark_internal_alwaysinline(clone);

        for (CallBase call : users_of(fn)) {
            if (call_arguments_match_votes(call, votes)) {
                retarget_call(call, clone);
                work.push(caller_function(call));
            }
        }
    }
}
```

Termination follows from monotonicity: each argument vote moves only from unknown to a concrete space or to generic conflict. Clone budgets bound recursive helper families without weakening the lattice.

## MemorySpaceOpt

MemorySpaceOpt tags every generic pointer in the function with a concrete address space by walking the SSA def-use chains backward from each dereference toward kernel arguments and other roots that already carry provenance. It runs after `LowerStructArgs` (which promotes byval struct parameters into explicit pointer arguments) and before `ProcessRestrict` (which attaches alias scopes). Every pointer-typed SSA value leaves the pass tagged with a concrete AS (`1=global`, `3=shared`, `5=local`, `4=constant`, and so on) or with `0=generic` when inference fails to converge on a single space.

### SSA Inference Walker (`sub_28469E0`, ~10.7 KB)

The walker is the pass's primary engine. It maintains a lattice `{pointer SSA value -> AS tag}`, seeds it from kernel-argument attributes, and propagates through every pointer-defining opcode:

```c
void memspace_walker(Function fn, Lattice *lat) {
    for (Argument arg : fn.arguments) {
        if (has_attr(arg, KERNEL_POINTER)) {
            lat_seed(lat, arg, AS_GLOBAL);            // 1
        } else if (has_attr(arg, GRID_CONSTANT)) {
            lat_seed(lat, arg, AS_CONSTANT);          // 4
        } else if (has_attr(arg, NVVM_BYVAL)) {
            lat_seed(lat, arg, AS_GENERIC);           // 0 - needs cast at deref
        }
    }

    while (lat_has_changes(lat)) {
        for (Instruction inst : fn.pointer_instructions) {
            switch (inst.opcode) {
            case GEP:
            case BITCAST:
                lat_propagate(lat, inst, lat_get(lat, inst.operand[0]));
                break;
            case SELECT:
            case PHI:
                lat_propagate(lat, inst, lat_meet_all_incoming(lat, inst));
                break;
            case ADDR_SPACE_CAST:
                lat_propagate(lat, inst, inst.target_as);
                break;
            case LOAD:
            case STORE:
            case ATOMIC:
                lat_consume(lat, inst.pointer_operand);
                break;
            }
        }
    }
}
```

The lattice meet rule from earlier in this page is the join operator at PHI and Select fan-in. AddrSpaceCast is the only opcode that does not inherit from its operand — it forces the destination AS regardless of the source value's tag. Kernel-argument-derived pointers reach load/store/atomic sites already concrete; only values that cross a true generic boundary (a byval pointer, an opaque external return, an unhandled intrinsic) stay at lattice bottom.

### Diagnostic and Rewriter (`sub_285DB30`)

Once the walker reaches fixed point, the rewriter visits every pointer-typed instruction, attaches the inferred AS as a metadata tag, and emits diagnostics on any lattice-bottom value. Three diagnostic strings come straight from the binary:

| Site | Diagnostic |
|---|---|
| atomic op on AS=5 | ``"Cannot do atomic on local memory"`` |
| lattice-top unreachable | ``"assuming global memory space"`` |
| value remains at bottom | ``"Cannot tell what pointer points to"`` |

The first fires before instruction selection and stops the backend from emitting a local-memory atomic the architecture doesn't support. The second is the fallback when no kernel-argument seed reached the dereference: the rewriter assumes global and continues. The third is the only hard failure path — the pointer stays at `AS_GENERIC` so a later `cvta` survives into PTX.

### Opcode-79 Cast Folder (`sub_285F390`)

NVPTX MI opcode `79` is the private `CVT_GENERIC_TO_AS_LOCAL` form. The folder removes redundant casts left by the frontend or produced by the walker's own AS rewrites. Three rewriting rules cover the entire fixed point:

```c
Value fold_addrspace_cast(Lattice *lat, Instruction cast) {
    Value src = cast.operand[0];
    AddressSpace dst = cast.target_as;

    // (1) cast to the AS the operand already has -> drop the cast.
    if (lat_get(lat, src) == dst) {
        return src;
    }

    // (2) cast of a cast -> collapse to a single cast with the outer target.
    if (src.opcode == ADDR_SPACE_CAST && src.target_as != dst) {
        return make_addrspace_cast(src.operand[0], dst);
    }

    // (3) kernel pointer arg already global -> drop the cast to global.
    if (has_attr(src, KERNEL_POINTER) && dst == AS_GLOBAL) {
        return src;
    }

    return cast;
}
```

Rule (1) is the common case once the walker has tagged `cvta.to.global` results; rule (2) collapses the back-to-back casts the frontend emits when a generic pointer is briefly routed through `cvta` and immediately cast again; rule (3) handles the canonical `cast(KERNEL_PTR, GLOBAL)` shape produced by source code that re-asserts the argument's known space.

### Atomic and WMMA Predicates

Two predicates gate the diagnostics and the AS-forcing path:

| Predicate | Behavior |
|---|---|
| `isAtomicOpcode(op)` | true for `op` in `[8305, 8362]`; consulted by the AS=5 atomic diagnostic |
| `isWmmaOp(op)`       | true for `op` in `{8351, 8353}` (``wmma.load.sync`` / ``wmma.store.sync``) |

The WMMA predicate runs before propagation. When it fires, the walker forces the operand pointer to `AS_GLOBAL` regardless of the lattice state — the wmma async-load/store family is defined only against global memory, and codegen would otherwise leave the operand generic and stall the kernel.

### `cl::opt` Flag Bytes

Four `.bss` flag bytes configure the pass at startup. They surface to the driver as `cl::opt` so each one can be overridden from the command line in debug builds:

| Symbol | Default | Role |
|---|---|---|
| `byte_5B6CAC0` | 1 | enable MemorySpaceOpt |
| `byte_5B6CC40` | 0 | emit verbose lattice trace |
| `byte_5B6CDC0` | 0 | force conservative inference (treat unknowns as generic immediately) |
| `unk_5B6CF80` | enum | alias-set merging policy (3-bit enum) |

The conservative-inference flag gets flipped most often during regression triage. It short-circuits the lattice the first time a value fails to acquire a concrete AS, which makes diff-style comparisons against an older toolchain easier to read.

## Restrict Processing

Restrict processing turns frontend `__restrict__` intent into LLVM `noalias` attributes on pointer arguments and into `nvvm.restrict_*` metadata on every load and store reached from a restricted root. It runs after MemorySpaceOpt because the propagation worker consults the inferred address-space tag when deciding whether a derived pointer is global; the reverse order would over-restrict shared and local pointers and degrade downstream alias analysis. The output feeds the NVPTX alias-analysis pipeline and ultimately reaches the backend scheduler as a noalias guarantee.

The per-function worker `sub_2867840` runs once per function in the module. It checks an idempotency attribute, walks each restrict-tagged pointer argument, propagates the restrict qualifier through the def-use graph, and stamps every derived pointer with a scope identifier. Loads and stores reached through a restricted pointer pick up matching metadata so the alias-analysis layer can correlate the memory operation back to its restricted root.

The pass attaches six distinct attribute and metadata strings to the IR.

| Key | Purpose |
|---|---|
| `nvvm.restrict_processed` | Function attribute marking that this function has been processed (prevents re-entry) |
| `nvvm.restrict_scope` | Per-pointer attribute carrying the restrict scope ID |
| `nvvm.restrict_keyword` | Per-pointer attribute carrying the original keyword form (`restrict` vs `__restrict__`) |
| `user_specified_restrict_scope` | Source-level annotation as parsed by the front-end |
| `user_specified_restrict_keyword` | Source-level annotation, preserved verbatim for diagnostics |
| `"function contains restrict keyword in struct"` | Diagnostic emitted when a restrict-qualified pointer is found inside a struct field |

The `user_specified_*` variants are the front-end deposit; the `nvvm.restrict_*` variants are this pass's canonicalized form. Both stay on the IR because later diagnostic passes need the original keyword spelling, while alias analysis reads only the canonical scope.

Four `cl::opt` knobs control the pass. The bss slots are reconstructed from the cl::opt instantiation block and may shift slightly between builds.

| Knob | bss slot | Default | Meaning |
|---|---|---|---|
| `process-restrict` | `byte_5B6CCC0` | 1 | Master enable; setting to 0 disables the entire pass |
| `allow-restrict-in-struct` | `byte_5B6CCC8` | 0 | Permit struct-field restrict; otherwise emit the diagnostic above |
| `apply-multi-level-restrict` | `byte_5B6CCD0` | 0 | Walk through two or more levels of pointer indirection |
| `dump-process-restrict` | `byte_5B6CCD8` | 0 | Print before/after IR for debugging |

The default policy is conservative: only direct-argument restrict propagates, struct-field restrict gets rejected with a diagnostic, and multi-level indirection is left alone. The two opt-in knobs exist for code bases that rely on more aggressive aliasing assumptions; the dump knob is strictly a debugging aid.

```c
LogicalResult processRestrict(Function *F) {
    if (F->hasAttr("nvvm.restrict_processed")) {
        return success();
    }

    for (Argument &arg : F->args()) {
        if (!hasRestrictAnnotation(arg)) {
            continue;
        }
        propagateRestrict(arg, /*scopeId=*/nextScopeId());
    }

    F->setAttr("nvvm.restrict_processed", UnitAttr::get(ctx));
    return success();
}

void propagateRestrict(Value *root, uint32_t scopeId) {
    WorkList wl({root});

    while (!wl.empty()) {
        Value *v = wl.pop();
        attachAttr(v, "nvvm.restrict_scope", IntAttr(scopeId));

        for (User *u : v->users()) {
            if (isPointerArithmetic(u)) {
                wl.push(u);
            }
            if (isLoadOrStore(u)) {
                attachLoadStoreMD(u, scopeId);
            }
        }
    }
}
```

The worker's entry path leads with the per-function idempotency check. The `nvvm.restrict_processed` attribute prevents accidental re-entry when a later pass clones or specializes a function and runs the cluster again, and it gives the rest of the pipeline a cheap way to ask whether restrict metadata is already canonicalized. The worklist is a flat traversal of the def-use graph: pointer-arithmetic users stay in the frontier and load/store users terminate it with a metadata stamp. `apply-multi-level-restrict` gates the only place where the walker is allowed to recurse through a pointer-of-pointer load.

Restrict metadata is not a proof of address space. It is a noalias relation among pointer families. MemorySpaceOpt and ProcessRestrict cooperate but do not replace each other — the former tells the backend which state space to use, the latter tells alias analysis which pointer pairs cannot overlap.

## Operational Knobs

These passes expose useful controls in debugging and testing builds:

| Knob family | Purpose |
| --- | --- |
| Clone budget | Bounds inter-procedural specialization on recursive or template-heavy helper graphs. |
| Dump memory-space propagation | Prints specialization decisions and affected callees. |
| Process restrict enable | Allows disabling restrict metadata generation for differential testing. |
| Propagate-only restrict mode | Reapplies already-stamped scopes after another pass creates new derived values. |
| Multi-level restrict mode | Follows `T**` and deeper pointer chains when frontend metadata requested it. |

