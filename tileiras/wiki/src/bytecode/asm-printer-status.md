# Dialect Asm-Printer Status

## Abstract

Textual MLIR assembly is the only inspection path for the non-input dialects
inside `tileiras`, since this binary never serializes them as MLIR bytecode.
The printer surface is intentionally uneven — dialects near the user boundary
invest in custom spelling and aliases, while short-lived pipeline dialects fall
back on MLIR's generic operation printer.

The practical rule: expect polished textual forms for `cuda_tile`, `cute`,
and `cute_nvgpu`; expect generic MLIR for most `nv_tileaa`, `nv_tileas`,
and `cutlass` operations, with a few aliases or SSA-name hints sprinkled in
to keep dumps readable.

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

`cute_nvgpu` ships a full type parser/printer for architecture-specific MMA,
copy, TMA, shared-memory descriptor, and tensor-memory atoms. Its aliases
exist to keep large dumps readable:

```c
const char *alias_cute_nvgpu_type(Type t) {
    if (is_memref_type(t))
        return format("memref_%s_%u", element_name(t), rank(t));
    if (is_copy_atom(t))
        return format("copy_%s", copy_atom_family(t));
    if (is_mma_atom(t))
        return format("mma_%s_%s_%s_%ux%ux%u",
                      elem_a(t), elem_b(t), elem_c(t), m(t), n(t), k(t));
    return NULL;
}
```

The MMA alias exposes the element-type triple and tile shape without expanding
the full atom type body.

### `cutlass` — generic spelling

`cutlass` leaves textual assembly to the framework on purpose. Its operations
carry registered attributes and opaque types, so the generic ODS printer
produces sufficient IR without dialect-wide aliases or per-op pretty names.

## Reimplementation Guidance

1. Implement `cuda_tile` constant result naming first; it gives the largest
   readability improvement in input and early pipeline dumps.
2. Keep `nv_tileaa.load` result naming stable because downstream docs and tests
   can rely on `result` and `resultMemToken`.
3. Preserve the `cute` rule that type-like syntax is parsed as attributes.
4. Implement `cute_nvgpu` aliases for memrefs, copy atoms, and MMA atoms before
   expanding every individual atom printer.
5. Let `cutlass` and most `nv_tileas` operations use generic MLIR printing
   unless a human-facing dump becomes ambiguous.
