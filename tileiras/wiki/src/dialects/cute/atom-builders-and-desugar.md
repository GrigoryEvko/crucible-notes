# cute Atom Builders and Desugar

## Abstract

`cute` uses atoms to stand in for hardware-sized copy, prefetch, and MMA instructions before any target-specific lowering runs. High-level builders — `make_atom`, `make_tiled_copy`, `make_tiled_mma` — construct typed atom values. `CuteDesugar` expands syntactic sugar into primitive `cute`, `arith`, `scf`, `memref`, and LLVM-compatible operations. The final `cute`-to-LLVM conversion strips out the remaining target-neutral layout helpers. The contract that ties the three stages together is the result-type interface: the desugar pass and the LLVM conversion both ask the result type what operands and attributes to produce.

## Atom Builder Contract

`cute.make_atom` is generic. The result type decides whether the atom is an MMA, copy, prefetch, or other atom-like value; the builder queries the result-type interface rather than guessing from operand count. The dispatcher reads a `TypeID` slot off the result type, looks up the matching atom interface, and forwards the operand and attribute bundles to the per-atom builder.

The atom call ops — `cute.copy_atom_call` and `cute.mma_atom_call` — wrap the underlying atom value with the operand layouts the call site supplies. Verification covers layout rank, operand rank, and atom-instance compatibility against the target; the selected atom type carries the SM-specific rules, so the generic `cute` dialect never branches on SM tier directly.

## Per-Atom Desugar Rewrites

`CuteDesugar` rewrites convenience syntax into smaller primitives. It does not select SM-specific instructions — that is the next pass. Its job is to make layout, coordinate, view, and atom construction explicit enough for ordinary conversion patterns to handle, in three ordered phases per op family: **shape-eval**, **stride-eval**, and **composed-layout construction**.

### `make_layout` → `make_int_tuple` + `make_layout_raw`

`cute.make_layout` consumes a shape and a stride and produces a `!cute.layout`. The desugar pass rewrites it into the tuple-construction primitives so later passes can fold equal shapes and equal strides without re-parsing the sugar.

```mlir
// Before
%L = cute.make_layout(shape = (M, N, K), stride = (s_M, s_N, s_K))
   : !cute.layout

// After
%shape  = cute.make_int_tuple %M, %N, %K : !cute.int_tuple
%stride = cute.make_int_tuple %s_M, %s_N, %s_K : !cute.int_tuple
%L      = cute.make_layout_raw %shape, %stride : !cute.layout
```

The rewrite preserves operand order. `make_int_tuple` is the shared constructor for compile-time integer tuples; canonicalisation later compares equal-valued tuples structurally so two layouts built from the same shape produce identical SSA values.

### `make_shape` and `make_stride` → tuple construction

These two are pure shape-eval and stride-eval respectively.

```mlir
// Before
%S = cute.make_shape (M, N) : !cute.shape

// After
%S = cute.make_int_tuple %M, %N : !cute.shape
```

The result type narrows from `!cute.int_tuple` to `!cute.shape` (or `!cute.stride`) through a kind-discriminator field on the tuple type. The desugar pass writes the kind directly on the constructor; no separate cast is inserted.

### `make_coord` and `make_tile` → tuple construction + kind tag

Both rewrite the same way, distinguished only by the trailing kind tag.

```mlir
%C = cute.make_int_tuple %i, %j, %k : !cute.coord
%T = cute.make_int_tuple %M, %N, %K : !cute.tile
```

### `make_composed_layout` → outer + inner + offset

`cute.make_composed_layout` is the only sugar that emits more than one primitive. It builds the inner layout (typically a swizzle), the outer layout (the value layout the swizzle composes with), and an integer-tuple offset.

```mlir
// Before
%C = cute.make_composed_layout(outer = %outer,
                               swizzle = swizzle<B, M, S>,
                               offset = (0, 0))
   : !cute.layout

// After
%inner  = cute.make_swizzle B, M, S : !cute.swizzle
%offset = cute.make_int_tuple %c0, %c0 : !cute.int_tuple
%C      = cute.make_composed_layout_raw %outer, %inner, %offset : !cute.layout
```

The rewrite preserves the upstream CUTLASS rule that the inner component is the address-bit permutation and the outer component is the value-layout — flipping their order produces a different layout and the verifier catches it as a `compose_layout` failure.

### View construction → layout extraction + `make_view`

A `cute.make_tensor` or `cute.tensor` form expands into three primitives: take the pointer/memref, take the layout, and reassemble through `make_view`.

```mlir
%layout = cute.get_layout %tensor : !cute.layout
%iter   = cute.get_iter   %tensor : !cute.iter
%v      = cute.make_view %iter, %layout : !cute.view
```

