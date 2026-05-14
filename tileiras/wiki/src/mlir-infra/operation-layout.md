# MLIR Operation Layout

## Abstract

Every MLIR `Operation*` in tileiras is a fixed 0x48-byte header followed by a contiguous
`TrailingObjects` run — inline result storage, the operand slab, regions, successors, and the
attribute-dictionary slot. The header is constant across every dialect linked into the binary
(`cuda_tile`, `nv_tileas`, `nv_tileaa`, `cute`, `cute_nvgpu`, `cutlass`, `nvvm`, `llvm`); per-op
variation lives entirely in the trailing area. Kind dispatch is a single pointer-identity compare
against the interned `OperationName` slot at `+0x40`, the operand count is masked with `0x7FFFFFF`
on every load, and the canonical `TrailingObjects` decoder at `sub_4492630` is seven lines of
arithmetic with one branch.

## Fixed Header

```c
typedef struct Operation {
    /*+0x00*/ Block            *block;                  // parent block, ilist owner
    /*+0x08*/ Region           *regions_inline;         // first inline region (single-region ops)
    /*+0x10*/ Operation        *parent;                 // parent operation, IRObjectWithUseList base
    /*+0x18*/ OpOperand        *first_use;              // head of this op's result use-list
    /*+0x20*/ OperandStorage   *operand_storage;        // pointer to OpOperand[] / resizable slab
    /*+0x28*/ uint32_t          num_operands;           // walkers AND with 0x7FFFFFF before use
    /*+0x2C*/ uint16_t          num_results : 23;       // low 23 bits = numResults
    /*+0x2D*/ uint16_t          flags        : 9;       // upper bits: trailing-result + dialect flags
    /*+0x2E*/ uint8_t           trailing_result_flag;   // = (flags & 0x80) >> 7, 16-byte stride gate
    /*+0x2F*/ uint8_t           num_inline_results;     // small-count slot, scaled by 8 in decoder
    /*+0x38*/ Location          loc;                    // source location pointer
    /*+0x40*/ OperationName     name;                   // interned pointer — identity dispatch key
} Operation;                                            // sizeof == 0x48
```

Kind dispatch lives in one field: `+0x40`. The `OperationName` there is an interned record
pointer whose *address* identifies the op kind — not its mnemonic, not a hash. Every walker,
canonicalizer, and verifier in the binary compares `op->name` to entries in the `&unk_5B44...` /
`&unk_5B45...` / `&unk_5BE6...` slot banks with a plain `MOV`+`CMP`. No string compare, no hash
lookup, no indirect call on the hot path. Slot interning is documented in
[storage-uniquer-and-context-impl.md](storage-uniquer-and-context-impl.md); the sentinel records
themselves are catalogued in [typeid-sentinels-and-anchors.md](typeid-sentinels-and-anchors.md).

Every load of `num_operands` at `+0x28` is followed by a mask against `0x7FFFFFF`. The upper five
bits carry per-op flags — bit `0x4000000` is `HasDebugValue`, bit `0x40` is `NoFPExcept`, the rest
dialect-specific. That 27-bit mask appears verbatim in 28 distinct functions and is the single
sharpest fingerprint for MLIR-walker sites in stripped tileiras code: load a `DWORD` at `+0x28`,
AND it with `0x7FFFFFF`, and you are operating on a `mlir::Operation`.

## TrailingObjects Decoder

The canonical decoder lives at `sub_4492630`. Seven lines of source, one branch, one alignment
round-up:

```c
uintptr_t getTrailingStorage(const Operation *op) {
    uintptr_t hdr      = (uintptr_t)op;
    uint8_t   trailing = *((const uint8_t  *)op + 0x2E) >> 7;                   // 0 or 1
    uint8_t   n_inline = *((const uint8_t  *)op + 0x2F);                        // small-count
    uintptr_t base     = (hdr + 16 * trailing + 8 * n_inline + 64 + 7) & ~(uintptr_t)7;
    uint32_t  n_ops    = *(const uint32_t *)((const char *)op + 0x28) & 0x7FFFFFF;
    return base + 32 * n_ops;
}
```

The `+64` term is `sizeof(Operation header) - 8` rounded into the alignment math; combined with the
`& ~7` mask it lands on the first 8-byte boundary after the inline-result prologue. The 32-byte
stride applied to `n_ops` is `sizeof(OpOperand)` — forward link, backward link, owning operation
pointer, and `Value`, eight bytes each. The decoder returns the address of the region slab; callers
that want operands, successors, or the attribute dictionary subtract the appropriate stride.

Trailing storage follows canonical upstream order. Inline results lead when `trailing_result_flag
== 0`; otherwise an outline-result prologue (one 8-byte cell per `num_inline_results`) precedes the
operand slab. The slab follows, aligned to 8 bytes. After `32 * num_operands` bytes of `OpOperand`
storage come `24 * num_regions` bytes of `Region` slots (the stride is confirmed by the `for ( i =
v4 + 24 * v5; i != v4; v4 += 24 )` walk in `sub_7C6150`), then `24 * num_successors` bytes of
`BlockOperand`, then the trailing attribute-dictionary pointer. `sub_4492630` itself never decodes
the split between `num_regions`, `num_successors`, and the upper flag bits of `+0x2C`; that lives in
the per-op accessors the ODS generator inlines into each builder.

