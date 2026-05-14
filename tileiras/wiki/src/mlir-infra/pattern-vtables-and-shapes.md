# Pattern Vtables and Shapes

## Abstract

Tileiras instantiates several thousand MLIR rewrite-pattern objects when a pass manager is constructed.
The compiler reuses a small number of physical layouts for every one of these objects: four pattern
shapes, distinguished by total size and by a single trailing field at `+0x60`, and two virtual tables,
distinguished by slot count. Recognising the shape and vtable of a pattern object is enough to
identify whether it is a plain `RewritePattern` or an `OpConversionPattern`, whether it carries a type
converter, and whether it captures a predicate closure. This page documents the layout so that a
catalogue scan can classify a pattern without entering its constructor.

## Fixed Prefix

Every pattern object shares the same `0x60`-byte prefix, regardless of shape: the vtable pointer, the
rooted operation name, the benefit and kind tags, the owning context, the typeinfo string built from
`__PRETTY_FUNCTION__`, and the inline `SmallVector` of generated operation names.

```c
typedef struct PatternShape {
    /*+0x00*/ void          **vtable;            // 8-slot OpConversionPattern or 5-slot RewritePattern
    /*+0x08*/ StringRef       op_name;           // rooted MLIR operation name
    /*+0x18*/ uint16_t        benefit;           // PatternBenefit tie-breaker
    /*+0x1A*/ uint16_t        kind;              // RootKind / MatchAnyOpTypeTag discriminant
    /*+0x20*/ MLIRContext    *ctx;               // owning context pointer
    /*+0x28*/ const char     *typeinfo_str;      // captured from __PRETTY_FUNCTION__
    /*+0x30*/ size_t          typeinfo_len;      // length of typeinfo_str, sans NUL
    /*+0x38*/ SmallVector<OperationName, 4> generatedOps;  // inline marker 0x400000000
    /*+0x60*/ /* shape-dependent trailing slot */
} PatternShape;
```

`typeinfo_str` always closes with `]`. The constructor slices its class name out of `__PRETTY_FUNCTION__`,
and the surrounding macro expansion ends with that bracket. A literal like
`mlir::nv_tile_ir::as::{anonymous}::FuncOpConversion]` is the normal form, and the trailing `]`
is a reliable fingerprint when scanning for pattern objects in a stripped binary.

The inline `SmallVector` at `+0x38` lists the operations the pattern intends to generate. Empty
vectors carry the marker `0x400000000` in the size word; non-empty vectors point at a heap buffer of
`OperationName` slots. Both layouts are valid; the marker is the discriminator.

## Four Shapes

Four sizes exist, and the only thing that varies between them is the trailing slot at `+0x60`. The
prefix is identical across all of them.

| Shape | Size      | `+0x60` slot           | Used by                                          |
|-------|-----------|------------------------|--------------------------------------------------|
| A     | 0x60 (96 B)  | (none)               | minimal `RewritePattern` with no extra state     |
| B     | 0x68 (104 B) | `MLIRContext *`      | standard `OpConversionPattern`                   |
| C     | 0x70 (112 B) | `TypeConverter *`    | `OpConversionPattern` carrying a type converter  |
| D     | 0x78 (120 B) | closure tuple        | rare closure-capturing patterns                  |

Shape A is the bare layout — no extra fields beyond the prefix, matches one operation name, rewrites
in place. Canonicalisers and small folds live here.

Shape B carries a second pointer at `+0x60`: a re-stored `MLIRContext *`. The conversion driver reads
the context from this slot rather than from `+0x20` when it populates the type-converter-free path of
`OpConversionPattern`. The duplication is intentional; a scan that misses it misclassifies Shape B as
Shape A.

Shape C carries a `TypeConverter *` at `+0x60`. This is the workhorse conversion pattern — the
pattern calls into the converter when materialising operand and result types. One converter is shared
across every pattern in a population set, so a single converter object accounts for many Shape C
entries.

Shape D embeds a closure tuple at `+0x60`, opaquely pointing at a heap-allocated tuple whose payload
includes a `std::function<bool(int)>` predicate. The closure lets a pattern condition its match on
captured tile sizes, lane counts, or feature toggles without specialising the class itself. The main
TileAA/TileAS phase in `ConvertTileASToLLVM` registers a small number of these.

## Eight-Slot Vtable

Shapes B, C, and D all point at the same eight-slot `OpConversionPattern` vtable.

| Slot | Function                       | Notes                                                   |
|------|--------------------------------|---------------------------------------------------------|
| 0    | typeinfo helper                | rtti accessor for the pattern class                     |
| 1    | dtor (delete)                  | calls `j_j_free` on the pattern object                  |
| 2    | dtor (no delete)               | invariant body `sub_36C8EC0`                            |
| 3    | empty trait                    | `nullsub_11937` at `0x447F250`, returns immediately     |
| 4    | getDebugName                   | returns `typeinfo_str` from `+0x28`                     |
| 5    | match                          | sometimes inlined into slot 6                           |
| 6    | matchAndRewrite                | the pattern body                                        |
| 7    | getDependentOperationNames     | returns `generatedOps` from `+0x38`                     |

