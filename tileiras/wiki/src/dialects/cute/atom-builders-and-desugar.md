# cute Atom Builders and Desugar

## Abstract

`cute` uses atoms to stand in for hardware-sized copy, prefetch, and MMA instructions before any target-specific lowering runs. High-level builders — `make_atom`, `make_tiled_copy`, `make_tiled_mma` — construct typed atom values. `CuteDesugar` expands syntactic sugar into primitive `cute`, `arith`, `scf`, `memref`, and LLVM-compatible operations. The final `cute`-to-LLVM conversion strips out the remaining target-neutral layout helpers. This page describes the pipeline as a contract.

## Atom Builder Contract

`cute.make_atom` is generic. The result type decides whether the atom is an MMA, copy, prefetch, or other atom-like value; the builder queries the result-type interface rather than guessing from operand count.

```c
AtomValue make_atom(Type result_type, ArrayRef<Value> operands, AttrDict attrs) {
    AtomInterface iface = dyn_cast_atom_interface(result_type);
    require(iface.valid);

    iface.verify_builder_operands(operands, attrs);
    return create_atom_value(result_type, operands, attrs);
}
```

`cute.copy_atom_call` and `cute.mma_atom_call` are the call-site forms. Each takes an atom value and executes one logical hardware operation. Structural verification covers layout rank, operand rank, and atom-instance compatibility; the selected atom type carries the target-specific rules.

```c
LogicalResult verify_copy_atom_call(CopyAtomCallOp op, Target target) {
    require(op.atom.type.implements_copy_atom());
    require(layouts_match_copy_operands(op.atom, op.src, op.dst));
    return op.atom.type.verify_copy_instance(op.src.shape, op.dst.shape, target);
}
```

## Desugar Pass

`CuteDesugar` rewrites convenience syntax into smaller primitives. It does not select SM-specific instructions. Its job is to make layout, coordinate, print, and atom construction explicit enough for ordinary conversion patterns to handle.

| Input sugar | Desugared shape |
|---|---|
| `make_layout`, `make_shape`, `make_stride`, `make_coord`, `make_tile` | Tuple builders, static integer tuples, shape/stride operations. |
| View construction | Explicit layout extraction, iterator extraction, and `make_view`. |
| `equal` over views or layouts | Shape equality, stride equality, and boolean conjunction. |
| Dynamic `print` | Loops over coordinates, loads values, and emits scalar print calls. |
| `make_atom` with atom interface | Rebuilds the atom through the selected result-type interface. |

```c
void run_cute_desugar(Module module) {
    for (Operation op : module.walk()) {
        if (is_make_layout_sugar(op)) {
            rewrite_make_layout(op);
        } else if (is_make_shape_sugar(op)) {
            rewrite_make_shape(op);
        } else if (is_view_sugar(op)) {
            rewrite_view_construction(op);
        } else if (is_dynamic_print(op)) {
            rewrite_dynamic_print(op);
        } else if (is_atom_builder(op)) {
            rewrite_atom_builder(op);
        }
    }
}
```

Dynamic print is the most involved desugaring. It builds a loop over the flattened coordinate domain, turns loop indices back into coordinates, loads the element, and prints a formatted line. Strictly a debugging transform — not a data-layout optimization.

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

`sub_1698C20` is the body of the `CuteKernelToNvvmRewrite` pass. It runs downstream of the D08 MaterializeConvertLayout pass, after the type converter has produced LLVM-legal function arguments. Each kernel function gets two related rewrites: a kernel-attribute rename so NVPTX codegen recognises the entry, and a per-argument lift of each grid-constant arg-attr into the LLVM-dialect triple the backend emits as a PTX `.param` constant-space descriptor.

The first rewrite is the `cute.kernel`-to-`nvvm.kernel` rename. Kernel functions enter the pass tagged with a `cute.kernel` UnitAttr left over from the front end; the rewrite drops it and writes `nvvm.kernel` in its place. NVPTX codegen recognises kernel entries by `nvvm.kernel`, so after this rewrite the function is visible to the downstream NVVM lowering as a real kernel entry rather than a plain device function. Nothing about the function body changes — only the function-level attribute.

The second rewrite walks every function argument carrying the `cute_nvgpu.grid_constant` arg-attribute. For each such argument it deletes the `cute_nvgpu.grid_constant` arg-attr and installs the LLVM-dialect triple `{llvm.align = 16, llvm.byval, nvvm.grid_constant}`. Each component of the triple has a specific job in the final ABI:

| Attribute | Role at the kernel boundary |
|---|---|
| `llvm.align = 16` | matches the TMA descriptor's 16-byte alignment requirement; Hopper TMA hardware refuses unaligned descriptors. |
| `llvm.byval` | tells the LLVM backend to pass the descriptor by value, in `.param` space, rather than as a pointer to host memory. |
| `nvvm.grid_constant` | persists through NVVM lowering to the final PTX as constant-space placement on the kernel parameter. |

Ordering matters. The pass must run after the type converter has produced LLVM-legal function arguments — `llvm.byval` is only meaningful on an LLVM-dialect aggregate type and would attach to a non-LLVM type if the rewrite ran earlier. It must also run after MaterializeConvertLayout has finalised the descriptor argument types, because the alignment requirement is keyed off the descriptor's concrete layout. Encode both ordering constraints in the pass-manager pipeline rather than relying on the textual order of pass registration.

Together the two rewrites make a kernel function self-describing to the NVVM backend. The function-level attribute tells the backend "this is a kernel entry, emit `.entry`"; the per-argument triple tells the backend "place this descriptor in `.param` constant space, 16-byte aligned, by value". After this pass the kernel is ready for plain NVVM-to-PTX translation, and no later pass touches the kernel-entry ABI.

