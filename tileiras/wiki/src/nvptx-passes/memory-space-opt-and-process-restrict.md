# Memory Space Optimization and Restrict Processing

## Abstract

This cluster prepares pointer provenance for NVPTX codegen. It specializes generic-pointer callees whose callers consistently pass concrete address spaces, rewrites provable generic pointers inside each function, and translates `__restrict__` into alias metadata. The payoff is both correctness and quality — the backend gets to emit direct global, shared, constant, local, tensor-memory, or distributed-shared operations instead of dragging generic conversions through the pipeline.

For an overview of GPU memory spaces and how they appear at each compilation stage, see [Memory Hierarchy and Data Flow](../topics/memory-hierarchy-and-dataflow.md).

Ordering is deliberate. Inter-procedural specialization runs first, function-local memory-space optimization second, and restrict processing last, once pointer forms have become more concrete.

## Address-Space Lattice

Every pointer-typed SSA value carries an inferred address space. The lattice is two-tier and finite-height: BOTTOM at the unknown root, GENERIC at the conflicting top, and one concrete element per NVPTX address space sitting between them.

```text
                            GENERIC (top)
                                ↑
            ┌────────┬──────────┼──────────┬────────┐
            │        │          │          │        │
          GLOBAL  SHARED    CONSTANT     LOCAL    TENSOR
          (AS=1)  (AS=3)    (AS=4)      (AS=5)    (AS=6)
            │        │          │          │        │
            └────────┴──────────┼──────────┴────────┘
                                ↑
                            BOTTOM (⊥, unknown)
```

The ordering captures `BOTTOM ⊑ GLOBAL ⊑ GENERIC`, `BOTTOM ⊑ SHARED ⊑ GENERIC`, `BOTTOM ⊑ LOCAL ⊑ GENERIC`, `BOTTOM ⊑ CONSTANT ⊑ GENERIC`, `BOTTOM ⊑ TENSOR ⊑ GENERIC`, and `BOTTOM ⊑ DISTRIBUTED_SHARED ⊑ GENERIC`. The meet of two distinct concrete spaces is GENERIC; the meet of any element with BOTTOM is the other element.

```c
AddressSpace meet(AddressSpace current, AddressSpace observed) {
    if (observed == AS_BOTTOM) return current;
    if (current == AS_BOTTOM)  return observed;
    if (current == observed)   return current;
    return AS_GENERIC;
}
```

### Termination

The lattice has finite height: every chain `BOTTOM ⊏ AS ⊏ GENERIC` has length two, so any SSA value can be refined at most twice (BOTTOM → AS → GENERIC). The worklist iterates until no value's tag changes; with `n` pointer-typed values, the iteration count is bounded by `2n` regardless of CFG shape. The propagation is therefore a standard finite-height-lattice fixpoint and terminates without an explicit budget.

| Element | Meaning |
| --- | --- |
| BOTTOM | No useful evidence yet. |
| GLOBAL | Device global memory (`addrspace(1)`). |
| SHARED | CTA-local shared memory (`addrspace(3)`). |
| CONSTANT | Constant memory or grid-constant parameters (`addrspace(4)`). |
| LOCAL | Per-thread local memory (`addrspace(5)`). |
| TENSOR | Blackwell tensor memory. |
| DISTRIBUTED_SHARED | Cluster-wide shared memory. |
| GENERIC | Conflicting or unknown-at-boundary provenance (`addrspace(0)`). |

Tensor-memory and distributed-shared spaces are first-class elements of the lattice. Folding them into ordinary generic memory would keep unnecessary `cvta` conversions in precisely the code that needs the most accurate state-space lowering.

## Callee Specialization

The inter-procedural part of MemorySpaceOpt hunts for helper functions with generic pointer parameters. When every call site to a helper passes one parameter in the same concrete address space, the pass clones the helper with a specialized signature and retargets matching calls.

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

Termination follows from the lattice argument earlier in this page. Each argument vote can be refined at most twice (BOTTOM → concrete → GENERIC), the iteration count is bounded by `2 × (helpers × pointer parameters)`, and the clone budget bounds the worst case for recursive helper families without weakening the lattice.

## MemorySpaceOpt

### Input and Output IR Shape

MemorySpaceOpt is an inter-procedural address-space inference pass. It consumes LLVM IR where pointer-typed values may carry the generic address space `addrspace(0)` and produces the same IR with as many of those values as possible retagged to their concrete address space. The pass runs after `LowerStructArgs` (which promotes byval struct parameters into explicit pointer arguments) and before `ProcessRestrict` (which attaches alias scopes).

