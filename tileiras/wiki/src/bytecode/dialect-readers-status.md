# Dialect Bytecode Reader/Writer Status

## Abstract

`tileiras` consumes TileIR bytecode in one direction only. It accepts serialized `cuda_tile` modules at the driver boundary, lowers them through several internal dialects, and emits PTX or object code — none of those dialects ever round-trip back to MLIR bytecode. The compatibility rule is simple: `cuda_tile` is the only TileIR dialect with a linked bytecode reader, and no TileIR dialect in this binary ships a bytecode writer. Downstream dialects — `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass` — are in-memory pipeline representations.

## Reader architecture

A single reader-driver walks the bytecode container in a fixed order: file envelope (magic, version, dialect-list), string section, type section, attribute section, IR section (functions and globals), resource section, and an optional debug section. The driver reads each section header, validates the recorded byte count against the available span, and hands the section body to a per-section dispatcher. Section dispatchers iterate the body, reading one record at a time and routing each record through a tag-keyed switch onto a typed handler.

The reader is decoder-only at every level. Four wire-format dispatchers carry the work — one for [operation opcodes](mlir-bc-format.md#operation-opcode-dispatch) (110 cases over the `cuda_tile` public opcode space), one for [self-contained attribute payloads](mlir-bc-format.md#self-contained-attribute-dispatch) (13 cases, wire-format-breaking versus upstream MLIR), one for [type tags](mlir-bc-format.md#type-tag-dispatch) (18 cases), and one for [debug attributes](mlir-bc-format.md#debug-info-attribute-dispatch) (7 cases). None has a sibling writer dispatcher linked in. A reimplementation that wants to produce TileIR bytecode must build its own encoder against the tag numberings documented in [MLIR Bytecode Format](mlir-bc-format.md); the shipped reader is the only source of truth for the wire-format constants, and the attribute-tag numbering deliberately diverges from upstream MLIR (tag `1` is `StringAttr`, tag `13` is `AssumePredicateAttr`, magic byte 7 is `0x00`).

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

## Reader contract

The `cuda_tile` reader is the only path that materializes IR at the driver boundary. Its top-level loop validates the envelope, scans the section table, then dispatches each section in container order — the later sections reference indices into the earlier ones, so reordering would break cross-section lookups.

```c
ModuleOp read_tileir_module(ByteSpan input, MLIRContext *ctx) {
    BytecodeReader r = bytecode_reader_init(input);

    /* envelope: 8-byte magic, LEB128 version, dialect-list, blob preamble */
    if (!read_and_verify_magic(&r))         { diag(r, "invalid TileIR magic");   return nullptr; }
    uint64_t version = read_leb128(&r);
    if (!version_is_supported(version))     { diag(r, "unsupported version");    return nullptr; }
    DialectList dialects = read_dialect_list(&r);  /* requires "cuda_tile" entry */

    /* sections appear in a fixed order; later sections index into earlier ones */
    SectionTable sections = scan_section_table(&r);

    StringTable    strings    = read_string_section   (&r, sections.string);
    TypeTable      types      = read_type_section     (&r, sections.type,      ctx, strings);
    AttributeTable attrs      = read_attribute_section(&r, sections.attribute, ctx, strings, types);
    ResourceTable  resources  = read_resource_section (&r, sections.resource,  ctx, strings);
    DebugTable     debug      = sections.debug.present
                                ? read_debug_section  (&r, sections.debug,     ctx, strings, attrs)
                                : empty_debug_table();

    ModuleOp module = create_builtin_module(ctx);
    read_ir_section(&r, sections.ir, module, strings, types, attrs, resources, debug);
    return module;
}
```

Each section dispatcher follows the same shape — read a small header (record count, dialect index, optional flags), then iterate record bodies, routing each record's lead byte through the appropriate tag switch:

```c
ParseResult read_attribute_section(BytecodeReader *r, SectionSpan span, ...) {
    bytecode_reader_seek(r, span.begin);
    uint64_t count = read_leb128(r);
    for (uint64_t i = 0; i < count; ++i) {
        uint8_t tag = read_byte(r);
        switch (tag) {
            case ATTR_STRING:           parse_string_attr(r, /*has_dialect=*/false); break;
            case ATTR_INTEGER:          parse_integer_attr(r); break;
            case ATTR_FLOAT:            parse_float_attr(r); break;
            case ATTR_DICT:             parse_dictionary_attr(r); break;
            case ATTR_DENSE_ELT:        parse_dense_elements_attr(r); break;
            case ATTR_DENSE_ELT_STRING: parse_dense_elements_string_attr(r); break;
            /* …7 more cases, each named after its attribute family… */
            case ATTR_ASSUME_PREDICATE: parse_assume_predicate_attr(r); break;
            default:                    parse_external_attr(r, tag);    break;
        }
    }
    return success();
}
```

Operation records inside the IR section follow the same dispatch shape with the 110-case opcode switch in place of the 13-case attribute switch. Type records use the 18-case type switch; debug records use the 7-case debug switch. The four switches are independent — they share no fallthrough — and each one terminates a section: when the section's byte count is exhausted, the reader returns to the driver loop.

Non-`cuda_tile` TileIR bytecode is rejected at the driver boundary. When the input looks like ordinary upstream MLIR bytecode (different magic byte 7, different attribute-tag numbering), the driver reports that shape explicitly instead of silently reinterpreting it as TileIR.

## Reader-only contract

The missing writers are user-visible. A tool can hand `tileiras` a `cuda_tile` bytecode module and ask for compiled output, but it cannot ask this binary to emit optimized TileIR bytecode or any intermediate-dialect bytecode. The capability surface is a one-line predicate:

```c
bool tileiras_can_read_bytecode (const char *dialect) { return strcmp(dialect, "cuda_tile") == 0; }
bool tileiras_can_write_bytecode(const char *dialect) { (void)dialect; return false; }
```

The asymmetry is deliberate. Round-trip workflows use textual IR dumps for inspection; cacheable intermediate artifacts require an external writer linked against compatible dialect implementations. Treating the intermediate dialects as encoder-absent on purpose lets the pipeline evolve without freezing a stable wire format for every internal representation — the only stable surface is the `cuda_tile` input boundary.

Several driver behaviors fall out of this asymmetry: the command-line input must be TileIR bytecode (not generic MLIR bytecode); the driver exposes no `--emit-bytecode` or `--write-bytecode` mode; intermediate IR dumps, when enabled, are textual MLIR asm rather than bytecode; the internal dialect stack can change shape between releases without breaking external tooling.

## Cross-references

The detailed wire format consumed by the reader-driver — file envelope, section ordering, every tag enumeration, the validation diagnostics, and the LLVM bitcode path used for NVVM modules — lives in [MLIR Bytecode Format](mlir-bc-format.md). The textual-asm side of the reader contract, including how intermediate dialects are inspected when bytecode serialization is unavailable, is covered in [Dialect Asm-Printer Status](asm-printer-status.md). The dialect-level semantics that the bytecode reader materializes are documented in the per-dialect references — [cuda_tile bytecode reference](../dialects/cuda_tile/bytecode.md) and [cuda_tile Overview](../dialects/cuda_tile/overview.md) for the input dialect, plus the corresponding overviews for [nv_tileaa](../dialects/nv_tileaa/overview.md), [nv_tileas](../dialects/nv_tileas/overview.md), [cute](../dialects/cute/overview.md), [cute_nvgpu](../dialects/cute_nvgpu/overview.md), and [cutlass](../dialects/cutlass/overview.md).