```c
struct OpOperand {                  // sizeof == 32
    /*+0x00*/ Value      value;     // SSA operand, tagged pointer
    /*+0x08*/ OpOperand *next;      // ilist next in def's use-list
    /*+0x10*/ OpOperand *prev;      // ilist prev
    /*+0x18*/ Operation *owner;     // back-pointer to enclosing Operation
};
```

The `(num_results | flags)` packed word at `+0x2C` doubles as the "has any result"
gate: `sub_4492630` returns zero outright when `(*(uint32_t *)(op + 44) & 0x7FFFFF) == 0`, because a
result-less operation cannot have inline-result storage and the trailing area starts at the
aligned-up boundary anyway. The walker at `sub_44924B0` uses the same `0x7FFFFF` probe to decide
whether to descend through trailing objects. Both functions encode an identical contract: zero
results means the trailing area is degenerate and callers must compute their own bases.

## Walker Contract

`sub_447FBB0` is `mlir::detail::walk_impl` — 1242 LOC, lock-free, the iteration backbone behind
every walker vtable in the binary. No `pthread_mutex_lock` or `pthread_rwlock_*` call appears in
its body; it reads the 0x48-byte `Operation` header directly. The MLIR rule is single-threaded
walks, with concurrent passes using separate `MLIRContext` instances — which is why a walker
descending through 100k+ ops compiles down to such tight code.

The walker maintains a worklist of 40-byte frames on a stack-allocated `SmallVector`. Each frame
holds the current op, the user-supplied visitor vtable, the next region and block cursors, a phase
discriminator that distinguishes pre-order entry from child descent and post-order exit, and a
skip/interrupt flag word:

```c
typedef struct WalkFrame {
    /*+0x00*/ Operation        *op;             // current op
    /*+0x08*/ const WalkVisitor *visitor;       // user-supplied vtable
    /*+0x10*/ Region           *region_cursor;  // next region to walk
    /*+0x18*/ Block            *block_cursor;
    /*+0x20*/ uint32_t          phase;          // 0=pre, 1=children, 2=post
    /*+0x24*/ uint32_t          flags;          // skip/interrupt
} WalkFrame;                                    // 40 B
```

Dispatch is two levels of indirection through the visitor at frame `+0x08`. The walker reads the
visitor's vtable, then loads the per-op callback at slot `+64`, matches the loaded function pointer
against the op's interned `name` at header `+0x40`, and calls through. Pre-order entry and post-order
exit go through slots `+16` and `+0x24` of the same vtable. The pattern
`*((void**)(*(void**)(frame[1])) + 64)` is the fingerprint that identifies a walker callback site in
stripped tileiras code:

```c
typedef uint32_t (*WalkCallback)(Operation *op);

uint32_t dispatch_per_op(const WalkFrame *frame) {
    const WalkVisitor *v   = frame->visitor;                                 // frame[+0x08]
    void *const       *vt  = *(void *const *const *)v;                       // visitor->vtable
    WalkCallback       cb  = (WalkCallback)vt[8];                            // vtable + 64 == slot 8
    return cb(frame->op);                                                    // resolves via op->name (+0x40)
}
```

The binary ships three walker-vtable instantiations: `sub_4481140` is the bare driver — the
canonical 7-LOC tail — `sub_4481150` is the kind-filtered driver, and `sub_4481220` is the
post-order driver. Each is a 3-slot vtable shaped `{enter, leave, perOp}`; the bare driver is the
smallest body and the cleanest reference for reimplementation.

The visitor callback returns one of four control words. `0` is continue — descend into regions and
blocks, then run post-order. `1` is skip-children — run post-order on the current frame but push no
child frames. `2` is interrupt — pop every frame and return to the caller. `3` is re-visit, used by
fixed-point rewrites to re-run the per-op callback after children complete. The verifier wires its
first-error path to the interrupt return, so a single failed invariant unwinds the entire walk in
one pop sweep.

The walker reads exactly four fields from the header on each iteration: `name` at `+0x40` for kind
dispatch, the 27-bit operand count via `*(uint32_t *)(op + 0x28) & 0x7FFFFFF`, the first inline
region pointer at `+0x08`, and the interned-slot identity used by the kind-filtered driver — again
through `name` at `+0x40`, compared pointer-equal against the `&unk_5B...` slot bank. All four reads
are plain `MOV`+`CMP`; no string compare, no hash, and no indirect call fires until the per-op
callback at vtable `+64` is invoked.

## Pointer-Identity Dispatch