### `equal` over views or layouts → shape equality + stride equality + `andi`

The sugar form `cute.equal` lifts a logical equality test over composite types. Desugaring expands it into the per-field equalities the IR can fold.

```mlir
%s1 = cute.get_shape %L1 : !cute.shape
%s2 = cute.get_shape %L2 : !cute.shape
%t1 = cute.get_stride %L1 : !cute.stride
%t2 = cute.get_stride %L2 : !cute.stride
%seq = cute.tuple_eq %s1, %s2 : i1
%teq = cute.tuple_eq %t1, %t2 : i1
%r   = arith.andi %seq, %teq : i1
```

### `make_atom` with atom interface → result-type-driven rebuild

Atom sugar is rebuilt through the result type's atom interface. The desugar pass walks the result type's `TypeID`, dispatches to the per-atom builder, and replaces the sugar op with the atom-specific construction shape. For MMA atoms the rebuild expands into an atom value plus an attribute bundle (`MMA shape`, `element types`, `accumulator type`); for copy atoms it expands into an atom value plus a copy-shape attribute. The actual instruction selection still belongs to the `cute_nvgpu` lowering pass that runs later.

### Dynamic `print` → coord loop

Dynamic print is the most involved desugaring because it builds an `scf.for` over the flattened coordinate domain. It is strictly a debugging transform — not a data-layout optimisation.

```c
void rewrite_dynamic_print(PrintOp op) {
    Shape shape = infer_runtime_shape(op.value);
    int64_t total = product(shape);

    scf_for(0, total, 1, [&](Value flat_index) {
        Coord coord = flat_to_coord(flat_index, shape);
        Value element = cute_memref_load(op.value, coord);
        emit_scalar_print(op.format, coord, element);
    });

    erase(op);
}
```

### Rewrite Order

The order matters for two reasons. Shape-eval runs first because every later step reads the shape tuple to drive its own rewrite — composed-layout construction needs the shape to size the offset tuple, and view construction needs the shape to validate the layout against the iter. Stride-eval runs second because composed-layout construction needs both shape and stride to call `make_composed_layout_raw`. Composed-layout construction runs last because all earlier ops have already been replaced with their primitive forms and the layout constructor sees only stable SSA values for its operands.

```c
void run_cute_desugar(Module module) {
    for (Operation op : module.walk()) {
        // Phase 1: shape-eval sugar.
        if (is_make_shape_sugar(op))          rewrite_make_shape(op);
        else if (is_make_coord_sugar(op))     rewrite_make_coord(op);
        else if (is_make_tile_sugar(op))      rewrite_make_tile(op);
    }
    for (Operation op : module.walk()) {
        // Phase 2: stride-eval sugar.
        if (is_make_stride_sugar(op))         rewrite_make_stride(op);
    }
    for (Operation op : module.walk()) {
        // Phase 3: layout / view / atom construction.
        if (is_make_layout_sugar(op))         rewrite_make_layout(op);
        else if (is_make_composed_sugar(op))  rewrite_make_composed_layout(op);
        else if (is_view_sugar(op))           rewrite_view_construction(op);
        else if (is_equal_sugar(op))          rewrite_equal(op);
        else if (is_dynamic_print(op))        rewrite_dynamic_print(op);
        else if (is_atom_builder(op))         rewrite_atom_builder(op);
    }
}
```

## Target-Neutral LLVM Conversion

Once desugaring is done, target-neutral `cute` helpers lower into stock MLIR and LLVM ops. The conversion covers tuple construction, layout field access, integer tuple arithmetic, descriptor iterators, pointer casts, pointer loads and stores, and descriptor dereferencing. SM-specific copies, MMA atoms, TMA, and WGMMA stay in `cute_nvgpu` and later target passes — they do not belong here.

```c
void populate_cute_to_llvm_patterns(PatternSet *patterns) {
    add(patterns, lower_make_int_tuple);
    add(patterns, lower_make_shape);
    add(patterns, lower_make_layout);
    add(patterns, lower_get_shape);
    add(patterns, lower_get_stride);
    add(patterns, lower_tuple_arithmetic);
    add(patterns, lower_descriptor_iterator);
    add(patterns, lower_pointer_casts);
    add(patterns, lower_pointer_load_store);
}
```

The descriptor-iterator lowering materializes an LLVM struct carrying base pointer, shape, stride, swizzle metadata, and rank. Model this as a typed descriptor object — never as a bag of unrelated scalars threaded through the pipeline.

```c
DescriptorIterator lower_make_desc_iter(MakeDescIterOp op) {
    DescriptorIterator desc;
    desc.base = op.base_pointer;
    desc.shape = materialize_shape(op.layout);
    desc.stride = materialize_stride(op.layout);
    desc.swizzle = encode_swizzle(op.layout);
    desc.rank = rank(op.layout);
    return desc;
}
```

