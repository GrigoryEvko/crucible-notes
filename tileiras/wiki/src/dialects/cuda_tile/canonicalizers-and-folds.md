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
eight rewrite patterns, and `cuda_tile.select` adds three more. The eleven
patterns drive structural canonicalization for control-flow tile ops; they run
during the dialect's own canonicalize step, not as part of dialect-to-dialect
conversion. The two non-trivial entries — `CombineIfs` and `CombineNestedIfs`
— rewrite across more than one operation and are documented below as
input/output IR pairs.

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
dialect performs on its own ops before any conversion driver runs:

| Pattern | Action |
|---|---|
| RemoveUnusedResults | Drops `if` results that have no uses. |
| ReplaceYieldWithValue | When both then- and else-branches yield the same SSA value, replaces the `if` with that value. |
| RemoveStaticCondition | When the condition is a compile-time constant, replaces the `if` with the chosen branch's contents. |
| ConvertToSelect | When both branches are single-op yield-only, rewrites into `cuda_tile.select`. |
| RemoveEmptyElseBranch | Drops empty else-branches that yield no values. |
| CombineIfs | Two adjacent `if`s with the same condition merged into one. |
| CombineNestedIfs | Nested `if (a) { if (b) { ... } }` rewritten as `if (a && b) { ... }`. |
| MoveTerminatorToParent | When a branch ends with a `cuda_tile.return`, hoists it past the `if`. |

The two structural combiners (`CombineIfs` and `CombineNestedIfs`) rewrite
across more than one operation. Each is documented below as an input/output
pair plus a precise match predicate.

## CombineIfs

The pattern fires on two adjacent `cuda_tile.if` ops in the same block whose
conditions are pointer-identical SSA values. Identity (not value equality) is
the match criterion: identity comparison is constant-time, and any earlier
canonicalization that normalized one of the conditions has already replaced
the SSA value at every use site.

Input IR:

```mlir
%a, %b = cuda_tile.if %cond -> (tile<128xf32>, tile<128xi1>) {
    %x = cuda_tile.mulf %lhs, %rhs : tile<128xf32>
    %m = cuda_tile.cmpf olt, %x, %thr : tile<128xf32>
    cuda_tile.yield %x, %m : tile<128xf32>, tile<128xi1>
} else {
    cuda_tile.yield %zero, %fmask : tile<128xf32>, tile<128xi1>
}
%c = cuda_tile.if %cond -> (tile<128xf32>) {
    %y = cuda_tile.addf %a, %a : tile<128xf32>
    cuda_tile.yield %y : tile<128xf32>
} else {
    cuda_tile.yield %a : tile<128xf32>
}
```

Output IR:

```mlir
%a, %b, %c = cuda_tile.if %cond -> (tile<128xf32>, tile<128xi1>, tile<128xf32>) {
    %x = cuda_tile.mulf %lhs, %rhs : tile<128xf32>
    %m = cuda_tile.cmpf olt, %x, %thr : tile<128xf32>
    %y = cuda_tile.addf %x, %x : tile<128xf32>
    cuda_tile.yield %x, %m, %y : tile<128xf32>, tile<128xi1>, tile<128xf32>
} else {
    cuda_tile.yield %zero, %fmask, %zero : tile<128xf32>, tile<128xi1>, tile<128xf32>
}
```

Match predicate (all must hold):

1. `first` and `second` are both `cuda_tile.if`.
2. `first.condition` and `second.condition` are the same SSA value (pointer-identical).
3. `second` immediately follows `first` in the same block; no other op separates them.
4. Each use of a result of `first` that lives inside `second`'s regions has already been rewritten — otherwise the merge would create a dominance violation.

The two then-regions are concatenated in source order; the two else-regions are concatenated in source order; each yield-list is the concatenation of the original yield-lists. Uses of the original results redirect to the corresponding slice of the combined result list before either original is erased.

## CombineNestedIfs

