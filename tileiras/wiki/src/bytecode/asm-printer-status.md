# Dialect Asm-Printer Status

## Abstract

An MLIR asm printer turns in-memory operations, attributes, types, and values into the textual `.mlir` form used for human inspection and crash dumps. Each dialect contributes to the result through three mechanisms: a `printType` / `parseType` pair that handles dialect-specific type bodies, a `printAttribute` / `parseAttribute` pair that handles dialect-specific attribute bodies, and an `OpAsmDialectInterface` that supplies short readable aliases plus per-value SSA-name hints. Per-operation pretty-printing is layered on top through `OpAsmOpInterface` hooks attached at ODS time. When a dialect leaves a hook unimplemented, MLIR's default trampoline takes over and emits the verbose generic form — `"dialect.op"(operands) : (types) -> types` for operations, `!ns<...stored_payload...>` for types, `#ns<...stored_payload...>` for attributes.

Textual assembly is the only inspection path for the non-input dialects inside `tileiras`, since this binary never serializes them as bytecode. The printer surface is intentionally uneven: dialects near the user boundary invest in custom spelling and aliases, while short-lived pipeline dialects fall back on the generic printer. Expect polished textual forms for `cuda_tile`, `cute`, and `cute_nvgpu`; expect generic MLIR for most `nv_tileaa`, `nv_tileas`, and `cutlass` operations, with a few aliases or SSA-name hints sprinkled in to keep large dumps legible.

## Alias resolution

`OpAsmDialectInterface` exposes two virtual hooks the printer consults before falling back on the generic form: `getAlias(Type, raw_ostream&)` and `getAlias(Attribute, raw_ostream&)`. Each hook returns an `AliasResult` — `NoAlias`, `OverridableAlias`, or `FinalAlias` — and, when the result is not `NoAlias`, writes the alias name into the stream. The printer queries every loaded dialect in registration order; the first non-`NoAlias` answer wins. `FinalAlias` short-circuits subsequent dialects; `OverridableAlias` permits a later dialect to refine the name.

```c
bool emit_type_with_alias(AsmPrinter *p, Type t) {
    SmallString<32> name;
    raw_svector_ostream os(name);
    for (Dialect *d : p->context()->loadedDialects()) {
        OpAsmDialectInterface *iface = d->getRegisteredInterface<OpAsmDialectInterface>();
        if (!iface) continue;
        AliasResult r = iface->getAlias(t, os);
        if (r == AliasResult::NoAlias) { name.clear(); continue; }
        register_alias_decl(p, t, name);   /* emit `!name = type ...` at top of module */
        p->os() << "!" << name;
        return true;
    }
    return false;  /* caller falls back to generic !ns<...> form */
}
```

When `emit_type_with_alias` returns false the printer writes the generic form — `!ns<storage-blob>` for parametric types, the registered mnemonic plus storage for ODS-generated types. Attribute printing follows the same shape with `#` in place of `!`.

## Per-dialect feature matrix

The table below summarizes which textual-IR hook each dialect installs. "ODS-only" means the slot is wired by the TableGen-generated dialect registration to MLIR's default trampoline (which reads the registered mnemonic/storage and emits the canonical form). "stub" means the slot is patched to a body that either does nothing or emits a `parsing in dialect '<ns>' is disabled` diagnostic. "real" means a hand-written dispatcher of non-trivial size.

| Dialect       | printType                                  | parseType                                  | printAttribute                              | parseAttribute                              | OpAsmDialectInterface                    | per-op OpAsmOpInterface |
|---------------|--------------------------------------------|--------------------------------------------|---------------------------------------------|---------------------------------------------|------------------------------------------|-------------------------|
| `cuda_tile`   | ODS/default                                | ODS/default                                | ODS/default                                 | ODS/default                                 | full aliasing and constant names | yes, including constants and selected TKO ops |
| `nv_tileaa`   | ODS/default                                | ODS/default                                | ODS/default                                 | ODS/default                                 | absent | yes on six operations |
| `nv_tileas`   | ODS/default                                | ODS/default                                | ODS/default                                 | ODS/default                                 | attribute and type aliases | none |
| `cute`        | handled through printable type interfaces | disabled                                   | ODS/default for registered attributes        | real keyword parser                         | absent | none |
| `cute_nvgpu`  | real type printer                          | real type parser                           | empty/default                               | disabled                                    | type aliases | none |
| `cutlass`     | empty/default                              | disabled                                   | empty/default                               | disabled                                    | absent | none |

### `cuda_tile` — user-facing input syntax

`cuda_tile` has the richest textual surface. Constants receive stable SSA-name
hints — `cst`, `true`, `false`, `cst_NaN`, `cst_<int>` — that keep debug dumps
legible. Selected TKO load/store and atomic operations carry hand-written
printers and parsers instead of generic MLIR spelling.

### `nv_tileaa` — generic dialect with a few name hints

