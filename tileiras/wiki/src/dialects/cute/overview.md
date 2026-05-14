# cute Dialect Overview

## Abstract

`cute` is tileiras's MLIR form of CUTLASS cuTe layout algebra. It encodes shapes, strides, layouts, swizzles, coordinates, tiles, pointer views, copy atoms, and MMA atoms — together with the operations that compose, divide, complement, coalesce, and filter them — and stops short of binding any of it to NVIDIA hardware. That binding is the job of `cute_nvgpu`. Every later GPU-specific dialect (`cute_nvgpu`, `nvgpu`, `nvvm`) reads layout values produced here.

`cute` is not a code-generation dialect. Its values describe structure: how a logical tile maps to physical coordinates, how coordinates become offsets, how one layout composes with another, how a tiled copy or tiled MMA partitions work across lanes, warps, and memory spaces. That makes it the common language shared by CUTLASS pipeline modeling, TileAS layout assignment, TMA descriptor construction, and MMA lowering.

## Role in the Cascade

```text
cuda_tile / nv_tileaa / nv_tileas
    |
    | choose tile shapes, views, and partitioning
    v
cute
    |
    | attach target-specific atoms and SM-tier constraints
    v
cute_nvgpu
    |
    | normalize to nvgpu and nvvm
    v
PTX
```

Think of `cute` as a compact typed form of the same algebra that CUTLASS C++ expresses with templates. The templates become values and attributes that passes inspect, compose, verify, and lower.

## Core Concepts

| Concept | Meaning | Typical use |
| --- | --- | --- |
| Shape | Extents of a logical tile or nested coordinate tuple | Describes the iteration space of a tile. |
| Stride | Offset step for each coordinate dimension | Converts coordinates into linear offsets. |
| Layout | Shape plus stride, optionally decorated with swizzle | Maps logical coordinates to storage locations. |
| Tile | A grouped shape/layout fragment | Represents a fragment moved or computed as a unit. |
| Coord | A point in a shape or tile | Indexes layouts, views, and partitioned fragments. |
| Swizzle | Bit permutation applied to low address bits | Avoids bank conflicts or matches hardware layout rules. |
| View | Pointer or memref plus layout metadata | Describes an addressed object without losing its layout. |
| Tiled copy / MMA | Layout plus atom-level partitioning | Feeds target-specific copy or matrix-multiply lowering. |

The key invariant is that `cute` values remain algebraic. A layout should be composable and queryable without knowing whether it will eventually become a TMA descriptor, an ldmatrix load, a WGMMA operand, or a Blackwell tensor-memory operation.

## Layout Semantics in One Line

A layout maps a coordinate to an offset. The simplest model is `(shape, stride)`; the real dialect adds nested tuples, composition, complement, divide, product, and swizzles on top of that single primitive. The algebraic rules and the concrete `compose`/`complement`/`divide`/`product` definitions live on the algebra page below; this overview only states the kernel.

```c
int64_t layout_offset(Layout L, Coord c) {
    int64_t offset = 0;
    for (int d = 0; d < rank(c); ++d) offset += c[d] * L.stride[d];
    return apply_swizzle(L.swizzle, offset);
}
```

For a reimplementation, the storage class the original compiler picks does not matter. What does matter: equivalent layouts canonicalize consistently, nested tuple layouts preserve rank and dimension identity, and swizzle composition stays explicit until a target-specific lowering consumes it.

## Where to Find What

The dialect is split across four pages by concern. Use this map to find the exact place a topic is documented; the overview does not duplicate any of these.

