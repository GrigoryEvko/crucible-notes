# cuda_tile Bytecode Reader and Writer

## Abstract

The `cuda_tile` dialect ships its own bytecode reader and writer. Neither parses a standalone container — the top-level TileIR envelope is handled by the generic MLIR bytecode header parser documented in [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md). What `cuda_tile` contributes is the dialect-private Op-opcode dispatcher plus the `cuda_tile`-introduced arms of three otherwise-shared dispatchers (TypeTag, AttributeTag, DebugTag). Only the Op-opcode dispatcher is exclusively `cuda_tile`; the other three carry both builtin and `cuda_tile` cases.

The split:

| Dispatcher | Cases | Owner |
|---|---:|---|
| TypeTag | 19 (`0..18`) | shared `sub_59C710`; tags `12..18` are `cuda_tile`-introduced types |
| AttributeTag | 13 (`1..13`) | shared `sub_59F100`; cuda_tile attrs route through tags `4..13` |
| DebugTag | 7 (`0..6`) | shared `sub_589B90` |
| Op opcode | 110 (`0..109`) | `cuda_tile`-private `sub_5B13D0` |

The private Op-opcode dispatcher is reached from the top-level bytecode-parse-into-scratch path. The three shared dispatchers come in through that same path and through other dialects' readers; they hold no per-dialect state, so the same `Type`, `Attribute`, and `Location` results round-trip through either entry point.

## TypeTag Dispatcher