```llvm
; before: generic-pointer chains
define void @child(ptr %p, i32 %i) {
  %a = getelementptr i32, ptr %p, i32 %i
  %v = load i32, ptr %a, align 4
  store i32 %v, ptr %a, align 4
  ret void
}

define void @kernel(ptr addrspace(1) %g, i32 %i) {
  %cast = addrspacecast ptr addrspace(1) %g to ptr
  call void @child(ptr %cast, i32 %i)
  ret void
}

; after: callee specialized, generic cast and inner GEP retagged
define internal void @child.global(ptr addrspace(1) %p, i32 %i) alwaysinline {
  %a = getelementptr i32, ptr addrspace(1) %p, i32 %i
  %v = load i32, ptr addrspace(1) %a, align 4
  store i32 %v, ptr addrspace(1) %a, align 4
  ret void
}

define void @kernel(ptr addrspace(1) %g, i32 %i) {
  call void @child.global(ptr addrspace(1) %g, i32 %i)
  ret void
}
```

### Matching Predicate

The pass propagates AS tags through the SSA def-use graph and through calls. A pointer value `v` reaches the concrete tag `AS` iff every reaching definition of `v` (via GEP, bitcast, PHI, select, addrspacecast, parameter binding) refines to `AS` under the lattice meet. The pass is intra-procedural for stores, GEPs, and PHIs, but inter-procedural across calls and returns: when every call site to a function passes pointers in the same concrete AS, the callee gets cloned with refined parameter types and matching calls are redirected to the clone.

### Lattice Propagation Walker

The walker seeds the lattice from kernel-argument attributes and propagates through every pointer-defining opcode until fixpoint.

```c
void memspace_walker(Function fn, Lattice *lat) {
    for (Argument arg : fn.arguments) {
        if (has_attr(arg, KERNEL_POINTER)) {
            lat_seed(lat, arg, AS_GLOBAL);
        } else if (has_attr(arg, GRID_CONSTANT)) {
            lat_seed(lat, arg, AS_CONSTANT);
        } else if (has_attr(arg, NVVM_BYVAL)) {
            lat_seed(lat, arg, AS_GENERIC);  // needs cast at every deref
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
                lat_propagate(lat, inst, inst.target_as);   // forces dst AS
                break;
            case CALL:
                lat_propagate_call_args(lat, inst);         // backward to caller
                lat_propagate_call_ret(lat, inst);          // forward from callee
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

The lattice meet rule is the join operator at PHI and Select fan-in. AddrSpaceCast is the only opcode that does not inherit from its operand — it forces the destination AS regardless of the source value's tag. Kernel-argument-derived pointers reach load/store/atomic sites already concrete; only values that cross a true generic boundary (a byval pointer, an opaque external return, an unhandled intrinsic) stay at lattice bottom.

### Diagnostic Rewriter and Catalog

Once the walker reaches fixed point, the rewriter visits every pointer-typed instruction, attaches the inferred AS, and emits a diagnostic for every pointer that did not converge. Three diagnostic strings reach the user:

| Site | Diagnostic | Severity |
|---|---|---|
| atomic op on a pointer inferred as LOCAL | `Cannot do atomic on local memory` | error |
| dereference of a still-unconcrete pointer | `assuming global memory space` | warning |
| value remains at lattice BOTTOM | `Cannot tell what pointer points to` | warning |

The first fires before instruction selection and stops the backend from emitting a local-memory atomic the architecture doesn't support. The second is the fallback when no kernel-argument seed reached the dereference: the rewriter assumes global and continues. The third is the soft failure: the pointer stays at GENERIC so a later `cvta` survives into PTX, with a corresponding cost at runtime.

### WMMA Forces Global

A specific family of intrinsics — the WMMA load/store and the related async-bulk family — is defined only against global memory. Their operand pointers cannot reach codegen as generic without a stall, so the pass treats them as a backward constraint: at a WMMA call site the operand pointer's lattice tag is meet-refined with GLOBAL, and that refinement propagates back through the def-use graph until it either hits a kernel argument (which is already global) or contradicts another concrete tag (which becomes GENERIC and surfaces as the warning above).

```c
void apply_wmma_constraint(Lattice *lat, Instruction inst) {
    if (is_wmma_pointer_intrinsic(inst)) {
        Value *p = inst.pointer_operand;
        lat_refine_backward(lat, p, AS_GLOBAL);  // propagate AS_GLOBAL up the chain
    }
}
```

The refinement is one-directional: WMMA forces GLOBAL on the operand chain but never demotes a value that already proved itself in a different concrete space. A contradicting tag terminates with the lattice top (GENERIC) and the user sees the WMMA failure as a verifier diagnostic.

### Addrspacecast Folder

NVPTX frontends emit a number of `addrspacecast` instructions that become no-ops once the walker has refined the source pointer. The folder applies three rewriting rules at fixpoint:

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

Rule (1) is the common case once the walker has tagged `cvta.to.global` results; rule (2) collapses the back-to-back casts the frontend emits when a generic pointer is briefly routed through a cast and immediately cast again; rule (3) handles the canonical `cast(KERNEL_PTR, GLOBAL)` shape produced by source code that re-asserts the argument's known space.

### Tunables

Four `cl::opt` knobs configure the pass:

| Knob | Default | Meaning |
|---|---|---|
| `memspace-opt-enable` | 1 | Master enable. |
| `memspace-opt-verbose` | 0 | Emit verbose lattice trace. |
| `memspace-opt-conservative` | 0 | Force conservative inference: treat unknowns as GENERIC on first contact. |
| `memspace-opt-alias-merge` | enum | Alias-set merging policy (none / per-AS / unified). |

The conservative-inference flag short-circuits the lattice the first time a value fails to acquire a concrete AS, which makes diff-style comparisons against an older toolchain easier to read during regression triage.

### Failure Modes

- **Generic survives to atomic-on-LOCAL.** A backward propagation that reaches `addrspace(5)` on an atomic pointer is a hard error; the pass refuses to lower it.
- **WMMA constraint contradiction.** A WMMA operand reached through a shared-memory chain is unrepresentable; the verifier rejects the kernel.
- **Cast folder loop.** Without rule (2)'s outer-target preservation, two back-to-back casts could ping-pong forever. The folder applies rules in order and only fires once per cast per round.

## Restrict Processing

### Input and Output IR Shape

`ProcessRestrict` turns frontend `__restrict__` intent into LLVM `noalias` attributes on pointer arguments and into `nvvm.restrict_*` metadata on every load and store reached from a restricted root. It runs after [MemorySpaceOpt](#memoryspaceopt) because the propagation worker consults the inferred address-space tag when deciding whether a derived pointer is global; the reverse order would over-restrict shared and local pointers and degrade downstream alias analysis. The output feeds the NVPTX alias-analysis pipeline and ultimately reaches the backend scheduler as a noalias guarantee.

```llvm
; before
define void @k(ptr addrspace(1) %a, ptr addrspace(1) %b)
    "user_specified_restrict_scope"="1"
    "user_specified_restrict_keyword"="__restrict__" {
  %va = load i32, ptr addrspace(1) %a, align 4
  %vb = load i32, ptr addrspace(1) %b, align 4
  store i32 %va, ptr addrspace(1) %b, align 4
  ret void
}

