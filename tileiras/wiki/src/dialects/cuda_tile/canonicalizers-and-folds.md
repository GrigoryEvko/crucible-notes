# cuda_tile Canonicalizers and Folds

## Abstract

`cuda_tile` keeps the public IR simple and leaves the heavy lifting to private
lowering. Its public fold surface is deliberately small: constants fold to
their value, a guarded floating add folds when both operands are safe
constants, `if` inverts a negated condition by swapping branches, and `select`
carries the usual identity, constant-condition, boolean, compare, and
nested-select folds. A separate expression simplifier handles deeper
recursive cleanup before conversion; the rules below capture the public
semantics.

Beneath the fold surface sits a larger pattern set. `cuda_tile.if` registers
eight `RewritePattern`s through a vtable bank at `unk_59A9F08..unk_59AA1A8`
(stride `0x60`). These are Shape A `RewritePattern` entries rather than Shape
B `OpConversionPattern` entries because they run during the dialect's own
canonicalize step, not during dialect-to-dialect conversion.
`cuda_tile.select` adds three more at `unk_59AA208..unk_59AA2C8`. Together
the eleven patterns drive structural canonicalization for control-flow tile
ops. The two non-trivial entries — `CombineIfs` and `CombineNestedIfs` — are
documented below.

## Fold Surface

| Operation | Fold | Safety condition |
|---|---|---|
| `constant` | Returns the literal attribute. | Always safe. |
| `addf` | Constant plus constant becomes a constant sum. | Both operands are finite constants and the fold can preserve the expected floating semantics. |
| `if` | `if (xori(cond, true)) then A else B` becomes `if (cond) then B else A`. | The else region is present and the RHS of `xori` is the boolean constant true. |
| `select` | Applies identity, constant-condition, boolean, compare, invert, and nested-select rules. | Rules must preserve result type and avoid materializing side effects. |

The small surface is deliberate. Public `cuda_tile` folding strips obvious
redundancy without committing to target-specific numeric or memory choices
too early.

## Constant and AddF

`cuda_tile.constant` is the canonical literal operation. Folding it returns the
attribute value directly.

```c
OpFoldResult fold_constant(ConstantOp op) {
    return op.value;
}
```

`addf` folds only when both operands are constants that need no special NaN
or infinity handling. Algebraic identities like `x + 0` wait for later
canonicalization because floating flags and target lowering can change their
legality.

```c
Optional<Attribute> fold_addf(AddFOp op) {
    Optional<FloatAttr> lhs = finite_float_constant(op.lhs);
    Optional<FloatAttr> rhs = finite_float_constant(op.rhs);

    if (!lhs.has_value || !rhs.has_value) {
        return none();
    }

    FloatAttr sum = add_with_declared_semantics(lhs.value, rhs.value, op.rounding);
    return cast_to_result_element_type(sum, op.result.type);
}
```

## If Inversion

The `if` fold spots a boolean negation expressed as `xori(cond, true)`,
rewrites the condition to `cond`, and swaps the then and else regions. The
rewrite is in-place and produces no replacement values.

```c
RewriteResult fold_if_negated_condition(IfOp op) {
    XorIOp xor = dyn_cast_xori(op.condition.defining_op);
    if (!xor.valid) {
        return no_change();
    }

    if (!is_constant_true(xor.rhs)) {
        return no_change();
    }

    if (op.else_region.empty()) {
        return no_change();
    }

    op.condition = xor.lhs;
    swap_regions(op.then_region, op.else_region);
    return changed();
}
```

The fold is correct because `if (!c) A else B` is equivalent to
`if (c) B else A` whenever both branches exist and region result types already
match the verifier contract.

## IfOp Canonicalization Pattern Set

Eight patterns registered for `cuda_tile.if` cover the structural rewrites the
dialect performs on its own ops before any conversion driver runs. Vtable
slots appear here at their approximate offsets; exact addresses are
recoverable from the dialect's pattern-registration constructor in `cicc`.