`nv_tileaa` installs no dialect-wide asm aliases — most operations print in
generic MLIR form. Six operations attach per-op asm interfaces, and the only
pretty-name behavior worth knowing lives on `nv_tileaa.load`: the value result
is named `result`, and the optional memory-token result is named `resultMemToken`.

### `nv_tileas` — aliases for scheduling concepts

`nv_tileas` falls back on generic operation printing for most ops but ships
useful dialect-level aliases for scheduling attributes and types. Attribute
aliases cover memory-space layouts, copy atoms, reduction atoms, MMA atoms,
and resource requirements. Type aliases cover `pipeline` and role-qualified
iterator types such as producer and consumer iterators.

### `cute` — attributes are the serialized type surface

`cute` disables standalone type parsing. Its canonical textual form represents
types as `#cute.<keyword>` attributes rather than `!cute.<keyword>` types. The
attribute parser recognizes layout-algebra terms — `coord`, `stride`, `shape`,
`tile`, `swizzle`, `layout`, `composed_layout`, `ptr`, `memref`, `coord_tensor` —
along with constrained integer forms.

### `cute_nvgpu` — architecture atom spelling

`cute_nvgpu` ships a full type parser/printer for architecture-specific MMA, copy, TMA, shared-memory descriptor, and tensor-memory atoms. Its `OpAsmDialectInterface` aliases collapse the otherwise unwieldy stored type body — element-type triples, operand orderings, instruction shapes, swizzle patterns — into a short label that survives in 10 000-line dumps.

The alias hook is a discriminated dispatch on the type's class id. Each branch builds the alias name from the type's own accessors rather than from the encoded storage body, so the alias survives parameter reordering inside the storage struct.

```c
AliasResult cute_nvgpu_type_alias(Type t, raw_ostream &os) {
    if (auto m = dyn_cast<MemRefAtomType>(t)) {
        os << "memref_" << element_type_keyword(m.getElementType())
           << "_"      << m.getRank();
        return AliasResult::OverridableAlias;
    }
    if (auto c = dyn_cast<CopyAtomType>(t)) {
        os << "Cp(" << element_type_keyword(c.getElementType()) << ","
                    << c.getShape().M << "x" << c.getShape().N << ","
                    << layout_keyword(c.getLayout()) << ")";
        return AliasResult::FinalAlias;
    }
    if (auto m = dyn_cast<MmaAtomType>(t)) {
        os << "Mma(m" << m.getInstShape().M
           <<   "n"  << m.getInstShape().N
           <<   "k"  << m.getInstShape().K << ","
           << element_type_keyword(m.getElementTypeA()) << ","
           << layout_keyword(m.getLayoutA()) << ","
           << layout_keyword(m.getLayoutB()) << ")";
        return AliasResult::FinalAlias;
    }
    if (isa<TmaDescriptorType, SharedDescriptorType, TensorMemoryType>(t))
        return tma_family_alias(t, os);
    return AliasResult::NoAlias;
}
```

The MMA alias exposes the instruction shape and element-type/layout triple without expanding the full atom storage body. A printed `cute_nvgpu` dump that would otherwise contain `!cute_nvgpu.mma_atom<inst_shape = <m = 16, n = 8, k = 16>, a = <element = f16, layout = row>, b = <element = f16, layout = col>, c = <element = f32>>` instead reads `!Mma(m16n8k16,f16,row,col)`. The 23 typed copy and MMA atoms collapse to one short label each.

### `cutlass` — generic spelling

`cutlass` leaves textual assembly to the framework on purpose. Its operations
carry registered attributes and opaque types, so the generic ODS printer
produces sufficient IR without dialect-wide aliases or per-op pretty names.

## Practical rules

A reimplementer who wants compatible textual dumps starts at the `cuda_tile` boundary because every input module passes through it: stable constant SSA hints (`cst`, `true`, `false`, `cst_NaN`, `cst_<int>`) and the hand-written TKO load/store/atomic printers carry the largest readability payoff. The `nv_tileaa.load` result names `result` and `resultMemToken` are fixed contract — downstream regression dumps reference them. The `cute` invariant that all type-like syntax appears as `#cute.<keyword>` attributes rather than `!cute.<keyword>` types must be preserved because the textual parser refuses standalone `cute` types outright. The `cute_nvgpu` aliases for memref, copy, and MMA atoms cover the bulk of the alias-driven readability gain; per-atom custom printers can come later. Operations in `cutlass` and most of `nv_tileas` ship without aliases on purpose — the generic ODS form is precise enough and avoids divergence between the textual dialect description and the dialect's stored representation.

## Cross-references

The companion page [Dialect Bytecode Reader/Writer Status](dialect-readers-status.md) covers the input-side wire format that produces the IR these printers later spell. Layout-algebra spelling for the `cute` attribute family is documented in the dialect's own reference; tensor-memory and TMA atom encodings are described under the `cute_nvgpu` dialect reference. The architectural rationale for keeping intermediate dialects on the generic textual form lives in the pipeline overview.