; after
define void @k(ptr addrspace(1) noalias %a, ptr addrspace(1) noalias %b)
    "nvvm.restrict_processed"
    "nvvm.restrict_scope"="1"
    "nvvm.restrict_keyword"="__restrict__" {
  %va = load i32, ptr addrspace(1) %a, align 4, !alias.scope !0, !noalias !1
  %vb = load i32, ptr addrspace(1) %b, align 4, !alias.scope !1, !noalias !0
  store i32 %va, ptr addrspace(1) %b, align 4, !alias.scope !1, !noalias !0
  ret void
}
!0 = !{!2}
!1 = !{!3}
!2 = !{!"restrict.scope.1.a", !4}
!3 = !{!"restrict.scope.1.b", !4}
!4 = !{!"restrict.domain.k"}
```

The transformation has two effects. The argument attributes change from the front-end's `user_specified_*` form to the canonical `nvvm.restrict_*` form and gain a real `noalias` attribute that LLVM's alias analysis honors directly. Every load and store derived from a restricted argument acquires `!alias.scope` and `!noalias` metadata pointing at the per-argument scope, which is what teaches the backend scheduler that the two pointer families never overlap.

### Matching Predicate

A pointer argument is in scope iff it carries a `user_specified_restrict_scope` annotation or one of the legacy spellings the front-end emits. The propagation predicate, applied at every def-use edge, is: an SSA value is derived from a restricted root iff every reaching definition arrives through GEP, bitcast, or address-space cast from a restricted root (or from another value already proved derived). PHI and select join multiple roots and produce a derived-from-multiple-scopes value, which receives the meet of the contributing scopes in its alias-scope metadata.

### Attribute Keys: Frontend vs Canonical

The pass operates over six attribute keys split between the front-end's annotation form and the canonical form alias analysis actually reads.

| Front-end key | Canonical key | Purpose |
|---|---|---|
| `user_specified_restrict_scope` | `nvvm.restrict_scope` | Per-pointer scope ID. |
| `user_specified_restrict_keyword` | `nvvm.restrict_keyword` | Keyword form (`restrict` vs `__restrict__`) preserved for diagnostics. |
| `user_specified_restrict_processed` | `nvvm.restrict_processed` | Function-level idempotency marker; prevents re-entry. |

The `user_specified_*` variants are the frontend's deposit; the `nvvm.restrict_*` variants are this pass's canonical form. The frontend keys stay on the IR after canonicalization because later diagnostic passes need the original keyword spelling for source-line error messages, while alias analysis reads only the canonical form. A reimplementation that drops the frontend keys after canonicalization compiles correctly but loses the original spelling in diagnostics.

The diagnostic `"function contains restrict keyword in struct"` fires when a restrict-qualified pointer is found inside a struct field; the default policy rejects that shape because the pass does not propagate restrict through aggregate loads.

### Tunables

Four `cl::opt` knobs control the pass:

| Knob | Default | Meaning |
|---|---|---|
| `process-restrict` | 1 | Master enable. |
| `allow-restrict-in-struct` | 0 | Permit struct-field restrict; otherwise emit the diagnostic above. |
| `apply-multi-level-restrict` | 0 | Walk through two or more levels of pointer indirection. |
| `dump-process-restrict` | 0 | Print before/after IR for debugging. |

The default policy is conservative: only direct-argument restrict propagates, struct-field restrict gets rejected with a diagnostic, and multi-level indirection is left alone. The two opt-in knobs exist for code bases that rely on more aggressive aliasing assumptions; the dump knob is strictly a debugging aid.

### Algorithm

```c
LogicalResult processRestrict(Function *F) {
    if (F->hasAttr("nvvm.restrict_processed")) {
        return success();
    }

    for (Argument &arg : F->args()) {
        if (!hasRestrictAnnotation(arg)) {
            continue;
        }
        uint32_t scopeId = nextScopeId();
        canonicalize_keys(arg);                     // user_specified_* -> nvvm.restrict_*
        attachNoaliasAttr(arg);
        propagateRestrict(arg, scopeId);
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
                attachAliasScopeMD(u, scopeId);
            }
            if (isPointerLoad(u) && apply_multi_level_restrict) {
                wl.push(u);                          // recurse through T**
            }
        }
    }
}
```

The worker's entry path leads with the per-function idempotency check. The `nvvm.restrict_processed` attribute prevents accidental re-entry when a later pass clones or specializes a function and runs the cluster again, and it gives the rest of the pipeline a cheap way to ask whether restrict metadata is already canonicalized. The worklist is a flat traversal of the def-use graph: pointer-arithmetic users stay in the frontier and load/store users terminate it with a metadata stamp. `apply-multi-level-restrict` gates the only place where the walker is allowed to recurse through a pointer-of-pointer load.

### Failure Modes

- **Struct-field restrict.** With `allow-restrict-in-struct` off, the pass refuses to propagate from a restrict-qualified pointer reached through a struct member load and emits the diagnostic. Flipping the knob enables the propagation but the user accepts responsibility for the relaxed aliasing.
- **PHI of two restricted roots.** The two scopes do not meet to a single canonical scope; the PHI inherits both as a noalias set, which is correct but weakens the alias analysis at the merge.
- **Re-entry without canonical key.** A pass that clones a function and copies `user_specified_*` but not `nvvm.restrict_processed` will re-enter and emit a fresh scope, breaking the alias analysis cache. The idempotency attribute exists precisely to make the re-entry detectable.

Restrict metadata is not a proof of address space. It is a noalias relation among pointer families. [MemorySpaceOpt](#memoryspaceopt) and [Restrict Processing](#restrict-processing) cooperate but do not replace each other — the former tells the backend which state space to use, the latter tells alias analysis which pointer pairs cannot overlap.

## Operational Knobs

These passes expose useful controls in debugging and testing builds:

| Knob family | Purpose |
| --- | --- |
| Clone budget | Bounds inter-procedural specialization on recursive or template-heavy helper graphs. |
| Dump memory-space propagation | Prints specialization decisions and affected callees. |
| Process restrict enable | Allows disabling restrict metadata generation for differential testing. |
| Propagate-only restrict mode | Reapplies already-stamped scopes after another pass creates new derived values. |
| Multi-level restrict mode | Follows `T**` and deeper pointer chains when frontend metadata requested it. |

## Cross-References

[LowerStructArgs Rewrite Shape](lower-args-and-aggr-and-struct.md#rewrite-shape) produces the parameter-space pointers and `CVT_PARAM_TO_*` casts this pass consumes. [Address-space vote lattice](../topics/addrspace-vote-lattice.md) covers the inter-procedural worker that drives callee specialization. [NVPTX Backend Passes Overview — Shared parameter-space enable flag](overview.md#shared-parameter-space-enable-flag) documents the byval-AS flag both this pass and LowerStructArgs read. [Parameter-Space Sizer](nvvm-ir-verifier.md#parameter-space-sizer) is the downstream consumer that turns inferred address-space tags into the launch-argument check.