The TypeTag dispatcher (`sub_59C710`) reads a single VarInt tag and switches on it across a dense `[0..18]` namespace shared between builtin element types and the `cuda_tile`-introduced aggregate types. Tags `0..11` are builtin integer/float element types resolved without any further reads; tags `12..17` are the `cuda_tile` aggregate types (Pointer, Tile, TensorView, PartitionView, Function, Token); tag `18` is the microscale `f8E8M0FNU` element type reachable only as a leaf inside a tile shape. The full byte-for-byte table lives in [Wire-Format Constants — Layer 2: TypeTag Namespace](../../reference/wire-format-constants.md#layer-2--typetag-namespace-sub_59c710) and the dispatcher walk in [MLIR Bytecode Format — Type Tag Dispatch](../../bytecode/mlir-bc-format.md#type-tag-dispatch). The summary the `cuda_tile`-side reader cares about:

| Tag | Type | Payload |
|---:|---|---|
| `0..4` | `i1`, `i8`, `i16`, `i32`, `i64` | none (width fully determined by tag) |
| `5..11` | `f16`, `bf16`, `f32`, `tf32`, `f64`, `f8E4M3FN`, `f8E5M2` | none |
| `12` | PointerType | pointee type-ref + VarInt address space |
| `13` | TileType | element type-ref + VarInt rank + VarInt-encoded shape |
| `14` | TensorViewType | element type-ref + shape + strides |
| `15` | PartitionViewType | element type-ref + shape + dim-map + partition-mode byte |
| `16` | FunctionType | input type-ref list + result type-ref list |
| `17` | TokenType | no payload |
| `18` | `f8E8M0FNU` | parameterless; reachable only as a leaf via the extension path |

TileType is the workhorse of the `cuda_tile`-introduced cluster. Its payload is a TypeRef for the element type, a VarInt rank, and a VarInt-encoded shape. The reader shares its shape parser with TensorViewType and PartitionViewType, keeping the three Tile-family decoders byte-compatible across the shape prefix and letting the writer emit any of them through a single shape-writer helper. PointerType carries a TypeRef for the pointee and a VarInt address space; TokenType is payload-free.

The dispatcher's contract with its caller is uniform: every case path returns a heap-allocated MLIR `Type` on success or `nullptr` on failure. The single-byte return convention lets the bytecode reader push results straight into the Type-section table without rechecking each case.

## Six Enum-Attr Readers

Six attribute kinds defined by `cuda_tile` carry one-of-N enum payloads — Comparison, Overflow, PaddingValue, Rounding, Signedness, Width. Each has its own dedicated reader body, byte-identical to the others except for the embedded enum-value-to-name lookup table. Each body decodes the enum payload, validates it against the table, and emits a per-enum diagnostic on out-of-range values.

The byte-identity is a consequence of the table-driven layout: every reader reads a VarInt, indexes into its embedded `(name, value)` array, and either constructs the enum attribute or emits the diagnostic. Since the only thing that differs between the six readers is the table they consult, a future deduplication could collapse them into a single shared body plus six table pointers without touching the wire format. The shipped binary keeps them separate.

## F8E8M0FNU Tag 18 Fallback

The cuda_tile builder normally emits `f8E4M3FN` and `f8E5M2` as tagged `FloatType`s through the upstream MLIR builtin reader. Those two element types have stock TypeTag values in the upstream Type space and the upstream reader resolves them without ever entering the cuda_tile dispatcher.

The microscale `f8E8M0FNU` element type is the exception. Used by the microscale FP8 attention path, it has no upstream tag, the upstream reader doesn't recognize it, and the cuda_tile-private dispatcher catches it on the fallback path through tag 18. Tag 18 fires only when `f8E8M0FNU` appears as the element type of a `TileType`, `TensorViewType`, or `PartitionViewType` — that is, only as a leaf type inside a tile shape. A standalone `f8E8M0FNU` outside any tile shape cannot be emitted because the cuda_tile builder does not expose it as a top-level type; tag 18 is a leaf-only fallback, not a general-purpose tag.

## Op Opcode Dispatcher

The op-opcode dispatcher reads a VarInt opcode and switches on it. The 110 opcodes cover the 92-op user-visible roster (some opcodes use private fallthrough variants). The full opcode table is reproduced on [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md).

Each opcode arm decodes the operation's expected payload: location reference (optional), result type-refs from the type table, operand value-refs from the value table, attribute-dictionary reference, and any op-specific region bodies. The dispatcher returns the constructed `Operation*` on success or `nullptr` on failure.

## AttrTag Payloads

The cross-dialect attribute dispatcher accepts cuda_tile-owned attributes alongside attributes owned by builtin and other dialects. The cuda_tile attribute families fall into five payload shapes:

| Attribute family | Payload shape |
|---|---|
| Enum attrs (Comparison, Overflow, PaddingValue, Rounding, Signedness, Width) | VarInt enum index; resolved through the dedicated table-driven reader described above. |
| Optimization hint dict | VarInt entry count, then `(architecture-key, value)` pairs where each value is an AttributeRef into the attribute table. |
| Assumption predicate (`div_by`, `bounded`, `same_elements`) | Predicate-kind VarInt, then predicate-specific payload (divisor + optional `every`/`along`, lower/upper bounds, or shape extents respectively). |
| Operand-segment array | Dense i32 array encoded as VarInt rank + N signed VarInts; reused by every op with operand segments. |
| Tile-shape attribute | VarInt rank + N VarInt extents; reused by ops that carry a shape attribute independent of result type. |

The writer mirrors each shape exactly: a reader-writer pair is byte-symmetric, and any new attribute family added to the dialect must come with its own pair.

## Per-Tag Builder Cluster

The 13-case `sub_59F100` dispatcher described in [MLIR Bytecode Format — Self-Contained Attribute Dispatch](../../bytecode/mlir-bc-format.md#self-contained-attribute-dispatch) is the entry point. Each tag's builder consumes a tag-specific payload shape, constructs the corresponding `mlir::Attribute`, and either returns it or emits a per-builder diagnostic and returns `nullptr`. The per-tag wire-format / builder / failure-mode triple is the practical reference a reimplementation needs:

### Tag 1 — `StringAttr` (inline)

- **Wire-format bytes.** SSO (short-string-optimization) length VarInt, then the raw UTF-8 bytes — or, when the length VarInt encodes a string-table index instead, a single VarInt that points into the String section. The discriminator between the two encodings is the low bit of the length VarInt: even → embedded bytes (length = `len >> 1`), odd → string-table index (`index = len >> 1`).
- **Builder.** `sub_59AD90` resolves the string and wraps the result in `StringAttr` via the inline arm of `sub_59F100` itself.
- **Failure modes.** Out-of-range string-table index → `"string index "` concatenated with the offending index. SSO read past the section span → `"failed to read StringAttr."`.

### Tag 2 — `FloatAttr` (inline)

- **Wire-format bytes.** Type-ref VarInt (must resolve to a `FloatType`), then an inline `APFloat` payload — IEEE-754 bit pattern packed by `sub_586200` according to the float type's bit width.
- **Builder.** Inline arm of `sub_59F100`. `sub_58C400` resolves the type ref; `sub_586200` reads the bit pattern; the `sub_4462700`-family float-type-builder casts the pattern into an `APFloat` of the right semantics and wraps it in `FloatAttr`.
- **Failure modes.** Type ref does not resolve to a `FloatType` → `"failed to read valid FloatType for FloatAttr"`. Post-construction cast guard → `"failed to cast parsed attribute to FloatAttr"`.

### Tag 3 — `TypeAttr` (inline)

- **Wire-format bytes.** A single type-ref VarInt.
- **Builder.** Inline arm of `sub_59F100`. `sub_58BDE0` looks up the type and wraps it in `TypeAttr`.
- **Failure modes.** Null type lookup → `"failed to get referenced type for TypeAttr"`.

### Tag 4 — `DenseElementsAttr` int/float variant (`sub_59FB80`)

- **Wire-format bytes.** Shape: VarInt rank + N VarInt extents; element-type ref VarInt (must resolve to an integer or float type); payload: a flat run of element-typed words, total count = product of extents. Integer payload words use VarInt-zig-zag encoding; float payload words use the same IEEE-754 bit pattern as tag 2.
- **Builder.** `sub_59FB80` allocates a result vector via `sub_456A580`, fills it from the payload run, and wraps it in `DenseIntOrFPElementsAttr`.
- **Failure modes.** Element type does not resolve to an int/float type → `"failed to read valid MLIR Type for self-contained DenseElementsAttr"`. Payload word fails to decode → `"array contains unsupported value "` concatenated with the offending VarInt.

### Tag 5 — `DenseElementsAttr` string variant (`sub_59FCD0`)

- **Wire-format bytes.** Shape, then a per-element VarInt count (total element count = product of extents), then that many length-prefixed strings. Each string follows the same SSO rule as tag 1.
- **Builder.** `sub_59FCD0` builds the `DenseStringElementsAttr` from the per-element string vector.
- **Failure modes.** Count prefix read failed → `"failed to read number of string attrs in DenseElementsAttr"`. Per-element string read failed → `"failed to read string in DenseElementsAttr"`.

### Tag 6 — `DivByAttr` (`sub_59FE40`)

- **Wire-format bytes.** Divisor VarInt; flags byte (low two bits: `verify_with_assume`, `predicate_covariance`); on flags bit 1, two extra VarInts `every` and `along`.
- **Builder.** `sub_59FE40` constructs the `DivByAttr` (`div_by` assumption predicate) with the populated divisor and the optional `every`/`along` covariance fields.
- **Failure modes.** Divisor VarInt failed → `"failed to read divisor for DivByAttr"`. Flags byte failed → `"failed to read flags byte for DivByAttr"`. `every` field failed → `"failed to read value for 'every' in DivByAttr"`. `along` field failed → `"failed to read value for 'along' in DivByAttr"`.

### Tags 7 / 8 — `DenseI64ArrayAttr` two layout variants (`sub_59FF60`, `sub_5A0080`)

- **Wire-format bytes.** Both variants encode the same logical content — a length-prefixed i64 array — but with two physical layouts. Variant A (`sub_59FF60`) keeps the array inline next to the dispatch tag (suitable for short arrays). Variant B (`sub_5A0080`) emits a sidecar offset VarInt that points into a shared i64 pool elsewhere in the Constant section (suitable for arrays that recur across many attributes). Both layouts start with a VarInt rank, then either inline or sidecar-resolved i64 values.
- **Builder.** Both arms build the same `DenseI64ArrayAttr`; only the source of the i64 stream differs.
- **Failure modes.** Either layout's bulk value read failed → `"failed to read DenseI64ArrayAttr values."`.

### Tag 9 — `SameElementsAttr` (`sub_5A01A0`)

- **Wire-format bytes.** A nested DenseI64ArrayAttr-shaped payload encoding the shape extents (the "all elements equal" invariant means the attribute carries only the shape and a single splat value, but the wire format reuses the dense-array codec for the shape).
- **Builder.** `sub_5A01A0` constructs the `SameElementsAttr` after decoding the canonical-form payload.
- **Failure modes.** Nested decode failed → `"failed to read DenseI64ArrayAttr for SameElementsAttr"`.

### Tags 10 / 11 / 12 — `BoundedAttr` three discriminator variants (`sub_5A02C0`, `sub_5A03E0`, `sub_5A0500`)

- **Wire-format bytes.** All three variants share a flags byte that selects between lower-only, upper-only, and lower+upper layouts. Variant 0 (tag 10): flags + lower-bound payload. Variant 1 (tag 11): flags + upper-bound payload. Variant 2 (tag 12): flags + lower-bound + upper-bound payload. Each bound is a VarInt-encoded i64.
- **Builder.** Each arm constructs the `BoundedAttr` with the populated bound fields.
- **Failure modes.** Flags byte read failed → `"failed to read flags byte for BoundedAttr"`. Lower bound read failed (variants 0, 2) → `"failed to read lower bound for BoundedAttr"`. Upper bound read failed (variants 1, 2) → `"failed to read upper bound for BoundedAttr"`.

### Tag 13 — `AssumePredicateAttr` (`sub_5A0620`)

- **Wire-format bytes.** A packed predicate header (predicate kind + size) followed by the predicate-specific payload — typically a nested AttributeRef into the attribute table, plus an integer condition word.
- **Builder.** `sub_5A0620` constructs the `AssumePredicateAttr` carrying the predicate body. This is the slot that has no upstream MLIR equivalent and is the most visible piece of the wire-format-breaking divergence.
- **Failure modes.** The packed predicate decode shares its prefix with `DivByAttr` and the `BoundedAttr` family; failures here surface through the same string-table-index, divisor, and bound diagnostics those decoders emit.

The complete cross-dialect numbering — including the side-by-side comparison with upstream MLIR `mlir/Bytecode/BytecodeEnums.h::AttributeTag` — lives in [MLIR Bytecode Format — Self-Contained Attribute Dispatch](../../bytecode/mlir-bc-format.md#self-contained-attribute-dispatch). The default arm of `sub_59F100` rejects every tag outside the `1..13` range with the `"unsupported AttributeTag "` / `" for self-contained attribute"` sentinel; producers that need to remain forward-compatible with the shipped CUDA 13.1 reader must restrict themselves to those 13 tags.

## Encoding Walk: `cuda_tile.addi`

A concrete byte-level walk closes the loop on the format. Consider the operation

```mlir
%c = cuda_tile.addi %a, %b : tile<8 × i32>
```

assuming `%a` and `%b` occupy entries 4 and 5 of the current value table and `tile<8 × i32>` occupies entry 3 of the type table. The opcode for `cuda_tile.addi` is `3` (dispatch case `0x03` in [MLIR Bytecode Format — Operation Opcode Dispatch](../../bytecode/mlir-bc-format.md#operation-opcode-dispatch)). The on-wire encoding contains seven VarInt fields, each fitting in one byte at these table indices:

| Bytes | Field | VarInt | Hex | Decoded |
|---|---|---|---|---|
| 1 | Opcode | `0x03` | `03` | `3` → `cuda_tile.addi` |
| 1 | Location index (signed LEB128) | `0x7f` | `7f` | `-1` → `UnknownLoc` (no `--lineinfo`) |
| 1 | Result-type ref | `0x03` | `03` | `3` → `tile<8 × i32>` |
| 1 | Operand count | `0x02` | `02` | `2` operands |
| 1 | Operand 0 ref | `0x04` | `04` | `4` → `%a` |
| 1 | Operand 1 ref | `0x05` | `05` | `5` → `%b` |
| 1 | Attribute-dict ref | `0x00` | `00` | `0` → empty dict |

The final on-wire byte stream is therefore exactly seven bytes:

```text
03 7f 03 02 04 05 00
```

With `--lineinfo` enabled the `0x7f` sentinel becomes a non-negative `LocAttr` index (one byte for any module with fewer than 64 distinct locations after zig-zag encoding). With a non-empty inline attribute dictionary the trailing `0x00` becomes a VarInt index into the attribute table; if the dict is built from cuda_tile-private enum attributes (`Comparison`, `Overflow`, …), each entry routes through the dedicated table-driven reader documented above before reaching the dispatch in [MLIR Bytecode Format — Self-Contained Attribute Dispatch](../../bytecode/mlir-bc-format.md#self-contained-attribute-dispatch).

All references are positional into per-section tables; the bytecode never embeds operand SSA names or string mnemonics in the operation stream. The mnemonic resides exactly once per operation kind in the dialect's mnemonic table; per-op cost stays constant in the section size, not linear in the mnemonic length.

The corresponding writer emits the same fields in the same order. The shape parser/writer for TileType resolves the result-type reference before the op-opcode dispatcher fires, so the type-table index already exists by the time `cuda_tile.addi`'s opcode arm runs. The result type's element width — `i32` — is recovered through the type-table lookup, not through the op opcode.

## Missing Op 0x6E (atan2)

The op-opcode dispatcher covers 110 cases numbered 0..109. The underlying `cuda_tile` dialect advertises 111 ops to the MLIR registry, so exactly one op has no dispatcher case. The missing op is `cuda_tile.atan2`, removed from this binary as documented in [cuda_tile Overview — Operation Families](overview.md#operation-families).

The wire-level consequence: opcode 110 lands on the default arm of the dispatcher and surfaces the `"unknown or unimplemented opcode: "` diagnostic. A producer that hand-encodes opcode 110 against the next-version opcode space sees its module load fail at that exact opcode. A future-version reader accepts the opcode by adding the 111th case at the end of the dispatch table; this reader has no path to do so.

## Version-13.1 vs 13.2 Compatibility

The bytecode header version check accepts only `13.1.x`. The version-range table is encoded as an inclusive `[13.1.0 .. 13.1.UINT32_MAX]` window, and the predicate `major == 13 && minor == 1` is the only one that yields acceptance.

A 13.2.0 file emitted by a future tileiras would carry additional `TypeTag`, `AttributeTag`, and `DebugTag` values — at minimum a 14th AttributeTag for any new attribute kind, a 19th TypeTag for any new Type subclass, and an 8th DebugTag for any new debug attribute. The 13.1 reader never sees those tag values: it rejects the version block before any section body decoding begins. The forward-incompatibility guarantee is therefore stronger than tag-by-tag rejection — a single header-block check shields the entire downstream pipeline from unknown payloads.

## Cross-References

[MLIR Bytecode Format](../../bytecode/mlir-bc-format.md) is the parent reference for the wire format consumed by this dialect's dispatchers. The four wire-format dispatchers — [Type Tag Dispatch](../../bytecode/mlir-bc-format.md#type-tag-dispatch), [Operation Opcode Dispatch](../../bytecode/mlir-bc-format.md#operation-opcode-dispatch), [Self-Contained Attribute Dispatch](../../bytecode/mlir-bc-format.md#self-contained-attribute-dispatch), and [Debug-Info Attribute Dispatch](../../bytecode/mlir-bc-format.md#debug-info-attribute-dispatch) — together cover every byte the reader looks at after the envelope is accepted. The wire-format-breaking AttrTag numbering and the side-by-side comparison with upstream MLIR live in the third of those sections; the per-builder failure modes documented above expand the same numbering with the payload bytes each builder reads.

[Dialect Bytecode Reader/Writer Status](../../bytecode/dialect-readers-status.md) restricts the parent reference to the dialects that actually ship a reader. The status matrix shows that `cuda_tile` is the only TileIR dialect with a linked bytecode reader and that no TileIR dialect ships a writer, which is why this page only documents the reader half of the contract.

[Types and Attributes — Concrete Types](types-and-attrs.md#concrete-types) documents the underlying `cuda_tile` Type and Attribute subclasses that the TypeTag and AttributeTag dispatchers construct. [Operation Roster](op-roster.md#operation-families) lists the 92 user-visible ops that the opcode dispatcher covers, alongside the small set of private-region ops.
