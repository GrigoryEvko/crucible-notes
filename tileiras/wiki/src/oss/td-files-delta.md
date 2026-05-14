# .td Files Delta

## Abstract

The public `cuda_tile` TableGen surface is declared by three files: `Types.td`, `AttrDefs.td`, and `Ops.td`. Tileiras matches almost all of that surface. Three concrete deltas distinguish the tileiras dialect from the upstream declarations:

1. The mnemonic `print_tko` in upstream `Ops.td` is renamed to `print` in tileiras (semantic change at the parser/printer level).
2. The operation `cuda_tile.atan2` declared in upstream `Ops.td` is absent from tileiras — it ships in the 13.2 dialect surface but not in the 13.1 release this wiki covers.
3. The type mnemonic `cuda_tile.string` is added in tileiras with no upstream counterpart, used by the renamed `cuda_tile.print` op as the format-string operand type.

The rest of the surface — all 92 operations beyond the two ops above, the entire type system, all attributes, all interfaces, all predicate helpers — is declaration-for-declaration identical between the public TableGen sources and the recovered tileiras declarations. For a reimplementer, the dialect is a `cuda_tile` 13.1 dialect with one mnemonic rename, one operation suppression, and one added type.

## Types.td

Upstream `Types.td` declares five concrete types and thirteen scalar aliases. Tileiras carries all five concrete types unchanged and all thirteen aliases unchanged. The one delta is the addition of `cuda_tile.string`.

### Concrete Types

| Definition | Mnemonic | Upstream | Tileiras |
| --- | --- | --- | --- |
| `CudaTile_PointerType` | `cuda_tile.ptr` | declared | declared (identical) |
| `CudaTile_TileType` | `cuda_tile.tile` | declared | declared (identical) |
| `CudaTile_TensorViewType` | `cuda_tile.tensor_view` | declared | declared (identical) |
| `CudaTile_PartitionViewType` | `cuda_tile.partition_view` | declared | declared (identical) |
| `CudaTile_TokenType` | `cuda_tile.token` | declared | declared (identical) |
| (tileiras-only) | `cuda_tile.string` | absent | added |

### Added Type: cuda_tile.string

Tileiras adds one type with no upstream equivalent. The declaration shape:

```tablegen
// Tileiras-only (no upstream counterpart in Types.td)
def CudaTile_StringType : CudaTile_Type<"String", "string"> {
  let summary = "A static-length string value used by cuda_tile.print";
  let description = [{
    Carries a UTF-8 byte sequence with a static length. The compiler does not
    expect arbitrary string-valued computations at the cuda_tile layer; this
    type exists so the renamed print op can take a typed format string as an
    operand rather than as an attribute.
  }];
  let parameters = (ins "int64_t":$length);
  let assemblyFormat = "`<` $length `>`";
}
```

The type is parsed and printed as `!cuda_tile.string<N>` where `N` is the static byte length. The op that consumes it is `cuda_tile.print` (described below in the `Ops.td` section). No other tileiras op accepts `cuda_tile.string` operands.

### Scalar Aliases

Both upstream and tileiras declare the same thirteen scalar aliases:

```text
i1, i8, i16, i32, i64
f16, bf16, f32, tf32, f64
f8E4M3FN, f8E5M2, f8E8M0FNU
```

These are predicate-helper aliases used by op verifiers; they are not standalone types. `f8E8M0FNU` is carried through ODS predicate expansion in tileiras but no observed op consumer accepts it as an element type at runtime. Practical element-type validation in both trees ends at `f8E5M2`.

### Predicate Helpers

The predicate helpers (`CudaTile_IntegerType`, `CudaTile_FloatType`, `CudaTile_NumberType`, `CudaTile_TileElementType`, `CudaTile_TileOf<...>`, `CudaTile_RankedTileOf<...>`, `CudaTile_ScalarTileOf<...>`, `CudaTile_IntegerTile`, `CudaTile_BaseFloatTile`, `CudaTile_FloatTile`, `CudaTile_NumberTile`, `CudaTile_PointerTile`) are declared identically in both trees. They are TableGen predicate templates expanded inline at each consuming op's verifier. No runtime helpers exist for them in either tree.

## AttrDefs.td

The attribute surface is identical between upstream and tileiras. All six attribute groups are present declaration-for-declaration. No deltas exist in this file.

### Attribute Groups (Both Trees, Identical)