Slots 2 and 3 are invariant across every concrete pattern: `sub_36C8EC0` for the non-deleting
destructor body, `nullsub_11937` (at `0x447F250`) for the empty trait callback. That pair is the
reliable fingerprint for the eight-slot vtable. A vtable whose slot 2 is not `sub_36C8EC0` or whose
slot 3 is not `nullsub_11937` is not an `OpConversionPattern`.

Slot 5 is sometimes a real `match` body, sometimes a stub deferring to slot 6. The combined form is
the default — most patterns share legality check and rewrite. A separate slot 5 is rare and signals
a pattern with an expensive feasibility check that should not be paid again in the rewrite phase.

Slot 7 returns the inline `SmallVector` at `+0x38`. The conversion driver reads this list to seed the
worklist when the pattern itself generates new operations. An empty list with the inline marker
`0x400000000` is a valid return; the driver treats it as "no further dependence."

## Five-Slot RewritePattern Vtable

Shape A points at a smaller, five-slot vtable.

| Slot | Function          |
|------|-------------------|
| 0    | typeinfo helper   |
| 1    | dtor (delete)     |
| 2    | dtor (no delete)  |
| 3    | matchAndRewrite   |
| 4    | getDebugName      |

No empty-trait slot, no dependent-operation accessor. A pattern prefix at `+0x00..+0x60` paired with
a five-entry vtable is a plain `RewritePattern`.

712 entries in the catalogued binary match the eight-slot fingerprint and 235 match the five-slot
fingerprint — together accounting for every pattern object the constructors register.

## Closure Patterns

Shape D is the only shape that owns a non-trivial heap object beyond the prefix. The closure tuple
at `+0x60` wraps an `std::function<bool(int)>` predicate along with the original lambda's captures.
The tuple's destructor runs from slot 1 of the pattern's own vtable, which calls `j_j_free` on the
closure pointer before freeing the pattern object itself.

`match` queries the predicate, which is why slot 5 of a Shape D pattern is almost always a real
function rather than a stub — the closure makes the match path heavier than the rewrite path.
ConvertTileASToLLVM picks this shape for patterns whose legality depends on tile-shape attributes
that cannot be encoded as a simple operation-name root.

## Concrete Cluster

The largest contiguous run of pattern objects sits in the constructor that populates
`GenericOpPattern<arith::*Op>`. The cluster spans `0x59B5480..0x59B61A0`, contains 43 pattern objects
of Shape C, and uses a stride of `0x50` because the constructor packs successive entries with no
padding between adjacent `0x70`-byte objects (the cluster is built from a static array). The vtable
pointer is identical for every entry; the `op_name` and `typeinfo_str` fields vary across the cluster
because each entry roots a different arith operation.

Locating that cluster and following its `op_name` strings is the cheapest way to enumerate the arith
lowering set without entering the constructor itself. The same trick works for any pattern set built
from a static array: scan for a run of equally spaced objects with a shared vtable pointer.

## Pattern Application Drivers

Once a pattern object has the shape and vtable documented above, it still has to be applied. Tileiras
runs the standard MLIR application pipeline plus a PDL-to-PDLInterp compile stage, and the flow
splits cleanly into four sub-routines that own one stage each. The stages are ordered: a mutable
vector is populated, frozen into an immutable lookup table, walked by the greedy driver, and
optionally wrapped by a partial- or full-conversion driver.

```c
void apply_pattern_set(Operation *root, MLIRContext *ctx) {
    RewritePatternSet           set(ctx);        // std::vector<std::unique_ptr<Pattern>>, 8 B stride
    populate_arith_generic_op_patterns(set);     // sub_873F30; 43 Shape C pushes, one arena slab each
    /* ... further populate* helpers ... */

    FrozenRewritePatternSet     frozen(set);     // sub_36F9730; sort by (benefit desc, kind),
                                                 // build OperationName* fingerprint hashmap,
                                                 // compile any PDL bytecode to PDL Interpreter ops
    ConversionTarget            target(*ctx);
    /* populate legal/illegal/dynamic ops on target ... */

    if (failed(applyPartialConversion(root,      // sub_1308320; 56 KB body, gpu->nvvm driver
                                      target,
                                      frozen))) {
        rollback_partial_changes(root);
    }
    /* sub_36F8A00 runs on frozen's destruction; frees fingerprint map + patterns */
}
```

The greedy driver itself is the inner loop of `applyPartialConversion`. It walks the IR via the
walker at `sub_447FBB0`, looks each operation up in the frozen fingerprint hashmap, dispatches the
highest-benefit matching pattern's vtable slot 6 (`matchAndRewrite`, see the eight-slot table
above), and reruns until fixed-point or until the iteration cap is hit. The cap is typically `10`;
exceeding it emits a "fixed-point not reached" remark rather than aborting the pass.

