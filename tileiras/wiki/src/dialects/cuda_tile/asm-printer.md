# cuda_tile Assembly Printer

## Abstract

`cuda_tile` installs a textual assembly surface so public TileIR reads like a source-level dialect instead of generic quoted MLIR. Most operations use declarative assembly formats. Custom printing is concentrated around token-ordered memory operations, aggregate regions, constant result-name hints, enum spelling, and cuTe layout attributes carried by tile metadata. This page documents the behavior a printer and parser must preserve.

The dialect installs the `CudaTileOpAsmInterface` concept on its operation models. Three consumers matter: a 5-path result-name algorithm on `cuda_tile.constant`, five custom `*_tko` printer callbacks for token-producing memory and atomic ops, and a set of seven namespace-getter trampolines that route concept-model interface calls back to the dialect's `"cuda_tile"` `StringRef`.

## Printing Model

The printer should prefer concise, typed operation syntax:

```mlir
%tok0 = cuda_tile.make_token : !cuda_tile.token
%v, %tok1 = cuda_tile.load_view_tko acquire gpu %view[%i, %j] token=%tok0
    : !cuda_tile.partition_view<...> -> !cuda_tile.tile<...>, !cuda_tile.token
```

The general rules are:

- print the `cuda_tile.` prefix at dialect boundaries;
- allow the default dialect shortcut inside `cuda_tile` regions when supported;
- print inherent attributes in the structured syntax and elide them from the
  trailing attribute dictionary;
- keep memory ordering, memory scope, token operands, masks, and result token
  types visible for token-ordered operations;
- use SSA result-name hints only as hints, never as semantic identifiers.

## Token-Ordered Memory Syntax

The `_tko` family carries the most important custom syntax — memory ordering
and token edges ride the operation, not a trailing attribute dictionary. A
reimplementation may tweak spacing, but every field must remain visible and
parseable.

```mlir
%old, %tok1 = cuda_tile.atomic_cas_tko acq_rel gpu %ptrs, %cmp, %val, %mask
    token=%tok0 : !cuda_tile.tile<ptr>, !cuda_tile.tile<i32>, !cuda_tile.tile<i1>
    -> !cuda_tile.tile<i32>, !cuda_tile.token

%old, %tok1 = cuda_tile.atomic_rmw_tko acquire gpu add %ptrs, %val, %mask
    token=%tok0 : !cuda_tile.tile<ptr>, !cuda_tile.tile<i32>, !cuda_tile.tile<i1>
    -> !cuda_tile.tile<i32>, !cuda_tile.token

%tok1 = cuda_tile.store_view_tko release gpu %view[%i, %j], %value, %mask
    token=%tok0 : !cuda_tile.partition_view<...>, !cuda_tile.tile<f32>
    -> !cuda_tile.token
```

`load_ptr_tko` and `store_ptr_tko` mirror the view forms but take pointer
tiles instead of view-plus-indices. `make_token` and `join_tokens` use the
declarative formats — their syntax is pure token construction and merging.

## Attribute Elision

Custom printers must not duplicate inherent attributes. When ordering, scope,
mode, token segment sizes, or optimization hints already appear in the
operation's structured syntax, drop them from the trailing attribute
dictionary.

```c
void print_optional_attrs(OpAsmPrinter *printer, Operation op) {
    Set<StringRef> elided = {
        "memory_ordering_semantics",
        "memory_scope",
        "operandSegmentSizes",
        "mode",
        "optimization_hints",
    };

    printer->print_optional_attr_dict(op.attributes, elided);
}
```

The parser reconstructs the same attributes from the structured fields so
round-tripping loses nothing.

## Enum Spellings

Enum attributes print as short stable keywords:

| Attribute | Example spellings |
|---|---|
| Memory ordering | `weak`, `relaxed`, `acquire`, `release`, `acq_rel`, `seq_cst` |
| Memory scope | `tl_blk`, `cluster`, `gpu`, `sys` |
| Atomic RMW mode | `add`, `addf`, `addu`, `and`, `or`, `xor`, `xchg`, `min`, `umin`, `max`, `umax`, `cmpxchg` |
| Signedness | `signed`, `unsigned` |
| Rounding | `nearest_even`, `zero`, `negative_inf`, `positive_inf`, `approx`, `full` |