| Topic | Page |
|---|---|
| Layout algebra rules (composition, complement, divide, product, coalesce, filter) | [Layout Algebra and Descriptor Grammar — Algebra Rules on Shape and Stride Tuples](layout-algebra-and-descriptor-grammar.md#algebra-rules-on-shape-and-stride-tuples) |
| Tuple-shape grammar, swizzle composition, descriptor round-trip | [Layout Algebra and Descriptor Grammar — Descriptor Grammar](layout-algebra-and-descriptor-grammar.md#descriptor-grammar) |
| Tile partitioning ops (`local_tile`, `local_partition`, `group_modes`, `dice`, `slice`) | [Tile and Divide Ops — Builder Operations](tile-and-divide-ops.md#builder-operations) |
| Atom builders (`make_atom`, `make_tiled_copy`, `make_tiled_mma`) and desugar rewrites | [Atom Builders and Desugar — Atom Builder Contract](atom-builders-and-desugar.md#atom-builder-contract) |
| `cute.make_int_tuple` hub, `make_layout` desugaring shape | [Atom Builders and Desugar — `make_int_tuple` Hub](atom-builders-and-desugar.md#make_int_tuple-hub) |
| Kernel-entry ABI (`cute.kernel` → `nvvm.kernel`, grid-constant arg-attrs) | [Atom Builders and Desugar — Kernel-entry ABI](atom-builders-and-desugar.md#kernel-entry-abi) |
| Verbatim verifier diagnostics (every error string the dialect emits) | [Verifiers — Verbatim Diagnostics](verifiers.md#verbatim-diagnostics) |
| Mode-range, divide, product, tuple-arithmetic verifier algorithms | [Verifiers — Mode and Rank Checks](verifiers.md#mode-and-rank-checks) |
| `crd2idx` weak-congruence walk, worked diagnostic example | [Verifiers — Worked Example: `crd2idx` Weak Congruence Violation](verifiers.md#worked-example-crd2idx-weak-congruence-violation) |
| `LayoutTypeInterface` kind discriminator and per-kind dispatch tables | [Verifiers — LayoutTypeInterface Kind Discriminator](verifiers.md#layouttypeinterface-kind-discriminator) |

## In-Memory IR Tier

Treat `cute` as an in-memory compiler tier. It exists so passes can exchange rich layout objects without serializing every intermediate shape into the public input format. Textual rendering helps with debugging and documentation; production input normally enters through `cuda_tile`, `nv_tileaa`, `cutlass`, or another higher-level dialect, and the pipeline constructs `cute` objects internally.

Practical consequence: do not build tooling that depends on `cute` bytecode as a stable interchange format unless the serializer is explicitly provided. Textual dumps are for inspecting the compiler, not as a user-facing artifact.

## If You Know CUTLASS (open source) — cross-walk

The open-source `cute/` C++ headers map almost directly onto this dialect:

| CUTLASS C++ (cute namespace) | tileiras `cute` IR |
|---|---|
| `cute::Shape<...>` and `cute::Stride<...>` | hierarchical `(shape, stride)` tuples in a `!cute.layout` |
| `cute::Layout<Shape, Stride>` | `!cute.layout` type, kind-discriminated through the seven-entry sentinel table |
| `cute::Swizzle<B, M, S>` | `!cute.swizzle` value composed into a layout via `make_composed_layout` |
| `cute::make_tile`, `cute::make_layout` | `cute.make_tile`, `cute.make_layout` ops |
| `cute::Tensor<Engine, Layout>` | `cute.make_view` ties a pointer/memref to a layout |
| `composition`, `complement`, `logical_divide`, `logical_product` | identically-named `cute.*` ops |
| `cute::make_tiled_copy`, `cute::make_tiled_mma` | `cute.make_tiled_copy`, `cute.make_tiled_mma` (target binding deferred to `cute_nvgpu`) |
| Compile-time integer arithmetic in C++ templates | `cute.make_int_tuple` + `tuple_div/mod/mul/sub` ops |

The main difference is where the target boundary sits. The open-source `cute/` library compiles SM-specific `MMA_Atom` and `Copy_Atom` traits straight into the same headers; tileiras keeps the SM-neutral atoms in `cute` and pushes every target-specific atom into `cute_nvgpu`. A pass running inside `cute` should never need to ask which SM tier is in use. If it does, the layout choice belongs on the `cute_nvgpu` side.

## Cross-links

- [Layout Algebra and Descriptor Grammar — Descriptor Grammar](layout-algebra-and-descriptor-grammar.md#descriptor-grammar) covers the concrete grammar and [Round Trip](layout-algebra-and-descriptor-grammar.md#round-trip) rules.
- [Tile and Divide Ops — Divide Variants](tile-and-divide-ops.md#divide-variants) covers tile partitioning operations.
- [Atom Builders and Desugar — Per-Atom Desugar Rewrites](atom-builders-and-desugar.md#per-atom-desugar-rewrites) covers construction of copy and MMA atoms.
- [Verifiers — Verbatim Diagnostics](verifiers.md#verbatim-diagnostics) covers layout and atom verifier behavior.
