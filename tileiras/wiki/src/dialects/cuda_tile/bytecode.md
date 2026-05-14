# cuda_tile Bytecode Reader and Writer

## Abstract

The `cuda_tile` dialect ships its own bytecode reader and writer. Neither parses a standalone container — the top-level TileIR envelope is handled by `sub_5838A0` (see [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md)). What `cuda_tile` contributes is four dialect-private dispatchers that the top-level reader hands control to whenever a Type, Attribute, Debug attribute, or Op originates from `cuda_tile`. Two of those dispatchers are shared with the cross-dialect bytecode machinery and are documented end-to-end on the format page; the remaining two are `cuda_tile`-private and are the subject of this page.

The split is worth keeping straight. `sub_59C710` is the cuda_tile `TypeTag` dispatcher with 18 cases for the dialect's own Type subclasses. `sub_5B13D0` is the cuda_tile op-opcode dispatcher with 110 cases for the dialect's own ops. `sub_59F100` and `sub_589B90` are the cross-dialect Attribute and Debug dispatchers — they accept `cuda_tile` attributes alongside everything else, and [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md) covers them.

## Dispatcher Table

| Dispatcher | sub_ADDR | Cases | Owner |
|---|---|---:|---|
| TypeTag | `sub_59C710` | 18 | cuda_tile-private Type tags |
| AttributeTag | `sub_59F100` | 13 | shared with mlir-bc-format.md (cuda_tile attrs route here) |
| DebugTag | `sub_589B90` | 7 | shared with mlir-bc-format.md |
| Op opcode | `sub_5B13D0` | 110 | cuda_tile-private op opcodes |

Both private dispatchers are reached from `sub_57FF40`, the top-level bytecode-parse-into-scratch path. The two shared dispatchers come in through that same path and through other dialects' readers; they hold no per-dialect state, so the same `Attribute` and `Location` results round-trip through either entry point.

## TypeTag Dispatcher (`sub_59C710`)

The cuda_tile TypeTag dispatcher reads a single `uint64_t` tag VarInt and switches on it. Tag `0` is the null sentinel: the reader returns `nullptr` on tag `0` and the writer never emits it. Tags 1..18 cover the Type subclasses introduced by `cuda_tile`, with tag 18 reserved for the microscale element type that only appears as a leaf inside a tile shape.

| Tag | Type | Notes |
|---|---|---|
| 1 | TileType | with shape, element type, padding |
| 2 | TensorViewType | with stride |
| 3 | PartitionViewType | with TileView interface |
| 4 | PointerType | with addrspace |
| 5 | TokenType | scalar, no payload |
| 6 | StringType | |
| 7..17 | (cuda_tile-private extensions) | (varies) |
| 18 | f8E8M0FNU | the microscale element-type tag, reachable via fallback only |

TileType is the workhorse. Its payload is a TypeRef for the element type, a VarInt rank, and a VarInt-encoded shape. The reader shares its shape parser with TensorViewType and PartitionViewType, keeping the three Tile-family decoders byte-compatible across the shape prefix and letting the writer emit any of them through a single shape-writer helper. PointerType carries a TypeRef for the pointee and a VarInt addrspace; TokenType is payload-free. StringType wraps the string table and resolves through the same string-index helper that backs `StringAttr`.

The dispatcher's contract with its caller is uniform: every case path returns a heap-allocated MLIR `Type` on success or `nullptr` on failure. The single-byte return convention lets `sub_57FF40` push results straight into the Type-section table without rechecking each case.

## Six Enum-Attr Readers

Six attribute kinds defined by `cuda_tile` carry one-of-N enum payloads — Comparison, Overflow, PaddingValue, Rounding, Signedness, Width. Each has its own dedicated reader body, 1 212 bytes long, byte-identical to the others except for the embedded enum-value-to-name lookup table. Each body decodes the enum payload, validates it against the table, and emits a per-enum diagnostic on out-of-range values.

The byte-identity is a consequence of the table-driven layout: every reader reads a VarInt, indexes into its embedded `(name, value)` array, and either constructs the enum attribute or emits the diagnostic. Since the only thing that differs between the six readers is the table they consult, a future deduplication could collapse them into a single 1 212-byte body plus six table pointers without touching the wire format. The shipped binary keeps them separate.