Printers emit canonical spellings even when parsers accept legacy aliases.

## cuTe Layout Attributes

`cuda_tile` may carry cuTe layout attributes on tile metadata. Those attributes
use cuTe's basis-vector notation: a stride and dimension pair prints as
`N@dim`, and tuples print as parenthesized comma-separated lists.

```mlir
#cute.layout<(1@0, 16@1)>
#cute.shape<(128@0, 64@1)>
#cute.stride<(1@0, 128@1)>
```

`cuda_tile` treats these as attributes, not first-class cuTe types. When
layout parsing fails, report the malformed parameter — never fall back to an
opaque string.

## SSA Result Names and the 5-Path Constant Algorithm

SSA names are not semantic, but good hints make dumps readable. `cuda_tile.constant` is the most visible case. When the IR printer formats a constant op, it asks the op for a preferred name through `getAsmResultNames`. The cuda_tile implementation walks five cases keyed on the constant's value and writes the hint into a `SmallString<32>` (32-byte inline buffer plus heap spillover, the canonical libc++ `SmallString` layout with the capacity marker at offset `+0`).

| Path | Trigger | Result name |
|---|---|---|
| 1 | value is boolean true (`i1` constant `1`) | `"true"` |
| 2 | value is boolean false (`i1` constant `0`) | `"false"` |
| 3 | value is a floating-point NaN, by any bit pattern matching the type's NaN encoding | `"cst_NaN"` |
| 4 | value is `i1` and is neither 0 nor 1 (poison, defensive only) | `"cst_i1"` |
| 5 | default: any integer prints `"cst_<int>"` with the decimal value; non-integer fallback prints `"cst"` and lets the IR printer pick a numeric suffix | `"cst_42"`, `"cst"` |

Path 4 never fires on well-formed IR, but the printer handles it anyway so a poisoned `i1` constant still produces a deterministic dump.

```c
StringRef constant_result_name(ConstantOp op, SmallString<32> *scratch) {
    if (is_i1_splat(op.value, true)) {
        return "true";
    }

    if (is_i1_splat(op.value, false)) {
        return "false";
    }

    if (is_splat_float_nan(op.value)) {
        return "cst_NaN";
    }

    if (is_i1_dense(op.value)) {
        return "cst_i1";
    }

    if (Optional<int64_t> value = splat_integer(op.value)) {
        scratch->assign(format("cst_%lld", value.value));
        return scratch->view();
    }

    return "cst";
}
```

Other useful hints include `view`, `bcast`, `red`, and `mm` for view construction, broadcast, reductions, and matrix multiply results. A parser must never rely on those names — they exist only for humans reading dumps.

## Custom `*_tko` Printer Callbacks

Five cuda_tile operations carry custom AsmPrinter callbacks instead of declarative assembly. The `_tko` suffix marks ops that produce a `cuda_tile.token`; the callbacks emit specialised syntax with structured fields and a tight elided-attribute set, keeping the trailing attribute dictionary compact.

| Address | Op | Elided attrs |
|---|---|---|
| `0x664920` | `cuda_tile.load_view_tko` | `addr_space`, `align`, `token` |
| `0x664E90` | `cuda_tile.store_view_tko` | `addr_space`, `align`, `token` |
| `0x66C1A0` | `cuda_tile.atomic_cas_tko` | `success_ordering`, `failure_ordering`, `token` |
| `0x66C810` | `cuda_tile.atomic_rmw_tko` | `ordering`, `kind`, `token` |
| `0x677250` | `cuda_tile.make_token` | (none — emits the token name directly) |

The elided lists suppress the verbose attribute dictionaries the default printer would otherwise emit. Structured fields in each printer (memory ordering keyword, memory scope keyword, RMW kind keyword, token operand) carry the same information in keyword form, so round-tripping through the parser rebuilds every elided attribute.

## OpAsmPrinter Vtable Slots

Custom printers touch only four `OpAsmPrinter` vtable slots:

| Offset | Slot | Notes |
|---|---|---|
| `+16` | `getStream` | Returns the raw `raw_ostream` being written to |
| `+48` | `printAttr` | Prints a single `Attribute` |
| `+128` | `printOperandList` | Prints an SSA-value list with formatting |
| `+192` | `printOptionalAttrDict` | Prints the attribute dictionary, optionally eliding listed keys |

A reimplementation that targets the same binary ABI keeps these offsets stable. The `printOptionalAttrDict` slot at `+192` consumes every elided-attr list in the `*_tko` table above.

## Namespace-Getter Trampoline Quad

Seven 8-byte trampolines sit between `0x61F5D0` and `0x61FB20`, each `ret`-tail-returning the cached pointer to the cuda_tile dialect's `"cuda_tile"` `StringRef`. They route concept-model interface calls to a single namespace string:

| Address | Concept |
|---|---|
| `0x61F5D0` | `Dialect::getDialectNamespace` |
| `0x61F640` | `InlinerInterface::getDialectNamespace` |
| `0x61F6B0` | `OpAsmInterface::getDialectNamespace` |
| `0x61F720` | `BytecodeInterface::getDialectNamespace` |
| `0x61F790` | `FoldInterface::getDialectNamespace` |
| `0x61F800` | `TypeInfererInterface::getDialectNamespace` |
| `0x61FB20` | `MemoryEffectsInterface::getDialectNamespace` |

Each trampoline is seven bytes — a single `ret` after the cached pointer load. Seven distinct copies exist because every concept-model instantiation generates its own `getDialectNamespace`; the linker deduplicates only to these seven slots because the model classes are template-instantiated separately, each keeping an independent code address. Tail-calling all of them into one shared body would shrink the binary, but the existing layout matches what an MLIR concept-model emitter produces when no explicit deduplication is applied.

## Region Printing

Structured control-flow printers keep region layout readable:

```mlir
%r = cuda_tile.if %cond -> (!cuda_tile.tile<...>) {
^then:
    cuda_tile.yield %a : !cuda_tile.tile<...>
} else {
^else:
    cuda_tile.yield %b : !cuda_tile.tile<...>
}
```

`for`, `loop`, `reduce`, and `scan` likewise name body blocks and print
yield types explicitly enough that verifier failures stay easy to diagnose.

## Dense Constant Debug Output

Some debug paths print dense element payloads as comma-separated values
without full MLIR type or shape wrappers. That format is for logs and replay
debugging, not persistent IR. Round-tripable IR always uses the normal
`cuda_tile.constant` operation and its typed attribute.

## Invariants

- Memory ordering, memory scope, atomic mode, masks, token operands, and token result types remain visible for `_tko` operations.
- Attributes printed in custom syntax are elided from the optional attribute dictionary and reconstructed by the parser; the elided sets match the per-op lists in the `*_tko` printer table.
- The constant result-name algorithm preserves its 5-path ordering — `"true"`, `"false"`, `"cst_NaN"`, `"cst_i1"`, then the integer/default fallback — so dumps stay stable across rebuilds.
- The `OpAsmPrinter` vtable slots at `+16`, `+48`, `+128`, and `+192` are the only entry points the custom printers use.
- All seven `getDialectNamespace` trampolines return the same `"cuda_tile"` `StringRef`.
- SSA result-name hints are deterministic and non-semantic.
- cuTe layout values are parsed and printed as attributes.
- Debug dense-element output is not treated as stable assembly.

## Reimplementation Checklist

1. Use declarative assembly formats for ordinary arithmetic, shape, conversion, and control-flow operations.
2. Implement the five custom `*_tko` printers — `load_view_tko`, `store_view_tko`, `atomic_cas_tko`, `atomic_rmw_tko`, `make_token` — with the per-op elided-attribute lists.
3. Implement the 5-path constant result-name algorithm with a `SmallString<32>` scratch buffer for the integer fallback.
4. Centralize enum keyword parsing and printing.
5. Keep custom attribute elision symmetric between printer and parser.
6. Route every concept-model `getDialectNamespace` call to the single cached `"cuda_tile"` `StringRef`.
7. Keep log-only dense payload printers separate from textual IR round-trip printers.