| Group | Attributes | Upstream / Tileiras |
| --- | --- | --- |
| Arithmetic enums | `signedness`, `overflow`, `rounding`, `comparison_ordering`, `comparison_predicate` | identical declarations |
| Atomics and memory | `AtomicRMWModeAttr`, `MemoryScopeAttr`, `MemoryOrderingSemanticsAttr` | identical declarations |
| Assumption predicates | `DivByAttr`, `SameElementsAttr`, `BoundedAttr` | identical declarations |
| Layout and padding | `OptimizationHintsAttr`, `PaddingValueAttr` | identical declarations |
| Debug-info nodes | `DILocAttr`, `DICompileUnitAttr`, `DIFileAttr`, `DILexicalBlockAttr`, `DISubprogramAttr` | identical declarations |
| Debug-info bases | `DIAttr`, `DINodeAttr`, `DIScopeAttr`, `DILocalScopeAttr` | identical declarations |

`AtomicRMWModeAttr` has ten cases in both trees: `AND`, `OR`, `XOR`, `ADD`, `ADDF`, `MAX`, `MIN`, `UMAX`, `UMIN`, `XCHG`. The three assumption-predicate attributes (`DivByAttr`, `SameElementsAttr`, `BoundedAttr`) all implement `AssumePredicateAttrInterface`, so `cuda_tile.assume` verifies them through the same interface dispatch in both trees. `DivByAttr` uses the same custom assembly format (`div_by<...>`) in both trees.

`OptimizationHintsAttr` accepts the same SM-key vocabulary in both trees: `sm_80`, `sm_86`, `sm_87`, `sm_88`, `sm_89`, `sm_90`, `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121`. The useful keys (`kNumCTAInCGA`, `kAllowTMA`, `kLatency`, `kOccupancy`) are declared identically.

## Ops.td

Upstream `Ops.td` declares 94 operation records — 93 ops plus the `CudaTile_FmaTile` type-constraint pseudo-record. Tileiras carries 92 of those records unchanged, renames one mnemonic, and omits one.

### Operation Census

| Source | Op count | Notes |
| --- | --- | --- |
| Upstream `Ops.td` | 93 ops + 1 type constraint | full 13.2-preview surface |
| Tileiras | 92 ops + 1 type constraint | 13.1 surface |
| Renamed | 1 | `print_tko` → `print` |
| Removed | 1 | `atan2` (13.2-only) |
| Added | 0 | no tileiras-only ops in `Ops.td` |

The 92 carried ops are identical declarations. Listing them would duplicate the OSS source verbatim; instead, the table below shows the two deltas with their exact declaration shapes.

### Delta 1: print_tko → print Rename

Upstream declaration:

```tablegen
// Ops.td (OSS)
def CudaTile_PrintTkoOp : CudaTile_Op<"print_tko", [
    DeclareOpInterfaceMethods<MemoryEffectsOpInterface>
]> {
  let summary = "Token-ordered runtime print operation";
  let arguments = (ins
    CudaTile_TokenType:$inToken,
    StrAttr:$format,
    Variadic<AnyType>:$args
  );
  let results = (outs CudaTile_TokenType:$outToken);
  let assemblyFormat = [{
    $inToken `,` $format ( `,` $args^ )? attr-dict `:` type($args)
  }];
}
```

Tileiras-recovered declaration:

```tablegen
// Tileiras Ops.td (recovered)
def CudaTile_PrintTkoOp : CudaTile_Op<"print", [
    DeclareOpInterfaceMethods<MemoryEffectsOpInterface>
]> {
  let summary = "Token-ordered runtime print operation";
  let arguments = (ins
    CudaTile_TokenType:$inToken,
    CudaTile_StringType:$format,
    Variadic<AnyType>:$args
  );
  let results = (outs CudaTile_TokenType:$outToken);
  let assemblyFormat = [{
    $inToken `,` $format ( `,` $args^ )? attr-dict `:` type($format) ( `,` type($args)^ )?
  }];
}
```

Two changes. First, the mnemonic in the op definition is `print` rather than `print_tko`, so the textual and bytecode forms use `cuda_tile.print` everywhere. The `_tko` suffix that the upstream dialect uses to flag token-ordered ops is dropped from this specific op's mnemonic, though the C++ class name (`PrintTkoOp`) and the token-ordered semantics are preserved.

