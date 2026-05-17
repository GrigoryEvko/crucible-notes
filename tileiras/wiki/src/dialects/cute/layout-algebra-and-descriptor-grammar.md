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

## Worked Algebra Examples

The rules above are short enough to verify by hand, but every worked example below shows the full derivation so the result table doubles as a regression oracle. Notation: a flat 1-D layout writes as `<shape : stride>`; a 2-D layout writes as `(s0, s1) : (d0, d1)`. The coordinate-to-offset map at a leaf is `coord -> coord * stride`; at a tuple, the sum of the per-leaf products.

### Composition, dense one-mode case

Inputs:

- `A = <8 : 2>` — domain `[0, 8)`, offset map `i -> 2i`, image `{0, 2, 4, 6, 8, 10, 12, 14}`, cosize 15.
- `B = <4 : 1>` — domain `[0, 4)`, offset map `j -> j`, image `{0, 1, 2, 3}`, cosize 4.

`composition(A, B)` applies `A` first and `B` second — `C(i) = B(A(i))`. Legality test: `cosize(A) = 15` must be `<= size(B) = 4`. **It is not.** The fold is rejected; the verifier emits a domain-mismatch diagnostic. Reordered as `composition(B, A)`: `cosize(B) = 4 <= size(A) = 8`, legal. The composite is `C(j) = A(B(j)) = A(j) = 2j` over domain `[0, 4)`, yielding the one-mode layout `<4 : 2>`.

### Composition, hierarchical case

Inputs:

- `A = (4, 2) : (1, 4)` — 4x2 column-major over `[0, 8)`. At coord `(r, c)`, offset is `r + 4c`.
- `B = (2, 2) : (1, 2)` — 2x2 column-major selector, image `{0, 1, 2, 3}`, cosize 4.

Walk `B` and look up `A` at each image element. `B(0,0) = 0 -> A(0,0) = 0`. `B(1,0) = 1 -> A(1,0) = 1`. `B(0,1) = 2 -> A(2,0) = 2`. `B(1,1) = 3 -> A(3,0) = 3`. The resulting offsets `{0, 1, 2, 3}` are linear in the 2x2 `B` domain, so `composition(A, B) = (2, 2) : (1, 2)`. The composite forgets the second mode of `A` because `B`'s image never crosses the second-mode stride. This is the structural reason composition preserves mode hierarchy: the result mode tree is `B`'s, populated by `A`'s strides.

### Complement, gap-filling case

Input:

- `A = <4 : 32>` — image `{0, 32, 64, 96}` inside the target domain `[0, 256)`.

`complement(A, 256)` returns the layout whose image is `[0, 256) \ {0, 32, 64, 96} + offsets that tile cleanly around A`. The sorted-by-stride flatten of `A` produces one boundary point `(0, 32)`; between consecutive image points the gap is 32 elements at stride 1; after the last image point `96`, the gap to 256 is `160`, but the complement must factor evenly with `|A| = 4` so each gap is `256 / 4 - 1 = 63` plus the one-element image, i.e., 32 elements per gap. The complement is `<32 : 1>`: 32 contiguous elements per `A`-slot, four slots, total `4 * 32 = 128`. Combined image: `image(A) + image(C)` covers `[0, 128) ∪ {128..., not yet covered}`. The reader should note that `|A| * |C| = 4 * 32 = 128`, not 256; the complement only spans the elements `A` failed to cover up to the smallest factor of 256 that closes the tile. To complete the cover of `[0, 256)`, compose this complement with the second-level complement, which is how `complement(A, M)` is used inside `logical_divide` for multi-mode tiles.

### Logical divide, one-mode case

Inputs:

- `A = <128 : 1>` — contiguous run of 128 elements.
- `T = <32 : 1>` — tile of 32 contiguous elements.

`logical_divide(A, T)` produces `(A ∘ T, complement(A ∘ T, |A|))` per divided mode. `A ∘ T = <32 : 1>` because composing two unit-stride layouts gives a unit-stride layout of the inner domain. `complement(<32:1>, 128)` returns the residual layout `<4 : 32>` — four copies stepping by 32 elements. Combined, `logical_divide(<128:1>, <32:1>) = ((32:1), (4:32))`: mode 0 is the tile of size 32 at stride 1; mode 1 is the rest mode of size 4 at stride 32. Total size: `32 * 4 = 128`, preserved.

