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

    if (!ends_with_pipeline_yield(region)) {
        return op.emit_error("pipeline regions must end with async.pipeline.yield");
    }

    if (!block_args_match_token_elements(region, op.input_token_type())) {
        return op.emit_error("pipeline region argument types do not match token elements");
    }

    if (!yield_operands_match_results(region, op.result_types())) {
        return op.emit_error("pipeline yield operands do not match operation results");
    }

    return success();
}
```

The verifier also checks iterator agreement across structured control flow. When two branch arms yield a pipeline iterator, both yielded values must have the same iterator type.

## Region-Op Verifier Quintuplet

Five region-bearing pipeline ops route through verifiers sitting at consecutive `.rodata` addresses. Each body is 1 995
bytes and byte-identical to its neighbours apart from the per-op `OperationName` slot pointer and the producer-type-list
source pointer. Treat them as one template stamped five times, not as five independently authored verifiers.

| Verifier | Address | Op |
|---|---|---|
| `produce_one` verify | `0x1478BB0` | `nv_tileas.async.pipeline.produce_one` |
| `produce_one_async` verify | `0x14795B0` | `nv_tileas.async.pipeline.produce_one_async` |
| `consume_one` verify | `0x1479FB0` | `nv_tileas.async.pipeline.consume_one` |
| `consume_one_async` verify | `0x147A9B0` | `nv_tileas.async.pipeline.consume_one_async` |
| 5th region-op verify | `0x147B250` | `nv_tileas.async.pipeline.yield` (likely; identity is MED-confidence) |

Each entry is reached through a thin per-op thunk — `sub_1479380`, `sub_1479D80`, `sub_147A780`, `sub_147B180`,
`sub_147BA20` — that resolves the op's registered `OperationName` and tail-calls into the verifier proper. The five
bodies stay distinct in the binary because each is referenced from its op's `AbstractOperation+0x68` verifier slot;
HexRays sees the duplication only after the slot indirection has been resolved per-class.

The shared algorithm has four steps:

1. **Fetch producer types.** Each pipeline op's `OperationName` carries a `producer_types: ArrayAttr<Type>` attribute
   encoding the type-list the producer agreed to emit. The verifier reads it from the op's attribute dictionary via
   `sub_1497220`, the `PipelineProducerTokenType` element-type getter.
2. **Iterator-arg remap via `sub_1496C90`.** Block arguments of type `PipelineIteratorType` (tagged
   `&unk_5B45A60`) need remapping before type comparison. The iterator type wraps a payload type, and the verifier
   compares the unwrapped payload against the producer-type entry. `sub_1496C90` is the unwrap.
3. **Arity and type match.** The verifier walks the region's block-argument list and the producer-type list in
   parallel. On length or per-position mismatch, it emits
   `"expects region arguement types to match with producer types ["` (verbatim, including the typo `"arguement"`).
4. **Terminator-yield match.** The region's terminator — a `nv_tileas.async.pipeline.yield` op — carries its own
   operand types. These must equal the parent op's result-type list. On mismatch, the verifier emits
   `"expects region result types to be match with operation result types ["` (verbatim, with the additional
   grammatical oddity).

Both diagnostics are followed by `"], but got: ["`, then the actual-types list, then a closing `"]"`. The format
reproduces the upstream MLIR `OpAsmPrinter` shape exactly so the IDE's error-jumping recognises the diagnostic. The
tail format helpers are `sub_4470160` → `sub_581460` → `sub_444ABF0` — the dialect's standard `Type`-list printer
chain.

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
        Type bodyArg = sub_1496C90(args[i].getType());      // unwrap PipelineIteratorType
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

The 1 995-byte length is not accidental. HexRays inlines the verifier template once per op subclass against the
per-class `OperationName` value and the per-class producer-type accessor; the diagnostic-format chain alone accounts
for roughly two thirds of each body. A faithful reimplementation should factor the template into a single function
templated on `OperationName` rather than stamp five near-clones — but the binary's layout, five separate
`.rodata`-resident bodies referenced from per-class slots, is part of the ABI contract for any downstream tool that
walks `AbstractOperation+0x68` directly.

Two invariants are worth preserving verbatim. First, the typo `"arguement"` and the awkward phrasing
`"result types to be match"` are stable across all five verifiers — error-scraping infrastructure downstream has been
matching them exactly, and silently fixing them breaks log capture. Second, the iterator-unwrap step always runs on
the block-arg side, never on the producer-type side: the producer-type list is already in payload form, and
double-unwrapping would compare apples against payload-of-payload and accept type-incoherent regions.

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
    verify_operand_segments(op);
    verify_optional_token(op);
    verify_coordinates(op.view(), op.coords());
    verify_tile_shape_and_element_type(op);
    verify_tile_dimensions(op.tile_shape());
    return verify_memory_semantics(op);
}
```

