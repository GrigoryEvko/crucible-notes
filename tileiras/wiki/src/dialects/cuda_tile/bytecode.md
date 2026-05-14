# cuda_tile Bytecode Reader and Writer

## Abstract

The `cuda_tile` dialect ships its own bytecode reader and writer. Neither parses a standalone container — the top-level TileIR envelope is handled by the generic MLIR bytecode header parser documented in [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md). What `cuda_tile` contributes is four dialect-private dispatchers that the top-level reader hands control to whenever a Type, Attribute, Debug attribute, or Op originates from `cuda_tile`. Two of those dispatchers are shared with the cross-dialect bytecode machinery; the remaining two — TypeTag and Op opcode — are `cuda_tile`-private and are the subject of this page.

The split:

| Dispatcher | Cases | Owner |
|---|---:|---|
| TypeTag | 18 | cuda_tile-private Type tags |
| AttributeTag | 13 | shared (cuda_tile attrs route here) |
| DebugTag | 7 | shared |
| Op opcode | 110 | cuda_tile-private op opcodes |

Both private dispatchers are reached from the top-level bytecode-parse-into-scratch path. The two shared dispatchers come in through that same path and through other dialects' readers; they hold no per-dialect state, so the same `Attribute` and `Location` results round-trip through either entry point.

## TypeTag Dispatcher

The cuda_tile TypeTag dispatcher reads a single `uint64_t` tag VarInt and switches on it. Tag `0` is the null sentinel: the reader returns `nullptr` on tag `0` and the writer never emits it. Tags 1..17 cover the Type subclasses introduced by `cuda_tile`; tag 18 is reserved for the microscale element type that only appears as a leaf inside a tile shape.

| Tag | Type | Payload |
|---|---|---|
| 1 | TileType | element type ref + VarInt rank + VarInt-encoded shape |
| 2 | TensorViewType | element type ref + shape + stride |
| 3 | PartitionViewType | tile view interface payload |
| 4 | PointerType | pointee type ref + VarInt address space |
| 5 | TokenType | no payload |
| 6 | StringType | string-table index |
| 7..17 | cuda_tile-private extensions | varies |
| 18 | f8E8M0FNU | parameterless; reachable only as a leaf via fallback |

TileType is the workhorse. Its payload is a TypeRef for the element type, a VarInt rank, and a VarInt-encoded shape. The reader shares its shape parser with TensorViewType and PartitionViewType, keeping the three Tile-family decoders byte-compatible across the shape prefix and letting the writer emit any of them through a single shape-writer helper. PointerType carries a TypeRef for the pointee and a VarInt address space; TokenType is payload-free. StringType wraps the string table and resolves through the same string-index helper that backs `StringAttr`.

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

## Encoding Walk: `cuda_tile.addi`

A concrete byte-level walk closes the loop on the format. Consider the operation

```mlir
%c = cuda_tile.addi %a, %b : tile<8xi32>
```

assuming `%a` and `%b` occupy entries 4 and 5 of the current value table and `tile<8xi32>` occupies entry 3 of the type table. The on-wire encoding contains:

| Bytes | Field | Value |
|---|---|---|
| 1 | Opcode VarInt | The opcode index for `cuda_tile.addi` in the dialect's opcode table. |
| 1 | Location flag | `0` when `--lineinfo` is off; `1` followed by a DebugTag-ref VarInt otherwise. |
| 1 | Result-type ref | VarInt `3` (index into the type table for `tile<8xi32>`). |
| 1 | Operand count VarInt | `2` operands. |
| 1 | Operand 0 ref | VarInt `4` (value-table index for `%a`). |
| 1 | Operand 1 ref | VarInt `5` (value-table index for `%b`). |
| 1 | Attribute-dict ref | VarInt `0` when the dict is empty; otherwise an AttributeRef into the attribute table. |

With line info disabled, an attribute-empty `cuda_tile.addi` therefore encodes in 7 VarInt-bounded bytes (eight if the opcode index needs two VarInt bytes). All references are positional into per-section tables; the bytecode never embeds operand SSA names or string mnemonics in the operation stream. The mnemonic resides exactly once per operation kind in the dialect's mnemonic table; per-op cost stays constant in the section size, not linear in the mnemonic length.

The corresponding writer emits the same fields in the same order. The shape parser/writer for TileType resolves the result-type reference before the op-opcode dispatcher fires, so the type-table index already exists by the time `cuda_tile.addi`'s opcode arm runs. The result type's element width — `i32` — is recovered through the type-table lookup, not through the op opcode.

## Missing Op 0x6E (atan2)

The op-opcode dispatcher covers 110 cases numbered 0..109. The underlying `cuda_tile` dialect advertises 111 ops to the MLIR registry, so exactly one op has no dispatcher case. The missing op is `cuda_tile.atan2`, removed from this binary as documented in [cuda_tile Overview — Operation Families](overview.md#operation-families).

The wire-level consequence: opcode 110 lands on the default arm of the dispatcher and surfaces the `"unknown or unimplemented opcode: "` diagnostic. A producer that hand-encodes opcode 110 against the next-version opcode space sees its module load fail at that exact opcode. A future-version reader accepts the opcode by adding the 111th case at the end of the dispatch table; this reader has no path to do so.

## Version-13.1 vs 13.2 Compatibility

The bytecode header version check accepts only `13.1.x`. The version-range table is encoded as an inclusive `[13.1.0 .. 13.1.UINT32_MAX]` window, and the predicate `major == 13 && minor == 1` is the only one that yields acceptance.

A 13.2.0 file emitted by a future tileiras would carry additional `TypeTag`, `AttributeTag`, and `DebugTag` values — at minimum a 14th AttributeTag for any new attribute kind, a 19th TypeTag for any new Type subclass, and an 8th DebugTag for any new debug attribute. The 13.1 reader never sees those tag values: it rejects the version block before any section body decoding begins. The forward-incompatibility guarantee is therefore stronger than tag-by-tag rejection — a single header-block check shields the entire downstream pipeline from unknown payloads.

## Cross-References

[MLIR Bytecode Format](../../bytecode/mlir-bc-format.md) documents the cross-dialect dispatchers and the bytecode header parser that decides whether this reader is invoked at all. [Types and Attributes — Concrete Types](types-and-attrs.md#concrete-types) documents the underlying `cuda_tile` Type and Attribute subclasses that the TypeTag and AttributeTag dispatchers construct. [Operation Roster](op-roster.md#operation-families) lists the 92 user-visible ops that the opcode dispatcher covers, alongside the small set of private-region ops.
