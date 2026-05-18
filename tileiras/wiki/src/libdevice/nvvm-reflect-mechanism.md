# NVVMReflect Mechanism

## Abstract

The `tileiras` binary hosts a complete copy of the LLVM `NVVMReflectPass` machinery — the device-side mechanism that resolves the `__nvvm_reflect("KEY")` intrinsic into a compile-time integer so that the libdevice bitcode bodies can specialise themselves on per-module decisions (FTZ mode, target SM, IEEE divide/sqrt precision, etc.) without runtime branches. The pass takes an LLVM module immediately after the libdevice bitcode has been linked in, walks every call to `__nvvm_reflect` / `__nvvm_reflect_ocl` plus five mangled variants, looks the key string up in a `DenseMap<StringRef,int>` populated from three orthogonal sources, replaces the call with a `ConstantInt::get(callType, value, /*IsSigned=*/false)`, and erases the now-useless declarations. Missing keys default to 0 and are silently inserted into the map so a single key is folded consistently across every call site that names it.

This page covers the end-to-end registration, CLI surface, var-map population, replacement loop, and post-reflect constant-conditional cleanup pass. The legacy pass-manager entry is fail-fast, so the reachable path is the new pass manager.

## New-PM registration

`NVVMReflectPass` is registered as an NVPTX-specific new-PM pass under the CLI key `nvvm-reflect`. It lives alongside the target pass family: `generic-to-nvvm`, `nvptx-lower-ctor-dtor`, `nvptx-set-global-array-alignment`, `register-pressure-analysis`, `nvptx-aa`, `nvvm-intr-range`, `lower-struct-args`, `nvptx-lower-args`, and related NVPTX preparation passes. The split between the NVPTX-specific registry and the generic LLVM pass registry mirrors upstream LLVM's source-tree split.

## CLI surface

The pass exposes two options and one alias:

| CLI argument | Type | Help | Default |
|---|---|---|---|
| `nvvm-reflect-enable` | `cl::opt<bool>` | "NVVM reflection, enabled by default" | `true` |
| `nvvm-reflect-add` | `cl::list<std::string>` | "A key=value pair. Replace `__nvvm_reflect(name)` with value." | empty list |
| `R` | `cl::alias` to `nvvm-reflect-add` | inherited | — |

`nvvm-reflect-add` accepts entries of the form `name=<int>`. The alias follows normal LLVM `cl::alias` validation rules.

## Runtime pipeline

`NVVMReflect::runOnModule` fires once per module. It first builds the reflection map, then rewrites calls to `__nvvm_reflect`, `__nvvm_reflect_ocl`, and five ABI-mangled variants used by libdevice and C++ frontend paths. The pass reports that it changed the module if any call site was replaced.

## Three var-map sources

`populateVarMap` merges three sources into one reflection map. The important rule is ordering:
named metadata is read first, the FTZ module flag is read second, and command-line overrides are
read last. A later source overwrites an earlier value for the same key, so `-nvvm-reflect-add` is
the user-visible escape hatch for testing or forcing a libdevice configuration.

| Order | Source | Key extraction | Value extraction |
|---|---|---|---|
| A | `!nvvm.reflection` named metadata | operand 0 as an `MDString` | operand 1 as a signed integer constant |
| B | module flag `nvvm-reflect-ftz` | remapped to `__CUDA_FTZ` | same signed integer normalization |
| C | `-nvvm-reflect-add name=value` / `-R name=value` | substring before `=` | decimal integer parsed after `=` |

Metadata and module-flag values are treated as signed integers. Narrow integer constants are
sign-extended before insertion; ordinary 32-bit reflect values therefore behave like plain C
`int` values. CLI values are decimal only. Malformed CLI entries are reported as option errors,
not compiler crashes:

- empty key before `=`
- missing value after `=`
- non-integer value after `=`

```c
static int64_t normalize_reflect_int(const ConstantInt *value) {
    unsigned bits = constant_int_width(value);
    uint64_t raw = constant_int_zext(value);

    if (bits < 64) {
        unsigned shift = 64 - bits;
        return ((int64_t)(raw << shift)) >> shift;
    }

    return (int64_t)raw;
}

static void populate_reflect_map(Module *module, const ReflectOptions *options, ReflectMap *values) {
    for (MetadataEntry entry : nvvm_reflection_metadata(module)) {
        reflect_map_set(values, metadata_key(entry), normalize_reflect_int(metadata_value(entry)));
    }

    if (ConstantInt *ftz = module_flag_int(module, "nvvm-reflect-ftz")) {
        reflect_map_set(values, "__CUDA_FTZ", normalize_reflect_int(ftz));
    }

    for (StringRef option : options->reflect_add) {
        ParsedReflectOption parsed = parse_reflect_add(option);
        reflect_map_set(values, parsed.name, parsed.value);
    }
}
```

## FTZ module flag

`__CUDA_FTZ` is the only reflect key with a dedicated module-flag path. The compiler reads the
module flag named `nvvm-reflect-ftz`, normalizes its integer value, and stores it under the
libdevice key `__CUDA_FTZ`. A later `-nvvm-reflect-add __CUDA_FTZ=<int>` still overrides it.