Atomic RMW carries stricter element-type rules. Sixteen-bit floating-point atomics are limited to add, max, and min. This path rejects fadd and exchange modes so the lowering can pick a supported target operation without ambiguity.

## Layout, Copy, and Dot Verification

`convert_layout` checks that source and destination tiles have the same element type, the same total element count, and layouts that the materialization pass knows how to decompose.

`copy` and `async.copy` require an `atom` attribute and a legal source/destination memory-space pair. Legal pairs include global/register, global/shared, register/global, register/shared, register/tensor, shared/global, shared/register, shared/tensor, and tensor/register.

`dot` and `async.dot` require an atom, compatible A/B element types, the right signedness attributes for integer MMA, and a Float32 accumulator for floating-point paths.

## Block-Scaled MMA Verification

Block-scaled MMA is the Blackwell-specific correctness gate driving the `tcgen05.mma::block_scale` family. Every
`nv_tileas.block_scaled_mma` op flows through `sub_14B71C0` (1 771 bytes, 63 basic blocks) — the densest verifier in
the dialect. Three callers reach it: the op builder `sub_14B28C0` (which clears the bottom three result bits with a
`~7` mask before reading the packed atom shape), the ConvertTileAAToTileAS MMA lowering at `sub_13DCEC0`, and the
dialect builder `sub_72C180`.

The signature takes seven typed handles — A type, B type, accumulator type, scale-factor-A (`sfa`) type,
scale-factor-B (`sfb`) type, the MMA atom kind handle, and the destination tile type — followed by a `char` selector
that picks between the 2-CTA and 1-CTA atom catalogs. On success the function returns a packed
`(atom_K << 32) | vecSize`; on failure it returns zero and the diagnostic is already on the op. Callers therefore
treat `0` as "verification rejected", not as a legal `(0, 0)` shape.

### Type-Singleton Bank

MLIR built-in types appear in the binary as pointer-comparable singletons stored in `.data.rel.ro`. The verifier
resolves every type predicate by comparing the incoming handle against a fixed table:

| Slot | Type |
|---|---|
| `&unk_5B46FA0` | unregistered placeholder (used when the dialect has not yet bound the type) |
| `&unk_5B46FA8` | erased / wildcard (skip-this-check sentinel) |
| `&unk_5BAADB8` | opaque target-side handle |
| `&unk_5BE6030` | `Float32` |
| `&unk_5BE6050` | `Float8E8M0FNU` (microscale exponent-only) |
| `&unk_5BE6068` | `Float4E2M1FN` and `FloatNV4E0M3F` share this slot; the verifier distinguishes them through the `sfa`/`sfb` ratio rather than the slot itself |
| `&unk_5BE6090` | `Float8E5M2` |
| `&unk_5BE60A0` | `Float8E4M3FN` |

The FP4 collision on `&unk_5BE6068` is intentional. NVIDIA reuses the same logical tile element for the OCP MX-FP4
and NVFP4 paths; the scale-factor ratio is what resolves which Blackwell instruction kind to emit. A verifier that
tries to disambiguate FP4 by element type alone rejects legal NVFP4 programs.

### Diagnostic Surface

Eleven diagnostics cover five phase failures. Two of them go through helpers the verifier shares across the dialect:
`sub_14B7090` emits a plain string (nine uses in this verifier alone) and `sub_14B6F30` emits a string followed by
an integer parameter (used twice, for the K-extent mismatch detail).

| Phase | Diagnostic | Cause |
|---|---|---|
| 1 — scale-factor presence | `"fp4 mma should expect scaling factors"` | A type pair landed on the FP4 slot but `sfa` or `sfb` is missing |
| 2 — scale-factor agreement | `"sfa and sfb element type mismatch"` | `sfa` and `sfb` resolve to different singletons |
| 3 — accumulator type | `"expects c type to be Float32"` | The destination/accumulator slot is not `&unk_5BE6030` |
| 4 — K-extent agreement | `"Scale factor vector size mismatch:"` followed by two formatted K extents | A and B disagree on the scale-factor K dimension after vectorisation |
| 5 — atom catalog | `"unsupported block-scaled mma configuration"` and four narrower variants for FP8, MX-FP4, NVFP4, and 2-CTA selector failures | The resolved `(atom_K, vecSize)` does not appear in the legal catalog |