| # | Vtable | Name | Action |
|---|---|---|---|
| 1 | `unk_59A9F08` | RemoveUnusedResults | Drops `if` results that have no uses. |
| 2 | `unk_59A9F68` | ReplaceYieldWithValue | When both then- and else-branches yield the same SSA value, replaces the `if` with that value. |
| 3 | `unk_59A9FC8` | RemoveStaticCondition | When the condition is a compile-time constant, replaces the `if` with the contents of the chosen branch. |
| 4 | `unk_59AA028` | ConvertToSelect | When both branches are single-op (yield-only), rewrites into `cuda_tile.select`. |
| 5 | `unk_59AA088` | RemoveEmptyElseBranch | Drops empty else-branches that yield no values. |
| 6 | `unk_59AA0E8` | CombineIfs | Two adjacent `if`s with the same condition merged into one. |
| 7 | `unk_59AA148` | CombineNestedIfs | Nested `if (a) { if (b) { ... } }` rewritten as `if (a && b) { ... }`. |
| 8 | `unk_59AA1A8` | MoveTerminatorToParent | When a branch ends with a `cuda_tile.return`, hoists it past the `if`. |

The two structural combiners (`CombineIfs` and `CombineNestedIfs`) are the
only entries that rewrite across more than one operation. Their algorithms
follow next.

## CombineIfs

`CombineIfs` runs from `sub_6950B0`. The pattern triggers on two adjacent
`if` ops with the same condition and merges them into one combined `if`. The
match uses pointer identity on the SSA value, not equal-by-compare — the
check stays cheap and side-steps canonicalization order dependencies inside
the surrounding block.

```c
RewriteResult combine_adjacent_ifs(IfOp first, IfOp second) {
    if (first.condition.ssa_value != second.condition.ssa_value) {
        return no_change();
    }

    if (!is_adjacent_in_block(first, second)) {
        return no_change();
    }

    IfOp combined = create_if(first.condition,
                              merge_then_regions(first.then_region, second.then_region),
                              merge_else_regions(first.else_region, second.else_region));

    replace_uses_with_combined_results(first, second, combined);
    erase(first);
    erase(second);
    return changed();
}
```

Both then-regions are concatenated in source order, as are both else-regions.
Each original `if`'s result list maps to a contiguous slice of the combined
yield-list, and uses of the originals are redirected before the originals are
erased.

## CombineNestedIfs

`CombineNestedIfs` runs from `sub_6963F0`. The pattern fires on an outer
`if` whose then-branch contains exactly one op (an inner `if`) plus a yield,
and whose else-branch yields a poison or undef value. Under those
preconditions the two condition tests fold into a single `arith.andi`
without changing semantics: the original outer-else result was already
undefined, so the combined op's empty else-branch is observationally
identical.

```c
RewriteResult combine_nested_ifs(IfOp outer) {
    IfOp inner = match_single_inner_if(outer.then_region);
    if (!inner.valid) {
        return no_change();
    }

    if (!else_yields_poison(outer.else_region)) {
        return no_change();
    }

    Value combined_condition = emit_andi(outer.condition, inner.condition);
    IfOp rewritten = create_if(combined_condition,
                               steal_then_region(inner),
                               empty_region());

    replace_uses(outer, rewritten);
    erase(outer);
    return changed();
}
```

The poison-yielding outer else-branch already licenses the combined op to
leave its own else-branch empty, so no semantically visible value is lost.

## Select Rules

`select` tries value-preserving folds in a fixed order. Same-arm folding
runs before constant-condition folding; boolean identity folding runs before
the more expensive rewrites. Swap that order and folds shadow each other.

```c
Optional<Value> fold_select(SelectOp op) {
    if (op.true_value == op.false_value) {
        return op.true_value;
    }

    Optional<bool> cond = constant_bool(op.condition);
    if (cond.has_value) {
        return cond.value ? op.true_value : op.false_value;
    }

    if (is_i1_tile(op.result.type)) {
        if (is_true(op.true_value) && is_false(op.false_value)) {
            return op.condition;
        }
    }

    Optional<Value> cmp_fold = fold_select_with_compare(op);
    if (cmp_fold.has_value) {
        return cmp_fold.value;
    }

    Optional<Value> xor_fold = fold_select_with_inverted_condition(op);
    if (xor_fold.has_value) {
        return xor_fold.value;
    }

    return fold_select_with_nested_select(op);
}
```