## F8E8M0FNU Tag 18 Fallback

The cuda_tile builder normally emits `f8E4M3FN` and `f8E5M2` as tagged `FloatType`s through the upstream MLIR builtin reader. Those two element types have stock TypeTag values in the upstream Type space — `f8E4M3FN` is tag 10 and `f8E5M2` is tag 11 on the [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md) table — and the upstream reader resolves them without ever entering the cuda_tile dispatcher.

The microscale `f8E8M0FNU` element type is the exception. Used by the microscale FP8 attention path, it has no upstream tag, the upstream reader doesn't recognize it, and the cuda_tile-private dispatcher catches it on the fallback path through tag 18. Tag 18 fires only when `f8E8M0FNU` appears as the element type of a `TileType`, `TensorViewType`, or `PartitionViewType` — that is, only as a leaf type inside a tile shape. A standalone `f8E8M0FNU` outside any tile shape can't be emitted because the cuda_tile builder doesn't expose it as a top-level type; tag 18 is a leaf-only fallback, not a general-purpose tag.

## Missing Op 0x6E (atan2)

The op-opcode dispatcher `sub_5B13D0` covers 110 cases numbered 0..109; the full 110-row table is reproduced on [MLIR Bytecode Format](../../bytecode/mlir-bc-format.md). The underlying `cuda_tile` dialect advertises 111 ops to the MLIR registry, so exactly one op has no dispatcher case. The missing op is `cuda_tile.atan2` — removed from this 13.1 binary, as documented in [cuda_tile Overview](overview.md).

The wire-level consequence: opcode `0x6E` (= 110) lands on the default arm of `sub_5B13D0` and surfaces the `"unknown or unimplemented opcode: "` diagnostic. A producer that hand-encodes `0x6E` against the 13.2 opcode space sees its module load fail at that exact opcode. The corresponding 13.2 reader accepts the opcode by adding a 111th case at the end of the dispatch table; the 13.1 reader has no path to do so.

## Version-13.1 vs 13.2 Compatibility

The bytecode header version check (see the [Header Parser section of MLIR Bytecode Format](../../bytecode/mlir-bc-format.md#header-parser-sub_5838a0)) accepts only `13.1.x`. The version-range table at rodata `0x45EBF10` is encoded as an inclusive `[13.1.0 .. 13.1.UINT32_MAX]` window, and the predicate `major == 13 && minor == 1` is the only one that yields acceptance.

A 13.2.0 file emitted by a future tileiras would carry additional `TypeTag`, `AttributeTag`, and `DebugTag` values — at minimum, the 14th AttributeTag for any new attribute kind, a 19th TypeTag for any new Type subclass, and an 8th DebugTag for any new debug attribute. The 13.1 reader never sees those tag values: it rejects the version block before any section body decoding begins. The forward-incompatibility guarantee is therefore stronger than tag-by-tag rejection — a single header-block check shields the entire downstream pipeline from unknown payloads.

## Reimplementation Invariants

- Treat TypeTag `0` as the null sentinel; never emit it, return `nullptr` if it appears.
- Route `f8E4M3FN` and `f8E5M2` through the upstream FloatType tags, not through cuda_tile tag 18.
- Route `f8E8M0FNU` through cuda_tile tag 18 only as a leaf inside a tile-family shape.
- Keep the six enum-attr readers byte-identical except for their enum tables.
- Reserve dispatcher slot for opcode `0x6E`; treat it as `"unknown or unimplemented opcode: "` until `cuda_tile.atan2` is reintroduced.
- Gate every new tag value on a corresponding bump in the bytecode header version field.

## Cross-References

[MLIR Bytecode Format](../../bytecode/mlir-bc-format.md) documents the cross-dialect dispatchers and the bytecode header parser that decides whether this reader is invoked at all. [Types and Attrs](types-and-attrs.md) documents the underlying `cuda_tile` Type and Attribute subclasses that the TypeTag and AttributeTag dispatchers construct. [Op Roster](op-roster.md) lists the 92 user-visible ops that the opcode dispatcher covers, alongside the small set of private-region ops.