The pattern fires on an outer `cuda_tile.if` whose then-region is exactly one
inner `cuda_tile.if` followed by a yield that forwards the inner op's results,
and whose else-region yields poison or undef for every outer result. Under
those preconditions, the two condition tests fold into one `cuda_tile.andi`
without changing observable semantics: the outer else-branch was already
producing an undefined value.

Input IR:

```mlir
%r = cuda_tile.if %a -> (tile<64xi32>) {
    %inner = cuda_tile.if %b -> (tile<64xi32>) {
        %v = cuda_tile.muli %x, %y : tile<64xi32>
        cuda_tile.yield %v : tile<64xi32>
    } else {
        cuda_tile.yield %x : tile<64xi32>
    }
    cuda_tile.yield %inner : tile<64xi32>
} else {
    cuda_tile.yield %poison : tile<64xi32>
}
```

Output IR:

```mlir
%conj = cuda_tile.andi %a, %b : i1
%r = cuda_tile.if %conj -> (tile<64xi32>) {
    %v = cuda_tile.muli %x, %y : tile<64xi32>
    cuda_tile.yield %v : tile<64xi32>
} else {
    cuda_tile.yield %poison : tile<64xi32>
}
```

Match predicate:

1. `outer.then_region` has exactly two ops: an inner `cuda_tile.if` and a yield that forwards the inner op's results unchanged.
2. The inner op's result-type list matches `outer`'s result-type list.
3. `outer.else_region`'s yield supplies poison/undef for every result, so the outer-else value is already unobservable.
4. Both `outer.condition` and `inner.condition` are `i1` scalars.

The rewrite emits `cuda_tile.andi %outer.cond, %inner.cond : i1` to build the
combined predicate, hoists the inner then-region's body into a new outer-shaped
`if`, and forwards the original else-region (still yielding poison). Because the
outer else-branch was already undefined, the rewrite preserves every legitimate
observation. The combined predicate may itself fold later if either input is a
constant.

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
rewrite patterns:

| Pattern | Action |
|---|---|
| ReplaceConstantSelect | `select(true, a, b)` becomes `a`; `select(false, a, b)` becomes `b`. |
| ReplaceIdenticalSelect | `select(c, a, a)` becomes `a`. |
| InverseConditionSelect | `select(not c, a, b)` becomes `select(c, b, a)`. |

The constant and identical patterns overlap with the corresponding fold rules
but stay registered because the canonicalize driver applies them to operations
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

The public canonicalization set is small and predictable. The driver applies
fold rules and rewrite patterns in a greedy fixed-point loop, then runs the
recursive expression simplifier once over the residual IR.

The rewrite pipeline carries three contracts:

- Pure tile rewrites never reorder, duplicate, or erase a token-ordered memory
  operation. `combine_adjacent_ifs` may merge two `if`s only when no
  side-effecting op sits between them in the parent block, because the merge
  reshuffles the operation's position relative to the surrounding token chain.
- Floating folds preserve declared rounding mode and flush-to-zero policy.
  `fold_addf` is the only constant-folding rule that touches floats and runs
  only when both operands are finite constants, so the rule does not silently
  rewrite an `inf - inf` form whose semantics the producer chose.
- Region rewrites preserve verifier-approved branch and yield types. The
  `CombineIfs` and `CombineNestedIfs` rewrites grow the combined op's result
  list rather than reordering it, so dominance and result-list slices stay
  consistent across the pattern boundary.

The greedy driver iterates until no pattern fires, then hands control to the
recursive expression simplifier, which performs deeper boolean, integer, and
mask cleanup with memoization and a depth cap. The expression simplifier never
rewrites region-bearing ops; that responsibility belongs to the rewrite
pattern set above.

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

## Cross-References

[Verifiers](verifiers.md#verification-pipeline) describes the legality contracts these rewrites must preserve. [Operation Roster](op-roster.md#operation-families) catalogues the operations the patterns target. The TileAS-side counterpart in [nv_tileas Folds and Memory Consistency](../nv_tileas/folds-and-mem-consistency.md#canonicalization-patterns) describes the rewrite shapes that operate on the next dialect down.

