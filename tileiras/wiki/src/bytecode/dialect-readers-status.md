# Dialect Bytecode Reader/Writer Status

## Abstract

`tileiras` consumes TileIR bytecode in one direction only. It accepts
serialized `cuda_tile` modules at the driver boundary, lowers them through
several internal dialects, and emits PTX or object code — none of those
dialects ever round-trip back to MLIR bytecode.

The compatibility rule is simple: `cuda_tile` is the only TileIR dialect with
a linked bytecode reader, and no TileIR dialect in this binary has a linked
bytecode writer. Downstream dialects — `nv_tileaa`, `nv_tileas`, `cute`,
`cute_nvgpu`, `cutlass` — are in-memory pipeline representations.

**Reader-side encoder/decoder pairing.** Every wire-format dispatcher in the
binary is decoder-only — the matching writer is absent. The reader recognizes
four tag families: the OpTag (`sub_5B13D0`, 110 cases), the AttrTag
(`sub_59F100`, 13 cases, wire-format-breaking vs upstream MLIR), the DebugTag
(`sub_589B90`, 7 cases), and the TypeTag (`sub_59C710`, 18 cases). All four
hang off the top-level bytecode walker `sub_57FF40`; none has a sibling writer
dispatcher linked in. A reimplementation that wants to produce TileIR bytecode
must build its own encoder against the exact tag numberings documented in
[MLIR Bytecode Format](mlir-bc-format.md) — the shipped reader is the only
source of truth for the wire-format constants.

## Status Matrix

| Dialect | Bytecode reader | Bytecode writer | Public meaning |
| --- | --- | --- | --- |
| `cuda_tile` | Present | Absent | Input wire format accepted by the driver. |
| `nv_tileaa` | Absent | Absent | Produced by lowering from `cuda_tile`; not loadable from bytecode. |
| `nv_tileas` | Absent | Absent | Produced by TileAA-to-TileAS conversion; not loadable from bytecode. |
| `cute` | Absent | Absent | Persisted through textual asm only when dumped. |
| `cute_nvgpu` | Absent | Absent | Persisted through textual asm only when dumped. |
| `cutlass` | Absent | Absent | Frontend scheduling dialect inside the pipeline, not a bytecode format. |

Upstream MLIR `builtin` bytecode support is still linked in because the file
container uses MLIR infrastructure for built-in types and attributes. That
does not mean the TileIR dialects themselves provide general MLIR bytecode
round-tripping.

## Reader Contract

The `cuda_tile` reader covers the input module boundary:

1. Validate the TileIR magic and version.
2. Read String, Func, Debug, Constant, Type, and Global sections.
3. Decode `cuda_tile` type tags and self-contained attribute payloads.
4. Decode debug attributes and function/global records.
5. Decode operation records from the `cuda_tile` public opcode space.
6. Materialize a `builtin.module` in an MLIR context with `cuda_tile` loaded.

```c
ModuleOp read_tileir_module(ByteSpan bytes, MLIRContext *ctx) {
    TileIRFile file = scan_tileir_sections(bytes);
    if (!file.valid)
        return NULL;

    StringTable strings = read_string_section(file.string_section);
    TypeTable types = read_type_section(file.type_section, ctx);
    ConstantTable constants = read_constant_section(file.constant_section, ctx);
    DebugTable debug = read_debug_section(file.debug_section, ctx);

    ModuleOp module = create_builtin_module(ctx);
    read_globals(file.global_section, module, strings, types, constants);
    read_functions(file.func_section, module, strings, types, constants, debug);
    return module;
}
```

Non-`cuda_tile` TileIR bytecode is rejected at the driver boundary. When the
input looks like ordinary upstream MLIR bytecode, the driver reports that
shape explicitly instead of silently reinterpreting it as TileIR.

## Non-Writer Contract

The missing bytecode writers are user-visible. A tool can use `tileiras` to
consume a `cuda_tile` bytecode module and produce compiled output, but it
cannot ask this binary to emit optimized TileIR bytecode or any intermediate
dialect bytecode.

```c
bool tileiras_can_read_bytecode(const char *dialect) {
    return strcmp(dialect, "cuda_tile") == 0;
}

bool tileiras_can_write_bytecode(const char *dialect) {
    (void)dialect;
    return false;
}
```

Round-trip workflows must use textual IR dumps for inspection or link an
external writer against a compatible dialect implementation. Do not assume
the shipped assembler can serialize an intermediate `nv_tileaa`, `nv_tileas`,
`cute`, `cute_nvgpu`, or `cutlass` module.

## Practical Consequences

The asymmetry explains several driver behaviors:

1. The command-line input must be TileIR bytecode, not generic MLIR bytecode.
2. There is no `--emit-bytecode` or `--write-bytecode` mode in the driver.
3. Intermediate IR dumps, when enabled, are textual MLIR asm, not bytecode.
4. The internal dialect stack can evolve without defining stable wire formats
   for every intermediate representation.
5. A reimplementation that wants cacheable intermediate artifacts must design
   its own serialization boundary or reuse upstream MLIR textual/bytecode
   support with matching dialect interfaces.