Phase 4 formats its two integers through `sub_459A3F0`, the dialect's `Twine`-style integer-to-stream helper,
splicing `", "` from the rodata templates at `xmmword_4CD8D80` and `xmmword_4CD8D90`. The trailing colon in the
diagnostic signals that two integers follow on the same line — reimplementations that print the integers on a
separate line break log-scrapers.

### Legal Atom Catalog

Three `(atom_K, vecSize)` pairs survive verification. Each maps to exactly one Blackwell MMA kind, and each has a
fixed packed return value:

| `(atom_K, vecSize)` | Type pattern | PTX kind | Return |
|---|---|---|---|
| `(32, 32)` | FP8 (`E5M2` or `E4M3FN`) tiles with `E8M0` scales | `tcgen05.mma.kind::f8f6f4` | `0x2000000020` |
| `(64, 16)` | FP4 tiles with `E8M0` or `E4M3FN` scales | `tcgen05.mma.kind::mxf4` (OCP MX-FP4) | `0x4000000010` |
| `(64, 32)` | FP4 tiles with `E8M0` scales, block size 64 | `tcgen05.mma.kind::mxf4nvf4` (NVFP4) | `0x4000000020` |

Shape `(64, 16)` discriminates OCP MX-FP4 from NVFP4. OCP requires scale block size 16 and tolerates an `E4M3FN`
scale; NVFP4 pins block size to 32 and demands `E8M0` scales over a 64-K tile. The 2-CTA selector (the `char`
argument) further narrows the catalog — 1-CTA accepts all three rows, 2-CTA rejects the NVFP4 row because Blackwell
has no `mxf4nvf4` 2-CTA atom.

```c
uint64_t verify_block_scaled_mma(Type a, Type b, Type c,
                                 Type sfa, Type sfb,
                                 MmaAtomKind atom, Type dst,
                                 char two_cta) {
    bool is_fp4 = (a == &Float4E2M1FN) || (a == &FloatNV4E0M3F);
    bool is_fp8 = (a == &Float8E5M2)   || (a == &Float8E4M3FN);

    if (is_fp4 && (!sfa || !sfb)) {
        emit_diag(op, "fp4 mma should expect scaling factors");
        return 0;
    }
    if (sfa && sfb && sfa != sfb) {
        emit_diag(op, "sfa and sfb element type mismatch");
        return 0;
    }
    if (c != &Float32) {
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
        return (uint64_t)32 << 32 | 32;          // 0x2000000020
    }
    if (is_fp4 && atom_k == 64 && vec_size == 16 && !two_cta) {
        return (uint64_t)64 << 32 | 16;          // 0x4000000010
    }
    if (is_fp4 && atom_k == 64 && vec_size == 32 && !two_cta) {
        return (uint64_t)64 << 32 | 32;          // 0x4000000020
    }

    emit_diag(op, "unsupported block-scaled mma configuration");
    return 0;
}
```

The exact pointer-singleton dispatch, the ordering of the five phases, and the packed return encoding belong to the
reimplementation contract. The op builder at `sub_14B28C0` reads the low 32 bits as `vecSize` and the high 32 bits
as `atom_K`, then masks the result with `~7` before writing it into the op's atom attribute — any other return
encoding silently corrupts the op.

A correct reimplementation therefore enforces:

- Phase order is presence, agreement, accumulator, K-extent, catalog. Reordering catalog before accumulator changes which
  diagnostic the user sees when both are wrong, and breaks downstream test expectations.
- The FP4 element-type slot is shared. Disambiguation is by `(atom_K, vecSize)` and the 2-CTA selector, never by element
  identity alone.
- Helpers `sub_14B7090` and `sub_14B6F30` are reused; both increment the op's error counter and return a uniform
  `LogicalResult::failure()` so the verifier driver does not double-report.
- The packed return uses `atom_K` in the high word and `vecSize` in the low word, both as 32-bit unsigned values.
- Zero is reserved for failure. Legal shapes always have at least the `vecSize` field set.

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

