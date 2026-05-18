# TypeID Construction Idioms

## Abstract

`mlir::TypeID` is MLIR's runtime identity tag for compiler-internal RTTI. Conceptually it is a
non-null `const void *` that is unique per C++ class and stable across the life of the
`MLIRContext`. The natural C++ implementation — `&typeid(T)` from the Itanium ABI — is **not
viable** for an MLIR-style framework, and tileiras's binary makes the reason visible in its
layout. The Itanium `typeinfo` blocks in `.data.rel.ro` (`0x4FA5242..0x5A2C360`) hold libstdc++
types only — exceptions, locale facets, stream buffers — and no MLIR class appears there. Every
`Dialect`, `Op`, `Type`, `Attribute`, and `Interface` that the binary dispatches on builds its
TypeID through one of two idioms that sidestep `typeid` entirely.

This page is the canonical description of those two idioms in isolation. The companion page
[TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) covers where in `.bss` the two
idioms land in the tileiras image and how the dispatcher consumes them; the address-band reference
table is [TypeID Sentinel Address Table](../reference/typeid-sentinel-table.md). Wave 22B's
finding — that MLIR uses two distinct idioms because `&typeid(T)` is unusable across DSO
boundaries under hidden visibility — is the architectural justification this page expands on.

## Why `&typeid(T)` Cannot Be the Identity

The Itanium C++ ABI specifies one `type_info` object per type per program. Cross-DSO uniqueness
relies on weak symbols emitted into `.data.rel.ro` with `STB_WEAK` binding and `STV_DEFAULT`
visibility, so the dynamic linker can merge duplicates from different shared objects into a
single instance at load time. MLIR's static-linking and packaging discipline breaks this
guarantee on three independent axes.

- **Hidden visibility.** Tileiras's dialect libraries are compiled `-fvisibility=hidden`. Hidden
  `type_info` symbols cannot participate in cross-DSO merging — each shared object that
  instantiates the same template gets its own private copy, and `&typeid(T)` differs between
  callers. The `extern template` discipline upstream LLVM uses for `llvm::cl::opt` doesn't help
  here because the underlying `type_info` symbol still ends up hidden.
- **Anonymous namespaces.** Several MLIR base classes (`OpInterface<...>` template instantiations,
  `Trait<...>` mixins, generated `Storage` classes) appear inside anonymous namespaces in
  generated TableGen code. The Itanium ABI gives anonymous-namespace types internal linkage, so
  `&typeid(T)` is per-translation-unit by definition — there is no merging step the linker could
  perform even if visibility were `default`.
- **Static linkage.** When dialects are statically linked into a host (which tileiras does for
  the bundled CUDA toolchain) the entire `.data.rel.ro` typeinfo block is duplicated per linked
  archive, and only the linker's `--gc-sections` heuristics decide which copy survives. A TypeID
  derived from `&typeid(T)` would silently differ depending on whether a dialect is loaded
  through a plugin or compiled in.

The result is that an MLIR-style framework needs its own discriminator. The two idioms below are
how the framework — and tileiras's MLIR vendor branch — synthesise one.

## Idiom 1 — Per-Class Static Sentinel

The first idiom builds the TypeID out of the **address of a per-class static storage object**.
The object's value is never read. Only its address matters, and the address is the TypeID.

```c
/* Per-class declaration (one of these exists for every concrete dialect / op /
 * type / attribute that the binary dispatches on). */
typedef struct {
    char id;            /* one byte in .bss, value never read */
} ClassNameTypeIDStorage;

static ClassNameTypeIDStorage kClassNameTypeIDStorage;   /* .bss, 1 byte */

/* The TypeID is just the address of the storage byte. */
typedef struct { const void *opaque; } TypeID;

static inline TypeID class_name_typeid(void) {
    return (TypeID){ &kClassNameTypeIDStorage };
}
```

Upstream MLIR spells the same shape as `TypeID::get<T>()` returning
`&detail::TypeIDResolver<T>::id`, with the static `id` field defined inside the resolver
specialisation. The `inline static` discipline plus the C++17 inline-variable rule guarantees one
storage instance per program. Inside a single executable that is enough.

The properties this idiom relies on:

- The address of a static-storage object is a link-time constant within one binary or DSO.
- The C++ standard guarantees one storage instance per `static` variable defined in an
  `inline`/`constexpr` context, which the linker enforces by COMDAT-merging duplicate definitions.
- Hot dispatch becomes `MOV` plus `CMP` — load `op->kindPtr`, compare against a sentinel address
  baked into the dispatcher arm. No string compare, no hash, no atomic load.

