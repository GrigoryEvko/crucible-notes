# Interface Vtables and Dispatch

## Abstract

MLIR interfaces let generic code ask semantic questions about a dialect object without knowing its
concrete C++ class. In tileiras every concrete op, type, attribute, and dialect carries a sorted
array of `(TypeID, concept*)` pairs — its `InterfaceMap` — and `lookup` is a 16-byte-pitch binary
search keyed on the TypeID address documented in
[TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md). The concept block is a small vtable
whose first slot is always the concept's own destructor and whose remaining slots are the methods
the interface defines; layout-type interfaces expose shape and coordinate-mapping callbacks, view
types expose element type and memory space, copy-atom types expose value shape and copied bits, and
MMA-atom types expose A/B/C types plus the verifier callback. This page documents the entry
layouts, the binary-search lookup, and the registration shim that installs implementations during
dialect load.

## InterfaceMap Layout

Each object that participates in interface dispatch (op, type, attribute, dialect) carries one
`InterfaceMap`: a flat sorted array of 16-byte entries — the same pitch the `OperationName` slot
banks use — sorted ascending on the TypeID address.

```c
typedef struct InterfaceEntry {        /* 16 B */
    /*+0x00*/ TypeID         *id;      /* interned TypeID sentinel — Idiom 1 or Idiom 2 */
    /*+0x08*/ InterfaceConcept *concept; /* small vtable; first slot is the concept's dtor */
} InterfaceEntry;

typedef struct InterfaceMap {
    /*+0x00*/ InterfaceEntry *entries;
    /*+0x08*/ uint32_t        size;     /* live count; entries are dense, no tombstones */
    /*+0x0C*/ uint32_t        cap;
} InterfaceMap;
```

No tombstones: interfaces are write-once after dialect load. A registration phase runs during
`addInterfaces<>` and the resulting map is never mutated again for the life of the context. The sort
key is the raw TypeID address, and binary search is well-defined because Idiom-1 sentinels are
link-time constants and Idiom-2 qwords are Meyers-cached on first use — every address compared
against is stable by the time lookup runs.

## Concept Vtable Shape

An `InterfaceConcept` is a small vtable. Slot 0 is the concept's destructor (so the map can free
the concept on context teardown); the remaining slots are the per-interface methods in declaration
order. Slot counts vary by interface — copy-atom carries 4 methods, MMA-atom 5, layout-type 6 — but
the prefix is fixed.

```c
typedef struct InterfaceConcept {
    /*+0x00*/ void (*dtor)(InterfaceConcept *self);   /* concept teardown, always populated */
    /*+0x08*/ void (*method0)(/* iface-specific signature */);
    /*+0x10*/ void (*method1)(/* iface-specific signature */);
    /* ... */
} InterfaceConcept;
```

The method-pointer entries are per-implementer thunks generated at dialect-init time. They adapt
the generic `(void *concrete, ...)` calling convention used by the dispatch helper to the concrete
C++ member function on the implementing class. Dialect conversion's
`populateConvertToLLVMConversionPatterns` and the per-type `printAssembly` callbacks both follow
this shape.

## Lookup Algorithm

Lookup is a stride-16 binary search over the entries array. On a miss it returns `NULL`, which every
caller treats as "capability not supported":

```c
InterfaceConcept *interface_map_lookup(const InterfaceMap *map, TypeID *id) {
    uint32_t lo = 0, hi = map->size;
    while (lo < hi) {
        uint32_t mid = lo + ((hi - lo) >> 1);
        TypeID  *k   = map->entries[mid].id;
        if (k == id) return map->entries[mid].concept;   /* hit — pointer-identity */
        if ((uintptr_t)k < (uintptr_t)id) lo = mid + 1;
        else                              hi = mid;
    }
    return NULL;                                          /* miss — capability absent */
}
```

The comparator is raw pointer order, not a structural property of the interface — correct because
insert sorts by the same address comparison. Insert holds the invariant with a one-shot
`lower_bound` plus an in-place memmove:

```c
void interface_map_register(InterfaceMap *map, TypeID *id, InterfaceConcept *concept) {
    uint32_t pos = lower_bound_typeid(map, id);
    if (pos < map->size && map->entries[pos].id == id) {
        /* replace — dialects may rebind an interface */
        map->entries[pos].concept = concept;
        return;
    }
    if (map->size == map->cap) grow_entries(map);         /* arena-backed; doubles cap */
    memmove(&map->entries[pos + 1], &map->entries[pos],
            (map->size - pos) * sizeof(InterfaceEntry));
    map->entries[pos].id      = id;
    map->entries[pos].concept = concept;
    ++map->size;
}
```

Replace-in-place is rare but legal — a downstream dialect can override an interface installed by an
upstream dialect. A target dialect, for example, may rebind `ConvertToLLVMInterface` for a base
op-class that the standard dialect already registered.

## Dispatch Helper

Every generic caller funnels through the same one-line dispatch helper to ask "does this object
implement this interface, and if so, run method N":

```c
InterfaceConcept *get_interface(void *concrete, TypeID *id) {
    InterfaceMap *map = object_interface_map(concrete);   /* per-class accessor */
    return interface_map_lookup(map, id);
}

/* Typical call site — generic code asks for a capability without knowing the class. */
unsigned get_num_warps(Operation *op) {
    InterfaceConcept *c = get_interface(op, &qword_AgentLikeOpInterface_TypeID);
    if (c == NULL) return 0;
    typedef unsigned (*GetNumWarpsFn)(Operation *);
    GetNumWarpsFn fn = (GetNumWarpsFn)((void **)c)[1];    /* slot 1 — first real method */
    return fn(op);
}
```

