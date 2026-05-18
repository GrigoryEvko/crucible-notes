# nv_tileas Verifiers

## Abstract

The `nv_tileas` verifier layer shields the scheduler from impossible pipeline, memory, layout, TMA, and MMA shapes. Two broad families fall under it: async pipeline operations with region and token contracts, and target-facing operations — tiled memory ops, TMA descriptors, layout conversions, copies, dots, and block-scaled MMA.

These verifiers belong to the public reimplementation contract. Scheduling assumes they have already run. A malformed TileAS operation may still look like valid MLIR, but it can describe a pipeline or memory operation the target cannot execute.

## Async Pipeline Verification

Async pipeline verification is mostly structural. Region-bearing operations need matching block argument types, result types, and terminators. Token-only operations fall under ordinary operand/result arity and type rules.

| Operation | Required invariant |
|---|---|
| `create_pipeline` | results form the producer/consumer token pair for the pipeline |
| `produce_one` | producer region arguments match producer-token element types |
| `produce_one_async` | same as `produce_one`, plus async result token shape |
| `consume_one` | consumer region arguments match consumer-token element types |
| `consume_one_async` | same as `consume_one`, plus async result token shape |
| `producer_write` | producer body region arguments match the write payload |
| `producer_acquire` | operand is a producer token |
| `producer_commit` | operand is a producer token produced by the write/acquire path |
| `consumer_wait` | operand is a consumer token and `consumer_idx` is valid |
| `consumer_release` | operand is a consumer token produced by read/wait |
| `async.pipeline.yield` | operands match the enclosing pipeline region result contract |

```c
LogicalResult verify_pipeline_region(PipelineRegionOp op) {
    Region &region = op.body();

    if (!region.front().has_terminator()
        || region.front().terminator().name != "nv_tileas.async.pipeline.yield") {
        return op.emit_error("pipeline regions must end with 'nv_tileas.async.pipeline.yield'");
    }

    if (!block_args_match_token_elements(region, op.input_token_type())) {
        return op.emit_error("expects region arguement types to match with producer types");
    }

    if (!yield_operands_match_results(region, op.result_types())) {
        return op.emit_error("expects region result types to be match with operation result types");
    }
    return success();
}
```

The verifier also checks iterator agreement across structured control flow. When two branch arms yield a pipeline iterator, both yielded values must have the same iterator type. The diagnostic emitted on mismatch is `"branch arms must yield matching pipeline iterator types"`.

The diagnostics this routine emits:

| Diagnostic | Cause |
|---|---|
| `"pipeline regions must end with 'nv_tileas.async.pipeline.yield'"` | A pipeline region's terminator is the wrong op kind. |
| `"expects region arguement types to match with producer types"` (typo preserved) | Region block-argument types disagree with the producer-type list. |
| `"expects region result types to be match with operation result types"` (phrasing preserved) | Yield operand types disagree with the parent's result types. |
| `"branch arms must yield matching pipeline iterator types"` | Two `scf.if` arms yield iterators with different payload types. |
| `"consumer_idx out of range for consumer group"` | A `consumer_wait` or `consumer_read` carries an index outside the consumer group's bounds. |

## Region-Op Verifier Template

Five region-bearing pipeline ops share one verifier template: `produce_one`, `produce_one_async`, `consume_one`, `consume_one_async`, and `async.pipeline.yield`. Each op installs the template against its own `OperationName` and producer-type accessor, so the per-op bodies remain distinct in the binary even though their algorithm is identical.

The shared algorithm has four steps:

1. **Fetch producer types.** Each pipeline op carries a `producer_types: ArrayAttr<Type>` attribute encoding the type-list the producer agreed to emit. The verifier reads it from the op's attribute dictionary.
2. **Iterator-arg unwrap.** Block arguments of type `PipelineIteratorType` wrap a payload type. The verifier unwraps each block argument before comparing it against the producer-type entry; the producer-type list is already in payload form, so a double unwrap would compare payload against payload-of-payload and accept type-incoherent regions.
3. **Arity and type match.** The verifier walks the region's block-argument list and the producer-type list in parallel. On length or per-position mismatch, it emits `"expects region arguement types to match with producer types ["` (verbatim, including the typo `"arguement"`), followed by the producer-type list, `"], but got: ["`, the actual types, and a closing `"]"`.
4. **Terminator-yield match.** The region's terminator — `nv_tileas.async.pipeline.yield` — carries its own operand types. These must equal the parent op's result-type list. On mismatch, the verifier emits `"expects region result types to be match with operation result types ["` (verbatim, with the additional grammatical oddity).