The cost is that two independently-loaded DSOs each get their own sentinel for the same C++ class
if visibility is hidden — exactly the failure mode that motivates Idiom 2. tileiras avoids this by
statically linking the dialects that use Idiom 1 into one image, so every Idiom-1 sentinel is a
link-time constant in the same `.bss` slab. The address bands listed in
[TypeID Sentinels and Anchors — Idiom 1](typeid-sentinels-and-anchors.md#idiom-1--static-pointer-identity-sentinel)
show the result: each owning dialect or category gets one contiguous slab of one-byte sentinels at
an 8-byte pitch.

## Idiom 2 — `__PRETTY_FUNCTION__` String Interning

The second idiom builds the TypeID out of the **interned address of a C++ type-name string**.
The string is produced by the compiler's `__PRETTY_FUNCTION__` macro inside a template, captured
verbatim, and looked up in a process-wide string pool the first time the accessor runs. The pool's
returned pointer is the TypeID, and a Meyers-style cache stores it for subsequent calls.

```c
/* The intern pool — one per process, owned by MLIRContext / a ManagedStatic.
 * In tileiras's binary this is sub_44A6CA0; upstream MLIR ships it as
 * SelfOwningTypeID::resolveTypeID under llvm::ManagedStatic. */
extern const void *intern_typeid_string(const char *rtti_name, size_t len);

/* Per-class lazy accessor. The compiler bakes __PRETTY_FUNCTION__ at the call
 * site, which expands to something like:
 *   "const void *typeid_string<mlir::FunctionOpInterface>() [T = ...]"
 * The slice between the angle brackets is what the interner keys on. In the
 * tileiras image the captured slice is the suffix ending in ']'. */
const void *typeid_meyers_cached_FunctionOpInterface(void) {
    static uint8_t  guard  = 0;      /* one byte, Itanium ABI guard */
    static uint64_t cached = 0;      /* qword that ends up holding TypeID */

    if (__builtin_expect(guard == 0, 0)) {
        if (__cxa_guard_acquire(&guard) != 0) {
            cached = (uint64_t)intern_typeid_string(
                "mlir::FunctionOpInterface]", 26);
            __cxa_guard_release(&guard);
        }
    }
    return (const void *)cached;
}
```

The `__PRETTY_FUNCTION__` trick is the cross-compiler-stable way to get a textual name for a C++
type without RTTI. GCC and Clang both emit the unmangled, human-readable form, including template
arguments. MLIR's `TypeID::getFromOpaquePointer<T>()` machinery captures the slice between two
fixed markers (`[T = ` and the closing `]`) and passes the result to the interner.

The properties this idiom relies on:

- Two DSOs that instantiate `TypeID::get<Foo>()` produce **byte-identical** `__PRETTY_FUNCTION__`
  strings, because the compiler generates them from the same C++ type expression. Hidden
  visibility doesn't affect string contents.
- The interner is one process-wide table. Both DSOs find or insert the same row, and the row's
  address is the TypeID. Cross-DSO identity holds even without `STB_WEAK`.
- The interned string survives for the life of the context, so the cached qword remains valid
  forever.

The cost is one branch on the cached qword, one atomic load on the guard, and a one-time string
lookup. Idiom 1 is strictly faster — one less indirection, no atomic — but Idiom 2 is the only
choice when the same C++ type must yield the same TypeID across statically-linked DSOs or across
arbitrary template instantiations whose storage cannot be named at link time.

## Why Each Idiom Is Used

| Decision axis              | Idiom 1 — Static sentinel              | Idiom 2 — Interned string             |
|----------------------------|----------------------------------------|---------------------------------------|
| Cross-DSO identity         | Breaks under hidden visibility         | Stable; relies on string equality     |
| Anonymous-namespace types  | Per-TU storage, per-TU identity        | Stable; string contents are well-defined |
| Cost on hot path           | One load, one compare                  | One load (cached qword), one compare  |
| Cost on first call         | Zero — address is link-time constant   | Guard acquire, string intern, qword store |
| Storage shape in `.bss`    | 1-byte slot at 8-byte alignment        | `{u8 guard, u64 qword}` pair, 9 bytes |
| Created                    | Before `main` (link-time addresses)    | First call after dialect load          |
| Tileiras tenants           | Dialects, concrete Types/Attributes, per-op opInfo, per-op kindPtr | Op/Type/Attr interfaces, registered analyses, pattern RTTI tags |

Idiom 1 is reserved for objects that exist before `main`. Their identities are link-time constants
and the linker packs them into dense bands one slab per owning dialect — the
`&unk_5B38B[B0..C8]`, `&unk_5B48D[88..F8]`, `&unk_5B49A[98..B18]`, and the larger NVVM op slab at
`0x5B8D610..0x5B8DCB8` are the visible result.

Idiom 2 is reserved for objects whose existence depends on a runtime registration step. Op and
Type interfaces are attached via `addInterfaces<>` calls made well after dialect construction;
analyses are keyed by C++ type and instantiated on first request through the `AnalysisManager`;
pattern RTTI tags exist only for patterns that explicitly opt into RTTI. None of these have a
link-time storage owner — the runtime has to derive the identity from the C++ type alone.

## A TypeID Never Moves Between Idioms

Once a class is assigned to one idiom by its declaration site, every install site and every
dispatcher uses the same idiom. There is no fallback path from Idiom 1 to Idiom 2 or vice versa.
The two pools never collide because their address bands never overlap — Idiom 1 sentinels are
one-byte storage objects at 8-byte alignment within `.bss` slabs the linker emits per dialect,
while Idiom 2 qwords are part of `{guard, qword}` pairs that the C++ compiler scatters at the
declaration sites of the Meyers accessors. Both are stable for the lifetime of the
`MLIRContext`, which is what the binary-searched `InterfaceMap` and the pointer-equality dispatch
both depend on.

## QUIRK — The captured slice ends in `]`

The string interned by Idiom 2 in tileiras's binary always ends in `]`, even though the
human-eye-friendly version of the type name would end with the type itself. This is the closing
bracket of `__PRETTY_FUNCTION__`'s `[T = ...]` slot, captured along with the type name. A binary
triage that searches for the string `"mlir::FunctionOpInterface"` (without the trailing bracket)
will not find the literal in `.rodata`. Search for `"mlir::FunctionOpInterface]"` instead, with
the bracket; that is the byte sequence the interner sees and the string the comparator hashes.
The 9-class table in
[TypeID Sentinels and Anchors — Idiom 2](typeid-sentinels-and-anchors.md#idiom-2--meyers-cached-typeid)
preserves the bracket on every row for exactly this reason.

## QUIRK — Idiom 1 sentinels are 1-byte storage, but the slab pitch is 8 bytes

Each Idiom-1 sentinel is conceptually a `char` — one byte that nobody ever reads. The linker
nonetheless allocates an 8-byte slot per sentinel so that the next sentinel's address remains
8-byte-aligned. This is a side effect of how MLIR declares the storage (`inline static char id`
inside a class that has 8-byte members elsewhere) plus the linker's default alignment. A
disassembler scanning the slab will see runs of `00 00 00 00 00 00 00 00` between every used byte;
those aren't padding bytes in a meaningful sense, but they are not addressable as sentinels
either. The address of the *first* byte of each 8-byte slot is the TypeID; the trailing seven
bytes are unused.

## QUIRK — The Meyers guard byte must come before the qword

The Itanium C++ ABI's `__cxa_guard_acquire` machinery expects a 64-bit guard variable, but the
compiler is free to allocate the guard byte separately from the cached value. tileiras's binary
consistently places the guard byte at `qword_addr - 8`, immediately before the 8-byte cached
slot, with the qword 8-byte-aligned. A reimplementation that places the guard *after* the qword
(or interleaves multiple guards in front of one qword) breaks the steady-state load pattern that
every interface-using dispatcher in the binary assumes. The fast-path body is `cmp byte ptr
[guard], 0` followed by `mov rax, qword ptr [qword]` — if the guard byte sits anywhere other than
`[qword - 8]` the loader generates a different sequence and the cache-line locality argument
breaks.

## Cross-References

- [TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) — where in the tileiras image
  the two idioms physically land, the null-opinfo guard, and the dispatch-by-pointer-identity
  pattern in `sub_7ACC40` and the other load-store classifiers.
- [TypeID Sentinel Address Table](../reference/typeid-sentinel-table.md) — the address-sorted
  enumeration of every sentinel referenced anywhere in the binary, with idiom-form (1-byte
  pointer-identity vs 8-byte Meyers qword vs 9-byte guard+qword pair) attached to each row.
- [Interface Vtables](interface-vtables.md) — the `InterfaceMap` that performs binary search
  against the TypeID addresses produced by both idioms, including the 16-byte entry pitch and
  the binary-search invariants.
- [Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) — the registration
  machinery that installs both idioms during dialect load, plus the relationship between Idiom-1
  sentinels and the `UniquedStorage` slab.
- [Operation Layout](operation-layout.md) — the op header that holds the kindPtr at
  `*(qword*)(op+48)+16`, which is the read every dispatcher performs before comparing against an
  Idiom-1 or Idiom-2 sentinel.