Second, the `format` operand is typed as `CudaTile_StringType` rather than as a `StrAttr`. This converts the format string from an attribute (compile-time constant) to an operand (SSA value). The motivation is downstream lowering: a typed string operand can be lowered through `cuda_tile.string` materialization to a global symbol holding the format bytes, which is what the NVPTX `vprintf` ABI expects. A `StrAttr`-typed format would force every print site to emit the string bytes inline at the use site.

The renamed op is the only consumer of `cuda_tile.string`. Its absence in the upstream dialect — combined with the upstream `StrAttr` format — explains why upstream `Types.td` does not need a string type at all.

### Delta 2: atan2 Absent

Upstream declaration:

```tablegen
// Ops.td (OSS, 13.2-preview)
def CudaTile_Atan2Op : CudaTile_Op<"atan2", [
    Pure, ElementwiseMappable, SameOperandsAndResultElementType
]> {
  let summary = "Elementwise two-argument arctangent";
  let arguments = (ins
    CudaTile_FloatTile:$y,
    CudaTile_FloatTile:$x
  );
  let results = (outs CudaTile_FloatTile:$result);
  let assemblyFormat = "$y `,` $x attr-dict `:` type($result)";
}
```

Tileiras-recovered declaration:

```tablegen
// Tileiras Ops.td (recovered): no CudaTile_Atan2Op record declared.
```

The operation is absent. A strict tileiras-compatible 13.1 parser rejects `cuda_tile.atan2` because no op record matches the mnemonic. Frontends emitting `cuda_tile` IR that target both the 13.1 and 13.2 dialect surfaces should gate `atan2` emission behind explicit version logic and fall back to a `mul`/`div`/`atan`/`select` sequence for 13.1 targets.

The absence is not an accident of the recovery: it reflects that `atan2` was added to the dialect after the tileiras 13.1 release. The carried-through `f8E8M0FNU` alias mentioned in the `Types.td` section is the inverse case — a declaration that survives in tileiras as a TableGen artifact but has no observed runtime consumer.

### Delta 3: cuda_tile.string Added (Cross-Reference)

The added `cuda_tile.string` type belongs structurally to `Types.td` (see above), but its only consumer is the renamed `cuda_tile.print` op. The two deltas — the string type and the print rename — are coupled: removing one without the other would leave either a typeless format operand or an unused type declaration.

### Renamed-Or-Removed Op Summary

| Upstream definition | Upstream mnemonic | Tileiras mnemonic | Status |
| --- | --- | --- | --- |
| `CudaTile_PrintTkoOp` | `print_tko` | `print` | RENAMED (also: format operand retyped) |
| `CudaTile_Atan2Op` | `atan2` | (absent) | ABSENT (13.2-only) |

The other 92 ops — every arithmetic op, every memory op, every control-flow op, every shape op, every conversion op, every MMA op, every reduction/scan op, every constant/select/diagnostic op — are declaration-for-declaration identical between upstream `Ops.td` and the tileiras-recovered declarations. The producer-side surface for those 92 ops can be lifted directly from upstream without modification.

## Reimplementation Guidance

- Use upstream `Types.td` as the authoritative declaration for all five concrete types and all thirteen aliases. Add one extra `CudaTile_StringType` declaration with the shape shown above.
- Use upstream `AttrDefs.td` verbatim. No deltas.
- Use upstream `Ops.td` for 92 of the 93 ops verbatim. Rename `CudaTile_PrintTkoOp`'s mnemonic from `print_tko` to `print`, and retype its `format` operand from `StrAttr` to `CudaTile_StringType`. Delete the `CudaTile_Atan2Op` record entirely.
- A strict tileiras-compatible 13.1 parser must reject `cuda_tile.atan2` and must accept `cuda_tile.print` while rejecting `cuda_tile.print_tko`. Older bytecode files emitted against the 13.0 dialect surface would have used `print_tko`; the tileiras bytecode reader does not accept that mnemonic — a re-emission against the 13.1 dialect is required.

## Cross-References

- [OSS Comparison Overview](overview.md) — the divergence taxonomy classifying the three deltas.
- [cuda_tile Tree Mapping](cuda-tile-tree-mapping.md) — how the interface declarations in `Interfaces.td` (a fourth public TableGen file outside the three covered here) map between trees.
- [cuda_tile Op Roster](../dialects/cuda_tile/op-roster.md) — the operation surface as exposed inside tileiras, with the deltas applied.
- [cuda_tile Types and Attrs](../dialects/cuda_tile/types-and-attrs.md) — the type and attribute surface inside tileiras, including the added `cuda_tile.string`.