| Stage | Owner                                                     | Sub                    | Size       |
|-------|-----------------------------------------------------------|------------------------|------------|
| 1     | `RewritePatternSet` construction; one `populate*` helper  | `sub_873F30` (example) | varies     |
| 1a    | per-pattern arena allocation                              | `sub_44A8C20(0x68)`    | per push   |
| 2     | `FrozenRewritePatternSet` ctor + PDL bytecode compile     | `sub_36F9730`          | `15 119 B` |
| 3     | greedy match-and-rewrite loop                             | `sub_36D01B0`          | `4 653 B`  |
| 3a    | IR walker used by the greedy driver                       | `sub_447FBB0`          | walker     |
| 4     | `applyPartialConversion` / `applyFullConversion` driver   | `sub_1308320`          | `56 KB`    |
| 5     | `FrozenRewritePatternSet` dtor (tear-down)                | `sub_36F8A00`          | `124 B`    |

Stage 1 is `std::vector<std::unique_ptr<Pattern>>` with an 8-byte stride. Each `populate*` helper —
`sub_873F30` is the canonical example, registering the 43 arith `GenericOpPattern` entries that form
the Shape C cluster at `0x59B5480..0x59B61A0` — performs one `sub_44A8C20(0x68)` arena allocation
per push followed by one indirect vtable construction call. Patterns expressed in PDL bytecode
rather than C++ classes are pushed as PDL pattern modules; their compilation to PDL Interpreter ops
is deferred until stage 2.

Stage 2 walks the vector, sorts entries by benefit descending and then by pattern kind, and builds a
fingerprint hashmap keyed by `OperationName *` for O(1) per-op lookup. The PDL pattern-module
fallback also lives here: bytecode patterns are compiled to PDL Interpreter ops by this constructor.
The result is an immutable handle the application drivers can share without further synchronization.

Stage 4 is the large body. `sub_1308320` is the gpu-to-nvvm conversion driver and the canonical
instantiation; its 56 KB size is HexRays fully inlining the conversion template against the
per-instantiation operand and result types. It builds a `ConversionTarget` set of legal, illegal,
and dynamic ops, drives the greedy loop against the frozen set with a type converter, and on
failure walks the IR rolling back partial changes. The same template is instantiated for every
conversion pass; each instantiation produces its own large sub-routine.

Tear-down at `sub_36F8A00` is the frozen-set destructor. It frees the fingerprint hashmap and then
walks the pattern vector calling each pattern's vtable slot 1 (the deleting destructor), which in
turn calls `j_j_free` on the pattern object — the same `j_j_free` cited in the eight-slot table.

Benefit tie-break in stage 3 is lexicographic on `(benefit descending, registration order
ascending)`. Two patterns with equal benefit at the same registration order are a programmer error;
the greedy driver picks one deterministically (the first in vector order) but emits no warning. The
benefit value lives at `+0x18` of every pattern object, so the sort key is read directly from the
prefix without needing a virtual dispatch.

## How to Recognize in a Binary

The eight-slot vtable is the strongest fingerprint: any vtable whose slot 2 is `sub_36C8EC0`
(non-deleting destructor) and whose slot 3 is `nullsub_11937` at `0x447F250` is an
`OpConversionPattern`. The five-slot vtable is identified by absence — five slots, no empty-trait
nullsub — and by slot 3 being the `matchAndRewrite` body rather than a stub.

Object-level fingerprints:

- The 0x60-byte prefix terminating with a `SmallVector` size word that is either heap-pointing or
  carries the inline marker `0x400000000` (size=0, cap=4) at `+0x40` of the prefix is a pattern
  prefix.
- The typeinfo string at `+0x28` always ends with a literal `]` because it is sliced from
  `__PRETTY_FUNCTION__`. Scanning for `]` near a `MLIRContext *` slot at `+0x20` locates pattern
  objects in stripped code.
- The Shape C cluster `0x59B5480..0x59B61A0` of 43 equally spaced `0x70`-byte entries with a shared
  vtable is the easiest entry point for enumerating the arith lowering set.

## Consumers

Patterns are consumed by three driver families. The greedy match-and-rewrite loop at `sub_36D01B0`
drives canonicalisation and `applyPatternsAndFoldGreedily`-style fixed-point passes. The partial-
and full-conversion drivers built on `sub_1308320` drive dialect-to-dialect lowering — gpu-to-nvvm,
TileAS-to-LLVM, and similar template instantiations. The instruction-selection DAG in
[ISel DAG and Matcher Table](../codegen/iseldag-and-matchertable.md) reuses the same `0x7FFFFFFF`
operand-count mask documented in [Operation Layout — Fixed Header](operation-layout.md#fixed-header) when matching backend
`SDNode` operations, but does not consume MLIR pattern objects directly — it has its own table-driven
matcher.

## Cross-References

[TileAS to LLVM Lowering](../lowering/tileas-to-llvm.md) documents the pass that registers the
arith `GenericOpPattern` cluster. [Pattern Set and Type Converter](../lowering/pattern-set-and-typeconverter.md)
documents how the population functions wire Shape C patterns to a shared `TypeConverter`.
[Operation Layout](operation-layout.md) documents the operation header that pattern matchers read
through `+0x40`. [ISel DAG and Matcher Table](../codegen/iseldag-and-matchertable.md) covers the
later backend matcher that reuses the operand-count mask but runs on `SDNode`, not `Operation`.