```c
LogicalResult verify_pipeline_region_op(Operation *op) {
    ArrayRef<Type> producers   = op->getAttr("producer_types").cast<ArrayAttr>().getValues();
    Region        &body        = op->getRegion(0);
    BlockArgListType args      = body.front().getArguments();

    if (args.size() != producers.size()) {
        return emit(op, "expects region arguement types to match with producer types [",
                    producers, "], but got: [", args.getTypes(), "]");
    }
    for (size_t i = 0; i < args.size(); ++i) {
        Type bodyArg = unwrap_pipeline_iterator(args[i].getType());
        if (bodyArg != producers[i]) {
            return emit(op, "expects region arguement types to match with producer types [",
                        producers, "], but got: [", args.getTypes(), "]");
        }
    }

    Operation     *term        = body.front().getTerminator();
    ArrayRef<Type> termTypes   = term->getOperandTypes();
    ArrayRef<Type> resultTypes = op->getResultTypes();
    if (termTypes != resultTypes) {
        return emit(op, "expects region result types to be match with operation result types [",
                    resultTypes, "], but got: [", termTypes, "]");
    }
    return success();
}
```

Two diagnostic invariants are worth preserving. The typo `"arguement"` and the phrasing `"result types to be match"` are stable across all five verifiers — error-scraping infrastructure downstream has been matching them exactly, and silently fixing them breaks log capture. The iterator-unwrap step always runs on the block-arg side, never on the producer-type side.

> ⚡ **QUIRK — two preserved English errors are part of the public diagnostic contract**
> The verifier emits `"region arguement types to match"` (noun typo) and `"region result types to be match with"` (verb-form mistake) verbatim across all five pipeline ops. These look like obvious bugs but are wire-format-stable strings: log scrapers, frontends, and golden tests downstream key on the exact text. Silently correcting either string is a contract break with the same blast radius as renaming an op — reimplementations must keep both errors byte-identical, and any fix has to roll out at the consumer side first.

## Agent Switch Verification

`agent_switch` has two region groups — one leaving an agent context, one entering another. The verifier checks that the regions agree on warp count and that the sum of requested warps doesn't exceed the enclosing launch budget.

```c
LogicalResult verify_agent_switch(AgentSwitchOp op, GpuModuleInfo module) {
    SmallVector<uint32_t> counts = op.agent_warp_counts();
    if (!all_equal_or_inherited(counts)) {
        return op.emit_error("agent regions disagree on warp count");
    }
    if (resolved_warp_count(counts) > module.available_warps()) {
        return op.emit_error("agent warp count exceeds module budget");
    }
    return success();
}
```

## TMA Verification

TMA operations get checked against atom kind, descriptor shape, box dimensions, memory layout, and padding behavior.

| Operation | Required invariant |
|---|---|
| `async.tiled_tma_load` | atom is a TMA load atom; box dimensions match; element stride is one |
| `async.tiled_tma_store` | atom is a TMA store atom; box dimensions and layout are store-compatible |
| `async.tiled_atomic_rmw` TMA mode | atom is a TMA reduce atom; unsupported scatter modes are rejected |
| `make_tiled_tma_desc` | descriptor pointer is aligned; captured values are representable; structured-control dependencies are rejected |

```c
LogicalResult verify_tma_load(TmaLoadOp op) {
    if (!op.atom().is_tma_load()) {
        return op.emit_error("expected a TMA load atom");
    }
    if (op.box_dims().size() != op.atom().box_dims().size()) {
        return op.emit_error("TMA box dimensions do not match atom box dimensions");
    }
    if (op.element_stride() != 1) {
        return op.emit_error("TMA descriptors require unit element stride");
    }
    if (!op.shared_layout().is_tma_compatible()) {
        return op.emit_error("shared-memory layout is not TMA-compatible");
    }
    return success();
}
```

The TMA diagnostic surface:

| Diagnostic | Cause |
|---|---|
| `"expected a TMA load atom"` | The op's `atom` attribute references a non-load atom on a load op. |
| `"expected a TMA store atom"` | Same for a store op. |
| `"expected a TMA reduce atom"` | Same for a TMA atomic-RMW op. |
| `"TMA box dimensions do not match atom box dimensions"` | The op's box-dim count differs from the atom's. |
| `"TMA descriptors require unit element stride"` | A non-unit element stride was passed to a TMA descriptor builder. |
| `"shared-memory layout is not TMA-compatible"` | The shared-memory layout does not match a swizzle-and-stride combination the TMA descriptor accepts. |
| `"TMA descriptor capture references a non-representable value"` | The descriptor captures an SSA value the descriptor ABI cannot represent (for example, a value defined inside a structured-control region). |

Descriptor capture is deliberately conservative. A descriptor moved to the host or passed through the descriptor ABI must not depend on values the ABI cannot represent.

## Tiled Memop Verification

`tiled_load`, `tiled_store`, and `tiled_atomic_rmw` share a base shape:

- operand segments are `{view, coords, offsets, token}`;
- token segment has zero or one value;
- coordinate count matches the view rank, plus any descriptor-specific coordinate;
- coordinate type matches the memref index type;
- tile shape matches tensor shape;
- tile element type matches view element type;
- tile dimensions are positive powers of two and do not exceed the implementation limit.

Load and store differ in allowed memory semantics.

| Operation | Additional rules |
|---|---|
| `tiled_load` | acquire and acquire-release semantics are rejected |
| `tiled_store` | release and acquire-release semantics are rejected; padding and in-bounds flags must agree |
| `tiled_atomic_rmw` | `rmw_mode` is required; 8-bit types and 16-bit integer atomics are rejected |

```c
LogicalResult verify_tiled_memop(TiledMemOp op) {
    if (failed(verify_operand_segments(op))) {
        return op.emit_error("operandSegmentSizes does not match the op schema");
    }
    if (op.has_token() && op.token_segment_size() > 1) {
        return op.emit_error("tiled memop token segment must hold zero or one value");
    }
    if (op.coord_count() != op.view().rank() + op.descriptor_coord_count()) {
        return op.emit_error("tiled memop coordinate count does not match the view rank");
    }
    if (!coords_are_index_typed(op)) {
        return op.emit_error("tiled memop coordinates must be index-typed");
    }
    if (op.tile_shape() != op.view().shape()) {
        return op.emit_error("tile shape must match tensor shape");
    }
    if (op.tile_element_type() != op.view().element_type()) {
        return op.emit_error("tile element type must match view element type");
    }
    for (int64_t d : op.tile_shape()) {
        if (d <= 0 || (d & (d - 1)) != 0) {
            return op.emit_error("tile dimensions must be positive powers of two");
        }
    }
    if (total_elements(op.tile_shape()) > MAX_TILE_ELEMENTS) {
        return op.emit_error("tile total element count exceeds the implementation limit");
    }
    return verify_memory_semantics(op);
}
```

The tiled-memop diagnostic surface:

| Diagnostic | Cause |
|---|---|
| `"operandSegmentSizes does not match the op schema"` | The segment-size attribute does not partition the operand list correctly. |
| `"tiled memop token segment must hold zero or one value"` | More than one token slot was supplied for a single tile op. |
| `"tiled memop coordinate count does not match the view rank"` | The number of coordinate operands disagrees with the view's rank plus the descriptor-specific coordinate count. |
| `"tiled memop coordinates must be index-typed"` | A coordinate operand has a non-index type. |
| `"tile shape must match tensor shape"` | The result tile's shape disagrees with the view's tensor shape. |
| `"tile element type must match view element type"` | The result tile's element type differs from the view's element type. |
| `"tile dimensions must be positive powers of two"` | A tile dimension is zero, negative, or not a power of two. |
| `"tile total element count exceeds the implementation limit"` | The product of the tile dimensions exceeds the implementation cap. |
| `"tiled_load rejects acquire_release semantic"` | A load op carries `acquire_release` memory semantic. |
| `"tiled_store rejects acquire semantic"` | A store op carries `acquire` or `acquire_release` memory semantic. |
| `"padding value is allowed only when in-bounds is false"` | A `padding_value` attribute appears alongside `in_bounds = true` on some axis. |
| `"tiled_atomic_rmw requires rmw_mode"` | The atomic op is missing its `rmw_mode` attribute. |
| `"tiled_atomic_rmw rejects 8-bit element types"` | An 8-bit element was passed to an atomic op. |
| `"tiled_atomic_rmw rejects 16-bit integer atomics"` | A 16-bit integer atomic was attempted. |
| `"16-bit floating atomic supports only add, max, and min"` | A 16-bit floating atomic uses an unsupported mode. |

