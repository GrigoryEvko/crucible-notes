# TypeID Sentinels and Anchors

## Abstract

Tileiras materialises an MLIR `TypeID` in exactly two ways. Idiom 1 is a 1-byte sentinel in `.bss` whose
*address* is the identity — the byte's value is never read, only its pointer is. Idiom 2 is a Meyers
singleton that lazily interns a `__PRETTY_FUNCTION__`-derived RTTI string the first time the accessor
runs, then caches the resulting `TypeID*` in a qword next to a one-shot guard byte. The two never mix:
every TypeID in the binary is either a static sentinel pointer or a Meyers-cached qword.

Neither idiom touches the Itanium C++ ABI's `typeinfo`/`vtable` machinery. The binary's `.data.rel.ro`
typeinfo block (`0x4FA5242..0x5A2C360`) holds only libstdc++ classes — exceptions, streams, locale
facets — and no MLIR class appears there. This is the architectural reason both idioms exist: MLIR
needs cross-DSO identity for types that the C++ standard's `&typeid(T)` cannot give it (anonymous
namespaces, hidden visibility, statically linked dialects), so it builds its own discriminators on
top of address-taking and string-interning instead. A reimplementation that swaps in `std::type_info*`
will be unable to keep pointer-equality stable across the registered-dialect set.

The distinction is significant. Idiom 1 carries the ABI-frozen identities — dialects, the registered
Type and Attribute subclasses, the upstream MLIR built-ins — whose addresses are link-time constants and
whose registration happens before `main`. Idiom 2 carries identities that come into existence at runtime
as part of an `addInterfaces<>` call, an analysis registration, or a pattern RTTI tag.

## Idiom 1 — Static Pointer-Identity Sentinel

