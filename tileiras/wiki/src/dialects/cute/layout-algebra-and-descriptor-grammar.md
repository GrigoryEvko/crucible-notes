# cuTe Layout Algebra and Descriptor Grammar

## Abstract

A `cute` layout is a hierarchical pair: a shape tree paired with a stride tree, together mapping logical coordinates to physical offsets. The algebra over those pairs describes tensor views, tile partitions, swizzles, MMA operands, copy atoms, and the layout conversions that later become NVGPU and TileAS code. The rest of this page covers the mathematical model, textual descriptor grammar, parser behavior, composition algorithm, and round-trip invariants.

## Layout Model

A cuTe layout is a function from a coordinate domain to an offset domain. It is
stored as two congruent trees:

```text
Layout  = (Shape, Stride)
Shape   = integer leaf | tuple of Shape
Stride  = integer leaf | tuple of Stride
size    = product of all Shape leaves
offset  = sum(coord_leaf[i] * stride_leaf[i])
cosize  = maximum reachable offset plus one
```

For a flat two-dimensional row-major tile:

```text
Shape  = (2, 2)
Stride = (2, 1)

coord(row, col) -> row * 2 + col
```

For a column-major tile:

```text
Shape  = (2, 2)
Stride = (1, 2)

coord(row, col) -> row + col * 2
```

Hierarchy matters. A mode can itself contain a sub-layout, so a shape like `((2, 2), 4)` is not a flattened rank-three vector. The inner `2 x 2` structure survives composition, divide, product, filtering, and swizzling. That is why most `cute` verifier and folder code is a structural tree walk rather than a flat affine-matrix calculation.

## Descriptor Grammar

Textual layout descriptors use basis-vector entries of the form `N@dim`. A
descriptor is a parenthesized list; entries may nest, and one basis may name
more than one output dimension.

```ebnf
layout       ::= group ;
group        ::= "(" ws [ entry { ws "," ws entry } ] ws ")" ;
entry        ::= group | basis ;
basis        ::= count ws "@" ws dim { ws "@" ws dim } ;
count        ::= int | int "/" int ;
dim          ::= uint ;
int          ::= [ "-" ] digit { digit } ;
uint         ::= digit { digit } ;
digit        ::= "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" ;
ws           ::= { " " | "\t" } ;
```

Examples:

```text
(1@0, 1@1)
(16@0, 1@1)
((1@0, 8@1), 1@2)
(1/2@0, 4@1)
(1@0@1)
```

The grammar ignores whitespace. Empty groups are legal and stand for the degenerate empty layout. Fractional counts parse cleanly at the syntactic level, but normalization must drop or reject impossible bases so no divide-by-zero or non-integral layout reaches lowering.

## Parser Algorithm

Parsing is recursive descent over groups, basis counts, and dimension lists. Malformed descriptors must surface as invalid attributes with a precise diagnostic — never as a silently manufactured default layout.

```c
ParseResult parse_layout(StringRef text) {
    Parser parser = { .input = text, .pos = 0 };
    LayoutNode root = parse_group(&parser);
    skip_ws(&parser);

    if (!parser.at_end()) {
        return parse_error("unexpected trailing layout text");
    }

    if (!root.valid) {
        return parse_error("failed to parse layout descriptor");
    }

    return ParseResult(root);
}

LayoutNode parse_group(Parser *parser) {
    require_char(parser, '(');

    SmallVector<LayoutEntry> entries;
    skip_ws(parser);

    if (peek(parser) == ')') {
        consume(parser);
        return LayoutNode(entries);
    }

    while (true) {
        entries.push(parse_entry(parser));
        skip_ws(parser);

        if (peek(parser) == ')') {
            consume(parser);
            return LayoutNode(entries);
        }

        require_char(parser, ',');
    }
}
```

The basis parser reads an integer or fraction, then one or more `@dim` pieces:

```c
LayoutEntry parse_basis(Parser *parser) {
    Rational count = parse_count(parser);
    SmallVector<uint32_t> dims;

    do {
        require_char(parser, '@');
        dims.push(parse_uint(parser));
        skip_ws(parser);
    } while (peek(parser) == '@');

    require(count.denominator != 0);
    return LayoutEntry(count, dims);
}
```

## Composition

Composition is the core layout fold. Given layouts `A` and `B`,
`composition(A, B)` describes applying `A` first and `B` second. In functional
notation:

```text
C(i) = B(A(i))
```

The fold is legal when the image of `A` fits inside the domain of `B`. With
static layouts, the result can be computed and interned immediately.

