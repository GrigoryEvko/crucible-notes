# Force-Inline and Specialize Callees

## Abstract

Two module-level NVVM passes in tileiras share one purpose: remove call boundaries that are expensive or impossible to preserve in the NVPTX `.param` ABI. One marks functions as mandatory inline when their signature cannot be lowered cheaply. The other specializes callees after proving more precise address spaces for generic-pointer arguments.

Together they turn difficult interprocedural cases into simpler local code before NVPTX lowering:

- Kernel and image-handle helpers are forced through the normal LLVM inliner.
- Large argument lists and large aggregate returns become inline candidates before param-space lowering.
- Generic-pointer callees can be cloned into address-space-specialized variants.
- Rewritten or cloned callees are marked so later passes can assume the call boundary is temporary.

The semantic preference is clear: tileiras deletes an unsafe call boundary rather than teaching the downstream ABI path to carry a shape it cannot represent reliably.

## Operational Model

The force-inline pass is a pure function-attribute pass — no cloning, no call-site rewriting. For each defined function it decides whether inlining is mandatory, then writes both the normal LLVM function attribute and NVIDIA's compact cached attribute field.

The callee-specialization pass is interprocedural. It builds a worklist of functions with generic pointer parameters, infers the concrete address spaces passed at call sites, and either rewrites the original callee or creates a private clone with a narrower signature, then retargets matching call sites to the specialized body.

The two passes are complementary:

```text
force-inline pass:
    signature is hard for NVPTX ABI -> mark original function always_inline

callee-specialization pass:
    generic address-space argument has a stable concrete space -> rewrite or clone
```

The specialization pass does not replace the inliner. It prepares better callees for it: clones are internal, address-space-resolved, and marked as inline-friendly.

The address-space lattice used by the specialization pass has its own page; this page summarises only the call-graph rewrite. For the full lattice contract (the `UNDET/POISON` partition, the meet operator, the `kBudgetCap` per-block bound, and the I08 type-converter handoff that publishes `"nvvm.as"`), see [AddrSpace Vote Lattice](addrspace-vote-lattice.md). The two pages are complementary by design: this page owns the inliner-vs-specializer choice, the size thresholds, and the call-site retargeting machinery; the lattice page owns the data-flow rules that decide whether specialization is even legal.

## Force-Inline Decision

The force-inline pass evaluates functions in priority order. Earlier reasons override later cost-model reasons.

| Reason | Condition | Effect |
|---|---|---|
| Kernel | Function is an NVVM/PTX kernel entry. | Force inline even if the source requested `noinline`. |
| Image handle | Any argument carries an image/sampler typedef such as `wroimage`, `rdoimage`, or `sampler`. | Force inline because image handles do not survive the param ABI cleanly. |
| Large parameters | Aligned parameter payload exceeds 384 bytes. | Force inline unless the user explicitly requested `noinline`. |
| Large return | Return payload exceeds 144 bytes. | Force inline unless the user explicitly requested `noinline`. |

The parameter-size rule uses the ABI-allocated size, not merely the IR type bit width. Each parameter contributes at least 4 bytes, and pointer-like values are rounded according to their ABI alignment.

```c
static size_t param_slot_size(Type *ty, DataLayout dl) {
    size_t bytes = dl.alloc_size(ty);
    size_t align = dl.pointer_abi_align_if_pointer_like(ty);

    if (align != 0)
        bytes = align_up(bytes, align);

    return max(bytes, 4);
}

static bool has_large_param_payload(Function *fn, DataLayout dl) {
    size_t total = 0;

    for (Argument *arg = fn->first_arg; arg != NULL; arg = arg->next) {
        total += param_slot_size(arg->type, dl);
        if (total > 384)
            return true;
    }

    return false;
}

static bool has_large_return_payload(Function *fn, DataLayout dl) {
    Type *ret = fn->return_type;

    if (ret->is_void)
        return false;

    return dl.alloc_size(ret) > 144;
}
```

The pass is intentionally idempotent. A function already carrying `alwaysinline` is skipped, so repeated pipeline construction does not accumulate redundant mutations.

```c
bool should_force_inline(Function *fn, DataLayout dl, ForceInlineReason *reason) {
    if (fn->is_declaration || fn->has_alwaysinline)
        return false;

    if (is_kernel(fn)) {
        *reason = FORCE_INLINE_KERNEL;
        return true;
    }

    if (has_image_or_sampler_argument(fn)) {
        *reason = FORCE_INLINE_IMAGE_HANDLE;
        return true;
    }

    if (fn->has_noinline)
        return false;

    if (has_large_param_payload(fn, dl)) {
        *reason = FORCE_INLINE_LARGE_PARAMS;
        return true;
    }

    if (has_large_return_payload(fn, dl)) {
        *reason = FORCE_INLINE_LARGE_RETURN;
        return true;
    }

    return false;
}
```

When the answer is yes, tileiras sets the normal LLVM `alwaysinline` attribute and updates its compact cached flags so downstream proprietary passes see the same decision without re-querying the attribute set. A compatible reimplementation should treat the LLVM attribute as the source of truth and mirror any cached representation only if it reproduces NVIDIA's in-memory ABI.

## Address-Space Specialization

Specialization targets functions that still take generic pointers after ordinary lowering. Generic pointers are legal in LLVM IR, but they hide address-space facts that matter to NVPTX: global, shared, constant, local, tensor memory, and distributed shared memory have different instruction-selection and aliasing consequences.

The pass maintains a lattice per pointer argument:

```text
UNDETERMINED
    -> global
    -> shared
    -> constant
    -> local
    -> tensor_memory
    -> distributed_shared
    -> POISON
```

`UNDETERMINED` means no useful evidence has been seen. A concrete address space means every inspected use agrees. `POISON` means conflicting evidence was found and the argument must remain generic.

```c
typedef enum AddressVote {
    AS_UNDETERMINED,
    AS_GLOBAL,
    AS_SHARED,
    AS_CONSTANT,
    AS_LOCAL,
    AS_TENSOR_MEMORY,
    AS_DISTRIBUTED_SHARED,
    AS_POISON,
} AddressVote;

static AddressVote meet_address_votes(AddressVote old_vote, AddressVote new_vote) {
    if (old_vote == AS_UNDETERMINED)
        return new_vote;
    if (new_vote == AS_UNDETERMINED)
        return old_vote;
    if (old_vote == new_vote)
        return old_vote;
    return AS_POISON;
}
```

Only functions with bodies, at least one generic pointer parameter, and no hard opt-out attributes are seeded into the worklist. Kernels are excluded; they are handled by the force-inline and kernel-argument paths.

```c
static bool specialization_candidate(Function *fn) {
    return !fn->is_declaration
        && !fn->is_kernel
        && !fn->has_optnone
        && !fn->has_noinline
        && !fn->has_naked
        && !fn->already_specialized
        && has_generic_pointer_parameter(fn);
}
```

## Specialization Algorithm

The pass is a fixed-point worklist. Each successful specialization can make callers newly profitable, so affected callers are re-enqueued.

```c
bool specialize_callees(Module *m, int clone_budget) {
    Worklist wl = {};
    bool changed = false;

    for (Function *fn = m->first_function; fn != NULL; fn = fn->next) {
        if (specialization_candidate(fn))
            worklist_push(&wl, fn);
    }

    while (!worklist_empty(&wl)) {
        Function *fn = worklist_pop(&wl);

        AddressVote votes[MAX_ARGS];
        init_votes(votes, fn->arg_count);

        for (Use *use = fn->first_use; use != NULL; use = use->next) {
            CallSite call = classify_callsite(use);
            if (!call.valid)
                continue;

            for (unsigned i = 0; i < fn->arg_count; ++i) {
                if (!is_generic_pointer(fn->arg[i].type))
                    continue;

                AddressVote vote = infer_argument_address_space(call.arg[i]);
                votes[i] = meet_address_votes(votes[i], vote);
            }
        }

        if (!has_resolved_specialization(votes, fn->arg_count))
            continue;

        Function *target = fn;
        if (!can_rewrite_in_place(fn, votes)) {
            if (!clone_allowed(&clone_budget))
                continue;

            target = clone_for_address_spaces(fn, votes);
            mark_internal_inline_candidate(target);
            changed = true;
        }

        changed |= rewrite_matching_calls(fn, target, votes);
        changed |= resolve_return_address_space(target);

        for (Function *caller = first_affected_caller(fn);
             caller != NULL;
             caller = next_affected_caller(fn, caller)) {
            if (specialization_candidate(caller))
                worklist_push(&wl, caller);
        }
    }

    return changed;
}
```

The clone budget has three modes:

| Budget | Meaning |
|---|---|
| `-1` | Unlimited cloning. |
| `0` | Disable cloning; only in-place rewrites can happen. |
| Positive `N` | Permit at most `N` clone attempts before suppressing further clones. |

The counter is attempt-based rather than success-based. This prevents recursive or ambiguous call graphs from retrying indefinitely.

## Call-Site Retargeting

Retargeting is not a textual rename. The pass builds a replacement call with operands converted to the specialized address spaces, inserts it before the original call, rewires every use of the original result, then erases the old call.

```c
bool rewrite_matching_calls(Function *old_fn,
                            Function *new_fn,
                            const AddressVote *votes) {
    bool changed = false;

    for (CallSite call = first_callsite(old_fn);
         call.valid;
         call = next_callsite(old_fn, call)) {
        if (!call_matches_votes(call, votes))
            continue;

        Value *new_args[MAX_ARGS];
        for (unsigned i = 0; i < call.arg_count; ++i)
            new_args[i] = convert_arg_for_vote(call.arg[i], votes[i]);

        CallInst *replacement = build_call_before(call.inst, new_fn, new_args);
        replace_all_uses_with(call.inst, replacement);
        erase_instruction(call.inst);
        changed = true;
    }

    return changed;
}
```

Return values are resolved through the same lattice. If every return instruction produces a pointer in the same concrete address space, the result type can be treated as address-space-resolved by later passes.

## Reimplementation Notes

The important compatibility requirements are:

- Preserve user `noinline` for cost-model cases, but allow tileiras-required overrides for kernels and image/sampler handles.
- Use ABI allocation size and alignment when computing parameter payload, not source-level type size.
- Treat `384` bytes for parameters and `144` bytes for returns as behavioral thresholds.
- Use a monotone meet lattice for address-space inference; any conflict becomes poison.
- Make specialized clones internal and inline-friendly so they do not escape as ABI-visible alternatives.
- Retarget calls by constructing new IR, not by mutating the callee pointer in place when operand address spaces change.
- Bound clone attempts so recursive call graphs cannot keep cloning forever.

## Diagnostics and Knobs

The implementation has debug output for the force-inline reason and for interprocedural memory-space specialization. The useful user-facing controls are the IPMSP dump switch and clone-budget switch. A reimplementation should provide equivalent observability: initial worklist size, clone suppression, affected caller count, and successful return-address-space resolution are the events needed to debug this pass family.