Each dialect, each concrete Type, each concrete Attribute that ships with the binary owns a 1-byte
sentinel in `.bss`. The byte's value is irrelevant; the linker assigns it an address and that
address is the TypeID. Hot dispatch compares `op->kindPtr` (or a Type's vtable slot) against a
sentinel by pointer-identity — one `MOV`+`CMP`, no string compare, no hash lookup.

```c
typedef uint8_t TypeIDSentinel;

extern TypeIDSentinel kCuteLayoutTypeID;          /* &unk_5B49AE0 */
extern TypeIDSentinel kCuteNvgpuSm90MmaTypeID;    /* &unk_5B48E28 */

bool is_cute_layout(Type *t) {
    return t->kind_ptr == &kCuteLayoutTypeID;
}
```

Sentinels do not scatter across the binary — they cluster into a small number of address bands, one
band per owning dialect or category. Three bands carry the weight of Tileiras dispatch.

| Band                          | Owner                                                                | Examples                                             |
|-------------------------------|----------------------------------------------------------------------|------------------------------------------------------|
| `&unk_5B38B[B0..C8]`          | `cuda_tile` dialect Type TypeIDs                                     | `cuda_tile.tile`, `cuda_tile.ptr`, `cuda_tile.tensor_view` |
| `&unk_5B48D[88..F8]` / `5B48E[00..58]` | `cute_nvgpu` concrete Type TypeIDs (27 slots, 8-byte pitch)  | `cute_nvgpu.sm90.mma`, `cute_nvgpu.smem_desc`, `cute_nvgpu.atom.tma_load` |
| `&unk_5B49A[98..B18]`         | `cute` dialect concrete Type TypeIDs (17 slots, 8-byte pitch)        | `cute.layout`, `cute.swizzle`, `cute.tile`           |
| `&unk_5B44E[B8..F8]` / `5B44F[08..FD8]` | `nv_tileas` per-op `opInfo` sentinels (21 ops, 8-byte pitch) | `nv_tileas.tiled_load` @ `5B44ED0`, `nv_tileas.gather_load` @ `5B44FA8`, `nv_tileas.convert_layout` @ `5B44FD8`. Paired kindPtr forms live in `0x5BE3F*` / `0x5BE4*` / `0x5BE5*` — see [Sentinel Sharing And Aliasing](#sentinel-sharing-and-aliasing). |
| `&unk_5B46[D28..F68]`         | `nv_tileaa` per-op FoldRecord sentinels (33 ops)                     | `nv_tileaa.make_memref`, `nv_tileaa.block_tile`      |
| `&unk_5BE5xxx` / `&unk_5BE6xxx` | Upstream MLIR Type and Attribute TypeIDs (built-in dialect)        | `f32` at `&unk_5BE6030`, `f8E4M3FN` at `&unk_5BE60A0` |
| `&unk_5BAADxx`                | Opaque / erased-storage TypeIDs                                      | the i32-blocked-layout-id-1 variant at `&unk_5BAADB8`|

Dialect TypeIDs get their own one-byte slots too: `&unk_5B496B8` for the `cute` dialect,
`&unk_5B482C8` for `cute_nvgpu`, `&unk_5BA8F60` for `LLVM`, `&unk_5BE5908` for `arith`. The
`nv_tile_ir::as::schedule_utils::ScheduleAnalysis` analysis registration at `qword_5B38E78` is the
canonical Idiom-2 example for analyses; the dialect-level Idiom-1 sentinels and the analysis-level
Idiom-2 sentinels coexist in the same `MLIRContext` without colliding because their bands never
overlap.

## Dispatch By Pointer-Identity

Every walker, canonicalizer, and verifier in the binary distinguishes ops the same way: load
`op->kindPtr` from `*(qword*)(op + 48) + 16` and compare it against a list of sentinel addresses.
The classifier `sub_7ACC40` — the mode-classifier for the TileAS layout-assignment pass — is a
representative example.

```c
int classify_load_store_op(Operation *op, ModuleSpec *spec, LayoutCandVec *cands) {
    void *kind = *(void **)(*(qword *)(op + 48) + 16);

    if (kind == &unk_5BE6138) {                       /* null-opinfo (mid-rewrite) */
        if (kind_ptr_is_tiled_atomic_rmw(op))         /* &unk_5B44ED8 via leaf */
            return tail_call_primary_resolver(op, spec, cands);
        if (kind_ptr_is_scatter_store(op))            /* &unk_5B44EF0 via leaf */
            return tail_call_fallback_resolver(op, spec, cands);
        return FAILURE;
    }

    if (kind == &unk_5B44ED0 /* tiled_load   */ ||
        kind == &unk_5B44EC8 /* tiled_store  */)
        return tail_call_primary_resolver(op, spec, cands);

    if (kind == &unk_5B44F90 /* nv_tileas.load     */) return classify_load_inline(op, spec, cands);
    if (kind == &unk_5B44EE0 /* nv_tileas.store    */) return CANONICAL_MODE;
    if (kind == &unk_5B44FA8 /* gather_load        */) return tail_call_gather_resolver(op, spec, cands);

    return FAILURE;
}
```

The entire switch is a sequence of pointer comparisons. No string lives in this function — six
`CMP` instructions on a hot path that runs once per op. Reimplementations must preserve this
property; the address-as-identity model is what makes generic walkers cheap.

## The `&unk_5BE6138` Null-Opinfo Guard

One sentinel earns its own paragraph. `&unk_5BE6138` is the "no properties" guard that ops without
an inline `Properties` payload carry as their kindPtr discriminator during construction and
mid-rewrite. Dispatchers test it first to short-circuit the properties-decode path. It is also the
address an in-flight `RewritePattern` leaves in `op->kindPtr` after wiping the original singleton —
which is why every resolver in the load-store cluster (`sub_7ACC40`, `sub_788BE0`, `sub_7E3440`)
tests for it before falling through to the leaf-predicate helpers `sub_7A9D30` (tiled_atomic_rmw)
and `sub_79DA80` (scatter_store), which read one indirection deeper to recover the original
identity.

Treat the null-opinfo sentinel as a transient state. A walker that observes it on a fully
constructed op outside a rewrite frame should report failure rather than guess the kind.

## Idiom 2 — Meyers-Cached TypeID

When a TypeID is not a link-time constant — primarily Op and Type interfaces attached via
`addInterfaces<>`, analysis types registered through `mlir::AnalysisManager`, and pattern RTTI tags —
the binary falls back to a `{guard:u8, qword:u64}` pair plus a one-shot init function. The factory
`sub_44A6CA0` takes a string ending in `]` (the closing bracket of `__PRETTY_FUNCTION__` captured by
MLIR's `TypeID::get<T>()` trick) and returns the uniqued `TypeID*` for that string. These strings
sit in ordinary `.rodata` literal pools, not in the Itanium typeinfo block, so a binary triage that
scans for `typeinfo for'mlir::...` will find nothing — every MLIR identity string in this binary is
addressable only through the corresponding install-site call.

```c
TypeID get_function_op_interface_typeid(void) {
    static uint8_t  guard = 0;        /* byte_5B37668  */
    static uint64_t cached = 0;       /* qword_5B37670 */

    if (__builtin_expect(guard == 0, 0)) {
        if (__cxa_guard_acquire(&guard) != 0) {
            cached = (uint64_t)sub_44A6CA0("mlir::FunctionOpInterface]", 22);
            __cxa_guard_release(&guard);
        }
    }
    return (TypeID)cached;
}

void install_interface(InterfaceMap *map, TypeID id, void *concept) {
    sub_4492D60(map, id, concept);
}
```

The guard byte sits immediately before the qword in `.bss`, with 8-byte alignment so the qword
stays naturally aligned. The Itanium ABI's `__cxa_guard_acquire` / `__cxa_guard_release` pair makes
initialisation thread-safe; `__builtin_expect(guard == 0, 0)` keeps the steady-state load on the
fast path. After the first call the qword is the TypeID, and the slot behaves exactly like an
Idiom-1 sentinel — except its address is the address of a 64-bit pointer, not a 1-byte tag.

Concrete examples observed in the binary, all matching this exact template:

| Qword slot       | RTTI string (verbatim)                                              | Used by                                                            |
|------------------|---------------------------------------------------------------------|--------------------------------------------------------------------|
| `qword_5B37670`  | `mlir::FunctionOpInterface]`                                        | `ConvertTileFuncToLLVM`                                            |
| `qword_5B37798`  | `mlir::SymbolTable]`                                                | symbol-table analysis lookup                                       |
| `qword_5B38E18`  | `mlir::LoopLikeOpInterface]`                                        | loop pipeliner and licm passes                                     |
| `qword_5B38E78`  | `mlir::nv_tile_ir::as::schedule_utils::ScheduleAnalysis]`           | TileAS scheduler analysis manager                                  |
| `qword_5B44600`  | `mlir::cutlass_ir::cute::LayoutTypeInterface]`                      | every CuTe layout-bearing type                                     |
| `qword_5B44618`  | `mlir::cutlass_ir::cute::ViewTypeInterface]`                        | every CuTe view-bearing type                                       |
| `qword_5B46FF8`  | `mlir::cutlass_ir::cute::MmaAtomTypeInterface]`                     | 9 SM-specific MMA atom installs (SM70..SM120)                      |
| `qword_5B47028`  | `mlir::cutlass_ir::cute::PrintableTypeInterface]`                   | 16+ concrete cute / cute_nvgpu type installs                       |
| `qword_5B47088`  | `mlir::cutlass_ir::cute::DescriptorIteratorTypeInterface]`          | TMA / shared-memory descriptor iteration                           |

The pairs cluster tightly in `.bss` by design: seven cute-interface slots in the band
`0x5B47000..0x5B470D0`, three more in `0x5B44600..0x5B44890`. Each band holds the interface-id
table for a single owning dialect, registered in one initialiser.

## Choosing Between the Two

Idiom 1 covers objects that exist before `main` — dialect TypeIDs, registered Type and Attribute
subclasses, the per-op kindPtr singletons in `&unk_5B44Exx` / `5B44Fxx`. Their addresses are
link-time constants, and the linker packs them into dense 8-byte-pitched bands. Idiom 2 covers
objects whose existence depends on a runtime registration step — interfaces attached after a dialect
loads, analyses keyed by C++ type, pattern RTTI tags. Their identity has to derive from the C++ type
alone, even across translation units, so the binary spells the type name out and uniques the string.

A TypeID never moves between idioms. Once an interface owns a Meyers slot, every install site uses
that slot; once a concrete Type owns a `.bss` sentinel, no caller ever asks the factory for its name.

## Sentinel Sharing And Aliasing

Two cross-dialect aliases deserve a flag. `&unk_5B49B18` serves as the TypeID for both
`cute.ConstrainedInt` and `cute_nvgpu.AtomIType` — the two share an identical inline
`i<N>(<divby M>)?` printer surface, and the binary treats them as one identity. `qword_5B47028`
(PrintableTypeInterface) attaches to every concrete `cute` Type and most `cute_nvgpu` Types — 27+
installs of the same interface against different concrete types. Both patterns are legitimate; both
rely on Idiom-1 and Idiom-2 sentinels being stable for the lifetime of the `MLIRContext`.

The "OperationName ↔ AbstractOperation" split is the other place dispatch sentinels alias. A single
op mnemonic owns two singletons: `&unk_5B44FD8` is the `OperationName.opInfo` (the descriptor passed
to `sub_4461CA0` during dialect registration) for `nv_tileas.convert_layout`, while `&unk_5BE4008`
is the `AbstractOperation` kindPtr that ends up at `*(qword*)(op+48)+16` after the op is uniqued.
Same op identity, two sentinels at different indirection levels — a verifier that wants to recognise
the op needs to pick the right level.

## How to Recognize in a Binary

Idiom 1 is identified by the address band rather than the content. Sentinels cluster densely in
8-byte-pitched runs inside the `.bss` ranges listed above; the only operation performed on a sentinel
is to take its address. A 1-byte object at an 8-byte-aligned offset, never written, whose address
appears as the right-hand side of a `CMP` against an op-header field at `+0x40` or against
`*(qword*)(op + 48) + 16` is an Idiom-1 sentinel.

Idiom 2 is identified by the `{guard:u8, qword:u64}` pair plus a guarded one-shot init body. The
characteristic sequence is `__cxa_guard_acquire(guard) → factory(rtti_string, length) → store
result into qword → __cxa_guard_release(guard)`. The factory `sub_44A6CA0` takes a string ending in
`]` (the closing bracket from `__PRETTY_FUNCTION__`); any call to this factory with a string literal
argument is an Idiom-2 install site.

The null-opinfo sentinel `&unk_5BE6138` is the single most useful cross-cutting fingerprint for
in-flight rewrites. Any walker that loads `*(qword*)(op + 48) + 16` and immediately compares it
against `&unk_5BE6138` is auditing for the mid-rewrite state documented above.

## Consumers

The kindPtr at `*(qword*)(op + 48) + 16` is read by every walker, verifier, canonicaliser, and
pattern matcher in the binary. The walker driver `sub_447FBB0` from
[Operation Layout — Walker Contract](operation-layout.md#walker-contract) dispatches against these sentinels; the pattern fingerprint
map built by `FrozenRewritePatternSet` in
[Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) keys on `OperationName.opInfo`
addresses; the `InterfaceMap` in [Interface Vtables — InterfaceMap Layout](interface-vtables.md#interfacemap-layout) keys on the same TypeID
addresses (the Meyers Idiom-2 ones for interfaces, the Idiom-1 ones for concrete classes).

## Cross-References

[Operation Layout](operation-layout.md) describes the op header where the kindPtr lives. [Interface
VTables](interface-vtables.md) covers the concept tables that the Meyers-cached interface TypeIDs key
into. [Storage Uniquer and ContextImpl](storage-uniquer-and-context-impl.md) documents the registration
machinery that installs both idioms during dialect load. The companion address-sorted reference
[TypeID Sentinel Address Table](../reference/typeid-sentinel-table.md) enumerates every individual
sentinel referenced anywhere in the binary, including the full 213-slot NVVM op slab at
`0x5B8D610..0x5B8DCB8` and the 33-slot `nv_tileaa` FoldRecord band at `0x5B46D28..0x5B46F68`,
neither of which is unpacked here.