`((void**)concept)[0]` is always the concept destructor; `((void**)concept)[1]` is the first declared
method. A caller that knows the interface knows the slot index of the method it needs — there is no
runtime name-to-slot resolution.

## Dialect Interfaces

Dialect interfaces describe behavior owned by an entire dialect rather than a single operation or
type.

| Interface | Contract |
| --- | --- |
| Assembly interface | Provides aliases and preferred SSA names for dialect types and attributes. |
| Inliner interface | Decides whether operations, regions, and blocks may be inlined. |
| Convert-to-LLVM interface | Populates conversion patterns for dialect lowering to LLVM. |

```c
void populate_llvm_patterns(Context *ctx, RewritePatternSet *patterns) {
    for (Dialect *dialect : loaded_dialects(ctx)) {
        ConvertToLLVMInterface *iface = dialect_interface(dialect, CONVERT_TO_LLVM);

        if (iface != NULL) {
            iface->populate_patterns(dialect, patterns);
        }
    }
}
```

## Type Interfaces

Tileiras uses type interfaces to make layout and target-specific atom types composable.

| Interface | Typical implementers | Questions answered |
| --- | --- | --- |
| Layout type | `cute.layout`, composed layouts | Shape, body, static-ness, coordinate mapping. |
| View type | `cute.ptr`, `cute.memref` | Element type, memory space, rank, effective layout. |
| Pointer type | pointer-like CuTe types | Memory space and alignment. |
| Iterator type | descriptor and iterator types | Element type, layout refinement, projection. |
| Copy atom type | copy atom descriptors | Value shape, layouts, copied bits, value type. |
| MMA atom type | SM-specific MMA atoms | A/B/C types, shape, operand partitioning, verifier rules. |
| Printable type | public textual types | Stable type printer implementation. |

```c
LogicalResult verify_view_type(Type type) {
    ViewTypeInterface *view = dyn_cast_view_interface(type);

    if (view == NULL) {
        return failure("expected a view type");
    }
    if (view->rank(type) == 0) {
        return failure("view type must have at least one dimension");
    }

    return success();
}
```

Interfaces let verifiers stay declarative. A pass asks for "view rank" or "MMA atom shape" without
knowing every concrete type class that implements the concept.

## Operation Interfaces

Operation interfaces express control-flow, scheduling, and producer-consumer behaviour. Each is a
concept vtable installed against per-op TypeIDs at op-class registration time.

| Interface | Contract |
| --- | --- |
| Producer op | Names the region that produces async pipeline data. |
| Region branch terminator | Describes where a region terminator can transfer values. |
| Agent-like op | Exposes agent bodies, group size, and warp allocation. |
| Constant-like trait | Marks an op as a constant for folding and canonicalization. |

Marker traits are zero-method interfaces — a single-slot vtable carrying only the destructor, with
"present" determined by `interface_map_lookup` returning non-NULL.

## How to Recognize in a Binary

The dispatch helper is the cleanest fingerprint: a function that loads an `InterfaceMap*` from a
per-class slot, runs a stride-16 binary search keyed on a sentinel address from one of the bands
catalogued in [TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md), and returns either
`NULL` or a small vtable pointer is an `interface_map_lookup` call.

Concrete fingerprints:

- The `qword_5B47028` (`PrintableTypeInterface`) and `qword_5B44600` (`LayoutTypeInterface`) Meyers
  slots from the TypeID page are the most frequent keys passed to lookup; any function that loads
  one of those qwords and then performs a stride-16 search is dispatching against the cute /
  cute_nvgpu interface set.
- The replace-in-place behaviour distinguishes interface maps from operation-name slot banks.
  Operation-name banks are write-once on dialect registration; interface maps occasionally see a
  `pos < size && entries[pos].id == id` replace. A function that does the equality probe and then
  conditionally rewrites `entries[pos].concept` is registering an override.
- The concept dtor at slot 0 is the cleanest concept-vs-arbitrary-vtable distinguisher. A 5-slot or
  6-slot vtable whose slot 0 is a small function ending in a `free` or arena-discard call, with the
  rest being type-dispatched method thunks, is an `InterfaceConcept`.

## Verifier Use

A verifier checking an interface-bearing operand should name the missing capability, never the
implementation class.

```c
LogicalResult verify_copy_atom(Type atom) {
    CopyAtomTypeInterface *iface = dyn_cast_copy_atom(atom);

    if (iface == NULL) {
        return failure("expected a copy atom type");
    }

    if (iface->copy_bits(atom) == 0) {
        return failure("copy atom must move at least one bit");
    }

    return success();
}
```

This wording stays stable even if a later version adds new atom classes.

## Consumers

Every generic pass in the binary that asks "does this object support capability X" routes through
the dispatch helper above. Pattern application drivers in
[Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) consult `ConvertToLLVMInterface` per
dialect during conversion-pattern population. Verifiers consult type-side interfaces such as
`LayoutTypeInterface` and `ViewTypeInterface` per operand. The scheduler consults
`AgentLikeOpInterface` and `LoopLikeOpInterface` to identify region-bearing producer/consumer
boundaries. The diagnostic engine documented in
[Diagnostic ABI and Helpers](diagnostic-abi-and-helpers.md) does not depend on the interface map
directly, but verifiers that emit diagnostics universally key their messages on the missing
interface name rather than on a concrete class.

## Cross-References

[TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) catalogues the sentinel addresses
this map keys on. [Operation Layout](operation-layout.md) describes the operation header that owns
the per-op `InterfaceMap`. [Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md)
documents the dialect-registration machinery that installs interface implementations on context
load.