Atomic RMW carries stricter element-type rules. Sixteen-bit floating-point atomics are limited to add, max, and min. The path rejects fadd and exchange modes on 16-bit floats so the lowering can pick a supported target operation without ambiguity.

## Layout, Copy, and Dot Verification

`convert_layout` checks that source and destination tiles have the same element type, the same total element count, and layouts that the materialization pass knows how to decompose.

`copy` and `async.copy` require an `atom` attribute and a legal source/destination memory-space pair. Legal pairs include global/register, global/shared, register/global, register/shared, register/tensor, shared/global, shared/register, shared/tensor, and tensor/register.

`dot` and `async.dot` require an atom, compatible A/B element types, the right signedness attributes for integer MMA, and a Float32 accumulator for floating-point paths.

## Block-Scaled MMA Verification

Block-scaled MMA is the Blackwell-specific correctness gate driving the `tcgen05.mma::block_scale` family. Every `nv_tileas.block_scaled_mma` op flows through one verifier function shared between the op builder, the ConvertTileAAToTileAS MMA lowering, and the dialect builder.

The verifier takes seven typed handles — A type, B type, accumulator type, scale-factor-A (`sfa`) type, scale-factor-B (`sfb`) type, the MMA atom kind handle, and the destination tile type — followed by a selector that picks between the 2-CTA and 1-CTA atom catalogs. On success it returns a packed `(atom_K << 32) | vecSize`; on failure it returns zero and the diagnostic is already on the op. Callers treat `0` as "verification rejected", not as a legal `(0, 0)` shape.

### Type Resolution

MLIR built-in types are pointer-comparable singletons. The verifier resolves every type predicate by comparing the incoming handle against the canonical entries for `Float32`, `Float8E8M0FNU`, `Float8E5M2`, `Float8E4M3FN`, and the two FP4 variants (`Float4E2M1FN` and `FloatNV4E0M3F`). The FP4 variants share an internal type slot intentionally: NVIDIA reuses the same logical tile element for the OCP MX-FP4 and NVFP4 paths, and the scale-factor ratio is what resolves which Blackwell instruction kind to emit. A verifier that tries to disambiguate FP4 by element type alone rejects legal NVFP4 programs.

### Phase-Ordered Diagnostics

Eleven diagnostics cover five phases. The phase order is fixed: presence, agreement, accumulator, K-extent, catalog. Reordering the phases changes which diagnostic the user sees when more than one phase is wrong, and breaks downstream test expectations.

| Phase | Diagnostic | Cause |
|---|---|---|
| 1 — scale-factor presence | `"fp4 mma should expect scaling factors"` | A type pair landed on the FP4 slot but `sfa` or `sfb` is missing |
| 2 — scale-factor agreement | `"sfa and sfb element type mismatch"` | `sfa` and `sfb` resolve to different types |
| 3 — accumulator type | `"expects c type to be Float32"` | The destination/accumulator type is not `Float32` |
| 4 — K-extent agreement | `"Scale factor vector size mismatch:"` followed by two formatted K extents | A and B disagree on the scale-factor K dimension after vectorisation |
| 5 — atom catalog | `"unsupported block-scaled mma configuration"` and four narrower variants for FP8, MX-FP4, NVFP4, and 2-CTA selector failures | The resolved `(atom_K, vecSize)` does not appear in the legal catalog |

The trailing colon in the phase-4 diagnostic signals that two integers follow on the same line. Reimplementations that print the integers on a separate line break log-scrapers.

### Legal Atom Catalog

Three `(atom_K, vecSize)` pairs survive verification. Each maps to exactly one Blackwell MMA kind, and each has a fixed packed return value:

| `(atom_K, vecSize)` | Type pattern | PTX kind | Return |
|---|---|---|---|
| `(32, 32)` | FP8 (`E5M2` or `E4M3FN`) tiles with `E8M0` scales | `tcgen05.mma.kind::f8f6f4` | `0x2000000020` |
| `(64, 16)` | FP4 tiles with `E8M0` or `E4M3FN` scales | `tcgen05.mma.kind::mxf4` (OCP MX-FP4) | `0x4000000010` |
| `(64, 32)` | FP4 tiles with `E8M0` scales, block size 64 | `tcgen05.mma.kind::mxf4nvf4` (NVFP4) | `0x4000000020` |