```c
Optional<Layout> compose_layout(Layout a, Layout b) {
    if (cosize(a) > size(b)) {
        return none();
    }

    Shape shape = a.shape;
    Stride stride = compose_stride_tree(a, b);
    Layout result = normalize_layout(Layout(shape, stride));

    if (!is_valid_layout(result)) {
        return none();
    }

    return intern_layout(result);
}
```

Composition preserves hierarchy. Flatten too early and you lose information that divide/product regrouping and swizzle-aware atom selection still need.

## Layout Primitives

The cute dialect represents tile layouts as nested-tuple `(Shape, Stride)` pairs. Six primitive operations compute layout
transformations. Each branches at entry on the 7-sentinel kind tag at `*(type + 0x88)` of the operand Layout-class Type
to handle the per-kind variation, then delegates to a per-kind handler resolved through the 16-entry dispatch table at
`0x59B1DE0`. The shape and stride trees stored inside a Layout share a single `Tuple` representation:

```c
typedef struct Tuple {
    /*+0x00*/ uint8_t   kind;           // 0 = leaf, 1 = tuple, 2 = dynamic
    /*+0x08*/ union {
                  int64_t i;            // leaf value
                  Tuple  *t;            // tuple children
              };
    /*+0x10*/ uint32_t  n;              // children count (for tuple kind)
} Tuple;

typedef struct Layout {
    /*+0x00*/ Tuple shape;
    /*+0x18*/ Tuple stride;
} Layout;
```

`crd2idx(coord, Shape, Stride) -> idx` converts a multi-dimensional coordinate into a linear memory offset. The walk
mirrors the congruence between the coordinate, shape, and stride trees: at a leaf, the coordinate is multiplied by the
matching stride leaf; at a tuple, the sum is taken over the children.

```c
int64_t crd2idx(Tuple coord, Tuple shape, Tuple stride) {
    if (isLeaf(coord))                       return coord.i * stride.i;
    int64_t sum = 0;
    for (size_t i = 0; i < coord.n; ++i)     sum += crd2idx(coord.t[i], shape.t[i], stride.t[i]);
    return sum;
}
```

`shape_div(Shape, divisor) -> Shape` performs element-wise integer division across the shape tree with a rounding mode
(ceil, floor, or exact). Verifier `sub_18B4200` (1 114 B) reports failure when any shape leaf does not divide cleanly
under the chosen rounding mode. `ceil_div(a, b) -> ceil(a / b)`, verified by `sub_18AC960` (1 432 B), is the helper used
by `shape_div` in ceil mode and by stage-count math elsewhere in the compiler.

```c
int64_t ceil_div(int64_t a, int64_t b) {
    return (a + b - 1) / b;
}
```

`filter_zeros(Layout)` at `sub_18B3510` (3 298 B, the largest primitive) eliminates every `Stride == 0` axis from a
layout. Zero-stride axes are broadcasting axes that do not address memory, and removing them is a prerequisite for
coalescing and for emitting correct TMA descriptors. The walk is recursive: a leaf with zero stride collapses to the
scalar layout of its shape; a tuple keeps only those children whose recursive result is not the unit scalar layout.

```c
Layout filter_zeros(Layout L) {
    if (isLeaf(L))                                          return (L.stride == 0) ? scalarLayout(L.shape) : L;
    Tuple newShape, newStride;
    for (size_t i = 0; i < L.n; ++i) {
        Layout sub = filter_zeros(L.children[i]);
        if (sub != scalarLayout(1))                         { newShape.push(sub.shape); newStride.push(sub.stride); }
    }
    return Layout{newShape, newStride};
}
```

`group_modes(Layout, indices) -> Layout` at `sub_18C5F40` (2 329 B) collapses the specified mode indices into a single
nested tuple, converting for example `(M, N, K)` into `((M, N), K)`. The operation is purely a regrouping: the leaves
and their order are preserved, only the tree shape changes.

`coalesce(Layout) -> Layout` merges adjacent axes when the inner axis's stride times its shape equals the outer axis's
stride, which is precisely the condition for the two axes to be contiguous in memory. After `filter_zeros` has removed
broadcast axes, coalesce reduces the remaining hierarchy as far as the contiguity test allows without changing the
function the layout computes.

```c
Layout coalesce(Layout L) {
    Layout out = emptyLayout();
    for (size_t i = 0; i < L.n; ++i) {
        Layout inner = L.children[i];
        if (!out.empty() && out.back().stride * out.back().shape == inner.stride) {
            out.back().shape = out.back().shape * inner.shape;
        } else {
            out.push(inner);
        }
    }
    return out;
}
```

