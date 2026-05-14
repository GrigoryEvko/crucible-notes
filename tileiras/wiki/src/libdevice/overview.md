# libdevice Overview

`libdevice` is the NVIDIA device math library used to implement calls such as `__nv_sin`, `__nv_exp`, `__nv_pow`, and their float or double variants. TileIR lowering can emit calls to these functions when a GPU math operation is better represented as a library call than as a single intrinsic. Before NVPTX code generation, those declarations must resolve to device-side LLVM bitcode bodies.

The compiler handles libdevice as a correctness sequence: link the library bitcode, resolve `__nvvm_reflect` configuration queries, inline the selected math bodies into kernels, simplify dead configuration branches, and then let normal LLVM optimization clean up the result. This must work even at low optimization levels because unresolved `__nv_*` declarations cannot be emitted as PTX.

## Pipeline

```text
LLVM module with calls to __nv_* declarations
    |
    | link embedded or supplied libdevice bitcode
    v
LLVM module with __nv_* definitions
    |
    | fold __nvvm_reflect("KEY") queries
    v
configuration-specialized libdevice bodies
    |
    | always-inline libdevice calls into kernels
    v
kernel bodies containing selected math implementations
    |
    | simplify branches, fold constants, remove unused library functions
    v
LLVM module ready for NVPTX code generation
```

The effective order matters. Libdevice bodies contain reflection queries, so reflection folding must see the linked bodies. Inlining should run after reflection so dead configuration arms are already easy to remove. Constant folding and global dead-code elimination then remove unused paths and unused library definitions.

## Reflection

`__nvvm_reflect` is a compile-time query mechanism. Libdevice bodies call it with string keys that describe target or mode choices, such as flush-to-zero behavior. The reflect pass replaces those calls with integer constants drawn from module metadata, module flags, command-line overrides, or target defaults.

```c
void fold_nvvm_reflect(Module module, ReflectConfig config) {
    for (CallInst *call : calls_named(module, "__nvvm_reflect")) {
        StringRef key = require_constant_string_argument(call, 0);
        int value = lookup_reflect_value(config, key);
        replace_all_uses_with_constant_i32(call, value);
        erase(call);
    }
}
```

A reimplementation should treat unknown keys carefully. The safe behavior is to use the same default table as the target runtime or to issue a clear diagnostic. Silently replacing an unknown key with zero can select the wrong libdevice implementation.

## Linking and Inlining

Libdevice linking is not an ordinary optimization pass. It is a module construction step that merges bitcode definitions into the user module. Once linked, each selected `__nv_*` body should be available to the inliner. NVIDIA libdevice functions are designed to be inlined into kernels; leaving calls behind risks unresolved symbols, missed target specialization, or ABI mismatches.

```c
void prepare_libdevice(Module module, LibdeviceBitcode libdevice, ReflectConfig config) {
    link_module(module, parse_bitcode(libdevice));
    fold_nvvm_reflect(module, config);

    run_always_inliner(module);
    simplify_constant_conditionals(module);
    run_standard_cleanup(module);

    require(no_unresolved_libdevice_declarations(module));
}
```

At higher optimization levels, normal inlining, instruction combining, SCCP, GVN, and global DCE improve the result further. At lower levels, the always-inline path still needs to run because libdevice resolution is required for correctness.

## Constant Folding

After linking and reflection, many libdevice call paths become compile-time constants or simple arithmetic. Constant folding can evaluate calls with constant operands, collapse dead `if` branches selected by reflection, and remove library helpers that no longer have users. This is especially important for math functions that contain multiple approximation paths behind target-mode checks.

The point is not to prove every math call at compile time. The point is to specialize the library to the selected target and remove impossible branches before the backend sees them.

## Cross-links

- [NVVM Reflect Mechanism](nvvm-reflect-mechanism.md) covers reflection keys and replacement behavior.
- [Intrinsic ID Switch](intrinsic-id-switch.md) covers constant folding and intrinsic dispatch.
- [Math Pass Pipeline](math-pass-pipeline.md) covers the surrounding LLVM math optimization flow.