Shape `(64, 16)` discriminates OCP MX-FP4 from NVFP4. OCP requires scale block size 16 and tolerates an `E4M3FN` scale; NVFP4 pins block size to 32 and demands `E8M0` scales over a 64-K tile. The 2-CTA selector further narrows the catalog — 1-CTA accepts all three rows, 2-CTA rejects the NVFP4 row because Blackwell has no `mxf4nvf4` 2-CTA atom.

```c
uint64_t verify_block_scaled_mma(Type a, Type b, Type c,
                                 Type sfa, Type sfb,
                                 MmaAtomKind atom, Type dst,
                                 bool two_cta) {
    bool is_fp4 = (a == Float4E2M1FN) || (a == FloatNV4E0M3F);
    bool is_fp8 = (a == Float8E5M2)   || (a == Float8E4M3FN);

    if (is_fp4 && (!sfa || !sfb)) {
        emit_diag(op, "fp4 mma should expect scaling factors");
        return 0;
    }
    if (sfa && sfb && sfa != sfb) {
        emit_diag(op, "sfa and sfb element type mismatch");
        return 0;
    }
    if (c != Float32) {
        emit_diag(op, "expects c type to be Float32");
        return 0;
    }

    uint32_t atom_k   = resolve_atom_k(a, b, atom);
    uint32_t vec_size = resolve_vec_size(a, sfa);
    uint32_t k_a      = scale_factor_k_extent(a, sfa);
    uint32_t k_b      = scale_factor_k_extent(b, sfb);

    if (k_a != k_b) {
        emit_diag(op, "Scale factor vector size mismatch: ", k_a, ", ", k_b);
        return 0;
    }

    if (is_fp8 && atom_k == 32 && vec_size == 32) {
        return ((uint64_t)32 << 32) | 32;
    }
    if (is_fp4 && atom_k == 64 && vec_size == 16 && !two_cta) {
        return ((uint64_t)64 << 32) | 16;
    }
    if (is_fp4 && atom_k == 64 && vec_size == 32 && !two_cta) {
        return ((uint64_t)64 << 32) | 32;
    }

    emit_diag(op, "unsupported block-scaled mma configuration");
    return 0;
}
```

The packed return uses `atom_K` in the high word and `vecSize` in the low word, both as 32-bit unsigned values. Zero is reserved for failure; legal shapes always have at least the `vecSize` field set. The op builder reads the low 32 bits as `vecSize` and the high 32 bits as `atom_K` before writing the result into the op's atom attribute — any other return encoding silently corrupts the op.

### Worked Failure: sfa/sfb Element Type Mismatch

A concrete walk illustrates the phase-2 diagnostic. Consider the input

```mlir
%d = nv_tileas.block_scaled_mma %a, %b, %c, %sfa, %sfb
    { atom = #nv_tileas<atom mxf4>,
      operandSegmentSizes = array<i32: 1, 1, 1, 1, 1> }
    : tile<128x64xf4E2M1FN>, tile<64x128xf4E2M1FN>, tile<128x128xf32>,
      tile<128x4xf8E8M0FNU>, tile<4x128xf8E4M3FN>
    -> tile<128x128xf32>
```

The atom selects MX-FP4 with `(atom_K=64, vecSize=16)`. Phase 1 passes because both `sfa` and `sfb` are present. Phase 2 fails: `sfa` resolves to `Float8E8M0FNU` while `sfb` resolves to `Float8E4M3FN`. The verifier emits `"sfa and sfb element type mismatch"` and returns `0`. Phases 3 through 5 never run; the op never reaches lowering. Fixing the input requires the producer to choose a single scale-factor element type (typically `Float8E8M0FNU` for MX-FP4, since `Float8E4M3FN` is legal only on the OCP MX-FP4 path that further constrains the block size).

A correct reimplementation therefore enforces:

- Phase order is presence, agreement, accumulator, K-extent, catalog.
- The FP4 element-type slot is shared. Disambiguation is by `(atom_K, vecSize)` and the 2-CTA selector, never by element identity alone.
- The packed return uses `atom_K` in the high word and `vecSize` in the low word, both as 32-bit unsigned values.
- Zero is reserved for failure.

## Shared Helper Rules

Several checks are reused across the dialect:

