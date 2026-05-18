# MLIR Bytecode Format

## Abstract

`tileiras` consumes a private TileIR bytecode container for `cuda_tile` modules. The format borrows MLIR bytecode's spirit — unsigned LEB128 integers, section-local offset tables, indexed cross-section references, self-contained attribute payloads — but it is not upstream MLIR bytecode. The magic, version block, section enum, type tags, attribute tags, and `cuda_tile` opcode space are all private to this binary.

**Wire-format divergence vs upstream MLIR.** The shipped AttrTag numbering inside `sub_59F100` breaks wire compatibility with upstream `mlir/Bytecode/BytecodeEnums.h::AttributeTag`. Tag `1` is `StringAttr` here (upstream `IntegerAttr=1`), tag `4` is `DenseElementsAttr` (upstream `TypeAttr=4`), tag `5` is `DenseElementsAttr<string>` (upstream `StringAttr=5`), and tag `13` is `AssumePredicateAttr` — a slot upstream does not define at all. Bytecode emitted by stock MLIR with stock numbering decodes wrong through this binary; bytecode emitted by this binary cannot be consumed by stock MLIR. Any external tool that needs to round-trip through both must freeze the tileiras tag assignments rather than the upstream header. The full 13-tag table sits below in [Self-Contained Attribute Dispatch](#self-contained-attribute-dispatch). At the envelope level, the partition signal is magic byte 7 — `0x00` here, `'\n'` upstream.

**Reader contract.** The bytecode reader is an in→out transform: a byte buffer goes in, a `builtin.module` containing a `cuda_tile` body comes out. Failure paths return `nullptr`/`false` with one of the verbatim diagnostics enumerated in the dispatcher sections below.

```c
ModuleOp readTileIRBytecode(ByteSpan input, MLIRContext *ctx) {
    if (!sub_5838A0_validate_header(input))                 // magic + version + dialect-list + blob preamble
        return nullptr;                                     // see "Header Parser (sub_5838A0)" below
    SectionSpans spans = scan_section_table(input);
    StringTable   strings   = read_string_section(spans.string);
    TypeTable     types     = read_type_section(spans.type, ctx);
    ConstantTable constants = read_constant_section(spans.constant, ctx);  // routes payloads through sub_59F100
    DebugTable    debug     = read_debug_section(spans.debug, ctx);        // routes payloads through sub_589B90
    ModuleOp      module    = create_builtin_module(ctx);
    read_globals(spans.global,  module, strings, types, constants);
    read_functions(spans.func,  module, strings, types, constants, debug); // bodies invoke sub_5B13D0 per op
    return module;
}
```

The rest of the page walks the container at reimplementation depth: file envelope, the six payload sections, the inter-section reference model, validation behavior, and the separate LLVM bitcode path used when tileiras hands an NVVM module to libNVVM.

## Overall File Format

The container starts with an 8-byte magic. The first three bytes are the upstream MLIR-bytecode framing prefix (`0x06 0x03 0x80`); bytes 3–6 spell `"Tile"`; byte 7 is `0x00` — the tileiras / upstream MLIR split (upstream writes `"\nMLIR"` starting at byte 7):

```text
06 03 80 54 69 6c 65 00    // MLIR-bc framing + "Tile" + 0x00 terminator
```

The trailing zero is a sentinel byte inside the magic, not a C-string terminator. Three unsigned-LEB128 VarInts follow — `major`, `minor`, optional `patch` — encoding the `Tile version` triple. The CUDA 13.1 reader accepts Tile version `13.1.x` for any patch value. A mismatched major or minor produces an `unsupported Tile version ...` diagnostic rather than falling back to upstream MLIR bytecode parsing. See [Header Parser (`sub_5838A0`)](#header-parser-sub_5838a0) for the full magic and version table.

After the version block comes a sequence of sections. Each section starts with one ID byte: the low seven bits are the section ID, the high bit signals an alignment field in the header. A zero ID marks end-of-bytecode.

```c
typedef struct SectionHeader {
    uint8_t id_and_alignment_flag;
    uleb128 length;
    optional<uleb128> alignment;
    uint8_t padding[];
    uint8_t payload[];
} SectionHeader;
```

`length` covers the optional alignment field, padding, and payload. Padding bytes are `0xcf`. The end marker carries no length, alignment, padding, or payload, and its high bit must be clear.

The reader scans section headers first and records the byte span for each section, then decodes bodies in a second pass. Physical section order is therefore flexible — required sections must exist and all offsets must land inside captured spans, but the order on disk is the producer's choice.

## VarInt Encoding

Every multi-byte integer in the container — section length, offset, type reference, operand index, opcode — uses the same unsigned LEB128 variant as upstream MLIR bytecode. The encoding rule is a leading-byte trick: the number of low-order 1-bits in the first byte indicates how many additional bytes follow, the bits above the run-of-ones in that byte form the low bits of the integer, and the subsequent bytes contribute eight bits each in little-endian order.

| First byte | Total bytes | Payload bits | Value range |
|---|---:|---:|---|
| `0xxxxxxx` | 1 | 7 | `0..127` |
| `xxxxxx01` | 2 | 14 | `0..16383` |
| `xxxxx011` | 3 | 21 | `0..2097151` |
| `xxxx0111` | 4 | 28 | up to `2^28 - 1` |
| `xxx01111` | 5 | 35 | up to `2^35 - 1` |
| `xx011111` | 6 | 42 | up to `2^42 - 1` |
| `x0111111` | 7 | 49 | up to `2^49 - 1` |
| `01111111` | 8 | 56 | up to `2^56 - 1` |
| `00000000` | 9 | 64 | up to `2^64 - 1` (all 8 trailing bytes form the integer) |

The leading byte counts its low-order 1-bits, masks them off, and shifts the surviving high bits up to occupy the bottom of the decoded integer; the remaining bytes are appended above those bits. A 10-byte encoding is rejected as overlong — the canonical 9-byte form covers the entire 64-bit range. Signed payloads (the `location_index` slot in particular) wrap the unsigned VarInt in zig-zag: `(n << 1) ^ (n >> 63)` going out, `(u >> 1) ^ -(u & 1)` coming back.

Three concrete decoded examples make the bit pattern unambiguous:

```text
0x0a            → first byte 0000_1010, low bit clear → 1-byte form,
                  payload = 0a >> 1 = 5

0x01 0x02       → first byte 0000_0001, trailing "01" → 2-byte form,
                  payload bits = (byte0 >> 2) | (byte1 << 6)
                              = (0x01 >> 2) | (0x02 << 6)
                              = 0 | 0x80 = 128

0xfb 0xff 0x0f  → first byte 1111_1011, trailing "011" → 3-byte form,
                  payload bits = (byte0 >> 3) | (byte1 << 5) | (byte2 << 13)
                              = (0xfb >> 3) | (0xff << 5) | (0x0f << 13)
                              = 0x1f | 0x1fe0 | 0x1e000 = 0x1ffff = 131071
```

Producers must always emit the canonical (shortest) encoding for a given integer: an overlong but otherwise-valid encoding decodes to the same integer but flags as `"non-canonical VarInt"` and rejects the section.

## Section Walker Algorithm

Once `sub_5838A0` has accepted the envelope and built the blob-section descriptor array, the top-level driver invokes the section walker. The walker is not a recursive parser — it is a fixed, dependency-ordered dispatch over the descriptor array. Earlier sections build the lookup tables that later sections cross-reference, so the order is required even though the on-disk order is the producer's choice.

> ⚡ **QUIRK — on-disk section order is free, walker order is fixed**
> The bytecode envelope lets the producer write sections in any order, but the reader's `walk_sections` always dispatches them in the fixed dependency-ordered sequence (STRING → TYPE → ATTRIBUTE → IR → optional RESOURCE/DEBUG). Two byte-different files can therefore decode to identical IR, and the reader uses descriptor-array lookup rather than position to find each section. A reimplementation that follows on-disk order to drive parsing will read references against half-built tables and emit `unknown <kind>` errors for files the official reader accepts.

```c
ParseResult walk_sections(BytecodeReader *r, const BlobSectionDesc *desc, size_t n_desc,
                          MLIRContext *ctx, ModuleOp *out_module) {
    SectionSpans spans = {0};
    for (size_t i = 0; i < n_desc; ++i)
        spans.by_kind[desc[i].section_kind] = (Span){desc[i].offset, desc[i].length};

    /* Sections must be present in dependency order:
     * 1. STRING section (referenced by every later section)
     * 2. DIALECT section (declares the dialects this bytecode uses; implicit here:
     *    the dialect list lives in the envelope, not in its own section)
     * 3. TYPE section (references STRING)
     * 4. ATTRIBUTE / CONSTANT section (references TYPE)
     * 5. IR section: FUNC + GLOBAL records (references all of the above)
     * 6. RESOURCE section (optional)
     * 7. DEBUG section (optional, references the IR section via location slots)
     */
    StringTable    strings   = read_string_section   (r, spans.by_kind[SEC_STRING]);
    TypeTable      types     = read_type_section     (r, spans.by_kind[SEC_TYPE],     ctx, strings);
    ConstantTable  constants = read_constant_section (r, spans.by_kind[SEC_CONSTANT], ctx, strings, types);
    DebugTable     debug     = spans.by_kind[SEC_DEBUG].length
                               ? read_debug_section  (r, spans.by_kind[SEC_DEBUG],    ctx, strings, types)
                               : empty_debug_table();

    ModuleOp module = create_builtin_module(ctx);
    read_globals  (r, spans.by_kind[SEC_GLOBAL], module, strings, types, constants);
    read_functions(r, spans.by_kind[SEC_FUNC],   module, strings, types, constants, debug);

    *out_module = module;
    return success();
}
```

The walker traverses each section by seeking the reader cursor to `span.begin`, decoding records until the cursor reaches `span.begin + span.length`, then asserting that no bytes were left over. A short read inside a section emits a per-section truncation diagnostic; a long read — cursor past `span.length` after the final record — emits a section-overflow diagnostic. Both fire before any cross-section index from that section is exposed to the next reader, so a corrupt section never poisons downstream lookups with a half-populated table.

The reader trusts the descriptor's offset and length pair to disjoint sections — overlapping spans are not checked. The validation that prevents two sections from claiming the same bytes happens earlier, inside `sub_5838A0`'s preamble loop, where each new descriptor's `[offset, offset+length)` range is checked against the union of previously accepted ranges.

## Header Parser (`sub_5838A0`)

`sub_5838A0` parses the bytecode header. It is invoked from `sub_57FF40` once the top-level reader confirms the input buffer starts with the tileiras magic, and it validates the magic prefix, the `Tile version` field, and the dialect-list / blob-section preamble before handing control to the per-section sub-readers. Everything downstream — section dispatch, attribute decoding, opcode decoding, debug decoding — assumes the header parser has already consumed and accepted this envelope.

**Magic prefix.** Every TileIR bytecode container opens with an 8-byte magic prefix, compared byte-for-byte against the static literal at rodata `0x45EBF08`. The bytes flow through `sub_57F420`, the bounded-read helper that refuses to walk past the end of the input buffer.

| Offset | Byte | Meaning |
|---:|---|---|
| 0 | `0x06` | `MAGIC_LEN_HI` — MLIR-bytecode-compatible framing |
| 1 | `0x03` | `MAGIC_LEN_LO` — MLIR-bytecode-compatible framing |
| 2 | `0x80` | `MAGIC_FLAGS` — MLIR-bytecode-compatible framing |
| 3 | `'T'` (`0x54`) | tileiras dialect tag, byte 1 |
| 4 | `'i'` (`0x69`) | tileiras dialect tag, byte 2 |
| 5 | `'l'` (`0x6C`) | tileiras dialect tag, byte 3 |
| 6 | `'e'` (`0x65`) | tileiras dialect tag, byte 4 |
| 7 | `0x00` | tileiras terminator (upstream MLIR writes `"\nMLIR"` instead) |

The trailing `0x00` is the byte that separates tileiras-MLIR-bytecode from upstream MLIR-bytecode at the magic level. Upstream MLIR fills the same slot with `"\nMLIR"`, so the shared MLIR-bytecode framing prefix combined with the tileiras-specific terminator cleanly partitions the two dialects. On mismatch the parser emits `"invalid magic number at position "` concatenated with the buffer offset, then `", got "` concatenated with the offending byte sequence, then `" expected "` concatenated with the expected literal — three verbatim fragments joined into one diagnostic.

**Tile version table.** Three VarInts follow the magic and encode the `Tile version` triple. The minimum and maximum supported versions live in a static table at rodata `0x45EBF10`:

```c
static const TileVersion supported_versions[] = {
    /*min:*/ { .major = 13, .minor = 1, .patch = 0          }, // inclusive
    /*max:*/ { .major = 13, .minor = 1, .patch = UINT32_MAX }, // inclusive (only 13.1.x)
};
```

Major and minor VarInts are mandatory. The patch VarInt is optional and defaults to zero when absent — the parser reads it for forward compatibility but never gates on its value. The version-range predicates are:

| Predicate | Decision |
|---|---|
| `major < 13` | reject |
| `major == 13 && minor < 1` | reject |
| `major > 13` | reject |
| `major == 13 && minor > 1` | reject |
| `major == 13 && minor == 1` | accept (any patch) |

Only `13.1.x` decodes. On reject, the parser emits `"unsupported Tile version "` followed by the parsed version (formatted via `"{0}.{1}.{2}"` when the patch field was present, or `"{0}.{1}"` when absent), then `"this reader supports versions ["` followed by the rendered min..max range. The two-string format mirrors the patch-optional encoding: a producer that omits the patch VarInt sees its rejection echoed back without a synthetic `.0` suffix.

**Dialect list and blob preamble.** A VarInt dialect-count comes next, then for each dialect a VarInt length-prefixed string followed by a VarInt op-count. Each dialect string is checked against the reader's registered dialect set; unknown dialects emit `"unregistered dialect: "` concatenated with the offending name. The blob-section preamble that follows is a fixed 24-byte record per section:

```c
typedef struct BlobSectionDesc {
    uint8_t  section_kind;   // one of the seven section IDs documented above
    uint8_t  pad[3];         // alignment padding
    uint64_t offset;         // byte offset from the start of the buffer
    uint64_t length;         // payload length in bytes
    uint32_t alignment;      // required alignment of the payload base
} BlobSectionDesc;
```

The descriptor array lets the reader build the section-span table without scanning section headers in order: every section's location is known once the preamble is consumed, so the per-section sub-readers can run in any order the driver finds convenient.

**Section alignment / ordering invariants.** Two structural invariants carry verbatim diagnostics. The end-of-bytecode marker section must have `alignment == 0` — otherwise the parser emits `"end section should not have alignment flag set"`. The end marker must also be the last section in the preamble; otherwise the parser emits `"end section is not the last section"`. Both checks fire before any per-section body is decoded, so a malformed envelope is rejected before the reader commits a single byte of section-payload allocation.

**Pseudocode shape.** The full parser is large, but its skeleton is a magic-and-version prefix followed by the dialect/blob loop. The prefix half looks like:

```c
LogicalResult parseBytecodeHeader(Reader *r, TileVersion *out_ver) {
    uint8_t magic[8];
    if (!readBytes(r, magic, 8))           return emit("failed to read magic"), failure();
    if (memcmp(magic, kMagicLit, 8) != 0)  return emitMagicMismatch(magic),     failure();

    TileVersion v;
    if (!readVarInt(r, &v.major))          return emit("failed to read major"), failure();
    if (!readVarInt(r, &v.minor))          return emit("failed to read minor"), failure();
    if (!readOptionalVarInt(r, &v.patch))  v.patch = 0;
    if (!versionInRange(v))                return emitVersionReject(v),         failure();

    *out_ver = v;
    return success();
}
```

The dialect-list and blob-preamble loops follow the same shape: every VarInt read pairs with a verbatim diagnostic, every structural invariant is checked before the next field is consumed, and the function returns `failure()` on the first mismatch so the surrounding driver surfaces a single precise error rather than a cascade.

## Per-Section Grammar

The seven section IDs are:

| ID | Name | Required | Body layout | Reference width | Outbound references |
|---:|---|---:|---|---|---|
| `0x00` | EndOfBytecode | yes | Single zero byte; must be last. | n/a | none |
| `0x01` | String | yes | `numStrings`, aligned `u32 stringOffsets[]`, UTF-8 string data. Strings are not NUL-terminated. | `u32` offsets | none |
| `0x02` | Func | yes | Sequential function records, each with name, signature, flags, location, optional optimization hints, body length, and body bytes. | sequential | String, Type, Debug, Constant |
| `0x03` | Debug | no | Parallel debug-op, debug-index, and debug-attribute tables plus debug payload data. | `u32` and `u64` offsets | String, Type, Debug |
| `0x04` | Constant | no | `numConstants`, aligned `u64 constantOffsets[]`, and self-contained attribute payloads. | `u64` offsets | String, Type, Constant, Debug |
| `0x05` | Type | no | `numTypes`, aligned `u32 typeOffsets[]`, and type-tag payloads. | `u32` offsets | Type |
| `0x06` | Global | no | Sequential global records: symbol name, type, constant initializer, alignment. | sequential | String, Type, Constant |

Function records are the richest records in the container:

```c
typedef struct FunctionRecord {
    uleb128 name_string_index;
    uleb128 function_type_index;
    uint8_t flags;
    uleb128 location_index;
    optional<Attribute> optimization_hints;
    uleb128 body_length;
    uint8_t body[body_length];
} FunctionRecord;
```

The low three flag bits encode visibility, entry/kernel kind, and whether `optimization_hints` is present. Function bodies contain regions, blocks, block arguments, and operation records. Each operation record starts with an opcode and a location reference, followed by opcode-specific operands, attributes, regions, and result type references.

```c
typedef struct RegionBody {
    uleb128 block_count;
    Block blocks[block_count];
} RegionBody;

typedef struct Block {
    uleb128 arg_count;
    uleb128 arg_type_index[arg_count];
    uleb128 op_count;
    OperationRecord ops[op_count];
} Block;

typedef struct OperationRecord {
    uleb128 opcode;          // cuda_tile opcode, 0..109 in CUDA 13.1
    sleb128 location_index;  // -1 means unknown location
    uint8_t payload[];       // decoded by the opcode-specific reader
} OperationRecord;
```

Cross-section references are width-sensitive. String and Type offset tables use `u32` slots; Constant offsets and some Debug cross-reference tables widen to `u64` so large dense payloads remain addressable. Function and Global records are sequential rather than table-indexed.

VarInts are unsigned LEB128 capped at 10 bytes — overlong 11-byte encodings are rejected. Signed integer payloads use zig-zag decoding; APInt multiword payloads apply zig-zag per word.

## Type Tag Dispatch

`sub_59C710` decodes the Type-section payload table. The routine is 6 474 bytes long and switches on a one-byte `TypeTag` at `0x59C79D`; it is the type-side sibling of the Op, Attr, and Debug dispatchers covered later. Each type-table entry triggers one call from `sub_58C0C0` (the cached Type-by-index lookup), itself reached via `sub_58C400` whenever a downstream consumer needs to resolve a type reference. The TypeTag numbering is independent of upstream MLIR's `BytecodeTypeOpcodes.td` and uses the dense 0..18 assignment shown below.

Type records start with a `TypeTag` followed by a tag-specific payload:

| Tag | Type |
|---:|---|
| `0..4` | `i1`, `i8`, `i16`, `i32`, `i64` |
| `5..11` | `f16`, `bf16`, `f32`, `tf32`, `f64`, `f8E4M3FN`, `f8E5M2` |
| `12` | Pointer type |
| `13` | Tile type |
| `14` | Tensor-view type |
| `15` | Partition-view type |
| `16` | Function type |
| `17` | Token type |
| `18` | `f8E8M0FNU` extension, reachable through the extensible registered-type path |

Tile types carry an element type and shape. Tensor views add strides. Partition views point at a tensor-view type, then add tile shape, dimension map, and partition mode. Function types carry parameter and result type-index vectors. The per-tag operand layout in wire order is:

| Tag | Operands read in order | Total VarInts (worst case) |
|---:|---|---:|
| `0..11` | none (integer/float width fully determined by tag) | 0 |
| `12` Pointer | one type-ref | 1 |
| `13` Tile | one type-ref, then a `read_i64_shape` (count VarInt + count signed-LEB128 dims) | 2 + dim_count |
| `14` Tensor view | one type-ref, then a shape VarInt list, then a stride VarInt list | 3 + dim_count + stride_count |
| `15` Partition view | one type-ref, then a shape, then a dim-map list, then a partition-mode byte | 4 + dim_count + map_count |
| `16` Function | a type-ref list for inputs, a type-ref list for results | 2 + input_count + result_count |
| `17` Token | none | 0 |
| `18` `f8E8M0FNU` | extension prefix (registered-type path), then per-extension payload | varies |

```c
Type read_type(Reader *r) {                                  // sub_59C710
    uint64_t tag = read_uleb128(r);                           // (1) one TypeTag VarInt

    switch (tag) {
    case TYPE_I1: case TYPE_I8: case TYPE_I16: case TYPE_I32: case TYPE_I64:
        return integer_type(width_for_tag(tag));
    case TYPE_F16: case TYPE_BF16: case TYPE_F32: case TYPE_TF32:
    case TYPE_F64: case TYPE_F8E4M3FN: case TYPE_F8E5M2:
        return float_type_for_tag(tag);
    case TYPE_POINTER:
        return pointer_type(read_type_ref(r));
    case TYPE_TILE:
        return tile_type(read_type_ref(r), read_i64_shape(r));
    case TYPE_TENSOR_VIEW:
        return tensor_view_type(read_type_ref(r), read_i64_shape(r), read_i64_strides(r));
    case TYPE_PARTITION_VIEW:
        return partition_view_type(read_type_ref(r), read_shape(r), read_dim_map(r), read_partition_mode(r));
    case TYPE_FUNCTION:
        return function_type(read_type_ref_list(r), read_type_ref_list(r));
    case TYPE_TOKEN:
        return token_type();
    default:
        return registered_extension_type(tag, r);
    }
}
```

## Operation Opcode Dispatch

One master dispatcher decodes the entire `cuda_tile` public opcode space: `sub_5B13D0`, 10 650 bytes, jump table at `0x5B158E`. It takes one operation record at a time, reads the opcode as a VarInt, attaches a source location, switches on the opcode integer, and either inlines the canonical op-builder skeleton or tail-calls a dedicated per-op parser. It returns true if the opcode was recognized and the resulting MLIR `Operation*` passed verification, false otherwise.

Calls come from `sub_57FF40`, the bytecode-parse-into-scratch path that walks a function body and stages each operation into per-block operand and location tables before final placement into the materialized region. `sub_5B13D0` itself stays op-local: no block-structure work, no region allocation, and no cursor advances outside its argument context.

The canonical body is a five-step sequence repeated for every operation record:

```c
bool parse_operation(BytecodeReader *r, OpBuilder *b, ValueList *operands,
                     LocAttrList *locs, AttrList *attrs, Operation **out) {
    uint64_t opcode;
    if (!sub_5847D0_read_opcode(r, &opcode))                       // (1) read opcode VarInt
        return error(r, "failed to read operation opcode.");

    Location loc;
    if (!read_location(r, locs, &loc))                             // (2) resolve source location
        return error(r, "failed to read operation location.");

    switch (opcode) {                                              // (3) dispatch on opcode 0..109
    case 0x00:  return sub_58C5C0(r, b, operands, attrs, loc);     // (4a) delegate to per-op parser
    case 0x09:  return build_inline_bitcast(r, b, operands, attrs, loc, out);  // (4b) inline skeleton
    /* ... 108 more cases ... */
    default:    return error(r, "unknown or unimplemented opcode: ", opcode);
    }
    /* (5) common LABEL_280 cleanup chain frees scratch buffers and returns the verifier result */
}
```

Step (1) calls the opcode reader, which returns the VarInt and a position-advanced cursor. Step (2) resolves the location either by reading a `LocAttr` index from the current location-table slot or, when no location was emitted, by synthesizing an `UnknownLoc` from the MLIR context. Step (3) is the 110-entry jump table at `0x5B158E`. Step (4) splits two ways: a tail call to the per-op parser, which reads its own result types, operand indices, and attributes before calling `sub_5847D0`; or an inline ODS-shaped skeleton that reads the result type via `sub_58C400`, reads zero or more operand indices via `sub_585AD0`, then calls `sub_5847D0` with the verbatim mnemonic literal embedded in the dispatcher binary. Step (5) is shared by every case path that does not tail-call: it frees the SSO op-name scratch and the transient attribute vector, then returns the verifier's verdict on the freshly built operation.

Four verbatim diagnostic strings are emitted by this dispatcher and its helpers:

- `"failed to read operation opcode."`
- `"failed to read operation location."`
- `"unknown or unimplemented opcode: "`
- `"failed to create operation '…' due to verification error."`

The first three live inside `sub_5B13D0` itself. The fourth comes from `sub_5847D0`, the create-and-verify helper every case path eventually reaches; its prefix and suffix wrap the mnemonic the current case passed in, so a verifier rejection on, say, `cuda_tile.addf` surfaces as `failed to create operation 'cuda_tile.addf' due to verification error.`.

Two reserved opcode ranges sit in the jump table and fall through to the default arm. Opcodes 25–36 inclusive (`0x19`–`0x24`) and opcodes 52–57 inclusive (`0x34`–`0x39`) are unassigned in CUDA 13.1 and emit the `"unknown or unimplemented opcode: "` diagnostic if a producer encodes them. These gaps line up with corresponding holes in the public ODS opcode assignment — room for future op additions without invalidating already-shipped bytecode.

The full 110-row dispatch table follows. Each row gives the decimal opcode, the `cuda_tile` mnemonic, and either the dedicated parser address or `inline` for cases handled by the inline ODS-shaped skeleton inside `sub_5B13D0`. Region-bearing ops (entry, for, if, loop, module, reduce, scan) delegate to dedicated parsers because they additionally consume nested region bodies before calling `sub_5847D0`.

| Opcode | Mnemonic | Handler | Notes |
|---:|---|---|---|
|   0 | `cuda_tile.absf` | `sub_58C5C0` | |
|   1 | `cuda_tile.absi` | `sub_58C930` | |
|   2 | `cuda_tile.addf` | `sub_58CCA0` | |
|   3 | `cuda_tile.addi` | `sub_58D3A0` | |
|   4 | `cuda_tile.andi` | `sub_58D7B0` | |
|   5 | `cuda_tile.assert` | `sub_587B50` | |
|   6 | `cuda_tile.assume` | `sub_5A1CA0` | |
|   7 | `cuda_tile.atomic_cas_tko` | `sub_58DB20` | |
|   8 | `cuda_tile.atomic_rmw_tko` | `sub_58EF30` | |
|   9 | `cuda_tile.bitcast` | inline | |
|  10 | `cuda_tile.break` | `sub_5AC120` | |
|  11 | `cuda_tile.broadcast` | `sub_590280` | |
|  12 | `cuda_tile.cat` | `sub_5A8300` | |
|  13 | `cuda_tile.ceil` | inline | |
|  14 | `cuda_tile.cmpf` | `sub_590560` | |
|  15 | `cuda_tile.cmpi` | `sub_590F00` | |
|  16 | `cuda_tile.constant` | `sub_5AFE90` | |
|  17 | `cuda_tile.continue` | `sub_5AB850` | |
|  18 | `cuda_tile.cos` | inline | |
|  19 | `cuda_tile.cosh` | inline | |
|  20 | `cuda_tile.divf` | `sub_591400` | |
|  21 | `cuda_tile.divi` | `sub_591B00` | |
|  22 | `cuda_tile.entry` | `sub_5BAD00` | region-op |
|  23 | `cuda_tile.exp` | `sub_592670` | |
|  24 | `cuda_tile.exp2` | `sub_5920A0` | |
| 25–36 | — | default | reserved; emits `"unknown or unimplemented opcode: "` |
|  37 | `cuda_tile.exti` | `sub_592950` | |
|  38 | `cuda_tile.extract` | `sub_5A8B60` | |
|  39 | `cuda_tile.floor` | `sub_593930` | |
|  40 | `cuda_tile.fma` | `sub_593C10` | |
|  41 | `cuda_tile.for` | `sub_5BBFF0` | region-op |
|  42 | `cuda_tile.ftof` | `sub_592E80` | |
|  43 | `cuda_tile.ftoi` | `sub_5933B0` | |
|  44 | `cuda_tile.get_global` | `sub_59E980` | |
|  45 | `cuda_tile.get_index_space_shape` | `sub_5A9D70` | |
|  46 | `cuda_tile.get_num_tile_blocks` | inline | |
|  47 | `cuda_tile.get_tensor_shape` | `sub_5AA6E0` | |
|  48 | `cuda_tile.get_tile_block_id` | inline | post-switch fallthrough loop at `0x5B2C05` |
|  49 | `cuda_tile.global` | `sub_5B0720` | GlobalOp |
|  50 | `cuda_tile.if` | `sub_5BCCD0` | region-op |
|  51 | `cuda_tile.int_to_ptr` | inline | |
| 52–57 | — | default | reserved; emits `"unknown or unimplemented opcode: "` |
|  58 | `cuda_tile.iota` | inline | |
|  59 | `cuda_tile.itof` | `sub_594400` | |
|  60 | `cuda_tile.join_tokens` | `sub_5AAF80` | |
|  61 | `cuda_tile.load_ptr_tko` | `sub_5A30D0` | |
|  62 | `cuda_tile.load_view_tko` | `sub_5A4420` | |
|  63 | `cuda_tile.log` | inline | |
|  64 | `cuda_tile.log2` | `sub_594980` | |
|  65 | `cuda_tile.loop` | `sub_5BDA00` | region-op |
|  66 | `cuda_tile.make_partition_view` | `sub_594CF0` | |
|  67 | `cuda_tile.make_tensor_view` | `sub_5AE190` | |
|  68 | `cuda_tile.make_token` | inline | |
|  69 | `cuda_tile.maxf` | `sub_594FD0` | |
|  70 | `cuda_tile.maxi` | `sub_595630` | |
|  71 | `cuda_tile.minf` | `sub_595B60` | |
|  72 | `cuda_tile.mini` | `sub_5961C0` | |
|  73 | `cuda_tile.mmaf` | `sub_5966F0` | |
|  74 | `cuda_tile.mmai` | `sub_596A60` | |
|  75 | `cuda_tile.module` | `sub_5BE6E0` | region-op |
|  76 | `cuda_tile.mulf` | `sub_596EE0` | |
|  77 | `cuda_tile.mulhii` | `sub_5979F0` | |
|  78 | `cuda_tile.muli` | `sub_5975E0` | |
|  79 | `cuda_tile.negf` | inline | |
|  80 | `cuda_tile.negi` | inline | |
|  81 | `cuda_tile.offset` | `sub_597CD0` | |
|  82 | `cuda_tile.ori` | inline | |
|  83 | `cuda_tile.permute` | `sub_59E060` | |
|  84 | `cuda_tile.pow` | `sub_597FB0` | |
|  85 | `cuda_tile.print_tko` | `sub_5AD2C0` | |
|  86 | `cuda_tile.ptr_to_int` | `sub_598290` | |
|  87 | `cuda_tile.ptr_to_ptr` | `sub_598570` | |
|  88 | `cuda_tile.reduce` | `sub_5BF2E0` | region-op |
|  89 | `cuda_tile.remf` | inline | |
|  90 | `cuda_tile.remi` | `sub_598850` | |
|  91 | `cuda_tile.reshape` | `sub_598D80` | |
|  92 | `cuda_tile.return` | `sub_5A9400` | |
|  93 | `cuda_tile.rsqrt` | `sub_599110` | |
|  94 | `cuda_tile.scan` | `sub_5B9B20` | region-op |
|  95 | `cuda_tile.select` | inline | |
|  96 | `cuda_tile.shli` | `sub_599700` | |
|  97 | `cuda_tile.shri` | `sub_599B10` | |
|  98 | `cuda_tile.sin` | `sub_59A3B0` | |
|  99 | `cuda_tile.sinh` | `sub_59A040` | |
| 100 | `cuda_tile.sqrt` | `sub_59A690` | |
| 101 | `cuda_tile.store_ptr_tko` | `sub_5A55B0` | |
| 102 | `cuda_tile.store_view_tko` | `sub_5A6790` | |
| 103 | `cuda_tile.subf` | `sub_59B0E0` | |
| 104 | `cuda_tile.subi` | `sub_59B7E0` | |
| 105 | `cuda_tile.tan` | inline | |
| 106 | `cuda_tile.tanh` | `sub_59BBF0` | |
| 107 | `cuda_tile.trunci` | `sub_59BF60` | |
| 108 | `cuda_tile.xori` | `sub_59C3A0` | |
| 109 | `cuda_tile.yield` | `sub_5AC9F0` | |

Opcode `0x6E` — `atan2` in the public 13.2 opcode space — is absent from this binary. The dispatcher has no case for it and embeds no `cuda_tile.atan2` mnemonic; encoding the op would land on the default arm and surface the `"unknown or unimplemented opcode: "` diagnostic. Consistent with a 13.1-vintage reader that predates the atan2 addition.

**Worked encode example.** Take the operation

```mlir
%c = cuda_tile.addi %a, %b : tile<8 × i32>
```

and assume the surrounding context has already populated the per-section tables so that `%a` is value-table entry `4`, `%b` is value-table entry `5`, and `tile<8xi32>` is type-table entry `3`. The function body's operation-record encoder writes seven fields in fixed order, each as a single VarInt:

| Field | VarInt | Byte | Decoded |
|---|---|---|---|
| opcode | `0x03` | `03` | `3` → `cuda_tile.addi` (dispatch row 3 above) |
| location index (signed LEB128) | `0x7f` | `7f` | `-1` → `UnknownLoc` (no `--lineinfo`) |
| result type-ref | `0x03` | `03` | `3` → `tile<8xi32>` from the type table |
| operand count | `0x02` | `02` | `2` operands |
| operand 0 value-ref | `0x04` | `04` | `4` → `%a` from the value table |
| operand 1 value-ref | `0x05` | `05` | `5` → `%b` from the value table |
| attribute-dict ref | `0x00` | `00` | `0` → empty dict (no inline attrs) |

The final on-wire byte stream for this operation record is therefore exactly seven bytes:

```text
03 7f 03 02 04 05 00
```

A run with `--lineinfo` replaces the `0x7f` sentinel with a non-negative `LocAttr` index encoded as a positive zig-zagged VarInt — typically one byte (`0x00` for index `0`, `0x02` for index `1`, `0x04` for index `2`, and so on) — and stretches the record to eight bytes. A run with a non-empty inline attribute dictionary stretches the trailing `0x00` into a VarInt index into the attribute table, again typically one byte for small modules.

The operation cost in the IR section is therefore constant in the number of operands plus a tiny constant for the bookkeeping fields, and is independent of the mnemonic string. The mnemonic `cuda_tile.addi` lives once — in the dispatcher's per-opcode string literal at dispatch case `0x03` — and never appears in the per-operation byte stream.

The three functions around this dispatcher fit together cleanly. `sub_5847D0` is the opcode-reader producing the integer the master switch keys on, and every case of `sub_5B13D0` either inlines the ODS skeleton that ends in a `sub_5847D0` call or tail-calls a per-op parser that itself ends in `sub_5847D0`. The Location decoder runs once per operation — between the opcode read and the switch — and writes the resolved `Location` into the per-op slot every case path reads when populating its `OperationState`. One layer up, `sub_57FF40` is the bytecode-parse-into-scratch path the driver invokes per function body; it calls `sub_5B13D0` in a loop for each operation record while maintaining the operand, location, and attribute vectors the dispatcher consumes through its argument context.

## Self-Contained Attribute Dispatch

Every attribute payload is self-contained. Constants, function optimization hints, and the inline attribute slots on operations all funnel through the same decoder. The 13-case dispatcher inside `sub_59F100` recognizes string, float, type, dense-elements (int/float and string variants), divisibility, dense-i64-array (two layout variants), same-elements, bounded (three discriminator variants), and assume-predicate attributes. The integer, bool, array, dictionary, and optimization-hints attribute kinds are not handled by this dispatcher — they arrive through the upstream MLIR builtin dispatcher path on a different code path. A Global initializer must resolve to a dense integer-or-floating elements attribute even though the Constant section can store a broader attribute set.

Anywhere the reader encounters an attribute payload that does not come pre-resolved through the Constant offset table — operation attribute dictionaries, type-attribute slots, location-attribute slots, the Constant payloads themselves — the bytes route through `sub_59F100`. This dispatcher is the attribute-side sibling of the 110-case opcode switch. Roughly 8 KB, it dispatches on a `uint32_t` AttrTag through a jump table at the entry switch and returns either a heap-allocated `Attribute` on success or `nullptr` on failure. The caller pushes the result into the bytecode reader's attribute table; failures propagate up to the section-level error path that aborts the load.

The shipped tileiras tag numbering is **wire-format-breaking versus upstream MLIR**. The two numberings are reproduced side by side so the divergence is unambiguous:

| AttrTag | Upstream MLIR `BytecodeEnums.h::AttributeTag` | Tileiras `sub_59F100` |
|---:|---|---|
| 0 | (reserved / sentinel) | (default-arm; emits "unsupported AttributeTag") |
| 1 | `IntegerAttr` | `StringAttr` |
| 2 | `FloatAttr` | `FloatAttr` |
| 3 | `BoolAttr` | `TypeAttr` |
| 4 | `TypeAttr` | `DenseElementsAttr` (int/float) |
| 5 | `StringAttr` | `DenseElementsAttr` (string) |
| 6 | `ArrayAttr` | `DivByAttr` |
| 7 | `DenseElements` | `DenseI64ArrayAttr` (variant A) |
| 8 | `DivByAttr` | `DenseI64ArrayAttr` (variant B) |
| 9 | `SameElementsAttr` | `SameElementsAttr` |
| 10 | `Dictionary` | `BoundedAttr` (variant 0) |
| 11 | `OptimizationHints` | `BoundedAttr` (variant 1) |
| 12 | `BoundedAttr` | `BoundedAttr` (variant 2) |
| 13 | (no upstream slot) | `AssumePredicateAttr` |

The only tag that matches upstream by accident is tag `2` (`FloatAttr` in both). Every other tag in the `1..13` range disagrees: tag `1` is `StringAttr` here versus upstream `IntegerAttr`, tag `4` lands on `DenseElementsAttr` instead of `TypeAttr`, tag `5` lands on `DenseElementsAttr<string>` instead of `StringAttr`, tag `6` lands on `DivByAttr` instead of `ArrayAttr`, and so on. Going the other direction, an `AssumePredicateAttr` emitted by tileiras at tag 13 has no destination in upstream's table at all — upstream's reader rejects the tag with its own default-arm diagnostic.

The structural consequence is sharper than tag-by-tag remapping: **tileiras's bytecode reader cannot consume upstream MLIR's bytecode files when those files carry attributes**, and tileiras-emitted bytecode (when a future build links a writer) cannot be loaded by stock MLIR. The textual MLIR asm is still interoperable through the printer / parser, but the bytecode wire format is a hard fork. Any external tool that wants to round-trip MLIR bytecode through both tileiras and upstream MLIR must freeze the tag assignments used by this binary rather than the ones in the upstream header. The upstream numbering is reserved for future stock cuda_tile builds; the shipped binary stays compatible with an earlier frozen scheme.

The thirteen recognized tag values, the attribute kinds they construct, and the per-tag builder functions are:

| Tag | Attribute kind | Builder | Notes |
|---:|---|---|---|
|  1 | `StringAttr` | inline | Reads SSO + raw bytes |
|  2 | `FloatAttr` | inline | Reads u32 type-ref + f64 value |
|  3 | `TypeAttr` | inline | Reads u32 type-ref |
|  4 | `DenseElementsAttr` (int/float) | `sub_59FB80` | Reads shape + elem-type + payload |
|  5 | `DenseElementsAttr` (string) | `sub_59FCD0` | Reads shape + length-prefixed strings |
|  6 | `DivByAttr` | `sub_59FE40` | Reads divisor + verify-with-assume payload |
|  7 | `DenseI64ArrayAttr` (variant A) | `sub_59FF60` | Inline-cap layout |
|  8 | `DenseI64ArrayAttr` (variant B) | `sub_5A0080` | Sidecar-cap layout |
|  9 | `SameElementsAttr` | `sub_5A01A0` | Reads canonical-form payload |
| 10 | `BoundedAttr` (variant 0) | `sub_5A02C0` | Reads lower-bound payload |
| 11 | `BoundedAttr` (variant 1) | `sub_5A03E0` | Reads upper-bound payload |
| 12 | `BoundedAttr` (variant 2) | `sub_5A0500` | Reads lower+upper payload |
| 13 | `AssumePredicateAttr` | `sub_5A0620` | Reads packed predicate |

Tags 1, 2, and 3 decode inline in `sub_59F100` itself. Tag 1 resolves a string via `sub_59AD90` and wraps it in `StringAttr`. Tag 2 reads a type reference, validates it as a `FloatType` via `sub_58C400`, reads an inline `APFloat` payload via `sub_586200`, and dispatches through the `sub_4462700`-family float-type-builder to produce a `FloatAttr`. Tag 3 reads a type reference via `sub_58BDE0` and wraps it in `TypeAttr`. Every other tag tail-calls a dedicated sub-decoder in the `sub_59FB80`–`sub_5A0620` cluster; those decoders read the tag-specific payload, build the corresponding attribute, and either return the new attribute or emit the per-decoder error string and return `nullptr`. The default arm covers tag 0 and every tag above 13: it emits `"unsupported AttributeTag "` (verbatim, trailing space included) concatenated with the tag integer and the suffix `" for self-contained attribute"`, then returns `nullptr`. Diagnostics route through the standard emitter chain `sub_57EA50` / `sub_581460` at severity `0x103`.

The canonical body is the entry prologue plus the 13-way switch and the default-arm error path:

```c
Attribute *parseSelfContainedAttribute(BytecodeReader *r) {
    uint32_t tag;
    if (!read_varint_u32(r, &tag)) {                              // (1) read AttrTag VarInt
        emit_error(r, "failed to read AttributeTag for self-contained attribute.");
        return NULL;
    }

    switch (tag) {                                                // (2) 13-case dispatch
    case  1: return read_string_attr_inline(r);                   // StringAttr via sub_59AD90
    case  2: return read_float_attr_inline(r);                    // FloatAttr (type-ref + APFloat)
    case  3: return read_type_attr_inline(r);                     // TypeAttr (type-ref only)
    case  4: return sub_59FB80(r);                                // DenseElementsAttr int/float
    case  5: return sub_59FCD0(r);                                // DenseElementsAttr string
    case  6: return sub_59FE40(r);                                // DivByAttr
    case  7: return sub_59FF60(r);                                // DenseI64ArrayAttr variant A
    case  8: return sub_5A0080(r);                                // DenseI64ArrayAttr variant B
    case  9: return sub_5A01A0(r);                                // SameElementsAttr
    case 10: return sub_5A02C0(r);                                // BoundedAttr variant 0
    case 11: return sub_5A03E0(r);                                // BoundedAttr variant 1
    case 12: return sub_5A0500(r);                                // BoundedAttr variant 2
    case 13: return sub_5A0620(r);                                // AssumePredicateAttr
    default:                                                      // (3) default-arm error path
        emit_error(r, "unsupported AttributeTag ", tag, " for self-contained attribute");
        return NULL;
    }
}
```

Twenty-one verbatim diagnostic strings are reachable from `sub_59F100` and its inline tag arms. They are reproduced below exactly as they appear in the binary; the trailing period or trailing space is part of the string. The "Tag" column identifies which switch arm emits each string, and the "Trigger" column states the failure condition that surfaces the diagnostic.

| Tag | Verbatim string | Trigger |
|---:|---|---|
| any | `"failed to read AttributeTag for self-contained attribute."` | Entry-prologue VarInt read of the AttrTag failed. |
|  1 | `"string index "` | Out-of-range string-table index reported by `sub_59AD90`; concatenated with the offending index. |
|  1 | `"failed to read StringAttr."` | StringAttr decode reached the SSO read but the underlying byte slice was short. |
|  2 | `"failed to read valid FloatType for FloatAttr"` | Type-ref for the FloatAttr resolved to something that is not a `FloatType`. |
|  2 | `"failed to cast parsed attribute to FloatAttr"` | Builder produced a non-FloatAttr (post-construction invariant guard). |
|  3 | `"failed to get referenced type for TypeAttr"` | Type-table lookup for TypeAttr's type-ref returned null. |
|  4 | `"failed to read valid MLIR Type for self-contained DenseElementsAttr"` | Element-type reference for the dense attribute did not resolve. |
|  4 | `"array contains unsupported value "` | Dense int/float bulk-element loop hit a payload word it cannot decode; concatenated with the value. |
|  5 | `"failed to read number of string attrs in DenseElementsAttr"` | String-variant count-prefix read failed. |
|  5 | `"failed to read string in DenseElementsAttr"` | String-variant per-element string read failed. |
|  6 | `"failed to read divisor for DivByAttr"` | DivByAttr divisor field VarInt read failed. |
|  6 | `"failed to read flags byte for DivByAttr"` | DivByAttr flags byte (verify-with-assume + covariance bits) read failed. |
|  6 | `"failed to read value for 'every' in DivByAttr"` | DivByAttr `every` predicate-covariance field read failed. |
|  6 | `"failed to read value for 'along' in DivByAttr"` | DivByAttr `along` predicate-covariance field read failed. |
|  7,8 | `"failed to read DenseI64ArrayAttr values."` | DenseI64ArrayAttr bulk i64 value read failed in either layout variant. |
|  9 | `"failed to read DenseI64ArrayAttr for SameElementsAttr"` | SameElementsAttr canonical-form payload (which is itself a DenseI64ArrayAttr) failed to decode. |
| 10,11,12 | `"failed to read flags byte for BoundedAttr"` | BoundedAttr variant-discriminator flags byte read failed. |
| 10,11,12 | `"failed to read lower bound for BoundedAttr"` | BoundedAttr lower-bound payload read failed (variants 0 and 2). |
| 10,11,12 | `"failed to read upper bound for BoundedAttr"` | BoundedAttr upper-bound payload read failed (variants 1 and 2). |
| default | `"unsupported AttributeTag "` | Default arm; concatenated with the tag integer. |
| default | `" for self-contained attribute"` | Default-arm suffix; concatenated after the tag integer to complete the diagnostic. |

The `"unsupported AttributeTag "` / `" for self-contained attribute"` pair is the canonical sentinel for forward-incompatible bytecode: any future tileiras that adds AttrTag values 14+ will be rejected by this CUDA 13.1 reader with that exact pair of fragments wrapping the offending integer. Producers that need to stay compatible with the shipped binary must restrict themselves to the thirteen tags above.

This dispatcher relates to the rest of the bytecode reader the same way the opcode dispatcher does. Callers are `sub_5A0A50` (the Constant-section attribute-table populator), `sub_5A1CA0` (the `cuda_tile.assume` parser, which carries a self-contained `AssumePredicateAttr` payload), `sub_5A2410`, and `sub_5A7AD0`. Each one passes a reader cursor and receives a heap-allocated `Attribute` or a propagated failure back; no caller attempts to recover from a `nullptr` return. The per-tag builders in the `sub_59FB80`–`sub_5A0620` cluster share callees with the inline arms — `sub_57BCF0` for VarInts, `sub_58C400` for Type-by-index, `sub_58BDE0` for Attr-by-index, `sub_456A580` for vector reservation, `sub_586200` for inline APInt/APFloat decoding — so the dispatcher's behavior is fully described by the tag table above plus the diagnostic table.

This section is the attribute-side companion to the [Operation Opcode Dispatch](#operation-opcode-dispatch) above. The corresponding dialect-level question — which `cuda_tile` attributes are recognized by this binary at all — is summarized in [Dialect Bytecode Reader/Writer Status — Status Matrix](dialect-readers-status.md#status-matrix). The [cuda_tile bytecode reference](../dialects/cuda_tile/bytecode.md) details the attribute-readers per dialect, including the exact payload layout each `sub_59FB80`–`sub_5A0620` builder consumes.

## Debug-Info Attribute Dispatch

Debug information — `DICompileUnit`, `DIFile`, `DILexicalBlock`, `DILoc`, `DISubprogram`, `CallSite` — does not flow through the AttrTag dispatcher above. It has its own third dispatcher: `sub_589B90`. The function is 8 779 bytes, switches on a `uint32_t` DebugTag at `0x589E05`, and fires whenever a `DistinctAttr`-class or LLVM `DI*` attribute appears in any Location slot, in the Debug section's parallel attribute table, or — through recursion — as a nested scope reference inside another debug attribute. It rounds out the trio of bytecode dispatchers alongside the 13-case AttrTag switch in `sub_59F100` and the 110-case Op opcode switch in `sub_5B13D0`. All three are reached from the top-level bytecode-parse routine `sub_57FF40`, which walks function bodies and routes each encountered tag through the appropriate dispatcher based on its section context.

The seven recognized tag values, the attribute kinds they construct, and the per-tag builder strategy are:

| Tag | Attribute kind | Builder | Notes |
|---:|---|---|---|
| 0 | (default) | inline | Hit when the tag is 0 or unknown; emits the "fail to read kind" diagnostic and returns `nullptr` |
| 1 | `DICompileUnitAttr` | inline | File ref + producer string + flags |
| 2 | `DIFileAttr` | inline | Name string + directory string |
| 3 | `DILexicalBlockAttr` | inline | Scope ref + file ref + line + column; **recursive on scope** |
| 4 | `DILocAttr` | inline | Scope ref + file ref + line + column + optional inlined-at ref; **recursive on scope and inlined-at** |
| 5 | `DISubprogramAttr` | `sub_588E60` (3 375 B) | CU ref + name + linkage name + scope + line + flags + sp-purpose + optional spFlags |
| 6 | `CallSiteAttr` | inline | Callee subprogram ref + caller subprogram ref + line + column; **recursive on both subprogram refs** |

Tag 5 is the only case that tail-calls a dedicated sub-parser. `DISubprogramAttr` carries the heaviest payload of the seven — compile-unit reference, function name string, linkage-name string, enclosing scope reference, line number, generic flags, special-purpose discriminator, and an optional `spFlags` word — and the body is large enough that the dispatcher delegates rather than inlining it. The 8 779-byte main body of `sub_589B90` open-codes every smaller case: tags 1, 2, 3, 4, and 6 each have their full read sequence inline in the switch arm, with one diagnostic per failable field read.

Tags 3, 4, and 6 decode scope-like cross-references and call `sub_589B90` recursively. A `DILexicalBlock` references its enclosing scope, itself another `DI*` attribute. A `DILoc` references both its containing scope and, if inlined, an inlined-at location, both of which route back through the same dispatcher. A `CallSite` references its caller subprogram and its callee subprogram, again as full debug attributes. The recursion has no cycle detection: the bytecode writer never emits cycles and the reader trusts the input, so a malformed stream with a transitive scope cycle recurses until the stack is exhausted. Producers that hand-craft debug bytecode must topologically order the debug table so every reference points at an attribute emitted earlier in the stream.

**Worked example — recursive scope cycle.** A small input that exercises the recursion (and shows the cycle failure mode) starts from a `DILexicalBlock` whose scope reference points back at itself. Upstream MLIR forbids self-cycles, but a hand-crafted bytecode stream can emit them; the reader's behavior on such input is observable and worth documenting.

```text
debug attribute table, index 0:
    tag  = 3                          // DILexicalBlock
    scope_ref  = attr_index(0)        // self-reference (the cycle)
    file_ref   = attr_index(1)        // forward ref to the DIFile below
    line       = 42
    column     = 0
debug attribute table, index 1:
    tag  = 2                          // DIFile
    name_string_index      = 7
    directory_string_index = 8
```

Loading this table walks `sub_589B90` like so:

1. Top-level call: tag VarInt `0x03` → `DILexicalBlock` arm.
2. Read scope-ref VarInt `0x00` → recurse into `sub_589B90` at attribute index `0`.
3. Top-level frame is still in flight; the recursive call reads tag VarInt `0x03` again and recurses again.
4. The cycle has no fixed point: step (3) repeats until the C stack overflows. Empirically this fires after a few thousand frames on a default 8 MiB stack; the process dies with `SIGSEGV`, not with a tileiras diagnostic.

A well-formed equivalent of the same intent emits the inner attribute first and indexes into it from the outer one:

```text
debug attribute table, index 0:
    tag  = 2                          // DIFile, emitted first
    name_string_index      = 7
    directory_string_index = 8
debug attribute table, index 1:
    tag  = 3                          // DILexicalBlock, now references a *prior* attr
    scope_ref  = attr_index(0)        // resolves cleanly
    file_ref   = attr_index(0)
    line       = 42
    column     = 0
```

The constructed `mlir::LocationAttr` is a `DILexicalBlockAttr` whose `scope` and `file` both point at the `DIFileAttr` at index `0`. The recursion bottoms out on the first call because tag `2` (DIFile) has no scope-shaped fields and decodes inline.

The takeaway is asymmetric: well-formed input always terminates in a single bounded recursion sweep because attributes are emitted in topological order; ill-formed input that introduces a cycle is detected only by stack exhaustion. A future reader that wants to harden this path would maintain a visited-set keyed by attribute index during the recursive walk and emit a `"cyclic debug attribute reference at index "` diagnostic on revisit. The shipped CUDA 13.1 reader does not.

The canonical dispatcher body is the entry-prologue VarInt read plus the 7-arm switch:

```c
Attribute *parseDebugAttribute(BytecodeReader *r) {
    uint32_t tag;
    if (!read_varint_u32(r, &tag)) {                                  // (1) read DebugTag VarInt
        emit_error(r, "failed to read kind tag");
        return NULL;
    }

    switch (tag) {                                                    // (2) 7-arm dispatch
    case 0:  emit_error(r, "unknown debug attribute tag");            // default-equivalent inline arm
             return NULL;
    case 1:  return read_di_compile_unit_inline(r);                   // DICompileUnitAttr
    case 2:  return read_di_file_inline(r);                           // DIFileAttr
    case 3:  return read_di_lexical_block_inline(r);                  // recurses on scope
    case 4:  return read_di_loc_inline(r);                            // recurses on scope + inlined-at
    case 5:  return sub_588E60(r);                                    // DISubprogramAttr (delegated)
    case 6:  return read_call_site_inline(r);                         // recurses on caller and callee
    default: emit_error(r, "unsupported DebugTag ", tag);             // forward-incompatibility sentinel
             return NULL;
    }
}
```

Fourteen verbatim diagnostic strings are reachable from `sub_589B90` and its inline tag arms. They are reproduced below exactly as they appear in the binary; the trailing punctuation, if any, is part of the string. The "Tag" column identifies which switch arm emits each string, and the "Trigger" column states the failure condition that surfaces the diagnostic.

| Tag | Verbatim string | Trigger |
|---:|---|---|
| any | `"string index "` | Out-of-range string-table index reported by the shared string lookup; concatenated with the offending index. |
|  1 | `"failed to read file attribute when parsing DICompileUnitAttr"` | `DICompileUnitAttr` file-reference field read failed. |
|  1 | `"failed to read producer for DICompileUnitAttr"` | `DICompileUnitAttr` producer string field read failed. |
|  2 | `"failed to read file name attribute when parsing DIFileAttr"` | `DIFileAttr` file-name string field read failed. |
|  2 | `"failed to read directory attribute when parsing DIFileAttr"` | `DIFileAttr` directory string field read failed. |
|  3 | `"failed to read scope attribute when parsing DILexicalBlockAttr"` | `DILexicalBlockAttr` enclosing-scope recursive read failed. |
|  3 | `"failed to read file attribute when parsing DILexicalBlockAttr"` | `DILexicalBlockAttr` file-reference field read failed. |
|  3 | `"failed to read line number when parsing DILexicalBlockAttr"` | `DILexicalBlockAttr` line-number VarInt read failed. |
|  3 | `"failed to read column number when parsing DILexicalBlockAttr"` | `DILexicalBlockAttr` column-number VarInt read failed. |
|  4 | `"failed to read scope attribute when parsing DILocAttr"` | `DILocAttr` containing-scope recursive read failed. |
|  4 | `"failed to read file name attribute when parsing FileLineColLoc"` | Inner `FileLineColLoc` file-name field read failed inside the `DILocAttr` arm. |
|  4 | `"failed to read line number when parsing FileLineColLoc"` | Inner `FileLineColLoc` line-number VarInt read failed inside the `DILocAttr` arm. |
|  4 | `"failed to read column number when parsing FileLineColLoc"` | Inner `FileLineColLoc` column-number VarInt read failed inside the `DILocAttr` arm. |
|  6 | `"failed to read callee attribute when parsing CallSiteLoc"` | `CallSiteAttr` callee-subprogram recursive read failed. |
|  6 | `"failed to read caller attribute when parsing CallSiteLoc"` | `CallSiteAttr` caller-subprogram recursive read failed. |

Two structural observations follow from the table. The tag 4 arm emits its inner errors under the `FileLineColLoc` name rather than `DILocAttr`, which reflects how `DILocAttr` is built on top of MLIR's `FileLineColLoc` primitive: line and column flow through a sub-helper shared with plain locations, and that sub-helper emits its own diagnostics under its own attribute name. The tag 6 arm spells `CallSiteLoc` rather than `CallSiteAttr` for the same reason: the location form predates the attribute form in MLIR's debug-info subsystem, and the embedded diagnostic literals carry the older name. Consumers parsing tileiras error output must accept both spellings as referring to the same `sub_589B90` switch arms.

`sub_589B90`'s relationship with the rest of the bytecode reader mirrors the other two dispatchers. Callers are `sub_588E60` itself (when the `DISubprogramAttr` body needs to recurse for its CU or scope reference), `sub_58BDE0` (the Attr-by-index cached lookup, which routes any `DistinctAttr`-class index through the debug dispatcher), and itself recursively as described above. Every caller passes a reader cursor and receives a heap-allocated `Attribute` or a propagated `nullptr`; nobody recovers from a failed debug read, so a single corrupt DebugTag aborts the entire module load. Shared callees with the other two dispatchers are `sub_57BCF0` for VarInts, `sub_58BDE0` for Attr-by-index, the `sub_57EA50` / `sub_581460` emitter pair at severity `0x103` for diagnostics, and the string-table lookup path that emits the shared `"string index "` prefix on out-of-range references.

This section is the debug-info companion to the [Operation Opcode Dispatch](#operation-opcode-dispatch) and [Self-Contained Attribute Dispatch](#self-contained-attribute-dispatch) sections above. The dialect-level question of which debug attributes the bytecode writer actually emits in CUDA 13.1 is tracked in [Dialect Bytecode Reader/Writer Status — Status Matrix](dialect-readers-status.md#status-matrix); the seven tags above correspond to the strict subset of LLVM `DI*` attributes that survive round-tripping through this reader.

## Ordering and Diagnostics

Physical section order stays flexible because the reader captures section spans before decoding bodies. Required sections are String and Func. Structural errors live in a separate channel from mandatory-section errors and per-section decode errors — the distinction lets tools tell "not a TileIR file" apart from "valid TileIR envelope with a malformed Type section".

The driver also distinguishes TileIR bytecode from upstream MLIR bytecode. When the input looks like ordinary MLIR bytecode, the diagnostic says so rather than emitting only a generic magic-number failure.

## NVPTX LLVM Bitcode Path

When tileiras takes the in-process libNVVM path, it also serializes LLVM bitcode — a different format entirely from the TileIR bytecode described above. The NVPTX64 data layout is stamped onto the LLVM module unconditionally before serialization:

```c
const char *NVPTX64_DATA_LAYOUT =
    "e-p:64:64:64-p3:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-"
    "i64:64:64-i128:128:128-f32:32:32-f64:64:64-v16:16:16-v32:32:32-"
    "v64:64:64-v128:128:128-n16:32:64";
```

The override discards whatever data layout MLIR's LLVM dialect translation left behind. The NVPTX ABI fact worth pinning down is address space 3 — 32-bit shared memory.

NVPTX modules ship as bare LLVM bitcode, not the wrapper-framed object format used for some host triples. The bitcode identifies itself as LLVM 21-era output and reaches libNVVM under the module name `mlir-input`.

```c
BitcodeBuffer serialize_for_libnvvm(LLVMModule *m) {
    module_set_data_layout(m, NVPTX64_DATA_LAYOUT);

    BitcodeBuffer bc = write_llvm_bitcode(
        m,
        /*wrapper=*/false,
        /*producer=*/"LLVM21.0.0git");

    return bc;
}

NvvmResult compile_with_libnvvm(BitcodeBuffer bc) {
    NvvmProgram prog = nvvmCreateProgram();
    nvvmAddModuleToProgram(prog, bc.data, bc.size, "mlir-input");
    nvvmCompileProgram(prog, options);
    nvvmVerifyProgram(prog, options);
    return nvvmGetCompiledResult(prog);
}
```

The default command-line path still goes through PTX and `ptxas`. The bitcode path only matters when the pipeline is wired to use libNVVM directly. The NVPTX target initialization and the data-layout stamping documented above are covered end-to-end in [NVPTX Bring-up and Target Init](../codegen/nvptx-bring-up-and-target-init.md); the libdevice bitcode that gets linked into the same module is documented in [libdevice Overview — Link, inline, simplify](../libdevice/overview.md#link-inline-simplify).

## Cross-References

This page documents the wire-format the bytecode reader consumes; four companion pages cover the reader from complementary angles. [Frontend Contract and Tile IR Emission](../topics/frontend-contract-and-tile-ir-emission.md) documents the producer-side rules a frontend must satisfy to emit conformant bytecode — the dialect list, the magic and version constants, the AttrTag numbering, and the canonical VarInt encoding — and catalogues the common emission mistakes that produce buffers this reader rejects. [Dialect Bytecode Reader/Writer Status](dialect-readers-status.md) restricts the wire format to the dialects that actually ship a reader — `cuda_tile` is the only TileIR dialect with one, and no TileIR dialect ships a writer — and frames the asymmetry as a deliberate input-only driver contract. [Dialect Asm-Printer Status](asm-printer-status.md) documents the textual side of the same contract, because round-trip workflows on intermediate dialects rely on the asm-printer rather than the bytecode writer this binary does not link. [cuda_tile Bytecode Reader](../dialects/cuda_tile/bytecode.md) zooms back in on the `cuda_tile`-private dispatchers — the 18-case TypeTag dispatcher, the cuda_tile-specific AttrTag payload shapes that route through the 13-case dispatcher documented above, and the 110-case Op opcode dispatcher whose dispatch table is reproduced in [Operation Opcode Dispatch](#operation-opcode-dispatch). The wire-format-breaking AttrTag divergence is the most consequential single fact across all four pages: a bytecode file containing attributes is not portable between tileiras and stock MLIR, and any reimplementation must freeze the tileiras numbering reproduced in [Self-Contained Attribute Dispatch](#self-contained-attribute-dispatch).