The precision keys, such as `__CUDA_PREC_DIV` and `__CUDA_PREC_SQRT`, do not have equivalent
module-flag shortcuts. They enter the map through `!nvvm.reflection` or through explicit CLI
overrides.

Missing keys are deliberately benign. If a call names a key that is absent from every source, the
lookup path creates a zero-valued entry and folds the call to integer zero. That makes unsupported
or future libdevice probes deterministic instead of fatal.

## Replacement loop

The rewriter runs once for each accepted reflect function spelling: `__nvvm_reflect`,
`__nvvm_reflect_ocl`, and five ABI-mangled forms observed in C++ libdevice paths. For each
function, it walks every use and requires a direct call with exactly one argument. The argument
must reduce to a constant, null-terminated string after stripping pointer casts and the simple
constant-expression forms produced for global string literals.

Malformed calls are fatal IR errors. The public diagnostics are intentionally specific:

- `__nvvm_reflect can only be used in a call instruction`
- `__nvvm_reflect requires exactly one argument`
- `__nvvm_reflect argument must be a constant string`
- `__nvvm_reflect argument must be a string constant`
- `__nvvm_reflect argument must be a null-terminated string`
- `__nvvm_reflect argument cannot be empty`

For a valid call, the pass reads the key, looks up the integer value with default zero, creates a
constant of the call's result type, replaces every use of the call with that constant, erases the
call, and removes the now-unused declaration.

```c
static StringRef read_reflect_key(Value *arg) {
    Value *base = strip_pointer_casts(arg);

    if (ConstantExpr *expr = dyn_cast_constant_expr(base)) {
        base = peel_global_string_gep(expr);
    }

    ConstantDataSequential *data = dyn_cast_constant_data(base);
    if (data == NULL) {
        fatal("__nvvm_reflect argument must be a string constant");
    }

    if (!constant_data_is_c_string(data)) {
        fatal("__nvvm_reflect argument must be a null-terminated string");
    }

    StringRef key = constant_data_as_c_string(data);
    if (key.empty()) {
        fatal("__nvvm_reflect argument cannot be empty");
    }

    return key;
}

static bool replace_reflect_calls(Module *module, StringRef name, ReflectMap *values) {
    Function *function = module_get_function(module, name);
    if (function == NULL) {
        return false;
    }

    bool changed = false;
    SmallVector<CallInst *, 16> calls = collect_reflect_calls(function);

    for (CallInst *call : calls) {
        StringRef key = read_reflect_key(call_arg(call, 0));
        int64_t value = reflect_map_lookup_or_insert_zero(values, key);
        Constant *replacement = constant_int(call_type(call), value, false);

        replace_all_uses_with(call, replacement);
        erase_instruction(call);
        changed = true;
    }

    if (function_has_no_uses(function)) {
        erase_function(function);
    }

    return changed;
}
```

## Post-reflect cleanup — `nvvm-reflect-pp`

The reflect rewrite usually exposes branches whose conditions are now constants. A libdevice body
often has the shape "if `__nvvm_reflect(KEY)` equals N, use this implementation; otherwise use the
fallback." Once the call is replaced, those branches are no longer semantic choices; they are dead
IR structure.

`nvvm-reflect-pp` runs immediately after reflection as a small function pass. It folds constant
conditional branches, drops unreachable successors, and invalidates the affected control-flow
analyses. Scheduling the cleanup next to reflection keeps the rest of the optimization pipeline
from repeatedly rediscovering the same trivial facts, and it gives the NVPTX backend a smaller,
more predictable CFG even in low-optimization pipelines.

## Reimplementation Notes

A compatible implementation has to preserve three behavioral contracts:

- merge metadata, FTZ module flag, and CLI overrides in that exact order
- fold missing keys to zero, not to an error
- run constant-conditional cleanup directly after reflection

```c
bool run_nvvm_reflect(Module *module, const ReflectOptions *options) {
    if (!options->enable_reflect) {
        return false;
    }

    ReflectMap values = reflect_map_create();
    populate_reflect_map(module, options, &values);

    bool changed = false;
    for (StringRef name : reflect_function_names()) {
        changed |= replace_reflect_calls(module, name, &values);
    }

    if (changed) {
        simplify_constant_conditionals(module);
    }

    reflect_map_destroy(&values);
    return changed;
}
```

## Cross-references

The end-to-end libdevice integration that drives `NVVMReflectPass` is documented in [libdevice Overview — Pipeline](overview.md#pipeline) and [libdevice Overview — Link, inline, simplify](overview.md#link-inline-simplify). The constant-folding consumer that sees reflect-stripped libdevice bodies is [Intrinsic ID Switch and Name Table](intrinsic-id-switch-and-name-table.md). The downstream math lowering whose `__CUDA_PREC_*` / `__CUDA_FTZ` arms collapse after reflection is documented in [Math Pass Pipeline and Crosswalk — Cases that skip libdevice entirely](math-pass-pipeline-and-crosswalk.md#cases-that-skip-libdevice-entirely). The user-facing precision model that composes reflect-driven libdevice gating with per-op fast-math flags, FTZ, and FP8 cast semantics is documented in [Fast-Math and Numerical Precision](../topics/fast-math-and-numerical-precision.md).