| Helper concept | Rule |
|---|---|
| tile dimensions | every dimension must be a positive power of two; total tile size is capped |
| memory semantics and scope | scope is required when semantic is stronger than weak; weak semantic must not carry scope |
| store padding | padding value is allowed only when in-bounds is false |
| special padding | NaN, infinities, and negative zero are valid only for floating-point elements |
| operand segments | segment-size attribute must match the op schema |
| pipeline terminators | pipeline regions must end in `async.pipeline.yield` |

## OpTrait::nv_tile Inventory

Verifier behavior in this dialect is partly templated by op traits — small mixin classes the
`OpTrait::nv_tile` namespace declares once and that every op in the families above stamps onto its
declaration. The trait family is closed: twenty-three traits, each with a single semantic job, and
verification dispatches on the trait set attached to a concrete op rather than on a per-op
switch. Recovering this table from the binary is straightforward because every trait emits a
typeinfo string of the form `OpTrait::nv_tile::<TraitName>` that the verifier framework reads at
registration time.

| Trait | Role in Verification |
|---|---|
| `FirstOperandIsNonAliasingQueue` | the queue operand must not alias any other op input or output |
| `MemoryModelReadTrait`, `MemoryModelWriteTrait`, `MemoryModelReadWriteTrait` | tags the op for the memory-effect collector; orthogonal to the side-effect interface |
| `MustHaveMemLayoutAmongOperandsAndResult` | at least one operand or the result must carry an explicit memory layout |
| `PipelineAcquireOpTrait`, `PipelineReleaseOpTrait` | marks the op as a producer-acquire or producer-release boundary in the async pipeline region |
| `ResultsAreSharedEncoding` | every result inherits the shared-memory encoding of the producing tile |
| `SameLoadStoreOperandsAndResultEncoding`, `SameLoadStoreOperandsAndResultShape` | tiled load/store must agree on both encoding and shape between operands and result |
| `SameLoadStoreOperandsEncoding`, `SameLoadStoreOperandsShape` | weaker form, used by ops with no result (stores) |
| `SameOperandsAndResultEncoding`, `SameOperandsEncoding` | encoding-only invariants for ops that touch tile values without changing shape |
| `SameOperandsAndResultsAtom`, `SameOperationAndResultsAtom` | every operand or result that carries a copy/MMA atom must report the same atom identity |
| `SameTiledViewAndTensorShapeTrait` | shape on a tiled view must match the producing tensor's shape on the same axes |
| `TensorSizeTrait` | total tile size must be representable as a positive power of two within the per-dialect cap |
| `TensorTypeHavingLayout` | every tensor operand must already carry a layout attribute when the verifier runs |
| `TiledLoadStoreOpTrait`, `TiledLoadStoreOpSameElementTypeTrait` | grouping trait that pulls in the tiled memop helper bundle; the element-type variant adds the elementType-match check |
| `TiledPaddingValueTrait` | padding value is allowed only when the in-bounds attribute is false (also enforced by the Shared Helper Rules table) |
| `ValidTileASLoadOperandsAndResultEncoding` | combined operand/result encoding check specific to `tiled_load` and its async variant |

A correct reimplementation declares each trait as a mixin whose `verifyTrait` returns
`LogicalResult` and chains into the op's bespoke verifier. The trait order is irrelevant — every
trait either succeeds standalone or emits its own diagnostic — and the framework runs them all
before the op's own `verify` method gets a chance.

## Cross-References

[Operation Roster and Builders](op-roster-and-builders.md#operation-families) catalogues the operations these verifiers run against, with full operand/result tables and a worked producer/consumer pipeline example. [Types](types.md#pipeline-types) describes the pipeline-token and iterator types the region-op verifier template inspects. [Folds and Memory Consistency](folds-and-mem-consistency.md#canonicalization-patterns) describes the rewrites that run after verification succeeds. The `nv_tileaa` block-scaled MMA contract documented here is grounded by the `dot` verifier in [nv_tileaa Types, Attributes, Verifiers — Dot Diagnostics](../nv_tileaa/types-attrs-verifiers.md#dot-and-block-scaled-mma-diagnostics). The OpInterface side of the dispatch story — including the `BasicPtxBuilderInterface` and `PtxBuilderOpInterface` families consumed by NVVM lowering — is inventoried in [Interface Vtables and Dispatch — Interface Inventory](../../mlir-infra/interface-vtables.md#opinterface-inventory).