`sub_447FBB0` is the canonical example of MLIR kind dispatch in this binary. It loads `op->name`
from `+0x40` and compares the pointer against entries in the slot banks at `&unk_5B44...`,
`&unk_5B45...`, and `&unk_5BE6...`. Each comparison is one `MOV` plus one `CMP`; the dispatch tree
is a chain of conditional branches over interned addresses, with no string compare and no vtable
lookup on the fast path. Pattern matching over MLIR ops in stripped tileiras code is cheap enough to
inline into hot canonicalizers because the kind check fits in two instructions and the
trailing-object decoder fits in seven.

Pattern-matching helpers wrap the same idiom. The `isa<OpT>` shape from `pattern-vtables-and-shapes`
loads `+0x40`, compares against the interned slot for `OpT::getOperationName()`, and falls through
to a no-op or into the matched-pattern body. Diagnostic helpers such as `sub_446EC50`
(`Operation::emitOpError`) reach the same field to spell the op's mnemonic in the error prefix
before returning a builder for the caller to append to.

## Accessor Map

The accessor surface in the binary maps cleanly onto upstream MLIR methods, with the canonical
offset loads shown below.

| Binary thunk     | Upstream equivalent                                              | Offsets read                          |
|------------------|------------------------------------------------------------------|---------------------------------------|
| `sub_446E0D0`    | `Operation::getOperation()` — identity thunk                     | returns `a1`                          |
| `sub_446E0E0`    | `Operation::getOperation() const` overload                       | returns `a1`                          |
| `sub_4492630`    | `TrailingObjectsImpl::getTrailingObjects<Region>()`              | `+0x28`, `+0x2C`, `+0x2E`, `+0x2F`    |
| `sub_44924B0`    | `Operation::walk()` body, descends through trailing objects      | `+0x2C` masked with `0x7FFFFF`        |
| `sub_446EC50`    | `Operation::emitOpError()` — diagnostic builder                  | `+0x40` (OperationName)               |
| `sub_44499A0`    | MLIRContext DenseMap probe (operation-name / TypeID lookup)      | context table, not header             |
| `sub_447FBB0`    | walker / pattern driver, lock-free                               | `+0x40` against sentinel slot banks   |

The two `getOperation` identity thunks exist so that templated ODS code can call
`op.getOperation()` uniformly whether `op` is a concrete `OpT` wrapper or a raw `Operation*`. Both
return their argument verbatim.

## Invariants

The 0x48-byte size is fixed by the upstream MLIR contract and is not negotiable for any dialect that
participates in the shared infrastructure. The five fields that any reimplementation must place at
the documented offsets are `num_operands` at `+0x28`, the packed `(num_results | flags)` word at
`+0x2C` (with the trailing-result bit at `+0x2E`, bit 7), the inline-result count at `+0x2F`, and
the interned `OperationName` pointer at `+0x40`. The 27-bit operand mask must be `0x7FFFFFF`; the
32-byte `OpOperand` stride and the 64-byte alignment base in `sub_4492630` follow from the upstream
`IROperand<OpOperand, Value>` layout and the `TrailingObjects` alignment policy.

## How to Recognize in a Binary

Three independent fingerprints identify the Operation header path:

- The 27-bit mask `0x7FFFFFF` AND-ed with a 32-bit load from `+0x28` of any object is the most
  distinctive single signature. The mask appears verbatim in 28 distinct functions; any function
  that performs this load-and-mask is operating on a `mlir::Operation`.
- The seven-line `getTrailingStorage` shape at `sub_4492630` — `(hdr + 16*trailing + 8*n_inline + 64
  + 7) & ~7` followed by `32 * n_ops` — identifies the canonical trailing-object decoder. The 32-byte
  stride is `sizeof(OpOperand)` and the `& ~7` mask is the alignment policy.
- The visitor-vtable callback site `*((void**)(*(void**)(frame[1])) + 64)` from the walker body is
  the third fingerprint. Two indirections followed by a `+64` offset (slot 8 of a 3-slot vtable) is
  always a per-op walker callback.

## Consumers

Every walker, verifier, canonicaliser, and pattern matcher in the binary reads this header. The
walker driver `sub_447FBB0` is the iteration backbone documented above; the storage uniquer in
[Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) is the source of the
`OperationName` pointer at `+0x40`; the pattern application drivers in
[Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) read the `+0x40` slot to dispatch
matching patterns; and the diagnostic constructor at `sub_446EC50`
([Diagnostic ABI and Helpers](diagnostic-abi-and-helpers.md)) reads `+0x40` to spell the op
mnemonic in error prefixes.

Cross-references: [storage-uniquer-and-context-impl.md](storage-uniquer-and-context-impl.md) for
how `OperationName` slots are interned; [iseldag-and-matchertable.md](../codegen/iseldag-and-matchertable.md)
for the same `0x7FFFFFF` mask reused on backend `SDNode`; and
[typeid-sentinels-and-anchors.md](typeid-sentinels-and-anchors.md) for the slot bank that backs
`+0x40`.