## `make_int_tuple` Hub

`cute.make_int_tuple` is the shared constructor for compile-time integer tuples. Most layout operations reach for it whenever they need a static rank, shape, permutation, coordinate, or mode list.

```c
Value make_int_tuple(OpBuilder *builder, ArrayRef<int64_t> values) {
    Type type = infer_int_tuple_type(values.length);
    SmallVector<Value> constants;

    for (int64_t value : values) {
        constants.push(builder->create_index_constant(value));
    }

    return builder->create("cute.make_int_tuple", type, constants);
}
```

Desugaring canonicalizes equivalent static tuples so later layout folds can compare them structurally.

## Error Handling

A builder failure caused by a missing dialect or missing operation is a fatal compiler configuration error. A verification failure for illegal operands, layouts, or atom instances is a normal MLIR diagnostic. Keeping the two classes separate keeps frontend mistakes debuggable and stops broken pass registration from hiding behind them.

## Invariants

- Atom kind is determined by result type interfaces.
- Atom call verification checks both structural layout compatibility and
  target-specific atom legality.
- Desugar expands syntax but does not choose SM-specific instructions.
- Descriptor iterators lower to typed aggregate state.
- Static integer tuples are canonical intermediate values.
- Missing operation registration is a compiler setup bug, not a recoverable
  rewrite miss.

## Kernel-entry ABI

The `CuteKernelToNvvmRewrite` pass runs downstream of `MaterializeConvertLayout`, after the type converter has produced LLVM-legal function arguments. Each kernel function gets two related rewrites: a kernel-attribute rename so NVPTX codegen recognises the entry, and a per-argument lift of each grid-constant arg-attr into the LLVM-dialect triple the backend emits as a PTX `.param` constant-space descriptor.

The first rewrite is the `cute.kernel`-to-`nvvm.kernel` rename. Kernel functions enter the pass tagged with a `cute.kernel` UnitAttr left over from the front end; the rewrite drops it and writes `nvvm.kernel` in its place. NVPTX codegen recognises kernel entries by `nvvm.kernel`, so after this rewrite the function is visible to the downstream NVVM lowering as a real kernel entry rather than a plain device function. Nothing about the function body changes — only the function-level attribute.

The second rewrite walks every function argument carrying the `cute_nvgpu.grid_constant` arg-attribute. For each such argument it deletes the `cute_nvgpu.grid_constant` arg-attr and installs the LLVM-dialect triple `{llvm.align = 16, llvm.byval, nvvm.grid_constant}`. Each component of the triple has a specific job in the final ABI:

| Attribute | Role at the kernel boundary |
|---|---|
| `llvm.align = 16` | matches the TMA descriptor's 16-byte alignment requirement; Hopper TMA hardware refuses unaligned descriptors. |
| `llvm.byval` | tells the LLVM backend to pass the descriptor by value, in `.param` space, rather than as a pointer to host memory. |
| `nvvm.grid_constant` | persists through NVVM lowering to the final PTX as constant-space placement on the kernel parameter. |

Ordering matters. The pass must run after the type converter has produced LLVM-legal function arguments — `llvm.byval` is only meaningful on an LLVM-dialect aggregate type and would attach to a non-LLVM type if the rewrite ran earlier. It must also run after MaterializeConvertLayout has finalised the descriptor argument types, because the alignment requirement is keyed off the descriptor's concrete layout. Encode both ordering constraints in the pass-manager pipeline rather than relying on the textual order of pass registration.

Together the two rewrites make a kernel function self-describing to the NVVM backend. The function-level attribute tells the backend "this is a kernel entry, emit `.entry`"; the per-argument triple tells the backend "place this descriptor in `.param` constant space, 16-byte aligned, by value". After this pass the kernel is ready for plain NVVM-to-PTX translation, and no later pass touches the kernel-entry ABI.

## Cross-References

[Verifiers — Verbatim Diagnostics](verifiers.md#verbatim-diagnostics) lists every verbatim diagnostic the verifier surface emits, including the atom-call diagnostics that fire on the desugared forms above. [Layout Algebra and Descriptor Grammar — Layout Primitives](layout-algebra-and-descriptor-grammar.md#layout-primitives) covers the layout primitives the shape-eval and stride-eval phases produce. [SM Tier Roster and Copy Atom Registry — Atom TypeID Registry](../cute_nvgpu/sm-tier-roster-and-copy-atom-registry.md#atom-typeid-registry) documents the atom interfaces the result-type-driven atom rebuild dispatches against.