`complement(Layout, total_size) -> Layout` returns a layout that addresses the elements of `[0, total_size)` not
already covered by the input. It is the stride remainder used by partition operations: given a tile layout that names
part of a tensor, the complement names the surrounding storage so the two together tile the whole array exactly once.

## Algebra Rules on Shape and Stride Tuples

The transformations above are not opaque routines. Each has an algebraic definition over the shape/stride tuple representation small enough to type-check by hand. Treat these rules as the canonical specification and the recursive walkers as one possible implementation.

Notation: `S = (s_0, ..., s_{n-1})` is a shape tuple, `D = (d_0, ..., d_{n-1})` is a stride tuple; both are
hierarchical (leaves may be sub-tuples). `|S|` is `product(s_i)` taken over the flattened leaves.

```c
// composition(A, B) : domain(A) -> codomain(B), defined when |codomain(A)| <= |domain(B)|.
//   Layout(S_A, D_A) ∘ Layout(S_B, D_B) = Layout(S_A, D_C)
//     where D_C is obtained by walking B with the offset stream A produces.
//
// complement(A, M) : produces the unique layout C such that
//   |A| * |C| == M  AND  image(A) ∩ image(C) == {0}  AND  image(A) ⊕ image(C) covers [0, M).
//   Algorithm: take the sorted-by-stride flatten of A as boundary points (b_i, s_i),
//              then emit the missing-interval layout between consecutive boundaries.
//
// logical_divide(A, T) = (A ∘ T, complement(A ∘ T, |A|))   per divided mode.
// logical_product(A, B) = composition(A, identity(|A|)) regrouped against B's shape tree.
// coalesce(A): merge adjacent leaves (s_i, d_i), (s_{i+1}, d_{i+1}) when d_{i+1} == s_i * d_i.
// filter_zeros(A): replace every leaf with d_i == 0 by the scalar leaf shape(1).
```

Four rules — composition, complement, divide, product — generate the rest of the layout algebra. `tiled_divide`, `flat_divide`, `zipped_divide`, `raked_product`, and `blocked_product` are the same operation seen through different regrouping permutations of the resulting mode tree; their algebraic content matches `logical_divide` / `logical_product` exactly.

A useful sanity invariant: `coalesce ∘ filter_zeros` is idempotent and preserves layout meaning. Two layouts that differ only after this canonicalisation compare equal in any verifier-level equivalence check.

Cross-references: [verifiers and kind-tag dispatch](verifiers.md) for the seven LayoutTypeInterface sentinels read from
`*(type + 0x88)` and the per-primitive verifier table, and [TMA atoms](../cute_nvgpu/tma-atoms.md) for the descriptor
builders that consume these primitives.

## Candidate Records

The implementation carries two conceptual layout-candidate records:

| Record | Role | Fields |
|---|---|---|
| Simple candidate | Parser-local basis entry. | Count, denominator, dimension list, layout-kind tag. |
| Rich candidate | Layout-assignment candidate. | Basis list, layout kind, optional swizzle, normalized stride, uniqued layout identity, reference state. |

The simple candidate must never escape parsing. The rich candidate is what layout assignment, convert-layout materialization, and atom planning consume. Splitting the two keeps every parsed token from dragging along state only the layout search ever reads.

## Round Trip

A valid descriptor should round-trip through parse, normalize, print, and parse
again without changing the represented layout. Whitespace and redundant grouping
may change; basis order and meaning must not.

```c
bool round_trips(StringRef descriptor) {
    Layout first = parse_layout(descriptor).value;
    StringRef printed = print_layout(first);
    Layout second = parse_layout(printed).value;
    return layouts_equivalent(first, second);
}
```

For diagnostic output, printers should prefer the basis notation users can read:
`(1@0, 1@1)` is better than dumping the internal tree object.

## Invariants

- Shape and stride trees are congruent.
- `size` is the product of shape leaves.
- `cosize` is the maximum reachable offset plus one.
- Composition is defined only when the inner image fits the outer domain.
- Normalization may simplify hierarchy but must not change the layout function.
- Parser output is either a valid candidate or a precise parse failure.
- Rich layout candidates carry swizzle and assignment metadata; simple parser
  candidates do not.

## Reimplementation Checklist

1. Implement a recursive shape/stride tree, not a flat-only layout.
2. Parse descriptors with explicit invalid states and diagnostics.
3. Normalize layouts before interning or comparing them.
4. Preserve hierarchy through composition, divide, and product.
5. Keep parser-local candidates separate from layout-assignment candidates.
6. Round-trip descriptors through parse and print tests.