The inverted-condition case may mutate the op by replacing the condition with
the underlying value and swapping arms. The nested-select case collapses
patterns like `select(c, select(c, a, b), d)` whenever doing so erases a
duplicate condition test.

## SelectOp Canonicalization Pattern Set

Alongside the fold logic above, `cuda_tile.select` registers three standalone
`RewritePattern`s through a small vtable bank at `unk_59AA208..unk_59AA2C8`.

| # | Vtable | Name | Action |
|---|---|---|---|
| 1 | `unk_59AA208` | ReplaceConstantSelect | `select(true, a, b)` becomes `a`; `select(false, a, b)` becomes `b`. |
| 2 | `unk_59AA268` | ReplaceIdenticalSelect | `select(c, a, a)` becomes `a`. |
| 3 | `unk_59AA2C8` | InverseConditionSelect | `select(not c, a, b)` becomes `select(c, b, a)`. |

The constant and identical patterns overlap with the corresponding fold rules
but stay registered because the canonicalize driver applies them on operations
the fold path skips — for example, after a peer rewrite materializes a
constant where there was previously a variable condition.

## Recursive Expression Simplifier

A private expression simplifier handles repeated scalar, mask, and
integer-like cleanup. It lowers expression fragments into a compact tree,
memoizes simplified nodes, and rebuilds canonical operations. The useful
algorithm is ordinary fixed-point simplification:

```c
Value simplify_expr(Node node, Mode mode, int depth) {
    if (depth > max_simplifier_depth()) {
        return rebuild_without_simplifying(node);
    }

    CacheKey key = { .node = node, .mode = mode };
    if (cache_contains(key)) {
        return cache_get(key);
    }

    SmallVector<Value> operands;
    for (Node child : node.operands) {
        operands.push(simplify_expr(child, mode, depth + 1));
    }

    Value simplified = simplify_by_kind(node.kind, operands, node.flags);
    cache_put(key, simplified);
    return simplified;
}
```

Typical rules include boolean negation cleanup, variadic `and`/`or` folding,
integer comparison folding, arithmetic simplification, bit-vector constants,
and select simplification. Keep the simplifier deterministic and bounded —
every recursive path needs a depth limit and a memoization cache.

## Canonicalization Driver

Keep the public canonicalization set small and predictable.

```c
void populate_cuda_tile_canonicalizers(PatternSet *patterns) {
    add(patterns, fold_if_negated_condition);
    add(patterns, combine_adjacent_ifs);
    add(patterns, fold_select_same_operands);
    add(patterns, fold_select_constant_condition);
    add(patterns, fold_select_bool_identity);
    add(patterns, fold_select_with_compare);
    add(patterns, fold_select_with_inverted_condition);
    add(patterns, fold_select_with_nested_select);
}

void canonicalize_cuda_tile(Module module) {
    PatternSet patterns;
    populate_cuda_tile_canonicalizers(&patterns);
    run_greedy_rewrite(module, patterns);
    run_expression_simplifier(module);
}
```

`combine_adjacent_ifs` is safe only when region order, yielded values, and
side-effecting operations survive intact. The pattern must not merge across
token-ordered memory effects unless the token graph stays equivalent.

## Dense Constant Printing

Debug replay paths may print dense constants as comma-separated element lists.
That output is fine for diagnostics, but treat it as throwaway — it omits
shape and dialect typing context. Round-tripable IR must come from the normal
operation printer.

## Invariants

- Public folding never drops a token-ordered memory effect.
- Floating folds avoid NaN and infinity cases unless the exact semantics are
  modeled.
- Region rewrites preserve verifier-approved branch and yield types.
- `select` folds preserve result type and condition dominance.
- Recursive simplification is memoized and depth-bounded.
- Debug dense-element printing is not a serialization format.

## Reimplementation Checklist

1. Implement `constant`, guarded `addf`, `if`, and `select` folds first.
2. Keep floating identity folds conservative until numeric flags are modeled.
3. Run region verifiers before branch-swapping canonicalizations.
4. Memoize recursive simplifier results by node and simplification mode.
5. Bound simplifier recursion and provide a non-folding rebuild fallback.
6. Keep debug printers separate from round-trip textual assembly.