### Logical product, one-mode case

Inputs:

- `A = <128 : 1>` — contiguous run of 128 elements.
- `B = <4 : 32>` — four copies stepping by 32.

`logical_product(A, B)` produces a 2-mode layout `(A, A' )` where `A'` is `A` re-laid-out at `B`'s coordinate basis. The standard cuTe construction is `(A, complement(A, |A| * |B|) ∘ B)`. Here `|A| * |B| = 128 * 4 = 512`. `complement(<128:1>, 512)` is `<4 : 128>` — four slots each starting at multiples of 128. Composing with `B = <4:32>` gives `<4 : 32 * 128 / |A| > = <4 : 128>` once the strides are scaled by `A`'s extent. The result is `((128:1), (4:128))`: the inner tile is the 128-element `A`; the outer mode places 4 copies of that tile, 128 elements apart. Total size: 512, total image: `[0, 512)`.

The reader who walks the same derivation with `B = <4 : 1>` will arrive at a different result — `((128:1), (4:1))` is illegal because the two modes overlap at offset 0. `logical_product` is well-defined only when `B`'s image, after scaling by `|A|`, avoids `A`'s image; the complement-then-compose construction enforces that automatically.

### Coalesce, contiguous-mode case

Inputs:

- `L = (2, 4) : (4, 1)` — two modes; the outer mode steps by 4, the inner by 1.

Contiguity test: `stride(inner) * shape(inner) = 1 * 4 = 4 = stride(outer)`. The two modes are adjacent in memory and can fuse. `coalesce` returns `<8 : 1>` — one mode of shape 8 at stride 1, semantically identical but representationally flat.

A non-coalescible counter-example: `L = (2, 4) : (8, 1)`. `stride(inner) * shape(inner) = 4`, but `stride(outer) = 8`. There is a 4-element gap per outer step; the modes cannot fuse. `coalesce` returns `L` unchanged.

### Filter-zeros, broadcasting-mode case

Input:

- `L = (4, 3) : (1, 0)` — 4 contiguous elements broadcast 3 times.

`filter_zeros` walks the tree and replaces any leaf with stride 0 by the scalar shape-1 leaf, then drops the resulting scalar from the tuple. Result: `<4 : 1>`. The broadcast count of 3 is correct semantically but contributes no addressable memory; downstream coalesce and complement walks must not see it, or they would over-count the footprint.

## Swizzle Operator

A `swizzle<B, M, S>` permutes bits of a linear SMEM offset so that consecutive elements in a tile land in different SMEM banks, eliminating conflicts when a warp issues 32-way parallel loads. The operator is a pure XOR-based permutation; it preserves the offset domain, so any layout `L` composed with a swizzle has the same size and cosize as `L`.

### Bit-Manipulation Formula

```c
uint32_t swizzle_apply(uint32_t B, uint32_t M, uint32_t S, uint32_t offset) {
    uint32_t mask = ((1u << B) - 1u) << S;
    uint32_t bits_to_xor = (offset & mask) >> S;
    return offset ^ (bits_to_xor << M);
}
```

The parameter triple has the following meanings:

- `B` — number of bits to swizzle. The mask `(1 << B) - 1` is the field width.
- `M` — destination bit position. The XOR pattern lands at bits `[M, M + B)`.
- `S` — source bit position. The XOR pattern is read from bits `[S, S + B)` of the offset.

The forward transform writes `output[M..M+B] = input[M..M+B] XOR input[S..S+B]`. The transform is self-inverse: applying it twice returns the original offset, because XOR is its own inverse. That self-inverse property is why the same `swizzle<B, M, S>` mediates both stores and loads — there is no separate "unswizzle" operator.

### Worked Example: swizzle<3, 4, 3>(0x40)

Step through `swizzle<3, 4, 3>(offset = 0x40 = 0b1000000)`:

1. Compute the mask: `((1 << 3) - 1) << 3 = 0b111 << 3 = 0b111000 = 0x38`.
2. Extract source bits: `(0x40 & 0x38) >> 3 = (0b1000000 & 0b0111000) >> 3 = 0b0000000 >> 3 = 0b000 = 0x0`.
3. XOR target field: `0x0 << 4 = 0b0000000 = 0x0`.
4. Apply: `0x40 ^ 0x0 = 0x40`. Output: `0x40`.

This input lies outside the swizzle's active range — bit 6 (`0x40`) sits above the destination field `[4, 7)` but does not overlap with the source field `[3, 6)`. Now try `offset = 0x18 = 0b11000`:

1. Mask: `0x38` (unchanged).
2. Source extract: `(0x18 & 0x38) >> 3 = (0b11000 & 0b111000) >> 3 = 0b011000 >> 3 = 0b011 = 0x3`.
3. XOR field: `0x3 << 4 = 0b00110000 = 0x30`.
4. Apply: `0x18 ^ 0x30 = 0b00011000 ^ 0b00110000 = 0b00101000 = 0x28`.

Offset `0x18` swizzles to `0x28`. The next offset `0x20 = 0b100000` swizzles to:

1. Source extract: `(0x20 & 0x38) >> 3 = 0b100000 >> 3 = 0b100 = 0x4`.
2. XOR field: `0x4 << 4 = 0b01000000 = 0x40`.
3. Apply: `0x20 ^ 0x40 = 0b01100000 = 0x60`.

Offsets `0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38` (eight consecutive 8-byte rows) swizzle to `0x00, 0x18, 0x30, 0x28, 0x60, 0x78, 0x50, 0x48`. Modulo 128 (the 32-bank SMEM word stride times 4 bytes), the swizzled bank indices are `0, 6, 12, 10, 24, 30, 20, 18` — all distinct, eight rows wide, no two rows colliding on the same bank.

### Canonical SMEM Swizzle Modes

The Hopper SMEM swizzle modes use three triples. Each entry below names the byte-row width it accepts and the bit-manipulation parameters it carries.

| Swizzle | Bytes per row | B | M | S | Used by |
|---|---:|---:|---:|---:|---|
| `swizzle<0, 0, 0>` | any | 0 | 0 | 0 | no swizzle, plain row-major SMEM |
| `swizzle<1, 4, 3>` | 32 | 1 | 4 | 3 | 32-B WGMMA operand sub-tile |
| `swizzle<2, 4, 3>` | 64 | 2 | 4 | 3 | 64-B WGMMA operand sub-tile |
| `swizzle<3, 4, 3>` | 128 | 3 | 4 | 3 | canonical 128-B Hopper SMEM swizzle |

Reading the table: `M = 4` and `S = 3` are fixed across all non-zero modes; only `B` varies. `B = 3` produces an 8-row × 8-bank rotation; `B = 2` produces 4-row × 4-bank; `B = 1` produces 2-row × 2-bank. The WGMMA SMEM descriptor's `swizzle_mode` field encodes these as 0/3/2/1 respectively — see [MMA Atoms SM70-SM120 — SMEM-Descriptor Construction](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for the descriptor packing.

A swizzle is part of the layout's identity. Two layouts `L1` and `L2` with the same `(shape, stride)` but different swizzles produce different memory access patterns; verifier equivalence treats them as distinct.

Cross-references: [Verifiers — LayoutTypeInterface Kind Discriminator](verifiers.md#layouttypeinterface-kind-discriminator) for the seven LayoutTypeInterface sentinels read from
`*(type + 0x88)` and the per-primitive verifier table, and [cute_nvgpu TMA Atoms — Descriptor Builder](../cute_nvgpu/tma-atoms.md#descriptor-builder) for the descriptor
builders that consume these primitives. [Tile and Divide Operations — Divide Variants](tile-and-divide-ops.md#divide-variants) and [Product Variants](tile-and-divide-ops.md#product-variants) show how the worked examples above re-group when the divide/product result is named through the `cute.*` op surface. [MMA Atoms SM70-SM120 — SMEM-Descriptor Construction](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) consumes the swizzle parameters in the canonical Hopper descriptor word.

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

